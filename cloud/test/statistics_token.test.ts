/**
 * M2 (F5) — токен statistics-api: срок проверяется ДО первого запроса, отказ авторизации WB —
 * детерминированный WB_STATS_AUTH без повтора, значение токена не попадает ни в ошибку, ни в лог.
 * Механизм общий для потоков M2–M5 (src/http/wbToken.ts); здесь — проводка в orders/sales/sales-reconcile.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { LoaderError } from '../src/errors.js';
import { classifyFailure } from '../src/failure.js';
import { runCli, type CliDeps } from '../src/cli.js';
import { EXIT_ERROR } from '../src/errors.js';
import type { ManifestStore } from '../src/bq/runManifest.js';
import { Logger } from '../src/logging.js';
import { runOrders, runSales, runSalesReconcile, type StatsDeps } from '../src/loaders/statistics/index.js';
import { STATS_HTTP_RETRY_CODES } from '../src/loaders/statistics/constants.js';
import { normalizeOrdersRows, ordersBqRow } from '../src/loaders/statistics/orders.js';
import { normalizeSalesRows, salesBqRow } from '../src/loaders/statistics/sales.js';
import { buildSkuIndex } from '../src/loaders/statistics/common.js';
import { FakeStatsBq, NOW, REF_ROWS, fakeTransport, orderRow, saleRow, statsCtx, statsDeps, wbScript } from './statistics_fixture.js';

const b64url = (o: unknown) => Buffer.from(JSON.stringify(o)).toString('base64url');
/** Синтетический JWT (как в wbToken.test.ts): подпись — мусор, читается только exp. */
const jwt = (payload: unknown) => `${b64url({ alg: 'ES256', typ: 'JWT' })}.${b64url(payload)}.c2lnbmF0dXJlLXN5bnRoZXRpYw`;
const tokenExpIn = (days: number) => jwt({ exp: Math.floor((NOW.getTime() + days * 86_400_000) / 1000), id: 'synthetic-m2' });

const W = '2026-11-09T12:00:00';
const SKU = buildSkuIndex(REF_ROWS);
const existingOrders = () => normalizeOrdersRows([orderRow(1, { lastChangeDate: W })], 'ORD_INC_X', '2026-11-10 03:31:00', SKU).map(ordersBqRow);
const existingSales = () => normalizeSalesRows([saleRow(1, { lastChangeDate: W })], 'SALE_INC_X', '2026-11-10 03:22:00', SKU).map(salesBqRow);

type Run = typeof runOrders;
const RUNS: Array<[string, Run, () => FakeStatsBq]> = [
  ['orders', runOrders, () => new FakeStatsBq(existingOrders())],
  ['sales', runSales, () => new FakeStatsBq(existingSales())],
  ['sales-reconcile', runSalesReconcile, () => new FakeStatsBq(existingSales())],
];

function depsWith(bq: FakeStatsBq, script: ReturnType<typeof wbScript>, token: string): StatsDeps {
  return { ...statsDeps(bq, fakeTransport(script)), getToken: async () => token };
}

async function errorOf(p: Promise<unknown>): Promise<LoaderError> {
  try { await p; } catch (e) { if (e instanceof LoaderError) return e; throw e; }
  throw new Error('ожидался отказ');
}

/** Захват строк логгера (stdout/stderr) без вывода в консоль теста. */
function captureLog(): { lines: string[] } {
  const lines: string[] = [];
  const grab = (chunk: unknown) => { lines.push(String(chunk)); return true; };
  vi.spyOn(process.stdout, 'write').mockImplementation(grab as typeof process.stdout.write);
  vi.spyOn(process.stderr, 'write').mockImplementation(grab as typeof process.stderr.write);
  return { lines };
}

afterEach(() => { vi.restoreAllMocks(); });

describe('F5: срок токена до первого запроса', () => {
  it.each(RUNS)('%s: истёкший токен → WB_STATS_TOKEN_EXPIRED, ни одного fetch, ни чтения BigQuery', async (_n, run, mkBq) => {
    const script = wbScript([{ status: 200, body: [] }]);
    const bq = mkBq();
    let bqCalls = 0;
    const deps = depsWith(bq, script, tokenExpIn(-10));
    deps.bq = new Proxy(bq, {
      get: (t, k) => { const v = Reflect.get(t, k); return typeof v === 'function' ? (...a: unknown[]) => { bqCalls++; return v.apply(t, a); } : v; },
    });
    const err = await errorOf(run(statsCtx(), deps));
    expect(classifyFailure(err)).toEqual({ code: 'WB_STATS_TOKEN_EXPIRED', category: 'DETERMINISTIC' });
    expect(err.message).toContain('WB_TOKEN_STATISTICS');
    expect(script.urls).toHaveLength(0);
    expect(bqCalls).toBe(0);
  });

  it('скоро истекает → warn WB_TOKEN_EXPIRING_SOON с именем секрета, прогон идёт; значения токена в логе нет', async () => {
    const { lines } = captureLog();
    const token = tokenExpIn(3);
    const ctx = { ...statsCtx(), logger: new Logger({}, 'warn') };
    const script = wbScript([{ status: 200, body: [] }]);
    await runSales(ctx, depsWith(new FakeStatsBq(existingSales()), script, token));
    expect(script.urls).toHaveLength(1);
    const warn = lines.map((l) => JSON.parse(l) as Record<string, unknown>).find((r) => r.message === 'wb_token_expiring');
    expect(warn).toMatchObject({ code: 'WB_TOKEN_EXPIRING_SOON', secret: 'WB_TOKEN_STATISTICS', daysLeft: 3 });
    const all = lines.join('');
    expect(all).not.toContain(token);
    expect(all).not.toContain(token.split('.')[1]!);
  });
});

describe('F5: 401/403 от WB → WB_STATS_AUTH, без повтора', () => {
  it('401/403 не входят в коды HTTP-повтора', () => {
    expect(STATS_HTTP_RETRY_CODES.has(401)).toBe(false);
    expect(STATS_HTTP_RETRY_CODES.has(403)).toBe(false);
  });

  it.each(RUNS)('%s: 401 → WB_STATS_AUTH (DETERMINISTIC), ровно один запрос, без пауз', async (_n, run, mkBq) => {
    const token = tokenExpIn(60);
    const sleeps: number[] = [];
    const script = wbScript([{ status: 401, body: { title: 'unauthorized', detail: 'token is expired' } }, { status: 200, body: [] }]);
    const deps = { ...statsDeps(mkBq(), fakeTransport(script, sleeps)), getToken: async () => token };
    const err = await errorOf(run(statsCtx(), deps));
    expect(err.code).toBe('WB_STATS_AUTH');
    expect(classifyFailure(err)).toEqual({ code: 'WB_STATS_AUTH', category: 'DETERMINISTIC' });
    expect(script.urls).toHaveLength(1);
    expect(sleeps).toEqual([]);
    expect(err.message).toContain('HTTP 401');
    expect(err.message).toContain('WB_TOKEN_STATISTICS');
    expect(err.message).toContain('token is expired');
    expect(err.message).not.toContain(token);
  });

  it.each(RUNS)('%s: 403 → WB_STATS_AUTH (подсказка про категорию токена)', async (_n, run, mkBq) => {
    const script = wbScript([{ status: 403, body: { title: 'forbidden' } }]);
    const err = await errorOf(run(statsCtx(), depsWith(mkBq(), script, tokenExpIn(60))));
    expect(classifyFailure(err)).toEqual({ code: 'WB_STATS_AUTH', category: 'DETERMINISTIC' });
    expect(err.message).toContain('нет категории доступа');
    expect(script.urls).toHaveLength(1);
  });

  it('orders: 401 на второй странице — тоже WB_STATS_AUTH, а не ORDERS_PARTIAL', async () => {
    const newer = [orderRow(2, { lastChangeDate: '2026-11-10T01:00:00' })];
    const script = wbScript([{ status: 200, body: newer }, { status: 401, body: 'unauthorized' }]);
    const err = await errorOf(runOrders(statsCtx(), depsWith(new FakeStatsBq(existingOrders()), script, tokenExpIn(60))));
    expect(err.code).toBe('WB_STATS_AUTH');
    expect(script.urls).toHaveLength(2);
  });

  it('прочие не-200 (например 400) — прежний код потока, не AUTH', async () => {
    const script = wbScript([{ status: 400, body: 'bad request' }]);
    const err = await errorOf(runSales(statsCtx(), depsWith(new FakeStatsBq(existingSales()), script, tokenExpIn(60))));
    expect(err.code).toBe('SALES_ERROR');
  });

  it('токен, отражённый WB в теле 401, вырезается из ошибки', async () => {
    const token = tokenExpIn(60);
    const script = wbScript([{ status: 401, body: `invalid token ${token}` }]);
    const err = await errorOf(runSales(statsCtx(), depsWith(new FakeStatsBq(existingSales()), script, token)));
    expect(err.message).not.toContain(token);
    expect(err.message).not.toContain(token.split('.')[1]!);
  });
});

describe('F5: через cli — retryTransient не повторяет AUTH, лог без токена', () => {
  function cliDeps(run: Run, deps: StatsDeps) {
    const finalized: Array<Record<string, unknown>> = [];
    const store: ManifestStore = {
      acquire: async () => ({ acquired: true, runId: 'r', recovered: false }),
      finalize: async (...a: unknown[]) => { finalized.push({ args: a }); },
    } as unknown as ManifestStore;
    const cli: CliDeps = {
      makeStore: () => store,
      runHandler: (_spec, ctx) => run(ctx, deps),
      nowMs: () => NOW.getTime(),
      sleep: async () => {},
      probeWatermark: async () => '2026-11-10T09',
    };
    return { cli, finalized };
  }
  const env = (o: Record<string, string> = {}) => ({
    GCP_PROJECT_ID: 'p', BQ_RAW_DATASET: 'wb_raw', ENVIRONMENT: 'shadow', LOG_LEVEL: 'info', GIT_SHA: 'testsha', CLOUD_RUN_EXECUTION: 'e1', ...o,
  });

  // Запись loader_failed (cli.ts `failed`): поле message контекста перекрывает имя записи — ищем по stage.
  it.each(RUNS)('%s: 401 → EXIT_ERROR, loader_failed code=WB_STATS_AUTH, ровно один запрос в WB', async (name, run, mkBq) => {
    const { lines } = captureLog();
    const token = tokenExpIn(60);
    const script = wbScript([{ status: 401, body: { detail: 'token is expired' } }, { status: 200, body: [] }]);
    const { cli } = cliDeps(run, depsWith(mkBq(), script, token));
    expect(await runCli(['node', 'cli.js', name], env(), cli)).toBe(EXIT_ERROR);
    expect(script.urls).toHaveLength(1);
    const failed = lines.map((l) => JSON.parse(l) as Record<string, unknown>).find((r) => r.severity === 'ERROR' && r.stage === 'handler');
    expect(failed).toMatchObject({ code: 'WB_STATS_AUTH', category: 'DETERMINISTIC' });
    const all = lines.join('');
    expect(all).not.toContain(token);
    expect(all).not.toContain(token.split('.')[1]!);
  });

  it('истёкший токен → loader_failed code=WB_STATS_TOKEN_EXPIRED, ни одного запроса', async () => {
    const { lines } = captureLog();
    const token = tokenExpIn(-1);
    const script = wbScript([{ status: 200, body: [] }]);
    const { cli } = cliDeps(runOrders, depsWith(new FakeStatsBq(existingOrders()), script, token));
    expect(await runCli(['node', 'cli.js', 'orders'], env(), cli)).toBe(EXIT_ERROR);
    expect(script.urls).toHaveLength(0);
    const failed = lines.map((l) => JSON.parse(l) as Record<string, unknown>).find((r) => r.severity === 'ERROR' && r.stage === 'handler');
    expect(failed).toMatchObject({ code: 'WB_STATS_TOKEN_EXPIRED', category: 'DETERMINISTIC' });
    expect(lines.join('')).not.toContain(token.split('.')[1]!);
  });
});
