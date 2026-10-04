-- ============================================================================
-- DRO-1 · §3 Оценка: три независимых состояния.
-- Док: docs/ops/DRO1_DETECTION_ALERTING_2026-10-04.md
--
--   run_status     — что сделал ПОСЛЕДНИЙ запуск: SUCCESS | PARTIAL | FAILED | ABANDONED |
--                    RUNNING | SKIPPED | UNKNOWN. История запусков не переписывается.
--   data_status    — что лежит в таблице за ПЕРИОД: COMPLETE | RECOVERED | PROVISIONAL |
--                    PROVISIONAL_STUCK | PENDING | MISSING | MISSING_UNRECOVERABLE |
--                    INCOMPLETE | NOT_EXPECTED | UNKNOWN.
--   serving_status — можно ли пользоваться конвейером СЕЙЧАС: HEALTHY | DEGRADED | STALE |
--                    BLOCKED | UNKNOWN.
--
-- Главное правило: исторический FAILED запуска влияет на serving_status только через
-- data_status своего периода. Реклама 30.09: запуск ERROR/ADS_PARTIAL остаётся в журнале
-- как есть, период 30.09 → RECOVERED (данные пришли прогоном 02.10), конвейер → HEALTHY.
--
-- Классификация (CTE period_classified, classified_reason, classified) написана без
-- функций BigQuery: тесты исполняют ровно этот текст в sqlite на фикстурах
-- (tools/tests/test_dro1_health.py). Время в минутах считается заранее, в *_input.
-- ============================================================================

-- ── Состояние данных по периодам (конвейеры с суточным слотом), 35 суток ──────
CREATE OR REPLACE TABLE FUNCTION `evetis_health.TVF_DATA_PERIOD_STATE`(as_of TIMESTAMP)
OPTIONS (description = 'DRO-1. Состояние данных по суткам за 35 дней для конвейеров с суточным слотом: COMPLETE / RECOVERED / MISSING / MISSING_UNRECOVERABLE / NOT_EXPECTED / INCOMPLETE; списания рекламы WB — PROVISIONAL / PROVISIONAL_STUCK.') AS (
WITH
clock AS (
  SELECT DATETIME(as_of, 'Europe/Moscow') AS now_msk, DATE(as_of, 'Europe/Moscow') AS today),
base AS (
  SELECT c.pipeline_id, c.marketplace, c.data_class, c.business_lag_days, c.loss_window_days,
         c.cumulative_presence, c.run_cover_days, c.history_start_date,
         PARSE_TIME('%H:%M', c.slot_time_msk) AS slot_time,
         p.latest_business_date, p.dates_present, p.ok_periods, p.failed_periods, p.ok_run_ts,
         k.now_msk, k.today,
         IF(k.now_msk >= DATETIME(k.today, PARSE_TIME('%H:%M', c.slot_time_msk)),
            DATE_SUB(k.today, INTERVAL c.business_lag_days DAY),
            DATE_SUB(k.today, INTERVAL c.business_lag_days + 1 DAY)) AS target_business_date
  FROM `evetis_health.V_PIPELINE_CONTRACT` c
  JOIN `evetis_health.TVF_PIPELINE_PROBE`(as_of) p USING (pipeline_id)
  CROSS JOIN clock k
  WHERE c.evaluation_mode = 'EVALUATED' AND c.freshness_basis = 'SLOT'),
days AS (
  SELECT b.*, d AS period_date
  FROM base b,
       UNNEST(GENERATE_DATE_ARRAY(
         GREATEST(IFNULL(b.history_start_date, DATE_SUB(b.today, INTERVAL 35 DAY)), DATE_SUB(b.today, INTERVAL 35 DAY)),
         b.target_business_date)) AS d),
period_input AS (
  SELECT pipeline_id, marketplace, data_class, period_date, target_business_date, loss_window_days,
         IF(cumulative_presence, period_date <= latest_business_date, period_date IN UNNEST(dates_present)) AS present,
         period_date IN UNNEST(failed_periods) AS failed_run,
         EXISTS (SELECT 1 FROM UNNEST(ok_periods) op WHERE op >= period_date) AS later_success,
         run_cover_days IS NOT NULL AND EXISTS (
           SELECT 1 FROM UNNEST(ok_run_ts) t
           WHERE DATE(t, 'Europe/Moscow') BETWEEN DATE_ADD(period_date, INTERVAL 1 DAY)
                                              AND DATE_ADD(period_date, INTERVAL run_cover_days DAY)) AS covered_by_run,
         CASE
           WHEN loss_window_days IS NULL THEN NULL
           WHEN loss_window_days = 0 THEN DATETIME_DIFF(now_msk, DATETIME(DATE_ADD(period_date, INTERVAL 1 DAY)), MINUTE)
           ELSE DATETIME_DIFF(now_msk, DATETIME(DATE_ADD(period_date, INTERVAL loss_window_days DAY), slot_time), MINUTE)
         END AS minutes_past_loss_deadline
  FROM days),
-- 🧪 sqlite-тестируемо: только CASE и сравнения.
period_classified AS (
  SELECT period_input.*,
         CASE
           WHEN present AND failed_run AND later_success THEN 'RECOVERED'
           WHEN present AND failed_run THEN 'INCOMPLETE'
           WHEN present THEN 'COMPLETE'
           WHEN covered_by_run THEN 'NOT_EXPECTED'
           WHEN minutes_past_loss_deadline IS NOT NULL AND minutes_past_loss_deadline >= 0 THEN 'MISSING_UNRECOVERABLE'
           ELSE 'MISSING'
         END AS data_status
  FROM period_input)
SELECT pipeline_id, marketplace, data_class, period_date, data_status,
       present, failed_run, later_success, covered_by_run, minutes_past_loss_deadline,
       target_business_date, as_of AS evaluated_at,
       'evetis_health.TVF_PIPELINE_PROBE' AS evidence_ref
FROM period_classified
UNION ALL
-- Списания рекламы WB: стабильность доказывается тремя одинаковыми чтениями в окне
-- D-14..D-1. День вне окна без доказательства — PROVISIONAL_STUCK: значения есть,
-- окончательности не будет. Источник живой (не as_of) — ограничение задокументировано.
SELECT 'ads_costs', 'WB', 'EVENT_HISTORY', v.date,
       CASE
         WHEN v.zero_spend_day THEN 'NOT_EXPECTED'
         WHEN v.not_loaded THEN 'MISSING'
         WHEN v.billed_complete THEN 'COMPLETE'
         WHEN NOT v.stable_ok AND v.age_days > 14 THEN 'PROVISIONAL_STUCK'
         ELSE 'PROVISIONAL'
       END,
       NOT IFNULL(v.not_loaded, FALSE), FALSE, CAST(NULL AS BOOL), CAST(NULL AS BOOL), CAST(NULL AS INT64),
       CAST(NULL AS DATE), as_of, 'wb_raw.V_ADV_COSTS_DAY_COVERAGE (текущее состояние, не as_of)'
FROM `wb_raw.V_ADV_COSTS_DAY_COVERAGE` v, clock k
WHERE v.date >= DATE_SUB(k.today, INTERVAL 35 DAY)
);

-- ── Текущее состояние каждого конвейера ──────────────────────────────────────
CREATE OR REPLACE TABLE FUNCTION `evetis_health.TVF_DATA_HEALTH`(as_of TIMESTAMP)
OPTIONS (description = 'DRO-1. Состояние каждого критичного конвейера на момент as_of: run_status / data_status / serving_status, severity, reason_code, свежесть, сроки потери. Детектор пишет результат в evetis_health.DATA_HEALTH_SNAPSHOT.') AS (
WITH
clock AS (
  SELECT DATETIME(as_of, 'Europe/Moscow') AS now_msk, DATE(as_of, 'Europe/Moscow') AS today),
probe AS (
  SELECT * FROM `evetis_health.TVF_PIPELINE_PROBE`(as_of)),
periods AS (
  SELECT pipeline_id, period_date, data_status FROM `evetis_health.TVF_DATA_PERIOD_STATE`(as_of)),
health_base AS (
  SELECT
    c.*,
    p.latest_business_date,
    p.latest_load_ts AS latest_success_load_ts,
    p.failed_periods,
    p.run_status AS probe_run_status,
    p.run_error_code, p.run_started_at, p.run_finished_at,
    IFNULL(p.completeness_status, 'NOT_MEASURED') AS completeness_status,
    p.completeness_reason, p.completeness_detail,
    IFNULL(p.provisional_stuck_count, 0) AS provisional_stuck_count,
    k.now_msk, k.today,
    PARSE_TIME('%H:%M', c.slot_time_msk) AS slot_time,
    IF(c.freshness_basis = 'SLOT',
       IF(k.now_msk >= DATETIME(k.today, PARSE_TIME('%H:%M', c.slot_time_msk)),
          DATE_SUB(k.today, INTERVAL c.business_lag_days DAY),
          DATE_SUB(k.today, INTERVAL c.business_lag_days + 1 DAY)),
       NULL) AS target_business_date
  FROM `evetis_health.V_PIPELINE_CONTRACT` c
  LEFT JOIN probe p USING (pipeline_id)
  CROSS JOIN clock k),
health_slot AS (
  SELECT b.*,
         CASE
           WHEN b.freshness_basis <> 'SLOT' THEN NULL
           WHEN b.latest_business_date >= b.target_business_date THEN NULL
           WHEN b.latest_business_date IS NULL THEN b.target_business_date
           ELSE DATE_ADD(b.latest_business_date, INTERVAL 1 DAY)
         END AS first_missing_date
  FROM health_base b),
health_times AS (
  SELECT s.*,
         IF(s.first_missing_date IS NULL, NULL,
            DATETIME(DATE_ADD(s.first_missing_date, INTERVAL s.business_lag_days DAY), s.slot_time)) AS slot_due_msk,
         CASE
           WHEN s.first_missing_date IS NULL OR s.loss_window_days IS NULL THEN NULL
           WHEN s.loss_window_days = 0 THEN DATETIME(DATE_ADD(s.first_missing_date, INTERVAL 1 DAY))
           ELSE DATETIME(DATE_ADD(s.first_missing_date, INTERVAL s.loss_window_days DAY), s.slot_time)
         END AS loss_deadline_msk
  FROM health_slot s),
period_summary AS (
  SELECT x.pipeline_id,
         COUNTIF(x.data_status IN ('MISSING', 'INCOMPLETE')
                 AND x.period_date < COALESCE(t.first_missing_date, DATE_ADD(t.target_business_date, INTERVAL 1 DAY))) AS open_recoverable_gaps,
         COUNTIF(x.data_status = 'MISSING_UNRECOVERABLE') AS unrecoverable_gaps_35d,
         MAX(IF(x.data_status = 'MISSING_UNRECOVERABLE', x.period_date, NULL)) AS last_unrecoverable_period,
         COUNTIF(x.data_status = 'RECOVERED') AS recovered_periods_35d,
         MAX(IF(x.data_status = 'RECOVERED', x.period_date, NULL)) AS last_recovered_period
  FROM periods x
  JOIN health_times t USING (pipeline_id)
  GROUP BY x.pipeline_id),
-- Всё, что зависит от функций BigQuery, посчитано здесь; дальше — чистая логика.
health_input AS (
  SELECT t.*,
         IF(t.slot_due_msk IS NULL, IF(t.freshness_basis = 'SLOT', 0, NULL),
            DATETIME_DIFF(t.now_msk, t.slot_due_msk, MINUTE)) AS slot_late_minutes,
         DATETIME_DIFF(t.loss_deadline_msk, t.now_msk, MINUTE) AS minutes_to_loss,
         TIMESTAMP_DIFF(as_of, t.latest_success_load_ts, MINUTE) AS age_minutes,
         CASE
           WHEN t.probe_run_status = 'RUNNING'
                AND TIMESTAMP_DIFF(as_of, t.run_started_at, MINUTE) > t.stale_run_minutes THEN 'ABANDONED'
           ELSE IFNULL(t.probe_run_status, 'UNKNOWN')
         END AS run_status,
         IFNULL(t.target_business_date IN UNNEST(t.failed_periods), FALSE) AS target_failed_run,
         IFNULL(ps.open_recoverable_gaps, 0) AS open_recoverable_gaps,
         IFNULL(ps.unrecoverable_gaps_35d, 0) AS unrecoverable_gaps_35d,
         ps.last_unrecoverable_period,
         IFNULL(ps.recovered_periods_35d, 0) AS recovered_periods_35d,
         ps.last_recovered_period
  FROM health_times t
  LEFT JOIN period_summary ps USING (pipeline_id)),
-- 🧪 sqlite-тестируемо (classified_reason, classified): только CASE и сравнения.
classified_reason AS (
  SELECT h.*,
         CASE
           WHEN h.evaluation_mode <> 'EVALUATED' THEN 'CHECK_NOT_IMPLEMENTED'
           WHEN h.latest_business_date IS NULL AND h.latest_success_load_ts IS NULL THEN 'NO_DATA_OBSERVED'
           WHEN h.freshness_basis = 'SLOT' AND h.slot_late_minutes > 0
                AND h.minutes_to_loss IS NOT NULL AND h.minutes_to_loss <= 0 THEN 'DATA_LOSS_CONFIRMED'
           WHEN h.freshness_basis = 'SLOT' AND h.slot_late_minutes > 0 AND h.slot_late_minutes >= h.warn_after_minutes
                AND h.minutes_to_loss IS NOT NULL AND h.minutes_to_loss <= h.imminent_loss_minutes THEN 'DATA_LOSS_IMMINENT'
           WHEN h.freshness_basis = 'SLOT' AND h.slot_late_minutes > 0
                AND h.slot_late_minutes >= h.fail_after_minutes THEN 'SLOT_MISSED'
           WHEN h.freshness_basis = 'SLOT' AND h.slot_late_minutes > 0
                AND h.slot_late_minutes >= h.warn_after_minutes THEN 'SLOT_LATE'
           WHEN h.freshness_basis = 'AGE' AND h.age_minutes >= h.fail_after_minutes THEN 'FRESHNESS_STALE'
           WHEN h.freshness_basis = 'AGE' AND h.age_minutes >= h.warn_after_minutes THEN 'FRESHNESS_LATE'
           WHEN h.run_status IN ('FAILED', 'ABANDONED') THEN 'RUN_FAILED'
           WHEN h.run_status = 'PARTIAL' THEN 'RUN_PARTIAL'
           WHEN h.completeness_status = 'INCOMPLETE' THEN COALESCE(h.completeness_reason, 'INCOMPLETE')
           WHEN h.open_recoverable_gaps > 0 THEN 'MISSING_DATES_RECOVERABLE'
           ELSE 'OK'
         END AS reason_code
  FROM health_input h),
classified AS (
  SELECT r.*,
         CASE r.reason_code
           WHEN 'OK' THEN 'HEALTHY'
           WHEN 'CHECK_NOT_IMPLEMENTED' THEN 'UNKNOWN'
           WHEN 'NO_DATA_OBSERVED' THEN 'UNKNOWN'
           WHEN 'DATA_LOSS_CONFIRMED' THEN 'BLOCKED'
           WHEN 'DATA_LOSS_IMMINENT' THEN 'BLOCKED'
           WHEN 'SLOT_MISSED' THEN 'STALE'
           WHEN 'FRESHNESS_STALE' THEN 'STALE'
           ELSE 'DEGRADED'
         END AS serving_status,
         CASE
           WHEN r.reason_code IN ('OK', 'CHECK_NOT_IMPLEMENTED') THEN 'INFO'
           WHEN r.reason_code IN ('DATA_LOSS_CONFIRMED', 'DATA_LOSS_IMMINENT') THEN
             CASE r.criticality WHEN 'LOW' THEN 'MEDIUM' WHEN 'MEDIUM' THEN 'HIGH' ELSE 'CRITICAL' END
           WHEN r.reason_code IN ('SLOT_MISSED', 'FRESHNESS_STALE') THEN
             CASE r.criticality WHEN 'CRITICAL' THEN 'CRITICAL' WHEN 'HIGH' THEN 'HIGH' ELSE 'MEDIUM' END
           WHEN r.reason_code IN ('SLOT_LATE', 'FRESHNESS_LATE') THEN
             CASE WHEN r.data_class IN ('SNAPSHOT', 'WINDOWED') AND r.criticality IN ('CRITICAL', 'HIGH')
                  THEN 'HIGH' ELSE 'MEDIUM' END
           WHEN r.reason_code = 'NO_DATA_OBSERVED' THEN 'HIGH'
           WHEN r.reason_code IN ('RUN_FAILED', 'RUN_PARTIAL') THEN
             CASE r.criticality WHEN 'CRITICAL' THEN 'HIGH' ELSE 'MEDIUM' END
           WHEN r.reason_code IN ('CONTROL_MISMATCH', 'COVERAGE_LOW') THEN
             CASE WHEN r.data_class = 'SNAPSHOT' AND r.criticality IN ('CRITICAL', 'HIGH')
                  THEN 'HIGH' ELSE 'MEDIUM' END
           ELSE 'MEDIUM'
         END AS severity,
         CASE
           WHEN r.reason_code IN ('CHECK_NOT_IMPLEMENTED', 'NO_DATA_OBSERVED') THEN 'UNKNOWN'
           WHEN r.freshness_basis = 'SLOT' AND r.slot_late_minutes > 0 THEN
             CASE
               WHEN r.minutes_to_loss IS NOT NULL AND r.minutes_to_loss <= 0 THEN 'MISSING_UNRECOVERABLE'
               WHEN r.slot_late_minutes < r.warn_after_minutes THEN 'PENDING'
               ELSE 'MISSING'
             END
           WHEN r.freshness_basis = 'AGE' AND r.age_minutes >= r.fail_after_minutes THEN 'MISSING'
           WHEN r.run_status = 'PARTIAL' OR r.completeness_status = 'INCOMPLETE' THEN 'INCOMPLETE'
           WHEN r.freshness_basis = 'SLOT' AND r.target_failed_run THEN 'RECOVERED'
           WHEN r.latest_period_provisional THEN 'PROVISIONAL'
           ELSE 'COMPLETE'
         END AS data_status,
         (r.evaluation_mode = 'EVALUATED' AND r.alerting_enabled AND r.reason_code <> 'OK') AS alertable
  FROM classified_reason r)
SELECT
  as_of AS evaluated_at,
  pipeline_id, display_name, marketplace, tenant_id, domain, source_system, data_class, criticality,
  recoverability, freshness_basis, cadence,
  CASE freshness_basis
    WHEN 'SLOT' THEN FORMAT('бизнес-дата D-%t к %s МСК; WARN +%t мин, FAIL +%t мин', business_lag_days, slot_time_msk,
                            warn_after_minutes, fail_after_minutes)
    WHEN 'AGE' THEN FORMAT('успешная загрузка не старше %t мин (WARN) / %t мин (FAIL)', warn_after_minutes, fail_after_minutes)
  END AS expected_slot,
  evaluation_mode, not_evaluated_reason, alerting_enabled, in_registry,
  latest_business_date, latest_success_load_ts, age_minutes,
  target_business_date, first_missing_date, slot_due_msk, slot_late_minutes, loss_deadline_msk, minutes_to_loss,
  warn_after_minutes, fail_after_minutes, imminent_loss_minutes,
  run_status, run_error_code, run_started_at, run_finished_at,
  completeness_status, completeness_reason, completeness_detail,
  data_status, serving_status, severity, reason_code,
  CASE reason_code
    WHEN 'OK' THEN 'Свежо и полно по контракту'
    WHEN 'CHECK_NOT_IMPLEMENTED' THEN CONCAT('Не оценивается в DRO-1: ', IFNULL(not_evaluated_reason, '-'))
    WHEN 'NO_DATA_OBSERVED' THEN 'Проба не нашла ни данных, ни успешных запусков за 45 суток'
    WHEN 'DATA_LOSS_CONFIRMED' THEN FORMAT('Данных за %t нет, срок восстановления %t МСК прошёл: потеря необратима',
                                           first_missing_date, loss_deadline_msk)
    WHEN 'DATA_LOSS_IMMINENT' THEN FORMAT('Данных за %t нет; до необратимой потери %t мин (срок %t МСК)',
                                          first_missing_date, minutes_to_loss, loss_deadline_msk)
    WHEN 'SLOT_MISSED' THEN FORMAT('Данных за %t нет: опоздание %t мин, порог FAIL %t мин',
                                   first_missing_date, slot_late_minutes, fail_after_minutes)
    WHEN 'SLOT_LATE' THEN FORMAT('Данных за %t нет: опоздание %t мин, порог WARN %t мин',
                                 first_missing_date, slot_late_minutes, warn_after_minutes)
    WHEN 'FRESHNESS_STALE' THEN FORMAT('Последняя успешная загрузка %t мин назад, порог FAIL %t мин',
                                       age_minutes, fail_after_minutes)
    WHEN 'FRESHNESS_LATE' THEN FORMAT('Последняя успешная загрузка %t мин назад, порог WARN %t мин',
                                      age_minutes, warn_after_minutes)
    WHEN 'RUN_FAILED' THEN FORMAT('Последний запуск %s (%s); данные пока в пределах порогов',
                                  run_status, IFNULL(run_error_code, '-'))
    WHEN 'RUN_PARTIAL' THEN FORMAT('Последний запуск PARTIAL (%s)', IFNULL(run_error_code, '-'))
    WHEN 'MISSING_DATES_RECOVERABLE' THEN FORMAT('%t дат(ы) без данных в окне 35 суток, ещё восстановимы',
                                                 open_recoverable_gaps)
    ELSE IFNULL(completeness_detail, reason_code)
  END AS reason_text,
  CASE freshness_basis
    WHEN 'SLOT' THEN FORMAT('последняя дата %t, опоздание %t мин', latest_business_date, slot_late_minutes)
    WHEN 'AGE' THEN FORMAT('возраст успешной загрузки %t мин', age_minutes)
    ELSE '-'
  END AS observed_text,
  alertable,
  open_recoverable_gaps, unrecoverable_gaps_35d, last_unrecoverable_period,
  recovered_periods_35d, last_recovered_period, provisional_stuck_count,
  ARRAY(SELECT x FROM UNNEST([
          IF(recovered_periods_35d > 0, 'RECOVERED_PERIODS', NULL),
          IF(unrecoverable_gaps_35d > 0, 'KNOWN_DATA_LOSS', NULL),
          IF(provisional_stuck_count > 0, 'PROVISIONAL_STUCK', NULL),
          IF(known_source_limitation IS NOT NULL, 'SOURCE_LIMITATION', NULL),
          IF(evaluation_mode = 'EVALUATED' AND completeness_status = 'NOT_MEASURED', 'COMPLETENESS_NOT_MEASURED', NULL)
        ]) AS x WHERE x IS NOT NULL) AS caveats,
  known_source_limitation, evidence_ref, contract_version
FROM classified
);
