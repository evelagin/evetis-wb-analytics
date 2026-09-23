-- ============================================================================
-- PR-PROMO-2 · ОТКАТ канонического слоя состояния акций.
-- Удаляет ТОЛЬКО 16 представлений, созданных PR-PROMO-2, в порядке, обратном
-- зависимостям (сначала evetis_mart, затем ozon_mart и wb_mart).
-- Данных в представлениях нет: RAW наблюдения (PR-PROMO-1) не затрагиваются, история
-- наблюдений сохраняется полностью, наблюдатели продолжают работать.
-- Ни один существующий до PR-PROMO-2 объект здесь не упомянут.
-- После отката: удалить записи 11 объектов из sql/current/*/MANIFEST.json и их файлы,
-- иначе R2C покажет MISSING_LIVE.
-- ============================================================================
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_OBSERVABILITY_CURRENT`;
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_SKU_STATE_CURRENT`;
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_SKU_STATE_HISTORY`;
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_SKU_EVIDENCE_HISTORY`;
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_STATE_CURRENT`;
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_STATE_HISTORY`;
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_OBSERVATION_HISTORY`;
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_PROMO_SKU_STATE_HISTORY`;
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_PROMO_SKU_EVIDENCE_HISTORY`;
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_PROMO_HISTORY`;
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_PROMO_OBSERVATION_HISTORY`;
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_PROMO_SKU_STATE_HISTORY`;
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_PROMO_SKU_EVIDENCE_HISTORY`;
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_PROMO_RANGING_HISTORY`;
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_PROMO_HISTORY`;
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_PROMO_OBSERVATION_HISTORY`;
