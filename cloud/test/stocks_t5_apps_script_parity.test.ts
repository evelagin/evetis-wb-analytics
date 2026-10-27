import { describe, it, expect, vi, beforeEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import vm from 'node:vm';
import type { LoaderContext } from '../src/loaders/types.js';
import { loadConfig } from '../src/config.js';
import { Logger } from '../src/logging.js';

/**
 * PR-Mig1c — replay-parity 1:1 с БОЕВЫМ Apps Script (дизайн rev2 §8.A).
 *
 * Один и тот же сохранённый ответ WB (T6 + задача T5) прогоняется:
 *   A) через исполняемый код apps-script/ingestion/WbStocksSnapshot.gs
 *      (wbStocksSnapshotCore_ в node:vm, BQ/Sheets/UrlFetch — заглушки);
 *   B) через Cloud Run stocksLoader (fetch и StocksBq — заглушки).
 * Сравниваются RAW-строки T6, RAW-строки T5 и все метрики manifest. Расходятся только
 * идентификаторы запуска (snapshot_id/load_id/snapshot_ts): в Apps Script они случайные
 * на каждый запуск, в Cloud Run — детерминированные на сутки (дизайн PR-Mig1 fix2).
 */

const here = dirname(fileURLToPath(import.meta.url));
const repo = join(here, '..', '..');
const T6 = JSON.parse(readFileSync(join(here, 'fixtures', 'wb_stocks_t6_anonymized.json'), 'utf8'));
const T5 = JSON.parse(readFileSync(join(here, 'fixtures', 'wb_stocks_t5_anonymized.json'), 'utf8'));
const SKU = new Map<number, string>([[101, 'SKU-A'], [202, 'SKU-B']]);

type Obj = Record<string, unknown>;

/** WB-роутер ответов: одна и та же лента для обеих реализаций. */
function wbRouter() {
  let polls = 0;
  return (url: string): string => {
    if (url.includes('/stocks-report/wb-warehouses')) return JSON.stringify(T6);
    if (url.endsWith('/download')) return JSON.stringify(T5.download);
    if (url.endsWith('/status')) return JSON.stringify(T5.status[Math.min(polls++, T5.status.length - 1)]);
    if (url.includes('/warehouse_remains')) return JSON.stringify(T5.create);
    throw new Error('неожиданный URL ' + url);
  };
}

// ── A. Apps Script ────────────────────────────────────────────────────────────
function runAppsScript(prevGap: number | null, t5Broken = false) {
  const snapshotSrc = readFileSync(join(repo, 'apps-script', 'ingestion', 'WbStocksSnapshot.gs'), 'utf8');
  const financeSrc = readFileSync(join(repo, 'apps-script', 'ingestion', 'Wbfinance.gs'), 'utf8');
  const bqSrc = readFileSync(join(repo, 'apps-script', 'ingestion', 'WbStocksBigQuery.gs'), 'utf8');
  const nmFn = financeSrc.match(/function normalizeNmIdFinance_\(val\) \{[\s\S]*?\n\}/);
  if (!nmFn) throw new Error('normalizeNmIdFinance_ не найдена в Wbfinance.gs');

  const route = wbRouter();
  const out: { t6: Obj[]; t5: Obj[]; manifest: Obj | null; status: string } = { t6: [], t5: [], manifest: null, status: '' };
  const byNm: Record<string, { sku: string }> = {};
  for (const [nm, sku] of SKU) byNm[String(nm)] = { sku };

  const sandbox: Obj = {
    console: { log: () => {}, error: () => {} },
    // Константа берётся из боевого файла, а не копируется: её смена в Apps Script обязана уронить тест.
    WB_STOCKS_SOURCE_API_: bqSrc.match(/var WB_STOCKS_SOURCE_API_\s*=\s*'([^']+)'/)![1],
    SpreadsheetApp: { getActiveSpreadsheet: () => ({}) },
    PropertiesService: { getScriptProperties: () => ({ getProperty: () => 'not-a-real-token' }) },
    Utilities: {
      sleep: () => {},
      getUuid: () => '00000000-0000-0000-0000-000000000000',
      formatDate: (d: Date, _tz: string, f: string) => (f === 'yyyy-MM-dd' ? '2026-10-21' : '20261021_062300'),
    },
    UrlFetchApp: {
      fetch: (url: string) => {
        if (t5Broken && url.includes('/warehouse_remains')) return { getResponseCode: () => 400, getContentText: () => 'down' };
        const body = route(url);
        return { getResponseCode: () => 200, getContentText: () => body };
      },
    },
    buildSkuIndex_: () => ({ byNm, byBarcode: {} }),
    wbStocksBqSinkOn_: () => true,
    wbStocksBqEnsureRaw_: () => {},
    wbStocksBqEnsureManifest_: () => {},
    wbStocksBqEnsureT5_: () => {},
    wbStocksBqManifestStart_: () => {},
    wbStocksBqPrevAvailabilityGap_: () => prevGap,
    wbStocksBqAppendT5_: (rows: Obj[]) => { out.t5 = rows; return rows.length; },
    wbStocksBqAppendRaw_: (rows: Obj[]) => { out.t6 = rows; return rows.length; },
    wbStocksBqSnapshotCounts_: () => ({ count: out.t6.length, distinct: out.t6.length }),
    wbStocksBqManifestFinalize_: (_id: string, status: string, m: Obj) => { out.manifest = { ...m, status }; return 1; },
  };
  vm.createContext(sandbox);
  vm.runInContext(nmFn[0] + '\n' + snapshotSrc, sandbox);
  const r: Obj = { _manifestStarted: false, _manifestFinalized: false };
  (sandbox.wbStocksSnapshotCore_ as (r: Obj) => void)(r);
  out.status = String(r.status);
  return out;
}

// ── B. Cloud Run ──────────────────────────────────────────────────────────────
const cr = vi.hoisted(() => ({
  appends: [] as Array<{ table: string; rows: Record<string, unknown>[] }>,
  finalize: null as null | { status: string; metrics: Record<string, unknown> | null; written: number; control: string; t5?: Record<string, unknown> },
  prevGap: null as number | null,
  urls: [] as string[],
  failT5Append: false,
  t6CountOverride: null as number | null,
}));

vi.mock('../src/secrets.js', () => ({
  SecretsClient: class { async access() { return 'not-a-real-token'; } },
}));
vi.mock('../src/loaders/stocks/bq.js', () => ({
  StocksBq: class {
    async loadSkuIndex() { return new Map<number, string>([[101, 'SKU-A'], [202, 'SKU-B']]); }
    async manifestStart() {}
    async prevAvailabilityGap() { return cr.prevGap; }
    async appendRaw(table: string, rows: Record<string, unknown>[]) {
      if (cr.failT5Append && table.includes('T5')) throw new Error('load job failed');
      cr.appends.push({ table, rows });
      return 'LOADED';
    }
    async countRows(table: string) { return cr.appends.find((a) => a.table === table)?.rows.length ?? 0; }
    async snapshotCounts(table: string) {
      const n = cr.t6CountOverride ?? cr.appends.find((a) => a.table === table)?.rows.length ?? 0;
      return { count: n, distinct: n };
    }
    async manifestFinalize(_t: string, _id: string, status: string, metrics: Record<string, unknown> | null, written: number, control: string, _err: string, t5?: Record<string, unknown>) {
      cr.finalize = { status, metrics, written, control, t5 };
    }
    async manifestMarkReused() { throw new Error('reuse не ожидается'); }
  },
}));

const { stocksLoader } = await import('../src/loaders/stocks/index.js');

async function runCloudRun(
  prevGap: number | null, t5Broken = false, t5Mode: 'on' | 'off' = 'on',
  opts: { failT5Append?: boolean; t6CountOverride?: number } = {},
) {
  cr.appends = [];
  cr.finalize = null;
  cr.prevGap = prevGap;
  cr.failT5Append = opts.failT5Append ?? false;
  cr.t6CountOverride = opts.t6CountOverride ?? null;
  const route = wbRouter();
  const orig = globalThis.fetch;
  cr.urls = [];
  globalThis.fetch = (async (input: RequestInfo | URL) => {
    const url = String(input);
    cr.urls.push(url);
    if (t5Broken && url.includes('/warehouse_remains')) return new Response('down', { status: 400 });
    return new Response(route(url), { status: 200 });
  }) as typeof fetch;
  try {
    const config = loadConfig({
      ENVIRONMENT: 'shadow', GCP_PROJECT_ID: 'p', BQ_RAW_DATASET: 'wb_raw',
      STOCKS_T5_MODE: t5Mode, STOCKS_T5_POLL_INTERVAL_MS: '0', WB_HTTP_TIMEOUT_MS: '1000',
    });
    const ctx: LoaderContext = {
      config, logger: new Logger({}, 'error'), logicalPeriod: '2026-10-21', runId: 'r', targetDate: '2026-10-21',
    };
    await stocksLoader(ctx).catch((e) => { if (!opts.t6CountOverride) throw e; });
  } finally {
    globalThis.fetch = orig;
  }
  const t6 = cr.appends.find((a) => a.table === 'RAW_WB_STOCKS__CR')?.rows ?? [];
  const t5 = cr.appends.find((a) => a.table === 'RAW_WB_STOCKS_T5__CR')?.rows ?? [];
  return { t6, t5, finalize: cr.finalize! };
}

// ── Сравнение ─────────────────────────────────────────────────────────────────
const RUN_IDS = ['load_id', 'snapshot_id', 'snapshot_ts'];
const strip = (rows: Obj[]) => rows.map((r) => Object.fromEntries(Object.entries(r).filter(([k]) => !RUN_IDS.includes(k))));

/** Метрики manifest в именах колонок WB_STOCKS_SNAPSHOTS — как их пишет Cloud Run. */
function crManifest(f: NonNullable<typeof cr.finalize>): Obj {
  const m = f.metrics ?? {};
  return {
    status: f.status,
    expected_rows: m.expected_rows, written_rows: f.written, distinct_keys: m.distinct_keys, duplicate_keys: m.duplicate_keys,
    unique_nm_ids: m.unique_nm_ids, warehouses_count: m.warehouses_count,
    qty_positive_rows: m.qty_positive_rows, qty_zero_rows: m.qty_zero_rows,
    aggregate_warehouse_rows: m.aggregate_warehouse_rows, anonymized_warehouse_rows: f.t5?.anonymizedWarehouseRows,
    sum_quantity_all_t6: m.sum_quantity_all_t6, sum_quantity_physical_t6: m.sum_quantity_physical_t6,
    unmatched_nm_ids: JSON.stringify(m.unmatched_nm_ids),
    control_status: f.control, t5_rows_written: f.t5?.t5RowsWritten,
    ...(f.t5?.control as Obj),
  };
}
/**
 * Метрические колонки WB_STOCKS_SNAPSHOTS (wbStocksManifestFields_, WbStocksBigQuery.gs:95-131)
 * без идентификаторов/времени/периода/ошибки. Список явный: колонка, которую одна из
 * реализаций не заполнила, проявится как null против значения.
 */
const MANIFEST_KEYS = [
  'status', 'expected_rows', 'written_rows', 'distinct_keys', 'duplicate_keys', 'unique_nm_ids',
  'warehouses_count', 'qty_positive_rows', 'qty_zero_rows', 'aggregate_warehouse_rows',
  'anonymized_warehouse_rows', 'sum_quantity_all_t6', 'sum_quantity_physical_t6',
  't5_control_sum', 't5_wb_rf_sum', 't5_named_sum', 't5_rows_written', 'control_status', 'control_delta',
  't6_comparable', 'availability_gap', 'availability_gap_prev', 'availability_gap_delta',
  'availability_ratio', 'availability_status', 'unmatched_nm_ids',
];
const pick = (m: Obj) => Object.fromEntries(MANIFEST_KEYS.map((k) => [k, m[k] ?? null]));
const asManifest = pick;

describe('replay-parity 1:1: Apps Script WbStocksSnapshot.gs ↔ Cloud Run stocksLoader', () => {
  beforeEach(() => { cr.appends = []; });

  for (const prevGap of [null, 1, -200]) {
    it(`T6 + T5 + manifest совпадают (availability_gap_prev=${prevGap})`, async () => {
      const a = runAppsScript(prevGap);
      const b = await runCloudRun(prevGap);
      expect(a.status).toBe('OK');
      expect(b.finalize.status).toBe('COMPLETE');

      expect(strip(b.t6)).toEqual(strip(a.t6));
      expect(strip(b.t5)).toEqual(strip(a.t5));
      expect(b.t5.length).toBe(9);
      expect(pick(crManifest(b.finalize))).toEqual(asManifest(a.manifest!));
    });
  }

  it('T5 недоступен: обе реализации пишут T6 и T5_UNAVAILABLE с тем же manifest', async () => {
    const a = runAppsScript(1, true);
    const b = await runCloudRun(1, true);
    expect(a.status).toBe('OK');
    expect(b.finalize.status).toBe('COMPLETE');
    expect(strip(b.t6)).toEqual(strip(a.t6));
    expect(b.t5).toEqual([]);
    expect(a.t5).toEqual([]);
    expect(pick(crManifest(b.finalize))).toEqual(asManifest(a.manifest!));
    expect(b.finalize.control).toBe('T5_UNAVAILABLE');
  });

  it('эталонные значения фикстуры (не только равенство двух реализаций)', async () => {
    const b = await runCloudRun(1);
    expect(crManifest(b.finalize)).toMatchObject({
      sum_quantity_physical_t6: 14, t6_comparable: 14,
      t5_control_sum: 48, t5_wb_rf_sum: 15, t5_named_sum: 33, t5_rows_written: 9,
      control_status: 'OK', control_delta: 1,
      availability_gap: 1, availability_gap_prev: 1, availability_gap_delta: 0, availability_status: 'OK',
      anonymized_warehouse_rows: 3,
    });
  });

  it('STOCKS_T5_MODE=off: T5 не запрашивается, manifest — прежний контракт (NOT_RUN, без колонок T5)', async () => {
    const b = await runCloudRun(1, false, 'off');
    expect(cr.urls.some((u) => u.includes('/warehouse_remains'))).toBe(false);
    expect(b.t5).toEqual([]);
    expect(b.finalize.t5).toBeUndefined();
    expect(b.finalize.control).toBe('NOT_RUN');
    expect(b.finalize.status).toBe('COMPLETE');
  });

  it('сбой записи детализации T5 не валит снимок: COMPLETE, контроль посчитан, t5_rows_written=0', async () => {
    const b = await runCloudRun(1, false, 'on', { failT5Append: true });
    expect(b.finalize.status).toBe('COMPLETE');
    expect(b.t5).toEqual([]);
    expect(b.finalize.t5).toMatchObject({ t5RowsWritten: 0 });
    expect((b.finalize.t5 as { control: Obj }).control).toMatchObject({ control_status: 'OK', t5_wb_rf_sum: 15 });
  });

  it('ERROR пост-проверки T6 сохраняет посчитанный T5-контроль в manifest (как Apps Script)', async () => {
    const b = await runCloudRun(1, false, 'on', { t6CountOverride: 2 });
    expect(b.finalize.status).toBe('ERROR');
    expect(b.finalize.control).toBe('OK');
    expect((b.finalize.t5 as { control: Obj }).control).toMatchObject({ t5_control_sum: 48, availability_status: 'OK' });
  });

  it('Mig1b-0: prod с пустым env падает ДО любой записи и до WB API', async () => {
    cr.appends = []; cr.finalize = null; cr.urls = [];
    const orig = globalThis.fetch;
    globalThis.fetch = (async (input: RequestInfo | URL) => { cr.urls.push(String(input)); return new Response('[]'); }) as typeof fetch;
    try {
      const config = loadConfig({ ENVIRONMENT: 'prod', GCP_PROJECT_ID: 'p', BQ_RAW_DATASET: 'wb_raw' });
      const ctx: LoaderContext = { config, logger: new Logger({}, 'error'), logicalPeriod: '2026-10-27', runId: 'r', targetDate: '2026-10-27' };
      await expect(stocksLoader(ctx)).rejects.toMatchObject({ code: 'STOCKS_TARGET_MISMATCH' });
    } finally {
      globalThis.fetch = orig;
    }
    expect(cr.urls).toEqual([]);
    expect(cr.appends).toEqual([]);
    expect(cr.finalize).toBeNull();
  });
});

