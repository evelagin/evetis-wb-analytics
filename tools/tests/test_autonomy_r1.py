"""AE-R1: канонические сигналы, детерминированный классификатор, область задачи и лимит диффа,
независимый retest, вердикты ревьюера, политика вывода (B3), идентичность публикатора (B2).

Отрицательные случаи из owner ACK: отсутствие данных у маркетплейса, устаревший ФФ, неутверждённый
план, ручные операции и файлы вне allowlist НЕ дают инженерного кандидата PR.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import ae_fixtures as F  # noqa: E402 — добавляет корень репозитория в sys.path
from tools.autonomy import classifier
from tools.autonomy import gatekeeper as G
from tools.autonomy.agents import ScriptedAdapter
from tools.autonomy.evidence import (RetestRunner, offline_prefix, parse_junit, profiles_for, reconcile,
                                     run_profile)
from tools.autonomy.orchestrator import Orchestrator
from tools.autonomy.output_policy import (DataExposureError, ensure_public, find_data, fingerprint, mask_data,
                                          public_tail, sanitize_engineer_report)
from tools.autonomy.policy import diff_size, load_policy, out_of_scope, scope_violations, task_scope, tcb_paths
from tools.autonomy.publisher import GitPublisher, PublishRefused
from tools.autonomy.schema import require_valid
from tools.autonomy.signals import canonical_signals, fixture_fetcher
from tools.autonomy.state import StateStore
from tools.autonomy.watcher import watch

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "quality/autonomy/examples/signals.sheets_503_2026-10-07.json"
POLICY = load_policy()
SHA = "a" * 40


def signals(doc: dict | None = None) -> list[dict]:
    if doc is None:
        return canonical_signals(fixture_fetcher(FIXTURE), POLICY)
    return canonical_signals(lambda sql: _fake(doc, sql), POLICY)


def _fake(doc: dict, sql: str):
    for name, marker in (("incidents", "OPS_INCIDENT"), ("ledger", "V_RUN_FAILURE_LEDGER"),
                         ("health", "V_DATA_HEALTH_CURRENT")):
        if marker in sql:
            if name in doc.get("fail", []):
                raise RuntimeError(name)
            return doc.get(name, [])
    raise AssertionError(sql)


def ledger(**over) -> dict:
    row = {"source_log": "LOADER_RUNS", "loader_name": "unitka", "environment": "prod", "error_code": "SHEETS_API",
           "failure_signature": "TRANSIENT_UPSTREAM", "recorded_as_transient": False,
           "message_fingerprint": "78fbf2aedfbacf71", "occurrences_7d": 1, "occurrences_30d": 1,
           "first_seen_at": "2026-10-07T09:30:47Z", "last_seen_at": "2026-10-07T09:30:47Z",
           "last_run_id": "prod:unitka:x", "recovery_status": "RECOVERED", "minutes_to_recovery": 81}
    row.update(over)
    return {"kind": "run_failure", "key": "runfail:x", **row}


def health(**over) -> dict:
    row = {"pipeline_id": "p", "serving_status": "STALE", "data_status": "MISSING", "reason_code": "SLOT_MISSED",
           "data_class": "EVENT_HISTORY", "source_system": "WB_API", "evaluation_mode": "EVALUATED"}
    row.update(over)
    return {"kind": "dro_health", "key": "dro:p:x", **row}


# =================================================================== классификатор ===
@pytest.mark.parametrize("sig, expected", [
    (ledger(), "RETRY_CLASSIFIER_DEFECT"),                                         # кейс 07.10
    (ledger(error_code="LOADER_ERROR"), "RETRY_CLASSIFIER_DEFECT"),                 # обёртка без классификации
    (ledger(error_code="MONTH_SECTION_INVALID"), "NOT_ENGINEERING"),                # временная сигнатура, код бизнеса
    (ledger(error_code="SOME_NEW_CODE"), "UNCLASSIFIED"),                           # временная сигнатура, код вне списка
    (ledger(error_code="ADS_TIMEOUT", recorded_as_transient=True), "NOT_ENGINEERING"),  # распознан правильно
    (ledger(error_code="SHEETS_API_TRANSIENT"), "NOT_ENGINEERING"),                 # код сам объявил временным
    (ledger(error_code="FRESHNESS_GATE", failure_signature="OTHER"), "NOT_ENGINEERING"),   # бизнесовый гейт
    (ledger(error_code="SOURCE_STALE", failure_signature="OTHER"), "NOT_ENGINEERING"),
    (ledger(error_code="X", failure_signature="AUTH"), "NOT_ENGINEERING"),
    (ledger(error_code="X", failure_signature="QUOTA"), "NOT_ENGINEERING"),
    (ledger(error_code="WB_PRICES_SHAPE", failure_signature="OTHER", occurrences_7d=3), "SCHEMA_DRIFT"),
    (ledger(error_code="WB_PRICES_SHAPE", failure_signature="OTHER"), "UNCLASSIFIED"),          # однократно
    (ledger(error_code="WB_PRICES_BAD_JSON", failure_signature="OTHER"), "UNCLASSIFIED"),       # обрезанный ответ
    (ledger(error_code="WB_T6_PARSE", failure_signature="SCHEMA", occurrences_7d=4), "SCHEMA_DRIFT"),
    (ledger(error_code="X", failure_signature="SCHEMA"), "UNCLASSIFIED"),           # сигнатура без кода из списка
    (ledger(error_code="FUTURE_LEAKAGE", failure_signature="OTHER", occurrences_7d=3), "PARITY_DEFECT"),
    (ledger(error_code="FUTURE_LEAKAGE", failure_signature="OTHER", occurrences_7d=1), "UNCLASSIFIED"),
    # INVARIANT_FAIL сообщает и пустое окно данных, и расхождение источников (второе ревью, HIGH-1)
    (ledger(error_code="INVARIANT_FAIL", failure_signature="OTHER", occurrences_7d=9), "UNCLASSIFIED"),
    (ledger(error_code="DUP_KEY", failure_signature="OTHER", occurrences_7d=3), "LOADER_DEFECT"),
    (ledger(error_code="DUP_KEY", failure_signature="OTHER", occurrences_7d=2), "UNCLASSIFIED"),
    # обобщённые обёртки без временной сигнатуры смысла не несут — никогда не LOADER_DEFECT
    (ledger(error_code="MART_ERROR", failure_signature="OTHER", occurrences_7d=9), "UNCLASSIFIED"),
    (ledger(error_code="ENGINE_ERROR", failure_signature="OTHER", occurrences_7d=9), "UNCLASSIFIED"),
    (ledger(error_code="LOADER_ERROR", failure_signature="OTHER", occurrences_7d=9), "UNCLASSIFIED"),
    # Apps Script (INGEST_RUNS) — доверенная база, задачи AE не бывает
    (ledger(source_log="INGEST_RUNS", loader_name="sales", error_code="LOADER_ERROR"), "NOT_ENGINEERING"),
    (ledger(source_log="WB_PRICES_OBSERVATIONS", error_code="LOADER_ERROR"), "UNCLASSIFIED"),
    (ledger(error_code="SOME_NEW_CODE", failure_signature="OTHER", occurrences_7d=9), "UNCLASSIFIED"),  # fail closed
    (ledger(error_code=None, failure_signature="OTHER"), "UNCLASSIFIED"),
    # реальные коды состояния данных/бизнеса — никогда не инженерные, даже повторяясь (независимое ревью H1)
    (ledger(error_code="WB_PRICES_EMPTY", failure_signature="OTHER", occurrences_7d=3), "NOT_ENGINEERING"),
    (ledger(error_code="NEXT_MONTH_SECTION_MISSING", failure_signature="OTHER", occurrences_7d=3), "NOT_ENGINEERING"),
    (ledger(error_code="STOCK_SNAPSHOT_MISSING", failure_signature="OTHER", occurrences_7d=5), "NOT_ENGINEERING"),
    (ledger(error_code="WB_PROMO_AUTH", failure_signature="OTHER", occurrences_7d=3), "NOT_ENGINEERING"),
    (ledger(error_code="INTEGRITY_DATA_ERROR", failure_signature="PARITY_QA", occurrences_7d=4), "NOT_ENGINEERING"),
    (ledger(error_code="RECON_CANCELS_DECREASE_REQUIRES_ACK", failure_signature="OTHER", occurrences_7d=4), "NOT_ENGINEERING"),
    (ledger(error_code="LCD_HELD_BY_COVERAGE_GAP", failure_signature="OTHER", occurrences_7d=4), "NOT_ENGINEERING"),
    (ledger(error_code="RETRY_LOCK_UNAVAILABLE", failure_signature="OTHER", occurrences_7d=4), "NOT_ENGINEERING"),
    (health(), "NOT_ENGINEERING"),                                                  # данных нет (SLOT_MISSED)
    (health(reason_code="DATA_LOSS_CONFIRMED", serving_status="BLOCKED"), "NOT_ENGINEERING"),
    (health(reason_code="NO_DATA_OBSERVED", serving_status="UNKNOWN"), "NOT_ENGINEERING"),
    (health(reason_code="FRESHNESS_STALE", data_class="MANUAL", source_system="MANUAL"), "NOT_ENGINEERING"),  # ФФ
    (health(reason_code="RUN_PARTIAL", data_class="MANUAL"), "NOT_ENGINEERING"),    # план продаж / ручная операция
    (health(reason_code="RUN_FAILED"), "UNCLASSIFIED"),                             # диагноз — только по журналу
    (health(reason_code="DETECTOR_STALE", serving_status="UNKNOWN"), "UNCLASSIFIED"),  # может быть паузой планировщика
    (health(reason_code="SOMETHING_NEW"), "UNCLASSIFIED"),
    ({"kind": "mystery"}, "UNCLASSIFIED"),
])
def test_classifier_rules(sig, expected):
    d = classifier.classify(sig, POLICY["signals"]["recurrence_threshold_7d"])
    assert d["task_class"] == expected, d
    assert d["engineering"] is (expected in classifier.ENGINEERING)


def test_business_and_marketplace_conditions_never_become_engineering():
    for sig in (health(), health(reason_code="DATA_LOSS_CONFIRMED"), health(data_class="MANUAL"),
                health(pipeline_id="ff_stock", data_class="MANUAL", reason_code="FRESHNESS_STALE"),
                health(pipeline_id="sales_plan", data_class="MANUAL", reason_code="RUN_FAILED"),
                ledger(error_code="FRESHNESS_GATE", failure_signature="OTHER"),
                ledger(error_code="MANUAL_OVERRIDE_ACTIVE", failure_signature="TRANSIENT_UPSTREAM"),
                ledger(error_code="WB_PRICES_POSTCOUNT_EMPTY", failure_signature="OTHER", occurrences_7d=10)):
        assert not classifier.classify(sig)["engineering"], sig


# ======================================================================= наблюдатель ===
def test_sheets_503_fixture_dispatches_exactly_one_retry_classifier_task(tmp_path):
    rep = watch(signals(), StateStore(tmp_path / "state"), SHA, tmp_path / "out")
    assert rep["status"] == "ACTION" and rep["llm_invocations"] == 0
    assert len(rep["dispatch"]) == 1
    cand = [r for r in rep["results"] if r["class"] == "ENGINEERING_CANDIDATE"]
    assert [r["task_class"] for r in cand] == ["RETRY_CLASSIFIER_DEFECT"]
    assert cand[0]["loader_name"] == "unitka" and cand[0]["error_code"] == "SHEETS_API"
    obj = json.loads(Path(rep["dispatch"][0]["objective_path"]).read_text())
    require_valid(obj, "objective")
    require_valid(obj["incident"], "incident")
    assert obj["task_class"] == "RETRY_CLASSIFIER_DEFECT"
    assert obj["scope"]["allowed_paths"] == POLICY["task_classes"]["classes"]["RETRY_CLASSIFIER_DEFECT"]["allowed_paths"]
    assert obj["incident"]["evidence"]["signal"]["message_fingerprint"] == "78fbf2aedfbacf71"
    # B3: исходного текста ошибки, URL и данных в цели нет
    text = json.dumps(obj, ensure_ascii=False)
    assert "currently unavailable" not in text.lower() and "spreadsheets" not in text and "ZZ_CONFIG" not in text
    ensure_public(text)
    # отрицательные строки той же фикстуры — без задач
    classes = {r["key"].split(":")[1]: r["class"] for r in rep["results"]}
    assert classes["ff_stock"] == classes["sales_plan"] == "NOT_ENGINEERING"
    assert classes["wb_finance"] == classes["ozon_finance_accrual"] == "NOT_ENGINEERING"
    assert classes["unitka_wb"] == "HEALTHY"


def test_only_non_engineering_signals_create_no_objective(tmp_path):
    doc = json.loads(FIXTURE.read_text())
    doc["ledger"] = [r for r in doc["ledger"] if r["error_code"] != "SHEETS_API"]
    out = tmp_path / "out"
    rep = watch(signals(doc), StateStore(tmp_path / "state"), SHA, out)
    assert rep["dispatch"] == [] and not (out.exists() and list(out.glob("*.json")))
    assert "ENGINEERING_CANDIDATE" not in {r["class"] for r in rep["results"]}


@pytest.mark.parametrize("fail", [["health"], ["ledger"], ["incidents"]])
def test_unreadable_source_is_infra_blocked_not_healthy(tmp_path, fail):
    doc = {**json.loads(FIXTURE.read_text()), "fail": fail}
    rep = watch(signals(doc), StateStore(tmp_path / "state"), SHA, tmp_path / "out")
    assert "INFRA_BLOCKED" in {r["class"] for r in rep["results"]}
    assert rep["status"] in ("INFRA_BLOCKED", "ACTION")


def test_stale_detector_snapshot_is_infra_blocked(tmp_path):
    doc = json.loads(FIXTURE.read_text())
    for r in doc["health"]:
        r["detector_age_minutes"] = 500
    doc["ledger"] = []
    rep = watch(signals(doc), StateStore(tmp_path / "state"), SHA, tmp_path / "out")
    assert rep["status"] == "INFRA_BLOCKED" and rep["dispatch"] == []


def test_stale_detector_is_one_aggregated_signal_and_never_a_task(tmp_path):
    """Пауза планировщика DRO (документированный откат) не размножается в задачи по конвейерам."""
    doc = json.loads(FIXTURE.read_text())
    for r in doc["health"]:
        r["reason_code"], r["serving_status"] = "DETECTOR_STALE", "UNKNOWN"
    doc["health"].append({**doc["health"][0], "pipeline_id": "x_not_evaluated", "evaluation_mode": "NOT_EVALUATED"})
    doc["ledger"] = []
    store = StateStore(tmp_path / "state")
    for _ in range(3):
        rep = watch(signals(doc), store, SHA, tmp_path / "out")
    dro = [r for r in rep["results"] if r["key"].startswith("dro:")]
    assert [r["key"] for r in dro] == ["dro:detector:DETECTOR_STALE"] and dro[0]["class"] == "UNCLASSIFIED"
    assert rep["dispatch"] == [] and "INFRA_BLOCKED" in {r["class"] for r in rep["results"]}


def test_same_ledger_row_is_dispatched_once(tmp_path):
    store = StateStore(tmp_path / "state")
    first = watch(signals(), store, SHA, tmp_path / "out")
    second = watch(signals(), store, SHA, tmp_path / "out")
    assert len(first["dispatch"]) == 1 and second["dispatch"] == []
    row = next(r for r in second["results"] if r["class"] == "ENGINEERING_CANDIDATE")
    assert row["dispatch"] in ("already_processed",) or row["dispatch"].startswith("already_active")
    doc = json.loads(FIXTURE.read_text())
    doc["ledger"][0]["last_seen_at"] = "2026-10-09T09:30:47Z"          # новый отказ — новая задача
    assert len(watch(signals(doc), store, SHA, tmp_path / "out")["dispatch"]) == 1


def test_legacy_check_failure_without_classification_is_not_dispatched(tmp_path):
    store = StateStore(tmp_path / "state")
    obs = [{"key": "check:X_NEW", "kind": "check", "check_id": "X_NEW", "suite": "wb_core", "status": "FAIL"}]
    watch(obs, store, SHA, tmp_path / "out")
    rep = watch(obs, store, SHA, tmp_path / "out")
    assert rep["results"][0]["class"] == "UNCLASSIFIED" and rep["dispatch"] == []


def test_cli_watch_signals_fixture(tmp_path, capsys):
    from tools.autonomy.cli import main
    assert main(["watch", "--state-dir", str(tmp_path / "s"), "--out", str(tmp_path / "o"),
                 "--signals-fixture", str(FIXTURE)]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert len(rep["dispatch"]) == 1 and rep["dispatch"][0]["deduplication_key"].startswith("runfail:unitka:SHEETS_API")


# ========================================================== область задачи и лимит ===
def test_task_scope_comes_from_policy_not_from_objective():
    wide = {"task_class": "RETRY_CLASSIFIER_DEFECT", "scope": {"allowed_paths": ["**"], "max_changed_lines": 99999,
                                                               "max_files": 999}}
    s = task_scope(wide)
    assert "**" not in s["allowed_paths"] and s["max_changed_lines"] <= POLICY["diff_limits"]["max_changed_lines"]
    assert task_scope({}) is None
    assert task_scope({"task_class": "NOT_ENGINEERING"}) is None
    assert task_scope({"task_class": "SYNTHETIC_FIXTURE"}) is None                       # без synthetic-инцидента
    assert task_scope({"task_class": "SYNTHETIC_FIXTURE", "incident": {"source": "synthetic"}}) is not None
    assert task_scope({"commissioning": {"iteration_marker": "AE-COMMISSIONING-ABCD"}})["allowed_paths"] == \
        ["tools/tests/test_ae_commissioning_canary.py"]


def test_out_of_allowlist_and_oversized_diff_are_violations():
    s = task_scope({"task_class": "RETRY_CLASSIFIER_DEFECT"})
    assert out_of_scope(["cloud/src/failure.ts", "cloud/test/failure.test.ts"], s) == []
    assert out_of_scope(["cloud/src/failure.ts", "sql/mart/x.sql"], s) == ["sql/mart/x.sql"]
    patch = "diff --git a/cloud/src/failure.ts b/cloud/src/failure.ts\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n" + "+x\n" * 400
    assert diff_size(patch) == {"changed_lines": 400, "files": 1, "binary": 0}
    # удалённые SQL-комментарии внутри ханка («--- …» в диффе) считаются, а не принимаются за заголовок
    sql = "diff --git a/sql/a.sql b/sql/a.sql\n--- a/sql/a.sql\n+++ b/sql/a.sql\n@@ -1,500 +1 @@\n" + "--- old\n" * 500
    assert diff_size(sql)["changed_lines"] == 500
    binary = "diff --git a/cloud/test/fixtures/x.bin b/cloud/test/fixtures/x.bin\nGIT binary patch\nliteral 3\n"
    assert any(v.startswith("BINARY_CHANGE") for v in scope_violations(["cloud/test/fixtures/x.bin"], binary, s))
    v = scope_violations(["cloud/src/failure.ts"], patch, s)
    assert any(x.startswith("DIFF_TOO_LARGE") for x in v)
    assert scope_violations(["docs/x.md"], "", s)[0].startswith("SCOPE_OUT_OF_ALLOWLIST")
    assert "fail closed" in scope_violations(["cloud/src/failure.ts"], "", None)[0]


def test_tcb_paths_stay_tcb_even_inside_a_class_allowlist():
    for f in (".github/workflows/x.yml", "infra/terraform/iam.tf", "cloud/package.json", "cloud/vitest.config.ts",
              "cloud/.eslintrc.json", "cloud/tsconfig.json", "tools/autonomy/policy.py"):
        assert tcb_paths([f]) == [f], f


GOOD = {"tests": [{"name": "python: pytest", "status": "PASS"}], "sql_validation": {"status": "PASS"},
        "runtime_access": {"status": "NOT_APPLICABLE"}, "parity": {"status": "NOT_APPLICABLE"},
        "data_suites": {}, "objective_resolution": "NOT_APPLICABLE"}


def review(v="APPROVE", findings=(), tv="VERIFIED", tests=("cloud/test/cli_retry.test.ts",)):
    return {"verdict": v, "summary": "s", "findings": list(findings), "gate_weakening_detected": False,
            "missing_negative_tests": [], "human_decision_reason": None,
            "test_verification": {"status": tv, "relevant_tests": list(tests), "basis": "b"}}


def ctx(**over):
    base = {"scope_violations": [], "scope_present": True, "test_provenance": "RECONCILED",
            "evidence_disagreement": [], "code_changed": True,
            "known_test_paths": {"cloud/test/cli_retry.test.ts", "cloud/src/failure.ts"}}
    base.update(over)
    return base


def finding(sev):
    return {"severity": sev, "category": "correctness", "summary": "x", "evidence": "y"}


@pytest.mark.parametrize("rev, c, verdict, fixable", [
    (review(), ctx(), "READY_FOR_PR", False),
    (review("APPROVE_WITH_NITS", [finding("MINOR")]), ctx(), "READY_FOR_PR", False),
    (review("PASS"), ctx(), "READY_FOR_PR", False),                                   # устаревшее имя
    (review("APPROVE_WITH_NITS", [finding("MAJOR")]), ctx(), "INCONCLUSIVE", True),
    (review("CHANGES_REQUIRED", [finding("MAJOR")]), ctx(), "INCONCLUSIVE", True),
    (review("BLOCK", [finding("BLOCKER")]), ctx(), "BLOCKED_BY_REVIEW", False),
    (review("UNPROVEN"), ctx(), "INCONCLUSIVE", False),
    (review("BLOCKED"), ctx(), "INCONCLUSIVE", False),                                # устаревшее → UNPROVEN
    (review(tv="INSUFFICIENT"), ctx(), "INCONCLUSIVE", True),
    (review(tests=("cloud/test/nonexistent.test.ts",)), ctx(), "INCONCLUSIVE", True),
    (review(tests=("cloud/test/cli_retry.test.ts > retries 503 > once",)), ctx(), "READY_FOR_PR", False),  # id vitest
    ({k: v for k, v in review().items() if k != "test_verification"}, ctx(), "INCONCLUSIVE", True),  # старый вердикт
    (review(tv="NOT_APPLICABLE"), ctx(), "INCONCLUSIVE", True),
    (review(), ctx(scope_violations=["SCOPE_OUT_OF_ALLOWLIST (X): ['sql/x.sql']"]), "HUMAN_DECISION_REQUIRED", True),
    (review(), ctx(scope_violations=["DIFF_TOO_LARGE: 900 > 300"]), "HUMAN_DECISION_REQUIRED", True),
    (review(), ctx(scope_violations=["класс задачи не определён"], scope_present=False), "HUMAN_DECISION_REQUIRED", False),
    (review(), ctx(test_provenance="UNTRUSTED_ONLY"), "INCONCLUSIVE", False),
    (review(), ctx(evidence_disagreement=["python: недоверенный PASS ≠ retest FAIL"]), "UNSAFE", False),
    (review(), {}, "HUMAN_DECISION_REQUIRED", True),                                 # нет контекста — fail closed
])
def test_gatekeeper_r1_semantics(rev, c, verdict, fixable):
    r = G.evaluate(GOOD, GOOD, rev, c)
    assert r["verdict"] == verdict, r["reasons"]
    assert (r["review_requests_changes"] or r["scope_fixable"]) is fixable, r


# ======================================================== оркестратор (стенд) ===
@pytest.fixture()
def env(tmp_path):
    repo, sha = F.make_repo(tmp_path)
    return {"repo": repo, "sha": sha, "store": StateStore(tmp_path / "state"), "out": tmp_path / "out",
            "sandboxes": tmp_path / "sandboxes"}


def synthetic_objective(env) -> dict:
    watch(F.observations("FAIL"), env["store"], env["sha"], env["out"], synthetic=True)
    rep = watch(F.observations("FAIL"), env["store"], env["sha"], env["out"], synthetic=True)
    return json.loads(Path(rep["dispatch"][0]["objective_path"]).read_text())


def orch(env, eng, rev):
    from tools.autonomy.publisher import GitPublisher as P
    return Orchestrator(env["store"], env["repo"], eng, rev, F.SyntheticEvidenceRunner(), env["sandboxes"],
                        audit=F.clean_audit, publisher=P(env["repo"], dry_run=True), verifier=F.ci_verifier(),
                        trusted_base_ref="main")


def edit_outside_allowlist(ws: Path) -> None:
    F.edit_fix_with_extra_test(ws)
    (ws / "docs").mkdir(exist_ok=True)
    (ws / "docs" / "note.md").write_text("заметка вне области\n")


def test_candidate_outside_allowlist_never_becomes_ready(env):
    eng = ScriptedAdapter({"engineer_plan": [{"respond": F.plan()}],
                           "engineer_implement": [{"edit": edit_outside_allowlist, "respond": F.implemented()}] * 3})
    rev = ScriptedAdapter({"reviewer": [{"respond": F.verdict("PASS")}] * 3})
    o = orch(env, eng, rev)
    run = o.advance(o.submit(synthetic_objective(env))[0]["run_id"])
    assert run["state"] != "READY_FOR_HUMAN_REVIEW" and run.get("pr_url") is None
    assert "READY_FOR_PR" not in [t["to"] for t in run["transitions"]]
    assert run["state"] == "WAITING_FOR_HUMAN" and "SCOPE_OUT_OF_ALLOWLIST" in run["transitions"][-1]["reason"]
    assert "TESTING" not in [t["to"] for t in run["transitions"]]       # код вне области не исполнялся


def test_plan_outside_allowlist_waits_for_owner(env):
    eng = ScriptedAdapter({"engineer_plan": [{"respond": F.plan(files=("synthetic/calc.py", "sql/mart/x.sql"))}]})
    o = orch(env, eng, ScriptedAdapter({}))
    run = o.advance(o.submit(synthetic_objective(env))[0]["run_id"])
    assert run["state"] == "WAITING_FOR_HUMAN" and "SCOPE_OUT_OF_ALLOWLIST" in run["transitions"][-1]["reason"]


def test_reviewer_gets_no_engineer_report_and_engineer_narrative_is_fingerprinted(env):
    secret_story = "выручка 123 456 ₽ у покупателя Иванов, заказ 438775437"
    plan = {**F.plan(), "summary": secret_story, "root_cause": secret_story}
    impl = {**F.implemented(), "summary": secret_story, "root_cause": secret_story, "uncertainty": [secret_story]}
    eng = ScriptedAdapter({"engineer_plan": [{"respond": plan}],
                           "engineer_implement": [{"edit": F.edit_fix_with_extra_test, "respond": impl}]})
    rev = ScriptedAdapter({"reviewer": [{"respond": F.verdict("APPROVE")}]})
    o = orch(env, eng, rev)
    run = o.advance(o.submit(synthetic_objective(env))[0]["run_id"])
    assert run["state"] == "READY_FOR_HUMAN_REVIEW", run["transitions"]
    assert secret_story not in rev.calls[0]["prompt"] and "Иванов" not in rev.calls[0]["prompt"]
    assert '"allowed_paths"' in rev.calls[0]["prompt"]
    state_text = "".join(p.read_text(encoding="utf-8") for p in env["store"].root.rglob("*") if p.is_file()
                         and p.suffix in (".json", ".md"))
    assert "Иванов" not in state_text and "438775437" not in state_text and "123 456" not in state_text
    from tools.autonomy.report import render_report
    body = render_report(run, env["store"].root / "artifacts" / run["run_id"])
    assert "Иванов" not in body and "sha256:" in body
    ensure_public(body)


# =============================================================== политика вывода ===
@pytest.mark.parametrize("text, kind", [
    ("выручка 12 345,67 ₽", "money"), ("₽ 500", "money"), ("1500 руб.", "money"), ("120 rub", "money"),
    ("выкуп 54,3 %", "percent"), ("nm 438775437", "long_number"), ("a@b.ru", "email"),
    ("+7 (912) 345-67-89", "phone"), ("https://x.ru/a?token=1", "url_query"),
    ('{"f":[{"v":"1"}]}', "bq_row"), ("1;2;3;4;5", "table_row"),
])
def test_data_patterns(text, kind):
    assert kind in find_data(text)
    assert kind not in find_data(mask_data(text))
    with pytest.raises(DataExposureError):
        ensure_public(text)


def test_public_metadata_passes():
    for ok in ("run-20260927T121235Z-ab2e1c53", "sha256 `634910ace8f7d8eb`", "итераций 2 · циклов ревью 1",
               "cloud/test/cli_retry.test.ts: **PASS** (JUnit 12 тестов)", "2026-10-07T09:30:47Z", "$0.80"):
        assert ensure_public(ok) == ok


def test_fingerprint_is_idempotent_and_report_sanitized():
    fp = fingerprint("секрет")
    assert re.fullmatch(r"\[sha256:[0-9a-f]{16};chars:6\]", fp) and fingerprint(fp) == fp
    rep = sanitize_engineer_report({**F.implemented(), "uncertainty": ["x 1 000 ₽"]},
                                   POLICY["output_policy"]["engineer_narrative_fields"])
    assert rep["status"] == "CANDIDATE_READY" and rep["changed_files"] == ["synthetic/calc.py"]
    assert all(v.startswith("[sha256:") for v in rep["uncertainty"]) and rep["summary"].startswith("[sha256:")
    require_valid(rep, "engineer_report")
    assert sanitize_engineer_report(rep, POLICY["output_policy"]["engineer_narrative_fields"]) == rep


def test_public_tail_keeps_only_test_ids():
    tail = ("FAILED tools/tests/test_x.py::test_money - assert 123456 == 0\nвыручка 1 000 ₽\n"
            " FAIL  cloud/test/cli_retry.test.ts > retries 503\n")
    pt = public_tail(tail)
    assert pt["failed_tests"] == ["tools/tests/test_x.py::test_money", "cloud/test/cli_retry.test.ts"]
    assert "123456" not in json.dumps(pt) and pt["fingerprint"].startswith("[sha256:")


def test_engineer_cli_stdout_is_fingerprinted_in_diagnostics():
    from tools.autonomy import diagnostics as D
    inv = D.new_invocation("engineer_implement")
    inv.update(stdout_tail='{"result": "выручка 12 345 ₽"}', stderr_tail="ошибка 438775437", summary="ok")
    _, doc = D.finalize(D.bundle("engineer", "run-20261008T000000Z-aaaaaaaa", [inv]))
    i = doc["invocations"][0]
    assert i["stdout_tail"].startswith("[sha256:") and "438775437" not in i["stderr_tail"]


# ===================================================== тесты кандидата и retest ===
def test_parse_junit(tmp_path):
    (tmp_path / "a.xml").write_text('<testsuites><testsuite tests="3" failures="1" errors="0" skipped="1"/>'
                                    '<testsuite tests="2" failures="0" errors="0"/></testsuites>')
    assert parse_junit(tmp_path / "a.xml") == {"tests": 5, "failures": 1, "errors": 0, "skipped": 1}
    (tmp_path / "b.xml").write_text('<testsuites tests="7" failures="0" errors="0"><testsuite tests="7"/></testsuites>')
    assert parse_junit(tmp_path / "b.xml")["tests"] == 7
    assert parse_junit(tmp_path / "missing.xml") is None


def test_exit_zero_without_junit_is_not_a_pass(tmp_path, monkeypatch):
    """os._exit(0) из теста кандидата: код 0, отчёта нет — FAIL, а не PASS."""
    import sys
    prof = {"commands": [["{python}", "-c", "import os; os._exit(0)", "--junit={junit}"]]}
    monkeypatch.setenv("AE_FORCE_NETWORK_ISOLATION", "off")
    t = run_profile("python", prof, tmp_path, sys.executable, [])
    assert t[0]["status"] == "FAIL" and t[0]["reason"] == "JUNIT_MISSING_OR_EMPTY"
    ok = {"commands": [["{python}", "-c",
                        "import sys; open(sys.argv[1],'w').write('<testsuite tests=\"1\" failures=\"0\" errors=\"0\"/>')",
                        "{junit}"]]}
    assert run_profile("python", ok, tmp_path, sys.executable, [])[0]["status"] == "PASS"


def test_profiles_follow_changed_files_and_class():
    assert profiles_for(["cloud/src/failure.ts"], ["python", "cloud"], POLICY) == ["python", "cloud"]
    assert profiles_for(["cloud/src/failure.ts"], ["python"], POLICY) == ["python"]
    assert profiles_for(["pipelines/ozon/runtime/x.py"], ["python", "cloud", "ozon"], POLICY) == ["python", "ozon"]
    cloud = POLICY["test_profiles"]["cloud"]
    assert cloud["install"][:2] == ["npm", "ci"] and "--ignore-scripts" in cloud["install"]
    assert ["npm", "run", "typecheck"] in cloud["commands"] and any("vitest" in c for c in map(" ".join, cloud["commands"]))


def test_retest_flags_disappearing_tests(tmp_path, monkeypatch):
    import tools.autonomy.evidence as E
    def fake(name, prof, ws, py, pfx, env=None):
        n = 10 if ws.name == "base" else 7
        return [{"name": f"{name}: t", "status": "PASS", "exit_code": 0,
                 "junit": {"tests": n, "failures": 0, "errors": 0, "skipped": 0}, "tail": ""}]
    monkeypatch.setattr(E, "run_profile", fake)
    monkeypatch.setenv("AE_FORCE_NETWORK_ISOLATION", "off")
    (tmp_path / "base").mkdir(); (tmp_path / "cand").mkdir()
    doc = RetestRunner().collect(tmp_path / "base", tmp_path / "cand", ["cloud/src/failure.ts"],
                                 {"task_class": "RETRY_CLASSIFIER_DEFECT"})
    assert doc["profiles"] == ["python", "cloud"] and doc["network_isolation"] == "NOT_ENFORCED"
    assert all(t["status"] == "FAIL" and t["reason"].startswith("TESTS_DISAPPEARED") for t in doc["tests"])


def test_reconcile():
    ev = {**GOOD, "tests": [{"name": "a", "status": "PASS"}]}
    assert reconcile(ev, None)["test_provenance"] == "UNTRUSTED_ONLY"
    ok = reconcile(ev, {"tests": [{"name": "a", "status": "PASS"}], "network_isolation": "SUDO_UNSHARE"})
    assert ok["test_provenance"] == "RECONCILED" and ok["evidence_disagreement"] == []
    bad = reconcile(ev, {"tests": [{"name": "a", "status": "FAIL"}]})
    assert bad["test_provenance"] == "DISAGREEMENT" and bad["evidence_disagreement"]
    assert bad["tests"] == [{"name": "a", "status": "FAIL"}]            # тесты — из retest, не из недоверенного


def test_offline_prefix_can_be_forced_off(monkeypatch):
    monkeypatch.setenv("AE_FORCE_NETWORK_ISOLATION", "off")
    assert offline_prefix() == ([], "NOT_ENFORCED")


# ============================================================== публикатор (B2) ===
def test_default_publisher_identity_is_github_app_and_refuses_before_any_write(tmp_path):
    pub = GitPublisher(tmp_path)
    assert pub.identity.name == "github-app"
    with pytest.raises(PublishRefused, match="B2"):
        pub.publish({"branch": "ae/x-12345678"}, "", tmp_path, ["ci.yml"])
    assert pub.log == []
    with pytest.raises(PublishRefused):
        GitPublisher(tmp_path, identity="pat")


# ========================================================= workflows и SQL ===
def _wf(name: str) -> str:
    return (REPO / ".github/workflows" / name).read_text(encoding="utf-8")


def test_retest_job_has_no_credentials():
    from tools.tests.test_autonomy_security import jobs
    body = jobs(_wf("autonomy-test.yml"))["retest"]
    assert "id-token: write" not in body and "google-github-actions/auth" not in body and "contents: read" in body
    assert "write" not in body.split("steps:")[0]
    assert "collect-retest" in body


def test_verify_step_receives_retest_and_publish_uses_app_identity():
    run = _wf("autonomy-run.yml")
    assert "retest_artifact:" in run and "retest.json=" in run
    assert "--publisher-identity github-app" in run
    assert "retest_artifact" in _wf("autonomy-gate.yml")


def test_run_failure_ledger_sql_exposes_no_message_text():
    sql = (REPO / "sql/health/dro1_07_run_failure_ledger.sql").read_text(encoding="utf-8")
    assert "CREATE OR REPLACE VIEW `evetis_health.V_RUN_FAILURE_LEDGER`" in sql
    final = sql[sql.rindex("SELECT"):]
    assert "error_message" not in final and "msg" not in final
    for col in ("message_fingerprint", "failure_signature", "recorded_as_transient", "occurrences_7d",
                "recovery_status", "minutes_to_recovery"):
        assert col in final, col
    rollback = (REPO / "sql/health/dro1_99_rollback.sql").read_text(encoding="utf-8")
    assert "DROP VIEW IF EXISTS `evetis_health.V_RUN_FAILURE_LEDGER`" in rollback


def test_fixture_is_public_safe():
    text = FIXTURE.read_text(encoding="utf-8")
    ensure_public(json.dumps(json.loads(text)["ledger"], ensure_ascii=False))
    assert "spreadsheets" not in text and "ZZ_CONFIG" not in text


# ========================================== находки независимого ревью (повторный проход) ===
def test_legacy_review_without_test_verification_is_schema_valid():
    legacy = {k: v for k, v in review("PASS").items() if k != "test_verification"}
    require_valid(legacy, "review_verdict")


@pytest.mark.parametrize("line", ["+  it.fails('x', () => {})", "-  it('keeps total', () => {",
                                  "+  it.only('x', () => {})", "+  describe.skip('x', () => {})", "+  test.todo('x')",
                                  "+    pytest.skip('later')", "+np = pytest.importorskip('numpy')", "+  xit('x', f)"])
def test_new_test_weakening_patterns(line):
    from tools.autonomy.policy import detect_gate_weakening
    patch = f"diff --git a/cloud/test/a.test.ts b/cloud/test/a.test.ts\n+++ b/cloud/test/a.test.ts\n{line}\n"
    assert detect_gate_weakening(patch, ["cloud/test/a.test.ts"])


def test_patch_data_scan():
    from tools.autonomy.policy import patch_data_findings
    code = ("diff --git a/cloud/src/failure.ts b/cloud/src/failure.ts\n@@ -1 +1 @@\n"
            "+const TIMEOUT_MS = 120000; // 50% запаса\n")
    assert patch_data_findings(code) == []                          # число и процент в КОДЕ — не данные
    fixture = ("diff --git a/cloud/test/fixtures/r.json b/cloud/test/fixtures/r.json\n@@ -0,0 +1 @@\n"
               '+{"nm_id": 438775437, "revenue": "12 345 ₽"}\n')
    hits = patch_data_findings(fixture)
    assert hits and "long_number" in hits[0] and "money" in hits[0]
    mail = "diff --git a/cloud/src/x.ts b/cloud/src/x.ts\n@@ -0,0 +1 @@\n+const who = 'buyer@mail.ru';\n"
    assert patch_data_findings(mail)
    assert patch_data_findings("diff --git a/a b/a\nBinary files a/a and b/a differ\n")[0].startswith("BINARY")


def edit_with_data_fixture(ws: Path) -> None:
    F.edit_fix_with_extra_test(ws)
    (ws / "tests_synthetic" / "fixture.json").write_text('{"revenue": "12 345 ₽", "nm_id": 438775437}\n')


def test_candidate_with_data_is_not_persisted(env):
    eng = ScriptedAdapter({"engineer_plan": [{"respond": F.plan(files=("synthetic/calc.py", "tests_synthetic/test_calc.py",
                                                                       "tests_synthetic/fixture.json"))}],
                           "engineer_implement": [{"edit": edit_with_data_fixture, "respond": F.implemented()}]})
    o = orch(env, eng, ScriptedAdapter({}))
    run = o.advance(o.submit(synthetic_objective(env))[0]["run_id"])
    assert run["state"] == "WAITING_FOR_HUMAN" and "PATCH_DATA" in run["transitions"][-1]["reason"]
    art = env["store"].root / "artifacts" / run["run_id"]
    assert not (art / "candidate.patch").exists()
    state_text = "".join(p.read_text(encoding="utf-8") for p in env["store"].root.rglob("*.json"))
    assert "438775437" not in state_text and "12 345" not in state_text


def test_replayed_evidence_keeps_only_known_keys(tmp_path):
    from tools.autonomy.agents import sha256_file
    from tools.autonomy.evidence import ReplayEvidenceRunner
    ev = {**GOOD, "changed_files": ["a.py"], "leak": {"rows": [{"revenue": 1}]},
          "sql_validation": {"status": "PASS", "tail": "выручка 12 345 ₽"}}
    f = tmp_path / "evidence.json"; f.write_text(json.dumps(ev, ensure_ascii=False))

    class Imp:
        def impact(self, repo, files):
            return {"risk_tier": None}
    out = ReplayEvidenceRunner(f, sha256_file(f), Imp(), tmp_path).collect(tmp_path, ["a.py"], {})
    assert "leak" not in out and "12 345" not in json.dumps(out, ensure_ascii=False)
    assert out["test_provenance"] == "UNTRUSTED_ONLY"


def test_isolation_prefix_drops_privileges():
    import inspect
    from tools.autonomy import evidence as E
    src = inspect.getsource(E.offline_prefix)
    for flag in ("--no-new-privs", "--inh-caps=-all", "--bounding-set=-all", "--clear-groups"):
        assert flag in src
    assert "--init-groups" not in src and "_PROBE_NO_SUDO" in src
    assert "docker.sock" in E._PROBE_NO_SUDO


# ========================================== находки второго независимого ревью ===
def test_path_with_space_cannot_bypass_data_scan_or_scope():
    from tools.autonomy.policy import patch_data_findings
    patch = ('diff --git a/cloud/src/loaders/x y.ts b/cloud/src/loaders/x y.ts\n@@ -0,0 +1 @@\n'
             "+const who = 'buyer@mail.ru'; // 12 345 ₽\n")
    hits = patch_data_findings(patch)
    assert any(h.startswith("PATCH_PATH_UNPARSEABLE") for h in hits) and any("email" in h for h in hits)
    s = task_scope({"task_class": "RETRY_CLASSIFIER_DEFECT"})
    assert any(v.startswith("PATH_NOT_ALLOWED") for v in scope_violations(["cloud/src/loaders/x y.ts"], "", s))


def test_parity_class_sql_paths_all_require_owner_ack():
    from tools.autonomy.policy import glob_match
    ack = POLICY["plan_ack"]["requires_ack_path_globs"]
    sql_globs = [g for g in POLICY["task_classes"]["classes"]["PARITY_DEFECT"]["allowed_paths"] if g.startswith("sql/")]
    for g in sql_globs:
        probe = g.replace("**/", "a/").replace("*", "x")
        assert any(glob_match(probe, a) for a in ack), g
    for path in ("sql/unitka/x.sql", "sql/promo/x.sql", "sql/pricing/x.sql", "sql/ops/x.sql"):
        assert any(glob_match(path, a) for a in ack), path
