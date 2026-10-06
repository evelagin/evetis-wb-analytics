/**
 * Классификация отказов прогона (инцидент 2026-10-06, unitka-engine-prod-zbfk7).
 *
 * Прогон упал ДО захвата lease на транзиентной ошибке BigQuery («Retrying the job may solve
 * the problem»). Верхний `fatal` писал запись без `code`, алерт ловит `jsonPayload.code!=""`,
 * maxRetries=0 — сбой прошёл молча. Отсюда два правила:
 *   1) у ЛЮБОГО отказа есть стабильный непустой код (алерт и LOADER_RUNS.error_code);
 *   2) повторять можно ТОЛЬКО транзиентный инфраструктурный отказ. Детерминированный отказ
 *      (контракт, QA, гейт свежести, геометрия листа) повтор не лечит — его не повторяем.
 */
import { LoaderError } from './errors.js';

export type FailureCategory = 'TRANSIENT' | 'DETERMINISTIC';

export interface FailureClass {
  /** Непустой стабильный код — уходит в LOADER_RUNS.error_code и в лог (`jsonPayload.code`). */
  code: string;
  category: FailureCategory;
}

export const TRANSIENT_INFRA = 'TRANSIENT_INFRA';
export const SHEETS_API_TRANSIENT = 'SHEETS_API_TRANSIENT';
/** Сырой (не LoaderError) отказ неизвестной природы — прежний код, потребители его знают. */
export const UNCLASSIFIED = 'LOADER_ERROR';
/** Отказ, долетевший до верхнего уровня процесса мимо runCli. */
export const FATAL_UNHANDLED = 'FATAL_UNHANDLED';
/** Обобщённые обёртки: смысл несёт `cause`, а не сама обёртка (unitka/index.ts → ENGINE_ERROR). */
const WRAPPER_CODES = new Set(['ENGINE_ERROR', UNCLASSIFIED]);

const TRANSIENT_HTTP = new Set([408, 429, 500, 502, 503, 504]);
const TRANSIENT_NET = new Set([
  'ECONNRESET', 'ETIMEDOUT', 'ECONNREFUSED', 'ECONNABORTED', 'EAI_AGAIN', 'EPIPE', 'UND_ERR_SOCKET',
]);
// Причины BigQuery, которые сам BigQuery объявляет повторяемыми.
const TRANSIENT_BQ_REASONS = new Set(['backendError', 'internalError', 'jobInternalError', 'rateLimitExceeded']);
// Текстовые признаки — только однозначные: формулировка BigQuery, HTTP-статус gaxios, сетевые коды.
const TRANSIENT_TEXT =
  /Retrying the job may solve the problem|Exceeded rate limits|The user aborted a request|status code (?:408|429|5\d\d)\b|socket hang up|\bE(?:CONNRESET|TIMEDOUT|CONNREFUSED|CONNABORTED|AI_AGAIN|PIPE)\b|\b(?:backendError|internalError|jobInternalError|rateLimitExceeded)\b/i;

function asRecord(e: unknown): Record<string, unknown> {
  return e !== null && typeof e === 'object' ? (e as Record<string, unknown>) : {};
}

function rawIsTransient(e: unknown): boolean {
  const r = asRecord(e);
  const code = r.code;
  if (typeof code === 'number' && TRANSIENT_HTTP.has(code)) return true;
  if (typeof code === 'string' && (TRANSIENT_NET.has(code) || TRANSIENT_HTTP.has(Number(code)))) return true;
  const status = asRecord(r.response).status;
  if (typeof status === 'number' && TRANSIENT_HTTP.has(status)) return true;
  const errors = Array.isArray(r.errors) ? r.errors : [];
  if (errors.some((x) => TRANSIENT_BQ_REASONS.has(String(asRecord(x).reason ?? '')))) return true;
  const msg = e instanceof Error ? e.message : typeof e === 'string' ? e : '';
  return TRANSIENT_TEXT.test(msg);
}

export function classifyFailure(e: unknown, depth = 0): FailureClass {
  if (e instanceof LoaderError) {
    // Обёртка без собственного смысла: классифицируем причину (транзиентный BigQuery внутри Engine).
    if (WRAPPER_CODES.has(e.code) && e.cause !== undefined && depth < 3) {
      const inner = classifyFailure(e.cause, depth + 1);
      if (inner.category === 'TRANSIENT') return inner;
    }
    // Обёртка Sheets API сохраняет текст gaxios («status code 503») — 429/5xx повторяемы.
    if (e.code === 'SHEETS_API' && TRANSIENT_TEXT.test(e.message)) {
      return { code: SHEETS_API_TRANSIENT, category: 'TRANSIENT' };
    }
    return { code: e.code || UNCLASSIFIED, category: 'DETERMINISTIC' };
  }
  if (rawIsTransient(e)) return { code: TRANSIENT_INFRA, category: 'TRANSIENT' };
  // Собственный строковый код не-LoaderError (StaleSourceError.code = 'SOURCE_STALE') — сохраняем.
  const own = asRecord(e).code;
  if (typeof own === 'string' && /^[A-Z][A-Z0-9_]{2,63}$/.test(own)) return { code: own, category: 'DETERMINISTIC' };
  return { code: UNCLASSIFIED, category: 'DETERMINISTIC' };
}

/** Запись верхнего `fatal`: код есть всегда, поэтому алерт `jsonPayload.code!=""` её видит. */
export function fatalRecord(e: unknown): Record<string, string> {
  const c = classifyFailure(e);
  const code = c.category === 'TRANSIENT' ? c.code : e instanceof LoaderError ? c.code : FATAL_UNHANDLED;
  return {
    severity: 'ERROR',
    message: 'fatal',
    code,
    category: c.category,
    error: e instanceof Error ? e.message : String(e),
  };
}
