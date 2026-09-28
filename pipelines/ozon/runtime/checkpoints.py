"""План бэкфилла, журнал чекпойнтов и аренда отрезков (Tenancy T5, §G/§J) — чистая логика.

Журнал tenant_ops.BACKFILL_CHECKPOINTS только дописывается (Tables API insertAll у control; ни
UPDATE, ни DELETE у него нет — нет bigquery.jobs.create). Состояние отрезка — свёртка его версий:
  * DONE неизменяем: первая версия DONE побеждает всё последующее;
  * ремонт — только решение владельца REOPEN_CHUNK в ref.OPERATOR_DECISIONS со ссылкой на run_id
    той версии DONE, которую оно отменяет (control писать в ref не может; часы не участвуют).
    Строки REOPENED в журнале tenant_ops (прежний формат) игнорируются;
  * иначе — последняя версия по recorded_at (PENDING | RUNNING | FAILED);
  * FAILED, у которого неудач НЕ по квоте ≥ MAX_ATTEMPTS, — FAILED_PERMANENT. Ошибка квоты
    (QUOTA_ERROR_CLASS) попыткой не штрафуется: отрезок ждёт следующего окна бюджета (§I).

Аренда отрезка — атомарное «создать, если нет» в BigQuery: таблица tenant_locks.L_<chunk_id>_<gen>.
tables.insert возвращает 409 при существующем имени, поэтому из двух претендентов побеждает ровно
один. Срок аренды — метка until (секунды эпохи); сама таблица живёт ещё LEASE_TABLE_KEEP, чтобы
BigQuery не удалил её раньше, чем поколение перестанет быть нужным (иначе номер переиспользуется).
Поколение = max(поколения таблиц, поколения журнала) + 1. Перехват допустим только после
until + VISIBILITY_GRACE: строки журнала прогонов runtime (потоковая вставка) видны tabledata.list с
задержкой до ~90 мин, а исполнитель (таймаут job'а 1 ч) к этому моменту завершён.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from identity import as_utc

CHUNK_DAYS = {"fbo_postings": 7, "finance_accrual": 7, "ads_expense_daily": 31, "ads_sku_daily": 60}
MAX_ATTEMPTS = 5
LEASE_TTL = timedelta(hours=2)
VISIBILITY_GRACE = timedelta(hours=3)
LEASE_TABLE_KEEP = timedelta(days=7)
QUOTA_ERROR_CLASS = "QUOTA"
LEASE_RE = re.compile(r"^L_([0-9a-f]{16})_(\d{4})$")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class Chunk:
    domain: str
    start: date
    end: date

    @property
    def chunk_id(self) -> str:
        return hashlib.sha256(f"{self.domain}|{self.start}|{self.end}".encode()).hexdigest()[:16]


def plan(domain: str, start: date, end: date) -> list[Chunk]:
    """Сплошное покрытие [start, end] отрезками CHUNK_DAYS[domain] суток; детерминировано."""
    if domain not in CHUNK_DAYS:
        raise ValueError(f"{domain}: бэкфилл не определён (снимок или реестр)")
    if start > end:
        return []
    out, cur, n = [], start, CHUNK_DAYS[domain]
    while cur <= end:
        e = min(cur + timedelta(days=n - 1), end)
        out.append(Chunk(domain, cur, e))
        cur = e + timedelta(days=1)
    return out


def plan_hash(chunks) -> str:
    return hashlib.sha256(json.dumps(sorted((c.domain, str(c.start), str(c.end)) for c in chunks)).encode()).hexdigest()


def fold(rows, reopened_run_ids=frozenset()) -> dict:
    """chunk_id → {'status', 'attempts', 'penalized', 'done_at', 'last_error_class', 'run_id'}.

    reopened_run_ids — run_id версий DONE, отменённых решениями владельца REOPEN_CHUNK.
    """
    by = {}
    for r in rows:
        by.setdefault(r["backfill_id"], []).append(r)
    out = {}
    for cid, versions in by.items():
        vs = sorted(versions, key=lambda r: (as_utc(r.get("updated_at")) or _EPOCH, str(r.get("status"))))
        attempts = max(int(r.get("attempts") or 0) for r in vs)
        state, done_at, err, run = "PENDING", None, None, None
        # Неудачи, которые штрафуются: FAILED с классом не QUOTA, по одной на прогон исполнителя.
        penalized = len({r.get("run_id") for r in vs
                         if r.get("status") == "FAILED" and r.get("error_code") != QUOTA_ERROR_CLASS})
        for r in vs:
            st = r.get("status")
            if state == "DONE":
                continue                         # DONE неизменяем (кроме решения владельца)
            if st == "DONE" and r.get("run_id") in reopened_run_ids:
                state, done_at, run = "PENDING", None, None
                continue                         # эту версию DONE владелец отменил
            if st == "DONE":
                state, done_at, run = "DONE", as_utc(r.get("updated_at")), r.get("run_id")
            elif st in ("PENDING", "RUNNING", "FAILED"):
                state, err, run = st, r.get("error_code"), r.get("run_id")
        if state == "FAILED" and penalized >= MAX_ATTEMPTS:
            state = "FAILED_PERMANENT"
        out[cid] = {"status": state, "attempts": attempts, "penalized": penalized, "done_at": done_at,
                    "last_error_class": err, "run_id": run}
    return out


def lease_name(chunk_id: str, generation: int) -> str:
    return f"L_{chunk_id}_{generation:04d}"


def next_lease_generation(lease_tables, chunk_id: str, now: datetime, ledger_generations=()):
    """Поколение для новой аренды или None, если аренда ещё держится.

    lease_tables — [(имя таблицы, until datetime|None)] из tenant_locks (until — метка аренды).
    Аренда держится до until + VISIBILITY_GRACE; без метки — держится (подозрительная таблица).
    """
    gens = [int(g) for g in ledger_generations if g is not None]
    for name, until in lease_tables:
        m = LEASE_RE.match(name)
        if not m or m.group(1) != chunk_id:
            continue
        gens.append(int(m.group(2)))
        until = as_utc(until)
        if until is None or until + VISIBILITY_GRACE > now:
            return None
    return (max(gens) + 1) if gens else 1


def claimable(state: dict | None) -> bool:
    return state is None or state["status"] in ("PENDING", "FAILED", "RUNNING")


def next_chunk(chunks, folded: dict):
    """Первый по плану отрезок, который можно взять (DONE и FAILED_PERMANENT — нет)."""
    for c in chunks:
        if claimable(folded.get(c.chunk_id)):
            return c
    return None


def plan_versions(ledger) -> dict:
    """plan_hash → (отрезки, момент построения) по строкам PENDING команды plan.

    Версия, у которой видны не все строки (tabledata.list отстаёт от потоковой вставки), даёт
    другой хеш и отбрасывается: план используется только целиком."""
    by = {}
    for r in ledger:
        ph = r.get("plan_hash")
        if r.get("status") != "PENDING" or not ph:
            continue
        v = by.setdefault(ph, {"chunks": {}, "at": _EPOCH})
        v["chunks"][r["backfill_id"]] = Chunk(r["entity"], date.fromisoformat(str(r["window_from"])),
                                              date.fromisoformat(str(r["window_to"])))
        v["at"] = max(v["at"], as_utc(r.get("updated_at")) or _EPOCH)
    out = {}
    for ph, v in by.items():
        chunks = sorted(v["chunks"].values(), key=lambda c: (c.domain, c.start))
        if plan_hash(chunks) == ph:
            out[ph] = (chunks, v["at"])
    return out


def latest_plan(ledger):
    vs = plan_versions(ledger)
    if not vs:
        return None, []
    ph = max(vs, key=lambda h: (vs[h][1], h))
    return ph, vs[ph][0]
