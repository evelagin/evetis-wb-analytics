"""Проверки полноты для /v3/product/list и /api/client/campaign.

Пагинацию здесь не вводим: конец списка по last_id в Swagger не доказан
(у product/list last_id непуст и на последней странице). Доказан total:
  product/list 2026-08-30: 20 товаров, result.total=20;
  campaign     2026-08-31: 92 кампании, total="92".
Если total больше, чем пришло, ответ неполный — сущность падает, а не грузит часть.
"""

from __future__ import annotations

import json

import pytest

PRODUCT_LIST = "/v3/product/list"


def product_list(items, total, last_id="WzUy"):
    return 200, {"result": {"items": items, "total": total, "last_id": last_id}}


def products(n):
    return [{"product_id": 10 + i, "sku": 500 + i, "offer_id": f"o{i}"} for i in range(n)]


def route(pl_response, other=None):
    def seller_post(path, body):
        if path == PRODUCT_LIST:
            return pl_response
        return other(path, body) if other else (200, {"items": []})
    return seller_post


# ------------------------------------------------------------ product/list
def test_product_list_complete_when_total_matches(entities, monkeypatch):
    monkeypatch.setattr(entities, "seller_post", route(product_list(products(20), 20)))
    assert len(entities._product_list_items()) == 20


def test_product_list_incomplete_fails_closed(entities, monkeypatch):
    monkeypatch.setattr(entities, "seller_post", route(product_list(products(1000), 1001)))
    with pytest.raises(entities.PaginationError, match="1000 из total=1001"):
        entities._product_list_items()


def test_product_list_without_total_keeps_old_semantics(entities, monkeypatch):
    monkeypatch.setattr(entities, "seller_post",
                        route((200, {"result": {"items": products(3)}})))
    assert len(entities._product_list_items()) == 3


def test_product_list_request_is_unchanged(entities, monkeypatch):
    seen = []

    def sp(path, body):
        seen.append((path, body))
        return product_list([], 0)
    monkeypatch.setattr(entities, "seller_post", sp)
    entities._product_list_items()
    assert seen == [(PRODUCT_LIST, {"filter": {"visibility": "ALL"}, "last_id": "", "limit": 1000})]


def test_catalog_fails_on_incomplete_product_list(entities, captured_merges, monkeypatch):
    monkeypatch.setattr(entities, "seller_post", route(product_list(products(2), 5)))
    with pytest.raises(entities.PaginationError):
        entities.catalog("rt", "ts", None, None)
    assert captured_merges == []


def test_stocks_fails_on_incomplete_product_list(entities, captured_merges, monkeypatch):
    monkeypatch.setattr(entities, "seller_post", route(product_list(products(2), 5)))
    with pytest.raises(entities.PaginationError):
        entities.stocks("rt", "ts", None, None)
    assert captured_merges == []


def test_stocks_fails_on_product_list_error(entities, captured_merges, monkeypatch):
    """Раньше код ответа не проверялся и в analytics/stocks уходил пустой фильтр skus."""
    monkeypatch.setattr(entities, "seller_post", route((500, {"_error": "x"})))
    with pytest.raises(RuntimeError, match="product/list 500"):
        entities.stocks("rt", "ts", None, None)
    assert captured_merges == []


def test_stocks_passes_all_skus_when_complete(entities, captured_merges, monkeypatch):
    seen = {}

    def other(path, body):
        seen["skus"] = body["skus"]
        return 200, {"items": []}
    monkeypatch.setattr(entities, "seller_post", route(product_list(products(3), 3), other))
    entities.stocks("rt", "ts", None, None)
    assert seen["skus"] == ["500", "501", "502"]


# ------------------------------------------------------------ campaign
def campaigns(n, total):
    body = {"list": [{"id": str(30000000 + i), "state": "CAMPAIGN_STATE_RUNNING"} for i in range(n)]}
    if total is not None:
        body["total"] = total
    return json.dumps(body)


def test_campaign_complete_string_total(entities):
    assert len(entities._campaign_list(campaigns(92, "92"))) == 92


def test_campaign_incomplete_fails_closed(entities):
    with pytest.raises(entities.PaginationError, match="100 из total=101"):
        entities._campaign_list(campaigns(100, "101"))


@pytest.mark.parametrize("total", [None, "", "n/a"])
def test_campaign_without_numeric_total_keeps_old_semantics(entities, total):
    assert len(entities._campaign_list(campaigns(3, total))) == 3


def test_ads_campaigns_fails_on_incomplete_list(entities, captured_merges, monkeypatch):
    monkeypatch.setattr(entities, "perf_get", lambda path, raw_text=True: (200, campaigns(100, "150")))
    with pytest.raises(entities.PaginationError):
        entities.ads_campaigns("rt", "ts", None, None)
    assert captured_merges == []


def test_ads_sku_daily_fails_on_incomplete_campaign_perimeter(entities, captured_merges, monkeypatch):
    monkeypatch.setattr(entities, "perf_get", lambda path, raw_text=True: (200, campaigns(100, "150")))
    with pytest.raises(entities.PaginationError):
        entities.ads_sku_daily("rt", "ts", "2026-09-01", "2026-09-07")
    assert captured_merges == []


def test_ads_sku_daily_campaign_error_branch_unchanged(entities, captured_merges, monkeypatch):
    """Ветка code != 200 → пустой периметр оставлена как была (OPEN QUESTION)."""
    def pg(path, raw_text=True):
        if path == "/api/client/campaign":
            return 500, ""
        return 200, "ID;Расход\n"
    monkeypatch.setattr(entities, "perf_get", pg)
    res = entities.ads_sku_daily("rt", "ts", "2026-09-01", "2026-09-07")
    assert res["received"] == 0
