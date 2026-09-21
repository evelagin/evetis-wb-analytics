WITH post AS (
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
  SELECT p.order_date d, p.internal_sku, p.status, p.quantity,
    CASE WHEN p.buyout_proceeds_rub IS NOT NULL THEN p.buyout_proceeds_rub * p.quantity
         ELSE IFNULL(p.sp_unit, p.price_rub) * p.quantity END seller_base,
    IF(p.op_type='MARKETPLACE_SALE', IFNULL(-p.comm, NUMERIC '0'), NUMERIC '0') commission_known,
    IF(p.op_type='MARKETPLACE_SALE' AND p.sp_unit IS NULL, p.quantity, 0) comm_missing_qty,
    IF(p.op_type='CIS_BUYOUT', p.quantity, 0) comm_na_qty,
    IF(p.op_type='CIS_BUYOUT' AND p.buyout_proceeds_rub IS NULL, p.quantity, 0) buyout_unproven_qty,
    IF(p.op_type='CIS_BUYOUT' AND p.buyout_proceeds_rub IS NULL,
       p.price_rub * p.quantity, NUMERIC '0') buyout_unproven_rub,
    c.u * p.quantity cogs_amt, IF(c.u IS NULL, p.quantity, 0) cogs_missing_qty
  FROM cls p
  LEFT JOIN cogs c ON c.internal_sku=p.internal_sku AND p.order_date BETWEEN c.effective_from AND c.et),
s AS (SELECT d, internal_sku,
    SUM(quantity) gross_qty,
    SUM(IF(status='cancelled', quantity, 0)) cancelled_qty,
    SUM(IF(status IN ('delivering','awaiting_deliver','awaiting_packaging'), quantity, 0)) in_transit_qty,
    SUM(IF(status='delivered', quantity, 0)) realized_qty,
    SUM(IF(status='delivered', seller_base, 0)) seller_base_revenue_rub,
    SUM(IF(status='delivered', cogs_amt, 0)) product_cogs_rub,
    SUM(IF(status='delivered', cogs_missing_qty, 0)) cogs_missing_qty,
    SUM(IF(status='delivered', commission_known, 0)) commission_rub,
    SUM(IF(status='delivered', comm_missing_qty, 0)) comm_missing_qty,
    SUM(IF(status='delivered', comm_na_qty, 0)) comm_na_qty,
    SUM(IF(status='delivered', buyout_unproven_qty, 0)) buyout_unproven_qty,
    SUM(IF(status='delivered', buyout_unproven_rub, 0)) buyout_unproven_rub
  FROM sales GROUP BY 1,2),
dcost AS (
  SELECT pm.order_date d, mp.internal_sku, f.type_id, -f.amount_rub amt
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_FINANCE_ACCRUAL` f
  JOIN pmap pm ON pm.posting_number=f.posting_number AND pm.sku=f.sku
  JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP` mp
    ON mp.marketplace='OZON' AND mp.marketplace_sku=f.sku
  WHERE f.posting_number IS NOT NULL
  UNION ALL
  SELECT f.event_date d, mp.internal_sku, f.type_id, -f.amount_rub amt
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_FINANCE_ACCRUAL` f
  JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP` mp
    ON mp.marketplace='OZON' AND mp.marketplace_sku=f.sku
  WHERE f.sku IS NOT NULL AND f.posting_number IS NULL),
dc AS (SELECT d, internal_sku,
    SUM(IF(type_id IN (32,29,28,98,30,59,45,78,9), amt, 0)) logistics,
    SUM(IF(type_id = 1, amt, 0)) acquiring,
    SUM(IF(type_id = 79, amt, 0)) storage,
    SUM(IF(type_id IN (32,29,28,98,30,1,59,45,78,9,79), amt, 0)) direct_var,
    SUM(IF(type_id IN (15,71,39,38,6), amt, 0)) other_direct,
    SUM(IF(type_id IN (116,74,48), amt, 0)) sku_promotion
  FROM dcost GROUP BY 1,2),
ads AS (SELECT a.date d, mp.internal_sku, SUM(a.attributed_spend_rub) ad_attr
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_ADS_SKU_DAILY` a
  JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP` mp
    ON mp.marketplace='OZON' AND mp.marketplace_sku=a.sku GROUP BY 1,2),
j AS (SELECT COALESCE(s.d, dc.d, ads.d) fact_date,
    COALESCE(s.internal_sku, dc.internal_sku, ads.internal_sku) internal_sku,
    IFNULL(s.gross_qty,0) gross_qty, IFNULL(s.cancelled_qty,0) cancelled_qty,
    IFNULL(s.in_transit_qty,0) in_transit_qty, IFNULL(s.realized_qty,0) realized_qty,
    IFNULL(s.seller_base_revenue_rub,0) seller_base_revenue_rub,
    IFNULL(s.commission_rub,0) commission_rub, IFNULL(s.comm_missing_qty,0) commission_missing_qty,
    IFNULL(s.comm_na_qty,0) commission_not_applicable_qty,
    IFNULL(s.buyout_unproven_qty,0) buyout_revenue_unproven_qty,
    IFNULL(s.buyout_unproven_rub,0) buyout_revenue_unproven_rub,
    IFNULL(dc.logistics,0) logistics_rub, IFNULL(dc.acquiring,0) acquiring_rub, IFNULL(dc.storage,0) storage_rub,
    IFNULL(dc.direct_var,0) direct_variable_marketplace_costs_rub,
    IFNULL(dc.other_direct,0) other_direct_marketplace_costs_rub,
    IFNULL(dc.sku_promotion,0) sku_promotion_rub,
    IFNULL(s.product_cogs_rub,0) product_cogs_rub, IFNULL(s.cogs_missing_qty,0) cogs_missing_qty,
    IFNULL(ads.ad_attr,0) ad_spend_attributed_rub
  FROM s FULL JOIN dc USING (d, internal_sku)
         FULL JOIN ads USING (d, internal_sku))
SELECT j.fact_date, j.internal_sku,
  j.gross_qty, j.cancelled_qty, j.in_transit_qty, j.realized_qty,
  j.seller_base_revenue_rub, j.commission_rub, j.commission_missing_qty,
  j.commission_not_applicable_qty, j.buyout_revenue_unproven_qty, j.buyout_revenue_unproven_rub,
  j.logistics_rub, j.acquiring_rub, j.storage_rub,
  j.direct_variable_marketplace_costs_rub, j.other_direct_marketplace_costs_rub,
  j.product_cogs_rub, j.cogs_missing_qty, j.sku_promotion_rub,
  j.seller_base_revenue_rub - j.product_cogs_rub - j.commission_rub
    - j.direct_variable_marketplace_costs_rub - j.other_direct_marketplace_costs_rub
    - j.sku_promotion_rub contribution_before_ads_rub,
  j.ad_spend_attributed_rub,
  j.seller_base_revenue_rub - j.product_cogs_rub - j.commission_rub
    - j.direct_variable_marketplace_costs_rub - j.other_direct_marketplace_costs_rub
    - j.sku_promotion_rub - j.ad_spend_attributed_rub contribution_after_attributed_ads_rub,
  'OZON_V1: orders, sold, revenue, commission, cogs, posting-linked costs = order date; sku-only costs = accrual date; ads = ad stat date' fact_date_semantics
FROM j
WHERE j.fact_date IS NOT NULL AND j.internal_sku IS NOT NULL