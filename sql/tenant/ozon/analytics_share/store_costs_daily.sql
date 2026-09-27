-- Расходы магазина вне отправлений по суткам начисления: подписка, продвижение (биллинг),
-- хранение, логистика магазина, компенсации. Знак — как у Ozon (расход < 0).
CREATE OR REPLACE VIEW `__tenant__.analytics_share.store_costs_daily`
OPTIONS(description = 'Начисления Ozon вне отправлений: сутки × класс (подписка, биллинг продвижения, хранение, компенсации). Расход < 0.')
AS
SELECT s.event_date, s.accrual_class, SUM(s.amount_rub) AS amount_rub, SUM(s.accruals) AS accruals,
  COALESCE(ANY_VALUE(c.status), 'UNKNOWN') AS data_status
FROM `__tenant__.ozon_mart.FACT_OZON_STORE_COSTS_DAILY` s
LEFT JOIN `__tenant__.tenant_ops.V_COVERAGE_DAILY` c ON c.entity = 'finance_accrual' AND c.coverage_date = s.event_date
GROUP BY s.event_date, s.accrual_class;
