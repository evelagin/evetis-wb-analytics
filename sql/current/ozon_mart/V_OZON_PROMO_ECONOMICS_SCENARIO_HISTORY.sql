-- ============================================================================
-- CANONICAL CURRENT DEFINITION — ozon_mart.V_OZON_PROMO_ECONOMICS_SCENARIO_HISTORY (VIEW)
-- Authoritative Git definition. Rules: sql/current/README.md. Metadata: MANIFEST.json.
-- Origin: PR-PROMO-3 (Git-first, pending_deploy). Contract:
-- docs/promotions/PR_PROMO_3_ECONOMICS_INTEGRATION_2026-09-24.md.
-- Internal dependencies: V_OZON_PROMO_ECONOMICS_BASIS_HISTORY, V_OZON_PROMO_OBSERVATION_HISTORY,
-- V_OZON_PROMO_HISTORY, V_OZON_PROMO_SKU_EVIDENCE_HISTORY, V_OZON_PROMO_SKU_STATE_HISTORY.
--
-- Грейн: economics_slot × source_promotion_id × product_id × scenario_key.
-- «Что будет с вкладом, если этот товар продаётся по этой цене» — по канону FORWARD_MODELLED.
--
-- Контракт цены (источник → смысл), доказательство — docs §5:
--   /v1/actions/products.action_price             CURRENT_PARTICIPATION_ACTION_PRICE
--   /v1/actions/candidates.max_action_price       CANDIDATE_MAX_QUALIFYING_ENTRY_PRICE (высшая цена входа)
--   /auto-add/products/list.action_price_to_auto_add        SCHEDULED_AUTO_ADD_PRICE
--   /auto-add/products/candidates.action_price_to_auto_add  AUTO_ADD_ELIGIBLE_PRICE
--   action_price кандидата (= 0, «не задана»), marketing_actions[].value — не цены сценария.
-- Все четыре — шкала цены продавца (база комиссии): участие по 1258 при цене 1260 дало
-- marketing_seller_price = 1258 (42/42 строк); цена автодобавления 3279 = action_price
-- участника. Применяется ли цена к продажам — отдельная ось price_binding_evidence, а не
-- условие расчёта: экономика условна («если продано по P»).
--
-- BASELINE — канонический seller_base_price снимка. Если он уже ниже прейскурантной цены
-- (действующая акция применена к цене по умолчанию), baseline_includes_promotion_effect = TRUE.
-- Экономика — только из снимка базиса того же слота; нет снимка → TEMPORAL_ALIGNMENT_UNAVAILABLE.
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_PROMO_ECONOMICS_SCENARIO_HISTORY`
OPTIONS (description = "PR-PROMO-3. Сценарии экономики Ozon по слотам: BASELINE — канон FORWARD_MODELLED при текущей цене продавца; PROMO — по ценам источника с явным смыслом: цена участия, максимальная цена входа кандидата, цена запланированного и возможного автодобавления. Применение цены к продажам — отдельная ось price_binding_evidence. EXPECTED и WORST (максимальная логистика) Ozon, требуемый рост продаж, break-even. Экономика только из снимка базиса того же слота. Без рекомендаций.")
AS
WITH b AS (
  SELECT *
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_PROMO_ECONOMICS_BASIS_HISTORY`
),
slots AS (
  SELECT DISTINCT economics_slot
  FROM b
),
obs AS (
  SELECT observation_id, observation_slot, observed_at
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_PROMO_OBSERVATION_HISTORY`
),
h AS (
  SELECT observation_id, action_id, promotion_type, lifecycle_status, starts_at
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_PROMO_HISTORY`
),
st AS (
  SELECT observation_id, action_id, product_id, resolved_state
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_PROMO_SKU_STATE_HISTORY`
),
mk AS (
  SELECT observation_id, product_id, MAX(marketing_seller_price_rub) AS observed_seller_price_rub,
    MAX(price_rub) AS observed_list_price_rub
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PROMO_PRODUCT_MARKETING`
  WHERE environment = 'prod' AND product_id IS NOT NULL
  GROUP BY observation_id, product_id
),
cur AS (
  SELECT observation_id, action_id, auto_add_at, list_kind, product_id, MAX(currency_code) AS currency_code
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PROMO_AUTO_ADD`
  WHERE environment = 'prod'
  GROUP BY observation_id, action_id, auto_add_at, list_kind, product_id
),
ref AS (
  SELECT internal_sku, MAX(marketplace_product_id) AS marketplace_product_id, COUNT(*) AS n
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP`
  WHERE marketplace = 'OZON' AND is_current
  GROUP BY internal_sku
),
ev AS (
  SELECT e.observation_id, e.observed_at, e.evidence_source, e.action_id, e.product_id, e.offer_id,
    e.internal_sku, e.mapping_status, e.auto_add_at, e.action_price_rub, e.max_action_price_rub,
    e.action_price_to_auto_add_rub,
    CASE e.evidence_source
      WHEN 'ACTION_PRODUCTS' THEN 'CURRENT_PARTICIPATION_ACTION_PRICE'
      WHEN 'ACTION_CANDIDATES' THEN 'CANDIDATE_MAX_QUALIFYING_ENTRY_PRICE'
      WHEN 'AUTO_ADD_LIST' THEN 'SCHEDULED_AUTO_ADD_PRICE'
      WHEN 'AUTO_ADD_CANDIDATES' THEN 'AUTO_ADD_ELIGIBLE_PRICE'
    END AS price_interpretation,
    CASE e.evidence_source
      WHEN 'ACTION_PRODUCTS' THEN e.action_price_rub
      WHEN 'ACTION_CANDIDATES' THEN e.max_action_price_rub
      ELSE e.action_price_to_auto_add_rub
    END AS source_price_rub,
    CASE e.evidence_source
      WHEN 'ACTION_PRODUCTS' THEN 'OZON POST /v1/actions/products.action_price'
      WHEN 'ACTION_CANDIDATES' THEN 'OZON POST /v1/actions/candidates.max_action_price'
      WHEN 'AUTO_ADD_LIST' THEN 'OZON POST /v1/actions/auto-add/products/list.action_price_to_auto_add'
      ELSE 'OZON POST /v1/actions/auto-add/products/candidates.action_price_to_auto_add'
    END AS price_source,
    CASE WHEN e.evidence_source IN ('AUTO_ADD_LIST', 'AUTO_ADD_CANDIDATES') THEN cu.currency_code ELSE 'RUB' END AS currency,
    CASE WHEN e.evidence_source IN ('AUTO_ADD_LIST', 'AUTO_ADD_CANDIDATES') THEN 'SOURCE_CURRENCY_CODE'
         ELSE 'SOURCE_HAS_NO_CURRENCY_SELLER_PRICE_LIST_RUB' END AS currency_basis
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_PROMO_SKU_EVIDENCE_HISTORY` e
  LEFT JOIN cur cu
    ON cu.observation_id = e.observation_id AND cu.action_id = e.action_id AND cu.auto_add_at = e.auto_add_at
   AND cu.product_id = e.product_id
   AND cu.list_kind = IF(e.evidence_source = 'AUTO_ADD_LIST', 'SCHEDULED', 'ELIGIBLE')
  WHERE e.evidence_source IN ('ACTION_PRODUCTS', 'ACTION_CANDIDATES', 'AUTO_ADD_LIST', 'AUTO_ADD_CANDIDATES')
),
rows_ AS (
  SELECT
    b.economics_slot, o.observation_id, o.observed_at,
    CAST(NULL AS INT64) AS action_id, CAST(NULL AS STRING) AS promotion_type,
    CAST(NULL AS STRING) AS promotion_lifecycle_status, CAST(NULL AS TIMESTAMP) AS promotion_starts_at,
    SAFE_CAST(IF(rf.n = 1, rf.marketplace_product_id, NULL) AS INT64) AS product_id, b.offer_id, b.internal_sku,
    'CANONICAL_FE_SKU' AS mapping_status,
    CAST(NULL AS STRING) AS sku_state, CAST(NULL AS STRING) AS evidence_source,
    'BASELINE' AS scenario_type, 'BASELINE' AS scenario_key, 'CURRENT_SELLER_PRICE' AS price_interpretation,
    CAST(NULL AS TIMESTAMP) AS auto_add_at,
    'RUB' AS currency, 'RUB_BY_CANONICAL_CONTRACT' AS currency_basis,
    b.baseline_price_rub AS scenario_price_rub, b.baseline_price_source AS price_source,
    b.baseline_price_observed_at AS price_observed_at,
    'CANONICAL_SELLER_PRICE' AS price_evidence_status,
    'NOT_APPLICABLE_BASELINE' AS price_binding_evidence,
    'NO_PROMOTION_LINK' AS promotion_link_evidence,
    'ASSUMPTIONS_AT_OBSERVATION' AS temporal_basis,
    CAST(NULL AS NUMERIC) AS observed_seller_price_rub, CAST(NULL AS NUMERIC) AS observed_list_price_rub,
    b.economics_slot AS join_slot, b.internal_sku AS join_sku
  FROM b
  LEFT JOIN obs o ON o.observation_slot = b.economics_slot
  LEFT JOIN ref rf ON rf.internal_sku = b.internal_sku
  UNION ALL
  SELECT
    o.observation_slot, e.observation_id, e.observed_at,
    e.action_id, h.promotion_type, h.lifecycle_status, h.starts_at,
    e.product_id, e.offer_id, e.internal_sku, e.mapping_status,
    s.resolved_state, e.evidence_source,
    'PROMO', CONCAT(e.price_interpretation, '|', IFNULL(FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%SZ', e.auto_add_at), '-')),
    e.price_interpretation, e.auto_add_at,
    e.currency, e.currency_basis,
    e.source_price_rub, e.price_source, e.observed_at,
    IF(e.evidence_source = 'ACTION_CANDIDATES', 'PROVEN_SELLER_PRICE_SCALE_CEILING', 'PROVEN_SELLER_PRICE_SCALE'),
    CASE
      WHEN e.evidence_source = 'ACTION_CANDIDATES' THEN 'HYPOTHETICAL_ENTRY'
      WHEN e.evidence_source = 'AUTO_ADD_LIST' THEN 'SCHEDULED_FUTURE_ENTRY'
      WHEN e.evidence_source = 'AUTO_ADD_CANDIDATES' THEN 'ELIGIBLE_FUTURE_ENTRY'
      WHEN m.observed_seller_price_rub IS NULL THEN 'OBSERVED_SELLER_PRICE_MISSING'
      WHEN e.source_price_rub = m.observed_seller_price_rub AND e.source_price_rub < m.observed_list_price_rub
        THEN 'APPLIED_TO_DEFAULT_SELLER_PRICE'
      WHEN e.source_price_rub = m.observed_seller_price_rub THEN 'EQUAL_TO_CURRENT_SELLER_PRICE'
      WHEN e.source_price_rub > m.observed_seller_price_rub THEN 'ABOVE_CURRENT_SELLER_PRICE_NOT_APPLIED'
      ELSE 'BELOW_CURRENT_SELLER_PRICE_NOT_REFLECTED_IN_DEFAULT_PRICE'
    END,
    'DIRECT_ID',
    CASE
      WHEN e.auto_add_at > e.observed_at OR h.lifecycle_status = 'UPCOMING' THEN 'CURRENT_ASSUMPTIONS_FOR_FUTURE_PROMO'
      WHEN h.lifecycle_status = 'ENDED' THEN 'OBSERVATION_ASSUMPTIONS_PROMO_ENDED'
      ELSE 'ASSUMPTIONS_AT_OBSERVATION'
    END,
    m.observed_seller_price_rub, m.observed_list_price_rub,
    o.observation_slot, e.internal_sku
  FROM ev e
  JOIN obs o ON o.observation_id = e.observation_id
  LEFT JOIN h ON h.observation_id = e.observation_id AND h.action_id = e.action_id
  LEFT JOIN st s ON s.observation_id = e.observation_id AND s.action_id = e.action_id AND s.product_id = e.product_id
  LEFT JOIN mk m ON m.observation_id = e.observation_id AND m.product_id = e.product_id
),
x AS (
  SELECT r.*,
    bb.basis_snapshot_id, bb.basis_captured_at, bb.baseline_price_rub, bb.baseline_price_source,
    bb.baseline_price_observed_at, bb.cogs_rub, bb.cogs_source, bb.cogs_effective_from,
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
  'OZON' AS marketplace,
  economics_slot,
  observation_id,
  observed_at,
  basis_snapshot_id,
  basis_captured_at,
  'OBSERVED' AS history_class,
  action_id,
  promotion_type,
  promotion_lifecycle_status,
  promotion_starts_at,
  product_id,
  offer_id,
  internal_sku,
  mapping_status,
  sku_state,
  evidence_source,
  scenario_type,
  scenario_key,
  price_interpretation,
  auto_add_at,
  currency,
  currency_basis,
  scenario_price_rub,
  price_source,
  price_observed_at,
  price_evidence_status,
  price_binding_evidence,
  promotion_link_evidence,
  observed_seller_price_rub,
  observed_list_price_rub,
  baseline_price_rub,
  baseline_price_source,
  baseline_price_observed_at,
  baseline_price_rub < observed_list_price_rub AS baseline_includes_promotion_effect,
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
  'DIRECT_CANONICAL_CASE' AS downside_evidence_class,
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
