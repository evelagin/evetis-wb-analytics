# -*- coding: utf-8 -*-
"""Part 2: hand cream plan, bundles, moisture line, price recommendations, FBO/FBS allocation, supply plans, 8-week plan."""
exec(open('model.py').read())
import datetime as dt

# ---------------------------------------------------------------- hand cream liquidation plan
HC = 'EVT-HC-HAND-300'
hc_stock = INV[HC]['total_sellable_units']   # 2 952 incl. 40 in transit, WB live 164 (143 solo + 21 in sets), Ozon 28 in sets
hc_rows = []
wb_now = wb_unit(HC, None, 0.0); oz_now = oz_unit(HC, None, 0.0)
hist_hc = [(ym, hist[(ym,HC,'WB')]['sold_qty'], hist[(ym,HC,'OZON')]['sold_qty'], hist[(ym,HC,'WB')]['ads']+hist[(ym,HC,'OZON')]['ads']) for ym in ['2025-09','2025-10','2025-11','2025-12','2026-01','2026-02','2026-03','2026-04','2026-05','2026-06','2026-07','2026-08']]
A('FACT','Hand cream WB realized ASP: 28d 418 RUB, 7d 494 RUB (V_ADS_SKU_ECONOMIC_LIMITS 08.09) vs listed seller price 640 (list 1600 -60%). At 418 the unit is -95 RUB before ads. Peak velocity: Dec 2024 2 301/mo (launch season, body-cream twin, heavy ads); Dec 2025 643/mo; Jun-Aug 2026 183-277/mo at 20-24k RUB/mo ads.')
for per_day in (7, 10, 20, 30, 40, 50):
    per_month = per_day*30
    months_to_zero = hc_stock/per_month
    runout = (dt.date(2026,9,9) + dt.timedelta(days=hc_stock/per_day)).isoformat()
    # channel mix assumption: WB 65% / Ozon 25% / bundles 10% (bundles carry hand cream at set economics)
    for label, wb_price, oz_price in (('current prices (WB 640 / Ozon 973)', 640, 973), ('promo -8% (WB 590 / Ozon 895)', 590, 895), ('floor: WB break-even no ads / Ozon 850', None, 850)):
        wbp = wb_price if wb_price else price_for_margin(wb_unit, HC, 0.0, 0.0)
        uw = wb_unit(HC, wbp, 0.0); uo = oz_unit(HC, oz_price, 0.0)
        wb_share, oz_share = 0.70, 0.30
        gmv = per_month*(wb_share*wbp + oz_share*oz_price)
        cash_before_ads = per_month*(wb_share*uw['cash_proceeds'] + oz_share*uo['cash_proceeds'])
        cogs_m = per_month*cogs[HC]
        contrib_before_ads = cash_before_ads - cogs_m
        allowable_ads_breakeven = max(0.0, contrib_before_ads)
        # ads needed (ASSUMPTION): historical CPO on hand cream WB ~ 60-120 RUB/unit at 200-280/mo; scaling velocity needs more: cost per incremental unit grows.
        est_ads = per_month * (0 if per_day <= 7 else 45 if per_day <= 10 else 90 if per_day <= 20 else 140 if per_day <= 30 else 200 if per_day <= 40 else 260)
        contrib_after = contrib_before_ads - est_ads
        hc_rows.append(OD(target_units_per_day=per_day, units_per_month=per_month, price_case=label, wb_price=round(wbp), ozon_price=oz_price, channel_mix='WB 70% / Ozon 30% (incl. bundle carry)',
            months_to_clear_2952=round(months_to_zero,1), runout_date=runout, gmv_month=round(gmv), seller_cash_before_ads_month=round(cash_before_ads), cogs_month=round(cogs_m),
            contribution_before_ads_month=round(contrib_before_ads), allowable_ads_at_breakeven_month=round(allowable_ads_breakeven), allowable_drr_at_breakeven_pct=round(allowable_ads_breakeven/gmv*100,1) if gmv else '',
            estimated_ads_needed_month=round(est_ads), contribution_after_ads_month=round(contrib_after), contribution_after_ads_pct=round(contrib_after/gmv*100,1) if gmv else '',
            feasibility='FACT-based: 7-10/day is the Jun-Aug rate at 418-494 ASP with 13-24k ads; 20/day seen only Dec 2025 (643/mo); 30+/day only in launch season 2024/25 (different market, twin product)' if per_day>=20 else 'current run-rate'))
wr('HAND_CREAM_PLAN.csv', hc_rows)
A('ASSUMPTION','Hand cream ads needed per unit at higher velocities (45/90/140/200/260 RUB per unit for 10/20/30/40/50 per day) - extrapolated from Jun-Aug CPO 60-120 RUB at 6-9/day; no data exists for 20+/day at 640 RUB price. Sensitivity: every +50 RUB/unit of ads costs 1.5% of GMV.')

# ---------------------------------------------------------------- bundles
b_rows = []
recB = RESULTS[('BASE', True)]; bsold = recB[3]
for s in SETS:
    if s == 'EVT-SET-HAND-BODY': continue
    wb90 = VEL[s]['wb90']; oz90 = VEL[s]['oz90']
    uw = wb_unit(s, None, 0.0); uo = oz_unit(s, None, 0.0)
    comp_stock = {c: INV[c]['total_sellable_units'] for c in comps(s)}
    limiting = min(comp_stock, key=comp_stock.get)
    cap = comp_stock[limiting]
    # solo contribution of components (WB) vs set contribution
    solo_sum = sum(wb_unit(c, None, 0.0)['contribution'] for c in comps(s)); solo_price = sum(wb_unit(c, None, 0.0)['price'] for c in comps(s))
    b_rows.append(OD(set_sku=s, name=NAME[s], bom=' + '.join(NAME[c] for c in comps(s)), units_consumed_per_set=len(comps(s)),
        wb_sold_90d=wb90, ozon_sold_90d=oz90, wb_price=round(uw['price']), ozon_price=round(uo['price']) if uo else '', cogs=round(uw['cogs']),
        wb_contribution_before_ads=round(uw['contribution']), wb_contribution_pct=round(uw['contribution_pct']*100,1),
        ozon_contribution_before_ads=round(uo['contribution']) if uo else '', ozon_contribution_pct=round(uo['contribution_pct']*100,1) if uo else '',
        components_sold_solo_wb_price=round(solo_price), components_sold_solo_wb_contribution=round(solo_sum), set_vs_solo_delta_rub=round(uw['contribution']-solo_sum),
        limiting_component=NAME[limiting], max_sets_on_current_stock=cap, plan_sets_sep_mar_BASE_B=round(bsold.get(s,0)),
        bundle_class=VEL[s]['cls'], recommendation=('SCALE: hero set, contribution above components sold solo' if uw['contribution'] > solo_sum*0.9 and uw['contribution_pct']>0.2 else
            'INVENTORY RELEASE: use to move Cherry/Amber/hand cream; keep price >= components floor' if s in ('EVT-SET-CHERRY-AMBER','EVT-SET-HAND-CHERRY','EVT-SET-HAND-AMBER') else
            'BLOCKED until inbound cream (Scenario B)' if 'EVT-FC-MOIST-50' in comps(s) else
            'KEEP at economic DRR; raise price +5-8% (set is priced below sum of components)' if uw['contribution'] < solo_sum else 'KEEP')))
wr('BUNDLE_PLAN.csv', b_rows)

# ---------------------------------------------------------------- moisture line A/B
ml = []
for scn in ('A','B'):
    st = {c: INV[c]['total_sellable_units'] + (INBOUND.get(c,0) if scn=='B' else 0) for c in ('EVT-FS-MOIST-30','EVT-FT-MOIST-150','EVT-FC-MOIST-50')}
    res = RESULTS[('BASE', scn=='B')]; mrows = res[1]
    closing = {c: [r['closing'] for r in mrows if r['internal_sku']==c and r['ym']=='2027-03'][0] for c in st}
    sold = {c: st[c]-closing[c] for c in st}
    ml.append(OD(scenario=scn, serum_stock=st['EVT-FS-MOIST-30'], tonic_stock=st['EVT-FT-MOIST-150'], cream_stock=st['EVT-FC-MOIST-50'],
        max_sets_ser_cream=min(st['EVT-FS-MOIST-30'], st['EVT-FC-MOIST-50']), max_sets_ton_cream=min(st['EVT-FT-MOIST-150'], st['EVT-FC-MOIST-50']), max_sets_ton_ser_cream=min(st.values()), max_sets_ton_ser=min(st['EVT-FS-MOIST-30'], st['EVT-FT-MOIST-150']),
        BASE_sold_serum=round(sold['EVT-FS-MOIST-30']), BASE_sold_tonic=round(sold['EVT-FT-MOIST-150']), BASE_sold_cream=round(sold['EVT-FC-MOIST-50']),
        march_serum=round(closing['EVT-FS-MOIST-30']), march_tonic=round(closing['EVT-FT-MOIST-150']), march_cream=round(closing['EVT-FC-MOIST-50']),
        cash_need_rub='0' if scn=='A' else '~430-500k (5 000 x ~130.5 all-in = ~652k minus advance 225 792; customs+transport on arrival) ASSUMPTION',
        note='A: cream=2 units, 3 sets with cream BOM_BLOCKED, serum/tonic sell solo only. B: cream unblocks sets sers+cream / ton+cream / ton+ser+cream (Mar 2026 fact: 194+9+13 sets/mo on WB)'))
wr('MOISTURE_LINE_AB.csv', ml)

# ---------------------------------------------------------------- price recommendations
pr_rows = []
for s in ALL:
    if s in ('EVT-HC-BODY-300','EVT-SET-HAND-BODY'): continue
    cls = VEL[s]['cls']
    for ch, fn in (('WB', wb_unit), ('OZON', oz_unit)):
        u = fn(s, None, 0.0)
        if u is None: continue
        P = u['price']; cb = u['contribution_pct']
        floor = price_for_margin(fn, s, 0.0, 0.0)
        if s == HC:
            normal, promo, target_drr = (P, round(P*0.92), 4) if ch=='WB' else (P, round(P*0.92), 8)
            logic = 'LIQUIDATION: hold >= break-even, no paid traffic on the single on WB (contribution 45 RUB before ads at 640); push via Ozon (52% comm but higher price) and body bundles'
        elif cls.startswith('D-OVERSTOCK') or cls.startswith('C-'):
            normal, promo = (round(P*1.05) if cb < 0.25 else P), round(P*0.92)
            target_drr = max(0, round((cb-0.12)*100))
            logic = 'SELL DOWN: keep list price, run -8% only on event dates (11.11, BF, NY, 8 March); ads only within economic ceiling'
        elif 'R-REPLENISH' in cls:
            normal, promo, target_drr = P, round(P*0.95), max(0, round((cb-0.15)*100)); logic = 'KEEP STOCK/REPLENISH: strongest margin, no deep discount; -5% only at NY'
        elif cls.startswith('A-'):
            normal, promo, target_drr = P, round(P*0.95), max(0, round((cb-0.15)*100)); logic = 'HERO: protect price; -5% max on event dates; ads up to ceiling that keeps 15% after ads'
        elif cls.startswith('E-'):
            normal, promo, target_drr = P, round(P*0.90), max(0, round((cb-0.08)*100)); logic = 'INVENTORY-RELEASE bundle: price so that set >= sum of components at promo; -10% on events; ads to 8% floor'
        else:
            normal, promo, target_drr = P, round(P*0.95), max(0, round((cb-0.12)*100)); logic = 'KEEP'
        pr_rows.append(OD(internal_sku=s, name=NAME[s], channel=ch, current_price=round(P), contribution_before_ads_pct=round(cb*100,1), recommended_normal_price=round(normal), promo_price_events=promo,
            absolute_floor_no_ads=floor, target_drr_pct=target_drr, velocity_class=cls, logic=logic))
wr('PRICE_RECOMMENDATION.csv', pr_rows)

# ---------------------------------------------------------------- FBO / FBS / FF allocation (recommended plan = BASE B)
LEAD_WB, LEAD_OZ = 14, 10; COVER_WB, COVER_OZ = 45, 45; Z = 1.65
A('ASSUMPTION','FBO target = cover days (45) x daily demand + safety z=1.65*sqrt(lead x daily); lead time WB 14 d (slot + acceptance, NOT_PROVEN), Ozon 10 d. FF reserve = 21 days of FBS+bundle assembly demand, min 20 units per active SKU.')
res = RESULTS[('BASE', True)]; det = res[0]
def ch_month(s, ym, ch): return sum(r['units'] for r in det if r['internal_sku']==s and r['ym']==ym and r['channel']==ch)
alloc = []; supply_m = []
oct_ = '2026-10'
for s in BASE:
    if s == 'EVT-HC-BODY-300': continue
    # daily demand in cards incl. bundle consumption, by channel, Oct-Nov average
    def daily(ch):
        tot_ = 0
        for ym in ('2026-10','2026-11'):
            for x in ALL:
                if s in comps(x): tot_ += ch_month(x, ym, ch)
        return tot_/61
    dw, do = daily('WB'), daily('OZON')
    tw = round(COVER_WB*dw + Z*math.sqrt(max(LEAD_WB*dw,0.01))); to = round(COVER_OZ*do + Z*math.sqrt(max(LEAD_OZ*do,0.01)))
    ff_res = max(20, round(21*(dw+do)*0.35))  # FBS + assembly reserve (35% of flow assumed to go FBS/bundles from FF)
    wb_now = INV[s]['wb_fbo_live']; oz_now = INV[s]['ozon_fbo'] + INV[s]['in_transit_to_ozon']
    mode = 'FBO WB + FBO Ozon + FBS reserve' if dw+do > 3 else ('FBS-first (low velocity): keep FBO minimal, ship on demand' if dw+do > 0.5 else 'FBS only')
    if s == HC: mode = 'HYBRID: Ozon FBO + WB FBO limited (45d), FBS for bundles; no deep FBO stocking (negative WB margin)'
    if s == 'EVT-FC-MOIST-50': mode = 'Scenario B: after arrival split 45d cover to WB/Ozon FBO, rest at FF for set assembly'
    alloc.append(OD(internal_sku=s, name=NAME[s], daily_demand_wb=round(dw,2), daily_demand_ozon=round(do,2), wb_fbo_now=wb_now, wb_fbo_target=tw, wb_replenish_now=max(0,tw-wb_now),
        ozon_fbo_now_incl_transit=oz_now, ozon_fbo_target=to, ozon_replenish_now=max(0,to-oz_now), ff_operational_reserve=ff_res, ff_operational_now=INV[s]['ff_operational'],
        pull_from_pallets_now=max(0, max(0,tw-wb_now)+max(0,to-oz_now)+ff_res-INV[s]['ff_operational']), strategy=mode))
wr('FBO_FBS_ALLOCATION.csv', alloc)
# monthly supply plan: FBO stock rolls month to month; shipment = demand + target_end - opening FBO (>=0)
mrows = res[1]
fbo_w = {s: INV[s]['wb_fbo_live'] for s in BASE}; fbo_o = {s: INV[s]['ozon_fbo']+INV[s]['in_transit_to_ozon'] for s in BASE}
for i, ym in enumerate(MONTHS):
    for s in BASE:
        if s == 'EVT-HC-BODY-300': continue
        dem_w = sum(ch_month(x, ym, 'WB') for x in ALL if s in comps(x)); dem_o = sum(ch_month(x, ym, 'OZON') for x in ALL if s in comps(x))
        nxt = MONTHS[i+1] if i+1 < len(MONTHS) else ym
        dem_w_n = sum(ch_month(x, nxt, 'WB') for x in ALL if s in comps(x)); dem_o_n = sum(ch_month(x, nxt, 'OZON') for x in ALL if s in comps(x))
        tgt_w = round(1.5*dem_w_n); tgt_o = round(1.5*dem_o_n)   # 45 days of next month's demand at month end
        ship_w = max(0, round(dem_w + tgt_w - fbo_w[s])); ship_o = max(0, round(dem_o + tgt_o - fbo_o[s]))
        if s == 'EVT-FC-MOIST-50' and ym in ('2026-09','2026-10'): ship_w = ship_o = 0
        row = [r for r in mrows if r['internal_sku']==s and r['ym']==ym][0]
        close_w = max(0, fbo_w[s] + ship_w - dem_w); close_o = max(0, fbo_o[s] + ship_o - dem_o)
        supply_m.append(OD(ym=ym, internal_sku=s, name=NAME[s], forecast_demand_wb=round(dem_w), forecast_demand_ozon=round(dem_o), opening_total_stock=row['opening'], inbound=row['inbound_units'],
            opening_fbo_wb=round(fbo_w[s]), opening_fbo_ozon=round(fbo_o[s]), target_fbo_wb_end=tgt_w, target_fbo_ozon_end=tgt_o, shipment_wb=ship_w, shipment_ozon=ship_o,
            ff_reserve=max(20, round(0.35*(dem_w+dem_o)*0.7)), expected_closing_fbo_wb=round(close_w), expected_closing_fbo_ozon=round(close_o), expected_closing_total=row['closing']))
        fbo_w[s], fbo_o[s] = close_w, close_o
wr('SUPPLY_PLAN_MONTHLY.csv', supply_m)

# ---------------------------------------------------------------- 8-week operational plan (weeks from Mon 14.09.2026)
weeks = [dt.date(2026,9,14) + dt.timedelta(days=7*i) for i in range(8)]
w8 = []
for wi, wk in enumerate(weeks):
    ym = '2026-09' if wk.month == 9 else ('2026-10' if wk.month == 10 else '2026-11')
    for s in BASE:
        if s == 'EVT-HC-BODY-300': continue
        dem_w = sum(ch_month(x, ym, 'WB') for x in ALL if s in comps(x)); dem_o = sum(ch_month(x, ym, 'OZON') for x in ALL if s in comps(x))
        wk_w, wk_o = dem_w/4.33, dem_o/4.33
        if ym == '2026-09': wk_w, wk_o = wk_w/SEP_FRACTION, wk_o/SEP_FRACTION
        a = [x for x in alloc if x['internal_sku']==s][0]
        ship_w = a['wb_replenish_now'] if wi == 0 else (round(wk_w) if wi % 2 == 0 else 0)
        ship_o = a['ozon_replenish_now'] if wi == 0 else (round(wk_o*2) if wi % 2 == 1 else 0)
        if s == 'EVT-FC-MOIST-50' and wi < 5: ship_w = ship_o = 0
        sets_assembly = round(sum(ch_month(x, ym, 'WB')+ch_month(x, ym, 'OZON') for x in SETS if s in comps(x))/4.33/len([1]))
        alerts = []
        if s == 'EVT-FC-ACNE-50': alerts.append('RUNOUT Jan-2027 in BASE: order acne face cream now')
        if s == HC: alerts.append('LIQUIDATION: hold price >= 640 WB / 973 Ozon; ads only Ozon+bundles')
        if s == 'EVT-FC-MOIST-50': alerts.append('INBOUND 5000: release order this week; ETA FF ~20.10; WB/Ozon from Nov' if wi==0 else ('ETA FF this week' if wi==5 else ''))
        if s in ('EVT-HC-CHERRY-300','EVT-HC-AMBER-300'): alerts.append('190+ months of cover: bundle-only promotion, no solo ads')
        if wi == 0 and a['pull_from_pallets_now'] > 0: alerts.append(f"PULL {a['pull_from_pallets_now']} from pallets")
        w8.append(OD(week_start=wk.isoformat(), week=wi+1, internal_sku=s, name=NAME[s], target_sales_wb=round(wk_w), target_sales_ozon=round(wk_o), target_fbo_wb=a['wb_fbo_target'], target_fbo_ozon=a['ozon_fbo_target'],
            shipment_wb=ship_w, shipment_ozon=ship_o, bundle_units_to_assemble=sets_assembly, inventory_alert='; '.join(x for x in alerts if x)))
wr('SUPPLY_PLAN_8W.csv', w8)
json.dump({'assumptions': ASSUMPTIONS}, open(os.path.join(OUT,'assumptions.json'),'w'), ensure_ascii=False, indent=1)
print('hand cream sellable', hc_stock)
for r in hc_rows:
    if r['price_case'].startswith('current'): print(r['target_units_per_day'], r['runout_date'], 'contrib before ads', r['contribution_before_ads_month'], 'allow ads', r['allowable_ads_at_breakeven_month'], 'est ads', r['estimated_ads_needed_month'], 'after', r['contribution_after_ads_month'])
