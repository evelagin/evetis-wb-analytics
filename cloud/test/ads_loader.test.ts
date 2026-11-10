/**
 * M3 — реклама WB в Cloud Run: офлайн-тесты на синтетике.
 * Нормализация, окна и граница суток, проверка контура, 429/5xx, пустой ответ, commit-marker расходов,
 * изоляция Ads-3/Ads-2, аудит, идемпотентный load-job, регистрация в реестре.
 */
import { describe, it, expect } from 'vitest';
import type { JobLoadMetadata } from '@google-cloud/bigquery';
import { readFileSync } from 'node:fs';
import { makeAdsLoader, makeAdsCostsAuditLoader, adsRunId } from '../src/loaders/ads/index.js';
import { loadAdsConfig, assertAdsTargets, ADS_PROD_TABLES } from '../src/loaders/ads/targets.js';
import { AdsHttp, retryAfterMs, retryPauseMs } from '../src/loaders/ads/http.js';
import { AdsBq, adsLoadJobId, type BqLike } from '../src/loaders/ads/bq.js';
import { overallStatus, isStale } from '../src/loaders/ads/status.js';
import {
  toBqRow, rawJson, collectNmIds, statsIndexFromCount, orderFullstatsIds, costRow, costsRunLogRow, campaignRow,
} from '../src/loaders/ads/normalize.js';
import { COSTS_COLUMNS } from '../src/loaders/ads/constants.js';
import { mskNow, mskCompact, fullstatsRange, rangeBack, splitPeriod, dayCount, daysOf, addDays } from '../src/loaders/ads/time.js';
import { d1Moscow } from '../src/loaders/mart/targetDate.js';
import { resolveLoader } from '../src/loaders/registry.js';
import { harness, ctxFor, fetchFromRoute, PROD_ENV } from './ads_harness.js';
import { COUNT } from './ads_fixture.js';

const LP = '2026-11-09';

// ─────────────────────────────────────────────────────────────────────────
describe('ads: время и окна', () => {
  it('load_ts — МСК без зоны, как wbAdsNow_', () => {
    const d = new Date('2026-11-09T21:05:07Z'); // 00:05:07 МСК 10.11
    expect(mskNow(d)).toBe('2026-11-10 00:05:07');
    expect(mskCompact(d)).toBe('20261110_000507');
  });

  it('граница суток МСК: logical_period = D−1 меняется ровно в 00:00 МСК', () => {
    expect(d1Moscow(new Date('2026-11-09T20:59:59Z'))).toBe('2026-11-08');
    expect(d1Moscow(new Date('2026-11-09T21:00:00Z'))).toBe('2026-11-09');
  });

  it('fullstats D−7…D−1, расходы D−14…D−1, аудит D−46…D−8', () => {
    expect(fullstatsRange(LP)).toEqual({ from: '2026-11-03', to: '2026-11-09' });
    expect(rangeBack(addDays(LP, 1), 14, 1)).toEqual({ from: '2026-10-27', to: '2026-11-09' });
    expect(rangeBack('2026-11-10', 46, 8)).toEqual({ from: '2026-09-25', to: '2026-11-02' });
  });

  it('splitPeriod: смежные окна ≤ maxDays без перекрытий; dayCount включительно', () => {
    expect(splitPeriod('2026-09-25', '2026-11-02', 30)).toEqual([
      { from: '2026-09-25', to: '2026-10-24' }, { from: '2026-10-25', to: '2026-11-02' },
    ]);
    expect(splitPeriod('2026-10-27', '2026-11-09', 31)).toEqual([{ from: '2026-10-27', to: '2026-11-09' }]);
    expect(splitPeriod('x', 'y', 31)).toEqual([{ from: 'x', to: 'y' }]);
    expect(dayCount('2026-10-27', '2026-11-09')).toBe(14);
    expect(daysOf('2026-11-03', '2026-11-09')).toHaveLength(7);
    expect(() => daysOf('2026-11-09', '2026-11-03')).toThrow();
  });
});

// ─────────────────────────────────────────────────────────────────────────
describe('ads: нормализация', () => {
  it('toBqRow: пустое/null/undefined → NULL (колонки нет), числа → строки, чужая колонка — ошибка', () => {
    expect(toBqRow({ a: '', b: null, c: undefined, d: 0, e: 1.25, f: 'x', g: false }, ['a', 'b', 'c', 'd', 'e', 'f', 'g']))
      .toEqual({ d: '0', e: '1.25', f: 'x', g: 'false' });
    expect(() => toBqRow({ zzz: 1 }, ['a'])).toThrow(/zzz/);
  });

  it('rawJson обрезается до 45 000 символов', () => {
    expect(rawJson({ s: 'x'.repeat(50_000) })).toHaveLength(45_000);
    expect(rawJson(undefined)).toBe('');
  });

  it('collectNmIds: обход как в эталоне, без нулей и повторов', () => {
    expect(collectNmIds({ nm_settings: [{ nm_id: 2 }, { nmId: 1 }, { nm: 2 }, { nested: { nmID: 3, nm: '0' } }] })).toEqual([2, 1, 3]);
  });

  it('statsIndexFromCount: кратность сохраняется, статус — более приоритетный; порядок 9→11→7', () => {
    const c = statsIndexFromCount(COUNT);
    expect(c.statsAdvertIds).toEqual([1001, 1002, 1003, 1004, 1001]);
    expect(c.statusById[1001]).toBe(9);
    expect(orderFullstatsIds(c.statsAdvertIds, c.statusById)).toEqual([1001, 1001, 1002, 1003, 1004]);
  });

  it('campaignRow без деталей → count_only', () => {
    const r = toBqRow(campaignRow('R', 'T', 7, { type: 8, status: 9, changeTime: 'ct' }, null), Object.keys(campaignRow('R', 'T', 7, {}, null)));
    expect(r.processed_status).toBe('count_only');
    expect(r.type).toBe('8');
    expect(r.change_time).toBe('ct');
    expect(r.nm_count).toBe('0');
    expect(r.nm_ids).toBeUndefined();
  });

  it('costRow/costsRunLogRow: updDate из updTime, window_index, «хвост» вне окна считается', () => {
    const win = { from: '2026-10-27', to: '2026-11-09' };
    const rows = [
      costRow('R', 'T', { updTime: '2026-11-09T10:00:00+03:00', updSum: 1, updNum: 0, advertId: 1 }, win.from, win.to, 0),
      costRow('R', 'T', { updTime: '2026-11-10T01:00:00+03:00', updSum: 2, advertId: 1 }, win.from, win.to, 0),
    ];
    expect(toBqRow(rows[0]!, COSTS_COLUMNS)).toMatchObject({ updDate: '2026-11-09', updNum: '0', window_index: '0' });
    const log = costsRunLogRow({ rid: 'R', windowIndex: 0, win, httpCode: 200, httpOk: true, rows, durationMs: 5, status: 'OK',
      errorMessage: '', completedAt: 'T', requestedDays: 14 });
    expect(log).toMatchObject({ http_success: 'true', returned_rows: '2', returned_days: '2', rows_out_of_window: '1',
      returned_min_date: '2026-11-09', returned_max_date: '2026-11-10', requested_days: '14' });
  });
});

// ─────────────────────────────────────────────────────────────────────────
describe('ads: итоговый статус (wbAdsSelfTestOverallStatus)', () => {
  const r = (status: string) => ({ source: 's', status, rows: 0 });
  const cases: Array<[unknown, boolean | null, string]> = [
    [[r('OK'), r('OK'), r('OK')], false, 'OK'],
    [[r('OK'), r('OK'), r('OK')], true, 'STALE'],
    [[r('OK'), r('OK'), r('OK')], null, 'OK'],
    [[r('OK'), r('PARTIAL'), r('OK')], false, 'PARTIAL'],
    [[r('OK'), r('PARTIAL'), r('OK')], true, 'PARTIAL'],
    [[r('OK'), r('ERROR'), r('OK')], true, 'ERROR'],
    [[r('OK'), null, r('OK')], false, 'ERROR'],
    [[r('OK'), {}, r('OK')], false, 'ERROR'],
    [[r('OK'), { source: 's' }, r('OK')], false, 'ERROR'],
    [[r('OK'), r('WAT'), r('OK')], false, 'ERROR'],
    [[r('OK'), r('OK')], false, 'ERROR'],
    [[r('OK'), r('OK'), r('OK'), r('OK')], false, 'ERROR'],
    [null, false, 'ERROR'],
    [[r('ok'), r('partial'), r('OK')], false, 'PARTIAL'],
    [[r('OK'), r('SKIPPED'), r('OK')], false, 'ERROR'],
  ];
  it.each(cases)('%j stale=%s → %s', (inp, stale, exp) => expect(overallStatus(inp, stale)).toBe(exp));

  it('isStale: > 2 суток от сегодняшней МСК; пусто → stale', () => {
    expect(isStale('2026-11-10', '2026-11-08', 2)).toBe(false);
    expect(isStale('2026-11-10', '2026-11-07', 2)).toBe(true);
    expect(isStale('2026-11-10', '', 2)).toBe(true);
  });
});

// ─────────────────────────────────────────────────────────────────────────
describe('ads: HTTP — повторы как wbFetchWithRetry_', () => {
  const seq = (statuses: Array<number | 'throw'>, headers: Record<string, string> = {}) => {
    let i = 0;
    const sleeps: number[] = [];
    const http = new AdsHttp('tok', {
      timeoutMs: 1000, sleep: async (ms) => { sleeps.push(ms); }, nowMs: () => 0,
      fetchFn: fetchFromRoute(() => {
        const s = statuses[Math.min(i++, statuses.length - 1)]!;
        if (s === 'throw') throw new Error('ECONNRESET');
        return { status: s, body: '{"ok":1}', headers };
      }),
    });
    return { http, sleeps, attempts: () => i };
  };

  it('429 → пауза Retry-After, затем успех', async () => {
    const t = seq([429, 200], { 'Retry-After': '7' });
    const r = await t.http.request('get', 'https://x/adv/v1/upd?from=a');
    expect(r.ok).toBe(true);
    expect(t.sleeps).toEqual([7000]);
  });

  it('5xx: 20 c, 40 c, 60 c, затем ПОСЛЕДНИЙ ответ без исключения (4 попытки)', async () => {
    const t = seq([503, 503, 503, 503, 200]);
    const r = await t.http.request('get', 'https://x/y');
    expect(r).toMatchObject({ ok: false, code: 503 });
    expect(t.sleeps).toEqual([20_000, 40_000, 60_000]);
    expect(t.attempts()).toBe(4);
  });

  it('400 и 501 не повторяются', async () => {
    for (const s of [400, 501]) {
      const t = seq([s, 200]);
      expect((await t.http.request('get', 'https://x/y')).code).toBe(s);
      expect(t.sleeps).toEqual([]);
    }
  });

  it('сетевое исключение после всех повторов — бросается (источник даст FAILED)', async () => {
    const t = seq(['throw']);
    await expect(t.http.request('get', 'https://x/y')).rejects.toThrow(/после 4 попыток/);
  });

  it('Retry-After: секунды, HTTP-дата, мусор; пауза ≤ 60 c', () => {
    expect(retryAfterMs('3', 0)).toBe(3000);
    expect(retryAfterMs(new Date(10_000).toUTCString(), 0)).toBe(10_000);
    expect(retryAfterMs('soon', 0)).toBe(-1);
    expect(retryPauseMs(1, 600_000, 20_000, 60_000)).toBe(60_000);
  });

  it('токен — только в заголовке Authorization', async () => {
    let seen: Record<string, string> = {};
    let seenUrl = '';
    const http = new AdsHttp('secret-value', { timeoutMs: 1000, fetchFn: async (url, init) => {
      seen = init.headers; seenUrl = url;
      return { status: 200, headers: { get: () => null }, text: async () => '[]' };
    } });
    await http.request('post', 'https://x/adv/v0/normquery/stats', { a: 1 });
    expect(seen.Authorization).toBe('secret-value');
    expect(seenUrl).not.toContain('secret-value');
  });
});

// ─────────────────────────────────────────────────────────────────────────
describe('ads: проверка контура', () => {
  it('shadow по умолчанию пишет только в __CR', () => {
    const c = loadAdsConfig({});
    expect(Object.values(c.tables).every((t) => t.endsWith('__CR'))).toBe(true);
    expect(() => assertAdsTargets('shadow', c, Object.keys(ADS_PROD_TABLES) as never)).not.toThrow();
  });

  it('shadow с боевой таблицей — отказ', () => {
    const c = loadAdsConfig({ ADS_COSTS_TABLE: 'RAW_WB_ADV_COSTS' });
    expect(() => assertAdsTargets('shadow', c, ['costs'])).toThrow(/ADS_COSTS_TABLE=RAW_WB_ADV_COSTS/);
  });

  it('prod без явных боевых имён (пустой env → __CR) — отказ; с боевыми — да', () => {
    expect(() => assertAdsTargets('prod', loadAdsConfig({}), ['costs'])).toThrow(/ожидалось RAW_WB_ADV_COSTS/);
    expect(() => assertAdsTargets('prod', loadAdsConfig(PROD_ENV), Object.keys(ADS_PROD_TABLES) as never)).not.toThrow();
  });

  it('запись во вью невозможна', () => {
    const c = loadAdsConfig({ ...PROD_ENV, ADS_COSTS_TABLE: 'V_ADV_COSTS' });
    expect(() => assertAdsTargets('prod', c, ['costs'])).toThrow();
  });

  it('кривой конфиг — ошибка, а не молчаливый дефолт', () => {
    expect(() => loadAdsConfig({ ADS_COSTS_AUDIT_ENABLED: 'yes' })).toThrow(/ADS_COSTS_AUDIT_ENABLED/);
    expect(() => loadAdsConfig({ ADS_FULLSTATS_BUDGET_MS: '5' })).toThrow(/ADS_FULLSTATS_BUDGET_MS/);
  });

  it('нарушение контура — до токена и до первого вызова WB', async () => {
    const h = harness(LP, { ADS_COSTS_TABLE: 'RAW_WB_ADV_COSTS' });
    await expect(makeAdsLoader(h.rt)(ctxFor('shadow', LP))).rejects.toMatchObject({ code: 'ADS_TARGET_MISMATCH' });
    expect(h.tokenCalls).toBe(0);
    expect(h.calls).toEqual([]);
    expect(h.sink.order).toEqual([]);
  });

  it('wbAdsBqCreateViews не воспроизводится: в коде загрузчика нет DDL и записи во вью', () => {
    for (const f of ['index', 'sources', 'bq', 'normalize', 'targets', 'http', 'status', 'time', 'constants']) {
      const src = readFileSync(new URL(`../src/loaders/ads/${f}.ts`, import.meta.url), 'utf8');
      const code = src.split('\n').filter((l) => !/^\s*(\*|\/\/|\/\*)/.test(l)).join('\n');
      expect(code, f).not.toMatch(/CREATE\s+(OR\s+REPLACE\s+)?(VIEW|TABLE)|\bALTER\s+TABLE|\bDELETE\s+FROM|\bMERGE\b|\bUPDATE\s+`|\bINSERT\s+INTO|\bDROP\s+/i);
    }
  });
});

// ─────────────────────────────────────────────────────────────────────────
describe('ads: суточный прогон', () => {
  it('штатный день: COMPLETE, run_id ADSRAW_…_CR, окна, порядок данные → маркер', async () => {
    const h = harness(LP);
    const res = await makeAdsLoader(h.rt)(ctxFor('shadow', LP));
    expect(res.rowsLoaded).toBeGreaterThan(0);
    const costs = h.sink.rows('RAW_WB_ADV_COSTS__CR');
    const runs = h.sink.rows('RAW_WB_ADV_COSTS_RUNS__CR');
    expect(costs[0]!.run_id).toMatch(/^ADSRAW_\d{8}_\d{6}_CRab12c$/);
    expect(costs.every((r) => r.period_from === '2026-10-27' && r.period_to === '2026-11-09' && r.window_index === '0')).toBe(true);
    expect(costs[0]!.load_ts).toMatch(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/);
    expect(runs).toHaveLength(1);
    expect(runs[0]).toMatchObject({ status: 'OK', http_success: 'true', returned_rows: '3', rows_out_of_window: '1', requested_days: '14' });
    expect(h.sink.order.indexOf('RAW_WB_ADV_COSTS__CR')).toBeLessThan(h.sink.order.indexOf('RAW_WB_ADV_COSTS_RUNS__CR'));
    const stats = h.sink.rows('RAW_WB_ADV_CAMPAIGN_STATS__CR');
    expect(stats.every((r) => r.period_from === '2026-11-03' && r.period_to === '2026-11-09')).toBe(true);
    // Ads-2 — ровно семь суток D−7…D−1, по строке run-log на сутки, от свежих к старым.
    expect(h.sink.rows('RAW_WB_ADV_QUERY_STATS_RUNS__CR').map((r) => r.period_from))
      .toEqual(['2026-11-09', '2026-11-08', '2026-11-07', '2026-11-06', '2026-11-05', '2026-11-04', '2026-11-03']);
    // ни одного write-вызова в WB: только чтение
    expect(h.calls.every((c) => /^(GET \/adv\/v1\/promotion\/count|GET \/api\/advert\/v2\/adverts|GET \/adv\/v3\/fullstats|GET \/adv\/v1\/upd|POST \/adv\/v0\/normquery\/(get-bids|stats))$/.test(c))).toBe(true);
  });

  it('пустой ответ upd: окно успешно, маркер с нулём строк (а не «не спрашивали»)', async () => {
    const h = harness(LP, {}, { updEmpty: true });
    await makeAdsLoader(h.rt)(ctxFor('shadow', LP));
    expect(h.sink.rows('RAW_WB_ADV_COSTS__CR')).toEqual([]);
    expect(h.sink.rows('RAW_WB_ADV_COSTS_RUNS__CR')[0]).toMatchObject({ status: 'OK', http_success: 'true', returned_rows: '0' });
  });

  it('429 на upd → повтор по Retry-After, итог OK', async () => {
    const h = harness(LP, {}, { upd429: 2 });
    await makeAdsLoader(h.rt)(ctxFor('shadow', LP));
    expect(h.sleeps.filter((s) => s === 1000)).toHaveLength(2);
    expect(h.sink.rows('RAW_WB_ADV_COSTS_RUNS__CR')[0]!.status).toBe('OK');
  });

  it('upd 5xx до конца → маркер ERROR, http_success=false; итог ADS_PARTIAL', async () => {
    const h = harness(LP, {}, { upd429: 99 });
    await expect(makeAdsLoader(h.rt)(ctxFor('shadow', LP))).rejects.toMatchObject({ code: 'ADS_PARTIAL' });
    expect(h.sink.rows('RAW_WB_ADV_COSTS__CR')).toEqual([]);
    expect(h.sink.rows('RAW_WB_ADV_COSTS_RUNS__CR')[0]).toMatchObject({ status: 'ERROR', http_success: 'false', http_status: '429' });
  });

  it('маркер расходов не записался → данные есть, но прогон PARTIAL (окно не станет каноническим)', async () => {
    const h = harness(LP);
    h.sink.failOn.add('RAW_WB_ADV_COSTS_RUNS__CR');
    await expect(makeAdsLoader(h.rt)(ctxFor('shadow', LP))).rejects.toMatchObject({ code: 'ADS_PARTIAL' });
    expect(h.sink.rows('RAW_WB_ADV_COSTS__CR')).toHaveLength(3);
  });

  it('fullstats 500 → дробление до single, PARTIAL; Ads-3/Ads-2 всё равно отработали', async () => {
    const h = harness(LP, {}, { fullstatsFailId: 1004 });
    await expect(makeAdsLoader(h.rt)(ctxFor('shadow', LP))).rejects.toMatchObject({ code: 'ADS_PARTIAL' });
    // пачка (4 попытки HTTP-уровня) + 4 удачных single + упавший single 1004 (4 попытки) = 12, как в эталоне
    expect(h.calls.filter((c) => c === 'GET /adv/v3/fullstats')).toHaveLength(12);
    expect(h.sink.rows('RAW_WB_ADV_QUERY_BIDS_RUNS__CR')).toHaveLength(1);
    expect(h.sink.rows('RAW_WB_ADV_QUERY_STATS_RUNS__CR')).toHaveLength(7);
  });

  it('сбой Ads-3 и Ads-2 не меняет итог: COMPLETE', async () => {
    const h = harness(LP, {}, { bidsFail: true, qstatsDropPack: true });
    await expect(makeAdsLoader(h.rt)(ctxFor('shadow', LP))).resolves.toBeTruthy();
    expect(h.sink.rows('RAW_WB_ADV_QUERY_BIDS_RUNS__CR')[0]).toMatchObject({ status: 'FAILED', http_success: 'FALSE' });
    expect(h.sink.rows('RAW_WB_ADV_QUERY_STATS_RUNS__CR')[0]).toMatchObject({ period_from: LP, status: 'PARTIAL' });
  });

  it('Ads-2: недоступный scope → FAILED по каждым суткам; данные не пишутся', async () => {
    const h = harness(LP);
    h.reader.scopeFail = true;
    await makeAdsLoader(h.rt)(ctxFor('shadow', LP));
    const runs = h.sink.rows('RAW_WB_ADV_QUERY_STATS_RUNS__CR');
    expect(runs.every((r) => r.status === 'FAILED' && /Scope не получен/.test(r.error_message ?? ''))).toBe(true);
    expect(h.sink.rows('RAW_WB_ADV_QUERY_STATS__CR')).toEqual([]);
  });

  it('устаревший fullstats → STALE = успех; недоступная свежесть → OK', async () => {
    const h1 = harness(LP);
    h1.reader.maxDate = '2026-10-01';
    await expect(makeAdsLoader(h1.rt)(ctxFor('shadow', LP))).resolves.toBeTruthy();
    const h2 = harness(LP);
    h2.reader.maxDate = new Error('bq down');
    await expect(makeAdsLoader(h2.rt)(ctxFor('shadow', LP))).resolves.toBeTruthy();
  });

  it('count 500 → кампании FAILED, fullstats SKIPPED → ADS_ERROR', async () => {
    const h = harness(LP, {}, { countFail: true });
    await expect(makeAdsLoader(h.rt)(ctxFor('shadow', LP))).rejects.toMatchObject({ code: 'ADS_ERROR' });
  });

  it('prod с боевыми именами пишет в RAW_WB_ADV_*', async () => {
    const h = harness(LP, PROD_ENV);
    await makeAdsLoader(h.rt)(ctxFor('prod', LP));
    expect([...h.sink.tables.keys()].every((t) => !t.endsWith('__CR') && t.startsWith('RAW_WB_ADV_'))).toBe(true);
  });

  it('токен недоступен → ADS_TOKEN_UNAVAILABLE, WB не вызывался', async () => {
    const h = harness(LP);
    h.rt.getToken = async () => { throw new Error('denied'); };
    await expect(makeAdsLoader(h.rt)(ctxFor('shadow', LP))).rejects.toMatchObject({ code: 'ADS_TOKEN_UNAVAILABLE' });
    expect(h.calls).toEqual([]);
  });

  it('DRY_RUN=1: ни WB, ни токена, ни записи', async () => {
    const h = harness(LP, { DRY_RUN: '1' });
    await makeAdsLoader(h.rt)(ctxFor('shadow', LP));
    expect([h.tokenCalls, h.calls.length, h.sink.order.length]).toEqual([0, 0, 0]);
  });

  it('run_id без execution id — случайный суффикс, префикс сохраняется', () => {
    expect(adsRunId('ADSAUDIT_', new Date('2026-11-09T21:00:00Z'), '', () => 0.5)).toBe('ADSAUDIT_20261110_000000_CR7fff');
  });
});

// ─────────────────────────────────────────────────────────────────────────
describe('ads-costs-audit', () => {
  const TODAY = '2026-11-10';

  it('shadow: ADSAUDIT_, D−46…D−8 окнами по 30 суток, маркер на каждое окно', async () => {
    const h = harness(TODAY);
    await makeAdsCostsAuditLoader(h.rt)(ctxFor('shadow', TODAY));
    const runs = h.sink.rows('RAW_WB_ADV_COSTS_RUNS__CR');
    expect(runs.map((r) => [r.window_index, r.period_from, r.period_to])).toEqual([
      ['0', '2026-09-25', '2026-10-24'], ['1', '2026-10-25', '2026-11-02'],
    ]);
    expect(runs.every((r) => r.run_id?.startsWith('ADSAUDIT_'))).toBe(true);
    expect(h.calls.every((c) => c === 'GET /adv/v1/upd')).toBe(true);
    expect(h.sleeps).toContain(1200); // пауза между окнами upd
  });

  it('prod без ADS_COSTS_AUDIT_ENABLED=1 — отказ до токена (snapshot v1 занизил бы края)', async () => {
    const h = harness(TODAY, PROD_ENV);
    await expect(makeAdsCostsAuditLoader(h.rt)(ctxFor('prod', TODAY))).rejects.toMatchObject({ code: 'ADS_AUDIT_DISABLED' });
    expect([h.tokenCalls, h.calls.length]).toEqual([0, 0]);
  });

  it('ADS_COSTS_AUDIT_FROM_DAYS=45 возвращает окно эталона D−45…D−8', async () => {
    const h = harness(TODAY, { ...PROD_ENV, ADS_COSTS_AUDIT_ENABLED: '1', ADS_COSTS_AUDIT_FROM_DAYS: '45' });
    await makeAdsCostsAuditLoader(h.rt)(ctxFor('prod', TODAY));
    expect(h.sink.rows('RAW_WB_ADV_COSTS_RUNS')[0]!.period_from).toBe('2026-09-26');
  });

  it('аудит с ошибкой окна → ADS_AUDIT_PARTIAL', async () => {
    const h = harness(TODAY, {}, { upd429: 99 });
    await expect(makeAdsCostsAuditLoader(h.rt)(ctxFor('shadow', TODAY))).rejects.toMatchObject({ code: 'ADS_AUDIT_PARTIAL' });
  });
});

// ─────────────────────────────────────────────────────────────────────────
describe('ads: BigQuery load-job', () => {
  const fake = (loadImpl: (src: string, md: JobLoadMetadata) => Promise<unknown>, jobState = { state: 'DONE' } as unknown) => {
    const loads: Array<{ table: string; md: JobLoadMetadata; body: string }> = [];
    const bq: BqLike = {
      query: async () => [[]],
      dataset: () => ({ table: (t: string) => ({ load: async (src: string, md: JobLoadMetadata) => {
        loads.push({ table: t, md, body: readFileSync(src, 'utf8') });
        return loadImpl(src, md);
      } }) }),
      job: () => ({ get: async () => [{ metadata: { status: jobState } }] }),
    };
    return { bq, loads };
  };

  it('append: CREATE_NEVER, WRITE_APPEND, детерминированный jobId, NDJSON без пустых полей', async () => {
    const f = fake(async () => ({}));
    const a = new AdsBq('p', 'EU', 'wb_raw', 'shadow', 'ADSRAW_20261110_050000_CRab12c', f.bq);
    expect(await a.append('RAW_WB_ADV_COSTS__CR', COSTS_COLUMNS, [{ run_id: 'R', updSum: 1, campName: '' }])).toBe(1);
    expect(await a.append('RAW_WB_ADV_COSTS__CR', COSTS_COLUMNS, [])).toBe(0);
    expect(f.loads).toHaveLength(1);
    expect(f.loads[0]!.md).toMatchObject({ createDisposition: 'CREATE_NEVER', writeDisposition: 'WRITE_APPEND', ignoreUnknownValues: false });
    expect(f.loads[0]!.md.jobId).toBe(adsLoadJobId('shadow', 'ADSRAW_20261110_050000_CRab12c', 'RAW_WB_ADV_COSTS__CR', 1));
    expect(JSON.parse(f.loads[0]!.body.trim())).toEqual({ run_id: 'R', updSum: '1' });
  });

  it('повтор того же load (409, задача DONE) не удваивает строки и не падает; 409 с ошибкой — падает', async () => {
    const dup = Object.assign(new Error('Already Exists: Job'), { code: 409 });
    const ok = fake(async () => { throw dup; });
    expect(await new AdsBq('p', 'EU', 'd', 'shadow', 'R', ok.bq).append('T', ['a'], [{ a: 1 }])).toBe(1);
    const bad = fake(async () => { throw dup; }, { state: 'DONE', errorResult: { message: 'boom' } });
    await expect(new AdsBq('p', 'EU', 'd', 'shadow', 'R', bad.bq).append('T', ['a'], [{ a: 1 }])).rejects.toThrow(/boom/);
  });
});

// ─────────────────────────────────────────────────────────────────────────
describe('ads: реестр', () => {
  it("'ads' — D−1 МСК (как INGEST_RUNS), не prodOnly, без retryTransient; аудит — сегодня МСК", () => {
    const now = new Date('2026-11-10T02:00:00Z');
    expect(resolveLoader('ads')?.logicalPeriod(now)).toBe('2026-11-09');
    expect(resolveLoader('ads')?.prodOnly).toBeFalsy();
    expect(resolveLoader('ads')?.retryTransient).toBeFalsy();
    expect(resolveLoader('ads-costs-audit')?.logicalPeriod(now)).toBe('2026-11-10');
  });
});
