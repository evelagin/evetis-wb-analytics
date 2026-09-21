-- ============================================================================
-- PHASE 2 · D0 — ПРЕДПРОВЕРКА перед D1/D2. ТОЛЬКО ЧТЕНИЕ.
-- Прогнать целиком; все строки обязаны вернуть status = 'PASS'.
-- Контракт: tools/run_data_checks.py --file … --adapter verdict_select --status-column status
-- ============================================================================

-- @check D0_1_REGISTRY_HAS_NO_OZON_YET
-- D1 — вставка. Если строки уже есть, повторный прогон создаст дубликаты.
SELECT COUNT(*) AS existing_ozon_rows,
       IF(COUNT(*) = 0, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.wb_ops.OPS_PIPELINE_REGISTRY`
WHERE pipeline_id LIKE 'ozon%';

-- @check D0_2_ORDERS_AND_SALES_THRESHOLDS_EXIST
-- D2 не изобретает порогов: он читает их из реестра. Без строк порога проверки молчат.
SELECT COUNTIF(pipeline_id = 'orders' AND freshness_sla_minutes IS NOT NULL) AS orders_ok,
       COUNTIF(pipeline_id = 'sales' AND freshness_sla_minutes IS NOT NULL) AS sales_ok,
       IF(COUNTIF(pipeline_id IN ('orders', 'sales') AND freshness_sla_minutes IS NOT NULL) = 2,
          'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.wb_ops.OPS_PIPELINE_REGISTRY`
WHERE enabled AND environment = 'prod';

-- @check D0_3_APPLY_HELPER_EXISTS
-- D2 опирается на объявленную точку расширения; её отсутствие делает развёртывание бессмысленным.
SELECT COUNT(*) AS helper,
       IF(COUNT(*) = 1, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.wb_ops`.INFORMATION_SCHEMA.ROUTINES
WHERE routine_name = 'sp_ops_apply_health';

-- @check D0_4_EXT_PROCEDURE_ABSENT
-- Имя нового объекта не должно быть занято.
SELECT COUNT(*) AS already_there,
       IF(COUNT(*) = 0, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.wb_ops`.INFORMATION_SCHEMA.ROUTINES
WHERE routine_name = 'sp_evaluate_health_ext';

-- @check D0_5_BASELINE_HEALTH_ROWS
-- Снимок «до»: число проверок и конвейеров без покрытия. Сравнивается постпроверкой.
SELECT (SELECT COUNT(DISTINCT scope_id) FROM `project-fa311fc0-4d87-4781-986.wb_ops.OPS_HEALTH_STATE`
        WHERE scope = 'PIPELINE_CHECK') AS distinct_checks_before,
       (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_ops.OPS_PIPELINE_REGISTRY`
        WHERE enabled) AS enabled_pipelines_before,
       'PASS' AS status;
