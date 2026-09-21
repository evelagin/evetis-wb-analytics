-- ============================================================================
-- PHASE 2 · D3 — ПОСТПРОВЕРКА после D1, D2 и одного вызова
-- CALL `wb_ops.sp_evaluate_health_ext`(). ТОЛЬКО ЧТЕНИЕ.
-- Все строки обязаны вернуть status = 'PASS'.
-- ============================================================================

-- @check D3_1_OZON_REGISTERED_EXACTLY_ONCE
SELECT COUNT(*) AS ozon_rows, COUNT(DISTINCT pipeline_id) AS distinct_ids,
       IF(COUNT(*) = 11 AND COUNT(DISTINCT pipeline_id) = 11, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.wb_ops.OPS_PIPELINE_REGISTRY`
WHERE pipeline_id LIKE 'ozon%';

-- @check D3_2_NO_OTHER_REGISTRY_ROW_CHANGED
-- 16 прежних конвейеров обязаны остаться нетронутыми: D1 — только INSERT.
SELECT COUNT(*) AS non_ozon_rows,
       IF(COUNT(*) = 16, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.wb_ops.OPS_PIPELINE_REGISTRY`
WHERE pipeline_id NOT LIKE 'ozon%';

-- @check D3_3_NEW_CHECKS_PRESENT
-- Ожидается 11 OZ1 + 11 OZ2 + H7 + H8 + H9 = 25 новых scope_id.
SELECT COUNT(DISTINCT scope_id) AS new_checks,
       IF(COUNT(DISTINCT scope_id) = 25, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.wb_ops.OPS_HEALTH_STATE`
WHERE scope = 'PIPELINE_CHECK'
  AND (scope_id LIKE '%/OZ1_%' OR scope_id LIKE '%/OZ2_%'
       OR scope_id LIKE '%/H7_%' OR scope_id LIKE '%/H8_%' OR scope_id LIKE '%/H9_%');

-- @check D3_4_EXISTING_DETECTOR_UNTOUCHED
-- Прежние семь проверок обязаны продолжать обновляться: D2 в них не вмешивается.
SELECT COUNT(DISTINCT scope_id) AS legacy_checks,
       IF(COUNT(DISTINCT scope_id) >= 7, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.wb_ops.V_OPS_CURRENT_HEALTH`
WHERE scope_id LIKE '%/H0_%' OR scope_id LIKE '%/H1_%' OR scope_id LIKE '%/H2_%'
   OR scope_id LIKE '%/H3_%' OR scope_id LIKE '%/H4_%' OR scope_id LIKE '%/H5_%'
   OR scope_id LIKE '%/H6_%';

-- @check D3_5_IDEMPOTENT_NO_DUPLICATE_ALERTS
-- Второй вызов в том же состоянии не имеет права создать второе оповещение:
-- alert_event_id детерминирован.
SELECT COUNT(*) AS duplicate_alert_ids,
       IF(COUNT(*) = 0, 'PASS', 'FAIL') AS status
FROM (SELECT alert_event_id FROM `project-fa311fc0-4d87-4781-986.wb_ops.OPS_ALERT_EVENT`
      GROUP BY alert_event_id HAVING COUNT(*) > 1);

-- @check D3_6_COVERAGE_IMPROVED
-- Итог этапа: конвейеров без единой проверки должно остаться меньше, чем было (11 из 16).
SELECT COUNT(*) AS pipelines_without_check,
       IF(COUNT(*) <= 8, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.wb_ops.OPS_PIPELINE_REGISTRY` r
LEFT JOIN (SELECT DISTINCT SPLIT(scope_id, '/')[OFFSET(0)] AS pipeline_id
           FROM `project-fa311fc0-4d87-4781-986.wb_ops.OPS_HEALTH_STATE`
           WHERE scope = 'PIPELINE_CHECK') c USING (pipeline_id)
WHERE r.enabled AND c.pipeline_id IS NULL;
