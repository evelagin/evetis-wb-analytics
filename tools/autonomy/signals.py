"""Канонические сигналы для наблюдателя AE (AE-R1). Только чтение, ни одного вызова модели.

Наблюдатель НЕ вычисляет здоровье данных сам — это решено DRO-1. Источники:
  * `evetis_health.V_DATA_HEALTH_CURRENT` — состояние каждого конвейера (serving/data status,
    severity, reason_code, свежесть, восстановимость, устаревание самого детектора);
  * `wb_ops.OPS_INCIDENT` (scope DRO_PIPELINE) — открытые инциденты DRO и их возраст;
  * `evetis_health.V_RUN_FAILURE_LEDGER` — отказы прогонов с кодом/сигнатурой/отпечатком: то, что
    DRO не видит, когда данные всё равно дошли (кейс Sheets 503, 2026-10-07).

Сигнал — плоский словарь только из перечислений, счётчиков, идентификаторов и отпечатков
(политика вывода B3): текст ошибок и строки данных из BigQuery сюда не выбираются вовсе.
Ошибка чтения, отсутствие снимка или устаревший детектор → сигнал `infra_blocked` (не здоровье).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

HEALTH_SQL = """
SELECT pipeline_id, marketplace, domain, source, data_class, criticality, recoverability,
       serving_status, data_status, severity, reason_code, run_status, run_error_code,
       evaluation_mode, alerting_enabled, open_incidents, open_incident_id,
       FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%SZ', evaluated_at) AS evaluated_at,
       detector_age_minutes, detector_run_id
FROM `{health_view}`
"""
INCIDENT_SQL = """
SELECT scope_id AS pipeline_id, incident_id, reason_code, severity, state,
       FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%SZ', first_seen_at) AS first_seen_at,
       FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%SZ', last_seen_at) AS last_seen_at
FROM `{incident_table}`
WHERE scope = '{scope}' AND state = 'OPEN'
"""
LEDGER_SQL = """
SELECT source_log, loader_name, environment, error_code, failure_signature, recorded_as_transient,
       message_fingerprint, occurrences_7d, occurrences_30d,
       FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%SZ', first_seen_at) AS first_seen_at,
       FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%SZ', last_seen_at) AS last_seen_at,
       last_run_id, recovery_status, minutes_to_recovery
FROM `{ledger_view}`
WHERE environment = 'prod'
  AND last_seen_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {window} DAY)
"""

# Поля сигнала, которые вообще могут выйти наружу. Всё прочее отбрасывается до записи.
HEALTH_FIELDS = ("pipeline_id", "marketplace", "domain", "source_system", "data_class", "criticality",
                 "recoverability", "serving_status", "data_status", "severity", "reason_code", "run_status",
                 "run_error_code", "evaluation_mode", "open_incidents", "open_incident_id", "incident_first_seen_at",
                 "evaluated_at", "detector_age_minutes", "detector_run_id")
LEDGER_FIELDS = ("source_log", "loader_name", "environment", "error_code", "failure_signature",
                 "recorded_as_transient", "message_fingerprint", "occurrences_7d", "occurrences_30d",
                 "first_seen_at", "last_seen_at", "last_run_id", "recovery_status", "minutes_to_recovery")
_ID = re.compile(r"[^A-Za-z0-9_.:-]+")


def _slug(text: str, n: int = 120) -> str:
    return _ID.sub("-", text or "")[:n].strip("-") or "unknown"


def _int(v):
    try:
        return int(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def _bool(v) -> bool:
    return v is True or str(v).lower() == "true"


def health_signal(row: dict, incident: dict | None = None) -> dict:
    sig = {"kind": "dro_health", "key": f"dro:{_slug(row.get('pipeline_id'))}:{_slug(row.get('reason_code'), 60)}"}
    src = {**row, "source_system": row.get("source") or row.get("source_system")}
    for f in HEALTH_FIELDS:
        if f in src and src[f] not in (None, ""):
            sig[f] = src[f]
    for f in ("open_incidents", "detector_age_minutes"):
        if f in sig:
            sig[f] = _int(sig[f])
    if incident:
        sig["open_incident_id"] = incident.get("incident_id")
        sig["incident_first_seen_at"] = incident.get("first_seen_at")
    return sig


def ledger_signal(row: dict) -> dict:
    sig = {"kind": "run_failure",
           "key": f"runfail:{_slug(row.get('loader_name'), 60)}:{_slug(row.get('error_code') or 'NO_CODE', 60)}:"
                  f"{_slug(row.get('message_fingerprint'), 16)}"}
    for f in LEDGER_FIELDS:
        if f in row and row[f] not in (None, ""):
            sig[f] = row[f]
    for f in ("occurrences_7d", "occurrences_30d", "minutes_to_recovery"):
        if f in sig:
            sig[f] = _int(sig[f])
    sig["recorded_as_transient"] = _bool(row.get("recorded_as_transient"))
    return sig


def infra_blocked(source: str, reason: str) -> dict:
    """Доказательство не получено — это НЕ здоровье. Текст ошибки наружу не идёт: только вид отказа."""
    return {"kind": "infra_blocked", "key": f"infra:{_slug(source, 60)}", "source": source,
            "reason": _slug(reason, 80)}


def canonical_signals(fetch, policy: dict) -> list[dict]:
    """fetch(sql) → list[dict]. Отдельный отказ каждого источника → свой infra_blocked."""
    s = policy["signals"]
    out: list[dict] = []
    incidents: dict[str, dict] = {}
    try:
        for r in fetch(INCIDENT_SQL.format(incident_table=s["incident_table"], scope=s["incident_scope"])):
            incidents.setdefault(r["pipeline_id"], r)
    except Exception as e:  # noqa: BLE001 — любой отказ чтения = отсутствие доказательства
        out.append(infra_blocked("dro_incidents", type(e).__name__))
    try:
        rows = fetch(HEALTH_SQL.format(health_view=s["health_view"]))
        if not rows:
            out.append(infra_blocked("dro_health", "EMPTY_SNAPSHOT"))
        stale = [r for r in rows if (_int(r.get("detector_age_minutes")) or 0) > s["max_detector_age_minutes"]
                 or r.get("evaluated_at") in (None, "")]
        if rows and len(stale) == len(rows):
            # Весь снимок старше допуска: здоровье неизвестно, но сам детектор — кандидат DETECTOR_DEFECT
            # через reason_code DETECTOR_STALE, который V_DATA_HEALTH_CURRENT уже подставил.
            out.append(infra_blocked("dro_health", "DETECTOR_STALE"))
        for r in rows:
            if r.get("evaluation_mode") != "EVALUATED" and r.get("reason_code") not in ("DETECTOR_STALE",
                                                                                        "DETECTOR_NEVER_RAN"):
                continue
            out.append(health_signal(r, incidents.get(r["pipeline_id"])))
    except Exception as e:  # noqa: BLE001
        out.append(infra_blocked("dro_health", type(e).__name__))
    try:
        for r in fetch(LEDGER_SQL.format(ledger_view=s["ledger_view"], window=int(s["ledger_window_days"]))):
            out.append(ledger_signal(r))
    except Exception as e:  # noqa: BLE001
        out.append(infra_blocked("run_failure_ledger", type(e).__name__))
    return out


def live_fetcher(project: str, token_command: str):
    import sys
    repo = Path(__file__).resolve().parent.parent.parent
    sys.path.insert(0, str(repo / "tools"))
    from lib.bq_readonly import ReadOnlyBigQuery, resolve_token  # noqa: E402 — существующий read-only клиент
    import os
    bq = ReadOnlyBigQuery(project=project, token=resolve_token("BQ_TOKEN", token_command, os.environ),
                          label_purpose="ae-watch")
    return lambda sql: bq.query(sql, max_results=2000)


def fixture_fetcher(path: Path):
    """Фикстура: {"health": [...], "incidents": [...], "ledger": [...], "fail": ["health"|...]}."""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))

    def fetch(sql: str):
        for name, marker in (("incidents", "OPS_INCIDENT"), ("ledger", "V_RUN_FAILURE_LEDGER"),
                             ("health", "V_DATA_HEALTH_CURRENT")):
            if marker in sql:
                if name in doc.get("fail", []):
                    raise RuntimeError(f"fixture failure: {name}")
                return doc.get(name, [])
        raise AssertionError("неизвестный запрос")
    return fetch
