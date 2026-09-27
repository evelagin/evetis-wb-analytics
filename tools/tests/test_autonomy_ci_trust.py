"""Модель доверия CI: конвейер из отдельных «машин» и отказ от подделанных артефактов.

Каждый job GitHub Actions — отдельная эфемерная машина со своей копией состояния.
Авторитетное хранилище (ветка autonomy-state) меняют только доверенные jobs; модельные и
тестовые jobs отдают недоверенный вывод, чей sha256 объявлен их outputs. Здесь это
воспроизведено копиями каталогов: «ветка» — один каталог, «машины» — копии.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import ae_fixtures as F  # noqa: E402
from tools.autonomy.agents import AgentNotPermitted, IntegrityError, NoAgentAdapter, ReplayAdapter, ScriptedAdapter
from tools.autonomy.evidence import ReplayEvidenceRunner
from tools.autonomy.orchestrator import Orchestrator, agent_run, collect_candidate_evidence, next_pass, review_only
from tools.autonomy.publisher import GitPublisher
from tools.autonomy.state import StateStore
from tools.autonomy.watcher import watch


class Pipeline:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.repo, self.sha = F.make_repo(tmp)
        self.branch = StateStore(tmp / "autonomy-state")          # авторитетное состояние

    def machine(self, name: str) -> StateStore:
        """Новый раннер: клон ветки состояния, ничего общего с другими раннерами."""
        root = self.tmp / f"runner-{name}" / "state"
        if root.exists():
            shutil.rmtree(root)
        shutil.copytree(self.branch.root, root)
        return StateStore(root)

    def orch(self, store, engineer=None, reviewer=None, evidence=None, publisher=None, verifier=None, audit=None):
        extra = {"audit": audit} if audit is not None else {}
        return Orchestrator(store, self.repo, engineer or NoAgentAdapter(), reviewer or NoAgentAdapter(),
                            evidence or F.SyntheticEvidenceRunner(), self.tmp / "sandboxes", publisher=publisher,
                            verifier=verifier, trusted_base_ref="main", **extra)

    def objective(self) -> dict:
        out = self.tmp / "objectives"
        watch(F.observations("FAIL"), self.branch, self.sha, out, synthetic=True)
        rep = watch(F.observations("FAIL"), self.branch, self.sha, out, synthetic=True)
        return json.loads(Path(rep["dispatch"][0]["objective_path"]).read_text())


def engineer_script():
    return ScriptedAdapter({"engineer_plan": [{"respond": F.plan()}],
                            "engineer_implement": [{"edit": F.edit_fix_with_extra_test, "respond": F.implemented()}]})


def run_until_testing(p: Pipeline) -> tuple[str, Path, dict]:
    # prepare (доверенный): открыть прогон, собрать базовые доказательства
    orch = p.orch(p.branch)
    run_id = orch.submit(p.objective())[0]["run_id"]
    assert orch.advance(run_id, stop_before={"PLANNING"})["state"] == "PLANNING"
    # engineer (недоверенный): агент на черновой копии; авторитетное состояние не трогается
    eng_machine = p.machine("engineer")
    out = p.tmp / "pending-engineer"
    hashes = agent_run(lambda s: p.orch(s, engineer=engineer_script()), eng_machine, run_id, out)
    assert set(hashes) == {"engineer_plan.json", "engineer_implement.json", "engineer_implement.patch",
                           "engineer_diagnostics.json"}
    assert eng_machine.load(run_id)["state"] == "PLANNING"          # машина инженера тоже не изменилась
    return run_id, out, hashes


def test_full_pipeline_across_isolated_runners(tmp_path):
    p = Pipeline(tmp_path)
    run_id, eng_out, eng_hashes = run_until_testing(p)
    # ingest (доверенный): воспроизвести план и реализацию с проверкой хешей
    run = p.orch(p.branch, engineer=ReplayAdapter(eng_out, eng_hashes)).advance(run_id, stop_before={"TESTING"})
    assert run["state"] == "TESTING" and run["iteration"] == 1
    # test (недоверенный, исполняет код кандидата): доказательства без решений
    ev_out = tmp_path / "pending-test" / "evidence.json"
    ev_sha = collect_candidate_evidence(p.orch(p.machine("test")), run_id, ev_out)
    # verify (доверенный): доказательства → переход
    verify = p.orch(p.branch, evidence=ReplayEvidenceRunner(ev_out, ev_sha, F.SyntheticEvidenceRunner(), p.repo))
    assert verify.advance(run_id, stop_before={"REVIEWING"})["state"] == "REVIEWING"
    # review (модель, чистая машина): только вердикт
    rv_dir = tmp_path / "pending-review"
    rv_hashes = review_only(p.orch(p.machine("review"), reviewer=ScriptedAdapter({"reviewer": [{"respond": F.verdict()}]})),
                            run_id, rv_dir)
    assert set(rv_hashes) == {"reviewer.json", "reviewer_diagnostics.json"}
    # gate (доверенный): воспроизвести вердикт, решить
    gate = p.orch(p.branch, reviewer=ReplayAdapter(rv_dir, rv_hashes))
    assert gate.advance(run_id)["state"] == "READY_FOR_PR"
    # audit (доверенный, sa-ae-reader): 0 production-мутаций за всё окно — без него публикации нет
    assert p.orch(p.branch, audit=lambda run: 0).trusted_audit(run_id)["audit_status"] == "PASS"
    # publish (доверенный, запись только ae/*)
    pub = GitPublisher(p.repo, dry_run=True)
    run = p.orch(p.branch, publisher=pub).advance(run_id)
    assert run["state"] == "AWAITING_VERIFICATION" and run["production_mutations"] == 0
    # ci-verify (доверенный, actions: read): решение по фактическим прогонам на опубликованном SHA
    run = p.orch(p.branch, verifier=F.ci_verifier()).advance(run_id)
    assert run["state"] == "READY_FOR_HUMAN_REVIEW"
    assert next_pass(p.branch, run_id) == "report"


def test_tampered_engineer_patch_is_rejected_and_state_is_unchanged(tmp_path):
    p = Pipeline(tmp_path)
    run_id, eng_out, eng_hashes = run_until_testing(p)
    patch = eng_out / "engineer_implement.patch"
    patch.write_text(patch.read_text() + "\n")                    # подмена после объявления хеша
    orch = p.orch(p.branch, engineer=ReplayAdapter(eng_out, eng_hashes))
    with pytest.raises(IntegrityError):
        orch.advance(run_id, stop_before={"TESTING"})
    assert p.branch.load(run_id)["state"] in ("PLANNING", "IMPLEMENTING")
    assert p.branch.load(run_id)["state"] != "TESTING"


def test_forged_state_files_in_untrusted_output_are_ignored(tmp_path):
    p = Pipeline(tmp_path)
    run_id, eng_out, eng_hashes = run_until_testing(p)
    # недоверенный job подкладывает «готовое» состояние и «зелёный» гейт
    (eng_out / f"{run_id}.json").write_text(json.dumps({"state": "READY_FOR_PR"}))
    (eng_out / "gate.json").write_text(json.dumps({"verdict": "READY_FOR_PR"}))
    (eng_out / "reviewer.json").write_text(json.dumps(F.verdict("PASS")))
    run = p.orch(p.branch, engineer=ReplayAdapter(eng_out, eng_hashes)).advance(run_id, stop_before={"TESTING"})
    assert run["state"] == "TESTING"                              # подложенное не прочитано
    # подложенный инженером «вердикт ревьюера» отвергается: его хеш никто не объявлял
    with pytest.raises(IntegrityError, match="reviewer.json"):
        ReplayAdapter(eng_out, {}).run("reviewer", "", tmp_path, {})


def test_tampered_or_mismatched_evidence_is_rejected(tmp_path):
    p = Pipeline(tmp_path)
    run_id, eng_out, eng_hashes = run_until_testing(p)
    p.orch(p.branch, engineer=ReplayAdapter(eng_out, eng_hashes)).advance(run_id, stop_before={"TESTING"})
    ev_out = tmp_path / "pending-test" / "evidence.json"
    ev_sha = collect_candidate_evidence(p.orch(p.machine("test")), run_id, ev_out)
    forged = json.loads(ev_out.read_text()); forged["tests"][0]["status"] = "PASS"
    ev_out.write_text(json.dumps(forged))
    verify = p.orch(p.branch, evidence=ReplayEvidenceRunner(ev_out, ev_sha, F.SyntheticEvidenceRunner(), p.repo))
    with pytest.raises(IntegrityError, match="sha256"):
        verify.advance(run_id, stop_before={"REVIEWING"})
    other = dict(forged, changed_files=["synthetic/other.py"])
    ev_out.write_text(json.dumps(other))
    from tools.autonomy.agents import sha256_file
    verify = p.orch(p.branch, evidence=ReplayEvidenceRunner(ev_out, sha256_file(ev_out), F.SyntheticEvidenceRunner(), p.repo))
    with pytest.raises(IntegrityError, match="не по этому кандидату"):
        verify.advance(run_id, stop_before={"REVIEWING"})
    assert p.branch.load(run_id)["state"] == "TESTING"


def test_deterministic_job_cannot_call_an_agent(tmp_path):
    p = Pipeline(tmp_path)
    orch = p.orch(p.branch)
    run_id = orch.submit(p.objective())[0]["run_id"]
    orch.advance(run_id, stop_before={"PLANNING"})
    with pytest.raises(AgentNotPermitted):
        orch.advance(run_id)                                       # PLANNING требует инженера
    assert p.branch.load(run_id)["state"] == "PLANNING"


def test_next_pass_never_loops_on_a_crashed_job(tmp_path):
    p = Pipeline(tmp_path)
    orch = p.orch(p.branch)
    run_id = orch.submit(p.objective())[0]["run_id"]
    orch.advance(run_id, stop_before={"PLANNING"})
    assert next_pass(p.branch, run_id) == "infra_failure"           # застрял в рабочем состоянии
    run = p.branch.load(run_id)
    for to in ("IMPLEMENTING", "TESTING", "FIXING"):
        run = p.branch.transition(run, to, "симуляция")
    decisions = [next_pass(p.branch, run_id) for _ in range(12)]
    assert decisions.count("dispatch") < 12 and decisions[-1] == "exhausted"
