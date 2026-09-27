-- Экономика доставленных заказов: товар × сутки заказа (Москва). Результат до себестоимости —
-- при классифицированных начислениях; после себестоимости — только при полной себестоимости
-- продавца (cogs_coverage = COMPLETE). NULL означает «не вычисляется», а не ноль.
CREATE OR REPLACE VIEW `__tenant__.analytics_share.profitability_daily`
OPTIONS(description = 'Экономика доставленных заказов: товар × сутки заказа (Москва). Выручка продавца, комиссия и расходы Ozon, результат до и после себестоимости со статусами полноты.')
AS
SELECT e.order_date_msk AS sales_date, e.sku, e.internal_sku, d.product_name, e.delivered_units,
  e.seller_revenue_rub, e.commission_rub, e.logistics_rub, e.acquiring_rub, e.return_logistics_rub,
  e.other_costs_rub, e.contribution_pre_cogs_rub, e.product_cogs_rub, e.contribution_after_cogs_rub,
  e.revenue_basis, e.taxonomy_status, e.cogs_coverage,
  COALESCE(cf.status, 'UNKNOWN') AS finance_data_status
FROM `__tenant__.ozon_mart.FACT_OZON_SKU_ECONOMICS_DAILY` e
LEFT JOIN `__tenant__.ozon_mart.DIM_OZON_PRODUCT` d ON d.sku = e.sku
LEFT JOIN `__tenant__.tenant_ops.V_COVERAGE_DAILY` cf ON cf.entity = 'finance_accrual' AND cf.coverage_date = e.order_date_msk;
