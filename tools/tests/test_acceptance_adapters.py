"""Тесты адаптеров приёмочных артефактов. Офлайн: BigQuery не вызывается.

Адаптер переводит уже согласованное условие в исполнимую форму. Поэтому проверяется
прежде всего то, что он НЕ делает: не меняет выражение, не теряет оператор молча,
не пропускает мутацию и не выдаёт нераспознанное за проверку.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS))

from lib import acceptance as A  # noqa: E402
from lib.bq_readonly import ReadOnlyViolation, assert_read_only, blank_string_literals  # noqa: E402


# ------------------------------------------------------------ assert_script ---

def test_assert_becomes_select_without_changing_the_condition():
    src = "-- шапка\nASSERT (SELECT COUNT(*) = 0 FROM `p.d.t`) AS 'T1 сломалось';"
    res = A.parse_assert_script(src, "f.sql", prefix="X")
    assert len(res.executable) == 1
    c = res.executable[0]
    assert c.label == "T1 сломалось"
    assert "SELECT COUNT(*) = 0 FROM `p.d.t`" in c.sql   # выражение дословно
    assert c.sql.lstrip().upper().startswith("SELECT IF(")
    assert "ASSERT" not in c.sql.upper()


def test_assert_with_condition_outside_parentheses():
    """`ASSERT (подзапрос) = 0 AS '…'` — условие целиком, а не только подзапрос."""
    src = "ASSERT (SELECT COUNT(*) FROM `p.d.t`) = 0 AS 'DQ-1';"
    c = A.parse_assert_script(src, "f.sql").executable[0]
    assert "(SELECT COUNT(*) FROM `p.d.t`) = 0" in c.sql


def test_assert_name_is_found_past_nested_as_aliases():
    src = "ASSERT (SELECT x AS y FROM (SELECT 1 AS x)) AS 'настоящее имя';"
    c = A.parse_assert_script(src, "f.sql").executable[0]
    assert c.label == "настоящее имя"


def test_assert_keyword_inside_comment_is_not_the_statement():
    src = "-- раньше тут был ASSERT\nSELECT 1;"
    res = A.parse_assert_script(src, "f.sql")
    assert res.executable == []
    assert "не ASSERT" in res.not_executable[0].not_executable_reason


def test_assert_without_name_is_not_executable_not_dropped():
    res = A.parse_assert_script("ASSERT (SELECT TRUE);", "f.sql")
    assert res.executable == []
    assert len(res.not_executable) == 1
    assert "нет имени" in res.not_executable[0].not_executable_reason


def test_label_quote_is_escaped_not_dropped():
    c = A.parse_assert_script("ASSERT (SELECT TRUE) AS 'it\\'s broken';", "f.sql").executable[0]
    assert_read_only(c.sql)          # не должно ломать оператор
    assert "\\'" in c.sql


def test_trailing_line_comment_in_expression_does_not_eat_the_verdict():
    src = "ASSERT (SELECT TRUE -- пояснение\n) AS 'T';"
    c = A.parse_assert_script(src, "f.sql").executable[0]
    assert c.sql.rstrip().endswith("AS check_label")
    assert_read_only(c.sql)


def test_ids_are_stable_and_unique():
    src = "ASSERT (SELECT TRUE) AS 'одно и то же';\nASSERT (SELECT TRUE) AS 'одно и то же';"
    ids = [c.check_id for c in A.parse_assert_script(src, "f.sql", prefix="P").checks]
    assert len(set(ids)) == 2
    assert all(i.startswith("P__") for i in ids)
    again = [c.check_id for c in A.parse_assert_script(src, "f.sql", prefix="P").checks]
    assert ids == again                      # воспроизводимость


# ------------------------------------------------------------ безопасность ---

def test_keyword_inside_string_literal_is_not_a_mutation():
    """Проверка, сторожащая мутацию, сама обязана проходить read-only гейт."""
    assert_read_only("SELECT routine_definition LIKE '%TRUNCATE%' AS x FROM `p.d.r`")
    assert_read_only("SELECT REPLACE(x, ',', '.') FROM `p.d.t`")


@pytest.mark.parametrize("sql", [
    "DROP TABLE `p.d.t`",
    "SELECT 1; TRUNCATE TABLE `p.d.t`",
    "CREATE OR REPLACE VIEW `p.d.v` AS SELECT 1",
    "MERGE `p.d.t` USING `p.d.s` ON TRUE WHEN MATCHED THEN DELETE",
    "EXECUTE IMMEDIATE 'DROP TABLE x'",
])
def test_real_mutations_still_rejected(sql):
    with pytest.raises(ReadOnlyViolation):
        assert_read_only(sql)


def test_blanking_preserves_length_and_hides_content():
    t = "SELECT 'DROP TABLE x' AS a, 'b\\'c' AS d"
    b = blank_string_literals(t)
    assert len(b) == len(t)
    assert "DROP" not in b


def test_mutating_assert_is_reported_not_silently_skipped():
    res = A.parse_assert_script("ASSERT (SELECT 1) = (DELETE FROM t) AS 'плохо';", "f.sql")
    assert res.executable == []
    assert "мутирующая" in res.not_executable[0].not_executable_reason


# ----------------------------------------------------------- verdict_select ---

def test_verdict_select_requires_the_declared_column():
    src = ("SELECT 'a' AS check_name, IF(TRUE,'PASS','FAIL') AS verdict FROM `p.d.t`;\n"
           "SELECT 'b' AS check_name, COUNT(*) AS n FROM `p.d.t`;")
    res = A.parse_verdict_select(src, "f.sql", status_column="verdict")
    assert len(res.executable) == 1
    assert "нет колонки `verdict`" in res.not_executable[0].not_executable_reason


def test_verdict_select_label_comes_from_first_literal():
    c = A.parse_verdict_select("SELECT 'S-1 проверка' AS n, 'PASS' AS verdict;", "f.sql").executable[0]
    assert c.label == "S-1 проверка"


# ------------------------------------------------------------- диспетчер ---

def test_unknown_adapter_is_an_error():
    with pytest.raises(A.SuiteContractError):
        A.parse_source("SELECT 1;", "f.sql", "не-такой-адаптер")


def test_every_registered_suite_parses_and_ids_are_unique():
    import json
    import run_data_checks as R
    reg = json.loads((TOOLS.parent / "quality" / "suites.json").read_text(encoding="utf-8"))
    for suite in reg["suites"]:
        res = R.parse_suite(suite)          # бросит при дублирующемся идентификаторе
        assert res.checks, f"{suite['suite']}: ни одной проверки"
        for cid in suite.get("check_overrides", {}):
            assert cid in {c.check_id for c in res.checks}, (
                f"{suite['suite']}: исключение {cid} ссылается в пустоту")


def test_backlog_entries_point_at_existing_files_and_are_not_also_gates():
    import json
    reg = json.loads((TOOLS.parent / "quality" / "suites.json").read_text(encoding="utf-8"))
    wired = {f for s in reg["suites"] for f in s["files"]}
    for b in reg["backlog"]["files"]:
        assert (TOOLS.parent / b["file"]).exists(), f"нет файла {b['file']}"
        assert b["file"] not in wired, f"{b['file']} одновременно в воротах и в долге"


def test_every_override_and_backlog_rule_exists_in_the_ubr_registry():
    import json
    reg = json.loads((TOOLS.parent / "quality" / "suites.json").read_text(encoding="utf-8"))
    ubr = json.loads((TOOLS.parent / "quality" / "unresolved_business_rules.json").read_text(encoding="utf-8"))
    known = {r["id"] for r in ubr["rules"]}
    referenced = {o["unresolved_rule"] for s in reg["suites"]
                  for o in s.get("check_overrides", {}).values() if o.get("unresolved_rule")}
    referenced |= {r for b in reg["backlog"]["files"] for r in b.get("unresolved_rules", [])}
    assert referenced <= known, f"ссылки на несуществующие правила: {sorted(referenced - known)}"


def test_every_override_declares_status_and_evidence():
    import json
    import run_data_checks as R
    reg = json.loads((TOOLS.parent / "quality" / "suites.json").read_text(encoding="utf-8"))
    for s in reg["suites"]:
        for cid, ov in s.get("check_overrides", {}).items():
            assert ov.get("status") in R.OVERRIDE_STATUSES, f"{cid}: неизвестный статус"
            assert ov.get("reason"), f"{cid}: исключение без причины"
            assert ov.get("evidence"), f"{cid}: исключение без доказательства"
