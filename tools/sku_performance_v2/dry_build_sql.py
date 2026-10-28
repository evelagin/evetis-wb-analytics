#!/usr/bin/env python3
"""Печатает SQL «сухой сборки» SKU Performance V2: тело процедуры до записи (TEMP b + все ASSERT S1–S6) и сводку.

Release gate перед развёртыванием процедуры: выполнить вывод `bq query` — постоянные объекты не меняются
(журнал, замок и таблица слоя не трогаются; TEMP-таблица живёт в сессии скрипта). Если тело содержит
запись в постоянный объект — скрипт отказывается печатать.

    python3 tools/sku_performance_v2/dry_build_sql.py | bq query --nouse_legacy_sql --maximum_bytes_billed=30000000000
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


if __name__ == "__main__":
    sys.stdout.write(build(SQL.read_text(encoding="utf-8")))
