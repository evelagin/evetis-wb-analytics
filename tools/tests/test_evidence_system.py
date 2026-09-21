"""Тесты системы доказательств: исполнитель, реестр, impact-анализ, Definition of Done.

Ворота, которые умеют только зеленеть, бесполезны. Здесь проверяется каждый способ,
которым доказательство может НЕ состояться: провал, пустой результат, ошибка запроса,
таймаут, незарегистрированная проверка, отсутствующая зависимость инструмента.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parent.parent
REPO = TOOLS.parent
sys.path.insert(0, str(TOOLS))

import build_contract_registry as B  # noqa: E402
import impact_analysis as I  # noqa: E402
import run_data_checks as R  # noqa: E402
import verify_task as V  # noqa: E402
from lib.acceptance import Check  # noqa: E402
from lib.bq_readonly import BigQueryError  # noqa: E402


class FakeBQ:
    def __init__(self, rows=None, error=None):
        self._rows, self._error = rows or [], error
        self.queries_issued, self.bytes_billed = 0, 0

    def query(self, sql, dry_run=False, **kw):
        self.queries_issued += 1
        if self._error:
            raise self._error
        self.bytes_billed += 1024
        return [] if dry_run else self._rows


def chk(**kw) -> Check:
    base = dict(check_id="C", source="f.sql", line=1, sql="SELECT 1", adapter="check_blocks")
    base.update(kw)
    return Check(**base)


# --------------------------------------------- исполнитель: все виды провала ---

def test_expected_pass():
    r = R.run_check(FakeBQ([{"status": "PASS"}]), chk(), 5)
    assert r["status"] == R.PASS and r["gate_relevant"] is True


def test_forced_fail_reports_sample():
    rows = [{"status": "FAIL", "month": "2026-08", "delta": "12.5"}]
    r = R.run_check(FakeBQ(rows), chk(), 5)
    assert r["status"] == R.FAIL
    assert r["failing_sample"][0]["delta"] == "12.5"


def test_empty_is_not_success():
    assert R.run_check(FakeBQ([]), chk(), 5)["status"] == R.EMPTY


def test_empty_allowed_only_when_declared():
    assert R.run_check(FakeBQ([]), chk(expect_empty_ok=True), 5)["status"] == R.PASS


def test_query_error_is_error_not_fail():
    r = R.run_check(FakeBQ(error=BigQueryError("BigQuery HTTP 400: Syntax error")), chk(), 5)
    assert r["status"] == R.ERROR and "400" in r["error"]


def test_timeout_surfaces_as_error():
    r = R.run_check(FakeBQ(error=BigQueryError("BigQuery не завершил задание за отведённое время")),
                    chk(), 5)
    assert r["status"] == R.ERROR and "время" in r["error"]


def test_missing_verdict_column_is_error():
    r = R.run_check(FakeBQ([{"n": "0"}]), chk(), 5)
    assert r["status"] == R.ERROR


def test_verdict_outside_pass_fail_is_error_not_fail():
    r = R.run_check(FakeBQ([{"status": "СВЕРИТЬ С BASELINE"}]), chk(), 5)
    assert r["status"] == R.ERROR


def test_not_executable_check_is_blocked_and_never_silently_green():
    r = R.run_check(FakeBQ([{"status": "PASS"}]),
                    chk(executable=False, not_executable_reason="нет условия"), 5)
    assert r["status"] == R.BLOCKED


def test_override_excludes_from_gate_but_keeps_the_observed_status():
    r = R.run_check(FakeBQ([{"status": "FAIL"}]), chk(),
                    5, override={"status": "superseded", "excluded_from_gate": True})
    assert r["status"] == R.FAIL and r["gate_relevant"] is False


def test_dry_run_does_not_claim_a_verdict_on_data():
    r = R.run_check(FakeBQ([{"status": "FAIL"}]), chk(), 5, dry_run=True)
    assert r["status"] == R.PASS and r["rows"] == 0   # доказана компилируемость, не вердикт


@pytest.mark.parametrize("statuses, expected", [
    ([R.PASS], R.PASS), ([R.PASS, R.EMPTY], R.EMPTY),
    ([R.FAIL, R.EMPTY], R.FAIL), ([R.FAIL, R.ERROR], R.ERROR), ([], R.PASS),
])
def test_verdict_precedence(statuses, expected):
    assert R.verdict([{"status": s} for s in statuses]) == expected


# ------------------------------------------------------------ CLI-контракт ---

def _cli(*args):
    return subprocess.run([sys.executable, "tools/run_data_checks.py", *args],
                          cwd=str(REPO), capture_output=True, text=True)


def test_unknown_suite_is_an_error():
    r = _cli("--suite", "нет-такого", "--list")
    assert r.returncode == R.EXIT[R.ERROR] and "не объявлен" in r.stderr


def test_unknown_check_id_is_an_error_not_an_empty_green_run():
    r = _cli("--suite", "ozon_unit", "--check", "НЕТ_ТАКОЙ", "--list")
    assert r.returncode == R.EXIT[R.ERROR] and "нет таких проверок" in r.stderr


def test_list_all_parses_every_registered_suite():
    r = _cli("--list-all")
    assert r.returncode == 0 and "ОШИБКА РАЗБОРА" not in r.stdout


def test_exit_codes_are_distinct():
    assert sorted(R.EXIT.values()) == [0, 1, 2, 3]


# ---------------------------------------------------------------- реестр ---

def test_contract_registry_is_reproducible_and_complete():
    a, b = B.build(strict=True), B.build(strict=True)
    for d in (a, b):
        d.pop("generated_at")
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    assert a["summary"]["parse_problems"] == 0
    required = {"check_id", "domain", "pipelines", "marketplace", "severity", "source_contract",
                "execution_mode", "expected_result", "owner", "type", "status", "dependencies",
                "timeout_seconds", "freshness_requirement", "business_rule_reference"}
    for c in a["checks"]:
        assert required <= set(c), f"{c['check_id']}: нет полей {sorted(required - set(c))}"


def test_registry_answers_what_cannot_be_executed_and_why():
    reg = B.build()
    for c in reg["checks"]:
        if c["status"] != "active":
            assert c["status_reason"], f"{c['check_id']}: статус {c['status']} без причины"


# --------------------------------------------------------- impact-анализ ---

def test_tiering_is_deterministic_and_total():
    inv = I.load(I.INVENTORY)
    tiers = I.classify_tiers(inv, I.load(I.REGISTRY), I.load(I.SUITES))
    assert set(tiers) == set(inv["objects"]), "классифицирован не каждый объект"
    assert set(tiers.values()) <= set(I.TIER_RULES)
    assert tiers == I.classify_tiers(inv, I.load(I.REGISTRY), I.load(I.SUITES))


def test_canonical_sql_change_requires_its_contract():
    res = I.analyse(["sql/current/ozon_mart/FCT_OZON_SKU_PNL_DAILY.sql"])
    assert "ozon_mart.FCT_OZON_SKU_PNL_DAILY" in res["directly_affected_assets"]
    assert "evetis_mart.FACT_SKU_DAILY" in res["downstream_assets"]
    assert "ozon_unit" in res["required_contracts"]
    assert res["risk_tier"] == "TIER0"


def test_loader_change_maps_to_the_assets_it_produces():
    res = I.analyse(["pipelines/ozon/runtime/entities.py"])
    assert "ozon_raw.RAW_OZON_POSTINGS_FBO" in res["directly_affected_assets"]


def test_unknown_file_is_reported_as_unmapped_not_as_harmless():
    res = I.analyse(["README.md"])
    assert res["file_notes"]["README.md"] == ["соответствие объектам не установлено"]
    assert res["affected_total"] == 0


def test_evidence_graph_answers_which_checks_guard_an_asset():
    reg = I.load(I.REGISTRY)
    node = reg["evidence_graph"]["evetis_mart.FACT_SKU_DAILY"]
    assert "ozon_unit" in node["contracts"] and node["checks"]


# -------------------------------------------------- Definition of Done ---

def test_missing_dependency_is_blocked_not_failed():
    assert V.classify({"exit_code": 1, "tail": "ModuleNotFoundError: No module named 'sqlglot'"}) \
        == V.BLOCKED


def test_timeout_and_missing_binary_are_blocked():
    assert V.classify({"exit_code": 124, "tail": "истёк таймаут"}) == V.BLOCKED
    assert V.classify({"exit_code": 127, "tail": "не найдена команда"}) == V.BLOCKED


def test_real_failure_is_failed():
    assert V.classify({"exit_code": 1, "tail": "AssertionError"}) == V.FAIL


def test_blocked_stage_never_yields_a_pass_verdict():
    assert V.BLOCKED not in V.GOOD and V.FAIL in V.BAD and V.ERROR in V.BAD
    assert V.UNKNOWN not in V.GOOD and V.PARTIAL not in V.GOOD
    assert sorted(V.EXIT.values()) == [0, 1, 2, 3]
