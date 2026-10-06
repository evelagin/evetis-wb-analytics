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
import { promoLoader } from './promo/index.js';
import { promoSlot } from './promo/slot.js';
import { tariffsLoader } from './tariffs/index.js';
import { funnelLoader } from './funnel/index.js';
import { storageLoader } from './storage/index.js';
import { unitkaLoader } from './unitka/index.js';
import { unitkaSlot } from './unitka/slot.js';
import { unitkaMonthPrepLoader } from './unitka/prep.js';
import { unitkaMonthRollbackLoader } from './unitka/rollback.js';
import { unitkaSppRollbackLoader } from './unitka/spp_rollback.js';
import { ozonUnitkaLoader } from './unitka/ozon/loader.js';
import { ozonLcdMigrationLoader } from './unitka/ozon/lcd_migration_loader.js';

export interface LoaderSpec {
  handler: LoaderHandler;
  /** Логический период — идемпотентный ключ LOADER_RUNS и ctx.targetDate. */
  logicalPeriod: (now?: Date) => string;
  /** true → загрузчик публикует production-данные и запрещён вне ENVIRONMENT=prod. */
  prodOnly?: boolean;
  /**
   * true → после захвата lease транзиентный отказ (BigQuery/сеть/Sheets 429·5xx) повторяется ОДИН
   * раз в том же слоте. Только для идемпотентных писателей: план строится как разница с листом,
   * поэтому повтор после частичной записи дописывает недостающее и не дублирует уже записанное.
   */
  retryTransient?: boolean;
}

export const LOADERS: Record<string, LoaderSpec> = {
  noop: { handler: noopLoader, logicalPeriod: (now) => dailyPeriodMoscow(now) },
  stocks: { handler: stocksLoader, logicalPeriod: (now) => dailyPeriodMoscow(now) },
  mart: { handler: martLoader, logicalPeriod: (now) => d1Moscow(now), prodOnly: true },
  prices: { handler: pricesLoader, logicalPeriod: (now) => observationBucket(now) },
  // PR-PROMO-1: наблюдатель акций WB. Период — слот расписания (4 раза в сутки),
  // а не сутки: суточный период схлопнул бы четыре наблюдения в одно.
  promo: { handler: promoLoader, logicalPeriod: (now) => promoSlot(now) },
  tariffs: { handler: tariffsLoader, logicalPeriod: (now) => (now ?? new Date()).toISOString().slice(0, 10) },
  funnel: { handler: funnelLoader, logicalPeriod: (now) => d1Moscow(now) },
  // E4: платное хранение — только закрытые сутки, RAW production, overlap внутри loader.
  storage: { handler: storageLoader, logicalPeriod: (now) => d1Moscow(now), prodOnly: true },
  unitka: { handler: unitkaLoader, logicalPeriod: (now) => unitkaSlot(now), retryTransient: true },
  // Calendar V2 (Phase 2B): подготовка секции месяца. По умолчанию ТОЛЬКО план; запись — prod +
  // UNITKA_MONTH_PREP_WRITE=1. Не активирован нигде: нет расписания, нет шага деплоя.
  'unitka-month-prep': { handler: unitkaMonthPrepLoader, logicalPeriod: (now) => unitkaSlot(now) },
  // Calendar V2: откат СОЗДАНИЯ месяца по манифесту. По умолчанию ТОЛЬКО план; исполнение — prod +
  // UNITKA_MONTH_ROLLBACK_WRITE=1 + манифест + явный месяц. Нет расписания, нет шага деплоя.
  'unitka-month-rollback': { handler: unitkaMonthRollbackLoader, logicalPeriod: (now) => unitkaSlot(now) },
  // SPP-3: откат миграции колонки AB по манифесту. По умолчанию ТОЛЬКО план; исполнение — prod +
  // UNITKA_SPP_ROLLBACK_WRITE=1 + манифест + подтверждённый отпечаток. Нет расписания.
  'unitka-spp-rollback': { handler: (ctx) => unitkaSppRollbackLoader(ctx), logicalPeriod: (now) => unitkaSlot(now) },
  // Gate 8: суточный прогон Ozon-Юнитки. Окно перезаписи 45 суток (раз в месяц 120),
  // провизорная экономика (факт > оценка). Запись — prod + OZON_UNITKA_WRITE_ENABLED=1;
  // по умолчанию прогон только считает план. Расписание НЕ создано: см. runbook Gate 8.
  'ozon-unitka': { handler: ozonUnitkaLoader, logicalPeriod: (now) => unitkaSlot(now), retryTransient: true },
  // Gate 10, этап 5: одноразовый перевод формул Ozon на OZON_LAST_CLOSED_DATE. По умолчанию ТОЛЬКО план;
  // запись — prod + OZON_LCD_MIGRATION_WRITE=1 + ожидания из плана. Нет расписания, запуск — одно
  // исполнение ozon-unitka-prod с --args=ozon-unitka-lcd-migration.
  'ozon-unitka-lcd-migration': { handler: (ctx) => ozonLcdMigrationLoader(ctx), logicalPeriod: (now) => unitkaSlot(now) },
};

export function resolveLoader(name: string): LoaderSpec | undefined {
  return LOADERS[name];
}

export function availableLoaderNames(): string {
  return Object.keys(LOADERS).join(', ');
}
