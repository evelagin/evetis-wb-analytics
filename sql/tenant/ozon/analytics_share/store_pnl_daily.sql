-- Результат магазина по суткам НАЧИСЛЕНИЯ: все начисления Ozon (выручка и комиссия продажи,
-- логистика, хранение, продвижение, подписка, компенсации и т. д.) × уровень. Сумма amount_rub
-- суток — результат до себестоимости по дате начисления, если data_status = COMPLETE и
-- unknown_amounts = 0. NULL — «неизвестно», не ноль; известная часть — amount_known_rub.
CREATE OR REPLACE VIEW `__tenant__.analytics_share.store_pnl_daily`
OPTIONS(description = 'Результат магазина по дате начисления: сутки × вид строки (выручка, комиссия продажи, класс начисления) × уровень. Знак Ozon; data_status — полнота начислений за сутки.')
AS
SELECT f.event_date, f.line_class, f.cost_scope,
  IF(SUM(f.unknown_amounts) = 0, SUM(f.amount_rub), NULL) AS amount_rub,
  SUM(f.amount_known_rub) AS amount_known_rub,
  SUM(f.accruals) AS accruals,
  SUM(f.posting_lines) AS posting_lines,
  SUM(f.unknown_amounts) AS unknown_amounts,
  SUM(f.unresolved_accruals) AS unresolved_accruals,
  COALESCE(ANY_VALUE(c.status), 'UNKNOWN') AS data_status
FROM `__tenant__.ozon_mart.FACT_OZON_ACCRUAL_DAILY` f
LEFT JOIN `__tenant__.tenant_ops.V_COVERAGE_DAILY` c ON c.entity = 'finance_accrual' AND c.coverage_date = f.event_date
GROUP BY f.event_date, f.line_class, f.cost_scope;
