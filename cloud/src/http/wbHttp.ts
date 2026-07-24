/**
 * HTTP-клиент для WB API (PR-Mig0: только транспорт, без эндпоинтов и токенов).
 *
 * Уровень HTTP-повторов — САМЫЙ НИЗКИЙ в иерархии retries и отвечает ТОЛЬКО за:
 *   - 429 (rate limit);
 *   - временные 5xx;
 *   - сетевой таймаут.
 * Экспоненциальный backoff с верхним лимитом. Task-retry (Cloud Run) и
 * schedule-retry (Cloud Scheduler) — отдельные уровни, здесь НЕ дублируются.
 */
import { Logger } from '../logging.js';

export interface WbHttpOptions {
  timeoutMs: number;
  maxRetries: number;
  backoffBaseMs?: number;
  backoffCapMs?: number;
}

export interface WbHttpResult {
  ok: boolean;
  status: number;
  body: string;
  attempts: number;
  error?: string;
}

/** Повторяем ТОЛЬКО эти статусы. */
export function isRetryableStatus(status: number): boolean {
  return status === 429 || (status >= 500 && status <= 599);
}

/** Детерминированный backoff (без джиттера — джиттер добавим при нужде). */
export function computeBackoffMs(attempt: number, baseMs: number, capMs: number): number {
  const raw = baseMs * Math.pow(2, Math.max(0, attempt - 1));
  return Math.min(raw, capMs);
}

const sleep = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms));

export async function wbFetch(
  url: string,
  init: RequestInit,
  opts: WbHttpOptions,
  logger?: Logger,
): Promise<WbHttpResult> {
  const base = opts.backoffBaseMs ?? 1000;
  const cap = opts.backoffCapMs ?? 21000;
  let attempt = 0;
  let lastErr = '';

  while (attempt <= opts.maxRetries) {
    attempt++;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), opts.timeoutMs);
    try {
      const resp = await fetch(url, { ...init, signal: controller.signal });
      const body = await resp.text();
      if (resp.ok) return { ok: true, status: resp.status, body, attempts: attempt };
      if (isRetryableStatus(resp.status) && attempt <= opts.maxRetries) {
        const wait = computeBackoffMs(attempt, base, cap);
        logger?.warn('wb_http_retryable', { status: resp.status, attempt, waitMs: wait });
        await sleep(wait);
        continue;
      }
      return { ok: false, status: resp.status, body, attempts: attempt, error: `HTTP ${resp.status}` };
    } catch (e) {
      lastErr = e instanceof Error ? e.message : String(e);
      if (attempt <= opts.maxRetries) {
        const wait = computeBackoffMs(attempt, base, cap);
        logger?.warn('wb_http_network_retry', { error: lastErr, attempt, waitMs: wait });
        await sleep(wait);
        continue;
      }
    } finally {
      clearTimeout(timer);
    }
  }
  return { ok: false, status: 0, body: '', attempts: attempt, error: lastErr || 'exhausted' };
}
