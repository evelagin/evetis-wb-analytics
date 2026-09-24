"""S1 — привилегированная идентичность GCP недоступна job'ам AE (AE_V1_SECURITY.md §2).

Проверка идёт на ТОЧНЫХ claims OIDC-токена GitHub через ту же модель решения GCP, которой
пользуется `tools/autonomy/wif_check.py --live` после применения. Случаи:
  A — легитимный привилегированный workflow принят;
  B — job инженера (и тестов кандидата) получает только read-only SA;
  C — job ревьюера не получает ничего;
  D — workflow на ветке кандидата не получает ничего;
  E — попытка выдать себя за привилегированный workflow отклонена.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.autonomy import wif_check
from tools.autonomy.wif_check import CelError, WifConfig, cases, cel, claims, load_terraform

REPO = Path(__file__).resolve().parents[2]
SNAPSHOT = REPO / "quality" / "autonomy" / "wif_live_snapshot_2026-09-24.json"
DESIRED = load_terraform()
CASES = {c.id: c for c in cases()}


@pytest.mark.parametrize("case_id", sorted(CASES))
def test_desired_terraform_matches_every_case(case_id):
    c = CASES[case_id]
    assert DESIRED.obtainable(c.claims) == set(c.expect), c.title


def test_case_families_complete():
    families = {cid[0] for cid in CASES}
    assert families == {"A", "B", "C", "D", "E"}


@pytest.mark.parametrize("case_id", [c for c in sorted(CASES) if c[0] in "BCDE"])
def test_no_ae_or_candidate_claim_reaches_privileged_sa(case_id):
    got = DESIRED.obtainable(CASES[case_id].claims)
    assert not (got & wif_check.PRIVILEGED), f"{case_id}: утечка {got & wif_check.PRIVILEGED}"


def test_reviewer_has_no_gcp_identity_at_all():
    assert DESIRED.obtainable(CASES["C1"].claims) == set()


def test_every_ae_job_file_is_distinguished_by_job_workflow_ref():
    """Инженер и ревьюер — один workflow_ref (autonomy-run.yml), разные job_workflow_ref.
    Если бы привязка шла по workflow_ref, ревьюер получил бы тот же SA, что инженер."""
    eng, rev = CASES["B1"].claims, CASES["C1"].claims
    assert eng["workflow_ref"] == rev["workflow_ref"]
    assert eng["job_workflow_ref"] != rev["job_workflow_ref"]
    assert DESIRED.obtainable(eng) != DESIRED.obtainable(rev)


def test_bindings_are_pinned_to_file_and_ref_never_to_repo_or_branch_alone():
    for sa, members in DESIRED.bindings.items():
        for attr, value in members:
            assert attr not in ("attribute.repository", "attribute.ref", "attribute.repo_ref", "google.subject"), (sa, attr)
            if sa in ("sa-deployer", "sa-terraform-apply", "sa-ae-reader"):
                assert value.endswith("@refs/heads/main"), (sa, value)
                assert "/.github/workflows/" in value, (sa, value)


def test_binding_inventory_is_exact():
    by_sa = {sa: sorted(v for _, v in m) for sa, m in DESIRED.bindings.items()}
    wf = "evelagin/evetis-wb-analytics/.github/workflows/"
    assert by_sa == {
        "sa-deployer": [wf + "deploy-prod.yml@refs/heads/main", wf + "deploy-shadow.yml@refs/heads/main"],
        "sa-terraform-apply": [wf + "infra.yml@refs/heads/main", wf + "scheduler-control.yml@refs/heads/main"],
        "sa-terraform-plan": ["infra.yml"],
        "sa-ae-reader": sorted(wf + f + "@refs/heads/main" for f in
                               ("autonomy-watch.yml", "autonomy-gate.yml", "autonomy-engineer.yml", "autonomy-test.yml")),
    }


# ---------------------------------------------------------- живой провайдер до исправления ---
def test_live_snapshot_2026_09_24_violates_s1():
    """Исторический факт, на котором держится требование «AE выключен до применения»:
    на 2026-09-24 job инженера и даже ревьюера получают deployer и terraform-apply."""
    live = wif_check.from_snapshot(json.loads(SNAPSHOT.read_text(encoding="utf-8")), "snapshot")
    for cid in ("B1", "B2", "C1", "A7"):
        got = live.obtainable(CASES[cid].claims)
        assert {"sa-deployer", "sa-terraform-apply", "sa-terraform-plan"} <= got, (cid, got)
    assert {"sa-deployer", "sa-terraform-plan"} <= live.obtainable(CASES["D1"].claims)
    assert not all(r["status"] == "PASS" for r in wif_check.verify(live))


# ------------------------------------------------------------- почему не extract('{path}@') ---
def test_extract_takes_text_before_first_at():
    ref = "evelagin/evetis-wb-analytics/.github/workflows/deploy-prod.yml@x.yml@refs/heads/x"
    assert cel("assertion.workflow_ref.extract('{path}@')", {"assertion": {"workflow_ref": ref}}) == \
        "evelagin/evetis-wb-analytics/.github/workflows/deploy-prod.yml"


def test_previous_path_only_design_would_leak_deployer():
    """Первая версия исправления (PR #165 до 2026-09-24) привязывала deployer к ПУТИ файла на
    любой ветке. Она пропускала и ветку кандидата (D1), и двойника «deploy-prod.yml@x.yml» (E1)."""
    old = WifConfig(
        "path-only", DESIRED.condition,
        {"google.subject": "assertion.sub", "attribute.workflow_path": "assertion.workflow_ref.extract('{path}@')"},
        {"sa-deployer": [("attribute.workflow_path", "evelagin/evetis-wb-analytics/.github/workflows/deploy-prod.yml")]})
    assert "sa-deployer" in old.obtainable(CASES["D1"].claims)
    assert "sa-deployer" in old.obtainable(CASES["E1"].claims)
    assert "sa-deployer" not in DESIRED.obtainable(CASES["E1"].claims)


# ---------------------------------------------------------------------------- CEL-модель ---
def test_cel_subset_semantics():
    env = {"assertion": {"a": "x", "b": "y"}}
    assert cel("assertion.a + '/' + assertion.b", env) == "x/y"
    assert cel("assertion.a == 'x' && !assertion.b.startsWith('z') ? 'ok' : 'no'", env) == "ok"
    assert cel("(assertion.a == 'q' || assertion.b == 'y') ? 'ok' : 'no'", env) == "ok"


def test_cel_unknown_construct_fails_closed():
    with pytest.raises(CelError):
        cel("assertion.a.matches('x')", {"assertion": {"a": "x"}})
    with pytest.raises(CelError):
        cel("assertion.missing", {"assertion": {}})


def test_token_without_claim_is_rejected_not_matched():
    c = claims("deploy-prod.yml", "refs/heads/main")
    del c["workflow_ref"]
    assert DESIRED.obtainable(c) == set()


def test_cli_exit_code_on_desired_state():
    assert wif_check.main([]) == 0
    assert wif_check.main(["--snapshot", str(SNAPSHOT)]) == 1
