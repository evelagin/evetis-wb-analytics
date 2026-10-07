-- ОТКАТ Phase 1A (refusals_v1): тело V_UNITKA_RECON_FACT в production ДО изменения 2026-10-07 (git HEAD b6812a3, live = Git, проверено).
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_RECON_FACT` AS
WITH w AS (
  SELECT window_from AS d1, window_to AS d2 FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_RECON_WINDOW`
),
days AS (SELECT d FROM w, UNNEST(GENERATE_DATE_ARRAY(w.d1, w.d2)) AS d),
sku AS (
  SELECT nm_id, LOGICAL_OR(active) AS sku_active
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.REF_SKU_MASTER`
  WHERE marketplace = 'WB' AND nm_id IS NOT NULL
  GROUP BY nm_id
),
g AS (SELECT s.nm_id, s.sku_active, d.d FROM sku s CROSS JOIN days d),
o AS (
  SELECT nm_id, order_date AS d, SUM(quantity) AS gross,
         -- S листа = отмены СЛЕДУЮЩИХ дней. Отмену дня заказа воронка уже исключила из Q, поэтому
         -- вычитать её второй раз нельзя (контракт отмен доказан 20.09.2026, см. engine_v1_views.sql).
         SUM(IF(is_cancel AND SAFE_CAST(SUBSTR(cancel_dt, 1, 10) AS DATE) > order_date, quantity, 0)) AS canc,
         -- Отмена В ДЕНЬ ЗАКАЗА: воронка не показывает ни заказ, ни отмену. ТОЛЬКО диагностика расхождения
         -- счётчиков Orders API и воронки; в Q / S / цену листа не входит.
         SUM(IF(is_cancel AND SAFE_CAST(SUBSTR(cancel_dt, 1, 10) AS DATE) = order_date, quantity, 0)) AS same_day_canc,
         SAFE_DIVIDE(SUM(price_with_disc * quantity), NULLIF(SUM(quantity), 0)) AS price,
         MAX(built_at) AS built_at
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ORDERS`, w
  WHERE order_date BETWEEN w.d1 AND w.d2
  GROUP BY 1, 2
),
m AS (
  SELECT nm_id, day AS d, SUM(views) AS views, SUM(ad_spend) AS ads
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.MART_SKU_DAILY`, w
  WHERE day BETWEEN w.d1 AND w.d2
  GROUP BY 1, 2
),
f AS (
  SELECT nm_id, date_msk AS d, MAX(open_card_count) AS opens, MAX(add_to_cart_count) AS carts,
         MAX(orders_count) AS forders, MAX(orders_sum_rub) AS fsum, MAX(observed_at) AS observed_at
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FUNNEL_DAILY`, w
  WHERE date_msk BETWEEN w.d1 AND w.d2
  GROUP BY 1, 2
),
-- Та же жёсткая граница XLSX-бэкфилла, что в V_UNITKA_DAILY_FACT (решение владельца 11.09).
bf AS (
  SELECT nm_id, date_msk AS d, open_card_count AS opens, add_to_cart_count AS carts,
         orders_count AS forders, cancel_count AS canc
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_FUNNEL_XLSX_BACKFILL`
  WHERE date_msk <= DATE '2026-09-03'
),
sd AS (SELECT DISTINCT snapshot_date AS d FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_STOCKS_SNAPSHOT`, w WHERE snapshot_date BETWEEN w.d1 AND w.d2),
st AS (
  SELECT nm_id, snapshot_date AS d, SUM(quantity) AS stock
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_STOCKS_SNAPSHOT`, w
  WHERE snapshot_date BETWEEN w.d1 AND w.d2
  GROUP BY 1, 2
),
pd AS (SELECT DISTINCT date_msk AS d FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_STORAGE_DAILY`, w WHERE date_msk BETWEEN w.d1 AND w.d2),
ps AS (
  SELECT nm_id, date_msk AS d, storage_rub_exact AS storage, observed_at
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_STORAGE_DAILY`, w
  WHERE date_msk BETWEEN w.d1 AND w.d2
),
x AS (
  SELECT
    g.nm_id, g.sku_active, g.d,
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
    IFNULL(o.same_day_canc, 0)                           AS same_day_cancel_qty,
    f.forders                                            AS funnel_orders,
    f.fsum                                               AS funnel_orders_sum,
    f.observed_at                                        AS funnel_observed_at,
    o.built_at                                           AS orders_built_at,
    ps.observed_at                                       AS storage_observed_at
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
  END                                                    AS price_source,
  fact_order_qty,
  funnel_orders,
  funnel_orders_sum,
  sku_active,
  FORMAT_DATE('%Y-%m', d)                                AS month_key,
  funnel_observed_at,
  orders_built_at,
  storage_observed_at,
  same_day_cancel_qty
FROM p;

