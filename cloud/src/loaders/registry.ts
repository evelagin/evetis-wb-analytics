/**
 * Реестр загрузчиков (PR-Mig0). Foundation регистрирует только `noop`.
 * Cloud Run Job выбирает загрузчик по имени: node dist/cli.js <loader>.
 */
import type { LoaderHandler } from './types.js';
import { noopLoader } from './noop.js';
import { stocksLoader } from './stocks/index.js';

// PR-Mart3b-1: loader `mart` НЕ регистрируется в generic CLI (блокер #3 аудита кода) —
// generic-cli вычисляет current-day, а mart'у нужен D-1, и run-lease ещё не доказан.
// Всё wiring mart в общий CLI (+D-1, +LOADER_RUNS) переносится в PR-Mart3b-2.
// В PR-Mart3b-1 mart запускается ТОЛЬКО через dedicated `mart_manual.ts` (там D-1 корректный).
export const LOADERS: Record<string, LoaderHandler> = {
  noop: noopLoader,
  stocks: stocksLoader,
};

export function resolveLoader(name: string): LoaderHandler | undefined {
  return LOADERS[name];
}
