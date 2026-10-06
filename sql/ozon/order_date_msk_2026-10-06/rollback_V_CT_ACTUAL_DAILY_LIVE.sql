-- Откат 2026-10-06: тело production до перевода даты заказа Ozon на МСК (снято INFORMATION_SCHEMA.VIEWS).
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
  SELECT p.posting_number, p.sku, m.internal_sku, p.status, p.order_date, p.quantity, p.price_rub
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
