/**
 * M2 — общие чистые помощники загрузчиков statistics-api (заказы, продажи, ночная сверка).
 * Каждая функция — порт эталона Apps Script; ссылка на исходник стоит у функции.
 * Модуль без побочных эффектов: тестируется офлайн.
 */
import { createHash } from 'node:crypto';

const pad2 = (n: number | string): string => String(n).padStart(2, '0');

/**
 * normalizeDateKey_ (Wbfinance.gs:66) → 'YYYY-MM-DD' или ''.
 * WB отдаёт `date` как 'YYYY-MM-DDTHH:mm:ss', поэтому в бою работает ветка ISO.
 * Ветка fallback (new Date) в Apps Script считает в часовом поясе проекта (Europe/Moscow);
 * здесь тот же пояс задан явно — Cloud Run живёт в UTC.
 */
export function normalizeDateKey(value: unknown): string {
  if (value === null || value === undefined || value === '' || value === 0 || value === false) return '';
  if (value instanceof Date) {
    if (Number.isNaN(value.getTime())) return '';
    return mskDate(value);
  }
  let s = String(value).trim();
  if (!s) return '';
  s = s.split('T')[0]!.split(' ')[0]!;
  const iso = /^(\d{4})-(\d{1,2})-(\d{1,2})$/.exec(s);
  if (iso) return `${iso[1]}-${pad2(iso[2]!)}-${pad2(iso[3]!)}`;
  const ru = /^(\d{1,2})\.(\d{1,2})\.(\d{4})$/.exec(s);
  if (ru) return `${ru[3]}-${pad2(ru[2]!)}-${pad2(ru[1]!)}`;
  const ruShort = /^(\d{1,2})\.(\d{1,2})\.(\d{2})$/.exec(s);
  if (ruShort) {
    const yr = Number(ruShort[3]);
    return `${yr >= 70 ? 1900 + yr : 2000 + yr}-${pad2(ruShort[2]!)}-${pad2(ruShort[1]!)}`;
  }
  const us = /^(\d{1,2})\/(\d{1,2})\/(\d{4})$/.exec(s);
  if (us) return `${us[3]}-${pad2(us[1]!)}-${pad2(us[2]!)}`;
  const d = new Date(s);
  if (!Number.isNaN(d.getTime())) return mskDate(d);
  return '';
}

/**
 * normalizeNmIdFinance_ (Wbfinance.gs:132): число → без дробной части; строка → trim, без '.0+';
 * пусто и '0' → ''.
 */
export function normalizeNmId(val: unknown): string {
  if (val === null || val === undefined || val === '') return '';
  let v: unknown = val;
  if (typeof v === 'number') v = Math.floor(v);
  const s = String(v).trim().replace(/\.0+$/, '');
  if (!s || s === '0') return '';
  return s;
}

/**
 * ordersValidWatermark_ / salesValidWatermark_ (одинаковы): ТОЛЬКО 'YYYY-MM-DDTHH:mm:ss[.дробь]'
 * плюс календарная корректность. Дата без времени и форма с пробелом недопустимы.
 */
export function isValidWatermark(s: unknown): boolean {
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(\.\d+)?$/.exec(String(s ?? ''));
  if (!m) return false;
  const Y = +m[1]!, Mo = +m[2]!, D = +m[3]!, H = +m[4]!, Mi = +m[5]!, S = +m[6]!;
  if (Y < 1) return false;
  if (Mo < 1 || Mo > 12) return false;
  if (H > 23 || Mi > 59 || S > 59) return false;
  const leap = (Y % 4 === 0 && Y % 100 !== 0) || Y % 400 === 0;
  const dim = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][Mo - 1]!;
  return D >= 1 && D <= dim;
}

/** ordersMd5_ / salesMd5_: MD5 над UTF-8, lowercase hex (= TO_HEX(MD5(x)) в BigQuery). */
export function md5Hex(s: string): string {
  return createHash('md5').update(String(s), 'utf8').digest('hex');
}

function mskParts(d: Date): Record<string, string> {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Europe/Moscow', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  }).formatToParts(d);
  const out: Record<string, string> = {};
  for (const p of parts) out[p.type] = p.value;
  if (out.hour === '24') out.hour = '00';
  return out;
}

/** 'YYYY-MM-DD' по Москве. */
export function mskDate(d: Date): string {
  const p = mskParts(d);
  return `${p.year}-${p.month}-${p.day}`;
}

/** Utilities.formatDate(d, 'Europe/Moscow', 'yyyy-MM-dd HH:mm:ss') — loaded_at/started_at Apps Script. */
export function mskDateTime(d: Date): string {
  const p = mskParts(d);
  return `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}:${p.second}`;
}

/** Utilities.formatDate(d, 'Europe/Moscow', 'yyyyMMdd_HHmmss') — суффикс load_id Apps Script. */
export function mskStamp(d: Date): string {
  const p = mskParts(d);
  return `${p.year}${p.month}${p.day}_${p.hour}${p.minute}${p.second}`;
}

/**
 * Слот запуска для guard: час по Москве. Им различаются запуски одних и тех же суток D-1
 * (Apps Script пишет INGEST_RUNS каждым запуском, а guard LOADER_RUNS без слота пропустил бы
 * все запуски дня после первого COMPLETE). См. registry.ts и runbook.
 */
export function statsSlot(now: Date = new Date()): string {
  const p = mskParts(now);
  return `stats-slot:${p.year}-${p.month}-${p.day}T${p.hour}`;
}

/**
 * Индекс SKU для привязки при загрузке (buildSkuIndex_, Wbfinanceimportfromdrive.gs:736).
 * byNm: нормализованный nm_id → internal_sku; byBarcode: barcode (trim) → nm_id.
 */
export interface SkuIndex {
  byNm: Map<string, string>;
  byBarcode: Map<string, string>;
}

export interface RefSkuRow {
  nm_id: unknown;
  internal_sku: unknown;
  barcode: unknown;
}

/** Индекс из строк REF_SKU_MASTER (то же правило, что buildSkuIndex_ над листом SKU_MASTER). */
export function buildSkuIndex(rows: RefSkuRow[]): SkuIndex {
  const byNm = new Map<string, string>();
  const byBarcode = new Map<string, string>();
  for (const r of rows) {
    const nm = normalizeNmId(r.nm_id);
    if (!nm) continue;
    byNm.set(nm, r.internal_sku === null || r.internal_sku === undefined ? '' : String(r.internal_sku));
    const bc = r.barcode === null || r.barcode === undefined ? '' : String(r.barcode).trim();
    if (bc) byBarcode.set(bc, nm);
  }
  return { byNm, byBarcode };
}

/**
 * Привязка SKU строки API (WbOrdersLoader.gs:444-458 и WbSalesReturnsLoader.gs:425-439 — одинаковы):
 * сначала по nmId, иначе по barcode. internal_sku пишется только при совпадении.
 * Статус замораживается в RAW навсегда — пересборка витрины его не лечит.
 */
export function matchSku(index: SkuIndex, nmIdRaw: unknown, barcodeRaw: unknown): { matched: boolean; internalSku: string } {
  const nm = normalizeNmId(nmIdRaw);
  if (nm && index.byNm.has(nm)) return { matched: true, internalSku: index.byNm.get(nm) ?? '' };
  const bc = String(barcodeRaw || '').trim();
  if (bc && index.byBarcode.has(bc)) {
    const rNm = index.byBarcode.get(bc)!;
    return { matched: true, internalSku: index.byNm.get(rNm) ?? '' };
  }
  return { matched: false, internalSku: '' };
}

