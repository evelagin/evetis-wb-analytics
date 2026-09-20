-- ============================================================================
-- SCALE 1 · проверки FCT_OZON_SKU_PNL_DAILY и FACT_SKU_DAILY. ТОЛЬКО ЧТЕНИЕ.
-- Каждый блок «-- @check <ID>» — один самостоятельный SELECT; ничего не создаёт и не меняет.
-- Колонка status: PASS / FAIL. Допуски: деньги ≤ 0,01 ₽, штуки — точное равенство.
--
-- После развёртывания блоки выполняются как есть. ДО развёртывания новые объекты ещё не
-- существуют — текст готовит tools/scale1_predeploy_render.py (подставляет канонические тела
-- pending_deploy-объектов вместо их имён):
--     python tools/scale1_predeploy_render.py sql/scale1/fact_sku_daily_validation.sql
-- Эталон приёмки Ozon — production ozon_mart.FCT_OZON_SKU_PNL_MONTHLY (не меняется этой задачей).
-- ============================================================================

-- @check V01_OZON_DAILY_TO_MONTHLY_SKU_PARITY
-- Суточный факт, свёрнутый до месяц × internal_sku, против месячного P&L: все месяцы, все SKU.
-- Два сравнения на каждую ячейку:
--   raw     — сумма суточных значений как есть против месячного значения; допуск денег 0,01 ₽;
--   rounded — то же после правила округления САМОГО месячного вью: компоненты ROUND(.., 2) на зерне
--             месяц × SKU, вклад считается из уже округлённых компонент. Обязано совпасть ТОЧНО.
-- Разница raw — только это округление (суточный факт хранит полную точность NUMERIC, без ROUND).
WITH d0 AS (
  SELECT DATE_TRUNC(fact_date, MONTH) month, internal_sku,
    SUM(gross_qty) gross_qty, SUM(realized_qty) realized_qty,
    SUM(seller_base_revenue_rub) revenue, SUM(product_cogs_rub) cogs, SUM(commission_rub) commission,
    SUM(direct_variable_marketplace_costs_rub) direct_var, SUM(other_direct_marketplace_costs_rub) other_direct,
    SUM(contribution_before_ads_rub) contrib_before_ads, SUM(ad_spend_attributed_rub) ads,
    SUM(contribution_after_attributed_ads_rub) contrib_after_ads,
    SUM(cogs_missing_qty) cogs_missing_qty, SUM(commission_missing_qty) commission_missing_qty
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_SKU_PNL_DAILY` x GROUP BY 1,2),
d AS (
  SELECT *, ROUND(ROUND(revenue, 2) - ROUND(cogs, 2) - ROUND(commission, 2) - ROUND(direct_var, 2) - ROUND(other_direct, 2), 2) r_contrib_before_ads,
    ROUND(ROUND(revenue, 2) - ROUND(cogs, 2) - ROUND(commission, 2) - ROUND(direct_var, 2) - ROUND(other_direct, 2) - ROUND(ads, 2), 2) r_contrib_after_ads
  FROM d0),
m AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_SKU_PNL_MONTHLY`),
cells AS (
  SELECT COALESCE(m.month, d.month) month, COALESCE(m.internal_sku, d.internal_sku) internal_sku,
    m.month IS NULL only_in_daily, d.month IS NULL only_in_monthly, c.*
  FROM m FULL JOIN d ON d.month = m.month AND d.internal_sku = m.internal_sku,
  UNNEST([
    STRUCT('gross_qty' AS metric, 'QTY' AS kind, CAST(m.gross_qty AS NUMERIC) AS monthly, CAST(d.gross_qty AS NUMERIC) AS daily, CAST(d.gross_qty AS NUMERIC) AS daily_rounded),
    ('realized_qty', 'QTY', CAST(m.realized_qty AS NUMERIC), CAST(d.realized_qty AS NUMERIC), CAST(d.realized_qty AS NUMERIC)),
    ('seller_base_revenue_rub', 'RUB', m.seller_base_revenue_rub, d.revenue, ROUND(d.revenue, 2)),
    ('product_cogs_rub', 'RUB', m.product_cogs_rub, d.cogs, ROUND(d.cogs, 2)),
    ('commission_rub', 'RUB', m.commission_rub, d.commission, ROUND(d.commission, 2)),
    ('direct_variable_marketplace_costs_rub', 'RUB', m.direct_variable_marketplace_costs_rub, d.direct_var, ROUND(d.direct_var, 2)),
    ('other_direct_marketplace_costs_rub', 'RUB', m.other_direct_marketplace_costs_rub, d.other_direct, ROUND(d.other_direct, 2)),
    ('contribution_before_ads_rub', 'RUB', m.contribution_before_ads_rub, d.contrib_before_ads, d.r_contrib_before_ads),
    ('ad_spend_attributed_rub', 'RUB', m.ad_spend_attributed_rub, d.ads, ROUND(d.ads, 2)),
    ('contribution_after_attributed_ads_rub', 'RUB', m.contribution_after_attributed_ads_rub, d.contrib_after_ads, d.r_contrib_after_ads),
    ('cogs_status=MISSING <=> cogs_missing_qty>0', 'QTY', CAST(IF(m.cogs_status = 'COVERED', 0, 1) AS NUMERIC), CAST(IF(d.cogs_missing_qty > 0, 1, 0) AS NUMERIC), CAST(IF(d.cogs_missing_qty > 0, 1, 0) AS NUMERIC)),
    ('commission_status=MISSING <=> commission_missing_qty>0', 'QTY', CAST(IF(m.commission_status = 'COVERED', 0, 1) AS NUMERIC), CAST(IF(d.commission_missing_qty > 0, 1, 0) AS NUMERIC), CAST(IF(d.commission_missing_qty > 0, 1, 0) AS NUMERIC))
  ]) c)
SELECT metric, kind, COUNT(*) cells, COUNT(DISTINCT month) months, COUNT(DISTINCT internal_sku) skus,
  SUM(monthly) existing_monthly_total, SUM(daily_rounded) daily_aggregated_total_rounded,
  SUM(daily_rounded) - SUM(monthly) difference_rounded,
  ROUND(SUM(daily) - SUM(monthly), 6) difference_raw_unrounded,
  ROUND(MAX(ABS(IFNULL(daily, 0) - IFNULL(monthly, 0))), 6) max_abs_cell_difference_raw,
  COUNTIF(only_in_daily OR only_in_monthly) cells_missing_on_one_side,
  COUNTIF(ABS(IFNULL(daily, 0) - IFNULL(monthly, 0)) > IF(kind = 'RUB', 0.01, 0)) cells_failed_raw,
  COUNTIF(IFNULL(daily_rounded, 0) != IFNULL(monthly, 0)) cells_failed_rounded,
  IF(COUNTIF(only_in_daily OR only_in_monthly) = 0
     AND COUNTIF(ABS(IFNULL(daily, 0) - IFNULL(monthly, 0)) > IF(kind = 'RUB', 0.01, 0)) = 0
     AND COUNTIF(IFNULL(daily_rounded, 0) != IFNULL(monthly, 0)) = 0, 'PASS', 'FAIL') status
FROM cells GROUP BY metric, kind ORDER BY kind DESC, metric;

-- @check V02_OZON_PARITY_BY_MONTH
-- Тот же паритет по месяцам (все SKU). Суточный агрегат приведён к правилу округления месячного вью
-- (см. V01), поэтому разницы обязаны быть ровно 0. d_cogs_raw — справочно, без округления.
WITH d0 AS (
  SELECT DATE_TRUNC(fact_date, MONTH) month, internal_sku, SUM(realized_qty) realized_qty,
    ROUND(SUM(seller_base_revenue_rub), 2) revenue, ROUND(SUM(product_cogs_rub), 2) cogs, SUM(product_cogs_rub) cogs_raw,
    ROUND(SUM(commission_rub), 2) + ROUND(SUM(direct_variable_marketplace_costs_rub), 2) + ROUND(SUM(other_direct_marketplace_costs_rub), 2) mp_costs,
    ROUND(SUM(ad_spend_attributed_rub), 2) ads
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_SKU_PNL_DAILY` x GROUP BY 1,2),
d AS (
  SELECT month, SUM(realized_qty) realized_qty, SUM(revenue) revenue, SUM(mp_costs) mp_costs, SUM(ads) ads,
    SUM(cogs) cogs, SUM(cogs_raw) cogs_raw, SUM(ROUND(revenue - cogs - mp_costs - ads, 2)) contrib
  FROM d0 GROUP BY 1),
m AS (
  SELECT month, SUM(realized_qty) realized_qty, SUM(seller_base_revenue_rub) revenue,
    SUM(commission_rub + direct_variable_marketplace_costs_rub + other_direct_marketplace_costs_rub) mp_costs,
    SUM(ad_spend_attributed_rub) ads, SUM(product_cogs_rub) cogs, SUM(contribution_after_attributed_ads_rub) contrib
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_SKU_PNL_MONTHLY` GROUP BY 1)
SELECT COALESCE(m.month, d.month) month, m.realized_qty monthly_qty, d.realized_qty daily_qty,
  m.revenue monthly_revenue, d.revenue - m.revenue d_revenue, m.mp_costs monthly_mp_costs, d.mp_costs - m.mp_costs d_mp_costs,
  m.ads monthly_ads, d.ads - m.ads d_ads, m.cogs monthly_cogs, d.cogs - m.cogs d_cogs, ROUND(d.cogs_raw - m.cogs, 6) d_cogs_raw,
  m.contrib monthly_contribution_after_ads, d.contrib - m.contrib d_contribution,
  IF(m.month IS NOT NULL AND d.month IS NOT NULL AND m.realized_qty = d.realized_qty AND d.revenue = m.revenue
     AND d.mp_costs = m.mp_costs AND d.ads = m.ads AND d.cogs = m.cogs AND d.contrib = m.contrib, 'PASS', 'FAIL') status
FROM m FULL JOIN d ON d.month = m.month ORDER BY 1;

-- @check V03_OZON_STORE_LEVEL_SALES_CROSSCHECK
-- Продажная сторона против P&L уровня магазина (FCT_OZON_PNL_MONTHLY). Расходы не сверяются:
-- магазинный P&L датирует ВСЕ расходы датой начисления, SKU-P&L — order_date для расходов с posting.
WITH d AS (
  SELECT DATE_TRUNC(fact_date, MONTH) month, SUM(gross_qty) gross_qty, SUM(cancelled_qty) cancelled_qty,
    SUM(in_transit_qty) in_transit_qty, SUM(realized_qty) realized_qty, SUM(seller_base_revenue_rub) revenue,
    SUM(product_cogs_rub) cogs, SUM(commission_rub) commission
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_SKU_PNL_DAILY` x GROUP BY 1),
m AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_PNL_MONTHLY` WHERE gross_ordered_qty > 0)
SELECT COUNT(*) months,
  COUNTIF(d.month IS NULL) months_missing_in_daily,
  COUNTIF(m.gross_ordered_qty != d.gross_qty) gross_qty_failed,
  COUNTIF(m.cancelled_qty != d.cancelled_qty) cancelled_qty_failed,
  COUNTIF(m.in_transit_qty != d.in_transit_qty) in_transit_qty_failed,
  COUNTIF(m.realized_qty != d.realized_qty) realized_qty_failed,
  ROUND(MAX(ABS(m.seller_base_revenue_rub - d.revenue)), 6) max_d_revenue,
  ROUND(MAX(ABS(m.product_cogs_rub - d.cogs)), 6) max_d_cogs,
  ROUND(MAX(ABS(m.commission_known_rub - d.commission)), 6) max_d_commission,
  IF(COUNTIF(d.month IS NULL) = 0
     AND COUNTIF(m.gross_ordered_qty != d.gross_qty OR m.cancelled_qty != d.cancelled_qty
                 OR m.in_transit_qty != d.in_transit_qty OR m.realized_qty != d.realized_qty) = 0
     AND MAX(GREATEST(ABS(m.seller_base_revenue_rub - d.revenue), ABS(m.product_cogs_rub - d.cogs),
                      ABS(m.commission_known_rub - d.commission))) <= 0.01, 'PASS', 'FAIL') status
FROM m LEFT JOIN d ON d.month = m.month;

-- @check V04_OZON_DAILY_GRAIN_AND_IDENTITIES
SELECT COUNT(*) row_count, COUNT(DISTINCT FORMAT('%t|%s', fact_date, internal_sku)) unique_keys,
  COUNTIF(fact_date IS NULL OR internal_sku IS NULL) null_keys,
  MIN(fact_date) min_date, MAX(fact_date) max_date, COUNT(DISTINCT internal_sku) skus,
  COUNTIF(logistics_rub + acquiring_rub + storage_rub != direct_variable_marketplace_costs_rub) decomposition_failed,
  COUNTIF(contribution_before_ads_rub != seller_base_revenue_rub - product_cogs_rub - commission_rub
          - direct_variable_marketplace_costs_rub - other_direct_marketplace_costs_rub) contribution_before_ads_failed,
  COUNTIF(contribution_after_attributed_ads_rub != contribution_before_ads_rub - ad_spend_attributed_rub) contribution_after_ads_failed,
  COUNTIF(gross_qty < cancelled_qty + in_transit_qty + realized_qty) status_split_exceeds_gross,
  COUNTIF(seller_base_revenue_rub IS NULL OR commission_rub IS NULL OR product_cogs_rub IS NULL
          OR direct_variable_marketplace_costs_rub IS NULL OR ad_spend_attributed_rub IS NULL) null_money,
  IF(COUNT(*) = COUNT(DISTINCT FORMAT('%t|%s', fact_date, internal_sku))
     AND COUNTIF(fact_date IS NULL OR internal_sku IS NULL) = 0
     AND COUNTIF(logistics_rub + acquiring_rub + storage_rub != direct_variable_marketplace_costs_rub) = 0
     AND COUNTIF(contribution_after_attributed_ads_rub != contribution_before_ads_rub - ad_spend_attributed_rub) = 0
     AND COUNTIF(seller_base_revenue_rub IS NULL OR commission_rub IS NULL OR product_cogs_rub IS NULL) = 0, 'PASS', 'FAIL') status
FROM `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_SKU_PNL_DAILY` x;

-- @check V05_PRODUCT_IDENTITY_GATE
-- До построения нейтрального факта: каждый идентификатор площадки → ровно один internal_sku.
WITH cm AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP`),
pm AS (SELECT internal_sku FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_PRODUCT_MASTER`),
wbm AS (SELECT DISTINCT nm_id, internal_sku FROM `project-fa311fc0-4d87-4781-986.wb_mart.SKU_PERFORMANCE_V2_DAILY`),
g AS (SELECT
  (SELECT COUNT(*) FROM (SELECT 1 FROM cm WHERE marketplace = 'WB' AND is_current GROUP BY marketplace_sku HAVING COUNT(*) > 1)) dup_active_wb_mappings,
  (SELECT COUNT(*) FROM (SELECT 1 FROM cm WHERE marketplace = 'OZON' AND is_current GROUP BY marketplace_sku HAVING COUNT(*) > 1)) dup_active_ozon_mappings,
  (SELECT COUNT(*) FROM (SELECT 1 FROM cm GROUP BY marketplace, marketplace_sku HAVING COUNT(DISTINCT internal_sku) > 1)) conflicting_identifier_mappings,
  (SELECT COUNT(*) FROM (SELECT 1 FROM cm GROUP BY marketplace, marketplace_sku HAVING COUNT(*) > 1)) fanout_risk_identifier_rows,
  (SELECT COUNT(*) FROM (SELECT 1 FROM cm WHERE is_current GROUP BY marketplace, internal_sku HAVING COUNT(*) > 1)) sku_with_multiple_current_rows,
  (SELECT COUNT(*) FROM wbm w LEFT JOIN cm ON cm.marketplace = 'WB' AND cm.marketplace_sku = CAST(w.nm_id AS STRING)
     WHERE cm.internal_sku IS DISTINCT FROM w.internal_sku) wb_source_vs_channel_map_conflicts,
  (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_mart.SKU_PERFORMANCE_V2_DAILY` WHERE internal_sku IS NULL) wb_rows_without_internal_sku,
  (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_POSTINGS_FBO` p LEFT JOIN cm ON cm.marketplace = 'OZON' AND cm.marketplace_sku = p.sku
     WHERE cm.internal_sku IS NULL) ozon_posting_rows_unmapped,
  (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_ADS_SKU_DAILY` a LEFT JOIN cm ON cm.marketplace = 'OZON' AND cm.marketplace_sku = a.sku
     WHERE cm.internal_sku IS NULL) ozon_ads_rows_unmapped,
  (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_FINANCE_ACCRUAL` f LEFT JOIN cm ON cm.marketplace = 'OZON' AND cm.marketplace_sku = f.sku
     WHERE f.sku IS NOT NULL AND cm.internal_sku IS NULL) ozon_finance_sku_rows_unmapped,
  (SELECT COUNT(DISTINCT internal_sku) FROM cm WHERE internal_sku NOT IN (SELECT internal_sku FROM pm)) mapped_sku_not_in_product_master)
SELECT g.*, IF(dup_active_wb_mappings + dup_active_ozon_mappings + conflicting_identifier_mappings + fanout_risk_identifier_rows
  + sku_with_multiple_current_rows + wb_source_vs_channel_map_conflicts + wb_rows_without_internal_sku
  + ozon_posting_rows_unmapped + ozon_ads_rows_unmapped + ozon_finance_sku_rows_unmapped + mapped_sku_not_in_product_master = 0,
  'PASS', 'FAIL') status FROM g;

-- @check V06_NEUTRAL_GRAIN_AND_CONTRACT
SELECT COUNT(*) row_count, COUNT(DISTINCT FORMAT('%t|%s|%s', fact_date, marketplace, internal_sku)) unique_keys,
  COUNT(*) - COUNT(DISTINCT FORMAT('%t|%s|%s', fact_date, marketplace, internal_sku)) duplicate_keys,
  COUNTIF(fact_date IS NULL OR marketplace IS NULL OR internal_sku IS NULL) null_keys,
  COUNTIF(marketplace NOT IN ('WB', 'OZON')) unexpected_marketplace_rows,
  COUNTIF(product_name IS NULL OR is_bundle IS NULL) rows_not_in_product_master,
  COUNTIF(marketplace_sku IS NULL) rows_without_marketplace_sku,
  COUNTIF(marketplace = 'WB') wb_rows, COUNTIF(marketplace = 'OZON') ozon_rows,
  MIN(IF(marketplace = 'WB', fact_date, NULL)) wb_min_date, MAX(IF(marketplace = 'WB', fact_date, NULL)) wb_max_date,
  MIN(IF(marketplace = 'OZON', fact_date, NULL)) ozon_min_date, MAX(IF(marketplace = 'OZON', fact_date, NULL)) ozon_max_date,
  COUNT(DISTINCT IF(marketplace = 'WB', internal_sku, NULL)) wb_skus, COUNT(DISTINCT IF(marketplace = 'OZON', internal_sku, NULL)) ozon_skus,
  COUNTIF(contract_version != 'FACT_SKU_DAILY_V1' OR fact_date_semantics IS NULL OR source_contract IS NULL) contract_metadata_failed,
  IF(COUNT(*) = COUNT(DISTINCT FORMAT('%t|%s|%s', fact_date, marketplace, internal_sku))
     AND COUNTIF(fact_date IS NULL OR marketplace IS NULL OR internal_sku IS NULL) = 0
     AND COUNTIF(marketplace NOT IN ('WB', 'OZON')) = 0
     AND COUNTIF(product_name IS NULL OR is_bundle IS NULL) = 0
     AND COUNTIF(contract_version != 'FACT_SKU_DAILY_V1' OR fact_date_semantics IS NULL OR source_contract IS NULL) = 0, 'PASS', 'FAIL') status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.FACT_SKU_DAILY` x;

-- @check V07_WB_ADAPTER_PARITY
-- Срез WB нейтрального факта против авторитетного wb_mart.SKU_PERFORMANCE_V2_DAILY: построчно
-- (IS DISTINCT FROM — NULL обязан остаться NULL) и итогами. Итоги дополнительно против V_DASH-вью.
WITH n AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.evetis_mart.FACT_SKU_DAILY` x WHERE marketplace = 'WB'),
s AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.SKU_PERFORMANCE_V2_DAILY`),
v AS (SELECT SUM(fin_seller_price_rub) revenue, SUM(contribution_before_cogs_rub) c_ads, SUM(contribution_after_cogs_rub) c_cogs,
        SUM(orders_gross_units) orders_qty FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_SKU_PERFORMANCE_V2_DAILY`),
cells AS (
  SELECT c.* FROM s FULL JOIN n ON n.fact_date = s.day AND n.internal_sku = s.internal_sku,
  UNNEST([
    STRUCT('row present on both sides' AS metric, CAST(IF(s.day IS NULL, NULL, 1) AS NUMERIC) AS source, CAST(IF(n.fact_date IS NULL, NULL, 1) AS NUMERIC) AS neutral),
    ('orders_qty', CAST(s.orders_gross_units AS NUMERIC), CAST(n.orders_qty AS NUMERIC)),
    ('cancelled_qty', CAST(s.orders_cancelled_units AS NUMERIC), CAST(n.cancelled_qty AS NUMERIC)),
    ('sold_qty', CAST(s.fin_sale_units AS NUMERIC), CAST(n.sold_qty AS NUMERIC)),
    ('return_qty', CAST(s.fin_return_units AS NUMERIC), CAST(n.return_qty AS NUMERIC)),
    ('seller_revenue_rub', s.fin_seller_price_rub, n.seller_revenue_rub),
    ('marketplace_commission_rub', s.fin_seller_price_rub - s.credited_for_goods_rub, n.marketplace_commission_rub),
    ('logistics_rub', s.logistics_rub, n.logistics_rub),
    ('storage_rub', s.storage_sku_rub, n.storage_rub),
    ('advertising_attributed_rub', s.ads_attributed_rub, n.advertising_attributed_rub),
    ('contribution_after_ads_rub', s.contribution_before_cogs_rub, n.contribution_after_ads_rub),
    ('cogs_rub', s.cogs_rub, n.cogs_rub),
    ('contribution_after_cogs_rub', s.contribution_after_cogs_rub, n.contribution_after_cogs_rub)
  ]) c)
SELECT metric, COUNT(*) cells, SUM(source) wb_authoritative_total, SUM(neutral) neutral_total,
  IFNULL(SUM(neutral), 0) - IFNULL(SUM(source), 0) total_difference,
  COUNTIF(source IS DISTINCT FROM neutral) cells_failed,
  CASE metric WHEN 'seller_revenue_rub' THEN (SELECT revenue FROM v) WHEN 'contribution_after_ads_rub' THEN (SELECT c_ads FROM v)
    WHEN 'contribution_after_cogs_rub' THEN (SELECT c_cogs FROM v) WHEN 'orders_qty' THEN CAST((SELECT orders_qty FROM v) AS NUMERIC) END v_dash_view_total,
  IF(COUNTIF(source IS DISTINCT FROM neutral) = 0, 'PASS', 'FAIL') status
FROM cells GROUP BY metric ORDER BY metric;

-- @check V08_OZON_ADAPTER_PARITY
WITH n AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.evetis_mart.FACT_SKU_DAILY` x WHERE marketplace = 'OZON'),
s AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_SKU_PNL_DAILY` x),
cells AS (
  SELECT c.* FROM s FULL JOIN n ON n.fact_date = s.fact_date AND n.internal_sku = s.internal_sku,
  UNNEST([
    STRUCT('row present on both sides' AS metric, CAST(IF(s.fact_date IS NULL, NULL, 1) AS NUMERIC) AS source, CAST(IF(n.fact_date IS NULL, NULL, 1) AS NUMERIC) AS neutral),
    ('orders_qty', CAST(s.gross_qty AS NUMERIC), CAST(n.orders_qty AS NUMERIC)),
    ('cancelled_qty', CAST(s.cancelled_qty AS NUMERIC), CAST(n.cancelled_qty AS NUMERIC)),
    ('sold_qty', CAST(s.realized_qty AS NUMERIC), CAST(n.sold_qty AS NUMERIC)),
    ('seller_revenue_rub', s.seller_base_revenue_rub, n.seller_revenue_rub),
    ('marketplace_commission_rub', s.commission_rub, n.marketplace_commission_rub),
    ('logistics_rub', s.logistics_rub, n.logistics_rub),
    ('storage_rub', s.storage_rub, n.storage_rub),
    ('acquiring_rub', s.acquiring_rub, n.acquiring_rub),
    ('other_marketplace_costs_rub', s.other_direct_marketplace_costs_rub, n.other_marketplace_costs_rub),
    ('marketplace_costs_total_rub', s.commission_rub + s.direct_variable_marketplace_costs_rub + s.other_direct_marketplace_costs_rub, n.marketplace_costs_total_rub),
    ('advertising_attributed_rub', s.ad_spend_attributed_rub, n.advertising_attributed_rub),
    ('cogs_rub (NULL if COGS unresolved)', IF(s.cogs_missing_qty = 0, s.product_cogs_rub, NULL), n.cogs_rub),
    ('contribution_after_cogs_rub (NULL if COGS unresolved)', IF(s.cogs_missing_qty = 0, s.contribution_after_attributed_ads_rub, NULL), n.contribution_after_cogs_rub)
  ]) c)
SELECT metric, COUNT(*) cells, SUM(source) ozon_daily_total, SUM(neutral) neutral_total,
  IFNULL(SUM(neutral), 0) - IFNULL(SUM(source), 0) total_difference,
  COUNTIF(source IS DISTINCT FROM neutral) cells_failed,
  IF(COUNTIF(source IS DISTINCT FROM neutral) = 0, 'PASS', 'FAIL') status
FROM cells GROUP BY metric ORDER BY metric;

-- @check V09_NEUTRAL_IDENTITIES_AND_NULL_CONTRACT
SELECT marketplace, COUNT(*) row_count,
  COUNTIF(marketplace_costs_total_rub IS NOT NULL AND marketplace_costs_total_rub != marketplace_commission_rub
    + IFNULL(logistics_rub, 0) + IFNULL(storage_rub, 0) + IFNULL(acquiring_rub, 0) + IFNULL(other_marketplace_costs_rub, 0)) costs_total_identity_failed,
  COUNTIF(contribution_after_ads_rub IS NOT NULL
    AND contribution_after_ads_rub != seller_revenue_rub - marketplace_costs_total_rub - advertising_attributed_rub) contribution_after_ads_identity_failed,
  COUNTIF(contribution_after_cogs_rub IS NOT NULL
    AND contribution_after_cogs_rub != contribution_after_ads_rub - cogs_rub) contribution_after_cogs_identity_failed,
  COUNTIF(economics_covered AND contribution_after_cogs_rub IS NULL) covered_but_null_contribution,
  COUNTIF(marketplace = 'WB' AND (acquiring_rub IS NOT NULL OR other_marketplace_costs_rub IS NOT NULL)) wb_unsupported_not_null,
  COUNTIF(marketplace = 'OZON' AND return_qty IS NOT NULL) ozon_unsupported_not_null,
  COUNTIF(marketplace = 'WB' AND fact_date < DATE '2026-09-01' AND storage_rub IS NOT NULL) wb_storage_before_coverage_not_null,
  COUNTIF(marketplace = 'OZON' AND (seller_revenue_rub IS NULL OR marketplace_costs_total_rub IS NULL OR advertising_attributed_rub IS NULL
    OR orders_qty IS NULL OR sold_qty IS NULL)) ozon_observed_metric_null,
  COUNTIF(orders_qty IS NULL) orders_qty_null_rows, COUNTIF(seller_revenue_rub IS NULL) revenue_null_rows,
  COUNTIF(cogs_rub IS NULL) cogs_null_rows, COUNTIF(NOT economics_covered) not_covered_rows, COUNTIF(is_provisional) provisional_rows,
  IF(COUNTIF(marketplace_costs_total_rub IS NOT NULL AND marketplace_costs_total_rub != marketplace_commission_rub
       + IFNULL(logistics_rub, 0) + IFNULL(storage_rub, 0) + IFNULL(acquiring_rub, 0) + IFNULL(other_marketplace_costs_rub, 0)) = 0
     AND COUNTIF(contribution_after_ads_rub IS NOT NULL
       AND contribution_after_ads_rub != seller_revenue_rub - marketplace_costs_total_rub - advertising_attributed_rub) = 0
     AND COUNTIF(contribution_after_cogs_rub IS NOT NULL AND contribution_after_cogs_rub != contribution_after_ads_rub - cogs_rub) = 0
     AND COUNTIF(economics_covered AND contribution_after_cogs_rub IS NULL) = 0
     AND COUNTIF(marketplace = 'WB' AND (acquiring_rub IS NOT NULL OR other_marketplace_costs_rub IS NOT NULL)) = 0
     AND COUNTIF(marketplace = 'OZON' AND return_qty IS NOT NULL) = 0
     AND COUNTIF(marketplace = 'WB' AND fact_date < DATE '2026-09-01' AND storage_rub IS NOT NULL) = 0
     AND COUNTIF(marketplace = 'OZON' AND (seller_revenue_rub IS NULL OR marketplace_costs_total_rub IS NULL
       OR advertising_attributed_rub IS NULL OR orders_qty IS NULL OR sold_qty IS NULL)) = 0, 'PASS', 'FAIL') status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.FACT_SKU_DAILY` x GROUP BY marketplace ORDER BY marketplace;

-- @check V10_COMBINED_ARITHMETIC
-- Итог двух площадок = сумма срезов (площадка — часть ключа, дедупликация не нужна).
WITH f AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.evetis_mart.FACT_SKU_DAILY` x
           WHERE fact_date BETWEEN DATE '2026-06-01' AND DATE '2026-08-31'),
p AS (SELECT FORMAT_DATE('%Y-%m', fact_date) period, marketplace, SUM(orders_qty) orders_qty, SUM(sold_qty) sold_qty,
        SUM(seller_revenue_rub) revenue, SUM(contribution_after_ads_rub) c_ads, SUM(contribution_after_cogs_rub) c_cogs
      FROM f GROUP BY 1,2),
t AS (SELECT FORMAT_DATE('%Y-%m', fact_date) period, SUM(orders_qty) orders_qty, SUM(sold_qty) sold_qty,
        SUM(seller_revenue_rub) revenue, SUM(contribution_after_ads_rub) c_ads, SUM(contribution_after_cogs_rub) c_cogs
      FROM f GROUP BY 1)
SELECT t.period, wb.orders_qty wb_orders, oz.orders_qty ozon_orders, t.orders_qty total_orders,
  wb.sold_qty wb_sold, oz.sold_qty ozon_sold, t.sold_qty total_sold,
  ROUND(wb.revenue, 2) wb_revenue, ROUND(oz.revenue, 2) ozon_revenue, ROUND(t.revenue, 2) total_revenue,
  ROUND(wb.c_cogs, 2) wb_contribution_after_cogs, ROUND(oz.c_cogs, 2) ozon_contribution_after_cogs, ROUND(t.c_cogs, 2) total_contribution_after_cogs,
  IF(t.orders_qty = wb.orders_qty + oz.orders_qty AND t.sold_qty = wb.sold_qty + oz.sold_qty
     AND t.revenue = wb.revenue + oz.revenue AND t.c_ads = wb.c_ads + oz.c_ads AND t.c_cogs = wb.c_cogs + oz.c_cogs, 'PASS', 'FAIL') status
FROM t JOIN p wb ON wb.period = t.period AND wb.marketplace = 'WB'
       JOIN p oz ON oz.period = t.period AND oz.marketplace = 'OZON' ORDER BY 1;

-- @check V11_TRACE_SKUS_AUGUST_2026
-- HAND / MOIST / ACNE, август 2026: авторитетный источник → нейтральный факт, обе площадки.
WITH n AS (
  SELECT internal_sku, marketplace, SUM(orders_qty) orders_qty, SUM(sold_qty) sold_qty, SUM(seller_revenue_rub) revenue,
    SUM(marketplace_costs_total_rub) mp_costs, SUM(advertising_attributed_rub) ads, SUM(cogs_rub) cogs,
    SUM(contribution_after_cogs_rub) contribution_after_cogs
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.FACT_SKU_DAILY` x
  WHERE fact_date BETWEEN DATE '2026-08-01' AND DATE '2026-08-31'
    AND internal_sku IN ('EVT-HC-HAND-300', 'EVT-FS-MOIST-30', 'EVT-FS-ACNE-30') GROUP BY 1,2),
src AS (
  SELECT internal_sku, 'WB' marketplace, SUM(orders_gross_units) orders_qty, SUM(fin_sale_units) sold_qty, SUM(fin_seller_price_rub) revenue,
    SUM(fin_seller_price_rub - credited_for_goods_rub + logistics_rub + IFNULL(storage_sku_rub, 0)) mp_costs,
    SUM(ads_attributed_rub) ads, SUM(cogs_rub) cogs, SUM(contribution_after_cogs_rub) contribution_after_cogs
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.SKU_PERFORMANCE_V2_DAILY`
  WHERE day BETWEEN DATE '2026-08-01' AND DATE '2026-08-31'
    AND internal_sku IN ('EVT-HC-HAND-300', 'EVT-FS-MOIST-30', 'EVT-FS-ACNE-30') GROUP BY 1
  UNION ALL
  SELECT internal_sku, 'OZON', gross_qty, realized_qty, seller_base_revenue_rub,
    commission_rub + direct_variable_marketplace_costs_rub + other_direct_marketplace_costs_rub,
    ad_spend_attributed_rub, product_cogs_rub, contribution_after_attributed_ads_rub
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_SKU_PNL_MONTHLY`
  WHERE month = DATE '2026-08-01' AND internal_sku IN ('EVT-HC-HAND-300', 'EVT-FS-MOIST-30', 'EVT-FS-ACNE-30'))
SELECT src.internal_sku, src.marketplace, src.orders_qty src_orders, n.orders_qty neutral_orders, src.sold_qty src_sold, n.sold_qty neutral_sold,
  ROUND(src.revenue, 2) src_revenue, ROUND(n.revenue, 2) neutral_revenue, ROUND(src.mp_costs, 2) src_mp_costs, ROUND(n.mp_costs, 2) neutral_mp_costs,
  ROUND(src.ads, 2) src_ads, ROUND(n.ads, 2) neutral_ads, ROUND(src.cogs, 2) src_cogs, ROUND(n.cogs, 2) neutral_cogs,
  ROUND(src.contribution_after_cogs, 2) src_contribution_after_cogs, ROUND(n.contribution_after_cogs, 2) neutral_contribution_after_cogs,
  IF(src.orders_qty = n.orders_qty AND src.sold_qty = n.sold_qty
     AND GREATEST(ABS(src.revenue - n.revenue), ABS(src.mp_costs - n.mp_costs), ABS(src.ads - n.ads), ABS(src.cogs - n.cogs),
                  ABS(src.contribution_after_cogs - n.contribution_after_cogs)) <= 0.01, 'PASS', 'FAIL') status
FROM src LEFT JOIN n USING (internal_sku, marketplace) ORDER BY 1, 2;
