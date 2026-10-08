-- ============================================================================
-- DRO-1 · §7 Журнал отказов прогонов (AE-R1). Второй канонический сигнал для
-- наблюдателя Autonomous Engineering: DRO-1 смотрит на уровне ДАННЫХ, а дефект кода,
-- после которого данные всё равно дошли (повтор, ручной перезапуск), DRO не видит.
-- Кейс: 2026-10-07 09:30 UTC unitka-engine-prod — Google Sheets 503 записан как
-- детерминированный SHEETS_API, а DRO весь день показывал unitka_wb HEALTHY.
--
--   SELECT * FROM evetis_health.V_RUN_FAILURE_LEDGER WHERE last_seen_at > ...
--
-- 🔴 Текст ошибки НЕ выходит из BigQuery: представление отдаёт только код, сигнатуру
-- (перечисление, вычисленное здесь из текста), отпечаток нормализованного текста, счётчики
-- и время. Наблюдатель AE пишет сигналы в публичные места (политика вывода B3), поэтому
-- error_message сюда не попадает ни в каком виде, кроме sha256.
--
-- Источники — журналы прогонов с error_code/error_message:
--   wb_raw.LOADER_RUNS (Cloud Run: unitka, ozon-unitka, mart, stocks, prices, funnel, …),
--   wb_raw.INGEST_RUNS (Apps Script: orders, sales, ads), wb_raw.WB_PRICES_OBSERVATIONS,
--   wb_raw.WB_TARIFF_OBSERVATIONS. Отказы Unitka Engine приходят через LOADER_RUNS
--   (loader_name = 'unitka'), поэтому wb_ops.UNITKA_ENGINE_RUNS не дублируется.
-- Окно — 30 суток; группа — (журнал, загрузчик, среда, код, отпечаток).
-- Статус «восстановлено» — был ли после последнего отказа успешный прогон того же
-- загрузчика в той же среде.
--
-- Развёртывание — отдельные ворота (DDL в production). Только чтение источников.
-- ============================================================================
CREATE OR REPLACE VIEW `evetis_health.V_RUN_FAILURE_LEDGER`
OPTIONS (description = 'DRO-1 / AE-R1. Отказы прогонов загрузчиков за 30 суток: код, сигнатура, отпечаток текста (без самого текста), повторяемость, последующее восстановление. Канонический вход наблюдателя AE. Только чтение.') AS
WITH
runs AS (
  SELECT 'LOADER_RUNS' AS source_log, loader_name, environment, run_id, started_at, status, error_code, error_message
  FROM `wb_raw.LOADER_RUNS`
  WHERE started_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
  UNION ALL
  SELECT 'INGEST_RUNS', loader_name, 'prod', run_id, started_at, status, error_code, error_message
  FROM `wb_raw.INGEST_RUNS`
  WHERE started_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
  UNION ALL
  SELECT 'WB_PRICES_OBSERVATIONS', 'prices_observer', environment, run_id, started_at, status, error_code, error_message
  FROM `wb_raw.WB_PRICES_OBSERVATIONS`
  WHERE started_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
  UNION ALL
  SELECT 'WB_TARIFF_OBSERVATIONS', 'tariffs_observer', environment, run_id, started_at, status, error_code, error_message
  FROM `wb_raw.WB_TARIFF_OBSERVATIONS`
  WHERE started_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)),
normalized AS (
  SELECT r.*,
         LOWER(IFNULL(r.error_message, '')) AS msg,
         UPPER(IFNULL(r.error_code, '')) AS code,
         r.status IN ('COMPLETE', 'OK', 'OK_NO_NEW', 'SUCCESS') AS is_success,
         -- Отпечаток: числа, длинные hex и идентификаторы таблиц/диапазонов схлопнуты, чтобы один и
         -- тот же отказ на разных датах и листах давал один отпечаток.
         SUBSTR(TO_HEX(SHA256(REGEXP_REPLACE(REGEXP_REPLACE(LOWER(IFNULL(r.error_message, '')),
                                                           r'[0-9a-f]{12,}|[A-Za-z0-9_-]{30,}', '<id>'),
                                            r'\d+', '#'))), 1, 16) AS message_fingerprint
  FROM runs r),
-- Сигнатура — перечисление из текста ошибки; сам текст дальше этого CTE не идёт.
signatures AS (
  SELECT n.*,
         CASE
           -- Только однозначные признаки: HTTP-статус в контексте («status code 503», «HTTP 503»,
           -- «503 Service Unavailable»), формулировки Google/BigQuery и сетевые коды. Голое «500» в тексте
           -- («получено 500 диапазонов») временным сбоем НЕ считается.
           WHEN REGEXP_CONTAINS(n.msg, r'status code 5\d\d|http(?:/[\d.]+)?[ :]+5\d\d|\b5\d\d (?:service unavailable|bad gateway|gateway timeout|internal server error)|service is currently unavailable|service unavailable|backenderror|retrying the job may solve|\beconnreset\b|\betimedout\b|\beai_again\b|socket hang up|deadline exceeded')
                OR REGEXP_CONTAINS(n.code, r'(^|_)(TIMEOUT|RATE_LIMIT|TRANSIENT|UNAVAILABLE)(_|$)')
                OR REGEXP_CONTAINS(n.msg, r'status code 429|http(?:/[\d.]+)?[ :]+429|too many requests|rate limit exceeded')
             THEN 'TRANSIENT_UPSTREAM'
           WHEN REGEXP_CONTAINS(n.msg, r'status code 40[13]|http(?:/[\d.]+)?[ :]+40[13]|unauthori[sz]ed|permission denied|invalid token')
             THEN 'AUTH'
           WHEN REGEXP_CONTAINS(n.msg, r'quota exceeded|quota_exceeded|billing')
             THEN 'QUOTA'
           WHEN REGEXP_CONTAINS(n.msg, r'unknown field|unexpected field|missing field|schema|cannot parse|failed to parse|unexpected token')
                OR REGEXP_CONTAINS(n.code, r'(^|_)(SCHEMA|PARSE|UNKNOWN_FIELD)(_|$)')
             THEN 'SCHEMA'
           WHEN REGEXP_CONTAINS(n.msg, r'parity|assert|reconcil|integrity')
                OR REGEXP_CONTAINS(n.code, r'(^|_)(PARITY|QA|ASSERT|INTEGRITY)(_|$)')
             THEN 'PARITY_QA'
           ELSE 'OTHER'
         END AS failure_signature,
         REGEXP_CONTAINS(n.code, r'(^|_)(TRANSIENT|RATE_LIMIT|TIMEOUT|RETRY|UNAVAILABLE)(_|$)') AS recorded_as_transient
  FROM normalized n),
failures AS (
  -- Без исходных error_code/error_message: дальше идут только код (code), сигнатура и отпечаток.
  SELECT * EXCEPT (error_code, error_message, msg)
  FROM signatures WHERE NOT is_success AND (code <> '' OR status IN ('ERROR', 'FAILED'))),
grouped AS (
  SELECT source_log, loader_name, environment,
         NULLIF(code, '') AS error_code, failure_signature, recorded_as_transient, message_fingerprint,
         COUNT(*) AS occurrences_30d,
         COUNTIF(started_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)) AS occurrences_7d,
         MIN(started_at) AS first_seen_at,
         MAX(started_at) AS last_seen_at,
         ARRAY_AGG(run_id IGNORE NULLS ORDER BY started_at DESC LIMIT 1)[SAFE_OFFSET(0)] AS last_run_id
  FROM failures
  GROUP BY source_log, loader_name, environment, error_code, failure_signature, recorded_as_transient,
           message_fingerprint),
recovery AS (
  SELECT g.source_log, g.loader_name, g.environment, g.error_code, g.message_fingerprint,
         MIN(s.started_at) AS recovered_at
  FROM grouped g
  JOIN signatures s
    ON s.source_log = g.source_log AND s.loader_name = g.loader_name
   AND IFNULL(s.environment, '') = IFNULL(g.environment, '')
   AND s.is_success AND s.started_at > g.last_seen_at
  GROUP BY g.source_log, g.loader_name, g.environment, g.error_code, g.message_fingerprint)
SELECT
  g.source_log, g.loader_name, g.environment, g.error_code, g.failure_signature, g.recorded_as_transient,
  g.message_fingerprint, g.occurrences_7d, g.occurrences_30d, g.first_seen_at, g.last_seen_at, g.last_run_id,
  r.recovered_at,
  CASE WHEN r.recovered_at IS NULL THEN 'NOT_RECOVERED' ELSE 'RECOVERED' END AS recovery_status,
  TIMESTAMP_DIFF(r.recovered_at, g.last_seen_at, MINUTE) AS minutes_to_recovery
FROM grouped g
LEFT JOIN recovery r
  ON r.source_log = g.source_log AND r.loader_name = g.loader_name
 AND IFNULL(r.environment, '') = IFNULL(g.environment, '')
 AND IFNULL(r.error_code, '') = IFNULL(g.error_code, '')
 AND r.message_fingerprint = g.message_fingerprint;
