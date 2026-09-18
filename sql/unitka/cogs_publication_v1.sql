-- ============================================================================
-- UNITKA COGS PUBLICATION V1 — физическая публикация канонического COGS в wb_mart
-- (Phase 1C2A, 18.09.2026). СТАТУС: ТОЛЬКО ИСХОДНИК В GIT, в BigQuery НЕ применён.
-- Док: docs/UNITKA_INTEGRITY_GUARD_V1.md §6. Откат: sql/unitka/cogs_publication_v1_rollback.sql.
--
-- ЗАЧЕМ. Integrity Guard сверяет COGS листа с каноном. Канон — evetis_ref.V_PRODUCT_COGS_EFFECTIVE —
-- runtime-учётки Unitka (sa-loaders-*) читать НЕ должны (решение владельца: ни READER на evetis_ref,
-- ни ручного authorized view). Логическая вью в wb_mart над evetis_ref задачу не решает: BigQuery
-- проверяет права вызывающего на исходные таблицы. Поэтому — ФИЗИЧЕСКАЯ копия, которую собирает
-- отдельная учётка sa-unitka-cogs-pub (шаблон Executive V2 / SKU Performance V2).
--
-- ИСТИНА ОСТАЁТСЯ В evetis_ref. Эта таблица — производная, пересобирается целиком каждый час;
-- вручную её не правят и вторым источником правды она не является.
--
-- ПОРЯДОК ПРИМЕНЕНИЯ (1C2B, гейт владельца): §1 таблицы → §2 процедура → Terraform
-- (SA, IAM, Scheduler; см. infra/terraform/unitka_cogs_publication.tf) → первый CALL →
-- затем sql/unitka/integrity_v1.sql (вью V_UNITKA_COGS_CANONICAL читает таблицу из §1).
--
-- Процедура таблиц НЕ создаёт (у SA нет права создания в датасете): нет таблицы — сборка
-- падает fail-closed, последняя удачная копия остаётся.
-- ============================================================================

-- ── §1. Таблицы: снимок, журнал, замок ─────────────────────────────────────
CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.wb_mart.UNITKA_COGS_EFFECTIVE` (
  internal_sku            STRING    NOT NULL,
  effective_from          DATE      NOT NULL,
  effective_to            DATE,                -- NULL = интервал открыт (контракт резолвера)
  product_cogs_rub        NUMERIC   NOT NULL,  -- > 0 (ASSERT); NULL/0 в канон не публикуются
  -- Провенанс — ровно те поля, что уже отдаёт канон (ничего не выдумано):
  resolver_lane           STRING,
  cogs_origin_type        STRING,
  confidence              STRING,
  owner_confirmed         BOOL,
  cogs_provenance_status  STRING,
  cost_basis              STRING,
  -- Метаданные публикации (одинаковы у всех строк одного снимка):
  published_at            TIMESTAMP NOT NULL,
  publish_run_id          STRING    NOT NULL,
  source_fingerprint      STRING    NOT NULL,  -- отпечаток канонических строк на момент публикации
  source_row_count        INT64     NOT NULL
)
CLUSTER BY internal_sku
OPTIONS (description = 'UNITKA COGS PUBLICATION V1: физическая копия evetis_ref.V_PRODUCT_COGS_EFFECTIVE для Integrity Guard (Unitka не читает evetis_ref). Производная, пересобирается целиком; пишет ТОЛЬКО wb_mart.sp_publish_unitka_cogs. Истина — evetis_ref.');

CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.wb_mart.UNITKA_COGS_PUBLISH_LOG` (
  run_id              STRING    NOT NULL,
  trigger_source      STRING,
  started_at          TIMESTAMP NOT NULL,
  finished_at         TIMESTAMP,
  status              STRING    NOT NULL,   -- STARTED | SUCCESS | FAILED | SKIPPED_LOCKED
  rows_published      INT64,
  source_fingerprint  STRING,
  error_message       STRING
)
OPTIONS (description = 'UNITKA COGS PUBLICATION V1: журнал публикаций UNITKA_COGS_EFFECTIVE (sp_publish_unitka_cogs).');

CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.wb_mart._UNITKA_COGS_PUBLISH_LOCK` (
  lock_id      STRING NOT NULL,
  is_running   BOOL   NOT NULL,
  run_id       STRING,
  acquired_at  TIMESTAMP,
  released_at  TIMESTAMP
)
OPTIONS (description = 'UNITKA COGS PUBLICATION V1: замок публикации, одна строка lock_id = unitka_cogs_publication.');

INSERT INTO `project-fa311fc0-4d87-4781-986.wb_mart._UNITKA_COGS_PUBLISH_LOCK` (lock_id, is_running)
SELECT 'unitka_cogs_publication', FALSE FROM (SELECT 1)
WHERE NOT EXISTS (SELECT 1 FROM `project-fa311fc0-4d87-4781-986.wb_mart._UNITKA_COGS_PUBLISH_LOCK`
                  WHERE lock_id = 'unitka_cogs_publication');

-- ── §2. Процедура публикации ───────────────────────────────────────────────
-- Последовательность: замок → staging (TEMP) → ASSERT → отпечаток → атомарная подмена
-- (транзакция DELETE+INSERT) → SUCCESS → освобождение замка. Любая ошибка → ROLLBACK, FAILED,
-- таблица остаётся от последней удачной публикации, частичный снимок текущим не становится.
CREATE OR REPLACE PROCEDURE `project-fa311fc0-4d87-4781-986.wb_mart.sp_publish_unitka_cogs`(in_trigger STRING)
BEGIN
  DECLARE v_run_id      STRING    DEFAULT GENERATE_UUID();
  DECLARE v_started     TIMESTAMP DEFAULT CURRENT_TIMESTAMP();
  DECLARE v_rows        INT64;
  DECLARE v_source_rows INT64;
  DECLARE v_fp          STRING;
  DECLARE v_in_tx       BOOL DEFAULT FALSE;   -- @@transaction_id недоступен в обработчике исключений

  INSERT INTO `project-fa311fc0-4d87-4781-986.wb_mart.UNITKA_COGS_PUBLISH_LOG`
    (run_id, trigger_source, started_at, status)
  VALUES (v_run_id, IFNULL(in_trigger, 'manual'), v_started, 'STARTED');

  -- 1) Замок. Висящий дольше 30 мин (публикация занимает секунды) считается зависшим.
  UPDATE `project-fa311fc0-4d87-4781-986.wb_mart._UNITKA_COGS_PUBLISH_LOCK`
     SET is_running = TRUE, run_id = v_run_id, acquired_at = CURRENT_TIMESTAMP(), released_at = NULL
   WHERE lock_id = 'unitka_cogs_publication'
     AND (NOT is_running OR acquired_at < TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 MINUTE));
  IF @@row_count = 0 THEN
    UPDATE `project-fa311fc0-4d87-4781-986.wb_mart.UNITKA_COGS_PUBLISH_LOG`
       SET status = 'SKIPPED_LOCKED', finished_at = CURRENT_TIMESTAMP(), error_message = 'предыдущая публикация ещё выполняется'
     WHERE run_id = v_run_id;
    RETURN;
  END IF;

  BEGIN
    -- 2) Staging: канон как есть, без преобразований значений.
    CREATE TEMP TABLE s AS
    SELECT internal_sku, effective_from, effective_to, product_cogs_rub,
           resolver_lane, cogs_origin_type, confidence, owner_confirmed, cogs_provenance_status, cost_basis
    FROM `project-fa311fc0-4d87-4781-986.evetis_ref.V_PRODUCT_COGS_EFFECTIVE`;

    SET v_source_rows = (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.V_PRODUCT_COGS_EFFECTIVE`);
    SET v_rows = (SELECT COUNT(*) FROM s);

    -- 3) Инварианты ДО подмены. Нарушение — FAILED, прежний снимок остаётся.
    ASSERT v_rows > 0
      AS 'UNITKA_COGS P1: канон пуст — публиковать нечего';
    ASSERT v_rows = v_source_rows
      AS 'UNITKA_COGS P2: число строк staging не совпало с каноном';
    ASSERT (SELECT COUNTIF(internal_sku IS NULL OR TRIM(internal_sku) = '') FROM s) = 0
      AS 'UNITKA_COGS P3: internal_sku пуст';
    ASSERT (SELECT COUNTIF(effective_from IS NULL) FROM s) = 0
      AS 'UNITKA_COGS P4: effective_from пуст';
    ASSERT (SELECT COUNTIF(product_cogs_rub IS NULL OR product_cogs_rub <= 0) FROM s) = 0
      AS 'UNITKA_COGS P5: COGS NULL или <= 0 (контракт резолвера: никогда 0)';
    ASSERT (SELECT COUNTIF(effective_to IS NOT NULL AND effective_to < effective_from) FROM s) = 0
      AS 'UNITKA_COGS P6: интервал с effective_to < effective_from';
    ASSERT (SELECT COUNT(*) FROM (SELECT internal_sku, effective_from, COUNT(*) AS n FROM s GROUP BY 1, 2 HAVING n > 1)) = 0
      AS 'UNITKA_COGS P7: дубль интервала (internal_sku, effective_from)';
    ASSERT (
      SELECT COUNT(*) FROM s a JOIN s b
        ON a.internal_sku = b.internal_sku
       AND a.effective_from < b.effective_from
       AND (a.effective_to IS NULL OR a.effective_to >= b.effective_from)
    ) = 0
      AS 'UNITKA_COGS P8: пересекающиеся интервалы одного internal_sku';

    -- 4) Отпечаток канонических строк (детерминированный порядок).
    SET v_fp = (
      SELECT TO_HEX(SHA256(STRING_AGG(
        FORMAT('%s|%t|%t|%s|%s|%s|%s|%t|%s|%s', internal_sku, effective_from, effective_to,
               CAST(product_cogs_rub AS STRING), IFNULL(resolver_lane, ''), IFNULL(cogs_origin_type, ''),
               IFNULL(confidence, ''), owner_confirmed, IFNULL(cogs_provenance_status, ''), IFNULL(cost_basis, '')),
        '\n' ORDER BY internal_sku, effective_from)))
      FROM s
    );

    -- 5) Атомарная подмена.
    BEGIN TRANSACTION;
    SET v_in_tx = TRUE;
      DELETE FROM `project-fa311fc0-4d87-4781-986.wb_mart.UNITKA_COGS_EFFECTIVE` WHERE TRUE;
      INSERT INTO `project-fa311fc0-4d87-4781-986.wb_mart.UNITKA_COGS_EFFECTIVE`
        (internal_sku, effective_from, effective_to, product_cogs_rub, resolver_lane, cogs_origin_type,
         confidence, owner_confirmed, cogs_provenance_status, cost_basis,
         published_at, publish_run_id, source_fingerprint, source_row_count)
      SELECT internal_sku, effective_from, effective_to, product_cogs_rub, resolver_lane, cogs_origin_type,
             confidence, owner_confirmed, cogs_provenance_status, cost_basis,
             v_started, v_run_id, v_fp, v_source_rows
      FROM s;
    COMMIT TRANSACTION;
    SET v_in_tx = FALSE;

    -- 6) SUCCESS и освобождение замка.
    UPDATE `project-fa311fc0-4d87-4781-986.wb_mart._UNITKA_COGS_PUBLISH_LOCK`
       SET is_running = FALSE, released_at = CURRENT_TIMESTAMP()
     WHERE lock_id = 'unitka_cogs_publication' AND run_id = v_run_id;
    UPDATE `project-fa311fc0-4d87-4781-986.wb_mart.UNITKA_COGS_PUBLISH_LOG`
       SET status = 'SUCCESS', finished_at = CURRENT_TIMESTAMP(), rows_published = v_rows, source_fingerprint = v_fp
     WHERE run_id = v_run_id;

  EXCEPTION WHEN ERROR THEN
    IF v_in_tx THEN
      ROLLBACK TRANSACTION;
    END IF;
    UPDATE `project-fa311fc0-4d87-4781-986.wb_mart._UNITKA_COGS_PUBLISH_LOCK`
       SET is_running = FALSE, released_at = CURRENT_TIMESTAMP()
     WHERE lock_id = 'unitka_cogs_publication' AND run_id = v_run_id;
    UPDATE `project-fa311fc0-4d87-4781-986.wb_mart.UNITKA_COGS_PUBLISH_LOG`
       SET status = 'FAILED', finished_at = CURRENT_TIMESTAMP(), error_message = @@error.message
     WHERE run_id = v_run_id;
    RAISE USING MESSAGE = CONCAT('sp_publish_unitka_cogs FAILED: ', @@error.message);
  END;
END;
