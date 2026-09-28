"""Автомат состояний арендатора (Tenancy T5, §L) — чистая логика переходов и валидаторов.

Никакого «set-state»: переход записывается, только если
  * текущее состояние (последнее событие журнала) равно `from`;
  * ребро разрешено и исполнитель ребра — из допустимых (OPERATOR | CONTROL);
  * валидатор ребра по снимку доказательств не нашёл ни одного нарушения;
  * журнал до этого момента сам по себе корректен (audit_history без нарушений).
Повтор того же перехода с тем же хешем доказательств ничего не делает (идемпотентность).

Снимок доказательств (Snapshot) собирает control из таблиц tenant_ops / ref / ozon_raw (Tables
API), оператор — своими инструментами. Валидатор получает только снимок: сети и облака нет.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

from identity import as_utc

CREDENTIALS_PENDING = "CREDENTIALS_PENDING"
VALIDATING = "VALIDATING"
CAPABILITY_DISCOVERY = "CAPABILITY_DISCOVERY"
READY_FOR_BACKFILL = "READY_FOR_BACKFILL"
BACKFILLING = "BACKFILLING"
RECONCILING = "RECONCILING"
READY = "READY"
SUSPENDED = "SUSPENDED"
STATES = (CREDENTIALS_PENDING, VALIDATING, CAPABILITY_DISCOVERY, READY_FOR_BACKFILL, BACKFILLING,
          RECONCILING, READY, SUSPENDED)
OPERATOR, CONTROL = "OPERATOR", "CONTROL"
# NOT_APPLICABLE — проверять нечего (например, асинхронный отчёт при отсутствии CPC-расхода).
OK_CAPABILITY = ("AVAILABLE", "NOT_APPLICABLE")

# Сущности с историей (бэкфилл) и снимки (истории нет).
HISTORICAL_DOMAINS = ("fbo_postings", "finance_accrual", "ads_expense_daily", "ads_sku_daily")
ADS_DOMAINS = ("ads_campaigns", "ads_expense_daily", "ads_sku_daily")
# Проверки DQ, которые обязаны быть в последнем прогоне DQ со статусом PASS для READY.
READY_REQUIRED_DQ = ("CHECKPOINTS_COMPLETE", "COVERAGE", "TRUNCATION", "RAW_UNIQUENESS",
                     "FIN_CLASSIFICATION", "FIN_MATURITY", "BINDING")


@dataclass
class Snapshot:
    tenant_id: str
    now: datetime
    events: list = field(default_factory=list)            # строки TENANT_STATE_EVENTS
    enabled_entities: tuple = ()
    credentials: dict = field(default_factory=dict)       # api → вердикт credentials.evaluate_*
    binding: dict = field(default_factory=dict)            # api → статус live (identity.live_status)
    capabilities: dict = field(default_factory=dict)       # (api, capability) → статус
    required_capabilities: tuple = ()                      # [(api, capability)] для включённых сущностей
    history: dict = field(default_factory=dict)            # domain → строка HISTORY_BOUNDARIES
    plan_hash: str | None = None
    chunks: dict = field(default_factory=dict)             # chunk_id → статус (checkpoints.fold)
    last_chunk_done_at: datetime | None = None
    dq: dict | None = None                                 # {"run_id", "evaluated_at", "checks": {id: {status, severity}}}
    maturity_days: float | None = None
    secret_versions: dict = field(default_factory=dict)    # имя секрета → число ENABLED-версий (оператор)
    operator_plan_hash: str | None = None                  # хеш плана, который подтверждает оператор


def _ads_enabled(s: Snapshot) -> bool:
    return any(e in ADS_DOMAINS for e in s.enabled_entities)


# ───────────────────────────────────────────── валидаторы рёбер
def _v_bootstrap(s):
    return ["журнал уже начат"] if s.events else []


def _v_credentials_inserted(s):
    out = []
    if len(s.secret_versions) < 4:
        out.append("не все 4 секрета проверены оператором")
    out += [f"секрет {k}: нет ENABLED-версии" for k, n in sorted(s.secret_versions.items()) if n < 1]
    return out


def _v_binding(s, need_perf: bool):
    out = []
    if (s.credentials.get("seller") or {}).get("status") != "PASS":
        out.append("ключ Seller не прошёл проверку «только чтение»")
    if s.binding.get("seller") != "BOUND":
        out.append(f"привязка Seller: {s.binding.get('seller') or 'нет'}")
    if need_perf:
        if (s.credentials.get("performance") or {}).get("status") != "PASS":
            out.append("Performance API не прошёл проверку")
        if s.binding.get("performance") != "BOUND":
            out.append(f"привязка Performance: {s.binding.get('performance') or 'нет'}")
    return out


def _v_to_discovery(s):
    return _v_binding(s, _ads_enabled(s))


def _v_to_ready_for_backfill(s):
    out = _v_binding(s, _ads_enabled(s))
    for key in s.required_capabilities:
        if s.capabilities.get(tuple(key)) not in OK_CAPABILITY:
            out.append(f"возможность {key[0]}.{key[1]}: {s.capabilities.get(tuple(key)) or 'не проверена'}")
    for d in HISTORICAL_DOMAINS:
        if d in s.enabled_entities and d not in s.history:
            out.append(f"границы истории {d} не определены")
    if not s.plan_hash or not s.chunks:
        out.append("план бэкфилла не построен")
    return out


def _v_to_backfilling(s):
    out = _v_binding(s, _ads_enabled(s))
    if not s.plan_hash:
        out.append("плана нет")
    elif s.operator_plan_hash != s.plan_hash:
        out.append("оператор подтвердил другой план (хеш не совпал)")
    return out


def _v_to_reconciling(s):
    out = []
    if not s.chunks:
        out.append("план пуст")
    pending = [c for c, st in s.chunks.items() if st != "DONE"]
    if pending:
        out.append(f"не завершено отрезков: {len(pending)}")
    return out


def ready_contract(s: Snapshot) -> list[str]:
    """RECONCILING → READY: все условия владельца одновременно (решение T5, §4)."""
    out = _v_binding(s, _ads_enabled(s))
    for key in s.required_capabilities:
        if s.capabilities.get(tuple(key)) not in OK_CAPABILITY:
            out.append(f"возможность {key[0]}.{key[1]} не AVAILABLE")
    for d in HISTORICAL_DOMAINS:
        if d in s.enabled_entities:
            h = s.history.get(d) or {}
            if h.get("completeness_status") != "COMPLETE":
                out.append(f"история {d}: {h.get('completeness_status') or 'не определена'}")
    out += _v_to_reconciling(s)
    if s.maturity_days is None or s.maturity_days <= 0:
        out.append("FINANCE_SETTLEMENT_MATURITY_DAYS не задан (D3)")
    dq = s.dq or {}
    if not dq:
        out.append("прогона DQ нет")
    else:
        if s.last_chunk_done_at and dq.get("evaluated_at") and dq["evaluated_at"] < s.last_chunk_done_at:
            out.append("прогон DQ старше последнего завершённого отрезка")
        checks = dq.get("checks") or {}
        blocking = [c for c, v in checks.items() if v.get("severity") == "BLOCKING" and v.get("status") != "PASS"]
        if blocking:
            out.append(f"BLOCKING DQ: {sorted(blocking)}")
        for c in READY_REQUIRED_DQ:
            if (checks.get(c) or {}).get("status") != "PASS":
                out.append(f"проверка DQ {c}: {(checks.get(c) or {}).get('status') or 'не выполнена'}")
    return out


def _v_back_to_backfilling(s):
    pending = [c for c, st in s.chunks.items() if st != "DONE"]
    return [] if pending else ["нет новых отрезков — возвращаться в BACKFILLING незачем"]


def _v_suspend(s):
    return []


def _v_resume(s):
    return []


EDGES = {
    (None, CREDENTIALS_PENDING): ({OPERATOR}, _v_bootstrap),
    (CREDENTIALS_PENDING, VALIDATING): ({OPERATOR}, _v_credentials_inserted),
    (VALIDATING, CAPABILITY_DISCOVERY): ({CONTROL}, _v_to_discovery),
    (CAPABILITY_DISCOVERY, READY_FOR_BACKFILL): ({CONTROL}, _v_to_ready_for_backfill),
    (READY_FOR_BACKFILL, BACKFILLING): ({OPERATOR}, _v_to_backfilling),
    (BACKFILLING, RECONCILING): ({CONTROL}, _v_to_reconciling),
    (RECONCILING, READY): ({CONTROL}, ready_contract),
    (RECONCILING, BACKFILLING): ({CONTROL}, _v_back_to_backfilling),
    (SUSPENDED, VALIDATING): ({OPERATOR}, _v_resume),
    (READY, VALIDATING): ({OPERATOR}, _v_resume),          # ротация ключа
}
for _st in STATES:
    if _st != SUSPENDED:
        EDGES[(_st, SUSPENDED)] = ({OPERATOR, CONTROL}, _v_suspend)


# ───────────────────────────────────────────── журнал
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def ordered(events):
    return sorted(events, key=lambda e: (as_utc(e.get("occurred_at")) or _EPOCH, str(e.get("event_id"))))


def current_state(events):
    ev = ordered(events)
    return ev[-1]["to_state"] if ev else None


def audit_history(events) -> list[str]:
    """Независимая проверка журнала: каждое событие — разрешённое ребро разрешённым исполнителем."""
    out, prev = [], None
    for e in ordered(events):
        edge = (e.get("from_state"), e.get("to_state"))
        if e.get("from_state") != prev:
            out.append(f"{e.get('event_id')}: from={e.get('from_state')} при текущем {prev}")
        if edge not in EDGES:
            out.append(f"{e.get('event_id')}: ребро {edge} не разрешено")
        elif (e.get("actor") or "").split(":")[0] not in EDGES[edge][0]:
            out.append(f"{e.get('event_id')}: исполнитель {e.get('actor')} не допустим для {edge}")
        prev = e.get("to_state")
    return out


def evidence_hash(evidence: dict) -> str:
    return hashlib.sha256(json.dumps(evidence, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def decide(s: Snapshot, to_state: str, actor: str, reason_code: str, evidence: dict, run_id: str):
    """Решение о переходе. (статус, нарушения, событие | None); статус ∈ WRITE | NOOP | REJECT."""
    frm = current_state(s.events)
    actor_class = actor.split(":")[0]
    problems = audit_history(s.events)
    if problems:
        return "REJECT", ["журнал состояний некорректен: " + "; ".join(problems[:3])], None
    ev_hash = evidence_hash(evidence)
    last = ordered(s.events)[-1] if s.events else None
    if last and last.get("to_state") == to_state and frm == to_state:
        prior = json.loads(last.get("evidence_json") or "{}").get("evidence_hash")
        if prior == ev_hash:
            return "NOOP", [], None
    edge = (frm, to_state)
    if edge not in EDGES:
        return "REJECT", [f"ребро {frm} → {to_state} не разрешено (прыжок через состояние)"], None
    actors, validator = EDGES[edge]
    if actor_class not in actors:
        return "REJECT", [f"исполнитель {actor_class} не может {frm} → {to_state}"], None
    if to_state == SUSPENDED and not reason_code:
        return "REJECT", ["приостановка без причины"], None
    failures = validator(s)
    if failures:
        return "REJECT", failures, None
    event = {
        "event_id": hashlib.sha256(f"{s.tenant_id}|{frm}|{to_state}|{ev_hash}|{run_id}".encode()).hexdigest()[:32],
        "tenant_id": s.tenant_id, "from_state": frm, "to_state": to_state,
        "reason_code": reason_code, "reason_detail": None,
        "evidence_json": json.dumps(dict(evidence, evidence_hash=ev_hash), sort_keys=True, ensure_ascii=False,
                                    default=str),
        "actor": actor, "run_id": run_id, "occurred_at": s.now.isoformat(),
    }
    return "WRITE", [], event
