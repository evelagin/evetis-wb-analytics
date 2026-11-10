/**
 * M2 — транспорт statistics-api и выгрузки заказов/продаж. Порт:
 *   wbFetchWithRetry_ (Utils.gs:157) — повтор 429/5xx, приоритет Retry-After, иначе backoff;
 *   fetchOrdersApiData_ (WbOrdersLoader.gs:292) — пагинация по lastChangeDate;
 *   fetchSalesApiData_ (WbSalesReturnsLoader.gs.gs:293) — ровно один запрос.
 * fetch и sleep инъектируются — тесты идут без сети. Токен не логируется, URL логируется без query.
 */
import type { Logger } from '../../logging.js';
import {
  STATS_HTTP_BASE_DELAY_MS, STATS_HTTP_MAX_DELAY_MS, STATS_HTTP_MAX_RETRIES, STATS_HTTP_RETRY_CODES,
  ORDERS_MAX_PAGES, ORDERS_PAGE_PAUSE_MS, ORDERS_ROWS_CAP, SALES_ROWS_CAP,
} from './constants.js';

export type WbRow = Record<string, unknown>;

export interface StatsTransport {
  fetch: (url: string, init: { method: string; headers: Record<string, string>; signal?: AbortSignal }) => Promise<{
    status: number;
    text: () => Promise<string>;
    headers: { get: (name: string) => string | null };
  }>;
  sleep: (ms: number) => Promise<void>;
  nowMs: () => number;
}

export const defaultTransport: StatsTransport = {
  fetch: (url, init) => fetch(url, init),
  sleep: (ms) => new Promise((r) => setTimeout(r, ms)),
  nowMs: () => Date.now(),
};

export interface StatsHttpResult {
  /** HTTP-код последнего ответа; 0 — сетевое исключение после всех повторов. */
  status: number;
  body: string;
  attempts: number;
  /** Текст сетевой ошибки (только при status=0). */
  error?: string;
}

/** wbRetryAfterMs_: секунды или HTTP-дата; -1 — заголовка нет или он не распознан. */
export function retryAfterMs(header: string | null, nowMs: number): number {
  const ra = String(header ?? '').trim();
  if (!ra) return -1;
  if (/^\d+$/.test(ra)) return parseInt(ra, 10) * 1000;
  const when = Date.parse(ra);
  if (!Number.isNaN(when)) return Math.max(0, when - nowMs);
  return -1;
}

/**
 * wbFetchWithRetry_ с параметрами вызова заказов/продаж: maxRetries=3, base=20 с, cap=60 с,
 * коды 429/500/502/503/504. Пауза = Retry-After, иначе base·2^(n−1), не больше cap.
 * Исчерпанный повтор возвращает ПОСЛЕДНИЙ ответ (решение — у вызывающего, fail-closed).
 */
export async function statsGet(
  url: string, token: string, timeoutMs: number, t: StatsTransport, logger?: Logger, label = 'WB',
): Promise<StatsHttpResult> {
  const safeUrl = url.split('?')[0];
  for (let attempt = 1; ; attempt++) {
    let status = 0;
    let body = '';
    let raHeader: string | null = null;
    let errMsg = '';
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const resp = await t.fetch(url, { method: 'GET', headers: { Authorization: token }, signal: controller.signal });
      status = resp.status;
      body = await resp.text();
      raHeader = resp.headers.get('Retry-After');
    } catch (e) {
      errMsg = e instanceof Error ? e.message : String(e);
    } finally {
      clearTimeout(timer);
    }
    const threw = status === 0;
    if (!threw && !STATS_HTTP_RETRY_CODES.has(status)) return { status, body, attempts: attempt };
    if (attempt > STATS_HTTP_MAX_RETRIES) {
      return threw ? { status: 0, body: '', attempts: attempt, error: errMsg } : { status, body, attempts: attempt };
    }
    let pause = threw ? -1 : retryAfterMs(raHeader, t.nowMs());
    if (pause < 0) pause = STATS_HTTP_BASE_DELAY_MS * Math.pow(2, attempt - 1);
    pause = Math.min(STATS_HTTP_MAX_DELAY_MS, pause);
    logger?.warn('wb_stats_http_retry', {
      label, url: safeUrl, attempt, status, waitMs: pause, ...(threw ? { error: errMsg } : {}),
    });
    await t.sleep(pause);
  }
}

// ─────────────────────────────────────────────────────────────────────────
// Заказы: пагинация по lastChangeDate (fetchOrdersApiData_)
// ─────────────────────────────────────────────────────────────────────────

export interface OrdersFetchResult {
  ok: boolean;
  /** PARTIAL: данные могли не долиться (≥1 страница получена, затем сбой; упор в лимит). */
  partial: boolean;
  error?: string;
  /** Последнее состояние каждой srid в порядке первого появления (last-wins по lastChangeDate). */
  data: WbRow[];
  pages: number;
  apiRowsReceived: number;
  /** Точное lastChangeDate последней строки последней непустой страницы — кандидат watermark. */
  cursorEnd: string;
}

/**
 * Контракт WB (flag=0): dateFrom трактуется как lastChangeDate, возвращаются записи с
 * lastChangeDate ≥ dateFrom (до ~80 000 строк). Следующая страница — dateFrom = lastChangeDate
 * последней строки ответа без смены формата. Конец — пустой массив.
 * Признаки завершения 1:1 с Apps Script (см. комментарий в WbOrdersLoader.gs:282-290).
 */
export async function fetchOrders(
  host: string, path: string, token: string, dateFrom: string, timeoutMs: number,
  t: StatsTransport, logger?: Logger,
): Promise<OrdersFetchResult> {
  let cursor = dateFrom;
  let cursorEnd = '';
  let apiRows = 0;
  let pages = 0;
  let reachedEnd = false;
  let stalled = false;
  const byKey = new Map<string, WbRow>();
  const order: string[] = [];
  const snapshot = () => order.map((k) => byKey.get(k)!);
  const fail = (partial: boolean, error: string): OrdersFetchResult => ({
    ok: false, partial, error, data: snapshot(), pages, apiRowsReceived: apiRows, cursorEnd,
  });

  while (pages < ORDERS_MAX_PAGES) {
    const url = `${host}${path}?dateFrom=${encodeURIComponent(cursor)}&flag=0`;
    const resp = await statsGet(url, token, timeoutMs, t, logger, 'Orders');
    if (resp.status === 0) return fail(pages > 0, `HTTP исключение: ${resp.error ?? ''}`);
    if (resp.status !== 200) return fail(pages > 0, `HTTP ${resp.status}: ${resp.body.substring(0, 200)}`);
    let arr: unknown;
    try {
      arr = JSON.parse(resp.body);
    } catch (e) {
      return fail(pages > 0, `Повреждённый JSON ответа WB (страница ${pages + 1}): ${e instanceof Error ? e.message : String(e)}`);
    }
    if (!Array.isArray(arr)) {
      return fail(pages > 0, `Ответ WB не является массивом (страница ${pages + 1}): ${resp.body.substring(0, 200)}`);
    }
    if (arr.length === 0) { reachedEnd = true; break; }
    apiRows += arr.length;

    let progress = 0;
    for (const raw of arr) {
      const o = (raw ?? {}) as WbRow;
      const key = o.srid ? String(o.srid) : `${String(o.gNumber ?? '')}_${String(o.date ?? '')}`;
      const lcd = String(o.lastChangeDate ?? '');
      const prev = byKey.get(key);
      if (!prev) {
        byKey.set(key, o); order.push(key); progress++;
      } else if (lcd > String(prev.lastChangeDate ?? '')) {
        byKey.set(key, o); progress++;
      }
    }
    pages++;

    const atCap = arr.length >= ORDERS_ROWS_CAP;
    const last = (arr[arr.length - 1] ?? {}) as WbRow;
    const nextCursor = String(last.lastChangeDate ?? '');
    if (nextCursor) cursorEnd = nextCursor;
    if (!nextCursor) { stalled = true; break; }
    if (nextCursor === cursor) {
      if (!atCap) reachedEnd = true; else stalled = true;
      break;
    }
    if (progress === 0 && !atCap) { reachedEnd = true; break; }
    cursor = nextCursor;
    await t.sleep(ORDERS_PAGE_PAUSE_MS);
  }

  if (!reachedEnd) {
    const why = stalled
      ? `Незавершённая выгрузка: пустой cursor_end при непустом ответе ИЛИ граница на лимите строк (${ORDERS_ROWS_CAP})`
      : `Достигнут лимит страниц (${ORDERS_MAX_PAGES})`;
    return fail(true, `${why}: данные за период могут быть НЕПОЛНЫМИ.`);
  }
  return { ok: true, partial: false, data: snapshot(), pages, apiRowsReceived: apiRows, cursorEnd };
}

// ─────────────────────────────────────────────────────────────────────────
// Продажи: один fail-closed запрос (fetchSalesApiData_)
// ─────────────────────────────────────────────────────────────────────────

export interface SalesFetchResult {
  ok: boolean;
  partial: boolean;
  error?: string;
  data: WbRow[];
  apiRowsReceived: number;
}

export async function fetchSales(
  host: string, path: string, token: string, dateFrom: string, timeoutMs: number,
  t: StatsTransport, logger?: Logger,
): Promise<SalesFetchResult> {
  const url = `${host}${path}?dateFrom=${encodeURIComponent(dateFrom)}&flag=0`;
  const resp = await statsGet(url, token, timeoutMs, t, logger, 'Sales');
  if (resp.status === 0) return { ok: false, partial: false, error: `HTTP исключение: ${resp.error ?? ''}`, data: [], apiRowsReceived: 0 };
  if (resp.status !== 200) {
    return { ok: false, partial: false, error: `HTTP ${resp.status}: ${resp.body.substring(0, 200)}`, data: [], apiRowsReceived: 0 };
  }
  let arr: unknown;
  try {
    arr = JSON.parse(resp.body);
  } catch (e) {
    return { ok: false, partial: false, error: `Повреждённый JSON ответа WB: ${e instanceof Error ? e.message : String(e)}`, data: [], apiRowsReceived: 0 };
  }
  if (!Array.isArray(arr)) {
    return { ok: false, partial: false, error: `Ответ WB не является массивом: ${resp.body.substring(0, 200)}`, data: [], apiRowsReceived: 0 };
  }
  if (arr.length >= SALES_ROWS_CAP) {
    return {
      ok: false, partial: true, data: [], apiRowsReceived: arr.length,
      error: `Ответ упёрся в лимит ${SALES_ROWS_CAP} строк — выгрузка может быть НЕПОЛНОЙ. Ничего не записано.`,
    };
  }
  return { ok: true, partial: false, data: arr as WbRow[], apiRowsReceived: arr.length };
}
