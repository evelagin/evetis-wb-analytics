-- ============================================================================
-- CANONICAL CURRENT DEFINITION — ozon_mart.V_OZON_AGENT_DECISION_INPUT (VIEW)
-- Authoritative Git definition of the CURRENT production object. Not a migration,
-- not a rollback. Rules: sql/current/README.md. Metadata: MANIFEST.json.
-- Captured verbatim from production INFORMATION_SCHEMA.VIEWS at 2026-09-18T14:14:32Z
-- (main eecde14936d1). Historical source: sql/ozon/stage3_4d3_ozon_mart_agent_contract.sql (parity: TEXT_DIFFERS_SCHEMA_DATA_EQUIVALENT).
-- Internal dependencies: FCT_OZON_SKU_PNL_MONTHLY, V_OZON_SKU_FORWARD_ECONOMICS_CURRENT, V_OZON_TARIFF_SOURCE_HEALTH.
-- Live text differs from the historical file but is schema- and data-equivalent
-- (R2A preflight); per owner decision the validated live definition is canonical.
-- The view body below is byte-for-byte the production body: do not reformat it.
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_AGENT_DECISION_INPUT`
OPTIONS (description = "READ-ONLY контракт входа для будущих теневых агентов Ozon. Одна строка на текущий продаваемый SKU. Только проверенные текущие входы и наблюдённые канонические метрики с префиксом trailing_. Рекомендаций нет. agent_write_ready всегда FALSE и не выводится из данных.")
AS
WITH f AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_SKU_FORWARD_ECONOMICS_CURRENT`),
h AS (SELECT unknown_component_rows, agent_shadow_ready, agent_shadow_gate
      FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_TARIFF_SOURCE_HEALTH`),
obs AS (
  SELECT internal_sku, SUM(realized_qty) AS trailing_90d_realized_qty,
         ROUND(SUM(seller_base_revenue_rub), 2) AS trailing_90d_revenue_rub,
         ROUND(SUM(ad_spend_attributed_rub), 2) AS trailing_90d_ad_spend_rub,
         ROUND(SAFE_DIVIDE(SUM(ad_spend_attributed_rub), NULLIF(SUM(seller_base_revenue_rub), 0)) * 100, 2) AS trailing_90d_actual_drr_pct
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_SKU_PNL_MONTHLY`
  WHERE month >= DATE_TRUNC(DATE_SUB(CURRENT_DATE(), INTERVAL 90 DAY), MONTH)
  GROUP BY 1)
SELECT
  f.internal_sku, f.offer_id, f.ozon_sku, f.product_name, f.product_type,
  f.catalog_status, f.fbo_stock,
  IF(f.fbo_stock > 0, 'IN_STOCK', 'OUT_OF_STOCK') AS stock_status,
  f.seller_base_price, f.current_management_cogs_rub AS management_cogs,
  f.commission_pct AS current_commission_pct, f.commission_rub AS current_commission_rub,
  f.acquiring_rub, f.last_mile_rub,
  f.logistics_min_rub, f.logistics_expected_rub, f.logistics_max_rub, f.logistics_expected_basis,
  f.contribution_best_case, f.contribution_expected, f.contribution_worst_case,
  f.margin_best_case_pct, f.margin_expected_pct, f.margin_worst_case_pct,
  f.break_even_drr_best_pct, f.break_even_drr_expected_pct, f.break_even_drr_worst_pct,
  f.safe_ad_spend_15pct_worst_case_rub, f.safe_drr_15pct_worst_case_pct,
  f.max_ad_spend_at_20_margin_worst, f.drr_limit_at_20_margin_worst_pct,
  f.route_sensitivity_pp, f.safety_class_worst_case, f.expected_case_label,
  f.pricing_review_required, f.loss_risk_worst_case,
  IFNULL(o.trailing_90d_realized_qty, 0) AS trailing_90d_realized_qty,
  IFNULL(o.trailing_90d_revenue_rub, 0) AS trailing_90d_revenue_rub,
  IFNULL(o.trailing_90d_ad_spend_rub, 0) AS trailing_90d_ad_spend_rub,
  o.trailing_90d_actual_drr_pct,
  f.cancellation_rate_pct AS trailing_cancellation_rate_pct,
  f.cancellation_probability_status,
  f.tariff_snapshot_at, f.tariff_freshness_status, f.seller_info_freshness_status,
  f.tariff_change_baseline_status, f.premium_status,
  h.unknown_component_rows AS unknown_tariff_component_count,
  f.forward_economics_ready, f.agent_decision_gate,
  h.agent_shadow_ready, h.agent_shadow_gate,
  FALSE AS agent_write_ready,
  'HARDCODED_FALSE_NOT_DERIVABLE_FROM_DATA_REQUIRES_SEPARATE_OWNER_ACK' AS agent_write_gate,
  'READ_ONLY_INPUT_NO_RECOMMENDATIONS_NO_WRITES' AS usage_note,
  CURRENT_TIMESTAMP() AS mart_computed_at
FROM f LEFT JOIN obs o USING (internal_sku)
CROSS JOIN h
ORDER BY f.margin_worst_case_pct;
