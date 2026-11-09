-- История остатков FBO по снимкам: дата снимка × SKU (сумма по складам). Начинается с первого
-- снимка; дней без снимка в истории нет (это не ноль и не «не менялся»). SKU без строки в полном
-- снимке (stock_data_status = COMPLETE) — нет остатка на складах FBO.
CREATE OR REPLACE VIEW `__tenant__.analytics_share.stock_daily`
OPTIONS(description = 'Остатки FBO по датам снимков × SKU: доступно, годно, в пути, склады и кластеры с остатком; stock_data_status — полнота снимка.')
AS
SELECT s.snapshot_date, s.sku, d.internal_sku, d.product_name, d.offer_id, d.ozon_name,
  SUM(s.available_stock_count) AS available_units, SUM(s.valid_stock_count) AS valid_units,
  SUM(s.transit_stock_count) AS transit_units,
  COUNT(DISTINCT IF(s.available_stock_count > 0, s.warehouse_id, NULL)) AS warehouses_with_stock,
  COUNT(DISTINCT IF(s.available_stock_count > 0, s.cluster_name, NULL)) AS clusters_with_stock,
  COALESCE(ANY_VALUE(c.status), 'UNKNOWN') AS stock_data_status
FROM `__tenant__.ozon_mart.SNAP_OZON_STOCK` s
LEFT JOIN `__tenant__.ozon_mart.DIM_OZON_PRODUCT` d ON d.sku_joinable AND d.sku = s.sku
LEFT JOIN `__tenant__.tenant_ops.V_COVERAGE_DAILY` c ON c.entity = 'stocks' AND c.coverage_date = s.snapshot_date
GROUP BY s.snapshot_date, s.sku, d.internal_sku, d.product_name, d.offer_id, d.ozon_name;
