-- Атрибуция рекламы Performance по SKU и суткам. Это атрибуция отчётов, а не списанные деньги:
-- биллинг — начисления класса PROMOTION_BILLING.
CREATE OR REPLACE VIEW `__tenant__.ozon_mart.NORM_OZON_ADS_SKU_DAILY`
OPTIONS(description = 'Реклама Performance: сутки × кампания × SKU (атрибуция отчёта).')
AS
SELECT s.date AS stat_date, s.campaign_id, s.sku, s.attributed_spend_rub, s.impressions, s.clicks,
  s.cart_adds, s.orders, s.revenue_promo_rub, s.ordered_total_rub, s.attribution_status, s.extracted_at
FROM `__tenant__.ozon_raw.RAW_OZON_ADS_SKU_DAILY` s;
