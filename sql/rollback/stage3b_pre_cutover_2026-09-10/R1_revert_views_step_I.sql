-- ============================================================================
-- EVETIS · Stage 3B ROLLBACK · R1 · вернуть 8 вью шага I (ads4_funnel_v1 + dashboard_layer_v1)
-- Снято с production 2026-09-10 до шага B1 (INFORMATION_SCHEMA.*.ddl, read-only).
-- Это ТОЧНОЕ состояние до cutover, а не реконструкция из Git.
-- Манифест и порядок: docs/ops/STAGE_B_CUTOVER_ROLLBACK_MANIFEST.md
-- ============================================================================
-- Первым: прежние тела читают только колонки, которые есть и в прежней, и в новой
-- (надмножество) схеме MART_SKU_DAILY, поэтому компилируются ДО отката витрины.

-- V_ADS_FUNNEL_QUERY_DAILY · sha256(view_definition) = 6335e873929ca0a437fe1926e4491ba80d207d1dd3c644a48440ecd0edf68071
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_ADS_FUNNEL_QUERY_DAILY`
AS WITH src AS (
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

-- V_ADS_FUNNEL_QUERY_28D · sha256(view_definition) = e8c32bc025a5aa40837249a1e43635330b9aa1bffdc31bd0e7deaeec2cc75115
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_ADS_FUNNEL_QUERY_28D`
AS WITH anchor AS (
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
  GROUP BY 1, 2
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
    s.views_sum     - IFNULL(p.views_sum, 0)       AS x_views,
    s.clicks_sum    - p.clicks_sum                 AS x_clicks,
    s.clicks_on_imp - p.clicks_on_imp              AS x_clicks_on_imp,
    s.atbs_sum      - p.atbs_sum                   AS x_atbs,
    s.orders_sum    - p.orders_sum                 AS x_orders,
    s.spend_sum     - p.spend_sum                  AS x_spend,
    s.clicks_sum                                   AS sku_clicks_total,
    s.pair_count    - 1                            AS x_pair_count,
    st.views_sum     - IFNULL(p.views_sum, 0)      AS y_views,
    st.clicks_sum    - p.clicks_sum                AS y_clicks,
    st.clicks_on_imp - p.clicks_on_imp             AS y_clicks_on_imp,
    st.atbs_sum      - p.atbs_sum                  AS y_atbs,
    st.orders_sum    - p.orders_sum                AS y_orders,
    st.spend_sum     - p.spend_sum                 AS y_spend,
    st.pair_count    - 1                           AS y_pair_count,
    st.spend_on_imp_total                          AS store_spend_on_imp,
    st.spend_total                                 AS store_spend_total,
    pp.views_sum     AS prev_views_sum,
    pp.clicks_sum    AS prev_clicks_sum,
    pp.clicks_on_imp AS prev_clicks_on_imp,
    pp.atbs_sum      AS prev_atbs_sum,
    pp.orders_sum    AS prev_orders_sum,
    pp.spend_sum     AS prev_spend_sum,
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
    SAFE_DIVIDE(clicks_on_imp, views_sum)                  AS ctr,
    SAFE_DIVIDE(atbs_sum,      clicks_sum)                 AS cart_cr_clicks,
    SAFE_DIVIDE(orders_sum,    atbs_sum)                   AS order_cr_carts,
    SAFE_DIVIDE(orders_sum,    clicks_sum)                 AS order_cr_clicks,
    SAFE_DIVIDE(shks_sum,      orders_sum)                 AS buyout_ratio_shk,
    SAFE_DIVIDE(spend_sum,     clicks_sum)                 AS cpc_calc,
    SAFE_DIVIDE(spend_sum,     NULLIF(orders_sum, 0))      AS cpo_ads,
    SAFE_DIVIDE(spend_on_imp,  views_sum) * 1000           AS cpm_calc,
    SAFE_DIVIDE(apxv,          views_sum)                  AS avg_pos_w_views,
    SAFE_DIVIDE(apxc,          clicks_sum)                 AS avg_pos_w_clicks,
    SAFE_DIVIDE(spend_sum,     store_spend_total)          AS spend_share_window,
    SAFE_DIVIDE(store_spend_on_imp, store_spend_total)     AS ctr_coverage_spend_share,
    ( (views_sum IS NULL OR clicks_on_imp <= views_sum)
      AND atbs_sum   <= clicks_sum
      AND orders_sum <= atbs_sum )                         AS funnel_monotonic,
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
  IF(clicks_sum >= 20 AND prev_clicks_sum >= 20,
     SAFE_DIVIDE(spend_sum, clicks_sum) - SAFE_DIVIDE(prev_spend_sum, prev_clicks_sum),
     NULL)                                             AS delta_cpc,
  IF(clicks_sum >= 40 AND prev_clicks_sum >= 40,
     SAFE_DIVIDE(orders_sum, clicks_sum) - SAFE_DIVIDE(prev_orders_sum, prev_clicks_sum),
     NULL)                                             AS delta_order_cr_clicks,
  IF(prev_spend_sum IS NULL, NULL, spend_sum - prev_spend_sum) AS delta_spend
FROM calc;

-- V_ADS_FUNNEL_QUERY_90D · sha256(view_definition) = c3b83d5b16fa9bdf1f9b824cc7ec726cc0480711518c66284a0279ee33523ef6
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_ADS_FUNNEL_QUERY_90D`
AS WITH anchor AS (
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
  GROUP BY 1, 2
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
    s.views_sum     - IFNULL(p.views_sum, 0)       AS x_views,
    s.clicks_sum    - p.clicks_sum                 AS x_clicks,
    s.clicks_on_imp - p.clicks_on_imp              AS x_clicks_on_imp,
    s.atbs_sum      - p.atbs_sum                   AS x_atbs,
    s.orders_sum    - p.orders_sum                 AS x_orders,
    s.spend_sum     - p.spend_sum                  AS x_spend,
    s.clicks_sum                                   AS sku_clicks_total,
    s.pair_count    - 1                            AS x_pair_count,
    st.views_sum     - IFNULL(p.views_sum, 0)      AS y_views,
    st.clicks_sum    - p.clicks_sum                AS y_clicks,
    st.clicks_on_imp - p.clicks_on_imp             AS y_clicks_on_imp,
    st.atbs_sum      - p.atbs_sum                  AS y_atbs,
    st.orders_sum    - p.orders_sum                AS y_orders,
    st.spend_sum     - p.spend_sum                 AS y_spend,
    st.pair_count    - 1                           AS y_pair_count,
    st.spend_on_imp_total                          AS store_spend_on_imp,
    st.spend_total                                 AS store_spend_total,
    pp.views_sum     AS prev_views_sum,
    pp.clicks_sum    AS prev_clicks_sum,
    pp.clicks_on_imp AS prev_clicks_on_imp,
    pp.atbs_sum      AS prev_atbs_sum,
    pp.orders_sum    AS prev_orders_sum,
    pp.spend_sum     AS prev_spend_sum,
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
    SAFE_DIVIDE(clicks_on_imp, views_sum)                  AS ctr,
    SAFE_DIVIDE(atbs_sum,      clicks_sum)                 AS cart_cr_clicks,
    SAFE_DIVIDE(orders_sum,    atbs_sum)                   AS order_cr_carts,
    SAFE_DIVIDE(orders_sum,    clicks_sum)                 AS order_cr_clicks,
    SAFE_DIVIDE(shks_sum,      orders_sum)                 AS buyout_ratio_shk,
    SAFE_DIVIDE(spend_sum,     clicks_sum)                 AS cpc_calc,
    SAFE_DIVIDE(spend_sum,     NULLIF(orders_sum, 0))      AS cpo_ads,
    SAFE_DIVIDE(spend_on_imp,  views_sum) * 1000           AS cpm_calc,
    SAFE_DIVIDE(apxv,          views_sum)                  AS avg_pos_w_views,
    SAFE_DIVIDE(apxc,          clicks_sum)                 AS avg_pos_w_clicks,
    SAFE_DIVIDE(spend_sum,     store_spend_total)          AS spend_share_window,
    SAFE_DIVIDE(store_spend_on_imp, store_spend_total)     AS ctr_coverage_spend_share,
    ( (views_sum IS NULL OR clicks_on_imp <= views_sum)
      AND atbs_sum   <= clicks_sum
      AND orders_sum <= atbs_sum )                         AS funnel_monotonic,
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
  IF(clicks_sum >= 20 AND prev_clicks_sum >= 20,
     SAFE_DIVIDE(spend_sum, clicks_sum) - SAFE_DIVIDE(prev_spend_sum, prev_clicks_sum),
     NULL)                                             AS delta_cpc,
  IF(clicks_sum >= 40 AND prev_clicks_sum >= 40,
     SAFE_DIVIDE(orders_sum, clicks_sum) - SAFE_DIVIDE(prev_orders_sum, prev_clicks_sum),
     NULL)                                             AS delta_order_cr_clicks,
  IF(prev_spend_sum IS NULL, NULL, spend_sum - prev_spend_sum) AS delta_spend
FROM calc;

-- V_ADS_FUNNEL_SKU_28D · sha256(view_definition) = 008cca3b970397750607c73837b4076b334959e52c205e85f3b82483abb61b6c
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_ADS_FUNNEL_SKU_28D`
AS WITH q AS (
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
    SUM(d.ad_spend)                                       AS mart_ad_spend_rub,
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
  m.mart_ad_spend_rub,
  SAFE_DIVIDE(q.query_spend_rub, m.mart_ad_spend_rub)    AS query_spend_share_of_total,
  m.orders_rub, m.buyouts_rub, m.orders_qty,
  m.hybrid_contribution_pre_cogs,
  m.settlement_contribution_pre_cogs
FROM q
LEFT JOIN m ON m.nm_id = q.nm_id;

-- V_ADS_FUNNEL_SIGNALS · sha256(view_definition) = 3b70e54d3f64bcfe2ee24a7679029c27552e0d796f1e5cf5e521b10db1b635ab
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_ADS_FUNNEL_SIGNALS`
AS WITH u AS (
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

-- V_DATA_FRESHNESS · sha256(view_definition) = 32c52cfadea5c28d23c3efebcda5de5c6b7dc2c3b8793ee8a1778369492eff92
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_DATA_FRESHNESS`
AS WITH
-- реестр слоёв и порогов: порог есть свойство слоя, а не ветка CASE (§1.8)
layers AS (
  SELECT * FROM UNNEST([
    -- 🔴 у ads_query_stats СВОЙ журнал (§1.3 ред. 5) и осознанно НЕ задан порог часов
    --    прогона: история лога 12 ч короче ожидаемого интервала между прогонами,
    --    max_gap неизмерим, правило §1.8 неприменимо. Гейтит только возраст данных.
    STRUCT(
      'ads_query_stats'     AS layer_code, 'ads'     AS layer_group,
      'ads_query_stats_log' AS run_source,
      3                     AS data_age_ok_days,      CAST(NULL AS INT64) AS success_age_ok_hours),
    ('ads_query_bids',       'ads',     'ads',      1, 36),
    ('ads_costs',            'ads',     'ads',      2, 36),
    ('ads_fullstats',        'ads',     'ads',      2, 36),
    ('orders',               'ops',     'orders',   1,  3),
    ('sales',                'ops',     'sales',    2,  4),
    -- 🔴 журнал боевого пути (Apps Script), а не LOADER_RUNS — инвариант 11
    ('stocks',               'ops',     'stocks_snapshots', 1, 36),
    ('finance',              'finance', 'finance',  3, 20),
    ('mart_sku_daily',       'mart',    'mart',     2, 36),
    ('fact_ads_costs_daily', 'mart',    'mart',     2, 36),
    ('fact_ads_sku_daily',   'mart',    'mart',     2, 36),
    ('ref_sku_master',       'ref',     'ref',      CAST(NULL AS INT64), 72)
  ])
),

-- 🔴 нормализация форматов даты — здесь, один раз (§1.2)
data_as_of AS (
  SELECT 'ads_query_stats' AS layer_code,
         (SELECT MAX(SAFE.PARSE_DATE('%Y-%m-%d', period_from))
            FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_ADV_QUERY_STATS`) AS data_as_of
  UNION ALL SELECT 'ads_query_bids',
         (SELECT MAX(SAFE.PARSE_DATE('%Y-%m-%d', snapshot_date))
            FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_ADV_QUERY_BIDS`)
  UNION ALL SELECT 'ads_costs',
         (SELECT MAX(SAFE.PARSE_DATE('%Y-%m-%d', updDate))
            FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_ADV_COSTS`)
  UNION ALL SELECT 'ads_fullstats',
         (SELECT MAX(SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(date, 1, 10)))
            FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_ADV_CAMPAIGN_STATS`)
  UNION ALL SELECT 'orders',
         (SELECT MAX(order_date) FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ORDERS`)
  UNION ALL SELECT 'sales',
         (SELECT MAX(sale_date) FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_SALES`)
  UNION ALL SELECT 'stocks',
         (SELECT MAX(snapshot_date) FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_STOCKS_SNAPSHOT`)
  UNION ALL SELECT 'finance',
         (SELECT MAX(finance_date) FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_FINANCE`)
  UNION ALL SELECT 'mart_sku_daily',
         (SELECT MAX(day) FROM `project-fa311fc0-4d87-4781-986.wb_mart.MART_SKU_DAILY`)
  UNION ALL SELECT 'fact_ads_costs_daily',
         (SELECT MAX(date) FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ADS_COSTS_DAILY`)
  UNION ALL SELECT 'fact_ads_sku_daily',
         (SELECT MAX(date) FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ADS_SKU_DAILY`)
  UNION ALL SELECT 'ref_sku_master', CAST(NULL AS DATE)
),

-- built_at: справочная колонка. У сырых ads-слоёв её физически нет — остаётся NULL (§1.6)
built AS (
  SELECT 'orders' AS layer_code,
         (SELECT MAX(built_at) FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ORDERS`) AS built_at
  UNION ALL SELECT 'sales',
         (SELECT MAX(built_at) FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_SALES`)
  UNION ALL SELECT 'stocks',
         (SELECT MAX(built_at) FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_STOCKS_SNAPSHOT`)
  UNION ALL SELECT 'finance',
         (SELECT MAX(built_at) FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_FINANCE`)
  UNION ALL SELECT 'mart_sku_daily',
         (SELECT MAX(built_at) FROM `project-fa311fc0-4d87-4781-986.wb_mart.MART_SKU_DAILY`)
  UNION ALL SELECT 'fact_ads_costs_daily',
         (SELECT MAX(built_at) FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ADS_COSTS_DAILY`)
  UNION ALL SELECT 'fact_ads_sku_daily',
         (SELECT MAX(built_at) FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ADS_SKU_DAILY`)
  UNION ALL SELECT 'ref_sku_master',
         (SELECT MAX(d._synced_at)
            FROM `project-fa311fc0-4d87-4781-986.wb_raw.REF_SKU_MASTER_DATA` d
            JOIN `project-fa311fc0-4d87-4781-986.wb_raw.REF_ACTIVE_VERSION` v
              ON d.ref_run_id = v.active_ref_run_id)
),

-- 🔴 собственный журнал Ads-2 (§1.3 ред. 5). Строка лога = ЗАПРОШЕННЫЕ СУТКИ,
--    поэтому попытка собирается по run_id: один прогон = одна попытка.
--    Статус прогона — худший из суточных: неизвестный → FAILED → PARTIAL → OK.
--    load_ts здесь STRING 'YYYY-MM-DD HH:MM:SS' в UTC; started/completed не разделены,
--    берём границы прогона. Ни один суточный статус не теряется.
ads_query_stats_runs AS (
  SELECT
    run_id,
    MIN(SAFE.PARSE_TIMESTAMP('%Y-%m-%d %H:%M:%S', load_ts)) AS started_at,
    MAX(SAFE.PARSE_TIMESTAMP('%Y-%m-%d %H:%M:%S', load_ts)) AS completed_at,
    COALESCE(
      MAX(IF(status NOT IN ('OK', 'PARTIAL', 'FAILED', 'ERROR'), status, NULL)),
      MAX(IF(status IN ('FAILED', 'ERROR'), status, NULL)),
      MAX(IF(status = 'PARTIAL', status, NULL)),
      'OK') AS raw_status
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_ADV_QUERY_STATS_RUNS`
  GROUP BY run_id
),

-- run-логи, приведённые к общей схеме. `selftest` не выбирается вовсе (§1.3)
run_attempts AS (
  SELECT 'ads_query_stats_log' AS run_source, run_id, started_at, completed_at, raw_status
    FROM ads_query_stats_runs
  UNION ALL
  SELECT 'ads', run_id, started_at, completed_at, status
    FROM `project-fa311fc0-4d87-4781-986.wb_raw.INGEST_RUNS` WHERE loader_name = 'ads'
  UNION ALL
  SELECT 'orders', run_id, started_at, completed_at, status
    FROM `project-fa311fc0-4d87-4781-986.wb_raw.INGEST_RUNS` WHERE loader_name = 'orders'
  UNION ALL
  SELECT 'sales', run_id, started_at, completed_at, status
    FROM `project-fa311fc0-4d87-4781-986.wb_raw.INGEST_RUNS` WHERE loader_name = 'sales'
  UNION ALL
  -- 🔴 Остатки: журнал БОЕВОГО загрузчика (Apps Script), инвариант 11.
  --    Грейн строки = снимок, `snapshot_id` уникален (39/39 на 19.08.2026), NULL нет.
  --    Наблюдённые статусы за всю историю — только COMPLETE (38) и ERROR (1), оба
  --    внутри карты §1.5, ветка UNMAPPED недостижима на текущих данных.
  --    ⚠️ Строка пишется по завершении прогона, промежуточного STARTED нет — значит
  --    состояние STUCK для этого слоя не наблюдаемо по построению. Зависший прогон
  --    ловится ростом `success_age_hours` против порога 36 ч, а не `run_state`.
  SELECT 'stocks_snapshots', snapshot_id, started_at, completed_at, status
    FROM `project-fa311fc0-4d87-4781-986.wb_raw.WB_STOCKS_SNAPSHOTS`
  UNION ALL
  SELECT 'mart', run_id, started_at, completed_at, status
    FROM `project-fa311fc0-4d87-4781-986.wb_raw.LOADER_RUNS` WHERE loader_name = 'mart'
  UNION ALL
  SELECT 'finance', run_id, started_at, finished_at, status
    FROM `project-fa311fc0-4d87-4781-986.wb_raw.FINANCE_LOADER_RUNS`
  UNION ALL
  SELECT 'ref', run_id, started_at, finished_at, status
    FROM `project-fa311fc0-4d87-4781-986.wb_raw.REF_SYNC_RUNS`
),

-- карта нормализации статусов (§1.5). UNMAPPED = статус вне карты: падаем громко
attempts AS (
  SELECT
    l.layer_code,
    r.run_source,
    r.run_id,
    r.started_at,
    r.completed_at,
    r.raw_status,
    CASE
      WHEN r.raw_status IN ('COMPLETE', 'OK')   THEN 'SUCCESS'
      WHEN r.raw_status = 'OK_NO_NEW'           THEN 'SUCCESS_EMPTY'
      WHEN r.raw_status = 'PARTIAL'             THEN 'PARTIAL'
      WHEN r.raw_status IN ('ERROR', 'FAILED')  THEN 'FAILED'
      WHEN r.raw_status = 'STARTED'
           AND TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), r.started_at, HOUR) <= 6 THEN 'RUNNING'
      WHEN r.raw_status = 'STARTED'             THEN 'STUCK'
      ELSE 'UNMAPPED'
    END AS run_state
  FROM layers l
  JOIN run_attempts r ON r.run_source = l.run_source
),

-- 🔴 последняя попытка — целая строка
attempt_ranked AS (
  SELECT *, ROW_NUMBER() OVER (
             PARTITION BY layer_code
             ORDER BY started_at DESC, run_id DESC
           ) AS rn
  FROM attempts
),
last_attempt AS (SELECT * FROM attempt_ranked WHERE rn = 1),

-- 🔴 последний успех — тоже целая строка, тем же порядком. Агрегатов нет (§1.5)
success_ranked AS (
  SELECT *, ROW_NUMBER() OVER (
             PARTITION BY layer_code
             ORDER BY started_at DESC, run_id DESC
           ) AS rn
  FROM attempts
  WHERE run_state IN ('SUCCESS', 'SUCCESS_EMPTY')
),
last_success AS (SELECT * FROM success_ranked WHERE rn = 1),

base AS (
  SELECT
    l.layer_code,
    l.layer_group,
    l.run_source,
    d.data_as_of,
    IF(d.data_as_of IS NULL, NULL,
       DATE_DIFF(CURRENT_DATE('Europe/Moscow'), d.data_as_of, DAY))      AS data_age_days,
    l.data_age_ok_days,
    COALESCE(a.run_state, 'NO_RUN')                                     AS run_state,
    a.raw_status                                                        AS last_attempt_status,
    a.run_id                                                            AS last_attempt_run_id,
    a.started_at                                                        AS last_attempt_started_at,
    a.completed_at                                                      AS last_attempt_completed_at,
    TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), a.started_at, HOUR)             AS run_age_hours,
    s.run_id                                                            AS success_run_id,
    s.raw_status                                                        AS success_raw_status,
    s.started_at                                                        AS success_started_at,
    s.completed_at                                                      AS success_completed_at,
    -- 🔴 обе метки — из ОДНОЙ строки last_success
    TIMESTAMP_DIFF(CURRENT_TIMESTAMP(),
                   COALESCE(s.completed_at, s.started_at), HOUR)        AS success_age_hours,
    l.success_age_ok_hours,
    -- 🔴 порог не задан = часы прогона у этого слоя НЕ являются SLA. Колонка нужна,
    --    чтобы экран не показывал зелёные часы как обещание (§1.8 ред. 5)
    (l.success_age_ok_hours IS NOT NULL)                                AS success_age_is_sla,
    b.built_at,
    TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), b.built_at, HOUR)               AS build_age_hours
  FROM layers l
  LEFT JOIN data_as_of  d USING (layer_code)
  LEFT JOIN last_attempt a USING (layer_code)
  LEFT JOIN last_success s USING (layer_code)
  LEFT JOIN built       b USING (layer_code)
),

scored AS (
  SELECT
    base.*,
    CASE
      WHEN run_state IN ('FAILED', 'STUCK', 'NO_RUN', 'UNMAPPED')            THEN 'ERROR'
      WHEN success_run_id IS NULL                                           THEN 'ERROR'
      WHEN run_state = 'PARTIAL'                                            THEN 'STALE'
      WHEN success_age_ok_hours IS NOT NULL
           AND success_age_hours > success_age_ok_hours                     THEN 'STALE'
      WHEN data_age_ok_days IS NOT NULL
           AND data_age_days > data_age_ok_days                             THEN 'STALE'
      ELSE 'OK'
    END AS status,
    CASE
      WHEN run_state = 'NO_RUN'
        THEN 'для слоя не найдено ни одной релевантной попытки'
      WHEN run_state = 'UNMAPPED'
        THEN FORMAT('статус вне карты §1.5: %s', COALESCE(last_attempt_status, 'NULL'))
      WHEN run_state = 'STUCK'
        THEN FORMAT('прогон STUCK %d ч', run_age_hours)
      WHEN run_state = 'FAILED'
        THEN FORMAT('последняя попытка FAILED (%s)', COALESCE(last_attempt_status, 'NULL'))
      WHEN success_run_id IS NULL
        THEN 'нет ни одного успешного прогона'
      WHEN run_state = 'PARTIAL'
        THEN 'последняя попытка PARTIAL'
      WHEN success_age_ok_hours IS NOT NULL AND success_age_hours > success_age_ok_hours
        THEN FORMAT('success_age=%dч>%dч', success_age_hours, success_age_ok_hours)
      WHEN data_age_ok_days IS NOT NULL AND data_age_days > data_age_ok_days
        THEN FORMAT('data_age=%d>%d', data_age_days, data_age_ok_days)
      ELSE NULL
    END AS status_reason
  FROM base
),

-- детектор сирот: смотрит на факты, а не на метаданные синка (§1.7)
--
-- 🔴 ПРАВКА 19.08.2026 (Stage 2). Вопрос детектора — ТЕКУЩИЙ: «есть ли среди
--    валидных nm_id > 0 в заказах за 90 суток такие, которых СЕЙЧАС нет в
--    authoritative wb_raw.REF_SKU_MASTER?». Прежняя редакция фильтровала по
--    `sku_match_status = 'not_found'`, а это СНИМОК сопоставления на момент
--    загрузки: его проставляет загрузчик при записи в RAW, FACT копирует без
--    пересчёта. SKU, доехавший в справочник позже, оставался «сиротой» до выхода
--    старых строк из окна — nm_id 1083392113 держал бы ERROR до 03.11.2026,
--    уже находясь в REF. Детектор отвечал на исторический вопрос, а не на текущий.
-- 🔴 `sku_match_status` в RAW/FACT НЕ трогаем: на нём стоит `is_sku_row`
--    финансового контура (pr_mart2a/pr_mart2b). Правка локальна — только этот CTE.
-- 🔴 NOT EXISTS, а не JOIN: дубль nm_id в справочнике не должен ни размножать
--    строки, ни менять счётчик.
orphans AS (
  SELECT COUNT(DISTINCT o.nm_id) AS orphan_nm_ids
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ORDERS` o
  WHERE o.order_date >= DATE_SUB(CURRENT_DATE('Europe/Moscow'), INTERVAL 90 DAY)
    AND o.nm_id > 0
    AND NOT EXISTS (
      SELECT 1 FROM `project-fa311fc0-4d87-4781-986.wb_raw.REF_SKU_MASTER` r
      WHERE r.nm_id = o.nm_id
    )
)

SELECT
  layer_code, layer_group, run_source,
  data_as_of, data_age_days, data_age_ok_days,
  run_state, last_attempt_status, last_attempt_run_id,
  last_attempt_started_at, last_attempt_completed_at, run_age_hours,
  success_run_id, success_raw_status, success_started_at, success_completed_at,
  success_age_hours, success_age_ok_hours, success_age_is_sla,
  built_at, build_age_hours,
  CAST(NULL AS INT64) AS metric_value,
  status, status_reason,
  CURRENT_TIMESTAMP() AS generated_at
FROM scored

UNION ALL

SELECT
  'sku_orphans', 'qc', CAST(NULL AS STRING),
  CAST(NULL AS DATE), CAST(NULL AS INT64), CAST(NULL AS INT64),
  CAST(NULL AS STRING), CAST(NULL AS STRING), CAST(NULL AS STRING),
  CAST(NULL AS TIMESTAMP), CAST(NULL AS TIMESTAMP), CAST(NULL AS INT64),
  CAST(NULL AS STRING), CAST(NULL AS STRING), CAST(NULL AS TIMESTAMP), CAST(NULL AS TIMESTAMP),
  CAST(NULL AS INT64), CAST(NULL AS INT64), CAST(NULL AS BOOL),
  CAST(NULL AS TIMESTAMP), CAST(NULL AS INT64),
  orphan_nm_ids,
  IF(orphan_nm_ids > 0, 'ERROR', 'OK'),
  FORMAT('SKU с продажами вне справочника за 90 сут: %d', orphan_nm_ids),
  CURRENT_TIMESTAMP()
FROM orphans;

-- V_ADS_SCREEN_SKU · sha256(view_definition) = 54c1378c6b01fb2e9ac033eb798acc77b9f9533242dbed52b49170c22e53bc86
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_ADS_SCREEN_SKU`
AS WITH ref_ranked AS (
  SELECT
    r.*,
    COUNT(*)     OVER (PARTITION BY nm_id)                      AS ref_rows_for_nm_id,
    ROW_NUMBER() OVER (PARTITION BY nm_id ORDER BY internal_sku) AS rn
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.REF_SKU_MASTER` r
),
-- справочник сведён к одной строке на nm_id ДО join — fan-out невозможен
ref AS (SELECT * EXCEPT (rn) FROM ref_ranked WHERE rn = 1)
SELECT
  f.as_of_date,
  f.window_days,
  f.nm_id,
  -- атрибуты справочника
  r.internal_sku,
  r.product_name_short,
  r.product_name_full,
  r.category,
  r.line,
  r.product_type,
  r.brand,
  r.is_bundle,
  r.status                                    AS sku_status,
  r.active                                    AS sku_active,
  r.include_in_ads_analysis,
  (r.nm_id IS NULL)                           AS is_orphan,
  COALESCE(r.ref_rows_for_nm_id, 0)           AS ref_rows_for_nm_id,
  -- состав запросов по доказательности
  f.queries_total,
  f.queries_actionable,
  f.queries_observational,
  f.queries_insufficient,
  f.queries_zero_order,
  f.zero_order_spend_rub,
  f.queries_non_monotonic,
  f.queries_with_bid,
  -- воронка
  f.views_sum,
  f.clicks_sum,
  f.clicks_on_imp,
  f.atbs_sum,
  f.orders_sum,
  f.shks_sum,
  f.query_spend_rub,
  f.query_spend_on_imp_rub,
  f.ctr,
  f.cart_cr_clicks,
  f.order_cr_carts,
  f.order_cr_clicks,
  f.cpc_calc,
  f.cpo_ads,
  f.ctr_coverage_spend_share,
  -- сверка с денежной витриной
  f.mart_ad_spend_rub,
  f.query_spend_share_of_total,
  -- торговая часть и вклад ДО себестоимости (§2.3)
  f.orders_rub,
  f.buyouts_rub,
  f.orders_qty,
  f.hybrid_contribution_pre_cogs,
  f.settlement_contribution_pre_cogs,
  'PRE_COGS'                                                              AS economics_basis,
  'Себестоимость не подключена: вклад посчитан до COGS'                   AS economics_note
FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_ADS_FUNNEL_SKU_28D` f
LEFT JOIN ref r ON r.nm_id = f.nm_id;

-- V_ADS_SCREEN_QUERY · sha256(view_definition) = 0b1d0de51c6363ef2629841ce0f7610a1959e4b6b42bff040d189551404ef287
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_ADS_SCREEN_QUERY`
AS WITH q AS (
  SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_ADS_FUNNEL_QUERY_28D`
  UNION ALL
  SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_ADS_FUNNEL_QUERY_90D`
),
-- 🔴 функция приоритета сигналов — контракт §2.2. Тот же CASE проверяет гейт S7
sig_prio AS (
  SELECT
    as_of_date, window_days, nm_id, norm_query, signal_code, signal_strength,
    CASE signal_code
      WHEN 'CTR_BELOW_BASELINE'            THEN 1
      WHEN 'CART_CR_BELOW_BASELINE'        THEN 2
      WHEN 'ORDER_CR_CARTS_BELOW_BASELINE' THEN 3
      WHEN 'CPO_BELOW_BASELINE'            THEN 4
      WHEN 'CPC_ABOVE_BASELINE'            THEN 5
      WHEN 'CPC_BELOW_BASELINE'            THEN 6
      WHEN 'CTR_ABOVE_BASELINE'            THEN 7
      WHEN 'ZERO_ORDER_SPEND'              THEN 8
      ELSE 99
    END AS prio
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_ADS_FUNNEL_SIGNALS`
),
-- свёртка один-к-одному: строки экрана размножиться не могут
sig AS (
  SELECT
    as_of_date, window_days, nm_id, norm_query,
    ARRAY_AGG(signal_code ORDER BY prio, signal_code)                     AS signals,
    STRING_AGG(signal_code, ', ' ORDER BY prio, signal_code)              AS signals_text,
    ARRAY_AGG(signal_code ORDER BY prio, signal_code LIMIT 1)[OFFSET(0)]  AS signal_top,
    LOGICAL_OR(signal_strength = 'STRONG')                                AS has_strong_signal,
    COUNT(*)                                                              AS signal_count
  FROM sig_prio
  GROUP BY as_of_date, window_days, nm_id, norm_query
),
-- знаменатель доли расхода внутри SKU; агрегат ДО join — fan-out невозможен
sku_totals AS (
  SELECT as_of_date, window_days, nm_id, SUM(spend_sum) AS sku_spend_rub
  FROM q
  GROUP BY as_of_date, window_days, nm_id
),
ref_ranked AS (
  SELECT
    nm_id, internal_sku, product_name_short,
    ROW_NUMBER() OVER (PARTITION BY nm_id ORDER BY internal_sku) AS rn
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.REF_SKU_MASTER`
),
ref AS (SELECT * EXCEPT (rn) FROM ref_ranked WHERE rn = 1)
SELECT
  q.as_of_date,
  q.window_days,
  q.nm_id,
  q.norm_query,
  r.internal_sku,
  r.product_name_short,
  -- 🔴 единственная денежная колонка вью
  q.spend_sum                                              AS spend_rub,
  SAFE_DIVIDE(q.spend_sum, NULLIF(t.sku_spend_rub, 0))     AS spend_share_of_sku,
  q.spend_share_window,
  -- диагностическая строка воронки
  q.views_sum,
  q.clicks_sum,
  q.clicks_on_imp,
  q.ctr,
  q.cpc_calc,
  q.atbs_sum,
  q.cart_cr_clicks,
  q.orders_sum,
  q.order_cr_carts,
  q.order_cr_clicks,
  q.shks_sum,
  q.buyout_ratio_shk,
  q.cpo_ads,
  q.cpm_calc,
  q.avg_pos_w_views,
  q.avg_pos_w_clicks,
  q.advert_count,
  q.days_with_data,
  q.days_with_spend,
  q.rows_with_impressions,
  q.ctr_coverage_spend_share,
  q.funnel_monotonic,
  -- доказательность (пороги Ads-4, новых не заводим)
  q.evidence_status,
  q.evidence_reason,
  q.can_compare_ctr,
  q.can_judge_cart_cr,
  q.can_judge_order_cr,
  q.can_judge_order_carts,
  q.can_compare_cpc,
  -- baseline: диагностический ориентир, не норматив
  q.baseline_level,
  q.baseline_fallback_reason,
  q.baseline_ctr,       q.baseline_ctr_level,
  q.baseline_cart_cr,   q.baseline_cart_level,
  q.baseline_order_cr,  q.baseline_order_level,
  q.baseline_order_carts, q.baseline_order_carts_level,
  q.baseline_cpc,       q.baseline_cpc_level,
  q.baseline_cpo,       q.baseline_cpo_level,
  q.ctr_vs_baseline,
  q.cart_cr_vs_baseline,
  q.order_cr_vs_baseline,
  q.order_carts_vs_baseline,
  q.cpc_vs_baseline,
  q.cpo_vs_baseline,
  q.sku_ex_self_sample_clicks,
  q.sku_ex_self_pair_count,
  -- ставка
  q.bid_min,
  q.bid_max,
  q.bid_is_uniform,
  q.bid_campaign_count,
  q.payment_type,
  q.bid_type,
  q.campaign_status,
  q.bid_snapshot_date,
  q.bid_snapshot_status,
  q.bid_snapshot_offset_days,
  q.bid_is_after_stats_as_of,
  -- динамика к предыдущему окну
  q.delta_cpc,
  q.delta_order_cr_clicks,
  q.delta_spend,
  -- сигналы, свёрнутые в строку запроса
  IFNULL(s.signals, ARRAY<STRING>[])                       AS signals,
  s.signals_text,
  s.signal_top,
  IFNULL(s.has_strong_signal, FALSE)                       AS has_strong_signal,
  IFNULL(s.signal_count, 0)                                AS signal_count
FROM q
LEFT JOIN sku_totals t
       ON  t.as_of_date  = q.as_of_date
       AND t.window_days = q.window_days
       AND t.nm_id       = q.nm_id
LEFT JOIN sig s
       ON  s.as_of_date  = q.as_of_date
       AND s.window_days = q.window_days
       AND s.nm_id       = q.nm_id
       AND s.norm_query  = q.norm_query
LEFT JOIN ref r ON r.nm_id = q.nm_id;
