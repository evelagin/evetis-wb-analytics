"""PR-PROMO-2 — офлайн-тесты контракта канонического слоя состояния акций.

Без сети, без учётных данных, без BigQuery. Эти тесты охраняют КОНТРАКТ в Git: изоляцию
площадок, отсутствие экономики и ПДн, воспроизводимость истории, отсутствие нечёткой
связи по названию, порядок приоритета состояний, полноту регрессионных сценариев.

Семантику на данных проверяют два исполнимых набора (read-only, в BigQuery):
  - регрессионные сценарии на фикстурах: python tools/promo_canonical_render.py run fixtures …
  - проверки production: sql/promotions/pr_promo2_canonical_validation.sql
Здесь проверяется, что оба набора собираются, разбираются и не могут ничего изменить.
"""
import json
import re
import sys
from pathlib import Path

import pytest
import sqlglot
from sqlglot import exp

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools"))
import promo_canonical_deploy as deploy  # noqa: E402
import promo_canonical_render as r  # noqa: E402
import validate_current_sql as v  # noqa: E402
from lib.bq_readonly import assert_read_only  # noqa: E402

P = r.PROJECT
VALIDATION = REPO / "sql/promotions/pr_promo2_canonical_validation.sql"
ROLLBACK = REPO / "sql/promotions/pr_promo2_rollback.sql"
DOC = REPO / "docs/promotions/PR_PROMO_2_CANONICAL_STATE_2026-09-22.md"

ALLOWED_READS = {
    "wb_mart": {"wb_raw", "wb_mart", "evetis_ref"},
    "ozon_mart": {"ozon_raw", "ozon_mart", "evetis_ref"},
    "evetis_mart": {"wb_mart", "ozon_mart", "evetis_mart"},
}
PII = re.compile(r"customer|first_name|last_name|patronymic|email|phone|user_comment|address|buyer", re.I)
ECONOMICS = re.compile(r"contribution|margin|cogs|uplift|drr|profit|commission|logistic|tax|recommend|decision|"
                       r"enter|exit|stay|pass_|fail_|attractiv|admissib|score", re.I)
UNRESOLVED_SEMANTICS = {"stock", "min_stock", "price_min_elastic_rub", "price_max_elastic_rub",
                        "alert_max_action_price_rub", "alert_max_action_price_failed", "action_value_num"}


def facts(dataset, name):
    findings = []
    f = v.analyze_sql(r.object_path(dataset, name).read_text(encoding="utf-8"), name, findings)
    assert findings == [], findings
    return f


@pytest.fixture(scope="module")
def all_facts():
    return {(d, n): facts(d, n) for d, n, _ in r.OBJECTS}


def test_sixteen_objects_each_one_create_view_of_its_own_name(all_facts):
    assert len(r.OBJECTS) == 16
    for (d, n), f in all_facts.items():
        assert f.target == (P, d, n), (d, n)
        assert f.description and "PR-PROMO-2" in f.description
        # deploy.statement() повторяет свои проверки: ровно один CREATE, нет иных изменений
        assert deploy.statement(d, n)


def test_marketplace_isolation(all_facts):
    for (d, n), f in all_facts.items():
        read = {ds for _, ds, _ in f.references}
        assert read <= ALLOWED_READS[d], (d, n, read - ALLOWED_READS[d])
    wb = {ds for (d, _), f in all_facts.items() if d == "wb_mart" for _, ds, _ in f.references}
    oz = {ds for (d, _), f in all_facts.items() if d == "ozon_mart" for _, ds, _ in f.references}
    assert not any(ds.startswith("ozon_") for ds in wb)
    assert not any(ds.startswith("wb_") for ds in oz)


def test_objects_listed_in_dependency_order(all_facts):
    seen = set()
    for d, n, _ in r.OBJECTS:
        for _, ds, obj in all_facts[(d, n)].references:
            if (ds, obj) in {(x, y) for x, y, _ in r.OBJECTS}:
                assert (ds, obj) in seen, f"{d}.{n} ссылается на {ds}.{obj} раньше его объявления"
        seen.add((d, n))


def test_history_views_do_not_depend_on_query_time():
    """История воспроизводима: в *_HISTORY нет CURRENT_TIMESTAMP/CURRENT_DATE."""
    for d, n, _ in r.OBJECTS:
        if not n.endswith("_HISTORY"):
            continue
        body = re.sub(r"--[^\n]*", "", r.view_body(d, n)).upper()
        assert "CURRENT_TIMESTAMP" not in body and "CURRENT_DATE" not in body, n


def test_lifecycle_is_derived_from_observed_at_only():
    for d, n in (("wb_mart", "V_WB_PROMO_HISTORY"), ("ozon_mart", "V_OZON_PROMO_HISTORY")):
        body = r.view_body(d, n)
        case = body[body.index("CASE", body.index("ends_on_msk")):body.index("AS lifecycle_status")]
        assert "observed_at <" in case and "observed_at >" in case, n
        assert "CURRENT" not in case.upper(), n


def test_current_lifecycle_uses_observation_instants_not_now():
    body = r.view_body("evetis_mart", "V_PROMO_STATE_CURRENT")
    case = body[body.index("CASE"):body.index("AS current_lifecycle_status")]
    assert "marketplace_latest_observed_at" in case and "CURRENT_TIMESTAMP" not in case


def test_no_economics_no_decision_no_pii_columns(all_facts):
    for (d, n), f in all_facts.items():
        for col in f.output_columns:
            assert not PII.search(col), (n, col)
            assert not ECONOMICS.search(col), (n, col)


def test_no_economics_sources_read(all_facts):
    forbidden = re.compile(r"ECONOMICS|COGS|PNL|MART_SKU_DAILY|FACT_SKU_DAILY|ADS|FINANCE|TARIFF|STOCK", re.I)
    for (d, n), f in all_facts.items():
        for _, ds, obj in f.references:
            assert not forbidden.search(obj), (n, obj)


def test_unresolved_semantics_not_exposed(all_facts):
    """L-3/L-4: stock и эластичные цены остаются только в RAW."""
    for (d, n), f in all_facts.items():
        assert not (set(f.output_columns) & UNRESOLVED_SEMANTICS), n


def test_marketing_title_link_is_exact_only():
    body = r.view_body("ozon_mart", "V_OZON_PROMO_SKU_EVIDENCE_HISTORY")
    assert "a.title = m.action_title" in body
    for fuzzy in ("TRIM(", "LOWER(", "UPPER(", " LIKE ", "EDIT_DISTANCE", "SOUNDEX", "REGEXP_CONTAINS", "STARTS_WITH"):
        assert fuzzy not in body.upper().replace("\n", " "), fuzzy
    # DETERMINISTIC только при единственном совпадении title И окна дат
    assert "WHEN title_matches = 1 AND window_matches = 1 THEN 'DETERMINISTIC_EXACT_MATCH'" in body


def test_resolution_precedence_order():
    body = r.view_body("ozon_mart", "V_OZON_PROMO_SKU_STATE_HISTORY")
    end = body.index("END AS resolved_state,")
    case = body[body.rindex("CASE", 0, end):end]
    assert re.findall(r"THEN '([A-Z_]+)'", case) == [
        "PARTICIPATING", "SCHEDULED_AUTO_ADD", "AUTO_ADD_ELIGIBLE", "CANDIDATE", "NOT_ELIGIBLE"]
    assert "ELSE 'UNKNOWN'" in case
    # NOT_ELIGIBLE выводится только при полном перечислении
    assert "WHEN lists_complete AND auto_add_dates_count >= 0 THEN 'NOT_ELIGIBLE'" in case


def test_wb_has_no_catalog_cartesian():
    """WB: строки SKU только из свидетельств, никакого «каталог × акция»."""
    for n in ("V_WB_PROMO_SKU_EVIDENCE_HISTORY", "V_WB_PROMO_SKU_STATE_HISTORY"):
        body = r.view_body("wb_mart", n).upper()
        assert "CROSS JOIN" not in body and "RAW_WB_PROMO_CALENDAR" not in body, n
    state = r.view_body("wb_mart", "V_WB_PROMO_SKU_STATE_HISTORY")
    assert "FROM pairs p\nLEFT JOIN h" in state  # вселенная пар = свидетельства, акция только дополняет


def test_wb_auto_flags_are_null_not_false():
    body = r.view_body("wb_mart", "V_WB_PROMO_SKU_STATE_HISTORY")
    assert "CAST(NULL AS BOOL) AS in_auto_add_scheduled_list" in body
    assert "CAST(NULL AS BOOL) AS in_auto_add_eligible_list" in body


def test_fixture_scenarios_cover_the_contract():
    ids = [a[0] for a in r.ASSERTIONS]
    for i in range(1, 16):
        assert f"FX{i:02d}" in ids, f"сценарий {i} не покрыт"
    names = [(a[0], a[1]) for a in r.ASSERTIONS]
    assert len(names) == len(set(names))
    assert len(r.ASSERTIONS) >= 120


def test_fixtures_match_raw_ddl():
    schemas = r.raw_schemas()
    assert set(r.RAW_TABLES) <= set(schemas)
    for key, rows in r.fixtures().items():
        cols = {c for c, _ in schemas[key]}
        for row in rows:
            assert set(row) <= cols, (key, set(row) - cols)


@pytest.fixture(scope="module")
def fixture_blocks():
    return r.split_blocks(r.render_fixture_checks())


def test_fixture_checks_are_read_only_selects_without_tables(fixture_blocks):
    assert [cid for cid, _ in fixture_blocks] == list(dict.fromkeys(a[0] for a in r.ASSERTIONS))
    for cid, sql in fixture_blocks:
        assert_read_only(sql)
        assert f"`{P}." not in sql, f"{cid}: блок фикстур читает таблицу production"
        tree = sqlglot.parse(sql, read="bigquery")
        assert len(tree) == 1 and isinstance(tree[0], exp.Select), cid


def test_predeploy_validation_inlines_views_and_stays_read_only():
    blocks = r.split_blocks(r.render_predeploy_file(VALIDATION.read_text(encoding="utf-8")))
    assert len(blocks) == 24
    objects = {f"`{P}.{d}.{n}`" for d, n, _ in r.OBJECTS}
    for cid, sql in blocks:
        assert_read_only(sql)
        assert not any(o in sql for o in objects), cid
        assert "status" in sql, cid


def test_validation_file_follows_check_contract():
    raw = r.split_blocks(VALIDATION.read_text(encoding="utf-8"))
    ids = [cid for cid, _ in raw]
    assert len(ids) == len(set(ids)) == 24
    for cid, sql in raw:
        assert re.fullmatch(r"[A-Z0-9_]+", cid)
        assert_read_only(sql)
        assert sql.count(";") == 0, cid


def test_rollback_drops_exactly_the_sixteen_views_in_reverse_order():
    drops = re.findall(r"^DROP VIEW IF EXISTS `([^`]+)`;$", ROLLBACK.read_text(encoding="utf-8"), re.M)
    assert drops == [f"{P}.{d}.{n}" for d, n, _ in reversed(r.OBJECTS)]
    body = re.sub(r"--[^\n]*", "", ROLLBACK.read_text(encoding="utf-8"))
    assert re.findall(r"\b(DROP|DELETE|TRUNCATE|ALTER|INSERT|UPDATE|MERGE|CREATE)\b", body, re.I) == ["DROP"] * 16


def test_canonical_objects_are_pending_deploy_with_declared_schema(all_facts):
    for ds in ("ozon_mart", "evetis_mart"):
        man = json.loads((REPO / f"sql/current/{ds}/MANIFEST.json").read_text(encoding="utf-8"))
        entries = {o["object_name"]: o for o in man["objects"]}
        for d, n, folder in r.OBJECTS:
            if d != ds:
                continue
            assert folder == f"sql/current/{ds}"
            o = entries[n]
            assert o["canonical_schema_verification"] in ("unverified", "bigquery_verified")
            assert [c["column_name"] for c in o["canonical_schema"]] == all_facts[(d, n)].output_columns


def test_wb_views_stay_outside_sql_current():
    """wb_mart не канонизирован (CANONICAL_COVERAGE §3): частичный манифест дал бы R2C DRIFT."""
    assert not (REPO / "sql/current/wb_mart").exists()
    for d, _, folder in r.OBJECTS:
        if d == "wb_mart":
            assert folder == "sql/promotions/pr_promo2/wb_mart"


def test_deploy_refuses_objects_outside_the_list():
    with pytest.raises(KeyError):
        r.object_path("wb_mart", "V_DASH_KPI_DAILY")


def test_contract_document_exists_and_names_every_object():
    text = DOC.read_text(encoding="utf-8")
    for d, n, _ in r.OBJECTS:
        assert f"{d}.{n}" in text, n
