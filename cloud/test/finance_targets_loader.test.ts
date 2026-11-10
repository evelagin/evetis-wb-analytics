/**
 * M4 · Контур (shadow → только свои __CR, prod → только боевые + cutover 2026-07-13) проверяется ДО любого
 * I/O; SQL хранилища ссылается только на таблицы контура и не содержит DDL; исход тика → LOADER_RUNS.
 */
import { describe, it, expect } from 'vitest';
import { loadConfig } from '../src/config.js';
import { LoaderError } from '../src/errors.js';
import { assertFinanceTargets, WB_FINANCE_PROD_TARGETS } from '../src/loaders/finance/targets.js';
import type { BigQuery } from '@google-cloud/bigquery';
import { BqFinanceStore, BigQuerySqlRunner, type SqlRunner, type ParamType } from '../src/loaders/finance/bq.js';
import { financeLoader, financeBackfillLoader } from '../src/loaders/finance/index.js';
import { resolveLoader } from '../src/loaders/registry.js';
import type { LoaderContext } from '../src/loaders/types.js';
import { FakeClock, SynthFinanceApi, MemFinanceStore, synthRow, silentLogger } from './finance_fixture.js';

const PROD_ENV = {
  FINANCE_RAW_TABLE: 'RAW_WB_FINANCE', FINANCE_RUNS_TABLE: 'FINANCE_LOADER_RUNS', FINANCE_LOADS_TABLE: 'FINANCE_REPORT_LOADS',
  FINANCE_WEEK_STATUS_TABLE: 'FINANCE_WEEK_STATUS', FINANCE_WEEK_RECON_TABLE: 'FINANCE_WEEK_RECON',
};
const cfg = (env: Record<string, string>) => loadConfig({ GCP_PROJECT_ID: 'proj-synthetic', BQ_RAW_DATASET: 'wb_raw', ...env });

describe('targets: проверка контура', () => {
  it('shadow по умолчанию — свои __CR, cutover 2026-07-13: проходит', () => {
    expect(() => assertFinanceTargets(cfg({ ENVIRONMENT: 'shadow' }))).not.toThrow();
  });
  it('shadow с более поздним понедельником-cutover проходит', () => {
    expect(() => assertFinanceTargets(cfg({ ENVIRONMENT: 'shadow', FINANCE_CUTOVER: '2026-11-02' }))).not.toThrow();
  });
  it('shadow → боевая таблица: отказ', () => {
    expect(() => assertFinanceTargets(cfg({ ENVIRONMENT: 'shadow', FINANCE_RAW_TABLE: 'RAW_WB_FINANCE' }))).toThrow(/FINANCE_RAW_TABLE=RAW_WB_FINANCE/);
  });
  it('shadow → чужая __CR (остатки): отказ', () => {
    expect(() => assertFinanceTargets(cfg({ ENVIRONMENT: 'shadow', FINANCE_RAW_TABLE: 'RAW_WB_STOCKS__CR' }))).toThrow(LoaderError);
  });
  it('prod с пустым env (значения по умолчанию = __CR): отказ', () => {
    try {
      assertFinanceTargets(cfg({ ENVIRONMENT: 'prod' }));
      throw new Error('не бросил');
    } catch (e) {
      expect((e as LoaderError).code).toBe('FINANCE_TARGET_MISMATCH');
    }
  });
  it('prod с боевыми таблицами и cutover 2026-07-13: проходит; иной cutover — отказ', () => {
    expect(() => assertFinanceTargets(cfg({ ENVIRONMENT: 'prod', ...PROD_ENV }))).not.toThrow();
    expect(() => assertFinanceTargets(cfg({ ENVIRONMENT: 'prod', ...PROD_ENV, FINANCE_CUTOVER: '2026-07-20' }))).toThrow(/FINANCE_CUTOVER=2026-07-13/);
  });
  it('cutover не понедельник / не дата: отказ в любом контуре', () => {
    expect(() => assertFinanceTargets(cfg({ ENVIRONMENT: 'shadow', FINANCE_CUTOVER: '2026-11-03' }))).toThrow(/понедельник/);
    expect(() => assertFinanceTargets(cfg({ ENVIRONMENT: 'shadow', FINANCE_CUTOVER: 'x' }))).toThrow(/понедельник/);
  });
  it('config: опечатки в числах не роняют loadConfig (мягкий разбор)', () => {
    const c = cfg({ ENVIRONMENT: 'shadow', FINANCE_BUDGET_MS: 'abc', FINANCE_HTTP_TIMEOUT_MS: '1', FINANCE_TRIGGER_TYPE: 'manual' });
    expect(c.financeBudgetMs).toBe(1_200_000);
    expect(c.financeHttpTimeoutMs).toBe(120_000);
    expect(c.financeTriggerType).toBe('MANUAL');
    expect(c.wbFinanceSecret).toBe('WB_TOKEN_FINANCE');
  });
});

// ── SQL хранилища: только таблицы контура, без DDL, даты строкой ──────────────

interface Rec { sql: string; params?: Record<string, unknown>; types?: Record<string, ParamType> }
class RecordingRunner implements SqlRunner {
  recs: Rec[] = [];
  loads: Array<{ table: string; jobId: string }> = [];
  cols: Record<string, string[] | null> = {};
  async run(sql: string, params?: Record<string, unknown>, types?: Record<string, ParamType>) {
    this.recs.push({ sql, ...(params ? { params } : {}), ...(types ? { types } : {}) });
    return { rows: [{ c: 0, d: 0, n: 0 }], affected: /^\s*MERGE/.test(sql) ? 1 : null };
  }
  async load(table: string, _nd: string, jobId: string) { this.loads.push({ table, jobId }); return 'LOADED' as const; }
  async columns(table: string) { return table in this.cols ? this.cols[table]! : ['report_period', 'report_type', 'run_id', 'raw_json', '_rr_date', 'rrd_id', 'report_id']; }
}

const CR = { raw: 'RAW_WB_FINANCE__CR', runs: 'FINANCE_LOADER_RUNS__CR', loads: 'FINANCE_REPORT_LOADS__CR', weekStatus: 'FINANCE_WEEK_STATUS__CR', weekRecon: 'FINANCE_WEEK_RECON__CR' };
const metrics0 = { list_forpay: 1.5, list_retail: 0, list_delivery: 0, list_storage: 0, list_acceptance: 0, list_deduction: 0, list_penalty: 0 };
const det0 = { det_forpay: 1, det_retail: 0, det_delivery: 0, det_storage: 0, det_acceptance: 0, det_deduction: 0, det_penalty: 0 };

async function exerciseAll(s: BqFinanceStore): Promise<void> {
  await s.assertReady();
  await s.runOpen('R', 'AUTO', '2026-11-10T04:30:00.000Z');
  await s.runClose('R', 'OK', { discovered: 1, loaded: 1, errors: 0, requests: 3 }, '');
  await s.staleRecovery(120);
  await s.ensureWeekRows('2026-07-13', '2026-11-02');
  await s.assertManifestPk('t');
  await s.queue(5);
  await s.discovered();
  await s.finalWeeks();
  await s.hasOpenWeeks();
  await s.manifestByIds(['900000000000000001']);
  await s.mergeDiscovered({ reportId: '900000000000000001', period: 'DAILY', reportType: 1, dateFrom: '2026-11-09', dateTo: '2026-11-09', metrics: metrics0, metricsJson: '{}' }, 'R');
  await s.mergeStarted('900000000000000001', 'R', 5);
  await s.mergeComplete('900000000000000001', 'R', 1, 1, det0);
  await s.mergeError('900000000000000001', 'R', 'x');
  await s.manifestDiag('900000000000000001');
  await s.orphanPruned('900000000000000001', 'R', '2026-11-09', '2026-11-09');
  await s.orphanFull('900000000000000001', '2026-11-09', '2026-11-09');
  await s.loadRaw([{ report_id: '1' }], 'fin_1_R_0');
  await s.postLoadThisRun('900000000000000001', 'R');
  await s.postLoadCrossRun('900000000000000001', '2026-11-09', '2026-11-09');
  await s.finalizeCandidates('2026-07-13');
  await s.dailySums(1, '2026-11-02');
  await s.mergeRecon('2026-11-02', 1, [{ metric: 'forPaySum', sum_daily: 1, sum_weekly: 2 }], 0.01);
  await s.markWeekFinal('2026-11-02', 1, '900000000000000002', 7);
}

describe('BqFinanceStore: SQL', () => {
  it('shadow: каждая ссылка на таблицу — свой __CR; DDL нет; изменения только MERGE; load — только в __CR', async () => {
    const r = new RecordingRunner();
    await exerciseAll(new BqFinanceStore(r, 'proj-synthetic', 'wb_raw', CR));
    const allowed = new Set(Object.values(CR).map((t) => `\`proj-synthetic.wb_raw.${t}\``));
    for (const rec of r.recs) {
      const refs = rec.sql.match(/`[^`]+`/g) ?? [];
      expect(refs.length).toBeGreaterThan(0);
      for (const ref of refs) expect(allowed.has(ref)).toBe(true);
      expect(/\b(CREATE|DROP|ALTER|TRUNCATE|DELETE|INSERT\s+INTO)\b/i.test(rec.sql)).toBe(false);
      if (!/^\s*SELECT/.test(rec.sql)) expect(rec.sql.trimStart().startsWith('MERGE')).toBe(true);
    }
    expect(r.loads.map((l) => l.table).sort()).toEqual(['FINANCE_LOADER_RUNS__CR', 'RAW_WB_FINANCE__CR']);
  });

  it('даты и NUMERIC идут строкой с CAST в SQL (Node-клиент теряет DATE-строку как NULL)', async () => {
    const r = new RecordingRunner();
    await exerciseAll(new BqFinanceStore(r, 'proj-synthetic', 'wb_raw', CR));
    for (const rec of r.recs) {
      for (const [k, v] of Object.entries(rec.params ?? {})) {
        if (typeof v === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(v)) {
          expect(rec.types?.[k]).toBe('STRING');
          expect(rec.sql).toContain(`CAST(@${k} AS DATE)`);
        }
      }
    }
    const disc = r.recs.find((x) => x.sql.includes("'DISCOVERED', @run_id"))!;
    expect(disc.params!.m0).toBe('1.5');
    expect(disc.sql).toContain('CAST(@m0 AS NUMERIC)');
  });

  it('assertReady: нет таблицы или нет колонок контура → FINANCE_SCHEMA_NOT_READY', async () => {
    const r = new RecordingRunner();
    r.cols['FINANCE_WEEK_RECON__CR'] = null;
    await expect(new BqFinanceStore(r, 'p', 'wb_raw', CR).assertReady()).rejects.toMatchObject({ code: 'FINANCE_SCHEMA_NOT_READY' });
    const r2 = new RecordingRunner();
    r2.cols['RAW_WB_FINANCE__CR'] = ['report_id', 'rrd_id', 'raw_json'];
    await expect(new BqFinanceStore(r2, 'p', 'wb_raw', CR).assertReady()).rejects.toThrow(/report_period, report_type, run_id/);
  });

  it('захват STARTED — только при affected=1', async () => {
    const r = new RecordingRunner();
    r.run = async () => ({ rows: [], affected: 0 });
    expect(await new BqFinanceStore(r, 'p', 'wb_raw', CR).mergeStarted('1', 'R', 5)).toBe(false);
  });

  it('prod-таблицы — те же имена, что у Apps Script', () => {
    expect(WB_FINANCE_PROD_TARGETS).toEqual({
      raw: 'RAW_WB_FINANCE', runs: 'FINANCE_LOADER_RUNS', loads: 'FINANCE_REPORT_LOADS', weekStatus: 'FINANCE_WEEK_STATUS', weekRecon: 'FINANCE_WEEK_RECON',
    });
  });
});

// ── Загрузчик целиком: порядок fail-closed и исход для LOADER_RUNS ─────────────

const T0 = Date.UTC(2026, 10, 10, 4, 30);
function ctxOf(env: Record<string, string>): LoaderContext {
  const config = cfg(env);
  return { config, logger: silentLogger, logicalPeriod: '2026-11-10T07', runId: 'shadow:finance:2026-11-10T07:x:y', targetDate: '2026-11-10T07' };
}

describe('financeLoader', () => {
  it('зарегистрирован как finance / finance-backfill, не prodOnly (есть shadow)', () => {
    expect(resolveLoader('finance')?.prodOnly).toBeFalsy();
    expect(resolveLoader('finance-backfill')).toBeDefined();
    expect(resolveLoader('finance')!.logicalPeriod(new Date(T0))).toBe('2026-11-10T07');
  });

  it('prod с пустым env: отказ ДО хранилища и ДО чтения токена', async () => {
    let touched = false;
    await expect(financeLoader(ctxOf({ ENVIRONMENT: 'prod' }), {
      makeStore: () => { touched = true; throw new Error('store'); },
      readToken: async () => { touched = true; return 't'; },
    })).rejects.toMatchObject({ code: 'FINANCE_TARGET_MISMATCH' });
    expect(touched).toBe(false);
  });

  it('схема не готова: отказ ДО токена и WB API', async () => {
    const clock = new FakeClock(T0);
    const store = new MemFinanceStore(clock);
    store.ready = false;
    let tokenRead = false;
    await expect(financeLoader(ctxOf({ ENVIRONMENT: 'shadow' }), {
      makeStore: () => store, clock, readToken: async () => { tokenRead = true; return 't'; },
    })).rejects.toThrow('not ready');
    expect(tokenRead).toBe(false);
  });

  it('OK → результат со строками; ERROR тика → LoaderError FINANCE_RUN_ERROR', async () => {
    const clock = new FakeClock(T0);
    const store = new MemFinanceStore(clock);
    const wb = new SynthFinanceApi([{ reportId: '900000000000000401', reportType: 1, period: 'daily', dateFrom: '2026-11-09', dateTo: '2026-11-09',
      rows: [synthRow('9301000000000000001', 0, '2026-11-09'), synthRow('9301000000000000002', 1, '2026-11-09')] }], clock);
    const deps = { makeStore: () => store, clock, fetchFn: wb.fetch, readToken: async () => 'synthetic' };
    const res = await financeLoader(ctxOf({ ENVIRONMENT: 'shadow' }), deps);
    expect(res).toEqual({ rowsFetched: 2, rowsLoaded: 2 });
    wb.overrides.push(() => ({ status: 401, body: 'unauthorized' }));
    await expect(financeLoader(ctxOf({ ENVIRONMENT: 'shadow' }), deps)).rejects.toMatchObject({ code: 'FINANCE_RUN_ERROR' });
  });

  it('DRY_RUN=1: контур и схема проверены, токен не читается, записей нет', async () => {
    const clock = new FakeClock(T0);
    const store = new MemFinanceStore(clock);
    let tokenRead = false;
    const res = await financeLoader(ctxOf({ ENVIRONMENT: 'shadow', DRY_RUN: '1' }), { makeStore: () => store, clock, readToken: async () => { tokenRead = true; return 't'; } });
    expect(res).toEqual({ rowsFetched: 0, rowsLoaded: 0 });
    expect(tokenRead).toBe(false);
    expect(store.runs.size).toBe(0);
  });

  it('пустой токен → FINANCE_TOKEN_MISSING', async () => {
    const clock = new FakeClock(T0);
    await expect(financeLoader(ctxOf({ ENVIRONMENT: 'shadow' }), { makeStore: () => new MemFinanceStore(clock), clock, readToken: async () => ' ' }))
      .rejects.toMatchObject({ code: 'FINANCE_TOKEN_MISSING' });
  });

  it('finance-backfill: без периода — отказ до любого I/O', async () => {
    await expect(financeBackfillLoader(ctxOf({ ENVIRONMENT: 'shadow' }), { makeStore: () => { throw new Error('io'); } }))
      .rejects.toMatchObject({ code: 'FINANCE_BACKFILL_PERIOD' });
  });
});

// ── BigQuerySqlRunner: exactly-once load-job и счётчик DML (фейковый клиент) ──
function fakeBq(o: { loadErr?: unknown; job?: unknown | 'NOT_FOUND'; affected?: string }) {
  const calls = { load: 0, jobGet: 0 };
  const bq = {
    createQueryJob: async () => [{ getQueryResults: async () => [[{ x: 1 }]], getMetadata: async () => [{ statistics: { query: o.affected === undefined ? {} : { numDmlAffectedRows: o.affected } } }] }],
    dataset: () => ({ table: () => ({
      load: async () => { calls.load++; if (o.loadErr) throw o.loadErr; return [{}]; },
      getMetadata: async () => [{ schema: { fields: [{ name: 'a' }] } }],
    }) }),
    job: () => ({ get: async () => { calls.jobGet++; if (o.job === 'NOT_FOUND') throw Object.assign(new Error('Not found: Job'), { code: 404 }); return [{ metadata: { status: o.job } }]; } }),
  };
  return { bq: bq as unknown as BigQuery, calls };
}

describe('BigQuerySqlRunner', () => {
  it('409 Already Exists + job DONE без ошибки → ALREADY_LOADED (второго append нет)', async () => {
    const f = fakeBq({ loadErr: Object.assign(new Error('Already Exists: Job'), { code: 409 }), job: { state: 'DONE' } });
    expect(await new BigQuerySqlRunner('p', 'wb_raw', 'EU', f.bq).load('T', '{}', 'fin_1_R_0')).toBe('ALREADY_LOADED');
    expect(f.calls.load).toBe(1);
  });
  it('409 + job с ошибкой → исключение (jobId «сожжён»)', async () => {
    const f = fakeBq({ loadErr: Object.assign(new Error('Already Exists'), { code: 409 }), job: { state: 'DONE', errorResult: { message: 'bad' } } });
    await expect(new BigQuerySqlRunner('p', 'wb_raw', 'EU', f.bq).load('T', '{}', 'j')).rejects.toThrow(/не успешен: bad/);
  });
  it('ответ потерялся, а job принят и DONE → LOADED без повторного insert', async () => {
    const f = fakeBq({ loadErr: new Error('socket hang up'), job: { state: 'DONE' } });
    expect(await new BigQuerySqlRunner('p', 'wb_raw', 'EU', f.bq).load('T', '{}', 'j')).toBe('LOADED');
    expect(f.calls.load).toBe(1);
  });
  it('numDmlAffectedRows → affected; SELECT → null', async () => {
    expect((await new BigQuerySqlRunner('p', 'wb_raw', 'EU', fakeBq({ affected: '1' }).bq).run('MERGE x')).affected).toBe(1);
    expect((await new BigQuerySqlRunner('p', 'wb_raw', 'EU', fakeBq({}).bq).run('SELECT 1')).affected).toBeNull();
  });
  it('columns: нет таблицы → null', async () => {
    const bq = { dataset: () => ({ table: () => ({ getMetadata: async () => { throw Object.assign(new Error('Not found: Table'), { code: 404 }); } }) }) };
    expect(await new BigQuerySqlRunner('p', 'wb_raw', 'EU', bq as unknown as BigQuery).columns('X')).toBeNull();
  });
});
