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

from common import (h, log, merge_rows, now_msk, perf_get, perf_post, seller_post)

CPC_STATES = ["DATA_FILLING", "READY_TO_SUPPLY", "ACCEPTED_AT_SUPPLY_WAREHOUSE",
              "IN_TRANSIT", "ACCEPTANCE_AT_STORAGE_WAREHOUSE",
              "REPORTS_CONFIRMATION_AWAITING", "REPORT_REJECTED", "COMPLETED",
              "REJECTED_AT_SUPPLY_WAREHOUSE", "CANCELLED", "OVERDUE"]


def _meta(endpoint, run_id, ts):
    return {"extracted_at": ts, "source_endpoint": endpoint, "ingestion_run_id": run_id}


def _num(v):
    return float(v) if v not in (None, "") else None


# --------------------------------------------------------------- каталог
def catalog(run_id, ts, _f, _t):
    code, lst = seller_post("/v3/product/list",
                            {"filter": {"visibility": "ALL"}, "last_id": "", "limit": 1000})
    if code != 200:
        raise RuntimeError(f"product/list {code}: {lst}")
    ids = [i["product_id"] for i in ((lst.get("result") or {}).get("items") or [])]
    code, info = seller_post("/v3/product/info/list",
                             {"product_id": ids, "offer_id": [], "sku": []})
    if code != 200:
        raise RuntimeError(f"product/info {code}: {info}")
    d = str(now_msk().date())
    rows = []
    for i in (info.get("items") or []):
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
    return merge_rows("RAW_OZON_CATALOG", rows, ["snapshot_date", "sku"], run_id)


# ------------------------------------------------------------------ цены
def prices(run_id, ts, _f, _t):
    d = str(now_msk().date())
    rows, cursor = [], ""
    while True:
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
            c = i.get("commissions") or {}
            rows.append(dict(snapshot_ts=ts, snapshot_date=d, offer_id=str(i["offer_id"]),
                product_id=str(i.get("product_id")), price_rub=_num(p.get("price")),
                old_price_rub=_num(p.get("old_price")), min_price_rub=_num(p.get("min_price")),
                marketing_seller_price_rub=_num(p.get("marketing_seller_price")),
                net_price_rub=_num(p.get("net_price")), acquiring_rub=_num(i.get("acquiring")),
                sales_percent_fbo=_num(c.get("sales_percent_fbo")),
                price_index_color=pi.get("color_index"),
                external_min_price_rub=_num(ex.get("min_price")),
                external_index_value=_num(ex.get("price_index_value")),
                ozon_actions_exist=(i.get("marketing_actions") or {}).get("ozon_actions_exist"),
                source_payload_hash=h(d, i["offer_id"]),
                **_meta("POST /v5/product/info/prices", run_id, ts)))
        cursor = r.get("cursor") or ""
        if not cursor or not items:
            break
    # логический ключ снимка — дата, а не момент: повтор в тот же день не плодит строк
    return merge_rows("RAW_OZON_PRICES", rows, ["snapshot_date", "offer_id"], run_id)


# --------------------------------------------------------------- остатки
def stocks(run_id, ts, _f, _t):
    code, cat = seller_post("/v3/product/list",
                            {"filter": {"visibility": "ALL"}, "last_id": "", "limit": 1000})
    skus = [str(i["sku"]) for i in ((cat.get("result") or {}).get("items") or [])]
    code, r = seller_post("/v1/analytics/stocks", {"skus": skus})
    if code != 200:
        raise RuntimeError(f"stocks {code}: {r}")
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
        for i in (r.get("items") or [])]
    return merge_rows("RAW_OZON_STOCKS", rows,
                      ["snapshot_date", "sku", "warehouse_id"], run_id)


# ---------------------------------------------------------- заказы FBO
def fbo_postings(run_id, ts, frm, to):
    rows, cursor, page = [], "", 0
    while True:
        page += 1
        code, d = seller_post("/v3/posting/fbo/list", {
            "cursor": cursor,
            "filter": {"since": f"{frm}T00:00:00.000Z", "to": f"{to}T23:59:59.000Z"},
            "limit": 100, "with": {"analytics_data": True, "financial_data": True}})
        if code != 200:
            raise RuntimeError(f"posting/fbo/list {code}: {d}")
        for p in (d.get("postings") or []):
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
        cursor = d.get("cursor") or ""
        if not d.get("has_next") or not cursor or page > 200:
            break
        time.sleep(1)
    return merge_rows("RAW_OZON_POSTINGS_FBO", rows, ["posting_number", "sku"], run_id)


# ------------------------------------------------------------- финансы
def finance_accrual(run_id, ts, frm, to):
    types = {}
    code, t = seller_post("/v1/finance/accrual/types", {})
    if code == 200:
        types = {x["id"]: x["description"] for x in (t.get("accrual_types") or [])}
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
            last = r.get("last_id") or ""
            if not last or not chunk or page > 60:
                break
            time.sleep(1)
        cur += timedelta(days=1)
        time.sleep(1)
    return merge_rows("RAW_OZON_FINANCE_ACCRUAL", rows,
                      ["accrual_id", "type_id", "sku"], run_id)


# ------------------------------------------------------- реклама: кампании
def ads_campaigns(run_id, ts, _f, _t):
    code, txt = perf_get("/api/client/campaign")
    if code != 200:
        raise RuntimeError(f"campaign {code}")
    d = str(now_msk().date())
    rows = []
    for c in (json.loads(txt).get("list") or []):
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
    return merge_rows("RAW_OZON_ADS_CAMPAIGNS", rows,
                      ["snapshot_date", "campaign_id"], run_id)


def _csv_rows(text):
    return list(csv.DictReader(io.StringIO(text.lstrip("﻿")), delimiter=";"))


def _rub(v):
    return float((v or "0").replace(",", ".").replace("\xa0", "").replace(" ", "") or 0)


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


def ads_sku_daily(run_id, ts, frm, to):
    """Асинхронный отчёт — единственный доказанный источник расхода по SKU.

    Периметр берётся из ВСЕХ кампаний с активностью в окне, а не только RUNNING.
    """
    code, txt = perf_get("/api/client/campaign")
    ids = [c["id"] for c in (json.loads(txt).get("list") or [])] if code == 200 else []
    # оставляем только кампании с расходом в окне — иначе отчёт не сформируется
    active = set()
    d0, d1 = date.fromisoformat(str(frm)), date.fromisoformat(str(to))
    c2, t2 = perf_get(f"/api/client/statistics/expense?dateFrom={d0}&dateTo={d1}")
    if c2 == 200:
        for r in _csv_rows(t2):
            if r.get("ID") and _rub(r.get("Расход")) > 0:
                active.add(r["ID"])
    ids = [i for i in ids if i in active]
    rows = []
    for i in range(0, len(ids), 10):
        batch = ids[i:i + 10]
        code, sub = perf_post("/api/client/statistics",
                              {"campaigns": batch, "from": f"{d0}T00:00:00Z",
                               "to": f"{d1}T00:00:00Z", "groupBy": "DATE"})
        uuid = (sub or {}).get("UUID")
        if not uuid:
            continue
        for _ in range(60):
            time.sleep(10)
            c3, st = perf_get(f"/api/client/statistics/{uuid}", raw_text=False)
            if c3 == 200 and st.get("state") in ("OK", "ERROR"):
                break
        c4, blob = perf_get(f"/api/client/statistics/report?UUID={uuid}", raw_text=True)
        if c4 != 200:
            continue
        try:
            z = zipfile.ZipFile(io.BytesIO(blob.encode("utf-8", "surrogateescape")))
            files = [z.read(n).decode("utf-8-sig") for n in z.namelist()]
            names = z.namelist()
        except Exception:
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
                    impressions=int(_rub(r.get("Показы"))), clicks=int(_rub(r.get("Клики"))),
                    cart_adds=int(_rub(r.get("Добавления в корзину"))),
                    orders=int(_rub(r.get("Продано товаров"))),
                    revenue_promo_rub=_rub(r.get("Продажи в продвижении, ₽")),
                    ordered_total_rub=_rub(r.get("Заказано на сумму, ₽")),
                    drr_promo_pct=_rub(r.get("ДРР в продвижении, %")),
                    drr_total_pct=_rub(r.get("ДРР (общий), %")),
                    attribution_status="ATTRIBUTED_ACTUAL",
                    source_payload_hash=h(iso, cid, sku),
                    **_meta("POST /api/client/statistics (async report)", run_id, ts)))
        time.sleep(3)
    return merge_rows("RAW_OZON_ADS_SKU_DAILY", rows, ["date", "campaign_id", "sku"], run_id)


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


def supplies(run_id, ts, _f, _t):
    code, lst = seller_post("/v3/supply-order/list",
                            {"filter": {"states": CPC_STATES}, "limit": 100,
                             "sort_by": "ORDER_CREATION", "sort_dir": "DESC"})
    ids = (lst or {}).get("order_ids") or []
    orders = []
    for i in range(0, len(ids), 25):
        c, d = seller_post("/v3/supply-order/get", {"order_ids": ids[i:i + 25]})
        orders += (d or {}).get("orders") or []
        time.sleep(1)
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
    r1 = merge_rows("RAW_OZON_SUPPLY_ORDERS", o_rows, ["order_id"], run_id)
    r2 = merge_rows("RAW_OZON_SUPPLIES", s_rows, ["order_id", "supply_id"], run_id)
    # составы
    bmap = {(s["bundle_id"], s["order_id"], s["supply_id"])
            for s in s_rows if s.get("bundle_id")}
    b_rows = []
    for bid, oid, sid in sorted(bmap):
        c, d = seller_post("/v1/supply-order/bundle", {"bundle_ids": [bid], "limit": 100})
        if c != 200:
            continue
        for i in (d.get("items") or []):
            b_rows.append(dict(bundle_id=bid, supply_id=sid, order_id=oid,
                sku=str(i["sku"]), offer_id=str(i.get("offer_id") or ""),
                product_name=i.get("name"), quantity_planned=i.get("quantity"),
                quantity_accepted=None,     # ACTUAL_RECEIPT_QUANTITY = NOT_PROVEN
                volume_in_litres=_num(i.get("volume_in_litres")),
                item_tags=json.dumps(i.get("tags"), ensure_ascii=False) if i.get("tags") else None,
                source_payload_hash=h(bid, i["sku"]),
                **_meta("POST /v1/supply-order/bundle", run_id, ts)))
        time.sleep(1)
    r3 = merge_rows("RAW_OZON_SUPPLY_BUNDLES", b_rows, ["bundle_id", "sku"], run_id)
    return {"received": r1["received"] + r2["received"] + r3["received"],
            "inserted": r1["inserted"] + r2["inserted"] + r3["inserted"],
            "updated": r1["updated"] + r2["updated"] + r3["updated"]}


# сущность → (функция, окно ретроспективы в днях, частота)
REGISTRY = {
    "catalog":           (catalog, 0, "daily"),
    "prices":            (prices, 0, "daily"),
    "stocks":            (stocks, 0, "twice_daily"),
    "fbo_postings":      (fbo_postings, 30, "twice_daily"),
    "finance_accrual":   (finance_accrual, 14, "daily"),
    "ads_campaigns":     (ads_campaigns, 0, "daily"),
    "ads_expense_daily": (ads_expense_daily, 7, "daily"),
    "ads_sku_daily":     (ads_sku_daily, 7, "daily"),
    "clusters":          (clusters, 0, "weekly"),
    "supplies":          (supplies, 0, "daily"),
}
