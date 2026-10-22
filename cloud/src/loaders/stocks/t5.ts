/**
 * PR-Mig1c — чистое ядро T5 `warehouse_remains`: нормализация детализации,
 * контрольные суммы, контроль T5↔T6 и детектор доступности.
 * Никаких сетевых/BQ-вызовов — детерминированно, тестируется replay-фикстурой.
 *
 * Эталон — боевой Apps Script, семантика 1:1 (parity, а не улучшения):
 *   wbStocksT5PhysicalSum_ / wbStocksNormalizeT5_ — WbStocksSnapshot.gs:161-246;
 *   контроль и детектор — wbStocksSnapshotCore_, WbStocksSnapshot.gs:440-511.
 */
import {
  WB_STOCKS_T5_AGG_NAME,
  WB_STOCKS_T5_PSEUDO,
  WB_STOCKS_CONTROL_TOLERANCE,
  WB_STOCKS_AVAIL_DROP_ABS,
  WB_STOCKS_AVAIL_DROP_REL,
  WB_STOCKS_AVAIL_OK_REL,
} from './constants.js';

/** Строка RAW_WB_STOCKS_T5. Грейн: snapshot × nmId × barcode × techSize × warehouseName. */
export interface RawStockT5Row {
  load_id: string;
  snapshot_id: string;
  snapshot_ts: string;
  source_api: string;
  nm_id: number | null;
  barcode: string;
  tech_size: string;
  vendor_code: string;
  volume: number;
  warehouse_name: string;
  row_type: string; // WAREHOUSE | AGGREGATE | PSEUDO_TOTAL | PSEUDO_TO_CLIENT | PSEUDO_FROM_CLIENT
  quantity: number;
  internal_sku: string;
  sku_match_status: string;
  _snapshot_date: string;
}

export interface T5NormalizeCtx {
  snapshotId: string;
  snapshotTsIso: string;
  snapshotDate: string;
  loadId: string;
  sourceApi: string;
  skuByNm: Map<number, string>;
}

/** Контрольные суммы T5 по реальным складам (псевдо-строки в сумму не идут). */
export interface T5Sums {
  sum: number;     // агрегат + поимённые склады
  aggSum: number;  // только «Склад WB РФ» — сопоставим с T6
  namedSum: number; // поимённые склады — их T6 с 16.08.2026 не отдаёт
}

/** Поля manifest, которые пишет контроль (имена = колонки WB_STOCKS_SNAPSHOTS). */
export interface StockControl {
  t5_control_sum: number | null;
  t5_wb_rf_sum: number | null;
  t5_named_sum: number | null;
  control_status: string; // OK | MISMATCH | T5_UNAVAILABLE
  control_delta: number | null;
  t6_comparable: number | null;
  availability_gap: number | null;
  availability_gap_prev: number | null;
  availability_gap_delta: number | null;
  availability_ratio: number | null;
  availability_status: string; // OK | DEGRADED | WAREHOUSE_DROP | DATA_ERROR | NO_BASELINE | T5_UNAVAILABLE
}

type Rec = Record<string, unknown>;

/** Строгое целое (иначе null) — зеркало wbStocksInt_ (WbStocksSnapshot.gs:61-66). */
function toInt(v: unknown): number | null {
  if (v === null || v === undefined || v === '') return null;
  const n = Number(v);
  if (!Number.isFinite(n) || Math.floor(n) !== n) return null;
  return n;
}

function warehousesOf(o: Rec): Rec[] {
  const w = o.warehouses;
  return Array.isArray(w) ? (w as Rec[]) : [];
}

/** Имя склада: `warehouseName || warehouse`, trim — как в Apps Script (|| , а не ??). */
function warehouseNameOf(w: Rec): string {
  return String(w.warehouseName || w.warehouse || '').trim();
}

export function t5RowType(warehouseName: string): string {
  return WB_STOCKS_T5_PSEUDO[warehouseName] ?? (warehouseName === WB_STOCKS_T5_AGG_NAME ? 'AGGREGATE' : 'WAREHOUSE');
}

/**
 * Тело download → массив товаров. Как в Apps Script: всё, что не массив, = пустой отчёт
 * (`Array.isArray(dl.json) ? dl.json : []`). Пустой отчёт НЕ роняет снимок: суммы станут
 * 0 и контроль честно покажет MISMATCH — это и есть сигнал.
 */
export function extractT5Array(json: unknown): unknown[] {
  return Array.isArray(json) ? json : [];
}

/** taskId из ответа create: data.taskId | data.id | taskId (WbStocksSnapshot.gs:167-170). */
export function extractT5TaskId(json: unknown): string {
  const j = (json ?? {}) as { data?: { taskId?: unknown; id?: unknown }; taskId?: unknown };
  const v = j.data?.taskId || j.data?.id || j.taskId;
  return v ? String(v) : '';
}

export type T5TaskState = { kind: 'ready' } | { kind: 'failed'; status: string } | { kind: 'pending' };

/** Статус задачи: data.status | status, без учёта регистра (WbStocksSnapshot.gs:177-184). */
export function t5TaskState(json: unknown): T5TaskState {
  const j = (json ?? {}) as { data?: { status?: unknown }; status?: unknown };
  const st = String(j.data?.status || j.status || '').toLowerCase();
  if (st === 'done' || st === 'ready' || st === 'completed' || st === 'success') return { kind: 'ready' };
  if (st === 'purged' || st === 'canceled' || st === 'cancelled' || st === 'failed' || st === 'error') {
    return { kind: 'failed', status: st };
  }
  return { kind: 'pending' };
}

/**
 * Суммы физических остатков T5. В сумму идут реальные склады (агрегат + поимённые),
 * псевдо-строки — нет. `Number(q || 0)` и `Math.round` — ровно как в Apps Script:
 * дробное quantity в сумму попадает, а в RAW-строку — нет (там строгое целое).
 */
export function sumT5(data: unknown[]): T5Sums {
  let sum = 0, aggSum = 0, namedSum = 0;
  for (const oo of data) {
    for (const w of warehousesOf(oo as Rec)) {
      const name = warehouseNameOf(w);
      if (WB_STOCKS_T5_PSEUDO[name]) continue;
      const q = Number(w.quantity || 0);
      sum += q;
      if (name === WB_STOCKS_T5_AGG_NAME) aggSum += q;
      else namedSum += q;
    }
  }
  return { sum: Math.round(sum), aggSum: Math.round(aggSum), namedSum: Math.round(namedSum) };
}

/** Нормализация T5 → строки RAW_WB_STOCKS_T5 (одна строка на товар × склад). */
export function normalizeT5(data: unknown[], ctx: T5NormalizeCtx): RawStockT5Row[] {
  const rows: RawStockT5Row[] = [];
  for (const oo of data) {
    const o = oo as Rec;
    const nm = toInt(o.nmId !== undefined ? o.nmId : o.nm_id);
    const mapped = nm === null ? undefined : ctx.skuByNm.get(nm);
    const matched = mapped !== undefined;
    for (const w of warehousesOf(o)) {
      const name = warehouseNameOf(w);
      rows.push({
        load_id: ctx.loadId, snapshot_id: ctx.snapshotId, snapshot_ts: ctx.snapshotTsIso,
        source_api: ctx.sourceApi,
        nm_id: nm, barcode: String(o.barcode || ''), tech_size: String(o.techSize || ''),
        vendor_code: String(o.vendorCode || ''), volume: Number(o.volume || 0),
        warehouse_name: name, row_type: t5RowType(name),
        quantity: toInt(w.quantity) || 0,
        internal_sku: matched ? (mapped as string) : '', sku_match_status: matched ? 'matched' : 'not_found',
        _snapshot_date: ctx.snapshotDate,
      });
    }
  }
  return rows;
}

/** Контроль, когда T5 недоступен: t6_comparable сохраняется — это независимая метрика T6. */
export function t5UnavailableControl(t6Comparable: number): StockControl {
  return {
    t5_control_sum: null, t5_wb_rf_sum: null, t5_named_sum: null,
    control_status: 'T5_UNAVAILABLE', control_delta: null, t6_comparable: t6Comparable,
    availability_gap: null, availability_gap_prev: null, availability_gap_delta: null,
    availability_ratio: null, availability_status: 'T5_UNAVAILABLE',
  };
}

/**
 * Контроль T5↔T6 + детектор доступности (WbStocksSnapshot.gs:453-492).
 *
 * T6 сопоставляется с АГРЕГАТОМ T5, а не с общим итогом: с 16.08.2026 поимённые склады
 * живут только в T5. Потоки in_way_* в инвариант не входят (гипотеза опровергнута 16.08).
 * control_* — историческая метрика (OK/MISMATCH по допуску), availability_* — отдельный
 * детектор: DATA_ERROR (T6 больше баланса), NO_BASELINE, WAREHOUSE_DROP, DEGRADED, OK.
 *
 * @param prevGap availability_gap предыдущего COMPLETE-снимка с непустым gap; null = базы нет.
 */
export function evaluateT5Control(t5: T5Sums, t6Comparable: number, prevGap: number | null): StockControl {
  const controlDelta = Math.abs(t5.aggSum - t6Comparable);
  const gap = t5.aggSum - t6Comparable;
  const ratio = t5.aggSum > 0 ? t6Comparable / t5.aggSum : null;
  const gapDelta = prevGap === null ? null : gap - prevGap;

  const dropThreshold = Math.max(WB_STOCKS_AVAIL_DROP_ABS, Math.round(t5.aggSum * WB_STOCKS_AVAIL_DROP_REL));
  const okThreshold = Math.max(WB_STOCKS_CONTROL_TOLERANCE, Math.round(t5.aggSum * WB_STOCKS_AVAIL_OK_REL));

  let availability: string;
  if (t6Comparable > t5.aggSum + WB_STOCKS_CONTROL_TOLERANCE) availability = 'DATA_ERROR';
  else if (gapDelta === null) availability = 'NO_BASELINE';
  else if (gapDelta >= dropThreshold) availability = 'WAREHOUSE_DROP';
  else if (gap > okThreshold) availability = 'DEGRADED';
  else availability = 'OK';

  return {
    t5_control_sum: t5.sum, t5_wb_rf_sum: t5.aggSum, t5_named_sum: t5.namedSum,
    control_status: controlDelta <= WB_STOCKS_CONTROL_TOLERANCE ? 'OK' : 'MISMATCH',
    control_delta: controlDelta, t6_comparable: t6Comparable,
    availability_gap: gap, availability_gap_prev: prevGap, availability_gap_delta: gapDelta,
    availability_ratio: ratio, availability_status: availability,
  };
}
