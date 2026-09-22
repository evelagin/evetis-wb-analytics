-- ============================================================================
-- UBR-012 · ПРЕДПРОВЕРКА ПЕРЕД РАЗВЁРТЫВАНИЕМ. ТОЛЬКО ЧТЕНИЕ.
-- Все пять строк обязаны вернуть PASS. Любой FAIL — развёртывание не начинать.
-- ============================================================================
WITH
-- 1. Живой Control Tower ещё содержит дефект ровно той величины, что описана в манифесте.
p1 AS (
  SELECT 'PRE1_CT_DEFECT_IS_12788_84' AS check_id,
    ROUND((SELECT SUM(revenue_seller_base) FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_ACTUAL_DAILY_LIVE` WHERE marketplace = 'OZON')
        - (SELECT SUM(seller_base_revenue_rub) FROM `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_SKU_PNL_DAILY`), 2) AS observed,
    12788.84 AS expected),
-- 2. Реестр выкупов не изменился: 35 доказанных документов, недоказанных нет.
p2 AS (
  SELECT 'PRE2_REGISTRY_35_PROVEN' AS check_id,
    (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_CIS_BUYOUT`) AS observed,
    35 AS expected),
-- 3. Недоказанных выкупов нет — иначе fail-closed изменит суммы, а не только гарантию.
p3 AS (
  SELECT 'PRE3_NO_UNPROVEN_BUYOUT' AS check_id,
    (SELECT SUM(buyout_revenue_unproven_qty) FROM `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_SKU_PNL_DAILY`) AS observed,
    0 AS expected),
-- 4. Обычных продаж без начисления нет — второй путь подстановки цены заказа мёртв.
p4 AS (
  SELECT 'PRE4_NO_COMMISSION_MISSING' AS check_id,
    (SELECT SUM(commission_missing_qty) FROM `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_SKU_PNL_DAILY`) AS observed,
    0 AS expected),
-- 5. Множества строк Control Tower и витрины тождественны: LEFT JOIN за выручкой не потеряет строку.
p5 AS (
  SELECT 'PRE5_ROW_SETS_IDENTICAL' AS check_id,
    (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_SKU_PNL_DAILY` f
     WHERE (f.fact_date, f.internal_sku) NOT IN (
       SELECT (d, internal_sku) FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_ACTUAL_DAILY_LIVE` WHERE marketplace = 'OZON'))
    AS observed,
    0 AS expected)
SELECT check_id, observed, expected,
       IF(ABS(observed - expected) <= 0.01, 'PASS', 'FAIL') AS status
FROM (SELECT * FROM p1 UNION ALL SELECT * FROM p2 UNION ALL SELECT * FROM p3
      UNION ALL SELECT * FROM p4 UNION ALL SELECT * FROM p5)
ORDER BY check_id;
