"""РНП владельца — узкое исключение из T6: READER на analytics_share ровно sa-rnp-web.

Доказывает:
  * допускается только sa-rnp-web@<проект арендатора> и только для client_001; правило T6 для клиентов не ослаблено;
  * ни одной клиентской идентичности и ни одной роли проекта блок не создаёт;
  * включение меняет план арендатора ровно в 5 датасетах (in-place ACL) и больше нигде;
  * сканер плана принимает ровно эту форму и отвергает любое расширение.
Офлайн, без облака.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from tools.tenancy import client_access as CA  # noqa: E402
from tools.tenancy import plan_scan as PS  # noqa: E402
from tools.tenancy import registry as R  # noqa: E402
from tools.tenancy import rnp_access as RA  # noqa: E402
from tools.tenancy import synthetic as SY  # noqa: E402
from tools.tenancy.validation import validate_tenant  # noqa: E402

TF = REPO / "infra" / "tenant"
P = "mpa-t-client-001"
SA = f"sa-rnp-web@{P}.iam.gserviceaccount.com"
DATASETS = {"ozon_raw": "ozon_raw", "ref": "ref", "ozon_mart": "ozon_mart", "tenant_ops": "tenant_ops",
            "analytics_share": "analytics_share", "tenant_locks": "tenant_locks"}


def doc(sa=SA, mode="OWNER_RNP_SERVICE", tenant="client_001"):
    d = {"tenant_id": tenant, "rnp_access": {"mode": mode}}
    if sa is not None:
        d["rnp_access"]["service_account"] = sa
    return d


# ═══════════════════════════════════════ только эта учётка
def test_exact_service_account_renders_reader_and_t6_edges():
    b = RA.contract_block(doc(), P, DATASETS)
    assert b == {"mode": "OWNER_RNP_SERVICE", "dataset_key": "analytics_share", "service_account": SA,
                 "authorized_views": [{"viewer_key": v, "source_key": s} for v, s in CA.AUTHORIZED_VIEW_EDGES]}


@pytest.mark.parametrize("sa", [
    "sa-rnp-web@mpa-t-client-002.iam.gserviceaccount.com",      # чужой арендатор
    "sa-rnp-web@mpa-platform.iam.gserviceaccount.com",          # платформа
    "sa-rnp-web@project-fa311fc0-4d87-4781-986.iam.gserviceaccount.com",   # EVETIS
    "sa-ozon-runtime@mpa-t-client-001.iam.gserviceaccount.com",  # другой SA арендатора
    "sa-rnp-refresh@mpa-t-client-001.iam.gserviceaccount.com",
    "serviceAccount:" + SA, SA.upper(), "analyst@example-client.test", "",
])
def test_any_other_identity_is_rejected(sa):
    with pytest.raises(RA.RnpAccessError):
        RA.contract_block(doc(sa=sa), P, DATASETS)


def test_only_allowed_tenants_and_modes():
    with pytest.raises(RA.RnpAccessError):
        RA.contract_block(doc(tenant="client_002", sa="sa-rnp-web@mpa-t-client-002.iam.gserviceaccount.com"), "mpa-t-client-002", DATASETS)
    with pytest.raises(RA.RnpAccessError):
        RA.contract_block(doc(mode="ANALYTICS_SHARE_VIEWER"), P, DATASETS)
    with pytest.raises(RA.RnpAccessError):
        RA.contract_block(doc(mode="NONE"), P, DATASETS)            # NONE с учёткой — ошибка
    with pytest.raises(RA.RnpAccessError):
        RA.contract_block({"tenant_id": "client_001", "rnp_access": {"mode": "OWNER_RNP_SERVICE", "service_account": SA, "role": "WRITER"}}, P, DATASETS)
    assert RA.contract_block(doc(mode="NONE", sa=None), P, DATASETS) is None
    assert RA.contract_block({"tenant_id": "client_001"}, P, DATASETS) is None
    assert RA.ALLOWED_TENANTS == ("client_001",) and RA.ACCOUNT_ID == "sa-rnp-web"


def test_t6_client_rule_is_not_relaxed():
    """Клиентский путь по-прежнему отвергает любые сервисные аккаунты, включая sa-rnp-web."""
    for p in ("user:" + SA, "group:" + SA, "serviceAccount:" + SA):
        with pytest.raises(CA.ClientAccessError):
            CA.principal(p)


def test_registry_validation():
    base = json.loads((REPO / "tenants" / "client_001" / "tenant.json").read_text(encoding="utf-8"))
    ok = dict(base, rnp_access={"mode": "OWNER_RNP_SERVICE", "service_account": SA})
    assert validate_tenant(ok, "client_001") == []
    for bad in ({"mode": "OWNER_RNP_SERVICE"}, {"mode": "OWNER_RNP_SERVICE", "service_account": "sa-rnp-web@mpa-t-client-002.iam.gserviceaccount.com"},
                {"mode": "NONE", "service_account": SA}, {"mode": "OWNER_RNP_SERVICE", "service_account": SA, "principals": ["user:a@b.test"]},
                {"mode": "ANALYTICS_SHARE_VIEWER"}):
        assert validate_tenant(dict(base, rnp_access=bad), "client_001"), bad


# ═══════════════════════════════════════ живой реестр и отсутствие клиентских идентичностей
def test_live_registry_activates_exactly_the_owner_rnp_service():
    """P1b: живой реестр client_001 включает РНП владельца и ничего клиентского."""
    c = R.terraform_inputs("client_001")
    assert c["client_access"] is None
    assert c["rnp_access"] == {"mode": "OWNER_RNP_SERVICE", "dataset_key": "analytics_share", "service_account": SA,
                               "authorized_views": [{"viewer_key": v, "source_key": s} for v, s in CA.AUTHORIZED_VIEW_EDGES]}


def test_block_has_no_client_identities_and_no_project_roles():
    c = SY.rnp_access_contract()
    ra = c["rnp_access"]
    assert c["client_access"] is None, "РНП не включает клиентский доступ"
    assert set(ra) == {"mode", "dataset_key", "service_account", "authorized_views"}
    assert "principals" not in ra and "query_role" not in ra
    assert PS.expected_iam(c) == PS.expected_iam(SY.contract_for_doc(_registry_without_rnp())), \
        "ни одной новой IAM-привязки проекта"


def test_rnp_fixture_is_current_registry_output():
    path = SY.FIXTURE_DIR / "rnp_access.client_001.contract.json"
    assert path.read_text(encoding="utf-8") == SY.rnp_access_fixture_text()


# ═══════════════════════════════════════ план арендатора: ровно 5 датасетов in-place
def _registry_without_rnp():
    d = copy.deepcopy(SY.load_tenant_document(SY.TENANTS_DIR / "client_001" / "tenant.json"))
    d.pop("rnp_access", None)
    return d


def _before_after():
    before = SY.contract_for_doc(_registry_without_rnp())
    after = SY.rnp_access_contract()
    return before, after


def test_contract_delta_is_only_rnp_access():
    before, after = _before_after()
    assert {k for k in set(before) | set(after) if before.get(k) != after.get(k)} == {"rnp_access"}


def test_tenant_plan_changes_exactly_five_datasets_in_place():
    before, after = _before_after()
    a, b = PS.expected_dataset_access(before), PS.expected_dataset_access(after)
    assert set(a) == set(b)
    changed = {ds for ds in a if a[ds] != b[ds]}
    assert changed == {"analytics_share", "ozon_mart", "tenant_ops", "ozon_raw", "ref"}
    assert "tenant_locks" not in changed
    added = {ds: b[ds] - a[ds] for ds in changed}
    assert all(not (a[ds] - b[ds]) for ds in changed), "только добавления в ACL"
    assert added["analytics_share"] == {("READER", "user_by_email", SA, None)}
    edge = lambda v: (PS.AUTHORIZED_VIEWS, "dataset", f"{P}.{v}", None)
    assert added["ozon_mart"] == {edge("analytics_share"), edge("tenant_ops")}
    assert added["tenant_ops"] == {edge("analytics_share")}
    assert added["ozon_raw"] == {edge("ozon_mart")}
    assert added["ref"] == {edge("ozon_mart"), edge("tenant_ops")}
    assert sum(len(x) for x in added.values()) == 7          # 1 READER + 6 рёбер
    assert PS.expected_iam(before) == PS.expected_iam(after)


def test_t6_and_rnp_together_do_not_duplicate_edges():
    c = SY.rnp_access_contract()
    c["client_access"] = SY.client_access_contract()["client_access"]
    acl = PS.expected_dataset_access(c)
    assert sum(1 for e in acl["ozon_mart"] if e[0] == PS.AUTHORIZED_VIEWS) == 2
    assert RA.authorized_edges(c) == set(CA.AUTHORIZED_VIEW_EDGES)


# ═══════════════════════════════════════ сканер плана
def _plan(contract):
    """Синтетический план корня арендатора (T3.2); записи РНП в ACL строит _plan_for по контракту."""
    from tools.tests.test_tenancy_t32_remediation import _plan_for
    c = copy.deepcopy(contract)
    c.pop("orchestration", None)        # синтетический план T3.2 строит только корень и площадку
    return c, _plan_for(c)


def test_plan_scan_accepts_exact_rnp_plan_and_nothing_else():
    c, plan = _plan(SY.rnp_access_contract())
    assert PS.scan_plan(plan, c) == []
    c0, _ = _plan(_before_after()[0])
    assert PS.scan_plan(plan, c0), "без активации в реестре эти записи ACL — лишние"


def _mut(kind):
    c, plan = _plan(SY.rnp_access_contract())
    ds = {rc["change"]["after"]["dataset_id"]: rc["change"]["after"]["access"] for rc in plan["resource_changes"] if rc["type"] == "google_bigquery_dataset"}
    if kind == "SA READER on ozon_mart":
        ds["ozon_mart"].append({"role": "READER", "user_by_email": SA})
    elif kind == "SA WRITER on analytics_share":
        next(a for a in ds["analytics_share"] if a.get("user_by_email") == SA)["role"] = "WRITER"
    elif kind == "another SA on analytics_share":
        ds["analytics_share"].append({"role": "READER", "user_by_email": f"sa-ozon-runtime@{P}.iam.gserviceaccount.com"})
    elif kind == "client user on analytics_share":
        ds["analytics_share"].append({"role": "READER", "user_by_email": "analyst@example-client.test"})
    elif kind == "SA on tenant_locks":
        ds["tenant_locks"].append({"role": "READER", "user_by_email": SA})
    elif kind == "extra edge analytics_share -> ozon_raw":
        ds["ozon_raw"].append({"dataset": [{"dataset": [{"project_id": P, "dataset_id": "analytics_share"}], "target_types": ["VIEWS"]}]})
    elif kind == "contract SA swapped":
        c["rnp_access"]["service_account"] = f"sa-ozon-runtime@{P}.iam.gserviceaccount.com"
    elif kind == "contract on other tenant":
        c["tenant_id"] = "client_002"
    elif kind == "contract edge drift":
        c["rnp_access"]["authorized_views"].append({"viewer_key": "analytics_share", "source_key": "ozon_raw"})
    return c, plan


@pytest.mark.parametrize("kind", ["SA READER on ozon_mart", "SA WRITER on analytics_share", "another SA on analytics_share",
                                  "client user on analytics_share", "SA on tenant_locks", "extra edge analytics_share -> ozon_raw",
                                  "contract SA swapped", "contract on other tenant", "contract edge drift"])
def test_plan_scan_rejects_any_widening(kind):
    c, plan = _mut(kind)
    assert PS.scan_plan(plan, c), kind


# ═══════════════════════════════════════ Terraform
def test_terraform_gates_the_exception():
    main = (TF / "main.tf").read_text(encoding="utf-8"); var = (TF / "variables.tf").read_text(encoding="utf-8")
    assert "var.contract.rnp_access.service_account" in main and "local.authorized_views" in main
    assert 'var.contract.rnp_access.service_account == "sa-rnp-web@${var.contract.project_id}.iam.gserviceaccount.com"' in var
    assert "client_00" not in var and "client_00" not in main          # список арендаторов — только в реестре (rnp_access.py)
    assert (TF / "tests" / "rnp_access.tftest.hcl").exists()
