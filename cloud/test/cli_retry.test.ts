import { describe, it, expect, vi, afterEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { runCli, PRE_LOCK_RETRY_DELAY_MS, POST_LOCK_RETRY_DELAY_MS, type CliDeps } from '../src/cli.js';
import { EXIT_OK, EXIT_ERROR, LoaderError } from '../src/errors.js';
import { classifyFailure, fatalRecord, TRANSIENT_INFRA, SHEETS_API_TRANSIENT, FATAL_UNHANDLED } from '../src/failure.js';
import { BqManifestStore, type ManifestStore, type AcquireParams, type FinalizePatch } from '../src/bq/runManifest.js';
import type { BqClient } from '../src/bq/client.js';

/**
 * Инцидент 2026-10-06 (unitka-engine-prod-zbfk7): прогон упал ДО lease на транзиентной ошибке
 * BigQuery, верхний `fatal` был без `code`, алерт (`jsonPayload.code!=""`) молчал, maxRetries=0.
 */

// Текст настоящей ошибки из лога zbfk7.
const BQ_TRANSIENT_TEXT = 'The job encountered an error during execution. Retrying the job may solve the problem.';
const bqTransient = () => Object.assign(new Error(BQ_TRANSIENT_TEXT), { errors: [{ reason: 'jobInternalError' }] });

const env = (o: Record<string, string> = {}): NodeJS.ProcessEnv => ({
  GCP_PROJECT_ID: 'p', BQ_RAW_DATASET: 'wb_raw', LOG_LEVEL: 'info', GIT_SHA: 'sha', ENVIRONMENT: 'prod',
  CLOUD_RUN_EXECUTION: 'exec1', ...o,
});
const argv = (loader: string) => ['node', 'cli.js', loader];

/** Фальшивый журнал: строки по run_id, ERROR не блокирует (как BqManifestStore). */
function fakeStore(opts: { acquireFailures?: unknown[] } = {}) {
  const rows = new Map<string, string>();
  const finals: Array<{ runId: string; patch: FinalizePatch }> = [];
  const acquired: string[] = [];
  const failures = [...(opts.acquireFailures ?? [])];
  const store: ManifestStore = {
    acquire: async (p: AcquireParams) => {
      if (failures.length) throw failures.shift();
      const blocking = [...rows.values()].some((s) => s === 'STARTED' || s === 'COMPLETE');
      if (blocking) return { acquired: false, reason: [...rows.values()].includes('COMPLETE') ? 'COMPLETE' : 'ALREADY_RUNNING' };
      rows.set(p.runId, 'STARTED');
      acquired.push(p.runId);
      return { acquired: true, runId: p.runId, recovered: rows.size > 1 };
    },
    finalize: async (_k, runId, patch) => {
      rows.set(runId, patch.status);
      finals.push({ runId, patch });
    },
  };
  return { store, rows, finals, acquired };
}

function capture() {
  const lines: Array<Record<string, unknown>> = [];
  const grab = (chunk: unknown) => {
    for (const l of String(chunk).split('\n')) if (l.trim()) { try { lines.push(JSON.parse(l)); } catch { /* not json */ } }
    return true;
  };
  vi.spyOn(process.stderr, 'write').mockImplementation(grab as never);
  vi.spyOn(process.stdout, 'write').mockImplementation(grab as never);
  return lines;
}
afterEach(() => vi.restoreAllMocks());

/** Предикат действующего алерта: severity>=ERROR AND jsonPayload.code!="". */
const alertMatches = (r: Record<string, unknown>) => r.severity === 'ERROR' && typeof r.code === 'string' && r.code !== '';

function deps(store: ManifestStore, handler: CliDeps['runHandler']) {
  const sleeps: number[] = [];
  const d: CliDeps = { makeStore: () => store, runHandler: handler, nowMs: () => 0, sleep: async (ms) => { sleeps.push(ms); } };
  return { d, sleeps };
}

describe('classifyFailure / fatalRecord', () => {
  it('транзиентная ошибка BigQuery из zbfk7 → TRANSIENT_INFRA', () => {
    expect(classifyFailure(new Error(BQ_TRANSIENT_TEXT))).toEqual({ code: TRANSIENT_INFRA, category: 'TRANSIENT' });
    expect(classifyFailure(Object.assign(new Error('x'), { errors: [{ reason: 'backendError' }] })).category).toBe('TRANSIENT');
    expect(classifyFailure(Object.assign(new Error('x'), { code: 503 })).category).toBe('TRANSIENT');
    expect(classifyFailure(Object.assign(new Error('x'), { code: 'ECONNRESET' })).category).toBe('TRANSIENT');
  });
  it('Sheets 429/5xx → SHEETS_API_TRANSIENT; прочие SHEETS_API — детерминированные', () => {
    expect(classifyFailure(new LoaderError('Sheets API POST x: Request failed with status code 503', 'SHEETS_API')).code).toBe(SHEETS_API_TRANSIENT);
    expect(classifyFailure(new LoaderError('Sheets API POST x: Request failed with status code 400', 'SHEETS_API')).category).toBe('DETERMINISTIC');
  });
  it('контрактный отказ и неизвестная ошибка — детерминированные', () => {
    expect(classifyFailure(new LoaderError('qa', 'OZON_MODEL_QA_FAILED'))).toEqual({ code: 'OZON_MODEL_QA_FAILED', category: 'DETERMINISTIC' });
    expect(classifyFailure(new Error('Cannot read properties of undefined'))).toEqual({ code: 'LOADER_ERROR', category: 'DETERMINISTIC' });
    expect(classifyFailure(new Error('table has 500 rows')).category).toBe('DETERMINISTIC'); // число в тексте ≠ HTTP-статус
  });
  it('fatal без исходного кода всё равно получает непустой code (алерт его видит)', () => {
    for (const e of [new Error('boom'), 'string thrown', undefined, new Error(BQ_TRANSIENT_TEXT), new LoaderError('m', 'CONFIG_ERROR')]) {
      const r = fatalRecord(e);
      expect(alertMatches(r)).toBe(true);
    }
    expect(fatalRecord(new Error('boom')).code).toBe(FATAL_UNHANDLED);
    expect(fatalRecord(new Error(BQ_TRANSIENT_TEXT)).code).toBe(TRANSIENT_INFRA);
  });
});

describe('runCli: отказ BigQuery ДО lease (сценарий zbfk7)', () => {
  it('один транзиентный отказ acquire → пауза, повтор, прогон выполнен один раз', async () => {
    const lines = capture();
    const f = fakeStore({ acquireFailures: [bqTransient()] });
    let calls = 0;
    const { d, sleeps } = deps(f.store, async () => { calls++; return { rowsFetched: 1, rowsLoaded: 1 }; });
    expect(await runCli(argv('unitka'), env(), d)).toBe(EXIT_OK);
    expect(calls).toBe(1);
    expect(sleeps).toEqual([PRE_LOCK_RETRY_DELAY_MS]);
    expect(lines.some((l) => l.message === 'transient_retry' && l.stage === 'acquire')).toBe(true);
  });

  it('повтор acquire тоже упал → EXIT_ERROR, запись с code (алерт срабатывает), handler не исполнялся', async () => {
    const lines = capture();
    const f = fakeStore({ acquireFailures: [bqTransient(), bqTransient()] });
    let calls = 0;
    const { d } = deps(f.store, async () => { calls++; return { rowsFetched: 0, rowsLoaded: 0 }; });
    expect(await runCli(argv('unitka'), env(), d)).toBe(EXIT_ERROR);
    expect(calls).toBe(0);
    const errs = lines.filter((l) => l.severity === 'ERROR');
    expect(errs.length).toBeGreaterThan(0);
    expect(errs.every(alertMatches)).toBe(true);
    expect(errs[0]).toMatchObject({ code: TRANSIENT_INFRA, stage: 'acquire' });
  });

  it('детерминированный отказ acquire не повторяется', async () => {
    capture();
    const f = fakeStore({ acquireFailures: [new Error('Access Denied: Table LOADER_RUNS')] });
    const { d, sleeps } = deps(f.store, async () => ({ rowsFetched: 0, rowsLoaded: 0 }));
    expect(await runCli(argv('unitka'), env(), d)).toBe(EXIT_ERROR);
    expect(sleeps).toEqual([]);
  });
});

describe('runCli: повтор после lease', () => {
  it('транзиентный отказ handler → ровно один повтор со своим run_id; ERROR первой попытки сохранён', async () => {
    capture();
    const f = fakeStore();
    let calls = 0;
    const { d, sleeps } = deps(f.store, async () => {
      calls++;
      if (calls === 1) throw bqTransient();
      return { rowsFetched: 5, rowsLoaded: 5 };
    });
    expect(await runCli(argv('unitka'), env(), d)).toBe(EXIT_OK);
    expect(calls).toBe(2);
    expect(sleeps).toEqual([POST_LOCK_RETRY_DELAY_MS]);
    expect(f.acquired).toEqual(['prod:unitka:' + f.acquired[0]!.split(':')[2] + ':sha:exec1', expect.stringMatching(/:exec1-r2$/)]);
    expect(f.finals.map((x) => [x.runId.endsWith('-r2'), x.patch.status, x.patch.errorCode ?? null])).toEqual([
      [false, 'ERROR', TRANSIENT_INFRA],
      [true, 'COMPLETE', null],
    ]);
  });

  it('второй транзиентный отказ → стоп, без третьей попытки (не зацикливается)', async () => {
    const lines = capture();
    const f = fakeStore();
    let calls = 0;
    const { d } = deps(f.store, async () => { calls++; throw bqTransient(); });
    expect(await runCli(argv('unitka'), env(), d)).toBe(EXIT_ERROR);
    expect(calls).toBe(2);
    expect(lines.filter((l) => l.severity === 'ERROR').every(alertMatches)).toBe(true);
  });

  it('детерминированный отказ (контракт/QA) НЕ повторяется', async () => {
    capture();
    const f = fakeStore();
    let calls = 0;
    const { d, sleeps } = deps(f.store, async () => { calls++; throw new LoaderError('QA', 'OZON_MODEL_QA_FAILED'); });
    expect(await runCli(argv('ozon-unitka'), env(), d)).toBe(EXIT_ERROR);
    expect(calls).toBe(1);
    expect(sleeps).toEqual([]);
    expect(f.finals[0]!.patch).toMatchObject({ status: 'ERROR', errorCode: 'OZON_MODEL_QA_FAILED' });
  });

  it('загрузчик без retryTransient (stocks) после lease не повторяется', async () => {
    capture();
    const f = fakeStore();
    let calls = 0;
    const { d } = deps(f.store, async () => { calls++; throw bqTransient(); });
    expect(await runCli(argv('stocks'), env(), d)).toBe(EXIT_ERROR);
    expect(calls).toBe(1);
  });

  it('успешный повтор не дублирует запись в лист (писатель строит план как разницу с листом)', async () => {
    capture();
    const f = fakeStore();
    const sheet = new Map<string, number>();
    const wanted = new Map([['A1', 1], ['A2', 2], ['A3', 3]]);
    let writes = 0;
    let calls = 0;
    const { d } = deps(f.store, async () => {
      calls++;
      const plan = [...wanted].filter(([k, v]) => sheet.get(k) !== v);
      for (const [k, v] of plan) { sheet.set(k, v); writes++; }
      if (calls === 1) throw bqTransient(); // отказ ПОСЛЕ записи (например, на чтении для QA)
      return { rowsFetched: plan.length, rowsLoaded: plan.length };
    });
    expect(await runCli(argv('unitka'), env(), d)).toBe(EXIT_OK);
    expect(calls).toBe(2);
    expect(writes).toBe(3); // повтор спланировал 0 ячеек
    expect(f.finals.at(-1)!.patch).toMatchObject({ status: 'COMPLETE', rowsLoaded: 0 });
  });
});

describe('BqManifestStore.acquire: потерянный ответ после COMMIT', () => {
  const params: AcquireParams = {
    environment: 'prod', loaderName: 'unitka', logicalPeriod: '2026-10-06T10', runId: 'prod:unitka:2026-10-06T10:sha:exec1',
    executionId: 'exec1', imageDigest: 'img', gitSha: 'sha', nowMs: Date.parse('2026-10-06T07:00:00Z'), staleMs: 3_600_000,
  };
  const storeReturning = (row: Record<string, unknown>) =>
    new BqManifestStore({ projectId: 'p', query: async () => [row] } as unknown as BqClient, 'wb_raw', 'LOADER_RUNS');

  it('своя строка STARTED (тот же run_id) — это наш lease, не guard_skip', async () => {
    const r = await storeReturning({ active: 1, cur_status: 'STARTED', cur_run_id: params.runId }).acquire(params);
    expect(r).toEqual({ acquired: true, runId: params.runId, recovered: true });
  });
  it('чужая строка STARTED — ALREADY_RUNNING, как раньше', async () => {
    const r = await storeReturning({ active: 1, cur_status: 'STARTED', cur_run_id: 'prod:unitka:2026-10-06T10:sha:other' }).acquire(params);
    expect(r).toEqual({ acquired: false, reason: 'ALREADY_RUNNING' });
  });
  it('COMPLETE своего же run_id — не перезапускаем', async () => {
    const r = await storeReturning({ active: 1, cur_status: 'COMPLETE', cur_run_id: params.runId }).acquire(params);
    expect(r).toEqual({ acquired: false, reason: 'COMPLETE' });
  });
});

describe('Terraform: алерты Юнитки видят отказ без кода приложения', () => {
  const tf = (f: string) => readFileSync(new URL(`../../infra/terraform/${f}`, import.meta.url), 'utf8');
  for (const [file, job] of [['unitka_engine.tf', 'unitka-engine-'], ['ozon_unitka.tf', 'ozon-unitka-prod']] as const) {
    it(`${file}: условие по коду И условие по системному событию «execution failed»`, () => {
      const s = tf(file);
      expect(s).toContain('jsonPayload.code!=""');
      expect(s).toMatch(/cloudaudit\.googleapis\.com%2Fsystem_event/);
      expect(s).toContain('protoPayload.methodName="/Jobs.RunJob"');
      expect(s).toContain(job);
    });
  }
});
