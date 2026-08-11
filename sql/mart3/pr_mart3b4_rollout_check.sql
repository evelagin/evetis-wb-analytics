-- PR-Mart3b-4 — ежедневная проверка rollout Cloud Scheduler `wb-mart-prod`.
--
-- Один запрос отвечает на вопрос «как отработала витрина этой ночью». Гонять КАЖДОЕ утро окна
-- наблюдения (3–5 дней после resume), пока не появятся алерты (PR-Mart3b-5).
-- Read-only, чистый SELECT (параметры через CTE `p`) — работает и через read-only коннектор.
--
-- ⚠️ ВАЖНО ПРО ПОЛНОТУ КАРТИНЫ. Журналы содержат только ПОПЫТКИ, дошедшие до lease. Окна, которые
-- отработали как `guard_skip` (витрина за D-1 уже COMPLETE), строк НЕ пишут — это штатно и
-- доказано на приёмке PR3b-3. Поэтому `attempts` — это НЕ число сработавших окон Scheduler.
-- Факт срабатывания окон смотреть в Cloud Scheduler / Cloud Run Executions, а не здесь.
--
-- ⚠️ ПРОПУЩЕННЫЙ ДЕНЬ ≠ ДЫРА В ДАННЫХ. `sp_build_mart_sku_daily` полностью пересобирает
-- MART_SKU_DAILY от global_start до target_date (проверено 11.08: один build_as_of_date на
-- всю таблицу, 705 дней истории). Неудачный день = ВИТРИНА УСТАРЕЛА, а не потеряла строки.

-- ═════════════════════════════════════════════════════════════════════════════
-- Q1. ГЛАВНЫЙ ЕЖЕДНЕВНЫЙ ЗАПРОС: по одной строке на каждый target_date окна наблюдения.
-- Каркас ПЛОТНЫЙ (GENERATE_DATE_ARRAY + LEFT JOIN): день, за который прогона не было вообще,
-- обязан быть ВИДЕН как ⚫, а не исчезнуть из выборки. Без этого 06.08 и 08.08 (реальные
-- пропуски) молча отсутствовали в первой версии проверки.
-- ⚙️ d_from — дата resume Scheduler.
-- ═════════════════════════════════════════════════════════════════════════════
WITH p AS (SELECT DATE '2026-08-11' AS d_from,                                   -- ⚙️ дата resume
                  DATE_SUB(CURRENT_DATE('Europe/Moscow'), INTERVAL 1 DAY) AS d_to),
spine AS (SELECT d AS target_date FROM p, UNNEST(GENERATE_DATE_ARRAY(p.d_from, p.d_to)) AS d),
runs AS (
  SELECT target_date, status, mart_rows, error_code, lease_only_no_mart, started_at,
         ads_activity_lagged,
         FORMAT_TIMESTAMP('%H:%M', started_at, 'Europe/Moscow') AS start_msk,
         ROUND(duration_ms / 1000) AS dur_s
  FROM `wb_mart.V_MART_RUN_LOG`, p
  WHERE environment = 'prod' AND target_date >= p.d_from
),
agg AS (
  SELECT s.target_date,
         COUNT(r.status)                                              AS attempts,
         COUNTIF(r.status = 'ERROR')                                  AS errors,
         MAX(IF(r.status = 'COMPLETE', r.start_msk,  NULL))           AS built_at_msk,
         MAX(IF(r.status = 'COMPLETE', r.mart_rows,  NULL))           AS mart_rows,
         MAX(IF(r.status = 'COMPLETE', r.dur_s,      NULL))           AS dur_s,
         STRING_AGG(IF(r.status = 'ERROR',
                       r.start_msk || '/' || IFNULL(r.error_code, '?'), NULL),
                    ' ' ORDER BY r.started_at)                        AS error_trail,
         LOGICAL_OR(IFNULL(r.lease_only_no_mart,  FALSE))             AS lease_only,
         LOGICAL_OR(IFNULL(r.ads_activity_lagged, FALSE))             AS ads_lagged
  FROM spine s LEFT JOIN runs r USING (target_date)
  GROUP BY s.target_date
)
SELECT target_date, attempts, errors, built_at_msk, mart_rows,
       mart_rows - LAG(mart_rows) OVER (ORDER BY target_date) AS rows_delta,
       dur_s, error_trail, ads_lagged,
       CASE
         WHEN attempts = 0        THEN '⚫ НЕТ ЖУРНАЛЬНОЙ ПОПЫТКИ — проверить Scheduler/Executions или guard_skip'
         WHEN lease_only          THEN '🔴 LEASE_ONLY_NO_MART — lease есть, MART_RUNS нет'
         WHEN mart_rows IS NULL   THEN '🔴 FAILED — витрина за этот день не построена'
         WHEN errors > 0          THEN '🟡 OK ПОСЛЕ РЕТРАЯ — разобрать error_trail'
         ELSE                          '🟢 OK'
       END AS verdict
FROM agg ORDER BY target_date DESC;

-- ═════════════════════════════════════════════════════════════════════════════
-- Q2. Детализация: почему упала конкретная попытка. Запускать только при 🔴/🟡 в Q1.
-- freshness_json показывает, КАКОЙ источник не покрыл target_date; steps_json — на каком шаге
-- остановились (freshness_gate / sp_bootstrap_facts / sp_build_mart_sku_daily / mart_snapshot).
-- ═════════════════════════════════════════════════════════════════════════════
SELECT FORMAT_TIMESTAMP('%Y-%m-%d %H:%M:%S', started_at, 'Europe/Moscow') AS started_msk,
       target_date, status, error_code, error_message, freshness_json, steps_json, run_id
FROM `wb_mart.V_MART_RUN_LOG`
WHERE environment = 'prod' AND status = 'ERROR'
ORDER BY started_at DESC
LIMIT 10;

-- ═════════════════════════════════════════════════════════════════════════════
-- Q3. Свежесть самой витрины «здесь и сейчас» — независимо от журналов.
-- build_as_of_date обязан быть D-1 МСК. Если отстал — витрина устарела, что бы ни писали журналы.
-- ═════════════════════════════════════════════════════════════════════════════
SELECT MAX(build_as_of_date)                                    AS build_as_of,
       DATE_SUB(CURRENT_DATE('Europe/Moscow'), INTERVAL 1 DAY)  AS expected_d_minus_1,
       DATE_DIFF(DATE_SUB(CURRENT_DATE('Europe/Moscow'), INTERVAL 1 DAY),
                 MAX(build_as_of_date), DAY)                    AS days_behind,
       COUNT(DISTINCT build_as_of_date)                         AS distinct_builds, -- обязан быть 1
       COUNT(*)                                                 AS mart_rows,
       MAX(day)                                                 AS max_data_day
FROM `wb_mart.MART_SKU_DAILY`;
