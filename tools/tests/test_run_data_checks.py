"""Тесты исполнителя проверок данных. Офлайн: BigQuery не вызывается.

Зелёные ворота, которые не умеют краснеть, хуже отсутствия ворот. Поэтому здесь
проверяется прежде всего то, что исполнитель ОТКЛОНЯЕТ: сломанный контракт файла,
мутирующий SQL, пустой результат, отсутствие колонки `status`, чужое значение статуса.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS))

import run_data_checks as R  # noqa: E402
from lib.bq_readonly import mask_sql_comments  # noqa: E402


# --------------------------------------------------------------- маска/сплит ---

def test_mask_preserves_length_and_hides_comments():
    text = "SELECT 1 -- ; DROP\nFROM t /* ; */ WHERE x = ';'"
    masked = mask_sql_comments(text)
    assert len(masked) == len(text)
    assert "DROP" not in masked
    assert masked.count(";") == 1  # только точка с запятой внутри строкового литерала


def test_split_ignores_semicolons_in_strings_and_comments():
    block = "SELECT ';' AS a -- ; comment\nFROM t;"
    assert R.split_statements(block) == ["SELECT ';' AS a -- ; comment\nFROM t"]


def test_split_returns_two_real_statements():
    assert len(R.split_statements("SELECT 1; SELECT 2;")) == 2


# ------------------------------------------------------------------- парсер ---

def test_parse_ok():
    checks = R.parse_checks("шапка\n-- @check A_ONE\nSELECT 1 status;\n", "f.sql")
    assert [c["check_id"] for c in checks] == ["A_ONE"]
    assert checks[0]["expect_empty_ok"] is False


def test_parse_marks_empty_ok():
    checks = R.parse_checks("-- @check A\n-- @expect empty_ok\nSELECT 1 status;", "f.sql")
    assert checks[0]["expect_empty_ok"] is True


@pytest.mark.parametrize("text, fragment", [
    ("SELECT 1;", "нет ни одного маркера"),
    ("-- @check A\nSELECT 1 status;\n-- @check A\nSELECT 2 status;", "повторяющийся"),
    ("-- @check A\nSELECT 1 status; SELECT 2 status;", "ровно один оператор"),
    ("-- @check A\nDROP TABLE t;", "отклонён до отправки"),
    ("-- @check A\nSELECT 1 status; DELETE FROM t;", "ровно один оператор"),
    ("-- @check A\nCALL `x.sp`();", "отклонён до отправки"),
])
def test_parse_rejects_broken_contract(text, fragment):
    with pytest.raises(R.SuiteContractError) as e:
        R.parse_checks(text, "f.sql")
    assert fragment in str(e.value)


# --------------------------------------------------------------- исполнение ---

class FakeBQ:
    """Подставной клиент: отдаёт заранее заданные строки или бросает ошибку."""

    def __init__(self, rows=None, error=None):
        self._rows, self._error, self.queries_issued = rows or [], error, 0

    def query(self, sql, **kw):
        self.queries_issued += 1
        if self._error:
            raise self._error
        return self._rows


def _check(expect_empty_ok=False):
    return {"check_id": "C", "source": "f.sql", "line": 1,
            "sql": "SELECT 1", "expect_empty_ok": expect_empty_ok}


def test_all_pass():
    r = R.run_check(FakeBQ([{"status": "PASS"}, {"status": "PASS"}]), _check(), 5)
    assert r["status"] == R.PASS and r["failing_rows"] == 0


def test_one_fail_fails_the_check():
    r = R.run_check(FakeBQ([{"status": "PASS"}, {"status": "FAIL", "month": "2026-08"}]), _check(), 5)
    assert r["status"] == R.FAIL
    assert r["failing_rows"] == 1
    assert r["failing_sample"][0]["month"] == "2026-08"


def test_empty_result_is_not_success():
    r = R.run_check(FakeBQ([]), _check(), 5)
    assert r["status"] == R.EMPTY


def test_empty_result_allowed_when_declared():
    assert R.run_check(FakeBQ([]), _check(expect_empty_ok=True), 5)["status"] == R.PASS


def test_missing_status_column_is_error():
    r = R.run_check(FakeBQ([{"rows": "1"}]), _check(), 5)
    assert r["status"] == R.ERROR and "status" in r["error"]


def test_unknown_status_value_is_error_not_fail():
    r = R.run_check(FakeBQ([{"status": "WARN"}]), _check(), 5)
    assert r["status"] == R.ERROR


def test_bigquery_error_is_error():
    from lib.bq_readonly import BigQueryError
    r = R.run_check(FakeBQ(error=BigQueryError("HTTP 404")), _check(), 5)
    assert r["status"] == R.ERROR and "404" in r["error"]


def test_failing_sample_is_capped():
    rows = [{"status": "FAIL", "i": str(i)} for i in range(50)]
    r = R.run_check(FakeBQ(rows), _check(), 3)
    assert r["failing_rows"] == 50 and len(r["failing_sample"]) == 3


# ----------------------------------------------------------------- вердикт ---

@pytest.mark.parametrize("statuses, expected", [
    ([R.PASS, R.PASS], R.PASS),
    ([R.PASS, R.EMPTY], R.EMPTY),
    ([R.PASS, R.FAIL, R.EMPTY], R.FAIL),
    ([R.FAIL, R.ERROR], R.ERROR),
    ([], R.PASS),
])
def test_verdict_precedence(statuses, expected):
    assert R.verdict([{"status": s} for s in statuses]) == expected


def test_exit_codes_are_distinct():
    assert sorted(R.EXIT.values()) == [0, 1, 2, 3]


# ------------------------------------------------------------------ реестр ---

def test_registry_files_exist_and_parse():
    reg = json.loads((TOOLS.parent / "quality" / "suites.json").read_text(encoding="utf-8"))
    assert reg["suites"], "реестр наборов пуст"
    for suite in reg["suites"]:
        for rel in suite["files"]:
            path = TOOLS.parent / rel
            assert path.exists(), f"{suite['suite']}: нет файла {rel}"
            checks = R.parse_checks(path.read_text(encoding="utf-8"), rel)
            assert checks, f"{suite['suite']}: в {rel} нет проверок"


def test_backlog_files_exist():
    """Список долга должен указывать на существующие файлы, иначе он врёт о размере долга."""
    reg = json.loads((TOOLS.parent / "quality" / "suites.json").read_text(encoding="utf-8"))
    for rel in reg["backlog"]["files"]:
        assert (TOOLS.parent / rel).exists(), f"в долге указан несуществующий файл {rel}"


def test_backlog_and_suites_do_not_overlap():
    reg = json.loads((TOOLS.parent / "quality" / "suites.json").read_text(encoding="utf-8"))
    in_suites = {f for s in reg["suites"] for f in s["files"]}
    assert not (in_suites & set(reg["backlog"]["files"])), "файл одновременно в воротах и в долге"
