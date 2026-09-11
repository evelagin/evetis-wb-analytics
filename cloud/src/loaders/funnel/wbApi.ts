/**
 * Воронка продаж WB — синхронный метод «Статистика карточек товаров по дням».
 *
 * UNITKA 2.0 R7 (11.09.2026). До R7 здесь был асинхронный CSV-отчёт
 * DETAIL_HISTORY_REPORT (POST /api/v2/nm-report/downloads). WB отдаёт его ТОЛЬКО
 * с подпиской «Джем»; у EVETIS её нет (подтверждено владельцем 11.09.2026), поэтому
 * отчёт отвечал 403 «Report not available» на любом токене — дело было не в правах
 * токена. Метод ниже доступен без Джема, но у него свои ограничения:
 *   - глубина — максимум последняя неделя;
 *   - от 1 до 20 nmID на запрос;
 *   - ответ — JSON, а не ZIP с CSV.
 * Контракт сверен с dev.wildberries.ru/docs/openapi/analytics 11.09.2026.
 */
import { wbFetch, type WbHttpOptions } from '../../http/wbHttp.js';
import { LoaderError } from '../../errors.js';
import type { Logger } from '../../logging.js';
import { parseHistoryBody, type HistoryItem } from './normalize.js';

export const HISTORY_PATH = '/api/analytics/v3/sales-funnel/products/history';
/** WB: nmIds — от 1 до 20 на запрос. */
export const MAX_NM_PER_REQUEST = 20;
/** WB: без Джема — данные максимум за последнюю неделю. */
export const MAX_DEPTH_DAYS = 7;

export interface FunnelWindow {
  /** YYYY-MM-DD, включительно. */
  startDate: string;
  endDate: string;
}

export function chunk<T>(xs: readonly T[], size: number): T[][] {
  const out: T[][] = [];
  for (let i = 0; i < xs.length; i += size) out.push(xs.slice(i, i + size));
  return out;
}

export async function fetchHistory(
  host: string, token: string, w: FunnelWindow, nmIds: number[],
  opts: WbHttpOptions, logger?: Logger,
): Promise<{ httpStatus: number; items: HistoryItem[] }> {
  const body = {
    selectedPeriod: { start: w.startDate, end: w.endDate },
    nmIds,
    skipDeletedNm: false,
    aggregationLevel: 'day',
  };
  const res = await wbFetch(`${host}${HISTORY_PATH}`, {
    method: 'POST',
    headers: { Authorization: token, 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }, opts, logger);
  if (!res.ok) {
    // Тело ответа WB токена не содержит и нужно для разбора 400/403.
    const detail = res.status === 0 ? (res.error ?? 'network') : res.body.slice(0, 500);
    throw new LoaderError(`POST ${HISTORY_PATH} -> HTTP ${res.status}: ${detail}`, 'WB_FUNNEL_HTTP_FAILED');
  }
  return { httpStatus: res.status, items: parseHistoryBody(res.body) };
}
