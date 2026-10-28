"""SKU Performance V2 · S6 хранение: исполнение настоящих ASSERT сборки на SQLite-фикстурах.

Инцидент 19–28.10.2026: nm 909951444 хранился с 17.10 (2,2542 ₽), а его календарь в MART_SKU_DAILY
начинается с 18.10. Прежняя итоговая S6 сравнивала Σ слоя с Σ отчёта по всем дням ≤ конца окна и
роняла каждую сборку. Новая S6: (а) точное равенство на парах слоя; (б) внутри календаря SKU
ни один покрытый SKU-день хранения не выпадает из слоя.

Из файла берутся тексты обоих ASSERT; заменяются только идентификаторы таблиц.
"""
from pathlib import Path
import re
import sqlite3

import pytest
import sqlglot

ROOT = Path(__file__).resolve().parents[2]
SQL = (ROOT / "sql" / "dash" / "sku_performance_v2_daily_v1.sql").read_text(encoding="utf-8")
P = "project-fa311fc0-4d87-4781-986"


def s6_storage_asserts():
    out = {}
    for m in re.finditer(r"\n\s*AS '(SKU_V2 S6: хранение[^']*)'", SQL):
        start = SQL.rindex("ASSERT ", 0, m.start()) + len("ASSERT ")
        out[m.group(1)] = SQL[start:m.start()].strip()
    return out


def run(expr, b_rows, storage_rows, coverage_rows, events=None):
    sql = (expr.replace(f"`{P}.wb_raw.V_WB_STORAGE_DAILY`", "storage_daily")
               .replace(f"`{P}.wb_raw.V_WB_STORAGE_COVERAGE`", "storage_coverage"))
    for t in ("FACT_ORDERS", "FACT_SALES", "FACT_FINANCE", "FACT_ADS_SKU_DAILY"):
        sql = sql.replace(f"`{P}.wb_mart.{t}`", t.lower())
    lite = sqlglot.transpile(f"SELECT {sql}", read="bigquery", write="sqlite")[0]
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE b (day TEXT, nm_id INTEGER, storage_sku_rub REAL)")
    db.execute("CREATE TABLE storage_daily (date_msk TEXT, nm_id INTEGER, storage_rub_exact REAL)")
    db.execute("CREATE TABLE storage_coverage (date_msk TEXT, status TEXT)")
    db.executemany("INSERT INTO b VALUES (?, ?, ?)", b_rows)
    db.executemany("INSERT INTO storage_daily VALUES (?, ?, ?)", storage_rows)
    db.executemany("INSERT INTO storage_coverage VALUES (?, ?)", coverage_rows)
    db.execute("CREATE TABLE fact_orders (order_date TEXT, nm_id INTEGER)")
    db.execute("CREATE TABLE fact_sales (sale_date TEXT, nm_id INTEGER)")
    db.execute("CREATE TABLE fact_finance (finance_date TEXT, nm_id INTEGER)")
    db.execute("CREATE TABLE fact_ads_sku_daily (date TEXT, nm_id INTEGER)")
    for table, rows in (events if events is not None else EVENTS).items():
        db.executemany(f"INSERT INTO {table} VALUES (?, ?)", rows)
    return bool(db.execute(lite).fetchone()[0])


COV = [(d, "OK") for d in ("2026-10-16", "2026-10-17", "2026-10-18", "2026-10-19")]
OLD_SKU, NEW_SKU = 252442517, 909951444
# Хранение: старый SKU каждый день (по двум складам), новый — с 17.10, хотя в MART он с 18.10.
STORAGE = [
    ("2026-10-16", OLD_SKU, 10.0), ("2026-10-16", OLD_SKU, 1.5),
    ("2026-10-17", OLD_SKU, 10.0), ("2026-10-17", NEW_SKU, 2.2542),
    ("2026-10-18", OLD_SKU, 10.0), ("2026-10-18", NEW_SKU, 2.31),
    ("2026-10-19", OLD_SKU, 9.0),  ("2026-10-19", NEW_SKU, 2.4),
]
LAYER = [
    ("2026-10-16", OLD_SKU, 11.5), ("2026-10-17", OLD_SKU, 10.0), ("2026-10-18", OLD_SKU, 10.0),
    ("2026-10-19", OLD_SKU, 9.0), ("2026-10-18", NEW_SKU, 2.31), ("2026-10-19", NEW_SKU, 2.4),
]
# События MART-источников: старый SKU продаётся каждый день; у нового первый заказ 18.10 (= начало календаря),
# 17.10 у него только приёмка на склад, поэтому хранение 17.10 — единственный «сирота до календаря».
EVENTS = {
    "fact_orders": [(d, OLD_SKU) for d in ("2026-10-16", "2026-10-17", "2026-10-18", "2026-10-19")] + [("2026-10-18", NEW_SKU)],
    "fact_sales": [],
    "fact_finance": [("2026-10-19", 0)],   # строка уровня счёта (nm_id = 0) не событие SKU
    "fact_ads_sku_daily": [("2026-10-19", NEW_SKU)],
}
PAIRS = "SKU_V2 S6: хранение по SKU разошлось с отчётом платного хранения"
CALENDAR = "SKU_V2 S6: хранение SKU внутри его календаря не попало в слой"


def test_both_storage_asserts_present_and_old_total_form_gone():
    a = s6_storage_asserts()
    assert set(a) == {PAIRS, CALENDAR}
    # Прежняя форма: Σ отчёта по nm ∈ b и дням ≤ MAX(day) — без привязки к парам слоя.
    assert "WHERE s.nm_id IN (SELECT nm_id FROM b) AND s.date_msk <= (SELECT MAX(day) FROM b))" not in SQL


def test_sku_stored_before_its_calendar_passes_without_losing_money():
    a = s6_storage_asserts()
    assert run(a[PAIRS], LAYER, STORAGE, COV)
    assert run(a[CALENDAR], LAYER, STORAGE, COV)
    # 2,2542 ₽ не исчезает: это ровно разница отчёта и слоя, и она — до календаря SKU.
    total = sum(r for _, _, r in STORAGE)
    in_layer = sum(r for _, _, r in LAYER)
    assert round(total - in_layer, 6) == 2.2542


def test_missing_sku_day_inside_calendar_still_fails():
    gap = [r for r in LAYER if not (r[0] == "2026-10-19" and r[1] == NEW_SKU)]
    a = s6_storage_asserts()
    assert not run(a[CALENDAR], gap, STORAGE, COV)


def test_missing_first_calendar_day_of_new_sku_still_fails():
    # Если слой потерял сам первый день календаря, «начало» сдвигается — ловит независимый якорь событий.
    gap = [r for r in LAYER if not (r[0] == "2026-10-18" and r[1] == NEW_SKU)]
    assert not run(s6_storage_asserts()[CALENDAR], gap, STORAGE, COV)


def test_pre_calendar_storage_with_any_sku_event_fails():
    ev = dict(EVENTS)
    ev["fact_finance"] = EVENTS["fact_finance"] + [("2026-10-17", NEW_SKU)]
    assert not run(s6_storage_asserts()[CALENDAR], LAYER, STORAGE, COV, ev)


def test_missing_old_sku_day_still_fails():
    gap = [r for r in LAYER if not (r[0] == "2026-10-17" and r[1] == OLD_SKU)]
    assert not run(s6_storage_asserts()[CALENDAR], gap, STORAGE, COV)


def test_wrong_amount_on_layer_pair_still_fails():
    wrong = [(d, n, v + 0.01 if (d, n) == ("2026-10-19", OLD_SKU) else v) for d, n, v in LAYER]
    assert not run(s6_storage_asserts()[PAIRS], wrong, STORAGE, COV)


def test_uncovered_storage_day_is_not_required_in_layer():
    cov = [c for c in COV if c[0] != "2026-10-19"]
    layer = [(d, n, None if d == "2026-10-19" else v) for d, n, v in LAYER]
    a = s6_storage_asserts()
    assert run(a[PAIRS], layer, STORAGE, cov)
    assert run(a[CALENDAR], layer, STORAGE, cov)


def test_unattributed_storage_view_declares_reasons():
    m = re.search(r"CREATE OR REPLACE VIEW `[^`]*V_SKU_PERFORMANCE_V2_STORAGE_UNATTRIBUTED`(.*?)WHERE l\.nm_id IS NULL;", SQL, re.S)
    assert m, "view V_SKU_PERFORMANCE_V2_STORAGE_UNATTRIBUTED не найден"
    for reason in ("BEFORE_SKU_CALENDAR", "NM_OUTSIDE_SKU_UNIVERSE", "AFTER_LAYER_WINDOW", "MISSING_SKU_DAY"):
        assert reason in m.group(1)


@pytest.mark.parametrize("msg", [PAIRS, CALENDAR])
def test_asserts_stay_inside_build_procedure(msg):
    proc = SQL[SQL.index("CREATE OR REPLACE PROCEDURE"):SQL.index("\nEND;", SQL.index("CREATE OR REPLACE PROCEDURE"))]
    assert msg in proc


def test_dry_build_sql_has_no_permanent_writes_and_all_asserts():
    import importlib.util
    spec = importlib.util.spec_from_file_location("dry", ROOT / "tools" / "sku_performance_v2" / "dry_build_sql.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    out = mod.build(SQL)
    for w in mod.WRITES:
        assert w not in out
    stmt = re.compile(r"^\s+ASSERT ", re.M)
    body = SQL[SQL.index("CREATE TEMP TABLE b AS"):SQL.index("SET v_rows")]
    assert len(stmt.findall(out)) == len(stmt.findall(body)) >= 10  # S1–S6, включая обе проверки хранения
    assert PAIRS in out and CALENDAR in out


def test_recon_is_read_only_and_covers_every_numeric_field():
    import importlib.util
    spec = importlib.util.spec_from_file_location("dry", ROOT / "tools" / "sku_performance_v2" / "dry_build_sql.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    out = mod.recon(SQL)
    for w in mod.WRITES:
        assert w not in out
    assert "INFORMATION_SCHEMA.COLUMNS" in out  # список полей сверяется со схемой живого слоя
    for c in mod.NUMERIC:
        assert f"SELECT 'money', '{c}'," in out
    assert {"credited_for_goods_rub", "storage_sku_rub", "cogs_rub", "contribution_after_cogs_rub"} <= set(mod.NUMERIC)
    assert len(mod.NUMERIC) == len(set(mod.NUMERIC)) == 52
