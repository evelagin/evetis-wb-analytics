"""Автомат состояний арендатора (Tenancy T5, §L) — чистая логика переходов и валидаторов.

Порядок и полномочия (ревью PR #226):
  * событие получает номер seq; номер занимается атомарно — созданием таблицы-маркера
    tenant_locks.S_<seq> (409 = параллельный переход, запись отменяется). Порядок — по seq, а не
    по часам разных писателей; метаданные таблиц читаются консистентно, в отличие от
    tabledata.list (строки потоковой вставки видны с задержкой до ~90 мин);
  * ребро оператора (кроме приостановки) с номером N действительно, только если владелец создал
    таблицу-решение ref.OPD_<N> с тем же ребром в метках. В ref у control нет tables.create, решение
    привязано к номеру и повторно не предъявляется; tables.get консистентен (ревью PR #226, 2-й проход);
  * стоп-кран владельца — ref.OPH_<n> (suspend/resume): пока последний — suspend, действует только
    приостановка; control и runtime его видят и подделать не могут;
  * приостановка (→ SUSPENDED) допустима всегда, даже при испорченном журнале.

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
import re
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
    decisions: dict | None = None                          # seq → решение владельца ref.OPD_<seq>
    hold: bool = False                                     # стоп-кран владельца ref.OPH_* действует
    accepted_limitations: set = field(default_factory=set)     # {(домен, boundary_id)}, принятые владельцем


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
            # COMPLETE — граница найдена внутри принятых API окон; NOT_APPLICABLE — активности нет во
            # всём принятом диапазоне. PARTIAL (предел хранения / документированная граница) и
            # UNKNOWN — не READY: принять ограничение может только решение владельца (новая версия политики).
            accepted = h.get("completeness_status") == "PARTIAL" and h.get("boundary_id") and \
                (d, h.get("boundary_id")) in s.accepted_limitations
            if h.get("completeness_status") not in ("COMPLETE", "NOT_APPLICABLE") and not accepted:
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
    """По номеру seq (линеаризация через tenant_locks); время — только для строк без номера."""
    return sorted(events, key=lambda e: (0 if e.get("seq") is not None else 1, int(e.get("seq") or 0),
                                         as_utc(e.get("occurred_at")) or _EPOCH, str(e.get("event_id"))))


def current_state(events):
    """Состояние — событие с наибольшим seq (как V_TENANT_STATE_CURRENT). Строки без номера — формат до
    T5: учитываются, только если нумерованных нет. Недействительные маркеры (invalid_marker) состоянием
    не бывают — их ловит audit_history."""
    ev = [e for e in events if not e.get("invalid_marker")]
    numbered = [e for e in ev if e.get("seq") is not None]
    if numbered:
        return max(numbered, key=lambda e: (int(e["seq"]), as_utc(e.get("occurred_at")) or _EPOCH,
                                            str(e.get("event_id"))))["to_state"]
    ev = ordered(ev)
    return ev[-1]["to_state"] if ev else None


def decision_problem(e, decisions) -> str | None:
    """Ребро оператора без решения владельца ref.OPD_<seq> (кроме приостановки) — недействительно."""
    if (e.get("actor") or "").split(":")[0] != OPERATOR or e.get("to_state") == SUSPENDED:
        return None
    d = (decisions or {}).get(int(e.get("seq") or 0))
    if not d:
        return f"нет решения владельца ref.OPD_{int(e.get('seq') or 0):06d} — подделка"
    if d.get("to_state") != e.get("to_state") or (d.get("expect_state") or None) != (e.get("from_state") or None):
        return "решение владельца не совпадает с ребром"
    return None


def audit_history(events, decisions=None) -> list[str]:
    """Независимая проверка журнала: каждое событие — разрешённое ребро разрешённым исполнителем;
    номера seq идут подряд; при переданных решениях — у рёбер оператора есть решение владельца."""
    out, prev = [], None
    out += [f"маркер {e['invalid_marker']} недействителен (номер вне формата или срок жизни)"
            for e in events if e.get("invalid_marker")]
    events = [e for e in events if not e.get("invalid_marker")]
    seqs = [e.get("seq") for e in events if e.get("seq") is not None]
    if seqs and sorted(int(x) for x in seqs) != list(range(1, len(seqs) + 1)):
        out.append(f"номера событий не подряд: {sorted(int(x) for x in seqs)[:10]}")
    for e in ordered(events):
        edge = (e.get("from_state"), e.get("to_state"))
        if e.get("from_state") != prev:
            out.append(f"{e.get('event_id')}: from={e.get('from_state')} при текущем {prev}")
        if edge not in EDGES:
            out.append(f"{e.get('event_id')}: ребро {edge} не разрешено")
        elif (e.get("actor") or "").split(":")[0] not in EDGES[edge][0]:
            out.append(f"{e.get('event_id')}: исполнитель {e.get('actor')} не допустим для {edge}")
        if decisions is not None and (why := decision_problem(e, decisions)):
            out.append(f"{e.get('event_id')}: {why}")
        prev = e.get("to_state")
    return out


def evidence_hash(evidence: dict) -> str:
    return hashlib.sha256(json.dumps(evidence, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def decide(s: Snapshot, to_state: str, actor: str, reason_code: str, evidence: dict, run_id: str,
           decision_id: str | None = None):
    if s.hold and to_state != SUSPENDED:
        return "REJECT", ["стоп-кран владельца (ref.OPH_*): допустима только приостановка"], None
    return _decide(s, to_state, actor, reason_code, evidence, run_id, decision_id)


def next_seq(events) -> int:
    return max([int(e["seq"]) for e in events if e.get("seq") is not None and not e.get("invalid_marker")] or [0]) + 1


def _decide(s, to_state, actor, reason_code, evidence, run_id, decision_id):
    """Решение о переходе. (статус, нарушения, событие | None); статус ∈ WRITE | NOOP | REJECT.

    Событие несёт seq = следующий номер; записать его можно, только заняв маркер S_<seq>.
    """
    frm = current_state(s.events)
    actor_class = actor.split(":")[0]
    problems = audit_history(s.events, s.decisions)
    if problems and not (to_state == SUSPENDED and frm != SUSPENDED and reason_code):
        return "REJECT", ["журнал состояний некорректен: " + "; ".join(problems[:3])], None
    ev_hash = evidence_hash(evidence)
    if s.events and frm == to_state:
        return "NOOP", [], None                      # уже в целевом состоянии: повтор ничего не пишет
    if problems:                                     # приостановка испорченного журнала: без проверки ребра
        edge = None
    else:
        edge = (frm, to_state)
    if edge is not None and edge not in EDGES:
        return "REJECT", [f"ребро {frm} → {to_state} не разрешено (прыжок через состояние)"], None
    actors, validator = EDGES[edge] if edge else ({OPERATOR, CONTROL}, _v_suspend)
    if actor_class not in actors:
        return "REJECT", [f"исполнитель {actor_class} не может {frm} → {to_state}"], None
    if to_state == SUSPENDED and not reason_code:
        return "REJECT", ["приостановка без причины"], None
    seq = next_seq(s.events)
    if seq > MAX_SEQ:
        return "REJECT", [f"номер перехода {seq} вне формата S_<6 цифр> — журнал испорчен (стоп-кран — ref.OPH_*)"], None
    if actor_class == OPERATOR and to_state != SUSPENDED:
        d = (s.decisions or {}).get(seq)
        if not d or d.get("to_state") != to_state or (d.get("expect_state") or None) != frm:
            return "REJECT", [f"ребро оператора требует решения владельца ref.OPD_{seq:06d}"], None
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
        "seq": seq, "decision_id": decision_id,
    }
    return "WRITE", [], event


# ───────────────────────────────────────────── маркеры переходов tenant_locks.S_<seq>
STATE_MARKER_RE = re.compile(r"^S_(\d{6})$")
MAX_SEQ = 999_999
DECISION_MARKER_RE = re.compile(r"^OPD_(\d{6})$")
HOLD_MARKER_RE = re.compile(r"^OPH_(\d{4})$")


def _label(v) -> str:
    return re.sub(r"[^a-z0-9_-]", "_", str(v).lower())[:63] if v is not None else "none"


def marker_name(seq: int) -> str:
    return f"S_{int(seq):06d}"


def marker_labels(event: dict) -> dict:
    """Метки маркера — всё, что нужно для цепочки без чтения строк (tables.list консистентен)."""
    return {k: _label(v) for k, v in {"to": event["to_state"], "from": event.get("from_state"),
                                      "actor": event["actor"].split(":")[0], "decision": event.get("decision_id"),
                                      "event": event["event_id"]}.items()}


def chain_from_markers(items) -> list[dict]:
    """items — [(имя таблицы, метки, момент создания сервером)] → события цепочки (без маркеров — нет)."""
    out = []
    for item in items:
        name, labels, created = item[:3]
        expires = item[3] if len(item) > 3 else None
        m = STATE_MARKER_RE.match(name or "")
        if not m or expires:
            # Вне формата или со сроком жизни (маркер, который «сотрётся» сам, — подлог истории).
            if (name or "").startswith("S_"):
                out.append({"invalid_marker": name, "occurred_at": created})
            continue
        lb = labels or {}
        val = lambda k: None if lb.get(k) in (None, "none") else lb[k]
        up = lambda k: val(k).upper() if val(k) else None
        out.append({"seq": int(m.group(1)), "event_id": val("event"), "from_state": up("from"),
                    "to_state": up("to"), "actor": up("actor") or "", "decision_id": val("decision"),
                    "occurred_at": created})
    return out


# ───────────────────────────────────────────── знаки владельца в ref (у control нет tables.create)
def decision_marker_name(seq: int) -> str:
    return f"OPD_{int(seq):06d}"


def decision_marker(event_from, event_to, **payload) -> tuple[dict, str]:
    """(метки, описание) таблицы-решения ref.OPD_<seq> для ребра оператора."""
    # Описание не обрезается (лимит BigQuery 16384): обрезанный JSON потерял бы plan_hash.
    return ({"to": _label(event_to), "from": _label(event_from)},
            json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))


def decision_from_marker(labels, description) -> dict:
    lb = labels or {}
    val = lambda k: None if lb.get(k) in (None, "none") else lb[k].upper()
    try:
        extra = json.loads(description or "{}")
    except ValueError:
        extra = {}
    return dict(extra if isinstance(extra, dict) else {}, to_state=val("to"), expect_state=val("from"))


def hold_active(items) -> bool:
    """items — [(имя, метки)] стоп-крана ref.OPH_<n>: действует, если последний знак — НЕ явный resume
    (метки нет или она иная — тоже остановка: fail-closed)."""
    best = (0, None)
    for name, labels in items:
        m = HOLD_MARKER_RE.match(name or "")
        if m and int(m.group(1)) > best[0]:
            best = (int(m.group(1)), (labels or {}).get("action"))
    return best[0] > 0 and best[1] != "resume"


def series_gap(numbers) -> bool:
    """Номера знаков владельца обязаны идти подряд с 1 (владелец занимает max+1); пропуск — подлог
    или ручное удаление, и решения по такой серии не принимаются."""
    ns = sorted(numbers)
    return ns != list(range(1, len(ns) + 1))
