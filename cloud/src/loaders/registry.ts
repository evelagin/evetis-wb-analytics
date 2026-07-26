/**
 * Реестр загрузчиков (PR-Mig0). Foundation регистрирует только `noop`.
 * Cloud Run Job выбирает загрузчик по имени: node dist/cli.js <loader>.
 */
import type { LoaderHandler } from './types.js';
import { noopLoader } from './noop.js';
import { stocksLoader } from './stocks/index.js';

export const LOADERS: Record<string, LoaderHandler> = {
  noop: noopLoader,
  stocks: stocksLoader,
};

export function resolveLoader(name: string): LoaderHandler | undefined {
  return LOADERS[name];
}
