#!/usr/bin/env python3
"""Сборщики сущностей Ozon. Только READ-методы API.

Запрещены и не вызываются: любые mutation endpoints Seller и Performance API,
а также deprecated /v3/finance/transaction/*, /v2/posting/fbo/list.
"""
import csv
import io
import json
import re
import time
import zipfile
from datetime import date, timedelta

import common as C
from common import (h, log, merge_rows, now_msk, perf_get, perf_post, seller_post)
from promo import promo

CPC_STATES = ["DATA_FILLING", "READY_TO_SUPPLY", "ACCEPTED_AT_SUPPLY_WAREHOUSE",
              "IN_TRANSIT", "ACCEPTANCE_AT_STORAGE_WAREHOUSE",
              "REPORTS_CONFIRMATION_AWAITING", "REPORT_REJECTED", "COMPLETED",
              "REJECTED_AT_SUPPLY_WAREHOUSE", "CANCELLED", "OVERDUE"]

# Потолки страниц — защита от бесконечного цикла, а не ожидаемый объём.
# Упор в потолок роняет сущность, а не обрезает данные молча.
# Лимиты страниц — максимумы из Swagger Seller API (снимок 2026-08-31).
SUPPLY_LIST_PAGE_LIMIT = 100          # /v3/supply-order/list: limit 1..100
SUPPLY_LIST_MAX_PAGES = 500           # 50 000 заявок
BUNDLE_PAGE_LIMIT = 100               # /v1/supply-order/bundle: limit 1..100
BUNDLE_MAX_PAGES = 500                # 50 000 позиций в одном составе


class PaginationError(RuntimeError):
    """Пагинация не может завершиться корректно: повтор курсора, потолок страниц,
    обещание следующей страницы без курсора или неполный ответ."""


# ─────────────────────────────── строгие лимиты бэкфилла (Tenancy T2)
# Места ниже исторически обрезают данные МОЛЧА: упор в потолок страниц или
# пропуск отчёта заканчивается частичной загрузкой со статусом OK. Для суточного
# прогона EVETIS это известный и принятый риск (окна короткие). Для бэкфилла
# нового арендатора молчаливая обрезка недопустима: история выглядела бы полной.
#
# STRICT_PAGE_CAPS=1 превращает каждое такое место в явный отказ сущности с
# диагностикой. Без флага (или =0) поведение прежнее, байт в байт.
FBO_POSTINGS_MAX_PAGES = 200            # прежний литерал `page > 200`
FINANCE_ACCRUAL_MAX_PAGES_PER_DAY = 60  # прежний литерал `page > 60`
ADS_REPORT_POLL_ATTEMPTS = 60           # прежний литерал range(60)
# Performance API, POST /api/client/statistics, поля from/to: «Максимальный
# период, за который можно получить отчёт — 62 дня». Окно длиннее отдаёт ноль
# строк при HTTP 200 (замер EVETIS: 65 дней → 0 строк).
PERFORMANCE_REPORT_MAX_DAYS = 62

# ─────────────────────────────── T5: пределы источников (Swagger Seller/Performance 2026-09-28)
PRODUCT_LIST_LIMIT = 1000               # /v3/product/list: limit 1..1000, пагинация last_id
PRODUCT_LIST_MAX_PAGES = 100            # 100 000 товаров — защита от бесконечного цикла
PRODUCT_INFO_BATCH = 1000               # /v3/product/info/list: ≤ 1000 идентификаторов за запрос
STOCKS_SKU_BATCH = 100                  # /v1/analytics/stocks: skus maximum 100
PRICES_MAX_PAGES = 2000                 # /v5/product/info/prices: 200 000 строк при limit 100
FBO_MAX_PERIOD_DAYS = 365               # /v3/posting/fbo/list: период > года → PERIOD_IS_TOO_LONG
# Строгий режим: окно отчёта по SKU режется на части по датам (МСК, dateFrom/dateTo), не
# длиннее этого числа суток включительно — с запасом к лимиту 62 дня.
ADS_SKU_STRICT_CHUNK_DAYS = 60
ADS_SKU_BATCH = 10                      # Performance API: не больше 10 кампаний в отчёте


class StrictLimitError(PaginationError):
    """STRICT_PAGE_CAPS=1: источник отдал бы неполные данные, а прогон — статус OK."""


def _strict_cap(entity, what, hint, **diag):
    """В строгом режиме — отказ с диагностикой; иначе ничего (прежнее поведение).

    В сообщение попадают только коды ответов, окно и счётчики — никаких
    заголовков, тел запросов и учётных данных.
    """
    if not C.STRICT_PAGE_CAPS:
        return
    details = ", ".join(f"{k}={v}" for k, v in diag.items())
    raise StrictLimitError(
        f"{entity}: {what} [{details}]. STRICT_PAGE_CAPS=1: данные были бы неполными "
        f"при статусе OK. Что сделать: {hint}")


def _meta(endpoint, run_id, ts):
    return {"extracted_at": ts, "source_endpoint": endpoint, "ingestion_run_id": run_id}


def _num(v):
    return float(v) if v not in (None, "") else None


# ------------------------------------------------ полнота списочных ответов
def _list_total(result):
    """Всего элементов. total отключат 23.11.2026 в пользу total_items.

    Без флага — прежний порядок (total, затем total_items): решение EVETIS о полноте не
    меняется, пока total есть. Строгий режим: оба поля есть и различаются — отказ.
    """
    vals = {k: result.get(k) for k in ("total", "total_items")
            if isinstance(result.get(k), int) and not isinstance(result.get(k), bool)}
    if C.STRICT_PAGE_CAPS and len(set(vals.values())) > 1:
        _strict_cap("catalog", f"total={vals['total']} и total_items={vals['total_items']} различаются",
                    "проверить контракт метода")
    for k in ("total", "total_items"):
        if k in vals:
            return vals[k]
    return None


def _product_list_items(visibility="ALL"):
    """Полный список товаров: страницы по last_id до total, с проверкой полноты.

    Swagger /v3/product/list: limit ≤ 1000, пагинация через result.last_id, «всего» —
    result.total_items (result.total отключат 23.11.2026). last_id на последней странице НЕ
    пуст (ответ 2026-08-30: 20 товаров, total=20, last_id непустой), поэтому конец
    определяется по total, а не по курсору.

    Первый запрос прежний. Следующие страницы запрашиваются только когда total больше
    полученного (раньше это был отказ). Страница без новых товаров, повтор курсора или
    потолок страниц до достижения total — PaginationError: часть каталога не грузится.
    Нет total: прежде — предупреждение в журнал; в строгом режиме (T5) — отказ.
    """
    if visibility not in {"ALL", "ARCHIVED"}:
        raise ValueError("unreviewed catalog visibility")
    body = {"filter": {"visibility": visibility}, "last_id": "", "limit": PRODUCT_LIST_LIMIT}
    items, seen, cursors, total = [], set(), set(), None
    for page in range(1, PRODUCT_LIST_MAX_PAGES + 1):
        code, lst = seller_post("/v3/product/list", body)
        if code != 200:
            raise RuntimeError(f"product/list {code}: {lst}")
        result = lst.get("result") or {}
        fresh = []
        for i in (result.get("items") or []):
            # Повтор товара считается один раз. Исключение — первая страница без флага: она
            # учитывается как раньше (сырой счёт), это поведение EVETIS.
            if (not C.STRICT_PAGE_CAPS and page == 1) or i.get("product_id") not in seen:
                seen.add(i.get("product_id"))
                fresh.append(i)
        items += fresh
        total = _list_total(result) if total is None else total
        if total is None:
            _strict_cap("catalog", "в ответе /v3/product/list нет total_items/total — полноту "
                        "каталога проверить нечем", "проверить контракт метода", items=len(items))
            log(event="completeness_unverified", endpoint="/v3/product/list",
                reason="в ответе нет целого total", items=len(items))
            return items
        if len(items) >= total:
            return items
        nxt = result.get("last_id") or ""
        if not fresh or not nxt or nxt in cursors:
            break
        cursors.add(nxt)
        body = dict(body, last_id=nxt)
        time.sleep(0.2)
    raise PaginationError(
        f"product/list: получено {len(items)} из total={total}; "
        "ответ неполный — пагинация по last_id не дошла до total")


def _campaign_list(txt):
    """Список кампаний Performance API с проверкой полноты.

    Swagger GET /api/client/campaign объявляет page/pageSize, размер страницы
    по умолчанию не указан. Ответ содержит total (строка): 2026-08-31 total="92"
    при 92 элементах. Если total больше, чем пришло, — ответ неполный.
    """
    d = json.loads(txt)
    items = d.get("list") or []
    try:
        total = int(d.get("total"))
    except (TypeError, ValueError):
        log(event="completeness_unverified", endpoint="/api/client/campaign",
            reason="в ответе нет числового total", items=len(items))
        return items
    if total > len(items):
        raise PaginationError(
            f"campaign: получено {len(items)} из total={total}; "
            "ответ неполный — нужна пагинация page/pageSize")
    return items


# --------------------------------------------------------------- каталог
def _product_info_items(ids):
    """Карточки по product_id партиями ≤ 1000 (лимит /v3/product/info/list)."""
    out = []
    for k in range(0, max(len(ids), 1), PRODUCT_INFO_BATCH):
        code, info = seller_post("/v3/product/info/list",
                                 {"product_id": ids[k:k + PRODUCT_INFO_BATCH], "offer_id": [], "sku": []})
        if code != 200:
            raise RuntimeError(f"product/info {code}: {info}")
        out += info.get("items") or []
    if C.STRICT_PAGE_CAPS and {i.get("id") for i in out} != set(ids):
        _strict_cap("catalog", f"карточек {len(out)} на {len(set(ids))} товаров списка (наборы id различаются)",
                    "повторить прогон; часть карточек не вернулась", ids=len(set(ids)))
    return out


def _catalog_rows(items, run_id, ts):
    d = str(now_msk().date())
    rows = []
    for i in items:
        st = i.get("stocks") or {}
        vd = i.get("visibility_details") or {}
        pi = i.get("price_indexes") or {}
        rows.append(dict(snapshot_date=d, sku=str(i["sku"]), product_id=str(i["id"]),
            offer_id=str(i["offer_id"]), name=i.get("name"),
            is_archived=bool(i.get("is_archived")), has_stock=bool(st.get("has_stock")),
            stock_present=sum(x.get("present", 0) for x in (st.get("stocks") or [])),
            stock_reserved=sum(x.get("reserved", 0) for x in (st.get("stocks") or [])),
            visibility_has_price=vd.get("has_price"), visibility_has_stock=vd.get("has_stock"),
            status_name=(i.get("statuses") or {}).get("status_name"),
            price_index_color=(pi.get("color_index") or "").replace("COLOR_INDEX_", "") or None,
            description_category_id=i.get("description_category_id"), type_id=i.get("type_id"),
            created_at=i.get("created_at"), updated_at=i.get("updated_at"),
            source_event_time=i.get("updated_at"), source_payload_hash=h(d, i["sku"]),
            **_meta("POST /v3/product/list + /v3/product/info/list", run_id, ts)))
    return rows


def catalog(run_id, ts, _f, _t):
    ids = [i["product_id"] for i in _product_list_items()]
    rows = _catalog_rows(_product_info_items(ids), run_id, ts)
    return merge_rows("RAW_OZON_CATALOG", rows, ["snapshot_date", "sku"], run_id)


# ------------------------------------------------------------------ цены
# Карта блока commissions: ключ API → (схема продажи, компонента, единица).
# Всё, чего здесь НЕТ, всё равно сохраняется в RAW_OZON_PRICE_COMMISSIONS со
# схемой UNKNOWN и is_known_component=FALSE. Новый тариф Ozon не должен
# раствориться молча: витрина форвардной экономики закрывается по этому флагу.
COMMISSION_MAP = {
    "sales_percent_fbo":               ("FBO",  "SALES_PERCENT",          "PERCENT"),
    "sales_percent_fbs":               ("FBS",  "SALES_PERCENT",          "PERCENT"),
    "sales_percent_rfbs":              ("RFBS", "SALES_PERCENT",          "PERCENT"),
    "sales_percent_fbp":               ("FBP",  "SALES_PERCENT",          "PERCENT"),
    "sales_percent":                   ("LEGACY", "SALES_PERCENT",        "PERCENT"),
    "fbo_direct_flow_trans_min_amount": ("FBO", "DIRECT_FLOW_TRANS_MIN",  "RUB"),
    "fbo_direct_flow_trans_max_amount": ("FBO", "DIRECT_FLOW_TRANS_MAX",  "RUB"),
    "fbo_deliv_to_customer_amount":     ("FBO", "DELIV_TO_CUSTOMER",      "RUB"),
    "fbo_return_flow_amount":           ("FBO", "RETURN_FLOW",            "RUB"),
    "fbo_fulfillment_amount":           ("FBO", "FULFILLMENT",            "RUB"),
    "fbs_direct_flow_trans_min_amount": ("FBS", "DIRECT_FLOW_TRANS_MIN",  "RUB"),
    "fbs_direct_flow_trans_max_amount": ("FBS", "DIRECT_FLOW_TRANS_MAX",  "RUB"),
    "fbs_deliv_to_customer_amount":     ("FBS", "DELIV_TO_CUSTOMER",      "RUB"),
    "fbs_return_flow_amount":           ("FBS", "RETURN_FLOW",            "RUB"),
    "fbs_first_mile_min_amount":        ("FBS", "FIRST_MILE_MIN",         "RUB"),
    "fbs_first_mile_max_amount":        ("FBS", "FIRST_MILE_MAX",         "RUB"),
}


def prices(run_id, ts, _f, _t):
    d = str(now_msk().date())
    rows, comm_rows, cursor = [], [], ""
    cursors, page, total = set(), 0, None
    while True:
        page += 1
        if page > PRICES_MAX_PAGES:
            raise PaginationError(f"prices: достигнут потолок {PRICES_MAX_PAGES} страниц")
        code, r = seller_post("/v5/product/info/prices",
                              {"cursor": cursor, "limit": 100,
                               "filter": {"offer_id": [], "product_id": [], "visibility": "ALL"}})
        if code != 200:
            raise RuntimeError(f"prices {code}: {r}")
        items = r.get("items") or []
        for i in items:
            p = i.get("price") or {}
            pi = i.get("price_indexes") or {}
            ex = pi.get("external_index_data") or {}
            oz = pi.get("ozon_index_data") or {}
            sm = pi.get("self_marketplaces_index_data") or {}
            c = i.get("commissions") or {}
            offer = str(i["offer_id"])
            unknown = sorted(k for k in c if k not in COMMISSION_MAP)
            rows.append(dict(snapshot_ts=ts, snapshot_date=d, offer_id=offer,
                product_id=str(i.get("product_id")), price_rub=_num(p.get("price")),
                old_price_rub=_num(p.get("old_price")), min_price_rub=_num(p.get("min_price")),
                marketing_seller_price_rub=_num(p.get("marketing_seller_price")),
                net_price_rub=_num(p.get("net_price")), acquiring_rub=_num(i.get("acquiring")),
                sales_percent_fbo=_num(c.get("sales_percent_fbo")),
                price_index_color=pi.get("color_index"),
                external_min_price_rub=_num(ex.get("min_price")),
                external_index_value=_num(ex.get("price_index_value")),
                ozon_actions_exist=(i.get("marketing_actions") or {}).get("ozon_actions_exist"),
                # --- расширение Stage 3.4D.2: полный текущий тарифный контракт ---
                currency_code=p.get("currency_code"),
                retail_price_rub=_num(p.get("retail_price")), vat_rate=_num(p.get("vat")),
                auto_action_enabled=p.get("auto_action_enabled"),
                auto_add_to_ozon_actions_enabled=p.get("auto_add_to_ozon_actions_list_enabled"),
                volume_weight_l=_num(i.get("volume_weight")),
                sales_percent_fbs=_num(c.get("sales_percent_fbs")),
                sales_percent_rfbs=_num(c.get("sales_percent_rfbs")),
                sales_percent_fbp=_num(c.get("sales_percent_fbp")),
                fbo_direct_flow_trans_min_rub=_num(c.get("fbo_direct_flow_trans_min_amount")),
                fbo_direct_flow_trans_max_rub=_num(c.get("fbo_direct_flow_trans_max_amount")),
                fbo_deliv_to_customer_rub=_num(c.get("fbo_deliv_to_customer_amount")),
                fbo_return_flow_rub=_num(c.get("fbo_return_flow_amount")),
                fbs_first_mile_min_rub=_num(c.get("fbs_first_mile_min_amount")),
                fbs_first_mile_max_rub=_num(c.get("fbs_first_mile_max_amount")),
                fbs_direct_flow_trans_min_rub=_num(c.get("fbs_direct_flow_trans_min_amount")),
                fbs_direct_flow_trans_max_rub=_num(c.get("fbs_direct_flow_trans_max_amount")),
                fbs_deliv_to_customer_rub=_num(c.get("fbs_deliv_to_customer_amount")),
                fbs_return_flow_rub=_num(c.get("fbs_return_flow_amount")),
                ozon_index_min_price_rub=_num(oz.get("min_price")),
                ozon_index_value=_num(oz.get("price_index_value")),
                self_marketplaces_index_min_price_rub=_num(sm.get("min_price")),
                self_marketplaces_index_value=_num(sm.get("price_index_value")),
                commissions_json=json.dumps(c, ensure_ascii=False, sort_keys=True),
                commissions_field_count=len(c),
                commissions_unknown_fields=(",".join(unknown) or None),
                source_payload_hash=h(d, offer),
                **_meta("POST /v5/product/info/prices", run_id, ts)))
            # длинная проекция: одна строка на компоненту тарифа
            for k, v in sorted(c.items()):
                scheme, comp, unit = COMMISSION_MAP.get(k, ("UNKNOWN", k, "UNKNOWN"))
                comm_rows.append(dict(snapshot_ts=ts, snapshot_date=d, offer_id=offer,
                    product_id=str(i.get("product_id")), sale_scheme=scheme,
                    commission_component=comp, api_field=k, value_num=_num(v), unit=unit,
                    is_known_component=(k in COMMISSION_MAP),
                    source_payload_hash=h(d, offer, k),
                    **_meta("POST /v5/product/info/prices", run_id, ts)))
            comm_rows.append(dict(snapshot_ts=ts, snapshot_date=d, offer_id=offer,
                product_id=str(i.get("product_id")), sale_scheme="COMMON",
                commission_component="ACQUIRING", api_field="acquiring",
                value_num=_num(i.get("acquiring")), unit="RUB", is_known_component=True,
                source_payload_hash=h(d, offer, "acquiring"),
                **_meta("POST /v5/product/info/prices", run_id, ts)))
        total = _list_total(r) if total is None else total
        cursor = r.get("cursor") or ""
        if not cursor or not items:
            break
        if cursor in cursors:
            raise PaginationError(f"prices: cursor повторился на странице {page}")
        cursors.add(cursor)
    if C.STRICT_PAGE_CAPS:
        offers = len({x["offer_id"] for x in rows})
        if total is None or offers != total:
            _strict_cap("prices", f"получено {offers} товаров из total={total}",
                        "повторить прогон; снимок цен неполный", pages=page)
    # логический ключ снимка — дата, а не момент: повтор в тот же день не плодит строк
    r1 = merge_rows("RAW_OZON_PRICES", rows, ["snapshot_date", "offer_id"], run_id)
    r2 = merge_rows("RAW_OZON_PRICE_COMMISSIONS", comm_rows,
                    ["snapshot_date", "offer_id", "sale_scheme", "commission_component"], run_id)
    return {k: r1[k] + r2[k] for k in ("received", "inserted", "updated")}


# ------------------------------------------------- состояние продавца
def seller_info(run_id, ts, _f, _t):
    """Статус подписки и налоговый режим продавца. Только чтение.

    Зачем отдельная сущность. Premium — постоянный расход уровня магазина
    (9 990 ₽/мес). До этой сущности его статус проверялся руками и был
    зашит в витрину константой FALSE: если бы подписку возобновили,
    форвардный слой этого не заметил бы. Теперь статус приходит каждые
    сутки и имеет возраст, который можно проверить.

    Идентификаторы компании (ИНН, ОГРН, юридическое и торговое название)
    НЕ сохраняются: для экономики они не нужны, а хранить их без нужды
    незачем. Остаются только налоговый режим, страна и валюта.
    """
    d = str(now_msk().date())
    code, si = seller_post("/v1/seller/info", {})
    if code != 200:
        raise RuntimeError(f"seller/info {code}: {si}")
    code, rs = seller_post("/v1/rating/summary", {})
    if code != 200:
        raise RuntimeError(f"rating/summary {code}: {rs}")

    comp = {k: v for k, v in (si.get("company") or {}).items()
            if k in ("country", "currency", "tax_system")}
    sub = si.get("subscription") or {}
    loc = (rs.get("localization_index") or [{}])
    loc = loc[0] if isinstance(loc, list) and loc else (loc if isinstance(loc, dict) else {})
    raw = {"seller_info": {"company": comp, "subscription": sub},
           "rating_summary": {k: rs.get(k) for k in
                              ("premium", "premium_plus", "penalty_score_exceeded")},
           "localization_index": loc}

    row = dict(snapshot_date=d, retrieved_at=ts, seller_id=None,
        is_premium=sub.get("is_premium"), subscription_type=sub.get("type"),
        premium=rs.get("premium"), premium_plus=rs.get("premium_plus"),
        penalty_score_exceeded=rs.get("penalty_score_exceeded"),
        tax_system=comp.get("tax_system"), country=comp.get("country"),
        currency=comp.get("currency"), vat_rate=None,
        localization_percentage=_num(loc.get("localization_percentage")),
        localization_calculated_at=loc.get("calculation_date"),
        raw_json=json.dumps(raw, ensure_ascii=False, sort_keys=True),
        source_payload_hash=h(d, "seller_info"),
        **_meta("POST /v1/seller/info + POST /v1/rating/summary", run_id, ts))
    # один снимок в сутки: повтор в тот же день перезаписывает, а не плодит
    return merge_rows("RAW_OZON_SELLER_INFO", [row], ["snapshot_date"], run_id)


# --------------------------------------------------------------- остатки
def stocks(run_id, ts, _f, _t):
    # Раньше код ответа product/list здесь не проверялся: при ошибке уходил пустой
    # фильтр skus. Теперь та же проверка кода и полноты, что в catalog().
    skus = [str(i["sku"]) for i in _product_list_items()]
    items = []
    # /v1/analytics/stocks: skus maximum 100 (Swagger 2026-09-28) — партиями по 100.
    for k in range(0, max(len(skus), 1), STOCKS_SKU_BATCH):
        code, r = seller_post("/v1/analytics/stocks", {"skus": skus[k:k + STOCKS_SKU_BATCH]})
        if code != 200:
            raise RuntimeError(f"stocks {code}: {r}")
        items += r.get("items") or []
    d = str(now_msk().date())
    rows = [dict(snapshot_date=d, sku=str(i["sku"]), warehouse_id=str(i.get("warehouse_id")),
        warehouse_name=i.get("warehouse_name"), cluster_id=str(i.get("cluster_id")),
        cluster_name=i.get("cluster_name"),
        available_stock_count=i.get("available_stock_count"),
        valid_stock_count=i.get("valid_stock_count"),
        transit_stock_count=i.get("transit_stock_count"),
        excess_stock_count=i.get("excess_stock_count"),
        days_without_sales=i.get("days_without_sales"),
        turnover_grade=i.get("turnover_grade"), idc=_num(i.get("idc")),
        source_payload_hash=h(d, i["sku"], i.get("warehouse_id")),
        **_meta("POST /v1/analytics/stocks", run_id, ts))
        for i in items]
    return merge_rows("RAW_OZON_STOCKS", rows,
                      ["snapshot_date", "sku", "warehouse_id"], run_id)


# ---------------------------------------------------------- заказы FBO
def _fbo_rows(postings, run_id, ts):
    rows = []
    for p in postings:
        ad = p.get("analytics_data") or {}
        fin = {str(x.get("product_id")): x
               for x in ((p.get("financial_data") or {}).get("products") or [])}
        for pr in (p.get("products") or []):
            sku = str(pr.get("sku"))
            fd = fin.get(sku, {})
            rows.append(dict(posting_number=p["posting_number"], sku=sku,
                order_date=p["created_at"][:10], order_id=p.get("order_id"),
                order_number=p.get("order_number"), status=p.get("status"),
                substatus=p.get("substatus"), created_at=p.get("created_at"),
                in_process_at=p.get("in_process_at"),
                cancel_reason_id=p.get("cancel_reason_id"), quantity=pr.get("quantity"),
                price_rub=_num(fd.get("price") if fd.get("price") is not None else pr.get("price")),
                old_price_rub=_num(fd.get("old_price")),
                total_discount_value_rub=_num(fd.get("total_discount_value")),
                commission_amount_rub=_num(fd.get("commission_amount")),
                payout_rub=_num(fd.get("payout")), actions=fd.get("actions") or [],
                warehouse_name=ad.get("warehouse_name"),
                warehouse_id=str(ad["warehouse_id"]) if ad.get("warehouse_id") else None,
                city=ad.get("city"), source_event_time=p.get("created_at"),
                source_payload_hash=h(p["posting_number"], sku),
                **_meta("POST /v3/posting/fbo/list", run_id, ts)))
    return rows


def fbo_postings(run_id, ts, frm, to):
    rows, cursor, page = [], "", 0
    # Строгий режим: конец суток включительно до миллисекунды — соседние окна бэкфилла
    # не оставляют секундного разрыва; окно не длиннее года (иначе PERIOD_IS_TOO_LONG).
    end = f"{to}T23:59:59.999Z" if C.STRICT_PAGE_CAPS else f"{to}T23:59:59.000Z"
    if C.STRICT_PAGE_CAPS:
        span = (date.fromisoformat(str(to)) - date.fromisoformat(str(frm))).days + 1
        if span > FBO_MAX_PERIOD_DAYS:
            _strict_cap("fbo_postings", f"окно {span} сут. длиннее {FBO_MAX_PERIOD_DAYS}",
                        "бэкфилл режет окно на части", window=f"{frm}..{to}")
    while True:
        page += 1
        code, d = seller_post("/v3/posting/fbo/list", {
            "cursor": cursor,
            "filter": {"since": f"{frm}T00:00:00.000Z", "to": end},
            "limit": 100, "with": {"analytics_data": True, "financial_data": True}})
        if code != 200:
            raise RuntimeError(f"posting/fbo/list {code}: {d}")
        rows.extend(_fbo_rows(d.get("postings") or [], run_id, ts))
        cursor = d.get("cursor") or ""
        if not d.get("has_next") or not cursor:
            break
        if page > FBO_POSTINGS_MAX_PAGES:
            # has_next=true и курсор есть: источник говорит, что данные ещё есть.
            _strict_cap("fbo_postings",
                        f"упор в потолок {FBO_POSTINGS_MAX_PAGES} страниц при has_next=true",
                        "сузить окно SINCE/UNTIL и загрузить его частями",
                        window=f"{frm}..{to}", pages=page, rows=len(rows))
            break
        time.sleep(1)
    return merge_rows("RAW_OZON_POSTINGS_FBO", rows, ["posting_number", "sku"], run_id)


# ------------------------------------------------------------- финансы
def _finance_rows(chunk, types, ds, run_id, ts):
    rows = []
    for a in chunk:
        base = dict(event_date=ds, accrual_id=a["accrual_id"],
                    accrued_category=a["accrued_category"],
                    unit_number=str(a.get("unit_number")),
                    currency=(a.get("total_amount") or {}).get("currency", "RUB"),
                    **_meta("POST /v1/finance/accrual/by-day", run_id, ts))

        def emit(tid, amt, sku=None, posting=None, econ=None):
            e = econ or {}
            rows.append(dict(base, type_id=tid,
                operation_name=types.get(tid, "UNKNOWN"),
                unit_number_meaning=("campaign_id" if tid in (41, 54) else
                                     ("posting_number" if a["accrued_category"] == "POSTING"
                                      else "order_or_item_ref")),
                posting_number=posting, sku=sku, amount_rub=_num(amt),
                seller_base_price_rub=e.get("sp"), buyer_paid_price_rub=e.get("bp"),
                ozon_bonus_rub=e.get("bn"), ozon_coinvestment_rub=e.get("co"),
                commission_rub=e.get("cm"), commission_ratio=e.get("cr"),
                quantity=None,
                source_payload_hash=h(a["accrual_id"], tid, sku, amt)))

        nf = a.get("non_item_fee")
        if nf:
            emit(nf["type_id"], nf["accrued"]["amount"])
        for fee in ((a.get("item_fees") or {}).get("fees") or []):
            for x in (fee.get("fees") or []):
                emit(x["type_id"], x["accrued"]["amount"], sku=str(fee.get("sku")))
        p = a.get("posting") or {}
        pn = p.get("posting_number") or (a.get("unit_number")
                                         if a["accrued_category"] == "POSTING" else None)
        for pr in (p.get("products") or []):
            sku = str(pr.get("sku"))
            c = pr.get("commission") or {}
            econ = None
            if c:
                g = lambda k: _num((c.get(k) or {}).get("amount"))
                m = re.search(r"([\d.]+)", str(c.get("commission_ratio") or ""))
                econ = {"sp": g("seller_price"), "bp": g("sale_price"),
                        "bn": g("bonus"), "co": g("coinvestment"),
                        "cm": g("commission"),
                        "cr": float(m.group(1)) if m else None}
            first = True
            for _k, blk in pr.items():
                if isinstance(blk, dict) and blk.get("services"):
                    for s in blk["services"]:
                        # экономический блок продукта несётся ровно один раз
                        emit(s["type_id"], s["accrued"]["amount"], sku=sku,
                             posting=pn, econ=econ if first else None)
                        first = False
    return rows


def finance_accrual(run_id, ts, frm, to):
    types = {}
    code, t = seller_post("/v1/finance/accrual/types", {})
    if code == 200:
        types = {x["id"]: x["description"] for x in (t.get("accrual_types") or [])}
    else:
        _strict_cap("finance_accrual", "справочник типов начислений не получен — все типы "
                    "стали бы UNKNOWN", "повторить прогон", http=code)
    rows = []
    d0, d1 = date.fromisoformat(str(frm)), date.fromisoformat(str(to))
    cur = d0
    while cur <= d1:
        ds, last, page = cur.isoformat(), "", 0
        while True:
            page += 1
            code, r = seller_post("/v1/finance/accrual/by-day", {"date": ds, "last_id": last})
            if code != 200:
                raise RuntimeError(f"accrual/by-day {ds} {code}: {r}")
            chunk = r.get("accruals") or []
            rows.extend(_finance_rows(chunk, types, ds, run_id, ts))
            last = r.get("last_id") or ""
            if not last or not chunk:
                break
            if page > FINANCE_ACCRUAL_MAX_PAGES_PER_DAY:
                # last_id непуст и страница непуста: начисления за сутки ещё есть.
                _strict_cap("finance_accrual",
                            f"упор в потолок {FINANCE_ACCRUAL_MAX_PAGES_PER_DAY} страниц "
                            "за одни сутки при непустом last_id",
                            "поднять потолок после проверки объёма суток; окно уже "
                            "идёт по одному дню, сузить его нельзя",
                            day=ds, pages=page, rows_so_far=len(rows))
                break
            time.sleep(1)
        cur += timedelta(days=1)
        time.sleep(1)
    # Грейн не меняется. Аудит 2026-09-16: 1 702 строки за 92 дня, 0 повторов ключа.
    # reject: любой повтор ключа — отказ загрузки, а не схлопывание. Две одинаковые
    # строки здесь могут быть двумя реальными начислениями на одну сумму.
    return merge_rows("RAW_OZON_FINANCE_ACCRUAL", rows,
                      ["accrual_id", "type_id", "sku"], run_id, on_duplicate_key="reject")


# ------------------------------------------------------- реклама: кампании
def _campaign_rows(items, run_id, ts):
    d = str(now_msk().date())
    rows = []
    for c in items:
        rows.append(dict(snapshot_date=d, campaign_id=c["id"], title=c.get("title") or None,
            state=c.get("state", "").replace("CAMPAIGN_STATE_", ""),
            adv_object_type=c.get("advObjectType"),
            payment_type=c.get("PaymentType", "").replace("CAMPAIGN_TYPE_", ""),
            expense_strategy=c.get("expenseStrategy", "").replace("EXPENSE_STRATEGY_", ""),
            placement="|".join(c.get("placement") or []) or None,
            weekly_budget_rub=int(c.get("weeklyBudget") or 0) / 1e6,
            daily_budget_rub=int(c.get("dailyBudget") or 0) / 1e6,
            product_autopilot_strategy=c.get("productAutopilotStrategy"),
            autostop_status=c.get("autostopStatus", "").replace("AUTOSTOP_STATUS_", ""),
            campaign_created_at=c.get("createdAt"), campaign_updated_at=c.get("updatedAt"),
            source_payload_hash=h(d, c["id"]),
            **_meta("GET /api/client/campaign", run_id, ts)))
    return rows


def ads_campaigns(run_id, ts, _f, _t):
    code, txt = perf_get("/api/client/campaign")
    if code != 200:
        raise RuntimeError(f"campaign {code}")
    rows = _campaign_rows(_campaign_list(txt), run_id, ts)
    return merge_rows("RAW_OZON_ADS_CAMPAIGNS", rows,
                      ["snapshot_date", "campaign_id"], run_id)


def _csv_rows(text):
    return list(csv.DictReader(io.StringIO(text.lstrip("﻿")), delimiter=";"))


def _rub(v):
    """Число из CSV Performance API. Прочерк — «не определено», а не ноль.

    В исторических отчётах ДРР и «Заказано на сумму» приходят как "-", когда
    показатель не определён. Возвращаем None: ноль исказил бы смысл.
    """
    t = (v or "0").replace(",", ".").replace("\xa0", "").replace(" ", "") or "0"
    try:
        return float(t)
    except ValueError:
        return None


def ads_expense_daily(run_id, ts, frm, to):
    rows = []
    d0, d1 = date.fromisoformat(str(frm)), date.fromisoformat(str(to))
    cur = d0
    while cur <= d1:
        ds = cur.isoformat()
        code, txt = perf_get(f"/api/client/statistics/expense?dateFrom={ds}&dateTo={ds}")
        if code != 200:
            raise RuntimeError(f"expense {ds} {code}")
        stats = {}
        code2, t2 = perf_get(f"/api/client/statistics/daily?dateFrom={ds}&dateTo={ds}")
        if code2 == 200:
            for r in _csv_rows(t2):
                if r.get("ID"):
                    stats[r["ID"]] = r
        else:
            # Раньше показы/клики/заказы молча становились NULL при статусе OK.
            _strict_cap("ads_expense_daily", "суточная статистика не получена — показатели "
                        "стали бы NULL", "повторить прогон", http=code2, day=ds)
        for r in _csv_rows(txt):
            if not r.get("ID"):
                continue
            s = stats.get(r["ID"], {})
            rows.append(dict(date=ds, campaign_id=r["ID"], campaign_title=r.get("Название"),
                expense_rub=_rub(r.get("Расход")),
                bonus_expense_rub=_rub(r.get("Расход бонусов")),
                subscription_expense_rub=_rub(r.get("Расход с абонентского счета")),
                impressions=int(_rub(s.get("Показы"))) if s else None,
                clicks=int(_rub(s.get("Клики"))) if s else None,
                orders=int(_rub(s.get("Заказы, шт."))) if s else None,
                revenue_rub=_rub(s.get("Заказы, ₽")) if s else None,
                source_payload_hash=h(ds, r["ID"]),
                **_meta("GET /api/client/statistics/expense + /daily", run_id, ts)))
        cur += timedelta(days=1)
        time.sleep(1.5)
    return merge_rows("RAW_OZON_ADS_EXPENSE_DAILY", rows, ["date", "campaign_id"], run_id)


def _date_chunks(d0, d1, days):
    """Сплошное покрытие [d0, d1] отрезками по датам не длиннее `days` суток включительно."""
    cur = d0
    while cur <= d1:
        end = min(cur + timedelta(days=days - 1), d1)
        yield cur, end
        cur = end + timedelta(days=1)


_CAMPAIGN_FILE_RE = re.compile(r"^(\d+)")


def _sku_report_files(blob, batch):
    """(campaign_id, csv) из ответа отчёта: ZIP — по файлу на кампанию, CSV — одна кампания.

    Документация /api/client/statistics/report: одна кампания в запросе — CSV, несколько —
    ZIP-архив «<идентификатор кампании>.csv» (TECH_DEBT P2-6). Кампания файла обязана быть
    из партии; всё остальное — StrictLimitError.
    """
    raw = blob.encode("utf-8", "surrogateescape")
    if raw[:2] == b"PK":
        z = zipfile.ZipFile(io.BytesIO(raw))
        out = []
        for name in z.namelist():
            m = _CAMPAIGN_FILE_RE.match(name)
            if not m or m.group(1) not in batch:
                _strict_cap("ads_sku_daily", "файл отчёта не относится к кампании партии",
                            "проверить формат отчёта", file_count=len(z.namelist()))
            out.append((m.group(1), z.read(name).decode("utf-8-sig")))
        return out
    if len(batch) == 1:
        return [(batch[0], raw.decode("utf-8-sig"))]
    _strict_cap("ads_sku_daily", "отчёт по нескольким кампаниям пришёл не ZIP-архивом",
                "проверить формат отчёта", batch_size=len(batch))
    return []


def _sku_rows_from_csv(cid, text, run_id, ts):
    rows = []
    lines = text.splitlines()
    for r in _csv_rows("\n".join(lines[1:])):
        sku = (r.get("sku") or "").strip()
        if not sku:
            continue
        dd = r["День"]
        iso = f"{dd[6:10]}-{dd[3:5]}-{dd[0:2]}"
        rows.append(dict(date=iso, campaign_id=cid, sku=sku,
            attributed_spend_rub=_rub(r.get("Расход, ₽, с НДС")),
            impressions=int(_rub(r.get("Показы")) or 0),
            clicks=int(_rub(r.get("Клики")) or 0),
            cart_adds=int(_rub(r.get("Добавления в корзину")) or 0),
            orders=int(_rub(r.get("Продано товаров")) or 0),
            revenue_promo_rub=_rub(r.get("Продажи в продвижении, ₽")),
            ordered_total_rub=_rub(r.get("Заказано на сумму, ₽")),
            drr_promo_pct=_rub(r.get("ДРР в продвижении, %")),
            drr_total_pct=_rub(r.get("ДРР (общий), %")),
            attribution_status="ATTRIBUTED_ACTUAL",
            source_payload_hash=h(iso, cid, sku),
            **_meta("POST /api/client/statistics (async report)", run_id, ts)))
    return rows


# Кампании, по которым асинхронный отчёт отдаёт строки SKU: «Оплата за клик» (advObjectType SKU).
# Замер EVETIS 2026-09-28: у SKU/CPC 2191 из 2191 суток с расходом есть строки SKU; у SEARCH_PROMO и
# ALL_SKU_PROMO (оплата за заказ) — 0 из 12 пар «кампания × месяц»: отчёт по SKU для них пуст всегда.
SKU_REPORT_ADV_TYPES = frozenset({"SKU"})
NO_SKU_REPORT_ADV_TYPES = frozenset({"SEARCH_PROMO", "ALL_SKU_PROMO", "BANNER", "VIDEO_BANNER"})
EXPENSE_CSV_REQUIRED = ("ID", "Расход")


def _expense_by_campaign(day):
    """Расход кампаний за одни сутки МСК: {campaign_id: рубли}. Заголовок CSV проверяется."""
    code, txt = perf_get(f"/api/client/statistics/expense?dateFrom={day}&dateTo={day}")
    if code != 200:
        _strict_cap("ads_sku_daily", "расход кампаний за сутки не получен", "повторить прогон",
                    http=code, day=str(day))
    rows = _csv_rows(txt)
    header = list(rows[0].keys()) if rows else (txt.lstrip("\ufeff").splitlines() or [""])[0].split(";")
    missing = [h for h in EXPENSE_CSV_REQUIRED if h not in header]
    if missing:
        _strict_cap("ads_sku_daily", f"в CSV расхода нет колонок {missing}", "проверить формат отчёта",
                    day=str(day))
    out = {}
    for r in rows:
        if r.get("ID"):
            v = _rub(r.get("Расход"))
            if v is None:
                _strict_cap("ads_sku_daily", "расход кампании не число", "проверить формат отчёта",
                            day=str(day))
            out[r["ID"]] = out.get(r["ID"], 0.0) + v
    return out


def _ads_sku_rows_strict(run_id, ts, d0, d1):
    """Строгий путь (T5): окна по датам МСК, CSV одной кампании, посуточная сверка с расходом.

    * Окно режется на отрезки ≤ ADS_SKU_STRICT_CHUNK_DAYS суток и запрашивается полями
      dateFrom/dateTo (ГГГГ-ММ-ДД). Даты отчётов Performance API группируются по Москве
      (Swagger, раздел «Статистика»), поэтому отрезки по датам стыкуются без разрыва и без
      двойного учёта суток — в отличие от моментов from/to (TECH_DEBT P2-5).
    * Периметр отчёта — кампании «Оплата за клик» (SKU_REPORT_ADV_TYPES) с ненулевым расходом.
      Оплата за заказ и баннеры отчёта по SKU не дают — они не заказываются и помечаются в
      журнале; кампания с расходом и неизвестным типом — отказ.
    * Для каждой пары «кампания CPC × сутки» с ненулевым расходом (/statistics/expense по
      суткам) обязаны быть строки SKU за эти сутки: иначе отказ. Этим ловится «ноль строк при
      HTTP 200» (окно > 62 дней, иной формат, потерянный CSV одной кампании).
    * Сумма SKU-расхода с расходом кампании не сверяется: у EVETIS она сходится в пределах 1 %
      лишь в 95 из 148 пар — это предупреждение DQ, а не отказ загрузки.
    * Строка с датой вне отрезка или кампанией вне партии — отказ.
    """
    code, txt = perf_get("/api/client/campaign")
    if code != 200:
        _strict_cap("ads_sku_daily", "список кампаний не получен", "повторить прогон", http=code)
    types = {str(c["id"]): c.get("advObjectType") for c in _campaign_list(txt)}
    rows = []
    for c0, c1 in _date_chunks(d0, d1, ADS_SKU_STRICT_CHUNK_DAYS):
        need = set()                         # (campaign_id, дата) с ненулевым расходом
        skipped = {}                         # campaign_id → расход, ₽
        for k in range((c1 - c0).days + 1):
            day = c0 + timedelta(days=k)
            for cid, v in _expense_by_campaign(day).items():
                if v == 0:
                    continue
                t = types.get(cid)
                if cid not in types:
                    _strict_cap("ads_sku_daily", "расход у кампании вне реестра кампаний",
                                "проверить /api/client/campaign", window=f"{c0}..{c1}")
                if t in SKU_REPORT_ADV_TYPES:
                    need.add((cid, str(day)))
                elif t in NO_SKU_REPORT_ADV_TYPES:
                    skipped[cid] = skipped.get(cid, 0.0) + v
                else:
                    _strict_cap("ads_sku_daily", f"кампания с расходом неизвестного типа {t!r}",
                                "добавить тип в SKU_REPORT_ADV_TYPES или NO_SKU_REPORT_ADV_TYPES",
                                window=f"{c0}..{c1}")
            time.sleep(0.5)
        if skipped:
            # Расход таких кампаний остаётся в ads_expense_daily на уровне кампании; SUM по SKU
            # не равна всему расходу на рекламу — это видно в каждом прогоне.
            log(event="ads_sku_not_applicable", window=f"{c0}..{c1}", campaigns=sorted(skipped),
                spend_rub=round(sum(skipped.values()), 2),
                reason="оплата за заказ / баннеры: отчёт по SKU не формируется")
        active = sorted({cid for cid, _d in need})
        seen = set()
        for i in range(0, len(active), ADS_SKU_BATCH):
            batch = active[i:i + ADS_SKU_BATCH]
            code, sub = perf_post("/api/client/statistics",
                                  {"campaigns": batch, "dateFrom": str(c0), "dateTo": str(c1),
                                   "groupBy": "DATE"})
            uuid = (sub or {}).get("UUID")
            if not uuid:
                _strict_cap("ads_sku_daily", "отчёт не заказан: в ответе нет UUID", "повторить прогон",
                            http=code, batch_size=len(batch), window=f"{c0}..{c1}")
            state = None
            for _ in range(ADS_REPORT_POLL_ATTEMPTS):
                time.sleep(10)
                c3, st = perf_get(f"/api/client/statistics/{uuid}", raw_text=False)
                state = st.get("state") if c3 == 200 else None
                if state in ("OK", "ERROR"):
                    break
            if state != "OK":
                _strict_cap("ads_sku_daily", "отчёт не готов или завершился ошибкой", "повторить прогон",
                            state=state, batch_size=len(batch), window=f"{c0}..{c1}")
            c4, blob = perf_get(f"/api/client/statistics/report?UUID={uuid}", raw_text=True)
            if c4 != 200:
                _strict_cap("ads_sku_daily", "отчёт не скачан", "повторить прогон",
                            http=c4, batch_size=len(batch), window=f"{c0}..{c1}")
            for cid, text in _sku_report_files(blob, batch):
                got = _sku_rows_from_csv(cid, text, run_id, ts)
                bad = [r for r in got if not (str(c0) <= r["date"] <= str(c1))]
                if bad:
                    _strict_cap("ads_sku_daily", f"{len(bad)} строк SKU с датой вне окна",
                                "проверить границы dateFrom/dateTo", window=f"{c0}..{c1}")
                seen.update((r["campaign_id"], r["date"]) for r in got)
                rows += got
            time.sleep(3)
        missing = need - seen
        if missing:
            _strict_cap("ads_sku_daily", f"{len(missing)} пар «кампания × сутки» с расходом без строк SKU",
                        "сузить окно; проверить формат отчёта", window=f"{c0}..{c1}",
                        campaigns_with_spend=len(active))
    return rows


def ads_sku_daily(run_id, ts, frm, to):
    """Асинхронный отчёт — единственный доказанный источник расхода по SKU.

    Периметр берётся из ВСЕХ кампаний с активностью в окне, а не только RUNNING.
    Строгий режим (арендатор, T5) — отдельный путь _ads_sku_daily_strict; ниже — прежний.
    """
    d0, d1 = date.fromisoformat(str(frm)), date.fromisoformat(str(to))
    rows = (_ads_sku_rows_strict(run_id, ts, d0, d1) if C.STRICT_PAGE_CAPS
            else _ads_sku_rows_legacy(run_id, ts, d0, d1))
    return merge_rows("RAW_OZON_ADS_SKU_DAILY", rows, ["date", "campaign_id", "sku"], run_id)


def _ads_sku_rows_legacy(run_id, ts, d0, d1):
    """Прежний путь EVETIS — без изменений поведения (дифференциальный тест T2)."""
    # Окно отчёта — от d0T00:00Z до d1T00:00Z, то есть (d1 - d0) суток. Длиннее
    # PERFORMANCE_REPORT_MAX_DAYS Ozon молча отдаёт ноль строк. Окно здесь НЕ
    # дробится: границы — UTC-моменты, а сутки отчёта московские, и любое
    # дробление даёт либо разрыв, либо одни сутки в двух частях
    # (docs/architecture/TECH_DEBT.md, P2-5).
    if (d1 - d0).days > PERFORMANCE_REPORT_MAX_DAYS:
        _strict_cap("ads_sku_daily",
                    f"окно {(d1 - d0).days} сут. длиннее {PERFORMANCE_REPORT_MAX_DAYS} сут., "
                    "Performance API вернёт ноль строк",
                    f"запускать окнами не длиннее {PERFORMANCE_REPORT_MAX_DAYS} сут. "
                    "до решения по границам суток (TECH_DEBT P2-5)",
                    window=f"{d0}..{d1}")
    code, txt = perf_get("/api/client/campaign")
    # Ветка code != 200 → пустой периметр оставлена как была (OPEN QUESTION R1).
    if code != 200:
        _strict_cap("ads_sku_daily", "список кампаний не получен, периметр пуст",
                    "повторить прогон; проверить доступ Performance API", http=code)
    ids = [c["id"] for c in _campaign_list(txt)] if code == 200 else []
    # оставляем только кампании с расходом в окне — иначе отчёт не сформируется
    active = set()
    c2, t2 = perf_get(f"/api/client/statistics/expense?dateFrom={d0}&dateTo={d1}")
    if c2 == 200:
        for r in _csv_rows(t2):
            if r.get("ID") and _rub(r.get("Расход")) > 0:
                active.add(r["ID"])
    else:
        _strict_cap("ads_sku_daily", "расход кампаний за окно не получен, периметр пуст",
                    "повторить прогон", http=c2, window=f"{d0}..{d1}")
    ids = [i for i in ids if i in active]
    rows = []
    for i in range(0, len(ids), 10):
        batch = ids[i:i + 10]
        code, sub = perf_post("/api/client/statistics",
                              {"campaigns": batch, "from": f"{d0}T00:00:00Z",
                               "to": f"{d1}T00:00:00Z", "groupBy": "DATE"})
        uuid = (sub or {}).get("UUID")
        if not uuid:
            _strict_cap("ads_sku_daily", "отчёт не заказан: в ответе нет UUID",
                        "повторить прогон", http=code, batch_size=len(batch),
                        window=f"{d0}..{d1}")
            continue
        state = None
        for _ in range(ADS_REPORT_POLL_ATTEMPTS):
            time.sleep(10)
            c3, st = perf_get(f"/api/client/statistics/{uuid}", raw_text=False)
            state = st.get("state") if c3 == 200 else None
            if c3 == 200 and st.get("state") in ("OK", "ERROR"):
                break
        if state != "OK":
            _strict_cap("ads_sku_daily", "отчёт не готов или завершился ошибкой",
                        "повторить прогон", state=state, batch_size=len(batch),
                        window=f"{d0}..{d1}")
        c4, blob = perf_get(f"/api/client/statistics/report?UUID={uuid}", raw_text=True)
        if c4 != 200:
            _strict_cap("ads_sku_daily", "отчёт не скачан", "повторить прогон",
                        http=c4, batch_size=len(batch), window=f"{d0}..{d1}")
            continue
        try:
            z = zipfile.ZipFile(io.BytesIO(blob.encode("utf-8", "surrogateescape")))
            files = [z.read(n).decode("utf-8-sig") for n in z.namelist()]
            names = z.namelist()
        except Exception:
            # Партия из ОДНОЙ кампании приходит text/csv, а не ZIP (документация
            # /api/client/statistics/report). Такая партия сейчас теряется молча —
            # это известный дефект, исправление меняет поведение EVETIS
            # (docs/architecture/TECH_DEBT.md, P2-6).
            _strict_cap("ads_sku_daily", "отчёт не разобран как ZIP-архив",
                        "если в партии одна кампания, ответ — CSV (известный дефект); "
                        "до исправления такая партия не загружается",
                        batch_size=len(batch), window=f"{d0}..{d1}")
            continue
        for name, txt2 in zip(names, files):
            cid = name.split("_")[0]
            lines = txt2.splitlines()
            for r in _csv_rows("\n".join(lines[1:])):
                sku = (r.get("sku") or "").strip()
                if not sku:
                    continue
                dd = r["День"]
                iso = f"{dd[6:10]}-{dd[3:5]}-{dd[0:2]}"
                rows.append(dict(date=iso, campaign_id=cid, sku=sku,
                    attributed_spend_rub=_rub(r.get("Расход, ₽, с НДС")),
                    # счётчики: отсутствие показателя — ноль наблюдений, а не NULL
                    impressions=int(_rub(r.get("Показы")) or 0),
                    clicks=int(_rub(r.get("Клики")) or 0),
                    cart_adds=int(_rub(r.get("Добавления в корзину")) or 0),
                    orders=int(_rub(r.get("Продано товаров")) or 0),
                    revenue_promo_rub=_rub(r.get("Продажи в продвижении, ₽")),
                    ordered_total_rub=_rub(r.get("Заказано на сумму, ₽")),
                    drr_promo_pct=_rub(r.get("ДРР в продвижении, %")),
                    drr_total_pct=_rub(r.get("ДРР (общий), %")),
                    attribution_status="ATTRIBUTED_ACTUAL",
                    source_payload_hash=h(iso, cid, sku),
                    **_meta("POST /api/client/statistics (async report)", run_id, ts)))
        time.sleep(3)
    return rows


# -------------------------------------------------------------- поставки
def clusters(run_id, ts, _f, _t):
    code, r = seller_post("/v1/cluster/list", {"cluster_type": "CLUSTER_TYPE_OZON"})
    if code != 200:
        raise RuntimeError(f"cluster/list {code}: {r}")
    d = str(now_msk().date())
    rows = []
    for c in (r.get("clusters") or []):
        for lc in (c.get("logistic_clusters") or []):
            for w in (lc.get("warehouses") or []):
                rows.append(dict(snapshot_date=d, cluster_id=c["id"], cluster_name=c.get("name"),
                    cluster_type=c.get("type"), macrolocal_cluster_id=c.get("macrolocal_cluster_id"),
                    warehouse_id=str(w["warehouse_id"]), warehouse_name=w.get("name"),
                    warehouse_type=w.get("type"), source_payload_hash=h(d, w["warehouse_id"]),
                    **_meta("POST /v1/cluster/list", run_id, ts)))
    return merge_rows("RAW_OZON_CLUSTERS", rows, ["snapshot_date", "warehouse_id"], run_id)


def _supply_order_ids():
    """Все id заявок на поставку, постранично по last_id.

    Swagger /v3/supply-order/list: last_id в запросе и ответе, limit 1..100,
    has_next НЕТ. Конец — пустой last_id в ответе (ответ 2026-08-31: 65 заявок,
    last_id="") или пустая страница. Первый запрос — прежний, без last_id;
    фильтр и сортировка не меняются.

    Гарантии завершения: курсор, который уже встречался, — PaginationError;
    упор в SUPPLY_LIST_MAX_PAGES — PaginationError; не-200 на любой странице —
    RuntimeError (раньше ошибка неотличима от пустого списка).
    """
    base = {"filter": {"states": CPC_STATES}, "limit": SUPPLY_LIST_PAGE_LIMIT,
            "sort_by": "ORDER_CREATION", "sort_dir": "DESC"}
    ids, seen_ids, seen_cursors, last, repeated = [], set(), set(), "", 0
    for page in range(1, SUPPLY_LIST_MAX_PAGES + 1):
        body = dict(base, last_id=last) if last else base
        code, r = seller_post("/v3/supply-order/list", body)
        if code != 200:
            raise RuntimeError(f"supply-order/list page {page} {code}: {r}")
        page_ids = r.get("order_ids") or []
        for i in page_ids:
            if i in seen_ids:
                repeated += 1       # тот же объект, повторный get не нужен
            else:
                seen_ids.add(i)
                ids.append(i)
        nxt = r.get("last_id") or ""
        if not page_ids or not nxt:
            log(event="pagination_done", endpoint="/v3/supply-order/list", pages=page,
                ids=len(ids), repeated_ids=repeated)
            return ids
        if nxt in seen_cursors:
            raise PaginationError(
                f"supply-order/list: last_id повторился на странице {page}")
        seen_cursors.add(nxt)
        last = nxt
        time.sleep(1)
    raise PaginationError(
        f"supply-order/list: достигнут потолок {SUPPLY_LIST_MAX_PAGES} страниц")


def _supply_bundle_items(bundle_id):
    """Все позиции одного состава, постранично по last_id + has_next.

    Swagger /v1/supply-order/bundle: limit 1..100, в ответе items, total_count,
    has_next, last_id. Конец — has_next=false. last_id на последней странице
    НЕ пуст (160 ответов из 160 от 2026-08-31), поэтому по нему конец не
    определяется.

    Гарантии: has_next=true без last_id или с повторным last_id —
    PaginationError; потолок страниц — PaginationError; не-200 — RuntimeError.
    total_count проверяется только в одну сторону (позиций меньше, чем total_count
    → PaginationError): это верно при любом прочтении поля.
    """
    items, seen_cursors, last, total = [], set(), "", None
    for page in range(1, BUNDLE_MAX_PAGES + 1):
        body = {"bundle_ids": [bundle_id], "limit": BUNDLE_PAGE_LIMIT}
        if last:
            body["last_id"] = last
        code, d = seller_post("/v1/supply-order/bundle", body)
        if code != 200:
            raise RuntimeError(f"supply-order/bundle page {page} {code}")
        items += d.get("items") or []
        if isinstance(d.get("total_count"), int):
            total = d["total_count"]
        if not d.get("has_next"):
            if total is not None and len(items) < total:
                raise PaginationError(
                    f"supply-order/bundle: получено {len(items)} позиций "
                    f"из total_count={total}")
            return items
        nxt = d.get("last_id") or ""
        if not nxt:
            raise PaginationError(
                f"supply-order/bundle: has_next=true без last_id на странице {page}")
        if nxt in seen_cursors:
            raise PaginationError(
                f"supply-order/bundle: last_id повторился на странице {page}")
        seen_cursors.add(nxt)
        last = nxt
        time.sleep(1)
    raise PaginationError(f"supply-order/bundle: достигнут потолок {BUNDLE_MAX_PAGES} страниц")


def _supply_rows(orders, run_id, ts):
    o_rows, s_rows = [], []
    for o in orders:
        t = o.get("timeslot") or {}
        tsl = t.get("timeslot") or {}
        dw = o.get("drop_off_warehouse") or {}
        tg = o.get("order_tags") or {}
        o_rows.append(dict(order_id=o["order_id"], order_number=o.get("order_number"),
            state=o.get("state"), created_at=o.get("created_date"),
            state_updated_at=o.get("state_updated_date"),
            data_filling_deadline=o.get("data_filling_deadline"),
            planned_arrival_from=tsl.get("from"), planned_arrival_to=tsl.get("to"),
            timeslot_timezone=(t.get("timezone_info") or {}).get("iana_name"),
            dropoff_warehouse_id=str(dw["warehouse_id"]) if dw.get("warehouse_id") else None,
            dropoff_warehouse_name=dw.get("name"), dropoff_address=dw.get("address"),
            is_super_fbo=tg.get("is_super_fbo"), is_virtual=tg.get("is_virtual"),
            is_econom=tg.get("is_econom"), is_pickup=tg.get("is_pickup"),
            supplies_count=len(o.get("supplies") or []),
            source_payload_hash=h(o["order_id"]),
            **_meta("POST /v3/supply-order/get", run_id, ts)))
        for s in (o.get("supplies") or []):
            sw = s.get("storage_warehouse") or {}
            s_rows.append(dict(supply_id=s["supply_id"], order_id=o["order_id"],
                bundle_id=s.get("bundle_id"), state=s.get("state"),
                is_crossdock=s.get("is_crossdock"),
                storage_warehouse_id=str(sw["warehouse_id"]) if sw.get("warehouse_id") else None,
                storage_warehouse_name=sw.get("name"), storage_address=sw.get("address"),
                arrival_date=sw.get("arrival_date"),   # ACTUAL_RECEIPT_TIME = NOT_PROVEN
                macrolocal_cluster_id=s.get("macrolocal_cluster_id"),
                order_created_at=o.get("created_date"), order_state=o.get("state"),
                source_payload_hash=h(o["order_id"], s["supply_id"]),
                **_meta("POST /v3/supply-order/get", run_id, ts)))
    return o_rows, s_rows


def _bundle_rows(bundle_items, bid, oid, sid, run_id, ts):
    rows = []
    for i in bundle_items:
        rows.append(dict(bundle_id=bid, supply_id=sid, order_id=oid,
            sku=str(i["sku"]), offer_id=str(i.get("offer_id") or ""),
            product_name=i.get("name"), quantity_planned=i.get("quantity"),
            quantity_accepted=None,     # ACTUAL_RECEIPT_QUANTITY = NOT_PROVEN
            volume_in_litres=_num(i.get("volume_in_litres")),
            item_tags=json.dumps(i.get("tags"), ensure_ascii=False) if i.get("tags") else None,
            source_payload_hash=h(bid, i["sku"]),
            **_meta("POST /v1/supply-order/bundle", run_id, ts)))
    return rows


def supplies(run_id, ts, _f, _t):
    ids = _supply_order_ids()
    orders = []
    for i in range(0, len(ids), 25):
        c, d = seller_post("/v3/supply-order/get", {"order_ids": ids[i:i + 25]})
        if c != 200:
            # раньше код не проверялся: заявки партии молча выпадали
            raise RuntimeError(f"supply-order/get {c}")
        orders += (d or {}).get("orders") or []
        time.sleep(1)
    o_rows, s_rows = _supply_rows(orders, run_id, ts)
    r1 = merge_rows("RAW_OZON_SUPPLY_ORDERS", o_rows, ["order_id"], run_id)
    r2 = merge_rows("RAW_OZON_SUPPLIES", s_rows, ["order_id", "supply_id"], run_id)
    # составы
    bmap = {(s["bundle_id"], s["order_id"], s["supply_id"])
            for s in s_rows if s.get("bundle_id")}
    b_rows, b_failed = [], []
    for bid, oid, sid in sorted(bmap):
        # Раньше не-200 давал молчаливый `continue`, а позиции сверх первой сотни
        # терялись. Теперь состав читается целиком или не пишется вовсе: сбой
        # одного состава не мешает остальным, но сущность в конце падает.
        try:
            bundle_items = _supply_bundle_items(bid)
        except RuntimeError as e:
            b_failed.append((bid, C.safe_error_text(e, 120)))
            continue
        b_rows.extend(_bundle_rows(bundle_items, bid, oid, sid, run_id, ts))
        time.sleep(1)
    r3 = merge_rows("RAW_OZON_SUPPLY_BUNDLES", b_rows, ["bundle_id", "sku"], run_id)
    if b_failed:
        raise RuntimeError(
            f"supply-order/bundle: не прочитано составов {len(b_failed)} из {len(bmap)}; "
            f"прочитанные записаны; первые: {b_failed[:3]}")
    return {"received": r1["received"] + r2["received"] + r3["received"],
            "inserted": r1["inserted"] + r2["inserted"] + r3["inserted"],
            "updated": r1["updated"] + r2["updated"] + r3["updated"]}


# сущность → (функция, окно ретроспективы в днях, частота)
#
# Окно ретроспективы — СВОЙСТВО ИСТОЧНИКА, а не общая константа: у каждой сущности свой срок
# поздней публикации. Общий LOOKBACK_DAYS здесь заводить нельзя — он связал бы источники,
# у которых нет ничего общего, кроме площадки.
#
# finance_accrual = 30 (Gate 9, было 14). Обоснование: начисление возникает через 4–25 суток
# после заказа, и Ozon публикует его позже даты операции. Прежнее окно 14 суток работало с
# нулевым запасом — один пропущенный суточный прогон сдвинул бы окно и потерял сутки
# начислений безвозвратно. Повторная загрузка того же окна безопасна: запись идёт MERGE по
# ключу (accrual_id, type_id, sku), и проверено на 113 сутках (01.06–21.09.2026): 2300 строк
# перезагружено, вставлено 0, финансовые итоги не изменились ни на копейку.
# Цена вопроса — 30 обращений к API вместо 14 и ~50 с вместо ~25 с на прогон.
REGISTRY = {
    "catalog":           (catalog, 0, "daily"),
    "prices":            (prices, 0, "daily"),
    "seller_info":       (seller_info, 0, "daily"),
    "stocks":            (stocks, 0, "twice_daily"),
    "fbo_postings":      (fbo_postings, 30, "twice_daily"),
    "finance_accrual":   (finance_accrual, 30, "daily"),
    "ads_campaigns":     (ads_campaigns, 0, "daily"),
    "ads_expense_daily": (ads_expense_daily, 7, "daily"),
    "ads_sku_daily":     (ads_sku_daily, 7, "daily"),
    "clusters":          (clusters, 0, "weekly"),
    "supplies":          (supplies, 0, "daily"),
    # PR-PROMO-1: наблюдатель акций. Каденция — 4 раза в сутки, поэтому сущность
    # живёт в отдельном job'е (ozon-runtime-promo), а не в суточном: состав акции
    # и акционные цены меняются внутри суток. Окно ретроспективы 0 — это снимок
    # текущего состояния, у Ozon исторической выгрузки акций нет вовсе.
    "promo":             (promo, 0, "four_times_daily"),
}
