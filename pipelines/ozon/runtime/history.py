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
Подсказка оператора («примерно с 2024», seed) записывается в доказательства, но границу не задаёт и
поиск не сокращает: «данных раньше нет» доказывает только проход вперёд от нижней границы.

Отказ API окну (REJECTED) — НЕ отсутствие активности (ревью PR #226):
  * отвергнутое окно дробится двоичным поиском самого раннего принятого начала [x, e]: данные в его
    принятой части не теряются;
  * любой отказ до первой активности делает границу PARTIAL (что было до предела — неизвестно);
    активности нет, но отказы были — тоже PARTIAL, а не NOT_APPLICABLE;
  * все пробы отвергнуты — ошибка (скорее всего, запрос неверен), а не «истории нет».
Первая дата из ответа (FBO, sort_dir ASC) подтверждается пробой [начало, первая − 1] = EMPTY; если
там есть данные (сортировку не соблюли) — двоичный поиск.
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


class _Search:
    def __init__(self, b: Boundary, probe, max_calls: int):
        self.b, self.probe, self.max_calls = b, probe, max_calls
        self.rejected = self.accepted = False

    def call(self, s, e):
        if self.b.calls >= self.max_calls:
            raise HistoryProbeError(f"{self.b.domain}: превышен бюджет проб {self.max_calls}")
        r = _call(self.b, self.probe, s, e)
        if r.status == REJECTED:
            self.rejected = True
            self.b.limitation_reason = "API_RETENTION"
        else:
            self.accepted = True
            self.b.api_verified_from = self.b.api_verified_from or s
        return r

    def earliest_accepted(self, s, e):
        """Самое раннее x ∈ (s, e], для которого окно [x, e] API принимает; (x, результат) | None.

        Предел хранения монотонен: если отвергнуты даже последние сутки окна, отвергнуто всё окно —
        одна проба вместо двоичного поиска.
        """
        if s == e or self.call(e, e).status == REJECTED:
            return None
        lo, hi, best = s + timedelta(days=1), e, None
        while lo <= hi:
            mid = lo + timedelta(days=(hi - lo).days // 2)
            r = self.call(mid, e)
            if r.status == REJECTED:
                lo = mid + timedelta(days=1)
            else:
                best, hi = (mid, r), mid - timedelta(days=1)
        return best

    def first_day(self, s, e):
        """Первые сутки с данными в [s, e] при известном DATA в [s, e] (предикат монотонен по концу)."""
        a, z = s, e
        while a < z:
            mid = a + timedelta(days=(z - a).days // 2)
            if self.call(s, mid).status == DATA:
                z = mid
            else:
                a = mid + timedelta(days=1)
        return a


def windowed_first_activity(domain: str, probe, today: date, seed: date | None = None,
                            floor: date | None = None, exact_first=False, max_calls: int = 64) -> Boundary:
    """fbo_postings (exact_first=True: проба отдаёт первую дату) и ads_expense_daily (двоичный поиск)."""
    b = Boundary(domain, api_documented_from=DOCUMENTED_FLOOR.get(domain))
    q = _Search(b, probe, max_calls)
    lo = floor or DOCUMENTED_FLOOR.get(domain) or SEARCH_FLOOR
    if seed:
        b.evidence.append({"operator_seed": str(seed), "note": "подсказка не сокращает поиск"})
    hit = None
    for s, e in _windows(lo, today, WINDOW_DAYS[domain]):
        r = q.call(s, e)
        if r.status == REJECTED:
            part = q.earliest_accepted(s, e)
            if part is None:
                continue
            s, r = part
        if r.status == DATA:
            hit = (s, e, r)
            break
    if not q.accepted:
        raise HistoryProbeError(f"{domain}: API отверг все окна — граница не определена")
    if hit is None:
        if q.rejected:
            b.completeness_status, b.confidence = "PARTIAL", "EMPTY_AFTER_RETENTION"
        else:
            b.completeness_status, b.confidence = "NOT_APPLICABLE", "VERIFIED_EMPTY"
        return b
    s, e, r = hit
    first = None
    if exact_first and r.first_date and s <= r.first_date <= e:
        if r.first_date == s or q.call(s, r.first_date - timedelta(days=1)).status == EMPTY:
            first = r.first_date                               # сортировка подтверждена пробой
        else:
            b.evidence.append({"note": "первая дата ответа не самая ранняя — двоичный поиск"})
    b.first_observed_activity = first or q.first_day(s, e)
    b.confidence = "VERIFIED"
    b.completeness_status = "PARTIAL" if q.rejected else "COMPLETE"
    return b


def sampled_first_activity(domain: str, probe_day, today: date, anchor: date | None = None,
                           max_calls: int = 400) -> Boundary:
    """finance_accrual: сутки только по одной; выборка 5/15/25 от границы до СЕГОДНЯ + посуточно.

    anchor (первая активность FBO/рекламы) границу не ограничивает (ревью PR #226: начисления идут
    после доставки, поиск до якоря давал «истории нет»); он учитывается в плане (lifecycle.build_plan):
    бэкфилл начислений начинается не позже самой ранней активности любого домена.
    """
    floor = DOCUMENTED_FLOOR[domain]
    b = Boundary(domain, api_documented_from=floor)
    q = _Search(b, lambda s, e: probe_day(s), max_calls)
    if anchor:
        b.evidence.append({"anchor": str(anchor)})
    first_month = None
    m = date(floor.year, floor.month, 1)
    while m <= today and first_month is None:
        for d in SAMPLE_DAYS:
            day = m.replace(day=d)
            if floor <= day <= today and q.call(day, day).status == DATA:
                first_month = m
                break
        m = (m.replace(day=28) + timedelta(days=4)).replace(day=1)
    if not q.accepted:
        raise HistoryProbeError(f"{domain}: API отверг все сутки выборки — граница не определена")
    if first_month is None:
        b.completeness_status, b.confidence = ("PARTIAL", "SAMPLED_EMPTY_AFTER_RETENTION") if q.rejected \
            else ("NOT_APPLICABLE", "SAMPLED_EMPTY")
        return b
    prev = (first_month - timedelta(days=1)).replace(day=1)
    month_end = (first_month.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    d = max(prev, floor)
    while d <= min(month_end, today):                     # посуточно M−1 и M: ≤ 62 вызова
        if q.call(d, d).status == DATA:
            b.first_observed_activity = d
            break
        d += timedelta(days=1)
    b.confidence = "SAMPLED"
    if b.first_observed_activity is None:
        b.completeness_status = "UNKNOWN"                  # выборка видела данные, посуточно — нет
    elif b.first_observed_activity == floor:
        b.completeness_status, b.limitation_reason = "PARTIAL", "API_DOCUMENTED_FLOOR"
    else:
        b.completeness_status = "PARTIAL" if q.rejected else "COMPLETE"
    return b
