-- Реклама по суткам и кампаниям (отчёт Performance). Это отчётные траты кампаний, а не
-- списания в финансах. data_status — полнота рекламных данных за сутки.
CREATE OR REPLACE VIEW `__tenant__.analytics_share.advertising_daily`
OPTIONS(description = 'Реклама: сутки × кампания по отчёту Performance (траты, показы, клики, заказы, выручка). data_status — полнота за сутки.')
AS
SELECT e.stat_date, e.campaign_id, e.campaign_title, e.expense_rub, e.bonus_expense_rub, e.impressions,
  e.clicks, e.orders, e.revenue_rub,
  SAFE_DIVIDE(e.expense_rub, e.revenue_rub) AS acos_ratio,
  COALESCE(c.status, 'UNKNOWN') AS data_status
FROM `__tenant__.ozon_mart.NORM_OZON_ADS_CAMPAIGN_DAILY` e
LEFT JOIN `__tenant__.tenant_ops.V_COVERAGE_DAILY` c
  ON c.entity = 'ads_expense_daily' AND c.coverage_date = e.stat_date;
