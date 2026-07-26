/**
 * Чистое ядро загрузчика остатков (PR-Mig1): валидация T6, нормализация в
 * RAW-строки + метрики, контроли дубликатов/несопоставленных SKU.
 * Никаких сетевых/BQ-вызовов — детерминированно, тестируется replay-фикстурой.
 */
import { WB_STOCKS_AGG_WAREHOUSE } from './constants.js';

export interface RawStockRow {
  load_id: string;
  snapshot_id: string;
  snapshot_ts: string;
  source_api: string;
  nm_id: number;
  chrt_id: number;
  warehouse_id: number;
  warehouse_name: string;
  region_name: string;
  quantity: number;
  in_way_to_client: number;
  in_way_from_client: number;
  is_aggregate_warehouse: boolean;
  internal_sku: string;
  sku_match_status: string;
  raw_json: string;
  _snapshot_date: string;
}

export interface StockMetrics {
  expected_rows: number;
  unique_nm_ids: number;
  warehouses_count: number;
  qty_positive_rows: number;
  qty_zero_rows: number;
  aggregate_warehouse_rows: number;
  sum_quantity_all_t6: number;
  sum_quantity_physical_t6: number;
  distinct_keys: number;
  duplicate_keys: number;
  unmatched_nm_ids: string[];
}

export interface ValidationResult {
  ok: boolean;
  error?: string;
  distinctKeys?: number;
  duplicateKeys?: number;
}

type Rec = Record<string, unknown>;

function toInt(v: unknown): number | null {
  if (v === null || v === undefined || v === '') return null;
  const n = Number(v);
  if (!Number.isFinite(n) || Math.floor(n) !== n) return null;
  return n;
}

const nmOf = (o: Rec) => toInt(o.nmId ?? o.nmid ?? o.nm_id);
const chrtOf = (o: Rec) => toInt(o.chrtId ?? o.chrt_id);
const whOf = (o: Rec) => toInt(o.warehouseId ?? o.warehouse_id);
const qOf = (o: Rec) => toInt(o.quantity ?? o.qty);
const tOf = (o: Rec) => toInt(o.inWayToClient ?? o.in_way_to_client);
const fOf = (o: Rec) => toInt(o.inWayFromClient ?? o.in_way_from_client);

/** Извлечь массив строк из разных форм ответа T6. */
export function extractT6Array(json: unknown): unknown[] | null {
  if (Array.isArray(json)) return json;
  const j = json as { data?: unknown };
  const d = j?.data as { items?: unknown } | unknown[] | undefined;
  if (d && Array.isArray((d as { items?: unknown }).items)) return (d as { items: unknown[] }).items;
  if (Array.isArray(d)) return d;
  return null;
}

/**
 * Валидация всего пакета ДО записи. Пустой ответ для действующего кабинета = ERROR.
 * Проверяет типы и уникальность ключа nmId|chrtId|warehouseId (distinct==rows, dup==0).
 */
export function validateT6(data: unknown): ValidationResult {
  if (!Array.isArray(data)) return { ok: false, error: 'T6 ответ не массив' };
  if (data.length === 0) return { ok: false, error: 'T6 вернул 0 строк — для действующего кабинета трактуем как ERROR.' };
  const seen = new Set<string>();
  let dup = 0;
  for (let i = 0; i < data.length; i++) {
    const o = data[i] as Rec;
    const nm = nmOf(o), chrt = chrtOf(o), wh = whOf(o);
    if (nm === null || nm <= 0) return { ok: false, error: `Строка #${i + 1}: nmId не положительный INT64` };
    if (chrt === null) return { ok: false, error: `Строка #${i + 1}: chrtId не INT64` };
    if (wh === null || wh < 0) return { ok: false, error: `Строка #${i + 1}: warehouseId не INT64 ≥0` };
    const q = qOf(o), t = tOf(o), f = fOf(o);
    if (q === null || q < 0) return { ok: false, error: `Строка #${i + 1}: quantity не целое ≥0` };
    if (t === null || t < 0) return { ok: false, error: `Строка #${i + 1}: inWayToClient не целое ≥0` };
    if (f === null || f < 0) return { ok: false, error: `Строка #${i + 1}: inWayFromClient не целое ≥0` };
    const key = `${nm}|${chrt}|${wh}`;
    if (seen.has(key)) dup++;
    else seen.add(key);
  }
  if (dup > 0) return { ok: false, error: `T6: дубли ключа nmId|chrtId|warehouseId = ${dup} (ожидалось 0)` };
  return { ok: true, distinctKeys: seen.size, duplicateKeys: 0 };
}

export interface NormalizeCtx {
  snapshotId: string;
  snapshotTsIso: string;
  snapshotDate: string;
  loadId: string;
  sourceApi: string;
  skuByNm: Map<number, string>;
}

/** Нормализация T6 → RAW-строки + метрики. SKU-привязка по nmId (у T6 нет barcode). */
export function normalizeT6(data: unknown[], ctx: NormalizeCtx): { rows: RawStockRow[]; metrics: StockMetrics } {
  const rows: RawStockRow[] = [];
  const nmSet = new Set<number>();
  const whSet = new Set<string>();
  const unmatched = new Set<string>();
  const seen = new Set<string>();
  let qtyPos = 0, qtyZero = 0, agg = 0, sumAll = 0, sumPhys = 0, dup = 0;

  for (const oo of data) {
    const o = oo as Rec;
    const nm = nmOf(o) as number;
    const chrt = chrtOf(o) as number;
    const wh = whOf(o) as number;
    const whName = String(o.warehouseName ?? o.warehouse ?? '');
    const region = String(o.regionName ?? o.region ?? '');
    const q = qOf(o) ?? 0;
    const t = tOf(o) ?? 0;
    const f = fOf(o) ?? 0;
    const isAgg = wh === 0 || whName === WB_STOCKS_AGG_WAREHOUSE;
    const mapped = ctx.skuByNm.get(nm);
    const matched = mapped !== undefined;
    if (!matched) unmatched.add(String(nm));

    rows.push({
      load_id: ctx.loadId, snapshot_id: ctx.snapshotId, snapshot_ts: ctx.snapshotTsIso, source_api: ctx.sourceApi,
      nm_id: nm, chrt_id: chrt, warehouse_id: wh, warehouse_name: whName, region_name: region,
      quantity: q, in_way_to_client: t, in_way_from_client: f, is_aggregate_warehouse: isAgg,
      internal_sku: matched ? (mapped as string) : '', sku_match_status: matched ? 'matched' : 'not_found',
      raw_json: JSON.stringify(o), _snapshot_date: ctx.snapshotDate,
    });

    nmSet.add(nm);
    if (whName) whSet.add(whName);
    if (q > 0) qtyPos++; else qtyZero++;
    sumAll += q;
    if (!isAgg) sumPhys += q; else agg++;
    const key = `${nm}|${chrt}|${wh}`;
    if (seen.has(key)) dup++; else seen.add(key);
  }

  return {
    rows,
    metrics: {
      expected_rows: rows.length, unique_nm_ids: nmSet.size, warehouses_count: whSet.size,
      qty_positive_rows: qtyPos, qty_zero_rows: qtyZero, aggregate_warehouse_rows: agg,
      sum_quantity_all_t6: sumAll, sum_quantity_physical_t6: sumPhys,
      distinct_keys: seen.size, duplicate_keys: dup, unmatched_nm_ids: [...unmatched],
    },
  };
}
