-- ============================================================================
-- PHASE 2 · D1 — ОТКАТ регистрации конвейеров Ozon.
-- Удаляет ровно те 11 строк, что добавил d1_register_ozon_pipelines.sql, и ничего больше:
-- перечисление идентификаторов явное, шаблонов вроде LIKE 'ozon%' здесь нет намеренно.
-- Ожидаемое число затронутых строк: 11 DELETE.
-- Данных наблюдений этот откат не трогает: OPS_HEALTH_STATE / OPS_INCIDENT остаются.
-- ============================================================================
DELETE FROM `project-fa311fc0-4d87-4781-986.wb_ops.OPS_PIPELINE_REGISTRY`
WHERE pipeline_id IN (
  'ozon_fbo_postings', 'ozon_stocks', 'ozon_finance_accrual', 'ozon_catalog', 'ozon_prices',
  'ozon_ads_sku_daily', 'ozon_ads_expense_daily', 'ozon_ads_campaigns', 'ozon_supplies',
  'ozon_seller_info', 'ozon_clusters');
