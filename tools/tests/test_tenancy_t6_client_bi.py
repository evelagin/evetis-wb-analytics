"""Tenancy T6 — клиентский BI: витрины inventory_cover и data_freshness, спецификация отчёта.

Офлайн: поведение представлений исполняется в sqlite (tools/tests/tenancy_sql_harness.py) на
синтетических строках. Главное правило — «нет данных ≠ ноль»: неполная база скорости, не
загруженные сегодня сутки и ни разу не оценённая сущность дают PARTIAL/UNKNOWN, а не нули.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from tools.client_bi import report as BI  # noqa: E402
from tools.tenancy import ozon_contract as OC  # noqa: E402
from tools.tenancy import validation as V  # noqa: E402
from tools.tests import tenancy_sql_harness as H  # noqa: E402

SHARE = H.PACKAGE_DIR / "analytics_share"
COV = ["entity", "coverage_date", "status", "reason", "evaluated_at"]
STOCK = ["snapshot_date", "sku", "warehouse_id", "warehouse_name", "cluster_id", "cluster_name", "available_stock_count",
         "valid_stock_count", "transit_stock_count", "excess_stock_count", "days_without_sales", "turnover_grade", "extracted_at"]
SALES = ["order_date_msk", "sku", "postings", "ordered_units", "ordered_value_rub", "cancelled_units", "delivered_units",
         "in_progress_units", "delivered_value_rub"]
DIM = (["sku", "internal_sku", "product_name", "offer_id", "ozon_name", "sku_joinable"],
       [(1, None, None, "OF-1", "Сыворотка", True), (2, None, None, "OF-2", "Крем", True), (3, None, None, "OF-3", "Тоник", True)])


def _d(s: str) -> date:
    return date.fromisoformat(s)


def _days(end: str, n: int) -> list[str]:
    return [(_d(end) - timedelta(days=i)).isoformat() for i in range(n)]


def _cover(postings_days, stock_status="COMPLETE", snap="2026-10-25", stock_rows=None, sales=None):
    cov = [("fbo_postings", d, st, None, "2026-10-25T00:00:00") for d, st in postings_days]
    if stock_status:
        cov.append(("stocks", snap, stock_status, None, "2026-10-25T00:00:00"))
    stock_rows = stock_rows if stock_rows is not None else [
        (snap, 1, 10, "W1", 1, "Москва", 70, 70, 5, 0, 3, "ACTUAL", "t"),
        (snap, 1, 11, "W2", 2, "Казань", 30, 30, 0, 0, 3, "ACTUAL", "t"),
        (snap, 2, 10, "W1", 1, "Москва", 50, 50, 0, 0, 40, "SURPLUS", "t"),
    ]
    rows = H.run("analytics_share", "inventory_cover", {
        "SNAP_OZON_STOCK": (STOCK, stock_rows),
        "V_COVERAGE_DAILY": (COV, cov),
        "FACT_OZON_SALES_DAILY": (SALES, sales or []),
        "DIM_OZON_PRODUCT": DIM,
    })
    return {r["sku"]: r for r in rows}


def _sales(end, n, sku, ordered, cancelled):
    return [(d, sku, ordered, ordered, ordered * 100.0, cancelled, ordered - cancelled, 0, 0.0) for d in _days(end, n)]


# ═══════════════════════════════════════ inventory_cover
def test_cover_is_complete_only_with_14_complete_fresh_days():
    end = "2026-10-24"
    r = _cover([(d, "COMPLETE") for d in _days(end, 14)], sales=_sales(end, 14, 1, 3, 1) + _sales(end, 14, 3, 1, 0))
    assert r[1]["available_units"] == 100 and r[1]["transit_units"] == 5 and r[1]["clusters_with_stock"] == 2
    assert r[1]["net_ordered_units_basis"] == 28 and r[1]["avg_daily_units"] == pytest.approx(2.0)
    assert r[1]["days_of_cover"] == pytest.approx(50.0)
    assert r[1]["velocity_data_status"] == "COMPLETE" and r[1]["velocity_status_reason"] is None
    assert r[1]["offer_id"] == "OF-1" and r[1]["stock_data_status"] == "COMPLETE"
    # Есть в полной базе, нет продаж — скорость 0, запас в днях не вычисляется (а не бесконечность/ноль).
    assert r[2]["avg_daily_units"] == 0 and r[2]["days_of_cover"] is None
    # Продаётся, но в ПОЛНОМ снимке нет — остаток 0, запас 0 дней (закончился).
    assert r[3]["available_units"] == 0 and r[3]["days_of_cover"] == 0


def test_stale_basis_keeps_cover_but_marks_partial():
    end = "2026-10-07"                                     # как у client_001: отправления полны только по 07.10
    r = _cover([(d, "COMPLETE") for d in _days(end, 14)], sales=_sales(end, 14, 1, 3, 1))
    assert r[1]["velocity_data_status"] == "PARTIAL" and r[1]["velocity_status_reason"] == "BASIS_STALE"
    assert r[1]["velocity_basis_lag_days"] == 18 and r[1]["days_of_cover"] == pytest.approx(50.0)


def test_incomplete_basis_gives_no_velocity_not_zero():
    end = "2026-10-24"
    days = [(d, "COMPLETE") for d in _days(end, 14)]
    days[5] = (days[5][0], "PARTIAL")
    r = _cover(days, sales=_sales(end, 14, 1, 3, 1))
    assert r[1]["avg_daily_units"] is None and r[1]["days_of_cover"] is None
    assert r[1]["velocity_data_status"] == "PARTIAL" and r[1]["velocity_status_reason"] == "BASIS_INCOMPLETE"
    assert r[2]["avg_daily_units"] is None                 # без полной базы и «нет продаж» не утверждаем


def test_no_complete_postings_is_unknown():
    r = _cover([])
    assert r[1]["velocity_data_status"] == "UNKNOWN" and r[1]["velocity_status_reason"] == "NO_COMPLETE_POSTINGS"
    assert r[1]["avg_daily_units"] is None and r[1]["available_units"] == 100


def test_missing_from_unevaluated_snapshot_is_null_not_zero():
    end = "2026-10-24"
    r = _cover([(d, "COMPLETE") for d in _days(end, 14)], stock_status=None, sales=_sales(end, 14, 3, 1, 0))
    assert r[3]["available_units"] is None and r[3]["stock_data_status"] == "UNKNOWN" and r[3]["days_of_cover"] is None


# ═══════════════════════════════════════ data_freshness
def _today() -> date:
    # sqlite CURRENT_DATE — UTC; в BigQuery — Москва. Тест опирается на ту же дату, что видит стенд.
    return datetime.now(timezone.utc).date()


def _fresh(cov_rows):
    rows = H.run("analytics_share", "data_freshness", {"V_COVERAGE_DAILY": (COV, cov_rows)})
    return {r["entity"]: r for r in rows}


def test_every_package_entity_is_listed_even_if_never_evaluated():
    r = _fresh([])
    assert set(r) == set(BI.FRESHNESS_ENTITIES)
    a = r["ads_sku_daily"]
    assert a["last_status"] == "UNKNOWN" and a["as_of_status"] == "UNKNOWN" and a["unknown_days_30"] == 30
    assert a["last_complete_date"] is None and a["days_since_last_complete"] is None and a["is_stale"] == 1


def test_today_not_loaded_is_unknown_not_yesterdays_complete():
    """STOP контроллера 25.10 20:10Z: сутки 26.10 не загружены — UNKNOWN, а не перенос COMPLETE."""
    t = _today()
    y = (t - timedelta(days=1)).isoformat()
    r = _fresh([("stocks", y, "COMPLETE", None, "x"), ("fbo_postings", (t - timedelta(days=18)).isoformat(), "COMPLETE", None, "x"),
                ("supplies", y, "PARTIAL", "BOUNDED", "x")])
    s = r["stocks"]
    assert s["as_of_status"] == "UNKNOWN" and s["last_status"] == "COMPLETE" and s["days_since_last_complete"] == 1
    assert s["is_stale"] == 0 and s["complete_days_30"] == 1 and s["unknown_days_30"] == 29
    f = r["fbo_postings"]
    assert f["days_since_last_complete"] == 18 and f["is_stale"] == 1
    assert r["supplies"]["last_status"] == "PARTIAL" and r["supplies"]["last_reason"] == "BOUNDED" and r["supplies"]["partial_days_30"] == 1


def test_freshness_entities_are_exactly_the_tenant_entities():
    doc = V.load_tenant_document(REPO / "tenants" / "client_001" / "tenant.json")
    assert set(BI.FRESHNESS_ENTITIES) == set(doc["marketplaces"]["ozon"]["entities"])
    assert set(BI.FRESHNESS_ENTITIES) <= set(OC.ENTITY_TABLES)
    sql = (SHARE / "data_freshness.sql").read_text(encoding="utf-8")
    assert set(re.findall(r"SELECT '([a-z_]+)'", sql)) == set(BI.FRESHNESS_ENTITIES)


# ═══════════════════════════════════════ спецификация отчёта ↔ контракт витрин
CONTRACT = json.loads((SHARE / "CONTRACT.json").read_text(encoding="utf-8"))


def test_every_field_in_the_report_spec_exists_in_the_share_contract():
    for source, fields in BI.spec_fields().items():
        spec = CONTRACT["objects"][source]
        known = set(spec["dimensions"]) | set(spec["metrics"]) | set(spec["status_columns"])
        assert fields <= known, (source, sorted(fields - known))


def test_report_reads_only_analytics_share_and_labels_contribution_pre_cogs():
    spec = BI.SPEC
    assert {s["dataset"] for s in spec["data_sources"].values()} == {"analytics_share"}
    assert all(s["credentials"] == "VIEWER" for s in spec["data_sources"].values())
    econ = next(p for p in spec["pages"] if p["id"] == "economics")
    text = json.dumps(econ, ensure_ascii=False)
    assert "PRE_COGS" in text and "прибыль" not in text.lower().replace("не прибыль", "")
    assert "contribution_after_cogs_rub" not in text            # себестоимости нет — «после» не показывается


def test_linking_url_targets_only_the_tenant_share_and_bills_the_tenant():
    url = BI.linking_url("TEMPLATE_ID", "mpa-t-client-001", "Аналитика Ozon — client_001")
    assert url.startswith("https://lookerstudio.google.com/reporting/create?c.reportId=TEMPLATE_ID")
    assert "ds.%2A.projectId=mpa-t-client-001" in url and "ds.%2A.datasetId=analytics_share" in url
    assert "ds.%2A.billingProjectId=mpa-t-client-001" in url
    assert "ozon_mart" not in url and "tenant_ops" not in url and "ozon_raw" not in url


def test_isolation_probe_covers_every_share_view_and_every_internal_dataset():
    probes = BI.isolation_probes("mpa-t-client-001")
    allowed = {p["object"] for p in probes if p["expect"] == "ALLOW"}
    denied = {p["object"].split(".")[1] for p in probes if p["expect"] == "DENY"}
    assert allowed == {f"mpa-t-client-001.analytics_share.{v}" for v in BI.CA.client_facing_views()}
    assert denied >= {"ozon_raw", "ref", "ozon_mart", "tenant_ops", "tenant_locks"}
