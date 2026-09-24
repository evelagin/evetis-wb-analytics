"""S2 — изменения доверенной вычислительной базы (TCB) требуют решения человека.

Инвариант: кандидат, затронувший TCB, получает минимум HUMAN_DECISION_REQUIRED и никогда
не публикуется AE — ни PASS ревьюера, ни ACK плана владельцем (owner_ack.plan_sha256) не
делают его самоодобряемым. Секретный материал — UNSAFE.
"""
from __future__ import annotations

import json
import subprocess

import pytest

from tools.autonomy import gatekeeper as G
from tools.autonomy.agents import ScriptedAdapter
from tools.autonomy.evidence import Sandbox
from tools.autonomy.orchestrator import Orchestrator, _sha
from tools.autonomy.policy import forbidden_paths, load_policy, tcb_paths
from tools.autonomy.publisher import GitPublisher, PublishRefused
from tools.autonomy.state import StateStore
from tools.tests import ae_fixtures as F
from tools.tests.test_autonomy_acceptance import dispatch_synthetic, env  # noqa: F401  (фикстура)

# Категории, перечисленные в требованиях фазы, — на РЕАЛЬНЫХ путях репозитория.
MISSION_TCB = {
    "workflows": ".github/workflows/deploy-prod.yml",
    "autonomy_code": "tools/autonomy/orchestrator.py",
    "autonomy_policy": "quality/autonomy/policy.json",
    "autonomy_schema": "quality/autonomy/objective.schema.json",
    "gatekeeper": "tools/autonomy/gatekeeper.py",
    "publisher": "tools/autonomy/publisher.py",
    "evidence_collectors": "tools/autonomy/evidence.py",
    "data_checks": "tools/run_data_checks.py",
    "runtime_access_checks": "tools/check_runtime_access.py",
    "bigquery_readonly_enforcement": "tools/lib/bq_readonly.py",
    "iam_terraform": "infra/terraform/wif.tf",
    "deployment_tooling": "tools/promo_canonical_deploy.py",
    "canonical_deployment_tooling": "tools/promo_canonical_render.py",
    "rollback_tooling_sql": "sql/promotions/pr_promo2_rollback.sql",
    "rollback_tooling_script": "tools/stage2_sku_performance_rollback.sh",
    "rollback_metabase": "metabase/rollback/exec_v2_phase_c2_before/card-map.json",
    "auth_tooling": "tools/autonomy/ci/refresh_anthropic_oidc.sh",
    "secret_env_tooling": "services/wb-communications/deploy/preflight_env.py",
    "runtime_identities": "quality/runtime_identities.json",
    "build_image": "cloud/Dockerfile",
    "dependency_manifest": "pipelines/ozon/runtime/requirements.txt",
    "auto_exec_pytest": "pipelines/ozon/tests/conftest.py",
    "gate_tests": "tools/tests/test_autonomy_wif.py",
    "agent_instructions": "CLAUDE.md",
    "apps_script": "apps-script/Code.gs",
}
REMEDIATION_TARGETS = ["sql/current/ozon_mart/V_OZON_CIS_BUYOUT.sql", "tools/tests/test_new_regression.py",
                       "docs/ops/NOTE.md", "cloud/src/loaders/x.ts", "pipelines/ozon/src/loader.py",
                       "sql/ozon/fix_2026-09-24/V_X.sql"]


@pytest.mark.parametrize("category", sorted(MISSION_TCB))
def test_every_required_category_is_tcb(category):
    path = MISSION_TCB[category]
    assert tcb_paths([path]) == [path], f"{category}: {path} вне TCB"


def test_real_repo_paths_exist_for_the_categories():
    missing = [p for p in MISSION_TCB.values() if not (F.REPO / p).exists() and not p.startswith("apps-script/")]
    assert missing == [], "категория проверяется на несуществующем пути — тест бы ничего не доказывал"


@pytest.mark.parametrize("path", REMEDIATION_TARGETS)
def test_legitimate_remediation_targets_are_not_tcb(path):
    assert tcb_paths([path]) == [] and forbidden_paths([path]) == []


def test_all_tracked_workflows_autonomy_and_infra_files_are_tcb():
    files = subprocess.run(["git", "ls-files", ".github", "tools/autonomy", "quality", "infra"], cwd=F.REPO,
                           capture_output=True, text=True, check=True).stdout.split()
    assert files and tcb_paths(files) == sorted(files)


def test_secret_material_is_forbidden_not_merely_tcb():
    for p in ("synthetic/.env", "infra/terraform/terraform.tfvars", "keys/sa.pem", "x/service-account.json"):
        assert forbidden_paths([p]) == [p]


# ---------------------------------------------------------------------- гейткипер ---
GOOD = {"tests": [{"name": "t", "status": "PASS"}], "sql_validation": {"status": "PASS"},
        "runtime_access": {"status": "NOT_APPLICABLE"}, "parity": {"status": "NOT_APPLICABLE"},
        "data_suites": {}, "objective_resolution": "NOT_APPLICABLE"}


def test_gatekeeper_tcb_is_human_decision_even_with_reviewer_pass():
    r = G.evaluate(GOOD, GOOD, F.verdict("PASS"), {"tcb_paths": ["tools/autonomy/gatekeeper.py"]})
    assert r["verdict"] == "HUMAN_DECISION_REQUIRED"
    assert any("TCB_MODIFICATION" in x for x in r["reasons"]["HUMAN_DECISION_REQUIRED"])


def test_gatekeeper_tcb_never_ready_whatever_else_holds():
    for ctx in ({"tcb_paths": [".github/x"]}, {"tcb_paths": [".github/x"], "objective_resolution": "RESOLVED"}):
        assert G.evaluate(GOOD, GOOD, F.verdict("PASS"), ctx)["verdict"] != "READY_FOR_PR"


# ------------------------------------------------------------------- оркестратор ---
def _orch(env, eng, rev):
    pub = GitPublisher(env["repo"], dry_run=True)
    return Orchestrator(env["store"], env["repo"], eng, rev, F.SyntheticEvidenceRunner(), env["sandboxes"],
                        audit=lambda run: 0, publisher=pub), pub


def test_owner_ack_of_the_plan_does_not_make_tcb_self_approvable(env):
    objective = dispatch_synthetic(env)
    plan = F.plan(files=("synthetic/calc.py", ".github/workflows/x.yml"))
    objective = {**objective, "owner_ack": {"plan_sha256": _sha(plan), "acked_by": "owner",
                                            "acked_at": "2026-09-24T00:00:00Z"}}
    eng = ScriptedAdapter({"engineer_plan": [{"respond": plan}],
                           "engineer_implement": [{"edit": F.edit_workflow, "respond": F.implemented()}]})
    rev = ScriptedAdapter({"reviewer": [{"respond": F.verdict("PASS")}]})
    orch, pub = _orch(env, eng, rev)
    run = orch.advance(orch.submit(objective)[0]["run_id"])
    assert run["state"] == "WAITING_FOR_HUMAN" and "TCB_MODIFICATION" in run["last_gate"]["reason"]
    assert [c["role"] for c in eng.calls] == ["engineer_plan"]      # до реализации не дошло
    assert rev.calls == [] and pub.log == []


def test_publisher_refuses_tcb_even_with_ready_gate(tmp_path):
    art = tmp_path / "art"; art.mkdir()
    (art / "gate.json").write_text(json.dumps({"verdict": "READY_FOR_PR"}))
    run = {"branch": "ae/x-12345678", "run_id": "r", "objective_id": "o", "repository_sha": "a" * 40,
           "production_mutations": 0}
    pub = GitPublisher(tmp_path, dry_run=True)
    for path in ("tools/autonomy/gatekeeper.py", "conftest.py", "tools/lib/bq_readonly.py"):
        with pytest.raises(PublishRefused, match="TCB"):
            pub.preflight(run, f"diff --git a/{path} b/{path}\n", art)


def test_rename_out_of_tcb_is_still_a_tcb_change(tmp_path):
    repo, sha = F.make_repo(tmp_path)
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows" / "x.yml").write_text("on: push\n" * 20)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "wf"], cwd=repo, check=True)
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    sb = Sandbox(repo, base, tmp_path / "sb")
    ws = sb.create()
    (ws / "docs").mkdir()
    subprocess.run(["git", "mv", ".github/workflows/x.yml", "docs/x.yml"], cwd=ws, check=True)
    files = sb.changed_files()
    assert ".github/workflows/x.yml" in files
    assert tcb_paths(files) == [".github/workflows/x.yml"]
    assert "rename from" not in sb.diff()
    sb.cleanup()


def test_policy_has_no_empty_tcb_class():
    classes = load_policy()["trusted_computing_base"]["classes"]
    assert classes and all(classes.values())
