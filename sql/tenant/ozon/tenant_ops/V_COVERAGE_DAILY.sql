-- Последний статус полноты по сущности и суткам. При равном времени оценки побеждает худший
-- статус по шкале FAILED > UNKNOWN > PARTIAL > NOT_AVAILABLE > NOT_APPLICABLE > COMPLETE
-- (незнакомый статус — как FAILED). Отсутствие строки у потребителя = UNKNOWN.
CREATE OR REPLACE VIEW `__tenant__.tenant_ops.V_COVERAGE_DAILY`
OPTIONS(description = 'Статус полноты: сущность × сутки (последняя оценка; при равенстве — худшая).')
AS
SELECT c.entity, c.coverage_date, c.status, c.reason, c.evaluated_at
FROM `__tenant__.tenant_ops.DATA_COVERAGE` c
QUALIFY ROW_NUMBER() OVER (
  PARTITION BY c.entity, c.coverage_date
  ORDER BY c.evaluated_at DESC,
    CASE c.status WHEN 'FAILED' THEN 0 WHEN 'UNKNOWN' THEN 1 WHEN 'PARTIAL' THEN 2
                  WHEN 'NOT_AVAILABLE' THEN 3 WHEN 'NOT_APPLICABLE' THEN 4 WHEN 'COMPLETE' THEN 5 ELSE 0 END,
    c.status) = 1;
