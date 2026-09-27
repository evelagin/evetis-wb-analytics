"""M6 Phase 1 remediation: доверенный аудит «0 production-мутаций» и идемпотентная публикация кандидата.

Инцидент (2026-09-27, run-20260927T121235Z-ab2e1c53): доверенные replay-шаги перезаписывали
`audit_status` на NOT_APPLICABLE (аудит в них не настроен), поэтому приёмка не видела доказанного нуля;
публикатор при повторе создавал бы НОВЫЙ коммит кандидата и упирался в уже существующую ветку ae/*.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

import ae_fixtures as F  # noqa: E402
from tools.autonomy.agents import ReplayAdapter
from tools.autonomy.audit import zero_mutations_proven
from tools.autonomy import commissioning as C
from tools.autonomy.orchestrator import agent_run
from tools.autonomy.publisher import GitPublisher, PublishRefused
from tools.autonomy.state import TransitionError
from tools.tests.test_autonomy_ci_trust import Pipeline, run_until_testing

REPO = Path(__file__).resolve().parents[2]
WF = REPO / ".github" / "workflows"


def _run(**over) -> dict:
    base = {"run_id": "run-20260927T121235Z-ab2e1c53", "created_at": "2026-09-27T12:12:35Z", "state": "REVIEWING",
            "production_mutations": 0, "usage": [{"role": "engineer_plan"}, {"role": "reviewer"}]}
    return {**base, **over}


@pytest.fixture()
def orch(tmp_path):
    p = Pipeline(tmp_path)
    return p.orch(p.branch)


# ------------------------------------------------------------ семантика аудита ---
def test_not_applicable_never_overwrites_real_evidence(orch):
    run = orch._merge_audit(_run(), 0)
    assert run["audit_status"] == "PASS" and zero_mutations_proven(run)[0]
    again = orch._merge_audit(run, {"status": "NOT_APPLICABLE", "mutations": 0})
    assert again["audit_status"] == "PASS" and again["audit_evidence"] == run["audit_evidence"]


def test_not_applicable_alone_is_not_zero_mutations(orch):
    run = orch._merge_audit(_run(), {"status": "NOT_APPLICABLE", "mutations": 0})
    assert "audit_evidence" not in run
    ok, why = zero_mutations_proven(run)
    assert not ok and "нет доверенного аудита" in why


def test_fail_is_sticky_and_mutations_accumulate(orch):
    run = orch._merge_audit(_run(), {"status": "FAIL", "mutations": 2})
    run = orch._merge_audit(run, 0)
    assert run["audit_status"] == "FAIL" and run["production_mutations"] == 2
    assert not zero_mutations_proven(run)[0]


def test_blocked_audit_replaces_earlier_pass(orch):
    run = orch._merge_audit(orch._merge_audit(_run(), 0), {"status": "BLOCKED", "mutations": None})
    assert run["audit_status"] == "BLOCKED" and not zero_mutations_proven(run)[0]


@pytest.mark.parametrize("mutate,needle", [
    (lambda r: r["usage"].append({"role": "engineer_implement"}), "последний вызов"),
    (lambda r: r.__setitem__("created_at", "2026-09-27T12:00:00Z"), "окно аудита"),
    (lambda r: r["audit_evidence"].__setitem__("mutations", None), "production-мутации"),
    (lambda r: r.__setitem__("production_mutations", 1), "production-мутации"),
    (lambda r: r.__setitem__("audit_status", "NOT_APPLICABLE"), "а не PASS"),
])
def test_stale_or_inconsistent_evidence_fails_closed(orch, mutate, needle):
    run = orch._merge_audit(_run(), 0)
    run = json.loads(json.dumps(run))
    mutate(run)
    ok, why = zero_mutations_proven(run)
    assert not ok and needle in why


def test_trusted_audit_without_configured_audit_is_refused(tmp_path):
    p = Pipeline(tmp_path)
    run_id = p.orch(p.branch).submit(p.objective())[0]["run_id"]
    with pytest.raises(TransitionError, match="не настроен"):
        p.orch(p.branch).trusted_audit(run_id)                     # аудит по умолчанию — NOT_APPLICABLE
    assert "audit_evidence" not in p.branch.load(run_id)
    run = p.orch(p.branch, audit=lambda r: 0).trusted_audit(run_id)
    assert run["audit_evidence"]["usage_count"] == len(run["usage"]) and zero_mutations_proven(run)[0]


def test_cli_audit_requires_real_audit_configuration(tmp_path, capsys):
    from tools.autonomy.cli import main
    rc = main(["audit", "--state-dir", str(tmp_path / "s"), "--run-id", "run-20260927T121235Z-ab2e1c53"])
    assert rc == 2 and "без них доказательства нет" in capsys.readouterr().err


def test_commissioning_assess_needs_trusted_evidence():
    from tools.tests.test_autonomy_commissioning import GATE, GOOD_RUN, JOBS, MARKER, PATCH
    assert C.assess(GOOD_RUN, GATE, PATCH, JOBS, MARKER)["checks"]["zero_mutations"] is True
    without = {k: v for k, v in GOOD_RUN.items() if k != "audit_evidence"}
    assert C.assess(without, GATE, PATCH, JOBS, MARKER)["checks"]["zero_mutations"] is False
    na = {**GOOD_RUN, "audit_status": "NOT_APPLICABLE"}
    assert C.assess(na, GATE, PATCH, JOBS, MARKER)["checks"]["zero_mutations"] is False


def test_engineer_cannot_declare_zero_mutations(tmp_path):
    """Вывод недоверенного job'а (любые его файлы) не создаёт доверенного доказательства аудита."""
    p = Pipeline(tmp_path)
    run_id, out, hashes = run_until_testing(p)
    (out / "usage.json").write_text(json.dumps([{"audit_status": "PASS", "audit_evidence": {"status": "PASS"}}]))
    (out / "scratch_state.json").write_text(json.dumps({"state": "READY_FOR_PR", "audit_status": "PASS"}))
    run = p.orch(p.branch, engineer=ReplayAdapter(out, hashes)).advance(run_id, stop_before={"TESTING"})
    assert run["state"] == "TESTING" and "audit_evidence" not in run
    assert run.get("audit_status") in (None, "NOT_APPLICABLE") and not zero_mutations_proven(run)[0]


# ------------------------------------------------------------- публикация кандидата ---
def _ready_for_pr(p: Pipeline) -> str:
    """Полный доверенный путь до READY_FOR_PR + доверенный аудит (как test_full_pipeline)."""
    from tools.autonomy.agents import ScriptedAdapter, sha256_file  # noqa: F401
    from tools.autonomy.evidence import ReplayEvidenceRunner
    from tools.autonomy.orchestrator import collect_candidate_evidence, review_only
    run_id, eng_out, eng_hashes = run_until_testing(p)
    p.orch(p.branch, engineer=ReplayAdapter(eng_out, eng_hashes)).advance(run_id, stop_before={"TESTING"})
    ev_out = p.tmp / "pending-test" / "evidence.json"
    ev_sha = collect_candidate_evidence(p.orch(p.machine("test")), run_id, ev_out)
    p.orch(p.branch, evidence=ReplayEvidenceRunner(ev_out, ev_sha, F.SyntheticEvidenceRunner(), p.repo)).advance(
        run_id, stop_before={"REVIEWING"})
    rv_dir = p.tmp / "pending-review"
    rv = review_only(p.orch(p.machine("review"), reviewer=ScriptedAdapter({"reviewer": [{"respond": F.verdict()}]})),
                     run_id, rv_dir)
    assert p.orch(p.branch, reviewer=ReplayAdapter(rv_dir, rv)).advance(run_id)["state"] == "READY_FOR_PR"
    p.orch(p.branch, audit=lambda r: 0).trusted_audit(run_id)
    return run_id


class FakeGhPublisher(GitPublisher):
    """Настоящий git и настоящий bare-remote; gh подменён — сети нет."""
    def __init__(self, *a, prs=None, **k):
        super().__init__(*a, **k)
        self.gh_calls, self.prs = [], prs if prs is not None else []

    def _x(self, cmd, cwd, mutating=False):
        if cmd[0] == "gh":
            self.gh_calls.append(cmd[1:])
            if cmd[1:3] == ["pr", "list"]:
                return json.dumps(self.prs)
            if cmd[1:3] == ["pr", "create"]:
                self.prs = [{"url": "https://github.com/o/r/pull/1", "isDraft": True, "baseRefName": "main"}]
                return "https://github.com/o/r/pull/1"
            return ""
        return super()._x(cmd, cwd, mutating)


def _with_remote(p: Pipeline) -> Path:
    bare = p.tmp / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    F.git(p.repo, "remote", "add", "origin", str(bare))
    F.git(p.repo, "push", "-q", "origin", "main")
    return bare


def _publish(p: Pipeline, run_id: str, pub: GitPublisher) -> dict:
    run = p.branch.load(run_id)
    orch = p.orch(p.branch, publisher=pub)
    return pub.publish(run, orch._get(run, "candidate.patch"), orch._art(run), ["ci.yml"])


def test_republication_reuses_the_verified_candidate_without_push(tmp_path):
    p = Pipeline(tmp_path)
    bare = _with_remote(p)
    run_id = _ready_for_pr(p)
    branch = p.branch.load(run_id)["branch"]
    first = _publish(p, run_id, FakeGhPublisher(p.repo))
    remote_sha = F.git(bare, "rev-parse", f"refs/heads/{branch}")
    assert first["head_sha"] == remote_sha
    # второй проход (упал на PR): новый локальный коммит, но публикуется ПРЕЖНИЙ проверенный SHA, без push
    second_pub = FakeGhPublisher(p.repo, prs=[])
    second = _publish(p, run_id, second_pub)
    assert second["head_sha"] == remote_sha and F.git(bare, "rev-parse", f"refs/heads/{branch}") == remote_sha
    assert not any(c[:2] == ["git", "push"] for c in second_pub.log)
    assert ["pr", "create"] == second_pub.gh_calls[1][:2] and "--draft" in second_pub.gh_calls[1]


def test_existing_different_candidate_branch_is_never_overwritten(tmp_path):
    p = Pipeline(tmp_path)
    bare = _with_remote(p)
    run_id = _ready_for_pr(p)
    branch = p.branch.load(run_id)["branch"]
    F.git(p.repo, "checkout", "-q", "-b", "tamper")
    (p.repo / "synthetic" / "evil.py").write_text("x = 1\n")
    F.git(p.repo, "add", "-A"); F.git(p.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "x")
    F.git(p.repo, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
    before = F.git(bare, "rev-parse", f"refs/heads/{branch}")
    F.git(p.repo, "checkout", "-q", "main")
    pub = FakeGhPublisher(p.repo)
    with pytest.raises(PublishRefused, match="не совпадает с проверенным"):
        _publish(p, run_id, pub)
    assert F.git(bare, "rev-parse", f"refs/heads/{branch}") == before and pub.gh_calls == []


@pytest.mark.parametrize("pr", [{"url": "u", "isDraft": False, "baseRefName": "main"},
                                {"url": "u", "isDraft": True, "baseRefName": "release"}])
def test_existing_non_draft_or_foreign_base_pr_is_refused(tmp_path, pr):
    p = Pipeline(tmp_path)
    _with_remote(p)
    run_id = _ready_for_pr(p)
    pub = FakeGhPublisher(p.repo, prs=[pr])
    with pytest.raises(PublishRefused, match="не draft"):
        _publish(p, run_id, pub)
    assert not any(c[:2] == ["pr", "create"] for c in pub.gh_calls)


def test_existing_draft_pr_is_reused_not_duplicated(tmp_path):
    p = Pipeline(tmp_path)
    _with_remote(p)
    run_id = _ready_for_pr(p)
    pub = FakeGhPublisher(p.repo, prs=[{"url": "https://github.com/o/r/pull/7", "isDraft": True, "baseRefName": "main"}])
    assert _publish(p, run_id, pub)["url"] == "https://github.com/o/r/pull/7"
    assert not any(c[:2] == ["pr", "create"] for c in pub.gh_calls)


def test_publication_refused_without_trusted_audit(tmp_path):
    p = Pipeline(tmp_path)
    _with_remote(p)
    run_id = _ready_for_pr(p)
    run = p.branch.load(run_id)
    run.pop("audit_evidence"); run["audit_status"] = "NOT_APPLICABLE"
    p.branch.save(run)
    pub = FakeGhPublisher(p.repo)
    with pytest.raises(PublishRefused, match="0 production-мутаций"):
        _publish(p, run_id, pub)
    assert pub.gh_calls == [] and not any(c[:2] == ["git", "push"] for c in pub.log)


@pytest.mark.parametrize("args", [["pr", "review", "1", "--approve"], ["pr", "merge", "1"], ["pr", "ready", "1"],
                                  ["pr", "create", "--base", "main"], ["api", "repos/o/r/pulls/1/merge"]])
def test_gh_wrapper_refuses_approve_merge_ready_and_non_draft(tmp_path, args):
    class NoExec(GitPublisher):                       # gh в тесте не исполняется никогда
        def _x(self, cmd, cwd, mutating=False):
            raise AssertionError(f"страж пропустил команду к исполнению: {cmd}")
    with pytest.raises(PublishRefused, match="не разрешено|без --draft запрещён"):
        NoExec(tmp_path, dry_run=True)._gh(args, tmp_path)


# ------------------------------------------------------------------ статические стражи ---
def test_publisher_source_has_no_approve_merge_or_force():
    src = (REPO / "tools/autonomy/publisher.py").read_text()
    for bad in ("pr merge", "pr review", "pr ready", '"merge"', "--admin", "+refs/", "--approve", "--force-with-lease"):
        assert bad not in src, bad
    pushes = re.findall(r'\["git", "push"[^\]]*\]', src)
    assert pushes == ['["git", "push", self.remote, refspec]'], pushes          # без -f/--force/+refspec
    assert 'base_branch: str = "main"' in src and '"--draft"' in src
    assert re.search(r"GitPublisher\(REPO, dry_run=a\.dry_run\)", (REPO / "tools/autonomy/cli.py").read_text())


def test_only_the_publish_job_has_pull_requests_write():
    from tools.tests.test_autonomy_security import AE_WORKFLOWS, jobs, text
    holders = [(wf.name, job) for wf in AE_WORKFLOWS for job, body in jobs(text(wf)).items()
               if "pull-requests: write" in body]
    assert holders == [("autonomy-run.yml", "publish")]
    for wf in AE_WORKFLOWS:
        assert not re.search(r"gh pr (merge|review|ready)|pulls/\S+/(merge|reviews)", text(wf)), wf.name


def test_audit_step_sits_between_gate_and_publish_and_uses_real_audit():
    from tools.tests.test_autonomy_security import jobs
    run = (WF / "autonomy-run.yml").read_text()
    js = jobs(run)
    assert "needs: [prepare, gate]" in js["audit"] and "step: audit" in js["audit"]
    assert "needs: [prepare, audit]" in js["publish"]
    gate = (WF / "autonomy-gate.yml").read_text()
    assert gate.count("if: inputs.step == 'prepare' || inputs.step == 'audit'") == 2
    block = gate.split("            audit)", 1)[1].split(";;", 1)[0]
    for flag in ('--project "$GCP_PROJECT_ID"', '--token-command "gcloud auth print-access-token"',
                 '--audit-identity "$AE_READER_SA"', 'READY_FOR_PR'):
        assert flag in block, flag


# ------------------------------------------------------ стоимость в отчёте (issue #201) ---
def test_report_cost_comes_from_verified_diagnostics_without_double_counting(tmp_path):
    """Инженер: план + реализация в одном пакете диагностики → каждая запись usage получает стоимость
    СВОЕЙ роли; сумма = сумма вызовов, а не удвоенный пакет."""
    import uuid
    from tools.autonomy.agents import ClaudeCliAdapter
    from tools.autonomy.report import render_report
    from tools.tests.test_autonomy_agent_diagnostics import cli_json
    p = Pipeline(tmp_path)
    run_id = p.orch(p.branch).submit(p.objective())[0]["run_id"]
    p.orch(p.branch).advance(run_id, stop_before={"PLANNING"})
    d = tmp_path / f"cli-{uuid.uuid4().hex[:6]}"; d.mkdir()
    plan = json.dumps(json.loads(cli_json(structured_output=F.plan(), total_cost_usd=0.25)))
    impl = json.dumps(json.loads(cli_json(structured_output=F.implemented(), total_cost_usd=0.5)))
    (d / "plan.json").write_text(plan); (d / "impl.json").write_text(impl)
    script = d / "claude"
    script.write_text("#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then echo '2.1.280 (Claude Code)'; exit 0; fi\n"
                      "cat > /dev/null\n"
                      f"if printf '%s ' \"$@\" | grep -q 'Edit Write'; then (cd \"$PWD\" && python3 -c \"import pathlib; p=pathlib.Path('synthetic/calc.py'); p.write_text(p.read_text().replace('return 0', 'return sum(xs)'))\" 2>/dev/null; cat '{d}/impl.json'); else cat '{d}/plan.json'; fi\n")
    script.chmod(0o755)
    out = tmp_path / "pending"
    hashes = agent_run(lambda s: p.orch(s, engineer=ClaudeCliAdapter(binary=str(script))), p.machine("engineer"),
                       run_id, out)
    run = p.orch(p.branch, engineer=ReplayAdapter(out, hashes)).advance(run_id, stop_before={"TESTING", "REVIEWING"})
    costs = {u["role"]: u.get("total_cost_usd") for u in run["usage"]}
    assert costs == {"engineer_plan": 0.25, "engineer_implement": 0.5}, run["usage"]   # не 0.75 + 0.75
    total = sum(float(u.get("total_cost_usd") or 0) for u in run["usage"])
    assert "$%.2f" % total in render_report(run, p.branch.root / "artifacts" / run_id)
    assert all(u.get("cost_source") == "cli_reported_untrusted_job" for u in run["usage"] if "total_cost_usd" in u)


def test_report_cost_falls_back_to_trusted_diagnostics_copy_for_legacy_entries(tmp_path):
    """Записи usage, сделанные до исправления (без total_cost_usd), берут стоимость своей роли из
    доверенной копии диагностики; путь вне artifacts/<run>/diagnostics/ не читается."""
    from tools.autonomy.report import _usage_cost
    art = tmp_path / "art"; (art / "diagnostics").mkdir(parents=True)
    inv = lambda role, c: {"role": role, "total_cost_usd": c}  # noqa: E731
    (art / "diagnostics" / "engineer_plan-01.json").write_text(json.dumps(
        {"invocations": [inv("engineer_plan", 0.26784), inv("engineer_implement", 0.23208)]}))
    assert _usage_cost({"role": "engineer_plan", "diagnostics": {"file": "diagnostics/engineer_plan-01.json"}}, art) == 0.26784
    assert _usage_cost({"role": "engineer_plan", "total_cost_usd": 0.1,
                        "diagnostics": {"file": "diagnostics/engineer_plan-01.json"}}, art) == 0.1
    (tmp_path / "evil.json").write_text(json.dumps({"invocations": [inv("engineer_plan", 99)]}))
    assert _usage_cost({"role": "engineer_plan", "diagnostics": {"file": "diagnostics/../../evil.json"}}, art) == 0.0
    assert _usage_cost({"role": "reviewer"}, art) == 0.0
