-- ============================================================================
-- PR-Mart1 — НЕГАТИВНЫЕ ТЕСТЫ каждого класса каста/парса (read-only, ничего не создаёт).
-- Каждый тест подаёт заведомо «грязный» вход и проверяет, что предикат guard'а
-- (тот же, что в ASSERT процедуры) ловит его: столбец *_caught должен быть > 0.
-- Последняя строка сводит всё: all_guards_fire = TRUE, если каждый класс сработал.
--
-- Дополнительно — сквозной негативный тест (для владельца, в песочнице):
--   1) CREATE TABLE wb_mart._neg AS SELECT * FROM `wb_raw.V_ADV_COSTS`;  -- копия
--   2) INSERT ... одну строку с updSum='1,2,3' (битое число);
--   3) точечно переписать источник процедуры на _neg и вызвать sp_bootstrap_facts —
--      ASSERT 'FACT_ADS_COSTS_DAILY: parse-QC != 0' должен ПРЕРВАТЬ прогон,
--      финальные FACT НЕ перезаписаться, lock освободиться. Затем DROP _neg.
-- ============================================================================

WITH
-- 1) money: REPLACE(',','.')→NUMERIC. '12,50' валиден; 'abc','1.2.3','10,,5' — нет.
money AS (
  SELECT COUNTIF(v IS NOT NULL AND TRIM(v)<>'' AND SAFE_CAST(REPLACE(v,',','.') AS NUMERIC) IS NULL) caught
  FROM UNNEST(['12,50','1000.00','abc','1.2.3','10,,5','']) v
),
-- 2) INT64 (quantity/nm/advert/views): '5' ок; '1.5','abc','7e3' — нет.
i64 AS (
  SELECT COUNTIF(v IS NOT NULL AND TRIM(v)<>'' AND SAFE_CAST(v AS INT64) IS NULL) caught
  FROM UNNEST(['5','0','1.5','abc','7e3','']) v
),
-- 3) DATE: PARSE_DATE('%Y-%m-%d', SUBSTR(v,1,10)). ISO-midnight ок; мусор — нет.
dt AS (
  SELECT COUNTIF(v IS NOT NULL AND TRIM(v)<>'' AND SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(v,1,10)) IS NULL) caught
  FROM UNNEST(['2026-04-13T00:00:00Z','2026-04-13','notadate','2026-13-40','']) v
),
-- 4) not-null grain: строка с NULL-ключом должна попасть в счётчик.
grain_null AS (
  SELECT COUNTIF(k IS NULL) caught FROM UNNEST(['a', CAST(NULL AS STRING), 'b']) k
),
-- 5) дедуп: дублирующийся ключ → COUNT(*) != COUNT(DISTINCT).
dedup AS (
  SELECT NOT (COUNT(*)=COUNT(DISTINCT k)) caught FROM UNNEST(['x','x','y']) k
)
SELECT
  money.caught       AS money_caught,        -- факт: 3  (abc, 1.2.3, '10,,5'→'10..5')
  i64.caught         AS int64_caught,        -- факт: 3  (1.5, abc, 7e3)
  dt.caught          AS date_caught,         -- факт: 2  (notadate, 2026-13-40)
  grain_null.caught  AS grain_null_caught,   -- факт: 1
  dedup.caught       AS dedup_caught,        -- факт: TRUE
  (money.caught>0 AND i64.caught>0 AND dt.caught>0 AND grain_null.caught>0 AND dedup.caught)
                     AS all_guards_fire      -- ожидание: TRUE
FROM money, i64, dt, grain_null, dedup;
