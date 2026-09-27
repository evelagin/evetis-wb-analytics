-- Последний статус полноты по сущности и суткам. При равном времени оценки побеждает худший
-- статус (не COMPLETE). Отсутствие строки у потребителя = UNKNOWN.
CREATE OR REPLACE VIEW `__tenant__.tenant_ops.V_COVERAGE_DAILY`
OPTIONS(description = 'Статус полноты: сущность × сутки (последняя оценка; при равенстве — худшая).')
AS
SELECT c.entity, c.coverage_date, c.status, c.reason, c.evaluated_at
FROM `__tenant__.tenant_ops.DATA_COVERAGE` c
QUALIFY ROW_NUMBER() OVER (PARTITION BY c.entity, c.coverage_date
                           ORDER BY c.evaluated_at DESC, IF(c.status = 'COMPLETE', 1, 0), c.status) = 1;
