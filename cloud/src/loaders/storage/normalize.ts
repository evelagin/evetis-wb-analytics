/** Нормализация строк отчёта WB Paid Storage в RAW_WB_PAID_STORAGE. */

export interface PaidStorageRow {
  observation_id: string;
  run_id: string;
  observed_at: string;
  date_msk: string;
  nm_id: number;
  chrt_id: number | null;
  barcode: string;
  warehouse: string;
  office_id: number | null;
  warehouse_coef: number | null;
  log_warehouse_coef: number | null;
  subject: string;
  brand: string;
  vendor_code: string;
  volume: number | null;
  calc_type: string;
  warehouse_price: number | null;
  barcodes_count: number | null;
  pallet_place_code: number | null;
  pallet_count: number | null;
  loyalty_discount: number | null;
  tariff_fix_date: string;
  tariff_lower_date: string;
  raw_row_json: string;
  ingested_at: string;
}

function n(v: unknown): number | null {
  if (v === null || v === undefined || v === '') return null;
  const x = Number(v);
  return Number.isFinite(x) ? x : null;
}
function s(v: unknown): string { return v === null || v === undefined ? '' : String(v); }
function day(v: unknown): string | null {
  const x = s(v).slice(0, 10);
  return /^\d{4}-\d{2}-\d{2}$/.test(x) ? x : null;
}

export function normalizePaidStorage(
  raw: unknown[],
  p: { observationId: string; runId: string; observedAtIso: string; startDate: string; endDate: string },
): { rows: PaidStorageRow[]; rejected: number; days: string[]; nmIds: number[]; storageRub: number } {
  const rows: PaidStorageRow[] = [];
  let rejected = 0;
  for (const item of raw) {
    const r = (item && typeof item === 'object' ? item : {}) as Record<string, unknown>;
    const d = day(r.date);
    const nm = n(r.nmId);
    if (!d || nm === null || d < p.startDate || d > p.endDate) { rejected++; continue; }
    rows.push({
      observation_id: p.observationId,
      run_id: p.runId,
      observed_at: p.observedAtIso,
      date_msk: d,
      nm_id: nm,
      chrt_id: n(r.chrtId),
      barcode: s(r.barcode),
      warehouse: s(r.warehouse),
      office_id: n(r.officeId),
      warehouse_coef: n(r.warehouseCoef),
      log_warehouse_coef: n(r.logWarehouseCoef),
      subject: s(r.subject),
      brand: s(r.brand),
      vendor_code: s(r.vendorCode),
      volume: n(r.volume),
      calc_type: s(r.calcType),
      warehouse_price: n(r.warehousePrice),
      barcodes_count: n(r.barcodesCount),
      pallet_place_code: n(r.palletPlaceCode),
      pallet_count: n(r.palletCount),
      loyalty_discount: n(r.loyaltyDiscount),
      tariff_fix_date: s(r.tariffFixDate),
      tariff_lower_date: s(r.tariffLowerDate),
      raw_row_json: JSON.stringify(r),
      ingested_at: p.observedAtIso,
    });
  }
  const days = [...new Set(rows.map((r) => r.date_msk))].sort();
  const nmIds = [...new Set(rows.map((r) => r.nm_id))].sort((a, b) => a - b);
  const storageRub = rows.reduce((acc, r) => acc + (r.warehouse_price ?? 0), 0);
  return { rows, rejected, days, nmIds, storageRub };
}
