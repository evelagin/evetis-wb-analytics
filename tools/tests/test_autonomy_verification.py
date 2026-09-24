"""S8 — автономный PR получает фактическую обязательную проверку; S5 — доверенный шаг не
исполняет код кандидата и не принимает непроверяемый вывод.

S8. PR от GITHUB_TOKEN не запускает pull_request-CI. READY_FOR_HUMAN_REVIEW ставится только
после того, как ВСЕ обязательные workflows завершились success на опубликованном SHA —
по данным API Actions, а не по словам агента или по факту создания PR.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tools.autonomy.agents import IntegrityError, ReplayAdapter, ScriptedAdapter, sha256_file
from tools.autonomy.evidence import RepoEvidenceRunner, ReplayEvidenceRunner, Sandbox, _run, evidence_env
from tools.autonomy.policy import load_policy
from tools.autonomy.publisher import GitPublisher, PublishRefused
from tools.autonomy.state import TRANSITIONS, StateStore, TransitionError
from tools.autonomy.verification import evaluate
from tools.tests import ae_fixtures as F
from tools.tests.test_autonomy_acceptance import dispatch_synthetic, env, orchestrator  # noqa: F401

WF = F.REPO / ".github" / "workflows"
REQUIRED = load_policy()["required_verification"]["workflows"]
SHA = "a" * 40
BR = "ae/fix-x-1234abcd"
T0 = "2026-09-24T10:00:00Z"


def _run_obj(wf, **kw):
    base = {"id": 1, "head_sha": SHA, "head_branch": BR, "event": "workflow_dispatch",
            "path": f".github/workflows/{wf}", "created_at": "2026-09-24T10:00:05Z",
            "status": "completed", "conclusion": "success", "html_url": "u"}
    return {**base, **kw}


# ------------------------------------------------------------------- решение по прогонам ---
def test_all_required_success_is_pass():
    r = evaluate(REQUIRED, {wf: [_run_obj(wf)] for wf in REQUIRED}, SHA, BR, T0)
    assert r["status"] == "PASS" and [x["status"] for x in r["runs"]] == ["PASS"] * len(REQUIRED)


def test_one_failure_is_fail():
    runs = {wf: [_run_obj(wf)] for wf in REQUIRED}
    runs[REQUIRED[-1]] = [_run_obj(REQUIRED[-1], conclusion="failure")]
    assert evaluate(REQUIRED, runs, SHA, BR, T0)["status"] == "FAIL"


@pytest.mark.parametrize("conclusion", ["cancelled", "timed_out", "skipped", "neutral", "action_required", None])
def test_anything_but_success_is_not_pass(conclusion):
    runs = {wf: [_run_obj(wf, conclusion=conclusion)] for wf in REQUIRED}
    assert evaluate(REQUIRED, runs, SHA, BR, T0)["status"] == "FAIL"


def test_in_progress_or_missing_is_pending():
    runs = {REQUIRED[0]: [_run_obj(REQUIRED[0], status="in_progress", conclusion=None)]}
    assert evaluate(REQUIRED, runs, SHA, BR, T0)["status"] == "PENDING"
    assert evaluate(REQUIRED, {}, SHA, BR, T0)["status"] == "PENDING"


@pytest.mark.parametrize("field,value", [
    ("head_sha", "b" * 40),                           # прогон другого коммита
    ("head_branch", "main"),                          # прогон другой ветки
    ("event", "push"), ("event", "pull_request"),     # не наш диспатч
    ("created_at", "2026-09-24T09:00:00Z"),           # старый прогон до публикации
    ("path", ".github/workflows/other.yml"),          # другой файл
])
def test_foreign_green_runs_do_not_count(field, value):
    runs = {wf: [_run_obj(wf, **{field: value})] for wf in REQUIRED}
    assert evaluate(REQUIRED, runs, SHA, BR, T0)["status"] == "PENDING"


def test_latest_attempt_decides():
    wf = REQUIRED[0]
    runs = {w: [_run_obj(w)] for w in REQUIRED}
    runs[wf] = [_run_obj(wf, id=1, conclusion="success", created_at="2026-09-24T10:00:05Z"),
                _run_obj(wf, id=2, conclusion="failure", created_at="2026-09-24T10:05:00Z")]
    assert evaluate(REQUIRED, runs, SHA, BR, T0)["status"] == "FAIL"


# ------------------------------------------------------------------- машина состояний ---
def test_publication_is_not_readiness():
    assert "READY_FOR_HUMAN_REVIEW" not in TRANSITIONS["READY_FOR_PR"]
    assert "COMPLETED" not in TRANSITIONS["READY_FOR_PR"]
    assert TRANSITIONS["READY_FOR_PR"] == {"AWAITING_VERIFICATION", "FAILED"}
    reach = {s for s, nxt in TRANSITIONS.items() if "READY_FOR_HUMAN_REVIEW" in nxt}
    assert reach == {"AWAITING_VERIFICATION"}


def _published(env, verifier):
    objective = dispatch_synthetic(env)
    eng = ScriptedAdapter({"engineer_plan": [{"respond": F.plan()}],
                           "engineer_implement": [{"edit": F.edit_fix_with_extra_test, "respond": F.implemented()}]})
    rev = ScriptedAdapter({"reviewer": [{"respond": F.verdict("PASS")}]})
    orch, pub = orchestrator(env, eng, rev, verifier=verifier)
    return orch, orch.submit(objective)[0]["run_id"]


def test_without_verification_the_run_stops_at_awaiting(env):
    objective = dispatch_synthetic(env)
    eng = ScriptedAdapter({"engineer_plan": [{"respond": F.plan()}],
                           "engineer_implement": [{"edit": F.edit_fix_with_extra_test, "respond": F.implemented()}]})
    rev = ScriptedAdapter({"reviewer": [{"respond": F.verdict("PASS")}]})
    orch, _ = orchestrator(env, eng, rev)
    orch.verifier = None
    run = orch.advance(orch.submit(objective)[0]["run_id"])
    assert run["state"] == "AWAITING_VERIFICATION" and run["verification"]["result"] is None


def test_required_ci_failure_blocks(env):
    orch, run_id = _published(env, F.ci_verifier(conclusion="failure"))
    run = orch.advance(run_id)
    assert run["state"] == "BLOCKED" and run["last_gate"]["verdict"] == "BLOCKED_BY_TEST"


def test_required_ci_pending_waits_then_times_out(env):
    orch, run_id = _published(env, F.ci_verifier(status="in_progress"))
    run = orch.advance(run_id)
    assert run["state"] == "AWAITING_VERIFICATION"              # ждём, не «готово»
    later = datetime.now(timezone.utc) + timedelta(minutes=load_policy()["required_verification"]["timeout_minutes"] + 1)
    orch.now = lambda: later
    run = orch.advance(run_id)
    assert run["state"] == "BLOCKED" and run["last_gate"]["verdict"] == "INCONCLUSIVE"


def test_green_run_of_another_commit_is_not_accepted(env):
    orch, run_id = _published(env, F.ci_verifier(sha="b" * 40))
    assert orch.advance(run_id)["state"] == "AWAITING_VERIFICATION"


# ------------------------------------------------------------ обязательные workflows ---
@pytest.mark.parametrize("wf", REQUIRED)
def test_required_workflow_is_dispatchable_and_carries_no_credentials(wf):
    text = (WF / wf).read_text()
    on_block = text.split("\non:\n", 1)[1].split("\n\n", 1)[0]
    assert re.search(r"^  workflow_dispatch:", on_block, re.M), \
        f"{wf}: без workflow_dispatch GITHUB_TOKEN не сможет его запустить"
    # Он исполняет код кандидата: ни секретов, ни OIDC, ни прав записи.
    assert "secrets." not in text and "id-token" not in text
    perms = re.findall(r"^(\s*)permissions:\s*\n((?:\1  [a-z-]+: [a-z]+\n)+)", text, re.M)
    assert perms and all(block.split() == ["contents:", "read"] for _, block in perms), perms
    assert not re.search(r"permissions:\s*(write-all|\{[^}]*write)", text)


@pytest.mark.parametrize("wf", REQUIRED)
def test_required_workflow_does_not_restore_unverified_caches(wf):
    """Недоверенный job AE (ref main) может записать в кэш области main; прогон на ветке ae/*
    читает кэш main. pip-кэш без --require-hashes — незаметная подмена зависимостей.
    npm-кэш допустим: npm ci сверяет integrity каждого пакета с package-lock."""
    text = (WF / wf).read_text()
    assert "cache: pip" not in text and "actions/cache" not in text


def test_publisher_dispatches_only_policy_workflows(tmp_path):
    pub = GitPublisher(tmp_path, dry_run=True)
    art = tmp_path / "art"; art.mkdir()
    (art / "gate.json").write_text(json.dumps({"verdict": "READY_FOR_PR"}))
    run = {"branch": "ae/x-12345678", "run_id": "r", "objective_id": "o", "repository_sha": "a" * 40,
           "production_mutations": 0}
    with pytest.raises((PublishRefused, subprocess.CalledProcessError)):
        pub.publish(run, "", art, ["deploy-prod.yml; rm -rf /"])


# ======================================================== S5: доверенный шаг и код кандидата ===
BENIGN_IMPACT = '''import json, sys
out = sys.argv[sys.argv.index("--output") + 1]
json.dump({"risk_tier": "TIER2", "required_contracts": {}, "affected_total": 0}, open(out, "w"))
'''


def _repo_with_impact_tool(tmp_path):
    repo, _ = F.make_repo(tmp_path)
    (repo / "tools").mkdir(exist_ok=True)
    (repo / "tools" / "impact_analysis.py").write_text(BENIGN_IMPACT)
    F.git(repo, "add", "-A")
    F.git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "impact tool")
    return repo, F.git(repo, "rev-parse", "HEAD")


def test_trusted_replay_never_executes_candidate_impact_tool(tmp_path):
    """Атака: кандидат подменяет tools/impact_analysis.py. Доверенный verify раньше исполнял его
    в песочнице кандидата — рядом с GH_TOKEN (contents: write)."""
    repo, base = _repo_with_impact_tool(tmp_path)
    canary = tmp_path / "CANDIDATE_CODE_RAN"
    sb = Sandbox(repo, base, tmp_path / "sb")
    ws = sb.create()
    (ws / "tools" / "impact_analysis.py").write_text(f"open({str(canary)!r}, 'w').write('pwned')\n" + BENIGN_IMPACT)
    files = sb.changed_files()
    # Контроль: вектор реален — исполнение инструмента В ПЕСОЧНИЦЕ запускает код кандидата.
    RepoEvidenceRunner().impact(ws, files)
    assert canary.exists()
    canary.unlink()
    # Доверенный путь: анализ влияния считается по доверенному checkout, песочница — только данные.
    ev = {"tests": [], "sql_validation": {"status": "NOT_APPLICABLE"}, "runtime_access": {"status": "NOT_APPLICABLE"},
          "parity": {"status": "NOT_APPLICABLE"}, "data_suites": {}, "objective_resolution": "NOT_APPLICABLE",
          "changed_files": files}
    ev_file = tmp_path / "evidence.json"
    ev_file.write_text(json.dumps(ev))
    replay = ReplayEvidenceRunner(ev_file, sha256_file(ev_file), RepoEvidenceRunner(), trusted_repo=repo)
    out = replay.collect(ws, files, {})
    assert not canary.exists(), "доверенный шаг исполнил код кандидата"
    assert out["impact"]["risk_tier"] == "TIER2"
    sb.cleanup()


def test_replay_without_declared_hashes_is_refused(tmp_path):
    with pytest.raises(IntegrityError):
        ReplayAdapter(tmp_path, None)
    with pytest.raises(IntegrityError):
        ReplayEvidenceRunner(tmp_path / "e.json", "", RepoEvidenceRunner(), trusted_repo=tmp_path)


def test_cli_replay_without_expect_fails_closed(tmp_path):
    r = subprocess.run([sys.executable, "-m", "tools.autonomy.cli", "advance", "--engineer", "replay",
                        "--reviewer", "none", "--pending-dir", str(tmp_path), "--state-dir", str(tmp_path / "s"),
                        "--run-id", "run-x"], cwd=F.REPO, capture_output=True, text=True)
    assert r.returncode != 0 and "expect" in (r.stderr + r.stdout)


def test_evidence_subprocesses_do_not_inherit_github_or_actions_tokens(monkeypatch, tmp_path):
    for k in ("GH_TOKEN", "GITHUB_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_URL",
              "ACTIONS_RUNTIME_TOKEN", "ANTHROPIC_API_KEY"):
        monkeypatch.setenv(k, "secret-value")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/tmp/gha-creds.json")
    probe = ("GH_TOKEN", "GITHUB_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_URL",
             "ACTIONS_RUNTIME_TOKEN", "ANTHROPIC_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS")
    # Печатаем только пробу: полное окружение раннера длиннее хвоста, который хранит _run.
    r = _run([sys.executable, "-c", f"import os, json; print(json.dumps([k for k in {probe!r} if k in os.environ]))"],
             tmp_path)
    seen = set(json.loads(r["tail"]))
    assert not seen & {"GH_TOKEN", "GITHUB_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_URL",
                       "ACTIONS_RUNTIME_TOKEN", "ANTHROPIC_API_KEY"}
    assert "GOOGLE_APPLICATION_CREDENTIALS" in seen           # read-only SA для доказательств остаётся
    assert "PATH" in evidence_env({"PATH": "/bin", "GH_TOKEN": "x"})


def test_objective_with_base_outside_trusted_branch_is_refused(env):
    objective = dispatch_synthetic(env)
    F.git(env["repo"], "checkout", "-qb", "ae/evil")
    (env["repo"] / "synthetic" / "evil.py").write_text("x = 1\n")
    F.git(env["repo"], "add", "-A")
    F.git(env["repo"], "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "evil")
    evil = F.git(env["repo"], "rev-parse", "HEAD")
    F.git(env["repo"], "checkout", "-q", "main")
    orch, _ = orchestrator(env, ScriptedAdapter({}), ScriptedAdapter({}))
    with pytest.raises(TransitionError, match="не входит в историю"):
        orch.submit({**objective, "repository_sha": evil})
