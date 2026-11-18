-- Заказы (отправление × SKU) в текущем состоянии. Без данных покупателя, кроме города доставки.
-- status_observed_at — когда этот статус прочитан у Ozon (последняя выгрузка отправления): при
-- остановленной загрузке «в пути» устаревает, и это видно. data_status — полнота отправлений за сутки заказа.
CREATE OR REPLACE VIEW `__tenant__.analytics_share.orders`
OPTIONS(description = 'Заказы FBO: отправление × SKU, текущий статус, сутки заказа (Москва), цена и выплата Ozon.')
AS
SELECT p.posting_number, p.sku, d.internal_sku, d.product_name, d.offer_id, d.ozon_name, p.order_date_msk AS order_date, p.status,
  p.is_cancelled, p.is_delivered, p.quantity, p.price_rub, p.old_price_rub, p.payout_rub,
  p.warehouse_name, p.city, p.extracted_at AS status_observed_at,
  COALESCE(c.status, 'UNKNOWN') AS data_status
FROM `__tenant__.ozon_mart.NORM_OZON_POSTING_LINE` p
LEFT JOIN `__tenant__.ozon_mart.DIM_OZON_PRODUCT` d ON d.sku_joinable AND d.sku = p.sku
LEFT JOIN `__tenant__.tenant_ops.V_COVERAGE_DAILY` c ON c.entity = 'fbo_postings' AND c.coverage_date = p.order_date_msk;
