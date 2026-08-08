-- ============================================================================
-- PR-Mart2a — read-only контрольные запросы (аудитор / повторная сверка). REV2.
-- Ничего не создаёт. §1-§2 работают на FACT_FINANCE (можно ДО publish); §3+ — на публикуемых
-- объектах ПОСЛЕ применения pr_mart2a_finance_longform.sql (или при dry-run — см. §7 инлайн-реплика).
-- Снимок dry-run 2026-07-30: conservation_bad=0 (9/9 полей), domain_bad=0, norm_bad=0, unknown=0,
--   comp_nonzero=0; total 9 479 676.06 = SKU 4 243 822.66 + ACCOUNT 5 235 853.40; Σ signed 6 989 474.30.
-- ============================================================================

-- 1) Лемма консервации по-полю — ЯВНЫЙ spine всех 9 amount_field + LEFT JOIN + COALESCE (fix #2).
--    other_amount (нет ненулевых LONG-строк) ОСТАЁТСЯ в проверке. Ожидание: conservation_bad=0, fields=9.
WITH long AS (
  SELECT u.amount_field, u.s AS source_signed_amount
  FROM `wb_mart.FACT_FINANCE`,
  UNNEST([STRUCT('commission_amount' AS amount_field, commission_amount AS s),
    ('logistics_amount',logistics_amount),('storage_fee',storage_fee),('deduction',deduction),
    ('penalty',penalty),('acceptance',acceptance),('acquiring_fee',acquiring_fee),
    ('additional_payment',additional_payment),('other_amount',other_amount)]) u
  WHERE u.s IS NOT NULL AND u.s <> 0),
fields AS (SELECT f FROM UNNEST(['commission_amount','logistics_amount','storage_fee','deduction',
             'penalty','acceptance','acquiring_fee','additional_payment','other_amount']) f),
ls AS (SELECT amount_field, SUM(source_signed_amount) s FROM long GROUP BY amount_field),
fs AS (
  SELECT 'commission_amount' amount_field, COALESCE(SUM(commission_amount),0) fact_sum FROM `wb_mart.FACT_FINANCE`
  UNION ALL SELECT 'logistics_amount', COALESCE(SUM(logistics_amount),0) FROM `wb_mart.FACT_FINANCE`
  UNION ALL SELECT 'storage_fee', COALESCE(SUM(storage_fee),0) FROM `wb_mart.FACT_FINANCE`
  UNION ALL SELECT 'deduction', COALESCE(SUM(deduction),0) FROM `wb_mart.FACT_FINANCE`
  UNION ALL SELECT 'penalty', COALESCE(SUM(penalty),0) FROM `wb_mart.FACT_FINANCE`
  UNION ALL SELECT 'acceptance', COALESCE(SUM(acceptance),0) FROM `wb_mart.FACT_FINANCE`
  UNION ALL SELECT 'acquiring_fee', COALESCE(SUM(acquiring_fee),0) FROM `wb_mart.FACT_FINANCE`
  UNION ALL SELECT 'additional_payment', COALESCE(SUM(additional_payment),0) FROM `wb_mart.FACT_FINANCE`
  UNION ALL SELECT 'other_amount', COALESCE(SUM(other_amount),0) FROM `wb_mart.FACT_FINANCE`)
SELECT COUNT(*) fields_checked,                                                         -- 9
  COUNTIF(ABS(COALESCE(ls.s,0)-COALESCE(fs.fact_sum,0))>0.005) conservation_bad,        -- 0
  ROUND(SUM(COALESCE(ls.s,0)),2) sum_signed_all                                         -- 6 989 474.30
FROM fields fld LEFT JOIN ls ON ls.amount_field=fld.f LEFT JOIN fs ON fs.amount_field=fld.f;

-- 2) compensation guard.
SELECT COUNTIF(compensation_amount IS NOT NULL AND compensation_amount<>0) comp_nonzero  -- 0
FROM `wb_mart.FACT_FINANCE`;

-- 3) REF_COST_MAP: 23 пары, уникальны, NULL-safe домены (fix #3), 10 категорий. (ПОСЛЕ publish)
SELECT COUNT(*) ref_pairs,                                                              -- 23
  COUNT(DISTINCT FORMAT('%t|%t',op_key,amount_field)) dk,                               -- 23
  COUNT(DISTINCT cost_category) categories,                                             -- 10
  COUNTIF(economic_direction IS NULL OR economic_direction NOT IN ('COST','CREDIT','ADJUSTMENT')) dir_bad, -- 0
  COUNTIF(field_normalization_sign IS NULL OR field_normalization_sign NOT IN (-1,1)) sign_bad,            -- 0
  COUNTIF(cost_category IS NULL OR TRIM(cost_category)='') cat_bad                       -- 0
FROM `wb_mart.REF_COST_MAP`;

-- 4) LONG: грейн (finance_row_key×amount_field) уникален. (ПОСЛЕ publish)
SELECT COUNT(*) long_rows,                                                              -- 240292
  COUNT(DISTINCT FORMAT('%t|%t',finance_row_key,amount_field)) dk                       -- == long_rows
FROM `wb_mart.V_WB_FINANCE_AMOUNTS_LONG`;

-- 5) MAPPED: unknown=0; формула cost_amount_positive точна во ВСЕХ ТРЁХ режимах (fix #3). (ПОСЛЕ publish)
SELECT
  COUNTIF(cost_category IS NULL) unknown_rows,                                          -- 0
  COUNTIF(economic_direction='COST' AND cost_amount_positive<>ABS(source_signed_amount)) cost_bad,      -- 0
  COUNTIF(economic_direction='CREDIT' AND cost_amount_positive<>-ABS(source_signed_amount)) credit_bad, -- 0
  COUNTIF(economic_direction='ADJUSTMENT' AND cost_amount_positive<>source_signed_amount*field_normalization_sign) adj_bad, -- 0
  COUNTIF(cost_category IS NOT NULL AND cost_amount_positive IS NULL) null_cp           -- 0
FROM `wb_mart.V_WB_FINANCE_AMOUNTS_LONG_MAPPED`;

-- 6) Консервация расщепления + разбивка по категориям. (ПОСЛЕ publish)
SELECT ROUND(SUM(cost_amount_positive),2) total_cost_pos,                               -- 9 479 676.06
  ROUND(SUM(IF(is_sku_row,cost_amount_positive,0)),2) sku_costs,                        -- 4 243 822.66
  ROUND(SUM(IF(NOT is_sku_row,cost_amount_positive,0)),2) account_costs                 -- 5 235 853.40
FROM `wb_mart.V_WB_FINANCE_AMOUNTS_LONG_MAPPED`;
