-- ============================================================================
-- WB HISTORICAL H0 (OWNER ACK 2026-10-11) — исторический канонический P&L магазина WB.
-- Отдельный слой: месяцы [HIST_FROM, PHASE_C_FROM) считаются СВЕРХУ ВНИЗ из канонических источников,
-- формулы листа — только сравнение. Месяцы с PHASE_C_FROM — текущая V_WB_STORE_PNL_MONTHLY. Одна власть на месяц.
-- ============================================================================

-- ─── H1. Утверждённые построчные классификации (доказательства — docs/finance/WB_HISTORICAL_H0_2026-10-11.md) ──
-- Override меняет ТОЛЬКО класс и месяц услуги строки журнала; сумма всегда из журнала (QA: ключ ровно один раз,
-- сумма не изменилась). 8 строк «Баллы за отзывы»: авансы и возвраты остатка, месяц услуги — месяц аванса.
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_FINANCE_HISTORICAL_OVERRIDES` AS
SELECT * FROM UNNEST(ARRAY<STRUCT<finance_row_key STRING, category STRING, treatment STRING, service_month DATE, evidence STRING>>[
  ('736339502#3125108308594', 'REVIEW_POINTS', 'ACCOUNT_LEVEL', DATE '2026-05-01', 'Аванс «Баллы за отзывы» 7 320 (=6 000×1,22), пара → 778547028#3129482305787, лаг 42 дн.'),
  ('736339502#3125395067778', 'REVIEW_POINTS', 'ACCOUNT_LEVEL', DATE '2026-05-01', 'Аванс 5 270,40 (=4 320×1,22), группа возвратов 11.07'),
  ('736339502#3125395067779', 'REVIEW_POINTS', 'ACCOUNT_LEVEL', DATE '2026-05-01', 'Аванс 4 392 (=3 600×1,22), группа возвратов 11.07'),
  ('757272778#3127070470717', 'REVIEW_POINTS', 'ACCOUNT_LEVEL', DATE '2026-06-01', 'Аванс 2 928 (=2 400×1,22), пара → 799477037#3131430022164 (подписан), лаг 43 дн.'),
  ('778547028#3129482305787', 'REVIEW_POINTS', 'ACCOUNT_LEVEL', DATE '2026-05-01', 'Возврат остатка −6 954 (=5 700×1,22) аванса 27.05 → месяц услуги аванса'),
  ('778547028#3129808785281', 'REVIEW_POINTS', 'ACCOUNT_LEVEL', DATE '2026-05-01', 'Возврат остатка −3 952,80 (=3 240×1,22) аванса 30.05'),
  ('778547028#3129808785280', 'REVIEW_POINTS', 'ACCOUNT_LEVEL', DATE '2026-05-01', 'Возврат остатка −4 172,40 (=3 420×1,22) аванса 30.05'),
  ('799477037#3131430022164', 'REVIEW_POINTS', 'ACCOUNT_LEVEL', DATE '2026-06-01', 'Подписанный возврат остатка аванса «Баллы за отзывы» −2 928 → контра-расход месяца аванса 15.06')
]);

-- ─── H2. Исторический P&L сверху вниз ────────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_STORE_PNL_HISTORICAL` AS
WITH bounds AS (SELECT DATE '2026-05-01' hist_from, DATE '2026-08-01' phase_c_from),
months AS (
  SELECT m FROM bounds, UNNEST(GENERATE_DATE_ARRAY(hist_from, DATE_SUB(phase_c_from, INTERVAL 1 MONTH), INTERVAL 1 MONTH)) m),
-- Когорта заказа (сутки МСК × nm): выручка по цене продавца, удержание WB, логистика — как Phase C.
-- 🔴 Без evetis_ref (решение 1C1B/1C2A: учётки загрузчиков Unitka не читают справочный слой). Связка nm → SKU —
-- wb_raw.REF_SKU_MASTER, себестоимость — физическая копия канона wb_mart.UNITKA_COGS_EFFECTIVE (sp_publish_unitka_cogs,
-- ASSERT на пересечения интервалов). Обе связки — РОВНО одно значение, иначе NULL (себестоимость неизвестна, не 0).
coh AS (
  SELECT c.order_date_msk d, c.nm_id, c.sold_qty, c.seller_base_rub, c.wb_take_rub, c.logistics_rub, c.provisional_rows
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_STORE_FINANCE_COHORT_DAILY` c
  WHERE c.order_date_msk >= (SELECT hist_from FROM bounds) AND c.order_date_msk < (SELECT phase_c_from FROM bounds)),
ref AS (
  SELECT nm_id, IF(COUNT(DISTINCT internal_sku) = 1, ANY_VALUE(internal_sku), NULL) internal_sku
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.REF_SKU_MASTER` WHERE marketplace = 'WB' GROUP BY 1),
unit_cogs AS (
  SELECT k.d, k.nm_id, IF(COUNT(u.internal_sku) = 1, MAX(u.product_cogs_rub), NULL) cogs_unit
  FROM (SELECT DISTINCT d, nm_id FROM coh) k
  LEFT JOIN ref USING (nm_id)
  LEFT JOIN `project-fa311fc0-4d87-4781-986.wb_mart.UNITKA_COGS_EFFECTIVE` u
    ON u.internal_sku = ref.internal_sku AND k.d BETWEEN u.effective_from AND IFNULL(u.effective_to, DATE '9999-12-31')
  GROUP BY 1, 2),
-- Связки агрегированы до (сутки, nm) — размножение строк когорты невозможно по построению; QA сверяет ещё и итоги.
coh_cogs AS (SELECT coh.*, g.cogs_unit FROM coh LEFT JOIN unit_cogs g USING (d, nm_id)),
coh_base AS (
  SELECT DATE_TRUNC(d, MONTH) m, SUM(seller_base_rub) base_rub, SUM(sold_qty) base_qty FROM coh GROUP BY 1),
sku AS (
  SELECT DATE_TRUNC(d, MONTH) m,
    SUM(seller_base_rub) revenue_rub, SUM(wb_take_rub) wb_take_rub, SUM(logistics_rub) logistics_rub, SUM(sold_qty) sold_qty,
    SUM(0.02 * seller_base_rub) management_tax_rub,                       -- налог 2 % от цены продавца (контракт R6)
    SUM(sold_qty * IFNULL(cogs_unit, 0)) cogs_rub,
    SUM(IF(cogs_unit IS NULL AND sold_qty != 0, sold_qty, 0)) cogs_missing_qty,
    SUM(provisional_rows) provisional_rows
  FROM coh_cogs GROUP BY 1),
-- Атрибуция рекламы по SKU (представление в SKU) и биллинг WB по дате услуги (факт) — мост как в Phase C.
ads AS (
  SELECT DATE_TRUNC(day, MONTH) m, SUM(ad_spend_financial_rub) ads_billing_rub, SUM(ad_spend_attributed_rub) ads_attributed_rub,
    COUNTIF(ads_billing_is_final) ads_final_days, COUNT(*) ads_days
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_FINANCE_CORRECTED_DAILY` GROUP BY 1),
storage_fin AS (
  SELECT DATE_TRUNC(_rr_date, MONTH) m, SUM(IFNULL(SAFE_CAST(storage_fee AS NUMERIC), 0)) storage_finance_rub
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FINANCE_CANONICAL`
  WHERE supplier_oper_name IN ('Хранение', 'Коррекция хранения') GROUP BY 1),
-- Журнал кабинета + утверждённые построчные классификации (override заменяет класс строки, сумма — из журнала).
ledger AS (
  SELECT IFNULL(o.service_month, l.service_month) service_month,
    IFNULL(o.category, l.category) category, IFNULL(o.treatment, l.treatment) treatment, l.amount_rub, l.booking_date,
    o.finance_row_key IS NOT NULL overridden
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_STORE_ACCOUNT_LEDGER` l
  LEFT JOIN `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_FINANCE_HISTORICAL_OVERRIDES` o USING (finance_row_key)),
acc AS (
  SELECT service_month m,
    SUM(IF(category = 'MINIMUM_PAYMENT', amount_rub, 0)) minimum_payment_rub,
    SUM(IF(category = 'UTILIZATION', amount_rub, 0)) utilization_rub,
    SUM(IF(category = 'PENALTY', amount_rub, 0)) penalty_rub,
    SUM(IF(category = 'TRANSIT', amount_rub, 0)) transit_rub,
    SUM(IF(category = 'ACCEPTANCE', amount_rub, 0)) acceptance_rub,
    SUM(IF(treatment = 'ACCOUNT_LEVEL' AND category NOT IN ('MINIMUM_PAYMENT', 'UTILIZATION', 'PENALTY', 'TRANSIT', 'ACCEPTANCE'), amount_rub, 0)) other_marketplace_cost_rub,
    -SUM(IF(treatment = 'ACCOUNT_LEVEL_INCOME', amount_rub, 0)) marketplace_income_rub,
    -SUM(IF(treatment = 'LOSS_COMPENSATION_INCOME', amount_rub, 0)) wb_loss_compensation_rub,
    STRING_AGG(IF(treatment = 'LOSS_COMPENSATION_INCOME', CAST(booking_date AS STRING), NULL), ', ' ORDER BY booking_date) wb_loss_compensation_dates,
    SUM(IF(treatment = 'UNKNOWN', ABS(amount_rub), 0)) unknown_rub,
    COUNTIF(overridden) overridden_rows,
    COUNTIF(category = 'MINIMUM_PAYMENT') minimum_payment_invoices, COUNTIF(category = 'UTILIZATION') utilization_invoices
  FROM ledger GROUP BY 1),
-- Деньги финотчёта, которые модель не читает (кроме PENDING) — должно быть 0; PENDING — отдельно. Окно: месяц + 20 суток.
uncovered AS (
  SELECT mo.m,
    SUM(IF(c.treatment IN ('NEW_FINANCE_OPERATION', 'PENDING_CLASSIFICATION'), 0, c.unconsumed_abs_rub)) unconsumed_finance_rub,
    SUM(IF(c.treatment IN ('NEW_FINANCE_OPERATION', 'PENDING_CLASSIFICATION'), c.unconsumed_abs_rub, 0)) pending_rub,
    COUNTIF(c.treatment IN ('NEW_FINANCE_OPERATION', 'PENDING_CLASSIFICATION')) pending_rows
  FROM months mo JOIN `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_STORE_FINANCE_COVERAGE` c
    ON c.booking_date BETWEEN mo.m AND DATE_ADD(LAST_DAY(mo.m), INTERVAL 20 DAY)
  GROUP BY 1),
-- Внешняя реклама (блогеры, самовыкупы) — ручной ввод листа (N), как в Phase C; источник — последний снимок листа.
-- Снимок, начатый позже начала истории (образ до H0 снимает лист с 2026-08), НЕ даёт «внешней рекламы = 0»:
-- ext_covered = FALSE → месяц UNKNOWN_COST_PRESENT, пока снимок не покроет историю.
ext_snap AS (
  SELECT snapshot_id, MIN(date_msk) <= (SELECT hist_from FROM bounds) covered
  FROM `project-fa311fc0-4d87-4781-986.wb_ops.UNITKA_SKU_COMPONENTS_DAILY`
  WHERE snapshot_id = (SELECT MAX(snapshot_id) FROM `project-fa311fc0-4d87-4781-986.wb_ops.UNITKA_SKU_COMPONENTS_DAILY`)
  GROUP BY 1),
ext AS (
  SELECT DATE_TRUNC(date_msk, MONTH) m, SUM(ads_out) external_marketing_rub
  FROM `project-fa311fc0-4d87-4781-986.wb_ops.UNITKA_SKU_COMPONENTS_DAILY`
  WHERE snapshot_id = (SELECT snapshot_id FROM ext_snap)
  GROUP BY 1),
fin_days AS (
  SELECT DISTINCT d
  FROM (SELECT DISTINCT SAFE_CAST(report_period_from AS DATE) pf, SAFE_CAST(report_period_to AS DATE) pt
        FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FINANCE_CANONICAL` WHERE finance_status = 'FINAL'),
    UNNEST(GENERATE_DATE_ARRAY(pf, pt)) d
  WHERE pf IS NOT NULL AND pt >= pf),
cov AS (
  SELECT mo.m,
    COUNTIF(fd.d BETWEEN mo.m AND LAST_DAY(mo.m)) = DATE_DIFF(LAST_DAY(mo.m), mo.m, DAY) + 1 final_month,
    COUNTIF(fd.d BETWEEN mo.m AND DATE_ADD(LAST_DAY(mo.m), INTERVAL 20 DAY)) = DATE_DIFF(LAST_DAY(mo.m), mo.m, DAY) + 21 final_m20,
    COUNTIF(fd.d BETWEEN mo.m AND DATE_ADD(LAST_DAY(mo.m), INTERVAL 46 DAY)) = DATE_DIFF(LAST_DAY(mo.m), mo.m, DAY) + 47 final_m46
  FROM months mo CROSS JOIN fin_days fd GROUP BY 1),
j AS (
  SELECT mo.m,
    IFNULL(s.revenue_rub, 0) revenue_rub, IFNULL(s.wb_take_rub, 0) wb_take_rub, IFNULL(s.logistics_rub, 0) logistics_rub,
    IFNULL(s.sold_qty, 0) sold_qty, IFNULL(s.management_tax_rub, 0) management_tax_rub, IFNULL(s.cogs_rub, 0) cogs_rub,
    IFNULL(s.cogs_missing_qty, 0) cogs_missing_qty, IFNULL(s.provisional_rows, 0) provisional_rows,
    IFNULL(a.ads_billing_rub, 0) ads_billing_rub, IFNULL(a.ads_attributed_rub, 0) ads_attributed_rub,
    IFNULL(a.ads_final_days, 0) ads_final_days, IFNULL(a.ads_days, 0) ads_days,
    IFNULL(sf.storage_finance_rub, 0) storage_finance_rub,
    IFNULL(ac.minimum_payment_rub, 0) minimum_payment_rub, IFNULL(ac.utilization_rub, 0) utilization_rub,
    IFNULL(ac.penalty_rub, 0) penalty_rub, IFNULL(ac.transit_rub, 0) transit_rub, IFNULL(ac.acceptance_rub, 0) acceptance_rub,
    IFNULL(ac.other_marketplace_cost_rub, 0) other_marketplace_cost_rub, IFNULL(ac.marketplace_income_rub, 0) marketplace_income_rub,
    IFNULL(ac.unknown_rub, 0) unknown_rub, IFNULL(ac.overridden_rows, 0) overridden_rows,
    IFNULL(u.unconsumed_finance_rub, 0) unconsumed_finance_rub, IFNULL(u.pending_rub, 0) pending_rub, IFNULL(u.pending_rows, 0) pending_rows,
    IFNULL(e.external_marketing_rub, 0) external_marketing_rub, IFNULL((SELECT covered FROM ext_snap), FALSE) external_marketing_covered,
    ABS(IFNULL(cb.base_rub, 0) - IFNULL(s.revenue_rub, 0)) + ABS(IFNULL(cb.base_qty, 0) - IFNULL(s.sold_qty, 0)) cohort_join_gap,
    IFNULL(cv.final_month, FALSE) month_finance_final, IFNULL(cv.final_m20, FALSE) account_invoice_window_closed,
    IFNULL(cv.final_m46, FALSE) cohort_mature,
    IFNULL(ac.minimum_payment_invoices, 0) minimum_payment_invoices, IFNULL(ac.utilization_invoices, 0) utilization_invoices,
    IFNULL(ac.wb_loss_compensation_rub, 0) wb_loss_compensation_rub, ac.wb_loss_compensation_dates,
    IFNULL(lo.inventory_loss_writeoff_rub, 0) inventory_loss_writeoff_rub, IFNULL(lo.inventory_loss_qty, 0) inventory_loss_qty,
    IFNULL(lo.inventory_loss_cogs_missing_qty, 0) inventory_loss_cogs_missing_qty,
    IFNULL(lo.inventory_loss_post_incident_orders, 0) inventory_loss_post_incident_orders, lo.inventory_loss_incident_list
  FROM months mo LEFT JOIN sku s ON s.m = mo.m LEFT JOIN ads a ON a.m = mo.m LEFT JOIN storage_fin sf ON sf.m = mo.m
  LEFT JOIN acc ac ON ac.m = mo.m LEFT JOIN uncovered u ON u.m = mo.m LEFT JOIN ext e ON e.m = mo.m LEFT JOIN cov cv ON cv.m = mo.m
  LEFT JOIN coh_base cb ON cb.m = mo.m
  LEFT JOIN `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_INVENTORY_LOSS_MONTHLY` lo ON lo.service_month = mo.m),
k AS (
  SELECT j.*,
    -- Вклад SKU (канонический): выручка − удержание WB − логистика − налог − COGS − реклама по атрибуции + внешняя реклама.
    -- Хранение по SKU в истории не представлено (факт только итогом магазина) — вся сумма идёт мостом.
    revenue_rub - wb_take_rub - logistics_rub - management_tax_rub - cogs_rub - ads_attributed_rub + external_marketing_rub sku_contribution_rub,
    ads_attributed_rub - ads_billing_rub ads_adjustment_rub,
    0 - storage_finance_rub storage_adjustment_rub,
    minimum_payment_rub + utilization_rub + penalty_rub + transit_rub + acceptance_rub + other_marketplace_cost_rub marketplace_costs_rub,
    -- Утрата товара: списание (расход месяца события) и компенсация WB (доход месяца проводки) — отдельными строками.
    wb_loss_compensation_rub - inventory_loss_writeoff_rub force_majeure_net_effect_rub
  FROM j)
SELECT
  FORMAT_DATE('%Y-%m', m) month, m service_month, 'HISTORICAL_CANONICAL' authority,
  revenue_rub, sku_contribution_rub, management_tax_rub,
  minimum_payment_rub, utilization_rub, penalty_rub, transit_rub, acceptance_rub, other_marketplace_cost_rub, marketplace_costs_rub,
  marketplace_income_rub, inventory_loss_writeoff_rub, wb_loss_compensation_rub, force_majeure_net_effect_rub,
  ads_adjustment_rub, storage_adjustment_rub, 0 commission_adjustment_rub, 0 price_adjustment_rub,
  ads_adjustment_rub + storage_adjustment_rub reconciliation_adjustments_rub,
  0 mature_cohort_adjustment_rub, 0 timing_bridge_logistics_rub, 0 timing_bridge_unsettled_margin_rub,
  sku_contribution_rub + ads_adjustment_rub + storage_adjustment_rub - marketplace_costs_rub + marketplace_income_rub
    - inventory_loss_writeoff_rub + wb_loss_compensation_rub management_net_store_profit_rub,
  SAFE_DIVIDE(sku_contribution_rub + ads_adjustment_rub + storage_adjustment_rub - marketplace_costs_rub + marketplace_income_rub
    - inventory_loss_writeoff_rub + wb_loss_compensation_rub, revenue_rub) management_net_store_margin,
  CASE WHEN unknown_rub > 0.5 OR unconsumed_finance_rub > 0.5 OR cogs_missing_qty > 0
         OR NOT external_marketing_covered OR cohort_join_gap > 0.005
         -- утрата: каждая единица с доказанной себестоимостью, ни одного заказа со склада после события
         OR inventory_loss_cogs_missing_qty > 0 OR inventory_loss_post_incident_orders > 0 THEN 'UNKNOWN_COST_PRESENT'
       WHEN pending_rows > 0 THEN 'PENDING_CLASSIFICATION'
       WHEN NOT month_finance_final OR ads_days = 0 OR ads_final_days < ads_days OR provisional_rows > 0 THEN 'PARTIAL_AWAITING_ACCOUNT_INVOICE'
       WHEN NOT account_invoice_window_closed AND (minimum_payment_invoices = 0 OR utilization_invoices = 0) THEN 'PARTIAL_AWAITING_ACCOUNT_INVOICE'
       WHEN NOT cohort_mature THEN 'FINANCIAL_COMPLETE_WITH_TIMING_BRIDGE'
       ELSE 'FINANCIAL_COMPLETE' END financial_state,
  -- Сохранение денег: та же прибыль из первичных величин без промежуточных колонок; остаток должен быть 0.
  (sku_contribution_rub + ads_adjustment_rub + storage_adjustment_rub - marketplace_costs_rub + marketplace_income_rub
   - inventory_loss_writeoff_rub + wb_loss_compensation_rub)
  - (revenue_rub - wb_take_rub - logistics_rub - management_tax_rub - cogs_rub - ads_billing_rub - storage_finance_rub
     + external_marketing_rub - marketplace_costs_rub + marketplace_income_rub
     - inventory_loss_writeoff_rub + wb_loss_compensation_rub) double_count_residual_rub,
  -- раскрытие
  wb_take_rub, logistics_rub, sold_qty, cogs_rub, cogs_missing_qty, ads_billing_rub, ads_attributed_rub,
  ads_final_days, ads_days, storage_finance_rub, external_marketing_rub, external_marketing_covered, cohort_join_gap, unknown_rub,
  inventory_loss_qty, inventory_loss_cogs_missing_qty, inventory_loss_post_incident_orders, inventory_loss_incident_list,
  wb_loss_compensation_dates, unconsumed_finance_rub, pending_rub, pending_rows,
  overridden_rows, month_finance_final, cohort_mature, TRUE sku_month_closed, account_invoice_window_closed,
  minimum_payment_invoices, utilization_invoices, 0 orphan_finance_base_rub, 0 orphan_finance_logistics_rub,
  0 deduction_source_gap_rub
FROM k;

-- ─── H3. Единая последовательность для представления: одна власть на месяц ─────────────────────────────
-- Исторический слой — месяцы [2026-05, 2026-08), CURRENT_PHASE_C — с 2026-08 (V_WB_STORE_PNL_MONTHLY, нижняя граница
-- там же). Пересечения нет по построению границ; QA SINGLE_AUTHORITY проверяет это на живых данных.
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_STORE_PNL_PRESENTATION` AS
SELECT month, service_month, authority, revenue_rub, sku_contribution_rub, management_tax_rub, minimum_payment_rub, utilization_rub, penalty_rub, transit_rub,
  acceptance_rub, other_marketplace_cost_rub, marketplace_costs_rub, marketplace_income_rub,
  inventory_loss_writeoff_rub, wb_loss_compensation_rub, force_majeure_net_effect_rub, ads_adjustment_rub, storage_adjustment_rub,
  commission_adjustment_rub, price_adjustment_rub, reconciliation_adjustments_rub, mature_cohort_adjustment_rub,
  timing_bridge_logistics_rub, timing_bridge_unsettled_margin_rub, management_net_store_profit_rub, management_net_store_margin,
  financial_state, double_count_residual_rub, pending_rows, unknown_rub, unconsumed_finance_rub, deduction_source_gap_rub,
  orphan_finance_base_rub, orphan_finance_logistics_rub, sku_month_closed, month_finance_final, ads_days, ads_final_days,
  account_invoice_window_closed, minimum_payment_invoices, utilization_invoices,
  inventory_loss_qty, inventory_loss_cogs_missing_qty, inventory_loss_post_incident_orders, inventory_loss_incident_list,
  wb_loss_compensation_dates
FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_STORE_PNL_HISTORICAL`
UNION ALL
SELECT month, service_month, 'CURRENT_PHASE_C' authority, revenue_rub, sku_contribution_rub, management_tax_rub, minimum_payment_rub, utilization_rub, penalty_rub, transit_rub,
  acceptance_rub, other_marketplace_cost_rub, marketplace_costs_rub, marketplace_income_rub,
  inventory_loss_writeoff_rub, wb_loss_compensation_rub, force_majeure_net_effect_rub, ads_adjustment_rub, storage_adjustment_rub,
  commission_adjustment_rub, price_adjustment_rub, reconciliation_adjustments_rub, mature_cohort_adjustment_rub,
  timing_bridge_logistics_rub, timing_bridge_unsettled_margin_rub, management_net_store_profit_rub, management_net_store_margin,
  financial_state, double_count_residual_rub, pending_rows, unknown_rub, unconsumed_finance_rub, deduction_source_gap_rub,
  orphan_finance_base_rub, orphan_finance_logistics_rub, sku_month_closed, month_finance_final, ads_days, ads_final_days,
  account_invoice_window_closed, minimum_payment_invoices, utilization_invoices,
  inventory_loss_qty, inventory_loss_cogs_missing_qty, inventory_loss_post_incident_orders, inventory_loss_incident_list,
  wb_loss_compensation_dates
FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_STORE_PNL_MONTHLY`;
