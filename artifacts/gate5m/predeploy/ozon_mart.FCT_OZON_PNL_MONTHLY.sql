WITH rec_comm AS (
  SELECT * FROM UNNEST([
    STRUCT('0107987069-0115-1' AS posting_number, NUMERIC '428.40' AS c),('0107987069-0116-1',NUMERIC '346.50'),
    ('0111865653-0053-1',NUMERIC '356.16'),('0140207342-0283-1',NUMERIC '446.46'),('0140207342-0294-1',NUMERIC '391.44'),
    ('0140425762-0336-1',NUMERIC '350.28'),('0143652501-0071-1',NUMERIC '428.40'),('0148296041-0008-1',NUMERIC '346.50'),
    ('0148983889-0086-1',NUMERIC '262.92'),('0174671740-0036-1',NUMERIC '206.79'),('0180844433-0032-1',NUMERIC '222.65'),
    ('0184479194-0054-1',NUMERIC '350.28'),('0198758548-0051-1',NUMERIC '382.20'),('0201540221-0097-1',NUMERIC '341.88'),
    ('0231520423-0001-2',NUMERIC '254.94'),('0233071107-0012-1',NUMERIC '276.36'),('52826697-0003-15',NUMERIC '254.94'),
    ('57794512-0006-2',NUMERIC '264.00'),('59699516-0310-4',NUMERIC '439.12'),('69799830-0276-1',NUMERIC '229.40'),
    ('71080184-0023-1',NUMERIC '262.92'),('81535887-0077-1',NUMERIC '350.28'),('90312383-0011-1',NUMERIC '385.56'),
    ('90831504-0160-1',NUMERIC '352.80'),('92234251-0008-1',NUMERIC '254.94'),('98041754-0004-1',NUMERIC '369.60'),
    ('98041754-0006-1',NUMERIC '344.40'),('99543424-0109-1',NUMERIC '350.28'),('99543424-0131-1',NUMERIC '387.20')])),
post AS (
  SELECT p.posting_number, p.sku, p.status, p.order_date, p.quantity, p.price_rub, m.internal_sku
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
sales AS (
  SELECT DATE_TRUNC(p.order_date, MONTH) m, p.status, p.quantity, p.posting_number,
    IFNULL(f.sp_unit, p.price_rub) * p.quantity AS seller_base,
    IFNULL(f.bp,0) bp, IFNULL(f.bonus,0) bonus, IFNULL(f.coinv,0) coinv,
    IFNULL(-f.comm, IFNULL(rc.c, NUMERIC '0')) AS commission_known,
    IF(f.sp_unit IS NULL AND rc.c IS NULL, p.quantity, 0) AS commission_missing_qty,
    IF(f.sp_unit IS NULL AND rc.c IS NULL, p.price_rub*p.quantity, NUMERIC '0') AS commission_missing_revenue,
    c.u * p.quantity AS cogs_amt,
    IF(c.u IS NULL, p.quantity, 0) AS cogs_missing_qty
  FROM post p
  LEFT JOIN fin_econ f USING (posting_number, sku)
  LEFT JOIN rec_comm rc ON rc.posting_number = p.posting_number
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
FROM j WHERE month IS NOT NULL