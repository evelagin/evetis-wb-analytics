-- ============================================================================
-- DRO-1 · §6 Read model: «каким данным можно доверять сейчас» одним запросом.
-- Док: docs/ops/DRO1_DETECTION_ALERTING_2026-10-04.md
--
--   SELECT * FROM evetis_health.V_DATA_HEALTH_CURRENT WHERE serving_status <> 'HEALTHY'
--
-- Читает последний снимок детектора, а не пересчитывает пробы: дёшево для частых
-- чтений (будущий AI-ассистент) и совпадает с тем, по чему ушли алерты.
-- 🔴 Устаревание самого детектора: если последний снимок старше 60 минут (2×
-- интервал 30 мин) или его нет вовсе, serving_status = UNKNOWN, а не прежний
-- HEALTHY. Это закрывает дефект V_OPS_CURRENT_HEALTH, где проверки, не
-- запускавшиеся 12 дней, показывались HEALTHY.
-- ============================================================================
CREATE OR REPLACE VIEW `evetis_health.V_DATA_HEALTH_CURRENT`
OPTIONS (description = 'DRO-1. Текущее состояние каждого критичного конвейера EVETIS (WB, Ozon, производные): serving / data / run status, свежесть, причина, серьёзность. Устаревший детектор → UNKNOWN. Только чтение.') AS
WITH
contract AS (
  SELECT pipeline_id, display_name, marketplace, tenant_id, domain, source_system, data_class, criticality,
         recoverability, freshness_basis, cadence, evaluation_mode, alerting_enabled, contract_version
  FROM `evetis_health.V_PIPELINE_CONTRACT`),
last_run AS (
  SELECT MAX(evaluated_at) AS ts
  FROM `evetis_health.DATA_HEALTH_SNAPSHOT`
  WHERE DATE(evaluated_at) >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)),
latest_eval AS (
  SELECT s.*
  FROM `evetis_health.DATA_HEALTH_SNAPSHOT` s
  JOIN last_run r ON s.evaluated_at = r.ts
  WHERE DATE(s.evaluated_at) >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)),
latest_eval_aged AS (
  SELECT c.*,
         e.* EXCEPT (pipeline_id, display_name, marketplace, tenant_id, domain, source_system, data_class,
                     criticality, recoverability, freshness_basis, cadence, evaluation_mode, alerting_enabled,
                     contract_version),
         TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), e.evaluated_at, MINUTE) AS detector_age_minutes,
         60 AS detector_max_age_minutes
  FROM contract c
  LEFT JOIN latest_eval e ON e.pipeline_id = c.pipeline_id),
-- 🧪 sqlite-тестируемо: только CASE и сравнения.
overlay AS (
  SELECT a.*,
         CASE
           WHEN a.evaluated_at IS NULL THEN 'DETECTOR_NEVER_RAN'
           WHEN a.detector_age_minutes > a.detector_max_age_minutes THEN 'DETECTOR_STALE'
         END AS detector_issue
  FROM latest_eval_aged a),
effective AS (
  SELECT o.*,
         CASE WHEN o.detector_issue IS NOT NULL THEN 'UNKNOWN' ELSE o.serving_status END AS serving_status_effective,
         CASE WHEN o.detector_issue IS NOT NULL THEN o.detector_issue ELSE o.reason_code END AS reason_code_effective,
         CASE
           WHEN o.detector_issue IS NOT NULL AND o.evaluation_mode = 'EVALUATED' THEN 'CRITICAL'
           WHEN o.detector_issue IS NOT NULL THEN 'INFO'
           ELSE o.severity
         END AS severity_effective
  FROM overlay o),
open_incident AS (
  SELECT scope_id AS pipeline_id,
         COUNT(*) AS open_incidents,
         ARRAY_AGG(STRUCT(incident_id, severity, reason_code, first_seen_at) ORDER BY first_seen_at DESC LIMIT 1)[OFFSET(0)] AS latest
  FROM `wb_ops.OPS_INCIDENT`
  WHERE scope = 'DRO_PIPELINE' AND state = 'OPEN'
  GROUP BY scope_id)
SELECT
  f.pipeline_id, f.display_name, f.marketplace, f.tenant_id, f.domain,
  f.source_system AS source, f.data_class, f.criticality, f.recoverability,
  f.freshness_basis, f.cadence, f.expected_slot,
  f.latest_business_date, f.latest_success_load_ts, f.age_minutes,
  f.target_business_date, f.first_missing_date, f.slot_late_minutes, f.loss_deadline_msk, f.minutes_to_loss,
  f.completeness_status, f.completeness_reason,
  f.run_status, f.run_error_code, f.run_started_at, f.run_finished_at,
  IF(f.detector_issue IS NOT NULL, 'UNKNOWN', f.data_status) AS data_status,
  f.serving_status_effective AS serving_status,
  f.severity_effective AS severity,
  f.reason_code_effective AS reason_code,
  CASE f.detector_issue
    WHEN 'DETECTOR_NEVER_RAN' THEN 'Детектор DRO-1 ещё не записал ни одного снимка'
    WHEN 'DETECTOR_STALE' THEN CONCAT('Последняя оценка ', CAST(f.detector_age_minutes AS STRING),
                                      ' мин назад (допуск ', CAST(f.detector_max_age_minutes AS STRING),
                                      '): состояние неизвестно')
    ELSE f.reason_text
  END AS reason_text,
  f.caveats,
  f.open_recoverable_gaps, f.unrecoverable_gaps_35d, f.last_unrecoverable_period,
  f.recovered_periods_35d, f.last_recovered_period, f.provisional_stuck_count,
  f.known_source_limitation,
  f.evaluation_mode, f.not_evaluated_reason, f.alerting_enabled,
  IFNULL(i.open_incidents, 0) AS open_incidents,
  i.latest.incident_id AS open_incident_id,
  f.evaluated_at, f.detector_age_minutes, f.detector_run_id,
  f.serving_status AS serving_status_at_evaluation,
  f.reason_code AS reason_code_at_evaluation,
  f.evidence_ref, f.contract_version
FROM effective f
LEFT JOIN open_incident i ON i.pipeline_id = f.pipeline_id;

-- ── Все проверки здоровья (ADS-1B, расширенная, DRO-1) с устареванием ────────
CREATE OR REPLACE VIEW `evetis_health.V_HEALTH_CHECK_CURRENT`
OPTIONS (description = 'DRO-1. Последний результат каждой проверки wb_ops.OPS_HEALTH_STATE с учётом устаревания: проверка старше 2× своего интервала → UNKNOWN (DETECTOR_STALE), а не прежний HEALTHY. Только чтение.') AS
WITH
checks_aged AS (
  SELECT h.pipeline_id, h.check_id, h.scope, h.scope_id, h.health_status, h.reason_code, h.reason,
         h.severity, h.last_checked_at,
         CASE
           WHEN h.scope = 'DRO_PIPELINE' THEN 'DRO1'
           WHEN REGEXP_CONTAINS(IFNULL(h.check_id, ''), r'^H[0-6]_') THEN 'ADS1B'
           ELSE 'HEALTH_EXT'
         END AS detector,
         -- DRO-1 — каждые 30 мин; ADS-1B и расширенная проверка спроектированы на 3 ч.
         IF(h.scope = 'DRO_PIPELINE', 30, 180) AS expected_interval_minutes,
         TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), h.last_checked_at, MINUTE) AS check_age_minutes
  FROM `wb_ops.V_OPS_CURRENT_HEALTH` h),
-- 🧪 sqlite-тестируемо.
checks_classified AS (
  SELECT c.*,
         CASE
           WHEN c.check_age_minutes > 2 * c.expected_interval_minutes THEN 'UNKNOWN'
           WHEN c.health_status = 'HEALTHY' THEN 'HEALTHY'
           ELSE 'UNHEALTHY'
         END AS effective_status,
         CASE
           WHEN c.check_age_minutes > 2 * c.expected_interval_minutes THEN 'DETECTOR_STALE'
         END AS staleness_reason
  FROM checks_aged c)
SELECT detector, pipeline_id, check_id, scope, scope_id, effective_status, staleness_reason,
       health_status AS health_status_at_check, reason_code, reason, severity,
       last_checked_at, check_age_minutes, expected_interval_minutes
FROM checks_classified;

-- ── Состояние данных по суткам за 35 дней, «сейчас» ──────────────────────────
CREATE OR REPLACE VIEW `evetis_health.V_DATA_PERIOD_STATE`
OPTIONS (description = 'DRO-1. Состояние данных по суткам за 35 дней (RECOVERED / MISSING_UNRECOVERABLE / PROVISIONAL_STUCK и др.), вычисляется при чтении. Только чтение.') AS
SELECT * FROM `evetis_health.TVF_DATA_PERIOD_STATE`(CURRENT_TIMESTAMP());
