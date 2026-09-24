"""S7 (конфигурация) — идентичность Claude API без статического ключа, раздельно для ролей.

Проверяется желаемая конфигурация федерации Anthropic (quality/autonomy/anthropic_federation.json)
по семантике документации (2026-09-24): все заданные условия правила обязаны выполниться,
subject_prefix — точное совпадение (префикс только с завершающим *), claims — точные значения.
Клиент выбирает правило ПО ID, поэтому чужой job, указавший ID чужого правила, всё равно
упирается в его условия. Живое срабатывание (обмен токена) — часть ввода в эксплуатацию (S9).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tools.autonomy.wif_check import MAIN, claims

REPO = Path(__file__).resolve().parents[2]
CFG = json.loads((REPO / "quality" / "autonomy" / "anthropic_federation.json").read_text(encoding="utf-8"))
RULES = CFG["rules"]
AUD = "https://api.anthropic.com"


def _tok(**kw):
    c = claims(**kw)
    c["aud"] = AUD
    return c


def rule_matches(rule: dict, jwt: dict) -> bool:
    m = rule["match"]
    assert set(m) <= {"subject_prefix", "audience", "claims", "condition"}
    assert "condition" not in m, "CEL-условие проверкой не моделируется — держать правила на точных claims"
    ok = True
    sp = m.get("subject_prefix")
    if sp is not None:
        ok &= jwt.get("sub", "").startswith(sp[:-1]) if sp.endswith("*") else jwt.get("sub") == sp
    if "audience" in m:
        ok &= jwt.get("aud") == m["audience"]
    for k, v in m.get("claims", {}).items():
        ok &= jwt.get(k) == v
    return bool(ok)


def matching(jwt: dict) -> set[str]:
    return {role for role, rule in RULES.items() if rule_matches(rule, jwt)}


ENGINEER = _tok(workflow="autonomy-run.yml", ref=MAIN, job_workflow="autonomy-engineer.yml")
REVIEWER = _tok(workflow="autonomy-run.yml", ref=MAIN, job_workflow="autonomy-review.yml")
CASES = {
    "engineer_job": (ENGINEER, {"engineer"}),
    "reviewer_job": (REVIEWER, {"reviewer"}),
    "test_job_runs_candidate_code": (_tok(workflow="autonomy-run.yml", ref=MAIN, job_workflow="autonomy-test.yml"), set()),
    "gate_job": (_tok(workflow="autonomy-run.yml", ref=MAIN, job_workflow="autonomy-gate.yml"), set()),
    "publish_job": (_tok(workflow="autonomy-run.yml", ref=MAIN), set()),
    "watch": (_tok(workflow="autonomy-watch.yml", ref=MAIN, event="schedule"), set()),
    "candidate_branch_engineer": (_tok(workflow="autonomy-run.yml", ref="refs/heads/ae/x-1234abcd",
                                       job_workflow="autonomy-engineer.yml"), set()),
    "pull_request": (_tok(workflow="autonomy-run.yml", ref="refs/pull/7/merge", event="pull_request",
                          job_workflow="autonomy-engineer.yml"), set()),
    "other_repo": (_tok(workflow="autonomy-run.yml", ref=MAIN, job_workflow="autonomy-engineer.yml",
                        repo="evelagin/evetis-wb-analytics-fork"), set()),
    "deploy_workflow": (_tok(workflow="deploy-prod.yml", ref=MAIN, environment="production"), set()),
    "reusable_from_other_caller": (_tok(workflow="ci.yml", ref=MAIN, job_workflow="autonomy-engineer.yml"), set()),
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_federation_rule_matrix(case):
    jwt, expect = CASES[case]
    assert matching(jwt) == expect


def test_engineer_cannot_obtain_reviewer_identity_even_naming_its_rule():
    assert not rule_matches(RULES["reviewer"], ENGINEER)
    assert not rule_matches(RULES["engineer"], REVIEWER)


def test_wrong_audience_is_rejected():
    assert matching({**ENGINEER, "aud": "https://iam.googleapis.com/x"}) == set()


def test_roles_have_distinct_service_accounts():
    targets = [r["target_service_account"] for r in RULES.values()]
    assert len(set(targets)) == len(targets) == 2
    assert set(CFG["service_accounts"]) == set(targets)


def test_token_lifetime_and_scope_are_bounded():
    for role, r in RULES.items():
        assert 60 <= r["token_lifetime_seconds"] <= 600, role       # не дольше 10 мин (и ≤ 2× жизни JWT GitHub)
        assert r["oauth_scope"] == "workspace:developer", role       # никаких org:admin
    assert CFG["issuer"]["check_jti"] is True                         # повторный обмен одного JWT отвергается


def test_rules_are_pinned_to_file_ref_and_hosted_runners():
    for role, r in RULES.items():
        c = r["match"]["claims"]
        assert c["ref"] == MAIN and c["job_workflow_ref"].endswith("@refs/heads/main")
        assert c["runner_environment"] == "github-hosted"
        assert not r["match"]["subject_prefix"].endswith("*")


def test_workflows_request_exactly_these_identities_and_no_static_key():
    wf = REPO / ".github" / "workflows"
    eng, rev = (wf / "autonomy-engineer.yml").read_text(), (wf / "autonomy-review.yml").read_text()
    assert "vars.AE_ANTHROPIC_FEDERATION_RULE_ID" in eng and "REVIEWER" not in eng.split("steps:")[0]
    assert "ANTHROPIC_FEDERATION_RULE_ID: ${{ vars.AE_ANTHROPIC_REVIEWER_FEDERATION_RULE_ID }}" in rev
    for p in wf.glob("*.yml"):
        src = p.read_text()
        assert "secrets.ANTHROPIC" not in src and not re.search(r"^\s*ANTHROPIC_API_KEY\s*:", src, re.M), p.name
    for name in CFG["github_variables"]:
        assert name in eng or name in rev, name


def test_oidc_token_file_lives_outside_the_workspace():
    src = (REPO / "tools/autonomy/ci/refresh_anthropic_oidc.sh").read_text()
    assert "GITHUB_WORKSPACE" in src and "exit 2" in src and "chmod 600" in src
