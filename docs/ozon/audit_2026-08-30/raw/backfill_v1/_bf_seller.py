#!/usr/bin/env python3
"""Stage 2 backfill — Seller API: каталог, цены, остатки, заказы FBO, возвраты, аналитика.

Заказы берутся ТОЛЬКО через /v3/posting/fbo/list. /v2/posting/fbo/list запрещён.
"""
import time
from datetime import date

from _bf_common import save, seller_post

MONTHS = (("202606", "2026-06-01", "2026-06-30"),
          ("202607", "2026-07-01", "2026-07-31"),
          ("202608", "2026-08-01", "2026-08-31"))


def catalog():
    code, lst = seller_post("/v3/product/list",
                            {"filter": {"visibility": "ALL"}, "last_id": "", "limit": 1000},
                            "catalog", "snapshot")
    save("catalog", "product_list.json", {"http_status": code, "data": lst})
    items = ((lst or {}).get("result") or {}).get("items") or []
    ids = [i["product_id"] for i in items]
    print(f"product_list             HTTP {code}  товаров: {len(ids)}", flush=True)
    time.sleep(2)
    code2, info = seller_post("/v3/product/info/list",
                              {"product_id": ids, "offer_id": [], "sku": []},
                              "catalog", "snapshot")
    save("catalog", "product_info.json", {"http_status": code2, "data": info})
    print(f"product_info             HTTP {code2}  товаров: {len((info or {}).get('items') or [])}",
          flush=True)


def prices():
    code, d = seller_post("/v5/product/info/prices",
                          {"cursor": "", "filter": {"offer_id": [], "product_id": [],
                                                    "visibility": "ALL"}, "limit": 100},
                          "prices_snapshot", "snapshot")
    save("seller", "prices_snapshot.json", {"http_status": code, "data": d})
    print(f"prices_snapshot          HTTP {code}  товаров: {len((d or {}).get('items') or [])}",
          flush=True)


def stocks():
    info = None
    import json as _j
    import os
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "catalog", "product_info.json")
    if os.path.exists(p):
        info = _j.load(open(p, encoding="utf-8"))["data"]
    skus = [str(i["sku"]) for i in (info or {}).get("items", []) if i.get("sku")]
    code, d = seller_post("/v1/analytics/stocks", {"skus": skus}, "stocks_snapshot", "snapshot")
    save("seller", "stocks_snapshot.json", {"http_status": code, "skus": skus, "data": d})
    print(f"stocks_snapshot          HTTP {code}  строк: {len((d or {}).get('items') or [])}",
          flush=True)


def orders_fbo():
    total = 0
    for label, a, b in MONTHS:
        cursor, page, rows, code = "", 0, [], None
        while True:
            page += 1
            code, d = seller_post(
                "/v3/posting/fbo/list",
                {"cursor": cursor,
                 "filter": {"since": f"{a}T00:00:00.000Z", "to": f"{b}T23:59:59.000Z"},
                 "limit": 100,
                 "with": {"analytics_data": True, "financial_data": True}},
                "orders_fbo", f"{label}-p{page}")
            if code != 200:
                break
            rows.extend(d.get("postings") or [])
            cursor = d.get("cursor") or ""
            if not d.get("has_next") or not cursor or page > 40:
                break
            time.sleep(2)
        save("seller", f"posting_fbo_{label}.json",
             {"month": label, "http_status": code, "pages": page, "postings": rows})
        total += len(rows)
        print(f"posting_fbo {label}      HTTP {code}  стр:{page}  отправлений:{len(rows)}",
              flush=True)
        time.sleep(2)
    print(f"orders_fbo ИТОГО: {total}", flush=True)


def returns():
    last, page, rows, code = 0, 0, [], None
    while True:
        page += 1
        body = {"filter": {}, "limit": 500}
        if last:
            body["last_id"] = last
        code, d = seller_post("/v1/returns/list", body, "returns", f"p{page}")
        if code != 200:
            break
        chunk = d.get("returns") or []
        rows.extend(chunk)
        if not d.get("has_next") or not chunk or page > 40:
            break
        last = chunk[-1]["id"]
        time.sleep(2)
    save("seller", "returns.json", {"http_status": code, "pages": page, "returns": rows})
    print(f"returns                  HTTP {code}  стр:{page}  возвратов:{len(rows)}", flush=True)


def analytics():
    for label, a, b in MONTHS:
        code, d = seller_post(
            "/v1/analytics/data",
            {"date_from": a, "date_to": b, "dimension": ["sku", "day"],
             "metrics": ["ordered_units", "revenue"], "limit": 1000, "offset": 0},
            "analytics_sales", label)
        save("seller", f"analytics_data_{label}.json", {"http_status": code, "data": d})
        n = len(((d or {}).get("result") or {}).get("data") or [])
        print(f"analytics_data {label}   HTTP {code}  строк: {n}", flush=True)
        time.sleep(2)


if __name__ == "__main__":
    print("=== SELLER BACKFILL 2026-06-01 → 2026-08-31 ===", flush=True)
    catalog(); time.sleep(2)
    prices(); time.sleep(2)
    stocks(); time.sleep(2)
    analytics()
    returns(); time.sleep(2)
    orders_fbo()
    print("=== SELLER DONE ===", flush=True)
