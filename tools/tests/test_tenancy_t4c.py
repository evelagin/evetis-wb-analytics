"""Tenancy T4-c — пакет SQL арендатора: состав, семантика, отсутствие эмпирики EVETIS.

Офлайн. Компиляция в BigQuery проверяется отдельно командой
`python tools/tenancy/sql_package.py dryrun <tenant_id>` (живой dry-run, 0 байт).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
import sqlglot
from sqlglot import exp

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from tools.tenancy import sql_package as SP  # noqa: E402
from tools.tenancy import synthetic as SY  # noqa: E402

C1 = SY.fixture_contract("client_001")


def _order():
    order, findings = SP.load_package(SP.PACKAGE_DIR, C1)
    assert findings == [], findings
    return {(o.dataset_key, o.name): o for o in order}


def _sql(ds, name):
    return (SP.PACKAGE_DIR / ds / f"{name}.sql").read_text(encoding="utf-8")


def test_package_has_the_designed_objects_and_only_package_layers():
    objs = _order()
    mart = {n for ds, n in objs if ds == "ozon_mart"}
    ops = {n for ds, n in objs if ds == "tenant_ops"}
    assert {"NORM_OZON_POSTING_LINE", "NORM_OZON_ACCRUAL", "FACT_OZON_SALES_DAILY",
            "FACT_OZON_SKU_ECONOMICS_DAILY", "FACT_OZON_STORE_COSTS_DAILY", "SNAP_OZON_STOCK",
            "SNAP_OZON_PRICE", "DIM_OZON_PRODUCT", "ECON_TENANT_COGS", "DIM_OZON_ACCRUAL_TYPE"} <= mart
    assert {"V_SELLER_BINDING_STATUS", "V_ENTITY_COVERAGE", "V_DQ_UNCLASSIFIED_ACCRUALS",
            "V_TENANT_STATE_CURRENT"} <= ops


def test_accrual_taxonomy_is_complete_unique_and_class_bounded():
    tree = sqlglot.parse_one(_sql("ozon_mart", "DIM_OZON_ACCRUAL_TYPE"), read="bigquery")
    ids, classes = [], set()
    for st in tree.find_all(exp.Struct):
        vals = {e.this.name: e.expression for e in st.expressions if isinstance(e, exp.PropertyEQ)}
        ids.append(int(vals["type_id"].this))
        classes.add(vals["accrual_class"].this)
    assert len(ids) == 33 and len(set(ids)) == 33
    assert classes == {"PROMOTION_BILLING", "PROMOTION_SERVICES", "SUBSCRIPTION", "LOGISTICS", "LAST_MILE",
                       "STORAGE", "ACQUIRING", "RETURN_LOGISTICS", "CANCELLATION_COST",
                       "OTHER_MARKETPLACE_COST", "COMPENSATION"}


def test_unknown_accrual_types_are_never_folded_into_other():
    s = _sql("ozon_mart", "NORM_OZON_ACCRUAL")
    assert "COALESCE(t.accrual_class, 'UNCLASSIFIED')" in s and "LEFT JOIN" in s
    econ = _sql("ozon_mart", "FACT_OZON_SKU_ECONOMICS_DAILY")
    assert "UNCLASSIFIED_PRESENT" in econ
    # результат до себестоимости только при нулевой неклассифицированной сумме
    assert re.search(r"IF\(SUM\(x\.unclassified_rub\) = 0,\s*SUM\(x\.seller_revenue_rub\)", econ)


def test_profit_after_cogs_is_null_unless_tenant_cogs_is_complete():
    econ = _sql("ozon_mart", "FACT_OZON_SKU_ECONOMICS_DAILY")
    assert "'COMPLETE', IF(COUNTIF(x.has_cogs) = 0, 'NOT_AVAILABLE', 'PARTIAL')" in econ
    assert re.search(r"IF\(COUNTIF\(NOT x\.has_cogs\) = 0 AND SUM\(x\.unclassified_rub\) = 0,", econ)
    assert "IF(COUNTIF(NOT x.has_cogs) = 0, SUM(x.product_cogs_rub), NULL)" in econ


def test_cogs_comes_only_from_tenant_reference_data():
    objs = _order()
    users = {k for k, o in objs.items() if ("ref", "REF_COGS") in o.refs}
    assert users == {("ozon_mart", "ECON_TENANT_COGS")}


@pytest.mark.parametrize("name", ["FACT_OZON_SKU_ECONOMICS_DAILY", "NORM_OZON_POSTING_SETTLEMENT",
                                  "FACT_OZON_SALES_DAILY", "FACT_OZON_STORE_COSTS_DAILY"])
def test_economic_facts_contain_no_calibration_constants(name):
    tree = sqlglot.parse_one(_sql("ozon_mart", name), read="bigquery")
    nums = {lit.this for lit in tree.find_all(exp.Literal) if not lit.is_string}
    assert nums <= {"0", "1"}, (name, nums)


def test_marketplace_day_is_moscow_not_utc():
    s = _sql("ozon_mart", "NORM_OZON_POSTING_LINE")
    assert "DATE(p.created_at, 'Europe/Moscow') AS order_date_msk" in s
    assert "p.order_date," not in s                       # UTC-дата RAW наружу не выдаётся


def test_unmapped_products_stay_unmapped():
    s = _sql("ozon_mart", "DIM_OZON_PRODUCT")
    assert "IF(m.internal_sku IS NULL, 'UNMAPPED', 'MAPPED')" in s


def test_seller_binding_status_fails_closed_on_any_difference():
    s = _sql("tenant_ops", "V_SELLER_BINDING_STATUS")
    assert "WHEN o.identity_fingerprint = b.identity_fingerprint THEN 'BOUND'" in s
    assert "ELSE 'MISMATCH'" in s and "'UNBOUND'" in s and "'NOT_OBSERVED'" in s


@pytest.mark.parametrize("marker", ["V_OZON_CIS_BUYOUT", "V_PRODUCT_COGS_EFFECTIVE", "REF_COST_BATCH",
                                    "REF_SKU_COGS_HISTORY", "FCT_OZON_SKU_PNL", "V_OZON_SKU_CURRENT_TARIFF",
                                    "sp_unit", "buyout_proceeds"])
def test_no_evetis_mart_objects_or_heuristics_in_package(marker):
    for p in SP.PACKAGE_DIR.glob("*/*.sql"):
        assert marker not in p.read_text(encoding="utf-8"), (p.name, marker)


def test_real_package_renders_identically_for_two_tenants_except_project(tmp_path, monkeypatch):
    from tools.tenancy import registry as R
    monkeypatch.setattr(R, "terraform_inputs", lambda t: SY.fixture_contract(t))
    a = SP.render("client_001", tmp_path / "a")
    b = SP.render("client_002", tmp_path / "b")
    assert [o["name"] for o in a["objects"]] == [o["name"] for o in b["objects"]]
    for o in a["objects"]:
        ta = (tmp_path / "a" / o["dataset"] / f"{o['name']}.sql").read_text(encoding="utf-8")
        tb = (tmp_path / "b" / o["dataset"] / f"{o['name']}.sql").read_text(encoding="utf-8")
        assert ta.replace("mpa-t-client-001", "P") == tb.replace("mpa-t-client-002", "P"), o["name"]


def test_compile_queries_are_self_contained_for_offline_contract():
    qs = SP.compile_queries(C1, live_tables=set())
    assert len(qs) == len(_order())
    for name, q in qs:
        assert SP.TEMPLATE_PROJECT not in q and "mpa-t-client-001" not in q, name   # всё — CTE-заглушки
        sqlglot.parse_one(q, read="bigquery")


def test_semantics_document_lists_every_package_object():
    doc = (REPO / "docs" / "architecture" / "TENANT_SQL_SEMANTICS.md").read_text(encoding="utf-8")
    for (ds, name) in _order():
        assert f"`{ds}.{name}`" in doc, name
