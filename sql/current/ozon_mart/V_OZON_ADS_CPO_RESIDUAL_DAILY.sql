-- ============================================================================
-- CANONICAL CURRENT DEFINITION — ozon_mart.V_OZON_ADS_CPO_RESIDUAL_DAILY (VIEW) · sync_state: pending_deploy
-- Phase B (OWNER ACK 2026-10-08). Rules: sql/current/README.md. Док: docs/finance/OZON_CPO_PHASE_B_2026-10-08.md.
--
-- Тождество «биллинговый рубль не исчезает», зерно = CPO-кампания × дата списания:
--   billed_rub = attributed_sku_rub + unmapped_rub + residual_rub
--   billed_rub        — биллинг кампании RAW_OZON_ADS_EXPENSE_DAILY.expense_rub (АВТОРИТЕТ итога CPO);
--   attributed_sku_rub — строки отчёта с сопоставленным ЗАКАЗАННЫМ SKU (они и только они попадают в
--                        FCT_OZON_SKU_PNL_DAILY.cpo_expense_rub);
--   unmapped_rub      — строки отчёта без однозначного SKU (видны, но в SKU-слой не идут);
--   residual_rub      — биллинг, не покрытый отчётом.
-- store_level_rub = unmapped_rub + residual_rub — то, что остаётся на уровне магазина. Перенос части CPO
-- из магазина в SKU не меняет чистую прибыль магазина: FCT_OZON_PNL_MONTHLY вычитает весь биллинг
-- кампаний и до Phase B, и после.
--
-- СТАТУС суток (допуск 0,02 ₽ — отчёт и биллинг округляют независимо, наблюдено 0,01 ₽):
--   NOT_COVERED             — отчёт за эти сутки не загружен (ни один COMPLETE-блок журнала их не покрывает);
--   BILLING_PENDING         — сутки позже последнего дня биллинга в RAW (биллинг ещё не пришёл);
--   MATCH                   — |residual| ≤ 0,02;
--   POSITIVE_RESIDUAL       — биллинг больше отчёта: остаток уровня магазина;
--   NEGATIVE_RESIDUAL_ERROR — отчёт больше биллинга: атрибуция превысила списанное — QA ERROR.
-- finance_type54_rub — независимая сверка: начисления финансов type 54 с unit_number = campaign_id.
-- Проводка финансов отстаёт от даты списания (наблюдено до суток), поэтому суточное расхождение
-- финансов — не ошибка; сверка финансов накопленная (sql/ozon/qa_cpo_orders_v1.sql, F1).
-- Internal dependencies: V_OZON_ADS_CPO_ORDERS.
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_ADS_CPO_RESIDUAL_DAILY`
OPTIONS (description = "Сверка Оплаты за заказ (CPO) Ozon, зерно = CPO-кампания x дата списания. Тождество: billed_rub (биллинг кампании, авторитет итога) = attributed_sku_rub (строки отчёта с сопоставленным заказанным SKU, они же FCT_OZON_SKU_PNL_DAILY.cpo_expense_rub) + unmapped_rub + residual_rub. store_level_rub = unmapped_rub + residual_rub остаётся на уровне магазина. Статус: NOT_COVERED, BILLING_PENDING, MATCH, POSITIVE_RESIDUAL, NEGATIVE_RESIDUAL_ERROR (допуск 0,02 руб.). finance_type54_rub - независимая сверка финансов type 54 по campaign_id с задержкой проводки.")
AS
WITH camp AS (
  SELECT DISTINCT campaign_id, adv_object_type report_family
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_ADS_CAMPAIGNS`
  WHERE payment_type = 'CPO'),
billed AS (
  SELECT e.date d, e.campaign_id, SUM(e.expense_rub) billed_rub
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_ADS_EXPENSE_DAILY` e
  JOIN camp USING (campaign_id)
  GROUP BY 1, 2),
billing_through AS (
  SELECT MAX(date) d FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_ADS_EXPENSE_DAILY`),
rep AS (
  SELECT charge_date d, campaign_id,
    SUM(expense_rub) report_rub,
    SUM(IF(ordered_mapping_status = 'MAPPED', expense_rub, 0)) attributed_sku_rub,
    SUM(IF(ordered_mapping_status != 'MAPPED', expense_rub, 0)) unmapped_rub,
    COUNT(*) report_rows
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_ADS_CPO_ORDERS`
  WHERE campaign_status IN ('RESOLVED', 'RESOLVED_BY_BILLING_DAY')
  GROUP BY 1, 2),
fin AS (
  SELECT f.event_date d, f.unit_number campaign_id, -SUM(f.amount_rub) finance_type54_rub
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_FINANCE_ACCRUAL` f
  JOIN camp c ON c.campaign_id = f.unit_number
  WHERE f.type_id = 54 AND f.unit_number_meaning = 'campaign_id'
  GROUP BY 1, 2),
covered AS (
  SELECT DISTINCT d
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.OZON_CPO_ORDER_RUNS`,
       UNNEST(GENERATE_DATE_ARRAY(chunk_from, chunk_to)) d
  WHERE record_type = 'CHUNK' AND status = 'COMPLETE'),
keys AS (
  SELECT d, campaign_id FROM billed UNION DISTINCT
  SELECT d, campaign_id FROM rep UNION DISTINCT
  SELECT d, campaign_id FROM fin),
j AS (
  SELECT k.d, k.campaign_id, c.report_family,
    IFNULL(b.billed_rub, 0) billed_rub,
    IFNULL(r.report_rub, 0) report_rub,
    IFNULL(r.attributed_sku_rub, 0) attributed_sku_rub,
    IFNULL(r.unmapped_rub, 0) unmapped_rub,
    IFNULL(r.report_rows, 0) report_rows,
    f.finance_type54_rub,
    cv.d IS NOT NULL report_covered,
    k.d <= bt.d billing_loaded
  FROM keys k
  JOIN camp c ON c.campaign_id = k.campaign_id
  CROSS JOIN billing_through bt
  LEFT JOIN billed b ON b.d = k.d AND b.campaign_id = k.campaign_id
  LEFT JOIN rep r ON r.d = k.d AND r.campaign_id = k.campaign_id
  LEFT JOIN fin f ON f.d = k.d AND f.campaign_id = k.campaign_id
  LEFT JOIN covered cv ON cv.d = k.d)
SELECT
  d charge_date, campaign_id, report_family,
  billed_rub, report_rub, attributed_sku_rub, unmapped_rub,
  IF(report_covered, billed_rub - report_rub, billed_rub) residual_rub,
  IF(report_covered, billed_rub - report_rub, billed_rub) + unmapped_rub store_level_rub,
  report_rows, finance_type54_rub,
  report_covered, billing_loaded,
  CASE WHEN NOT report_covered THEN 'NOT_COVERED'
       WHEN NOT billing_loaded THEN 'BILLING_PENDING'
       WHEN ABS(billed_rub - report_rub) <= 0.02 THEN 'MATCH'
       WHEN billed_rub - report_rub > 0.02 THEN 'POSITIVE_RESIDUAL'
       ELSE 'NEGATIVE_RESIDUAL_ERROR' END residual_status
FROM j;
