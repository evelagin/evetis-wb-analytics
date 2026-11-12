/**
 * M2 — заказы WB: нормализация, row_hash, план инкремента и форма строки BigQuery.
 * Чистый модуль. Эталон — Apps Script:
 *   normalizeOrdersApiRows_ / computeOrdersRowHash_ (WbOrdersLoader.gs:390-483);
 *   wbOrdersIncrementalCore_ (WbOrdersLoader.gs:852-973);
 *   appendOrdersRows_ + wbOrdersBqAppendRows_ (WbOrdersLoader.gs:517, WbOrdersBigQuery.gs:137).
 */
import { ORDERS_RAW_HEADERS, ORDERS_SOURCE_API } from './constants.js';
import { isValidWatermark, matchSku, md5Hex, normalizeDateKey, normalizeNmId, type SkuIndex } from './common.js';
import type { WbRow } from './wbApi.js';

/** Нормализованная строка в форме листа: имя колонки → значение ('' = пустая ячейка). */
export type OrdersSheetRow = Record<(typeof ORDERS_RAW_HEADERS)[number], unknown>;

/** ordersRawApiValue_: null/undefined/'' → '' (NULL в BQ), 0 остаётся 0. */
function rawApiValue(v: unknown): unknown {
  return v === null || v === undefined || v === '' ? '' : v;
}

const str = (v: unknown): string => (v === null || v === undefined ? '' : String(v).trim());

/** computeOrdersRowHash_: md5(srid | nm | order_day | is_cancel | sticker_id). */
export function computeOrdersRowHash(r: OrdersSheetRow): string {
  const parts = [
    str(r.srid), normalizeNmId(str(r.wb_nm_id)), normalizeDateKey(str(r.order_dt)), str(r.is_cancel), str(r.sticker_id),
  ];
  return md5Hex(parts.join('|'));
}

/**
 * normalizeOrdersApiRows_ в режиме инкремента (noWindow: без фильтра order_dt). Строка без даты
 * заказа пропускается — в инкременте её отсекает валидация ДО нормализации.
 */
export function normalizeOrdersRows(
  apiData: WbRow[], loadId: string, loadedAt: string, sku: SkuIndex,
): OrdersSheetRow[] {
  const rows: OrdersSheetRow[] = [];
  let rowNo = 0;
  for (const o of apiData) {
    const day = normalizeDateKey(o.date);
    if (!day) continue;
    rowNo++;
    const r = Object.fromEntries(ORDERS_RAW_HEADERS.map((h) => [h, ''])) as OrdersSheetRow;
    r.load_id = loadId;
    r.loaded_at = loadedAt;
    r.source_api = ORDERS_SOURCE_API;
    r.raw_row_number = rowNo;
    r.srid = o.srid || '';
    r.order_uid = o.gNumber || '';
    r.order_dt = o.date || '';
    r.cancel_dt = o.cancelDate || '';
    r.is_cancel = o.isCancel === true;
    r.last_change_date = String(o.lastChangeDate || '').replace('T', ' ');
    r.wb_nm_id = o.nmId || '';
    r.wb_vendor_code = o.supplierArticle || '';
    r.barcode = o.barcode || '';
    r.category = o.category || '';
    r.subject = o.subject || '';
    r.brand = o.brand || '';
    r.tech_size = o.techSize || '';
    r.warehouse_name = o.warehouseName || '';
    r.region_name = o.oblastOkrugName || '';
    r.country_name = o.countryName || '';
    r.price_with_disc = Number(o.priceWithDisc) || 0;
    r.total_price = rawApiValue(o.totalPrice);
    r.discount_percent = rawApiValue(o.discountPercent);
    r.spp = rawApiValue(o.spp);
    r.finished_price = rawApiValue(o.finishedPrice);
    r.quantity = 1;
    r.sticker_id = o.sticker || '';
    r.is_duplicate = 'FALSE';
    r.processed_status = 'OK';
    r.error_message = '';
    const m = matchSku(sku, o.nmId, o.barcode);
    if (m.matched) r.internal_sku = m.internalSku;
    r.sku_match_status = m.matched ? 'matched' : 'not_found';
    r.row_hash = computeOrdersRowHash(r);
    rows.push(r);
  }
  return rows;
}

/**
 * Строка для NDJSON-загрузки в RAW_WB_ORDERS (appendOrdersRows_ → wbOrdersBqAppendRows_):
 * пустые ''/null/undefined опускаются (NULL), прочее → STRING; `_order_date` — первые 10
 * символов order_dt, если это дата.
 */
export function ordersBqRow(r: OrdersSheetRow): Record<string, string> {
  const out: Record<string, string> = {};
  for (const h of ORDERS_RAW_HEADERS) {
    const v = r[h];
    if (v === '' || v === null || v === undefined) continue;
    out[h] = typeof v === 'string' ? v : String(v);
  }
  const od = String(r.order_dt || '').substring(0, 10);
  if (/^\d{4}-\d{2}-\d{2}$/.test(od)) out._order_date = od;
  return out;
}

// ─────────────────────────────────────────────────────────────────────────
// План инкремента (wbOrdersIncrementalCore_)
// ─────────────────────────────────────────────────────────────────────────

export type OrdersStatus = 'OK' | 'OK_NO_CHANGES' | 'PARTIAL' | 'ERROR';

export interface OrdersPlanInput {
  /** Watermark в API-форме (с 'T'). */
  watermark: string;
  fetchedOk: boolean;
  fetchedPartial: boolean;
  fetchError?: string;
  data: WbRow[];
  apiRowsReceived: number;
  cursorEnd: string;
  /** Пары srid|row_hash уже в RAW на ТОЧНОЙ границе last_change_date = watermark (форма хранения). */
  boundaryKeys: ReadonlySet<string>;
}

export interface OrdersPlan {
  status: OrdersStatus;
  error: string;
  /** Строки, которые надо дописать (только при status=OK). */
  toAppend: OrdersSheetRow[];
  watermarkBefore: string;
  watermarkCandidate: string;
  /** Watermark после успешной записи (= наибольший last_change_date в RAW). */
  watermarkAfter: string;
}

/**
 * Шаги до нормализации: статус fetch, кандидат, валидация КАЖДОЙ строки. Возвращает null, если
 * можно нормализовать, иначе итоговый план (ничего не пишем, watermark не двигаем).
 */
export function precheckOrders(i: Omit<OrdersPlanInput, 'boundaryKeys'>): OrdersPlan | null {
  const base = { toAppend: [], watermarkBefore: i.watermark, watermarkCandidate: i.cursorEnd, watermarkAfter: i.watermark };
  if (!i.fetchedOk) {
    return { ...base, status: i.fetchedPartial ? 'PARTIAL' : 'ERROR', error: i.fetchError || 'Незавершённая выгрузка' };
  }
  const candidate = i.cursorEnd;
  if (!candidate) {
    if (i.data.length === 0 && i.apiRowsReceived === 0) return { ...base, status: 'OK_NO_CHANGES', error: '' };
    return {
      ...base, status: 'ERROR',
      error: `Непустой ответ WB без cursor_end (data=${i.data.length}, api_rows=${i.apiRowsReceived}) — запись отменена, watermark не сдвинут.`,
    };
  }
  if (!isValidWatermark(candidate)) {
    return { ...base, status: 'ERROR', error: `Невалидный cursor_end от WB: ${candidate} — запись отменена, watermark не сдвинут.` };
  }
  if (candidate < i.watermark) {
    return { ...base, status: 'ERROR', error: `cursor_end (${candidate}) < watermark_before (${i.watermark}) — аномалия, запись отменена.` };
  }
  let bad = 0;
  for (const o of i.data) {
    const sridOk = !!o && String(o.srid || '').trim() !== '';
    const dateOk = !!o && !!normalizeDateKey(o.date);
    const lcdOk = !!o && isValidWatermark(String(o.lastChangeDate || ''));
    if (!sridOk || !dateOk || !lcdOk) bad++;
  }
  if (bad > 0) {
    return { ...base, status: 'ERROR', error: `Невалидных строк (srid/date/lastChangeDate): ${bad} — запись отменена, watermark не сдвинут.` };
  }
  return null;
}

/**
 * После нормализации: строки строго новее watermark — всегда; граничные (== watermark) — только
 * если пары srid|row_hash ещё нет в RAW; внутрипакетный дедуп по row_hash.
 */
export function selectOrdersToAppend(
  rows: OrdersSheetRow[], watermark: string, candidate: string, boundaryKeys: ReadonlySet<string>,
): OrdersPlan {
  const wmStorage = watermark.replace('T', ' ');
  const seen = new Set<string>();
  const toAppend: OrdersSheetRow[] = [];
  for (const r of rows) {
    const lcd = String(r.last_change_date || '');
    const srid = String(r.srid || '').trim();
    const rh = String(r.row_hash || '');
    let include = false;
    if (lcd > wmStorage) include = true;
    else if (lcd === wmStorage) include = !boundaryKeys.has(`${srid}|${rh}`);
    if (include && !seen.has(rh)) { seen.add(rh); toAppend.push(r); }
  }
  const base = { watermarkBefore: watermark, watermarkCandidate: candidate };
  if (toAppend.length === 0) return { ...base, status: 'OK_NO_CHANGES', error: '', toAppend: [], watermarkAfter: watermark };
  return { ...base, status: 'OK', error: '', toAppend, watermarkAfter: candidate > watermark ? candidate : watermark };
}

/** Контрольные суммы записанного пакета (aggregateOrdersRowArray_ без окна): для журнала. */
export function ordersBatchSums(rows: OrdersSheetRow[]): { uniqueSrid: number; cancelCount: number } {
  const srid = new Set<string>();
  let cancel = 0;
  for (const r of rows) {
    if (!normalizeDateKey(r.order_dt)) continue;
    if (r.is_cancel === true || String(r.is_cancel).toLowerCase() === 'true') cancel++;
    const s = String(r.srid || '').trim();
    if (s) srid.add(s);
  }
  return { uniqueSrid: srid.size, cancelCount: cancel };
}
