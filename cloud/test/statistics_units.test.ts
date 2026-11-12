/**
 * M2 — модульные тесты помощников, транспорта, контура и конфигурации загрузчиков statistics-api.
 * Поведение, совпадающее с Apps Script, доказывает statistics_apps_script_parity.test.ts;
 * здесь — граничные случаи и то, чего у Apps Script нет (контур, слот guard, конфиг).
 */
import { describe, it, expect } from 'vitest';
import {
  buildSkuIndex, isValidWatermark, matchSku, md5Hex, mskDate, mskDateTime, mskStamp, normalizeDateKey, normalizeNmId, statsSlot,
} from '../src/loaders/statistics/common.js';
import { fetchOrders, fetchSales, retryAfterMs, statsGet } from '../src/loaders/statistics/wbApi.js';
import { assertStatsTarget, STATS_TARGET_MISMATCH } from '../src/loaders/statistics/targets.js';
import { statsLoadJobId } from '../src/loaders/statistics/bq.js';
import { reconcileFromLcd } from '../src/loaders/statistics/sales.js';
import { LOADERS } from '../src/loaders/registry.js';
import { loadConfig } from '../src/config.js';
import { LoaderError } from '../src/errors.js';
import { NOW, fakeTransport, orderRow, wbScript } from './statistics_fixture.js';

describe('помощники (порт Apps Script)', () => {
  it('normalizeDateKey: ISO с временем, RU, US, пусто', () => {
    expect(normalizeDateKey('2026-11-09T23:59:59')).toBe('2026-11-09');
    expect(normalizeDateKey('2026-1-9 10:00:00')).toBe('2026-01-09');
    expect(normalizeDateKey('09.11.2026')).toBe('2026-11-09');
    expect(normalizeDateKey('09.11.26')).toBe('2026-11-09');
    expect(normalizeDateKey('11/09/2026')).toBe('2026-11-09');
    expect(normalizeDateKey('')).toBe('');
    expect(normalizeDateKey(null)).toBe('');
    expect(normalizeDateKey('мусор')).toBe('');
  });

  it('normalizeNmId: число без дроби, строка без .0, ноль и пусто → ""', () => {
    expect(normalizeNmId(101.9)).toBe('101');
    expect(normalizeNmId(' 101.00 ')).toBe('101');
    expect(normalizeNmId(0)).toBe('');
    expect(normalizeNmId('0')).toBe('');
    expect(normalizeNmId(undefined)).toBe('');
  });

  it('isValidWatermark: только форма API с T и календарная корректность', () => {
    expect(isValidWatermark('2026-11-09T12:00:00')).toBe(true);
    expect(isValidWatermark('2026-11-09T12:00:00.123')).toBe(true);
    expect(isValidWatermark('2024-02-29T00:00:00')).toBe(true);
    expect(isValidWatermark('2026-02-29T00:00:00')).toBe(false);
    expect(isValidWatermark('2026-11-09 12:00:00')).toBe(false);
    expect(isValidWatermark('2026-11-09')).toBe(false);
    expect(isValidWatermark('2026-99-99T99:99:99')).toBe(false);
    expect(isValidWatermark('0000-01-01T00:00:00')).toBe(false);
    expect(isValidWatermark('2026-11-09T12:00:00Z')).toBe(false);
  });

  it('md5Hex = TO_HEX(MD5()) BigQuery: lowercase hex над UTF-8', () => {
    expect(md5Hex('')).toBe('d41d8cd98f00b204e9800998ecf8427e');
    expect(md5Hex('крем')).toMatch(/^[0-9a-f]{32}$/);
  });

  it('время по Москве для load_id/loaded_at и часовой слот guard', () => {
    expect(mskStamp(NOW)).toBe('20261110_093100');
    expect(mskDateTime(NOW)).toBe('2026-11-10 09:31:00');
    expect(mskDate(new Date('2026-11-09T21:30:00Z'))).toBe('2026-11-10');
    expect(statsSlot(new Date('2026-11-09T21:30:00Z'))).toBe('stats-slot:2026-11-10T00');
    expect(statsSlot(NOW)).toBe('stats-slot:2026-11-10T09');
  });

  it('SKU: сначала nm_id, иначе barcode; иначе not_found', () => {
    const idx = buildSkuIndex([
      { nm_id: 101, internal_sku: 'A', barcode: ' 111 ' },
      { nm_id: 202, internal_sku: 'B', barcode: null },
      { nm_id: null, internal_sku: 'X', barcode: '999' },
    ]);
    expect(matchSku(idx, 101, '')).toEqual({ matched: true, internalSku: 'A' });
    expect(matchSku(idx, 303, '111')).toEqual({ matched: true, internalSku: 'A' });
    expect(matchSku(idx, 303, '999')).toEqual({ matched: false, internalSku: '' });
    expect(matchSku(idx, 0, '')).toEqual({ matched: false, internalSku: '' });
  });

  it('окно сверки: полночь МСК сегодня−N', () => {
    expect(reconcileFromLcd(7, NOW.getTime(), mskDate)).toBe('2026-11-03T00:00:00');
    expect(reconcileFromLcd(1, new Date('2026-11-09T21:30:00Z').getTime(), mskDate)).toBe('2026-11-09T00:00:00');
  });
});

describe('транспорт statistics-api', () => {
  it('Retry-After: секунды и HTTP-дата; мусор → -1', () => {
    expect(retryAfterMs('7', 0)).toBe(7000);
    expect(retryAfterMs(new Date(NOW.getTime() + 5000).toUTCString(), NOW.getTime())).toBe(5000);
    expect(retryAfterMs('soon', 0)).toBe(-1);
    expect(retryAfterMs(null, 0)).toBe(-1);
  });

  it('429: пауза по Retry-After (не больше 60 с), иначе 20/40/60 с; после 3 повторов — последний ответ', async () => {
    const sleeps: number[] = [];
    const t = fakeTransport(wbScript([
      { status: 429, body: 'x', retryAfter: '120' }, { status: 429, body: 'x' }, { status: 503, body: 'x' }, { status: 429, body: 'x' },
    ]), sleeps);
    const r = await statsGet('https://h/p?dateFrom=a', 'tok', 1000, t);
    expect(r.status).toBe(429);
    expect(r.attempts).toBe(4);
    expect(sleeps).toEqual([60_000, 40_000, 60_000]);
  });

  it('400 не повторяется', async () => {
    const sleeps: number[] = [];
    const r = await statsGet('https://h/p', 'tok', 1000, fakeTransport(wbScript([{ status: 400, body: 'bad' }]), sleeps));
    expect(r).toMatchObject({ status: 400, attempts: 1 });
    expect(sleeps).toEqual([]);
  });

  it('сетевое исключение повторяется и после исчерпания даёт status=0', async () => {
    const sleeps: number[] = [];
    const t = { fetch: async () => { throw new Error('ECONNRESET'); }, sleep: async (ms: number) => { sleeps.push(ms); }, nowMs: () => 0 };
    const r = await statsGet('https://h/p', 'tok', 1000, t);
    expect(r).toMatchObject({ status: 0, attempts: 4, error: 'ECONNRESET' });
    expect(sleeps).toEqual([20_000, 40_000, 60_000]);
  });

  it('заказы: пауза между страницами 61 с (лимит WB 1 запрос/мин), токен — только в заголовке', async () => {
    const sleeps: number[] = [];
    const calls: Array<{ url: string; auth: string }> = [];
    const t = {
      ...fakeTransport(wbScript([]), sleeps),
      fetch: async (url: string, init: { headers: Record<string, string> }) => {
        calls.push({ url, auth: init.headers.Authorization! });
        const body = calls.length === 1 ? [orderRow(1, { lastChangeDate: '2026-11-09T13:00:00' })] : [];
        return { status: 200, text: async () => JSON.stringify(body), headers: { get: () => null } };
      },
    };
    const r = await fetchOrders('https://h', '/api/v1/supplier/orders', 'secret-token', '2026-11-09T12:00:00', 1000, t);
    expect(r.ok).toBe(true);
    expect(r.pages).toBe(1);
    expect(sleeps).toEqual([61_000]);
    expect(calls[0]!.url).toBe('https://h/api/v1/supplier/orders?dateFrom=2026-11-09T12%3A00%3A00&flag=0');
    expect(calls[1]!.url).toContain('dateFrom=2026-11-09T13%3A00%3A00');
    expect(calls.every((c) => c.auth === 'secret-token' && !c.url.includes('secret'))).toBe(true);
  });

  it('заказы: граница на лимите строк → PARTIAL; лимит страниц → PARTIAL', async () => {
    const big = Array.from({ length: 80_000 }, (_, i) => orderRow(i, { lastChangeDate: '2026-11-09T12:00:00' }));
    const r1 = await fetchOrders('https://h', '/p', 't', '2026-11-09T12:00:00', 1000, fakeTransport(wbScript([{ status: 200, body: big }])));
    expect(r1).toMatchObject({ ok: false, partial: true });

    const pages = Array.from({ length: 31 }, (_, i) => ({ status: 200, body: [orderRow(i, { lastChangeDate: `2026-11-09T12:${String(i).padStart(2, '0')}:01` })] }));
    const r2 = await fetchOrders('https://h', '/p', 't', '2026-11-09T12:00:00', 1000, fakeTransport(wbScript(pages)));
    expect(r2).toMatchObject({ ok: false, partial: true, pages: 30 });
    expect(r2.error).toContain('лимит страниц');
  });

  it('заказы: непустая страница без lastChangeDate → PARTIAL', async () => {
    const r = await fetchOrders('https://h', '/p', 't', 'x', 1000, fakeTransport(wbScript([{ status: 200, body: [orderRow(1, { lastChangeDate: '' })] }])));
    expect(r).toMatchObject({ ok: false, partial: true });
  });

  it('продажи: ровно один запрос; упор в 80 000 строк → неполно, данных нет', async () => {
    const script = wbScript([{ status: 200, body: Array.from({ length: 80_000 }, () => ({})) }]);
    const r = await fetchSales('https://h', '/p', 't', '2026-11-09T12:00:00', 1000, fakeTransport(script));
    expect(r).toMatchObject({ ok: false, partial: true, apiRowsReceived: 80_000, data: [] });
    expect(script.urls).toHaveLength(1);
  });
});

describe('контур записи (до секрета и WB API)', () => {
  const cfg = (environment: 'shadow' | 'prod', orders: string, sales: string, rawDataset = 'wb_raw') =>
    ({ environment, rawDataset, ordersRawTable: orders, salesRawTable: sales });

  it('shadow пишет только в __CR', () => {
    expect(assertStatsTarget(cfg('shadow', 'RAW_WB_ORDERS__CR', 'RAW_WB_SALES_RETURNS__CR'), 'orders')).toBe('RAW_WB_ORDERS__CR');
    expect(() => assertStatsTarget(cfg('shadow', 'RAW_WB_ORDERS', 'x__CR'), 'orders')).toThrow(LoaderError);
    try { assertStatsTarget(cfg('shadow', 'x__CR', 'RAW_WB_SALES_RETURNS'), 'sales'); } catch (e) {
      expect((e as LoaderError).code).toBe(STATS_TARGET_MISMATCH);
    }
  });

  it('prod пишет ровно в боевые таблицы wb_raw', () => {
    expect(assertStatsTarget(cfg('prod', 'RAW_WB_ORDERS', 'RAW_WB_SALES_RETURNS'), 'sales')).toBe('RAW_WB_SALES_RETURNS');
    // Пустой env prod-Job'а → дефолт __CR → отказ, а не тихая запись мимо боевой таблицы.
    expect(() => assertStatsTarget(cfg('prod', 'RAW_WB_ORDERS__CR', 'RAW_WB_SALES_RETURNS'), 'orders')).toThrow(/ожидалось RAW_WB_ORDERS/);
    expect(() => assertStatsTarget(cfg('prod', 'RAW_WB_ORDERS', 'RAW_WB_SALES_RETURNS', 'ozon_raw'), 'orders')).toThrow(/wb_raw/);
  });
});

describe('конфигурация и реестр', () => {
  const base = { ENVIRONMENT: 'shadow', GCP_PROJECT_ID: 'p', BQ_RAW_DATASET: 'wb_raw' };

  it('по умолчанию: теневые таблицы, секрет WB_TOKEN_STATISTICS, окно 7', () => {
    const c = loadConfig(base);
    expect(c.ordersRawTable).toBe('RAW_WB_ORDERS__CR');
    expect(c.salesRawTable).toBe('RAW_WB_SALES_RETURNS__CR');
    expect(c.wbStatisticsSecret).toBe('WB_TOKEN_STATISTICS');
    expect(c.wbStatisticsHost).toBe('https://statistics-api.wildberries.ru');
    expect(c.salesReconcileDays).toBe(7);
  });

  it('SALES_RECONCILE_DAYS: 1..90, иначе 7 (мягко, как wbSalesReconcileDays_)', () => {
    expect(loadConfig({ ...base, SALES_RECONCILE_DAYS: '14' }).salesReconcileDays).toBe(14);
    expect(loadConfig({ ...base, SALES_RECONCILE_DAYS: '0' }).salesReconcileDays).toBe(7);
    expect(loadConfig({ ...base, SALES_RECONCILE_DAYS: '91' }).salesReconcileDays).toBe(7);
    expect(loadConfig({ ...base, SALES_RECONCILE_DAYS: 'abc' }).salesReconcileDays).toBe(7);
  });

  it('имена загрузчиков = loader_name INGEST_RUNS; период D-1 МСК (ingestClosedDayMsk_); слот — версия guard', async () => {
    for (const name of ['orders', 'sales', 'sales-reconcile']) {
      const spec = LOADERS[name]!;
      expect(spec, name).toBeDefined();
      expect(spec.prodOnly).toBeFalsy();
      expect(spec.retryTransient).toBe(true);
      expect(spec.logicalPeriod(new Date('2026-11-09T21:30:00Z'))).toBe('2026-11-09'); // 00:30 МСК 10.11 → D-1 = 09.11
      expect(spec.logicalPeriod(new Date('2026-11-10T20:59:00Z'))).toBe('2026-11-09'); // 23:59 МСК 10.11
      expect(await spec.sourceWatermark!.probe(loadConfig(base))).toMatch(/^stats-slot:\d{4}-\d{2}-\d{2}T\d{2}$/);
    }
  });

  it('load job id: только допустимые символы, уникален на запуск', () => {
    const id = statsLoadJobId('shadow', 'RAW_WB_ORDERS__CR', 'ORD_INC_20261110_093100', 'ab:cd/ef');
    expect(id).toMatch(/^[A-Za-z0-9_-]+$/);
    expect(id).not.toBe(statsLoadJobId('shadow', 'RAW_WB_ORDERS__CR', 'ORD_INC_20261110_093100', 'other'));
  });
});
