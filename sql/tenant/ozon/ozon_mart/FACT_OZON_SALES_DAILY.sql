-- Заказы по суткам заказа (Москва) × SKU из текущего состояния отправлений.
-- ordered_* — все заказы дня; cancelled/delivered — их текущий исход; в пути — остальное.
-- Пустое количество или цена в строке отправления делает сумму суток NULL, а не заниженной.
CREATE OR REPLACE VIEW `__tenant__.ozon_mart.FACT_OZON_SALES_DAILY`
OPTIONS(description = 'Заказы: сутки заказа (Москва) × SKU; исход — по текущему статусу отправлений.')
AS
SELECT p.order_date_msk, p.sku,
  COUNT(DISTINCT p.posting_number) AS postings,
  IF(COUNTIF(p.quantity IS NULL) = 0, SUM(p.quantity), NULL) AS ordered_units,
  IF(COUNTIF(p.quantity IS NULL OR p.price_rub IS NULL) = 0, SUM(p.price_rub * p.quantity), NULL) AS ordered_value_rub,
  IF(COUNTIF(p.quantity IS NULL) = 0, SUM(IF(p.is_cancelled, p.quantity, 0)), NULL) AS cancelled_units,
  IF(COUNTIF(p.quantity IS NULL) = 0, SUM(IF(p.is_delivered, p.quantity, 0)), NULL) AS delivered_units,
  IF(COUNTIF(p.quantity IS NULL) = 0, SUM(IF(NOT p.is_cancelled AND NOT p.is_delivered, p.quantity, 0)), NULL) AS in_progress_units,
  IF(COUNTIF(p.is_delivered AND (p.quantity IS NULL OR p.price_rub IS NULL)) = 0,
     SUM(IF(p.is_delivered, p.price_rub * p.quantity, 0)), NULL) AS delivered_value_rub
FROM `__tenant__.ozon_mart.NORM_OZON_POSTING_LINE` p
GROUP BY p.order_date_msk, p.sku;
