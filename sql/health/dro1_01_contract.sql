-- ============================================================================
-- DRO-1 · Data Reliability & Observability v1 · §1 Контракт конвейеров.
-- Док: docs/ops/DRO1_DETECTION_ALERTING_2026-10-04.md
--
-- Единственное место, где живут пороги свежести. Ни одна проба и ни одна
-- классификация не держит своих чисел: всё берётся отсюда по pipeline_id.
-- Контракт — код (repository-first): меняется только PR-ом, не UPDATE-ом.
--
-- Основа — реестр wb_ops.OPS_PIPELINE_REGISTRY (27 строк, снимок 2026-10-04).
-- Все 27 pipeline_id присутствуют (in_registry = TRUE); тест
-- tools/tests/test_dro1_health.py это проверяет. Ещё 13 строк — конвейеры,
-- которых в реестре нет (воронка, хранение, акции, Юнитка, слои V2, CT, ФФ, план).
--
-- Смысл полей:
--   data_class          EVENT_HISTORY | SNAPSHOT | WINDOWED | DERIVED | REFERENCE | MANUAL
--   freshness_basis     SLOT — ждём бизнес-дату (сегодня − business_lag_days) к slot_time_msk;
--                       AGE  — меряем возраст последней успешной загрузки.
--   warn/fail_after_minutes  SLOT: минуты опоздания относительно слота; AGE: возраст.
--   loss_window_days    NULL — пропуск восстановим без срока;
--                       0    — снимок: дата D теряется в 00:00 МСК D+1;
--                       N    — окно источника: дата D теряется после слота D+N.
--   imminent_loss_minutes  за сколько минут до потери поднимается DATA_LOSS_IMMINENT.
--   cumulative_presence TRUE — полная пересборка/LCD: дата d есть, если d <= latest.
--   run_cover_days      окно перечитывания успешного запуска: день без строк, покрытый
--                       успешным запуском в этом окне, — NOT_EXPECTED, а не пропуск.
--   evaluation_mode     EVALUATED | NOT_EVALUATED (причина в not_evaluated_reason).
--
-- Пороги — DRO v1 design §4 (D5). ФФ и план продаж сознательно NOT_EVALUATED:
-- их гейт — отдельный workstream DRO-2 (owner ACK 2026-10-04).
-- ============================================================================
CREATE OR REPLACE VIEW `evetis_health.V_PIPELINE_CONTRACT`
OPTIONS (description = 'DRO-1. Контракт свежести и восстановимости критичных конвейеров EVETIS: пороги WARN/FAIL, слоты, сроки невосстановимой потери. Источник истины для детектора evetis_health. Меняется только через PR (sql/health/dro1_01_contract.sql).') AS
SELECT c.*, 'dro1-2026-10-04' AS contract_version
FROM UNNEST(ARRAY<STRUCT<
  pipeline_id STRING, display_name STRING, marketplace STRING, tenant_id STRING, domain STRING,
  source_system STRING, data_class STRING, criticality STRING, in_registry BOOL,
  evaluation_mode STRING, not_evaluated_reason STRING, alerting_enabled BOOL,
  freshness_basis STRING, cadence STRING, business_lag_days INT64, slot_time_msk STRING,
  warn_after_minutes INT64, fail_after_minutes INT64, loss_window_days INT64,
  imminent_loss_minutes INT64, recoverability STRING, cumulative_presence BOOL,
  run_cover_days INT64, stale_run_minutes INT64, history_start_date DATE,
  latest_period_provisional BOOL, known_source_limitation STRING, evidence_ref STRING>>[
  -- ── WB: реестр ──────────────────────────────────────────────────────────────
  ('orders', 'WB заказы', 'WB', 'evetis', 'sales', 'WB_API/APPS_SCRIPT', 'EVENT_HISTORY', 'CRITICAL', TRUE,
   'EVALUATED', NULL, TRUE, 'AGE', 'ежечасно :31 МСК', 0, NULL, 180, 360, NULL, NULL, 'RECOVERABLE', FALSE,
   NULL, 120, DATE '2026-04-13', FALSE, NULL, 'wb_raw.RAW_WB_ORDERS; wb_raw.INGEST_RUNS(orders)'),
  ('sales', 'WB продажи и возвраты', 'WB', 'evetis', 'sales', 'WB_API/APPS_SCRIPT', 'EVENT_HISTORY', 'CRITICAL', TRUE,
   'EVALUATED', NULL, TRUE, 'AGE', 'ежечасно :22 МСК', 0, NULL, 240, 480, NULL, NULL, 'RECOVERABLE', FALSE,
   NULL, 120, DATE '2026-04-13', FALSE, NULL, 'wb_raw.RAW_WB_SALES_RETURNS; wb_raw.INGEST_RUNS(sales)'),
  ('finance', 'WB финансовый отчёт', 'WB', 'evetis', 'finance', 'WB_API/APPS_SCRIPT', 'EVENT_HISTORY', 'CRITICAL', TRUE,
   'EVALUATED', NULL, TRUE, 'AGE', '07/12/18 МСК + weekly finalize', 0, NULL, 1560, 3000, NULL, NULL, 'RECOVERABLE', FALSE,
   NULL, 240, DATE '2024-09-05', TRUE, NULL, 'wb_raw.RAW_WB_FINANCE; wb_raw.FINANCE_LOADER_RUNS'),
  ('ads_daily', 'WB реклама: статистика (суточный прогон)', 'WB', 'evetis', 'advertising', 'WB_API/APPS_SCRIPT', 'WINDOWED', 'HIGH', TRUE,
   'EVALUATED', NULL, TRUE, 'SLOT', 'ежедневно 05:07 МСК, окно 7 дн.', 1, '05:15', 45, 105, 7, 2880, 'WINDOWED_7D', FALSE,
   NULL, 120, DATE '2026-04-13', FALSE, NULL, 'wb_raw.RAW_WB_ADV_CAMPAIGN_STATS; wb_raw.INGEST_RUNS(ads)'),
  ('ads_costs', 'WB реклама: биллинг (списания)', 'WB', 'evetis', 'advertising', 'WB_API/APPS_SCRIPT', 'EVENT_HISTORY', 'HIGH', TRUE,
   'EVALUATED', NULL, TRUE, 'AGE', 'ежедневно 05:08 МСК, окно D-14..D-1', 0, NULL, 1560, 3000, NULL, NULL, 'RECOVERABLE', FALSE,
   NULL, 60, DATE '2026-04-13', TRUE, NULL, 'wb_raw.RAW_WB_ADV_COSTS_RUNS; wb_raw.V_ADV_COSTS_DAY_COVERAGE'),
  ('ads_query_bids', 'WB реклама: снимок ставок', 'WB', 'evetis', 'advertising', 'WB_API/APPS_SCRIPT', 'SNAPSHOT', 'HIGH', TRUE,
   'EVALUATED', NULL, TRUE, 'SLOT', 'ежедневно ~05:11 МСК', 0, '05:15', 105, 405, 0, 480, 'NON_RECOVERABLE', FALSE,
   NULL, 60, DATE '2026-08-25', FALSE, NULL, 'wb_raw.RAW_WB_ADV_QUERY_BIDS_RUNS'),
  ('ads_query_stats', 'WB реклама: статистика запросов', 'WB', 'evetis', 'advertising', 'WB_API/APPS_SCRIPT', 'EVENT_HISTORY', 'MEDIUM', TRUE,
   'EVALUATED', NULL, TRUE, 'SLOT', 'ежедневно ~05:11 МСК', 1, '05:15', 105, 405, NULL, NULL, 'RECOVERABLE', FALSE,
   NULL, 60, DATE '2026-04-13', FALSE, NULL, 'wb_raw.RAW_WB_ADV_QUERY_STATS_RUNS'),
  ('ads_search_clusters', 'WB реклама: поисковые кластеры', 'WB', 'evetis', 'advertising', 'WB_API/APPS_SCRIPT', 'MANUAL', 'LOW', TRUE,
   'NOT_EVALUATED', 'MANUAL_PIPELINE', FALSE, NULL, 'вручную', NULL, NULL, NULL, NULL, NULL, NULL, 'RECOVERABLE', FALSE,
   NULL, NULL, NULL, FALSE, NULL, 'wb_raw.RAW_WB_ADV_SEARCH_CLUSTERS'),
  ('finance_backfill', 'WB финансы: бэкфилл', 'WB', 'evetis', 'finance', 'WB_API/APPS_SCRIPT', 'EVENT_HISTORY', 'LOW', TRUE,
   'NOT_EVALUATED', 'NO_RUN_LOG', FALSE, NULL, 'интервальный воркер', NULL, NULL, NULL, NULL, NULL, NULL, 'RECOVERABLE', FALSE,
   NULL, NULL, NULL, FALSE, NULL, 'wb_raw.RAW_WB_FINANCE'),
  ('sales_reconcile', 'WB продажи: сверка', 'WB', 'evetis', 'sales', 'APPS_SCRIPT', 'DERIVED', 'MEDIUM', TRUE,
   'NOT_EVALUATED', 'NO_RUN_LOG', FALSE, NULL, 'ежедневно', NULL, NULL, NULL, NULL, NULL, NULL, 'RECOVERABLE', FALSE,
   NULL, NULL, NULL, FALSE, NULL, 'wb_raw.V_WB_SALES_RETURNS'),
  ('stocks_cloudrun', 'WB остатки: Cloud Run (prod выключен)', 'WB', 'evetis', 'inventory', 'WB_API/CLOUD_RUN', 'SNAPSHOT', 'HIGH', TRUE,
   'NOT_EVALUATED', 'DISABLED_IN_REGISTRY', FALSE, NULL, 'на паузе (noop)', NULL, NULL, NULL, NULL, NULL, NULL, 'NON_RECOVERABLE', FALSE,
   NULL, NULL, NULL, FALSE, NULL, 'wb_raw.LOADER_RUNS(stocks)'),
  ('stocks_snapshot', 'WB остатки (снимок)', 'WB', 'evetis', 'inventory', 'WB_API/APPS_SCRIPT', 'SNAPSHOT', 'HIGH', TRUE,
   'EVALUATED', NULL, TRUE, 'SLOT', 'ежедневно 06:23 МСК', 0, '06:30', 90, 330, 0, 480, 'NON_RECOVERABLE', FALSE,
   NULL, 180, DATE '2026-07-16', FALSE, NULL, 'wb_raw.RAW_WB_STOCKS; wb_raw.WB_STOCKS_SNAPSHOTS'),
  ('wb_prices_observer', 'WB цены продавца', 'WB', 'evetis', 'pricing', 'WB_API/CLOUD_RUN', 'SNAPSHOT', 'HIGH', TRUE,
   'EVALUATED', NULL, TRUE, 'AGE', 'каждые 20 мин', 0, NULL, 45, 90, NULL, NULL, 'NON_RECOVERABLE', FALSE,
   NULL, 60, DATE '2026-09-07', FALSE, NULL, 'wb_raw.WB_PRICES_OBSERVATIONS; wb_raw.RAW_WB_PRICES'),
  ('wb_tariffs_loader', 'WB тарифы', 'WB', 'evetis', 'pricing', 'WB_API/CLOUD_RUN', 'SNAPSHOT', 'HIGH', TRUE,
   'EVALUATED', NULL, TRUE, 'SLOT', 'ежедневно 08:15 МСК', 0, '08:30', 90, 330, 0, 480, 'NON_RECOVERABLE', FALSE,
   NULL, 180, DATE '2026-09-07', FALSE, NULL, 'wb_raw.WB_TARIFF_OBSERVATIONS'),
  ('ref_sync', 'Справочник SKU (ref-sync)', 'WB', 'evetis', 'reference', 'APPS_SCRIPT', 'REFERENCE', 'HIGH', TRUE,
   'EVALUATED', NULL, TRUE, 'AGE', 'ежедневно 08:22 МСК', 0, NULL, 1560, 3000, NULL, NULL, 'RECOVERABLE', FALSE,
   NULL, 120, NULL, FALSE, NULL, 'wb_raw.REF_SYNC_RUNS; wb_raw.REF_SKU_MASTER'),
  ('mart', 'WB витрина (FACT_* / MART_SKU_DAILY)', 'WB', 'evetis', 'mart', 'CLOUD_RUN', 'DERIVED', 'CRITICAL', TRUE,
   'EVALUATED', NULL, TRUE, 'SLOT', '07/09/12/16 МСК, полная пересборка', 1, '07:30', 30, 150, NULL, NULL, 'RECOVERABLE', TRUE,
   NULL, 180, DATE '2026-08-05', FALSE, NULL, 'wb_mart.MART_RUNS; wb_mart.MART_SKU_DAILY'),
  -- ── Ozon: реестр ────────────────────────────────────────────────────────────
  ('ozon_fbo_postings', 'Ozon отправления FBO', 'OZON', 'evetis', 'sales', 'OZON_API/CLOUD_RUN', 'EVENT_HISTORY', 'CRITICAL', TRUE,
   'EVALUATED', NULL, TRUE, 'AGE', '07/13/19 МСК, окно 30 дн.', 0, NULL, 480, 840, NULL, NULL, 'RECOVERABLE', FALSE,
   NULL, 180, DATE '2025-05-01', FALSE, 'order_date хранится как UTC-дата (TIMEZONE_BOUNDARY); статусы старше 30 дн. не обновляются',
   'ozon_raw.RAW_OZON_POSTINGS_FBO; ozon_raw.OZON_INGESTION_RUNS(fbo_postings)'),
  ('ozon_finance_accrual', 'Ozon начисления', 'OZON', 'evetis', 'finance', 'OZON_API/CLOUD_RUN', 'EVENT_HISTORY', 'CRITICAL', TRUE,
   'EVALUATED', NULL, TRUE, 'AGE', 'ежедневно 06:30 МСК, окно 30 дн.', 0, NULL, 1560, 3000, NULL, NULL, 'RECOVERABLE', FALSE,
   NULL, 180, DATE '2025-05-05', FALSE, NULL, 'ozon_raw.RAW_OZON_FINANCE_ACCRUAL; ozon_raw.OZON_INGESTION_RUNS(finance_accrual)'),
  ('ozon_ads_expense_daily', 'Ozon реклама: расход', 'OZON', 'evetis', 'advertising', 'OZON_API/CLOUD_RUN', 'EVENT_HISTORY', 'MEDIUM', TRUE,
   'EVALUATED', NULL, TRUE, 'SLOT', 'ежедневно 06:30 МСК, окно 7 дн.', 1, '07:00', 120, 1560, NULL, NULL, 'RECOVERABLE', FALSE,
   7, 180, DATE '2025-05-05', FALSE, NULL, 'ozon_raw.RAW_OZON_ADS_EXPENSE_DAILY; ozon_raw.OZON_INGESTION_RUNS(ads_expense_daily)'),
  ('ozon_ads_sku_daily', 'Ozon реклама: атрибуция на SKU', 'OZON', 'evetis', 'advertising', 'OZON_API/CLOUD_RUN', 'EVENT_HISTORY', 'HIGH', TRUE,
   'EVALUATED', NULL, TRUE, 'SLOT', 'ежедневно 06:30 МСК, окно 7 дн.', 1, '07:00', 120, 1560, NULL, NULL, 'RECOVERABLE', FALSE,
   7, 180, DATE '2025-05-05', FALSE, 'обрезка отчёта не фиксируется в журнале (строгий режим не развёрнут)',
   'ozon_raw.RAW_OZON_ADS_SKU_DAILY; ozon_raw.OZON_INGESTION_RUNS(ads_sku_daily)'),
  ('ozon_ads_campaigns', 'Ozon реклама: кампании (снимок)', 'OZON', 'evetis', 'advertising', 'OZON_API/CLOUD_RUN', 'SNAPSHOT', 'MEDIUM', TRUE,
   'EVALUATED', NULL, TRUE, 'SLOT', 'ежедневно 06:30 МСК', 0, '07:00', 120, 420, 0, 480, 'NON_RECOVERABLE', FALSE,
   NULL, 180, DATE '2026-09-03', FALSE, NULL, 'ozon_raw.OZON_INGESTION_RUNS(ads_campaigns)'),
  ('ozon_catalog', 'Ozon каталог (снимок)', 'OZON', 'evetis', 'reference', 'OZON_API/CLOUD_RUN', 'SNAPSHOT', 'HIGH', TRUE,
   'EVALUATED', NULL, TRUE, 'SLOT', 'ежедневно 06:30 МСК', 0, '07:00', 120, 420, 0, 480, 'NON_RECOVERABLE', FALSE,
   NULL, 180, DATE '2026-08-31', FALSE, NULL, 'ozon_raw.RAW_OZON_CATALOG'),
  ('ozon_prices', 'Ozon цены (снимок)', 'OZON', 'evetis', 'pricing', 'OZON_API/CLOUD_RUN', 'SNAPSHOT', 'HIGH', TRUE,
   'EVALUATED', NULL, TRUE, 'SLOT', 'ежедневно 06:30 МСК', 0, '07:00', 120, 420, 0, 480, 'NON_RECOVERABLE', FALSE,
   NULL, 180, DATE '2026-08-31', FALSE, NULL, 'ozon_raw.RAW_OZON_PRICES'),
  ('ozon_stocks', 'Ozon остатки (снимок)', 'OZON', 'evetis', 'inventory', 'OZON_API/CLOUD_RUN', 'SNAPSHOT', 'HIGH', TRUE,
   'EVALUATED', NULL, TRUE, 'SLOT', '07/13/19 МСК', 0, '07:15', 105, 405, 0, 480, 'NON_RECOVERABLE', FALSE,
   NULL, 180, DATE '2026-08-31', FALSE, NULL, 'ozon_raw.RAW_OZON_STOCKS'),
  ('ozon_seller_info', 'Ozon сведения о продавце', 'OZON', 'evetis', 'reference', 'OZON_API/CLOUD_RUN', 'SNAPSHOT', 'LOW', TRUE,
   'EVALUATED', NULL, TRUE, 'AGE', 'ежедневно 06:30 МСК', 0, NULL, 1560, 3000, NULL, NULL, 'NON_RECOVERABLE', FALSE,
   NULL, 180, DATE '2026-09-06', FALSE, NULL, 'ozon_raw.OZON_INGESTION_RUNS(seller_info)'),
  ('ozon_supplies', 'Ozon поставки', 'OZON', 'evetis', 'inventory', 'OZON_API/CLOUD_RUN', 'SNAPSHOT', 'MEDIUM', TRUE,
   'EVALUATED', NULL, TRUE, 'AGE', 'ежедневно 06:30 МСК', 0, NULL, 1560, 3000, NULL, NULL, 'NON_RECOVERABLE', FALSE,
   NULL, 180, NULL, FALSE, NULL, 'ozon_raw.OZON_INGESTION_RUNS(supplies)'),
  ('ozon_clusters', 'Ozon кластеры складов', 'OZON', 'evetis', 'reference', 'OZON_API/CLOUD_RUN', 'SNAPSHOT', 'LOW', TRUE,
   'EVALUATED', NULL, TRUE, 'AGE', 'еженедельно пн 05:00 МСК', 0, NULL, 11520, 21600, NULL, NULL, 'NON_RECOVERABLE', FALSE,
   NULL, 180, NULL, FALSE, NULL, 'ozon_raw.OZON_INGESTION_RUNS(clusters)'),
  -- ── WB: вне реестра ─────────────────────────────────────────────────────────
  ('wb_funnel', 'WB воронка', 'WB', 'evetis', 'sales', 'WB_API/CLOUD_RUN', 'WINDOWED', 'HIGH', FALSE,
   'EVALUATED', NULL, TRUE, 'SLOT', 'ежедневно 09:30 МСК, окно 7 дн.', 1, '09:45', 135, 1575, 7, 2880, 'WINDOWED_7D', FALSE,
   NULL, 120, DATE '2026-09-04', FALSE, NULL, 'wb_raw.RAW_WB_FUNNEL_DAILY; wb_raw.LOADER_RUNS(funnel)'),
  ('wb_paid_storage', 'WB платное хранение', 'WB', 'evetis', 'finance', 'WB_API/CLOUD_RUN', 'WINDOWED', 'HIGH', FALSE,
   'EVALUATED', NULL, TRUE, 'SLOT', 'ежедневно 11:45 МСК, окно 8 дн.', 1, '12:00', 120, 1560, 8, 2880, 'WINDOWED_8D', FALSE,
   NULL, 120, DATE '2026-09-01', FALSE, NULL, 'wb_raw.RAW_WB_PAID_STORAGE; wb_raw.LOADER_RUNS(storage)'),
  ('wb_promo', 'WB акции (календарь)', 'WB', 'evetis', 'promotions', 'WB_API/CLOUD_RUN', 'SNAPSHOT', 'MEDIUM', FALSE,
   'EVALUATED', NULL, TRUE, 'AGE', '07/12/17/22 МСК', 0, NULL, 420, 780, NULL, NULL, 'NON_RECOVERABLE', FALSE,
   NULL, 120, DATE '2026-09-22', FALSE, 'состав автоакций WB не отдаёт: номенклатура SOURCE_NOT_OBSERVABLE',
   'wb_raw.LOADER_RUNS(promo); wb_raw.RAW_WB_PROMO_CALENDAR'),
  ('promo_basis_wb', 'WB база экономики акций', 'WB', 'evetis', 'promotions', 'BIGQUERY_SCHEDULER', 'SNAPSHOT', 'MEDIUM', FALSE,
   'EVALUATED', NULL, TRUE, 'AGE', '07:10/12:10/17:10/22:10 МСК', 0, NULL, 420, 780, NULL, NULL, 'NON_RECOVERABLE', FALSE,
   NULL, NULL, DATE '2026-09-24', FALSE, NULL, 'wb_raw.WB_PROMO_ECONOMICS_BASIS_SNAPSHOT'),
  ('executive_v2', 'Executive V2 (материализованный слой)', 'WB', 'evetis', 'mart', 'BIGQUERY_SCHEDULER', 'DERIVED', 'HIGH', FALSE,
   'EVALUATED', NULL, TRUE, 'AGE', 'ежечасно :10, 07–23 МСК', 0, NULL, 600, 1020, NULL, NULL, 'RECOVERABLE', FALSE,
   NULL, 60, NULL, FALSE, 'source_data_through = D-2 при витрине D-1 — задуман ли лаг, не доказано (D6)',
   'wb_mart.EXECUTIVE_V2_BUILD_LOG'),
  ('sku_performance_v2', 'SKU Performance V2', 'WB', 'evetis', 'mart', 'BIGQUERY_SCHEDULER', 'DERIVED', 'HIGH', FALSE,
   'EVALUATED', NULL, TRUE, 'AGE', 'ежечасно :20, 07–23 МСК', 0, NULL, 600, 1020, NULL, NULL, 'RECOVERABLE', FALSE,
   NULL, 60, NULL, FALSE, 'source_data_through = D-2 при витрине D-1 — задуман ли лаг, не доказано (D6)',
   'wb_mart.SKU_PERFORMANCE_V2_BUILD_LOG'),
  ('unitka_wb', 'Юнитка WB (LCD)', 'WB', 'evetis', 'unitka', 'CLOUD_RUN', 'DERIVED', 'HIGH', FALSE,
   'EVALUATED', NULL, TRUE, 'SLOT', '10:00 + резерв 12:30 МСК', 1, '13:00', 0, 1440, NULL, NULL, 'RECOVERABLE', TRUE,
   NULL, 120, DATE '2026-09-11', FALSE, NULL, 'wb_ops.UNITKA_ENGINE_RUNS (prod, WRITE, PASS)'),
  ('ct_refresh', 'Control Tower: пересчёт', 'SHARED', 'evetis', 'inventory', 'BIGQUERY_SCHEDULER', 'DERIVED', 'MEDIUM', FALSE,
   'EVALUATED', NULL, TRUE, 'AGE', '07:40/09:40/12:40/16:40/19:40 МСК', 0, NULL, 1560, 3000, NULL, NULL, 'RECOVERABLE', FALSE,
   NULL, NULL, NULL, FALSE, 'свежесть входного остатка ФФ здесь не оценивается — гейт DRO-2',
   'evetis_ref.CT_REFRESH_LOG(sp_ct_refresh_daily)'),
  ('ff_stock', 'Остаток фулфилмента (Usend)', 'SHARED', 'evetis', 'inventory', 'MANUAL', 'MANUAL', 'HIGH', FALSE,
   'NOT_EVALUATED', 'DEFERRED_DRO2', FALSE, NULL, 'ручной пересчёт', NULL, NULL, NULL, NULL, NULL, NULL, 'RECOVERABLE', FALSE,
   NULL, NULL, NULL, FALSE, NULL, 'evetis_ref.CT_STOCK_SNAPSHOT; evetis_ops'),
  ('sales_plan', 'План продаж (утверждённый)', 'SHARED', 'evetis', 'planning', 'MANUAL', 'MANUAL', 'HIGH', FALSE,
   'NOT_EVALUATED', 'DEFERRED_DRO2', FALSE, NULL, 'при утверждении', NULL, NULL, NULL, NULL, NULL, NULL, 'RECOVERABLE', FALSE,
   NULL, NULL, NULL, FALSE, NULL, 'evetis_ref.PLAN_LINE_MONTHLY; evetis_mart.V_SALES_PLAN_APPROVED'),
  -- ── Ozon: вне реестра ───────────────────────────────────────────────────────
  ('ozon_promo', 'Ozon акции', 'OZON', 'evetis', 'promotions', 'OZON_API/CLOUD_RUN', 'SNAPSHOT', 'MEDIUM', FALSE,
   'EVALUATED', NULL, TRUE, 'AGE', '07/12/17/22 МСК', 0, NULL, 420, 780, NULL, NULL, 'NON_RECOVERABLE', FALSE,
   NULL, 180, DATE '2026-09-22', FALSE, NULL, 'ozon_raw.OZON_INGESTION_RUNS(promo)'),
  ('promo_basis_ozon', 'Ozon база экономики акций', 'OZON', 'evetis', 'promotions', 'BIGQUERY_SCHEDULER', 'SNAPSHOT', 'MEDIUM', FALSE,
   'EVALUATED', NULL, TRUE, 'AGE', '07:10/12:10/17:10/22:10 МСК', 0, NULL, 420, 780, NULL, NULL, 'NON_RECOVERABLE', FALSE,
   NULL, NULL, DATE '2026-09-24', FALSE, NULL, 'ozon_raw.OZON_PROMO_ECONOMICS_BASIS_SNAPSHOT'),
  ('unitka_ozon', 'Юнитка Ozon', 'OZON', 'evetis', 'unitka', 'CLOUD_RUN', 'DERIVED', 'HIGH', FALSE,
   'EVALUATED', NULL, TRUE, 'AGE', 'ежедневно 10:00 МСК', 0, NULL, 1560, 3000, NULL, NULL, 'RECOVERABLE', FALSE,
   NULL, 120, NULL, FALSE, 'LCD Ozon хранится только в листе и логах — в BigQuery не наблюдаем',
   'wb_raw.LOADER_RUNS(ozon-unitka)')
]) AS c;
