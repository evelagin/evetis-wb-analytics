/**
 * Чистое решение по итогу append (PR-Mig1 fix2). Разделяет:
 *  - LOADED: свежая загрузка этой попытки → строгая пост-проверка count==distinct==expected;
 *  - ALREADY_LOADED: детерминированный load job уже был успешен, строки на месте →
 *    COMPLETE (REUSED), НО 0 строк = ERROR (ложный success недопустим), дубли = ERROR.
 */
export type AppendResult = 'LOADED' | 'ALREADY_LOADED';

export type PostLoadDecision =
  | { ok: true; reused: boolean }
  | { ok: false; code: string; message: string };

export function evaluatePostLoad(
  append: AppendResult,
  count: number,
  distinct: number,
  expected: number,
): PostLoadDecision {
  if (count !== distinct) {
    return { ok: false, code: 'STOCKS_POSTCOUNT_DUP', message: `дубли в снимке: count=${count} distinct=${distinct}` };
  }
  if (append === 'ALREADY_LOADED') {
    if (count === 0) {
      return { ok: false, code: 'STOCKS_REUSE_EMPTY', message: 'load job успешен, но 0 строк под детерминированным snapshot_id' };
    }
    return { ok: true, reused: true };
  }
  // LOADED
  if (count !== expected) {
    return { ok: false, code: 'STOCKS_POSTCOUNT', message: `пост-проверка: expected=${expected} written=${count} distinct=${distinct}` };
  }
  return { ok: true, reused: false };
}

import type { StockMetrics } from './normalize.js';

/**
 * План финализации manifest. reused → markReused (метрики нового fetch НЕ пишем);
 * иначе → finalize с метриками текущей загрузки.
 */
export type FinalizePlan =
  | { kind: 'finalize'; metrics: StockMetrics; written: number; controlStatus: 'NOT_RUN' }
  | { kind: 'markReused'; written: number };

export function planFinalize(reused: boolean, metrics: StockMetrics, count: number): FinalizePlan {
  if (reused) return { kind: 'markReused', written: count };
  return { kind: 'finalize', metrics, written: count, controlStatus: 'NOT_RUN' };
}
