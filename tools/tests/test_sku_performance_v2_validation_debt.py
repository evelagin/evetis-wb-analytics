"""SKU Performance V2 · долг приёмки после выпуска 29.10.2026 (B2-D1…D3).

V9.provisional_window была привязана к датам 01–17.09.2026 и падала на любом слое с 22.09 (UBR-011).
V4.cohort сравнивала слой со ЖИВОЙ когортой Executive: продажа, загруженная между сборкой и проверкой,
давала ложное расхождение (29.10: nm 535581674, загрузка 15:22:59Z при старте сборки 15:20:30Z).
Цены набора вне календаря SKU (909951444: 41 сутки, 2 907 наблюдений) ничем не проверялись.
"""
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from lib.acceptance import parse_assert_script  # noqa: E402

PATH = "sql/dash/sku_performance_v2_daily_validation.sql"
TEXT = (ROOT / PATH).read_text(encoding="utf-8")
SALES_VIEW = (ROOT / "apps-script/ingestion/WbSalesReturnsBigQuery.gs").read_text(encoding="utf-8")


def block(name: str) -> str:
    start = TEXT.index(f"-- {name}\n")
    nxt = re.compile(r"\n-- V[\w.]+\n").search(TEXT, start + 1)
    return TEXT[start:] if nxt is None else TEXT[start:nxt.start()]


def checks() -> dict[str, object]:
    return {c.label.split(":")[0]: c for c in parse_assert_script(TEXT, PATH, "SKUV2").checks}


def test_every_statement_is_an_executable_assert():
    assert all(c.executable for c in checks().values())


def test_check_ids_of_rewritten_checks_are_stable():
    # Реестр и UBR ссылаются на id — переписанная проверка остаётся той же проверкой.
    ids = {k: c.check_id for k, c in checks().items()}
    assert ids["V4.cohort"] == "SKUV2__V4_COHORT_SKU_EXECUTIVE_V2"
    assert ids["V9.provisional_window"] == "SKUV2__V9_PROVISIONAL_WINDOW_FINAL"


def test_v9_window_comes_from_data_not_dates():
    v9 = block("V9.provisional_window")
    assert not re.search(r"DATE '\d{4}-\d{2}-\d{2}'", v9)
    assert "WHERE NOT IFNULL(finance_is_final, FALSE)" in v9
    assert "LOGICAL_AND(data_status <> 'FINAL')" in v9
    # окончательная сторона по-прежнему проверяется своей проверкой
    assert "V9.final_window_is_final" in checks()


def test_v4_cohort_cuts_sales_at_build_start_and_has_no_tolerance():
    v4 = block("V4.cohort")
    assert "<= (SELECT MAX(started_at) FROM `project-fa311fc0-4d87-4781-986.wb_mart.SKU_PERFORMANCE_V2_BUILD_LOG`" \
           " WHERE status = 'SUCCESS')" in v4
    assert "PARSE_TIMESTAMP('%Y-%m-%d %H:%M:%S', loaded_at, 'Europe/Moscow')" in v4   # loaded_at — строка МСК
    assert ") = (0\n) AS 'V4.cohort:" in v4                                        # любое расхождение — FAIL
    assert "ABS(" not in v4 and "<= 1" not in v4


def test_v4_cohort_reads_orders_as_of_the_mart_run_of_its_build():
    # живая FACT_ORDERS могла быть пересобрана после сборки слоя (02.11: 09:33Z против 09:20Z)
    v4 = block("V4.cohort")
    assert "wb_mart.FACT_ORDERS`" not in v4
    assert "wb_raw.RAW_WB_ORDERS`" in v4 and "wb_mart.MART_RUNS` r" in v4
    assert "SELECT mart_built_at FROM `project-fa311fc0-4d87-4781-986.wb_mart.SKU_PERFORMANCE_V2_BUILD_LOG`" in v4
    assert "LEFT JOIN (SELECT o.order_date AS cohort_date" in v4                     # сутки без заказов не выпадают


def test_v4_orders_dedupe_matches_v_wb_orders():
    # V_WB_ORDERS (wb_raw, живое определение 02.11.2026) — грейн FACT_ORDERS
    for name in ("V4.cohort", "V4.orders_method"):
        b = block(name)
        for frag in ("PARTITION BY COALESCE(NULLIF(srid, ''), row_hash)",
                     "ORDER BY SAFE_CAST(last_change_date AS TIMESTAMP) DESC",
                     "SAFE_CAST(loaded_at AS TIMESTAMP) DESC, load_id DESC",
                     "WHERE source_api = 'WB_API_ORDERS'"):
            assert frag in b, (name, frag)


def test_v4_cohort_method_is_proven_equal_to_executive_view():
    m = block("V4.cohort_method")
    assert "FULL JOIN `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_BUYOUT_COHORT_DAILY` e USING (cohort_date)" in m
    assert "loaded_at, 'Europe/Moscow')" not in m                                   # без среза
    for col in ("cohort_orders", "buyout_orders", "cancelled_orders", "unresolved_orders", "conflict_orders"):
        assert f"r.{col} IS DISTINCT FROM e.{col}" in m


def test_v4_sales_dedupe_matches_v_wb_sales_returns():
    # Срез воспроизводит ЖИВУЮ V_WB_SALES_RETURNS (её читает сборка слоя): те же фильтры и порядок «последний
    # выигрывает». В Git (Apps Script) у вью есть ещё последний ключ TO_HEX(MD5(raw_json)), в production его нет
    # (сверка 02.11.2026) — V4 следует production; ничьих, меняющих исход, на 02.11 ноль.
    v4 = block("V4.cohort")
    for frag in ("PARTITION BY sale_id",
                 "ORDER BY SAFE_CAST(REPLACE(last_change_date, 'T', ' ') AS TIMESTAMP) DESC",
                 "SAFE_CAST(loaded_at AS TIMESTAMP) DESC, load_id DESC",
                 "processed_status <> 'MISSING_EVENT_KEY'"):
        assert frag in v4 and frag in SALES_VIEW, frag
    assert "MD5" not in v4


def test_price_checks_cover_pairs_and_calendar():
    pairs, cal = block("V10.price_pairs"), block("V10.price_calendar")
    assert "IFNULL(t.set_price_observations, 0) <> IFNULL(p.n, 0)" in pairs
    assert "WHERE k.nm_id IS NULL AND p.day BETWEEN f.first_day AND f.last_day" in cal


# ── Исполнение V4 на синтетических строках (sqlite) ──────────────────────────────────────────────────────────────
# Негативные сценарии рецензии #393: поздняя продажа, поздний заказ, поздняя отмена. Проверка не должна падать
# от данных, которых сборка слоя видеть не могла, и обязана падать, когда слой расходится с тем, что видел.
import sqlite3  # noqa: E402
from datetime import datetime, timedelta  # noqa: E402

import sqlglot  # noqa: E402
from sqlglot import exp  # noqa: E402

COLS = {
    "SKU_PERFORMANCE_V2_DAILY": ["day", "nm_id", "orders_covered", "cohort_orders", "cohort_buyout_orders",
                                 "cohort_cancelled_orders", "cohort_unresolved_orders", "cohort_conflict_orders",
                                 "orders_gross_units", "orders_cancelled_units", "orders_gross_seller_price_rub",
                                 "orders_cancelled_seller_price_rub"],
    "SKU_PERFORMANCE_V2_BUILD_LOG": ["started_at", "status", "mart_built_at"],
    "MART_RUNS": ["run_id", "environment", "status", "started_at"],
    "RAW_WB_ORDERS": ["source_api", "srid", "row_hash", "_order_date", "wb_nm_id", "is_cancel",
                      "last_change_date", "loaded_at", "load_id", "price_with_disc"],
    "RAW_WB_SALES_RETURNS": ["source_api", "sale_id", "srid", "is_return", "last_change_date", "loaded_at",
                             "load_id", "processed_status"],
    "FACT_ORDERS": ["order_srid", "order_date", "nm_id", "is_cancel", "mart_run_id", "price_with_disc"],
    "REF_SKU_MASTER": ["nm_id", "marketplace", "active"],
}
NM = 535581674
DAY = "2026-10-19"


def sqlite_check(label: str) -> str:
    q = sqlglot.parse_one(checks()[label].sql, read="bigquery")
    for t in q.find_all(exp.Table):
        t.set("catalog", None)
        t.set("db", None)
    for n in list(q.find_all(exp.StrToTime)):          # PARSE_TIMESTAMP(fmt, loaded_at, 'Europe/Moscow')
        assert n.args["zone"].name == "Europe/Moscow", n.sql()
        n.replace(exp.Anonymous(this="MSK_TO_UTC", expressions=[n.this]))
    for n in list(q.find_all(exp.TryCast)):            # SAFE_CAST(… AS TIMESTAMP/INT64)
        if n.to.this in (exp.DataType.Type.TIMESTAMP, exp.DataType.Type.TIMESTAMPTZ):
            n.replace(exp.Anonymous(this="TS", expressions=[n.this]))
    return q.sql(dialect="sqlite")


def run_check(label: str, rows: dict[str, list[tuple]]) -> str:
    db = sqlite3.connect(":memory:")
    db.create_function("MSK_TO_UTC", 1, lambda s: (datetime.fromisoformat(s) - timedelta(hours=3)).isoformat(" "))
    db.create_function("TS", 1, lambda s: None if s is None else datetime.fromisoformat(s.replace("T", " ")).isoformat(" "))
    for name, cols in COLS.items():
        db.execute(f"CREATE TABLE {name} ({', '.join(cols)})")
        db.executemany(f"INSERT INTO {name} VALUES ({', '.join('?' * len(cols))})", rows.get(name, []))
    return db.execute(sqlite_check(label)).fetchone()[0]


def order(srid, loaded_utc, cancel="false", lcd="2026-10-19T10:00:00", load="o1"):
    msk = (datetime.fromisoformat(loaded_utc) + timedelta(hours=3)).isoformat(" ")
    return ("WB_API_ORDERS", srid, "h" + srid, DAY, str(NM), cancel, lcd, msk, load, "100,0")


def sale(srid, loaded_utc, load="s1"):
    msk = (datetime.fromisoformat(loaded_utc) + timedelta(hours=3)).isoformat(" ")
    return ("WB_API_SALES", "S" + srid, srid, 0, "2026-10-20T10:00:00", msk, load, "OK")


def world(layer, raw_orders, raw_sales, fact=None, orders=(2, 0, 200, 0)):
    """Витрина 09:33Z (run m1) → слой 09:20Z прочитал витрину 30.10 (run m0) — классическое окно 02.11."""
    return {
        "SKU_PERFORMANCE_V2_DAILY": [(DAY, NM, 1, *layer, *orders)],
        "SKU_PERFORMANCE_V2_BUILD_LOG": [("2026-11-02 09:20:03", "SUCCESS", "2026-10-30 06:03:38")],
        "MART_RUNS": [("m0", "prod", "COMPLETE", "2026-10-30 06:00:30"),
                      ("m1", "prod", "COMPLETE", "2026-11-02 09:33:28")],
        "RAW_WB_ORDERS": raw_orders, "RAW_WB_SALES_RETURNS": raw_sales,
        "FACT_ORDERS": fact or [], "REF_SKU_MASTER": [(NM, "WB", 1)],
    }


BASE_ORDERS = [order("a", "2026-10-19 12:00:00"), order("b", "2026-10-19 12:00:00")]
BASE_SALES = [sale("a", "2026-10-21 12:00:00")]
BASE_LAYER = (2, 1, 0, 1, 0)        # заказы, выкуп, отмена, неразрешённые, конфликт


def test_v4_sqlite_baseline_passes():
    assert run_check("V4.cohort", world(BASE_LAYER, BASE_ORDERS, BASE_SALES)) == "PASS"


def test_v4_late_sale_after_build_start_is_not_a_failure():
    late = BASE_SALES + [sale("b", "2026-11-02 09:22:00")]          # после старта сборки 09:20:03Z
    assert run_check("V4.cohort", world(BASE_LAYER, BASE_ORDERS, late)) == "PASS"


def test_v4_late_order_from_later_mart_run_is_not_a_failure():
    # заказ пришёл 01.11 и попал в витрину m1 (09:33Z), а слой 09:20Z читал витрину m0
    late = BASE_ORDERS + [order("c", "2026-11-01 12:00:00", load="o2")]
    fact_m1 = [("a", DAY, NM, 0, "m1", 100), ("b", DAY, NM, 0, "m1", 100), ("c", DAY, NM, 0, "m1", 100)]
    assert run_check("V4.cohort", world(BASE_LAYER, late, BASE_SALES, fact=fact_m1)) == "PASS"   # ba62fcdb: FAIL


def test_v4_late_cancel_from_later_mart_run_is_not_a_failure():
    late = BASE_ORDERS + [order("b", "2026-11-01 12:00:00", cancel="true", lcd="2026-10-31T10:00:00", load="o2")]
    fact_m1 = [("a", DAY, NM, 0, "m1", 100), ("b", DAY, NM, 1, "m1", 100)]
    assert run_check("V4.cohort", world(BASE_LAYER, late, BASE_SALES, fact=fact_m1)) == "PASS"   # ba62fcdb: FAIL


def test_v4_order_the_build_could_see_but_layer_lost_fails():
    seen = BASE_ORDERS + [order("c", "2026-10-29 12:00:00", load="o2")]   # до старта витрины m0
    assert run_check("V4.cohort", world(BASE_LAYER, seen, BASE_SALES)) == "FAIL"


def test_v4_layer_resolving_with_a_sale_it_could_not_see_fails():
    late = BASE_SALES + [sale("b", "2026-11-02 09:22:00")]
    assert run_check("V4.cohort", world((2, 2, 0, 0, 0), BASE_ORDERS, late)) == "FAIL"


def test_v4_cancel_the_build_could_see_but_layer_missed_fails():
    seen = BASE_ORDERS + [order("b", "2026-10-29 12:00:00", cancel="true", lcd="2026-10-28T10:00:00", load="o2")]
    assert run_check("V4.cohort", world(BASE_LAYER, seen, BASE_SALES)) == "FAIL"


def test_v4_dedupe_takes_latest_change_not_latest_load():
    # отмена с более ранним last_change_date, загруженная позже, не перекрывает актуальную строку
    stale = BASE_ORDERS + [order("b", "2026-10-29 12:00:00", cancel="true", lcd="2026-10-18T10:00:00", load="o2")]
    assert run_check("V4.cohort", world(BASE_LAYER, stale, BASE_SALES)) == "PASS"


def test_v4_without_mart_run_is_fail_closed():
    w = world(BASE_LAYER, BASE_ORDERS, BASE_SALES)
    w["MART_RUNS"] = [("m1", "prod", "COMPLETE", "2026-11-02 09:33:28")]   # нет прогона, давшего mart_built_at
    assert run_check("V4.cohort", w) == "FAIL"


def fact_world(fact, raw_orders=BASE_ORDERS):
    return world(BASE_LAYER, raw_orders, BASE_SALES, fact=fact)


def test_v4_orders_method_reproduces_fact_orders_of_its_mart_run():
    fact = [("a", DAY, NM, 0, "m1", 100), ("b", DAY, NM, 0, "m1", 100), ("c", DAY, NM, 0, "m1", 100)]
    raw = BASE_ORDERS + [order("c", "2026-11-01 12:00:00", load="o2"),
                         order("d", "2026-11-02 09:40:00", load="o3")]        # после старта m1 — не в FACT
    assert run_check("V4.orders_method", fact_world(fact, raw)) == "PASS"


def test_v4_orders_method_fails_on_divergent_fact_orders():
    assert run_check("V4.orders_method", fact_world([("a", DAY, NM, 0, "m1", 100)])) == "FAIL"                  # потерян b
    assert run_check("V4.orders_method", fact_world([("a", DAY, NM, 0, "m1", 100), ("b", DAY, NM, 1, "m1", 100)])) == "FAIL"


def test_v4_orders_late_order_and_cancel_from_later_mart_run_are_not_failures():
    late = BASE_ORDERS + [order("c", "2026-11-01 12:00:00", load="o2"),
                          order("b", "2026-11-01 12:00:00", cancel="true", lcd="2026-10-31T10:00:00", load="o2")]
    fact_m1 = [("a", DAY, NM, 0, "m1", 100), ("b", DAY, NM, 1, "m1", 100), ("c", DAY, NM, 0, "m1", 100)]
    assert run_check("V4.orders", world(BASE_LAYER, late, BASE_SALES, fact=fact_m1)) == "PASS"   # ba62fcdb: FAIL


def test_v4_orders_divergence_the_build_could_see_fails():
    seen = BASE_ORDERS + [order("c", "2026-10-29 12:00:00", load="o2")]
    assert run_check("V4.orders", world(BASE_LAYER, seen, BASE_SALES)) == "FAIL"                      # потерян заказ
    assert run_check("V4.orders", world(BASE_LAYER, BASE_ORDERS, BASE_SALES, orders=(2, 0, 199, 0))) == "FAIL"


def test_v4_orders_method_fails_on_price_divergence():
    fact = [("a", DAY, NM, 0, "m1", 100), ("b", DAY, NM, 0, "m1", 101)]
    assert run_check("V4.orders_method", fact_world(fact)) == "FAIL"
