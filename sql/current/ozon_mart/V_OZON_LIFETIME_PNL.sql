-- ============================================================================
-- CANONICAL CURRENT DEFINITION — ozon_mart.V_OZON_LIFETIME_PNL (VIEW)
-- Authoritative Git definition of the CURRENT production object. Not a migration,
-- not a rollback. Rules: sql/current/README.md. Metadata: MANIFEST.json.
-- Captured verbatim from production INFORMATION_SCHEMA.VIEWS at 2026-09-18T14:14:32Z
-- (main eecde14936d1). Historical source: sql/ozon/stage3_4c_ozon_mart.sql (parity: EXACT_TEXT).
-- Internal dependencies: FCT_OZON_PNL_MONTHLY, FCT_OZON_SKU_PNL_MONTHLY, V_OZON_MART_FRESHNESS.
-- The view body below is byte-for-byte the production body: do not reformat it.
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_LIFETIME_PNL`
OPTIONS (description = "Агрегат FCT_OZON_PNL_MONTHLY за всю историю. Мост L1-L4 по OZON_PNL_POLICY_V1. Статус PASS_WITH_KNOWN_COMMISSION_GAP: комиссия по 6 postings неизвестна, диапазон истинного вклада показан явно. Отсутствующая комиссия никогда не ноль. Поля freshness_* относятся к свежести ozon_raw, не к марту.")
AS
SELECT
  CAST(MIN(month) AS STRING) period_from, CAST(MAX(month) AS STRING) period_to, COUNT(*) months,
  SUM(gross_ordered_qty) gross_ordered_qty, SUM(cancelled_qty) cancelled_qty,
  SUM(in_transit_qty) in_transit_qty, SUM(realized_qty) realized_qty,
  SUM(seller_base_revenue_rub) seller_base_revenue_rub,
  SUM(buyer_paid_revenue_rub) buyer_paid_revenue_rub,
  SUM(bonus_rub) bonus_rub, SUM(coinvestment_rub) coinvestment_rub,
  SUM(product_cogs_rub) product_cogs_rub,
  SUM(commission_known_rub) commission_known_rub,
  SUM(direct_variable_marketplace_costs_rub) direct_variable_marketplace_costs_rub,
  SUM(l1_gross_seller_contribution_rub) l1_gross_seller_contribution_rub,
  SUM(store_level_variable_costs_rub) store_level_variable_costs_rub,
  SUM(other_marketplace_costs_ex_ads_rub) other_marketplace_costs_ex_ads_rub,
  SUM(l2_contribution_before_ads_rub) l2_contribution_before_ads_rub,
  SUM(advertising_rub) advertising_rub,
  SUM(l3_after_ads_before_fixed_rub) l3_after_ads_before_fixed_rub,
  SUM(marketplace_fixed_costs_rub) marketplace_fixed_costs_rub,
  SUM(compensations_rub) compensations_rub,
  SUM(l4_marketplace_contribution_profit_known_rub) canonical_known_contribution_rub,
  SUM(l4_profit_uncertainty_lower_rub) contribution_uncertainty_lower_rub,
  SUM(l4_profit_uncertainty_upper_rub) contribution_uncertainty_upper_rub,
  ROUND(SAFE_DIVIDE(SUM(l4_marketplace_contribution_profit_known_rub), SUM(seller_base_revenue_rub))*100, 4) canonical_known_margin_pct,
  ROUND(SAFE_DIVIDE(SUM(advertising_rub), SUM(seller_base_revenue_rub))*100, 4) actual_drr_pct,
  ROUND(SAFE_DIVIDE(SUM(l2_contribution_before_ads_rub), SUM(seller_base_revenue_rub))*100, 4) variable_break_even_drr_pct,
  ROUND(SAFE_DIVIDE(SUM(l2_contribution_before_ads_rub) - SUM(marketplace_fixed_costs_rub) + SUM(compensations_rub),
        SUM(seller_base_revenue_rub))*100, 4) full_marketplace_break_even_drr_pct,
  ROUND(SAFE_DIVIDE(SUM(realized_qty), NULLIF(SUM(realized_qty),0))*100, 2) seller_base_coverage_pct,
  SUM(cogs_covered_qty) cogs_covered_qty, SUM(cogs_missing_qty) cogs_missing_qty,
  ROUND(SAFE_DIVIDE(SUM(cogs_covered_qty), SUM(realized_qty))*100, 4) cogs_coverage_pct,
  SUM(commission_coverage_qty) commission_coverage_qty, SUM(commission_missing_qty) commission_missing_qty,
  SUM(commission_missing_revenue_rub) commission_missing_revenue_rub,
  SUM(commission_uncertainty_lower_rub) commission_uncertainty_lower_rub,
  SUM(commission_uncertainty_upper_rub) commission_uncertainty_upper_rub,
  ROUND(SAFE_DIVIDE(SUM(commission_coverage_qty), SUM(realized_qty))*100, 4) commission_coverage_pct,
  NUMERIC '100' finance_taxonomy_coverage_pct,
  NUMERIC '100' ad_total_coverage_pct,
  ROUND(SAFE_DIVIDE((SELECT SUM(ad_spend_attributed_rub) FROM `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_SKU_PNL_MONTHLY`),
        (SELECT SUM(expense_rub) FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_ADS_EXPENSE_DAILY`))*100, 4) ad_sku_attribution_coverage_pct,
  'PASS_WITH_KNOWN_COMMISSION_GAP' data_status,
  CURRENT_TIMESTAMP() mart_computed_at,
  (SELECT source_max_posting_date FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_MART_FRESHNESS`) source_max_posting_date,
  (SELECT source_max_finance_date FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_MART_FRESHNESS`) source_max_finance_date,
  (SELECT source_max_ads_date FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_MART_FRESHNESS`) source_max_ads_date,
  (SELECT freshness_status FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_MART_FRESHNESS`) freshness_status,
  (SELECT agent_decision_gate FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_MART_FRESHNESS`) agent_decision_gate
FROM `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_PNL_MONTHLY`;
