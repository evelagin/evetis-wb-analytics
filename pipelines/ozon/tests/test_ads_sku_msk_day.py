"""D2 (2026-10-11): отчёт Performance по SKU на пути EVETIS запрашивает сутки МСК целиком.

Всё синтетическое: кампании, SKU и суммы выдуманы, Ozon и BigQuery не вызываются.

Фальшивый Performance API ниже хранит расход по ЧАСАМ МСК и агрегирует «День» отчёта по
московским суткам, как Ozon (замер D2: dateFrom/dateTo и моменты [d0−1 21:00Z, d1 21:00Z) дают
одинаковые полные сутки, моменты d0T00:00Z..d1T00:00Z — без ночи 00:00–02:59 МСК суток d0 и
только эту ночь суток d1). Поэтому тесты различают «полные сутки» и прежний сдвиг на 3 часа.
"""
from __future__ import annotations

import io
import json
import zipfile
from datetime import date, datetime, timedelta, timezone

import pytest

import common as C

MSK = timezone(timedelta(hours=3))


@pytest.fixture
def lenient(monkeypatch):
    monkeypatch.setattr(C, "STRICT_PAGE_CAPS", False)


def _zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return buf.getvalue().decode("utf-8", "surrogateescape")


def _ru(v):
    return f"{v:.2f}".replace(".", ",")


class HourlyPerf:
    """Расход кампания × SKU × час МСК; отчёт и расход агрегируются по суткам МСК."""

    def __init__(self, campaigns, d0, d1, *, skus=("9000001", "9000002"), extra_day=None):
        self.campaigns = [str(c) for c in campaigns]
        self.skus = skus
        self.d0, self.d1 = d0, d1
        self.extra_day = extra_day              # строка вне окна — нарушение контракта
        self.submits, self.expense_windows, self.last = [], [], None

    def hourly(self, cid, sku, day, hour):
        # ночь 00–02 МСК не нулевая: её потеря видна в каждой паре
        base = (int(cid) % 7 + 1) * (2 if sku.endswith("1") else 1)
        return round(base * (1.5 if hour < 3 else 1.0) + day.day / 100, 2)

    def _hours(self, body):
        """Часы МСК, покрытые запросом: dateFrom/dateTo — сутки МСК; from/to — моменты UTC."""
        if "dateFrom" in body:
            a = datetime.fromisoformat(body["dateFrom"]).replace(tzinfo=MSK)
            b = datetime.fromisoformat(body["dateTo"]).replace(tzinfo=MSK) + timedelta(days=1)
        else:
            a = datetime.fromisoformat(body["from"].replace("Z", "+00:00"))
            b = datetime.fromisoformat(body["to"].replace("Z", "+00:00"))
        t = a.astimezone(MSK)
        while t < b:
            if self.d0 <= t.date() <= self.d1:
                yield t.date(), t.hour
            t += timedelta(hours=1)

    def day_total(self, cid, day):
        return round(sum(self.hourly(cid, s, day, h) for s in self.skus for h in range(24)), 2)

    def aggregate(self, body):
        out = {}
        for day, hour in self._hours(body):
            for cid in body["campaigns"]:
                for sku in self.skus:
                    k = (str(day), cid, sku)
                    out[k] = round(out.get(k, 0.0) + self.hourly(cid, sku, day, hour), 2)
        return out

    def _csv(self, cid, agg):
        lines = [f"﻿;Кампания по продвижению товаров № {cid}, период синтетический",
                 "День;sku;Название товара;Показы;Клики;Добавления в корзину;Расход, ₽, с НДС;"
                 "Продано товаров;Продажи в продвижении, ₽;Заказано на сумму, ₽;ДРР в продвижении, %;"
                 "ДРР (общий), %"]
        for (d, c, sku), v in sorted(agg.items()):
            if c == cid:
                dd = f"{d[8:10]}.{d[5:7]}.{d[0:4]}"
                lines.append(f"{dd};{sku};Синтетика;10;2;1;{_ru(v)};1;100,00;-;5,0;-")
        if self.extra_day:
            dd = f"{self.extra_day[8:10]}.{self.extra_day[5:7]}.{self.extra_day[0:4]}"
            lines.append(f"{dd};{self.skus[0]};Синтетика;1;1;0;1,00;0;0,00;-;-;-")
        return "\n".join(lines) + "\n"

    def get(self, path, raw_text=True):
        if path == "/api/client/campaign":
            lst = [{"id": c, "advObjectType": "SKU"} for c in self.campaigns]
            return 200, json.dumps({"list": lst, "total": str(len(lst))})
        if path.startswith("/api/client/statistics/expense"):
            q = dict(x.split("=") for x in path.split("?")[1].split("&"))
            self.expense_windows.append((q["dateFrom"], q["dateTo"]))
            return 200, "ID;Название;Расход\n" + "".join(f"{c};К;{_ru(10)}\n" for c in self.campaigns)
        if path.startswith("/api/client/statistics/report"):
            agg = self.aggregate(self.last)
            batch = self.last["campaigns"]
            if len(batch) == 1:                         # одна кампания — text/csv (P2-6)
                return 200, self._csv(batch[0], agg)
            return 200, _zip({f"{c}_синтетика.csv": self._csv(c, agg) for c in batch})
        if path.startswith("/api/client/statistics/"):
            return 200, {"state": "OK"}
        raise AssertionError(path)

    def post(self, path, body):
        assert path == "/api/client/statistics"
        self.submits.append(body)
        self.last = body
        return 200, {"UUID": f"u{len(self.submits)}"}


def _use(monkeypatch, entities, fake):
    monkeypatch.setattr(entities, "perf_get", fake.get)
    monkeypatch.setattr(entities, "perf_post", fake.post)


def _by_key(rows):
    return {(r["date"], r["campaign_id"], r["sku"]): r for r in rows}


def _day_sums(rows):
    out = {}
    for r in rows:
        k = (r["date"], r["campaign_id"])
        out[k] = round(out.get(k, 0.0) + r["attributed_spend_rub"], 2)
    return out


# ───────────────────────────────────────────── каноническая функция окна
@pytest.mark.parametrize("d0,d1", [
    ("2026-10-11", "2026-10-11"),          # одни сутки
    ("2026-01-31", "2026-02-01"),          # стык месяцев
    ("2025-12-31", "2026-01-01"),          # стык лет
    ("2028-02-28", "2028-03-01"),          # високосный февраль
    ("2026-03-28", "2026-03-30"),          # бывшая смена времени: у МСК её нет с 2014
    ("2026-08-03", "2026-10-01"),          # ровно 60 суток
])
def test_window_is_the_moscow_dates_themselves(entities, d0, d1):
    a, b = date.fromisoformat(d0), date.fromisoformat(d1)
    assert entities.msk_day_report_window(a, b) == {"dateFrom": d0, "dateTo": d1}


def test_window_never_sends_utc_instants(entities):
    w = entities.msk_day_report_window(date(2026, 9, 1), date(2026, 9, 7))
    assert set(w) == {"dateFrom", "dateTo"} and "T" not in w["dateFrom"] + w["dateTo"]


@pytest.mark.parametrize("d0,d1,msg", [
    ("2026-09-02", "2026-09-01", "позже конца"),
    ("2026-08-03", "2026-10-02", "длиннее 60 суток"),          # 61 сутки
])
def test_window_rejects_inverted_or_too_long(entities, d0, d1, msg):
    with pytest.raises(ValueError, match=msg):
        entities.msk_day_report_window(date.fromisoformat(d0), date.fromisoformat(d1))


def test_moscow_has_constant_plus_three_offset():
    """Сутки МСК = [d−1 21:00Z, d 21:00Z) каждый день 2025–2027: перехода на летнее время нет."""
    zoneinfo = pytest.importorskip("zoneinfo")
    try:
        tz = zoneinfo.ZoneInfo("Europe/Moscow")
    except zoneinfo.ZoneInfoNotFoundError:
        pytest.skip("нет базы часовых поясов")
    d = date(2025, 1, 1)
    while d <= date(2027, 12, 31):
        start = datetime(d.year, d.month, d.day, tzinfo=tz)
        assert start.utcoffset() == timedelta(hours=3), d
        assert start.astimezone(timezone.utc) == datetime(d.year, d.month, d.day, tzinfo=timezone.utc) - \
            timedelta(hours=3)
        d += timedelta(days=1)


@pytest.mark.parametrize("d0,d1,n", [
    ("2026-10-04", "2026-10-11", 1),          # суточный прогон: окно 7 суток назад + сегодня
    ("2025-12-15", "2026-03-15", 2),          # через стык лет и февраль
    ("2025-05-05", "2026-10-10", 9),          # вся история CPC EVETIS
    ("2026-01-01", "2026-01-01", 1),
])
def test_windows_partition_days_without_gaps_or_overlaps(entities, d0, d1, n):
    a, b = date.fromisoformat(d0), date.fromisoformat(d1)
    ws = entities.msk_day_report_windows(a, b)
    assert len(ws) == n
    seen = []
    for c0, c1, body in ws:
        assert body == entities.msk_day_report_window(c0, c1)
        assert 1 <= (c1 - c0).days + 1 <= entities.ADS_SKU_STRICT_CHUNK_DAYS
        seen += [c0 + timedelta(days=i) for i in range((c1 - c0).days + 1)]
    assert seen == [a + timedelta(days=i) for i in range((b - a).days + 1)], "разрыв или пересечение"
    for (_, e, _), (s, _, _) in zip(ws, ws[1:]):
        assert s == e + timedelta(days=1)


def test_inverted_range_has_no_windows(entities):
    assert entities.msk_day_report_windows(date(2026, 9, 2), date(2026, 9, 1)) == []


# ───────────────────────────────────────────── путь EVETIS (без STRICT_PAGE_CAPS)
def test_daily_run_loads_complete_moscow_days(entities, captured_merges, monkeypatch, lenient):
    fake = HourlyPerf([101, 102], date(2026, 9, 1), date(2026, 10, 31))
    _use(monkeypatch, entities, fake)
    entities.ads_sku_daily("rt-d", "ts", "2026-10-04", "2026-10-11")
    assert fake.submits == [{"campaigns": ["101", "102"], "dateFrom": "2026-10-04",
                             "dateTo": "2026-10-11", "groupBy": "DATE"}]
    assert fake.expense_windows == [("2026-10-04", "2026-10-11")]
    (table, rows, keys, _kw), = captured_merges
    assert table == "RAW_OZON_ADS_SKU_DAILY" and keys == ["date", "campaign_id", "sku"]
    sums = _day_sums(rows)
    for k in range(8):
        d = date(2026, 10, 4) + timedelta(days=k)
        for cid in ("101", "102"):
            assert sums[(str(d), cid)] == fake.day_total(cid, d), (d, cid)


def test_fake_source_distinguishes_the_old_utc_window(entities):
    """Контроль фальшивки: прежний запрос (моменты UTC) теряет ночь d0 и берёт только ночь d1."""
    fake = HourlyPerf([101], date(2026, 9, 1), date(2026, 10, 31))
    old = fake.aggregate({"campaigns": ["101"], "from": "2026-10-04T00:00:00Z", "to": "2026-10-11T00:00:00Z"})
    new = fake.aggregate({"campaigns": ["101"], **entities.msk_day_report_window(date(2026, 10, 4),
                                                                                 date(2026, 10, 11))})
    msk = fake.aggregate({"campaigns": ["101"], "from": "2026-10-03T21:00:00Z", "to": "2026-10-11T21:00:00Z"})
    assert new == msk
    lost = round(sum(new.values()) - sum(old.values()), 2)
    d0_lost = round(sum(v for k, v in new.items() if k[0] == "2026-10-04")
                    - sum(v for k, v in old.items() if k[0] == "2026-10-04"), 2)
    assert d0_lost > 0 and lost > 0
    assert sum(v for k, v in old.items() if k[0] == "2026-10-11") < \
        sum(v for k, v in new.items() if k[0] == "2026-10-11")


def test_consecutive_daily_runs_leave_every_closed_day_complete(entities, captured_merges, monkeypatch,
                                                                 lenient):
    """Перекрытие 7 суток: сутки, ставшие d0, больше не теряют ночь (MERGE — последний прогон)."""
    fake = HourlyPerf([101, 102, 103], date(2026, 8, 1), date(2026, 10, 31))
    _use(monkeypatch, entities, fake)
    table = {}
    for k in range(10):
        today = date(2026, 9, 20) + timedelta(days=k)
        entities.ads_sku_daily(f"rt-{k}", "ts", str(today - timedelta(days=7)), str(today))
        table.update(_by_key(captured_merges[-1][1]))
    sums = _day_sums(table.values())
    for d in (date(2026, 9, 13) + timedelta(days=i) for i in range(17)):
        for cid in ("101", "102", "103"):
            assert sums[(str(d), cid)] == fake.day_total(cid, d), (d, cid)


def test_backfill_window_is_chunked_and_stitched(entities, captured_merges, monkeypatch, lenient):
    fake = HourlyPerf([101, 102], date(2025, 12, 1), date(2026, 3, 31))
    _use(monkeypatch, entities, fake)
    entities.ads_sku_daily("rt-bf", "ts", "2025-12-15", "2026-03-15")
    assert [(b["dateFrom"], b["dateTo"]) for b in fake.submits] == [
        ("2025-12-15", "2026-02-12"), ("2026-02-13", "2026-03-15")]
    assert fake.expense_windows == [("2025-12-15", "2026-02-12"), ("2026-02-13", "2026-03-15")]
    rows = captured_merges[-1][1]
    keys = [(r["date"], r["campaign_id"], r["sku"]) for r in rows]
    assert len(keys) == len(set(keys)) == 91 * 2 * 2, "сутки × кампании × SKU, каждая пара один раз"
    sums = _day_sums(rows)
    for edge in ("2025-12-31", "2026-01-01", "2026-02-12", "2026-02-13", "2026-03-15"):
        for cid in ("101", "102"):
            assert sums[(edge, cid)] == fake.day_total(cid, date.fromisoformat(edge))


def test_repeat_run_is_idempotent(entities, captured_merges, monkeypatch, lenient):
    fake = HourlyPerf([101, 102], date(2026, 9, 1), date(2026, 10, 31))
    _use(monkeypatch, entities, fake)
    entities.ads_sku_daily("rt-1", "ts-1", "2026-10-01", "2026-10-08")
    entities.ads_sku_daily("rt-2", "ts-2", "2026-10-01", "2026-10-08")
    drop = ("ingestion_run_id", "extracted_at")
    first, second = ([{k: v for k, v in r.items() if k not in drop} for r in m[1]] for m in captured_merges)
    assert first == second
    assert len({r["source_payload_hash"] for r in first}) == len(first)
    assert all(r["source_payload_hash"] == entities.h(r["date"], r["campaign_id"], r["sku"]) for r in first)


def test_single_campaign_batch_csv_is_loaded_like_zip(entities, captured_merges, monkeypatch, lenient):
    """11 кампаний → партии 10 (ZIP) + 1 (CSV): P2-6, ни одна кампания не теряется."""
    ids = [str(200 + i) for i in range(11)]
    fake = HourlyPerf(ids, date(2026, 9, 1), date(2026, 9, 30), skus=("9000001",))
    _use(monkeypatch, entities, fake)
    entities.ads_sku_daily("rt", "ts", "2026-09-10", "2026-09-11")
    assert [len(b["campaigns"]) for b in fake.submits] == [10, 1]
    sums = _day_sums(captured_merges[-1][1])
    assert {cid for _d, cid in sums} == set(ids)
    assert sums[("2026-09-11", "210")] == fake.day_total("210", date(2026, 9, 11))


def test_multi_campaign_non_zip_is_still_skipped_without_flag(entities, captured_merges, monkeypatch,
                                                              lenient):
    fake = HourlyPerf([101, 102], date(2026, 9, 1), date(2026, 9, 30))
    monkeypatch.setattr(fake, "get", lambda path, raw_text=True: (200, "не архив") if path.startswith(
        "/api/client/statistics/report") else HourlyPerf.get(fake, path, raw_text))
    _use(monkeypatch, entities, fake)
    entities.ads_sku_daily("rt", "ts", "2026-09-10", "2026-09-11")
    assert captured_merges[-1][1] == []


def test_row_outside_requested_moscow_days_fails_closed(entities, captured_merges, monkeypatch, lenient):
    fake = HourlyPerf([101, 102], date(2026, 9, 1), date(2026, 9, 30), extra_day="2026-09-12")
    _use(monkeypatch, entities, fake)
    with pytest.raises(RuntimeError, match="вне окна 2026-09-10..2026-09-11"):
        entities.ads_sku_daily("rt", "ts", "2026-09-10", "2026-09-11")
    assert captured_merges == []
