#!/usr/bin/env python3
"""Stage 2 — нормализация raw backfill в фактовые датасеты.

Принципы:
  * raw не изменяется, только читается;
  * NOT_PROVEN / NOT_AVAILABLE / NOT_APPLICABLE не превращаются в 0;
  * COGS берётся по effective-дате операции, без forward-fill;
  * unit_number трактуется в зависимости от типа операции.
"""
import csv
import glob
import json
import os
from collections import defaultdict
from datetime import date, datetime

AUDIT = os.path.dirname(os.path.abspath(__file__))
BF = os.path.join(AUDIT, "raw", "backfill_v1")
DATA = os.path.join(AUDIT, "data")

PERIOD = [date(2026, 6, 1), date(2026, 8, 31)]
COMMISSION_CHANGE = date(2026, 8, 28)

# unit_number значит разное в зависимости от типа операции — доказано Stage 1.7
UNIT_IS_CAMPAIGN = {41, 54}


def rd(path):
    return json.load(open(path, encoding="utf-8"))


def wr(name, fields, rows):
    p = os.path.join(DATA, name)
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"  {name:44} {len(rows)} строк")
    return p


# ---------------------------------------------------------------- справочники
TYPES = {t["id"]: t for t in rd(os.path.join(BF, "finance", "accrual_types.json"))
         ["data"]["accrual_types"]}

CAT = {}
for r in csv.DictReader(open(os.path.join(DATA, "catalog_reconciliation.csv"), encoding="utf-8")):
    CAT[r["ozon_sku"]] = r

REG = {r["campaign_id"]: r for r in
       csv.DictReader(open(os.path.join(DATA, "campaign_registry_current.csv"), encoding="utf-8"))}

COGS = defaultdict(list)
for r in csv.DictReader(open(os.path.join(DATA, "cogs_effective_history.csv"), encoding="utf-8")):
    COGS[r["internal_sku"]].append(
        (date.fromisoformat(r["effective_from"]),
         date.fromisoformat(r["effective_to"]) if r["effective_to"] else date(9999, 12, 31),
         float(r["product_cogs_rub"])))


def cogs_at(internal_sku, d):
    """COGS, действующий на дату операции. Без forward-fill: нет интервала — COGS_MISSING."""
    for a, b, v in COGS.get(internal_sku, []):
        if a <= d <= b:
            return v
    return None


def sku_ref(sku):
    c = CAT.get(str(sku))
    return (c["offer_id"], c["internal_sku"]) if c else ("", "")


def grp(cid):
    return REG.get(str(cid), {}).get("campaign_group", "UNKNOWN")


def f(x):
    return None if x in (None, "") else float(x)


# ------------------------------------------------------- 1. finance_accrual_fact
def finance_fact():
    F = ["event_date", "operation_id", "type_id", "operation_name", "accrued_category",
         "unit_number", "unit_number_meaning", "posting_number", "ozon_sku", "offer_id",
         "internal_sku", "quantity", "seller_price", "amount", "currency",
         "source_endpoint", "source_raw_file"]
    rows = []
    for path in sorted(glob.glob(os.path.join(BF, "finance", "accrual_by_day_*.json"))):
        d = rd(path)
        src = os.path.relpath(path, AUDIT)
        for a in d["accruals"]:
            base = dict(event_date=d["date"], operation_id=a["accrual_id"],
                        accrued_category=a["accrued_category"],
                        unit_number=a.get("unit_number"),
                        currency=(a.get("total_amount") or {}).get("currency", "RUB"),
                        source_endpoint="/v1/finance/accrual/by-day", source_raw_file=src)

            def emit(tid, amount, sku=None, qty=None, sprice=None, posting=None):
                off, isku = sku_ref(sku) if sku else ("", "")
                meaning = ("campaign_id" if tid in UNIT_IS_CAMPAIGN
                           else ("posting_number" if a["accrued_category"] == "POSTING"
                                 else "order_or_item_ref"))
                rows.append(dict(base, type_id=tid,
                                 operation_name=TYPES.get(tid, {}).get("description", "UNKNOWN"),
                                 unit_number_meaning=meaning, posting_number=posting or "",
                                 ozon_sku=sku or "", offer_id=off, internal_sku=isku,
                                 quantity=qty if qty is not None else "NOT_AVAILABLE",
                                 seller_price=sprice if sprice is not None else "NOT_AVAILABLE",
                                 amount=amount))

            nf = a.get("non_item_fee")
            if nf:
                emit(nf["type_id"], f(nf["accrued"]["amount"]))
            itf = a.get("item_fees") or {}
            for fee in (itf.get("fees") or []):
                for x in (fee.get("fees") or []):
                    emit(x["type_id"], f(x["accrued"]["amount"]), sku=fee.get("sku"))
            p = a.get("posting") or {}
            pn = p.get("posting_number") or (a.get("unit_number")
                                             if a["accrued_category"] == "POSTING" else None)
            for pr in (p.get("products") or []):
                for key, blk in pr.items():
                    if isinstance(blk, dict) and blk.get("services"):
                        for s in blk["services"]:
                            emit(s["type_id"], f(s["accrued"]["amount"]),
                                 sku=pr.get("sku"), posting=pn)
            cf = a.get("container_fees")
            if cf:
                for x in (cf if isinstance(cf, list) else [cf]):
                    if isinstance(x, dict) and "type_id" in x:
                        emit(x["type_id"], f((x.get("accrued") or {}).get("amount")))
    return wr("finance_accrual_fact.csv", F, rows), rows


# ---------------------------------------------- 2. performance_campaign_daily
def perf_daily():
    F = ["date", "campaign_id", "campaign_group", "campaign_state_current", "expense_rub",
         "bonus_expense_rub", "subscription_expense_rub", "impressions", "clicks", "orders",
         "revenue", "ctr", "cpc", "drr_api_if_present", "source_raw_file"]
    exp = {}
    for path in sorted(glob.glob(os.path.join(BF, "performance", "expense_*.csv"))):
        txt = open(path, encoding="utf-8").read()
        for r in csv.DictReader(txt.splitlines(), delimiter=";"):
            if not r.get("ID"):
                continue
            exp[(r["Дата"], r["ID"])] = (
                float(r["Расход"].replace(",", ".")),
                float(r["Расход бонусов"].replace(",", ".")),
                float(r["Расход с абонентского счета"].replace(",", ".")),
                os.path.relpath(path, AUDIT))
    stats = {}
    for path in sorted(glob.glob(os.path.join(BF, "performance", "daily_*.csv"))):
        txt = open(path, encoding="utf-8").read()
        for r in csv.DictReader(txt.splitlines(), delimiter=";"):
            if not r.get("ID"):
                continue
            stats[(r["Дата"], r["ID"])] = r
    rows = []
    for k in sorted(set(exp) | set(stats)):
        d, cid = k
        e = exp.get(k)
        s = stats.get(k)
        imp = int(s["Показы"]) if s else "NOT_AVAILABLE"
        clk = int(s["Клики"]) if s else "NOT_AVAILABLE"
        orders = int(s["Заказы, шт."]) if s else "NOT_AVAILABLE"
        rev = float(s["Заказы, ₽"].replace(",", ".")) if s else "NOT_AVAILABLE"
        spend = e[0] if e else "NOT_AVAILABLE"
        ctr = round(clk / imp * 100, 4) if s and imp else ("NOT_APPLICABLE" if s else "NOT_AVAILABLE")
        cpc = round(spend / clk, 4) if (e and s and clk) else ("NOT_APPLICABLE" if s else "NOT_AVAILABLE")
        rows.append(dict(date=d, campaign_id=cid, campaign_group=grp(cid),
                         campaign_state_current=REG.get(cid, {}).get("state", "UNKNOWN"),
                         expense_rub=spend,
                         bonus_expense_rub=e[1] if e else "NOT_AVAILABLE",
                         subscription_expense_rub=e[2] if e else "NOT_AVAILABLE",
                         impressions=imp, clicks=clk, orders=orders, revenue=rev,
                         ctr=ctr, cpc=cpc,
                         drr_api_if_present="NOT_AVAILABLE",
                         source_raw_file=e[3] if e else "performance/daily_*.csv"))
    return wr("performance_campaign_daily.csv", F, rows), rows


# ------------------------------------------------------ 3. performance_sku_daily
def perf_sku_daily(pdaily):
    """Связь campaign→SKU доказуема только там, где кампания содержит РОВНО один SKU.
    Искусственно расщеплять расход мультитоварных кампаний нельзя."""
    F = ["date", "campaign_id", "campaign_group", "ozon_sku", "offer_id", "internal_sku",
         "expense_rub", "impressions", "clicks", "orders", "revenue",
         "allocation_method", "note"]
    camp_sku = defaultdict(list)
    for path in glob.glob(os.path.join(BF, "performance", "products_*.json")):
        cid = os.path.basename(path)[len("products_"):-len(".json")]
        try:
            d = json.loads(open(path, encoding="utf-8").read())
        except Exception:
            continue
        for p in (d.get("products") or []):
            camp_sku[cid].append(str(p["sku"]))
    rows, skipped = [], 0
    for r in pdaily:
        skus = camp_sku.get(r["campaign_id"], [])
        if len(skus) != 1:
            skipped += 1
            continue
        off, isku = sku_ref(skus[0])
        rows.append(dict(r, ozon_sku=skus[0], offer_id=off, internal_sku=isku,
                         allocation_method="DIRECT_SINGLE_SKU_CAMPAIGN",
                         note="кампания содержит ровно один SKU, расход относится к нему целиком"))
    print(f"  (пропущено строк без доказуемой связки campaign→SKU: {skipped})")
    return wr("performance_sku_daily.csv", F, rows), rows


# --------------------------------------------------------- 4. orders_fbo_fact
def orders_fact():
    F = ["created_date", "posting_number", "order_id", "order_number", "status", "substatus",
         "ozon_sku", "offer_id", "internal_sku", "quantity", "price", "old_price",
         "total_discount_value", "commission_amount", "payout", "is_cancelled",
         "cancel_reason_id", "source_raw_file"]
    rows = []
    for path in sorted(glob.glob(os.path.join(BF, "seller", "posting_fbo_*.json"))):
        d = rd(path)
        src = os.path.relpath(path, AUDIT)
        for p in d["postings"]:
            fin = {str(x.get("product_id")): x for x in
                   ((p.get("financial_data") or {}).get("products") or [])}
            for pr in (p.get("products") or []):
                sku = str(pr.get("sku"))
                off, isku = sku_ref(sku)
                fd = fin.get(sku, {})
                rows.append(dict(
                    created_date=(p.get("created_at") or "")[:10],
                    posting_number=p.get("posting_number"), order_id=p.get("order_id"),
                    order_number=p.get("order_number"), status=p.get("status"),
                    substatus=p.get("substatus") or "", ozon_sku=sku, offer_id=off,
                    internal_sku=isku, quantity=pr.get("quantity"),
                    price=f(fd.get("price")) if fd.get("price") is not None else f(pr.get("price")),
                    old_price=f(fd.get("old_price")),
                    total_discount_value=f(fd.get("total_discount_value")),
                    commission_amount=f(fd.get("commission_amount")),
                    payout=f(fd.get("payout")),
                    is_cancelled="yes" if p.get("status") == "cancelled" else "no",
                    cancel_reason_id=p.get("cancel_reason_id") or "",
                    source_raw_file=src))
    return wr("orders_fbo_fact.csv", F, rows), rows


if __name__ == "__main__":
    print("=== BUILD FACTS ===")
    _, fin_rows = finance_fact()
    _, pd_rows = perf_daily()
    perf_sku_daily(pd_rows)
    orders_fact()
    print("=== DONE ===")
