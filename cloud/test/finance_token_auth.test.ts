/**
 * M4 · F5 (общий механизм #423, http/wbToken.ts): срок токена до первого запроса и 401/403 Finance API.
 *   - истёкший токен → WB_FINANCE_TOKEN_EXPIRED, ни одного fetch, журнал FINANCE_LOADER_RUNS не открыт;
 *   - 401/403 → WB_FINANCE_AUTH (DETERMINISTIC), ровно один запрос, без повтора;
 *   - значения токена нет ни в тексте ошибки, ни в логе.
 * Токен — синтетический JWT (подпись — мусор), как в wbToken.test.ts.
 */
import { describe, it, expect } from 'vitest';
import { loadConfig } from '../src/config.js';
import { classifyFailure } from '../src/failure.js';
import { Logger, type LogContext } from '../src/logging.js';
import { WB_TOKEN_EXPIRING_SOON } from '../src/http/wbToken.js';
import { FinanceApi, isRetryableFinanceStatus, WB_FINANCE_AUTH, type FetchFn } from '../src/loaders/finance/wbApi.js';
import { financeLoader, financeBackfillLoader } from '../src/loaders/finance/index.js';
import type { LoaderContext } from '../src/loaders/types.js';
import type { PeerProbe } from '../src/loaders/finance/mutex.js';
import { FakeClock, SynthFinanceApi, MemFinanceStore, synthRow } from './finance_fixture.js';

const T0 = Date.UTC(2026, 10, 10, 4, 30);
const b64url = (o: unknown) => Buffer.from(JSON.stringify(o)).toString('base64url');
const jwt = (payload: unknown) => `${b64url({ alg: 'ES256', typ: 'JWT' })}.${b64url(payload)}.c2lnbmF0dXJlLXN5bnRoZXRpYw`;
const tokenExpIn = (days: number) => jwt({ exp: Math.floor((T0 + days * 86_400_000) / 1000), id: 'synthetic-finance' });
const noPeer: PeerProbe = async () => 0;

/** Логгер, который копит всё, что ушло бы в stdout/stderr. */
class CaptureLogger extends Logger {
  lines: string[] = [];
  override child(): Logger { return this; }
  private push(level: string, message: string, ctx?: LogContext): void { this.lines.push(JSON.stringify({ level, message, ...ctx })); }
  override debug(m: string, c?: LogContext): void { this.push('debug', m, c); }
  override info(m: string, c?: LogContext): void { this.push('info', m, c); }
  override warn(m: string, c?: LogContext): void { this.push('warn', m, c); }
  override error(m: string, c?: LogContext): void { this.push('error', m, c); }
}

function ctxOf(env: Record<string, string>, logger: Logger): LoaderContext {
  const config = loadConfig({ GCP_PROJECT_ID: 'proj-synthetic', BQ_RAW_DATASET: 'wb_raw', ...env });
  return { config, logger, logicalPeriod: '2026-11-10T07', runId: 'shadow:finance:2026-11-10T07:x:y', targetDate: '2026-11-10T07' };
}

/** Ни следа токена: ни целиком, ни полезной нагрузки (её id уникален), ни подписи. Заголовок JWT общий у всех — не секрет. */
function expectNoToken(text: string, token: string): void {
  const [, payload, signature] = token.split('.');
  expect(text).not.toContain(token);
  expect(text).not.toContain(payload!);
  expect(text).not.toContain(signature!);
}

describe('F5 · FinanceApi: 401/403 → WB_FINANCE_AUTH без повтора', () => {
  it('401/403 не повторяемы по isRetryableFinanceStatus', () => {
    expect(isRetryableFinanceStatus(401)).toBe(false);
    expect(isRetryableFinanceStatus(403)).toBe(false);
    expect(isRetryableFinanceStatus(429)).toBe(true);
  });

  for (const status of [401, 403]) {
    it(`${status}: исключение WB_FINANCE_AUTH, DETERMINISTIC, один запрос, токена в тексте нет`, async () => {
      const token = tokenExpIn(60);
      const clock = new FakeClock(T0);
      const wb = new SynthFinanceApi([], clock);
      // WB иногда эхом возвращает заголовок — проверяем, что и такое тело не протаскивает токен в ошибку.
      wb.overrides.push(() => ({ status, body: `{"title":"unauthorized","detail":"token is expired","echo":"${token}"}` }));
      const log = new CaptureLogger();
      const api = new FinanceApi({ host: 'https://finance.example.invalid', token, secretName: 'WB_TOKEN_FINANCE', t0: T0, budgetMs: 1_200_000, timeoutMs: 1000 }, clock, wb.fetch, log);
      let err: unknown;
      try { await api.list('daily', '2026-11-07', '2026-11-10'); } catch (e) { err = e; }
      expect(classifyFailure(err)).toEqual({ code: WB_FINANCE_AUTH, category: 'DETERMINISTIC' });
      expect(wb.calls.length).toBe(1);
      expect(api.requests).toBe(1);
      const msg = (err as Error).message;
      expect(msg).toContain(`HTTP ${status}`);
      expect(msg).toContain('WB_TOKEN_FINANCE');
      expect(msg).toContain(status === 401 ? 'истёк или отозван' : 'нет категории доступа');
      expectNoToken(msg, token);
      expectNoToken(log.lines.join('\n'), token);
    });
  }

  it('detailed 401: тоже исключение, без повтора', async () => {
    const clock = new FakeClock(T0);
    const wb = new SynthFinanceApi([], clock);
    wb.overrides.push(() => ({ status: 401, body: 'unauthorized' }));
    const api = new FinanceApi({ host: 'h', token: tokenExpIn(60), t0: T0, budgetMs: 1_200_000, timeoutMs: 1000 }, clock, wb.fetch);
    await expect(api.detailed('900000000000000501')).rejects.toMatchObject({ code: WB_FINANCE_AUTH });
    expect(wb.calls.length).toBe(1);
  });
});

describe('F5 · financeLoader: срок токена до первого запроса', () => {
  it('истёкший токен → WB_FINANCE_TOKEN_EXPIRED: ни одного fetch, журнал не открыт, токена нет в ошибке и логе', async () => {
    const token = tokenExpIn(-10);
    const clock = new FakeClock(T0);
    const store = new MemFinanceStore(clock);
    let fetches = 0;
    const fetchFn: FetchFn = async () => { fetches++; throw new Error('WB API не должен вызываться'); };
    const log = new CaptureLogger();
    let err: unknown;
    try {
      await financeLoader(ctxOf({ ENVIRONMENT: 'shadow' }, log), { makeStore: () => store, clock, fetchFn, readToken: async () => token, peerProbe: noPeer });
    } catch (e) { err = e; }
    expect(classifyFailure(err)).toEqual({ code: 'WB_FINANCE_TOKEN_EXPIRED', category: 'DETERMINISTIC' });
    expect(fetches).toBe(0);
    expect(store.runs.size).toBe(0);
    expect((err as Error).message).toContain('WB_TOKEN_FINANCE');
    expectNoToken((err as Error).message, token);
    expectNoToken(log.lines.join('\n'), token);
  });

  it('finance-backfill с истёкшим токеном — тот же отказ до запроса', async () => {
    const clock = new FakeClock(T0);
    let fetches = 0;
    await expect(financeBackfillLoader(ctxOf({ ENVIRONMENT: 'shadow', FINANCE_BACKFILL_FROM: '2026-11-02', FINANCE_BACKFILL_TO: '2026-11-08' }, new CaptureLogger()), {
      makeStore: () => new MemFinanceStore(clock), clock, readToken: async () => tokenExpIn(-1), peerProbe: noPeer,
      fetchFn: async () => { fetches++; throw new Error('нет'); },
    })).rejects.toMatchObject({ code: 'WB_FINANCE_TOKEN_EXPIRED' });
    expect(fetches).toBe(0);
  });

  it('скоро истекает → warn WB_TOKEN_EXPIRING_SOON (без значения токена), прогон идёт', async () => {
    const token = tokenExpIn(3);
    const clock = new FakeClock(T0);
    const wb = new SynthFinanceApi([], clock);
    const log = new CaptureLogger();
    const res = await financeLoader(ctxOf({ ENVIRONMENT: 'shadow' }, log), { makeStore: () => new MemFinanceStore(clock), clock, fetchFn: wb.fetch, readToken: async () => token, peerProbe: noPeer });
    expect(res).toEqual({ rowsFetched: 0, rowsLoaded: 0 });
    const warn = log.lines.map((l) => JSON.parse(l) as Record<string, unknown>).find((l) => l.message === 'wb_token_expiring');
    expect(warn).toMatchObject({ level: 'warn', code: WB_TOKEN_EXPIRING_SOON, secret: 'WB_TOKEN_FINANCE', daysLeft: 3 });
    expect(wb.calls.length).toBeGreaterThan(0);
    expectNoToken(log.lines.join('\n'), token);
  });
});

describe('F5 · financeLoader: 401/403 Finance API', () => {
  for (const status of [401, 403]) {
    it(`${status} на list → WB_FINANCE_AUTH (DETERMINISTIC), один запрос, журнал закрыт ERROR, токена нет`, async () => {
      const token = tokenExpIn(60);
      const clock = new FakeClock(T0);
      const store = new MemFinanceStore(clock);
      const wb = new SynthFinanceApi([], clock);
      wb.overrides.push(() => ({ status, body: 'unauthorized' }));
      const log = new CaptureLogger();
      let err: unknown;
      try {
        await financeLoader(ctxOf({ ENVIRONMENT: 'shadow' }, log), { makeStore: () => store, clock, fetchFn: wb.fetch, readToken: async () => token, peerProbe: noPeer });
      } catch (e) { err = e; }
      expect(classifyFailure(err)).toEqual({ code: WB_FINANCE_AUTH, category: 'DETERMINISTIC' });
      expect(wb.calls.length).toBe(1);
      const runs = [...store.runs.values()];
      expect(runs).toHaveLength(1);
      expect(runs[0]).toMatchObject({ status: 'ERROR' });
      expect(runs[0]!.error_message).toContain(`HTTP ${status}`);
      expectNoToken((err as Error).message, token);
      expectNoToken(runs[0]!.error_message ?? '', token);
      expectNoToken(log.lines.join('\n'), token);
    });
  }

  it('401 на detailed → отчёт в ERROR, прогон прерван WB_FINANCE_AUTH, detailed не повторялся и дальше запросов нет', async () => {
    const token = tokenExpIn(60);
    const clock = new FakeClock(T0);
    const store = new MemFinanceStore(clock);
    const rid = '900000000000000502';
    const wb = new SynthFinanceApi([{ reportId: rid, reportType: 1, period: 'daily', dateFrom: '2026-11-09', dateTo: '2026-11-09',
      rows: [synthRow('9302000000000000001', 0, '2026-11-09')] }], clock);
    wb.overrides.push((path) => (path.includes('/detailed/') ? { status: 401, body: 'unauthorized' } : undefined));
    await expect(financeLoader(ctxOf({ ENVIRONMENT: 'shadow' }, new CaptureLogger()), { makeStore: () => store, clock, fetchFn: wb.fetch, readToken: async () => token, peerProbe: noPeer }))
      .rejects.toMatchObject({ code: WB_FINANCE_AUTH });
    const det = wb.calls.filter((c) => c.path.includes('/detailed/'));
    expect(det).toHaveLength(1);
    expect(wb.calls[wb.calls.length - 1]!.path).toContain('/detailed/');
    expect(store.loads.get(rid)?.status).toBe('ERROR');
    expect([...store.runs.values()][0]).toMatchObject({ status: 'ERROR' });
  });
});
