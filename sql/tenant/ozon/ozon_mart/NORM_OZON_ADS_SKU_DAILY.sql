-- Атрибуция рекламы по SKU из отчёта Performance: сутки × кампания × SKU. Это атрибутированные
-- траты отчёта, а не списанные деньги (биллинг — начисления класса PROMOTION_BILLING).
-- Одна строка на ключ записи RAW — последняя выгрузка.
CREATE OR REPLACE VIEW `__tenant__.ozon_mart.NORM_OZON_ADS_SKU_DAILY`
OPTIONS(description = 'Атрибуция рекламы Performance: сутки × кампания × SKU. Не биллинг.')
AS
SELECT s.date AS stat_date, s.campaign_id, s.sku, s.attributed_spend_rub, s.impressions, s.clicks,
  s.cart_adds, s.orders, s.revenue_promo_rub, s.ordered_total_rub, s.attribution_status, s.extracted_at
FROM `__tenant__.ozon_raw.RAW_OZON_ADS_SKU_DAILY` s
QUALIFY ROW_NUMBER() OVER (PARTITION BY s.date, s.campaign_id, s.sku ORDER BY s.extracted_at DESC) = 1;
