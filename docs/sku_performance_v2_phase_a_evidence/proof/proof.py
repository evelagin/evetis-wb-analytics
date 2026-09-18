#!/usr/bin/env python3
"""SKU Performance V2 Phase A — proof values. Read-only: SELECT only, one source per query.
Usage: proof.py D0 D1 [nm_id ...]   (no nm_ids → the 4 proof SKUs); writes proof_<D0>_<D1>.json"""
import sys, json, subprocess, urllib.request
P = 'project-fa311fc0-4d87-4781-986'
D0, D1 = sys.argv[1], sys.argv[2]
NMS = [int(x) for x in sys.argv[3:]] or [252442517, 305101361, 438775437, 567668635]
tok = subprocess.check_output(['gcloud', 'auth', 'print-access-token']).decode().strip()
def q(sql):
    body = json.dumps({"query": sql, "useLegacySql": False, "location": "EU", "timeoutMs": 200000, "maxResults": 10000}).encode()
    r = urllib.request.Request(f'https://www.googleapis.com/bigquery/v2/projects/{P}/queries', data=body,
                               headers={'Authorization': 'Bearer ' + tok, 'Content-Type': 'application/json'})
    try: d = json.load(urllib.request.urlopen(r))
    except urllib.error.HTTPError as e: raise SystemExit(e.read().decode()[:1500] + '\n' + sql)
    f = [x['name'] for x in d['schema']['fields']]
    return [dict(zip(f, [c['v'] for c in row['f']])) for row in d.get('rows', [])]
U = f"(SELECT DISTINCT nm_id FROM `{P}.wb_raw.REF_SKU_MASTER` WHERE marketplace='WB' AND active AND nm_id IS NOT NULL)"
W = f"BETWEEN DATE '{D0}' AND DATE '{D1}'"
KEY = "IF(nm_id IN UNNEST(@n), CAST(nm_id AS STRING), 'OTHER')".replace('@n', json.dumps(NMS))
SQL = {
 # A. заказы: FACT_ORDERS, 1 строка = 1 единица (quantity=1 доказано), дата заказа
 'orders': f"""SELECT {KEY} k, COUNT(*) orders_gross, COUNTIF(is_cancel) cancelled, COUNTIF(NOT is_cancel) orders_net,
     SUM(price_with_disc) seller_price_orders_gross_rub, SUM(IF(NOT is_cancel, price_with_disc, 0)) seller_price_orders_net_rub
   FROM `{P}.wb_mart.FACT_ORDERS` WHERE order_date {W} AND nm_id IN {U} GROUP BY ROLLUP(k)""",
 # B/C. выкупы-события: FACT_SALES (Statistics API), дата выкупа
 'sales': f"""SELECT {KEY} k, COUNTIF(NOT is_return) buyouts, COUNTIF(is_return) returns_,
     SUM(IF(NOT is_return, price_with_disc, 0)) seller_price_buyouts_rub, SUM(IF(NOT is_return, finished_price, 0)) buyer_paid_buyouts_rub,
     SUM(IF(NOT is_return, total_price, 0)) list_price_buyouts_rub
   FROM `{P}.wb_mart.FACT_SALES` WHERE sale_date {W} AND nm_id IN {U} GROUP BY ROLLUP(k)""",
 # F. когорта по дате заказа (методология V_DASH_BUYOUT_COHORT_DAILY + nm_id)
 'cohort': f"""WITH s AS (SELECT NULLIF(TRIM(srid),'') srid, COUNTIF(NOT is_return) se FROM `{P}.wb_raw.V_WB_SALES_RETURNS` GROUP BY 1),
   o AS (SELECT o.nm_id, CASE WHEN IFNULL(s.se,0) > 0 AND o.is_cancel THEN 'CONFLICT' WHEN IFNULL(s.se,0) > 0 THEN 'BUYOUT'
                              WHEN o.is_cancel THEN 'CANCELLED' ELSE 'UNRESOLVED' END oc
         FROM `{P}.wb_mart.FACT_ORDERS` o LEFT JOIN s ON s.srid = o.order_srid WHERE o.order_date {W} AND o.nm_id IN {U})
   SELECT {KEY} k, COUNT(*) cohort_orders, COUNTIF(oc='BUYOUT') cohort_buyout, COUNTIF(oc='CANCELLED') cohort_cancelled,
     COUNTIF(oc='UNRESOLVED') cohort_unresolved, COUNTIF(oc='CONFLICT') cohort_conflict FROM o GROUP BY ROLLUP(k)""",
 # D. цепочка цены: финотчёт, дата финотчёта, Продажа/Возврат, только SKU-строки универсума
 'chain': f"""SELECT {KEY} k, COUNTIF(supplier_oper_name='Продажа') fin_sales, COUNTIF(supplier_oper_name='Возврат') fin_returns,
     COUNTIF(NOT price_chain_available) rows_without_chain,
     SUM(seller_price_rub) fin_seller_price_rub, SUM(buyer_paid_rub) fin_buyer_paid_rub, SUM(spp_rub) spp_rub,
     SUM(wb_remuneration_rub) wb_remuneration_rub, SUM(wb_remuneration_vat_rub) wb_remuneration_vat_rub,
     SUM(acquiring_rub) acquiring_rub, SUM(pvz_reward_rub) pvz_reward_rub, SUM(price_chain_rounding_rub) rounding_rub,
     SUM(marketplace_fee_gap_rub) spread_rub, SUM(for_pay_rub) credited_for_goods_rub
   FROM `{P}.wb_mart.V_WB_FINANCE_PRICE_COMPONENTS` WHERE finance_date {W} AND in_mart_universe GROUP BY ROLLUP(k)""",
 # логистика SKU: финотчёт, cost_category logistics, is_sku_row
 'logistics': f"""SELECT {KEY} k, SUM(cost_amount_positive) logistics_rub, COUNT(*) logistics_rows
   FROM `{P}.wb_mart.V_WB_FINANCE_AMOUNTS_LONG_MAPPED` WHERE finance_date {W} AND is_sku_row AND cost_category='logistics' AND nm_id IN {U} GROUP BY ROLLUP(k)""",
 # хранение SKU: отчёт платного хранения (с 01.09), дата хранения
 'storage': f"""SELECT {KEY} k, SUM(storage_rub_exact) storage_rub, COUNT(*) storage_days
   FROM `{P}.wb_raw.V_WB_STORAGE_DAILY` WHERE date_msk {W} AND nm_id IN {U} GROUP BY ROLLUP(k)""",
 # реклама: атрибуция FACT_ADS_SKU_DAILY, дата активности
 'ads': f"""SELECT {KEY} k, SUM(stats_spend_rub) ads_attributed_rub, COUNT(DISTINCT advert_id) campaigns
   FROM `{P}.wb_mart.FACT_ADS_SKU_DAILY` WHERE date {W} AND nm_id IN {U} GROUP BY ROLLUP(k)""",
 # COGS: V_MART_SKU_DAILY_COGS — по дате выкупа (operational) и по дате финотчёта (settlement)
 'cogs': f"""SELECT {KEY} k, SUM(net_product_cogs_operational_rub) cogs_operational_rub, SUM(product_cogs_settlement_rub) cogs_settlement_rub,
     COUNTIF(cogs_resolution_status NOT IN ('RESOLVED','NOT_APPLICABLE')) cogs_unresolved_days
   FROM `{P}.wb_mart.V_MART_SKU_DAILY_COGS` WHERE day {W} AND nm_id IN {U} GROUP BY ROLLUP(k)""",
 # текущий dashboard 3: V_DASH_SKU_DAILY
 'dash3': f"""SELECT {KEY} k, SUM(orders_gross_qty) d3_orders_gross, SUM(canceled_qty) d3_cancelled, SUM(buyouts_qty) d3_buyouts,
     SUM(sales_revenue_seller_base_rub) d3_revenue_rub, SUM(ad_spend_attributed_rub) d3_ads_rub, SUM(marketplace_fee_rub) d3_marketplace_fee_rub,
     SUM(logistics_rub) d3_logistics_rub, SUM(contribution_pre_cogs_rub) d3_contribution_pre_cogs_rub
   FROM `{P}.wb_mart.V_DASH_SKU_DAILY` WHERE day {W} GROUP BY ROLLUP(k)""",
}
out = {'window': [D0, D1], 'nm_ids': NMS, 'sql': SQL, 'rows': {}}
for name, sql in SQL.items():
    for r in q(sql):
        k = r.pop('k') or 'TOTAL'
        out['rows'].setdefault(k, {}).update(r)
json.dump(out, open(f'proof_{D0}_{D1}.json', 'w'), ensure_ascii=False, indent=1)
print(json.dumps(out['rows'], ensure_ascii=False, indent=1))
