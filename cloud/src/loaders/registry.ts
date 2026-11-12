/**
 * Реестр загрузчиков. Cloud Run Job выбирает загрузчик по имени: node dist/cli.js <loader>.
 */
import { dailyPeriodMoscow } from '../period.js';
import type { LoaderHandler } from './types.js';
import type { Config } from '../config.js';
import { noopLoader } from './noop.js';
import { stocksLoader } from './stocks/index.js';
import { martLoader } from './mart/index.js';
import { d1Moscow } from './mart/targetDate.js';
import {
  martSourceWatermark, MART_SOURCE_WATERMARK_PROBE_FAILED, MART_SOURCE_CHANGED_DURING_BUILD,
} from './mart/watermark.js';
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
import { unitkaCancelRestoreLoader } from './unitka/cancel_restore.js';
import { ozonUnitkaLoader } from './unitka/ozon/loader.js';
import { ozonLcdMigrationLoader } from './unitka/ozon/lcd_migration_loader.js';
import { ozonCpoOrdersLoader } from './ozon_cpo/index.js';
import { unitkaStorePnlLoader } from './unitka/storepnl/index.js';
import { ozonStorePnlLoader } from './unitka/ozonstorepnl/index.js';
import { ordersLoader, salesLoader, salesReconcileLoader } from './statistics/index.js';
import { statsSlot } from './statistics/common.js';
import { adsLoader, adsQueryLoader, adsCostsAuditLoader } from './ads/index.js';
import { financeLoader, financeBackfillLoader } from './finance/index.js';
import { suppliesLoader } from './supplies/index.js';
import { refSyncLoader } from './refsync/index.js';

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
  /**
   * Source-aware guard (2026-10-12, только mart). Есть → COMPLETE за период блокирует слот, только
   * если собран на той же версии позднего источника; иначе слот пересобирает тот же период.
   * Нет (все прочие загрузчики) → guard и SQL acquire прежние байт-в-байт.
   *   probe — версия источника, считается ДО acquire; отказ = «версия неизвестна»: сборка
   *           не пропускается, в строку пишется NULL, в лог — ERROR с кодом probeFailureCode.
   */
  sourceWatermark?: {
    probe: (config: Config) => Promise<string>;
    probeFailureCode: string;
    /** WARNING: версия после сборки ≠ версии до неё → COMPLETE пишется без версии (NULL). */
    changedDuringBuildCode: string;
  };
}

/**
 * M2: guard загрузчиков statistics-api. Период — закрытые сутки D-1 МСК, как ingestClosedDayMsk_ у
 * Apps Script: именно эту строку ждёт гейт витрины (V_INGEST_HEARTBEAT, LATEST-ATTEMPT). Но Apps Script
 * пишет INGEST_RUNS КАЖДЫМ запуском (раз в 6 ч), а guard по одному периоду пропустил бы все запуски
 * суток после первого COMPLETE. Поэтому «версией источника» служит часовой слот МСК: COMPLETE блокирует
 * только повтор в том же часу; следующий слот — новый прогон того же D-1 (механизм source-aware guard
 * витрины, cli.ts/runManifest.ts без изменений). Слот пишется в LOADER_RUNS.source_watermark.
 */
const STATS_SLOT_GUARD: NonNullable<LoaderSpec['sourceWatermark']> = {
  probe: async () => statsSlot(),
  probeFailureCode: 'STATS_SLOT_PROBE_FAILED',
  changedDuringBuildCode: 'STATS_SLOT_CHANGED_DURING_RUN',
};

export const LOADERS: Record<string, LoaderSpec> = {
  noop: { handler: noopLoader, logicalPeriod: (now) => dailyPeriodMoscow(now) },
  stocks: { handler: stocksLoader, logicalPeriod: (now) => dailyPeriodMoscow(now) },
  // Финансы WB приходят после утренней сборки (DAILY ~04:28, WEEKLY пн ~09:26 UTC): версия
  // V_WB_FINANCE_SEMANTIC открывает пересборку того же D-1 в следующем слоте (mart/watermark.ts).
  mart: {
    handler: martLoader,
    logicalPeriod: (now) => d1Moscow(now),
    prodOnly: true,
    sourceWatermark: {
      probe: martSourceWatermark,
      probeFailureCode: MART_SOURCE_WATERMARK_PROBE_FAILED,
      changedDuringBuildCode: MART_SOURCE_CHANGED_DURING_BUILD,
    },
  },
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
  // OWNER ACK «CANCEL RECONCILIATION» (02.11.2026): адресное восстановление зрелых суток по классу отмен
  // (cancel_class_v1.sql). По умолчанию ТОЛЬКО план и манифест; запись — prod + UNITKA_CANCEL_RESTORE_WRITE=1 + отпечаток
  // просмотренного манифеста. Нет расписания: одно исполнение unitka-engine-prod с --args=unitka-cancel-restore.
  'unitka-cancel-restore': { handler: (ctx) => unitkaCancelRestoreLoader(ctx), logicalPeriod: (now) => unitkaSlot(now) },
  // Gate 8: суточный прогон Ozon-Юнитки. Окно перезаписи 45 суток (раз в месяц 120),
  // провизорная экономика (факт > оценка). Запись — prod + OZON_UNITKA_WRITE_ENABLED=1;
  // по умолчанию прогон только считает план. Расписание НЕ создано: см. runbook Gate 8.
  'ozon-unitka': { handler: ozonUnitkaLoader, logicalPeriod: (now) => unitkaSlot(now), retryTransient: true },
  // Gate 10, этап 5: одноразовый перевод формул Ozon на OZON_LAST_CLOSED_DATE. По умолчанию ТОЛЬКО план;
  // запись — prod + OZON_LCD_MIGRATION_WRITE=1 + ожидания из плана. Нет расписания, запуск — одно
  // исполнение ozon-unitka-prod с --args=ozon-unitka-lcd-migration.
  'ozon-unitka-lcd-migration': { handler: (ctx) => ozonLcdMigrationLoader(ctx), logicalPeriod: (now) => unitkaSlot(now) },
  // Phase B (OWNER ACK 08.10.2026): «Оплата за заказ» Ozon по заказам → ozon_raw.RAW_OZON_ADS_CPO_ORDERS.
  // Свой Job ozon-cpo-orders-prod, BQ_RAW_DATASET=ozon_raw (аренда и журнал — в домене Ozon).
  // Период — часовой слот МСК: ручной бэкфилл в другой час не упирается в COMPLETE суточного прогона.
  // Замена блока окна идемпотентна, поэтому транзиентный отказ повторяется один раз в слоте.
  'ozon-cpo-orders': { handler: (ctx) => ozonCpoOrdersLoader(ctx), logicalPeriod: (now) => unitkaSlot(now), prodOnly: true, retryTransient: true },
  // Phase C (OWNER ACK 09.10.2026): P&L магазина WB. Лист Юнитки — только чтение (scope readonly);
  // пишет лишь снимок wb_ops.UNITKA_SKU_COMPONENTS_DAILY (добавление, новый snapshot_id), план вкладки —
  // в журнал. Повтор после транзиентного отказа безопасен: снимок либо лёг целиком, либо не лёг.
  'unitka-store-pnl': { handler: (ctx) => unitkaStorePnlLoader(ctx), logicalPeriod: (now) => unitkaSlot(now), prodOnly: true, retryTransient: true },
  // D4 (OWNER ACK 15.10.2026): P&L магазина Ozon — вкладка «Ozon Магазин P&L». Только чтение ozon_mart; без
  // OZON_STORE_PNL_PUBLISH=1 Sheets не открывается (план, QA и гейты — в журнал). Запись идемпотентна (NOOP по отпечатку, ручные
  // правки → отказ), атомарна — повтор транзиентного отказа в том же слоте безопасен.
  'ozon-store-pnl': { handler: (ctx) => ozonStorePnlLoader(ctx), logicalPeriod: (now) => unitkaSlot(now), prodOnly: true, retryTransient: true },
  // M2 (перенос из Apps Script): заказы, продажи и ночная сверка продаж — statistics-api WB.
  // Имена 'orders'/'sales' = loader_name INGEST_RUNS: на них завязан гейт витрины (mart/bq.ts).
  // Повтор безопасен: watermark из таблицы + граничные ключи не дают дубля (statistics/index.ts).
  orders: { handler: (ctx) => ordersLoader(ctx), logicalPeriod: (now) => d1Moscow(now), retryTransient: true, sourceWatermark: STATS_SLOT_GUARD },
  sales: { handler: (ctx) => salesLoader(ctx), logicalPeriod: (now) => d1Moscow(now), retryTransient: true, sourceWatermark: STATS_SLOT_GUARD },
  // Сверка в INGEST_RUNS не писала; своё имя, чтобы её ERROR не стал LATEST-ATTEMPT для 'sales' в гейте.
  'sales-reconcile': { handler: (ctx) => salesReconcileLoader(ctx), logicalPeriod: (now) => d1Moscow(now), retryTransient: true, sourceWatermark: STATS_SLOT_GUARD },
  // M3 (перенос из Apps Script runWbAdsDaily + catch-up): имя 'ads' = INGEST_RUNS.loader_name — на него
  // завязан freshness-гейт витрины (mart/bq.ts REQUIRED_LOADERS). Период D−1 МСК, как rng.to эталона.
  // retryTransient НЕТ: повтор — следующим окном Scheduler (catch-up), с новым run_id и новым снапшотом.
  ads: { handler: (ctx) => adsLoader(ctx), logicalPeriod: (now) => d1Moscow(now) },
  // M3: хвост runWbAdsDaily (Ads-3 ставки + Ads-2 query stats) — свой loader_name, чтобы журнал 'ads'
  // закрывался до хвоста (в эталоне ingestFinalizeByStatus_ шёл раньше Ads-3). Стартует после 'ads' того же D−1.
  'ads-query': { handler: (ctx) => adsQueryLoader(ctx), logicalPeriod: (now) => d1Moscow(now) },
  // M3: недельный аудит расходов ADSAUDIT_ (loadWbAdsCostsAudit). Отдельное имя: не «последняя попытка» 'ads'.
  // Период — сегодня МСК: окно D−46…D−8 считается от сегодняшней даты, как wbAdsCostsRangeBack_.
  'ads-costs-audit': { handler: (ctx) => adsCostsAuditLoader(ctx), logicalPeriod: (now) => dailyPeriodMoscow(now) },
  // M4 (перенос runWbFinanceDaily из Apps Script): финансовые отчёты WB DAILY/WEEKLY. Период — часовой слот МСК:
  // три слота в сутки (07:30/12:30/18:30) — три независимых прогона, как три триггера Apps Script; повтор в том же
  // часе после COMPLETE не нужен (следующий слот дочитает очередь). shadow → только __CR (finance/targets.ts).
  finance: { handler: (ctx) => financeLoader(ctx), logicalPeriod: (now) => unitkaSlot(now) },
  // M4: ручная регистрация WEEKLY-отчётов периода (wbFinDeepBackfillRegister). Без расписания; грузят обычные тики.
  'finance-backfill': { handler: (ctx) => financeBackfillLoader(ctx), logicalPeriod: (now) => unitkaSlot(now) },
  // M5 (перенос из Apps Script runWbSuppliesDaily): снимок поставок WB, один COMPLETE за сутки МСК.
  // run_id данных уникален на попытку (DL_yyyyMMdd_HHmmss), каждая порция — один load job с
  // детерминированным jobId → повтор после транзиентного отказа добавляет новый снимок, не дубль.
  supplies: { handler: (ctx) => suppliesLoader(ctx), logicalPeriod: (now) => dailyPeriodMoscow(now), retryTransient: true },
  // M5 (перенос из Apps Script runRefSync): версия REF_SKU_MASTER из листа SKU_MASTER. Период —
  // часовой слот МСК: ручной прогон после правки листа не упирается в COMPLETE суточного.
  // Повтор безопасен: новая попытка — новая версия, указатель переключается одной DML.
  'ref-sync': { handler: (ctx) => refSyncLoader(ctx), logicalPeriod: (now) => unitkaSlot(now), retryTransient: true },
};

export function resolveLoader(name: string): LoaderSpec | undefined {
  return LOADERS[name];
}

export function availableLoaderNames(): string {
  return Object.keys(LOADERS).join(', ');
}
