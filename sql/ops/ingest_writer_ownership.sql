-- ════════════════════════════════════════════════════════════════════════════════════════════════
-- Этап C (OWNER ACK 11.11.2026): единый writer-ownership gate и координатор cutover WB.
-- Док: docs/ops/INGEST_CUTOVER_COORDINATOR.md. Код: cloud/src/ownership/, cloud/src/loaders/cutover/,
-- apps-script/ingestion/WriterGate.gs.
--
-- ⚠️ ПРИМЕНЯЕТ ВЛАДЕЛЕЦ, отдельным решением, ДО выката gate в Apps Script и в Cloud Run:
--    без строки потока ОБА писателя fail-closed (ничего не пишут).
--    bq query --use_legacy_sql=false --project_id=<проект> < sql/ops/ingest_writer_ownership.sql
-- Скрипт идемпотентен: CREATE TABLE IF NOT EXISTS; seed вставляет только отсутствующие строки и
-- существующих НЕ трогает (повторный прогон не откатывает владение).
-- ════════════════════════════════════════════════════════════════════════════════════════════════

-- Seed остатков (M1). Остатки переключаются утверждённой процедурой MIG1B (снятие триггера + resume
-- Scheduler), не координатором. ПЕРЕД применением выставьте stocks_owner по факту:
--   MIG1B ещё не выполнен (пишет Apps Script)          → 'APPS_SCRIPT' (по умолчанию);
--   MIG1B выполнен (триггер снят, wb-stocks-prod ENABLED) → 'CLOUD_RUN'.
-- Ошибка здесь = остатки перестают писаться (неправообладатель штатно пропускает прогон).
-- DECLARE обязан стоять в начале скрипта BigQuery.
DECLARE stocks_owner STRING DEFAULT 'APPS_SCRIPT';

-- 1) Владение: одна строка на поток. Пишет ТОЛЬКО координатор (CAS: MERGE … WHEN MATCHED AND epoch/state/revision).
CREATE TABLE IF NOT EXISTS `wb_ops.INGEST_WRITER_OWNERSHIP` (
  stream STRING NOT NULL OPTIONS (description = 'stocks | statistics | ads | finance | supplies | ref'),
  owner STRING NOT NULL OPTIONS (description = 'APPS_SCRIPT | CLOUD_RUN | NONE — единственный писатель потока'),
  state STRING NOT NULL OPTIONS (description = 'LEGACY_ACTIVE → SHADOW_VALIDATING → QUALIFIED → LEGACY_FENCED → CLOUD_ACTIVE → MONITORING → COMPLETED; отказы BLOCKED, STOP, ROLLBACK_REQUIRED, UNKNOWN_OUTCOME; откат CLOUD_FENCED'),
  epoch INT64 NOT NULL OPTIONS (description = 'Fencing-токен: растёт при КАЖДОЙ смене владельца'),
  revision INT64 NOT NULL OPTIONS (description = 'Растёт при каждом переходе; входит в условие CAS (защита от ABA)'),
  legacy_watermark STRING OPTIONS (description = 'Отметка Apps Script, зафиксированная при доказанном дренаже: с неё начинает Cloud Run'),
  cloud_watermark STRING OPTIONS (description = 'Отметка Cloud Run при завершении мониторинга или при возврате владения Apps Script'),
  auto_cutover_enabled BOOL DEFAULT FALSE NOT NULL OPTIONS (description = 'tick сам ставит fence QUALIFIED → LEGACY_FENCED; иначе — только командой fence'),
  fenced_at TIMESTAMP OPTIONS (description = 'Момент последнего fence (owner → NONE): от него отсчитывается дренаж'),
  active_since TIMESTAMP OPTIONS (description = 'Начало окна мониторинга Cloud Run (активация или повторный мониторинг)'),
  changed_at TIMESTAMP NOT NULL,
  changed_by STRING,
  reason STRING
)
OPTIONS (description = 'Этап C: единственный источник правды о писателе потока WB. Читают gate Cloud Run (cli.ts) и Apps Script (WriterGate.gs) перед записью; пишет только координатор ingest-cutover-prod.');

-- 2) Журнал переходов: append-only, строка пишется в ТОЙ ЖЕ транзакции, что и CAS.
CREATE TABLE IF NOT EXISTS `wb_ops.INGEST_CUTOVER_LOG` (
  transition_id STRING NOT NULL OPTIONS (description = '<stream>:r<revision> — уникален: revision входит в CAS'),
  stream STRING NOT NULL,
  from_state STRING NOT NULL,
  to_state STRING NOT NULL,
  epoch INT64 NOT NULL OPTIONS (description = 'epoch ПОСЛЕ перехода'),
  run_id STRING OPTIONS (description = 'Исполнение координатора'),
  source_watermark STRING OPTIONS (description = 'Точная отметка перехода (legacy при активации, cloud при возврате)'),
  reason STRING,
  evidence JSON OPTIONS (description = 'Доказательства: квалификация, окно, дренаж (прогоны, задания BigQuery), циклы'),
  at TIMESTAMP NOT NULL
)
PARTITION BY DATE(at)
CLUSTER BY stream
OPTIONS (description = 'Этап C: журнал переходов владения писателей WB. Только добавление.');

-- 3) Вердикты квалификации shadow ↔ production по суткам и группам проверок.
CREATE TABLE IF NOT EXISTS `wb_ops.INGEST_QUALIFICATION` (
  stream STRING NOT NULL,
  business_date DATE NOT NULL,
  check_group STRING NOT NULL OPTIONS (description = 'Группа проверок: STOCKS_T6, STATS_ORDERS, ADS_COSTS, FINANCE_WEEKLY… — cloud/src/loaders/cutover/profiles.ts'),
  verdict STRING NOT NULL OPTIONS (description = 'PASS | FAIL | DRIFT_EXPLAINED | MISSING'),
  details JSON,
  image_digest STRING OPTIONS (description = 'Образ shadow, на котором получен вердикт'),
  at TIMESTAMP NOT NULL
)
PARTITION BY business_date
CLUSTER BY stream, check_group
OPTIONS (description = 'Этап C: вердикты гейта квалификации. Последняя строка (дата, группа) по at заменяет прежние.');

-- 4) Seed: по строке на поток. Существующие строки НЕ меняются. stocks_owner — см. DECLARE в начале скрипта.
ASSERT stocks_owner IN ('APPS_SCRIPT', 'CLOUD_RUN') AS 'stocks_owner: APPS_SCRIPT или CLOUD_RUN';

MERGE `wb_ops.INGEST_WRITER_OWNERSHIP` T
USING (
  SELECT s AS stream,
         IF(s = 'stocks', stocks_owner, 'APPS_SCRIPT') AS owner,
         IF(s = 'stocks' AND stocks_owner = 'CLOUD_RUN', 'COMPLETED', 'LEGACY_ACTIVE') AS state
  FROM UNNEST(['stocks', 'statistics', 'ads', 'finance', 'supplies', 'ref']) AS s
) S
ON T.stream = S.stream
WHEN NOT MATCHED THEN
  INSERT (stream, owner, state, epoch, revision, auto_cutover_enabled, changed_at, changed_by, reason)
  VALUES (S.stream, S.owner, S.state, 1, 0, FALSE, CURRENT_TIMESTAMP(), 'seed:sql/ops/ingest_writer_ownership.sql',
          'начальное состояние этапа C');

-- 5) Проверка seed: ровно 6 потоков, по одной строке, epoch ≥ 1.
ASSERT (SELECT COUNT(*) FROM `wb_ops.INGEST_WRITER_OWNERSHIP`) = 6 AS 'ожидалось 6 строк владения';
ASSERT (SELECT COUNT(DISTINCT stream) FROM `wb_ops.INGEST_WRITER_OWNERSHIP`) = 6 AS 'дубль потока в таблице владения';
ASSERT NOT EXISTS (SELECT 1 FROM `wb_ops.INGEST_WRITER_OWNERSHIP` WHERE owner NOT IN ('APPS_SCRIPT', 'CLOUD_RUN', 'NONE') OR epoch < 1)
  AS 'невалидный owner/epoch';

-- ────────────────────────────────────────────────────────────────────────────────────────────────
-- Справка (НЕ исполняется): форма CAS-перехода, которую применяет координатор (cloud/src/loaders/cutover/store.ts).
--   BEGIN TRANSACTION;
--   MERGE `wb_ops.INGEST_WRITER_OWNERSHIP` T USING (SELECT @stream AS stream) S ON T.stream = S.stream
--   WHEN MATCHED AND T.epoch = @epoch AND T.state = @from_state AND T.revision = @revision THEN UPDATE SET
--     owner = @owner, state = @to_state, epoch = @new_epoch, revision = @revision + 1, …;
--   -- @@row_count = 1 → INSERT INTO `wb_ops.INGEST_CUTOVER_LOG` …; = 0 → CAS не совпал (OK_ALREADY / конфликт)
--   COMMIT TRANSACTION;
-- Ручной правки строк владения нет: отказ/откат — командами координатора (CUTOVER_ACTION=rollback).
-- Откат САМОЙ схемы — docs/ops/INGEST_CUTOVER_COORDINATOR.md §7 (сначала gate, потом таблицы).
