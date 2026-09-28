"""SPP-2: контракт wb_mart.V_WB_SPP_DAILY и защиты его развёртывания (офлайн, без BigQuery)."""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
import sqlglot
from sqlglot import exp

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import spp_daily_view_deploy as dep  # noqa: E402
from lib.bq_readonly import strip_sql_comments  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
SQL = REPO / "sql/unitka/spp2/wb_mart/V_WB_SPP_DAILY.sql"
DOC = REPO / "docs/UNITKA_SPP_2_DAILY_VIEW_2026-09-28.md"
CONTRACT = [
    "date_msk", "nm_id", "internal_sku", "orders_qty", "cancelled_orders_qty", "orders_with_spp",
    "raw_spp_avg_pct", "raw_spp_median_pct", "raw_spp_min_pct", "raw_spp_max_pct", "zero_spp_orders",
    "sum_price_with_disc", "sum_finished_price", "effective_spp_pct", "neg_markup_orders",
    "price_missing_rows", "spp_source", "spp_status",
]
STATUSES = {"OK", "ZERO_SPP", "NEGATIVE_MARKUP", "PRICE_DATA_MISSING"}


def _create():
    return sqlglot.parse_one(SQL.read_text(encoding="utf-8"), read="bigquery")


def _select():
    return _create().expression if isinstance(_create().expression, exp.Select) else _create().find(exp.Select)


def _bare():
    return strip_sql_comments(SQL.read_text(encoding="utf-8"))


def test_closed_list_and_single_create():
    assert dep.OBJECTS == [("wb_mart", "V_WB_SPP_DAILY", "sql/unitka/spp2/wb_mart")]
    assert dep.object_path("wb_mart", "V_WB_SPP_DAILY") == SQL
    with pytest.raises(KeyError):
        dep.object_path("wb_mart", "V_DASH_KPI_DAILY")
    assert "CREATE OR REPLACE VIEW" in dep.statement("wb_mart", "V_WB_SPP_DAILY")


def test_statement_refuses_mutations(tmp_path, monkeypatch):
    bad = tmp_path / "V_WB_SPP_DAILY.sql"
    bad.write_text(SQL.read_text(encoding="utf-8") + "\n;DROP VIEW `x.y.z`", encoding="utf-8")
    monkeypatch.setattr(dep, "object_path", lambda d, n: bad)
    with pytest.raises(SystemExit):
        dep.statement("wb_mart", "V_WB_SPP_DAILY")


def test_output_columns_match_contract_in_order():
    outer = _create().expression
    names = [e.alias_or_name for e in outer.expressions]
    assert names == CONTRACT


def test_reads_only_orders_and_channel_map_fully_qualified():
    create = _create()
    target = create.this.find(exp.Table) if not isinstance(create.this, exp.Table) else create.this
    tables = {".".join(p for p in (t.catalog, t.db, t.name) if p) for t in create.expression.find_all(exp.Table)}
    assert (target.db, target.name) == ("wb_mart", "V_WB_SPP_DAILY")
    real = {t for t in tables if t.count(".") == 2}
    assert real == {
        "project-fa311fc0-4d87-4781-986.wb_raw.V_WB_ORDERS",
        "project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP",
    }
    assert not re.search(r"\bozon_", _bare(), re.I)      # изоляция маркетплейсов


def test_single_source_no_fallback():
    bare = _bare()
    literals = set(re.findall(r"'([A-Z_]+)'", bare))
    assert "ORDER_SPP_ACTUAL" in literals
    for banned in ("BUYOUT_FALLBACK", "LAST_KNOWN", "MANUAL", "SHOWCASE_SNAPSHOT", "ESTIMATED"):
        assert banned not in literals
    assert not re.search(r"V_WB_SALES_RETURNS|FACT_SALES|RAW_WB_STOREFRONT", bare, re.I)


def test_status_contract_is_exactly_four_states():
    case = re.search(r"CASE(.*?)END\s+AS\s+spp_status", _bare(), re.S | re.I).group(1)
    assert set(re.findall(r"'([A-Z_]+)'", case)) == STATUSES
    # NULL не маскируется нулём: первая ветка — отсутствие данных
    assert re.search(r"WHEN\s+effective_spp_pct\s+IS\s+NULL\s+THEN\s+'PRICE_DATA_MISSING'", case, re.I)


def test_effective_formula_money_weighted_and_not_clamped():
    bare = _bare()
    assert re.search(r"100\s*\*\s*\(1\s*-\s*sum_finished_price\s*/\s*sum_price_with_disc\)", bare)
    assert re.search(r"SUM\(pwd \* qty\) AS sum_price_with_disc", bare)
    assert re.search(r"SUM\(fp \* qty\) AS sum_finished_price", bare)
    # отрицательное значение не обрезается: ни GREATEST, ни MAX(…, 0) в слое нет
    assert not re.search(r"\bGREATEST\s*\(", bare, re.I)
    assert not re.search(r"\bMAX\s*\([^()]*,\s*0\s*\)", bare, re.I)


def test_population_includes_cancelled_orders():
    # популяция = все строки V_WB_ORDERS дня, как у AA Юнитки: фильтра по отмене нет
    o_cte = re.search(r"WITH o AS \((.*?)\n\),", _bare(), re.S).group(1)
    where = o_cte.split("WHERE", 1)[1]
    assert "is_cancel" not in where
    assert "_order_date IS NOT NULL" in where


def test_zero_spp_is_not_turned_into_null():
    bare = _bare()
    assert "SAFE_CAST(spp AS FLOAT64) AS spp" in bare
    assert not re.search(r"NULLIF\s*\(\s*spp", bare, re.I)


def test_apply_refuses_unmerged_code(monkeypatch):
    calls = {"status --porcelain": "", "rev-parse HEAD": "aaa", "rev-parse origin/main": "bbb"}
    monkeypatch.setattr(dep.d2, "git", lambda *a: calls[" ".join(a)])
    with pytest.raises(SystemExit, match="origin/main"):
        dep.main(["--project", dep.PROJECT, "--apply"])
    calls["status --porcelain"] = " M x"
    with pytest.raises(SystemExit, match="не чистое"):
        dep.main(["--project", dep.PROJECT, "--apply"])


def test_wrong_project_refused():
    with pytest.raises(SystemExit, match="не совпадает"):
        dep.main(["--project", "other"])


def test_stays_outside_sql_current_and_is_documented():
    assert not (REPO / "sql/current/wb_mart").exists()
    text = DOC.read_text(encoding="utf-8")
    for c in CONTRACT:
        assert f"`{c}`" in text, c
