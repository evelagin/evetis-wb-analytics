#!/usr/bin/env python3
"""Печатает SQL «сухой сборки» SKU Performance V2: тело процедуры до записи (TEMP b + все ASSERT S1–S6) и сводку.

Release gate перед развёртыванием процедуры: выполнить вывод `bq query` — постоянные объекты не меняются
(журнал, замок и таблица слоя не трогаются; TEMP-таблица живёт в сессии скрипта). Если тело содержит
запись в постоянный объект — скрипт отказывается печатать.

    python3 tools/sku_performance_v2/dry_build_sql.py | bq query --nouse_legacy_sql --maximum_bytes_billed=30000000000

`--recon`: та же сухая сборка + сверка с живым слоем SKU_PERFORMANCE_V2_DAILY (только чтение): ключи
ADDED / REMOVED / COMMON по SKU и по каждому числовому полю — число отличающихся общих строк, Δ на общих ключах,
Σ удалённых, Σ добавленных. Список полей сверяется со схемой живой таблицы (ASSERT), чтобы новое поле не выпало.
"""
import re
import sys
from pathlib import Path

SQL = Path(__file__).resolve().parents[2] / "sql" / "dash" / "sku_performance_v2_daily_v1.sql"
START = "    CREATE TEMP TABLE b AS"
STOP = "    SET v_rows    = (SELECT COUNT(*) FROM b);"
WRITES = ("INSERT INTO", "UPDATE `", "DELETE FROM", "MERGE ", "CREATE TABLE", "CREATE OR REPLACE", "DROP ", "TRUNCATE")


def build(text: str) -> str:
    body = text[text.index(START):text.index(STOP)]
    found = [w for w in WRITES if w in body]
    if found:
        raise SystemExit(f"тело сборки содержит запись в постоянный объект: {found}")
    extra = sorted(set(re.findall(r"\bv_[a-z_]+\b", body)) - {"v_run_id", "v_started"})
    if extra:
        raise SystemExit(f"тело ссылается на неизвестные переменные процедуры: {extra}")
    return ("-- СУХАЯ СБОРКА SKU V2 (TEMP b + ASSERT S1–S6; постоянные объекты не меняются)\n"
            "DECLARE v_run_id STRING DEFAULT 'DRY-BUILD';\n"
            "DECLARE v_started TIMESTAMP DEFAULT CURRENT_TIMESTAMP();\n"
            + body +
            "\nSELECT COUNT(*) AS rows_built, MIN(day) AS d_min, MAX(day) AS d_max,\n"
            "       ANY_VALUE(source_data_through) AS source_data_through,\n"
            "       ROUND(SUM(storage_sku_rub), 6) AS storage_sku_rub,\n"
            "       ROUND(SUM(credited_for_goods_rub), 2) AS credited_rub, ROUND(SUM(cogs_rub), 2) AS cogs_rub\n"
            "FROM b;\n")


LAYER = "`project-fa311fc0-4d87-4781-986.wb_mart.SKU_PERFORMANCE_V2_DAILY`"
NUMERIC = (
    "orders_gross_units", "orders_cancelled_units", "orders_gross_seller_price_rub", "orders_cancelled_seller_price_rub",
    "cohort_orders", "cohort_buyout_orders", "cohort_cancelled_orders", "cohort_unresolved_orders", "cohort_conflict_orders",
    "cohort_resolved_orders", "cohort_returned_after_buyout_orders", "buyout_units", "buyout_seller_price_rub",
    "buyout_buyer_paid_rub", "buyout_buyer_paid_units", "sales_return_units", "fin_sale_units", "fin_return_units",
    "fin_chain_units", "fin_seller_price_rub", "fin_spp_rub", "fin_buyer_paid_rub", "credited_for_goods_rub",
    "fin_sale_seller_price_rub", "fin_return_seller_price_rub", "fin_return_for_pay_rub", "wb_fee_as_reported_rub",
    "chain_wb_remuneration_rub", "chain_wb_remuneration_vat_rub", "chain_acquiring_rub", "chain_pvz_reward_rub",
    "chain_rounding_rub", "logistics_rub", "logistics_sold_rub", "logistics_cancel_to_customer_rub",
    "logistics_cancel_from_customer_rub", "logistics_customer_return_rub", "logistics_defect_return_rub",
    "logistics_correction_rub", "logistics_unlabeled_rub", "logistics_other_label_rub", "storage_sku_rub",
    "ads_attributed_rub", "ad_views", "ad_clicks", "cogs_rub", "cogs_settlement_rub", "set_seller_price_min_rub",
    "set_seller_price_max_rub", "set_price_observations", "contribution_before_cogs_rub", "contribution_after_cogs_rub",
)


def recon(text: str) -> str:
    dry = build(text)
    body = dry[:dry.rindex("\nSELECT COUNT(*) AS rows_built")]
    cols = ", ".join(f"'{c}'" for c in NUMERIC)
    money = "\nUNION ALL\n".join(
        f"SELECT 'money', '{c}', NULL, "
        f"COUNTIF(o.day IS NOT NULL AND n.day IS NOT NULL AND IFNULL(CAST(o.{c} AS BIGNUMERIC), 0) != IFNULL(CAST(n.{c} AS BIGNUMERIC), 0)), "
        f"SUM(IF(o.day IS NOT NULL AND n.day IS NOT NULL, CAST(n.{c} AS BIGNUMERIC) - CAST(o.{c} AS BIGNUMERIC), 0)), "
        f"SUM(IF(n.day IS NULL, CAST(o.{c} AS BIGNUMERIC), 0)), SUM(IF(o.day IS NULL, CAST(n.{c} AS BIGNUMERIC), 0)) FROM j"
        for c in NUMERIC)
    return (body
            + "\n-- СВЕРКА С ЖИВЫМ СЛОЕМ (только чтение)\n"
            + "ASSERT (SELECT STRING_AGG(column_name, ',' ORDER BY column_name) FROM `project-fa311fc0-4d87-4781-986.wb_mart.INFORMATION_SCHEMA.COLUMNS`\n"
            + "        WHERE table_name = 'SKU_PERFORMANCE_V2_DAILY' AND data_type IN ('NUMERIC', 'INT64') AND column_name != 'nm_id')\n"
            + f"     = (SELECT STRING_AGG(c, ',' ORDER BY c) FROM UNNEST([{cols}]) c)\n"
            + "  AS 'RECON: числовые поля живого слоя не совпадают со списком сверки';\n"
            + f"CREATE TEMP TABLE j AS SELECT o, n FROM {LAYER} o FULL JOIN b n ON o.day = n.day AND o.nm_id = n.nm_id;\n"
            + "SELECT 'keys' section, k item, FORMAT('%d %t..%t', nm, MIN(day), MAX(day)) detail,\n"
            + "       COUNT(*) common_rows_diff, CAST(NULL AS BIGNUMERIC) common_delta, CAST(NULL AS BIGNUMERIC) removed_sum,\n"
            + "       CAST(NULL AS BIGNUMERIC) added_sum\n"
            + "FROM (SELECT CASE WHEN o.day IS NULL THEN 'ADDED' WHEN n.day IS NULL THEN 'REMOVED' ELSE 'COMMON' END k,\n"
            + "             COALESCE(o.nm_id, n.nm_id) nm, COALESCE(o.day, n.day) day FROM j)\n"
            + "GROUP BY k, nm\nUNION ALL\n"
            + money + "\nORDER BY 1, 2, 3;\n")


if __name__ == "__main__":
    text = SQL.read_text(encoding="utf-8")
    sys.stdout.write(recon(text) if "--recon" in sys.argv[1:] else build(text))
