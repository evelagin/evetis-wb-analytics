-- ============================================================================
-- STAGE 3.1C PR1 — ACCEPTANCE / ВАЛИДАЦИЯ (READ-ONLY)
-- Дата: 2026-08-27.  Сборка: sql/mart/pr_ad_billing_classification_v1.sql
--
-- 🔴 A1-A8, A12, A13 — ИНВАРИАНТЫ. Нарушение блокирует выкат.
-- 🔴 A9-A11      — МОНИТОРИНГ качества классификации, НЕ бухгалтерские
--    инварианты. Их деградация означает «посмотри на данные», а не «всё сломано».
--
-- 🔴 ОКНО СРАВНЕНИЯ ЗАДАНО ПРАВИЛОМ, А НЕ СПИСКОМ ДАТ. Хардкод конкретных
--    аномальных недель был бы подгонкой. Правило: из сравнения исключаются
--    неделя со сторно (billed <= 0) и следующая за ней неделя перевыставления,
--    плюс незакрытая хвостовая неделя. На данных 27.08.2026 это механически
--    даёт те же 17 недель, что и forensic Stage 3.1C.
--
-- Все запросы — только SELECT.
-- ============================================================================

-- ── A1-A7. Структурные инварианты классификации ────────────────────────────
WITH c AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_DEDUCTIONS_CLASSIFIED`),
canon AS (
  SELECT COUNT(*) n, SUM(IFNULL(deduction, NUMERIC '0') + IFNULL(additional_payment, NUMERIC '0')) amt
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_FINANCE` WHERE supplier_oper_name = 'Удержание')
SELECT 'A1 ровно один класс на каждую каноническую операцию' AS check_name,
       (SELECT COUNT(*) FROM c) AS actual, (SELECT n FROM canon) AS expected,
       IF((SELECT COUNT(*) FROM c) = (SELECT n FROM canon)
          AND (SELECT COUNTIF(deduction_class NOT IN
                ('AD_BILLING_RECONSTRUCTED','TRANSIT_DEDUCTION','UNCLASSIFIED_DEDUCTION','CLASSIFICATION_CONFLICT')) FROM c) = 0,
          'PASS','FAIL') AS verdict
UNION ALL SELECT 'A2 CLASSIFICATION_CONFLICT = 0',
       (SELECT COUNTIF(deduction_class='CLASSIFICATION_CONFLICT') FROM c), 0,
       IF((SELECT COUNTIF(deduction_class='CLASSIFICATION_CONFLICT') FROM c)=0,'PASS','FAIL')
UNION ALL SELECT 'A3 deduction_amount_rub NOT NULL',
       (SELECT COUNTIF(deduction_amount_rub IS NULL) FROM c), 0,
       IF((SELECT COUNTIF(deduction_amount_rub IS NULL) FROM c)=0,'PASS','FAIL')
UNION ALL SELECT 'A5 TRANSIT только transit-pattern',
       (SELECT COUNTIF(deduction_class='TRANSIT_DEDUCTION'
          AND NOT REGEXP_CONTAINS(IFNULL(srid,''), r'^transit_deduction_[0-9]+$')) FROM c), 0,
       IF((SELECT COUNTIF(deduction_class='TRANSIT_DEDUCTION'
          AND NOT REGEXP_CONTAINS(IFNULL(srid,''), r'^transit_deduction_[0-9]+$')) FROM c)=0,'PASS','FAIL')
UNION ALL SELECT 'A6 AD_BILLING только hex40-pattern',
       (SELECT COUNTIF(deduction_class='AD_BILLING_RECONSTRUCTED'
          AND NOT REGEXP_CONTAINS(IFNULL(srid,''), r'^[0-9a-f]{40}$')) FROM c), 0,
       IF((SELECT COUNTIF(deduction_class='AD_BILLING_RECONSTRUCTED'
          AND NOT REGEXP_CONTAINS(IFNULL(srid,''), r'^[0-9a-f]{40}$')) FROM c)=0,'PASS','FAIL')
UNION ALL SELECT 'A7 UNCLASSIFIED не соответствует ни одному правилу',
       (SELECT COUNTIF(deduction_class='UNCLASSIFIED_DEDUCTION'
          AND (REGEXP_CONTAINS(IFNULL(srid,''), r'^[0-9a-f]{40}$')
            OR REGEXP_CONTAINS(IFNULL(srid,''), r'^transit_deduction_[0-9]+$'))) FROM c), 0,
       IF((SELECT COUNTIF(deduction_class='UNCLASSIFIED_DEDUCTION'
          AND (REGEXP_CONTAINS(IFNULL(srid,''), r'^[0-9a-f]{40}$')
            OR REGEXP_CONTAINS(IFNULL(srid,''), r'^transit_deduction_[0-9]+$'))) FROM c)=0,'PASS','FAIL')
ORDER BY check_name;

-- ── A4. Ни одна копейка не потеряна при классификации ──────────────────────
SELECT 'A4 сумма классифицированного = каноническому итогу' AS check_name,
       (SELECT ROUND(SUM(deduction_amount_rub),2) FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_DEDUCTIONS_CLASSIFIED`) AS classified_total,
       (SELECT ROUND(SUM(IFNULL(deduction, NUMERIC '0') + IFNULL(additional_payment, NUMERIC '0')),2)
          FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_FINANCE` WHERE supplier_oper_name='Удержание') AS canonical_total,
       IF((SELECT SUM(deduction_amount_rub) FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_DEDUCTIONS_CLASSIFIED`)
        = (SELECT SUM(IFNULL(deduction, NUMERIC '0') + IFNULL(additional_payment, NUMERIC '0'))
             FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_FINANCE` WHERE supplier_oper_name='Удержание'),
          'PASS','FAIL') AS verdict;

-- ── A8. Сверка с forensic-контролем 01-26.08.2026 ──────────────────────────
--   Расхождение допустимо ТОЛЬКО как приход новых канонических строк и обязано
--   быть объяснено дельтой. Значения НЕ подгонять.
SELECT 'A8 контроль 01-26.08.2026' AS check_name, deduction_class,
       ROUND(SUM(deduction_amount_rub),2) AS actual_rub, COUNT(*) AS rows_
FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_DEDUCTIONS_CLASSIFIED`
WHERE finance_date BETWEEN '2026-08-01' AND '2026-08-26'
GROUP BY ROLLUP(deduction_class) ORDER BY deduction_class;
-- Forensic baseline: TOTAL 57 186.19 = AD_BILLING 52 875.00 + TRANSIT 2 596.45
--                                     + UNCLASSIFIED 1 714.74

-- ── A9 / A10. Мониторинг связи биллинга и атрибуции ────────────────────────
WITH r AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_ADVERTISING_RECONCILIATION_DAILY`
           WHERE ads_attribution_covered),
wk AS (SELECT DATE_TRUNC(day, WEEK(MONDAY)) w, SUM(ad_spend_billed_rub) billed,
              SUM(ad_spend_attributed_rub) attr FROM r GROUP BY w),
tail AS (SELECT DATE_TRUNC((SELECT MAX(day) FROM r), WEEK(MONDAY)) lw),
-- Неделя сторно и следующая за ней неделя перевыставления определяются
-- ПРАВИЛОМ (billed <= 0), а не перечислением дат.
reversal AS (SELECT w FROM wk WHERE billed <= 0),
core AS (
  SELECT wk.* FROM wk, tail
  WHERE wk.w < tail.lw
    AND wk.w NOT IN (SELECT w FROM reversal)
    AND wk.w NOT IN (SELECT DATE_ADD(w, INTERVAL 1 WEEK) FROM reversal)),
full_ AS (SELECT wk.* FROM wk, tail WHERE wk.w < tail.lw)
SELECT 'A9 correlation billed~attributed (CORE)' AS check_name,
       CAST(ROUND((SELECT CORR(CAST(billed AS FLOAT64), CAST(attr AS FLOAT64)) FROM core),4) AS STRING) AS value,
       CAST((SELECT COUNT(*) FROM core) AS STRING) AS weeks,
       IF((SELECT CORR(CAST(billed AS FLOAT64), CAST(attr AS FLOAT64)) FROM core) >= 0.95,'OK','WARNING') AS status
UNION ALL SELECT 'A10 ratio billed/attributed (CORE)',
       CAST(ROUND((SELECT SAFE_DIVIDE(SUM(billed),SUM(attr)) FROM core),4) AS STRING),
       CAST((SELECT COUNT(*) FROM core) AS STRING),
       IF((SELECT SAFE_DIVIDE(SUM(billed),SUM(attr)) FROM core) BETWEEN 0.90 AND 1.10,'OK','DEGRADED')
UNION ALL SELECT 'A9/A10 наблюдение: FULL window (со сторно)',
       CONCAT('corr=', CAST(ROUND((SELECT CORR(CAST(billed AS FLOAT64), CAST(attr AS FLOAT64)) FROM full_),4) AS STRING),
              ' ratio=', CAST(ROUND((SELECT SAFE_DIVIDE(SUM(billed),SUM(attr)) FROM full_),4) AS STRING)),
       CAST((SELECT COUNT(*) FROM full_) AS STRING), 'INFO'
UNION ALL SELECT 'недель со сторно (billed <= 0)',
       CAST((SELECT COUNT(*) FROM reversal) AS STRING), '', 'INFO';

-- ── A11. Доля неклассифицированного — по периодам, а не одним числом ───────
SELECT 'A11 unknown share' AS check_name, scope,
       ROUND(total,2) AS total_rub, ROUND(unknown,2) AS unclassified_rub,
       ROUND(100*SAFE_DIVIDE(unknown,total),2) AS unknown_pct,
       IF(SAFE_DIVIDE(unknown,total) <= 0.10, 'ACCEPTABLE', 'DEGRADED') AS status
FROM (
  SELECT 'lifetime' scope, SUM(deduction_amount_rub) total,
         SUM(IF(is_unclassified, deduction_amount_rub, NUMERIC '0')) unknown
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_DEDUCTIONS_CLASSIFIED`
  UNION ALL
  SELECT FORMAT_DATE('%Y', finance_date), SUM(deduction_amount_rub),
         SUM(IF(is_unclassified, deduction_amount_rub, NUMERIC '0'))
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_DEDUCTIONS_CLASSIFIED` GROUP BY 1
  UNION ALL
  SELECT 'последние 90 суток', SUM(deduction_amount_rub),
         SUM(IF(is_unclassified, deduction_amount_rub, NUMERIC '0'))
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_DEDUCTIONS_CLASSIFIED`
  WHERE finance_date >= DATE_SUB(CURRENT_DATE('Europe/Moscow'), INTERVAL 90 DAY))
ORDER BY scope;

-- ── A12 / A13. Границы слоя ────────────────────────────────────────────────
--   A12: ни Product COGS, ни legacy_cogs, ни ff_cost, ни налог не участвуют.
--   A13: nm_id отсутствует — удержание уровня счёта не является затратой SKU.
SELECT 'A12/A13 границы слоя' AS check_name, table_name,
       COUNTIF(LOWER(column_name) LIKE '%nm_id%')      AS nm_id_cols,
       COUNTIF(LOWER(column_name) LIKE '%cogs%')       AS cogs_cols,
       COUNTIF(LOWER(column_name) LIKE '%ff_cost%')    AS ff_cols,
       COUNTIF(LOWER(column_name) LIKE '%tax%')        AS tax_cols,
       IF(COUNTIF(LOWER(column_name) LIKE '%nm_id%')
        + COUNTIF(LOWER(column_name) LIKE '%cogs%')
        + COUNTIF(LOWER(column_name) LIKE '%ff_cost%')
        + COUNTIF(LOWER(column_name) LIKE '%tax%') = 0, 'PASS','FAIL') AS verdict
FROM `project-fa311fc0-4d87-4781-986.wb_mart.INFORMATION_SCHEMA.COLUMNS`
WHERE table_name IN ('V_WB_DEDUCTIONS_CLASSIFIED','V_ADVERTISING_RECONCILIATION_DAILY')
GROUP BY table_name ORDER BY table_name;

-- ── Поправка №5: покрытие атрибуции не создаёт фиктивный unallocated ───────
SELECT 'COVERAGE-GUARD вне окна атрибуции' AS check_name,
       COUNTIF(NOT ads_attribution_covered) AS days_outside_coverage,
       COUNTIF(NOT ads_attribution_covered AND ad_spend_billed_rub <> 0) AS days_with_billed_but_no_attribution,
       COUNTIF(NOT ads_attribution_covered AND ad_spend_unallocated_rub IS NOT NULL) AS leaked_unallocated,
       IF(COUNTIF(NOT ads_attribution_covered AND ad_spend_unallocated_rub IS NOT NULL) = 0
          AND COUNTIF(NOT ads_attribution_covered AND ad_spend_attributed_rub IS NOT NULL) = 0,
          'PASS','FAIL') AS verdict
FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_ADVERTISING_RECONCILIATION_DAILY`;

-- ── Диагностика: структура удержаний по годам ─────────────────────────────
SELECT FORMAT_DATE('%Y', finance_date) AS year,
       ROUND(SUM(deduction_amount_rub),2) AS total_rub,
       ROUND(SUM(IF(is_ad_billing_reconstructed, deduction_amount_rub, NUMERIC '0')),2) AS ad_billing_rub,
       ROUND(SUM(IF(is_transit_deduction, deduction_amount_rub, NUMERIC '0')),2) AS transit_rub,
       ROUND(SUM(IF(is_unclassified, deduction_amount_rub, NUMERIC '0')),2) AS unclassified_rub,
       COUNTIF(transit_supply_resolved) AS transit_supply_resolved_rows
FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_DEDUCTIONS_CLASSIFIED`
GROUP BY year ORDER BY year;
