"""S9 (подготовка) — синтетическая цель ввода в эксплуатацию и детерминированный вердикт по ней.

Живой прогон требует федерации Anthropic и применённого исправления WIF (решения владельца).
Здесь доказано то, что можно доказать до него: цель валидна и НЕ запускаема (execute=false,
нулевой SHA), мишень вне TCB, протокол итерации виден только ревьюеру, вердикт S9 не
выдаётся без каждого из фактов."""
from __future__ import annotations

import copy
import json
import math
import subprocess
import sys

import pytest

from tools.autonomy import commissioning as C
from tools.autonomy.agents import ScriptedAdapter
from tools.autonomy.policy import tcb_paths
from tools.autonomy.schema import require_valid
from tools.autonomy.state import TransitionError
from tools.commissioning.ae_canary import clamp_percent
from tools.tests import ae_fixtures as F
from tools.tests.test_autonomy_acceptance import dispatch_synthetic, env, orchestrator  # noqa: F401

OBJ = json.loads((F.REPO / "quality/autonomy/examples/objective.commissioning.json").read_text())
MARKER = OBJ["commissioning"]["iteration_marker"]


def test_objective_is_valid_and_not_executable():
    require_valid(OBJ, "objective")
    assert OBJ["execute"] is False and OBJ["repository_sha"] == "0" * 40
    assert OBJ["known_ubr_links"] == [] and "UBR" not in OBJ["deduplication_key"]


def test_target_and_expected_change_are_outside_tcb():
    assert tcb_paths(["tools/commissioning/ae_canary.py", *C.ALLOWED_FILES]) == []


def test_target_function_contract():
    assert clamp_percent(-5) == 0 and clamp_percent(150) == 100 and clamp_percent(42.5) == 42.5
    with pytest.raises(ValueError):
        clamp_percent(math.nan)


def test_submit_refuses_non_executable_objective(env):
    orch, _ = orchestrator(env, ScriptedAdapter({}), ScriptedAdapter({}))
    with pytest.raises(TransitionError):
        orch.submit({**OBJ, "repository_sha": env["sha"]})


def test_protocol_reaches_reviewer_but_not_engineer(env):
    objective = {**dispatch_synthetic(env), "commissioning": {"iteration_marker": MARKER}}
    eng = ScriptedAdapter({"engineer_plan": [{"respond": F.plan()}],
                           "engineer_implement": [{"edit": F.edit_fix, "respond": F.implemented()},
                                                  {"edit": F.edit_fix_with_extra_test, "respond": F.implemented()}]})
    rev = ScriptedAdapter({"reviewer": [{"respond": F.verdict("CHANGES_REQUIRED", [F.finding("MAJOR")])},
                                        {"respond": F.verdict("PASS")}]})
    orch, _ = orchestrator(env, eng, rev)
    run = orch.advance(orch.submit(objective)[0]["run_id"])
    assert run["state"] == "READY_FOR_HUMAN_REVIEW" and run["review_cycles"] == 2
    assert all(MARKER not in c["prompt"] for c in eng.calls)
    assert all(MARKER in c["prompt"] for c in rev.calls)


# ------------------------------------------------------------------------ вердикт S9 ---
PATCH = f'diff --git a/tools/tests/test_ae_commissioning_canary.py b/tools/tests/test_ae_commissioning_canary.py\n+"""{MARKER}"""\n'
GOOD_RUN = {"state": "READY_FOR_HUMAN_REVIEW", "production_mutations": 0, "audit_status": "PASS",
            "created_at": "2026-09-27T12:12:35Z",
            "usage": [{"role": "engineer_implement"}, {"role": "engineer_implement"}],
            # доверенный аудит всего окна, покрывающий оба вызова агента (tools/autonomy/audit.py)
            "audit_evidence": {"status": "PASS", "mutations": 0, "source": "jobs_list+audit_logs+iam_selftest+iam_history+ingress_attribution/v5", "audited_at": "2026-09-27T12:54:00Z",
                               "since": "2026-09-27T12:12:35Z", "usage_count": 2},
            "verification": {"result": {"status": "PASS"}},
            "transitions": [{"from": a, "to": b} for a, b in [
                (None, "RECEIVED"), ("RECEIVED", "DISCOVERING"), ("DISCOVERING", "PLANNING"),
                ("PLANNING", "IMPLEMENTING"), ("IMPLEMENTING", "TESTING"), ("TESTING", "REVIEWING"),
                ("REVIEWING", "FIXING"), ("FIXING", "TESTING"), ("TESTING", "REVIEWING"),
                ("REVIEWING", "READY_FOR_PR"), ("READY_FOR_PR", "AWAITING_VERIFICATION"),
                ("AWAITING_VERIFICATION", "READY_FOR_HUMAN_REVIEW")]]}
GATE = {"verdict": "READY_FOR_PR", "decided_by": "gatekeeper (deterministic)"}
JOBS = [{"name": "engineer / engineer", "conclusion": "success", "runner_name": "GitHub Actions 1"},
        {"name": "engineer / engineer", "conclusion": "success", "runner_name": "GitHub Actions 5"},
        {"name": "review / review", "conclusion": "success", "runner_name": "GitHub Actions 3"},
        {"name": "review / review", "conclusion": "success", "runner_name": "GitHub Actions 7"}]


def test_all_facts_present_is_pass():
    assert C.assess(GOOD_RUN, GATE, PATCH, JOBS, MARKER)["status"] == "PASS"


@pytest.mark.parametrize("breaker", [
    lambda r, g, p, j: (r | {"production_mutations": 1}, g, p, j),
    lambda r, g, p, j: (r | {"audit_status": "BLOCKED"}, g, p, j),
    lambda r, g, p, j: (r | {"state": "AWAITING_VERIFICATION"}, g, p, j),
    lambda r, g, p, j: (r | {"verification": {"result": {"status": "PENDING"}}}, g, p, j),
    lambda r, g, p, j: (r | {"transitions": [t for t in r["transitions"] if t["to"] != "FIXING"]}, g, p, j),
    lambda r, g, p, j: (r, g | {"decided_by": "reviewer"}, p, j),
    lambda r, g, p, j: (r, g, p.replace(MARKER, "x"), j),
    lambda r, g, p, j: (r, g, p + "diff --git a/tools/commissioning/ae_canary.py b/tools/commissioning/ae_canary.py\n", j),
    lambda r, g, p, j: (r, g, p, j[:1] + j[2:]),                                # один проход инженера
    lambda r, g, p, j: (r, g, p, [x | {"runner_name": "same"} for x in j]),     # тот же раннер
], ids=["mutation", "audit-blocked", "not-verified", "ci-pending", "no-iteration", "non-deterministic-gate",
        "no-marker", "out-of-scope", "single-engineer-pass", "shared-runner"])
def test_any_missing_fact_fails(breaker):
    args = breaker(copy.deepcopy(GOOD_RUN), dict(GATE), PATCH, copy.deepcopy(JOBS))
    assert C.assess(*args, MARKER)["status"] == "FAIL"
