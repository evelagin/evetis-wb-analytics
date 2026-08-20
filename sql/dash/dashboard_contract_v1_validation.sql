-- ============================================================================
-- Stage 4A · PR1 — гейты приёмки C1–C16 для sql/dash/dashboard_contract_v1.sql
-- Дата: 2026-08-20.  Якорь измерений: production BigQuery 20.08.2026.
--
-- 🔴 КРИТЕРИЙ ПРИЁМКИ — СООТНОШЕНИЕ, А НЕ ЗАМОРОЖЕННАЯ КОНСТАНТА.
--    Между дизайном и выкатом приезжают новые сутки, и абсолютные числа смещаются.
--    Гейты C1, C2, C5, C6, C9, C10, C11, C13 сформулированы как инварианты и обязаны
--    держаться в любой день. Гейты C3, C4, C7, C8, C12, C14, C15, C16 содержат
--    привязку к якорю: при прогоне в другой день сверять ЗНАЧЕНИЕ с фактом того дня,
--    а не с числом из этого файла.
--
-- Каждый запрос возвращает одну строку с колонкой `verdict` = 'PASS' | 'FAIL'.
-- ============================================================================

-- C1. Плотность календаря: строк ровно столько, сколько суток между краями. ИНВАРИАНТ.
SELECT 'C1' AS gate, COUNT(*) AS rows_, DATE_DIFF(MAX(day), MIN(day), DAY) + 1 AS span_days,
       IF(COUNT(*) = DATE_DIFF(MAX(day), MIN(day), DAY) + 1
          AND COUNT(*) = COUNT(DISTINCT day), 'PASS', 'FAIL') AS verdict
FROM `wb_mart.V_DASH_COVERAGE_DAILY`;

-- C2. Ни одного NULL в булевых флагах покрытия. ИНВАРИАНТ (fail-closed).
--     finance_is_final НАМЕРЕННО исключён: его NULL означает «финансовых строк
--     за сутки нет» и является частью контракта, а не пропуском.
SELECT 'C2' AS gate,
  COUNTIF(orders_covered IS NULL OR sales_covered IS NULL OR ads_covered IS NULL
          OR finance_covered IS NULL OR stocks_covered IS NULL
          OR contribution_covered IS NULL OR contains_provisional_finance IS NULL
          OR is_current_day IS NULL OR bounds_complete IS NULL) AS null_flags,
  IF(COUNTIF(orders_covered IS NULL OR sales_covered IS NULL OR ads_covered IS NULL
          OR finance_covered IS NULL OR stocks_covered IS NULL
          OR contribution_covered IS NULL OR contains_provisional_finance IS NULL
          OR is_current_day IS NULL OR bounds_complete IS NULL) = 0, 'PASS', 'FAIL') AS verdict
FROM `wb_mart.V_DASH_COVERAGE_DAILY`;

-- C3. Границы покрытия потоков. ЯКОРЬ 20.08.2026: orders/sales/ads = 2026-04-13,
--     finance = 2024-09-05, stocks = 2026-07-16.
--     🔴 sales = 2026-04-13, а НЕ 2026-03-30: одиночная строка от 30.03 отделена
--     13 пустыми сутками и непрерывный участок не образует.
SELECT 'C3' AS gate, orders_coverage_start, sales_coverage_start, ads_coverage_start,
       finance_coverage_start, stocks_coverage_start,
       IF(orders_coverage_start = DATE '2026-04-13' AND sales_coverage_start = DATE '2026-04-13'
          AND ads_coverage_start = DATE '2026-04-13' AND finance_coverage_start = DATE '2024-09-05'
          AND stocks_coverage_start = DATE '2026-07-16', 'PASS', 'FAIL') AS verdict
FROM `wb_mart.V_DASH_COVERAGE_DAILY` LIMIT 1;

-- C4. 🔴 ГЛАВНЫЙ ГЕЙТ FAIL-CLOSED. До начала рекламного покрытия покрытых суток нет
--     НИ ОДНИХ. Именно здесь ноль витрины перестаёт выдаваться за факт. ИНВАРИАНТ.
SELECT 'C4' AS gate,
  COUNTIF(day < ads_coverage_start AND ads_covered)     AS covered_before_start,
  COUNTIF(day < ads_coverage_start)                      AS days_before_start,
  IF(COUNTIF(day < ads_coverage_start AND ads_covered) = 0, 'PASS', 'FAIL') AS verdict
FROM `wb_mart.V_DASH_COVERAGE_DAILY`;

-- C5. Внутри рекламного окна не покрыто ноль суток. ИНВАРИАНТ.
SELECT 'C5' AS gate, SUM(ads_uncovered_days) AS uncovered_in_window, COUNT(*) AS window_days,
       IF(SUM(ads_uncovered_days) = 0, 'PASS', 'FAIL') AS verdict
FROM `wb_mart.V_DASH_COVERAGE_DAILY`
WHERE day BETWEEN ads_coverage_start AND ads_coverage_end;

-- C6. Внутри своих окон не покрыто ноль суток у заказов и продаж. ИНВАРИАНТ.
SELECT 'C6' AS gate,
  SUM(IF(day BETWEEN orders_coverage_start AND orders_coverage_end, orders_uncovered_days, 0)) AS ord_uncovered,
  SUM(IF(day BETWEEN sales_coverage_start  AND sales_coverage_end,  sales_uncovered_days,  0)) AS sal_uncovered,
  IF(SUM(IF(day BETWEEN orders_coverage_start AND orders_coverage_end, orders_uncovered_days, 0)) = 0
     AND SUM(IF(day BETWEEN sales_coverage_start AND sales_coverage_end, sales_uncovered_days, 0)) = 0,
     'PASS', 'FAIL') AS verdict
FROM `wb_mart.V_DASH_COVERAGE_DAILY`;

-- C7. Финализация финансов. ЯКОРЬ: последняя FINAL-дата 2026-08-16; 17–18.08 PROVISIONAL;
--     за 19.08 финансовых строк нет вовсе.
SELECT 'C7' AS gate,
  MAX(IF(finance_is_final, day, NULL))            AS last_final_day,
  ANY_VALUE(finance_last_final_date)              AS declared_last_final,
  COUNTIF(finance_is_final IS FALSE)              AS provisional_days,
  COUNTIF(day = DATE '2026-08-19' AND finance_is_final IS NULL) AS aug19_no_finance,
  IF(MAX(IF(finance_is_final, day, NULL)) = ANY_VALUE(finance_last_final_date), 'PASS', 'FAIL') AS verdict
FROM `wb_mart.V_DASH_COVERAGE_DAILY`;

-- C8. Остатки — СНИМОК: отсутствие снимка = не покрыто. ЯКОРЬ: 18–20.07.2026, 3 суток.
SELECT 'C8' AS gate, SUM(stocks_uncovered_days) AS gap_days,
       STRING_AGG(IF(NOT stocks_covered, CAST(day AS STRING), NULL), ', ' ORDER BY day) AS gap_list,
       IF(SUM(stocks_uncovered_days) = 3, 'PASS', 'FAIL') AS verdict
FROM `wb_mart.V_DASH_COVERAGE_DAILY`
WHERE day BETWEEN stocks_coverage_start AND stocks_coverage_end;

-- C9. Шапка свежести — ровно одна строка. ИНВАРИАНТ.
SELECT 'C9' AS gate, COUNT(*) AS rows_, IF(COUNT(*) = 1, 'PASS', 'FAIL') AS verdict
FROM `wb_mart.V_DASH_FRESHNESS_HEADER`;

-- C10. Разложение по статусам полно и совпадает с источником. ИНВАРИАНТ.
SELECT 'C10' AS gate, h.layers_total, h.layers_ok, h.layers_stale, h.layers_error, h.layers_unknown_status,
  (SELECT COUNT(*) FROM `wb_mart.V_DATA_FRESHNESS`) AS source_rows,
  IF(h.layers_ok + h.layers_stale + h.layers_error + h.layers_unknown_status = h.layers_total
     AND h.layers_total = (SELECT COUNT(*) FROM `wb_mart.V_DATA_FRESHNESS`), 'PASS', 'FAIL') AS verdict
FROM `wb_mart.V_DASH_FRESHNESS_HEADER` h;

-- C11. 🔴 Шапка НЕ ПЕРЕСЧИТЫВАЕТ статус: worst_status обязан быть значением,
--      реально присутствующим в V_DATA_FRESHNESS — после нормализации NULL → 'UNKNOWN'
--      (FIX-1). Сам worst_status при этом NULL быть не может. ИНВАРИАНТ.
SELECT 'C11' AS gate, h.worst_status, h.worst_status_raw,
  IF(h.worst_status IS NOT NULL
     AND h.worst_status IN (SELECT DISTINCT IFNULL(status, 'UNKNOWN') FROM `wb_mart.V_DATA_FRESHNESS`),
     'PASS', 'FAIL') AS verdict
FROM `wb_mart.V_DASH_FRESHNESS_HEADER` h;

-- C12. Baseline «до = после»: PR1 не меняет ни одного существующего объекта.
--      ЯКОРЬ 20.08: 7 302 / 523 365,38 ₽ · 14 · 1 821 · 1 816 / 514 064,00 ₽.
SELECT 'C12' AS gate,
  (SELECT COUNT(*)                    FROM `wb_mart.MART_SKU_DAILY`)        AS mart_rows,
  (SELECT ROUND(SUM(ad_spend), 2)     FROM `wb_mart.MART_SKU_DAILY`)        AS mart_ad_spend,
  (SELECT COUNT(*)                    FROM `wb_mart.V_ADS_SCREEN_SKU`)      AS screen_sku_rows,
  (SELECT COUNT(*)                    FROM `wb_mart.V_ADS_SCREEN_QUERY`)    AS screen_query_rows,
  (SELECT COUNT(*)                    FROM `wb_mart.FACT_ADS_COSTS_DAILY`)  AS fact_costs_rows,
  (SELECT ROUND(SUM(actual_spend_rub), 2) FROM `wb_mart.FACT_ADS_COSTS_DAILY`) AS fact_costs_sum,
  'СВЕРИТЬ С BASELINE ДО ВЫКАТА' AS verdict;

-- C13. K9: текст файла в main ↔ view_definition в BigQuery (нормализация: снять
--      строки-комментарии, пустые строки, хвостовые пробелы; сравнить sha256).
SELECT 'C13' AS gate, table_name, LENGTH(view_definition) AS def_chars,
       TO_HEX(SHA256(view_definition)) AS sha256_raw
FROM `wb_mart.INFORMATION_SCHEMA.VIEWS`
WHERE table_name IN ('V_DASH_COVERAGE_DAILY', 'V_DASH_FRESHNESS_HEADER')
ORDER BY table_name;

-- ---------------------------------------------------------------------------
-- C14–C16 добавлены после ACK владельца по OPEN-1 (PROVISIONAL считается известным).
-- Новых объектов не вводят — проверяют семантику качества финансовых суток.
-- ---------------------------------------------------------------------------

-- C14. PROVISIONAL размечен и не выдаётся за FINAL. ЯКОРЬ: 2 суток (17–18.08).
SELECT 'C14' AS gate, SUM(provisional_finance_days) AS provisional_days,
  STRING_AGG(IF(contains_provisional_finance, CAST(day AS STRING), NULL), ', ' ORDER BY day) AS provisional_list,
  COUNTIF(contains_provisional_finance AND finance_is_final) AS contradiction,
  IF(COUNTIF(contains_provisional_finance AND finance_is_final) = 0, 'PASS', 'FAIL') AS verdict
FROM `wb_mart.V_DASH_COVERAGE_DAILY`;

-- C15. Контрибуция покрыта только там, где есть ВСЕ три компонента.
--      ЯКОРЬ: последний покрытый день 2026-08-18 (за 19.08 финансов ещё нет).
SELECT 'C15' AS gate,
  MAX(IF(contribution_covered, day, NULL)) AS last_contribution_day,
  SUM(IF(contribution_covered, 0, 1))      AS uncovered_days,
  COUNTIF(contribution_covered AND (NOT sales_covered OR NOT ads_covered OR finance_is_final IS NULL)) AS leak,
  IF(COUNTIF(contribution_covered AND (NOT sales_covered OR NOT ads_covered OR finance_is_final IS NULL)) = 0,
     'PASS', 'FAIL') AS verdict
FROM `wb_mart.V_DASH_COVERAGE_DAILY`;

-- C16. Требование владельца: агрегируемый признак provisional ДЛЯ ПЕРИОДА.
--      SUM(contribution_provisional_days) > 0 ⇔ период задевает provisional-сутки,
--      участвующие в контрибуции. Проверяется на трёх диапазонах.
SELECT 'C16' AS gate,
  SUM(IF(day BETWEEN DATE '2026-08-13' AND DATE '2026-08-19', contribution_provisional_days, 0)) AS last7_must_be_gt0,
  SUM(IF(day BETWEEN DATE '2026-04-13' AND DATE '2026-08-12', contribution_provisional_days, 0)) AS closed_period_must_be_0,
  SUM(contribution_provisional_days) AS all_time,
  IF(SUM(IF(day BETWEEN DATE '2026-08-13' AND DATE '2026-08-19', contribution_provisional_days, 0)) > 0
     AND SUM(IF(day BETWEEN DATE '2026-04-13' AND DATE '2026-08-12', contribution_provisional_days, 0)) = 0,
     'PASS', 'FAIL') AS verdict
FROM `wb_mart.V_DASH_COVERAGE_DAILY`;

-- ---------------------------------------------------------------------------
-- C17–C18 добавлены по FIX-1 (решение владельца 20.08.2026): NULL-статус слоя
-- обязан обрабатываться fail-closed. Новых объектов не вводят.
-- ---------------------------------------------------------------------------

-- C17. 🔴 SYNTHETIC TEST. Production не трогается: логика шапки прогоняется на
--      четырёх выдуманных слоях — OK, STALE, NULL и нераспознанный 'WEIRD'.
--      Доказывает ровно то, чего нельзя доказать на живых данных: сегодня в
--      V_DATA_FRESHNESS нет ни одного NULL-статуса, поэтому дефект трёхзначной
--      логики на production молчал бы.
--      ⚠️ Тест повторяет ВЫРАЖЕНИЯ вью, а не вызывает её (подсунуть вью другой
--      вход нельзя). Поэтому он идёт В ПАРЕ с C18, который статически доказывает,
--      что в выкаченном объекте стоят те же выражения. Урок PR #100: инструмент
--      сверки сам должен быть проверен, прежде чем его вердикт считать фактом.
WITH synth AS (
  SELECT 'a_ok'    AS layer_code, 'OK'    AS status UNION ALL
  SELECT 'b_stale',                'STALE'          UNION ALL
  SELECT 'c_null',                 CAST(NULL AS STRING) UNION ALL
  SELECT 'd_weird',                'WEIRD'
),
f AS (
  SELECT layer_code, status AS status_raw, IFNULL(status, 'UNKNOWN') AS status,
         CASE IFNULL(status, 'UNKNOWN')
           WHEN 'OK' THEN 0 WHEN 'STALE' THEN 1 WHEN 'ERROR' THEN 2 ELSE 3 END AS status_prio
  FROM synth
),
worst AS (SELECT status AS worst_status FROM f ORDER BY status_prio DESC, layer_code LIMIT 1),
h AS (
  SELECT
    (SELECT worst_status FROM worst)                                AS worst_status,
    COUNT(*)                                                        AS layers_total,
    COUNTIF(status = 'OK')                                          AS layers_ok,
    COUNTIF(status = 'STALE')                                       AS layers_stale,
    COUNTIF(status = 'ERROR')                                       AS layers_error,
    COUNTIF(status NOT IN ('OK', 'STALE', 'ERROR'))                 AS layers_unknown_status,
    STRING_AGG(IF(status NOT IN ('OK', 'STALE', 'ERROR'),
                  FORMAT('%s (%s)', layer_code, status), NULL), ', ' ORDER BY layer_code)
                                                                    AS unknown_status_layers,
    IF(COUNTIF(status <> 'OK') = 0, NULL,
       FORMAT('%d слой(ёв) не в норме: %s', COUNTIF(status <> 'OK'),
              STRING_AGG(IF(status <> 'OK', FORMAT('%s (%s)', layer_code, status), NULL), ', '
                         ORDER BY layer_code)))                     AS header_warning
  FROM f
)
SELECT 'C17' AS gate, worst_status, layers_total, layers_ok, layers_stale, layers_error,
       layers_unknown_status, unknown_status_layers, header_warning,
       IF(   layers_total = 4
         AND layers_ok = 1 AND layers_stale = 1 AND layers_error = 0
         AND layers_unknown_status = 2                                   -- NULL И 'WEIRD' оба сосчитаны
         AND layers_ok + layers_stale + layers_error + layers_unknown_status = layers_total
         AND worst_status = 'UNKNOWN'                                    -- NULL выиграл приоритет
         AND unknown_status_layers = 'c_null (UNKNOWN), d_weird (WEIRD)' -- назван, а не только сосчитан
         AND STRPOS(header_warning, 'c_null (UNKNOWN)') > 0              -- виден в предупреждении
         AND STRPOS(header_warning, 'd_weird (WEIRD)')  > 0
         AND STARTS_WITH(header_warning, '3 слой(ёв) не в норме'),       -- STALE + NULL + WEIRD
         'PASS', 'FAIL') AS verdict
FROM h;

-- C18. 🔴 СТАТИЧЕСКАЯ ПРОВЕРКА ВЫКАЧЕННОГО ТЕКСТА. Доказывает, что в BigQuery стоят
--      те же выражения, что проверил C17, и что нормализацию нельзя обойти:
--        · нормализующее выражение присутствует;
--        · `V_DATA_FRESHNESS` читается РОВНО ОДИН РАЗ — второй вход означал бы
--          вторую, ненормализованную ветку;
--        · исходное значение сохранено под `status_raw` (NULL остаётся наблюдаемым).
--      ⚠️ До выката гейт неисполним (объекта нет). Эквивалент до выката — те же три
--      проверки по тексту файла sql/dash/dashboard_contract_v1.sql.
SELECT 'C18' AS gate,
  REGEXP_CONTAINS(view_definition, r"IFNULL\(status,\s*'UNKNOWN'\)")            AS has_normalization,
  ARRAY_LENGTH(REGEXP_EXTRACT_ALL(view_definition, r"V_DATA_FRESHNESS"))        AS source_refs,
  REGEXP_CONTAINS(view_definition, r"status_raw")                              AS keeps_raw,
  IF(REGEXP_CONTAINS(view_definition, r"IFNULL\(status,\s*'UNKNOWN'\)")
     AND ARRAY_LENGTH(REGEXP_EXTRACT_ALL(view_definition, r"V_DATA_FRESHNESS")) = 1
     AND REGEXP_CONTAINS(view_definition, r"status_raw"), 'PASS', 'FAIL')      AS verdict
FROM `wb_mart.INFORMATION_SCHEMA.VIEWS`
WHERE table_name = 'V_DASH_FRESHNESS_HEADER';
