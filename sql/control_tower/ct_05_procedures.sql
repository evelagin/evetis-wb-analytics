-- =====================================================================================
-- CONTROL TOWER PHASE 1 (+1.1) — ПРОЦЕДУРЫ (evetis_ref.sp_ct_*)
-- =====================================================================================
-- sp_ct_generate_actions — правила → очередь действий, с дедупликацией
-- sp_ct_action_update    — контролируемая смена статуса действия владельцем
-- sp_ct_refresh_daily    — ежедневное обновление витрин + прогон правил
-- Применять ПОСЛЕ ct_04a/ct_04b. Откат: DROP PROCEDURE (см. tools/ct_phase1_rollback.sh).
-- =====================================================================================


CREATE OR REPLACE PROCEDURE `project-fa311fc0-4d87-4781-986.evetis_ref.sp_ct_generate_actions`()
OPTIONS (description = 'Control Tower: idempotent rule-engine run. Reads wb_mart.V_CT_ACTION_CANDIDATES and merges into CT_OWNER_ACTION_QUEUE by dedup_key: refreshes open matches (last_seen_at/times_seen; text+qty only for RULE_ENGINE rows), inserts new actions only when no open action covers the same (action_type, marketplace, sku) and the same key was not closed within 7 days, supersedes RULE_ENGINE actions not seen for 7 days. Writes CT_ACTION_STATUS_LOG and CT_REFRESH_LOG.')
BEGIN
  DECLARE v_run_id STRING DEFAULT GENERATE_UUID();
  DECLARE v_t0 TIMESTAMP DEFAULT CURRENT_TIMESTAMP();
  DECLARE v_next_no INT64 DEFAULT 0;
  DECLARE v_upd INT64 DEFAULT 0;
  DECLARE v_ins INT64 DEFAULT 0;
  DECLARE v_sup INT64 DEFAULT 0;

  CREATE TEMP TABLE cand AS
  SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_ACTION_CANDIDATES`;

  -- 1. Refresh open actions that are still generated today (same dedup_key). Seed/manual rows keep their text, priority and qty.
  UPDATE `project-fa311fc0-4d87-4781-986.evetis_ref.CT_OWNER_ACTION_QUEUE` t
  SET last_seen_at = CURRENT_TIMESTAMP(),
      times_seen = IFNULL(t.times_seen, 0) + 1,
      qty = IF(t.generated_by = 'RULE_ENGINE', CAST(c.qty AS FLOAT64), t.qty),
      financial_effect_rub = IF(t.generated_by = 'RULE_ENGINE', c.financial_effect_rub, t.financial_effect_rub),
      reason_text = IF(t.generated_by = 'RULE_ENGINE', c.reason_text, t.reason_text),
      priority = IF(t.generated_by = 'RULE_ENGINE', c.priority, t.priority),
      deadline = IF(t.generated_by = 'RULE_ENGINE', LEAST(t.deadline, c.deadline), t.deadline)
  FROM cand c
  WHERE t.dedup_key = c.dedup_key AND t.status IN ('OPEN', 'IN_PROGRESS');
  SET v_upd = @@row_count;

  -- 2. Insert genuinely new actions.
  SET v_next_no = (SELECT IFNULL(MAX(SAFE_CAST(REGEXP_EXTRACT(action_id, r'^CT-(\d+)$') AS INT64)), 0)
                   FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_OWNER_ACTION_QUEUE`);
  INSERT INTO `project-fa311fc0-4d87-4781-986.evetis_ref.CT_OWNER_ACTION_QUEUE`
    (action_id, dedup_key, generated_at, generated_by, action_date, priority, deadline, action_type, marketplace, sku, bundle_id, qty,
     financial_effect_rub, reason_code, reason_text, source_metric, status, status_updated_at, completed_at, owner_note, plan_version,
     first_seen_at, last_seen_at, times_seen)
  SELECT CONCAT('CT-', LPAD(CAST(v_next_no + ROW_NUMBER() OVER (ORDER BY c.priority, c.dedup_key) AS STRING), 4, '0')),
    c.dedup_key, CURRENT_TIMESTAMP(), 'RULE_ENGINE', c.action_date, c.priority, c.deadline, c.action_type, c.marketplace, c.sku, c.bundle_id,
    CAST(c.qty AS FLOAT64), c.financial_effect_rub, c.reason_code, c.reason_text, c.source_metric, 'OPEN', CURRENT_TIMESTAMP(),
    CAST(NULL AS TIMESTAMP), CAST(NULL AS STRING), c.plan_version,
    CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP(), 1
  FROM cand c
  WHERE NOT EXISTS (SELECT 1 FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_OWNER_ACTION_QUEUE` t
                    WHERE t.dedup_key = c.dedup_key AND t.status IN ('OPEN', 'IN_PROGRESS'))
    AND NOT EXISTS (SELECT 1 FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_OWNER_ACTION_QUEUE` t
                    WHERE t.action_type = c.action_type AND t.marketplace = c.marketplace AND IFNULL(t.sku, '') = IFNULL(c.sku, '')
                      AND t.status IN ('OPEN', 'IN_PROGRESS'))
    AND NOT EXISTS (SELECT 1 FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_OWNER_ACTION_QUEUE` t
                    WHERE t.dedup_key = c.dedup_key AND t.status IN ('DONE', 'CANCELLED')
                      AND t.status_updated_at > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY));
  SET v_ins = @@row_count;

  -- 3. Supersede rule-generated OPEN actions whose condition has not been observed for 7 days (manual/seed rows are never auto-closed).
  INSERT INTO `project-fa311fc0-4d87-4781-986.evetis_ref.CT_ACTION_STATUS_LOG` (action_id, changed_at, old_status, new_status, owner_note, changed_by)
  SELECT action_id, CURRENT_TIMESTAMP(), status, 'SUPERSEDED', 'auto: условие не наблюдалось 7 дней', 'sp_ct_generate_actions'
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_OWNER_ACTION_QUEUE`
  WHERE generated_by = 'RULE_ENGINE' AND status = 'OPEN' AND last_seen_at < TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY);
  UPDATE `project-fa311fc0-4d87-4781-986.evetis_ref.CT_OWNER_ACTION_QUEUE`
  SET status = 'SUPERSEDED', status_updated_at = CURRENT_TIMESTAMP(),
      owner_note = CONCAT(IFNULL(owner_note, ''), IF(owner_note IS NULL, '', ' | '), 'auto: условие не наблюдалось 7 дней')
  WHERE generated_by = 'RULE_ENGINE' AND status = 'OPEN' AND last_seen_at < TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY);
  SET v_sup = @@row_count;

  INSERT INTO `project-fa311fc0-4d87-4781-986.evetis_ref.CT_REFRESH_LOG` (run_id, run_ts, step, status, rows_affected, message, duration_ms)
  VALUES (v_run_id, CURRENT_TIMESTAMP(), 'sp_ct_generate_actions', 'OK', v_ins,
          CONCAT('candidates=', CAST((SELECT COUNT(*) FROM cand) AS STRING), ' refreshed=', CAST(v_upd AS STRING), ' inserted=', CAST(v_ins AS STRING), ' superseded=', CAST(v_sup AS STRING)),
          TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), v_t0, MILLISECOND));
END
;

-- ── Смена статуса действия владельцем (единственный разрешённый способ) ─────────────
-- Пример: CALL `evetis_ref.sp_ct_action_update`('CT-0002', 'DONE', 'партия 8 отпущена 10.09', 'owner');
-- Пишет CT_ACTION_STATUS_LOG, проставляет completed_at для DONE/CANCELLED,
-- дописывает заметку владельца с датой. Никаких записей в маркетплейсы.
CREATE OR REPLACE PROCEDURE `project-fa311fc0-4d87-4781-986.evetis_ref.sp_ct_action_update`(p_action_id STRING, p_new_status STRING, p_note STRING, p_changed_by STRING)
OPTIONS (description = 'Control Tower: controlled status change for one owner action. Allowed statuses OPEN / IN_PROGRESS / DONE / CANCELLED / SUPERSEDED. Sets status_updated_at, completed_at (DONE/CANCELLED), appends owner_note and writes CT_ACTION_STATUS_LOG. Usage: CALL evetis_ref.sp_ct_action_update("CT-0002", "DONE", "партия 8 отпущена 10.09", "owner");')
BEGIN
  DECLARE v_old STRING;
  IF p_new_status NOT IN ('OPEN', 'IN_PROGRESS', 'DONE', 'CANCELLED', 'SUPERSEDED') THEN
    RAISE USING MESSAGE = CONCAT('sp_ct_action_update: недопустимый статус ', IFNULL(p_new_status, 'NULL'));
  END IF;
  SET v_old = (SELECT status FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_OWNER_ACTION_QUEUE` WHERE action_id = p_action_id);
  IF v_old IS NULL THEN
    RAISE USING MESSAGE = CONCAT('sp_ct_action_update: действие не найдено ', IFNULL(p_action_id, 'NULL'));
  END IF;
  UPDATE `project-fa311fc0-4d87-4781-986.evetis_ref.CT_OWNER_ACTION_QUEUE`
  SET status = p_new_status,
      status_updated_at = CURRENT_TIMESTAMP(),
      completed_at = IF(p_new_status IN ('DONE', 'CANCELLED'), CURRENT_TIMESTAMP(), NULL),
      owner_note = IF(p_note IS NULL OR p_note = '', owner_note, CONCAT(IFNULL(owner_note, ''), IF(owner_note IS NULL OR owner_note = '', '', ' | '), FORMAT_DATE('%d.%m', CURRENT_DATE()), ': ', p_note))
  WHERE action_id = p_action_id;
  INSERT INTO `project-fa311fc0-4d87-4781-986.evetis_ref.CT_ACTION_STATUS_LOG` (action_id, changed_at, old_status, new_status, owner_note, changed_by)
  VALUES (p_action_id, CURRENT_TIMESTAMP(), v_old, p_new_status, p_note, IFNULL(p_changed_by, 'owner'));
END;

-- ── Ежедневное обновление Control Tower ─────────────────────────────────────────────
-- Phase 1.1: запускается автоматически — Cloud Scheduler `ct-refresh-prod` → BigQuery jobs API
-- → CALL (infra/terraform/ct_refresh.tf), окна 07:40 / 09:40 / 12:40 / 16:40 / 19:40 МСК;
-- ручной CALL по-прежнему безопасен.
-- Шаги: 0) guard — отпечаток источников (MART_RUNS, V_DASH_SKU_DAILY, Ozon stocks/postings/supply,
--          WB stocks, срез ФФ, активная версия плана, очередь): совпал с последним OK-прогоном за
--          сегодня → SKIP (переопределение: CT_CONFIG.refresh_force = '1', сбрасывается автоматически);
--       1) freshness_gate — WARN (не падение), если последняя попытка витрины за D-1 не COMPLETE;
--       2) перестроить CT_ACTUAL_DAILY; 3) заменить партицию CT_INVENTORY_SNAPSHOT_DAILY за сегодня;
--       4) прогнать правила очереди действий.
-- Каждый шаг пишет CT_REFRESH_LOG (OK / SKIP / WARN / ERROR); итоговая строка sp_ct_refresh_daily
-- несёт отпечаток (fp=…) — по ней V_CT_REFRESH_STATUS строит «CONTROL TOWER UPDATED AT».
-- Идемпотентна в пределах суток. Только DML (DELETE вместо TRUNCATE) — достаточно потабличного
-- dataEditor для sa-ct-refresh. Предохранители по числу строк сохранены из Phase 1.
-- Читает production, пишет только CT_*.
CREATE OR REPLACE PROCEDURE `project-fa311fc0-4d87-4781-986.evetis_ref.sp_ct_refresh_daily`()
OPTIONS (description = "Control Tower daily refresh (scheduled: Cloud Scheduler ct-refresh-prod → BigQuery jobs API, windows 07:40/09:40/12:40/16:40/19:40 MSK; safe to CALL manually). Steps: 0) guard — skip if an OK run today already used the same source fingerprint (MART_RUNS, V_DASH_SKU_DAILY, Ozon stocks/postings/supply, WB stocks, FF snapshot, action-queue changes) unless CT_CONFIG.refresh_force='1'; 1) freshness gate — WARN (not fail) when the latest MART_RUNS attempt for D-1 is not COMPLETE; 2) rebuild evetis_ref.CT_ACTUAL_DAILY from wb_mart.V_CT_ACTUAL_DAILY_LIVE; 3) replace today's partition of evetis_ref.CT_INVENTORY_SNAPSHOT_DAILY from wb_mart.V_CT_INVENTORY_TRUTH_LIVE; 4) CALL sp_ct_generate_actions(). Every step is logged to CT_REFRESH_LOG (OK / SKIP / WARN / ERROR). Idempotent; reads production only; writes CT_* objects only; DML only (no TRUNCATE) so a table-level dataEditor grant is sufficient.")
BEGIN
  DECLARE v_run_id STRING DEFAULT GENERATE_UUID();
  DECLARE v_t0 TIMESTAMP DEFAULT CURRENT_TIMESTAMP();
  DECLARE v_t1 TIMESTAMP;
  DECLARE v_rows INT64 DEFAULT 0;
  DECLARE v_fp STRING;
  DECLARE v_prev_fp STRING;
  DECLARE v_force BOOL DEFAULT FALSE;
  DECLARE v_mart_ok BOOL DEFAULT TRUE;
  DECLARE v_mart_msg STRING;

  -- 0. GUARD: source fingerprint. Same fingerprint as the last OK run today → nothing to rebuild.
  SET v_fp = (
    SELECT CONCAT(
      'mart=', IFNULL(FORMAT_TIMESTAMP('%Y%m%d%H%M%S', (SELECT MAX(completed_at) FROM `project-fa311fc0-4d87-4781-986.wb_mart.MART_RUNS` WHERE status = 'COMPLETE')), 'none'),
      '|wb_day=', IFNULL(CAST((SELECT MAX(day) FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_SKU_DAILY`) AS STRING), 'none'),
      '|oz_stock=', IFNULL(FORMAT_TIMESTAMP('%Y%m%d%H%M%S', (SELECT MAX(extracted_at) FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_STOCKS`)), 'none'),
      '|oz_post=', IFNULL(FORMAT_TIMESTAMP('%Y%m%d%H%M%S', (SELECT MAX(extracted_at) FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_POSTINGS_FBO`)), 'none'),
      '|oz_supply=', IFNULL(FORMAT_TIMESTAMP('%Y%m%d%H%M%S', (SELECT MAX(extracted_at) FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_SUPPLY_ORDERS`)), 'none'),
      '|wb_stock=', IFNULL(FORMAT_TIMESTAMP('%Y%m%d%H%M%S', (SELECT MAX(snapshot_ts) FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_STOCKS_T5_CURRENT`)), 'none'),
      '|ff=', IFNULL(CAST((SELECT MAX(snapshot_date) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_STOCK_SNAPSHOT`) AS STRING), 'none'),
      '|plan=', IFNULL((SELECT STRING_AGG(plan_version ORDER BY plan_version) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_PLAN_VERSION` WHERE plan_status = 'ACTIVE'), 'none'),
      '|queue=', IFNULL(FORMAT_TIMESTAMP('%Y%m%d%H%M%S', (SELECT MAX(status_updated_at) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_OWNER_ACTION_QUEUE`)), 'none'),
      '|day=', CAST(CURRENT_DATE() AS STRING))
  );
  SET v_force = IFNULL((SELECT MAX(config_value) = '1' FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_CONFIG` WHERE config_key = 'refresh_force'), FALSE);
  SET v_prev_fp = (
    SELECT REGEXP_EXTRACT(message, r'fp=(.*)$')
    FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_REFRESH_LOG`
    WHERE step = 'sp_ct_refresh_daily' AND status = 'OK' AND DATE(run_ts) = CURRENT_DATE()
    ORDER BY run_ts DESC LIMIT 1
  );
  IF v_prev_fp IS NOT NULL AND v_prev_fp = v_fp AND NOT v_force THEN
    INSERT INTO `project-fa311fc0-4d87-4781-986.evetis_ref.CT_REFRESH_LOG` (run_id, run_ts, step, status, rows_affected, message, duration_ms)
    VALUES (v_run_id, CURRENT_TIMESTAMP(), 'sp_ct_refresh_daily', 'SKIP', 0, CONCAT('guard_skip: sources unchanged since last OK run today; fp=', v_fp), TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), v_t0, MILLISECOND));
    RETURN;
  END IF;
  IF v_force THEN
    UPDATE `project-fa311fc0-4d87-4781-986.evetis_ref.CT_CONFIG` SET config_value = '0', updated_at = CURRENT_TIMESTAMP() WHERE config_key = 'refresh_force';
  END IF;

  -- 1. FRESHNESS GATE (observability only): latest MART attempt for D-1 must be COMPLETE.
  SET v_mart_msg = (
    SELECT CONCAT('mart D-1 latest attempt: ', IFNULL(status, 'NO_RUN'), IFNULL(CONCAT(' at ', FORMAT_TIMESTAMP('%d.%m %H:%M', started_at, 'Europe/Moscow'), ' MSK'), ''))
    FROM (SELECT status, started_at FROM `project-fa311fc0-4d87-4781-986.wb_mart.MART_RUNS`
          WHERE target_date = DATE_SUB(CURRENT_DATE('Europe/Moscow'), INTERVAL 1 DAY)
          ORDER BY started_at DESC LIMIT 1)
  );
  SET v_mart_ok = IFNULL((SELECT status = 'COMPLETE' FROM `project-fa311fc0-4d87-4781-986.wb_mart.MART_RUNS`
                          WHERE target_date = DATE_SUB(CURRENT_DATE('Europe/Moscow'), INTERVAL 1 DAY)
                          ORDER BY started_at DESC LIMIT 1), FALSE);
  INSERT INTO `project-fa311fc0-4d87-4781-986.evetis_ref.CT_REFRESH_LOG` (run_id, run_ts, step, status, rows_affected, message, duration_ms)
  VALUES (v_run_id, CURRENT_TIMESTAMP(), 'freshness_gate', IF(v_mart_ok, 'OK', 'WARN'), NULL, IFNULL(v_mart_msg, 'mart D-1: NO_RUN'), TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), v_t0, MILLISECOND));

  -- 2. CT_ACTUAL_DAILY
  BEGIN
    SET v_t1 = CURRENT_TIMESTAMP();
    CREATE TEMP TABLE new_actual AS
    SELECT d, marketplace, internal_sku, sales_mode, component_count, cards_ordered, cards_cancelled, cards_sold, cards_returned, units_ordered, units_sold,
      gmv_ordered, revenue_seller_base, marketplace_costs, ad_spend, seller_cash, cogs, contribution, contribution_covered, cogs_covered, include_in_pnl, economics_basis,
      CURRENT_TIMESTAMP() AS refreshed_at
    FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_ACTUAL_DAILY_LIVE`
    WHERE d IS NOT NULL AND internal_sku IS NOT NULL;
    SET v_rows = (SELECT COUNT(*) FROM new_actual);
    IF v_rows < 1000 THEN
      RAISE USING MESSAGE = CONCAT('sp_ct_refresh_daily: V_CT_ACTUAL_DAILY_LIVE вернул слишком мало строк (', CAST(v_rows AS STRING), ') — витрина не перезаписана');
    END IF;
    DELETE FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_ACTUAL_DAILY` WHERE TRUE;
    INSERT INTO `project-fa311fc0-4d87-4781-986.evetis_ref.CT_ACTUAL_DAILY` SELECT * FROM new_actual;
    INSERT INTO `project-fa311fc0-4d87-4781-986.evetis_ref.CT_REFRESH_LOG` (run_id, run_ts, step, status, rows_affected, message, duration_ms)
    VALUES (v_run_id, CURRENT_TIMESTAMP(), 'CT_ACTUAL_DAILY', 'OK', v_rows, 'full rebuild from V_CT_ACTUAL_DAILY_LIVE', TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), v_t1, MILLISECOND));
  EXCEPTION WHEN ERROR THEN
    INSERT INTO `project-fa311fc0-4d87-4781-986.evetis_ref.CT_REFRESH_LOG` (run_id, run_ts, step, status, rows_affected, message, duration_ms)
    VALUES (v_run_id, CURRENT_TIMESTAMP(), 'CT_ACTUAL_DAILY', 'ERROR', 0, @@error.message, TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), v_t1, MILLISECOND));
    INSERT INTO `project-fa311fc0-4d87-4781-986.evetis_ref.CT_REFRESH_LOG` (run_id, run_ts, step, status, rows_affected, message, duration_ms)
    VALUES (v_run_id, CURRENT_TIMESTAMP(), 'sp_ct_refresh_daily', 'ERROR', 0, CONCAT('failed at CT_ACTUAL_DAILY: ', @@error.message), TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), v_t0, MILLISECOND));
    RAISE;
  END;

  -- 3. CT_INVENTORY_SNAPSHOT_DAILY (today's partition)
  BEGIN
    SET v_t1 = CURRENT_TIMESTAMP();
    CREATE TEMP TABLE new_inv AS
    SELECT CURRENT_DATE() AS snapshot_date, CURRENT_TIMESTAMP() AS snapshot_ts, v.*
    FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_INVENTORY_TRUTH_LIVE` v;
    SET v_rows = (SELECT COUNT(*) FROM new_inv);
    IF v_rows < 5 THEN
      RAISE USING MESSAGE = CONCAT('sp_ct_refresh_daily: V_CT_INVENTORY_TRUTH_LIVE вернул слишком мало строк (', CAST(v_rows AS STRING), ')');
    END IF;
    DELETE FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_INVENTORY_SNAPSHOT_DAILY` WHERE snapshot_date = CURRENT_DATE();
    INSERT INTO `project-fa311fc0-4d87-4781-986.evetis_ref.CT_INVENTORY_SNAPSHOT_DAILY` SELECT * FROM new_inv;
    INSERT INTO `project-fa311fc0-4d87-4781-986.evetis_ref.CT_REFRESH_LOG` (run_id, run_ts, step, status, rows_affected, message, duration_ms)
    VALUES (v_run_id, CURRENT_TIMESTAMP(), 'CT_INVENTORY_SNAPSHOT_DAILY', 'OK', v_rows, 'partition replaced from V_CT_INVENTORY_TRUTH_LIVE', TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), v_t1, MILLISECOND));
  EXCEPTION WHEN ERROR THEN
    INSERT INTO `project-fa311fc0-4d87-4781-986.evetis_ref.CT_REFRESH_LOG` (run_id, run_ts, step, status, rows_affected, message, duration_ms)
    VALUES (v_run_id, CURRENT_TIMESTAMP(), 'CT_INVENTORY_SNAPSHOT_DAILY', 'ERROR', 0, @@error.message, TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), v_t1, MILLISECOND));
    INSERT INTO `project-fa311fc0-4d87-4781-986.evetis_ref.CT_REFRESH_LOG` (run_id, run_ts, step, status, rows_affected, message, duration_ms)
    VALUES (v_run_id, CURRENT_TIMESTAMP(), 'sp_ct_refresh_daily', 'ERROR', 0, CONCAT('failed at CT_INVENTORY_SNAPSHOT_DAILY: ', @@error.message), TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), v_t0, MILLISECOND));
    RAISE;
  END;

  -- 4. Rule engine
  BEGIN
    CALL `project-fa311fc0-4d87-4781-986.evetis_ref.sp_ct_generate_actions`();
  EXCEPTION WHEN ERROR THEN
    INSERT INTO `project-fa311fc0-4d87-4781-986.evetis_ref.CT_REFRESH_LOG` (run_id, run_ts, step, status, rows_affected, message, duration_ms)
    VALUES (v_run_id, CURRENT_TIMESTAMP(), 'sp_ct_refresh_daily', 'ERROR', 0, CONCAT('failed at sp_ct_generate_actions: ', @@error.message), TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), v_t0, MILLISECOND));
    RAISE;
  END;

  INSERT INTO `project-fa311fc0-4d87-4781-986.evetis_ref.CT_REFRESH_LOG` (run_id, run_ts, step, status, rows_affected, message, duration_ms)
  VALUES (v_run_id, CURRENT_TIMESTAMP(), 'sp_ct_refresh_daily', 'OK', NULL, CONCAT('daily refresh complete; fp=', v_fp), TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), v_t0, MILLISECOND));
END;
