-- Экономика доставленных заказов: товар × сутки заказа (Москва). Суммы со знаком Ozon
-- (выручка > 0, комиссия и расходы < 0). Результат до себестоимости — только когда факт его
-- публикует (все отправления рассчитаны, нет нераспознанных начислений и признаков возврата)
-- и начисления полны от суток заказа до последних оценённых суток с учётом срока созревания
-- продавца (finance_data_status = COMPLETE, причина иначе — finance_status_reason); после себестоимости — ещё и при полной себестоимости продавца. NULL — «не
-- вычисляется», а не ноль.
CREATE OR REPLACE VIEW `__tenant__.analytics_share.profitability_daily`
OPTIONS(description = 'Экономика доставленных заказов: товар × сутки заказа (Москва). Выручка продавца, комиссия и расходы Ozon со знаком Ozon, результат до и после себестоимости — только при полных данных; статусы полноты.')
AS
SELECT e.order_date_msk AS sales_date, e.sku, e.internal_sku, d.product_name, e.delivered_units,
  e.seller_revenue_rub, e.commission_rub, e.logistics_rub, e.acquiring_rub, e.return_logistics_rub,
  e.promotion_rub, e.other_costs_rub, e.unclassified_rub, e.unsettled_postings, e.postings_with_return_costs,
  IF(w.status = 'COMPLETE', e.contribution_pre_cogs_rub, NULL) AS contribution_pre_cogs_rub,
  e.product_cogs_rub,
  IF(w.status = 'COMPLETE', e.contribution_after_cogs_rub, NULL) AS contribution_after_cogs_rub,
  e.revenue_basis, e.taxonomy_status, e.cogs_coverage,
  COALESCE(w.status, 'UNKNOWN') AS finance_data_status,
  IF(w.status IS NULL, 'NOT_EVALUATED', w.reason) AS finance_status_reason
FROM `__tenant__.ozon_mart.FACT_OZON_SKU_ECONOMICS_DAILY` e
LEFT JOIN `__tenant__.ozon_mart.DIM_OZON_PRODUCT` d ON d.sku = e.sku
LEFT JOIN `__tenant__.tenant_ops.V_FINANCE_WINDOW_STATUS` w ON w.from_date = e.order_date_msk;
