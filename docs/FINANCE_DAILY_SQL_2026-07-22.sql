-- ══════════════════════════════════════════════════════════════
-- PR-Fin1: SQL-объекты BigQuery (справочно для аудита; создаёт код
-- wbFinDailyInitC0()/finDailyCreateViews_ в WbFinanceDaily.gs — 1:1).
-- Проект/датасет подставляются из getBqConfig_(); здесь P.D = плейсхолдер.
-- Дизайн: docs/FINANCE_DAILY_DESIGN_2026-07-22.md (ревизия 3-final).
--
-- v1.1 (code audit): C0 неразрушительный — вью §7–§9 сначала создаются
-- с суффиксом __TEST, снапшоты (count/MIN/MAX _rr_date/Σfor_pay/Σretail)
-- и schema-совместимость по INFORMATION_SCHEMA.COLUMNS сравниваются
-- программно со старым V_WB_FINANCE, и только при полном совпадении
-- вью пересоздаются под production-именами (затем __TEST удаляются).
-- ══════════════════════════════════════════════════════════════

-- 1. ALTER RAW (nullable, идемпотентно; raw_json/report_period_from/to уже есть)
ALTER TABLE `P.D.RAW_WB_FINANCE`
ADD COLUMN IF NOT EXISTS report_period STRING,   -- 'DAILY' | 'WEEKLY'
ADD COLUMN IF NOT EXISTS report_type INT64,      -- 1 основной | 2 по выкупам | 3 Грузия
ADD COLUMN IF NOT EXISTS run_id STRING;          -- NULL = legacy-строка

-- 2. Preflight (fail-closed в C0 и в установщике триггеров): ожидание = 0
SELECT COUNT(*) AS n FROM `P.D.RAW_WB_FINANCE`
WHERE run_id IS NULL AND _rr_date >= DATE '2026-07-13';

-- 3. Журнал прогонов
CREATE TABLE IF NOT EXISTS `P.D.FINANCE_LOADER_RUNS` (
  run_id STRING, started_at TIMESTAMP, finished_at TIMESTAMP, status STRING,
  trigger_type STRING, reports_discovered INT64, reports_loaded INT64,
  reports_errors INT64, requests_made INT64, error_message STRING);

-- 4. Manifest/очередь: grain = report_id (логический PK, только MERGE)
CREATE TABLE IF NOT EXISTS `P.D.FINANCE_REPORT_LOADS` (
  report_id STRING NOT NULL, report_period STRING, report_type INT64,
  date_from DATE, date_to DATE, status STRING,            -- DISCOVERED|STARTED|COMPLETE|ERROR
  discovered_run_id STRING, processing_run_id STRING, attempt_count INT64,
  discovered_at TIMESTAMP, started_at TIMESTAMP, completed_at TIMESTAMP,
  rows_fetched INT64, rows_loaded INT64,
  list_forpay NUMERIC, list_retail NUMERIC, list_delivery NUMERIC,
  list_storage NUMERIC, list_acceptance NUMERIC, list_deduction NUMERIC,
  list_penalty NUMERIC,
  det_forpay NUMERIC, det_retail NUMERIC, det_delivery NUMERIC,
  det_storage NUMERIC, det_acceptance NUMERIC, det_deduction NUMERIC,
  det_penalty NUMERIC,
  list_metrics_json STRING, error_message STRING);

-- 5. Статус недель
CREATE TABLE IF NOT EXISTS `P.D.FINANCE_WEEK_STATUS` (
  week_start DATE, report_type INT64, weekly_report_id STRING,
  weekly_final BOOL, finalized_at TIMESTAMP, daily_days_loaded INT64, notes STRING);

-- 6. Reconciliation недель
CREATE TABLE IF NOT EXISTS `P.D.FINANCE_WEEK_RECON` (
  week_start DATE, report_type INT64, metric STRING,
  sum_daily NUMERIC, sum_weekly NUMERIC, delta NUMERIC,
  recon_status STRING, checked_at TIMESTAMP);

-- 7. V_WB_FINANCE_COMPLETE: две явные ветви (блокер 1 rev2)
CREATE OR REPLACE VIEW `P.D.V_WB_FINANCE_COMPLETE` AS
WITH src AS (
  SELECT r.* FROM `P.D.RAW_WB_FINANCE` r WHERE r.run_id IS NULL   -- ВЕТВЬ 1: legacy
  UNION ALL
  SELECT r.* FROM `P.D.RAW_WB_FINANCE` r                          -- ВЕТВЬ 2: manifest-confirmed
  JOIN `P.D.FINANCE_REPORT_LOADS` m
    ON r.report_id = m.report_id AND r.run_id = m.processing_run_id
  WHERE m.status = 'COMPLETE'
)
SELECT * EXCEPT(_rn) FROM (
  SELECT s.*, ROW_NUMBER() OVER (
    PARTITION BY s.report_id, s.rrd_id
    ORDER BY s.loaded_at DESC, IFNULL(s.run_id, '') DESC
  ) AS _rn
  FROM src s
  WHERE s.rrd_id IS NOT NULL AND s.rrd_id != ''
) WHERE _rn = 1;

-- 8. V_WB_FINANCE_CANONICAL: cutover + замещение daily→weekly по (неделя × тип)
CREATE OR REPLACE VIEW `P.D.V_WB_FINANCE_CANONICAL` AS
WITH base AS (
  SELECT c.*, DATE_TRUNC(c._rr_date, WEEK(MONDAY)) AS week_start,
         COALESCE(c.report_period, 'WEEKLY') AS rp
  FROM `P.D.V_WB_FINANCE_COMPLETE` c
),
final_weeks AS (
  SELECT week_start, report_type FROM `P.D.FINANCE_WEEK_STATUS` WHERE weekly_final = TRUE
),
backfilled_weeks AS (
  SELECT DISTINCT DATE_TRUNC(date_from, WEEK(MONDAY)) AS week_start
  FROM `P.D.FINANCE_REPORT_LOADS`
  WHERE status = 'COMPLETE' AND report_period = 'WEEKLY' AND date_from < DATE '2026-07-13'
)
SELECT b.* EXCEPT(rp),
  CASE WHEN b.run_id IS NULL THEN 'LEGACY'
       WHEN b.rp = 'DAILY' THEN 'DAILY' ELSE 'WEEKLY' END AS source_layer,
  CASE WHEN b.run_id IS NOT NULL AND b.rp = 'DAILY'
       THEN 'PROVISIONAL' ELSE 'FINAL' END AS finance_status
FROM base b
WHERE
  (b.week_start IS NULL AND b.run_id IS NULL)              -- legacy без даты — сохраняем
  OR (b.week_start < DATE '2026-07-13' AND (               -- недели < cutover
       (b.run_id IS NULL
        AND b.week_start NOT IN (SELECT week_start FROM backfilled_weeks))
    OR (b.run_id IS NOT NULL AND b.rp = 'WEEKLY'
        AND b.week_start IN (SELECT week_start FROM backfilled_weeks))
  ))
  OR (b.week_start >= DATE '2026-07-13'                    -- недели >= cutover
      AND b.run_id IS NOT NULL AND (
       b.rp = 'WEEKLY'
    OR (b.rp = 'DAILY' AND NOT EXISTS (
          SELECT 1 FROM final_weeks f
          WHERE f.week_start = b.week_start AND f.report_type = b.report_type))
  ));

-- 9. V_WB_FINANCE (легаси-имя): WEEKLY-слой поверх COMPLETE.
-- Семантика для существующих читателей прежняя (недельный канон), но:
--  (а) daily-строки не просачиваются (не задваивают недели >= cutover);
--  (б) manifest-гейт наследуется — частичные попытки невидимы.
CREATE OR REPLACE VIEW `P.D.V_WB_FINANCE` AS
SELECT * FROM `P.D.V_WB_FINANCE_COMPLETE`
WHERE COALESCE(report_period, 'WEEKLY') = 'WEEKLY';

-- 10. Post-load контроль перед COMPLETE (per report; из кода, параметризовано)
-- SELECT COUNT(*) AS persisted_rows, COUNT(DISTINCT rrd_id) AS persisted_distinct_rrd
-- FROM `P.D.RAW_WB_FINANCE` WHERE report_id = @rid AND run_id = @processing_run_id;
-- Требование: persisted_rows = persisted_distinct_rrd = rows_fetched = rows_loaded.

-- 11. Self-test логического PK manifest
-- SELECT COUNT(*) = COUNT(DISTINCT report_id) FROM `P.D.FINANCE_REPORT_LOADS`;
