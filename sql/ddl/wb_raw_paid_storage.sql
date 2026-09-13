-- UNITKA 2.0 R3 — фактическое платное хранение WB по дням и nmID.
-- Создано и заполнено 10.09.2026: 3352 строки, 22 nmID, 01–09.09.2026.
-- Сверка с финансовым отчётом за то же окно: 1183,40 ₽ против 1183,37 ₽, дельта 0,03 ₽.

CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_PAID_STORAGE` (
  observation_id     STRING,
  run_id             STRING,
  observed_at        TIMESTAMP,
  date_msk           DATE,
  nm_id              INT64,
  chrt_id            INT64,
  barcode            STRING,
  warehouse          STRING,
  office_id          INT64,
  warehouse_coef     NUMERIC,
  log_warehouse_coef NUMERIC,
  subject            STRING,
  brand              STRING,
  vendor_code        STRING,
  volume             NUMERIC,
  calc_type          STRING,
  warehouse_price    NUMERIC,   -- стоимость хранения строки, ₽
  barcodes_count     INT64,
  pallet_place_code  INT64,
  pallet_count       NUMERIC,
  loyalty_discount   NUMERIC,
  tariff_fix_date    STRING,
  tariff_lower_date  STRING,
  raw_row_json       STRING,
  ingested_at        TIMESTAMP
)
PARTITION BY date_msk
CLUSTER BY nm_id, warehouse;

-- Схема таблицы дублирует infra/terraform/wb_paid_storage_loader.tf (Stage E4, commit 230c64c) —
-- production-загрузчик Job wb-paid-storage-prod (atomic window replace через RAW_WB_PAID_STORAGE__STAGE).
-- Вью слоя хранения (V_WB_STORAGE_DAILY / _COVERAGE / _RECONCILIATION) определены в
-- sql/unitka/engine_v1_views.sql, раздел 0 — единственное место определений, сверено с live BQ 13.09.2026.
