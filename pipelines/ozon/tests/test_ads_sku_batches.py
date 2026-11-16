"""P2-6 (16.11.2026): партии асинхронного отчёта SKU не теряются молча.

16.11 партия [CPC «крем руки» + CPO «все товары»] пришла не ZIP-архивом и пропала:
у крема для рук за 15.11 нет строк SKU (250,23 ₽), РНП и Юнитка Ozon показали ноль.
"""
import io
import json
import zipfile

REPORT_CSV = "; Кампания 111\nДень;sku;Расход, ₽, с НДС;Показы;Клики\n01.09.2026;500;1,00;1;1\n"


def _zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return buf.getvalue().decode("utf-8", "surrogateescape")


def _perf(monkeypatch, entities, campaigns, expense, report, posted):
    def perf_get(path, raw_text=True):
        if path == "/api/client/campaign":
            return 200, json.dumps({"list": campaigns, "total": str(len(campaigns))})
        if path.startswith("/api/client/statistics/expense"):
            return 200, expense
        if path.startswith("/api/client/statistics/report"):
            return 200, report
        if path.startswith("/api/client/statistics/"):
            return 200, {"state": "OK"}
        raise AssertionError(path)

    def perf_post(path, body):
        posted.append(list(body["campaigns"]))
        return 200, {"UUID": "u-1"}

    monkeypatch.setattr(entities, "perf_get", perf_get)
    monkeypatch.setattr(entities, "perf_post", perf_post)


def test_zip_report_is_unchanged(entities, captured_merges, monkeypatch):
    posted = []
    _perf(monkeypatch, entities, [{"id": "111", "advObjectType": "SKU"}],
          "ID;Название;Расход\n111;A;10,00\n", _zip({"111_report.csv": REPORT_CSV}), posted)
    entities.ads_sku_daily("rt-x", "ts", "2026-09-01", "2026-09-02")
    rows = captured_merges[-1][1]
    assert [(r["campaign_id"], r["sku"], r["date"]) for r in rows] == [("111", "500", "2026-09-01")]


def test_single_campaign_csv_is_parsed(entities, captured_merges, monkeypatch):
    posted = []
    _perf(monkeypatch, entities, [{"id": "111", "advObjectType": "SKU"}],
          "ID;Название;Расход\n111;A;10,00\n", REPORT_CSV, posted)
    entities.ads_sku_daily("rt-x", "ts", "2026-09-01", "2026-09-02")
    rows = captured_merges[-1][1]
    assert [(r["campaign_id"], r["sku"], r["attributed_spend_rub"]) for r in rows] == [("111", "500", 1.0)]


def test_cpo_and_banners_are_not_batched(entities, captured_merges, monkeypatch):
    posted = []
    campaigns = [{"id": "111", "advObjectType": "SKU"}, {"id": "222", "advObjectType": "ALL_SKU_PROMO"},
                 {"id": "333", "advObjectType": "SEARCH_PROMO"}, {"id": "444", "advObjectType": "BANNER"}]
    expense = "ID;Название;Расход\n111;CPC;10,00\n222;CPO;5,00\n333;CPO2;1,00\n444;Баннер;2,00\n"
    _perf(monkeypatch, entities, campaigns, expense, REPORT_CSV, posted)
    entities.ads_sku_daily("rt-x", "ts", "2026-09-01", "2026-09-02")
    assert posted == [["111"]]
    assert [r["campaign_id"] for r in captured_merges[-1][1]] == ["111"]


def test_eleven_cpc_campaigns_last_single_batch_is_loaded(entities, captured_merges, monkeypatch):
    """N mod 10 = 1: последняя партия из одной кампании раньше пропадала."""
    posted = []
    ids = [str(100 + i) for i in range(11)]
    campaigns = [{"id": i, "advObjectType": "SKU"} for i in ids]
    expense = "ID;Название;Расход\n" + "".join(f"{i};X;1,00\n" for i in ids)
    zip_first = _zip({f"{i}_r.csv": REPORT_CSV.replace("111", i) for i in ids[:10]})
    reports = iter([zip_first, REPORT_CSV])

    def perf_get(path, raw_text=True):
        if path == "/api/client/campaign":
            return 200, json.dumps({"list": campaigns, "total": "11"})
        if path.startswith("/api/client/statistics/expense"):
            return 200, expense
        if path.startswith("/api/client/statistics/report"):
            return 200, next(reports)
        return 200, {"state": "OK"}

    monkeypatch.setattr(entities, "perf_get", perf_get)
    monkeypatch.setattr(entities, "perf_post", lambda p, b: (posted.append(b["campaigns"]), (200, {"UUID": "u"}))[1])
    entities.ads_sku_daily("rt-x", "ts", "2026-09-01", "2026-09-02")
    assert [len(b) for b in posted] == [10, 1]
    assert sorted({r["campaign_id"] for r in captured_merges[-1][1]}) == sorted(ids)


def test_multi_campaign_non_zip_is_logged_and_skipped(entities, captured_merges, monkeypatch):
    posted, events = [], []
    monkeypatch.setattr(entities, "log", lambda **kw: events.append(kw))
    _perf(monkeypatch, entities, [{"id": "111", "advObjectType": "SKU"}, {"id": "112", "advObjectType": "SKU"}],
          "ID;Название;Расход\n111;A;10,00\n112;B;3,00\n", REPORT_CSV, posted)
    entities.ads_sku_daily("rt-x", "ts", "2026-09-01", "2026-09-02")
    assert captured_merges[-1][1] == []
    assert any(e.get("event") == "ads_sku_batch_not_zip" for e in events)


def test_zip_file_named_only_by_campaign_id(entities, captured_merges, monkeypatch):
    posted = []
    _perf(monkeypatch, entities, [{"id": "111", "advObjectType": "SKU"}, {"id": "112", "advObjectType": "SKU"}],
          "ID;Название;Расход\n111;A;10,00\n112;B;3,00\n",
          _zip({"111.csv": REPORT_CSV, "112.csv": REPORT_CSV.replace("111", "112")}), posted)
    entities.ads_sku_daily("rt-x", "ts", "2026-09-01", "2026-09-02")
    assert sorted(r["campaign_id"] for r in captured_merges[-1][1]) == ["111", "112"]
