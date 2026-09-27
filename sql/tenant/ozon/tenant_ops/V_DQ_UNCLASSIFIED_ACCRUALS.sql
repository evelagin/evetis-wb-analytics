-- Типы начислений вне таксономии с суммами. Ненулевая сумма останавливает публикацию
-- финансового результата соответствующих суток до классификации.
CREATE OR REPLACE VIEW `__tenant__.tenant_ops.V_DQ_UNCLASSIFIED_ACCRUALS`
OPTIONS(description = 'DQ: начисления вне таксономии по суткам и type_id; ненулевая сумма блокирует финансовый результат суток.')
AS
SELECT a.event_date, a.type_id, ANY_VALUE(a.operation_name) AS operation_name,
  SUM(a.amount_rub) AS amount_rub, SUM(ABS(a.amount_rub)) AS gross_amount_rub, COUNT(*) AS accruals
FROM `__tenant__.ozon_mart.NORM_OZON_ACCRUAL` a
WHERE a.is_unclassified
GROUP BY a.event_date, a.type_id;
