-- Полнота начислений от даты d до последних оценённых суток. Начисления по заказу приходят
-- позже заказа, поэтому для суток заказа важна полнота не одного дня, а всего хвоста после
-- него: COMPLETE — каждые сутки [d, последние оценённые] оценены COMPLETE или NOT_APPLICABLE
-- без пропусков; иначе PARTIAL. Сутки без оценки здесь отсутствуют (у потребителя — UNKNOWN).
CREATE OR REPLACE VIEW `__tenant__.tenant_ops.V_FINANCE_WINDOW_STATUS`
OPTIONS(description = 'Полнота начислений Ozon от даты до последних оценённых суток: COMPLETE или PARTIAL.')
AS
WITH cov AS (
  SELECT c.coverage_date, c.status
  FROM `__tenant__.tenant_ops.V_COVERAGE_DAILY` c
  WHERE c.entity = 'finance_accrual'
),
tail AS (
  SELECT v.coverage_date AS from_date,
    COUNT(*) OVER (ORDER BY v.coverage_date ROWS BETWEEN CURRENT ROW AND UNBOUNDED FOLLOWING) AS evaluated_days,
    SUM(IF(v.status IN ('COMPLETE', 'NOT_APPLICABLE'), 0, 1))
      OVER (ORDER BY v.coverage_date ROWS BETWEEN CURRENT ROW AND UNBOUNDED FOLLOWING) AS incomplete_days,
    MAX(v.coverage_date) OVER () AS last_evaluated_date
  FROM cov v
)
SELECT t.from_date, t.last_evaluated_date, t.incomplete_days,
  DATE_DIFF(t.last_evaluated_date, t.from_date, DAY) + 1 - t.evaluated_days AS missing_days,
  IF(t.incomplete_days = 0 AND t.evaluated_days = DATE_DIFF(t.last_evaluated_date, t.from_date, DAY) + 1,
     'COMPLETE', 'PARTIAL') AS status
FROM tail t;
