-- ============================================================================
-- Stage 4A · PR1 — Dashboard Contract v1. Два вью presentation-слоя.
-- Дата: 2026-08-20.  Контракт: docs/STAGE4A_DASHBOARD_CONTRACT_2026-08-20.md.
-- Предшественник: docs/STAGE4_LOOKER_MVP_READINESS_AUDIT_2026-08-20.md (принят владельцем).
--
-- ЧТО ЭТО. Фундамент fail-closed семантики дашборда. От `V_DASH_COVERAGE_DAILY`
-- зависит NULL-логика всех остальных вью контракта (KPI, SKU, когорта, реклама,
-- остатки, поставки), поэтому она выкатывается первой и отдельно.
--
-- 🔴 ПРИНЦИП. Витрина `MART_SKU_DAILY` — DENSE spine: пропуски источников заполнены
-- нулём (`IFNULL(...,0)`). За 585 суток до 2026-04-13 она отдаёт `ad_spend = 0`,
-- хотя рекламных данных за те сутки не существует и не будет. Ноль там — не факт,
-- а артефакт построения. Эта вью — единственный объект, который знает, где ноль
-- является утверждением, а где — незнанием.
--
-- ЧТО ЭТИ ДВА ВЬЮ СОЗНАТЕЛЬНО НЕ ДЕЛАЮТ
--   1. Не меняют `MART_SKU_DAILY`, FACT, загрузчики, job'ы, heartbeat, Scheduler.
--   2. Не меняют и не читают на запись `V_DATA_FRESHNESS` — только `SELECT`.
--   3. Не трогают `V_ADS_SCREEN_SKU` / `V_ADS_SCREEN_QUERY` / `V_ADS_FUNNEL_*`.
--   4. НЕ читают `V_ADV_COSTS_SNAPSHOT` / `V_ADV_COSTS_DAY_COVERAGE` и не содержат
--      биллинговых флагов: MVP не связан со Stage 3B.1 Фазой A ни одной ссылкой.
--   5. Не содержат ни одной ratio-колонки и ни одного `IFNULL(..., 0)` для метрик.
--   6. Не пересчитывают статус свежести — агрегируют готовый (контракт 1, §1.9).
--   7. Не используют `built_at` как гейт свежести — только как справочную
--      колонку (контракт 1, §1.6).
--
-- 🔴 СЕМАНТИКА ПОКРЫТИЯ — ДВА РАЗНЫХ ПРАВИЛА, ИХ НЕЛЬЗЯ СМЕШИВАТЬ
--
--   ПОТОК (orders / sales / ads / finance). Покрытие = максимальный НЕПРЕРЫВНЫЙ
--   участок суток, заканчивающийся последними наблюдёнными сутками (islands по
--   `DATE_SUB(d, ROW_NUMBER())`). Внутри участка отсутствие строк за сутки означало бы
--   «событий не было» — настоящий ноль. Причина брать непрерывный участок, а не
--   `MIN(date)`: у `FACT_SALES` есть одиночная строка от 2026-03-30, после которой
--   13 суток пусты, и лишь с 2026-04-13 ряд сплошной. `MIN(date) = 2026-03-30`
--   объявил бы покрытыми 14 суток, которых загрузчик не видел.
--   ⚠️ ПРИНЯТОЕ КОНСЕРВАТИВНОЕ СМЕЩЕНИЕ: настоящие сутки с нулём событий разорвут
--   участок и сократят объявленное покрытие. Ошибка направлена в безопасную сторону
--   (покрытие занижается, KPI гасится в NULL, а не завышается). Уточнение по журналам
--   `INGEST_RUNS` — отдельная задача, в PR1 сознательно не вносится.
--
--   СНИМОК (stocks). Покрытие = наличие снимка ЗА ЭТИ СУТКИ. Отсутствие снимка не
--   означает нулевой остаток — оно означает, что остаток неизвестен. Поэтому
--   островной анализ здесь неприменим: 18–20.07.2026 обязаны быть НЕ покрыты.
--
-- 🔴 ФИНАНСЫ — ДВА РАЗНЫХ ПРИЗНАКА (решение владельца OPEN-1, ACK 20.08.2026)
--   finance_covered              — сутки внутри непрерывного финансового участка.
--   finance_is_final             — NULL, если финансовых строк за сутки нет;
--                                  TRUE, если ВСЕ строки суток `FINAL`;
--                                  FALSE, если есть хоть одна не-`FINAL`.
--                                  Неизвестный статус ⇒ FALSE (fail-closed).
--   Правило владельца: нет финансовых данных → contribution = NULL;
--   PROVISIONAL → значение разрешено, но `finance_is_final = FALSE`.
--
-- 🔴 АГРЕГИРУЕМОСТЬ ДЛЯ ПРОИЗВОЛЬНОГО DATE RANGE
--   Булев флаг Looker сложить не может, поэтому каждый признак продублирован
--   счётчиком 1/0 (`*_days`), который корректно суммируется по любому диапазону.
--   `contribution_provisional_days` считает ТОЛЬКО те сутки, которые реально
--   участвуют в контрибуции: `SUM(contribution_provisional_days) > 0` и есть
--   ответ на вопрос «содержит ли выбранный период хотя бы один provisional-день».
--
-- Откат: DROP VIEW обоих объектов. Существующие объекты не изменяются.
-- ============================================================================

CREATE OR REPLACE VIEW `wb_mart.V_DASH_COVERAGE_DAILY` AS
WITH
-- 1. Наблюдённые сутки каждого источника.
obs_orders AS (SELECT DISTINCT order_date    AS d FROM `wb_mart.FACT_ORDERS`),
obs_sales  AS (SELECT DISTINCT sale_date     AS d FROM `wb_mart.FACT_SALES`),
obs_ads    AS (SELECT DISTINCT `date`        AS d FROM `wb_mart.FACT_ADS_SKU_DAILY`),
obs_fin    AS (SELECT DISTINCT finance_date  AS d FROM `wb_mart.FACT_FINANCE`),
obs_stocks AS (SELECT DISTINCT snapshot_date AS d FROM `wb_mart.FACT_STOCKS_SNAPSHOT`),

-- 2. Островной анализ ТОЛЬКО для потоковых источников (см. шапку).
flow AS (
  SELECT 'orders'  AS s, d FROM obs_orders
  UNION ALL SELECT 'sales',   d FROM obs_sales
  UNION ALL SELECT 'ads',     d FROM obs_ads
  UNION ALL SELECT 'finance', d FROM obs_fin
),
flow_isl  AS (
  SELECT s, d, DATE_SUB(d, INTERVAL ROW_NUMBER() OVER (PARTITION BY s ORDER BY d) DAY) AS grp
  FROM flow
),
flow_runs AS (SELECT s, MIN(d) AS run_start, MAX(d) AS run_end FROM flow_isl GROUP BY s, grp),
flow_last AS (
  SELECT s, run_start, run_end FROM (
    SELECT s, run_start, run_end, ROW_NUMBER() OVER (PARTITION BY s ORDER BY run_end DESC) AS rn
    FROM flow_runs
  ) WHERE rn = 1
),
bounds AS (
  SELECT
    MAX(IF(s = 'orders',  run_start, NULL)) AS orders_coverage_start,
    MAX(IF(s = 'orders',  run_end,   NULL)) AS orders_coverage_end,
    MAX(IF(s = 'sales',   run_start, NULL)) AS sales_coverage_start,
    MAX(IF(s = 'sales',   run_end,   NULL)) AS sales_coverage_end,
    MAX(IF(s = 'ads',     run_start, NULL)) AS ads_coverage_start,
    MAX(IF(s = 'ads',     run_end,   NULL)) AS ads_coverage_end,
    MAX(IF(s = 'finance', run_start, NULL)) AS finance_coverage_start,
    MAX(IF(s = 'finance', run_end,   NULL)) AS finance_coverage_end
  FROM flow_last
),
stock_bounds   AS (SELECT MIN(d) AS stocks_coverage_start, MAX(d) AS stocks_coverage_end FROM obs_stocks),

-- 3. Качество финансовых суток. Неизвестный статус ⇒ не FINAL (fail-closed).
fin_day        AS (
  SELECT finance_date AS d, LOGICAL_AND(finance_status = 'FINAL') AS all_final
  FROM `wb_mart.FACT_FINANCE` GROUP BY finance_date
),
fin_last_final AS (SELECT MAX(d) AS finance_last_final_date FROM fin_day WHERE all_final),

-- 4. Справочная привязка к сборке витрины (НЕ гейт свежести — контракт 1 §1.6).
mart AS (SELECT MAX(build_as_of_date) AS mart_build_as_of_date FROM `wb_mart.MART_SKU_DAILY`),

scope AS (
  SELECT
    b.*, sb.*, flf.finance_last_final_date, m.mart_build_as_of_date,
    CURRENT_DATE('Europe/Moscow') AS today,
    -- NULL-safe минимум: пустой источник даёт NULL и не роняет остальные (та же
    -- ловушка GREATEST/LEAST, что закрывалась в pr_mart2b REV4).
    (SELECT MIN(x) FROM UNNEST([b.orders_coverage_start, b.sales_coverage_start,
                                b.ads_coverage_start, b.finance_coverage_start,
                                sb.stocks_coverage_start]) AS x) AS calendar_start,
    (b.orders_coverage_start  IS NOT NULL AND b.sales_coverage_start IS NOT NULL
     AND b.ads_coverage_start IS NOT NULL AND b.finance_coverage_start IS NOT NULL
     AND sb.stocks_coverage_start IS NOT NULL) AS bounds_complete
  FROM bounds b, stock_bounds sb, fin_last_final flf, mart m
),

-- 5. Плотный календарь.
--    🔴 Точное поведение при пустом источнике (исправлено 20.08.2026, FIX-2):
--    `MIN(x) FROM UNNEST([...])` игнорирует NULL. Поэтому если пуст ОДИН источник,
--    а остальные непусты, календарь продолжает строиться от минимальной известной
--    даты; у пустого источника `*_coverage_start/end` остаются NULL, выражение
--    `day BETWEEN NULL AND NULL` даёт NULL, `COALESCE(..., FALSE)` превращает его
--    в FALSE — то есть сутки объявляются НЕ покрытыми этим источником, а
--    `bounds_complete = FALSE` делает неполноту границ видимой. Это и есть
--    fail-closed: пустой источник ничего не покрывает и молчать об этом не может.
--    Ноль строк вью отдаёт только в одном случае — когда пусты ВСЕ ПЯТЬ источников
--    и `calendar_start` не определён вовсе.
cal AS (SELECT day FROM scope, UNNEST(GENERATE_DATE_ARRAY(scope.calendar_start, scope.today)) AS day),

daily AS (
  SELECT
    cal.day,
    s.* EXCEPT (calendar_start, today),
    COALESCE(cal.day BETWEEN s.orders_coverage_start  AND s.orders_coverage_end,  FALSE) AS orders_covered,
    COALESCE(cal.day BETWEEN s.sales_coverage_start   AND s.sales_coverage_end,   FALSE) AS sales_covered,
    COALESCE(cal.day BETWEEN s.ads_coverage_start     AND s.ads_coverage_end,     FALSE) AS ads_covered,
    COALESCE(cal.day BETWEEN s.finance_coverage_start AND s.finance_coverage_end, FALSE) AS finance_covered,
    (os.d IS NOT NULL)                       AS stocks_covered,
    fd.all_final                             AS finance_is_final,
    COALESCE(NOT fd.all_final, FALSE)        AS contains_provisional_finance,
    -- Контрибуция: выкупы + реклама + ФАКТИЧЕСКОЕ наличие финансовых строк.
    -- Именно наличие, а не интервал — правило владельца «нет данных → NULL».
    (COALESCE(cal.day BETWEEN s.sales_coverage_start AND s.sales_coverage_end, FALSE)
     AND COALESCE(cal.day BETWEEN s.ads_coverage_start AND s.ads_coverage_end, FALSE)
     AND fd.d IS NOT NULL)                   AS contribution_covered,
    (cal.day = s.today)                      AS is_current_day
  FROM cal
  CROSS JOIN scope s
  LEFT JOIN fin_day    fd ON fd.d = cal.day
  LEFT JOIN obs_stocks os ON os.d = cal.day
)
SELECT
  d.*,
  -- Аддитивные счётчики: единственный способ дать Looker ответ по произвольному
  -- Date Range, потому что булев флаг он сложить не может.
  1                                                                  AS days_total,
  IF(d.orders_covered,  0, 1)                                        AS orders_uncovered_days,
  IF(d.sales_covered,   0, 1)                                        AS sales_uncovered_days,
  IF(d.ads_covered,     0, 1)                                        AS ads_uncovered_days,
  IF(d.finance_covered, 0, 1)                                        AS finance_uncovered_days,
  IF(d.stocks_covered,  0, 1)                                        AS stocks_uncovered_days,
  IF(d.contribution_covered, 0, 1)                                   AS contribution_uncovered_days,
  IF(d.contains_provisional_finance, 1, 0)                           AS provisional_finance_days,
  IF(d.contribution_covered AND d.contains_provisional_finance, 1, 0) AS contribution_provisional_days,
  CURRENT_TIMESTAMP()                                                AS generated_at
FROM daily d;


-- ============================================================================
-- V_DASH_FRESHNESS_HEADER — скалярная шапка «Данные актуальны на …».
--
-- 🔴 Вью НЕ вычисляет статус слоя. Она берёт готовый `status` из `V_DATA_FRESHNESS`
--    и только сворачивает 13 строк в одну. Приоритет: OK < STALE < ERROR < прочее.
--    Неизвестный статус получает наивысший приоритет (fail-closed, контракт 1 §1.9
--    `UNMAPPED` → `ERROR`), и `worst_status` при этом остаётся ЗНАЧЕНИЕМ ИЗ ИСТОЧНИКА,
--    а не выдуманной строкой.
--
-- 🔴 NULL-СТАТУС (исправлено 20.08.2026, FIX-1). Трёхзначная логика SQL: `NULL <> 'OK'`
--    и `NULL NOT IN (...)` дают NULL, а не TRUE, поэтому `COUNTIF` их МОЛЧА ПРОПУСКАЕТ.
--    Слой с NULL-статусом провалился бы во все счётчики сразу: не попал бы ни в `ok`,
--    ни в `stale`, ни в `error`, ни в `unknown`, и не появился бы в предупреждении —
--    то есть исчез бы с экрана, оставаясь сломанным. Это ровно тот отказ, который
--    контракт обязан ловить. Поэтому КАЖДОЕ сравнение статуса идёт через
--    `IFNULL(status, 'UNKNOWN')`, а не через голый `status`.
--    Следствия, закреплённые гейтами C17/C18:
--      · NULL и любой нераспознанный статус попадают в `layers_unknown_status`;
--      · они ненормальны для `header_warning` и печатаются там как `UNKNOWN`;
--      · они получают наивысший приоритет (`status_prio = 3`) и выигрывают `worst_status`;
--      · `worst_status` НИКОГДА не NULL; исходное значение сохранено в `worst_status_raw`.
--    ⚠️ `V_DATA_FRESHNESS` при этом НЕ меняется — правка живёт только здесь.
-- 🔴 `layers_without_sla` — слои, у которых порог осознанно не задан
--    (`success_age_is_sla` FALSE или NULL). Интерфейс обязан красить их часы серым,
--    а не зелёным: это измерение, а не обещание (контракт 1 §1.8).
-- 🔴 `mart_built_at` — СПРАВОЧНАЯ колонка. Гейтом свежести не является (§1.6).
-- Детальная плашка на экране — прямой pass-through `V_DATA_FRESHNESS`, 13 строк.
-- ============================================================================

CREATE OR REPLACE VIEW `wb_mart.V_DASH_FRESHNESS_HEADER` AS
WITH
f AS (
  SELECT
    layer_code, layer_group, data_as_of, success_age_is_sla,
    status AS status_raw,
    -- FIX-1: ЕДИНСТВЕННАЯ точка нормализации статуса во всей вью. Имя `status`
    -- ниже по тексту всегда означает уже нормализованное значение, поэтому
    -- сравнение с NULL физически невозможно обойти. Исходное значение источника
    -- сохранено под именем `status_raw` и используется ровно один раз —
    -- в `worst_status_raw`, чтобы NULL остался наблюдаемым, а не подменённым.
    IFNULL(status, 'UNKNOWN') AS status,
    CASE IFNULL(status, 'UNKNOWN')
      WHEN 'OK' THEN 0 WHEN 'STALE' THEN 1 WHEN 'ERROR' THEN 2 ELSE 3 END AS status_prio
  FROM `wb_mart.V_DATA_FRESHNESS`
),
worst AS (
  SELECT status AS worst_status, status_raw AS worst_status_raw
  FROM f ORDER BY status_prio DESC, layer_code LIMIT 1
),
m     AS (
  SELECT MAX(build_as_of_date) AS mart_build_as_of_date, MAX(built_at) AS mart_built_at
  FROM `wb_mart.MART_SKU_DAILY`
)
SELECT
  (SELECT MIN(data_as_of) FROM f)                                   AS data_as_of_min,
  (SELECT MAX(data_as_of) FROM f)                                   AS data_as_of_max,
  (SELECT worst_status     FROM worst)                              AS worst_status,
  (SELECT worst_status_raw FROM worst)                              AS worst_status_raw,
  COUNT(*)                                                          AS layers_total,
  COUNTIF(status = 'OK')                                            AS layers_ok,
  COUNTIF(status = 'STALE')                                         AS layers_stale,
  COUNTIF(status = 'ERROR')                                         AS layers_error,
  COUNTIF(status NOT IN ('OK', 'STALE', 'ERROR'))                   AS layers_unknown_status,
  COUNTIF(success_age_is_sla IS NOT TRUE)                           AS layers_without_sla,
  STRING_AGG(IF(status = 'STALE', layer_code, NULL), ', ' ORDER BY layer_code) AS stale_layers,
  STRING_AGG(IF(status = 'ERROR', layer_code, NULL), ', ' ORDER BY layer_code) AS error_layers,
  -- FIX-1: слой с NULL/нераспознанным статусом обязан быть НАЗВАН, а не просто сосчитан.
  STRING_AGG(IF(status NOT IN ('OK', 'STALE', 'ERROR'),
                FORMAT('%s (%s)', layer_code, status), NULL), ', ' ORDER BY layer_code)
                                                                    AS unknown_status_layers,
  STRING_AGG(IF(success_age_is_sla IS NOT TRUE, layer_code, NULL), ', ' ORDER BY layer_code)
                                                                    AS layers_without_sla_list,
  (SELECT mart_build_as_of_date FROM m)                             AS mart_build_as_of_date,
  (SELECT mart_built_at         FROM m)                             AS mart_built_at,
  FORMAT('Данные актуальны на %s · витрина собрана %s МСК',
         COALESCE(FORMAT_DATE('%d.%m.%Y', (SELECT MIN(data_as_of) FROM f)), '—'),
         COALESCE(FORMAT_TIMESTAMP('%d.%m %H:%M', (SELECT mart_built_at FROM m), 'Europe/Moscow'), '—'))
                                                                    AS header_text,
  -- FIX-1: `status` здесь — нормализованное значение, поэтому слой с NULL-статусом
  -- попадает в предупреждение и печатается как «layer_code (UNKNOWN)».
  IF(COUNTIF(status <> 'OK') = 0, NULL,
     FORMAT('%d слой(ёв) не в норме: %s',
            COUNTIF(status <> 'OK'),
            STRING_AGG(IF(status <> 'OK', FORMAT('%s (%s)', layer_code, status), NULL), ', '
                       ORDER BY layer_code)))                       AS header_warning,
  CURRENT_TIMESTAMP()                                               AS generated_at
FROM f;
