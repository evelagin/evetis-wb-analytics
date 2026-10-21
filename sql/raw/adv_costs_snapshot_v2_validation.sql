-- ============================================================================
-- Сверка снапшот-слоя расходов v2 (граница суток UTC) против v1. ТОЛЬКО ЧТЕНИЕ.
-- Рендер: python3 tools/ads_snapshot_v2_render.py pre|post <номер запроса>
--   pre  — ДО развёртывания: NEW = тело v2 из adv_costs_snapshot_v2_utc_edge.sql (подзапрос),
--          OLD = живые wb_raw.V_ADV_COSTS_SNAPSHOT / V_ADV_COSTS_DAY_COVERAGE;
--   post — ПОСЛЕ развёртывания: NEW = живые вью, OLD = тело v1 из adv_costs_snapshot_v1.sql.
-- Плейсхолдеры: @@SNAP_NEW@@ @@SNAP_OLD@@ @@COV_NEW@@ @@COV_OLD@@.
-- Ожидания (pre, 21.10.2026) — в комментарии к каждому запросу. Любое отличие — STOP.
-- 🔴 Срок годности ожиданий: прогон до 26.10.2026 включительно. С 27.10 v1 начнёт обрезать новые
--    даты (13.10 → 27.10, 17.10 → 31.10, 21.10 → 04.11), и Q1/Q2/Q5 законно покажут их дополнительно
--    к пяти датам ниже. Перед ACK после 26.10 — пересчитать ожидания pre-прогоном и обновить ACK.
-- ============================================================================

-- @check Q1 — суммы по датам: меняются ровно 5 дат, все вверх, итого +137 ₽.
-- Ожидание: 5 строк — 2026-07-12 +108, 2026-09-03 +2, 2026-09-18 +3, 2026-09-19 +13, 2026-09-24 +11.
WITH n AS (SELECT SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(updDate, 1, 10)) d, COUNT(*) rows_n,
                  SUM(SAFE_CAST(REPLACE(updSum, ',', '.') AS NUMERIC)) sum_n FROM @@SNAP_NEW@@ GROUP BY 1),
     o AS (SELECT SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(updDate, 1, 10)) d, COUNT(*) rows_o,
                  SUM(SAFE_CAST(REPLACE(updSum, ',', '.') AS NUMERIC)) sum_o FROM @@SNAP_OLD@@ GROUP BY 1)
SELECT CAST(d AS STRING) d, rows_o, rows_n, sum_o, sum_n, IFNULL(sum_n, 0) - IFNULL(sum_o, 0) AS delta_rub
FROM n FULL OUTER JOIN o USING (d)
WHERE rows_n IS DISTINCT FROM rows_o OR sum_n IS DISTINCT FROM sum_o
ORDER BY d;

-- @check Q2 — итог эффекта по месяцам (для Executive P&L после пересборки витрины).
-- Ожидание: 2026-07 +108, 2026-09 +29; остальные месяцы 0.
WITH n AS (SELECT FORMAT_DATE('%Y-%m', SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(updDate, 1, 10))) m,
                  SUM(SAFE_CAST(REPLACE(updSum, ',', '.') AS NUMERIC)) s FROM @@SNAP_NEW@@ GROUP BY 1),
     o AS (SELECT FORMAT_DATE('%Y-%m', SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(updDate, 1, 10))) m,
                  SUM(SAFE_CAST(REPLACE(updSum, ',', '.') AS NUMERIC)) s FROM @@SNAP_OLD@@ GROUP BY 1)
SELECT m, o.s AS old_rub, n.s AS new_rub, IFNULL(n.s, 0) - IFNULL(o.s, 0) AS delta_rub
FROM n FULL OUTER JOIN o USING (m) ORDER BY m;

-- @check Q3 — дубли записей: v2 не добавляет ни одного дубля (advertId, updTime) внутри даты.
-- Идентичность НОРМАЛИЗОВАНА (updTime как момент времени, advertId как число) — сырые строки не
-- поймали бы один момент в двух сериализациях. Ожидание: dup_new = dup_old (=0 на 21.10.2026).
SELECT
  (SELECT COUNT(*) - COUNT(DISTINCT FORMAT('%s|%s|%s', SUBSTR(updDate, 1, 10),
          COALESCE(CAST(SAFE_CAST(advertId AS INT64) AS STRING), advertId),
          COALESCE(CAST(SAFE_CAST(updTime AS TIMESTAMP) AS STRING), updTime))) FROM @@SNAP_NEW@@) AS dup_new,
  (SELECT COUNT(*) - COUNT(DISTINCT FORMAT('%s|%s|%s', SUBSTR(updDate, 1, 10),
          COALESCE(CAST(SAFE_CAST(advertId AS INT64) AS STRING), advertId),
          COALESCE(CAST(SAFE_CAST(updTime AS TIMESTAMP) AS STRING), updTime))) FROM @@SNAP_OLD@@) AS dup_old;

-- @check Q4 — каждая запись v1 осталась в v2 (v2 только ДОБАВЛЯЕТ ночные записи краевых дат),
-- и выбранный ран сменился только там, где v1 выбирала краевое прочтение.
-- Ожидание: lost_records = 0; changed_chosen — только даты из Q1 и даты с идентичными суммами.
SELECT
  (SELECT COUNT(*) FROM @@SNAP_OLD@@ o
   WHERE NOT EXISTS (SELECT 1 FROM @@SNAP_NEW@@ n
                     WHERE n.advertId = o.advertId AND n.updTime = o.updTime
                       AND SUBSTR(n.updDate, 1, 10) = SUBSTR(o.updDate, 1, 10))) AS lost_records,
  (SELECT COUNT(*) FROM @@COV_NEW@@ n JOIN @@COV_OLD@@ o USING (`date`)
   WHERE n.chosen_run_id IS DISTINCT FROM o.chosen_run_id) AS changed_chosen,
  (SELECT COUNT(*) FROM @@COV_NEW@@ n JOIN @@COV_OLD@@ o USING (`date`)
   WHERE n.chosen_run_id IS DISTINCT FROM o.chosen_run_id AND n.answer_sum_rub != o.answer_sum_rub) AS changed_chosen_and_sum;

-- @check Q5 — покрытие: статусы дат v1 → v2.
-- Ожидание: complete→incomplete = 0; incomplete→complete = 03/18/19/24.09; EDGE_ONLY = 13.04.
SELECT CAST(n.`date` AS STRING) d,
       o.billed_complete AS old_complete, n.billed_complete AS new_complete,
       o.stable_reads AS old_stable, n.stable_reads AS new_stable,
       o.revision_count AS old_revisions, n.revision_count AS new_revisions,
       o.answer_sum_rub AS old_sum, n.answer_sum_rub AS new_sum,
       n.read_basis, n.edge_reads_ignored
FROM @@COV_NEW@@ n JOIN @@COV_OLD@@ o USING (`date`)
WHERE n.billed_complete IS DISTINCT FROM o.billed_complete
   OR n.answer_sum_rub != o.answer_sum_rub
   OR n.read_basis = 'EDGE_ONLY'
   OR n.needs_recheck IS DISTINCT FROM o.needs_recheck
   OR n.not_loaded IS DISTINCT FROM o.not_loaded
ORDER BY d;

-- @check Q6 — поздние ревизии не теряются. Независимо от обеих вью пересчитываются РЕАЛЬНЫЕ ревизии:
-- смена нормализованного содержимого между соседними НЕкраевыми прочтениями-окнами даты (краевые —
-- дата первая в окне — из ряда исключены ДО LAG, иначе краевое прочтение между двумя полными
-- спрятало бы смену A→C). Журнал — как во вью: последний маркер окна (rn = 1), затем OK.
-- Ожидание: shortfall = 0 во всех строках (на 21.10.2026 реальных ревизий 0).
WITH j AS (
  SELECT * FROM (
    SELECT run_id, SAFE_CAST(window_index AS INT64) wi,
           SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(period_from, 1, 10)) pf,
           SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(period_to, 1, 10)) pt,
           SAFE_CAST(IFNULL(window_completed_at, load_ts) AS TIMESTAMP) jts, status, http_success,
           ROW_NUMBER() OVER (PARTITION BY run_id, SAFE_CAST(window_index AS INT64)
                              ORDER BY SAFE_CAST(IFNULL(window_completed_at, load_ts) AS TIMESTAMP) DESC) rn
    FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_ADV_COSTS_RUNS`
    WHERE source_method = 'adv/v1/upd')
  WHERE rn = 1 AND status = 'OK' AND http_success = 'true' AND pf IS NOT NULL AND pt IS NOT NULL
),
r AS (
  SELECT run_id, SAFE_CAST(window_index AS INT64) wi, SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(updDate, 1, 10)) d,
         FORMAT('%s|%s|%s',
                COALESCE(CAST(SAFE_CAST(advertId AS INT64) AS STRING), advertId),
                COALESCE(CAST(SAFE_CAST(updTime AS TIMESTAMP) AS STRING), updTime),
                COALESCE(FORMAT('%.2f', SAFE_CAST(REPLACE(updSum, ',', '.') AS NUMERIC)), updSum)) rec
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_ADV_COSTS` WHERE source_method = 'adv/v1/upd'
),
reads AS (
  SELECT dd AS d, j.run_id, j.wi, j.jts,
         TO_HEX(SHA256(IFNULL((SELECT STRING_AGG(rec, '\n' ORDER BY rec) FROM r
                               WHERE r.run_id = j.run_id AND r.wi = j.wi AND r.d = dd), ''))) AS fp
  FROM j, UNNEST(GENERATE_DATE_ARRAY(j.pf, j.pt)) dd
  WHERE dd > j.pf                                  -- только НЕкраевые прочтения
),
real AS (
  SELECT d, COUNTIF(prev_fp IS NOT NULL AND fp != prev_fp) AS real_revisions
  FROM (SELECT d, fp, LAG(fp) OVER (PARTITION BY d ORDER BY jts, run_id, wi) AS prev_fp FROM reads)
  GROUP BY d
)
SELECT CAST(n.`date` AS STRING) d, o.revision_count AS old_revisions, n.revision_count AS new_revisions,
       IFNULL(real.real_revisions, 0) AS real_revisions,
       GREATEST(IFNULL(real.real_revisions, 0) - n.revision_count, 0) AS shortfall
FROM @@COV_NEW@@ n JOIN @@COV_OLD@@ o USING (`date`) LEFT JOIN real ON real.d = n.`date`
WHERE o.revision_count > 0 OR n.revision_count > 0 OR IFNULL(real.real_revisions, 0) > 0
ORDER BY d;

-- @check Q7 — согласованность двух вью v2: сумма И число строк покрытия = снапшоту по каждой дате.
-- Ожидание: 0 строк.
WITH s AS (SELECT SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(updDate, 1, 10)) d, COUNT(*) n,
                  SUM(SAFE_CAST(REPLACE(updSum, ',', '.') AS NUMERIC)) s FROM @@SNAP_NEW@@ GROUP BY 1)
SELECT CAST(c.`date` AS STRING) d, c.answer_sum_rub, s.s, c.answer_rows, s.n
FROM @@COV_NEW@@ c LEFT JOIN s ON s.d = c.`date`
WHERE c.chosen_run_id IS NOT NULL
  AND (c.answer_sum_rub != IFNULL(s.s, 0) OR c.answer_rows != IFNULL(s.n, 0))
ORDER BY d;

-- @check Q8 — предпосылка v2 «внутри рана дата ровно в одном окне» (правило 4 v1).
-- Ожидание: overlapping_pairs = 0. Иначе v2 не применять: строки прочтений надо переразобрать.
WITH j AS (
  SELECT * FROM (
    SELECT run_id, SAFE_CAST(window_index AS INT64) wi,
           SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(period_from, 1, 10)) pf,
           SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(period_to, 1, 10)) pt, status, http_success,
           ROW_NUMBER() OVER (PARTITION BY run_id, SAFE_CAST(window_index AS INT64)
                              ORDER BY SAFE_CAST(IFNULL(window_completed_at, load_ts) AS TIMESTAMP) DESC) rn
    FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_ADV_COSTS_RUNS`
    WHERE source_method = 'adv/v1/upd')
  WHERE rn = 1 AND status = 'OK' AND http_success = 'true'
)
SELECT COUNT(*) AS overlapping_pairs, COUNTIF(wi IS NULL) AS null_window_index
FROM (SELECT run_id, dd, ANY_VALUE(wi) wi, COUNT(*) n FROM j, UNNEST(GENERATE_DATE_ARRAY(pf, pt)) dd GROUP BY 1, 2)
WHERE n > 1 OR wi IS NULL;
