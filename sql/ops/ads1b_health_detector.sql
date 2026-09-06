-- ============================================================================
-- Stage ADS-1B — детектор здоровья конвейеров WB. Дата: 06.09.2026.
-- Док: docs/ADS1B_HEALTH_DETECTOR_2026-09-06.md
--
-- 🔴 КОНТЕКСТ. Датасет wb_ops существует в production с Stage 3.1A и до этого файла
--    НЕ ИМЕЛ НИ ОДНОЙ СТРОКИ В РЕПОЗИТОРИИ: ни DDL, ни дизайна, ни доменов значений.
--    Это тот же класс дефекта, что Stage 1.6 нашёл в sp_build_mart_sku_daily и
--    Stage ADS-1A — в REF_COST_MAP. Здесь он закрыт частично: объекты, созданные
--    ADS-1B, лежат в §1–§4; таблицы Stage 3.1A зафиксированы в §0 КАК ЕСТЬ.
--    Полная инвентаризация publication-скриптов — BL-8/BL-9, целевой Stage ADS-10.
--
-- Порядок применения: §1 → §2 → §3 → §4 → §5.
-- Fail-open: детектор не вызывается из ingestion/MART и не может их заблокировать.
-- ============================================================================

-- ── §0. AS-IS СНИМОК ТАБЛИЦ Stage 3.1A ──────────────────────────────────────
--    ⚠️ НЕ ВЫПОЛНЯТЬ. Это документация фактического состояния production на
--    06.09.2026, снятая из INFORMATION_SCHEMA. Таблицы уже существуют и содержат
--    данные; повторный CREATE их уничтожит. Раздел нужен для того, чтобы контракт
--    ops-слоя перестал существовать только внутри BigQuery.
--
-- CREATE TABLE `wb_ops.OPS_PIPELINE_REGISTRY` (
--   pipeline_id STRING NOT NULL, pipeline_name STRING NOT NULL, parent_pipeline_id STRING,
--   source_system STRING NOT NULL, target_object STRING NOT NULL, environment STRING NOT NULL,
--   enabled BOOL NOT NULL, criticality STRING NOT NULL, cadence_type STRING NOT NULL,
--   cadence_spec STRING, cadence_definition_source STRING NOT NULL,
--   runtime_verification_status STRING NOT NULL, cadence_observed_evidence STRING,
--   cadence_last_observed_at TIMESTAMP, is_failure_isolated BOOL NOT NULL,
--   is_snapshot_only BOOL NOT NULL, is_backfillable BOOL NOT NULL,
--   missed_run_data_loss BOOL NOT NULL, freshness_sla_minutes INT64,
--   stale_run_threshold_minutes INT64, expected_business_lag_days INT64,
--   grace_period_minutes INT64, reminder_interval_hours INT64, run_log_source STRING,
--   coverage_check_enabled BOOL NOT NULL, owner STRING, notes STRING, updated_at TIMESTAMP NOT NULL)
-- CLUSTER BY environment, criticality
-- OPTIONS(description="Stage 3.1A. Ожидаемое поведение pipeline: каденс, SLA, иерархия
--   parent/child, provenance. environment=SELFTEST исключается из production health.");
--
-- CREATE TABLE `wb_ops.OPS_HEALTH_STATE` (
--   check_ts TIMESTAMP NOT NULL, scope STRING NOT NULL, scope_id STRING NOT NULL,
--   health_status STRING, verification_status STRING, commissioning_status STRING,
--   previous_health STRING, is_transition BOOL NOT NULL, reason_code STRING,
--   reason_text STRING, evidence_json JSON)
-- PARTITION BY DATE(check_ts) CLUSTER BY scope, scope_id
-- OPTIONS(partition_expiration_days=400.0, description="Stage 3.1A. Снимки состояния,
--   append-only. Три ортогональных измерения: health / verification / commissioning.
--   Единая шкала НЕ используется.");
--
-- CREATE TABLE `wb_ops.OPS_INCIDENT` (
--   incident_id STRING NOT NULL, condition_fingerprint STRING NOT NULL,
--   occurrence_seq INT64 NOT NULL, scope STRING NOT NULL, scope_id STRING NOT NULL,
--   reason_code STRING NOT NULL, subject_key STRING NOT NULL, severity STRING NOT NULL,
--   state STRING NOT NULL, first_seen_at TIMESTAMP NOT NULL, last_seen_at TIMESTAMP NOT NULL,
--   recovery_deadline_ts TIMESTAMP, resolved_at TIMESTAMP, closed_at TIMESTAMP,
--   closure_reason STRING, observation_count INT64 NOT NULL, is_data_loss BOOL NOT NULL,
--   is_recoverable BOOL NOT NULL, evidence_json JSON, notes STRING)
-- CLUSTER BY state, scope, scope_id
-- OPTIONS(description="Stage 3.1A. Жизненный цикл инцидентов. RESOLVED = условие устранено;
--   CLOSED_UNRECOVERABLE = ожидание окончено, потеря необратима (НЕ recovery). Терминальные
--   состояния не переоткрываются: повтор условия -> occurrence_seq+1. Счётчиков оповещений
--   здесь НЕТ — источник истины OPS_ALERT_EVENT.");
--
-- CREATE TABLE `wb_ops.OPS_ALERT_EVENT` (
--   alert_event_id STRING NOT NULL, incident_id STRING NOT NULL, alert_type STRING NOT NULL,
--   reminder_bucket INT64, severity STRING NOT NULL, scope STRING NOT NULL,
--   scope_id STRING NOT NULL, reason_code STRING NOT NULL, state STRING NOT NULL,
--   created_at TIMESTAMP NOT NULL, sent_at TIMESTAMP, attempt_count INT64 NOT NULL,
--   next_retry_at TIMESTAMP, channel STRING NOT NULL, recipient_mask STRING NOT NULL,
--   subject STRING, body_preview STRING, delivery_error STRING)
-- PARTITION BY DATE(created_at) CLUSTER BY state, incident_id
-- OPTIONS(partition_expiration_days=400.0, description="Stage 3.1A. DELIVERY SEMANTICS =
--   AT_LEAST_ONCE. alert_event_id детерминирован. Окно дубля: отказ между MailApp и
--   UPDATE state. Полный адрес получателя тут НЕ хранится — только маска.");
--
-- Плюс OPS_METRIC_COVERAGE и OPS_METRIC_COVERAGE_GAPS — ADS-1B их не использует.

-- ── §1. Применение переходов состояния (чистое, без вычисления проверок) ─────
CREATE OR REPLACE PROCEDURE `wb_ops.sp_ops_apply_health`(
  in_results ARRAY<STRUCT<
    scope STRING, scope_id STRING, health_status STRING, reason_code STRING,
    reason_text STRING, severity STRING, subject_key STRING,
    observed STRING, expected STRING, is_data_loss BOOL, is_recoverable BOOL>>,
  in_check_ts TIMESTAMP)
BEGIN
  -- Stage ADS-1B. Чистое применение переходов состояния. Реальные проверки НЕ вычисляет —
  -- получает готовый результат. Это позволяет прогонять детерминированный lifecycle-тест
  -- на изолированном scope, не трогая production.
  --
  -- Домены значений заводятся здесь (в схеме Stage 3.1A их не было):
  --   health_status  : HEALTHY | UNHEALTHY
  --   incident.state : OPEN | RESOLVED   (CLOSED_UNRECOVERABLE оставлен будущему этапу —
  --                    требует политики recovery_deadline_ts, её в ADS-1B нет)
  --   alert_type     : OPEN | RECOVERY
  --   alert.state    : RECORDED — событие ЗАЖУРНАЛИРОВАНО, доставка в ADS-1B НЕ реализована.
  --                    channel='NONE', recipient_mask='n/a'. Доставщик должен забирать RECORDED.
  --   condition_fingerprint = SHA256(scope|scope_id|reason_code|subject_key)
  --   incident_id    = <fingerprint[0:16]>#<occurrence_seq>
  --   alert_event_id = <incident_id>|<alert_type> — детерминирован, поэтому повторный прогон
  --                    в том же состоянии физически не может создать дубль.
  -- ⚠️ CREATE OR REPLACE TEMP TABLE обязателен: TEMP-таблицы живут до конца СКРИПТА,
  --    а не вызова, и несколько CALL подряд иначе падают на "Already Exists".

  CREATE OR REPLACE TEMP TABLE _res AS
  SELECT r.*,
         TO_HEX(SHA256(CONCAT(r.scope,'|',r.scope_id,'|',r.reason_code,'|',r.subject_key))) AS fp
  FROM UNNEST(in_results) r;

  CREATE OR REPLACE TEMP TABLE _prev AS
  SELECT scope, scope_id, health_status AS prev_health FROM (
    SELECT scope, scope_id, health_status,
           ROW_NUMBER() OVER (PARTITION BY scope, scope_id ORDER BY check_ts DESC) rn
    FROM `wb_ops.OPS_HEALTH_STATE`
    WHERE scope IN (SELECT DISTINCT scope FROM _res))
  WHERE rn = 1;

  CREATE OR REPLACE TEMP TABLE _eval AS
  SELECT r.*, p.prev_health,
         (p.prev_health IS NULL OR p.prev_health <> r.health_status) AS is_transition
  FROM _res r LEFT JOIN _prev p ON p.scope = r.scope AND p.scope_id = r.scope_id;

  INSERT INTO `wb_ops.OPS_HEALTH_STATE`
    (check_ts, scope, scope_id, health_status, verification_status, commissioning_status,
     previous_health, is_transition, reason_code, reason_text, evidence_json)
  SELECT in_check_ts, scope, scope_id, health_status, 'OBSERVED', 'ACTIVE',
         prev_health, is_transition, reason_code, reason_text,
         TO_JSON(STRUCT(observed AS observed, expected AS expected, severity AS severity,
                        subject_key AS subject_key, fp AS condition_fingerprint))
  FROM _eval;

  MERGE `wb_ops.OPS_INCIDENT` t
  USING (
    SELECT e.*,
           IFNULL((SELECT MAX(occurrence_seq) FROM `wb_ops.OPS_INCIDENT` i
                   WHERE i.condition_fingerprint = e.fp), 0) AS max_seq
    FROM _eval e WHERE e.health_status = 'UNHEALTHY') s
  ON t.condition_fingerprint = s.fp AND t.state = 'OPEN'
  WHEN MATCHED THEN UPDATE SET
    last_seen_at = in_check_ts,
    observation_count = t.observation_count + 1,
    evidence_json = TO_JSON(STRUCT(s.observed AS observed, s.expected AS expected,
                                   s.reason_text AS reason_text))
  WHEN NOT MATCHED THEN INSERT
    (incident_id, condition_fingerprint, occurrence_seq, scope, scope_id, reason_code,
     subject_key, severity, state, first_seen_at, last_seen_at, observation_count,
     is_data_loss, is_recoverable, evidence_json)
  VALUES
    (CONCAT(SUBSTR(s.fp,1,16),'#',CAST(s.max_seq + 1 AS STRING)), s.fp, s.max_seq + 1,
     s.scope, s.scope_id, s.reason_code, s.subject_key, s.severity, 'OPEN',
     in_check_ts, in_check_ts, 1, s.is_data_loss, s.is_recoverable,
     TO_JSON(STRUCT(s.observed AS observed, s.expected AS expected, s.reason_text AS reason_text)));

  UPDATE `wb_ops.OPS_INCIDENT` t
  SET state = 'RESOLVED', resolved_at = in_check_ts, closed_at = in_check_ts,
      closure_reason = 'CONDITION_CLEARED', last_seen_at = in_check_ts
  WHERE t.state = 'OPEN'
    AND EXISTS (SELECT 1 FROM _eval e
                WHERE e.scope = t.scope AND e.scope_id = t.scope_id
                  AND e.health_status = 'HEALTHY');

  INSERT INTO `wb_ops.OPS_ALERT_EVENT`
    (alert_event_id, incident_id, alert_type, severity, scope, scope_id, reason_code,
     state, created_at, attempt_count, channel, recipient_mask, subject, body_preview)
  SELECT
    CONCAT(i.incident_id,'|',a.alert_type), i.incident_id, a.alert_type, i.severity,
    i.scope, i.scope_id, i.reason_code, 'RECORDED', in_check_ts, 0, 'NONE', 'n/a',
    CONCAT('[', i.severity, '] ', a.alert_type, ' — ', i.scope_id),
    CONCAT('observed=', IFNULL(JSON_VALUE(i.evidence_json,'$.observed'),'?'),
           ' | expected=', IFNULL(JSON_VALUE(i.evidence_json,'$.expected'),'?'),
           ' | ', IFNULL(JSON_VALUE(i.evidence_json,'$.reason_text'),''))
  FROM `wb_ops.OPS_INCIDENT` i
  JOIN (SELECT 'OPEN' AS alert_type UNION ALL SELECT 'RECOVERY') a
    ON (a.alert_type = 'OPEN'     AND i.first_seen_at = in_check_ts)
    OR (a.alert_type = 'RECOVERY' AND i.state = 'RESOLVED' AND i.resolved_at = in_check_ts)
  WHERE NOT EXISTS (SELECT 1 FROM `wb_ops.OPS_ALERT_EVENT` x
                    WHERE x.alert_event_id = CONCAT(i.incident_id,'|',a.alert_type));
END;

-- ── §2. Вычисление H0–H6 по живым данным ────────────────────────────────────
CREATE OR REPLACE PROCEDURE `wb_ops.sp_evaluate_pipeline_health`()
BEGIN
  DECLARE v_ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP();
  DECLARE v_today DATE DEFAULT CURRENT_DATE('Europe/Moscow');
  DECLARE v_results ARRAY<STRUCT<
    scope STRING, scope_id STRING, health_status STRING, reason_code STRING,
    reason_text STRING, severity STRING, subject_key STRING,
    observed STRING, expected STRING, is_data_loss BOOL, is_recoverable BOOL>>;

  SET v_results = (
    WITH
    reg AS (SELECT pipeline_id, criticality, freshness_sla_minutes, expected_business_lag_days,
                   missed_run_data_loss, is_backfillable
            FROM `wb_ops.OPS_PIPELINE_REGISTRY` WHERE environment='prod' AND enabled),
    -- 🔴 H1: уточнение против буквальной формулировки ТЗ (ADS-1B, 06.09.2026).
    --   «≥2 ERROR на один target_date без последующего COMPLETE» по ВСЕЙ истории даёт
    --   вечное срабатывание: target_date 02–04.09 никогда не получат COMPLETE, потому что
    --   sp_build_mart_sku_daily делает ПОЛНУЮ пересборку и конвейер уходит на новый
    --   target_date. Данные за те сутки при этом восстановлены. Проверено на bootstrap:
    --   буквальное правило дало UNHEALTHY на уже здоровом проде.
    --   Операционно проверка про то, СЛОМАН ЛИ КОНВЕЙЕР СЕЙЧАС, поэтому берём последний
    --   по времени target_date и считаем ERROR в сегменте ПОСЛЕ последнего COMPLETE.
    --   Раннее обнаружение не теряется: replay даёт то же срабатывание 03.09 06:04 UTC.
    --   Устаревание данных за прошлые сутки ловит H2 (lag), а не H1.
    cur AS (SELECT target_date FROM `wb_mart.MART_RUNS` ORDER BY started_at DESC LIMIT 1),
    h1_src AS (SELECT m.status, m.started_at, m.completed_at,
                 COUNTIF(m.status='COMPLETE') OVER (ORDER BY m.started_at
                   ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) grp
               FROM `wb_mart.MART_RUNS` m WHERE m.target_date=(SELECT target_date FROM cur)),
    h1 AS (SELECT (SELECT target_date FROM cur) td,
                  (SELECT COUNTIF(status='ERROR') FROM h1_src WHERE grp=(SELECT MAX(grp) FROM h1_src)) e_cnt,
                  (SELECT MIN(started_at) FROM h1_src WHERE grp=(SELECT MAX(grp) FROM h1_src) AND status='ERROR') f_err),
    h2 AS (SELECT (SELECT MAX(day) FROM `wb_mart.MART_SKU_DAILY`) mart_max,
                  DATE_SUB(v_today, INTERVAL 1 DAY) expected_closed,
                  DATE_DIFF(DATE_SUB(v_today, INTERVAL 1 DAY),
                            (SELECT MAX(day) FROM `wb_mart.MART_SKU_DAILY`), DAY) lag_days,
                  (SELECT expected_business_lag_days FROM reg WHERE pipeline_id='mart') tol),
    h3 AS (SELECT (SELECT MAX(completed_at) FROM `wb_raw.V_INGEST_HEARTBEAT`
                   WHERE loader_name='ads' AND status='COMPLETE') last_ok,
                  (SELECT freshness_sla_minutes FROM reg WHERE pipeline_id='ads_daily') sla),
    h4 AS (SELECT (SELECT MAX(SAFE_CAST(snapshot_ts AS TIMESTAMP))
                   FROM `wb_raw.RAW_WB_ADV_QUERY_BIDS_RUNS`
                   WHERE http_success='TRUE' AND status IN ('OK','PARTIAL')) last_ts,
                  (SELECT MAX(SAFE.PARSE_DATE('%Y-%m-%d',snapshot_date))
                   FROM `wb_raw.RAW_WB_ADV_QUERY_BIDS_RUNS`
                   WHERE http_success='TRUE' AND status IN ('OK','PARTIAL')) last_snap,
                  (SELECT freshness_sla_minutes FROM reg WHERE pipeline_id='ads_query_bids') sla),
    h5 AS (SELECT * EXCEPT(rn) FROM (
             SELECT SAFE.PARSE_DATE('%Y-%m-%d',period_from) d, coverage_ratio r,
                    ROW_NUMBER() OVER (ORDER BY period_from DESC) rn
             FROM `wb_raw.V_ADV_QUERY_STATS_COVERAGE` WHERE coverage_ratio IS NOT NULL) WHERE rn=1),
    h6 AS (SELECT COUNT(*) pairs, SUM(rws) rws, MIN(mn) first_seen,
                  STRING_AGG(FORMAT('%s×%s(%d стр, %.2f ₽, с %s)',op_key,amount_field,rws,amt,CAST(mn AS STRING)),'; ') det
           FROM (SELECT op_key, amount_field, COUNT(*) rws, SUM(source_signed_amount) amt, MIN(finance_date) mn
                 FROM `wb_mart.V_WB_FINANCE_AMOUNTS_LONG_MAPPED`
                 WHERE cost_category IS NULL GROUP BY 1,2))
    SELECT [
      STRUCT('PIPELINE_CHECK','mart/H1_MART_REPEATED_FAILURE',
        IF((SELECT e_cnt FROM h1)>=2,'UNHEALTHY','HEALTHY'),
        IF((SELECT e_cnt FROM h1)>=2,'MART_REPEATED_ERROR','OK'),
        IF((SELECT e_cnt FROM h1)>=2,
           FORMAT('Витрина падает подряд: %d ERROR без COMPLETE на текущем target_date %s, первый отказ %s',
                  (SELECT e_cnt FROM h1), CAST((SELECT td FROM h1) AS STRING),
                  FORMAT_TIMESTAMP('%Y-%m-%d %H:%M UTC',(SELECT f_err FROM h1))),
           FORMAT('Текущий target_date %s: подряд идущих ERROR после последнего COMPLETE — %d',
                  CAST((SELECT td FROM h1) AS STRING), (SELECT e_cnt FROM h1))),
        (SELECT criticality FROM reg WHERE pipeline_id='mart'),
        CAST((SELECT td FROM h1) AS STRING),
        FORMAT('%d ERROR подряд', (SELECT e_cnt FROM h1)), '<= 1 ERROR подряд', FALSE, TRUE),
      STRUCT('PIPELINE_CHECK','mart/H2_MART_LAG',
        IF((SELECT lag_days FROM h2) > (SELECT tol FROM h2),'UNHEALTHY','HEALTHY'),
        IF((SELECT lag_days FROM h2) > (SELECT tol FROM h2),'MART_LAG_EXCEEDED','OK'),
        FORMAT('MART max(day)=%s, последняя закрытая дата=%s, отставание %d сут при допуске %d',
               CAST((SELECT mart_max FROM h2) AS STRING), CAST((SELECT expected_closed FROM h2) AS STRING),
               (SELECT lag_days FROM h2),(SELECT tol FROM h2)),
        (SELECT criticality FROM reg WHERE pipeline_id='mart'),
        CAST((SELECT expected_closed FROM h2) AS STRING),
        FORMAT('lag=%d сут', (SELECT lag_days FROM h2)),
        FORMAT('lag<=%d сут', (SELECT tol FROM h2)), FALSE, TRUE),
      STRUCT('PIPELINE_CHECK','ads_daily/H3_ADS_INGESTION_STALE',
        IF(TIMESTAMP_DIFF(v_ts,(SELECT last_ok FROM h3),MINUTE) > (SELECT sla FROM h3),'UNHEALTHY','HEALTHY'),
        IF(TIMESTAMP_DIFF(v_ts,(SELECT last_ok FROM h3),MINUTE) > (SELECT sla FROM h3),'ADS_INGESTION_STALE','OK'),
        FORMAT('Последний COMPLETE рекламной загрузки %.1f ч назад',
               TIMESTAMP_DIFF(v_ts,(SELECT last_ok FROM h3),MINUTE)/60.0),
        (SELECT criticality FROM reg WHERE pipeline_id='ads_daily'),
        CAST(DATE((SELECT last_ok FROM h3)) AS STRING),
        FORMAT('%.1f ч', TIMESTAMP_DIFF(v_ts,(SELECT last_ok FROM h3),MINUTE)/60.0),
        FORMAT('<= %.0f ч', (SELECT sla FROM h3)/60.0), FALSE, TRUE),
      STRUCT('PIPELINE_CHECK','ads_query_bids/H4_BID_SNAPSHOT_MISSING',
        IF(TIMESTAMP_DIFF(v_ts,(SELECT last_ts FROM h4),MINUTE) > (SELECT sla FROM h4),'UNHEALTHY','HEALTHY'),
        IF(TIMESTAMP_DIFF(v_ts,(SELECT last_ts FROM h4),MINUTE) > (SELECT sla FROM h4),'BID_SNAPSHOT_MISSING','OK'),
        FORMAT('Последний снимок ставок %s (%.1f ч назад). История ставок НЕВОССТАНОВИМА.',
               CAST((SELECT last_snap FROM h4) AS STRING),
               TIMESTAMP_DIFF(v_ts,(SELECT last_ts FROM h4),MINUTE)/60.0),
        (SELECT criticality FROM reg WHERE pipeline_id='ads_query_bids'),
        CAST((SELECT last_snap FROM h4) AS STRING),
        FORMAT('%.1f ч', TIMESTAMP_DIFF(v_ts,(SELECT last_ts FROM h4),MINUTE)/60.0),
        FORMAT('<= %.0f ч', (SELECT sla FROM h4)/60.0),
        (SELECT missed_run_data_loss FROM reg WHERE pipeline_id='ads_query_bids'),
        (SELECT is_backfillable FROM reg WHERE pipeline_id='ads_query_bids')),
      STRUCT('PIPELINE_CHECK','ads_query_stats/H5_QUERY_COVERAGE_DEGRADED',
        IF((SELECT r FROM h5) < 0.90,'UNHEALTHY','HEALTHY'),
        IF((SELECT r FROM h5) < 0.90,'QUERY_COVERAGE_DEGRADED','OK'),
        FORMAT('coverage_ratio=%.4f на %s. Это покрытие scope query-источника, НЕ доля рекламного бюджета.',
               (SELECT r FROM h5), CAST((SELECT d FROM h5) AS STRING)),
        (SELECT criticality FROM reg WHERE pipeline_id='ads_query_stats'),
        CAST((SELECT d FROM h5) AS STRING),
        FORMAT('%.4f', (SELECT r FROM h5)), '>= 0.9000', FALSE, TRUE),
      STRUCT('PIPELINE_CHECK','finance/H6_FINANCE_UNKNOWN_PAIRS',
        IF((SELECT pairs FROM h6) > 0,'UNHEALTHY','HEALTHY'),
        IF((SELECT pairs FROM h6) > 0,'FINANCE_UNKNOWN_MONEY_PAIR','OK'),
        IF((SELECT pairs FROM h6) > 0,
           CONCAT('Денежные пары вне REF_COST_MAP: ', (SELECT det FROM h6),
                  '. Остановят сборку витрины и выпадут из downstream-экономики.'),
           'Все денежные пары классифицированы'),
        'HIGH',
        IFNULL((SELECT CAST(first_seen AS STRING) FROM h6),'-'),
        FORMAT('%d пар / %d строк', (SELECT pairs FROM h6), IFNULL((SELECT rws FROM h6),0)),
        '0 пар', FALSE, TRUE),
      STRUCT('PIPELINE_CHECK','health_detector/H0_DETECTOR_HEARTBEAT','HEALTHY','OK',
        'Отметка последнего прогона детектора. Сам себя не оценивает: остановку детектора видно по устареванию last_checked_at в V_OPS_CURRENT_HEALTH.',
        'MEDIUM','-', FORMAT_TIMESTAMP('%Y-%m-%d %H:%M UTC', v_ts), 'прогон не реже 1 раза в 3 ч', FALSE, TRUE)
    ]);

  CALL `wb_ops.sp_ops_apply_health`(v_results, v_ts);
END;

-- ── §3. Dashboard-ready: текущее состояние ──────────────────────────────────
CREATE OR REPLACE VIEW `wb_ops.V_OPS_CURRENT_HEALTH`
OPTIONS (description = 'Stage ADS-1B. Текущее состояние здоровья: последний снимок OPS_HEALTH_STATE на каждую пару (scope, scope_id) + активный инцидент. Dashboard-ready, только чтение.') AS
WITH latest AS (
  SELECT * EXCEPT(_rn) FROM (
    SELECT h.*, ROW_NUMBER() OVER (PARTITION BY scope, scope_id ORDER BY check_ts DESC) AS _rn
    FROM `wb_ops.OPS_HEALTH_STATE` h)
  WHERE _rn = 1),
inc AS (
  SELECT * EXCEPT(_rn) FROM (
    SELECT i.*, ROW_NUMBER() OVER (PARTITION BY scope, scope_id ORDER BY first_seen_at DESC) AS _rn
    FROM `wb_ops.OPS_INCIDENT` i
    WHERE state = 'OPEN')
  WHERE _rn = 1)
SELECT
  SPLIT(l.scope_id, '/')[SAFE_OFFSET(0)]                       AS pipeline_id,
  SPLIT(l.scope_id, '/')[SAFE_OFFSET(1)]                       AS check_id,
  l.scope, l.scope_id, l.health_status,
  IFNULL(i.severity, JSON_VALUE(l.evidence_json, '$.severity')) AS severity,
  l.reason_code,
  l.reason_text                                                AS reason,
  JSON_VALUE(l.evidence_json, '$.observed')                    AS observed_value,
  JSON_VALUE(l.evidence_json, '$.expected')                    AS expected_value,
  JSON_VALUE(l.evidence_json, '$.subject_key')                 AS subject_key,
  i.incident_id, i.state AS incident_state, i.occurrence_seq, i.observation_count,
  i.first_seen_at, i.last_seen_at,
  l.check_ts                                                   AS last_checked_at,
  l.previous_health, l.is_transition, l.evidence_json
FROM latest l
LEFT JOIN inc i ON i.scope = l.scope AND i.scope_id = l.scope_id;

-- ── §4. Dashboard-ready: открытые инциденты ─────────────────────────────────
CREATE OR REPLACE VIEW `wb_ops.V_OPS_ACTIVE_INCIDENTS`
OPTIONS (description = 'Stage ADS-1B. Открытые инциденты с длительностью и числом наблюдений. Dashboard-ready, только чтение.') AS
SELECT
  i.incident_id,
  SPLIT(i.scope_id,'/')[SAFE_OFFSET(0)]                                  AS pipeline_id,
  SPLIT(i.scope_id,'/')[SAFE_OFFSET(1)]                                  AS check_id,
  i.severity, i.reason_code, i.subject_key,
  JSON_VALUE(i.evidence_json,'$.observed')                               AS observed_value,
  JSON_VALUE(i.evidence_json,'$.expected')                               AS expected_value,
  JSON_VALUE(i.evidence_json,'$.reason_text')                            AS reason,
  i.first_seen_at, i.last_seen_at,
  TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), i.first_seen_at, MINUTE)/60.0      AS open_hours,
  i.observation_count, i.occurrence_seq, i.is_data_loss, i.is_recoverable,
  (SELECT COUNT(*) FROM `wb_ops.OPS_ALERT_EVENT` a WHERE a.incident_id = i.incident_id) AS alert_events
FROM `wb_ops.OPS_INCIDENT` i
WHERE i.state = 'OPEN';

-- ── §5. Bootstrap / штатный прогон ──────────────────────────────────────────
-- CALL `wb_ops.sp_evaluate_pipeline_health`();
