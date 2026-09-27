-- ============================================================================
-- AIE V1 (PR-3, финализация 2026-09-27) · ozon_mart.V_AIE_OZON_PAIR_EVIDENCE (VIEW) · sync_state: pending_deploy
-- Грейн: as_of_date × policy_id × campaign_id × sku. Одна дата решения (CTE aie_clock); политика —
-- evetis_ref.V_AIE_POLICY.
-- Контракт: docs/ads_intel/AIE_DESIGN_GATE_V1_2026-09-27.md §7.
--
-- 🔴 OZON НЕ PRODUCTION-GRADE. Каждая строка несёт production_grade = FALSE до PR-8:
--    • P2-5 (docs/architecture/TECH_DEBT.md): отчёт по SKU запрашивается окном UTC, день отчёта — МСК,
--      поэтому каждый хранимый день теряет 00:00–03:00 МСК; здесь НЕ исправляется;
--    • P2-6: однокампанийная пачка приходит CSV вместо ZIP и молча теряется; здесь НЕ исправляется,
--      но сигнатура «у кампании есть расход за день, а строк SKU нет» считается (p26_suspect_days);
--    • текущая CPC-ставка не собирается (нет снимка v2/products);
--    • RAW пишется MERGE: в replay значения — последние известные, а не на дату.
-- 🔴 АВТОПИЛОТ (решение владельца P9): TARGET_BIDS / TARGET_CIR — кампания неуправляема в V1.
-- 🔴 АТРИБУЦИЯ, НЕ БИЛЛИНГ: attributed_spend_rub из отчёта Performance по SKU.
-- 🔴 Пороги доказательности — числа Ads-4 (выведены на данных WB); для Ozon не калиброваны.
-- 🔴 NULL ≠ 0: нет снимка остатка — NULL; нет снимка кампании — тип и автопилот NULL.
-- Границы режима: |относительная смена price_rub| между снимками RAW_OZON_PRICES не меньше порога P7 из
-- V_AIE_POLICY (решение владельца 2026-09-27: 3 %; NULL — любое изменение) и смена набора акций со статусом
-- PARTICIPATING в V_OZON_PROMO_SKU_STATE_HISTORY. last_spend_date — вход правила текущего набора (K).
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.ozon_mart.V_AIE_OZON_PAIR_EVIDENCE`
OPTIONS (description = "AIE V1. Доказательная база пары кампания × SKU Ozon на дату решения; production_grade = FALSE до PR-8 (P2-5, P2-6, нет снимка ставок, RAW не на дату). Окно 28 суток после смены цены продавца не меньше порога P7 (V_AIE_POLICY) или участия в акциях, без дней с нулевым остатком FBO. Автопилот TARGET_BIDS/TARGET_CIR — неуправляемо (P9). Расход — атрибуция Performance. NULL = не наблюдается.")
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
  SELECT 1000 AS views_ctr, 40 AS clicks_actionable, 10 AS clicks_observational
),
pol AS (
  SELECT * FROM `project-fa311fc0-4d87-4781-986.evetis_ref.V_AIE_POLICY`
),
stat AS (
  SELECT s.`date` AS d, s.campaign_id, s.sku, s.attributed_spend_rub AS spend, s.impressions, s.clicks,
         s.cart_adds, s.orders, s.revenue_promo_rub, s.ordered_total_rub
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_ADS_SKU_DAILY` s
  CROSS JOIN aie_clock c
  WHERE s.`date` <= c.as_of_date AND s.`date` > DATE_SUB(c.as_of_date, INTERVAL 28 DAY)
),
ads_through AS (
  SELECT MAX(s.`date`) AS ads_data_through
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_ADS_SKU_DAILY` s
  CROSS JOIN aie_clock c
  WHERE s.`date` <= c.as_of_date
),
scope AS (
  SELECT campaign_id, sku FROM stat WHERE spend > 0 GROUP BY campaign_id, sku
),
expense AS (
  SELECT e.`date` AS d, e.campaign_id, e.expense_rub
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_ADS_EXPENSE_DAILY` e
  CROSS JOIN aie_clock c
  WHERE e.`date` <= c.as_of_date AND e.`date` > DATE_SUB(c.as_of_date, INTERVAL 28 DAY)
),
p26 AS (
  -- Сигнатура P2-6: расход кампании за день > 0, а ни одной строки SKU за этот день нет.
  SELECT e.campaign_id, COUNT(*) AS p26_suspect_days
  FROM expense e
  LEFT JOIN (SELECT DISTINCT d, campaign_id FROM stat) s ON s.d = e.d AND s.campaign_id = e.campaign_id
  WHERE e.expense_rub > 0 AND s.campaign_id IS NULL
  GROUP BY e.campaign_id
),
camp AS (
  SELECT * EXCEPT (rn) FROM (
    SELECT k.campaign_id, k.snapshot_date, k.title, k.state, k.adv_object_type, k.payment_type,
           k.product_autopilot_strategy, k.placement, k.daily_budget_rub, k.weekly_budget_rub,
           ROW_NUMBER() OVER (PARTITION BY k.campaign_id ORDER BY k.snapshot_date DESC) AS rn
    FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_ADS_CAMPAIGNS` k
    CROSS JOIN aie_clock c
    WHERE k.snapshot_date <= c.as_of_date
  )
  WHERE rn = 1
),
idmap AS (
  SELECT marketplace_sku AS sku, COUNT(DISTINCT internal_sku) AS sku_mapping_count, MIN(internal_sku) AS internal_sku
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP`
  WHERE marketplace = 'OZON' AND is_current
  GROUP BY marketplace_sku
),
offer_map AS (
  SELECT offer_id, MIN(internal_sku) AS internal_sku
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP`
  WHERE marketplace = 'OZON' AND is_current
  GROUP BY offer_id
),
price_seq AS (
  SELECT om.internal_sku, pr.snapshot_date, pr.price_rub,
         LAG(pr.price_rub) OVER (PARTITION BY om.internal_sku ORDER BY pr.snapshot_date) AS prev_price,
         LAG(pr.snapshot_date) OVER (PARTITION BY om.internal_sku ORDER BY pr.snapshot_date) AS prev_date
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PRICES` pr
  JOIN offer_map om ON om.offer_id = pr.offer_id
  CROSS JOIN aie_clock c
  WHERE pr.snapshot_date <= DATE(c.knowledge_ts, 'Europe/Moscow')
),
price_boundary AS (
  SELECT pl.policy_id, q.internal_sku, MAX(DATE_ADD(q.snapshot_date, INTERVAL 1 DAY)) AS regime_start
  FROM price_seq q
  CROSS JOIN aie_clock c
  CROSS JOIN pol pl
  WHERE q.prev_price IS NOT NULL AND q.price_rub IS DISTINCT FROM q.prev_price AND q.prev_date <= c.as_of_date
    AND (pl.p7_price_change_pct IS NULL
         OR 100 * ABS(SAFE_DIVIDE(q.price_rub - q.prev_price, q.prev_price)) >= pl.p7_price_change_pct)
  GROUP BY pl.policy_id, q.internal_sku
),
price_obs AS (
  SELECT internal_sku, MIN(snapshot_date) AS first_observed_date FROM price_seq GROUP BY internal_sku
),
promo_obs AS (
  SELECT h.internal_sku, h.observed_at,
         STRING_AGG(DISTINCT IF(h.resolved_state = 'PARTICIPATING', CAST(h.action_id AS STRING), NULL), ',' ORDER BY IF(h.resolved_state = 'PARTICIPATING', CAST(h.action_id AS STRING), NULL)) AS participating_set
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_PROMO_SKU_STATE_HISTORY` h
  CROSS JOIN aie_clock c
  WHERE h.observed_at <= c.knowledge_ts AND h.internal_sku IS NOT NULL
  GROUP BY h.internal_sku, h.observed_at
),
promo_seq AS (
  SELECT *, LAG(IFNULL(participating_set, '')) OVER (PARTITION BY internal_sku ORDER BY observed_at) AS prev_set,
            LAG(observed_at) OVER (PARTITION BY internal_sku ORDER BY observed_at) AS prev_at
  FROM promo_obs
),
promo_boundary AS (
  SELECT q.internal_sku, MAX(DATE_ADD(DATE(q.observed_at, 'Europe/Moscow'), INTERVAL 1 DAY)) AS regime_start
  FROM promo_seq q
  CROSS JOIN aie_clock c
  WHERE q.prev_set IS NOT NULL AND IFNULL(q.participating_set, '') != q.prev_set
    AND DATE(q.prev_at, 'Europe/Moscow') <= c.as_of_date
  GROUP BY q.internal_sku
),
stock_day AS (
  SELECT st.snapshot_date, st.sku, SUM(st.available_stock_count) AS units
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_STOCKS` st
  CROSS JOIN aie_clock c
  WHERE st.snapshot_date <= DATE(c.knowledge_ts, 'Europe/Moscow')
    AND st.snapshot_date > DATE_SUB(c.as_of_date, INTERVAL 28 DAY)
  GROUP BY st.snapshot_date, st.sku
),
stock_global AS (SELECT MAX(snapshot_date) AS global_snapshot_date FROM stock_day),
stock_latest AS (
  SELECT sd.sku, sd.snapshot_date AS stock_snapshot_date, sd.units AS stock_units
  FROM stock_day sd
  JOIN (SELECT sku, MAX(snapshot_date) AS md FROM stock_day GROUP BY sku) m
    ON m.sku = sd.sku AND m.md = sd.snapshot_date
),
windowed AS (
  SELECT
    pl.policy_id, sc.campaign_id, sc.sku, im.internal_sku, IFNULL(im.sku_mapping_count, 0) AS sku_mapping_count,
    DATE_SUB(c.as_of_date, INTERVAL 27 DAY) AS window_start_nominal,
    pb.regime_start AS price_regime_start,
    mb.regime_start AS promo_regime_start,
    po.first_observed_date AS price_first_observed_date,
    GREATEST(DATE_SUB(c.as_of_date, INTERVAL 27 DAY),
             IFNULL(pb.regime_start, DATE '1970-01-01'),
             IFNULL(mb.regime_start, DATE '1970-01-01')) AS effective_window_start
  FROM scope sc
  CROSS JOIN aie_clock c
  CROSS JOIN pol pl
  LEFT JOIN idmap im ON im.sku = sc.sku
  LEFT JOIN price_boundary pb ON pb.internal_sku = im.internal_sku AND pb.policy_id = pl.policy_id
  LEFT JOIN promo_boundary mb ON mb.internal_sku = im.internal_sku
  LEFT JOIN price_obs po ON po.internal_sku = im.internal_sku
),
days AS (
  SELECT w.policy_id, w.campaign_id, w.sku, dd AS d, sd.units AS stock_units_day
  FROM windowed w
  CROSS JOIN aie_clock c
  CROSS JOIN UNNEST(GENERATE_DATE_ARRAY(w.effective_window_start, c.as_of_date)) AS dd
  LEFT JOIN stock_day sd ON sd.sku = w.sku AND sd.snapshot_date = dd
),
eff AS (
  SELECT
    dy.policy_id, dy.campaign_id, dy.sku,
    COUNTIF(dy.stock_units_day IS NULL OR dy.stock_units_day > 0) AS effective_days,
    COUNTIF(dy.stock_units_day = 0) AS stockout_days_excluded,
    COUNTIF(dy.stock_units_day IS NULL) AS stock_unknown_days,
    SUM(IF(dy.stock_units_day = 0, NULL, s.impressions)) AS eff_impressions,
    SUM(IF(dy.stock_units_day = 0, NULL, s.clicks)) AS eff_clicks,
    SUM(IF(dy.stock_units_day = 0, NULL, s.cart_adds)) AS eff_cart_adds,
    SUM(IF(dy.stock_units_day = 0, NULL, s.orders)) AS eff_orders,
    SUM(IF(dy.stock_units_day = 0, NULL, s.spend)) AS eff_spend_attributed_rub,
    SUM(IF(dy.stock_units_day = 0, NULL, s.revenue_promo_rub)) AS eff_revenue_promo_rub,
    SUM(IF(dy.stock_units_day = 0, NULL, s.ordered_total_rub)) AS eff_ordered_total_rub
  FROM days dy
  LEFT JOIN stat s ON s.campaign_id = dy.campaign_id AND s.sku = dy.sku AND s.d = dy.d
  GROUP BY dy.policy_id, dy.campaign_id, dy.sku
),
nominal AS (
  SELECT s.campaign_id, s.sku, SUM(s.spend) AS spend_28d_attributed_rub, SUM(s.clicks) AS clicks_28d,
         SUM(s.orders) AS orders_28d,
         MAX(IF(s.spend > 0, s.d, NULL)) AS last_spend_date
  FROM stat s
  GROUP BY s.campaign_id, s.sku
)
SELECT
  c.as_of_date,
  c.knowledge_ts,
  c.run_mode,
  w.policy_id,
  w.campaign_id,
  w.sku,
  w.internal_sku,
  w.sku_mapping_count,
  k.snapshot_date AS campaign_snapshot_date,
  k.title AS campaign_title,
  k.state AS campaign_state,
  k.adv_object_type,
  k.payment_type,
  k.product_autopilot_strategy,
  (k.product_autopilot_strategy IN ('TARGET_BIDS', 'TARGET_CIR')) AS autopilot_controls_bids,
  k.daily_budget_rub,
  k.weekly_budget_rub,
  a.ads_data_through,
  IFNULL(p.p26_suspect_days, 0) AS p26_suspect_days,
  sg.global_snapshot_date AS stock_global_snapshot_date,
  sl.stock_snapshot_date,
  sl.stock_units,
  CASE
    WHEN sg.global_snapshot_date IS NULL THEN 'NO_SNAPSHOT'
    WHEN sl.stock_snapshot_date = sg.global_snapshot_date THEN 'OBSERVED'
    ELSE 'NOT_IN_LATEST_SNAPSHOT'
  END AS stock_status,
  28 AS window_days_nominal,
  w.window_start_nominal,
  w.effective_window_start,
  w.price_regime_start,
  w.promo_regime_start,
  w.price_first_observed_date,
  (w.price_first_observed_date IS NULL OR w.price_first_observed_date > w.effective_window_start) AS price_unobserved_part,
  IFNULL(e.effective_days, 0) AS effective_days,
  IFNULL(e.stockout_days_excluded, 0) AS stockout_days_excluded,
  IFNULL(e.stock_unknown_days, 0) AS stock_unknown_days,
  n.spend_28d_attributed_rub,
  n.clicks_28d,
  n.orders_28d,
  n.last_spend_date,
  DATE_DIFF(c.as_of_date, n.last_spend_date, DAY) AS days_since_last_spend,
  e.eff_impressions,
  e.eff_clicks,
  e.eff_cart_adds,
  e.eff_orders,
  e.eff_spend_attributed_rub,
  e.eff_revenue_promo_rub,
  e.eff_ordered_total_rub,
  SAFE_DIVIDE(e.eff_clicks, e.eff_impressions) AS eff_ctr,
  SAFE_DIVIDE(e.eff_spend_attributed_rub, e.eff_clicks) AS eff_cpc_rub,
  SAFE_DIVIDE(e.eff_spend_attributed_rub, e.eff_orders) AS eff_cpo_rub,
  CASE
    WHEN IFNULL(e.eff_clicks, 0) >= g.clicks_actionable THEN 'ACTIONABLE'
    WHEN IFNULL(e.eff_clicks, 0) >= g.clicks_observational OR IFNULL(e.eff_impressions, 0) >= g.views_ctr THEN 'OBSERVATIONAL'
    ELSE 'INSUFFICIENT'
  END AS evidence_status,
  'ADS4_WB_GATES_NOT_CALIBRATED_FOR_OZON' AS evidence_gate_source,
  FALSE AS production_grade,
  'P2_5_WINDOW_BIAS,P2_6_BATCH_LOSS,NO_BID_SNAPSHOT,RAW_NOT_POINT_IN_TIME' AS production_grade_blockers
FROM windowed w
CROSS JOIN aie_clock c
CROSS JOIN ads4_gates g
CROSS JOIN ads_through a
CROSS JOIN stock_global sg
LEFT JOIN eff e ON e.policy_id = w.policy_id AND e.campaign_id = w.campaign_id AND e.sku = w.sku
LEFT JOIN nominal n ON n.campaign_id = w.campaign_id AND n.sku = w.sku
LEFT JOIN camp k ON k.campaign_id = w.campaign_id
LEFT JOIN p26 p ON p.campaign_id = w.campaign_id
LEFT JOIN stock_latest sl ON sl.sku = w.sku;
