-- ============================================================================
-- QA (read-only) — P&L МАГАЗИНА WB (Phase C, C4, OWNER ACK 2026-10-09).
-- Каждая проверка — один SELECT (check_name, status, detail); блоки разделены «-- @check <ID>» и исполняются
-- ПО ОДНОМУ: единый запрос по всем вью упирается в лимит планировщика BigQuery («query is too complex»).
-- status: PASS / WARN / FAIL. FAIL блокирует запись вкладки «WB Магазин P&L»; WARN раскрывается владельцу.
-- Ничего не создаёт и не меняет. Док: docs/finance/WB_STORE_PNL_PHASE_C_2026-10-09.md.
-- ============================================================================

-- @check NEW_FINANCE_OPERATION
-- 1. Карта операций: операция финотчёта вне утверждённой карты = новая операция WB.
WITH newops AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_FINANCE_NEW_OPERATIONS`)
SELECT 'NEW_FINANCE_OPERATION' check_name,
  IF(COUNTIF(treatment = 'NEW_FINANCE_OPERATION') = 0, 'PASS', 'FAIL') status,
  IFNULL(STRING_AGG(IF(treatment = 'NEW_FINANCE_OPERATION', CONCAT(supplier_oper_name, ' (', CAST(rows_n AS STRING), ' строк, до ', CAST(last_seen AS STRING), ')'), NULL), '; '), 'нет') detail
FROM newops;

-- @check PENDING_CLASSIFICATION_IN_PNL_MONTHS
-- Неразобранные и новые операции в окне месяца (месяц + 20 суток проводки).
WITH pnl AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_STORE_PNL_MONTHLY`),
newops AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_FINANCE_NEW_OPERATIONS`)
SELECT 'PENDING_CLASSIFICATION_IN_PNL_MONTHS' check_name,
  IF(SUM(pending_rows) = 0, 'PASS', 'FAIL') status,
  CONCAT('строк PENDING/NEW в окнах месяцев P&L: ', CAST(SUM(pending_rows) AS STRING),
         '; за всю историю — ', (SELECT IFNULL(STRING_AGG(CONCAT(supplier_oper_name, ' ', months), '; '), 'нет') FROM newops)) detail
FROM pnl;

-- @check SNAPSHOT_FRESH
-- 2. Снимок листа: свежесть и ключ.
WITH snap AS (
  SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_ops.UNITKA_SKU_COMPONENTS_DAILY`
  WHERE snapshot_id = (SELECT MAX(snapshot_id) FROM `project-fa311fc0-4d87-4781-986.wb_ops.UNITKA_SKU_COMPONENTS_DAILY`))
SELECT 'SNAPSHOT_FRESH' check_name,
  IF(DATE_DIFF(CURRENT_DATE('Europe/Moscow'), MAX(lcd), DAY) <= 3, 'PASS', 'WARN') status,
  CONCAT('snapshot ', MAX(snapshot_id), ', LCD ', CAST(MAX(lcd) AS STRING), ', строк ', CAST(COUNT(*) AS STRING)) detail
FROM snap;

-- @check SNAPSHOT_KEY_UNIQUE
WITH snap AS (
  SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_ops.UNITKA_SKU_COMPONENTS_DAILY`
  WHERE snapshot_id = (SELECT MAX(snapshot_id) FROM `project-fa311fc0-4d87-4781-986.wb_ops.UNITKA_SKU_COMPONENTS_DAILY`))
SELECT 'SNAPSHOT_KEY_UNIQUE' check_name,
  IF(COUNT(*) = COUNT(DISTINCT CONCAT(CAST(date_msk AS STRING), '|', CAST(nm_id AS STRING))), 'PASS', 'FAIL') status,
  CONCAT('строк ', CAST(COUNT(*) AS STRING), ', ключей ', CAST(COUNT(DISTINCT CONCAT(CAST(date_msk AS STRING), '|', CAST(nm_id AS STRING))) AS STRING)) detail
FROM snap;

-- @check FINANCE_COVERAGE
-- Полнота (C2): каждый рубль финотчёта в окнах месяцев P&L прочитан моделью — ни операции без потребителя,
-- ни когортной строки без srid / даты заказа / nm, ни денег в «чужом» поле.
WITH pnl AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_STORE_PNL_MONTHLY`),
cov AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_STORE_FINANCE_COVERAGE`),
bad AS (
  SELECT c.supplier_oper_name, c.consumer, COUNT(*) n, SUM(c.unconsumed_abs_rub) rub
  FROM cov c JOIN pnl p ON c.booking_date BETWEEN p.service_month AND DATE_ADD(LAST_DAY(p.service_month), INTERVAL 20 DAY)
  WHERE c.unconsumed_abs_rub > 0 GROUP BY 1, 2)
SELECT 'FINANCE_COVERAGE' check_name,
  IF(IFNULL(SUM(rub), 0) <= 0.5, 'PASS', 'FAIL') status,
  IFNULL(STRING_AGG(CONCAT(supplier_oper_name, ' [', consumer, '] ', CAST(n AS STRING), ' строк, ', FORMAT('%.2f', rub), ' ₽'), '; '), 'нет') detail
FROM bad;

-- @check DEDUCTION_SOURCE_RECONCILIATION
-- Источник счетов кабинета (V_WB_DEDUCTIONS_CLASSIFIED ← FACT_FINANCE) = удержания финотчёта (canonical) по месяцу.
WITH pnl AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_STORE_PNL_MONTHLY`)
SELECT 'DEDUCTION_SOURCE_RECONCILIATION' check_name,
  IF(MAX(deduction_source_gap_rub) <= 0.5, 'PASS', 'FAIL') status,
  STRING_AGG(CONCAT(month, ': ', FORMAT('%.2f', deduction_source_gap_rub)), '; ' ORDER BY month) detail
FROM pnl;

-- @check ORPHAN_FINANCE
-- 3. Финансы без SKU листа: деньги когорты, которых модель не видит.
WITH pnl AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_STORE_PNL_MONTHLY`)
SELECT 'ORPHAN_FINANCE' check_name,
  IF(MAX(ABS(orphan_finance_base_rub) + ABS(orphan_finance_logistics_rub)) < 0.01, 'PASS', 'FAIL') status,
  IFNULL(STRING_AGG(IF(ABS(orphan_finance_base_rub) + ABS(orphan_finance_logistics_rub) >= 0.01,
    CONCAT(month, ': база ', FORMAT('%.2f', orphan_finance_base_rub), ', логистика ', FORMAT('%.2f', orphan_finance_logistics_rub)), NULL), '; '), 'нет') detail
FROM pnl;

-- @check ACCOUNT_SERVICE_PERIOD
-- 4. Счета кабинета: месяц услуги назначен, класс известен (Р1).
WITH ledger AS (
  SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_STORE_ACCOUNT_LEDGER`
  WHERE booking_date >= DATE '2026-08-01' OR service_month >= DATE '2026-08-01')
SELECT 'ACCOUNT_SERVICE_PERIOD' check_name,
  IF(COUNTIF(service_month IS NULL) = 0 AND COUNTIF(category = 'UTILIZATION' AND service_period_basis != 'LABEL_PERIOD') = 0, 'PASS', 'FAIL') status,
  CONCAT('без месяца услуги: ', CAST(COUNTIF(service_month IS NULL) AS STRING),
         '; утилизация без периода в подписи: ', CAST(COUNTIF(category = 'UTILIZATION' AND service_period_basis != 'LABEL_PERIOD') AS STRING),
         '; мин. платёж (выведенный период): ', CAST(COUNTIF(category = 'MINIMUM_PAYMENT') AS STRING)) detail
FROM ledger;

-- @check UNKNOWN_ACCOUNT_COST
-- Неклассифицированные удержания в месяцах P&L. Месяц с ними — UNKNOWN_COST_PRESENT (это уже видно в состоянии);
-- здесь — раскрытие сумм. FAIL — только если такой месяц всё же объявлен закрытым (ловит и STATE_CONSISTENCY).
WITH ledger AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_STORE_ACCOUNT_LEDGER`),
pnl AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_STORE_PNL_MONTHLY`),
u AS (
  SELECT FORMAT_DATE('%Y-%m', l.service_month) month, COUNT(*) n, SUM(l.amount_rub) rub, ANY_VALUE(p.financial_state) state
  FROM ledger l JOIN pnl p ON p.service_month = l.service_month
  WHERE l.treatment = 'UNKNOWN' GROUP BY 1)
SELECT 'UNKNOWN_ACCOUNT_COST' check_name,
  CASE WHEN COUNTIF(state LIKE 'FINANCIAL_COMPLETE%') > 0 THEN 'FAIL' WHEN COUNT(*) > 0 THEN 'WARN' ELSE 'PASS' END status,
  IFNULL(STRING_AGG(CONCAT(month, ': ', CAST(n AS STRING), ' строк, ', FORMAT('%.2f', rub), ' ₽, ', state), '; ' ORDER BY month), 'нет') detail
FROM u;

-- @check R2_REIMBURSEMENT_OFFSET
-- 5. Р2: возмещения WB погашены вознаграждением WB до копеек, к выплате 0 → MEMO_NON_PNL.
WITH snap AS (
  SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_ops.UNITKA_SKU_COMPONENTS_DAILY`
  WHERE snapshot_id = (SELECT MAX(snapshot_id) FROM `project-fa311fc0-4d87-4781-986.wb_ops.UNITKA_SKU_COMPONENTS_DAILY`)),
reimb_rows AS (
  SELECT IFNULL(SAFE_CAST(JSON_VALUE(raw_json, '$.rebillLogisticCost') AS NUMERIC), 0)
       + IFNULL(SAFE_CAST(JSON_VALUE(raw_json, '$.ppvzReward') AS NUMERIC), 0) reimb_rub,
         IFNULL(SAFE_CAST(JSON_VALUE(raw_json, '$.vw') AS FLOAT64), 0)
       + IFNULL(SAFE_CAST(JSON_VALUE(raw_json, '$.vwNds') AS NUMERIC), 0) vw_rub,
         IFNULL(SAFE_CAST(for_pay AS NUMERIC), 0) for_pay
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FINANCE_CANONICAL`
  WHERE supplier_oper_name LIKE 'Возмещение%' AND _rr_date >= (SELECT MIN(date_msk) FROM snap))
SELECT 'R2_REIMBURSEMENT_OFFSET' check_name,
  IF(COUNTIF(ABS(reimb_rub + vw_rub) > 0.01) = 0 AND COUNTIF(for_pay != 0) = 0, 'PASS', 'FAIL') status,
  CONCAT('строк ', CAST(COUNT(*) AS STRING), ', возмещение ', FORMAT('%.2f', SUM(reimb_rub)), ', vw+vwNds ', FORMAT('%.2f', SUM(vw_rub)),
         ', строк с |Δ|>0,01: ', CAST(COUNTIF(ABS(reimb_rub + vw_rub) > 0.01) AS STRING), ', строк с for_pay≠0: ', CAST(COUNTIF(for_pay != 0) AS STRING)) detail
FROM reimb_rows;

-- @check BRIDGE_ADS
-- 6. Мосты по статьям (C2): представлено в SKU − поправка = факт площадки из независимого источника.
WITH pnl AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_STORE_PNL_MONTHLY`),
ads AS (
  SELECT DATE_TRUNC(day, MONTH) m, SUM(ad_spend_financial_rub) bill
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_FINANCE_CORRECTED_DAILY` GROUP BY 1)
SELECT 'BRIDGE_ADS' check_name,
  IF(MAX(ABS(p.ads_sku_rub - p.ads_adjustment_rub - IFNULL(a.bill, 0))) < 0.01, 'PASS', 'FAIL') status,
  STRING_AGG(CONCAT(p.month, ': SKU ', FORMAT('%.2f', p.ads_sku_rub), ' − биллинг ', FORMAT('%.2f', IFNULL(a.bill, 0)), ' = ', FORMAT('%.2f', p.ads_adjustment_rub)), '; ' ORDER BY p.month) detail
FROM pnl p LEFT JOIN ads a ON a.m = p.service_month;

-- @check BRIDGE_STORAGE
WITH pnl AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_STORE_PNL_MONTHLY`),
stor AS (
  SELECT DATE_TRUNC(_rr_date, MONTH) m, SUM(IFNULL(SAFE_CAST(storage_fee AS NUMERIC), 0)) fin
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FINANCE_CANONICAL`
  WHERE supplier_oper_name IN ('Хранение', 'Коррекция хранения') GROUP BY 1)
SELECT 'BRIDGE_STORAGE' check_name,
  IF(MAX(ABS(p.storage_sku_rub - p.storage_adjustment_rub - IFNULL(s.fin, 0))) < 0.01, 'PASS', 'FAIL') status,
  STRING_AGG(CONCAT(p.month, ': SKU ', FORMAT('%.2f', p.storage_sku_rub), ' − факт ', FORMAT('%.2f', IFNULL(s.fin, 0)), ' = ', FORMAT('%.2f', p.storage_adjustment_rub)), '; ' ORDER BY p.month) detail
FROM pnl p LEFT JOIN stor s ON s.m = p.service_month;

-- @check BRIDGE_LOGISTICS_TIMING
WITH pnl AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_STORE_PNL_MONTHLY`)
SELECT 'BRIDGE_LOGISTICS_TIMING' check_name,
  IF(MAX(ABS(IF(cohort_mature, mature_cohort_adjustment_rub - (logistics_adjustment_rub - unsettled_margin_rub),
                timing_bridge_logistics_rub - logistics_adjustment_rub))) < 0.01, 'PASS', 'FAIL') status,
  STRING_AGG(CONCAT(month, ': ', IF(cohort_mature, 'созрела', 'мост'), ' логистика ', FORMAT('%.2f', logistics_adjustment_rub),
    ', непроданные ', CAST(unsettled_qty AS STRING), ' ед. / ', FORMAT('%.2f', unsettled_margin_rub)), '; ' ORDER BY month) detail
FROM pnl;

-- @check FORMULA_IDENTITY
-- 7. Тождество формы (C2): ни одна поправка не вошла дважды и не выпала. Алгебраическое — полноту данных
-- доказывают FINANCE_COVERAGE, DEDUCTION_SOURCE_RECONCILIATION, ORPHAN_FINANCE и сверка снимка с листом (загрузчик).
WITH pnl AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_STORE_PNL_MONTHLY`)
SELECT 'FORMULA_IDENTITY' check_name,
  IF(MAX(ABS(double_count_residual_rub)) < 0.01, 'PASS', 'FAIL') status,
  STRING_AGG(CONCAT(month, ': остаток ', FORMAT('%.4f', double_count_residual_rub)), '; ' ORDER BY month) detail
FROM pnl;

-- @check ROW_IDENTITY
-- 8. Строка P&L складывается из своих колонок (то, что увидит вкладка).
WITH pnl AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_STORE_PNL_MONTHLY`)
SELECT 'ROW_IDENTITY' check_name,
  IF(MAX(ABS(sku_contribution_rub + reconciliation_adjustments_rub + mature_cohort_adjustment_rub - marketplace_costs_rub
             + marketplace_income_rub - management_net_store_profit_rub)) < 0.01, 'PASS', 'FAIL') status,
  CONCAT('месяцев ', CAST(COUNT(*) AS STRING)) detail
FROM pnl;

-- @check STATE_CONSISTENCY
-- 9. Ни один месяц не FINANCIAL_COMPLETE* при открытых условиях; NULL в условиях считается открытым.
WITH pnl AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_STORE_PNL_MONTHLY`)
SELECT 'STATE_CONSISTENCY' check_name,
  IF(COUNTIF(financial_state LIKE 'FINANCIAL_COMPLETE%' AND NOT IFNULL(
         pending_rows = 0 AND unknown_rub <= 0.5 AND unconsumed_finance_rub <= 0.5 AND deduction_source_gap_rub <= 0.5
         AND orphan_finance_base_rub = 0 AND orphan_finance_logistics_rub = 0 AND sku_month_closed AND month_finance_final
         AND ads_days > 0 AND ads_final_days = ads_days
         AND (account_invoice_window_closed OR (minimum_payment_invoices > 0 AND utilization_invoices > 0)), FALSE)) = 0
     AND COUNTIF(financial_state IS NULL) = 0, 'PASS', 'FAIL') status,
  STRING_AGG(CONCAT(month, ' ', IFNULL(financial_state, 'NULL')), '; ' ORDER BY month) detail
FROM pnl;

-- @check SHEET_MODEL_GAP
-- 10. Наследие формул листа (не чинится в Phase C): раскрытие, не блок.
WITH pnl AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_STORE_PNL_MONTHLY`)
SELECT 'SHEET_MODEL_GAP' check_name,
  IF(MAX(model_gap_abs_rub) < 0.01, 'PASS', 'WARN') status,
  STRING_AGG(IF(model_gap_abs_rub >= 0.01, CONCAT(month, ': ', FORMAT('%.2f', model_gap_abs_rub)), NULL), '; ' ORDER BY month) detail
FROM pnl;
