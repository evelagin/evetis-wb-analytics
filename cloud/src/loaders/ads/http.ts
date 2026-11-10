/**
 * HTTP к WB Advert API — тот же контракт ретрая, что wbAdsHttp_ → wbFetchWithRetry_
 * (WbAdsProbe.gs:559, Utils.gs:157):
 *   • повтор ТОЛЬКО на 429/500/502/503/504 и на сетевое исключение;
 *   • до 1 + WB_ADS_MAX_RETRY попыток;
 *   • пауза: Retry-After (секунды или HTTP-дата), иначе base·2^(n−1); обе ≤ 60 c;
 *   • повторы исчерпаны на HTTP-коде → возвращается ПОСЛЕДНИЙ ответ (ok=false), не исключение;
 *   • повторы исчерпаны на сетевом исключении → бросаем (как эталон; источник ловит и даёт FAILED).
 * Отдельный модуль, а не cloud/src/http/wbHttp.ts: общий клиент не умеет Retry-After и
 * повторяет любые 5xx, а менять его поведение для остальных загрузчиков здесь нельзя.
 * Токен уходит только в заголовок Authorization и нигде не логируется.
 */
import type { Logger } from '../../logging.js';
import {
  WB_ADS_MAX_RETRY, WB_ADS_RETRY_BASE_MS, WB_ADS_RETRY_MAX_DELAY_MS, WB_ADS_RETRY_CODES,
} from './constants.js';

export interface AdsHttpResponse {
  code: number;
  ok: boolean;
  body: string;
  json: unknown;
}

/** Минимальный fetch-контракт (инъекция в тестах). */
export type FetchFn = (url: string, init: { method: string; headers: Record<string, string>; body?: string; signal?: AbortSignal }) =>
  Promise<{ status: number; headers: { get(name: string): string | null }; text(): Promise<string> }>;

export type SleepFn = (ms: number) => Promise<void>;

export const realSleep: SleepFn = (ms) => new Promise((r) => setTimeout(r, ms));

export interface AdsHttpOptions {
  timeoutMs: number;
  maxRetries?: number;
  baseDelayMs?: number;
  maxDelayMs?: number;
  fetchFn?: FetchFn;
  sleep?: SleepFn;
  nowMs?: () => number;
  logger?: Logger;
}

/** Разбор Retry-After: секунды или HTTP-дата → мс; −1, если заголовка нет или он не распознан. */
export function retryAfterMs(value: string | null, nowMs: number): number {
  if (!value) return -1;
  const ra = value.trim();
  if (/^\d+$/.test(ra)) return parseInt(ra, 10) * 1000;
  const when = Date.parse(ra);
  if (!Number.isNaN(when)) { const ms = when - nowMs; return ms > 0 ? ms : 0; }
  return -1;
}

/** Пауза перед повтором n (n = номер завершившейся попытки, с 1). */
export function retryPauseMs(attempt: number, retryAfter: number, baseMs: number, maxMs: number): number {
  const pause = retryAfter >= 0 ? retryAfter : baseMs * Math.pow(2, attempt - 1);
  return Math.min(maxMs, pause);
}

export class AdsHttp {
  private readonly fetchFn: FetchFn;
  private readonly sleep: SleepFn;
  private readonly nowMs: () => number;
  /** Счётчик фактических HTTP-попыток — для телеметрии прогона. */
  calls = 0;

  constructor(private readonly token: string, private readonly opts: AdsHttpOptions) {
    this.fetchFn = opts.fetchFn ?? (globalThis.fetch as unknown as FetchFn);
    this.sleep = opts.sleep ?? realSleep;
    this.nowMs = opts.nowMs ?? (() => Date.now());
  }

  async request(method: 'get' | 'post', url: string, payload?: unknown): Promise<AdsHttpResponse> {
    const maxRetries = this.opts.maxRetries ?? WB_ADS_MAX_RETRY;
    const base = this.opts.baseDelayMs ?? WB_ADS_RETRY_BASE_MS;
    const cap = this.opts.maxDelayMs ?? WB_ADS_RETRY_MAX_DELAY_MS;
    const safeUrl = url.split('?')[0];
    const headers: Record<string, string> = { Authorization: this.token };
    let body: string | undefined;
    if (payload !== undefined && payload !== null) {
      headers['Content-Type'] = 'application/json';
      body = JSON.stringify(payload);
    }

    let attempt = 0;
    for (;;) {
      attempt++;
      this.calls++;
      let status = 0;
      let text = '';
      let raHeader: string | null = null;
      let threw = false;
      let errMsg = '';
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), this.opts.timeoutMs);
      try {
        const resp = await this.fetchFn(url, { method: method.toUpperCase(), headers, body, signal: controller.signal });
        status = resp.status;
        raHeader = resp.headers.get('retry-after');
        text = await resp.text();
      } catch (e) {
        threw = true;
        errMsg = e instanceof Error ? e.message : String(e);
      } finally {
        clearTimeout(timer);
      }

      if (!threw && !(WB_ADS_RETRY_CODES as readonly number[]).includes(status)) {
        return toResponse(status, text);
      }
      if (attempt > maxRetries) {
        if (threw) throw new Error(`[WB_ADS] HTTP-исключение после ${attempt} попыток: ${errMsg}`);
        this.opts.logger?.warn('wb_ads_http_retries_exhausted', { status, attempts: attempt, url: safeUrl });
        return toResponse(status, text);
      }
      const pause = retryPauseMs(attempt, threw ? -1 : retryAfterMs(raHeader, this.nowMs()), base, cap);
      this.opts.logger?.warn('wb_ads_http_retry', {
        reason: threw ? `exception: ${errMsg}` : `HTTP ${status}`, attempt, pauseMs: pause, url: safeUrl,
      });
      await this.sleep(pause);
    }
  }
}

function toResponse(code: number, body: string): AdsHttpResponse {
  let json: unknown = null;
  try { json = JSON.parse(body); } catch { json = null; }
  return { code, ok: code >= 200 && code < 300, body, json };
}

/** Обрезка тела ответа для сообщений об ошибке (wbAdsClip_, WbAdsProbe.gs:757). */
export function clip(s: string | null | undefined, n = 150): string {
  const t = String(s ?? '');
  return t.length > n ? t.slice(0, n) : t;
}
