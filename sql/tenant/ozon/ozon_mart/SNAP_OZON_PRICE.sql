-- Снимки цен продавца. История — с первого снимка: API прошлых цен не отдаёт.
CREATE OR REPLACE VIEW `__tenant__.ozon_mart.SNAP_OZON_PRICE`
OPTIONS(description = 'Снимок цен: дата снимка × offer_id (последнее извлечение за день).')
AS
SELECT p.snapshot_date, p.offer_id, p.product_id, p.price_rub, p.old_price_rub, p.min_price_rub,
  p.marketing_seller_price_rub, p.retail_price_rub, p.acquiring_rub, p.sales_percent_fbo,
  p.volume_weight_l, p.vat_rate, p.currency_code, p.extracted_at
FROM `__tenant__.ozon_raw.RAW_OZON_PRICES` p
QUALIFY ROW_NUMBER() OVER (PARTITION BY p.snapshot_date, p.offer_id ORDER BY p.extracted_at DESC) = 1;
