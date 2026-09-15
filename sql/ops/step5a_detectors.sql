-- STEP 5A observability detectors (read-only SELECT). Порог выведен из распределения 15.08–15.09:
--   критичный путь ADS (fullstats done от старта run): p50 225 c, p90 296 c, max 346 c (kill).
--   WARNING > 300 c (83 % стены 360), CRITICAL > 330 c (92 %).
DECLARE p STRING DEFAULT 'project-fa311fc0-4d87-4781-986';
WITH
stale AS (
  -- PRE-PR REVIEW: без разделения детектор вечно кричал CRITICAL на 4 исторических
  -- STARTED (11.09 ads, 29.08 sales, 19.08 orders/sales), которые закрываются только
  -- отдельным approval владельца. Свежие (< 24 ч) = живой инцидент, старые = долг.
  SELECT 'STALE_INGEST_RUN' check_id,
         IF(started_at < TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR), 'HIGH', 'CRITICAL') severity,
         loader_name || IF(started_at < TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR),
                           ' (historical, нужен отдельный approval)', '') subject,
         FORMAT('run_id=%s lp=%t age_min=%d', run_id, logical_period, TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), started_at, MINUTE)) observed,
         '<15 мин для apps_script' expected
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.INGEST_RUNS`
  WHERE status='STARTED' AND source='apps_script' AND started_at < TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 15 MINUTE)),
fs AS (
  SELECT run_id, PARSE_DATETIME('%Y%m%d_%H%M%S', SUBSTR(run_id,8,15)) t0,
         MAX(SAFE.PARSE_DATETIME('%F %T', SUBSTR(CAST(load_ts AS STRING),1,19))) fs_done
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_ADV_CAMPAIGN_STATS`
  WHERE _PARTITIONTIME >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 3 DAY) AND run_id LIKE 'ADSRAW_%' GROUP BY 1),
rt AS (
  SELECT 'ADS_RUNTIME_NEAR_LIMIT' check_id,
         CASE WHEN s > 330 THEN 'CRITICAL' WHEN s > 300 THEN 'WARNING' ELSE 'OK' END severity, run_id subject,
         FORMAT('fullstats_done=%d c', s) observed, '<=300 c' expected
  FROM (SELECT run_id, DATETIME_DIFF(fs_done, t0, SECOND) s FROM fs ORDER BY t0 DESC LIMIT 1)),
tail AS (
  SELECT 'ADS_TAIL_MISSING' check_id, IF(b.run_id IS NULL OR q.run_id IS NULL, 'HIGH', 'OK') severity, f.run_id subject,
         FORMAT('bids=%s query_stats=%s', IF(b.run_id IS NULL,'MISSING','OK'), IF(q.run_id IS NULL,'MISSING','OK')) observed, 'оба снимка есть' expected
  FROM (SELECT run_id FROM fs ORDER BY t0 DESC LIMIT 1) f
  LEFT JOIN (SELECT DISTINCT run_id FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_ADV_QUERY_BIDS_RUNS` WHERE _PARTITIONTIME >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 3 DAY)) b USING(run_id)
  LEFT JOIN (SELECT DISTINCT run_id FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_ADV_QUERY_STATS_RUNS` WHERE _PARTITIONTIME >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 3 DAY)) q USING(run_id)),
mart AS (
  SELECT 'MART_BLOCKED_BY_SOURCE' check_id, IF(error_code='FRESHNESS_GATE', 'CRITICAL', 'OK') severity, CAST(target_date AS STRING) subject,
         FORMAT('%s %s %s', status, IFNULL(error_code,''), IFNULL(SUBSTR(error_message,1,90),'')) observed, 'COMPLETE' expected
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.MART_RUNS` WHERE environment='prod' QUALIFY ROW_NUMBER() OVER (ORDER BY started_at DESC)=1),
lcd AS (
  SELECT 'UNITKA_LCD_LAG' check_id,
         CASE WHEN last_closed_date >= d1_msk THEN 'OK'
              WHEN EXTRACT(HOUR FROM CURRENT_DATETIME('Europe/Moscow')) >= 11 THEN 'CRITICAL' ELSE 'PENDING' END severity,
         CAST(last_closed_date AS STRING) subject,
         FORMAT('lcd=%t d1=%t gating=%s', last_closed_date, d1_msk, TO_JSON_STRING(gating_sources)) observed, 'lcd = D−1 после 11:00 МСК' expected
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_LAST_CLOSED_DATE`)
SELECT * FROM stale UNION ALL SELECT * FROM rt UNION ALL SELECT * FROM tail UNION ALL SELECT * FROM mart UNION ALL SELECT * FROM lcd
ORDER BY check_id;
