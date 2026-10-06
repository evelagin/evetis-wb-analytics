-- =====================================================================================
-- CONTROL TOWER PHASE 1 — БАЗОВЫЕ ВИТРИНЫ (wb_mart.V_CT_*)
-- =====================================================================================
-- Слой «сырьё Control Tower»: BOM, активный план, факт продаж, физические единицы,
-- движение поставок, правда о запасе, свежесть источников.
-- Все объекты — НОВЫЕ, префикс V_CT_. Ни один production-объект не изменяется.
-- Порядок применения: ct_01 → ct_02 → ct_03 → ct_04a → ct_04b → ct_05.
-- Откат: tools/ct_phase1_rollback.sh
-- =====================================================================================

-- ── BOM: карточка → компоненты. Соло-SKU раскрывается сам в себя (qty 1) ─────────────
-- Grain: card_sku × component_sku. Источник: REF_BUNDLE_COMPONENTS + REF_PRODUCT_MASTER.
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_BOM_CURRENT` AS
WITH comp AS (
  SELECT bundle_internal_sku AS card_sku, component_internal_sku AS component_sku, component_qty
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_BUNDLE_COMPONENTS`
  WHERE effective_to IS NULL OR effective_to >= CURRENT_DATE()
),
base AS (
  SELECT internal_sku AS card_sku, internal_sku AS component_sku, 1 AS component_qty
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_PRODUCT_MASTER` WHERE NOT is_bundle
)
SELECT card_sku, component_sku, component_qty,
       SUM(component_qty) OVER (PARTITION BY card_sku) AS component_count,
       card_sku != component_sku AS is_bundle
FROM (SELECT * FROM comp UNION ALL SELECT * FROM base);

-- ── Активная версия плана ───────────────────────────────────────────────────────────
-- target_physical_units — величина из seed-артефакта (округлена до 0,1).
-- target_physical_units_bom — пересчёт из BOM; расхождение ≤ 0,25 фл. на строку месяца.
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_PLAN_ACTIVE` AS
SELECT p.* EXCEPT(plan_status), v.plan_status, v.plan_name, v.scenario_code, v.horizon_from, v.horizon_to,
  p.target_cards * p.component_count AS target_physical_units_bom,
  p.target_physical_units - p.target_cards * p.component_count AS target_physical_units_rounding_delta
FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_SEASON_PLAN` p
JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.CT_PLAN_VERSION` v ON v.plan_version = p.plan_version
WHERE v.plan_status = 'ACTIVE';

-- ── Факт продаж (LIVE, считается из production при каждом обращении) ────────────────
-- Grain: date × marketplace × internal_sku × sales_mode.
-- СЕМАНТИКА (контракт проекта, не меняется):
--   cards_ordered  — заказы без отмен, по дате заказа
--   cards_sold     — WB: выкупы по дате продажи; OZON: статус delivered
--   seller_cash    — деньги продавца = выручка по базе продавца − комиссии/логистика − реклама
--   contribution   — вклад = seller_cash − COGS проданного. Это НЕ чистая прибыль.
--   ad_spend       — атрибутированная реклама (не биллинг). V_ADV_COSTS не затрагивается.
-- WB берёт готовую экономику из V_DASH_SKU_DAILY + V_MART_SKU_DAILY_COGS.
-- OZON собирает дневное зерно по семантике ozon_mart: постинги + accrual + type_id-затраты.
-- ⚠️ Тело синхронизировано с production 2026-09-22: включает UBR-010 (promotion_billed, L3)
-- и UBR-012 (выручка Ozon из ozon_mart.FCT_OZON_SKU_PNL_DAILY, без fallback на цену заказа).
-- Патч-файлы ct_promotion_l3_2026-09-22.sql и ct_ubr012_revenue_2026-09-22.sql — историческая
-- запись изменений; повторный прогон ЭТОГО файла больше не откатывает оба решения владельца.
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

-- ── Факт продаж (материализованный, читают все дашборды) ────────────────────────────
-- Наполняется evetis_ref.sp_ct_refresh_daily(); дашборды не пересчитывают тяжёлый LIVE.
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_ACTUAL_DAILY` AS
SELECT d, marketplace, internal_sku, sales_mode, component_count, cards_ordered, cards_cancelled, cards_sold, cards_returned, units_ordered, units_sold,
  gmv_ordered, revenue_seller_base, marketplace_costs, ad_spend, seller_cash, cogs, contribution, contribution_covered, cogs_covered, include_in_pnl, economics_basis
FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_ACTUAL_DAILY`;

-- ── Физические единицы: карточки разложены по BOM на компоненты ─────────────────────
-- Двойного счёта нет: набор учитывается один раз как карточка и один раз как N флаконов
-- своих компонентов; тест T8 в ct_phase1_validation.sql сверяет суммы.
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_PHYSICAL_DAILY` AS
SELECT a.d, a.marketplace, b.component_sku AS internal_sku,
  SUM(IF(a.sales_mode = 'SOLO', a.cards_ordered * b.component_qty, 0)) AS units_ordered_solo,
  SUM(IF(a.sales_mode = 'BUNDLE', a.cards_ordered * b.component_qty, 0)) AS units_ordered_via_bundle,
  SUM(a.cards_ordered * b.component_qty) AS units_ordered,
  SUM(IF(a.sales_mode = 'SOLO', a.cards_sold * b.component_qty, 0)) AS units_sold_solo,
  SUM(IF(a.sales_mode = 'BUNDLE', a.cards_sold * b.component_qty, 0)) AS units_sold_via_bundle,
  SUM(a.cards_sold * b.component_qty) AS units_sold,
  SUM(IF(a.sales_mode = 'BUNDLE', a.cards_ordered, 0)) AS bundle_cards_ordered_containing_sku,
  SUM(a.cards_cancelled * b.component_qty) AS units_cancelled
FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_ACTUAL_DAILY` a
JOIN `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_BOM_CURRENT` b ON b.card_sku = a.internal_sku
GROUP BY 1, 2, 3;

-- ── Движение поставок относительно среза ФФ ─────────────────────────────────────────
-- Разделяет «наряды эпохи среза» (созданы до среза) и созданные после него, чтобы не
-- вычесть из ФФ одно и то же дважды. Ozon: RAW_OZON_SUPPLY_ORDERS/BUNDLES, WB: V_WB_SUPPLIES_*.
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_SUPPLY_FLOW` AS
WITH snap AS (SELECT MAX(snapshot_date) AS snapshot_date FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_STOCK_SNAPSHOT`),
bom AS (SELECT card_sku, component_sku, component_qty FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_BOM_CURRENT`),
oz_map AS (SELECT marketplace_sku, internal_sku FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP` WHERE marketplace = 'OZON'),
wb_map AS (SELECT CAST(marketplace_sku AS INT64) AS nm_id, internal_sku FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP` WHERE marketplace = 'WB'),
oo AS (
  SELECT order_id, state, DATE(created_at) AS created_date, DATE(state_updated_at) AS state_date FROM (
    SELECT order_id, state, created_at, state_updated_at, ROW_NUMBER() OVER (PARTITION BY order_id ORDER BY extracted_at DESC) AS rn
    FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_SUPPLY_ORDERS`) WHERE rn = 1
),
ob AS (
  SELECT order_id, sku, quantity_planned, quantity_accepted FROM (
    SELECT order_id, sku, quantity_planned, quantity_accepted, ROW_NUMBER() OVER (PARTITION BY bundle_id, supply_id, sku ORDER BY extracted_at DESC) AS rn
    FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_SUPPLY_BUNDLES`) WHERE rn = 1
),
oz_units AS (
  SELECT b.component_sku AS internal_sku, oo.state,
    oo.created_date <= s.snapshot_date AS created_before_snap,
    (oo.state NOT IN ('COMPLETED', 'CANCELLED')) OR (oo.state_date > s.snapshot_date) AS open_at_snap_or_after,
    SUM(ob.quantity_planned * b.component_qty) AS planned_units
  FROM oo JOIN ob ON ob.order_id = oo.order_id
  JOIN oz_map m ON m.marketplace_sku = ob.sku
  JOIN bom b ON b.card_sku = m.internal_sku
  CROSS JOIN snap s
  GROUP BY 1, 2, 3, 4
),
oz AS (
  SELECT internal_sku,
    SUM(IF(created_before_snap AND open_at_snap_or_after AND state = 'COMPLETED', planned_units, 0)) AS ozon_seed_era_completed_units,
    SUM(IF(created_before_snap AND open_at_snap_or_after AND state = 'CANCELLED', planned_units, 0)) AS ozon_seed_era_cancelled_units,
    SUM(IF(created_before_snap AND state NOT IN ('COMPLETED', 'CANCELLED'), planned_units, 0)) AS ozon_seed_era_open_units,
    SUM(IF(NOT created_before_snap AND state NOT IN ('COMPLETED', 'CANCELLED'), planned_units, 0)) AS ozon_post_snapshot_open_units,
    SUM(IF(NOT created_before_snap AND state = 'COMPLETED', planned_units, 0)) AS ozon_post_snapshot_completed_units
  FROM oz_units GROUP BY 1
),
wbs AS (
  SELECT g.nm_id, s.is_completed, DATE(s.create_dt) AS created_date, g.quantity, g.accepted_quantity
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_SUPPLIES_GOODS_CURRENT` g
  JOIN `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_SUPPLIES_CURRENT` s ON s.supply_id = g.supply_id
  WHERE s.supply_dt IS NOT NULL AND (s.reject_reason IS NULL OR s.reject_reason = '')
),
wb AS (
  SELECT b.component_sku AS internal_sku,
    SUM(IF(w.created_date > sn.snapshot_date AND NOT IFNULL(w.is_completed, FALSE), w.quantity * b.component_qty, 0)) AS wb_post_snapshot_in_transit_units,
    SUM(IF(w.created_date > sn.snapshot_date AND IFNULL(w.is_completed, FALSE), IFNULL(w.accepted_quantity, w.quantity) * b.component_qty, 0)) AS wb_post_snapshot_accepted_units
  FROM wbs w JOIN wb_map m ON m.nm_id = w.nm_id JOIN bom b ON b.card_sku = m.internal_sku CROSS JOIN snap sn
  GROUP BY 1
)
SELECT COALESCE(oz.internal_sku, wb.internal_sku) AS internal_sku, (SELECT snapshot_date FROM snap) AS snapshot_date,
  IFNULL(oz.ozon_seed_era_completed_units, 0) AS ozon_seed_era_completed_units,
  IFNULL(oz.ozon_seed_era_cancelled_units, 0) AS ozon_seed_era_cancelled_units,
  IFNULL(oz.ozon_seed_era_open_units, 0) AS ozon_seed_era_open_units,
  IFNULL(oz.ozon_post_snapshot_open_units, 0) AS ozon_post_snapshot_open_units,
  IFNULL(oz.ozon_post_snapshot_completed_units, 0) AS ozon_post_snapshot_completed_units,
  IFNULL(wb.wb_post_snapshot_in_transit_units, 0) AS wb_post_snapshot_in_transit_units,
  IFNULL(wb.wb_post_snapshot_accepted_units, 0) AS wb_post_snapshot_accepted_units
FROM oz FULL OUTER JOIN wb ON wb.internal_sku = oz.internal_sku;

-- ── Правда о запасе (материализованный дневной срез) ────────────────────────────────
-- Читает партицию CT_INVENTORY_SNAPSHOT_DAILY за последнюю дату; сам срез пишет
-- sp_ct_refresh_daily() из V_CT_INVENTORY_TRUTH_LIVE (определение — ct_04b).
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_INVENTORY_TRUTH` AS
SELECT * EXCEPT(snapshot_date, snapshot_ts), snapshot_date AS ct_snapshot_date, snapshot_ts AS ct_snapshot_ts
FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_INVENTORY_SNAPSHOT_DAILY`
WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_INVENTORY_SNAPSHOT_DAILY`);

-- ── Свежесть источников ─────────────────────────────────────────────────────────────
-- Grain: domain. status = OK | STALE | ERROR по возрасту относительно sla_days.
-- Owner Home обязан показывать STALE как STALE — устаревшее не выдаётся за текущее.
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_FRESHNESS` AS
WITH d AS (
  SELECT 'WB_SALES' AS domain, 'Заказы и выкупы WB' AS domain_ru, (SELECT MAX(day) FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_SKU_DAILY`) AS data_as_of, CAST(NULL AS TIMESTAMP) AS data_as_of_ts, 1 AS sla_days, 'wb_mart.V_DASH_SKU_DAILY' AS source, 1 AS ord
  UNION ALL SELECT 'OZON_SALES', 'Заказы Ozon (постинги)', (SELECT MAX(order_date) FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_POSTINGS_FBO`), NULL, 1, 'ozon_raw.RAW_OZON_POSTINGS_FBO', 2
  UNION ALL SELECT 'WB_STOCK', 'Остатки WB', (SELECT MAX(_snapshot_date) FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_STOCKS_T5_CURRENT`), (SELECT MAX(snapshot_ts) FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_STOCKS_T5_CURRENT`), 1, 'wb_raw.V_WB_STOCKS_T5_CURRENT', 3
  UNION ALL SELECT 'OZON_STOCK', 'Остатки Ozon', (SELECT MAX(snapshot_date) FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_STOCKS`), (SELECT MAX(extracted_at) FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_STOCKS`), 1, 'ozon_raw.RAW_OZON_STOCKS', 4
  UNION ALL SELECT 'FF_STOCK', 'Остатки фулфилмента (срез владельца)', (SELECT MAX(snapshot_date) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_STOCK_SNAPSHOT`), NULL, 7, 'evetis_ref.CT_STOCK_SNAPSHOT', 5
  UNION ALL SELECT 'WB_ADS', 'Реклама WB (attributed)', (SELECT MAX(day) FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_SKU_DAILY` WHERE ads_covered), NULL, 2, 'wb_mart.FACT_ADS_SKU_DAILY', 6
  UNION ALL SELECT 'OZON_ADS', 'Реклама Ozon (кабинет)', (SELECT MAX(date) FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_ADS_EXPENSE_DAILY`), NULL, 2, 'ozon_raw.RAW_OZON_ADS_EXPENSE_DAILY', 7
  UNION ALL SELECT 'WB_FINANCE', 'Финансы WB (отчёты)', (SELECT MAX(data_as_of) FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DATA_FRESHNESS` WHERE layer_code = 'finance'), NULL, 9, 'wb_raw.V_WB_FINANCE_CANONICAL', 8
  UNION ALL SELECT 'CT_ACTUALS', 'Control Tower: витрина продаж (обновление)', (SELECT DATE(MAX(refreshed_at)) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_ACTUAL_DAILY`), (SELECT MAX(refreshed_at) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_ACTUAL_DAILY`), 1, 'evetis_ref.CT_ACTUAL_DAILY (sp_ct_refresh_daily)', 9
  UNION ALL SELECT 'CT_INVENTORY', 'Control Tower: срез запасов (обновление)', (SELECT MAX(snapshot_date) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_INVENTORY_SNAPSHOT_DAILY`), (SELECT MAX(snapshot_ts) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_INVENTORY_SNAPSHOT_DAILY`), 1, 'evetis_ref.CT_INVENTORY_SNAPSHOT_DAILY (sp_ct_refresh_daily)', 10
  UNION ALL SELECT 'CT_ACTIONS', 'Control Tower: очередь действий (правила)', (SELECT DATE(MAX(run_ts)) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_REFRESH_LOG` WHERE step = 'sp_ct_generate_actions' AND status = 'OK'), (SELECT MAX(run_ts) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_REFRESH_LOG` WHERE step = 'sp_ct_generate_actions' AND status = 'OK'), 1, 'evetis_ref.CT_REFRESH_LOG (sp_ct_generate_actions)', 11
)
SELECT domain, domain_ru, data_as_of, data_as_of_ts, DATE_DIFF(CURRENT_DATE(), data_as_of, DAY) AS age_days, sla_days,
  IF(data_as_of IS NULL, 'ERROR', IF(DATE_DIFF(CURRENT_DATE(), data_as_of, DAY) > sla_days, 'STALE', 'OK')) AS status,
  source, ord AS sort_order, CURRENT_TIMESTAMP() AS generated_at
FROM d;
