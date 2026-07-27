/** WB Stocks API (T6). Транспорт — общий wbHttp (retry 429/5xx/timeout). */
import { wbFetch, type WbHttpOptions } from '../../http/wbHttp.js';
import { Logger } from '../../logging.js';
import { LoaderError } from '../../errors.js';
import { extractT6Array } from './normalize.js';
import { WB_STOCKS_T6_PATH } from './constants.js';

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
