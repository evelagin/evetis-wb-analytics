"""PR-PLAN-1 — офлайн-контракт плана продаж и траектории запаса (без BigQuery).

Регрессия на фикстурах исполняется движком BigQuery (tools/plan1_render.py run fixtures); здесь — то,
что проверяется без сети: состав объектов и порядок зависимостей, границы чтения (Control Tower и
C1 не переключены, BOM только из снимка версии), отсутствие автоматического утверждения и
расписаний, единый канон хеша, откат, защиты развёртывания, полнота карты §17.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1]
REPO = TOOLS.parent
sys.path.insert(0, str(TOOLS))

import plan1_deploy as deploy  # noqa: E402
import plan1_render as pr  # noqa: E402
import promo_inventory_render as ir  # noqa: E402
import validate_current_sql as v  # noqa: E402

PLAN_VIEWS = [n for _, n, _ in ir.PLAN1_OBJECTS]
PROCS = REPO / "sql" / "plan" / "plan1_procedures.sql"
STORAGE = REPO / "sql" / "plan" / "plan1_storage.sql"
ROLLBACK = REPO / "sql" / "plan" / "plan1_rollback.sql"
VALIDATION = REPO / "sql" / "plan" / "plan1_validation.sql"


def facts(name: str):
    findings: list = []
    f = v.analyze_sql((REPO / "sql" / "current" / "evetis_mart" / f"{name}.sql").read_text(encoding="utf-8"), name, findings)
    assert not findings, findings
    return f


def refs(name: str) -> set[tuple[str, str]]:
    return {(d, n) for _, d, n in facts(name).references}


def body(name: str) -> str:
    return re.sub(r"--[^\n]*", "", facts(name).body)


def test_render_graph_is_in_dependency_order():
    order = [n for _, n, _ in ir.RENDER_OBJECTS]
    seen: set[str] = set()
    for name in order:
        internal = {n for d, n in refs(name) if d == "evetis_mart" and n in order}
        assert internal <= seen, (name, internal - seen)
        seen.add(name)


def test_control_tower_and_c1_are_not_switched():
    # Новый контракт строится параллельно: ни одно представление PR-PLAN-1 не читает план CT как
    # бизнес-план и ничего из evetis_ops; CT читается только адаптером legacy-строк и срезом запаса.
    for name in PLAN_VIEWS:
        r = refs(name)
        assert not any(d == "evetis_ops" for d, _ in r), name
        assert ("wb_mart", "V_CT_PLAN_ACTIVE") not in r and ("evetis_ref", "CT_PLAN_VERSION") not in r, name
    assert ("evetis_ref", "CT_SEASON_PLAN_MONTHLY") in refs("V_PLAN_LINE_MONTHLY_ALL")
    ct_readers = [n for n in PLAN_VIEWS if ("evetis_ref", "CT_SEASON_PLAN_MONTHLY") in refs(n)]
    assert ct_readers == ["V_PLAN_LINE_MONTHLY_ALL"]


def test_bom_only_from_the_version_snapshot():
    # Смысл утверждённой версии не меняется при правке REF_BUNDLE_COMPONENTS: представления её не читают.
    for name in PLAN_VIEWS:
        assert ("evetis_ref", "REF_BUNDLE_COMPONENTS") not in refs(name), name
        assert ("wb_mart", "V_CT_BOM_CURRENT") not in refs(name), name
    assert ("evetis_ref", "PLAN_BOM_BASIS") in refs("V_PLAN_PHYSICAL_MONTHLY")


def test_no_yousend_or_raw_reads_and_policy_datasets_only():
    allowed = v.EXTERNAL_DATASET_POLICY["evetis_mart"] | {"evetis_mart"}
    for name in PLAN_VIEWS:
        for d, n in refs(name):
            assert d in allowed and not d.endswith("_raw"), (name, d, n)
    text = " ".join((REPO / "sql" / "current" / "evetis_mart" / f"{n}.sql").read_text(encoding="utf-8").lower()
                    for n in PLAN_VIEWS)
    assert "usend" not in text.replace("yousend", "usend") or "product/stock" not in text


def test_approved_plan_comes_only_from_status_windows():
    b = body("V_SALES_PLAN_APPROVED")
    assert refs("V_SALES_PLAN_APPROVED") == {("evetis_mart", "V_PLAN_VERSION_STATUS"), ("evetis_mart", "V_PLAN_LINE_MONTHLY_ALL")}
    assert "lifecycle_status IN ('APPROVED', 'SUPERSEDED', 'REVOKED')" in b
    assert "l.month >= s.effective_from_month AND l.month < s.effective_to_month_exclusive" in b


def test_model_scenario_cannot_be_approved_anywhere():
    st = body("V_PLAN_VERSION_STATUS")
    assert "v.plan_kind IN ('SYSTEM_PROPOSED', 'OWNER_AUTHORED') AS approvable_kind" in st
    assert "WHEN v.plan_kind = 'MODEL_SCENARIO' THEN 'MODEL_SCENARIO'" in st
    procs = PROCS.read_text(encoding="utf-8")
    assert "v.plan_kind = 'MODEL_SCENARIO' AND p_event IN ('PROPOSED', 'APPROVED')" in procs


def test_one_hash_canon_in_view_procedures_and_fixtures():
    st = facts("V_PLAN_VERSION_STATUS").body
    assert "CONCAT(CAST(month AS STRING), '|', marketplace, '|', internal_sku, '|', CAST(planned_cards AS STRING))" in st
    assert "CONCAT(bundle_sku, '|', component_sku, '|', CAST(component_qty AS STRING))" in st
    assert "'\\n#BOM\\n'" in st
    procs = PROCS.read_text(encoding="utf-8")
    assert procs.count("'\\n#BOM\\n'") == 2
    assert "CAST(ROUND(planned_cards, 4) AS STRING)" in procs            # то же, что ROUND в адаптере строк
    assert "ROUND(l.planned_cards, 4) AS planned_cards" in facts("V_PLAN_LINE_MONTHLY_ALL").body
    assert "ROUND(CAST(SUM(c.target_cards) AS NUMERIC), 4)" in facts("V_PLAN_LINE_MONTHLY_ALL").body
    assert "ROUND(CAST(SUM(c.target_cards) AS NUMERIC), 4)" in procs
    # Python-канон фикстур: без хвостовых нулей, сортировка строк.
    assert ir.num_str(300.0) == "300" and ir.num_str(12.5) == "12.5" and ir.num_str(0) == "0" and ir.num_str(7.25) == "7.25"


def test_nothing_approves_automatically():
    # Ни расписания, ни сервисного аккаунта, ни загрузчика: процедуры упоминаются только в SQL
    # PR-PLAN-1, документации и инструментах, которые их не вызывают.
    hits = []
    for path in list(REPO.glob("infra/**/*.tf")) + list(REPO.glob("cloud/**/*.ts")) + list(REPO.glob("apps-script/**/*.gs")) \
            + list(REPO.glob("pipelines/**/*.py")) + list(REPO.glob(".github/workflows/*.yml")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        # Полные имена таблиц: ключ свежести C1 «PLAN_VERSION» в Apps Script — другое понятие.
        if re.search(r"sp_plan_|sp_inbound_record|evetis_ref\.(PLAN_VERSION|PLAN_LINE_MONTHLY|INBOUND_LOT_EVENT)\b", text):
            hits.append(str(path.relative_to(REPO)))
    assert hits == []
    procs = PROCS.read_text(encoding="utf-8")
    assert "sp_plan_approve" in procs and "CALL `project-fa311fc0-4d87-4781-986.evetis_ref.sp_plan_approve`" not in procs
    for name in PLAN_VIEWS:
        assert "INSERT" not in facts(name).body.upper().replace("INSERTED", "")


def test_trajectory_formula_and_no_clipping():
    b = body("V_PLAN_TRAJECTORY_MONTHLY")
    assert "s.inventory_position_units + s.net_change_to_date AS closing_raw" in b
    assert "SUM(r.eligible_inbound_units - IFNULL(r.pm.planned_units_remaining, 0)) OVER w AS net_change_to_date" in b
    assert "GREATEST(-closing_raw, 0)" in b and "'COMPONENT_SHORTFALL'" in b
    assert "GREATEST(closing_raw" not in b                                  # остаток не обрезается
    assert "WHERE in_base_trajectory" in b                                   # только допущенные поступления


def test_inbound_rule_requires_proof():
    b = body("V_INBOUND_LOT_CURRENT")
    for s in ("'EXCLUDED_CANCELLED'", "'EXCLUDED_HYPOTHETICAL'", "'RECEIVED_IN_POSITION'", "'RECEIVED_NOT_IN_POSITION'",
              "'BLOCKED'", "'ETA_UNKNOWN'", "'ETA_NOT_CONFIRMED'", "'ETA_OVERDUE'", "'ELIGIBLE_CONFIRMED'"):
        assert s in b, s
    order = [b.index(x) for x in ("'EXCLUDED_CANCELLED'", "'EXCLUDED_HYPOTHETICAL'", "'BLOCKED'", "'ETA_UNKNOWN'",
                                  "'ETA_NOT_CONFIRMED'", "'ETA_OVERDUE'", "'ELIGIBLE_CONFIRMED'")]
    assert order == sorted(order)
    assert "ev.received_date <= s.ff_snapshot_date THEN 'RECEIVED_IN_POSITION'" in b


def test_calendar_is_real_and_no_thirty_day_months():
    for name in PLAN_VIEWS:
        b = body(name)
        assert "30.44" not in b and "30.4" not in b, name
        assert not re.search(r"\*\s*30\b", b), name
    procs = re.sub(r"--[^\n]*", "", PROCS.read_text(encoding="utf-8"))
    assert "DATE_DIFF(LEAST(LAST_DAY(mo), p_horizon_to), mo, DAY) + 1" in procs
    assert "/ 30" in procs and procs.count("/ 30") == 1                     # только окно темпа: 30 суток / 30


def test_no_revenue_or_economics_in_plan1():
    for name in PLAN_VIEWS:
        cols = " ".join(facts(name).output_columns or [])
        for word in ("gmv", "revenue", "contribution", "margin", "cogs", "price", "profit", "_rub"):
            assert word not in cols, (name, word)


def test_spec_17_is_fully_mapped_to_fixture_scenarios():
    ids = {sid for sid, *_ in pr.SCENARIOS}
    assert len(pr.SPEC_CHECKS) == 23
    for item, blocks in pr.SPEC_CHECKS.items():
        assert blocks and set(blocks) <= ids, (item, blocks)


def test_storage_is_append_only_and_extends_the_existing_ledger():
    text = deploy.storage_script()
    assert deploy._no_strings(text).count("CREATE TABLE IF NOT EXISTS") == 5
    assert "ALTER TABLE `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SALES_PLAN_APPROVAL`" in text
    assert "REF_PLAN_APPROVAL" not in text                                 # второго реестра утверждений нет


def test_deploy_guards():
    assert len(deploy.VIEWS) == 11 and deploy.VIEWS[-1] == ("evetis_mart", "V_PLANNING_HEADER")
    for d, n in deploy.VIEWS:
        assert deploy.view_statement(d, n).count("CREATE OR REPLACE VIEW") == 1
    assert deploy.procedures_script().count("CREATE OR REPLACE PROCEDURE") == 7
    assert deploy.FORBIDDEN_PROC.search("DELETE FROM x") and deploy.FORBIDDEN_PROC.search("UPDATE x SET")
    assert deploy._no_strings("SELECT '\\n#BOM\\n' AS a") == "SELECT '' AS a"


def test_rollback_keeps_owner_data():
    text = ROLLBACK.read_text(encoding="utf-8")
    drops = re.findall(r"DROP (VIEW|PROCEDURE|TABLE) IF EXISTS `[^.]+\.(\w+)\.(\w+)`", text)
    assert {n for k, _, n in drops if k == "VIEW"} == set(PLAN_VIEWS)
    assert {n for k, _, n in drops if k == "PROCEDURE"} == set(deploy.PROCS)
    assert not [d for d in drops if d[0] == "TABLE"]


def test_validation_blocks_are_read_only():
    text = VALIDATION.read_text(encoding="utf-8")
    assert text.count("-- @check P") == 18
    bare = deploy._no_strings(text)
    assert not re.search(r"\b(INSERT|UPDATE|DELETE|MERGE|DROP|CREATE|CALL|ALTER)\b", bare, re.I)
