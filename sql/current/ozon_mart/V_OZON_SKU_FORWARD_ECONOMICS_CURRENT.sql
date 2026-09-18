-- ============================================================================
-- CANONICAL CURRENT DEFINITION — ozon_mart.V_OZON_SKU_FORWARD_ECONOMICS_CURRENT (VIEW)
-- Authoritative Git definition of the CURRENT production object. Not a migration,
-- not a rollback. Rules: sql/current/README.md. Metadata: MANIFEST.json.
-- Captured verbatim from production INFORMATION_SCHEMA.VIEWS at 2026-09-18T14:14:32Z
-- (main eecde14936d1). Historical source: sql/ozon/stage3_4d3_ozon_mart_agent_contract.sql (parity: COMMENTS_WHITESPACE_ONLY).
-- Internal dependencies: V_OZON_SKU_CURRENT_TARIFF, V_OZON_TARIFF_SOURCE_HEALTH.
-- Older definition in sql/ozon/stage3_4d2_ozon_mart_forward.sql is SUPERSEDED
-- and must not be applied (it would break dependants; proven in R2 forensic preflight).
-- The view body below is byte-for-byte the production body: do not reformat it.
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_SKU_FORWARD_ECONOMICS_CURRENT`
OPTIONS (description = "FORWARD_MODELLED юнит-экономика одного нового заказа Ozon по ДЕЙСТВУЮЩЕМУ тарифу. Три сценария логистики: best (API min) / expected (MODELLED, не тариф) / worst (API max). Классификация безопасности - по worst case. Premium в формулу вклада SKU не входит. НЕ путать с V_OZON_SKU_UNIT_ECONOMICS_CURRENT (TRAILING_OBSERVED, 180 дней). ТОЛЬКО АНАЛИТИКА.")
AS
WITH t AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_SKU_CURRENT_TARIFF`),
health AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_TARIFF_SOURCE_HEALTH`),
canc AS (
  SELECT mp.internal_sku,
         ROUND(SAFE_DIVIDE(COUNTIF(po.status='cancelled'), COUNT(*)) * 100, 2) AS cancellation_rate_pct,
         COUNT(*) AS orders_in_window
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_POSTINGS_FBO` po
  JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP` mp
    ON mp.marketplace='OZON' AND mp.marketplace_sku = po.sku AND mp.is_current
  WHERE po.order_date BETWEEN DATE '2026-06-01' AND DATE_SUB(CURRENT_DATE(), INTERVAL 12 DAY)
  GROUP BY 1),
b AS (
  SELECT t.*, SAFE_DIVIDE(t.commission_fbo_pct, 100) AS c_frac,
    SAFE_DIVIDE(t.acquiring_pct, 100) AS a_frac,
    t.current_management_cogs_rub + t.last_mile_rub AS fixed_ex_logistics,
    ROUND(t.seller_base_price - t.current_management_cogs_rub - t.commission_fbo_rub_at_current_price
          - t.acquiring_max_rub - t.fbo_logistics_min_rub - t.last_mile_rub, 2) AS contribution_best_case,
    ROUND(t.seller_base_price - t.current_management_cogs_rub - t.commission_fbo_rub_at_current_price
          - t.acquiring_max_rub - t.fbo_logistics_expected_rub - t.last_mile_rub, 2) AS contribution_expected,
    ROUND(t.seller_base_price - t.current_management_cogs_rub - t.commission_fbo_rub_at_current_price
          - t.acquiring_max_rub - t.fbo_logistics_max_rub - t.last_mile_rub, 2) AS contribution_worst_case
  FROM t)
SELECT
  b.internal_sku, b.offer_id, b.ozon_sku, b.product_name, b.product_type, b.catalog_status, b.fbo_stock,
  'FORWARD_MODELLED' AS economics_mode,
  b.snapshot_at AS tariff_snapshot_at, b.tariff_effective_at,
  'MODELLED_EXPECTED' AS expected_case_label_basis,
  b.seller_base_price, b.current_management_cogs_rub, b.cogs_basis,
  b.commission_fbo_pct AS commission_pct, b.commission_fbo_rub_at_current_price AS commission_rub,
  b.acquiring_max_rub AS acquiring_rub, b.last_mile_rub,
  b.fbo_logistics_min_rub AS logistics_min_rub, b.fbo_logistics_expected_rub AS logistics_expected_rub,
  b.fbo_logistics_max_rub AS logistics_max_rub, b.fbo_logistics_expected_basis AS logistics_expected_basis,
  b.fbo_logistics_observations,
  ROUND(b.commission_fbo_rub_at_current_price + b.acquiring_max_rub + b.fbo_logistics_min_rub + b.last_mile_rub, 2) AS mandatory_costs_min_logistics_rub,
  ROUND(b.commission_fbo_rub_at_current_price + b.acquiring_max_rub + b.fbo_logistics_expected_rub + b.last_mile_rub, 2) AS mandatory_costs_expected_rub,
  ROUND(b.commission_fbo_rub_at_current_price + b.acquiring_max_rub + b.fbo_logistics_max_rub + b.last_mile_rub, 2) AS mandatory_costs_max_logistics_rub,
  b.contribution_best_case, b.contribution_expected, b.contribution_worst_case,
  b.contribution_best_case AS fbo_contribution_min_logistics_case,
  b.contribution_expected AS fbo_contribution_expected_case,
  b.contribution_worst_case AS fbo_contribution_max_logistics_case,
  ROUND(SAFE_DIVIDE(b.contribution_best_case,  b.seller_base_price) * 100, 2) AS margin_best_case_pct,
  ROUND(SAFE_DIVIDE(b.contribution_expected,   b.seller_base_price) * 100, 2) AS margin_expected_pct,
  ROUND(SAFE_DIVIDE(b.contribution_worst_case, b.seller_base_price) * 100, 2) AS margin_worst_case_pct,
  ROUND(SAFE_DIVIDE(b.contribution_best_case,  b.seller_base_price) * 100, 2) AS break_even_drr_best_pct,
  ROUND(SAFE_DIVIDE(b.contribution_expected,   b.seller_base_price) * 100, 2) AS break_even_drr_expected_pct,
  ROUND(SAFE_DIVIDE(b.contribution_worst_case, b.seller_base_price) * 100, 2) AS break_even_drr_worst_pct,
  b.contribution_best_case AS max_ad_spend_at_0_margin_best,
  b.contribution_expected AS max_ad_spend_at_0_margin_expected,
  b.contribution_worst_case AS max_ad_spend_at_0_margin_worst,
  ROUND(b.contribution_best_case  - b.seller_base_price * NUMERIC '0.10', 2) AS max_ad_spend_at_10_margin_best,
  ROUND(b.contribution_expected   - b.seller_base_price * NUMERIC '0.10', 2) AS max_ad_spend_at_10_margin_expected,
  ROUND(b.contribution_worst_case - b.seller_base_price * NUMERIC '0.10', 2) AS max_ad_spend_at_10_margin_worst,
  ROUND(b.contribution_best_case  - b.seller_base_price * NUMERIC '0.15', 2) AS max_ad_spend_at_15_margin_best,
  ROUND(b.contribution_expected   - b.seller_base_price * NUMERIC '0.15', 2) AS max_ad_spend_at_15_margin_expected,
  ROUND(b.contribution_worst_case - b.seller_base_price * NUMERIC '0.15', 2) AS max_ad_spend_at_15_margin_worst,
  ROUND(b.contribution_best_case  - b.seller_base_price * NUMERIC '0.20', 2) AS max_ad_spend_at_20_margin_best,
  ROUND(b.contribution_expected   - b.seller_base_price * NUMERIC '0.20', 2) AS max_ad_spend_at_20_margin_expected,
  ROUND(b.contribution_worst_case - b.seller_base_price * NUMERIC '0.20', 2) AS max_ad_spend_at_20_margin_worst,
  ROUND(b.contribution_best_case  - b.seller_base_price * NUMERIC '0.25', 2) AS max_ad_spend_at_25_margin_best,
  ROUND(b.contribution_expected   - b.seller_base_price * NUMERIC '0.25', 2) AS max_ad_spend_at_25_margin_expected,
  ROUND(b.contribution_worst_case - b.seller_base_price * NUMERIC '0.25', 2) AS max_ad_spend_at_25_margin_worst,
  ROUND(SAFE_DIVIDE(b.contribution_expected   - b.seller_base_price * NUMERIC '0.15', b.seller_base_price) * 100, 2) AS drr_limit_at_15_margin_expected_pct,
  ROUND(SAFE_DIVIDE(b.contribution_worst_case - b.seller_base_price * NUMERIC '0.15', b.seller_base_price) * 100, 2) AS drr_limit_at_15_margin_worst_pct,
  ROUND(SAFE_DIVIDE(b.contribution_worst_case - b.seller_base_price * NUMERIC '0.20', b.seller_base_price) * 100, 2) AS drr_limit_at_20_margin_worst_pct,
  ROUND(b.contribution_worst_case - b.seller_base_price * NUMERIC '0.15', 2) AS safe_ad_spend_15pct_worst_case_rub,
  ROUND(SAFE_DIVIDE(b.contribution_worst_case - b.seller_base_price * NUMERIC '0.15', b.seller_base_price) * 100, 2) AS safe_drr_15pct_worst_case_pct,
  ROUND(b.fbo_logistics_max_rub - b.fbo_logistics_min_rub, 2) AS route_sensitivity_rub,
  ROUND(SAFE_DIVIDE(b.fbo_logistics_max_rub - b.fbo_logistics_min_rub, b.seller_base_price) * 100, 2) AS route_sensitivity_pp,
  ROUND(SAFE_DIVIDE(b.fixed_ex_logistics + b.fbo_logistics_expected_rub, NULLIF(1 - b.c_frac - b.a_frac, 0)), 2) AS break_even_price_expected,
  ROUND(SAFE_DIVIDE(b.fixed_ex_logistics + b.fbo_logistics_max_rub,      NULLIF(1 - b.c_frac - b.a_frac, 0)), 2) AS break_even_price_worst,
  ROUND(SAFE_DIVIDE(b.fixed_ex_logistics + b.fbo_logistics_min_rub,      NULLIF(1 - b.c_frac - b.a_frac - 0.10, 0)), 2) AS target_price_10pct_no_ads_best,
  ROUND(SAFE_DIVIDE(b.fixed_ex_logistics + b.fbo_logistics_expected_rub, NULLIF(1 - b.c_frac - b.a_frac - 0.10, 0)), 2) AS target_price_10pct_no_ads_expected,
  ROUND(SAFE_DIVIDE(b.fixed_ex_logistics + b.fbo_logistics_max_rub,      NULLIF(1 - b.c_frac - b.a_frac - 0.10, 0)), 2) AS target_price_10pct_no_ads_worst,
  ROUND(SAFE_DIVIDE(b.fixed_ex_logistics + b.fbo_logistics_min_rub,      NULLIF(1 - b.c_frac - b.a_frac - 0.15, 0)), 2) AS target_price_15pct_no_ads_best,
  ROUND(SAFE_DIVIDE(b.fixed_ex_logistics + b.fbo_logistics_expected_rub, NULLIF(1 - b.c_frac - b.a_frac - 0.15, 0)), 2) AS target_price_15pct_no_ads_expected,
  ROUND(SAFE_DIVIDE(b.fixed_ex_logistics + b.fbo_logistics_max_rub,      NULLIF(1 - b.c_frac - b.a_frac - 0.15, 0)), 2) AS target_price_15pct_no_ads_worst,
  ROUND(SAFE_DIVIDE(b.fixed_ex_logistics + b.fbo_logistics_min_rub,      NULLIF(1 - b.c_frac - b.a_frac - 0.20, 0)), 2) AS target_price_20pct_no_ads_best,
  ROUND(SAFE_DIVIDE(b.fixed_ex_logistics + b.fbo_logistics_expected_rub, NULLIF(1 - b.c_frac - b.a_frac - 0.20, 0)), 2) AS target_price_20pct_no_ads_expected,
  ROUND(SAFE_DIVIDE(b.fixed_ex_logistics + b.fbo_logistics_max_rub,      NULLIF(1 - b.c_frac - b.a_frac - 0.20, 0)), 2) AS target_price_20pct_no_ads_worst,
  ROUND(SAFE_DIVIDE(b.fixed_ex_logistics + b.fbo_logistics_min_rub,      NULLIF(1 - b.c_frac - b.a_frac - 0.25, 0)), 2) AS target_price_25pct_no_ads_best,
  ROUND(SAFE_DIVIDE(b.fixed_ex_logistics + b.fbo_logistics_expected_rub, NULLIF(1 - b.c_frac - b.a_frac - 0.25, 0)), 2) AS target_price_25pct_no_ads_expected,
  ROUND(SAFE_DIVIDE(b.fixed_ex_logistics + b.fbo_logistics_max_rub,      NULLIF(1 - b.c_frac - b.a_frac - 0.25, 0)), 2) AS target_price_25pct_no_ads_worst,
  ROUND(SAFE_DIVIDE(b.fixed_ex_logistics + b.fbo_logistics_expected_rub, NULLIF(1 - b.c_frac - b.a_frac - 0.15 - 0.05, 0)), 2) AS target_price_15pct_at_drr5_expected,
  ROUND(SAFE_DIVIDE(b.fixed_ex_logistics + b.fbo_logistics_expected_rub, NULLIF(1 - b.c_frac - b.a_frac - 0.15 - 0.10, 0)), 2) AS target_price_15pct_at_drr10_expected,
  ROUND(SAFE_DIVIDE(b.fixed_ex_logistics + b.fbo_logistics_expected_rub, NULLIF(1 - b.c_frac - b.a_frac - 0.15 - 0.15, 0)), 2) AS target_price_15pct_at_drr15_expected,
  ROUND(SAFE_DIVIDE(b.fixed_ex_logistics + b.fbo_logistics_expected_rub, NULLIF(1 - b.c_frac - b.a_frac - 0.15 - 0.20, 0)), 2) AS target_price_15pct_at_drr20_expected,
  ROUND(SAFE_DIVIDE(b.fixed_ex_logistics + b.fbo_logistics_expected_rub, NULLIF(1 - b.c_frac - b.a_frac - 0.15 - 0.25, 0)), 2) AS target_price_15pct_at_drr25_expected,
  ROUND(SAFE_DIVIDE(b.fixed_ex_logistics + b.fbo_logistics_max_rub, NULLIF(1 - b.c_frac - b.a_frac - 0.15 - 0.05, 0)), 2) AS target_price_15pct_at_drr5_worst,
  ROUND(SAFE_DIVIDE(b.fixed_ex_logistics + b.fbo_logistics_max_rub, NULLIF(1 - b.c_frac - b.a_frac - 0.15 - 0.10, 0)), 2) AS target_price_15pct_at_drr10_worst,
  ROUND(SAFE_DIVIDE(b.fixed_ex_logistics + b.fbo_logistics_max_rub, NULLIF(1 - b.c_frac - b.a_frac - 0.15 - 0.15, 0)), 2) AS target_price_15pct_at_drr15_worst,
  ROUND(SAFE_DIVIDE(b.fixed_ex_logistics + b.fbo_logistics_expected_rub, NULLIF(1 - b.c_frac - b.a_frac - 0.20 - 0.05, 0)), 2) AS target_price_20pct_at_drr5_expected,
  ROUND(SAFE_DIVIDE(b.fixed_ex_logistics + b.fbo_logistics_expected_rub, NULLIF(1 - b.c_frac - b.a_frac - 0.20 - 0.10, 0)), 2) AS target_price_20pct_at_drr10_expected,
  ROUND(SAFE_DIVIDE(b.fixed_ex_logistics + b.fbo_logistics_expected_rub, NULLIF(1 - b.c_frac - b.a_frac - 0.20 - 0.15, 0)), 2) AS target_price_20pct_at_drr15_expected,
  ROUND(SAFE_DIVIDE(b.fixed_ex_logistics + b.fbo_logistics_expected_rub, NULLIF(1 - b.c_frac - b.a_frac - 0.20 - 0.20, 0)), 2) AS target_price_20pct_at_drr20_expected,
  ROUND(SAFE_DIVIDE(b.fixed_ex_logistics + b.fbo_logistics_expected_rub, NULLIF(1 - b.c_frac - b.a_frac - 0.20 - 0.25, 0)), 2) AS target_price_20pct_at_drr25_expected,
  ROUND(SAFE_DIVIDE(b.fixed_ex_logistics + b.fbo_logistics_max_rub, NULLIF(1 - b.c_frac - b.a_frac - 0.20 - 0.05, 0)), 2) AS target_price_20pct_at_drr5_worst,
  ROUND(SAFE_DIVIDE(b.fixed_ex_logistics + b.fbo_logistics_max_rub, NULLIF(1 - b.c_frac - b.a_frac - 0.20 - 0.10, 0)), 2) AS target_price_20pct_at_drr10_worst,
  ROUND(SAFE_DIVIDE(b.fixed_ex_logistics + b.fbo_logistics_max_rub, NULLIF(1 - b.c_frac - b.a_frac - 0.20 - 0.15, 0)), 2) AS target_price_20pct_at_drr15_worst,
  ROUND(SAFE_DIVIDE(b.fixed_ex_logistics + b.fbo_logistics_max_rub, NULLIF(1 - b.c_frac - b.a_frac - 0.25 - 0.10, 0)), 2) AS target_price_25pct_at_drr10_worst,
  cn.cancellation_rate_pct, cn.orders_in_window AS cancellation_window_orders,
  IF(cn.orders_in_window IS NULL, 'NO_ORDERS_IN_WINDOW', 'PROVEN_STATUS_BASED') AS cancellation_probability_status,
  'NOT_PROVEN_RETURN_QTY' AS return_probability_status,
  CAST(NULL AS NUMERIC) AS expected_return_cost_rub,
  ROUND(b.fbo_logistics_expected_rub + b.fbo_return_logistics_rub + b.return_processing_rub, 2) AS cancel_cost_if_shipped_expected_rub,
  ROUND(b.fbo_logistics_max_rub + b.fbo_return_logistics_rub + b.return_processing_rub, 2) AS cancel_cost_if_shipped_worst_rub,
  NUMERIC '0.558' AS p_shipped_given_cancel_portfolio,
  ROUND((1 - SAFE_DIVIDE(cn.cancellation_rate_pct, 100)) * b.contribution_expected
        - SAFE_DIVIDE(cn.cancellation_rate_pct, 100) * NUMERIC '0.558'
          * (b.fbo_logistics_expected_rub + b.fbo_return_logistics_rub + b.return_processing_rub), 2) AS expected_contribution_per_created_order_expected_rub,
  ROUND((1 - SAFE_DIVIDE(cn.cancellation_rate_pct, 100)) * b.contribution_worst_case
        - SAFE_DIVIDE(cn.cancellation_rate_pct, 100) * NUMERIC '0.558'
          * (b.fbo_logistics_max_rub + b.fbo_return_logistics_rub + b.return_processing_rub), 2) AS expected_contribution_per_created_order_worst_rub,
  'CANCEL_ONLY_RETURNS_EXCLUDED' AS expected_mode_status,
  CASE
    WHEN b.current_management_cogs_rub IS NULL OR b.seller_base_price IS NULL
      OR b.commission_fbo_pct IS NULL OR b.fbo_logistics_max_rub IS NULL
      OR NOT hh.forward_economics_ready THEN 'BLOCKED_BY_DATA'
    WHEN b.contribution_worst_case < 0 THEN 'LOSS_RISK_WORST_CASE'
    WHEN SAFE_DIVIDE(b.contribution_worst_case, b.seller_base_price) * 100 >= 25 THEN 'SCALE_SAFE'
    WHEN SAFE_DIVIDE(b.contribution_worst_case, b.seller_base_price) * 100 >= 20 THEN 'HEALTHY_SAFE'
    WHEN SAFE_DIVIDE(b.contribution_worst_case, b.seller_base_price) * 100 >= 10 THEN 'WATCH'
    ELSE 'RESTRICT_ADS' END AS safety_class_worst_case,
  CASE
    WHEN b.contribution_expected < 0 THEN 'LOSS_MAKING_BEFORE_ADS'
    WHEN SAFE_DIVIDE(b.contribution_expected, b.seller_base_price) * 100 >= 30 THEN 'SCALE_CANDIDATE'
    WHEN SAFE_DIVIDE(b.contribution_expected, b.seller_base_price) * 100 >= 20 THEN 'HEALTHY'
    WHEN SAFE_DIVIDE(b.contribution_expected, b.seller_base_price) * 100 >= 10 THEN 'WATCH'
    ELSE 'RESTRICT_ADS_PRICE_REVIEW' END AS expected_case_label,
  b.contribution_worst_case < 0 AS loss_risk_worst_case,
  SAFE_DIVIDE(b.contribution_worst_case, b.seller_base_price) * 100 < 10 AS pricing_review_required,
  b.premium_status, b.premium_active, b.premium_plus_active,
  b.premium_status_source_at, b.premium_affects_sku_contribution,
  hh.forward_economics_ready, hh.agent_decision_gate,
  hh.tariff_freshness_status, hh.seller_info_freshness_status,
  hh.tariff_change_baseline_status, hh.unknown_tariff_component_detected,
  hh.agent_shadow_ready, hh.agent_write_ready,
  b.proof_status_commission, b.proof_status_logistics, b.proof_status_acquiring,
  'ANALYTICAL_ONLY_NO_PRICE_WRITES' AS usage_note,
  CURRENT_TIMESTAMP() AS mart_computed_at
FROM b LEFT JOIN canc cn USING (internal_sku)
CROSS JOIN health hh
ORDER BY margin_worst_case_pct DESC;
