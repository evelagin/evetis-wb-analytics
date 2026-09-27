-- Заказы по суткам заказа (Москва) × SKU из текущего состояния отправлений.
-- ordered_* — все заказы дня; cancelled/delivered — их текущий исход; в пути — остальное.
CREATE OR REPLACE VIEW `__tenant__.ozon_mart.FACT_OZON_SALES_DAILY`
OPTIONS(description = 'Заказы: сутки заказа (Москва) × SKU; исход — по текущему статусу отправлений.')
AS
SELECT p.order_date_msk, p.sku,
  COUNT(DISTINCT p.posting_number) AS postings,
  SUM(p.quantity) AS ordered_units,
  SUM(p.price_rub * p.quantity) AS ordered_value_rub,
  SUM(IF(p.is_cancelled, p.quantity, 0)) AS cancelled_units,
  SUM(IF(p.is_delivered, p.quantity, 0)) AS delivered_units,
  SUM(IF(NOT p.is_cancelled AND NOT p.is_delivered, p.quantity, 0)) AS in_progress_units,
  SUM(IF(p.is_delivered, p.price_rub * p.quantity, 0)) AS delivered_value_rub
FROM `__tenant__.ozon_mart.NORM_OZON_POSTING_LINE` p
GROUP BY p.order_date_msk, p.sku;
