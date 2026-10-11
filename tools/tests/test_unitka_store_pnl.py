"""Phase C (2026-10-09): P&L магазина WB — вью, QA, развёртывание, Terraform. Только офлайн-проверки контракта."""
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import unitka_store_pnl_deploy as t  # noqa: E402

SQL = (ROOT / "sql/unitka/store_pnl_v1.sql").read_text()
QA = (ROOT / "sql/unitka/qa_store_pnl_v1.sql").read_text()
TF = (ROOT / "infra/terraform/unitka_store_pnl.tf").read_text()
STATES = ["LEGACY_PARTIAL_KNOWN_DEFECTS", "FINANCIAL_COMPLETE", "FINANCIAL_COMPLETE_WITH_TIMING_BRIDGE", "PARTIAL_AWAITING_ACCOUNT_INVOICE",
          "PENDING_CLASSIFICATION", "UNKNOWN_COST_PRESENT"]


def bodies():
    return {n: b for n, _, b in t.plan()}


def test_plan_is_the_closed_list_in_dependency_order():
    names = [n for n, _, _ in t.plan()]
    assert names == t.OBJECTS
    b = bodies()
    for i, n in enumerate(names):
        later = [x for x in names[i + 1:] if f".wb_mart.{x}`" in b[n]]
        assert not later, f"{n} читает {later}, которые создаются позже"


def test_views_stay_in_wb_domain():
    # Изоляция маркетплейсов: P&L магазина WB не читает домен Ozon и общих расчётных слоёв.
    for n, b in bodies().items():
        ds = set(re.findall(r"`project-fa311fc0-4d87-4781-986\.([a-z_]+)\.", b))
        assert ds <= {"wb_raw", "wb_mart", "wb_ops"}, (n, ds)


def test_every_statement_is_a_single_view_without_dml():
    for n, stmt, _ in t.plan():
        bare = re.sub(r"--[^\n]*", "", stmt)
        assert len(re.findall(r"\bCREATE\b", bare)) == 1, n
        assert not re.search(r"\b(INSERT|UPDATE|DELETE|MERGE|DROP|ALTER|TRUNCATE|CALL)\b", re.sub(r"'[^']*'", "''", bare)), n


def test_state_machine_has_exactly_the_owner_states_and_unknown_first():
    pnl = bodies()["V_WB_STORE_PNL_MONTHLY"]
    case = re.search(r"CASE WHEN m < DATE '2026-09-01'.*?END financial_state", pnl, re.S).group(0)
    assert set(re.findall(r"'([A-Z_]+)'", case)) == set(STATES)
    order = [case.index(f"'{s}'") for s in ["LEGACY_PARTIAL_KNOWN_DEFECTS", "UNKNOWN_COST_PRESENT", "PENDING_CLASSIFICATION", "PARTIAL_AWAITING_ACCOUNT_INVOICE",
                                           "FINANCIAL_COMPLETE_WITH_TIMING_BRIDGE"]]
    assert order == sorted(order)


def test_timing_bridge_is_outside_net_until_cohort_matures():
    pnl = bodies()["V_WB_STORE_PNL_MONTHLY"]
    net = re.search(r"\n  sku_contribution_rub \+ reconciliation_adjustments_rub.*?management_net_store_profit_rub", pnl, re.S).group(0)
    assert "logistics_adjustment_rub" not in net and "unsettled_margin_rub" not in net
    assert "mature_cohort_adjustment_rub" in net
    assert "IF(cohort_mature, logistics_adjustment_rub - unsettled_margin_rub, 0) mature_cohort_adjustment_rub" in pnl


def test_service_period_rules_r1():
    led = bodies()["V_WB_STORE_ACCOUNT_LEDGER"]
    assert "'INFERRED_PREVIOUS_MONTH'" in led and "'LABEL_PERIOD'" in led
    assert "DATE_SUB(DATE_TRUNC(finance_date, MONTH), INTERVAL 1 MONTH)" in led
    # одна группа захвата на регулярное выражение (BigQuery REGEXP_EXTRACT)
    for rx in re.findall(r"REGEXP_EXTRACT\([^,]+, r'([^']+)'\)", led):
        assert rx.count("(") == 1, rx


def test_reimbursements_are_memo_r2_and_historical_classes_are_period_bounded():
    m = bodies()["V_WB_FINANCE_OPERATION_MAP"]
    for op in ["Возмещение издержек по перевозке/по складским операциям с товаром",
               "Возмещение издержек по перемещению и операционной обработке товара",
               "Возмещение за выдачу и возврат товаров на ПВЗ"]:
        assert re.search(rf"'{re.escape(op)}', 'MEMO_NON_PNL'", m), op
    # H0 (OWNER ACK 11.10): доказанные исторические классы — только в доказанном периоде
    proven = {
        "Коррекция продаж": ("DIRECT_SKU", "2024-09-01", "2025-07-31"),
        "Корректировка эквайринга": ("DIRECT_SKU", "2024-09-01", "2025-01-31"),
        "Добровольная компенсация при возврате": ("ACCOUNT_LEVEL_INCOME", "2024-11-01", "2026-04-30"),
        "Компенсация ущерба": ("ACCOUNT_LEVEL_INCOME", "2024-12-01", "2025-02-28"),
        "Стоимость участия в программе лояльности": ("DIRECT_SKU", "2025-08-01", "2025-09-30"),
    }
    for op, (tr, a, b) in proven.items():
        assert re.search(rf"'{re.escape(op)}', '{tr}', '[A-Z_]+', '[^']*', DATE '{a}', DATE '{b}'\)", m), op
    assert re.search(r"'Сумма удержанная за начисленные баллы программы лояльности', 'PENDING_CLASSIFICATION'", m)
    assert re.search(r"'Компенсация скидки по программе лояльности', 'MEMO_NON_PNL', 'LOYALTY', '[^']*', DATE '2025-08-01', NULL", m)
    cov = bodies()["V_WB_STORE_FINANCE_COVERAGE"]
    assert "WHEN NOT in_period THEN 'PENDING_CLASSIFICATION'" in cov


def test_every_mapped_treatment_has_a_consumer_in_coverage():
    m = bodies()["V_WB_FINANCE_OPERATION_MAP"]
    cov = bodies()["V_WB_STORE_FINANCE_COVERAGE"]
    cohort = bodies()["V_WB_STORE_FINANCE_COHORT_DAILY"]
    for op, tr in re.findall(r"\(?'([^']+)'(?: AS supplier_oper_name)?, '([A-Z_]+)'", m):
        if tr == "DIRECT_SKU":
            assert f"'{op}'" in cov, op
            if op not in ("Хранение", "Коррекция хранения"):
                assert f"'{op}'" in cohort, op


def test_state_flags_are_data_driven_and_null_safe():
    pnl = bodies()["V_WB_STORE_PNL_MONTHLY"]
    assert "CURRENT_DATE" not in pnl
    for f in ["final_month", "final_m20", "final_m46"]:
        assert f"IFNULL(cov.{f}, FALSE)" in pnl, f
    assert "IFNULL(j.snapshot_lcd >= j.month_end, FALSE) sku_month_closed" in pnl
    assert "AND date_msk >= DATE '2026-08-01'" in pnl


def test_qa_is_read_only_blocks_and_covers_c4():
    blocks = re.split(r"^-- @check (\S+)\n", QA, flags=re.M)[1:]
    names = blocks[0::2]
    assert names == ["NEW_FINANCE_OPERATION", "PENDING_CLASSIFICATION_IN_PNL_MONTHS", "SNAPSHOT_FRESH", "SNAPSHOT_KEY_UNIQUE",
                     "FINANCE_COVERAGE", "DEDUCTION_SOURCE_RECONCILIATION", "ORPHAN_FINANCE", "ACCOUNT_SERVICE_PERIOD",
                     "UNKNOWN_ACCOUNT_COST", "LATE_INVOICE_RESTATEMENT", "R2_MEMO_ROW_GUARD", "R2_REIMBURSEMENT_OFFSET", "BRIDGE_ADS", "BRIDGE_STORAGE", "BRIDGE_LOGISTICS_TIMING",
                     "FORMULA_IDENTITY", "ROW_IDENTITY", "STATE_CONSISTENCY", "SHEET_MODEL_GAP"]
    for name, sql in zip(names, blocks[1::2]):
        bare = re.sub(r"--[^\n]*", "", sql).strip()
        assert re.match(r"^(SELECT|WITH)\b", bare), name
        code = re.sub(r"'[^']*'", "''", bare)
        assert code.count(";") == 1 and code.endswith(";"), name
        assert not re.search(r"\b(INSERT|UPDATE|DELETE|MERGE|CREATE|DROP|ALTER|TRUNCATE|CALL)\b", re.sub(r"'[^']*'", "''", bare)), name
        assert f"SELECT '{name}' check_name," in bare and " status," in bare and " detail\nFROM " in bare, name


def test_terraform_scheduler_paused_and_writes_only_two_tables():
    assert re.search(r'resource "google_cloud_scheduler_job" "unitka_store_pnl_prod" \{[^}]*paused\s*=\s*true', TF, re.S)
    grants = re.findall(r'role\s*=\s*"([^"]+)"', TF)
    assert grants == ["roles/bigquery.dataEditor", "roles/run.invoker"]
    assert "google_bigquery_dataset_iam_member" not in TF and "google_project_iam_member" not in TF
    assert 'args  = ["unitka-store-pnl"]' in TF


def test_wrong_project_is_refused_before_network():
    with pytest.raises(SystemExit, match="не совпадает"):
        t.main(["--project", "other"])


def test_no_comment_line_ends_with_semicolon():
    # statement() режет оператор по первой «;» в конце строки: такой комментарий обрезал бы тело вью.
    assert not re.findall(r"^\s*--.*;\s*$", SQL, flags=re.M)
    tails = {n: b.rstrip()[-12:] for n, b in bodies().items()}
    assert tails["V_WB_STORE_PNL_MONTHLY"].endswith("FROM n"), tails


def test_late_invoice_lands_in_its_service_month():
    led = bodies()["V_WB_STORE_ACCOUNT_LEDGER"]
    for col in ["service_month", "booking_date", "source_operation_date", "report_date"]:
        assert re.search(rf"\b{col}\b", led), col
    pnl = bodies()["V_WB_STORE_PNL_MONTHLY"]
    acc = re.search(r"\nacc AS \(.*?GROUP BY 1\)", pnl, re.S).group(0)
    assert "SELECT service_month m," in acc and "WHERE" not in acc.split("FROM")[-1], "кабинет — по месяцу услуги без фильтра по дате проводки"


def test_memo_guard_is_schema_aware_and_fails_closed():
    cov = bodies()["V_WB_STORE_FINANCE_COVERAGE"]
    # MEMO не наследуется: без доказанного основания — PENDING
    assert "map_treatment = 'MEMO_NON_PNL' AND op LIKE 'Возмещение%' AND memo_basis IS NULL THEN 'PENDING_CLASSIFICATION'" in cov
    # camelCase — прежнее тождество; LEGACY snake_case — НДС 1,20 до 2026-01-01, 1,22 после; иначе UNKNOWN → PENDING
    assert "ABS(IFNULL(c_rb, 0) + IFNULL(c_pr, 0) + IFNULL(c_vw, 0) + IFNULL(c_nds, 0))" in cov
    assert "s_vw * IF(booking_date < DATE '2026-01-01', 1.20, 1.22)" in cov
    assert "ELSE 'UNKNOWN' END memo_schema" in cov
    # ПВЗ LEGACY: MEMO только при нулевом денежном эффекте строки
    assert "THEN IF(zero_cash, 'LEGACY_ZERO_CASH', NULL)" in cov
    assert "IFNULL(s_fp, 0) = 0" in cov and "fp + retail + lg + st + ded + pen + acc + addp + rb + comp + oth + acq + ppvz = 0" in cov


def test_management_tax_is_exposed_but_not_in_net():
    pnl = bodies()["V_WB_STORE_PNL_MONTHLY"]
    assert "SUM(realized * tax_per_unit) management_tax_rub" in pnl
    net = re.search(r"\n  sku_contribution_rub \+ reconciliation_adjustments_rub.*?management_net_store_profit_rub", pnl, re.S).group(0)
    assert "management_tax_rub" not in net


H_SQL = (ROOT / "sql/unitka/store_pnl_historical_v1.sql").read_text()


def h_bodies():
    import unitka_store_pnl_h0_deploy as h
    return {n: b for n, _, b in h.plan(False)}


def test_historical_layer_single_authority_and_canonical_sources():
    b = h_bodies()
    hist = b["V_WB_STORE_PNL_HISTORICAL"]
    assert "'HISTORICAL_CANONICAL' authority" in hist and "DATE '2026-05-01' hist_from, DATE '2026-08-01' phase_c_from" in hist
    assert "wb_mart.UNITKA_COGS_EFFECTIVE" in hist and "wb_raw.REF_SKU_MASTER" in hist and "0.02 * seller_base_rub" in hist
    assert "ad_spend_financial_rub" in hist and "V_WB_FINANCE_HISTORICAL_OVERRIDES" in hist
    # формулы листа не задают экономику истории: из снимка — только внешняя реклама
    assert hist.count("UNITKA_SKU_COMPONENTS_DAILY") == 3 and "SUM(ads_out) external_marketing_rub" in hist
    pres = b["V_WB_STORE_PNL_PRESENTATION"]
    assert "'CURRENT_PHASE_C' authority" in pres and pres.count("UNION ALL") == 1
    pnl = bodies()["V_WB_STORE_PNL_MONTHLY"]
    assert "AND date_msk >= DATE '2026-08-01'" in pnl     # Phase C начинается там, где кончается история


def test_wb_loader_views_do_not_read_evetis_ref():
    # 1C1B/1C2A: учётки загрузчиков Unitka не читают evetis_ref — COGS из физической копии в wb_mart.
    for n, body in h_bodies().items():
        code = re.sub(r"--[^\n]*", "", body)
        assert "evetis_ref" not in code, n


def test_historical_joins_fail_closed_and_snapshot_must_cover_history():
    hist = h_bodies()["V_WB_STORE_PNL_HISTORICAL"]
    assert "IF(COUNT(DISTINCT internal_sku) = 1, ANY_VALUE(internal_sku), NULL)" in hist
    assert "IF(COUNT(u.internal_sku) = 1, MAX(u.product_cogs_rub), NULL) cogs_unit" in hist
    assert "FROM (SELECT DISTINCT d, nm_id FROM coh) k" in hist       # связки до (сутки, nm) — без размножения
    assert "MIN(date_msk) <= (SELECT hist_from FROM bounds) covered" in hist
    state = hist.split("CASE WHEN unknown_rub")[1].split("'UNKNOWN_COST_PRESENT'")[0]
    assert "NOT external_marketing_covered" in state and "cohort_join_gap > 0.005" in state


def test_bundle_seed_matches_acked_start_dates():
    seed = (ROOT / "sql/ref/pr_ref_cogs_history.sql").read_text()
    for bc, d in [("BC-28", "2026-05-01"), ("BC-29", "2026-05-01"), ("BC-30", "2026-07-01"), ("BC-31", "2026-07-01")]:
        assert re.search(rf"^\('{bc}','[^']+','[^']+',1,DATE '{d}'", seed, flags=re.M), bc


def test_inventory_loss_reads_private_evidence_and_is_not_netted():
    b = h_bodies()
    items = b["V_WB_INVENTORY_LOSS_ITEMS"]
    assert "wb_ops.WB_INVENTORY_LOSS_EVIDENCE" in items and "UNNEST" not in items       # доказательства — не в Git
    assert "wb_mart.UNITKA_COGS_EFFECTIVE" in items and "IF(COUNT(u.internal_sku) = 1, MAX(u.product_cogs_rub), NULL)" in items
    mon = b["V_WB_INVENTORY_LOSS_MONTHLY"]
    assert "_snapshot_date >= DATE '2026-08-26'" in mon and "ABS(pos.lost_qty - IFNULL(r.q, 0))" in mon   # отчёт = остаток 26.08
    assert "V_WB_ORDERS" not in mon                                           # заказы с 14.08 без склада — не доказательство
    ev = b["V_WB_INVENTORY_LOSS_COMPENSATION_EVIDENCE"]
    assert "wb_ops.WB_INVENTORY_LOSS_COMPENSATION_EVIDENCE" in ev
    assert "IF(attribution_basis = 'DOCUMENT_LINKED', attributed_incident_month, NULL)" in ev   # месяц события — только по документу
    claim = b["V_WB_INVENTORY_LOSS_CLAIM_TRACKING"]
    assert "outstanding_claim_rub" in claim and "claim_basis_difference_rub" in claim
    for n in ("V_WB_STORE_PNL_MONTHLY", "V_WB_STORE_PNL_HISTORICAL", "V_WB_STORE_PNL_PRESENTATION"):
        assert "CLAIM" not in re.sub(r"--[^\n]*", "", b[n]), n                 # претензия не входит в P&L
    for n in ("V_WB_STORE_PNL_MONTHLY", "V_WB_STORE_PNL_HISTORICAL"):
        body = re.sub(r"--[^\n]*", "", b[n])
        assert "- inventory_loss_writeoff_rub + wb_loss_compensation_rub management_net_store_profit_rub" in body, n
        assert "inventory_loss_cogs_missing_qty > 0 OR inventory_loss_double_count_risk_qty > 0" in body, n
        assert "WHEN pending_rows > 0 OR loss_compensation_unreconciled_rows > 0 THEN 'PENDING_CLASSIFICATION'" in body, n
        assert "NOT IFNULL(loss_evidence_matched, FALSE)" in body, n
    led = b["V_WB_STORE_ACCOUNT_LEDGER"]
    assert "WHEN deduction_class = 'FORCE_MAJEURE_PAYMENT' THEN 'LOSS_COMPENSATION_INCOME'" in led
    assert "WHEN 'FORCE_MAJEURE_PAYMENT' THEN 'LOSS_COMPENSATION'" in led
    assert "V_WB_INVENTORY_LOSS_COMPENSATION_EVIDENCE" in led and "e.attributed_incident_month IS NOT NULL AND le.loss_evidence_matched" in led


def test_public_tree_holds_no_itemized_loss_or_claim_evidence():
    # OWNER DECISION 11.10: поштучные доказательства, документы WB и претензия — только в закрытых таблицах wb_ops.
    paths = [p for d in ("sql", "docs", "tools", "cloud/src", "cloud/test", "infra") for p in (ROOT / d).rglob("*")
             if p.is_file() and p.suffix in {".sql", ".md", ".py", ".ts", ".tf", ".json"}]
    paths.append(ROOT / "CHANGELOG.md")
    # Вне области: отчёт Ozon 3.4A (уже в main до H0) упоминает сумму претензии — отдельное решение владельца.
    skip = {ROOT / "tools/tests/test_unitka_store_pnl.py", ROOT / "docs/ozon/STAGE_3_4A_COGS_PROVENANCE_REPORT.md"}
    paths = [p for p in paths if p not in skip]
    bad = []
    for p in paths:
        t = p.read_text(errors="ignore")
        for pat in (r"damaged-product-report", r"FM-2026-0\d-\d\d-[A-Z]{3}", r"\b2270352\b", r"\b2336254\b", r"\b91705474\b",
                    r"698[ \u00a0]?420[,.]03", r"628[ \u00a0]?940[,.]31"):
            if re.search(pat, t):
                bad.append(f"{p.relative_to(ROOT)}: {pat}")
    assert not bad, bad


def test_inventory_loss_tables_are_read_only_for_the_loader():
    tf = (ROOT / "infra/terraform/wb_inventory_loss.tf").read_text()
    tables = re.findall(r'table_id\s*=\s*"(WB_INVENTORY_LOSS_[A-Z_]+)"', tf)
    assert sorted(tables) == ["WB_INVENTORY_LOSS_CLAIM", "WB_INVENTORY_LOSS_COMPENSATION_EVIDENCE", "WB_INVENTORY_LOSS_EVIDENCE"]
    assert tf.count("deletion_protection = true") == 3
    grants = re.findall(r'resource "google_bigquery_table_iam_member" "(\w+)"', tf)
    assert grants == ["wb_inventory_loss_read"]
    assert re.findall(r'role\s*=\s*"([^"]+)"', tf) == ["roles/bigquery.dataViewer"]
    block = tf.split('resource "google_bigquery_table_iam_member"')[1]
    assert "claim" not in block.lower()                                         # претензию загрузчик не читает


def test_overrides_are_the_8_proven_review_points_rows():
    ov = h_bodies()["V_WB_FINANCE_HISTORICAL_OVERRIDES"]
    keys = re.findall(r"\('(\d+#\d+)', 'REVIEW_POINTS', 'ACCOUNT_LEVEL', DATE '(2026-0[56])-01'", ov)
    assert len(keys) == 8 and len({k for k, _ in keys}) == 8
    assert sorted(m for _, m in keys) == ["2026-05"] * 6 + ["2026-06"] * 2
    assert "amount" not in ov.split("UNNEST")[1].split("]")[0].lower()   # override не несёт сумм


def test_h0_deploy_tool_order_and_rollback_bodies_exist():
    import unitka_store_pnl_h0_deploy as h
    names = [n for n, _, _ in h.plan(False)]
    k = len(h.LOSS)
    assert names[:k] == [n for n, _ in h.LOSS] and names[k:k + 6] == [n for n, _ in h.CHANGED] and names[k + 6:] == [n for n, _ in h.NEW]
    rb = [n for n, _, _ in h.plan(True)]
    assert rb == [n for n, _ in reversed(h.CHANGED)]
    b = {n: body for n, _, body in h.plan(False)}
    for i, n in enumerate(names):
        later = [x for x in names[i + 1:] if f".wb_mart.{x}`" in b[n]]
        assert not later, f"{n} читает {later}"
    with pytest.raises(SystemExit, match="не совпадает"):
        h.main(["--project", "other"])


def test_historical_qa_is_read_only_blocks():
    qa = (ROOT / "sql/unitka/qa_store_pnl_historical_v1.sql").read_text()
    blocks = re.split(r"^-- @check (\S+)\n", qa, flags=re.M)[1:]
    names = blocks[0::2]
    assert names == ["SINGLE_AUTHORITY", "OVERRIDE_INTEGRITY", "HIST_CONSERVATION", "FINANCE_COVERAGE_ALL_HISTORY", "LEGACY_MEMO_GUARD",
                     "COGS_COVERAGE", "HIST_INPUTS_COMPLETE", "TAX_NOT_DOUBLE_COUNTED", "LOSS_EVIDENCE", "LOSS_NO_DOUBLE_COUNT",
                     "LOSS_COMPENSATION_RECONCILED", "LOSS_COMPENSATION_IN_PNL", "LOSS_SINGLE_SOURCE", "ADS_RECONCILES", "STORAGE_RECONCILES", "HIST_STATE"]
    assert "o.service_month >= DATE '2026-08-01' OR l.base_service_month >= DATE '2026-08-01'" in qa   # override только до Phase C
    for name, sql in zip(names, blocks[1::2]):
        bare = re.sub(r"--[^\n]*", "", sql).strip()
        code = re.sub(r"'[^']*'", "''", bare)
        assert re.match(r"^(SELECT|WITH)\b", bare) and code.count(";") == 1, name
        assert not re.search(r"\b(INSERT|UPDATE|DELETE|MERGE|CREATE|DROP|ALTER|TRUNCATE|CALL)\b", code), name


def test_no_comment_line_ends_with_semicolon_in_historical_sql():
    assert not re.findall(r"^\s*--.*;\s*$", H_SQL, flags=re.M)


def test_no_comment_line_ends_with_semicolon_in_loss_sql():
    loss = (ROOT / "sql/unitka/store_inventory_loss_v1.sql").read_text()
    assert not re.findall(r"^\s*--.*;\s*$", loss, flags=re.M)
