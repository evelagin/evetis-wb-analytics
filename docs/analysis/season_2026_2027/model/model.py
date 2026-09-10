# -*- coding: utf-8 -*-
"""EVETIS season model Sep 2026 - Mar 2027. READ-ONLY inputs pulled from BigQuery SELECTs (CSV in this dir)
plus documented fulfilment stock (docs/FF_OPERATIONAL_STOCK_2026-09-09.md). All assumptions are tagged in ASSUMPTIONS."""
import csv, json, math, os, collections
from collections import OrderedDict as OD

OUT = 'out'; os.makedirs(OUT, exist_ok=True)
def rd(fn):
    with open(fn, newline='') as f: return list(csv.DictReader(f))
def wr(fn, rows, hdr=None):
    if not rows: return
    hdr = hdr or list(rows[0].keys())
    with open(os.path.join(OUT, fn), 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=hdr, extrasaction='ignore'); w.writeheader(); w.writerows(rows)
    print('wrote', fn, len(rows))

ASSUMPTIONS = []
def A(tag, text): ASSUMPTIONS.append((tag, text))

# ---------------------------------------------------------------- master data
BASE = ['EVT-FS-ACNE-30','EVT-FS-MOIST-30','EVT-FC-ACNE-50','EVT-FC-MOIST-50','EVT-FT-ACNE-150','EVT-FT-MOIST-150',
        'EVT-EP-ENZYME-75','EVT-HC-HAND-300','EVT-HC-CHERRY-300','EVT-HC-AMBER-300','EVT-HC-BODY-300']
NAME = {'EVT-FS-ACNE-30':'Сыворотка АКНЕ 30 мл','EVT-FS-MOIST-30':'Сыворотка УВЛ 30 мл','EVT-FC-ACNE-50':'Крем для лица АКНЕ 50 мл',
 'EVT-FC-MOIST-50':'Крем для лица УВЛ 50 мл','EVT-FT-ACNE-150':'Тоник АКНЕ 150 мл','EVT-FT-MOIST-150':'Тоник УВЛ 150 мл',
 'EVT-EP-ENZYME-75':'Энзимная пудра 75 г','EVT-HC-HAND-300':'Крем для рук Oud Wood 300 мл','EVT-HC-CHERRY-300':'Крем для тела Lost Cherry 300 мл',
 'EVT-HC-AMBER-300':'Крем для тела Amber Vanilla 300 мл','EVT-HC-BODY-300':'Крем для тела Oud Wood 300 мл',
 'EVT-SET-4PC-ACNE':'Набор 4 шага АКНЕ','EVT-SET-SER-CREAM-ACNE':'Набор сыв+крем АКНЕ','EVT-SET-TON-SER-CREAM-ACNE':'Набор тон+сыв+крем АКНЕ',
 'EVT-SET-ACNE-POWDER-SERUM-CREAM':'Набор пудра+сыв+крем АКНЕ','EVT-SET-ACNE-TONIC-SERUM':'Набор тоник+сыв АКНЕ','EVT-SET-TON-CREAM-ACNE':'Набор тон+крем АКНЕ',
 'EVT-SET-SER-CREAM-MOIST':'Набор сыв+крем УВЛ','EVT-SET-TON-CREAM-MOIST':'Набор тон+крем УВЛ','EVT-SET-TON-SER-CREAM-MOIST':'Набор тон+сыв+крем УВЛ',
 'EVT-SET-MOIST-TONIC-SERUM':'Набор тоник+сыв УВЛ','EVT-SET-HAND-CHERRY':'Набор руки+Lost Cherry','EVT-SET-HAND-AMBER':'Набор руки+Amber',
 'EVT-SET-CHERRY-AMBER':'Набор Cherry+Amber','EVT-SET-HAND-BODY':'Набор руки+тело'}
LINE = {'EVT-FS-ACNE-30':'ACNE','EVT-FC-ACNE-50':'ACNE','EVT-FT-ACNE-150':'ACNE','EVT-EP-ENZYME-75':'ACNE',
        'EVT-FS-MOIST-30':'MOIST','EVT-FC-MOIST-50':'MOIST','EVT-FT-MOIST-150':'MOIST',
        'EVT-HC-HAND-300':'BODY','EVT-HC-CHERRY-300':'BODY','EVT-HC-AMBER-300':'BODY','EVT-HC-BODY-300':'BODY'}
BOM = {  # FACT: evetis_ref.REF_BUNDLE_COMPONENTS, all component_qty = 1
 'EVT-SET-4PC-ACNE':['EVT-EP-ENZYME-75','EVT-FC-ACNE-50','EVT-FS-ACNE-30','EVT-FT-ACNE-150'],
 'EVT-SET-SER-CREAM-ACNE':['EVT-FC-ACNE-50','EVT-FS-ACNE-30'],
 'EVT-SET-TON-SER-CREAM-ACNE':['EVT-FC-ACNE-50','EVT-FS-ACNE-30','EVT-FT-ACNE-150'],
 'EVT-SET-ACNE-POWDER-SERUM-CREAM':['EVT-EP-ENZYME-75','EVT-FC-ACNE-50','EVT-FS-ACNE-30'],
 'EVT-SET-ACNE-TONIC-SERUM':['EVT-FS-ACNE-30','EVT-FT-ACNE-150'],
 'EVT-SET-TON-CREAM-ACNE':['EVT-FC-ACNE-50','EVT-FT-ACNE-150'],
 'EVT-SET-SER-CREAM-MOIST':['EVT-FC-MOIST-50','EVT-FS-MOIST-30'],
 'EVT-SET-TON-CREAM-MOIST':['EVT-FC-MOIST-50','EVT-FT-MOIST-150'],
 'EVT-SET-TON-SER-CREAM-MOIST':['EVT-FC-MOIST-50','EVT-FS-MOIST-30','EVT-FT-MOIST-150'],
 'EVT-SET-MOIST-TONIC-SERUM':['EVT-FS-MOIST-30','EVT-FT-MOIST-150'],
 'EVT-SET-HAND-CHERRY':['EVT-HC-HAND-300','EVT-HC-CHERRY-300'],
 'EVT-SET-HAND-AMBER':['EVT-HC-HAND-300','EVT-HC-AMBER-300'],
 'EVT-SET-CHERRY-AMBER':['EVT-HC-CHERRY-300','EVT-HC-AMBER-300'],
 'EVT-SET-HAND-BODY':['EVT-HC-HAND-300','EVT-HC-BODY-300'],
}
SETS = list(BOM.keys()); ALL = BASE + SETS
def comps(sku): return BOM.get(sku, [sku])

# ---------------------------------------------------------------- inventory 09.09.2026
# FACT: LK Usend 09.09 minus supplies already shipped (docs/FF_OPERATIONAL_STOCK_2026-09-09.md); after today's 5 Ozon supplies.
FF_OPER = {'EVT-FS-MOIST-30':277,'EVT-EP-ENZYME-75':77,'EVT-HC-CHERRY-300':77,'EVT-HC-AMBER-300':89,'EVT-FT-ACNE-150':36,
           'EVT-FT-MOIST-150':52,'EVT-FS-ACNE-30':0,'EVT-HC-HAND-300':0,'EVT-FC-MOIST-50':2,'EVT-FC-ACNE-50':0,'EVT-HC-BODY-300':0}
A('ASSUMPTION','FF operational shelf = LK 09.09 minus 4 supplies IN_TRANSIT (03.09, 178 units) minus 5 supplies shipped 09.09 (191 of 314 units; 123 short per FF letter). Not yet confirmed by a fresh FF snapshot.')
# FACT: docs/FBS_OPENING_STOCK_2026-09-03.md block B (ИП Елагина ДХ.xlsx) minus ~40 undocumented pick of acne cream in Sep.
FF_LT = {'EVT-FS-ACNE-30':5200,'EVT-HC-AMBER-300':4400,'EVT-HC-CHERRY-300':4320,'EVT-EP-ENZYME-75':3630,'EVT-FS-MOIST-30':3600,
         'EVT-FT-ACNE-150':3600,'EVT-FT-MOIST-150':3600,'EVT-HC-HAND-300':2720,'EVT-FC-ACNE-50':1077,'EVT-FC-MOIST-50':0,'EVT-HC-BODY-300':0}
A('INFERENCE','Long-term (pallet) stock of acne face cream reduced 1 117 -> 1 077 for the undocumented pick under 03.09 Ozon supplies (LK card shows -32).')
# in transit to Ozon FBO (base units): 03.09 IN_TRANSIT 178 + 09.09 shipped 191
TRANSIT = {'EVT-FS-ACNE-30':40+23,'EVT-HC-HAND-300':24+16,'EVT-FC-ACNE-50':40+0,'EVT-FT-ACNE-150':23+40,'EVT-EP-ENZYME-75':11+30,
           'EVT-HC-CHERRY-300':7+32,'EVT-FS-MOIST-30':19+15,'EVT-FT-MOIST-150':10+15,'EVT-HC-AMBER-300':4+20,'EVT-FC-MOIST-50':0,'EVT-HC-BODY-300':0}
wb_stock = {r['internal_sku']: r for r in rd('wb_stock.csv')}
oz_stock = {r['internal_sku']: int(r['qty']) for r in rd('ozon_stock.csv')}
INBOUND = {'EVT-FC-MOIST-50': 5000}  # scenario B only
A('FACT','Inbound: 5 000 units EVT-FC-MOIST-50 produced in China (batch 8), advance 225 792 RUB paid 09.06.2026; ~1 month lead after release order.')

def base_units_from_cards(card_dict):
    out = collections.Counter()
    for sku, q in card_dict.items():
        for c in comps(sku): out[c] += q
    return out
wb_live_cards = {s: int(r['live_agg']) for s, r in wb_stock.items()}
wb_lost_cards = {s: int(r['lost_claimed']) for s, r in wb_stock.items()}
wb_live_base = base_units_from_cards(wb_live_cards)
wb_lost_base = base_units_from_cards(wb_lost_cards)
oz_base = base_units_from_cards(oz_stock)
wb_live_sets = {s: q for s, q in wb_live_cards.items() if s in BOM and q}
oz_sets = {s: q for s, q in oz_stock.items() if s in BOM and q}

cogs = {r['internal_sku']: float(r['cogs']) for r in rd('wb_econ.csv')}
cogs['EVT-HC-BODY-300'] = 205.88  # documented batch 1 cost (no current record in BQ); zero stock anyway
A('FACT','COGS per SKU = evetis_ref.V_PRODUCT_COGS_EFFECTIVE via wb_mart.V_WB_SKU_FORWARD_ECONOMICS_CURRENT (07.09.2026); bundle COGS = sum of components.')

inv_rows = []
tot = collections.Counter()
for s in BASE:
    ff_o, ff_lt, tr = FF_OPER.get(s,0), FF_LT.get(s,0), TRANSIT.get(s,0)
    wbl, wbx, ozn = wb_live_base.get(s,0), wb_lost_base.get(s,0), oz_base.get(s,0)
    sellable = ff_o + ff_lt + tr + wbl + ozn
    inb = INBOUND.get(s,0)
    r = OD(internal_sku=s, product_name=NAME[s], line=LINE[s], ff_operational=ff_o, ff_long_term=ff_lt, in_transit_to_ozon=tr,
           wb_fbo_live=wbl, ozon_fbo=ozn, fbs=0, wb_lost_claimed=wbx, physical_sets_note='см. столбцы sets ниже',
           inbound_scenario_B=inb, total_sellable_units=sellable, total_incl_lost=sellable+wbx,
           cogs_rub=round(cogs[s],2), sellable_value_rub=round(sellable*cogs[s]), lost_value_rub=round(wbx*cogs[s]))
    inv_rows.append(r)
    for k in ['ff_operational','ff_long_term','in_transit_to_ozon','wb_fbo_live','ozon_fbo','wb_lost_claimed','total_sellable_units','sellable_value_rub','lost_value_rub','inbound_scenario_B']:
        tot[k] += r[k]
inv_rows.append(OD(internal_sku='TOTAL', product_name='', line='', **{k: tot[k] for k in ['ff_operational','ff_long_term','in_transit_to_ozon','wb_fbo_live','ozon_fbo']}, fbs=0,
                   wb_lost_claimed=tot['wb_lost_claimed'], physical_sets_note='', inbound_scenario_B=tot['inbound_scenario_B'], total_sellable_units=tot['total_sellable_units'],
                   total_incl_lost=tot['total_sellable_units']+tot['wb_lost_claimed'], cogs_rub='', sellable_value_rub=tot['sellable_value_rub'], lost_value_rub=tot['lost_value_rub']))
wr('INVENTORY_TRUTH.csv', inv_rows)
# physical sets
set_rows = []
for s in SETS:
    set_rows.append(OD(set_sku=s, name=NAME[s], bom=' + '.join(comps(s)), units_per_set=len(comps(s)), wb_fbo_live_sets=wb_live_cards.get(s,0),
                       wb_lost_sets=wb_lost_cards.get(s,0), ozon_fbo_sets=oz_stock.get(s,0), ff_assembled_sets='MISSING (ФФ не отдаёт срез наборов; наряд 03.09 на сборку не подтверждён)',
                       assembly_model='FF_ASSEMBLED on demand', cogs_rub=round(sum(cogs[c] for c in comps(s)),2)))
wr('PHYSICAL_SETS.csv', set_rows)
INV = {r['internal_sku']: r for r in inv_rows if r['internal_sku'] != 'TOTAL'}
print('SELLABLE TOTAL', tot['total_sellable_units'], 'value', tot['sellable_value_rub'], 'lost', tot['wb_lost_claimed'])

# ---------------------------------------------------------------- history
wbfin = rd('wb_fin_monthly.csv'); wbmart = rd('wb_mart_monthly.csv'); ozm = rd('ozon_monthly.csv'); ozads = rd('ozon_ads.csv')
hist = collections.defaultdict(lambda: collections.defaultdict(float))  # (ym, sku, channel) -> metrics
for r in wbfin:
    k = (r['ym'], r['internal_sku'], 'WB'); h = hist[k]
    h['sold_qty'] += int(r['sold_qty']); h['sold_rub'] += float(r['sold_rub']); h['for_pay'] += float(r['for_pay']); h['ret_qty'] += int(r['ret_qty']); h['logistics'] += float(r['logi'])
for r in wbmart:
    k = (r['ym'], r['internal_sku'], 'WB'); h = hist[k]
    h['ads'] += float(r['ads']); h['orders'] += int(r['orders']); h['cancel'] += int(r['canc'])
for r in ozm:
    k = (r['ym'], r['internal_sku'], 'OZON'); h = hist[k]
    h['sold_qty'] += int(r['net_qty']); h['sold_rub'] += float(r['net_rub']); h['for_pay'] += float(r['payout']); h['cancel'] += int(r['canc_qty']); h['orders'] += int(r['net_qty'])+int(r['canc_qty'])
for r in ozads:
    k = (r['ym'], r['internal_sku'], 'OZON'); hist[k]['ads'] += float(r['spend'])
hist_rows = []
for (ym, sku, ch), h in sorted(hist.items()):
    if sku.startswith('UNMAPPED'): continue
    q = h['sold_qty']; c = cogs.get(sku, 0)
    hist_rows.append(OD(ym=ym, internal_sku=sku, channel=ch, orders=int(h['orders']), cancels=int(h['cancel']), sold_units=int(q), returns=int(h['ret_qty']),
        gmv_rub=round(h['sold_rub']), asp_rub=round(h['sold_rub']/q) if q else '', seller_payout_rub=round(h['for_pay']), ads_rub=round(h['ads']),
        cogs_rub=round(q*c), contribution_after_ads_rub=round(h['for_pay']-h['ads']-q*c) if ch=='WB' else round(h['for_pay']-h['ads']-q*c),
        contribution_pct=round((h['for_pay']-h['ads']-q*c)/h['sold_rub']*100,1) if h['sold_rub'] else '',
        note='WB: payout = for_pay (после комиссии), логистика/хранение НЕ вычтены; ads с 04.2026. OZON: payout из постингов (после комиссии), логистика не вычтена; ads = attributed_spend' ))
wr('SKU_SALES_HISTORY.csv', hist_rows)

# ---------------------------------------------------------------- velocity + classification
wbv = {r['internal_sku']: r for r in rd('wb_vel.csv')}; ozv = {r['internal_sku']: r for r in rd('ozon_vel.csv')}
def m(sku, ym, ch): return hist[(ym, sku, ch)]['sold_qty']
vel_rows = []; VEL = {}
for s in ALL:
    w = wbv.get(s, {}); o = ozv.get(s, {})
    g = lambda d, k: int(d.get(k, 0))
    wb30, wb90 = g(w,'o30'), g(w,'o90'); oz30, oz90 = g(o,'o30'), g(o,'o90')
    wb_full = (m(s,'2026-04','WB') + m(s,'2026-05','WB'))/2   # full-availability reference, spring 2026
    oz_ref = max(m(s,'2026-08','OZON'), oz90/3)
    stock_now = INV[s]['total_sellable_units'] if s in INV else min(INV[c]['total_sellable_units'] for c in comps(s))
    mp_stock = (wb_live_cards.get(s,0) + oz_stock.get(s,0))
    d7 = (g(w,'o7')+g(o,'o7'))/7; d30 = (wb30+oz30)/30; d90 = (wb90+oz90)/90
    trend = 'DECEL' if d7 < 0.7*d30 else ('ACCEL' if d7 > 1.3*d30 else 'FLAT')
    months_cover = stock_now/((wb90+oz90)/3) if (wb90+oz90) else 999
    # class
    if s == 'EVT-HC-BODY-300' or s == 'EVT-SET-HAND-BODY': cls = 'X-NO_STOCK'
    elif s == 'EVT-FC-MOIST-50' or s in ('EVT-SET-SER-CREAM-MOIST','EVT-SET-TON-CREAM-MOIST','EVT-SET-TON-SER-CREAM-MOIST'): cls = 'R-REPLENISH (inbound 5000)'
    elif s == 'EVT-HC-HAND-300': cls = 'D-LIQUIDATE (expiry, negative margin at realized ASP)'
    elif s in BOM:
        cls = 'A-HERO_BUNDLE' if s in ('EVT-SET-SER-CREAM-ACNE','EVT-SET-4PC-ACNE','EVT-SET-TON-SER-CREAM-ACNE') else ('E-INVENTORY_RELEASE_BUNDLE' if s in ('EVT-SET-CHERRY-AMBER','EVT-SET-HAND-CHERRY','EVT-SET-HAND-AMBER','EVT-SET-MOIST-TONIC-SERUM','EVT-SET-ACNE-TONIC-SERUM') else 'C-SLOW_BUNDLE')
    elif s in ('EVT-FS-ACNE-30','EVT-FC-ACNE-50','EVT-FS-MOIST-30'): cls = 'A-FAST (stock-limited now)' if s != 'EVT-FS-ACNE-30' else 'B-HEALTHY + E-bundle enabler'
    elif s in ('EVT-FT-ACNE-150','EVT-EP-ENZYME-75','EVT-FT-MOIST-150'): cls = 'C-SLOW / E-bundle enabler'
    else: cls = 'D-OVERSTOCK (Cherry/Amber 190+ months) / E-bundle enabler'
    VEL[s] = dict(wb30=wb30, wb90=wb90, oz30=oz30, oz90=oz90, wb_full=wb_full, oz_ref=oz_ref, cls=cls)
    vel_rows.append(OD(internal_sku=s, name=NAME[s], is_bundle=s in BOM, wb_orders_7d=g(w,'o7'), wb_14d=g(w,'o14'), wb_30d=wb30, wb_60d=g(w,'o60'), wb_90d=wb90,
        ozon_7d=g(o,'o7'), ozon_14d=g(o,'o14'), ozon_30d=oz30, ozon_60d=g(o,'o60'), ozon_90d=oz90,
        units_per_day_7d=round(d7,2), units_per_day_30d=round(d30,2), units_per_day_90d=round(d90,2), trend=trend,
        wb_full_availability_ref_per_month=round(wb_full,1), ozon_ref_per_month=round(oz_ref,1),
        marketplace_stock_cards=mp_stock, sellable_stock_base=stock_now, months_of_cover_at_90d_rate=round(months_cover,1), velocity_class=cls))
wr('SKU_VELOCITY.csv', vel_rows)

# ---------------------------------------------------------------- unit economics
wbe = {r['internal_sku']: r for r in rd('wb_econ.csv')}; oze = {r['internal_sku']: r for r in rd('ozon_prices.csv')}
WB_STORAGE = 20.5; A('FACT','WB storage 20.50 RUB/unit = fact of last 180 days (memory wb-unit-economics); applied per card.')
A('ASSUMPTION','Ozon variable costs per card: 52% commission (FACT since 28.08), last mile 25 (FACT, prices API), direct flow = fbo_direct_flow_trans_min (56-75, FACT), acquiring 1.0% (FACT ~0.75-1%), returns/cancel allowance 1.5% of price (ASSUMPTION).')
def wb_unit(sku, price=None, drr=0.0):
    e = wbe[sku]; P = price if price is not None else float(e['price'])
    comm = P*float(e['comm_pct'])/100; acq = P*0.0354
    logi = float(e['logi']) if float(e['logi'])>0 else 60.0
    stor = WB_STORAGE*(1.5 if sku in BOM else 1.0)
    ads = P*drr; c = cogs[sku]
    proceeds = P - comm - acq - logi - stor - ads   # cash to seller after all marketplace + ads
    return dict(price=P, commission=comm, acquiring=acq, logistics=logi, storage=stor, ads=ads, cogs=c, cash_proceeds=proceeds, contribution=proceeds-c, contribution_pct=(proceeds-c)/P if P else 0)
def oz_unit(sku, price=None, drr=0.0):
    e = oze.get(sku);
    if e is None: return None
    P = price if price is not None else float(e['price'])
    comm = P*0.52; last = 25.0; logi = float(e['logi_min']); acq = P*0.01; ret = P*0.015; ads = P*drr; c = cogs[sku]
    proceeds = P - comm - last - logi - acq - ret - ads
    return dict(price=P, commission=comm, acquiring=acq, logistics=logi+last, storage=0.0, returns_allow=ret, ads=ads, cogs=c, cash_proceeds=proceeds, contribution=proceeds-c, contribution_pct=(proceeds-c)/P if P else 0)

def price_for_margin(fn, sku, target_pct, drr):
    # solve price so that contribution_pct == target (linear in P)
    lo, hi = 100.0, 20000.0
    for _ in range(60):
        mid = (lo+hi)/2; u = fn(sku, mid, drr)
        if u is None: return None
        if u['contribution_pct'] < target_pct: lo = mid
        else: hi = mid
    return round(hi)

pm_rows = []
for s in ALL:
    if s in ('EVT-HC-BODY-300','EVT-SET-HAND-BODY'): continue
    for ch, fn in (('WB', wb_unit), ('OZON', oz_unit)):
        u = fn(s, None, 0.0)
        if u is None: continue
        if s == 'EVT-SET-ACNE-POWDER-SERUM-CREAM' and ch == 'WB': continue  # placeholder price 6000, not sold on WB
        drr_ref = 0.10
        u10 = fn(s, None, drr_ref)
        row = OD(internal_sku=s, name=NAME[s], channel=ch, current_price=round(u['price']), commission=round(u['commission'],1), acquiring=round(u['acquiring'],1),
            logistics=round(u['logistics'],1), storage=round(u['storage'],1), cogs=round(u['cogs'],1),
            contribution_before_ads=round(u['contribution']), contribution_before_ads_pct=round(u['contribution_pct']*100,1),
            contribution_at_drr10=round(u10['contribution']), contribution_pct_at_drr10=round(u10['contribution_pct']*100,1),
            max_drr_for_20pct=round(max(0,(u['contribution_pct']-0.20))*100,1), max_drr_breakeven=round(max(0,u['contribution_pct'])*100,1),
            price_target20_at_drr10=price_for_margin(fn, s, 0.20, drr_ref), price_margin15_at_drr10=price_for_margin(fn, s, 0.15, drr_ref),
            price_margin10_at_drr10=price_for_margin(fn, s, 0.10, drr_ref), price_breakeven_at_drr10=price_for_margin(fn, s, 0.0, drr_ref),
            price_breakeven_no_ads=price_for_margin(fn, s, 0.0, 0.0),
            liquidation_floor_cash_positive=round((u['logistics']+u['storage']+ (u.get('returns_allow',0)))/(1-0.4225-0.0354) if ch=='WB' else (u['logistics']+u['storage'])/(1-0.52-0.025)))
        pm_rows.append(row)
wr('PRICE_MARGIN_MATRIX.csv', pm_rows)
PM = {(r['internal_sku'], r['channel']): r for r in pm_rows}

# ---------------------------------------------------------------- seasonality (ASSUMPTION, derived from 2025/26 WB season)
MONTHS = ['2026-09','2026-10','2026-11','2026-12','2027-01','2027-02','2027-03']
SEAS = {'CONSERVATIVE':[0.95,1.00,1.05,1.50,0.80,1.00,1.20],
        'BASE':        [0.95,1.00,1.15,2.00,0.85,1.05,1.50],
        'AGGRESSIVE':  [1.00,1.10,1.40,2.30,0.90,1.20,1.60]}
A('ASSUMPTION','Seasonality index vs Sep-Oct level, from WB season 2025/26 (Dec x2.30, Mar x1.53, Jan x0.77 vs Sep-Nov avg): CONSERVATIVE Dec 1.5/Mar 1.2, BASE Dec 2.0/Mar 1.5, AGGRESSIVE Nov 1.4 (11.11+BF)/Dec 2.3/Mar 1.6. Only one comparable season exists; 2024/25 was a launch ramp.')
SEP_FRACTION = 22/30  # from 09.09
# SKU-aware advertising: DRR = clamp(contribution_before_ads_pct - margin_floor, 0, cap). Floors keep the after-ads margin; caps stop runaway spend.
DRR_RULE = {'CONSERVATIVE': dict(floor=0.15, cap=0.10), 'BASE': dict(floor=0.10, cap=0.12), 'AGGRESSIVE': dict(floor=0.05, cap=0.15)}
LIQ_FLOOR = {'CONSERVATIVE': 0.0, 'BASE': 0.0, 'AGGRESSIVE': -0.03}   # hand cream: break-even (or -3% in aggressive)
A('ASSUMPTION','Advertising DRR is SKU- and channel-specific = clamp(contribution_before_ads% - margin floor, 0, cap): MARGIN_PROTECTION floor 15%/cap 10%; BALANCED floor 10%/cap 12%; AGGRESSIVE floor 5%/cap 15%. Hand cream floor 0% (break-even liquidation), -3% in AGGRESSIVE. Fact Jun-Aug 2026: WB DRR 20-25%, Ozon 23% produced negative contribution on 5 SKUs.')
def drr_for(s, ch, scen, P, fn):
    u0 = fn(s, P, 0.0)
    if u0 is None: return 0.0
    r = DRR_RULE[scen]; floor = LIQ_FLOOR[scen] if s == 'EVT-HC-HAND-300' else r['floor']
    return max(0.0, min(r['cap'], u0['contribution_pct'] - floor))

# base monthly demand (cards) per SKU per channel at full availability
def base_demand(s, scen):
    v = VEL[s]
    wb_cur = v['wb90']/3; oz_cur = v['oz90']/3
    if scen == 'CONSERVATIVE':
        wb, oz = wb_cur, oz_cur
    elif scen == 'BASE':
        wb = max(wb_cur, 0.85*v['wb_full']); oz = max(oz_cur, v['oz_ref'])*1.15
    else:
        wb = max(wb_cur, 0.85*v['wb_full'])*1.30; oz = max(oz_cur, v['oz_ref'])*1.50
    # special cases
    if s == 'EVT-FC-MOIST-50':   # inbound-dependent; historical WB 200-670/mo, Ozon 12-64
        wb = {'CONSERVATIVE':180,'BASE':240,'AGGRESSIVE':320}[scen]; oz = {'CONSERVATIVE':20,'BASE':35,'AGGRESSIVE':50}[scen]
    if s in ('EVT-SET-SER-CREAM-MOIST','EVT-SET-TON-CREAM-MOIST','EVT-SET-TON-SER-CREAM-MOIST'):
        ref = {'EVT-SET-SER-CREAM-MOIST':(45,3),'EVT-SET-TON-CREAM-MOIST':(20,2),'EVT-SET-TON-SER-CREAM-MOIST':(40,3)}[s]
        wb, oz = ref[0]*{'CONSERVATIVE':0.7,'BASE':1.0,'AGGRESSIVE':1.4}[scen], ref[1]*{'CONSERVATIVE':0.7,'BASE':1.0,'AGGRESSIVE':1.4}[scen]
    if s == 'EVT-HC-HAND-300':   # explicit liquidation targets (see HAND_CREAM_PLAN)
        wb, oz = {'CONSERVATIVE':(130,15),'BASE':(200,45),'AGGRESSIVE':(280,80)}[scen]
    if s in ('EVT-SET-HAND-CHERRY','EVT-SET-HAND-AMBER','EVT-SET-CHERRY-AMBER') and scen != 'CONSERVATIVE':
        k = 2.0 if scen == 'BASE' else 3.5   # bundles get traffic (inventory-release lever)
        wb, oz = wb*k, oz*k
    if s == 'EVT-SET-ACNE-POWDER-SERUM-CREAM': wb = 0  # not listed on WB (BLOCKED in economics view)
    if s in ('EVT-HC-BODY-300','EVT-SET-HAND-BODY'): wb = oz = 0
    return wb, oz
A('ASSUMPTION','Base demand: CONSERVATIVE = last-90-day order rate as is (stock-constrained). BASE = max(90d rate, 85% of Apr-May 2026 full-availability rate) for WB; Ozon = max(Aug 2026, 90d avg) x1.15 (growth x2/mo in Jun-Aug damped). AGGRESSIVE = BASE x1.30 WB / x1.50 Ozon, body bundles x3.5 (traffic + promo). Hand cream and moisture line set explicitly.')
A('ASSUMPTION','Inbound 5 000 moisture face cream: available at FF from 20.10.2026 (30 days), on WB/Ozon from November (acceptance lead). Scenario A = no import.')

def price_factor(s, scen, ym):
    # promo depth by scenario and SKU class
    promo_m = ym in ('2026-11','2026-12','2027-03')
    if s == 'EVT-HC-HAND-300': return {'CONSERVATIVE':1.0,'BASE':1.0,'AGGRESSIVE':0.94}[scen]
    if VEL[s]['cls'].startswith('D-OVERSTOCK') or VEL[s]['cls'].startswith('E-'):
        return {'CONSERVATIVE':1.0,'BASE':0.95 if promo_m else 1.0,'AGGRESSIVE':0.90 if promo_m else 0.95}[scen]
    if VEL[s]['cls'].startswith('C-'):
        return {'CONSERVATIVE':1.0,'BASE':0.95 if promo_m else 1.0,'AGGRESSIVE':0.92 if promo_m else 0.97}[scen]
    return {'CONSERVATIVE':1.0,'BASE':1.0,'AGGRESSIVE':0.95 if promo_m else 1.0}[scen]
A('ASSUMPTION','Promo price factors: BASE -5% on slow/overstock SKUs in Nov/Dec/Mar; AGGRESSIVE -8..-10% on slow/overstock and -6% hand cream all season; hero SKUs never below -5%. Volume response to price is embedded in scenario velocities, not modelled by elasticity (no reliable EVETIS elasticity data).')

# ---------------------------------------------------------------- runout simulation
def simulate(scen, inbound_on):
    stock = {s: INV[s]['total_sellable_units'] for s in BASE}
    rows = []; agg = collections.defaultdict(float); month_rows = []
    bundle_sold = collections.Counter(); sku_month_sales = collections.defaultdict(dict)
    for i, ym in enumerate(MONTHS):
        seas = SEAS[scen][i]; frac = SEP_FRACTION if i == 0 else 1.0
        inb = {s: INBOUND.get(s,0) if (inbound_on and ym == '2026-10') else 0 for s in BASE}
        for s in BASE: stock[s] += inb[s]
        # planned card demand
        demand = {}
        for s in ALL:
            wb, oz = base_demand(s, scen)
            if s == 'EVT-FC-MOIST-50' or (s in BOM and 'EVT-FC-MOIST-50' in comps(s)):
                if not inbound_on or ym in ('2026-09','2026-10'):  # only the 2 units on shelf
                    wb, oz = 0, 0
            if s in BOM and s not in oze: oz = 0
            demand[s] = (wb*seas*frac, oz*seas*frac)
        # allocate stock: sets first? No - proportional cut when component short. Iterate: compute required per component, scale down all users of a short component.
        scale = {s: 1.0 for s in ALL}
        for _ in range(20):
            need = collections.Counter()
            for s in ALL:
                q = (demand[s][0]+demand[s][1])*scale[s]
                for c in comps(s): need[c] += q
            changed = False
            for c in BASE:
                if need[c] > stock[c] + 1e-9:
                    f = stock[c]/need[c] if need[c] else 0
                    for s in ALL:
                        if c in comps(s) and scale[s] > f*scale[s]:
                            scale[s] *= f; changed = True
            if not changed: break
        consumed = collections.Counter(); solo = collections.Counter(); via_b = collections.Counter()
        for s in ALL:
            wbq = demand[s][0]*scale[s]; ozq = demand[s][1]*scale[s]
            if oz_unit(s, None, 0.0) is None: ozq = 0.0   # not listed on Ozon -> no sales, no consumption
            pf = price_factor(s, scen, ym)
            for ch, q, fn in (('WB', wbq, wb_unit), ('OZON', ozq, oz_unit)):
                if q <= 0: continue
                u = fn(s, None, 0.0)
                if u is None: continue
                P = u['price']*pf
                drr = drr_for(s, ch, scen, P, fn)
                u = fn(s, P, drr)
                gmv = q*P; proceeds = q*u['cash_proceeds']; ads = q*u['ads']; cg = q*u['cogs']; contrib = proceeds - cg
                mp_cost = q*(u['commission']+u['acquiring']+u['logistics']+u['storage']+u.get('returns_allow',0))
                rows.append(OD(scenario=scen, inbound='B' if inbound_on else 'A', ym=ym, internal_sku=s, channel=ch, units=round(q,1), price=round(P), gmv=round(gmv), marketplace_costs=round(mp_cost), ads=round(ads), seller_cash=round(proceeds), cogs=round(cg), contribution=round(contrib)))
                for k, v in (('units',q),('gmv',gmv),('mp',mp_cost),('ads',ads),('cash',proceeds),('cogs',cg),('contrib',contrib)): agg[(ym,k)] += v; agg[('TOTAL',k)] += v
                if s in BOM: agg[(ym,'bundles')] += q; agg[('TOTAL','bundles')] += q; bundle_sold[s] += q
            tq = wbq + ozq
            sku_month_sales[s][ym] = tq
            for c in comps(s):
                consumed[c] += tq
                if s in BOM: via_b[c] += tq
                else: solo[c] += tq
        for s in BASE:
            opening = stock[s]
            stock[s] = max(0.0, stock[s] - consumed[s])
            month_rows.append(OD(scenario=scen, inbound_scn='B' if inbound_on else 'A', ym=ym, internal_sku=s, name=NAME[s], opening=round(opening - inb[s]), inbound_units=inb[s], solo_units=round(solo[s]), units_via_bundles=round(via_b[s]), closing=round(stock[s]), days_of_stock_at_month_rate=round(stock[s]/(consumed[s]/30),0) if consumed[s] else ''))
    return rows, month_rows, agg, bundle_sold, sku_month_sales

RESULTS = {}
all_rows = []; all_month = []; scen_rows = []; fc_rows = []
for scen in ('CONSERVATIVE','BASE','AGGRESSIVE'):
    for inb in (False, True):
        rows, mrows, agg, bsold, sms = simulate(scen, inb)
        RESULTS[(scen, inb)] = (rows, mrows, agg, bsold, sms)
        all_rows += rows; all_month += mrows
        open_units = tot['total_sellable_units'] + (INBOUND['EVT-FC-MOIST-50'] if inb else 0)
        closing = sum(r['closing'] for r in mrows if r['ym']=='2027-03')
        units = agg[('TOTAL','units')]
        # base units sold
        base_sold = open_units - closing
        scen_rows.append(OD(scenario=scen, inbound_5000='B' if inb else 'A', cards_sold=round(units), base_units_sold=round(base_sold), opening_units=open_units, march_residual_units=round(closing),
            inventory_reduction_pct=round(base_sold/open_units*100,1), gmv_rub=round(agg[('TOTAL','gmv')]), marketplace_costs_rub=round(agg[('TOTAL','mp')]), advertising_rub=round(agg[('TOTAL','ads')]),
            seller_cash_generated_rub=round(agg[('TOTAL','cash')]), cogs_released_rub=round(agg[('TOTAL','cogs')]), contribution_rub=round(agg[('TOTAL','contrib')]),
            contribution_margin_pct=round(agg[('TOTAL','contrib')]/agg[('TOTAL','gmv')]*100,1), ads_drr_pct=round(agg[('TOTAL','ads')]/agg[('TOTAL','gmv')]*100,1), bundles_sold=round(agg[('TOTAL','bundles')]),
            cash_conversion_of_opening_inventory_value_pct=round(agg[('TOTAL','cogs')]/(tot['sellable_value_rub']+(INBOUND['EVT-FC-MOIST-50']*cogs['EVT-FC-MOIST-50'] if inb else 0))*100,1)))
        for s in ALL:
            fc_rows.append(OD(scenario=scen, inbound='B' if inb else 'A', internal_sku=s, name=NAME[s], **{ym: round(sms[s].get(ym,0)) for ym in MONTHS}, total=round(sum(sms[s].values()))))
wr('SCENARIO_COMPARISON.csv', scen_rows)
wr('SKU_FORECAST_SEP_MAR.csv', fc_rows)
wr('RUNOUT_MODEL.csv', all_month)
wr('SALES_ECON_DETAIL.csv', all_rows)
# monthly targets for recommended = BASE + inbound B
rec = RESULTS[('BASE', True)]
mt = []
for ym in MONTHS:
    a = rec[2]
    mt.append(OD(ym=ym, cards=round(a[(ym,'units')]), bundles=round(a[(ym,'bundles')]), gmv=round(a[(ym,'gmv')]), ads=round(a[(ym,'ads')]), seller_cash=round(a[(ym,'cash')]), cogs_released=round(a[(ym,'cogs')]), contribution=round(a[(ym,'contrib')]), contribution_pct=round(a[(ym,'contrib')]/a[(ym,'gmv')]*100,1)))
wr('MONTHLY_SALES_TARGETS.csv', mt)
json.dump({'assumptions': ASSUMPTIONS}, open(os.path.join(OUT,'assumptions.json'),'w'), ensure_ascii=False, indent=1)
for r in scen_rows: print(r['scenario'], r['inbound_5000'], 'cards', r['cards_sold'], 'base', r['base_units_sold'], 'resid', r['march_residual_units'], 'cash', r['seller_cash_generated_rub'], 'contrib', r['contribution_rub'], 'cm%', r['contribution_margin_pct'])
