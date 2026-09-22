/**
 * Клиент «Календаря акций» WB (PR-PROMO-1). ТОЛЬКО чтение.
 *
 * В этом файле нет и не должно появиться ни одного пути, способного изменить
 * состояние маркетплейса. Мутирующий метод категории («добавить товар в акцию»)
 * сознательно не реализован и не упомянут даже строкой: отсутствие кода — более
 * надёжная гарантия, чем флаг, который можно переключить. Полный перечень
 * запрещённых путей и его проверка — в test/promo_security.test.ts.
 *
 * Дополнительная страховка: buildUrl() собирает адрес ТОЛЬКО из ALLOWED_PATHS
 * и роняет вызов на пути вне списка — до выхода в сеть.
 */
import { wbFetch } from '../../http/wbHttp.js';
import { LoaderError } from '../../errors.js';
import type { Logger } from '../../logging.js';
import {
  ALLOWED_PATHS,
  WB_PROMO_DETAILS_BATCH,
  WB_PROMO_DETAILS_PATH,
  WB_PROMO_LIST_PATH,
  WB_PROMO_MAX_PAGES,
  WB_PROMO_NOMENCLATURES_PATH,
  WB_PROMO_NOMENCLATURE_LIMIT,
  WB_PROMO_NOMENCLATURE_MAX_PAGES,
  WB_PROMO_PAGE_LIMIT,
  WB_PROMO_REQUEST_SPACING_MS,
} from './constants.js';

export type RawPromotion = Record<string, unknown>;
export type RawNomenclature = Record<string, unknown>;

export interface FetchOpts {
  timeoutMs: number;
  maxRetries: number;
  /** Пауза между запросами; в тестах 0. */
  spacingMs?: number;
}

export interface ListResult {
  promotions: RawPromotion[];
  httpStatus: number;
  attempts: number;
  pages: number;
}

export interface DetailsResult {
  details: RawPromotion[];
  httpStatus: number;
  attempts: number;
  batches: number;
}

/** Классы исхода запроса состава акции. HTTP 422 — ограничение способности, а не отказ. */
export type NomenclatureOutcome =
  | { kind: 'FETCHED'; items: RawNomenclature[]; attempts: number }
  | { kind: 'EMPTY'; attempts: number }
  | { kind: 'UNSUPPORTED_422'; attempts: number };

const sleep = (ms: number): Promise<void> => (ms > 0 ? new Promise((r) => setTimeout(r, ms)) : Promise.resolve());

/**
 * Единственная точка сборки адреса. Путь обязан быть из ALLOWED_PATHS —
 * иначе исключение до сети. Токен в сообщения об ошибках не попадает.
 */
export function buildUrl(host: string, path: string, query: string): string {
  if (!ALLOWED_PATHS.includes(path)) {
    throw new LoaderError(`Путь ${path} не входит в разрешённый список наблюдателя акций`, 'WB_PROMO_PATH_DENIED');
  }
  return `${host}${path}${query ? `?${query}` : ''}`;
}

function parseBody(body: string, what: string): unknown {
  try {
    return JSON.parse(body);
  } catch {
    throw new LoaderError(`WB ${what} вернул невалидный JSON`, 'WB_PROMO_BAD_JSON');
  }
}

function classifyHttp(status: number, what: string, body: string): LoaderError {
  if (status === 401 || status === 403) {
    return new LoaderError(`WB ${what}: HTTP ${status} — отказ авторизации`, 'WB_PROMO_AUTH');
  }
  if (status === 400) {
    return new LoaderError(`WB ${what}: HTTP 400 — некорректный запрос: ${body.slice(0, 200)}`, 'WB_PROMO_BAD_REQUEST');
  }
  if (status === 429) {
    return new LoaderError(`WB ${what}: HTTP 429 — лимит запросов исчерпан после ретраев`, 'WB_PROMO_RATE_LIMIT');
  }
  return new LoaderError(`WB ${what}: HTTP ${status}: ${body.slice(0, 200)}`, 'WB_PROMO_HTTP');
}

/** Список акций за окно. Пагинация offset += limit до неполной страницы. */
export async function fetchPromotions(
  host: string,
  token: string,
  windowFromIso: string,
  windowToIso: string,
  opts: FetchOpts,
  logger?: Logger,
): Promise<ListResult> {
  const promotions: RawPromotion[] = [];
  let offset = 0;
  let attempts = 0;
  let httpStatus = 0;
  let pages = 0;

  for (let page = 0; page < WB_PROMO_MAX_PAGES; page++) {
    const q =
      `startDateTime=${encodeURIComponent(windowFromIso)}&endDateTime=${encodeURIComponent(windowToIso)}` +
      `&allPromo=true&limit=${WB_PROMO_PAGE_LIMIT}&offset=${offset}`;
    const res = await wbFetch(
      buildUrl(host, WB_PROMO_LIST_PATH, q),
      { method: 'GET', headers: { Authorization: token } },
      opts,
      logger,
    );
    attempts += res.attempts;
    httpStatus = res.status;
    pages = page + 1;
    if (!res.ok) throw classifyHttp(res.status, '/calendar/promotions', res.body);

    const body = parseBody(res.body, '/calendar/promotions') as { data?: { promotions?: unknown } };
    const list = body.data?.promotions;
    if (!Array.isArray(list)) {
      throw new LoaderError('В ответе WB отсутствует data.promotions[]', 'WB_PROMO_SHAPE');
    }
    if (list.length === 0) break;
    promotions.push(...(list as RawPromotion[]));
    if (list.length < WB_PROMO_PAGE_LIMIT) break;
    offset += WB_PROMO_PAGE_LIMIT;
    await sleep(opts.spacingMs ?? WB_PROMO_REQUEST_SPACING_MS);
  }

  return { promotions, httpStatus, attempts, pages };
}

/**
 * Детали акций пачками по WB_PROMO_DETAILS_BATCH.
 * Пустой массив в ответе — штатное состояние завершённых акций, а не ошибка.
 */
export async function fetchPromotionDetails(
  host: string,
  token: string,
  promotionIds: number[],
  opts: FetchOpts,
  logger?: Logger,
): Promise<DetailsResult> {
  const details: RawPromotion[] = [];
  let attempts = 0;
  let httpStatus = 0;
  let batches = 0;
  if (promotionIds.length === 0) return { details, httpStatus, attempts, batches };

  for (let i = 0; i < promotionIds.length; i += WB_PROMO_DETAILS_BATCH) {
    const chunk = promotionIds.slice(i, i + WB_PROMO_DETAILS_BATCH);
    const q = chunk.map((id) => `promotionIDs=${id}`).join('&');
    const res = await wbFetch(
      buildUrl(host, WB_PROMO_DETAILS_PATH, q),
      { method: 'GET', headers: { Authorization: token } },
      opts,
      logger,
    );
    attempts += res.attempts;
    httpStatus = res.status;
    batches += 1;
    if (!res.ok) throw classifyHttp(res.status, '/calendar/promotions/details', res.body);

    const body = parseBody(res.body, '/calendar/promotions/details') as { data?: { promotions?: unknown } };
    const list = body.data?.promotions;
    if (!Array.isArray(list)) {
      throw new LoaderError('В ответе WB отсутствует data.promotions[] (details)', 'WB_PROMO_SHAPE');
    }
    details.push(...(list as RawPromotion[]));
    if (i + WB_PROMO_DETAILS_BATCH < promotionIds.length) {
      await sleep(opts.spacingMs ?? WB_PROMO_REQUEST_SPACING_MS);
    }
  }

  return { details, httpStatus, attempts, batches };
}

/**
 * Состав акции. Вызывается ТОЛЬКО для акций type != 'auto' — решение принимает
 * index.ts, здесь его не дублируем.
 *
 * HTTP 422 возвращается как исход UNSUPPORTED_422, а не как исключение:
 * площадка так отвечает, когда состав по этой акции отдать не может. Ронять
 * из-за этого весь снимок календаря нельзя — телеметрия сохранит событие.
 */
export async function fetchPromotionNomenclatures(
  host: string,
  token: string,
  promotionId: number,
  inAction: boolean,
  opts: FetchOpts,
  logger?: Logger,
): Promise<NomenclatureOutcome> {
  const items: RawNomenclature[] = [];
  let offset = 0;
  let attempts = 0;

  for (let page = 0; page < WB_PROMO_NOMENCLATURE_MAX_PAGES; page++) {
    const q =
      `promotionID=${promotionId}&inAction=${inAction ? 'true' : 'false'}` +
      `&limit=${WB_PROMO_NOMENCLATURE_LIMIT}&offset=${offset}`;
    const res = await wbFetch(
      buildUrl(host, WB_PROMO_NOMENCLATURES_PATH, q),
      { method: 'GET', headers: { Authorization: token } },
      opts,
      logger,
    );
    attempts += res.attempts;

    if (res.status === 422) {
      logger?.warn('wb_promo_nomenclature_unsupported', { promotionId, inAction, httpStatus: 422 });
      return { kind: 'UNSUPPORTED_422', attempts };
    }
    if (!res.ok) throw classifyHttp(res.status, '/calendar/promotions/nomenclatures', res.body);

    const body = parseBody(res.body, '/calendar/promotions/nomenclatures') as {
      data?: { nomenclatures?: unknown };
    };
    const list = body.data?.nomenclatures;
    if (!Array.isArray(list)) {
      throw new LoaderError('В ответе WB отсутствует data.nomenclatures[]', 'WB_PROMO_SHAPE');
    }
    if (list.length === 0) break;
    items.push(...(list as RawNomenclature[]));
    if (list.length < WB_PROMO_NOMENCLATURE_LIMIT) break;
    offset += WB_PROMO_NOMENCLATURE_LIMIT;
    await sleep(opts.spacingMs ?? WB_PROMO_REQUEST_SPACING_MS);
  }

  return items.length === 0 ? { kind: 'EMPTY', attempts } : { kind: 'FETCHED', items, attempts };
}
