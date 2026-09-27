-- Продажи магазина по суткам заказа (Москва). Сутки берутся из фактов и из покрытия загрузки:
-- сутки без заказов показываются нулями только при data_status = COMPLETE, иначе NULL
-- (нет данных ≠ ноль). data_status — полнота загрузки отправлений за сутки.
CREATE OR REPLACE VIEW `__tenant__.analytics_share.sales_daily`
OPTIONS(description = 'Продажи магазина: сутки заказа (Москва). Заказы, единицы, стоимость, исход. Нули — только при полных данных; data_status — полнота за сутки.')
AS
WITH facts AS (
  SELECT f.order_date_msk AS sales_date, SUM(f.postings) AS orders, SUM(f.ordered_units) AS ordered_units,
    SUM(f.ordered_value_rub) AS ordered_value_rub, SUM(f.cancelled_units) AS cancelled_units,
    SUM(f.delivered_units) AS delivered_units, SUM(f.in_progress_units) AS in_progress_units,
    SUM(f.delivered_value_rub) AS delivered_value_rub
  FROM `__tenant__.ozon_mart.FACT_OZON_SALES_DAILY` f
  GROUP BY f.order_date_msk
),
cov AS (
  SELECT c.coverage_date AS sales_date, c.status
  FROM `__tenant__.tenant_ops.V_COVERAGE_DAILY` c
  WHERE c.entity = 'fbo_postings'
),
days AS (SELECT sales_date FROM facts UNION DISTINCT SELECT sales_date FROM cov)
SELECT d.sales_date,
  IF(f.sales_date IS NULL AND c.status = 'COMPLETE', 0, f.orders) AS orders,
  IF(f.sales_date IS NULL AND c.status = 'COMPLETE', 0, f.ordered_units) AS ordered_units,
  IF(f.sales_date IS NULL AND c.status = 'COMPLETE', 0, f.ordered_value_rub) AS ordered_value_rub,
  IF(f.sales_date IS NULL AND c.status = 'COMPLETE', 0, f.cancelled_units) AS cancelled_units,
  IF(f.sales_date IS NULL AND c.status = 'COMPLETE', 0, f.delivered_units) AS delivered_units,
  IF(f.sales_date IS NULL AND c.status = 'COMPLETE', 0, f.in_progress_units) AS in_progress_units,
  IF(f.sales_date IS NULL AND c.status = 'COMPLETE', 0, f.delivered_value_rub) AS delivered_value_rub,
  COALESCE(c.status, 'UNKNOWN') AS data_status
FROM days d
LEFT JOIN facts f ON f.sales_date = d.sales_date
LEFT JOIN cov c ON c.sales_date = d.sales_date;
