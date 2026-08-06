import { describe, it, expect } from 'vitest';
import { martLoader, classifyMartError } from '../src/loaders/mart/index.js';
import { MartBq, type QueryRunner, type MartRunRecord } from '../src/loaders/mart/bq.js';
import { LoaderError } from '../src/errors.js';
import type { LoaderContext } from '../src/loaders/types.js';
import type { Config } from '../src/config.js';
import type { Logger } from '../src/logging.js';

const TARGET = '2026-08-02';

const silentLogger = { info() {}, warn() {}, error() {}, debug() {}, child() { return silentLogger; } } as unknown as Logger;
const config = { environment: 'prod', gitSha: 'sha1', imageDigest: 'img1', projectId: 'proj', bqLocation: 'EU' } as unknown as Config;
const ctx = (over: Partial<LoaderContext> = {}): LoaderContext =>
  ({ config, logger: silentLogger, logicalPeriod: TARGET, targetDate: TARGET, runId: 'run-1', ...over });

type Fresh = { loader: string; covers_target: boolean; last_complete_at: string | null };
type Snap = { c: number; d_run: number; d_date: number; wrong_run: number; wrong_date: number; al: boolean | null; abmd: string | null };

interface FakeOpts {
  freshness?: Fresh[];
  snapshot?: Partial<Snap>;
  buildThrows?: string;
  writeFails?: boolean;          // падают ОБЕ записи MART_RUNS
  completeWriteFails?: boolean;  // падает ТОЛЬКО MERGE со status='COMPLETE'; ERROR проходит
}

const OK_FRESH: Fresh[] = [
  { loader: 'orders', covers_target: true, last_complete_at: 'x' },
  { loader: 'sales', covers_target: true, last_complete_at: 'x' },
  { loader: 'ads', covers_target: true, last_complete_at: 'x' },
];
const OK_SNAP: Snap = { c: 100, d_run: 1, d_date: 1, wrong_run: 0, wrong_date: 0, al: false, abmd: TARGET };

class FakeRunner implements QueryRunner {
  readonly projectId = 'proj';
  bootstrapCalled = false;
  buildCalled = false;
  martRuns = new Map<string, Record<string, unknown>>();
  calls: Array<{ sql: string; params?: Record<string, unknown>; types?: Record<string, string> }> = [];
  constructor(private readonly o: FakeOpts = {}) {}
  get inserts(): Array<Record<string, unknown>> { return [...this.martRuns.values()]; }
  async query<T = Record<string, unknown>>(
    sql: string,
    params?: Record<string, unknown>,
    types?: Record<string, string>,
  ): Promise<T[]> {
    this.calls.push({ sql, params, types });
    if (sql.includes('V_INGEST_HEARTBEAT')) return (this.o.freshness ?? OK_FRESH) as unknown as T[];
    if (sql.includes('sp_bootstrap_facts')) { this.bootstrapCalled = true; return [] as T[]; }
    if (sql.includes('sp_build_mart_sku_daily')) {
      this.buildCalled = true;
      if (this.o.buildThrows) throw new Error(this.o.buildThrows);
      return [] as T[];
    }
    if (sql.includes('MART_SKU_DAILY')) {
      return [{ ...OK_SNAP, ...this.o.snapshot }] as unknown as T[];
    }
    if (sql.includes('MERGE') && sql.includes('MART_RUNS')) {
      if (this.o.writeFails) throw new Error('insert failed');
      if (this.o.completeWriteFails && params?.status === 'COMPLETE') throw new Error('merge complete failed');
      const id = String(params?.run_id);
      if (!this.martRuns.has(id)) this.martRuns.set(id, { ...params }); // WHEN NOT MATCHED INSERT
      return [] as T[];
    }
    if (sql.includes('MART_RUNS') && sql.includes('run_id')) { // read-back (status + identity)
      const row = this.martRuns.get(String(params?.run_id));
      return [{
        c: row ? 1 : 0,
        status: row ? row.status : null,
        environment: row ? row.environment : null,
        target_date: row ? row.target_date : null,
        git_sha: row ? row.git_sha : null,
      }] as unknown as T[];
    }
    throw new Error(`unexpected sql: ${sql}`);
  }
}

const run = (o: FakeOpts = {}, over: Partial<LoaderContext> = {}) => {
  const fake = new FakeRunner(o);
  const bq = new MartBq('proj', 'EU', fake);
  return { fake, promise: martLoader(ctx(over), bq) };
};

describe('classifyMartError', () => {
  it('сохраняет код LoaderError', () => expect(classifyMartError(new LoaderError('x', 'FRESHNESS_GATE'))).toBe('FRESHNESS_GATE'));
  it('дефолт MART_ERROR', () => expect(classifyMartError(new Error('boom'))).toBe('MART_ERROR'));
});

describe('DATE-параметр сериализуется как STRING+CAST (hotfix DATE-null)', () => {
  it('ни один вызов не типизирует target_date/ads_activity_max_date как DATE; SQL кастит', async () => {
    const { fake, promise } = run({}); // happy path — проходит freshness→FACT→MART→snapshot→MART_RUNS
    await promise;
    // ни один параметр даты не объявлен типом 'DATE'
    for (const c of fake.calls) {
      for (const [name, t] of Object.entries(c.types ?? {})) {
        expect(t, `параметр ${name} не должен быть DATE`).not.toBe('DATE');
      }
    }
    const withTarget = fake.calls.filter((c) => c.params && 'target_date' in c.params);
    expect(withTarget.length).toBeGreaterThan(0);
    for (const c of withTarget) {
      expect(c.types?.target_date).toBe('STRING');                 // STRING на границе Node↔BQ
      expect(c.sql).toContain('CAST(@target_date AS DATE)');       // каст внутри SQL
    }
    // запись MART_RUNS: nullable ads_activity_max_date тоже STRING+CAST
    const write = fake.calls.find((c) => c.sql.includes('MERGE') && c.sql.includes('MART_RUNS'));
    expect(write?.types?.ads_activity_max_date).toBe('STRING');
    expect(write?.sql).toContain('CAST(@ads_activity_max_date AS DATE)');
  });
});

describe('martLoader — happy / ads (PR-Mart3b-1 REV2)', () => {
  it('gate OK → FACT+MART, одна строка MART_RUNS COMPLETE, rows проброшены', async () => {
    const { fake, promise } = run({ snapshot: { c: 6796 } });
    const res = await promise;
    expect(res).toEqual({ rowsFetched: 6796, rowsLoaded: 6796 });
    expect(fake.bootstrapCalled).toBe(true);
    expect(fake.buildCalled).toBe(true);
    expect(fake.inserts).toHaveLength(1);
    const row = fake.inserts[0]!;
    expect(row.status).toBe('COMPLETE');
    expect(row.run_id).toBe('run-1');
    expect(row.mart_rows).toBe(6796);
    expect(row.ads_activity_lagged).toBe(false);
    expect(row.error_code).toBeNull();
  });

  it('ads activity лаг (ads_activity_lagged=TRUE) — НЕ ошибка, COMPLETE', async () => {
    const { fake, promise } = run({ snapshot: { al: true, abmd: '2026-08-01' } });
    await promise;
    expect(fake.inserts[0]!.status).toBe('COMPLETE');
    expect(fake.inserts[0]!.ads_activity_lagged).toBe(true);
  });
});

describe('martLoader — инвариант / форма freshness (extra аудита)', () => {
  it('logicalPeriod !== targetDate → CTX_INVARIANT, одна строка MART_RUNS ERROR, FACT/MART не вызваны', async () => {
    const { fake, promise } = run({}, { logicalPeriod: '2026-08-01' });
    await expect(promise).rejects.toThrow(/CTX_INVARIANT/);
    expect(fake.bootstrapCalled).toBe(false);
    expect(fake.buildCalled).toBe(false);
    expect(fake.inserts).toHaveLength(1);
    expect(fake.inserts[0]!.status).toBe('ERROR');
    expect(fake.inserts[0]!.error_code).toBe('CTX_INVARIANT');
  });

  it('freshness не ровно orders/sales/ads (нехватка) → FRESHNESS_SHAPE, build не вызван', async () => {
    const { fake, promise } = run({ freshness: [OK_FRESH[0]!, OK_FRESH[1]!] });
    await expect(promise).rejects.toThrow(/FRESHNESS_SHAPE/);
    expect(fake.buildCalled).toBe(false);
    expect(fake.inserts[0]!.status).toBe('ERROR');
  });

  it('freshness с дублем → FRESHNESS_SHAPE', async () => {
    const dup: Fresh[] = [OK_FRESH[0]!, OK_FRESH[0]!, OK_FRESH[2]!];
    const { promise } = run({ freshness: dup });
    await expect(promise).rejects.toThrow(/FRESHNESS_SHAPE/);
  });
});

describe('martLoader — gate/build ошибки', () => {
  it('gate FAIL → ERROR/FRESHNESS_GATE, build не вызван, mart_rows NULL, freshness сохранён', async () => {
    const fresh: Fresh[] = [
      { loader: 'orders', covers_target: true, last_complete_at: 'x' },
      { loader: 'sales', covers_target: false, last_complete_at: null },
      { loader: 'ads', covers_target: true, last_complete_at: 'x' },
    ];
    const { fake, promise } = run({ freshness: fresh });
    await expect(promise).rejects.toThrow(/FRESHNESS_GATE/);
    expect(fake.buildCalled).toBe(false);
    expect(fake.inserts[0]!.status).toBe('ERROR');
    expect(fake.inserts[0]!.error_code).toBe('FRESHNESS_GATE');
    expect(fake.inserts[0]!.mart_rows).toBeNull();
    expect(String(fake.inserts[0]!.freshness_json)).toContain('sales');
  });

  it('ошибка процедуры build → ERROR/MART_ERROR', async () => {
    const { fake, promise } = run({ buildThrows: 'sp_build failed: something' });
    await expect(promise).rejects.toThrow(/something/);
    expect(fake.inserts[0]!.status).toBe('ERROR');
    expect(fake.inserts[0]!.error_code).toBe('MART_ERROR');
  });

  it('сбой записи MART_RUNS НЕ маскирует исходную ошибку', async () => {
    const fresh: Fresh[] = [
      { loader: 'orders', covers_target: false, last_complete_at: null },
      { loader: 'sales', covers_target: true, last_complete_at: 'x' },
      { loader: 'ads', covers_target: true, last_complete_at: 'x' },
    ];
    const { fake, promise } = run({ writeFails: true, freshness: fresh });
    await expect(promise).rejects.toThrow(/FRESHNESS_GATE/);
    await expect(promise).rejects.not.toThrow(/insert failed/);
    expect(fake.inserts).toHaveLength(0);
  });
});

describe('martLoader — честный steps_json (аудит REV4 #2)', () => {
  const stepsOf = (row: Record<string, unknown>): Array<{ step: string; status: string }> =>
    JSON.parse(String(row.steps_json)) as Array<{ step: string; status: string }>;

  it('успех → ровно один freshness_gate=OK', async () => {
    const { fake, promise } = run({});
    await promise;
    const steps = stepsOf(fake.inserts[0]!);
    expect(steps.filter((s) => s.step === 'freshness_gate' && s.status === 'OK')).toHaveLength(1);
  });

  it('FRESHNESS_SHAPE → в steps_json НЕТ freshness_gate=OK, есть failed-step с кодом', async () => {
    const { fake, promise } = run({ freshness: [OK_FRESH[0]!, OK_FRESH[1]!] });
    await expect(promise).rejects.toThrow(/FRESHNESS_SHAPE/);
    const steps = stepsOf(fake.inserts[0]!);
    expect(steps.some((s) => s.step === 'freshness_gate' && s.status === 'OK')).toBe(false);
    expect(steps.some((s) => s.step === 'freshness_gate' && s.status === 'FRESHNESS_SHAPE')).toBe(true);
  });

  it('FRESHNESS_GATE → в steps_json НЕТ freshness_gate=OK, есть failed-step с кодом', async () => {
    const fresh: Fresh[] = [
      { loader: 'orders', covers_target: true, last_complete_at: 'x' },
      { loader: 'sales', covers_target: false, last_complete_at: null },
      { loader: 'ads', covers_target: true, last_complete_at: 'x' },
    ];
    const { fake, promise } = run({ freshness: fresh });
    await expect(promise).rejects.toThrow(/FRESHNESS_GATE/);
    const steps = stepsOf(fake.inserts[0]!);
    expect(steps.some((s) => s.step === 'freshness_gate' && s.status === 'OK')).toBe(false);
    expect(steps.some((s) => s.step === 'freshness_gate' && s.status === 'FRESHNESS_GATE')).toBe(true);
  });

  it('ошибка build → failed-step = sp_build_mart_sku_daily (не общий error), при этом freshness_gate=OK есть', async () => {
    const { fake, promise } = run({ buildThrows: 'boom' });
    await expect(promise).rejects.toThrow(/boom/);
    const steps = stepsOf(fake.inserts[0]!);
    expect(steps.some((s) => s.step === 'freshness_gate' && s.status === 'OK')).toBe(true);
    expect(steps.some((s) => s.step === 'sp_build_mart_sku_daily' && s.status === 'MART_ERROR')).toBe(true);
    expect(steps.some((s) => s.step === 'error')).toBe(false);
  });

  it('сбой записи COMPLETE при успешной записи ERROR → failed-step = mart_runs_complete, mart_snapshot=OK (аудит REV5)', async () => {
    const { fake, promise } = run({ completeWriteFails: true });
    await expect(promise).rejects.toThrow(/merge complete failed/);
    // одна строка — ERROR (COMPLETE не вставился)
    expect(fake.inserts).toHaveLength(1);
    expect(fake.inserts[0]!.status).toBe('ERROR');
    const steps = stepsOf(fake.inserts[0]!);
    // snapshot был успешен и помечен OK; упал именно шаг записи журнала COMPLETE
    expect(steps.some((s) => s.step === 'mart_snapshot' && s.status === 'OK')).toBe(true);
    expect(steps.some((s) => s.step === 'mart_runs_complete' && s.status === 'MART_ERROR')).toBe(true);
    // mart_snapshot с ошибочным статусом отсутствует
    expect(steps.some((s) => s.step === 'mart_snapshot' && s.status !== 'OK')).toBe(false);
  });

  it('успех → mart_snapshot=OK присутствует ровно один раз', async () => {
    const { fake, promise } = run({});
    await promise;
    const steps = stepsOf(fake.inserts[0]!);
    expect(steps.filter((s) => s.step === 'mart_snapshot' && s.status === 'OK')).toHaveLength(1);
  });
});

describe('martLoader — привязка публикации к run_id/target_date (блокер #2)', () => {
  it('пустая витрина → MART_EMPTY', async () => {
    const { promise } = run({ snapshot: { c: 0 } });
    await expect(promise).rejects.toThrow(/MART_EMPTY/);
  });
  it('чужой run_id в строках → MART_RUN_MISMATCH', async () => {
    const { fake, promise } = run({ snapshot: { c: 10, d_run: 2, wrong_run: 3 } });
    await expect(promise).rejects.toThrow(/MART_RUN_MISMATCH/);
    expect(fake.inserts[0]!.error_code).toBe('MART_RUN_MISMATCH');
  });
  it('чужой build_as_of_date → MART_DATE_MISMATCH', async () => {
    const { promise } = run({ snapshot: { c: 10, d_date: 2, wrong_date: 5 } });
    await expect(promise).rejects.toThrow(/MART_DATE_MISMATCH/);
  });
});

describe('MartBq.writeMartRun — идемпотентность / cross-state (блокер #1)', () => {
  const baseRec: MartRunRecord = {
    runId: 'run-x', environment: 'prod', targetDate: TARGET, status: 'COMPLETE',
    startedAtIso: '2026-08-03T00:00:00.000Z', durationMs: 1, freshnessJson: null,
    adsActivityLagged: false, adsActivityMaxDate: TARGET, stepsJson: '[]', martRows: 1,
    gitSha: 'sha1', imageDigest: 'img1', errorCode: null, errorMessage: null,
  };
  it('повторный COMPLETE того же run_id — идемпотентно (1 строка)', async () => {
    const fake = new FakeRunner();
    const bq = new MartBq('proj', 'EU', fake);
    await bq.writeMartRun(baseRec);
    await bq.writeMartRun(baseRec);
    expect(fake.martRuns.size).toBe(1);
  });
  it('ERROR затем COMPLETE того же run_id → MART_RUNS_CONFLICT, ERROR не перезаписан', async () => {
    const fake = new FakeRunner();
    const bq = new MartBq('proj', 'EU', fake);
    await bq.writeMartRun({ ...baseRec, status: 'ERROR', errorCode: 'X', errorMessage: 'm' });
    await expect(bq.writeMartRun({ ...baseRec, status: 'COMPLETE' })).rejects.toThrow(/MART_RUNS_CONFLICT/);
    expect(String(fake.martRuns.get('run-x')!.status)).toBe('ERROR');
  });
  it('тот же run_id + COMPLETE, но другая target_date → MART_RUNS_IDENTITY_CONFLICT', async () => {
    const fake = new FakeRunner();
    const bq = new MartBq('proj', 'EU', fake);
    await bq.writeMartRun(baseRec); // target 2026-08-02
    await expect(bq.writeMartRun({ ...baseRec, targetDate: '2026-08-01' }))
      .rejects.toThrow(/MART_RUNS_IDENTITY_CONFLICT/);
    expect(String(fake.martRuns.get('run-x')!.target_date)).toBe(TARGET);
  });
  it('тот же run_id + COMPLETE, но другое environment → MART_RUNS_IDENTITY_CONFLICT', async () => {
    const fake = new FakeRunner();
    const bq = new MartBq('proj', 'EU', fake);
    await bq.writeMartRun(baseRec); // prod
    await expect(bq.writeMartRun({ ...baseRec, environment: 'shadow' }))
      .rejects.toThrow(/MART_RUNS_IDENTITY_CONFLICT/);
  });
  it('тот же run_id + COMPLETE, но другой git_sha → MART_RUNS_IDENTITY_CONFLICT', async () => {
    const fake = new FakeRunner();
    const bq = new MartBq('proj', 'EU', fake);
    await bq.writeMartRun(baseRec);
    await expect(bq.writeMartRun({ ...baseRec, gitSha: 'sha2' }))
      .rejects.toThrow(/MART_RUNS_IDENTITY_CONFLICT/);
  });
});
