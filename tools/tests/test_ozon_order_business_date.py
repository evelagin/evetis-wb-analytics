"""Регрессия 2026-10-06: метрики заказов Ozon группируются по бизнес-дате МСК, а не по UTC.

RAW_OZON_POSTINGS_FBO.order_date — это UTC-дата created_at (загрузчик берёт created_at[:10]).
Ozon Seller Analytics («Заказано товаров») считает сутки по Москве, поэтому заказ 00:00–02:59 МСК
с UTC-датой уезжал в предыдущие сутки (05.10.2026: Ozon 10, Юнитка 11). Канон — одно выражение
DATE(created_at, 'Europe/Moscow'); поведение проверяется исполнением SQL в
test_ozon_unitka_model_sql.py, здесь — что никто не читает RAW order_date как дату заказа.
"""
from pathlib import Path
import re

import sqlglot
from sqlglot import exp

ROOT = Path(__file__).resolve().parents[2]
RAW = "RAW_OZON_POSTINGS_FBO"
BUSINESS_DATE = "DATE(created_at, 'Europe/Moscow')"
# Не экономика заказа: свежесть источника (максимум UTC-даты загрузки — происхождение, а не сутки бизнеса).
LINEAGE_ONLY = {"V_OZON_MART_FRESHNESS"}


def _raw_order_date_reads(sql: str) -> list[str]:
    """Колонки order_date, которые SELECT берёт прямо из RAW_OZON_POSTINGS_FBO (не из CTE-производной)."""
    body = sql.split("\nAS\n", 1)[1]
    tree = sqlglot.parse_one(body, read="bigquery")
    hits = []
    for select in tree.find_all(exp.Select):
        sources = {}
        for table in select.find_all(exp.Table):
            if table.find_ancestor(exp.Select) is select:
                sources[table.alias_or_name] = table.name
        raw_aliases = {a for a, n in sources.items() if n == RAW}
        if not raw_aliases:
            continue
        for col in select.find_all(exp.Column):
            if col.find_ancestor(exp.Select) is not select or col.name != "order_date":
                continue
            if col.table in raw_aliases or (not col.table and len(sources) == 1):
                hits.append(col.sql(dialect="bigquery"))
    return hits


def test_canonical_ozon_views_derive_moscow_business_date_from_created_at():
    checked = []
    for path in sorted((ROOT / "sql/current/ozon_mart").glob("*.sql")):
        sql = path.read_text()
        if RAW not in sql or path.stem in LINEAGE_ONLY:
            continue
        checked.append(path.stem)
        assert _raw_order_date_reads(sql) == [], f"{path.stem}: RAW order_date (UTC) читается как дата заказа"
        if "order_date" in sql.split("\nAS\n", 1)[1]:
            normalized = re.sub(r"\b\w+\.created_at\b", "created_at", sql)
            assert BUSINESS_DATE in normalized, f"{path.stem}: дата заказа не выведена из created_at по МСК"
    assert {"FCT_OZON_SKU_PNL_DAILY", "FCT_OZON_SKU_PNL_MONTHLY", "FCT_OZON_PNL_MONTHLY",
            "V_OZON_SKU_PNL_DAILY_OPERATIONAL", "V_OZON_COMMISSION_POLICY",
            "V_OZON_LOGISTICS_ESTIMATOR", "V_OZON_SKU_FORWARD_ECONOMICS_CURRENT"} <= set(checked)


def test_detector_catches_a_raw_utc_order_date():
    bad = "x\nAS\nSELECT p.order_date d, COUNT(*) FROM `p.ozon_raw.RAW_OZON_POSTINGS_FBO` p GROUP BY 1"
    good = ("x\nAS\nWITH post AS (SELECT DATE(p.created_at, 'Europe/Moscow') order_date "
            "FROM `p.ozon_raw.RAW_OZON_POSTINGS_FBO` p) SELECT order_date FROM post")
    assert _raw_order_date_reads(bad) == ["p.order_date"]
    assert _raw_order_date_reads(good) == []


def test_unitka_loader_sql_uses_the_single_business_date_constant():
    text = (ROOT / "cloud/src/loaders/unitka/ozon/bq.ts").read_text()
    assert "export const OZON_ORDER_BUSINESS_DATE_SQL = \"DATE(p.created_at, 'Europe/Moscow')\";" in text
    code = re.sub(r"/\*[\s\S]*?\*/|//[^\n]*|--[^\n]*", "", text)
    assert not re.search(r"\border_date\b", code), "SQL загрузчика читает RAW order_date (UTC)"
    assert text.count("${OZON_ORDER_BUSINESS_DATE_SQL}") >= 8


# ── Control Tower — сопутствующее изменение (wb_mart, 2026-10-06) ───────────────────────────────
CT = ROOT / "sql/control_tower"
CT_MIGRATION = CT / "ct_ozon_order_date_msk_2026-10-06.sql"


def _ct_views(path: Path) -> dict[str, str]:
    """{имя: тело} для CREATE OR REPLACE VIEW `….wb_mart.<имя>` AS … ; в файле Control Tower."""
    text = path.read_text()
    out = {}
    for m in re.finditer(r"CREATE OR REPLACE VIEW `[^`]+\.wb_mart\.(\w+)`\s+(?:OPTIONS \(description = \"(?:\\.|[^\"\\])*\"\)\s+)?AS\s*\n", text):
        rest = text[m.end():]
        end = re.search(r";\s*(\n|$)", rest)
        out[m.group(1)] = rest[:end.start()].strip()
    return out


def test_control_tower_ozon_rows_use_the_same_business_date():
    base = {**_ct_views(CT / "ct_04a_views_base.sql"), **_ct_views(CT / "ct_04b_views_owner.sql")}
    migration = _ct_views(CT_MIGRATION)
    assert set(migration) == {"V_CT_ACTUAL_DAILY_LIVE", "V_CT_PLAN_VS_ACTUAL_DAILY"}
    for name, body in migration.items():
        assert base[name] == body, f"{name}: миграция и базовый файл CT разошлись"
        assert _raw_order_date_reads("x\nAS\n" + body) == [], f"{name}: CT читает RAW order_date (UTC)"
        assert BUSINESS_DATE in re.sub(r"\b\w+\.created_at\b", "created_at", body), name
    # Выручка CT приходит из FCT по сутки × SKU — строки CT обязаны стоять на тех же сутках.
    assert "FCT_OZON_SKU_PNL_DAILY" in migration["V_CT_ACTUAL_DAILY_LIVE"]


def test_control_tower_freshness_stays_on_source_utc_semantics():
    """V_CT_FRESHNESS.OZON_SALES — свежесть загрузки: возраст = CURRENT_DATE() (UTC) − дата, SLA 1 сутки."""
    body = _ct_views(CT / "ct_04a_views_base.sql")["V_CT_FRESHNESS"]
    assert "(SELECT MAX(order_date) FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_POSTINGS_FBO`)" in body
    assert "DATE_DIFF(CURRENT_DATE(), data_as_of, DAY)" in body


def test_deploy_tool_order_bodies_and_rollback_are_exact():
    """Инструмент развёртывания: порядок графа, тела = канон/миграция CT, откат = тела production до изменения."""
    import json
    import sys
    sys.path.insert(0, str(ROOT / "tools"))
    import ozon_order_date_msk_deploy as dep
    from validate_current_sql import sha256_text
    man = {o["object_name"]: o for o in json.loads((ROOT / "sql/current/ozon_mart/MANIFEST.json").read_text())["objects"]}
    forward = dep.plan(False)
    names = [n for _, n, _, _ in forward]
    assert names.index("FCT_OZON_SKU_PNL_DAILY") < names.index("V_OZON_SKU_PNL_DAILY_OPERATIONAL") < names.index("V_CT_ACTUAL_DAILY_LIVE")
    assert names.index("V_OZON_COMMISSION_POLICY") < names.index("V_OZON_SKU_PNL_DAILY_OPERATIONAL")
    ct = _ct_views(CT_MIGRATION)
    for ds, n, _, body in forward:
        if ds == "ozon_mart":
            assert sha256_text(body) == man[n]["canonical_body_sha256"], n
        else:
            assert body == ct[n], n
    # откат ozon_mart = тела production: снимок R2C, а у операционного представления — развёрнутое 2026-10-06 тело #256
    prod_operational = "44f97ecc0c457090fb50b7bfee2cf3337a407d1a072b5ab8ece69acd4e419d0c"
    for ds, n, _, body in dep.plan(True):
        if ds == "ozon_mart":
            want = prod_operational if n == "V_OZON_SKU_PNL_DAILY_OPERATIONAL" else man[n]["live_body_sha256_at_capture"]
            assert sha256_text(body) == want, n
            assert "DATE(p.created_at, 'Europe/Moscow')" not in body and "DATE(created_at, 'Europe/Moscow')" not in body, n
        else:
            assert BUSINESS_DATE not in re.sub(r"\b\w+\.created_at\b", "created_at", body), n
