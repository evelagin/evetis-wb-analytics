-- ОТКАТ Phase 1A (refusals_v1): тело V_UNITKA_DAILY_FACT в production ДО изменения 2026-10-07 (git HEAD b6812a3, live = Git, проверено).
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_DAILY_FACT` AS
WITH lcd AS (
  SELECT last_closed_date AS d2, DATE_TRUNC(last_closed_date, MONTH) AS d1
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_LAST_CLOSED_DATE`
),
days AS (SELECT d FROM lcd, UNNEST(GENERATE_DATE_ARRAY(lcd.d1, lcd.d2)) AS d),
sku AS (
  SELECT nm_id FROM `project-fa311fc0-4d87-4781-986.wb_raw.REF_SKU_MASTER`
  WHERE marketplace = 'WB' AND active
),
g AS (SELECT s.nm_id, d.d FROM sku s CROSS JOIN days d),
o AS (
  SELECT nm_id, order_date AS d, SUM(quantity) AS gross,
         -- Только отмены СЛЕДУЮЩИХ дней: отмену дня заказа воронка уже исключила из Q (см. контракт выше).
         SUM(IF(is_cancel AND SAFE_CAST(SUBSTR(cancel_dt, 1, 10) AS DATE) > order_date, quantity, 0)) AS canc,
         SAFE_DIVIDE(SUM(price_with_disc * quantity), NULLIF(SUM(quantity), 0)) AS price
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ORDERS`, lcd
  WHERE order_date BETWEEN lcd.d1 AND lcd.d2
  GROUP BY 1, 2
),
m AS (
  SELECT nm_id, day AS d, SUM(views) AS views, SUM(ad_spend) AS ads
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.MART_SKU_DAILY`, lcd
  WHERE day BETWEEN lcd.d1 AND lcd.d2
  GROUP BY 1, 2
),
f AS (
  -- fsum — сумма заказов воронки; нужна ТОЛЬКО как делимое подстановки цены (см. контракт цены выше).
  SELECT nm_id, date_msk AS d, MAX(open_card_count) AS opens, MAX(add_to_cart_count) AS carts,
         MAX(orders_count) AS forders, MAX(orders_sum_rub) AS fsum
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FUNNEL_DAILY`, lcd
  WHERE date_msk BETWEEN lcd.d1 AND lcd.d2
  GROUP BY 1, 2
),
bf AS (
  SELECT nm_id, date_msk AS d, open_card_count AS opens, add_to_cart_count AS carts,
         orders_count AS forders, cancel_count AS canc
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_FUNNEL_XLSX_BACKFILL`
  WHERE date_msk <= DATE '2026-09-03'
),
sd AS (SELECT DISTINCT snapshot_date AS d FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_STOCKS_SNAPSHOT`, lcd WHERE snapshot_date BETWEEN lcd.d1 AND lcd.d2),
st AS (
  SELECT nm_id, snapshot_date AS d, SUM(quantity) AS stock
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_STOCKS_SNAPSHOT`, lcd
  WHERE snapshot_date BETWEEN lcd.d1 AND lcd.d2
  GROUP BY 1, 2
),
pd AS (SELECT DISTINCT date_msk AS d FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_STORAGE_DAILY`, lcd WHERE date_msk BETWEEN lcd.d1 AND lcd.d2),
ps AS (
  SELECT nm_id, date_msk AS d, storage_rub_exact AS storage
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_STORAGE_DAILY`, lcd
  WHERE date_msk BETWEEN lcd.d1 AND lcd.d2
),
x AS (
  SELECT
    g.nm_id,
    g.d,
    IFNULL(m.views, 0)                                   AS views,
    COALESCE(f.opens, bf.opens)                          AS opens,
    COALESCE(f.carts, bf.carts)                          AS carts,
    COALESCE(f.forders, bf.forders, o.gross, 0)          AS orders,
    COALESCE(bf.canc, o.canc, 0)                         AS cancels,
    IF(sd.d IS NULL, NULL, IFNULL(st.stock, 0))          AS stock,
    ROUND(IFNULL(m.ads, 0), 2)                           AS ads_in,
    IF(pd.d IS NULL, NULL, ROUND(IFNULL(ps.storage, 0), 2)) AS storage,
    CASE WHEN f.forders IS NOT NULL THEN 'FUNNEL_API'
         WHEN bf.forders IS NOT NULL THEN 'XLSX_BACKFILL'
         ELSE 'ORDERS_API' END                           AS orders_source,
    CASE WHEN bf.canc IS NOT NULL THEN 'XLSX_BACKFILL'
         ELSE 'PROXY_FACT_ORDERS' END                    AS cancels_source,
    o.price                                              AS orders_api_price,
    IFNULL(o.gross, 0)                                   AS fact_order_qty,
    f.forders                                            AS funnel_orders,
    f.fsum                                               AS funnel_orders_sum
  FROM g
  LEFT JOIN o  ON o.nm_id  = g.nm_id AND o.d  = g.d
  LEFT JOIN m  ON m.nm_id  = g.nm_id AND m.d  = g.d
  LEFT JOIN f  ON f.nm_id  = g.nm_id AND f.d  = g.d
  LEFT JOIN bf ON bf.nm_id = g.nm_id AND bf.d = g.d
  LEFT JOIN st ON st.nm_id = g.nm_id AND st.d = g.d
  LEFT JOIN ps ON ps.nm_id = g.nm_id AND ps.d = g.d
  LEFT JOIN sd ON sd.d = g.d
  LEFT JOIN pd ON pd.d = g.d
),
p AS (
  SELECT
    x.*,
    -- FUNNEL_FALLBACK разрешён ТОЛЬКО так: Orders API пуст, счётчик Unitka — воронка, сумма воронки положительна.
    (x.orders_api_price IS NULL AND x.fact_order_qty = 0 AND x.orders_source = 'FUNNEL_API'
      AND IFNULL(x.funnel_orders, 0) > 0 AND x.funnel_orders = x.orders AND IFNULL(x.funnel_orders_sum, 0) > 0) AS funnel_fallback_ok
  FROM x
)
SELECT
  nm_id,
  d AS date_msk,
  views, opens, carts, orders, cancels, stock, ads_in,
  CASE
    WHEN orders_api_price IS NOT NULL THEN ROUND(orders_api_price, 2)
    WHEN funnel_fallback_ok THEN ROUND(SAFE_DIVIDE(funnel_orders_sum, funnel_orders), 2)
  END                                                    AS price,
  storage,
  orders_source,
  cancels_source,
  CASE
    WHEN orders_api_price IS NOT NULL THEN 'ORDERS_API'
    WHEN funnel_fallback_ok THEN 'FUNNEL_FALLBACK'
  END                                                    AS price_source
FROM p;
