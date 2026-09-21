-- ============================================================================
-- CANONICAL CURRENT DEFINITION — ozon_mart.FCT_OZON_PNL_MONTHLY (VIEW)
-- Authoritative Git definition of the CURRENT production object. Not a migration,
-- not a rollback. Rules: sql/current/README.md. Metadata: MANIFEST.json.
-- Captured verbatim from production INFORMATION_SCHEMA.VIEWS at 2026-09-18T14:14:32Z
-- (main eecde14936d1). Historical source: sql/ozon/stage3_4c_ozon_mart.sql (parity: COMMENTS_WHITESPACE_ONLY).
-- Тип операции (Gate 5K): MARKETPLACE_SALE (агентская реализация) и CIS_BUYOUT (выкуп товара
-- Ozon у продавца, Беларусь). У выкупа агентского вознаграждения не существует как факта —
-- комиссия не MISSING, а неприменима; выручка выкупа равна сумме по первичному документу
-- (ozon_mart.V_OZON_CIS_BUYOUT). Прежний инлайн-CTE rec_comm (29 строк «восстановленной
-- комиссии») удалён: записанные в нём суммы были «Дисконтом по категории» из документа о
-- выкупе, а не вознаграждением. Семантика совпадает с FCT_OZON_SKU_PNL_DAILY.
-- Internal dependencies: V_OZON_CIS_BUYOUT.
-- The view body below is byte-for-byte the production body: do not reformat it.
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_PNL_MONTHLY`
OPTIONS (description = "Канонический P&L Ozon, зерно = календарный месяц. VIEW, не таблица: пересчитывается при каждом чтении, устареть относительно ozon_raw не может. Мост L1-L4 по OZON_PNL_POLICY_V1. Продажи и себестоимость привязаны к order_date, расходы уровня магазина - к дате начисления. Отсутствующая комиссия НЕ ноль: см. commission_missing_qty и поля uncertainty. Тип операции: MARKETPLACE_SALE и CIS_BUYOUT (выкуп товара Ozon у продавца); у выкупа комиссия неприменима, а выручка равна сумме по первичному документу.")
AS
WITH
post AS (
  SELECT p.posting_number, p.sku, p.status, p.order_date, p.quantity, p.price_rub,
         p.payout_rub, m.internal_sku
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_POSTINGS_FBO` p
  JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP` m
    ON m.marketplace='OZON' AND m.marketplace_sku=p.sku),
fin_econ AS (
  SELECT posting_number, sku, SUM(seller_base_price_rub) sp_unit, SUM(buyer_paid_price_rub) bp,
         SUM(ozon_bonus_rub) bonus, SUM(ozon_coinvestment_rub) coinv, SUM(commission_rub) comm
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_FINANCE_ACCRUAL`
  WHERE seller_base_price_rub IS NOT NULL GROUP BY 1,2),
cogs AS (SELECT internal_sku, effective_from, COALESCE(effective_to, DATE '9999-12-31') et, product_cogs_rub u
         FROM `project-fa311fc0-4d87-4781-986.evetis_ref.V_PRODUCT_COGS_EFFECTIVE`),
cls AS (
  SELECT p.*, f.sp_unit, f.comm, f.bp, f.bonus, f.coinv, b.buyout_proceeds_rub,
    CASE WHEN b.posting_number IS NOT NULL THEN 'CIS_BUYOUT'
         WHEN p.status='delivered' AND f.sp_unit IS NULL AND IFNULL(p.payout_rub, NUMERIC '0')=0
           THEN 'CIS_BUYOUT'
         ELSE 'MARKETPLACE_SALE' END op_type
  FROM post p LEFT JOIN fin_econ f USING (posting_number, sku)
  LEFT JOIN `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_CIS_BUYOUT` b
    ON b.posting_number=p.posting_number),
sales AS (
  SELECT DATE_TRUNC(p.order_date, MONTH) m, p.status, p.quantity, p.posting_number,
    CASE WHEN p.buyout_proceeds_rub IS NOT NULL THEN p.buyout_proceeds_rub * p.quantity
         ELSE IFNULL(p.sp_unit, p.price_rub) * p.quantity END AS seller_base,
    IFNULL(p.bp,0) bp, IFNULL(p.bonus,0) bonus, IFNULL(p.coinv,0) coinv,
    IF(p.op_type='MARKETPLACE_SALE', IFNULL(-p.comm, NUMERIC '0'), NUMERIC '0') AS commission_known,
    IF(p.op_type='MARKETPLACE_SALE' AND p.sp_unit IS NULL, p.quantity, 0) AS commission_missing_qty,
    IF(p.op_type='MARKETPLACE_SALE' AND p.sp_unit IS NULL,
       p.price_rub*p.quantity, NUMERIC '0') AS commission_missing_revenue,
    c.u * p.quantity AS cogs_amt,
    IF(c.u IS NULL, p.quantity, 0) AS cogs_missing_qty
  FROM cls p
  LEFT JOIN cogs c ON c.internal_sku=p.internal_sku AND p.order_date BETWEEN c.effective_from AND c.et),
s AS (
  SELECT m,
    SUM(quantity) gross_ordered_qty,
    SUM(IF(status='cancelled', quantity, 0)) cancelled_qty,
    SUM(IF(status IN ('delivering','awaiting_deliver','awaiting_packaging'), quantity, 0)) in_transit_qty,
    SUM(IF(status='delivered', quantity, 0)) realized_qty,
    ROUND(SUM(IF(status='delivered', seller_base, 0)),2) seller_base_revenue_rub,
    ROUND(SUM(IF(status='delivered', bp, 0)),2) buyer_paid_revenue_rub,
    ROUND(SUM(IF(status='delivered', bonus, 0)),2) bonus_rub,
    ROUND(SUM(IF(status='delivered', coinv, 0)),2) coinvestment_rub,
    ROUND(SUM(IF(status='delivered', cogs_amt, 0)),2) product_cogs_rub,
    SUM(IF(status='delivered', quantity - cogs_missing_qty, 0)) cogs_covered_qty,
    SUM(IF(status='delivered', cogs_missing_qty, 0)) cogs_missing_qty,
    ROUND(SUM(IF(status='delivered', commission_known, 0)),2) commission_known_rub,
    SUM(IF(status='delivered', quantity - commission_missing_qty, 0)) commission_coverage_qty,
    SUM(IF(status='delivered', commission_missing_qty, 0)) commission_missing_qty,
    ROUND(SUM(IF(status='delivered', commission_missing_revenue, 0)),2) commission_missing_revenue_rub
  FROM sales GROUP BY m),
f AS (
  SELECT DATE_TRUNC(event_date, MONTH) m,
    ROUND(SUM(IF(type_id IN (32,29,28,98,30,1,59,45,78,9,79), -amount_rub, 0)),2) direct_variable_marketplace_costs_rub,
    ROUND(SUM(IF(type_id IN (12,46), -amount_rub, 0)),2) store_level_variable_costs_rub,
    ROUND(SUM(IF(type_id IN (77,76,15,71,39,38,57), -amount_rub, 0)),2) other_marketplace_costs_ex_ads_rub,
    ROUND(SUM(IF(type_id=52, -amount_rub, 0)),2) marketplace_fixed_costs_rub,
    ROUND(SUM(IF(type_id IN (25,10), amount_rub, 0)),2) compensations_rub,
    ROUND(SUM(IF(type_id IN (116,47,96,74,48), -amount_rub, 0)),2) ad_reviews_rub
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_FINANCE_ACCRUAL` GROUP BY m),
a AS (SELECT DATE_TRUNC(date, MONTH) m, ROUND(SUM(expense_rub),2) ad_campaign_rub
      FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_ADS_EXPENSE_DAILY` GROUP BY m),
asku AS (SELECT DATE_TRUNC(date, MONTH) m, ROUND(SUM(attributed_spend_rub),2) ad_sku_attr_rub
      FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_ADS_SKU_DAILY` GROUP BY m),
j AS (
  SELECT COALESCE(s.m, f.m, a.m) month,
    IFNULL(s.gross_ordered_qty,0) gross_ordered_qty, IFNULL(s.cancelled_qty,0) cancelled_qty,
    IFNULL(s.in_transit_qty,0) in_transit_qty, IFNULL(s.realized_qty,0) realized_qty,
    IFNULL(s.seller_base_revenue_rub,0) seller_base_revenue_rub,
    IFNULL(s.buyer_paid_revenue_rub,0) buyer_paid_revenue_rub,
    IFNULL(s.bonus_rub,0) bonus_rub, IFNULL(s.coinvestment_rub,0) coinvestment_rub,
    IFNULL(s.product_cogs_rub,0) product_cogs_rub,
    IFNULL(s.cogs_covered_qty,0) cogs_covered_qty, IFNULL(s.cogs_missing_qty,0) cogs_missing_qty,
    IFNULL(s.commission_known_rub,0) commission_known_rub,
    IFNULL(s.commission_coverage_qty,0) commission_coverage_qty,
    IFNULL(s.commission_missing_qty,0) commission_missing_qty,
    IFNULL(s.commission_missing_revenue_rub,0) commission_missing_revenue_rub,
    IFNULL(f.direct_variable_marketplace_costs_rub,0) direct_variable_marketplace_costs_rub,
    IFNULL(f.store_level_variable_costs_rub,0) store_level_variable_costs_rub,
    IFNULL(f.other_marketplace_costs_ex_ads_rub,0) other_marketplace_costs_ex_ads_rub,
    IFNULL(f.marketplace_fixed_costs_rub,0) marketplace_fixed_costs_rub,
    IFNULL(f.compensations_rub,0) compensations_rub,
    IFNULL(a.ad_campaign_rub,0) + IFNULL(f.ad_reviews_rub,0) advertising_rub,
    IFNULL(a.ad_campaign_rub,0) ad_campaign_rub,
    IFNULL(asku.ad_sku_attr_rub,0) ad_sku_attributed_rub
  FROM s FULL JOIN f USING (m) FULL JOIN a USING (m) FULL JOIN asku USING (m))
SELECT
  month, gross_ordered_qty, cancelled_qty, in_transit_qty, realized_qty,
  seller_base_revenue_rub, buyer_paid_revenue_rub, bonus_rub, coinvestment_rub,
  product_cogs_rub, cogs_covered_qty, cogs_missing_qty,
  ROUND(SAFE_DIVIDE(cogs_covered_qty, NULLIF(realized_qty,0))*100, 4) cogs_coverage_pct,
  commission_known_rub, commission_coverage_qty, commission_missing_qty, commission_missing_revenue_rub,
  ROUND(commission_missing_revenue_rub * NUMERIC '0.18', 2) commission_uncertainty_lower_rub,
  ROUND(commission_missing_revenue_rub * NUMERIC '0.52', 2) commission_uncertainty_upper_rub,
  direct_variable_marketplace_costs_rub, store_level_variable_costs_rub, other_marketplace_costs_ex_ads_rub,
  ROUND(seller_base_revenue_rub - product_cogs_rub - commission_known_rub - direct_variable_marketplace_costs_rub, 2) l1_gross_seller_contribution_rub,
  ROUND(seller_base_revenue_rub - product_cogs_rub - commission_known_rub - direct_variable_marketplace_costs_rub
        - store_level_variable_costs_rub - other_marketplace_costs_ex_ads_rub, 2) l2_contribution_before_ads_rub,
  advertising_rub,
  ROUND(seller_base_revenue_rub - product_cogs_rub - commission_known_rub - direct_variable_marketplace_costs_rub
        - store_level_variable_costs_rub - other_marketplace_costs_ex_ads_rub - advertising_rub, 2) l3_after_ads_before_fixed_rub,
  marketplace_fixed_costs_rub, compensations_rub,
  ROUND(seller_base_revenue_rub - product_cogs_rub - commission_known_rub - direct_variable_marketplace_costs_rub
        - store_level_variable_costs_rub - other_marketplace_costs_ex_ads_rub - advertising_rub
        - marketplace_fixed_costs_rub + compensations_rub, 2) l4_marketplace_contribution_profit_known_rub,
  ROUND(seller_base_revenue_rub - product_cogs_rub - commission_known_rub - direct_variable_marketplace_costs_rub
        - store_level_variable_costs_rub - other_marketplace_costs_ex_ads_rub - advertising_rub
        - marketplace_fixed_costs_rub + compensations_rub - commission_missing_revenue_rub*NUMERIC '0.52', 2) l4_profit_uncertainty_lower_rub,
  ROUND(seller_base_revenue_rub - product_cogs_rub - commission_known_rub - direct_variable_marketplace_costs_rub
        - store_level_variable_costs_rub - other_marketplace_costs_ex_ads_rub - advertising_rub
        - marketplace_fixed_costs_rub + compensations_rub - commission_missing_revenue_rub*NUMERIC '0.18', 2) l4_profit_uncertainty_upper_rub,
  ROUND(SAFE_DIVIDE(advertising_rub, NULLIF(seller_base_revenue_rub,0))*100, 4) actual_drr_pct,
  ROUND(SAFE_DIVIDE(seller_base_revenue_rub - product_cogs_rub - commission_known_rub - direct_variable_marketplace_costs_rub
        - store_level_variable_costs_rub - other_marketplace_costs_ex_ads_rub, NULLIF(seller_base_revenue_rub,0))*100, 4) variable_break_even_drr_pct,
  ROUND(SAFE_DIVIDE(seller_base_revenue_rub - product_cogs_rub - commission_known_rub - direct_variable_marketplace_costs_rub
        - store_level_variable_costs_rub - other_marketplace_costs_ex_ads_rub - marketplace_fixed_costs_rub + compensations_rub,
        NULLIF(seller_base_revenue_rub,0))*100, 4) full_marketplace_break_even_drr_pct,
  NUMERIC '100' finance_classification_coverage_pct,
  NUMERIC '100' ad_total_coverage_pct,
  ROUND(SAFE_DIVIDE(ad_sku_attributed_rub, NULLIF(ad_campaign_rub,0))*100, 4) ad_sku_attribution_coverage_pct,
  CASE WHEN commission_missing_qty > 0 OR cogs_missing_qty > 0 THEN 'KNOWN_GAP' ELSE 'COMPLETE' END data_confidence,
  'PASS_WITH_KNOWN_COMMISSION_GAP' data_status,
  CURRENT_TIMESTAMP() computed_at
FROM j WHERE month IS NOT NULL;
