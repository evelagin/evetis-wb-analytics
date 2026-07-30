-- ============================================================================
-- PR-Mart2a — read-only контрольные запросы (аудитор / повторная сверка).
-- Ничего не создаёт. Прогоняются на wb_mart ПОСЛЕ применения pr_mart2a_finance_longform.sql.
-- Ожидаемые числа — снимок 2026-07-30 (на живых данных счётчики растут, инварианты держатся).
-- Сводка приёмки 30.07: long_rows=240292; ref_pairs=22; categories=10; unknown=0;
--   total cost_positive=9 479 676.06; SKU 4 243 822.66 + ACCOUNT 5 235 853.40; Σ signed=6 989 474.30.
-- ============================================================================

-- 1) REF_COST_MAP: 22 пары, уникальны, домены direction/sign, 10 категорий.
SELECT COUNT(*) ref_pairs,                                    -- 22
  COUNT(DISTINCT FORMAT('%t|%t', op_key, amount_field)) dk,   -- 22
  COUNT(DISTINCT cost_category) categories,                   -- 10
  COUNTIF(economic_direction NOT IN ('COST','CREDIT','ADJUSTMENT')) dir_bad,  -- 0
  COUNTIF(field_normalization_sign NOT IN (-1,1)) sign_bad     -- 0
FROM `wb_mart.REF_COST_MAP`;

-- 2) LONG: грейн (finance_row_key × amount_field) уникален; строк 240292.
SELECT COUNT(*) long_rows,                                                       -- 240292
  COUNT(DISTINCT FORMAT('%t|%t', finance_row_key, amount_field)) dk,             -- == long_rows
  ROUND(SUM(source_signed_amount),2) sum_signed                                  -- 6 989 474.30
FROM `wb_mart.V_WB_FINANCE_AMOUNTS_LONG`;

-- 3) Лемма консервации по-полю: LONG == FACT (ожидание bad=0). Σ signed по полю:
--   commission -1 327 094.41; deduction 4 582 269.34; logistics 2 744 332.23; acquiring 440 482.53;
--   storage 403 566.92; acceptance 116 587.80; additional_payment 7 335.89; penalty 21 994; other 0 строк.
SELECT COUNTIF(ABS(long_sum - fact_sum) > 0.005) bad
FROM (SELECT amount_field, SUM(source_signed_amount) long_sum FROM `wb_mart.V_WB_FINANCE_AMOUNTS_LONG` GROUP BY amount_field) L
JOIN (
  SELECT 'commission_amount' amount_field, SUM(commission_amount) fact_sum FROM `wb_mart.FACT_FINANCE`
  UNION ALL SELECT 'logistics_amount', SUM(logistics_amount) FROM `wb_mart.FACT_FINANCE`
  UNION ALL SELECT 'storage_fee', SUM(storage_fee) FROM `wb_mart.FACT_FINANCE`
  UNION ALL SELECT 'deduction', SUM(deduction) FROM `wb_mart.FACT_FINANCE`
  UNION ALL SELECT 'penalty', SUM(penalty) FROM `wb_mart.FACT_FINANCE`
  UNION ALL SELECT 'acceptance', SUM(acceptance) FROM `wb_mart.FACT_FINANCE`
  UNION ALL SELECT 'acquiring_fee', SUM(acquiring_fee) FROM `wb_mart.FACT_FINANCE`
  UNION ALL SELECT 'additional_payment', SUM(additional_payment) FROM `wb_mart.FACT_FINANCE`
  UNION ALL SELECT 'other_amount', SUM(other_amount) FROM `wb_mart.FACT_FINANCE`
) F USING (amount_field);

-- 4) MAPPED: unknown money-pairs = 0; знак cost_positive соответствует направлению.
SELECT
  (SELECT COUNT(*) FROM (SELECT op_key,amount_field FROM `wb_mart.V_WB_FINANCE_AMOUNTS_LONG_MAPPED` WHERE cost_category IS NULL GROUP BY 1,2)) unknown_pairs, -- 0
  COUNTIF(economic_direction='COST' AND cost_amount_positive<0) cost_neg,   -- 0
  COUNTIF(economic_direction='CREDIT' AND cost_amount_positive>0) credit_pos -- 0
FROM `wb_mart.V_WB_FINANCE_AMOUNTS_LONG_MAPPED`;

-- 5) Консервация расщепления SKU/ACCOUNT: total == SKU + ACCOUNT (потерь нет).
SELECT ROUND(SUM(cost_amount_positive),2) total_cost_pos,                        -- 9 479 676.06
  ROUND(SUM(IF(is_sku_row,cost_amount_positive,0)),2) sku_costs,                 -- 4 243 822.66
  ROUND(SUM(IF(NOT is_sku_row,cost_amount_positive,0)),2) account_costs         -- 5 235 853.40
FROM `wb_mart.V_WB_FINANCE_AMOUNTS_LONG_MAPPED`;

-- 6) Разбивка cost_positive по категориям (обзор структуры затрат, для дашборда/аудита).
SELECT cost_category, economic_direction, COUNT(*) n, ROUND(SUM(cost_amount_positive),2) cost_pos
FROM `wb_mart.V_WB_FINANCE_AMOUNTS_LONG_MAPPED`
GROUP BY 1,2 ORDER BY cost_pos DESC;

-- 7) compensation guard: должно быть 0 ненулевых.
SELECT COUNTIF(compensation_amount IS NOT NULL AND compensation_amount<>0) comp_nonzero  -- 0
FROM `wb_mart.FACT_FINANCE`;
