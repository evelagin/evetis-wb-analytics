-- ============================================================================
-- PR-1 — Wildberries Intraday Price Observer (READ-ONLY)
--
-- Назначение: append-only история наблюдений цен продавца WB.
-- Источник:   GET https://discounts-prices-api.wildberries.ru/api/v2/list/goods/filter
-- Каденс:     20 минут (см. docs/pricing/PR1_WB_PRICE_OBSERVER.md, раздел «Каденс»)
--
-- ЖЁСТКИЕ СВОЙСТВА, которые нельзя нарушать при доработках:
--   1. RAW_WB_PRICES — ТОЛЬКО append. Никакого MERGE по дате, никакого UPDATE
--      цен. Схлопывание наблюдений в сутки уничтожает предмет наблюдения.
--   2. Наблюдение, в котором цена НЕ изменилась, обязано сохраниться. Отсутствие
--      строки означает «наблюдатель не отработал», а не «цена не менялась».
--   3. Цена никогда не приводится к 0 при отсутствии значения. NULL — валидное
--      состояние «источник не дал»; 0 — валидная цена. Их нельзя смешивать.
--
-- Откат: sql/pricing/pr1_wb_price_observer_rollback.sql
-- ============================================================================

-- ── 1. RAW: история наблюдений ──────────────────────────────────────────────
-- Грейн: nm_id × size_id × observed_at.
-- Товар без размеров WB всё равно отдаёт одним элементом sizes[] — грейн держим
-- на размере, чтобы появление размерных цен (editableSizePrice=true) не сломало
-- ключ задним числом.
CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_PRICES`
(
  observed_at            TIMESTAMP NOT NULL OPTIONS(description="Момент наблюдения (UTC). Задаётся ОДИН раз на прогон, одинаков для всех строк снимка."),
  observation_bucket     STRING    NOT NULL OPTIONS(description="Логический период прогона = 20-минутное окно UTC, YYYY-MM-DDTHH:MM. Он же logical_period в LOADER_RUNS."),
  observation_id         STRING    NOT NULL OPTIONS(description="Детерминированный id снимка = f(environment, observation_bucket). Ключ идемпотентности."),
  environment            STRING    NOT NULL OPTIONS(description="shadow | prod"),
  run_id                 STRING    OPTIONS(description="Сквозной id прогона Cloud Run → LOADER_RUNS."),

  nm_id                  INT64     NOT NULL OPTIONS(description="Артикул WB (nmID)."),
  internal_sku           STRING    OPTIONS(description="Канонический SKU EVETIS. NULL = товар отсутствует в REF_SKU_MASTER (нештатно, см. unexpected_products)."),
  vendor_code            STRING    OPTIONS(description="Артикул продавца (vendorCode) — как отдаёт WB, без нормализации."),
  size_id                INT64     OPTIONS(description="sizeID из sizes[]."),
  tech_size_name         STRING    OPTIONS(description="techSizeName из sizes[]."),

  seller_list_price      NUMERIC   OPTIONS(description="sizes[].price — цена продавца ДО скидки продавца. НЕ цена покупателя."),
  seller_discount_pct    NUMERIC   OPTIONS(description="discount — скидка продавца, %. Управляется продавцом."),
  seller_effective_price NUMERIC   OPTIONS(description="sizes[].discountedPrice — цена продавца ПОСЛЕ скидки продавца. НЕ цена покупателя: СПП и акции WB сюда не входят."),
  wb_club_discount_pct   NUMERIC   OPTIONS(description="clubDiscount — скидка WB Club, %. Задаётся и оплачивается ПРОДАВЦОМ, это не СПП."),
  wb_club_price          NUMERIC   OPTIONS(description="sizes[].clubDiscountedPrice — цена для подписчиков WB Club."),
  currency_code          STRING    OPTIONS(description="currencyIsoCode4217."),
  editable_size_price    BOOL      OPTIONS(description="editableSizePrice — разрешены ли размерные цены."),

  raw_item_json          STRING    OPTIONS(description="Элемент listGoods[] как его вернул WB, дословно. Форензика: отвечает на вопрос «WB так отдал или мы так посчитали»."),
  source_endpoint        STRING    OPTIONS(description="Endpoint, породивший строку."),
  source_payload_hash    STRING    OPTIONS(description="sha256 от (observation_id, nm_id, size_id) — стабильный ключ строки снимка."),
  ingested_at            TIMESTAMP OPTIONS(description="Момент записи в BigQuery (UTC). Отличается от observed_at при ретрае.")
)
PARTITION BY DATE(observed_at)
CLUSTER BY nm_id, internal_sku
OPTIONS(
  description="PR-1. Append-only история наблюдений цен продавца WB. Источник: GET /api/v2/list/goods/filter (discounts-prices-api). НЕ содержит цену покупателя и НЕ содержит СПП — их этот endpoint не отдаёт. Никогда не схлопывать по дате.",
  require_partition_filter=false
);

-- ── 2. Манифест наблюдений: покрытие, свежесть, дрейф схемы ─────────────────
-- Одна строка на снимок. Здесь живут run-level факты, которых нет в RAW.
CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.wb_raw.WB_PRICES_OBSERVATIONS`
(
  observation_id        STRING    NOT NULL OPTIONS(description="Детерминированный id снимка = f(environment, observation_bucket)."),
  observation_bucket    STRING    NOT NULL OPTIONS(description="20-минутное окно UTC, YYYY-MM-DDTHH:MM."),
  environment           STRING    NOT NULL,
  run_id                STRING,
  started_at            TIMESTAMP NOT NULL,
  observed_at           TIMESTAMP OPTIONS(description="Момент успешного получения ответа WB. NULL, если ответа не было."),
  completed_at          TIMESTAMP,
  status                STRING    NOT NULL OPTIONS(description="STARTED | COMPLETE | ERROR | REUSED. REUSED = повтор того же снимка, данные уже были записаны."),

  http_status           INT64     OPTIONS(description="Код последнего ответа WB."),
  http_attempts         INT64     OPTIONS(description="Число попыток HTTP (включая ретраи 429/5xx)."),

  expected_products     INT64     OPTIONS(description="Активные WB nm_id из REF_SKU_MASTER на момент прогона."),
  observed_products     INT64     OPTIONS(description="Уникальные nm_id, полученные от WB."),
  missing_products      INT64     OPTIONS(description="Ожидали, но WB не вернул."),
  unexpected_products   INT64     OPTIONS(description="WB вернул, но их нет в справочнике."),
  coverage_pct          NUMERIC   OPTIONS(description="observed_expected / expected × 100. 100 ≠ отсутствие проблем: смотреть также unexpected."),
  missing_nm_ids        STRING    OPTIONS(description="Список отсутствующих nm_id через запятую."),
  unexpected_nm_ids     STRING    OPTIONS(description="Список неожиданных nm_id через запятую."),

  rows_written          INT64,
  schema_status         STRING    OPTIONS(description="OK | DRIFT_NEW_FIELDS | DRIFT_MISSING_FIELDS. Дрейф не роняет прогон, если обязательные поля на месте, но обязан быть виден."),
  schema_unknown_fields STRING    OPTIONS(description="Поля ответа WB, которых нет в нашем контракте. Новое поле цен не должно раствориться молча."),
  error_code            STRING,
  error_message         STRING
)
PARTITION BY DATE(started_at)
CLUSTER BY environment, observation_bucket
OPTIONS(description="PR-1. Манифест наблюдений цен WB: покрытие SKU, свежесть, дрейф схемы, ошибки. Источник операционных сигналов для OPS.");

-- ── 3. Канонический слой: последнее наблюдение на товар ─────────────────────
-- Текущее состояние ВЫВОДИТСЯ из append-only истории, а не хранится отдельно.
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_PRICES_CURRENT` AS
WITH ranked AS (
  SELECT
    p.*,
    ROW_NUMBER() OVER (PARTITION BY p.environment, p.nm_id, p.size_id ORDER BY p.observed_at DESC) AS rn
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_PRICES` p
)
SELECT
  r.environment,
  r.internal_sku,
  r.nm_id,
  r.vendor_code,
  r.size_id,
  r.tech_size_name,
  r.seller_list_price,
  r.seller_discount_pct,
  r.seller_effective_price,
  r.wb_club_discount_pct,
  r.wb_club_price,
  r.currency_code,
  r.editable_size_price,
  r.observed_at,
  TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), r.observed_at, MINUTE) AS observation_age_minutes,
  -- Пороги привязаны к каденсу 20 мин: FRESH = не более двух пропущенных окон + запас.
  CASE
    WHEN r.observed_at IS NULL                                                          THEN 'UNKNOWN'
    WHEN TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), r.observed_at, MINUTE) <= 45               THEN 'FRESH'
    WHEN TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), r.observed_at, MINUTE) <= 90               THEN 'DELAYED'
    ELSE 'STALE'
  END AS freshness_status
FROM ranked r
WHERE r.rn = 1;

-- ── 4. Наблюдённые изменения цены ───────────────────────────────────────────
-- ТОЛЬКО наблюдение. Событие называется PRICE_OBSERVED_CHANGE, а не REPRICE:
-- система фиксирует, что цена изменилась, и не утверждает, что это сделала она.
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_PRICES_OBSERVED_CHANGES` AS
WITH seq AS (
  SELECT
    environment, internal_sku, nm_id, size_id, observed_at,
    seller_list_price, seller_effective_price, seller_discount_pct,
    wb_club_discount_pct, wb_club_price,
    LAG(seller_list_price)      OVER w AS prev_seller_list_price,
    LAG(seller_effective_price) OVER w AS prev_seller_effective_price,
    LAG(seller_discount_pct)    OVER w AS prev_seller_discount_pct,
    LAG(wb_club_discount_pct)   OVER w AS prev_wb_club_discount_pct,
    LAG(wb_club_price)          OVER w AS prev_wb_club_price,
    LAG(observed_at)            OVER w AS prev_observed_at
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_PRICES`
  WINDOW w AS (PARTITION BY environment, nm_id, size_id ORDER BY observed_at)
)
SELECT
  environment, internal_sku, nm_id, size_id,
  prev_observed_at, observed_at,
  TIMESTAMP_DIFF(observed_at, prev_observed_at, MINUTE) AS minutes_since_previous,
  prev_seller_list_price,      seller_list_price,
  prev_seller_effective_price, seller_effective_price,
  prev_seller_discount_pct,    seller_discount_pct,
  prev_wb_club_discount_pct,   wb_club_discount_pct,
  prev_wb_club_price,          wb_club_price,
  seller_list_price      - prev_seller_list_price      AS delta_seller_list_price,
  seller_effective_price - prev_seller_effective_price AS delta_seller_effective_price,
  wb_club_price          - prev_wb_club_price          AS delta_wb_club_price,
  SAFE_DIVIDE(seller_effective_price - prev_seller_effective_price, prev_seller_effective_price) * 100
    AS delta_seller_effective_price_pct,
  'PRICE_OBSERVED_CHANGE' AS event_type
FROM seq
WHERE prev_observed_at IS NOT NULL
  AND (seller_list_price      IS DISTINCT FROM prev_seller_list_price
    OR seller_effective_price IS DISTINCT FROM prev_seller_effective_price
    OR seller_discount_pct    IS DISTINCT FROM prev_seller_discount_pct
    OR wb_club_discount_pct   IS DISTINCT FROM prev_wb_club_discount_pct
    OR wb_club_price          IS DISTINCT FROM prev_wb_club_price);

-- ── 5. Здоровье наблюдателя ─────────────────────────────────────────────────
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_PRICES_OBSERVER_HEALTH` AS
WITH last_ok AS (
  SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_raw.WB_PRICES_OBSERVATIONS`
  WHERE status IN ('COMPLETE', 'REUSED')
  QUALIFY ROW_NUMBER() OVER (PARTITION BY environment ORDER BY started_at DESC) = 1
),
last_any AS (
  SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_raw.WB_PRICES_OBSERVATIONS`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY environment ORDER BY started_at DESC) = 1
),
fails AS (
  -- Подряд идущие неуспехи, начиная с последнего прогона: считаем строки,
  -- до которых (в порядке от свежих к старым) ещё не встретился успех.
  SELECT environment, COUNT(*) AS consecutive_failures
  FROM (
    SELECT environment,
           COUNTIF(status IN ('COMPLETE','REUSED')) OVER (
             PARTITION BY environment ORDER BY started_at DESC
             ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS ok_seen
    FROM `project-fa311fc0-4d87-4781-986.wb_raw.WB_PRICES_OBSERVATIONS`
  )
  WHERE ok_seen = 0
  GROUP BY environment
)
SELECT
  a.environment,
  o.observation_id             AS last_success_observation_id,
  o.observed_at                AS last_success_observed_at,
  o.coverage_pct               AS last_success_coverage_pct,
  o.expected_products, o.observed_products, o.missing_products, o.unexpected_products,
  o.missing_nm_ids, o.unexpected_nm_ids,
  o.rows_written               AS last_success_rows_written,
  o.schema_status              AS last_success_schema_status,
  o.schema_unknown_fields,
  a.status                     AS last_attempt_status,
  a.started_at                 AS last_attempt_started_at,
  a.http_status                AS last_attempt_http_status,
  a.error_code, a.error_message,
  IFNULL(f.consecutive_failures, 0) AS consecutive_failures,
  TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), o.observed_at, MINUTE) AS snapshot_age_minutes,
  CASE
    WHEN o.observed_at IS NULL                                                    THEN 'UNKNOWN'
    WHEN TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), o.observed_at, MINUTE) <= 45         THEN 'FRESH'
    WHEN TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), o.observed_at, MINUTE) <= 90         THEN 'DELAYED'
    ELSE 'STALE'
  END AS freshness_status,
  -- Полное покрытие и отсутствие дрейфа — разные вещи; здоровье требует обоих.
  CASE
    WHEN o.observation_id IS NULL                        THEN 'UNKNOWN'
    WHEN IFNULL(f.consecutive_failures, 0) >= 3          THEN 'RED'
    WHEN TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), o.observed_at, MINUTE) > 90 THEN 'RED'
    WHEN o.coverage_pct < 100
      OR o.unexpected_products > 0
      OR o.schema_status != 'OK'
      OR IFNULL(f.consecutive_failures, 0) > 0
      OR TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), o.observed_at, MINUTE) > 45 THEN 'YELLOW'
    ELSE 'GREEN'
  END AS health_status,
  CURRENT_TIMESTAMP() AS generated_at
FROM last_any a
LEFT JOIN last_ok o USING (environment)
LEFT JOIN fails   f USING (environment);

-- ── 6. Регистрация в OPS ────────────────────────────────────────────────────
-- ⚠️ ВЫПОЛНЯТЬ НА ШАГЕ ДЕПЛОЯ, а не вместе с DDL.
-- Строка реестра объявляет пайплайн ожидаемым: детекторы здоровья начнут требовать
-- прогоны. Пока Cloud Scheduler на паузе, это дало бы ложный инцидент.
--
-- freshness_sla_minutes = 45 — два окна по 20 минут плюс запас; те же пороги
-- зашиты в V_WB_PRICES_OBSERVER_HEALTH и V_WB_PRICES_CURRENT.
-- is_snapshot_only = TRUE и missed_run_data_loss = TRUE: у WB нет эндпоинта истории
-- цен, поэтому пропущенное окно невосстановимо — ровно как у ads_query_bids.
/*
INSERT INTO `project-fa311fc0-4d87-4781-986.wb_ops.OPS_PIPELINE_REGISTRY`
(pipeline_id, pipeline_name, parent_pipeline_id, source_system, target_object,
 environment, enabled, criticality, cadence_type, cadence_spec,
 cadence_definition_source, runtime_verification_status, cadence_observed_evidence,
 is_failure_isolated, is_snapshot_only, is_backfillable, missed_run_data_loss,
 freshness_sla_minutes, stale_run_threshold_minutes, expected_business_lag_days,
 grace_period_minutes, reminder_interval_hours, run_log_source, coverage_check_enabled,
 owner, notes, updated_at)
VALUES
('wb_prices_observer', 'Наблюдатель цен WB (PR-1)', NULL, 'WB_API',
 'wb_raw.RAW_WB_PRICES', 'prod', TRUE, 'HIGH', 'INTERVAL_WORKER', '*/20 * * * * Etc/UTC',
 'TERRAFORM', 'NOT_VERIFIED', 'infra/terraform/wb_prices_observer.tf — ДЕКЛАРАЦИЯ',
 FALSE, TRUE, FALSE, TRUE,
 45, 60, 0, 20, 12, 'wb_raw.WB_PRICES_OBSERVATIONS', TRUE,
 'evelagin',
 'SNAPSHOT-ONLY. У WB нет эндпоинта истории цен: пропущенное окно потеряно навсегда. READ-ONLY: мутирующих методов в коде нет.',
 CURRENT_TIMESTAMP());
*/
