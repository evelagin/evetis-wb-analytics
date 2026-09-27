-- DQ: нераспознанные начисления по суткам и type_id — тип вне таксономии или пустая сумма.
-- blocking_accruals > 0 (тип вне таксономии с ненулевой суммой или сумма неизвестна)
-- останавливает финансовый результат затронутых строк до классификации типа (новая версия
-- таксономии в пакете, одинаковая для всех арендаторов).
CREATE OR REPLACE VIEW `__tenant__.tenant_ops.V_DQ_UNRESOLVED_ACCRUALS`
OPTIONS(description = 'DQ: начисления вне таксономии или с пустой суммой по суткам и type_id; blocking_accruals останавливает результат.')
AS
SELECT a.event_date, a.type_id, ANY_VALUE(a.operation_name) AS operation_name,
  LOGICAL_OR(a.is_unclassified) AS is_unclassified,
  SUM(a.amount_rub) AS amount_rub, SUM(ABS(a.amount_rub)) AS gross_amount_rub, COUNT(*) AS accruals,
  COUNTIF(a.amount_rub IS NULL) AS empty_amount_accruals,
  COUNTIF(a.is_unresolved) AS blocking_accruals
FROM `__tenant__.ozon_mart.NORM_OZON_ACCRUAL` a
WHERE a.is_unclassified OR a.amount_rub IS NULL
GROUP BY a.event_date, a.type_id;
