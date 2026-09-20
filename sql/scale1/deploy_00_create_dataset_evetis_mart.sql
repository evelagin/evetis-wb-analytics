-- SCALE 1 · шаг развёртывания 0: датасет нейтрального слоя. ВЫПОЛНЯЕТ ВЛАДЕЛЕЦ, после ревью PR.
-- Идемпотентно. Данных не содержит: в датасете живёт только вью FACT_SKU_DAILY.
-- IAM не нужен: metabase-read-only имеет roles/bigquery.dataViewer на проект (проверено 2026-09-20).
-- Дальше — канонические файлы, строго в этом порядке (каждый — один CREATE OR REPLACE VIEW):
--   1. sql/current/ozon_mart/V_OZON_COMMISSION_RECOVERY.sql
--   2. sql/current/ozon_mart/FCT_OZON_SKU_PNL_DAILY.sql
--   3. sql/current/evetis_mart/FACT_SKU_DAILY.sql
-- Затем: sql/scale1/fact_sku_daily_validation.sql (как есть, все блоки PASS) и R2C → PR снятия снимка.
CREATE SCHEMA IF NOT EXISTS `project-fa311fc0-4d87-4781-986.evetis_mart`
OPTIONS (location = 'EU', description = 'EVETIS · нейтральный к площадке слой (SCALE 1). Приводит авторитетные факты wb_mart и ozon_mart к одному контракту по internal_sku. Экономику площадок заново не считает, RAW не читает.');
