-- UNITKA 2.0 R2 — воронка продаж WB. Источник с R7: POST /api/analytics/v3/sales-funnel/products/history
-- (openCount -> open_card_count, cartCount -> add_to_cart_count; отмен в методе нет — NULL).
-- Слой RAW хранит ОРИГИНАЛЬНЫЕ значения API; дедупликация и «последнее наблюдение»
-- живут в витрине, а не в таблице: WB пересчитывает воронку задним числом, и схлопывание
-- на приёме уничтожило бы историю пересчётов.

CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_FUNNEL_DAILY` (
  observation_id           STRING  NOT NULL,
  environment              STRING  NOT NULL,
  run_id                   STRING,
  observed_at              TIMESTAMP NOT NULL,
  date_msk                 DATE    NOT NULL,   -- дата в Europe/Moscow, как запрошено у WB
  nm_id                    INT64   NOT NULL,
  open_card_count          INT64,              -- Переходы в карточку
  add_to_cart_count        INT64,              -- Положили в корзину
  orders_count             INT64,
  orders_sum_rub           NUMERIC,
  buyouts_count            INT64,
  buyouts_sum_rub          NUMERIC,
  cancel_count             INT64,
  cancel_sum_rub           NUMERIC,
  add_to_cart_conversion   NUMERIC,
  cart_to_order_conversion NUMERIC,
  buyout_percent           NUMERIC,
  add_to_wishlist          INT64,
  source_endpoint          STRING,
  raw_row_json             STRING,
  ingested_at              TIMESTAMP
)
PARTITION BY date_msk
CLUSTER BY nm_id, observation_id;

-- Манифест наблюдений: freshness, coverage, дрейф схемы, ошибки.
CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.wb_raw.WB_FUNNEL_OBSERVATIONS` (
  observation_id        STRING NOT NULL,
  window_start          DATE,
  window_end            DATE,
  environment           STRING,
  run_id                STRING,
  started_at            TIMESTAMP,
  observed_at           TIMESTAMP,
  completed_at          TIMESTAMP,
  status                STRING,      -- STARTED | COMPLETE | REUSED | ERROR
  rows_fetched          INT64,
  rows_written          INT64,
  rows_rejected         INT64,
  days_covered          INT64,
  days_expected         INT64,
  nm_covered            INT64,
  http_status           INT64,
  poll_attempts         INT64,
  schema_status         STRING,      -- OK | DRIFT_UNKNOWN_FIELDS
  schema_unknown_fields STRING,
  error_code            STRING,
  error_message         STRING
);

-- Нормализованная дневная сущность. Грейн: (date_msk, nm_id). Последнее наблюдение.
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FUNNEL_DAILY` AS
SELECT * EXCEPT(rn) FROM (
  SELECT
    date_msk, nm_id,
    open_card_count, add_to_cart_count, orders_count, orders_sum_rub,
    buyouts_count, buyouts_sum_rub, cancel_count, cancel_sum_rub,
    add_to_cart_conversion, cart_to_order_conversion, buyout_percent, add_to_wishlist,
    observed_at, observation_id, environment,
    ROW_NUMBER() OVER (PARTITION BY date_msk, nm_id ORDER BY observed_at DESC) AS rn
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_FUNNEL_DAILY`
  WHERE environment = 'prod'
) WHERE rn = 1;

-- Покрытие: какие дни закрыты, а какие — дыра. Пустая строка в источнике и
-- отсутствие строки — РАЗНЫЕ вещи, поэтому дни берём из календаря, а не из данных.
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FUNNEL_COVERAGE` AS
WITH cal AS (
  SELECT d AS date_msk
  FROM UNNEST(GENERATE_DATE_ARRAY(
    (SELECT MIN(date_msk) FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_FUNNEL_DAILY`),
    DATE_SUB(CURRENT_DATE('Europe/Moscow'), INTERVAL 1 DAY))) AS d
)
SELECT
  c.date_msk,
  COUNT(DISTINCT f.nm_id)                       AS nm_with_data,
  SUM(f.open_card_count)                        AS open_card_total,
  SUM(f.add_to_cart_count)                      AS add_to_cart_total,
  MAX(f.observed_at)                            AS last_observed_at,
  IF(COUNT(f.nm_id) = 0, 'MISSING', 'OK')       AS status
FROM cal c
LEFT JOIN `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FUNNEL_DAILY` f USING (date_msk)
GROUP BY c.date_msk
ORDER BY c.date_msk;
