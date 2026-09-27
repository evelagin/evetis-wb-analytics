-- Экономика доставленных заказов: сутки заказа (Москва) × SKU.
-- Выручка продавца — цена продавца за единицу из расчётов Ozon × количество; если Ozon ещё не
-- рассчитал отправление — цена из отправления, revenue_basis = NOT_SETTLED или PARTIALLY_SETTLED.
-- Себестоимость — только из справочника продавца на дату заказа; без неё результат после
-- себестоимости NULL, а cogs_coverage показывает причину. Никаких умолчаний.
CREATE OR REPLACE VIEW `__tenant__.ozon_mart.FACT_OZON_SKU_ECONOMICS_DAILY`
OPTIONS(description = 'Доставленные заказы: сутки заказа (Москва) × SKU; выручка продавца, комиссия, расходы Ozon по отправлениям, себестоимость продавца с явным покрытием.')
AS
WITH lines AS (
  SELECT p.order_date_msk, p.sku, p.posting_number, p.quantity,
    s.seller_price_unit_rub, p.price_rub,
    s.posting_number IS NOT NULL AND s.seller_price_unit_rub IS NOT NULL AS is_settled,
    IFNULL(s.commission_rub, 0) AS commission_rub,
    IFNULL(s.logistics_rub, 0) + IFNULL(s.last_mile_rub, 0) AS logistics_rub,
    IFNULL(s.acquiring_rub, 0) AS acquiring_rub,
    IFNULL(s.return_logistics_rub, 0) AS return_logistics_rub,
    IFNULL(s.other_costs_rub, 0) AS other_costs_rub,
    IFNULL(s.unclassified_rub, 0) AS unclassified_rub
  FROM `__tenant__.ozon_mart.NORM_OZON_POSTING_LINE` p
  LEFT JOIN `__tenant__.ozon_mart.NORM_OZON_POSTING_SETTLEMENT` s
    ON s.posting_number = p.posting_number AND s.sku = p.sku
  WHERE p.is_delivered
),
costed AS (
  SELECT l.order_date_msk, l.sku, l.posting_number, l.quantity, l.is_settled, l.commission_rub,
    l.logistics_rub, l.acquiring_rub, l.return_logistics_rub, l.other_costs_rub, l.unclassified_rub,
    IF(l.is_settled, l.seller_price_unit_rub, l.price_rub) * l.quantity AS seller_revenue_rub,
    d.internal_sku, c.product_cogs_rub * l.quantity AS product_cogs_rub,
    c.product_cogs_rub IS NOT NULL AS has_cogs
  FROM lines l
  LEFT JOIN `__tenant__.ozon_mart.DIM_OZON_PRODUCT` d ON d.sku = l.sku
  LEFT JOIN `__tenant__.ozon_mart.ECON_TENANT_COGS` c
    ON c.internal_sku = d.internal_sku AND l.order_date_msk BETWEEN c.effective_from AND c.effective_to
)
SELECT x.order_date_msk, x.sku, ANY_VALUE(x.internal_sku) AS internal_sku,
  COUNT(DISTINCT x.posting_number) AS delivered_postings,
  SUM(x.quantity) AS delivered_units,
  SUM(x.seller_revenue_rub) AS seller_revenue_rub,
  SUM(x.commission_rub) AS commission_rub,
  SUM(x.logistics_rub) AS logistics_rub,
  SUM(x.acquiring_rub) AS acquiring_rub,
  SUM(x.return_logistics_rub) AS return_logistics_rub,
  SUM(x.other_costs_rub) AS other_costs_rub,
  SUM(x.unclassified_rub) AS unclassified_rub,
  COUNTIF(NOT x.is_settled) AS unsettled_postings,
  IF(COUNTIF(NOT x.is_settled) = 0, 'SETTLED', IF(COUNTIF(x.is_settled) = 0, 'NOT_SETTLED', 'PARTIALLY_SETTLED')) AS revenue_basis,
  IF(SUM(x.unclassified_rub) = 0, 'CLASSIFIED', 'UNCLASSIFIED_PRESENT') AS taxonomy_status,
  IF(SUM(x.unclassified_rub) = 0,
     SUM(x.seller_revenue_rub) - SUM(x.commission_rub)
       + SUM(x.logistics_rub) + SUM(x.acquiring_rub) + SUM(x.return_logistics_rub) + SUM(x.other_costs_rub),
     NULL) AS contribution_pre_cogs_rub,
  IF(COUNTIF(NOT x.has_cogs) = 0, SUM(x.product_cogs_rub), NULL) AS product_cogs_rub,
  IF(COUNTIF(NOT x.has_cogs) = 0, 'COMPLETE', IF(COUNTIF(x.has_cogs) = 0, 'NOT_AVAILABLE', 'PARTIAL')) AS cogs_coverage,
  IF(COUNTIF(NOT x.has_cogs) = 0 AND SUM(x.unclassified_rub) = 0,
     SUM(x.seller_revenue_rub) - SUM(x.commission_rub) + SUM(x.logistics_rub) + SUM(x.acquiring_rub)
       + SUM(x.return_logistics_rub) + SUM(x.other_costs_rub) - SUM(x.product_cogs_rub),
     NULL) AS contribution_after_cogs_rub
FROM costed x
GROUP BY x.order_date_msk, x.sku;
