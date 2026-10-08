/**
 * OZON CPO (Phase B) — строка отчёта → строка RAW `ozon_raw.RAW_OZON_ADS_CPO_ORDERS`.
 *
 * RAW хранит ТОЛЬКО то, что прислал источник, плюс происхождение. Производные величины —
 * offer_id продвигаемого товара, internal_sku, дата заказа МСК и кампания — вычисляет
 * каноническая вью `ozon_mart.V_OZON_ADS_CPO_ORDERS` на каждом чтении: замороженная при загрузке
 * производная не лечится пересборкой (урок `sku_match_status`, wb_raw).
 *
 * Естественный ключ — (семейство, дата списания, ID заказа, заказанный SKU, продвигаемый SKU):
 * на 315 строках истории 2025-05…2026-10 он уникален, и Ozon сам сводит несколько отправлений
 * одного заказа в одну строку с количеством. `row_key` — его SHA-256, стабильный между прогонами.
 */
import { createHash } from 'node:crypto';
import type { CpoFamily, CpoReport } from './parse.js';

export interface CpoRawRow {
  row_key: string;
  report_family: CpoFamily;
  charge_date: string;
  order_id: string;
  order_number: string;
  ordered_sku: string;
  promoted_sku: string;
  ordered_offer_id_reported: string;
  order_source: string | null;
  product_name: string;
  quantity: number;
  unit_sale_price_rub: string;
  sale_value_rub: string;
  rate_pct: string;
  rate_rub: string;
  expense_rub: string;
  completeness_status: 'COMPLETE';
  report_uuid: string;
  report_title: string;
  requested_from: string;
  requested_to: string;
  requested_from_utc: string;
  requested_to_utc: string;
  fetched_at: string;
  run_id: string;
  raw_line: string;
}

export function cpoNaturalKey(r: { report_family: string; charge_date: string; order_id: string; ordered_sku: string; promoted_sku: string }): string {
  return createHash('sha256')
    .update([r.report_family, r.charge_date, r.order_id, r.ordered_sku, r.promoted_sku].join('\x1f'), 'utf8')
    .digest('hex');
}

export function normalizeCpoReport(rep: CpoReport, prov: {
  uuid: string; fromUtc: string; toUtc: string; fetchedAt: string; runId: string;
}): CpoRawRow[] {
  return rep.rows.map((r) => {
    const base = {
      report_family: rep.family, charge_date: r.chargeDate, order_id: r.orderId,
      ordered_sku: r.orderedSku, promoted_sku: r.promotedSku,
    };
    return {
      row_key: cpoNaturalKey(base), ...base,
      order_number: r.orderNumber, ordered_offer_id_reported: r.orderedOfferId, order_source: r.orderSource,
      product_name: r.productName, quantity: r.quantity, unit_sale_price_rub: r.unitSalePriceRub,
      sale_value_rub: r.saleValueRub, rate_pct: r.ratePct, rate_rub: r.rateRub, expense_rub: r.expenseRub,
      // Окно прогона — только закрытые сутки, период и итог отчёта сверены (parse.ts):
      // неполный отчёт до этой точки не доходит.
      completeness_status: 'COMPLETE',
      report_uuid: prov.uuid, report_title: rep.title,
      requested_from: rep.periodFrom, requested_to: rep.periodTo,
      requested_from_utc: prov.fromUtc, requested_to_utc: prov.toUtc,
      fetched_at: prov.fetchedAt, run_id: prov.runId, raw_line: r.rawLine,
    };
  });
}

/** Дубль естественного ключа внутри прогона — отказ, а не схлопывание: две строки могут быть двумя списаниями. */
export function duplicateKeys(rows: readonly CpoRawRow[]): string[] {
  const seen = new Set<string>(), dup = new Set<string>();
  for (const r of rows) { if (seen.has(r.row_key)) dup.add(r.row_key); seen.add(r.row_key); }
  return [...dup];
}
