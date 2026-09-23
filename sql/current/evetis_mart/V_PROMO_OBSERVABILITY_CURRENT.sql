-- ============================================================================
-- CANONICAL CURRENT DEFINITION — evetis_mart.V_PROMO_OBSERVABILITY_CURRENT (VIEW)
-- Authoritative Git definition. Rules: sql/current/README.md. Metadata: MANIFEST.json.
-- Origin: PR-PROMO-2 (Git-first, pending_deploy). Contract:
-- docs/promotions/PR_PROMO_2_CANONICAL_STATE_2026-09-22.md.
-- Нейтральный слой: читает только evetis_mart (которое читает wb_mart и ozon_mart).
--
-- Грейн: marketplace × capability. Что каждая площадка способна доказать в ПОСЛЕДНЕМ
-- годном снимке. Ответ на вопрос «можно ли вообще на это опираться» до любой экономики.
--
--   units_established    — факт установлен (прямо или детерминированно);
--   units_not_observable — источник этого не отдаёт (ограничение контракта площадки);
--   units_undetermined   — источник мог бы, но в этом снимке не доказано (неполное
--                          перечисление, неоднозначная связь, нет сопоставления SKU).
-- source_provides_capability = FALSE — у площадки нет такого источника вовсе
-- (у WB нет автодобавления и marketing_actions; зафиксировано фазой 0, CONFIRMED_DOCS).
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_OBSERVABILITY_CURRENT`
OPTIONS (description = "PR-PROMO-2. Наблюдаемость состояния акций в последнем годном снимке каждой площадки: одна строка на marketplace × capability. Для каждой способности: сколько единиц установлено, сколько не наблюдаемо по контракту источника, сколько не определено в этом снимке, и даёт ли площадка такой источник вовсе. Свежесть — возраст последнего снимка в минутах.")
AS
WITH mp AS (
  SELECT marketplace, observation_id, observed_at, observation_status
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_OBSERVATION_HISTORY`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY marketplace ORDER BY observed_at DESC, observation_id DESC) = 1
),
p AS (
  SELECT h.marketplace,
    COUNT(*) AS promotions,
    COUNTIF(h.lifecycle_status != 'UNKNOWN') AS lifecycle_known,
    COUNTIF(h.aggregate_state = 'RETURNED_BY_SOURCE') AS aggregates_returned,
    COUNTIF(h.aggregate_state = 'NOT_RETURNED_BY_SOURCE') AS aggregates_not_returned,
    COUNTIF(h.sku_membership_observability IN ('OBSERVED', 'OBSERVED_EMPTY')) AS membership_observed,
    COUNTIF(h.sku_membership_observability = 'NOT_OBSERVABLE') AS membership_not_observable
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_STATE_HISTORY` h
  JOIN mp ON mp.marketplace = h.marketplace AND mp.observation_id = h.observation_id
  GROUP BY h.marketplace
),
s AS (
  SELECT st.marketplace,
    COUNT(*) AS pairs,
    COUNTIF(st.participation_state != 'UNKNOWN') AS participation_known,
    COUNTIF(st.auto_add_state != 'UNKNOWN') AS auto_add_known,
    COUNT(DISTINCT st.marketplace_product_id) AS products,
    COUNT(DISTINCT IF(st.mapping_status LIKE 'MAPPED%', st.marketplace_product_id, NULL)) AS products_mapped
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_SKU_STATE_HISTORY` st
  JOIN mp ON mp.marketplace = st.marketplace AND mp.observation_id = st.observation_id
  GROUP BY st.marketplace
),
e AS (
  SELECT ev.marketplace,
    COUNTIF(ev.evidence_source = 'PRODUCT_MARKETING_ACTIONS') AS marketing_rows,
    COUNTIF(ev.evidence_source = 'PRODUCT_MARKETING_ACTIONS' AND ev.promotion_link_method = 'DETERMINISTIC_EXACT_MATCH') AS marketing_exact,
    COUNTIF(ev.evidence_source = 'PRODUCT_MARKETING_ACTIONS' AND ev.promotion_link_method = 'UNRESOLVED') AS marketing_unresolved,
    COUNTIF(ev.evidence_source = 'PRODUCT_MARKETING_ACTIONS' AND ev.promotion_link_method = 'AMBIGUOUS') AS marketing_ambiguous
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_SKU_EVIDENCE_HISTORY` ev
  JOIN mp ON mp.marketplace = ev.marketplace AND mp.observation_id = ev.observation_id
  GROUP BY ev.marketplace
),
caps AS (
  SELECT
    mp.marketplace, mp.observation_id, mp.observed_at, mp.observation_status, c.*
  FROM mp
  LEFT JOIN p ON p.marketplace = mp.marketplace
  LEFT JOIN s ON s.marketplace = mp.marketplace
  LEFT JOIN e ON e.marketplace = mp.marketplace
  CROSS JOIN UNNEST([
    STRUCT('PROMOTION_CALENDAR' AS capability, 'promotions' AS unit, TRUE AS source_provides_capability,
      IFNULL(p.promotions, 0) AS units_total, IFNULL(p.promotions, 0) AS units_established,
      0 AS units_not_observable, 0 AS units_undetermined, 'DIRECT_SOURCE' AS evidence_basis),
    STRUCT('PROMOTION_LIFECYCLE', 'promotions', TRUE,
      IFNULL(p.promotions, 0), IFNULL(p.lifecycle_known, 0),
      0, IFNULL(p.promotions - p.lifecycle_known, 0), 'DERIVED_DETERMINISTIC'),
    STRUCT('PROMOTION_AGGREGATE_COUNTERS', 'promotions', TRUE,
      IFNULL(p.promotions, 0), IFNULL(p.aggregates_returned, 0),
      IFNULL(p.aggregates_not_returned, 0),
      IFNULL(p.promotions - p.aggregates_returned - p.aggregates_not_returned, 0), 'DIRECT_SOURCE'),
    STRUCT('SKU_MEMBERSHIP', 'promotions', TRUE,
      IFNULL(p.promotions, 0), IFNULL(p.membership_observed, 0),
      IFNULL(p.membership_not_observable, 0),
      IFNULL(p.promotions - p.membership_observed - p.membership_not_observable, 0), 'DIRECT_SOURCE'),
    STRUCT('SKU_PARTICIPATION_STATE', 'sku_promotion_pairs', TRUE,
      IFNULL(s.pairs, 0), IFNULL(s.participation_known, 0),
      0, IFNULL(s.pairs - s.participation_known, 0), 'DIRECT_SOURCE_OR_DERIVED_DETERMINISTIC'),
    STRUCT('SKU_AUTO_ADD_SCHEDULE', 'sku_promotion_pairs', mp.marketplace = 'OZON',
      IFNULL(s.pairs, 0), IF(mp.marketplace = 'OZON', IFNULL(s.auto_add_known, 0), 0),
      IF(mp.marketplace = 'OZON', 0, IFNULL(s.pairs, 0)),
      IF(mp.marketplace = 'OZON', IFNULL(s.pairs - s.auto_add_known, 0), 0), 'DIRECT_SOURCE_OR_DERIVED_DETERMINISTIC'),
    STRUCT('MARKETING_ACTION_LINKAGE', 'marketing_action_rows', mp.marketplace = 'OZON',
      IFNULL(e.marketing_rows, 0), IFNULL(e.marketing_exact, 0),
      IFNULL(e.marketing_unresolved, 0), IFNULL(e.marketing_ambiguous, 0), 'DETERMINISTIC_EXACT_MATCH'),
    STRUCT('SKU_IDENTITY_MAPPING', 'products', TRUE,
      IFNULL(s.products, 0), IFNULL(s.products_mapped, 0),
      0, IFNULL(s.products - s.products_mapped, 0), 'REFERENCE_MAPPING')
  ]) AS c
)
SELECT
  marketplace,
  capability,
  unit,
  source_provides_capability,
  CASE
    WHEN NOT source_provides_capability THEN 'NOT_PROVIDED_BY_SOURCE'
    WHEN units_total = 0 THEN 'NO_UNITS_IN_LATEST_OBSERVATION'
    WHEN units_established = units_total THEN 'OBSERVED'
    WHEN units_established = 0 AND units_not_observable = units_total THEN 'NOT_OBSERVABLE'
    ELSE 'PARTIALLY_OBSERVED'
  END AS capability_status,
  units_total,
  units_established,
  units_not_observable,
  units_undetermined,
  evidence_basis,
  observation_id AS latest_observation_id,
  observed_at AS latest_observed_at,
  observation_status AS latest_observation_status,
  TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), observed_at, MINUTE) AS observation_age_minutes
FROM caps
