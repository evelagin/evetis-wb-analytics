-- ============================================================================
-- PHASE 2 · D1 — регистрация конвейеров Ozon в реестре наблюдаемости.
-- ТОЛЬКО ДАННЫЕ: ни одного DDL, ни одного изменения существующих строк.
-- Ожидаемое число затронутых строк: 11 INSERT, 0 UPDATE, 0 DELETE.
-- Откат: sql/ops/phase2_health/d1_rollback.sql (DELETE ровно этих 11 pipeline_id).
--
-- Почему это нужно: 11 сущностей Ozon грузятся ежедневно и ведут собственный журнал
-- ozon_raw.OZON_INGESTION_RUNS, но платформенный реестр о них не знает — вопрос
-- «здорова ли платформа» для Ozon сегодня не имеет машинного ответа.
--
-- 🔴 ЕДИНСТВЕННЫЙ ПАРАМЕТР, ТРЕБУЮЩИЙ ПОДТВЕРЖДЕНИЯ ВЛАДЕЛЬЦА — freshness_sla_minutes.
--    Он не изобретён: взято уже одобренное отношение из WB-конвейера того же класса
--    (ads_daily: суточная каденция → 2160 мин = 24 ч + 50 % запаса). По той же
--    пропорции: 3 раза в сутки (8 ч) → 720 мин; раз в неделю (168 ч) → 15120 мин.
--    Если владелец считает запас другим — меняется одно число в каждой строке.
--
-- Каденция взята из Cloud Scheduler (infra/terraform/ozon_ingestion.tf), а не из
-- наблюдения: ozon-daily «30 6 * * *», ozon-fast «0 7,13,19 * * *»,
-- ozon-weekly «0 5 * * 1», зона Europe/Moscow.
-- ============================================================================
INSERT INTO `project-fa311fc0-4d87-4781-986.wb_ops.OPS_PIPELINE_REGISTRY`
  (pipeline_id, pipeline_name, parent_pipeline_id, source_system, target_object, environment,
   enabled, criticality, cadence_type, cadence_spec, cadence_definition_source,
   runtime_verification_status, cadence_observed_evidence, cadence_last_observed_at,
   is_failure_isolated, is_snapshot_only, is_backfillable, missed_run_data_loss,
   freshness_sla_minutes, stale_run_threshold_minutes, expected_business_lag_days,
   grace_period_minutes, reminder_interval_hours, run_log_source, coverage_check_enabled,
   owner, notes, updated_at)
SELECT * FROM UNNEST([
  STRUCT('ozon_fbo_postings' AS pipeline_id, 'Ozon: отправления FBO' AS pipeline_name,
    CAST(NULL AS STRING) AS parent_pipeline_id, 'OZON_API' AS source_system,
    'ozon_raw.RAW_OZON_POSTINGS_FBO' AS target_object, 'prod' AS environment,
    TRUE AS enabled, 'CRITICAL' AS criticality, 'MULTI_DAILY' AS cadence_type,
    '0 7,13,19 * * * Europe/Moscow' AS cadence_spec, 'TERRAFORM' AS cadence_definition_source,
    'VERIFIED' AS runtime_verification_status,
    'OZON_INGESTION_RUNS: 44 прогона за 14 суток, все OK, errors=0 (замер 2026-09-21)' AS cadence_observed_evidence,
    CAST(NULL AS TIMESTAMP) AS cadence_last_observed_at,
    FALSE AS is_failure_isolated, FALSE AS is_snapshot_only, TRUE AS is_backfillable,
    FALSE AS missed_run_data_loss,
    720 AS freshness_sla_minutes, 120 AS stale_run_threshold_minutes,
    1 AS expected_business_lag_days, 60 AS grace_period_minutes, 12 AS reminder_interval_hours,
    'ozon_raw.OZON_INGESTION_RUNS' AS run_log_source, TRUE AS coverage_check_enabled,
    'evelagin' AS owner,
    'Источник заказов и выручки Ozon; вход суточного факта ozon_mart.FCT_OZON_SKU_PNL_DAILY.' AS notes,
    CURRENT_TIMESTAMP() AS updated_at),
  ('ozon_stocks', 'Ozon: остатки', NULL, 'OZON_API', 'ozon_raw.RAW_OZON_STOCKS', 'prod',
    TRUE, 'HIGH', 'MULTI_DAILY', '0 7,13,19 * * * Europe/Moscow', 'TERRAFORM', 'VERIFIED',
    'OZON_INGESTION_RUNS: 44 прогона за 14 суток, все OK', NULL,
    FALSE, TRUE, FALSE, TRUE, 720, 120, 1, 60, 12, 'ozon_raw.OZON_INGESTION_RUNS', TRUE, 'evelagin',
    'SNAPSHOT-ONLY: история остатков у Ozon не запрашивается, пропущенное окно не восстанавливается.',
    CURRENT_TIMESTAMP()),
  ('ozon_finance_accrual', 'Ozon: начисления финансов', NULL, 'OZON_API',
    'ozon_raw.RAW_OZON_FINANCE_ACCRUAL', 'prod', TRUE, 'CRITICAL', 'DAILY',
    '30 6 * * * Europe/Moscow', 'TERRAFORM', 'VERIFIED',
    'OZON_INGESTION_RUNS: 15 прогонов за 14 суток, все OK', NULL,
    FALSE, FALSE, TRUE, FALSE, 2160, 180, 3, 90, 12, 'ozon_raw.OZON_INGESTION_RUNS', TRUE, 'evelagin',
    'Комиссии и прямые переменные расходы P&L Ozon.', CURRENT_TIMESTAMP()),
  ('ozon_catalog', 'Ozon: каталог товаров', NULL, 'OZON_API', 'ozon_raw.RAW_OZON_CATALOG', 'prod',
    TRUE, 'HIGH', 'DAILY', '30 6 * * * Europe/Moscow', 'TERRAFORM', 'VERIFIED',
    'OZON_INGESTION_RUNS: 15 прогонов за 14 суток, все OK', NULL,
    FALSE, TRUE, FALSE, FALSE, 2160, 180, 1, 90, 12, 'ozon_raw.OZON_INGESTION_RUNS', TRUE, 'evelagin',
    'Связка offer_id → internal_sku через evetis_ref.REF_SKU_CHANNEL_MAP.', CURRENT_TIMESTAMP()),
  ('ozon_prices', 'Ozon: цены и комиссии', NULL, 'OZON_API', 'ozon_raw.RAW_OZON_PRICES', 'prod',
    TRUE, 'HIGH', 'DAILY', '30 6 * * * Europe/Moscow', 'TERRAFORM', 'VERIFIED',
    'OZON_INGESTION_RUNS: 15 прогонов за 14 суток, все OK', NULL,
    FALSE, TRUE, FALSE, TRUE, 2160, 180, 0, 90, 12, 'ozon_raw.OZON_INGESTION_RUNS', TRUE, 'evelagin',
    'SNAPSHOT-ONLY: истории цен Ozon не отдаёт.', CURRENT_TIMESTAMP()),
  ('ozon_ads_sku_daily', 'Ozon: рекламный расход по SKU', NULL, 'OZON_API',
    'ozon_raw.RAW_OZON_ADS_SKU_DAILY', 'prod', TRUE, 'HIGH', 'DAILY',
    '30 6 * * * Europe/Moscow', 'TERRAFORM', 'VERIFIED',
    'OZON_INGESTION_RUNS: 15 прогонов за 14 суток, все OK', NULL,
    FALSE, FALSE, TRUE, FALSE, 2160, 180, 2, 90, 12, 'ozon_raw.OZON_INGESTION_RUNS', TRUE, 'evelagin',
    'Атрибуция рекламы Ozon. Окно Performance API — не более 62 суток.', CURRENT_TIMESTAMP()),
  ('ozon_ads_expense_daily', 'Ozon: расход по кампаниям', NULL, 'OZON_API',
    'ozon_raw.RAW_OZON_ADS_EXPENSE_DAILY', 'prod', TRUE, 'MEDIUM', 'DAILY',
    '30 6 * * * Europe/Moscow', 'TERRAFORM', 'VERIFIED',
    'OZON_INGESTION_RUNS: 15 прогонов за 14 суток, все OK', NULL,
    TRUE, FALSE, TRUE, FALSE, 2160, 180, 2, 90, 24, 'ozon_raw.OZON_INGESTION_RUNS', TRUE, 'evelagin',
    NULL, CURRENT_TIMESTAMP()),
  ('ozon_ads_campaigns', 'Ozon: кампании', NULL, 'OZON_API', 'ozon_raw.RAW_OZON_ADS_CAMPAIGNS',
    'prod', TRUE, 'MEDIUM', 'DAILY', '30 6 * * * Europe/Moscow', 'TERRAFORM', 'VERIFIED',
    'OZON_INGESTION_RUNS: 15 прогонов за 14 суток, все OK', NULL,
    TRUE, TRUE, FALSE, FALSE, 2160, 180, 0, 90, 24, 'ozon_raw.OZON_INGESTION_RUNS', TRUE, 'evelagin',
    'SNAPSHOT-ONLY: настройки кампаний историей не отдаются.', CURRENT_TIMESTAMP()),
  ('ozon_supplies', 'Ozon: поставки', NULL, 'OZON_API', 'ozon_raw.RAW_OZON_SUPPLIES', 'prod',
    TRUE, 'MEDIUM', 'DAILY', '30 6 * * * Europe/Moscow', 'TERRAFORM', 'VERIFIED',
    'OZON_INGESTION_RUNS: 15 прогонов за 14 суток, все OK', NULL,
    TRUE, FALSE, TRUE, FALSE, 2160, 180, 1, 90, 24, 'ozon_raw.OZON_INGESTION_RUNS', TRUE, 'evelagin',
    NULL, CURRENT_TIMESTAMP()),
  ('ozon_seller_info', 'Ozon: сведения о продавце', NULL, 'OZON_API',
    'ozon_raw.RAW_OZON_SELLER_INFO', 'prod', TRUE, 'LOW', 'DAILY',
    '30 6 * * * Europe/Moscow', 'TERRAFORM', 'VERIFIED',
    'OZON_INGESTION_RUNS: 15 прогонов за 14 суток, все OK', NULL,
    TRUE, TRUE, FALSE, FALSE, 2160, 180, 0, 90, 24, 'ozon_raw.OZON_INGESTION_RUNS', FALSE, 'evelagin',
    NULL, CURRENT_TIMESTAMP()),
  ('ozon_clusters', 'Ozon: кластеры складов', NULL, 'OZON_API', 'ozon_raw.RAW_OZON_CLUSTERS',
    'prod', TRUE, 'LOW', 'WEEKLY', '0 5 * * 1 Europe/Moscow', 'TERRAFORM', 'VERIFIED',
    'OZON_INGESTION_RUNS: 3 прогона за 14 суток, все OK', NULL,
    TRUE, TRUE, FALSE, FALSE, 15120, 180, 0, 120, 24, 'ozon_raw.OZON_INGESTION_RUNS', FALSE, 'evelagin',
    NULL, CURRENT_TIMESTAMP())
]);
