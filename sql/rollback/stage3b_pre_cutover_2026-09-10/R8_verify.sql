-- ============================================================================
-- EVETIS · Stage 3B ROLLBACK · R8 · доказать, что состояние до cutover восстановлено
-- Ожидаемые значения — из этого манифеста (снято 2026-09-10 до B1).
-- Затем FP_fingerprint.sql и сравнение с B0.
-- ============================================================================
SELECT 'routine' AS kind, routine_name AS obj, TO_HEX(SHA256(routine_definition)) AS sha256
FROM `project-fa311fc0-4d87-4781-986.wb_mart.INFORMATION_SCHEMA.ROUTINES`
WHERE routine_name IN ('sp_bootstrap_facts','sp_build_mart_sku_daily')
UNION ALL
SELECT 'view', table_name, TO_HEX(SHA256(view_definition))
FROM `project-fa311fc0-4d87-4781-986.wb_mart.INFORMATION_SCHEMA.VIEWS`
WHERE table_name IN ('V_ADS_FUNNEL_QUERY_DAILY','V_ADS_FUNNEL_QUERY_28D','V_ADS_FUNNEL_QUERY_90D',
  'V_ADS_FUNNEL_SKU_28D','V_ADS_FUNNEL_SIGNALS','V_DATA_FRESHNESS','V_ADS_SCREEN_SKU','V_ADS_SCREEN_QUERY',
  'V_ADS_SPEND_RECONCILIATION','V_ADS_SPEND_RECONCILIATION_DAILY')
UNION ALL
SELECT 'view', table_name, TO_HEX(SHA256(view_definition))
FROM `project-fa311fc0-4d87-4781-986.wb_raw.INFORMATION_SCHEMA.VIEWS` WHERE table_name = 'V_ADV_COSTS'
UNION ALL
SELECT 'table', table_name, CAST(NULL AS STRING)
FROM `project-fa311fc0-4d87-4781-986.wb_mart.INFORMATION_SCHEMA.TABLES`
WHERE table_name LIKE 'FACT_ADS_SPEND_%'
ORDER BY kind, obj;
-- ожидается: 2 routine + 8 view wb_mart + V_ADV_COSTS с sha256 из манифеста §2;
--            V_ADS_SPEND_RECONCILIATION* и FACT_ADS_SPEND_* — ОТСУТСТВУЮТ.
