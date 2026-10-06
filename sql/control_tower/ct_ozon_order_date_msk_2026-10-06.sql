-- ============================================================================
-- Control Tower · бизнес-дата заказа Ozon = календарные сутки МСК (2026-10-06).
-- Сопутствующее изменение к ozon_mart (docs/ops/OZON_ORDER_DATE_MSK_2026-10-06.md): разворачивается
-- В ТОМ ЖЕ ОКНЕ, что и 7 представлений ozon_mart, затем — ПРИНУДИТЕЛЬНАЯ пересборка снимка CT_ACTUAL_DAILY
-- (CT_CONFIG.refresh_force = '1' → CALL evetis_ref.sp_ct_refresh_daily(); без флага процедура выйдет SKIP:
-- её отпечаток источников не учитывает определения представлений). Описание V_CT_PLAN_VS_ACTUAL_DAILY сохранено.
--
-- ПОЧЕМУ: RAW_OZON_POSTINGS_FBO.order_date — UTC-дата created_at. После перевода ozon_mart на
-- DATE(created_at, 'Europe/Moscow') CT строил бы строки Ozon (заказы, отмены, COGS, затраты по отправлению)
-- на UTC-сутках, а выручку подтягивал LEFT JOIN-ом из FCT_OZON_SKU_PNL_DAILY на МСК-сутках: у граничных
-- отправлений выручка выпадала из CT (≈ 2 258 ₽, U03 FAIL), у остальных — вклад суток рассогласован.
--
-- ЧТО ИЗМЕНЕНО (по одному выражению, остальное тело дословно равно production 2026-10-06):
--   • V_CT_ACTUAL_DAILY_LIVE: CTE post — order_date := DATE(p.created_at, 'Europe/Moscow'). Дальше по той же
--     колонке идут продажи, дата себестоимости и затраты по posting_number: отправление едет целиком.
--   • V_CT_PLAN_VS_ACTUAL_DAILY: data_as_of Ozon — граница is_past для бизнес-суток d фактов, поэтому тоже МСК.
-- ЧТО НЕ ИЗМЕНЕНО: V_CT_FRESHNESS.OZON_SALES — свежесть загрузки (возраст = CURRENT_DATE() UTC − дата, SLA 1):
--   это не экономика суток, при МСК-дате и UTC-«сегодня» возраст стал бы отрицательным. Реклама, начисления
--   только со sku, ветка WB, схема CT_ACTUAL_DAILY — без изменений.
-- Откат: sql/ozon/order_date_msk_2026-10-06/rollback_V_CT_*.sql (тела production до изменения).
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_ACTUAL_DAILY_LIVE` AS
WITH bom AS (SELECT card_sku, MAX(component_count) AS component_count, LOGICAL_OR(is_bundle) AS is_bundle FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_BOM_CURRENT` GROUP BY 1),
wb AS (
  SELECT d.day AS d, 'WB' AS marketplace, d.internal_sku,
    IF(d.is_bundle, 'BUNDLE', 'SOLO') AS sales_mode,
    d.orders_qty AS cards_ordered, d.canceled_qty AS cards_cancelled, d.buyouts_qty AS cards_sold, d.returns_qty AS cards_returned,
    d.orders_revenue_rub AS gmv_ordered,
    d.sales_revenue_seller_base_rub AS revenue_seller_base,
    IFNULL(d.marketplace_fee_rub, 0) + IFNULL(d.logistics_rub, 0) AS marketplace_costs,
    d.ad_spend_attributed_rub AS ad_spend,
    d.contribution_pre_cogs_rub AS seller_cash,
    c.net_product_cogs_operational_rub AS cogs,
    c.contribution_after_product_cogs_rub AS contribution,
    d.contribution_covered, c.cogs_covered, d.include_in_pnl,
    'WB: V_DASH_SKU_DAILY + V_MART_SKU_DAILY_COGS (buyout basis, attributed ads)' AS economics_basis
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_SKU_DAILY` d
  LEFT JOIN `project-fa311fc0-4d87-4781-986.wb_mart.V_MART_SKU_DAILY_COGS` c ON c.day = d.day AND c.nm_id = d.nm_id
  WHERE d.internal_sku IS NOT NULL
),
oz_map AS (SELECT marketplace_sku, internal_sku FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP` WHERE marketplace = 'OZON'),
post AS (
  SELECT p.posting_number, p.sku, m.internal_sku, p.status, DATE(p.created_at, 'Europe/Moscow') AS order_date, p.quantity, p.price_rub
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_POSTINGS_FBO` p
  JOIN oz_map m ON m.marketplace_sku = p.sku
),
fin_econ AS (
  SELECT posting_number, sku, SUM(seller_base_price_rub) AS sp_unit, SUM(commission_rub) AS comm
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_FINANCE_ACCRUAL` WHERE seller_base_price_rub IS NOT NULL GROUP BY 1, 2
),
cogs AS (SELECT internal_sku, effective_from, COALESCE(effective_to, DATE '9999-12-31') AS et, product_cogs_rub AS u
         FROM `project-fa311fc0-4d87-4781-986.evetis_ref.V_PRODUCT_COGS_EFFECTIVE`),
-- UBR-012: выручка НЕ реконструируется здесь. Каноническая модель Ozon уже различает
-- агентскую реализацию и выкуп CIS и несёт признаки недоказанности.
oz_rev AS (
  SELECT fact_date AS d, internal_sku,
    seller_base_revenue_rub AS revenue_seller_base,
    buyout_revenue_unproven_qty, buyout_revenue_unproven_rub, commission_missing_qty
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_SKU_PNL_DAILY`
),
oz_sales AS (
  SELECT p.order_date AS d, p.internal_sku,
    SUM(IF(p.status != 'cancelled', p.quantity, 0)) AS cards_ordered,
    SUM(IF(p.status = 'cancelled', p.quantity, 0)) AS cards_cancelled,
    SUM(IF(p.status = 'delivered', p.quantity, 0)) AS cards_sold,
    SUM(IF(p.status != 'cancelled', p.price_rub * p.quantity, 0)) AS gmv_ordered,
    SUM(IF(p.status = 'delivered', IFNULL(-f.comm, 0), 0)) AS commission_rub,
    SUM(IF(p.status = 'delivered', c.u * p.quantity, 0)) AS cogs,
    SUM(IF(p.status = 'delivered' AND c.u IS NULL, p.quantity, 0)) AS cogs_missing_qty
  FROM post p
  LEFT JOIN fin_econ f ON f.posting_number = p.posting_number AND f.sku = p.sku
  LEFT JOIN cogs c ON c.internal_sku = p.internal_sku AND p.order_date BETWEEN c.effective_from AND c.et
  GROUP BY 1, 2
),
oz_cost_post AS (
  SELECT pm.order_date AS d, pm.internal_sku,
    SUM(IF(f.type_id IN (32, 29, 28, 98, 30, 1, 59, 45, 78, 9, 79), -f.amount_rub, 0)) AS direct_var,
    SUM(IF(f.type_id IN (15, 71, 39, 38), -f.amount_rub, 0)) AS other_direct,
    SUM(IF(f.type_id IN (116, 74, 48), -f.amount_rub, 0)) AS promotion_billed
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_FINANCE_ACCRUAL` f
  JOIN (SELECT DISTINCT posting_number, sku, internal_sku, order_date FROM post) pm ON pm.posting_number = f.posting_number AND pm.sku = f.sku
  WHERE f.posting_number IS NOT NULL GROUP BY 1, 2
),
oz_cost_sku AS (
  SELECT f.event_date AS d, m.internal_sku,
    SUM(IF(f.type_id IN (32, 29, 28, 98, 30, 1, 59, 45, 78, 9, 79), -f.amount_rub, 0)) AS direct_var,
    SUM(IF(f.type_id IN (15, 71, 39, 38), -f.amount_rub, 0)) AS other_direct,
    SUM(IF(f.type_id IN (116, 74, 48), -f.amount_rub, 0)) AS promotion_billed
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_FINANCE_ACCRUAL` f
  JOIN oz_map m ON m.marketplace_sku = f.sku
  WHERE f.sku IS NOT NULL AND f.posting_number IS NULL GROUP BY 1, 2
),
oz_ads AS (
  SELECT a.date AS d, m.internal_sku, SUM(a.attributed_spend_rub) AS ad_spend
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_ADS_SKU_DAILY` a
  JOIN oz_map m ON m.marketplace_sku = a.sku GROUP BY 1, 2
),
oz AS (
  SELECT COALESCE(s.d, cp.d, cs.d, a.d) AS d, 'OZON' AS marketplace,
    COALESCE(s.internal_sku, cp.internal_sku, cs.internal_sku, a.internal_sku) AS internal_sku,
    IF(STARTS_WITH(COALESCE(s.internal_sku, cp.internal_sku, cs.internal_sku, a.internal_sku), 'EVT-SET-'), 'BUNDLE', 'SOLO') AS sales_mode,
    IFNULL(s.cards_ordered, 0) AS cards_ordered, IFNULL(s.cards_cancelled, 0) AS cards_cancelled, IFNULL(s.cards_sold, 0) AS cards_sold, 0 AS cards_returned,
    IFNULL(s.gmv_ordered, 0) AS gmv_ordered,
    IFNULL(r.revenue_seller_base, 0) AS revenue_seller_base,
    IFNULL(s.commission_rub, 0) + IFNULL(cp.direct_var, 0) + IFNULL(cp.other_direct, 0) + IFNULL(cs.direct_var, 0) + IFNULL(cs.other_direct, 0) AS marketplace_costs,
    IFNULL(a.ad_spend, 0) AS ad_spend,
    IFNULL(r.revenue_seller_base, 0) - (IFNULL(s.commission_rub, 0) + IFNULL(cp.direct_var, 0) + IFNULL(cp.other_direct, 0) + IFNULL(cs.direct_var, 0) + IFNULL(cs.other_direct, 0)) - IFNULL(a.ad_spend, 0) - IFNULL(cp.promotion_billed, 0) - IFNULL(cs.promotion_billed, 0) AS seller_cash,
    IFNULL(s.cogs, 0) AS cogs,
    IFNULL(r.revenue_seller_base, 0) - (IFNULL(s.commission_rub, 0) + IFNULL(cp.direct_var, 0) + IFNULL(cp.other_direct, 0) + IFNULL(cs.direct_var, 0) + IFNULL(cs.other_direct, 0)) - IFNULL(a.ad_spend, 0) - IFNULL(cp.promotion_billed, 0) - IFNULL(cs.promotion_billed, 0) - IFNULL(s.cogs, 0) AS contribution,
    IFNULL(r.buyout_revenue_unproven_qty, 0) = 0 AND IFNULL(r.commission_missing_qty, 0) = 0 AS contribution_covered,
    IFNULL(s.cogs_missing_qty, 0) = 0 AS cogs_covered, TRUE AS include_in_pnl,
    'OZON: revenue from ozon_mart.FCT_OZON_SKU_PNL_DAILY (canonical: MARKETPLACE_SALE accrual / CIS_BUYOUT primary document, no order-price fallback) + postings (order basis) for qty/GMV + finance accrual commission + direct costs by type_id + attributed ads + billed SKU promotion (types 116/74/48, L3: subtracted in seller_cash and contribution, NOT in marketplace_costs and NOT in ad_spend) (ozon_mart semantics, daily grain)' AS economics_basis
  FROM oz_sales s
  FULL JOIN oz_cost_post cp ON cp.d = s.d AND cp.internal_sku = s.internal_sku
  FULL JOIN oz_cost_sku cs ON cs.d = COALESCE(s.d, cp.d) AND cs.internal_sku = COALESCE(s.internal_sku, cp.internal_sku)
  FULL JOIN oz_ads a ON a.d = COALESCE(s.d, cp.d, cs.d) AND a.internal_sku = COALESCE(s.internal_sku, cp.internal_sku, cs.internal_sku)
  LEFT JOIN oz_rev r ON r.d = COALESCE(s.d, cp.d, cs.d, a.d)
                    AND r.internal_sku = COALESCE(s.internal_sku, cp.internal_sku, cs.internal_sku, a.internal_sku)
),
u AS (SELECT * FROM wb UNION ALL SELECT * FROM oz)
SELECT u.d, u.marketplace, u.internal_sku, u.sales_mode, IFNULL(b.component_count, 1) AS component_count,
  u.cards_ordered, u.cards_cancelled, u.cards_sold, u.cards_returned,
  u.cards_ordered * IFNULL(b.component_count, 1) AS units_ordered,
  u.cards_sold * IFNULL(b.component_count, 1) AS units_sold,
  u.gmv_ordered, u.revenue_seller_base, u.marketplace_costs, u.ad_spend, u.seller_cash, u.cogs, u.contribution,
  u.contribution_covered, u.cogs_covered, u.include_in_pnl, u.economics_basis
FROM u LEFT JOIN bom b ON b.card_sku = u.internal_sku;

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
