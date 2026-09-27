-- ============================================================================
-- AIE V1 (PR-1, финализация 2026-09-27) · wb_mart.V_AIE_WB_PAIR_EVIDENCE (VIEW)
-- Грейн: as_of_date × policy_id × advert_id × nm_id. Одна дата решения (CTE aie_clock); политика —
-- evetis_ref.V_AIE_POLICY (в production одна строка).
-- Контракт: docs/ads_intel/AIE_DESIGN_GATE_V1_2026-09-27.md §4–5.
--
-- Что это. Доказательная база по паре «кампания × карточка» за эффективное окно:
-- номинальное окно 28 суток, начало сдвигается после последней смены режима
-- (ставка/размещение/оплата кампании; цена продавца, если |изменение| ≥ P7 % — решение владельца
-- 2026-09-27: P7 = 3 %, порог в V_AIE_POLICY), дни с нулевым остатком карточки исключаются. Решений нет.
-- last_spend_date / days_since_last_spend — вход правила текущего набора рекомендаций (K, решение владельца).
--
-- 🔴 АТРИБУЦИЯ, НЕ БИЛЛИНГ (FIN CONTRACT V2). Расход — FACT_ADS_SKU_DAILY.stats_spend_rub.
--    FACT_ADS_COSTS_DAILY здесь не читается и по SKU не распределяется.
-- 🔴 NULL ≠ 0. Нет снимка остатка — остаток NULL, а не 0. Нет снимка настроек — ставка NULL.
-- 🔴 АКТИВНОСТЬ ДНЯ = ФАКТИЧЕСКИЙ РАСХОД (решение владельца P10). Статус кампании в снимке —
--    момент снимка, а не состояние дня: статус 11 с расходом — обычный случай, он хранится как контекст.
-- 🔴 ПОРОГИ ДОКАЗАТЕЛЬНОСТИ — Ads-4 без изменений (docs/ADS4_FUNNEL_MART_DESIGN_2026-08-15.md §6.2):
--    ACTIONABLE clicks ≥ 40; OBSERVATIONAL clicks ≥ 10 или views ≥ 1000; иначе INSUFFICIENT.
--    40/60/1000/20 выведены на 2σ / 5 %; 10 — проектное решение Ads-4.
-- 🔴 REPLAY. tools/aie_render.py заменяет CTE между маркерами @aie:clock, а FACT_ADS_SKU_DAILY и
--    V_ADV_CAMPAIGN_STATS — той же логикой поверх RAW с load_ts ≤ knowledge_ts. Все остальные
--    входы отфильтрованы по knowledge_ts / as_of_date здесь же, поэтому одно тело служит обоим режимам.
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_AIE_WB_PAIR_EVIDENCE`
OPTIONS (description = "AIE V1. Доказательная база пары кампания × карточка WB на дату решения и политику: эффективное окно 28 суток после последней смены режима (ставка, размещение, оплата; цена продавца при изменении не меньше порога P7 из evetis_ref.V_AIE_POLICY), без дней с нулевым остатком; дата последнего расхода для текущего набора. Расход — атрибуция FACT_ADS_SKU_DAILY, не биллинг. Доказательность — пороги Ads-4. NULL = не наблюдается, не ноль. Решений нет.")
AS
WITH
-- @aie:clock:begin
aie_clock AS (
  SELECT
    DATE_SUB(CURRENT_DATE('Europe/Moscow'), INTERVAL 1 DAY) AS as_of_date,
    CURRENT_TIMESTAMP() AS knowledge_ts,
    'CURRENT' AS run_mode
),
-- @aie:clock:end
ads4_gates AS (
  -- Пороги Ads-4 (sql/mart/ads4_funnel_v1.sql L251-264). Не AIE-политика: менять только вместе с Ads-4.
  SELECT 1000 AS views_ctr, 40 AS clicks_actionable, 10 AS clicks_observational,
         60 AS clicks_cart, 40 AS atbs_order, 20 AS clicks_cpc
),
pol AS (
  SELECT * FROM `project-fa311fc0-4d87-4781-986.evetis_ref.V_AIE_POLICY`
),
fact AS (
  SELECT f.`date` AS d, f.advert_id, f.nm_id, f.views, f.clicks,
         f.stats_spend_rub AS spend, f.ad_orders_raw AS orders_raw, f.ad_orders_dedup_estimate AS orders_dedup,
         f.ads_revenue_raw_rub AS revenue_raw, f.ads_revenue_dedup_estimate_rub AS revenue_dedup,
         f.multitouch_ambiguous_flag
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ADS_SKU_DAILY` f
  CROSS JOIN aie_clock c
  WHERE f.`date` <= c.as_of_date
    AND f.`date` > DATE_SUB(c.as_of_date, INTERVAL 28 DAY)
),
ads_through AS (
  SELECT MAX(f.`date`) AS ads_data_through
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ADS_SKU_DAILY` f
  CROSS JOIN aie_clock c
  WHERE f.`date` <= c.as_of_date
),
funnel AS (
  -- Корзины и шки теряются в FACT_ADS_SKU_DAILY; берём из того же дедуп-вью, из которого собран FACT.
  SELECT d, advert_id, nm_id,
         SUM(atbs) AS atbs, SUM(shks) AS shks
  FROM (
    SELECT SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(s.`date`, 1, 10)) AS d,
           SAFE_CAST(s.advertId AS INT64) AS advert_id,
           SAFE_CAST(s.nmId AS INT64) AS nm_id,
           SAFE_CAST(s.atbs AS INT64) AS atbs,
           SAFE_CAST(s.shks AS INT64) AS shks
    FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_ADV_CAMPAIGN_STATS` s
  ) x
  CROSS JOIN aie_clock c
  WHERE x.d <= c.as_of_date AND x.d > DATE_SUB(c.as_of_date, INTERVAL 28 DAY)
  GROUP BY d, advert_id, nm_id
),
scope AS (
  -- Пара в составе, если в номинальном окне был фактический расход (P10).
  SELECT advert_id, nm_id FROM fact WHERE spend > 0 GROUP BY advert_id, nm_id
),
cfg AS (
  SELECT s.snapshot_ts, s.advert_id, s.nm_id, s.status_raw, s.campaign_type_raw, s.payment_type_raw,
         s.bid_type_raw, s.placement_search_enabled, s.placement_recommendations_enabled,
         s.search_bid_kopecks, s.recommendations_bid_kopecks
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_ADV_CAMPAIGN_CONFIG_SNAPSHOT` s
  CROSS JOIN aie_clock c
  WHERE s.snapshot_ts <= c.knowledge_ts
    AND s.advert_id IN (SELECT advert_id FROM scope)
),
camp_cfg AS (
  -- Параметры уровня кампании одинаковы во всех nm-строках одного снимка; MIN/MAX детерминированы.
  SELECT snapshot_ts, advert_id,
         MIN(status_raw) AS status_raw, MIN(campaign_type_raw) AS campaign_type_raw,
         MIN(payment_type_raw) AS payment_type_raw, MIN(bid_type_raw) AS bid_type_raw,
         LOGICAL_OR(placement_search_enabled) AS placement_search_enabled,
         LOGICAL_OR(placement_recommendations_enabled) AS placement_recommendations_enabled,
         TO_JSON_STRING(STRUCT(MIN(payment_type_raw) AS p, MIN(bid_type_raw) AS b,
                               LOGICAL_OR(placement_search_enabled) AS s,
                               LOGICAL_OR(placement_recommendations_enabled) AS r)) AS camp_key
  FROM cfg
  GROUP BY snapshot_ts, advert_id
),
camp_seq AS (
  SELECT *, LAG(camp_key) OVER (PARTITION BY advert_id ORDER BY snapshot_ts) AS prev_key,
            LAG(snapshot_ts) OVER (PARTITION BY advert_id ORDER BY snapshot_ts) AS prev_ts
  FROM camp_cfg
),
camp_boundary AS (
  -- Смена между двумя снимками: смешанными считаются все дни от prev_ts до snapshot_ts,
  -- окно начинается на следующий день после МСК-даты снимка, заметившего смену.
  SELECT q.advert_id, MAX(DATE_ADD(DATE(q.snapshot_ts, 'Europe/Moscow'), INTERVAL 1 DAY)) AS regime_start
  FROM camp_seq q
  CROSS JOIN aie_clock c
  WHERE q.prev_key IS NOT NULL AND q.camp_key != q.prev_key
    AND DATE(q.prev_ts, 'Europe/Moscow') <= c.as_of_date
  GROUP BY q.advert_id
),
camp_latest AS (
  SELECT * EXCEPT (rn) FROM (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY advert_id ORDER BY snapshot_ts DESC) AS rn FROM camp_cfg)
  WHERE rn = 1
),
nm_cfg AS (
  SELECT snapshot_ts, advert_id, nm_id, search_bid_kopecks, recommendations_bid_kopecks,
         TO_JSON_STRING(STRUCT(search_bid_kopecks AS s, recommendations_bid_kopecks AS r)) AS bid_key
  FROM cfg
  WHERE nm_id IS NOT NULL
),
nm_seq AS (
  SELECT *, LAG(bid_key) OVER (PARTITION BY advert_id, nm_id ORDER BY snapshot_ts) AS prev_key,
            LAG(snapshot_ts) OVER (PARTITION BY advert_id, nm_id ORDER BY snapshot_ts) AS prev_ts
  FROM nm_cfg
),
bid_boundary AS (
  SELECT q.advert_id, q.nm_id, MAX(DATE_ADD(DATE(q.snapshot_ts, 'Europe/Moscow'), INTERVAL 1 DAY)) AS regime_start
  FROM nm_seq q
  CROSS JOIN aie_clock c
  WHERE q.prev_key IS NOT NULL AND q.bid_key != q.prev_key
    AND DATE(q.prev_ts, 'Europe/Moscow') <= c.as_of_date
  GROUP BY q.advert_id, q.nm_id
),
nm_latest AS (
  SELECT * EXCEPT (rn) FROM (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY advert_id, nm_id ORDER BY snapshot_ts DESC) AS rn FROM nm_cfg)
  WHERE rn = 1
),
price_boundary AS (
  -- P7 (решение владельца 2026-09-27): граница режима — |изменение seller_effective_price| ≥ p7 %.
  -- p7 = NULL означает любое наблюдаемое изменение (прежнее решение владельца), а не отсутствие границы.
  SELECT pl.policy_id, p.nm_id, MAX(DATE_ADD(DATE(p.observed_at, 'Europe/Moscow'), INTERVAL 1 DAY)) AS regime_start
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_PRICES_OBSERVED_CHANGES` p
  CROSS JOIN aie_clock c
  CROSS JOIN pol pl
  WHERE p.environment = 'prod'
    AND p.observed_at <= c.knowledge_ts
    AND p.delta_seller_effective_price IS NOT NULL
    AND p.delta_seller_effective_price != 0
    AND (pl.p7_price_change_pct IS NULL OR ABS(p.delta_seller_effective_price_pct) >= pl.p7_price_change_pct)
    AND DATE(p.prev_observed_at, 'Europe/Moscow') <= c.as_of_date
  GROUP BY pl.policy_id, p.nm_id
),
price_obs AS (
  SELECT r.nm_id, MIN(DATE(r.observed_at, 'Europe/Moscow')) AS first_observed_date
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_PRICES` r
  CROSS JOIN aie_clock c
  WHERE r.environment = 'prod' AND r.observed_at <= c.knowledge_ts
  GROUP BY r.nm_id
),
stock_day AS (
  -- Остаток карточки = сумма всех строк снимка (с 2026-08-15 склады анонимизированы в -999999).
  SELECT s.snapshot_date, s.nm_id, SUM(s.quantity) AS units
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_STOCKS_SNAPSHOT` s
  CROSS JOIN aie_clock c
  WHERE s.snapshot_date <= DATE(c.knowledge_ts, 'Europe/Moscow')
    AND s.snapshot_date > DATE_SUB(c.as_of_date, INTERVAL 28 DAY)
  GROUP BY s.snapshot_date, s.nm_id
),
stock_global AS (
  SELECT MAX(snapshot_date) AS global_snapshot_date FROM stock_day
),
stock_latest AS (
  SELECT sd.nm_id, sd.snapshot_date AS stock_snapshot_date, sd.units AS stock_units
  FROM stock_day sd
  JOIN (SELECT nm_id, MAX(snapshot_date) AS md FROM stock_day GROUP BY nm_id) m
    ON m.nm_id = sd.nm_id AND m.md = sd.snapshot_date
),
idmap AS (
  SELECT SAFE_CAST(marketplace_sku AS INT64) AS nm_id,
         COUNT(DISTINCT internal_sku) AS sku_mapping_count,
         MIN(internal_sku) AS internal_sku
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP`
  WHERE marketplace = 'WB' AND is_current
  GROUP BY 1
),
master AS (
  SELECT nm_id, include_in_ads_analysis, is_bundle
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.REF_SKU_MASTER`
),
windowed AS (
  SELECT
    pl.policy_id, sc.advert_id, sc.nm_id,
    DATE_SUB(c.as_of_date, INTERVAL 27 DAY) AS window_start_nominal,
    cb.regime_start AS campaign_regime_start,
    bb.regime_start AS bid_regime_start,
    pb.regime_start AS price_regime_start,
    po.first_observed_date AS price_first_observed_date,
    GREATEST(DATE_SUB(c.as_of_date, INTERVAL 27 DAY),
             IFNULL(cb.regime_start, DATE '1970-01-01'),
             IFNULL(bb.regime_start, DATE '1970-01-01'),
             IFNULL(pb.regime_start, DATE '1970-01-01')) AS effective_window_start
  FROM scope sc
  CROSS JOIN aie_clock c
  CROSS JOIN pol pl
  LEFT JOIN camp_boundary cb ON cb.advert_id = sc.advert_id
  LEFT JOIN bid_boundary bb ON bb.advert_id = sc.advert_id AND bb.nm_id = sc.nm_id
  LEFT JOIN price_boundary pb ON pb.nm_id = sc.nm_id AND pb.policy_id = pl.policy_id
  LEFT JOIN price_obs po ON po.nm_id = sc.nm_id
),
days AS (
  SELECT w.policy_id, w.advert_id, w.nm_id, dd AS d, sd.units AS stock_units_day
  FROM windowed w
  CROSS JOIN aie_clock c
  CROSS JOIN UNNEST(GENERATE_DATE_ARRAY(w.effective_window_start, c.as_of_date)) AS dd
  LEFT JOIN stock_day sd ON sd.nm_id = w.nm_id AND sd.snapshot_date = dd
),
eff AS (
  SELECT
    dy.policy_id, dy.advert_id, dy.nm_id,
    COUNTIF(dy.stock_units_day IS NULL OR dy.stock_units_day > 0) AS effective_days,
    COUNTIF(dy.stock_units_day = 0) AS stockout_days_excluded,
    COUNTIF(dy.stock_units_day IS NULL) AS stock_unknown_days,
    SUM(IF(dy.stock_units_day = 0, f.spend, 0)) AS spend_on_stockout_days,
    SUM(IF(dy.stock_units_day = 0, NULL, f.views)) AS eff_views,
    SUM(IF(dy.stock_units_day = 0, NULL, f.clicks)) AS eff_clicks,
    SUM(IF(dy.stock_units_day = 0, NULL, fu.atbs)) AS eff_atbs,
    SUM(IF(dy.stock_units_day = 0, NULL, fu.shks)) AS eff_shks,
    SUM(IF(dy.stock_units_day = 0, NULL, f.orders_raw)) AS eff_orders_raw,
    SUM(IF(dy.stock_units_day = 0, NULL, f.orders_dedup)) AS eff_orders_dedup,
    SUM(IF(dy.stock_units_day = 0, NULL, f.spend)) AS eff_spend_attributed_rub,
    SUM(IF(dy.stock_units_day = 0, NULL, f.revenue_raw)) AS eff_revenue_raw_rub,
    SUM(IF(dy.stock_units_day = 0, NULL, f.revenue_dedup)) AS eff_revenue_dedup_rub,
    COUNTIF(dy.stock_units_day IS DISTINCT FROM 0 AND f.spend > 0) AS eff_active_days,
    COUNTIF(dy.stock_units_day IS DISTINCT FROM 0 AND f.multitouch_ambiguous_flag) AS eff_multitouch_days
  FROM days dy
  LEFT JOIN fact f ON f.advert_id = dy.advert_id AND f.nm_id = dy.nm_id AND f.d = dy.d
  LEFT JOIN funnel fu ON fu.advert_id = dy.advert_id AND fu.nm_id = dy.nm_id AND fu.d = dy.d
  GROUP BY dy.policy_id, dy.advert_id, dy.nm_id
),
nominal AS (
  SELECT f.advert_id, f.nm_id,
         SUM(f.spend) AS spend_28d_attributed_rub, SUM(f.views) AS views_28d, SUM(f.clicks) AS clicks_28d,
         SUM(f.orders_raw) AS orders_raw_28d,
         LOGICAL_OR(f.d = c.as_of_date AND f.spend > 0) AS had_spend_on_as_of,
         MAX(IF(f.spend > 0, f.d, NULL)) AS last_spend_date
  FROM fact f
  CROSS JOIN aie_clock c
  GROUP BY f.advert_id, f.nm_id
)
SELECT
  c.as_of_date,
  c.knowledge_ts,
  c.run_mode,
  w.policy_id,
  w.advert_id,
  w.nm_id,
  im.internal_sku,
  IFNULL(im.sku_mapping_count, 0) AS sku_mapping_count,
  ms.include_in_ads_analysis,
  ms.is_bundle,
  -- ── настройки кампании на момент последнего снимка ≤ knowledge_ts (контекст, P10) ──
  cl.snapshot_ts AS campaign_config_ts,
  cl.status_raw AS campaign_status_raw,
  cl.campaign_type_raw,
  cl.payment_type_raw,
  cl.bid_type_raw,
  cl.placement_search_enabled,
  cl.placement_recommendations_enabled,
  nl.snapshot_ts AS bid_config_ts,
  nl.search_bid_kopecks / 100 AS search_bid_rub,
  nl.recommendations_bid_kopecks / 100 AS recommendations_bid_rub,
  (cl.advert_id IS NOT NULL) AS config_observed,
  -- ── полнота источников ──
  a.ads_data_through,
  sg.global_snapshot_date AS stock_global_snapshot_date,
  sl.stock_snapshot_date,
  sl.stock_units,
  CASE
    WHEN sg.global_snapshot_date IS NULL THEN 'NO_SNAPSHOT'
    WHEN sl.stock_snapshot_date = sg.global_snapshot_date THEN 'OBSERVED'
    ELSE 'NOT_IN_LATEST_SNAPSHOT'
  END AS stock_status,
  -- ── окно ──
  28 AS window_days_nominal,
  w.window_start_nominal,
  w.effective_window_start,
  w.campaign_regime_start,
  w.bid_regime_start,
  w.price_regime_start,
  w.price_first_observed_date,
  (w.price_first_observed_date IS NULL OR w.price_first_observed_date > w.effective_window_start) AS price_unobserved_part,
  IFNULL(e.effective_days, 0) AS effective_days,
  IFNULL(e.stockout_days_excluded, 0) AS stockout_days_excluded,
  IFNULL(e.stock_unknown_days, 0) AS stock_unknown_days,
  e.spend_on_stockout_days,
  -- ── номинальное окно 28 суток (без обрезки) ──
  n.spend_28d_attributed_rub,
  n.views_28d,
  n.clicks_28d,
  n.orders_raw_28d,
  IFNULL(n.had_spend_on_as_of, FALSE) AS had_spend_on_as_of,
  n.last_spend_date,
  DATE_DIFF(c.as_of_date, n.last_spend_date, DAY) AS days_since_last_spend,
  -- ── эффективное окно ──
  e.eff_views,
  e.eff_clicks,
  e.eff_atbs,
  e.eff_shks,
  e.eff_orders_raw,
  e.eff_orders_dedup,
  e.eff_spend_attributed_rub,
  e.eff_revenue_raw_rub,
  e.eff_revenue_dedup_rub,
  IFNULL(e.eff_active_days, 0) AS eff_active_days,
  IFNULL(e.eff_multitouch_days, 0) AS eff_multitouch_days,
  SAFE_DIVIDE(e.eff_clicks, e.eff_views) AS eff_ctr,
  SAFE_DIVIDE(e.eff_spend_attributed_rub, e.eff_clicks) AS eff_cpc_rub,
  SAFE_DIVIDE(e.eff_spend_attributed_rub, e.eff_orders_raw) AS eff_cpo_rub,
  SAFE_DIVIDE(e.eff_revenue_raw_rub, e.eff_spend_attributed_rub) AS eff_roas_raw,
  -- ── доказательность Ads-4 ──
  (IFNULL(e.eff_views, 0) >= g.views_ctr) AS can_compare_ctr,
  (IFNULL(e.eff_clicks, 0) >= g.clicks_cart) AS can_judge_cart_cr,
  (IFNULL(e.eff_clicks, 0) >= g.clicks_actionable) AS can_judge_order_cr,
  (IFNULL(e.eff_atbs, 0) >= g.atbs_order) AS can_judge_order_carts,
  (IFNULL(e.eff_clicks, 0) >= g.clicks_cpc) AS can_compare_cpc,
  CASE
    WHEN IFNULL(e.eff_clicks, 0) >= g.clicks_actionable THEN 'ACTIONABLE'
    WHEN IFNULL(e.eff_clicks, 0) >= g.clicks_observational OR IFNULL(e.eff_views, 0) >= g.views_ctr THEN 'OBSERVATIONAL'
    ELSE 'INSUFFICIENT'
  END AS evidence_status,
  (IFNULL(e.eff_clicks, 0) >= g.clicks_actionable
    AND IFNULL(e.eff_orders_raw, 0) = 0
    AND IFNULL(e.eff_spend_attributed_rub, 0) > 0) AS waste_zero_orders_ads4
FROM windowed w
CROSS JOIN aie_clock c
CROSS JOIN ads4_gates g
CROSS JOIN ads_through a
CROSS JOIN stock_global sg
LEFT JOIN eff e ON e.policy_id = w.policy_id AND e.advert_id = w.advert_id AND e.nm_id = w.nm_id
LEFT JOIN nominal n ON n.advert_id = w.advert_id AND n.nm_id = w.nm_id
LEFT JOIN camp_latest cl ON cl.advert_id = w.advert_id
LEFT JOIN nm_latest nl ON nl.advert_id = w.advert_id AND nl.nm_id = w.nm_id
LEFT JOIN stock_latest sl ON sl.nm_id = w.nm_id
LEFT JOIN idmap im ON im.nm_id = w.nm_id
LEFT JOIN master ms ON ms.nm_id = w.nm_id;
