-- ════════════════════════════════════════════════════════════════════════════
-- WB остатки: адресное восстановление пропущенного боевого снимка из shadow (Cloud Run)
-- ════════════════════════════════════════════════════════════════════════════
-- ⚠️ ЗАПИСЬ В PRODUCTION. Выполнять ТОЛЬКО по OWNER ACK, для ОДНОЙ даты за запуск (p_date).
--
-- Повод: 28.10.2026 Apps Script пропустил снимок (`STOCKS: SKIPPED_LOCKED`, общий
-- ScriptLock, 03:23:16 UTC); WB не отдаёт остатки за прошедшие сутки. Shadow
-- `wb-stocks-shadow` (Cloud Run, PR-Mig1c) снял те же сутки в 03:30 UTC.
--
-- Доказательство эквивалентности (live, 28.10.2026, SELECT):
--   RAW_WB_STOCKS ↔ RAW_WB_STOCKS__CR за 01.09–27.10: 56 общих суток, 1 171 ключ
--   (nm_id, chrt_id, warehouse_id, warehouse_name) — 0 расхождений quantity /
--   in_way_to_client / in_way_from_client / is_aggregate_warehouse; дублей 0 с обеих сторон.
--   Схемы RAW_WB_STOCKS и __CR идентичны (18 колонок); T5 и T5__CR — идентичны (15 колонок).
--   Shadow 28.10: 22 строки, 22 nm_id, 1 106 шт., manifest COMPLETE, control OK, duplicate_keys 0.
--   T5 (RAW_WB_STOCKS_T5): паритет 23–27.10, кроме 24.10 (9 399 против 9 400) —
--   снимки сняты с разницей ~7 мин; T5 — контрольный контур, не денежный.
--
-- Граница записи: только партиция `_snapshot_date = p_date` трёх таблиц (RAW, RAW_T5, manifest).
-- Источник (`__CR`) только читается. Идемпотентность: при любой боевой строке за дату в RAW или
-- RAW_T5, либо COMPLETE-манифесте — ASSERT до записи, ничего не пишется; повтор после успеха
-- падает на том же ASSERT. Запись — одна транзакция; любая ошибка внутри → ROLLBACK.
-- Провенанс: snapshot_id сохраняется (`STOCK_SNAP_shadow_<yyyymmdd>`), load_id получает
-- префикс `RESTORE_FROM_SHADOW_`, error_message манифеста — отметку восстановления.
--
-- Откат (тоже по ACK, та же дата):
--   DELETE FROM wb_raw.RAW_WB_STOCKS    WHERE _snapshot_date = p_date AND load_id = p_load_id;
--   DELETE FROM wb_raw.RAW_WB_STOCKS_T5 WHERE _snapshot_date = p_date AND load_id = p_load_id;
--   DELETE FROM wb_raw.WB_STOCKS_SNAPSHOTS WHERE snapshot_id = p_snapshot_id AND STARTS_WITH(error_message, 'RESTORED_FROM_SHADOW');
--
-- До развёртывания защиты Apps Script (PR #386, `wbStocksBqCompleteSnapshotExists_`) не запускать
-- Apps Script-снимок вручную за восстановленную дату. Плановый триггер пишет только «сегодня».
-- ════════════════════════════════════════════════════════════════════════════

DECLARE p_date DATE DEFAULT DATE '2026-10-28';
DECLARE p_snapshot_id STRING DEFAULT CONCAT('STOCK_SNAP_shadow_', FORMAT_DATE('%Y%m%d', p_date));
DECLARE p_load_id STRING DEFAULT CONCAT('RESTORE_FROM_SHADOW_', FORMAT_DATE('%Y%m%d', p_date));
DECLARE n_raw INT64;
DECLARE n_t5 INT64;
DECLARE qty_raw INT64;
DECLARE total_raw_before INT64;
DECLARE total_t5_before INT64;
DECLARE total_manifest_before INT64;

-- ── 1. Предусловия (только чтение; при провале ничего не записано) ──────────
ASSERT p_date <= CURRENT_DATE('Europe/Moscow')
  AS 'RESTORE: дата из будущего';
ASSERT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_STOCKS` WHERE _snapshot_date = p_date) = 0
  AS 'RESTORE: боевой RAW за дату уже есть — восстановление не нужно';
ASSERT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_STOCKS_T5` WHERE _snapshot_date = p_date) = 0
  AS 'RESTORE: боевой RAW_T5 за дату уже есть — восстановление не нужно';
ASSERT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_raw.WB_STOCKS_SNAPSHOTS`
        WHERE period_from = FORMAT_DATE('%Y-%m-%d', p_date) AND status = 'COMPLETE') = 0
  AS 'RESTORE: боевой COMPLETE-манифест за дату уже есть';
ASSERT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_raw.WB_STOCKS_SNAPSHOTS` WHERE snapshot_id = p_snapshot_id) = 0
  AS 'RESTORE: манифест с этим snapshot_id уже есть';
ASSERT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_raw.WB_STOCKS_SNAPSHOTS__CR`
        WHERE snapshot_id = p_snapshot_id AND status = 'COMPLETE' AND duplicate_keys = 0
          AND written_rows = expected_rows AND period_from = FORMAT_DATE('%Y-%m-%d', p_date)
          AND period_to = FORMAT_DATE('%Y-%m-%d', p_date)) = 1
  AS 'RESTORE: shadow-манифест за дату не COMPLETE, неполный или не за эту дату';

SET (n_raw, qty_raw) = (SELECT AS STRUCT COUNT(*), SUM(quantity) FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_STOCKS__CR`
                        WHERE _snapshot_date = p_date AND snapshot_id = p_snapshot_id);
SET n_t5 = (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_STOCKS_T5__CR`
            WHERE _snapshot_date = p_date AND snapshot_id = p_snapshot_id);

ASSERT n_raw > 0 AND n_raw = (SELECT written_rows FROM `project-fa311fc0-4d87-4781-986.wb_raw.WB_STOCKS_SNAPSHOTS__CR` WHERE snapshot_id = p_snapshot_id)
  AS 'RESTORE: число строк shadow RAW не равно манифесту';
ASSERT n_raw = (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_STOCKS__CR` WHERE _snapshot_date = p_date)
  AS 'RESTORE: в shadow за дату есть строки другого snapshot_id — неоднозначно';
ASSERT qty_raw = (SELECT sum_quantity_all_t6 FROM `project-fa311fc0-4d87-4781-986.wb_raw.WB_STOCKS_SNAPSHOTS__CR` WHERE snapshot_id = p_snapshot_id)
  AS 'RESTORE: сумма quantity shadow RAW не равна манифесту';
ASSERT (SELECT COUNT(*) - COUNT(DISTINCT FORMAT('%t|%t|%t|%t', nm_id, chrt_id, warehouse_id, IFNULL(warehouse_name, '')))
        FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_STOCKS__CR` WHERE _snapshot_date = p_date) = 0
  AS 'RESTORE: дубли ключа в shadow RAW';
ASSERT IFNULL((SELECT t5_rows_written FROM `project-fa311fc0-4d87-4781-986.wb_raw.WB_STOCKS_SNAPSHOTS__CR` WHERE snapshot_id = p_snapshot_id), n_t5) = n_t5
  AS 'RESTORE: число строк shadow T5 не равно манифесту';

SET total_raw_before = (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_STOCKS`);
SET total_t5_before = (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_STOCKS_T5`);
SET total_manifest_before = (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_raw.WB_STOCKS_SNAPSHOTS`);

-- ── 2. Запись одной транзакцией; любая ошибка → ROLLBACK ────────────────────
BEGIN
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

  -- ── 3. Readback внутри транзакции ─────────────────────────────────────────
  ASSERT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_STOCKS` WHERE _snapshot_date = p_date AND load_id = p_load_id) = n_raw
    AS 'RESTORE: после записи число строк RAW за дату не совпало';
  ASSERT (SELECT SUM(quantity) FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_STOCKS` WHERE _snapshot_date = p_date) = qty_raw
    AS 'RESTORE: после записи сумма quantity не совпала';
  ASSERT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_STOCKS`) = total_raw_before + n_raw
    AS 'RESTORE: изменились строки вне восстанавливаемой даты (RAW)';
  ASSERT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_STOCKS_T5`) = total_t5_before + n_t5
    AS 'RESTORE: изменились строки вне восстанавливаемой даты (RAW_T5)';
  ASSERT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_raw.WB_STOCKS_SNAPSHOTS`) = total_manifest_before + 1
    AS 'RESTORE: в манифесте добавлено не ровно одна строка';
  ASSERT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_raw.WB_STOCKS_SNAPSHOTS`
          WHERE period_from = FORMAT_DATE('%Y-%m-%d', p_date) AND status = 'COMPLETE') = 1
    AS 'RESTORE: COMPLETE-манифест за дату не ровно один';

  COMMIT TRANSACTION;
EXCEPTION WHEN ERROR THEN
  ROLLBACK TRANSACTION;
  RAISE USING MESSAGE = CONCAT('RESTORE откатан, записей нет: ', @@error.message);
END;
