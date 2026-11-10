/**
 * M3 — реклама WB: общие защиты токена (F5, cloud/src/http/wbToken.ts) во всех трёх режимах
 * (`ads`, `ads-query`, `ads-costs-audit`) и разбор DRY_RUN ровно как cli.ts (F3).
 * Токен — синтетический JWT (подпись — мусор), как в wbToken.test.ts. Сети нет.
 */
import { describe, it, expect } from 'vitest';
import { makeAdsLoader, makeAdsQueryLoader, makeAdsCostsAuditLoader } from '../src/loaders/ads/index.js';
import { classifyFailure } from '../src/failure.js';
import type { Logger } from '../src/logging.js';
import type { LoaderContext, LoaderResult } from '../src/loaders/types.js';
import { harness, ctxFor, type Harness } from './ads_harness.js';
import type { WbRoute } from './ads_fixture.js';

const LP = '2026-11-09';
const b64url = (o: unknown) => Buffer.from(JSON.stringify(o)).toString('base64url');
const jwt = (payload: unknown) => `${b64url({ alg: 'ES256', typ: 'JWT' })}.${b64url(payload)}.c2lnbmF0dXJlLXN5bnRoZXRpYw`;
const expInDays = (days: number) => jwt({ exp: Math.floor((Date.now() + days * 86_400_000) / 1000), id: 'synthetic-ads' });

type Mode = 'ads' | 'ads-query' | 'ads-costs-audit';
const MODES: Mode[] = ['ads', 'ads-query', 'ads-costs-audit'];
const make = (m: Mode, h: Harness): ((ctx: LoaderContext) => Promise<LoaderResult>) =>
  m === 'ads' ? makeAdsLoader(h.rt) : m === 'ads-query' ? makeAdsQueryLoader(h.rt) : makeAdsCostsAuditLoader(h.rt);

/** Логгер, который пишет всё в массив строк — чтобы проверить, что значения токена в логе нет. */
function captureLogger(): { logger: Logger; lines: string[] } {
  const lines: string[] = [];
  const rec = (level: string) => (message: string, ctx?: unknown) => { lines.push(JSON.stringify({ level, message, ...(ctx as object) })); };
  const logger = { debug: rec('debug'), info: rec('info'), warn: rec('warn'), error: rec('error'), child() { return logger; } } as unknown as Logger;
  return { logger, lines };
}

/** WB отвечает status на любой запрос; тело — как у WB, и эхом содержит токен (проверка вырезания). */
const authRoute = (status: number, token: string, seenAuth: string[]): WbRoute => () => {
  seenAuth.push('called');
  return { status, body: JSON.stringify({ title: 'unauthorized', detail: `token is expired: Bearer ${token}`, echo: token }) };
};

async function failure(p: Promise<unknown>): Promise<Error & { code?: string }> {
  try { await p; } catch (e) { return e as Error & { code?: string }; }
  throw new Error('ожидался отказ');
}

describe('ads F5: срок токена до первого запроса', () => {
  for (const m of MODES) {
    it(`${m}: истёкший токен → WB_ADS_TOKEN_EXPIRED, ни одного fetch, значения токена нет`, async () => {
      const tok = expInDays(-3);
      const h = harness(LP);
      h.rt.getToken = async () => { h.tokenCalls++; return tok; };
      const { logger, lines } = captureLogger();
      const ctx = { ...ctxFor('shadow', LP), logger };
      const err = await failure(make(m, h)(ctx));
      expect(err.code).toBe('WB_ADS_TOKEN_EXPIRED');
      expect(classifyFailure(err)).toEqual({ code: 'WB_ADS_TOKEN_EXPIRED', category: 'DETERMINISTIC' });
      expect(h.tokenCalls).toBe(1);
      expect(h.calls).toEqual([]);
      expect(h.sink.order).toEqual([]);
      expect(err.message).toContain('WB_TOKEN_ADS');
      for (const part of tok.split('.')) {
        expect(err.message).not.toContain(part);
        expect(lines.join('\n')).not.toContain(part);
      }
    });
  }

  it('пустой секрет → WB_ADS_AUTH до WB (а не ADS_TOKEN_UNAVAILABLE)', async () => {
    const h = harness(LP);
    h.rt.getToken = async () => '  \n';
    await expect(makeAdsLoader(h.rt)(ctxFor('shadow', LP))).rejects.toMatchObject({ code: 'WB_ADS_AUTH' });
    expect(h.calls).toEqual([]);
  });

  it('токен истекает через 5 суток → предупреждение WB_TOKEN_EXPIRING_SOON, прогон идёт', async () => {
    const tok = expInDays(5.5);
    const h = harness(LP);
    h.rt.getToken = async () => tok;
    const { logger, lines } = captureLogger();
    await makeAdsLoader(h.rt)({ ...ctxFor('shadow', LP), logger });
    expect(lines.some((l) => l.includes('"code":"WB_TOKEN_EXPIRING_SOON"') && l.includes('"secret":"WB_TOKEN_ADS"'))).toBe(true);
    expect(h.calls.length).toBeGreaterThan(0);
    for (const part of tok.split('.')) expect(lines.join('\n')).not.toContain(part);
  });
});

describe('ads F5: 401/403 → WB_ADS_AUTH, без повторов', () => {
  for (const status of [401, 403]) {
    for (const m of MODES) {
      it(`${m}: HTTP ${status} → WB_ADS_AUTH (DETERMINISTIC), ровно один запрос, токена нет в ошибке и логе`, async () => {
        const tok = expInDays(60);
        const seen: string[] = [];
        const h = harness(LP, {}, {}, undefined, authRoute(status, tok, seen));
        h.rt.getToken = async () => tok;
        const { logger, lines } = captureLogger();
        const err = await failure(make(m, h)({ ...ctxFor('shadow', LP), logger }));
        expect(err.code).toBe('WB_ADS_AUTH');
        expect(classifyFailure(err)).toEqual({ code: 'WB_ADS_AUTH', category: 'DETERMINISTIC' });
        expect(seen).toHaveLength(1);
        expect(h.calls).toHaveLength(1);
        expect(h.sleeps).toEqual([]);
        expect(err.message).toContain(`HTTP ${status}`);
        expect(err.message).toContain('WB_TOKEN_ADS');
        const log = lines.join('\n');
        for (const part of tok.split('.')) {
          expect(err.message).not.toContain(part);
          expect(log).not.toContain(part);
        }
      });
    }
  }

  it('ads: 401 на втором источнике (upd) тоже прерывает прогон — не ADS_PARTIAL', async () => {
    const tok = expInDays(60);
    const h = harness(LP, {}, {}, undefined, (_m, u) =>
      new URL(u).pathname === '/adv/v1/upd'
        ? { status: 401, body: '{"detail":"token is expired"}' }
        : { status: 200, body: '{"adverts":[],"all":0}' });
    h.rt.getToken = async () => tok;
    await expect(makeAdsLoader(h.rt)(ctxFor('shadow', LP))).rejects.toMatchObject({ code: 'WB_ADS_AUTH' });
    expect(h.calls.filter((c) => c === 'GET /adv/v1/upd')).toHaveLength(1);
    expect(h.calls).not.toContain('GET /adv/v3/fullstats'); // следующий источник не начат
  });
});

describe('ads F3: DRY_RUN — ровно === \'1\', как cli.ts', () => {
  for (const m of MODES) {
    it(`${m}: DRY_RUN=1 — ни токена, ни WB, ни записи`, async () => {
      const h = harness(LP, { DRY_RUN: '1' });
      await make(m, h)(ctxFor('shadow', LP));
      expect([h.tokenCalls, h.calls.length, h.sink.order.length]).toEqual([0, 0, 0]);
    });
    for (const v of [' 1', '1 ', 'true', 'yes', '0', '']) {
      it(`${m}: DRY_RUN=${JSON.stringify(v)} — НЕ сухой прогон (cli.ts тоже исполнил бы его по-настоящему)`, async () => {
        const h = harness(LP, { DRY_RUN: v });
        await make(m, h)(ctxFor('shadow', LP));
        expect(h.tokenCalls).toBe(1);
        expect(h.calls.length).toBeGreaterThan(0);
      });
    }
  }
});
