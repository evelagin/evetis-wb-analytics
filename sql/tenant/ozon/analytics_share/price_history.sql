-- История цен продавца по снимкам. Начинается с первого снимка после подключения: API прошлых
-- цен не отдаёт. Дней без снимка в этой истории нет (а не «цена не менялась»).
CREATE OR REPLACE VIEW `__tenant__.analytics_share.price_history`
OPTIONS(description = 'Цены продавца по датам снимков × offer_id. Начинается с первого снимка; пропуски — отсутствие снимка, а не неизменная цена.')
AS
SELECT p.snapshot_date, p.offer_id, p.product_id, p.price_rub, p.old_price_rub, p.min_price_rub,
  p.marketing_seller_price_rub, p.sales_percent_fbo, p.acquiring_rub,
  p.price_rub - LAG(p.price_rub) OVER (PARTITION BY p.offer_id ORDER BY p.snapshot_date) AS price_change_rub
FROM `__tenant__.ozon_mart.SNAP_OZON_PRICE` p;
