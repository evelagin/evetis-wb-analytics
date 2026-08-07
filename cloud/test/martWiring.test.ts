import { describe, it, expect } from 'vitest';
import { LOADERS, resolveLoader, availableLoaderNames } from '../src/loaders/registry.js';
import { d1Moscow } from '../src/loaders/mart/targetDate.js';
import { dailyPeriodMoscow } from '../src/period.js';
import { martLoader } from '../src/loaders/mart/index.js';
import type { LoaderContext } from '../src/loaders/types.js';
import type { Config } from '../src/config.js';
import type { MartBq, MartRunRecord } from '../src/loaders/mart/bq.js';
import { Logger, parseLevel } from '../src/logging.js';

/**
 * PR-Mart3b-2 — контракт регистрации mart в generic CLI:
 *   - политика периода = D-1 МСК (НЕ current-day, как у stocks/noop);
 *   - prodOnly=true (витрина публикует production wb_mart);
 *   - loader самозащищается по среде (MART_ENV) даже если вызван напрямую, минуя cli.
 */

const FIXED_NOW = new Date('2026-08-06T09:00:00Z'); // 12:00 МСК того же дня

describe('registry: политика периода загрузчиков', () => {
  it('mart → D-1 МСК (а не текущие сутки)', () => {
    const got = LOADERS.mart.logicalPeriod(FIXED_NOW);
    expect(got).toBe(d1Moscow(FIXED_NOW));
    // критично: D-1, а не сегодня — иначе гейт/публикация ушли бы на неполные сутки
    expect(got).not.toBe(dailyPeriodMoscow(FIXED_NOW));
  });
  it('stocks/noop → текущие сутки МСК', () => {
    expect(LOADERS.stocks.logicalPeriod(FIXED_NOW)).toBe(dailyPeriodMoscow(FIXED_NOW));
    expect(LOADERS.noop.logicalPeriod(FIXED_NOW)).toBe(dailyPeriodMoscow(FIXED_NOW));
  });
});

describe('registry: prodOnly и разрешение имён', () => {
  it('mart prodOnly=true; stocks/noop — нет', () => {
    expect(LOADERS.mart.prodOnly).toBe(true);
    expect(LOADERS.stocks.prodOnly).toBeFalsy();
    expect(LOADERS.noop.prodOnly).toBeFalsy();
  });
  it('resolveLoader находит mart; availableLoaderNames перечисляет все', () => {
    expect(resolveLoader('mart')).toBeDefined();
    expect(resolveLoader('missing')).toBeUndefined();
    const names = availableLoaderNames();
    expect(names).toContain('mart');
    expect(names).toContain('stocks');
    expect(names).toContain('noop');
  });
});

/** Мини-фейк BQ-слоя: env-guard срабатывает ДО любых запросов, кроме writeMartRun в catch. */
function fakeBq(captured: MartRunRecord[]): MartBq {
  return {
    writeMartRun: async (rec: MartRunRecord) => {
      captured.push(rec);
    },
    checkFreshness: async () => [],
    callBootstrapFacts: async () => {},
    callBuildMart: async () => {},
    readMartSnapshot: async () => ({
      martRows: 0, distinctRun: 0, distinctDate: 0, wrongRun: 0, wrongDate: 0,
      adsActivityLagged: null, adsActivityMaxDate: null,
    }),
  } as unknown as MartBq;
}

function ctx(environment: 'shadow' | 'prod'): LoaderContext {
  const config = {
    environment, projectId: 'p', bqLocation: 'EU', gitSha: 'abc123', imageDigest: 'sha256:x',
  } as unknown as Config;
  const logger = new Logger({ loader: 'mart', environment }, parseLevel('error'));
  return { config, logger, logicalPeriod: '2026-08-05', targetDate: '2026-08-05', runId: 'rid-1' };
}

describe('martLoader: fail-closed по среде (MART_ENV)', () => {
  it('shadow → бросает MART_ENV и пишет терминальную строку MART_RUNS ERROR', async () => {
    const captured: MartRunRecord[] = [];
    await expect(martLoader(ctx('shadow'), fakeBq(captured))).rejects.toMatchObject({ code: 'MART_ENV' });
    expect(captured).toHaveLength(1);
    expect(captured[0]).toMatchObject({
      status: 'ERROR', environment: 'shadow', errorCode: 'MART_ENV', runId: 'rid-1', targetDate: '2026-08-05',
    });
  });

  it('prod → env-guard пропускает (падает ПОЗЖЕ, уже на бизнес-шаге, не на MART_ENV)', async () => {
    const captured: MartRunRecord[] = [];
    // freshness=[] → FRESHNESS_SHAPE (не MART_ENV): доказывает, что prod прошёл env-guard.
    await expect(martLoader(ctx('prod'), fakeBq(captured))).rejects.toMatchObject({ code: 'FRESHNESS_SHAPE' });
    expect(captured[0]).toMatchObject({ status: 'ERROR', environment: 'prod', errorCode: 'FRESHNESS_SHAPE' });
  });
});
