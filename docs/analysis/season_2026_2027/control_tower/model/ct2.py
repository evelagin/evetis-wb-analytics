# -*- coding: utf-8 -*-
"""Control Tower artifacts (part 2): expiry, priority, SKU control, hand cream programs, bundle production plan,
WB/Ozon FBO plans, ads/price actions, plan-vs-actual, cash conversion, owner action queue."""
exec(open('ct.py').read())

# ---------------------------------------------------------------- helpers
def set_month(s, ym, ch=None):
    return sum(float(r['cards']) for r in det if r['internal_sku'] == s and r['ym'] == ym and (ch is None or r['channel'] == ch))
def set_base_units(s, ym, ch=None):
    return sum(set_month(x, ym, ch) for x in ALL if s in comps(x))
def closing(s, ym, sc=None):
    rr = sc[1] if sc else runout
    return [r['closing'] for r in rr if r['internal_sku'] == s and r['ym'] == ym][0]
wbl = {(r['internal_sku'], r['window']): r for r in rd('wb_limits.csv')}
def wb_actual(s, win='28'):
    r = wbl.get((s, win));
    if not r: return None
    q = int(r['buy_qty']); rub = float(r['buy_rub']); ads = float(r['ads'])
    return dict(qty=q, asp=rub/q if q else 0, drr=ads/rub if rub else 0, contrib_after=float(r['contrib_after_ads']), state=r['state'], ads=ads)
oz_aug = {}
for r in rd('ozon_monthly.csv'):
    if r['ym'] == '2026-08': oz_aug[r['internal_sku']] = (int(r['net_qty']), float(r['net_rub']))
oz_ads_aug = {r['internal_sku']: float(r['spend']) for r in rd('ozon_ads.csv') if r['ym'] == '2026-08'}
WEIGHT = {'EVT-HC-HAND-300': 0.391, 'EVT-HC-CHERRY-300': 0.391, 'EVT-HC-AMBER-300': 0.391, 'EVT-HC-BODY-300': 0.391, 'EVT-FT-ACNE-150': 0.215, 'EVT-FT-MOIST-150': 0.215,
          'EVT-FS-ACNE-30': 0.08, 'EVT-FS-MOIST-30': 0.08, 'EVT-EP-ENZYME-75': 0.14, 'EVT-FC-ACNE-50': 0.105, 'EVT-FC-MOIST-50': 0.105}
N('ASSUMPTION', 'Unit weights for PVZ lots (limit 25 kg per shipment, FACT from WB cabinet errors): hand/body cream 0.391 kg (FACT), tonic 0.215, serum 0.08, powder 0.14, face cream 0.105 (ASSUMPTION from packaging).')
def kg(s, q): return sum(WEIGHT[c] for c in comps(s))*q

# ---------------------------------------------------------------- EXPIRY CONTROL
exp_rows = []
for s in BASE:
    if s == 'EVT-HC-BODY-300': continue
    e, src = EXP[s]; units = INV[s]['total_sellable_units']
    months = max(0.1, (e - TODAY).days/30.4)
    cur = (VEL[s]['wb90'] + VEL[s]['oz90'])/3 + sum((VEL[x]['wb90']+VEL[x]['oz90'])/3 for x in SETS if s in comps(x))
    plan_m = sum(set_base_units(s, ym) for ym in MONTHS)/7
    req = units/months
    status = 'RED' if (months <= 4 or (req > cur*1.5 and months <= 15)) else ('YELLOW' if req > cur else 'GREEN')
    exp_rows.append(OD(internal_sku=s, name=NAME[s], batch=BATCH[s][0], import_date=str(BATCH[s][1]), expiry_date=str(e), expiry_source=src, units_now=units, value_rub=round(units*cogs[s]),
        months_to_expiry=round(months,1), required_units_per_month=round(req), current_units_per_month=round(cur), SET_plan_units_per_month=round(plan_m), gap_factor_vs_current=round(req/cur,1) if cur else '',
        sell_through_by_march_SET=round(units - closing(s, '2027-03')), residual_march_SET=closing(s, '2027-03'), expiry_priority=status,
        note=('HARD: liquidate to zero by 31.12.2026' if s=='EVT-HC-HAND-300' else 'sold out by Jan in plan; reorder 3 000 assumed Dec' if s=='EVT-FC-ACNE-50' else 'expiry cliff Oct-2027: needs Q1-Q2 2027 program' if BATCH[s][0]=='BATCH-05' else 'expiry Nov-2027: lot check on pallets for BATCH-03 remnants' if BATCH[s][0]=='BATCH-06' else 'expiry Jan-2028; marketplaces cannot clear it - external channel' if BATCH[s][0]=='BATCH-07' else '')))
inb_e = (dt.date(2026,8,20) + dt.timedelta(days=365*3)).isoformat()
exp_rows.append(OD(internal_sku='EVT-FC-MOIST-50 (batch 8 inbound)', name='Крем для лица УВЛ — партия 8', batch='BATCH-08', import_date='~2026-10-20 (ETA)', expiry_date=inb_e, expiry_source='OWNER_FACT 3 years from mfg (mfg ~Aug-2026 ASSUMPTION)', units_now=5000, value_rub=round(5000*130.5), months_to_expiry=36, required_units_per_month=139, current_units_per_month=0, SET_plan_units_per_month=round(sum(set_base_units('EVT-FC-MOIST-50', ym) for ym in MONTHS[2:])/5), gap_factor_vs_current='', sell_through_by_march_SET=5002-closing('EVT-FC-MOIST-50','2027-03'), residual_march_SET=closing('EVT-FC-MOIST-50','2027-03'), expiry_priority='GREEN', note='bundle unlocker for moisture serum/tonic'))
wr('EXPIRY_CONTROL.csv', exp_rows)
EXPR = {r['internal_sku']: r for r in exp_rows}

# ---------------------------------------------------------------- PRIORITY SCORE + ROLES + SKU CONTROL
def score(s):
    e = EXPR[s]; units = INV[s]['total_sellable_units']; value = units*cogs[s]
    months_cover = VEL[s]['cls']  # placeholder
    cur = float(e['current_units_per_month']); cover = units/cur if cur else 999
    mte = float(e['months_to_expiry'])
    sc = 0
    sc += min(25, value/4600000*25)                      # value at risk (max 25 at 4.6M)
    sc += 40 if mte <= 4 else 18 if mte <= 15 else 8     # expiry proximity (hard expiry dominates)
    sc += min(20, cover/200*20)                          # months of cover
    wbu = wb_unit(s, None, 0); cb = wbu['contribution_pct']
    sc += 15 if cb < 0.10 else 5 if cb < 0.20 else 0     # weak margin -> needs action
    sc += 10 if s in ('EVT-HC-CHERRY-300','EVT-HC-AMBER-300','EVT-FT-MOIST-150','EVT-EP-ENZYME-75','EVT-FT-ACNE-150') else 0  # slow demand trend
    sc += 10 if s == 'EVT-HC-HAND-300' else 0
    return min(100, round(sc))
ROLE = {'EVT-HC-HAND-300': 'LIQUIDATION', 'EVT-FC-MOIST-50': 'HERO + BUNDLE DRIVER (REPLENISH inbound)', 'EVT-FC-ACNE-50': 'HERO + BUNDLE DRIVER (REPLENISH)', 'EVT-FS-ACNE-30': 'CASH GENERATOR + BUNDLE DRIVER',
        'EVT-FS-MOIST-30': 'HERO + CASH GENERATOR', 'EVT-FT-ACNE-150': 'SLOW STOCK + BUNDLE RELEASE', 'EVT-FT-MOIST-150': 'SLOW STOCK + BUNDLE RELEASE', 'EVT-EP-ENZYME-75': 'SLOW STOCK + BUNDLE RELEASE',
        'EVT-HC-CHERRY-300': 'SLOW STOCK (liquidation channel needed)', 'EVT-HC-AMBER-300': 'SLOW STOCK (liquidation channel needed)', 'EVT-HC-BODY-300': 'NO STOCK'}
def prio(s, sc):
    if s == 'EVT-HC-HAND-300': return 'P0 URGENT LIQUIDATION'
    if s in ('EVT-FC-ACNE-50','EVT-FC-MOIST-50','EVT-FS-MOIST-30'): return 'P3 PROTECT / REPLENISH'
    if s in ('EVT-FS-ACNE-30',): return 'P2 NORMAL SELL (bundle driver)'
    return 'P1 ACCELERATE'
sku_rows = []
for s in BASE:
    if s == 'EVT-HC-BODY-300': continue
    e = EXPR[s]; units = INV[s]['total_sellable_units']; cur = float(e['current_units_per_month'])
    d30 = (VEL[s]['wb30'] + VEL[s]['oz30'] + sum(VEL[x]['wb30']+VEL[x]['oz30'] for x in SETS if s in comps(x)))/30
    tgt = set_base_units(s, '2026-10')/31
    wa = wb_actual(s); mp_stock = INV[s]['wb_fbo_live'] + INV[s]['ozon_fbo']
    dos_mp = mp_stock/d30 if d30 else 999
    sc = score(s)
    alerts = []
    if float(e['months_to_expiry']) <= 4: alerts.append('RED expiry <4m')
    if dos_mp < 7 and tgt > 1: alerts.append('RED FBO<7d')
    if units/cur > 180/30 if cur else True: alerts.append('RED stock>180d')
    if wa and wa['state'] in ('NEGATIVE_BEFORE_ADS',): alerts.append('RED negative contribution')
    if wa and wa['state'] == 'ABOVE_BREAKEVEN': alerts.append('RED DRR>ceiling')
    if s == 'EVT-FC-ACNE-50': alerts.append('YELLOW bundle component <5 months')
    action = {'EVT-HC-HAND-300': 'LIQUIDATE by 31.12: WB 640→490 step-down, Ozon 973→799, liquidation ads Nov-Dec, bundles руки+Cherry/Amber; ship WB 210 + Ozon 44 now',
              'EVT-FC-ACNE-50': 'PROTECT price; order 3 000 by 15.10 (arrival Dec); reserve for sets', 'EVT-FC-MOIST-50': 'RELEASE batch 8 now; price Ozon 848→1 200; 30% into moisture sets',
              'EVT-FS-MOIST-30': 'PROTECT; ship WB 336 now; watch: plan exhausts stock by Mar', 'EVT-FS-ACNE-30': 'SELL via ser+cream set; ship WB 49; verify lot (BATCH-03 remnants)',
              'EVT-FT-ACNE-150': 'ACCELERATE via Ozon + 4-step/ton+ser sets; expiry Oct-27 program from Jan', 'EVT-FT-MOIST-150': 'ACCELERATE via Ozon + moisture sets after batch 8; expiry Oct-27',
              'EVT-EP-ENZYME-75': 'ACCELERATE via 4-step set + Ozon; no WB solo ads; expiry Oct-27', 'EVT-HC-CHERRY-300': 'BUNDLE ONLY (Cherry+Amber, руки+Cherry); open wholesale channel',
              'EVT-HC-AMBER-300': 'BUNDLE ONLY (Cherry+Amber, руки+Amber); open wholesale channel'}[s]
    sku_rows.append(OD(internal_sku=s, name=NAME[s], role=ROLE[s], priority=prio(s, sc), sell_priority_score=sc, stock_total=units, ff_operational=INV[s]['ff_operational'], ff_pallets=INV[s]['ff_long_term'], wb_fbo=INV[s]['wb_fbo_live'], ozon_fbo=INV[s]['ozon_fbo'], in_transit=INV[s]['in_transit_to_ozon'],
        expiry=e['expiry_date'], months_to_expiry=e['months_to_expiry'], dos_total_at_30d_rate=round(units/(d30*30)*30) if d30 else 999, dos_marketplace=round(dos_mp) if dos_mp < 999 else 999,
        sales_per_day_30d=round(d30,1), target_per_day_SET_oct=round(tgt,1), wb_margin_before_ads_pct=round(wb_unit(s,None,0)['contribution_pct']*100,1), ozon_margin_before_ads_pct=round(oz_unit(s,None,0)['contribution_pct']*100,1) if oz_unit(s,None,0) else '',
        wb_drr_28d_pct=round(wa['drr']*100,1) if wa else '', wb_realized_asp_28d=round(wa['asp']) if wa and wa['qty'] else '', march_residual_SET=closing(s, '2027-03'), alerts='; '.join(alerts), action=action))
sku_rows.sort(key=lambda r: -r['sell_priority_score'])
wr('SKU_CONTROL.csv', sku_rows)

# ---------------------------------------------------------------- HAND CREAM PROGRAMS
hc_rows = []
for prog in ('HC-A','HC-B','HC-C','HC-D'):
    r = simulate2('SET', True, True, prog)
    d_ = r[0]; ru = r[1]
    for ym in ['2026-09','2026-10','2026-11','2026-12']:
        solo_rows = [x for x in d_ if x['internal_sku']=='EVT-HC-HAND-300' and x['ym']==ym]
        set_rows = [x for x in d_ if x['internal_sku'] in ('EVT-SET-HAND-CHERRY','EVT-SET-HAND-AMBER') and x['ym']==ym]
        solo = sum(float(x['cards']) for x in solo_rows); bund = sum(float(x['cards']) for x in set_rows)
        gmv = sum(float(x['gmv']) for x in solo_rows); cash = sum(float(x['seller_cash']) for x in solo_rows); contrib = sum(float(x['contribution']) for x in solo_rows); ads = sum(float(x['ads']) for x in solo_rows)
        bc = sum(float(x['contribution']) for x in set_rows); bcash = sum(float(x['seller_cash']) for x in set_rows)
        asp = gmv/solo if solo else 0
        hc_rows.append(OD(program=prog, ym=ym, days=DAYS[ym], hand_cream_units_total=round(solo+bund), units_per_day=round((solo+bund)/DAYS[ym],1), solo_units=round(solo), bundle_units=round(bund), asp_solo=round(asp), ads_solo=round(ads), drr_solo_pct=round(ads/gmv*100,1) if gmv else '',
            seller_cash_solo=round(cash), contribution_solo=round(contrib), contribution_bundles=round(bc), seller_cash_bundles=round(bcash), residual_end_of_month=[x['closing'] for x in ru if x['internal_sku']=='EVT-HC-HAND-300' and x['ym']==ym][0]))
    tot_units = sum(x['hand_cream_units_total'] for x in hc_rows if x['program']==prog); tot_contrib = sum(x['contribution_solo']+x['contribution_bundles'] for x in hc_rows if x['program']==prog); tot_cash = sum(x['seller_cash_solo']+x['seller_cash_bundles'] for x in hc_rows if x['program']==prog)
    resid = [x['closing'] for x in ru if x['internal_sku']=='EVT-HC-HAND-300' and x['ym']=='2026-12'][0]
    hc_rows.append(OD(program=prog, ym='TOTAL Sep-Dec', days=113, hand_cream_units_total=tot_units, units_per_day=round(tot_units/113,1), solo_units='', bundle_units='', asp_solo='', ads_solo='', drr_solo_pct='', seller_cash_solo='', contribution_solo='', contribution_bundles='', seller_cash_bundles='',
        residual_end_of_month=resid))
    hc_rows[-1]['note'] = f"cost of liquidation (contribution) {round(tot_contrib)} RUB; cash generated {round(tot_cash)} RUB; write-off if not sold {round(resid*231.38)} RUB; alternative write-off of ALL 2 952 = 683 033 RUB"
wr('HAND_CREAM_PROGRAMS.csv', hc_rows)
HC_SUMMARY = {p: [x for x in hc_rows if x['program']==p and x['ym']=='TOTAL Sep-Dec'][0] for p in ('HC-A','HC-B','HC-C','HC-D')}

# ---------------------------------------------------------------- BUNDLE PRODUCTION PLAN (8 weeks from Mon 14.09)
weeks = [TODAY + dt.timedelta(days=(7 - TODAY.weekday()) % 7) + dt.timedelta(days=7*i) for i in range(8)]
bp = []
for wi, wk0 in enumerate(weeks):
    ym = '2026-09' if wk0.month == 9 else ('2026-10' if wk0.month == 10 else '2026-11')
    for s in SETS:
        if s == 'EVT-SET-HAND-BODY': continue
        wb_m = set_month(s, ym, 'WB'); oz_m = set_month(s, ym, 'OZON')
        dpm = DAYS[ym]
        wk_wb = wb_m/dpm*7; wk_oz = oz_m/dpm*7
        # assemble every 2 weeks: two weeks of demand + 20% safety, on even weeks; odd weeks = 0 unless first week
        assemble = round((wk_wb + wk_oz)*2*1.2) if wi % 2 == 0 else 0
        if (wk_wb + wk_oz) <= 0: continue
        comp_txt = ' + '.join(f"{NAME[c]} x{assemble}" for c in comps(s)) if assemble else ''
        limiting = min(comps(s), key=lambda c: INV[c]['total_sellable_units'])
        bp.append(OD(week=wi+1, week_start=wk0.isoformat(), bundle=s, name=NAME[s], qty_assemble=assemble, components_consumed=comp_txt, units_consumed=assemble*len(comps(s)), sell_target_wb=round(wk_wb), sell_target_ozon=round(wk_oz),
            marketplace='WB+Ozon' if wk_oz > 0.5 else 'WB', limiting_component=NAME[limiting], on_hand_limit=INV[limiting]['total_sellable_units'],
            note='BLOCKED until batch 8 arrives (~20.10)' if 'EVT-FC-MOIST-50' in comps(s) and wk0 < dt.date(2026,10,26) else ('liquidation set: hand cream inside' if 'EVT-HC-HAND-300' in comps(s) else '')))
wr('BUNDLE_PRODUCTION_PLAN.csv', bp)

# ---------------------------------------------------------------- WB FBO PLAN (PVZ lots <= 25 kg, 4-5 lots/week)
LEAD_WB = 12; Z = 1.65
wbp = []
for s in BASE:
    if s == 'EVT-HC-BODY-300': continue
    d_oct = set_base_units(s, '2026-10', 'WB')/31; d_nov = set_base_units(s, '2026-11', 'WB')/30
    target45 = round(45*d_oct + Z*math.sqrt(max(LEAD_WB*d_oct, 0.01)))
    live = INV[s]['wb_fbo_live']; ship = max(0, target45 - live)
    if s == 'EVT-FC-MOIST-50': ship = 0; target45 = round(45*set_base_units(s,'2026-11','WB')/30)
    nov_build = max(0, round(45*set_base_units(s,'2026-12','WB')/31) - round(45*d_nov))  # December pre-build in Nov
    wbp.append(OD(internal_sku=s, name=NAME[s], wb_live_now=live, wb_daily_demand_SET_oct=round(d_oct,1), wb_daily_demand_SET_nov=round(d_nov,1), target_fbo_45d=target45, ship_now=ship, ship_kg=round(kg(s, ship),1), pvz_lots_25kg=math.ceil(kg(s, ship)/25) if ship else 0,
        days_cover_after_shipment=round((live+ship)/d_oct) if d_oct else '', december_prebuild_extra_units_in_nov=nov_build,
        destination='any PVZ (WB routes to warehouses itself since 15.08.2026; Kolyedino/Elektrostal/Krasnodar/Tula hold the LOST lots - not seller-selectable anymore)',
        constraint='25 kg per shipment (FACT), 500 units, 200 L; 4-5 shipments/week needed for portfolio', note='NOT_PROVEN: acceptance slot must be confirmed in cabinet before Tuesday'))
wr('WB_FBO_PLAN.csv', wbp)
# weekly lot schedule for 8 weeks
lots = []
for wi, wk0 in enumerate(weeks):
    ym = '2026-09' if wk0.month == 9 else ('2026-10' if wk0.month == 10 else '2026-11')
    total_kg = 0; lines = []
    for s in BASE:
        if s == 'EVT-HC-BODY-300': continue
        weekly = set_base_units(s, ym, 'WB')/DAYS[ym]*7
        if wi == 0: q = [x for x in wbp if x['internal_sku']==s][0]['ship_now']
        else: q = round(weekly)
        if s == 'EVT-FC-MOIST-50' and wi < 6: q = 0
        if q <= 0: continue
        lines.append(f"{NAME[s]} {q}"); total_kg += kg(s, q)
    lots.append(OD(week=wi+1, week_start=wk0.isoformat(), shipments=' · '.join(lines), total_units=sum(int(x.split()[-1]) for x in lines) if lines else 0, total_kg=round(total_kg,1), pvz_lots_needed=math.ceil(total_kg/25), lots_per_working_day=round(math.ceil(total_kg/25)/5,1)))
wr('WB_PVZ_SCHEDULE_8W.csv', lots)

# ---------------------------------------------------------------- OZON FBO PLAN
ozp = []
for s in ALL:
    if s in ('EVT-HC-BODY-300','EVT-SET-HAND-BODY') or oz_unit(s, None, 0) is None: continue
    d30 = set_month(s, '2026-10', 'OZON'); d45 = d30*1.5
    now = oz_stock.get(s, 0) + (INV[s]['in_transit_to_ozon'] if s in INV else 0)
    target = round(d45 + Z*math.sqrt(max(d30/30*10, 0.01)))
    ship = max(0, target - now)
    if s == 'EVT-FC-MOIST-50': ship = 0
    uo = oz_unit(s, None, 0); P = uo['price']
    ozp.append(OD(internal_sku=s, name=NAME[s], ozon_fbo_now=oz_stock.get(s,0), in_transit=INV[s]['in_transit_to_ozon'] if s in INV else 0, demand_30d_SET_oct=round(d30), demand_45d=round(d45), target_fbo=target, recommended_shipment_now=ship,
        price=round(P), contribution_before_ads_pct=round(uo['contribution_pct']*100,1), target_drr_pct=round(plan_drr(s, 'OZON', 'SET', P, oz_unit)*100,1), ad_class=ad_class(s),
        cadence='weekly' if d30 >= 30 else 'bi-weekly', cluster_note='allocate by cluster with IPF method (plan 03.09): Moscow/Rostov/Kazan/Ufa/Perm/SPb first; Krasnoyarsk small'))
wr('OZON_FBO_PLAN.csv', ozp)

# ---------------------------------------------------------------- ADVERTISING ACTIONS
ad_rows = []
for s in ALL:
    if s in ('EVT-HC-BODY-300','EVT-SET-HAND-BODY'): continue
    for ch, fn in (('WB', wb_unit), ('OZON', oz_unit)):
        u = fn(s, None, 0)
        if u is None or (s == 'EVT-SET-ACNE-POWDER-SERUM-CREAM' and ch == 'WB'): continue
        cb = u['contribution_pct']; cls = ad_class(s)
        target_after = 0.15 if cls == 'ADVERTISED' else 0.10 if cls == 'ORGANIC_HALO' else 0.0 if cls == 'LIQUIDATION_ADS' else 0.12
        ceiling = max(0.0, cb)
        working = plan_drr(s, ch, 'SET', u['price'], fn) if cls != 'LIQUIDATION_ADS' else (0.06 if ch=='WB' else 0.08)
        if ch == 'WB':
            wa = wb_actual(s); actual = wa['drr'] if wa and wa['qty'] else None; state = wa['state'] if wa else ''
        else:
            q, rub = oz_aug.get(s, (0, 0)); actual = oz_ads_aug.get(s, 0)/rub if rub else None; state = ''
        if cls == 'LIQUIDATION_ADS': status = 'LIQUIDATION ADS (Nov-Dec 15-18%; Sep-Oct 6-8%)'
        elif cls == 'BUNDLE_SUPPORTED': status = 'ORGANIC ONLY (sold via Cherry+Amber / руки+ sets ads)'
        elif actual is None or actual == 0: status = 'SCALE ADS' if cls == 'ADVERTISED' and cb > 0.20 else 'KEEP (organic)'
        elif actual > ceiling: status = 'STOP' if actual > 1.5*ceiling else 'REDUCE'
        elif actual > working*1.2: status = 'REDUCE'
        elif actual < working*0.6 and cls == 'ADVERTISED': status = 'SCALE ADS'
        else: status = 'KEEP'
        ad_rows.append(OD(internal_sku=s, name=NAME[s], channel=ch, ad_class=cls, price=round(u['price']), margin_before_ads_pct=round(cb*100,1), target_contribution_after_ads_pct=round(target_after*100), drr_ceiling_pct=round(ceiling*100,1), working_drr_target_pct=round(working*100,1),
            actual_drr_pct=round(actual*100,1) if actual is not None else 'n/a', actual_window='WB 28d to 08.09' if ch=='WB' else 'Ozon Aug-2026 attributed', economic_state=state, status=status,
            monthly_budget_SET_oct=round(sum(float(r['ads']) for r in det if r['internal_sku']==s and r['ym']=='2026-10' and r['channel']==ch))))
wr('ADVERTISING_ACTIONS.csv', ad_rows)

# ---------------------------------------------------------------- PRICE ACTIONS
pa = []
for s in ALL:
    if s in ('EVT-HC-BODY-300','EVT-SET-HAND-BODY'): continue
    for ch, fn in (('WB', wb_unit), ('OZON', oz_unit)):
        u = fn(s, None, 0)
        if u is None or (s == 'EVT-SET-ACNE-POWDER-SERUM-CREAM' and ch == 'WB'): continue
        P = u['price']; cb = u['contribution_pct']; floor = price_for_margin(fn, s, 0.0, 0.0)
        if ch == 'WB':
            wa = wb_actual(s); realized = round(wa['asp']) if wa and wa['qty'] else ''
        else:
            q, rub = oz_aug.get(s, (0,0)); realized = round(rub/q) if q else ''
        drr = plan_drr(s, ch, 'SET', P, fn)
        if s == 'EVT-HC-HAND-300':
            action, rec, promo = 'LIQUIDATION', (640 if ch=='WB' else 973), ('590 Oct → 540 Nov → 490 Dec (floor 450)' if ch=='WB' else '895 Oct → 850 Nov → 799 Dec (floor 699)')
        elif s in ('EVT-SET-HAND-CHERRY','EVT-SET-HAND-AMBER'):
            action, rec, promo = 'LIQUIDATION', round(P), f"{round(P*0.95)} Oct → {round(P*0.90)} Nov-Dec"
        elif s == 'EVT-FC-MOIST-50' and ch == 'OZON':
            action, rec, promo = 'RAISE', 1200, '1 140 on events'
        elif s in ('EVT-SET-4PC-ACNE','EVT-SET-TON-SER-CREAM-ACNE') and ch == 'WB':
            action, rec, promo = 'RAISE', round(P*1.06), f"{round(P)} on events"
        elif s in ('EVT-FT-MOIST-150','EVT-HC-CHERRY-300','EVT-FT-ACNE-150') and ch == 'OZON':
            action, rec, promo = 'RAISE', {'EVT-FT-MOIST-150':1367,'EVT-HC-CHERRY-300':1378,'EVT-FT-ACNE-150':1324}[s], 'hold new price, -8% only on events'
        elif realized and isinstance(realized, int) and realized < 0.9*P and ch == 'WB':
            action, rec, promo = 'HOLD (check auto-promos: realized < 90% of list)', round(P), f"{round(P*0.95)} on events"
        elif VEL[s]['cls'].startswith('D-OVERSTOCK') or VEL[s]['cls'].startswith('C-') or VEL[s]['cls'].startswith('E-'):
            action, rec, promo = 'PROMO ONLY', round(P), f"{round(P*0.92)} on 11.11 / BF / NY / 8 March"
        elif cb > 0.25:
            action, rec, promo = 'HOLD', round(P), f"{round(P*0.95)} on events"
        else:
            action, rec, promo = 'TEST', round(P*1.05), f"{round(P)} on events"
        u2 = fn(s, rec, drr)
        pa.append(OD(internal_sku=s, name=NAME[s], channel=ch, action=action, current_list_price=round(P), current_realized_price=realized, recommended_shelf_price=rec, promo_range=promo, drr_assumption_pct=round(drr*100,1), expected_margin_after_ads_pct=round(u2['contribution_pct']*100,1) if u2 else '', absolute_floor_no_ads=floor, margin_before_ads_now_pct=round(cb*100,1)))
wr('PRICE_ACTIONS.csv', pa)

# ---------------------------------------------------------------- PLAN VS ACTUAL (September MTD 1-8.09 actual; plan = SET)
sep_plan_cards = agg[('2026-09','cards')]; sep_days_plan = 22
act = dict(wb_orders=107, oz_orders=36, wb_buyouts=110, wb_gmv=75686, oz_gmv=53119, wb_ads=10996, oz_ads=7327, bundles=sum(hist[('2026-09', s, 'WB')]['sold_qty'] for s in BOM) + sum(hist[('2026-09', s, 'OZON')]['sold_qty'] for s in BOM))
act_units = act['wb_orders'] + act['oz_orders']
act_bottles = sum(hist[('2026-09', s, c)]['sold_qty']*len(comps(s)) for s in ALL for c in ('WB','OZON'))
plan_rate = sep_plan_cards/22   # per day from 09.09
pva = []
def pv(kpi, plan_mtd, actual_mtd, fc_eom, unit=''):
    delta = actual_mtd - plan_mtd if isinstance(plan_mtd, (int,float)) else ''
    ratio = actual_mtd/plan_mtd if plan_mtd else None
    status = 'GREEN' if ratio is not None and ratio >= 0.9 else 'YELLOW' if ratio is not None and ratio >= 0.7 else 'RED'
    pva.append(OD(kpi=kpi, plan_mtd_1_8_sep=round(plan_mtd) if isinstance(plan_mtd,(int,float)) else plan_mtd, actual_mtd_1_8_sep=round(actual_mtd), delta=round(delta) if delta != '' else '', pct_of_plan=round(ratio*100) if ratio is not None else '', forecast_eom=round(fc_eom), status=status, unit=unit))
pv('cards sold (orders)', plan_rate*8, act_units, act_units + sep_plan_cards, 'cards')
pv('physical units (bottles)', agg[('2026-09','bottles')]/22*8, act_bottles, act_bottles + agg[('2026-09','bottles')], 'bottles')
pv('WB cards', agg[('2026-09','cards_WB')]/22*8, act['wb_orders'], act['wb_orders'] + agg[('2026-09','cards_WB')], 'cards')
pv('Ozon cards', agg[('2026-09','cards_OZON')]/22*8, act['oz_orders'], act['oz_orders'] + agg[('2026-09','cards_OZON')], 'cards')
pv('bundle cards', agg[('2026-09','bundle_cards')]/22*8, act['bundles'], act['bundles'] + agg[('2026-09','bundle_cards')], 'cards')
pv('revenue (GMV)', agg[('2026-09','gmv')]/22*8, act['wb_gmv'] + act['oz_gmv'], act['wb_gmv'] + act['oz_gmv'] + agg[('2026-09','gmv')], 'RUB')
pv('advertising', agg[('2026-09','ads')]/22*8, act['wb_ads'] + act['oz_ads'], act['wb_ads'] + act['oz_ads'] + agg[('2026-09','ads')], 'RUB')
drr_plan = agg[('2026-09','ads')]/agg[('2026-09','gmv')]*100; drr_act = (act['wb_ads']+act['oz_ads'])/(act['wb_gmv']+act['oz_gmv'])*100
pva.append(OD(kpi='DRR %', plan_mtd_1_8_sep=round(drr_plan,1), actual_mtd_1_8_sep=round(drr_act,1), delta=round(drr_act-drr_plan,1), pct_of_plan='', forecast_eom=round(drr_plan,1), status='GREEN' if drr_act <= drr_plan*1.2 else 'RED', unit='%'))
contrib_act = sum(float(wbl[(s,'7')]['contrib_after_ads']) for s in ALL if (s,'7') in wbl)  # WB 7d proxy
pv('contribution after ads (WB 7d proxy)', agg[('2026-09','contrib')]/22*7*0.8, contrib_act, contrib_act + agg[('2026-09','contrib')], 'RUB')
pv('stock reduction (bottles sold)', agg[('2026-09','bottles')]/22*8, act_bottles, act_bottles + agg[('2026-09','bottles')], 'bottles')
wr('PLAN_VS_ACTUAL.csv', pva)
N('FACT', 'September 1-8 actuals: WB orders 107 (FACT_ORDERS), Ozon 36 postings, WB buyouts 110 cards / 75 686 RUB, Ozon 53 119 RUB, ads WB 10 996 (billing) + Ozon 7 327 (attributed).')

# ---------------------------------------------------------------- CASH CONVERSION
cc = []
for name, r in SCN.items():
    sm = r[5]
    cc.append(OD(scenario=name, opening_inventory_at_cost=sm['opening_inventory_value'], cogs_sold=sm['cogs_sold'], seller_cash_generated=sm['seller_cash'], contribution=sm['contribution'], opex_sep_mar=sm['opex'],
        hand_cream_writeoff=sm['hand_cream_writeoff_rub'], operating_result=sm['operating_result'], closing_inventory_at_cost=sm['closing_inventory_value'], inventory_cash_conversion_pct=sm['inventory_cash_conversion_pct'], stock_reduction_pct=sm['stock_reduction_pct'],
        opening_units=sm['opening_units'], march_residual_units=sm['march_residual_units']))
wr('CASH_CONVERSION.csv', cc)

# ---------------------------------------------------------------- BUNDLE-FIRST FORECAST (physical units by piece count)
bf = []
for r in mon:
    bf.append(OD(ym=r['ym'], solo_units=r['pieces_1'], units_in_2piece=r['pieces_2'], units_in_3piece=r['pieces_3'], units_in_4piece=r['pieces_4'], total_physical_units=r['bottles'], bundle_share_of_units_pct=round((r['bottles']-r['pieces_1'])/r['bottles']*100,1) if r['bottles'] else 0, bundle_cards=r['bundle_cards']))
wr('BUNDLE_FIRST_FORECAST.csv', bf)

# ---------------------------------------------------------------- OWNER ACTION QUEUE
Q = []
def q(p, dl, action, sku, qty, mp, fin, why, status='OPEN'): Q.append(OD(priority=p, deadline=dl, action=action, sku_or_bundle=sku, qty=qty, marketplace=mp, financial_effect=fin, why=why, status=status))
ship_wb = {x['internal_sku']: x['ship_now'] for x in wbp}; ship_oz = {x['internal_sku']: x['recommended_shipment_now'] for x in ozp}
# component need for sets: Ozon set shipment now vs week-1 assembly -> take the larger per component (assembled sets feed the shipment)
comp_oz = {}; comp_w1 = {}
for k, v in ship_oz.items():
    if k in BOM:
        for c in BOM[k]: comp_oz[c] = comp_oz.get(c, 0) + v
for x in bp:
    if x['week'] == 1:
        for c in BOM[x['bundle']]: comp_w1[c] = comp_w1.get(c, 0) + x['qty_assemble']
comp_need = {s: max(comp_oz.get(s, 0), comp_w1.get(s, 0)) for s in BASE}
pull = {s: min(INV[s]['ff_long_term'], max(0, ship_wb.get(s,0) + (ship_oz.get(s,0) if s not in BOM else 0) + comp_need[s] + 20 - INV[s]['ff_operational'])) for s in BASE if s != 'EVT-HC-BODY-300'}
print('PULL', pull, sum(pull.values()), 'comp_need', comp_need)
q('P0 — TODAY', '10.09', f"Фулфилмент: снять с паллет и переложить на оперативную полку {sum(pull.values())} флаконов: " + ', '.join(f"{NAME[s]} {v}" for s, v in pull.items() if v > 0), 'портфель', sum(pull.values()), 'FF', 'разблокирует поставки WB/Ozon этой недели (≈0,5 млн ₽ GMV в сентябре)', 'оперативная полка 610 ед после отгрузок на Ozon 07.09; сыворотка АКНЕ, крем для рук и крем АКНЕ на полке = 0; паллеты упакованы к переезду — нужно распаковать')
q('P0 — TODAY', '10.09', 'Отпустить партию 8 (5 000 крем УВЛ) — команда поставщику на отгрузку + доплата', 'EVT-FC-MOIST-50', 5000, 'China→FF', '+1,05 млн ₽ денег и +0,48 млн ₽ вклада до марта (SET vs без партии)', 'крем УВЛ = SKU №1 по выручке 2025/26, 2 шт на компании; разблокирует 3 набора УВЛ', 'НУЖНО РЕШЕНИЕ ВЛАДЕЛЬЦА')
q('P0 — TODAY', '10.09', 'WB: проверить в кабинете доступность приёмки (ПВЗ, лимит 25 кг/поставка) и создать первые 4 поставки', 'портфель', ship_wb and sum(ship_wb.values()), 'WB', 'WB = 76 % планового оборота; без слота сентябрь −0,4 млн ₽', 'RAW_WB_SUPPLIES: 16 черновиков без склада; приёмка NOT_PROVEN')
q('P0 — TODAY', '10.09', 'Крем для рук: снять рекламу с одиночной карточки WB, цена держать 640; Ozon 973; включить наборы руки+Cherry/Amber в промо', 'EVT-HC-HAND-300', INV['EVT-HC-HAND-300']['total_sellable_units'], 'WB+Ozon', 'останавливает −79 ₽/флакон после рекламы при 7 шт/день; программа HC-B: cash ≈ 0,5 млн, cost −0,28 млн vs списание −0,68 млн', 'срок годности декабрь 2026 (HARD); реализованная цена 418–494 при прайсе 640')
q('P1 — THIS WEEK', '12.09', f"Отгрузить WB через ПВЗ: {', '.join(f'{NAME[s]} {v}' for s, v in ship_wb.items() if v > 0)}", 'портфель', sum(ship_wb.values()), 'WB', 'восстанавливает наличие 8 SKU; сентябрь–октябрь план 1 882 карточки WB', '4 из 10 SKU на WB в нуле или почти')
q('P1 — THIS WEEK', '12.09', f"Отгрузить Ozon FBO: {', '.join(f'{NAME[s]} {v}' for s, v in ship_oz.items() if v > 0)} (после прихода 369 в пути)", 'портфель', sum(ship_oz.values()), 'Ozon', 'Ozon план октября 350 карточек (Aug факт 180)', 'крем для рук и сыворотка АКНЕ на Ozon = 0')
q('P1 — THIS WEEK', '13.09', 'Собрать наборы недели 1 по BUNDLE_PRODUCTION_PLAN (сыв+крем АКНЕ, 4 шага, тон+сыв+крем АКНЕ, Cherry+Amber, руки+Cherry, руки+Amber, тоник+сыв)', 'наборы', sum(x['qty_assemble'] for x in bp if x['week']==1), 'FF', 'наборы = 35 % физического оборота плана; вклад 8–20 %', 'на ФФ наборов как позиций нет; сборка под отгрузку')
q('P1 — THIS WEEK', '12.09', 'Реклама: STOP на WB для пудры, Amber, тоника УВЛ, набора тоник+сыв УВЛ (выше потолка); перенести бюджет на сыворотки, кремы для лица, набор сыв+крем АКНЕ (ADVERTISING_ACTIONS)', 'реклама', '', 'WB', '−94 тыс ₽ за 90 дн по шести объектам → 0; план ДРР 8–10 %', 'V_ADS_SKU_ECONOMIC_LIMITS: 6 объектов ABOVE_BREAKEVEN')
q('P1 — THIS WEEK', '12.09', 'Цены Ozon: тоник УВЛ 1 197→1 367, Cherry 1 209→1 378, тоник АКНЕ 1 219→1 324 (план 06.09); WB: набор 4 шага +6 %, тон+сыв+крем АКНЕ +6 %', 'цены', '', 'WB+Ozon', '+4–9 п.п. маржи на этих SKU', 'наборы на WB стоят ниже суммы компонентов')
q('P1 — THIS WEEK', '12.09', 'Usend письменно: (1) состав и статус «переезда» паллет, (2) партии/сроки годности на паллетах (сыворотка АКНЕ — есть ли остаток партии 3), (3) свежий срез оперативной полки, (4) долг −40 692 ₽', 'ФФ', '', 'FF', 'снимает блокер отгрузок; подтверждает expiry-модель', 'ФФ сообщил об упаковке паллет к переезду; сроки годности партии 3 ≈ декабрь 2026')
q('P2 — NEXT 2 WEEKS', '25.09', 'Запросить производство крема для лица АКНЕ 3 000–4 000 шт (аванс, ETA декабрь)', 'EVT-FC-ACNE-50', 3000, 'China', 'без заказа: −1 100 бутылок и −87 тыс ₽ вклада в янв–мар, 4 анти-акне набора уходят с полки', 'по плану SET крем АКНЕ кончается в январе', 'НУЖНО РЕШЕНИЕ ВЛАДЕЛЬЦА')
q('P2 — NEXT 2 WEEKS', '25.09', 'Запустить WB FBS для наборов, Cherry/Amber, тоников (резерв на ФФ 20–75 ед/SKU)', 'наборы + медленные SKU', '', 'WB FBS', 'наличие без FBO-запаса; наборы собираются под заказ', 'FBS = 0 сегодня')
q('P2 — NEXT 2 WEEKS', '25.09', 'Проверить эластичность крема для рук: 2 недели WB 640 без рекламы → если < 4/день, перейти на 590 с 1.10 (HC-B)', 'EVT-HC-HAND-300', '', 'WB', 'решает цену октября: 590 vs 640 = −16 ₽/флакон × 330', 'нет данных об эластичности')
q('P3 — THIS MONTH', '30.09', 'Открыть внешний канал для Cherry/Amber (опт/офлайн/сети): 9 095 шт, срок годности ~январь 2028', 'EVT-HC-CHERRY-300 / AMBER', 9095, 'вне МП', 'опт по 350–400 ₽ = до 3 млн ₽ денег; маркетплейсы за сезон снимают ~700', 'экспирация января 2028 при темпе 40/мес', 'НУЖНО РЕШЕНИЕ ВЛАДЕЛЬЦА')
q('P1 — THIS WEEK', '12.09', 'WB: выяснить в кабинете/поддержке, действует ли лимит 25 кг только для ПВЗ и доступна ли поставка коробами/паллетой через СЦ или транзит; зафиксировать ответ', 'портфель', '', 'WB', 'снимает узкое место сезона: декабрь SET = 5 420 флаконов WB = 1 000 кг = 40 лотов по 25 кг', 'при 4–5 лотах в неделю (≈110 кг ≈ 550 фл.) на WB въезжает ≈2 400 фл./мес — хватает на октябрь, но не на ноябрь–декабрь', 'OPEN')
q('P3 — THIS MONTH', '30.09', 'Загрузка WB под декабрь: с 1 октября по 30 ноября отгрузить ≈8 800 флаконов (окт 1 532 + ноя 3 180 + ¾ декабря 4 065) = ≈67 лотов по 25 кг = 7–8 лотов в неделю; расписать ПВЗ-отгрузки по дням, при невозможности — короба через СЦ или часть декабря на FBS/Ozon', 'портфель', 8800, 'WB', 'декабрь = 30 % карточек и 28 % GMV сезона (4,2 млн ₽); недовоз 1 000 фл. = −0,7 млн ₽ GMV', 'лимит 25 кг на поставку (FACT); 4–5 отгрузок в неделю (ASSUMPTION) недостаточно')
q('P3 — THIS MONTH', '30.09', 'Программа тоники/пудра до октября 2027 (экспирация партии 5): наборы + Ozon + опт; целевой темп от 850/мес с января', 'BATCH-05 (11 460 шт)', 11460, 'все', 'риск списания до 1,8 млн ₽ в октябре 2027', 'expiry Oct-2027, текущий темп ~100/мес')
q('P4 — WATCH', 'еженедельно', 'Сыворотка УВЛ: план SET исчерпывает запас к марту — решить о дозаказе в январе', 'EVT-FS-MOIST-30', 3948, 'China', 'SKU с маржой 25–28 %', 'runout март 2027 в SET')
q('P4 — WATCH', 'еженедельно', 'Приёмка партии 8 (ETA ФФ ~20.10) → 45 дней на WB/Ozon, остальное под наборы УВЛ', 'EVT-FC-MOIST-50', 5000, 'FF→WB/Ozon', '', 'ETA ASSUMPTION')
wr('OWNER_ACTION_QUEUE.csv', Q)
# refresh SKU_CONTROL actions with computed shipment quantities
for r in sku_rows:
    s = r['internal_sku']
    if s == 'EVT-HC-HAND-300': r['action'] = f"LIQUIDATE by 31.12 (HC-B): WB 640→590 Oct→540 Nov→490 Dec, Ozon 973→895→850→799; ads WB 6%→17%, Ozon 8-10%; sets руки+Cherry/Amber; ship WB {ship_wb.get(s,0)} + Ozon {ship_oz.get(s,0)} now; pull {pull.get(s,0)} from pallets"
    elif s in ship_wb: r['action'] = r['action'].split(';')[0] + f"; ship WB {ship_wb.get(s,0)} / Ozon {ship_oz.get(s,0)} now; pull {pull.get(s,0)} from pallets"
wr('SKU_CONTROL.csv', sku_rows)
json.dump(A2, open(os.path.join(OUT, 'assumptions_v2.json'), 'w'), ensure_ascii=False, indent=1)
print('PULL', pull, sum(pull.values()))
print('SHIP WB', ship_wb, sum(ship_wb.values()))
print('SHIP OZ', ship_oz, sum(ship_oz.values()))
print('HC', {p: (v['hand_cream_units_total'], v['residual_end_of_month'], v['note']) for p, v in HC_SUMMARY.items()})
print('BP week1', [(x['bundle'][8:], x['qty_assemble']) for x in bp if x['week']==1])
print('LOTS', [(l['week'], l['total_units'], l['total_kg'], l['pvz_lots_needed']) for l in lots])
