"""PR-PROMO-4 — офлайн-контракт слоя запасов и распродажи (без BigQuery).

Регрессия на фикстурах исполняется движком BigQuery (tools/promo_inventory_render.py run fixtures);
здесь — то, что проверяется без сети: состав объектов, границы чтения, отсутствие решений и
баллов, календарь, защита развёртывания, синхронность контракта фикстур с проверками.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1]
REPO = TOOLS.parent
sys.path.insert(0, str(TOOLS))

import promo_inventory_deploy as deploy  # noqa: E402
import promo_inventory_render as ir  # noqa: E402
import validate_current_sql as v  # noqa: E402

P = ir.PROJECT
VIEWS = [name for _, name, _ in ir.OBJECTS]
VALIDATION = REPO / "sql" / "promotions" / "pr_promo4_inventory_validation.sql"
ROLLBACK = REPO / "sql" / "promotions" / "pr_promo4_rollback.sql"
PR3_CURRENT = REPO / "sql" / "current" / "evetis_mart" / "V_PROMO_ECONOMICS_SCENARIO_CURRENT.sql"


def facts(name: str):
    findings: list = []
    f = v.analyze_sql((REPO / "sql" / "current" / "evetis_mart" / f"{name}.sql").read_text(encoding="utf-8"), name, findings)
    assert not findings, findings
    return f


def refs(name: str) -> set[tuple[str, str]]:
    return {(d, n) for _, d, n in facts(name).references}


def test_seven_views_in_dependency_order():
    assert VIEWS == ["V_INVENTORY_POSITION_HISTORY", "V_SKU_SELL_THROUGH_CURRENT", "V_SKU_INVENTORY_TARGET_CURRENT",
                     "V_SALES_PLAN_MONTHLY_CURRENT", "V_SKU_INVENTORY_TRAJECTORY_MONTHLY_CURRENT",
                     "V_BUNDLE_ASSEMBLY_CAPACITY_CURRENT", "V_PROMO_INVENTORY_CONTEXT_CURRENT"]
    seen: set[str] = set()
    for name in VIEWS:
        internal = {n for d, n in refs(name) if d == "evetis_mart" and n in VIEWS}
        assert internal <= seen, (name, internal - seen)
        seen.add(name)


def test_no_second_inventory_system_history_reads_only_the_control_tower_snapshot():
    # История запаса — переименование понятий поверх среза Control Tower, без пересчёта формул.
    assert refs("V_INVENTORY_POSITION_HISTORY") == {
        ("evetis_ref", "CT_INVENTORY_SNAPSHOT_DAILY"), ("evetis_ref", "REF_PRODUCT_MASTER"),
        ("evetis_ref", "REF_SKU_CHANNEL_MAP")}
    body = re.sub(r"--[^\n]*", "", facts("V_INVENTORY_POSITION_HISTORY").body)
    assert "CURRENT_DATE" not in body and "CURRENT_TIMESTAMP" not in body  # история не зависит от «сейчас»
    assert "s.sellable_units AS inventory_position_units" in body


def test_neutral_layer_never_reads_raw_and_only_policy_datasets():
    allowed = v.EXTERNAL_DATASET_POLICY["evetis_mart"] | {"evetis_mart"}
    for name in VIEWS:
        for d, n in refs(name):
            assert d in allowed, (name, d, n)
            assert not d.endswith("_raw"), (name, d, n)


def test_heavy_control_tower_freshness_view_is_not_read_by_the_layer():
    # V_CT_FRESHNESS раскрывается при каждом обращении и переполняет планировщик; паритет — DQ I05.
    for name in VIEWS:
        assert ("wb_mart", "V_CT_FRESHNESS") not in refs(name), name
    body = facts("V_SKU_SELL_THROUGH_CURRENT").body
    assert "1 AS wb_stock_sla_days" in body and "1 AS ozon_stock_sla_days" in body and "7 AS ff_stock_sla_days" in body
    i05 = VALIDATION.read_text(encoding="utf-8").split("-- @check I05_")[1].split("-- @check ")[0]
    assert "WHEN 'WB_STOCK' THEN 1 WHEN 'OZON_STOCK' THEN 1 WHEN 'FF_STOCK' THEN 7" in i05


def test_external_fixture_contract_covers_exactly_the_external_reads():
    # С PR-PLAN-1 фикстуры рендерят объединённый граф (PR-PROMO-4 + PR-PLAN-1). CT_PLAN_VERSION
    # представления больше не читают (его читает только процедура регистрации legacy-версии), но
    # он остаётся в контракте схем: на нём стоит отпечаток I40.
    render = [n for _, n, _ in ir.RENDER_OBJECTS]
    external = set()
    for name in render:
        external |= {(d, n) for d, n in refs(name) if not (d == "evetis_mart" and n in render)}
    external -= set(ir.RENDER_TABLES)
    assert external <= set(ir.EXTERNALS), external - set(ir.EXTERNALS)
    assert set(ir.EXTERNALS) - external == {("evetis_ref", "CT_PLAN_VERSION")}


def test_validation_fingerprint_matches_the_fixture_contract():
    doc = json.loads(ir.EXTERNAL_SCHEMAS.read_text(encoding="utf-8"))
    lines = [f"{obj}.{c['column_name']}:{c['data_type']}" for obj in sorted(doc["objects"]) for c in doc["objects"][obj]]
    digest = hashlib.sha256("\n".join(lines).encode()).hexdigest()
    assert f"= '{digest}'" in VALIDATION.read_text(encoding="utf-8")


FORBIDDEN_COLUMN = re.compile(r"score|rank|recommend|decision|verdict|enter_|exit_|stay_|watch_|floor|relax|"
                              r"buyer|customer|phone|email|address|surname|patronymic|fio", re.I)


def test_no_decision_score_floor_or_pii_columns():
    for name in VIEWS:
        cols = facts(name).output_columns
        assert cols, name
        bad = [c for c in cols if FORBIDDEN_COLUMN.search(c)]
        assert not bad, (name, bad)


def test_no_decision_labels_in_view_bodies():
    for name in VIEWS:
        body = facts(name).body
        for word in ("'ENTER'", "'STAY'", "'EXIT'", "'WATCH'", "'APPROVE'", "'REJECT'", "'GOOD'", "'BAD'"):
            assert word not in body, (name, word)


def test_calendar_is_never_thirty_day_months():
    for name in VIEWS:
        body = re.sub(r"--[^\n]*", "", facts(name).body)
        assert "30.44" not in body and "30.4" not in body, name
        assert not re.search(r"\*\s*30\b(?!\s*\))", body) or name == "V_SKU_SELL_THROUGH_CURRENT", name
    plan = facts("V_SALES_PLAN_MONTHLY_CURRENT").body
    traj = facts("V_SKU_INVENTORY_TRAJECTORY_MONTHLY_CURRENT").body
    assert "LAST_DAY(" in plan and "LAST_DAY(" in traj and "GENERATE_DATE_ARRAY(" in traj


def test_economics_pass_through_verbatim_from_pr_promo3():
    ctx = facts("V_PROMO_INVENTORY_CONTEXT_CURRENT")
    findings: list = []
    pr3 = v.analyze_sql(PR3_CURRENT.read_text(encoding="utf-8"), "pr3", findings)
    economics = ["scenario_price_rub", "baseline_price_rub", "baseline_contribution_expected_rub",
                 "promo_contribution_expected_rub", "delta_contribution_expected_rub", "baseline_margin_expected_pct",
                 "promo_margin_expected_pct", "break_even_price_expected_rub", "promo_contribution_downside_rub",
                 "promo_downside_negative", "uplift_status", "required_sales_uplift_pct", "economics_status"]
    for col in economics:
        assert col in pr3.output_columns and col in ctx.output_columns, col
        assert re.search(rf"\bx\.{col},", ctx.body), col          # перенесено без выражения
    assert "required_inventory_uplift" not in " ".join(pr3.output_columns)


def test_targets_have_provenance_and_no_invented_types():
    body = facts("V_SKU_INVENTORY_TARGET_CURRENT").body
    assert "'EXPIRY_DATE'" in body and "'EXPIRY_SELL_BY'" in body and "'OWNER_TARGET'" in body
    assert "TARGET_COVER" not in body and "OVERSTOCK" not in body
    assert "target_provenance" in facts("V_SKU_INVENTORY_TARGET_CURRENT").output_columns
    sell = facts("V_SKU_SELL_THROUGH_CURRENT").body
    assert "config_key = 'c1_expiry_margin_days'" in sell     # буфер — только утверждённый владельцем


def test_plan_numbers_only_with_owner_approval():
    # С PR-PLAN-1 план читается только из контракта утверждённых версий, план Control Tower — нет.
    body = facts("V_SALES_PLAN_MONTHLY_CURRENT").body
    r = refs("V_SALES_PLAN_MONTHLY_CURRENT")
    assert ("evetis_mart", "V_SALES_PLAN_APPROVED") in r
    assert not {("evetis_ref", "CT_SEASON_PLAN_MONTHLY"), ("evetis_ref", "CT_PLAN_VERSION"),
                ("evetis_ref", "REF_SALES_PLAN_APPROVAL")} & r
    assert "IF(m.approved, m.target_cards, NULL) AS planned_cards" in body
    assert ("evetis_mart", "V_SALES_PLAN_MONTHLY_CURRENT") not in refs("V_SKU_INVENTORY_TRAJECTORY_MONTHLY_CURRENT")


def test_bundle_capacity_only_from_authoritative_bom():
    r = refs("V_BUNDLE_ASSEMBLY_CAPACITY_CURRENT")
    assert ("wb_mart", "V_CT_BOM_CURRENT") in r
    body = facts("V_BUNDLE_ASSEMBLY_CAPACITY_CURRENT").body
    assert "'BUNDLE_BOM_UNAVAILABLE'" in body and "TECHNICAL_ASSEMBLY_CAPACITY_FROM_FF_UNITS_NOT_ALLOCATED" in body
    assert "product_name" not in re.sub(r"--[^\n]*", "", body).split("bom_agg AS")[0].split("bundle_links AS")[1]


def test_every_spec_scenario_has_a_fixture_block():
    ids = {sid for sid, *_ in ir.SCENARIOS}
    assert set(ir.SPEC_SCENARIOS) == set(range(1, 46))
    for n, blocks in ir.SPEC_SCENARIOS.items():
        assert blocks and set(blocks) <= ids, (n, blocks)


def test_fixture_calendar_months_are_what_they_claim():
    import calendar
    assert calendar.monthrange(ir.FEB_NONLEAP.year, 2)[1] == 28
    assert calendar.monthrange(ir.FEB_LEAP.year, 2)[1] == 29
    assert calendar.monthrange(ir.M30.year, ir.M30.month)[1] == 30
    assert calendar.monthrange(ir.M31.year, ir.M31.month)[1] == 31
    assert ir.TODAY < ir.FEB_NONLEAP < ir.LEAP_TARGET and ir.TODAY < ir.FEB_LEAP < ir.LEAP_TARGET


def test_deploy_guards_accept_the_repo_and_refuse_writes():
    assert "CREATE TABLE IF NOT EXISTS" in deploy.ddl_script()
    for d, n, _ in ir.OBJECTS:
        assert deploy.view_statement(d, n).count("CREATE OR REPLACE VIEW") == 1
    assert deploy.FORBIDDEN.search("INSERT INTO x") and deploy.FORBIDDEN.search("DROP TABLE x")


def test_rollback_drops_only_the_new_objects_and_guards_owner_data():
    text = ROLLBACK.read_text(encoding="utf-8")
    drops = re.findall(r"DROP (VIEW|TABLE) IF EXISTS `[^.]+\.(\w+)\.(\w+)`", text)
    assert {(k, d, n) for k, d, n in drops} == (
        {("VIEW", "evetis_mart", n) for n in VIEWS} | {("TABLE", d, n) for d, n in ir.NEW_TABLES})
    assert text.index("ASSERT") < text.index("DROP")
    assert text.count("ASSERT (SELECT COUNT(*)") == 2


def test_owner_reference_tables_have_no_automated_writer():
    # Пишет только владелец: ни процедура, ни Terraform, ни загрузчик не упоминают эти таблицы.
    hits = []
    for path in list(REPO.glob("sql/**/*.sql")) + list(REPO.glob("infra/**/*.tf")) + list(REPO.glob("cloud/**/*.ts")) \
            + list(REPO.glob("apps-script/**/*.gs")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for t in ("REF_SALES_PLAN_APPROVAL", "REF_SKU_INVENTORY_TARGET"):
            if t in text:
                hits.append(str(path.relative_to(REPO)))
    # PR-PLAN-1: реестр событий плана пишут только процедуры sql/plan/plan1_procedures.sql, которые
    # запускаются вручную (расписаний и сервисных аккаунтов нет — tools/tests/test_plan1.py).
    allowed = {"sql/promotions/pr_promo4_planning_refs.sql", "sql/promotions/pr_promo4_inventory_validation.sql",
               "sql/promotions/pr_promo4_rollback.sql", "sql/current/evetis_mart/V_SKU_INVENTORY_TARGET_CURRENT.sql",
               "sql/plan/plan1_storage.sql", "sql/plan/plan1_procedures.sql", "sql/plan/plan1_validation.sql",
               "sql/current/evetis_mart/V_PLAN_VERSION_STATUS.sql", "sql/current/evetis_mart/V_SALES_PLAN_MONTHLY_CURRENT.sql"}
    assert set(hits) <= allowed, set(hits) - allowed
