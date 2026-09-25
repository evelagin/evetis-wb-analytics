"""Инцидент M6 (2026-09-25): редакция до записи, окружение агента без учётных данных, диагностика.

Упавший тест вывел repr stdout `gcloud auth print-access-token` (обрезанный токен sa-ae-reader), хвост
ушёл в baseline.json и в ветку autonomy-state. Здесь закреплено: (1) секретоподобное вычищается ДО
любой записи и отказ при остатке; (2) дифф кандидата с секретом — UNSAFE и не сохраняется;
(3) переменные учётных данных CI не попадают в окружение агента; (4) диагностика отказа федерации
не печатает JWT. Поддельные токены собираются во время выполнения — в исходнике их нет."""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest

from tools.autonomy import anthropic_scope as S
from tools.autonomy import redact as R
from tools.autonomy.agents import ClaudeCliAdapter, ScriptedAdapter, scrubbed_env
from tools.autonomy.cli import CI_KEEP_ENV
from tools.autonomy.evidence import _run
from tools.autonomy.orchestrator import Orchestrator, collect_candidate_evidence
from tools.autonomy.publisher import GitPublisher, PublishRefused
from tools.autonomy.state import StateStore
from tools.tests import ae_fixtures as F
from tools.tests.test_autonomy_acceptance import dispatch_synthetic, env, orchestrator  # noqa: F401

REPO = Path(__file__).resolve().parents[2]


def fake(kind: str) -> str:
    """Секретоподобные значения, собранные на лету (исходник не содержит их целиком)."""
    body = "Z9" * 20
    return {
        "ya29": "ya" + "29." + "c.c0AZ4bNp" + body,
        "ya29_truncated": "ya" + "29." + "c.c0AZ4bNpbpIcEhs...1Upr22M0skVV8t7iZyJvplws",   # форма из инцидента
        "sk_ant": "sk" + "-ant-" + "oat01-" + body,
        "ghp": "gh" + "p_" + body,
        "gho": "gh" + "o_" + body,
        "gh_pat": "github" + "_pat_" + body,
        "jwt": "ey" + "J" + "hbGciOiJSUzI1NiJ9" + "." + "ey" + "JzdWIiOiJ4In0" + "." + "c2lnbmF0dXJl",
        "pem": "-----BEGIN " + "PRIVATE KEY-----\nMIIEv" + body + "\n-----END " + "PRIVATE KEY-----",
        "pem_truncated": "-----BEGIN RSA " + "PRIVATE KEY-----\nMIIEv" + body,
        "pk_field": '"private' + '_key": "abc' + body + '"',
        "bearer": "Authorization: " + "Bearer " + body,
        "aws": "AK" + "IA" + "ABCDEFGHIJKLMNOP",
        "gapi": "AI" + "za" + "Sy" + body,
    }[kind]


KINDS = ["ya29", "ya29_truncated", "sk_ant", "ghp", "gho", "gh_pat", "jwt", "pem", "pem_truncated", "pk_field",
         "bearer", "aws", "gapi"]


# ------------------------------------------------------------------------- редакция ---
@pytest.mark.parametrize("kind", KINDS)
def test_every_kind_is_redacted(kind):
    raw = f"stdout='{fake(kind)}\\n' tail"
    out = R.redact_text(raw)
    assert R.find_secrets(out) == [] and "[REDACTED:" in out
    assert fake(kind) not in out


def test_incident_shape_is_redacted_even_when_pytest_truncated_it():
    tail = ("FAILED test_gcloud - AssertionError\n + where 0 = CompletedProcess(args=['gcloud', 'auth', "
            f"'print-access-token'], returncode=0, stdout='{fake('ya29_truncated')}\\n', stderr='').returncode")
    out = R.redact_text(tail)
    assert "ya" + "29." not in out and "1Upr22M0" not in out
    assert "FAILED test_gcloud" in out                     # диагностическая часть сохраняется


def test_redaction_is_idempotent_and_leaves_normal_text():
    normal = ("sa-ae-reader@project-fa311fc0-4d87-4781-986.iam.gserviceaccount.com fdrl_017uB6D6 "
              "a20503a29f25dadf138bfb5aad7418e774c84d08 1202 passed")
    assert R.redact_text(normal) == normal
    once = R.redact_text(fake("jwt"))
    assert R.redact_text(once) == once


def test_ensure_clean_fails_closed_on_residue():
    with pytest.raises(R.RedactionError):
        R.ensure_clean("x " + fake("sk_ant"))


def test_safe_dumps_redacts_nested_documents():
    doc = {"tests": [{"tail": fake("ya29")}], "k": {"e": fake("ghp")}, "list": [fake("jwt")]}
    text = R.safe_dumps(doc)
    assert R.find_secrets(text) == [] and text.count("[REDACTED:") == 3


# --------------------------------------------------------------- редакция до записи ---
def test_state_store_persists_only_redacted_text(tmp_path):
    store = StateStore(tmp_path)
    obj = json.loads((REPO / "quality/autonomy/examples/objective.synthetic.json").read_text())
    run, _ = store.open_or_get(obj, "a" * 40, 24)
    store.save({**run, "notes": [f"ошибка: {fake('ya29_truncated')}"]})
    text = (tmp_path / "runs" / f"{run['run_id']}.json").read_text()
    assert "ya" + "29." not in text and "[REDACTED:google_access_token]" in text


def test_orchestrator_artifacts_are_redacted_before_write(env):
    orch, _ = orchestrator(env, ScriptedAdapter({}), ScriptedAdapter({}))
    run = orch.submit(dispatch_synthetic(env))[0]
    orch._put(run, "evidence.json", {"tests": [{"tail": fake("sk_ant")}]})
    orch._put(run, "report.md", "tail " + fake("ghp"))
    art = env["store"].root / "artifacts" / run["run_id"]
    for name in ("evidence.json", "report.md"):
        assert R.find_secrets((art / name).read_text()) == []
    with pytest.raises(R.RedactionError):
        orch._put(run, "candidate.patch", "+x = '" + fake("sk_ant") + "'\n")
    assert not (art / "candidate.patch").exists()


def test_evidence_tail_is_redacted_at_source(tmp_path):
    r = _run([sys.executable, "-c", f"print({fake('ya29')!r}); print({fake('pem')!r})"], tmp_path)
    assert R.find_secrets(r["tail"]) == [] and "[REDACTED:" in r["tail"]


def test_agent_output_and_error_are_redacted(tmp_path):
    script = tmp_path / "fake-claude"
    script.write_text(f"#!/bin/sh\necho '{fake('sk_ant')}'\necho '{fake('ghp')}' >&2\nexit 1\n")
    script.chmod(0o755)
    res = ClaudeCliAdapter(binary=str(script)).run("reviewer", "p", tmp_path, {"type": "object"})
    assert R.find_secrets(res.raw_tail) == [] and R.find_secrets(res.error or "") == []


def test_untrusted_evidence_file_is_written_redacted_and_hashed_after_redaction(env, tmp_path):
    from tools.autonomy.agents import sha256_file

    class LeakyRunner(F.SyntheticEvidenceRunner):
        def collect(self, workdir, changed_files, objective):
            ev = super().collect(workdir, changed_files, objective)
            ev["tests"][0]["tail"] = "repr " + fake("ya29_truncated")
            return ev

    objective = dispatch_synthetic(env)
    eng = ScriptedAdapter({"engineer_plan": [{"respond": F.plan()}],
                           "engineer_implement": [{"edit": F.edit_fix, "respond": F.implemented()}]})
    orch = Orchestrator(env["store"], env["repo"], eng, ScriptedAdapter({}), LeakyRunner(), env["sandboxes"],
                        trusted_base_ref="main")
    run_id = orch.submit(objective)[0]["run_id"]
    assert orch.advance(run_id, stop_before={"TESTING"})["state"] == "TESTING"
    out = tmp_path / "evidence.json"
    sha = collect_candidate_evidence(orch, run_id, out)
    assert R.find_secrets(out.read_text()) == [] and sha == sha256_file(out)


def test_candidate_diff_with_secret_is_unsafe_and_not_persisted(env):
    def leak(ws):
        F.edit_fix(ws)
        (ws / "synthetic" / "conf.py").write_text("TOKEN = '" + fake("ghp") + "'\n")

    objective = dispatch_synthetic(env)
    eng = ScriptedAdapter({"engineer_plan": [{"respond": F.plan()}],
                           "engineer_implement": [{"edit": leak, "respond": F.implemented()}]})
    orch, pub = orchestrator(env, eng, ScriptedAdapter({}))
    run = orch.advance(orch.submit(objective)[0]["run_id"])
    assert run["state"] == "BLOCKED" and run["last_gate"]["verdict"] == "UNSAFE"
    assert not (env["store"].root / "artifacts" / run["run_id"] / "candidate.patch").exists()
    assert pub.log == []


def test_publisher_refuses_diff_with_secret(tmp_path):
    art = tmp_path / "art"; art.mkdir()
    (art / "gate.json").write_text(json.dumps({"verdict": "READY_FOR_PR"}))
    run = {"branch": "ae/x-12345678", "run_id": "r", "objective_id": "o", "repository_sha": "a" * 40,
           "production_mutations": 0}
    patch = "diff --git a/synthetic/c.py b/synthetic/c.py\n+T = '" + fake("sk_ant") + "'\n"
    with pytest.raises(PublishRefused, match="секретоподоб"):
        GitPublisher(tmp_path, dry_run=True).preflight(run, patch, art)


# ------------------------------------------------------------ окружение агента ---
CI_ENV = {
    "PATH": "/usr/bin", "HOME": "/home/runner",
    "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE": "/home/runner/work/x/gha-creds-1.json",
    "GOOGLE_GHA_CREDS_PATH": "/home/runner/work/x/gha-creds-1.json",
    "GOOGLE_APPLICATION_CREDENTIALS": "/home/runner/work/x/gha-creds-1.json",
    "CLOUDSDK_AUTH_ACCESS_TOKEN_FILE": "/tmp/t", "CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT": "sa@x",
    "GH_TOKEN": "x", "GITHUB_TOKEN": "x", "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "x",
    "ACTIONS_ID_TOKEN_REQUEST_URL": "https://x", "ACTIONS_RUNTIME_TOKEN": "x",
    "ANTHROPIC_API_KEY": "x", "ANTHROPIC_AUTH_TOKEN": "x", "ANTHROPIC_IDENTITY_TOKEN": "x",
    "CLAUDE_CODE_OAUTH_TOKEN": "x", "GOOGLE_OAUTH_ACCESS_TOKEN": "x",
}
CREDENTIAL_VARS = set(CI_ENV) - {"PATH", "HOME"}


def test_agent_env_drops_every_ci_credential_variable():
    env = scrubbed_env(CI_ENV)
    assert not CREDENTIAL_VARS & set(env), sorted(CREDENTIAL_VARS & set(env))
    assert env["PATH"] == "/usr/bin"


def test_ci_keep_list_keeps_only_the_intended_read_only_identity():
    env = scrubbed_env(CI_ENV, keep=CI_KEEP_ENV)
    kept = CREDENTIAL_VARS & set(env)
    assert kept == {"CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE", "GOOGLE_APPLICATION_CREDENTIALS"}
    for bad in ("GH_TOKEN", "GITHUB_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_TOKEN", "ACTIONS_RUNTIME_TOKEN",
                "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "GOOGLE_GHA_CREDS_PATH"):
        assert bad not in env


def test_gcloud_test_never_captures_credentials():
    src = (REPO / "tools/tests/test_autonomy_security.py").read_text()
    block = src.split("def test_gcloud_is_unauthenticated_inside_agent_env", 1)[1].split("\ndef ", 1)[0]
    assert "capture_output" not in block and "subprocess.DEVNULL" in block


# ----------------------------------------------------- диагностика отказа федерации ---
SPEC = json.loads((REPO / "quality/autonomy/anthropic_federation.json").read_text())


def _jwt(claims: dict) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    return enc({"alg": "RS256", "kid": "k"}) + "." + enc(claims) + "." + "c2lnbmF0dXJlLXNlY3JldA"


GOOD = {"iss": "https://token.actions.githubusercontent.com", "aud": "https://api.anthropic.com",
        "sub": "repo:evelagin/evetis-wb-analytics:ref:refs/heads/main", "jti": "unique-jti-123",
        "iat": 1000, "exp": 1300, **SPEC["rules"]["engineer"]["match"]["claims"]}


def test_diagnose_names_the_mismatching_claim():
    bad = {**GOOD, "job_workflow_ref": "evelagin/evetis-wb-analytics/.github/workflows/autonomy-test.yml@refs/heads/main"}
    d = S.diagnose(S.decode_claims(_jwt(bad)), "engineer", SPEC)
    assert [m["claim"] for m in d["mismatches_vs_spec"]] == ["job_workflow_ref"]
    assert d["claims"]["lifetime_seconds"] == 300


def test_diagnose_with_matching_claims_points_to_live_rule():
    d = S.diagnose(S.decode_claims(_jwt(GOOD)), "engineer", SPEC)
    assert d["mismatches_vs_spec"] == [] and "живом правиле" in d["hint"]


def test_diagnostics_never_contain_jwt_signature_or_jti(monkeypatch, capsys):
    token = _jwt({**GOOD, "repository_id": "1"})
    monkeypatch.setattr(S, "_oidc", lambda: token)

    def reject(_assertion):
        raise S.ExchangeRejected(401)
    monkeypatch.setattr(S, "exchange", reject)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    assert S.main(["check", "--role", "engineer"]) == 1
    out = capsys.readouterr().out
    assert token not in out and token.split(".")[1] not in out and token.split(".")[2] not in out
    assert "unique-jti-123" not in out and "jti" not in json.loads(out)["claims"]
    assert any(m["claim"] == "repository_id" for m in json.loads(out)["mismatches_vs_spec"])
    assert set(json.loads(out)["claims"]) <= set(S.SAFE_CLAIMS) | {"lifetime_seconds"}
