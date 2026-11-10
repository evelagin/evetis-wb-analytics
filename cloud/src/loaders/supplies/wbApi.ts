/**
 * M5: supplies-api WB. Глаголы и пути — как в Apps Script (wbsList_/wbsCard_/wbsGoods_):
 *   список  — POST /api/v1/supplies?limit=1000&offset=0, тело {};
 *   карточка — GET  /api/v1/supplies/{supplyID};
 *   состав  — GET  /api/v1/supplies/{supplyID}/goods?limit=1000&offset=0.
 * Успех — только HTTP 200/204. 429/5xx/сеть — повторы в общем wbFetch.
 *
 * 🔴 Диагноз 100 % отказов Apps Script (31.10–10.11.2026): прогон умирал ДО первого запроса
 * в BigQuery, то есть на токене или на списке; в те же минуты статистика и реклама получали
 * 401 «token is expired». Поэтому отказ авторизации здесь — отдельный код WB_SUPPLIES_AUTH
 * (детерминированный, без повторов) из общего wbAuthError, а срок токена проверяется ДО запроса
 * общим assertWbTokenUsable (cloud/src/http/wbToken.ts, PR #423) — в index.ts.
 */
import { wbFetch, type WbHttpOptions, type WbHttpResult } from '../../http/wbHttp.js';
import { isWbAuthStatus, redactTokenLike, wbAuthError } from '../../http/wbToken.js';
import type { Logger } from '../../logging.js';
import { LoaderError } from '../../errors.js';
import { WB_SUPPLIES_PAGE_LIMIT, WB_SUPPLIES_SECRET } from './constants.js';

/** Префикс кодов потока: WB_SUPPLIES_AUTH / WB_SUPPLIES_TOKEN_EXPIRED. */
export const WB_SUPPLIES_CODE_PREFIX = 'WB_SUPPLIES';

type Obj = Record<string, unknown>;
export type Fetcher = (url: string, init: RequestInit, opts: WbHttpOptions, logger?: Logger) => Promise<WbHttpResult>;

export interface SuppliesApi {
  list(): Promise<Obj[]>;
  card(supplyId: number): Promise<Obj | null>;
  goods(supplyId: number): Promise<Obj[]>;
}

/** Строки ответа: массив или { data: [...] } (wbsRows_). Иное — null. */
export function rowsOf(json: unknown): Obj[] | null {
  if (Array.isArray(json)) return json as Obj[];
  const d = (json as { data?: unknown } | null)?.data;
  return Array.isArray(d) ? (d as Obj[]) : null;
}

function parse(body: string): unknown {
  try { return JSON.parse(body); } catch { return null; }
}

export class WbSuppliesApi implements SuppliesApi {
  constructor(
    private readonly host: string,
    private readonly token: string,
    private readonly opts: WbHttpOptions,
    private readonly logger?: Logger,
    private readonly fetcher: Fetcher = wbFetch,
    /** Имя секрета — только для текста ошибки; значение токена в ошибки и лог не попадает. */
    private readonly secretName: string = WB_SUPPLIES_SECRET,
  ) {}

  private async call(method: 'GET' | 'POST', path: string, what: string): Promise<unknown> {
    const init: RequestInit = method === 'POST'
      ? { method, headers: { Authorization: this.token, 'Content-Type': 'application/json' }, body: '{}' }
      : { method, headers: { Authorization: this.token } };
    const r = await this.fetcher(this.host + path, init, this.opts, this.logger);
    if (r.status === 200 || r.status === 204) return parse(r.body);
    if (isWbAuthStatus(r.status)) throw wbAuthError(WB_SUPPLIES_CODE_PREFIX, this.secretName, r.status, `supplies ${what}`, r.body);
    const tail = redactTokenLike(r.body.slice(0, 200).replace(/\s+/g, ' '));
    if (r.status === 0) throw new LoaderError(`WB supplies ${what}: сеть — ${r.error ?? 'нет ответа'}`, 'WB_SUPPLIES_NETWORK');
    throw new LoaderError(`WB supplies ${what}: HTTP ${r.status} :: ${tail}`, 'WB_SUPPLIES_HTTP');
  }

  async list(): Promise<Obj[]> {
    const rows = rowsOf(await this.call('POST', `/api/v1/supplies?limit=${WB_SUPPLIES_PAGE_LIMIT}&offset=0`, 'список'));
    if (!rows) throw new LoaderError('Список поставок не разобрался', 'WB_SUPPLIES_SHAPE');
    if (rows.length >= WB_SUPPLIES_PAGE_LIMIT) {
      throw new LoaderError(`Выборка усечена на ${WB_SUPPLIES_PAGE_LIMIT} — нужен проход по offset, загрузка остановлена`, 'WB_SUPPLIES_TRUNCATED');
    }
    return rows;
  }

  /** Карточка: неразобранный JSON → null (card_read_ok=FALSE), как `r.json || null`. */
  async card(supplyId: number): Promise<Obj | null> {
    const j = await this.call('GET', `/api/v1/supplies/${supplyId}`, `карточка ${supplyId}`);
    return j && typeof j === 'object' ? (j as Obj) : null;
  }

  /** 🔴 Неразобранный состав — отказ, а не «0 позиций» (иначе commit-шапка стёрла бы состав). */
  async goods(supplyId: number): Promise<Obj[]> {
    const rows = rowsOf(await this.call('GET', `/api/v1/supplies/${supplyId}/goods?limit=${WB_SUPPLIES_PAGE_LIMIT}&offset=0`, `состав ${supplyId}`));
    if (!rows) {
      throw new LoaderError(`Состав поставки ${supplyId} не разобрался как массив — структура ответа изменилась, загрузка остановлена`, 'WB_SUPPLIES_SHAPE');
    }
    if (rows.length >= WB_SUPPLIES_PAGE_LIMIT) {
      throw new LoaderError(`Состав поставки ${supplyId} усечён на ${WB_SUPPLIES_PAGE_LIMIT} — загрузка остановлена`, 'WB_SUPPLIES_TRUNCATED');
    }
    return rows;
  }
}

