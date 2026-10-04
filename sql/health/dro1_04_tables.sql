-- ============================================================================
-- DRO-1 · §4 Таблицы evetis_health (append-only журналы).
-- Док: docs/ops/DRO1_DETECTION_ALERTING_2026-10-04.md
--
-- IF NOT EXISTS: повторное применение файла не трогает накопленную историю.
-- Схема снимка выводится из TVF_DATA_HEALTH (WHERE FALSE) — колонки таблицы и
-- функции совпадают по построению; детектор пишет SELECT *, detector_run_id.
-- Изменение колонок TVF после первого деплоя требует ALTER TABLE ADD COLUMN
-- (описать в CHANGELOG.md), а не пересоздания: таблица хранит доказательства.
-- ============================================================================
CREATE TABLE IF NOT EXISTS `evetis_health.DATA_HEALTH_SNAPSHOT`
PARTITION BY DATE(evaluated_at)
CLUSTER BY pipeline_id
OPTIONS (
  partition_expiration_days = 400,
  description = 'DRO-1. Снимки состояния конвейеров, append-only: одна строка на конвейер на прогон детектора (каждые 30 мин). Источник V_DATA_HEALTH_CURRENT.')
AS
SELECT *, CAST(NULL AS STRING) AS detector_run_id
FROM `evetis_health.TVF_DATA_HEALTH`(CURRENT_TIMESTAMP())
WHERE FALSE;

CREATE TABLE IF NOT EXISTS `evetis_health.ALERT_DISPATCH_LOG` (
  dispatch_id   STRING    NOT NULL OPTIONS (description = 'drob_<МСК> — пакет алертов; drod_<дата> — дайджест; <run>_dispatch_error — сбой доставки'),
  kind          STRING    NOT NULL OPTIONS (description = 'ALERT_BATCH | DIGEST | DISPATCH_ERROR'),
  dispatched_at TIMESTAMP NOT NULL,
  quiet_hours   BOOL               OPTIONS (description = 'Пакет сформирован в тихие часы 23:00–07:00 МСК'),
  n_events      INT64,
  event_ids     ARRAY<STRING>      OPTIONS (description = 'wb_ops.OPS_ALERT_EVENT.alert_event_id, переданные в Cloud Monitoring'),
  query_label   STRING             OPTIONS (description = 'Метки задания-маркера, по которым срабатывает политика Cloud Monitoring'),
  summary       STRING)
PARTITION BY DATE(dispatched_at)
OPTIONS (
  partition_expiration_days = 400,
  description = 'DRO-1. Журнал передачи алертов в Cloud Monitoring (email). Передача ≠ доставка: факт письма подтверждает только Cloud Monitoring.');
