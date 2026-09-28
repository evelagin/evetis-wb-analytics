"""Tenancy T5-G: блокеры runtime до ворот учётных данных.

Для каждого места: без флага STRICT_PAGE_CAPS поведение EVETIS прежнее (или прежний отказ
стал успехом на тех же данных — пагинация и партии), с флагом — закрытый отказ вместо
молчаливого успеха. Лимиты — Swagger Seller/Performance API от 2026-09-28.
"""
from __future__ import annotations

import io
import json
import random
import zipfile
from datetime import date, timedelta

import pytest

import common as C


@pytest.fixture
def strict(monkeypatch):
    monkeypatch.setattr(C, "STRICT_PAGE_CAPS", True)


@pytest.fixture
def lenient(monkeypatch):
    monkeypatch.setattr(C, "STRICT_PAGE_CAPS", False)


@pytest.fixture(autouse=True)
def _no_sleep(entities, monkeypatch):
    monkeypatch.setattr(entities.time, "sleep", lambda *_a: None)


def _products(a, b):
    return [{"product_id": 10_000 + i, "sku": 500_000 + i, "offer_id": f"o{i}"} for i in range(a, b)]


# ─────────────────────────────────────────── каталог > 1000 и total → total_items
def _paged_product_list(pages, total_key="total_items"):
    """pages: список списков товаров; last_id непуст и на последней странице (как у Ozon)."""
    calls = []
    total = sum(len(p) for p in pages)

    def seller_post(path, body):
        if path == "/v3/product/list":
            calls.append(("list", body["last_id"]))
            idx = 0 if not body["last_id"] else int(body["last_id"][1:])
            res = {"items": pages[idx], "last_id": f"p{idx + 1}"}
            if total_key:
                res[total_key] = total
            return 200, {"result": res}
        if path == "/v3/product/info/list":
            calls.append(("info", len(body["product_id"])))
            assert len(body["product_id"]) <= 1000, "лимит /v3/product/info/list — 1000"
            return 200, {"items": [{"id": pid, "sku": 1, "offer_id": "o"} for pid in body["product_id"]]}
        if path == "/v1/analytics/stocks":
            calls.append(("stocks", len(body["skus"])))
            assert len(body["skus"]) <= 100, "лимит /v1/analytics/stocks — 100 SKU"
            return 200, {"items": [{"sku": s, "warehouse_id": 1} for s in body["skus"]]}
        raise AssertionError(path)
    return seller_post, calls


@pytest.mark.parametrize("mode", ["lenient", "strict"])
def test_catalog_over_1000_products_is_paged_and_batched(entities, captured_merges, monkeypatch, mode):
    monkeypatch.setattr(C, "STRICT_PAGE_CAPS", mode == "strict")
    sp, calls = _paged_product_list([_products(0, 1000), _products(1000, 1500)])
    monkeypatch.setattr(entities, "seller_post", sp)
    entities.catalog("rt", "ts", None, None)
    assert [c for c in calls if c[0] == "list"] == [("list", ""), ("list", "p1")]
    assert [c for c in calls if c[0] == "info"] == [("info", 1000), ("info", 500)]
    (table, rows, _k, _kw), = captured_merges
    assert table == "RAW_OZON_CATALOG" and len(rows) == 1500


def test_catalog_up_to_1000_keeps_the_single_old_request(entities, captured_merges, monkeypatch, lenient):
    sp, calls = _paged_product_list([_products(0, 20)], total_key="total")
    monkeypatch.setattr(entities, "seller_post", sp)
    entities.catalog("rt", "ts", None, None)
    assert calls == [("list", ""), ("info", 20)]


def test_product_list_stall_fails_closed(entities, monkeypatch):
    """Повтор страницы без новых товаров до total — отказ, а не часть каталога."""
    calls = []

    def sp(path, body):
        calls.append(body["last_id"])
        return 200, {"result": {"items": _products(0, 1000), "total_items": 1200, "last_id": "same"}}
    monkeypatch.setattr(entities, "seller_post", sp)
    with pytest.raises(entities.PaginationError, match="1000 из total=1200"):
        entities._product_list_items()
    assert calls == ["", "same"], "застревание распознаётся на второй странице, а не потолком"


def test_product_list_uses_total_items_after_total_is_removed(entities, monkeypatch):
    sp, _ = _paged_product_list([_products(0, 3)], total_key="total_items")
    monkeypatch.setattr(entities, "seller_post", sp)
    assert len(entities._product_list_items()) == 3


def test_product_list_without_any_total_fails_only_in_strict(entities, monkeypatch):
    sp, _ = _paged_product_list([_products(0, 3)], total_key=None)
    monkeypatch.setattr(entities, "seller_post", sp)
    monkeypatch.setattr(C, "STRICT_PAGE_CAPS", False)
    assert len(entities._product_list_items()) == 3
    monkeypatch.setattr(C, "STRICT_PAGE_CAPS", True)
    with pytest.raises(entities.StrictLimitError, match="total_items/total"):
        entities._product_list_items()


def test_catalog_strict_detects_missing_cards(entities, captured_merges, monkeypatch, strict):
    def sp(path, body):
        if path == "/v3/product/list":
            return 200, {"result": {"items": _products(0, 3), "total_items": 3, "last_id": "x"}}
        return 200, {"items": [{"id": 10_000, "sku": 1, "offer_id": "o"}]}
    monkeypatch.setattr(entities, "seller_post", sp)
    with pytest.raises(entities.StrictLimitError, match="карточек 1 на 3"):
        entities.catalog("rt", "ts", None, None)
    assert captured_merges == []


# ─────────────────────────────────────────── остатки: не больше 100 SKU за запрос
def test_stocks_are_requested_in_batches_of_100(entities, captured_merges, monkeypatch, lenient):
    sp, calls = _paged_product_list([_products(0, 250)])
    monkeypatch.setattr(entities, "seller_post", sp)
    entities.stocks("rt", "ts", None, None)
    assert [c for c in calls if c[0] == "stocks"] == [("stocks", 100), ("stocks", 100), ("stocks", 50)]
    assert len(captured_merges[0][1]) == 250


# ─────────────────────────────────────────── цены: потолок, повтор курсора, total
def _prices(pages, total):
    def sp(path, body):
        idx = 0 if not body["cursor"] else int(body["cursor"][1:])
        items = [{"offer_id": o, "product_id": 1, "price": {}, "commissions": {}} for o in pages[idx]]
        nxt = f"c{idx + 1}" if idx + 1 < len(pages) else ""
        return 200, {"items": items, "cursor": nxt, "total_items": total}
    return sp


def test_prices_repeated_cursor_fails_even_without_flag(entities, monkeypatch, lenient):
    monkeypatch.setattr(entities, "seller_post",
                        lambda p, b: (200, {"items": [{"offer_id": "o", "product_id": 1}], "cursor": "same"}))
    with pytest.raises(entities.PaginationError, match="cursor повторился"):
        entities.prices("rt", "ts", None, None)


def test_prices_strict_requires_total(entities, captured_merges, monkeypatch, strict):
    monkeypatch.setattr(entities, "seller_post", _prices([["a", "b"], ["c"]], total=4))
    with pytest.raises(entities.StrictLimitError, match="получено 3 товаров из total=4"):
        entities.prices("rt", "ts", None, None)
    assert captured_merges == []
    monkeypatch.setattr(entities, "seller_post", _prices([["a", "b"], ["c"]], total=3))
    entities.prices("rt", "ts", None, None)
    assert len(captured_merges[0][1]) == 3


# ─────────────────────────────────────────── FBO: граница суток и год
def _fbo(calls):
    def sp(path, body):
        calls.append(body["filter"])
        return 200, {"postings": [], "has_next": False, "cursor": ""}
    return sp


def test_fbo_window_end_is_inclusive_to_the_millisecond_in_strict(entities, captured_merges, monkeypatch):
    calls = []
    monkeypatch.setattr(entities, "seller_post", _fbo(calls))
    monkeypatch.setattr(C, "STRICT_PAGE_CAPS", False)
    entities.fbo_postings("rt", "ts", "2026-09-01", "2026-09-02")
    monkeypatch.setattr(C, "STRICT_PAGE_CAPS", True)
    entities.fbo_postings("rt", "ts", "2026-09-01", "2026-09-02")
    assert calls[0]["to"] == "2026-09-02T23:59:59.000Z", "EVETIS — прежняя граница"
    assert calls[1]["to"] == "2026-09-02T23:59:59.999Z", "стык соседних окон без секундного разрыва"


def test_fbo_window_longer_than_a_year_fails_before_any_request(entities, monkeypatch, strict):
    calls = []
    monkeypatch.setattr(entities, "seller_post", _fbo(calls))
    with pytest.raises(entities.StrictLimitError, match="366 сут. длиннее 365"):
        entities.fbo_postings("rt", "ts", "2025-01-01", "2026-01-01")
    assert calls == []


# ─────────────────────────────────────────── молчаливые пустоты → отказ
def test_finance_types_failure_fails_only_in_strict(entities, captured_merges, monkeypatch):
    def sp(path, body):
        if path == "/v1/finance/accrual/types":
            return 500, {"_error": "x"}
        return 200, {"accruals": [], "last_id": ""}
    monkeypatch.setattr(entities, "seller_post", sp)
    monkeypatch.setattr(C, "STRICT_PAGE_CAPS", False)
    entities.finance_accrual("rt", "ts", "2026-09-01", "2026-09-01")
    monkeypatch.setattr(C, "STRICT_PAGE_CAPS", True)
    with pytest.raises(entities.StrictLimitError, match="справочник типов"):
        entities.finance_accrual("rt", "ts", "2026-09-01", "2026-09-01")


def test_daily_stats_failure_no_longer_becomes_silent_nulls(entities, captured_merges, monkeypatch):
    def pg(path, raw_text=True):
        if "/expense" in path:
            return 200, "ID;Название;Расход\n111;К;10,00\n"
        return 503, ""
    monkeypatch.setattr(entities, "perf_get", pg)
    monkeypatch.setattr(C, "STRICT_PAGE_CAPS", False)
    entities.ads_expense_daily("rt", "ts", "2026-09-01", "2026-09-01")
    row = captured_merges[-1][1][0]
    assert row["impressions"] is None and row["clicks"] is None, "прежнее поведение EVETIS"
    captured_merges.clear()
    monkeypatch.setattr(C, "STRICT_PAGE_CAPS", True)
    with pytest.raises(entities.StrictLimitError, match="суточная статистика не получена"):
        entities.ads_expense_daily("rt", "ts", "2026-09-01", "2026-09-01")
    assert captured_merges == []


# ─────────────────────────────────────────── реклама по SKU: P2-5 и P2-6
def _csv_for(cid, day, sku="500"):
    d = date.fromisoformat(day)
    return (f"; Кампания {cid}\nДень;sku;Расход, ₽, с НДС;Показы;Клики\n"
            f"{d:%d.%m.%Y};{sku};1,00;1;1\n")


def _zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return buf.getvalue().decode("utf-8", "surrogateescape")


class FakePerf:
    """Performance API: реестр кампаний, расход по окну, асинхронный отчёт по датам окна."""

    def __init__(self, campaigns, spend, *, report=None, registry=None):
        self.campaigns, self.spend = [str(c) for c in campaigns], {str(k): v for k, v in spend.items()}
        self.registry = [str(c) for c in (registry or campaigns)]
        self.report, self.submits, self.expense_windows, self.last = report, [], [], None

    def get(self, path, raw_text=True):
        if path == "/api/client/campaign":
            return 200, json.dumps({"list": [{"id": c} for c in self.registry], "total": str(len(self.registry))})
        if path.startswith("/api/client/statistics/expense"):
            q = dict(x.split("=") for x in path.split("?")[1].split("&"))
            self.expense_windows.append((q["dateFrom"], q["dateTo"]))
            body = "ID;Название;Расход\n" + "".join(f"{c};К;{v:.2f}\n".replace(".", ",")
                                                     for c, v in self.spend.items())
            return 200, body
        if path.startswith("/api/client/statistics/report"):
            if self.report:
                return self.report(self.last)
            batch, day = self.last["campaigns"], self.last["dateFrom"]
            if len(batch) == 1:
                return 200, _csv_for(batch[0], day)
            return 200, _zip({f"{c}.csv": _csv_for(c, day) for c in batch})
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


def test_long_window_is_split_by_moscow_dates_not_moments(entities, captured_merges, monkeypatch, strict):
    fake = FakePerf([111], {111: 5.0})
    _use(monkeypatch, entities, fake)
    entities.ads_sku_daily("rt", "ts", "2026-01-01", "2026-05-10")      # 130 суток
    windows = [(b["dateFrom"], b["dateTo"]) for b in fake.submits]
    assert windows == [("2026-01-01", "2026-03-01"), ("2026-03-02", "2026-04-30"), ("2026-05-01", "2026-05-10")]
    assert all("from" not in b and "to" not in b for b in fake.submits), "моменты UTC не отправляются"
    assert fake.expense_windows == windows, "периметр кампаний — по тем же датам"
    for a, b in windows:
        assert (date.fromisoformat(b) - date.fromisoformat(a)).days + 1 <= entities.ADS_SKU_STRICT_CHUNK_DAYS


def test_single_campaign_csv_is_loaded_in_strict(entities, captured_merges, monkeypatch, strict):
    _use(monkeypatch, entities, FakePerf([111], {111: 5.0}))
    entities.ads_sku_daily("rt", "ts", "2026-09-01", "2026-09-02")
    (table, rows, _k, _kw), = captured_merges
    assert table == "RAW_OZON_ADS_SKU_DAILY" and rows[0]["campaign_id"] == "111"


def test_campaigns_are_reported_in_batches_of_at_most_10(entities, captured_merges, monkeypatch, strict):
    ids = list(range(1000, 1025))
    _use(monkeypatch, entities, fake := FakePerf(ids, {i: 1.0 for i in ids}))
    entities.ads_sku_daily("rt", "ts", "2026-09-01", "2026-09-02")
    assert [len(b["campaigns"]) for b in fake.submits] == [10, 10, 5]
    assert len(captured_merges[0][1]) == 25


def test_campaign_with_spend_but_no_sku_rows_fails_closed(entities, captured_merges, monkeypatch, strict):
    """«Ноль строк при HTTP 200» (окно > 62 дней, иной формат) больше не проходит как OK."""
    empty = lambda body: (200, "; Кампания\nДень;sku;Расход, ₽, с НДС\n")
    _use(monkeypatch, entities, FakePerf([111], {111: 5.0}, report=empty))
    with pytest.raises(entities.StrictLimitError, match="у 1 кампаний с расходом нет строк SKU"):
        entities.ads_sku_daily("rt", "ts", "2026-09-01", "2026-09-02")
    assert captured_merges == []


def test_sku_row_outside_the_window_fails_closed(entities, captured_merges, monkeypatch, strict):
    shifted = lambda body: (200, _csv_for(body["campaigns"][0], "2026-08-31"))
    _use(monkeypatch, entities, FakePerf([111], {111: 5.0}, report=shifted))
    with pytest.raises(entities.StrictLimitError, match="с датой вне окна"):
        entities.ads_sku_daily("rt", "ts", "2026-09-01", "2026-09-02")


def test_zip_file_of_a_foreign_campaign_fails_closed(entities, monkeypatch, strict):
    foreign = lambda body: (200, _zip({"999.csv": _csv_for(999, body["dateFrom"]),
                                       f"{body['campaigns'][0]}.csv": _csv_for(body["campaigns"][0], body["dateFrom"])}))
    _use(monkeypatch, entities, FakePerf([111, 222], {111: 1.0, 222: 1.0}, report=foreign))
    with pytest.raises(entities.StrictLimitError, match="не относится к кампании партии"):
        entities.ads_sku_daily("rt", "ts", "2026-09-01", "2026-09-02")


def test_multi_campaign_report_that_is_not_zip_fails_closed(entities, monkeypatch, strict):
    plain = lambda body: (200, _csv_for(body["campaigns"][0], body["dateFrom"]))
    _use(monkeypatch, entities, FakePerf([111, 222], {111: 1.0, 222: 1.0}, report=plain))
    with pytest.raises(entities.StrictLimitError, match="не ZIP-архивом"):
        entities.ads_sku_daily("rt", "ts", "2026-09-01", "2026-09-02")


def test_spend_on_campaign_outside_registry_fails_closed(entities, monkeypatch, strict):
    _use(monkeypatch, entities, FakePerf([111], {111: 1.0, 777: 3.0}, registry=[111]))
    with pytest.raises(entities.StrictLimitError, match="вне реестра кампаний"):
        entities.ads_sku_daily("rt", "ts", "2026-09-01", "2026-09-02")


def test_evetis_zip_names_with_suffix_still_map_to_campaign(entities, captured_merges, monkeypatch, strict):
    evetis_style = lambda body: (200, _zip({f"{c}_report.csv": _csv_for(c, body["dateFrom"])
                                            for c in body["campaigns"]}))
    _use(monkeypatch, entities, FakePerf([111, 222], {111: 1.0, 222: 1.0}, report=evetis_style))
    entities.ads_sku_daily("rt", "ts", "2026-09-01", "2026-09-02")
    assert {r["campaign_id"] for r in captured_merges[0][1]} == {"111", "222"}


def test_date_chunks_cover_every_day_exactly_once():
    rnd = random.Random(20260928)
    import entities as E
    for _ in range(200):
        d0 = date(2022, 1, 1) + timedelta(days=rnd.randint(0, 1500))
        d1 = d0 + timedelta(days=rnd.randint(0, 800))
        n = rnd.choice([1, 7, 60, 62])
        days = []
        for a, b in E._date_chunks(d0, d1, n):
            assert a <= b and (b - a).days + 1 <= n
            days += [a + timedelta(days=i) for i in range((b - a).days + 1)]
        assert days == [d0 + timedelta(days=i) for i in range((d1 - d0).days + 1)]


def test_strict_ads_messages_carry_no_credentials(entities, monkeypatch, strict):
    C.register_secret("sentinel-perf-secret-value-0042", "performance_client_secret")
    leak = lambda body: (200, "sentinel-perf-secret-value-0042")
    _use(monkeypatch, entities, FakePerf([111, 222], {111: 1.0, 222: 1.0}, report=leak))
    with pytest.raises(entities.StrictLimitError) as e:
        entities.ads_sku_daily("rt", "ts", "2026-09-01", "2026-09-02")
    assert "sentinel-perf-secret-value-0042" not in str(e.value)


# ─────────────────────────────────────────── окно прогона SINCE/UNTIL
@pytest.mark.parametrize("since,until,expect", [
    ("2026-09-10", "2026-09-01", "SINCE позже UNTIL"),
    ("2026-9-1", None, "SINCE не дата"),
    ("2026-09-01T00:00", None, "SINCE не дата"),
    (None, "2026-09-29", "UNTIL позже сегодняшних суток"),
    ("2026-09-01", "2026-09-28", None),
    (None, None, None),
])
def test_validate_window(since, until, expect):
    import main as M
    got = M.validate_window(since, until, date(2026, 9, 28))
    assert (got is None and expect is None) or (expect and expect in got)


def test_strict_run_with_inverted_window_makes_no_request(monkeypatch, strict):
    import main as M
    calls = []
    monkeypatch.setattr(M, "REGISTRY", {"fbo_postings": (lambda *a: calls.append(a) or {}, 30, "d")})
    monkeypatch.setattr(M.C, "now_msk", lambda: __import__("datetime").datetime(2026, 9, 28, 12, tzinfo=C.MSK))
    monkeypatch.setenv("SINCE", "2026-09-10")
    monkeypatch.setenv("UNTIL", "2026-09-01")
    with pytest.raises(SystemExit) as e:
        M.main()
    assert e.value.code == 2 and calls == []
