-- ROLLBACK (H0): тело production wb_mart.V_WB_STORE_PNL_MONTHLY ДО изменения H0, снято tables.get 2026-10-11 (Phase C, main 8753337).
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_STORE_PNL_MONTHLY` AS
WITH snap AS (
  -- Окно Phase C — с 2026-08 (решение ACK: сентябрь, октябрь, август для сравнения). Раньше: наследие формул
  -- листа, оценочное хранение, LEGACY-слой финотчёта и неполный биллинг рекламы — такие месяцы P&L не считает.
  SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_ops.UNITKA_SKU_COMPONENTS_DAILY`
  WHERE snapshot_id = (SELECT MAX(snapshot_id) FROM `project-fa311fc0-4d87-4781-986.wb_ops.UNITKA_SKU_COMPONENTS_DAILY`)
    AND date_msk >= DATE '2026-08-01'),
sku_day AS (
  SELECT s.date_msk, s.nm_id, s.orders, s.cancels, s.orders - s.cancels realized, s.price, s.commission_rate,
    s.logistics_per_unit, s.reverse_leg_rate, s.storage, s.ads_in, s.ads_out, s.profit, s.lcd, s.model_gap, s.tax_per_unit, s.cogs_per_unit,
    c.sold_qty, c.seller_base_rub, c.wb_take_rub, c.logistics_rub fin_logistics_rub
  FROM snap s LEFT JOIN `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_STORE_FINANCE_COHORT_DAILY` c
    ON c.order_date_msk = s.date_msk AND c.nm_id = s.nm_id),
sku AS (
  SELECT DATE_TRUNC(date_msk, MONTH) m, MAX(date_msk) last_day,
    SUM(realized * price) revenue_model_rub,
    SUM(profit) sku_contribution_rub,
    SUM(ads_in) ads_sku_rub, SUM(storage) storage_sku_rub,
    SUM(orders * logistics_per_unit + cancels * reverse_leg_rate) logistics_sku_rub,
    SUM(IFNULL(sold_qty, 0)) sold_qty, SUM(realized) realized_qty,
    SUM(IFNULL(seller_base_rub, 0) - IFNULL(sold_qty, 0) * price) price_adjustment_rub,
    SUM(IFNULL(sold_qty, 0) * price * commission_rate - IFNULL(wb_take_rub, 0)) commission_adjustment_rub,
    SUM(GREATEST(realized - IFNULL(sold_qty, 0), 0)) unsettled_qty,
    SUM(GREATEST(IFNULL(sold_qty, 0) - realized, 0)) oversold_qty,
    SUM(GREATEST(realized - IFNULL(sold_qty, 0), 0) * price) unsettled_revenue_rub,
    -- ожидаемый вклад единиц когорты, так и не ставших продажей (без логистики: её факт — в логистике когорты).
    -- БЕЗ отсечения по нулю: сутки, где продано больше, чем лист насчитал выкупов (заказ, потерянный Orders API),
    -- вычитают отрицательный «непроданный» вклад, то есть добавляют вклад лишних продаж. Так тождество
    -- «сверху вниз» (ниже, double_count_residual_rub) точное.
    SUM((realized - IFNULL(sold_qty, 0)) * (price - price * commission_rate - tax_per_unit - cogs_per_unit)) unsettled_margin_rub,
    -- для тождества «сверху вниз»: финансы когорты и управленческие налог/COGS проданных единиц
    SUM(IFNULL(seller_base_rub, 0)) seller_base_rub, SUM(IFNULL(wb_take_rub, 0)) wb_take_rub,
    SUM(IFNULL(sold_qty, 0) * (tax_per_unit + cogs_per_unit)) sold_tax_cogs_rub,
    SUM(ads_out) ads_out_rub, SUM(model_gap) model_gap_rub, MAX(lcd) snapshot_lcd,
    SUM(IFNULL(fin_logistics_rub, 0)) logistics_finance_rub,
    SUM(ABS(model_gap)) model_gap_abs_rub
  FROM sku_day GROUP BY 1),
fin_extra AS (
  -- финансы когорты на сутки × nm, которых нет в снимке листа (SKU вне блоков) — должны быть 0
  SELECT DATE_TRUNC(c.order_date_msk, MONTH) m, SUM(c.seller_base_rub) orphan_base_rub, SUM(c.logistics_rub) orphan_logistics_rub
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_STORE_FINANCE_COHORT_DAILY` c
  LEFT JOIN snap s ON s.date_msk = c.order_date_msk AND s.nm_id = c.nm_id
  WHERE s.nm_id IS NULL AND c.order_date_msk <= (SELECT MAX(lcd) FROM snap)
  GROUP BY 1),
fin_status AS (
  SELECT MAX(IF(finance_status = 'FINAL', _rr_date, NULL)) final_through, MAX(_rr_date) any_through
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FINANCE_CANONICAL`),
-- Полнота финотчёта — по ПОКРЫТИЮ суток периодами FINAL-отчётов, а не по MAX(даты): пропавшая неделя видна.
fin_days AS (
  SELECT DISTINCT d
  FROM (SELECT DISTINCT SAFE_CAST(report_period_from AS DATE) pf, SAFE_CAST(report_period_to AS DATE) pt
        FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FINANCE_CANONICAL` WHERE finance_status = 'FINAL'),
    UNNEST(GENERATE_DATE_ARRAY(pf, pt)) d
  WHERE pf IS NOT NULL AND pt IS NOT NULL AND pt >= pf),
cov AS (
  SELECT s.m,
    COUNTIF(fd.d BETWEEN s.m AND LAST_DAY(s.m)) = DATE_DIFF(LAST_DAY(s.m), s.m, DAY) + 1 final_month,
    COUNTIF(fd.d BETWEEN s.m AND DATE_ADD(LAST_DAY(s.m), INTERVAL 20 DAY)) = DATE_DIFF(LAST_DAY(s.m), s.m, DAY) + 21 final_m20,
    COUNTIF(fd.d BETWEEN s.m AND DATE_ADD(LAST_DAY(s.m), INTERVAL 46 DAY)) = DATE_DIFF(LAST_DAY(s.m), s.m, DAY) + 47 final_m46
  FROM (SELECT DISTINCT DATE_TRUNC(date_msk, MONTH) m FROM snap) s CROSS JOIN fin_days fd
  GROUP BY 1),
-- Деньги финотчёта, которые модель не читает, и неразобранные операции — в окне месяца и 20 суток после
-- (счета и сторно за месяц приходят в следующем): иначе операция, проведённая 12-го, блокировала бы не тот месяц.
uncovered AS (
  -- деньги PENDING/NEW-операций — это PENDING_CLASSIFICATION (разбор), а не «неизвестный расход»
  SELECT s.m, SUM(IF(c.treatment IN ('NEW_FINANCE_OPERATION', 'PENDING_CLASSIFICATION'), 0, c.unconsumed_abs_rub)) unconsumed_finance_rub,
    COUNTIF(c.treatment IN ('NEW_FINANCE_OPERATION', 'PENDING_CLASSIFICATION')) pending_rows
  FROM (SELECT DISTINCT DATE_TRUNC(date_msk, MONTH) m FROM snap) s
  JOIN `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_STORE_FINANCE_COVERAGE` c
    ON c.booking_date BETWEEN s.m AND DATE_ADD(LAST_DAY(s.m), INTERVAL 20 DAY)
  GROUP BY 1),
-- Удержания: классификатор (FACT_FINANCE) против финотчёта (canonical) — суммы по месяцу проводки должны совпасть,
-- иначе источник счетов кабинета отстал или разошёлся (месяц и следующий за ним).
ded_rec AS (
  SELECT m, ABS(IFNULL(canon, 0) - IFNULL(cls, 0)) gap
  FROM (SELECT DATE_TRUNC(_rr_date, MONTH) m, SUM(IFNULL(SAFE_CAST(deduction AS NUMERIC), 0) + IFNULL(SAFE_CAST(additional_payment AS NUMERIC), 0)) canon
        FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FINANCE_CANONICAL` WHERE supplier_oper_name = 'Удержание' GROUP BY 1)
  FULL JOIN (SELECT DATE_TRUNC(finance_date, MONTH) m, SUM(deduction_amount_rub) cls
        FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_DEDUCTIONS_CLASSIFIED` GROUP BY 1) USING (m)),
ded_gap AS (
  SELECT s.m, SUM(r.gap) deduction_source_gap_rub
  FROM (SELECT DISTINCT DATE_TRUNC(date_msk, MONTH) m FROM snap) s
  JOIN ded_rec r ON r.m IN (s.m, DATE_ADD(s.m, INTERVAL 1 MONTH)) GROUP BY 1),
storage_fin AS (
  SELECT DATE_TRUNC(_rr_date, MONTH) m, SUM(IFNULL(SAFE_CAST(storage_fee AS NUMERIC), 0)) storage_finance_rub
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FINANCE_CANONICAL`
  WHERE supplier_oper_name IN ('Хранение', 'Коррекция хранения') GROUP BY 1),
ads AS (
  SELECT DATE_TRUNC(day, MONTH) m, SUM(ad_spend_financial_rub) ads_billing_rub,
    COUNTIF(ads_billing_is_final) ads_final_days, COUNT(*) ads_days
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_FINANCE_CORRECTED_DAILY` GROUP BY 1),
acc AS (
  SELECT service_month m,
    SUM(IF(category = 'MINIMUM_PAYMENT', amount_rub, 0)) minimum_payment_rub,
    SUM(IF(category = 'UTILIZATION', amount_rub, 0)) utilization_rub,
    SUM(IF(category = 'PENALTY', amount_rub, 0)) penalty_rub,
    SUM(IF(category = 'TRANSIT', amount_rub, 0)) transit_rub,
    SUM(IF(category = 'ACCEPTANCE', amount_rub, 0)) acceptance_rub,
    SUM(IF(treatment = 'ACCOUNT_LEVEL' AND category NOT IN ('MINIMUM_PAYMENT', 'UTILIZATION', 'PENALTY', 'TRANSIT', 'ACCEPTANCE'), amount_rub, 0)) other_marketplace_cost_rub,
    -SUM(IF(treatment = 'ACCOUNT_LEVEL_INCOME', amount_rub, 0)) marketplace_income_rub,
    SUM(IF(treatment = 'UNKNOWN', ABS(amount_rub), 0)) unknown_rub,
    COUNTIF(category = 'MINIMUM_PAYMENT') minimum_payment_invoices,
    COUNTIF(category = 'UTILIZATION') utilization_invoices,
    -- проведено позже окна (месяц + 20 суток): месяц, уже вышедший из PARTIAL, пересчитан этим счётом
    COUNTIF(booking_date > DATE_ADD(LAST_DAY(service_month), INTERVAL 20 DAY)) late_account_bookings,
    SUM(IF(booking_date > DATE_ADD(LAST_DAY(service_month), INTERVAL 20 DAY), amount_rub, 0)) late_account_rub,
    STRING_AGG(DISTINCT CONCAT(category, ':', CAST(booking_date AS STRING)), ', ') account_bookings
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_STORE_ACCOUNT_LEDGER` GROUP BY 1),
memo AS (
  SELECT DATE_TRUNC(_rr_date, MONTH) m,
    SUM(IFNULL(SAFE_CAST(rebill_logistics AS NUMERIC), 0)) + SUM(IFNULL(SAFE_CAST(JSON_VALUE(raw_json, '$.ppvzReward') AS NUMERIC), 0)) memo_reimbursement_rub
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FINANCE_CANONICAL`
  WHERE supplier_oper_name LIKE 'Возмещение%' GROUP BY 1),
j AS (
  SELECT sku.*, fs.final_through, fs.any_through,
    LAST_DAY(sku.m) month_end,
    IFNULL(sf.storage_finance_rub, 0) storage_finance_rub,
    IFNULL(ads.ads_billing_rub, 0) ads_billing_rub, IFNULL(ads.ads_final_days, 0) ads_final_days, IFNULL(ads.ads_days, 0) ads_days,
    IFNULL(acc.minimum_payment_rub, 0) minimum_payment_rub, IFNULL(acc.utilization_rub, 0) utilization_rub,
    IFNULL(acc.penalty_rub, 0) penalty_rub, IFNULL(acc.transit_rub, 0) transit_rub, IFNULL(acc.acceptance_rub, 0) acceptance_rub,
    IFNULL(acc.other_marketplace_cost_rub, 0) other_marketplace_cost_rub, IFNULL(acc.marketplace_income_rub, 0) marketplace_income_rub,
    IFNULL(acc.unknown_rub, 0) unknown_rub, IFNULL(acc.minimum_payment_invoices, 0) minimum_payment_invoices,
    IFNULL(acc.utilization_invoices, 0) utilization_invoices, acc.account_bookings,
    IFNULL(acc.late_account_bookings, 0) late_account_bookings, IFNULL(acc.late_account_rub, 0) late_account_rub,
    IFNULL(memo.memo_reimbursement_rub, 0) memo_reimbursement_rub, IFNULL(u.pending_rows, 0) pending_rows,
    IFNULL(u.unconsumed_finance_rub, 0) unconsumed_finance_rub, IFNULL(dg.deduction_source_gap_rub, 0) deduction_source_gap_rub,
    IFNULL(cov.final_month, FALSE) final_month, IFNULL(cov.final_m20, FALSE) final_m20, IFNULL(cov.final_m46, FALSE) final_m46,
    IFNULL(fe.orphan_base_rub, 0) orphan_finance_base_rub, IFNULL(fe.orphan_logistics_rub, 0) orphan_finance_logistics_rub
  FROM sku CROSS JOIN fin_status fs
  LEFT JOIN storage_fin sf ON sf.m = sku.m LEFT JOIN ads ON ads.m = sku.m LEFT JOIN acc ON acc.m = sku.m
  LEFT JOIN memo ON memo.m = sku.m LEFT JOIN uncovered u ON u.m = sku.m LEFT JOIN fin_extra fe ON fe.m = sku.m
  LEFT JOIN cov ON cov.m = sku.m LEFT JOIN ded_gap dg ON dg.m = sku.m),
k AS (
  SELECT j.*,
    j.ads_sku_rub - j.ads_billing_rub ads_adjustment_rub,
    j.storage_sku_rub - j.storage_finance_rub storage_adjustment_rub,
    j.logistics_sku_rub - j.logistics_finance_rub logistics_adjustment_rub,
    -- Все флаги — по ДАННЫМ (покрытие суток FINAL-отчётами), не по календарю, и NULL = FALSE (не «закрыт»):
    -- когорта созрела — FINAL покрывает месяц + 46 суток (срок второго плеча отказа),
    j.final_m46 cohort_mature,
    -- окно счетов кабинета закрыто — FINAL покрывает месяц + 20 суток (мин. платёж и утилизация проводятся ~10–13-го),
    j.final_m20 account_invoice_window_closed,
    j.final_month month_finance_final,
    IFNULL(j.snapshot_lcd >= j.month_end, FALSE) sku_month_closed
  FROM j),
n AS (
  SELECT k.*,
    minimum_payment_rub + utilization_rub + penalty_rub + transit_rub + acceptance_rub + other_marketplace_cost_rub marketplace_costs_rub,
    ads_adjustment_rub + storage_adjustment_rub + commission_adjustment_rub + price_adjustment_rub reconciliation_adjustments_rub,
    IF(cohort_mature, logistics_adjustment_rub - unsettled_margin_rub, 0) mature_cohort_adjustment_rub
  FROM k)
SELECT
  FORMAT_DATE('%Y-%m', m) month, m service_month, last_day sku_last_day,
  -- Выручка: до созревания — ожидаемая (выкупы листа по цене продавца, поправленной на факт); после —
  -- база финотчёта WB по проданным единицам когорты (непроданные единицы сняты и из прибыли).
  IF(cohort_mature, seller_base_rub, revenue_model_rub + price_adjustment_rub) revenue_rub,
  sku_contribution_rub,
  minimum_payment_rub, utilization_rub, penalty_rub, transit_rub, acceptance_rub, other_marketplace_cost_rub,
  marketplace_costs_rub, marketplace_income_rub,
  ads_adjustment_rub, storage_adjustment_rub, commission_adjustment_rub, price_adjustment_rub, reconciliation_adjustments_rub,
  -- Логистика и непроданные единицы: пока когорта НЕ созрела — только раскрытие (timing bridge), в прибыль
  -- не входит: разница там — ещё не выставленные плечи и ещё не проданные единицы, а не экономия. После
  -- созревания — остаток (логистика когорты по факту, ожидаемый вклад непроданных единиц снимается).
  mature_cohort_adjustment_rub,
  IF(cohort_mature, 0, logistics_adjustment_rub) timing_bridge_logistics_rub,
  IF(cohort_mature, 0, unsettled_margin_rub) timing_bridge_unsettled_margin_rub,
  sku_contribution_rub + reconciliation_adjustments_rub + mature_cohort_adjustment_rub - marketplace_costs_rub
    + marketplace_income_rub management_net_store_profit_rub,
  SAFE_DIVIDE(sku_contribution_rub + reconciliation_adjustments_rub + mature_cohort_adjustment_rub - marketplace_costs_rub
    + marketplace_income_rub, IF(cohort_mature, seller_base_rub, revenue_model_rub + price_adjustment_rub)) management_net_store_margin,
  -- Август 2026 — только сравнение (OWNER ACK 09.10): известные дефекты Юнитки (отказы, оценочное хранение, формулы)
  -- Phase C не чинит, поэтому месяц не может выглядеть закрытым, сумма — справочная.
  CASE WHEN m < DATE '2026-09-01' THEN 'LEGACY_PARTIAL_KNOWN_DEFECTS'
       WHEN unknown_rub > 0.5 OR orphan_finance_base_rub != 0 OR orphan_finance_logistics_rub != 0
         OR unconsumed_finance_rub > 0.5 THEN 'UNKNOWN_COST_PRESENT'
       WHEN pending_rows > 0 THEN 'PENDING_CLASSIFICATION'
       -- месяц ещё идёт в листе (или снимок отстал) — счета за него заведомо впереди
       WHEN NOT sku_month_closed THEN 'PARTIAL_AWAITING_ACCOUNT_INVOICE'
       WHEN NOT account_invoice_window_closed AND (minimum_payment_invoices = 0 OR utilization_invoices = 0)
         THEN 'PARTIAL_AWAITING_ACCOUNT_INVOICE'
       WHEN NOT month_finance_final OR ads_days = 0 OR ads_final_days < ads_days THEN 'PARTIAL_AWAITING_ACCOUNT_INVOICE'
       -- источник счетов кабинета (FACT_FINANCE) отстал от финотчёта: ждём, а не «неизвестный расход»
       WHEN deduction_source_gap_rub > 0.5 THEN 'PARTIAL_AWAITING_ACCOUNT_INVOICE'
       WHEN NOT cohort_mature THEN 'FINANCIAL_COMPLETE_WITH_TIMING_BRIDGE'
       ELSE 'FINANCIAL_COMPLETE' END financial_state,
  -- Тождество ФОРМЫ (C2): та же прибыль, собранная сверху вниз (финотчёт когорты, биллинг рекламы, налог/COGS проданных
  -- единиц, разрыв модели листа). Алгебраически равна полной прибыли (чистая + мост сроков) — это проверка, что ни
  -- одна поправка не вошла в формулу дважды и не выпала при правке вью, а НЕ проверка полноты данных. Полноту данных
  -- доказывают: покрытие финотчёта (unconsumed_finance_rub), сверка источников удержаний (deduction_source_gap_rub),
  -- финансы без SKU листа (orphan_*) и сверка снимка с итоговой колонкой листа (загрузчик, parse.ts).
  seller_base_rub - wb_take_rub - logistics_finance_rub - storage_finance_rub - ads_billing_rub - sold_tax_cogs_rub
    + ads_out_rub + model_gap_rub - marketplace_costs_rub + marketplace_income_rub topdown_net_rub,
  (sku_contribution_rub + reconciliation_adjustments_rub + logistics_adjustment_rub - unsettled_margin_rub
    - marketplace_costs_rub + marketplace_income_rub)
  - (seller_base_rub - wb_take_rub - logistics_finance_rub - storage_finance_rub - ads_billing_rub - sold_tax_cogs_rub
    + ads_out_rub + model_gap_rub - marketplace_costs_rub + marketplace_income_rub) double_count_residual_rub,
  -- раскрытие и QA
  revenue_model_rub, seller_base_rub, wb_take_rub, sold_tax_cogs_rub, ads_out_rub, model_gap_rub,
  ads_sku_rub, ads_billing_rub, ads_final_days, ads_days,
  storage_sku_rub, storage_finance_rub, logistics_sku_rub, logistics_finance_rub,
  logistics_adjustment_rub, realized_qty, sold_qty, unsettled_qty, oversold_qty, unsettled_revenue_rub, unsettled_margin_rub,
  memo_reimbursement_rub, unknown_rub, pending_rows, unconsumed_finance_rub, deduction_source_gap_rub,
  orphan_finance_base_rub, orphan_finance_logistics_rub,
  minimum_payment_invoices, utilization_invoices, account_bookings, late_account_bookings, late_account_rub,
  cohort_mature, account_invoice_window_closed, month_finance_final, sku_month_closed, final_through, snapshot_lcd,
  model_gap_abs_rub,
  (SELECT MAX(snapshot_id) FROM `project-fa311fc0-4d87-4781-986.wb_ops.UNITKA_SKU_COMPONENTS_DAILY`) snapshot_id
FROM n;
