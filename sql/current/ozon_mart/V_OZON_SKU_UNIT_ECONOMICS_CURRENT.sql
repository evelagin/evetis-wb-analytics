-- ============================================================================
-- CANONICAL CURRENT DEFINITION — ozon_mart.V_OZON_SKU_UNIT_ECONOMICS_CURRENT (VIEW)
-- Authoritative Git definition of the CURRENT production object. Not a migration,
-- not a rollback. Rules: sql/current/README.md. Metadata: MANIFEST.json.
-- Captured verbatim from production INFORMATION_SCHEMA.VIEWS at 2026-09-18T14:14:32Z
-- (main eecde14936d1). Historical source: sql/ozon/stage3_4c_ozon_mart.sql (parity: COMMENTS_WHITESPACE_ONLY).
-- Internal dependencies: FCT_OZON_SKU_PNL_MONTHLY.
-- The view body below is byte-for-byte the production body: do not reformat it.
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_SKU_UNIT_ECONOMICS_CURRENT`
OPTIONS (description = "TRAILING_OBSERVED юнит-экономика SKU Ozon за окно 180 дней. НЕ прогнозная: forward_pricing_ready = FALSE, действующие тарифы Ozon документально не подтверждены. ТОЛЬКО АНАЛИТИКА, цены и ставки не изменяются. Все расчётные поля с префиксом trailing_. full_marketplace_break_even_drr на уровне SKU = NULL: постоянные расходы магазина не разносятся. Прогнозное ценообразование - Stage 3.4D.")
AS
WITH win AS (SELECT DATE_SUB(CURRENT_DATE(), INTERVAL 180 DAY) d0, CURRENT_DATE() d1),
agg AS (
  SELECT f.internal_sku, ANY_VALUE(f.product_name) product_name, ANY_VALUE(f.product_type) product_type,
    SUM(f.realized_qty) realized_qty, SUM(f.seller_base_revenue_rub) rev,
    SUM(f.product_cogs_rub) cogs, SUM(f.commission_rub) comm,
    SUM(f.direct_variable_marketplace_costs_rub + f.other_direct_marketplace_costs_rub) var_cost,
    SUM(f.ad_spend_attributed_rub) ads,
    MIN(f.month) first_month, MAX(f.month) last_month,
    MAX(IF(f.cogs_status='MISSING_BOM_INTERVAL', 1, 0)) cogs_gap,
    MAX(IF(f.commission_status='MISSING_SOURCE', 1, 0)) comm_gap
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_SKU_PNL_MONTHLY` f, win
  WHERE f.month >= DATE_TRUNC(win.d0, MONTH) GROUP BY f.internal_sku
  HAVING SUM(f.realized_qty) > 0)
SELECT a.internal_sku, a.product_name, a.product_type, a.realized_qty,
  'TRAILING_OBSERVED' economics_mode,
  180 economics_window_days,
  (SELECT DATE_TRUNC(d0, MONTH) FROM win) window_start,
  (SELECT d1 FROM win) window_end,
  a.first_month first_observed_month,
  a.last_month last_observed_month,
  'TRAILING_OBSERVED_REALIZED_AVG' price_basis,
  'TRAILING_OBSERVED_EFFECTIVE_RATE' commission_basis,
  'TRAILING_OBSERVED_DIRECT_ACTUAL' marketplace_cost_basis,
  'TRAILING_OBSERVED_ATTRIBUTED_ACTUAL' advertising_basis,
  'PROVEN_DOCUMENT_EFFECTIVE_DATE' cogs_basis,
  FALSE forward_pricing_ready,
  'Действующая комиссия Ozon документально не подтверждена (дата ставки 52% NOT_PROVEN); действующие тарифы логистики и последней мили не подтверждены. Выводить их из средних за 180 дней запрещено. Stage 3.4D.' forward_pricing_blockers,
  ROUND(SAFE_DIVIDE(a.rev, a.realized_qty), 2) trailing_avg_seller_base_price_rub,
  ROUND(SAFE_DIVIDE(a.cogs, a.realized_qty), 2) trailing_avg_management_cogs_unit_rub,
  ROUND(SAFE_DIVIDE(a.comm, a.realized_qty), 2) trailing_avg_commission_unit_rub,
  ROUND(SAFE_DIVIDE(a.comm, NULLIF(a.rev,0))*100, 2) trailing_effective_commission_pct,
  ROUND(SAFE_DIVIDE(a.var_cost, a.realized_qty), 2) trailing_avg_variable_mp_cost_unit_rub,
  ROUND(SAFE_DIVIDE(a.ads, a.realized_qty), 2) trailing_ad_cost_per_realized_unit_rub,
  ROUND(SAFE_DIVIDE(a.rev - a.cogs - a.comm - a.var_cost, a.realized_qty), 2) trailing_l1_unit_contribution_rub,
  ROUND(SAFE_DIVIDE(a.rev - a.cogs - a.comm - a.var_cost, a.realized_qty), 2) trailing_l2_unit_available_for_ads_rub,
  ROUND(SAFE_DIVIDE(a.ads, NULLIF(a.rev,0))*100, 4) trailing_actual_drr_pct,
  ROUND(SAFE_DIVIDE(a.rev - a.cogs - a.comm - a.var_cost, NULLIF(a.rev,0))*100, 4) trailing_variable_break_even_drr_pct,
  CAST(NULL AS NUMERIC) full_marketplace_break_even_drr_pct,
  ROUND(SAFE_DIVIDE(a.rev - a.cogs - a.comm - a.var_cost, a.realized_qty), 2) trailing_max_ad_spend_per_unit_rub,
  ROUND(SAFE_DIVIDE(a.cogs + a.var_cost, a.realized_qty) / (1 - SAFE_DIVIDE(a.comm, NULLIF(a.rev,0))), 2) trailing_basis_break_even_price_rub,
  ROUND(SAFE_DIVIDE(a.cogs + a.var_cost, a.realized_qty) / ((1 - SAFE_DIVIDE(a.comm, NULLIF(a.rev,0))) - NUMERIC '0.10'), 2) trailing_basis_indicative_price_margin_10pct,
  ROUND(SAFE_DIVIDE(a.cogs + a.var_cost, a.realized_qty) / ((1 - SAFE_DIVIDE(a.comm, NULLIF(a.rev,0))) - NUMERIC '0.15'), 2) trailing_basis_indicative_price_margin_15pct,
  ROUND(SAFE_DIVIDE(a.cogs + a.var_cost, a.realized_qty) / ((1 - SAFE_DIVIDE(a.comm, NULLIF(a.rev,0))) - NUMERIC '0.20'), 2) trailing_basis_indicative_price_margin_20pct,
  ROUND(SAFE_DIVIDE(a.cogs + a.var_cost, a.realized_qty) / ((1 - SAFE_DIVIDE(a.comm, NULLIF(a.rev,0))) - NUMERIC '0.25'), 2) trailing_basis_indicative_price_margin_25pct,
  CASE WHEN a.cogs_gap = 1 THEN 'COGS_INCOMPLETE'
       WHEN a.rev - a.cogs - a.comm - a.var_cost <= 0 THEN 'LOSS_BEFORE_ADS'
       WHEN a.rev - a.cogs - a.comm - a.var_cost - a.ads > 0 THEN 'PROFITABLE_AFTER_ADS'
       ELSE 'PROFITABLE_BEFORE_ADS_ONLY' END trailing_profitability_status,
  CASE WHEN a.cogs_gap = 1 OR a.comm_gap = 1 THEN 'KNOWN_GAP' ELSE 'COMPLETE' END data_confidence,
  'ANALYTICAL_ONLY_NO_PRICE_WRITES' usage_note,
  CURRENT_TIMESTAMP() mart_computed_at
FROM agg a ORDER BY a.realized_qty DESC;
