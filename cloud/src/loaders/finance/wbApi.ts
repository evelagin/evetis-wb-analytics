/**
 * M4 · Клиент Finance API WB: пейсер 1 запрос/мин, бюджет прогона, BigInt-безопасный разбор.
 * Порт finDailyPost_/finDailyList_/finDailyFetchDetailed_ (Wbfinancedaily.gs).
 *
 * Пейсер: ≥ 61 с между ЛЮБЫМИ запросами к Finance API, включая повторы. В Apps Script метка
 * последнего запроса жила в ScriptProperties (общая с legacy-кодом); здесь — в памяти прогона,
 * а сериализацию прогонов обеспечивает lease LOADER_RUNS. Межпрогонный зазор (два прогона подряд)
 * и чужой клиент того же токена (Apps Script в shadow-фазе) закрываются повтором на 429 с паузой
 * по заголовку WB (ОТЛИЧИЕ, см. runbook §3).
 *
 * Токен передаётся в заголовке Authorization как есть (без Bearer) — как в Apps Script.
 * Значение токена не логируется.
 */
import type { Logger } from '../../logging.js';
import {
  FIN_API_LIST_PATH, FIN_API_DET_PATH, FIN_PACER_MS, FIN_REQ_MARGIN_MS, FIN_LIST_LIMIT, FIN_PAGE_LIMIT,
  FIN_LIST_PAGE_GUARD, FIN_DET_PAGE_GUARD, FIN_HTTP_MAX_RETRIES, FIN_RETRY_AFTER_CAP_MS, FIN_BQ_TAIL_MS,
} from './constants.js';
import { parseSafe, isDigits } from './parse.js';
import { parseListRow, type ListReport } from './normalize.js';

export interface Clock {
  now(): number;
  sleep(ms: number): Promise<void>;
}

export const realClock: Clock = {
  now: () => Date.now(),
  sleep: (ms) => new Promise((r) => setTimeout(r, ms)),
};

export interface HttpResponseLike {
  status: number;
  text(): Promise<string>;
  headers: { get(name: string): string | null };
}
export type FetchFn = (url: string, init: { method: string; headers: Record<string, string>; body: string; signal: AbortSignal }) => Promise<HttpResponseLike>;

export interface FinanceApiOptions {
  host: string;
  token: string;
  /** Момент старта прогона (мс) — от него считается бюджет. */
  t0: number;
  budgetMs: number;
  timeoutMs: number;
  maxRetries?: number;
  /** Резерв на хвост BigQuery после запроса (по умолчанию FIN_BQ_TAIL_MS). */
  bqTailMs?: number;
}

export interface PostResult { code: number; body: string }
export interface ListResult { ok: boolean; reports: ListReport[]; error: string }
export interface DetailedResult { ok: boolean; rows: Array<Record<string, unknown>>; pages: number; error: string }

export function isRetryableFinanceStatus(code: number): boolean {
  return code === 0 || code === 429 || (code >= 500 && code <= 599);
}

/** Пауза, которую просит WB: X-Ratelimit-Retry (сек) или Retry-After (сек). Нет/мусор → 0. */
export function retryAfterMs(h: { get(name: string): string | null }): number {
  for (const name of ['X-Ratelimit-Retry', 'Retry-After']) {
    const v = h.get(name);
    if (v !== null && /^\d+(\.\d+)?$/.test(v.trim())) return Math.min(Math.ceil(Number(v) * 1000), FIN_RETRY_AFTER_CAP_MS);
  }
  return 0;
}

export class FinanceApi {
  requests = 0;
  private lastReqAt = 0;
  /** Не раньше этого момента (мс): пауза, которую попросил WB в ответе 429. */
  private notBefore = 0;

  constructor(
    private readonly o: FinanceApiOptions,
    private readonly clock: Clock,
    private readonly fetchFn: FetchFn,
    private readonly logger?: Logger,
  ) {}

  budgetLeft(): number {
    return this.o.budgetMs - (this.clock.now() - this.o.t0);
  }

  /** Ожидание до ближайшего разрешённого запроса, мс (finDailyProjectedWait_). */
  projectedWait(): number {
    const now = this.clock.now();
    const byPacer = this.lastReqAt ? Math.max(0, FIN_PACER_MS - (now - this.lastReqAt)) : 0;
    return Math.max(byPacer, Math.max(0, this.notBefore - now));
  }

  /**
   * finDailyCanRequest_: бюджет > будущее ожидание пейсера + маржа. ОТЛИЧИЕ: сверх маржи Apps Script (30 с)
   * резервируются таймаут самого запроса и хвост BigQuery — начатый запрос всегда успевает дойти до COMPLETE.
   */
  canRequest(): boolean {
    const reserve = FIN_REQ_MARGIN_MS + this.o.timeoutMs + (this.o.bqTailMs ?? FIN_BQ_TAIL_MS);
    return this.budgetLeft() > this.projectedWait() + reserve;
  }

  private async reserve(): Promise<void> {
    for (let g = 0; g < 5; g++) {
      const wait = this.projectedWait();
      if (wait > 0) { await this.clock.sleep(wait); continue; }
      this.lastReqAt = this.clock.now();
      return;
    }
    throw new Error('Finance API pacer: слот не зарезервирован');
  }

  /** Один POST через пейсер, с повтором 429/5xx/сети. Отказ после повторов — код ≠ 200 (не исключение). */
  async post(path: string, payload: string): Promise<PostResult> {
    const maxRetries = this.o.maxRetries ?? FIN_HTTP_MAX_RETRIES;
    let last: PostResult = { code: 0, body: '' };
    for (let attempt = 1; attempt <= maxRetries + 1; attempt++) {
      if (attempt > 1 && !this.canRequest()) {
        this.logger?.warn('finance_retry_budget_exhausted', { path: redactPath(path), attempt, code: last.code });
        return last;
      }
      await this.reserve();
      this.requests++;
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), this.o.timeoutMs);
      try {
        const resp = await this.fetchFn(this.o.host + path, {
          method: 'POST',
          headers: { Authorization: this.o.token, 'Content-Type': 'application/json' },
          body: payload,
          signal: controller.signal,
        });
        const body = await resp.text();
        last = { code: resp.status, body };
        if (!isRetryableFinanceStatus(resp.status)) return last;
        const ra = retryAfterMs(resp.headers);
        if (ra > 0) this.notBefore = Math.max(this.notBefore, this.clock.now() + ra);
        this.logger?.warn('finance_http_retryable', { status: resp.status, attempt, retryAfterMs: ra, path: redactPath(path) });
      } catch (e) {
        last = { code: 0, body: `network: ${e instanceof Error ? e.message : String(e)}` };
        this.logger?.warn('finance_http_network', { attempt, error: last.body, path: redactPath(path) });
      } finally {
        clearTimeout(timer);
      }
    }
    return last;
  }

  /** finDailyList_ — list за период с offset-пагинацией и fail-closed валидацией строк. */
  async list(period: 'daily' | 'weekly', dateFrom: string, dateTo: string): Promise<ListResult> {
    const out: ListResult = { ok: true, reports: [], error: '' };
    let offset = 0;
    for (let guard = 0; guard < FIN_LIST_PAGE_GUARD; guard++) {
      if (!this.canRequest()) return { ...out, ok: false, error: 'BUDGET_EXCEEDED (list)' };
      const payload = JSON.stringify({ dateFrom, dateTo, period, limit: FIN_LIST_LIMIT, offset });
      const r = await this.post(FIN_API_LIST_PATH, payload);
      if (r.code === 204) return out; // пусто — норма
      if (r.code !== 200) return { ...out, ok: false, error: `list HTTP ${r.code}: ${r.body.substring(0, 200)}` };
      const p = parseSafe(r.body, 'reportId');
      if (p.mappingError || !p.arr) return { ...out, ok: false, error: `list ${p.note}` };
      for (const o of p.arr) {
        const v = parseListRow(o);
        if (!v.ok) return { ...out, ok: false, error: v.error };
        out.reports.push(v.report);
      }
      if (p.arr.length < FIN_LIST_LIMIT) return out;
      offset += FIN_LIST_LIMIT; // редкий случай: страниц больше одной
    }
    return { ...out, ok: false, error: 'list: превышен guard пагинации' };
  }

  /** finDailyFetchDetailed_ — detailed целиком, ВСЕГДА с rrdId=0 (повтор = полная перезагрузка). */
  async detailed(reportId: string): Promise<DetailedResult> {
    const out: DetailedResult = { ok: true, rows: [], pages: 0, error: '' };
    let cursor = '0';
    for (let guard = 0; guard < FIN_DET_PAGE_GUARD; guard++) {
      if (!this.canRequest()) return { ...out, ok: false, error: 'BUDGET_EXCEEDED (detailed)' };
      // Курсор подставляется вербатим (строка цифр): int64 rrdId не теряет точность.
      const payload = `{"rrdId":${cursor},"limit":${FIN_PAGE_LIMIT}}`;
      const r = await this.post(FIN_API_DET_PATH + encodeURIComponent(reportId), payload);
      if (r.code === 204) return out;
      if (r.code !== 200) return { ...out, ok: false, error: `detailed HTTP ${r.code}: ${r.body.substring(0, 200)}` };
      const p = parseSafe(r.body, 'rrdId');
      if (p.mappingError || !p.arr) return { ...out, ok: false, error: `detailed ${p.note}` };
      if (!p.arr.length) return out;
      for (const row of p.arr) out.rows.push(row);
      out.pages++;
      const last = String(p.arr[p.arr.length - 1]!.rrdId || '');
      if (!isDigits(last) || last === cursor) return { ...out, ok: false, error: 'detailed: битый курсор rrdId' };
      cursor = last;
      if (p.arr.length < FIN_PAGE_LIMIT) return out;
    }
    return { ...out, ok: false, error: 'detailed: превышен guard пагинации' };
  }
}

/** В логах номер отчёта не нужен (репозиторий публичный, логи — нет; но лишнего не пишем). */
function redactPath(p: string): string {
  return p.startsWith(FIN_API_DET_PATH) ? `${FIN_API_DET_PATH}<id>` : p;
}
