-- ============================================================================
-- QA (read-only) — WB HISTORICAL H0: исторический канонический P&L магазина и единое представление.
-- Каждая проверка — один SELECT (check_name, status, detail); блоки «-- @check <ID>» исполняются по одному.
-- status: PASS / WARN / FAIL. Ничего не создаёт и не меняет. Док: docs/finance/WB_HISTORICAL_H0_2026-10-11.md.
-- ============================================================================

-- @check SINGLE_AUTHORITY
-- Одна власть на месяц: каждый месяц ровно один раз, история — до 2026-08, Phase C — с 2026-08.
WITH p AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_STORE_PNL_PRESENTATION`),
k AS (SELECT month, COUNT(*) n, ANY_VALUE(authority) authority, ANY_VALUE(service_month) sm FROM p GROUP BY 1)
SELECT 'SINGLE_AUTHORITY' check_name,
  IF(COUNTIF(n != 1) = 0 AND COUNTIF(authority != IF(sm < DATE '2026-08-01', 'HISTORICAL_CANONICAL', 'CURRENT_PHASE_C')) = 0, 'PASS', 'FAIL') status,
  STRING_AGG(CONCAT(month, ' ', authority, IF(n != 1, CONCAT(' ×', CAST(n AS STRING)), '')), '; ' ORDER BY month) detail
FROM k;

-- @check OVERRIDE_INTEGRITY
-- Override меняет только класс и месяц услуги: каждый ключ ровно один раз в журнале, сумма — из журнала: 7 авансов/возвратов
-- (нетто 4 831,20) + подписанный возврат −2 928 = 1 903,20 — ровно чистый расход «Баллов за отзывы».
-- Phase C читает журнал БЕЗ override: и строка, и месяц услуги override обязаны лежать до 2026-08, иначе сумма
-- попала бы в Phase C по исходному месяцу и в историю по override — дважды.
WITH o AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_FINANCE_HISTORICAL_OVERRIDES`),
l AS (SELECT finance_row_key, COUNT(*) n, SUM(amount_rub) rub, MAX(booking_date) booking_date, MAX(service_month) base_service_month
      FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_STORE_ACCOUNT_LEDGER` GROUP BY 1)
SELECT 'OVERRIDE_INTEGRITY' check_name,
  IF(COUNT(*) = 8 AND COUNTIF(IFNULL(l.n, 0) != 1) = 0 AND ABS(SUM(l.rub) - 1903.20) < 0.005
     AND COUNT(DISTINCT o.finance_row_key) = COUNT(*)
     AND COUNTIF(o.service_month >= DATE '2026-08-01' OR l.base_service_month >= DATE '2026-08-01') = 0, 'PASS', 'FAIL') status,
  CONCAT('override ', CAST(COUNT(*) AS STRING), ', без строки журнала или дубль: ', CAST(COUNTIF(IFNULL(l.n, 0) != 1) AS STRING),
         ', сумма журнала по ключам ', FORMAT('%.2f', SUM(l.rub)),
         ', месяцы Phase C: ', CAST(COUNTIF(o.service_month >= DATE '2026-08-01' OR l.base_service_month >= DATE '2026-08-01') AS STRING)) detail
FROM o LEFT JOIN l USING (finance_row_key);

-- @check HIST_CONSERVATION
-- Сохранение денег: прибыль из колонок = прибыль из первичных величин (финотчёт, биллинг, хранение, COGS, налог).
WITH h AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_STORE_PNL_HISTORICAL`)
SELECT 'HIST_CONSERVATION' check_name,
  IF(MAX(ABS(double_count_residual_rub)) < 0.01, 'PASS', 'FAIL') status,
  STRING_AGG(CONCAT(month, ': ', FORMAT('%.4f', double_count_residual_rub)), '; ' ORDER BY month) detail
FROM h;

-- @check FINANCE_COVERAGE_ALL_HISTORY
-- Вся история: деньги вне PENDING прочитаны (0); PENDING — только «Сумма удержанная за начисленные баллы…» 2025-08/09.
WITH c AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_STORE_FINANCE_COVERAGE`)
SELECT 'FINANCE_COVERAGE_ALL_HISTORY' check_name,
  IF(SUM(IF(treatment IN ('PENDING_CLASSIFICATION', 'NEW_FINANCE_OPERATION'), 0, unconsumed_abs_rub)) = 0
     AND COUNTIF(treatment = 'NEW_FINANCE_OPERATION') = 0
     AND COUNTIF(treatment = 'PENDING_CLASSIFICATION' AND NOT (supplier_oper_name = 'Сумма удержанная за начисленные баллы программы лояльности'
                 AND booking_date BETWEEN DATE '2025-08-01' AND DATE '2025-09-30')) = 0, 'PASS', 'FAIL') status,
  CONCAT('непрочитано вне PENDING: ', FORMAT('%.2f', SUM(IF(treatment IN ('PENDING_CLASSIFICATION', 'NEW_FINANCE_OPERATION'), 0, unconsumed_abs_rub))),
         '; PENDING строк: ', CAST(COUNTIF(treatment = 'PENDING_CLASSIFICATION') AS STRING),
         ', их деньги: ', FORMAT('%.2f', SUM(IF(treatment = 'PENDING_CLASSIFICATION', unconsumed_abs_rub, 0)))) detail
FROM c;

-- @check LEGACY_MEMO_GUARD
-- Р2: каждая MEMO-строка возмещения имеет основание; ПВЗ LEGACY — только при нулевом денежном эффекте.
WITH c AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_STORE_FINANCE_COVERAGE` WHERE supplier_oper_name LIKE 'Возмещение%')
SELECT 'LEGACY_MEMO_GUARD' check_name,
  IF(COUNTIF(treatment = 'MEMO_NON_PNL' AND memo_basis IS NULL) = 0
     AND COUNTIF(supplier_oper_name = 'Возмещение за выдачу и возврат товаров на ПВЗ' AND memo_schema = 'LEGACY_SNAKE'
                 AND treatment = 'MEMO_NON_PNL' AND memo_basis != 'LEGACY_ZERO_CASH') = 0, 'PASS', 'FAIL') status,
  STRING_AGG(DISTINCT CONCAT(memo_schema, '/', IFNULL(memo_basis, 'NONE'), '/', treatment), '; ') detail
FROM c;

-- @check COGS_COVERAGE
-- Каноническая себестоимость покрывает каждую проданную единицу исторических месяцев.
WITH h AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_STORE_PNL_HISTORICAL`)
SELECT 'COGS_COVERAGE' check_name,
  IF(SUM(cogs_missing_qty) = 0, 'PASS', 'FAIL') status,
  STRING_AGG(CONCAT(month, ': без COGS ', CAST(cogs_missing_qty AS STRING), ' ед.'), '; ' ORDER BY month) detail
FROM h;

-- @check HIST_INPUTS_COMPLETE
-- Связки когорты не размножают строки (итоги до и после связок равны) и снимок листа покрывает историю (внешняя
-- реклама не читается как 0 из снимка, начатого с 2026-08).
WITH h AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_STORE_PNL_HISTORICAL`)
SELECT 'HIST_INPUTS_COMPLETE' check_name,
  IF(MAX(cohort_join_gap) <= 0.005 AND LOGICAL_AND(external_marketing_covered), 'PASS', 'FAIL') status,
  STRING_AGG(CONCAT(month, ': связки ', FORMAT('%.4f', cohort_join_gap), ', снимок ', IF(external_marketing_covered, 'покрывает', 'НЕ покрывает'),
    ', внешняя реклама ', FORMAT('%.2f', external_marketing_rub)), '; ' ORDER BY month) detail
FROM h;

-- @check TAX_NOT_DOUBLE_COUNTED
-- Налог 2 % — расшифровка внутри вклада: прибыль складывается БЕЗ колонки налога; история — ровно 2 % выручки.
WITH p AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_STORE_PNL_PRESENTATION`)
SELECT 'TAX_NOT_DOUBLE_COUNTED' check_name,
  IF(MAX(ABS(sku_contribution_rub + reconciliation_adjustments_rub + mature_cohort_adjustment_rub - marketplace_costs_rub
             + marketplace_income_rub - management_net_store_profit_rub)) < 0.01
     AND MAX(IF(authority = 'HISTORICAL_CANONICAL', ABS(management_tax_rub - 0.02 * revenue_rub), 0)) < 0.01
     AND COUNTIF(management_tax_rub IS NULL) = 0, 'PASS', 'FAIL') status,
  STRING_AGG(CONCAT(month, ': налог ', FORMAT('%.2f', management_tax_rub), ', прибыль ', FORMAT('%.2f', management_net_store_profit_rub)), '; ' ORDER BY month) detail
FROM p;

-- @check ADS_RECONCILES
-- История: поправка рекламы = атрибуция − биллинг WB по дате услуги (V_DASH_FINANCE_CORRECTED_DAILY).
WITH h AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_STORE_PNL_HISTORICAL`),
a AS (SELECT DATE_TRUNC(day, MONTH) m, SUM(ad_spend_attributed_rub) attr, SUM(ad_spend_financial_rub) bill
      FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_FINANCE_CORRECTED_DAILY` GROUP BY 1)
SELECT 'ADS_RECONCILES' check_name,
  IF(MAX(ABS(h.ads_adjustment_rub - (IFNULL(a.attr, 0) - IFNULL(a.bill, 0)))) < 0.01 AND MIN(h.ads_final_days = h.ads_days), 'PASS', 'FAIL') status,
  STRING_AGG(CONCAT(h.month, ': ', FORMAT('%.2f', IFNULL(a.attr, 0)), ' − ', FORMAT('%.2f', IFNULL(a.bill, 0)), ' = ', FORMAT('%.2f', h.ads_adjustment_rub)), '; ' ORDER BY h.month) detail
FROM h LEFT JOIN a ON a.m = h.service_month;

-- @check STORAGE_RECONCILES
-- История: поправка хранения = −(«Хранение» + «Коррекция хранения» финотчёта за месяц).
WITH h AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_STORE_PNL_HISTORICAL`),
s AS (SELECT DATE_TRUNC(_rr_date, MONTH) m, SUM(IFNULL(SAFE_CAST(storage_fee AS NUMERIC), 0)) fin
      FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FINANCE_CANONICAL`
      WHERE supplier_oper_name IN ('Хранение', 'Коррекция хранения') GROUP BY 1)
SELECT 'STORAGE_RECONCILES' check_name,
  IF(MAX(ABS(h.storage_adjustment_rub + IFNULL(s.fin, 0))) < 0.01, 'PASS', 'FAIL') status,
  STRING_AGG(CONCAT(h.month, ': ', FORMAT('%.2f', h.storage_adjustment_rub)), '; ' ORDER BY h.month) detail
FROM h LEFT JOIN s ON s.m = h.service_month;

-- @check HIST_STATE
-- Исторические месяцы закрыты (после правки дат наборов); иначе — раскрытие причины.
WITH h AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_STORE_PNL_HISTORICAL`)
SELECT 'HIST_STATE' check_name,
  IF(COUNTIF(financial_state != 'FINANCIAL_COMPLETE') = 0, 'PASS', 'WARN') status,
  STRING_AGG(CONCAT(month, ' ', financial_state, ' (без COGS ', CAST(cogs_missing_qty AS STRING), ', неизвестно ', FORMAT('%.2f', unknown_rub),
    ', PENDING ', CAST(pending_rows AS STRING), ')'), '; ' ORDER BY month) detail
FROM h;
