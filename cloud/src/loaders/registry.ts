/**
 * Реестр загрузчиков. PR-Mig0 регистрировал noop/stocks; PR-Mart3b-2 добавляет `mart`.
 * Cloud Run Job выбирает загрузчик по имени: node dist/cli.js <loader>.
 *
 * Каждый загрузчик объявляет СВОЮ политику логического периода (он же идемпотентный ключ
 * LOADER_RUNS и targetDate — инвариант LoaderContext: targetDate === logicalPeriod):
 *   - stocks/noop — текущие сутки МСК (данные за сегодня);
 *   - mart        — D-1 МСК (источники за прошлые сутки полны) и prodOnly (publish → production wb_mart).
 * Раньше период вычислялся в cli как current-day для всех — mart'у это не подходило (нужен D-1),
 * поэтому в PR-Mart3b-1 mart в generic CLI НЕ регистрировался. Теперь политика — часть реестра.
 */
import { dailyPeriodMoscow } from '../period.js';
import type { LoaderHandler } from './types.js';
import { noopLoader } from './noop.js';
import { stocksLoader } from './stocks/index.js';
import { martLoader } from './mart/index.js';
import { d1Moscow } from './mart/targetDate.js';

export interface LoaderSpec {
  handler: LoaderHandler;
  /**
   * Логический период прогона (YYYY-MM-DD). Он же идемпотентный ключ LOADER_RUNS и ctx.targetDate.
   * `now` инжектится в тестах.
   */
  logicalPeriod: (now?: Date) => string;
  /**
   * true → загрузчик публикует production-данные и запрещён вне ENVIRONMENT=prod.
   * cli отклоняет такой запуск ДО захвата lease (fail-closed); loader дополнительно самозащищается.
   */
  prodOnly?: boolean;
}

export const LOADERS: Record<string, LoaderSpec> = {
  noop: { handler: noopLoader, logicalPeriod: (now) => dailyPeriodMoscow(now) },
  stocks: { handler: stocksLoader, logicalPeriod: (now) => dailyPeriodMoscow(now) },
  // Витрина: целевая дата = D-1 МСК; процедуры публикуют production `wb_mart` → только prod.
  mart: { handler: martLoader, logicalPeriod: (now) => d1Moscow(now), prodOnly: true },
};

export function resolveLoader(name: string): LoaderSpec | undefined {
  return LOADERS[name];
}

/** Список зарегистрированных имён — для сообщения об unknown_loader. */
export function availableLoaderNames(): string {
  return Object.keys(LOADERS).join(', ');
}
