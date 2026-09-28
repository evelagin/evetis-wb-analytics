"""Границы истории по доменам (Tenancy T5, §H) — ограниченный поиск самой ранней активности.

Проба — функция probe(start, end) → ProbeResult: DATA (с first_date, если API её отдаёт),
EMPTY, REJECTED (API отказал окно — предел хранения/период) или ERROR (сбой → поиск прерывается,
fail closed). Пустой ответ доказывает отсутствие активности только там, где API окно ПРИНЯЛ;
дата раньше принятого окна или раньше документированной границы — NOT_AVAILABLE, а не «нет данных».

Алгоритмы (стоимость — число вызовов API, верхняя граница указана):
  fbo_postings    — /v3/posting/fbo/list, sort_dir ASC, limit 1: первое отправление окна приходит
                    первым. Окна ≤ 365 суток вперёд от нижней границы; первая DATA — точная дата.
                    ≤ ceil((today − floor)/365) + 1 вызовов (floor 2018-01-01 → ≤ 10).
  ads_expense_daily — /statistics/expense помесячно вперёд от нижней границы; в первом месяце с
                    расходом — двоичный поиск суток по монотонному предикату «расход в [start, mid]».
                    ≤ число месяцев + 5 вызовов (от 2018 — около 110, ~2 мин при 1 запросе/с).
  finance_accrual — /v1/finance/accrual/by-day: только сутки. Нижняя граница документирована
                    (2022-01-01). Выборка 3 суток в месяц (5/15/25) от границы до якоря (первой
                    активности FBO/рекламы или сегодня), затем посуточно месяц M−1 и M, где M —
                    первый месяц с данными. ≤ 36·лет + 62 вызова; уверенность SAMPLED.
  ads_sku_daily   — производная CPC-расхода: граница = первые сутки CPC-расхода (проба отчёта —
                    на первом отрезке бэкфилла).
Подсказка оператора («примерно с 2024») задаёт только стартовую точку уточнения (seed): если в
окне до seed данных нет — поиск идёт от seed; граница из подсказки не берётся никогда.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

DATA, EMPTY, REJECTED, ERROR = "DATA", "EMPTY", "REJECTED", "ERROR"
DOCUMENTED_FLOOR = {"finance_accrual": date(2022, 1, 1)}   # Swagger /v1/finance/accrual/by-day
SEARCH_FLOOR = date(2018, 1, 1)                             # начало маркетплейса для продавцов (INFERRED)
# FBO: период ≤ 1 года документирован. Расход рекламы: длина окна не документирована — месяц,
# как в аудите EVETIS (отказ длинного окна иначе выглядел бы как предел хранения).
WINDOW_DAYS = {"fbo_postings": 365, "ads_expense_daily": 31}
SAMPLE_DAYS = (5, 15, 25)


class HistoryProbeError(RuntimeError):
    """Проба вернула ERROR: границу определить нельзя, поиск прерван."""


@dataclass
class ProbeResult:
    status: str
    first_date: date | None = None
    http: int | None = None


@dataclass
class Boundary:
    domain: str
    api_documented_from: date | None = None
    api_verified_from: date | None = None
    first_observed_activity: date | None = None
    limitation_reason: str | None = None
    completeness_status: str = "UNKNOWN"
    confidence: str = "INFERRED"
    calls: int = 0
    evidence: list = field(default_factory=list)


def _windows(start: date, end: date, n: int):
    cur = start
    while cur <= end:
        e = min(cur + timedelta(days=n - 1), end)
        yield cur, e
        cur = e + timedelta(days=1)


def _call(b: Boundary, probe, s, e):
    r = probe(s, e)
    b.calls += 1
    b.evidence.append({"from": str(s), "to": str(e), "status": r.status, "http": r.http,
                       "first_date": str(r.first_date) if r.first_date else None})
    if r.status == ERROR:
        raise HistoryProbeError(f"{b.domain}: проба {s}..{e} — ERROR (http {r.http})")
    return r


def windowed_first_activity(domain: str, probe, today: date, seed: date | None = None,
                            floor: date | None = None, exact_first=False, max_calls: int = 64) -> Boundary:
    """fbo_postings (exact_first=True: проба отдаёт первую дату) и ads_expense_daily (двоичный поиск)."""
    b = Boundary(domain, api_documented_from=DOCUMENTED_FLOOR.get(domain))
    lo = floor or DOCUMENTED_FLOOR.get(domain) or SEARCH_FLOOR
    n = WINDOW_DAYS[domain]
    starts = [lo]
    if seed and lo < seed <= today:
        # seed: сначала проверяется, есть ли что-то ДО seed; если нет — поиск от seed.
        before = list(_windows(lo, seed - timedelta(days=1), n))
        found = False
        for s, e in before:
            r = _call(b, probe, s, e)
            if r.status == REJECTED:
                continue
            b.api_verified_from = b.api_verified_from or s
            if r.status == DATA:
                found = True
                break
        starts = [lo] if found else [seed]
    first_window = None
    for s, e in _windows(starts[0], today, n):
        if b.calls >= max_calls:
            raise HistoryProbeError(f"{domain}: превышен бюджет проб {max_calls}")
        r = _call(b, probe, s, e)
        if r.status == REJECTED:
            b.limitation_reason = "API_RETENTION"
            continue
        b.api_verified_from = b.api_verified_from or s
        if r.status == DATA:
            first_window = (s, e, r)
            break
    if first_window is None:
        b.completeness_status = "NOT_APPLICABLE"              # активности нет во всём принятом диапазоне
        b.confidence = "VERIFIED_EMPTY" if not b.limitation_reason else "EMPTY_AFTER_RETENTION"
        return b
    s, e, r = first_window
    if exact_first and r.first_date:
        b.first_observed_activity, b.confidence = r.first_date, "VERIFIED"
    else:
        a, z = s, e                                           # «данные в [s, mid]» монотонен по mid
        while a < z:
            mid = a + timedelta(days=(z - a).days // 2)
            if _call(b, probe, s, mid).status == DATA:
                z = mid
            else:
                a = mid + timedelta(days=1)
        b.first_observed_activity, b.confidence = a, "VERIFIED"
    if b.limitation_reason == "API_RETENTION" and b.first_observed_activity == b.api_verified_from:
        b.completeness_status = "PARTIAL"                     # активность упирается в предел хранения
    return b


def sampled_first_activity(domain: str, probe_day, today: date, anchor: date | None = None,
                           max_calls: int = 400) -> Boundary:
    """finance_accrual: сутки только по одной; выборка 5/15/25 + посуточное уточнение."""
    floor = DOCUMENTED_FLOOR[domain]
    b = Boundary(domain, api_documented_from=floor)
    end = min(anchor or today, today)
    first_month = None
    m = date(floor.year, floor.month, 1)
    while m <= end and first_month is None:
        for d in SAMPLE_DAYS:
            day = m.replace(day=d)
            if day < floor or day > end:
                continue
            if b.calls >= max_calls:
                raise HistoryProbeError(f"{domain}: превышен бюджет проб {max_calls}")
            r = _call(b, lambda s, e: probe_day(s), day, day)
            if r.status == REJECTED:
                b.limitation_reason = "API_RETENTION"
                continue
            b.api_verified_from = b.api_verified_from or day
            if r.status == DATA:
                first_month = m
                break
        m = (m.replace(day=28) + timedelta(days=4)).replace(day=1)
    if first_month is None:
        b.completeness_status, b.confidence = "NOT_APPLICABLE", "SAMPLED_EMPTY"
        return b
    prev = (first_month - timedelta(days=1)).replace(day=1)
    month_end = (first_month.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    d = max(prev, floor)
    while d <= min(month_end, end):                       # посуточно M−1 и M: ≤ 62 вызова
        r = _call(b, lambda s, e: probe_day(s), d, d)
        if r.status == DATA:
            b.first_observed_activity = d
            break
        d += timedelta(days=1)
    b.confidence = "SAMPLED"
    if b.first_observed_activity == floor:
        b.completeness_status, b.limitation_reason = "PARTIAL", "API_DOCUMENTED_FLOOR"
    return b
