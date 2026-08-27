-- ============================================================================
-- STAGE 3.1A — ACCEPTANCE / ВАЛИДАЦИЯ (READ-ONLY)
-- Дата: 2026-08-27.  Основной скрипт: sql/ref/pr_ref_cogs_history.sql
--
-- ПОЧЕМУ ОТДЕЛЬНЫЙ ФАЙЛ. Проверка покрытия событий WB-специфична: она читает
--   wb_raw.RAW_WB_FINANCE и wb_raw.REF_SKU_MASTER. Если бы она жила вью внутри
--   evetis_ref, справочник себестоимости оказался бы привязан к WB-контуру.
--   Решение владельца (ACK GATE A): evetis_ref остаётся marketplace-independent,
--   V_PRODUCT_COGS_EFFECTIVE зависит только от объектов внутри evetis_ref.
--
-- Все запросы ниже — только SELECT. Ничего не создают и не меняют.
-- ============================================================================

-- ── AC-1..AC-4. Состав слоя ────────────────────────────────────────────────
SELECT 'AC-1 REF_SKU_COGS_HISTORY' AS check_name,
       COUNT(*) AS actual, 17 AS expected,
       COUNTIF(cogs_origin_type='PURCHASE_BATCH') AS purchase_batch,
       COUNTIF(cogs_origin_type='INVENTORY_TRANSFORMATION') AS transformation,
       COUNTIF(cogs_origin_type='IMPORTED_FINISHED_SET') AS imported_set,
       IF(COUNT(*)=17 AND COUNTIF(cogs_origin_type='PURCHASE_BATCH')=14
          AND COUNTIF(cogs_origin_type='INVENTORY_TRANSFORMATION')=2
          AND COUNTIF(cogs_origin_type='IMPORTED_FINISHED_SET')=1,'PASS','FAIL') AS verdict
FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_COGS_HISTORY`;

SELECT 'AC-2 REF_BUNDLE_COMPONENTS' AS check_name, COUNT(*) AS rows_, 33 AS expected_rows,
       COUNT(DISTINCT bundle_internal_sku) AS bundles_, 14 AS expected_bundles,
       IF(COUNT(*)=33 AND COUNT(DISTINCT bundle_internal_sku)=14,'PASS','FAIL') AS verdict
FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_BUNDLE_COMPONENTS`;

SELECT 'AC-3 V_BUNDLE_COGS_DERIVED' AS check_name, COUNT(*) AS actual, 21 AS expected,
       IF(COUNT(*)=21,'PASS','FAIL') AS verdict
FROM `project-fa311fc0-4d87-4781-986.evetis_ref.V_BUNDLE_COGS_DERIVED`;

SELECT 'AC-4 V_PRODUCT_COGS_EFFECTIVE' AS check_name, COUNT(*) AS actual, 38 AS expected,
       COUNTIF(product_cogs_rub IS NULL) AS null_cogs,
       IF(COUNT(*)=38 AND COUNTIF(product_cogs_rub IS NULL)=0,'PASS','FAIL') AS verdict
FROM `project-fa311fc0-4d87-4781-986.evetis_ref.V_PRODUCT_COGS_EFFECTIVE`;

-- ── AC-6. Event coverage через unified resolver (WB-специфично) ────────────
--   Инвариант I-10: на каждое COGS-требующее событие resolver возвращает
--   РОВНО одно значение. matches=0 -> 0 событий, matches>1 -> 0 событий.
--   total_current_events сравнивается с baseline Stage 3.0.3B = 40 110;
--   расхождение допустимо только как приход новых данных WB и обязано быть
--   объяснено дельтой, а не подгонкой контракта.
WITH ev AS (
  SELECT SAFE_CAST(wb_nm_id AS INT64) AS nm, _rr_date AS dt, SAFE_CAST(quantity AS INT64) AS q,
         GENERATE_UUID() AS ev_id
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_FINANCE`
  WHERE supplier_oper_name IN ('Продажа','Возврат')
), mapped AS (
  SELECT ev.ev_id, ev.nm, m.internal_sku, ev.dt, ev.q
  FROM ev LEFT JOIN `project-fa311fc0-4d87-4781-986.wb_raw.REF_SKU_MASTER` m ON m.nm_id = ev.nm
), scored AS (
  SELECT mp.ev_id, ANY_VALUE(mp.internal_sku) AS internal_sku, ANY_VALUE(mp.dt) AS dt, ANY_VALUE(mp.q) AS q,
         COUNTIF(r.internal_sku IS NOT NULL) AS matches
  FROM mapped mp
  LEFT JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.V_PRODUCT_COGS_EFFECTIVE` r
    ON r.internal_sku = mp.internal_sku
   AND mp.dt BETWEEN r.effective_from AND COALESCE(r.effective_to, DATE '9999-12-31')
  GROUP BY mp.ev_id
)
SELECT 'AC-6 event coverage' AS check_name, matches, COUNT(*) AS events, SUM(q) AS units,
       COUNT(DISTINCT internal_sku) AS skus,
       CAST(MIN(dt) AS STRING) AS min_dt, CAST(MAX(dt) AS STRING) AS max_dt
FROM scored GROUP BY matches ORDER BY matches;

-- ── AC-7 / AC-8. Текущий срез: 11 базовых SKU и 14 наборов ────────────────
--   Ожидается 23 значения OK и 2 SKU без действующего интервала:
--   EVT-HC-BODY-300 и EVT-SET-HAND-BODY выбыли 2026-04-26 (fail-closed by design).
WITH cur AS (
  SELECT internal_sku, product_cogs_rub, resolver_lane
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.V_PRODUCT_COGS_EFFECTIVE`
  WHERE CURRENT_DATE() BETWEEN effective_from AND COALESCE(effective_to, DATE '9999-12-31')
), expect AS (
  SELECT * FROM UNNEST([
   STRUCT('EVT-HC-HAND-300' AS sku, NUMERIC '240' AS exp),('EVT-HC-BODY-300',NUMERIC '219'),
   ('EVT-HC-CHERRY-300',NUMERIC '172'),('EVT-HC-AMBER-300',NUMERIC '172'),
   ('EVT-FS-MOIST-30',NUMERIC '133'),('EVT-FS-ACNE-30',NUMERIC '138'),
   ('EVT-FC-MOIST-50',NUMERIC '145'),('EVT-FC-ACNE-50',NUMERIC '145'),
   ('EVT-FT-MOIST-150',NUMERIC '182'),('EVT-FT-ACNE-150',NUMERIC '182'),
   ('EVT-EP-ENZYME-75',NUMERIC '159'),
   ('EVT-SET-HAND-BODY',NUMERIC '459'),('EVT-SET-HAND-CHERRY',NUMERIC '412'),('EVT-SET-HAND-AMBER',NUMERIC '412'),
   ('EVT-SET-CHERRY-AMBER',NUMERIC '344'),('EVT-SET-SER-CREAM-MOIST',NUMERIC '278'),('EVT-SET-SER-CREAM-ACNE',NUMERIC '283'),
   ('EVT-SET-TON-CREAM-MOIST',NUMERIC '327'),('EVT-SET-TON-CREAM-ACNE',NUMERIC '327'),
   ('EVT-SET-TON-SER-CREAM-MOIST',NUMERIC '460'),('EVT-SET-TON-SER-CREAM-ACNE',NUMERIC '465'),
   ('EVT-SET-4PC-ACNE',NUMERIC '624'),('EVT-SET-ACNE-POWDER-SERUM-CREAM',NUMERIC '442'),
   ('EVT-SET-MOIST-TONIC-SERUM',NUMERIC '315'),('EVT-SET-ACNE-TONIC-SERUM',NUMERIC '320')])
)
SELECT e.sku, e.exp AS expected, c.product_cogs_rub AS actual, c.resolver_lane,
       IF(c.product_cogs_rub = e.exp,'OK',
          IF(c.product_cogs_rub IS NULL,'НЕТ ДЕЙСТВУЮЩЕГО ИНТЕРВАЛА (fail-closed by design)','MISMATCH')) AS verdict
FROM expect e LEFT JOIN cur c ON c.internal_sku = e.sku ORDER BY verdict DESC, e.sku;

-- ── AC-9. Шесть management boundaries ─────────────────────────────────────
WITH exp AS (
  SELECT * FROM UNNEST([
    STRUCT('T1' AS tr, DATE '2025-01-26' AS b),('T2',DATE '2025-03-10'),('T3',DATE '2025-11-01'),
    ('T4',DATE '2025-05-01'),('T5',DATE '2026-03-01'),('T6',DATE '2026-08-01')])
), act AS (
  SELECT effective_from_transition_id AS tr, effective_from AS b, internal_sku
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_COGS_HISTORY` WHERE effective_from_transition_id IS NOT NULL
  UNION ALL
  SELECT DISTINCT effective_from_transition_id, effective_from, bundle_internal_sku
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_BUNDLE_COMPONENTS` WHERE effective_from_transition_id IS NOT NULL
)
SELECT 'AC-9 boundaries' AS check_name, exp.tr, CAST(exp.b AS STRING) AS expected,
       STRING_AGG(DISTINCT CAST(act.b AS STRING)) AS actual,
       STRING_AGG(DISTINCT act.internal_sku) AS applies_to,
       IF(LOGICAL_AND(act.b = exp.b),'PASS','FAIL') AS verdict
FROM exp LEFT JOIN act ON act.tr = exp.tr GROUP BY exp.tr, exp.b ORDER BY exp.tr;

-- ── AC-10 / AC-11. Baseline соседних слоёв не тронут ──────────────────────
SELECT 'wb_raw' AS ds, COUNT(*) AS objects, 56 AS baseline, IF(COUNT(*)=56,'PASS','FAIL') AS verdict
FROM `project-fa311fc0-4d87-4781-986.wb_raw.INFORMATION_SCHEMA.TABLES`
-- Structural baseline maintenance (Stage 3.1B PR1, 2026-08-27): wb_mart 35 -> 37.
--   +wb_mart.V_FACT_FINANCE_COGS, +wb_mart.V_MART_SKU_DAILY_COGS (sql/mart/pr_cogs_consumer_v1.sql).
--   Это обслуживание СТРУКТУРНОГО baseline, а НЕ переоткрытие Stage 3.1A: экономические
--   инварианты (17 / 33 / 21 / 38, AC-6..AC-9) не изменены ни одним символом.
UNION ALL SELECT 'wb_mart', COUNT(*), 37, IF(COUNT(*)=37,'PASS','FAIL')
FROM `project-fa311fc0-4d87-4781-986.wb_mart.INFORMATION_SCHEMA.TABLES`
UNION ALL SELECT 'evetis_ref', COUNT(*), 4, IF(COUNT(*)=4,'PASS','FAIL')
FROM `project-fa311fc0-4d87-4781-986.evetis_ref.INFORMATION_SCHEMA.TABLES`
UNION ALL SELECT 'MART_SKU_DAILY rows', (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_mart.MART_SKU_DAILY`), 7477,
  IF((SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_mart.MART_SKU_DAILY`)=7477,'PASS','FAIL')
UNION ALL SELECT 'REF_COST_MAP rules', (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_mart.REF_COST_MAP`), 19,
  IF((SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_mart.REF_COST_MAP`)=19,'PASS','FAIL')
ORDER BY ds;

-- ── Справочный листинг: все 38 интервалов resolver ────────────────────────
SELECT internal_sku, CAST(effective_from AS STRING) AS eff_from,
       IFNULL(CAST(effective_to AS STRING),'—') AS eff_to,
       product_cogs_rub, resolver_lane, cogs_origin_type, resolver_ref, confidence
FROM `project-fa311fc0-4d87-4781-986.evetis_ref.V_PRODUCT_COGS_EFFECTIVE`
ORDER BY resolver_lane, internal_sku, effective_from;
