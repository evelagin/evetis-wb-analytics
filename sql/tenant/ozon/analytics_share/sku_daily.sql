-- Товар по суткам: заказы (сутки заказа, Москва) и реклама (сутки отчёта Performance) на общей
-- оси суток. Реклама — атрибуция отчёта по SKU, а не списанные деньги; товар с рекламой, но без
-- заказов в эти сутки, сохраняется (реклама не теряется). data_status — полнота отправлений,
-- ads_data_status — полнота рекламы по SKU за сутки.
CREATE OR REPLACE VIEW `__tenant__.analytics_share.sku_daily`
OPTIONS(description = 'Товар × сутки: заказы и исход, атрибуция рекламы по SKU, ДРР от стоимости заказов, статусы полноты.')
AS
WITH ads AS (
  SELECT a.stat_date, a.sku, SUM(a.attributed_spend_rub) AS ad_spend_attributed_rub,
    SUM(a.orders) AS ad_orders, SUM(a.revenue_promo_rub) AS ad_revenue_rub, SUM(a.clicks) AS ad_clicks
  FROM `__tenant__.ozon_mart.NORM_OZON_ADS_SKU_DAILY` a
  GROUP BY a.stat_date, a.sku
),
keyed AS (
  SELECT COALESCE(f.order_date_msk, a.stat_date) AS sales_date, COALESCE(f.sku, a.sku) AS sku,
    f.postings, f.ordered_units, f.ordered_value_rub, f.cancelled_units, f.delivered_units,
    f.delivered_value_rub, a.ad_spend_attributed_rub, a.ad_orders, a.ad_revenue_rub, a.ad_clicks
  FROM `__tenant__.ozon_mart.FACT_OZON_SALES_DAILY` f
  FULL OUTER JOIN ads a ON a.stat_date = f.order_date_msk AND a.sku = f.sku
)
SELECT k.sales_date, k.sku, d.internal_sku, d.product_name, COALESCE(d.mapping_status, 'UNMAPPED') AS mapping_status,
  k.postings AS orders, k.ordered_units, k.ordered_value_rub, k.cancelled_units, k.delivered_units,
  k.delivered_value_rub, k.ad_spend_attributed_rub, k.ad_orders, k.ad_revenue_rub, k.ad_clicks,
  SAFE_DIVIDE(k.ad_spend_attributed_rub, k.ordered_value_rub) AS drr_ratio,
  COALESCE(cp.status, 'UNKNOWN') AS data_status,
  COALESCE(ca.status, 'UNKNOWN') AS ads_data_status
FROM keyed k
LEFT JOIN `__tenant__.ozon_mart.DIM_OZON_PRODUCT` d ON d.sku = k.sku
LEFT JOIN `__tenant__.tenant_ops.V_COVERAGE_DAILY` cp ON cp.entity = 'fbo_postings' AND cp.coverage_date = k.sales_date
LEFT JOIN `__tenant__.tenant_ops.V_COVERAGE_DAILY` ca ON ca.entity = 'ads_sku_daily' AND ca.coverage_date = k.sales_date;
