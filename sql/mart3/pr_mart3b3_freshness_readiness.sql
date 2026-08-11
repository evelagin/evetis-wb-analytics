-- PR-Mart3b-3 — доказательная база для расписания Cloud Scheduler `wb-mart-prod`.
--
-- Зачем: freshness-gate витрины работает по семантике LATEST ATTEMPT (см. MartBq.checkFreshness
-- в cloud/src/loaders/mart/bq.ts): для каждого из orders/sales/ads берётся ПОСЛЕДНЯЯ попытка
-- за logical_period = target_date (ORDER BY started_at DESC, run_id DESC), и она обязана быть
-- COMPLETE с непустым completed_at. Значит одна упавшая hourly-попытка делает гейт красным
-- ДО следующей успешной попытки того же загрузчика — независимо от того, что раньше в этот день
-- уже был COMPLETE. Расписание mart обязано учитывать эти «окна красноты», а не угадывать их.
--
-- Все запросы read-only и являются чистыми SELECT (параметры — через CTE `p`, БЕЗ scripting/DECLARE),
-- поэтому работают в том числе через read-only коннекторы. Каждый блок запускать отдельно.
-- Перепрогонять при изменении расписания, на rollout (PR-Mart3b-4) и через 2–3 недели после него.
-- ⚠️ История INGEST_RUNS начинается 02.08.2026 (PR#82). Полные сутки наблюдения — с 03.08.
--
-- Общий фильтр во всех запросах: started_at >= полуночи МСК дня (logical_period + 1).
-- Он повторяет условие боевого гейта — попытки, сделанные ДО закрытия целевых суток, не в счёт.

-- ═════════════════════════════════════════════════════════════════════════════
-- Q1. Первый момент суток, когда источники одновременно готовы.
-- Гейт может стать зелёным только в момент завершения какой-либо попытки → кандидаты = completed_at.
-- Если за сутки зелёного момента не было — вернётся NULL (честный ответ, а не пропавшая строка).
-- ═════════════════════════════════════════════════════════════════════════════
WITH att AS (
  SELECT loader_name, logical_period AS d, run_id, status, started_at, completed_at
  FROM `wb_raw.V_INGEST_HEARTBEAT`
  WHERE loader_name IN ('orders','sales','ads')
    AND started_at >= TIMESTAMP(DATE_ADD(logical_period, INTERVAL 1 DAY), 'Europe/Moscow')
),
cand AS (SELECT DISTINCT d, completed_at AS t FROM att WHERE completed_at IS NOT NULL),
ev AS (
  SELECT c.d, c.t, a.loader_name, a.status, a.completed_at,
         ROW_NUMBER() OVER (PARTITION BY c.d, c.t, a.loader_name
                            ORDER BY a.started_at DESC, a.run_id DESC) rn
  FROM cand c JOIN att a ON a.d = c.d AND a.started_at <= c.t
),
gate AS (
  SELECT d, t,
         COUNTIF(status='COMPLETE' AND completed_at IS NOT NULL AND completed_at <= t) AS green,
         COUNT(*) AS present   -- present<3 → какой-то загрузчик вообще без попыток → не зелёный
  FROM ev WHERE rn = 1 GROUP BY d, t
)
SELECT d AS target_date,
       FORMAT_TIMESTAMP('%Y-%m-%d %H:%M', MIN(IF(green=3 AND present=3, t, NULL)), 'Europe/Moscow')
         AS first_ready_msk
FROM gate GROUP BY d ORDER BY d;

-- ═════════════════════════════════════════════════════════════════════════════
-- Q2. ГЛАВНЫЙ запрос: доля зелёных суток по каждому часу МСК. Именно он выбирает cron.
-- Час с pct_green < 100 в расписание не берём.
--
-- Каркас ПОЛНОСТЬЮ ПЛОТНЫЙ (аудит REV3): days × hours × required_loaders + LEFT JOIN.
-- Иначе час, в котором ещё не существовало НИ ОДНОЙ попытки, просто исчезал бы из выборки
-- вместо того, чтобы честно считаться красным. При отсутствии попытки status IS NULL, поэтому
-- предикат обёрнут в COALESCE(..., FALSE): LOGICAL_AND игнорирует NULL и без этого вернул бы
-- TRUE по остальным загрузчикам. Это тот же fail-closed, что в боевом гейте.
-- Побочная польза плотного каркаса: в who_is_red видно `ads:NO_ATTEMPT` для ночных часов —
-- то есть видно ПРИЧИНУ красноты, а не только факт.
-- ⚙️ Границы окна наблюдения — в CTE `p`; расширять при перепрогоне.
-- ═════════════════════════════════════════════════════════════════════════════
WITH p AS (SELECT DATE '2026-08-03' AS d_from, DATE '2026-08-09' AS d_to),
att AS (
  SELECT loader_name, logical_period AS d, run_id, status, started_at, completed_at
  FROM `wb_raw.V_INGEST_HEARTBEAT`
  WHERE loader_name IN ('orders','sales','ads')
    AND started_at >= TIMESTAMP(DATE_ADD(logical_period, INTERVAL 1 DAY), 'Europe/Moscow')
),
days  AS (SELECT d FROM p, UNNEST(GENERATE_DATE_ARRAY(p.d_from, p.d_to)) AS d),
hours AS (SELECT h FROM UNNEST(GENERATE_ARRAY(0, 23)) AS h),
req   AS (SELECT l AS loader_name FROM UNNEST(['orders','sales','ads']) AS l),
spine AS (
  SELECT d, h, loader_name,
         TIMESTAMP_ADD(TIMESTAMP(DATE_ADD(d, INTERVAL 1 DAY), 'Europe/Moscow'), INTERVAL h HOUR) AS t
  FROM days CROSS JOIN hours CROSS JOIN req
),
ranked AS (
  SELECT s.d, s.h, s.t, s.loader_name, a.status, a.completed_at,
         ROW_NUMBER() OVER (PARTITION BY s.d, s.h, s.loader_name
                            ORDER BY a.started_at DESC, a.run_id DESC) rn
  FROM spine s
  LEFT JOIN att a
    ON a.d = s.d AND a.loader_name = s.loader_name AND a.started_at <= s.t
),
g AS (
  SELECT d, h,
         LOGICAL_AND(COALESCE(status='COMPLETE' AND completed_at IS NOT NULL AND completed_at <= t,
                              FALSE)) AS ok,
         STRING_AGG(IF(COALESCE(status='COMPLETE' AND completed_at IS NOT NULL AND completed_at <= t,
                                FALSE),
                       NULL,
                       loader_name || ':' || COALESCE(status, 'NO_ATTEMPT')),
                    ',' ORDER BY loader_name) AS red_loaders
  FROM ranked WHERE rn = 1 GROUP BY d, h
)
SELECT h AS hour_msk, COUNT(*) AS days, COUNTIF(ok) AS green,
       ROUND(100 * COUNTIF(ok) / COUNT(*), 1) AS pct_green,
       STRING_AGG(DISTINCT red_loaders, ' | ') AS who_is_red
FROM g GROUP BY h ORDER BY h;

-- ═════════════════════════════════════════════════════════════════════════════
-- Q3. Диагностика: в какие минуты МСК стартуют попытки и где сидят отказы.
-- Так был найден детерминированный слот sales 10:22 (WB HTTP 429 global limiter, 7 из 7 суток),
-- а также то, что ads делает РОВНО ОДНУ попытку в сутки (~05:07) без hourly-ретраев.
-- ═════════════════════════════════════════════════════════════════════════════
SELECT loader_name,
       FORMAT_TIMESTAMP('%H:%M', started_at, 'Europe/Moscow') AS start_msk,
       COUNT(*) AS n, COUNTIF(status='COMPLETE') AS ok_n, COUNTIF(status='ERROR') AS err_n
FROM `wb_raw.V_INGEST_HEARTBEAT`
WHERE loader_name IN ('orders','sales','ads')
GROUP BY loader_name, start_msk
HAVING COUNTIF(status='ERROR') > 0 OR COUNT(*) >= 3
ORDER BY err_n DESC, loader_name, start_msk;

-- ═════════════════════════════════════════════════════════════════════════════
-- Q4a. ВЕРДИКТ по rollout PR#87: позеленел ли слот sales 10:2x ПОСЛЕ синхронизации .gs.
--
-- ⚠️ Запрос сознательно отсекает историю по `sync_date`. Без отсечки он возвращал бы ERROR за
-- 04–10.08 навсегда, и вывод «фикс доехал» был бы недоказуем в принципе (аудит REV3).
-- Запрос всегда возвращает РОВНО ОДНУ строку — в том числе когда попыток после cutoff ещё нет
-- (тогда INCONCLUSIVE). Пустой результат как ответ не годится: его не отличить от «не проверяли».
-- ⚠️ Порог `min_days` считается по УНИКАЛЬНЫМ московским СУТКАМ, а не по числу попыток (аудит REV4):
--    дублирующий/ручной/восстановительный запуск дал бы 3 попытки за одни сутки и ложный CONFIRMED.
-- ⚠️ Вердикт при отказе намеренно нейтрален: ERROR в этом слоте — не обязательно 429 (бывает 5xx,
--    malformed payload и т.п.). Классификацию кода ошибки смотреть в Q4b, здесь её нет.
-- ⚙️ ПЕРЕД ЗАПУСКОМ подставить фактическую дату синхронизации PR#87 в Apps Script-проект
--    (НЕ дату merge в main — важен момент, когда новый код реально начал исполняться).
-- ═════════════════════════════════════════════════════════════════════════════
WITH p AS (SELECT DATE '2026-08-11' AS sync_date, 3 AS min_days),  -- ⚙️ заменить sync_date
slot AS (
  SELECT DATE(h.started_at, 'Europe/Moscow') AS run_day_msk, h.status
  FROM `wb_raw.V_INGEST_HEARTBEAT` h, p
  WHERE h.loader_name = 'sales'
    AND DATE(h.started_at, 'Europe/Moscow') >= p.sync_date
    AND EXTRACT(HOUR FROM h.started_at AT TIME ZONE 'Europe/Moscow') = 10
)
SELECT p.sync_date                                          AS pr87_sync_date,
       (SELECT COUNT(*)                        FROM slot)   AS attempts_after_sync,
       (SELECT COUNT(DISTINCT run_day_msk)     FROM slot)   AS days_after_sync,
       (SELECT COUNTIF(status='ERROR')         FROM slot)   AS errors_after_sync,
       CASE
         WHEN (SELECT COUNTIF(status='ERROR') FROM slot) > 0
           THEN 'NOT CONFIRMED — слот 10:2x содержит ERROR'
         WHEN (SELECT COUNT(DISTINCT run_day_msk) FROM slot) < p.min_days
           THEN 'INCONCLUSIVE — мало чистых суток после синка'
         ELSE 'CONFIRMED — слот 10:2x чист'
       END                                                  AS pr87_verdict
FROM p;

-- ═════════════════════════════════════════════════════════════════════════════
-- Q4b. Посуточная детализация того же слота — чтобы вердикт можно было перепроверить глазами.
-- Ожидаемый вид после успешного rollout:  11.08 10:22 COMPLETE / 12.08 10:22 COMPLETE / ...
-- ═════════════════════════════════════════════════════════════════════════════
WITH p AS (SELECT DATE '2026-08-11' AS sync_date)  -- ⚙️ тот же sync_date, что в Q4a
SELECT DATE(h.started_at, 'Europe/Moscow')                        AS run_day_msk,
       FORMAT_TIMESTAMP('%H:%M:%S', h.started_at, 'Europe/Moscow') AS started_msk,
       h.status, h.error_code, SUBSTR(h.error_message, 1, 100)     AS err
FROM `wb_raw.V_INGEST_HEARTBEAT` h, p
WHERE h.loader_name = 'sales'
  AND DATE(h.started_at, 'Europe/Moscow') >= p.sync_date
  AND EXTRACT(HOUR FROM h.started_at AT TIME ZONE 'Europe/Moscow') = 10
ORDER BY run_day_msk DESC;
