-- ============================================================================
-- PR-Mart3a — read-only контрольные запросы (аудитор / приёмка / наблюдение).
-- Ничего не создаёт. Прогонять ПОСЛЕ применения pr_mart3a_ingest_runs.sql
-- и ручных тестов логгера (ingestSelfTest* в apps-script/IngestRunLog.gs).
-- ============================================================================

-- 1) Физика таблицы: партиционирование по DATE(started_at), кластеризация 3 колонки.
--    Ожидание: part_cols=1, clust_cols=3.
SELECT
  COUNTIF(is_partitioning_column = 'YES')            AS part_cols,   -- 1
  COUNTIF(clustering_ordinal_position IS NOT NULL)   AS clust_cols   -- 3 (loader_name, status, logical_period)
FROM `wb_raw.INFORMATION_SCHEMA.COLUMNS`
WHERE table_name = 'INGEST_RUNS';

-- 2) Модель «одна строка на run»: run_id уникален. Ожидание: dupes=0.
SELECT COUNT(*) AS rows_all,
       COUNT(DISTINCT run_id) AS distinct_runs,
       COUNT(*) - COUNT(DISTINCT run_id) AS dupes          -- 0
FROM `wb_raw.INGEST_RUNS`;

-- 3) Терминальность и зависшие раны.
--    stuck_started — STARTED старше 2 часов ((!) сигнал: загрузчик умер, не финализировав).
SELECT
  COUNTIF(status = 'STARTED')  AS started_now,
  COUNTIF(status = 'COMPLETE') AS complete_all,
  COUNTIF(status = 'ERROR')    AS error_all,
  COUNTIF(status = 'STARTED' AND started_at < TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 2 HOUR)) AS stuck_started,
  COUNTIF(status <> 'STARTED' AND completed_at IS NULL) AS terminal_without_completed_at   -- 0
FROM `wb_raw.INGEST_RUNS`;

-- 4) ZERO-ROW SUCCESS (ключевой контракт): успешные раны с rows_loaded=0 существуют и валидны.
--    После ingestSelfTestZeroRows() ожидание: zero_row_complete >= 1.
SELECT COUNTIF(status = 'COMPLETE' AND IFNULL(rows_loaded, 0) = 0) AS zero_row_complete,
       COUNTIF(status = 'COMPLETE' AND rows_loaded > 0)            AS nonzero_complete
FROM `wb_raw.INGEST_RUNS`;

-- 5) Идемпотентность финализации: у каждого run_id ровно один терминальный статус.
--    Ожидание: bad=0 (нет run_id с двумя разными терминальными статусами).
SELECT COUNT(*) AS bad
FROM (
  SELECT run_id
  FROM `wb_raw.INGEST_RUNS`
  WHERE status IN ('COMPLETE', 'ERROR')
  GROUP BY run_id
  HAVING COUNT(DISTINCT status) > 1
);

-- 6) Heartbeat-вью: объединяет Apps Script и Cloud Run без потери схемы.
SELECT source, loader_name, status, COUNT(*) n,
       CAST(MAX(completed_at) AS STRING) last_complete,
       CAST(MAX(logical_period) AS STRING) max_period
FROM `wb_raw.V_INGEST_HEARTBEAT`
GROUP BY 1, 2, 3
ORDER BY source, loader_name, status;

-- 7) ЭТАЛОН ГЕЙТА Mart3 — LATEST-ATTEMPT семантика (то, что спрашивает оркестратор для target_date = D-1).
--    ПРАВИЛО: последняя попытка каждого загрузчика за logical_period=target_date (по started_at,
--    tie-break run_id) обязана быть COMPLETE. «Существует хоть один COMPLETE» НЕЛЬЗЯ: ранний ночной
--    успех + поздний ERROR/PARTIAL дали бы ложный covers_target=TRUE и сборку на неполных данных
--    (00:10 COMPLETE → 08:00 ERROR → 09:00 build). Обязательный список — слева через LEFT JOIN:
--    полностью отсутствующий загрузчик даёт FALSE, а не исчезает из результата (fail-closed).
--    Ожидание после 1-2 суток наблюдения: covers_target = TRUE у всех трёх.
WITH t AS (
  SELECT DATE_SUB(CURRENT_DATE('Europe/Moscow'), INTERVAL 1 DAY) AS target_date
),
required AS (
  SELECT l AS loader_name FROM UNNEST(['orders', 'sales', 'ads']) AS l
),
attempts AS (
  SELECT
    h.loader_name, h.status, h.completed_at,
    ROW_NUMBER() OVER (
      PARTITION BY h.loader_name
      ORDER BY h.started_at DESC, h.run_id DESC
    ) AS rn
  FROM `wb_raw.V_INGEST_HEARTBEAT` h, t
  WHERE h.loader_name IN ('orders', 'sales', 'ads')
    AND h.logical_period = t.target_date
    AND h.started_at >= TIMESTAMP(DATE_ADD(t.target_date, INTERVAL 1 DAY), 'Europe/Moscow')
)
SELECT
  r.loader_name,
  (SELECT target_date FROM t) AS target_date,
  COALESCE(l.status = 'COMPLETE' AND l.completed_at IS NOT NULL, FALSE) AS covers_target,
  l.status AS latest_status   -- диагностика: NULL = ни одной попытки за сутки (полностью отсутствует)
FROM required r
LEFT JOIN (SELECT loader_name, status, completed_at FROM attempts WHERE rn = 1) l
  USING (loader_name)
ORDER BY r.loader_name;

-- 8) Ежедневное покрытие за последние 7 суток (наблюдение перед стартом Mart3b).
--    Ожидание: у каждого загрузчика COMPLETE на КАЖДЫЙ день (в т.ч. дни без продаж).
SELECT logical_period, loader_name,
       COUNTIF(status = 'COMPLETE') AS complete_runs,
       COUNTIF(status = 'ERROR')    AS error_runs,
       SUM(IFNULL(rows_loaded, 0))  AS rows_loaded_total
FROM `wb_raw.V_INGEST_HEARTBEAT`
WHERE logical_period >= DATE_SUB(CURRENT_DATE('Europe/Moscow'), INTERVAL 7 DAY)
  AND loader_name IN ('orders', 'sales', 'ads')
GROUP BY 1, 2
ORDER BY logical_period DESC, loader_name;

-- 9) Secondary sanity (НЕ гейт): бизнес-даты RAW с КОРРЕКТНЫМ парсингом строкового времени.
--    Формат '2026-07-31 11:31:47' без TZ → '%Y-%m-%d %H:%M:%S' (ISO-парсер даёт NULL!).
SELECT 'orders' AS src,
  CAST(MAX(SAFE.PARSE_TIMESTAMP('%Y-%m-%d %H:%M:%S', loaded_at)) AS STRING) AS last_loaded,
  CAST(MAX(_order_date) AS STRING) AS business_max
FROM `wb_raw.RAW_WB_ORDERS`
UNION ALL SELECT 'sales',
  CAST(MAX(SAFE.PARSE_TIMESTAMP('%Y-%m-%d %H:%M:%S', loaded_at)) AS STRING),
  CAST(MAX(_sale_date) AS STRING)
FROM `wb_raw.RAW_WB_SALES_RETURNS`
UNION ALL SELECT 'ads',
  CAST(MAX(SAFE.PARSE_TIMESTAMP('%Y-%m-%d %H:%M:%S', load_ts)) AS STRING),
  CAST(MAX(SAFE.PARSE_DATE('%Y-%m-%d', period_to)) AS STRING)
FROM `wb_raw.RAW_WB_ADV_CAMPAIGN_STATS`;

-- 10) ДОМЕН СТАТУСОВ (follow-up аудита): в журнале допустимы только STARTED/COMPLETE/ERROR,
--     source — только apps_script/cloud_run. Ожидание: status_domain_bad = 0, source_domain_bad = 0.
--     Ловит регресс, при котором «сырой» статус загрузчика (PARTIAL/SKIPPED_*/STALE) попал бы
--     в журнал напрямую в обход whitelist ingestFinalizeByStatus_().
SELECT
  COUNTIF(status IS NULL OR status NOT IN ('STARTED','COMPLETE','ERROR')) AS status_domain_bad,   -- 0
  COUNTIF(source IS NULL OR source NOT IN ('apps_script','cloud_run'))    AS source_domain_bad,   -- 0
  COUNTIF(logical_period IS NULL)                                         AS null_period,         -- 0
  COUNTIF(status <> 'STARTED' AND completed_at IS NULL)                   AS terminal_no_ts       -- 0
FROM `wb_raw.INGEST_RUNS`;

-- 11) WHITELIST-регресс: успешные heartbeat'ы не должны нести код неуспешного статуса.
--     Ожидание: 0 строк. (COMPLETE с error_code вида *_PARTIAL/*_SKIPPED_* = дефект финализации.)
SELECT COUNT(*) AS complete_with_failure_code                                                     -- 0
FROM `wb_raw.INGEST_RUNS`
WHERE status = 'COMPLETE'
  AND (REGEXP_CONTAINS(IFNULL(error_code, ''), r'PARTIAL|SKIPPED|SINK_OFF|UNKNOWN'));
