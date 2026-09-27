"""Модульные тесты детерминированных решений AE v1: схемы, политика, гейткипер, состояния."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import ae_fixtures as F  # noqa: E402
from tools.autonomy import gatekeeper as G
from tools.autonomy.policy import detect_gate_weakening, forbidden_paths, glob_match, plan_requires_ack, tcb_paths
from tools.autonomy.publisher import GitPublisher, PublishRefused
from tools.autonomy.schema import SchemaError, check_schema, load_schema, require_valid, validate
from tools.autonomy.state import StateStore, TransitionError, dedup_slug

EXAMPLES = F.REPO / "quality" / "autonomy" / "examples"


# ------------------------------------------------------------------ схемы ---
@pytest.mark.parametrize("name", ["incident", "objective", "engineer_report", "review_verdict", "run_state"])
def test_every_schema_tree_uses_only_supported_keywords(name):
    check_schema(load_schema(name))


def test_validator_rejects_unknown_schema_keyword_instead_of_ignoring_it():
    with pytest.raises(SchemaError):
        validate({"a": 1}, {"type": "object", "properties": {"a": {"type": "integer", "multipleOf": 2}}})


def test_example_incident_is_valid():
    require_valid(json.loads((EXAMPLES / "incident.synthetic.json").read_text()), "incident")


@pytest.mark.parametrize("mutate", [
    lambda d: d.pop("deduplication_key"),
    lambda d: d.update(deduplication_key="free text with spaces"),
    lambda d: d.update(repository_sha="main"),
    lambda d: d.update(triggering_checks=[]),
    lambda d: d.update(created_at="вчера"),
    lambda d: d.update(unexpected="field"),
    lambda d: d.update(severity="EXTREME"),
], ids=["no-key", "bad-key", "sha-not-hex", "no-checks", "bad-time", "extra-field", "bad-severity"])
def test_malformed_incident_fails_closed(mutate):
    doc = json.loads((EXAMPLES / "incident.synthetic.json").read_text())
    mutate(doc)
    with pytest.raises(ValueError):
        require_valid(doc, "incident")


def test_bool_is_not_accepted_as_integer():
    assert validate(True, {"type": "integer"})


# ---------------------------------------------------------------- политика ---
def test_glob_does_not_strip_the_leading_dot():
    assert glob_match(".github/workflows/a.yml", ".github/**")
    assert not glob_match("github/workflows/a.yml", ".github/**")


def test_policy_itself_and_autonomy_code_are_trusted_computing_base():
    tcb = ["quality/autonomy/policy.json", "tools/autonomy/gatekeeper.py", "tools/verify_task.py",
           "tools/lib/bq_readonly.py"]
    assert tcb_paths(tcb) == sorted(tcb)
    assert forbidden_paths(tcb) == []          # TCB — решение человека, а не «секрет»


@pytest.mark.parametrize("line", [
    "-  -- @check V01_PARITY", "-ASSERT (SELECT 1) = 1 AS 'x';", "-def test_something():",
    "+@pytest.mark.skip", "+    \"known_failing\": true,", "+  \"gate\": false,", "+-- @expect empty_ok"])
def test_gate_weakening_mechanisms_are_detected(line):
    diff = f"--- a/f\n+++ b/f\n{line}\n"
    assert detect_gate_weakening(diff, ["f"])


def test_editing_a_file_that_defines_the_gates_is_weakening():
    assert detect_gate_weakening("", ["quality/suites.json"])[0]["kind"] == "protected_file"


def test_plan_ack_required_for_tier0_and_satisfied_only_by_exact_plan_hash():
    obj = {"owner_ack": None}
    need, _ = plan_requires_ack(["sql/current/ozon_mart/X.sql"], "TIER0", False, obj, "a" * 64)
    assert need
    obj = {"owner_ack": {"plan_sha256": "b" * 64}}
    assert plan_requires_ack(["sql/current/ozon_mart/X.sql"], "TIER0", False, obj, "a" * 64)[0]
    obj = {"owner_ack": {"plan_sha256": "a" * 64}}
    assert not plan_requires_ack(["sql/current/ozon_mart/X.sql"], "TIER0", False, obj, "a" * 64)[0]
    assert not plan_requires_ack(["tools/x.py"], "TIER3", False, {"owner_ack": None}, "a" * 64)[0]
    assert plan_requires_ack(["tools/x.py"], "TIER3", True, {"owner_ack": None}, "a" * 64)[0]


# -------------------------------------------------------------- гейткипер ---
GOOD = {"tests": [{"name": "t", "status": "PASS"}], "sql_validation": {"status": "PASS"},
        "runtime_access": {"status": "NOT_APPLICABLE"}, "parity": {"status": "NOT_APPLICABLE"},
        "data_suites": {"s": {"verdict": "PASS", "checks": {"C1": "PASS"}}}}
PASS_REVIEW = F.verdict("PASS")


def test_all_green_is_ready():
    assert G.evaluate(GOOD, GOOD, PASS_REVIEW, {})["verdict"] == "READY_FOR_PR"


def test_reviewer_pass_cannot_override_a_deterministic_prohibition():
    for ctx in ({"forbidden_paths": [".github/x"]}, {"production_mutations": 1},
                {"gate_weakening": [{"file": "f", "text": "+@pytest.mark.skip"}]}):
        assert G.evaluate(GOOD, GOOD, PASS_REVIEW, ctx)["verdict"] == "UNSAFE"


def test_reviewer_findings_demote_ready_even_with_pass_verdict():
    r = G.evaluate(GOOD, GOOD, F.verdict("PASS", [F.finding("BLOCKER")]), {})
    assert r["verdict"] != "READY_FOR_PR" and r["review_requests_changes"]


def test_missing_review_is_not_ready():
    assert G.evaluate(GOOD, GOOD, None, {})["verdict"] == "INCONCLUSIVE"


def test_unknown_data_evidence_is_never_converted_to_pass():
    ev = copy.deepcopy(GOOD); ev["data_suites"]["s"] = {"verdict": "BLOCKED", "checks": {"C1": "BLOCKED"}}
    assert G.evaluate(ev, GOOD, PASS_REVIEW, {})["verdict"] == "INCONCLUSIVE"


def test_new_data_failure_blocks_but_inherited_one_is_reported():
    ev = copy.deepcopy(GOOD); ev["data_suites"]["s"]["checks"]["C1"] = "FAIL"
    assert G.evaluate(ev, GOOD, PASS_REVIEW, {})["verdict"] == "BLOCKED_BY_DATA"
    base = copy.deepcopy(ev)
    r = G.evaluate(ev, base, PASS_REVIEW, {})
    assert r["verdict"] == "READY_FOR_PR" and r["informational"]


def test_red_repo_test_blocks_even_if_red_on_baseline():
    ev = copy.deepcopy(GOOD); ev["tests"][0]["status"] = "FAIL"
    assert G.evaluate(ev, ev, PASS_REVIEW, {})["verdict"] == "BLOCKED_BY_TEST"


def test_runtime_access_failure_has_its_own_verdict():
    ev = copy.deepcopy(GOOD); ev["runtime_access"] = {"status": "FAIL"}
    assert G.evaluate(ev, GOOD, PASS_REVIEW, {})["verdict"] == "BLOCKED_BY_RUNTIME_ACCESS"


def test_unproven_resolution_and_open_ubr_need_a_human():
    assert G.evaluate(GOOD, GOOD, PASS_REVIEW, {"objective_resolution": "NOT_DEMONSTRATED"})["verdict"] \
        == "HUMAN_DECISION_REQUIRED"
    assert G.evaluate(GOOD, GOOD, PASS_REVIEW, {"touches_open_ubr": ["UBR-011"]})["verdict"] \
        == "HUMAN_DECISION_REQUIRED"


def test_priority_is_total_and_worst_wins():
    ev = copy.deepcopy(GOOD); ev["tests"][0]["status"] = "FAIL"; ev["runtime_access"] = {"status": "FAIL"}
    assert G.evaluate(ev, GOOD, None, {"forbidden_paths": ["infra/x"]})["verdict"] == "UNSAFE"
    assert G.evaluate(ev, GOOD, None, {})["verdict"] == "BLOCKED_BY_TEST"


# ------------------------------------------------------------- состояния ---
def objective():
    return json.loads((EXAMPLES / "objective.synthetic.json").read_text())


def test_forbidden_transition_raises(tmp_path):
    store = StateStore(tmp_path)
    run, _ = store.open_or_get(objective(), "a" * 40, 24)
    with pytest.raises(TransitionError):
        store.transition(run, "READY_FOR_PR", "перепрыгнуть через тесты и ревью")


def test_every_transition_is_recorded_and_record_is_valid(tmp_path):
    store = StateStore(tmp_path)
    run, _ = store.open_or_get(objective(), "a" * 40, 24)
    run = store.transition(run, "DISCOVERING", "x")
    reloaded = store.load(run["run_id"])
    assert [t["to"] for t in reloaded["transitions"]] == ["RECEIVED", "DISCOVERING"]
    require_valid(reloaded, "run_state")


def test_dedup_slug_is_stable_valid_and_never_main():
    s1, s2 = dedup_slug("check:SKUV2__V9_PROVISIONAL_WINDOW_FINAL"), dedup_slug("check:SKUV2__V9_PROVISIONAL_WINDOW_FINAL")
    assert s1 == s2 and s1.startswith("ae/") and s1 != "main"
    assert dedup_slug("check:A") != dedup_slug("check:B")


# ------------------------------------------------------------- публикатор ---
def test_publisher_refuses_without_ready_gate_and_on_forbidden_content(tmp_path):
    art = tmp_path / "art"; art.mkdir()
    run = {"branch": "ae/x-12345678", "run_id": "r", "objective_id": "o", "repository_sha": "a" * 40,
           "production_mutations": 0, "created_at": "2026-09-27T12:00:00Z", "usage": [], "audit_status": "PASS",
           "audit_evidence": {"status": "PASS", "mutations": 0, "source": "jobs_list+audit_logs+iam_selftest/v3", "audited_at": "2026-09-27T12:30:00Z",
                              "since": "2026-09-27T12:00:00Z", "usage_count": 0}}
    pub = GitPublisher(tmp_path, dry_run=True)
    patch = "diff --git a/synthetic/calc.py b/synthetic/calc.py\n"
    (art / "gate.json").write_text(json.dumps({"verdict": "HUMAN_DECISION_REQUIRED"}))
    with pytest.raises(PublishRefused, match="READY_FOR_PR"):
        pub.preflight(run, patch, art)
    (art / "gate.json").write_text(json.dumps({"verdict": "READY_FOR_PR"}))
    with pytest.raises(PublishRefused, match="защищена|вне ae"):
        pub.preflight({**run, "branch": "main"}, patch, art)
    with pytest.raises(PublishRefused, match="запрещённые"):
        pub.preflight(run, "diff --git a/.github/workflows/x.yml b/.github/workflows/x.yml\n", art)
    with pytest.raises(PublishRefused, match="production"):
        pub.preflight({**run, "production_mutations": 1}, patch, art)
    with pytest.raises(PublishRefused, match="0 production-мутаций"):
        pub.preflight({k: v for k, v in run.items() if k != "audit_evidence"}, patch, art)
    assert pub.preflight(run, patch, art) == ["synthetic/calc.py"]


def test_deploy_tooling_is_forbidden_supply_chain_path():
    """Скрипт развёртывания запускает владелец со своими правами на DDL: правка его агентом —
    путь к production через цепочку поставки, хотя у самого агента прав нет."""
    deploy = sorted(p.relative_to(F.REPO).as_posix() for p in (F.REPO / "tools").glob("*deploy*.py"))
    assert deploy, "в tools/ ожидался хотя бы один инструмент развёртывания"
    assert tcb_paths(deploy) == deploy
    assert tcb_paths(["tools/impact_analysis_helper.py"]) == []
