"""План бэкфилла, журнал чекпойнтов и аренда отрезков (Tenancy T5, §G/§J) — чистая логика.

Журнал tenant_ops.BACKFILL_CHECKPOINTS только дописывается (Tables API insertAll у control; ни
UPDATE, ни DELETE у него нет — нет bigquery.jobs.create). Состояние отрезка — свёртка его версий:
  * DONE неизменяем: первая версия DONE побеждает всё последующее, кроме REOPENED;
  * REOPENED — единственная процедура ремонта: пишет только оператор, с причиной; после неё
    отрезок снова PENDING, счётчик попыток сохраняется;
  * иначе — последняя версия по recorded_at (PENDING | RUNNING | FAILED);
  * FAILED с attempts ≥ MAX_ATTEMPTS (кроме ошибки квоты) — FAILED_PERMANENT.

Аренда отрезка — атомарное «создать, если нет» в BigQuery: таблица
tenant_locks.L_<chunk_id>_<generation> с expirationTime = lease_until. tables.insert возвращает
409 при существующем имени, поэтому из двух исполнителей побеждает ровно один. Истёкшая аренда
перехватывается созданием следующего поколения; поколение N+1 создаётся только после истечения N
(иначе claim отказывает), поэтому живая аренда в каждый момент не больше одной.
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


def fold(rows) -> dict:
    """chunk_id → {'status', 'attempts', 'done_at', 'last_error_class', 'run_id'}."""
    by = {}
    for r in rows:
        by.setdefault(r["backfill_id"], []).append(r)
    out = {}
    for cid, versions in by.items():
        vs = sorted(versions, key=lambda r: (as_utc(r.get("updated_at")) or _EPOCH, str(r.get("status"))))
        attempts = max(int(r.get("attempts") or 0) for r in vs)
        state, done_at, err, run = "PENDING", None, None, None
        for r in vs:
            st = r.get("status")
            if st == "REOPENED" and str(r.get("run_id") or "").startswith("operator:"):
                state, done_at = "PENDING", None
                continue
            if state == "DONE":
                continue                         # DONE неизменяем до REOPENED оператора
            if st == "DONE":
                state, done_at, run = "DONE", as_utc(r.get("updated_at")), r.get("run_id")
            elif st in ("PENDING", "RUNNING", "FAILED"):
                state, err, run = st, r.get("error_code"), r.get("run_id")
        if state == "FAILED" and attempts >= MAX_ATTEMPTS and err != QUOTA_ERROR_CLASS:
            state = "FAILED_PERMANENT"
        out[cid] = {"status": state, "attempts": attempts, "done_at": done_at,
                    "last_error_class": err, "run_id": run}
    return out


def lease_name(chunk_id: str, generation: int) -> str:
    return f"L_{chunk_id}_{generation:04d}"


def next_lease_generation(lease_tables, chunk_id: str, now: datetime):
    """Поколение для новой аренды или None, если живая аренда есть.

    lease_tables — [(имя таблицы, expiration datetime|None)] из tenant_locks. Истёкшая таблица
    (или уже удалённая по сроку) аренду не держит.
    """
    gens = []
    for name, exp in lease_tables:
        m = LEASE_RE.match(name)
        if not m or m.group(1) != chunk_id:
            continue
        g = int(m.group(2))
        exp = as_utc(exp)
        if exp is None or exp > now:
            return None                          # живая (или бессрочная — подозрительная) аренда
        gens.append(g)
    return (max(gens) + 1) if gens else 1


def claimable(state: dict | None) -> bool:
    return state is None or state["status"] in ("PENDING", "FAILED", "RUNNING")


def next_chunk(chunks, folded: dict):
    """Первый по плану отрезок, который можно взять (DONE и FAILED_PERMANENT — нет)."""
    for c in chunks:
        if claimable(folded.get(c.chunk_id)):
            return c
    return None
