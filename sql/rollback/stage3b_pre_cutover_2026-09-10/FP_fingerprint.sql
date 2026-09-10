-- ============================================================================
-- EVETIS · Stage B cutover · ОТПЕЧАТКИ СОСТОЯНИЯ (read-only)
-- Снимаются в B0, после B8/B9, после J и после любого отката — и сравниваются.
-- Отпечаток = BIT_XOR(FARM_FINGERPRINT(строка без служебных колонок)): не зависит
-- от порядка строк и меняется от любого изменения любого значения.
--
-- 🔴 MART_SKU_DAILY снимается ТОЛЬКО по 57 колонкам, существовавшим до cutover.
--    Stage 3B добавляет колонки аддитивно; если бы отпечаток брался по «*», он
--    менялся бы от самого факта добавления, и сравнение ничего бы не доказывало.
--    Служебные колонки исключены: mart_run_id, built_at, build_as_of_date.
-- ============================================================================

-- FP-1. Таблицы FACT/MART целиком.
SELECT 'FACT_ORDERS' AS obj, COUNT(*) AS rows_, BIT_XOR(FARM_FINGERPRINT(TO_JSON_STRING((SELECT AS STRUCT t.* EXCEPT(mart_run_id, built_at))))) AS fp FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ORDERS` t
UNION ALL SELECT 'FACT_SALES', COUNT(*), BIT_XOR(FARM_FINGERPRINT(TO_JSON_STRING((SELECT AS STRUCT t.* EXCEPT(mart_run_id, built_at))))) FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_SALES` t
UNION ALL SELECT 'FACT_FINANCE', COUNT(*), BIT_XOR(FARM_FINGERPRINT(TO_JSON_STRING((SELECT AS STRUCT t.* EXCEPT(mart_run_id, built_at))))) FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_FINANCE` t
UNION ALL SELECT 'FACT_STOCKS_SNAPSHOT', COUNT(*), BIT_XOR(FARM_FINGERPRINT(TO_JSON_STRING((SELECT AS STRUCT t.* EXCEPT(mart_run_id, built_at))))) FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_STOCKS_SNAPSHOT` t
UNION ALL SELECT 'FACT_ADS_SKU_DAILY', COUNT(*), BIT_XOR(FARM_FINGERPRINT(TO_JSON_STRING((SELECT AS STRUCT t.* EXCEPT(mart_run_id, built_at))))) FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ADS_SKU_DAILY` t
UNION ALL SELECT 'FACT_ADS_COSTS_DAILY', COUNT(*), BIT_XOR(FARM_FINGERPRINT(TO_JSON_STRING((SELECT AS STRUCT t.* EXCEPT(mart_run_id, built_at))))) FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ADS_COSTS_DAILY` t
UNION ALL SELECT 'MART_SKU_DAILY[57 legacy cols]', COUNT(*), BIT_XOR(FARM_FINGERPRINT(TO_JSON_STRING(STRUCT(day,nm_id,ad_spend,views,clicks,ad_orders_raw,ads_revenue_raw_rub,ads_revenue_dedup_estimate_rub,ad_orders_dedup_estimate,orders_qty,orders_rub,canceled_qty,canceled_rub,buyouts_qty,buyouts_rub,sales_for_pay_operational,returns_qty,returns_rub,wb_reward_cost_positive,logistics_cost_positive,marketplace_fee_rub,finance_for_pay_accounting,ctr,cpm,cpc,cpo_attributed,blended_cpo,drr_orders,drr_buyouts,roas,acos,hybrid_day_contribution_pre_cogs,settlement_day_contribution_pre_cogs,ad_spend_7d,ad_spend_14d,ads_revenue_raw_7d,ads_revenue_raw_14d,ads_revenue_dedup_estimate_7d,ads_revenue_dedup_estimate_14d,ad_orders_raw_7d,ad_orders_raw_14d,ad_orders_dedup_estimate_7d,ad_orders_dedup_estimate_14d,buyouts_rub_7d,buyouts_rub_14d,orders_rub_7d,orders_rub_14d,orders_qty_7d,orders_qty_14d,drr_buyouts_7d,drr_buyouts_14d,roas_7d,roas_14d,blended_cpo_7d,blended_cpo_14d,ads_activity_max_date,ads_activity_lagged)))) FROM `project-fa311fc0-4d87-4781-986.wb_mart.MART_SKU_DAILY`
ORDER BY obj;

-- FP-2. MART по суткам — чтобы локализовать любое расхождение FP-1 до даты.
SELECT day, COUNT(*) AS rows_, BIT_XOR(FARM_FINGERPRINT(TO_JSON_STRING(STRUCT(day,nm_id,ad_spend,views,clicks,ad_orders_raw,ads_revenue_raw_rub,ads_revenue_dedup_estimate_rub,ad_orders_dedup_estimate,orders_qty,orders_rub,canceled_qty,canceled_rub,buyouts_qty,buyouts_rub,sales_for_pay_operational,returns_qty,returns_rub,wb_reward_cost_positive,logistics_cost_positive,marketplace_fee_rub,finance_for_pay_accounting,ctr,cpm,cpc,cpo_attributed,blended_cpo,drr_orders,drr_buyouts,roas,acos,hybrid_day_contribution_pre_cogs,settlement_day_contribution_pre_cogs,ad_spend_7d,ad_spend_14d,ads_revenue_raw_7d,ads_revenue_raw_14d,ads_revenue_dedup_estimate_7d,ads_revenue_dedup_estimate_14d,ad_orders_raw_7d,ad_orders_raw_14d,ad_orders_dedup_estimate_7d,ad_orders_dedup_estimate_14d,buyouts_rub_7d,buyouts_rub_14d,orders_rub_7d,orders_rub_14d,orders_qty_7d,orders_qty_14d,drr_buyouts_7d,drr_buyouts_14d,roas_7d,roas_14d,blended_cpo_7d,blended_cpo_14d,ads_activity_max_date,ads_activity_lagged)))) AS fp,
       ROUND(SUM(ad_spend),2) AS ad_spend, ROUND(SUM(orders_rub),2) AS orders_rub,
       ROUND(SUM(hybrid_day_contribution_pre_cogs),2) AS hybrid_contrib
FROM `project-fa311fc0-4d87-4781-986.wb_mart.MART_SKU_DAILY` GROUP BY day ORDER BY day;

-- FP-3. Биллинг по суткам — ровно здесь Фаза B обязана дать −300 ₽ на двух датах.
SELECT `date`, COUNT(*) AS rows_, ROUND(SUM(actual_spend_rub),2) AS rub
FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ADS_COSTS_DAILY` GROUP BY `date` ORDER BY `date`;

-- FP-4. Водяные знаки RAW: если между двумя снятиями они сдвинулись, расхождение
--       FACT/MART на соответствующих датах объясняется новыми данными, а не cutover.
SELECT table_id, row_count, TIMESTAMP_MILLIS(last_modified_time) AS last_modified
FROM `project-fa311fc0-4d87-4781-986.wb_raw.__TABLES__`
WHERE table_id IN ('RAW_WB_ORDERS','RAW_WB_SALES_RETURNS','RAW_WB_FINANCE','RAW_WB_STOCKS','WB_STOCKS_SNAPSHOTS',
                   'RAW_WB_ADV_CAMPAIGN_STATS','RAW_WB_ADV_COSTS','RAW_WB_ADV_COSTS_RUNS')
ORDER BY table_id;

-- FP-5. Схемы — число и список колонок.
SELECT table_name, COUNT(*) AS n_cols, TO_HEX(SHA256(STRING_AGG(column_name, ',' ORDER BY ordinal_position))) AS cols_sha
FROM `project-fa311fc0-4d87-4781-986.wb_mart.INFORMATION_SCHEMA.COLUMNS`
WHERE table_name IN ('FACT_ORDERS','FACT_SALES','FACT_FINANCE','FACT_STOCKS_SNAPSHOT','FACT_ADS_SKU_DAILY',
                     'FACT_ADS_COSTS_DAILY','MART_SKU_DAILY','FACT_ADS_SPEND_ALLOC_DAILY','FACT_ADS_SPEND_UNALLOC_DAILY')
GROUP BY table_name ORDER BY table_name;
