-- ============================================================================
-- PR-PROMO-3 · PROMOTION ECONOMICS · wb_mart.V_WB_PROMO_ECONOMICS_SCENARIO_HISTORY (VIEW)
-- Грейн: economics_slot × source_promotion_id × nm_id × scenario_key.
--
-- BASELINE — текущая экономика продавца по канону WB_FE_V1 на слоте, для каждого SKU.
--   Это НЕ экономика акции (связи с акцией нет, source_promotion_id = NULL). Она доказывает,
--   что примитив сценария совпадает с каноном при scenario_price = baseline_price.
-- PROMO — только из свидетельств /calendar/promotions/nomenclatures (обычные акции WB).
--   Для автоакций строк нет вовсе: состав не наблюдаем (PR-PROMO-2), агрегаты акции на SKU не
--   опускаются. Лестница бустинга цены не несёт. Смысл planPrice (цена продавца до СПП или
--   иная) живыми данными не доказан — таких акций у EVETIS не было, — поэтому такие строки
--   получают PRICE_SEMANTICS_UNPROVEN и экономику не считают.
-- Экономика считается только из снимка базиса того же слота; нет снимка — строка остаётся с
-- TEMPORAL_ALIGNMENT_UNAVAILABLE, текущие значения не подставляются.
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_PROMO_ECONOMICS_SCENARIO_HISTORY`
OPTIONS (description = "PR-PROMO-3. Сценарии экономики WB по слотам: BASELINE — канон WB_FE_V1 при текущей цене продавца для каждого SKU (не экономика акции); PROMO — только из свидетельств состава обычных акций, их цена planPrice семантически не доказана → PRICE_SEMANTICS_UNPROVEN. Для автоакций строк нет: состав не наблюдаем. Вклад считается только из снимка базиса того же слота, без подстановки текущих значений. Без рекомендаций.")
AS
WITH b AS (
  SELECT *
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_PROMO_ECONOMICS_BASIS_HISTORY`
),
slots AS (
  SELECT DISTINCT economics_slot
  FROM b
),
obs AS (
  SELECT observation_id, observation_slot, observed_at
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_PROMO_OBSERVATION_HISTORY`
),
h AS (
  SELECT observation_id, promotion_id, promotion_type, lifecycle_status, starts_at
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_PROMO_HISTORY`
),
promo AS (
  SELECT e.observation_id, e.observation_slot, e.observed_at, e.promotion_id, e.nm_id, e.internal_sku,
    e.mapping_status, e.source_state, e.evidence_source, e.currency_code, e.plan_price,
    s.resolved_state
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_PROMO_SKU_EVIDENCE_HISTORY` e
  LEFT JOIN `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_PROMO_SKU_STATE_HISTORY` s
    ON s.observation_id = e.observation_id AND s.promotion_id = e.promotion_id AND s.nm_id = e.nm_id
),
rows_ AS (
  SELECT
    b.economics_slot, o.observation_id, o.observed_at,
    CAST(NULL AS INT64) AS promotion_id, CAST(NULL AS STRING) AS promotion_type,
    CAST(NULL AS STRING) AS promotion_lifecycle_status, CAST(NULL AS TIMESTAMP) AS promotion_starts_at,
    b.nm_id, b.internal_sku, 'CANONICAL_FE_SKU' AS mapping_status,
    CAST(NULL AS STRING) AS sku_state, CAST(NULL AS STRING) AS evidence_source,
    'BASELINE' AS scenario_type, 'BASELINE' AS scenario_key, 'CURRENT_SELLER_PRICE' AS price_interpretation,
    'RUB' AS currency, 'RUB_BY_CANONICAL_CONTRACT' AS currency_basis,
    b.baseline_price_rub AS scenario_price_rub,
    b.baseline_price_source AS price_source, b.baseline_price_observed_at AS price_observed_at,
    'CANONICAL_SELLER_PRICE' AS price_evidence_status,
    'NOT_APPLICABLE_BASELINE' AS price_binding_evidence,
    'NO_PROMOTION_LINK' AS promotion_link_evidence,
    'ASSUMPTIONS_AT_OBSERVATION' AS temporal_basis,
    b.economics_slot AS join_slot, b.internal_sku AS join_sku
  FROM b
  LEFT JOIN obs o ON o.observation_slot = b.economics_slot
  UNION ALL
  SELECT
    p.observation_slot, p.observation_id, p.observed_at,
    p.promotion_id, h.promotion_type, h.lifecycle_status, h.starts_at,
    p.nm_id, p.internal_sku, p.mapping_status,
    p.resolved_state, p.evidence_source,
    'PROMO', CONCAT('WB_NOMENCLATURE_PLAN_PRICE|', p.source_state), 'WB_NOMENCLATURE_PLAN_PRICE',
    p.currency_code, 'SOURCE_CURRENCY_CODE',
    p.plan_price,
    'WB /calendar/promotions/nomenclatures.planPrice', p.observed_at,
    'PRICE_SEMANTICS_UNPROVEN',
    'NOT_ESTABLISHED',
    'DIRECT_ID',
    CASE WHEN h.lifecycle_status = 'UPCOMING' THEN 'CURRENT_ASSUMPTIONS_FOR_FUTURE_PROMO'
         WHEN h.lifecycle_status = 'ENDED' THEN 'OBSERVATION_ASSUMPTIONS_PROMO_ENDED'
         ELSE 'ASSUMPTIONS_AT_OBSERVATION' END,
    p.observation_slot, p.internal_sku
  FROM promo p
  LEFT JOIN h ON h.observation_id = p.observation_id AND h.promotion_id = p.promotion_id
),
x AS (
  SELECT r.*,
    bb.basis_snapshot_id, bb.basis_captured_at, bb.basis_status, bb.baseline_price_rub,
    bb.baseline_price_source, bb.baseline_price_observed_at, bb.cogs_rub, bb.cogs_source, bb.cogs_effective_from,
    bb.price_retention_expected, bb.fixed_cost_expected_rub, bb.price_retention_downside, bb.fixed_cost_downside_rub,
    bb.downside_case, bb.canonical_contribution_expected_rub, bb.canonical_break_even_expected_rub,
    bb.economics_model_version,
    CASE
      WHEN sl.economics_slot IS NULL THEN 'TEMPORAL_ALIGNMENT_UNAVAILABLE'
      WHEN r.internal_sku IS NULL THEN 'MISSING_CANONICAL_SKU'
      WHEN bb.internal_sku IS NULL THEN 'MISSING_ECONOMICS_COMPONENT'
      WHEN bb.basis_status != 'COMPUTABLE' THEN bb.basis_status
      WHEN r.currency IS NULL OR r.currency != 'RUB' THEN 'UNSUPPORTED_CURRENCY'
      WHEN r.scenario_price_rub IS NULL OR r.scenario_price_rub <= 0 THEN 'MISSING_SOURCE_PRICE'
      WHEN r.price_evidence_status = 'PRICE_SEMANTICS_UNPROVEN' THEN 'PRICE_SEMANTICS_UNPROVEN'
      ELSE 'COMPUTABLE'
    END AS economics_status
  FROM rows_ r
  LEFT JOIN slots sl ON sl.economics_slot = r.join_slot
  LEFT JOIN b bb ON bb.economics_slot = r.join_slot AND bb.internal_sku = r.join_sku
),
c AS (
  SELECT x.*,
    IF(economics_status = 'COMPUTABLE', baseline_price_rub * price_retention_expected - fixed_cost_expected_rub, NULL) AS baseline_contribution_expected_rub,
    IF(economics_status = 'COMPUTABLE', scenario_price_rub * price_retention_expected - fixed_cost_expected_rub, NULL) AS promo_contribution_expected_rub,
    IF(economics_status = 'COMPUTABLE', baseline_price_rub * price_retention_downside - fixed_cost_downside_rub, NULL) AS baseline_contribution_downside_rub,
    IF(economics_status = 'COMPUTABLE', scenario_price_rub * price_retention_downside - fixed_cost_downside_rub, NULL) AS promo_contribution_downside_rub
  FROM x
)
SELECT
  'WB' AS marketplace,
  economics_slot,
  observation_id,
  observed_at,
  basis_snapshot_id,
  basis_captured_at,
  'OBSERVED' AS history_class,
  promotion_id,
  promotion_type,
  promotion_lifecycle_status,
  promotion_starts_at,
  nm_id,
  internal_sku,
  mapping_status,
  sku_state,
  evidence_source,
  scenario_type,
  scenario_key,
  price_interpretation,
  CAST(NULL AS TIMESTAMP) AS auto_add_at,
  currency,
  currency_basis,
  scenario_price_rub,
  price_source,
  price_observed_at,
  price_evidence_status,
  price_binding_evidence,
  promotion_link_evidence,
  baseline_price_rub,
  baseline_price_source,
  baseline_price_observed_at,
  cogs_rub,
  cogs_source,
  cogs_effective_from,
  economics_model_version,
  temporal_basis,
  economics_status,
  baseline_contribution_expected_rub,
  promo_contribution_expected_rub,
  promo_contribution_expected_rub - baseline_contribution_expected_rub AS delta_contribution_expected_rub,
  SAFE_DIVIDE(baseline_contribution_expected_rub, baseline_price_rub) * 100 AS baseline_margin_expected_pct,
  SAFE_DIVIDE(promo_contribution_expected_rub, scenario_price_rub) * 100 AS promo_margin_expected_pct,
  IF(economics_status = 'COMPUTABLE', canonical_break_even_expected_rub, NULL) AS break_even_price_expected_rub,
  IF(economics_status = 'COMPUTABLE', scenario_price_rub - canonical_break_even_expected_rub, NULL) AS promo_price_minus_break_even_rub,
  IF(economics_status = 'COMPUTABLE', canonical_contribution_expected_rub, NULL) AS canonical_contribution_at_baseline_rub,
  downside_case,
  'DERIVED_DETERMINISTIC' AS downside_evidence_class,
  baseline_contribution_downside_rub,
  promo_contribution_downside_rub,
  promo_contribution_downside_rub < 0 AS promo_downside_negative,
  CASE
    WHEN economics_status != 'COMPUTABLE' THEN 'NOT_COMPUTED_ECONOMICS_UNAVAILABLE'
    WHEN baseline_contribution_expected_rub <= 0 THEN 'NOT_MEANINGFUL_BASELINE_NON_POSITIVE'
    WHEN promo_contribution_expected_rub <= 0 THEN 'NOT_ACHIEVABLE_PROMO_NON_POSITIVE'
    ELSE 'COMPUTED'
  END AS uplift_status,
  IF(economics_status = 'COMPUTABLE' AND baseline_contribution_expected_rub > 0 AND promo_contribution_expected_rub > 0,
     (baseline_contribution_expected_rub / promo_contribution_expected_rub - 1) * 100, NULL) AS required_sales_uplift_pct,
  CASE
    WHEN economics_status != 'COMPUTABLE' OR baseline_contribution_expected_rub <= 0 OR promo_contribution_expected_rub <= 0 THEN NULL
    WHEN promo_contribution_expected_rub < baseline_contribution_expected_rub THEN 'MORE_UNITS_REQUIRED'
    WHEN promo_contribution_expected_rub = baseline_contribution_expected_rub THEN 'NO_CHANGE'
    ELSE 'PROMO_UNIT_CONTRIBUTION_HIGHER'
  END AS uplift_interpretation
FROM c
