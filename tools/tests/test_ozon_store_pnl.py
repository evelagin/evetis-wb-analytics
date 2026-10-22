"""D3 «Ozon Магазин P&L»: офлайн-контракт вью, QA и инструмента развёртывания (без BigQuery и сети).

Живые проверки чисел — sql/ozon/qa_ozon_store_pnl_v1.sql (read-only, по одному запросу на проверку).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import ozon_store_pnl_deploy as deploy  # noqa: E402
import validate_current_sql as V  # noqa: E402
from lib.bq_readonly import assert_read_only, blank_string_literals, strip_sql_comments  # noqa: E402

D = ROOT / "sql/current/ozon_mart"
P = "project-fa311fc0-4d87-4781-986"
D3 = sorted(deploy.OBJECTS)
QA = (ROOT / "sql/ozon/qa_ozon_store_pnl_v1.sql").read_text(encoding="utf-8")
MANIFEST = json.loads((D / "MANIFEST.json").read_text(encoding="utf-8"))


def sql(name: str) -> str:
    return (D / f"{name}.sql").read_text(encoding="utf-8")


def body(name: str) -> str:
    text = sql(name)
    return V.view_body(text, V.tokenize(text))


def bare(name: str) -> str:
    return strip_sql_comments(body(name))


# ── Таксономия ─────────────────────────────────────────────────────────────────────────────────────────
MAP_ROW = re.compile(r"\(\s*(?:STRUCT\()?\s*(-?\d+)(?: AS type_id)?,\s*'([^']*)'(?: AS operation_name)?,\s*'([A-Z_]+)'(?: AS pnl_class)?,"
                     r"\s*(NULL|'[A-Z_]+')(?: AS pnl_column)?,\s*'([A-Z_]+)'(?: AS date_basis)?,\s*'([A-Z_]+)'")
OZON_TYPES = {1, 6, 9, 10, 12, 15, 25, 28, 29, 30, 32, 33, 38, 39, 41, 45, 46, 47, 48, 52, 54, 57, 59, 71, 74, 75,
              76, 77, 78, 79, 96, 98, 116, 125}
COLUMN_TYPES = {  # чертёж D3 B.2: колонка → типы
    "E": {32}, "F": {29, 28, 98, 30}, "G": {59, 45, 9, 6, 78}, "H": {1}, "I": {79}, "J": {116, 74, 48},
    "K": {39, 38, 10, 15, 71}, "L": {41, 75, 33}, "L_OR_M": {54}, "N": {47, 96}, "O": {12}, "P": {77}, "Q": {46},
    "R": {76}, "S": {57, 125}, "T": {52}, "U": {25}, "ECON_ONLY": {-1},
}


def map_rows():
    return [m.groups() for m in MAP_ROW.finditer(body("V_OZON_FINANCE_OPERATION_MAP"))]


def test_map_covers_all_34_ozon_types_and_synthetic_rows_once():
    ids = [int(r[0]) for r in map_rows()]
    assert len(ids) == len(set(ids)) == 36
    assert set(ids) == OZON_TYPES | {-1, -2}


def test_each_type_lands_in_exactly_one_blueprint_column():
    got = {}
    for tid, _, _, col, _, _ in map_rows():
        got.setdefault(col.strip("'") if col != "NULL" else None, set()).add(int(tid))
    assert got.pop(None) == {-2}            # синтетический −2 — PENDING до первого доказанного случая
    assert got == COLUMN_TYPES


def test_classes_and_date_bases():
    rows = {int(r[0]): r for r in map_rows()}
    account = {12, 77, 46, 76, 57, 125, 52, 25}
    ads = {41, 75, 33, 54, 47, 96}
    for tid, (_, _, cls, _, basis, sign) in rows.items():
        if tid in account:
            assert cls == "ACCOUNT_LEVEL"
        elif tid in ads:
            assert cls == "ADS_MARKETING"
        elif tid == -2:
            assert cls == "PENDING_CLASSIFICATION"
        else:
            assert cls == "DIRECT_SKU"
        assert sign in {"NEG", "NEG_OR_ZERO", "ZERO", "POS", "ANY"}
    assert rows[52][4] == "SERVICE_PERIOD" and rows[54][4] == "CHARGE_DATE"
    assert {t for t, r in rows.items() if r[4] == "ORDER_MONTH"} == {32, 29, 28, 98, 30, 59, 45, 9, 6, 1, 116, 74,
                                                                    48, 39, 38, 10, -1, -2}
    assert rows[25][5] == "POS" and rows[46][5] == "NEG"     # положительный 46 — сигнал, а не тихий доход


# ── Вью: изоляция, время, ссылки ────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("name", D3)
def test_no_wall_clock_and_no_wb(name):
    b = bare(name).upper()
    assert not re.search(r"CURRENT_(DATE|TIMESTAMP|DATETIME|TIME)\b", b)
    assert ".WB_" not in b and "`WB_" not in b


# 2026-10-16: карта операций изменена Git-first (тип 29 → ANY) и ждёт развёртывания; снимок 15.10 сохранён.
D3_PENDING = {"V_OZON_FINANCE_OPERATION_MAP"}


@pytest.mark.parametrize("name", [n for n in D3 if n not in D3_PENDING])
def test_manifest_entry_is_captured_live_after_deploy(name):
    # Развёрнуто 2026-10-15 из main a7108ab, readback MATCH, снятие R2C — PENDING_DEPLOYED_MATCH.
    o = next(o for o in MANIFEST["objects"] if o["object_name"] == name)
    assert o["sync_state"] == "captured_live" and o["canonical_schema_verification"] == "bigquery_verified"
    assert o["capture_main_sha"] == "a7108ab80bde302f91887c4954ceff6f705d483d"
    assert (o["live_body_sha256_at_capture"], o["live_schema_sha256_at_capture"], o["live_description_sha256_at_capture"]) == (
        o["canonical_body_sha256"], o["canonical_schema_sha256"], o["canonical_description_sha256"])
    assert o["live_schema_at_capture"] == o["canonical_schema"]


@pytest.mark.parametrize("name", sorted(D3_PENDING))
def test_pending_entry_keeps_previous_live_capture(name):
    o = next(o for o in MANIFEST["objects"] if o["object_name"] == name)
    assert o["sync_state"] == "pending_deploy" and o["canonical_schema_verification"] == "unverified"
    # прежнее снятие 15.10 не переписано: тело меняется, схема и описание — нет
    assert o["capture_main_sha"] == "a7108ab80bde302f91887c4954ceff6f705d483d"
    assert o["canonical_body_sha256"] != o["live_body_sha256_at_capture"]
    assert (o["canonical_schema_sha256"], o["canonical_description_sha256"]) == (
        o["live_schema_sha256_at_capture"], o["live_description_sha256_at_capture"])
    assert V.sha256_text(body(name)) == o["canonical_body_sha256"]


def test_existing_l1_l4_and_fct_untouched_by_d3():
    fct = next(o for o in MANIFEST["objects"] if o["object_name"] == "FCT_OZON_PNL_MONTHLY")
    assert fct["sync_state"] == "captured_live"
    assert fct["canonical_body_sha256"] == fct["live_body_sha256_at_capture"]
    assert V.sha256_text(body("FCT_OZON_PNL_MONTHLY")) == fct["canonical_body_sha256"]


# ── P&L: формулы и тождества ────────────────────────────────────────────────────────────────────────────
COSTS = ["product_cogs_rub", "commission_rub", "logistics_rub", "last_mile_rub", "return_logistics_rub",
         "acquiring_rub", "sku_storage_rub", "sku_promotion_rub", "other_direct_rub", "cpc_billing_rub",
         "cpo_billing_rub", "store_ads_rub", "crossdock_rub", "processing_rub", "fbo_placement_rub", "insurance_rub",
         "other_store_costs_rub", "premium_rub"]


def expr(text: str, alias: str) -> str:
    """Выражение, которое заканчивается псевдонимом `alias` (до предыдущей запятой верхнего уровня строки)."""
    m = re.search(r"((?:[^,]|,(?!\s*\n\s*\w+_rub\b))*?)\s" + alias + r"\b", text, re.S)
    assert m, alias
    return m.group(1)


def test_store_contribution_subtracts_every_cost_once_and_adds_u_and_x():
    b = bare("V_OZON_STORE_PNL_MONTHLY")
    v = b[b.index("k AS ("):b.index("store_contribution_pre_tax_rub,")]
    v = v[v.index("seller_base_revenue_rub - product_cogs_rub"):]
    for c in COSTS:
        assert len(re.findall(r"-\s*" + c + r"\b", v)) == 1, c
    assert len(re.findall(r"\+\s*marketplace_compensation_rub\b", v)) == 1
    assert len(re.findall(r"\+\s*provisional_open_rub\b", v)) == 1
    assert "seller_base_revenue_rub * 0.02 management_tax_rub" in b


def test_sku_contribution_uses_attribution_not_billing_and_identity_terms_cancel():
    b = bare("V_OZON_STORE_PNL_MONTHLY")
    aa = b[b.index("seller_base_revenue_rub - product_cogs_rub - commission_rub - logistics_rub - last_mile_rub\n    - return"):
           b.index(" sku_contribution_rub")]
    assert "cpc_attributed_rub" in aa and "cpo_attributed_rub" in aa and "management_tax_rub" in aa
    assert "cpc_billing_rub" not in aa and "premium_rub" not in aa and "crossdock_rub" not in aa
    ab = b[b.index("cpc_attributed_rub + cpo_attributed_rub - cpc_billing_rub"):b.index(" ads_adjustment_rub")]
    assert ab.split() == "cpc_attributed_rub + cpo_attributed_rub - cpc_billing_rub - cpo_billing_rub - store_ads_rub".split()


def test_every_amount_column_of_the_map_is_read_by_the_pnl():
    b = bare("V_OZON_STORE_PNL_MONTHLY")
    for col in "EFGHIJKLMNOPQRSTU":
        assert f"pnl_column = '{col}'" in b, col


def test_ledger_money_identity_and_memo_excluded():
    b = bare("V_OZON_STORE_ACCOUNT_LEDGER")
    assert ("IFNULL(u.seller_base_price_rub * u.qty_used, 0) + IFNULL(u.commission_rub, 0) + IFNULL(u.amount_rub, 0) money_rub"
            in b)
    money = b[b.index("money_rub") - 200:b.index("money_rub")]
    assert "bonus" not in money and "coinvestment" not in money and "buyer_paid" not in money
    assert "COALESCE(a.quantity, q.q, 1) qty_used" in b


def test_premium_split_is_kopeck_exact_by_largest_remainder():
    b = bare("V_OZON_STORE_ACCOUNT_LEDGER")
    assert "ORDER BY frac DESC, m" in b and "IF(rn <= ABS(rem), SIGN(rem), 0)" in b
    assert "GENERATE_DATE_ARRAY(p0, DATE_SUB(p1, INTERVAL 1 DAY))" in b


def test_cogs_not_reversed_on_returns():
    b = bare("V_OZON_STORE_PNL_MONTHLY")
    assert "IF(econ_direction = 'SALE' OR source = 'CIS_DOCUMENT', quantity * product_cogs_rub, 0)) product_cogs_rub" in b
    assert "IF(econ_direction = 'REVERSAL', ABS(quantity) * product_cogs_rub, 0)) returned_units_cogs_not_reversed_rub" in b


# ── Состояние месяца ───────────────────────────────────────────────────────────────────────────────────
def test_state_order_and_watermark():
    b = bare("V_OZON_STORE_MONTH_STATE")
    order = [s for s in re.findall(r"THEN '([A-Z_]+)'", b[b.index("financial_state") - 2000:])
             if s in {"PENDING_CLASSIFICATION", "PARTIAL_SOURCE_MISSING", "UNPROVEN", "PARTIAL_AWAITING_ACCRUALS",
                      "FINANCIAL_COMPLETE_WITH_TIMING_BRIDGE"}]
    assert order == ["PENDING_CLASSIFICATION", "PARTIAL_SOURCE_MISSING", "UNPROVEN", "PARTIAL_AWAITING_ACCRUALS",
                     "FINANCIAL_COMPLETE_WITH_TIMING_BRIDGE"]
    assert "ELSE 'FINANCIAL_COMPLETE' END financial_state" in b
    assert "entity = 'finance_accrual' AND status = 'OK'" in b
    assert "INTERVAL 36 DAY" in b


def test_known_defects_registry_holds_codes_not_amounts():
    b = bare("V_OZON_STORE_MONTH_STATE")
    reg = b[b.index("known_defects AS ("):b.index("runs AS (")]
    assert re.findall(r"DATE '(\d{4}-\d{2}-01)'", reg)
    assert not re.search(r"\d+[.,]\d{2}", reg)             # ни одной суммы — только месяц и код


# ── QA ─────────────────────────────────────────────────────────────────────────────────────────────────
def qa_blocks():
    return {b.split("\n", 1)[0].strip(): b.split("\n", 1)[1] for b in QA.split("-- @check ")[1:]
            if not b.startswith("<")}


def test_qa_has_q01_to_q23_one_read_only_select_each():
    blocks = qa_blocks()
    ids = [k.split("_", 1)[0] for k in blocks]
    assert ids == [f"Q{i:02d}" for i in range(1, 24)]
    for k, s in blocks.items():
        stmt = strip_sql_comments(s).strip()
        assert blank_string_literals(stmt).count(";") == 1 and stmt.endswith(";"), k
        assert_read_only(stmt.rstrip(";"))
        assert f"'{k}' check_name" in s


def test_qa_l1_l4_hash_constant_equals_manifest():
    fct = next(o for o in MANIFEST["objects"] if o["object_name"] == "FCT_OZON_PNL_MONTHLY")
    assert re.findall(r"= '([0-9a-f]{64})'", qa_blocks()["Q15_L1_L4_UNCHANGED"]) == [fct["canonical_body_sha256"]]


# ── Инструмент развёртывания ───────────────────────────────────────────────────────────────────────────
def test_no_view_body_line_ends_with_semicolon_before_the_statement_end():
    # deploy statement() режет оператор на первой «;» в конце строки — комментарий с «;» обрезал бы тело
    for n in D3:
        lines = body(n).rstrip().rstrip(";").splitlines()
        assert not [x for x in lines if x.rstrip().endswith(";")], n


def test_deploy_tool_refuses_after_capture_but_order_and_bodies_hold():
    # Инструмент одноразовый: объекты уже captured_live — повторное развёртывание отказывается до сети.
    with pytest.raises(SystemExit):
        deploy.plan()
    order = [n for n in MANIFEST["rebuild_order"] if n in deploy.OBJECTS]
    assert sorted(order) == D3
    deps = {o["object_name"]: o["dependencies"] for o in MANIFEST["objects"]}
    for i, n in enumerate(order):
        assert all(d in order[:i] for d in deps[n] if d in deploy.OBJECTS), n
    for n in order:
        stmt, b = deploy.statement(deploy.DATASET, n, f"sql/current/{deploy.DATASET}/{n}.sql")
        assert b == body(n).strip().rstrip(";").strip()
        assert stmt.startswith(f"CREATE OR REPLACE VIEW `{P}.ozon_mart.{n}`")


def test_deploy_refuses_wrong_project():
    with pytest.raises(SystemExit):
        deploy.main(["--project", "other-project-123"])


# ── Исправления ревью PR #346 ──────────────────────────────────────────────────────────────────────────
def test_watermark_ignores_finance_control_runs_and_backfill_horizon():
    b = bare("V_OZON_STORE_MONTH_STATE")
    assert "REGEXP_CONTAINS(IFNULL(error_message, ''), r'OZON_FINANCE_') finance_control_event" in b
    assert "MAX(LEAST(source_to, DATE_SUB(DATE(completed_at, 'Europe/Moscow'), INTERVAL 1 DAY))) finance_watermark_date" in b
    assert "FROM runs WHERE NOT finance_control_event" in b
    # окно загрузки с кодом контроля переводит свои месяцы в PARTIAL_SOURCE_MISSING
    assert "finance_control_open_days > 0" in b[b.index("'PENDING_CLASSIFICATION'"):b.index("'PARTIAL_SOURCE_MISSING'")]
    # сутки решает последняя покрывающая успешная загрузка; открытые сутки действуют на все месяцы
    assert "ARRAY_AGG(finance_control_event ORDER BY completed_at DESC LIMIT 1)[OFFSET(0)] finance_control_event" in b
    assert "CROSS JOIN ctrl ct" in b
    assert "finance_control_event AND (source_from IS NULL OR source_to IS NULL OR source_from > source_to)" in b


def test_state_fails_closed_without_watermark():
    b = bare("V_OZON_STORE_MONTH_STATE")
    assert "IFNULL(wm.finance_watermark_date >= DATE_ADD(LAST_DAY(months.m), INTERVAL 36 DAY), FALSE) is_closed" in b
    assert "WHEN finance_watermark_date IS NULL OR pnl_month >= DATE_TRUNC(finance_watermark_date, MONTH) THEN 'OPEN'" in b


def test_provisional_commission_from_payout_and_unknown_input_is_not_zero():
    b = bare("V_OZON_STORE_MONTH_STATE")
    assert "commission_amount_rub" not in b                      # колонка отправлений пуста во всех строках
    assert "CASE WHEN p.payout_rub = 0 THEN 0 ELSE p.price_rub * p.q - p.payout_rub END commission" in b
    assert "SUM(revenue - commission - logistics - cogs - revenue * 0.02) provisional_open_rub" in b
    assert ("COUNTIF(revenue IS NULL OR commission IS NULL OR logistics IS NULL OR cogs IS NULL) provisional_inputs_missing"
            in b)
    assert "provisional_inputs_missing > 0" in b[b.index("'PENDING_CLASSIFICATION'"):b.index("'PARTIAL_SOURCE_MISSING'")]


def test_d1_known_defect_months_clear_only_when_repair_rows_are_loaded():
    b = bare("V_OZON_STORE_MONTH_STATE")
    reg = b[b.index("known_defects AS ("):b.index("runs AS (")]
    d1 = re.findall(r"DATE '(\d{4}-\d{2})-01'(?: AS m)?, 'D1_REVERSAL_NOT_LOADED'(?: AS defect_code)?, TRUE", reg)
    assert len(d1) == 9
    assert "WHERE NOT (k.clears_when_d1_repair_loaded AND IFNULL(g.d1_repair_rows, 0) > 0)" in b


def test_detector_covers_campaign_and_cis_document_anomalies():
    b = bare("V_OZON_FINANCE_NEW_OPERATIONS")
    for code in ("UNKNOWN_TYPE", "PENDING_CLASS", "NAME_CHANGED", "SIGN_UNEXPECTED", "OUTSIDE_VALID_PERIOD",
                 "POSTING_NOT_LINKED", "ECON_ON_UNEXPECTED_TYPE", "PREMIUM_PERIOD_UNPARSED", "CAMPAIGN_UNRESOLVED",
                 "CIS_DOC_WITHOUT_POSTING", "CIS_DOC_WITH_ECON_BLOCK", "CIS_DOC_MULTI_SKU"):
        assert f"'{code}'" in b, code


def test_map_evidence_carries_no_business_amounts():
    refs = re.findall(r"'([^']*)'\)?,?\s*$", body("V_OZON_FINANCE_OPERATION_MAP"), re.M)
    amounts = [r for r in refs if re.search(r"\d[\d ]*,\d{2}", r) and "0,00" not in r]
    assert amounts == []


def test_deploy_requires_live_upstream_views():
    assert deploy.upstream() == ["FCT_OZON_PNL_MONTHLY", "V_OZON_ADS_CPO_ORDERS", "V_OZON_CIS_BUYOUT",
                                 "V_OZON_MART_FRESHNESS"]


def test_type29_sign_any_only_sign_changed():
    """2026-10-16: тип 29 несёт сторно услуги вместе со сторно продажи (положительная строка) — знак ANY,
    как у типа 32. Класс, колонка P&L и база даты не меняются; остальные типы последней мили — NEG."""
    b = body("V_OZON_FINANCE_OPERATION_MAP")
    row = re.search(r"\(29, '([^']*)', '([^']*)', '([^']*)', '([^']*)', '([^']*)',", b)
    assert row and row.groups() == ("Доставка до места выдачи", "DIRECT_SKU", "F", "ORDER_MONTH", "ANY")
    for t in (28, 98, 30):
        assert re.search(rf"\({t}, '[^']*', 'DIRECT_SKU', 'F', 'ORDER_MONTH', 'NEG',", b), t


def test_type125_analytics_lite_is_account_level_other_cost():
    """OWNER ACK 2026-10-20: «Аналитика Lite» — прочие расходы кабинета (колонка S, без Premium), месяц начисления.
    Имя — точное из RAW (контроль NAME_CHANGED), знак NEG (положительная строка — сигнал), сумма в карте не задана;
    Premium (52, SERVICE_PERIOD) и корректировка 57 не меняются."""
    b = body("V_OZON_FINANCE_OPERATION_MAP")
    rows = {int(r[0]): r for r in map_rows()}
    assert rows[125] == ("125", "Аналитика Lite", "ACCOUNT_LEVEL", "'S'", "BOOKING_MONTH", "NEG")
    assert rows[52] == ("52", "Подписка Premium", "ACCOUNT_LEVEL", "'T'", "SERVICE_PERIOD", "NEG")
    assert rows[57] == ("57", "Корректировка стоимости услуг", "ACCOUNT_LEVEL", "'S'", "BOOKING_MONTH", "ANY")
    row = re.search(r"\(125, [^\n]*\),\n", b).group(0)
    assert "4990" not in row and "4 990" not in row
