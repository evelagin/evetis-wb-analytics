-- ============================================================================
-- QA (read-only) — P&L МАГАЗИНА OZON (D3, OWNER ACK 2026-10-14). Док: docs/finance/OZON_STORE_PNL_PHASE_D3.md.
-- Каждая проверка — один SELECT (check_name, status, detail); блоки разделены «-- @check <ID>» и исполняются
-- ПО ОДНОМУ (урок V_OPS / WB QA: единый запрос по всем вью упирается в «query is too complex»).
-- status: PASS / WARN / FAIL. FAIL модели (Q01, Q02, Q04, Q09, Q11, Q17, Q18, Q23) — дефект вью;
-- FAIL данных (Q03, Q05, Q10, Q12, Q13, Q14) — сигнал источника, месяц уже несёт состояние не FINANCIAL_COMPLETE.
-- WARN — раскрытие известного и объяснённого (остатки атрибуции, мост к L4, жизненный цикл отправлений).
-- Q15, Q21, Q22 читают INFORMATION_SCHEMA; Q21 и Q22 имеют смысл только после развёртывания вью.
-- Q24 (паритет с проекцией вне Git) здесь не исполняется.
-- Ничего не создаёт и не меняет.
-- ============================================================================

-- @check Q01_OP_ONCE
-- Каждый ключ RAW (accrual_id|type_id|sku) — ровно одна корневая строка леджера; дочерние строки Premium
-- в сумме дают родителя до копейки.
WITH raw AS (
  SELECT CONCAT(CAST(accrual_id AS STRING), '|', CAST(type_id AS STRING), '|', IFNULL(sku, '')) k, amount_rub
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_FINANCE_ACCRUAL`),
led AS (
  SELECT IFNULL(parent_key, ledger_key) k, SUM(amount_rub) amount_rub
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_STORE_ACCOUNT_LEDGER`
  WHERE source != 'CIS_DOCUMENT' GROUP BY 1),
j AS (
  SELECT raw.k, led.k lk, ABS(IFNULL(raw.amount_rub, 0) - IFNULL(led.amount_rub, 0)) d
  FROM raw FULL JOIN led USING (k))
SELECT 'Q01_OP_ONCE' check_name,
  IF(COUNTIF(k IS NULL OR lk IS NULL) = 0 AND MAX(d) <= 0.005, 'PASS', 'FAIL') status,
  CONCAT('ключей RAW ', CAST(COUNTIF(k IS NOT NULL) AS STRING), ', без строки леджера ', CAST(COUNTIF(lk IS NULL) AS STRING),
         ', лишних в леджере ', CAST(COUNTIF(k IS NULL) AS STRING), ', макс. |Δ суммы| ', CAST(MAX(d) AS STRING)) detail
FROM j;

-- @check Q02_CONSERVATION
-- По месяцу начисления: деньги RAW = деньги леджера; по месяцу P&L: деньги леджера = Σ колонок B…U + неразобранное.
WITH cov AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_STORE_FINANCE_COVERAGE`),
led AS (
  SELECT pnl_month, SUM(money_rub) money FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_STORE_ACCOUNT_LEDGER` GROUP BY 1),
pnl AS (
  SELECT pnl_month,
    seller_base_revenue_rub - commission_rub - logistics_rub - last_mile_rub - return_logistics_rub - acquiring_rub
    - sku_storage_rub - sku_promotion_rub - other_direct_rub - cpc_billing_rub - cpo_billing_rub - store_ads_rub
    - crossdock_rub - processing_rub - fbo_placement_rub - insurance_rub - other_store_costs_rub - premium_rub
    + marketplace_compensation_rub + unconsumed_rub cols
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_STORE_PNL_MONTHLY`),
p AS (SELECT MAX(ABS(IFNULL(led.money, 0) - IFNULL(pnl.cols, 0))) d FROM led FULL JOIN pnl USING (pnl_month))
SELECT 'Q02_CONSERVATION' check_name,
  IF((SELECT MAX(ABS(conservation_residual_rub)) FROM cov) <= 0.005 AND (SELECT d FROM p) <= 0.01, 'PASS', 'FAIL') status,
  CONCAT('макс. |RAW − леджер| по месяцу начисления ', CAST((SELECT MAX(ABS(conservation_residual_rub)) FROM cov) AS STRING),
         '; макс. |леджер − колонки| по месяцу P&L ', CAST((SELECT d FROM p) AS STRING)) detail;

-- @check Q03_NO_SILENT_LOSS
WITH cov AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_STORE_FINANCE_COVERAGE`)
SELECT 'Q03_NO_SILENT_LOSS' check_name,
  IF(COUNTIF(unconsumed_rub != 0 OR unlinked_posting_rub != 0) = 0, 'PASS', 'FAIL') status,
  IFNULL(STRING_AGG(IF(unconsumed_rub != 0 OR unlinked_posting_rub != 0,
    CONCAT(FORMAT_DATE('%Y-%m', booking_month), ': без колонки ', CAST(unconsumed_rub AS STRING), ', без отправления ',
           CAST(unlinked_posting_rub AS STRING)), NULL), '; '), 'неразобранных и несвязанных денег нет') detail
FROM cov;

-- @check Q04_NO_DUPLICATES
-- Ключи леджера уникальны; карта SKU Ozon 1:1; строк RAW = корневых строк леджера.
WITH led AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_STORE_ACCOUNT_LEDGER`),
m AS (
  SELECT COUNTIF(n > 1) multi FROM (
    SELECT marketplace_sku, COUNT(DISTINCT internal_sku) n
    FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP` WHERE marketplace = 'OZON' GROUP BY 1)),
r AS (SELECT COUNT(*) n FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_FINANCE_ACCRUAL`)
SELECT 'Q04_NO_DUPLICATES' check_name,
  IF(COUNT(*) = COUNT(DISTINCT ledger_key) AND ANY_VALUE(m.multi) = 0
     AND COUNT(DISTINCT IF(source != 'CIS_DOCUMENT', IFNULL(parent_key, ledger_key), NULL)) = ANY_VALUE(r.n), 'PASS', 'FAIL') status,
  CONCAT('строк ', CAST(COUNT(*) AS STRING), ', ключей ', CAST(COUNT(DISTINCT ledger_key) AS STRING),
         ', корней RAW ', CAST(COUNT(DISTINCT IF(source != 'CIS_DOCUMENT', IFNULL(parent_key, ledger_key), NULL)) AS STRING),
         ' из ', CAST(ANY_VALUE(r.n) AS STRING), ', SKU с >1 internal_sku ', CAST(ANY_VALUE(m.multi) AS STRING)) detail
FROM led CROSS JOIN m CROSS JOIN r;

-- @check Q05_NEW_TYPE_BLOCKS
WITH n AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_FINANCE_NEW_OPERATIONS`)
SELECT 'Q05_NEW_TYPE_BLOCKS' check_name, IF(COUNT(*) = 0, 'PASS', 'FAIL') status,
  IF(COUNT(*) = 0, 'неразобранных начислений нет',
     CONCAT('строк ', CAST(COUNT(*) AS STRING), ': ',
            STRING_AGG(DISTINCT CONCAT(issue, ' type ', CAST(type_id AS STRING)), '; '))) detail
FROM n;

-- @check Q06_ADS_BILLING_IDENTITY
-- Биллинг CPC+CPO по финансам = Performance expense по месяцу (кроме открытого месяца).
WITH a AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_STORE_ADS_RECON_MONTHLY`),
s AS (SELECT pnl_month, month_closure_status FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_STORE_MONTH_STATE`)
SELECT 'Q06_ADS_BILLING_IDENTITY' check_name,
  IF(COUNTIF(ABS(billing_vs_performance_rub) > 0.02 AND IFNULL(month_closure_status, 'OPEN') != 'OPEN') = 0,
     IF(COUNTIF(ABS(billing_vs_performance_rub) > 0.02) = 0, 'PASS', 'WARN'), 'FAIL') status,
  IFNULL(STRING_AGG(IF(ABS(billing_vs_performance_rub) > 0.02,
    CONCAT(FORMAT_DATE('%Y-%m', pnl_month), ' ', CAST(billing_vs_performance_rub AS STRING)), NULL), '; '),
    'финансы = Performance по каждому месяцу') detail
FROM a LEFT JOIN s USING (pnl_month);

-- @check Q07_ATTRIBUTION_LE_BILLING
-- Атрибуция CPC не превышает биллинг месяца (отрицательный остаток > 0,50 ₽ — объяснить: упор в бюджет).
WITH a AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_STORE_ADS_RECON_MONTHLY`)
SELECT 'Q07_ATTRIBUTION_LE_BILLING' check_name, IF(COUNTIF(cpc_residual_rub < -0.50) = 0, 'PASS', 'WARN') status,
  CONCAT('остаток CPC (биллинг − атрибуция) за всё время ', CAST(SUM(cpc_residual_rub) AS STRING),
         '; месяцев с остатком > 0,50 ₽: ', CAST(COUNTIF(cpc_residual_rub > 0.50) AS STRING),
         '; с атрибуцией выше биллинга: ', CAST(COUNTIF(cpc_residual_rub < -0.50) AS STRING)) detail
FROM a;

-- @check Q08_CPO_PHASE_B
-- Атрибуция CPO за закрытые месяцы ≈ биллинг CPO (сдвиг месяца списание → заказ раскрыт в cpo_residual_rub).
WITH a AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_STORE_ADS_RECON_MONTHLY`),
s AS (SELECT pnl_month, month_closure_status FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_STORE_MONTH_STATE`)
SELECT 'Q08_CPO_PHASE_B' check_name,
  IF(ABS(SUM(IF(IFNULL(month_closure_status, 'OPEN') = 'CLOSED', cpo_residual_rub, 0))) <= 0.02, 'PASS', 'WARN') status,
  CONCAT('Σ (биллинг CPO − атрибуция CPO) по закрытым месяцам ',
         CAST(SUM(IF(IFNULL(month_closure_status, 'OPEN') = 'CLOSED', cpo_residual_rub, 0)) AS STRING),
         '; по открытым ', CAST(SUM(IF(IFNULL(month_closure_status, 'OPEN') != 'CLOSED', cpo_residual_rub, 0)) AS STRING),
         ' (несопоставленный CPO остаётся на уровне магазина)') detail
FROM a LEFT JOIN s USING (pnl_month);

-- @check Q09_PROMO_ONCE
-- 116/74/48 — только в колонке J; ни одного рубля продвижения SKU в рекламе L/M/N.
SELECT 'Q09_PROMO_ONCE' check_name,
  IF(COUNTIF(type_id IN (116, 74, 48) AND pnl_column != 'J') + COUNTIF(pnl_column = 'J' AND type_id NOT IN (116, 74, 48)) = 0, 'PASS', 'FAIL') status,
  CONCAT('строк 116/74/48: ', CAST(COUNTIF(type_id IN (116, 74, 48)) AS STRING), ', вне J: ',
         CAST(COUNTIF(type_id IN (116, 74, 48) AND pnl_column != 'J') AS STRING)) detail
FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_STORE_ACCOUNT_LEDGER`;

-- @check Q10_MISSING_NE_ZERO
-- Доставлено без начисления → провизорная оценка ≠ 0 и со всеми входами (COGS, комиссия, логистика), иначе месяц
-- PARTIAL_SOURCE_MISSING; закрытый месяц с такими единицами не FINANCIAL_COMPLETE.
SELECT 'Q10_MISSING_NE_ZERO' check_name,
  IF(COUNTIF(provisional_units > 0 AND provisional_open_rub = 0) = 0
     AND COUNTIF(provisional_units > 0 AND financial_state LIKE 'FINANCIAL_COMPLETE%') = 0
     AND COUNTIF(provisional_inputs_missing > 0 AND financial_state != 'PARTIAL_SOURCE_MISSING'
                 AND financial_state != 'PENDING_CLASSIFICATION') = 0, 'PASS', 'FAIL') status,
  IFNULL(STRING_AGG(IF(provisional_units > 0,
    CONCAT(FORMAT_DATE('%Y-%m', pnl_month), ': ед. ', CAST(provisional_units AS STRING), ', X ',
           CAST(ROUND(provisional_open_rub, 2) AS STRING), ', без входов ', CAST(provisional_inputs_missing AS STRING),
           ', ', financial_state), NULL), '; '), 'доставленных без начисления нет') detail
FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_STORE_MONTH_STATE`;

-- @check Q11_ACCOUNT_LEVEL_NE_MISSING
-- Расходы кабинета и реклама магазина — свои колонки, на SKU не распределяются (ALLOCATED_SKU = 0).
SELECT 'Q11_ACCOUNT_LEVEL_NE_MISSING' check_name,
  IF(COUNTIF(type_id IN (12, 77, 46, 76, 57, 52, 25) AND (pnl_class != 'ACCOUNT_LEVEL' OR pnl_column NOT IN ('O', 'P', 'Q', 'R', 'S', 'T', 'U')))
     + COUNTIF(type_id IN (47, 96) AND pnl_column != 'N')
     + COUNTIF(type_id IN (12, 77, 46, 76, 57, 52, 25, 47, 96) AND internal_sku IS NOT NULL) = 0, 'PASS', 'FAIL') status,
  CONCAT('строк кабинета ', CAST(COUNTIF(pnl_class = 'ACCOUNT_LEVEL') AS STRING), ', с привязкой к SKU ',
         CAST(COUNTIF(type_id IN (12, 77, 46, 76, 57, 52, 25, 47, 96) AND internal_sku IS NOT NULL) AS STRING)) detail
FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_STORE_ACCOUNT_LEDGER`;

-- @check Q12_UNPROVEN_NE_ESTIMATED
-- Оценки — только в X; месяц с X ≠ 0 не может быть FINANCIAL_COMPLETE.
SELECT 'Q12_UNPROVEN_NE_ESTIMATED' check_name,
  IF(COUNTIF(financial_state = 'FINANCIAL_COMPLETE' AND provisional_open_rub != 0) = 0, 'PASS', 'FAIL') status,
  CONCAT('месяцев FINANCIAL_COMPLETE ', CAST(COUNTIF(financial_state = 'FINANCIAL_COMPLETE') AS STRING),
         ', из них с X ≠ 0: ', CAST(COUNTIF(financial_state = 'FINANCIAL_COMPLETE' AND provisional_open_rub != 0) AS STRING)) detail
FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_STORE_MONTH_STATE`;

-- @check Q13_CORRECTIONS_RESTATE
-- Сторно (−1) стоит на месяце заказа исходного отправления.
SELECT 'Q13_CORRECTIONS_RESTATE' check_name,
  IF(COUNTIF(type_id = -1 AND NOT posting_linked) = 0, 'PASS', 'FAIL') status,
  CONCAT('строк сторно −1: ', CAST(COUNTIF(type_id = -1) AS STRING), ', без отправления ',
         CAST(COUNTIF(type_id = -1 AND NOT posting_linked) AS STRING),
         ', сумма реализации ', CAST(SUM(IF(type_id = -1, seller_base_rub, 0)) AS STRING)) detail
FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_STORE_ACCOUNT_LEDGER`;

-- @check Q14_COGS_EFFECTIVE_DATE
-- Себестоимость на дату заказа ровно одним интервалом; AF (COGS возвращённых единиц) справочно.
SELECT 'Q14_COGS_EFFECTIVE_DATE' check_name, IF(SUM(cogs_gap_units) = 0, 'PASS', 'FAIL') status,
  CONCAT('единиц с 0 или >1 интервалом COGS: ', CAST(SUM(cogs_gap_units) AS STRING),
         '; COGS возвращённых единиц (не сторнируется): ',
         CAST((SELECT SUM(returned_units_cogs_not_reversed_rub)
               FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_STORE_PNL_MONTHLY`) AS STRING)) detail
FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_STORE_MONTH_STATE`;

-- @check Q15_L1_L4_UNCHANGED
-- Тело FCT_OZON_PNL_MONTHLY в production = канон Git (canonical_body_sha256 манифеста; tools/tests сверяют константу).
SELECT 'Q15_L1_L4_UNCHANGED' check_name,
  IF(TO_HEX(SHA256(REGEXP_REPLACE(TRIM(view_definition), r';$', '')))
     = 'e1b518237ca6dc1b1f1110d4b44215ee76305e5087cf838a0a2ba74b2828661a', 'PASS', 'FAIL') status,
  CONCAT('sha256 тела в production ', TO_HEX(SHA256(REGEXP_REPLACE(TRIM(view_definition), r';$', '')))) detail
FROM `project-fa311fc0-4d87-4781-986.ozon_mart.INFORMATION_SCHEMA.VIEWS`
WHERE table_name = 'FCT_OZON_PNL_MONTHLY';

-- @check Q16_L4_BRIDGE
-- V − L4 = Δбазиса + ΔPremium + прочее; за жизнь Σ Δбазиса = 0 и Σ ΔPremium = 0; «прочее» раскрывается по месяцам
-- (статус отправления CD-3, провизорная оценка X, выкуп СНГ без документа, сторно D1 после ремонта).
SELECT 'Q16_L4_BRIDGE' check_name,
  IF(ABS(SUM(delta_basis_rub)) > 0.01 OR ABS(SUM(delta_premium_rub)) > 0.01, 'FAIL',
     IF(COUNTIF(ABS(l4_bridge_other_rub) > 0.02) = 0, 'PASS', 'WARN')) status,
  CONCAT('Σ Δбазиса ', CAST(SUM(delta_basis_rub) AS STRING), ', Σ ΔPremium ', CAST(SUM(delta_premium_rub) AS STRING),
         '; прочее: ', IFNULL(STRING_AGG(IF(ABS(l4_bridge_other_rub) > 0.02,
           CONCAT(FORMAT_DATE('%Y-%m', pnl_month), ' ', CAST(ROUND(l4_bridge_other_rub, 2) AS STRING)), NULL), ', '), 'нет')) detail
FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_STORE_PNL_MONTHLY`;

-- @check Q17_TAX_NOT_DOUBLE_COUNTED
-- Y = V − W; Y = AA + AB + AC + X (I1); W = 0,02 × B.
SELECT 'Q17_TAX_NOT_DOUBLE_COUNTED' check_name,
  IF(MAX(ABS(management_net_store_profit_rub - (store_contribution_pre_tax_rub - management_tax_rub))) <= 0.02
     AND MAX(ABS(management_net_store_profit_rub - (sku_contribution_rub + ads_adjustment_rub + unattributed_account_net_rub
                                                    + provisional_open_rub))) <= 0.02
     AND MAX(ABS(management_tax_rub - 0.02 * seller_base_revenue_rub)) <= 0.01, 'PASS', 'FAIL') status,
  CONCAT('макс. |Y − (AA+AB+AC+X)| ',
         CAST(MAX(ABS(management_net_store_profit_rub - (sku_contribution_rub + ads_adjustment_rub + unattributed_account_net_rub
                                                         + provisional_open_rub))) AS STRING)) detail
FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_STORE_PNL_MONTHLY`;

-- @check Q18_MEMO_EXCLUDED
-- Бонус Ozon, соинвестирование и «оплачено покупателем» в деньги не входят.
SELECT 'Q18_MEMO_EXCLUDED' check_name,
  IF(COUNTIF(ABS(money_rub - (seller_base_rub + commission_rub + amount_rub)) > 0.000001) = 0, 'PASS', 'FAIL') status,
  CONCAT('MEMO (не в деньгах): бонус ', CAST(SUM(IFNULL(memo_ozon_bonus_rub, 0)) AS STRING),
         ', соинвестирование ', CAST(SUM(IFNULL(memo_ozon_coinvestment_rub, 0)) AS STRING)) detail
FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_STORE_ACCOUNT_LEDGER`;

-- @check Q19_FINANCE_CONTROL
-- Загрузки finance_accrual с кодами контроля D1 (OZON_FINANCE_ACCRUAL_QUARANTINED / OZON_FINANCE_CONTROL_MISMATCH
-- в error_message при status OK) переводят месяцы своего окна в PARTIAL_SOURCE_MISSING и не двигают водяной знак.
WITH r AS (
  SELECT * FROM `project-fa311fc0-4d87-4781-986.ozon_raw.OZON_INGESTION_RUNS`
  WHERE entity = 'finance_accrual' AND REGEXP_CONTAINS(IFNULL(error_message, ''), r'OZON_FINANCE_')),
s AS (
  SELECT COUNTIF(finance_control_runs > 0) months_flagged,
    COUNTIF(finance_control_runs > 0 AND financial_state NOT IN ('PARTIAL_SOURCE_MISSING', 'PENDING_CLASSIFICATION')) months_not_gated
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_STORE_MONTH_STATE`)
SELECT 'Q19_FINANCE_CONTROL' check_name,
  IF((SELECT months_not_gated FROM s) > 0, 'FAIL', IF((SELECT COUNT(*) FROM r) = 0, 'PASS', 'WARN')) status,
  CONCAT('загрузок с кодами контроля: ', CAST((SELECT COUNT(*) FROM r) AS STRING),
         '; месяцев под воротами: ', CAST((SELECT months_flagged FROM s) AS STRING),
         ', без ворот: ', CAST((SELECT months_not_gated FROM s) AS STRING)) detail;

-- @check Q20_LIFECYCLE
-- Нетерминальные отправления старше 30 суток относительно водяного знака (раскрытие; признание P&L
-- идёт по начислению и от статуса не зависит).
WITH wm AS (SELECT MAX(finance_watermark_date) d FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_STORE_MONTH_STATE`),
p AS (
  SELECT posting_number, MIN(DATE(created_at, 'Europe/Moscow')) od, MAX(status) status
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_POSTINGS_FBO` GROUP BY 1)
SELECT 'Q20_LIFECYCLE' check_name,
  IF(COUNTIF(status IN ('delivering', 'awaiting_deliver', 'awaiting_packaging') AND od < DATE_SUB(wm.d, INTERVAL 30 DAY)) = 0, 'PASS', 'WARN') status,
  CONCAT('нетерминальных отправлений старше 30 суток: ',
         CAST(COUNTIF(status IN ('delivering', 'awaiting_deliver', 'awaiting_packaging') AND od < DATE_SUB(wm.d, INTERVAL 30 DAY)) AS STRING)) detail
FROM p CROSS JOIN wm GROUP BY wm.d;

-- @check Q21_STATE_DATA_DRIVEN
-- Ни одно новое вью D3 не использует CURRENT_DATE / CURRENT_TIMESTAMP: время только из водяного знака.
SELECT 'Q21_STATE_DATA_DRIVEN' check_name,
  IF(COUNT(*) = 7 AND COUNTIF(REGEXP_CONTAINS(UPPER(view_definition), r'CURRENT_(DATE|TIMESTAMP|DATETIME)')) = 0, 'PASS', 'FAIL') status,
  CONCAT('вью D3 в production: ', CAST(COUNT(*) AS STRING), ' из 7; со ссылкой на текущее время: ',
         CAST(COUNTIF(REGEXP_CONTAINS(UPPER(view_definition), r'CURRENT_(DATE|TIMESTAMP|DATETIME)')) AS STRING)) detail
FROM `project-fa311fc0-4d87-4781-986.ozon_mart.INFORMATION_SCHEMA.VIEWS`
WHERE table_name IN ('V_OZON_FINANCE_OPERATION_MAP', 'V_OZON_STORE_ACCOUNT_LEDGER', 'V_OZON_FINANCE_NEW_OPERATIONS',
                     'V_OZON_STORE_FINANCE_COVERAGE', 'V_OZON_STORE_ADS_RECON_MONTHLY', 'V_OZON_STORE_MONTH_STATE',
                     'V_OZON_STORE_PNL_MONTHLY');

-- @check Q22_ISOLATION
-- Ни одной ссылки на датасеты WB в новых вью (изоляция маркетплейсов).
SELECT 'Q22_ISOLATION' check_name,
  IF(COUNTIF(REGEXP_CONTAINS(LOWER(view_definition), r'\.wb_[a-z]+\.')) = 0, 'PASS', 'FAIL') status,
  CONCAT('вью со ссылкой на wb_*: ', CAST(COUNTIF(REGEXP_CONTAINS(LOWER(view_definition), r'\.wb_[a-z]+\.')) AS STRING)) detail
FROM `project-fa311fc0-4d87-4781-986.ozon_mart.INFORMATION_SCHEMA.VIEWS`
WHERE table_name IN ('V_OZON_FINANCE_OPERATION_MAP', 'V_OZON_STORE_ACCOUNT_LEDGER', 'V_OZON_FINANCE_NEW_OPERATIONS',
                     'V_OZON_STORE_FINANCE_COVERAGE', 'V_OZON_STORE_ADS_RECON_MONTHLY', 'V_OZON_STORE_MONTH_STATE',
                     'V_OZON_STORE_PNL_MONTHLY');

-- @check Q23_ROW_IDENTITY
-- Каждая строка месяца: V = B − C − D − … − T + U + X до 0,02 ₽ (пересчёт независимо от вью).
SELECT 'Q23_ROW_IDENTITY' check_name,
  IF(MAX(ABS(store_contribution_pre_tax_rub - (seller_base_revenue_rub - product_cogs_rub - commission_rub - logistics_rub
       - last_mile_rub - return_logistics_rub - acquiring_rub - sku_storage_rub - sku_promotion_rub - other_direct_rub
       - cpc_billing_rub - cpo_billing_rub - store_ads_rub - crossdock_rub - processing_rub - fbo_placement_rub
       - insurance_rub - other_store_costs_rub - premium_rub + marketplace_compensation_rub + provisional_open_rub))) <= 0.02,
     'PASS', 'FAIL') status,
  CONCAT('месяцев ', CAST(COUNT(*) AS STRING)) detail
FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_STORE_PNL_MONTHLY`;
