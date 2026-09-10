-- =====================================================================================
-- CONTROL TOWER PHASE 1 — DAILY TARGET CURVE + MATERIALISED DAILY PLAN
-- =====================================================================================
-- Назначение : детерминированно построить суточную кривую спроса (CT_DAILY_CURVE) и
--              разложить месячный план (CT_SEASON_PLAN_MONTHLY) по дням в CT_SEASON_PLAN.
-- Зерно      : CT_DAILY_CURVE   — curve_version × marketplace × plan_date
--              CT_SEASON_PLAN   — plan_version × plan_date × marketplace × internal_sku × sales_mode
-- Источник   : CT_SEASON_PLAN_MONTHLY (seed из docs/analysis/season_2026_2027/control_tower/
--              SET_MONTHLY_PLAN.csv) × кривая ниже.
-- Владелец   : аналитика EVETIS. Обновление: только новой версией плана (immutable).
-- Откат      : DELETE по curve_version / plan_version; старые версии не трогаются.
--
-- ВОСПРОИЗВОДИМОСТЬ. Файл полностью детерминирован: те же входы дают ту же кривую и
--   тот же дневной план. Проверка сходимости — ct_phase1_validation.sql, тесты T3–T5.
--
-- ОТКУДА ФАКТОРЫ ДНЯ НЕДЕЛИ. Оценены на истории wb_mart.FACT_ORDERS (WB) за сезон
--   2025/26; для Ozon амплитуда демпфирована (канал моложе, выборка меньше).
--   Понедельник = 2 в EXTRACT(DAYOFWEEK), воскресенье = 1.
-- =====================================================================================

-- ── 1. Суточная кривая ──────────────────────────────────────────────────────────────
INSERT INTO `project-fa311fc0-4d87-4781-986.evetis_ref.CT_DAILY_CURVE`
  (curve_version, marketplace, plan_date, dow_factor, event_factor, event_label, raw_weight, created_at)
WITH horizon AS (
  SELECT d FROM UNNEST(GENERATE_DATE_ARRAY(DATE '2026-09-01', DATE '2027-03-31')) AS d
),
mp AS (SELECT 'WB' AS marketplace UNION ALL SELECT 'OZON'),
dow AS (
  -- WB: сезон 2025/26, FACT_ORDERS. OZON: та же форма, амплитуда демпфирована.
  SELECT 'WB' AS marketplace, 1 AS dow, 0.88 AS f UNION ALL
  SELECT 'WB', 2, 1.09 UNION ALL SELECT 'WB', 3, 1.14 UNION ALL SELECT 'WB', 4, 1.05 UNION ALL
  SELECT 'WB', 5, 1.05 UNION ALL SELECT 'WB', 6, 0.98 UNION ALL SELECT 'WB', 7, 0.81 UNION ALL
  SELECT 'OZON', 1, 0.95 UNION ALL SELECT 'OZON', 2, 1.05 UNION ALL SELECT 'OZON', 3, 1.05 UNION ALL
  SELECT 'OZON', 4, 1.00 UNION ALL SELECT 'OZON', 5, 1.05 UNION ALL SELECT 'OZON', 6, 1.00 UNION ALL
  SELECT 'OZON', 7, 0.90
),
ev AS (
  SELECT DATE '2026-11-09' AS d_from, DATE '2026-11-11' AS d_to, '11.11'              AS label, 1.80 AS f UNION ALL
  SELECT DATE '2026-11-12', DATE '2026-11-13', '11.11 tail',        1.20 UNION ALL
  SELECT DATE '2026-11-27', DATE '2026-11-30', 'Black Friday',      1.50 UNION ALL
  SELECT DATE '2026-12-15', DATE '2026-12-24', 'NY gifting peak',   1.35 UNION ALL
  SELECT DATE '2026-12-29', DATE '2026-12-31', 'pre-NY fade',       0.60 UNION ALL
  SELECT DATE '2027-01-01', DATE '2027-01-03', 'NY holidays',       0.35 UNION ALL
  SELECT DATE '2027-01-04', DATE '2027-01-08', 'NY holidays tail',  0.60 UNION ALL
  SELECT DATE '2027-02-08', DATE '2027-02-13', '14 Feb window',     1.30 UNION ALL
  SELECT DATE '2027-02-17', DATE '2027-02-22', '23 Feb window',     1.20 UNION ALL
  SELECT DATE '2027-02-23', DATE '2027-02-23', '23 Feb holiday',    0.70 UNION ALL
  SELECT DATE '2027-03-01', DATE '2027-03-06', '8 March window',    1.60 UNION ALL
  SELECT DATE '2027-03-07', DATE '2027-03-07', '8 March eve',       1.20 UNION ALL
  SELECT DATE '2027-03-08', DATE '2027-03-08', '8 March holiday',   0.50 UNION ALL
  SELECT DATE '2027-03-09', DATE '2027-03-10', 'post 8 March',      0.70
)
SELECT 'CURVE_v1_2026-09-10' AS curve_version, mp.marketplace, h.d AS plan_date,
  dow.f AS dow_factor,
  IFNULL(ev.f, 1.0) AS event_factor,
  ev.label AS event_label,
  dow.f * IFNULL(ev.f, 1.0) AS raw_weight,
  CURRENT_TIMESTAMP() AS created_at
FROM horizon h
CROSS JOIN mp
JOIN dow ON dow.marketplace = mp.marketplace AND dow.dow = EXTRACT(DAYOFWEEK FROM h.d)
LEFT JOIN ev ON h.d BETWEEN ev.d_from AND ev.d_to;

-- ── 2. Материализация дневного плана ────────────────────────────────────────────────
-- Вес дня нормируется внутри месяца × канала, поэтому сумма по месяцу в точности
-- равна месячному плану (тест T4). Денежные строки распределяются тем же весом.
INSERT INTO `project-fa311fc0-4d87-4781-986.evetis_ref.CT_SEASON_PLAN`
  (plan_version, plan_status, plan_date, month, marketplace, internal_sku, product_name, sales_mode, bundle_id,
   component_count, target_cards, target_physical_units, target_gmv, target_marketplace_costs, target_ad_spend,
   target_seller_cash, target_cogs, target_contribution, day_weight, curve_version, created_at, source_artifact, assumption_version)
WITH v AS (SELECT plan_version, curve_version, source_artifact, assumption_version FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_PLAN_VERSION` WHERE plan_version = 'SET_2026-09-09'),
bom AS (SELECT card_sku, MAX(component_count) AS component_count, LOGICAL_OR(is_bundle) AS is_bundle FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_BOM_CURRENT` GROUP BY 1),
w AS (
  -- Нормировка ТОЛЬКО по плановым дням: сентябрь стартует 09.09 (1–8.09 — факт без цели),
  -- поэтому сумма day_weight внутри месяца × канала равна ровно 1,0 по 22 дням сентября.
  SELECT c.marketplace, c.plan_date, DATE_TRUNC(c.plan_date, MONTH) AS month, c.raw_weight,
    SAFE_DIVIDE(c.raw_weight, SUM(c.raw_weight) OVER (PARTITION BY c.marketplace, DATE_TRUNC(c.plan_date, MONTH))) AS day_weight
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_DAILY_CURVE` c
  CROSS JOIN v
  JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.CT_PLAN_VERSION` pv ON pv.plan_version = v.plan_version
  WHERE c.curve_version = v.curve_version AND c.plan_date BETWEEN pv.horizon_from AND pv.horizon_to
)
SELECT v.plan_version, 'ACTIVE' AS plan_status, w.plan_date, w.month, m.marketplace, m.internal_sku,
  COALESCE(pm.product_name_short, pm.canonical_product_name, m.internal_sku) AS product_name,
  m.sales_mode, IF(m.sales_mode = 'BUNDLE', m.internal_sku, NULL) AS bundle_id,
  IFNULL(b.component_count, 1) AS component_count,
  m.target_cards * w.day_weight, m.target_physical_units * w.day_weight, m.target_gmv * w.day_weight,
  m.target_marketplace_costs * w.day_weight, m.target_ad_spend * w.day_weight, m.target_seller_cash * w.day_weight,
  m.target_cogs * w.day_weight, m.target_contribution * w.day_weight,
  w.day_weight, v.curve_version, CURRENT_TIMESTAMP(), v.source_artifact, v.assumption_version
FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_SEASON_PLAN_MONTHLY` m
CROSS JOIN v
JOIN w ON w.month = m.month AND w.marketplace = m.marketplace
LEFT JOIN bom b ON b.card_sku = m.internal_sku
LEFT JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_PRODUCT_MASTER` pm ON pm.internal_sku = m.internal_sku
WHERE m.plan_version = v.plan_version AND m.target_cards > 0;
