/**
 * Phase B — загрузчик «Оплаты за заказ» Ozon. Фикстуры синтетические (форма — живые выгрузки
 * 2025-05…2026-10), идентификаторы заказов вымышлены: репозиторий публичный.
 */
import { describe, it, expect } from 'vitest';
import { parseCpoReport, parseRub, centsToDecimal, CPO_HEADERS, type CpoFamily } from '../src/loaders/ozon_cpo/parse.js';
import { cpoWindow, chunkWindow, mskDayBoundsUtc, CPO_DAILY_LOOKBACK_DAYS } from '../src/loaders/ozon_cpo/window.js';
import { normalizeCpoReport, cpoNaturalKey, duplicateKeys } from '../src/loaders/ozon_cpo/normalize.js';
import { assertAllowedRoute, OzonPerfClient, OZON_PERF_HOST } from '../src/loaders/ozon_cpo/perfApi.js';
import { cpoReplaceSql, cpoRaiseError } from '../src/loaders/ozon_cpo/bq.js';
import { ozonCpoOrdersLoader, type CpoDeps } from '../src/loaders/ozon_cpo/index.js';
import { classifyFailure } from '../src/failure.js';
import { Logger } from '../src/logging.js';
import { LOADERS } from '../src/loaders/registry.js';
import type { LoaderContext } from '../src/loaders/types.js';

const BOM = '﻿';
const dmy = (iso: string) => `${iso.slice(8, 10)}.${iso.slice(5, 7)}.${iso.slice(0, 4)}`;

interface FxRow { d: string; oid: string; num: string; sku: string; psku: string; offer: string; src?: string; name?: string; q: number; unit: string; val: string; pct: string; rate: string; exp: string }

function csv(family: CpoFamily, from: string, to: string, rows: FxRow[], total?: string | null): string {
  const title = family === 'ALL_SKU_PROMO'
    ? `;Оплата за заказ (все товары). Отчёт по заказам, Период ${dmy(from)}-${dmy(to)}`
    : `;Отчёт по заказам, период ${dmy(from)}-${dmy(to)}`;
  const lines = [BOM + title, CPO_HEADERS[family].join(';')];
  for (const r of rows) {
    const mid = family === 'SEARCH_PROMO' ? [r.src ?? 'Кампания за клики', r.name ?? 'EVETIS Крем'] : [r.name ?? 'EVETIS Крем'];
    lines.push([dmy(r.d), r.oid, r.num, r.sku, r.psku, r.offer, ...mid, String(r.q), r.unit, r.val, r.pct, r.rate, r.exp].join(';'));
  }
  if (family === 'SEARCH_PROMO' && rows.length && total !== null) {
    lines.push(`Всего;;;;;;;;;;;;;${total ?? rows.reduce((n, r) => n + parseRub(r.exp)!, 0) / 100}`.replace('.', ','));
  }
  return lines.join('\r\n') + '\r\n';
}

const R1: FxRow = { d: '2026-10-02', oid: '900000001', num: '11111111-0001', sku: '3000000001', psku: '2000000001', offer: '930334396', q: 1, unit: '1450,00', val: '1450,00', pct: '5,00', rate: '72,50', exp: '72,50' };
const R2: FxRow = { d: '2026-10-03', oid: '900000002', num: '11111111-0002', sku: '2000000002', psku: '2000000002', offer: '305101272', q: 3, unit: '646,00', val: '1938,00', pct: '10,00', rate: '64,60', exp: '193,80' };

describe('Phase B CPO — разбор отчёта', () => {
  it('ALL_SKU_PROMO: строки, заказанный ≠ продвигаемый, итог в копейках', () => {
    const rep = parseCpoReport('ALL_SKU_PROMO', csv('ALL_SKU_PROMO', '2026-10-01', '2026-10-07', [R1, R2]), { from: '2026-10-01', to: '2026-10-07' });
    expect(rep.rows).toHaveLength(2);
    expect(rep.rows[0]).toMatchObject({ chargeDate: '2026-10-02', orderedSku: '3000000001', promotedSku: '2000000001', orderedOfferId: '930334396', orderSource: null, expenseRub: '72.50' });
    expect(rep.rows[1]).toMatchObject({ quantity: 3, unitSalePriceRub: '646.00', saleValueRub: '1938.00', rateRub: '64.60', expenseRub: '193.80', ratePct: '10.00' });
    expect(rep.expenseCents).toBe(26630);
  });

  it('SEARCH_PROMO: «Источник заказов» и «Всего» сверены', () => {
    const rep = parseCpoReport('SEARCH_PROMO', csv('SEARCH_PROMO', '2026-10-01', '2026-10-07', [R1, R2]), { from: '2026-10-01', to: '2026-10-07' });
    expect(rep.rows[0]!.orderSource).toBe('Кампания за клики');
    expect(rep.reportTotalRub).toBe('266.30');
  });

  it('округление «Всего» отчётом (0,01 ₽, наблюдено 2025-06) допускается, расхождение больше — отказ', () => {
    expect(() => parseCpoReport('SEARCH_PROMO', csv('SEARCH_PROMO', '2026-10-01', '2026-10-07', [R1, R2], '266,29'), { from: '2026-10-01', to: '2026-10-07' })).not.toThrow();
    expect(() => parseCpoReport('SEARCH_PROMO', csv('SEARCH_PROMO', '2026-10-01', '2026-10-07', [R1, R2], '193,80'), { from: '2026-10-01', to: '2026-10-07' }))
      .toThrow(expect.objectContaining({ code: 'CPO_REPORT_TOTAL_MISMATCH' }));
  });

  it('SEARCH_PROMO со строками без «Всего» — неполный отчёт (инвариант 6)', () => {
    expect(() => parseCpoReport('SEARCH_PROMO', csv('SEARCH_PROMO', '2026-10-01', '2026-10-07', [R1], null), { from: '2026-10-01', to: '2026-10-07' }))
      .toThrow(expect.objectContaining({ code: 'CPO_REPORT_PARTIAL' }));
  });

  it('период отчёта ≠ запрошенному — CPO_REPORT_PARTIAL, а не загрузка', () => {
    expect(() => parseCpoReport('ALL_SKU_PROMO', csv('ALL_SKU_PROMO', '2026-10-01', '2026-10-05', [R1]), { from: '2026-10-01', to: '2026-10-07' }))
      .toThrow(expect.objectContaining({ code: 'CPO_REPORT_PARTIAL' }));
  });

  it('изменённая шапка — CPO_REPORT_SCHEMA_CHANGED', () => {
    const body = csv('ALL_SKU_PROMO', '2026-10-01', '2026-10-07', [R1]).replace('Расход, ₽', 'Расход');
    expect(() => parseCpoReport('ALL_SKU_PROMO', body, { from: '2026-10-01', to: '2026-10-07' })).toThrow(expect.objectContaining({ code: 'CPO_REPORT_SCHEMA_CHANGED' }));
  });

  it('«;» в названии товара не сдвигает поля', () => {
    const rep = parseCpoReport('ALL_SKU_PROMO', csv('ALL_SKU_PROMO', '2026-10-01', '2026-10-07', [{ ...R1, name: 'Набор; 2 шт; с дозатором' }]), { from: '2026-10-01', to: '2026-10-07' });
    expect(rep.rows[0]).toMatchObject({ productName: 'Набор; 2 шт; с дозатором', expenseRub: '72.50', quantity: 1 });
  });

  it('расход ≠ ставка × количество — отказ; дата вне периода — отказ', () => {
    expect(() => parseCpoReport('ALL_SKU_PROMO', csv('ALL_SKU_PROMO', '2026-10-01', '2026-10-07', [{ ...R2, exp: '64,60' }]), { from: '2026-10-01', to: '2026-10-07' }))
      .toThrow(expect.objectContaining({ code: 'CPO_REPORT_ROW_ARITHMETIC' }));
    expect(() => parseCpoReport('ALL_SKU_PROMO', csv('ALL_SKU_PROMO', '2026-10-01', '2026-10-07', [{ ...R1, d: '2026-10-08' }]), { from: '2026-10-01', to: '2026-10-07' }))
      .toThrow(expect.objectContaining({ code: 'CPO_REPORT_ROW_INVALID' }));
  });

  it('пустой отчёт — законный ноль, не сбой', () => {
    const rep = parseCpoReport('ALL_SKU_PROMO', csv('ALL_SKU_PROMO', '2026-07-01', '2026-07-31', []), { from: '2026-07-01', to: '2026-07-31' });
    expect(rep.rows).toHaveLength(0);
    expect(rep.expenseCents).toBe(0);
  });

  it('денежный разбор без float: «1 450,00» → 145000 коп. → "1450.00"', () => {
    expect(parseRub('1 450,00')).toBe(145000);
    expect(centsToDecimal(parseRub('0,07')!)).toBe('0.07');
    expect(parseRub('-')).toBeNull();
  });
});

describe('Phase B CPO — окно', () => {
  const now = new Date('2026-10-08T05:40:00Z');           // 08:40 МСК

  it(`DAILY: ${CPO_DAILY_LOOKBACK_DAYS} закрытых суток списания, вчера МСК — последние`, () => {
    const w = cpoWindow({ now });
    expect(w).toMatchObject({ mode: 'DAILY', from: '2026-08-24', to: '2026-10-07' });
    expect(w.chunks).toEqual([
      { from: '2026-08-24', to: '2026-08-31' }, { from: '2026-09-01', to: '2026-09-30' }, { from: '2026-10-01', to: '2026-10-07' }]);
  });

  it('BACKFILL с мая 2025: месячные блоки, открытые сутки и дата до истории отклоняются', () => {
    const w = cpoWindow({ now, backfillFrom: '2025-05-01', backfillTo: '2026-10-07' });
    expect(w.chunks).toHaveLength(18);
    expect(w.chunks[0]).toEqual({ from: '2025-05-01', to: '2025-05-31' });
    expect(() => cpoWindow({ now, backfillFrom: '2026-10-01', backfillTo: '2026-10-08' })).toThrow(/открытые сутки/);
    expect(() => cpoWindow({ now, backfillFrom: '2025-04-30', backfillTo: '2025-05-31' })).toThrow(/истории/);
    expect(() => cpoWindow({ now, backfillFrom: '2025-05-01' })).toThrow(/обе границы/);
  });

  it('сутки МСК → моменты UTC (сутки начинаются в 21:00Z накануне)', () => {
    expect(mskDayBoundsUtc('2026-10-01', '2026-10-07')).toEqual({ fromUtc: '2026-09-30T21:00:00Z', toUtc: '2026-10-07T20:59:59Z' });
  });

  it('блок не длиннее месяца', () => {
    for (const c of chunkWindow('2025-05-01', '2026-10-07')) expect(c.from.slice(0, 7)).toBe(c.to.slice(0, 7));
  });
});

describe('Phase B CPO — Performance API только на чтение', () => {
  it('белый список: пять маршрутов чтения; запись кампаний, ставок, товаров — отказ до сети', () => {
    for (const [m, p] of [
      ['POST', '/api/client/token'], ['GET', '/api/client/statistics/all_sku_promo/orders/generate?timeBounds.from=x'],
      ['POST', '/api/client/statistic/orders/generate'], ['GET', '/api/client/statistics/0b1e7d1c-aaaa-bbbb-cccc-123456789012'],
      ['GET', '/api/client/statistics/report?UUID=abc'],
    ] as const) expect(() => assertAllowedRoute(m, `${OZON_PERF_HOST}${p}`)).not.toThrow();
    for (const [m, p] of [
      ['POST', '/api/client/campaign/search_promo/v2/products'], ['POST', '/api/client/campaign/search_promo/v2/products/enable'],
      ['POST', '/api/client/campaign/14503166/products/set'], ['PUT', '/api/client/statistics/report'],
      ['POST', '/api/client/statistics'], ['GET', '/api/client/campaign'],
    ] as const) expect(() => assertAllowedRoute(m, `${OZON_PERF_HOST}${p}`)).toThrow(expect.objectContaining({ code: 'CPO_PERF_ROUTE_DENIED' }));
    expect(() => assertAllowedRoute('POST', 'https://evil.example/api/client/token')).toThrow(expect.objectContaining({ code: 'CPO_PERF_ROUTE_DENIED' }));
  });

  function fakeHttp(script: Array<{ status: number; body: string }>) {
    const calls: Array<{ url: string; method: string; body?: string; auth?: string }> = [];
    const fetch = async (url: string, init: RequestInit) => {
      const h = init.headers as Record<string, string>;
      calls.push({ url, method: String(init.method), body: init.body as string | undefined, auth: h.Authorization });
      const r = script.shift() ?? { status: 500, body: '' };
      return { ok: r.status >= 200 && r.status < 300, status: r.status, body: r.body, attempts: 1 };
    };
    return { fetch, calls };
  }
  const creds = async () => ({ clientId: 'CID-secret-value', clientSecret: 'SECRET-value' });

  it('заказ → опрос → скачивание; токен один на прогон; секреты не в URL', async () => {
    const h = fakeHttp([
      { status: 200, body: '{"access_token":"TOKEN-xyz"}' }, { status: 200, body: '{"UUID":"0b1e7d1c-aaaa-bbbb-cccc-123456789012"}' },
      { status: 200, body: '{"state":"IN_PROGRESS"}' }, { status: 200, body: '{"state":"OK"}' }, { status: 200, body: 'CSV' },
    ]);
    const c = new OzonPerfClient(creds, { opts: { timeoutMs: 1, maxRetries: 0 }, fetch: h.fetch, sleep: async () => {} });
    const r = await c.fetchReport('SEARCH_PROMO', '2026-09-30T21:00:00Z', '2026-10-07T20:59:59Z');
    expect(r).toMatchObject({ body: 'CSV', polls: 2 });
    expect(h.calls.map((x) => `${x.method} ${new URL(x.url).pathname}`)).toEqual([
      'POST /api/client/token', 'POST /api/client/statistic/orders/generate',
      'GET /api/client/statistics/0b1e7d1c-aaaa-bbbb-cccc-123456789012', 'GET /api/client/statistics/0b1e7d1c-aaaa-bbbb-cccc-123456789012',
      'GET /api/client/statistics/report']);
    expect(JSON.parse(h.calls[1]!.body!)).toEqual({ from: '2026-09-30T21:00:00Z', to: '2026-10-07T20:59:59Z' });
    expect(h.calls.every((x) => !x.url.includes('SECRET') && !x.url.includes('CID-'))).toBe(true);
    expect(h.calls.slice(1).every((x) => x.auth === 'Bearer TOKEN-xyz')).toBe(true);
  });

  it('отказ токена не цитирует тело ответа (там может быть секрет)', async () => {
    const h = fakeHttp([{ status: 401, body: '{"error":"bad client SECRET-value"}' }]);
    const c = new OzonPerfClient(creds, { opts: { timeoutMs: 1, maxRetries: 0 }, fetch: h.fetch, sleep: async () => {} });
    const err = await c.fetchReport('ALL_SKU_PROMO', 'a', 'b').catch((e) => e as Error);
    expect(err.message).not.toContain('SECRET');
    expect(classifyFailure(err)).toEqual({ code: 'CPO_PERF_AUTH', category: 'DETERMINISTIC' });
  });

  it('5xx/429 и очередь Ozon — транзиентны (один повтор в слоте); state=ERROR — детерминирован', async () => {
    const h1 = fakeHttp([{ status: 200, body: '{"access_token":"T"}' }, { status: 503, body: 'busy' }]);
    const e1 = await new OzonPerfClient(creds, { opts: { timeoutMs: 1, maxRetries: 0 }, fetch: h1.fetch, sleep: async () => {} })
      .fetchReport('ALL_SKU_PROMO', 'a', 'b').catch((e) => e);
    expect(classifyFailure(e1).category).toBe('TRANSIENT');
    const h2 = fakeHttp([{ status: 200, body: '{"access_token":"T"}' }, { status: 200, body: '{"UUID":"0b1e7d1c-aaaa"}' },
      { status: 200, body: '{"state":"IN_PROGRESS"}' }, { status: 200, body: '{"state":"IN_PROGRESS"}' }]);
    const e2 = await new OzonPerfClient(creds, { opts: { timeoutMs: 1, maxRetries: 0 }, fetch: h2.fetch, sleep: async () => {}, maxPolls: 2 })
      .fetchReport('ALL_SKU_PROMO', 'a', 'b').catch((e) => e);
    expect(classifyFailure(e2).category).toBe('TRANSIENT');
    const h3 = fakeHttp([{ status: 200, body: '{"access_token":"T"}' }, { status: 200, body: '{"UUID":"0b1e7d1c-aaaa"}' }, { status: 200, body: '{"state":"ERROR"}' }]);
    const e3 = await new OzonPerfClient(creds, { opts: { timeoutMs: 1, maxRetries: 0 }, fetch: h3.fetch, sleep: async () => {} })
      .fetchReport('ALL_SKU_PROMO', 'a', 'b').catch((e) => e);
    expect(classifyFailure(e3)).toEqual({ code: 'CPO_PERF_REPORT_ERROR', category: 'DETERMINISTIC' });
  });
});

describe('Phase B CPO — естественный ключ и SQL замены', () => {
  it('row_key стабилен между прогонами и различает семейства (нет межсемейного слияния)', () => {
    const rep = parseCpoReport('ALL_SKU_PROMO', csv('ALL_SKU_PROMO', '2026-10-01', '2026-10-07', [R1]), { from: '2026-10-01', to: '2026-10-07' });
    const a = normalizeCpoReport(rep, { uuid: 'u1', fromUtc: 'f', toUtc: 't', fetchedAt: '2026-10-08T05:40:00Z', runId: 'r1' });
    const b = normalizeCpoReport(rep, { uuid: 'u2', fromUtc: 'f', toUtc: 't', fetchedAt: '2026-10-09T05:40:00Z', runId: 'r2' });
    expect(a[0]!.row_key).toBe(b[0]!.row_key);
    expect(cpoNaturalKey({ ...a[0]!, report_family: 'SEARCH_PROMO' })).not.toBe(a[0]!.row_key);
    expect(duplicateKeys([...a, ...a])).toEqual([a[0]!.row_key]);
    expect(a[0]!.completeness_status).toBe('COMPLETE');
  });

  it('все блокирующие проверки — внутри транзакции и до COMMIT; DELETE ограничен окном; ключ уникален по всей таблице', () => {
    const sql = cpoReplaceSql('`p.ozon_raw.T`', '`p.ozon_raw.T__STAGE`', '`p.ozon_raw.R`');
    const commit = sql.indexOf('COMMIT TRANSACTION');
    for (const code of ['CPO_STAGE_ROWS', 'CPO_STAGE_FOREIGN_RUN', 'CPO_STAGE_OUT_OF_WINDOW', 'CPO_STAGE_DUPLICATE_KEY',
      'CPO_STAGE_AMOUNT', 'CPO_ROWS_WOULD_DISAPPEAR', 'CPO_TARGET_MISMATCH', 'CPO_TARGET_DUPLICATE_KEY']) {
      const at = sql.indexOf(code);
      expect(at, code).toBeGreaterThan(sql.indexOf('BEGIN TRANSACTION'));
      expect(at, code).toBeLessThan(commit);
    }
    expect(sql).toMatch(/DELETE FROM `p\.ozon_raw\.T` WHERE charge_date BETWEEN DATE\(@from\) AND DATE\(@to\);/);
    expect(sql).toMatch(/SET target_dup = \(SELECT COUNT\(\*\) - COUNT\(DISTINCT row_key\) FROM `p\.ozon_raw\.T`\);/);
    expect(sql.indexOf("'CHUNK', @mode, 'COMPLETE'")).toBeLessThan(commit);
    expect(sql).toContain('ROLLBACK TRANSACTION');
  });
});

describe('Phase B CPO — прогон', () => {
  const ctx = (env: Partial<LoaderContext['config']> = {}): LoaderContext => ({
    config: { environment: 'prod', rawDataset: 'ozon_raw', projectId: 'p', bqLocation: 'EU', imageDigest: 'd', gitSha: 'g', executionId: 'e', ...env } as LoaderContext['config'],
    logger: new Logger({}, 'error'), logicalPeriod: '2026-10-08T08', runId: 'x', targetDate: '2026-10-08T08',
  });
  function deps(failOn?: string) {
    const chunks: string[] = []; const runs: Array<{ status: string; chunksLoaded: number; errorCode: string | null }> = [];
    const d: CpoDeps = {
      now: () => new Date('2026-10-08T05:40:00Z'),
      perf: () => ({ fetchReport: async (family: CpoFamily, fromUtc: string) => {
        const from = new Date(Date.parse(fromUtc) + 3 * 3_600_000).toISOString().slice(0, 10);
        if (failOn && from === failOn) throw Object.assign(new Error('Performance HTTP 503'), { code: 503 });
        const to = from === '2026-08-24' ? '2026-08-31' : from === '2026-09-01' ? '2026-09-30' : '2026-10-07';
        const rows = from === '2026-10-01' ? [family === 'ALL_SKU_PROMO' ? R1 : R2] : [];
        return { body: csv(family, from, to, rows), uuid: `${family}-${from}`, polls: 1 };
      } }),
      bq: () => ({
        replaceChunk: async (_rows, chunk) => { chunks.push(`${chunk.from}..${chunk.to}`); },
        journalRun: async (j) => { runs.push({ status: j.status, chunksLoaded: j.chunksLoaded, errorCode: j.errorCode }); },
      }),
    };
    return { d, chunks, runs };
  }

  it('COMPLETE: все блоки легли, журнал RUN = COMPLETE', async () => {
    const t = deps();
    const r = await ozonCpoOrdersLoader(ctx(), t.d);
    expect(t.chunks).toEqual(['2026-08-24..2026-08-31', '2026-09-01..2026-09-30', '2026-10-01..2026-10-07']);
    expect(t.runs).toEqual([{ status: 'COMPLETE', chunksLoaded: 3, errorCode: null }]);
    expect(r).toEqual({ rowsFetched: 2, rowsLoaded: 2 });
  });

  it('PARTIAL: блок упал после уже легших — журнал PARTIAL, Job падает (видно алертом)', async () => {
    const t = deps('2026-10-01');
    await expect(ozonCpoOrdersLoader(ctx(), t.d)).rejects.toThrow();
    expect(t.runs).toEqual([{ status: 'PARTIAL', chunksLoaded: 2, errorCode: 'TRANSIENT_INFRA' }]);
  });

  it('ERROR: первый же блок упал — ничего не легло', async () => {
    const t = deps('2026-08-24');
    await expect(ozonCpoOrdersLoader(ctx(), t.d)).rejects.toThrow();
    expect(t.chunks).toEqual([]);
    expect(t.runs).toEqual([{ status: 'ERROR', chunksLoaded: 0, errorCode: 'TRANSIENT_INFRA' }]);
  });

  it('только prod и только ozon_raw (изоляция маркетплейсов)', async () => {
    await expect(ozonCpoOrdersLoader(ctx({ environment: 'shadow' }), deps().d)).rejects.toMatchObject({ code: 'CPO_PROD_ONLY' });
    await expect(ozonCpoOrdersLoader(ctx({ rawDataset: 'wb_raw' }), deps().d)).rejects.toMatchObject({ code: 'CPO_CONFIG' });
  });

  it('реестр: prodOnly, повтор транзиентного отказа, часовой слот', () => {
    expect(LOADERS['ozon-cpo-orders']).toMatchObject({ prodOnly: true, retryTransient: true });
    expect(LOADERS['ozon-cpo-orders']!.logicalPeriod(new Date('2026-10-08T05:40:00Z'))).toBe('2026-10-08T08');
  });
});

describe('Phase B CPO — коды отказов транзакции и редиректы', () => {
  it('RAISE «CPO_…:» становится LoaderError со своим кодом (журнал и метка алерта)', () => {
    const e = cpoRaiseError(new Error('Query error: CPO_ROWS_WOULD_DISAPPEAR: 2 суток исчезли бы из RAW at [55:5]'));
    expect(classifyFailure(e)).toEqual({ code: 'CPO_ROWS_WOULD_DISAPPEAR', category: 'DETERMINISTIC' });
    const t = Object.assign(new Error('backendError'), { code: 503 });
    expect(cpoRaiseError(t)).toBe(t);
  });
  it('редиректы запрещены: белый список не обходится переадресацией', async () => {
    const inits: RequestInit[] = [];
    const c = new OzonPerfClient(async () => ({ clientId: 'a', clientSecret: 'b' }), { opts: { timeoutMs: 1, maxRetries: 0 },
      fetch: async (_u: string, init: RequestInit) => { inits.push(init); return { ok: false, status: 401, body: '', attempts: 1 }; } });
    await c.fetchReport('ALL_SKU_PROMO', 'a', 'b').catch(() => undefined);
    expect(inits[0]!.redirect).toBe('error');
  });
});
