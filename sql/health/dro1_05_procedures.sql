-- ============================================================================
-- DRO-1 · §5 Детектор и доставка.
-- Док: docs/ops/DRO1_DETECTION_ALERTING_2026-10-04.md
--
-- Детектор переиспользует контур ADS-1B, а не строит второй источник истины:
-- инциденты, дедупликация и события — wb_ops.sp_ops_apply_health (без изменений).
-- Scope = 'DRO_PIPELINE', scope_id = subject_key = pipeline_id: отпечаток инцидента
-- не содержит даты, поэтому длящийся сбой не открывает новый инцидент каждые сутки
-- (дефект H1/H2 ADS-1B). Смена причины (SLOT_LATE → DATA_LOSS_IMMINENT) — новый
-- инцидент: это и есть эскалация.
--
-- Доставка — без нового runtime. BigQuery не умеет слать письма, но каждое задание
-- попадает в журнал аудита Cloud Logging вместе с метками. Диспетчер ставит метки
-- (SET @@query_label) на одно задание-маркер, политика Cloud Monitoring ловит его и
-- шлёт email в существующий канал. Содержимое письма — значения меток.
-- Проверено 2026-10-04: метки заданий видны в audit log по пути
-- protoPayload.metadata.jobChange.job.jobConfig.labels.*, логи data_access идут в
-- _Default без исключений. Что @@query_label метит дочерние задания процедуры —
-- документировано BigQuery, на этом проекте подтверждается первым прогоном (раздел
-- «Проверка после деплоя» в доке).
--
-- Тихие часы 23:00–07:00 МСК: сразу уходят только DATA_LOSS_IMMINENT /
-- DATA_LOSS_CONFIRMED уровня CRITICAL. Остальное ждёт 07:00 (HIGH/CRITICAL) или
-- дайджеста 09:30 (MEDIUM и всё, что не ушло отдельно). Сторож (детектор не
-- работает) и сбой детектора — политики Cloud Monitoring, им тихие часы не нужны.
-- ============================================================================

-- Значение метки BigQuery: только [a-z0-9_-], не длиннее 63 символов.
CREATE OR REPLACE FUNCTION `evetis_health.f_label_value`(s STRING) AS (
  SUBSTR(REGEXP_REPLACE(LOWER(IFNULL(NULLIF(s, ''), 'none')), r'[^a-z0-9_-]', '_'), 1, 63)
);

CREATE OR REPLACE PROCEDURE `evetis_health.sp_dispatch_alerts`(in_now TIMESTAMP)
BEGIN
  DECLARE v_msk DATETIME DEFAULT DATETIME(in_now, 'Europe/Moscow');
  DECLARE v_quiet BOOL DEFAULT (EXTRACT(HOUR FROM DATETIME(in_now, 'Europe/Moscow')) >= 23
                                OR EXTRACT(HOUR FROM DATETIME(in_now, 'Europe/Moscow')) < 7);
  DECLARE v_batch STRING DEFAULT CONCAT('drob_', FORMAT_DATETIME('%Y%m%d_%H%M', DATETIME(in_now, 'Europe/Moscow')));
  DECLARE v_digest STRING DEFAULT CONCAT('drod_', FORMAT_DATE('%Y%m%d', DATE(in_now, 'Europe/Moscow')));
  DECLARE v_n INT64 DEFAULT 0;
  DECLARE v_label STRING;

  SET @@query_label = 'dro_component:dispatcher';

  -- §A Напоминания по открытым инцидентам: CRITICAL раз в 4 ч, HIGH раз в 12 ч.
  --    reminder_bucket — существующая колонка схемы Stage 3.1A; id детерминирован.
  INSERT INTO `wb_ops.OPS_ALERT_EVENT`
    (alert_event_id, incident_id, alert_type, reminder_bucket, severity, scope, scope_id, reason_code,
     state, created_at, attempt_count, channel, recipient_mask, subject, body_preview)
  SELECT CONCAT(i.incident_id, '|REMINDER|', CAST(b.bucket AS STRING)), i.incident_id, 'REMINDER', b.bucket,
         i.severity, i.scope, i.scope_id, i.reason_code, 'RECORDED', in_now, 0, 'NONE', 'n/a',
         CONCAT('[', i.severity, '] REMINDER — ', i.scope_id),
         CONCAT('observed=', IFNULL(JSON_VALUE(i.evidence_json, '$.observed'), '?'),
                ' | ', IFNULL(JSON_VALUE(i.evidence_json, '$.reason_text'), ''))
  FROM `wb_ops.OPS_INCIDENT` i
  CROSS JOIN UNNEST([STRUCT(DIV(TIMESTAMP_DIFF(in_now, i.first_seen_at, MINUTE),
                               CASE i.severity WHEN 'CRITICAL' THEN 240 ELSE 720 END) AS bucket)]) b
  WHERE i.scope = 'DRO_PIPELINE' AND i.state = 'OPEN' AND i.severity IN ('CRITICAL', 'HIGH') AND b.bucket >= 1
    AND NOT EXISTS (SELECT 1 FROM `wb_ops.OPS_ALERT_EVENT` x
                    WHERE x.alert_event_id = CONCAT(i.incident_id, '|REMINDER|', CAST(b.bucket AS STRING)));

  -- §B Что уходит сейчас отдельным письмом.
  CREATE OR REPLACE TEMP TABLE _dro_due AS
  WITH ev AS (
    SELECT alert_event_id, incident_id, alert_type, severity, scope_id AS pipeline_id, reason_code, created_at
    FROM `wb_ops.OPS_ALERT_EVENT`
    WHERE scope = 'DRO_PIPELINE' AND state = 'RECORDED'),
  opens AS (
    SELECT * FROM ev
    WHERE alert_type IN ('OPEN', 'REMINDER')
      AND ((severity = 'CRITICAL' AND (NOT v_quiet OR reason_code IN ('DATA_LOSS_IMMINENT', 'DATA_LOSS_CONFIRMED')))
           OR (severity = 'HIGH' AND NOT v_quiet))),
  recoveries AS (
    SELECT r.* FROM ev r
    WHERE r.alert_type = 'RECOVERY' AND NOT v_quiet
      AND (EXISTS (SELECT 1 FROM `wb_ops.OPS_ALERT_EVENT` o
                   WHERE o.incident_id = r.incident_id AND o.alert_type = 'OPEN' AND o.state = 'HANDED_OFF')
           OR EXISTS (SELECT 1 FROM opens o WHERE o.incident_id = r.incident_id AND o.alert_type = 'OPEN')))
  SELECT * FROM opens
  UNION ALL
  SELECT * FROM recoveries;

  SET v_n = (SELECT COUNT(*) FROM _dro_due);
  IF v_n > 0 THEN
    SET v_label = (
      SELECT CONCAT(
        'dro_kind:alert,dro_component:dispatcher,dro_batch:', v_batch,
        ',dro_n:', CAST(COUNT(*) AS STRING),
        ',dro_open:', CAST(COUNTIF(alert_type IN ('OPEN', 'REMINDER')) AS STRING),
        ',dro_recovery:', CAST(COUNTIF(alert_type = 'RECOVERY') AS STRING),
        ',dro_top_type:', `evetis_health.f_label_value`(ARRAY_AGG(alert_type ORDER BY rnk, created_at LIMIT 1)[OFFSET(0)]),
        ',dro_top_severity:', `evetis_health.f_label_value`(ARRAY_AGG(severity ORDER BY rnk, created_at LIMIT 1)[OFFSET(0)]),
        ',dro_top_pipeline:', `evetis_health.f_label_value`(ARRAY_AGG(pipeline_id ORDER BY rnk, created_at LIMIT 1)[OFFSET(0)]),
        ',dro_top_reason:', `evetis_health.f_label_value`(ARRAY_AGG(reason_code ORDER BY rnk, created_at LIMIT 1)[OFFSET(0)]),
        ',dro_pipelines:', `evetis_health.f_label_value`(STRING_AGG(DISTINCT pipeline_id, '-' ORDER BY pipeline_id)))
      FROM (SELECT *,
                   (CASE severity WHEN 'CRITICAL' THEN 0 WHEN 'HIGH' THEN 1 ELSE 2 END) * 10
                   + IF(alert_type = 'RECOVERY', 1, 0) AS rnk
            FROM _dro_due));
    SET @@query_label = v_label;
    SELECT 'dro_delivery_marker' AS marker, v_batch AS batch_id, v_n AS events;
    SET @@query_label = 'dro_component:dispatcher';

    -- AT_LEAST_ONCE (контракт таблицы): маркер уже ушёл; если UPDATE упадёт,
    -- следующий прогон отправит те же события ещё раз — дубль, а не потеря.
    UPDATE `wb_ops.OPS_ALERT_EVENT`
    SET state = 'HANDED_OFF', sent_at = in_now, attempt_count = attempt_count + 1,
        channel = 'EMAIL_CLOUD_MONITORING', recipient_mask = 'cloud-monitoring:ops-email'
    WHERE state = 'RECORDED' AND alert_event_id IN (SELECT alert_event_id FROM _dro_due);

    INSERT INTO `evetis_health.ALERT_DISPATCH_LOG`
      (dispatch_id, kind, dispatched_at, quiet_hours, n_events, event_ids, query_label, summary)
    SELECT v_batch, 'ALERT_BATCH', in_now, v_quiet, v_n, ARRAY_AGG(alert_event_id), v_label,
           STRING_AGG(CONCAT(alert_type, ':', severity, ':', pipeline_id, ':', reason_code), '; ')
    FROM _dro_due;
  END IF;

  -- §C Дайджест 09:30 МСК: один раз в сутки. Забирает всё, что не ушло отдельно
  --    (MEDIUM, восстановления неотправленных открытий), и считает состояние.
  IF NOT v_quiet AND TIME(v_msk) >= TIME '09:30:00'
     AND NOT EXISTS (SELECT 1 FROM `evetis_health.ALERT_DISPATCH_LOG`
                     WHERE dispatch_id = v_digest
                       AND DATE(dispatched_at) >= DATE_SUB(DATE(in_now), INTERVAL 1 DAY)) THEN
    CREATE OR REPLACE TEMP TABLE _dro_digest_events AS
    SELECT alert_event_id
    FROM `wb_ops.OPS_ALERT_EVENT`
    WHERE scope = 'DRO_PIPELINE' AND state = 'RECORDED';

    SET v_label = (
      SELECT CONCAT(
        'dro_kind:digest,dro_component:dispatcher,dro_day:', FORMAT_DATE('%Y%m%d', DATE(v_msk)),
        ',dro_healthy:', CAST(COUNTIF(serving_status = 'HEALTHY') AS STRING),
        ',dro_degraded:', CAST(COUNTIF(serving_status = 'DEGRADED') AS STRING),
        ',dro_stale:', CAST(COUNTIF(serving_status = 'STALE') AS STRING),
        ',dro_blocked:', CAST(COUNTIF(serving_status = 'BLOCKED') AS STRING),
        ',dro_unknown:', CAST(COUNTIF(serving_status = 'UNKNOWN' AND evaluation_mode = 'EVALUATED') AS STRING),
        ',dro_not_evaluated:', CAST(COUNTIF(evaluation_mode <> 'EVALUATED') AS STRING),
        ',dro_known_loss:', CAST(COUNTIF(unrecoverable_gaps_35d > 0) AS STRING),
        ',dro_pending_events:', CAST((SELECT COUNT(*) FROM _dro_digest_events) AS STRING),
        ',dro_open_incidents:', CAST((SELECT COUNT(*) FROM `wb_ops.OPS_INCIDENT`
                                      WHERE scope = 'DRO_PIPELINE' AND state = 'OPEN') AS STRING),
        ',dro_attention:', `evetis_health.f_label_value`(
            STRING_AGG(IF(serving_status <> 'HEALTHY' AND evaluation_mode = 'EVALUATED', pipeline_id, NULL), '-'
                       ORDER BY pipeline_id)))
      FROM `evetis_health.DATA_HEALTH_SNAPSHOT`
      WHERE DATE(evaluated_at) = DATE(in_now) AND evaluated_at = in_now);

    SET @@query_label = v_label;
    SELECT 'dro_digest_marker' AS marker, v_digest AS digest_id;
    SET @@query_label = 'dro_component:dispatcher';

    UPDATE `wb_ops.OPS_ALERT_EVENT`
    SET state = 'HANDED_OFF', sent_at = in_now, attempt_count = attempt_count + 1,
        channel = 'EMAIL_DIGEST', recipient_mask = 'cloud-monitoring:ops-email'
    WHERE state = 'RECORDED' AND alert_event_id IN (SELECT alert_event_id FROM _dro_digest_events);

    INSERT INTO `evetis_health.ALERT_DISPATCH_LOG`
      (dispatch_id, kind, dispatched_at, quiet_hours, n_events, event_ids, query_label, summary)
    VALUES (v_digest, 'DIGEST', in_now, FALSE,
            (SELECT COUNT(*) FROM _dro_digest_events),
            ARRAY(SELECT alert_event_id FROM _dro_digest_events ORDER BY alert_event_id),
            v_label, 'Ежедневный дайджест DRO-1');
  END IF;
END;

CREATE OR REPLACE PROCEDURE `evetis_health.sp_evaluate_data_health`()
BEGIN
  DECLARE v_ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP();
  DECLARE v_run_id STRING DEFAULT CONCAT('dro1_', FORMAT_TIMESTAMP('%Y%m%dT%H%M%S', CURRENT_TIMESTAMP()), '_',
                                         SUBSTR(GENERATE_UUID(), 1, 8));
  DECLARE v_results ARRAY<STRUCT<
    scope STRING, scope_id STRING, health_status STRING, reason_code STRING,
    reason_text STRING, severity STRING, subject_key STRING,
    observed STRING, expected STRING, is_data_loss BOOL, is_recoverable BOOL>>;

  SET @@query_label = 'dro_component:detector';

  CREATE OR REPLACE TEMP TABLE _dro_eval AS
  SELECT * FROM `evetis_health.TVF_DATA_HEALTH`(v_ts);

  INSERT INTO `evetis_health.DATA_HEALTH_SNAPSHOT`
  SELECT *, v_run_id AS detector_run_id FROM _dro_eval;

  -- Только оцениваемые конвейеры с включённым оповещением. NOT_EVALUATED (ФФ, план,
  -- ручные и выключенные) в инциденты не попадают: их UNKNOWN честный и постоянный.
  SET v_results = ARRAY(
    SELECT AS STRUCT
      'DRO_PIPELINE', pipeline_id,
      IF(alertable, 'UNHEALTHY', 'HEALTHY'),
      IF(alertable, reason_code, 'OK'),
      reason_text,
      IF(alertable, severity, 'INFO'),
      pipeline_id,
      observed_text,
      expected_slot,
      reason_code IN ('DATA_LOSS_IMMINENT', 'DATA_LOSS_CONFIRMED'),
      reason_code <> 'DATA_LOSS_CONFIRMED'
    FROM _dro_eval
    WHERE evaluation_mode = 'EVALUATED' AND alerting_enabled);
  CALL `wb_ops.sp_ops_apply_health`(v_results, v_ts);

  -- Сбой доставки не должен отменять обнаружение: состояние уже записано выше.
  BEGIN
    CALL `evetis_health.sp_dispatch_alerts`(v_ts);
  EXCEPTION WHEN ERROR THEN
    SET @@query_label = 'dro_component:dispatcher,dro_kind:dispatch_error';
    INSERT INTO `evetis_health.ALERT_DISPATCH_LOG`
      (dispatch_id, kind, dispatched_at, quiet_hours, n_events, event_ids, query_label, summary)
    VALUES (CONCAT(v_run_id, '_dispatch_error'), 'DISPATCH_ERROR', v_ts, NULL, 0, [], NULL,
            SUBSTR(@@error.message, 1, 1000));
  END;

  -- Пульс для сторожа Cloud Monitoring: отсутствие этой отметки 90 минут = детектор
  -- не работает (BL-13: детектор не может сам сообщить о своей остановке).
  SET @@query_label = 'dro_component:detector,dro_kind:heartbeat';
  SELECT 'dro_heartbeat_marker' AS marker, v_run_id AS detector_run_id;
  SET @@query_label = 'dro_component:detector';
END;
