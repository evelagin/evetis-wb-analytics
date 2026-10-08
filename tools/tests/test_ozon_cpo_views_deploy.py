"""Phase B (2026-10-08): развёртывание вью «Оплаты за заказ» и QA — только офлайн-проверки контракта."""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import ozon_cpo_views_deploy as t  # noqa: E402


def test_forward_order_follows_dependency_graph():
    names = [n for n, _, _ in t.plan(False)]
    assert names[0] == "V_OZON_ADS_CPO_ORDERS"
    assert names.index("FCT_OZON_SKU_PNL_DAILY") < names.index("V_OZON_SKU_PNL_DAILY_OPERATIONAL")
    assert set(names) == set(t.NEW) | set(t.CHANGED)


def test_rollback_restores_only_changed_views_with_pre_change_bodies():
    rb = {n: b for n, _, b in t.plan(True)}
    assert set(rb) == set(t.CHANGED)
    fwd = {n: b for n, _, b in t.plan(False)}
    for n in t.CHANGED:
        assert "cpo_expense_rub" in fwd[n] and "cpo_expense_rub" not in rb[n]


def test_new_columns_only_at_the_tail():
    for n in t.CHANGED:
        fwd = {k: b for k, _, b in t.plan(False)}[n].rstrip()
        assert re.search(r"cpo_expense_rub\s*\nFROM (j|r)\s*(\nWHERE[^\n]*)?$", fwd), n


def test_qa_file_is_read_only():
    text = (ROOT / "sql/ozon/qa_cpo_orders_v1.sql").read_text()
    blocks = re.split(r"^-- @check \S+\n", text, flags=re.M)[1:]
    assert len(blocks) == 13
    for b in blocks:
        sql = re.sub(r"--[^\n]*", "", b).strip()
        assert re.match(r"^(SELECT|WITH)\b", sql), sql[:60]
        assert not re.search(r"\b(INSERT|UPDATE|DELETE|MERGE|CREATE|DROP|ALTER|TRUNCATE|CALL)\b", sql), sql[:60]
