"""Прежний путь EVETIS для ads_sku_daily: партии кампаний и CSV одной кампании (16.11.2026).

Инцидент 16.11.2026: расход за окно был у 12 кампаний (11 «оплата за клик» и «оплата за
заказ — все товары»). Партии по 10: вторая — «крем руки» + «оплата за заказ». Ответ пришёл
не ZIP-архивом, партия молча пропала, расход крема для рук за 15.11 (250,23 ₽) не попал в
RAW_OZON_ADS_SKU_DAILY при статусе прогона OK. TECH_DEBT P2-6.
"""
from __future__ import annotations

import io
import json
import zipfile

import pytest

import common as C


@pytest.fixture(autouse=True)
def _lenient(entities, monkeypatch):
    monkeypatch.setattr(C, "STRICT_PAGE_CAPS", False)
    monkeypatch.setattr(entities.time, "sleep", lambda *_a: None)


def _csv(cid, sku="500"):
    return f"; Кампания {cid}\nДень;sku;Расход, ₽, с НДС;Показы;Клики\n15.11.2026;{sku};250,23;2244;34\n"


def _zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return buf.getvalue().decode("utf-8", "surrogateescape")


class LegacyPerf:
    """Performance API для прежнего пути: расход за всё окно, отчёт моментами from/to.

    Как Ozon: одна кампания в запросе — CSV, несколько — ZIP. Кампании «оплата за заказ»
    отчёта по SKU не дают: при одной CPC-кампании в партии ответ — её CSV без архива.
    """

    def __init__(self, cpc, cpo=()):
        self.cpc, self.cpo = [str(c) for c in cpc], [str(c) for c in cpo]
        self.submits, self.last = [], None

    def get(self, path, raw_text=True):
        if path == "/api/client/campaign":
            lst = ([{"id": c, "advObjectType": "SKU"} for c in self.cpc]
                   + [{"id": c, "advObjectType": "SEARCH_PROMO"} for c in self.cpo])
            return 200, json.dumps({"list": lst, "total": str(len(lst))})
        if path.startswith("/api/client/statistics/expense"):
            return 200, "ID;Название;Расход\n" + "".join(f"{c};К;10,00\n" for c in self.cpc + self.cpo)
        if path.startswith("/api/client/statistics/report"):
            with_rows = [c for c in self.last["campaigns"] if c in self.cpc]
            if len(with_rows) == 1:
                return 200, _csv(with_rows[0])
            return 200, _zip({f"{c}.csv": _csv(c) for c in with_rows})
        if path.startswith("/api/client/statistics/"):
            return 200, {"state": "OK"}
        raise AssertionError(path)

    def post(self, path, body):
        assert path == "/api/client/statistics"
        assert "from" in body and "dateFrom" not in body, "прежний путь — моменты from/to"
        self.submits.append(body)
        self.last = body
        return 200, {"UUID": f"u{len(self.submits)}"}


def _run(entities, monkeypatch, captured_merges, fake):
    monkeypatch.setattr(entities, "perf_get", fake.get)
    monkeypatch.setattr(entities, "perf_post", fake.post)
    entities.ads_sku_daily("rt", "ts", "2026-11-09", "2026-11-16")
    (_table, rows, _k, _kw), = captured_merges
    return rows


def test_incident_16_11_hand_cream_is_loaded(entities, captured_merges, monkeypatch):
    """11 CPC + «оплата за заказ»: крем для рук больше не теряется."""
    cpc = [49321276, 33678288, 33678220, 33678192, 32158176, 32158051, 32158008,
           32157988, 32157965, 32157947, 32157891]
    fake = LegacyPerf(cpc, cpo=[24086250])
    rows = _run(entities, monkeypatch, captured_merges, fake)
    assert {r["campaign_id"] for r in rows} == {str(c) for c in cpc}
    hand = [r for r in rows if r["campaign_id"] == "32157891"]
    assert hand and hand[0]["date"] == "2026-11-15" and float(hand[0]["attributed_spend_rub"]) == 250.23
    assert all("24086250" not in b["campaigns"] for b in fake.submits), "оплату за заказ не заказываем"


def test_single_campaign_batch_csv_is_loaded(entities, captured_merges, monkeypatch):
    """11 CPC-кампаний — партии 10 + 1; одиночная приходит CSV и загружается."""
    cpc = list(range(1000, 1011))
    fake = LegacyPerf(cpc)
    rows = _run(entities, monkeypatch, captured_merges, fake)
    assert [len(b["campaigns"]) for b in fake.submits] == [10, 1]
    assert len(rows) == 11 and {r["campaign_id"] for r in rows} == {str(c) for c in cpc}


def test_only_pay_per_order_campaigns_make_no_report_request(entities, captured_merges, monkeypatch):
    fake = LegacyPerf([], cpo=[24086250])
    monkeypatch.setattr(entities, "perf_get", fake.get)
    monkeypatch.setattr(entities, "perf_post", fake.post)
    entities.ads_sku_daily("rt", "ts", "2026-11-09", "2026-11-16")
    assert fake.submits == []


def test_multi_campaign_non_zip_is_logged_not_silent(entities, captured_merges, monkeypatch):
    fake = LegacyPerf([1, 2])
    fake_get = fake.get

    def broken(path, raw_text=True):
        if path.startswith("/api/client/statistics/report"):
            return 200, "не архив"
        return fake_get(path, raw_text)
    events = []
    monkeypatch.setattr(entities, "perf_get", broken)
    monkeypatch.setattr(entities, "perf_post", fake.post)
    monkeypatch.setattr(entities, "log", lambda **kw: events.append(kw))
    entities.ads_sku_daily("rt", "ts", "2026-11-09", "2026-11-16")
    assert any(e.get("event") == "ads_sku_batch_not_zip" for e in events)
