/**
 * Клиент WB «Цены и скидки» (PR-1). ТОЛЬКО чтение.
 *
 * В этом файле нет и не должно появиться ни одного пути, способного изменить
 * состояние маркетплейса. Мутирующие методы категории (/api/v2/upload/task и
 * производные) сознательно не реализованы: отсутствие кода — более надёжная
 * гарантия, чем флаг, который можно переключить.
 */
import { wbFetch } from '../../http/wbHttp.js';
import { LoaderError } from '../../errors.js';
import type { Logger } from '../../logging.js';
import { WB_PRICES_PATH, WB_PRICES_PAGE_LIMIT, WB_PRICES_MAX_PAGES } from './constants.js';

/** Сырой элемент listGoods[] — намеренно нетипизирован: контракт проверяется в normalize. */
export type RawGoodsItem = Record<string, unknown>;

export interface FetchGoodsResult {
  items: RawGoodsItem[];
  httpStatus: number;
  attempts: number;
  pages: number;
}

export interface FetchOpts {
  timeoutMs: number;
  maxRetries: number;
}

/**
 * Постранично забирает все товары с ценами.
 *
 * Пагинация по контракту WB: повторять с offset += limit до пустого массива.
 * Пустая страница — штатное завершение, а не ошибка.
 */
export async function fetchGoodsPrices(
  host: string,
  token: string,
  opts: FetchOpts,
  logger?: Logger,
): Promise<FetchGoodsResult> {
  const items: RawGoodsItem[] = [];
  let offset = 0;
  let attempts = 0;
  let httpStatus = 0;
  let pages = 0;

  for (let page = 0; page < WB_PRICES_MAX_PAGES; page++) {
    const url = `${host}${WB_PRICES_PATH}?limit=${WB_PRICES_PAGE_LIMIT}&offset=${offset}`;
    const res = await wbFetch(url, { method: 'GET', headers: { Authorization: token } }, opts, logger);
    attempts += res.attempts;
    httpStatus = res.status;
    pages = page + 1;

    if (!res.ok) {
      // Токен в сообщение не попадает: наружу отдаём только статус и усечённое тело.
      throw new LoaderError(
        `WB /list/goods/filter вернул HTTP ${res.status}: ${res.body.slice(0, 300)}`,
        res.status === 401 || res.status === 403 ? 'WB_PRICES_AUTH' : 'WB_PRICES_HTTP',
      );
    }

    let parsed: unknown;
    try {
      parsed = JSON.parse(res.body);
    } catch {
      throw new LoaderError('WB /list/goods/filter вернул невалидный JSON', 'WB_PRICES_BAD_JSON');
    }

    const body = parsed as { error?: unknown; errorText?: unknown; data?: { listGoods?: unknown } };
    if (body.error === true) {
      throw new LoaderError(`WB вернул error=true: ${String(body.errorText ?? '')}`, 'WB_PRICES_API_ERROR');
    }
    const list = body.data?.listGoods;
    if (!Array.isArray(list)) {
      // Отсутствие массива — дрейф контракта, а не «пустой ассортимент».
      throw new LoaderError('В ответе WB отсутствует data.listGoods[]', 'WB_PRICES_SHAPE');
    }
    if (list.length === 0) break;

    items.push(...(list as RawGoodsItem[]));
    if (list.length < WB_PRICES_PAGE_LIMIT) break;
    offset += WB_PRICES_PAGE_LIMIT;
  }

  return { items, httpStatus, attempts, pages };
}
