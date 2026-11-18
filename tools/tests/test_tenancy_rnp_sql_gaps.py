"""Пробелы analytics_share для РНП Heldi (G-1, G-4, G-5, G-10, G-12): исполнение в sqlite на синтетике.

  G-5  store_pnl_daily / FACT_OZON_ACCRUAL_DAILY — результат магазина по дате начисления, все начисления;
  G-4  profitability_daily.*_accrued_rub, settled_postings — известная часть начислений нерассчитанных;
  G-1  orders.data_status, status_observed_at;
  G-10 stock_daily — история остатков по снимкам;
  G-12 return_logistics_daily — обратная логистика (признак возвратов, не их число).
Офлайн, синтетические строки; правила «нет данных ≠ 0» и «сумма начислений сходится».
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from tools.tests import tenancy_sql_harness as H  # noqa: E402
from tools.tests.test_tenancy_t4c import ACC_COLS, COGS_COLS, LINE_COLS, _taxonomy, acc, line  # noqa: E402

COV = ["entity", "coverage_date", "status", "reason", "evaluated_at"]
DIM = (["sku", "internal_sku", "product_name", "offer_id", "ozon_name", "sku_joinable"],
       [("S1", "A1", "Товар 1", "OF-1", "Ozon 1", 1), ("S2", "A2", "Товар 2", "OF-2", "Ozon 2", 1)])


def _db(accruals, lines, coverage=()):
    tax = _taxonomy()
    db = H.database({
        "RAW_OZON_FINANCE_ACCRUAL": (ACC_COLS, accruals),
        "DIM_OZON_ACCRUAL_TYPE": (["type_id", "ozon_type_name", "accrual_class", "classification_basis"],
                                  [(i, v["ozon_type_name"], v["accrual_class"], v["classification_basis"]) for i, v in tax.items()]),
        "NORM_OZON_POSTING_LINE": (LINE_COLS, lines),
        "DIM_OZON_PRODUCT": DIM,
        "ECON_TENANT_COGS": (COGS_COLS, []),
        "V_COVERAGE_DAILY": (COV, list(coverage)),
    })
    for name in ("NORM_OZON_ACCRUAL", "NORM_OZON_POSTING_SETTLEMENT", "FACT_OZON_ACCRUAL_DAILY"):
        H.materialize(db, "ozon_mart", name)
    return db


def _class_of(type_id):
    return _taxonomy()[type_id]["accrual_class"]


# Продажа рассчитана (P1: блок 1000/−300, логистика −100 в том же начислении, эквайринг −15),
# отмена P2 (не доставлено) с логистикой −40, подписка магазина −1990, блок отправления вне истории P9.
SALE = [acc(1, 32, -100, sp=1000, cm=-300), acc(2, 1, -15)]
OTHER = [acc(3, 32, -40, posting="P2"), acc(4, 32, -7, posting="P9", sp=500, cm=-150)]
LINES = [line(), line(posting="P2", delivered=False)]
FIN_DAY = [("finance_accrual", "2026-01-10", "COMPLETE", None, "t")]


def test_g5_every_accrual_lands_exactly_once_and_sums_to_the_raw_ledger():
    db = _db(SALE + OTHER, LINES, FIN_DAY)
    rows = H.query(db, "analytics_share", "store_pnl_daily")
    norm = H.query(db, "ozon_mart", "NORM_OZON_ACCRUAL")
    non_sale = [r for r in rows if r["line_class"] not in ("SALE_REVENUE", "SALE_COMMISSION")]
    assert sum(r["amount_known_rub"] for r in non_sale) == sum(n["amount_rub"] for n in norm)          # −162
    assert sum(r["amount_known_rub"] for r in rows if r["line_class"] == "SALE_COMMISSION") == sum(n["commission_rub"] or 0 for n in norm)
    by = {(r["line_class"], r["cost_scope"]): r for r in rows}
    assert by[("SALE_REVENUE", "POSTING_DELIVERED")]["amount_rub"] == 1000
    assert by[("SALE_COMMISSION", "POSTING_DELIVERED")]["amount_rub"] == -300
    assert by[(_class_of(32), "POSTING_NOT_DELIVERED")]["amount_rub"] == -40
    assert all(r["data_status"] == "COMPLETE" for r in rows)


def test_g5_revenue_without_loaded_posting_is_unknown_not_zero():
    db = _db(SALE + OTHER, LINES, FIN_DAY)
    rows = H.query(db, "analytics_share", "store_pnl_daily")
    nl = {r["line_class"]: r for r in rows if r["cost_scope"] == "POSTING_NOT_LOADED"}
    assert nl["SALE_REVENUE"]["amount_rub"] is None and nl["SALE_REVENUE"]["unknown_amounts"] == 1
    assert nl["SALE_REVENUE"]["amount_known_rub"] is None
    assert nl["SALE_COMMISSION"]["amount_rub"] == -150                       # комиссия известна и без отправления


def test_g5_delivered_scope_equals_order_economics_of_settled_posting():
    db = _db(SALE, [line()], FIN_DAY)
    pnl = sum(r["amount_rub"] for r in H.query(db, "analytics_share", "store_pnl_daily") if r["cost_scope"] == "POSTING_DELIVERED")
    (econ,) = H.query(db, "ozon_mart", "FACT_OZON_SKU_ECONOMICS_DAILY")
    assert pnl == econ["contribution_pre_cogs_rub"] == 585                   # 1000 − 300 − 100 − 15


def test_g5_day_without_coverage_is_unknown():
    rows = H.query(_db(SALE, [line()], ()), "analytics_share", "store_pnl_daily")
    assert {r["data_status"] for r in rows} == {"UNKNOWN"}


def test_g4_accrued_columns_show_the_known_part_of_unsettled_postings():
    # P1 рассчитан; P3 доставлен, но экономического блока ещё нет — начислена только логистика.
    db = _db(SALE + [acc(5, 32, -60, posting="P3")], [line(), line(posting="P3")])
    (r,) = H.query(db, "ozon_mart", "FACT_OZON_SKU_ECONOMICS_DAILY")
    assert r["commission_rub"] is None and r["logistics_rub"] is None              # прежний контракт не тронут
    assert r["settled_postings"] == 1 and r["delivered_postings"] == 2
    assert r["commission_accrued_rub"] == -300 and r["logistics_accrued_rub"] == -160 and r["acquiring_accrued_rub"] == -15


def test_g4_profitability_exposes_accrued_and_settled_counts():
    db = _db(SALE + [acc(5, 32, -60, posting="P3")], [line(), line(posting="P3")])
    H.materialize(db, "ozon_mart", "FACT_OZON_SKU_ECONOMICS_DAILY")
    db.execute('CREATE TABLE "V_FINANCE_WINDOW_STATUS" (from_date, status, reason)')
    (r,) = H.query(db, "analytics_share", "profitability_daily")
    assert (r["postings"], r["settled_postings"], r["commission_accrued_rub"], r["logistics_accrued_rub"]) == (2, 1, -300, -160)
    assert r["commission_rub"] is None and r["contribution_pre_cogs_rub"] is None and r["finance_data_status"] == "UNKNOWN"


ORDER_LINE_COLS = ["posting_number", "sku", "order_date_msk", "status", "is_cancelled", "is_delivered", "quantity", "price_rub",
                   "old_price_rub", "payout_rub", "warehouse_name", "city", "extracted_at"]


@pytest.mark.parametrize("coverage,status", [([("fbo_postings", "2026-01-05", "COMPLETE", None, "t")], "COMPLETE"),
                                             ([("fbo_postings", "2026-01-05", "PARTIAL", "x", "t")], "PARTIAL"), ([], "UNKNOWN")])
def test_g1_orders_carry_completeness_and_status_age(coverage, status):
    rows = H.run("analytics_share", "orders", {
        "NORM_OZON_POSTING_LINE": (ORDER_LINE_COLS, [("P1", "S1", "2026-01-05", "delivering", 0, 0, 1, 100.0, 120.0, 80.0, "W", "Казань", "2026-01-06T03:00:00")]),
        "DIM_OZON_PRODUCT": DIM, "V_COVERAGE_DAILY": (COV, coverage)})
    (r,) = rows
    assert r["data_status"] == status and r["status_observed_at"] == "2026-01-06T03:00:00" and r["status"] == "delivering"


STOCK_COLS = ["snapshot_date", "sku", "warehouse_id", "warehouse_name", "cluster_id", "cluster_name", "available_stock_count",
              "valid_stock_count", "transit_stock_count", "excess_stock_count", "days_without_sales", "turnover_grade", "extracted_at"]


def test_g10_stock_history_sums_warehouses_per_snapshot_and_keeps_status():
    rows = H.run("analytics_share", "stock_daily", {
        "SNAP_OZON_STOCK": (STOCK_COLS, [("2026-01-01", "S1", "W1", "Склад 1", "C1", "Москва", 5, 5, 1, 0, 0, "G", "t"),
                                         ("2026-01-01", "S1", "W2", "Склад 2", "C2", "Юг", 0, 0, 3, 0, 0, "G", "t"),
                                         ("2026-01-02", "S1", "W1", "Склад 1", "C1", "Москва", 4, 4, 0, 0, 0, "G", "t")]),
        "DIM_OZON_PRODUCT": DIM, "V_COVERAGE_DAILY": (COV, [("stocks", "2026-01-01", "COMPLETE", None, "t")])})
    by = {r["snapshot_date"]: r for r in rows}
    assert set(by) == {"2026-01-01", "2026-01-02"}                         # дней без снимка нет
    assert (by["2026-01-01"]["available_units"], by["2026-01-01"]["transit_units"], by["2026-01-01"]["warehouses_with_stock"]) == (5, 4, 1)
    assert by["2026-01-01"]["stock_data_status"] == "COMPLETE" and by["2026-01-02"]["stock_data_status"] == "UNKNOWN"
    assert by["2026-01-02"]["offer_id"] == "OF-1"


def test_g12_return_logistics_split_by_delivery_of_the_posting():
    rl = next(i for i, v in _taxonomy().items() if v["accrual_class"] == "RETURN_LOGISTICS")
    db = _db(SALE + [acc(6, rl, -70), acc(7, rl, -30, posting="P2")], LINES, FIN_DAY)
    rows = H.query(db, "analytics_share", "return_logistics_daily")
    by = {r["cost_scope"]: r for r in rows}
    assert by["POSTING_DELIVERED"]["return_logistics_rub"] == -70 and by["POSTING_DELIVERED"]["postings"] == 1
    assert by["POSTING_NOT_DELIVERED"]["return_logistics_rub"] == -30
    assert {r["offer_id"] for r in rows} == {"OF-1"} and {r["data_status"] for r in rows} == {"COMPLETE"}
