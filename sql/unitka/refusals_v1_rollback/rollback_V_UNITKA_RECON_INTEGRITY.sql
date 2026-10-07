-- ОТКАТ Phase 1A (refusals_v1): тело V_UNITKA_RECON_INTEGRITY в production ДО изменения 2026-10-07 (git HEAD b6812a3, live = Git, проверено).
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_RECON_INTEGRITY` AS
WITH
lcd AS (SELECT last_closed_date, window_from, window_to FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_RECON_WINDOW`),
f AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_RECON_FACT`),
ref AS (
  SELECT nm_id, ANY_VALUE(internal_sku) AS internal_sku, ANY_VALUE(product_name_short) AS product_name_short
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.REF_SKU_MASTER`
  WHERE marketplace = 'WB' GROUP BY nm_id
),
fo AS (
  SELECT nm_id, order_date AS day, COUNT(*) AS fact_order_rows
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ORDERS`, lcd
  WHERE order_date BETWEEN lcd.window_from AND lcd.window_to
  GROUP BY 1, 2
),
-- OBSERVED_PRICE_NOT_FACTUAL_ORDER_PRICE: последнее prod-наблюдение за сутки МСК. ТОЛЬКО диагностика.
obs AS (
  SELECT nm_id, DATE(observed_at, 'Europe/Moscow') AS day,
         ARRAY_AGG(STRUCT(seller_effective_price AS price, observed_at) ORDER BY observed_at DESC LIMIT 1)[OFFSET(0)] AS o
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_PRICES`, lcd
  WHERE environment = 'prod'
    AND observed_at >= TIMESTAMP(lcd.window_from, 'Europe/Moscow')
    AND observed_at <  TIMESTAMP(DATE_ADD(lcd.window_to, INTERVAL 1 DAY), 'Europe/Moscow')
  GROUP BY 1, 2
)
SELECT
  'WB'                          AS marketplace,
  f.nm_id,
  ref.internal_sku,
  ref.product_name_short        AS product_name,
  f.date_msk                    AS day,
  lcd.last_closed_date,
  f.orders                      AS orders_unitka,
  f.cancels                     AS cancels_unitka,
  f.orders_source,
  f.price                       AS factual_order_price,   -- NULL ≠ 0
  f.price_source,                                         -- ORDERS_API | FUNNEL_FALLBACK | NULL
  f.funnel_orders               AS orders_funnel,         -- NULL = строки воронки нет (бэкфилл — отдельно, в orders_source)
  fo.fact_order_rows,
  NULLIF(f.fact_order_qty, 0)   AS fact_order_qty,
  f.funnel_orders_sum,
  f.same_day_cancel_qty,                                  -- отмены дня заказа: воронка их в orders_count не считает
  obs.o.price                   AS observed_price_diagnostic,  -- OBSERVED_PRICE_NOT_FACTUAL_ORDER_PRICE
  obs.o.observed_at             AS observed_price_at,
  f.storage                     AS storage_value,
  f.storage IS NOT NULL         AS storage_date_covered,
  f.stock IS NOT NULL           AS stock_date_covered,
  f.sku_active,
  CASE
    WHEN f.price IS NOT NULL THEN 'PRESENT'
    WHEN IFNULL(f.orders, 0) > 0 OR IFNULL(f.cancels, 0) > 0 THEN 'MISSING_WITH_ACTIVITY'
    ELSE 'MISSING_NO_ACTIVITY'
  END                           AS price_state,
  CASE
    WHEN f.orders_source != 'FUNNEL_API' THEN 'NO_FUNNEL_ROW'
    WHEN f.funnel_orders = f.fact_order_qty THEN 'EXACT'
    WHEN f.funnel_orders > 0 AND f.fact_order_qty = 0 THEN 'ONLY_FUNNEL'
    WHEN f.funnel_orders = 0 AND f.fact_order_qty > 0 THEN 'ONLY_FACT'
    WHEN f.funnel_orders > f.fact_order_qty THEN 'FUNNEL_GT_FACT'
    ELSE 'FACT_GT_FUNNEL'
  END                           AS divergence_class
FROM f
CROSS JOIN lcd
LEFT JOIN ref USING (nm_id)
LEFT JOIN fo  ON fo.nm_id = f.nm_id AND fo.day = f.date_msk
LEFT JOIN obs ON obs.nm_id = f.nm_id AND obs.day = f.date_msk;

