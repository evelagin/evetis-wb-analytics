#!/usr/bin/env python3
"""SKU Performance V2 Phase B — регрессия слоя против независимого пересчёта из источников.

Для окна [D0, D1] и списка nm_id (по умолчанию 4 proof-SKU Phase A):
  1) proof_sources.py — прямое чтение FACT_ORDERS / FACT_SALES / V_WB_FINANCE_PRICE_COMPONENTS /
     LONG_MAPPED / V_WB_STORAGE_DAILY / FACT_ADS_SKU_DAILY / V_MART_SKU_DAILY_COGS (одно чтение на величину);
  2) wb_mart.TVF_SKU_PERFORMANCE_V2_PERIOD(D0, D1);
  3) сравнение общих величин до копейки. Только SELECT.
  tools/sku_performance_v2/proof_regression.py 2026-09-01 2026-09-14 [nm_id ...]
Себестоимость сравнивается по дате выкупа (operational) — контракт Executive V2 и слоя.
"""
import json, os, subprocess, sys, urllib.request
from decimal import Decimal as D
HERE = os.path.dirname(os.path.abspath(__file__))
P = 'project-fa311fc0-4d87-4781-986'
D0, D1 = sys.argv[1], sys.argv[2]
NMS = [int(x) for x in sys.argv[3:]] or [252442517, 305101361, 593111986, 567668635]
subprocess.run([sys.executable, os.path.join(HERE, 'proof_sources.py'), D0, D1, *map(str, NMS)], check=True, capture_output=True, cwd=HERE)
src = json.load(open(os.path.join(HERE, f'proof_{D0}_{D1}.json')))['rows']
tok = subprocess.check_output(['gcloud', 'auth', 'print-access-token']).decode().strip()
sql = f"SELECT * FROM `{P}.wb_mart.TVF_SKU_PERFORMANCE_V2_PERIOD`(DATE '{D0}', DATE '{D1}') WHERE nm_id IN UNNEST({NMS}) OR nm_id IS NULL"
body = json.dumps({"query": sql, "useLegacySql": False, "location": "EU", "timeoutMs": 200000}).encode()
d = json.load(urllib.request.urlopen(urllib.request.Request(f'https://www.googleapis.com/bigquery/v2/projects/{P}/queries', data=body,
                                     headers={'Authorization': 'Bearer ' + tok, 'Content-Type': 'application/json'})))
f = [x['name'] for x in d['schema']['fields']]
tvf = {}
for row in d.get('rows', []):
    r = dict(zip(f, [c['v'] for c in row['f']]))
    tvf[r['nm_id'] or 'TOTAL'] = r
PAIRS = [('orders_gross', 'cur_orders_gross_units'), ('cancelled', 'cur_orders_cancelled_units'), ('buyouts', 'cur_buyout_units'),
         ('seller_price_orders_gross_rub', 'cur_orders_gross_seller_price_rub'), ('seller_price_buyouts_rub', 'cur_buyout_seller_price_rub'),
         ('buyer_paid_buyouts_rub', 'cur_buyout_buyer_paid_rub'), ('cohort_orders', 'cur_cohort_orders'), ('cohort_buyout', 'cur_cohort_buyout_orders'),
         ('cohort_cancelled', 'cur_cohort_cancelled_orders'), ('cohort_unresolved', 'cur_cohort_unresolved_orders'),
         ('fin_seller_price_rub', 'cur_fin_seller_price_rub'), ('spp_rub', 'cur_fin_spp_rub'), ('credited_for_goods_rub', 'cur_credited_for_goods_rub'),
         ('wb_remuneration_rub', 'cur_chain_wb_remuneration_rub'), ('wb_remuneration_vat_rub', 'cur_chain_wb_remuneration_vat_rub'),
         ('acquiring_rub', 'cur_chain_acquiring_rub'), ('pvz_reward_rub', 'cur_chain_pvz_reward_rub'), ('logistics_rub', 'cur_logistics_rub'),
         ('storage_rub', 'cur_storage_sku_rub'), ('ads_attributed_rub', 'cur_ads_attributed_rub'), ('cogs_operational_rub', 'cur_cogs_rub')]
num = lambda v: D(v) if v not in (None, '') else D(0)
report, bad = [], 0
for k in [str(n) for n in NMS] + ['TOTAL']:
    s, t = src.get(k, {}), tvf.get(k, {})
    chain_partial = num(s.get('rows_without_chain')) > 0
    for a, b in PAIRS:
        if chain_partial and b.startswith('cur_chain_'):
            # Контракт: в окне есть строки финотчёта без разложения (до 13.07.2026) → компонент NULL, не частичная сумма.
            eq = t.get(b) is None
        else:
            eq = abs(num(s.get(a)) - num(t.get(b))) <= D('0.000001')
        bad += not eq
        report.append({'sku': k, 'source': a, 'layer': b, 'source_value': s.get(a), 'layer_value': t.get(b), 'equal': eq})
out = os.path.join(HERE, f'regression_{D0}_{D1}.json')
json.dump({'window': [D0, D1], 'mismatches': bad, 'checks': len(report), 'report': report, 'tvf': tvf}, open(out, 'w'), ensure_ascii=False, indent=1)
print(f'{D0}..{D1}: checks={len(report)} mismatches={bad} → {out}')
for r in report:
    if not r['equal']: print('MISMATCH', r)
sys.exit(1 if bad else 0)
