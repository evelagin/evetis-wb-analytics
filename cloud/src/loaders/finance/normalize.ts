/**
 * M4 · Нормализация Finance API → строки манифеста и RAW_WB_FINANCE (порт Wbfinancedaily.gs 1:1).
 *
 * Схема RAW не меняется: маппер отдаёт ровно те 54 ключа, что и finDailyMapRow_, все они есть
 * среди 74 колонок RAW_WB_FINANCE (load-job с ignoreUnknownValues=false упадёт громко при
 * расхождении). Колонки, которые маппер не заполняет (internal_sku, sku_match_status, …), остаются
 * NULL — как у Apps Script: SKU разрешается не при загрузке, а в V_WB_FINANCE_SEMANTIC.
 */
import {
  FIN_SOURCE_API, FIN_LIST_METRICS, FIN_DET_METRICS, FIN_REPORT_TYPES,
  type ListMetricCol, type DetMetricCol,
} from './constants.js';
import { isDigits, isCalendarDate, finNum, r2, md5Hex } from './parse.js';

export type ReportPeriod = 'DAILY' | 'WEEKLY';

export interface ListReport {
  reportId: string;
  reportType: number;
  dateFrom: string;
  dateTo: string;
  createDate: string;
  metrics: Record<ListMetricCol, number>;
  metricsJson: string;
}

export type ListRowResult = { ok: true; report: ListReport } | { ok: false; error: string };

/** Fail-closed валидация строки list ДО попадания в манифест (finDailyList_). */
export function parseListRow(o: Record<string, unknown>): ListRowResult {
  const rid = String(o.reportId || '');
  if (!isDigits(rid)) return { ok: false, error: `list: reportId не цифры: ${rid}` };
  const rt = Number(o.reportType);
  if (!FIN_REPORT_TYPES.has(rt)) {
    return { ok: false, error: `list: неподдерживаемый reportType=${String(o.reportType)} у reportId=${rid}` };
  }
  const df = String(o.dateFrom || '').substring(0, 10);
  const dt = String(o.dateTo || '').substring(0, 10);
  if (!isCalendarDate(df) || !isCalendarDate(dt)) return { ok: false, error: `list: битые даты (${df}/${dt}) у ${rid}` };
  if (df > dt) return { ok: false, error: `list: dateFrom > dateTo (${df} > ${dt}) у ${rid}` };
  const metrics = {} as Record<ListMetricCol, number>;
  const mj: Record<string, unknown> = {};
  for (const [src, col] of FIN_LIST_METRICS) {
    metrics[col] = r2(finNum(o[src]));
    mj[src] = o[src];
  }
  return {
    ok: true,
    report: {
      reportId: rid, reportType: rt, dateFrom: df, dateTo: dt,
      createDate: String(o.createDate || '').substring(0, 10),
      metrics, metricsJson: JSON.stringify(mj),
    },
  };
}

/** Ключ внутри отчёта: rrdId заполнен цифрами и уникален (KEY_CONFLICT, fail-closed до записи). */
export function checkDetailedKeys(rows: ReadonlyArray<Record<string, unknown>>): { empty: number; dup: number } {
  const seen = new Set<string>();
  let dup = 0;
  let empty = 0;
  for (const r of rows) {
    const rr = String(r.rrdId || '');
    if (!rr || !isDigits(rr)) { empty++; continue; }
    if (seen.has(rr)) dup++;
    else seen.add(rr);
  }
  return { empty, dup };
}

/** Σ detailed-метрик (суммирование в порядке строк, округление в конце — как в Apps Script). */
export function detailedSums(rows: ReadonlyArray<Record<string, unknown>>): Record<DetMetricCol, number> {
  const s = {} as Record<DetMetricCol, number>;
  for (const [, col] of FIN_DET_METRICS) s[col] = 0;
  for (const r of rows) for (const [src, col] of FIN_DET_METRICS) s[col] += finNum(r[src]);
  for (const [, col] of FIN_DET_METRICS) s[col] = r2(s[col]);
  return s;
}

export interface MapContext {
  runId: string;
  loadedAt: string;
  reportId: string;
  reportPeriod: ReportPeriod;
  reportType: number;
  dateFrom: string;
  dateTo: string;
}

export type RawFinanceRow = Record<string, string | number>;

/** finDailyMapRow_ — detailed-объект WB → строка RAW_WB_FINANCE. Порядок ключей = Apps Script. */
export function mapDetailedRow(o: Record<string, unknown>, ctx: MapContext): RawFinanceRow {
  const S = (v: unknown): string => (v === null || v === undefined ? '' : String(v));
  const rrd = S(o.rrdId);
  const rrDate = S(o.rrDate).substring(0, 10);
  const row: RawFinanceRow = {
    load_id: ctx.runId, run_id: ctx.runId,
    loaded_at: ctx.loadedAt,
    source_api: FIN_SOURCE_API,
    request_date_from: ctx.dateFrom, request_date_to: ctx.dateTo,
    report_id: ctx.reportId,
    report_period_from: ctx.dateFrom, report_period_to: ctx.dateTo,
    report_period: ctx.reportPeriod,
    report_type: ctx.reportType,
    row_hash: md5Hex(`${FIN_SOURCE_API}|${ctx.reportId}|${rrd}`),
    rrd_id: rrd, srid: S(o.srid), shk_id: S(o.shkId), sticker_id: S(o.stickerId),
    doc_type_name: S(o.docTypeName), supplier_oper_name: S(o.sellerOperName),
    order_dt: S(o.orderDt), sale_dt: S(o.saleDt), rr_dt: S(o.rrDate), create_dt: S(o.createDate),
    wb_nm_id: S(o.nmId), wb_vendor_code: S(o.vendorCode), barcode: S(o.sku),
    sa_name: S(o.vendorCode), ts_name: S(o.techSize),
    brand_name: S(o.brandName), subject_name: S(o.subjectName),
    office_name: S(o.officeName), country_name: S(o.country),
    retail_price: S(o.retailPrice), retail_amount: S(o.retailAmount),
    retail_price_withdisc_rub: S(o.retailPriceWithDisc),
    sale_percent: S(o.salePercent), commission_percent: S(o.commissionPercent),
    product_discount_for_report: S(o.productDiscountForReport),
    supplier_promo: S(o.sellerPromo), spp_percent: S(o.spp),
    quantity: S(o.quantity),
    for_pay: S(o.forPay),
    sales_commission: S(o.ppvzSalesCommission),
    logistics_amount: S(o.deliveryService),
    storage_fee: S(o.paidStorage),
    deduction: S(o.deduction), penalty: S(o.penalty),
    acceptance: S(o.paidAcceptance), additional_payment: S(o.additionalPayment),
    acquiring_fee: S(o.acquiringFee),
    currency: S(o.currency),
    rebill_logistics: S(o.rebillLogisticCost),
    processed_status: 'OK',
    // JSON ОДНОГО detailed-объекта (не HTTP-body); BigInt-поля — строками после quoted-transform.
    raw_json: JSON.stringify(o),
  };
  // Партиция: rrDate, иначе saleDt; иначе ключа нет → NULL (как в Apps Script).
  if (isCalendarDate(rrDate)) row._rr_date = rrDate;
  else {
    const sd = S(o.saleDt).substring(0, 10);
    if (isCalendarDate(sd)) row._rr_date = sd;
  }
  return row;
}

/** Статус прогона (finDailyRun_, шаг 7). */
export type RunStatus = 'OK' | 'OK_NO_NEW' | 'PARTIAL' | 'ERROR';

export interface RunCounters {
  discovered: number;
  loaded: number;
  errors: number;
  requests: number;
}

export function runStatusOf(c: RunCounters, anomalies: number, queueLeft: number): RunStatus {
  if (anomalies > 0 || (c.errors > 0 && c.loaded === 0)) return 'ERROR';
  if (c.errors > 0 || queueLeft > 0) return 'PARTIAL';
  if (c.loaded === 0 && c.discovered === 0) return 'OK_NO_NEW';
  return 'OK';
}
