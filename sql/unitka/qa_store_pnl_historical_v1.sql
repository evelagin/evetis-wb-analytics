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
             + marketplace_income_rub - inventory_loss_writeoff_rub + wb_loss_compensation_rub
             - management_net_store_profit_rub)) < 0.01
     AND MAX(IF(authority = 'HISTORICAL_CANONICAL', ABS(management_tax_rub - 0.02 * revenue_rub), 0)) < 0.01
     AND COUNTIF(management_tax_rub IS NULL) = 0, 'PASS', 'FAIL') status,
  STRING_AGG(CONCAT(month, ': налог ', FORMAT('%.2f', management_tax_rub), ', прибыль ', FORMAT('%.2f', management_net_store_profit_rub)), '; ' ORDER BY month) detail
FROM p;

-- @check LOSS_EVIDENCE
-- Утрата товара: доказательства загружены (закрытый wb_secure, импорт с перечитыванием); у каждой
-- позиции ровно один SKU и ровно один интервал себестоимости на дату события; списание в P&L = Σ позиций по месяцу события.
-- Итоги, утверждённые владельцем (позиции/единицы/сумма), проверяет инструмент импорта; здесь они раскрываются.
-- Поштучный аудит читает wb_secure — QA исполняет владелец.
WITH it AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_secure.V_WB_INVENTORY_LOSS_ITEMS`),
p AS (SELECT service_month, inventory_loss_writeoff_rub FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_STORE_PNL_PRESENTATION`),
m AS (SELECT incident_month, SUM(inventory_loss_writeoff_rub) w FROM it GROUP BY 1)
SELECT 'LOSS_EVIDENCE' check_name,
  IF((SELECT COUNT(*) FROM it) > 0
     AND (SELECT COUNTIF(internal_sku IS NULL OR cogs_interval_count != 1 OR unit_cogs_rub IS NULL) FROM it) = 0
     AND (SELECT COUNT(*) FROM (SELECT incident_id, nm_id FROM it GROUP BY 1, 2 HAVING COUNT(*) > 1)) = 0
     AND (SELECT COUNT(*) FROM m LEFT JOIN p ON p.service_month = m.incident_month
          WHERE ABS(IFNULL(p.inventory_loss_writeoff_rub, 0) - m.w) >= 0.01) = 0
     AND ABS((SELECT SUM(inventory_loss_writeoff_rub) FROM p) - (SELECT SUM(inventory_loss_writeoff_rub) FROM it)) < 0.01, 'PASS', 'FAIL') status,
  CONCAT('позиций ', CAST((SELECT COUNT(*) FROM it) AS STRING), ', ед. ', CAST((SELECT SUM(lost_qty) FROM it) AS STRING),
    ', событий ', CAST((SELECT COUNT(DISTINCT incident_id) FROM it) AS STRING), '; ',
    (SELECT STRING_AGG(CONCAT(FORMAT_DATE('%Y-%m', incident_month), ': ', FORMAT('%.2f', w)), '; ' ORDER BY incident_month) FROM m)) detail;

-- @check LOSS_NO_DOUBLE_COUNT
-- Списана только непроданная единица: количество отчёта WB = остаток WB склад × nm на 26.08.2026 и после этого остаток
-- позиций не уменьшался (иначе единица могла уйти в продажу и её себестоимость уже во «Вкладе SKU»).
WITH m AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_INVENTORY_LOSS_MONTHLY`)
SELECT 'LOSS_NO_DOUBLE_COUNT' check_name,
  IF(SUM(inventory_loss_double_count_risk_qty) = 0, 'PASS', 'FAIL') status,
  STRING_AGG(CONCAT(FORMAT_DATE('%Y-%m', service_month), ': ед. под риском двойного счёта ', CAST(inventory_loss_double_count_risk_qty AS STRING),
    ' (', inventory_loss_incidents, ')'), '; ' ORDER BY service_month) detail
FROM m;

-- @check LOSS_COMPENSATION_RECONCILED
-- Компенсация WB за утрату: каждая выплата журнала (LOSS_COMPENSATION_INCOME) сверена с документом закрытого wb_secure
-- (номер, дата, сумма; одна выплата — одна строка финотчёта), каждый документ найден в финотчёте ровно один раз, суммы
-- журнала и документов равны; в «Доходы WB» не попадает. Номера документов в результат не выводятся.
-- Один проход по журналу (планировщик BigQuery не держит журнал дважды в одном запросе).
WITH a AS (
  SELECT COUNTIF(treatment = 'LOSS_COMPENSATION_INCOME' AND NOT IFNULL(loss_evidence_matched, FALSE)) unmatched,
    -SUM(IF(treatment = 'LOSS_COMPENSATION_INCOME', amount_rub, 0)) ledger_rub,
    COUNTIF(treatment = 'LOSS_COMPENSATION_INCOME') ledger_rows,
    COUNTIF(treatment = 'LOSS_COMPENSATION_INCOME' AND category != 'LOSS_COMPENSATION') bad_category,
    COUNTIF(treatment = 'ACCOUNT_LEVEL_INCOME' AND LOWER(label) LIKE '%пострадавшие%') income_leak
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_STORE_ACCOUNT_LEDGER`),
b AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_INVENTORY_LOSS_STATUS`)
SELECT 'LOSS_COMPENSATION_RECONCILED' check_name,
  IF(unmatched = 0 AND bad_category = 0 AND income_leak = 0 AND compensation_documents_not_once = 0
     AND ledger_rows = compensation_documents AND ABS(ledger_rub - compensation_documents_rub) < 0.01, 'PASS', 'FAIL') status,
  CONCAT('журнал ', FORMAT('%.2f', ledger_rub), ' (строк ', CAST(ledger_rows AS STRING), ', не сверено ', CAST(unmatched AS STRING),
    '), документов ', CAST(compensation_documents AS STRING), ' на ', FORMAT('%.2f', compensation_documents_rub),
    ', найдено не ровно один раз ', CAST(compensation_documents_not_once AS STRING),
    ', месяц по документу: ', CAST(compensation_documents_linked AS STRING)) detail
FROM a CROSS JOIN b;

-- @check LOSS_COMPENSATION_IN_PNL
-- В P&L компенсация за утрату = сумма сверенных документов (= журналу, LOSS_COMPENSATION_RECONCILED); несверенных строк нет;
-- доказательства утраты загружены одним импортом.
WITH p AS (SELECT SUM(wb_loss_compensation_rub) comp, SUM(loss_compensation_unreconciled_rows) unrec,
             ANY_VALUE(inventory_loss_evidence_rows) ev_rows, ANY_VALUE(inventory_loss_evidence_runs) ev_runs
           FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_STORE_PNL_PRESENTATION`),
e AS (SELECT compensation_documents_rub rub FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_INVENTORY_LOSS_STATUS`)
SELECT 'LOSS_COMPENSATION_IN_PNL' check_name,
  IF(ABS(p.comp - e.rub) < 0.01 AND p.unrec = 0 AND p.ev_rows > 0 AND p.ev_runs = 1, 'PASS', 'FAIL') status,
  CONCAT('в P&L ', FORMAT('%.2f', p.comp), ', документы ', FORMAT('%.2f', e.rub), ', несверенных строк ', CAST(p.unrec AS STRING),
    ', доказательств строк ', CAST(p.ev_rows AS STRING), ' / импортов ', CAST(p.ev_runs AS STRING)) detail
FROM p CROSS JOIN e;

-- @check LOSS_SINGLE_SOURCE
-- Один источник списания и один источник компенсации: в журнале кабинета нет других строк об утрате/пострадавшем товаре
-- вне LOSS_COMPENSATION_INCOME; утилизация — плата WB (UTILIZATION), а не утрата; в P&L списание = только позиции утраты.
WITH a AS (
  SELECT COUNTIF(REGEXP_CONTAINS(LOWER(label), r'пострадавш|утрач|утрат') AND treatment != 'LOSS_COMPENSATION_INCOME') other_loss_rows,
    COUNTIF(category = 'UTILIZATION' AND treatment NOT IN ('ACCOUNT_LEVEL', 'UNKNOWN')) utilization_misrouted,
    -SUM(IF(category = 'UTILIZATION', -amount_rub, 0)) utilization_rub
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_STORE_ACCOUNT_LEDGER`)
SELECT 'LOSS_SINGLE_SOURCE' check_name,
  IF(other_loss_rows = 0 AND utilization_misrouted = 0, 'PASS', 'FAIL') status,
  CONCAT('прочих строк об утрате: ', CAST(other_loss_rows AS STRING), '; утилизация (расход кабинета, не утрата): ',
    FORMAT('%.2f', utilization_rub)) detail
FROM a;

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
