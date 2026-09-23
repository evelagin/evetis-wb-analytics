"""Приёмка Autonomous Engineering v1: сценарии A–F из Definition of Done.

Агенты здесь — ScriptedAdapter: проверяется не «ум» модели, а поведение системы вокруг
неё — изоляция, бюджеты, возврат на доработку, отказ ворот, дедупликация, восстановление
после падения. Живой прогон с настоящим Claude Code — отдельно
(`docs/architecture/AE_V1_ACCEPTANCE.md`).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import ae_fixtures as F  # noqa: E402 — добавляет корень репозитория в sys.path
from tools.autonomy.agents import AgentResult, ScriptedAdapter
from tools.autonomy.envelope import known_checks
from tools.autonomy.orchestrator import Orchestrator
from tools.autonomy.policy import load_policy
from tools.autonomy.publisher import GitPublisher
from tools.autonomy.schema import require_valid
from tools.autonomy.state import StateStore, TransitionError, dedup_slug
from tools.autonomy.watcher import watch

BUDGET = load_policy()["budgets"]


@pytest.fixture()
def env(tmp_path):
    repo, sha = F.make_repo(tmp_path)
    return {"repo": repo, "sha": sha, "store": StateStore(tmp_path / "state"),
            "out": tmp_path / "out", "sandboxes": tmp_path / "sandboxes"}


def dispatch_synthetic(env) -> dict:
    """Два наблюдения FAIL подряд → устойчивое новое падение → цель инженеру."""
    first = watch(F.observations("FAIL"), env["store"], env["sha"], env["out"], synthetic=True)
    assert first["status"] == "ATTENTION" and first["dispatch"] == []      # первое падение: только наблюдаем
    second = watch(F.observations("FAIL"), env["store"], env["sha"], env["out"], synthetic=True)
    assert second["status"] == "ACTION" and len(second["dispatch"]) == 1
    objective = json.loads(Path(second["dispatch"][0]["objective_path"]).read_text())
    require_valid(objective, "objective")
    require_valid(objective["incident"], "incident")
    return objective


def orchestrator(env, engineer, reviewer, audit=lambda run: 0, publish=True):
    pub = GitPublisher(env["repo"], dry_run=True) if publish else None
    orch = Orchestrator(env["store"], env["repo"], engineer, reviewer, F.SyntheticEvidenceRunner(),
                        env["sandboxes"], audit=audit, publisher=pub)
    return orch, pub


# ======================================================================= A ===
def test_A_healthy_no_op(env):
    """Здоровая область: ни вызова модели, ни ветки, ни PR, машинный HEALTHY."""
    known_id = next(iter(known_checks()))           # реальная проверка открытого UBR
    obs = F.observations("PASS") + [{"key": f"check:{known_id}", "kind": "check", "check_id": known_id,
                                     "suite": "wb_sku_performance_v2", "status": "FAIL"}]
    rep = watch(obs, env["store"], env["sha"], env["out"])
    assert rep["status"] == "HEALTHY"
    assert rep["llm_invocations"] == 0 and rep["dispatch"] == [] and rep["notify"] == []
    assert {r["class"] for r in rep["results"]} == {"HEALTHY", "KNOWN"}
    assert not list((env["store"].root / "runs").glob("*.json"))
    assert not env["out"].exists() or not list(env["out"].glob("*.json"))
    assert F.git(env["repo"], "branch", "--list", "ae/*") == ""


def test_A_flapping_check_is_notified_but_never_dispatched(env):
    for s in ("FAIL", "PASS", "FAIL", "PASS", "FAIL"):
        rep = watch(F.observations(s), env["store"], env["sha"], env["out"], synthetic=True)
    assert rep["results"][0]["class"] == "FLAPPING" and rep["dispatch"] == []


# ======================================================================= B ===
def test_B_synthetic_failure_end_to_end(env):
    objective = dispatch_synthetic(env)
    eng = ScriptedAdapter({"engineer_plan": [{"respond": F.plan()}],
                           "engineer_implement": [{"edit": F.edit_fix_with_extra_test, "respond": F.implemented()}]})
    rev = ScriptedAdapter({"reviewer": [{"respond": F.verdict("PASS")}]})
    orch, pub = orchestrator(env, eng, rev)
    run, created = orch.submit(objective)
    assert created and run["branch"] == dedup_slug(objective["deduplication_key"])
    run = orch.advance(run["run_id"])

    assert run["state"] == "COMPLETED", run["transitions"]
    assert run["pr_url"] == f"dry-run://{run['branch']}"
    assert run["production_mutations"] == 0
    art = env["store"].root / "artifacts" / run["run_id"]
    assert json.loads((art / "gate.json").read_text())["verdict"] == "READY_FOR_PR"
    assert json.loads((art / "evidence.json").read_text())["objective_resolution"] == "RESOLVED"
    # изоляция: инженер и ревьюер работали в разных песочницах, ни одна — не репозиторий оператора
    eng_dirs = {c["workdir"] for c in eng.calls}
    rev_dirs = {c["workdir"] for c in rev.calls}
    assert not eng_dirs & rev_dirs and str(env["repo"]) not in eng_dirs | rev_dirs
    # независимость: ревьюер видит дифф, но не промпт и не рассуждения автора
    assert "total([]) == 0" in rev.calls[0]["prompt"]
    assert "Реализуй утверждённый план" not in rev.calls[0]["prompt"]
    # рабочая копия и main синтетического репозитория не тронуты; пуш — только ae/*
    assert F.git(env["repo"], "rev-parse", "main") == env["sha"]
    assert F.git(env["repo"], "status", "--porcelain") == ""
    pushes = [c for c in pub.log if c[:2] == ["git", "push"]]
    assert pushes == [["git", "push", "origin", f"HEAD:refs/heads/{run['branch']}"]]
    assert any(c[:4] == ["gh", "pr", "create", "--draft"] for c in pub.log)
    states = [t["to"] for t in run["transitions"]]
    assert states == ["RECEIVED", "DISCOVERING", "PLANNING", "IMPLEMENTING", "TESTING", "REVIEWING",
                      "READY_FOR_PR", "COMPLETED"]


# ======================================================================= C ===
def test_C_candidate_violating_mandatory_test_is_refused(env):
    objective = dispatch_synthetic(env)
    eng = ScriptedAdapter({"engineer_plan": [{"respond": F.plan()}],
                           "engineer_implement": [{"edit": F.edit_still_wrong, "respond": F.implemented()}]})
    rev = ScriptedAdapter({"reviewer": [{"respond": F.verdict("PASS")}]})
    orch, pub = orchestrator(env, eng, rev)
    run = orch.advance(orch.submit(objective)[0]["run_id"])
    assert run["state"] == "BLOCKED"
    assert run["iteration"] == BUDGET["max_engineer_iterations"]
    assert rev.calls == []                    # красный кандидат ревьюеру даже не показывают
    assert not any(c[:2] == ["git", "push"] for c in pub.log)
    gate = json.loads((env["store"].root / "artifacts" / run["run_id"] / "gate.json").read_text())
    assert gate["verdict"] == "BLOCKED_BY_TEST"


def test_C_skipped_test_is_unsafe_even_if_reviewer_says_pass(env):
    objective = dispatch_synthetic(env)
    eng = ScriptedAdapter({"engineer_plan": [{"respond": F.plan()}],
                           "engineer_implement": [{"edit": F.edit_skip_test, "respond": F.implemented()}]})
    rev = ScriptedAdapter({"reviewer": [{"respond": F.verdict("PASS")}]})
    orch, pub = orchestrator(env, eng, rev)
    run = orch.advance(orch.submit(objective)[0]["run_id"])
    assert run["state"] == "BLOCKED" and run["last_gate"]["verdict"] == "UNSAFE"
    assert rev.calls == [] and not any(c[:2] == ["git", "push"] for c in pub.log)


# ======================================================================= D ===
def test_D_reviewer_rejection_returns_work_to_engineer(env):
    objective = dispatch_synthetic(env)
    eng = ScriptedAdapter({"engineer_plan": [{"respond": F.plan()}],
                           "engineer_implement": [{"edit": F.edit_fix, "respond": F.implemented()},
                                                  {"edit": F.edit_fix_with_extra_test, "respond": F.implemented()}]})
    rev = ScriptedAdapter({"reviewer": [{"respond": F.verdict("CHANGES_REQUIRED", [F.finding()])},
                                        {"respond": F.verdict("PASS")}]})
    orch, _ = orchestrator(env, eng, rev)
    run = orch.advance(orch.submit(objective)[0]["run_id"])
    assert run["state"] == "COMPLETED"
    assert run["iteration"] == 2 and run["review_cycles"] == 2
    fix_prompt = [c for c in eng.calls if c["role"] == "engineer_implement"][1]["prompt"]
    assert "нет теста на пустой список" in fix_prompt          # находка дошла до инженера
    assert "FIXING" in [t["to"] for t in run["transitions"]]


def test_D_iteration_budget_is_finite(env):
    objective = dispatch_synthetic(env)
    eng = ScriptedAdapter({"engineer_plan": [{"respond": F.plan()}],
                           "engineer_implement": [{"edit": F.edit_fix, "respond": F.implemented()}]})
    rev = ScriptedAdapter({"reviewer": [{"respond": F.verdict("CHANGES_REQUIRED", [F.finding()])}]})
    orch, pub = orchestrator(env, eng, rev)
    run = orch.advance(orch.submit(objective)[0]["run_id"])
    assert run["state"] == "BLOCKED"
    assert run["iteration"] <= BUDGET["max_engineer_iterations"]
    assert run["review_cycles"] <= BUDGET["max_review_cycles"]
    assert len([c for c in eng.calls if c["role"] == "engineer_implement"]) <= BUDGET["max_engineer_iterations"]
    assert not any(c[:2] == ["git", "push"] for c in pub.log)


# ======================================================================= E ===
@pytest.mark.parametrize("editor", [F.edit_workflow, F.edit_infra], ids=["github-workflow", "infra-terraform"])
def test_E_forbidden_paths_are_unsafe_and_never_published(env, editor):
    objective = dispatch_synthetic(env)
    eng = ScriptedAdapter({"engineer_plan": [{"respond": F.plan()}],
                           "engineer_implement": [{"edit": editor, "respond": F.implemented()}]})
    rev = ScriptedAdapter({"reviewer": [{"respond": F.verdict("PASS")}]})
    orch, pub = orchestrator(env, eng, rev)
    run = orch.advance(orch.submit(objective)[0]["run_id"])
    assert run["state"] == "BLOCKED" and run["last_gate"]["verdict"] == "UNSAFE"
    assert rev.calls == [] and pub.log == []


def test_E_detected_production_mutation_stops_the_run(env):
    objective = dispatch_synthetic(env)
    eng = ScriptedAdapter({"engineer_plan": [{"respond": F.plan()}],
                           "engineer_implement": [{"edit": F.edit_fix, "respond": F.implemented()}]})
    rev = ScriptedAdapter({"reviewer": [{"respond": F.verdict("PASS")}]})
    calls = {"n": 0}

    def audit(run):                          # аудит журнала заданий «увидел» DDL после работы инженера
        calls["n"] += 1
        return 1 if calls["n"] == 2 else 0

    orch, pub = orchestrator(env, eng, rev, audit=audit)
    run = orch.advance(orch.submit(objective)[0]["run_id"])
    assert run["state"] == "BLOCKED" and run["production_mutations"] == 1
    assert run["last_gate"]["verdict"] == "UNSAFE" and pub.log == []


# ======================================================================= F ===
def test_F_same_incident_twice_is_one_remediation(env):
    objective = dispatch_synthetic(env)
    eng = ScriptedAdapter({"engineer_plan": [{"respond": F.plan(status="NEEDS_HUMAN")}]})
    rev = ScriptedAdapter({"reviewer": [{"respond": F.verdict("PASS")}]})
    orch, _ = orchestrator(env, eng, rev)
    run1, created1 = orch.submit(objective)
    run1 = orch.advance(run1["run_id"])
    assert run1["state"] == "WAITING_FOR_HUMAN"
    run2, created2 = orch.submit(objective)
    assert created1 and not created2 and run2["run_id"] == run1["run_id"]
    again = watch(F.observations("FAIL"), env["store"], env["sha"], env["out"], synthetic=True)
    assert again["dispatch"] == [] and again["results"][0]["dispatch"].startswith("already_active:")
    assert len(list((env["store"].root / "runs").glob("*.json"))) == 1


class Crash(RuntimeError):
    pass


class CrashingAdapter(ScriptedAdapter):
    def run(self, role, prompt, workdir, schema):
        if role == "engineer_implement":
            raise Crash("процесс убит посреди реализации")
        return super().run(role, prompt, workdir, schema)


def test_F_restart_recovers_from_durable_state_without_memory(env):
    objective = dispatch_synthetic(env)
    crashing = CrashingAdapter({"engineer_plan": [{"respond": F.plan()}]})
    orch1, _ = orchestrator(env, crashing, ScriptedAdapter({"reviewer": [{"respond": F.verdict()}]}))
    run_id = orch1.submit(objective)[0]["run_id"]
    with pytest.raises(Crash):
        orch1.advance(run_id)
    assert env["store"].load(run_id)["state"] == "IMPLEMENTING"
    # новый процесс: другие объекты, никакой общей памяти — только каталог состояния
    eng = ScriptedAdapter({"engineer_implement": [{"edit": F.edit_fix, "respond": F.implemented()}]})
    rev = ScriptedAdapter({"reviewer": [{"respond": F.verdict("PASS")}]})
    orch2, _ = orchestrator(env, eng, rev)
    run = orch2.advance(run_id)
    assert run["state"] == "COMPLETED"
    assert [c["role"] for c in eng.calls] == ["engineer_implement"]   # план заново не строился


def test_F_failed_run_is_not_retried_automatically(env):
    objective = dispatch_synthetic(env)
    eng = ScriptedAdapter({"engineer_plan": [{"error": "overloaded", "transient": True}]})
    orch, _ = orchestrator(env, eng, ScriptedAdapter({"reviewer": [{"respond": F.verdict()}]}))
    run = orch.advance(orch.submit(objective)[0]["run_id"])
    assert run["state"] == "FAILED" and run["infra_retries"] == BUDGET["max_infra_retries"]
    assert len(eng.calls) == BUDGET["max_infra_retries"] + 1
    with pytest.raises(TransitionError, match="автоматический повтор запрещён"):
        orch.submit(objective)
