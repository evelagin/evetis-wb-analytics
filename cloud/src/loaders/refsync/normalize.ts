/**
 * M5: REF_SKU_MASTER из листа SKU_MASTER — чистые функции, 1:1 с Apps Script Refsync.gs
 * (refStr_/refInt_/refBool_/refBuildSkuMaster_/refValidateSkuMaster_).
 *
 * Вход — сетка значений Sheets API (valueRenderOption=UNFORMATTED_VALUE): числа — числами,
 * флажки — boolean, пустые ячейки в хвосте строки API не присылает (здесь они = '', как в
 * getDataRange().getValues()).
 */
export type Cell = string | number | boolean | null | undefined;

export const refStr = (v: Cell): string => (v === null || v === undefined ? '' : String(v).trim());

export function refInt(v: Cell): number | null {
  if (v === null || v === undefined || v === '') return null;
  const n = Number(String(v).replace(/\s+/g, ''));
  if (!Number.isFinite(n) || Math.floor(n) !== n) return null;
  return n;
}

/** TRUE/ДА/1/YES/Y → true; иначе false. Нативный boolean — как есть. */
export function refBool(v: Cell): boolean {
  if (v === true) return true;
  if (v === false || v === null || v === undefined) return false;
  const s = String(v).trim().toUpperCase();
  return s === 'TRUE' || s === 'ДА' || s === '1' || s === 'YES' || s === 'Y';
}

export const REF_MARKETPLACE = 'WB';

export const SKU_MASTER_REQUIRED = [
  'active', 'internal_sku', 'product_name_short', 'product_name_full',
  'category', 'line', 'product_type', 'status', 'wb_nm_id', 'wb_vendor_code', 'barcode',
  'wb_subject_id', 'wb_subject_name', 'brand', 'volume_ml', 'is_bundle',
  'include_in_pnl', 'include_in_ads_analysis', 'include_in_supply_plan',
  'include_in_stock_alerts', 'data_quality_status',
] as const;

export interface SkuMasterRow {
  ref_run_id: string; marketplace: string; nm_id: number | null; internal_sku: string;
  product_name_short: string; product_name_full: string; category: string; line: string; product_type: string;
  is_bundle: boolean; status: string; active: boolean; wb_vendor_code: string; barcode: string;
  wb_subject_id: string; wb_subject_name: string; brand: string; volume_ml: string;
  include_in_pnl: boolean; include_in_ads_analysis: boolean; include_in_supply_plan: boolean;
  include_in_stock_alerts: boolean; data_quality_status: string; _synced_at: string;
}

export class SkuMasterShapeError extends Error {}

/** Строки REF из сетки листа. Нет обязательной колонки или данных — исключение (как в Apps Script). */
export function buildSkuMaster(values: Cell[][], runId: string, syncedIso: string): { rows: SkuMasterRow[]; source: number } {
  if (values.length < 2) throw new SkuMasterShapeError('SKU_MASTER: нет данных');
  const header = values[0] ?? [];
  const idx: Record<string, number> = {};
  header.forEach((h, i) => { idx[refStr(h)] = i; }); // дубль заголовка: побеждает последний, как в Apps Script
  for (const r of SKU_MASTER_REQUIRED) {
    if (!(r in idx)) throw new SkuMasterShapeError(`SKU_MASTER: нет обязательной колонки "${r}"`);
  }
  const rows: SkuMasterRow[] = [];
  let source = 0;
  for (let r = 1; r < values.length; r++) {
    const row = values[r] ?? [];
    const g = (name: string): Cell => { const v = row[idx[name]!]; return v === undefined ? '' : v; };
    const sku = refStr(g('internal_sku'));
    const nmRaw = refStr(g('wb_nm_id'));
    if (!sku && !nmRaw) continue; // пустая строка
    source++;
    rows.push({
      ref_run_id: runId, marketplace: REF_MARKETPLACE, nm_id: refInt(g('wb_nm_id')), internal_sku: sku,
      product_name_short: refStr(g('product_name_short')), product_name_full: refStr(g('product_name_full')),
      category: refStr(g('category')), line: refStr(g('line')), product_type: refStr(g('product_type')),
      is_bundle: refBool(g('is_bundle')), status: refStr(g('status')), active: refBool(g('active')),
      wb_vendor_code: refStr(g('wb_vendor_code')), barcode: refStr(g('barcode')),
      wb_subject_id: refStr(g('wb_subject_id')), wb_subject_name: refStr(g('wb_subject_name')),
      brand: refStr(g('brand')), volume_ml: refStr(g('volume_ml')),
      include_in_pnl: refBool(g('include_in_pnl')), include_in_ads_analysis: refBool(g('include_in_ads_analysis')),
      include_in_supply_plan: refBool(g('include_in_supply_plan')), include_in_stock_alerts: refBool(g('include_in_stock_alerts')),
      data_quality_status: refStr(g('data_quality_status')), _synced_at: syncedIso,
    });
  }
  return { rows, source };
}

export interface ValidationMetrics { n: number; n_nm: number; null_nm: number; null_sku: number; bundle_mismatch: number; n_single: number; n_bundle: number }

/** Те же жёсткие проверки, что refValidateSkuMaster_ (по строкам, уже лёгшим в *_DATA). */
export function validationErrors(v: ValidationMetrics, stagedExpected: number): string[] {
  const errors: string[] = [];
  if (v.n === 0) errors.push('0 строк — лист пуст/не прочитан');
  if (v.n !== stagedExpected) errors.push(`строк ${v.n} != загруженным ${stagedExpected}`);
  if (v.null_nm > 0) errors.push(`пустой nm_id: ${v.null_nm}`);
  if (v.n !== v.n_nm) errors.push(`nm_id не уникален: строк ${v.n}, различных ${v.n_nm}`);
  if (v.null_sku > 0) errors.push(`пустой internal_sku: ${v.null_sku}`);
  if (v.bundle_mismatch > 0) errors.push(`is_bundle ⇎ product_type: ${v.bundle_mismatch}`);
  return errors;
}

/** run_id формата Apps Script: REF_yyyyMMdd_HHmmss_<8 hex> (МСК). */
export function refRunId(ms: number, hex8: string): string {
  const p = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Europe/Moscow', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  }).formatToParts(new Date(ms));
  const g = (t: string) => p.find((x) => x.type === t)?.value ?? '00';
  const hh = g('hour') === '24' ? '00' : g('hour');
  return `REF_${g('year')}${g('month')}${g('day')}_${hh}${g('minute')}${g('second')}_${hex8}`;
}
