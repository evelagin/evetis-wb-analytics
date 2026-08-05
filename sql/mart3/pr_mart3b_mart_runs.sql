-- ============================================================================
-- PR-Mart3b-1 — журнал прогонов витрины MART_RUNS + окно наблюдения V_MART_RUN_LOG.
-- Дата: 2026-08-03.  Дизайн: docs/MART_PR3B_PLAN_2026-08-03.md (REV3 APPROVED, §6).
-- PR-нота: docs/MART_PR3B1_LOADER_2026-08-03.md.
--
-- ЗАЧЕМ. Типизированный журнал каждого прогона оркестратора витрины (loader `mart`):
--   свежесть источников на момент build, флаг provisional-рекламы, тайминги шагов,
--   число строк витрины, версия кода/образа, код/текст ошибки. Отдельно от LOADER_RUNS
--   (манифест миграции остаётся чисто guard/lease; его схему не трогаем).
--
-- КОНТРАКТ ЗАПИСИ (аудит REV2/REV3): РОВНО ОДНА строка на run_id, идемпотентно.
--   Loader пишет ОДИН терминальный `MERGE ON run_id WHEN NOT MATCHED THEN INSERT` (COMPLETE|ERROR),
--   затем read-back: ровно 1 строка, и её (status, environment, target_date, git_sha) совпадают с
--   записываемыми — иначе fail-closed (MART_RUNS_DUP / MART_RUNS_CONFLICT / MART_RUNS_IDENTITY_CONFLICT).
--   Так повтор/коллизия run_id между датами/средами/версиями кода не считается идемпотентным повтором.
--   Сбой записи НЕ маскирует исходную ошибку прогона (loader ловит и логирует, пробрасывая исходную).
--
-- ⚠️ IAM (важно для PR-Mart3b-2). Таблицами владеет DDL (этот файл), НЕ runtime. Но запись — `MERGE`
--    (mutating DML) + `SELECT` read-back, а НЕ append-only INSERT. Runtime SA нужны
--    bigquery.tables.getData И bigquery.tables.updateData на wb_mart.MART_RUNS (на практике —
--    dataset-level `BigQuery Data Editor` для wb_mart или эквивалентный custom role). Insert-only
--    доступ упадёт на первом MERGE. Применять до первого прогона (как INGEST_RUNS в PR-Mart3a). Location EU.
-- В PR-Mart3b-2 V_MART_RUN_LOG расширяется до JOIN с LOADER_RUNS(loader='mart') по run_id.
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS `wb_mart` OPTIONS (location = 'EU');

-- ── 1. Журнал прогонов витрины ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS `wb_mart.MART_RUNS` (
  run_id                STRING    NOT NULL,   -- сквозной id: Cloud Run → LOADER_RUNS → FACT → MART → сюда
  environment           STRING    NOT NULL,   -- 'shadow' | 'prod'
  target_date           DATE      NOT NULL,   -- D-1 (Europe/Moscow); == build_as_of_date витрины
  status                STRING    NOT NULL,   -- 'COMPLETE' | 'ERROR' (терминальные; STARTED здесь не пишется)
  started_at            TIMESTAMP NOT NULL,   -- старт прогона loader
  completed_at          TIMESTAMP,            -- момент записи строки (CURRENT_TIMESTAMP при INSERT)
  duration_ms           INT64,                -- длительность прогона (для инварианта build < TTL)
  freshness_json        STRING,               -- снимок гейта per-source: [{loader, covers_target, last_complete_at}]
  ads_activity_lagged   BOOL,                 -- ДИАГНОСТИКА активности (build-level; НЕ индикатор полноты — см. процедуру)
  ads_activity_max_date DATE,                 -- МАКС дата рекламной активности в FACT на момент build (диагностика)
  steps_json            STRING,               -- шаги: [{step, status, duration_ms}] (freshness_gate/bootstrap/build)
  mart_rows             INT64,                -- COUNT(*) MART_SKU_DAILY после publish (NULL при раннем ERROR)
  git_sha               STRING,               -- версия кода (сверяется в read-back → MART_RUNS_IDENTITY_CONFLICT)
  image_digest          STRING,               -- версия образа
  error_code            STRING,               -- при ERROR: FRESHNESS_GATE | FRESHNESS_SHAPE | MART_* | CTX_INVARIANT | ...
  error_message         STRING
)
PARTITION BY target_date
CLUSTER BY environment, status
OPTIONS (description = 'PR-Mart3b-1: журнал прогонов витрины MART. Одна терминальная строка на run_id (COMPLETE|ERROR).');

-- ── 2. Окно наблюдения (минимальное для PR3b-1: проекция MART_RUNS) ──────────
-- PR-Mart3b-2 расширит до LOADER_RUNS(loader='mart') JOIN MART_RUNS USING(run_id),
-- когда loader `mart` войдёт в общий CLI + run-lease. Пока — самодостаточная проекция.
CREATE OR REPLACE VIEW `wb_mart.V_MART_RUN_LOG` AS
SELECT
  run_id, environment, target_date, status,
  started_at, completed_at, duration_ms,
  ads_activity_lagged, ads_activity_max_date, mart_rows,
  git_sha, image_digest, error_code, error_message,
  freshness_json, steps_json
FROM `wb_mart.MART_RUNS`;
