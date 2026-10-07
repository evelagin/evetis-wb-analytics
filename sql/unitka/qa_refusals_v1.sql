-- ============================================================================
-- QA (read-only) — доказанные отказы вне Orders API (Phase 1A, 2026-10-07).
-- Независимые проверки: каждая — один SELECT со столбцом status. Блоки разделены строкой «-- @check <ID>».
-- Запуск после развёртывания вью (или до — подстановкой тел кандидатов).
-- ============================================================================

-- @check R1_PROVEN_REFUSAL_NOT_SOLD_IN_SALES_API
-- Независимый источник: Statistics API продаж (V_WB_SALES_RETURNS). Доказанный отказ не может быть продажей.
SELECT COUNT(*) proven, COUNTIF(s.srid IS NOT NULL) found_as_sale,
  IF(COUNTIF(s.srid IS NOT NULL) = 0, 'PASS', 'FAIL') status
FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_REFUSAL_EVIDENCE` e
LEFT JOIN (SELECT DISTINCT srid FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_SALES_RETURNS` WHERE operation_type = 'SALE') s USING (srid)
WHERE e.evidence_class = 'PROVEN_REFUSAL';

-- @check R2_PROVEN_REFUSAL_COUNTED_WHERE_FUNNEL_HAS_EXCESS
-- Отказ, который воронка посчитала (есть избыток над Orders API), не может остаться продажей: он в S.
SELECT COUNT(*) days_with_proven, COUNTIF(refusal_counted_qty < LEAST(proven_refusal_srids, funnel_excess_qty)) days_not_counted,
  IF(COUNTIF(refusal_counted_qty < LEAST(proven_refusal_srids, funnel_excess_qty)) = 0, 'PASS', 'FAIL') status
FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_REFUSAL_DAILY`
WHERE proven_refusal_srids > 0;

-- @check R3_NO_REFUSAL_WITHOUT_PROOF
-- В S не попадает ничего сверх доказанного и сверх избытка воронки: открытые и незрелые единицы — не отказы.
SELECT COUNT(*) days, COUNTIF(refusal_counted_qty > proven_refusal_srids OR refusal_counted_qty > funnel_excess_qty) violations,
  COUNTIF(refusal_evidence_status IN ('STILL_OPEN', 'MATURE_BUT_UNPROVEN') AND refusal_counted_qty > proven_refusal_srids) open_counted,
  IF(COUNTIF(refusal_counted_qty > proven_refusal_srids OR refusal_counted_qty > funnel_excess_qty) = 0, 'PASS', 'FAIL') status
FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_REFUSAL_DAILY`;

-- @check R4_FACT_VIEWS_CARRY_REFUSALS_EXACTLY
-- Обе вью фактов прибавляют к отменам Orders API ровно refusal_counted_qty, S ≤ Q на строках с отказом, источник помечен.
WITH fv AS (
  SELECT 'DAILY' v, nm_id, date_msk, orders, cancels, cancels_source, refusal_counted_qty FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_DAILY_FACT`
  UNION ALL
  SELECT 'RECON', nm_id, date_msk, orders, cancels, cancels_source, refusal_counted_qty FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_RECON_FACT`),
r AS (SELECT nm_id, date_msk, refusal_counted_qty rq FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_REFUSAL_DAILY`)
SELECT COUNT(*) rows_checked,
  COUNTIF(d.refusal_counted_qty != IFNULL(r.rq, 0) AND d.cancels_source != 'XLSX_BACKFILL') mismatched,
  COUNTIF(d.refusal_counted_qty > 0 AND d.cancels > d.orders) cancels_gt_orders,
  COUNTIF((d.refusal_counted_qty > 0) != (d.cancels_source = 'PROXY_FACT_ORDERS+FINANCE_REFUSAL')) source_mislabelled,
  IF(COUNTIF(d.refusal_counted_qty != IFNULL(r.rq, 0) AND d.cancels_source != 'XLSX_BACKFILL') + COUNTIF(d.cancels > d.orders)
     + COUNTIF((d.refusal_counted_qty > 0) != (d.cancels_source = 'PROXY_FACT_ORDERS+FINANCE_REFUSAL')) = 0, 'PASS', 'FAIL') status
FROM fv d LEFT JOIN r USING (nm_id, date_msk);

-- @check R5_MATURE_UNPROVEN_VISIBLE
-- Зрелые необъяснённые единицы видны (информационно): число и даты. Не FAIL — сигнал владельцу.
SELECT COUNTIF(refusal_evidence_status = 'MATURE_BUT_UNPROVEN') mature_unproven_days,
  SUM(IF(refusal_evidence_status = 'MATURE_BUT_UNPROVEN', unexplained_qty, 0)) mature_unproven_units,
  SUM(IF(refusal_evidence_status = 'STILL_OPEN', unexplained_qty, 0)) still_open_units,
  'INFO' status
FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_REFUSAL_DAILY`;
