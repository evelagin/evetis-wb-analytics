-- ============================================================================
-- PR-Mart3b — журнал прогонов витрины MART_RUNS + окно наблюдения V_MART_RUN_LOG.
-- Дата: 2026-08-03 (DDL, PR3b-1).  Обновление вью: 2026-08-06 (PR3b-2, JOIN LOADER_RUNS).
-- Дизайн: docs/MART_PR3B_PLAN_2026-08-03.md (REV3 APPROVED, §6).
-- PR-ноты: docs/MART_PR3B1_LOADER_2026-08-03.md, docs/MART_PR3B2_LOADER_2026-08-06.md.
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
-- ⚠️ IAM. Таблицами владеет DDL (этот файл), НЕ runtime. Но запись — `MERGE` (mutating DML) +
--    `SELECT` read-back, а НЕ append-only INSERT. Runtime SA (loaders_prod) нужны
--    bigquery.tables.getData И bigquery.tables.updateData на wb_mart.MART_RUNS — в PR-Mart3b-2 это
--    даётся dataset-level `BigQuery Data Editor` на wb_mart (infra/terraform/bigquery.tf). Применять
--    до первого прогона (как INGEST_RUNS в PR-Mart3a). Location EU.
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
  error_code            STRING,               -- при ERROR: FRESHNESS_GATE | FRESHNESS_SHAPE | MART_* | CTX_INVARIANT | MART_ENV | ...
  error_message         STRING
)
PARTITION BY target_date
CLUSTER BY environment, status
OPTIONS (description = 'PR-Mart3b-1: журнал прогонов витрины MART. Одна терминальная строка на run_id (COMPLETE|ERROR).');

-- ── 2. Окно наблюдения: MART_RUNS ⟗ LOADER_RUNS(loader='mart') по run_id ─────
-- PR-Mart3b-2: mart вошёл в общий CLI + run-lease (LOADER_RUNS ведёт cli). Вью сшивает два журнала:
--   • MART_RUNS   — бизнес-детали (свежесть, шаги, mart_rows, версия, ошибка);
--   • LOADER_RUNS — оркестрация lease (status/attempt_count/execution_id/тайминги/ошибка).
-- FULL OUTER JOIN (аудит REV2, блокер #3): сохраняем ОБЕ стороны, чтобы ни один тип отказа не
--   исчезал из наблюдения (критично для алертов PR3b-3):
--     • ручной MART без lease (mart_manual.ts, в т.ч. PR3b-1 acceptance) → lease_* = NULL;
--     • LEASE-ONLY сбой без MART_RUNS (отказ IAM / падение MERGE / смерть до терминальной записи)
--       → бизнес-поля NULL, но строка ВИДНА (флаг lease_only_no_mart=TRUE);
--     • нормальный автопрогон → обе стороны присутствуют.
--   run_id/environment/target_date выводим через COALESCE (target_date lease-стороны —
--   из LOADER_RUNS.logical_period, это D-1 строкой → PARSE_DATE).
-- Гранулярность: LOADER_RUNS.run_id уникален (одна строка на run_id: INSERT STARTED → UPDATE),
--   MART_RUNS.run_id уникален (MERGE), поэтому JOIN 1:1 — дублей нет.
-- ⚠️ Вью НЕ authorized: тому, кто SELECT'ит V_MART_RUN_LOG (аналитик/Looker), нужен read на ОБЕ
--   базовые таблицы — wb_mart.MART_RUNS и wb_raw.LOADER_RUNS.
CREATE OR REPLACE VIEW `wb_mart.V_MART_RUN_LOG` AS
WITH mart_lease AS (
  -- фильтр loader='mart' ДО join'а, иначе FULL OUTER затянул бы чужие lease-строки
  SELECT run_id, environment, logical_period, status, attempt_count, execution_id,
         started_at, completed_at, error_code, error_message
  FROM `wb_raw.LOADER_RUNS`
  WHERE loader_name = 'mart'
)
SELECT
  COALESCE(m.run_id, l.run_id)                                          AS run_id,
  COALESCE(m.environment, l.environment)                               AS environment,
  COALESCE(m.target_date, SAFE.PARSE_DATE('%Y-%m-%d', l.logical_period)) AS target_date,
  -- бизнес-сторона (MART_RUNS): NULL, если бизнес-строка не записана (lease-only сбой)
  m.status, m.started_at, m.completed_at, m.duration_ms,
  m.ads_activity_lagged, m.ads_activity_max_date, m.mart_rows,
  m.git_sha, m.image_digest, m.error_code, m.error_message,
  m.freshness_json, m.steps_json,
  -- lease-сторона (LOADER_RUNS): NULL для ручных прогонов
  l.status        AS lease_status,
  l.attempt_count AS lease_attempt_count,
  l.execution_id  AS lease_execution_id,
  l.started_at    AS lease_started_at,
  l.completed_at  AS lease_completed_at,
  l.error_code    AS lease_error_code,
  l.error_message AS lease_error_message,
  -- прямой индикатор для алертов PR3b-3: lease есть, а бизнес-строки нет
  (m.run_id IS NULL AND l.run_id IS NOT NULL) AS lease_only_no_mart
FROM `wb_mart.MART_RUNS` m
FULL OUTER JOIN mart_lease l
  ON l.run_id = m.run_id;
