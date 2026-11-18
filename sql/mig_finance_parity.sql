-- ============================================================================
-- M4 · Финансовые отчёты WB — ТОЧНАЯ сверка shadow (Cloud Run, `__CR`) ↔ prod (Apps Script).
-- Только чтение (SELECT). Регион EU, dataset wb_raw. Runbook: docs/ops/MIG_FINANCE_RUNBOOK.md §4.
--
-- Почему сверка по отчётам, а не по суткам загрузки. Единица финансового контура — отчёт WB (report_id):
-- он неизменяем после публикации, и оба контура читают ОДИН И ТОТ ЖЕ отчёт с разницей ~35 мин
-- (shadow стартует после Apps Script). Поэтому у отчёта, загруженного обоими, расхождений быть не должно
-- вообще: ни в манифесте, ни в строках RAW. Сутки = date_from отчёта (бизнес-дата МСК).
--
-- Валидные строки RAW — только строки прогона-владельца (r.run_id = m.processing_run_id, m.status='COMPLETE'),
-- как в V_WB_FINANCE_COMPLETE. Из сравнения строк исключены load_id, run_id, loaded_at (у контуров разные
-- по построению); row_hash и raw_json СРАВНИВАЮТСЯ.
--
-- Вердикт суток:
--   EXACT       — каждый отчёт суток есть COMPLETE в обоих манифестах; манифест и все строки RAW совпали;
--   DATA_DRIFT  — у отчётов, загруженных обоими, всё совпало, но часть отчётов есть только на одной стороне
--                 (WB опубликовал отчёт между слотами, хвост очереди PARTIAL, shadow начал позже prod);
--                 такие отчёты перечислены в QUERY 2 и обязаны сойтись в следующие сутки;
--   FAIL        — отчёт есть в обоих, но разошлись метрики манифеста (list_*/det_*/rows_*), период, тип,
--                 слой, или хотя бы одна строка RAW (хеш строки / односторонний rrd_id); либо дубль rrd_id;
--   NO_DATA     — отчётов суток нет ни у одного контура.
-- Окно — от первой недели календаря shadow (FINANCE_WEEK_STATUS__CR) и не глубже lookback_days (14; при смене
-- поменять и константу фильтра партиций в p_rows/c_rows — она обязана быть литералом для отсечения партиций).
-- Приёмка: 5 последовательных суток без FAIL, из них не меньше 3 EXACT, и QUERY 3 без FAIL.
-- ============================================================================

-- ─────────────────────────────────────────────────────────────────────────
-- QUERY 1 — ВЕРДИКТ ПО СУТКАМ (date_from отчёта)
-- ─────────────────────────────────────────────────────────────────────────
WITH params AS (SELECT 14 AS lookback_days),
win AS (
  SELECT GREATEST(
           DATE_SUB(CURRENT_DATE('Europe/Moscow'), INTERVAL (SELECT lookback_days FROM params) DAY),
           IFNULL((SELECT MIN(week_start) FROM `wb_raw.FINANCE_WEEK_STATUS__CR`), DATE '2999-01-01')) AS d_from
),
pm AS (
  SELECT * FROM `wb_raw.FINANCE_REPORT_LOADS`
  WHERE status = 'COMPLETE' AND date_from >= (SELECT d_from FROM win)
),
cm AS (
  SELECT * FROM `wb_raw.FINANCE_REPORT_LOADS__CR`
  WHERE status = 'COMPLETE' AND date_from >= (SELECT d_from FROM win)
),
-- строки RAW прогона-владельца, без служебных полей прогона
p_rows AS (
  SELECT r.* EXCEPT (load_id, run_id, loaded_at)
  FROM `wb_raw.RAW_WB_FINANCE` r
  JOIN pm m ON r.report_id = m.report_id AND r.run_id = m.processing_run_id
  -- константа (не подзапрос): иначе BigQuery не отсекает партиции и читает всю RAW (~0,37 ГБ)
  WHERE r._rr_date >= DATE_SUB(CURRENT_DATE('Europe/Moscow'), INTERVAL 14 DAY)
),
c_rows AS (
  SELECT r.* EXCEPT (load_id, run_id, loaded_at)
  FROM `wb_raw.RAW_WB_FINANCE__CR` r
  JOIN cm m ON r.report_id = m.report_id AND r.run_id = m.processing_run_id
  -- константа (не подзапрос): иначе BigQuery не отсекает партиции и читает всю RAW (~0,37 ГБ)
  WHERE r._rr_date >= DATE_SUB(CURRENT_DATE('Europe/Moscow'), INTERVAL 14 DAY)
),
p_h AS (SELECT report_id, rrd_id, TO_HEX(SHA256(TO_JSON_STRING(t))) AS h FROM p_rows t),
c_h AS (SELECT report_id, rrd_id, TO_HEX(SHA256(TO_JSON_STRING(t))) AS h FROM c_rows t),
row_cmp AS (
  SELECT COALESCE(p.report_id, c.report_id) AS report_id,
         COUNTIF(p.rrd_id IS NOT NULL AND c.rrd_id IS NOT NULL AND p.h = c.h)  AS rows_equal,
         COUNTIF(p.rrd_id IS NOT NULL AND c.rrd_id IS NOT NULL AND p.h != c.h) AS rows_diff,
         COUNTIF(c.rrd_id IS NULL) AS rows_prod_only,
         COUNTIF(p.rrd_id IS NULL) AS rows_shadow_only
  FROM p_h p FULL OUTER JOIN c_h c ON p.report_id = c.report_id AND p.rrd_id = c.rrd_id
  GROUP BY 1
),
dup AS (
  SELECT report_id, COUNT(*) - COUNT(DISTINCT rrd_id) AS dup_rrd FROM p_h GROUP BY 1
  UNION ALL
  SELECT report_id, COUNT(*) - COUNT(DISTINCT rrd_id) FROM c_h GROUP BY 1
),
rep AS (
  SELECT COALESCE(p.report_id, c.report_id) AS report_id,
         COALESCE(p.date_from, c.date_from)   AS d,
         p.report_id IS NOT NULL AS in_prod,
         c.report_id IS NOT NULL AS in_shadow,
         -- манифест: всё, что не зависит от прогона
         (p.report_id IS NOT NULL AND c.report_id IS NOT NULL) AND NOT (
               p.report_period = c.report_period AND p.report_type = c.report_type
           AND p.date_from = c.date_from AND p.date_to = c.date_to
           AND p.rows_fetched = c.rows_fetched AND p.rows_loaded = c.rows_loaded
           AND p.list_forpay = c.list_forpay AND p.list_retail = c.list_retail AND p.list_delivery = c.list_delivery
           AND p.list_storage = c.list_storage AND p.list_acceptance = c.list_acceptance
           AND p.list_deduction = c.list_deduction AND p.list_penalty = c.list_penalty
           AND p.det_forpay = c.det_forpay AND p.det_retail = c.det_retail AND p.det_delivery = c.det_delivery
           AND p.det_storage = c.det_storage AND p.det_acceptance = c.det_acceptance
           AND p.det_deduction = c.det_deduction AND p.det_penalty = c.det_penalty
           AND p.list_metrics_json = c.list_metrics_json
         ) AS manifest_diff
  FROM pm p FULL OUTER JOIN cm c ON p.report_id = c.report_id
),
per_rep AS (
  SELECT r.*, IFNULL(x.rows_equal, 0) AS rows_equal, IFNULL(x.rows_diff, 0) AS rows_diff,
         IFNULL(x.rows_prod_only, 0) AS rows_prod_only, IFNULL(x.rows_shadow_only, 0) AS rows_shadow_only,
         IFNULL((SELECT SUM(dup_rrd) FROM dup WHERE dup.report_id = r.report_id), 0) AS dup_rrd
  FROM rep r LEFT JOIN row_cmp x USING (report_id)
),
days AS (
  SELECT d FROM UNNEST(GENERATE_DATE_ARRAY((SELECT d_from FROM win), CURRENT_DATE('Europe/Moscow'))) d
)
SELECT
  days.d AS report_date,
  COUNTIF(in_prod AND in_shadow)          AS reports_both,
  COUNTIF(in_prod AND NOT in_shadow)      AS reports_prod_only,
  COUNTIF(in_shadow AND NOT in_prod)      AS reports_shadow_only,
  COUNTIF(manifest_diff)                  AS manifest_diff,
  IFNULL(SUM(IF(in_prod AND in_shadow, rows_equal, 0)), 0) AS rows_equal,
  IFNULL(SUM(IF(in_prod AND in_shadow, rows_diff + rows_prod_only + rows_shadow_only, 0)), 0) AS rows_mismatch,
  IFNULL(SUM(dup_rrd), 0)                 AS dup_rrd,
  CASE
    WHEN COUNT(report_id) = 0 THEN 'NO_DATA'
    WHEN COUNTIF(manifest_diff) > 0
      OR IFNULL(SUM(IF(in_prod AND in_shadow, rows_diff + rows_prod_only + rows_shadow_only, 0)), 0) > 0
      OR IFNULL(SUM(dup_rrd), 0) > 0 THEN 'FAIL'
    WHEN COUNTIF(in_prod != in_shadow) > 0 THEN 'DATA_DRIFT'
    ELSE 'EXACT'
  END AS verdict
FROM days LEFT JOIN per_rep ON per_rep.d = days.d
GROUP BY days.d
ORDER BY days.d;

-- ─────────────────────────────────────────────────────────────────────────
-- QUERY 2 — ОТЧЁТЫ С РАСХОЖДЕНИЕМ (по одному на строку; номера отчётов в PR/документы НЕ переносить)
-- ─────────────────────────────────────────────────────────────────────────
WITH params AS (SELECT 14 AS lookback_days),
win AS (
  SELECT GREATEST(
           DATE_SUB(CURRENT_DATE('Europe/Moscow'), INTERVAL (SELECT lookback_days FROM params) DAY),
           IFNULL((SELECT MIN(week_start) FROM `wb_raw.FINANCE_WEEK_STATUS__CR`), DATE '2999-01-01')) AS d_from
),
pm AS (SELECT * FROM `wb_raw.FINANCE_REPORT_LOADS`     WHERE date_from >= (SELECT d_from FROM win)),
cm AS (SELECT * FROM `wb_raw.FINANCE_REPORT_LOADS__CR` WHERE date_from >= (SELECT d_from FROM win)),
p_rows AS (
  SELECT r.* EXCEPT (load_id, run_id, loaded_at) FROM `wb_raw.RAW_WB_FINANCE` r
  JOIN pm m ON r.report_id = m.report_id AND r.run_id = m.processing_run_id AND m.status = 'COMPLETE'
  -- константа (не подзапрос): иначе BigQuery не отсекает партиции и читает всю RAW (~0,37 ГБ)
  WHERE r._rr_date >= DATE_SUB(CURRENT_DATE('Europe/Moscow'), INTERVAL 14 DAY)
),
c_rows AS (
  SELECT r.* EXCEPT (load_id, run_id, loaded_at) FROM `wb_raw.RAW_WB_FINANCE__CR` r
  JOIN cm m ON r.report_id = m.report_id AND r.run_id = m.processing_run_id AND m.status = 'COMPLETE'
  -- константа (не подзапрос): иначе BigQuery не отсекает партиции и читает всю RAW (~0,37 ГБ)
  WHERE r._rr_date >= DATE_SUB(CURRENT_DATE('Europe/Moscow'), INTERVAL 14 DAY)
),
-- два хеша: всей строки и строки без raw_json — показывает, в raw_json ли расхождение
p_h AS (SELECT report_id, rrd_id, TO_HEX(SHA256(TO_JSON_STRING(t))) h,
               TO_HEX(SHA256(TO_JSON_STRING((SELECT AS STRUCT t.* EXCEPT (raw_json))))) h_noraw FROM p_rows t),
c_h AS (SELECT report_id, rrd_id, TO_HEX(SHA256(TO_JSON_STRING(t))) h,
               TO_HEX(SHA256(TO_JSON_STRING((SELECT AS STRUCT t.* EXCEPT (raw_json))))) h_noraw FROM c_rows t),
row_cmp AS (
  SELECT COALESCE(p.report_id, c.report_id) AS report_id,
         COUNTIF(p.h != c.h)                               AS rows_diff,
         COUNTIF(p.h != c.h AND p.h_noraw = c.h_noraw)     AS rows_diff_raw_json_only,
         COUNTIF(c.rrd_id IS NULL)                         AS rows_prod_only,
         COUNTIF(p.rrd_id IS NULL)                         AS rows_shadow_only
  FROM p_h p FULL OUTER JOIN c_h c ON p.report_id = c.report_id AND p.rrd_id = c.rrd_id
  GROUP BY 1
)
SELECT COALESCE(p.report_id, c.report_id) AS report_id,
       COALESCE(p.report_period, c.report_period) AS report_period,
       COALESCE(p.report_type, c.report_type)     AS report_type,
       COALESCE(p.date_from, c.date_from)         AS date_from,
       p.status AS prod_status, c.status AS shadow_status,
       p.attempt_count AS prod_attempts, c.attempt_count AS shadow_attempts,
       p.rows_loaded AS prod_rows, c.rows_loaded AS shadow_rows,
       p.det_forpay - c.det_forpay   AS d_det_forpay,
       p.list_forpay - c.list_forpay AS d_list_forpay,
       x.rows_diff, x.rows_diff_raw_json_only, x.rows_prod_only, x.rows_shadow_only,
       c.error_message AS shadow_error
FROM pm p FULL OUTER JOIN cm c ON p.report_id = c.report_id
LEFT JOIN row_cmp x ON x.report_id = COALESCE(p.report_id, c.report_id)
WHERE p.report_id IS NULL OR c.report_id IS NULL
   OR p.status != c.status
   OR IFNULL(x.rows_diff, 0) + IFNULL(x.rows_prod_only, 0) + IFNULL(x.rows_shadow_only, 0) > 0
   OR p.det_forpay != c.det_forpay OR p.list_forpay != c.list_forpay OR p.rows_loaded != c.rows_loaded
ORDER BY date_from, report_period, report_type;

-- ─────────────────────────────────────────────────────────────────────────
-- QUERY 3 — НЕДЕЛИ: финализация и RECON (неделя × тип), только недели календаря shadow
--   FAIL — неделя финальна в обоих, но weekly_report_id / daily_days_loaded / RECON различаются;
--   PENDING — финальна только на одной стороне (WEEKLY пришёл между слотами); иначе EXACT.
-- ─────────────────────────────────────────────────────────────────────────
WITH ws AS (
  SELECT c.week_start, c.report_type,
         p.weekly_final AS p_final, c.weekly_final AS c_final,
         p.weekly_report_id AS p_rid, c.weekly_report_id AS c_rid,
         p.daily_days_loaded AS p_days, c.daily_days_loaded AS c_days
  FROM `wb_raw.FINANCE_WEEK_STATUS__CR` c
  LEFT JOIN `wb_raw.FINANCE_WEEK_STATUS` p USING (week_start, report_type)
),
rc AS (
  SELECT c.week_start, c.report_type,
         COUNT(*) AS metrics,
         COUNTIF(p.metric IS NULL OR p.sum_daily != c.sum_daily OR p.sum_weekly != c.sum_weekly
                 OR p.recon_status != c.recon_status) AS recon_diff
  FROM `wb_raw.FINANCE_WEEK_RECON__CR` c
  LEFT JOIN `wb_raw.FINANCE_WEEK_RECON` p USING (week_start, report_type, metric)
  GROUP BY 1, 2
)
SELECT ws.week_start, ws.report_type, ws.p_final, ws.c_final, ws.p_days, ws.c_days,
       ws.p_rid = ws.c_rid AS same_weekly_report, IFNULL(rc.recon_diff, 0) AS recon_diff,
       CASE
         WHEN IFNULL(ws.p_final, FALSE) AND IFNULL(ws.c_final, FALSE)
              AND (ws.p_rid != ws.c_rid OR ws.p_days != ws.c_days OR IFNULL(rc.recon_diff, 0) > 0) THEN 'FAIL'
         WHEN IFNULL(ws.p_final, FALSE) != IFNULL(ws.c_final, FALSE) THEN 'PENDING'
         ELSE 'EXACT'
       END AS verdict
FROM ws LEFT JOIN rc USING (week_start, report_type)
ORDER BY ws.week_start, ws.report_type;
-- ⚠️ daily_days_loaded может законно разойтись, если shadow начал неделю позже prod (первая неделя
--    календаря shadow): shadow не грузил суточные отчёты до своего старта. Такую неделю разбирать вручную.

-- ─────────────────────────────────────────────────────────────────────────
-- QUERY 4 — ЖУРНАЛЫ ПРОГОНОВ по суткам МСК: статусы Apps Script и shadow, запросы к API, длительность
-- ─────────────────────────────────────────────────────────────────────────
SELECT DATE(started_at, 'Europe/Moscow') AS d, 'apps_script' AS side, status, COUNT(*) AS runs,
       SUM(reports_loaded) AS loaded, SUM(reports_errors) AS errors, SUM(requests_made) AS requests,
       MAX(TIMESTAMP_DIFF(finished_at, started_at, SECOND)) AS max_sec
FROM `wb_raw.FINANCE_LOADER_RUNS`
WHERE started_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 14 DAY)
GROUP BY 1, 2, 3
UNION ALL
SELECT DATE(started_at, 'Europe/Moscow'), 'cloud_run_shadow', status, COUNT(*),
       SUM(reports_loaded), SUM(reports_errors), SUM(requests_made),
       MAX(TIMESTAMP_DIFF(finished_at, started_at, SECOND))
FROM `wb_raw.FINANCE_LOADER_RUNS__CR`
WHERE started_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 14 DAY)
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3;
