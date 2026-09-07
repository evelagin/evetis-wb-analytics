#!/usr/bin/env python3
"""Stage 2 backfill — финансы. Только production endpoints из finance_api_contract_v1.csv.

ЗАПРЕЩЕНЫ и не вызываются: /v3/finance/transaction/list, /v3/finance/transaction/totals.
"""
import time

from _bf_common import (PERIOD_END, PERIOD_START, days, save, seller_post)


def accrual_types():
    code, d = seller_post("/v1/finance/accrual/types", {}, "finance_types", "-")
    save("finance", "accrual_types.json", {"http_status": code, "data": d})
    n = len((d or {}).get("accrual_types") or [])
    print(f"accrual_types            HTTP {code}  типов: {n}", flush=True)


def accrual_by_day():
    total = 0
    for d0 in days():
        ds = d0.isoformat()
        rows, last, page = [], "", 0
        code = None
        while True:
            page += 1
            code, r = seller_post("/v1/finance/accrual/by-day",
                                  {"date": ds, "last_id": last},
                                  "finance_accrual_daily", ds)
            if code != 200:
                break
            chunk = r.get("accruals") or []
            rows.extend(chunk)
            last = r.get("last_id") or ""
            if not last or not chunk or page > 60:
                break
            time.sleep(1.5)
        save("finance", f"accrual_by_day_{ds.replace('-', '')}.json",
             {"date": ds, "http_status": code, "pages": page, "accruals": rows})
        total += len(rows)
        print(f"accrual_by_day {ds}  HTTP {code}  стр:{page}  начислений:{len(rows)}",
              flush=True)
        time.sleep(2)
    print(f"accrual_by_day ИТОГО начислений: {total}", flush=True)


def cash_flow():
    for m, (a, b) in {"202606": ("2026-06-01", "2026-06-30"),
                      "202607": ("2026-07-01", "2026-07-31"),
                      "202608": ("2026-08-01", "2026-08-31")}.items():
        page, rows, code = 1, [], None
        while True:
            code, d = seller_post(
                "/v1/finance/cash-flow-statement/list",
                {"date": {"from": f"{a}T00:00:00.000Z", "to": f"{b}T23:59:59.000Z"},
                 "page": page, "page_size": 100, "with_details": True},
                "finance_cash_flow", m)
            if code != 200:
                break
            res = d.get("result") or {}
            rows.extend(res.get("cash_flows") or [])
            if page >= (res.get("page_count") or 1) or page > 20:
                save("finance", f"cash_flow_{m}.json",
                     {"month": m, "http_status": code, "raw_last_page": d,
                      "cash_flows": rows})
                break
            page += 1
            time.sleep(2)
        print(f"cash_flow {m}            HTTP {code}  строк: {len(rows)}", flush=True)
        time.sleep(2)


def realization():
    for y, m in ((2026, 6), (2026, 7), (2026, 8)):
        code, d = seller_post("/v2/finance/realization", {"month": m, "year": y},
                              "finance_realization", f"{y}{m:02d}")
        save("finance", f"realization_{y}{m:02d}.json", {"http_status": code, "data": d})
        n = len(((d or {}).get("result") or {}).get("rows") or [])
        print(f"realization {y}-{m:02d}       HTTP {code}  строк: {n}", flush=True)
        time.sleep(2)


if __name__ == "__main__":
    print(f"=== FINANCE BACKFILL {PERIOD_START} → {PERIOD_END} ===", flush=True)
    accrual_types()
    time.sleep(3)
    realization()
    cash_flow()
    accrual_by_day()
    print("=== FINANCE DONE ===", flush=True)
