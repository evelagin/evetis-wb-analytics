-- ============================================================================
-- PHASE 2 · D2 — минимальное покрытие здоровьем: Ozon, orders, sales.
--
-- Существующая процедура wb_ops.sp_evaluate_pipeline_health НЕ изменяется. Новая
-- процедура пользуется тем же объявленным расширением — wb_ops.sp_ops_apply_health,
-- которая принимает готовый результат и применяет переходы состояния. Поэтому:
--   * радиус поражения ограничен новым объектом; действующий детектор не трогается;
--   * откат — DROP PROCEDURE, без восстановления чужого кода;
--   * lifecycle инцидентов, отпечатки условий и дедупликация оповещений — те же.
--
-- Порогов здесь не изобретено НИ ОДНОГО: все читаются из OPS_PIPELINE_REGISTRY
-- (freshness_sla_minutes, expected_business_lag_days), где они уже согласованы
-- владельцем. Для orders и sales строки реестра существуют с 2026-09-02.
--
-- Проверяются только доказуемые технические свойства:
--   OZ1  успешность последнего прогона сущности Ozon (status и errors из журнала);
--   OZ2  свежесть: с последнего OK прошло не больше freshness_sla_minutes;
--   H7   orders: свежесть последнего COMPLETE по порогу реестра;
--   H8   sales:  то же;
--   H9   orders: непрерывность дат заказа за последние 30 суток с учётом
--        expected_business_lag_days из реестра (пропуск суток внутри окна — дефект).
--
-- Каденс orders в реестре объявлен UNKNOWN, поэтому детектор пропущенных прогонов
-- к нему не применяется — и здесь тоже не применяется. Свежесть от каденса не зависит.
--
-- Идемпотентна: повторный вызов в том же состоянии не создаёт ни инцидентов, ни
-- дублей оповещений (отпечаток условия детерминирован).
-- Запись: только через sp_ops_apply_health → OPS_HEALTH_STATE / OPS_INCIDENT / OPS_ALERT_EVENT.
-- ============================================================================
CREATE OR REPLACE PROCEDURE `project-fa311fc0-4d87-4781-986.wb_ops.sp_evaluate_health_ext`()
BEGIN
  DECLARE v_ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP();
  DECLARE v_results ARRAY<STRUCT<scope STRING, scope_id STRING, health_status STRING,
    reason_code STRING, reason_text STRING, severity STRING, subject_key STRING,
    observed STRING, expected STRING, is_data_loss BOOL, is_recoverable BOOL>>;

  CREATE OR REPLACE TEMP TABLE _reg AS
  SELECT pipeline_id, criticality, freshness_sla_minutes, expected_business_lag_days,
         missed_run_data_loss, is_backfillable, target_object
  FROM `wb_ops.OPS_PIPELINE_REGISTRY`
  WHERE enabled AND environment = 'prod';

  -- Последний прогон каждой сущности Ozon и его исход.
  CREATE OR REPLACE TEMP TABLE _oz AS
  WITH last_run AS (
    SELECT entity, status, errors, started_at,
           ROW_NUMBER() OVER (PARTITION BY entity ORDER BY started_at DESC) rn
    FROM `ozon_raw.OZON_INGESTION_RUNS`
    WHERE started_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)),
  last_ok AS (
    SELECT entity, MAX(started_at) ok_at
    FROM `ozon_raw.OZON_INGESTION_RUNS`
    WHERE status = 'OK' AND IFNULL(errors, 0) = 0
      AND started_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 60 DAY)
    GROUP BY entity)
  SELECT r.pipeline_id, l.entity, l.status, IFNULL(l.errors, 0) AS errors,
         o.ok_at,
         TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), o.ok_at, MINUTE) AS ok_age_min,
         r.freshness_sla_minutes, r.criticality, r.missed_run_data_loss, r.is_backfillable
  FROM (SELECT * FROM last_run WHERE rn = 1) l
  LEFT JOIN last_ok o USING (entity)
  -- Соответствие сущности журнала и pipeline_id реестра выводится из target_object,
  -- а не из совпадения имён: имя сущности в журнале и идентификатор конвейера разные.
  JOIN _reg r ON r.target_object = CONCAT('ozon_raw.RAW_OZON_', UPPER(l.entity))
              OR (l.entity = 'fbo_postings' AND r.pipeline_id = 'ozon_fbo_postings')
              OR (l.entity = 'ads_sku_daily' AND r.pipeline_id = 'ozon_ads_sku_daily')
              OR (l.entity = 'ads_expense_daily' AND r.pipeline_id = 'ozon_ads_expense_daily')
              OR (l.entity = 'ads_campaigns' AND r.pipeline_id = 'ozon_ads_campaigns')
              OR (l.entity = 'finance_accrual' AND r.pipeline_id = 'ozon_finance_accrual')
              OR (l.entity = 'seller_info' AND r.pipeline_id = 'ozon_seller_info');

  -- WB orders / sales: свежесть последнего COMPLETE против порога реестра.
  CREATE OR REPLACE TEMP TABLE _wb AS
  SELECT r.pipeline_id, r.criticality, r.freshness_sla_minutes,
         MAX(i.completed_at) AS last_complete,
         TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), MAX(i.completed_at), MINUTE) AS age_min
  FROM _reg r
  JOIN `wb_raw.INGEST_RUNS` i
    ON i.loader_name = r.pipeline_id AND i.status = 'COMPLETE'
   AND i.completed_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
  WHERE r.pipeline_id IN ('orders', 'sales')
  GROUP BY 1, 2, 3;

  -- Непрерывность дат заказа: сутки без единой строки внутри окна — дефект загрузки,
  -- потому что заказы у бренда идут каждый день (проверяется на самом окне, не на вере).
  CREATE OR REPLACE TEMP TABLE _cont AS
  WITH reg AS (SELECT IFNULL(expected_business_lag_days, 1) lag FROM _reg WHERE pipeline_id = 'orders'),
  cal AS (
    SELECT d FROM UNNEST(GENERATE_DATE_ARRAY(
      DATE_SUB(CURRENT_DATE('Europe/Moscow'), INTERVAL 30 DAY),
      DATE_SUB(CURRENT_DATE('Europe/Moscow'), INTERVAL (SELECT lag FROM reg) DAY))) d),
  -- Колонка даты заказа в RAW_WB_ORDERS называется `_order_date` (DATE).
  -- Проверено запросом к INFORMATION_SCHEMA, а не по памяти: `order_date` там нет.
  have AS (SELECT DISTINCT _order_date d FROM `wb_raw.RAW_WB_ORDERS`
           WHERE _order_date >= DATE_SUB(CURRENT_DATE('Europe/Moscow'), INTERVAL 30 DAY))
  SELECT COUNT(*) AS missing_days,
         IFNULL(STRING_AGG(FORMAT_DATE('%F', cal.d) ORDER BY cal.d LIMIT 10), '-') AS sample
  FROM cal LEFT JOIN have USING (d) WHERE have.d IS NULL;

  SET v_results = ARRAY(
    SELECT AS STRUCT
      'PIPELINE_CHECK' AS scope,
      CONCAT(pipeline_id, '/OZ1_OZON_RUN_FAILED') AS scope_id,
      IF(status != 'OK' OR errors > 0, 'UNHEALTHY', 'HEALTHY') AS health_status,
      IF(status != 'OK' OR errors > 0, 'OZON_RUN_FAILED', 'OK') AS reason_code,
      FORMAT('Последний прогон %s: статус %s, ошибок %d', entity, status, errors) AS reason_text,
      criticality AS severity,
      entity AS subject_key,
      FORMAT('%s / %d ошибок', status, errors) AS observed,
      'OK / 0 ошибок' AS expected,
      missed_run_data_loss AS is_data_loss,
      is_backfillable AS is_recoverable
    FROM _oz);

  SET v_results = ARRAY_CONCAT(v_results, ARRAY(
    SELECT AS STRUCT
      'PIPELINE_CHECK',
      CONCAT(pipeline_id, '/OZ2_OZON_INGESTION_STALE'),
      IF(ok_at IS NULL OR ok_age_min > freshness_sla_minutes, 'UNHEALTHY', 'HEALTHY'),
      IF(ok_at IS NULL OR ok_age_min > freshness_sla_minutes, 'OZON_INGESTION_STALE', 'OK'),
      IF(ok_at IS NULL,
         FORMAT('За 60 суток нет ни одного успешного прогона %s', entity),
         FORMAT('Последний успешный прогон %s — %.1f ч назад', entity, ok_age_min / 60)),
      criticality,
      FORMAT_DATE('%F', CURRENT_DATE('Europe/Moscow')),
      IF(ok_at IS NULL, 'нет успешных прогонов', FORMAT('%.1f ч', ok_age_min / 60)),
      FORMAT('<= %.1f ч', freshness_sla_minutes / 60),
      missed_run_data_loss,
      is_backfillable
    FROM _oz));

  SET v_results = ARRAY_CONCAT(v_results, ARRAY(
    SELECT AS STRUCT
      'PIPELINE_CHECK',
      CONCAT(pipeline_id, IF(pipeline_id = 'orders', '/H7_ORDERS_STALE', '/H8_SALES_STALE')),
      IF(age_min > freshness_sla_minutes, 'UNHEALTHY', 'HEALTHY'),
      IF(age_min > freshness_sla_minutes, 'INGESTION_STALE', 'OK'),
      FORMAT('Последняя успешная загрузка %s — %.1f ч назад (порог реестра %.1f ч)',
             pipeline_id, age_min / 60, freshness_sla_minutes / 60),
      criticality,
      FORMAT_DATE('%F', CURRENT_DATE('Europe/Moscow')),
      FORMAT('%.1f ч', age_min / 60),
      FORMAT('<= %.1f ч', freshness_sla_minutes / 60),
      FALSE,
      TRUE
    FROM _wb));

  SET v_results = ARRAY_CONCAT(v_results, ARRAY(
    SELECT AS STRUCT
      'PIPELINE_CHECK',
      'orders/H9_ORDERS_DATE_GAP',
      IF(missing_days > 0, 'UNHEALTHY', 'HEALTHY'),
      IF(missing_days > 0, 'ORDERS_DATE_GAP', 'OK'),
      IF(missing_days > 0,
         CONCAT('Сутки без единого заказа в окне 30 дней: ', sample),
         'Пропусков суток в окне 30 дней нет'),
      'CRITICAL',
      FORMAT_DATE('%F', CURRENT_DATE('Europe/Moscow')),
      FORMAT('%d суток без строк', missing_days),
      '0 суток',
      FALSE,
      TRUE
    FROM _cont));

  CALL `wb_ops.sp_ops_apply_health`(v_results, v_ts);
END;
