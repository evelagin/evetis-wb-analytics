"""Сборка конверта инцидента и цели инженера из детерминированных наблюдений.

Все поля выводятся из существующих источников истины: наборы и их scope —
`quality/suites.json`, потребители — граф `system_inventory.json`, известные
нарушения — `quality/unresolved_business_rules.json`. Конверт не содержит решения.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from tools.autonomy.schema import require_valid
from tools.autonomy.state import new_id, now_iso

REPO = Path(__file__).resolve().parent.parent.parent


def _load(rel: str) -> dict:
    return json.loads((REPO / rel).read_text(encoding="utf-8"))


def suites_index() -> dict[str, dict]:
    return {s["suite"]: s for s in _load("quality/suites.json")["suites"]}


def open_ubr() -> list[dict]:
    return [r for r in _load("quality/unresolved_business_rules.json")["rules"] if r["status"] == "open"]


def known_checks() -> dict[str, str]:
    """check_id → UBR, для всех открытых нерешённых правил."""
    out: dict[str, str] = {}
    for r in open_ubr():
        for c in r.get("blocked_checks", []):
            out[c] = r["id"]
    return out


def downstream_of(assets: list[str]) -> list[str]:
    objects = _load("docs/architecture/system_inventory.json")["objects"]
    seen, frontier = set(assets), list(assets)
    while frontier:
        for nxt in objects.get(frontier.pop(), {}).get("consumed_by", []):
            if nxt not in seen:
                seen.add(nxt)
                frontier.append(nxt)
    return sorted(seen - set(assets))


def build_incident(obs: dict, history: list[dict], repository_sha: str,
                   classification: str, synthetic: bool = False) -> dict:
    suites = suites_index()
    suite = suites.get(obs.get("suite", ""), {})
    affected = sorted(set(obs.get("affected_assets") or suite.get("scope", [])))
    downstream = [] if synthetic else downstream_of(affected)
    related = set(affected) | set(downstream)
    contract_suites = sorted({obs["suite"]} | {s for s, d in suites.items()
                                                if set(d.get("scope", [])) & related})
    ubr = sorted({r["id"] for r in open_ubr()
                  if obs["check_id"] in r.get("blocked_checks", [])
                  or any(a in r.get("source_artifact", "") for a in affected)})
    fails = [h for h in history if h["status"] not in ("PASS", "HEALTHY")]
    passes = [h for h in history if h["status"] in ("PASS", "HEALTHY")]
    incident = {
        "schema_version": 1,
        "incident_id": new_id("inc"),
        "created_at": now_iso(),
        "source": "synthetic" if synthetic else "watcher",
        "severity": obs.get("severity", suite.get("severity", "MEDIUM")),
        "trigger_type": "SYNTHETIC" if synthetic else
                        ("HEALTH_DEGRADED" if obs["kind"] == "health" else "CHECK_FAILURE"),
        "triggering_checks": [{k: v for k, v in {
            "check_id": obs["check_id"], "suite": obs["suite"], "status": obs["status"],
            "source": obs.get("source")}.items() if v is not None}],
        "first_failure_at": (fails[0]["at"] if fails else now_iso()),
        "last_success_at": (passes[-1]["at"] if passes else None),
        "repository_sha": repository_sha,
        "affected_assets": affected,
        "downstream_consumers": downstream,
        "contract_suites": contract_suites,
        "known_ubr_links": ubr,
        "evidence": {
            "observations": [{k: v for k, v in h.items() if k in ("at", "status", "failing_rows")}
                             for h in history[-10:]] or [{"at": now_iso(), "status": obs["status"]}],
            "classification": "SYNTHETIC" if synthetic else classification,
        },
        "deduplication_key": obs["key"],
    }
    require_valid(incident, "incident")
    return incident


def objective_from_incident(incident: dict) -> dict:
    check = incident["triggering_checks"][0]
    slug = hashlib.sha256(incident["deduplication_key"].encode()).hexdigest()[:10]
    objective = {
        "schema_version": 1,
        "objective_id": f"obj-inc-{slug}",
        "kind": "INCIDENT",
        "created_at": now_iso(),
        "requested_by": "autonomy-watch",
        "title": f"Устранить устойчивое падение {check['check_id']}"[:200],
        "statement": (
            f"Проверка {check['check_id']} набора {check['suite']} устойчиво находится в статусе "
            f"{check['status']} и не связана с открытым нерешённым правилом. Установить первопричину. "
            "Если причина в коде или SQL репозитория — подготовить исправление с тестом, который "
            "краснеет без исправления. Если причина в данных, в production-состоянии или в "
            "бизнес-правиле — не исправлять, а вернуть NEEDS_HUMAN с доказательствами."),
        "incident": incident,
        "acceptance": [
            f"{check['check_id']} возвращает PASS, либо доказано, что изменение кода не требуется",
            "ни одна проверка, бывшая PASS на базовой ветке, не становится FAIL",
            "ни одни ворота не ослаблены: нет удалённых @check, skip, known_failing, override",
            "production-мутаций: 0",
        ],
        "constraints": [
            "не менять production: никакого DDL/DML, deploy, terraform apply, IAM, расписаний",
            "не писать в WB и Ozon",
            "не трогать доверенную базу (trusted_computing_base) и секреты (forbidden_paths) политики AE",
        ],
        "repository_sha": incident["repository_sha"],
        "affected_assets": incident["affected_assets"],
        "downstream_consumers": incident["downstream_consumers"],
        "contract_suites": incident["contract_suites"],
        "known_ubr_links": incident["known_ubr_links"],
        "evidence": incident["evidence"],
        "owner_ack": None,
        "deduplication_key": incident["deduplication_key"],
        "execute": True,
    }
    require_valid(objective, "objective")
    return objective


# ------------------------------------------- канонические сигналы (AE-R1) ---
SIGNAL_PUBLIC = ("kind", "pipeline_id", "loader_name", "source_log", "environment", "error_code", "failure_signature",
                 "recorded_as_transient", "message_fingerprint", "occurrences_7d", "occurrences_30d",
                 "first_seen_at", "last_seen_at", "last_run_id", "recovery_status", "minutes_to_recovery",
                 "serving_status", "data_status", "severity", "reason_code", "run_status", "run_error_code",
                 "data_class", "criticality", "recoverability", "open_incident_id", "evaluated_at",
                 "detector_age_minutes", "detector_run_id")


def _public_signal(sig: dict) -> dict:
    """Только перечисления, счётчики, идентификаторы и отпечатки — политика вывода B3."""
    out = {}
    for k in SIGNAL_PUBLIC:
        v = sig.get(k)
        if isinstance(v, (bool, int)) or v is None:
            out[k] = v
        else:
            out[k] = str(v)[:200]
    return {k: v for k, v in out.items() if v is not None}


def build_signal_incident(sig: dict, decision: dict, history: list[dict], repository_sha: str) -> dict:
    run = sig["kind"] == "run_failure"
    subject = sig.get("loader_name") if run else sig.get("pipeline_id")
    asset = f"{'loader' if run else 'dro_pipeline'}.{subject or 'unknown'}"
    sev = sig.get("severity") if sig.get("severity") in ("CRITICAL", "HIGH", "MEDIUM", "LOW") else "MEDIUM"
    fails = [h for h in history if h["status"] not in ("PASS", "HEALTHY")]
    passes = [h for h in history if h["status"] in ("PASS", "HEALTHY")]
    incident = {
        "schema_version": 1,
        "incident_id": new_id("inc"),
        "created_at": now_iso(),
        "source": "watcher",
        "severity": sev,
        "trigger_type": "RUN_FAILURE" if run else "DRO_HEALTH",
        "triggering_checks": [{"check_id": sig["key"][:200], "suite": "run_failure_ledger" if run else "dro1",
                               "status": "RUN_FAILED" if run else "DEGRADED",
                               "source": ("evetis_health.V_RUN_FAILURE_LEDGER" if run
                                          else "evetis_health.V_DATA_HEALTH_CURRENT")}],
        "first_failure_at": sig.get("first_seen_at") or (fails[0]["at"] if fails else now_iso()),
        "last_success_at": passes[-1]["at"] if passes else None,
        "repository_sha": repository_sha,
        "affected_assets": [asset[:200]],
        "downstream_consumers": [],
        "contract_suites": [],
        "known_ubr_links": [],
        "evidence": {
            "observations": [{"at": h["at"], "status": h["status"]} for h in history[-10:]]
            or [{"at": now_iso(), "status": "RUN_FAILED" if run else "DEGRADED"}],
            "classification": "ENGINEERING_CANDIDATE",
            "task_class": decision["task_class"],
            "classifier_rule": decision["rule"],
            "signal": _public_signal(sig),
        },
        "deduplication_key": sig["key"],
    }
    require_valid(incident, "incident")
    return incident


def objective_from_signal_incident(incident: dict, policy: dict) -> dict:
    """Цель инженеру по каноническому сигналу. Текст — шаблон доверенного кода; данных в нём нет."""
    from tools.autonomy.policy import task_scope
    ev = incident["evidence"]
    sig, tc = ev["signal"], ev["task_class"]
    cls = policy["task_classes"]["classes"][tc]
    slug = hashlib.sha256(incident["deduplication_key"].encode()).hexdigest()[:10]
    facts = ", ".join(f"{k}={sig[k]}" for k in ("loader_name", "pipeline_id", "error_code", "failure_signature",
                                                 "recorded_as_transient", "reason_code", "message_fingerprint",
                                                 "occurrences_7d", "recovery_status") if k in sig)
    objective = {
        "schema_version": 1,
        "objective_id": f"obj-sig-{slug}",
        "kind": "INCIDENT",
        "created_at": now_iso(),
        "requested_by": "autonomy-watch",
        "title": f"{tc}: {sig.get('loader_name') or sig.get('pipeline_id') or 'сигнал'}"[:200],
        "statement": (
            f"Класс задачи {tc} (правило {ev['classifier_rule']}): {cls['description']} Факты сигнала: {facts}. "
            "Установить первопричину в коде репозитория. Исправление — минимальное, только в пределах allowlist "
            "класса (поле scope), с тестом, который краснеет без исправления. Текст исходной ошибки в цель не "
            "включён (политика вывода B3): найти его можно read-only запросом к журналу прогонов, но не цитировать "
            "в отчёте. Если причина вне кода (данные, production-состояние, бизнес-правило) — не исправлять, "
            "вернуть NEEDS_HUMAN.")[:4000],
        "incident": incident,
        "acceptance": [
            "регрессионный тест воспроизводит исходный отказ и краснеет без исправления",
            "ни один тест, бывший PASS на базовой ветке, не становится FAIL",
            "изменения только в пределах allowlist класса и лимита размера диффа",
            "ни одни ворота не ослаблены; production-мутаций: 0",
        ],
        "constraints": [
            "не менять production: никакого DDL/DML, deploy, terraform apply, IAM, расписаний",
            "не писать в WB и Ozon; не цитировать строки данных, суммы и тексты покупателей",
            "не трогать доверенную базу (trusted_computing_base) и секреты (forbidden_paths) политики AE",
        ],
        "repository_sha": incident["repository_sha"],
        "affected_assets": incident["affected_assets"],
        "downstream_consumers": [],
        "contract_suites": [],
        "known_ubr_links": [],
        "evidence": ev,
        "owner_ack": None,
        "deduplication_key": incident["deduplication_key"],
        "execute": True,
        "task_class": tc,
    }
    scope = task_scope(objective, policy)
    if scope is None:
        raise ValueError(f"{tc}: у класса нет области — цель не создаётся (fail closed)")
    objective["scope"] = {k: scope[k] for k in ("allowed_paths", "max_changed_lines", "max_files")}
    require_valid(objective, "objective")
    return objective
