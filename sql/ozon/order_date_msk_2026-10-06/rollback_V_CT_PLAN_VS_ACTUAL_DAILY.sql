-- Откат 2026-10-06: тело production до перевода даты заказа Ozon на МСК (снято INFORMATION_SCHEMA.VIEWS).
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_PLAN_VS_ACTUAL_DAILY`
OPTIONS (description = "Control Tower: план (ACTIVE-версия, date × target) против факта (V_CT_ACTUAL_DAILY). Grain: d × marketplace × internal_sku × sales_mode. Заказы ≠ продажи: actual_cards = заказы без отмен (leading), actual_cards_sold = выкупы/delivered (money basis). MTD/attainment считаются только по дням внутри горизонта плана (in_plan); forecast_month_end = MTD + средний темп 7 дн × оставшиеся дни. Вклад = НЕ прибыль (до OPEX).")
AS
WITH asof AS (
  SELECT 'WB' AS marketplace, MAX(day) AS data_as_of FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_SKU_DAILY`
  UNION ALL
  SELECT 'OZON', MAX(order_date) FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_POSTINGS_FBO`
),
p AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_PLAN_ACTIVE`),
a AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_ACTUAL_DAILY` WHERE d >= DATE '2026-09-01'),
j AS (
  SELECT
    COALESCE(p.plan_date, a.d) AS d,
    COALESCE(p.marketplace, a.marketplace) AS marketplace,
    COALESCE(p.internal_sku, a.internal_sku) AS internal_sku,
    COALESCE(p.sales_mode, a.sales_mode) AS sales_mode,
    COALESCE(p.bundle_id, IF(a.sales_mode = 'BUNDLE', a.internal_sku, NULL)) AS bundle_id,
    COALESCE(p.component_count, a.component_count) AS component_count,
    p.plan_version, p.product_name,
    p.plan_date IS NOT NULL AS in_plan,
    p.target_cards, p.target_physical_units, p.target_gmv, p.target_marketplace_costs, p.target_ad_spend, p.target_seller_cash, p.target_cogs, p.target_contribution,
    a.cards_ordered AS actual_cards, a.cards_sold AS actual_cards_sold, a.cards_cancelled AS actual_cards_cancelled,
    a.units_ordered AS actual_physical_units, a.units_sold AS actual_physical_units_sold,
    a.gmv_ordered AS actual_gmv, a.revenue_seller_base AS actual_revenue_seller_base,
    a.marketplace_costs AS actual_marketplace_costs, a.ad_spend AS actual_ad_spend,
    a.seller_cash AS actual_seller_cash, a.cogs AS actual_cogs, a.contribution AS actual_contribution,
    a.contribution_covered, a.cogs_covered
  FROM p FULL OUTER JOIN a
    ON a.d = p.plan_date AND a.marketplace = p.marketplace AND a.internal_sku = p.internal_sku AND a.sales_mode = p.sales_mode
),
e AS (
  SELECT j.*, s.data_as_of,
    DATE_TRUNC(j.d, MONTH) AS month,
    EXTRACT(DAY FROM LAST_DAY(j.d)) AS days_in_month,
    EXTRACT(DAY FROM j.d) AS day_of_month,
    j.d <= s.data_as_of AS is_past,
    IF(j.d <= s.data_as_of, IFNULL(j.actual_cards, 0), NULL) AS actual_cards_f,
    IF(j.d <= s.data_as_of, IFNULL(j.actual_physical_units, 0), NULL) AS actual_units_f,
    IF(j.d <= s.data_as_of, IFNULL(j.actual_gmv, 0), NULL) AS actual_gmv_f,
    IF(j.d <= s.data_as_of, IFNULL(j.actual_contribution, 0), NULL) AS actual_contrib_f
  FROM j LEFT JOIN asof s ON s.marketplace = j.marketplace
)
SELECT
  d, marketplace, internal_sku, sales_mode, bundle_id, component_count, plan_version, product_name, in_plan, is_past, data_as_of, month, days_in_month, day_of_month,
  target_cards, target_physical_units, target_gmv, target_marketplace_costs, target_ad_spend, target_seller_cash, target_cogs, target_contribution,
  actual_cards, actual_cards_sold, actual_cards_cancelled, actual_physical_units, actual_physical_units_sold,
  actual_gmv, actual_revenue_seller_base, actual_marketplace_costs, actual_ad_spend, actual_seller_cash, actual_cogs, actual_contribution,
  contribution_covered, cogs_covered,
  actual_cards_f - target_cards AS delta_cards,
  SAFE_DIVIDE(actual_cards_f, target_cards) * 100 AS attainment_pct,
  actual_units_f - target_physical_units AS delta_physical_units,
  SUM(target_cards) OVER w_month AS month_target_cards,
  SUM(target_physical_units) OVER w_month AS month_target_physical_units,
  SUM(target_gmv) OVER w_month AS month_target_gmv,
  SUM(target_contribution) OVER w_month AS month_target_contribution,
  SUM(target_cards) OVER w_mtd AS mtd_target_cards,
  SUM(target_physical_units) OVER w_mtd AS mtd_target_physical_units,
  SUM(target_gmv) OVER w_mtd AS mtd_target_gmv,
  SUM(target_contribution) OVER w_mtd AS mtd_target_contribution,
  SUM(actual_cards_f) OVER w_mtd AS mtd_actual_cards,
  SUM(IF(in_plan, actual_cards_f, NULL)) OVER w_mtd AS mtd_actual_cards_in_plan,
  SUM(actual_units_f) OVER w_mtd AS mtd_actual_physical_units,
  SUM(actual_gmv_f) OVER w_mtd AS mtd_actual_gmv,
  SUM(actual_contrib_f) OVER w_mtd AS mtd_actual_contribution,
  SAFE_DIVIDE(SUM(IF(in_plan, actual_cards_f, NULL)) OVER w_mtd, SUM(target_cards) OVER w_mtd) * 100 AS mtd_attainment_pct,
  SAFE_DIVIDE(SUM(target_cards) OVER w_month - SUM(actual_cards_f) OVER w_mtd, days_in_month - day_of_month) AS required_daily_velocity_remaining,
  SUM(actual_cards_f) OVER w_mtd + AVG(actual_cards_f) OVER w_7d * (days_in_month - day_of_month) AS forecast_month_end_cards
FROM e
WINDOW
  w_month AS (PARTITION BY month, marketplace, internal_sku, sales_mode),
  w_mtd AS (PARTITION BY month, marketplace, internal_sku, sales_mode ORDER BY d ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW),
  w_7d AS (PARTITION BY marketplace, internal_sku, sales_mode ORDER BY d ROWS BETWEEN 6 PRECEDING AND CURRENT ROW);
