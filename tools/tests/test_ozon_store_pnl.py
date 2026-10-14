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
              76, 77, 78, 79, 96, 98, 116}
COLUMN_TYPES = {  # чертёж D3 B.2: колонка → типы
    "E": {32}, "F": {29, 28, 98, 30}, "G": {59, 45, 9, 6, 78}, "H": {1}, "I": {79}, "J": {116, 74, 48},
    "K": {39, 38, 10, 15, 71}, "L": {41, 75, 33}, "L_OR_M": {54}, "N": {47, 96}, "O": {12}, "P": {77}, "Q": {46},
    "R": {76}, "S": {57}, "T": {52}, "U": {25}, "ECON_ONLY": {-1},
}


def map_rows():
    return [m.groups() for m in MAP_ROW.finditer(body("V_OZON_FINANCE_OPERATION_MAP"))]


def test_map_covers_all_33_ozon_types_and_synthetic_rows_once():
    ids = [int(r[0]) for r in map_rows()]
    assert len(ids) == len(set(ids)) == 35
    assert set(ids) == OZON_TYPES | {-1, -2}


def test_each_type_lands_in_exactly_one_blueprint_column():
    got = {}
    for tid, _, _, col, _, _ in map_rows():
        got.setdefault(col.strip("'") if col != "NULL" else None, set()).add(int(tid))
    assert got.pop(None) == {-2}            # синтетический −2 — PENDING до первого доказанного случая
    assert got == COLUMN_TYPES


def test_classes_and_date_bases():
    rows = {int(r[0]): r for r in map_rows()}
    account = {12, 77, 46, 76, 57, 52, 25}
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


@pytest.mark.parametrize("name", D3)
def test_manifest_entry_is_new_pending_object(name):
    o = next(o for o in MANIFEST["objects"] if o["object_name"] == name)
    assert o["sync_state"] == "pending_deploy" and o["canonical_schema_verification"] == "unverified"
    assert all(o[k] is None for k in ("capture_main_sha", "captured_at", "live_body_sha256_at_capture",
                                      "live_schema_sha256_at_capture", "live_description_sha256_at_capture",
                                      "live_schema_at_capture"))


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
    reg = b[b.index("known_defects AS ("):b.index("kd AS (")]
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
def test_deploy_plan_is_closed_list_in_dependency_order_with_canonical_bodies():
    views = deploy.plan()
    names = [n for n, _, _ in views]
    assert sorted(names) == D3
    deps = {o["object_name"]: o["dependencies"] for o in MANIFEST["objects"]}
    for i, n in enumerate(names):
        assert all(d in names[:i] for d in deps[n] if d in deploy.OBJECTS), n
    for n, stmt, b in views:
        assert b == body(n).strip().rstrip(";").strip()
        assert stmt.startswith(f"CREATE OR REPLACE VIEW `{P}.ozon_mart.{n}`")


def test_deploy_refuses_wrong_project():
    with pytest.raises(SystemExit):
        deploy.main(["--project", "other-project-123"])
