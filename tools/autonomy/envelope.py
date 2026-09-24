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
