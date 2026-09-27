-- Траты рекламных кампаний по суткам (отчёт Performance).
CREATE OR REPLACE VIEW `__tenant__.ozon_mart.NORM_OZON_ADS_CAMPAIGN_DAILY`
OPTIONS(description = 'Реклама Performance: сутки × кампания, траты и показатели отчёта.')
AS
SELECT e.date AS stat_date, e.campaign_id, e.campaign_title, e.expense_rub, e.bonus_expense_rub,
  e.subscription_expense_rub, e.impressions, e.clicks, e.orders, e.revenue_rub, e.extracted_at
FROM `__tenant__.ozon_raw.RAW_OZON_ADS_EXPENSE_DAILY` e;
