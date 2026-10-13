"""Офлайн-контракт read-only проверки отпечатка витрины (без BigQuery и без токена)."""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools"))

import mart_watermark_bq_check as chk  # noqa: E402
from lib.bq_readonly import assert_read_only  # noqa: E402


def test_every_scenario_sql_is_read_only():
    assert_read_only(chk.build_sql(chk.BASE))
    for _name, rows, _expect in chk.SCENARIOS:
        assert_read_only(chk.build_sql(rows))


def test_required_scenarios_present():
    names = {n for n, _r, _e in chk.SCENARIOS}
    for required in ("row_order_reversed", "duplicate_row", "duplicate_pair_xor_trap", "swap_for_pay_two_rows",
                     "null_vs_string_null", "remove1_add1_equal_count", "late_correction_2024"):
        assert required in names
    expect = {n: e for n, _r, e in chk.SCENARIOS}
    assert expect["row_order_reversed"] == "SAME"
    assert expect["duplicate_row"] == "DIFF"


def test_null_and_string_null_are_distinct_literals():
    base = chk.source_sql(chk.BASE)
    alt = chk.source_sql(chk._edit(2, deduction="null"))
    assert base != alt and "'null'" in alt


class _FakeBq:
    """Эмулирует ответы BigQuery: одинаковые источники → одинаковая версия, разные → разные."""

    def __init__(self):
        self.queries_issued = 0
        self.bytes_billed = 0

    def query(self, sql):
        import hashlib
        self.queries_issued += 1
        body = sql.split(">>[", 1)[1].split("]))", 1)[0]
        # SAME-сценарии: порядок строк и масштаб NUMERIC моделируем нормализацией
        body = body.replace("NUMERIC '467.660'", "NUMERIC '467.66'")
        items = sorted(body[1:-1].split("), (")) if body else []
        digest = hashlib.sha256("|".join(items).encode()).hexdigest()
        return [{"watermark": f"fin:v2:{len(items)}:{digest}"}]


def test_runner_reports_pass_on_consistent_engine(capsys):
    assert chk.run(_FakeBq(), None) == 0
    assert "ИТОГ: PASS" in capsys.readouterr().out
