-- ============================================================================
-- ОТКАТ SKU PERFORMANCE V2 · PHASE B (BigQuery)
--
-- Слой изолирован: ни dashboard 3, ни Executive V2 его не читают (Metabase на Phase B не менялся).
-- Канонические объекты (MART_SKU_DAILY, V_DASH_*, Executive V2) откат не затрагивает.
--
-- ПОРЯДОК:
--   1. Остановить расписание: `gcloud scheduler jobs pause sku-performance-v2-layer-build --location=europe-west1`
--      или удалить infra/terraform/sku_performance_v2_layer.tf (+ запись sku_v2_layer в iam.tf) целевым apply.
--   2. Выполнить этот файл.
-- ============================================================================
DROP TABLE FUNCTION IF EXISTS `project-fa311fc0-4d87-4781-986.wb_mart.TVF_SKU_PERFORMANCE_V2_PERIOD`;
DROP VIEW      IF EXISTS `project-fa311fc0-4d87-4781-986.wb_mart.V_SKU_PERFORMANCE_V2_EXEC_BRIDGE_DAILY`;
DROP VIEW      IF EXISTS `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_SKU_PERFORMANCE_V2_DAILY`;
DROP PROCEDURE IF EXISTS `project-fa311fc0-4d87-4781-986.wb_mart.sp_build_sku_performance_v2_daily`;
DROP TABLE     IF EXISTS `project-fa311fc0-4d87-4781-986.wb_mart.SKU_PERFORMANCE_V2_DAILY`;
DROP TABLE     IF EXISTS `project-fa311fc0-4d87-4781-986.wb_mart._SKU_PERFORMANCE_V2_BUILD_LOCK`;
DROP TABLE     IF EXISTS `project-fa311fc0-4d87-4781-986.wb_mart.SKU_PERFORMANCE_V2_CONFIG`;
-- Журнал сборок оставить как аудиторский след; удалить отдельно при необходимости:
-- DROP TABLE IF EXISTS `project-fa311fc0-4d87-4781-986.wb_mart.SKU_PERFORMANCE_V2_BUILD_LOG`;
