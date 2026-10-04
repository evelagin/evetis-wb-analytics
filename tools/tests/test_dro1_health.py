"""DRO-1: контракт конвейеров, классификация состояний и безопасность слоя evetis_health.

Классификаторы исполняются в sqlite ровно тем текстом, что уйдёт в BigQuery: CTE
classified_reason / classified (TVF_DATA_HEALTH), period_classified (TVF_DATA_PERIOD_STATE),
overlay / effective (V_DATA_HEALTH_CURRENT), checks_classified (V_HEALTH_CHECK_CURRENT)
вырезаются из sql/health/*.sql sqlglot-ом, входной CTE подаётся фикстурой.
Проверка на живых данных — tools/dro1_health.py backtest (read-only, вне CI).
"""
from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

import pytest
import sqlglot
from sqlglot import exp

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools"))

import dro1_health  # noqa: E402
from lib.bq_readonly import assert_read_only  # noqa: E402

SQL_DIR = REPO / "sql" / "health"
TF_FILE = REPO / "infra" / "terraform" / "evetis_health.tf"

# Реестр wb_ops.OPS_PIPELINE_REGISTRY, снимок 2026-10-04 (аудит DRO v1, appendix K §1.1).
REGISTRY_IDS = {
    "ads_costs", "ads_daily", "ads_query_bids", "ads_query_stats", "ads_search_clusters",
    "finance", "finance_backfill", "mart", "orders", "ozon_ads_campaigns", "ozon_ads_expense_daily",
    "ozon_ads_sku_daily", "ozon_catalog", "ozon_clusters", "ozon_fbo_postings", "ozon_finance_accrual",
    "ozon_prices", "ozon_seller_info", "ozon_stocks", "ozon_supplies", "ref_sync", "sales",
    "sales_reconcile", "stocks_cloudrun", "stocks_snapshot", "wb_prices_observer", "wb_tariffs_loader",
}


@pytest.fixture(scope="module")
def objects():
    return dro1_health.load_objects()


@pytest.fixture(scope="module")
def contract():
    return {r["pipeline_id"]: r for r in dro1_health.load_contract()}


# ── Контракт ─────────────────────────────────────────────────────────────────
def test_contract_covers_registry_exactly(contract):
    assert REGISTRY_IDS <= set(contract), REGISTRY_IDS - set(contract)
    assert {p for p, r in contract.items() if r["in_registry"]} == REGISTRY_IDS


def test_contract_ids_unique():
    rows = dro1_health.load_contract()
    ids = [r["pipeline_id"] for r in rows]
    assert len(ids) == len(set(ids)) == 40


def test_contract_thresholds_are_consistent(contract):
    for pid, r in contract.items():
        if r["evaluation_mode"] != "EVALUATED":
            assert r["evaluation_mode"] == "NOT_EVALUATED", pid
            assert r["not_evaluated_reason"], pid
            assert r["alerting_enabled"] is False, pid
            continue
        assert r["freshness_basis"] in ("SLOT", "AGE"), pid
        assert r["criticality"] in ("CRITICAL", "HIGH", "MEDIUM", "LOW"), pid
        assert r["data_class"] in ("EVENT_HISTORY", "SNAPSHOT", "WINDOWED", "DERIVED", "REFERENCE"), pid
        assert 0 <= r["warn_after_minutes"] <= r["fail_after_minutes"], pid
        if r["freshness_basis"] == "SLOT":
            assert re.fullmatch(r"[0-2]\d:[0-5]\d", r["slot_time_msk"]), pid
            assert r["business_lag_days"] >= 0, pid
        if r["loss_window_days"] is not None:
            assert r["freshness_basis"] == "SLOT", pid
            assert r["imminent_loss_minutes"] and r["imminent_loss_minutes"] >= 480, pid
        if r["data_class"] == "WINDOWED":
            assert r["loss_window_days"] and r["loss_window_days"] > 0, pid


def test_snapshot_warning_precedes_irreversible_loss_by_8h(contract):
    """Снимок с потерей в 00:00: WARN обязан прийти не позже 16:00 МСК (≥ 8 ч запаса)."""
    checked = 0
    for pid, r in contract.items():
        if r["evaluation_mode"] == "EVALUATED" and r["loss_window_days"] == 0:
            hh, mm = map(int, r["slot_time_msk"].split(":"))
            warn_at = hh * 60 + mm + r["warn_after_minutes"]
            assert warn_at <= 16 * 60, (pid, warn_at)
            assert r["recoverability"] == "NON_RECOVERABLE", pid
            checked += 1
    assert checked >= 7  # ставки, остатки WB, тарифы, кампании/каталог/цены/остатки Ozon


def test_fulfillment_and_plan_stay_out_of_dro1(contract):
    """Гейт ФФ и план продаж — DRO-2 (owner ACK 2026-10-04), здесь не оцениваются."""
    for pid in ("ff_stock", "sales_plan"):
        assert contract[pid]["evaluation_mode"] == "NOT_EVALUATED"
        assert contract[pid]["not_evaluated_reason"] == "DEFERRED_DRO2"
    text = "".join(p.read_text(encoding="utf-8") for p in SQL_DIR.glob("dro1_*.sql"))
    assert "V_CAPABILITY_HEALTH" not in text and "V_OPS_SUPPLY_PLAN" not in text


# ── sqlite-стенд для CTE-классификаторов ─────────────────────────────────────
def _cte_chain(body: str, names: list[str]) -> str:
    tree = sqlglot.parse_one(body, read="bigquery")
    ctes = {c.alias: c.this for c in tree.find_all(exp.CTE)}
    q = exp.select("*").from_(names[-1])
    for n in names:
        q = q.with_(n, as_=ctes[n].copy())
    return q.sql(dialect="sqlite")


def _run(sql: str, table: str, rows: list[dict]) -> list[dict]:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    cols = sorted({k for r in rows for k in r})
    db.execute(f"CREATE TABLE {table} ({', '.join(cols)})")
    for r in rows:
        db.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                   [r.get(c) for c in cols])
    return [dict(x) for x in db.execute(sql).fetchall()]


def _health_row(**over) -> dict:
    base = dict(
        pipeline_id="p", evaluation_mode="EVALUATED", alerting_enabled=1, criticality="HIGH",
        data_class="SNAPSHOT", freshness_basis="SLOT", latest_business_date="2026-10-04",
        latest_success_load_ts="2026-10-04 03:00:00", slot_late_minutes=0, minutes_to_loss=None,
        warn_after_minutes=105, fail_after_minutes=405, imminent_loss_minutes=480, age_minutes=30,
        run_status="SUCCESS", completeness_status="NOT_MEASURED", completeness_reason=None,
        open_recoverable_gaps=0, target_failed_run=0, latest_period_provisional=0,
    )
    base.update(over)
    return base


@pytest.fixture(scope="module")
def classify(objects):
    sql = _cte_chain(objects["TVF_DATA_HEALTH"]["body"], ["classified_reason", "classified"])

    def run(**over):
        return _run(sql, "health_input", [_health_row(**over)])[0]
    return run


def _state(row):
    return row["serving_status"], row["data_status"], row["reason_code"], row["severity"]


def test_normal_fresh_pipeline_is_healthy(classify):
    row = classify(freshness_basis="AGE", data_class="EVENT_HISTORY", criticality="CRITICAL",
                   warn_after_minutes=180, fail_after_minutes=360, age_minutes=20)
    assert _state(row) == ("HEALTHY", "COMPLETE", "OK", "INFO")
    assert row["alertable"] == 0


def test_ads_30_09_failed_run_recovered_data_healthy_pipeline(classify):
    """Запуск за период ERROR/ADS_PARTIAL, данные пришли позже: RECOVERED, конвейер HEALTHY."""
    row = classify(data_class="WINDOWED", warn_after_minutes=45, fail_after_minutes=105,
                   target_failed_run=1, run_status="SUCCESS")
    assert _state(row) == ("HEALTHY", "RECOVERED", "OK", "INFO")
    assert row["alertable"] == 0


def test_snapshot_missed_before_deadline_warns(classify):
    row = classify(latest_business_date="2026-10-03", slot_late_minutes=105, minutes_to_loss=1020)
    assert _state(row) == ("DEGRADED", "MISSING", "SLOT_LATE", "HIGH")
    assert row["alertable"] == 1


def test_snapshot_loss_imminent_is_critical(classify):
    row = classify(latest_business_date="2026-10-03", slot_late_minutes=675, minutes_to_loss=450)
    assert _state(row) == ("BLOCKED", "MISSING", "DATA_LOSS_IMMINENT", "CRITICAL")


def test_snapshot_deadline_passed_is_unrecoverable(classify):
    row = classify(latest_business_date="2026-10-03", slot_late_minutes=1155, minutes_to_loss=-30)
    assert _state(row) == ("BLOCKED", "MISSING_UNRECOVERABLE", "DATA_LOSS_CONFIRMED", "CRITICAL")


def test_recoverable_source_delayed_is_stale_not_unrecoverable(classify):
    age = classify(freshness_basis="AGE", data_class="EVENT_HISTORY", criticality="CRITICAL",
                   warn_after_minutes=180, fail_after_minutes=360, age_minutes=400)
    assert _state(age) == ("STALE", "MISSING", "FRESHNESS_STALE", "CRITICAL")
    slot = classify(data_class="DERIVED", criticality="CRITICAL", warn_after_minutes=30,
                    fail_after_minutes=150, slot_late_minutes=200, minutes_to_loss=None)
    assert _state(slot) == ("STALE", "MISSING", "SLOT_MISSED", "CRITICAL")
    late = classify(data_class="EVENT_HISTORY", criticality="HIGH", warn_after_minutes=120,
                    fail_after_minutes=1560, slot_late_minutes=130, minutes_to_loss=None)
    assert _state(late) == ("DEGRADED", "MISSING", "SLOT_LATE", "MEDIUM")


def test_within_grace_is_pending_not_alerting(classify):
    row = classify(latest_business_date="2026-10-03", slot_late_minutes=20, minutes_to_loss=1100)
    assert _state(row) == ("HEALTHY", "PENDING", "OK", "INFO")
    assert row["alertable"] == 0


def test_last_run_failed_with_fresh_data_degrades(classify):
    row = classify(run_status="FAILED")
    assert _state(row)[:3] == ("DEGRADED", "COMPLETE", "RUN_FAILED")
    abandoned = classify(run_status="ABANDONED", criticality="CRITICAL")
    assert _state(abandoned) == ("DEGRADED", "COMPLETE", "RUN_FAILED", "HIGH")


def test_completeness_failure_degrades_snapshot(classify):
    row = classify(completeness_status="INCOMPLETE", completeness_reason="CONTROL_MISMATCH")
    assert _state(row) == ("DEGRADED", "INCOMPLETE", "CONTROL_MISMATCH", "HIGH")


def test_low_criticality_loss_is_not_critical(classify):
    row = classify(criticality="LOW", latest_business_date="2026-10-03",
                   slot_late_minutes=1155, minutes_to_loss=-30)
    assert row["severity"] == "MEDIUM" and row["serving_status"] == "BLOCKED"


def test_not_evaluated_is_unknown_and_silent(classify):
    row = classify(evaluation_mode="NOT_EVALUATED", alerting_enabled=0,
                   latest_business_date=None, latest_success_load_ts=None)
    assert _state(row) == ("UNKNOWN", "UNKNOWN", "CHECK_NOT_IMPLEMENTED", "INFO")
    assert row["alertable"] == 0


def test_probe_found_nothing_is_unknown_and_alerts(classify):
    row = classify(latest_business_date=None, latest_success_load_ts=None)
    assert _state(row) == ("UNKNOWN", "UNKNOWN", "NO_DATA_OBSERVED", "HIGH")
    assert row["alertable"] == 1


def test_provisional_period(classify):
    row = classify(freshness_basis="AGE", data_class="EVENT_HISTORY", warn_after_minutes=1560,
                   fail_after_minutes=3000, age_minutes=100, latest_period_provisional=1)
    assert _state(row) == ("HEALTHY", "PROVISIONAL", "OK", "INFO")


# ── Периоды ──────────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def classify_period(objects):
    sql = _cte_chain(objects["TVF_DATA_PERIOD_STATE"]["body"], ["period_classified"])

    def run(**over):
        row = dict(pipeline_id="p", period_date="2026-09-30", present=0, failed_run=0, later_success=0,
                   covered_by_run=0, minutes_past_loss_deadline=None)
        row.update(over)
        return _run(sql, "period_input", [row])[0]["data_status"]
    return run


@pytest.mark.parametrize("over, expected", [
    (dict(present=1, failed_run=1, later_success=1), "RECOVERED"),       # реклама 30.09, витрина 30.09
    (dict(present=1, failed_run=1, later_success=0), "INCOMPLETE"),
    (dict(present=1), "COMPLETE"),
    (dict(covered_by_run=1), "NOT_EXPECTED"),                            # день без расходов Ozon
    (dict(minutes_past_loss_deadline=30), "MISSING_UNRECOVERABLE"),      # ставки 13.09, остатки 03.09
    (dict(minutes_past_loss_deadline=-600), "MISSING"),                  # ещё можно перезапустить
    (dict(failed_run=1, minutes_past_loss_deadline=None), "MISSING"),    # восстановимый источник
])
def test_period_classification(classify_period, over, expected):
    assert classify_period(**over) == expected


# ── Устаревание детектора ────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def overlay(objects):
    sql = _cte_chain(objects["V_DATA_HEALTH_CURRENT"]["body"], ["overlay", "effective"])

    def run(**over):
        row = dict(pipeline_id="p", evaluation_mode="EVALUATED", evaluated_at="2026-10-04 10:00:00",
                   detector_age_minutes=10, detector_max_age_minutes=60, serving_status="HEALTHY",
                   reason_code="OK", severity="INFO")
        row.update(over)
        return _run(sql, "latest_eval_aged", [row])[0]
    return run


def test_fresh_detector_passes_state_through(overlay):
    r = overlay(serving_status="DEGRADED", reason_code="SLOT_LATE", severity="HIGH")
    assert (r["serving_status_effective"], r["reason_code_effective"], r["severity_effective"]) == \
        ("DEGRADED", "SLOT_LATE", "HIGH")


def test_stale_detector_is_unknown_never_healthy(overlay):
    r = overlay(detector_age_minutes=61)
    assert (r["serving_status_effective"], r["reason_code_effective"], r["severity_effective"]) == \
        ("UNKNOWN", "DETECTOR_STALE", "CRITICAL")
    r12d = overlay(detector_age_minutes=12 * 24 * 60)
    assert r12d["serving_status_effective"] == "UNKNOWN"


def test_detector_never_ran_is_unknown(overlay):
    r = overlay(evaluated_at=None, detector_age_minutes=None)
    assert (r["serving_status_effective"], r["reason_code_effective"]) == ("UNKNOWN", "DETECTOR_NEVER_RAN")


@pytest.fixture(scope="module")
def check_status(objects):
    sql = _cte_chain(objects["V_HEALTH_CHECK_CURRENT"]["body"], ["checks_classified"])

    def run(**over):
        row = dict(pipeline_id="orders", check_id="H7_ORDERS_STALE", scope="PIPELINE_CHECK",
                   health_status="HEALTHY", check_age_minutes=60, expected_interval_minutes=180)
        row.update(over)
        return _run(sql, "checks_aged", [row])[0]
    return run


def test_extended_check_not_run_12_days_is_unknown(check_status):
    r = check_status(check_age_minutes=12 * 24 * 60)
    assert (r["effective_status"], r["staleness_reason"]) == ("UNKNOWN", "DETECTOR_STALE")


def test_scheduled_check_keeps_status(check_status):
    assert check_status()["effective_status"] == "HEALTHY"
    assert check_status(health_status="UNHEALTHY")["effective_status"] == "UNHEALTHY"


# ── Пороги — только из контракта ─────────────────────────────────────────────
def test_classifiers_hold_no_threshold_literals(objects):
    """Числа порогов живут в V_PIPELINE_CONTRACT; в классификаторах — только 0."""
    for name, ctes in (("TVF_DATA_HEALTH", ["classified_reason", "classified"]),
                       ("TVF_DATA_PERIOD_STATE", ["period_classified"])):
        tree = sqlglot.parse_one(objects[name]["body"], read="bigquery")
        for c in tree.find_all(exp.CTE):
            if c.alias in ctes:
                nums = {lit.this for lit in c.this.find_all(exp.Literal) if not lit.is_string}
                assert nums <= {"0"}, (name, c.alias, nums)


# ── Безопасность ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("name", ["V_PIPELINE_CONTRACT", "TVF_PIPELINE_PROBE", "TVF_DATA_PERIOD_STATE",
                                  "TVF_DATA_HEALTH", "V_HEALTH_CHECK_CURRENT", "V_DATA_PERIOD_STATE"])
def test_read_objects_render_to_read_only_select(objects, name):
    params = objects[name]["params"]
    sql = dro1_health.render(name, ["CURRENT_TIMESTAMP()"] if params else None, objects)
    assert_read_only(sql)


def test_procedures_write_only_to_allowed_targets():
    text = (SQL_DIR / "dro1_05_procedures.sql").read_text(encoding="utf-8")
    body = "\n".join(line.split("--")[0] for line in text.splitlines())
    targets = set(re.findall(r"\b(?:INSERT\s+INTO|UPDATE|MERGE(?:\s+INTO)?|DELETE\s+FROM|TRUNCATE\s+TABLE)\s+`?([\w.]+)`?",
                             body, re.IGNORECASE))
    assert targets == {"wb_ops.OPS_ALERT_EVENT", "evetis_health.ALERT_DISPATCH_LOG",
                       "evetis_health.DATA_HEALTH_SNAPSHOT"}, targets
    assert not re.search(r"\b(DELETE|MERGE|TRUNCATE|DROP|ALTER|GRANT|EXPORT)\b", body, re.IGNORECASE)
    calls = set(re.findall(r"\bCALL\s+`([\w.]+)`", body))
    assert calls == {"wb_ops.sp_ops_apply_health", "evetis_health.sp_dispatch_alerts"}
    temp = re.findall(r"CREATE\s+OR\s+REPLACE\s+(TEMP\s+)?TABLE\s+(\w+)", body, re.IGNORECASE)
    assert temp and all(t[0] and t[1].startswith("_dro_") for t in temp), temp


def test_query_labels_are_valid_bigquery_labels():
    text = (SQL_DIR / "dro1_05_procedures.sql").read_text(encoding="utf-8")
    literals = re.findall(r"SET\s+@@query_label\s*=\s*'([^']*)'", text)
    assert literals
    for lit in literals:
        for pair in lit.split(","):
            key, _, value = pair.partition(":")
            assert re.fullmatch(r"[a-z][a-z0-9_-]{0,62}", key), pair
            assert re.fullmatch(r"[a-z0-9_-]{1,63}", value), pair
    for key in re.findall(r"',?(dro_\w+):'", text):
        assert re.fullmatch(r"[a-z][a-z0-9_-]{0,62}", key)


def test_no_secrets_in_dro1_files():
    files = list(SQL_DIR.glob("dro1_*.sql")) + [TF_FILE, REPO / "tools" / "dro1_health.py",
                                                REPO / "docs" / "ops" / "DRO1_DETECTION_ALERTING_2026-10-04.md"]
    pattern = re.compile(r"(ya29\.[\w-]{20,}|AIza[\w-]{30,}|-----BEGIN|\d{8,10}:[\w-]{30,}|sk-[A-Za-z0-9]{20,}"
                         r"|X-Scheduler-Secret\s*[:=]\s*\S{8,})")
    for f in files:
        assert not pattern.search(f.read_text(encoding="utf-8")), f


# ── Terraform ────────────────────────────────────────────────────────────────
def test_terraform_wires_detector_and_policies_to_existing_channel():
    tf = TF_FILE.read_text(encoding="utf-8")
    assert 'resource "google_bigquery_dataset" "evetis_health"' in tf
    assert "sp_evaluate_data_health" in tf and '"*/30 * * * *"' in tf
    assert "google_monitoring_notification_channel.unitka_email" in tf
    assert 'resource "google_monitoring_notification_channel"' not in tf  # канал не новый
    assert "google_project_iam_member" not in tf                          # только права на датасеты
    for marker in ("dro_delivery_marker", "dro_digest_marker", "dro_heartbeat_marker"):
        assert marker in tf, marker
        assert marker in (SQL_DIR / "dro1_05_procedures.sql").read_text(encoding="utf-8"), marker
    for kind in ("alert", "digest", "heartbeat", "dispatch_error"):
        assert f'dro_kind="{kind}"' in tf, kind
    assert tf.count('count        = var.unitka_alert_email == "" ? 0 : 1') == 4
