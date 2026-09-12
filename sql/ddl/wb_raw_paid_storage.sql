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

-- Дневная агрегация по SKU — то, что читает юнитка.
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_STORAGE_DAILY` AS
SELECT
  date_msk,
  nm_id,
  ROUND(SUM(warehouse_price), 4) AS storage_rub,
  SUM(barcodes_count)            AS units_stored,
  COUNT(DISTINCT warehouse)      AS warehouses,
  MAX(observed_at)               AS observed_at
FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_PAID_STORAGE`
GROUP BY date_msk, nm_id;

-- Обязательная сверка: сумма по SKU против факта WB из финансового отчёта.
-- Аллокация не нужна вовсе — WB отдаёт хранение уже в разрезе nmID.
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_STORAGE_RECONCILIATION` AS
WITH ps AS (
  SELECT date_msk, SUM(warehouse_price) AS storage_api
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_PAID_STORAGE`
  GROUP BY date_msk
),
fin AS (
  SELECT _rr_date AS date_msk,
         SUM(SAFE_CAST(REPLACE(REPLACE(storage_fee, ' ', ''), ',', '.') AS FLOAT64)) AS storage_finance
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FINANCE_CANONICAL`
  GROUP BY _rr_date
)
SELECT
  COALESCE(ps.date_msk, fin.date_msk)                       AS date_msk,
  ROUND(ps.storage_api, 2)                                  AS storage_api,
  ROUND(fin.storage_finance, 2)                             AS storage_finance,
  ROUND(IFNULL(ps.storage_api,0) - IFNULL(fin.storage_finance,0), 2) AS delta,
  CASE
    WHEN ps.storage_api IS NULL  THEN 'NO_API_DATA'
    WHEN fin.storage_finance IS NULL THEN 'NO_FINANCE_DATA'
    WHEN ABS(ps.storage_api - fin.storage_finance) <= 1 THEN 'OK'
    ELSE 'MISMATCH'
  END AS status
FROM ps FULL OUTER JOIN fin USING (date_msk);
