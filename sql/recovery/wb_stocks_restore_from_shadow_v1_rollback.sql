-- ════════════════════════════════════════════════════════════════════════════
-- Откат `wb_stocks_restore_from_shadow_v1.sql` для одной даты
-- ════════════════════════════════════════════════════════════════════════════
-- ⚠️ ЗАПИСЬ В PRODUCTION. Только по OWNER ACK, та же дата, что у восстановления.
--
-- Удаляет ровно то, что записало восстановление: строки RAW / RAW_T5 с load_id
-- `RESTORE_FROM_SHADOW_<yyyymmdd>` в партиции даты и манифест `STOCK_SNAP_shadow_<yyyymmdd>`
-- с отметкой RESTORED_FROM_SHADOW. Строки других load_id и других дат не трогаются
-- (readback по общим числам строк). Одна транзакция; любая ошибка → ROLLBACK.
-- После отката витрина при следующей сборке MART (полная пересборка FACT_STOCKS_SNAPSHOT)
-- снова не содержит даты; DRO-1 снова видит её как MISSING.
-- ════════════════════════════════════════════════════════════════════════════

DECLARE p_date DATE DEFAULT DATE '2026-10-28';
DECLARE p_snapshot_id STRING DEFAULT CONCAT('STOCK_SNAP_shadow_', FORMAT_DATE('%Y%m%d', p_date));
DECLARE p_load_id STRING DEFAULT CONCAT('RESTORE_FROM_SHADOW_', FORMAT_DATE('%Y%m%d', p_date));
DECLARE n_raw INT64;
DECLARE n_t5 INT64;
DECLARE total_raw_before INT64;
DECLARE total_t5_before INT64;
DECLARE total_manifest_before INT64;

ASSERT p_date IN (DATE '2026-10-28')
  AS 'ROLLBACK: на эту дату нет OWNER ACK';

SET n_raw = (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_STOCKS`
             WHERE _snapshot_date = p_date AND load_id = p_load_id);
SET n_t5 = (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_STOCKS_T5`
            WHERE _snapshot_date = p_date AND load_id = p_load_id);

ASSERT n_raw > 0
  AS 'ROLLBACK: строк восстановления за дату нет — откатывать нечего';
ASSERT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_raw.WB_STOCKS_SNAPSHOTS`
        WHERE snapshot_id = p_snapshot_id AND STARTS_WITH(error_message, 'RESTORED_FROM_SHADOW')) = 1
  AS 'ROLLBACK: манифест восстановления не найден или не один';

SET total_raw_before = (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_STOCKS`);
SET total_t5_before = (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_STOCKS_T5`);
SET total_manifest_before = (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_raw.WB_STOCKS_SNAPSHOTS`);

BEGIN
  BEGIN TRANSACTION;

  DELETE FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_STOCKS`
  WHERE _snapshot_date = p_date AND load_id = p_load_id;
  DELETE FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_STOCKS_T5`
  WHERE _snapshot_date = p_date AND load_id = p_load_id;
  DELETE FROM `project-fa311fc0-4d87-4781-986.wb_raw.WB_STOCKS_SNAPSHOTS`
  WHERE snapshot_id = p_snapshot_id AND STARTS_WITH(error_message, 'RESTORED_FROM_SHADOW');

  ASSERT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_STOCKS`) = total_raw_before - n_raw
    AS 'ROLLBACK: удалено не ровно n_raw строк RAW';
  ASSERT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_STOCKS_T5`) = total_t5_before - n_t5
    AS 'ROLLBACK: удалено не ровно n_t5 строк RAW_T5';
  ASSERT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_raw.WB_STOCKS_SNAPSHOTS`) = total_manifest_before - 1
    AS 'ROLLBACK: удалена не ровно одна строка манифеста';

  COMMIT TRANSACTION;
EXCEPTION WHEN ERROR THEN
  ROLLBACK TRANSACTION;
  RAISE USING MESSAGE = CONCAT('ROLLBACK откатан, ничего не удалено: ', @@error.message);
END;
