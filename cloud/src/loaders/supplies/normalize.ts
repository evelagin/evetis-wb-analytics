/**
 * M5: нормализация поставок WB — чистые функции, 1:1 с Apps Script Wbsuppliesrawloader.gs
 * (wbsInt_/wbsNum_/wbsStr_/wbsBool_/wbsId_/wbsEntityKey_/wbsHeaderRow_/wbsGoodsRow_ и
 * выбор объектов суточного прогона runWbSuppliesDaily). Схема строк = схема боевых таблиц.
 */
import { createHash } from 'node:crypto';
import { TZ_MSK, WB_SUPPLIES_OVERLAP_DAYS, WB_SUPPLIES_STATUS_DONE } from './constants.js';

type Obj = Record<string, unknown>;

export function toInt(v: unknown): number | null {
  if (v === null || v === undefined || v === '') return null;
  const n = Number(v);
  return Number.isNaN(n) ? null : Math.round(n);
}

export function toNum(v: unknown): number | null {
  if (v === null || v === undefined || v === '') return null;
  const n = Number(v);
  return Number.isNaN(n) ? null : n;
}

export function toStr(v: unknown): string | null {
  if (v === null || v === undefined) return null;
  const s = String(v);
  return s === '' ? null : s;
}

export const toBool = (v: unknown): boolean => v === true || v === 'true';

/** RFC3339 с офсетом WB BigQuery принимает как есть (wbsTs_). */
export const toTs = (v: unknown): string | null => toStr(v);

/** 🔴 Идентификатор: 0 и пустое → NULL (WB отдаёт отсутствующий preorderID как 0). */
export function toId(v: unknown): number | null {
  const n = toInt(v);
  return n === null || n === 0 ? null : n;
}

/** Ключ объекта: P:<preorder> приоритетнее S:<supply>; без обоих — null (объект пропускается). */
export function entityKey(o: Obj): string | null {
  const pre = toId(o.preorderID);
  const sup = toId(o.supplyID);
  if (pre !== null) return `P:${pre}`;
  if (sup !== null) return `S:${sup}`;
  return null;
}

export const isCompleted = (o: Obj): boolean => Number(o.statusID) === WB_SUPPLIES_STATUS_DONE && !!toStr(o.factDate);

/** Как Array.prototype.join в Apps Script: null/undefined → пустая строка. */
export function md5Join(parts: unknown[]): string {
  return createHash('md5').update(parts.join('|'), 'utf8').digest('hex');
}

export interface HeaderRow {
  snapshot_ts: string; run_id: string; entity_key: string;
  supply_id: number | null; preorder_id: number | null; status_id: number | null;
  create_dt: string | null; supply_dt: string | null; fact_dt: string | null; updated_dt: string | null;
  box_type_id: number | null; is_box_on_pallet: boolean;
  warehouse_id: number | null; warehouse_name: string | null;
  transit_warehouse_id: number | null; transit_warehouse_name: string | null;
  actual_warehouse_id: number | null; actual_warehouse_name: string | null;
  acceptance_cost: number | null; paid_acceptance_coefficient: number | null;
  storage_coef: number | null; delivery_coef: number | null;
  reject_reason: string | null; supplier_assign_name: string | null;
  card_quantity: number | null; card_accepted_quantity: number | null;
  card_ready_quantity: number | null; card_unloading_quantity: number | null;
  depersonalized_quantity: number | null; can_show_quantity: boolean | null;
  is_completed: boolean; card_read_ok: boolean; raw_json: string; row_hash: string;
}

export interface GoodsRow {
  snapshot_ts: string; run_id: string; entity_key: string; supply_id: number | null;
  barcode: string | null; vendor_code: string | null; nm_id: number | null;
  tech_size: string | null; color: string | null; tnved: string | null; need_kiz: boolean;
  supplier_box_amount: number | null; quantity: number | null; accepted_quantity: number | null;
  ready_for_sale_quantity: number | null; unloading_quantity: number | null;
  accepted_quantity_zero: boolean; acceptance_resolution: null; raw_json: string; row_hash: string;
}

export function headerRow(o: Obj, card: Obj | null, ek: string, runId: string, snapshotTs: string): HeaderRow {
  const c = card;
  const row: Omit<HeaderRow, 'row_hash'> = {
    snapshot_ts: snapshotTs, run_id: runId, entity_key: ek,
    supply_id: toId(o.supplyID), preorder_id: toId(o.preorderID), status_id: toInt(o.statusID),
    create_dt: toTs(o.createDate), supply_dt: toTs(o.supplyDate), fact_dt: toTs(o.factDate), updated_dt: toTs(o.updatedDate),
    box_type_id: toInt(o.boxTypeID), is_box_on_pallet: toBool(o.isBoxOnPallet),
    warehouse_id: c ? toInt(c.warehouseID) : null, warehouse_name: c ? toStr(c.warehouseName) : null,
    transit_warehouse_id: c ? toInt(c.transitWarehouseID) : null, transit_warehouse_name: c ? toStr(c.transitWarehouseName) : null,
    actual_warehouse_id: c ? toInt(c.actualWarehouseID) : null, actual_warehouse_name: c ? toStr(c.actualWarehouseName) : null,
    acceptance_cost: c ? toNum(c.acceptanceCost) : null,
    paid_acceptance_coefficient: c ? toNum(c.paidAcceptanceCoefficient) : null,
    storage_coef: c ? toNum(c.storageCoef) : null, delivery_coef: c ? toNum(c.deliveryCoef) : null,
    reject_reason: c ? toStr(c.rejectReason) : null, supplier_assign_name: c ? toStr(c.supplierAssignName) : null,
    card_quantity: c ? toInt(c.quantity) : null, card_accepted_quantity: c ? toInt(c.acceptedQuantity) : null,
    card_ready_quantity: c ? toInt(c.readyForSaleQuantity) : null, card_unloading_quantity: c ? toInt(c.unloadingQuantity) : null,
    depersonalized_quantity: c ? toInt(c.depersonalizedQuantity) : null, can_show_quantity: c ? toBool(c.canShowQuantity) : null,
    is_completed: isCompleted(o), card_read_ok: !!c,
    raw_json: JSON.stringify({ list: o, card: c || null }),
  };
  const row_hash = md5Join([
    row.entity_key, row.supply_id, row.preorder_id, row.status_id,
    row.create_dt, row.supply_dt, row.fact_dt, row.updated_dt,
    row.warehouse_id, row.transit_warehouse_id, row.actual_warehouse_id,
    row.card_quantity, row.card_accepted_quantity, row.card_ready_quantity,
    row.card_unloading_quantity, row.depersonalized_quantity,
  ]);
  return { ...row, row_hash };
}

export function goodsRow(g: Obj, o: Obj, ek: string, runId: string, snapshotTs: string): GoodsRow {
  const qty = toInt(g.quantity);
  const acc = toInt(g.acceptedQuantity);
  const row: Omit<GoodsRow, 'row_hash'> = {
    snapshot_ts: snapshotTs, run_id: runId, entity_key: ek, supply_id: toId(o.supplyID),
    barcode: toStr(g.barcode), vendor_code: toStr(g.vendorCode), nm_id: toInt(g.nmID),
    tech_size: toStr(g.techSize), color: toStr(g.color), tnved: toStr(g.tnved), need_kiz: toBool(g.needKiz),
    supplier_box_amount: toInt(g.supplierBoxAmount), quantity: qty, accepted_quantity: acc,
    ready_for_sale_quantity: toInt(g.readyForSaleQuantity), unloading_quantity: toInt(g.unloadingQuantity),
    // Наблюдение, не интерпретация: ноль НЕ означает «не принято» (§13.1).
    accepted_quantity_zero: acc === 0 && qty !== null && qty > 0,
    acceptance_resolution: null,
    raw_json: JSON.stringify(g),
  };
  const row_hash = md5Join([row.entity_key, row.barcode, row.nm_id, row.quantity,
    row.accepted_quantity, row.ready_for_sale_quantity, row.unloading_quantity]);
  return { ...row, row_hash };
}

/** Дата МСК yyyy-MM-dd момента `ms` (Utilities.formatDate(…, 'Europe/Moscow', 'yyyy-MM-dd')). */
export function mskDate(ms: number): string {
  return new Intl.DateTimeFormat('en-CA', { timeZone: TZ_MSK, year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date(ms));
}

/** run_id суточного прогона — формат Apps Script: DL_yyyyMMdd_HHmmss (МСК). */
export function dailyRunId(ms: number): string {
  const p = new Intl.DateTimeFormat('en-CA', {
    timeZone: TZ_MSK, year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  }).formatToParts(new Date(ms));
  const g = (t: string) => p.find((x) => x.type === t)?.value ?? '00';
  const hh = g('hour') === '24' ? '00' : g('hour');
  return `DL_${g('year')}${g('month')}${g('day')}_${hh}${g('minute')}${g('second')}`;
}

export type TodoReason = 'новый' | 'не завершён' | 'updated_dt новее' | 'окно перекрытия';
export interface TodoItem { o: Obj; ek: string; why: TodoReason }

export interface SelectResult {
  todo: TodoItem[];
  skippedNoKey: number;
  /** entity_key, встреченные в списке больше одного раза (оставлена последняя запись, как в Apps Script). */
  duplicateKeys: string[];
}

/**
 * Выбор объектов суточного прогона (§7.3): новые; незавершённые (statusID ≠ 5); с updatedDate
 * новее известного в RAW (сравнение моментов, epoch ms); завершённые с factDate в окне 14 суток.
 *
 * Дубль entity_key в списке: Apps Script писал объект дважды в одном run_id, и второй DELETE+LOAD
 * затирал первый — побеждала ПОСЛЕДНЯЯ запись списка. Здесь то же правило без двойной записи.
 */
export function selectTodo(list: Obj[], known: ReadonlyMap<string, number>, nowMs: number): SelectResult {
  const overlapDay = mskDate(nowMs - WB_SUPPLIES_OVERLAP_DAYS * 24 * 3600 * 1000);
  const byKey = new Map<string, TodoItem>();
  const seen = new Set<string>();
  const dups = new Set<string>();
  let skippedNoKey = 0;
  for (const o of list) {
    const ek = entityKey(o);
    if (!ek) { skippedNoKey++; continue; }
    if (seen.has(ek)) dups.add(ek);
    seen.add(ek);
    let why: TodoReason | '' = '';
    const apiUpdMs = Date.parse(String(o.updatedDate ?? ''));
    if (!known.has(ek)) why = 'новый';
    else if (Number(o.statusID) !== WB_SUPPLIES_STATUS_DONE) why = 'не завершён';
    else if (!Number.isNaN(apiUpdMs) && apiUpdMs > Number(known.get(ek) || 0)) why = 'updated_dt новее';
    else if (String(o.factDate ?? '').substring(0, 10) >= overlapDay) why = 'окно перекрытия';
    // Повторная запись того же ключа заменяет прежнюю — как DELETE+LOAD в Apps Script. Вхождение
    // без причины Apps Script не писал вовсе, поэтому оно прежнюю запись НЕ отменяет.
    if (why) {
      byKey.delete(ek);
      byKey.set(ek, { o, ek, why });
    }
  }
  const todo = [...byKey.values()];
  return { todo, skippedNoKey, duplicateKeys: [...dups] };
}

/** И10 (barcode уникален внутри объекта) — наблюдение до записи, как инвариант Apps Script. */
export function duplicateBarcodes(goods: GoodsRow[]): string[] {
  const seen = new Set<string>();
  const dup = new Set<string>();
  for (const g of goods) {
    const k = String(g.barcode);
    if (seen.has(k)) dup.add(k);
    seen.add(k);
  }
  return [...dup];
}
