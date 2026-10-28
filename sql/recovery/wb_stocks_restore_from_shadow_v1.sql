-- ════════════════════════════════════════════════════════════════════════════
-- WB остатки: адресное восстановление пропущенного боевого снимка из shadow (Cloud Run)
-- ════════════════════════════════════════════════════════════════════════════
-- ⚠️ ЗАПИСЬ В PRODUCTION. Выполнять ТОЛЬКО по OWNER ACK, для одной даты за запуск.
--
-- Повод: 28.10.2026 Apps Script пропустил снимок (`STOCKS: SKIPPED_LOCKED`, общий
-- ScriptLock, 03:23:16 UTC); WB не отдаёт остатки за прошедшие сутки. Shadow
-- `wb-stocks-shadow` (Cloud Run, PR-Mig1c) снял те же сутки в 03:30 UTC.
--
-- Доказательство эквивалентности (live, 28.10.2026, SELECT):
--   RAW_WB_STOCKS ↔ RAW_WB_STOCKS__CR за 01.09–27.10: 56 общих суток, 1 171 ключ
--   (nm_id, chrt_id, warehouse_id, warehouse_name) — 0 расхождений quantity /
--   in_way_to_client / in_way_from_client / is_aggregate_warehouse; дублей 0 с обеих сторон.
--   Shadow 28.10: 22 строки, 22 nm_id, 1 106 шт., manifest COMPLETE, control OK, duplicate_keys 0.
--   T5 (RAW_WB_STOCKS_T5): паритет 23–27.10, кроме 24.10 (9 399 против 9 400) —
--   снимки сняты с разницей ~7 мин; T5 — контрольный контур, не денежный.
--
-- Идемпотентность: при любой боевой строке/COMPLETE-манифесте за дату — ASSERT,
-- ничего не пишется. Повторный запуск после успеха падает на том же ASSERT.
-- Провенанс: snapshot_id сохраняется (`STOCK_SNAP_shadow_<yyyymmdd>`), load_id получает
-- префикс `RESTORE_FROM_SHADOW_`, error_message манифеста — отметку восстановления.
--
-- Откат (тоже по ACK): DELETE FROM RAW_WB_STOCKS / RAW_WB_STOCKS_T5 WHERE
--   _snapshot_date = p_date AND load_id LIKE 'RESTORE_FROM_SHADOW_%';
--   DELETE FROM WB_STOCKS_SNAPSHOTS WHERE snapshot_id = 'STOCK_SNAP_shadow_' || FORMAT_DATE('%Y%m%d', p_date)
--   AND error_message LIKE 'RESTORED_FROM_SHADOW%';
--
-- После записи: защита Apps Script `wbStocksBqCompleteSnapshotExists_` (PR fix/wb-stocks-lock-retry)
-- не даст второму производителю записать ту же дату. До её развёртывания — не запускать
-- Apps Script-снимок вручную за восстановленную дату.
-- ════════════════════════════════════════════════════════════════════════════

DECLARE p_date DATE DEFAULT DATE '2026-10-28';
DECLARE p_snapshot_id STRING DEFAULT CONCAT('STOCK_SNAP_shadow_', FORMAT_DATE('%Y%m%d', p_date));
DECLARE p_load_id STRING DEFAULT CONCAT('RESTORE_FROM_SHADOW_', FORMAT_DATE('%Y%m%d', p_date));

-- ── 1. Предусловия (только чтение) ──────────────────────────────────────────
ASSERT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_STOCKS` WHERE _snapshot_date = p_date) = 0
  AS 'RESTORE: боевой RAW за дату уже есть — восстановление не нужно';
ASSERT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_raw.WB_STOCKS_SNAPSHOTS`
        WHERE period_from = FORMAT_DATE('%Y-%m-%d', p_date) AND status = 'COMPLETE') = 0
  AS 'RESTORE: боевой COMPLETE-манифест за дату уже есть';
ASSERT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_raw.WB_STOCKS_SNAPSHOTS__CR`
        WHERE snapshot_id = p_snapshot_id AND status = 'COMPLETE' AND duplicate_keys = 0
          AND written_rows = expected_rows AND period_from = FORMAT_DATE('%Y-%m-%d', p_date)) = 1
  AS 'RESTORE: shadow-манифест за дату не COMPLETE или неполный';
ASSERT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_STOCKS__CR`
        WHERE _snapshot_date = p_date AND snapshot_id = p_snapshot_id)
     = (SELECT written_rows FROM `project-fa311fc0-4d87-4781-986.wb_raw.WB_STOCKS_SNAPSHOTS__CR` WHERE snapshot_id = p_snapshot_id)
  AS 'RESTORE: число строк shadow RAW не равно манифесту';
ASSERT (SELECT COUNT(*) - COUNT(DISTINCT FORMAT('%t|%t|%t|%t', nm_id, chrt_id, warehouse_id, IFNULL(warehouse_name, '')))
        FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_STOCKS__CR` WHERE _snapshot_date = p_date) = 0
  AS 'RESTORE: дубли ключа в shadow RAW';

-- ── 2. Запись одной транзакцией ─────────────────────────────────────────────
BEGIN TRANSACTION;

INSERT INTO `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_STOCKS`
  (load_id, snapshot_id, snapshot_ts, source_api, nm_id, chrt_id, warehouse_id, warehouse_name, region_name,
   quantity, in_way_to_client, in_way_from_client, is_aggregate_warehouse, internal_sku, sku_match_status,
   raw_json, _snapshot_date, warehouse_code)
SELECT p_load_id, snapshot_id, snapshot_ts, source_api, nm_id, chrt_id, warehouse_id, warehouse_name, region_name,
   quantity, in_way_to_client, in_way_from_client, is_aggregate_warehouse, internal_sku, sku_match_status,
   raw_json, _snapshot_date, warehouse_code
FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_STOCKS__CR`
WHERE _snapshot_date = p_date AND snapshot_id = p_snapshot_id;

INSERT INTO `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_STOCKS_T5`
  (load_id, snapshot_id, snapshot_ts, source_api, nm_id, barcode, tech_size, vendor_code, volume,
   warehouse_name, row_type, quantity, internal_sku, sku_match_status, _snapshot_date)
SELECT p_load_id, snapshot_id, snapshot_ts, source_api, nm_id, barcode, tech_size, vendor_code, volume,
   warehouse_name, row_type, quantity, internal_sku, sku_match_status, _snapshot_date
FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_STOCKS_T5__CR`
WHERE _snapshot_date = p_date AND snapshot_id = p_snapshot_id;

INSERT INTO `project-fa311fc0-4d87-4781-986.wb_raw.WB_STOCKS_SNAPSHOTS`
  (snapshot_id, started_at, completed_at, status, period_from, period_to, expected_rows, written_rows,
   distinct_keys, duplicate_keys, unique_nm_ids, warehouses_count, qty_positive_rows, qty_zero_rows,
   aggregate_warehouse_rows, sum_quantity_all_t6, sum_quantity_physical_t6, t5_control_sum, control_status,
   control_delta, unmatched_nm_ids, error_message, anonymized_warehouse_rows, t5_wb_rf_sum, t5_named_sum,
   t5_rows_written, t6_comparable, availability_gap, availability_gap_prev, availability_gap_delta,
   availability_ratio, availability_status)
SELECT snapshot_id, started_at, completed_at, status, period_from, period_to, expected_rows, written_rows,
   distinct_keys, duplicate_keys, unique_nm_ids, warehouses_count, qty_positive_rows, qty_zero_rows,
   aggregate_warehouse_rows, sum_quantity_all_t6, sum_quantity_physical_t6, t5_control_sum, control_status,
   control_delta, unmatched_nm_ids,
   CONCAT('RESTORED_FROM_SHADOW ', FORMAT_TIMESTAMP('%FT%TZ', CURRENT_TIMESTAMP()), ' (Apps Script SKIPPED_LOCKED)'),
   anonymized_warehouse_rows, t5_wb_rf_sum, t5_named_sum,
   t5_rows_written, t6_comparable, availability_gap, availability_gap_prev, availability_gap_delta,
   availability_ratio, availability_status
FROM `project-fa311fc0-4d87-4781-986.wb_raw.WB_STOCKS_SNAPSHOTS__CR`
WHERE snapshot_id = p_snapshot_id;

-- ── 3. Readback внутри транзакции ───────────────────────────────────────────
ASSERT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_STOCKS` WHERE _snapshot_date = p_date)
     = (SELECT written_rows FROM `project-fa311fc0-4d87-4781-986.wb_raw.WB_STOCKS_SNAPSHOTS__CR` WHERE snapshot_id = p_snapshot_id)
  AS 'RESTORE: после записи число строк не совпало — откат транзакции';
ASSERT (SELECT SUM(quantity) FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_STOCKS` WHERE _snapshot_date = p_date)
     = (SELECT SUM(quantity) FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_STOCKS__CR` WHERE _snapshot_date = p_date AND snapshot_id = p_snapshot_id)
  AS 'RESTORE: после записи сумма quantity не совпала — откат транзакции';
ASSERT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_raw.WB_STOCKS_SNAPSHOTS`
        WHERE period_from = FORMAT_DATE('%Y-%m-%d', p_date) AND status = 'COMPLETE') = 1
  AS 'RESTORE: COMPLETE-манифест за дату не ровно один — откат транзакции';

COMMIT TRANSACTION;
