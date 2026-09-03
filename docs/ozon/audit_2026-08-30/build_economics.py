#!/usr/bin/env python3
"""Stage 2 — маркетинговые затраты, сверка и юнит-экономика поверх фактов.

Комиссия берётся ФАКТИЧЕСКАЯ, из commission_ratio на уровне операции.
Вводные 41 % / 52 % используются только как перекрёстная проверка.
NOT_PROVEN не превращается в 0.
"""
import csv
import glob
import json
import os
import re
from collections import defaultdict
from datetime import date

AUDIT = os.path.dirname(os.path.abspath(__file__))
BF = os.path.join(AUDIT, "raw", "backfill_v1")
DATA = os.path.join(AUDIT, "data")

PERIOD_A = (date(2026, 6, 1), date(2026, 8, 27))     # комиссия 41 %
PERIOD_B = (date(2026, 8, 28), date(2026, 8, 31))    # комиссия 52 %

LOGISTICS = {32, 29, 98, 59, 12}          # логистика и доставка
OTHER_MP = {1, 46, 76, 15, 39, 38, 45, 71}  # эквайринг, хранение, страховка, утилизация и прочее
COMMISSION_T = {69}


def rd(p):
    return json.load(open(p, encoding="utf-8"))


def wr(name, fields, rows):
    with open(os.path.join(DATA, name), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"  {name:44} {len(rows)} строк")


TYPES = {t["id"]: t for t in rd(os.path.join(BF, "finance", "accrual_types.json"))
         ["data"]["accrual_types"]}
CAT = {r["ozon_sku"]: r for r in
       csv.DictReader(open(os.path.join(DATA, "catalog_reconciliation.csv"), encoding="utf-8"))}
REG = {r["campaign_id"]: r for r in
       csv.DictReader(open(os.path.join(DATA, "campaign_registry_current.csv"), encoding="utf-8"))}

COGS = defaultdict(list)
for r in csv.DictReader(open(os.path.join(DATA, "cogs_effective_history.csv"), encoding="utf-8")):
    COGS[r["internal_sku"]].append(
        (date.fromisoformat(r["effective_from"]),
         date.fromisoformat(r["effective_to"]) if r["effective_to"] else date(9999, 12, 31),
         float(r["product_cogs_rub"])))


def cogs_at(isku, d):
    for a, b, v in COGS.get(isku, []):
        if a <= d <= b:
            return v
    return None


def ref(sku):
    c = CAT.get(str(sku))
    return (c["offer_id"], c["internal_sku"]) if c else ("", "")


def num(x):
    return float(x) if x not in (None, "") else 0.0


# ------------------------------------------------- разбор начислений по SKU/дню
def parse_accruals():
    sku_day = defaultdict(lambda: defaultdict(float))   # (date, sku) -> metric -> value
    ratios = defaultdict(list)
    review = []                                          # type 116, SKU-уровень
    camp_day = defaultdict(lambda: defaultdict(float))   # (date, campaign) -> type -> amount
    for path in sorted(glob.glob(os.path.join(BF, "finance", "accrual_by_day_*.json"))):
        d = rd(path)
        ds = d["date"]
        for a in d["accruals"]:
            nf = a.get("non_item_fee")
            if nf:
                camp_day[(ds, str(a.get("unit_number")))][nf["type_id"]] += num(
                    nf["accrued"]["amount"])
            for fee in ((a.get("item_fees") or {}).get("fees") or []):
                sku = str(fee.get("sku"))
                for x in (fee.get("fees") or []):
                    amt = num(x["accrued"]["amount"])
                    if x["type_id"] == 116:
                        off, isku = ref(sku)
                        review.append(dict(date=ds, ozon_sku=sku, offer_id=off,
                                           internal_sku=isku, cost_rub=round(abs(amt), 2)))
                    sku_day[(ds, sku)][f"type_{x['type_id']}"] += amt
            for pr in ((a.get("posting") or {}).get("products") or []):
                sku = str(pr.get("sku"))
                k = (ds, sku)
                c = pr.get("commission") or {}
                if c:
                    sku_day[k]["seller_price"] += num((c.get("seller_price") or {}).get("amount"))
                    sku_day[k]["sale_price"] += num((c.get("sale_price") or {}).get("amount"))
                    sku_day[k]["bonus"] += num((c.get("bonus") or {}).get("amount"))
                    sku_day[k]["coinvestment"] += num((c.get("coinvestment") or {}).get("amount"))
                    sku_day[k]["commission"] += num((c.get("commission") or {}).get("amount"))
                    sku_day[k]["units"] += 1
                    m = re.search(r'([\d.]+)', str(c.get("commission_ratio") or ""))
                    if m:
                        ratios[(ds, sku)].append(float(m.group(1)))
                for key, blk in pr.items():
                    if isinstance(blk, dict) and blk.get("services"):
                        for s in blk["services"]:
                            amt = num(s["accrued"]["amount"])
                            t = s["type_id"]
                            sku_day[k]["logistics" if t in LOGISTICS else
                                       ("other_mp" if t in OTHER_MP else "other_unclassified")] += amt
    return sku_day, ratios, review, camp_day


# ---------------------------------------------------- 1. marketing_cost_fact
def marketing_fact(camp_day, review, perf_sku):
    F = ["date", "marketing_cost_type", "campaign_id", "ozon_sku", "offer_id", "internal_sku",
         "cash_cost_rub", "product_cogs_cost_rub", "total_marketing_cost_rub",
         "evidence_status", "primary_source", "finance_type_id", "source_raw_file"]
    rows = []
    for (ds, unit), types_ in sorted(camp_day.items()):
        for tid, amt in types_.items():
            if tid not in (41, 54):
                continue
            cost = round(abs(amt), 2)
            kind = "CPC_MEDIA" if tid == 41 else "SEARCH_PROMO_CPO"
            rows.append(dict(date=ds, marketing_cost_type=kind, campaign_id=unit,
                             ozon_sku="NOT_APPLICABLE", offer_id="NOT_APPLICABLE",
                             internal_sku="NOT_APPLICABLE", cash_cost_rub=cost,
                             product_cogs_cost_rub="NOT_APPLICABLE",
                             total_marketing_cost_rub=cost, evidence_status="CONFIRMED_SPEND",
                             primary_source="Seller API /v1/finance/accrual/by-day",
                             finance_type_id=tid,
                             source_raw_file=f"raw/backfill_v1/finance/accrual_by_day_{ds.replace('-','')}.json"))
    for r in review:
        rows.append(dict(date=r["date"], marketing_cost_type="FIRST_REVIEW_PROMO",
                         campaign_id="NOT_APPLICABLE", ozon_sku=r["ozon_sku"],
                         offer_id=r["offer_id"], internal_sku=r["internal_sku"],
                         cash_cost_rub=r["cost_rub"], product_cogs_cost_rub="NOT_APPLICABLE",
                         total_marketing_cost_rub=r["cost_rub"], evidence_status="CONFIRMED_SPEND",
                         primary_source="Seller API /v1/finance/accrual/by-day",
                         finance_type_id=116,
                         source_raw_file=f"raw/backfill_v1/finance/accrual_by_day_{r['date'].replace('-','')}.json"))
    # бонусы и абонентский счёт — из Performance, доказанный ноль
    for r in perf_sku:
        for kind, col in (("BONUS_AD_SPEND", "bonus_expense_rub"),
                          ("SUBSCRIPTION_AD_SPEND", "subscription_expense_rub")):
            v = r.get(col)
            if v in ("NOT_AVAILABLE", None, ""):
                continue
            if float(v) != 0:
                rows.append(dict(date=r["date"], marketing_cost_type=kind,
                                 campaign_id=r["campaign_id"], ozon_sku="NOT_APPLICABLE",
                                 offer_id="NOT_APPLICABLE", internal_sku="NOT_APPLICABLE",
                                 cash_cost_rub=float(v), product_cogs_cost_rub="NOT_APPLICABLE",
                                 total_marketing_cost_rub=float(v),
                                 evidence_status="CONFIRMED_SPEND",
                                 primary_source="Performance API statistics/expense",
                                 finance_type_id="NOT_APPLICABLE", source_raw_file="raw/backfill_v1/performance/"))
    # недоказанные компоненты — строкой с NULL, не нулём
    for kind, g in (("REF_VK_PLATFORM_FEE", "REF_VK"), ("REF_BLOGGER_PLATFORM_FEE", "REF_BLOGGER")):
        rows.append(dict(date="PERIOD_TOTAL", marketing_cost_type=kind, campaign_id="ALL",
                         ozon_sku="NOT_APPLICABLE", offer_id="NOT_APPLICABLE",
                         internal_sku="NOT_APPLICABLE", cash_cost_rub=0,
                         product_cogs_cost_rub="NOT_APPLICABLE", total_marketing_cost_rub=0,
                         evidence_status="CONFIRMED_ZERO_DIRECT_SPEND",
                         primary_source="Performance API + accrual/by-day",
                         finance_type_id="NOT_FOUND", source_raw_file="см. LOG.md Stage 1.7"))
    for kind in ("REF_VK_PRODUCT_COGS", "REF_BLOGGER_PRODUCT_COGS"):
        rows.append(dict(date="PERIOD_TOTAL", marketing_cost_type=kind, campaign_id="NOT_APPLICABLE",
                         ozon_sku="NOT_APPLICABLE", offer_id="NOT_APPLICABLE",
                         internal_sku="NOT_APPLICABLE", cash_cost_rub="NULL",
                         product_cogs_cost_rub="NULL", total_marketing_cost_rub="NULL",
                         evidence_status="NOT_PROVEN",
                         primary_source="/v3/posting/fbo/list financial_data",
                         finance_type_id="NOT_FOUND",
                         source_raw_file="raw/backfill_v1/seller/posting_fbo_*.json"))
    rows.sort(key=lambda r: (str(r["date"]), r["marketing_cost_type"]))
    wr("marketing_cost_fact.csv", F, rows)
    return rows


# ------------------------------------------------ 2. сверка CPC и SEARCH_PROMO
def reconcile(camp_day):
    F = ["date", "performance_cpc", "finance_type41", "difference_rub", "difference_pct",
         "performance_search_promo", "finance_type54", "difference_promo_rub", "status"]
    perf = defaultdict(lambda: [0.0, 0.0])
    for path in sorted(glob.glob(os.path.join(BF, "performance", "expense_*.csv"))):
        for r in csv.DictReader(open(path, encoding="utf-8").read().splitlines(), delimiter=";"):
            if not r.get("ID"):
                continue
            v = float(r["Расход"].replace(",", "."))
            perf[r["Дата"]][1 if r["ID"] == "14503166" else 0] += v
    fin = defaultdict(lambda: [0.0, 0.0])
    for (ds, _u), t in camp_day.items():
        fin[ds][0] += abs(t.get(41, 0.0))
        fin[ds][1] += abs(t.get(54, 0.0))
    rows = []
    for ds in sorted(set(perf) | set(fin)):
        pc, pp = round(perf[ds][0], 2), round(perf[ds][1], 2)
        fc, fp = round(fin[ds][0], 2), round(fin[ds][1], 2)
        diff = round(pc - fc, 2)
        dp = round(diff / fc * 100, 4) if fc else (0.0 if pc == 0 else None)
        rows.append(dict(date=ds, performance_cpc=pc, finance_type41=fc,
                         difference_rub=diff,
                         difference_pct=dp if dp is not None else "NOT_APPLICABLE",
                         performance_search_promo=pp, finance_type54=fp,
                         difference_promo_rub=round(pp - fp, 2),
                         status="EXACT_MATCH" if abs(diff) < 0.005 and abs(pp - fp) < 0.005
                         else "MISMATCH"))
    wr("cpc_daily_reconciliation.csv", F, rows)
    return rows


# ---------------------------------------------- 3. first_review_promo_daily
def review_daily(review, sku_day):
    F = ["date", "ozon_sku", "internal_sku", "events", "cost_rub", "revenue_same_period",
         "cost_pct_of_revenue"]
    agg = defaultdict(lambda: [0, 0.0])
    for r in review:
        k = (r["date"], r["ozon_sku"], r["internal_sku"])
        agg[k][0] += 1
        agg[k][1] += r["cost_rub"]
    rows = []
    for (ds, sku, isku), (n, c) in sorted(agg.items()):
        rev = round(sku_day.get((ds, sku), {}).get("seller_price", 0.0), 2)
        rows.append(dict(date=ds, ozon_sku=sku, internal_sku=isku, events=n,
                         cost_rub=round(c, 2), revenue_same_period=rev,
                         cost_pct_of_revenue=round(c / rev * 100, 2) if rev else "NOT_APPLICABLE"))
    wr("first_review_promo_daily.csv", F, rows)
    return rows


# -------------------------------------------- 4. sku_unit_economics_daily
def unit_econ(sku_day, ratios, review, perf_sku):
    F = ["date", "period_label", "internal_sku", "ozon_sku", "seller_revenue", "quantity",
         "actual_commission_ratio", "cogs_unit", "cogs_total", "marketplace_commission",
         "logistics", "other_marketplace_cost", "cpc_spend", "search_promo_spend",
         "first_review_promo_spend", "other_confirmed_marketing_cost",
         "total_marketing_cost_confirmed", "ref_product_cogs_unproven",
         "profit_before_marketing", "profit_after_confirmed_marketing",
         "margin_before_marketing_pct", "margin_after_confirmed_marketing_pct",
         "drr_cpc_pct", "drr_total_marketing_pct", "cogs_status"]
    cpc = defaultdict(float)
    for r in perf_sku:
        if r["internal_sku"]:
            cpc[(r["date"], r["internal_sku"])] += float(r["expense_rub"]) \
                if r["expense_rub"] not in ("NOT_AVAILABLE", "", None) else 0.0
    rev_by = defaultdict(float)
    for r in review:
        rev_by[(r["date"], r["internal_sku"])] += r["cost_rub"]
    # Полное объединение ключей: реклама идёт и в дни без продаж этого SKU.
    # Внутренний join по дате терял бы основную часть рекламного расхода.
    sku_by_isku = {}
    for (ds, sku) in sku_day:
        off, isku = ref(sku)
        if isku:
            sku_by_isku[isku] = sku
    keys = {(ds, sku) for (ds, sku), m in sku_day.items() if m.get("seller_price")}
    for (ds, isku) in list(cpc) + list(rev_by):
        s = sku_by_isku.get(isku)
        if s and (ds, s) not in keys:
            keys.add((ds, s))
    rows = []
    for (ds, sku) in sorted(keys):
        m = sku_day.get((ds, sku), defaultdict(float))
        off, isku = ref(sku)
        if not isku:
            continue
        d = date.fromisoformat(ds)
        label = "A_commission_41" if d <= PERIOD_A[1] else "B_commission_52"
        units = int(m.get("units", 0))
        cu = cogs_at(isku, d)
        rev = round(m.get("seller_price", 0.0), 2)
        comm = round(abs(m.get("commission", 0.0)), 2)
        log = round(abs(m.get("logistics", 0.0)), 2)
        oth = round(abs(m.get("other_mp", 0.0)) + abs(m.get("other_unclassified", 0.0)), 2)
        c_cpc = round(cpc.get((ds, isku), 0.0), 2)
        c_rev = round(rev_by.get((ds, isku), 0.0), 2)
        tot_mkt = round(c_cpc + c_rev, 2)
        rr = ratios.get((ds, sku))
        ratio = round(sum(rr) / len(rr), 6) if rr else "NOT_AVAILABLE"
        if cu is None:
            rows.append(dict(date=ds, period_label=label, internal_sku=isku, ozon_sku=sku,
                             seller_revenue=rev, quantity=units, actual_commission_ratio=ratio,
                             cogs_unit="COGS_MISSING", cogs_total="COGS_MISSING",
                             marketplace_commission=comm, logistics=log,
                             other_marketplace_cost=oth, cpc_spend=c_cpc,
                             search_promo_spend="NOT_APPLICABLE",
                             first_review_promo_spend=c_rev,
                             other_confirmed_marketing_cost=0,
                             total_marketing_cost_confirmed=tot_mkt,
                             ref_product_cogs_unproven="NOT_PROVEN",
                             profit_before_marketing="COGS_MISSING",
                             profit_after_confirmed_marketing="COGS_MISSING",
                             margin_before_marketing_pct="COGS_MISSING",
                             margin_after_confirmed_marketing_pct="COGS_MISSING",
                             drr_cpc_pct=round(c_cpc / rev * 100, 2) if rev else "NOT_APPLICABLE",
                             drr_total_marketing_pct=round(tot_mkt / rev * 100, 2) if rev else "NOT_APPLICABLE",
                             cogs_status="COGS_MISSING"))
            continue
        ct = round(cu * units, 2)
        pbm = round(rev - comm - log - oth - ct, 2)
        pam = round(pbm - tot_mkt, 2)
        rows.append(dict(date=ds, period_label=label, internal_sku=isku, ozon_sku=sku,
                         seller_revenue=rev, quantity=units, actual_commission_ratio=ratio,
                         cogs_unit=cu, cogs_total=ct, marketplace_commission=comm,
                         logistics=log, other_marketplace_cost=oth, cpc_spend=c_cpc,
                         search_promo_spend="NOT_APPLICABLE", first_review_promo_spend=c_rev,
                         other_confirmed_marketing_cost=0,
                         total_marketing_cost_confirmed=tot_mkt,
                         ref_product_cogs_unproven="NOT_PROVEN",
                         profit_before_marketing=pbm, profit_after_confirmed_marketing=pam,
                         margin_before_marketing_pct=round(pbm / rev * 100, 2) if rev else "NOT_APPLICABLE",
                         margin_after_confirmed_marketing_pct=round(pam / rev * 100, 2) if rev else "NOT_APPLICABLE",
                         drr_cpc_pct=round(c_cpc / rev * 100, 2) if rev else "NOT_APPLICABLE",
                         drr_total_marketing_pct=round(tot_mkt / rev * 100, 2) if rev else "NOT_APPLICABLE",
                         cogs_status="OK"))
    wr("sku_unit_economics_daily.csv", F, rows)
    return rows


# ------------------------------------------------------- 5. portfolio_daily
def portfolio(ue, camp_day, review):
    F = ["date", "period_label", "seller_revenue", "quantity", "cogs_total",
         "marketplace_commission", "logistics", "other_marketplace_cost",
         "profit_before_marketing", "cpc_spend", "search_promo_spend",
         "first_review_promo_spend", "total_marketing_cost_confirmed",
         "ref_product_cogs_unproven", "profit_after_confirmed_marketing",
         "margin_before_marketing_pct", "drr_cpc_pct", "drr_total_marketing_pct",
         "sku_with_cogs_missing"]
    agg = defaultdict(lambda: defaultdict(float))
    miss = defaultdict(int)
    for r in ue:
        a = agg[r["date"]]
        a["rev"] += r["seller_revenue"]
        a["qty"] += r["quantity"]
        a["comm"] += r["marketplace_commission"]
        a["log"] += r["logistics"]
        a["oth"] += r["other_marketplace_cost"]
        a["rev_promo"] += r["first_review_promo_spend"]
        if r["cogs_status"] == "OK":
            a["cogs"] += r["cogs_total"]
            a["pbm"] += r["profit_before_marketing"]
        else:
            miss[r["date"]] += 1
    cpc_d, promo_d = defaultdict(float), defaultdict(float)
    for (ds, _u), t in camp_day.items():
        cpc_d[ds] += abs(t.get(41, 0.0))
        promo_d[ds] += abs(t.get(54, 0.0))
    rows = []
    for ds in sorted(set(agg) | set(cpc_d) | set(promo_d)):
        a = agg.get(ds, defaultdict(float))
        rev = round(a["rev"], 2)
        c, p = round(cpc_d[ds], 2), round(promo_d[ds], 2)
        rp = round(a["rev_promo"], 2)
        tot = round(c + p + rp, 2)
        pbm = round(a["pbm"], 2)
        d = date.fromisoformat(ds)
        rows.append(dict(date=ds,
                         period_label="A_commission_41" if d <= PERIOD_A[1] else "B_commission_52",
                         seller_revenue=rev, quantity=int(a["qty"]), cogs_total=round(a["cogs"], 2),
                         marketplace_commission=round(a["comm"], 2), logistics=round(a["log"], 2),
                         other_marketplace_cost=round(a["oth"], 2), profit_before_marketing=pbm,
                         cpc_spend=c, search_promo_spend=p, first_review_promo_spend=rp,
                         total_marketing_cost_confirmed=tot,
                         ref_product_cogs_unproven="NOT_PROVEN",
                         profit_after_confirmed_marketing=round(pbm - tot, 2),
                         margin_before_marketing_pct=round(pbm / rev * 100, 2) if rev else "NOT_APPLICABLE",
                         drr_cpc_pct=round(c / rev * 100, 2) if rev else "NOT_APPLICABLE",
                         drr_total_marketing_pct=round(tot / rev * 100, 2) if rev else "NOT_APPLICABLE",
                         sku_with_cogs_missing=miss.get(ds, 0)))
    wr("portfolio_daily.csv", F, rows)
    return rows


if __name__ == "__main__":
    print("=== BUILD ECONOMICS ===")
    sku_day, ratios, review, camp_day = parse_accruals()
    perf_sku = list(csv.DictReader(open(os.path.join(DATA, "performance_sku_daily.csv"),
                                        encoding="utf-8")))
    marketing_fact(camp_day, review, perf_sku)
    reconcile(camp_day)
    review_daily(review, sku_day)
    ue = unit_econ(sku_day, ratios, review, perf_sku)
    portfolio(ue, camp_day, review)
    print("=== DONE ===")
