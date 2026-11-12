/**
 * M4 · Чистые функции финансового загрузчика: BigInt-безопасный разбор, даты, маппинг строки RAW
 * (паритет finDailyMapRow_), валидация list (reportType ∈ {1,2}), статус прогона.
 */
import { describe, it, expect } from 'vitest';
import {
  isDigits, isCalendarDate, finNum, r2, md5Hex, weekStart, todayMsk, quoteBigInts, parseSafe,
  newFinanceRunId, financeLoadJobId,
} from '../src/loaders/finance/parse.js';
import { parseListRow, mapDetailedRow, detailedSums, checkDetailedKeys, runStatusOf } from '../src/loaders/finance/normalize.js';
import { synthRow, rawJson } from './finance_fixture.js';

describe('parse: даты и числа (порт finDailyIsDate_/Num_/R2_/WeekStart_)', () => {
  it('календарная проверка даты, а не только формы', () => {
    expect(isCalendarDate('2026-11-09')).toBe(true);
    expect(isCalendarDate('2026-99-99')).toBe(false);
    expect(isCalendarDate('2026-02-30')).toBe(false);
    expect(isCalendarDate('2026-11-9')).toBe(false);
    expect(isCalendarDate('')).toBe(false);
  });
  it('числа WB: пробелы, запятая, мусор → 0', () => {
    expect(finNum('1 234,5')).toBe(1234.5);
    expect(finNum(null)).toBe(0);
    expect(finNum('abc')).toBe(0);
    expect(finNum(Number.NaN)).toBe(0);
    expect(r2(0.1 + 0.2)).toBe(0.3);
  });
  it('понедельник ISO-недели', () => {
    expect(weekStart('2026-11-09')).toBe('2026-11-09'); // пн
    expect(weekStart('2026-11-15')).toBe('2026-11-09'); // вс
    expect(weekStart('2026-11-10T00:00:00')).toBe('2026-11-09');
    expect(weekStart('мусор')).toBe('');
  });
  it('сегодня МСК: 22:30 UTC — уже следующие сутки', () => {
    const t = Date.UTC(2026, 10, 9, 22, 30);
    expect(todayMsk(t)).toBe('2026-11-10');
    expect(todayMsk(t, -3)).toBe('2026-11-07');
  });
  it('isDigits', () => {
    expect(isDigits('900000000000000001')).toBe(true);
    expect(isDigits('-1')).toBe(false);
    expect(isDigits('')).toBe(false);
  });
  it('md5 hex = Utilities.computeDigest MD5 (row_hash)', () => {
    expect(md5Hex('WB_API_FIN_V1|1|2')).toMatch(/^[0-9a-f]{32}$/);
    expect(md5Hex('abc')).toBe('900150983cd24fb0d6963f7d28e17f72');
  });
});

describe('parse: BigInt-безопасность (quoted-transform + сверка raw/safe)', () => {
  const big = '900000000000000123'; // > 2^53: Number() потерял бы младшие разряды
  it('reportId/rrdId/giId/shkId остаются строками без потери точности', () => {
    const body = `[{"rrdId":${big},"giId":9100000000000000001,"shkId":9200000000000000001,"forPay":1.5}]`;
    expect(quoteBigInts(body)).toContain(`"rrdId":"${big}"`);
    const p = parseSafe(body, 'rrdId');
    expect(p.mappingError).toBe(false);
    expect(p.arr![0]!.rrdId).toBe(big);
    expect(p.arr![0]!.giId).toBe('9100000000000000001');
    expect(p.arr![0]!.forPay).toBe(1.5); // прочие числа не трогаем
  });
  it('обёртка {data:[…]} распознаётся', () => {
    const p = parseSafe(`{"data":[{"reportId":${big}}]}`, 'reportId');
    expect(p.arr![0]!.reportId).toBe(big);
  });
  it('битый JSON и не-массив → mappingError', () => {
    expect(parseSafe('{"oops"', 'rrdId').mappingError).toBe(true);
    expect(parseSafe('{"x":1}', 'rrdId').note).toBe('массив не распознан');
  });
  it('число id в теле ≠ число объектов → PARSE_MAPPING_ERROR', () => {
    // id внутри вложенного поля — регекс найдёт два, объектов один
    const p = parseSafe(`[{"rrdId":1,"nested":{"rrdId":2}}]`, 'rrdId');
    expect(p.mappingError).toBe(true);
    expect(p.note).toContain('PARSE_MAPPING_ERROR');
  });
});

describe('parse: идентификаторы прогона и load-job', () => {
  it('run_id в формате Apps Script (МСК) + маркер C у суффикса Cloud Run', () => {
    const id = newFinanceRunId(Date.UTC(2026, 10, 10, 4, 30, 5), () => 0.1234);
    expect(id).toBe('FINDAILY_20261110_073005_C1234');
    expect(/^FINDAILY_\d{8}_\d{6}_C\d{1,4}$/.test(id)).toBe(true);
  });
  it('jobId детерминирован и содержит run_id (ownership-контракт)', () => {
    const a = financeLoadJobId('900000000000000001', 'FINDAILY_20261110_073005_1', 0);
    expect(a).toBe('fin_900000000000000001_FINDAILY_20261110_073005_1_0');
    expect(financeLoadJobId('9', 'FINDAILY_20261110_073005_1', 0)).toBe(financeLoadJobId('9', 'FINDAILY_20261110_073005_1', 0));
    expect(financeLoadJobId('9', 'a:b/c', 1)).toBe('fin_9_a-b-c_1');
  });
});

describe('normalize: строка list — fail-closed валидация', () => {
  const base = { reportId: '900000000000000001', reportType: 1, dateFrom: '2026-11-09', dateTo: '2026-11-09', createDate: '2026-11-10T04:00:00',
    forPaySum: 100.005, retailAmountSum: '1 000,10', deliveryServiceSum: 5, paidStorageSum: 0, paidAcceptanceSum: 0, deductionSum: 0, penaltySum: 0 };
  it('валидная строка → метрики с R2', () => {
    const r = parseListRow(base);
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    expect(r.report.metrics.list_forpay).toBe(100.01);
    expect(r.report.metrics.list_retail).toBe(1000.1);
    expect(r.report.createDate).toBe('2026-11-10');
    expect(JSON.parse(r.report.metricsJson).retailAmountSum).toBe('1 000,10');
  });
  it('reportType 3 (не исследован) → отказ, а не молчаливое расширение', () => {
    const r = parseListRow({ ...base, reportType: 3 });
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.error).toContain('неподдерживаемый reportType=3');
  });
  it.each([
    [{ reportId: 'abc' }, 'reportId не цифры'],
    [{ dateFrom: '2026-99-99' }, 'битые даты'],
    [{ dateFrom: '2026-11-10', dateTo: '2026-11-09' }, 'dateFrom > dateTo'],
    [{ reportType: '' }, 'неподдерживаемый reportType'],
  ])('битая строка %j → %s', (patch, msg) => {
    const r = parseListRow({ ...base, ...patch });
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.error).toContain(msg);
  });
});

describe('normalize: строка RAW_WB_FINANCE (паритет finDailyMapRow_)', () => {
  const ctx = { runId: 'FINDAILY_20261110_073005_1', loadedAt: '2026-11-10T04:30:05.000Z', reportId: '900000000000000001',
    reportPeriod: 'DAILY' as const, reportType: 1, dateFrom: '2026-11-09', dateTo: '2026-11-09' };
  const parsed = parseSafe(rawJson([synthRow('900000000000000777', 1, '2026-11-09')]), 'rrdId').arr![0]!;

  it('ровно 54 ключа в порядке Apps Script (+ _rr_date)', () => {
    const row = mapDetailedRow(parsed, ctx);
    const keys = Object.keys(row);
    expect(keys.length).toBe(54);
    expect(keys.slice(0, 12)).toEqual(['load_id', 'run_id', 'loaded_at', 'source_api', 'request_date_from', 'request_date_to',
      'report_id', 'report_period_from', 'report_period_to', 'report_period', 'report_type', 'row_hash']);
    expect(keys[keys.length - 1]).toBe('_rr_date');
    // все ключи — колонки живой RAW_WB_FINANCE (снимок схемы 10.11.2026)
    const RAW_COLS = new Set(('load_id loaded_at source_api request_date_from request_date_to report_id report_period_from report_period_to row_hash '
      + 'raw_row_number wb_operation_id rrd_id srid rid shk_id sticker_id doc_type_name supplier_oper_name operation_type_normalized order_dt sale_dt '
      + 'rr_dt create_dt period_month last_change_date wb_nm_id wb_vendor_code barcode sa_name ts_name brand_name subject_name internal_sku '
      + 'sku_match_status office_name warehouse_name country_name region_name retail_price retail_amount retail_price_withdisc_rub sale_percent '
      + 'commission_percent product_discount_for_report supplier_promo spp_percent quantity sale_amount return_amount_rub for_pay commission_amount '
      + 'sales_commission logistics_amount storage_fee deduction penalty acceptance additional_payment acquiring_fee compensation_amount other_amount '
      + 'currency money_parse_status money_parse_comment is_duplicate processed_status error_message raw_json error_code rebill_logistics _rr_date '
      + 'report_period report_type run_id').split(' '));
    expect(keys.filter((k) => !RAW_COLS.has(k))).toEqual([]);
  });
  it('значения: строки, BigInt без потерь, row_hash, raw_json одного объекта', () => {
    const row = mapDetailedRow(parsed, ctx);
    expect(row.rrd_id).toBe('900000000000000777');
    expect(row.shk_id).toBe('9200000000000000001');
    expect(row.report_type).toBe(1);
    expect(row.for_pay).toBe('700.25');
    expect(row.sa_name).toBe('SYN-1');
    expect(row.barcode).toBe('4600000000001');
    expect(row.processed_status).toBe('OK');
    expect(row.row_hash).toBe(md5Hex('WB_API_FIN_V1|900000000000000001|900000000000000777'));
    expect(JSON.parse(String(row.raw_json)).rrdId).toBe('900000000000000777');
    expect(row._rr_date).toBe('2026-11-09');
    // internal_sku / sku_match_status при загрузке НЕ ставятся (разрешение SKU — в V_WB_FINANCE_SEMANTIC)
    expect('internal_sku' in row).toBe(false);
    expect('sku_match_status' in row).toBe(false);
  });
  it('_rr_date: rrDate → saleDt → нет ключа (NULL-партиция, как в Apps Script)', () => {
    expect(mapDetailedRow({ ...parsed, rrDate: '' }, ctx)._rr_date).toBe('2026-11-09');
    expect('_rr_date' in mapDetailedRow({ ...parsed, rrDate: 'x', saleDt: null }, ctx)).toBe(false);
  });
  it('NULL/undefined → пустая строка', () => {
    expect(mapDetailedRow({ ...parsed, srid: null, currency: undefined }, ctx).srid).toBe('');
  });
});

describe('normalize: ключи и суммы detailed', () => {
  it('KEY_CONFLICT: пустые и дубли rrdId', () => {
    expect(checkDetailedKeys([{ rrdId: '1' }, { rrdId: '2' }])).toEqual({ empty: 0, dup: 0 });
    expect(checkDetailedKeys([{ rrdId: '1' }, { rrdId: '1' }, { rrdId: '' }, { rrdId: 'x' }])).toEqual({ empty: 2, dup: 1 });
  });
  it('суммы — по всем строкам, R2 в конце', () => {
    const s = detailedSums([{ forPay: '0.1' }, { forPay: 0.2 }, { forPay: '1 000,005' }]);
    expect(s.det_forpay).toBe(1000.31);
    expect(s.det_penalty).toBe(0);
  });
});

describe('normalize: статус прогона (finDailyRun_ шаг 7)', () => {
  const c = (o: Partial<{ discovered: number; loaded: number; errors: number }>) => ({ discovered: 0, loaded: 0, errors: 0, requests: 0, ...o });
  it.each([
    [c({}), 0, 0, 'OK_NO_NEW'],
    [c({ loaded: 1 }), 0, 0, 'OK'],
    [c({ discovered: 1 }), 0, 0, 'OK'],
    [c({ loaded: 1, errors: 1 }), 0, 0, 'PARTIAL'],
    [c({ loaded: 1 }), 0, 2, 'PARTIAL'],
    [c({ errors: 1 }), 0, 0, 'ERROR'],
    [c({ loaded: 3 }), 1, 0, 'ERROR'],
  ])('%j anomalies=%i queueLeft=%i → %s', (cnt, an, ql, st) => {
    expect(runStatusOf(cnt, an, ql)).toBe(st);
  });
});
