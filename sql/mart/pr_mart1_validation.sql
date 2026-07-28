-- ============================================================================
-- PR-Mart1 — read-only контрольные запросы (для аудитора / повторной сверки).
-- Ничего не создаёт. Прогоняются на wb_raw. Ожидаемые числа — в комментариях
-- (снимок 2026-07-28; на живых данных счётчики растут, инварианты сохраняются).
-- ============================================================================

-- 1) ORDERS: ключ srid уникален; parse price/qty; каст nm_id (ожидание: fail=0).
SELECT COUNT(*) rows_all, COUNT(DISTINCT srid) distinct_srid,     -- 3800 = 3800
  COUNTIF(price_with_disc IS NOT NULL AND TRIM(price_with_disc)<>'' AND SAFE_CAST(REPLACE(price_with_disc,',','.') AS NUMERIC) IS NULL) price_fail,  -- 0
  COUNTIF(quantity IS NOT NULL AND TRIM(quantity)<>'' AND SAFE_CAST(quantity AS INT64) IS NULL) qty_fail,          -- 0
  COUNTIF(wb_nm_id IS NOT NULL AND TRIM(wb_nm_id)<>'' AND SAFE_CAST(wb_nm_id AS INT64) IS NULL) nm_cast_fail        -- 0
FROM `wb_raw.V_WB_ORDERS`;

-- 2) SALES: ключ sale_id уникален (деньги уже NUMERIC).
SELECT COUNT(*) rows_all, COUNT(DISTINCT sale_id) distinct_saleid  -- 3558 = 3558
FROM `wb_raw.V_WB_SALES_RETURNS`;

-- 3) FINANCE: ключ report_id#rrd_id уникален; 13 денежных колонок parse=0; каст nm=0.
--    Контракт ключа (аудит #2/#3): части ключа NOT NULL/empty (key_parts_fail=0), distinct по CONCAT БЕЗ IFNULL.
SELECT COUNT(*) rows_all,
  COUNT(DISTINCT CONCAT(report_id,'#',rrd_id)) distinct_key,  -- = rows_all
  COUNTIF(report_id IS NULL OR TRIM(report_id)='' OR rrd_id IS NULL OR TRIM(rrd_id)='') key_parts_fail,  -- 0
  COUNTIF(wb_nm_id IS NOT NULL AND TRIM(wb_nm_id)<>'' AND SAFE_CAST(wb_nm_id AS INT64) IS NULL) nm_cast_fail,       -- 0
  (COUNTIF(sale_amount IS NOT NULL AND TRIM(sale_amount)<>'' AND SAFE_CAST(REPLACE(sale_amount,',','.') AS NUMERIC) IS NULL)
  +COUNTIF(return_amount_rub IS NOT NULL AND TRIM(return_amount_rub)<>'' AND SAFE_CAST(REPLACE(return_amount_rub,',','.') AS NUMERIC) IS NULL)
  +COUNTIF(for_pay IS NOT NULL AND TRIM(for_pay)<>'' AND SAFE_CAST(REPLACE(for_pay,',','.') AS NUMERIC) IS NULL)
  +COUNTIF(commission_amount IS NOT NULL AND TRIM(commission_amount)<>'' AND SAFE_CAST(REPLACE(commission_amount,',','.') AS NUMERIC) IS NULL)
  +COUNTIF(logistics_amount IS NOT NULL AND TRIM(logistics_amount)<>'' AND SAFE_CAST(REPLACE(logistics_amount,',','.') AS NUMERIC) IS NULL)
  +COUNTIF(storage_fee IS NOT NULL AND TRIM(storage_fee)<>'' AND SAFE_CAST(REPLACE(storage_fee,',','.') AS NUMERIC) IS NULL)
  +COUNTIF(deduction IS NOT NULL AND TRIM(deduction)<>'' AND SAFE_CAST(REPLACE(deduction,',','.') AS NUMERIC) IS NULL)
  +COUNTIF(penalty IS NOT NULL AND TRIM(penalty)<>'' AND SAFE_CAST(REPLACE(penalty,',','.') AS NUMERIC) IS NULL)
  +COUNTIF(acceptance IS NOT NULL AND TRIM(acceptance)<>'' AND SAFE_CAST(REPLACE(acceptance,',','.') AS NUMERIC) IS NULL)
  +COUNTIF(acquiring_fee IS NOT NULL AND TRIM(acquiring_fee)<>'' AND SAFE_CAST(REPLACE(acquiring_fee,',','.') AS NUMERIC) IS NULL)
  +COUNTIF(additional_payment IS NOT NULL AND TRIM(additional_payment)<>'' AND SAFE_CAST(REPLACE(additional_payment,',','.') AS NUMERIC) IS NULL)
  +COUNTIF(compensation_amount IS NOT NULL AND TRIM(compensation_amount)<>'' AND SAFE_CAST(REPLACE(compensation_amount,',','.') AS NUMERIC) IS NULL)
  +COUNTIF(other_amount IS NOT NULL AND TRIM(other_amount)<>'' AND SAFE_CAST(REPLACE(other_amount,',','.') AS NUMERIC) IS NULL)
  ) money_parse_fail                                                                -- 0
FROM `wb_raw.V_WB_FINANCE_CANONICAL`;

-- 4) STOCKS: последний COMPLETE-снапшот дня → грейн snap_date×nm×склад уникален.
WITH pick AS (
  SELECT snapshot_id, SAFE.PARSE_DATE('%Y-%m-%d', period_to) snapshot_date
  FROM `wb_raw.WB_STOCKS_SNAPSHOTS` WHERE status='COMPLETE' AND period_to IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY period_to ORDER BY started_at DESC)=1)
SELECT COUNT(*) rows_all, COUNT(DISTINCT FORMAT('%t|%t|%t',snapshot_date,nm_id,warehouse_id)) dk  -- 1509 = 1509
FROM (SELECT p.snapshot_date, r.nm_id, r.warehouse_id FROM pick p JOIN `wb_raw.RAW_WB_STOCKS` r USING(snapshot_id) GROUP BY 1,2,3);

-- 5) ADS_SKU: грейн date×advert×nm (SUM по appType) + ПОЛНЫЙ parse-QC.
--    Дата — тем же алгоритмом, что в процедуре: SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(date,1,10)).
--    Все *_fail — ожидание 0; rows_all=dk=4962.
SELECT
  (SELECT COUNT(*) FROM (SELECT SAFE.PARSE_DATE('%Y-%m-%d',SUBSTR(`date`,1,10)) d, SAFE_CAST(advertId AS INT64) a, SAFE_CAST(nmId AS INT64) n FROM `wb_raw.V_ADV_CAMPAIGN_STATS` GROUP BY 1,2,3)) rows_all,
  (SELECT COUNT(DISTINCT FORMAT('%t|%t|%t', SAFE.PARSE_DATE('%Y-%m-%d',SUBSTR(`date`,1,10)), SAFE_CAST(advertId AS INT64), SAFE_CAST(nmId AS INT64))) FROM `wb_raw.V_ADV_CAMPAIGN_STATS`) dk,
  (SELECT COUNTIF(`date` IS NOT NULL AND TRIM(`date`)<>'' AND SAFE.PARSE_DATE('%Y-%m-%d',SUBSTR(`date`,1,10)) IS NULL) FROM `wb_raw.V_ADV_CAMPAIGN_STATS`) date_fail,
  (SELECT COUNTIF(advertId IS NOT NULL AND TRIM(advertId)<>'' AND SAFE_CAST(advertId AS INT64) IS NULL) FROM `wb_raw.V_ADV_CAMPAIGN_STATS`) adv_fail,
  (SELECT COUNTIF(nmId IS NOT NULL AND TRIM(nmId)<>'' AND SAFE_CAST(nmId AS INT64) IS NULL) FROM `wb_raw.V_ADV_CAMPAIGN_STATS`) nm_fail,
  (SELECT COUNTIF(views IS NOT NULL AND TRIM(views)<>'' AND SAFE_CAST(views AS INT64) IS NULL) FROM `wb_raw.V_ADV_CAMPAIGN_STATS`) views_fail,
  (SELECT COUNTIF(clicks IS NOT NULL AND TRIM(clicks)<>'' AND SAFE_CAST(clicks AS INT64) IS NULL) FROM `wb_raw.V_ADV_CAMPAIGN_STATS`) clicks_fail,
  (SELECT COUNTIF(orders IS NOT NULL AND TRIM(orders)<>'' AND SAFE_CAST(orders AS INT64) IS NULL) FROM `wb_raw.V_ADV_CAMPAIGN_STATS`) orders_fail,
  (SELECT COUNTIF(`sum` IS NOT NULL AND TRIM(`sum`)<>'' AND SAFE_CAST(REPLACE(`sum`,',','.') AS NUMERIC) IS NULL) FROM `wb_raw.V_ADV_CAMPAIGN_STATS`) sum_fail;

-- 6) ADS_COSTS: грейн date×advert уникален; Σcosts ≥ Σstats (инвариант рекламы §8.3).
SELECT
  (SELECT COUNT(DISTINCT FORMAT('%t|%t', SAFE.PARSE_DATE('%Y-%m-%d',SUBSTR(updDate,1,10)), SAFE_CAST(advertId AS INT64))) FROM `wb_raw.V_ADV_COSTS`) grain_costs,  -- 1558
  (SELECT ROUND(SUM(SAFE_CAST(REPLACE(updSum,',','.') AS NUMERIC)),2) FROM `wb_raw.V_ADV_COSTS`) total_costs,          -- 526082
  (SELECT ROUND(SUM(SAFE_CAST(REPLACE(`sum`,',','.') AS NUMERIC)),2) FROM `wb_raw.V_ADV_CAMPAIGN_STATS`) total_stats;  -- 446090.87  (costs ≥ stats ✓)

-- 7) ФИЗИКА финальных FACT (запускать ПОСЛЕ sp_bootstrap_facts).
--    Ожидание: part_cols=1 у каждой; clust_cols = 2/2/2/3/2/1 (ORDERS/SALES/STOCKS/FINANCE/ADS_SKU/ADS_COSTS).
SELECT table_name,
  COUNTIF(is_partitioning_column='YES')            AS part_cols,
  COUNTIF(clustering_ordinal_position IS NOT NULL) AS clust_cols
FROM `wb_mart.INFORMATION_SCHEMA.COLUMNS`
WHERE table_name IN ('FACT_ORDERS','FACT_SALES','FACT_STOCKS_SNAPSHOT','FACT_FINANCE','FACT_ADS_SKU_DAILY','FACT_ADS_COSTS_DAILY')
GROUP BY table_name ORDER BY table_name;
