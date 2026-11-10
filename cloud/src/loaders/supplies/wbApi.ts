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
 * (детерминированный, без повторов), а срок токена проверяется ДО запроса (tokenExpiry).
 */
import { wbFetch, type WbHttpOptions, type WbHttpResult } from '../../http/wbHttp.js';
import type { Logger } from '../../logging.js';
import { LoaderError } from '../../errors.js';
import { WB_SUPPLIES_PAGE_LIMIT } from './constants.js';

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
  ) {}

  private async call(method: 'GET' | 'POST', path: string, what: string): Promise<unknown> {
    const init: RequestInit = method === 'POST'
      ? { method, headers: { Authorization: this.token, 'Content-Type': 'application/json' }, body: '{}' }
      : { method, headers: { Authorization: this.token } };
    const r = await this.fetcher(this.host + path, init, this.opts, this.logger);
    if (r.status === 200 || r.status === 204) return parse(r.body);
    const tail = r.body.slice(0, 200).replace(/\s+/g, ' ');
    if (r.status === 401 || r.status === 403) {
      throw new LoaderError(`WB supplies ${what}: HTTP ${r.status} — токен WB_TOKEN_SUPPLIES отклонён (истёк или без категории «Поставки»): ${tail}`, 'WB_SUPPLIES_AUTH');
    }
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

/**
 * Срок токена WB из claim `exp` (токен — JWT). Значение токена не логируется и не возвращается.
 * null — срок не читается (не JWT / нет exp): проверка пропускается, решает ответ WB.
 */
export function tokenExpiryMs(token: string): number | null {
  const parts = token.trim().replace(/^Bearer\s+/i, '').split('.');
  if (parts.length !== 3) return null;
  try {
    const payload = JSON.parse(Buffer.from(parts[1]!.replace(/-/g, '+').replace(/_/g, '/'), 'base64').toString('utf8')) as { exp?: unknown };
    const exp = Number(payload.exp);
    return Number.isFinite(exp) && exp > 0 ? exp * 1000 : null;
  } catch {
    return null;
  }
}
