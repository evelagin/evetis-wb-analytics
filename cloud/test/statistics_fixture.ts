/**
 * M2 — общая обвязка тестов загрузчиков statistics-api.
 *   • синтетические строки WB (заказы/продажи) — без реальных srid, сумм и номеров;
 *   • исполнение БОЕВОГО Apps Script (apps-script/ingestion/*.gs) в node:vm с заглушками
 *     BigQuery/Sheets/UrlFetch — эталон для replay-паритета;
 *   • поддельные BigQuery и WB для Cloud Run.
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { createHash } from 'node:crypto';
import vm from 'node:vm';
import type { StatsDeps } from '../src/loaders/statistics/index.js';
import type { StatsTransport } from '../src/loaders/statistics/wbApi.js';
import type { Boundary } from '../src/loaders/statistics/bq.js';
import type { RefSkuRow } from '../src/loaders/statistics/common.js';
import type { LoaderContext } from '../src/loaders/types.js';
import { loadConfig } from '../src/config.js';
import { Logger } from '../src/logging.js';

type Obj = Record<string, unknown>;
const here = dirname(fileURLToPath(import.meta.url));
const GS = join(here, '..', '..', 'apps-script', 'ingestion');

/** Момент запуска обеих реализаций: 10.11.2026 09:31:00 МСК. */
export const NOW = new Date('2026-11-10T06:31:00Z');
export const NOW_STAMP = '20261110_093100';
export const NOW_DT = '2026-11-10 09:31:00';

/** Справочник SKU (синтетика): 101 и 202 есть; у 303 нет nm, но есть его barcode; 404 — нигде. */
export const REF_ROWS: RefSkuRow[] = [
  { nm_id: 101, internal_sku: 'TEST-SKU-A', barcode: '2000000000011' },
  { nm_id: 202, internal_sku: 'TEST-SKU-B', barcode: '2000000000022' },
  { nm_id: 909, internal_sku: 'TEST-SKU-Z', barcode: '2000000000033' },
];

export function orderRow(i: number, over: Obj = {}): Obj {
  return {
    date: '2026-11-08T10:15:00', lastChangeDate: '2026-11-09T12:00:00',
    warehouseName: 'Склад-Т', warehouseType: 'Склад WB', countryName: 'Россия',
    oblastOkrugName: 'Округ-Т', regionName: 'Регион-Т', supplierArticle: `ART-${i}`,
    nmId: 101, barcode: '2000000000011', category: 'Красота', subject: 'Крем', brand: 'TESTBRAND',
    techSize: '0', incomeID: 0, isSupply: false, isRealization: true,
    totalPrice: 1000, discountPercent: 60, spp: 30, finishedPrice: 280, priceWithDisc: 400,
    isCancel: false, cancelDate: '0001-01-01T00:00:00', sticker: `90000${i}`,
    gNumber: `G-TEST-${i}`, srid: `test.srid.${i}`, ...over,
  };
}

export function saleRow(i: number, over: Obj = {}): Obj {
  return {
    date: '2026-11-08T10:15:00', lastChangeDate: '2026-11-09T12:00:00',
    warehouseName: 'Склад-Т', warehouseType: 'Склад WB', countryName: 'Россия',
    oblastOkrugName: 'Округ-Т', regionName: 'Регион-Т', supplierArticle: `ART-${i}`,
    nmId: 101, barcode: '2000000000011', category: 'Красота', subject: 'Крем', brand: 'TESTBRAND',
    techSize: '0', incomeID: 5000 + i, isSupply: false, isRealization: true,
    totalPrice: 1000, discountPercent: 60, spp: 30, paymentSaleAmount: 0, forPay: 233.456,
    finishedPrice: 280, priceWithDisc: 400, saleID: `S${9000000 + i}`, sticker: `90000${i}`,
    gNumber: `G-TEST-${i}`, srid: `test.srid.${i}`, ...over,
  };
}

// ─────────────────────────────────────────────────────────────────────────
// Сценарий WB: последовательность ответов (один и тот же для обеих сторон)
// ─────────────────────────────────────────────────────────────────────────

export interface WbResp { status: number; body: unknown; retryAfter?: string }

/** Очередь ответов WB + журнал URL. */
export function wbScript(responses: WbResp[]) {
  const urls: string[] = [];
  let i = 0;
  const next = (url: string): WbResp => {
    urls.push(url);
    const r = responses[Math.min(i, responses.length - 1)];
    i++;
    if (!r) throw new Error('нет ответа WB в сценарии');
    return r;
  };
  return { urls, next };
}

export function fakeTransport(script: ReturnType<typeof wbScript>, sleeps: number[] = []): StatsTransport {
  return {
    fetch: async (url) => {
      const r = script.next(url);
      const body = typeof r.body === 'string' ? r.body : JSON.stringify(r.body);
      return { status: r.status, text: async () => body, headers: { get: (n: string) => (n === 'Retry-After' ? r.retryAfter ?? null : null) } };
    },
    sleep: async (ms) => { sleeps.push(ms); },
    nowMs: () => NOW.getTime(),
  };
}

// ─────────────────────────────────────────────────────────────────────────
// Поддельный BigQuery для Cloud Run: таблица = массив строк (форма NDJSON)
// ─────────────────────────────────────────────────────────────────────────

const md5 = (s: string) => createHash('md5').update(s, 'utf8').digest('hex');

export class FakeStatsBq {
  appended: Array<{ table: string; rows: Obj[]; jobId: string }> = [];
  skuQueries = 0;
  constructor(public table: Obj[] = [], public ref: RefSkuRow[] = REF_ROWS, public failAppend = false) {}

  private api(src: string) { return this.table.filter((r) => r.source_api === src); }

  /** Модель SQL StatsBq: watermark — MAX строк инкремента, иначе MAX API-строк (у продаж — кроме сверки). */
  private watermark(rows: Obj[], inc: string, excl?: string): string {
    const max = (rs: Obj[]) => rs.map((r) => String(r.last_change_date)).sort().pop() ?? '';
    const lid = (r: Obj) => String(r.load_id ?? '');
    return max(rows.filter((r) => lid(r).startsWith(inc))) || max(rows.filter((r) => !excl || !lid(r).startsWith(excl)));
  }

  async ordersBoundary(): Promise<Boundary> {
    const rows = this.api('WB_API_ORDERS').filter((r) => r.last_change_date && r.last_change_date !== '');
    const wm = this.watermark(rows, 'ORD_INC_');
    const keys = new Set(rows.filter((r) => r.last_change_date === wm).map((r) => `${String(r.srid ?? '')}|${String(r.row_hash ?? '')}`));
    return { watermarkStorage: wm, keys };
  }

  async salesBoundary(): Promise<Boundary> {
    const rows = this.api('WB_API_SALES').filter((r) => r.last_change_date != null);
    const wm = this.watermark(rows, 'SALE_INC_', 'SALE_RECON_');
    const keys = new Set(rows.filter((r) => r.last_change_date === wm && r.sale_id && String(r.sale_id).trim() && r.raw_json != null)
      .map((r) => `${String(r.sale_id)}|${md5(String(r.raw_json))}`));
    return { watermarkStorage: wm, keys };
  }

  async salesStateKeysSince(_t: string, from: string): Promise<Set<string>> {
    return new Set(this.api('WB_API_SALES').filter((r) => String(r.last_change_date ?? '') >= from && r.sale_id && r.raw_json != null)
      .map((r) => `${String(r.sale_id)}|${md5(String(r.raw_json))}`));
  }

  async loadSkuRows(): Promise<RefSkuRow[]> { this.skuQueries++; return this.ref; }

  async appendRows(table: string, rows: object[], jobId: string): Promise<number> {
    if (this.failAppend) throw new Error('BQ load error: synthetic');
    this.appended.push({ table, rows: rows as Obj[], jobId });
    this.table.push(...(rows as Obj[]));
    return rows.length;
  }

  lastRows(): Obj[] { return this.appended[this.appended.length - 1]?.rows ?? []; }
}

export function statsDeps(bq: FakeStatsBq, transport: StatsTransport): StatsDeps {
  return { bq, getToken: async () => 'synthetic-token', transport, now: () => NOW };
}

export function statsCtx(env: Record<string, string> = {}, environment: 'shadow' | 'prod' = 'shadow'): LoaderContext {
  const config = loadConfig({
    ENVIRONMENT: environment, GCP_PROJECT_ID: 'test-project', BQ_RAW_DATASET: 'wb_raw',
    ...(environment === 'prod' ? { ORDERS_RAW_TABLE: 'RAW_WB_ORDERS', SALES_RAW_TABLE: 'RAW_WB_SALES_RETURNS' } : {}),
    ...env,
  });
  return { config, logger: new Logger({}, 'error'), logicalPeriod: '2026-11-09', runId: 'shadow:orders:2026-11-09:sha:exec', targetDate: '2026-11-09' };
}

// ─────────────────────────────────────────────────────────────────────────
// Эталон: исполняемый Apps Script в node:vm
// ─────────────────────────────────────────────────────────────────────────

class FixedDate extends Date {
  constructor(...a: unknown[]) {
    if (a.length === 0) super(NOW.getTime());
    else super(...(a as [string]));
  }
  static now(): number { return NOW.getTime(); }
}

function extractFn(src: string, name: string): string {
  const m = new RegExp(`function ${name}\\([^)]*\\) \\{[\\s\\S]*?\\n\\}`).exec(src);
  if (!m) throw new Error(`${name} не найдена`);
  return m[0];
}

export interface AppsScriptRun {
  /** Строки, ушедшие в bqLoadRows_ (форма NDJSON Apps Script). */
  loaded: Obj[];
  /** Итог журнала INGEST_RUNS: COMPLETE или ERROR с кодом. */
  ingest: { status: string; code?: string; rowsFetched?: unknown; rowsLoaded?: unknown } | null;
  result: Obj;
  props: Record<string, string>;
  urls: string[];
}

/**
 * Запускает runWbOrdersIncremental / runWbSalesIncremental / runWbSalesNightReconcile боевого Apps Script.
 * existing — RAW-строки, которые уже лежат в таблице (форма NDJSON Apps Script).
 */
export function runAppsScript(
  fn: 'runWbOrdersIncremental' | 'runWbSalesIncremental' | 'runWbSalesNightReconcile',
  opts: { props: Record<string, string>; existing: Obj[]; wb: WbResp[]; ref?: RefSkuRow[] },
): AppsScriptRun {
  const read = (f: string) => readFileSync(join(GS, f), 'utf8');
  const finance = read('Wbfinance.gs');
  const clean = read('Cleanwbdaily.gs');
  const helpers = [
    extractFn(finance, 'normalizeDateKey_'), extractFn(finance, 'normalizeNmIdFinance_'),
    extractFn(clean, 'pad2_'), extractFn(clean, 'roundTwo_'),
  ].join('\n');
  const files = ['Utils.gs', 'IngestRunLog.gs', 'WbOrdersLoader.gs', 'WbOrdersBigQuery.gs',
    'WbSalesReturnsLoader.gs.gs', 'WbSalesReturnsBigQuery.gs', 'WbSalesIncremental.gs', 'WbSalesReconcile.gs'];

  const props: Record<string, string> = { ...opts.props };
  const script = wbScript(opts.wb);
  const out: AppsScriptRun = { loaded: [], ingest: null, result: {}, props, urls: script.urls };
  const byNm: Record<string, { sku: string }> = {};
  const byBarcode: Record<string, string> = {};
  for (const r of opts.ref ?? REF_ROWS) {
    byNm[String(r.nm_id)] = { sku: String(r.internal_sku) };
    if (r.barcode) byBarcode[String(r.barcode)] = String(r.nm_id);
  }
  const existing = opts.existing;

  const sandbox: Obj = {
    console: { log: () => {}, error: () => {} },
    Logger: { log: () => {} },
    SpreadsheetApp: { getActiveSpreadsheet: () => ({}) },
    LockService: { getScriptLock: () => ({ tryLock: () => true, releaseLock: () => {} }) },
    PropertiesService: {
      getScriptProperties: () => ({
        getProperty: (k: string) => (k in props ? props[k] : null),
        setProperty: (k: string, v: string) => { props[k] = String(v); },
        deleteProperty: (k: string) => { delete props[k]; },
      }),
    },
    Utilities: {
      sleep: () => {},
      getUuid: () => '00000000-0000-0000-0000-000000000000',
      DigestAlgorithm: { MD5: 'MD5' },
      Charset: { UTF_8: 'UTF_8' },
      computeDigest: (_a: string, s: string) => Array.from(createHash('md5').update(String(s), 'utf8').digest()).map((b) => (b > 127 ? b - 256 : b)),
      formatDate: (d: Date, _tz: string, f: string) => {
        if (f === 'yyyyMMdd_HHmmss') return NOW_STAMP;
        if (f === 'yyyy-MM-dd HH:mm:ss') return NOW_DT;
        if (f === 'yyyy-MM-dd') {
          return new Intl.DateTimeFormat('en-CA', { timeZone: 'Europe/Moscow', year: 'numeric', month: '2-digit', day: '2-digit' }).format(d);
        }
        if (f === 'yyyyMMddHHmmss') return '20261110093100';
        throw new Error('formatDate: ' + f);
      },
    },
    UrlFetchApp: {
      fetch: (url: string) => {
        const r = script.next(url);
        const body = typeof r.body === 'string' ? r.body : JSON.stringify(r.body);
        return {
          getResponseCode: () => r.status, getContentText: () => body,
          getHeaders: () => (r.retryAfter ? { 'Retry-After': r.retryAfter } : {}),
        };
      },
    },
    // Часы Apps Script = NOW: от них зависят ingestClosedDayMsk_ и окно сверки (wbSalesReconcileFromLcd_).
    Date: FixedDate,
  };
  vm.createContext(sandbox);
  vm.runInContext(helpers, sandbox);
  for (const f of files) vm.runInContext(read(f), sandbox, { filename: f });

  // ── Заглушки BigQuery/Sheets поверх боевого кода (вызовы разрешаются через глобал контекста) ──
  const md5h = (s: string) => createHash('md5').update(s, 'utf8').digest('hex');
  Object.assign(sandbox, {
    getBqConfig_: () => ({ projectId: 'p', datasetId: 'wb_raw', location: 'EU' }),
    buildSkuIndex_: () => ({ byNm, byBarcode, byVendor: {} }),
    wbOrdersBqEnsureTable_: () => false,
    wbSalesBqEnsureTable_: () => false,
    bqLoadRows_: (_t: string, rows: Obj[]) => { out.loaded.push(...rows); return rows.length; },
    wbOrdersBqBoundaryKeys_: (lcd: string) => {
      const set: Record<string, boolean> = {};
      for (const r of existing) if (r.source_api === 'WB_API_ORDERS' && r.last_change_date === lcd) set[`${String(r.srid ?? '')}|${String(r.row_hash ?? '')}`] = true;
      return set;
    },
    wbSalesBqBoundaryStateKeys_: (wm: string) => {
      const set: Record<string, boolean> = {};
      for (const r of existing) if (r.source_api === 'WB_API_SALES' && r.last_change_date === wm && r.sale_id && r.raw_json != null) set[`${String(r.sale_id)}|${md5h(String(r.raw_json))}`] = true;
      return set;
    },
    wbSalesBqStateKeysSince_: (from: string) => {
      const set: Record<string, boolean> = {};
      for (const r of existing) if (r.source_api === 'WB_API_SALES' && String(r.last_change_date ?? '') >= from && r.sale_id && r.raw_json != null) set[`${String(r.sale_id)}|${md5h(String(r.raw_json))}`] = true;
      return set;
    },
    ingestRunStart_: () => 'INS_TEST',
    ingestRunComplete_: (_id: string, f: unknown, l: unknown) => { out.ingest = { status: 'COMPLETE', rowsFetched: f, rowsLoaded: l }; return true; },
    ingestRunError_: (_id: string, code: string) => { out.ingest = { status: 'ERROR', code }; return true; },
  });
  out.result = vm.runInContext(`${fn}()`, sandbox) as Obj;
  return out;
}
