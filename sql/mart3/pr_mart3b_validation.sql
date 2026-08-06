-- ============================================================================
-- PR-Mart3b-1 — validation-запросы (read-only). Прогонять после ручного controlled-run
-- (`npm run mart:manual -- --target-date=<D-1>`) и в наблюдении.
-- Дизайн: docs/MART_PR3B_PLAN_2026-08-03.md (REV3). PR-нота: docs/MART_PR3B1_LOADER_2026-08-03.md.
-- Каждая секция возвращает НОЛЬ строк / ожидаемое значение = «зелено».
-- ============================================================================

-- ── §1. РОВНО одна строка на run_id (контракт записи MART_RUNS) ──────────────
-- Ожидание: ноль строк.
SELECT run_id, COUNT(*) AS rows_per_run
FROM `wb_mart.MART_RUNS`
GROUP BY run_id
HAVING COUNT(*) > 1;

-- ── §2. Домен статусов терминален ───────────────────────────────────────────
-- Ожидание: ноль строк.
SELECT run_id, status
FROM `wb_mart.MART_RUNS`
WHERE status NOT IN ('COMPLETE', 'ERROR');

-- ── §3. ERROR не маскирует причину: код и текст присутствуют ─────────────────
-- Ожидание: ноль строк.
SELECT run_id, status, error_code, error_message
FROM `wb_mart.MART_RUNS`
WHERE status = 'ERROR' AND (error_code IS NULL OR error_message IS NULL);

-- ── §3b. У COMPLETE обязателен completed_at (иначе ORDER BY completed_at ниже некорректен) ─
-- Ожидание: ноль строк.
SELECT run_id, target_date
FROM `wb_mart.MART_RUNS`
WHERE status = 'COMPLETE' AND completed_at IS NULL;

-- ── §4. Штатный прогон: последний COMPLETE закрывает именно D-1 (МСК) ────────
-- Ожидание: covers_d1_ok = TRUE (target_date последнего COMPLETE == сегодня(МСК)-1).
-- Примечание: при бэкфилле/ручном прогоне за иную дату эта проверка носит информационный характер.
WITH last_ok AS (
  SELECT target_date
  FROM `wb_mart.MART_RUNS`
  WHERE status = 'COMPLETE' AND environment = 'prod'
  ORDER BY completed_at DESC, run_id DESC
  LIMIT 1
)
SELECT
  (SELECT target_date FROM last_ok) AS last_complete_target_date,
  DATE_SUB(CURRENT_DATE('Europe/Moscow'), INTERVAL 1 DAY) AS expected_d1,
  (SELECT target_date FROM last_ok) = DATE_SUB(CURRENT_DATE('Europe/Moscow'), INTERVAL 1 DAY) AS covers_d1_ok;

-- ── §5. ads_* в витрине — единый build-level снимок (NULL-safe) ──────────────
-- Ожидание: distinct_ads_amd <= 1 И distinct_ads_activity_lagged <= 1.
-- NULL-safe: COUNT(DISTINCT col) игнорирует NULL и не заметил бы смесь NULL+дата.
SELECT
  COUNT(DISTINCT TO_JSON_STRING(STRUCT(ads_activity_max_date AS value))) AS distinct_ads_amd,
  COUNT(DISTINCT TO_JSON_STRING(STRUCT(ads_activity_lagged AS value)))   AS distinct_ads_activity_lagged
FROM `wb_mart.MART_SKU_DAILY`;

-- ── §6. Согласованность ads-диагностики витрины и журнала для последнего COMPLETE ─
-- Ожидание: match_amd = TRUE И match_lagged = TRUE.
WITH last_ok AS (
  SELECT run_id, ads_activity_max_date, ads_activity_lagged
  FROM `wb_mart.MART_RUNS`
  WHERE status = 'COMPLETE' AND environment = 'prod'
  ORDER BY completed_at DESC, run_id DESC
  LIMIT 1
),
mart AS (
  SELECT ANY_VALUE(ads_activity_max_date) AS ads_amd, ANY_VALUE(ads_activity_lagged) AS ads_al
  FROM `wb_mart.MART_SKU_DAILY`
)
SELECT
  (SELECT ads_activity_max_date FROM last_ok) IS NOT DISTINCT FROM (SELECT ads_amd FROM mart) AS match_amd,
  (SELECT ads_activity_lagged FROM last_ok)   IS NOT DISTINCT FROM (SELECT ads_al FROM mart)  AS match_lagged;

-- ── §7. mart_rows журнала совпадает с фактическим числом строк витрины ───────
-- Ожидание: match_rows = TRUE (для последнего COMPLETE).
WITH last_ok AS (
  SELECT mart_rows FROM `wb_mart.MART_RUNS`
  WHERE status = 'COMPLETE' AND environment = 'prod'
  ORDER BY completed_at DESC, run_id DESC LIMIT 1
)
SELECT
  (SELECT mart_rows FROM last_ok)                          AS logged_rows,
  (SELECT COUNT(*) FROM `wb_mart.MART_SKU_DAILY`)          AS actual_rows,
  (SELECT mart_rows FROM last_ok) = (SELECT COUNT(*) FROM `wb_mart.MART_SKU_DAILY`) AS match_rows;

-- ── §8. Опубликованная витрина ПРИНАДЛЕЖИТ последнему COMPLETE-журналу (обяз. синк) ─
-- Ожидание: wrong_run = 0 И wrong_date = 0.
WITH last_ok AS (
  SELECT run_id, target_date
  FROM `wb_mart.MART_RUNS`
  WHERE status = 'COMPLETE' AND environment = 'prod'
  ORDER BY completed_at DESC, run_id DESC
  LIMIT 1
)
SELECT
  COUNTIF(mart_run_id      IS DISTINCT FROM (SELECT run_id      FROM last_ok)) AS wrong_run,
  COUNTIF(build_as_of_date IS DISTINCT FROM (SELECT target_date FROM last_ok)) AS wrong_date
FROM `wb_mart.MART_SKU_DAILY`;
