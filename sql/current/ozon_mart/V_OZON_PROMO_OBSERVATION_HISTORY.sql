-- ============================================================================
-- CANONICAL CURRENT DEFINITION — ozon_mart.V_OZON_PROMO_OBSERVATION_HISTORY (VIEW)
-- Authoritative Git definition. Rules: sql/current/README.md. Metadata: MANIFEST.json.
-- Origin: PR-PROMO-2 (Git-first, pending_deploy). Contract:
-- docs/promotions/PR_PROMO_2_CANONICAL_STATE_2026-09-22.md.
-- Internal dependencies: none.
--
-- Грейн: observation_id. Одна строка на ГОДНЫЙ снимок акций Ozon: prod, id по контракту
-- слота (OZPROMO_prod_YYYYMMDDHHMM), последняя строка манифеста COMPLETE или REUSED.
-- observed_at — время данных из строк RAW (для REUSED манифест хранит время повтора:
-- T1 14:04:40 против 16:58:20). Снимок вне контракта слота в историю не входит.
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_PROMO_OBSERVATION_HISTORY`
OPTIONS (description = "PR-PROMO-2. Годные снимки акций Ozon: одна строка на observation_id. Годный = prod, id по контракту слота, статус манифеста COMPLETE или REUSED. observed_at — время данных из строк RAW, а не время последнего прогона слота; без строк — время манифеста. Основа канонической истории и текущего состояния.")
AS
WITH manifest AS (
  SELECT observation_id, observation_bucket, status, observed_at AS manifest_observed_at
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.OZON_PROMO_OBSERVATIONS`
  WHERE environment = 'prod'
    AND REGEXP_CONTAINS(observation_id, r'^OZPROMO_prod_[0-9]{12}$')
  QUALIFY ROW_NUMBER() OVER (PARTITION BY observation_id ORDER BY completed_at DESC, started_at DESC) = 1
),
data_time AS (
  SELECT observation_id, MIN(observed_at) AS data_observed_at
  FROM (
    SELECT observation_id, observed_at
    FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PROMO_ACTIONS`
    WHERE environment = 'prod'
    UNION ALL
    SELECT observation_id, observed_at
    FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PROMO_PRODUCT_MARKETING`
    WHERE environment = 'prod'
  )
  GROUP BY observation_id
),
actions AS (
  SELECT observation_id, COUNT(DISTINCT action_id) AS promotions_observed
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PROMO_ACTIONS`
  WHERE environment = 'prod'
  GROUP BY observation_id
),
catalog AS (
  SELECT observation_id, COUNT(DISTINCT product_id) AS catalog_products_observed
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PROMO_PRODUCT_MARKETING`
  WHERE environment = 'prod'
  GROUP BY observation_id
)
SELECT
  'OZON' AS marketplace,
  m.observation_id,
  m.observation_bucket AS observation_slot,
  COALESCE(d.data_observed_at, m.manifest_observed_at) AS observed_at,
  IF(d.data_observed_at IS NOT NULL, 'RAW_ROWS', 'MANIFEST') AS observed_at_basis,
  m.manifest_observed_at,
  'OBSERVED' AS history_class,
  m.status AS observation_status,
  IFNULL(a.promotions_observed, 0) AS promotions_observed,
  IFNULL(c.catalog_products_observed, 0) AS catalog_products_observed
FROM manifest m
LEFT JOIN data_time d ON d.observation_id = m.observation_id
LEFT JOIN actions a ON a.observation_id = m.observation_id
LEFT JOIN catalog c ON c.observation_id = m.observation_id
WHERE m.status IN ('COMPLETE', 'REUSED')
