-- ============================================================================
-- AIE V1 (PR-1, финализация 2026-09-27) · wb_mart.V_AIE_WB_ECON_GUARD (VIEW)
-- Грейн: as_of_date × policy_id × nm_id. Одна дата решения (CTE aie_clock); политика — evetis_ref.V_AIE_POLICY.
-- Контракт: docs/ads_intel/AIE_DESIGN_GATE_V1_2026-09-27.md §3–5.
--
-- Экономический ограничитель SKU. Формулы — ровно контракт Ads-4 (sql/ads/ads4_sku_economic_limits.sql):
--   contribution_before_ads = buyouts_rub − marketplace_fee_rub − logistics_cost_positive − net_product_cogs
--   actual_ad_drr = ad_spend / buyouts_rub;  max_ad_drr_breakeven = contribution_before_ads / buyouts_rub
-- Блок ref28_* — те же формулы на окне Ads-4 (28 суток до as_of без обрезки): проверка паритета
-- с wb_mart.V_ADS_SKU_ECONOMIC_LIMITS доказывает, что AIE переиспользует контракт, а не переизобретает.
--
-- Отличия окна AIE (aie_*), каждое — защита от заглядывания вперёд или смешения режимов:
--   • окно заканчивается на econ_as_of = LEAST(as_of, finance_known_through): комиссия и логистика
--     приходят из финансового отчёта с лагом, а MART подставляет 0 за дни без отчёта;
--   • finance_known_through = LEAST(последний отчёт с completed_at ≤ knowledge_ts,
--     последняя finance_date в FACT_FINANCE) — в replay решает первое, в текущем режиме — оба;
--   • начало сдвигается после последней смены цены продавца не меньше порога P7 (V_AIE_POLICY, 3 %).
-- FINANCIAL_DATA_MATURE (контракт, решение владельца 2026-09-27): конец фактического окна не позже
-- последней даты с доступными финансами (financial_data_mature). Искусственного исключения N дней нет:
-- дневной отчёт приходит к ~07:24 следующего дня и совпадает с недельным (Calibration Addendum §4).
--
-- Роли баз (P6, решение владельца 2026-09-27): realized — граница доказательности DECREASE; forward base —
-- текущий структурный и ценовой ограничитель (PRICE_BELOW_BREAKEVEN); forward conservative — ограничитель
-- против INCREASE; forward stress — только флаг риска. Прогнозные базы в replay не участвуют.
-- Разложение прогнозной цены (fwd_*) — диагностика для PRICING_REVIEW; цена здесь не меняется.
-- Рекламный расход — атрибуция FACT_ADS_SKU_DAILY (= MART_SKU_DAILY.ad_spend, паритет проверяется).
-- Биллинг (FACT_ADS_COSTS_DAILY) не читается (FIN CONTRACT V2).
--
-- Прогнозная экономика WB_FE_V1 (V_WB_SKU_FORWARD_ECONOMICS_CURRENT) есть только в текущем состоянии:
-- в replay forward_availability = SKIPPED_NOT_REPLAYABLE. Лимит ДРР по базам:
--   base         = max_affordable_drr_pre_tax_pct / 100 (значение контракта, округлено до 0,01 п.п.)
--   conservative = (1 − (commission + acquiring_p75)/100) · (1 − break_even_conservative / price)
--   stress       = (1 − (commission + acquiring_p90)/100) · (1 − break_even_stress / price)
-- (тождество: contribution/P = (1 − take)(1 − BE/P); для base проверяется паритетом).
--
-- 🔴 NULL ≠ 0. Себестоимость не покрыта хотя бы в одном дне окна → net_product_cogs и пределы NULL.
-- 🔴 Это не прибыль: без хранения, штрафов, приёмки, фулфилмента, OPEX и налога.
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_AIE_WB_ECON_GUARD`
OPTIONS (description = "AIE V1. Экономический ограничитель SKU WB на дату решения и политику: формулы Ads-4 (V_ADS_SKU_ECONOMIC_LIMITS, паритет — блок ref28_*) на окне, которое заканчивается на последнем дне с известными финансами (financial_data_mature) и начинается после смены цены продавца не меньше порога P7. Разложение прогнозной цены для PRICING_REVIEW. Расход — атрибуция, не биллинг. Прогнозная экономика WB_FE_V1 — только в текущем режиме. NULL = не покрыто, не ноль. Не прибыль.")
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
pol AS (
  SELECT * FROM `project-fa311fc0-4d87-4781-986.evetis_ref.V_AIE_POLICY`
),
fin_loads AS (
  SELECT MAX(l.date_to) AS loads_known_through
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.FINANCE_REPORT_LOADS` l
  CROSS JOIN aie_clock c
  WHERE l.status = 'COMPLETE' AND l.completed_at <= c.knowledge_ts
),
fin_fact AS (
  SELECT MAX(finance_date) AS fact_known_through
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_FINANCE`
),
mart_bounds AS (
  SELECT MAX(day) AS mart_through FROM `project-fa311fc0-4d87-4781-986.wb_mart.MART_SKU_DAILY`
),
bounds AS (
  SELECT
    c.as_of_date,
    LEAST(fl.loads_known_through, ff.fact_known_through) AS finance_known_through,
    LEAST(c.as_of_date, fl.loads_known_through, ff.fact_known_through, mb.mart_through) AS econ_as_of
  FROM aie_clock c
  CROSS JOIN fin_loads fl
  CROSS JOIN fin_fact ff
  CROSS JOIN mart_bounds mb
),
price_boundary AS (
  -- Тот же критерий, что в V_AIE_WB_PAIR_EVIDENCE (P7 из V_AIE_POLICY; NULL — любое изменение).
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
ads_day AS (
  SELECT f.`date` AS day, f.nm_id, SUM(f.stats_spend_rub) AS ad_spend_attributed_rub
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ADS_SKU_DAILY` f
  CROSS JOIN aie_clock c
  WHERE f.`date` <= c.as_of_date AND f.`date` > DATE_SUB(c.as_of_date, INTERVAL 28 DAY)
  GROUP BY f.`date`, f.nm_id
),
base AS (
  -- Тот же join, что в V_ADS_SKU_ECONOMIC_LIMITS: V_MART_SKU_DAILY_COGS ⋈ MART_SKU_DAILY 1:1.
  SELECT
    cg.day, cg.nm_id, cg.internal_sku, cg.is_bundle,
    m.orders_qty, m.buyouts_qty, m.buyouts_rub, m.marketplace_fee_rub, m.logistics_cost_positive,
    m.ad_spend AS mart_ad_spend,
    a.ad_spend_attributed_rub,
    cg.net_product_cogs_operational_rub AS net_cogs,
    cg.cogs_covered
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_MART_SKU_DAILY_COGS` cg
  JOIN `project-fa311fc0-4d87-4781-986.wb_mart.MART_SKU_DAILY` m ON m.day = cg.day AND m.nm_id = cg.nm_id
  CROSS JOIN aie_clock c
  LEFT JOIN ads_day a ON a.day = cg.day AND a.nm_id = cg.nm_id
  WHERE cg.day <= c.as_of_date AND cg.day > DATE_SUB(c.as_of_date, INTERVAL 28 DAY)
),
win AS (
  SELECT
    pl.policy_id,
    n.nm_id,
    n.internal_sku,
    n.is_bundle,
    b.econ_as_of,
    pb.regime_start AS price_regime_start,
    po.first_observed_date AS price_first_observed_date,
    GREATEST(DATE_SUB(b.econ_as_of, INTERVAL 27 DAY), IFNULL(pb.regime_start, DATE '1970-01-01')) AS econ_window_start
  FROM (SELECT nm_id, MIN(internal_sku) AS internal_sku, LOGICAL_OR(is_bundle) AS is_bundle
        FROM base GROUP BY nm_id) n
  CROSS JOIN bounds b
  CROSS JOIN pol pl
  LEFT JOIN price_boundary pb ON pb.nm_id = n.nm_id AND pb.policy_id = pl.policy_id
  LEFT JOIN price_obs po ON po.nm_id = n.nm_id
),
aie AS (
  SELECT
    w.policy_id,
    x.nm_id,
    COUNT(*) AS econ_window_days,
    SUM(x.orders_qty) AS orders_qty,
    SUM(x.buyouts_qty) AS buyouts_qty,
    SUM(x.buyouts_rub) AS buyouts_rub,
    SUM(x.marketplace_fee_rub) AS marketplace_fee_rub,
    SUM(x.logistics_cost_positive) AS logistics_cost_positive,
    SUM(IFNULL(x.ad_spend_attributed_rub, 0)) AS ad_spend_attributed_rub,
    IF(COUNTIF(NOT x.cogs_covered) = 0, SUM(x.net_cogs), NULL) AS net_product_cogs_rub,
    COUNTIF(NOT x.cogs_covered) AS days_without_cogs
  FROM base x
  JOIN win w ON w.nm_id = x.nm_id
  WHERE x.day >= w.econ_window_start AND x.day <= w.econ_as_of
  GROUP BY w.policy_id, x.nm_id
),
ref28 AS (
  -- Окно Ads-4 без обрезки: (as_of − 28, as_of]; расход — MART_SKU_DAILY.ad_spend, как в Ads-4.
  SELECT
    x.nm_id,
    SUM(x.buyouts_rub) AS buyouts_rub,
    SUM(x.marketplace_fee_rub) AS marketplace_fee_rub,
    SUM(x.logistics_cost_positive) AS logistics_cost_positive,
    SUM(x.mart_ad_spend) AS ad_spend_rub,
    SUM(IFNULL(x.ad_spend_attributed_rub, 0)) AS fact_ad_spend_rub,
    IF(COUNTIF(NOT x.cogs_covered) = 0, SUM(x.net_cogs), NULL) AS net_product_cogs_rub
  FROM base x
  GROUP BY x.nm_id
),
fwd AS (
  SELECT f.nm_id, f.economics_status, f.economics_confidence, f.economics_calculated_at,
         f.seller_effective_price_rub, f.contribution_before_ads_pre_tax_rub, f.max_affordable_drr_pre_tax_pct,
         f.effective_commission_pct, f.acquiring_p50_pct, f.acquiring_p75_pct, f.acquiring_p90_pct,
         f.commission_rub_at_current, f.acquiring_expected_rub, f.logistics_expected_rub, f.cogs_rub,
         f.break_even_before_ads_pre_tax_base, f.break_even_before_ads_pre_tax_conservative,
         f.break_even_before_ads_pre_tax_stress
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_SKU_FORWARD_ECONOMICS_CURRENT` f
),
calc AS (
  SELECT
    a.*,
    a.buyouts_rub - a.marketplace_fee_rub - a.logistics_cost_positive - a.net_product_cogs_rub
      AS contribution_before_ads_rub
  FROM aie a
)
SELECT
  c.as_of_date,
  c.knowledge_ts,
  c.run_mode,
  w.policy_id,
  w.nm_id,
  w.internal_sku,
  w.is_bundle,
  b.finance_known_through,
  w.econ_as_of,
  w.econ_window_start,
  IFNULL(k.econ_window_days, 0) AS econ_window_days,
  -- FINANCIAL_DATA_MATURE: фактическое окно не заходит за последнюю дату с финансами.
  (w.econ_as_of <= b.finance_known_through) AS financial_data_mature,
  DATE_DIFF(c.as_of_date, w.econ_as_of, DAY) AS econ_window_end_lag_days,
  w.price_regime_start,
  w.price_first_observed_date,
  (w.price_first_observed_date IS NULL OR w.price_first_observed_date > w.econ_window_start) AS price_unobserved_part,
  -- ── фактическая экономика на окне AIE ──
  k.orders_qty,
  k.buyouts_qty,
  k.buyouts_rub,
  k.marketplace_fee_rub,
  k.logistics_cost_positive,
  k.net_product_cogs_rub,
  k.days_without_cogs,
  k.ad_spend_attributed_rub,
  k.contribution_before_ads_rub,
  k.contribution_before_ads_rub - k.ad_spend_attributed_rub AS contribution_after_ads_and_product_cogs_rub,
  SAFE_DIVIDE(k.ad_spend_attributed_rub, NULLIF(k.buyouts_rub, 0)) AS actual_ad_drr,
  SAFE_DIVIDE(k.contribution_before_ads_rub, NULLIF(k.buyouts_rub, 0)) AS max_ad_drr_breakeven,
  CASE
    -- Окно пусто: смена цены (P7) позже последнего дня с известными финансами. Это не пропуск себестоимости.
    WHEN k.nm_id IS NULL THEN 'EMPTY_WINDOW'
    WHEN k.net_product_cogs_rub IS NULL THEN 'MISSING_COGS'
    WHEN IFNULL(k.buyouts_rub, 0) <= 0 THEN 'NO_REVENUE'
    WHEN k.contribution_before_ads_rub < 0 THEN 'NEGATIVE_BEFORE_ADS'
    WHEN IFNULL(k.ad_spend_attributed_rub, 0) = 0 THEN 'NO_AD_SPEND'
    WHEN k.ad_spend_attributed_rub > k.contribution_before_ads_rub THEN 'ABOVE_BREAKEVEN'
    ELSE 'BELOW_BREAKEVEN'
  END AS economic_state,
  -- ── паритет с V_ADS_SKU_ECONOMIC_LIMITS (окно 28, без обрезки) ──
  r.buyouts_rub AS ref28_buyouts_rub,
  r.marketplace_fee_rub AS ref28_marketplace_fee_rub,
  r.logistics_cost_positive AS ref28_logistics_cost_positive,
  r.net_product_cogs_rub AS ref28_net_product_cogs_rub,
  r.ad_spend_rub AS ref28_ad_spend_rub,
  r.fact_ad_spend_rub AS ref28_fact_ad_spend_rub,
  r.buyouts_rub - r.marketplace_fee_rub - r.logistics_cost_positive - r.net_product_cogs_rub AS ref28_contribution_before_ads_rub,
  SAFE_DIVIDE(r.ad_spend_rub, NULLIF(r.buyouts_rub, 0)) AS ref28_actual_ad_drr,
  SAFE_DIVIDE(r.buyouts_rub - r.marketplace_fee_rub - r.logistics_cost_positive - r.net_product_cogs_rub,
              NULLIF(r.buyouts_rub, 0)) AS ref28_max_ad_drr_breakeven,
  -- ── прогнозная экономика WB_FE_V1 (только CURRENT) ──
  CASE
    WHEN c.run_mode != 'CURRENT' THEN 'SKIPPED_NOT_REPLAYABLE'
    WHEN f.nm_id IS NULL THEN 'MISSING'
    WHEN f.economics_status = 'BLOCKED' THEN 'BLOCKED'
    ELSE 'AVAILABLE'
  END AS forward_availability,
  f.economics_status AS forward_economics_status,
  f.economics_confidence AS forward_economics_confidence,
  f.economics_calculated_at AS forward_calculated_at,
  f.seller_effective_price_rub AS forward_price_rub,
  f.contribution_before_ads_pre_tax_rub AS forward_contribution_before_ads_rub,
  f.max_affordable_drr_pre_tax_pct AS forward_max_affordable_drr_pre_tax_pct,
  -- base — значение контракта WB_FE_V1 (ROUND до 0,01 п.п.); тождество с формулой ниже проверяет AIE_E05.
  f.max_affordable_drr_pre_tax_pct / 100 AS forward_max_drr_base,
  (1 - (f.effective_commission_pct + f.acquiring_p50_pct) / 100)
    * (1 - SAFE_DIVIDE(f.break_even_before_ads_pre_tax_base, f.seller_effective_price_rub)) AS forward_max_drr_base_identity,
  (1 - (f.effective_commission_pct + f.acquiring_p75_pct) / 100)
    * (1 - SAFE_DIVIDE(f.break_even_before_ads_pre_tax_conservative, f.seller_effective_price_rub)) AS forward_max_drr_conservative,
  (1 - (f.effective_commission_pct + f.acquiring_p90_pct) / 100)
    * (1 - SAFE_DIVIDE(f.break_even_before_ads_pre_tax_stress, f.seller_effective_price_rub)) AS forward_max_drr_stress,
  -- ── разложение прогнозной цены (base, диагностика PRICE_BELOW_BREAKEVEN / PRICING_REVIEW) ──
  f.break_even_before_ads_pre_tax_base AS fwd_breakeven_price_rub,
  f.commission_rub_at_current AS fwd_commission_rub,
  f.effective_commission_pct AS fwd_commission_pct,
  f.acquiring_expected_rub AS fwd_acquiring_rub,
  f.acquiring_p50_pct AS fwd_acquiring_pct,
  f.logistics_expected_rub AS fwd_logistics_rub,
  100 * SAFE_DIVIDE(f.logistics_expected_rub, f.seller_effective_price_rub) AS fwd_logistics_pct,
  f.cogs_rub AS fwd_product_cogs_rub,
  100 * SAFE_DIVIDE(f.cogs_rub, f.seller_effective_price_rub) AS fwd_product_cogs_pct,
  -- ── структурная экономика (L4): отрицательно во ВСЕХ доступных оптимистичных базах ──
  (k.net_product_cogs_rub IS NOT NULL AND k.contribution_before_ads_rub < 0) AS realized_negative_before_ads,
  (c.run_mode = 'CURRENT' AND f.economics_status IS NOT NULL AND f.economics_status != 'BLOCKED'
     AND f.contribution_before_ads_pre_tax_rub < 0) AS forward_base_negative,
  CASE
    WHEN c.run_mode = 'CURRENT' AND f.economics_status IS NOT NULL AND f.economics_status != 'BLOCKED'
      THEN (f.contribution_before_ads_pre_tax_rub < 0
            AND (k.net_product_cogs_rub IS NULL OR k.contribution_before_ads_rub < 0))
    ELSE FALSE
  END AS negative_all_available_bases
FROM win w
CROSS JOIN aie_clock c
CROSS JOIN bounds b
LEFT JOIN calc k ON k.nm_id = w.nm_id AND k.policy_id = w.policy_id
LEFT JOIN ref28 r ON r.nm_id = w.nm_id
LEFT JOIN fwd f ON f.nm_id = w.nm_id;
