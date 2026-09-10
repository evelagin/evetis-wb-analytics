-- =====================================================================================
-- EVETIS OWNER CONTROL TOWER — PHASE 1.1 — VALIDATION / REGRESSION SUITE (T23–T32)
-- =====================================================================================
-- Назначение : проверить объекты Phase 1.1 (автообновление, уверенность прогноза, очередь
--              действий в человеческом виде, ежедневная сводка, таблица контроля SKU).
-- Запуск     : после ct_phase1_validation.sql (22 теста Phase 1 обязаны проходить как прежде);
--              bq query --use_legacy_sql=false < sql/control_tower/ct_phase11_validation.sql
-- Читает     : только CT_* / V_CT_* / INFORMATION_SCHEMA. Ничего не пишет.
-- Прогон 10.09.2026: 10/10 PASSED (job_X2SkOviEpuPRNPVRq64mTI9PoUQ_).
-- =====================================================================================

-- T23. CT_CONFIG содержит все ключи Phase 1.1
ASSERT (SELECT COUNT(DISTINCT config_key) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_CONFIG`
        WHERE config_key IN ('action_webapp_url','refresh_schedule_msk','refresh_sla_hours','refresh_force','metabase_details_dashboard_url')) = 5
  AS 'T23 FAILED: CT_CONFIG missing keys';

-- T24. Статус обновления: одна строка, статус из словаря, строка начинается с CONTROL TOWER UPDATED AT
ASSERT (SELECT COUNT(*) = 1 AND COUNTIF(refresh_status IN ('OK','STALE','ERROR')) = 1
          AND COUNTIF(STARTS_WITH(status_line, 'CONTROL TOWER UPDATED AT')) = 1
        FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_REFRESH_STATUS`)
  AS 'T24 FAILED: V_CT_REFRESH_STATUS';

-- T25. Последний успешный прогон несёт отпечаток источников (основа guard_skip)
ASSERT (SELECT message LIKE '%fp=%' FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_REFRESH_LOG`
        WHERE step = 'sp_ct_refresh_daily' AND status = 'OK' ORDER BY run_ts DESC LIMIT 1)
  AS 'T25 FAILED: last OK refresh has no source fingerprint';

-- T26. Очередь действий: дорожки из словаря, у открытых строк есть «что / кому / срок», технических кодов в тексте нет
ASSERT (SELECT COUNTIF(lane NOT IN ('DECISION','EXECUTION','WATCH')) = 0
          AND COUNTIF(is_open AND (what_to_do IS NULL OR what_to_do = '' OR executor_ru IS NULL OR deadline_ru IS NULL)) = 0
          AND COUNTIF(what_to_do LIKE '%PORTFOLIO%' OR what_to_do LIKE '%BUNDLES%') = 0
        FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_ACTION_QUEUE`)
  AS 'T26 FAILED: action queue human-readable fields';

-- T27. Уверенность прогноза: правила явные и проверяемые
--   < 3 плановых дней → LOW; HIGH только при ≥ 10 днях и без единого понижающего флага; LOW/MEDIUM всегда с причиной
ASSERT (SELECT forecast_confidence IN ('LOW','MEDIUM','HIGH')
          AND (plan_days_elapsed >= 3 OR forecast_confidence = 'LOW')
          AND (forecast_confidence != 'HIGH' OR (plan_days_elapsed >= 10 AND NOT conf_flag_data_incomplete AND NOT conf_flag_availability AND NOT conf_flag_deviation))
          AND (forecast_confidence = 'HIGH' OR LENGTH(forecast_confidence_reason) > 0)
          AND forecast_confidence_score BETWEEN 1 AND 3
        FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_OWNER_HOME`)
  AS 'T27 FAILED: forecast confidence rules';

-- T28. Ежедневная сводка: 6 разделов, не больше 7 действий, без SQL-терминов
ASSERT (SELECT COUNT(DISTINCT section_ord) = 6 AND COUNTIF(section_ord IN (4,5)) <= 7 AND COUNTIF(line_text IS NULL OR line_text = '') = 0
        FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_DAILY_BRIEF_LINES`)
  AS 'T28a FAILED: daily brief lines';
-- Phase 1.2: раздел переименован «Требует решения владельца» → «Решения владельца»
ASSERT (SELECT brief_text LIKE '%Сегодня сделать%' AND brief_text LIKE '%Решения владельца%' AND brief_text NOT LIKE '%SELECT %'
        FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_DAILY_BRIEF`)
  AS 'T28b FAILED: daily brief text';

-- T29. Таблица контроля SKU сходится с Owner Home (факт за плановые дни и план MTD)
ASSERT (SELECT s.a = h.cards_ordered_plan_days_mtd AND ABS(s.p - h.plan_cards_mtd) < 0.5 AND s.bad = 0
        FROM (SELECT SUM(actual_cards_mtd) a, SUM(plan_cards_mtd) p, COUNTIF(status NOT IN ('RED','YELLOW','GREEN','BLUE','GREY')) bad
              FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_SKU_CONTROL`) s,
             `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_OWNER_HOME` h)
  AS 'T29 FAILED: SKU control table does not reconcile with Owner Home';

-- T30. Новые колонки Owner Home заполнены
ASSERT (SELECT ct_refresh_status IS NOT NULL AND forecast_confidence IS NOT NULL AND plan_units_yesterday IS NOT NULL
          AND plan_contribution_yesterday IS NOT NULL AND ct_status_line IS NOT NULL
        FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_OWNER_HOME`)
  AS 'T30 FAILED: Owner Home Phase 1.1 columns';

-- T31. Ссылки действий строго следуют CT_CONFIG.action_webapp_url (NULL → ссылок нет)
ASSERT (SELECT COUNTIF(IF(cfg.u IS NULL, done_url IS NOT NULL, done_url != CONCAT(cfg.u, '?id=', action_id, '&status=DONE'))) = 0
        FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_ACTION_QUEUE`,
             (SELECT NULLIF(TRIM(MAX(IF(config_key = 'action_webapp_url', config_value, NULL))), '') AS u
              FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_CONFIG`) cfg)
  AS 'T31 FAILED: action links do not follow CT_CONFIG.action_webapp_url';

-- T32. Процедуры: только DML (без TRUNCATE — достаточно потабличного dataEditor), guard присутствует
ASSERT (SELECT COUNTIF(UPPER(routine_definition) LIKE '%TRUNCATE%') = 0
          AND COUNTIF(routine_name = 'sp_ct_refresh_daily' AND routine_definition LIKE '%guard_skip%') = 1
        FROM `project-fa311fc0-4d87-4781-986.evetis_ref.INFORMATION_SCHEMA.ROUTINES` WHERE routine_name LIKE 'sp_ct_%')
  AS 'T32 FAILED: refresh procedure must be DML-only with guard';

SELECT 'CT PHASE 1.1 VALIDATION: ALL 10 TESTS PASSED (T23–T32)' AS result, CURRENT_TIMESTAMP() AS checked_at;
