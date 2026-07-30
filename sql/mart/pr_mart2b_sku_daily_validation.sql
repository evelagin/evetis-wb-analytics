-- ============================================================================
-- PR-Mart2b (PR#81) — read-only контрольные запросы. REV2 (аудит).
-- §1-§3 работают на FACT/LONG_MAPPED (можно ДО build). §4+ — на MART_SKU_DAILY (ПОСЛЕ CALL).
-- Снимок dry-run 2026-07-30 (build_as_of=2026-07-30):
--   max_required=2026-07-30; global_start=2024-09-05; unknown_window=0; universe_missing=0;
--   n_rows=6772; gap_nms=0; Δ(ad_spend/orders/buyouts/finance, date-bounded)=0.
-- ============================================================================

-- 1) fix #4: NULL-safe вычисление границ (MAX/MIN через UNNEST WHERE d IS NOT NULL). Ожидание: обе даты NOT NULL.
SELECT
  (SELECT MAX(d) FROM UNNEST([(SELECT MAX(order_date) FROM `wb_mart.FACT_ORDERS`),
     (SELECT MAX(sale_date) FROM `wb_mart.FACT_SALES`),(SELECT MAX(`date`) FROM `wb_mart.FACT_ADS_SKU_DAILY`)]) d WHERE d IS NOT NULL) max_required,  -- 2026-07-30
  (SELECT MIN(d) FROM UNNEST([(SELECT MIN(order_date) FROM `wb_mart.FACT_ORDERS`),(SELECT MIN(sale_date) FROM `wb_mart.FACT_SALES`),
     (SELECT MIN(`date`) FROM `wb_mart.FACT_ADS_SKU_DAILY`),(SELECT MIN(finance_date) FROM `wb_mart.FACT_FINANCE`)]) d WHERE d IS NOT NULL) global_start; -- 2024-09-05

-- 2) fix #1: неизвестные денежные пары в окне (cost_category IS NULL) — ожидание 0.
SELECT COUNTIF(cost_category IS NULL) unknown_window
FROM `wb_mart.V_WB_FINANCE_AMOUNTS_LONG_MAPPED` WHERE finance_date <= DATE '2026-07-30';   -- 0

-- 3) fix #2: полнота universe ДО публикации нельзя (нет BUILD) — проверяется в процедуре ASSERT-ом
--    (active REF nm EXCEPT BUILD nm = 0). Здесь — что universe непуст и совпадает с ожиданием.
SELECT COUNT(*) universe_nm FROM (SELECT nm_id FROM `wb_raw.REF_SKU_MASTER`
  WHERE marketplace='WB' AND active=TRUE AND nm_id IS NOT NULL GROUP BY nm_id);            -- 24

-- ===== ПОСЛЕ CALL sp_build_mart_sku_daily(...) =====

-- 4) Грейн + полнота universe + плотность + граница. (fix #2)
SELECT COUNT(*) n_rows,
  COUNT(*)-COUNT(DISTINCT FORMAT('%t|%t',day,nm_id)) grain_dupes,                          -- 0
  (SELECT COUNT(*) FROM (SELECT nm_id FROM `wb_raw.REF_SKU_MASTER`
     WHERE marketplace='WB' AND active=TRUE AND nm_id IS NOT NULL
     EXCEPT DISTINCT SELECT nm_id FROM `wb_mart.MART_SKU_DAILY`)) universe_missing,         -- 0
  (SELECT COUNTIF(cnt<>span) FROM (SELECT nm_id,COUNT(*) cnt,DATE_DIFF(MAX(day),MIN(day),DAY)+1 span
     FROM `wb_mart.MART_SKU_DAILY` GROUP BY nm_id)) gap_nms,                                -- 0
  COUNT(DISTINCT nm_id) nm,                                                                 -- 24
  CAST(MAX(day) AS STRING) max_day,                                                         -- build_as_of
  MAX(build_as_of_date)=MAX(day) boundary_ok                                               -- true
FROM `wb_mart.MART_SKU_DAILY`;

-- 5) fix #3: консервация vs FACT/LONG_MAPPED ограничена <=build_as_of (universe). Ожидание: все дельты 0.
WITH u AS (SELECT nm_id FROM `wb_raw.REF_SKU_MASTER` WHERE marketplace='WB' AND active=TRUE AND nm_id IS NOT NULL GROUP BY nm_id),
b AS (SELECT MAX(build_as_of_date) d FROM `wb_mart.MART_SKU_DAILY`)
SELECT
  ROUND((SELECT SUM(ad_spend) FROM `wb_mart.MART_SKU_DAILY`)
       -(SELECT SUM(stats_spend_rub) FROM `wb_mart.FACT_ADS_SKU_DAILY` WHERE `date`<=(SELECT d FROM b) AND nm_id IN (SELECT nm_id FROM u)),2) d_ad_spend,      -- 0
  (SELECT SUM(orders_qty) FROM `wb_mart.MART_SKU_DAILY`)
  -(SELECT COUNTIF(NOT is_cancel) FROM `wb_mart.FACT_ORDERS` WHERE order_date<=(SELECT d FROM b) AND nm_id IN (SELECT nm_id FROM u)) d_orders_qty,             -- 0
  ROUND((SELECT SUM(buyouts_rub) FROM `wb_mart.MART_SKU_DAILY`)
       -(SELECT SUM(IF(NOT is_return,price_with_disc,0)) FROM `wb_mart.FACT_SALES` WHERE sale_date<=(SELECT d FROM b) AND nm_id IN (SELECT nm_id FROM u)),2) d_buyouts_rub, -- 0
  ROUND((SELECT SUM(commission_cost_positive+logistics_cost_positive) FROM `wb_mart.MART_SKU_DAILY`)
       -(SELECT SUM(cost_amount_positive) FROM `wb_mart.V_WB_FINANCE_AMOUNTS_LONG_MAPPED`
         WHERE is_sku_row AND cost_category IN ('commission','logistics') AND finance_date<=(SELECT d FROM b) AND nm_id IN (SELECT nm_id FROM u)),2) d_finance; -- 0

-- 6) Store-срез (агрегат витрины) 7д/14д + CPM.
SELECT wnd,
  ROUND(SUM(ad_spend)) ad_spend, SUM(orders_qty) orders_qty, SUM(buyouts_qty) buyouts_qty, ROUND(SUM(buyouts_rub)) buyouts_rub,
  ROUND(SAFE_DIVIDE(SUM(ad_spend),SUM(buyouts_rub))*100,1) drr_buyouts_pct,
  ROUND(SAFE_DIVIDE(SUM(ads_revenue_raw_rub),SUM(ad_spend)),2) roas,
  ROUND(SAFE_DIVIDE(SUM(ad_spend),SUM(views))*1000,1) cpm
FROM (
  SELECT '7d(23-29.07)' wnd,* FROM `wb_mart.MART_SKU_DAILY` WHERE day BETWEEN '2026-07-23' AND '2026-07-29'
  UNION ALL SELECT '14d(16-29.07)',* FROM `wb_mart.MART_SKU_DAILY` WHERE day BETWEEN '2026-07-16' AND '2026-07-29')
GROUP BY wnd ORDER BY wnd;

-- 7) Rolling spot-check: на 29.07 ad_spend_7d == прямая сумма 23–29.07 по nm (ожидание 0).
WITH direct AS (SELECT nm_id, ROUND(SUM(ad_spend),2) s7 FROM `wb_mart.MART_SKU_DAILY`
  WHERE day BETWEEN '2026-07-23' AND '2026-07-29' GROUP BY nm_id)
SELECT COUNTIF(ABS(ROUND(m.ad_spend_7d,2)-d.s7)>0.01) rolling_mismatches                    -- 0
FROM `wb_mart.MART_SKU_DAILY` m JOIN direct d USING(nm_id) WHERE m.day='2026-07-29';

-- 8) Границы estimate<=raw. Ожидание 0.
SELECT COUNTIF(ads_revenue_dedup_estimate_rub>ads_revenue_raw_rub OR ad_orders_dedup_estimate>ad_orders_raw) bounds_violations
FROM `wb_mart.MART_SKU_DAILY`;
