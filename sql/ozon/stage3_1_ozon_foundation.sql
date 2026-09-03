-- Stage 3.1 — фундамент домена Ozon.
-- MARKETPLACE ISOLATION: ни одна таблица не читает и не пишет wb_raw / wb_mart.
-- Единственная разрешённая внешняя зависимость домена — evetis_ref.
-- Location: EU (совпадает с существующими датасетами проекта).
--
-- Служебные колонки ingestion во всех RAW-таблицах:
--   extracted_at       момент извлечения из API
--   source_event_time  время события в терминах источника
--   source_endpoint    endpoint и его версия
--   ingestion_run_id   идентификатор прогона, для replay и идемпотентности
--   source_payload_hash хеш исходной полезной нагрузки, где применимо

CREATE SCHEMA IF NOT EXISTS `project-fa311fc0-4d87-4781-986.ozon_raw`
  OPTIONS(location='EU', description='Домен Ozon, сырой слой. Не читает и не пишет датасеты WB');
CREATE SCHEMA IF NOT EXISTS `project-fa311fc0-4d87-4781-986.ozon_stg`
  OPTIONS(location='EU', description='Домен Ozon, слой нормализации');
CREATE SCHEMA IF NOT EXISTS `project-fa311fc0-4d87-4781-986.ozon_mart`
  OPTIONS(location='EU', description='Домен Ozon, витрины. Кросс-маркетплейсный слой здесь не создаётся');

-- ------------------------------------------------------------ 1. Каталог
CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_CATALOG`
(
  snapshot_date DATE NOT NULL, sku STRING NOT NULL, product_id STRING, offer_id STRING,
  name STRING, is_archived BOOL, has_stock BOOL, stock_present INT64, stock_reserved INT64,
  visibility_has_price BOOL, visibility_has_stock BOOL, status_name STRING,
  price_index_color STRING OPTIONS(description="Нормализовано: COLOR_INDEX_RED и RED сводятся к одному значению"),
  description_category_id INT64, type_id INT64, created_at TIMESTAMP, updated_at TIMESTAMP,
  extracted_at TIMESTAMP NOT NULL, source_event_time TIMESTAMP, source_endpoint STRING NOT NULL,
  ingestion_run_id STRING NOT NULL, source_payload_hash STRING
)
PARTITION BY snapshot_date CLUSTER BY sku
OPTIONS(description="Снимок каталога Ozon. PK (snapshot_date, sku). MERGE по PK");

-- ------------------------------------------------------ 2. Цены продавца
CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PRICES`
(
  snapshot_ts TIMESTAMP NOT NULL, snapshot_date DATE NOT NULL, offer_id STRING NOT NULL,
  product_id STRING, price_rub NUMERIC, old_price_rub NUMERIC, min_price_rub NUMERIC,
  marketing_seller_price_rub NUMERIC, net_price_rub NUMERIC OPTIONS(description="Себестоимость из кабинета Ozon. Только для сверки; source of truth — evetis_ref"),
  acquiring_rub NUMERIC, sales_percent_fbo NUMERIC,
  price_index_color STRING, external_min_price_rub NUMERIC, external_index_value NUMERIC,
  ozon_actions_exist BOOL,
  extracted_at TIMESTAMP NOT NULL, source_endpoint STRING NOT NULL,
  ingestion_run_id STRING NOT NULL, source_payload_hash STRING
)
PARTITION BY snapshot_date CLUSTER BY offer_id
OPTIONS(description="Снимок цен продавца. Цены на полке здесь НЕТ. PK (snapshot_ts, offer_id). Retention 400 дней");

-- ---------------------------------------------------------- 3. Остатки
CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_STOCKS`
(
  snapshot_date DATE NOT NULL, sku STRING NOT NULL, warehouse_id STRING NOT NULL,
  warehouse_name STRING, cluster_id STRING, cluster_name STRING,
  available_stock_count INT64, valid_stock_count INT64, transit_stock_count INT64,
  excess_stock_count INT64, days_without_sales INT64, turnover_grade STRING, idc NUMERIC,
  extracted_at TIMESTAMP NOT NULL, source_endpoint STRING NOT NULL,
  ingestion_run_id STRING NOT NULL, source_payload_hash STRING
)
PARTITION BY snapshot_date CLUSTER BY sku, warehouse_id
OPTIONS(description="Снимок остатков. Историю задним числом восстанавливать ЗАПРЕЩЕНО. PK (snapshot_date, sku, warehouse_id)");

-- ------------------------------------------------------- 4. Заказы FBO
CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_POSTINGS_FBO`
(
  posting_number STRING NOT NULL, sku STRING NOT NULL, order_date DATE NOT NULL,
  order_id INT64, order_number STRING, status STRING, substatus STRING,
  created_at TIMESTAMP, in_process_at TIMESTAMP, cancel_reason_id INT64,
  quantity INT64, price_rub NUMERIC, old_price_rub NUMERIC,
  total_discount_value_rub NUMERIC, commission_amount_rub NUMERIC, payout_rub NUMERIC,
  actions ARRAY<STRING>, warehouse_name STRING, city STRING,
  extracted_at TIMESTAMP NOT NULL, source_event_time TIMESTAMP, source_endpoint STRING NOT NULL,
  ingestion_run_id STRING NOT NULL, source_payload_hash STRING
)
PARTITION BY order_date CLUSTER BY posting_number, sku
OPTIONS(description="Заказы FBO. Только /v3/posting/fbo/list; /v2 запрещён. PK (posting_number, sku). MERGE, окно перезабора 30 дней: статус меняется после создания");

-- ------------------------------------------------------ 5. Начисления
CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_FINANCE_ACCRUAL`
(
  event_date DATE NOT NULL, accrual_id INT64 NOT NULL, type_id INT64 NOT NULL,
  operation_name STRING, accrued_category STRING,
  unit_number STRING OPTIONS(description="МНОГОЗНАЧЕН: типы 41 и 54 = campaign_id; категория POSTING = posting_number; ITEM = ссылка на заказ. Трактовать по типу"),
  unit_number_meaning STRING, posting_number STRING, sku STRING,
  amount_rub NUMERIC, currency STRING,
  seller_base_price_rub NUMERIC, buyer_paid_price_rub NUMERIC,
  ozon_bonus_rub NUMERIC, ozon_coinvestment_rub NUMERIC,
  commission_rub NUMERIC, commission_ratio NUMERIC OPTIONS(description="Фактическая ставка комиссии на уровне операции. Не выводить из предполагаемой даты тарифа"),
  quantity INT64,
  extracted_at TIMESTAMP NOT NULL, source_endpoint STRING NOT NULL,
  ingestion_run_id STRING NOT NULL, source_payload_hash STRING
)
PARTITION BY event_date CLUSTER BY type_id, sku
OPTIONS(description="Начисления Ozon. PK (accrual_id, type_id, sku). Поля spp_pct НЕТ намеренно: SPP_SEMANTICS = NOT_PROVEN. MERGE, окно перезабора 14 дней");

-- ------------------------------------------------------- 6. Кампании
CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_ADS_CAMPAIGNS`
(
  snapshot_date DATE NOT NULL, campaign_id STRING NOT NULL, title STRING, state STRING,
  adv_object_type STRING, payment_type STRING, expense_strategy STRING OPTIONS(description="Исторический enum, удалён из запросов API. Расход ограничивает weekly_budget"),
  placement STRING, weekly_budget_rub NUMERIC OPTIONS(description="Уже переведено из микрорублей делением на 1e6"),
  daily_budget_rub NUMERIC OPTIONS(description="Поле помечено deprecated в API"),
  product_autopilot_strategy STRING, autostop_status STRING,
  campaign_created_at TIMESTAMP, campaign_updated_at TIMESTAMP,
  extracted_at TIMESTAMP NOT NULL, source_endpoint STRING NOT NULL,
  ingestion_run_id STRING NOT NULL, source_payload_hash STRING
)
PARTITION BY snapshot_date CLUSTER BY campaign_id
OPTIONS(description="Снимок кампаний. Реестр ОБЯЗАН покрывать архивные: исторический рекламный периметр не равен текущему. PK (snapshot_date, campaign_id)");

-- -------------------------------------------- 7. Подневный расход кампании
CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_ADS_EXPENSE_DAILY`
(
  date DATE NOT NULL, campaign_id STRING NOT NULL, campaign_title STRING,
  expense_rub NUMERIC, bonus_expense_rub NUMERIC, subscription_expense_rub NUMERIC,
  impressions INT64, clicks INT64, orders INT64, revenue_rub NUMERIC,
  extracted_at TIMESTAMP NOT NULL, source_endpoint STRING NOT NULL,
  ingestion_run_id STRING NOT NULL, source_payload_hash STRING
)
PARTITION BY date CLUSTER BY campaign_id
OPTIONS(description="Подневная статистика кампаний. Единицы РУБЛИ, делитель 1e6 не применять. Три денежные колонки хранятся раздельно. PK (date, campaign_id)");

-- --------------------------------------- 8. Подневный расход по SKU кампании
CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_ADS_SKU_DAILY`
(
  date DATE NOT NULL, campaign_id STRING NOT NULL, sku STRING NOT NULL,
  attributed_spend_rub NUMERIC OPTIONS(description="Расход с НДС из асинхронного отчёта Performance API"),
  impressions INT64, clicks INT64, cart_adds INT64, orders INT64,
  revenue_promo_rub NUMERIC, ordered_total_rub NUMERIC,
  drr_promo_pct NUMERIC, drr_total_pct NUMERIC,
  attribution_status STRING NOT NULL OPTIONS(description="ATTRIBUTED_ACTUAL | CAMPAIGN_ONLY | NOT_AVAILABLE. Остаток без атрибуции не распределяется"),
  extracted_at TIMESTAMP NOT NULL, source_endpoint STRING NOT NULL,
  ingestion_run_id STRING NOT NULL, source_payload_hash STRING
)
PARTITION BY date CLUSTER BY campaign_id, sku
OPTIONS(description="SKU-разрез рекламного расхода из асинхронного отчёта Performance API. Пропорциональное распределение ЗАПРЕЩЕНО. PK (date, campaign_id, sku)");
