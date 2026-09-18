SELECT IFNULL(CAST(nm_id AS STRING),'TOTAL') k, product_name_short, data_status, ARRAY_TO_STRING(data_status_reasons,' | ') reasons,
 cur_orders_gross_units, cur_orders_cancelled_units, ROUND(cur_cancellation_rate*100,2) canc_pct, cur_buyout_units,
 cur_cohort_orders, cur_cohort_buyout_orders, cur_cohort_cancelled_orders, cur_cohort_unresolved_orders, ROUND(cur_cohort_buyout_rate*100,2) cohort_pct,
 ROUND(cur_avg_seller_price_orders_rub,2) p_orders, ROUND(cur_avg_seller_price_buyouts_rub,2) p_buyouts, ROUND(cur_avg_buyer_paid_price_rub,2) p_buyer, ROUND(cur_credited_per_unit_rub,2) credited_unit,
 cur_fin_seller_price_rub, cur_fin_spp_rub, ROUND(cur_chain_wb_remuneration_rub,2) vw, cur_chain_wb_remuneration_vat_rub vat, cur_chain_acquiring_rub acq, ROUND(cur_chain_pvz_reward_rub,2) pvz, cur_credited_for_goods_rub credited,
 cur_logistics_rub, cur_logistics_sold_rub, cur_logistics_cancel_to_customer_rub, cur_logistics_cancel_from_customer_rub, ROUND(cur_storage_sku_rub,2) storage, cur_ads_attributed_rub ads, ROUND(cur_drr*100,2) drr_pct,
 ROUND(cur_contribution_before_cogs_rub,2) before_cogs, ROUND(cur_cogs_rub,2) cogs, ROUND(cur_contribution_after_cogs_rub,2) after_cogs, ROUND(cur_contribution_margin_after_cogs*100,2) margin_pct, ROUND(cur_contribution_after_cogs_per_buyout_rub,2) per_buyout,
 cur_orders_reliable, low_sample, orders_comparable, economics_comparable, ROUND(orders_delta_pct*100,1) orders_d_pct, ROUND(avg_seller_price_orders_delta_pct*100,1) price_d_pct, ROUND(drr_delta_pp,2) drr_d_pp, ROUND(cohort_buyout_rate_delta_pp,2) coh_d_pp
FROM `project-fa311fc0-4d87-4781-986.wb_mart.TVF_SKU_PERFORMANCE_V2_PERIOD`(DATE '2026-09-01', DATE '2026-09-14')
WHERE nm_id IN (252442517, 305101361, 593111986, 567668635) OR nm_id IS NULL ORDER BY k
