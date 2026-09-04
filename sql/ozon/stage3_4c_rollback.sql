-- =====================================================================
-- Stage 3.4C / 3.4C.3 — откат.
-- Удаляет ТОЛЬКО объекты, введённые Stage 3.4C. Дата: 2026-09-04.
--
-- Не трогает и не может тронуть:
--   * ozon_raw (все RAW_OZON_*) — источник, изменению не подлежит;
--   * evetis_ref (COGS, идентичность SKU, BOM) — слой Stage 3.4B/3.4B.1;
--   * wb_raw, wb_mart — другой домен, правило изоляции маркетплейсов;
--   * Cloud Run job ozon-runtime-ingest;
--   * планировщики ozon-fast / ozon-daily / ozon-weekly.
--
-- Stage 3.4C.3 НЕ создал ни планировщика, ни job, ни процедуры, ни
-- расписания, ни новых прав IAM. Все пять объектов — представления,
-- поэтому конфигурации для отката нет: достаточно DROP.
--
-- Потери данных при откате нет: ни одно представление не хранит
-- данных, которых нет в ozon_raw и evetis_ref.
-- Пересоздание: sql/ozon/stage3_4c_ozon_mart.sql целиком.
-- =====================================================================

-- Порядок обратный зависимостям:
--   V_OZON_SKU_UNIT_ECONOMICS_CURRENT → FCT_OZON_SKU_PNL_MONTHLY
--   V_OZON_LIFETIME_PNL → FCT_OZON_PNL_MONTHLY, FCT_OZON_SKU_PNL_MONTHLY,
--                         V_OZON_MART_FRESHNESS
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_SKU_UNIT_ECONOMICS_CURRENT`;
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_LIFETIME_PNL`;
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_MART_FRESHNESS`;
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_SKU_PNL_MONTHLY`;
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_PNL_MONTHLY`;

-- Страховка на случай отката с версии Stage 3.4C, где два факта были
-- материализованными таблицами.
DROP TABLE IF EXISTS `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_SKU_PNL_MONTHLY`;
DROP TABLE IF EXISTS `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_PNL_MONTHLY`;

-- Датасет удаляется последним и только если он пуст. CASCADE намеренно
-- не используется: если в ozon_mart появились объекты более поздних
-- стадий, откат Stage 3.4C не должен их сносить — DROP SCHEMA без
-- CASCADE в этом случае упадёт, и это правильное поведение.
DROP SCHEMA IF EXISTS `project-fa311fc0-4d87-4781-986.ozon_mart`;

-- Проверка после отката:
--   SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986`.INFORMATION_SCHEMA.SCHEMATA
--   WHERE schema_name = 'ozon_mart';   -- ожидается 0
