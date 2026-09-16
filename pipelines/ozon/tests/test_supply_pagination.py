"""F-01: пагинация /v3/supply-order/list и /v1/supply-order/bundle.

Контракт — снимок Swagger Seller API 2026-08-31:
  supply-order/list  : last_id в запросе и ответе, limit 1..100, has_next нет;
                       последняя страница реального ответа — last_id="".
  supply-order/bundle: last_id + has_next, limit 1..100; на последней странице
                       реального ответа has_next=false при НЕПУСТОМ last_id.
Транспорт замокан, к Ozon тесты не обращаются.
"""

from __future__ import annotations

import pytest

LIST = "/v3/supply-order/list"
BUNDLE = "/v1/supply-order/bundle"
GET = "/v3/supply-order/get"


def list_pages(pages):
    """pages: {запрошенный last_id ('' для первой): (code, payload)}."""
    def respond(_path, body):
        return pages[body.get("last_id", "")]
    return respond


def bundle_pages(pages):
    def respond(_path, body):
        return pages[body.get("last_id", "")]
    return respond


def is_(path):
    return lambda p, _b: p == path


# ---------------------------------------------------------------- A. list
def test_list_single_page(entities, transport, monkeypatch):
    t = transport([(is_(LIST), list_pages({"": (200, {"order_ids": [1, 2, 3], "last_id": ""})}))])
    monkeypatch.setattr(entities, "seller_post", t)
    assert entities._supply_order_ids() == [1, 2, 3]
    assert len(t.calls) == 1


def test_list_first_request_is_unchanged(entities, transport, monkeypatch):
    """Первый запрос побайтно прежний: без last_id, тот же фильтр и сортировка."""
    t = transport([(is_(LIST), list_pages({"": (200, {"order_ids": [], "last_id": ""})}))])
    monkeypatch.setattr(entities, "seller_post", t)
    entities._supply_order_ids()
    assert t.calls[0][1] == {"filter": {"states": entities.CPC_STATES}, "limit": 100,
                             "sort_by": "ORDER_CREATION", "sort_dir": "DESC"}


def test_list_two_pages_follow_last_id(entities, transport, monkeypatch):
    t = transport([(is_(LIST), list_pages({
        "": (200, {"order_ids": [1, 2], "last_id": "c1"}),
        "c1": (200, {"order_ids": [3], "last_id": ""}),
    }))])
    monkeypatch.setattr(entities, "seller_post", t)
    assert entities._supply_order_ids() == [1, 2, 3]
    assert [b.get("last_id") for _, b in t.calls] == [None, "c1"]
    # сортировка и фильтр не меняются между страницами
    assert all(b["sort_by"] == "ORDER_CREATION" and b["sort_dir"] == "DESC"
               and b["filter"] == {"states": entities.CPC_STATES} for _, b in t.calls)


def test_list_exactly_100_with_continuation_requests_next_page(entities, transport, monkeypatch):
    """Ровно 100 — не признак конца: старый код остановился бы здесь и потерял остаток."""
    t = transport([(is_(LIST), list_pages({
        "": (200, {"order_ids": list(range(100)), "last_id": "c1"}),
        "c1": (200, {"order_ids": [], "last_id": ""}),
    }))])
    monkeypatch.setattr(entities, "seller_post", t)
    assert len(entities._supply_order_ids()) == 100
    assert len(t.calls) == 2


def test_list_exactly_100_terminal(entities, transport, monkeypatch):
    t = transport([(is_(LIST), list_pages({
        "": (200, {"order_ids": list(range(100)), "last_id": ""}),
    }))])
    monkeypatch.setattr(entities, "seller_post", t)
    assert len(entities._supply_order_ids()) == 100
    assert len(t.calls) == 1


def test_list_more_than_100_is_not_truncated(entities, transport, monkeypatch):
    """Регрессия F-01: 101-я и дальнейшие заявки больше не теряются."""
    t = transport([(is_(LIST), list_pages({
        "": (200, {"order_ids": list(range(100)), "last_id": "c1"}),
        "c1": (200, {"order_ids": list(range(100, 175)), "last_id": ""}),
    }))])
    monkeypatch.setattr(entities, "seller_post", t)
    assert entities._supply_order_ids() == list(range(175))


def test_list_empty_response(entities, transport, monkeypatch):
    t = transport([(is_(LIST), list_pages({"": (200, {"order_ids": [], "last_id": ""})}))])
    monkeypatch.setattr(entities, "seller_post", t)
    assert entities._supply_order_ids() == []
    assert len(t.calls) == 1


def test_list_missing_fields_is_terminal(entities, transport, monkeypatch):
    t = transport([(is_(LIST), list_pages({"": (200, {})}))])
    monkeypatch.setattr(entities, "seller_post", t)
    assert entities._supply_order_ids() == []


def test_list_empty_page_with_cursor_is_terminal(entities, transport, monkeypatch):
    """Пустая страница завершает обход даже при непустом last_id — без зацикливания."""
    t = transport([(is_(LIST), list_pages({
        "": (200, {"order_ids": [1], "last_id": "c1"}),
        "c1": (200, {"order_ids": [], "last_id": "c2"}),
    }))])
    monkeypatch.setattr(entities, "seller_post", t)
    assert entities._supply_order_ids() == [1]
    assert len(t.calls) == 2


def test_list_terminal_last_id(entities, transport, monkeypatch):
    """Непустая страница с пустым last_id — конец (так выглядит реальный ответ 2026-08-31)."""
    t = transport([(is_(LIST), list_pages({
        "": (200, {"order_ids": list(range(65)), "last_id": ""}),
    }))])
    monkeypatch.setattr(entities, "seller_post", t)
    assert len(entities._supply_order_ids()) == 65
    assert len(t.calls) == 1


def test_list_repeated_last_id_fails_closed(entities, transport, monkeypatch):
    t = transport([(is_(LIST), list_pages({
        "": (200, {"order_ids": [1], "last_id": "c1"}),
        "c1": (200, {"order_ids": [2], "last_id": "c1"}),
    }))])
    monkeypatch.setattr(entities, "seller_post", t)
    with pytest.raises(entities.PaginationError, match="повторился"):
        entities._supply_order_ids()


def test_list_cursor_cycle_fails_closed(entities, transport, monkeypatch):
    t = transport([(is_(LIST), list_pages({
        "": (200, {"order_ids": [1], "last_id": "a"}),
        "a": (200, {"order_ids": [2], "last_id": "b"}),
        "b": (200, {"order_ids": [3], "last_id": "a"}),
    }))])
    monkeypatch.setattr(entities, "seller_post", t)
    with pytest.raises(entities.PaginationError):
        entities._supply_order_ids()


def test_list_page_ceiling_fails_closed(entities, transport, monkeypatch):
    monkeypatch.setattr(entities, "SUPPLY_LIST_MAX_PAGES", 3)
    counter = {"n": 0}

    def endless(_p, _b):
        counter["n"] += 1
        return 200, {"order_ids": [counter["n"]], "last_id": f"c{counter['n']}"}
    t = transport([(is_(LIST), endless)])
    monkeypatch.setattr(entities, "seller_post", t)
    with pytest.raises(entities.PaginationError, match="потолок"):
        entities._supply_order_ids()
    assert len(t.calls) == 3


def test_list_error_on_later_page_is_not_silent_truncation(entities, transport, monkeypatch):
    t = transport([(is_(LIST), list_pages({
        "": (200, {"order_ids": [1], "last_id": "c1"}),
        "c1": (500, {"_error": "boom"}),
    }))])
    monkeypatch.setattr(entities, "seller_post", t)
    with pytest.raises(RuntimeError, match="page 2 500"):
        entities._supply_order_ids()


def test_list_error_on_first_page_fails(entities, transport, monkeypatch):
    """Раньше ошибка первого запроса выглядела как пустой список и давала OK с 0 строк."""
    t = transport([(is_(LIST), list_pages({"": (403, {"_error": "denied"})}))])
    monkeypatch.setattr(entities, "seller_post", t)
    with pytest.raises(RuntimeError, match="403"):
        entities._supply_order_ids()


def test_list_duplicate_ids_across_pages_are_fetched_once(entities, transport, monkeypatch):
    t = transport([(is_(LIST), list_pages({
        "": (200, {"order_ids": [1, 2], "last_id": "c1"}),
        "c1": (200, {"order_ids": [2, 3], "last_id": ""}),
    }))])
    monkeypatch.setattr(entities, "seller_post", t)
    assert entities._supply_order_ids() == [1, 2, 3]


def test_list_page_limit_within_swagger_bounds(entities):
    assert 1 <= entities.SUPPLY_LIST_PAGE_LIMIT <= 100
    assert 1 <= entities.BUNDLE_PAGE_LIMIT <= 100


# ---------------------------------------------------------------- B. bundle
def items(n, start=0):
    return [{"sku": 1000 + i, "quantity": 1} for i in range(start, start + n)]


def test_bundle_single_page(entities, transport, monkeypatch):
    t = transport([(is_(BUNDLE), bundle_pages({
        "": (200, {"items": items(5), "total_count": 5, "has_next": False, "last_id": "x"}),
    }))])
    monkeypatch.setattr(entities, "seller_post", t)
    assert len(entities._supply_bundle_items("b1")) == 5
    assert len(t.calls) == 1
    assert t.calls[0][1] == {"bundle_ids": ["b1"], "limit": 100}


def test_bundle_exactly_100_items_terminal(entities, transport, monkeypatch):
    t = transport([(is_(BUNDLE), bundle_pages({
        "": (200, {"items": items(100), "total_count": 100, "has_next": False, "last_id": "x"}),
    }))])
    monkeypatch.setattr(entities, "seller_post", t)
    assert len(entities._supply_bundle_items("b1")) == 100
    assert len(t.calls) == 1


def test_bundle_has_next_follows_last_id(entities, transport, monkeypatch):
    """Состав больше 100 позиций: старый код брал только первую сотню."""
    t = transport([(is_(BUNDLE), bundle_pages({
        "": (200, {"items": items(100), "total_count": 130, "has_next": True, "last_id": "p1"}),
        "p1": (200, {"items": items(30, 100), "total_count": 130, "has_next": False, "last_id": "p2"}),
    }))])
    monkeypatch.setattr(entities, "seller_post", t)
    got = entities._supply_bundle_items("b1")
    assert [i["sku"] for i in got] == [1000 + i for i in range(130)]
    assert [b.get("last_id") for _, b in t.calls] == [None, "p1"]
    assert all(b["bundle_ids"] == ["b1"] for _, b in t.calls)


def test_bundle_nonempty_last_id_with_has_next_false_is_terminal(entities, transport, monkeypatch):
    """Реальный ответ: has_next=false и непустой last_id. По last_id конец не определяется."""
    t = transport([(is_(BUNDLE), bundle_pages({
        "": (200, {"items": items(5), "total_count": 5, "has_next": False, "last_id": "1997079254"}),
    }))])
    monkeypatch.setattr(entities, "seller_post", t)
    entities._supply_bundle_items("b1")
    assert len(t.calls) == 1


def test_bundle_repeated_cursor_fails_closed(entities, transport, monkeypatch):
    t = transport([(is_(BUNDLE), bundle_pages({
        "": (200, {"items": items(100), "has_next": True, "last_id": "p1"}),
        "p1": (200, {"items": items(100, 100), "has_next": True, "last_id": "p1"}),
    }))])
    monkeypatch.setattr(entities, "seller_post", t)
    with pytest.raises(entities.PaginationError, match="повторился"):
        entities._supply_bundle_items("b1")


def test_bundle_has_next_without_last_id_fails_closed(entities, transport, monkeypatch):
    t = transport([(is_(BUNDLE), bundle_pages({
        "": (200, {"items": items(100), "has_next": True, "last_id": ""}),
    }))])
    monkeypatch.setattr(entities, "seller_post", t)
    with pytest.raises(entities.PaginationError, match="без last_id"):
        entities._supply_bundle_items("b1")


def test_bundle_empty_last_page(entities, transport, monkeypatch):
    t = transport([(is_(BUNDLE), bundle_pages({
        "": (200, {"items": items(100), "total_count": 100, "has_next": True, "last_id": "p1"}),
        "p1": (200, {"items": [], "total_count": 100, "has_next": False, "last_id": "p1x"}),
    }))])
    monkeypatch.setattr(entities, "seller_post", t)
    assert len(entities._supply_bundle_items("b1")) == 100
    assert len(t.calls) == 2


def test_bundle_fewer_items_than_total_count_fails_closed(entities, transport, monkeypatch):
    t = transport([(is_(BUNDLE), bundle_pages({
        "": (200, {"items": items(100), "total_count": 150, "has_next": False, "last_id": "x"}),
    }))])
    monkeypatch.setattr(entities, "seller_post", t)
    with pytest.raises(entities.PaginationError, match="total_count=150"):
        entities._supply_bundle_items("b1")


def test_bundle_page_ceiling_fails_closed(entities, transport, monkeypatch):
    monkeypatch.setattr(entities, "BUNDLE_MAX_PAGES", 2)
    counter = {"n": 0}

    def endless(_p, _b):
        counter["n"] += 1
        return 200, {"items": items(1), "has_next": True, "last_id": f"p{counter['n']}"}
    t = transport([(is_(BUNDLE), endless)])
    monkeypatch.setattr(entities, "seller_post", t)
    with pytest.raises(entities.PaginationError, match="потолок"):
        entities._supply_bundle_items("b1")
    assert len(t.calls) == 2


def test_bundle_error_fails(entities, transport, monkeypatch):
    t = transport([(is_(BUNDLE), bundle_pages({"": (404, {"_error": "nf"})}))])
    monkeypatch.setattr(entities, "seller_post", t)
    with pytest.raises(RuntimeError, match="404"):
        entities._supply_bundle_items("b1")


# ---------------------------------------------------------------- supplies()
def order(oid, supplies):
    return {"order_id": oid, "order_number": f"N{oid}", "state": "COMPLETED",
            "created_date": "2026-09-01T00:00:00Z", "supplies": supplies}


def supply(sid, bid):
    return {"supply_id": sid, "bundle_id": bid, "state": "COMPLETED",
            "storage_warehouse": {"warehouse_id": 77, "name": "W"}}


def test_supplies_end_to_end_keys_and_row_counts(entities, transport, captured_merges, monkeypatch):
    orders = {1: order(1, [supply(11, "bA")]), 2: order(2, [supply(21, "bB")]),
              3: order(3, [supply(31, "bC")])}

    def get(_p, body):
        return 200, {"orders": [orders[i] for i in body["order_ids"]]}
    t = transport([
        (is_(LIST), list_pages({
            "": (200, {"order_ids": [1, 2], "last_id": "c1"}),
            "c1": (200, {"order_ids": [3], "last_id": ""}),
        })),
        (is_(GET), get),
        (lambda p, b: p == BUNDLE and b["bundle_ids"] == ["bA"], bundle_pages({
            "": (200, {"items": items(100), "has_next": True, "last_id": "p1"}),
            "p1": (200, {"items": items(20, 100), "has_next": False, "last_id": "p2"}),
        })),
        (lambda p, b: p == BUNDLE, (200, {"items": items(2), "has_next": False, "last_id": "z"})),
    ])
    monkeypatch.setattr(entities, "seller_post", t)
    res = entities.supplies("rt-test", "2026-09-16T00:00:00+03:00", None, None)

    tables = {c[0]: c for c in captured_merges}
    # ключи слияния не изменены
    assert tables["RAW_OZON_SUPPLY_ORDERS"][2] == ["order_id"]
    assert tables["RAW_OZON_SUPPLIES"][2] == ["order_id", "supply_id"]
    assert tables["RAW_OZON_SUPPLY_BUNDLES"][2] == ["bundle_id", "sku"]
    assert len(tables["RAW_OZON_SUPPLY_ORDERS"][1]) == 3            # страница 2 не потеряна
    assert len(tables["RAW_OZON_SUPPLIES"][1]) == 3
    assert len(tables["RAW_OZON_SUPPLY_BUNDLES"][1]) == 120 + 2 + 2  # >100 позиций в bA
    assert res["received"] == 3 + 3 + 124


def test_supplies_get_error_fails(entities, transport, captured_merges, monkeypatch):
    t = transport([
        (is_(LIST), list_pages({"": (200, {"order_ids": [1], "last_id": ""})})),
        (is_(GET), (500, {"_error": "x"})),
    ])
    monkeypatch.setattr(entities, "seller_post", t)
    with pytest.raises(RuntimeError, match="supply-order/get 500"):
        entities.supplies("rt-test", "ts", None, None)
    assert captured_merges == []


def test_supplies_failed_bundle_writes_others_then_fails(entities, transport, captured_merges,
                                                         monkeypatch):
    orders = {1: order(1, [supply(11, "bOK")]), 2: order(2, [supply(21, "bBAD")])}
    t = transport([
        (is_(LIST), list_pages({"": (200, {"order_ids": [1, 2], "last_id": ""})})),
        (is_(GET), lambda _p, b: (200, {"orders": [orders[i] for i in b["order_ids"]]})),
        (lambda p, b: p == BUNDLE and b["bundle_ids"] == ["bBAD"], (500, {"_error": "x"})),
        (lambda p, b: p == BUNDLE, (200, {"items": items(3), "has_next": False, "last_id": "z"})),
    ])
    monkeypatch.setattr(entities, "seller_post", t)
    with pytest.raises(RuntimeError, match="не прочитано составов 1 из 2"):
        entities.supplies("rt-test", "ts", None, None)
    bundles = [c for c in captured_merges if c[0] == "RAW_OZON_SUPPLY_BUNDLES"][0][1]
    # прочитанный состав записан целиком, упавший не записан частично
    assert {r["bundle_id"] for r in bundles} == {"bOK"} and len(bundles) == 3
