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
pmap AS (SELECT DISTINCT posting_number, sku, order_date FROM post),
fin_econ AS (
  SELECT posting_number, sku, SUM(seller_base_price_rub) sp_unit, SUM(commission_rub) comm
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_FINANCE_ACCRUAL`
  WHERE seller_base_price_rub IS NOT NULL GROUP BY 1,2),
cogs AS (SELECT internal_sku, effective_from, COALESCE(effective_to, DATE '9999-12-31') et, product_cogs_rub u
         FROM `project-fa311fc0-4d87-4781-986.evetis_ref.V_PRODUCT_COGS_EFFECTIVE`),
sales AS (
  SELECT DATE_TRUNC(p.order_date, MONTH) m, p.internal_sku, p.status, p.quantity,
    IFNULL(f.sp_unit, p.price_rub) * p.quantity seller_base,
    IFNULL(-f.comm, IFNULL(rc.c, NUMERIC '0')) commission_known,
    IF(f.sp_unit IS NULL AND rc.c IS NULL, p.quantity, 0) comm_missing_qty,
    c.u * p.quantity cogs_amt, IF(c.u IS NULL, p.quantity, 0) cogs_missing_qty
  FROM post p LEFT JOIN fin_econ f USING (posting_number, sku)
  LEFT JOIN rec_comm rc ON rc.posting_number=p.posting_number
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
    ROUND(SUM(IF(f.type_id IN (15,71,39,38), -f.amount_rub, 0)),2) other_direct
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_FINANCE_ACCRUAL` f
  JOIN pmap pm ON pm.posting_number=f.posting_number AND pm.sku=f.sku
  JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP` mp
    ON mp.marketplace='OZON' AND mp.marketplace_sku=f.sku
  WHERE f.posting_number IS NOT NULL GROUP BY 1,2),
dcost_sku AS (
  SELECT DATE_TRUNC(f.event_date, MONTH) m, mp.internal_sku,
    ROUND(SUM(IF(f.type_id IN (32,29,28,98,30,1,59,45,78,9,79), -f.amount_rub, 0)),2) direct_var,
    ROUND(SUM(IF(f.type_id IN (15,71,39,38), -f.amount_rub, 0)),2) other_direct
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
  j.direct_variable_marketplace_costs_rub, j.other_direct_marketplace_costs_rub,
  ROUND(j.seller_base_revenue_rub - j.product_cogs_rub - j.commission_rub
        - j.direct_variable_marketplace_costs_rub - j.other_direct_marketplace_costs_rub, 2) contribution_before_ads_rub,
  j.ad_spend_attributed_rub,
  ROUND(j.seller_base_revenue_rub - j.product_cogs_rub - j.commission_rub
        - j.direct_variable_marketplace_costs_rub - j.other_direct_marketplace_costs_rub
        - j.ad_spend_attributed_rub, 2) contribution_after_attributed_ads_rub,
  ROUND(SAFE_DIVIDE(j.seller_base_revenue_rub - j.product_cogs_rub - j.commission_rub
        - j.direct_variable_marketplace_costs_rub - j.other_direct_marketplace_costs_rub,
        NULLIF(j.seller_base_revenue_rub,0))*100, 4) margin_before_ads_pct,
  ROUND(SAFE_DIVIDE(j.seller_base_revenue_rub - j.product_cogs_rub - j.commission_rub
        - j.direct_variable_marketplace_costs_rub - j.other_direct_marketplace_costs_rub
        - j.ad_spend_attributed_rub, NULLIF(j.seller_base_revenue_rub,0))*100, 4) margin_after_ads_pct,
  ROUND(SAFE_DIVIDE(j.ad_spend_attributed_rub, NULLIF(j.seller_base_revenue_rub,0))*100, 4) actual_drr_pct,
  ROUND(SAFE_DIVIDE(j.seller_base_revenue_rub - j.product_cogs_rub - j.commission_rub
        - j.direct_variable_marketplace_costs_rub - j.other_direct_marketplace_costs_rub,
        NULLIF(j.seller_base_revenue_rub,0))*100, 4) variable_break_even_drr_pct,
  CASE
    WHEN j.cogs_missing_qty > 0 THEN 'COGS_INCOMPLETE'
    WHEN j.realized_qty = 0 THEN 'INSUFFICIENT_DATA'
    WHEN j.seller_base_revenue_rub - j.product_cogs_rub - j.commission_rub
         - j.direct_variable_marketplace_costs_rub - j.other_direct_marketplace_costs_rub <= 0 THEN 'LOSS_BEFORE_ADS'
    WHEN j.seller_base_revenue_rub - j.product_cogs_rub - j.commission_rub
         - j.direct_variable_marketplace_costs_rub - j.other_direct_marketplace_costs_rub
         - j.ad_spend_attributed_rub > 0 THEN 'PROFITABLE_AFTER_ADS'
    ELSE 'PROFITABLE_BEFORE_ADS_ONLY' END profitability_status,
  'DIRECT_POSTING+DIRECT_SKU+ACTUAL_AD_ATTRIBUTION' attribution_level,
  CURRENT_TIMESTAMP() computed_at
FROM j LEFT JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_PRODUCT_MASTER` pm
  ON pm.internal_sku = j.internal_sku
WHERE j.month IS NOT NULL AND j.internal_sku IS NOT NULL