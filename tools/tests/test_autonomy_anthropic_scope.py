"""Сторож scope Claude API: исключение workspace:developer явное, срочное и не расширяемое.

Живые правила (2026-09-25) созданы с workspace:developer, потому что Console не предлагает
workspace:inference. Желаемое состояние не меняется; фактический scope проверяется по ответу
обмена токена до запуска агента, а шире желаемого он допустим только по исключению с датой."""
from __future__ import annotations

import copy
import re
from datetime import date
from pathlib import Path

import pytest

from tools.autonomy import anthropic_scope as S
from tools.autonomy.policy import load_policy

REPO = Path(__file__).resolve().parents[2]
RT = load_policy()["anthropic_runtime"]
TODAY = date(2026, 9, 26)


def test_desired_scope_stays_inference_in_both_places():
    import json
    fed = json.loads((REPO / "quality/autonomy/anthropic_federation.json").read_text())
    assert RT["desired_scope"] == "workspace:inference"
    assert {r["oauth_scope"] for r in fed["rules"].values()} == {"workspace:inference"}


@pytest.mark.parametrize("role", ["engineer", "reviewer"])
def test_inference_passes(role):
    assert S.decide("workspace:inference", 600, role, RT, TODAY)["decision"] == "PASS"


@pytest.mark.parametrize("role", ["engineer", "reviewer"])
def test_developer_passes_only_as_visible_exception(role):
    r = S.decide("workspace:developer", 600, role, RT, TODAY)
    assert r["decision"] == "PASS_WITH_EXCEPTION" and "исключение" in r["reason"]


def test_exception_expires():
    assert S.decide("workspace:developer", 600, "engineer", RT, date(2026, 10, 10))["decision"] == "FAIL"


@pytest.mark.parametrize("scope", ["org:admin", "", None, "admin", "workspace:manage_tunnels"])
def test_admin_or_unknown_scope_always_fails(scope):
    assert S.decide(scope, 600, "engineer", RT, TODAY)["decision"] == "FAIL"


@pytest.mark.parametrize("ttl", [601, 3600, 86400, 0, None, "600"])
def test_token_lifetime_bound(ttl):
    assert S.decide("workspace:inference", ttl, "reviewer", RT, TODAY)["decision"] == "FAIL"


def test_exception_does_not_cover_unlisted_role():
    rt = copy.deepcopy(RT)
    rt["scope_exceptions"][0]["applies_to"] = ["engineer"]
    assert S.decide("workspace:developer", 600, "reviewer", rt, TODAY)["decision"] == "FAIL"


@pytest.mark.parametrize("mut", [
    lambda ex: ex.__setitem__("scope", "org:admin"),
    lambda ex: ex.__setitem__("expires", "2027-01-01"),          # длиннее max_exception_days
    lambda ex: ex.__setitem__("applies_to", ["engineer", "test"]),
])
def test_policy_cannot_grant_admin_or_open_ended_exception(mut):
    rt = copy.deepcopy(RT)
    mut(rt["scope_exceptions"][0])
    with pytest.raises(S.PolicyError):
        S.validate_runtime_policy(rt)


def test_live_policy_is_valid_and_exception_is_time_boxed():
    S.validate_runtime_policy(RT)
    ex = RT["scope_exceptions"]
    assert len(ex) == 1 and ex[0]["scope"] == "workspace:developer" and ex[0]["expires"] == "2026-10-09"


@pytest.mark.parametrize("wf,role", [("autonomy-engineer.yml", "engineer"), ("autonomy-review.yml", "reviewer")])
def test_guard_runs_before_agent_token_and_agent(wf, role):
    src = (REPO / ".github/workflows" / wf).read_text()
    guard = src.index(f"python -m tools.autonomy.anthropic_scope check --role {role}")
    assert guard < src.index("refresh_anthropic_oidc.sh") < src.index("--" + ("engineer" if role == "engineer" else "reviewer") + " claude")
    assert guard > src.index("${ANTHROPIC_API_KEY:-}")


def test_test_job_has_no_anthropic_identity():
    src = (REPO / ".github/workflows/autonomy-test.yml").read_text()
    assert "ANTHROPIC_" not in src and "anthropic_scope" not in src


def test_guard_never_emits_the_claude_token():
    src = (REPO / "tools/autonomy/anthropic_scope.py").read_text()
    assert "access_token" not in src
    assert re.search(r"resp\.clear\(\)", src)
    assert "ANTHROPIC_IDENTITY_TOKEN_FILE" not in src.split('"""', 2)[2]   # свой OIDC-токен, не файл агента
