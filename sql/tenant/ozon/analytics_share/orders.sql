-- Заказы (отправление × SKU) в текущем состоянии. Без данных покупателя, кроме города доставки.
CREATE OR REPLACE VIEW `__tenant__.analytics_share.orders`
OPTIONS(description = 'Заказы FBO: отправление × SKU, текущий статус, сутки заказа (Москва), цена и выплата Ozon.')
AS
SELECT p.posting_number, p.sku, d.internal_sku, d.product_name, p.order_date_msk AS order_date, p.status,
  p.is_cancelled, p.is_delivered, p.quantity, p.price_rub, p.old_price_rub, p.payout_rub,
  p.warehouse_name, p.city
FROM `__tenant__.ozon_mart.NORM_OZON_POSTING_LINE` p
LEFT JOIN `__tenant__.ozon_mart.DIM_OZON_PRODUCT` d ON d.sku_joinable AND d.sku = p.sku;
