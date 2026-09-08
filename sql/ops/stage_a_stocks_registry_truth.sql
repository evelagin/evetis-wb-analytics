-- ============================================================================
-- STAGE A / A5 — реестр конвейеров должен описывать фактического производителя
-- остатков WB. Дата: 2026-09-08. Находка F-03 (HIGH).
--
-- ЧТО БЫЛО НЕ ТАК.
--   wb_ops.OPS_PIPELINE_REGISTRY.stocks_cloudrun объявлял:
--     enabled = TRUE
--     target_object = wb_raw.RAW_WB_STOCKS
--     cadence_spec = '30 6 * * * Europe/Moscow'
--     run_log_source = wb_raw.LOADER_RUNS
--   Фактически на 2026-09-08:
--     Scheduler wb-stocks-prod ....... PAUSED, ни одного срабатывания
--     Cloud Run Job wb-stocks-prod ... args = ["noop"]
--     LOADER_RUNS(environment='prod', loader_name='stocks') ... 0 строк
--     RAW_WB_STOCKS наполняет Apps Script (WbStocksSnapshot.gs), 06:23 МСК,
--     журнал прогонов — wb_raw.WB_STOCKS_SNAPSHOTS
--   То есть реестр описывал НАМЕРЕНИЕ будущей архитектуры, а не production.
--
-- ПОЧЕМУ ЭТО ОПАСНО. Реестр — вход детектора здоровья. Пока запись врёт,
--   остановка Apps Script не породит инцидента, а любой будущий MISSED_RUN-чек
--   на stocks_cloudrun будет срабатывать вечно. Остатки при этом snapshot-only:
--   пропущенные сутки невосстановимы (подтверждено 18–20.07 и 03.09.2026).
--
-- ЧТО ЭТОТ ФАЙЛ НЕ ДЕЛАЕТ.
--   Не включает wb-stocks-prod, не трогает Apps Script, не мигрирует загрузку
--   в Cloud Run, не меняет данные остатков и не создаёт новых чеков здоровья.
--   Меняются ровно две строки операционных метаданных.
--
-- BLAST RADIUS. Ни один из семи действующих чеков (H0–H6) не читает
--   stocks_cloudrun и stocks_snapshot: CTE `reg` в sp_evaluate_pipeline_health
--   выбирается только по pipeline_id 'mart'. Поведение детектора не меняется.
--
-- ОТКАТ. Прежние значения приведены в §3 закомментированным UPDATE.
-- ============================================================================

-- ── §1. ГЕЙТЫ: сначала доказать, что реальность именно такая ────────────────
--    Fail-closed. Если Cloud Run stocks когда-нибудь введут в эксплуатацию,
--    первый же прогон этого файла упадёт, а не перепишет правду задним числом.

ASSERT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_raw.LOADER_RUNS`
        WHERE environment = 'prod' AND loader_name = 'stocks') = 0
  AS 'A5 GATE 1 FAIL: в LOADER_RUNS появились prod-прогоны cloud-загрузчика остатков. Cloud Run stocks введён в эксплуатацию — реестр надо приводить к НОВОЙ реальности, а не к описанной в этом файле.';

ASSERT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_raw.WB_STOCKS_SNAPSHOTS`
        WHERE status = 'COMPLETE'
          AND started_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 48 HOUR)) > 0
  AS 'A5 GATE 2 FAIL: за последние 48 часов нет ни одного успешного снимка остатков Apps Script. Прежде чем объявлять его производителем, надо разобраться, почему он молчит.';

-- ── §2. Правда о производителе ──────────────────────────────────────────────

-- 2.1. stocks_snapshot — это и есть производитель RAW-остатков.
--   target_object переводится с журнала на сами данные: журнал у пайплайна
--   есть и указывается отдельно в run_log_source. Раньше поле run_log_source
--   было пустым, а в notes стояло «собственного run-log нет» — это неверно:
--   WB_STOCKS_SNAPSHOTS содержит started_at/status/control_status по каждому
--   прогону, 57 COMPLETE и 2 ERROR на момент Stage A.
UPDATE `project-fa311fc0-4d87-4781-986.wb_ops.OPS_PIPELINE_REGISTRY`
SET target_object               = 'wb_raw.RAW_WB_STOCKS',
    run_log_source              = 'wb_raw.WB_STOCKS_SNAPSHOTS',
    runtime_verification_status = 'VERIFIED',
    cadence_observed_evidence   = 'Stage A 2026-09-08: WB_STOCKS_SNAPSHOTS — 57 COMPLETE, последний 2026-09-08 03:23 UTC; RAW_WB_STOCKS свежа на текущие сутки. Cloud Run в наполнении не участвует.',
    cadence_last_observed_at    = CURRENT_TIMESTAMP(),
    notes                       = 'ФАКТИЧЕСКИЙ производитель wb_raw.RAW_WB_STOCKS (T6) и wb_raw.RAW_WB_STOCKS_T5. Загрузка идёт из Apps Script (WbStocksSnapshot.gs), НЕ из Cloud Run. SNAPSHOT-ONLY: сутки без снимка невосстановимы — подтверждено пропусками 18-20.07.2026 и 03.09.2026 (HTTP 500 analytics-open-api). Журнал прогонов — WB_STOCKS_SNAPSHOTS.',
    updated_at                  = CURRENT_TIMESTAMP()
WHERE pipeline_id = 'stocks_snapshot';

-- 2.2. stocks_cloudrun — объявлен, но НЕ введён в эксплуатацию.
--   enabled=FALSE выводит запись из production-выборки детектора и снимает
--   ложное впечатление, что остатки грузит Cloud Run. Строка не удаляется:
--   история и намерение сохраняются.
UPDATE `project-fa311fc0-4d87-4781-986.wb_ops.OPS_PIPELINE_REGISTRY`
SET enabled                     = FALSE,
    runtime_verification_status = 'NOT_VERIFIED',
    coverage_check_enabled      = FALSE,
    cadence_observed_evidence   = 'Stage A 2026-09-08: Scheduler wb-stocks-prod = PAUSED (ни одного срабатывания), Cloud Run Job wb-stocks-prod развёрнут с args=["noop"], LOADER_RUNS(environment=prod, loader_name=stocks) = 0 строк.',
    cadence_last_observed_at    = CURRENT_TIMESTAMP(),
    notes                       = 'НЕ ВВЕДЁН В ЭКСПЛУАТАЦИЮ. Объявлен в infra/terraform/scheduler.tf и cloud_run_jobs.tf, но Job запускается как noop, а расписание на паузе. Production-остатки грузит pipeline_id=stocks_snapshot (Apps Script). Включение — отдельное решение: снять паузу Scheduler, вернуть args=["stocks"], сверить параллельные сутки с shadow-контуром (RAW_WB_STOCKS__CR), и только затем переносить сюда enabled=TRUE и target_object.',
    updated_at                  = CURRENT_TIMESTAMP()
WHERE pipeline_id = 'stocks_cloudrun';

-- ── §3. ОТКАТ (не выполнять; значения до Stage A) ───────────────────────────
-- UPDATE `project-fa311fc0-4d87-4781-986.wb_ops.OPS_PIPELINE_REGISTRY`
-- SET target_object = 'wb_raw.WB_STOCKS_SNAPSHOTS', run_log_source = NULL,
--     runtime_verification_status = 'NOT_VERIFIED',
--     cadence_observed_evidence = 'WbStocksSnapshot.gs:563',
--     notes = 'Snapshot-only: сутки без снимка невосстановимы. Собственного run-log нет -> свежесть по данным.'
-- WHERE pipeline_id = 'stocks_snapshot';
--
-- UPDATE `project-fa311fc0-4d87-4781-986.wb_ops.OPS_PIPELINE_REGISTRY`
-- SET enabled = TRUE, runtime_verification_status = 'NOT_VERIFIED', coverage_check_enabled = TRUE,
--     cadence_observed_evidence = 'infra/terraform/scheduler.tf:90 — ДЕКЛАРАЦИЯ, runtime не подтверждён (gcloud недоступен)',
--     notes = 'Фильтр environment=prod: shadow-прогоны в health не входят.'
-- WHERE pipeline_id = 'stocks_cloudrun';

-- ── §4. ПРИЁМКА ─────────────────────────────────────────────────────────────

ASSERT (SELECT enabled FROM `project-fa311fc0-4d87-4781-986.wb_ops.OPS_PIPELINE_REGISTRY`
        WHERE pipeline_id = 'stocks_cloudrun') = FALSE
  AS 'A5 CHECK 1 FAIL: stocks_cloudrun всё ещё объявлен включённым';

ASSERT (SELECT target_object FROM `project-fa311fc0-4d87-4781-986.wb_ops.OPS_PIPELINE_REGISTRY`
        WHERE pipeline_id = 'stocks_snapshot') = 'wb_raw.RAW_WB_STOCKS'
  AS 'A5 CHECK 2 FAIL: производитель RAW_WB_STOCKS не закреплён за stocks_snapshot';

-- Ровно один включённый production-пайплайн должен отвечать за RAW_WB_STOCKS.
ASSERT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_ops.OPS_PIPELINE_REGISTRY`
        WHERE environment = 'prod' AND enabled AND target_object = 'wb_raw.RAW_WB_STOCKS') = 1
  AS 'A5 CHECK 3 FAIL: за wb_raw.RAW_WB_STOCKS отвечает не ровно один включённый пайплайн';

-- Детектор не должен изменить поведение: он не читает эти два pipeline_id.
ASSERT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_ops.V_OPS_CURRENT_HEALTH`
        WHERE pipeline_id IN ('stocks_cloudrun', 'stocks_snapshot')) = 0
  AS 'A5 CHECK 4 FAIL: у stocks-пайплайнов появились чеки здоровья — оценить влияние изменения enabled ДО применения';
