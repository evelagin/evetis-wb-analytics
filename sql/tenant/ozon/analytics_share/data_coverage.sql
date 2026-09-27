-- Полнота данных по сущностям и суткам — чтобы потребитель (BI, AI-ассистент) отличал «ноль»
-- от «нет данных». Сутки без оценки здесь отсутствуют и означают UNKNOWN.
CREATE OR REPLACE VIEW `__tenant__.analytics_share.data_coverage`
OPTIONS(description = 'Полнота данных: сущность × сутки (COMPLETE, PARTIAL, NOT_AVAILABLE, NOT_APPLICABLE, FAILED). Нет строки — UNKNOWN.')
AS
SELECT c.entity, c.coverage_date, c.status, c.reason, c.evaluated_at
FROM `__tenant__.tenant_ops.V_COVERAGE_DAILY` c;
