-- ============================================================================
-- M3 (реклама WB) — сверка паритета shadow (Cloud Run, RAW_WB_ADV_*__CR) ↔ prod (Apps Script,
-- RAW_WB_ADV_*) для пятидневного окна приёмки. ТОЛЬКО ЧТЕНИЕ. Регион EU, dataset wb_raw.
-- Образец — sql/mig1c_stocks_t5_parity.sql. Runbook: docs/ops/MIG_ADS_RUNBOOK.md §3.
--
-- Единица сравнения — «прогон дня»: на каждой стороне последний суточный ран ADSRAW_ за сутки МСК,
-- записавший commit-marker расходов (RAW_WB_ADV_COSTS_RUNS). Apps Script ~05:00, shadow 05:40 МСК —
-- оба читают одни и те же окна (fullstats D−7…D−1, upd D−14…D−1, Ads-2 D−7…D−1).
-- Строки Apps Script: run_id 'ADSRAW_yyyyMMdd_HHmmss_<число>'; строки Cloud Run: '…_CR<хвост>'.
--
-- Вердикт дня — по трём mart-критичным источникам (как WB_ADS_EXPECTED_SOURCES = 3):
--   EXACT       — campaigns, costs и fullstats совпали построчно и поколоночно;
--   DATA_DRIFT  — те же ключи, разошлись только изменчивые атрибуты (статус кампании, updNum,
--                 метрики fullstats за свежие сутки) — WB поменял данные между 05:00 и 05:40;
--   SOURCE_GAP  — прогон одной из сторон не COMPLETE (ADS_PARTIAL/ADS_ERROR): ключи fullstats
--                 не сравниваются, день не засчитывается ни как EXACT, ни как FAIL (разбор вручную);
--   FAIL        — структурное расхождение: набор кампаний, ключи/суммы расходов, маркеры окон,
--                 ключи fullstats при обоих COMPLETE;
--   MISSING     — у одной из сторон нет прогона дня.
-- Ads-3 (ставки) и Ads-2 (query stats) — отдельная колонка `aux_verdict`, в вердикт дня не входят
-- (как и в журнал 'ads'): их расхождение не блокирует переключение, но разбирается.
--
-- Приёмка (runbook §3): 5 последовательных дней без FAIL и MISSING; costs_verdict = EXACT все 5 дней
-- (окно upd к 05:00 закрыто: граница UTC 03:00 МСК); не меньше 3 дней EXACT целиком.
-- Стоимость: ~10 таблиц × минимум 10 МБ ≈ 0,1 ГБ на запуск; партиции отсечены окном.
-- ============================================================================

-- ─────────────────────────────────────────────────────────────────────────
-- QUERY 1 — ВЕРДИКТ ПО ДНЯМ
-- ─────────────────────────────────────────────────────────────────────────
WITH params AS (SELECT 7 AS lookback_days),
win AS (
  SELECT DATE_SUB(CURRENT_DATE('Europe/Moscow'), INTERVAL (SELECT lookback_days FROM params) DAY) AS d0,
         TIMESTAMP(DATE_SUB(CURRENT_DATE('Europe/Moscow'), INTERVAL (SELECT lookback_days FROM params) + 1 DAY)) AS p0
),
-- ── прогон дня на каждой стороне ──
as_runs AS (
  SELECT DISTINCT run_id, PARSE_DATE('%Y%m%d', SUBSTR(run_id, 8, 8)) AS run_day
  FROM `wb_raw.RAW_WB_ADV_COSTS_RUNS`
  WHERE STARTS_WITH(run_id, 'ADSRAW_') AND NOT REGEXP_CONTAINS(run_id, r'_CR[0-9A-Za-z]+$')
    AND SAFE.PARSE_DATE('%Y%m%d', SUBSTR(run_id, 8, 8)) >= (SELECT d0 FROM win)
  QUALIFY ROW_NUMBER() OVER (PARTITION BY PARSE_DATE('%Y%m%d', SUBSTR(run_id, 8, 8)) ORDER BY run_id DESC) = 1
),
cr_runs AS (
  SELECT DISTINCT run_id, PARSE_DATE('%Y%m%d', SUBSTR(run_id, 8, 8)) AS run_day
  FROM `wb_raw.RAW_WB_ADV_COSTS_RUNS__CR`
  WHERE STARTS_WITH(run_id, 'ADSRAW_')
    AND SAFE.PARSE_DATE('%Y%m%d', SUBSTR(run_id, 8, 8)) >= (SELECT d0 FROM win)
  QUALIFY ROW_NUMBER() OVER (PARTITION BY PARSE_DATE('%Y%m%d', SUBSTR(run_id, 8, 8)) ORDER BY run_id DESC) = 1
),
-- ── исход журнала за D−1 прогона (LATEST-ATTEMPT, как гейт витрины) ──
as_journal AS (
  SELECT DATE_ADD(logical_period, INTERVAL 1 DAY) AS run_day, status, error_code
  FROM `wb_raw.INGEST_RUNS`
  WHERE loader_name = 'ads' AND logical_period >= DATE_SUB((SELECT d0 FROM win), INTERVAL 1 DAY)
    AND started_at >= (SELECT p0 FROM win)
  QUALIFY ROW_NUMBER() OVER (PARTITION BY logical_period ORDER BY started_at DESC, run_id DESC) = 1
),
cr_journal AS (
  SELECT DATE_ADD(SAFE.PARSE_DATE('%Y-%m-%d', logical_period), INTERVAL 1 DAY) AS run_day, status, error_code
  FROM `wb_raw.LOADER_RUNS`
  WHERE environment = 'shadow' AND loader_name = 'ads' AND started_at >= (SELECT p0 FROM win)
  QUALIFY ROW_NUMBER() OVER (PARTITION BY logical_period ORDER BY started_at DESC, run_id DESC) = 1
),
-- ── campaigns: ключ advertId ──
p_cmp AS (
  SELECT r.run_day, c.advertId, ANY_VALUE(c.processed_status) AS ps, ANY_VALUE(c.type) AS type, ANY_VALUE(c.status) AS status,
         ANY_VALUE(c.payment_type) AS payment_type, ANY_VALUE(c.campName) AS camp_name, ANY_VALUE(c.nm_ids) AS nm_ids,
         ANY_VALUE(c.change_time) AS change_time, ANY_VALUE(c.end_time) AS end_time
  FROM `wb_raw.RAW_WB_ADV_CAMPAIGNS` c JOIN as_runs r USING (run_id)
  WHERE c._PARTITIONTIME >= (SELECT p0 FROM win) GROUP BY 1, 2
),
c_cmp AS (
  SELECT r.run_day, c.advertId, ANY_VALUE(c.processed_status) AS ps, ANY_VALUE(c.type) AS type, ANY_VALUE(c.status) AS status,
         ANY_VALUE(c.payment_type) AS payment_type, ANY_VALUE(c.campName) AS camp_name, ANY_VALUE(c.nm_ids) AS nm_ids,
         ANY_VALUE(c.change_time) AS change_time, ANY_VALUE(c.end_time) AS end_time
  FROM `wb_raw.RAW_WB_ADV_CAMPAIGNS__CR` c JOIN cr_runs r USING (run_id)
  WHERE c._PARTITIONTIME >= (SELECT p0 FROM win) GROUP BY 1, 2
),
cmp AS (
  SELECT run_day,
    COUNTIF(p.advertId IS NOT NULL AND c.advertId IS NOT NULL) AS cmp_matched,
    COUNTIF(p.advertId IS NULL OR c.advertId IS NULL) AS cmp_one_sided,
    COUNTIF(p.advertId IS NOT NULL AND c.advertId IS NOT NULL AND (
      p.ps IS DISTINCT FROM c.ps OR p.type IS DISTINCT FROM c.type OR p.status IS DISTINCT FROM c.status
      OR p.payment_type IS DISTINCT FROM c.payment_type OR p.camp_name IS DISTINCT FROM c.camp_name
      OR p.nm_ids IS DISTINCT FROM c.nm_ids OR p.change_time IS DISTINCT FROM c.change_time
      OR p.end_time IS DISTINCT FROM c.end_time)) AS cmp_attr_diff
  FROM p_cmp p FULL OUTER JOIN c_cmp c USING (run_day, advertId)
  GROUP BY run_day
),
-- ── costs (биллинг): ключ записи (advertId, updTime) — как V_ADV_COSTS_SNAPSHOT; updSum — значение ──
p_cost AS (
  SELECT r.run_day, c.advertId, c.updTime, c.updSum, c.updNum, c.advertStatus, c.campName, c.window_index, c.period_from, c.period_to
  FROM `wb_raw.RAW_WB_ADV_COSTS` c JOIN as_runs r USING (run_id)
  WHERE c._PARTITIONTIME >= (SELECT p0 FROM win)
),
c_cost AS (
  SELECT r.run_day, c.advertId, c.updTime, c.updSum, c.updNum, c.advertStatus, c.campName, c.window_index, c.period_from, c.period_to
  FROM `wb_raw.RAW_WB_ADV_COSTS__CR` c JOIN cr_runs r USING (run_id)
  WHERE c._PARTITIONTIME >= (SELECT p0 FROM win)
),
cost AS (
  SELECT run_day,
    COUNTIF(p.advertId IS NOT NULL AND c.advertId IS NOT NULL) AS cost_matched,
    COUNTIF(p.advertId IS NULL OR c.advertId IS NULL) AS cost_one_sided,
    COUNTIF(p.advertId IS NOT NULL AND c.advertId IS NOT NULL AND (
      p.updSum IS DISTINCT FROM c.updSum OR p.window_index IS DISTINCT FROM c.window_index
      OR p.period_from IS DISTINCT FROM c.period_from OR p.period_to IS DISTINCT FROM c.period_to)) AS cost_struct_diff,
    COUNTIF(p.advertId IS NOT NULL AND c.advertId IS NOT NULL AND (
      p.updNum IS DISTINCT FROM c.updNum OR p.advertStatus IS DISTINCT FROM c.advertStatus
      OR p.campName IS DISTINCT FROM c.campName)) AS cost_attr_diff,
    ROUND(SUM(SAFE_CAST(p.updSum AS FLOAT64)), 2) AS cost_sum_prod,
    ROUND(SUM(SAFE_CAST(c.updSum AS FLOAT64)), 2) AS cost_sum_cr
  FROM p_cost p FULL OUTER JOIN c_cost c USING (run_day, advertId, updTime)
  GROUP BY run_day
),
-- маркеры окон: состав и исход каждого окна обязаны совпасть
p_mark AS (
  SELECT r.run_day, m.window_index, m.period_from, m.period_to, m.status, m.http_success, m.returned_rows,
         m.returned_days, m.rows_out_of_window, m.requested_days
  FROM `wb_raw.RAW_WB_ADV_COSTS_RUNS` m JOIN as_runs r USING (run_id)
),
c_mark AS (
  SELECT r.run_day, m.window_index, m.period_from, m.period_to, m.status, m.http_success, m.returned_rows,
         m.returned_days, m.rows_out_of_window, m.requested_days
  FROM `wb_raw.RAW_WB_ADV_COSTS_RUNS__CR` m JOIN cr_runs r USING (run_id)
),
mark AS (
  SELECT run_day,
    COUNTIF(p.window_index IS NULL OR c.window_index IS NULL OR p.period_from IS DISTINCT FROM c.period_from
      OR p.period_to IS DISTINCT FROM c.period_to OR p.status IS DISTINCT FROM c.status
      OR p.http_success IS DISTINCT FROM c.http_success OR p.returned_rows IS DISTINCT FROM c.returned_rows
      OR p.returned_days IS DISTINCT FROM c.returned_days OR p.rows_out_of_window IS DISTINCT FROM c.rows_out_of_window
      OR p.requested_days IS DISTINCT FROM c.requested_days) AS marker_diff
  FROM p_mark p FULL OUTER JOIN c_mark c USING (run_day, window_index)
  GROUP BY run_day
),
-- ── fullstats (атрибуция): ключ = ключ дедупа V_ADV_CAMPAIGN_STATS без source_level (всегда 'nm') ──
p_fs AS (
  SELECT r.run_day, s.`date`, s.advertId, s.nmId, s.appType, COUNT(*) AS n,
         ANY_VALUE(s.views) AS views, ANY_VALUE(s.clicks) AS clicks, ANY_VALUE(s.`sum`) AS spend, ANY_VALUE(s.orders) AS orders,
         ANY_VALUE(s.atbs) AS atbs, ANY_VALUE(s.canceled) AS canceled, ANY_VALUE(s.shks) AS shks, ANY_VALUE(s.sum_price) AS sum_price
  FROM `wb_raw.RAW_WB_ADV_CAMPAIGN_STATS` s JOIN as_runs r USING (run_id)
  WHERE s._PARTITIONTIME >= (SELECT p0 FROM win) AND s.processed_status = 'raw' GROUP BY 1, 2, 3, 4, 5
),
c_fs AS (
  SELECT r.run_day, s.`date`, s.advertId, s.nmId, s.appType, COUNT(*) AS n,
         ANY_VALUE(s.views) AS views, ANY_VALUE(s.clicks) AS clicks, ANY_VALUE(s.`sum`) AS spend, ANY_VALUE(s.orders) AS orders,
         ANY_VALUE(s.atbs) AS atbs, ANY_VALUE(s.canceled) AS canceled, ANY_VALUE(s.shks) AS shks, ANY_VALUE(s.sum_price) AS sum_price
  FROM `wb_raw.RAW_WB_ADV_CAMPAIGN_STATS__CR` s JOIN cr_runs r USING (run_id)
  WHERE s._PARTITIONTIME >= (SELECT p0 FROM win) AND s.processed_status = 'raw' GROUP BY 1, 2, 3, 4, 5
),
fs AS (
  SELECT run_day,
    COUNTIF(p.advertId IS NOT NULL AND c.advertId IS NOT NULL) AS fs_matched,
    COUNTIF(p.advertId IS NULL OR c.advertId IS NULL) AS fs_one_sided,
    COUNTIF(p.advertId IS NOT NULL AND c.advertId IS NOT NULL AND (
      p.n IS DISTINCT FROM c.n OR p.views IS DISTINCT FROM c.views OR p.clicks IS DISTINCT FROM c.clicks
      OR p.spend IS DISTINCT FROM c.spend OR p.orders IS DISTINCT FROM c.orders OR p.atbs IS DISTINCT FROM c.atbs
      OR p.canceled IS DISTINCT FROM c.canceled OR p.shks IS DISTINCT FROM c.shks
      OR p.sum_price IS DISTINCT FROM c.sum_price)) AS fs_metric_diff,
    ROUND(SUM(SAFE_CAST(p.spend AS FLOAT64)), 2) AS fs_spend_prod,
    ROUND(SUM(SAFE_CAST(c.spend AS FLOAT64)), 2) AS fs_spend_cr
  FROM p_fs p FULL OUTER JOIN c_fs c USING (run_day, `date`, advertId, nmId, appType)
  GROUP BY run_day
),
p_bs AS (
  SELECT r.run_day, b.`date`, b.advertId, b.nmId, ANY_VALUE(b.avg_position) AS pos
  FROM `wb_raw.RAW_WB_ADV_BOOSTER_STATS` b JOIN as_runs r USING (run_id)
  WHERE b._PARTITIONTIME >= (SELECT p0 FROM win) GROUP BY 1, 2, 3, 4
),
c_bs AS (
  SELECT r.run_day, b.`date`, b.advertId, b.nmId, ANY_VALUE(b.avg_position) AS pos
  FROM `wb_raw.RAW_WB_ADV_BOOSTER_STATS__CR` b JOIN cr_runs r USING (run_id)
  WHERE b._PARTITIONTIME >= (SELECT p0 FROM win) GROUP BY 1, 2, 3, 4
),
bs AS (
  SELECT run_day, COUNTIF(p.advertId IS NULL OR c.advertId IS NULL OR p.pos IS DISTINCT FROM c.pos) AS booster_diff
  FROM p_bs p FULL OUTER JOIN c_bs c USING (run_day, `date`, advertId, nmId)
  GROUP BY run_day
),
-- ── Ads-3 / Ads-2 (диагностика, вне вердикта дня) ──
-- В Apps Script хвост пишется тем же run_id, что и прогон дня; в Cloud Run — отдельным загрузчиком
-- `ads-query` со своим run_id (`ADSRAW_…_CRQ…`). Поэтому пара — по суткам прогона: последний ран хвоста дня.
p_qb AS (SELECT PARSE_DATE('%Y%m%d', SUBSTR(b.run_id, 8, 8)) AS run_day, b.status, b.requested_pairs, b.returned_bid_rows
         FROM `wb_raw.RAW_WB_ADV_QUERY_BIDS_RUNS` b
         WHERE b._PARTITIONTIME >= (SELECT p0 FROM win) AND STARTS_WITH(b.run_id, 'ADSRAW_')
           AND NOT REGEXP_CONTAINS(b.run_id, r'_CR[0-9A-Za-z]+$')
         QUALIFY ROW_NUMBER() OVER (PARTITION BY PARSE_DATE('%Y%m%d', SUBSTR(b.run_id, 8, 8)) ORDER BY b.run_id DESC) = 1),
c_qb AS (SELECT PARSE_DATE('%Y%m%d', SUBSTR(b.run_id, 8, 8)) AS run_day, b.status, b.requested_pairs, b.returned_bid_rows
         FROM `wb_raw.RAW_WB_ADV_QUERY_BIDS_RUNS__CR` b
         WHERE b._PARTITIONTIME >= (SELECT p0 FROM win) AND STARTS_WITH(b.run_id, 'ADSRAW_')
         QUALIFY ROW_NUMBER() OVER (PARTITION BY PARSE_DATE('%Y%m%d', SUBSTR(b.run_id, 8, 8)) ORDER BY b.run_id DESC) = 1),
p_qs_run AS (SELECT DISTINCT run_id, PARSE_DATE('%Y%m%d', SUBSTR(run_id, 8, 8)) AS run_day FROM `wb_raw.RAW_WB_ADV_QUERY_STATS_RUNS`
             WHERE _PARTITIONTIME >= (SELECT p0 FROM win) AND STARTS_WITH(run_id, 'ADSRAW_') AND NOT REGEXP_CONTAINS(run_id, r'_CR[0-9A-Za-z]+$')
             QUALIFY ROW_NUMBER() OVER (PARTITION BY PARSE_DATE('%Y%m%d', SUBSTR(run_id, 8, 8)) ORDER BY run_id DESC) = 1),
c_qs_run AS (SELECT DISTINCT run_id, PARSE_DATE('%Y%m%d', SUBSTR(run_id, 8, 8)) AS run_day FROM `wb_raw.RAW_WB_ADV_QUERY_STATS_RUNS__CR`
             WHERE _PARTITIONTIME >= (SELECT p0 FROM win) AND STARTS_WITH(run_id, 'ADSRAW_')
             QUALIFY ROW_NUMBER() OVER (PARTITION BY PARSE_DATE('%Y%m%d', SUBSTR(run_id, 8, 8)) ORDER BY run_id DESC) = 1),
p_qs AS (SELECT r.run_day, q.period_from, q.status, q.requested_pairs, q.returned_rows FROM `wb_raw.RAW_WB_ADV_QUERY_STATS_RUNS` q JOIN p_qs_run r USING (run_id)
         WHERE q._PARTITIONTIME >= (SELECT p0 FROM win)),
c_qs AS (SELECT r.run_day, q.period_from, q.status, q.requested_pairs, q.returned_rows FROM `wb_raw.RAW_WB_ADV_QUERY_STATS_RUNS__CR` q JOIN c_qs_run r USING (run_id)
         WHERE q._PARTITIONTIME >= (SELECT p0 FROM win)),
qs AS (
  SELECT run_day,
    COUNTIF(p.status = 'OK' AND c.status = 'OK') AS qs_both_ok,
    COUNTIF(p.period_from IS NULL OR c.period_from IS NULL) AS qs_one_sided_days,
    COUNTIF(p.status IS DISTINCT FROM c.status) AS qs_status_diff,
    COUNTIF(p.requested_pairs IS DISTINCT FROM c.requested_pairs OR p.returned_rows IS DISTINCT FROM c.returned_rows) AS qs_count_diff
  FROM p_qs p FULL OUTER JOIN c_qs c USING (run_day, period_from)
  GROUP BY run_day
),
days AS (SELECT run_day FROM as_runs UNION DISTINCT SELECT run_day FROM cr_runs)
SELECT
  d.run_day,
  ar.run_id AS prod_run_id, cr.run_id AS cr_run_id,
  aj.status AS prod_journal, aj.error_code AS prod_error, cj.status AS cr_journal, cj.error_code AS cr_error,
  cmp.cmp_matched, cmp.cmp_one_sided, cmp.cmp_attr_diff,
  cost.cost_matched, cost.cost_one_sided, cost.cost_struct_diff, cost.cost_attr_diff, cost.cost_sum_prod, cost.cost_sum_cr,
  mark.marker_diff,
  fs.fs_matched, fs.fs_one_sided, fs.fs_metric_diff, fs.fs_spend_prod, fs.fs_spend_cr, bs.booster_diff,
  pqb.status AS prod_bids_status, cqb.status AS cr_bids_status, pqb.returned_bid_rows AS prod_bid_rows, cqb.returned_bid_rows AS cr_bid_rows,
  qs.qs_both_ok, qs.qs_one_sided_days, qs.qs_status_diff, qs.qs_count_diff,
  CASE
    WHEN IFNULL(cost.cost_one_sided, 0) + IFNULL(cost.cost_struct_diff, 0) + IFNULL(mark.marker_diff, 0) > 0 THEN 'FAIL'
    WHEN IFNULL(cost.cost_attr_diff, 0) > 0 THEN 'DATA_DRIFT'
    ELSE 'EXACT'
  END AS costs_verdict,
  CASE
    WHEN ar.run_id IS NULL OR cr.run_id IS NULL THEN 'MISSING'
    WHEN IFNULL(cmp.cmp_one_sided, 0) > 0 THEN 'FAIL'
    WHEN IFNULL(cost.cost_one_sided, 0) + IFNULL(cost.cost_struct_diff, 0) + IFNULL(mark.marker_diff, 0) > 0 THEN 'FAIL'
    -- неполный fullstats одной стороны (ADS_PARTIAL/ADS_ERROR) — сбой источника, а не алгоритма
    WHEN IFNULL(aj.status, '') != 'COMPLETE' OR IFNULL(cj.status, '') != 'COMPLETE' THEN 'SOURCE_GAP'
    WHEN IFNULL(fs.fs_one_sided, 0) > 0 THEN 'FAIL'
    WHEN IFNULL(cmp.cmp_attr_diff, 0) + IFNULL(cost.cost_attr_diff, 0) + IFNULL(fs.fs_metric_diff, 0) + IFNULL(bs.booster_diff, 0) > 0 THEN 'DATA_DRIFT'
    ELSE 'EXACT'
  END AS verdict,
  CASE
    WHEN pqb.status IS DISTINCT FROM cqb.status OR IFNULL(qs.qs_status_diff, 0) + IFNULL(qs.qs_one_sided_days, 0) > 0 THEN 'REVIEW'
    WHEN pqb.returned_bid_rows IS DISTINCT FROM cqb.returned_bid_rows OR IFNULL(qs.qs_count_diff, 0) > 0 THEN 'DATA_DRIFT'
    ELSE 'EXACT'
  END AS aux_verdict
FROM days d
LEFT JOIN as_runs ar USING (run_day)
LEFT JOIN cr_runs cr USING (run_day)
LEFT JOIN as_journal aj USING (run_day)
LEFT JOIN cr_journal cj USING (run_day)
LEFT JOIN cmp USING (run_day)
LEFT JOIN cost USING (run_day)
LEFT JOIN mark USING (run_day)
LEFT JOIN fs USING (run_day)
LEFT JOIN bs USING (run_day)
LEFT JOIN p_qb pqb USING (run_day)
LEFT JOIN c_qb cqb USING (run_day)
LEFT JOIN qs USING (run_day)
ORDER BY d.run_day DESC;

-- ─────────────────────────────────────────────────────────────────────────
-- QUERY 2 — СТРОКИ РАСХОЖДЕНИЙ РАСХОДОВ ЗА ПРОГОН ДНЯ (разбор FAIL по costs)
-- Подставить @run_day = 'YYYY-MM-DD' (дата прогона МСК, не D−1).
-- ─────────────────────────────────────────────────────────────────────────
WITH as_run AS (
  SELECT DISTINCT run_id FROM `wb_raw.RAW_WB_ADV_COSTS_RUNS`
  WHERE STARTS_WITH(run_id, CONCAT('ADSRAW_', FORMAT_DATE('%Y%m%d', DATE(@run_day))))
    AND NOT REGEXP_CONTAINS(run_id, r'_CR[0-9A-Za-z]+$')
  QUALIFY ROW_NUMBER() OVER (ORDER BY run_id DESC) = 1
),
cr_run AS (
  SELECT DISTINCT run_id FROM `wb_raw.RAW_WB_ADV_COSTS_RUNS__CR`
  WHERE STARTS_WITH(run_id, CONCAT('ADSRAW_', FORMAT_DATE('%Y%m%d', DATE(@run_day))))
  QUALIFY ROW_NUMBER() OVER (ORDER BY run_id DESC) = 1
),
p AS (SELECT * FROM `wb_raw.RAW_WB_ADV_COSTS` WHERE run_id = (SELECT run_id FROM as_run)
        AND _PARTITIONTIME >= TIMESTAMP(DATE_SUB(DATE(@run_day), INTERVAL 1 DAY))),
c AS (SELECT * FROM `wb_raw.RAW_WB_ADV_COSTS__CR` WHERE run_id = (SELECT run_id FROM cr_run)
        AND _PARTITIONTIME >= TIMESTAMP(DATE_SUB(DATE(@run_day), INTERVAL 1 DAY)))
SELECT advertId, updTime, p.updDate AS prod_date, c.updDate AS cr_date, p.updSum AS prod_sum, c.updSum AS cr_sum,
       p.updNum AS prod_num, c.updNum AS cr_num, p.advertStatus AS prod_status, c.advertStatus AS cr_status
FROM p FULL OUTER JOIN c USING (advertId, updTime)
WHERE p.advertId IS NULL OR c.advertId IS NULL
   OR p.updSum IS DISTINCT FROM c.updSum OR p.updNum IS DISTINCT FROM c.updNum
   OR p.advertStatus IS DISTINCT FROM c.advertStatus OR p.window_index IS DISTINCT FROM c.window_index
ORDER BY updTime, advertId;

-- ─────────────────────────────────────────────────────────────────────────
-- QUERY 3 — РАСХОЖДЕНИЯ FULLSTATS ЗА ПРОГОН ДНЯ (разбор FAIL/DATA_DRIFT атрибуции)
-- Подставить @run_day. Показывает, на какие сутки окна приходится дрейф (обычно D−1).
-- ─────────────────────────────────────────────────────────────────────────
WITH as_run AS (
  SELECT DISTINCT run_id FROM `wb_raw.RAW_WB_ADV_COSTS_RUNS`
  WHERE STARTS_WITH(run_id, CONCAT('ADSRAW_', FORMAT_DATE('%Y%m%d', DATE(@run_day))))
    AND NOT REGEXP_CONTAINS(run_id, r'_CR[0-9A-Za-z]+$')
  QUALIFY ROW_NUMBER() OVER (ORDER BY run_id DESC) = 1
),
cr_run AS (
  SELECT DISTINCT run_id FROM `wb_raw.RAW_WB_ADV_COSTS_RUNS__CR`
  WHERE STARTS_WITH(run_id, CONCAT('ADSRAW_', FORMAT_DATE('%Y%m%d', DATE(@run_day))))
  QUALIFY ROW_NUMBER() OVER (ORDER BY run_id DESC) = 1
),
p AS (SELECT `date`, advertId, nmId, appType, COUNT(*) AS n, ANY_VALUE(views) AS views, ANY_VALUE(clicks) AS clicks, ANY_VALUE(`sum`) AS spend, ANY_VALUE(orders) AS orders
      FROM `wb_raw.RAW_WB_ADV_CAMPAIGN_STATS` WHERE run_id = (SELECT run_id FROM as_run) AND processed_status = 'raw'
        AND _PARTITIONTIME >= TIMESTAMP(DATE_SUB(DATE(@run_day), INTERVAL 1 DAY)) GROUP BY 1, 2, 3, 4),
c AS (SELECT `date`, advertId, nmId, appType, COUNT(*) AS n, ANY_VALUE(views) AS views, ANY_VALUE(clicks) AS clicks, ANY_VALUE(`sum`) AS spend, ANY_VALUE(orders) AS orders
      FROM `wb_raw.RAW_WB_ADV_CAMPAIGN_STATS__CR` WHERE run_id = (SELECT run_id FROM cr_run) AND processed_status = 'raw'
        AND _PARTITIONTIME >= TIMESTAMP(DATE_SUB(DATE(@run_day), INTERVAL 1 DAY)) GROUP BY 1, 2, 3, 4)
SELECT SUBSTR(`date`, 1, 10) AS stat_day,
  COUNTIF(p.advertId IS NULL) AS cr_only, COUNTIF(c.advertId IS NULL) AS prod_only,
  COUNTIF(p.advertId IS NOT NULL AND c.advertId IS NOT NULL AND (p.n IS DISTINCT FROM c.n OR p.views IS DISTINCT FROM c.views
    OR p.clicks IS DISTINCT FROM c.clicks OR p.spend IS DISTINCT FROM c.spend OR p.orders IS DISTINCT FROM c.orders)) AS metric_diff,
  ROUND(SUM(SAFE_CAST(p.spend AS FLOAT64)), 2) AS spend_prod, ROUND(SUM(SAFE_CAST(c.spend AS FLOAT64)), 2) AS spend_cr
FROM p FULL OUTER JOIN c USING (`date`, advertId, nmId, appType)
GROUP BY stat_day
ORDER BY stat_day DESC;
