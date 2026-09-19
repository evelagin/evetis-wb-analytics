/**
 * Реестр загрузчиков. Cloud Run Job выбирает загрузчик по имени: node dist/cli.js <loader>.
 */
import { dailyPeriodMoscow } from '../period.js';
import type { LoaderHandler } from './types.js';
import { noopLoader } from './noop.js';
import { stocksLoader } from './stocks/index.js';
import { martLoader } from './mart/index.js';
import { d1Moscow } from './mart/targetDate.js';
import { pricesLoader } from './prices/index.js';
import { observationBucket } from './prices/bucket.js';
import { tariffsLoader } from './tariffs/index.js';
import { funnelLoader } from './funnel/index.js';
import { storageLoader } from './storage/index.js';
import { unitkaLoader } from './unitka/index.js';
import { unitkaSlot } from './unitka/slot.js';
import { unitkaMonthPrepLoader } from './unitka/prep.js';
import { unitkaMonthRollbackLoader } from './unitka/rollback.js';

export interface LoaderSpec {
  handler: LoaderHandler;
  /** Логический период — идемпотентный ключ LOADER_RUNS и ctx.targetDate. */
  logicalPeriod: (now?: Date) => string;
  /** true → загрузчик публикует production-данные и запрещён вне ENVIRONMENT=prod. */
  prodOnly?: boolean;
}

export const LOADERS: Record<string, LoaderSpec> = {
  noop: { handler: noopLoader, logicalPeriod: (now) => dailyPeriodMoscow(now) },
  stocks: { handler: stocksLoader, logicalPeriod: (now) => dailyPeriodMoscow(now) },
  mart: { handler: martLoader, logicalPeriod: (now) => d1Moscow(now), prodOnly: true },
  prices: { handler: pricesLoader, logicalPeriod: (now) => observationBucket(now) },
  tariffs: { handler: tariffsLoader, logicalPeriod: (now) => (now ?? new Date()).toISOString().slice(0, 10) },
  funnel: { handler: funnelLoader, logicalPeriod: (now) => d1Moscow(now) },
  // E4: платное хранение — только закрытые сутки, RAW production, overlap внутри loader.
  storage: { handler: storageLoader, logicalPeriod: (now) => d1Moscow(now), prodOnly: true },
  unitka: { handler: unitkaLoader, logicalPeriod: (now) => unitkaSlot(now) },
  // Calendar V2 (Phase 2B): подготовка секции месяца. По умолчанию ТОЛЬКО план; запись — prod +
  // UNITKA_MONTH_PREP_WRITE=1. Не активирован нигде: нет расписания, нет шага деплоя.
  'unitka-month-prep': { handler: unitkaMonthPrepLoader, logicalPeriod: (now) => unitkaSlot(now) },
  // Calendar V2: откат СОЗДАНИЯ месяца по манифесту. По умолчанию ТОЛЬКО план; исполнение — prod +
  // UNITKA_MONTH_ROLLBACK_WRITE=1 + манифест + явный месяц. Нет расписания, нет шага деплоя.
  'unitka-month-rollback': { handler: unitkaMonthRollbackLoader, logicalPeriod: (now) => unitkaSlot(now) },
};

export function resolveLoader(name: string): LoaderSpec | undefined {
  return LOADERS[name];
}

export function availableLoaderNames(): string {
  return Object.keys(LOADERS).join(', ');
}
