-- Остатки FBO по последнему снимку. snapshot_age_days — возраст снимка; прошлые остатки
-- не восстанавливаются (история — только с первого снимка).
CREATE OR REPLACE VIEW `__tenant__.analytics_share.inventory_current`
OPTIONS(description = 'Остатки FBO: последний снимок × SKU × склад, возраст снимка в днях.')
AS
WITH last_day AS (SELECT MAX(s.snapshot_date) AS d FROM `__tenant__.ozon_mart.SNAP_OZON_STOCK` s)
SELECT s.snapshot_date, s.sku, d.internal_sku, d.product_name, s.warehouse_id, s.warehouse_name,
  s.cluster_name, s.available_stock_count, s.valid_stock_count, s.transit_stock_count,
  s.days_without_sales, s.turnover_grade,
  DATE_DIFF(CURRENT_DATE('Europe/Moscow'), s.snapshot_date, DAY) AS snapshot_age_days
FROM `__tenant__.ozon_mart.SNAP_OZON_STOCK` s
JOIN last_day l ON s.snapshot_date = l.d
LEFT JOIN `__tenant__.ozon_mart.DIM_OZON_PRODUCT` d ON d.sku_joinable AND d.sku = s.sku;
