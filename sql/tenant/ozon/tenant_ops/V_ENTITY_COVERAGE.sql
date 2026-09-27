-- Покрытие данных по сущностям: границы по статусам и последняя оценка. Нет строк покрытия —
-- статус UNKNOWN у потребителей (не COMPLETE и не 0).
CREATE OR REPLACE VIEW `__tenant__.tenant_ops.V_ENTITY_COVERAGE`
OPTIONS(description = 'Покрытие по сущностям: первые и последние дни по статусам, число дней в каждом статусе.')
AS
WITH latest AS (
  SELECT c.entity, c.coverage_date, c.status, c.reason, c.evaluated_at
  FROM `__tenant__.tenant_ops.DATA_COVERAGE` c
  QUALIFY ROW_NUMBER() OVER (PARTITION BY c.entity, c.coverage_date ORDER BY c.evaluated_at DESC) = 1
)
SELECT l.entity,
  MIN(IF(l.status = 'COMPLETE', l.coverage_date, NULL)) AS first_complete_date,
  MAX(IF(l.status = 'COMPLETE', l.coverage_date, NULL)) AS last_complete_date,
  COUNTIF(l.status = 'COMPLETE') AS complete_days,
  COUNTIF(l.status = 'PARTIAL') AS partial_days,
  COUNTIF(l.status = 'NOT_AVAILABLE') AS not_available_days,
  COUNTIF(l.status = 'NOT_APPLICABLE') AS not_applicable_days,
  COUNTIF(l.status = 'FAILED') AS failed_days,
  COUNTIF(l.status = 'UNKNOWN') AS unknown_days,
  MAX(l.evaluated_at) AS last_evaluated_at
FROM latest l
GROUP BY l.entity;
