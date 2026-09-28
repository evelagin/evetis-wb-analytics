"""Бюджет выгрузок Performance API (Tenancy T5, §I) — консервативная модель без выдуманной квоты.

Swagger Performance 2026-09-28, раздел «Лимиты»:
  * одна кампания в запросе = одна выгрузка; несколько кампаний = несколько выгрузок;
  * выгрузок за 24 ч: min(активные кампании × 240, 2000) на аккаунт; 2000 на организацию;
  * одновременных выгрузок с аккаунта — 1; кампаний в отчёте — не больше 10; дней — не больше 62.

Точную остаточную квоту API не отдаёт. Поэтому:
  * лимит оценивается снизу: активные кампании — только наблюдаемые (RUNNING в реестре);
    неизвестно — ставится CONSERVATIVE_FLOOR;
  * резерв под ежедневные инкрементальные прогоны (DAILY_RESERVE) и доля запаса SAFETY;
  * израсходованное за скользящие 24 ч — по собственному журналу (выгрузки каждого отрезка
    записаны в evidence чекпойнта), а не по догадке о чужих расходах;
  * нет бюджета — отрезок не берётся, продолжение — в следующем окне (resumable), не ошибка;
  * ошибка квоты от API (ERROR отчёта / 429) — класс QUOTA: не штрафует попытками.
"""
from __future__ import annotations

from datetime import datetime, timedelta

PER_ACTIVE_CAMPAIGN = 240
ACCOUNT_CAP = 2000
CONSERVATIVE_FLOOR = 60          # если активных кампаний не видно: не больше 60 выгрузок/сутки
SAFETY = 0.5                     # берём половину оценки
DAILY_RESERVE = 50               # под ежедневные прогоны ads_sku_daily (окно 7 суток)


def estimated_daily_limit(active_campaigns: int | None) -> int:
    if active_campaigns is None or active_campaigns <= 0:
        return CONSERVATIVE_FLOOR
    return min(active_campaigns * PER_ACTIVE_CAMPAIGN, ACCOUNT_CAP)


def consumed_last_24h(ledger, now: datetime) -> int:
    """Выгрузки своего журнала за скользящие 24 ч: [(момент, число выгрузок)]."""
    since = now - timedelta(hours=24)
    return sum(int(n) for at, n in ledger if at and at > since)


def budget(active_campaigns: int | None, ledger, now: datetime) -> dict:
    limit = estimated_daily_limit(active_campaigns)
    # Резерв — не больше четверти оценки: при консервативном полу бюджет не обнуляется, и
    # бэкфилл идёт медленно, но идёт (15 выгрузок/сутки при полу 60).
    usable = int(limit * SAFETY) - min(DAILY_RESERVE, int(limit * 0.25))
    used = consumed_last_24h(ledger, now)
    return {"estimated_limit": limit, "usable": max(usable, 0), "used_24h": used,
            "remaining": max(usable - used, 0),
            "basis": "observed_running_campaigns" if active_campaigns else "conservative_floor"}


def can_run(chunk_exports: int, b: dict) -> bool:
    """Отрезок берётся, только если все его выгрузки укладываются в остаток."""
    return 0 < chunk_exports <= b["remaining"] or chunk_exports == 0
