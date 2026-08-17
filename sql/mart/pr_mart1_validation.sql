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
  COUNT(DISTINCT rrd_id) distinct_rrd,                        -- = rows_all (PR-A/A1: инвариант CANONICAL)
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

-- 3a) FINANCE PR-A/A1: инвариант CANONICAL — ОДНА строка на rrd_id.
--     finance_row_key = report_id#rrd_id, поэтому проверка 3) пропускает пару
--     DAILY+WEEKLY по одному rrd_id. Здесь — fail-closed на сам rrd_id.
--     Спека: docs/FINANCE_PR_A_INGESTION_INTEGRITY_2026-08-11.md §2 A1.
ASSERT (
  SELECT COUNT(*) - COUNT(DISTINCT rrd_id)
  FROM `wb_raw.V_WB_FINANCE_CANONICAL`
) = 0 AS 'PR-A: CANONICAL содержит дубли rrd_id — дедуп weekly/daily не сработал';

-- 4) STOCKS: последний COMPLETE-снапшот дня → грейн snap_date×nm×склад уникален.
--    С 17.08.2026 ключ склада = warehouse_key (STRING), а не warehouse_id: WB обезличил
--    склад отгрузки, лоадер с 16.08 пишет warehouse_id = NULL и сентинел в warehouse_code.
--    Ожидание на 17.08.2026: 5637 = 5637, key_null = 0 (замер 30 суток истории).
--    Историческая отметка на старом ключе (28.07.2026): 1509 = 1509.
WITH pick AS (
  SELECT snapshot_id, SAFE.PARSE_DATE('%Y-%m-%d', period_to) snapshot_date
  FROM `wb_raw.WB_STOCKS_SNAPSHOTS` WHERE status='COMPLETE' AND period_to IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY period_to ORDER BY started_at DESC)=1)
SELECT COUNT(*) rows_all,
       COUNT(DISTINCT FORMAT('%t|%t|%t',snapshot_date,nm_id,warehouse_key)) dk,
       COUNTIF(warehouse_key IS NULL OR TRIM(warehouse_key)='') key_null
FROM (SELECT p.snapshot_date, r.nm_id,
             COALESCE(NULLIF(r.warehouse_code,''), CAST(r.warehouse_id AS STRING)) warehouse_key
      FROM pick p JOIN `wb_raw.RAW_WB_STOCKS` r USING(snapshot_id) GROUP BY 1,2,3);

-- 4a) STOCKS fail-closed: контроль эпох ключа. До 16.08 ключ приходит из warehouse_id,
--     с 16.08 — из warehouse_code. Обе колонки одновременно пустыми быть не должны
--     НИКОГДА: это ровно тот класс, что положил витрину 17.08 — источник перестал
--     заполнять колонку, из которой собирается грейн, и заметил это потребитель.
--     Замер 17.08: 0 на всей RAW (5 907 строк, 36 снапшотов, все COMPLETE).
ASSERT (
  SELECT COUNTIF(warehouse_id IS NULL AND (warehouse_code IS NULL OR TRIM(warehouse_code) = ''))
  FROM `wb_raw.RAW_WB_STOCKS`
) = 0 AS 'STOCKS: warehouse_id and warehouse_code both empty';

--     Диагностика к 4a — разбивка по датам, чтобы видеть границу эпох (и что она одна).
SELECT _snapshot_date,
       COUNT(*) rows_src,
       COUNTIF(warehouse_id IS NULL) wh_id_null,
       COUNTIF(warehouse_code IS NULL OR TRIM(warehouse_code)='') wh_code_empty,
       COUNTIF(warehouse_id IS NULL AND (warehouse_code IS NULL OR TRIM(warehouse_code)='')) both_empty
FROM `wb_raw.RAW_WB_STOCKS`
GROUP BY 1 ORDER BY 1 DESC LIMIT 10;  -- ожидание: ≥16.08 wh_id_null=rows_src, ≤15.08 wh_code_empty=rows_src

-- 4b) STOCKS: однозначность ANY_VALUE-колонок внутри warehouse_key — разбивка по колонкам.
--     В процедуре это два ASSERT (id отдельно, описательные вместе); здесь видно, какая
--     именно колонка сломалась. ⚠️ FORMAT('%t') обязателен — COUNT(DISTINCT) молча
--     игнорирует NULL, а группа {NULL, 5} это как раз недетерминированный ANY_VALUE.
--     Ожидание 17.08: все bad_* = 0, groups=5637, max_raw_rows_per_group=1.
WITH pick AS (
  SELECT snapshot_id, SAFE.PARSE_DATE('%Y-%m-%d', period_to) snapshot_date
  FROM `wb_raw.WB_STOCKS_SNAPSHOTS` WHERE status='COMPLETE' AND period_to IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY period_to ORDER BY started_at DESC)=1),
g AS (
  SELECT p.snapshot_date, r.nm_id,
         COALESCE(NULLIF(r.warehouse_code,''), CAST(r.warehouse_id AS STRING)) warehouse_key,
         COUNT(*) raw_rows,
         COUNT(DISTINCT FORMAT('%t', r.warehouse_id))     d_wh_id,
         COUNT(DISTINCT FORMAT('%t', r.warehouse_name))   d_wh_name,
         COUNT(DISTINCT FORMAT('%t', r.region_name))      d_region,
         COUNT(DISTINCT FORMAT('%t', r.internal_sku))     d_sku,
         COUNT(DISTINCT FORMAT('%t', r.sku_match_status)) d_sku_status
  FROM pick p JOIN `wb_raw.RAW_WB_STOCKS` r USING (snapshot_id)
  GROUP BY 1,2,3)
SELECT COUNT(*) groups, MAX(raw_rows) max_raw_rows_per_group,
       COUNTIF(d_wh_id>1) bad_warehouse_id, COUNTIF(d_wh_name>1) bad_warehouse_name,
       COUNTIF(d_region>1) bad_region_name, COUNTIF(d_sku>1) bad_internal_sku,
       COUNTIF(d_sku_status>1) bad_sku_match_status
FROM g;

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
  (SELECT COUNTIF(`sum` IS NOT NULL AND TRIM(`sum`)<>'' AND SAFE_CAST(REPLACE(`sum`,',','.') AS NUMERIC) IS NULL) FROM `wb_raw.V_ADV_CAMPAIGN_STATS`) sum_fail,
  (SELECT COUNTIF(sum_price IS NOT NULL AND TRIM(sum_price)<>'' AND SAFE_CAST(REPLACE(sum_price,',','.') AS NUMERIC) IS NULL) FROM `wb_raw.V_ADV_CAMPAIGN_STATS`) sum_price_fail;  -- Mart1.1: ожидание 0

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

-- 8) PR-Mart1.1 — новые поля FACT_ADS_SKU_DAILY (ПОСЛЕ пере-bootstrap).
--    Ожидание: raw == Σ source sum_price (dynamic acceptance); dedup <= raw; grain уникален; флаги — счётчики.
SELECT
  (SELECT COUNT(*) FROM `wb_mart.FACT_ADS_SKU_DAILY`) n_rows,
  (SELECT COUNT(DISTINCT FORMAT('%t|%t|%t',`date`,advert_id,nm_id)) FROM `wb_mart.FACT_ADS_SKU_DAILY`) dk,
  (SELECT ROUND(SUM(ads_revenue_raw_rub),2) FROM `wb_mart.FACT_ADS_SKU_DAILY`) rev_raw_fact,
  (SELECT ROUND(SUM(SAFE_CAST(REPLACE(sum_price,',','.') AS NUMERIC)),2) FROM `wb_raw.V_ADV_CAMPAIGN_STATS`) rev_raw_source, -- == rev_raw_fact
  (SELECT ROUND(SUM(ads_revenue_dedup_estimate_rub),2) FROM `wb_mart.FACT_ADS_SKU_DAILY`) rev_dedup_est, -- <= rev_raw_fact
  (SELECT SUM(ad_orders_raw) FROM `wb_mart.FACT_ADS_SKU_DAILY`) ord_raw,
  (SELECT SUM(ad_orders_dedup_estimate) FROM `wb_mart.FACT_ADS_SKU_DAILY`) ord_dedup_est,
  (SELECT COUNTIF(multitouch_ambiguous_flag) FROM `wb_mart.FACT_ADS_SKU_DAILY`) amb_groups,
  (SELECT COUNTIF(zero_revenue_multiorder_flag) FROM `wb_mart.FACT_ADS_SKU_DAILY`) zrm_groups,
  -- Mart1.1 границы estimate: ожидание 0 (0 ≤ dedup ≤ raw для revenue и orders)
  (SELECT COUNTIF(ads_revenue_dedup_estimate_rub < 0 OR ads_revenue_dedup_estimate_rub > ads_revenue_raw_rub
                OR ad_orders_dedup_estimate < 0 OR ad_orders_dedup_estimate > ad_orders_raw)
   FROM `wb_mart.FACT_ADS_SKU_DAILY`) estimate_bounds_violations;
