-- ============================================================================
-- STAGE 3.4B — ОТКАТ
-- Дата: 2026-09-04. Возвращает evetis_ref в состояние до Stage 3.4B.
--
-- Снимки сняты ДО первой изменяющей операции:
--   evetis_ref.BAK_20260904_REF_SKU_COGS_HISTORY   (17 строк, Σ product_cogs_rub = 3294)
--   evetis_ref.BAK_20260904_REF_SKU_CHANNEL_MAP    (45 строк)
--
-- Ожидаемое состояние ПОСЛЕ отката:
--   REF_SKU_COGS_HISTORY      17 строк, Σ product_cogs_rub = 3294
--   REF_SKU_CHANNEL_MAP       45 строк, из них OZON = 20
--   V_PRODUCT_COGS_EFFECTIVE  38 строк, Σ product_cogs_rub = 11795.5, 10 колонок
--   V_BUNDLE_COGS_DERIVED     21 строка, Σ product_cogs_rub = 8501.5
--   REF_COST_BATCH / REF_COST_BATCH_SKU — отсутствуют
--
-- ЗАПУСК: выполнять целиком, по порядку. Каждый шаг идемпотентен.
-- ============================================================================

-- 1. Вернуть значения себестоимости
CREATE OR REPLACE TABLE `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_COGS_HISTORY`
AS SELECT * FROM `project-fa311fc0-4d87-4781-986.evetis_ref.BAK_20260904_REF_SKU_COGS_HISTORY`;

-- 2. Вернуть карту идентичностей
CREATE OR REPLACE TABLE `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP`
AS SELECT * FROM `project-fa311fc0-4d87-4781-986.evetis_ref.BAK_20260904_REF_SKU_CHANNEL_MAP`;

-- 3. Вернуть view к состоянию Stage 3.1A: десять колонок, без полей происхождения
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_ref.V_PRODUCT_COGS_EFFECTIVE` AS
SELECT
  internal_sku,
  effective_from,
  effective_to,
  product_cogs_rub,
  'MATERIALIZED'   AS resolver_lane,
  cogs_origin_type,
  cogs_history_id  AS resolver_ref,
  is_reconstructed,
  confidence,
  owner_confirmed
FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_COGS_HISTORY`
UNION ALL
SELECT
  internal_sku,
  effective_from,
  effective_to,
  product_cogs_rub,
  'DERIVED_BUNDLE' AS resolver_lane,
  'FF_ASSEMBLED_DERIVED' AS cogs_origin_type,
  CONCAT('BUNDLE:', internal_sku, ':', FORMAT_DATE('%Y%m%d', effective_from)) AS resolver_ref,
  TRUE  AS is_reconstructed,
  'DERIVED_FROM_COMPONENTS' AS confidence,
  FALSE AS owner_confirmed
FROM `project-fa311fc0-4d87-4781-986.evetis_ref.V_BUNDLE_COGS_DERIVED`;

-- 4. Удалить канонические таблицы Stage 3.4B
DROP TABLE IF EXISTS `project-fa311fc0-4d87-4781-986.evetis_ref.REF_COST_BATCH_SKU`;
DROP TABLE IF EXISTS `project-fa311fc0-4d87-4781-986.evetis_ref.REF_COST_BATCH`;

-- 5. Проверки отката
ASSERT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_COGS_HISTORY`) = 17
  AS 'откат: REF_SKU_COGS_HISTORY должен вернуться к 17 строкам';
ASSERT (SELECT SUM(product_cogs_rub) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_COGS_HISTORY`) = NUMERIC '3294'
  AS 'откат: сумма product_cogs_rub должна вернуться к 3294';
ASSERT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP`) = 45
  AS 'откат: REF_SKU_CHANNEL_MAP должен вернуться к 45 строкам';
ASSERT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.V_PRODUCT_COGS_EFFECTIVE`) = 38
  AS 'откат: V_PRODUCT_COGS_EFFECTIVE должен вернуться к 38 строкам';

-- Снимки BAK_20260904_* намеренно НЕ удаляются: они остаются до отдельного
-- подтверждения владельца, чтобы откат можно было выполнить повторно.

-- 6. Вернуть исторические границы состава наборов (закрытие 3.4B)
CREATE OR REPLACE TABLE `project-fa311fc0-4d87-4781-986.evetis_ref.REF_BUNDLE_COMPONENTS`
AS SELECT * FROM `project-fa311fc0-4d87-4781-986.evetis_ref.BAK_20260904_REF_BUNDLE_COMPONENTS`;

ASSERT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_BUNDLE_COMPONENTS`) = 33
  AS 'откат: REF_BUNDLE_COMPONENTS должен вернуться к 33 строкам';
ASSERT (SELECT MIN(effective_from) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_BUNDLE_COMPONENTS`
        WHERE bundle_internal_sku = 'EVT-SET-MOIST-TONIC-SERUM') = DATE '2026-07-20'
  AS 'откат: граница BOM EVT-SET-MOIST-TONIC-SERUM должна вернуться к 2026-07-20';
