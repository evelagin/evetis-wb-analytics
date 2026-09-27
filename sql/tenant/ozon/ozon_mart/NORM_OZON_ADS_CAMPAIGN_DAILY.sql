-- Траты кампаний по отчёту Performance: сутки × кампания. Одна строка на ключ записи RAW —
-- последняя выгрузка.
CREATE OR REPLACE VIEW `__tenant__.ozon_mart.NORM_OZON_ADS_CAMPAIGN_DAILY`
OPTIONS(description = 'Траты кампаний по отчёту Performance: сутки × кампания.')
AS
SELECT e.date AS stat_date, e.campaign_id, e.campaign_title, e.expense_rub, e.bonus_expense_rub,
  e.subscription_expense_rub, e.impressions, e.clicks, e.orders, e.revenue_rub, e.extracted_at
FROM `__tenant__.ozon_raw.RAW_OZON_ADS_EXPENSE_DAILY` e
QUALIFY ROW_NUMBER() OVER (PARTITION BY e.date, e.campaign_id ORDER BY e.extracted_at DESC) = 1;
