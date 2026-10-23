-- ============================================================================
-- EVETIS WB — снапшот-слой расходов рекламы, v2: граница суток UTC в /adv/v1/upd.
-- Дата: 21.10.2026. Статус: ЧЕРНОВИК на ревью. В production НЕ выполнялось.
-- Применение — только по OWNER ACK (меняет рекламу Executive P&L на +137 ₽, см. ниже).
-- Предыдущая версия (действует, live == repo 21.10.2026): sql/raw/adv_costs_snapshot_v1.sql §3–§4.
-- Откат: выполнить §3 и §4 из adv_costs_snapshot_v1.sql без изменений (V_ADV_COSTS не меняется:
--        он остаётся `SELECT * FROM V_ADV_COSTS_SNAPSHOT`).
-- Проверка до и после: sql/raw/adv_costs_snapshot_v2_validation.sql (только чтение).
--
-- ЧТО НАЙДЕНО (21.10.2026, только чтение BQ):
--   WB отдаёт окно from..to в границах суток UTC = [from 03:00 МСК, to+1 03:00 МСК).
--   • первый день окна теряет записи 00:00–03:00 МСК: из 57 таких прочтений 6 ниже остальных
--     прочтений той же даты, ни одно не выше; прочтения НЕ на первом дне совпали 570/570;
--   • последний день окна получает «хвост» следующей даты (12 строк с updDate > period_to):
--     ровно недостающие записи. Правило 5 v1 отбрасывало их как «никем не запрошенные» —
--     это и были «3 строки / 65 ₽» из шапки v1.
--   Ежедневный ADSRAW (окно D−14…D−1) делает последнее прочтение каждой даты краевым, поэтому
--   v1 выбирала неполный снапшот и рвала ряд стабильности: 03/18/19/24.09 навсегда
--   stable_reads = 1, а 12.07 (только краевые прочтения bootstrap) занижен при billed_complete.
--
-- ПРАВИЛО v2: прочтение даты d раном R полное, только если R запросил и d−1. Строки d — из окна d
--   плюс хвост из окна d−1 того же рана. Краевые прочтения не участвуют в выборе и стабильности,
--   если у даты есть полное; иначе fallback на краевые (read_basis = 'EDGE_ONLY').
--
-- КОНТРОЛЬНЫЕ СУММЫ (симуляция по всей истории 13.04–21.10.2026, 212 дат):
--   меняются ровно 5 дат, все вверх, итого +137 ₽; остальные 207 — без изменений:
--     2026-07-12  3 986 → 4 094  (+108)
--     2026-09-03  1 256 → 1 258  (+2)
--     2026-09-18  2 082 → 2 085  (+3)
--     2026-09-19  2 381 → 2 394  (+13)
--     2026-09-24  1 120 → 1 131  (+11)
--   03/18/19/24.09 становятся billed_complete; 13.04 остаётся на краевых (EDGE_ONLY).
-- ============================================================================


-- ───────────────────────────────────────────────────────────────
-- §3 v2. wb_raw.V_ADV_COSTS_SNAPSHOT
-- ───────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_raw.V_ADV_COSTS_SNAPSHOT` AS
WITH j0 AS (
  SELECT
    run_id,
    SAFE_CAST(window_index AS INT64)                        AS window_index,
    SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(period_from, 1, 10)) AS pf,
    SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(period_to,   1, 10)) AS pt,
    SAFE_CAST(IFNULL(window_completed_at, load_ts) AS TIMESTAMP) AS jts,
    status, http_success,
    ROW_NUMBER() OVER (
      PARTITION BY run_id, SAFE_CAST(window_index AS INT64)
      ORDER BY SAFE_CAST(IFNULL(window_completed_at, load_ts) AS TIMESTAMP) DESC
    ) AS rn
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_ADV_COSTS_RUNS`
  WHERE source_method = 'adv/v1/upd'
),
j AS (   -- ПРАВИЛО 2 (без изменений): только окна с финальным commit-marker
  SELECT run_id, window_index, pf, pt, jts
  FROM j0
  WHERE rn = 1
    AND status = 'OK' AND http_success = 'true'
    AND pf IS NOT NULL AND pt IS NOT NULL AND jts IS NOT NULL
),
cov AS (
  SELECT j.run_id, j.window_index, j.jts, dd AS d
  FROM j, UNNEST(GENERATE_DATE_ARRAY(j.pf, j.pt)) dd
),
reads AS (
  SELECT c.d, c.run_id, c.window_index, c.jts,
         p.window_index         AS prev_window_index,
         (p.run_id IS NOT NULL) AS is_full
  FROM cov c
  LEFT JOIN cov p ON p.run_id = c.run_id AND p.d = DATE_SUB(c.d, INTERVAL 1 DAY)
),
chosen AS (   -- ПРАВИЛО 4 (v2): одна дата — одно ПОЛНОЕ прочтение; краевое — только если полного нет
  SELECT d, run_id, window_index, prev_window_index, is_full
  FROM reads
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY d ORDER BY is_full DESC, jts DESC, run_id DESC, window_index DESC
  ) = 1
),
raw AS (
  SELECT r.*,
         SAFE_CAST(r.window_index AS INT64)                  AS _wi,
         SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(r.updDate, 1, 10)) AS _d
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_ADV_COSTS` r
  WHERE r.source_method = 'adv/v1/upd'
)
-- ПРАВИЛО 5 (v2): строка принадлежит дате d, только если ран ЗАПРОСИЛ d (c.d порождена из
-- покрытия рана). Строки окна самой даты — все, как в v1.
SELECT r.* EXCEPT (_wi, _d)
FROM raw r
JOIN chosen c ON r.run_id = c.run_id AND r._wi = c.window_index AND r._d = c.d
UNION ALL
-- …плюс хвост даты из окна d−1 того же рана: ночные (00:00–03:00 МСК) записи d, которые WB
-- отдал в границах суток UTC. Только записи, которых нет в окне даты.
SELECT r.* EXCEPT (_wi, _d)
FROM raw r
JOIN chosen c ON r.run_id = c.run_id AND r._wi = c.prev_window_index AND r._d = c.d
WHERE c.is_full AND c.prev_window_index != c.window_index
  AND NOT EXISTS (SELECT 1 FROM raw x
                  WHERE x.run_id = c.run_id AND x._wi = c.window_index AND x._d = c.d
                    -- идентичность записи — нормализованная (WB сериализует updTime с 0/4/5/6 знаками
                    -- долей секунды): иначе один момент в двух сериализациях посчитался бы дважды
                    AND COALESCE(CAST(SAFE_CAST(x.advertId AS INT64) AS STRING), x.advertId)
                      = COALESCE(CAST(SAFE_CAST(r.advertId AS INT64) AS STRING), r.advertId)
                    AND COALESCE(CAST(SAFE_CAST(x.updTime AS TIMESTAMP) AS STRING), x.updTime)
                      = COALESCE(CAST(SAFE_CAST(r.updTime AS TIMESTAMP) AS STRING), r.updTime));


-- ───────────────────────────────────────────────────────────────
-- §4 v2. wb_raw.V_ADV_COSTS_DAY_COVERAGE — прежние 24 колонки + read_basis, edge_reads_ignored
-- ───────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_raw.V_ADV_COSTS_DAY_COVERAGE` AS
WITH k AS (
  -- КОНСТАНТЫ КОНТРАКТА. Пересматриваются по журналу (инвариант I7), а не по памяти.
  --   SETTLE_DAYS = 15 — на сутки дальше operational-окна D−14: день объявляется
  --     закрытым только после того, как все 14 ежедневных чтений состоялись.
  --     v2: из них в ряд стабильности входят 13 полных (D−1…D−13); чтение D−14 краевое.
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
-- 🔴 v2 (21.10.2026): ПРОЧТЕНИЕ = (дата × ран), а не (дата × окно).
--    WB `/adv/v1/upd` режет окно по суткам UTC: первый день окна теряет записи 00:00–03:00 МСК,
--    а последний получает их «хвостом» за следующую дату. Прочтение даты d раном R ПОЛНОЕ, только
--    если R запросил и d−1: тогда ночные записи d лежат в окне R, покрывающем d−1 (или в том же
--    окне, если d внутри него). Краевое прочтение (R не покрыл d−1) — неполное по построению и в
--    ряд стабильности не входит, если у даты есть хоть одно полное. Только краевые — fallback,
--    read_basis = 'EDGE_ONLY' (начало истории 13.04.2026).
cov AS (   -- дата → окно рана, в котором она запрошена (внутри рана дата ровно в одном окне)
  SELECT j.run_id, j.window_index, j.jts, dd AS d
  FROM j, UNNEST(GENERATE_DATE_ARRAY(j.pf, j.pt)) dd
),
reads AS (
  SELECT c.d, c.run_id, c.window_index, c.jts,
         p.window_index           AS prev_window_index,
         (p.run_id IS NOT NULL)   AS is_full
  FROM cov c
  LEFT JOIN cov p ON p.run_id = c.run_id AND p.d = DATE_SUB(c.d, INTERVAL 1 DAY)
),
basis AS (
  SELECT d, LOGICAL_OR(is_full) AS has_full, COUNTIF(NOT is_full) AS edge_reads,
         -- requested_ok — как в v1, по ЛЮБОМУ успешному прочтению: краевое прочтение тоже доказывает,
         -- что дату запрашивали (иначе полное прочтение возраста 0 при старом краевом дало бы not_loaded)
         MAX(DATE_DIFF(DATE(jts, 'Europe/Moscow'), d, DAY)) AS max_age_any_read
  FROM reads GROUP BY d
),
read_rows AS (
  -- строки прочтения: окно самой даты + хвост даты из окна d−1 ТОГО ЖЕ рана.
  -- Запись хвоста берётся, только если её (advertId, updTime) нет в окне даты:
  -- дубли между окнами не возникают, а дубли ВНУТРИ окна, как и в v1, не схлопываются.
  SELECT rd.d, rd.run_id, rd.window_index, r.advertId, r.updTime, r.v, r.norm_record
  FROM reads rd
  JOIN raw_d r ON r.run_id = rd.run_id AND r.wi = rd.window_index AND r.d = rd.d
  UNION ALL
  SELECT rd.d, rd.run_id, rd.window_index, r.advertId, r.updTime, r.v, r.norm_record
  FROM reads rd
  JOIN raw_d r ON r.run_id = rd.run_id AND r.wi = rd.prev_window_index AND r.d = rd.d
  WHERE rd.is_full AND rd.prev_window_index != rd.window_index
    AND NOT EXISTS (SELECT 1 FROM raw_d x
                    WHERE x.run_id = rd.run_id AND x.wi = rd.window_index AND x.d = rd.d
                      AND COALESCE(CAST(SAFE_CAST(x.advertId AS INT64) AS STRING), x.advertId)
                        = COALESCE(CAST(SAFE_CAST(r.advertId AS INT64) AS STRING), r.advertId)
                      AND COALESCE(CAST(SAFE_CAST(x.updTime AS TIMESTAMP) AS STRING), x.updTime)
                        = COALESCE(CAST(SAFE_CAST(r.updTime AS TIMESTAMP) AS STRING), r.updTime))
),
days_covered AS (
  SELECT d FROM cov GROUP BY d
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
    rd.d, rd.run_id, rd.window_index, rd.jts,
    COUNT(rr.advertId)                 AS answer_rows,
    IFNULL(SUM(rr.v), NUMERIC '0')     AS answer_sum_rub,
    -- 🔑 HARD GATE 2 (без изменений): SHA-256 по полному нормализованному содержимому прочтения.
    TO_HEX(SHA256(IFNULL(
      STRING_AGG(rr.norm_record, '\n' ORDER BY rr.norm_record), ''))) AS fingerprint
  FROM reads rd
  JOIN basis b ON b.d = rd.d
  -- LEFT JOIN обязателен: пустой успешный ответ — полноценное наблюдение (answer_rows = 0).
  -- ключ прочтения полный (дата × ран × окно даты): даже при перекрытии окон внутри рана
  -- (сейчас 0, проверка Q8) строки разных прочтений не смешаются
  LEFT JOIN read_rows rr ON rr.d = rd.d AND rr.run_id = rd.run_id AND rr.window_index = rd.window_index
  WHERE rd.is_full OR NOT b.has_full
  GROUP BY rd.d, rd.run_id, rd.window_index, rd.jts
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
    IFNULL(bb.max_age_any_read >= 1, FALSE)        AS requested_ok,
    k.settle_days, k.n_stable,
    -- v2: какие прочтения легли в ряд стабильности (NULL — дата не запрошена ни одним раном)
    CASE WHEN bb.d IS NULL THEN CAST(NULL AS STRING) WHEN bb.has_full THEN 'FULL' ELSE 'EDGE_ONLY' END AS read_basis,
    IF(bb.has_full, bb.edge_reads, 0)              AS edge_reads_ignored
  FROM days_all da
  CROSS JOIN k
  LEFT JOIN agg       a ON a.d = da.d
  LEFT JOIN last_read l ON l.d = da.d
  LEFT JOIN last_change c ON c.d = da.d
  LEFT JOIN basis     bb ON bb.d = da.d
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
  n_stable                                         AS contract_n_stable,
  -- v2: новые колонки — строго в конце, прежние 24 колонки не меняют имён и порядка
  read_basis,
  edge_reads_ignored
FROM base;
