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
    nxt = TEXT.find("\n-- V", start + 1)
    return TEXT[start:] if nxt == -1 else TEXT[start:nxt]


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


def test_v4_cohort_method_is_proven_equal_to_executive_view():
    m = block("V4.cohort_method")
    assert "FULL JOIN `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_BUYOUT_COHORT_DAILY` e USING (cohort_date)" in m
    assert "loaded_at, 'Europe/Moscow')" not in m                                   # без среза
    for col in ("cohort_orders", "buyout_orders", "cancelled_orders", "unresolved_orders", "conflict_orders"):
        assert f"r.{col} IS DISTINCT FROM e.{col}" in m


def test_v4_sales_dedupe_matches_v_wb_sales_returns():
    # Срез воспроизводит V_WB_SALES_RETURNS: те же фильтры и тот же порядок «последний выигрывает».
    v4 = block("V4.cohort")
    for frag in ("PARTITION BY sale_id",
                 "ORDER BY SAFE_CAST(REPLACE(last_change_date, 'T', ' ') AS TIMESTAMP) DESC",
                 "SAFE_CAST(loaded_at AS TIMESTAMP) DESC, load_id DESC",
                 "processed_status <> 'MISSING_EVENT_KEY'"):
        assert frag in v4 and frag in SALES_VIEW, frag


def test_price_checks_cover_pairs_and_calendar():
    pairs, cal = block("V10.price_pairs"), block("V10.price_calendar")
    assert "IFNULL(t.set_price_observations, 0) <> IFNULL(p.n, 0)" in pairs
    assert "WHERE k.nm_id IS NULL AND p.day BETWEEN f.first_day AND f.last_day" in cal
