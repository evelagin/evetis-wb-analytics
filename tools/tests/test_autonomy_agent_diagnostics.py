"""Диагностика агента при ЛЮБОМ исходе (remediation M6 Phase 1, ACK 2026-09-26).

Инцидент: Claude CLI отказал за ≤3 с, причина осталась в черновом состоянии раннера — артефакт
грузился только при непустых хешах. Здесь доказывается:
  * каждый исход вызова (успех, код ≠ 0, is_error, не-JSON, пустой вывод, таймаут, исключение,
    pre_invoke) даёт запись с классификацией, без учётных данных;
  * недоверенный job инженера/ревьюера ВСЕГДА отдаёт `<job>_diagnostics.json` в хешах;
  * доверенный ingest проверяет sha256, схему, роль, run_id, секреты — и только потом кладёт копию в
    состояние и переводит прогон в BLOCKED/FAILED; подмена — исключение, мусор — отказ без записи;
  * отказ редакции — REDACTION_BLOCKED без свободного текста;
  * воркфлоу: Node 22, выгрузка при любом исходе, модель и бюджеты не изменены;
  * в тестах нет пути к Anthropic.
Все «CLI» здесь — локальные скрипты; настоящий claude не вызывается.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from pathlib import Path

import pytest

import ae_fixtures as F  # noqa: E402
from tools.autonomy import diagnostics as D
from tools.autonomy.agents import ClaudeCliAdapter, IntegrityError, ReplayAdapter, ScriptedAdapter, sha256_file
from tools.autonomy.cli import _handoff_code, _scope_guard
from tools.autonomy.orchestrator import agent_run, review_only
from tools.autonomy.policy import load_policy
from tools.autonomy.redact import RedactionError, find_secrets
from tools.autonomy.report import render_report
from tools.autonomy.schema import load_schema, validate
from tools.tests.test_autonomy_ci_trust import Pipeline

REPO = Path(__file__).resolve().parents[2]
WF = REPO / ".github" / "workflows"
PLAN_SCHEMA = load_schema("engineer_report")


def fake_tok(kind: str) -> str:
    """Фиктивные секреты собираются во время выполнения: в исходнике их нет."""
    return {"ya29": "ya" + "29." + "Q" * 40, "sk_ant": "-".join(("sk", "ant", "oat01", "Z" * 40)),
            "ghp": "gh" + "p_" + "A" * 36}[kind]


def fake_cli(tmp: Path, stdout: str = "", stderr: str = "", rc: int = 0, sleep: int = 0,
             version: str = "2.1.251 (Claude Code)", shebang: str = "/bin/sh") -> str:
    d = tmp / f"cli-{uuid.uuid4().hex[:8]}"
    d.mkdir(parents=True)
    (d / "out").write_text(stdout)
    (d / "err").write_text(stderr)
    body = (f"#!{shebang}\nif [ \"$1\" = \"--version\" ]; then echo '{version}'; exit 0; fi\n"
            f"cat > /dev/null\n" + (f"exec sleep {sleep}\n" if sleep else "")
            + f"cat '{d / 'out'}'\ncat '{d / 'err'}' >&2\nexit {rc}\n")
    script = d / "claude"
    script.write_text(body)
    script.chmod(0o755)
    return str(script)


def cli_json(**kw) -> str:
    return json.dumps({"type": "result", "subtype": "success", "is_error": False, "duration_ms": 40,
                       "duration_api_ms": 0, "num_turns": 1, "total_cost_usd": 0, **kw})


API_400 = cli_json(is_error=True, api_error_status=400,
                   result='API Error: 400 {"type":"error","error":{"type":"invalid_request_error","message":"bad"}}')
API_529 = cli_json(is_error=True, api_error_status=529, result="API Error: 529 overloaded_error")


def run_adapter(tmp, role="engineer_plan", **kw) -> tuple:
    adapter = ClaudeCliAdapter(binary=fake_cli(tmp, **{k: v for k, v in kw.items() if k != "timeout_s"}),
                               timeout_s=kw.get("timeout_s", 60))
    res = adapter.run(role, "prompt", tmp, PLAN_SCHEMA)
    return res, res.diagnostics, adapter


def assert_valid_invocation(inv: dict) -> None:
    schema = load_schema("agent_diagnostics")["properties"]["invocations"]["items"]
    assert validate(inv, schema) == [], validate(inv, schema)
    assert find_secrets(json.dumps(inv, ensure_ascii=False)) == []


# ------------------------------------------------------------- исходы вызова CLI ---
def test_successful_agent(tmp_path):
    res, inv, adapter = run_adapter(tmp_path, stdout=cli_json(structured_output=F.plan(), duration_api_ms=900,
                                                               usage={"input_tokens": 10, "output_tokens": 5}))
    assert res.structured is not None and inv["failure_stage"] == "NONE" and inv["failure_class"] == "NONE"
    assert inv["process_started"] and inv["exit_code"] == 0 and inv["output_format"] == "json"
    assert inv["messages_api_reached"] is True and inv["model_response_began"] is True
    assert inv["usage"] == {"input_tokens": 10, "output_tokens": 5} and inv["elapsed_ms"] >= 0
    assert adapter.environment()["claude_code_version"] == "2.1.251"
    assert_valid_invocation(inv)


def test_cli_nonzero_non_json(tmp_path):
    res, inv, _ = run_adapter(tmp_path, stdout="boom", stderr="fatal: native crash", rc=2)
    assert res.structured is None and inv["failure_stage"] == "OUTPUT_NOT_JSON" and inv["failure_class"] == "CLI"
    assert inv["exit_code"] == 2 and "native crash" in inv["stderr_tail"] and inv["messages_api_reached"] is None
    assert_valid_invocation(inv)


def test_cli_nonzero_empty_output(tmp_path):
    res, inv, _ = run_adapter(tmp_path, rc=1)
    assert inv["failure_stage"] == "PROCESS_EXIT_NONZERO" and inv["output_format"] == "empty"
    assert_valid_invocation(inv)


def test_is_error_true_is_api_class_and_never_retried(tmp_path):
    res, inv, _ = run_adapter(tmp_path, stdout=API_400, rc=1)
    assert res.structured is None and res.transient is False            # повторов больше не стало
    assert inv["failure_stage"] == "CLI_REPORTED_ERROR" and inv["failure_class"] == "API"
    assert inv["api_error_status"] == 400 and inv["api_error_type"] == "invalid_request_error"
    assert inv["messages_api_reached"] is True and inv["model_response_began"] is False
    assert "invalid_request_error" in inv["summary"]
    assert_valid_invocation(inv)


def test_is_error_overloaded_is_infra_class_without_extra_retries(tmp_path):
    res, inv, _ = run_adapter(tmp_path, stdout=API_529, rc=1)
    assert inv["failure_class"] == "INFRA" and res.transient is False


def test_not_logged_in_shape_means_no_messages_request(tmp_path):
    res, inv, _ = run_adapter(tmp_path, stdout=cli_json(is_error=True, result="Not logged in · Please run /login"), rc=1)
    assert inv["failure_class"] == "CLI" and inv["messages_api_reached"] is False


def test_timeout(tmp_path):
    res, inv, _ = run_adapter(tmp_path, sleep=5, timeout_s=1)
    assert res.exit_code == 124 and res.transient is True
    assert inv["failure_stage"] == "TIMEOUT" and inv["failure_class"] == "INFRA" and inv["process_started"]
    assert_valid_invocation(inv)


def test_malformed_json(tmp_path):
    res, inv, _ = run_adapter(tmp_path, stdout='{"is_error": tru')
    assert inv["failure_stage"] == "OUTPUT_NOT_JSON" and inv["output_format"] == "non_json"


def test_json_without_structured_output_and_schema_violation(tmp_path):
    _, inv, _ = run_adapter(tmp_path, stdout=cli_json(result="готово"))
    assert inv["failure_stage"] == "NO_STRUCTURED_OUTPUT" and inv["failure_class"] == "AGENT_OUTPUT"
    _, inv, _ = run_adapter(tmp_path, stdout=cli_json(structured_output={"phase": "PLAN"}))
    assert inv["failure_stage"] == "SCHEMA_INVALID"


def test_subprocess_spawn_exception_is_recorded_not_raised(tmp_path):
    adapter = ClaudeCliAdapter(binary=fake_cli(tmp_path, shebang="/nonexistent/interpreter"))
    res = adapter.run("engineer_plan", "p", tmp_path, PLAN_SCHEMA)
    inv = res.diagnostics
    assert res.structured is None and inv["failure_stage"] in ("PROCESS_SPAWN", "WRAPPER_EXCEPTION")
    assert inv["exception_type"] and inv["failure_class"] == "WRAPPER"
    assert_valid_invocation(inv)


def test_wrapper_exception_is_recorded_not_raised(tmp_path, monkeypatch):
    adapter = ClaudeCliAdapter(binary=fake_cli(tmp_path, stdout=API_400))
    monkeypatch.setattr(adapter, "command", lambda role, schema: (_ for _ in ()).throw(RuntimeError("x")))
    res = adapter.run("engineer_plan", "p", tmp_path, PLAN_SCHEMA)
    assert res.diagnostics["failure_stage"] == "WRAPPER_EXCEPTION" and res.diagnostics["exception_type"] == "RuntimeError"


def test_missing_binary(tmp_path):
    res = ClaudeCliAdapter(binary=str(tmp_path / "no-such-claude")).run("reviewer", "p", tmp_path, PLAN_SCHEMA)
    assert res.diagnostics["failure_stage"] == "BINARY_MISSING" and res.diagnostics["process_started"] is False


def test_pre_invoke_failure(tmp_path):
    adapter = ClaudeCliAdapter(binary=fake_cli(tmp_path, stdout=API_400), pre_invoke=["sh", "-c", "echo nope >&2; exit 7"])
    res = adapter.run("engineer_plan", "p", tmp_path, PLAN_SCHEMA)
    inv = res.diagnostics
    assert inv["failure_stage"] == "PRE_INVOKE" and inv["pre_invoke_succeeded"] is False
    assert inv["failure_class"] == "INFRA" and inv["process_started"] is False and res.transient is True


def test_secret_like_stdout_stderr_are_redacted(tmp_path):
    leak = f"{fake_tok('ya29')} {fake_tok('sk_ant')}"
    res, inv, _ = run_adapter(tmp_path, stdout="token " + leak, stderr="auth " + fake_tok("ghp"), rc=1)
    blob = json.dumps(inv, ensure_ascii=False)
    for t in ("ya29", "sk_ant", "ghp"):
        assert fake_tok(t) not in blob
    assert "[REDACTED:" in inv["stdout_tail"] and "[REDACTED:" in inv["stderr_tail"]
    assert_valid_invocation(inv)


def test_oversized_output_is_bounded(tmp_path):
    _, inv, _ = run_adapter(tmp_path, stdout="x" * 500_000, stderr="y" * 500_000, rc=1)
    assert len(inv["stdout_tail"]) <= D.TAIL_OUT and len(inv["stderr_tail"]) <= D.TAIL_ERR


# ------------------------------------------------ редакция: отказ — REDACTION_BLOCKED ---
def _bundle_with(inv) -> dict:
    return D.bundle("engineer", "run-20260926T000000Z-0badc0de", [inv], expected=["engineer_plan.json"])


def test_redaction_failure_fails_closed(monkeypatch, tmp_path):
    _, inv, _ = run_adapter(tmp_path, stdout=API_400, rc=1)
    monkeypatch.setattr(D, "safe_dumps", lambda *a, **k: (_ for _ in ()).throw(RedactionError("остаток")))
    text, doc = D.finalize(_bundle_with(inv))
    assert doc["redaction"] == "REDACTION_BLOCKED" and doc["failure_stage"] == "REDACTION_BLOCKED"
    assert validate(doc, load_schema("agent_diagnostics")) == []
    assert all(i[k] == "" for i in doc["invocations"] for k in D.FREE_TEXT) and doc["scratch_reason"] == ""
    assert doc["invocations"][0]["failure_stage"] == "CLI_REPORTED_ERROR"   # структура сохранена


def test_unredactable_text_never_reaches_the_file(monkeypatch, tmp_path):
    _, inv, _ = run_adapter(tmp_path, stdout=API_400, rc=1)
    inv = {**inv, "summary": fake_tok("sk_ant")}
    # Первый слой (redact_obj диагностики) сломан — второй (safe_dumps) всё равно вычищает:
    monkeypatch.setattr(D, "redact_obj", lambda d: d)
    text, doc = D.finalize(_bundle_with(inv))
    assert fake_tok("sk_ant") not in text and "[REDACTED:anthropic_key]" in text
    # Сломаны оба слоя редакции — повторный поиск (ensure_clean) блокирует запись текста целиком:
    from tools.autonomy import redact as R
    monkeypatch.setattr(R, "redact_obj", lambda d: d)
    text, doc = D.finalize(_bundle_with(inv))
    assert fake_tok("sk_ant") not in text and doc["redaction"] == "REDACTION_BLOCKED"


# ------------------------------------------- job инженера: отказ до плана, ingest ---
def prepared(tmp_path) -> tuple[Pipeline, str]:
    p = Pipeline(tmp_path)
    orch = p.orch(p.branch)
    run_id = orch.submit(p.objective())[0]["run_id"]
    assert orch.advance(run_id, stop_before={"PLANNING"})["state"] == "PLANNING"
    return p, run_id


def engineer_job(p, run_id, cli_stdout, rc=1, scope_guard=None):
    out = p.tmp / f"pending-{uuid.uuid4().hex[:6]}"
    cli = fake_cli(p.tmp, stdout=cli_stdout, rc=rc)
    hashes = agent_run(lambda s: p.orch(s, engineer=ClaudeCliAdapter(binary=cli)), p.machine("engineer"),
                       run_id, out, scope_guard=scope_guard)
    return out, hashes


GUARD = {"role": "engineer", "scope": "workspace:developer", "expires_in": 598, "decision": "PASS_WITH_EXCEPTION",
         "request_id": "req_011CfQb7o1niS9GodpL54uqu"}


def test_failure_before_plan_still_emits_diagnostics_in_hashes(tmp_path):
    p, run_id = prepared(tmp_path)
    out, hashes = engineer_job(p, run_id, API_400, scope_guard=GUARD)
    assert set(hashes) == {"engineer_diagnostics.json"}                  # плана нет, диагностика есть
    doc = json.loads((out / "engineer_diagnostics.json").read_text())
    assert validate(doc, load_schema("agent_diagnostics")) == []
    assert doc["outcome"] == "FAILURE" and doc["failure_stage"] == "CLI_REPORTED_ERROR"
    assert doc["expected_artifacts"] == ["engineer_plan.json"] and doc["actually_created_artifacts"] == []
    assert doc["scope_guard"]["decision"] == "PASS_WITH_EXCEPTION" and doc["scope_guard"]["expires_in"] == 598
    assert doc["claude_code_version"] == "2.1.251" and doc["scratch_state"] == "BLOCKED"
    assert _handoff_code(out / "engineer_diagnostics.json") == 3
    assert p.branch.load(run_id)["state"] == "PLANNING"                 # авторитетное состояние не тронуто


def test_wrapper_exception_in_agent_run_still_emits_diagnostics(tmp_path):
    p, run_id = prepared(tmp_path)
    out = tmp_path / "pending-x"

    def broken(_scratch):
        raise RuntimeError("orchestrator factory failed")
    hashes = agent_run(broken, p.machine("engineer"), run_id, out)
    doc = json.loads((out / "engineer_diagnostics.json").read_text())
    assert set(hashes) == {"engineer_diagnostics.json"}
    assert doc["failure_stage"] == "WRAPPER_EXCEPTION" and "RuntimeError" in doc["scratch_reason"]
    assert doc["expected_artifacts"] == ["engineer_plan.json"]


@pytest.mark.parametrize("stdout,state,cls", [(API_400, "BLOCKED", "API"), (API_529, "FAILED", "INFRA")])
def test_trusted_ingest_classifies_and_keeps_a_trusted_copy(tmp_path, stdout, state, cls):
    p, run_id = prepared(tmp_path)
    out, hashes = engineer_job(p, run_id, stdout)
    run = p.orch(p.branch, engineer=ReplayAdapter(out, hashes)).advance(run_id, stop_before={"TESTING"})
    assert run["state"] == state and f"/{cls}]" in run["transitions"][-1]["reason"]
    entry = run["usage"][-1]["diagnostics"]
    assert entry["artifact_sha256"] == hashes["engineer_diagnostics.json"] and entry["failure_class"] == cls
    copy = p.branch.root / "artifacts" / run_id / entry["file"]
    doc = json.loads(copy.read_text())
    assert validate(doc, load_schema("agent_diagnostics")) == [] and doc["failure_class"] == cls
    report = render_report(run, p.branch.root / "artifacts" / run_id)
    assert "Диагностика агента" in report and entry["artifact_sha256"][:16] in report and cls in report


def test_tampered_diagnostics_is_rejected_and_state_unchanged(tmp_path):
    p, run_id = prepared(tmp_path)
    out, hashes = engineer_job(p, run_id, API_400)
    f = out / "engineer_diagnostics.json"
    f.write_text(f.read_text().replace("CLI_REPORTED_ERROR", "NONE", 1))
    with pytest.raises(IntegrityError):
        p.orch(p.branch, engineer=ReplayAdapter(out, hashes)).advance(run_id, stop_before={"TESTING"})
    assert p.branch.load(run_id)["state"] == "PLANNING"


def test_declared_but_missing_diagnostics_is_integrity_error(tmp_path):
    p, run_id = prepared(tmp_path)
    out, hashes = engineer_job(p, run_id, API_400)
    (out / "engineer_diagnostics.json").unlink()
    with pytest.raises(IntegrityError):
        p.orch(p.branch, engineer=ReplayAdapter(out, hashes)).advance(run_id, stop_before={"TESTING"})


def _resign(out: Path, name: str, mutate) -> dict:
    f = out / name
    doc = json.loads(f.read_text())
    mutate(doc)
    f.write_text(json.dumps(doc, ensure_ascii=False))
    return {name: sha256_file(f)}


@pytest.mark.parametrize("mutate,needle", [
    (lambda d: d.__setitem__("unexpected", 1), "схему"),
    (lambda d: d.__setitem__("schema_version", 2), "схему"),
    (lambda d: d["invocations"][0].__setitem__("summary", "leak " + fake_tok("ghp")), "секрет"),
    (lambda d: d.__setitem__("limitations", ["выдуманный текст"]), "limitations"),
    (lambda d: d.__setitem__("job_role", "reviewer"), "подана как"),
    (lambda d: d.__setitem__("scratch_reason", "x" * 100_000), "схему"),
])
def test_invalid_diagnostics_are_rejected_without_persisting(tmp_path, mutate, needle):
    """Хеш верный (производитель подписал), но содержимое плохое: отказ, в состояние не попадает."""
    p, run_id = prepared(tmp_path)
    out, _ = engineer_job(p, run_id, API_400)
    hashes = _resign(out, "engineer_diagnostics.json", mutate)
    run = p.orch(p.branch, engineer=ReplayAdapter(out, hashes)).advance(run_id, stop_before={"TESTING"})
    assert run["state"] == "BLOCKED" and "диагностика отвергнута" in run["transitions"][-1]["reason"]
    assert needle in run["transitions"][-1]["reason"] or needle in json.dumps(run["usage"][-1], ensure_ascii=False)
    assert not (p.branch.root / "artifacts" / run_id / "diagnostics").exists()
    assert fake_tok("ghp") not in json.dumps(run, ensure_ascii=False)


def test_oversized_diagnostics_rejected_before_parsing():
    with pytest.raises(D.DiagnosticsRejected):
        D.verify_untrusted(" " * (D.MAX_BYTES + 1), "engineer")


def test_foreign_run_id_diagnostics_are_not_persisted(tmp_path):
    p, run_id = prepared(tmp_path)
    out, _ = engineer_job(p, run_id, API_400)
    hashes = _resign(out, "engineer_diagnostics.json", lambda d: d.__setitem__("run_id", "run-20260101T000000Z-deadbeef"))
    run = p.orch(p.branch, engineer=ReplayAdapter(out, hashes)).advance(run_id, stop_before={"TESTING"})
    assert run["state"] == "BLOCKED" and "run_id чужого прогона" in run["transitions"][-1]["reason"]
    assert not (p.branch.root / "artifacts" / run_id / "diagnostics").exists()


# ------------------------------------------------------ изоляция инженер / ревьюер ---
def test_engineer_diagnostics_cannot_be_submitted_as_reviewer(tmp_path):
    p, run_id = prepared(tmp_path)
    out, hashes = engineer_job(p, run_id, API_400)
    shutil.copy(out / "engineer_diagnostics.json", out / "reviewer_diagnostics.json")
    rep = ReplayAdapter(out, {"reviewer_diagnostics.json": sha256_file(out / "reviewer_diagnostics.json")})
    diag, rejected = rep.diagnostics("reviewer")
    assert diag is None and "подана как reviewer" in rejected
    # и наоборот: ревьюерская роль вызова в документе инженера
    doc = json.loads((out / "engineer_diagnostics.json").read_text())
    doc["invocations"][0]["role"] = "reviewer"
    with pytest.raises(D.DiagnosticsRejected):
        D.verify_untrusted(json.dumps(doc), "engineer")


def test_reviewer_failure_emits_reviewer_diagnostics_and_gate_classifies(tmp_path):
    from tools.tests.test_autonomy_ci_trust import run_until_testing
    from tools.autonomy.evidence import ReplayEvidenceRunner
    from tools.autonomy.orchestrator import collect_candidate_evidence
    p = Pipeline(tmp_path)
    run_id, eng_out, eng_hashes = run_until_testing(p)
    p.orch(p.branch, engineer=ReplayAdapter(eng_out, eng_hashes)).advance(run_id, stop_before={"TESTING"})
    ev_out = tmp_path / "pending-test" / "evidence.json"
    ev_sha = collect_candidate_evidence(p.orch(p.machine("test")), run_id, ev_out)
    p.orch(p.branch, evidence=ReplayEvidenceRunner(ev_out, ev_sha, F.SyntheticEvidenceRunner(), p.repo)).advance(
        run_id, stop_before={"REVIEWING"})
    rv_dir = tmp_path / "pending-review"
    cli = fake_cli(tmp_path, stdout=API_400, rc=1)
    hashes = review_only(p.orch(p.machine("review"), reviewer=ClaudeCliAdapter(binary=cli)), run_id, rv_dir)
    assert set(hashes) == {"reviewer_diagnostics.json"}
    doc = json.loads((rv_dir / "reviewer_diagnostics.json").read_text())
    assert doc["job_role"] == "reviewer" and doc["outcome"] == "FAILURE" and doc["failure_class"] == "API"
    assert all(i["role"] == "reviewer" for i in doc["invocations"])
    assert _handoff_code(rv_dir / "reviewer_diagnostics.json") == 3
    run = p.orch(p.branch, reviewer=ReplayAdapter(rv_dir, hashes)).advance(run_id)
    assert run["state"] == "BLOCKED" and run["transitions"][-1]["reason"].startswith("ревью не получено")
    assert run["usage"][-1]["diagnostics"]["artifact_sha256"] == hashes["reviewer_diagnostics.json"]


def test_success_path_still_carries_diagnostics(tmp_path):
    from tools.tests.test_autonomy_ci_trust import run_until_testing
    p = Pipeline(tmp_path)
    run_id, out, hashes = run_until_testing(p)
    doc = json.loads((out / "engineer_diagnostics.json").read_text())
    assert doc["outcome"] == "SUCCESS" and doc["failure_stage"] == "NONE"
    assert set(doc["actually_created_artifacts"]) == set(doc["expected_artifacts"])
    assert _handoff_code(out / "engineer_diagnostics.json") == 0


def test_handoff_code_fails_closed_when_diagnostics_missing(tmp_path):
    assert _handoff_code(tmp_path / "nope.json") == 3


def test_scope_guard_parser_keeps_only_safe_fields(tmp_path):
    f = tmp_path / "g.json"
    f.write_text("noise\n" + json.dumps({**GUARD, "request_id": "req_" + fake_tok("ghp"), "extra": "x"}) + "\n")

    class A:
        scope_guard_file = str(f)
    doc = D.bundle("engineer", "run-20260926T000000Z-0badc0de", [], scope_guard=_scope_guard(A()))
    assert doc["scope_guard"]["request_id"] is None and "extra" not in doc["scope_guard"]
    assert doc["scope_guard"]["scope"] == "workspace:developer"


# ------------------------------------------------------------ воркфлоу и рантайм ---
@pytest.mark.parametrize("name", ["autonomy-engineer.yml", "autonomy-review.yml"])
def test_node_22_and_claude_code_pin(name):
    src = (WF / name).read_text()
    assert re.search(r'node-version: "22"', src) and 'node-version: "20"' not in src
    assert "@anthropic-ai/claude-code@2.1.251" in src


@pytest.mark.parametrize("name", ["autonomy-engineer.yml", "autonomy-review.yml"])
def test_diagnostics_upload_survives_failure(name):
    src = (WF / name).read_text()
    upload = src[src.index("actions/upload-artifact"):]
    assert upload.split("\n")[1].strip() == "if: always()" and "if-no-files-found: ignore" in upload
    assert "hashes != ''" not in src and "reviewer_sha" not in src
    assert "--scope-guard-file" in src and 'case "$rc" in 0|3) ;;' in src and "::error::" in src
    assert "diag-summary" in src


def test_gate_receives_reviewer_hashes_including_diagnostics():
    run = (WF / "autonomy-run.yml").read_text()
    assert "expect: ${{ needs.review.outputs.hashes }}" in run and "reviewer_sha" not in run
    gate = (WF / "autonomy-gate.yml").read_text()
    rx = re.search(r'"\$EXPECT" =~ (.+) \]\]', gate).group(1)
    for name in ("engineer_diagnostics.json", "reviewer_diagnostics.json"):
        assert re.match(rx, f"{name}={'a' * 64}"), name
    assert re.match(rx, f"reviewer.json={'a' * 64} reviewer_diagnostics.json={'b' * 64}")


def test_model_and_budgets_unchanged():
    p = load_policy()
    assert p["model"]["engineer"] == p["model"]["reviewer"] == "claude-opus-5-5"
    b = p["budgets"]
    assert (b["engineer_max_turns"], b["reviewer_max_turns"], b["engineer_max_budget_usd"],
            b["reviewer_max_budget_usd"], b["max_infra_retries"]) == (60, 25, 15, 5, 2)


# ---------------------------------------------------------------- без inference ---
def test_session_blocks_anthropic_network():
    assert os.environ["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:9"
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_IDENTITY_TOKEN_FILE"):
        assert k not in os.environ


def test_no_test_runs_the_real_claude_binary():
    """Каждый ClaudeCliAdapter в тестах, у которого вызывается run, создан с поддельным binary=."""
    for f in sorted((REPO / "tools" / "tests").glob("test_*.py")):
        src = f.read_text()
        for m in re.finditer(r"ClaudeCliAdapter\(([^()]*(?:\([^()]*\)[^()]*)*)\)(\.\w+)?", src):
            args, method = m.group(1), (m.group(2) or "")
            if "binary=" in args:
                continue
            assert method in (".command", ".cli_schema"), f"{f.name}: адаптер без binary= ({args}){method}"
