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


def test_reimbursements_are_memo_r2_and_pending_ops_stay_pending():
    m = bodies()["V_WB_FINANCE_OPERATION_MAP"]
    for op in ["Возмещение издержек по перевозке/по складским операциям с товаром",
               "Возмещение издержек по перемещению и операционной обработке товара",
               "Возмещение за выдачу и возврат товаров на ПВЗ"]:
        assert re.search(rf"'{re.escape(op)}', 'MEMO_NON_PNL'", m), op
    # операции, которые модель не читает, утверждёнными быть не могут (ревью M2)
    for op in ["Коррекция продаж", "Корректировка эквайринга", "Добровольная компенсация при возврате", "Компенсация ущерба",
               "Стоимость участия в программе лояльности", "Сумма удержанная за начисленные баллы программы лояльности"]:
        assert re.search(rf"'{re.escape(op)}', 'PENDING_CLASSIFICATION'", m), op
    assert "ACCOUNT_LEVEL_INCOME" not in m


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


def test_memo_is_not_inherited_without_row_identity():
    cov = bodies()["V_WB_STORE_FINANCE_COVERAGE"]
    assert "m.treatment = 'MEMO_NON_PNL' AND (f.memo_offset_abs > 0.01 OR f.fp != 0) THEN 'PENDING_CLASSIFICATION'" in cov
