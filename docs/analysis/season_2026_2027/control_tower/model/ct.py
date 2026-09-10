# -*- coding: utf-8 -*-
"""EVETIS OPERATIONAL CONTROL TOWER v1 — model v2 (09.09.2026).
Reuses base data/functions from model.py, applies owner facts of 09.09 (FF exact, hand cream expiry 12.2026,
shelf life 2y, inbound cream 3y, OPEX), rebuilds scenarios + all control-tower artifacts into ct_out/."""
import io, contextlib, datetime as dt, math, collections, csv, json, os
from collections import OrderedDict as OD
_buf = io.StringIO()
with contextlib.redirect_stdout(_buf):
    exec(open('model.py').read())
OUT = 'ct_out'; os.makedirs(OUT, exist_ok=True)
TODAY = dt.date(2026, 9, 9)
A2 = []
def N(tag, text): A2.append((tag, text))

# ============================================================ 1. EXPIRY (owner facts + batch dates)
# FACT: evetis_ref.REF_COST_BATCH import dates; REF_COST_BATCH_SKU quantities. Owner facts: hand cream expiry Dec-2026 (HARD),
# shelf life 2 years from manufacturing, inbound cream 3 years. Manufacturing date not stored -> INFERENCE = import_date - 70 days
# (calibrated on the hand cream fact: import 25.02.2025 -> mfg ~17.12.2024 -> expiry 17.12.2026).
BATCH = {  # sku -> (batch, import_date, qty_in_batch)
 'EVT-HC-HAND-300': ('BATCH-02', dt.date(2025,2,25), 10200), 'EVT-FS-ACNE-30': ('BATCH-06', dt.date(2026,1,19), 6000), 'EVT-FS-MOIST-30': ('BATCH-06', dt.date(2026,1,19), 6000),
 'EVT-FC-ACNE-50': ('BATCH-04', dt.date(2025,8,28), 4995), 'EVT-FC-MOIST-50': ('BATCH-04', dt.date(2025,8,28), 5017),
 'EVT-EP-ENZYME-75': ('BATCH-05', dt.date(2025,12,31), 5000), 'EVT-FT-ACNE-150': ('BATCH-05', dt.date(2025,12,31), 5000), 'EVT-FT-MOIST-150': ('BATCH-05', dt.date(2025,12,31), 5000),
 'EVT-HC-AMBER-300': ('BATCH-07', dt.date(2026,3,26), 4970), 'EVT-HC-CHERRY-300': ('BATCH-07', dt.date(2026,3,26), 4859), 'EVT-HC-BODY-300': ('BATCH-01', dt.date(2024,8,27), 6000)}
MFG_LAG = 70
def expiry_of(s):
    if s == 'EVT-HC-HAND-300': return dt.date(2026,12,31), 'OWNER_FACT (hard): December 2026'
    b, imp, q = BATCH[s]
    mfg = imp - dt.timedelta(days=MFG_LAG)
    return mfg.replace(year=mfg.year+2), f'INFERENCE: {b} import {imp} - 70d = mfg {mfg} + 2y'
N('INFERENCE', 'Manufacturing date = import date - 70 days (sea freight + customs), calibrated on the owner fact for hand cream (import 25.02.2025, expiry Dec 2026). Expiry = mfg + 2 years; inbound cream + 3 years.')
N('INFERENCE', 'FIFO check on serums: BATCH-03 (5 000 acne + 4 800 moist, expiry ~Dec 2026) is fully consumed for moist serum (4 679 sold by Jan 2026 vs 4 800) and ~95% consumed for acne serum (~270 units could remain, plus fire losses) -> current serum stock treated as BATCH-06 (expiry ~Nov 2027). Physical lot check on pallets required for acne serum.')
EXP = {}
for s in BASE:
    e, src = expiry_of(s); EXP[s] = (e, src)

# ============================================================ 2. HISTORY EVIDENCE (seasonality)
wk = rd('wb_weekly.csv'); adsm = {r['ym']: float(r['ads_billing']) for r in rd('wb_ads_monthly.csv')}
def msum(ym, ch='WB'): return sum(h['sold_qty'] for (y, s, c), h in hist.items() if y == ym and c == ch and not s.startswith('UNMAPPED'))
def mgmv(ym, ch='WB'): return sum(h['sold_rub'] for (y, s, c), h in hist.items() if y == ym and c == ch and not s.startswith('UNMAPPED'))
def msets(ym, ch='WB'): return sum(h['sold_qty'] for (y, s, c), h in hist.items() if y == ym and c == ch and s in BOM)
def mbottles(ym, ch='WB'): return sum(h['sold_qty']*len(comps(s)) for (y, s, c), h in hist.items() if y == ym and c == ch and not s.startswith('UNMAPPED'))
season_rows = []
for label, months in (('2024/25', ['2024-09','2024-10','2024-11','2024-12','2025-01','2025-02','2025-03']), ('2025/26', ['2025-09','2025-10','2025-11','2025-12','2026-01','2026-02','2026-03']), ('2026 (Apr-Aug)', ['2026-04','2026-05','2026-06','2026-07','2026-08'])):
    oct_ref = msum(months[1]) if label != '2026 (Apr-Aug)' else msum('2026-05')
    for ym in months:
        u = msum(ym); g = mgmv(ym); st = msets(ym); bt = mbottles(ym); oz = msum(ym, 'OZON'); ozb = mbottles(ym, 'OZON')
        wkrows = [r for r in wk if r['week'][:7] == ym]
        peak = max((int(r['units']) for r in wkrows), default=0)
        season_rows.append(OD(season=label, ym=ym, wb_cards=u, wb_bottles=bt, wb_bundles=st, bundle_share_cards_pct=round(st/u*100,1) if u else 0, wb_gmv=round(g), wb_asp=round(g/u) if u else 0,
            wb_ads_billing=round(adsm.get(ym, 0)), wb_drr_pct=round(adsm.get(ym,0)/g*100,1) if g else 0, peak_week_units=peak, peak_week_per_day=round(peak/7,1),
            ozon_cards=oz, ozon_bottles=ozb, index_vs_oct=round(u/oct_ref, 2) if oct_ref else '', hand_cream_wb=hist[(ym,'EVT-HC-HAND-300','WB')]['sold_qty'],
            stock_availability='not reconstructable before 2026 (stock snapshots start 2026); 2025/26: moisture cream out of stock from Apr-2026, fire losses Jul-2026' ))
wr('HISTORICAL_SEASONS.csv', season_rows)
SEAS_EVIDENCE = {'2024/25': {m: r['index_vs_oct'] for m, r in [(r['ym'], r) for r in season_rows if r['season']=='2024/25']}, '2025/26': {r['ym']: r['index_vs_oct'] for r in season_rows if r['season']=='2025/26'}}
N('FACT', 'Seasonal multipliers vs October (WB buyouts): 2024/25 Nov 1.91 / Dec 2.71 / Jan 1.39 / Feb 1.52 / Mar 2.22; 2025/26 Nov 1.03 / Dec 2.36 / Jan 0.79 / Feb 1.00 / Mar 1.57. Peak weeks: 23.12.2024 1 259 units (180/day), 08.12.2025 891 (127/day), 02.03.2026 877. Ads billing at peak: Dec-2024 229k (DRR 8.9%), Dec-2025 375k (15.8%).')

# ============================================================ 3. SCENARIO ENGINE v2
MONTHS = ['2026-09','2026-10','2026-11','2026-12','2027-01','2027-02','2027-03']
DAYS = {'2026-09': 22, '2026-10': 31, '2026-11': 30, '2026-12': 31, '2027-01': 31, '2027-02': 28, '2027-03': 31}
OPEX = {'2026-09': 77000, '2026-10': 77000, '2026-11': 77000, '2026-12': 77000, '2027-01': 77000, '2027-02': 77000, '2027-03': 65000}
N('FACT', 'OPEX (owner): fulfilment 45 000 + accountant 20 000 + designer 12 000 (6 months) = 77 000 RUB/month Sep-Feb, 65 000 from March.')
# seasonality (evidence-based, see HISTORICAL_SEASONS)
SEAS_WB = {'MP': [0.90,1.00,1.10,1.60,0.85,1.00,1.30], 'BAL': [0.55,0.90,1.25,2.00,0.90,1.05,1.50], 'SET': [0.55,0.90,1.35,2.40,1.00,1.10,1.60], 'AGGR': [0.60,0.95,1.45,2.60,1.05,1.20,1.75]}
SEAS_OZ = {'MP': [0.90,1.00,1.20,1.30,0.90,1.00,1.20], 'BAL': [0.90,1.05,1.40,1.50,1.00,1.10,1.40], 'SET': [0.90,1.10,1.50,1.60,1.05,1.15,1.50], 'AGGR': [0.90,1.15,1.70,1.80,1.15,1.25,1.70]}
N('ASSUMPTION', 'SET seasonality WB = average of the two observed seasons rounded down (Nov 1.35, Dec 2.40, Jan 1.00, Feb 1.10, Mar 1.60 vs Oct); Ozon = own 2025/26 pattern damped (Nov 1.6, Dec 1.7, Mar 1.6). MP uses half the seasonal lift; AGGR adds +8-10%.')
GROWTH_OZ = {'MP': 1.0, 'BAL': 1.15, 'SET': 1.25, 'AGGR': 1.4}   # structural Ozon growth from FBO scale plan (vs Aug-2026 base)
VOL_MULT = {'MP': 0.0, 'BAL': 0.75, 'SET': 0.90, 'AGGR': 1.05}   # MP = current run rate, others = share of full-availability base

# full-availability WB base (cards/month) per SKU: max(Apr-May 2026 avg, Sep-Oct 2025 avg)
def wb_full_base(s):
    a = (hist[('2026-04', s, 'WB')]['sold_qty'] + hist[('2026-05', s, 'WB')]['sold_qty'])/2
    b = (hist[('2025-09', s, 'WB')]['sold_qty'] + hist[('2025-10', s, 'WB')]['sold_qty'])/2
    if a and b: return 0.5*(a+b)
    return max(a, b)
def oz_base(s): return max(hist[('2026-08', s, 'OZON')]['sold_qty'], VEL[s]['oz90']/3)
def cur_wb(s): return VEL[s]['wb90']/3
def cur_oz(s): return VEL[s]['oz90']/3

# hand cream liquidation programs: month -> (wb_solo, oz_solo, bundles_units(hand cream inside sets), wb_price, oz_price, wb_drr, oz_drr)
HC_PROG = {
 'HC-A': {'2026-09': (400, 60, 30, 590, 895, 0.10, 0.10), '2026-10': (560, 90, 60, 560, 850, 0.12, 0.10), '2026-11': (560, 100, 90, 540, 850, 0.14, 0.10), '2026-12': (600, 110, 100, 520, 820, 0.16, 0.10)},
 'HC-B': {'2026-09': (170, 40, 25, 640, 973, 0.06, 0.08), '2026-10': (330, 90, 60, 590, 895, 0.10, 0.10), '2026-11': (640, 150, 120, 540, 850, 0.15, 0.10), '2026-12': (800, 170, 150, 490, 799, 0.17, 0.10)},
 'HC-C': {'2026-09': (150, 40, 20, 640, 973, 0.04, 0.08), '2026-10': (200, 60, 40, 640, 973, 0.05, 0.08), '2026-11': (750, 180, 130, 520, 830, 0.16, 0.12), '2026-12': (950, 200, 160, 470, 790, 0.18, 0.12)},
 'HC-D': {'2026-09': (140, 40, 60, 640, 973, 0.06, 0.08), '2026-10': (250, 80, 160, 590, 895, 0.10, 0.10), '2026-11': (480, 140, 300, 540, 850, 0.15, 0.10), '2026-12': (600, 160, 330, 500, 799, 0.17, 0.10)},
}
N('ASSUMPTION', 'Hand cream programs: monthly capacities are set from evidence (Sep-Oct 2025: 6-11/day; Nov-Dec 2025: 16-22/day at ASP 560-590 and ads ~100 RUB/card; Nov-Dec 2024: 65-96/day at ASP ~560 and ads ~58 RUB/unit). Prices step down 640 -> 490 on WB, 973 -> 799 on Ozon; liquidation DRR up to 17-18% in Nov-Dec. Volume response to price is not modelled by elasticity.')
HC_BUNDLE_SPLIT = {'EVT-SET-HAND-CHERRY': 0.55, 'EVT-SET-HAND-AMBER': 0.45}

def ad_class(s):
    if s == 'EVT-HC-HAND-300' or s in ('EVT-SET-HAND-CHERRY','EVT-SET-HAND-AMBER'): return 'LIQUIDATION_ADS'
    if s in ('EVT-FS-ACNE-30','EVT-FS-MOIST-30','EVT-FC-ACNE-50','EVT-FC-MOIST-50','EVT-SET-SER-CREAM-ACNE','EVT-SET-SER-CREAM-MOIST','EVT-SET-4PC-ACNE','EVT-SET-TON-SER-CREAM-ACNE','EVT-SET-TON-SER-CREAM-MOIST','EVT-SET-CHERRY-AMBER'): return 'ADVERTISED'
    if s in ('EVT-HC-CHERRY-300','EVT-HC-AMBER-300'): return 'BUNDLE_SUPPORTED'
    return 'ORGANIC_HALO'
def plan_drr(s, ch, scen, P, fn):
    """planning DRR: ADVERTISED ~10-12% within economics; ORGANIC 2%; BUNDLE_SUPPORTED 0; liquidation set by program"""
    u0 = fn(s, P, 0.0)
    if u0 is None: return 0.0
    cb = u0['contribution_pct']; cls = ad_class(s)
    cap = {'MP': 0.08, 'BAL': 0.10, 'SET': 0.12, 'AGGR': 0.15}[scen]; floor = {'MP': 0.15, 'BAL': 0.10, 'SET': 0.08, 'AGGR': 0.05}[scen]
    if cls == 'ADVERTISED': return max(0.0, min(cap, cb - floor))
    if cls == 'ORGANIC_HALO': return min(0.03, max(0.0, cb - floor))
    if cls == 'BUNDLE_SUPPORTED': return 0.0
    return 0.0
N('ASSUMPTION', 'Advertising classes: ADVERTISED (serums, face creams, ser+cream sets, 4-step, ton+ser+cream sets, Cherry+Amber set) get DRR = min(cap, contribution% - floor) with SET cap 12% / floor 8%; ORGANIC_HALO (tonics, powder, small sets) 2-3%; BUNDLE_SUPPORTED (Cherry, Amber solo) 0% direct; LIQUIDATION_ADS per hand-cream program. No SKU gets a flat 10%.')

def price_factor2(s, scen, ym):
    promo = ym in ('2026-11','2026-12','2027-03')
    cls = VEL[s]['cls']
    if s in ('EVT-HC-HAND-300','EVT-SET-HAND-CHERRY','EVT-SET-HAND-AMBER'): return 1.0   # handled by program / set price below
    if cls.startswith('D-OVERSTOCK') or cls.startswith('E-') or cls.startswith('C-'):
        return {'MP': 1.0, 'BAL': 0.95 if promo else 1.0, 'SET': 0.92 if promo else 0.97, 'AGGR': 0.88 if promo else 0.94}[scen]
    return {'MP': 1.0, 'BAL': 1.0, 'SET': 0.95 if promo else 1.0, 'AGGR': 0.92 if promo else 0.97}[scen]

def demand_v2(s, scen, ym, i, inbound_cream, reorder_acne):
    """returns (wb_cards, oz_cards) planned for month"""
    frac = DAYS[ym]/30 if ym == '2026-09' else 1.0
    sw, so = SEAS_WB[scen][i], SEAS_OZ[scen][i]
    if s in ('EVT-HC-BODY-300','EVT-SET-HAND-BODY'): return 0, 0
    if s == 'EVT-HC-HAND-300' or s in ('EVT-SET-HAND-CHERRY','EVT-SET-HAND-AMBER'): return None  # program-driven
    m = VOL_MULT[scen]
    if scen == 'MP': wb, oz = cur_wb(s), cur_oz(s)*GROWTH_OZ[scen]
    else: wb, oz = max(cur_wb(s), m*wb_full_base(s)), max(cur_oz(s), oz_base(s))*GROWTH_OZ[scen]
    if s == 'EVT-FC-MOIST-50':
        wb, oz = {'MP': (150, 20), 'BAL': (220, 35), 'SET': (260, 45), 'AGGR': (300, 55)}[scen]
    if s in ('EVT-SET-SER-CREAM-MOIST','EVT-SET-TON-CREAM-MOIST','EVT-SET-TON-SER-CREAM-MOIST'):
        ref = {'EVT-SET-SER-CREAM-MOIST': (40, 4), 'EVT-SET-TON-CREAM-MOIST': (18, 2), 'EVT-SET-TON-SER-CREAM-MOIST': (40, 4)}[s]
        k = {'MP': 0.7, 'BAL': 0.9, 'SET': 1.1, 'AGGR': 1.3}[scen]; wb, oz = ref[0]*k, ref[1]*k
    if s == 'EVT-FC-MOIST-50' or (s in BOM and 'EVT-FC-MOIST-50' in comps(s)):
        if not inbound_cream or ym in ('2026-09','2026-10'): return 0, 0
    if s in BOM and s not in oze: oz = 0
    if s == 'EVT-SET-ACNE-POWDER-SERUM-CREAM': wb = 0
    # bundle push in SET/AGGR for release bundles
    if s in ('EVT-SET-CHERRY-AMBER','EVT-SET-MOIST-TONIC-SERUM','EVT-SET-ACNE-TONIC-SERUM','EVT-SET-TON-CREAM-ACNE'):
        k = {'MP': 1.0, 'BAL': 1.5, 'SET': 2.5, 'AGGR': 3.5}[scen]; wb, oz = wb*k, oz*k
    return wb*sw*frac, oz*so*frac

HC_CLEAR = {}
def simulate2(scen, inbound_cream=True, reorder_acne=False, hc='HC-B', vol_override=None):
    HC_CLEAR.pop(hc, None)
    stock = {s: INV[s]['total_sellable_units'] for s in BASE}
    inb_plan = {}
    if inbound_cream: inb_plan[('2026-10','EVT-FC-MOIST-50')] = 5000
    if reorder_acne: inb_plan[('2026-12','EVT-FC-ACNE-50')] = 3000
    det = []; runout = []; agg = collections.defaultdict(float); bundles_sold = collections.Counter(); by_pieces = collections.defaultdict(lambda: collections.Counter())
    hc_left = stock['EVT-HC-HAND-300']
    for i, ym in enumerate(MONTHS):
        for s in BASE:
            stock[s] += inb_plan.get((ym, s), 0)
        demand = {}
        for s in ALL:
            d = demand_v2(s, scen, ym, i, inbound_cream, reorder_acne)
            if d is None:
                prog = HC_PROG[hc].get(ym)
                if prog is None: demand[s] = (0, 0); continue
                wbq, ozq, bnd, wbp, ozp, wdrr, odrr = prog
                if ym == '2026-12':   # final clearance: whatever is left must go (hard expiry)
                    remaining = stock['EVT-HC-HAND-300']; capacity = wbq + ozq + bnd
                    if remaining > capacity:
                        extra = remaining - capacity; wbq += extra*0.75; ozq += extra*0.15; bnd += extra*0.10
                        HC_CLEAR[hc] = extra
                if s == 'EVT-HC-HAND-300': demand[s] = (wbq, ozq)
                else: demand[s] = (bnd*HC_BUNDLE_SPLIT[s]*0.6, bnd*HC_BUNDLE_SPLIT[s]*0.4)   # sets: 60% WB / 40% Ozon
            else:
                demand[s] = d
            if vol_override: demand[s] = (demand[s][0]*vol_override, demand[s][1]*vol_override)
        # December final clearance for hand cream: sell whatever remains (program) - handled by stock cap below plus clearance flag
        # component-capacity scaling
        scale = {s: 1.0 for s in ALL}
        for _ in range(25):
            need = collections.Counter()
            for s in ALL:
                q = (demand[s][0] + demand[s][1])*scale[s]
                for c in comps(s): need[c] += q
            changed = False
            for c in BASE:
                if need[c] > stock[c] + 1e-9:
                    f = stock[c]/need[c] if need[c] else 0
                    for s in ALL:
                        if c in comps(s): scale[s] *= f; changed = True
            if not changed: break
        consumed = collections.Counter(); solo = collections.Counter(); viab = collections.Counter()
        for s in ALL:
            wbq = demand[s][0]*scale[s]; ozq = demand[s][1]*scale[s]
            if oz_unit(s, None, 0.0) is None: ozq = 0.0
            for ch, q, fn in (('WB', wbq, wb_unit), ('OZON', ozq, oz_unit)):
                if q <= 0: continue
                u = fn(s, None, 0.0)
                if u is None: continue
                # price & drr
                if s == 'EVT-HC-HAND-300':
                    prog = HC_PROG[hc][ym]; P = prog[3] if ch == 'WB' else prog[4]; drr = prog[5] if ch == 'WB' else prog[6]
                    if ym == '2026-12' and HC_CLEAR.get(hc, 0) > 0:   # blended price: program units at program price, clearance units at floor
                        cap = prog[0] if ch == 'WB' else prog[1]; floor = 450 if ch == 'WB' else 699
                        extra = max(0.0, q - cap); P = (cap*P + extra*floor)/q if q else P; drr = min(0.22, drr + 0.03)
                elif s in ('EVT-SET-HAND-CHERRY','EVT-SET-HAND-AMBER'):
                    P = u['price']*(1.0 if ym in ('2026-09',) else 0.95 if ym == '2026-10' else 0.90); drr = 0.08
                else:
                    P = u['price']*price_factor2(s, scen, ym); drr = plan_drr(s, ch, scen, P, fn)
                u = fn(s, P, drr)
                gmv = q*P; cash = q*u['cash_proceeds']; ads = q*u['ads']; cg = q*u['cogs']; contrib = cash - cg
                mp = q*(u['commission'] + u['acquiring'] + u['logistics'] + u['storage'] + u.get('returns_allow', 0))
                det.append(OD(scenario=scen, ym=ym, internal_sku=s, channel=ch, cards=round(q,1), bottles=round(q*len(comps(s)),1), price=round(P), drr_pct=round(drr*100,1), gmv=round(gmv), marketplace_costs=round(mp), ads=round(ads), seller_cash=round(cash), cogs=round(cg), contribution=round(contrib)))
                for k, v in (('cards', q), ('bottles', q*len(comps(s))), ('gmv', gmv), ('mp', mp), ('ads', ads), ('cash', cash), ('cogs', cg), ('contrib', contrib)):
                    agg[(ym, k)] += v; agg[('TOTAL', k)] += v
                agg[(ym, 'cards_'+ch)] += q; agg[(ym, 'bottles_'+ch)] += q*len(comps(s)); agg[('TOTAL', 'cards_'+ch)] += q; agg[('TOTAL', 'bottles_'+ch)] += q*len(comps(s))
                if s in BOM:
                    agg[(ym, 'bundle_cards')] += q; agg[(ym, 'bundle_bottles')] += q*len(comps(s)); agg[('TOTAL','bundle_cards')] += q; agg[('TOTAL','bundle_bottles')] += q*len(comps(s)); bundles_sold[s] += q
                by_pieces[ym][len(comps(s))] += q*len(comps(s))
            tq = wbq + ozq
            for c in comps(s):
                consumed[c] += tq
                if s in BOM: viab[c] += tq
                else: solo[c] += tq
        for s in BASE:
            opening = stock[s] - inb_plan.get((ym, s), 0)
            stock[s] = max(0.0, stock[s] - consumed[s])
            runout.append(OD(scenario=scen, ym=ym, internal_sku=s, name=NAME[s], opening=round(opening), inbound=inb_plan.get((ym, s), 0), solo_units=round(solo[s]), units_via_bundles=round(viab[s]), closing=round(stock[s]),
                             days_of_stock=round(stock[s]/(consumed[s]/DAYS[ym])) if consumed[s] else ''))
    # summary
    open_units = tot['total_sellable_units'] + sum(inb_plan.values())
    open_value = tot['sellable_value_rub'] + inb_plan.get(('2026-10','EVT-FC-MOIST-50'), 0)*130.5 + inb_plan.get(('2026-12','EVT-FC-ACNE-50'), 0)*131.66
    closing = sum(r['closing'] for r in runout if r['ym'] == '2027-03')
    hc_resid = [r['closing'] for r in runout if r['ym'] == '2026-12' and r['internal_sku'] == 'EVT-HC-HAND-300'][0]
    opex = sum(OPEX.values())
    summ = OD(scenario=scen, inbound_cream=inbound_cream, reorder_acne_3000=reorder_acne, hand_cream_program=hc,
        cards_sold=round(agg[('TOTAL','cards')]), bottles_sold=round(agg[('TOTAL','bottles')]), bundle_cards=round(agg[('TOTAL','bundle_cards')]), bundle_bottles=round(agg[('TOTAL','bundle_bottles')]),
        wb_cards=round(agg[('TOTAL','cards_WB')]), ozon_cards=round(agg[('TOTAL','cards_OZON')]), opening_units=open_units, march_residual_units=round(closing), stock_reduction_pct=round((open_units-closing)/open_units*100,1),
        hand_cream_residual_at_expiry_31_12=round(hc_resid), hand_cream_writeoff_rub=round(hc_resid*cogs['EVT-HC-HAND-300']),
        gmv=round(agg[('TOTAL','gmv')]), marketplace_costs=round(agg[('TOTAL','mp')]), advertising=round(agg[('TOTAL','ads')]), drr_pct=round(agg[('TOTAL','ads')]/agg[('TOTAL','gmv')]*100,1),
        seller_cash=round(agg[('TOTAL','cash')]), cogs_sold=round(agg[('TOTAL','cogs')]), contribution=round(agg[('TOTAL','contrib')]), contribution_pct=round(agg[('TOTAL','contrib')]/agg[('TOTAL','gmv')]*100,1),
        opex=opex, operating_result=round(agg[('TOTAL','contrib')] - opex - hc_resid*cogs['EVT-HC-HAND-300']),
        opening_inventory_value=round(open_value), inventory_cash_conversion_pct=round(agg[('TOTAL','cogs')]/open_value*100,1), closing_inventory_value=round(open_value - agg[('TOTAL','cogs')] - hc_resid*cogs['EVT-HC-HAND-300']))
    return det, runout, agg, bundles_sold, by_pieces, summ

SCN = OD()
SCN['MARGIN_PROTECTION'] = simulate2('MP', True, False, 'HC-C')
SCN['BALANCED'] = simulate2('BAL', True, False, 'HC-B')
SCN['SEASON_EXECUTION_TARGET'] = simulate2('SET', True, True, 'HC-B')
SCN['AGGRESSIVE_CASH_RELEASE'] = simulate2('AGGR', True, True, 'HC-A')
SCN['SET_no_acne_reorder'] = simulate2('SET', True, False, 'HC-B')
SCN['SET_no_inbound_cream'] = simulate2('SET', False, True, 'HC-B')
# stretch: residual < 15 000
for mult in (1.2, 1.4, 1.6, 1.8, 2.0):
    r = simulate2('SET', True, True, 'HC-A', vol_override=mult)
    SCN[f'STRETCH_x{mult}'] = r
    if r[5]['march_residual_units'] < 15000: break
sc_rows = [v[5] for v in SCN.values()]
wr('SCENARIOS_V2.csv', sc_rows)
for k, v in SCN.items(): print(k, v[5]['bottles_sold'], v[5]['march_residual_units'], 'cash', v[5]['seller_cash'], 'contrib', v[5]['contribution'], 'opres', v[5]['operating_result'], 'hc_resid', v[5]['hand_cream_residual_at_expiry_31_12'])
REC = SCN['SEASON_EXECUTION_TARGET']; det, runout, agg, bsold, bypieces, summ = REC
wr('SET_DETAIL.csv', det); wr('SET_RUNOUT.csv', runout)
# monthly plan table
mon = []
cum_cash = 0
for ym in MONTHS:
    cum_cash += agg[(ym,'cash')]
    inv_left = sum(r['closing'] for r in runout if r['ym'] == ym)
    mon.append(OD(ym=ym, days=DAYS[ym], cards=round(agg[(ym,'cards')]), bottles=round(agg[(ym,'bottles')]), bundle_cards=round(agg[(ym,'bundle_cards')]), bundle_bottles=round(agg[(ym,'bundle_bottles')]), solo_bottles=round(agg[(ym,'bottles')]-agg[(ym,'bundle_bottles')]),
        wb_cards=round(agg[(ym,'cards_WB')]), ozon_cards=round(agg[(ym,'cards_OZON')]), wb_bottles=round(agg[(ym,'bottles_WB')]), ozon_bottles=round(agg[(ym,'bottles_OZON')]),
        gmv=round(agg[(ym,'gmv')]), marketplace_costs=round(agg[(ym,'mp')]), ads=round(agg[(ym,'ads')]), drr_pct=round(agg[(ym,'ads')]/agg[(ym,'gmv')]*100,1), seller_cash=round(agg[(ym,'cash')]), cogs_sold=round(agg[(ym,'cogs')]),
        contribution=round(agg[(ym,'contrib')]), contribution_pct=round(agg[(ym,'contrib')]/agg[(ym,'gmv')]*100,1), opex=OPEX[ym], operating_result=round(agg[(ym,'contrib')]-OPEX[ym]), cumulative_seller_cash=round(cum_cash), inventory_remaining_units=round(inv_left),
        pieces_1=round(bypieces[ym][1]), pieces_2=round(bypieces[ym][2]), pieces_3=round(bypieces[ym][3]), pieces_4=round(bypieces[ym][4])))
wr('SET_MONTHLY_PLAN.csv', mon)
json.dump(A2, open(os.path.join(OUT, 'assumptions_v2.json'), 'w'), ensure_ascii=False, indent=1)
