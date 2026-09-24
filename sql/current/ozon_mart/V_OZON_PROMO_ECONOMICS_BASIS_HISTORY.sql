-- ============================================================================
-- CANONICAL CURRENT DEFINITION — ozon_mart.V_OZON_PROMO_ECONOMICS_BASIS_HISTORY (VIEW)
-- Authoritative Git definition. Rules: sql/current/README.md. Metadata: MANIFEST.json.
-- Origin: PR-PROMO-3 (Git-first, pending_deploy). Contract:
-- docs/promotions/PR_PROMO_3_ECONOMICS_INTEGRATION_2026-09-24.md.
-- Internal dependencies: none.
--
-- Грейн: economics_slot × internal_sku. Канонический базис Ozon FORWARD_MODELLED, снятый на
-- слоте (ozon_raw.OZON_PROMO_ECONOMICS_BASIS_SNAPSHOT — дословная копия
-- V_OZON_SKU_FORWARD_ECONOMICS_CURRENT). Второй модели нет: только линейная раскладка, которой
-- канон пользуется сам в break_even_price_expected / break_even_price_worst:
--   contribution(P) = P × (1 − c_frac − a_frac) − (COGS + last_mile + logistics_case)
--   c_frac = commission_pct / 100
--   a_frac = ROUND(acquiring_rub / seller_base_price × 100, 4) / 100  — ровно как acquiring_pct
--            в V_OZON_SKU_CURRENT_TARIFF (эквайринг пропорционален цене: 1,0000 % у 20 из 20)
--   EXPECTED: logistics_expected (MODELLED), WORST: logistics_max (тариф) — WORST Ozon.
-- Возвраты в вклад не входят (канон: CANCEL_ONLY_RETURNS_EXCLUDED), хранение исключено,
-- Premium не входит, налог не входит.
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_PROMO_ECONOMICS_BASIS_HISTORY`
OPTIONS (description = "PR-PROMO-3. Канонический базис Ozon FORWARD_MODELLED по слотам наблюдения акций: одна строка на economics_slot × internal_sku. Компоненты — дословные копии V_OZON_SKU_FORWARD_ECONOMICS_CURRENT на момент снимка; здесь только линейная раскладка вклад(P) = P × (1 − комиссия − эквайринг) − постоянные издержки, та же, что в каноническом break-even. EXPECTED — ожидаемая логистика, WORST — максимальная. До налога, без хранения, возвратов и рекламы.")
AS
WITH s AS (
  SELECT *
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.OZON_PROMO_ECONOMICS_BASIS_SNAPSHOT`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY snapshot_slot, internal_sku ORDER BY captured_at, snapshot_id) = 1
),
k AS (
  SELECT s.*,
    commission_pct / 100 AS c_frac,
    ROUND(SAFE_DIVIDE(acquiring_rub, seller_base_price) * 100, 4) / 100 AS a_frac
  FROM s
)
SELECT
  'OZON' AS marketplace,
  snapshot_slot AS economics_slot,
  snapshot_id AS basis_snapshot_id,
  captured_at AS basis_captured_at,
  trigger AS basis_trigger,
  internal_sku,
  offer_id,
  ozon_sku,
  product_name,
  product_type = 'BUNDLE' AS is_bundle,
  'RUB' AS currency,
  seller_base_price AS baseline_price_rub,
  'ozon_raw.RAW_OZON_PRICES.marketing_seller_price (FORWARD_MODELLED seller_base_price)' AS baseline_price_source,
  tariff_snapshot_at AS baseline_price_observed_at,
  tariff_effective_at,
  current_management_cogs_rub AS cogs_rub,
  cogs_basis AS cogs_source,
  cogs_effective_from,
  commission_pct,
  acquiring_rub,
  last_mile_rub,
  logistics_expected_rub,
  logistics_max_rub,
  logistics_expected_basis,
  1 - c_frac - a_frac AS price_retention_expected,
  current_management_cogs_rub + last_mile_rub + logistics_expected_rub AS fixed_cost_expected_rub,
  1 - c_frac - a_frac AS price_retention_downside,
  current_management_cogs_rub + last_mile_rub + logistics_max_rub AS fixed_cost_downside_rub,
  'OZON_WORST_MAX_LOGISTICS' AS downside_case,
  contribution_expected AS canonical_contribution_expected_rub,
  contribution_worst_case AS canonical_contribution_downside_rub,
  break_even_price_expected AS canonical_break_even_expected_rub,
  break_even_price_worst AS canonical_break_even_downside_rub,
  forward_economics_ready,
  tariff_freshness_status,
  expected_mode_status,
  economics_mode AS economics_model_version,
  CASE
    WHEN internal_sku IS NULL THEN 'MISSING_CANONICAL_SKU'
    WHEN current_management_cogs_rub IS NULL THEN 'MISSING_COGS'
    WHEN seller_base_price IS NULL OR seller_base_price <= 0 OR commission_pct IS NULL OR acquiring_rub IS NULL
      OR last_mile_rub IS NULL OR logistics_expected_rub IS NULL OR logistics_max_rub IS NULL
      OR c_frac + a_frac >= 1 THEN 'MISSING_ECONOMICS_COMPONENT'
    WHEN forward_economics_ready IS NOT TRUE THEN 'CANONICAL_ECONOMICS_NOT_READY'
    ELSE 'COMPUTABLE'
  END AS basis_status
FROM k
