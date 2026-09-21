WITH
post AS (
  SELECT p.posting_number, p.sku, p.status, p.order_date, p.quantity, p.price_rub,
         p.payout_rub, m.internal_sku
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_POSTINGS_FBO` p
  JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP` m
    ON m.marketplace='OZON' AND m.marketplace_sku=p.sku),
pmap AS (SELECT DISTINCT posting_number, sku, order_date FROM post),
fin_econ AS (
  SELECT posting_number, sku, SUM(seller_base_price_rub) sp_unit, SUM(commission_rub) comm
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_FINANCE_ACCRUAL`
  WHERE seller_base_price_rub IS NOT NULL GROUP BY 1,2),
cogs AS (SELECT internal_sku, effective_from, COALESCE(effective_to, DATE '9999-12-31') et, product_cogs_rub u
         FROM `project-fa311fc0-4d87-4781-986.evetis_ref.V_PRODUCT_COGS_EFFECTIVE`),
cls AS (
  SELECT p.*, f.sp_unit, f.comm, b.buyout_proceeds_rub,
    CASE WHEN b.posting_number IS NOT NULL THEN 'CIS_BUYOUT'
         WHEN p.status='delivered' AND f.sp_unit IS NULL AND IFNULL(p.payout_rub, NUMERIC '0')=0
           THEN 'CIS_BUYOUT'
         ELSE 'MARKETPLACE_SALE' END op_type
  FROM post p LEFT JOIN fin_econ f USING (posting_number, sku)
  LEFT JOIN `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_CIS_BUYOUT` b
    ON b.posting_number=p.posting_number),
sales AS (
  SELECT DATE_TRUNC(p.order_date, MONTH) m, p.internal_sku, p.status, p.quantity,
    CASE WHEN p.buyout_proceeds_rub IS NOT NULL THEN p.buyout_proceeds_rub * p.quantity
         ELSE IFNULL(p.sp_unit, p.price_rub) * p.quantity END seller_base,
    IF(p.op_type='MARKETPLACE_SALE', IFNULL(-p.comm, NUMERIC '0'), NUMERIC '0') commission_known,
    IF(p.op_type='MARKETPLACE_SALE' AND p.sp_unit IS NULL, p.quantity, 0) comm_missing_qty,
    c.u * p.quantity cogs_amt, IF(c.u IS NULL, p.quantity, 0) cogs_missing_qty
  FROM cls p
  LEFT JOIN cogs c ON c.internal_sku=p.internal_sku AND p.order_date BETWEEN c.effective_from AND c.et),
s AS (SELECT m, internal_sku,
    SUM(quantity) gross_qty, SUM(IF(status='delivered', quantity, 0)) realized_qty,
    ROUND(SUM(IF(status='delivered', seller_base, 0)),2) seller_base_revenue_rub,
    ROUND(SUM(IF(status='delivered', cogs_amt, 0)),2) product_cogs_rub,
    SUM(IF(status='delivered', cogs_missing_qty, 0)) cogs_missing_qty,
    ROUND(SUM(IF(status='delivered', commission_known, 0)),2) commission_rub,
    SUM(IF(status='delivered', comm_missing_qty, 0)) comm_missing_qty
  FROM sales GROUP BY 1,2),
dcost_post AS (
  SELECT DATE_TRUNC(pm.order_date, MONTH) m, mp.internal_sku,
    ROUND(SUM(IF(f.type_id IN (32,29,28,98,30,1,59,45,78,9,79), -f.amount_rub, 0)),2) direct_var,
    ROUND(SUM(IF(f.type_id IN (15,71,39,38,6), -f.amount_rub, 0)),2) other_direct,
    ROUND(SUM(IF(f.type_id IN (116,74,48), -f.amount_rub, 0)),2) sku_promotion
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_FINANCE_ACCRUAL` f
  JOIN pmap pm ON pm.posting_number=f.posting_number AND pm.sku=f.sku
  JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP` mp
    ON mp.marketplace='OZON' AND mp.marketplace_sku=f.sku
  WHERE f.posting_number IS NOT NULL GROUP BY 1,2),
dcost_sku AS (
  SELECT DATE_TRUNC(f.event_date, MONTH) m, mp.internal_sku,
    ROUND(SUM(IF(f.type_id IN (32,29,28,98,30,1,59,45,78,9,79), -f.amount_rub, 0)),2) direct_var,
    ROUND(SUM(IF(f.type_id IN (15,71,39,38,6), -f.amount_rub, 0)),2) other_direct,
    ROUND(SUM(IF(f.type_id IN (116,74,48), -f.amount_rub, 0)),2) sku_promotion
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_FINANCE_ACCRUAL` f
  JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP` mp
    ON mp.marketplace='OZON' AND mp.marketplace_sku=f.sku
  WHERE f.sku IS NOT NULL AND f.posting_number IS NULL GROUP BY 1,2),
ads AS (SELECT DATE_TRUNC(a.date, MONTH) m, mp.internal_sku, ROUND(SUM(a.attributed_spend_rub),2) ad_attr
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_ADS_SKU_DAILY` a
  JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP` mp
    ON mp.marketplace='OZON' AND mp.marketplace_sku=a.sku GROUP BY 1,2),
j AS (SELECT COALESCE(s.m, dp.m, ds.m, ads.m) month,
    COALESCE(s.internal_sku, dp.internal_sku, ds.internal_sku, ads.internal_sku) internal_sku,
    IFNULL(s.gross_qty,0) gross_qty, IFNULL(s.realized_qty,0) realized_qty,
    IFNULL(s.seller_base_revenue_rub,0) seller_base_revenue_rub,
    IFNULL(s.product_cogs_rub,0) product_cogs_rub, IFNULL(s.cogs_missing_qty,0) cogs_missing_qty,
    IFNULL(s.commission_rub,0) commission_rub, IFNULL(s.comm_missing_qty,0) comm_missing_qty,
    IFNULL(dp.direct_var,0) + IFNULL(ds.direct_var,0) direct_variable_marketplace_costs_rub,
    IFNULL(dp.other_direct,0) + IFNULL(ds.other_direct,0) other_direct_marketplace_costs_rub,
    IFNULL(dp.sku_promotion,0) + IFNULL(ds.sku_promotion,0) sku_promotion_rub,
    IFNULL(ads.ad_attr,0) ad_spend_attributed_rub
  FROM s FULL JOIN dcost_post dp USING (m, internal_sku)
         FULL JOIN dcost_sku ds USING (m, internal_sku)
         FULL JOIN ads USING (m, internal_sku))
SELECT j.month, j.internal_sku, pm.canonical_product_name product_name,
  IF(pm.is_bundle, 'BUNDLE', 'SINGLE') product_type,
  j.gross_qty, j.realized_qty, j.seller_base_revenue_rub, j.product_cogs_rub,
  CASE WHEN j.cogs_missing_qty > 0 THEN 'MISSING_BOM_INTERVAL' ELSE 'COVERED' END cogs_status,
  j.commission_rub,
  CASE WHEN j.comm_missing_qty > 0 THEN 'MISSING_SOURCE' ELSE 'COVERED' END commission_status,
  j.direct_variable_marketplace_costs_rub, j.other_direct_marketplace_costs_rub, j.sku_promotion_rub,
  ROUND(j.seller_base_revenue_rub - j.product_cogs_rub - j.commission_rub
        - j.direct_variable_marketplace_costs_rub - j.other_direct_marketplace_costs_rub - j.sku_promotion_rub, 2) contribution_before_ads_rub,
  j.ad_spend_attributed_rub,
  ROUND(j.seller_base_revenue_rub - j.product_cogs_rub - j.commission_rub
        - j.direct_variable_marketplace_costs_rub - j.other_direct_marketplace_costs_rub - j.sku_promotion_rub
        - j.ad_spend_attributed_rub, 2) contribution_after_attributed_ads_rub,
  ROUND(SAFE_DIVIDE(j.seller_base_revenue_rub - j.product_cogs_rub - j.commission_rub
        - j.direct_variable_marketplace_costs_rub - j.other_direct_marketplace_costs_rub - j.sku_promotion_rub,
        NULLIF(j.seller_base_revenue_rub,0))*100, 4) margin_before_ads_pct,
  ROUND(SAFE_DIVIDE(j.seller_base_revenue_rub - j.product_cogs_rub - j.commission_rub
        - j.direct_variable_marketplace_costs_rub - j.other_direct_marketplace_costs_rub - j.sku_promotion_rub
        - j.ad_spend_attributed_rub, NULLIF(j.seller_base_revenue_rub,0))*100, 4) margin_after_ads_pct,
  ROUND(SAFE_DIVIDE(j.ad_spend_attributed_rub, NULLIF(j.seller_base_revenue_rub,0))*100, 4) actual_drr_pct,
  ROUND(SAFE_DIVIDE(j.seller_base_revenue_rub - j.product_cogs_rub - j.commission_rub
        - j.direct_variable_marketplace_costs_rub - j.other_direct_marketplace_costs_rub - j.sku_promotion_rub,
        NULLIF(j.seller_base_revenue_rub,0))*100, 4) variable_break_even_drr_pct,
  CASE
    WHEN j.cogs_missing_qty > 0 THEN 'COGS_INCOMPLETE'
    WHEN j.realized_qty = 0 THEN 'INSUFFICIENT_DATA'
    WHEN j.seller_base_revenue_rub - j.product_cogs_rub - j.commission_rub
         - j.direct_variable_marketplace_costs_rub - j.other_direct_marketplace_costs_rub - j.sku_promotion_rub <= 0 THEN 'LOSS_BEFORE_ADS'
    WHEN j.seller_base_revenue_rub - j.product_cogs_rub - j.commission_rub
         - j.direct_variable_marketplace_costs_rub - j.other_direct_marketplace_costs_rub - j.sku_promotion_rub
         - j.ad_spend_attributed_rub > 0 THEN 'PROFITABLE_AFTER_ADS'
    ELSE 'PROFITABLE_BEFORE_ADS_ONLY' END profitability_status,
  'DIRECT_POSTING+DIRECT_SKU+ACTUAL_AD_ATTRIBUTION' attribution_level,
  CURRENT_TIMESTAMP() computed_at
FROM j LEFT JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_PRODUCT_MASTER` pm
  ON pm.internal_sku = j.internal_sku
WHERE j.month IS NOT NULL AND j.internal_sku IS NOT NULL