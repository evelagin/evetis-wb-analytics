/** WB Stocks API (T6 + T5). Транспорт — общий wbHttp (retry 429/5xx/timeout). */
import { wbFetch, type WbHttpOptions } from '../../http/wbHttp.js';
import { Logger } from '../../logging.js';
import { LoaderError } from '../../errors.js';
import { extractT6Array } from './normalize.js';
import { extractT5Array, extractT5TaskId, t5TaskState } from './t5.js';
import { WB_STOCKS_T6_PATH, WB_STOCKS_T5_PATH, WB_STOCKS_T5_CREATE_QUERY } from './constants.js';

/** T6: POST currentPeriod=from..to. Возвращает массив строк остатков. */
export async function fetchStocksT6(
  host: string,
  token: string,
  from: string,
  to: string,
  opts: WbHttpOptions,
  logger?: Logger,
): Promise<unknown[]> {
  const url = host + WB_STOCKS_T6_PATH;
  const body = { currentPeriod: { start: from, end: to }, stockType: '', skipDeletedNm: false };
  const res = await wbFetch(
    url,
    { method: 'POST', headers: { Authorization: token, 'Content-Type': 'application/json' }, body: JSON.stringify(body) },
    opts,
    logger,
  );
  if (!res.ok) throw new LoaderError(`T6 HTTP ${res.status}: ${res.body.slice(0, 200)}`, 'WB_T6_HTTP');
  let json: unknown;
  try {
    json = JSON.parse(res.body);
  } catch {
    throw new LoaderError('T6: ответ не JSON', 'WB_T6_PARSE');
  }
  const arr = extractT6Array(json);
  if (!arr) throw new LoaderError('T6: неожиданная форма ответа (не массив/data.items)', 'WB_T6_SHAPE');
  return arr;
}

export type T5FetchResult = { ok: true; data: unknown[] } | { ok: false; error: string };

export interface T5PollOptions {
  intervalMs: number;
  maxPolls: number;
  /** Общий бюджет T5, мс: по исчерпании — T5_UNAVAILABLE, а не убитый по таймауту Job. */
  budgetMs: number;
  /** Инъекция часов для тестов. */
  now?: () => number;
  /** Инъекция для тестов; по умолчанию — настоящий таймер. */
  sleep?: (ms: number) => Promise<void>;
}

/**
 * PR-Mig1c. T5 warehouse_remains — задача: create → status (до maxPolls, пауза ПЕРЕД
 * каждым опросом) → download. Зеркало wbStocksT5PhysicalSum_ (WbStocksSnapshot.gs:161-204).
 *
 * НЕ бросает: T5 — неблокирующий контроль. Любой сбой возвращается как {ok:false} и
 * превращается вызывающим в control_status='T5_UNAVAILABLE'; снимок T6 при этом пишется.
 * Неуспешный ответ status-опроса не прерывает ожидание (как в Apps Script) — решает только
 * явный статус задачи, исчерпание опросов или общий бюджет времени (poll.budgetMs).
 * Повторы 429 — как в Apps Script: пауза 21 с, до 3 раз (передаётся вызывающим через opts).
 */
export async function fetchStocksT5(
  host: string,
  token: string,
  opts: WbHttpOptions,
  poll: T5PollOptions,
  logger?: Logger,
): Promise<T5FetchResult> {
  const sleep = poll.sleep ?? ((ms: number) => new Promise<void>((r) => setTimeout(r, ms)));
  const now = poll.now ?? (() => Date.now());
  const deadline = now() + poll.budgetMs;
  const base = host + WB_STOCKS_T5_PATH;
  const get = (url: string) => wbFetch(url, { method: 'GET', headers: { Authorization: token } }, opts, logger);
  const parse = (body: string): unknown => {
    try {
      return JSON.parse(body);
    } catch {
      return null;
    }
  };

  try {
    const cr = await get(base + WB_STOCKS_T5_CREATE_QUERY);
    if (!cr.ok) return { ok: false, error: `T5 create: HTTP ${cr.status}: ${cr.body.slice(0, 200)}` };
    const taskId = extractT5TaskId(parse(cr.body));
    if (!taskId) return { ok: false, error: 'T5: нет taskId в ответе create' };

    let ready = false;
    for (let p = 0; p < poll.maxPolls; p++) {
      if (now() + poll.intervalMs > deadline) return { ok: false, error: 'T5: исчерпан бюджет времени' };
      await sleep(poll.intervalMs);
      const sr = await get(`${base}/tasks/${encodeURIComponent(taskId)}/status`);
      const st = t5TaskState(sr.ok ? parse(sr.body) : null);
      if (st.kind === 'ready') {
        ready = true;
        break;
      }
      if (st.kind === 'failed') return { ok: false, error: `T5 задача ${st.status}` };
    }
    if (!ready) return { ok: false, error: 'T5: таймаут ожидания задачи' };

    const dl = await get(`${base}/tasks/${encodeURIComponent(taskId)}/download`);
    if (!dl.ok) return { ok: false, error: `T5 download: HTTP ${dl.status}: ${dl.body.slice(0, 200)}` };
    const json = parse(dl.body);
    if (!Array.isArray(json)) logger?.warn('stocks_t5_download_not_array', { taskId });
    return { ok: true, data: extractT5Array(json) };
  } catch (e) {
    return { ok: false, error: `T5: ${e instanceof Error ? e.message : String(e)}` };
  }
}
