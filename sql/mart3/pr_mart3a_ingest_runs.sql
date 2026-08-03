-- ============================================================================
-- PR-Mart3a — технический журнал загрузчиков + единый heartbeat-вью.
-- Дата: 2026-07-31.  Дизайн: docs/MART3_ORCHESTRATION_DESIGN_2026-07-31.md (REV4, блок 1).
-- PR-нота: docs/MART_PR3A_INGEST_RUNS_2026-07-31.md.
--
-- ЗАЧЕМ. Freshness-gate Mart3 (fail-closed) обязан доказывать ФАКТ УСПЕШНОГО ЗАПУСКА
-- загрузчика за целевые сутки, а не наличие данных. `MAX(RAW.loaded_at)` для этого не годится:
--   1) zero-row дыра: успешный ран с 0 новых строк не пишет в RAW → MAX не двигается → ложный FAIL;
--   2) RAW.loaded_at/load_ts — STRING '2026-07-31 11:31:47' БЕЗ таймзоны (ISO-парсер даёт NULL);
--   3) журнала у Apps Script-загрузчиков orders/sales/ads нет вовсе
--      (wb_raw.LOADER_RUNS содержит только Cloud Run shadow/stocks).
--
-- МОДЕЛЬ ЗАПИСИ (контракт REV4 §2): ОДНА строка на run.
--   INSERT (STARTED) → UPDATE той же строки по run_id до COMPLETE или ERROR.
--   - run_id уникален (генерация в логгере: INS_<loader>_<ts>_<uuid8>);
--   - COMPLETE/ERROR терминальны: UPDATE идёт с условием status='STARTED';
--   - повторная финализация идемпотентна (0 affected rows, терминальный статус не перезаписывается);
--   - запись через DML (BigQuery.Jobs.query), НЕ streaming insertAll → UPDATE не блокируется
--     streaming buffer'ом (проверено: проект уже пишет через Jobs.query).
--
-- LOGICAL_PERIOD (контракт REV4 §1) — детерминированный, НЕ вычисляется из полученных строк:
--   - orders/sales (hourly incremental): последний ПОЛНОСТЬЮ закрытый день = DATE(ран, МСК) − 1;
--   - ads (daily): целевой период API (period_to).
-- ============================================================================

-- ── 1. Журнал ранов Apps Script-загрузчиков ─────────────────────────────────
CREATE TABLE IF NOT EXISTS `wb_raw.INGEST_RUNS` (
  run_id         STRING  NOT NULL,   -- уникален на попытку (генерит логгер)
  loader_name    STRING  NOT NULL,   -- 'orders' | 'sales' | 'ads' (далее — прочие)
  logical_period DATE    NOT NULL,   -- ДЕТЕРМИНИРОВАННЫЕ сутки, которые закрывает ран
  status         STRING  NOT NULL,   -- 'STARTED' | 'COMPLETE' | 'ERROR'
  source         STRING  NOT NULL,   -- 'apps_script' | 'cloud_run'
  trigger_type   STRING,             -- 'SCHEDULED' | 'MANUAL'
  started_at     TIMESTAMP NOT NULL,
  completed_at   TIMESTAMP,
  rows_fetched   INT64,              -- 0 допустимо и НЕ влияет на heartbeat
  rows_loaded    INT64,              -- 0 допустимо (zero-row success)
  error_code     STRING,
  error_message  STRING
)
PARTITION BY DATE(started_at)
CLUSTER BY loader_name, status, logical_period
OPTIONS (description = 'PR-Mart3a: журнал ранов загрузчиков. Источник heartbeat для freshness-gate Mart3. Одна строка на run: INSERT STARTED -> UPDATE COMPLETE/ERROR по run_id.');

-- ── 2. Единый heartbeat-вью (абстракция источника) ──────────────────────────
-- Mart3 читает ТОЛЬКО эту вью. Когда загрузчик переедет Apps Script → Cloud Run,
-- гейт менять НЕ придётся: изменится лишь значение `source`.
-- Включены только источники с ДЕТЕРМИНИРОВАННЫМ logical_period (годные для гейта).
-- finance/REF имеют собственные журналы (FINANCE_LOADER_RUNS / REF_SYNC_RUNS) и в гейт не входят
-- (finance лагает неделю) — наблюдать их отдельно, чтобы не смешивать семантику периода.
CREATE OR REPLACE VIEW `wb_raw.V_INGEST_HEARTBEAT` AS
SELECT
  run_id, loader_name, logical_period, status, source, trigger_type,
  started_at, completed_at, rows_fetched, rows_loaded, error_code, error_message
FROM `wb_raw.INGEST_RUNS`
UNION ALL
SELECT
  run_id,
  loader_name,
  SAFE.PARSE_DATE('%Y-%m-%d', logical_period)              AS logical_period,
  status,                                                   -- STARTED|COMPLETE|ERROR (та же доменная модель)
  'cloud_run'                                               AS source,
  'SCHEDULED'                                               AS trigger_type,
  started_at, completed_at, rows_fetched, rows_loaded, error_code, error_message
FROM `wb_raw.LOADER_RUNS`
WHERE environment = 'prod';

-- ── 3. Эталон запроса freshness-gate (используется в Mart3b) — LATEST-ATTEMPT ─
-- Для каждого обязательного источника L ∈ {orders, sales, ads} и target_date = D-1:
--   ПОСЛЕДНЯЯ попытка за logical_period (по started_at, tie-break run_id) обязана быть COMPLETE.
--   «Существует хоть один COMPLETE» НЕЛЬЗЯ: ранний ночной успех + поздний ERROR/PARTIAL →
--   EXISTS ложно пропустил бы сборку на неполных данных. Обязательный список — слева через LEFT JOIN,
--   чтобы полностью отсутствующий загрузчик дал FALSE, а не исчез (fail-closed).
-- (пример для ручной проверки; параметры подставляются в коде)
/*
DECLARE target_date DATE DEFAULT DATE_SUB(CURRENT_DATE('Europe/Moscow'), INTERVAL 1 DAY);
WITH required AS (SELECT l AS loader_name FROM UNNEST(['orders','sales','ads']) AS l),
     latest AS (
       SELECT loader_name, status, completed_at,
              ROW_NUMBER() OVER (PARTITION BY loader_name
                                 ORDER BY started_at DESC, run_id DESC) AS rn
       FROM `wb_raw.V_INGEST_HEARTBEAT`
       WHERE loader_name IN ('orders','sales','ads')
         AND logical_period = target_date
         AND started_at    >= TIMESTAMP(DATE_ADD(target_date, INTERVAL 1 DAY), 'Europe/Moscow')
     )
SELECT r.loader_name,
       COALESCE(l.status = 'COMPLETE' AND l.completed_at IS NOT NULL, FALSE) AS covers_target
FROM required r
LEFT JOIN (SELECT * FROM latest WHERE rn = 1) l USING (loader_name)
ORDER BY r.loader_name;
*/
