-- ============================================================================
-- PR-Mart2b — read-only контрольные запросы (аудитор / повторная сверка).
-- Прогоняются ПОСЛЕ CALL sp_build_mart_sku_daily(...). Ничего не создают.
-- Снимок приёмки 2026-07-30 (build_as_of=2026-07-30):
--   n_rows=6772; grain_dupes=0; gap_nms=0; nm=24; max_day=2026-07-30;
--   Δ(ad_spend/orders_qty/buyouts_rub/finance)=0 vs FACT/LONG_MAPPED (universe);
--   store 14д (16–29.07, universe): ad_spend 54 050, orders_qty 234, buyouts_rub 231 205.
-- ============================================================================

-- 1) Грейн + плотность spine + граница построения.
SELECT COUNT(*) n_rows,
  COUNT(*)-COUNT(DISTINCT FORMAT('%t|%t',day,nm_id)) grain_dupes,           -- 0
  (SELECT COUNTIF(cnt<>span) FROM (SELECT nm_id,COUNT(*) cnt,DATE_DIFF(MAX(day),MIN(day),DAY)+1 span
     FROM `wb_mart.MART_SKU_DAILY` GROUP BY nm_id)) gap_nms,                 -- 0
  COUNT(DISTINCT nm_id) nm,                                                  -- 24
  CAST(MAX(day) AS STRING) max_day,                                          -- build_as_of
  MAX(build_as_of_date)=MAX(day) boundary_ok                                 -- true
FROM `wb_mart.MART_SKU_DAILY`;

-- 2) Консервация операционки vs FACT (в рамках active-universe). Ожидание: все дельты 0.
WITH u AS (SELECT nm_id FROM `wb_raw.REF_SKU_MASTER` WHERE marketplace='WB' AND active=TRUE AND nm_id IS NOT NULL GROUP BY nm_id)
SELECT
  ROUND((SELECT SUM(ad_spend) FROM `wb_mart.MART_SKU_DAILY`)
       -(SELECT SUM(stats_spend_rub) FROM `wb_mart.FACT_ADS_SKU_DAILY` WHERE nm_id IN (SELECT nm_id FROM u)),2) d_ad_spend,   -- 0
  (SELECT SUM(orders_qty) FROM `wb_mart.MART_SKU_DAILY`)
  -(SELECT COUNTIF(NOT is_cancel) FROM `wb_mart.FACT_ORDERS` WHERE nm_id IN (SELECT nm_id FROM u)) d_orders_qty,             -- 0
  ROUND((SELECT SUM(buyouts_rub) FROM `wb_mart.MART_SKU_DAILY`)
       -(SELECT SUM(IF(NOT is_return,price_with_disc,0)) FROM `wb_mart.FACT_SALES` WHERE nm_id IN (SELECT nm_id FROM u)),2) d_buyouts_rub, -- 0
  ROUND((SELECT SUM(commission_cost_positive+logistics_cost_positive) FROM `wb_mart.MART_SKU_DAILY`)
       -(SELECT SUM(cost_amount_positive) FROM `wb_mart.V_WB_FINANCE_AMOUNTS_LONG_MAPPED`
         WHERE is_sku_row AND cost_category IN ('commission','logistics') AND nm_id IN (SELECT nm_id FROM u)),2) d_finance;  -- 0

-- 3) Store-срез (агрегат витрины по дням) — управленческая сводка 7д/14д.
SELECT wnd,
  ROUND(SUM(ad_spend)) ad_spend, SUM(orders_qty) orders_qty, SUM(canceled_qty) canceled_qty,
  SUM(buyouts_qty) buyouts_qty, ROUND(SUM(buyouts_rub)) buyouts_rub,
  ROUND(SAFE_DIVIDE(SUM(ad_spend),SUM(buyouts_rub))*100,1) drr_buyouts_pct,
  ROUND(SAFE_DIVIDE(SUM(ads_revenue_raw_rub),SUM(ad_spend)),2) roas
FROM (
  SELECT '7d(23-29.07)' wnd,* FROM `wb_mart.MART_SKU_DAILY` WHERE day BETWEEN '2026-07-23' AND '2026-07-29'
  UNION ALL SELECT '14d(16-29.07)',* FROM `wb_mart.MART_SKU_DAILY` WHERE day BETWEEN '2026-07-16' AND '2026-07-29')
GROUP BY wnd ORDER BY wnd;

-- 4) Rolling spot-check: на 29.07 rolling_7d == прямая сумма за 23–29.07 по nm (ожидание diff=0).
WITH direct AS (
  SELECT nm_id, ROUND(SUM(ad_spend),2) spend7_direct
  FROM `wb_mart.MART_SKU_DAILY` WHERE day BETWEEN '2026-07-23' AND '2026-07-29' GROUP BY nm_id)
SELECT COUNTIF(ABS(ROUND(m.ad_spend_7d,2)-d.spend7_direct)>0.01) rolling_mismatches   -- 0
FROM `wb_mart.MART_SKU_DAILY` m JOIN direct d USING(nm_id)
WHERE m.day='2026-07-29';

-- 5) Границы estimate <= raw (унаследовано из FACT, но проверяем на витрине): ожидание 0.
SELECT COUNTIF(ads_revenue_dedup_estimate_rub>ads_revenue_raw_rub OR ad_orders_dedup_estimate>ad_orders_raw) bounds_violations
FROM `wb_mart.MART_SKU_DAILY`;

-- 6) Топ nm по расходу 7д (ABC-превью для дашборда).
SELECT nm_id, ROUND(SUM(ad_spend)) spend, SUM(buyouts_qty) buyouts, ROUND(SUM(buyouts_rub)) buyouts_rub,
  ROUND(SAFE_DIVIDE(SUM(ad_spend),SUM(buyouts_rub))*100,1) drr_buyouts_pct,
  ROUND(SAFE_DIVIDE(SUM(ads_revenue_raw_rub),SUM(ad_spend)),2) roas
FROM `wb_mart.MART_SKU_DAILY` WHERE day BETWEEN '2026-07-23' AND '2026-07-29'
GROUP BY nm_id ORDER BY spend DESC LIMIT 10;
