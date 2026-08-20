-- ============================================================================
-- EVETIS WB — Stage 3B.1. Snapshot-based canonical слой расходов + coverage.
-- Дата: 20.08.2026. Статус: на ревью, в production НЕ выполнялось.
-- Дизайн: docs/ADS_COSTS_SNAPSHOT_CONTRACT_2026-08-20.md
-- Producer: apps-script/WbAdsRawLoader.gs (журнал окон) того же change-set.
--
-- 🔴 ПЯТЬ ПРАВИЛ, КОТОРЫЕ НЕЛЬЗЯ НАРУШАТЬ
--
-- 1. Атомарная единица — ОКНО ОДНОГО РАНА: (run_id, window_index). Не ран целиком:
--    ран из пяти окон, где два упали, полным ответом не является ни в каком смысле.
--
-- 2. Окно участвует в canonical ТОЛЬКО при финальном commit-marker:
--    строка журнала со status='OK' AND http_success='true'. Строки RAW без маркера
--    остаются в таблице (append-only), но авторитетными не становятся.
--
-- 3. Идентичность записи внутри снапшота — (advertId, updTime). updSum — MUTABLE
--    АТРИБУТ. Прежний ключ включал updSum, и потому исправленная сумма выглядела
--    новой записью, а старая не умирала никогда. Проверено: (advertId, updTime)
--    уникален внутри (run_id, updDate) на всех 344 парах (20.08.2026).
--
-- 4. Для каждой даты берётся РОВНО ОДИН последний успешный снапшот. Строки разных
--    ранов и разных окон не смешиваются. Проверено: внутри одного рана дата ни разу
--    не пришла из двух окон — 0 нарушений на 344 парах.
--
-- 5. Строки с updDate ВНЕ запрошенного окна отбрасываются: их никто не запрашивал,
--    полным ответом за ту дату они не являются. Наблюдалось 3 такие строки из 5 026,
--    и именно они дали 65,00 ₽ из расхождения в 334,00 ₽.
-- ============================================================================


-- ───────────────────────────────────────────────────────────────
-- 0. Схема producer'а
--    Обе операции идемпотентны. Таблицу и колонку фактически создаёт загрузчик
--    (wbAdvBqEnsureTable_), здесь они продублированы явно: слой не должен зависеть
--    от того, успел ли кто-то запустить Apps Script.
-- ───────────────────────────────────────────────────────────────
ALTER TABLE `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_ADV_COSTS`
  ADD COLUMN IF NOT EXISTS window_index STRING;

CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_ADV_COSTS_RUNS` (
  load_ts            STRING,   -- момент записи строки журнала
  run_id             STRING,   -- тот же id, что в RAW_WB_ADV_COSTS
  source_method      STRING,   -- 'adv/v1/upd'
  window_index       STRING,   -- порядковый номер окна внутри рана
  window_completed_at STRING,  -- 🔑 ВРЕМЯ ЗАВЕРШЕНИЯ ОКНА = момент commit-marker'а.
                              --    Именно по нему упорядочиваются снапшоты. Отдельная
                              --    колонка, а не load_ts: load_ts — это когда СТРОКА
                              --    журнала записана, и если журнал когда-нибудь начнут
                              --    писать пачкой в конце рана, load_ts «уедет», а
                              --    window_completed_at останется верным.
  period_from        STRING,   -- 🔑 ЧТО МЫ ПРОСИЛИ
  period_to          STRING,
  requested_days     STRING,
  http_status        STRING,
  http_success       STRING,   -- 'true' / 'false'
  returned_rows      STRING,   -- 0 — легитимное наблюдение, ради него журнал и заводится
  returned_days      STRING,
  returned_min_date  STRING,   -- 🔑 ЧТО НАМ ОТВЕТИЛИ
  returned_max_date  STRING,
  rows_out_of_window STRING,
  duration_ms        STRING,
  status             STRING,   -- 'OK' | 'ERROR'  ← commit-marker
  error_message      STRING
);


-- ───────────────────────────────────────────────────────────────
-- 1. wb_raw.V_ADV_COSTS_UNION_LEGACY — прежнее тело, дословно.
--    Только для сверки и разбора расхождений. Не подключать к FACT.
-- ───────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_raw.V_ADV_COSTS_UNION_LEGACY` AS
SELECT * EXCEPT(_rn) FROM (
  SELECT *, ROW_NUMBER() OVER (
    PARTITION BY advertId, updTime, updSum
    ORDER BY SAFE_CAST(load_ts AS TIMESTAMP) DESC, run_id DESC
  ) AS _rn
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_ADV_COSTS`
)
WHERE _rn = 1;


-- ───────────────────────────────────────────────────────────────
-- 2. wb_raw.V_ADV_COSTS_UNION_PREBOOTSTRAP — БИЗНЕС-СЕМАНТИКА ФАЗЫ A
--    и единственный допустимый откат после bootstrap.
--
--    Прежняя union-логика, из которой исключены строки НОВЫХ КОНТУРОВ чтения:
--    ADSBACKFILL_ / ADSAUDIT_ / ADSRECHECK_. Штатные ежедневные раны (ADSRAW_)
--    продолжают попадать сюда как раньше, поэтому wb-mart-prod во время bootstrap
--    работает штатно и видит ровно те же числа, что и до миграции.
--
--    🔴 ПОЧЕМУ ИСКЛЮЧЕНИЕ ПО ПРЕФИКСУ РАНА, А НЕ ПО ВРЕМЕНИ.
--    Отсечка `load_ts < MIN(load_ts по ADSBACKFILL_)` выглядит эквивалентной, но
--    ЗАМОРАЖИВАЕТ вью на моменте старта bootstrap: все последующие ЕЖЕДНЕВНЫЕ раны
--    тоже оказались бы за отсечкой, и FACT_ADS_COSTS_DAILY тихо перестал бы
--    обновляться на все дни bootstrap — ровно тогда, когда витрина должна работать
--    штатно. Исключение по префиксу убирает вклад новых контуров и не трогает
--    production-поток.
--
--    🔴 ADSAUDIT_ и ADSRECHECK_ исключены по той же причине, что и bootstrap:
--    это перечитывание СТАРЫХ суток. Union умеет только расти, поэтому любое
--    новое чтение истории сдвинуло бы прежние числа вверх — незаметно и без гейта.
-- ───────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_raw.V_ADV_COSTS_UNION_PREBOOTSTRAP` AS
SELECT * EXCEPT(_rn) FROM (
  SELECT r.*, ROW_NUMBER() OVER (
    PARTITION BY r.advertId, r.updTime, r.updSum
    ORDER BY SAFE_CAST(r.load_ts AS TIMESTAMP) DESC, r.run_id DESC
  ) AS _rn
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_ADV_COSTS` r
  WHERE NOT (STARTS_WITH(r.run_id, 'ADSBACKFILL_')
          OR STARTS_WITH(r.run_id, 'ADSAUDIT_')
          OR STARTS_WITH(r.run_id, 'ADSRECHECK_'))
)
WHERE _rn = 1;


-- ───────────────────────────────────────────────────────────────
-- 3. wb_raw.V_ADV_COSTS_SNAPSHOT — SNAPSHOT-BASED canonical (ХЕЛПЕР).
--
--    🔴 Это НЕ production-имя. В Фазе A вью создаётся рядом с прежней семантикой,
--    чтобы обе можно было сравнить на одних и тех же данных ДО переключения.
--    Production-имя V_ADV_COSTS переключается отдельной однострочной командой (§5),
--    и только это переключение является ломающим изменением producer contract.
-- ───────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_raw.V_ADV_COSTS_SNAPSHOT` AS
WITH j0 AS (
  -- журнал; на (run_id, window_index) оставляем ПОСЛЕДНЮЮ строку: если окно
  -- переспрашивали внутри рана, авторитетен финальный маркер, а не первый.
  SELECT
    run_id,
    SAFE_CAST(window_index AS INT64)                        AS window_index,
    SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(period_from, 1, 10)) AS pf,
    SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(period_to,   1, 10)) AS pt,
    -- 🔑 HARD GATE 1: время ЗАВЕРШЕНИЯ ОКНА из commit-marker'а, НЕ row-level load_ts
    --    строк данных. Fallback на load_ts журнала — только для строк, записанных до
    --    появления колонки; в норме обе величины совпадают.
    SAFE_CAST(IFNULL(window_completed_at, load_ts) AS TIMESTAMP) AS jts,
    status, http_success,
    ROW_NUMBER() OVER (
      PARTITION BY run_id, SAFE_CAST(window_index AS INT64)
      ORDER BY SAFE_CAST(IFNULL(window_completed_at, load_ts) AS TIMESTAMP) DESC
    ) AS rn
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_ADV_COSTS_RUNS`
  WHERE source_method = 'adv/v1/upd'
),
j AS (   -- ПРАВИЛО 2: только окна с финальным commit-marker
  SELECT run_id, window_index, pf, pt, jts
  FROM j0
  WHERE rn = 1
    AND status = 'OK' AND http_success = 'true'
    AND pf IS NOT NULL AND pt IS NOT NULL AND jts IS NOT NULL
),
days AS (
  SELECT d FROM j, UNNEST(GENERATE_DATE_ARRAY(pf, pt)) d GROUP BY d
),
chosen AS (   -- ПРАВИЛО 4: одна дата — один снапшот
  SELECT d, run_id, window_index
  FROM (
    SELECT days.d, j.run_id, j.window_index,
           ROW_NUMBER() OVER (
             PARTITION BY days.d
             ORDER BY j.jts DESC, j.run_id DESC, j.window_index DESC
           ) AS rn
    FROM days JOIN j ON days.d BETWEEN j.pf AND j.pt
  )
  WHERE rn = 1
)
SELECT r.*
FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_ADV_COSTS` r
JOIN chosen c
  ON  r.run_id = c.run_id
 AND  SAFE_CAST(r.window_index AS INT64) = c.window_index
 -- ПРАВИЛО 5: строка принадлежит дате только если дата внутри запрошенного окна;
 -- c.d порождена из [pf, pt], поэтому out-of-window строки сюда не попадут вовсе.
 AND  SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(r.updDate, 1, 10)) = c.d
WHERE r.source_method = 'adv/v1/upd';


-- ───────────────────────────────────────────────────────────────
-- 4. wb_raw.V_ADV_COSTS_DAY_COVERAGE — грейн date.
--
--    🔴 billed_complete — OPERATIONAL SETTLEMENT ПО НАШЕМУ SLA, а НЕ гарантия
--    финальности WB. Мы не знаем и не заявляем, что WB больше не изменит день.
--    Заявляем ровно следующее: день был явно запрошен успешным раном, ответ
--    получен и наблюдаем (в том числе нулевой), день старше SETTLE_DAYS, и
--    последние N_STABLE независимых чтений дали идентичный набор записей.
--
--    Поздняя ревизия обрабатывается БЕЗ спецправил: новый снапшот побеждает
--    (см. §3), serie стабильности обрывается, stable_reads падает до 1,
--    billed_complete снимается сам. revision_count / revised_after_settled_at /
--    revision_delta_rub при этом сохраняются и не затираются.
--
--    Все поля выводятся ИЗ ИСТОРИИ СНАПШОТОВ, без изменяемой таблицы состояния:
--    вью воспроизводима, и «settled» нельзя проставить руками.
-- ───────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_raw.V_ADV_COSTS_DAY_COVERAGE` AS
WITH k AS (
  -- КОНСТАНТЫ КОНТРАКТА. Пересматриваются по журналу (инвариант I7), а не по памяти.
  --   SETTLE_DAYS = 15 — на сутки дальше operational-окна D−14: день объявляется
  --     закрытым только после того, как все 14 ежедневных чтений состоялись.
  --   N_STABLE = 3 — три независимых совпавших чтения. 🔴 Два было бы самообманом:
  --     перед ревизией 02.08 совпало СЕМЬ чтений подряд.
  SELECT 15 AS settle_days, 3 AS n_stable
),
j0 AS (
  SELECT
    run_id,
    SAFE_CAST(window_index AS INT64)                        AS window_index,
    SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(period_from, 1, 10)) AS pf,
    SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(period_to,   1, 10)) AS pt,
    -- 🔑 HARD GATE 1: время ЗАВЕРШЕНИЯ ОКНА из commit-marker'а, НЕ row-level load_ts
    --    строк данных. Fallback на load_ts журнала — только для строк, записанных до
    --    появления колонки; в норме обе величины совпадают.
    SAFE_CAST(IFNULL(window_completed_at, load_ts) AS TIMESTAMP) AS jts,
    status, http_success,
    ROW_NUMBER() OVER (
      PARTITION BY run_id, SAFE_CAST(window_index AS INT64)
      ORDER BY SAFE_CAST(IFNULL(window_completed_at, load_ts) AS TIMESTAMP) DESC
    ) AS rn
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_ADV_COSTS_RUNS`
  WHERE source_method = 'adv/v1/upd'
),
j AS (
  SELECT run_id, window_index, pf, pt, jts
  FROM j0
  WHERE rn = 1 AND status = 'OK' AND http_success = 'true'
    AND pf IS NOT NULL AND pt IS NOT NULL AND jts IS NOT NULL
),
raw_d AS (
  SELECT
    run_id,
    SAFE_CAST(window_index AS INT64) AS wi,
    SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(updDate, 1, 10)) AS d,
    advertId, updTime, updSum,
    SAFE_CAST(REPLACE(updSum, ',', '.') AS NUMERIC) AS v,
    -- 🔑 HARD GATE 2: НОРМАЛИЗОВАННОЕ содержимое записи для отпечатка снапшота.
    --    Приводится к канону, потому что WB сериализует одно и то же по-разному:
    --    updTime приходит с 0/4/5/6 знаками долей секунды (4 формата на 5 026 строк),
    --    и повторная сериализация того же момента иначе выглядела бы «ревизией».
    --    Парсинг проверен: 0 непарсящихся, 592 строки ↔ 592 момента, 1:1.
    --
    --    🔴 updNum В ОТПЕЧАТОК НЕ ВХОДИТ. Измерено: у 354 записей (advertId, updTime,
    --    updSum) значение updNum РАЗЛИЧАЕТСЯ между чтениями — это порядковый номер
    --    внутри ответа, а не атрибут записи. Включив его, мы получили бы ложную
    --    ревизию почти на каждом перечитывании и сломали бы весь stability-контур.
    --
    --    🔴 campName / advertType / paymentType / advertStatus тоже НЕ входят: это
    --    изменяемые измерения кампании (у 20 кампаний advertStatus менялся за историю).
    --    Переименование кампании читалось бы как ревизия биллинга.
    FORMAT('%s|%s|%s',
      IFNULL(FORMAT('%d', SAFE_CAST(advertId AS INT64)), CONCAT('raw:', IFNULL(advertId, ''))),
      IFNULL(FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E6S%Ez',
                              SAFE_CAST(updTime AS TIMESTAMP), 'Europe/Moscow'),
             CONCAT('raw:', IFNULL(updTime, ''))),
      IFNULL(FORMAT('%.2f', SAFE_CAST(REPLACE(updSum, ',', '.') AS NUMERIC)),
             CONCAT('raw:', IFNULL(updSum, '')))
    ) AS norm_record
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_ADV_COSTS`
  WHERE source_method = 'adv/v1/upd'
    AND SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(updDate, 1, 10)) IS NOT NULL
),
days_covered AS (
  SELECT d FROM j, UNNEST(GENERATE_DATE_ARRAY(pf, pt)) d GROUP BY d
),
days_all AS (
  -- 🔑 спайн шире покрытия: даты, у которых строки в RAW есть, а успешного окна нет,
  -- ОБЯЗАНЫ быть видны как not_loaded. Именно так выглядит вся история до bootstrap.
  SELECT d FROM days_covered
  UNION DISTINCT
  SELECT d FROM raw_d GROUP BY d
),
snap AS (
  SELECT
    dd.d, j.run_id, j.window_index, j.jts,
    COUNT(r.advertId)                  AS answer_rows,
    IFNULL(SUM(r.v), NUMERIC '0')      AS answer_sum_rub,
    -- 🔑 HARD GATE 2: детерминированный SHA-256 по ПОЛНОМУ нормализованному
    -- содержимому снапшота, а не по числу строк и не по сумме. Ни count, ни total
    -- не поймали бы обмен двух записей местами при неизменном итоге.
    -- Порядок сортировки задан по самому нормализованному значению — полный порядок,
    -- поэтому хеш воспроизводим при любом плане выполнения.
    -- updSum входит как АТРИБУТ записи: изменение суммы обязано читаться как ревизия,
    -- а не как появление новой записи (это и была корневая ошибка union-слоя).
    TO_HEX(SHA256(IFNULL(
      STRING_AGG(r.norm_record, '\n' ORDER BY r.norm_record), ''))) AS fingerprint
  FROM days_covered dd
  JOIN j ON dd.d BETWEEN j.pf AND j.pt
  -- LEFT JOIN обязателен: пустой успешный ответ — полноценное наблюдение,
  -- и он должен дать строку снапшота с answer_rows = 0.
  LEFT JOIN raw_d r
    ON r.run_id = j.run_id AND r.wi = j.window_index AND r.d = dd.d
  GROUP BY dd.d, j.run_id, j.window_index, j.jts
),
ord AS (
  SELECT
    s.*,
    ROW_NUMBER()          OVER w AS seq,
    LAG(fingerprint)      OVER w AS prev_fp,
    LAG(answer_sum_rub)   OVER w AS prev_sum,
    DATE_DIFF(DATE(jts, 'Europe/Moscow'), d, DAY) AS age_at_read
  FROM snap s
  WINDOW w AS (PARTITION BY d ORDER BY jts, run_id, window_index)
),
grp AS (
  SELECT o.*,
    COUNTIF(prev_fp IS NULL OR fingerprint <> prev_fp)
      OVER (PARTITION BY d ORDER BY seq ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS fp_series
  FROM ord o
),
st AS (
  SELECT g.*, k.settle_days, k.n_stable,
    COUNT(*) OVER (PARTITION BY d, fp_series ORDER BY seq
                   ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS stable_reads_at
  FROM grp g CROSS JOIN k
),
st2 AS (
  -- было ли состояние settled НА МОМЕНТ каждого чтения (симуляция, а не текущий срез)
  SELECT st.*, (stable_reads_at >= n_stable AND age_at_read >= settle_days) AS settled_at_read
  FROM st
),
st3 AS (
  SELECT st2.*, LAG(settled_at_read) OVER (PARTITION BY d ORDER BY seq) AS prev_settled
  FROM st2
),
agg AS (
  SELECT
    d,
    COUNT(*)                                       AS successful_reads,
    MAX(age_at_read)                               AS max_age_at_read,
    MIN(jts)                                       AS first_successful_read_at,
    MAX(jts)                                       AS last_successful_read_at,
    MIN(IF(settled_at_read, jts, NULL))            AS settled_first_at,
    COUNTIF(prev_fp IS NOT NULL AND fingerprint <> prev_fp) AS revision_count,
    MIN(IF(prev_fp IS NOT NULL AND fingerprint <> prev_fp AND prev_settled, jts, NULL))
                                                   AS revised_after_settled_at
  FROM st3 GROUP BY d
),
last_read AS (
  SELECT d, run_id, window_index, answer_rows, answer_sum_rub, fingerprint, stable_reads_at
  FROM st3
  QUALIFY ROW_NUMBER() OVER (PARTITION BY d ORDER BY seq DESC) = 1
),
last_change AS (
  SELECT d, answer_sum_rub - prev_sum AS revision_delta_rub
  FROM st3
  WHERE prev_fp IS NOT NULL AND fingerprint <> prev_fp
  QUALIFY ROW_NUMBER() OVER (PARTITION BY d ORDER BY seq DESC) = 1
),
base AS (
  SELECT
    da.d AS `date`,
    IFNULL(a.successful_reads, 0)                  AS successful_reads,
    a.first_successful_read_at,
    a.last_successful_read_at,
    l.run_id                                       AS chosen_run_id,
    l.window_index                                 AS chosen_window_index,
    IFNULL(l.answer_rows, 0)                       AS answer_rows,
    IFNULL(l.answer_sum_rub, NUMERIC '0')          AS answer_sum_rub,
    l.fingerprint,
    IFNULL(l.stable_reads_at, 0)                   AS stable_reads,
    IFNULL(a.revision_count, 0)                    AS revision_count,
    a.settled_first_at,
    a.revised_after_settled_at,
    c.revision_delta_rub,
    DATE_DIFF(CURRENT_DATE('Europe/Moscow'), da.d, DAY) AS age_days,
    -- requested_ok — ПО ЖУРНАЛУ, а не по наличию строк: успешный ответ с 0 строк
    -- обязан считаться наблюдением, иначе нулевой день навсегда неотличим от пропуска.
    IFNULL(a.max_age_at_read >= 1, FALSE)          AS requested_ok,
    k.settle_days, k.n_stable
  FROM days_all da
  CROSS JOIN k
  LEFT JOIN agg       a ON a.d = da.d
  LEFT JOIN last_read l ON l.d = da.d
  LEFT JOIN last_change c ON c.d = da.d
)
SELECT
  `date`,
  successful_reads,
  first_successful_read_at,
  last_successful_read_at,
  chosen_run_id,
  chosen_window_index,
  answer_rows,
  answer_sum_rub,
  fingerprint,
  stable_reads,
  revision_count,
  settled_first_at,
  revised_after_settled_at,
  revision_delta_rub,
  age_days,
  requested_ok,
  (age_days >= settle_days)                        AS age_ok,
  (stable_reads >= n_stable)                       AS stable_ok,
  (requested_ok AND age_days >= settle_days AND stable_reads >= n_stable) AS billed_complete,
  -- «успешные сутки с 0 ₽» — отдельное, ПОЛОЖИТЕЛЬНОЕ утверждение, а не молчание
  (requested_ok AND age_days >= settle_days AND stable_reads >= n_stable
     AND answer_rows = 0)                          AS zero_spend_day,
  (NOT requested_ok)                               AS not_loaded,
  -- очередь точечного перечитывания: день был закрыт, потом изменился и ещё не устоялся
  (revised_after_settled_at IS NOT NULL
     AND NOT (requested_ok AND age_days >= settle_days AND stable_reads >= n_stable))
                                                   AS needs_recheck,
  settle_days                                      AS contract_settle_days,
  n_stable                                         AS contract_n_stable
FROM base;


-- ───────────────────────────────────────────────────────────────
-- 5. wb_raw.V_ADV_COSTS — ПРОИЗВОДСТВЕННОЕ ИМЯ.
--
--    🔴 ВЫПОЛНЯЕТСЯ РОВНО ОДНА ИЗ ДВУХ КОМАНД НИЖЕ, в зависимости от фазы.
--    Переключение сделано однострочным намеренно: тело не переписывается, поэтому
--    cutover и откат — это одна и та же операция в обе стороны, а K9 сравнивает
--    строку, а не сотню.
--
--    ФАЗА A (после доказательства E1: LEGACY == UNION_PREBOOTSTRAP):
--      wb-mart-prod продолжает работать штатно. Строки контуров ADSBACKFILL_ /
--      ADSAUDIT_ / ADSRECHECK_ в бизнес-семантику не попадают, поэтому bootstrap
--      можно вести дни подряд, не останавливая витрину.
-- ───────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_raw.V_ADV_COSTS` AS
SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_ADV_COSTS_UNION_PREBOOTSTRAP`;

-- ───────────────────────────────────────────────────────────────
--    ФАЗА B (короткое окно cutover, wb-mart-prod на паузе):
--      выполнить ВМЕСТО команды Фазы A, после I1–I9 и DAY_LOST = 0.
--      Раскомментировать при cutover; в Фазе A строки остаются комментарием.
-- ───────────────────────────────────────────────────────────────
-- CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_raw.V_ADV_COSTS` AS
-- SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_ADV_COSTS_SNAPSHOT`;
