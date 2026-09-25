"""S1 по всем доменам доверия GCP WIF репозитория: EVETIS и mpa-platform / tenant-infra-pool.

Инвариант арендатора: sa-tenant-provisioner получает ТОЛЬКО tenant-infra.yml@refs/heads/main
(workflow_dispatch, github-hosted, repository_id/owner_id репозитория). Никакие job'ы AE,
кандидаты, двойники и не-main ref его не получают. Неучтённый домен доверия — FAIL."""
from __future__ import annotations

import copy
import json

import pytest

from tools.autonomy import wif_domains as D
from tools.autonomy.wif_check import cases, claims, from_snapshot

INV = D.load_inventory()
DOM = {d["id"]: d for d in INV["domains"]}
TENANT = D.desired_config(DOM["mpa-tenant-infra"])
EVETIS = D.desired_config(DOM["evetis-prod"])
TC = {c.id: c for c in D.tenant_cases()}
BASE = {c.id: c for c in cases()}


@pytest.mark.parametrize("dom", sorted(DOM))
def test_every_approved_domain_satisfies_its_invariant(dom):
    rows = D.verify_domain(D.desired_config(DOM[dom]), DOM[dom])
    bad = [r for r in rows if r["status"] != "PASS"]
    assert not bad, bad


def test_legit_tenant_workflow_obtains_provisioner():
    assert TENANT.obtainable(TC["T1"].claims) == {D.TENANT_SA}


@pytest.mark.parametrize("cid", ["A6", "A7", "B1", "B2", "C1"])
def test_ae_jobs_cannot_obtain_tenant_provisioner(cid):
    assert TENANT.obtainable(BASE[cid].claims) == set()


@pytest.mark.parametrize("cid", ["D1", "D2", "D3", "D4", "D5", "D6"])
def test_candidate_jobs_cannot_obtain_tenant_provisioner(cid):
    assert TENANT.obtainable(BASE[cid].claims) == set()


@pytest.mark.parametrize("cid,why", [("T2", "не-main"), ("T3", "двойник пути"), ("T8", "ветка ae/*"),
                                     ("T4", "push вместо dispatch"), ("T5", "self-hosted"),
                                     ("T6", "другой repository_id"), ("T7", "другой owner_id"),
                                     ("T9", "вызов как переиспользуемый"), ("T10", "расписание")])
def test_tenant_provisioner_denied(cid, why):
    assert TENANT.obtainable(TC[cid].claims) == set(), why


def test_tenant_workflow_gets_nothing_in_evetis_pool():
    assert EVETIS.obtainable(TC["T1"].claims) == set()


def test_tenant_snapshot_pins_immutable_ids_and_workflow():
    cond = json.loads((D.REPO / DOM["mpa-tenant-infra"]["desired"]["path"]).read_text())["provider"]["attributeCondition"]
    for needle in ("repository_id == '1260095567'", "repository_owner_id == '286048501'",
                   "tenant-infra.yml@refs/heads/main", "event_name == 'workflow_dispatch'",
                   "runner_environment == 'github-hosted'"):
        assert needle in cond, needle


# ----------------------------------------------------------------- неучтённые домены ---
def _disc(project, pool, provider, condition, issuer=INV["github_issuer"]):
    trusts, evaluable = D.trust_of(condition)
    return D.Discovered(project, pool, provider, issuer, None, trusts, evaluable)


def _approved():
    return [_disc(d["project"], d["pool"], d["provider"], "assertion.repository == 'evelagin/evetis-wb-analytics'")
            for d in INV["domains"]]


def test_inventory_matching_live_is_clean():
    assert D.check_inventory(_approved(), INV) == []


def test_unknown_trusting_domain_fails_closed():
    extra = _disc("some-project", "rogue-pool", "gh", "assertion.repository == 'evelagin/evetis-wb-analytics'")
    f = D.check_inventory(_approved() + [extra], INV)
    assert any("НЕУЧТЁННЫЙ" in x and "rogue-pool" in x for x in f)


def test_unknown_domain_trusting_all_github_fails_closed():
    extra = _disc("p", "wide-pool", "gh", "")          # пустое условие = любой токен GitHub
    assert any("wide-pool" in x for x in D.check_inventory(_approved() + [extra], INV))


def test_unevaluable_condition_fails_closed():
    extra = _disc("p", "odd-pool", "gh", "assertion.repository.matches('.*')")
    f = D.check_inventory(_approved() + [extra], INV)
    assert any("не вычисляется" in x for x in f) and any("НЕУЧТЁННЫЙ" in x for x in f)


def test_provider_of_other_repo_or_other_issuer_is_not_a_trust_domain():
    other = _disc("p", "other-pool", "gh", "assertion.repository == 'someone/else'")
    aws = _disc("p", "aws-pool", "aws", "", issuer=None)
    assert D.check_inventory(_approved() + [other, aws], INV) == []


def test_approved_domain_missing_live_is_reported():
    assert any("не найден вживую" in x for x in D.check_inventory(_approved()[:1], INV))


def test_widened_tenant_binding_would_be_caught():
    snap = json.loads((D.REPO / DOM["mpa-tenant-infra"]["desired"]["path"]).read_text())
    wide = copy.deepcopy(snap)
    wide["provider"]["attributeCondition"] = "assertion.repository == 'evelagin/evetis-wb-analytics'"
    wide["service_account_bindings"]["sa-tenant-provisioner"][0]["members"] = [
        "principalSet://iam.googleapis.com/projects/777428383056/locations/global/workloadIdentityPools/"
        "tenant-infra-pool/attribute.repository_id/1260095567"]
    wide["provider"]["attributeMapping"]["attribute.repository_id"] = "assertion.repository_id"
    rows = D.verify_domain(from_snapshot(wide, "wide"), DOM["mpa-tenant-infra"])
    assert any(r["privileged_leak"] for r in rows if r["case"] in ("B1", "C1"))


def test_cli_desired_state_passes():
    assert D.main([]) == 0
