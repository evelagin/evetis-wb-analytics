-- ============================================================================
-- Stage 4A · PR2 — гейты приёмки D1–D18 для sql/dash/dashboard_contract_v2.sql
-- Дата: 2026-08-20.  Якорь измерений: production BigQuery 20.08.2026.
--
-- 🔴 КРИТЕРИЙ ПРИЁМКИ — СООТНОШЕНИЕ, А НЕ ЗАМОРОЖЕННАЯ КОНСТАНТА.
--    Инварианты (обязаны держаться в любой день): D1, D2, D3, D4, D6, D8, D9, D10, D14, D18.
--    С привязкой к якорю (сверять со значением ТОГО дня): D5, D7, D11, D12, D13, D15, D16.
--
-- Каждый запрос возвращает одну строку с колонкой `verdict` = 'PASS' | 'FAIL'.
-- ============================================================================

-- D1. Грейн KPI: строк ровно столько, сколько суток в календаре PR1. ИНВАРИАНТ.
SELECT 'D1' AS gate, COUNT(*) AS rows_,
  (SELECT COUNT(*) FROM `wb_mart.V_DASH_COVERAGE_DAILY`) AS coverage_rows,
  IF(COUNT(*) = (SELECT COUNT(*) FROM `wb_mart.V_DASH_COVERAGE_DAILY`)
     AND COUNT(*) = COUNT(DISTINCT day), 'PASS', 'FAIL') AS verdict
FROM `wb_mart.V_DASH_KPI_DAILY`;

-- D2. Грейн SKU: строгий pass-through витрины, ключ (day, nm_id) уникален. ИНВАРИАНТ.
SELECT 'D2' AS gate, COUNT(*) AS rows_,
  (SELECT COUNT(*) FROM `wb_mart.MART_SKU_DAILY`) AS mart_rows,
  COUNT(DISTINCT FORMAT('%t|%t', day, nm_id)) AS unique_keys,
  IF(COUNT(*) = (SELECT COUNT(*) FROM `wb_mart.MART_SKU_DAILY`)
     AND COUNT(*) = COUNT(DISTINCT FORMAT('%t|%t', day, nm_id)), 'PASS', 'FAIL') AS verdict
FROM `wb_mart.V_DASH_SKU_DAILY`;

-- D3. Fan-out справочника отсутствует; сирот нет. ИНВАРИАНТ.
SELECT 'D3' AS gate, MAX(ref_rows_for_nm_id) AS max_ref_rows, COUNTIF(is_orphan) AS orphans,
  COUNTIF(product_name_short IS NULL) AS rows_without_name,
  IF(MAX(ref_rows_for_nm_id) <= 1 AND COUNTIF(is_orphan) = 0, 'PASS', 'FAIL') AS verdict
FROM `wb_mart.V_DASH_SKU_DAILY`;

-- D4. SKU-часть KPI = агрегат SKU-вью по суткам, до копейки. ИНВАРИАНТ.
WITH k AS (
  SELECT ROUND(SUM(orders_revenue_rub),2) a, ROUND(SUM(sales_revenue_seller_base_rub),2) b,
         ROUND(SUM(ad_spend_attributed_rub),2) c, ROUND(SUM(marketplace_fee_rub),2) d,
         ROUND(SUM(net_settlement_rub),2) e, ROUND(SUM(contribution_pre_cogs_rub),2) f
  FROM `wb_mart.V_DASH_KPI_DAILY`),
s AS (
  SELECT ROUND(SUM(orders_revenue_rub),2) a, ROUND(SUM(sales_revenue_seller_base_rub),2) b,
         ROUND(SUM(ad_spend_attributed_rub),2) c, ROUND(SUM(marketplace_fee_rub),2) d,
         ROUND(SUM(net_settlement_rub),2) e, ROUND(SUM(contribution_pre_cogs_rub),2) f
  FROM `wb_mart.V_DASH_SKU_DAILY`)
SELECT 'D4' AS gate, k.a, s.a, k.f, s.f,
  IF(k.a = s.a AND k.b = s.b AND k.c = s.c AND k.d = s.d AND k.e = s.e AND k.f = s.f, 'PASS', 'FAIL') AS verdict
FROM k, s;

-- D5. Сбор WB и выплата = FACT_FINANCE (matched SKU). ЯКОРЬ: 7 954 656,56 / 18 870 292,80.
SELECT 'D5' AS gate,
  (SELECT ROUND(SUM(marketplace_fee_rub),2) FROM `wb_mart.V_DASH_KPI_DAILY`) AS kpi_fee,
  (SELECT ROUND(SUM(marketplace_fee_gap_rub),2) FROM `wb_mart.FACT_FINANCE`
     WHERE nm_id > 0 AND sku_match_status = 'matched') AS fact_fee,
  (SELECT ROUND(SUM(net_settlement_rub),2) FROM `wb_mart.V_DASH_KPI_DAILY`) AS kpi_payout,
  (SELECT ROUND(SUM(finance_for_pay_accounting),2) FROM `wb_mart.FACT_FINANCE`
     WHERE nm_id > 0 AND sku_match_status = 'matched') AS fact_payout,
  IF((SELECT ROUND(SUM(marketplace_fee_rub),2) FROM `wb_mart.V_DASH_KPI_DAILY`)
     = (SELECT ROUND(SUM(marketplace_fee_gap_rub),2) FROM `wb_mart.FACT_FINANCE`
        WHERE nm_id > 0 AND sku_match_status = 'matched')
   AND (SELECT ROUND(SUM(net_settlement_rub),2) FROM `wb_mart.V_DASH_KPI_DAILY`)
     = (SELECT ROUND(SUM(finance_for_pay_accounting),2) FROM `wb_mart.FACT_FINANCE`
        WHERE nm_id > 0 AND sku_match_status = 'matched'), 'PASS', 'FAIL') AS verdict;

-- D6. 🔴 СУММА ЧАСТЕЙ = ЦЕЛОЕ. Четыре взаимоисключающие группы расходов покрывают
--     V_WB_FINANCE_AMOUNTS_LONG_MAPPED без остатка. Гейт ловит любую категорию,
--     оказавшуюся не там, где её ждали, — включая ещё не существующие. ИНВАРИАНТ.
--     ЯКОРЬ: 4 105 620,55 + 240 900,49 + 14 711,02 + 5 298 874,47 = 9 660 106,53.
WITH k AS (
  SELECT
    ROUND(SUM(wb_reward_rub + logistics_rub), 2)                                        AS mart_carried,
    ROUND(SUM(acquiring_sku_rub + loyalty_sku_rub + penalty_sku_rub
              + reimbursement_sku_rub + other_sku_rub), 2)                              AS sku_not_in_mart,
    ROUND(SUM(sku_costs_outside_universe_rub), 2)                                       AS outside_universe,
    ROUND(SUM(account_level_total_rub), 2)                                              AS account_level,
    ROUND(SUM(finance_long_total_rub), 2)                                               AS long_total
  FROM `wb_mart.V_DASH_KPI_DAILY`)
SELECT 'D6' AS gate, mart_carried, sku_not_in_mart, outside_universe, account_level, long_total,
  ROUND(mart_carried + sku_not_in_mart + outside_universe + account_level - long_total, 2) AS parts_minus_total,
  IF(ROUND(mart_carried + sku_not_in_mart + outside_universe + account_level - long_total, 2) = 0,
     'PASS', 'FAIL') AS verdict
FROM k;

-- D7. 🔴 КОНТРИБУЦИЯ НАСЛЕДУЕТ ПОКРЫТИЕ. Главный гейт PR2.
--     ЯКОРЬ: 1 312 034,95 ₽ на 128 покрытых сутках; вне покрытия строго NULL,
--     а не −9 089 133,91 ₽, которые дала бы витрина без гейта.
SELECT 'D7' AS gate,
  ROUND(SUM(contribution_pre_cogs_rub), 2)                       AS contribution_covered,
  COUNTIF(contribution_covered)                                  AS covered_days,
  COUNTIF(NOT contribution_covered AND contribution_pre_cogs_rub IS NOT NULL) AS leak,
  (SELECT ROUND(SUM(hybrid_day_contribution_pre_cogs), 2) FROM `wb_mart.MART_SKU_DAILY`) AS mart_ungated,
  IF(COUNTIF(NOT contribution_covered AND contribution_pre_cogs_rub IS NOT NULL) = 0
     AND SUM(contribution_pre_cogs_rub) > 0, 'PASS', 'FAIL') AS verdict
FROM `wb_mart.V_DASH_KPI_DAILY`;

-- D8. 🔴 NULL PROPAGATION. Ни одной метрики вне своего покрытия. ИНВАРИАНТ.
WITH k AS (
  SELECT
    COUNTIF(NOT orders_covered AND (orders_qty IS NOT NULL OR orders_revenue_rub IS NOT NULL
            OR canceled_qty IS NOT NULL OR canceled_rub IS NOT NULL))                   AS leak_orders,
    COUNTIF(NOT sales_covered AND (buyouts_qty IS NOT NULL OR sales_revenue_seller_base_rub IS NOT NULL
            OR sales_revenue_buyer_paid_rub IS NOT NULL OR returns_qty IS NOT NULL))    AS leak_sales,
    COUNTIF(NOT ads_covered AND (ad_spend_attributed_rub IS NOT NULL OR views IS NOT NULL
            OR clicks IS NOT NULL OR ads_revenue_raw_rub IS NOT NULL))                  AS leak_ads,
    COUNTIF(NOT finance_covered AND (marketplace_fee_rub IS NOT NULL OR net_settlement_rub IS NOT NULL
            OR storage_rub IS NOT NULL OR deduction_rub IS NOT NULL
            OR sales_revenue_settled_rub IS NOT NULL))                                  AS leak_finance,
    COUNTIF(NOT contribution_covered AND contribution_pre_cogs_rub IS NOT NULL)         AS leak_contribution
  FROM `wb_mart.V_DASH_KPI_DAILY`),
s AS (
  SELECT
    COUNTIF(NOT orders_covered AND orders_qty IS NOT NULL)                              AS leak_orders,
    COUNTIF(NOT sales_covered AND sales_revenue_seller_base_rub IS NOT NULL)            AS leak_sales,
    COUNTIF(NOT ads_covered AND ad_spend_attributed_rub IS NOT NULL)                    AS leak_ads,
    COUNTIF(NOT finance_covered AND marketplace_fee_rub IS NOT NULL)                    AS leak_finance,
    COUNTIF(NOT contribution_covered AND contribution_pre_cogs_rub IS NOT NULL)         AS leak_contribution
  FROM `wb_mart.V_DASH_SKU_DAILY`)
SELECT 'D8' AS gate,
  k.leak_orders + k.leak_sales + k.leak_ads + k.leak_finance + k.leak_contribution AS kpi_leaks,
  s.leak_orders + s.leak_sales + s.leak_ads + s.leak_finance + s.leak_contribution AS sku_leaks,
  IF(k.leak_orders + k.leak_sales + k.leak_ads + k.leak_finance + k.leak_contribution = 0
     AND s.leak_orders + s.leak_sales + s.leak_ads + s.leak_finance + s.leak_contribution = 0,
     'PASS', 'FAIL') AS verdict
FROM k, s;

-- D9. Статически: ни одного IFNULL(...,0) / COALESCE(...,0) для метрик. ИНВАРИАНТ.
--     Единственное разрешённое исключение — IFNULL(ref_rows_for_nm_id, 0): это
--     СЧЁТЧИК СТРОК СПРАВОЧНИКА, а не метрика; ноль там означает «строк нет».
SELECT 'D9' AS gate, table_name,
  ARRAY_LENGTH(REGEXP_EXTRACT_ALL(view_definition, r"(?i)IFNULL\s*\([^)]*,\s*0\s*\)"))    AS ifnull_zero,
  ARRAY_LENGTH(REGEXP_EXTRACT_ALL(view_definition, r"(?i)COALESCE\s*\([^)]*,\s*0\s*\)"))  AS coalesce_zero,
  ARRAY_LENGTH(REGEXP_EXTRACT_ALL(view_definition, r"IFNULL\(r\.ref_rows_for_nm_id, 0\)")) AS allowed_exception,
  IF(ARRAY_LENGTH(REGEXP_EXTRACT_ALL(view_definition, r"(?i)COALESCE\s*\([^)]*,\s*0\s*\)")) = 0
     AND ARRAY_LENGTH(REGEXP_EXTRACT_ALL(view_definition, r"(?i)IFNULL\s*\([^)]*,\s*0\s*\)"))
       = ARRAY_LENGTH(REGEXP_EXTRACT_ALL(view_definition, r"IFNULL\(r\.ref_rows_for_nm_id, 0\)")),
     'PASS', 'FAIL') AS verdict
FROM `wb_mart.INFORMATION_SCHEMA.VIEWS`
WHERE table_name IN ('V_DASH_KPI_DAILY', 'V_DASH_SKU_DAILY') ORDER BY table_name;

-- D10. 🔴 НИ ОДНОЙ RATIO-КОЛОНКИ В СХЕМЕ. Закрытый список запрещённых имён. ИНВАРИАНТ.
SELECT 'D10' AS gate, table_name, COUNT(*) AS ratio_columns_found,
  STRING_AGG(column_name, ', ') AS names,
  IF(COUNT(*) = 0, 'PASS', 'FAIL') AS verdict
FROM `wb_mart.INFORMATION_SCHEMA.COLUMNS`
WHERE table_name IN ('V_DASH_KPI_DAILY', 'V_DASH_SKU_DAILY')
  AND (REGEXP_CONTAINS(column_name, r"(?i)(^|_)(ctr|cpc|cpm|cpo|roas|acos|drr)(_|$)")
       OR REGEXP_CONTAINS(column_name, r"(?i)(_rate|_ratio|_pct|_percent|_share|_avg|avg_)")
       OR REGEXP_CONTAINS(column_name, r"(?i)_(7d|14d|28d|30d|90d)$"))
GROUP BY table_name ORDER BY table_name;

-- D11. Доказательство, что AVG(row_ratio) ≠ ratio-of-sums. Гейт НЕ прячет расхождение,
--      а предъявляет его. ЯКОРЬ: 15,37 % против 15,77 % на покрытом окне.
SELECT 'D11' AS gate,
  ROUND(SAFE_DIVIDE(SUM(ad_spend_attributed_rub), SUM(sales_revenue_seller_base_rub)), 4) AS drr_ratio_of_sums,
  ROUND(AVG(SAFE_DIVIDE(ad_spend_attributed_rub, sales_revenue_seller_base_rub)), 4)      AS drr_avg_of_row_ratios_FORBIDDEN,
  IF(ROUND(SAFE_DIVIDE(SUM(ad_spend_attributed_rub), SUM(sales_revenue_seller_base_rub)), 4)
     <> ROUND(AVG(SAFE_DIVIDE(ad_spend_attributed_rub, sales_revenue_seller_base_rub)), 4),
     'PASS', 'FAIL') AS verdict
FROM `wb_mart.V_DASH_SKU_DAILY`
WHERE contribution_covered;

-- D12. PROVISIONAL остаётся наблюдаемым и агрегируемым по произвольному диапазону.
--      ЯКОРЬ: 13–19.08 → 2 суток, закрытый период 13.04–12.08 → 0.
SELECT 'D12' AS gate,
  SUM(IF(day BETWEEN DATE '2026-08-13' AND DATE '2026-08-19', contribution_provisional_days, 0)) AS last7,
  SUM(IF(day BETWEEN DATE '2026-04-13' AND DATE '2026-08-12', contribution_provisional_days, 0)) AS closed_period,
  COUNTIF(contains_provisional_finance AND finance_is_final) AS contradiction,
  IF(SUM(IF(day BETWEEN DATE '2026-08-13' AND DATE '2026-08-19', contribution_provisional_days, 0)) > 0
     AND SUM(IF(day BETWEEN DATE '2026-04-13' AND DATE '2026-08-12', contribution_provisional_days, 0)) = 0
     AND COUNTIF(contains_provisional_finance AND finance_is_final) = 0, 'PASS', 'FAIL') AS verdict
FROM `wb_mart.V_DASH_KPI_DAILY`;

-- D13. Реализация из финансов СО ЗНАКОМ (OPEN-3) и отдельная корректировка (OPEN-4).
--      ЯКОРЬ: signed 26 789 919,64 · MART-style unsigned 26 803 799,64 · delta 13 880,00
--      = 2 × 6 940,00 (11 строк «Возврат»). Корректировка 1 834,88 на 8 строках — отдельно.
WITH f AS (
  SELECT
    ROUND(SUM(CASE supplier_oper_name
                WHEN 'Продажа' THEN  (marketplace_fee_gap_rub + finance_for_pay_accounting)
                WHEN 'Возврат' THEN -(marketplace_fee_gap_rub + finance_for_pay_accounting) END), 2) AS signed_,
    ROUND(SUM(IF(supplier_oper_name IN ('Продажа', 'Возврат'),
                 marketplace_fee_gap_rub + finance_for_pay_accounting, NULL)), 2)                    AS unsigned_,
    ROUND(SUM(IF(supplier_oper_name = 'Возврат',
                 marketplace_fee_gap_rub + finance_for_pay_accounting, 0)), 2)                       AS returns_rpw
  FROM `wb_mart.FACT_FINANCE`
  WHERE supplier_oper_name IN ('Продажа', 'Возврат'))
SELECT 'D13' AS gate,
  (SELECT ROUND(SUM(sales_revenue_settled_rub), 2) FROM `wb_mart.V_DASH_KPI_DAILY`) AS view_settled,
  f.signed_, f.unsigned_, ROUND(f.unsigned_ - f.signed_, 2) AS mart_style_bias, f.returns_rpw,
  (SELECT ROUND(SUM(sales_adjustment_rub), 2) FROM `wb_mart.V_DASH_KPI_DAILY`) AS adjustment_rub,
  IF((SELECT ROUND(SUM(sales_revenue_settled_rub), 2) FROM `wb_mart.V_DASH_KPI_DAILY`) = f.signed_
     AND ROUND(f.unsigned_ - f.signed_, 2) = ROUND(2 * f.returns_rpw, 2), 'PASS', 'FAIL') AS verdict
FROM f;

-- D14. Свежесть по контракту: карта не пуста, слои без даты названы, опечаток нет. ИНВАРИАНТ.
SELECT 'D14' AS gate, contract_code, layers_used_count, layers_used_list,
  data_as_of_min_used, worst_status_used,
  layers_used_without_date_count, layers_used_without_date_list, layers_declared_but_absent_count,
  IF(layers_used_count > 0 AND layers_declared_but_absent_count = 0
     AND worst_status_used IS NOT NULL
     AND (layers_used_without_date_count = 0 OR layers_used_without_date_list IS NOT NULL),
     'PASS', 'FAIL') AS verdict
FROM `wb_mart.V_DASH_FRESHNESS_BY_CONTRACT` ORDER BY contract_code;

-- D15. Contract-свежесть НЕ подменяет системную. Оба значения существуют и различны
--      по смыслу. ЯКОРЬ: системный минимум 2026-08-17 (его задаёт ads_costs, не входящий
--      ни в один контракт PR2), contract-минимум 2026-08-18.
SELECT 'D15' AS gate,
  (SELECT CAST(data_as_of_min AS STRING) FROM `wb_mart.V_DASH_FRESHNESS_HEADER`) AS system_min,
  (SELECT worst_status FROM `wb_mart.V_DASH_FRESHNESS_HEADER`)                   AS system_worst,
  (SELECT CAST(data_as_of_min_used AS STRING) FROM `wb_mart.V_DASH_FRESHNESS_BY_CONTRACT`
     WHERE contract_code = 'KPI_DAILY')                                          AS kpi_min_used,
  (SELECT worst_status_used FROM `wb_mart.V_DASH_FRESHNESS_BY_CONTRACT`
     WHERE contract_code = 'KPI_DAILY')                                          AS kpi_worst_used,
  IF((SELECT data_as_of_min FROM `wb_mart.V_DASH_FRESHNESS_HEADER`)
     <= (SELECT data_as_of_min_used FROM `wb_mart.V_DASH_FRESHNESS_BY_CONTRACT`
         WHERE contract_code = 'KPI_DAILY'), 'PASS', 'FAIL') AS verdict;

-- D16. Baseline «до = после»: PR2 не меняет ни одного существующего объекта,
--      включая оба объекта PR1. ЯКОРЬ 20.08 — см. значения ниже.
SELECT 'D16' AS gate,
  (SELECT COUNT(*) FROM `wb_mart.MART_SKU_DAILY`)                                AS mart_rows,
  (SELECT ROUND(SUM(ad_spend), 2) FROM `wb_mart.MART_SKU_DAILY`)                 AS mart_ad_spend,
  (SELECT COUNT(*) FROM `wb_mart.V_ADS_SCREEN_SKU`)                              AS screen_sku,
  (SELECT COUNT(*) FROM `wb_mart.V_ADS_SCREEN_QUERY`)                            AS screen_query,
  (SELECT COUNT(*) FROM `wb_mart.FACT_ADS_COSTS_DAILY`)                          AS fact_costs_rows,
  (SELECT COUNT(*) FROM `wb_mart.V_DATA_FRESHNESS`)                              AS freshness_rows,
  (SELECT COUNT(*) FROM `wb_mart.V_DASH_COVERAGE_DAILY`)                         AS pr1_coverage_rows,
  (SELECT COUNT(*) FROM `wb_mart.V_DASH_FRESHNESS_HEADER`)                       AS pr1_header_rows,
  'СВЕРИТЬ С BASELINE ДО ВЫКАТА' AS verdict;

-- D17. K9: текст файла в main ↔ view_definition в BigQuery.
--      🔴 ПРАВИЛО ИЗ PR1: делить файл ТОЛЬКО по `CREATE OR REPLACE VIEW` (в файле есть
--      ';' внутри комментариев), а терминатор ';' снимать ПОСЛЕ нормализации —
--      иначе он остаётся приклеенным к последней содержательной строке первого
--      стейтмента и даёт ложное расхождение ровно в один символ.
SELECT 'D17' AS gate, table_name, LENGTH(view_definition) AS def_chars,
  TO_HEX(SHA256(ARRAY_TO_STRING(
    ARRAY(SELECT RTRIM(line) FROM UNNEST(SPLIT(view_definition, '\n')) AS line
          WHERE TRIM(line) <> '' AND NOT STARTS_WITH(TRIM(line), '--')), '\n'))) AS sha256_norm
FROM `wb_mart.INFORMATION_SCHEMA.VIEWS`
WHERE table_name IN ('V_DASH_KPI_DAILY', 'V_DASH_SKU_DAILY', 'V_DASH_FRESHNESS_BY_CONTRACT')
ORDER BY table_name;

-- D18. Stage 3B.1 не задет: ни одной ссылки на биллинговый контур. ИНВАРИАНТ.
SELECT 'D18' AS gate, table_name,
  ARRAY_LENGTH(REGEXP_EXTRACT_ALL(view_definition, r"V_ADV_COSTS"))          AS adv_costs_refs,
  ARRAY_LENGTH(REGEXP_EXTRACT_ALL(view_definition, r"FACT_ADS_COSTS_DAILY")) AS fact_costs_refs,
  ARRAY_LENGTH(REGEXP_EXTRACT_ALL(view_definition, r"(?i)billed"))           AS billed_refs,
  ARRAY_LENGTH(REGEXP_EXTRACT_ALL(view_definition, r"V_ADS_SCREEN"))         AS screen_refs,
  IF(ARRAY_LENGTH(REGEXP_EXTRACT_ALL(view_definition, r"V_ADV_COSTS")) = 0
     AND ARRAY_LENGTH(REGEXP_EXTRACT_ALL(view_definition, r"FACT_ADS_COSTS_DAILY")) = 0
     AND ARRAY_LENGTH(REGEXP_EXTRACT_ALL(view_definition, r"(?i)billed")) = 0
     AND ARRAY_LENGTH(REGEXP_EXTRACT_ALL(view_definition, r"V_ADS_SCREEN")) = 0, 'PASS', 'FAIL') AS verdict
FROM `wb_mart.INFORMATION_SCHEMA.VIEWS`
WHERE table_name IN ('V_DASH_KPI_DAILY', 'V_DASH_SKU_DAILY', 'V_DASH_FRESHNESS_BY_CONTRACT')
ORDER BY table_name;
