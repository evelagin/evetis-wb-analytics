"""Tenancy T4-d — analytics_share: семантический контракт, «нет данных ≠ ноль», доступа нет.

Офлайн. Поведение представлений проверяется исполнением в sqlite на синтетических строках
(tools/tests/tenancy_sql_harness.py). Привязка к кабинету проверяется в test_tenancy_t4c.
Компиляция в BigQuery — `python tools/tenancy/sql_package.py dryrun <tenant_id>`.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest
import sqlglot
from sqlglot import exp

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from tools.tenancy import plan_scan as PS  # noqa: E402
from tools.tenancy import sql_package as SP  # noqa: E402
from tools.tenancy import synthetic as SY  # noqa: E402
from tools.tenancy import validation as V  # noqa: E402
from tools.tests import tenancy_sql_harness as H  # noqa: E402

C1 = SY.fixture_contract("client_001")
SHARE = SP.PACKAGE_DIR / "analytics_share"
CONTRACT = json.loads((SHARE / "CONTRACT.json").read_text(encoding="utf-8"))
STATUSES = ("COMPLETE", "PARTIAL", "NOT_AVAILABLE", "NOT_APPLICABLE", "FAILED", "UNKNOWN")


def _objects():
    order, findings = SP.load_package(SP.PACKAGE_DIR, C1)
    assert findings == [], findings
    return {(o.dataset_key, o.name): o for o in order}


def _share_names():
    return sorted(n for ds, n in _objects() if ds == "analytics_share")


def _sql(ds, name):
    return (SP.PACKAGE_DIR / ds / f"{name}.sql").read_text(encoding="utf-8")


def _columns(name):
    tree = sqlglot.parse_one(_sql("analytics_share", name), read="bigquery")
    return [e.alias_or_name for e in tree.expression.selects]


def _run(ds, name, tables):
    return H.run(ds, name, tables)


# ═══════════════════════════════════════ контракт ↔ SQL
def test_contract_describes_exactly_the_share_views():
    assert sorted(CONTRACT["objects"]) == _share_names()
    assert len(_share_names()) == 9


@pytest.mark.parametrize("name", sorted(CONTRACT["objects"]))
def test_every_column_is_described_exactly_once(name):
    spec = CONTRACT["objects"][name]
    groups = [set(spec["dimensions"]), set(spec["metrics"]), set(spec["status_columns"])]
    assert not (groups[0] & groups[1] or groups[0] & groups[2] or groups[1] & groups[2]), name
    assert set().union(*groups) == set(_columns(name)), name
    assert set(spec["grain"]) <= groups[0], name
    for key in ("business_meaning", "freshness", "completeness", "limitations", "lineage"):
        assert key in spec, (name, key)
    for col, m in spec["metrics"].items():
        assert m["unit"] and m["meaning"] and isinstance(m["additive"], bool), (name, col)


@pytest.mark.parametrize("name", sorted(CONTRACT["objects"]))
def test_contract_lineage_matches_parsed_sql(name):
    objs = _objects()
    key = ("analytics_share", name)
    direct = sorted(f"{d}.{n}" for d, n in objs[key].refs)
    assert CONTRACT["objects"][name]["lineage"]["direct"] == direct

    def up(k, acc):
        for r in objs[k].refs:
            if r not in acc:
                acc.add(r)
                if r in objs:
                    up(r, acc)
        return acc
    sources = sorted(f"{d}.{n}" for d, n in up(key, set()) if (d, n) not in objs)
    assert CONTRACT["objects"][name]["lineage"]["sources"] == sources


def test_share_reads_only_marts_and_ops_never_raw_or_ref_directly():
    for (ds, name), o in _objects().items():
        if ds == "analytics_share":
            assert {d for d, _ in o.refs} <= {"ozon_mart", "tenant_ops"}, (name, o.refs)


def test_share_views_are_marketplace_facts_or_generic_derived():
    manifest = json.loads((SP.PACKAGE_DIR / "PACKAGE.json").read_text(encoding="utf-8"))
    classes = {o["semantic_class"] for o in manifest["objects"] if o["dataset"] == "analytics_share"}
    assert classes <= {"MARKETPLACE_FACT", "GENERIC_DERIVED"}


# ═══════════════════════════════════════ полнота: нет данных ≠ ноль
@pytest.mark.parametrize("name", sorted(CONTRACT["objects"]))
def test_status_columns_default_to_unknown_and_use_declared_entities(name):
    sql = _sql("analytics_share", name)
    doc = V.load_tenant_document(REPO / "tenants" / "client_001" / "tenant.json")
    entities = set(doc["marketplaces"]["ozon"]["entities"])
    for col, st in CONTRACT["objects"][name]["status_columns"].items():
        assert re.search(rf"COALESCE\([^()]*(?:\([^()]*\))?[^()]*, 'UNKNOWN'\) AS {col}", sql), (name, col)
        upstream = [sql] + [_sql(d, n) for d, n in _objects()[("analytics_share", name)].refs if d == "tenant_ops"]
        assert any(f"'{st['entity']}'" in s for s in upstream), (name, st["entity"])
        assert st["entity"] in entities, st["entity"]


def test_status_vocabulary_matches_the_coverage_table():
    assert tuple(CONTRACT["status_vocabulary"]) == STATUSES


@pytest.mark.parametrize("name", sorted(CONTRACT["objects"]))
def test_no_metric_is_coalesced_to_zero(name):
    sql = _sql("analytics_share", name)
    assert not re.search(r"(?i)(COALESCE|IFNULL)\([^;]*?,\s*0\)", sql), name


def test_every_daily_share_view_carries_a_completeness_status():
    for name, spec in CONTRACT["objects"].items():
        if "date" in " ".join(spec["grain"]) and name != "data_coverage":
            assert spec["status_columns"] or name == "price_history", name


COV = (["entity", "coverage_date", "status", "reason", "evaluated_at"])
SALES_COLS = ["order_date_msk", "sku", "postings", "ordered_units", "ordered_value_rub", "cancelled_units",
              "delivered_units", "in_progress_units", "delivered_value_rub"]


def test_sales_daily_zero_only_when_complete_null_otherwise():
    rows = _run("analytics_share", "sales_daily", {
        "FACT_OZON_SALES_DAILY": (SALES_COLS, [("2026-01-01", 1, 2, 3, 300.0, 0, 3, 0, 300.0)]),
        "V_COVERAGE_DAILY": (COV, [("fbo_postings", "2026-01-01", "COMPLETE", None, "t"),
                                   ("fbo_postings", "2026-01-02", "COMPLETE", None, "t"),
                                   ("fbo_postings", "2026-01-03", "FAILED", "x", "t"),
                                   ("ads_sku_daily", "2026-01-04", "COMPLETE", None, "t")]),
    })
    by = {r["sales_date"]: r for r in rows}
    assert set(by) == {"2026-01-01", "2026-01-02", "2026-01-03"}
    assert by["2026-01-01"]["orders"] == 2 and by["2026-01-01"]["data_status"] == "COMPLETE"
    assert by["2026-01-02"]["orders"] == 0 and by["2026-01-02"]["ordered_value_rub"] == 0
    assert by["2026-01-03"]["orders"] is None and by["2026-01-03"]["data_status"] == "FAILED"


def test_sales_daily_without_coverage_is_unknown_not_complete():
    rows = _run("analytics_share", "sales_daily", {
        "FACT_OZON_SALES_DAILY": (SALES_COLS, [("2026-01-01", 1, 1, 1, 10.0, 0, 1, 0, 10.0)]),
        "V_COVERAGE_DAILY": (COV, []),
    })
    assert rows == [dict(rows[0], data_status="UNKNOWN")] and rows[0]["orders"] == 1


def test_sku_daily_keeps_ad_spend_on_days_without_orders():
    rows = _run("analytics_share", "sku_daily", {
        "FACT_OZON_SALES_DAILY": (SALES_COLS, [("2026-01-01", 7, 1, 1, 100.0, 0, 1, 0, 100.0)]),
        "NORM_OZON_ADS_SKU_DAILY": (["stat_date", "sku", "attributed_spend_rub", "orders", "revenue_promo_rub", "clicks"],
                                    [("2026-01-01", 7, 10.0, 1, 100.0, 5), ("2026-01-02", 8, 4.0, 0, 0.0, 2)]),
        "DIM_OZON_PRODUCT": (["sku", "internal_sku", "product_name", "mapping_status", "sku_joinable"], [(7, "A", "a", "MAPPED", True)]),
        "V_COVERAGE_DAILY": (COV, []),
    })
    by = {(r["sales_date"], r["sku"]): r for r in rows}
    assert sum(r["ad_spend_attributed_rub"] for r in rows) == 14.0
    assert by[("2026-01-01", 7)]["drr_ratio"] == pytest.approx(0.1)
    lone = by[("2026-01-02", 8)]
    assert lone["orders"] is None and lone["mapping_status"] == "UNMAPPED" and lone["ads_data_status"] == "UNKNOWN"


# ═══════════════════════════════════════ доступ: никого, механизм — T6
def test_contract_grants_nothing_and_defers_the_access_mechanism():
    assert CONTRACT["access"]["principals"] == []
    assert CONTRACT["access"]["mechanism"] == "DEFERRED_T6"


def test_share_acl_is_project_owners_only_for_every_tenant():
    for tid in ("client_001", *SY.SYNTHETIC_TENANTS):
        c = SY.fixture_contract(tid)
        acl = PS.expected_dataset_access(c)[c["datasets"]["analytics_share"]]
        # T4.1: в клиентском слое — projectOwners и деплоер SQL (создание/изменение, без getData).
        dep = f"sa-sql-deployer@{c['project_id']}.iam.gserviceaccount.com"
        assert {(r.rsplit("/", 1)[-1], who) for r, _k, who, _c in acl} == \
            {("OWNER", "projectOwners"), ("mpaSqlViewCreate", dep), ("mpaSqlViewUpdate", dep)}, tid


def test_package_creates_no_grants_or_authorized_views():
    for p in SP.PACKAGE_DIR.rglob("*.sql"):
        text = p.read_text(encoding="utf-8").upper()
        assert "GRANT " not in text and "AUTHORIZED" not in text and "ROW ACCESS POLICY" not in text, p.name
    tf = "\n".join(p.read_text(encoding="utf-8") for p in (REPO / "infra" / "tenant").glob("*.tf"))
    assert "authorized_view" not in tf and "google_bigquery_dataset_access" not in tf


@pytest.mark.parametrize("col", ["customer", "phone", "email", "address", "buyer", "fio", "passport"])
def test_orders_expose_no_buyer_personal_data(col):
    assert not any(col in c.lower() for c in _columns("orders"))


# ═══════════════════════════════════════ переносимость
def test_contract_is_tenant_neutral():
    text = (SHARE / "CONTRACT.json").read_text(encoding="utf-8")
    for pat in (r"client_\d+", r"mpa-t", r"(?i)evetis", r"project-fa311fc0", r"[0-9]{10,}", r"20\d\d-\d\d-\d\d"):
        assert not re.search(pat, text), pat


def test_share_renders_identically_for_client_002(tmp_path, monkeypatch):
    from tools.tenancy import registry as R
    monkeypatch.setattr(R, "terraform_inputs", lambda t: SY.fixture_contract(t))
    a = SP.render("client_001", tmp_path / "a")
    b = SP.render("client_002", tmp_path / "b")
    share_a = [o for o in a["objects"] if o["dataset"] == "analytics_share"]
    assert len(share_a) == 9
    for o in share_a:
        ta = (tmp_path / "a" / "analytics_share" / f"{o['name']}.sql").read_text(encoding="utf-8")
        tb = (tmp_path / "b" / "analytics_share" / f"{o['name']}.sql").read_text(encoding="utf-8")
        assert "mpa-t-client-001" in ta and "mpa-t-client-002" in tb
        assert ta.replace("mpa-t-client-001", "P") == tb.replace("mpa-t-client-002", "P")


# ═══════════════════════════════════════ ревью T4: полнота начислений и результат
FIN = "finance_accrual"


ECONCFG = ["parameter", "scope", "internal_sku", "value_num", "value_text", "unit", "effective_from",
           "effective_to", "source", "loaded_at"]
MATURITY = "FINANCE_SETTLEMENT_MATURITY_DAYS"


def _window(coverage, maturity=((MATURITY, "STORE", None, 2, None, "d", "2026-01-01", None, "o", "t"),)):
    rows = _run("tenant_ops", "V_FINANCE_WINDOW_STATUS", {
        "V_COVERAGE_DAILY": (COV, [(FIN, d, s, None, "t") for d, s in coverage]
                             + [("fbo_postings", "2026-01-02", "FAILED", None, "t")]),
        "REF_TENANT_ECONOMICS": (ECONCFG, list(maturity))})
    return {r["from_date"]: (r["status"], r["reason"]) for r in rows}


C = "COMPLETE"


@pytest.mark.parametrize("coverage,expected", [
    ([("2026-01-01", C), ("2026-01-02", C), ("2026-01-03", C)],
     {"2026-01-01": (C, None), "2026-01-02": ("PARTIAL", "NOT_MATURE"), "2026-01-03": ("PARTIAL", "NOT_MATURE")}),
    ([("2026-01-01", C), ("2026-01-02", "FAILED"), ("2026-01-03", C), ("2026-01-04", C), ("2026-01-05", C)],
     {"2026-01-01": ("PARTIAL", "TAIL_INCOMPLETE"), "2026-01-02": ("PARTIAL", "TAIL_INCOMPLETE"),
      "2026-01-03": (C, None), "2026-01-04": ("PARTIAL", "NOT_MATURE"), "2026-01-05": ("PARTIAL", "NOT_MATURE")}),
    ([("2026-01-01", C), ("2026-01-03", C), ("2026-01-04", C), ("2026-01-05", C)],        # пропуск суток 02
     {"2026-01-01": ("PARTIAL", "TAIL_INCOMPLETE"), "2026-01-03": (C, None),
      "2026-01-04": ("PARTIAL", "NOT_MATURE"), "2026-01-05": ("PARTIAL", "NOT_MATURE")}),
    ([("2026-01-01", "NOT_APPLICABLE"), ("2026-01-02", C), ("2026-01-03", C)],
     {"2026-01-01": (C, None), "2026-01-02": ("PARTIAL", "NOT_MATURE"), "2026-01-03": ("PARTIAL", "NOT_MATURE")}),
])
def test_finance_window_requires_a_complete_and_mature_tail(coverage, expected):
    assert _window(coverage) == expected


@pytest.mark.parametrize("maturity", [
    (),                                                                                    # не задан
    ((MATURITY, "STORE", None, 2, None, "d", "2026-01-01", None, "o", "t"),
     (MATURITY, "STORE", None, 5, None, "d", "2026-01-01", None, "o", "t")),               # неоднозначно
    ((MATURITY, "STORE", None, 2, None, "d", "2099-01-01", None, "o", "t"),),              # ещё не действует
    ((MATURITY, "SKU", "A1", 2, None, "d", "2026-01-01", None, "o", "t"),),                # не уровень магазина
])
def test_finance_window_without_seller_maturity_is_unknown_never_complete(maturity):
    got = _window([("2026-01-01", C), ("2026-01-02", C), ("2026-01-03", C), ("2026-01-04", C)], maturity)
    assert {s for s, _ in got.values()} <= {"UNKNOWN", "PARTIAL"}
    assert got["2026-01-01"] == ("UNKNOWN", "MATURITY_NOT_CONFIGURED")


@pytest.mark.parametrize("statuses,winner", [
    (["COMPLETE", "FAILED"], "FAILED"),
    (["NOT_APPLICABLE", "PARTIAL"], "PARTIAL"),
    (["NOT_APPLICABLE", "UNKNOWN"], "UNKNOWN"),
    (["COMPLETE", "NOT_AVAILABLE"], "NOT_AVAILABLE"),
    (["COMPLETE", "SOMETHING_NEW"], "SOMETHING_NEW"),
])
def test_coverage_tie_prefers_the_worse_status(statuses, winner):
    rows = _run("tenant_ops", "V_COVERAGE_DAILY", {"DATA_COVERAGE": (
        ["entity", "coverage_date", "status", "reason", "rows_loaded", "source_run_id", "evaluated_at"],
        [(FIN, "2026-01-01", s, None, 1, str(i), "t") for i, s in enumerate(statuses)])})
    assert [r["status"] for r in rows] == [winner]


ECON = ["order_date_msk", "sku", "internal_sku", "delivered_units", "seller_revenue_rub", "commission_rub",
        "logistics_rub", "acquiring_rub", "return_logistics_rub", "promotion_rub", "other_costs_rub",
        "unclassified_rub", "unsettled_postings", "postings_with_return_costs", "contribution_pre_cogs_rub",
        "product_cogs_rub", "contribution_after_cogs_rub", "revenue_basis", "taxonomy_status", "cogs_coverage"]
ECON_ROW = ("2026-01-05", "S1", "A1", 1, 1000, -300, -100, -15, 0, 0, 0, 0, 0, 0, 585, 200, 385,
            "SETTLED", "CLASSIFIED", "COMPLETE")


@pytest.mark.parametrize("window,status,pre,after", [
    ([("2026-01-05", "COMPLETE", None)], "COMPLETE", 585, 385),
    ([("2026-01-05", "PARTIAL", "NOT_MATURE")], "PARTIAL", None, None),
    ([("2026-01-05", "UNKNOWN", "MATURITY_NOT_CONFIGURED")], "UNKNOWN", None, None),
    ([], "UNKNOWN", None, None),
])
def test_profitability_publishes_a_result_only_with_complete_finance(window, status, pre, after):
    rows = _run("analytics_share", "profitability_daily", {
        "FACT_OZON_SKU_ECONOMICS_DAILY": (ECON, [ECON_ROW]),
        "DIM_OZON_PRODUCT": (["sku", "product_name", "sku_joinable"], [("S1", "a", True)]),
        "V_FINANCE_WINDOW_STATUS": (["from_date", "status", "reason"], window)})
    (r,) = rows
    assert (r["finance_data_status"], r["contribution_pre_cogs_rub"], r["contribution_after_cogs_rub"]) == \
        (status, pre, after)
    assert r["seller_revenue_rub"] == 1000 and r["commission_rub"] == -300     # факты видны всегда


def test_store_costs_keep_the_scope_so_cancelled_postings_are_visible():
    rows = _run("analytics_share", "store_costs_daily", {
        "FACT_OZON_STORE_COSTS_DAILY": (["event_date", "accrual_class", "cost_scope", "sku", "amount_rub",
                                         "commission_rub", "accruals", "unresolved_accruals"],
                                        [("2026-01-10", "CANCELLATION_COST", "POSTING_NOT_DELIVERED", "S1", -80, None, 1, 0),
                                         ("2026-01-10", "SUBSCRIPTION", "STORE", None, -1990, None, 1, 0)]),
        "V_COVERAGE_DAILY": (COV, [(FIN, "2026-01-10", "COMPLETE", None, "t")])})
    assert {(r["cost_scope"], r["amount_rub"], r["data_status"]) for r in rows} == {
        ("POSTING_NOT_DELIVERED", -80, "COMPLETE"), ("STORE", -1990, "COMPLETE")}


def test_contract_states_the_known_gaps():
    lim = " ".join(CONTRACT["objects"]["profitability_daily"]["limitations"])
    assert "Возвраты" in lim and "не прибыль" in lim and "Поздние начисления" in lim
