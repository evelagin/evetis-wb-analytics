-- ============================================================================
-- ПРЕДЛОЖЕНИЕ (НЕ РАЗВЁРНУТО). Executive V2 Phase C — дневной слой Executive.
-- Статус: proposal. Разворачивать только после подтверждения владельца.
-- Документ: docs/EXECUTIVE_V2_PHASE_C_PERFORMANCE_2026-09-16.md
--
-- Это тело будущей таблицы wb_mart.EXECUTIVE_V2_DAILY (грейн day). Все колонки —
-- pass-through существующих канонических view; ни одна формула не вводится.
-- Имена колонок совпадают с исходными, поэтому SQL карточек переводится на слой
-- механической заменой имени источника (доказательство — proof-скрипт).
-- ============================================================================
WITH
k AS (
  SELECT day, orders_covered, orders_gross_qty, sales_covered, buyouts_qty,
         sales_revenue_seller_base_rub, sales_revenue_buyer_paid_rub, logistics_rub, storage_rub
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_KPI_DAILY`
),
c AS (
  SELECT day, ads_billing_covered, ad_spend_financial_rub,
         tariff_option_minimum_payment_rub, account_level_total_corrected_rub
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_FINANCE_CORRECTED_DAILY`
),
e AS (
  SELECT day, period_result_eligible, after_product_cogs_eligible,
         period_result_pre_cogs_corrected_rub, revenue_base_period_result_rub,
         product_cogs_rub, period_result_after_product_cogs_rub, revenue_base_after_product_cogs_rub,
         executive_incomplete_day, executive_provisional_day, executive_final_day,
         exec_provisional_ads_billing_day
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_EXECUTIVE_ECONOMICS_DAILY`
),
s AS (
  SELECT day,
         settlement_price_chain_covered, settlement_seller_price_rub, settlement_spp_rub,
         settlement_wb_remuneration_rub, settlement_wb_remuneration_vat_rub, settlement_acquiring_rub,
         settlement_pvz_reward_rub, settlement_goods_rub, post_realization_deductions_rub, wb_payout_rub,
         post_sale_wb_promotion_documents_rub, post_sale_logistics_rub, post_sale_storage_rub,
         post_sale_tariff_option_minimum_payment_rub, post_sale_utilization_rub, post_sale_transit_rub,
         post_sale_penalty_rub, post_sale_acceptance_rub, post_sale_force_majeure_rub,
         post_sale_review_points_refund_rub, post_sale_unclassified_deductions_rub,
         post_sale_loyalty_program_rub, post_sale_loyalty_points_rub, post_sale_other_operations_rub
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_SETTLEMENT_DAILY`
),
x AS (
  SELECT day, logistics_sold_rub, logistics_cancellation_rub,
         logistics_cancel_to_customer_rub, logistics_cancel_from_customer_rub
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_EXECUTIVE_BREAKDOWN_DAILY`
),
h AS (
  SELECT cohort_date AS day, cohort_orders, buyout_orders, resolved_orders, unresolved_orders, cohort_provisional_day
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_BUYOUT_COHORT_DAILY`
),
f AS (
  SELECT data_as_of_min, mart_built_at
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_FRESHNESS_HEADER`
)
SELECT
  k.day,
  k.day AS cohort_date,
  k.* EXCEPT (day),
  c.* EXCEPT (day),
  e.* EXCEPT (day),
  s.* EXCEPT (day),
  x.* EXCEPT (day),
  h.* EXCEPT (day),
  f.data_as_of_min,
  f.mart_built_at
FROM k
LEFT JOIN c USING (day)
LEFT JOIN e USING (day)
LEFT JOIN s USING (day)
LEFT JOIN x USING (day)
LEFT JOIN h USING (day)
CROSS JOIN f
