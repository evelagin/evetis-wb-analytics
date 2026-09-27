-- Снимки остатков FBO. История есть только с первого снимка: API прошлых остатков не отдаёт.
CREATE OR REPLACE VIEW `__tenant__.ozon_mart.SNAP_OZON_STOCK`
OPTIONS(description = 'Снимок остатков FBO: дата снимка × SKU × склад (последнее извлечение за день).')
AS
SELECT s.snapshot_date, s.sku, s.warehouse_id, s.warehouse_name, s.cluster_id, s.cluster_name,
  s.available_stock_count, s.valid_stock_count, s.transit_stock_count, s.excess_stock_count,
  s.days_without_sales, s.turnover_grade, s.extracted_at
FROM `__tenant__.ozon_raw.RAW_OZON_STOCKS` s
QUALIFY ROW_NUMBER() OVER (PARTITION BY s.snapshot_date, s.sku, s.warehouse_id ORDER BY s.extracted_at DESC) = 1;
