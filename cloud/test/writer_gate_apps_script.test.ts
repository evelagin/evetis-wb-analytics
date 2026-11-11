import { describe, it, expect, vi, afterEach } from 'vitest';
import { readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import vm from 'node:vm';
import { runCli, type CliDeps } from '../src/cli.js';
import { EXIT_OK } from '../src/errors.js';
import type { OwnershipReader } from '../src/ownership/gate.js';
import { PROFILES } from '../src/loaders/cutover/profiles.js';
import { STREAMS } from '../src/ownership/streams.js';

/**
 * Этап C — writer-ownership gate Apps Script (apps-script/ingestion/WriterGate.gs) в node:vm.
 *
 * В песочницу грузятся ВСЕ .gs-файлы ingestion (как один проект Apps Script); сервисы — записывающие
 * заглушки. Проверяется:
 *   • owner ≠ APPS_SCRIPT: ни одна из 10 точек входа не пишет ни в BigQuery, ни в лист, ни в Script Properties;
 *   • owner = APPS_SCRIPT: поведение прежнее — последовательность вызовов сервисов байт-в-байт та же, что у
 *     исходного кода (gate-строки и обёртки удалены), без учёта вызовов самого gate;
 *   • смена поколения/владельца посреди исполнения: ни одна запись после неё не уходит в BigQuery;
 *   • все пути записи (меню, backfill, catch-up, refresh) идут через обёртки — сырых Jobs.insert/query нет;
 *   • одновременные писатели (Apps Script в vm и Cloud Run cli) на одном реестре: пишет максимум один.
 */

const here = dirname(fileURLToPath(import.meta.url));
const dir = join(here, '..', '..', 'apps-script', 'ingestion');
const files = readdirSync(dir).filter((f) => f.endsWith('.gs')).sort();
const source = (f: string) => readFileSync(join(dir, f), 'utf8');

/** Исходный код до этапа C: без строк gate и с прямыми вызовами BigQuery. */
function original(f: string, text: string): string {
  if (f === 'WriterGate.gs') return text;
  return text
    .split('\n').filter((l) => !l.includes('wbWriterGate_(')).join('\n')
    .replace(/wbBqJobsInsert_\(/g, 'BigQuery.Jobs.insert(')
    .replace(/wbBqJobsQuery_\(/g, 'BigQuery.Jobs.query(')
    .replace(/wbBqTablesInsert_\(/g, 'BigQuery.Tables.insert(')
    .replace(/wbBqTablesPatch_\(/g, 'BigQuery.Tables.patch(');
}

const WRITE_SQL = /^\s*(?:--[^\n]*\n\s*)*(INSERT|UPDATE|MERGE|DELETE|CREATE|DROP|ALTER|TRUNCATE|BEGIN|DECLARE)\b/i;

type Owner = 'APPS_SCRIPT' | 'CLOUD_RUN' | 'NONE';
interface Registry { owner: Owner; epoch: number; fail?: boolean; missing?: boolean }

interface World {
  sb: Record<string, unknown>;
  trace: string[];
  writes: string[];
  gateDepth: { n: number };
  props: Map<string, string>;
  ownerReads: number;
}

/**
 * Песочница. registry(stream, readNo) — что вернёт чтение реестра владения (последовательность для
 * сценариев «сменилось посреди исполнения»).
 */
function makeWorld(mode: 'gated' | 'original', registry: (stream: string, n: number) => Registry): World {
  const trace: string[] = [];
  const writes: string[] = [];
  const gateDepth = { n: 0 };
  const w: World = { sb: {}, trace, writes, gateDepth, props: new Map(), ownerReads: 0 };
  const rec = (name: string, isWrite = false, detail = '') => {
    const tagged = gateDepth.n > 0;
    if (!tagged) trace.push(name);
    if (isWrite) writes.push(`${name}${detail ? ' ' + detail : ''}${tagged ? ' [gate]' : ''}`);
  };
  const props = w.props;
  for (const [k, v] of Object.entries({ BQ_PROJECT_ID: 'test-proj', BQ_DATASET: 'wb_raw', BQ_LOCATION: 'EU' })) props.set(k, v);
  const scriptProps = {
    getProperty: (k: string) => { rec('props.get'); return props.has(k) ? props.get(k)! : (/TOKEN/i.test(k) ? 'not-a-real-token' : null); },
    setProperty: (k: string, v: string) => { rec('props.set', true, k); props.set(k, String(v)); return scriptProps; },
    deleteProperty: (k: string) => { rec('props.delete', true, k); props.delete(k); return scriptProps; },
    getProperties: () => { rec('props.getAll'); return Object.fromEntries(props); },
    setProperties: () => { rec('props.setAll', true); return scriptProps; },
    getKeys: () => [...props.keys()],
  };
  const deep = (name: string): unknown => new Proxy(function () { /* заглушка */ }, {
    get: (_t, p) => (p === Symbol.toPrimitive ? () => '' : p === 'length' ? 0 : deep(`${name}.${String(p)}`)),
    apply: () => { rec(name.split('.').slice(0, 2).join('.'), name.startsWith('SpreadsheetApp')); return deep(`${name}()`); },
  });
  const okQuery = { jobComplete: true, rows: [], totalRows: '0', jobReference: { jobId: 'job_q', location: 'EU' }, numDmlAffectedRows: '1' };
  const ownerFor = (req: { queryParameters?: Array<{ parameterValue: { value: string } }> }) => {
    const stream = req.queryParameters?.[0]?.parameterValue.value ?? '';
    const r = registry(stream, ++w.ownerReads);
    if (r.fail) throw new Error('Access Denied: Table wb_ops.INGEST_WRITER_OWNERSHIP');
    return { ...okQuery, rows: r.missing ? [] : [{ f: [{ v: r.owner }, { v: String(r.epoch) }] }] };
  };
  const sb: Record<string, unknown> = {
    console: { log: () => {}, error: () => {}, warn: () => {}, info: () => {} },
    Logger: { log: () => {} },
    LockService: { getScriptLock: () => { rec('LockService.getScriptLock'); return { tryLock: () => { rec('lock.tryLock'); return true; }, waitLock: () => rec('lock.waitLock'), releaseLock: () => rec('lock.releaseLock'), hasLock: () => true }; } },
    PropertiesService: { getScriptProperties: () => scriptProps },
    BigQuery: {
      Jobs: {
        query: (req: { query?: string; queryParameters?: Array<{ parameterValue: { value: string } }> }) => {
          if (String(req.query).includes('INGEST_WRITER_OWNERSHIP')) { rec('bq.owner'); return ownerFor(req); }
          const isWrite = WRITE_SQL.test(String(req.query));
          rec(isWrite ? 'bq.query.write' : 'bq.query', isWrite, String(req.query).slice(0, 60));
          return okQuery;
        },
        insert: (job: { configuration?: { load?: { destinationTable?: { tableId?: string } } } }) => {
          rec('bq.insert', true, job.configuration?.load?.destinationTable?.tableId ?? 'query-job');
          return { jobReference: { jobId: 'job_i', location: 'EU' }, status: { state: 'DONE' } };
        },
        get: () => { rec('bq.jobs.get'); return { status: { state: 'DONE' }, statistics: { query: { numDmlAffectedRows: '1' } } }; },
        getQueryResults: () => { rec('bq.getQueryResults'); return okQuery; },
      },
      Tables: {
        get: () => { rec('bq.tables.get'); return { schema: { fields: [] } }; },
        insert: () => { rec('bq.tables.insert', true); return {}; },
        patch: () => { rec('bq.tables.patch', true); return {}; },
        remove: () => { rec('bq.tables.remove', true); },
      },
      Datasets: { get: () => ({}), insert: () => { rec('bq.datasets.insert', true); return {}; } },
      Tabledata: { insertAll: () => { rec('bq.insertAll', true); return {}; } },
    },
    SpreadsheetApp: deep('SpreadsheetApp'),
    DriveApp: deep('DriveApp'),
    ScriptApp: deep('ScriptApp'),
    Session: deep('Session'),
    UrlFetchApp: { fetch: () => { rec('http', false); return { getResponseCode: () => 401, getContentText: () => '{}', getAllHeaders: () => ({}), getHeaders: () => ({}) }; }, fetchAll: () => [] },
    Utilities: {
      sleep: () => {}, getUuid: () => '00000000-0000-0000-0000-000000000000',
      formatDate: (_d: Date, _tz: string, f: string) => (f.includes('HH') ? '20261116_100000' : '2026-11-16'),
      newBlob: () => ({}), base64Encode: () => '', computeDigest: () => [], DigestAlgorithm: {}, Charset: {},
    },
  };
  vm.createContext(sb);
  for (const f of files) {
    const text = source(f);
    vm.runInContext(mode === 'gated' ? text : original(f, text), sb, { filename: f });
  }
  // Вызовы внутри gate/guard не входят в сравнение «поведение прежнее».
  for (const fn of ['wbWriterGate_', 'wbWriterGuard_']) {
    const orig = sb[fn] as (...a: unknown[]) => unknown;
    sb[fn] = (...a: unknown[]) => { gateDepth.n++; try { return orig(...a); } finally { gateDepth.n--; } };
  }
  // Маркер C0 финансов и watermark'и — валидны: точки входа доходят до записи.
  props.set('WB_FIN_DAILY_C0_MARKER', String(sb.FIN_DAILY_C0_VERSION_ ?? ''));
  if (typeof sb.FIN_DAILY_C0_MARKER_PROP_ === 'string') props.set(sb.FIN_DAILY_C0_MARKER_PROP_, String(sb.FIN_DAILY_C0_VERSION_));
  for (const k of ['WB_ORDERS_BQ_PROP_', 'WB_SALES_BQ_PROP_', 'WB_ADS_BQ_PROP_', 'WB_STOCKS_BQ_SINK_PROP_']) props.set(String(sb[k]), '1');
  props.set(String(sb.WB_ORDERS_WATERMARK_PROP_), '2026-11-16T09:31:05');
  props.set(String(sb.WB_SALES_WATERMARK_PROP_), '2026-11-16T09:22:40');
  w.sb = sb;
  return w;
}

const ENTRIES: Array<[string, string, unknown[]]> = [
  ['runWbStocksSnapshot', 'stocks', []],
  ['runWbOrdersIncremental', 'statistics', []],
  ['runWbSalesIncremental', 'statistics', []],
  ['runWbSalesNightReconcile', 'statistics', []],
  ['runWbAdsDaily', 'ads', [{ triggerUid: 't' }]],
  ['runWbAdsDailyCatchUp', 'ads', []],
  ['loadWbAdsCostsAudit', 'ads', []],
  ['runWbFinanceDaily', 'finance', []],
  ['runWbSuppliesDaily', 'supplies', []],
  ['runRefSync', 'ref', []],
];

function call(w: World, fn: string, args: unknown[]): { value?: unknown; error?: string } {
  try {
    return { value: (w.sb[fn] as (...a: unknown[]) => unknown)(...args) };
  } catch (e) {
    return { error: e instanceof Error ? e.message : String(e) };
  }
}

const shape = (r: { value?: unknown; error?: string }) =>
  JSON.stringify(r, (k, v) => (k === 'duration_ms' || k === 't0' ? 0 : typeof v === 'string' ? v.replace(/\d{8}_\d{6}|\d{4}-\d{2}-\d{2}[T ][\d:.]+Z?|_\d{1,4}\b/g, '#') : v));

afterEach(() => vi.restoreAllMocks());

describe('Apps Script: gate в 10 точках входа', () => {
  it('каждая из 10 точек входа зовёт wbWriterGate_ своего потока — после ScriptLock и до любой записи', () => {
    for (const [fn, stream] of ENTRIES) {
      const w = makeWorld('gated', () => ({ owner: 'CLOUD_RUN', epoch: 3 }));
      const calls: string[] = [];
      const orig = w.sb.wbWriterGate_ as (s: string) => unknown;
      w.sb.wbWriterGate_ = (s: string) => { calls.push(s); return orig(s); };
      call(w, fn, ENTRIES.find((e) => e[0] === fn)![2]);
      expect(calls, fn).toEqual([stream]);
    }
  });

  for (const owner of ['CLOUD_RUN', 'NONE'] as const) {
    it(`owner = ${owner}: ни одной записи в BigQuery, лист и Script Properties; результат SKIPPED_FENCED`, () => {
      for (const [fn, , args] of ENTRIES) {
        const w = makeWorld('gated', () => ({ owner, epoch: 3 }));
        const r = call(w, fn, args);
        expect(w.writes, fn).toEqual([]);
        expect(w.trace.filter((t) => t.startsWith('SpreadsheetApp') || t === 'http'), fn).toEqual([]);
        expect(r.value, fn).toMatchObject({ status: 'SKIPPED_FENCED', owner });
        // Лок взят и отпущен (кроме аудита расходов — у него своего лока нет).
        if (fn !== 'loadWbAdsCostsAudit') expect(w.trace.filter((t) => t.startsWith('lock.')), fn).toEqual(['lock.tryLock', 'lock.releaseLock']);
        // Кроме чтения журнала catch-up'а (до лока, только SELECT) — ни одного запроса в BigQuery мимо gate.
        const bq = w.trace.filter((t) => t.startsWith('bq.'));
        expect(bq, fn).toEqual(fn === 'runWbAdsDailyCatchUp' ? ['bq.query'] : []);
      }
    });
  }

  const unavailable: Array<[string, Registry]> = [
    ['ошибка чтения реестра', { owner: 'APPS_SCRIPT', epoch: 1, fail: true }],
    ['нет строки потока', { owner: 'APPS_SCRIPT', epoch: 1, missing: true }],
  ];
  for (const [name, reg] of unavailable) {
    it(`fail-closed: ${name} → SKIPPED_GATE_UNAVAILABLE, ничего не записано`, () => {
      for (const [fn, , args] of ENTRIES) {
        const w = makeWorld('gated', () => reg);
        const r = call(w, fn, args);
        expect(w.writes, fn).toEqual([]);
        expect(r.value, fn).toMatchObject({ status: 'SKIPPED_GATE_UNAVAILABLE' });
      }
    });
  }

  it('owner = APPS_SCRIPT: поведение прежнее — те же вызовы сервисов и тот же результат, что у исходного кода', () => {
    for (const [fn, , args] of ENTRIES) {
      const before = makeWorld('original', () => ({ owner: 'APPS_SCRIPT', epoch: 1 }));
      const after = makeWorld('gated', () => ({ owner: 'APPS_SCRIPT', epoch: 1 }));
      const r0 = call(before, fn, args);
      const r1 = call(after, fn, args);
      expect(after.trace, fn).toEqual(before.trace);
      expect(shape(r1), fn).toEqual(shape(r0));
      expect(after.writes.filter((x) => !x.endsWith('[gate]')), fn).toEqual(before.writes);
      expect(after.ownerReads, fn).toBeGreaterThanOrEqual(1);
      // Поколение 1 и отметки нет — Script Properties gate не трогает.
      expect(after.writes.filter((x) => x.includes('WB_WRITER_GATE_EPOCH_')), fn).toEqual([]);
    }
  });

  it('точки входа реально доходят до записи в BigQuery (иначе сравнение выше пустое)', () => {
    let total = 0;
    for (const [fn, , args] of ENTRIES) {
      const w = makeWorld('gated', () => ({ owner: 'APPS_SCRIPT', epoch: 1 }));
      call(w, fn, args);
      total += w.writes.filter((x) => x.startsWith('bq.')).length;
    }
    expect(total).toBeGreaterThan(5);
  });
});

describe('Apps Script: перед каждой записью — повторная проверка поколения', () => {
  it('владелец сменился посреди исполнения (после gate) → ни одна запись в BigQuery не уходит', () => {
    for (const [fn, , args] of ENTRIES) {
      // Первое чтение (gate) — Apps Script, все последующие (перед каждой записью) — уже NONE.
      const w = makeWorld('gated', (_s, n) => (n === 1 ? { owner: 'APPS_SCRIPT', epoch: 1 } : { owner: 'NONE', epoch: 2 }));
      call(w, fn, args);
      expect(w.writes.filter((x) => x.startsWith('bq.') && !x.endsWith('[gate]')), fn).toEqual([]);
    }
  });

  it('то же поколение сменилось (откат и возврат: owner снова APPS_SCRIPT, epoch другой) → WRITER_EPOCH_CHANGED, записи нет', () => {
    for (const [fn, , args] of ENTRIES) {
      const w = makeWorld('gated', (_s, n) => ({ owner: 'APPS_SCRIPT', epoch: n === 1 ? 1 : 5 }));
      call(w, fn, args);
      expect(w.writes.filter((x) => x.startsWith('bq.') && !x.endsWith('[gate]')), fn).toEqual([]);
    }
  });

  it('смена между подготовкой load-задания и его отправкой: проверка идёт в момент Jobs.insert', () => {
    let flip = false;
    const w = makeWorld('gated', () => (flip ? { owner: 'CLOUD_RUN', epoch: 3 } : { owner: 'APPS_SCRIPT', epoch: 1 }));
    expect(() => (w.sb.bqLoadRows_ as (t: string, r: unknown[]) => number)('RAW_WB_ORDERS', [{ a: 1 }])).not.toThrow();
    expect(w.writes).toEqual(['bq.insert RAW_WB_ORDERS']);
    flip = true; // поток передан Cloud Run, пока строки готовились
    expect(() => (w.sb.bqLoadRows_ as (t: string, r: unknown[]) => number)('RAW_WB_ORDERS', [{ a: 2 }])).toThrow(/WRITER_NOT_OWNER: поток statistics принадлежит CLOUD_RUN/);
    expect(w.writes).toEqual(['bq.insert RAW_WB_ORDERS']);
  });

  it('каждый низкоуровневый писатель потоков закрыт обёрткой (owner = CLOUD_RUN → исключение, задания нет)', () => {
    const w = makeWorld('gated', () => ({ owner: 'CLOUD_RUN', epoch: 3 }));
    const fq = (t: string) => `\`test-proj.wb_raw.${t}\``;
    const cases: Array<[string, () => unknown]> = [
      ['bqLoadRows_ RAW_WB_FINANCE', () => (w.sb.bqLoadRows_ as (t: string, r: unknown[]) => number)('RAW_WB_FINANCE', [{}])],
      ['bqQuery_ DML рекламы', () => (w.sb.bqQuery_ as (s: string) => unknown)(`DELETE FROM ${fq('RAW_WB_ADV_COSTS')} WHERE 1=1`)],
      ['ingestRunQuery_ INGEST_RUNS', () => (w.sb.ingestRunQuery_ as (s: string, p: unknown[]) => unknown)(`UPDATE ${fq('INGEST_RUNS')} SET status="ERROR" WHERE run_id=@r`, [])],
      ['finDailyQuery_ MERGE', () => (w.sb.finDailyQuery_ as (s: string, p: unknown[]) => unknown)(`MERGE ${fq('FINANCE_REPORT_LOADS')} t USING (SELECT 1) s ON FALSE WHEN NOT MATCHED THEN INSERT ROW`, [])],
      ['refSql_ указатель REF', () => (w.sb.refSql_ as (s: string) => unknown)(`UPDATE ${fq('REF_ACTIVE_VERSION')} SET active_ref_run_id='x' WHERE id='singleton'`)],
      ['wbsExecDml_ поставки', () => (w.sb.wbsExecDml_ as (s: string) => unknown)(`DELETE FROM ${fq('RAW_WB_SUPPLIES_GOODS')} WHERE run_id='x'`)],
      ['wbStocksBqDml_ манифест', () => (w.sb.wbStocksBqDml_ as (s: string) => unknown)(`UPDATE ${fq('WB_STOCKS_SNAPSHOTS')} SET status='ERROR' WHERE snapshot_id='x'`)],
    ];
    for (const [name, fn] of cases) {
      expect(fn, name).toThrow(/WRITER_NOT_OWNER/);
    }
    expect(w.writes.filter((x) => !x.endsWith('[gate]'))).toEqual([]);
    // Чтение и запись в таблицы вне потоков gate не трогает.
    expect(() => (w.sb.bqQuery_ as (s: string) => unknown)(`SELECT COUNT(*) FROM ${fq('RAW_WB_ORDERS')}`)).not.toThrow();
    expect(() => (w.sb.bqLoadRows_ as (t: string, r: unknown[]) => number)('WB_KIZ_MARKING', [{}])).not.toThrow();
  });

  it('пути без точки входа (меню, backfill, catch-up, refresh, импорт) при owner ≠ APPS_SCRIPT не пишут в таблицы потоков', () => {
    const manual: Array<[string, unknown[]]> = [
      ['runWbDailyRefresh', []], ['backfillWbOperationalPilotLast14Days', []], ['backfillWbOperationalPilotLast30Days', []],
      ['importWbOrdersFromApiInternal_', ['2026-11-01', '2026-11-02']], ['importWbSalesReturnsFromApiInternal_', ['2026-11-01', '2026-11-02']],
      ['importWbStocksFromApiInternal_', []], ['importWbFinanceFromApiInternal_', ['2026-11-01', '2026-11-02']],
      ['loadWbAdsRawLast7Days', []], ['loadWbAdsCostsBackfill90', []], ['loadWbAdsCostsBootstrapPass', []], ['loadWbAdsCostsRecheck', []],
      ['loadWbAdsCostsRawOperational', []], ['loadWbAdsCampaignsRaw', ['RUN']], ['loadWbAdsQueryBidsRaw', ['RUN']],
      ['loadWbAdsQueryStatsRaw', ['RUN', '2026-11-01', '2026-11-02']], ['wbFinDailyRunManual', []], ['wbFinDeepBackfillRegister', []],
      ['wbOrdersIncrementalBootstrap', []], ['wbSalesIncrementalBootstrap', []],
    ];
    const streamTables = new Set(STREAMS.flatMap((s) => PROFILES[s].writeTables.exact));
    for (const [fn, args] of manual) {
      expect(typeof makeWorld('gated', () => ({ owner: 'CLOUD_RUN', epoch: 3 })).sb[fn], fn).toBe('function');
      const w = makeWorld('gated', () => ({ owner: 'CLOUD_RUN', epoch: 3 }));
      call(w, fn, args);
      const leaked = w.writes.filter((x) => x.startsWith('bq.') && !x.endsWith('[gate]')
        && ([...streamTables].some((t) => x.includes(t)) || x.includes('RAW_WB_ADV_') || x.includes('query-job')));
      expect(leaked, fn).toEqual([]);
    }
  });

  it('статически: вне WriterGate.gs нет прямых записывающих вызовов BigQuery (кроме модулей вне потоков)', () => {
    const offenders: string[] = [];
    // Вне потоков: Unitka R3 (своё платное хранение), КИЗ (своя таблица), самопроверка WbBigQuery (_selftest).
    const outside = new Set(['WriterGate.gs', 'UnitkaR3Ingest.gs', 'Wbmarkingkizloader.gs']);
    for (const f of files) {
      if (outside.has(f)) continue;
      const t = source(f);
      if (/BigQuery\.Jobs\.(insert|query)\(|BigQuery\.Tabledata\.insertAll\(|BigQuery\.Tables\.(insert|patch)\(/.test(t)) offenders.push(f);
    }
    expect(offenders).toEqual([]);
    expect(source('Wbmarkingkizloader.gs')).not.toMatch(/RAW_WB_(ORDERS|SALES|STOCKS|FINANCE|SUPPLIES|ADV_)/);
    // Unitka R3 пишет только своё платное хранение — ни одной таблицы потоков.
    const r3 = source('UnitkaR3Ingest.gs');
    for (const s of STREAMS) for (const t of PROFILES[s].writeTables.exact) expect(r3, t).not.toContain(t);
    expect(r3).not.toContain('RAW_WB_ADV_');
  });

  it('таблицы потоков в WriterGate.gs совпадают с writeTables координатора', () => {
    const w = makeWorld('gated', () => ({ owner: 'APPS_SCRIPT', epoch: 1 }));
    const map = w.sb.WB_WRITER_STREAM_TABLES_ as Record<string, { exact: string[]; prefixes: string[] }>;
    for (const s of STREAMS) {
      expect({ exact: [...map[s]!.exact], prefixes: [...map[s]!.prefixes] }, s).toEqual({
        exact: [...PROFILES[s].writeTables.exact], prefixes: [...PROFILES[s].writeTables.prefixes],
      });
    }
  });

  it('разбор целей записи в SQL', () => {
    const w = makeWorld('gated', () => ({ owner: 'APPS_SCRIPT', epoch: 1 }));
    const t = w.sb.wbWriterTargetsOfSql_ as (s: string) => Array<{ dataset: string; table: string }>;
    expect(t('SELECT * FROM `p.wb_raw.RAW_WB_ORDERS`')).toEqual([]);
    expect(t('INSERT INTO `p.wb_raw.INGEST_RUNS` (a) VALUES (1)')).toEqual([{ dataset: 'wb_raw', table: 'INGEST_RUNS' }]);
    expect(t('MERGE `p.wb_raw.FINANCE_LOADER_RUNS` t USING (SELECT 1) s ON t.a=s.a')).toEqual([{ dataset: 'wb_raw', table: 'FINANCE_LOADER_RUNS' }]);
    expect(t("SELECT last_update FROM x WHERE note = 'deleted'")).toEqual([]);
    expect(t('CREATE OR REPLACE VIEW `p.wb_raw.V_X` AS SELECT 1')).toEqual([{ dataset: 'wb_raw', table: 'V_X' }]);
    expect(t('DELETE FROM `p.wb_ops.OTHER` WHERE TRUE')).toEqual([{ dataset: 'wb_ops', table: 'OTHER' }]);
  });
});

describe('Apps Script: поколение и watermark заказов/продаж при возврате владения', () => {
  it('поколение 1 и нет отметки — ничего не пишется; поколение сменилось — watermark из таблицы, как читает Cloud Run', () => {
    const w = makeWorld('gated', () => ({ owner: 'APPS_SCRIPT', epoch: 5 }));
    // Таблица (Cloud Run продвинул отметку): заглушка ответа на запрос пересинхронизации.
    const q = (w.sb.BigQuery as { Jobs: { query: (r: { query: string }) => unknown } }).Jobs;
    const base = q.query;
    q.query = (r: { query: string }) => (r.query.includes("'ORD_INC_'") && r.query.includes('AS o')
      ? { jobComplete: true, rows: [{ f: [{ v: '2026-11-20 08:41:00' }, { v: '2026-11-20T08:22:10' }] }] }
      : base(r));
    const res = (w.sb.wbWriterGate_ as (s: string) => { allowed: boolean })('statistics');
    expect(res.allowed).toBe(true);
    expect(w.props.get(String(w.sb.WB_ORDERS_WATERMARK_PROP_))).toBe('2026-11-20T08:41:00');
    expect(w.props.get(String(w.sb.WB_SALES_WATERMARK_PROP_))).toBe('2026-11-20T08:22:10');
    expect(w.props.get('WB_WRITER_GATE_EPOCH_STATISTICS')).toBe('5');
    // Повтор с тем же поколением — без пересинхронизации.
    w.props.set(String(w.sb.WB_ORDERS_WATERMARK_PROP_), 'x');
    (w.sb.wbWriterGate_ as (s: string) => unknown)('statistics');
    expect(w.props.get(String(w.sb.WB_ORDERS_WATERMARK_PROP_))).toBe('x');
  });

  it('пересинхронизация не удалась → fail-closed, отметка поколения не записана', () => {
    const w = makeWorld('gated', () => ({ owner: 'APPS_SCRIPT', epoch: 5 }));
    const r = (w.sb.wbWriterGate_ as (s: string) => { allowed: boolean; result: { status: string } })('statistics');
    expect(r).toMatchObject({ allowed: false, result: { status: 'SKIPPED_GATE_UNAVAILABLE' } });
    expect(w.props.has('WB_WRITER_GATE_EPOCH_STATISTICS')).toBe(false);
  });

  it('прочие потоки при смене поколения только запоминают его', () => {
    const w = makeWorld('gated', () => ({ owner: 'APPS_SCRIPT', epoch: 4 }));
    expect((w.sb.wbWriterGate_ as (s: string) => { allowed: boolean })('ads').allowed).toBe(true);
    expect(w.props.get('WB_WRITER_GATE_EPOCH_ADS')).toBe('4');
  });
});

describe('одновременные писатели на одном реестре (Apps Script в vm + Cloud Run cli)', () => {
  const crDeps = (reg: () => Registry, writes: string[]): CliDeps => ({
    makeStore: () => ({
      acquire: async (p) => { writes.push('cr.lease'); return { acquired: true, runId: p.runId, recovered: false }; },
      finalize: async (_k, _r, patch) => { writes.push(`cr.finalize.${patch.status}`); },
    }),
    runHandler: async () => { writes.push('cr.data'); return { rowsFetched: 1, rowsLoaded: 1 }; },
    nowMs: () => 0,
    sleep: async () => {},
    makeOwnershipReader: (): OwnershipReader => ({ read: async () => { const r = reg(); if (r.fail) throw new Error('недоступен'); return { owner: r.owner, epoch: r.epoch, state: 'X' }; } }),
  });
  const env = { GCP_PROJECT_ID: 'p', BQ_RAW_DATASET: 'wb_raw', LOG_LEVEL: 'error', ENVIRONMENT: 'prod', GIT_SHA: 'sha' };

  for (const owner of ['APPS_SCRIPT', 'CLOUD_RUN', 'NONE'] as const) {
    it(`owner = ${owner}: пишет не больше одного писателя`, async () => {
      vi.spyOn(process.stdout, 'write').mockImplementation(() => true);
      vi.spyOn(process.stderr, 'write').mockImplementation(() => true);
      const reg = () => ({ owner, epoch: 2 });
      const w = makeWorld('gated', reg);
      const crWrites: string[] = [];
      const [asResult, crExit] = await Promise.all([
        Promise.resolve().then(() => call(w, 'runWbStocksSnapshot', [])),
        runCli(['node', 'cli.js', 'stocks'], env, crDeps(reg, crWrites)),
      ]);
      const asWrote = w.writes.some((x) => x.startsWith('bq.') && !x.endsWith('[gate]'));
      const crWrote = crWrites.includes('cr.data');
      expect(asWrote && crWrote).toBe(false);
      expect(asWrote).toBe(owner === 'APPS_SCRIPT');
      expect(crWrote).toBe(owner === 'CLOUD_RUN');
      expect(crExit).toBe(EXIT_OK);
      if (owner !== 'APPS_SCRIPT') expect(asResult.value).toMatchObject({ status: 'SKIPPED_FENCED' });
    });
  }

  it('реестр недоступен: не пишет никто (fail-closed у обоих)', async () => {
    vi.spyOn(process.stdout, 'write').mockImplementation(() => true);
    vi.spyOn(process.stderr, 'write').mockImplementation(() => true);
    const reg = () => ({ owner: 'APPS_SCRIPT' as Owner, epoch: 1, fail: true });
    const w = makeWorld('gated', reg);
    const crWrites: string[] = [];
    call(w, 'runWbStocksSnapshot', []);
    await runCli(['node', 'cli.js', 'stocks'], env, crDeps(reg, crWrites));
    expect(w.writes).toEqual([]);
    expect(crWrites).toEqual([]);
  });

  it('передача владения посреди исполнения Apps Script: Apps Script перестаёт писать до того, как Cloud Run начнёт', async () => {
    vi.spyOn(process.stdout, 'write').mockImplementation(() => true);
    vi.spyOn(process.stderr, 'write').mockImplementation(() => true);
    let state: Registry = { owner: 'APPS_SCRIPT', epoch: 1 };
    const timeline: string[] = [];
    const w = makeWorld('gated', () => state);
    // Каждая попытка записи Apps Script фиксируется в общей временной линии.
    const bq = (w.sb.BigQuery as { Jobs: { insert: (...a: unknown[]) => unknown; query: (...a: unknown[]) => unknown } }).Jobs;
    const ins = bq.insert;
    bq.insert = (...a: unknown[]) => { timeline.push(`AS:${state.epoch}`); return ins(...a); };
    // Gate Apps Script пройден под поколением 1, затем координатор: NONE (2) → CLOUD_RUN (3).
    const gate = (w.sb.wbWriterGate_ as (s: string) => { allowed: boolean })('stocks');
    expect(gate.allowed).toBe(true);
    state = { owner: 'NONE', epoch: 2 };
    state = { owner: 'CLOUD_RUN', epoch: 3 };
    const crWrites: string[] = [];
    await runCli(['node', 'cli.js', 'stocks'], env, crDeps(() => state, crWrites));
    timeline.push(...crWrites.filter((x) => x === 'cr.data').map(() => 'CR:3'));
    expect(() => (w.sb.bqLoadRows_ as (t: string, r: unknown[]) => number)('RAW_WB_STOCKS', [{}])).toThrow(/WRITER_NOT_OWNER/);
    expect(timeline).toEqual(['CR:3']);
  });
});

describe('wbWriterGateStatus — только чтение', () => {
  it('показывает владельца каждого потока и ничего не пишет; недоступный реестр — «НЕ пишет»', () => {
    const w = makeWorld('gated', (s) => (s === 'ads' ? { owner: 'APPS_SCRIPT', epoch: 1, fail: true } : { owner: s === 'stocks' ? 'CLOUD_RUN' : 'APPS_SCRIPT', epoch: 2 }));
    const out = (w.sb.wbWriterGateStatus as () => Record<string, string>)();
    expect(Object.keys(out).sort()).toEqual([...STREAMS].sort());
    expect(out.stocks).toMatch(/CLOUD_RUN \/ epoch 2 — Apps Script НЕ пишет/);
    expect(out.statistics).toMatch(/APPS_SCRIPT \/ epoch 2 — Apps Script пишет/);
    expect(out.ads).toMatch(/НЕДОСТУПНО/);
    expect(w.writes).toEqual([]);
  });
});
