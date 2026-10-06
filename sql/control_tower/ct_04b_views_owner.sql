-- =====================================================================================
-- CONTROL TOWER PHASE 1 (+1.1) — ВЛАДЕЛЬЧЕСКИЕ ВИТРИНЫ (wb_mart.V_CT_*)
-- =====================================================================================
-- Слой решений: правда о запасе (LIVE), план/факт, запас→деньги, наборы, потребность
-- в отгрузке, крем для рук, алерты, кандидаты действий, очередь, Owner Home.
-- Применять ПОСЛЕ ct_04a и ct_07 §1–2 (V_CT_OWNER_HOME читает V_CT_REFRESH_STATUS и CT_CONFIG).
-- Все объекты новые, префикс V_CT_. Phase 1.1 (10.09.2026): V_CT_ACTION_QUEUE и V_CT_OWNER_HOME расширены
-- человекочитаемыми полями, дорожками DECISION/EXECUTION, уверенностью прогноза и статусом обновления.
--
-- СЛОВАРЬ ДЕНЕГ (контракт проекта):
--   деньги продавца  = выручка по базе продавца − комиссии/логистика − реклама
--   вклад            = деньги продавца − COGS проданного      ← НЕ чистая прибыль
--   операционный рез = вклад − OPEX (пропорционально прошедшим дням месяца)
-- =====================================================================================


CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_INVENTORY_TRUTH_LIVE` AS
WITH bom AS (SELECT card_sku, component_sku, component_qty, is_bundle FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_BOM_CURRENT`),
base AS (SELECT internal_sku, COALESCE(product_name_short, canonical_product_name) AS product_name, product_line FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_PRODUCT_MASTER` WHERE NOT is_bundle),
snap AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_STOCK_SNAPSHOT` WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_STOCK_SNAPSHOT`)),
flow AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_SUPPLY_FLOW`),
wb_raw AS (
  SELECT t.internal_sku AS card_sku, t.row_type, t.warehouse_name, t.quantity, t._snapshot_date
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_STOCKS_T5_CURRENT` t WHERE t.internal_sku IS NOT NULL
),
wb AS (
  SELECT b.component_sku AS internal_sku,
    SUM(IF(w.row_type = 'AGGREGATE', w.quantity * b.component_qty, 0)) AS wb_fbo_live_units,
    SUM(IF(w.row_type = 'AGGREGATE' AND b.is_bundle, w.quantity * b.component_qty, 0)) AS wb_fbo_units_in_bundles,
    SUM(IF(w.row_type = 'WAREHOUSE', w.quantity * b.component_qty, 0)) AS wb_lost_claimed_units,
    SUM(IF(w.row_type = 'PSEUDO_TO_CLIENT', w.quantity * b.component_qty, 0)) AS wb_to_client_units,
    MAX(w._snapshot_date) AS wb_stock_as_of
  FROM wb_raw w JOIN bom b ON b.card_sku = w.card_sku GROUP BY 1
),
oz_snap AS (
  SELECT sku, warehouse_id, available_stock_count, transit_stock_count, snapshot_date FROM (
    SELECT sku, warehouse_id, available_stock_count, transit_stock_count, snapshot_date,
      ROW_NUMBER() OVER (PARTITION BY sku, warehouse_id ORDER BY snapshot_date DESC, extracted_at DESC) AS rn
    FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_STOCKS`
    WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_STOCKS`)) WHERE rn = 1
),
oz AS (
  SELECT b.component_sku AS internal_sku,
    SUM(o.available_stock_count * b.component_qty) AS ozon_fbo_units,
    SUM(IF(b.is_bundle, o.available_stock_count * b.component_qty, 0)) AS ozon_fbo_units_in_bundles,
    SUM(o.transit_stock_count * b.component_qty) AS ozon_api_transit_units,
    MAX(o.snapshot_date) AS ozon_stock_as_of
  FROM oz_snap o
  JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP` m ON m.marketplace = 'OZON' AND m.marketplace_sku = o.sku
  JOIN bom b ON b.card_sku = m.internal_sku GROUP BY 1
),
oz_since AS (
  SELECT internal_sku, SUM(units_ordered) AS ozon_units_ordered_since_snapshot
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_PHYSICAL_DAILY`
  WHERE marketplace = 'OZON' AND d >= (SELECT MAX(snapshot_date) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_STOCK_SNAPSHOT`) GROUP BY 1
),
cogs AS (SELECT internal_sku, product_cogs_rub FROM `project-fa311fc0-4d87-4781-986.evetis_ref.V_PRODUCT_COGS_EFFECTIVE` WHERE CURRENT_DATE() BETWEEN effective_from AND COALESCE(effective_to, DATE '9999-12-31')),
exp AS (SELECT internal_sku, MIN(expiry_date) AS expiry_date, ANY_VALUE(expiry_source) AS expiry_source FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_EXPIRY_BATCH` WHERE NOT is_inbound GROUP BY 1),
vel AS (
  SELECT internal_sku,
    SUM(IF(d > DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY), units_ordered, 0)) / 30.0 AS units_per_day_30d,
    SUM(IF(d > DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY), units_ordered, 0)) / 7.0 AS units_per_day_7d,
    SUM(IF(d > DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY), units_ordered_via_bundle, 0)) / 30.0 AS units_per_day_30d_via_bundle,
    SUM(IF(d >= DATE '2026-09-09', units_sold, 0)) AS units_sold_season_to_date
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_PHYSICAL_DAILY` WHERE d > DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY) OR d >= DATE '2026-09-09' GROUP BY 1
),
plan45 AS (
  SELECT b.component_sku AS internal_sku, SUM(p.target_cards * b.component_qty) AS plan_units_next_45d,
    SUM(p.target_cards * b.component_qty) / 45.0 AS plan_units_per_day_next_45d
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_PLAN_ACTIVE` p JOIN bom b ON b.card_sku = p.internal_sku
  WHERE p.plan_date BETWEEN CURRENT_DATE() AND DATE_ADD(CURRENT_DATE(), INTERVAL 44 DAY) GROUP BY 1
),
plan_march AS (
  SELECT b.component_sku AS internal_sku, SUM(p.target_cards * b.component_qty) AS plan_units_to_season_end
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_PLAN_ACTIVE` p JOIN bom b ON b.card_sku = p.internal_sku
  WHERE p.plan_date >= CURRENT_DATE() GROUP BY 1
),
j AS (
  SELECT base.internal_sku, base.product_name, base.product_line,
    s.snapshot_date AS ff_snapshot_date,
    IFNULL(s.ff_operational_units, 0) AS ff_operational_snapshot,
    IFNULL(s.ff_pallet_units, 0) AS ff_pallet_snapshot,
    IFNULL(s.in_transit_to_ozon_units, 0) AS ozon_transit_snapshot,
    IFNULL(s.ozon_fbo_units, 0) AS ozon_fbo_snapshot,
    IFNULL(f.ozon_post_snapshot_open_units, 0) + IFNULL(f.ozon_post_snapshot_completed_units, 0) + IFNULL(f.wb_post_snapshot_in_transit_units, 0) + IFNULL(f.wb_post_snapshot_accepted_units, 0) AS ff_outflow_since_snapshot,
    IFNULL(f.ozon_seed_era_completed_units, 0) AS ozon_seed_era_completed_units,
    IFNULL(f.ozon_post_snapshot_open_units, 0) AS ozon_post_snapshot_open_units,
    IFNULL(f.ozon_post_snapshot_completed_units, 0) AS ozon_post_snapshot_completed_units,
    IFNULL(z.ozon_units_ordered_since_snapshot, 0) AS ozon_units_ordered_since_snapshot,
    IFNULL(f.wb_post_snapshot_in_transit_units, 0) AS wb_in_transit_units,
    IFNULL(wb.wb_fbo_live_units, 0) AS wb_fbo_live_units,
    IFNULL(wb.wb_fbo_units_in_bundles, 0) AS wb_fbo_units_in_bundles,
    IFNULL(wb.wb_lost_claimed_units, 0) AS wb_lost_claimed_units,
    IFNULL(wb.wb_to_client_units, 0) AS wb_to_client_units,
    wb.wb_stock_as_of,
    IFNULL(oz.ozon_fbo_units, 0) AS ozon_fbo_units,
    IFNULL(oz.ozon_fbo_units_in_bundles, 0) AS ozon_fbo_units_in_bundles,
    IFNULL(oz.ozon_api_transit_units, 0) AS ozon_api_transit_units,
    oz.ozon_stock_as_of,
    IFNULL(s.fbs_units, 0) AS fbs_units,
    IFNULL(s.inbound_units, 0) AS inbound_units, s.inbound_status, s.inbound_eta,
    c.product_cogs_rub AS unit_cogs_rub,
    e.expiry_date, e.expiry_source,
    IFNULL(v.units_per_day_30d, 0) AS units_per_day_30d, IFNULL(v.units_per_day_7d, 0) AS units_per_day_7d,
    IFNULL(v.units_per_day_30d_via_bundle, 0) AS units_per_day_30d_via_bundle,
    IFNULL(v.units_sold_season_to_date, 0) AS units_sold_season_to_date,
    IFNULL(p45.plan_units_next_45d, 0) AS plan_units_next_45d, IFNULL(p45.plan_units_per_day_next_45d, 0) AS plan_units_per_day_next_45d,
    IFNULL(pm.plan_units_to_season_end, 0) AS plan_units_to_season_end
  FROM base
  LEFT JOIN snap s ON s.internal_sku = base.internal_sku
  LEFT JOIN flow f ON f.internal_sku = base.internal_sku
  LEFT JOIN wb ON wb.internal_sku = base.internal_sku
  LEFT JOIN oz ON oz.internal_sku = base.internal_sku
  LEFT JOIN oz_since z ON z.internal_sku = base.internal_sku
  LEFT JOIN cogs c ON c.internal_sku = base.internal_sku
  LEFT JOIN exp e ON e.internal_sku = base.internal_sku
  LEFT JOIN vel v ON v.internal_sku = base.internal_sku
  LEFT JOIN plan45 p45 ON p45.internal_sku = base.internal_sku
  LEFT JOIN plan_march pm ON pm.internal_sku = base.internal_sku
),
k AS (
  SELECT j.*,
    GREATEST(ff_operational_snapshot + ff_pallet_snapshot - ff_outflow_since_snapshot, 0) AS ff_total_units,
    GREATEST(ozon_fbo_units - ozon_fbo_snapshot + ozon_units_ordered_since_snapshot, 0) AS ozon_landed_since_snapshot,
    GREATEST(ozon_transit_snapshot + ozon_post_snapshot_open_units + ozon_post_snapshot_completed_units
             - GREATEST(ozon_fbo_units - ozon_fbo_snapshot + ozon_units_ordered_since_snapshot, 0), 0) AS ozon_transit_units,
    IF(inbound_units > 0 AND (inbound_eta IS NULL OR inbound_eta <= DATE '2027-03-31'), inbound_units, 0) AS inbound_expected_in_season
  FROM j
),
m AS (
  SELECT k.*,
    ff_total_units + wb_fbo_live_units + ozon_fbo_units + ozon_transit_units + wb_in_transit_units + fbs_units AS sellable_units,
    wb_fbo_live_units + ozon_fbo_units + fbs_units AS marketplace_units,
    wb_fbo_units_in_bundles + ozon_fbo_units_in_bundles AS assembled_bundle_units_on_marketplaces,
    DATE_DIFF(expiry_date, CURRENT_DATE(), DAY) AS days_to_expiry
  FROM k
),
n AS (
  SELECT m.*,
    sellable_units * unit_cogs_rub AS sellable_value_rub,
    SAFE_DIVIDE(sellable_units, NULLIF(units_per_day_30d, 0)) AS days_of_stock_total_at_30d_rate,
    SAFE_DIVIDE(marketplace_units, NULLIF(units_per_day_30d, 0)) AS days_of_stock_marketplace_at_30d_rate,
    SAFE_DIVIDE(sellable_units, NULLIF(plan_units_per_day_next_45d, 0)) AS days_of_stock_total_at_plan_rate,
    plan_units_next_45d AS target_stock_units_45d,
    SAFE_DIVIDE(sellable_units, NULLIF(days_to_expiry / 30.44, 0)) AS required_units_per_month_to_expiry,
    units_per_day_30d * 30.44 AS current_units_per_month,
    GREATEST(sellable_units + inbound_expected_in_season - plan_units_to_season_end, 0) AS projected_residual_31_03_2027_at_plan,
    GREATEST(plan_units_to_season_end - sellable_units - inbound_expected_in_season, 0) AS projected_shortfall_31_03_2027_at_plan,
    GREATEST(sellable_units + inbound_expected_in_season - units_per_day_30d * DATE_DIFF(DATE '2027-03-31', CURRENT_DATE(), DAY), 0) AS projected_residual_31_03_2027_at_30d_rate
  FROM m
)
SELECT n.*,
  CASE
    WHEN inbound_units > 0 AND sellable_units <= 20 THEN 'BLUE'
    WHEN days_to_expiry < 120 THEN 'RED'
    WHEN units_per_day_30d >= 1 AND days_of_stock_marketplace_at_30d_rate < 10 THEN 'RED'
    WHEN units_per_day_30d >= 1 AND days_of_stock_marketplace_at_30d_rate < 20 THEN 'YELLOW'
    WHEN days_of_stock_total_at_plan_rate > 365 THEN 'RED'
    WHEN days_to_expiry < 270 THEN 'YELLOW'
    ELSE 'GREEN'
  END AS status,
  CASE
    WHEN inbound_units > 0 AND sellable_units <= 20 THEN 'INBOUND'
    WHEN days_to_expiry < 120 THEN 'EXPIRY_HARD'
    WHEN units_per_day_30d >= 1 AND days_of_stock_marketplace_at_30d_rate < 10 THEN 'STOCKOUT'
    WHEN units_per_day_30d >= 1 AND days_of_stock_marketplace_at_30d_rate < 20 THEN 'LOW_MARKETPLACE'
    WHEN days_of_stock_total_at_plan_rate > 365 THEN 'OVERSTOCK'
    WHEN days_to_expiry < 270 THEN 'EXPIRY_WATCH'
    ELSE 'OK'
  END AS status_code,
  CASE
    WHEN inbound_units > 0 AND sellable_units <= 20 THEN 'ждём партию (inbound), продаваемый запас ≈ 0'
    WHEN days_to_expiry < 120 THEN 'срок годности < 4 мес — ликвидация до нуля'
    WHEN units_per_day_30d >= 1 AND days_of_stock_marketplace_at_30d_rate < 10 THEN 'на площадках < 10 дней при спросе — отгрузить сейчас'
    WHEN units_per_day_30d >= 1 AND days_of_stock_marketplace_at_30d_rate < 20 THEN 'на площадках 10–20 дней — планировать отгрузку'
    WHEN days_of_stock_total_at_plan_rate > 365 THEN 'запас > 365 дней плана — ускорять через наборы / Ozon / внешний канал'
    WHEN days_to_expiry < 270 THEN 'срок годности < 9 мес — держать темп'
    ELSE 'в норме'
  END AS status_reason
FROM n
;

-- ── План против факта, суточное зерно ───────────────────────────────────────────────
-- Grain: date × marketplace × internal_sku × sales_mode (FULL JOIN плана и факта).
-- in_plan  — строка покрыта планом; is_past — дата закрыта данными канала (data_as_of).
-- Оконные метрики: план и факт месяца, MTD, требуемый темп на остаток, прогноз EOM
-- по среднему за 7 дней. Отмены исключены (cards_ordered), выкупы отдельным полем.
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_PLAN_VS_ACTUAL_DAILY`
OPTIONS (description = "Control Tower: план (ACTIVE-версия, date × target) против факта (V_CT_ACTUAL_DAILY). Grain: d × marketplace × internal_sku × sales_mode. Заказы ≠ продажи: actual_cards = заказы без отмен (leading), actual_cards_sold = выкупы/delivered (money basis). MTD/attainment считаются только по дням внутри горизонта плана (in_plan); forecast_month_end = MTD + средний темп 7 дн × оставшиеся дни. Вклад = НЕ прибыль (до OPEX).")
AS
WITH asof AS (
  SELECT 'WB' AS marketplace, MAX(day) AS data_as_of FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_SKU_DAILY`
  UNION ALL
  SELECT 'OZON', MAX(DATE(created_at, 'Europe/Moscow')) FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_POSTINGS_FBO`
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

CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_CASH_CONVERSION` AS
WITH pv AS (
  SELECT plan_version, horizon_from, horizon_to, opening_inventory_as_of
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_PLAN_VERSION` WHERE plan_status = 'ACTIVE'
),
ref AS (SELECT DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY) AS yday),
months AS (
  SELECT m AS month FROM pv, UNNEST(GENERATE_DATE_ARRAY(DATE_TRUNC(pv.horizon_from, MONTH), DATE_TRUNC(pv.horizon_to, MONTH), INTERVAL 1 MONTH)) AS m
),
plan_m AS (
  SELECT month, SUM(target_cards) AS plan_cards, SUM(target_physical_units) AS plan_units, SUM(target_gmv) AS plan_gmv,
    SUM(target_ad_spend) AS plan_ad_spend, SUM(target_seller_cash) AS plan_seller_cash, SUM(target_cogs) AS plan_cogs, SUM(target_contribution) AS plan_contribution,
    MIN(plan_date) AS plan_from, MAX(plan_date) AS plan_to
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_PLAN_ACTIVE` GROUP BY 1
),
opex_m AS (SELECT month, SUM(amount_rub) AS opex_month FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_OPEX` WHERE is_active GROUP BY 1),
act_m AS (
  SELECT DATE_TRUNC(a.d, MONTH) AS month,
    SUM(a.cards_ordered) AS actual_cards, SUM(a.cards_sold) AS actual_cards_sold,
    SUM(a.units_ordered) AS actual_units, SUM(a.units_sold) AS actual_units_sold,
    SUM(a.gmv_ordered) AS actual_gmv, SUM(a.revenue_seller_base) AS actual_revenue_seller_base,
    SUM(a.marketplace_costs) AS actual_marketplace_costs, SUM(a.ad_spend) AS actual_ad_spend,
    SUM(a.seller_cash) AS actual_seller_cash, SUM(a.cogs) AS actual_cogs, SUM(a.contribution) AS actual_contribution,
    SUM(IF(a.d >= pv.opening_inventory_as_of, a.units_ordered, 0)) AS units_ordered_since_opening,
    SUM(IF(a.d >= pv.opening_inventory_as_of, a.units_sold, 0)) AS units_sold_since_opening,
    SUM(IF(a.d >= pv.opening_inventory_as_of, a.cogs, 0)) AS cogs_released_since_opening,
    SUM(IF(a.d >= pv.opening_inventory_as_of, a.seller_cash, 0)) AS seller_cash_since_opening
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_ACTUAL_DAILY` a CROSS JOIN pv CROSS JOIN ref
  WHERE a.d >= DATE_TRUNC(pv.horizon_from, MONTH) AND a.d <= ref.yday
  GROUP BY 1
),
opening AS (
  SELECT MAX(snapshot_date) AS opening_as_of, SUM(total_sellable_units) AS opening_units, SUM(sellable_value_rub) AS opening_value_rub,
    SUM(inbound_units) AS inbound_planned_units, SUM(inbound_units * unit_cogs_rub) AS inbound_planned_value_rub
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_STOCK_SNAPSHOT` WHERE is_season_opening
),
received AS (
  SELECT IFNULL(SUM(b.units_at_snapshot), 0) AS received_units, IFNULL(SUM(b.units_at_snapshot * c.product_cogs_rub), 0) AS received_value_rub
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_EXPIRY_BATCH` b
  CROSS JOIN opening o
  LEFT JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.V_PRODUCT_COGS_EFFECTIVE` c
    ON c.internal_sku = b.internal_sku AND CURRENT_DATE() BETWEEN c.effective_from AND COALESCE(c.effective_to, DATE '9999-12-31')
  WHERE NOT b.is_inbound AND b.import_date > o.opening_as_of
),
remaining AS (
  SELECT SUM(sellable_units) AS remaining_units, SUM(sellable_value_rub) AS remaining_value_rub,
    SUM(wb_lost_claimed_units) AS wb_lost_claimed_units, SUM(inbound_units) AS inbound_open_units,
    SUM(units_per_day_30d) AS units_per_day_30d, SUM(plan_units_to_season_end) AS plan_units_to_season_end,
    SUM(projected_residual_31_03_2027_at_plan) AS projected_residual_31_03_at_plan
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_INVENTORY_TRUTH`
),
m AS (
  SELECT mo.month,
    CASE WHEN LAST_DAY(mo.month) <= ref.yday THEN 'CLOSED' WHEN mo.month <= ref.yday THEN 'CURRENT' ELSE 'FUTURE' END AS month_status,
    EXTRACT(DAY FROM LAST_DAY(mo.month)) AS days_in_month,
    GREATEST(LEAST(DATE_DIFF(ref.yday, mo.month, DAY) + 1, EXTRACT(DAY FROM LAST_DAY(mo.month))), 0) AS days_elapsed,
    p.plan_from, p.plan_to,
    p.plan_cards, p.plan_units, p.plan_gmv, p.plan_ad_spend, p.plan_seller_cash, p.plan_cogs, p.plan_contribution,
    IFNULL(x.opex_month, 0) AS plan_opex,
    p.plan_contribution - IFNULL(x.opex_month, 0) AS plan_operating_result,
    a.actual_cards, a.actual_cards_sold, a.actual_units, a.actual_units_sold, a.actual_gmv, a.actual_revenue_seller_base,
    a.actual_marketplace_costs, a.actual_ad_spend, a.actual_seller_cash, a.actual_cogs, a.actual_contribution,
    IFNULL(x.opex_month, 0) AS opex_month,
    IFNULL(x.opex_month, 0) * SAFE_DIVIDE(GREATEST(LEAST(DATE_DIFF(ref.yday, mo.month, DAY) + 1, EXTRACT(DAY FROM LAST_DAY(mo.month))), 0), EXTRACT(DAY FROM LAST_DAY(mo.month))) AS opex_allocated,
    a.units_ordered_since_opening, a.units_sold_since_opening, a.cogs_released_since_opening, a.seller_cash_since_opening
  FROM months mo CROSS JOIN ref
  LEFT JOIN plan_m p ON p.month = mo.month
  LEFT JOIN opex_m x ON x.month = mo.month
  LEFT JOIN act_m a ON a.month = mo.month
),
rows_ AS (
  SELECT 'MONTH' AS period_type, FORMAT_DATE('%Y-%m', month) AS period_label, month, month_status, days_in_month, days_elapsed,
    month AS period_from, IF(month_status = 'FUTURE', NULL, LEAST(LAST_DAY(month), (SELECT yday FROM ref))) AS period_to, plan_from, plan_to,
    plan_cards, plan_units, plan_gmv, plan_ad_spend, plan_seller_cash, plan_cogs, plan_contribution, plan_opex, plan_operating_result,
    actual_cards, actual_cards_sold, actual_units, actual_units_sold, actual_gmv, actual_revenue_seller_base, actual_marketplace_costs, actual_ad_spend,
    actual_seller_cash, actual_cogs, actual_contribution, opex_allocated,
    IFNULL(actual_contribution, 0) - opex_allocated AS operating_result,
    units_ordered_since_opening, units_sold_since_opening, cogs_released_since_opening, seller_cash_since_opening,
    1 AS ord
  FROM m
  UNION ALL
  SELECT 'SEASON_TO_DATE', 'Сезон с 01.09 по вчера', NULL, 'CURRENT', NULL, NULL,
    MIN(month), (SELECT yday FROM ref), MIN(plan_from), (SELECT yday FROM ref),
    SUM(IF(month_status != 'FUTURE', plan_cards, 0)), SUM(IF(month_status != 'FUTURE', plan_units, 0)), SUM(IF(month_status != 'FUTURE', plan_gmv, 0)),
    SUM(IF(month_status != 'FUTURE', plan_ad_spend, 0)), SUM(IF(month_status != 'FUTURE', plan_seller_cash, 0)), SUM(IF(month_status != 'FUTURE', plan_cogs, 0)),
    SUM(IF(month_status != 'FUTURE', plan_contribution, 0)), SUM(IF(month_status != 'FUTURE', plan_opex, 0)), SUM(IF(month_status != 'FUTURE', plan_operating_result, 0)),
    SUM(actual_cards), SUM(actual_cards_sold), SUM(actual_units), SUM(actual_units_sold), SUM(actual_gmv), SUM(actual_revenue_seller_base), SUM(actual_marketplace_costs), SUM(actual_ad_spend),
    SUM(actual_seller_cash), SUM(actual_cogs), SUM(actual_contribution), SUM(opex_allocated),
    SUM(IFNULL(actual_contribution, 0)) - SUM(opex_allocated),
    SUM(units_ordered_since_opening), SUM(units_sold_since_opening), SUM(cogs_released_since_opening), SUM(seller_cash_since_opening),
    2
  FROM m
  UNION ALL
  SELECT 'SEASON_PLAN', 'Сезон целиком — план SET', NULL, 'PLAN', NULL, NULL,
    MIN(plan_from), MAX(plan_to), MIN(plan_from), MAX(plan_to),
    SUM(plan_cards), SUM(plan_units), SUM(plan_gmv), SUM(plan_ad_spend), SUM(plan_seller_cash), SUM(plan_cogs), SUM(plan_contribution), SUM(plan_opex), SUM(plan_operating_result),
    NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
    NULL, NULL, NULL, NULL,
    3
  FROM m
)
SELECT r.period_type, r.period_label, r.month, r.month_status, r.days_in_month, r.days_elapsed, r.period_from, r.period_to, r.plan_from, r.plan_to,
  r.plan_cards, r.plan_units, r.plan_gmv, r.plan_ad_spend, r.plan_seller_cash, r.plan_cogs, r.plan_contribution, r.plan_opex, r.plan_operating_result,
  r.actual_cards, r.actual_cards_sold, r.actual_units, r.actual_units_sold, r.actual_gmv, r.actual_revenue_seller_base, r.actual_marketplace_costs, r.actual_ad_spend,
  r.actual_seller_cash, r.actual_cogs, r.actual_contribution, r.opex_allocated, r.operating_result,
  SAFE_DIVIDE(r.actual_ad_spend, r.actual_gmv) * 100 AS drr_pct,
  SAFE_DIVIDE(r.actual_cards, r.plan_cards) * 100 AS attainment_cards_pct,
  SAFE_DIVIDE(r.actual_contribution, r.plan_contribution) * 100 AS attainment_contribution_pct,
  -- inventory block (season-level, source-driven; identical on every row)
  o.opening_as_of, o.opening_units, o.opening_value_rub, o.inbound_planned_units, o.inbound_planned_value_rub,
  rc.received_units, rc.received_value_rub,
  rm.remaining_units AS inventory_remaining_units, rm.remaining_value_rub AS inventory_remaining_value_rub,
  rm.wb_lost_claimed_units, rm.inbound_open_units,
  r.units_ordered_since_opening, r.units_sold_since_opening, r.cogs_released_since_opening, r.seller_cash_since_opening,
  CASE WHEN r.period_type = 'SEASON_PLAN' THEN o.opening_units + o.inbound_planned_units - r.plan_units
       ELSE o.opening_units + rc.received_units - r.units_ordered_since_opening END AS inventory_remaining_computed_units,
  CASE WHEN r.period_type = 'SEASON_PLAN' THEN NULL
       ELSE rm.remaining_units - (o.opening_units + rc.received_units - r.units_ordered_since_opening) END AS inventory_delta_units,
  CASE WHEN r.period_type = 'SEASON_PLAN' THEN SAFE_DIVIDE(r.plan_units, o.opening_units + o.inbound_planned_units) * 100
       ELSE SAFE_DIVIDE(o.opening_units + rc.received_units - rm.remaining_units, o.opening_units + rc.received_units) * 100 END AS stock_reduction_pct,
  CASE WHEN r.period_type = 'SEASON_PLAN' THEN SAFE_DIVIDE(r.plan_cogs, o.opening_value_rub + o.inbound_planned_value_rub) * 100
       ELSE SAFE_DIVIDE(r.cogs_released_since_opening, o.opening_value_rub + rc.received_value_rub) * 100 END AS inventory_cash_conversion_pct,
  CASE WHEN r.period_type = 'SEASON_PLAN' THEN SAFE_DIVIDE(r.plan_seller_cash, o.opening_value_rub + o.inbound_planned_value_rub) * 100
       ELSE SAFE_DIVIDE(r.seller_cash_since_opening, o.opening_value_rub + rc.received_value_rub) * 100 END AS seller_cash_to_opening_value_pct,
  rm.projected_residual_31_03_at_plan AS projected_residual_31_03_units_at_plan,
  GREATEST(rm.remaining_units - rm.units_per_day_30d * DATE_DIFF(DATE '2027-03-31', CURRENT_DATE(), DAY), 0) AS projected_residual_31_03_units_at_30d_rate,
  rm.units_per_day_30d AS current_units_per_day_30d,
  'Вклад = деньги продавца − COGS проданного; это НЕ чистая прибыль. Операционный результат = вклад − OPEX (пропорционально прошедшим дням месяца). Конверсия запаса в деньги % = COGS проданного с даты открытия / стоимость открытия по себестоимости (+ полученные партии).' AS semantics_note,
  r.ord AS sort_order,
  CURRENT_DATE() AS as_of
FROM rows_ r CROSS JOIN opening o CROSS JOIN received rc CROSS JOIN remaining rm
;

CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_BUNDLE_STATUS` AS
WITH bom AS (SELECT card_sku, component_sku, component_qty, component_count FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_BOM_CURRENT` WHERE is_bundle),
bundles AS (
  SELECT p.internal_sku AS bundle_sku, COALESCE(p.product_name_short, p.canonical_product_name) AS product_name, p.product_line,
    MAX(b.component_count) AS component_count, STRING_AGG(b.component_sku, ' + ' ORDER BY b.component_sku) AS components
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_PRODUCT_MASTER` p JOIN bom b ON b.card_sku = p.internal_sku
  WHERE p.is_bundle GROUP BY 1, 2, 3
),
inv AS (SELECT internal_sku, ff_total_units, sellable_units, inbound_units, inbound_eta, status_code AS component_status FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_INVENTORY_TRUTH`),
comp AS (
  SELECT b.card_sku AS bundle_sku,
    MIN(DIV(IFNULL(i.ff_total_units, 0), b.component_qty)) AS assemblable_now_ff,
    ARRAY_AGG(b.component_sku ORDER BY DIV(IFNULL(i.ff_total_units, 0), b.component_qty) LIMIT 1)[OFFSET(0)] AS limiting_component,
    MIN(IFNULL(i.ff_total_units, 0)) AS limiting_component_ff_units,
    LOGICAL_OR(IFNULL(i.ff_total_units, 0) < 5 * b.component_qty AND IFNULL(i.inbound_units, 0) > 0) AS waits_for_inbound,
    MAX(i.inbound_eta) AS inbound_eta
  FROM bom b LEFT JOIN inv i ON i.internal_sku = b.component_sku GROUP BY 1
),
wb AS (
  SELECT internal_sku AS bundle_sku, SUM(IF(row_type = 'AGGREGATE', quantity, 0)) AS wb_cards_live
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_STOCKS_T5_CURRENT` WHERE internal_sku IS NOT NULL GROUP BY 1
),
oz AS (
  SELECT m.internal_sku AS bundle_sku, SUM(o.available_stock_count) AS ozon_cards_live, SUM(o.transit_stock_count) AS ozon_cards_transit_api
  FROM (
    SELECT sku, warehouse_id, available_stock_count, transit_stock_count,
      ROW_NUMBER() OVER (PARTITION BY sku, warehouse_id ORDER BY snapshot_date DESC, extracted_at DESC) AS rn
    FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_STOCKS`
    WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_STOCKS`)) o
  JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP` m ON m.marketplace = 'OZON' AND m.marketplace_sku = o.sku
  WHERE o.rn = 1 GROUP BY 1
),
act AS (
  SELECT internal_sku AS bundle_sku,
    SUM(IF(d = DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY), cards_ordered, 0)) AS cards_ordered_yesterday,
    SUM(IF(d >= DATE_TRUNC(CURRENT_DATE(), MONTH) AND d < CURRENT_DATE(), cards_ordered, 0)) AS cards_ordered_mtd,
    SUM(IF(d >= DATE '2026-09-01' AND d < CURRENT_DATE(), cards_ordered, 0)) AS cards_ordered_season,
    SUM(IF(d >= DATE '2026-09-01' AND d < CURRENT_DATE(), cards_sold, 0)) AS cards_sold_season,
    SUM(IF(d >= DATE '2026-09-01' AND d < CURRENT_DATE(), units_ordered, 0)) AS units_released_season,
    SUM(IF(d >= DATE '2026-09-01' AND d < CURRENT_DATE(), contribution, 0)) AS contribution_season,
    SUM(IF(d > DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY) AND d < CURRENT_DATE(), cards_ordered, 0)) / 30.0 AS cards_per_day_30d,
    SUM(IF(d >= DATE_TRUNC(CURRENT_DATE(), MONTH) AND d < CURRENT_DATE() AND marketplace = 'WB', cards_ordered, 0)) AS cards_ordered_mtd_wb,
    SUM(IF(d >= DATE_TRUNC(CURRENT_DATE(), MONTH) AND d < CURRENT_DATE() AND marketplace = 'OZON', cards_ordered, 0)) AS cards_ordered_mtd_ozon
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_ACTUAL_DAILY` WHERE sales_mode = 'BUNDLE' GROUP BY 1
),
plan AS (
  SELECT internal_sku AS bundle_sku,
    SUM(IF(plan_date >= DATE_TRUNC(CURRENT_DATE(), MONTH) AND plan_date < CURRENT_DATE(), target_cards, 0)) AS plan_cards_mtd,
    SUM(IF(plan_date BETWEEN DATE_TRUNC(CURRENT_DATE(), MONTH) AND LAST_DAY(CURRENT_DATE()), target_cards, 0)) AS plan_cards_this_month,
    SUM(IF(plan_date BETWEEN CURRENT_DATE() AND DATE_ADD(CURRENT_DATE(), INTERVAL 44 DAY), target_cards, 0)) AS plan_cards_next_45d,
    SUM(IF(plan_date >= CURRENT_DATE(), target_cards, 0)) AS plan_cards_to_season_end,
    SUM(IF(plan_date >= CURRENT_DATE(), target_contribution, 0)) AS plan_contribution_to_season_end
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_PLAN_ACTIVE` WHERE sales_mode = 'BUNDLE' GROUP BY 1
),
bp AS (
  SELECT bundle_internal_sku AS bundle_sku,
    SUM(IF(CURRENT_DATE() BETWEEN week_start AND DATE_ADD(week_start, INTERVAL 6 DAY), qty_assemble, 0)) AS plan_assemble_this_week,
    SUM(IF(DATE_ADD(CURRENT_DATE(), INTERVAL 7 DAY) BETWEEN week_start AND DATE_ADD(week_start, INTERVAL 6 DAY), qty_assemble, 0)) AS plan_assemble_next_week,
    SUM(IF(week_start >= CURRENT_DATE(), qty_assemble, 0)) AS plan_assemble_remaining_8w,
    ARRAY_AGG(IF(week_start >= DATE_TRUNC(CURRENT_DATE(), WEEK(MONDAY)) AND qty_assemble > 0, STRUCT(week_start, qty_assemble), NULL) IGNORE NULLS ORDER BY week_start LIMIT 1)[SAFE_OFFSET(0)] AS next_assembly,
    ANY_VALUE(limiting_component) AS plan_limiting_component
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_BUNDLE_PLAN` bp
  JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.CT_PLAN_VERSION` v ON v.plan_version = bp.plan_version AND v.plan_status = 'ACTIVE'
  GROUP BY 1
),
j AS (
  SELECT b.bundle_sku, b.product_name, b.product_line, b.component_count, b.components,
    IFNULL(w.wb_cards_live, 0) AS wb_cards_live, IFNULL(o.ozon_cards_live, 0) AS ozon_cards_live, IFNULL(o.ozon_cards_transit_api, 0) AS ozon_cards_transit_api,
    IFNULL(w.wb_cards_live, 0) + IFNULL(o.ozon_cards_live, 0) AS marketplace_cards_live,
    c.assemblable_now_ff, c.limiting_component, c.limiting_component_ff_units, c.waits_for_inbound, c.inbound_eta,
    IFNULL(a.cards_ordered_yesterday, 0) AS cards_ordered_yesterday, IFNULL(a.cards_ordered_mtd, 0) AS cards_ordered_mtd,
    IFNULL(a.cards_ordered_mtd_wb, 0) AS cards_ordered_mtd_wb, IFNULL(a.cards_ordered_mtd_ozon, 0) AS cards_ordered_mtd_ozon,
    IFNULL(a.cards_ordered_season, 0) AS cards_ordered_season, IFNULL(a.cards_sold_season, 0) AS cards_sold_season,
    IFNULL(a.units_released_season, 0) AS units_released_season, IFNULL(a.contribution_season, 0) AS contribution_season,
    IFNULL(a.cards_per_day_30d, 0) AS cards_per_day_30d,
    IFNULL(p.plan_cards_mtd, 0) AS plan_cards_mtd, IFNULL(p.plan_cards_this_month, 0) AS plan_cards_this_month,
    IFNULL(p.plan_cards_next_45d, 0) AS plan_cards_next_45d, IFNULL(p.plan_cards_to_season_end, 0) AS plan_cards_to_season_end,
    IFNULL(p.plan_contribution_to_season_end, 0) AS plan_contribution_to_season_end,
    IFNULL(bp.plan_assemble_this_week, 0) AS plan_assemble_this_week, IFNULL(bp.plan_assemble_next_week, 0) AS plan_assemble_next_week,
    IFNULL(bp.plan_assemble_remaining_8w, 0) AS plan_assemble_remaining_8w, bp.plan_limiting_component,
    bp.next_assembly.week_start AS next_assembly_week_start, bp.next_assembly.qty_assemble AS next_assembly_qty
  FROM bundles b
  LEFT JOIN wb w ON w.bundle_sku = b.bundle_sku
  LEFT JOIN oz o ON o.bundle_sku = b.bundle_sku
  LEFT JOIN comp c ON c.bundle_sku = b.bundle_sku
  LEFT JOIN act a ON a.bundle_sku = b.bundle_sku
  LEFT JOIN plan p ON p.bundle_sku = b.bundle_sku
  LEFT JOIN bp ON bp.bundle_sku = b.bundle_sku
)
SELECT j.*,
  SAFE_DIVIDE(cards_ordered_mtd, plan_cards_mtd) * 100 AS mtd_attainment_pct,
  SAFE_DIVIDE(marketplace_cards_live, NULLIF(cards_per_day_30d, 0)) AS days_cover_marketplace_at_30d_rate,
  SAFE_DIVIDE(marketplace_cards_live, NULLIF(plan_cards_next_45d / 45.0, 0)) AS days_cover_marketplace_at_plan_rate,
  GREATEST(CAST(CEIL(plan_cards_next_45d) AS INT64) - marketplace_cards_live - ozon_cards_transit_api, 0) AS assembly_need_next_45d,
  CASE
    WHEN waits_for_inbound THEN 'BLOCKED_INBOUND'
    WHEN plan_cards_next_45d > 0 AND assemblable_now_ff = 0 AND marketplace_cards_live < plan_cards_next_45d THEN 'BLOCKED_COMPONENT'
    WHEN plan_cards_next_45d > 0 AND assemblable_now_ff < GREATEST(CAST(CEIL(plan_cards_next_45d) AS INT64) - marketplace_cards_live - ozon_cards_transit_api, 0) THEN 'COMPONENT_SHORT'
    WHEN plan_cards_next_45d > 0 AND marketplace_cards_live + ozon_cards_transit_api < plan_cards_next_45d * (10 / 45.0) THEN 'ASSEMBLE_NOW'
    WHEN plan_cards_next_45d > 0 AND marketplace_cards_live + ozon_cards_transit_api < plan_cards_next_45d * (20 / 45.0) THEN 'ASSEMBLE_SOON'
    WHEN plan_cards_this_month > 0 AND SAFE_DIVIDE(cards_ordered_mtd, plan_cards_mtd) < 0.7 AND plan_cards_mtd >= 5 THEN 'BELOW_PLAN'
    ELSE 'OK'
  END AS status_code,
  CASE
    WHEN waits_for_inbound THEN 'BLUE'
    WHEN plan_cards_next_45d > 0 AND assemblable_now_ff = 0 AND marketplace_cards_live < plan_cards_next_45d THEN 'RED'
    WHEN plan_cards_next_45d > 0 AND assemblable_now_ff < GREATEST(CAST(CEIL(plan_cards_next_45d) AS INT64) - marketplace_cards_live - ozon_cards_transit_api, 0) THEN 'YELLOW'
    WHEN plan_cards_next_45d > 0 AND marketplace_cards_live + ozon_cards_transit_api < plan_cards_next_45d * (10 / 45.0) THEN 'RED'
    WHEN plan_cards_next_45d > 0 AND marketplace_cards_live + ozon_cards_transit_api < plan_cards_next_45d * (20 / 45.0) THEN 'YELLOW'
    WHEN plan_cards_this_month > 0 AND SAFE_DIVIDE(cards_ordered_mtd, plan_cards_mtd) < 0.7 AND plan_cards_mtd >= 5 THEN 'YELLOW'
    ELSE 'GREEN'
  END AS status,
  CURRENT_DATE() AS as_of
FROM j
;

CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_SUPPLY_NEED` AS
-- Replenishment need per marketplace × component SKU: marketplace stock (+ in transit) vs the next-45-day plan (component units via BOM).
-- READ-ONLY recommendation; nothing here creates supplies.
WITH bom AS (SELECT card_sku, component_sku, component_qty FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_BOM_CURRENT`),
plan AS (
  SELECT p.marketplace, b.component_sku AS internal_sku,
    SUM(IF(p.plan_date BETWEEN CURRENT_DATE() AND DATE_ADD(CURRENT_DATE(), INTERVAL 44 DAY), p.target_cards * b.component_qty, 0)) AS plan_units_next_45d,
    SUM(IF(p.plan_date BETWEEN CURRENT_DATE() AND DATE_ADD(CURRENT_DATE(), INTERVAL 13 DAY), p.target_cards * b.component_qty, 0)) AS plan_units_next_14d,
    SAFE_DIVIDE(SUM(IF(p.plan_date BETWEEN CURRENT_DATE() AND DATE_ADD(CURRENT_DATE(), INTERVAL 44 DAY), p.target_contribution, 0)),
                NULLIF(SUM(IF(p.plan_date BETWEEN CURRENT_DATE() AND DATE_ADD(CURRENT_DATE(), INTERVAL 44 DAY), p.target_cards * b.component_qty, 0)), 0)) AS plan_contribution_per_unit
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_PLAN_ACTIVE` p JOIN bom b ON b.card_sku = p.internal_sku
  GROUP BY 1, 2
),
vel AS (
  SELECT marketplace, internal_sku, SUM(units_ordered) / 30.0 AS units_per_day_30d
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_PHYSICAL_DAILY` WHERE d > DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY) AND d < CURRENT_DATE() GROUP BY 1, 2
),
inv AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_INVENTORY_TRUTH`),
mp AS (
  SELECT 'WB' AS marketplace, internal_sku, product_name, wb_fbo_live_units AS marketplace_units, wb_in_transit_units AS in_transit_units, ff_total_units, ff_operational_snapshot, ff_pallet_snapshot, unit_cogs_rub, days_to_expiry, status_code AS inventory_status FROM inv
  UNION ALL
  SELECT 'OZON', internal_sku, product_name, ozon_fbo_units, ozon_transit_units, ff_total_units, ff_operational_snapshot, ff_pallet_snapshot, unit_cogs_rub, days_to_expiry, status_code FROM inv
),
j AS (
  SELECT m.marketplace, m.internal_sku, m.product_name, m.marketplace_units, m.in_transit_units, m.marketplace_units + m.in_transit_units AS available_units,
    m.ff_total_units, m.unit_cogs_rub, m.days_to_expiry, m.inventory_status,
    IFNULL(p.plan_units_next_45d, 0) AS plan_units_next_45d, IFNULL(p.plan_units_next_14d, 0) AS plan_units_next_14d,
    IFNULL(p.plan_units_next_45d, 0) / 45.0 AS plan_units_per_day, IFNULL(v.units_per_day_30d, 0) AS units_per_day_30d,
    IFNULL(p.plan_contribution_per_unit, 0) AS plan_contribution_per_unit
  FROM mp m LEFT JOIN plan p ON p.marketplace = m.marketplace AND p.internal_sku = m.internal_sku
  LEFT JOIN vel v ON v.marketplace = m.marketplace AND v.internal_sku = m.internal_sku
),
k AS (
  SELECT j.*,
    SAFE_DIVIDE(available_units, NULLIF(plan_units_per_day, 0)) AS cover_days_at_plan_rate,
    SAFE_DIVIDE(available_units, NULLIF(units_per_day_30d, 0)) AS cover_days_at_30d_rate,
    GREATEST(CAST(CEIL(plan_units_next_45d) AS INT64) - available_units, 0) AS gap_units_45d,
    LEAST(GREATEST(CAST(CEIL(plan_units_next_45d) AS INT64) - available_units, 0), GREATEST(ff_total_units - 20, 0)) AS recommended_ship_units
  FROM j
)
SELECT k.*,
  recommended_ship_units * plan_contribution_per_unit AS contribution_at_risk_rub,
  CASE
    WHEN plan_units_next_45d <= 0 THEN 'NO_PLAN'
    WHEN gap_units_45d = 0 THEN 'OK'
    WHEN ff_total_units <= 20 THEN 'FF_EMPTY'
    WHEN cover_days_at_plan_rate < 7 THEN 'SHIP_NOW'
    WHEN cover_days_at_plan_rate < 14 THEN 'SHIP_THIS_WEEK'
    WHEN cover_days_at_plan_rate < 21 THEN 'SHIP_NEXT_2W'
    ELSE 'PLAN_45D'
  END AS need_code,
  CASE
    WHEN plan_units_next_45d <= 0 OR gap_units_45d = 0 THEN 'GREEN'
    WHEN ff_total_units <= 20 THEN 'BLUE'
    WHEN cover_days_at_plan_rate < 7 THEN 'RED'
    WHEN cover_days_at_plan_rate < 21 THEN 'YELLOW'
    ELSE 'GREEN'
  END AS status,
  CURRENT_DATE() AS as_of
FROM k
;

-- ── Крем для рук: отдельный контроль жёсткого срока годности 31.12.2026 ─────────────
-- Одна строка. Цель: остаток на дату срока → 0. Учитывает продажи и соло, и через наборы.
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_HAND_CREAM_CONTROL` AS
WITH inv AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_INVENTORY_TRUTH` WHERE internal_sku = 'EVT-HC-HAND-300'),
ph AS (
  SELECT
    SUM(IF(d >= DATE_TRUNC(CURRENT_DATE(), MONTH), units_ordered, 0)) AS units_ordered_mtd,
    SUM(IF(d >= DATE_TRUNC(CURRENT_DATE(), MONTH), units_ordered_via_bundle, 0)) AS units_ordered_mtd_via_bundle,
    SUM(IF(d >= DATE '2026-09-09', units_ordered, 0)) AS units_ordered_season,
    SUM(IF(d > DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY), units_ordered, 0)) / 7.0 AS units_per_day_7d,
    SUM(IF(d > DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY), units_ordered, 0)) / 30.0 AS units_per_day_30d
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_PHYSICAL_DAILY` WHERE internal_sku = 'EVT-HC-HAND-300'
),
plan AS (
  SELECT
    SUM(IF(p.plan_date BETWEEN CURRENT_DATE() AND DATE '2026-12-31', p.target_cards * b.component_qty, 0)) AS plan_units_to_expiry,
    SUM(IF(p.plan_date BETWEEN DATE_TRUNC(CURRENT_DATE(), MONTH) AND LAST_DAY(CURRENT_DATE()), p.target_cards * b.component_qty, 0)) AS plan_units_this_month,
    SUM(IF(p.plan_date BETWEEN DATE_TRUNC(CURRENT_DATE(), MONTH) AND CURRENT_DATE(), p.target_cards * b.component_qty, 0)) AS plan_units_mtd
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_PLAN_ACTIVE` p
  JOIN `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_BOM_CURRENT` b ON b.card_sku = p.internal_sku AND b.component_sku = 'EVT-HC-HAND-300'
),
econ AS (
  SELECT
    SUM(IF(d >= DATE '2026-09-09', contribution, 0)) AS contribution_season_to_date,
    SUM(IF(d >= DATE '2026-09-09', seller_cash, 0)) AS seller_cash_season_to_date,
    SUM(IF(d >= DATE '2026-09-09', ad_spend, 0)) AS ad_spend_season_to_date,
    SUM(IF(d >= DATE '2026-09-09', revenue_seller_base, 0)) AS revenue_season_to_date
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_ACTUAL_DAILY` WHERE internal_sku = 'EVT-HC-HAND-300'
),
plan_econ AS (
  SELECT SUM(target_contribution) AS plan_contribution_solo_to_expiry, SUM(target_seller_cash) AS plan_seller_cash_solo_to_expiry
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_PLAN_ACTIVE` WHERE internal_sku = 'EVT-HC-HAND-300' AND plan_date BETWEEN CURRENT_DATE() AND DATE '2026-12-31'
)
SELECT
  inv.internal_sku, inv.product_name, inv.expiry_date, inv.days_to_expiry,
  inv.sellable_units AS current_stock_units, inv.ff_total_units, inv.wb_fbo_live_units, inv.ozon_fbo_units, inv.ozon_transit_units,
  inv.sellable_value_rub AS current_stock_value_rub,
  ph.units_ordered_mtd, ph.units_ordered_mtd_via_bundle, ph.units_ordered_season,
  SAFE_DIVIDE(inv.sellable_units, inv.days_to_expiry) AS required_units_per_day,
  ph.units_per_day_7d AS actual_units_per_day_7d, ph.units_per_day_30d AS actual_units_per_day_30d,
  plan.plan_units_to_expiry, plan.plan_units_this_month, plan.plan_units_mtd,
  SAFE_DIVIDE(plan.plan_units_to_expiry, inv.days_to_expiry) AS plan_units_per_day,
  GREATEST(inv.sellable_units - ph.units_per_day_30d * inv.days_to_expiry, 0) AS projected_residual_at_expiry_at_30d_rate,
  GREATEST(inv.sellable_units - ph.units_per_day_7d * inv.days_to_expiry, 0) AS projected_residual_at_expiry_at_7d_rate,
  GREATEST(inv.sellable_units - plan.plan_units_to_expiry, 0) AS projected_residual_at_expiry_at_plan,
  GREATEST(inv.sellable_units - ph.units_per_day_30d * inv.days_to_expiry, 0) * inv.unit_cogs_rub AS writeoff_risk_at_30d_rate_rub,
  econ.revenue_season_to_date, econ.ad_spend_season_to_date, econ.seller_cash_season_to_date, econ.contribution_season_to_date AS liquidation_contribution_season_to_date,
  plan_econ.plan_contribution_solo_to_expiry, plan_econ.plan_seller_cash_solo_to_expiry,
  CASE
    WHEN ph.units_per_day_7d >= SAFE_DIVIDE(inv.sellable_units, inv.days_to_expiry) * 0.9 THEN 'GREEN'
    WHEN ph.units_per_day_7d >= SAFE_DIVIDE(inv.sellable_units, inv.days_to_expiry) * 0.6 THEN 'YELLOW'
    ELSE 'RED'
  END AS status,
  'HC-B: WB 640 → 590 (окт) → 540 (ноя) → 490 (дек, пол 450); Ozon 973 → 895 → 850 → 799; реклама 6 % → 17 %; наборы руки+Cherry/Amber' AS program,
  CURRENT_DATE() AS as_of
FROM inv CROSS JOIN ph CROSS JOIN plan CROSS JOIN econ CROSS JOIN plan_econ;

CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_ATTENTION` AS
WITH inv AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_INVENTORY_TRUTH`),
-- 1. EXPIRY: units that will not sell before expiry at the 30-day run-rate
expiry AS (
  SELECT 'EXPIRY' AS alert_type, IF(days_to_expiry < 120, 'RED', 'YELLOW') AS severity, 'ALL' AS marketplace, internal_sku, product_name,
    CONCAT(product_name, ': срок годности ', FORMAT_DATE('%d.%m.%Y', expiry_date), ' (', CAST(days_to_expiry AS STRING), ' дн.), остаток ', CAST(sellable_units AS STRING), ' фл.') AS headline,
    CONCAT('Нужно ', CAST(ROUND(SAFE_DIVIDE(sellable_units, days_to_expiry), 1) AS STRING), ' фл./день, факт 30 дн. ', CAST(ROUND(units_per_day_30d, 1) AS STRING),
      ' фл./день → остаток на дату срока при текущем темпе ≈ ', CAST(ROUND(GREATEST(sellable_units - units_per_day_30d * days_to_expiry, 0)) AS STRING), ' фл. (', CAST(ROUND(GREATEST(sellable_units - units_per_day_30d * days_to_expiry, 0) * unit_cogs_rub / 1000) AS STRING), ' тыс ₽ по себестоимости)') AS detail,
    CAST(GREATEST(sellable_units - units_per_day_30d * days_to_expiry, 0) AS FLOAT64) AS metric_value, 0.0 AS threshold_value,
    CAST(-GREATEST(sellable_units - units_per_day_30d * days_to_expiry, 0) * unit_cogs_rub AS FLOAT64) AS financial_effect_rub,
    'V_CT_INVENTORY_TRUTH' AS source_view, 1 AS sort_rank
  FROM inv WHERE days_to_expiry < 270 AND sellable_units > 0
),
-- 2. STOCKOUT / LOW MARKETPLACE STOCK (component units on marketplaces vs 30d run-rate)
stockout AS (
  SELECT 'STOCKOUT' AS alert_type, IF(days_of_stock_marketplace_at_30d_rate < 10, 'RED', 'YELLOW') AS severity, 'WB+OZON' AS marketplace, internal_sku, product_name,
    CONCAT(product_name, ': на площадках ', CAST(marketplace_units AS STRING), ' фл. = ', CAST(ROUND(days_of_stock_marketplace_at_30d_rate) AS STRING), ' дн. при темпе ', CAST(ROUND(units_per_day_30d, 1) AS STRING), ' фл./день') AS headline,
    CONCAT('WB ', CAST(wb_fbo_live_units AS STRING), ' + Ozon ', CAST(ozon_fbo_units AS STRING), ' (в пути на Ozon ', CAST(ozon_transit_units AS STRING), ', на WB ', CAST(wb_in_transit_units AS STRING), '); на ФФ ', CAST(ff_total_units AS STRING), ' фл.; план на 45 дн. ', CAST(ROUND(plan_units_next_45d) AS STRING), ' фл.') AS detail,
    CAST(days_of_stock_marketplace_at_30d_rate AS FLOAT64) AS metric_value, 20.0 AS threshold_value,
    CAST(NULL AS FLOAT64) AS financial_effect_rub, 'V_CT_INVENTORY_TRUTH' AS source_view, 2 AS sort_rank
  FROM inv WHERE units_per_day_30d >= 1 AND days_of_stock_marketplace_at_30d_rate < 20 AND ff_total_units > 20
),
-- 3. SALES BELOW PLAN: per marketplace, MTD attainment < 70 % once the MTD target is material
pva AS (
  SELECT marketplace, month, MAX(day_of_month) AS dom,
    SUM(IF(is_past, target_cards, 0)) AS mtd_target, SUM(IF(is_past AND in_plan, IFNULL(actual_cards, 0), 0)) AS mtd_actual_in_plan, SUM(IF(is_past, IFNULL(actual_cards, 0), 0)) AS mtd_actual,
    SUM(target_cards) AS month_target
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_PLAN_VS_ACTUAL_DAILY`
  WHERE month = DATE_TRUNC(CURRENT_DATE(), MONTH) AND in_plan GROUP BY 1, 2
),
below_plan AS (
  SELECT 'BELOW_PLAN' AS alert_type, IF(SAFE_DIVIDE(mtd_actual, mtd_target) < 0.5, 'RED', 'YELLOW') AS severity, marketplace, 'PORTFOLIO' AS internal_sku, CONCAT('Портфель ', marketplace) AS product_name,
    CONCAT(marketplace, ': MTD ', CAST(mtd_actual AS STRING), ' поз. против плана ', CAST(ROUND(mtd_target) AS STRING), ' (', CAST(ROUND(SAFE_DIVIDE(mtd_actual, mtd_target) * 100) AS STRING), ' %)') AS headline,
    CONCAT('План месяца ', CAST(ROUND(month_target) AS STRING), ' проданных позиций; отставание ', CAST(ROUND(mtd_target - mtd_actual) AS STRING), ' поз. на ', FORMAT_DATE('%d.%m', DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY))) AS detail,
    CAST(SAFE_DIVIDE(mtd_actual, mtd_target) * 100 AS FLOAT64) AS metric_value, 70.0 AS threshold_value,
    CAST(NULL AS FLOAT64) AS financial_effect_rub, 'V_CT_PLAN_VS_ACTUAL_DAILY' AS source_view, 3 AS sort_rank
  FROM pva WHERE mtd_target >= 15 AND SAFE_DIVIDE(mtd_actual, mtd_target) < 0.7
),
-- 4. ADVERTISING: WB 7-day DRR above the economic ceiling (V_ADS_SKU_ECONOMIC_LIMITS, production semantics)
ads AS (
  SELECT 'DRR' AS alert_type, IF(economic_state = 'NEGATIVE_BEFORE_ADS' OR drr_headroom_pp < -0.05, 'RED', 'YELLOW') AS severity, 'WB' AS marketplace, internal_sku, product_name_short AS product_name,
    CONCAT(product_name_short, ': ДРР 7 дн. ', CAST(ROUND(actual_ad_drr * 100) AS STRING), ' % при потолке ', CAST(ROUND(IFNULL(max_ad_drr_breakeven, 0) * 100) AS STRING), ' % (', CAST(ROUND(ad_spend_rub) AS STRING), ' ₽ за 7 дн.)') AS headline,
    CONCAT('Состояние: ', economic_state, '; перерасход ≈ ', CAST(ROUND(-ad_spend_headroom_rub) AS STRING), ' ₽ за 7 дн.; выкупы ', CAST(buyouts_qty AS STRING), ' шт. на ', CAST(ROUND(buyouts_rub) AS STRING), ' ₽') AS detail,
    CAST(actual_ad_drr * 100 AS FLOAT64) AS metric_value, CAST(IFNULL(max_ad_drr_breakeven, 0) * 100 AS FLOAT64) AS threshold_value,
    CAST(ad_spend_headroom_rub AS FLOAT64) AS financial_effect_rub, 'V_ADS_SKU_ECONOMIC_LIMITS' AS source_view, 4 AS sort_rank
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_ADS_SKU_ECONOMIC_LIMITS`
  WHERE window_days = 7 AND ad_spend_rub >= 300 AND economic_state IN ('ABOVE_BREAKEVEN', 'NEGATIVE_BEFORE_ADS')
),
-- 5. OVERSTOCK: more than 365 days of plan-rate cover
overstock AS (
  SELECT 'OVERSTOCK' AS alert_type, 'YELLOW' AS severity, 'ALL' AS marketplace, internal_sku, product_name,
    CONCAT(product_name, ': запас ', CAST(sellable_units AS STRING), ' фл. = ', CAST(ROUND(days_of_stock_total_at_plan_rate) AS STRING), ' дн. плана; остаток на 31.03.2027 по плану ≈ ', CAST(ROUND(projected_residual_31_03_2027_at_plan) AS STRING), ' фл.') AS headline,
    CONCAT('Заморожено ≈ ', CAST(ROUND(projected_residual_31_03_2027_at_plan * unit_cogs_rub / 1000) AS STRING), ' тыс ₽ по себестоимости; срок годности ', IFNULL(FORMAT_DATE('%d.%m.%Y', expiry_date), '—'), '; рычаги: наборы, Ozon, внешний канал') AS detail,
    CAST(days_of_stock_total_at_plan_rate AS FLOAT64) AS metric_value, 365.0 AS threshold_value,
    CAST(projected_residual_31_03_2027_at_plan * unit_cogs_rub AS FLOAT64) AS financial_effect_rub, 'V_CT_INVENTORY_TRUTH' AS source_view, 6 AS sort_rank
  FROM inv WHERE days_of_stock_total_at_plan_rate > 365 AND sellable_units > 100
),
-- 6. BUNDLES: blocked or needing assembly
bundles AS (
  SELECT 'BUNDLE' AS alert_type, status AS severity, 'FF' AS marketplace, bundle_sku AS internal_sku, product_name,
    CONCAT(product_name, ': ', CASE status_code
      WHEN 'BLOCKED_INBOUND' THEN CONCAT('заблокирован — ждём партию ', IFNULL(limiting_component, ''), IF(inbound_eta IS NULL, '', CONCAT(' (ETA ', FORMAT_DATE('%d.%m', inbound_eta), ')')))
      WHEN 'BLOCKED_COMPONENT' THEN CONCAT('нет компонента ', IFNULL(limiting_component, ''), ' на ФФ')
      WHEN 'COMPONENT_SHORT' THEN CONCAT('компонента ', IFNULL(limiting_component, ''), ' хватает на ', CAST(assemblable_now_ff AS STRING), ' наборов из ', CAST(assembly_need_next_45d AS STRING))
      WHEN 'ASSEMBLE_NOW' THEN CONCAT('на площадках ', CAST(marketplace_cards_live AS STRING), ' шт. при плане ', CAST(ROUND(plan_cards_next_45d) AS STRING), ' на 45 дн. — собрать ', CAST(assembly_need_next_45d AS STRING))
      WHEN 'ASSEMBLE_SOON' THEN CONCAT('на площадках ', CAST(marketplace_cards_live AS STRING), ' шт. — собрать ', CAST(assembly_need_next_45d AS STRING), ' до ', FORMAT_DATE('%d.%m', IFNULL(next_assembly_week_start, DATE_ADD(CURRENT_DATE(), INTERVAL 7 DAY))))
      ELSE 'ниже плана' END) AS headline,
    CONCAT('WB ', CAST(wb_cards_live AS STRING), ' / Ozon ', CAST(ozon_cards_live AS STRING), ' (в пути ', CAST(ozon_cards_transit_api AS STRING), '); MTD ', CAST(cards_ordered_mtd AS STRING), ' из ', CAST(ROUND(plan_cards_mtd) AS STRING), '; ближайшая сборка ', IFNULL(FORMAT_DATE('%d.%m', next_assembly_week_start), '—'), ' × ', CAST(IFNULL(next_assembly_qty, 0) AS STRING)) AS detail,
    CAST(assembly_need_next_45d AS FLOAT64) AS metric_value, 0.0 AS threshold_value,
    CAST(NULL AS FLOAT64) AS financial_effect_rub, 'V_CT_BUNDLE_STATUS' AS source_view, 5 AS sort_rank
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_BUNDLE_STATUS` WHERE status_code != 'OK'
),
-- 7. DATA FRESHNESS
stale AS (
  SELECT 'DATA_STALE' AS alert_type, IF(status = 'ERROR', 'RED', 'YELLOW') AS severity, domain AS marketplace, 'DATA' AS internal_sku, domain_ru AS product_name,
    CONCAT('Данные устарели: ', domain_ru, ' — последняя дата ', IFNULL(FORMAT_DATE('%d.%m.%Y', data_as_of), 'нет данных'), ' (', CAST(IFNULL(age_days, 0) AS STRING), ' дн.)') AS headline,
    CONCAT('Источник ', source, '; допустимо ', CAST(sla_days AS STRING), ' дн. Показатели по этому домену на Owner Home считать неактуальными') AS detail,
    CAST(age_days AS FLOAT64) AS metric_value, CAST(sla_days AS FLOAT64) AS threshold_value,
    CAST(NULL AS FLOAT64) AS financial_effect_rub, 'V_CT_FRESHNESS' AS source_view, 0 AS sort_rank
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_FRESHNESS` WHERE status != 'OK'
),
u AS (
  SELECT * FROM stale UNION ALL SELECT * FROM expiry UNION ALL SELECT * FROM stockout UNION ALL SELECT * FROM below_plan
  UNION ALL SELECT * FROM ads UNION ALL SELECT * FROM bundles UNION ALL SELECT * FROM overstock
)
SELECT CONCAT(alert_type, '|', marketplace, '|', internal_sku) AS alert_id, alert_type, severity,
  CASE severity WHEN 'RED' THEN 1 WHEN 'YELLOW' THEN 2 WHEN 'BLUE' THEN 3 ELSE 4 END AS severity_rank,
  marketplace, internal_sku, product_name, headline, detail, metric_value, threshold_value, financial_effect_rub, source_view, sort_rank,
  CURRENT_DATE() AS as_of
FROM u
;

CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_ACTION_CANDIDATES` AS
-- Rule-based action candidates for the Owner Action Queue (Phase 1 rule set).
-- Each row carries a deterministic dedup_key = action_type|marketplace|sku|reason_code; sp_ct_generate_actions() merges by that key.
WITH pv AS (SELECT plan_version FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_PLAN_VERSION` WHERE plan_status = 'ACTIVE'),
-- R1 STOCKOUT vs plan → REPLENISH_WB / REPLENISH_OZON (one portfolio action per marketplace, SKU detail in reason_text)
sn AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_SUPPLY_NEED` WHERE recommended_ship_units > 0 AND need_code IN ('SHIP_NOW', 'SHIP_THIS_WEEK', 'SHIP_NEXT_2W')),
r1 AS (
  SELECT
    IF(marketplace = 'WB', 'REPLENISH_WB', 'REPLENISH_OZON') AS action_type, marketplace, 'PORTFOLIO' AS sku, CAST(NULL AS STRING) AS bundle_id,
    SUM(recommended_ship_units) AS qty,
    SUM(contribution_at_risk_rub) AS financial_effect_rub,
    'COVER_BELOW_21D_PLAN' AS reason_code,
    CONCAT('Покрытие на ', marketplace, ' ниже 21 дня плана по ', CAST(COUNT(*) AS STRING), ' SKU: ',
      STRING_AGG(CONCAT(product_name, ' ', CAST(recommended_ship_units AS STRING), ' фл. (', CAST(ROUND(cover_days_at_plan_rate) AS STRING), ' дн.)'), '; ' ORDER BY cover_days_at_plan_rate),
      IF(marketplace = 'WB', '. Лоты ≤ 25 кг через ПВЗ; детали — V_CT_SUPPLY_NEED', '. Поставка FBO по кластерам; детали — V_CT_SUPPLY_NEED')) AS reason_text,
    'V_CT_SUPPLY_NEED.cover_days_at_plan_rate' AS source_metric,
    CASE WHEN MIN(cover_days_at_plan_rate) < 7 THEN 'P0 TODAY' WHEN MIN(cover_days_at_plan_rate) < 14 THEN 'P1 THIS WEEK' ELSE 'P2 NEXT 2 WEEKS' END AS priority,
    CASE WHEN MIN(cover_days_at_plan_rate) < 7 THEN DATE_ADD(CURRENT_DATE(), INTERVAL 1 DAY) WHEN MIN(cover_days_at_plan_rate) < 14 THEN DATE_ADD(CURRENT_DATE(), INTERVAL 5 DAY) ELSE DATE_ADD(CURRENT_DATE(), INTERVAL 12 DAY) END AS deadline
  FROM sn GROUP BY marketplace
),
-- R2 SALES BELOW PLAN → INVESTIGATE (per marketplace, once ≥ 3 plan days elapsed and MTD attainment < 70 %)
pva AS (
  SELECT marketplace, month, COUNT(DISTINCT IF(is_past, d, NULL)) AS plan_days_elapsed,
    SUM(IF(is_past, target_cards, 0)) AS mtd_target, SUM(IF(is_past, IFNULL(actual_cards, 0), 0)) AS mtd_actual, SUM(target_cards) AS month_target
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_PLAN_VS_ACTUAL_DAILY` WHERE month = DATE_TRUNC(CURRENT_DATE(), MONTH) AND in_plan GROUP BY 1, 2
),
gaps AS (
  SELECT marketplace, STRING_AGG(CONCAT(product_name, ' −', CAST(ROUND(gap) AS STRING)), ', ' ORDER BY gap DESC LIMIT 3) AS gap_text FROM (
    SELECT marketplace, product_name, SUM(IF(is_past, target_cards, 0)) - SUM(IF(is_past, IFNULL(actual_cards, 0), 0)) AS gap
    FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_PLAN_VS_ACTUAL_DAILY` WHERE month = DATE_TRUNC(CURRENT_DATE(), MONTH) AND in_plan GROUP BY 1, 2
  ) WHERE gap > 0 GROUP BY 1
),
r2 AS (
  SELECT 'INVESTIGATE' AS action_type, p.marketplace, 'PORTFOLIO' AS sku, CAST(NULL AS STRING) AS bundle_id,
    CAST(ROUND(p.mtd_target - p.mtd_actual) AS INT64) AS qty,
    CAST(NULL AS FLOAT64) AS financial_effect_rub,
    CONCAT('BELOW_PLAN_', FORMAT_DATE('%Y%m', p.month)) AS reason_code,
    CONCAT(p.marketplace, ': ', CAST(p.mtd_actual AS STRING), ' поз. против плана ', CAST(ROUND(p.mtd_target) AS STRING), ' (', CAST(ROUND(SAFE_DIVIDE(p.mtd_actual, p.mtd_target) * 100) AS STRING), ' %) за ', CAST(p.plan_days_elapsed AS STRING), ' дн. плана. Наибольшее отставание: ',
      IFNULL(g.gap_text, '—'),
      '. Проверить наличие на полке, цену vs автоскидки, показы рекламы, позиции в выдаче') AS reason_text,
    'V_CT_PLAN_VS_ACTUAL_DAILY.mtd_attainment_pct' AS source_metric,
    IF(SAFE_DIVIDE(p.mtd_actual, p.mtd_target) < 0.5, 'P0 TODAY', 'P1 THIS WEEK') AS priority,
    DATE_ADD(CURRENT_DATE(), INTERVAL 3 DAY) AS deadline
  FROM pva p LEFT JOIN gaps g ON g.marketplace = p.marketplace WHERE p.plan_days_elapsed >= 3 AND p.mtd_target >= 15 AND SAFE_DIVIDE(p.mtd_actual, p.mtd_target) < 0.7
),
-- R3 EXPIRY → LIQUIDATION (per SKU; key matches the seeded hand-cream action so the seed row is refreshed, not duplicated)
r3 AS (
  SELECT 'LIQUIDATION' AS action_type, 'WB+OZON' AS marketplace, internal_sku AS sku, CAST(NULL AS STRING) AS bundle_id,
    sellable_units AS qty,
    CAST(-GREATEST(sellable_units - units_per_day_30d * days_to_expiry, 0) * unit_cogs_rub AS FLOAT64) AS financial_effect_rub,
    CONCAT('EXPIRY_HARD_', FORMAT_DATE('%Y-%m-%d', expiry_date)) AS reason_code,
    CONCAT(product_name, ': ', CAST(sellable_units AS STRING), ' фл. до ', FORMAT_DATE('%d.%m.%Y', expiry_date), ' (', CAST(days_to_expiry AS STRING), ' дн.). Нужно ', CAST(ROUND(SAFE_DIVIDE(sellable_units, days_to_expiry), 1) AS STRING), ' фл./день, факт 30 дн. ', CAST(ROUND(units_per_day_30d, 1) AS STRING), ' фл./день; остаток на дату срока при текущем темпе ≈ ', CAST(ROUND(GREATEST(sellable_units - units_per_day_30d * days_to_expiry, 0)) AS STRING), ' фл. Ликвидация по программе (цена, реклама, наборы, внешний канал)') AS reason_text,
    'V_CT_INVENTORY_TRUTH.days_to_expiry' AS source_metric,
    IF(days_to_expiry < 120, 'P0 TODAY', 'P1 THIS WEEK') AS priority,
    DATE_ADD(CURRENT_DATE(), INTERVAL 2 DAY) AS deadline
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_INVENTORY_TRUTH`
  WHERE days_to_expiry < 150 AND GREATEST(sellable_units - units_per_day_30d * days_to_expiry, 0) > 50
),
-- R4 ADVERTISING → ADS_REVIEW per WB SKU with 7-day DRR above the economic ceiling (production V_ADS_SKU_ECONOMIC_LIMITS)
r4 AS (
  SELECT 'ADS_REVIEW' AS action_type, 'WB' AS marketplace, internal_sku AS sku, CAST(NULL AS STRING) AS bundle_id,
    CAST(NULL AS INT64) AS qty,
    CAST(ad_spend_headroom_rub AS FLOAT64) AS financial_effect_rub,
    'DRR_ABOVE_CEILING_7D' AS reason_code,
    CONCAT(product_name_short, ': ДРР 7 дн. ', CAST(ROUND(actual_ad_drr * 100) AS STRING), ' % при потолке ', CAST(ROUND(IFNULL(max_ad_drr_breakeven, 0) * 100) AS STRING), ' % (', economic_state, '); расход ', CAST(ROUND(ad_spend_rub) AS STRING), ' ₽ за 7 дн., перерасход ≈ ', CAST(ROUND(-ad_spend_headroom_rub) AS STRING), ' ₽. Снизить ставки / перевести в класс по плану рекламы') AS reason_text,
    'V_ADS_SKU_ECONOMIC_LIMITS.drr_headroom_pp(7d)' AS source_metric,
    IF(economic_state = 'NEGATIVE_BEFORE_ADS' OR ad_spend_headroom_rub < -1000, 'P1 THIS WEEK', 'P2 NEXT 2 WEEKS') AS priority,
    DATE_ADD(CURRENT_DATE(), INTERVAL 2 DAY) AS deadline
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_ADS_SKU_ECONOMIC_LIMITS`
  WHERE window_days = 7 AND ad_spend_rub >= 300 AND economic_state IN ('ABOVE_BREAKEVEN', 'NEGATIVE_BEFORE_ADS')
),
-- R5 BUNDLES → ASSEMBLE_BUNDLES (one action per assembly week from CT_BUNDLE_PLAN + urgent shortfalls)
bs AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_BUNDLE_STATUS`),
r5 AS (
  SELECT 'ASSEMBLE_BUNDLES' AS action_type, 'FF' AS marketplace, 'BUNDLES' AS sku, CAST(NULL AS STRING) AS bundle_id,
    SUM(GREATEST(IFNULL(next_assembly_qty, 0), IF(status_code IN ('ASSEMBLE_NOW', 'COMPONENT_SHORT'), assembly_need_next_45d, 0))) AS qty,
    CAST(NULL AS FLOAT64) AS financial_effect_rub,
    CONCAT('WEEK_', FORMAT_DATE('%Y-%m-%d', MIN(next_assembly_week_start))) AS reason_code,
    CONCAT('Сборка наборов на неделю с ', FORMAT_DATE('%d.%m', MIN(next_assembly_week_start)), ': ',
      STRING_AGG(IF(GREATEST(IFNULL(next_assembly_qty, 0), IF(status_code IN ('ASSEMBLE_NOW', 'COMPONENT_SHORT'), assembly_need_next_45d, 0)) > 0,
        CONCAT(product_name, ' × ', CAST(GREATEST(IFNULL(next_assembly_qty, 0), IF(status_code IN ('ASSEMBLE_NOW', 'COMPONENT_SHORT'), assembly_need_next_45d, 0)) AS STRING),
               IF(status_code IN ('ASSEMBLE_NOW', 'COMPONENT_SHORT'), ' (срочно)', '')), NULL), '; ' ORDER BY GREATEST(IFNULL(next_assembly_qty, 0), assembly_need_next_45d) DESC),
      '. Лимитирующий компонент: ', IFNULL(ANY_VALUE(plan_limiting_component), '—'), '. Заявка на ФФ до пятницы') AS reason_text,
    'CT_BUNDLE_PLAN + V_CT_BUNDLE_STATUS' AS source_metric,
    IF(MIN(next_assembly_week_start) <= DATE_ADD(CURRENT_DATE(), INTERVAL 7 DAY) OR LOGICAL_OR(status_code = 'ASSEMBLE_NOW'), 'P1 THIS WEEK', 'P2 NEXT 2 WEEKS') AS priority,
    DATE_ADD(MIN(next_assembly_week_start), INTERVAL 4 DAY) AS deadline
  FROM bs WHERE next_assembly_week_start IS NOT NULL OR status_code IN ('ASSEMBLE_NOW', 'COMPONENT_SHORT')
  HAVING qty > 0
),
-- R6 OVERSTOCK → ACCELERATE per SKU (> 365 days of plan cover and material residual on 31.03.2027)
r6 AS (
  SELECT 'ACCELERATE' AS action_type, 'ALL' AS marketplace, internal_sku AS sku, CAST(NULL AS STRING) AS bundle_id,
    CAST(ROUND(projected_residual_31_03_2027_at_plan) AS INT64) AS qty,
    CAST(projected_residual_31_03_2027_at_plan * unit_cogs_rub AS FLOAT64) AS financial_effect_rub,
    'OVERSTOCK_365D' AS reason_code,
    CONCAT(product_name, ': запас ', CAST(sellable_units AS STRING), ' фл. = ', CAST(ROUND(days_of_stock_total_at_plan_rate) AS STRING), ' дн. плана; остаток на 31.03.2027 по плану ≈ ', CAST(ROUND(projected_residual_31_03_2027_at_plan) AS STRING), ' фл. (', CAST(ROUND(projected_residual_31_03_2027_at_plan * unit_cogs_rub / 1000) AS STRING), ' тыс ₽). Ускорить: наборы, Ozon, внешний канал; срок годности ', IFNULL(FORMAT_DATE('%d.%m.%Y', expiry_date), '—')) AS reason_text,
    'V_CT_INVENTORY_TRUTH.days_of_stock_total_at_plan_rate' AS source_metric,
    IF(projected_residual_31_03_2027_at_plan >= 2000, 'P3 THIS MONTH', 'P4 WATCH') AS priority,
    LAST_DAY(CURRENT_DATE()) AS deadline
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_INVENTORY_TRUTH`
  WHERE days_of_stock_total_at_plan_rate > 365 AND projected_residual_31_03_2027_at_plan > 500
),
-- R7 DATA FRESHNESS → DATA_FIX per stale domain
r7 AS (
  SELECT 'DATA_FIX' AS action_type, domain AS marketplace, 'DATA' AS sku, CAST(NULL AS STRING) AS bundle_id,
    CAST(NULL AS INT64) AS qty, CAST(NULL AS FLOAT64) AS financial_effect_rub,
    'STALE_SOURCE' AS reason_code,
    CONCAT('Данные устарели: ', domain_ru, ' — последняя дата ', IFNULL(FORMAT_DATE('%d.%m.%Y', data_as_of), 'нет'), ' (', CAST(IFNULL(age_days, 0) AS STRING), ' дн., допустимо ', CAST(sla_days AS STRING), '). Проверить загрузчик ', source) AS reason_text,
    'V_CT_FRESHNESS.age_days' AS source_metric,
    'P1 THIS WEEK' AS priority, DATE_ADD(CURRENT_DATE(), INTERVAL 1 DAY) AS deadline
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_FRESHNESS` WHERE status != 'OK'
),
u AS (
  SELECT * FROM r1 UNION ALL SELECT * FROM r2 UNION ALL SELECT * FROM r3 UNION ALL SELECT * FROM r4
  UNION ALL SELECT * FROM r5 UNION ALL SELECT * FROM r6 UNION ALL SELECT * FROM r7
)
SELECT CONCAT(action_type, '|', marketplace, '|', sku, '|', reason_code) AS dedup_key,
  action_type, marketplace, sku, bundle_id, qty, financial_effect_rub, reason_code, reason_text, source_metric, priority, deadline,
  CURRENT_DATE() AS action_date, (SELECT plan_version FROM pv) AS plan_version, CURRENT_TIMESTAMP() AS generated_at
FROM u
;

-- ── Очередь действий владельца (представление для дашборда) ─────────────────────────
-- Phase 1.1: читает CT_OWNER_ACTION_QUEUE, добавляет человекочитаемые поля для владельца:
--   lane        — DECISION (только владелец решает) | EXECUTION (исполняет ФФ / кабинет) | WATCH
--   what_to_do  — короткий императив («Снять с паллет 1 330 фл.»), без технических кодов
--   executor_ru — куда / кому (Фулфилмент Usend, WB · поставка через ПВЗ, Кабинет рекламы WB …)
--   why_short   — причина одной фразой; полный reason_text остаётся для drill-down
--   deadline_ru — «сегодня», «до 12.09», «просрочено 3 дн.»
--   effect_ru   — «+500 тыс ₽» / «−94 тыс ₽»
--   Phase 1.2: why_owner (одна фраза без перечня SKU), sku_breakdown (перечень для drill-down),
--   effect_kind_ru / effect_line («Потенциальная выручка: +500 тыс ₽», «Риск списания: −1.8 млн ₽»,
--   «Перерасход рекламы: −94 тыс ₽», «Потенциальный cash effect: +451 тыс ₽», «Стоимость решения: −87 тыс ₽»);
--   what_to_do больше не обрезается до 110 символов — на дашборде перенос строк вместо «…».
--   done_url / progress_url / cancel_url — ссылки на owner web-app (Apps Script), URL берётся из
--   evetis_ref.CT_CONFIG (action_webapp_url); пока ключ не заполнен — NULL, ссылки не показываются.
-- Статусы меняются ТОЛЬКО через evetis_ref.sp_ct_action_update() (web-app вызывает её же).
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_ACTION_QUEUE` AS
WITH cfg AS (
  SELECT MAX(IF(config_key = 'action_webapp_url', NULLIF(TRIM(config_value), ''), NULL)) AS webapp_url
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_CONFIG`
),
q AS (
  SELECT q.*,
    CASE priority WHEN 'P0 TODAY' THEN 0 WHEN 'P1 THIS WEEK' THEN 1 WHEN 'P2 NEXT 2 WEEKS' THEN 2 WHEN 'P3 THIS MONTH' THEN 3 ELSE 4 END AS priority_rank,
    CASE status WHEN 'IN_PROGRESS' THEN 0 WHEN 'OPEN' THEN 1 WHEN 'DONE' THEN 2 WHEN 'CANCELLED' THEN 3 ELSE 4 END AS status_rank,
    status IN ('OPEN', 'IN_PROGRESS') AS is_open,
    DATE_DIFF(deadline, CURRENT_DATE(), DAY) AS days_to_deadline,
    status IN ('OPEN', 'IN_PROGRESS') AND deadline < CURRENT_DATE() AS is_overdue,
    COALESCE(p.product_name_short, p.canonical_product_name, q.sku) AS sku_name,
    CASE action_type
      WHEN 'PULL_FROM_PALLETS' THEN 'Перекладка с паллет' WHEN 'REPLENISH_WB' THEN 'Отгрузка WB' WHEN 'REPLENISH_OZON' THEN 'Отгрузка Ozon'
      WHEN 'ASSEMBLE_BUNDLES' THEN 'Сборка наборов' WHEN 'LIQUIDATION' THEN 'Ликвидация' WHEN 'ADS_REVIEW' THEN 'Реклама'
      WHEN 'PRICE_ACTION' THEN 'Цены' WHEN 'DECISION' THEN 'Решение' WHEN 'FF_REQUEST' THEN 'Заявка на ФФ' WHEN 'INVESTIGATE' THEN 'Разобраться'
      WHEN 'ACCELERATE' THEN 'Ускорить сбыт' WHEN 'DATA_FIX' THEN 'Данные' WHEN 'WATCH' THEN 'Наблюдать' ELSE action_type END AS action_type_ru,
    CASE priority WHEN 'P0 TODAY' THEN 'RED' WHEN 'P1 THIS WEEK' THEN 'YELLOW' WHEN 'P2 NEXT 2 WEEKS' THEN 'YELLOW' WHEN 'P3 THIS MONTH' THEN 'BLUE' ELSE 'GREEN' END AS color,
    -- Дорожка: решение владельца против рутинного исполнения
    CASE
      WHEN action_type IN ('DECISION', 'LIQUIDATION', 'ACCELERATE', 'INVESTIGATE') THEN 'DECISION'
      WHEN action_type IN ('PULL_FROM_PALLETS', 'REPLENISH_WB', 'REPLENISH_OZON', 'ASSEMBLE_BUNDLES', 'FF_REQUEST', 'ADS_REVIEW', 'PRICE_ACTION', 'DATA_FIX') THEN 'EXECUTION'
      ELSE 'WATCH'
    END AS lane,
    -- Куда / кому
    CASE
      WHEN action_type IN ('PULL_FROM_PALLETS', 'ASSEMBLE_BUNDLES') THEN 'Фулфилмент Usend'
      WHEN action_type = 'FF_REQUEST' AND marketplace = 'FF' THEN 'Фулфилмент Usend (письменно)'
      WHEN action_type = 'FF_REQUEST' THEN CONCAT('Поддержка ', marketplace)
      WHEN action_type = 'REPLENISH_WB' THEN 'WB · поставка через ПВЗ (лот ≤ 25 кг)'
      WHEN action_type = 'REPLENISH_OZON' THEN 'Ozon · заявка FBO'
      WHEN action_type = 'ADS_REVIEW' THEN CONCAT('Кабинет рекламы ', IFNULL(NULLIF(marketplace, 'ALL'), 'WB и Ozon'))
      WHEN action_type = 'PRICE_ACTION' THEN CONCAT('Цены ', IFNULL(NULLIF(marketplace, 'ALL'), 'WB и Ozon'))
      WHEN marketplace = 'CHINA' THEN 'Поставщик (Китай)'
      WHEN marketplace = 'EXTERNAL' THEN 'Внешний канал (опт / офлайн)'
      WHEN marketplace = 'FF' THEN 'Фулфилмент Usend'
      ELSE 'Владелец'
    END AS executor_ru,
    -- Короткая формулировка: до первого « — » в тексте правила (Phase 1.2: без обрезки)
    TRIM(SPLIT(reason_text, ' — ')[SAFE_OFFSET(0)]) AS reason_head,
    TRIM(ARRAY_TO_STRING(ARRAY(SELECT x FROM UNNEST(SPLIT(reason_text, ' — ')) x WITH OFFSET o WHERE o > 0 ORDER BY o), ' — ')) AS reason_tail
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_OWNER_ACTION_QUEUE` q
  LEFT JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_PRODUCT_MASTER` p ON p.internal_sku = q.sku
),
q2 AS (
  SELECT q.*,
    -- Шаблон применяется к строкам правил и к seed-строкам с длинной формулировкой (> 110 симв.);
    -- короткая авторская формулировка владельца сохраняется как есть.
    (generated_by = 'RULE_ENGINE' OR LENGTH(reason_head) > 110)
      AND action_type IN ('PULL_FROM_PALLETS', 'REPLENISH_WB', 'REPLENISH_OZON', 'ASSEMBLE_BUNDLES', 'ADS_REVIEW', 'ACCELERATE')
      AND (qty > 0 OR action_type IN ('ADS_REVIEW', 'ACCELERATE'))
      AND (action_type NOT IN ('ADS_REVIEW', 'ACCELERATE') OR sku IS NULL OR sku NOT IN ('PORTFOLIO', 'BUNDLES', 'FF', 'DATA', 'BATCH-05')) AS use_template
  FROM q
),
q3 AS (
  SELECT q2.*,
    CASE
      WHEN use_template AND action_type = 'PULL_FROM_PALLETS' THEN CONCAT('Снять с паллет ', REPLACE(FORMAT('%\'d', CAST(qty AS INT64)), ',', ' '), ' фл.')
      WHEN use_template AND action_type = 'REPLENISH_WB' THEN CONCAT('Отгрузить на WB ', REPLACE(FORMAT('%\'d', CAST(qty AS INT64)), ',', ' '), ' фл.', IF(sku NOT IN ('PORTFOLIO', 'BUNDLES') AND sku IS NOT NULL, CONCAT(' · ', sku_name), ''))
      WHEN use_template AND action_type = 'REPLENISH_OZON' THEN CONCAT('Отгрузить на Ozon ', REPLACE(FORMAT('%\'d', CAST(qty AS INT64)), ',', ' '), ' фл.', IF(sku NOT IN ('PORTFOLIO', 'BUNDLES') AND sku IS NOT NULL, CONCAT(' · ', sku_name), ''))
      WHEN use_template AND action_type = 'ASSEMBLE_BUNDLES' THEN CONCAT('Собрать ', REPLACE(FORMAT('%\'d', CAST(qty AS INT64)), ',', ' '), ' наборов')
      WHEN use_template AND action_type = 'ADS_REVIEW' THEN CONCAT('Снизить ставки / пересмотреть рекламу: ', sku_name)
      WHEN use_template AND action_type = 'ACCELERATE' THEN CONCAT('Ускорить сбыт: ', sku_name, IF(qty > 0, CONCAT(' (остаток к 31.03 ≈ ', REPLACE(FORMAT('%\'d', CAST(qty AS INT64)), ',', ' '), ' фл.)'), ''))
      -- Phase 1.2: авторская формулировка владельца НЕ обрезается (перенос строк на дашборде вместо «…»)
      ELSE reason_head
    END AS what_to_do,
    CASE
      WHEN use_template THEN reason_text
      WHEN reason_tail IS NULL OR reason_tail = '' THEN reason_head
      ELSE reason_tail
    END AS why_full,
    -- Phase 1.2: «Зачем» одной фразой без перечня SKU (перечень уходит в sku_breakdown / drill-down)
    CASE
      WHEN use_template AND action_type = 'PULL_FROM_PALLETS' THEN 'Оперативная полка на фулфилменте пуста — без флаконов не будет отгрузок WB/Ozon и сборки наборов недели'
      WHEN use_template AND action_type = 'REPLENISH_WB' AND reason_code LIKE 'WB_STOCKOUT%' THEN 'На WB запаса меньше недели по плану — без поставки продажи остановятся'
      WHEN use_template AND action_type = 'REPLENISH_OZON' AND reason_code LIKE 'OZON_STOCKOUT%' THEN 'На Ozon запаса меньше недели по плану — без поставки продажи остановятся'
      WHEN use_template AND action_type = 'ASSEMBLE_BUNDLES' THEN 'Наборы недели по плану сборки — готовых наборов на фулфилменте нет, они собираются под отгрузку'
      WHEN use_template AND action_type = 'ADS_REVIEW' AND REGEXP_CONTAINS(reason_text, r'ДРР 7 дн\.') THEN
        CONCAT(TRIM(REGEXP_EXTRACT(reason_text, r'(ДРР 7 дн\. [^(;]+)')), IFNULL(CONCAT(' — ', REGEXP_EXTRACT(reason_text, r'(перерасход ≈ [^.]+)'), ' за неделю'), ''))
      WHEN use_template AND action_type = 'ACCELERATE' AND REGEXP_CONTAINS(reason_text, r'запас ') THEN
        CONCAT(REGEXP_EXTRACT(reason_text, r'(запас [^;]+)'), ' — к 31.03.2027 останется ≈ ', IFNULL(REGEXP_EXTRACT(reason_text, r'≈ (\d+) фл\.'), '?'), ' фл.',
               IFNULL(CONCAT(' (срок годности ', REGEXP_EXTRACT(reason_text, r'срок годности ([0-9.]+)'), ')'), ''))
      WHEN use_template THEN TRIM(SPLIT(reason_text, ';')[SAFE_OFFSET(0)])
      WHEN reason_tail IS NULL OR reason_tail = '' THEN reason_head
      ELSE reason_tail
    END AS why_owner,
    -- Перечень SKU из seed-строк «Снять с паллет / Отгрузить …: SKU N, SKU N …» — только для drill-down
    IF(use_template AND action_type IN ('PULL_FROM_PALLETS', 'REPLENISH_WB', 'REPLENISH_OZON') AND REGEXP_CONTAINS(reason_head, r': .+ \d+, .+ \d+'),
       REGEXP_EXTRACT(reason_head, r'^[^:]+: (.*)$'), NULL) AS sku_breakdown,
    -- Phase 1.2: тип финансового эффекта — что именно означает сумма
    CASE
      WHEN financial_effect_rub IS NULL THEN NULL
      WHEN reason_code LIKE 'EXPIRY%' OR action_type = 'LIQUIDATION' THEN IF(financial_effect_rub < 0, 'Риск списания', 'Предотвращённый риск списания')
      WHEN action_type = 'ADS_REVIEW' OR reason_code LIKE 'DRR%' THEN IF(financial_effect_rub < 0, 'Перерасход рекламы', 'Ожидаемый вклад')
      WHEN action_type = 'ACCELERATE' OR reason_code LIKE 'OVERSTOCK%' THEN 'Потенциальный cash effect'
      WHEN action_type = 'DECISION' AND financial_effect_rub < 0 THEN 'Стоимость решения'
      WHEN financial_effect_rub < 0 THEN 'Выручка под риском'
      ELSE 'Потенциальная выручка'
    END AS effect_kind_ru
  FROM q2
)
SELECT action_id, priority, SUBSTR(priority, 1, 2) AS priority_short, priority_rank, status, status_rank, is_open, is_overdue, color,
  lane, CASE lane WHEN 'DECISION' THEN 'Решение владельца' WHEN 'EXECUTION' THEN 'Исполнение' ELSE 'Наблюдение' END AS lane_ru,
  action_type, action_type_ru, marketplace, sku, sku_name, bundle_id, qty,
  IF(qty IS NULL OR qty = 0, NULL, CONCAT(REPLACE(FORMAT('%\'d', CAST(qty AS INT64)), ',', ' '), IF(action_type = 'ASSEMBLE_BUNDLES', ' наб.', ' фл.'))) AS qty_ru,
  financial_effect_rub,
  CASE
    WHEN financial_effect_rub IS NULL THEN NULL
    WHEN ABS(financial_effect_rub) >= 1000000 THEN CONCAT(IF(financial_effect_rub < 0, '−', '+'), FORMAT('%.1f', ABS(financial_effect_rub) / 1000000), ' млн ₽')
    WHEN ABS(financial_effect_rub) >= 1000 THEN CONCAT(IF(financial_effect_rub < 0, '−', '+'), CAST(CAST(ROUND(ABS(financial_effect_rub) / 1000) AS INT64) AS STRING), ' тыс ₽')
    ELSE CONCAT(IF(financial_effect_rub < 0, '−', '+'), CAST(CAST(ROUND(ABS(financial_effect_rub)) AS INT64) AS STRING), ' ₽')
  END AS effect_ru,
  effect_kind_ru,
  IF(financial_effect_rub IS NULL, NULL, CONCAT(effect_kind_ru, ': ',
    CASE
      WHEN ABS(financial_effect_rub) >= 1000000 THEN CONCAT(IF(financial_effect_rub < 0, '−', '+'), FORMAT('%.1f', ABS(financial_effect_rub) / 1000000), ' млн ₽')
      WHEN ABS(financial_effect_rub) >= 1000 THEN CONCAT(IF(financial_effect_rub < 0, '−', '+'), CAST(CAST(ROUND(ABS(financial_effect_rub) / 1000) AS INT64) AS STRING), ' тыс ₽')
      ELSE CONCAT(IF(financial_effect_rub < 0, '−', '+'), CAST(CAST(ROUND(ABS(financial_effect_rub)) AS INT64) AS STRING), ' ₽')
    END)) AS effect_line,
  -- Phase 1.2: технические коды из авторских seed-формулировок заменяются человеческими словами
  -- (оригинал остаётся в reason_text для drill-down)
  REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(what_to_do,
    ' (ADVERTISING_ACTIONS)', ''), ' (HC-B)', ''), ' по BUNDLE_PRODUCTION_PLAN', ' по плану сборки'), 'план SET', 'план'), ' в SET', ' по плану'), 'FBS = 0', 'FBS сейчас не используется') AS what_to_do,
  executor_ru,
  REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(why_owner,
    ' (HARD)', ' — жёсткий срок'), 'RAW_WB_SUPPLIES: ', 'в кабинете WB '), 'приёмка NOT_PROVEN', 'приёмка не подтверждена'), 'V_ADS_SKU_ECONOMIC_LIMITS: ', 'по экономике рекламы: '),
    'ABOVE_BREAKEVEN', 'выше точки безубыточности'), 'ETA ASSUMPTION', 'дата прихода — допущение, поставщиком не подтверждена'), 'expiry Oct-2027', 'срок годности октябрь 2027'),
    'по плану SET ', 'по плану '), 'runout март 2027 в SET', 'по плану запас кончится в марте 2027'), 'FBS = 0 сегодня', 'FBS сейчас не используется'), ' (NEGATIVE_BEFORE_ADS)', '') AS why_owner,
  sku_breakdown,
  -- why_short сохранён для совместимости (Phase 1.1); с Phase 1.2 без обрезки «…»
  why_full AS why_short,
  CONCAT('[', SUBSTR(priority, 1, 2), '] ', action_type_ru, ' · ', IFNULL(NULLIF(marketplace, 'ALL'), 'все каналы'), IF(sku IN ('PORTFOLIO', 'BUNDLES', 'FF', 'DATA', 'BATCH-05') OR sku IS NULL, '', CONCAT(' · ', sku_name)),
         IF(qty IS NULL OR qty = 0, '', CONCAT(' · ', CAST(CAST(qty AS INT64) AS STRING), ' шт.')),
         IF(deadline IS NULL, '', CONCAT(' · до ', FORMAT_DATE('%d.%m', deadline)))) AS headline,
  reason_code, reason_text, source_metric, deadline,
  CASE
    WHEN deadline IS NULL THEN '—'
    WHEN deadline < CURRENT_DATE() AND status IN ('OPEN', 'IN_PROGRESS') THEN CONCAT('просрочено ', CAST(DATE_DIFF(CURRENT_DATE(), deadline, DAY) AS STRING), ' дн.')
    WHEN deadline = CURRENT_DATE() THEN 'сегодня'
    WHEN deadline = DATE_ADD(CURRENT_DATE(), INTERVAL 1 DAY) THEN 'завтра'
    ELSE CONCAT('до ', FORMAT_DATE('%d.%m', deadline))
  END AS deadline_ru,
  days_to_deadline, action_date, generated_at, generated_by,
  status_updated_at, completed_at, owner_note, plan_version, first_seen_at, last_seen_at, times_seen, dedup_key,
  IF(cfg.webapp_url IS NULL, NULL, CONCAT(cfg.webapp_url, '?id=', action_id, '&status=DONE')) AS done_url,
  IF(cfg.webapp_url IS NULL, NULL, CONCAT(cfg.webapp_url, '?id=', action_id, '&status=IN_PROGRESS')) AS progress_url,
  IF(cfg.webapp_url IS NULL, NULL, CONCAT(cfg.webapp_url, '?id=', action_id, '&status=CANCELLED')) AS cancel_url,
  IF(cfg.webapp_url IS NULL, NULL, CONCAT(cfg.webapp_url, '?id=', action_id)) AS action_url,
  ROW_NUMBER() OVER (ORDER BY status_rank, priority_rank, deadline, action_id) AS queue_rank,
  ROW_NUMBER() OVER (PARTITION BY lane ORDER BY status_rank, priority_rank, deadline, action_id) AS lane_rank,
  CURRENT_DATE() AS as_of
FROM q3 CROSS JOIN cfg;

CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_OWNER_HOME` AS
-- Single-row owner KPI set: yesterday / MTD / season / operating result / bundles / hand cream / actions / attention / freshness.
-- Phase 1.1: + план на вчера (флаконы, вклад), + уверенность прогноза (LOW / MEDIUM / HIGH с причиной),
--   + риск наличия на площадках, + статус обновления Control Tower (CONTROL TOWER UPDATED AT), + готовые строки для дашборда.
--
-- ПРАВИЛА УВЕРЕННОСТИ ПРОГНОЗА (явные, проверяются тестом T27):
--   базовый уровень по числу закрытых плановых дней: 0–2 → LOW(1), 3–9 → MEDIUM(2), ≥10 → HIGH(3);
--   каждое из условий ниже понижает уровень на одну ступень (не ниже LOW):
--     a) данные за вчера неполные (WB или Ozon не закрыли вчерашний день);
--     b) риск наличия: ≥ 10 % плана ближайших 14 дней приходится на SKU без недельного покрытия на площадке;
--     c) темп расходится с сезонной кривой: |прогноз EOM / план месяца − 1| > 50 %.
--   forecast_confidence_reason перечисляет сработавшие условия человеческим языком.
WITH ref AS (SELECT DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY) AS yday, DATE_TRUNC(CURRENT_DATE(), MONTH) AS m0),
a AS (SELECT a.* FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_ACTUAL_DAILY` a CROSS JOIN ref WHERE a.d >= DATE_SUB(ref.m0, INTERVAL 1 MONTH) AND a.d <= ref.yday),
yday AS (
  SELECT
    SUM(cards_ordered) AS cards_ordered_yesterday, SUM(cards_sold) AS cards_sold_yesterday, SUM(cards_cancelled) AS cards_cancelled_yesterday,
    SUM(units_ordered) AS units_yesterday, SUM(units_sold) AS units_sold_yesterday,
    SUM(gmv_ordered) AS gmv_yesterday, SUM(revenue_seller_base) AS revenue_yesterday, SUM(seller_cash) AS seller_cash_yesterday,
    SUM(contribution) AS contribution_yesterday, SUM(ad_spend) AS ad_spend_yesterday,
    SAFE_DIVIDE(SUM(ad_spend), SUM(gmv_ordered)) * 100 AS drr_yesterday_pct,
    SUM(IF(marketplace = 'WB', cards_ordered, 0)) AS wb_cards_yesterday, SUM(IF(marketplace = 'OZON', cards_ordered, 0)) AS ozon_cards_yesterday,
    SUM(IF(sales_mode = 'BUNDLE', cards_ordered, 0)) AS bundles_ordered_yesterday,
    SUM(IF(sales_mode = 'BUNDLE', units_ordered, 0)) AS units_via_bundles_yesterday
  FROM a, ref WHERE a.d = ref.yday
),
-- Phase 1.2: сопоставимые периоды для контекста KPI «вчера» — позавчера и тот же день недели неделю назад
prev AS (
  SELECT
    SUM(IF(a.d = DATE_SUB(ref.yday, INTERVAL 1 DAY), cards_ordered, 0)) AS cards_ordered_dby,
    SUM(IF(a.d = DATE_SUB(ref.yday, INTERVAL 1 DAY), units_ordered, 0)) AS units_dby,
    SUM(IF(a.d = DATE_SUB(ref.yday, INTERVAL 1 DAY), gmv_ordered, 0)) AS gmv_dby,
    SUM(IF(a.d = DATE_SUB(ref.yday, INTERVAL 1 DAY), contribution, 0)) AS contribution_dby,
    SAFE_DIVIDE(SUM(IF(a.d = DATE_SUB(ref.yday, INTERVAL 1 DAY), ad_spend, 0)), SUM(IF(a.d = DATE_SUB(ref.yday, INTERVAL 1 DAY), gmv_ordered, 0))) * 100 AS drr_dby_pct,
    SUM(IF(a.d = DATE_SUB(ref.yday, INTERVAL 7 DAY), cards_ordered, 0)) AS cards_ordered_lw,
    SUM(IF(a.d = DATE_SUB(ref.yday, INTERVAL 7 DAY), units_ordered, 0)) AS units_lw,
    SUM(IF(a.d = DATE_SUB(ref.yday, INTERVAL 7 DAY), gmv_ordered, 0)) AS gmv_lw,
    SUM(IF(a.d = DATE_SUB(ref.yday, INTERVAL 7 DAY), contribution, 0)) AS contribution_lw,
    SAFE_DIVIDE(SUM(IF(a.d = DATE_SUB(ref.yday, INTERVAL 7 DAY), ad_spend, 0)), SUM(IF(a.d = DATE_SUB(ref.yday, INTERVAL 7 DAY), gmv_ordered, 0))) * 100 AS drr_lw_pct
  FROM a, ref WHERE a.d IN (DATE_SUB(ref.yday, INTERVAL 1 DAY), DATE_SUB(ref.yday, INTERVAL 7 DAY))
),
mtd AS (
  SELECT
    SUM(cards_ordered) AS cards_ordered_mtd, SUM(cards_sold) AS cards_sold_mtd, SUM(units_ordered) AS units_mtd, SUM(units_sold) AS units_sold_mtd,
    SUM(gmv_ordered) AS gmv_mtd, SUM(seller_cash) AS seller_cash_mtd, SUM(contribution) AS contribution_mtd, SUM(ad_spend) AS ad_spend_mtd,
    SAFE_DIVIDE(SUM(ad_spend), SUM(gmv_ordered)) * 100 AS drr_mtd_pct,
    SUM(IF(marketplace = 'WB', cards_ordered, 0)) AS wb_cards_mtd, SUM(IF(marketplace = 'OZON', cards_ordered, 0)) AS ozon_cards_mtd,
    SUM(IF(sales_mode = 'BUNDLE', cards_ordered, 0)) AS bundles_ordered_mtd,
    SUM(IF(sales_mode = 'BUNDLE', units_ordered, 0)) AS units_via_bundles_mtd,
    SAFE_DIVIDE(SUM(IF(sales_mode = 'BUNDLE', units_ordered, 0)), SUM(units_ordered)) * 100 AS bundle_share_units_mtd_pct,
    SUM(IF(a.d > DATE_SUB(ref.yday, INTERVAL 7 DAY), cards_ordered, 0)) / 7.0 AS cards_per_day_7d,
    SUM(IF(a.d > DATE_SUB(ref.yday, INTERVAL 7 DAY), gmv_ordered, 0)) / 7.0 AS gmv_per_day_7d
  FROM a, ref WHERE a.d >= ref.m0
),
plan AS (
  SELECT
    SUM(IF(p.plan_date = r.yday, target_cards, 0)) AS plan_cards_yesterday,
    SUM(IF(p.plan_date = r.yday, target_physical_units, 0)) AS plan_units_yesterday,
    SUM(IF(p.plan_date = r.yday, target_gmv, 0)) AS plan_gmv_yesterday,
    SUM(IF(p.plan_date = r.yday, target_contribution, 0)) AS plan_contribution_yesterday,
    SAFE_DIVIDE(SUM(IF(p.plan_date = r.yday, target_ad_spend, 0)), SUM(IF(p.plan_date = r.yday, target_gmv, 0))) * 100 AS plan_drr_yesterday_pct,
    SUM(IF(p.plan_date >= r.m0 AND p.plan_date <= r.yday, target_cards, 0)) AS plan_cards_mtd,
    SUM(IF(p.plan_date >= r.m0 AND p.plan_date <= r.yday, target_physical_units, 0)) AS plan_units_mtd,
    SUM(IF(p.plan_date >= r.m0 AND p.plan_date <= r.yday, target_gmv, 0)) AS plan_gmv_mtd,
    SUM(IF(p.plan_date >= r.m0 AND p.plan_date <= r.yday, target_contribution, 0)) AS plan_contribution_mtd,
    SUM(IF(p.plan_date >= r.m0 AND p.plan_date <= LAST_DAY(r.m0), target_cards, 0)) AS plan_cards_month,
    SUM(IF(p.plan_date >= r.m0 AND p.plan_date <= LAST_DAY(r.m0), target_physical_units, 0)) AS plan_units_month,
    SUM(IF(p.plan_date >= r.m0 AND p.plan_date <= LAST_DAY(r.m0), target_gmv, 0)) AS plan_gmv_month,
    SUM(IF(p.plan_date >= r.m0 AND p.plan_date <= LAST_DAY(r.m0), target_contribution, 0)) AS plan_contribution_month,
    MIN(IF(p.plan_date >= r.m0, p.plan_date, NULL)) AS plan_month_from,
    COUNT(DISTINCT IF(p.plan_date >= r.m0 AND p.plan_date <= r.yday, p.plan_date, NULL)) AS plan_days_elapsed,
    COUNT(DISTINCT IF(p.plan_date > r.yday AND p.plan_date <= LAST_DAY(r.m0), p.plan_date, NULL)) AS plan_days_remaining,
    COUNT(DISTINCT IF(p.plan_date >= r.m0 AND p.plan_date <= LAST_DAY(r.m0), p.plan_date, NULL)) AS plan_days_month,
    SUM(IF(p.plan_date >= r.m0 AND p.plan_date <= r.yday AND p.sales_mode = 'BUNDLE', target_cards, 0)) AS plan_bundles_mtd
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_PLAN_ACTIVE` p CROSS JOIN ref r
),
actual_in_plan AS (
  SELECT SUM(IF(a.d >= plan.plan_month_from, a.cards_ordered, 0)) AS cards_ordered_plan_days_mtd,
         SUM(IF(a.d < plan.plan_month_from, a.cards_ordered, 0)) AS pre_plan_cards,
         SUM(IF(a.d >= plan.plan_month_from, a.units_ordered, 0)) AS units_plan_days_mtd,
         SUM(IF(a.d >= plan.plan_month_from, a.gmv_ordered, 0)) AS gmv_plan_days_mtd,
         SUM(IF(a.d >= plan.plan_month_from, a.contribution, 0)) AS contribution_plan_days_mtd,
         SUM(IF(a.d >= plan.plan_month_from, a.seller_cash, 0)) AS seller_cash_plan_days_mtd,
         SUM(IF(a.d >= plan.plan_month_from, a.ad_spend, 0)) AS ad_spend_plan_days_mtd,
         SUM(IF(a.d >= plan.plan_month_from AND a.d > DATE_SUB(r.yday, INTERVAL 7 DAY), a.units_ordered, 0)) / 7.0 AS units_per_day_7d_plan,
         SUM(IF(a.d >= plan.plan_month_from AND a.d > DATE_SUB(r.yday, INTERVAL 7 DAY), a.gmv_ordered, 0)) / 7.0 AS gmv_per_day_7d_plan
  FROM a CROSS JOIN ref r CROSS JOIN plan WHERE a.d >= r.m0
),
cc_m AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_CASH_CONVERSION` WHERE period_type = 'MONTH' AND month_status = 'CURRENT'),
cc_s AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_CASH_CONVERSION` WHERE period_type = 'SEASON_TO_DATE'),
cc_p AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_CASH_CONVERSION` WHERE period_type = 'SEASON_PLAN'),
bundles AS (
  SELECT COUNTIF(status_code IN ('BLOCKED_INBOUND', 'BLOCKED_COMPONENT')) AS bundles_blocked_count,
    COUNTIF(status_code IN ('ASSEMBLE_NOW', 'COMPONENT_SHORT')) AS bundles_assemble_now_count,
    SUM(assembly_need_next_45d) AS bundles_assembly_need_45d,
    SUM(marketplace_cards_live) AS bundle_cards_on_marketplaces,
    MIN(next_assembly_week_start) AS next_assembly_week_start, SUM(next_assembly_qty) AS next_assembly_qty
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_BUNDLE_STATUS`
),
hc AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_HAND_CREAM_CONTROL`),
q AS (
  SELECT COUNTIF(is_open) AS open_actions, COUNTIF(is_open AND priority = 'P0 TODAY') AS p0_actions, COUNTIF(is_open AND priority = 'P1 THIS WEEK') AS p1_actions,
    COUNTIF(is_overdue) AS overdue_actions, COUNTIF(status = 'DONE' AND DATE(completed_at) >= DATE_TRUNC(CURRENT_DATE(), MONTH)) AS done_actions_mtd,
    COUNTIF(is_open AND lane = 'DECISION') AS open_decisions, COUNTIF(is_open AND lane = 'EXECUTION') AS open_executions,
    COUNTIF(status = 'IN_PROGRESS') AS in_progress_actions,
    ARRAY_AGG(IF(is_open, headline, NULL) IGNORE NULLS ORDER BY queue_rank LIMIT 1)[SAFE_OFFSET(0)] AS top_action
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_ACTION_QUEUE`
),
att AS (
  SELECT COUNTIF(severity = 'RED') AS red_alerts, COUNTIF(severity = 'YELLOW') AS yellow_alerts, COUNTIF(severity = 'BLUE') AS blue_alerts,
    ARRAY_AGG(IF(severity = 'RED', headline, NULL) IGNORE NULLS ORDER BY sort_rank, metric_value LIMIT 1)[SAFE_OFFSET(0)] AS top_red_alert
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_ATTENTION`
),
fr AS (
  SELECT
    MAX(IF(domain = 'WB_SALES', data_as_of, NULL)) AS wb_sales_as_of, MAX(IF(domain = 'OZON_SALES', data_as_of, NULL)) AS ozon_sales_as_of,
    MAX(IF(domain = 'WB_STOCK', data_as_of, NULL)) AS wb_stock_as_of, MAX(IF(domain = 'OZON_STOCK', data_as_of, NULL)) AS ozon_stock_as_of,
    MAX(IF(domain = 'FF_STOCK', data_as_of, NULL)) AS ff_stock_as_of, MAX(IF(domain = 'WB_ADS', data_as_of, NULL)) AS wb_ads_as_of,
    MAX(IF(domain = 'OZON_ADS', data_as_of, NULL)) AS ozon_ads_as_of, MAX(IF(domain = 'CT_ACTUALS', data_as_of_ts, NULL)) AS ct_refreshed_at,
    COUNTIF(status != 'OK') AS stale_domains_count,
    STRING_AGG(IF(status != 'OK', CONCAT(domain_ru, ' (', IFNULL(FORMAT_DATE('%d.%m', data_as_of), 'нет данных'), ')'), NULL), '; ' ORDER BY sort_order) AS stale_domains
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_FRESHNESS`
),
avail AS (
  -- Риск наличия: доля плана ближайших 14 дней у SKU без недельного покрытия (или с нулём) на площадке
  SELECT
    SAFE_DIVIDE(SUM(IF(marketplace_units + in_transit_units <= 0 OR cover_days_at_plan_rate < 7, plan_units_next_14d, 0)), SUM(plan_units_next_14d)) * 100 AS availability_risk_plan_share_pct,
    COUNTIF((marketplace_units + in_transit_units <= 0 OR cover_days_at_plan_rate < 7) AND plan_units_next_14d > 0) AS availability_risk_sku_count,
    STRING_AGG(IF((marketplace_units + in_transit_units <= 0 OR cover_days_at_plan_rate < 7) AND plan_units_next_14d > 0,
                  CONCAT(marketplace, ': ', product_name, ' (', IFNULL(CAST(CAST(ROUND(cover_days_at_plan_rate) AS INT64) AS STRING), '0'), ' дн.)'), NULL), ', ' ORDER BY plan_units_next_14d DESC LIMIT 3) AS availability_risk_top
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_SUPPLY_NEED`
),
rs AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_REFRESH_STATUS`),
base AS (
SELECT
  ref.yday AS yesterday_date, CURRENT_DATE() AS as_of, CURRENT_TIMESTAMP() AS generated_at,
  -- YESTERDAY
  yday.cards_ordered_yesterday, yday.cards_sold_yesterday, yday.cards_cancelled_yesterday, yday.units_yesterday, yday.units_sold_yesterday,
  yday.gmv_yesterday, yday.revenue_yesterday, yday.seller_cash_yesterday, yday.contribution_yesterday, yday.ad_spend_yesterday, yday.drr_yesterday_pct,
  yday.wb_cards_yesterday, yday.ozon_cards_yesterday, yday.bundles_ordered_yesterday, yday.units_via_bundles_yesterday,
  plan.plan_cards_yesterday, plan.plan_units_yesterday, plan.plan_gmv_yesterday, plan.plan_contribution_yesterday, plan.plan_drr_yesterday_pct,
  SAFE_DIVIDE(yday.cards_ordered_yesterday, plan.plan_cards_yesterday) * 100 AS yesterday_attainment_pct,
  fr.wb_sales_as_of >= ref.yday AND fr.ozon_sales_as_of >= ref.yday AS yesterday_data_complete,
  -- PREVIOUS COMPARABLE PERIODS (Phase 1.2): позавчера (dby) и тот же день недели неделю назад (lw)
  DATE_SUB(ref.yday, INTERVAL 1 DAY) AS dby_date, DATE_SUB(ref.yday, INTERVAL 7 DAY) AS lw_date,
  prev.cards_ordered_dby, prev.units_dby, prev.gmv_dby, prev.contribution_dby, prev.drr_dby_pct,
  prev.cards_ordered_lw, prev.units_lw, prev.gmv_lw, prev.contribution_lw, prev.drr_lw_pct,
  -- MTD
  mtd.cards_ordered_mtd, mtd.cards_sold_mtd, mtd.units_mtd, mtd.units_sold_mtd, mtd.gmv_mtd, mtd.seller_cash_mtd, mtd.contribution_mtd, mtd.ad_spend_mtd, mtd.drr_mtd_pct,
  mtd.wb_cards_mtd, mtd.ozon_cards_mtd, mtd.cards_per_day_7d, mtd.gmv_per_day_7d,
  plan.plan_month_from, plan.plan_days_elapsed, plan.plan_days_remaining, plan.plan_days_month,
  plan.plan_cards_mtd, plan.plan_units_mtd, plan.plan_gmv_mtd, plan.plan_contribution_mtd,
  plan.plan_cards_month, plan.plan_units_month, plan.plan_gmv_month, plan.plan_contribution_month,
  aip.cards_ordered_plan_days_mtd, aip.pre_plan_cards,
  aip.units_plan_days_mtd, aip.gmv_plan_days_mtd, aip.contribution_plan_days_mtd, aip.seller_cash_plan_days_mtd, aip.ad_spend_plan_days_mtd,
  SAFE_DIVIDE(aip.units_plan_days_mtd, plan.plan_units_mtd) * 100 AS mtd_attainment_units_pct,
  SAFE_DIVIDE(aip.gmv_plan_days_mtd, plan.plan_gmv_mtd) * 100 AS mtd_attainment_gmv_pct,
  SAFE_DIVIDE(aip.contribution_plan_days_mtd, plan.plan_contribution_mtd) * 100 AS mtd_attainment_contribution_pct,
  aip.units_plan_days_mtd + aip.units_per_day_7d_plan * plan.plan_days_remaining AS forecast_eom_units_plan_days,
  aip.gmv_plan_days_mtd + aip.gmv_per_day_7d_plan * plan.plan_days_remaining AS forecast_eom_gmv_plan_days,
  SAFE_DIVIDE(aip.cards_ordered_plan_days_mtd, plan.plan_cards_mtd) * 100 AS mtd_attainment_pct,
  mtd.cards_ordered_mtd + mtd.cards_per_day_7d * plan.plan_days_remaining AS forecast_eom_cards,
  mtd.gmv_mtd + mtd.gmv_per_day_7d * plan.plan_days_remaining AS forecast_eom_gmv,
  aip.cards_ordered_plan_days_mtd + mtd.cards_per_day_7d * plan.plan_days_remaining AS forecast_eom_cards_plan_days,
  SAFE_DIVIDE(aip.cards_ordered_plan_days_mtd + mtd.cards_per_day_7d * plan.plan_days_remaining, plan.plan_cards_month) * 100 AS forecast_eom_attainment_pct,
  SAFE_DIVIDE(plan.plan_cards_month - aip.cards_ordered_plan_days_mtd, NULLIF(plan.plan_days_remaining, 0)) AS required_daily_velocity_remaining,
  SAFE_DIVIDE(aip.cards_ordered_plan_days_mtd, NULLIF(plan.plan_days_elapsed, 0)) AS actual_daily_velocity_plan_days,
  -- OPERATING RESULT (month)
  cc_m.opex_allocated AS opex_allocated_mtd, cc_m.plan_opex AS opex_month, cc_m.operating_result AS operating_result_mtd, cc_m.plan_operating_result AS plan_operating_result_month,
  -- SEASON
  cc_s.opening_as_of, cc_s.opening_units AS opening_inventory_units, cc_s.opening_value_rub AS opening_inventory_value_rub,
  cc_s.received_units, cc_s.units_ordered_since_opening AS season_units_ordered, cc_s.units_sold_since_opening AS season_units_sold,
  cc_s.inventory_remaining_units, cc_s.inventory_remaining_value_rub, cc_s.inventory_delta_units, cc_s.wb_lost_claimed_units, cc_s.inbound_open_units,
  cc_s.stock_reduction_pct, cc_s.inventory_cash_conversion_pct, cc_s.cogs_released_since_opening, cc_s.seller_cash_since_opening,
  cc_s.projected_residual_31_03_units_at_plan, cc_s.projected_residual_31_03_units_at_30d_rate,
  cc_s.actual_cards AS season_cards, cc_s.actual_gmv AS season_gmv, cc_s.actual_seller_cash AS season_seller_cash, cc_s.actual_contribution AS season_contribution,
  cc_s.opex_allocated AS season_opex, cc_s.operating_result AS season_operating_result,
  cc_p.plan_cards AS season_plan_cards, cc_p.plan_units AS season_plan_units, cc_p.plan_gmv AS season_plan_gmv, cc_p.plan_contribution AS season_plan_contribution,
  cc_p.plan_opex AS season_plan_opex, cc_p.plan_operating_result AS season_plan_operating_result, cc_p.inventory_remaining_computed_units AS season_plan_closing_units,
  cc_p.stock_reduction_pct AS season_plan_stock_reduction_pct, cc_p.inventory_cash_conversion_pct AS season_plan_cash_conversion_pct,
  -- BUNDLES
  mtd.bundles_ordered_mtd, mtd.units_via_bundles_mtd, mtd.bundle_share_units_mtd_pct, plan.plan_bundles_mtd,
  bundles.bundles_blocked_count, bundles.bundles_assemble_now_count, bundles.bundles_assembly_need_45d, bundles.bundle_cards_on_marketplaces,
  bundles.next_assembly_week_start, bundles.next_assembly_qty,
  -- HAND CREAM
  hc.current_stock_units AS hc_stock_units, hc.days_to_expiry AS hc_days_to_expiry, hc.units_ordered_mtd AS hc_units_mtd,
  hc.required_units_per_day AS hc_required_units_per_day, hc.actual_units_per_day_7d AS hc_actual_units_per_day_7d, hc.actual_units_per_day_30d AS hc_actual_units_per_day_30d,
  hc.projected_residual_at_expiry_at_30d_rate AS hc_projected_residual_at_expiry, hc.writeoff_risk_at_30d_rate_rub AS hc_writeoff_risk_rub,
  hc.liquidation_contribution_season_to_date AS hc_liquidation_contribution, hc.status AS hc_status,
  -- ACTIONS / ATTENTION
  q.open_actions, q.p0_actions, q.p1_actions, q.overdue_actions, q.done_actions_mtd, q.open_decisions, q.open_executions, q.in_progress_actions, q.top_action,
  att.red_alerts, att.yellow_alerts, att.blue_alerts, att.top_red_alert,
  -- FRESHNESS
  fr.wb_sales_as_of, fr.ozon_sales_as_of, fr.wb_stock_as_of, fr.ozon_stock_as_of, fr.ff_stock_as_of, fr.wb_ads_as_of, fr.ozon_ads_as_of, fr.ct_refreshed_at,
  fr.stale_domains_count, fr.stale_domains, IF(fr.stale_domains_count = 0, 'OK', 'STALE') AS data_status,
  -- AVAILABILITY
  avail.availability_risk_plan_share_pct, avail.availability_risk_sku_count, avail.availability_risk_top,
  -- CONTROL TOWER REFRESH
  rs.refresh_status AS ct_refresh_status, rs.last_ok_ts AS ct_last_ok_ts, rs.last_ok_msk AS ct_last_ok_msk, rs.hours_since_ok AS ct_hours_since_ok,
  rs.last_attempt_status AS ct_last_attempt_status, rs.last_error_message AS ct_last_error_message, rs.status_line AS ct_status_line, rs.warn_today AS ct_warn_today,
  -- OVERALL
  CASE
    WHEN fr.stale_domains_count > 0 THEN 'YELLOW'
    WHEN SAFE_DIVIDE(aip.cards_ordered_plan_days_mtd, plan.plan_cards_mtd) < 0.7 AND plan.plan_days_elapsed >= 3 THEN 'RED'
    WHEN SAFE_DIVIDE(aip.cards_ordered_plan_days_mtd, plan.plan_cards_mtd) < 0.9 AND plan.plan_days_elapsed >= 3 THEN 'YELLOW'
    ELSE 'GREEN'
  END AS plan_status
FROM ref CROSS JOIN yday CROSS JOIN prev CROSS JOIN mtd CROSS JOIN plan CROSS JOIN actual_in_plan aip
LEFT JOIN cc_m ON TRUE LEFT JOIN cc_s ON TRUE LEFT JOIN cc_p ON TRUE
CROSS JOIN bundles LEFT JOIN hc ON TRUE CROSS JOIN q CROSS JOIN att CROSS JOIN fr CROSS JOIN avail LEFT JOIN rs ON TRUE
),
conf AS (
  SELECT b.*,
    CASE WHEN plan_days_elapsed >= 10 THEN 3 WHEN plan_days_elapsed >= 3 THEN 2 ELSE 1 END AS conf_base_level,
    NOT IFNULL(yesterday_data_complete, FALSE) AS conf_flag_data_incomplete,
    IFNULL(availability_risk_plan_share_pct, 0) >= 10 AS conf_flag_availability,
    ABS(IFNULL(forecast_eom_attainment_pct, 100) - 100) > 50 AS conf_flag_deviation
  FROM base b
),
conf2 AS (
  SELECT c.*,
    GREATEST(1, conf_base_level - IF(conf_flag_data_incomplete, 1, 0) - IF(conf_flag_availability, 1, 0) - IF(conf_flag_deviation, 1, 0)) AS forecast_confidence_score
  FROM conf c
)
SELECT c.* EXCEPT (conf_base_level),
  CASE forecast_confidence_score WHEN 3 THEN 'HIGH' WHEN 2 THEN 'MEDIUM' ELSE 'LOW' END AS forecast_confidence,
  CASE forecast_confidence_score WHEN 3 THEN 'высокая' WHEN 2 THEN 'средняя' ELSE 'низкая' END AS forecast_confidence_ru,
  ARRAY_TO_STRING(ARRAY(
    SELECT r FROM UNNEST([
      IF(plan_days_elapsed < 3, CONCAT('только ', CAST(plan_days_elapsed AS STRING), IF(plan_days_elapsed = 1, ' закрытый плановый день', ' закрытых плановых дня'), ' из ', CAST(plan_days_month AS STRING)), NULL),
      IF(plan_days_elapsed BETWEEN 3 AND 9, CONCAT(CAST(plan_days_elapsed AS STRING), ' плановых дней из ', CAST(plan_days_month AS STRING), ' — меньше двух недель'), NULL),
      IF(conf_flag_data_incomplete, 'данные за вчера неполные (WB или Ozon не закрыли день)', NULL),
      IF(conf_flag_availability, CONCAT('риск наличия: ', CAST(CAST(ROUND(availability_risk_plan_share_pct) AS INT64) AS STRING), ' % плана без недельного покрытия (', IFNULL(availability_risk_top, ''), ')'), NULL),
      IF(conf_flag_deviation, CONCAT('темп расходится с сезонной кривой на ', CAST(CAST(ROUND(ABS(forecast_eom_attainment_pct - 100)) AS INT64) AS STRING), ' %'), NULL)
    ]) r WHERE r IS NOT NULL), '; ') AS forecast_confidence_reason,
  CONCAT('Прогноз на ', CASE EXTRACT(MONTH FROM CURRENT_DATE()) WHEN 1 THEN 'январь' WHEN 2 THEN 'февраль' WHEN 3 THEN 'март' WHEN 4 THEN 'апрель' WHEN 5 THEN 'май' WHEN 6 THEN 'июнь' WHEN 7 THEN 'июль' WHEN 8 THEN 'август' WHEN 9 THEN 'сентябрь' WHEN 10 THEN 'октябрь' WHEN 11 THEN 'ноябрь' ELSE 'декабрь' END, ': ', CAST(CAST(ROUND(forecast_eom_cards_plan_days) AS INT64) AS STRING), ' из ', CAST(CAST(ROUND(plan_cards_month) AS INT64) AS STRING), ' проданных позиций (',
         CAST(CAST(ROUND(forecast_eom_attainment_pct) AS INT64) AS STRING), ' %) · уверенность ',
         CASE forecast_confidence_score WHEN 3 THEN 'HIGH' WHEN 2 THEN 'MEDIUM' ELSE 'LOW' END) AS forecast_line
FROM conf2 c;
