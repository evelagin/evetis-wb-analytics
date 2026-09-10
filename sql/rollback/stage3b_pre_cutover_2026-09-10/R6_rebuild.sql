-- ============================================================================
-- EVETIS · Stage 3B ROLLBACK · R6 · пересборка FACT и MART прежними процедурами
-- Исполнять ПОСЛЕ R1–R4 (и R5 при полном откате). Прежние процедуры пересоздают
-- таблицы через CREATE OR REPLACE TABLE, поэтому схема MART_SKU_DAILY вернётся к
-- 60 колонкам сама — отдельного DDL для таблиц не требуется.
-- ============================================================================
CALL `project-fa311fc0-4d87-4781-986.wb_mart.sp_bootstrap_facts`('');
CALL `project-fa311fc0-4d87-4781-986.wb_mart.sp_build_mart_sku_daily`(DATE_SUB(CURRENT_DATE('Europe/Moscow'), INTERVAL 1 DAY), NULL, '');
