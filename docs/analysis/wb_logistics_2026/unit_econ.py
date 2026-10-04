# Сравнительная юнит-экономика EVETIS: FBW через ПВЗ / FBW через СЦ / FBS (ФФ Usend, Москва)
# Источники параметров: справка WB «Доставка» 09.09.2026 (тарифные полосы и 170%), TG WB Партнёры 6658 (5 ₽/шт ПВЗ, 3–7 ₽/л перемещение),
# Tariffs API 14.09.2026 (комиссии), V_WB_SKU_FORWARD_ECONOMICS_CURRENT (цена, COGS, эквайринг), RAW_WB_PAID_STORAGE (объём, хранение).
import json
SKUS = [
 # sku, price(эфф. цена продавца), cogs, volume_l, buyout(1-отмены), orders/day(база сент, пред. стоки), contribution_before_ads_fwd (справочно)
 ("FS-MOIST-30 сыворотка УВЛ", 699.2, 130.81, 0.25, 0.92, 1.9),
 ("FS-ACNE-30 сыворотка АКНЕ", 805.6, 135.78, 0.16, 0.90, 4.2),
 ("FC-ACNE-50 крем АКНЕ", 799.6, 131.66, 0.272, 0.90, 3.5),
 ("FC-MOIST-50 крем УВЛ", 925.6, 130.78, 0.332, 0.93, 6.5),
 ("FT-ACNE-150 тоник АКНЕ", 740.0, 182.785, 0.40, 0.93, 3.2),
 ("FT-MOIST-150 тоник УВЛ", 740.0, 182.785, 0.40, 0.92, 1.5),
 ("EP-ENZYME-75 пудра", 666.0, 159.295, 0.35, 0.88, 1.9),
 ("HC-HAND-300 крем д/рук", 640.0, 231.38, 0.833, 0.92, 7.0),
 ("HC-CHERRY-300 крем д/тела", 640.0, 175.27, 0.833, 0.90, 1.4),
 ("SET-SER-CREAM-ACNE", 1294.0, 267.44, 0.544, 0.85, 1.7),
 ("SET-4PC-ACNE", 1900.0, 609.52, 1.294, 0.79, 0.8),
 ("SET-TON-SER-CREAM-MOIST", 1370.0, 444.375, 1.105, 0.80, 1.0),
 ("SET-HAND-CHERRY", 1120.0, 406.65, 1.563, 0.80, 0.4),
]
COEF = 1.7
def base_band(v):
    if v <= 0.2: return 23
    if v <= 0.4: return 26
    if v <= 0.6: return 29
    if v <= 0.8: return 30
    if v <= 1.0: return 32
    return 46 + 14*(v-1)
P = dict(
  comm_fbw=0.4225,        # paidStorageKgvp 41,5 + 0,75 надбавка (реализованная 42,25 %, MATCHED)
  comm_fbs_base=0.46,     # kgvpMarketplace «Красота» (API 14.09)
  comm_fbs_fast=0.41,     # −5 п.п. при отгрузке <13 ч (льгота до 31.01.2027)
  comm_fbs_mid=0.425,     # −3,5 п.п. при 13–42 ч
  acq=0.036,              # эквайринг p50
  storage_fbw_unit_month=1.5*COEF, # ₽/ед/мес факт 0,9–1,5 ₽ × 1,7 (новые поставки)
  storage_months=1.5,     # средний срок лежания при покрытии 45 дн
  pvz_handling=5.0,       # ₽/шт, ПВЗ (СЦ = 0)
  transfer_per_l=5.0,     # ₽/л, середина коридора 3–7 (с 30.09)
  ff_prep_fbw=12.0,       # ₽/ед: упаковка по оферте №101 + формирование поставки (рынок 10–20; Usend — уточнить)
  ff_delivery_lot=1500.0, # ₽ за рейс до ПВЗ/СЦ (допущение; Usend — уточнить)
  lot_units_pvz=180, lot_units_sc=800,
  ff_pick_fbs=50.0,       # ₽/заказ сборка+упаковка+передача (рынок 35–65; Usend — уточнить)
  fbs_return_ff=40.0,     # ₽ обработка возврата на ФФ (рынок 30–100)
  fbs_return_wb=25.0,     # ₽ возврат FBS в ПВЗ: 25 ₽ + 4 ₽/доп. л (с 07.08.2026)
  fbs_acceptance=0.0,     # приёмка FBS на СЦ бесплатна с 29.07 (ПВЗ: 10 ₽/шт первые 500/день)
)
def econ(sku, price, cogs, v, buy, dpd, scheme, fbs_comm=None):
    fwd = base_band(v)*COEF          # прямая логистика за отправление
    ret = base_band(v)               # обратная — по базовому тарифу без коэффициента (FBW)
    if scheme.startswith("FBW"):
        comm = P["comm_fbw"]*price
        log = fwd/buy + ret*(1-buy)/buy
        stor = P["storage_fbw_unit_month"]*P["storage_months"]
        handling = P["pvz_handling"] if scheme=="FBW_PVZ" else 0.0
        transfer = P["transfer_per_l"]*v
        lot = P["lot_units_pvz"] if scheme=="FBW_PVZ" else P["lot_units_sc"]
        ff = P["ff_prep_fbw"] + P["ff_delivery_lot"]/lot
        pick = 0.0; retff = 0.0
    else:
        comm = fbs_comm*price
        log = fwd/buy + (P["fbs_return_wb"]+4*max(0,v-1))*(1-buy)/buy
        stor = 0.105*v*30*P["storage_months"]  # Usend 105,42 ₽/м³/сут
        handling = P["fbs_acceptance"]; transfer = 0.0
        ff = P["ff_prep_fbw"]  # упаковка единицы всё равно нужна
        pick = P["ff_pick_fbs"]/buy
        retff = P["fbs_return_ff"]*(1-buy)/buy
    acq = P["acq"]*price
    total = comm+acq+log+stor+handling+transfer+ff+pick+retff
    contrib = price - cogs - total
    return dict(sku=sku, scheme=scheme, comm=round(comm,1), log=round(log,1), stor=round(stor,1), handling=round(handling,1),
                transfer=round(transfer,1), ff=round(ff+pick+retff,1), total_var=round(total,1), contrib=round(contrib,1), contrib_pct=round(100*contrib/price,1))
rows=[]
for s in SKUS:
    sku,price,cogs,v,buy,dpd = s
    rows.append(econ(sku,price,cogs,v,buy,dpd,"FBW_PVZ"))
    rows.append(econ(sku,price,cogs,v,buy,dpd,"FBW_SC"))
    rows.append(econ(sku,price,cogs,v,buy,dpd,"FBS_fast",P["comm_fbs_fast"]))
    rows.append(econ(sku,price,cogs,v,buy,dpd,"FBS_mid",P["comm_fbs_mid"]))
    rows.append(econ(sku,price,cogs,v,buy,dpd,"FBS_base",P["comm_fbs_base"]))
print(f"{'SKU':32} {'схема':9} {'комис':>6} {'логист':>6} {'хран':>5} {'ПВЗ':>4} {'перем':>5} {'ФФ':>6} {'перем.всего':>11} {'вклад ₽':>8} {'вклад %':>7}")
for r in rows:
    print(f"{r['sku']:32} {r['scheme']:9} {r['comm']:6} {r['log']:6} {r['stor']:5} {r['handling']:4} {r['transfer']:5} {r['ff']:6} {r['total_var']:11} {r['contrib']:8} {r['contrib_pct']:7}")
# Дельта FBW_PVZ − FBS_fast и точка безубыточности по объёму: на сколько % меньше продаж допустимо на FBS, чтобы абсолютный вклад сравнялся
print("\nДельта вклада на единицу и требуемое соотношение продаж")
for s in SKUS:
    sku,price,cogs,v,buy,dpd = s
    a=econ(sku,price,cogs,v,buy,dpd,"FBW_PVZ")["contrib"]; b=econ(sku,price,cogs,v,buy,dpd,"FBS_fast",P["comm_fbs_fast"])["contrib"]; c=econ(sku,price,cogs,v,buy,dpd,"FBS_mid",P["comm_fbs_mid"])["contrib"]
    print(f"{sku:32} FBW_PVZ {a:7.1f}  FBS<13ч {b:7.1f} (Δ {a-b:+6.1f})  FBS13-42ч {c:7.1f} (Δ {a-c:+6.1f})  FBS-объём для паритета vs FBW: {100*a/b if b>0 else float('nan'):5.0f}% / {100*a/c if c>0 else float('nan'):5.0f}%")
json.dump(rows, open('/private/tmp/claude-501/-Users-evgenelagin-Projects-evetis-wb-analytics/33630f35-a464-47d4-bbfd-115b3e5429a9/scratchpad/unit_econ.json','w'), ensure_ascii=False, indent=1)
