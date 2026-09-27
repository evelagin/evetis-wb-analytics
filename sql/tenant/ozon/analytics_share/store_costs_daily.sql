-- Начисления Ozon вне экономики доставленных заказов по суткам начисления, классу и уровню:
-- магазин, товар, отправления недоставленные / вне загруженной истории / с другим SKU.
-- Вместе с profitability_daily — все начисления. Знак — как у Ozon (расход < 0).
CREATE OR REPLACE VIEW `__tenant__.analytics_share.store_costs_daily`
OPTIONS(description = 'Начисления Ozon вне экономики доставленных заказов: сутки × класс × уровень. Расход < 0. Вместе с profitability_daily — все начисления.')
AS
SELECT s.event_date, s.accrual_class, s.cost_scope, SUM(s.amount_rub) AS amount_rub,
  SUM(s.commission_rub) AS commission_rub, SUM(s.accruals) AS accruals,
  SUM(s.unresolved_accruals) AS unresolved_accruals,
  COALESCE(ANY_VALUE(c.status), 'UNKNOWN') AS data_status
FROM `__tenant__.ozon_mart.FACT_OZON_STORE_COSTS_DAILY` s
LEFT JOIN `__tenant__.tenant_ops.V_COVERAGE_DAILY` c ON c.entity = 'finance_accrual' AND c.coverage_date = s.event_date
GROUP BY s.event_date, s.accrual_class, s.cost_scope;
