-- ══════════════════════════════════════════════════════════════
-- Ads-4 · Advertising Data Mart / Funnel Contract — v1
--
-- Спека: docs/ADS4_FUNNEL_MART_DESIGN_2026-08-15.md (ред. 3)
--
-- 🔴 ИНВАРИАНТЫ РЕАЛИЗАЦИИ (нарушение = откат, не правка):
--   1. Только CREATE OR REPLACE VIEW. Ни одной таблицы, ни одного INSERT.
--   2. Существующие MART/FACT/heartbeat не затрагиваются.
--   3. Грейн витрины — (nm_id, norm_query). advert_id НЕ входит в ключ:
--      кампания есть способ доставки, а не измерение диагноза (§2.2).
--   4. Ставки Ads-3 агрегируются ДО грейна витрины и только потом LEFT JOIN —
--      fan-out структурно невозможен (§3.2).
--   5. Baseline исключает саму пару (SKU_EX_SELF), иначе leakage (§7).
--   6. COALESCE(metric, 0) запрещён для ставок конверсии и CPO:
--      cpo_ads при orders_sum = 0 обязан быть NULL, ctr при views_sum NULL — NULL.
--   7. Числитель CTR — clicks_on_imp (клики строк, где показы ЕСТЬ), не clicks_sum:
--      у 67,5% расхода показов нет, наивная формула завышает CTR в 2,3 раза (§9.1).
--   8. Немонотонные строки воронки не чинятся обрезкой; они помечаются
--      funnel_monotonic = FALSE и исключаются из сигналов о конверсии (§9.3).
--
-- Файл сгенерирован из шаблона: блоки V_ADS_FUNNEL_QUERY_28D и _90D
-- отличаются ТОЛЬКО константами окна (28/56 против 90/180).
-- ══════════════════════════════════════════════════════════════


-- ────────────────────────────────────────────────────────────
-- 1/5 · V_ADS_FUNNEL_QUERY_DAILY — суточный слой, грейн (day, nm_id, norm_query)
--       Ads-4 не создаёт нового факта: это свёртка канонического слоя Ads-2
--       по кампаниям. Читаем V_ADV_QUERY_STATS (канон), а не RAW.
-- ────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_ADS_FUNNEL_QUERY_DAILY` AS
WITH src AS (
  SELECT
    SAFE.PARSE_DATE('%Y-%m-%d', period_from) AS day,
    SAFE_CAST(advert_id AS INT64)            AS advert_id,
    SAFE_CAST(nm_id     AS INT64)            AS nm_id,
    norm_query,
    SAFE_CAST(views   AS FLOAT64)            AS views,
    SAFE_CAST(clicks  AS FLOAT64)            AS clicks,
    SAFE_CAST(atbs    AS FLOAT64)            AS atbs,
    SAFE_CAST(orders  AS FLOAT64)            AS orders,
    SAFE_CAST(shks    AS FLOAT64)            AS shks,
    SAFE_CAST(spend   AS FLOAT64)            AS spend,
    SAFE_CAST(avg_pos AS FLOAT64)            AS avg_pos
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_ADV_QUERY_STATS`
)
SELECT
  day,
  nm_id,
  norm_query,
  ARRAY_AGG(DISTINCT advert_id ORDER BY advert_id)      AS advert_ids,
  COUNT(DISTINCT advert_id)                             AS advert_count,
  SUM(views)                                            AS views_sum,
  SUM(clicks)                                           AS clicks_sum,
  -- 🔴 числитель CTR: клики только тех строк, где показы есть
  SUM(IF(views IS NOT NULL, clicks, 0))                 AS clicks_on_imp,
  SUM(atbs)                                             AS atbs_sum,
  SUM(orders)                                           AS orders_sum,
  SUM(shks)                                             AS shks_sum,
  SUM(spend)                                            AS spend_sum,
  SUM(IF(views IS NOT NULL, spend, 0))                  AS spend_on_imp,
  SUM(avg_pos * views)                                  AS avg_pos_x_views,
  SUM(avg_pos * clicks)                                 AS avg_pos_x_clicks,
  COUNTIF(views IS NOT NULL)                            AS rows_with_impressions,
  COUNT(*)                                              AS src_rows
FROM src
GROUP BY day, nm_id, norm_query;


-- ────────────────────────────────────────────────────────────
-- 2/5 · V_ADS_FUNNEL_QUERY_28D — окно 28 суток, грейн (nm_id, norm_query)
--       as_of_date = MAX(day) факта, НЕ CURRENT_DATE(): витрина детерминирована
--       относительно данных, а не относительно момента запроса.
-- ────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_ADS_FUNNEL_QUERY_28D` AS
WITH anchor AS (
  SELECT MAX(day) AS as_of_date FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_ADS_FUNNEL_QUERY_DAILY`
),
win AS (
  SELECT d.*
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_ADS_FUNNEL_QUERY_DAILY` d, anchor a
  WHERE d.day >  DATE_SUB(a.as_of_date, INTERVAL 28 DAY)
    AND d.day <= a.as_of_date
),
prev AS (
  SELECT d.*
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_ADS_FUNNEL_QUERY_DAILY` d, anchor a
  WHERE d.day >  DATE_SUB(a.as_of_date, INTERVAL 56 DAY)
    AND d.day <= DATE_SUB(a.as_of_date, INTERVAL 28 DAY)
),
pair AS (
  SELECT
    nm_id, norm_query,
    SUM(views_sum)             AS views_sum,
    SUM(clicks_sum)            AS clicks_sum,
    SUM(clicks_on_imp)         AS clicks_on_imp,
    SUM(atbs_sum)              AS atbs_sum,
    SUM(orders_sum)            AS orders_sum,
    SUM(shks_sum)              AS shks_sum,
    SUM(spend_sum)             AS spend_sum,
    SUM(spend_on_imp)          AS spend_on_imp,
    SUM(avg_pos_x_views)       AS apxv,
    SUM(avg_pos_x_clicks)      AS apxc,
    SUM(rows_with_impressions) AS rows_with_impressions,
    COUNT(DISTINCT day)                             AS days_with_data,
    COUNT(DISTINCT IF(spend_sum > 0, day, NULL))    AS days_with_spend
  FROM win
  GROUP BY nm_id, norm_query
),
camps AS (
  SELECT nm_id, norm_query,
         ARRAY_AGG(DISTINCT aid ORDER BY aid) AS advert_ids,
         COUNT(DISTINCT aid)                  AS advert_count
  FROM win, UNNEST(advert_ids) AS aid
  GROUP BY nm_id, norm_query
),
pair_prev AS (
  SELECT nm_id, norm_query,
         SUM(views_sum)     AS views_sum,
         SUM(clicks_sum)    AS clicks_sum,
         SUM(clicks_on_imp) AS clicks_on_imp,
         SUM(atbs_sum)      AS atbs_sum,
         SUM(orders_sum)    AS orders_sum,
         SUM(spend_sum)     AS spend_sum
  FROM prev
  GROUP BY nm_id, norm_query
),
sku AS (
  SELECT nm_id,
         SUM(views_sum)     AS views_sum,
         SUM(clicks_sum)    AS clicks_sum,
         SUM(clicks_on_imp) AS clicks_on_imp,
         SUM(atbs_sum)      AS atbs_sum,
         SUM(orders_sum)    AS orders_sum,
         SUM(spend_sum)     AS spend_sum,
         COUNT(*)           AS pair_count
  FROM pair GROUP BY nm_id
),
store AS (
  SELECT SUM(views_sum)     AS views_sum,
         SUM(clicks_sum)    AS clicks_sum,
         SUM(clicks_on_imp) AS clicks_on_imp,
         SUM(atbs_sum)      AS atbs_sum,
         SUM(orders_sum)    AS orders_sum,
         SUM(spend_sum)     AS spend_sum,
         COUNT(*)           AS pair_count
  FROM pair
),
-- 🔴 Ставка = ПОСЛЕДНИЙ канонический OK-снимок вообще, без привязки к окну.
--    История ставок начинается с первого снимка Ads-3 и назад не восстанавливается,
--    поэтому «ставки, действовавшей в окне» у нас нет и быть не может (§3.2).
bid_anchor AS (
  SELECT MAX(snapshot_date) AS snap
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_ADV_QUERY_BIDS`
  WHERE snapshot_status = 'OK'
),
bids AS (
  SELECT
    SAFE_CAST(b.nm_id AS INT64)                                       AS nm_id,
    b.norm_query,
    MIN(SAFE_CAST(b.bid AS FLOAT64))                                  AS bid_min,
    MAX(SAFE_CAST(b.bid AS FLOAT64))                                  AS bid_max,
    MIN(SAFE_CAST(b.bid_kopecks AS INT64))                            AS bid_kopecks_min,
    MAX(SAFE_CAST(b.bid_kopecks AS INT64))                            AS bid_kopecks_max,
    COUNT(DISTINCT SAFE_CAST(b.bid AS FLOAT64)) = 1                   AS bid_is_uniform,
    COUNT(DISTINCT b.advert_id)                                       AS bid_campaign_count,
    STRING_AGG(DISTINCT b.payment_type,    ',' ORDER BY b.payment_type)    AS payment_type,
    STRING_AGG(DISTINCT b.bid_type,        ',' ORDER BY b.bid_type)        AS bid_type,
    STRING_AGG(DISTINCT b.campaign_status, ',' ORDER BY b.campaign_status) AS campaign_status,
    SAFE.PARSE_DATE('%Y-%m-%d', MAX(b.snapshot_date))                 AS bid_snapshot_date,
    MAX(b.snapshot_ts)                                                AS bid_snapshot_ts,
    STRING_AGG(DISTINCT b.snapshot_status, ',')                       AS bid_snapshot_status
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_ADV_QUERY_BIDS` b, bid_anchor ba
  WHERE b.snapshot_status = 'OK'
    AND b.snapshot_date   = ba.snap
  GROUP BY 1, 2            -- ровно одна строка на ключ витрины → fan-out невозможен
),
base AS (
  SELECT
    a.as_of_date,
    28                                             AS window_days,
    p.nm_id, p.norm_query,
    c.advert_ids, c.advert_count,
    p.days_with_data, p.days_with_spend, p.rows_with_impressions,
    p.views_sum, p.clicks_sum, p.clicks_on_imp, p.atbs_sum, p.orders_sum,
    p.shks_sum, p.spend_sum, p.spend_on_imp, p.apxv, p.apxc,
    -- ── ex-self агрегаты SKU (baseline без самой пары) ──
    s.views_sum     - IFNULL(p.views_sum, 0)       AS x_views,
    s.clicks_sum    - p.clicks_sum                 AS x_clicks,
    s.clicks_on_imp - p.clicks_on_imp              AS x_clicks_on_imp,
    s.atbs_sum      - p.atbs_sum                   AS x_atbs,
    s.orders_sum    - p.orders_sum                 AS x_orders,
    s.spend_sum     - p.spend_sum                  AS x_spend,
    s.clicks_sum                                   AS sku_clicks_total,
    s.pair_count    - 1                            AS x_pair_count,
    -- ── ex-self агрегаты магазина ──
    st.views_sum     - IFNULL(p.views_sum, 0)      AS y_views,
    st.clicks_sum    - p.clicks_sum                AS y_clicks,
    st.clicks_on_imp - p.clicks_on_imp             AS y_clicks_on_imp,
    st.atbs_sum      - p.atbs_sum                  AS y_atbs,
    st.orders_sum    - p.orders_sum                AS y_orders,
    st.spend_sum     - p.spend_sum                 AS y_spend,
    st.pair_count    - 1                           AS y_pair_count,
    st.spend_on_imp_total                          AS store_spend_on_imp,
    st.spend_total                                 AS store_spend_total,
    -- ── предыдущее непересекающееся окно ──
    pp.views_sum     AS prev_views_sum,
    pp.clicks_sum    AS prev_clicks_sum,
    pp.clicks_on_imp AS prev_clicks_on_imp,
    pp.atbs_sum      AS prev_atbs_sum,
    pp.orders_sum    AS prev_orders_sum,
    pp.spend_sum     AS prev_spend_sum,
    -- ── контекст ставки ──
    b.bid_min, b.bid_max, b.bid_kopecks_min, b.bid_kopecks_max,
    b.bid_is_uniform, b.bid_campaign_count,
    b.payment_type, b.bid_type, b.campaign_status,
    b.bid_snapshot_date, b.bid_snapshot_ts, b.bid_snapshot_status
  FROM pair p
  CROSS JOIN anchor a
  CROSS JOIN (SELECT store.*,
                     (SELECT SUM(spend_on_imp) FROM pair) AS spend_on_imp_total,
                     (SELECT SUM(spend_sum)    FROM pair) AS spend_total
              FROM store) st
  JOIN      sku       s  ON s.nm_id = p.nm_id
  LEFT JOIN camps     c  ON c.nm_id = p.nm_id AND c.norm_query = p.norm_query
  LEFT JOIN pair_prev pp ON pp.nm_id = p.nm_id AND pp.norm_query = p.norm_query
  LEFT JOIN bids      b  ON b.nm_id = p.nm_id AND b.norm_query = p.norm_query
),
calc AS (
  SELECT
    base.*,
    -- ── метрики пары (называются по знаменателю) ──
    SAFE_DIVIDE(clicks_on_imp, views_sum)                  AS ctr,
    SAFE_DIVIDE(atbs_sum,      clicks_sum)                 AS cart_cr_clicks,
    SAFE_DIVIDE(orders_sum,    atbs_sum)                   AS order_cr_carts,
    SAFE_DIVIDE(orders_sum,    clicks_sum)                 AS order_cr_clicks,
    SAFE_DIVIDE(shks_sum,      orders_sum)                 AS buyout_ratio_shk,
    SAFE_DIVIDE(spend_sum,     clicks_sum)                 AS cpc_calc,
    -- 🔴 NULL, а не 0: «стоимость заказа не определена» ≠ «равна нулю»
    SAFE_DIVIDE(spend_sum,     NULLIF(orders_sum, 0))      AS cpo_ads,
    SAFE_DIVIDE(spend_on_imp,  views_sum) * 1000           AS cpm_calc,
    SAFE_DIVIDE(apxv,          views_sum)                  AS avg_pos_w_views,
    SAFE_DIVIDE(apxc,          clicks_sum)                 AS avg_pos_w_clicks,
    SAFE_DIVIDE(spend_sum,     store_spend_total)          AS spend_share_window,
    SAFE_DIVIDE(store_spend_on_imp, store_spend_total)     AS ctr_coverage_spend_share,
    -- ── монотонность воронки: значения НЕ чинятся, только помечаются ──
    ( (views_sum IS NULL OR clicks_on_imp <= views_sum)
      AND atbs_sum   <= clicks_sum
      AND orders_sum <= atbs_sum )                         AS funnel_monotonic,
    -- ── доказательность ──
    (IFNULL(views_sum, 0) >= 1000)                         AS can_compare_ctr,
    (clicks_sum >= 60)                                     AS can_judge_cart_cr,
    (clicks_sum >= 40)                                     AS can_judge_order_cr,
    (atbs_sum   >= 40)                                     AS can_judge_order_carts,
    (clicks_sum >= 20)                                     AS can_compare_cpc,
    CASE
      WHEN clicks_sum >= 40                                    THEN 'ACTIONABLE'
      WHEN clicks_sum >= 10 OR IFNULL(views_sum, 0) >= 1000    THEN 'OBSERVATIONAL'
      ELSE 'INSUFFICIENT'
    END                                                    AS evidence_status,
    FORMAT('clicks=%d;views=%s;atbs=%d',
           CAST(clicks_sum AS INT64),
           IFNULL(CAST(CAST(views_sum AS INT64) AS STRING), 'NULL'),
           CAST(atbs_sum AS INT64))                        AS evidence_reason,
    -- ── baseline по классам утверждений, всегда ex-self ──
    CASE WHEN x_views >= 1000 THEN 'SKU_EX_SELF'
         WHEN y_views >= 1000 THEN 'STORE_EX_SELF' END     AS baseline_ctr_level,
    CASE WHEN x_views >= 1000 THEN SAFE_DIVIDE(x_clicks_on_imp, x_views)
         WHEN y_views >= 1000 THEN SAFE_DIVIDE(y_clicks_on_imp, y_views) END AS baseline_ctr,
    CASE WHEN x_views >= 1000 THEN x_views
         WHEN y_views >= 1000 THEN y_views END             AS baseline_ctr_sample_views,
    CASE WHEN x_clicks >= 60 THEN 'SKU_EX_SELF'
         WHEN y_clicks >= 60 THEN 'STORE_EX_SELF' END      AS baseline_cart_level,
    CASE WHEN x_clicks >= 60 THEN SAFE_DIVIDE(x_atbs, x_clicks)
         WHEN y_clicks >= 60 THEN SAFE_DIVIDE(y_atbs, y_clicks) END AS baseline_cart_cr,
    CASE WHEN x_clicks >= 60 THEN x_clicks
         WHEN y_clicks >= 60 THEN y_clicks END             AS baseline_cart_sample_clicks,
    CASE WHEN x_clicks >= 40 THEN 'SKU_EX_SELF'
         WHEN y_clicks >= 40 THEN 'STORE_EX_SELF' END      AS baseline_order_level,
    CASE WHEN x_clicks >= 40 THEN SAFE_DIVIDE(x_orders, x_clicks)
         WHEN y_clicks >= 40 THEN SAFE_DIVIDE(y_orders, y_clicks) END AS baseline_order_cr,
    CASE WHEN x_clicks >= 40 THEN x_clicks
         WHEN y_clicks >= 40 THEN y_clicks END             AS baseline_order_sample_clicks,
    CASE WHEN x_atbs >= 40 THEN 'SKU_EX_SELF'
         WHEN y_atbs >= 40 THEN 'STORE_EX_SELF' END        AS baseline_order_carts_level,
    CASE WHEN x_atbs >= 40 THEN SAFE_DIVIDE(x_orders, x_atbs)
         WHEN y_atbs >= 40 THEN SAFE_DIVIDE(y_orders, y_atbs) END AS baseline_order_carts,
    CASE WHEN x_atbs >= 40 THEN x_atbs
         WHEN y_atbs >= 40 THEN y_atbs END                 AS baseline_order_carts_sample_atbs,
    CASE WHEN x_clicks >= 20 THEN 'SKU_EX_SELF'
         WHEN y_clicks >= 20 THEN 'STORE_EX_SELF' END      AS baseline_cpc_level,
    CASE WHEN x_clicks >= 20 THEN SAFE_DIVIDE(x_spend, x_clicks)
         WHEN y_clicks >= 20 THEN SAFE_DIVIDE(y_spend, y_clicks) END AS baseline_cpc,
    CASE WHEN x_clicks >= 20 THEN x_clicks
         WHEN y_clicks >= 20 THEN y_clicks END             AS baseline_cpc_sample_clicks,
    CASE WHEN x_clicks >= 40 AND x_orders > 0 THEN 'SKU_EX_SELF'
         WHEN y_clicks >= 40 AND y_orders > 0 THEN 'STORE_EX_SELF' END AS baseline_cpo_level,
    CASE WHEN x_clicks >= 40 AND x_orders > 0 THEN SAFE_DIVIDE(x_spend, x_orders)
         WHEN y_clicks >= 40 AND y_orders > 0 THEN SAFE_DIVIDE(y_spend, y_orders) END AS baseline_cpo,
    CASE WHEN x_clicks >= 40 AND x_orders > 0 THEN x_orders
         WHEN y_clicks >= 40 AND y_orders > 0 THEN y_orders END AS baseline_cpo_sample_orders,
    -- ── предыдущее окно ──
    SAFE_DIVIDE(prev_clicks_on_imp, prev_views_sum)        AS prev_ctr,
    SAFE_DIVIDE(prev_atbs_sum,      prev_clicks_sum)       AS prev_cart_cr_clicks,
    SAFE_DIVIDE(prev_orders_sum,    prev_clicks_sum)       AS prev_order_cr_clicks,
    SAFE_DIVIDE(prev_spend_sum,     prev_clicks_sum)       AS prev_cpc_calc,
    SAFE_DIVIDE(prev_spend_sum,     NULLIF(prev_orders_sum, 0)) AS prev_cpo_ads
  FROM base
)
SELECT
  as_of_date, window_days, nm_id, norm_query,
  advert_ids, advert_count,
  days_with_data, days_with_spend, rows_with_impressions,
  views_sum, clicks_sum, clicks_on_imp, atbs_sum, orders_sum, shks_sum,
  spend_sum, spend_on_imp,
  ctr, cart_cr_clicks, order_cr_carts, order_cr_clicks, buyout_ratio_shk,
  cpc_calc, cpo_ads, cpm_calc, avg_pos_w_views, avg_pos_w_clicks,
  spend_share_window, ctr_coverage_spend_share,
  funnel_monotonic,
  can_compare_ctr, can_judge_cart_cr, can_judge_order_cr,
  can_judge_order_carts, can_compare_cpc,
  evidence_status, evidence_reason,
  baseline_ctr,          baseline_ctr_level,          baseline_ctr_sample_views,
  baseline_cart_cr,      baseline_cart_level,         baseline_cart_sample_clicks,
  baseline_order_cr,     baseline_order_level,        baseline_order_sample_clicks,
  baseline_order_carts,  baseline_order_carts_level,  baseline_order_carts_sample_atbs,
  baseline_cpc,          baseline_cpc_level,          baseline_cpc_sample_clicks,
  baseline_cpo,          baseline_cpo_level,          baseline_cpo_sample_orders,
  IFNULL((SELECT STRING_AGG(DISTINCT lv, ',' ORDER BY lv)
          FROM UNNEST([baseline_ctr_level, baseline_cart_level, baseline_order_level,
                       baseline_order_carts_level, baseline_cpc_level, baseline_cpo_level]) lv
          WHERE lv IS NOT NULL), 'NONE')               AS baseline_level,
  -- 🔴 почему класс утверждений не получил SKU_EX_SELF baseline (ред. 2 §7.1).
  --    NULL = все классы сравниваются с собственным SKU без самой пары.
  (SELECT STRING_AGG(r, ',' ORDER BY r) FROM UNNEST([
     IF(baseline_ctr_level          IS NULL, 'CTR:NO_BASELINE',
     IF(baseline_ctr_level          = 'STORE_EX_SELF', 'CTR:SKU_SAMPLE_TOO_SMALL', NULL)),
     IF(baseline_cart_level         IS NULL, 'CART:NO_BASELINE',
     IF(baseline_cart_level         = 'STORE_EX_SELF', 'CART:SKU_SAMPLE_TOO_SMALL', NULL)),
     IF(baseline_order_level        IS NULL, 'ORDER_CR:NO_BASELINE',
     IF(baseline_order_level        = 'STORE_EX_SELF', 'ORDER_CR:SKU_SAMPLE_TOO_SMALL', NULL)),
     IF(baseline_order_carts_level  IS NULL, 'ORDER_CARTS:NO_BASELINE',
     IF(baseline_order_carts_level  = 'STORE_EX_SELF', 'ORDER_CARTS:SKU_SAMPLE_TOO_SMALL', NULL)),
     IF(baseline_cpc_level          IS NULL, 'CPC:NO_BASELINE',
     IF(baseline_cpc_level          = 'STORE_EX_SELF', 'CPC:SKU_SAMPLE_TOO_SMALL', NULL)),
     IF(baseline_cpo_level          IS NULL, 'CPO:NO_BASELINE',
     IF(baseline_cpo_level          = 'STORE_EX_SELF', 'CPO:SKU_SAMPLE_TOO_SMALL', NULL))
   ]) r WHERE r IS NOT NULL)                          AS baseline_fallback_reason,
  x_clicks                                             AS sku_ex_self_sample_clicks,
  x_pair_count                                         AS sku_ex_self_pair_count,
  sku_clicks_total,
  SAFE_DIVIDE(ctr,             baseline_ctr)           AS ctr_vs_baseline,
  SAFE_DIVIDE(cart_cr_clicks,  baseline_cart_cr)       AS cart_cr_vs_baseline,
  SAFE_DIVIDE(order_cr_clicks, baseline_order_cr)      AS order_cr_vs_baseline,
  SAFE_DIVIDE(order_cr_carts,  baseline_order_carts)   AS order_carts_vs_baseline,
  SAFE_DIVIDE(cpc_calc,        baseline_cpc)           AS cpc_vs_baseline,
  SAFE_DIVIDE(cpo_ads,         baseline_cpo)           AS cpo_vs_baseline,
  bid_min, bid_max, bid_kopecks_min, bid_kopecks_max,
  bid_is_uniform, bid_campaign_count,
  payment_type, bid_type, campaign_status,
  bid_snapshot_date, bid_snapshot_ts, bid_snapshot_status,
  DATE_DIFF(bid_snapshot_date, as_of_date, DAY)        AS bid_snapshot_offset_days,
  (bid_snapshot_date > as_of_date)                     AS bid_is_after_stats_as_of,
  prev_views_sum, prev_clicks_sum, prev_atbs_sum, prev_orders_sum, prev_spend_sum,
  prev_ctr, prev_cart_cr_clicks, prev_order_cr_clicks, prev_cpc_calc, prev_cpo_ads,
  -- 🔴 дельта считается ТОЛЬКО если оба окна проходят гейт: сравнение шума с шумом
  --    даёт шум, но выглядит как вывод (§5)
  IF(clicks_sum >= 20 AND prev_clicks_sum >= 20,
     SAFE_DIVIDE(spend_sum, clicks_sum) - SAFE_DIVIDE(prev_spend_sum, prev_clicks_sum),
     NULL)                                             AS delta_cpc,
  IF(clicks_sum >= 40 AND prev_clicks_sum >= 40,
     SAFE_DIVIDE(orders_sum, clicks_sum) - SAFE_DIVIDE(prev_orders_sum, prev_clicks_sum),
     NULL)                                             AS delta_order_cr_clicks,
  IF(prev_spend_sum IS NULL, NULL, spend_sum - prev_spend_sum) AS delta_spend
FROM calc;


-- ────────────────────────────────────────────────────────────
-- 3/5 · V_ADS_FUNNEL_QUERY_90D — окно 90 суток, грейн (nm_id, norm_query)
--       as_of_date = MAX(day) факта, НЕ CURRENT_DATE(): витрина детерминирована
--       относительно данных, а не относительно момента запроса.
-- ────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_ADS_FUNNEL_QUERY_90D` AS
WITH anchor AS (
  SELECT MAX(day) AS as_of_date FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_ADS_FUNNEL_QUERY_DAILY`
),
win AS (
  SELECT d.*
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_ADS_FUNNEL_QUERY_DAILY` d, anchor a
  WHERE d.day >  DATE_SUB(a.as_of_date, INTERVAL 90 DAY)
    AND d.day <= a.as_of_date
),
prev AS (
  SELECT d.*
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_ADS_FUNNEL_QUERY_DAILY` d, anchor a
  WHERE d.day >  DATE_SUB(a.as_of_date, INTERVAL 180 DAY)
    AND d.day <= DATE_SUB(a.as_of_date, INTERVAL 90 DAY)
),
pair AS (
  SELECT
    nm_id, norm_query,
    SUM(views_sum)             AS views_sum,
    SUM(clicks_sum)            AS clicks_sum,
    SUM(clicks_on_imp)         AS clicks_on_imp,
    SUM(atbs_sum)              AS atbs_sum,
    SUM(orders_sum)            AS orders_sum,
    SUM(shks_sum)              AS shks_sum,
    SUM(spend_sum)             AS spend_sum,
    SUM(spend_on_imp)          AS spend_on_imp,
    SUM(avg_pos_x_views)       AS apxv,
    SUM(avg_pos_x_clicks)      AS apxc,
    SUM(rows_with_impressions) AS rows_with_impressions,
    COUNT(DISTINCT day)                             AS days_with_data,
    COUNT(DISTINCT IF(spend_sum > 0, day, NULL))    AS days_with_spend
  FROM win
  GROUP BY nm_id, norm_query
),
camps AS (
  SELECT nm_id, norm_query,
         ARRAY_AGG(DISTINCT aid ORDER BY aid) AS advert_ids,
         COUNT(DISTINCT aid)                  AS advert_count
  FROM win, UNNEST(advert_ids) AS aid
  GROUP BY nm_id, norm_query
),
pair_prev AS (
  SELECT nm_id, norm_query,
         SUM(views_sum)     AS views_sum,
         SUM(clicks_sum)    AS clicks_sum,
         SUM(clicks_on_imp) AS clicks_on_imp,
         SUM(atbs_sum)      AS atbs_sum,
         SUM(orders_sum)    AS orders_sum,
         SUM(spend_sum)     AS spend_sum
  FROM prev
  GROUP BY nm_id, norm_query
),
sku AS (
  SELECT nm_id,
         SUM(views_sum)     AS views_sum,
         SUM(clicks_sum)    AS clicks_sum,
         SUM(clicks_on_imp) AS clicks_on_imp,
         SUM(atbs_sum)      AS atbs_sum,
         SUM(orders_sum)    AS orders_sum,
         SUM(spend_sum)     AS spend_sum,
         COUNT(*)           AS pair_count
  FROM pair GROUP BY nm_id
),
store AS (
  SELECT SUM(views_sum)     AS views_sum,
         SUM(clicks_sum)    AS clicks_sum,
         SUM(clicks_on_imp) AS clicks_on_imp,
         SUM(atbs_sum)      AS atbs_sum,
         SUM(orders_sum)    AS orders_sum,
         SUM(spend_sum)     AS spend_sum,
         COUNT(*)           AS pair_count
  FROM pair
),
-- 🔴 Ставка = ПОСЛЕДНИЙ канонический OK-снимок вообще, без привязки к окну.
--    История ставок начинается с первого снимка Ads-3 и назад не восстанавливается,
--    поэтому «ставки, действовавшей в окне» у нас нет и быть не может (§3.2).
bid_anchor AS (
  SELECT MAX(snapshot_date) AS snap
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_ADV_QUERY_BIDS`
  WHERE snapshot_status = 'OK'
),
bids AS (
  SELECT
    SAFE_CAST(b.nm_id AS INT64)                                       AS nm_id,
    b.norm_query,
    MIN(SAFE_CAST(b.bid AS FLOAT64))                                  AS bid_min,
    MAX(SAFE_CAST(b.bid AS FLOAT64))                                  AS bid_max,
    MIN(SAFE_CAST(b.bid_kopecks AS INT64))                            AS bid_kopecks_min,
    MAX(SAFE_CAST(b.bid_kopecks AS INT64))                            AS bid_kopecks_max,
    COUNT(DISTINCT SAFE_CAST(b.bid AS FLOAT64)) = 1                   AS bid_is_uniform,
    COUNT(DISTINCT b.advert_id)                                       AS bid_campaign_count,
    STRING_AGG(DISTINCT b.payment_type,    ',' ORDER BY b.payment_type)    AS payment_type,
    STRING_AGG(DISTINCT b.bid_type,        ',' ORDER BY b.bid_type)        AS bid_type,
    STRING_AGG(DISTINCT b.campaign_status, ',' ORDER BY b.campaign_status) AS campaign_status,
    SAFE.PARSE_DATE('%Y-%m-%d', MAX(b.snapshot_date))                 AS bid_snapshot_date,
    MAX(b.snapshot_ts)                                                AS bid_snapshot_ts,
    STRING_AGG(DISTINCT b.snapshot_status, ',')                       AS bid_snapshot_status
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_ADV_QUERY_BIDS` b, bid_anchor ba
  WHERE b.snapshot_status = 'OK'
    AND b.snapshot_date   = ba.snap
  GROUP BY 1, 2            -- ровно одна строка на ключ витрины → fan-out невозможен
),
base AS (
  SELECT
    a.as_of_date,
    90                                             AS window_days,
    p.nm_id, p.norm_query,
    c.advert_ids, c.advert_count,
    p.days_with_data, p.days_with_spend, p.rows_with_impressions,
    p.views_sum, p.clicks_sum, p.clicks_on_imp, p.atbs_sum, p.orders_sum,
    p.shks_sum, p.spend_sum, p.spend_on_imp, p.apxv, p.apxc,
    -- ── ex-self агрегаты SKU (baseline без самой пары) ──
    s.views_sum     - IFNULL(p.views_sum, 0)       AS x_views,
    s.clicks_sum    - p.clicks_sum                 AS x_clicks,
    s.clicks_on_imp - p.clicks_on_imp              AS x_clicks_on_imp,
    s.atbs_sum      - p.atbs_sum                   AS x_atbs,
    s.orders_sum    - p.orders_sum                 AS x_orders,
    s.spend_sum     - p.spend_sum                  AS x_spend,
    s.clicks_sum                                   AS sku_clicks_total,
    s.pair_count    - 1                            AS x_pair_count,
    -- ── ex-self агрегаты магазина ──
    st.views_sum     - IFNULL(p.views_sum, 0)      AS y_views,
    st.clicks_sum    - p.clicks_sum                AS y_clicks,
    st.clicks_on_imp - p.clicks_on_imp             AS y_clicks_on_imp,
    st.atbs_sum      - p.atbs_sum                  AS y_atbs,
    st.orders_sum    - p.orders_sum                AS y_orders,
    st.spend_sum     - p.spend_sum                 AS y_spend,
    st.pair_count    - 1                           AS y_pair_count,
    st.spend_on_imp_total                          AS store_spend_on_imp,
    st.spend_total                                 AS store_spend_total,
    -- ── предыдущее непересекающееся окно ──
    pp.views_sum     AS prev_views_sum,
    pp.clicks_sum    AS prev_clicks_sum,
    pp.clicks_on_imp AS prev_clicks_on_imp,
    pp.atbs_sum      AS prev_atbs_sum,
    pp.orders_sum    AS prev_orders_sum,
    pp.spend_sum     AS prev_spend_sum,
    -- ── контекст ставки ──
    b.bid_min, b.bid_max, b.bid_kopecks_min, b.bid_kopecks_max,
    b.bid_is_uniform, b.bid_campaign_count,
    b.payment_type, b.bid_type, b.campaign_status,
    b.bid_snapshot_date, b.bid_snapshot_ts, b.bid_snapshot_status
  FROM pair p
  CROSS JOIN anchor a
  CROSS JOIN (SELECT store.*,
                     (SELECT SUM(spend_on_imp) FROM pair) AS spend_on_imp_total,
                     (SELECT SUM(spend_sum)    FROM pair) AS spend_total
              FROM store) st
  JOIN      sku       s  ON s.nm_id = p.nm_id
  LEFT JOIN camps     c  ON c.nm_id = p.nm_id AND c.norm_query = p.norm_query
  LEFT JOIN pair_prev pp ON pp.nm_id = p.nm_id AND pp.norm_query = p.norm_query
  LEFT JOIN bids      b  ON b.nm_id = p.nm_id AND b.norm_query = p.norm_query
),
calc AS (
  SELECT
    base.*,
    -- ── метрики пары (называются по знаменателю) ──
    SAFE_DIVIDE(clicks_on_imp, views_sum)                  AS ctr,
    SAFE_DIVIDE(atbs_sum,      clicks_sum)                 AS cart_cr_clicks,
    SAFE_DIVIDE(orders_sum,    atbs_sum)                   AS order_cr_carts,
    SAFE_DIVIDE(orders_sum,    clicks_sum)                 AS order_cr_clicks,
    SAFE_DIVIDE(shks_sum,      orders_sum)                 AS buyout_ratio_shk,
    SAFE_DIVIDE(spend_sum,     clicks_sum)                 AS cpc_calc,
    -- 🔴 NULL, а не 0: «стоимость заказа не определена» ≠ «равна нулю»
    SAFE_DIVIDE(spend_sum,     NULLIF(orders_sum, 0))      AS cpo_ads,
    SAFE_DIVIDE(spend_on_imp,  views_sum) * 1000           AS cpm_calc,
    SAFE_DIVIDE(apxv,          views_sum)                  AS avg_pos_w_views,
    SAFE_DIVIDE(apxc,          clicks_sum)                 AS avg_pos_w_clicks,
    SAFE_DIVIDE(spend_sum,     store_spend_total)          AS spend_share_window,
    SAFE_DIVIDE(store_spend_on_imp, store_spend_total)     AS ctr_coverage_spend_share,
    -- ── монотонность воронки: значения НЕ чинятся, только помечаются ──
    ( (views_sum IS NULL OR clicks_on_imp <= views_sum)
      AND atbs_sum   <= clicks_sum
      AND orders_sum <= atbs_sum )                         AS funnel_monotonic,
    -- ── доказательность ──
    (IFNULL(views_sum, 0) >= 1000)                         AS can_compare_ctr,
    (clicks_sum >= 60)                                     AS can_judge_cart_cr,
    (clicks_sum >= 40)                                     AS can_judge_order_cr,
    (atbs_sum   >= 40)                                     AS can_judge_order_carts,
    (clicks_sum >= 20)                                     AS can_compare_cpc,
    CASE
      WHEN clicks_sum >= 40                                    THEN 'ACTIONABLE'
      WHEN clicks_sum >= 10 OR IFNULL(views_sum, 0) >= 1000    THEN 'OBSERVATIONAL'
      ELSE 'INSUFFICIENT'
    END                                                    AS evidence_status,
    FORMAT('clicks=%d;views=%s;atbs=%d',
           CAST(clicks_sum AS INT64),
           IFNULL(CAST(CAST(views_sum AS INT64) AS STRING), 'NULL'),
           CAST(atbs_sum AS INT64))                        AS evidence_reason,
    -- ── baseline по классам утверждений, всегда ex-self ──
    CASE WHEN x_views >= 1000 THEN 'SKU_EX_SELF'
         WHEN y_views >= 1000 THEN 'STORE_EX_SELF' END     AS baseline_ctr_level,
    CASE WHEN x_views >= 1000 THEN SAFE_DIVIDE(x_clicks_on_imp, x_views)
         WHEN y_views >= 1000 THEN SAFE_DIVIDE(y_clicks_on_imp, y_views) END AS baseline_ctr,
    CASE WHEN x_views >= 1000 THEN x_views
         WHEN y_views >= 1000 THEN y_views END             AS baseline_ctr_sample_views,
    CASE WHEN x_clicks >= 60 THEN 'SKU_EX_SELF'
         WHEN y_clicks >= 60 THEN 'STORE_EX_SELF' END      AS baseline_cart_level,
    CASE WHEN x_clicks >= 60 THEN SAFE_DIVIDE(x_atbs, x_clicks)
         WHEN y_clicks >= 60 THEN SAFE_DIVIDE(y_atbs, y_clicks) END AS baseline_cart_cr,
    CASE WHEN x_clicks >= 60 THEN x_clicks
         WHEN y_clicks >= 60 THEN y_clicks END             AS baseline_cart_sample_clicks,
    CASE WHEN x_clicks >= 40 THEN 'SKU_EX_SELF'
         WHEN y_clicks >= 40 THEN 'STORE_EX_SELF' END      AS baseline_order_level,
    CASE WHEN x_clicks >= 40 THEN SAFE_DIVIDE(x_orders, x_clicks)
         WHEN y_clicks >= 40 THEN SAFE_DIVIDE(y_orders, y_clicks) END AS baseline_order_cr,
    CASE WHEN x_clicks >= 40 THEN x_clicks
         WHEN y_clicks >= 40 THEN y_clicks END             AS baseline_order_sample_clicks,
    CASE WHEN x_atbs >= 40 THEN 'SKU_EX_SELF'
         WHEN y_atbs >= 40 THEN 'STORE_EX_SELF' END        AS baseline_order_carts_level,
    CASE WHEN x_atbs >= 40 THEN SAFE_DIVIDE(x_orders, x_atbs)
         WHEN y_atbs >= 40 THEN SAFE_DIVIDE(y_orders, y_atbs) END AS baseline_order_carts,
    CASE WHEN x_atbs >= 40 THEN x_atbs
         WHEN y_atbs >= 40 THEN y_atbs END                 AS baseline_order_carts_sample_atbs,
    CASE WHEN x_clicks >= 20 THEN 'SKU_EX_SELF'
         WHEN y_clicks >= 20 THEN 'STORE_EX_SELF' END      AS baseline_cpc_level,
    CASE WHEN x_clicks >= 20 THEN SAFE_DIVIDE(x_spend, x_clicks)
         WHEN y_clicks >= 20 THEN SAFE_DIVIDE(y_spend, y_clicks) END AS baseline_cpc,
    CASE WHEN x_clicks >= 20 THEN x_clicks
         WHEN y_clicks >= 20 THEN y_clicks END             AS baseline_cpc_sample_clicks,
    CASE WHEN x_clicks >= 40 AND x_orders > 0 THEN 'SKU_EX_SELF'
         WHEN y_clicks >= 40 AND y_orders > 0 THEN 'STORE_EX_SELF' END AS baseline_cpo_level,
    CASE WHEN x_clicks >= 40 AND x_orders > 0 THEN SAFE_DIVIDE(x_spend, x_orders)
         WHEN y_clicks >= 40 AND y_orders > 0 THEN SAFE_DIVIDE(y_spend, y_orders) END AS baseline_cpo,
    CASE WHEN x_clicks >= 40 AND x_orders > 0 THEN x_orders
         WHEN y_clicks >= 40 AND y_orders > 0 THEN y_orders END AS baseline_cpo_sample_orders,
    -- ── предыдущее окно ──
    SAFE_DIVIDE(prev_clicks_on_imp, prev_views_sum)        AS prev_ctr,
    SAFE_DIVIDE(prev_atbs_sum,      prev_clicks_sum)       AS prev_cart_cr_clicks,
    SAFE_DIVIDE(prev_orders_sum,    prev_clicks_sum)       AS prev_order_cr_clicks,
    SAFE_DIVIDE(prev_spend_sum,     prev_clicks_sum)       AS prev_cpc_calc,
    SAFE_DIVIDE(prev_spend_sum,     NULLIF(prev_orders_sum, 0)) AS prev_cpo_ads
  FROM base
)
SELECT
  as_of_date, window_days, nm_id, norm_query,
  advert_ids, advert_count,
  days_with_data, days_with_spend, rows_with_impressions,
  views_sum, clicks_sum, clicks_on_imp, atbs_sum, orders_sum, shks_sum,
  spend_sum, spend_on_imp,
  ctr, cart_cr_clicks, order_cr_carts, order_cr_clicks, buyout_ratio_shk,
  cpc_calc, cpo_ads, cpm_calc, avg_pos_w_views, avg_pos_w_clicks,
  spend_share_window, ctr_coverage_spend_share,
  funnel_monotonic,
  can_compare_ctr, can_judge_cart_cr, can_judge_order_cr,
  can_judge_order_carts, can_compare_cpc,
  evidence_status, evidence_reason,
  baseline_ctr,          baseline_ctr_level,          baseline_ctr_sample_views,
  baseline_cart_cr,      baseline_cart_level,         baseline_cart_sample_clicks,
  baseline_order_cr,     baseline_order_level,        baseline_order_sample_clicks,
  baseline_order_carts,  baseline_order_carts_level,  baseline_order_carts_sample_atbs,
  baseline_cpc,          baseline_cpc_level,          baseline_cpc_sample_clicks,
  baseline_cpo,          baseline_cpo_level,          baseline_cpo_sample_orders,
  IFNULL((SELECT STRING_AGG(DISTINCT lv, ',' ORDER BY lv)
          FROM UNNEST([baseline_ctr_level, baseline_cart_level, baseline_order_level,
                       baseline_order_carts_level, baseline_cpc_level, baseline_cpo_level]) lv
          WHERE lv IS NOT NULL), 'NONE')               AS baseline_level,
  -- 🔴 почему класс утверждений не получил SKU_EX_SELF baseline (ред. 2 §7.1).
  --    NULL = все классы сравниваются с собственным SKU без самой пары.
  (SELECT STRING_AGG(r, ',' ORDER BY r) FROM UNNEST([
     IF(baseline_ctr_level          IS NULL, 'CTR:NO_BASELINE',
     IF(baseline_ctr_level          = 'STORE_EX_SELF', 'CTR:SKU_SAMPLE_TOO_SMALL', NULL)),
     IF(baseline_cart_level         IS NULL, 'CART:NO_BASELINE',
     IF(baseline_cart_level         = 'STORE_EX_SELF', 'CART:SKU_SAMPLE_TOO_SMALL', NULL)),
     IF(baseline_order_level        IS NULL, 'ORDER_CR:NO_BASELINE',
     IF(baseline_order_level        = 'STORE_EX_SELF', 'ORDER_CR:SKU_SAMPLE_TOO_SMALL', NULL)),
     IF(baseline_order_carts_level  IS NULL, 'ORDER_CARTS:NO_BASELINE',
     IF(baseline_order_carts_level  = 'STORE_EX_SELF', 'ORDER_CARTS:SKU_SAMPLE_TOO_SMALL', NULL)),
     IF(baseline_cpc_level          IS NULL, 'CPC:NO_BASELINE',
     IF(baseline_cpc_level          = 'STORE_EX_SELF', 'CPC:SKU_SAMPLE_TOO_SMALL', NULL)),
     IF(baseline_cpo_level          IS NULL, 'CPO:NO_BASELINE',
     IF(baseline_cpo_level          = 'STORE_EX_SELF', 'CPO:SKU_SAMPLE_TOO_SMALL', NULL))
   ]) r WHERE r IS NOT NULL)                          AS baseline_fallback_reason,
  x_clicks                                             AS sku_ex_self_sample_clicks,
  x_pair_count                                         AS sku_ex_self_pair_count,
  sku_clicks_total,
  SAFE_DIVIDE(ctr,             baseline_ctr)           AS ctr_vs_baseline,
  SAFE_DIVIDE(cart_cr_clicks,  baseline_cart_cr)       AS cart_cr_vs_baseline,
  SAFE_DIVIDE(order_cr_clicks, baseline_order_cr)      AS order_cr_vs_baseline,
  SAFE_DIVIDE(order_cr_carts,  baseline_order_carts)   AS order_carts_vs_baseline,
  SAFE_DIVIDE(cpc_calc,        baseline_cpc)           AS cpc_vs_baseline,
  SAFE_DIVIDE(cpo_ads,         baseline_cpo)           AS cpo_vs_baseline,
  bid_min, bid_max, bid_kopecks_min, bid_kopecks_max,
  bid_is_uniform, bid_campaign_count,
  payment_type, bid_type, campaign_status,
  bid_snapshot_date, bid_snapshot_ts, bid_snapshot_status,
  DATE_DIFF(bid_snapshot_date, as_of_date, DAY)        AS bid_snapshot_offset_days,
  (bid_snapshot_date > as_of_date)                     AS bid_is_after_stats_as_of,
  prev_views_sum, prev_clicks_sum, prev_atbs_sum, prev_orders_sum, prev_spend_sum,
  prev_ctr, prev_cart_cr_clicks, prev_order_cr_clicks, prev_cpc_calc, prev_cpo_ads,
  -- 🔴 дельта считается ТОЛЬКО если оба окна проходят гейт: сравнение шума с шумом
  --    даёт шум, но выглядит как вывод (§5)
  IF(clicks_sum >= 20 AND prev_clicks_sum >= 20,
     SAFE_DIVIDE(spend_sum, clicks_sum) - SAFE_DIVIDE(prev_spend_sum, prev_clicks_sum),
     NULL)                                             AS delta_cpc,
  IF(clicks_sum >= 40 AND prev_clicks_sum >= 40,
     SAFE_DIVIDE(orders_sum, clicks_sum) - SAFE_DIVIDE(prev_orders_sum, prev_clicks_sum),
     NULL)                                             AS delta_order_cr_clicks,
  IF(prev_spend_sum IS NULL, NULL, spend_sum - prev_spend_sum) AS delta_spend
FROM calc;


-- ────────────────────────────────────────────────────────────
-- 4/5 · V_ADS_FUNNEL_SKU_28D — свёртка по SKU + экономика витрины (pre_cogs)
--       🔴 Query-level строки с экономикой SKU НЕ соединяются: разложить
--       выручку SKU по запросам нечем, любая аллокация была бы выдумкой (§3.3).
-- ────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_ADS_FUNNEL_SKU_28D` AS
WITH q AS (
  SELECT
    as_of_date, window_days, nm_id,
    COUNT(*)                                              AS queries_total,
    COUNTIF(evidence_status = 'ACTIONABLE')               AS queries_actionable,
    COUNTIF(evidence_status = 'OBSERVATIONAL')            AS queries_observational,
    COUNTIF(evidence_status = 'INSUFFICIENT')             AS queries_insufficient,
    COUNTIF(orders_sum = 0 AND spend_sum > 0)             AS queries_zero_order,
    SUM(IF(orders_sum = 0 AND spend_sum > 0, spend_sum, 0)) AS zero_order_spend_rub,
    SUM(views_sum)     AS views_sum,
    SUM(clicks_sum)    AS clicks_sum,
    SUM(clicks_on_imp) AS clicks_on_imp,
    SUM(atbs_sum)      AS atbs_sum,
    SUM(orders_sum)    AS orders_sum,
    SUM(shks_sum)      AS shks_sum,
    SUM(spend_sum)     AS query_spend_rub,
    SUM(spend_on_imp)  AS query_spend_on_imp_rub,
    COUNTIF(NOT funnel_monotonic)                         AS queries_non_monotonic,
    COUNTIF(bid_snapshot_date IS NOT NULL)                AS queries_with_bid
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_ADS_FUNNEL_QUERY_28D`
  GROUP BY as_of_date, window_days, nm_id
),
m AS (
  SELECT
    SAFE_CAST(d.nm_id AS INT64)                           AS nm_id,
    -- Stage 3B (20.08.2026): имя уточнено. Величина НЕ изменилась — это по-прежнему
    --   ad_spend витрины, то есть АТРИБУТИРОВАННЫЙ расход. Доля запросов считается от
    --   атрибуции осознанно: query stats и campaign stats — один и тот же источник,
    --   а биллинг живёт в другом разрезе, и деление одного на другое было бы подлогом.
    SUM(d.ad_spend)                                       AS mart_ad_spend_attributed_rub,
    SUM(d.orders_rub)                                     AS orders_rub,
    SUM(d.buyouts_rub)                                    AS buyouts_rub,
    SUM(d.orders_qty)                                     AS orders_qty,
    SUM(d.hybrid_day_contribution_pre_cogs)               AS hybrid_contribution_pre_cogs,
    SUM(d.settlement_day_contribution_pre_cogs)           AS settlement_contribution_pre_cogs
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.MART_SKU_DAILY` d, (SELECT MAX(as_of_date) AS as_of FROM q) a
  WHERE d.day >  DATE_SUB(a.as_of, INTERVAL 28 DAY)
    AND d.day <= a.as_of
  GROUP BY 1
)
SELECT
  q.as_of_date, q.window_days, q.nm_id,
  q.queries_total, q.queries_actionable, q.queries_observational, q.queries_insufficient,
  q.queries_zero_order, q.zero_order_spend_rub, q.queries_non_monotonic, q.queries_with_bid,
  q.views_sum, q.clicks_sum, q.clicks_on_imp, q.atbs_sum, q.orders_sum, q.shks_sum,
  q.query_spend_rub, q.query_spend_on_imp_rub,
  SAFE_DIVIDE(q.clicks_on_imp, q.views_sum)              AS ctr,
  SAFE_DIVIDE(q.atbs_sum,      q.clicks_sum)             AS cart_cr_clicks,
  SAFE_DIVIDE(q.orders_sum,    q.atbs_sum)               AS order_cr_carts,
  SAFE_DIVIDE(q.orders_sum,    q.clicks_sum)             AS order_cr_clicks,
  SAFE_DIVIDE(q.query_spend_rub, q.clicks_sum)           AS cpc_calc,
  SAFE_DIVIDE(q.query_spend_rub, NULLIF(q.orders_sum, 0)) AS cpo_ads,
  SAFE_DIVIDE(q.query_spend_on_imp_rub, q.query_spend_rub) AS ctr_coverage_spend_share,
  m.mart_ad_spend_attributed_rub,
  -- 🔴 доля расхода SKU, вообще объяснимая на уровне запросов (§10.2):
  --    сумма по запросам никогда не сойдётся с рекламным расходом
  SAFE_DIVIDE(q.query_spend_rub, m.mart_ad_spend_attributed_rub) AS query_spend_share_of_total,
  m.orders_rub, m.buyouts_rub, m.orders_qty,
  -- 🔴 pre_cogs: себестоимости в системе нет, прибыль и маржа не вычисляются (§10.3)
  m.hybrid_contribution_pre_cogs,
  m.settlement_contribution_pre_cogs
FROM q
LEFT JOIN m ON m.nm_id = q.nm_id;


-- ────────────────────────────────────────────────────────────
-- 5/5 · V_ADS_FUNNEL_SIGNALS — по строке на сработавший сигнал, оба окна
--       🔴 POTENTIAL_WASTE — не сигнал, а ЯРЛЫК, который ZERO_ORDER_SPEND
--       получает только при evidence_status = 'ACTIONABLE' (§8.1).
--       Сигналы о конверсии не выставляются немонотонным строкам и строкам
--       без показов (для CTR) — не «как OK», а именно не выставляются.
-- ────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_ADS_FUNNEL_SIGNALS` AS
WITH u AS (
  SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_ADS_FUNNEL_QUERY_28D`
  UNION ALL
  SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_ADS_FUNNEL_QUERY_90D`
),
sig AS (
  SELECT u.*, s.signal_code, s.fired, s.signal_strength, s.funnel_stage
  FROM u, UNNEST([
    STRUCT('CTR_BELOW_BASELINE'            AS signal_code,
           (can_compare_ctr AND funnel_monotonic AND baseline_ctr IS NOT NULL
            AND ctr_vs_baseline < 0.7)     AS fired,
           'STRONG'                        AS signal_strength,
           'IMPRESSION_TO_CLICK'           AS funnel_stage),
    STRUCT('CTR_ABOVE_BASELINE',
           (can_compare_ctr AND funnel_monotonic AND baseline_ctr IS NOT NULL
            AND ctr_vs_baseline > 1.3),
           'STRONG', 'IMPRESSION_TO_CLICK'),
    STRUCT('CART_CR_BELOW_BASELINE',
           (can_judge_cart_cr AND funnel_monotonic AND baseline_cart_cr IS NOT NULL
            AND cart_cr_vs_baseline < 0.7),
           'STRONG', 'CLICK_TO_CART'),
    STRUCT('ORDER_CR_CARTS_BELOW_BASELINE',
           (can_judge_order_carts AND funnel_monotonic AND baseline_order_carts IS NOT NULL
            AND order_carts_vs_baseline < 0.7),
           'STRONG', 'CART_TO_ORDER'),
    STRUCT('CPC_ABOVE_BASELINE',
           (can_compare_cpc AND baseline_cpc IS NOT NULL AND cpc_vs_baseline > 1.3),
           'MEDIUM', 'TRAFFIC_PRICE'),
    STRUCT('CPC_BELOW_BASELINE',
           (can_compare_cpc AND baseline_cpc IS NOT NULL AND cpc_vs_baseline < 0.7),
           'MEDIUM', 'TRAFFIC_PRICE'),
    STRUCT('CPO_BELOW_BASELINE',
           (can_judge_order_cr AND funnel_monotonic AND orders_sum > 0
            AND baseline_cpo IS NOT NULL AND cpo_vs_baseline < 0.7),
           'MEDIUM', 'ORDER_PRICE'),
    STRUCT('ZERO_ORDER_SPEND',
           (orders_sum = 0 AND spend_sum > 0),
           'BY_EVIDENCE', 'NO_ATTRIBUTED_ORDER')
  ]) AS s
  WHERE s.fired
)
SELECT
  as_of_date, window_days, nm_id, norm_query, signal_code, funnel_stage,
  IF(signal_strength = 'BY_EVIDENCE', evidence_status, signal_strength) AS signal_strength,
  evidence_status, evidence_reason,
  IF(signal_code = 'ZERO_ORDER_SPEND' AND evidence_status = 'ACTIONABLE',
     'POTENTIAL_WASTE', NULL)                          AS signal_label,
  spend_sum, spend_share_window, clicks_sum, views_sum, atbs_sum, orders_sum,
  ctr, cart_cr_clicks, order_cr_carts, order_cr_clicks, cpc_calc, cpo_ads,
  ctr_vs_baseline, cart_cr_vs_baseline, order_cr_vs_baseline,
  order_carts_vs_baseline, cpc_vs_baseline, cpo_vs_baseline,
  baseline_level, baseline_fallback_reason, sku_ex_self_pair_count,
  advert_count, days_with_data, days_with_spend, funnel_monotonic,
  bid_min, bid_max, bid_is_uniform, bid_snapshot_date, bid_snapshot_offset_days,
  bid_is_after_stats_as_of
FROM sig;
