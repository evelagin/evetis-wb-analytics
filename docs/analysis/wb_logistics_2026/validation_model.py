# EVETIS WB logistics — Validation Pass model (14.09.2026). READ-ONLY: inputs copied from BigQuery SELECTs and official WB sources.
# Classes: OBS = observed EVETIS (BigQuery), OFF = official WB, ASM = assumption (range LOW/BASE/HIGH).
import math, json, datetime as dt

TODAY = dt.date(2026, 9, 14)

# ---------------- SKU inputs (OBS) ----------------
# price_now: V_WB_PRICES_CURRENT seller_effective_price 14.09; asp90: MART_SKU_DAILY buyouts_rub/buyouts 16.06-14.09;
# cogs: V_PRODUCT_COGS_EFFECTIVE; vol: RAW_WB_PAID_STORAGE.volume (WB-measured L); buyout: 1-canceled/orders 90d;
# log_obs: finance logistics per sale since 15.06 (old regime); mu/sd: daily orders on in-stock days; p95: daily / rolling 5/7/10d;
# live: FACT_STOCKS_SNAPSHOT 14.09; plan: CT_SEASON_PLAN_MONTHLY SET_2026-09-09 WB cards Oct/Nov/Dec/Jan/Feb.
S = {
 "FS-MOIST-30":   dict(seg="A", price_now=699.2,  asp90=722,  cogs=130.81,  vol=0.25,  buyout=0.934, log_obs=54.6,  mu=1.37, sd=1.40, p95d=4, p5=5,  p7=11, p10=16, live=20,  plan=[286.9,430.4,765.2,318.8,350.7]),
 "FS-ACNE-30":    dict(seg="A", price_now=805.6,  asp90=718,  cogs=135.78,  vol=0.16,  buyout=0.850, log_obs=53.6,  mu=1.38, sd=1.50, p95d=4, p5=9,  p7=15, p10=19, live=197, plan=[218.1,327.1,581.6,242.3,266.6]),
 "FC-ACNE-50":    dict(seg="A", price_now=799.6,  asp90=751,  cogs=131.66,  vol=0.272, buyout=0.807, log_obs=59.8,  mu=1.31, sd=1.48, p95d=4, p5=7,  p7=14, p10=19, live=289, plan=[66.4,99.6,177.1,73.8,81.2]),
 "FC-MOIST-50":   dict(seg="A", price_now=925.6,  asp90=841,  cogs=130.78,  vol=0.332, buyout=0.929, log_obs=56.0,  mu=2.02, sd=1.79, p95d=5, p5=8,  p7=14, p10=21, live=0,   plan=[0,351.0,624.0,260.0,286.0]),
 "SET-SER-CREAM-ACNE": dict(seg="A", price_now=1294, asp90=1236, cogs=267.44, vol=0.544, buyout=0.824, log_obs=60.8, mu=0.59, sd=0.85, p95d=2, p5=5, p7=7, p10=8, live=47, plan=[34.8,52.2,92.9,38.7,42.6]),
 "FT-ACNE-150":   dict(seg="B", price_now=740,    asp90=729,  cogs=182.785, vol=0.40,  buyout=0.928, log_obs=53.7,  mu=1.10, sd=1.19, p95d=3, p5=5,  p7=12, p10=15, live=47,  plan=[74.1,111.2,197.6,82.4,90.6]),
 "FT-MOIST-150":  dict(seg="B", price_now=740,    asp90=691,  cogs=182.785, vol=0.40,  buyout=0.923, log_obs=55.0,  mu=0.60, sd=0.99, p95d=2, p5=5,  p7=8,  p10=9,  live=57,  plan=[55.9,83.8,149.0,62.1,68.3]),
 "EP-ENZYME-75":  dict(seg="B", price_now=666,    asp90=713,  cogs=159.295, vol=0.35,  buyout=0.864, log_obs=56.5,  mu=0.72, sd=0.97, p95d=3, p5=4,  p7=9,  p10=12, live=101, plan=[46.6,69.9,124.2,51.8,56.9]),
 "SET-4PC-ACNE":  dict(seg="B", price_now=1900,   asp90=2372, cogs=609.52,  vol=1.294, buyout=0.731, log_obs=137.2, mu=0.75, sd=0.94, p95d=3, p5=4,  p7=7,  p10=8,  live=0,   plan=[38.1,57.1,101.5,42.3,46.5]),
 "SET-TON-SER-CREAM-ACNE": dict(seg="B", price_now=1343.2, asp90=1723, cogs=450.225, vol=1.105, buyout=0.744, log_obs=124.9, mu=0.56, sd=0.73, p95d=2, p5=4, p7=5, p10=7, live=0, plan=[14.1,21.2,37.6,15.7,17.2]),
 "HC-CHERRY-300": dict(seg="C", price_now=640,    asp90=649,  cogs=175.27,  vol=0.833, buyout=0.875, log_obs=68.9,  mu=0.64, sd=0.83, p95d=2, p5=3,  p7=6,  p10=8,  live=0,   plan=[25.1,37.7,67.0,27.9,30.7]),
 "HC-AMBER-300":  dict(seg="C", price_now=640,    asp90=700,  cogs=175.27,  vol=0.833, buyout=0.738, log_obs=79.2,  mu=0.50, sd=0.71, p95d=2, p5=2,  p7=6,  p10=8,  live=0,   plan=[25.1,37.7,67.0,27.9,30.7]),
 "SET-CHERRY-AMBER": dict(seg="C", price_now=1120, asp90=1007, cogs=350.54, vol=1.558, buyout=0.952, log_obs=103.2, mu=0.24, sd=0.53, p95d=1, p5=2, p7=4, p10=4, live=7, plan=[14.2,21.3,37.8,15.8,17.3]),
 "SET-HAND-CHERRY": dict(seg="C", price_now=1120, asp90=1092, cogs=406.65, vol=1.563, buyout=0.778, log_obs=125.7, mu=0.19, sd=0.52, p95d=2, p5=2, p7=3, p10=3, live=11, plan=[19.8,39.6,56.3,0,0]),
 "HC-HAND-300":   dict(seg="L", price_now=640,    asp90=471,  cogs=231.38,  vol=0.833, buyout=0.918, log_obs=71.0,  mu=6.76, sd=5.20, p95d=16, p5=26, p7=69, p10=95, live=138, plan=[330.0,640.0,955.2,0,0]),
}
MONTH_DAYS = [31,30,31,31,28]

# ---------------- Official WB parameters (OFF) ----------------
def base_band(v):            # справка «Доставка», ступени до 1 л; >1 л 46 + 14/л
    if v <= 0.2: return 23
    if v <= 0.4: return 26
    if v <= 0.6: return 29
    if v <= 0.8: return 30
    if v <= 1.0: return 32
    return 46 + 14*(v-1)
COEF = 1.70                   # единый коэффициент доставки/хранения РФ (FBS: Tariffs API «Свой склад РФ» 170 — OBS/OFF)
COMM_FBW = 0.415 + 0.0075     # Comission_RU.pdf 24.08.2026 (41,5) + опция конструктора +0,75 п.п. (OBS, REF_MARKETPLACE_COMMISSION_COMPONENT)
COMM_FBS = {"FBS<=13h": 0.46-0.05+0.0075, "FBS 13-18/42h": 0.46-0.035+0.0075, "FBS base": 0.46+0.0075}
PVZ_FEE = 5.0                 # оферта 13.2.2.1
FBS_RETURN = lambda v: 25 + 4*max(0.0, math.ceil(v)-1)   # возврат FBS в ПВЗ продавца, с 07.08.2026
STORAGE_L_DAY = 0.08*COEF     # 0,136 ₽/л/сутки

# ---------------- Assumption ranges (ASM) — order: HIGH-CM (favourable), BASE, LOW-CM (unfavourable) ----------------
A = {
 "acq":            (0.0362, 0.0388, 0.0400),   # OBS p50/p75/p90 эквайринга (V_WB_SKU_FORWARD_ECONOMICS_CURRENT)
 "wb_shelf_days":  (15, 25, 60),              # сколько дней единица лежит на WB до продажи
 "sc_accept_coef": (0.0, 0.0, 2.0),           # коэффициент приёмки в СЦ (сейчас 0 — проверить в ЛК)
 "transfer_l":     (3.0, 5.0, 7.0),           # ПВЗ/СЦ → склад после 29.09 (объявлен диапазон 3–7)
}
SC_KEYS = ["HIGH","BASE","LOW"]

def cm_components(k, s, idx, price_mode):
    price = s["price_now"] if price_mode=="now" else min(s["price_now"], s["asp90"])
    b, v = s["buyout"], s["vol"]
    fwd = base_band(v)*COEF
    log_fbw_off = fwd/b + base_band(v)*(1-b)/b
    log_fbs_off = fwd/b + FBS_RETURN(v)*(1-b)/b
    log_fbw = log_fbw_off if idx < 2 else max(log_fbw_off, s["log_obs"])
    log_fbs = log_fbs_off if idx < 2 else max(log_fbs_off, s["log_obs"] - (log_fbw_off-log_fbs_off))
    acq = A["acq"][idx]*price
    storage = STORAGE_L_DAY*v*A["wb_shelf_days"][idx]
    transfer = A["transfer_l"][idx]*v
    sc_accept = 1.7*v*A["sc_accept_coef"][idx]
    base = price - s["cogs"]
    fbw_sc  = base - COMM_FBW*price - acq - log_fbw - storage - transfer - sc_accept
    fbw_pvz = base - COMM_FBW*price - acq - log_fbw - storage - transfer - PVZ_FEE
    fbs = {m: base - c*price - acq - log_fbs for m, c in COMM_FBS.items()}
    return price, fbw_sc, fbw_pvz, fbs

rows = []
for k, s in S.items():
    for idx, sc in enumerate(SC_KEYS):
        pm = "now" if idx < 2 else "min"
        price, fbw_sc, fbw_pvz, fbs = cm_components(k, s, idx, pm)
        r = dict(sku=k, seg=s["seg"], scenario=sc, price=round(price), fbw_sc_preFF=round(fbw_sc,1), fbw_pvz_preFF=round(fbw_pvz,1))
        for m, val in fbs.items():
            r[m+"_preFF"] = round(val,1)
            # max FBS fulfilment fee per ORDER so that FBS CM == FBW-SC CM, FBW FF prep cost = 0 (strictest);
            # every ruble Usend charges for FBW box prep per unit raises this limit by the same ruble per unit.
            r["maxFF_"+m] = round((val - fbw_sc)*s["buyout"], 1)
        rows.append(r)

print("== UNIT ECONOMICS pre-FF, ₽ per bought-out unit ==")
hdr = ["sku","seg","scenario","price","fbw_sc_preFF","fbw_pvz_preFF","FBS<=13h_preFF","FBS 13-18/42h_preFF","FBS base_preFF","maxFF_FBS<=13h","maxFF_FBS 13-18/42h","maxFF_FBS base"]
print(" | ".join(hdr))
for r in rows:
    print(" | ".join(str(r[h]) for h in hdr))

# ---------------- Loss-risk break-even ----------------
print("\n== LOSS RISK: break-even monthly probability of a total-loss event of units in WB custody ==")
print("dCM = FBW_SC_preFF - FBS<=13h_preFF + F (FBS fulfilment fee per bought-out unit); exposure E = days a unit spends in WB custody")
E_DAYS = {"HIGH":15, "BASE":25, "LOW":45}
recov = [0.0, 0.25, 0.5, 1.0]
for k in ["FS-MOIST-30","FS-ACNE-30","FC-ACNE-50","FT-ACNE-150","SET-4PC-ACNE","HC-CHERRY-300"]:
    s = S[k]
    base = [r for r in rows if r["sku"]==k and r["scenario"]=="BASE"][0]
    for F in (0, 30, 50):
        dcm = base["fbw_sc_preFF"] - base["FBS<=13h_preFF"] + F/s["buyout"]
        out = []
        for rc in recov:
            if rc >= 1.0 or dcm <= 0:
                out.append("never" if rc>=1.0 else "FBS wins at p=0")
                continue
            lam = dcm / (E_DAYS["BASE"]*(1-rc)*s["cogs"])
            p_month = 1-(1-lam)**30
            out.append(f"{100*p_month:.1f}%/мес")
        print(k, f"F={F}", f"dCM={dcm:.1f}", out)

# portfolio expected loss: peak WB custody at 35-day cover of Dec demand
dec_units = sum(s["plan"][2]*(35/31) for s in S.values() if s["seg"]!="L")
dec_value = sum(s["plan"][2]*(35/31)*s["cogs"] for s in S.values() if s["seg"]!="L")
print(f"\nPeak WB custody (35d of Dec plan, excl. hand cream): units≈{dec_units:.0f}, COGS≈{dec_value/1e6:.2f} mln ₽")
for p in (0.05, 0.10, 0.25, 0.50):
    print(f" p(event, season)={p:.0%}: " + ", ".join(f"r={int(rc*100)}% → {p*(1-rc)*dec_value/1e3:.0f} тыс ₽" for rc in recov))

# ---------------- FBS safety stock ----------------
print("\n== FBS RESERVE (demand-based) ==")
Z = 1.645
def reserve(mu_f, phi, L):
    return math.ceil(mu_f*L + Z*math.sqrt(max(phi,1.0)*mu_f*L)) if mu_f > 0 else 0
print("sku | mu_obs | phi | fcst_oct/day | fcst_dec/day | P95 day | P95 5d/7d/10d obs | R5 oct | R7 oct | R10 oct | R5 dec | R7 dec | R10 dec")
res = {}
for k, s in S.items():
    phi = min(4.0, max(1.0, s["sd"]**2/s["mu"])) if s["mu"]>0 else 1.0
    f_oct = s["plan"][0]/31; f_dec = s["plan"][2]/31
    rr = [reserve(f_oct,phi,L) for L in (5,7,10)] + [reserve(f_dec,phi,L) for L in (5,7,10)]
    res[k] = dict(phi=round(phi,2), f_oct=round(f_oct,2), f_dec=round(f_dec,2), R=rr)
    print(k, "|", s["mu"], "|", round(phi,2), "|", round(f_oct,2), "|", round(f_dec,2), "|", s["p95d"], "|", f'{s["p5"]}/{s["p7"]}/{s["p10"]}', "|", " | ".join(map(str, rr)))

# ---------------- Expiry register (validated) ----------------
# FACT: shelf life 24 months — supplier Technical Specifications SP-TS-4…11, SP-ST-1 ("2 years to storage conditions", +5…+25 °C).
# FACT: import (customs declaration) dates — CT_EXPIRY_BATCH.import_date. Manufacture date is UNKNOWN for every batch except HC-HAND-300.
# BOUND: manufacture_date <= import_date  =>  expiry <= import_date + 24 months  (best case for the seller).
# ASSUMED: manufacture_date = import_date - 70 days (CT_EXPIRY_BATCH.expiry_source = INFERENCE_IMPORT_MINUS_70D).
# WB rule (справка «Этап 2», 20.08.2026): shelf life > 17 months -> remaining at acceptance >= 50 %.
print("\n== EXPIRY REGISTER (validated) ==")
HANDOVER = dt.date(2026, 9, 25)
ACC_DATE = dt.date(2026, 10, 9)          # handover + 10 working days (official acceptance ceiling)
BUFFER = dt.timedelta(days=14)
def add_months(d, m):
    y, mm = divmod(d.month - 1 + m, 12); return dt.date(d.year + y, mm + 1, min(d.day, 28))
batches = [
 # sku, batch, import_date FACT, mfg FACT or None, expiry FACT or None, ff_units OBS (V_CT_STOCK_BALANCE 14.09), note
 ("FS-ACNE-30",   "BATCH-06",            "2026-01-19", None, None, 5223, "на паллетах возможен остаток BATCH-03, количество UNKNOWN (FIFO-оценка ≤ ~270)"),
 ("FS-ACNE-30",   "BATCH-03 (остаток?)", "2025-02-25", None, None, None, "существование остатка не подтверждено"),
 ("FS-MOIST-30",  "BATCH-06",            "2026-01-19", None, None, 3892, ""),
 ("FC-ACNE-50",   "BATCH-04",            "2025-08-28", None, None, 1077, ""),
 ("FC-MOIST-50",  "BATCH-04",            "2025-08-28", None, None, 2, ""),
 ("FT-ACNE-150",  "BATCH-05",            "2025-12-31", None, None, 3676, ""),
 ("FT-MOIST-150", "BATCH-05",            "2025-12-31", None, None, 3667, ""),
 ("EP-ENZYME-75", "BATCH-05",            "2025-12-31", None, None, 3737, ""),
 ("HC-CHERRY-300","BATCH-07",            "2026-03-26", None, None, 4429, ""),
 ("HC-AMBER-300", "BATCH-07",            "2026-03-26", None, None, 4509, "ТС именно на Amber не найдена; 24 мес по ТС линейки"),
 ("HC-HAND-300",  "BATCH-02",            "2025-02-25", "2024-12-31", "2026-12-31", 2736, "OWNER_FACT_HARD"),
]
def eval_batch(imp, mfg_fact, exp_fact, acc):
    imp = dt.date.fromisoformat(imp)
    if exp_fact:
        mfg = dt.date.fromisoformat(mfg_fact); exp = dt.date.fromisoformat(exp_fact); tot = (exp - mfg).days
        r = (exp - acc).days / tot
        return dict(kind="FACT", exp_asm=exp, exp_max=exp, rem_asm=r, rem_max=r,
                    last_asm=exp - dt.timedelta(days=math.ceil(tot/2)) - BUFFER, last_max=exp - dt.timedelta(days=math.ceil(tot/2)) - BUFFER)
    out = {}
    for tag, mfg in (("asm", imp - dt.timedelta(days=70)), ("max", imp)):
        exp = add_months(mfg, 24); tot = (exp - mfg).days
        out["exp_"+tag] = exp; out["rem_"+tag] = (exp - acc).days / tot
        out["last_"+tag] = exp - dt.timedelta(days=math.ceil(tot/2)) - BUFFER
        out["mfg_"+tag] = mfg
    out["kind"] = "ASSUMED/BOUND"
    return out
def status(e):
    if e["rem_max"] < 0.5: return "НЕТ (доказано)"
    if e["rem_asm"] >= 0.5: return "УСЛОВНО (после фото даты)"
    return "UNKNOWN"
reg = []
for sku, batch, imp, mfg_f, exp_f, ff, note in batches:
    e = eval_batch(imp, mfg_f, exp_f, ACC_DATE)
    r = dict(sku=sku, batch=batch, import_date=imp, exp_assumed=str(e["exp_asm"]), exp_max=str(e["exp_max"]),
             rem_days_assumed=(e["exp_asm"] - TODAY).days, rem_days_max=(e["exp_max"] - TODAY).days,
             rem_pct_acc_assumed=round(100*e["rem_asm"]), rem_pct_acc_max=round(100*e["rem_max"]),
             fbw=status(e), last_ship_assumed=str(e["last_asm"]), last_ship_max=str(e["last_max"]),
             pass_if_printed_expiry_on_or_after=str(ACC_DATE + dt.timedelta(days=365)), ff=ff, kind=e["kind"], note=note)
    reg.append(r); print(r)
# sets inherit the worst component
BOM = {
 "SET-4PC-ACNE": ["EP-ENZYME-75","FC-ACNE-50","FS-ACNE-30","FT-ACNE-150"],
 "SET-ACNE-POWDER-SERUM-CREAM": ["EP-ENZYME-75","FC-ACNE-50","FS-ACNE-30"],
 "SET-ACNE-TONIC-SERUM": ["FS-ACNE-30","FT-ACNE-150"],
 "SET-MOIST-TONIC-SERUM": ["FS-MOIST-30","FT-MOIST-150"],
 "SET-SER-CREAM-ACNE": ["FC-ACNE-50","FS-ACNE-30"],
 "SET-SER-CREAM-MOIST": ["FC-MOIST-50","FS-MOIST-30"],
 "SET-TON-CREAM-ACNE": ["FC-ACNE-50","FT-ACNE-150"],
 "SET-TON-CREAM-MOIST": ["FC-MOIST-50","FT-MOIST-150"],
 "SET-TON-SER-CREAM-ACNE": ["FC-ACNE-50","FS-ACNE-30","FT-ACNE-150"],
 "SET-TON-SER-CREAM-MOIST": ["FC-MOIST-50","FS-MOIST-30","FT-MOIST-150"],
 "SET-CHERRY-AMBER": ["HC-AMBER-300","HC-CHERRY-300"],
 "SET-HAND-AMBER": ["HC-AMBER-300","HC-HAND-300"],
 "SET-HAND-CHERRY": ["HC-CHERRY-300","HC-HAND-300"],
}
first = {}
for r in reg:
    if r["sku"] not in first: first[r["sku"]] = r
rank = {"НЕТ (доказано)": 0, "UNKNOWN": 1, "УСЛОВНО (после фото даты)": 2}
sets = []
print("\n== SETS (worst component; BATCH-04 creams: FC-MOIST-50 has 2 units, new BATCH-08 expiry UNKNOWN) ==")
for s, comps in BOM.items():
    worst = min((first[c] for c in comps), key=lambda r: (rank[r["fbw"]], r["last_ship_assumed"]))
    row = dict(set=s, components=" + ".join(comps), fbw=worst["fbw"], limiting=worst["sku"], last_ship_assumed=worst["last_ship_assumed"])
    sets.append(row); print(row)

# ---------------- Dynamic replenishment for pilot candidates ----------------
# target_inventory = f*(LT+R) + z*sqrt(phi*f*(LT+R));  shipment = ceil_to_multiple(max(0, target - sellable - inbound - accepted_not_sellable))
print("\n== PILOT CANDIDATES: dynamic shipment (sellable = live 14.09, inbound = 0, accepted_not_sellable = 0) ==")
MULT = {"FS-MOIST-30":20, "FS-ACNE-30":20, "FT-ACNE-150":30, "FT-MOIST-150":30, "EP-ENZYME-75":20, "HC-CHERRY-300":20, "HC-AMBER-300":20}
LT_RANGE = (7, 14, 21); R = 7
rep = []
for k, m in MULT.items():
    s = S[k]; phi = min(4.0, max(1.0, s["sd"]**2/s["mu"]))
    for label, f in (("plan_oct", s["plan"][0]/31), ("observed", s["mu"])):
        vals = []
        for LT in LT_RANGE:
            h = LT + R
            tgt = f*h + Z*math.sqrt(phi*f*h)
            ship = max(0, tgt - s["live"])
            vals.append((round(tgt), int(math.ceil(ship/m)*m) if ship > 0 else 0))
        cover_obs = None if s["mu"] == 0 else round((s["live"] + vals[1][1]) / s["mu"])
        row = dict(sku=k, forecast=label, f_day=round(f,2), live=s["live"], mult=m,
                   target_LT7=vals[0][0], ship_LT7=vals[0][1], target_LT14=vals[1][0], ship_LT14=vals[1][1], target_LT21=vals[2][0], ship_LT21=vals[2][1],
                   cover_days_obs_after_LT14_ship=cover_obs)
        rep.append(row); print(row)

json.dump(dict(rows=rows, reserves=res, register=reg, sets=sets, replenishment=rep), open(__file__.replace(".py",".json"),"w"), ensure_ascii=False, indent=1, default=str)
