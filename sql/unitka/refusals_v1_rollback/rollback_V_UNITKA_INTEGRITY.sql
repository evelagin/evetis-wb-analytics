-- ОТКАТ Phase 1A (refusals_v1): тело V_UNITKA_INTEGRITY в production ДО изменения 2026-10-07 (git HEAD b6812a3, live = Git, проверено).
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_INTEGRITY` AS
WITH
lcd AS (
  SELECT last_closed_date FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_LAST_CLOSED_DATE`
),
b AS (
  -- Границы берутся ТАК ЖЕ, как в V_UNITKA_DAILY_FACT: месяц LCD. Своих календарных констант нет.
  SELECT DATE_TRUNC(last_closed_date, MONTH) AS d1, last_closed_date AS d2 FROM lcd
),
-- Ровно то, что Engine пишет в лист: Guard проверяет записываемые факты, а не свою копию логики.
f AS (
  SELECT nm_id, date_msk AS day, orders, cancels, price, storage, orders_source
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_DAILY_FACT`
),
ref AS (
  SELECT nm_id, internal_sku, product_name_short
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.REF_SKU_MASTER`
  WHERE marketplace = 'WB' AND active
),
fu AS (
  SELECT nm_id, date_msk AS day, MAX(orders_count) AS orders_count
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FUNNEL_DAILY`, b
  WHERE date_msk BETWEEN b.d1 AND b.d2
  GROUP BY 1, 2
),
-- Та же жёсткая граница XLSX-бэкфилла, что в V_UNITKA_DAILY_FACT (решение владельца 11.09).
bf AS (
  SELECT nm_id, date_msk AS day, orders_count
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_FUNNEL_XLSX_BACKFILL`
  WHERE date_msk <= DATE '2026-09-03'
),
fo AS (
  SELECT nm_id, order_date AS day, COUNT(*) AS fact_order_rows, SUM(quantity) AS fact_order_qty
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ORDERS`, b
  WHERE order_date BETWEEN b.d1 AND b.d2
  GROUP BY 1, 2
),
-- OBSERVED_PRICE_NOT_FACTUAL_ORDER_PRICE: последнее prod-наблюдение за сутки МСК.
obs AS (
  SELECT nm_id, DATE(observed_at, 'Europe/Moscow') AS day,
         ARRAY_AGG(STRUCT(seller_effective_price AS price, observed_at) ORDER BY observed_at DESC LIMIT 1)[OFFSET(0)] AS o
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_PRICES`, b
  WHERE environment = 'prod'
    AND observed_at >= TIMESTAMP(b.d1, 'Europe/Moscow')
    AND observed_at <  TIMESTAMP(DATE_ADD(b.d2, INTERVAL 1 DAY), 'Europe/Moscow')
  GROUP BY 1, 2
),
sd AS (
  SELECT DISTINCT date_msk AS day
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_STORAGE_DAILY`, b
  WHERE date_msk BETWEEN b.d1 AND b.d2
),
x AS (
  SELECT
    f.*,
    COALESCE(fu.orders_count, bf.orders_count) AS orders_funnel,   -- NULL = строки воронки нет
    fo.fact_order_rows, fo.fact_order_qty,
    obs.o.price AS observed_price_diagnostic,
    obs.o.observed_at AS observed_price_at,
    sd.day IS NOT NULL AS storage_date_covered
  FROM f
  LEFT JOIN fu  USING (nm_id, day)
  LEFT JOIN bf  USING (nm_id, day)
  LEFT JOIN fo  USING (nm_id, day)
  LEFT JOIN obs USING (nm_id, day)
  LEFT JOIN sd  USING (day)
)
SELECT
  'WB'                          AS marketplace,
  x.nm_id,
  ref.internal_sku,
  ref.product_name_short        AS product_name,
  x.day,
  lcd.last_closed_date,
  x.orders                      AS orders_unitka,        -- Q, как пишет Engine
  x.cancels                     AS cancels_unitka,       -- S, как пишет Engine
  x.orders_source,
  x.price                       AS factual_order_price,  -- NULL ≠ 0
  x.orders_funnel,
  x.fact_order_rows,
  x.fact_order_qty,
  x.observed_price_diagnostic,                           -- OBSERVED_PRICE_NOT_FACTUAL_ORDER_PRICE
  x.observed_price_at,
  x.storage                     AS storage_value,
  x.storage_date_covered,
  CASE
    WHEN x.price IS NOT NULL THEN 'PRESENT'
    WHEN IFNULL(x.orders, 0) > 0 OR IFNULL(x.cancels, 0) > 0 THEN 'MISSING_WITH_ACTIVITY'
    ELSE 'MISSING_NO_ACTIVITY'
  END                           AS price_state,
  CASE
    WHEN x.orders_funnel IS NULL THEN 'NO_FUNNEL_ROW'
    WHEN x.orders_funnel = IFNULL(x.fact_order_qty, 0) THEN 'EXACT'
    WHEN x.orders_funnel > 0 AND IFNULL(x.fact_order_qty, 0) = 0 THEN 'ONLY_FUNNEL'
    WHEN x.orders_funnel = 0 AND IFNULL(x.fact_order_qty, 0) > 0 THEN 'ONLY_FACT'
    WHEN x.orders_funnel > IFNULL(x.fact_order_qty, 0) THEN 'FUNNEL_GT_FACT'
    ELSE 'FACT_GT_FUNNEL'
  END                           AS divergence_class
FROM x
CROSS JOIN lcd
LEFT JOIN ref USING (nm_id);

