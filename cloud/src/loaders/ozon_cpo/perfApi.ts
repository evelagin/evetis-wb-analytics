/**
 * OZON CPO (Phase B) — клиент Ozon Performance API ТОЛЬКО для отчётов «Оплата за заказ».
 *
 * 🔒 БЕЛЫЙ СПИСОК. Ключи Performance API умеют менять ставки, кампании и товары в продвижении.
 * Этот клиент физически не может послать ничего, кроме пяти маршрутов чтения ниже: любой другой
 * метод/путь отклоняется ДО отправки запроса (CPO_PERF_ROUTE_DENIED). Тот же принцип, что
 * PERF_ALLOWED_GET/POST в pipelines/ozon/runtime/common.py.
 *
 *   POST /api/client/token                                   — токен (client_credentials)
 *   GET  /api/client/statistics/all_sku_promo/orders/generate — заказать отчёт ALL_SKU_PROMO
 *   POST /api/client/statistic/orders/generate                — заказать отчёт SEARCH_PROMO
 *   GET  /api/client/statistics/{UUID}                        — статус отчёта
 *   GET  /api/client/statistics/report?UUID=…                 — скачать отчёт
 *
 * Секреты: значения client_id/client_secret и токен живут только в памяти процесса и не попадают
 * ни в логи, ни в тексты ошибок (тело ответа на запрос токена не цитируется никогда).
 */
import { wbFetch, type WbHttpOptions } from '../../http/wbHttp.js';
import { LoaderError } from '../../errors.js';
import type { Logger } from '../../logging.js';
import type { CpoFamily } from './parse.js';

export const OZON_PERF_HOST = 'https://api-performance.ozon.ru';

const ROUTES: ReadonlyArray<{ method: 'GET' | 'POST'; path: RegExp }> = [
  { method: 'POST', path: /^\/api\/client\/token$/ },
  { method: 'GET', path: /^\/api\/client\/statistics\/all_sku_promo\/orders\/generate$/ },
  { method: 'POST', path: /^\/api\/client\/statistic\/orders\/generate$/ },
  { method: 'GET', path: /^\/api\/client\/statistics\/[0-9a-fA-F-]{8,64}$/ },
  { method: 'GET', path: /^\/api\/client\/statistics\/report$/ },
];

/** Отказ до сети: маршрут вне белого списка. Экспортируется для теста безопасности. */
export function assertAllowedRoute(method: string, url: string): void {
  const u = new URL(url);
  if (u.origin !== OZON_PERF_HOST) throw new LoaderError(`Performance API: хост ${u.origin} запрещён`, 'CPO_PERF_ROUTE_DENIED');
  if (!ROUTES.some((r) => r.method === method && r.path.test(u.pathname))) {
    throw new LoaderError(`Performance API: ${method} ${u.pathname} вне белого списка`, 'CPO_PERF_ROUTE_DENIED');
  }
}

/** HTTP 408/429/5xx и сетевой обрыв после повторов транспорта (status 0) — транзиентны. */
const isTransientStatus = (s: number): boolean => s === 0 || [408, 429, 500, 502, 503, 504].includes(s);

/** Транзиентный отказ: classifyFailure видит числовой code из TRANSIENT_HTTP → один повтор в слоте. */
function transient(message: string, status: number): Error {
  return Object.assign(new Error(message), { code: [408, 429, 500, 502, 503, 504].includes(status) ? status : 503 });
}

export interface PerfFetch {
  (url: string, init: RequestInit, opts: WbHttpOptions, logger?: Logger): ReturnType<typeof wbFetch>;
}

export interface CpoReportFetch { body: string; uuid: string; polls: number }

export class OzonPerfClient {
  private token: string | null = null;
  private tokenAt = 0;

  constructor(
    private readonly creds: () => Promise<{ clientId: string; clientSecret: string }>,
    private readonly http: { opts: WbHttpOptions; fetch?: PerfFetch; sleep?: (ms: number) => Promise<void>; nowMs?: () => number;
      pollIntervalMs?: number; maxPolls?: number },
    private readonly logger?: Logger,
  ) {}

  private async call(method: 'GET' | 'POST', path: string, body?: unknown, auth = true): Promise<{ status: number; text: string }> {
    const url = `${OZON_PERF_HOST}${path}`;
    assertAllowedRoute(method, url);
    const headers: Record<string, string> = { Accept: 'application/json, text/csv' };
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    if (auth) headers.Authorization = `Bearer ${await this.accessToken()}`;
    const f = this.http.fetch ?? wbFetch;
    const r = await f(url, { method, headers, body: body === undefined ? undefined : JSON.stringify(body), redirect: 'error' }, this.http.opts, this.logger);
    return { status: r.status, text: r.ok ? r.body : (r.body || r.error || '') };
  }

  private async accessToken(): Promise<string> {
    const now = (this.http.nowMs ?? Date.now)();
    // Токен живёт 30 минут; обновляем каждые 25, как runtime Ozon.
    if (this.token && now - this.tokenAt < 25 * 60_000) return this.token;
    const c = await this.creds();
    const r = await this.call('POST', '/api/client/token',
      { client_id: c.clientId, client_secret: c.clientSecret, grant_type: 'client_credentials' }, false);
    // Тело ответа токена не цитируется: в нём может быть сам токен или эхо учётных данных.
    if (r.status !== 200) {
      if (isTransientStatus(r.status)) throw transient(`Performance API: токен не получен, HTTP ${r.status}`, r.status);
      throw new LoaderError(`Performance API: токен не получен, HTTP ${r.status}`, 'CPO_PERF_AUTH');
    }
    let tok: unknown;
    try { tok = (JSON.parse(r.text) as { access_token?: unknown }).access_token; } catch { tok = undefined; }
    if (typeof tok !== 'string' || !tok) throw new LoaderError('Performance API: в ответе нет access_token', 'CPO_PERF_AUTH');
    this.token = tok; this.tokenAt = now;
    return tok;
  }

  private check(r: { status: number; text: string }, what: string, code: string): void {
    if (r.status === 200) return;
    const msg = `${what}: HTTP ${r.status}: ${r.text.slice(0, 300)}`;
    if (isTransientStatus(r.status)) throw transient(msg, r.status);
    throw new LoaderError(msg, code);
  }

  /** Заказ → ожидание → скачивание одного отчёта. Границы — моменты UTC (см. window.ts). */
  async fetchReport(family: CpoFamily, fromUtc: string, toUtc: string): Promise<CpoReportFetch> {
    const sub = family === 'ALL_SKU_PROMO'
      ? await this.call('GET', `/api/client/statistics/all_sku_promo/orders/generate?${new URLSearchParams({ 'timeBounds.from': fromUtc, 'timeBounds.to': toUtc })}`)
      : await this.call('POST', '/api/client/statistic/orders/generate', { from: fromUtc, to: toUtc });
    this.check(sub, `${family}: заказ отчёта`, 'CPO_PERF_SUBMIT');
    let uuid = '';
    try { uuid = String((JSON.parse(sub.text) as { UUID?: unknown }).UUID ?? ''); } catch { uuid = ''; }
    if (!/^[0-9a-fA-F-]{8,64}$/.test(uuid)) throw new LoaderError(`${family}: в ответе нет UUID отчёта`, 'CPO_PERF_SUBMIT');
    const sleep = this.http.sleep ?? ((ms: number) => new Promise<void>((res) => setTimeout(res, ms)));
    const maxPolls = this.http.maxPolls ?? 72;
    let polls = 0, state = '';
    for (; polls < maxPolls; polls++) {
      if (polls > 0) await sleep(this.http.pollIntervalMs ?? 5000);
      const st = await this.call('GET', `/api/client/statistics/${uuid}`);
      this.check(st, `${family}: статус отчёта`, 'CPO_PERF_STATUS');
      try { state = String((JSON.parse(st.text) as { state?: unknown }).state ?? ''); } catch { state = ''; }
      if (state === 'OK') break;
      if (state === 'ERROR') throw new LoaderError(`${family}: Ozon сформировал отчёт с ошибкой (${uuid})`, 'CPO_PERF_REPORT_ERROR');
    }
    // Очередь Ozon не уложилась — это не дефект данных: один повтор в слоте допустим.
    if (state !== 'OK') throw transient(`${family}: отчёт ${uuid} не готов за ${polls} опросов (state=${state || '?'})`, 504);
    this.logger?.info('ozon_cpo_report_ready', { family, uuid, polls: polls + 1 });
    const dl = await this.call('GET', `/api/client/statistics/report?${new URLSearchParams({ UUID: uuid })}`);
    this.check(dl, `${family}: скачивание отчёта`, 'CPO_PERF_DOWNLOAD');
    return { body: dl.text, uuid, polls: polls + 1 };
  }
}
