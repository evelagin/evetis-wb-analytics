# EVETIS WB logistics — FINAL pass model (14.09.2026). READ-ONLY. Supersedes unit-economics blocks of validation_model.py.
# Status labels: CONTRACT (Usend price list 01.01.2026, VAT 5% incl.), SPECIAL (owner agreement, handwritten 20.05.2026),
# OBSERVED (Usend detalization 02.03–11.08.2026, WB/Ozon supplies in BigQuery), OFFICIAL (WB/Ozon rules), ASSUMED, UNKNOWN.
import math, json, datetime as dt

TODAY = dt.date(2026, 9, 14)

# ---------------- SKU master (OBSERVED: V_WB_SKU_FORWARD_ECONOMICS_CURRENT 14.09, MART_SKU_DAILY 16.06–13.09, RAW_WB_PAID_STORAGE, STAGE_C0 Ozon cards) ----------------
# price, cogs, buyout, vol_wb (l), orders_90d, ad_share, weight kg, dims mm, usend_vol m3, comps (for sets)
SKU = {
 "FS-MOIST-30":  dict(price=699.2,  cogs=130.81, b=0.934, vol=0.25,  o90=122, ad=0.311, kg=0.136, mm=(40,40,100), uv=0.00025),
 "FS-ACNE-30":   dict(price=805.6,  cogs=135.78, b=0.850, vol=0.16,  o90=127, ad=0.630, kg=0.136, mm=(40,40,100), uv=0.00025),
 "FC-ACNE-50":   dict(price=799.6,  cogs=131.66, b=0.807, vol=0.272, o90=119, ad=0.529, kg=0.161, mm=(40,40,165), uv=0.000264),
 "FC-MOIST-50":  dict(price=925.6,  cogs=130.78, b=0.929, vol=0.332, o90=127, ad=0.315, kg=0.161, mm=(40,40,165), uv=0.000264),
 "FT-ACNE-150":  dict(price=740.0,  cogs=182.79, b=0.928, vol=0.40,  o90=97,  ad=0.691, kg=0.200, mm=(43,43,155), uv=0.000287),
 "FT-MOIST-150": dict(price=740.0,  cogs=182.79, b=0.923, vol=0.40,  o90=52,  ad=0.731, kg=0.200, mm=(43,43,155), uv=0.000287),
 "EP-ENZYME-75": dict(price=666.0,  cogs=159.30, b=0.864, vol=0.35,  o90=66,  ad=0.667, kg=0.125, mm=(43,43,133), uv=0.000246),
 "HC-CHERRY-300":dict(price=640.0,  cogs=175.27, b=0.875, vol=0.833, o90=48,  ad=0.604, kg=0.394, mm=(70,63,165), uv=0.00075, kiz_relabel=True),
 "HC-AMBER-300": dict(price=640.0,  cogs=175.27, b=0.738, vol=0.833, o90=42,  ad=0.952, kg=0.394, mm=(70,63,165), uv=0.00075, kiz_relabel=True),
 "HC-HAND-300":  dict(price=640.0,  cogs=231.38, b=0.918, vol=0.833, o90=599, ad=0.579, kg=0.391, mm=(65,65,167), uv=0.00083),
}
SETS = {
 "SET-SER-CREAM-ACNE":     dict(price=1294.0, cogs=267.44, b=0.824, vol=0.544, o90=51, ad=0.353, comps=["FC-ACNE-50","FS-ACNE-30"]),
 "SET-TON-CREAM-ACNE":     dict(price=1274.0, cogs=314.45, b=0.833, vol=0.765, o90=18, ad=0.333, comps=["FC-ACNE-50","FT-ACNE-150"]),
 "SET-TON-SER-CREAM-ACNE": dict(price=1343.2, cogs=450.23, b=0.744, vol=1.105, o90=43, ad=0.744, comps=["FC-ACNE-50","FS-ACNE-30","FT-ACNE-150"]),
 "SET-4PC-ACNE":           dict(price=1900.0, cogs=609.52, b=0.731, vol=1.294, o90=52, ad=0.712, comps=["EP-ENZYME-75","FC-ACNE-50","FS-ACNE-30","FT-ACNE-150"]),
 "SET-ACNE-TONIC-SERUM":   dict(price=1280.0, cogs=318.57, b=0.90,  vol=0.72,  o90=11, ad=0.455, comps=["FS-ACNE-30","FT-ACNE-150"], b_note="ASSUMED 0.90 (11 orders)"),
 "SET-MOIST-TONIC-SERUM":  dict(price=1248.0, cogs=313.60, b=0.90,  vol=0.72,  o90=6,  ad=0.667, comps=["FS-MOIST-30","FT-MOIST-150"], b_note="ASSUMED 0.90 (6 orders)"),
 "SET-SER-CREAM-MOIST":    dict(price=1320.0, cogs=261.59, b=0.826, vol=0.544, o90=23, ad=0.130, comps=["FC-MOIST-50","FS-MOIST-30"]),
 "SET-TON-CREAM-MOIST":    dict(price=1267.2, cogs=313.57, b=0.885, vol=0.765, o90=26, ad=0.346, comps=["FC-MOIST-50","FT-MOIST-150"]),
 "SET-TON-SER-CREAM-MOIST":dict(price=1370.0, cogs=444.38, b=0.768, vol=1.105, o90=56, ad=0.375, comps=["FC-MOIST-50","FS-MOIST-30","FT-MOIST-150"]),
 "SET-CHERRY-AMBER":       dict(price=1120.0, cogs=350.54, b=0.952, vol=1.558, o90=21, ad=0.381, comps=["HC-AMBER-300","HC-CHERRY-300"]),
 "SET-HAND-CHERRY":        dict(price=1120.0, cogs=406.65, b=0.778, vol=1.563, o90=18, ad=0.278, comps=["HC-CHERRY-300","HC-HAND-300"]),
 "SET-HAND-AMBER":         dict(price=1120.0, cogs=406.65, b=0.800, vol=1.476, o90=20, ad=0.850, comps=["HC-AMBER-300","HC-HAND-300"]),
}
for s in SETS.values():
    s["kg"] = sum(SKU[c]["kg"] for c in s["comps"]); s["uv"] = 0.00058; s["k"] = len(s["comps"])
    s["kiz_relabel"] = False   # OBSERVED Aug-2026: no separate KIZ lines on Cherry/Amber inside sets (set KIZ is inside 22.90)

def usend_class(kg, mm):
    tot_cm = sum(mm)/10.0 if mm else None
    if kg <= 0.25 and tot_cm is not None and tot_cm < 29: return "MINI"
    if kg <= 4.9: return "STANDARD"
    return "KGT"
for k, s in SKU.items(): s["cls"] = usend_class(s["kg"], s["mm"])
for k, s in SETS.items(): s["cls"] = "STANDARD"   # CONTRACT: weight 0.30–0.79 kg > 0.25 kg

# ---------------- WB official parameters (validation pass, BASE) ----------------
def base_band(v):
    if v <= 0.2: return 23
    if v <= 0.4: return 26
    if v <= 0.6: return 29
    if v <= 0.8: return 30
    if v <= 1.0: return 32
    return 46 + 14*(v-1)
COEF = 1.70
COMM = {"FBW": 0.415+0.0075, "FBS13": 0.46-0.05+0.0075, "FBS": 0.46+0.0075}
ACQ = 0.0358                      # OBSERVED p50
WB_STORAGE_DAYS = 25              # ASSUMED (15–60)
TRANSFER_L = 5.0                  # OFFICIAL range 3–7, point ASSUMED
FBS_RETURN = lambda v: 25 + 4*max(0, math.ceil(v)-1)
WB_PVZ_FBS_FEE = 10.0             # OFFICIAL, if Usend hands FBS over at a WB PVZ (drop point UNKNOWN)

# ---------------- Usend parameters ----------------
U = dict(
 fbs_fee = {"MINI": (35.0, 7.0), "STANDARD": (55.0, 15.0)},          # CONTRACT sec.12 (first unit, additional unit)
 pack_obs = 4.5,                                                     # OBSERVED Jun–Aug 2026: "Комплектация в короба" 4.50 for every single incl. 300 ml
 pack_contract = {"MINI": 4.5, "STANDARD": 8.5},                     # CONTRACT sec.8
 barcode = 5.4, kiz = 6.5, kiz_csv = 6.5,                            # CONTRACT sec.8 (ШК, печать/наклейка КИЗ, список КИЗ CSV)
 set_obs = 22.90,                                                    # OBSERVED: 344 sets billed Aug-2026 at 22.90 flat, 2/3/4 components
 set_special = {2: 22.9, 3: 26.1, 4: 29.4},                          # SPECIAL: 6.5 + 3.2*(k-2) + ШК 5.4 + КИЗ 6.5 + раскладка 4.5
 set_contract = lambda k: 10.7 + 5.4*(k-2) + 5.4 + 6.5 + 8.5 + 5.25,  # CONTRACT STANDARD + скотч 5.25 (OBSERVED op Mar–May, absent from 2026 list)
 docs = 350.0, order = 27.0, box_mat = 76.7, loading = 55.0,        # CONTRACT/OBSERVED per supply / per box
 delivery_box = (470.0, 500.0),                                      # CONTRACT/OBSERVED: Обухово/Хоругвино 470, Коледино/Электросталь 500
 downtime_per_supply = 400.0,                                        # OBSERVED ≈ 10 350 ₽ over ~26 WB/Ozon supplies (range 0–4 950)
 shelf_rate = 105.0, shelf_days_fbw = 3, shelf_days_fbs = 10,        # CONTRACT 105 ₽/m3/day (40+ days); days ASSUMED
 ret_process = 45.0, ret_pickup = (400.0, 5),                        # CONTRACT: intake 45 ₽/unit + PVZ pickup 400 ₽ per trip (≤5 units). Per-unit 125 ₽ = BASE ASSUMPTION (5 units per trip), not a contract rate; sensitivity 64–205 ₽
 compliant_fbw = {"MINI": 7.5+7.5+5.4+7.1, "STANDARD": 10.7+10.5+8.6+10.5, "SET": 10.7+10.5},   # CONTRACT prices; composition ASSUMED to meet WB FBW beauty rule
 compliant_fbs = {"MINI": 7.5+7.5+5.4+2.3, "STANDARD": 10.7+10.5+7.5+3.7, "SET": 10.7+10.5+7.5+7.2},  # bubble + courier bag
)
# OBSERVED July-2026 WB supplies: ~168 cards in 2–3 boxes 400x400x400, ≈99 WB-litres per supply → allocate box & supply costs per WB-litre (method ASSUMED)
SUPPLY_L = 99.0; BOXES_PER_SUPPLY = 2.5
BOX_COST = U["box_mat"] + U["loading"] + sum(U["delivery_box"])/2
PER_L_OBS = (BOXES_PER_SUPPLY*BOX_COST + U["docs"] + U["order"] + U["downtime_per_supply"]) / SUPPLY_L

# ---------------- cost functions (₽ per bought-out unit) ----------------
def wb_cost(s, ch, pvz=False):
    v, b, p = s["vol"], s["b"], s["price"]
    fwd = base_band(v)*COEF
    if ch == "FBW":
        log = fwd/b + base_band(v)*(1-b)/b
        extra = 0.08*COEF*v*WB_STORAGE_DAYS + TRANSFER_L*v
        comm = COMM["FBW"]
    else:
        log = fwd/b + FBS_RETURN(v)*(1-b)/b
        extra = (WB_PVZ_FBS_FEE/b if pvz else 0.0)
        comm = COMM["FBS13"] if ch == "FBS13" else COMM["FBS"]
    return p*comm + p*ACQ + log + extra

def usend_fbw(s, case, is_set=False):
    parts = {}
    if is_set:
        parts["set_assembly"] = U["set_obs"] if case == "OBS" else max(U["set_obs"], U["set_special"][s["k"]])
    else:
        parts["pack"] = U["pack_obs"] if case == "OBS" else U["pack_contract"][s["cls"]]
        if s.get("kiz_relabel"): parts["kiz_relabel"] = U["kiz"]
    if case == "CONS":
        parts["kiz_csv"] = U["kiz_csv"]
        parts["wb_packaging"] = U["compliant_fbw"]["SET" if is_set else s["cls"]]
    parts["shelf"] = U["shelf_rate"]*s["uv"]*U["shelf_days_fbw"]
    parts["box_supply_alloc"] = PER_L_OBS*s["vol"]
    return sum(parts.values()), parts

def usend_fbs(s, case, is_set=False, mode="B"):
    b = s["b"]; parts = {}
    if is_set:
        if mode == "B":   # preassembled set, one STANDARD item per order; assembly done in advance (22.90 minus box раскладка 4.5)
            parts["fbs_fee"] = U["fbs_fee"]["STANDARD"][0]/b
            parts["set_assembly"] = (U["set_obs"]-4.5)
        else:             # C: on-demand; BASE = set treated as one STANDARD order + special op without раскладка
            parts["fbs_fee"] = U["fbs_fee"]["STANDARD"][0]/b
            parts["set_assembly"] = (U["set_obs"]-4.5)/b
    else:
        parts["fbs_fee"] = U["fbs_fee"][s["cls"]][0]/b
        if s.get("kiz_relabel"): parts["kiz_relabel"] = U["kiz"]/b
    ret_unit = U["ret_process"] + U["ret_pickup"][0]/U["ret_pickup"][1]   # BASE ASSUMPTION 125 ₽/unit (5 returns per pickup trip)
    parts["returns_to_ff"] = ret_unit*(1-b)/b
    parts["shelf"] = U["shelf_rate"]*s["uv"]*U["shelf_days_fbs"]
    if case == "CONS":
        parts["packaging"] = U["compliant_fbs"]["SET" if is_set else s["cls"]]/b
    return sum(parts.values()), parts

def econ(name, s, is_set=False):
    out = dict(sku=name, price=s["price"], cogs=s["cogs"], cls=s["cls"], b=s["b"])
    for case in ("OBS", "CONS"):
        uf, pf = usend_fbw(s, case, is_set)
        wf = wb_cost(s, "FBW")
        us, ps = usend_fbs(s, case, is_set, "B")
        ws = wb_cost(s, "FBS13", pvz=(case == "CONS"))
        ws_nod = wb_cost(s, "FBS", pvz=(case == "CONS"))
        cm_fbw = s["price"] - wf - s["cogs"] - uf
        cm_fbs = s["price"] - ws - s["cogs"] - us
        cm_fbs_nod = s["price"] - ws_nod - s["cogs"] - us
        out[case] = dict(usend_fbw=round(uf,1), wb_fbw=round(wf,1), net_rev_fbw=round(s["price"]-wf,1), cm_fbw=round(cm_fbw,1), cm_fbw_pct=round(100*cm_fbw/s["price"],1),
                         usend_fbs=round(us,1), wb_fbs=round(ws,1), net_rev_fbs=round(s["price"]-ws,1), cm_fbs=round(cm_fbs,1), cm_fbs_pct=round(100*cm_fbs/s["price"],1),
                         cm_fbs_no_discount=round(cm_fbs_nod,1),
                         premium=round(cm_fbw-cm_fbs,1), premium_pct_price=round(100*(cm_fbw-cm_fbs)/s["price"],1),
                         parts_fbw={k: round(v,2) for k,v in pf.items()}, parts_fbs={k: round(v,2) for k,v in ps.items()})
    return out

ROWS = [econ(k, s) for k, s in SKU.items()] + [econ(k, s, True) for k, s in SETS.items()]
print("== COST-TO-SERVE & CM per bought-out unit (BASE WB params) ==")
print("sku | cls | price | OBS: usendFBW wbFBW CM_FBW(%) | usendFBS wbFBS CM_FBS(%) | premium ₽ (% price) | CONS: CM_FBW CM_FBS premium | FBS no-discount OBS")
for r in ROWS:
    o, c = r["OBS"], r["CONS"]
    print(f'{r["sku"]} | {r["cls"]} | {r["price"]:.0f} | {o["usend_fbw"]} {o["wb_fbw"]} {o["cm_fbw"]}({o["cm_fbw_pct"]}) | {o["usend_fbs"]} {o["wb_fbs"]} {o["cm_fbs"]}({o["cm_fbs_pct"]}) | {o["premium"]} ({o["premium_pct_price"]}) | {c["cm_fbw"]} {c["cm_fbs"]} {c["premium"]} | {o["cm_fbs_no_discount"]}')
print(f"\nper-WB-litre box+supply allocation OBS = {PER_L_OBS:.2f} ₽/l")

# ---------------- set scenarios A / B / C (per set order, ₽, before WB & COGS where noted) ----------------
print("\n== SET SCENARIOS (Usend ₽ per set; A per shipped set, B/C per FBS order before /buyout) ==")
SETSCEN = []
for k, s in SETS.items():
    kk = s["k"]
    A_obs = U["set_obs"] + PER_L_OBS*s["vol"]
    A_spec = U["set_special"][kk] + PER_L_OBS*s["vol"]
    A_contract = U["set_contract"](kk) + PER_L_OBS*s["vol"]
    B = U["fbs_fee"]["STANDARD"][0] + (U["set_obs"]-4.5)
    C_low = U["fbs_fee"]["MINI"][0] + U["fbs_fee"]["MINI"][1]*(kk-1) + (U["set_obs"]-4.5)
    C_base = U["fbs_fee"]["STANDARD"][0] + (U["set_obs"]-4.5)
    C_high = U["fbs_fee"]["STANDARD"][0] + U["fbs_fee"]["STANDARD"][1]*(kk-1) + (10.7 + 5.4*(kk-2) + 5.4 + 6.5 + 5.25)
    row = dict(set=k, k=kk, A_obs=round(A_obs,1), A_special=round(A_spec,1), A_contract=round(A_contract,1), B=round(B,1), C_low=round(C_low,1), C_base=round(C_base,1), C_high=round(C_high,1))
    SETSCEN.append(row); print(row)

# ---------------- emergency FBS & stock-out sensitivity ----------------
print("\n== EMERGENCY FBS: premium and value of avoiding a 7-day stock-out (OBS case) ==")
G = 7; CAPTURE = (0.5, 0.7, 0.9); REC_DAYS = (0, 7, 14); REC_DROP = 0.5
EMERG = []
for r in ROWS:
    s = SKU.get(r["sku"]) or SETS[r["sku"]]
    mu = s["o90"]/90.0
    o = r["OBS"]
    lost_no_fbs = mu*G*o["cm_fbw"]
    rec = {R: mu*R*REC_DROP*o["cm_fbw"] for R in REC_DAYS}
    fbs_gain = {c: mu*G*c*o["cm_fbs"] for c in CAPTURE}
    # break-even leakage L* for a permanently published FBS reserve: monthly benefit / monthly leakage cost
    lstar = {}
    for p_so in (0.10, 0.25, 0.50):
        benefit = p_so*(fbs_gain[0.7] + 0.5*rec[7])
        cost_per_leak = mu*30*max(o["premium"], 0.01)
        lstar[p_so] = round(100*benefit/cost_per_leak, 1) if o["premium"] > 0 else None
    row = dict(sku=r["sku"], mu_day=round(mu,2), premium=o["premium"], premium_pct=o["premium_pct_price"], cm_fbs=o["cm_fbs"],
               lost_cm_7d_no_fbs=round(lost_no_fbs,0), recovery_loss={R: round(v,0) for R,v in rec.items()},
               fbs_gain_7d={c: round(v,0) for c,v in fbs_gain.items()}, breakeven_leakage_pct=lstar)
    EMERG.append(row); print(row)

# ---------------- inventory fragmentation: pooled components vs preassembled sets ----------------
print("\n== POOLING (safety stock over L=7 days, z=1.645, phi=1.5 ASSUMED; demand = 90-day orders, lower bound) ==")
Z, PHI, LREP = 1.645, 1.5, 7
POOL = []
for c, sc in SKU.items():
    users = [(k, s) for k, s in SETS.items() if c in s["comps"]]
    if not users: continue
    mu_c = sc["o90"]/90.0
    ss = lambda mu: Z*math.sqrt(PHI*mu*LREP) if mu > 0 else 0.0
    separate = ss(mu_c) + sum(ss(s["o90"]/90.0) for _, s in users)
    pooled = ss(mu_c + sum(s["o90"]/90.0 for _, s in users))
    slow = [(k, round(5/(s["o90"]/90.0))) for k, s in users if s["o90"]/90.0 < 0.25]
    row = dict(component=c, sets=len(users), ss_separate=round(separate,1), ss_pooled=round(pooled,1), units_freed=round(separate-pooled,1),
               wc_freed_rub=round((separate-pooled)*sc["cogs"]), slow_sets_days_cover_of_lot5=slow)
    POOL.append(row); print(row)

# ---------------- SKU x batch decision matrix ----------------
def d(x): return dt.date.fromisoformat(x)
ECON = {r["sku"]: r for r in ROWS}
BATCH = [
 # sku, batch, ff_qty, wb_live, exp_assumed, exp_max, fbw_status, note
 ("FS-MOIST-30","BATCH-06",3892,20,"2027-11-10","2028-01-19","COND","ниже целевого запаса"),
 ("FS-ACNE-30","BATCH-06",5223,197,"2027-11-10","2028-01-19","COND","выше целевого запаса"),
 ("FS-ACNE-30","BATCH-03?",None,0,"2026-12-17","2027-02-25","NO","наличие не подтверждено"),
 ("FC-ACNE-50","BATCH-04",1077,289,"2027-06-19","2027-08-28","NO",""),
 ("FC-MOIST-50","BATCH-04",2,0,"2027-06-19","2027-08-28","NO","2 шт."),
 ("FC-MOIST-50","BATCH-08",0,0,None,None,"UNKNOWN","в пути, ≈20.10; срок 24 или 36 мес"),
 ("FT-ACNE-150","BATCH-05",3676,47,"2027-10-22","2027-12-31","COND","немного ниже целевого"),
 ("FT-MOIST-150","BATCH-05",3667,57,"2027-10-22","2027-12-31","COND","выше целевого"),
 ("EP-ENZYME-75","BATCH-05",3737,101,"2027-10-22","2027-12-31","COND","выше целевого"),
 ("HC-CHERRY-300","BATCH-07",4429,0,"2028-01-15","2028-03-26","COND","на WB 0"),
 ("HC-AMBER-300","BATCH-07",4509,0,"2028-01-15","2028-03-26","COND","на WB 0"),
 ("HC-HAND-300","BATCH-02",2736,138,"2026-12-31","2026-12-31","NO","факт владельца"),
]
MATRIX = []
for sku, batch, ff, live, ea, em, fbw, note in BATCH:
    e = ECON[sku]["OBS"]
    if ea:
        ozon_last = d(ea) - dt.timedelta(days=90); ozon_last_max = d(em) - dt.timedelta(days=90)
        fbs_last = d(ea) - dt.timedelta(days=33); fbs_last_max = d(em) - dt.timedelta(days=33)
        ozon = "YES" if ozon_last > TODAY + dt.timedelta(days=14) else ("NO" if ozon_last_max <= TODAY else "UNKNOWN")
        fbs = "YES" if fbs_last > TODAY + dt.timedelta(days=14) else ("NO" if fbs_last_max <= TODAY else "UNKNOWN")
    else:
        ozon = fbs = "UNKNOWN"; ozon_last = fbs_last = None
    MATRIX.append(dict(sku=sku, batch=batch, ff=ff, wb_live=live, fbw=fbw, fbs=fbs, ozon=ozon,
                       ozon_last_delivery=str(ozon_last) if ozon_last else None, wb_fbs_last_order=str(fbs_last) if fbs_last else None,
                       cm_fbw=e["cm_fbw"], cm_fbs=e["cm_fbs"], premium=e["premium"], note=note))
print("\n== MATRIX (expiry windows: Ozon FBO ≥90 d at delivery; WB FBS expiry date ≥30 d + 3 d buffer) ==")
for m in MATRIX: print(m)

# ---------------- first test shipment: Usend cost ----------------
units = {"FS-MOIST-30": 140, "FT-ACNE-150": 60}
boxes = 2
fs = dict(pack=sum(units.values())*U["pack_obs"], docs=U["docs"], order=U["order"], box_material=boxes*U["box_mat"], loading=boxes*U["loading"],
          delivery=boxes*sum(U["delivery_box"])/2, downtime_expected=U["downtime_per_supply"])
fs_total = sum(fs.values()); n = sum(units.values())
cons_extra = dict(kiz_csv=n*U["kiz_csv"], wb_packaging=n*U["compliant_fbw"]["MINI"])
cogs_risk = sum(q*SKU[k]["cogs"] for k, q in units.items())
FIRST = dict(units=units, boxes=boxes, obs_parts=fs, obs_total=round(fs_total), obs_per_unit=round(fs_total/n,1),
             cons_extra=cons_extra, cons_total=round(fs_total+sum(cons_extra.values())), cons_per_unit=round((fs_total+sum(cons_extra.values()))/n,1),
             downtime_range=(0, 2*450*1), cogs_at_risk=round(cogs_risk), kg=round(sum(q*SKU[k]["kg"] for k,q in units.items()),1),
             wb_litres=round(sum(q*SKU[k]["vol"] for k,q in units.items()),1))
print("\n== FIRST SHIPMENT ==\n", FIRST)

json.dump(dict(generated=str(TODAY), params=dict(PER_L_OBS=round(PER_L_OBS,2), WB_STORAGE_DAYS=WB_STORAGE_DAYS, TRANSFER_L=TRANSFER_L, ACQ=ACQ),
               rows=ROWS, set_scenarios=SETSCEN, emergency=EMERG, pooling=POOL, matrix=MATRIX, first_shipment=FIRST),
          open(__file__.replace(".py", ".json"), "w"), ensure_ascii=False, indent=1, default=str)
