"""Контракт адаптера `-- @check` и разбора операторов. Офлайн.

Поведение исполнителя (PASS / FAIL / EMPTY / ERROR / BLOCKED, вердикт набора,
коды выхода) проверяется в test_evidence_system.py; адаптеры ASSERT и
verdict_select — в test_acceptance_adapters.py. Здесь остаётся только то, что
касается исходного формата SCALE 1 и резки текста на операторы.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS))

import run_data_checks as R  # noqa: E402
from lib.acceptance import SuiteContractError, parse_check_blocks, split_statements  # noqa: E402
from lib.bq_readonly import mask_sql_comments  # noqa: E402


# --------------------------------------------------------------- маска/резка ---

def test_mask_preserves_length_and_hides_comments():
    text = "SELECT 1 -- ; DROP\nFROM t /* ; */ WHERE x = ';'"
    masked = mask_sql_comments(text)
    assert len(masked) == len(text)          # иначе смещения врут и SQL режется не там
    assert "DROP" not in masked
    assert masked.count(";") == 1            # только внутри строкового литерала


def test_split_ignores_semicolons_in_strings_and_comments():
    block = "SELECT ';' AS a -- ; comment\nFROM t;"
    assert split_statements(block) == ["SELECT ';' AS a -- ; comment\nFROM t"]


def test_split_returns_two_real_statements():
    assert len(split_statements("SELECT 1; SELECT 2;")) == 2


# ------------------------------------------------------- адаптер check_blocks ---

def test_parse_ok():
    checks = parse_check_blocks("шапка\n-- @check A_ONE\nSELECT 1 status;\n", "f.sql").checks
    assert [c.check_id for c in checks] == ["A_ONE"]
    assert checks[0].expect_empty_ok is False


def test_parse_marks_empty_ok():
    checks = parse_check_blocks("-- @check A\n-- @expect empty_ok\nSELECT 1 status;", "f.sql").checks
    assert checks[0].expect_empty_ok is True


@pytest.mark.parametrize("text, fragment", [
    ("SELECT 1;", "нет ни одного маркера"),
    ("-- @check A\nSELECT 1 status;\n-- @check A\nSELECT 2 status;", "повторяющийся"),
    ("-- @check A\nSELECT 1 status; SELECT 2 status;", "ровно один оператор"),
    ("-- @check A\nDROP TABLE t;", "отклонён до отправки"),
    ("-- @check A\nCALL `x.sp`();", "отклонён до отправки"),
])
def test_parse_rejects_broken_contract(text, fragment):
    with pytest.raises(SuiteContractError) as e:
        parse_check_blocks(text, "f.sql")
    assert fragment in str(e.value)


def test_legacy_parse_checks_alias_still_returns_checks():
    """`parse_checks` вызывался напрямую до введения адаптеров — совместимость сохранена."""
    assert [c.check_id for c in R.parse_checks("-- @check A\nSELECT 1 status;", "f.sql")] == ["A"]


# ------------------------------------------------------------------- реестр ---

def test_registry_files_exist_and_parse():
    reg = json.loads((TOOLS.parent / "quality" / "suites.json").read_text(encoding="utf-8"))
    assert reg["registry_version"] == 2
    assert reg["suites"], "реестр наборов пуст"
    for suite in reg["suites"]:
        for rel in suite["files"]:
            assert (TOOLS.parent / rel).exists(), f"{suite['suite']}: нет файла {rel}"
        assert R.parse_suite(suite).checks, f"{suite['suite']}: ни одной проверки"


def test_every_suite_declares_the_fields_the_registry_promises():
    reg = json.loads((TOOLS.parent / "quality" / "suites.json").read_text(encoding="utf-8"))
    required = {"suite", "title", "gate", "severity", "domain", "marketplace", "pipelines",
                "adapter", "execution_mode", "expected_result", "owner", "files", "scope",
                "source_contract", "cadence", "provenance"}
    for s in reg["suites"]:
        assert required <= set(s), f"{s['suite']}: нет полей {sorted(required - set(s))}"
        assert s["severity"] in ("CRITICAL", "HIGH", "MEDIUM", "LOW")
        assert s["adapter"] in ("check_blocks", "assert_script", "verdict_select")


def test_backlog_records_why_each_artifact_is_not_wired():
    reg = json.loads((TOOLS.parent / "quality" / "suites.json").read_text(encoding="utf-8"))
    for b in reg["backlog"]["files"]:
        assert (TOOLS.parent / b["file"]).exists(), f"нет файла {b['file']}"
        assert b.get("blocker"), f"{b['file']}: в долге нет причины неподключения"
        assert b.get("measured_2026_09_21"), f"{b['file']}: долг без измеренного результата"
