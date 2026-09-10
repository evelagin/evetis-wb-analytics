-- ============================================================================
-- EVETIS · Stage 3B ROLLBACK · R7 · удалить таблицы, которых до cutover не было
-- ПОСЛЕ R2 (вью шага H читают эти таблицы) и ПОСЛЕ R6 (прежняя sp_bootstrap_facts
-- их не создаёт и не трогает — без явного удаления они остались бы сиротами).
-- Данные в них производные: воспроизводятся из FACT_ADS_COSTS_DAILY и
-- FACT_ADS_SKU_DAILY повторным развёртыванием Stage 3B.
-- ============================================================================
DROP TABLE IF EXISTS `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ADS_SPEND_ALLOC_DAILY`;
DROP TABLE IF EXISTS `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ADS_SPEND_UNALLOC_DAILY`;
DROP TABLE IF EXISTS `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ADS_SPEND_ALLOC_DAILY__BUILD`;
DROP TABLE IF EXISTS `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ADS_SPEND_UNALLOC_DAILY__BUILD`;
