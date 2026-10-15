-- ============================================================================
-- CANONICAL CURRENT DEFINITION — ozon_mart.V_OZON_STORE_MONTH_STATE (VIEW)
-- Git-first (sync_state pending_deploy): в production объекта ещё нет. Rules: sql/current/README.md.
-- D3: финансовое состояние месяца P&L магазина Ozon. Все входы ворот видны колонками; состояние —
-- первое сработавшее правило. CURRENT_DATE не используется: время задаёт водяной знак финансов —
-- по успешным загрузкам finance_accrual (OZON_INGESTION_RUNS status OK, без кодов контроля
-- OZON_FINANCE_* в error_message) наибольшее из LEAST(source_to, сутки МСК завершения − 1). Нет
-- водяного знака — ни один месяц не закрыт (отказ в сторону «не окончательно»).
-- Закрытие месяца: водяной знак ≥ последний день месяца + 36 суток (D0: к +20 приходит ≥ 99,63 %).
-- Позднее начисление переоткрывает суммы месяца сами (VIEW).
--   1 PENDING_CLASSIFICATION — есть строки V_OZON_FINANCE_NEW_OPERATIONS (публикация запрещена);
--   2 PARTIAL_SOURCE_MISSING — действующий известный дефект источника (known_defects); загрузка финансов
--     с кодом контроля OZON_FINANCE_* (карантин начисления / несошедшиеся деньги, D1) в окне месяца;
--     деньги без колонки или без отправления; COGS 0 или >1 интервала; SKU вне карты; сильный признак
--     незагруженного сторно реализации (у проданного отправления есть сбор за возврат 59/45/9, а строки
--     сторно нет); у провизорной оценки нет входа (COGS, комиссия, логистика); после закрытия —
--     доставлено без начисления / СНГ без документа / единицы в пути без начисления;
--   3 UNPROVEN — в месяце есть строки ремонта D1 (ingestion_run_id ozd1-repair-*) или слабый признак
--     сторно (у проданного отправления только возврат эквайринга — так выглядит и частичная
--     компенсация): деньги не опровергнуты, но независимая сверка D0.5 ещё не записана;
--   4 PARTIAL_AWAITING_ACCRUALS — месяц не закрыт и есть доставленные без начисления или единицы в пути;
--   5 FINANCIAL_COMPLETE_WITH_TIMING_BRIDGE — месяц не закрыт, остальные ворота PASS;
--   6 FINANCIAL_COMPLETE.
-- provisional_open_rub (X) — оценка вклада доставленных без начисления: выручка (цена отправления × кол-во;
-- для выкупа СНГ без документа — цена × минимальная доля выручки по документам СНГ) − комиссия
-- (цена × кол-во − выплата отправления; совпадает с комиссией блока начисления на всех доставленных
-- отправлениях; у выкупа СНГ комиссии нет) − средняя логистика SKU на единицу по начислениям − COGS −
-- налог 2 %. Вход неизвестен → не ноль, а состояние PARTIAL_SOURCE_MISSING. Оценка помечена PROVISIONAL
-- и окончательной прибылью не является.
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_STORE_MONTH_STATE`
OPTIONS (description = "Финансовое состояние месяца P&L магазина Ozon (D3), зерно = календарный месяц P&L. financial_state - первое сработавшее: PENDING_CLASSIFICATION, PARTIAL_SOURCE_MISSING, UNPROVEN, PARTIAL_AWAITING_ACCRUALS, FINANCIAL_COMPLETE_WITH_TIMING_BRIDGE, FINANCIAL_COMPLETE. Входы ворот - отдельными колонками. Время - водяной знак finance_accrual из OZON_INGESTION_RUNS (успешные загрузки без кодов контроля OZON_FINANCE_*, без CURRENT_DATE); месяц закрыт при водяном знаке >= конец месяца + 36 суток; нет водяного знака - месяц не закрыт. provisional_open_rub - PROVISIONAL-оценка вклада доставленных без начисления, не окончательная прибыль.")
AS
WITH
led AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_STORE_ACCOUNT_LEDGER`),
-- Известные дефекты источника, которые RAW сам не показывает. Строка удаляется только PR вместе с
-- доказательством исправления. clears_when_d1_repair_loaded = TRUE: дефект снимается сам, когда в месяце
-- появились строки ремонта D1 (месяц переходит в UNPROVEN до записи сверки D0.5).
--   D1_REVERSAL_NOT_LOADED — месяцы заказа сторно реализации, не загруженных до D1 (план ремонта D1, PR #339).
--   M2_SALE_AMOUNT_REPRESENTATION — RAW хранит seller_price × количество, а не sale_amount (отдельный PR
--   загрузчика + перезагрузка, OWNER DIRECTIVE 2026-10-14 §3).
known_defects AS (
  SELECT * FROM UNNEST([
    STRUCT(DATE '2025-06-01' AS m, 'D1_REVERSAL_NOT_LOADED' AS defect_code, TRUE AS clears_when_d1_repair_loaded),
    (DATE '2025-07-01', 'D1_REVERSAL_NOT_LOADED', TRUE),
    (DATE '2025-08-01', 'D1_REVERSAL_NOT_LOADED', TRUE),
    (DATE '2025-11-01', 'D1_REVERSAL_NOT_LOADED', TRUE),
    (DATE '2026-01-01', 'D1_REVERSAL_NOT_LOADED', TRUE),
    (DATE '2026-03-01', 'D1_REVERSAL_NOT_LOADED', TRUE),
    (DATE '2026-04-01', 'D1_REVERSAL_NOT_LOADED', TRUE),
    (DATE '2026-05-01', 'D1_REVERSAL_NOT_LOADED', TRUE),
    (DATE '2026-07-01', 'D1_REVERSAL_NOT_LOADED', TRUE),
    (DATE '2026-08-01', 'M2_SALE_AMOUNT_REPRESENTATION', FALSE)
  ])),
runs AS (
  SELECT SAFE.PARSE_DATE('%Y-%m-%d', source_from) source_from, SAFE.PARSE_DATE('%Y-%m-%d', source_to) source_to,
    completed_at, REGEXP_CONTAINS(IFNULL(error_message, ''), r'OZON_FINANCE_') finance_control_event
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.OZON_INGESTION_RUNS`
  WHERE entity = 'finance_accrual' AND status = 'OK'),
wm AS (
  SELECT MAX(LEAST(source_to, DATE_SUB(DATE(completed_at, 'Europe/Moscow'), INTERVAL 1 DAY))) finance_watermark_date
  FROM runs WHERE NOT finance_control_event),
ctrl AS (
  SELECT DATE_TRUNC(d, MONTH) m, COUNT(DISTINCT completed_at) finance_control_runs
  FROM runs, UNNEST(GENERATE_DATE_ARRAY(source_from, source_to)) d
  WHERE finance_control_event AND source_from <= source_to
  GROUP BY 1),
fresh AS (
  SELECT ANY_VALUE(freshness_status) source_freshness_state
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_MART_FRESHNESS`),
cogs AS (
  SELECT internal_sku, effective_from, IFNULL(effective_to, DATE '9999-12-31') effective_to, product_cogs_rub
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.V_PRODUCT_COGS_EFFECTIVE`),
sale_units AS (
  SELECT l.ledger_key, l.pnl_month, l.internal_sku, l.quantity, COUNT(c.internal_sku) intervals
  FROM led l
  LEFT JOIN cogs c ON c.internal_sku = l.internal_sku
                  AND l.order_date_msk BETWEEN c.effective_from AND c.effective_to
  WHERE l.econ_direction = 'SALE' OR l.source = 'CIS_DOCUMENT'
  GROUP BY 1, 2, 3, 4),
pk AS (
  SELECT posting_number, sku, MIN(DATE(created_at, 'Europe/Moscow')) od, MAX(quantity) q,
    MAX(status) status, MAX(price_rub) price_rub, MAX(payout_rub) payout_rub
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_POSTINGS_FBO`
  GROUP BY 1, 2),
econ AS (
  SELECT DISTINCT posting_number, sku FROM led WHERE is_econ_block OR source = 'CIS_DOCUMENT'),
skumap AS (
  SELECT marketplace_sku, MIN(internal_sku) internal_sku, COUNT(DISTINCT internal_sku) n
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP`
  WHERE marketplace = 'OZON' GROUP BY 1),
cis_ratio AS (
  SELECT MIN(SAFE_DIVIDE(buyout_proceeds_rub, reference_price_rub)) r
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_CIS_BUYOUT`),
logi AS (
  SELECT internal_sku, SAFE_DIVIDE(-SUM(IF(pnl_column IN ('E', 'F'), amount_rub, 0)),
                                   NULLIF(SUM(IF(econ_direction = 'SALE', quantity, 0)), 0)) per_unit
  FROM led WHERE internal_sku IS NOT NULL AND posting_number IS NOT NULL
  GROUP BY 1),
logi_all AS (
  SELECT SAFE_DIVIDE(-SUM(IF(pnl_column IN ('E', 'F'), amount_rub, 0)),
                     NULLIF(SUM(IF(econ_direction = 'SALE', quantity, 0)), 0)) per_unit
  FROM led WHERE posting_number IS NOT NULL),
prov AS (
  SELECT DATE_TRUNC(p.od, MONTH) m, p.q,
    p.payout_rub = 0 is_cis_without_document,
    CASE WHEN p.payout_rub = 0 THEN p.price_rub * cr.r ELSE p.price_rub END * p.q revenue,
    CASE WHEN p.payout_rub = 0 THEN 0 ELSE p.price_rub * p.q - p.payout_rub END commission,
    CASE WHEN p.payout_rub = 0 THEN 0 ELSE IFNULL(lg.per_unit, la.per_unit) * p.q END logistics,
    c.product_cogs_rub * p.q cogs
  FROM pk p
  LEFT JOIN econ e USING (posting_number, sku)
  LEFT JOIN skumap s ON s.marketplace_sku = p.sku AND s.n = 1
  LEFT JOIN logi lg ON lg.internal_sku = s.internal_sku
  LEFT JOIN cogs c ON c.internal_sku = s.internal_sku AND p.od BETWEEN c.effective_from AND c.effective_to
  CROSS JOIN logi_all la
  CROSS JOIN cis_ratio cr
  WHERE p.status = 'delivered' AND e.posting_number IS NULL),
prov_m AS (
  SELECT m, SUM(q) provisional_units, COUNTIF(is_cis_without_document) cis_without_document_postings,
    SUM(revenue) provisional_revenue_rub,
    SUM(revenue - commission - logistics - cogs - revenue * 0.02) provisional_open_rub,
    COUNTIF(revenue IS NULL OR commission IS NULL OR logistics IS NULL OR cogs IS NULL) provisional_inputs_missing
  FROM prov GROUP BY 1),
transit AS (
  SELECT DATE_TRUNC(p.od, MONTH) m, SUM(p.q) in_transit_units,
    SUM(IF(e.posting_number IS NULL, p.q, 0)) in_transit_without_accrual_units
  FROM pk p LEFT JOIN econ e USING (posting_number, sku)
  WHERE p.status IN ('delivering', 'awaiting_deliver', 'awaiting_packaging')
  GROUP BY 1),
rev_sig AS (
  SELECT posting_number,
    COUNTIF(type_id IN (59, 45, 9)) return_fee_rows,
    COUNTIF(type_id = 1 AND amount_rub > 0) acquiring_refund_rows
  FROM led WHERE posting_number IS NOT NULL GROUP BY 1),
rev_econ AS (
  SELECT posting_number, ANY_VALUE(pnl_month) m,
    COUNTIF(econ_direction = 'SALE') sales, COUNTIF(econ_direction = 'REVERSAL') reversals
  FROM led WHERE is_econ_block GROUP BY 1),
rev_susp AS (
  SELECT e.m,
    COUNTIF(g.return_fee_rows > 0) reversal_suspected_postings,
    COUNTIF(g.return_fee_rows = 0 AND g.acquiring_refund_rows > 0) reversal_weak_signal_postings
  FROM rev_econ e JOIN rev_sig g USING (posting_number)
  WHERE e.sales > 0 AND e.reversals = 0
  GROUP BY 1),
gates AS (
  SELECT pnl_month m,
    SUM(IF(pnl_column IS NULL, money_rub, 0)) unconsumed_rub,
    SUM(IF(posting_link_required AND NOT posting_linked, money_rub, 0)) unlinked_posting_rub,
    COUNTIF(sku IS NOT NULL AND internal_sku IS NULL AND (is_econ_block OR source = 'CIS_DOCUMENT')) unmapped_sku_rows,
    COUNTIF(STARTS_WITH(IFNULL(ingestion_run_id, ''), 'ozd1-repair-')) d1_repair_rows
  FROM led GROUP BY 1),
kd AS (
  SELECT k.m, STRING_AGG(k.defect_code, ',' ORDER BY k.defect_code) known_source_defects
  FROM known_defects k LEFT JOIN gates g ON g.m = k.m
  WHERE NOT (k.clears_when_d1_repair_loaded AND IFNULL(g.d1_repair_rows, 0) > 0)
  GROUP BY 1),
cogs_m AS (
  SELECT pnl_month m, SUM(IF(intervals != 1, quantity, 0)) cogs_gap_units
  FROM sale_units GROUP BY 1),
pend AS (
  SELECT pnl_month m, COUNT(*) pending_issue_rows
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_FINANCE_NEW_OPERATIONS`
  GROUP BY 1),
months AS (
  SELECT m FROM gates UNION DISTINCT SELECT m FROM prov_m UNION DISTINCT SELECT m FROM transit),
j AS (
  SELECT months.m pnl_month, wm.finance_watermark_date,
    DATE_ADD(LAST_DAY(months.m), INTERVAL 36 DAY) close_after_date,
    IFNULL(wm.finance_watermark_date >= DATE_ADD(LAST_DAY(months.m), INTERVAL 36 DAY), FALSE) is_closed,
    IFNULL(pend.pending_issue_rows, 0) pending_issue_rows,
    IFNULL(ct.finance_control_runs, 0) finance_control_runs,
    IFNULL(g.unconsumed_rub, 0) unconsumed_rub,
    IFNULL(g.unlinked_posting_rub, 0) unlinked_posting_rub,
    IFNULL(g.unmapped_sku_rows, 0) unmapped_sku_rows,
    IFNULL(cm.cogs_gap_units, 0) cogs_gap_units,
    IFNULL(rs.reversal_suspected_postings, 0) reversal_suspected_postings,
    IFNULL(rs.reversal_weak_signal_postings, 0) reversal_weak_signal_postings,
    IFNULL(g.d1_repair_rows, 0) d1_repair_rows,
    IFNULL(pm.provisional_units, 0) provisional_units,
    IFNULL(pm.cis_without_document_postings, 0) cis_without_document_postings,
    IFNULL(pm.provisional_revenue_rub, 0) provisional_revenue_rub,
    IFNULL(pm.provisional_open_rub, 0) provisional_open_rub,
    IFNULL(pm.provisional_inputs_missing, 0) provisional_inputs_missing,
    IFNULL(t.in_transit_units, 0) in_transit_units,
    IFNULL(t.in_transit_without_accrual_units, 0) in_transit_without_accrual_units,
    kd.known_source_defects,
    fresh.source_freshness_state
  FROM months
  CROSS JOIN wm
  CROSS JOIN fresh
  LEFT JOIN gates g ON g.m = months.m
  LEFT JOIN cogs_m cm ON cm.m = months.m
  LEFT JOIN rev_susp rs ON rs.m = months.m
  LEFT JOIN prov_m pm ON pm.m = months.m
  LEFT JOIN transit t ON t.m = months.m
  LEFT JOIN pend ON pend.m = months.m
  LEFT JOIN ctrl ct ON ct.m = months.m
  LEFT JOIN kd ON kd.m = months.m)
SELECT j.*,
  CASE
    WHEN pending_issue_rows > 0 THEN 'PENDING_CLASSIFICATION'
    WHEN known_source_defects IS NOT NULL OR finance_control_runs > 0
      OR unconsumed_rub != 0 OR unlinked_posting_rub != 0 OR unmapped_sku_rows > 0 OR cogs_gap_units > 0
      OR reversal_suspected_postings > 0 OR provisional_inputs_missing > 0
      OR (is_closed AND (provisional_units > 0 OR in_transit_without_accrual_units > 0)) THEN 'PARTIAL_SOURCE_MISSING'
    WHEN d1_repair_rows > 0 OR reversal_weak_signal_postings > 0 THEN 'UNPROVEN'
    WHEN NOT is_closed AND (provisional_units > 0 OR in_transit_units > 0) THEN 'PARTIAL_AWAITING_ACCRUALS'
    WHEN NOT is_closed THEN 'FINANCIAL_COMPLETE_WITH_TIMING_BRIDGE'
    ELSE 'FINANCIAL_COMPLETE' END financial_state,
  CASE
    WHEN finance_watermark_date IS NULL OR pnl_month >= DATE_TRUNC(finance_watermark_date, MONTH) THEN 'OPEN'
    WHEN NOT is_closed THEN 'AWAITING_LATE_ACCRUALS'
    ELSE 'CLOSED' END month_closure_status
FROM j;
