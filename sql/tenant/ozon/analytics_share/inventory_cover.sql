-- Запас в днях по товару: остаток FBO последнего снимка (сумма по складам) ÷ средние чистые заказы
-- (заказано − отменено) за 14 суток, оканчивающихся последними ПОЛНЫМИ сутками отправлений.
-- Скорость считается только при 14 полных сутках базы, иначе NULL (нет данных ≠ ноль). Товар без
-- заказов в полной базе — скорость 0, запас в днях NULL («не расходуется»). Товар с заказами, но
-- без строки в полном снимке — остаток 0 (закончился). velocity_data_status = COMPLETE, только
-- если база полна и отстаёт от снимка не больше чем на 2 суток; иначе PARTIAL (причина —
-- velocity_status_reason); полных суток отправлений нет — UNKNOWN.
CREATE OR REPLACE VIEW `__tenant__.analytics_share.inventory_cover`
OPTIONS(description = 'Запас в днях: товар × последний снимок. Остаток FBO, средние чистые заказы за 14 полных суток, запас в днях; статусы полноты остатков и скорости.')
AS
WITH last_snap AS (
  SELECT MAX(s.snapshot_date) AS snapshot_date FROM `__tenant__.ozon_mart.SNAP_OZON_STOCK` s
),
stock AS (
  SELECT s.sku, SUM(s.available_stock_count) AS available_units, SUM(s.transit_stock_count) AS transit_units,
    COUNT(DISTINCT IF(s.available_stock_count > 0, s.cluster_name, NULL)) AS clusters_with_stock
  FROM `__tenant__.ozon_mart.SNAP_OZON_STOCK` s
  JOIN last_snap l ON s.snapshot_date = l.snapshot_date
  GROUP BY s.sku
),
cov AS (
  SELECT c.coverage_date, c.status
  FROM `__tenant__.tenant_ops.V_COVERAGE_DAILY` c
  WHERE c.entity = 'fbo_postings'
),
basis AS (
  SELECT MAX(IF(v.status = 'COMPLETE', v.coverage_date, NULL)) AS basis_to FROM cov v
),
basis_days AS (
  SELECT b.basis_to, DATE_SUB(b.basis_to, INTERVAL 13 DAY) AS basis_from,
    COUNTIF(v.status = 'COMPLETE' AND v.coverage_date >= DATE_SUB(b.basis_to, INTERVAL 13 DAY)
            AND v.coverage_date <= b.basis_to) AS complete_basis_days
  FROM basis b
  CROSS JOIN cov v
  WHERE b.basis_to IS NOT NULL
  GROUP BY b.basis_to
),
sold AS (
  SELECT f.sku,
    IF(COUNTIF(f.ordered_units IS NULL OR f.cancelled_units IS NULL) = 0,
       SUM(f.ordered_units - f.cancelled_units), NULL) AS net_units
  FROM `__tenant__.ozon_mart.FACT_OZON_SALES_DAILY` f
  CROSS JOIN basis_days bd
  WHERE f.order_date_msk >= bd.basis_from AND f.order_date_msk <= bd.basis_to
  GROUP BY f.sku
),
stock_cov AS (
  SELECT c.status
  FROM `__tenant__.tenant_ops.V_COVERAGE_DAILY` c
  JOIN last_snap l ON c.coverage_date = l.snapshot_date
  WHERE c.entity = 'stocks'
),
skus AS (SELECT sku FROM stock UNION DISTINCT SELECT sku FROM sold),
joined AS (
  SELECT l.snapshot_date, k.sku,
    IF(st.sku IS NULL AND sc.status = 'COMPLETE', 0, st.available_units) AS available_units,
    IF(st.sku IS NULL AND sc.status = 'COMPLETE', 0, st.transit_units) AS transit_units,
    st.clusters_with_stock, sc.status AS stock_status,
    bd.basis_from, bd.basis_to, bd.complete_basis_days,
    DATE_DIFF(l.snapshot_date, bd.basis_to, DAY) AS basis_lag_days,
    IF(bd.complete_basis_days = 14, IF(so.sku IS NULL, 0, so.net_units), NULL) AS net_units_basis
  FROM skus k
  CROSS JOIN last_snap l
  LEFT JOIN stock st ON st.sku = k.sku
  LEFT JOIN sold so ON so.sku = k.sku
  LEFT JOIN stock_cov sc ON TRUE
  LEFT JOIN basis_days bd ON TRUE
),
judged AS (
  SELECT j.*, SAFE_DIVIDE(j.net_units_basis, 14) AS avg_daily_units,
    CASE
      WHEN j.basis_to IS NULL THEN NULL
      WHEN j.complete_basis_days < 14 THEN 'PARTIAL'
      WHEN j.basis_lag_days > 2 THEN 'PARTIAL'
      ELSE 'COMPLETE'
    END AS velocity_status,
    CASE
      WHEN j.basis_to IS NULL THEN 'NO_COMPLETE_POSTINGS'
      WHEN j.complete_basis_days < 14 THEN 'BASIS_INCOMPLETE'
      WHEN j.basis_lag_days > 2 THEN 'BASIS_STALE'
    END AS velocity_reason
  FROM joined j
)
SELECT g.snapshot_date, g.sku, d.internal_sku, d.product_name, d.offer_id, d.ozon_name,
  g.available_units, g.transit_units, g.clusters_with_stock,
  g.basis_from AS velocity_basis_from, g.basis_to AS velocity_basis_to, g.basis_lag_days AS velocity_basis_lag_days,
  g.net_units_basis AS net_ordered_units_basis, g.avg_daily_units,
  IF(g.avg_daily_units > 0, SAFE_DIVIDE(g.available_units, g.avg_daily_units), NULL) AS days_of_cover,
  g.velocity_reason AS velocity_status_reason,
  COALESCE(g.stock_status, 'UNKNOWN') AS stock_data_status,
  COALESCE(g.velocity_status, 'UNKNOWN') AS velocity_data_status
FROM judged g
LEFT JOIN `__tenant__.ozon_mart.DIM_OZON_PRODUCT` d ON d.sku_joinable AND d.sku = g.sku;
