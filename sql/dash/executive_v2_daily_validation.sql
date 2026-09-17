-- ============================================================================
-- EXECUTIVE V2 · PHASE C2 — валидация материализованного слоя
-- Запускать сразу после успешной сборки: CALL wb_mart.sp_build_executive_v2_daily('manual').
-- Между сборкой и проверкой не должно быть перестройки витрины/загрузчиков,
-- иначе V1/V4 честно покажут, что слой отстал от источника.
--
-- V1  календарь view = календарь V_DASH_KPI_DAILY
-- V2  построчное равенство с каждым каноническим view, обе стороны EXCEPT DISTINCT,
--     включая признаки момента чтения (статусы пересчитываются во view)
-- V3  дни без строки в каноническом view: все колонки этого view в слое NULL
-- V4  свежесть: «Данные по» = V_DASH_FRESHNESS_HEADER.data_as_of_min, сборка витрины = mart_built_at
-- V5  журнал: последняя сборка слоя SUCCESS, замок свободен
-- ============================================================================

ASSERT (
  SELECT COUNT(*) FROM (
    (SELECT day FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_EXECUTIVE_V2_DAILY` EXCEPT DISTINCT SELECT day FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_KPI_DAILY`)
    UNION ALL
    (SELECT day FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_KPI_DAILY` EXCEPT DISTINCT SELECT day FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_EXECUTIVE_V2_DAILY`)
  )
) = 0 AS 'V1: календарь V_DASH_EXECUTIVE_V2_DAILY не совпал с V_DASH_KPI_DAILY';

-- V2 V_DASH_KPI_DAILY: 14 колонок
ASSERT (
  WITH canon AS (
    SELECT day AS day,
         orders_covered,
         sales_covered,
         ads_covered,
         finance_covered,
         contribution_covered,
         finance_is_final,
         contains_provisional_finance,
         orders_gross_qty,
         buyouts_qty,
         sales_revenue_seller_base_rub,
         sales_revenue_buyer_paid_rub,
         logistics_rub,
         storage_rub,
         is_current_day
    FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_KPI_DAILY`
  ),
  layer AS (
    SELECT day,
         orders_covered,
         sales_covered,
         ads_covered,
         finance_covered,
         contribution_covered,
         finance_is_final,
         contains_provisional_finance,
         orders_gross_qty,
         buyouts_qty,
         sales_revenue_seller_base_rub,
         sales_revenue_buyer_paid_rub,
         logistics_rub,
         storage_rub,
         is_current_day
    FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_EXECUTIVE_V2_DAILY`
    WHERE day IN (SELECT day FROM canon)
  )
  SELECT (SELECT COUNT(*) FROM (SELECT * FROM canon EXCEPT DISTINCT SELECT * FROM layer))
       + (SELECT COUNT(*) FROM (SELECT * FROM layer EXCEPT DISTINCT SELECT * FROM canon))
) = 0 AS 'V2: V_DASH_EXECUTIVE_V2_DAILY разошёлся с V_DASH_KPI_DAILY';

ASSERT (
  SELECT COUNTIF(NOT (orders_covered IS NULL AND sales_covered IS NULL AND ads_covered IS NULL AND finance_covered IS NULL AND contribution_covered IS NULL AND finance_is_final IS NULL AND contains_provisional_finance IS NULL AND orders_gross_qty IS NULL AND buyouts_qty IS NULL AND sales_revenue_seller_base_rub IS NULL AND sales_revenue_buyer_paid_rub IS NULL AND logistics_rub IS NULL AND storage_rub IS NULL))
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_EXECUTIVE_V2_DAILY`
  WHERE day NOT IN (SELECT day FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_KPI_DAILY`)
) = 0 AS 'V3: в слое есть значения V_DASH_KPI_DAILY на днях, которых нет в каноническом view';

-- V2 V_DASH_FINANCE_CORRECTED_DAILY: 5 колонок
ASSERT (
  WITH canon AS (
    SELECT day AS day,
         ads_billing_covered,
         ad_spend_financial_rub,
         tariff_option_minimum_payment_rub,
         account_level_total_corrected_rub,
         ads_billing_is_final
    FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_FINANCE_CORRECTED_DAILY`
  ),
  layer AS (
    SELECT day,
         ads_billing_covered,
         ad_spend_financial_rub,
         tariff_option_minimum_payment_rub,
         account_level_total_corrected_rub,
         ads_billing_is_final
    FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_EXECUTIVE_V2_DAILY`
    WHERE day IN (SELECT day FROM canon)
  )
  SELECT (SELECT COUNT(*) FROM (SELECT * FROM canon EXCEPT DISTINCT SELECT * FROM layer))
       + (SELECT COUNT(*) FROM (SELECT * FROM layer EXCEPT DISTINCT SELECT * FROM canon))
) = 0 AS 'V2: V_DASH_EXECUTIVE_V2_DAILY разошёлся с V_DASH_FINANCE_CORRECTED_DAILY';

ASSERT (
  SELECT COUNTIF(NOT (ads_billing_covered IS NULL AND ad_spend_financial_rub IS NULL AND tariff_option_minimum_payment_rub IS NULL AND account_level_total_corrected_rub IS NULL))
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_EXECUTIVE_V2_DAILY`
  WHERE day NOT IN (SELECT day FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_FINANCE_CORRECTED_DAILY`)
) = 0 AS 'V3: в слое есть значения V_DASH_FINANCE_CORRECTED_DAILY на днях, которых нет в каноническом view';

-- V2 V_DASH_EXECUTIVE_ECONOMICS_DAILY: 12 колонок
ASSERT (
  WITH canon AS (
    SELECT day AS day,
         period_result_eligible,
         product_cogs_covered,
         after_product_cogs_eligible,
         period_result_pre_cogs_corrected_rub,
         revenue_base_period_result_rub,
         product_cogs_rub,
         period_result_after_product_cogs_rub,
         revenue_base_after_product_cogs_rub,
         executive_incomplete_day,
         executive_provisional_day,
         executive_final_day,
         exec_provisional_ads_billing_day
    FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_EXECUTIVE_ECONOMICS_DAILY`
  ),
  layer AS (
    SELECT day,
         period_result_eligible,
         product_cogs_covered,
         after_product_cogs_eligible,
         period_result_pre_cogs_corrected_rub,
         revenue_base_period_result_rub,
         product_cogs_rub,
         period_result_after_product_cogs_rub,
         revenue_base_after_product_cogs_rub,
         executive_incomplete_day,
         executive_provisional_day,
         executive_final_day,
         exec_provisional_ads_billing_day
    FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_EXECUTIVE_V2_DAILY`
    WHERE day IN (SELECT day FROM canon)
  )
  SELECT (SELECT COUNT(*) FROM (SELECT * FROM canon EXCEPT DISTINCT SELECT * FROM layer))
       + (SELECT COUNT(*) FROM (SELECT * FROM layer EXCEPT DISTINCT SELECT * FROM canon))
) = 0 AS 'V2: V_DASH_EXECUTIVE_V2_DAILY разошёлся с V_DASH_EXECUTIVE_ECONOMICS_DAILY';

ASSERT (
  SELECT COUNTIF(NOT (period_result_eligible IS NULL AND product_cogs_covered IS NULL AND after_product_cogs_eligible IS NULL AND period_result_pre_cogs_corrected_rub IS NULL AND revenue_base_period_result_rub IS NULL AND product_cogs_rub IS NULL AND period_result_after_product_cogs_rub IS NULL AND revenue_base_after_product_cogs_rub IS NULL))
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_EXECUTIVE_V2_DAILY`
  WHERE day NOT IN (SELECT day FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_EXECUTIVE_ECONOMICS_DAILY`)
) = 0 AS 'V3: в слое есть значения V_DASH_EXECUTIVE_ECONOMICS_DAILY на днях, которых нет в каноническом view';

-- V2 V_DASH_SETTLEMENT_DAILY: 29 колонок
ASSERT (
  WITH canon AS (
    SELECT day AS day,
         settlement_eligible,
         settlement_price_chain_covered,
         settlement_seller_price_rub,
         settlement_spp_rub,
         settlement_buyer_paid_rub,
         settlement_wb_remuneration_rub,
         settlement_wb_remuneration_vat_rub,
         settlement_acquiring_rub,
         settlement_pvz_reward_rub,
         settlement_price_chain_rounding_rub,
         settlement_goods_other_operations_rub,
         settlement_goods_rub,
         post_realization_deductions_rub,
         wb_payout_rub,
         post_sale_deductions_rub,
         post_sale_wb_promotion_documents_rub,
         post_sale_logistics_rub,
         post_sale_storage_rub,
         post_sale_tariff_option_minimum_payment_rub,
         post_sale_utilization_rub,
         post_sale_transit_rub,
         post_sale_penalty_rub,
         post_sale_acceptance_rub,
         post_sale_force_majeure_rub,
         post_sale_review_points_refund_rub,
         post_sale_unclassified_deductions_rub,
         post_sale_loyalty_program_rub,
         post_sale_loyalty_points_rub,
         post_sale_other_operations_rub
    FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_SETTLEMENT_DAILY`
  ),
  layer AS (
    SELECT day,
         settlement_eligible,
         settlement_price_chain_covered,
         settlement_seller_price_rub,
         settlement_spp_rub,
         settlement_buyer_paid_rub,
         settlement_wb_remuneration_rub,
         settlement_wb_remuneration_vat_rub,
         settlement_acquiring_rub,
         settlement_pvz_reward_rub,
         settlement_price_chain_rounding_rub,
         settlement_goods_other_operations_rub,
         settlement_goods_rub,
         post_realization_deductions_rub,
         wb_payout_rub,
         post_sale_deductions_rub,
         post_sale_wb_promotion_documents_rub,
         post_sale_logistics_rub,
         post_sale_storage_rub,
         post_sale_tariff_option_minimum_payment_rub,
         post_sale_utilization_rub,
         post_sale_transit_rub,
         post_sale_penalty_rub,
         post_sale_acceptance_rub,
         post_sale_force_majeure_rub,
         post_sale_review_points_refund_rub,
         post_sale_unclassified_deductions_rub,
         post_sale_loyalty_program_rub,
         post_sale_loyalty_points_rub,
         post_sale_other_operations_rub
    FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_EXECUTIVE_V2_DAILY`
    WHERE day IN (SELECT day FROM canon)
  )
  SELECT (SELECT COUNT(*) FROM (SELECT * FROM canon EXCEPT DISTINCT SELECT * FROM layer))
       + (SELECT COUNT(*) FROM (SELECT * FROM layer EXCEPT DISTINCT SELECT * FROM canon))
) = 0 AS 'V2: V_DASH_EXECUTIVE_V2_DAILY разошёлся с V_DASH_SETTLEMENT_DAILY';

ASSERT (
  SELECT COUNTIF(NOT (settlement_eligible IS NULL AND settlement_price_chain_covered IS NULL AND settlement_seller_price_rub IS NULL AND settlement_spp_rub IS NULL AND settlement_buyer_paid_rub IS NULL AND settlement_wb_remuneration_rub IS NULL AND settlement_wb_remuneration_vat_rub IS NULL AND settlement_acquiring_rub IS NULL AND settlement_pvz_reward_rub IS NULL AND settlement_price_chain_rounding_rub IS NULL AND settlement_goods_other_operations_rub IS NULL AND settlement_goods_rub IS NULL AND post_realization_deductions_rub IS NULL AND wb_payout_rub IS NULL AND post_sale_deductions_rub IS NULL AND post_sale_wb_promotion_documents_rub IS NULL AND post_sale_logistics_rub IS NULL AND post_sale_storage_rub IS NULL AND post_sale_tariff_option_minimum_payment_rub IS NULL AND post_sale_utilization_rub IS NULL AND post_sale_transit_rub IS NULL AND post_sale_penalty_rub IS NULL AND post_sale_acceptance_rub IS NULL AND post_sale_force_majeure_rub IS NULL AND post_sale_review_points_refund_rub IS NULL AND post_sale_unclassified_deductions_rub IS NULL AND post_sale_loyalty_program_rub IS NULL AND post_sale_loyalty_points_rub IS NULL AND post_sale_other_operations_rub IS NULL))
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_EXECUTIVE_V2_DAILY`
  WHERE day NOT IN (SELECT day FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_SETTLEMENT_DAILY`)
) = 0 AS 'V3: в слое есть значения V_DASH_SETTLEMENT_DAILY на днях, которых нет в каноническом view';

-- V2 V_DASH_EXECUTIVE_BREAKDOWN_DAILY: 4 колонок
ASSERT (
  WITH canon AS (
    SELECT day AS day,
         logistics_sold_rub,
         logistics_cancellation_rub,
         logistics_cancel_to_customer_rub,
         logistics_cancel_from_customer_rub
    FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_EXECUTIVE_BREAKDOWN_DAILY`
  ),
  layer AS (
    SELECT day,
         logistics_sold_rub,
         logistics_cancellation_rub,
         logistics_cancel_to_customer_rub,
         logistics_cancel_from_customer_rub
    FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_EXECUTIVE_V2_DAILY`
    WHERE day IN (SELECT day FROM canon)
  )
  SELECT (SELECT COUNT(*) FROM (SELECT * FROM canon EXCEPT DISTINCT SELECT * FROM layer))
       + (SELECT COUNT(*) FROM (SELECT * FROM layer EXCEPT DISTINCT SELECT * FROM canon))
) = 0 AS 'V2: V_DASH_EXECUTIVE_V2_DAILY разошёлся с V_DASH_EXECUTIVE_BREAKDOWN_DAILY';

ASSERT (
  SELECT COUNTIF(NOT (logistics_sold_rub IS NULL AND logistics_cancellation_rub IS NULL AND logistics_cancel_to_customer_rub IS NULL AND logistics_cancel_from_customer_rub IS NULL))
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_EXECUTIVE_V2_DAILY`
  WHERE day NOT IN (SELECT day FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_EXECUTIVE_BREAKDOWN_DAILY`)
) = 0 AS 'V3: в слое есть значения V_DASH_EXECUTIVE_BREAKDOWN_DAILY на днях, которых нет в каноническом view';

-- V2 V_DASH_BUYOUT_COHORT_DAILY: 8 колонок
ASSERT (
  WITH canon AS (
    SELECT cohort_date AS day,
         cohort_orders,
         buyout_orders,
         cancelled_orders,
         resolved_orders,
         unresolved_orders,
         conflict_orders,
         returned_after_buyout_orders,
         cohort_provisional_day
    FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_BUYOUT_COHORT_DAILY`
  ),
  layer AS (
    SELECT day,
         cohort_orders,
         buyout_orders,
         cancelled_orders,
         resolved_orders,
         unresolved_orders,
         conflict_orders,
         returned_after_buyout_orders,
         cohort_provisional_day
    FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_EXECUTIVE_V2_DAILY`
    WHERE day IN (SELECT day FROM canon)
  )
  SELECT (SELECT COUNT(*) FROM (SELECT * FROM canon EXCEPT DISTINCT SELECT * FROM layer))
       + (SELECT COUNT(*) FROM (SELECT * FROM layer EXCEPT DISTINCT SELECT * FROM canon))
) = 0 AS 'V2: V_DASH_EXECUTIVE_V2_DAILY разошёлся с V_DASH_BUYOUT_COHORT_DAILY';

ASSERT (
  SELECT COUNTIF(NOT (cohort_orders IS NULL AND buyout_orders IS NULL AND cancelled_orders IS NULL AND resolved_orders IS NULL AND unresolved_orders IS NULL AND conflict_orders IS NULL AND returned_after_buyout_orders IS NULL AND cohort_provisional_day IS NULL))
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_EXECUTIVE_V2_DAILY`
  WHERE day NOT IN (SELECT cohort_date FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_BUYOUT_COHORT_DAILY`)
) = 0 AS 'V3: в слое есть значения V_DASH_BUYOUT_COHORT_DAILY на днях, которых нет в каноническом view';

ASSERT (
  SELECT LOGICAL_AND(l.source_data_through IS NOT DISTINCT FROM f.data_as_of_min
                     AND l.mart_built_at IS NOT DISTINCT FROM f.mart_built_at)
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_EXECUTIVE_V2_DAILY` l CROSS JOIN `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_FRESHNESS_HEADER` f
) AS 'V4: свежесть слоя не совпала с V_DASH_FRESHNESS_HEADER (витрина пересобрана после слоя?)';

ASSERT (
  SELECT LOGICAL_AND(last_build_status = 'SUCCESS' AND layer_built_at IS NOT NULL
                     AND layer_built_at <= CURRENT_TIMESTAMP())
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_EXECUTIVE_V2_DAILY`
) AS 'V5: последняя сборка слоя не SUCCESS';

ASSERT (
  SELECT LOGICAL_AND(NOT is_running) FROM `project-fa311fc0-4d87-4781-986.wb_mart._EXECUTIVE_V2_BUILD_LOCK`
) AS 'V5: замок сборки не освобождён';
