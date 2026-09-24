-- ============================================================================
-- PR-PROMO-3 · PROMOTION ECONOMICS · wb_mart.V_WB_PROMO_ECONOMICS_BASIS_HISTORY (VIEW)
-- Грейн: economics_slot × internal_sku. Канонический базис WB_FE_V1, снятый на слоте.
--
-- Это НЕ вторая модель экономики. Каждое число — дословная копия колонки
-- wb_mart.V_WB_SKU_FORWARD_ECONOMICS_CURRENT на момент снимка (wb_raw.WB_PROMO_ECONOMICS_
-- BASIS_SNAPSHOT). Здесь только раскладка канона в линейную форму, которой канон пользуется
-- сам (sql/pricing/pr2_wb_forward_economics.sql, TVF_WB_FORWARD_ECONOMICS):
--   contribution(P) = P × (1 − take) − (logistics + COGS),  take = (комиссия% + эквайринг%) / 100
--   EXPECTED:  эквайринг p50, логистика = нагрузка на проданную единицу за 90 сут (с невыкупом)
--   STRESS:    эквайринг p90, логистика p90 — те же слагаемые, что у канонического
--              break_even_before_ads_pre_tax_stress (его корень). Это стресс WB, а не WORST Ozon.
-- Арифметика FLOAT64 — как у канона (acquiring_p*_pct в каноне FLOAT64).
-- Хранение, налог, реклама не входят (канон PRE_TAX, хранение исключено). Юнитка не читается.
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_PROMO_ECONOMICS_BASIS_HISTORY`
OPTIONS (description = "PR-PROMO-3. Канонический базис WB_FE_V1 по слотам наблюдения акций: одна строка на economics_slot × internal_sku. Все компоненты — дословные копии V_WB_SKU_FORWARD_ECONOMICS_CURRENT на момент снимка; здесь только линейная раскладка вклад(P) = P × удерживаемая доля − постоянные издержки. EXPECTED = эквайринг p50 и логистика 90 сут; STRESS = p90 (стресс WB, не WORST Ozon). До налога, без хранения и рекламы.")
AS
WITH s AS (
  SELECT *
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.WB_PROMO_ECONOMICS_BASIS_SNAPSHOT`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY snapshot_slot, internal_sku ORDER BY captured_at, snapshot_id) = 1
)
SELECT
  'WB' AS marketplace,
  snapshot_slot AS economics_slot,
  snapshot_id AS basis_snapshot_id,
  captured_at AS basis_captured_at,
  trigger AS basis_trigger,
  internal_sku,
  nm_id,
  product_name_short AS product_name,
  is_bundle,
  'RUB' AS currency,
  seller_effective_price_rub AS baseline_price_rub,
  'wb_raw.V_WB_PRICES_CURRENT.seller_effective_price (WB_FE_V1 seller_effective_price_rub)' AS baseline_price_source,
  price_observed_at AS baseline_price_observed_at,
  price_freshness AS baseline_price_freshness,
  cogs_rub,
  cogs_source,
  cogs_effective_from,
  effective_commission_pct,
  commission_source,
  acquiring_p50_pct,
  acquiring_p90_pct,
  logistics_expected_rub,
  logistics_p90_rub,
  logistics_sample_size,
  1 - (effective_commission_pct + acquiring_p50_pct) / 100 AS price_retention_expected,
  logistics_expected_rub + cogs_rub AS fixed_cost_expected_rub,
  1 - (effective_commission_pct + acquiring_p90_pct) / 100 AS price_retention_downside,
  logistics_p90_rub + cogs_rub AS fixed_cost_downside_rub,
  'WB_STRESS_P90_ACQUIRING_AND_LOGISTICS' AS downside_case,
  contribution_before_ads_pre_tax_rub AS canonical_contribution_expected_rub,
  break_even_before_ads_pre_tax_base AS canonical_break_even_expected_rub,
  break_even_before_ads_pre_tax_stress AS canonical_break_even_downside_rub,
  economics_status AS canonical_economics_status,
  blocked_reason AS canonical_blocked_reason,
  economics_confidence AS canonical_economics_confidence,
  economics_model_version,
  tax_model_status,
  CASE
    WHEN internal_sku IS NULL THEN 'MISSING_CANONICAL_SKU'
    WHEN cogs_rub IS NULL THEN 'MISSING_COGS'
    WHEN seller_effective_price_rub IS NULL OR seller_effective_price_rub <= 0 OR effective_commission_pct IS NULL
      OR acquiring_p50_pct IS NULL OR logistics_expected_rub IS NULL
      OR effective_commission_pct + acquiring_p50_pct >= 100 THEN 'MISSING_ECONOMICS_COMPONENT'
    ELSE 'COMPUTABLE'
  END AS basis_status
FROM s
