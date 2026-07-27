-- ============================================================================
-- Mig1 (остатки) — ежедневная сверка shadow (Cloud Run) ↔ prod (Apps Script).
-- Гонять во время parity-окна (≥5 дней) ПОСЛЕ включения shadow scheduler
-- (scheduler-control: environment=shadow, loader=wb-stocks, action=resume),
-- когда shadow и prod снимаются почти одновременно (~06:30 vs ~06:23 МСК).
--
-- Источники (dataset wb_raw, регион EU):
--   prod   manifest : WB_STOCKS_SNAPSHOTS        (Apps Script)
--   shadow manifest : WB_STOCKS_SNAPSHOTS__CR    (Cloud Run)
--   shadow ран      : LOADER_RUNS (environment='shadow', loader_name='stocks')
--   shadow данные   : RAW_WB_STOCKS__CR
--
-- Ключ дня — period_to снимка. Если за день несколько снимков — берём ПОСЛЕДНИЙ
-- (по started_at), независимо от статуса; статус классифицируется в CASE, чтобы
-- отличать MISSING (манифеста нет) от FAIL (манифест есть, но ERROR).
--
-- ВАЖНО про parity остатков: это снимок «живого» объекта. Количественная сверка
-- (d_rows/d_qty) имеет смысл ТОЛЬКО когда снимки почти одновременны. gap_min —
-- разница во времени между снимками; при gap_min > gap_skew_min статус = SKEW
-- (расхождения объясняются временем, а не алгоритмом — не тревога).
--
-- Статусы QUERY 1:
--   MISSING — нет снимка на одной из сторон за этот день.
--   FAIL    — нарушен жёсткий инвариант (см. ниже) → разбираться.
--   SKEW    — снимки слишком далеко по времени (gap_min > порога); Δ не значимы.
--   WARN    — снимки одновременны, но Δrows/Δqty вне допуска или nm разошёлся.
--   OK      — одновременны и совпали в пределах допуска.
-- Жёсткие инварианты (любой сбой → FAIL): оба манифеста COMPLETE; shadow-ран
--   COMPLETE и rows_fetched=rows_loaded; shadow duplicate_keys=0;
--   shadow written_rows=distinct_keys.
--
-- Допуски вынесены в params (подстраивай под реальный разброс за первые дни).
-- ============================================================================

-- ─────────────────────────────────────────────────────────────────────────
-- QUERY 1 — PARITY BY DAY (заголовочный отчёт)
-- ─────────────────────────────────────────────────────────────────────────
WITH params AS (
  SELECT 2 AS tol_rows, 30 AS tol_qty, 60 AS gap_skew_min, 14 AS lookback_days
),
prod AS (
  SELECT * FROM (
    SELECT SAFE.PARSE_DATE('%Y-%m-%d', period_to) AS d, started_at,
           written_rows AS row_cnt, sum_quantity_all_t6 AS qty, unique_nm_ids AS nm,
           distinct_keys, duplicate_keys, status,
           ROW_NUMBER() OVER (PARTITION BY period_to ORDER BY started_at DESC) AS rn
    FROM `wb_raw.WB_STOCKS_SNAPSHOTS` WHERE period_to IS NOT NULL
  ) WHERE rn=1
),
shad AS (
  SELECT * FROM (
    SELECT SAFE.PARSE_DATE('%Y-%m-%d', period_to) AS d, started_at,
           written_rows AS row_cnt, sum_quantity_all_t6 AS qty, unique_nm_ids AS nm,
           distinct_keys, duplicate_keys, status,
           ROW_NUMBER() OVER (PARTITION BY period_to ORDER BY started_at DESC) AS rn
    FROM `wb_raw.WB_STOCKS_SNAPSHOTS__CR` WHERE period_to IS NOT NULL
  ) WHERE rn=1
),
run AS (
  SELECT * FROM (
    SELECT SAFE.PARSE_DATE('%Y-%m-%d', logical_period) AS d, status AS run_status,
           rows_fetched, rows_loaded,
           ROW_NUMBER() OVER (PARTITION BY logical_period ORDER BY started_at DESC) AS rn
    FROM `wb_raw.LOADER_RUNS` WHERE environment='shadow' AND loader_name='stocks'
  ) WHERE rn=1
)
SELECT
  CAST(COALESCE(p.d, s.d) AS STRING) AS d,
  p.row_cnt AS prod_rows, s.row_cnt AS shadow_rows, s.row_cnt - p.row_cnt AS d_rows,
  p.qty AS prod_qty, s.qty AS shadow_qty, s.qty - p.qty AS d_qty,
  p.nm AS prod_nm, s.nm AS shadow_nm,
  ABS(TIMESTAMP_DIFF(p.started_at, s.started_at, MINUTE)) AS gap_min,
  r.run_status, r.rows_fetched, r.rows_loaded,
  CASE
    WHEN p.d IS NULL OR s.d IS NULL THEN 'MISSING'
    WHEN p.status!='COMPLETE' OR s.status!='COMPLETE'
      OR r.run_status IS DISTINCT FROM 'COMPLETE'
      OR r.rows_fetched != r.rows_loaded
      OR s.duplicate_keys != 0 OR s.row_cnt != s.distinct_keys THEN 'FAIL'
    WHEN ABS(TIMESTAMP_DIFF(p.started_at, s.started_at, MINUTE)) > (SELECT gap_skew_min FROM params) THEN 'SKEW'
    WHEN ABS(s.row_cnt - p.row_cnt) <= (SELECT tol_rows FROM params)
      AND ABS(s.qty - p.qty) <= (SELECT tol_qty FROM params)
      AND s.nm = p.nm THEN 'OK'
    ELSE 'WARN'
  END AS status
FROM prod p
FULL OUTER JOIN shad s ON p.d = s.d
LEFT JOIN run r ON r.d = COALESCE(p.d, s.d)
WHERE COALESCE(p.d, s.d) >= DATE_SUB(CURRENT_DATE('Europe/Moscow'), INTERVAL (SELECT lookback_days FROM params) DAY)
ORDER BY d DESC;

-- ─────────────────────────────────────────────────────────────────────────
-- QUERY 2 — SHADOW RUN HEALTH (детально, для разбора FAIL)
-- Проверяет цепочку «пять независимых чисел должны совпасть»:
--   rows_fetched = rows_loaded = expected_rows = written_rows = raw_rows,
-- плюс duplicate_keys=0 и written_rows=distinct_keys. all_green=true — норма.
-- ─────────────────────────────────────────────────────────────────────────
WITH run AS (
  SELECT * FROM (
    SELECT SAFE.PARSE_DATE('%Y-%m-%d', logical_period) AS d, status AS run_status,
           rows_fetched, rows_loaded,
           ROW_NUMBER() OVER (PARTITION BY logical_period ORDER BY started_at DESC) AS rn
    FROM `wb_raw.LOADER_RUNS` WHERE environment='shadow' AND loader_name='stocks'
  ) WHERE rn=1
),
man AS (
  SELECT * FROM (
    SELECT SAFE.PARSE_DATE('%Y-%m-%d', period_to) AS d, snapshot_id, status AS man_status,
           expected_rows, written_rows, distinct_keys, duplicate_keys, control_status,
           ROW_NUMBER() OVER (PARTITION BY period_to ORDER BY started_at DESC) AS rn
    FROM `wb_raw.WB_STOCKS_SNAPSHOTS__CR` WHERE period_to IS NOT NULL
  ) WHERE rn=1
),
raw AS (
  SELECT snapshot_id, COUNT(*) AS raw_rows FROM `wb_raw.RAW_WB_STOCKS__CR` GROUP BY snapshot_id
)
SELECT CAST(m.d AS STRING) AS d, r.run_status, m.man_status,
       r.rows_fetched, r.rows_loaded, m.expected_rows, m.written_rows,
       rw.raw_rows, m.distinct_keys, m.duplicate_keys, m.control_status,
       (r.run_status='COMPLETE' AND m.man_status='COMPLETE'
        AND r.rows_fetched=r.rows_loaded AND m.expected_rows=m.written_rows
        AND rw.raw_rows=m.written_rows AND m.duplicate_keys=0
        AND m.written_rows=m.distinct_keys) AS all_green
FROM man m
LEFT JOIN run r ON r.d = m.d
LEFT JOIN raw rw ON rw.snapshot_id = m.snapshot_id
WHERE m.d >= DATE_SUB(CURRENT_DATE('Europe/Moscow'), INTERVAL 14 DAY)
ORDER BY m.d DESC;
