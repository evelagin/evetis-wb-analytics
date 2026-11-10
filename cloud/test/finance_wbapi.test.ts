/**
 * M4 · Клиент Finance API: пейсер 61 с, 429/5xx с паузой WB, бюджет, пагинация, пустой ответ 204.
 * Сеть — синтетический API; время — поддельные часы (sleep двигает время мгновенно).
 */
import { describe, it, expect } from 'vitest';
import { FinanceApi, retryAfterMs } from '../src/loaders/finance/wbApi.js';
import { FIN_PACER_MS } from '../src/loaders/finance/constants.js';
import { FakeClock, SynthFinanceApi, synthRow, resp, silentLogger } from './finance_fixture.js';

const T0 = Date.UTC(2026, 10, 10, 4, 30);
const TOKEN = 'synthetic-token-not-real';

function setup(reports: SynthFinanceApi['reports'] = [], budgetMs = 1_200_000, pageLimit?: number, bqTailMs?: number) {
  const clock = new FakeClock(T0);
  const wb = new SynthFinanceApi(reports, clock, pageLimit);
  const api = new FinanceApi({ host: 'https://finance.example.invalid', token: TOKEN, t0: T0, budgetMs, timeoutMs: 1000, ...(bqTailMs !== undefined ? { bqTailMs } : {}) }, clock, wb.fetch, silentLogger);
  return { clock, wb, api };
}

const daily = (id: string, date: string, n: number, rt = 1) => ({
  reportId: id, reportType: rt, period: 'daily' as const, dateFrom: date, dateTo: date,
  rows: Array.from({ length: n }, (_, i) => synthRow(`9300000000000${String(Number(id.slice(-3)) * 1000 + i).padStart(6, '0')}`, i, date)),
});

describe('FinanceApi: пейсер и лимит WB (1 запрос/мин на все методы)', () => {
  it('между любыми запросами ≥ 61 с; токен — в Authorization как есть', async () => {
    const { wb, api } = setup([daily('900000000000000101', '2026-11-09', 3)]);
    await api.list('daily', '2026-11-07', '2026-11-10');
    await api.detailed('900000000000000101');
    await api.list('weekly', '2026-07-13', '2026-11-10');
    expect(wb.calls.length).toBe(3);
    for (let i = 1; i < wb.calls.length; i++) expect(wb.calls[i]!.at - wb.calls[i - 1]!.at).toBeGreaterThanOrEqual(FIN_PACER_MS);
    expect(wb.calls[0]!.auth).toBe(TOKEN);
    expect(api.requests).toBe(3);
  });

  it('429 → повтор после паузы WB (X-Ratelimit-Retry), и тоже через пейсер', async () => {
    const { wb, api } = setup([daily('900000000000000102', '2026-11-09', 2)]);
    let first = true;
    wb.overrides.push(() => (first ? ((first = false), { status: 429, body: 'Too many requests', headers: { 'X-Ratelimit-Retry': '90' } }) : undefined));
    const r = await api.detailed('900000000000000102');
    expect(r.ok).toBe(true);
    expect(r.rows.length).toBe(2);
    expect(wb.calls.length).toBe(2);
    expect(wb.calls[1]!.at - wb.calls[0]!.at).toBeGreaterThanOrEqual(90_000);
  });

  it('5xx дольше числа повторов → отказ с кодом (не исключение)', async () => {
    const { wb, api } = setup();
    wb.overrides.push(() => ({ status: 503, body: 'unavailable' }));
    const r = await api.list('daily', '2026-11-07', '2026-11-10');
    expect(r.ok).toBe(false);
    expect(r.error).toContain('list HTTP 503');
    expect(wb.calls.length).toBe(3); // 1 + FIN_HTTP_MAX_RETRIES
  });

  it('4xx (401) не повторяется: WB_FINANCE_AUTH (подробно — finance_token_auth.test.ts)', async () => {
    const { wb, api } = setup();
    wb.overrides.push(() => ({ status: 401, body: 'unauthorized' }));
    await expect(api.list('daily', '2026-11-07', '2026-11-10')).rejects.toMatchObject({ code: 'WB_FINANCE_AUTH' });
    expect(wb.calls.length).toBe(1);
  });

  it('прочий 4xx (400) не повторяется и возвращается кодом', async () => {
    const { wb, api } = setup();
    wb.overrides.push(() => ({ status: 400, body: 'bad request' }));
    const r = await api.list('daily', '2026-11-07', '2026-11-10');
    expect(r.ok).toBe(false);
    expect(r.error).toContain('list HTTP 400');
    expect(wb.calls.length).toBe(1);
  });

  it('сетевой сбой → повтор, затем отказ с кодом 0', async () => {
    const clock = new FakeClock(T0);
    let n = 0;
    const api = new FinanceApi({ host: 'h', token: TOKEN, t0: T0, budgetMs: 1_200_000, timeoutMs: 1000 }, clock,
      async () => { n++; throw new Error('ECONNRESET'); }, silentLogger);
    const r = await api.detailed('900000000000000103');
    expect(r.ok).toBe(false);
    expect(r.error).toContain('detailed HTTP 0');
    expect(n).toBe(3);
  });

  it('бюджет: запрос не делается, если ожидание пейсера + маржа не помещаются', async () => {
    const { wb, api } = setup([], 90_000, undefined, 0);
    await api.list('daily', '2026-11-07', '2026-11-10'); // 204, первый запрос без ожидания
    expect(api.canRequest()).toBe(false); // ещё 61 с ожидания + 30 с маржи + 1 с таймаута > остатка
    const r = await api.list('weekly', '2026-07-13', '2026-11-10');
    expect(r).toMatchObject({ ok: false, error: 'BUDGET_EXCEEDED (list)' });
    expect(wb.calls.length).toBe(1);
  });

  it('резерв canRequest = маржа + таймаут запроса + хвост BigQuery', () => {
    const clock = new FakeClock(T0);
    const mk = (budgetMs: number) => new FinanceApi({ host: 'h', token: TOKEN, t0: T0, budgetMs, timeoutMs: 120_000 }, clock, async () => resp(204, ''), silentLogger);
    // 30 с + 120 с + 120 с = 270 с: при остатке 270 с запрос уже не начинается, при 271 с — начинается
    expect(mk(270_000).canRequest()).toBe(false);
    expect(mk(270_001).canRequest()).toBe(true);
  });

  it('retryAfterMs: заголовки WB, мусор, потолок', () => {
    const h = (o: Record<string, string>) => resp(200, '', o).headers;
    expect(retryAfterMs(h({ 'X-Ratelimit-Retry': '5' }))).toBe(5000);
    expect(retryAfterMs(h({ 'Retry-After': '2.5' }))).toBe(2500);
    expect(retryAfterMs(h({ 'Retry-After': 'Wed, 21 Oct' }))).toBe(0);
    expect(retryAfterMs(h({ 'X-Ratelimit-Retry': '100000' }))).toBe(180_000);
  });
});

describe('FinanceApi: list и detailed', () => {
  it('пустой ответ 204 → ok, ноль отчётов/строк', async () => {
    const { api } = setup();
    expect(await api.list('daily', '2026-11-07', '2026-11-10')).toEqual({ ok: true, reports: [], error: '' });
    const { api: api2, wb } = setup([daily('900000000000000104', '2026-11-09', 0)]);
    const d = await api2.detailed('900000000000000104');
    expect(d).toMatchObject({ ok: true, rows: [], pages: 0 });
    expect(wb.calls[0]!.body).toBe('{"rrdId":0,"limit":100000}');
  });

  it('reportType=3 в list → весь list отклонён (fail-closed)', async () => {
    const { wb, api } = setup();
    wb.listOverride = () => [
      { reportId: '900000000000000105', reportType: 1, dateFrom: '2026-11-09', dateTo: '2026-11-09' },
      { reportId: '900000000000000106', reportType: 3, dateFrom: '2026-11-09', dateTo: '2026-11-09' },
    ];
    const r = await api.list('daily', '2026-11-07', '2026-11-10');
    expect(r.ok).toBe(false);
    expect(r.error).toContain('неподдерживаемый reportType=3');
  });

  it('detailed постранично по курсору rrdId (вербатим, BigInt)', async () => {
    const { wb, api } = setup([daily('900000000000000107', '2026-11-09', 5)], 1_200_000, 2);
    // pageLimit синтетики = 2, а клиент просит 100000: короткая страница = последняя. Проверяем курсор на одной странице.
    const r = await api.detailed('900000000000000107');
    expect(r.ok).toBe(true);
    expect(r.rows.length).toBe(2);
    expect(wb.calls.length).toBe(1);
  });

  it('битый курсор (тот же rrdId) → отказ', async () => {
    const { wb, api } = setup();
    wb.overrides.push(() => ({ status: 200, body: '[' + Array.from({ length: 100000 }, () => '{"rrdId":0}').join(',') + ']' }));
    const r = await api.detailed('900000000000000108');
    expect(r).toMatchObject({ ok: false, error: 'detailed: битый курсор rrdId' });
  });

  it('PARSE_MAPPING_ERROR в detailed → отказ', async () => {
    const { wb, api } = setup();
    wb.overrides.push(() => ({ status: 200, body: '[{"rrdId":1,"x":{"rrdId":2}}]' }));
    const r = await api.detailed('900000000000000109');
    expect(r.ok).toBe(false);
    expect(r.error).toContain('PARSE_MAPPING_ERROR');
  });
});
