-- UNITKA 2.0 R7 — QA воронки WB. Только SELECT. PASS = все T* со status = 'PASS'.
-- Прогон 11.09.2026 после первого запуска wb-funnel-prod (окно 04–10.09): 6/6 PASS.

-- T1. Grain витрины: одна строка на date_msk × nm_id.
SELECT 'T1_view_grain' AS test, COUNT(*) AS bad, IF(COUNT(*) = 0, 'PASS', 'FAIL') AS status
FROM (SELECT date_msk, nm_id FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FUNNEL_DAILY`
      GROUP BY 1, 2 HAVING COUNT(*) > 1)
UNION ALL
-- T2. Внутри одного наблюдения ключ не повторяется (нет удвоения при повторе окна).
SELECT 'T2_raw_no_dup_in_observation', COUNT(*), IF(COUNT(*) = 0, 'PASS', 'FAIL')
FROM (SELECT observation_id, date_msk, nm_id FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_FUNNEL_DAILY`
      GROUP BY 1, 2, 3 HAVING COUNT(*) > 1)
UNION ALL
-- T3. Нет дыр внутри загруженного диапазона (дни — из календаря, а не из данных).
SELECT 'T3_no_gaps', COUNTIF(status = 'MISSING'), IF(COUNTIF(status = 'MISSING') = 0, 'PASS', 'FAIL')
FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FUNNEL_COVERAGE`
UNION ALL
-- T4. Обе ключевые метрики заполнены у крема для рук на каждом загруженном дне.
SELECT 'T4_cream_opens_carts_not_null', COUNTIF(open_card_count IS NULL OR add_to_cart_count IS NULL),
       IF(COUNTIF(open_card_count IS NULL OR add_to_cart_count IS NULL) = 0, 'PASS', 'FAIL')
FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FUNNEL_DAILY` WHERE nm_id = 252442517
UNION ALL
-- T5. Переходы — вся воронка, а не клики рекламы: переходов не меньше кликов в любой день.
SELECT 'T5_opens_ge_ad_clicks', COUNTIF(f.open_card_count < m.clicks), IF(COUNTIF(f.open_card_count < m.clicks) = 0, 'PASS', 'FAIL')
FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FUNNEL_DAILY` f
JOIN (SELECT day, nm_id, SUM(clicks) AS clicks FROM `project-fa311fc0-4d87-4781-986.wb_mart.MART_SKU_DAILY` GROUP BY 1, 2) m
  ON m.day = f.date_msk AND m.nm_id = f.nm_id
UNION ALL
-- T6. Последний прогон загрузчика завершился без ошибки и без дрейфа схемы.
SELECT 'T6_last_observation_ok', COUNTIF(status NOT IN ('COMPLETE', 'REUSED') OR schema_status != 'OK'),
       IF(COUNTIF(status NOT IN ('COMPLETE', 'REUSED') OR schema_status != 'OK') = 0, 'PASS', 'FAIL')
FROM (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_raw.WB_FUNNEL_OBSERVATIONS`
      WHERE environment = 'prod' ORDER BY started_at DESC LIMIT 1);
