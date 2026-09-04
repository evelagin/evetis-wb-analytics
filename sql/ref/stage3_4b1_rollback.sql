-- ============================================================================
-- STAGE 3.4B.1 — ОТКАТ
-- Дата: 2026-09-04. Возвращает состояние к концу Stage 3.4B.
-- Откат Stage 3.4B целиком — отдельный скрипт stage3_4b_rollback.sql.
--
-- Ожидаемое состояние ПОСЛЕ отката:
--   REF_SKU_COGS_HISTORY  17 строк, Σ product_cogs_rub = 3197.45
--   REF_COST_ADDITIONAL_LANDED — отсутствует
--   V_PRODUCT_COGS_EFFECTIVE — 15 колонок (без полей 3.4B.1)
-- ============================================================================

CREATE OR REPLACE TABLE `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_COGS_HISTORY`
AS SELECT * FROM `project-fa311fc0-4d87-4781-986.evetis_ref.BAK_20260904B_REF_SKU_COGS_HISTORY`;

DROP TABLE IF EXISTS `project-fa311fc0-4d87-4781-986.evetis_ref.REF_COST_ADDITIONAL_LANDED`;

-- View возвращается к варианту Stage 3.4B: 15 колонок, без legal/additional/cost_basis.
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_ref.V_PRODUCT_COGS_EFFECTIVE` AS
SELECT h.internal_sku, h.effective_from, h.effective_to, h.product_cogs_rub,
  'MATERIALIZED' AS resolver_lane, h.cogs_origin_type, h.cogs_history_id AS resolver_ref,
  h.is_reconstructed, h.confidence, h.owner_confirmed,
  b.batch_id AS cost_batch_id, b.batch_number AS cost_batch_number,
  'EFFECTIVE_DATE' AS cost_method, 'NOT_PROVEN' AS batch_traceability,
  IF(b.batch_id IS NULL, 'DERIVED_OR_TRANSFORMED', 'PROVEN_DOCUMENT') AS cogs_provenance_status
FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_COGS_HISTORY` h
LEFT JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_COST_BATCH_SKU` bs
       ON bs.internal_sku = h.internal_sku AND bs.effective_from = h.effective_from
LEFT JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_COST_BATCH` b ON b.batch_id = bs.batch_id
UNION ALL
SELECT internal_sku, effective_from, effective_to, product_cogs_rub,
  'DERIVED_BUNDLE', 'FF_ASSEMBLED_DERIVED',
  CONCAT('BUNDLE:', internal_sku, ':', FORMAT_DATE('%Y%m%d', effective_from)),
  TRUE, 'DERIVED_FROM_COMPONENTS', FALSE,
  CAST(NULL AS STRING), CAST(NULL AS STRING), 'EFFECTIVE_DATE', 'NOT_PROVEN',
  'DERIVED_FROM_COMPONENTS'
FROM `project-fa311fc0-4d87-4781-986.evetis_ref.V_BUNDLE_COGS_DERIVED`;

ASSERT (SELECT SUM(product_cogs_rub) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_COGS_HISTORY`)
       = NUMERIC '3197.45'
  AS 'откат 3.4B.1: сумма product_cogs_rub должна вернуться к 3197.45';
