"""PR-PROMO-3 — офлайн-тесты контракта слоя экономики акций.

Без сети, без учётных данных, без BigQuery. Охраняют в Git то, что нельзя доказать данными:
второго движка экономики нет (представления читают только снимки канона, процедура копирует
канон без арифметики), история не зависит от «сейчас», Юнитка не читается, площадки
изолированы, рекомендаций, запасов, налога, хранения и СПП нет, права и расписания узкие,
пути записи в маркетплейсы не появились.

Семантику на данных проверяют исполнимые наборы (read-only, BigQuery):
  регрессия:   python tools/promo_economics_render.py run fixtures …
  production:  sql/promotions/pr_promo3_economics_validation.sql (набор promo_economics)
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
import promo_canonical_render as base  # noqa: E402
import promo_economics_deploy as deploy  # noqa: E402
import promo_economics_render as e  # noqa: E402
import validate_current_sql as v  # noqa: E402
from lib.bq_readonly import assert_read_only  # noqa: E402

P = e.PROJECT
VALIDATION = REPO / "sql/promotions/pr_promo3_economics_validation.sql"
ROLLBACK = REPO / "sql/promotions/pr_promo3_rollback.sql"
TF = REPO / "infra/terraform/promo_economics_snapshot.tf"
DOC = REPO / "docs/promotions/PR_PROMO_3_ECONOMICS_INTEGRATION_2026-09-24.md"

ALLOWED_READS = {
    "wb_mart": {"wb_raw", "wb_mart", "evetis_ref"},
    "ozon_mart": {"ozon_raw", "ozon_mart", "evetis_ref"},
    "evetis_mart": {"wb_mart", "ozon_mart", "evetis_mart"},
}
# Источники, из которых СЧИТАЕТСЯ экономика. Слой экономики акций не имеет права их читать:
# он получает канон только через снимки. Иначе это был бы второй движок.
ECONOMICS_INPUTS = re.compile(
    r"FORWARD_ECONOMICS|COST_INPUTS|TARIFF|FINANCE|COGS|PRICES|UNITKA|PNL|(^|_)ADS(_|$)|STOCK|INVENTORY|STORAGE|(^|_)CT_", re.I)
FORBIDDEN_COLUMNS = re.compile(
    r"recommend|decision|enter|exit|stay|approve|reject|score|stock|inventory|expiry|storage|spp|policy|attractiv|"
    r"customer|email|phone|buyer", re.I)


def facts(dataset, name):
    findings = []
    f = v.analyze_sql(base.object_path(dataset, name, e.OBJECTS).read_text(encoding="utf-8"), name, findings)
    assert findings == [], findings
    return f


@pytest.fixture(scope="module")
def all_facts():
    return {(d, n): facts(d, n) for d, n, _ in e.NEW_OBJECTS}


def test_seven_views_each_one_create_view_of_its_own_name(all_facts):
    assert len(e.NEW_OBJECTS) == 7
    for (d, n), f in all_facts.items():
        assert f.target == (P, d, n)
        assert f.description and "PR-PROMO-3" in f.description
        assert deploy.view_statement(d, n)


def test_marketplace_isolation(all_facts):
    for (d, n), f in all_facts.items():
        read = {ds for _, ds, _ in f.references}
        assert read <= ALLOWED_READS[d], (n, read - ALLOWED_READS[d])


def test_no_second_economics_engine(all_facts):
    """Представления не читают ни одного источника экономики — только снимки базиса канона."""
    for (d, n), f in all_facts.items():
        for _, ds, obj in f.references:
            assert not ECONOMICS_INPUTS.search(obj), (n, f"{ds}.{obj}")


def test_basis_views_read_only_their_snapshot(all_facts):
    assert {(ds, o) for _, ds, o in all_facts[("wb_mart", "V_WB_PROMO_ECONOMICS_BASIS_HISTORY")].references} == \
        {("wb_raw", "WB_PROMO_ECONOMICS_BASIS_SNAPSHOT")}
    assert {(ds, o) for _, ds, o in all_facts[("ozon_mart", "V_OZON_PROMO_ECONOMICS_BASIS_HISTORY")].references} == \
        {("ozon_raw", "OZON_PROMO_ECONOMICS_BASIS_SNAPSHOT")}


def test_procedures_copy_canonical_views_without_arithmetic():
    """Процедура копирует канон: каждое выражение SELECT — голая колонка канонической вью,
    кроме служебных slot / id / now_ts / run_trigger и начала действия COGS (провенанс)."""
    text = e.BASIS_DDL.read_text(encoding="utf-8")
    service = {"slot", "now_ts", "run_trigger", "c.effective_from"}
    blocks = re.findall(r"\n  SELECT (slot, .*?)\n  FROM (`[^`]+`)", text, re.S)
    assert [b[1] for b in blocks] == [f"`{P}.wb_mart.V_WB_SKU_FORWARD_ECONOMICS_CURRENT`",
                                      f"`{P}.ozon_mart.V_OZON_SKU_FORWARD_ECONOMICS_CURRENT`"]
    for items, _ in blocks:
        for item in e._split_top_level(items):
            if item in service or item.startswith("'WBECON_prod_'") or item.startswith("'OZECON_prod_'"):
                continue
            assert re.fullmatch(r"(f\.)?[a-z_][a-z0-9_]*", item), f"не голая колонка канона: {item}"


def test_snapshot_ddl_matches_procedure_columns():
    sch = e.schemas()
    text = e.BASIS_DDL.read_text(encoding="utf-8")
    for ds, t in e.NEW_TABLES:
        cols = re.search(r"INSERT INTO `" + re.escape(f"{P}.{ds}.{t}") + r"`\n\s*\((.*?)\)", text, re.S).group(1)
        assert [c.strip() for c in cols.replace("\n", " ").split(",")] == [c for c, _ in sch[(ds, t)]], t


def test_history_does_not_depend_on_query_time():
    for d, n, _ in e.NEW_OBJECTS:
        if n.endswith("_HISTORY"):
            body = re.sub(r"--[^\n]*", "", base.view_body(d, n, e.OBJECTS)).upper()
            assert "CURRENT_TIMESTAMP" not in body and "CURRENT_DATE" not in body, n


def test_single_algebra_of_the_canon():
    """Вклад при цене P — одно выражение канона: P × удерживаемая доля − постоянные издержки."""
    for d, n in (("wb_mart", "V_WB_PROMO_ECONOMICS_SCENARIO_HISTORY"), ("ozon_mart", "V_OZON_PROMO_ECONOMICS_SCENARIO_HISTORY")):
        body = base.view_body(d, n, e.OBJECTS)
        for expr in ("baseline_price_rub * price_retention_expected - fixed_cost_expected_rub",
                     "scenario_price_rub * price_retention_expected - fixed_cost_expected_rub",
                     "baseline_price_rub * price_retention_downside - fixed_cost_downside_rub",
                     "scenario_price_rub * price_retention_downside - fixed_cost_downside_rub"):
            assert body.count(expr) == 1, (n, expr)


def test_uplift_only_when_both_contributions_positive():
    for d, n in (("wb_mart", "V_WB_PROMO_ECONOMICS_SCENARIO_HISTORY"), ("ozon_mart", "V_OZON_PROMO_ECONOMICS_SCENARIO_HISTORY")):
        body = base.view_body(d, n, e.OBJECTS)
        assert ("IF(economics_status = 'COMPUTABLE' AND baseline_contribution_expected_rub > 0 AND "
                "promo_contribution_expected_rub > 0,\n     (baseline_contribution_expected_rub / "
                "promo_contribution_expected_rub - 1) * 100, NULL) AS required_sales_uplift_pct") in body
        assert "GREATEST(" not in body.upper(), "рост не должен обрезаться до нуля"


def test_no_forbidden_columns_or_values(all_facts):
    for (d, n), f in all_facts.items():
        for col in f.output_columns:
            assert col == "tax_model_status" or not re.search(r"tax", col, re.I), (n, col)
            assert not FORBIDDEN_COLUMNS.search(col), (n, col)
        body = base.view_body(d, n, e.OBJECTS)
        assert not re.search(r"'(ENTER|EXIT|STAY|WATCH|APPROVE|REJECT|GOOD_PROMO|BAD_PROMO)'", body), n
        assert "0.2" not in re.sub(r"--[^\n]*", "", body), f"{n}: порог 20 % не утверждён политикой"


def test_downside_is_marketplace_specific():
    wb = base.view_body("wb_mart", "V_WB_PROMO_ECONOMICS_BASIS_HISTORY", e.OBJECTS)
    oz = base.view_body("ozon_mart", "V_OZON_PROMO_ECONOMICS_BASIS_HISTORY", e.OBJECTS)
    assert "'WB_STRESS_P90_ACQUIRING_AND_LOGISTICS' AS downside_case" in wb and "WORST" not in wb
    assert "'OZON_WORST_MAX_LOGISTICS' AS downside_case" in oz


def test_ozon_acquiring_fraction_is_the_canonical_definition():
    oz = base.view_body("ozon_mart", "V_OZON_PROMO_ECONOMICS_BASIS_HISTORY", e.OBJECTS)
    assert "ROUND(SAFE_DIVIDE(acquiring_rub, seller_base_price) * 100, 4) / 100 AS a_frac" in oz
    tariff = (REPO / "sql/current/ozon_mart/V_OZON_SKU_CURRENT_TARIFF.sql").read_text(encoding="utf-8")
    assert "ROUND(SAFE_DIVIDE(p.acquiring_rub, p.marketing_seller_price_rub) * 100, 4) AS acquiring_pct" in tariff


def test_wb_has_no_auto_promotion_rows():
    body = base.view_body("wb_mart", "V_WB_PROMO_ECONOMICS_SCENARIO_HISTORY", e.OBJECTS)
    # PROMO-строки WB — только из свидетельств /nomenclatures, агрегаты акции не читаются.
    assert "V_WB_PROMO_SKU_EVIDENCE_HISTORY" in body and "in_promo_total" not in body
    assert "'PRICE_SEMANTICS_UNPROVEN'" in body


def test_fixture_scenarios_cover_the_contract():
    ids = {a[0] for a in e.ASSERTIONS}
    for i in range(1, 31):
        assert f"FE{i:02d}" in ids, f"сценарий {i} не покрыт"
    names = [(a[0], a[1]) for a in e.ASSERTIONS]
    assert len(names) == len(set(names))


@pytest.fixture(scope="module")
def fixture_blocks():
    return base.split_blocks(e.render_fixture_checks())


def test_fixture_checks_are_read_only_selects_without_tables(fixture_blocks):
    for cid, sql in fixture_blocks:
        assert_read_only(sql)
        assert f"`{P}." not in sql, f"{cid}: блок фикстур читает таблицу production"
        tree = sqlglot.parse(sql, read="bigquery")
        assert len(tree) == 1 and isinstance(tree[0], exp.Select), cid


def test_predeploy_checks_are_read_only_and_use_the_virtual_snapshot():
    blocks = base.split_blocks(e.render_predeploy_file(VALIDATION.read_text(encoding="utf-8")))
    assert len(blocks) == 21
    new = {f"`{P}.{d}.{n}`" for d, n, _ in e.OBJECTS} | {f"`{P}.{d}.{t}`" for d, t in e.NEW_TABLES}
    for cid, sql in blocks:
        assert_read_only(sql)
        assert not any(o in sql for o in new), cid


def test_validation_file_follows_check_contract():
    raw = base.split_blocks(VALIDATION.read_text(encoding="utf-8"))
    ids = [c for c, _ in raw]
    assert len(ids) == len(set(ids)) == 21
    for cid, sql in raw:
        assert re.fullmatch(r"[A-Z0-9_]+", cid)
        assert_read_only(sql)


def test_deploy_refuses_mutating_ddl(tmp_path, monkeypatch):
    assert deploy.ddl_script()
    bad = tmp_path / "ddl.sql"
    bad.write_text(e.BASIS_DDL.read_text(encoding="utf-8") + "\nDROP TABLE `x.y.z`;\n", encoding="utf-8")
    monkeypatch.setattr(e, "BASIS_DDL", bad)
    with pytest.raises(SystemExit):
        deploy.ddl_script()


def test_rollback_drops_views_and_procedures_but_keeps_snapshots():
    text = ROLLBACK.read_text(encoding="utf-8")
    views = re.findall(r"^DROP VIEW IF EXISTS `([^`]+)`;$", text, re.M)
    assert views == [f"{P}.{d}.{n}" for d, n, _ in reversed(e.NEW_OBJECTS)]
    assert len(re.findall(r"^DROP PROCEDURE IF EXISTS", text, re.M)) == 2
    body = re.sub(r"--[^\n]*", "", text)
    assert "DROP TABLE" not in body.upper(), "снимки базиса — невосстановимая история, откат их не удаляет"


def test_terraform_grants_are_narrow_and_scheduler_targets_only_bigquery():
    tf = TF.read_text(encoding="utf-8")
    roles = set(re.findall(r'role\s*=\s*"([^"]+)"', tf))
    assert roles == {"roles/bigquery.jobUser", "roles/bigquery.dataViewer", "roles/bigquery.dataEditor"}
    assert re.search(r'resource "google_bigquery_table_iam_member" "promo_econ_write"[^}]*roles/bigquery.dataEditor', tf, re.S)
    assert 'google_bigquery_dataset_iam_member" "promo_econ_read"' in tf
    assert not re.search(r'dataset_iam_member[^}]*dataEditor', tf, re.S), "запись — только потабличная"
    assert set(re.findall(r'uri\s*=\s*"(https://[^/"]+)', tf)) == {"https://bigquery.googleapis.com"}
    wb, oz = tf.split("ozon = {")
    assert "ozon" not in wb.split("wb = {")[1], "SA WB не читает датасеты Ozon"
    assert "var.mart_dataset" not in oz.split("}")[0] and "var.raw_dataset" not in oz.split("}")[0]


def test_no_marketplace_api_paths_or_hosts_in_new_code():
    hosts = re.compile(r"api-seller\.ozon\.ru|wildberries\.ru|performance\.ozon\.ru|/v1/actions/(activate|deactivate|"
                       r"products/(activate|deactivate))|auto-add/products/(update|delete)", re.I)
    files = [e.BASIS_DDL, VALIDATION, ROLLBACK, TF, REPO / "tools/promo_economics_render.py",
             REPO / "tools/promo_economics_deploy.py"] + \
        [base.object_path(d, n, e.OBJECTS) for d, n, _ in e.NEW_OBJECTS]
    for f in files:
        assert not hosts.search(f.read_text(encoding="utf-8")), f


def test_unitka_is_not_read_or_changed():
    files = [e.BASIS_DDL, TF] + [base.object_path(d, n, e.OBJECTS) for d, n, _ in e.NEW_OBJECTS]
    for f in files:
        assert "UNITKA" not in re.sub(r"--[^\n]*", "", f.read_text(encoding="utf-8")).upper(), f


def test_canonical_objects_declare_schema(all_facts):
    for ds in ("ozon_mart", "evetis_mart"):
        man = json.loads((REPO / f"sql/current/{ds}/MANIFEST.json").read_text(encoding="utf-8"))
        entries = {o["object_name"]: o for o in man["objects"]}
        for d, n, folder in e.NEW_OBJECTS:
            if d == ds:
                assert folder == f"sql/current/{ds}"
                assert [c["column_name"] for c in entries[n]["canonical_schema"]] == all_facts[(d, n)].output_columns


def test_contract_document_names_every_object():
    text = DOC.read_text(encoding="utf-8")
    for d, n, _ in e.NEW_OBJECTS:
        assert f"{d}.{n}" in text, n
    for d, t in e.NEW_TABLES:
        assert f"{d}.{t}" in text, t
