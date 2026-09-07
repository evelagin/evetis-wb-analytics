#!/usr/bin/env python3
"""Stage 2 backfill — Ozon Performance API. ТОЛЬКО READ-методы.

Не вызываются: activate, deactivate, set_bid, daily_budget, period, products POST/PUT.
Токен живёт в памяти, обновляется каждые 25 минут, на диск не пишется.
"""
import json
import time

from _bf_common import PerfToken, days, perf_get, save

CPC = ["32157965", "32158176", "33678192", "32157919", "32157988", "33678288",
       "32157947", "33703975", "32157891", "32158051", "32158008", "32158299",
       "33678220", "33678331", "33678261"]


def campaigns(tk):
    code, txt = perf_get("/api/client/campaign", tk.get(), "perf_campaigns", "snapshot")
    save("performance", "campaigns_snapshot.json", txt, is_text=True)
    n = len((json.loads(txt) if code == 200 else {}).get("list") or [])
    print(f"campaigns snapshot       HTTP {code}  кампаний: {n}", flush=True)


def expense_daily(tk):
    ok = 0
    for d0 in days():
        ds = d0.isoformat()
        code, txt = perf_get(
            f"/api/client/statistics/expense?dateFrom={ds}&dateTo={ds}",
            tk.get(), "perf_expense_daily", ds)
        save("performance", f"expense_{ds.replace('-', '')}.csv",
             txt if isinstance(txt, str) else json.dumps(txt), is_text=True)
        rows = len([l for l in str(txt).splitlines() if l.strip()]) - 1
        ok += code == 200
        print(f"expense {ds}       HTTP {code}  строк: {max(rows, 0)}", flush=True)
        time.sleep(2)
    print(f"expense_daily: успешных дней {ok}", flush=True)


def daily_stats(tk):
    for label, a, b in (("202606", "2026-06-01", "2026-06-30"),
                        ("202607", "2026-07-01", "2026-07-31"),
                        ("202608", "2026-08-01", "2026-08-31")):
        code, txt = perf_get(f"/api/client/statistics/daily?dateFrom={a}&dateTo={b}",
                             tk.get(), "perf_daily_stats", label)
        save("performance", f"daily_{label}.csv",
             txt if isinstance(txt, str) else json.dumps(txt), is_text=True)
        rows = len([l for l in str(txt).splitlines() if l.strip()]) - 1
        print(f"daily_stats {label}       HTTP {code}  строк: {max(rows, 0)}", flush=True)
        time.sleep(2)


def campaign_products(tk):
    for cid in CPC:
        code, txt = perf_get(f"/api/client/campaign/{cid}/v2/products?page=1&pageSize=100",
                             tk.get(), "perf_campaign_products", cid)
        save("performance", f"products_{cid}.json",
             txt if isinstance(txt, str) else json.dumps(txt), is_text=True)
        print(f"products {cid}         HTTP {code}", flush=True)
        time.sleep(1.5)


if __name__ == "__main__":
    print("=== PERFORMANCE BACKFILL 2026-06-01 → 2026-08-31 ===", flush=True)
    tk = PerfToken()
    campaigns(tk)
    campaign_products(tk)
    daily_stats(tk)
    expense_daily(tk)
    print("=== PERFORMANCE DONE ===", flush=True)
