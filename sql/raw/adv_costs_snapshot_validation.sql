-- ============================================================================
-- EVETIS WB — Stage 3B.1. BASELINE + приёмка + отчёт по каждому bootstrap-проходу.
-- Дата: 20.08.2026. 🔴 Только SELECT. Ни одного DDL/DML.
-- Дизайн: docs/ADS_COSTS_SNAPSHOT_CONTRACT_2026-08-20.md
--
-- ПОРЯДОК ПО ФАЗАМ:
--   Фаза A: B0 (до деплоя) → E1 (гейт подмены V_ADV_COSTS) → I1–I3 → A1+P1 после
--           КАЖДОГО bootstrap-прохода → X1 после третьего.
--   Фаза B: I4–I9 и DAY_LOST=0 непосредственно перед cutover → после cutover
--           повтор I5/X1 на пересобранном FACT.
--
-- 🔴 ИМЕНА: снапшот-семантика проверяется по V_ADV_COSTS_SNAPSHOT — она существует
--    с Фазы A и не зависит от того, переключено ли production-имя. По V_ADV_COSTS
--    проверяется только то, что реально потребляет витрина (B0, A1).
-- ============================================================================

-- ════════════════════════════════════════════════════════════════
-- B0. BASELINE ДО МИГРАЦИИ. Снять и сохранить в PR-ноту.
-- ════════════════════════════════════════════════════════════════
SELECT 'V_ADV_COSTS (union, до миграции)' AS obj,
       COUNT(*) AS rows_,
       ROUND(SUM(SAFE_CAST(REPLACE(updSum, ',', '.') AS NUMERIC)), 2) AS sum_rub,
       CAST(MIN(SUBSTR(updDate,1,10)) AS STRING) AS min_date,
       CAST(MAX(SUBSTR(updDate,1,10)) AS STRING) AS max_date
FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_ADV_COSTS`
UNION ALL
SELECT 'FACT_ADS_COSTS_DAILY', COUNT(*), ROUND(SUM(actual_spend_rub), 2),
       CAST(MIN(`date`) AS STRING), CAST(MAX(`date`) AS STRING)
FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ADS_COSTS_DAILY`;

-- помесячный срез — чтобы откат сверялся не одной цифрой
SELECT DATE_TRUNC(`date`, MONTH) AS month, COUNT(*) AS rows_, SUM(actual_spend_rub) AS rub
FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ADS_COSTS_DAILY`
GROUP BY month ORDER BY month;

-- K9 прежнего тела (для отката)
SELECT table_name AS obj, TO_HEX(SHA256(view_definition)) AS sha, LENGTH(view_definition) AS len
FROM `project-fa311fc0-4d87-4781-986.wb_raw.INFORMATION_SCHEMA.VIEWS`
WHERE table_name IN ('V_ADV_COSTS', 'V_ADV_COSTS_UNION_LEGACY',
                     'V_ADV_COSTS_UNION_PREBOOTSTRAP', 'V_ADV_COSTS_DAY_COVERAGE')
ORDER BY obj;


-- ════════════════════════════════════════════════════════════════
-- E1. ЭКВИВАЛЕНТНОСТЬ ФАЗЫ A: LEGACY == UNION_PREBOOTSTRAP.
--     🔴 Выполнять ДО первого bootstrap-прохода и ДО первого рана нового контура.
--     Это гейт подмены production-вью: пока не доказано, что новая вью отдаёт то же
--     самое, менять V_ADV_COSTS нельзя. Сравнение на четырёх уровнях, потому что
--     совпадение сумм само по себе ничего не доказывает: разные наборы строк
--     легко дают одинаковый итог.
--     Ожидается: все *_diff и *_only_* = 0, оба итога равны, new_contour_runs = 0.
-- ════════════════════════════════════════════════════════════════
WITH legacy AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_ADV_COSTS_UNION_LEGACY`),
preboot AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_ADV_COSTS_UNION_PREBOOTSTRAP`),
lk AS (SELECT FORMAT('%t|%t|%t', advertId, updTime, updSum) k FROM legacy),
pk AS (SELECT FORMAT('%t|%t|%t', advertId, updTime, updSum) k FROM preboot),
lh AS (SELECT TO_HEX(SHA256(TO_JSON_STRING(t))) h FROM legacy t),
ph AS (SELECT TO_HEX(SHA256(TO_JSON_STRING(t))) h FROM preboot t),
ld AS (SELECT SUBSTR(updDate,1,10) d, SUM(SAFE_CAST(REPLACE(updSum,',','.') AS NUMERIC)) v FROM legacy GROUP BY d),
pd AS (SELECT SUBSTR(updDate,1,10) d, SUM(SAFE_CAST(REPLACE(updSum,',','.') AS NUMERIC)) v FROM preboot GROUP BY d)
SELECT
  (SELECT COUNT(*) FROM legacy)  AS legacy_rows,
  (SELECT COUNT(*) FROM preboot) AS preboot_rows,
  (SELECT COUNT(*) FROM (SELECT k FROM lk EXCEPT DISTINCT SELECT k FROM pk)) AS keys_only_in_legacy,
  (SELECT COUNT(*) FROM (SELECT k FROM pk EXCEPT DISTINCT SELECT k FROM lk)) AS keys_only_in_preboot,
  (SELECT COUNT(*) FROM (SELECT h FROM lh EXCEPT DISTINCT SELECT h FROM ph)) AS rows_only_in_legacy_fullhash,
  (SELECT COUNT(*) FROM (SELECT h FROM ph EXCEPT DISTINCT SELECT h FROM lh)) AS rows_only_in_preboot_fullhash,
  (SELECT COUNT(*) FROM ld FULL OUTER JOIN pd USING (d)
     WHERE IFNULL(ld.v, -1) <> IFNULL(pd.v, -1))                             AS days_with_sum_diff,
  (SELECT ROUND(SUM(v),2) FROM ld) AS legacy_total,
  (SELECT ROUND(SUM(v),2) FROM pd) AS preboot_total,
  (SELECT COUNT(DISTINCT run_id) FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_ADV_COSTS`
    WHERE STARTS_WITH(run_id,'ADSBACKFILL_') OR STARTS_WITH(run_id,'ADSAUDIT_')
       OR STARTS_WITH(run_id,'ADSRECHECK_')) AS new_contour_runs;


-- ════════════════════════════════════════════════════════════════
-- A1. МОНИТОР ФАЗЫ A. Запускать после КАЖДОГО bootstrap-прохода, вместе с P1.
--     Доказывает две вещи одновременно:
--       1) bootstrap-строки НЕ протекли в бизнес-семантику (помесячные суммы
--          совпадают с B0 до копейки);
--       2) production-поток НЕ замер (max(date) продолжает двигаться) — это ловит
--          ошибку «отсечка по времени вместо префикса», из-за которой вью
--          заморозилась бы молча.
--     🔴 Сверять с B0 вручную: помесячные числа B0 записаны в PR-ноту.
-- ════════════════════════════════════════════════════════════════
SELECT
  DATE_TRUNC(SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(updDate,1,10)), MONTH) AS month,
  COUNT(*) AS rows_,
  ROUND(SUM(SAFE_CAST(REPLACE(updSum,',','.') AS NUMERIC)), 2) AS rub
FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_ADV_COSTS`
GROUP BY month ORDER BY month;

SELECT
  CAST(MAX(`date`) AS STRING) AS fact_max_date,
  DATE_DIFF(CURRENT_DATE('Europe/Moscow'), MAX(`date`), DAY) AS fact_lag_days,
  ROUND(SUM(actual_spend_rub), 2) AS fact_total_rub,
  COUNT(*) AS fact_rows
FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ADS_COSTS_DAILY`;
-- ожидается: fact_lag_days не растёт от прохода к проходу (штатные раны продолжают идти),
--            fact_total_rub равен B0 (bootstrap в бизнес-семантику не протёк).


-- ════════════════════════════════════════════════════════════════
-- I1–I9. ИНВАРИАНТЫ. Все ожидания — нули, если не сказано иное.
-- ════════════════════════════════════════════════════════════════

-- I1. Ни одной строки RAW нового рана без строки журнала того же окна.
--     🔴 Проверяется ТОЛЬКО на ранах, у которых журнал вообще есть: исторические
--     раны его не писали, и требовать от них маркер бессмысленно.
WITH new_runs AS (
  SELECT DISTINCT run_id FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_ADV_COSTS_RUNS`
)
SELECT COUNT(*) AS orphan_rows
FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_ADV_COSTS` r
WHERE r.run_id IN (SELECT run_id FROM new_runs)
  AND NOT EXISTS (
    SELECT 1 FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_ADV_COSTS_RUNS` j
    WHERE j.run_id = r.run_id
      AND SAFE_CAST(j.window_index AS INT64) = SAFE_CAST(r.window_index AS INT64));

-- I2. Внутри (run_id, date) — ровно одно окно. Ожидается 0.
SELECT COUNT(*) AS run_days_with_two_windows FROM (
  SELECT run_id, SUBSTR(updDate,1,10) d
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_ADV_COSTS`
  WHERE source_method='adv/v1/upd'
  GROUP BY run_id, d
  HAVING COUNT(DISTINCT FORMAT('%t|%t', period_from, period_to)) > 1);

-- I3. (advertId, updTime) уникален внутри (run_id, updDate). Ожидается 0.
SELECT COUNT(*) AS identity_violations FROM (
  SELECT run_id, SUBSTR(updDate,1,10) d
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_ADV_COSTS`
  WHERE source_method='adv/v1/upd'
  GROUP BY run_id, d
  HAVING COUNT(*) <> COUNT(DISTINCT FORMAT('%t|%t', advertId, updTime)));

-- I4. Каждая дата canonical происходит ровно из ОДНОГО (run_id, window_index).
--     Ожидается 0 — смешение строк двух ранов/окон запрещено правилом 4.
SELECT COUNT(*) AS dates_with_mixed_snapshots FROM (
  SELECT SUBSTR(updDate,1,10) d
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_ADV_COSTS_SNAPSHOT`
  GROUP BY d
  HAVING COUNT(DISTINCT FORMAT('%t|%t', run_id, window_index)) > 1);

-- I5. Сверка нового canonical против прежнего FACT. 🔴 ГЕЙТ ОДИН: day_lost = 0.
--     Список изменившихся дат — к разбору, а НЕ к сравнению с ожиданием:
--     bootstrap читает WB заново, и WB вправе ответить иначе, чем в прошлый раз.
WITH new_canon AS (
  SELECT SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(updDate,1,10)) AS `date`,
         SUM(SAFE_CAST(REPLACE(updSum, ',', '.') AS NUMERIC)) AS new_rub
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_ADV_COSTS_SNAPSHOT` GROUP BY 1),
old_fact AS (
  SELECT `date`, SUM(actual_spend_rub) AS old_rub
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ADS_COSTS_DAILY` GROUP BY `date`)
SELECT
  COUNTIF(n.`date` IS NULL) AS day_lost,
  COUNTIF(o.`date` IS NULL) AS day_new,
  COUNTIF(n.`date` IS NOT NULL AND o.`date` IS NOT NULL AND n.new_rub <> o.old_rub) AS day_changed,
  ROUND(SUM(IFNULL(n.new_rub,0)) - SUM(IFNULL(o.old_rub,0)), 2) AS total_delta
FROM new_canon n FULL OUTER JOIN old_fact o USING (`date`);

-- I6. billed_complete = TRUE только при выполнении всех трёх условий. Ожидается 0.
SELECT COUNTIF(billed_complete AND NOT (requested_ok AND age_ok AND stable_ok)) AS violations,
       COUNTIF(billed_complete) AS settled_days,
       COUNTIF(NOT billed_complete) AS unsettled_days,
       COUNTIF(zero_spend_day) AS zero_spend_days,
       COUNTIF(not_loaded) AS not_loaded_days
FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_ADV_COSTS_DAY_COVERAGE`;

-- I7. 🔴 НЕ разовая проверка: максимальный наблюдённый лаг ревизии обязан быть
--     МЕНЬШЕ SETTLE_DAYS. Если сравнялся или превысил — константу поднимают,
--     а не объявляют исключением. Запускать вместе с недельным аудитом.
SELECT
  MAX(DATE_DIFF(DATE(revised_after_settled_at, 'Europe/Moscow'), `date`, DAY)) AS max_revision_lag_days,
  ANY_VALUE(contract_settle_days) AS contract_settle_days,
  COUNTIF(revised_after_settled_at IS NOT NULL) AS days_revised_after_settlement,
  MAX(DATE_DIFF(DATE(last_successful_read_at, 'Europe/Moscow'), `date`, DAY)) AS max_observation_horizon_days
FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_ADV_COSTS_DAY_COVERAGE`;

-- I8. Ни одного дня, объявленного нулевым «по умолчанию»: у zero_spend_day обязан
--     быть успешный журнальный ответ. Ожидается 0.
SELECT COUNT(*) AS zero_days_without_evidence
FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_ADV_COSTS_DAY_COVERAGE` c
WHERE c.zero_spend_day
  AND NOT EXISTS (
    SELECT 1 FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_ADV_COSTS_RUNS` j
    WHERE j.status='OK' AND j.http_success='true'
      AND c.`date` BETWEEN SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(j.period_from,1,10))
                       AND SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(j.period_to,1,10)));

-- I9. K9: repo == production по всем четырём объектам (см. запрос в B0).


-- ════════════════════════════════════════════════════════════════
-- P1. ОТЧЁТ ПОСЛЕ КАЖДОГО BOOTSTRAP-ПРОХОДА. Запускать после проходов 1, 2 и 3
--     и сравнивать между собой: смысл в динамике, а не в одном срезе.
-- ════════════════════════════════════════════════════════════════
WITH cov AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_ADV_COSTS_DAY_COVERAGE`),
new_canon AS (
  SELECT SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(updDate,1,10)) AS d,
         SUM(SAFE_CAST(REPLACE(updSum, ',', '.') AS NUMERIC)) AS new_rub
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_ADV_COSTS_SNAPSHOT` GROUP BY d),
old_union AS (
  SELECT SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(updDate,1,10)) AS d,
         SUM(SAFE_CAST(REPLACE(updSum, ',', '.') AS NUMERIC)) AS old_rub
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_ADV_COSTS_UNION_PREBOOTSTRAP` GROUP BY d),
fact AS (
  SELECT `date` AS d, SUM(actual_spend_rub) AS fact_rub
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ADS_COSTS_DAILY` GROUP BY `date`)
SELECT
  (SELECT COUNT(*)                         FROM cov) AS days_covered,
  (SELECT COUNTIF(billed_complete)         FROM cov) AS settled,
  (SELECT COUNTIF(NOT billed_complete)     FROM cov) AS unsettled,
  (SELECT COUNTIF(not_loaded)              FROM cov) AS not_loaded,
  (SELECT COUNTIF(zero_spend_day)          FROM cov) AS zero_spend_days,
  (SELECT MIN(stable_reads)                FROM cov) AS min_stable_reads,
  (SELECT MAX(stable_reads)                FROM cov) AS max_stable_reads,
  (SELECT ROUND(AVG(stable_reads), 2)      FROM cov) AS avg_stable_reads,
  (SELECT SUM(revision_count)              FROM cov) AS revisions_total,
  (SELECT COUNTIF(revised_after_settled_at IS NOT NULL) FROM cov) AS late_revisions,
  (SELECT COUNTIF(needs_recheck)           FROM cov) AS needs_recheck,
  (SELECT ROUND(SUM(new_rub), 2)           FROM new_canon) AS canonical_total_rub,
  (SELECT ROUND(SUM(old_rub), 2)           FROM old_union) AS prebootstrap_union_total_rub,
  (SELECT ROUND(SUM(new_rub), 2) FROM new_canon)
    - (SELECT ROUND(SUM(old_rub), 2) FROM old_union)      AS delta_vs_old_union,
  (SELECT COUNT(*) FROM fact f LEFT JOIN new_canon n USING (d) WHERE n.d IS NULL) AS day_lost,
  (SELECT COUNT(*) FROM new_canon n LEFT JOIN fact f USING (d) WHERE f.d IS NULL) AS day_new;

-- даты, требующие точечного перечитывания → в Script Property WB_ADS_COSTS_RECHECK_DATES
SELECT STRING_AGG(FORMAT('%t', `date`), ',' ORDER BY `date`) AS recheck_dates
FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_ADV_COSTS_DAY_COVERAGE`
WHERE needs_recheck;


-- ════════════════════════════════════════════════════════════════
-- X1. ДОКАЗАТЕЛЬСТВО ИСПРАВЛЕНИЯ ТРЁХ МЕХАНИЗМОВ (334,00 ₽).
--     Ожидается: по каждой дате canonical совпал с последним ответом WB,
--     а прежний union был завышен ровно на указанную величину.
-- ════════════════════════════════════════════════════════════════
WITH known AS (
  SELECT * FROM UNNEST([
    STRUCT(DATE '2026-07-10' AS d, 'REVOKED_RECORD'  AS mechanism, NUMERIC '-77'  AS expected_delta),
    STRUCT(DATE '2026-08-02',      'OUT_OF_WINDOW',                NUMERIC '-61'),
    STRUCT(DATE '2026-08-04',      'OUT_OF_WINDOW',                NUMERIC  '-4'),
    STRUCT(DATE '2026-08-05',      'TRANSIENT_SPIKE',              NUMERIC '-192')
  ])),
new_canon AS (
  SELECT SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(updDate,1,10)) AS d,
         SUM(SAFE_CAST(REPLACE(updSum, ',', '.') AS NUMERIC)) AS new_rub
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_ADV_COSTS_SNAPSHOT` GROUP BY d),
old_union AS (
  SELECT SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(updDate,1,10)) AS d,
         SUM(SAFE_CAST(REPLACE(updSum, ',', '.') AS NUMERIC)) AS old_rub
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_ADV_COSTS_UNION_PREBOOTSTRAP` GROUP BY d)
SELECT k.d, k.mechanism, o.old_rub, n.new_rub,
       n.new_rub - o.old_rub AS actual_delta,
       k.expected_delta,
       (n.new_rub - o.old_rub = k.expected_delta) AS matches_expectation
FROM known k
LEFT JOIN new_canon n USING (d)
LEFT JOIN old_union o USING (d)
ORDER BY k.d;
-- 🔴 matches_expectation = FALSE не обязательно означает дефект: bootstrap читает WB
--    заново. Означает — разобрать, а не переписать ожидание.
