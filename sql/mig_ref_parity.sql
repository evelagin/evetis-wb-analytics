-- ============================================================================
-- M5 — сверка паритета REF Sync: Apps Script (REF_SKU_MASTER_DATA + REF_SYNC_RUNS) ↔
-- Cloud Run (REF_*__CR). ТОЛЬКО ЧТЕНИЕ. Регион EU, датасет wb_raw. 4 таблицы (~40 МБ).
--
-- Версия дня на каждой стороне — последний COMPLETE-прогон суток МСК, который сам себя
-- активировал (active_version_after = run_id). Сравниваются строки версии по nm_id, все
-- колонки кроме ref_run_id и _synced_at (это и есть содержимое вью REF_SKU_MASTER).
--
-- Вердикт дня:
--   EXACT       — наборы строк совпали полностью;
--   DATA_DRIFT  — не совпали, но версия Cloud Run (08:32) совпала с версией Apps Script
--                 СЛЕДУЮЩЕГО дня: лист правили между 08:22 и 08:32, Cloud Run увидел правку раньше;
--   FAIL        — иное расхождение, либо у Cloud Run за день есть ERROR и нет COMPLETE;
--   NO_RUN      — у одной из сторон нет COMPLETE за день: день не засчитывается.
-- Приёмка: 5 дней подряд без FAIL и NO_RUN, из них ≥ 3 EXACT (runbook §4).
-- Глубина ограничена retention: обе стороны хранят 10 последних версий.
-- ============================================================================
WITH params AS (SELECT 7 AS lookback_days),
days AS (
  SELECT d FROM UNNEST(GENERATE_DATE_ARRAY(
    DATE_SUB(CURRENT_DATE('Europe/Moscow'), INTERVAL (SELECT lookback_days FROM params) DAY),
    CURRENT_DATE('Europe/Moscow'))) AS d
),
p_ver AS (
  SELECT DATE(started_at, 'Europe/Moscow') AS d, run_id
  FROM `wb_raw.REF_SYNC_RUNS`
  WHERE status = 'COMPLETE' AND active_version_after = run_id
    AND DATE(started_at, 'Europe/Moscow') >= DATE_SUB(CURRENT_DATE('Europe/Moscow'), INTERVAL (SELECT lookback_days + 1 FROM params) DAY)
  QUALIFY ROW_NUMBER() OVER (PARTITION BY DATE(started_at, 'Europe/Moscow') ORDER BY started_at DESC) = 1
),
c_ver AS (
  SELECT DATE(started_at, 'Europe/Moscow') AS d, run_id
  FROM `wb_raw.REF_SYNC_RUNS__CR`
  WHERE status = 'COMPLETE' AND active_version_after = run_id
    AND DATE(started_at, 'Europe/Moscow') >= DATE_SUB(CURRENT_DATE('Europe/Moscow'), INTERVAL (SELECT lookback_days FROM params) DAY)
  QUALIFY ROW_NUMBER() OVER (PARTITION BY DATE(started_at, 'Europe/Moscow') ORDER BY started_at DESC) = 1
),
c_err AS (
  SELECT DATE(started_at, 'Europe/Moscow') AS d, COUNTIF(status = 'ERROR') AS errors
  FROM `wb_raw.REF_SYNC_RUNS__CR` GROUP BY 1
),
-- Отпечаток строки = всё содержимое вью REF_SKU_MASTER.
p_rows AS (
  SELECT v.d, TO_JSON_STRING((SELECT AS STRUCT r.* EXCEPT (ref_run_id, _synced_at))) AS fp
  FROM p_ver v JOIN `wb_raw.REF_SKU_MASTER_DATA` r ON r.ref_run_id = v.run_id
),
c_rows AS (
  SELECT v.d, TO_JSON_STRING((SELECT AS STRUCT r.* EXCEPT (ref_run_id, _synced_at))) AS fp
  FROM c_ver v JOIN `wb_raw.REF_SKU_MASTER_DATA__CR` r ON r.ref_run_id = v.run_id
),
p_set AS (SELECT d, STRING_AGG(fp, '\n' ORDER BY fp) AS fps, COUNT(*) AS n FROM p_rows GROUP BY d),
c_set AS (SELECT d, STRING_AGG(fp, '\n' ORDER BY fp) AS fps, COUNT(*) AS n FROM c_rows GROUP BY d),
diff AS (
  SELECT d,
    COUNTIF(c.fp IS NULL) AS prod_only_rows,
    COUNTIF(p.fp IS NULL) AS cr_only_rows
  FROM p_rows p FULL OUTER JOIN c_rows c USING (d, fp)
  GROUP BY d
)
SELECT dd.d,
  pv.run_id AS prod_version, cv.run_id AS cr_version,
  ps.n AS prod_rows, cs.n AS cr_rows,
  IFNULL(df.prod_only_rows, 0) AS prod_only_rows, IFNULL(df.cr_only_rows, 0) AS cr_only_rows,
  IFNULL(ce.errors, 0) AS cr_errors,
  CASE
    WHEN cv.run_id IS NULL AND IFNULL(ce.errors, 0) > 0 THEN 'FAIL'
    WHEN pv.run_id IS NULL OR cv.run_id IS NULL THEN 'NO_RUN'
    WHEN ps.fps = cs.fps THEN 'EXACT'
    WHEN cs.fps = ps_next.fps THEN 'DATA_DRIFT'
    ELSE 'FAIL'
  END AS verdict
FROM days dd
LEFT JOIN c_ver cv ON cv.d = dd.d
LEFT JOIN p_ver pv ON pv.d = dd.d
LEFT JOIN p_set ps ON ps.d = dd.d
LEFT JOIN c_set cs ON cs.d = dd.d
LEFT JOIN p_set ps_next ON ps_next.d = DATE_ADD(dd.d, INTERVAL 1 DAY)
LEFT JOIN diff df ON df.d = dd.d
LEFT JOIN c_err ce ON ce.d = dd.d
ORDER BY dd.d;
