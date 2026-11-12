/**
 * M2 — продажи/возвраты WB: нормализация, row_hash, планы инкремента и ночной сверки, форма строки BQ.
 * Чистый модуль. Эталон — Apps Script:
 *   normalizeSalesApiRows_ / computeSalesRowHash_ (WbSalesReturnsLoader.gs.gs:354-470);
 *   wbSalesIncrementalCore_ (WbSalesIncremental.gs:207-336);
 *   wbSalesNightReconcileCore_ (WbSalesReconcile.gs:224-327);
 *   wbSalesBqAppendRows_ (WbSalesReturnsBigQuery.gs:211).
 */
import {
  SALES_BOOL_FIELDS, SALES_INT_FIELDS, SALES_NUMERIC_FIELDS, SALES_RAW_HEADERS, SALES_SOURCE_API,
} from './constants.js';
import { isValidWatermark, matchSku, md5Hex, normalizeDateKey, normalizeNmId, type SkuIndex } from './common.js';
import type { WbRow } from './wbApi.js';

export type SalesSheetRow = Record<(typeof SALES_RAW_HEADERS)[number], unknown>;

/** salesRound_ → roundTwo_: Math.round((Number(v)||0)*100)/100. */
function salesRound(v: unknown): number {
  const n = Number(v) || 0;
  return Math.round((n || 0) * 100) / 100;
}

const str = (v: unknown): string => (v === null || v === undefined ? '' : String(v).trim());

/** computeSalesRowHash_: md5(srid | nm | sale_day | sale_id | operation_type). */
export function computeSalesRowHash(r: SalesSheetRow): string {
  const parts = [str(r.srid), normalizeNmId(str(r.wb_nm_id)), normalizeDateKey(str(r.sale_dt)), str(r.sale_id), str(r.operation_type)];
  return md5Hex(parts.join('|'));
}

/**
 * normalizeSalesApiRows_ (noWindow). Возврат — по контракту WB: saleID начинается с 'R'.
 * raw_json = JSON.stringify(строки API) — тот же V8, что у Apps Script, поэтому порядок ключей
 * и форма чисел совпадают, и md5(raw_json) — общий ключ состояния обеих сторон.
 */
export function normalizeSalesRows(apiData: WbRow[], loadId: string, loadedAt: string, sku: SkuIndex): SalesSheetRow[] {
  const rows: SalesSheetRow[] = [];
  let rowNo = 0;
  for (const o of apiData) {
    const day = normalizeDateKey(o.date);
    if (!day) continue;
    const saleId = String(o.saleID || '');
    const isReturn = saleId.charAt(0).toUpperCase() === 'R';
    rowNo++;
    const r = Object.fromEntries(SALES_RAW_HEADERS.map((h) => [h, ''])) as SalesSheetRow;
    r.load_id = loadId;
    r.loaded_at = loadedAt;
    r.source_api = SALES_SOURCE_API;
    r.raw_row_number = rowNo;
    r.sale_id = saleId;
    r.srid = o.srid || '';
    r.g_number = o.gNumber || '';
    r.income_id = o.incomeID || '';
    r.sale_dt = o.date || '';
    r.last_change_date = o.lastChangeDate || '';
    r.operation_type = isReturn ? 'RETURN' : 'SALE';
    r.is_return = isReturn;
    r.wb_nm_id = o.nmId || '';
    r.wb_vendor_code = o.supplierArticle || '';
    r.barcode = o.barcode || '';
    r.category = o.category || '';
    r.subject = o.subject || '';
    r.brand = o.brand || '';
    r.tech_size = o.techSize || '';
    r.warehouse_name = o.warehouseName || '';
    r.warehouse_type = o.warehouseType || '';
    r.region_name = o.regionName || '';
    r.oblast_okrug_name = o.oblastOkrugName || '';
    r.country_name = o.countryName || '';
    r.total_price = salesRound(o.totalPrice);
    r.discount_percent = Number(o.discountPercent) || 0;
    r.spp = Number(o.spp) || 0;
    r.payment_sale_amount = salesRound(o.paymentSaleAmount);
    r.price_with_disc = salesRound(o.priceWithDisc);
    r.finished_price = salesRound(o.finishedPrice);
    r.for_pay = salesRound(o.forPay);
    r.is_supply = o.isSupply === true;
    r.is_realization = o.isRealization === true;
    r.sticker_id = o.sticker || '';
    r.processed_status = saleId ? 'OK' : 'MISSING_EVENT_KEY';
    r.error_message = '';
    r.raw_json = JSON.stringify(o);
    const m = matchSku(sku, o.nmId, o.barcode);
    if (m.matched) r.internal_sku = m.internalSku;
    r.sku_match_status = m.matched ? 'matched' : 'not_found';
    r.row_hash = computeSalesRowHash(r);
    rows.push(r);
  }
  return rows;
}

/** wbSalesBqAppendRows_: приведение к типу колонки, пустые опускаются, `_sale_date` из sale_dt. */
export function salesBqRow(r: SalesSheetRow): Record<string, string | number | boolean> {
  const out: Record<string, string | number | boolean> = {};
  for (const h of SALES_RAW_HEADERS) {
    const v = r[h];
    if (v === '' || v === null || v === undefined) continue;
    if (SALES_INT_FIELDS.has(h)) {
      const iv = parseInt(String(v).replace(/\s/g, ''), 10);
      if (!Number.isNaN(iv)) out[h] = iv;
    } else if (SALES_NUMERIC_FIELDS.has(h)) {
      const nv = parseFloat(String(v).replace(/\s/g, '').replace(',', '.'));
      if (!Number.isNaN(nv)) out[h] = nv;
    } else if (SALES_BOOL_FIELDS.has(h)) {
      out[h] = v === true || String(v).toLowerCase() === 'true';
    } else {
      out[h] = typeof v === 'string' ? v : String(v);
    }
  }
  const sd = String(r.sale_dt || '').substring(0, 10);
  if (/^\d{4}-\d{2}-\d{2}$/.test(sd)) out._sale_date = sd;
  return out;
}

/** Ключ состояния sale_id|md5(raw_json) — общий для границы инкремента и ночной сверки. */
export function salesStateKey(r: SalesSheetRow): string {
  return `${String(r.sale_id || '').trim()}|${md5Hex(String(r.raw_json || ''))}`;
}

// ─────────────────────────────────────────────────────────────────────────
// Общая валидация сырых строк (одинакова в инкременте и сверке)
// ─────────────────────────────────────────────────────────────────────────

/** null — все строки валидны; иначе 1-based номер первой плохой строки. Плюс max(lastChangeDate). */
export function validateSalesRaw(data: WbRow[]): { badRow: number | null; candidate: string } {
  let candidate = '';
  for (let i = 0; i < data.length; i++) {
    const o = data[i];
    const sid = String((o && o.saleID) || '').trim();
    const day = normalizeDateKey(o && o.date);
    const lcd = String((o && o.lastChangeDate) || '');
    if (sid === '' || !day || !isValidWatermark(lcd)) return { badRow: i + 1, candidate };
    if (!candidate || lcd > candidate) candidate = lcd;
  }
  return { badRow: null, candidate };
}

// ─────────────────────────────────────────────────────────────────────────
// Инкремент (wbSalesIncrementalCore_)
// ─────────────────────────────────────────────────────────────────────────

export type SalesStatus = 'OK' | 'OK_NO_CHANGES' | 'ERROR';

export interface SalesPlan {
  status: SalesStatus;
  error: string;
  toAppend: SalesSheetRow[];
  watermarkBefore: string;
  watermarkCandidate: string;
  watermarkAfter: string;
  rowsAfterBoundaryDedup: number;
}

/** До нормализации: статус fetch, пустой ответ, валидация, кандидат. `plan` — итог без записи. */
export function precheckSales(
  watermark: string, fetched: { ok: boolean; error?: string; data: WbRow[]; apiRowsReceived: number },
): { plan: SalesPlan } | { candidate: string } {
  const base = { toAppend: [], watermarkBefore: watermark, watermarkCandidate: '', watermarkAfter: watermark, rowsAfterBoundaryDedup: 0 };
  const stop = (status: SalesStatus, error: string, candidate = ''): { plan: SalesPlan } =>
    ({ plan: { ...base, watermarkCandidate: candidate, status, error } });
  if (!fetched.ok) return stop('ERROR', fetched.error || 'Незавершённая выгрузка Sales API');
  if (fetched.data.length === 0 && fetched.apiRowsReceived === 0) return stop('OK_NO_CHANGES', '');
  const v = validateSalesRaw(fetched.data);
  if (v.badRow !== null) {
    return stop('ERROR', `Невалидная сырая API-строка #${v.badRow} (saleID/date/lastChangeDate) — пакет отклонён до записи, watermark не сдвинут.`);
  }
  if (!isValidWatermark(v.candidate)) return stop('ERROR', 'Не удалось вычислить валидный candidate из пакета — запись отменена.');
  if (v.candidate < watermark) {
    return stop('ERROR', `candidate (${v.candidate}) < watermark_before (${watermark}) — аномалия, запись отменена.`, v.candidate);
  }
  return { candidate: v.candidate };
}

/**
 * После нормализации: внутрипакетный last-wins по sale_id (max lastChangeDate, tie-break — больший
 * state-hash), затем граница: строго новее watermark — всегда; == watermark — только если такого
 * состояния sale_id|md5(raw_json) ещё нет в RAW.
 */
export function selectSalesToAppend(
  rows: SalesSheetRow[], dataLen: number, watermark: string, candidate: string, boundaryKeys: ReadonlySet<string>,
): SalesPlan {
  const base = { watermarkBefore: watermark, watermarkCandidate: candidate, watermarkAfter: watermark };
  if (rows.length !== dataLen) {
    return {
      ...base, status: 'ERROR', toAppend: [], rowsAfterBoundaryDedup: 0,
      error: `Нормализатор отбросил строки (${dataLen}→${rows.length}) — пакет отклонён, watermark не сдвинут.`,
    };
  }
  const bySale = new Map<string, { row: SalesSheetRow; lcd: string; sh: string; sid: string }>();
  for (const row of rows) {
    const sid = String(row.sale_id || '').trim();
    const lcd = String(row.last_change_date || '');
    const sh = md5Hex(String(row.raw_json || ''));
    const cur = bySale.get(sid);
    if (!cur || lcd > cur.lcd || (lcd === cur.lcd && sh > cur.sh)) bySale.set(sid, { row, lcd, sh, sid });
  }
  const toAppend: SalesSheetRow[] = [];
  for (const w of bySale.values()) {
    let include = false;
    if (w.lcd > watermark) include = true;
    else if (w.lcd === watermark) include = !boundaryKeys.has(`${w.sid}|${w.sh}`);
    if (include) toAppend.push(w.row);
  }
  if (toAppend.length === 0) return { ...base, status: 'OK_NO_CHANGES', error: '', toAppend: [], rowsAfterBoundaryDedup: 0 };
  return {
    ...base, status: 'OK', error: '', toAppend, rowsAfterBoundaryDedup: toAppend.length,
    watermarkAfter: candidate > watermark ? candidate : watermark,
  };
}

// ─────────────────────────────────────────────────────────────────────────
// Ночная сверка (wbSalesNightReconcileCore_)
// ─────────────────────────────────────────────────────────────────────────

/**
 * dateFrom окна = полночь МСК (сегодня − N дней) — wbSalesReconcileFromLcd_. МСК без перехода на
 * летнее время, поэтому сдвиг на N·86 400 000 мс и перевод в дату МСК корректны.
 */
export function reconcileFromLcd(days: number, nowMs: number, mskDate: (d: Date) => string): string {
  return `${mskDate(new Date(nowMs - days * 86_400_000))}T00:00:00`;
}

export type ReconcileStatus = 'OK' | 'OK_NO_GAPS' | 'ERROR';

export interface ReconcilePlan {
  status: ReconcileStatus;
  error: string;
  toAppend: SalesSheetRow[];
  /** Различных состояний в пакете после внутрипакетного дедупа (rows_after_boundary_dedup в логе). */
  statesInBatch: number;
  gapsFilled: number;
}

/**
 * Внутрипакетный дедуп по sale_id|state (НЕ last-wins: латаются все отсутствующие состояния),
 * затем — только состояния, которых нет в RAW за окно (last_change_date >= fromLcd).
 */
export function selectReconcileGaps(rows: SalesSheetRow[], dataLen: number, existing: ReadonlySet<string>): ReconcilePlan {
  if (rows.length !== dataLen) {
    return {
      status: 'ERROR', toAppend: [], statesInBatch: 0, gapsFilled: 0,
      error: `Нормализатор отбросил строки (${dataLen}→${rows.length}) — пакет отклонён.`,
    };
  }
  const byState = new Map<string, SalesSheetRow>();
  for (const row of rows) {
    const sid = String(row.sale_id || '').trim();
    if (!sid) continue;
    const key = salesStateKey(row);
    if (!byState.has(key)) byState.set(key, row);
  }
  const toAppend: SalesSheetRow[] = [];
  for (const [key, row] of byState) if (!existing.has(key)) toAppend.push(row);
  if (toAppend.length === 0) return { status: 'OK_NO_GAPS', error: '', toAppend: [], statesInBatch: byState.size, gapsFilled: 0 };
  return { status: 'OK', error: '', toAppend, statesInBatch: byState.size, gapsFilled: toAppend.length };
}
