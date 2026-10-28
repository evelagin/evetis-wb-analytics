"""Наблюдатель акций Ozon (PR-PROMO-1): разбор ответов, резолв SKU, идемпотентность.

Тесты офлайн: ни Seller API, ни BigQuery, ни Secret Manager не вызываются.
Фикстуры обезличены — ни одного персонального данного покупателя.
"""

from __future__ import annotations

import json

import pytest

P_ACTIONS = "/v1/actions"
P_PRODUCTS = "/v1/actions/products"
P_CANDIDATES = "/v1/actions/candidates"
P_AUTO_LIST = "/v1/actions/auto-add/products/list"
P_AUTO_CAND = "/v1/actions/auto-add/products/candidates"
P_PRICES = "/v5/product/info/prices"


# ───────────────────────────────────────────────── фикстуры живых форм ответа
ACTIONS = [
    {"id": 1977747, "title": "Эластичный бустинг", "action_type": "ELASTIC_BOOSTING",
     "date_start": "2025-03-19T21:00:44Z", "date_end": "2026-12-31T20:59:59Z",
     "auto_add_dates": ["2026-10-01T21:00:00Z"], "freeze_date": "",
     "potential_products_count": 2, "participating_products_count": 2,
     "is_participating": True, "is_voucher_action": False, "banned_products_count": 0,
     "with_targeting": False, "order_amount": 0, "discount_type": "CURRENCY",
     "discount_value": 0, "description": "текст"},
    {"id": 4253043, "title": "Максимальный бустинг", "action_type": "STOCK_DISCOUNT",
     "date_start": "2026-09-08T21:00:00Z", "date_end": "2026-10-06T20:59:59Z",
     "auto_add_dates": [], "freeze_date": "", "potential_products_count": 1,
     "participating_products_count": 0, "is_participating": False,
     "is_voucher_action": False, "banned_products_count": 0, "with_targeting": False,
     "order_amount": 0, "discount_type": "CURRENCY", "discount_value": 0,
     "description": "текст"},
]

PARTICIPANT = {"id": 1563818723, "price": 1287, "action_price": 1287, "max_action_price": 1287,
               "add_mode": "MANUAL", "stock": 0, "min_stock": 0,
               "alert_max_action_price_failed": False, "alert_max_action_price": 0,
               "current_boost": 15, "price_min_elastic": 1287, "price_max_elastic": 1076,
               "min_boost": 15, "max_boost": 55}

CANDIDATE = {"id": 2875763373, "price": 848, "action_price": 0, "max_action_price": 763,
             "add_mode": "NOT_SET", "stock": 0, "min_stock": 2,
             "alert_max_action_price_failed": False, "alert_max_action_price": 0,
             "current_boost": 15, "price_min_elastic": 763, "price_max_elastic": 644,
             "min_boost": 15, "max_boost": 55}

AUTO_SCHEDULED = {"product_id": 1563818723, "offer_id": "305101272", "sku": 1991772098,
                  "name": "Товар", "price": 1287, "base_price": 3225,
                  "max_discount_price": 1287, "min_seller_price": 1287,
                  "marketplace_seller_price": 1287, "action_price_to_auto_add": 1287,
                  "min_action_quantity": 0, "quantity_to_auto_add": 0,
                  "currency": "RUB", "add_mode": "AUTO"}

AUTO_ELIGIBLE = {"product_id": 1563818723, "offer_id": "305101272", "sku": 1991772098,
                 "name": "Товар", "price": 1287, "base_price": 3225,
                 "max_discount_price": 1044, "min_seller_price": 1287,
                 "marketplace_seller_price": 1287, "action_price_to_auto_add": 1044,
                 "min_action_quantity": 0, "quantity_to_auto_add": 0, "currency": "RUB"}

PRICES_ITEM = {
    "offer_id": "305101272", "product_id": 1563818723,
    "price": {"price": 1287, "min_price": 1287, "marketing_seller_price": 1287,
              "old_price": 3225, "auto_action_enabled": False,
              "auto_add_to_ozon_actions_list_enabled": True},
    "marketing_actions": {
        "current_period_from": None, "current_period_to": None,
        "ozon_actions_exist": False,
        "actions": [
            {"title": "Эластичный бустинг", "value": 1287,
             "date_from": "2025-03-19T21:00:44Z", "date_to": "2026-12-31T20:59:59Z"},
            {"title": "РК. Рассрочка 0-0-12", "value": 12,
             "date_from": "2026-09-10T08:00:00Z", "date_to": "2027-01-20T20:59:00Z"},
        ]},
}


def route(overrides=None):
    """Транспорт-заглушка: отдаёт живые формы ответов по пути."""
    o = overrides or {}

    def call(path, body=None):
        if path in o:
            return o[path](path, body) if callable(o[path]) else o[path]
        if path == P_ACTIONS:
            return 200, {"result": ACTIONS}
        if path == P_PRODUCTS:
            aid = body["action_id"]
            return 200, {"result": {"products": [PARTICIPANT] if aid == 1977747 else [],
                                    "total": 1, "last_id": ""}}
        if path == P_CANDIDATES:
            return 200, {"result": {"products": [CANDIDATE], "total": 1, "last_id": ""}}
        if path == P_AUTO_LIST:
            return 200, {"result": {"products": [AUTO_SCHEDULED], "total": 1}}
        if path == P_AUTO_CAND:
            return 200, {"result": {"products": [AUTO_ELIGIBLE], "total": 1}}
        if path == P_PRICES:
            return 200, {"items": [PRICES_ITEM], "cursor": ""}
        raise AssertionError(f"неожиданный путь {path}")

    return call


@pytest.fixture
def promo_mod(monkeypatch):
    """Модуль promo без сети, без BigQuery и без Secret Manager."""
    import promo as M

    monkeypatch.setattr(M, "log", lambda **_: None)
    monkeypatch.setattr(M, "secret", lambda name: f"fake-{name}")
    monkeypatch.setattr(M, "_sku_maps", lambda: ({1563818723: "EVT-FS-MOIST-30"},
                                                 {"305101272": "EVT-FS-MOIST-30"}))
    return M


@pytest.fixture
def captured(promo_mod, monkeypatch):
    """Перехват записи: [(table, rows, job_id), ...] + подавление SQL манифеста."""
    calls = []

    def _append(table, rows, job_id):
        calls.append((table, rows, job_id))
        return "LOADED", len(rows)

    class _Q:
        def result(self):
            return []

    class _BQ:
        def query(self, *_a, **_k):
            return _Q()

    monkeypatch.setattr(promo_mod, "append_rows", _append)
    monkeypatch.setattr(promo_mod, "bq", lambda: _BQ())
    return calls


def rows_of(calls, table):
    for t, rows, _ in calls:
        if t == table:
            return rows
    raise AssertionError(f"таблица {table} не записывалась")


# ─────────────────────────────────────────────────────────────────── разбор
def test_actions_parsed_with_source_fields(promo_mod, captured, monkeypatch):
    monkeypatch.setattr(promo_mod, "promo_call", route())
    promo_mod.promo("run-1", "ts", None, None)
    rows = rows_of(captured, promo_mod.TBL_ACTIONS)
    assert [r["action_id"] for r in rows] == [1977747, 4253043]
    a = rows[0]
    assert a["action_type"] == "ELASTIC_BOOSTING"
    assert a["is_participating"] is True
    assert a["auto_add_dates_count"] == 1
    assert a["auto_add_dates_csv"] == "2026-10-01T21:00:00Z"


def test_empty_freeze_date_is_null_not_epoch(promo_mod, captured, monkeypatch):
    """Пустая строка Ozon не должна превратиться в 1970-01-01."""
    monkeypatch.setattr(promo_mod, "promo_call", route())
    promo_mod.promo("run-1", "ts", None, None)
    for r in rows_of(captured, promo_mod.TBL_ACTIONS):
        assert r["freeze_at"] is None


def test_candidate_and_participant_are_different_states(promo_mod, captured, monkeypatch):
    """Ключевая находка фазы 0: «может добавить» ≠ «участвует»."""
    monkeypatch.setattr(promo_mod, "promo_call", route())
    promo_mod.promo("run-1", "ts", None, None)
    rows = rows_of(captured, promo_mod.TBL_PRODUCTS)
    memberships = {r["membership"] for r in rows}
    assert memberships == {"PARTICIPATING", "CANDIDATE"}
    part = [r for r in rows if r["membership"] == "PARTICIPATING"]
    cand = [r for r in rows if r["membership"] == "CANDIDATE"]
    assert part and cand
    # у участника акционная цена назначена, у кандидата источник отдаёт 0
    assert part[0]["action_price_rub"] == 1287
    assert cand[0]["action_price_rub"] == 0
    assert cand[0]["add_mode"] == "NOT_SET"
    # грейн различает состояния: хеши строк разные
    assert len({r["source_payload_hash"] for r in rows}) == len(rows)


def test_scheduled_and_eligible_auto_add_not_collapsed(promo_mod, captured, monkeypatch):
    """SCHEDULED («добавит») и ELIGIBLE («может добавить») — разные строки."""
    monkeypatch.setattr(promo_mod, "promo_call", route())
    promo_mod.promo("run-1", "ts", None, None)
    rows = rows_of(captured, promo_mod.TBL_AUTO_ADD)
    kinds = {r["list_kind"] for r in rows}
    assert kinds == {"SCHEDULED", "ELIGIBLE"}
    sched = [r for r in rows if r["list_kind"] == "SCHEDULED"][0]
    elig = [r for r in rows if r["list_kind"] == "ELIGIBLE"][0]
    # та самая разница, которая стоит 41 % вклада: цена автодобавления различается
    assert sched["action_price_to_auto_add_rub"] == 1287
    assert elig["action_price_to_auto_add_rub"] == 1044
    assert sched["add_mode"] == "AUTO"
    assert elig["add_mode"] is None
    assert sched["source_payload_hash"] != elig["source_payload_hash"]


def test_marketing_actions_preserved(promo_mod, captured, monkeypatch):
    """Блок marketing_actions.actions[] больше не отбрасывается."""
    monkeypatch.setattr(promo_mod, "promo_call", route())
    promo_mod.promo("run-1", "ts", None, None)
    mk = rows_of(captured, promo_mod.TBL_MARKETING)[0]
    assert mk["actions_count"] == 2
    assert json.loads(mk["marketing_actions_json"])["actions"][0]["title"] == "Эластичный бустинг"
    acts = rows_of(captured, promo_mod.TBL_PRODUCT_ACTION)
    assert [a["action_ordinal"] for a in acts] == [0, 1]
    # value разнороден по единице измерения — сохраняем как есть, без нормализации
    assert acts[0]["action_value_num"] == 1287     # рубли
    assert acts[1]["action_value_num"] == 12       # месяцы рассрочки


def test_false_actions_exist_flag_kept_verbatim_and_not_authoritative(promo_mod, captured, monkeypatch):
    """Флаг сохраняется дословно (FALSE) рядом с непустым actions[] — он не авторитетен."""
    monkeypatch.setattr(promo_mod, "promo_call", route())
    promo_mod.promo("run-1", "ts", None, None)
    mk = rows_of(captured, promo_mod.TBL_MARKETING)[0]
    assert mk["source_actions_exist_flag"] is False
    assert mk["actions_count"] == 2                 # флаг лжёт, и это видно в одной строке


def test_sku_resolution_and_unmapped_not_dropped(promo_mod, captured, monkeypatch):
    """Нерезолвленный товар остаётся в истории со статусом UNRESOLVED."""
    monkeypatch.setattr(promo_mod, "promo_call", route())
    monkeypatch.setattr(promo_mod, "_sku_maps", lambda: ({}, {}))
    promo_mod.promo("run-1", "ts", None, None)
    rows = rows_of(captured, promo_mod.TBL_PRODUCTS)
    assert rows, "строки не должны исчезать из-за неудачного резолва"
    assert all(r["internal_sku"] is None for r in rows)
    assert all(r["sku_resolution_status"] == "UNRESOLVED" for r in rows)


def test_auto_add_resolves_by_offer_id_when_product_id_unknown(promo_mod, captured, monkeypatch):
    monkeypatch.setattr(promo_mod, "promo_call", route())
    monkeypatch.setattr(promo_mod, "_sku_maps", lambda: ({}, {"305101272": "EVT-FS-MOIST-30"}))
    promo_mod.promo("run-1", "ts", None, None)
    rows = rows_of(captured, promo_mod.TBL_AUTO_ADD)
    assert all(r["sku_resolution_status"] == "RESOLVED_BY_OFFER_ID" for r in rows)


# ────────────────────────────────────────────────── пустые и битые ответы
def test_empty_actions_list_writes_nothing_but_does_not_fail(promo_mod, captured, monkeypatch):
    monkeypatch.setattr(promo_mod, "promo_call",
                        route({P_ACTIONS: (200, {"result": []}),
                               P_PRICES: (200, {"items": [], "cursor": ""})}))
    res = promo_mod.promo("run-1", "ts", None, None)
    assert res["received"] == 0
    assert rows_of(captured, promo_mod.TBL_ACTIONS) == []


def test_http_error_on_actions_raises(promo_mod, captured, monkeypatch):
    monkeypatch.setattr(promo_mod, "promo_call", route({P_ACTIONS: (500, {"_error": "oops"})}))
    with pytest.raises(RuntimeError, match="/v1/actions"):
        promo_mod.promo("run-1", "ts", None, None)


def test_malformed_payload_does_not_fabricate_values(promo_mod, captured, monkeypatch):
    """Мусор в числовых полях → NULL, а не 0."""
    broken = dict(PARTICIPANT, price="", action_price=None, max_action_price="нет",
                  current_boost="", min_stock=None)
    monkeypatch.setattr(promo_mod, "promo_call", route({
        P_PRODUCTS: (200, {"result": {"products": [broken], "total": 1, "last_id": ""}})}))
    promo_mod.promo("run-1", "ts", None, None)
    r = [x for x in rows_of(captured, promo_mod.TBL_PRODUCTS) if x["membership"] == "PARTICIPATING"][0]
    assert r["price_rub"] is None
    assert r["action_price_rub"] is None
    assert r["max_action_price_rub"] is None
    assert r["current_boost_pct"] is None
    assert r["min_stock"] is None


def test_unknown_source_field_is_visible_not_lost(promo_mod, captured, monkeypatch):
    extended = dict(PARTICIPANT, brand_new_field=42)
    monkeypatch.setattr(promo_mod, "promo_call", route({
        P_PRODUCTS: (200, {"result": {"products": [extended], "total": 1, "last_id": ""}})}))
    promo_mod.promo("run-1", "ts", None, None)
    r = [x for x in rows_of(captured, promo_mod.TBL_PRODUCTS) if x["membership"] == "PARTICIPATING"][0]
    # значение не потеряно: сырой payload сохранён целиком
    assert json.loads(r["raw_item_json"])["brand_new_field"] == 42


def test_page_ceiling_fails_loudly(promo_mod, monkeypatch):
    """Упор в потолок страниц роняет сущность, а не обрезает данные молча."""
    full = [dict(PARTICIPANT, id=i) for i in range(promo_mod.PAGE_LIMIT)]
    monkeypatch.setattr(promo_mod, "promo_call",
                        lambda path, body=None: (200, {"result": {"products": full,
                                                                  "last_id": f"x{body.get('last_id','')}"}}))
    with pytest.raises(RuntimeError, match="потолок страниц"):
        promo_mod._paged_products(P_PRODUCTS, 1)


# ───────────────────────────────────────────────────────── идемпотентность
def test_load_job_id_is_deterministic_per_slot_and_table():
    from common import promo_load_job_id, promo_observation_id, promo_slot
    import datetime as dt

    a = promo_load_job_id("prod", "2026-09-22T09:00", "RAW_OZON_PROMO_ACTIONS")
    assert a == promo_load_job_id("prod", "2026-09-22T09:00", "RAW_OZON_PROMO_ACTIONS")
    assert a != promo_load_job_id("prod", "2026-09-22T14:00", "RAW_OZON_PROMO_ACTIONS")
    assert a != promo_load_job_id("prod", "2026-09-22T09:00", "RAW_OZON_PROMO_PRODUCTS")
    assert all(c.isalnum() or c in "_-" for c in a)

    obs = promo_observation_id("prod", "2026-09-22T09:00")
    assert obs == promo_observation_id("prod", "2026-09-22T09:00")
    assert obs != promo_observation_id("shadow", "2026-09-22T09:00")

    utc = dt.timezone.utc
    assert promo_slot(dt.datetime(2026, 9, 22, 9, 0, tzinfo=utc)) == "2026-09-22T09:00"
    assert promo_slot(dt.datetime(2026, 9, 22, 13, 59, tzinfo=utc)) == "2026-09-22T09:00"
    assert promo_slot(dt.datetime(2026, 9, 22, 2, 0, tzinfo=utc)) == "2026-09-21T19:00"
    # четыре различных слота в сутки — это и есть каденс
    assert len({promo_slot(dt.datetime(2026, 9, 22, h, 30, tzinfo=utc)) for h in range(24)}) == 5


def test_repeated_run_in_same_slot_reuses_job_id(promo_mod, monkeypatch):
    """Повтор слота обращается к тем же job_id — BigQuery дедуплицирует load."""
    seen = []

    def _append(table, rows, job_id):
        seen.append(job_id)
        return "REUSED", len(rows)

    class _Q:
        def result(self):
            return []

    class _BQ:
        def query(self, *_a, **_k):
            return _Q()

    monkeypatch.setattr(promo_mod, "promo_call", route())
    monkeypatch.setattr(promo_mod, "append_rows", _append)
    monkeypatch.setattr(promo_mod, "bq", lambda: _BQ())
    monkeypatch.setattr(promo_mod, "promo_slot", lambda: "2026-09-22T09:00")

    promo_mod.promo("run-1", "ts", None, None)
    first = list(seen)
    seen.clear()
    promo_mod.promo("run-2", "ts", None, None)
    assert seen == first, "job_id обязан зависеть от слота, а не от прогона"


# ───────────────────────────────── изоляция этапов (инцидент 14–28.10.2026)
BLOCKED_9 = (400, {"_error": '{"code":9, "message":"the method is blocked due to unified price logic"}'})
GONE_404 = (404, {"_error": "404 page not found\n"})


@pytest.fixture
def manifest_sql(promo_mod, monkeypatch):
    """Перехват записи и SQL манифеста: [(table, rows)], [sql]."""
    written, sqls = [], []

    def _append(table, rows, job_id):
        written.append((table, rows))
        return "LOADED", len(rows)

    class _Q:
        def result(self):
            return []

    class _BQ:
        def query(self, sql, *_a, **_k):
            sqls.append(sql)
            return _Q()

    monkeypatch.setattr(promo_mod, "append_rows", _append)
    monkeypatch.setattr(promo_mod, "bq", lambda: _BQ())
    return written, sqls


def _final_update(sqls):
    ups = [q for q in sqls if q.lstrip().startswith("UPDATE")]
    assert len(ups) == 1, ups
    return ups[0]


@pytest.mark.parametrize("failure", [BLOCKED_9, GONE_404], ids=["400_code9", "404"])
def test_withdrawn_auto_add_method_keeps_other_stages(promo_mod, manifest_sql, monkeypatch, failure):
    """Снятый метод автодобавления больше не уносит акции, участников, кандидатов и marketing."""
    written, sqls = manifest_sql
    monkeypatch.setattr(promo_mod, "promo_call", route({P_AUTO_LIST: failure}))
    with pytest.raises(promo_mod.PromoPartial, match="auto_add_scheduled=UNSUPPORTED"):
        promo_mod.promo("run-1", "ts", None, None)
    tables = {t: rows for t, rows in written}
    assert len(tables[promo_mod.TBL_ACTIONS]) == 2
    assert {r["membership"] for r in tables[promo_mod.TBL_PRODUCTS]} == {"PARTICIPATING", "CANDIDATE"}
    # автодобавление: соседний (рабочий) путь ELIGIBLE записан, снятый SCHEDULED — нет
    assert {r["list_kind"] for r in tables[promo_mod.TBL_AUTO_ADD]} == {"ELIGIBLE"}
    assert tables[promo_mod.TBL_MARKETING] and tables[promo_mod.TBL_PRODUCT_ACTION]
    up = _final_update(sqls)
    assert "status='PARTIAL'" in up
    assert "auto_add_scheduled_rows=NULL" in up          # не 0: «не собрано» ≠ «нет товаров»
    assert "auto_add_eligible_rows=1" in up
    assert "error_code='PARTIAL_STAGES'" in up
    assert f"auto_add_scheduled=UNSUPPORTED({failure[0]})" in up


@pytest.mark.parametrize("code", [403, 429, 500])
def test_access_or_rate_errors_are_failed_not_empty(promo_mod, manifest_sql, monkeypatch, code):
    written, sqls = manifest_sql
    monkeypatch.setattr(promo_mod, "promo_call", route({P_CANDIDATES: (code, {"_error": "x"})}))
    with pytest.raises(promo_mod.PromoPartial, match=f"products_candidate=FAILED\\({code}\\)"):
        promo_mod.promo("run-1", "ts", None, None)
    tables = {t: rows for t, rows in written}
    assert {r["membership"] for r in tables[promo_mod.TBL_PRODUCTS]} == {"PARTICIPATING"}
    up = _final_update(sqls)
    assert "products_candidate=NULL" in up and "products_participating=1" in up


def test_stage_is_atomic_across_actions(promo_mod, manifest_sql, monkeypatch):
    """Отказ участников на второй акции отбрасывает и строки первой: частичный список хуже явного отказа."""
    written, _ = manifest_sql

    def products(path, body):
        if body["action_id"] == 4253043:
            return 404, {"_error": "gone"}
        return 200, {"result": {"products": [PARTICIPANT], "total": 1, "last_id": ""}}

    monkeypatch.setattr(promo_mod, "promo_call", route({P_PRODUCTS: products}))
    with pytest.raises(promo_mod.PromoPartial, match="products_participating=UNSUPPORTED"):
        promo_mod.promo("run-1", "ts", None, None)
    rows = dict(written)[promo_mod.TBL_PRODUCTS]
    assert {r["membership"] for r in rows} == {"CANDIDATE"}


def test_all_product_and_auto_add_stages_failed_skip_their_tables(promo_mod, manifest_sql, monkeypatch):
    """Ни одной записи в таблицу этапа, если все её этапы не собраны (job_id слота не расходуется)."""
    written, _ = manifest_sql
    monkeypatch.setattr(promo_mod, "promo_call", route({
        P_PRODUCTS: GONE_404, P_CANDIDATES: GONE_404, P_AUTO_LIST: GONE_404, P_AUTO_CAND: GONE_404}))
    with pytest.raises(promo_mod.PromoPartial):
        promo_mod.promo("run-1", "ts", None, None)
    tables = [t for t, _ in written]
    assert promo_mod.TBL_PRODUCTS not in tables and promo_mod.TBL_AUTO_ADD not in tables
    assert promo_mod.TBL_ACTIONS in tables and promo_mod.TBL_MARKETING in tables


def test_marketing_stage_failure_is_isolated(promo_mod, manifest_sql, monkeypatch):
    written, sqls = manifest_sql
    monkeypatch.setattr(promo_mod, "promo_call", route({P_PRICES: (503, {"_error": "busy"})}))
    with pytest.raises(promo_mod.PromoPartial, match="marketing=FAILED\\(503\\)"):
        promo_mod.promo("run-1", "ts", None, None)
    tables = [t for t, _ in written]
    assert promo_mod.TBL_MARKETING not in tables and promo_mod.TBL_PRODUCT_ACTION not in tables
    up = _final_update(sqls)
    assert "marketing_products=NULL" in up and "marketing_action_rows=NULL" in up


def test_actions_list_failure_is_still_fatal(promo_mod, manifest_sql, monkeypatch):
    """Без списка акций снимка нет: ERROR, как и раньше (не PARTIAL)."""
    written, sqls = manifest_sql
    monkeypatch.setattr(promo_mod, "promo_call", route({P_ACTIONS: GONE_404}))
    with pytest.raises(RuntimeError) as ei:
        promo_mod.promo("run-1", "ts", None, None)
    assert not isinstance(ei.value, promo_mod.PromoPartial)
    assert written == []
    assert "status='ERROR'" in _final_update(sqls)


def test_all_stages_ok_is_complete(promo_mod, manifest_sql, monkeypatch):
    _, sqls = manifest_sql
    monkeypatch.setattr(promo_mod, "promo_call", route())
    promo_mod.promo("run-1", "ts", None, None)
    up = _final_update(sqls)
    assert "status='COMPLETE'" in up and "error_code" not in up and "=NULL, " not in up.split("unmapped_ids")[0]
