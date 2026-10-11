-- ============================================================================
-- WB FIRE / LOST INVENTORY (OWNER DECISION 2026-10-11) — товар EVETIS, утраченный на складах WB при атаках БПЛА.
-- Слой списания для P&L магазина: списание утраченного товара — РАСХОД месяца события (каноническая себестоимость на
-- дату события), компенсация WB — ДОХОД (месяц события, только если документ WB это доказывает; иначе месяц проводки).
-- Две строки, не неттуются. Претензия к WB — справочно, вне P&L. Док: docs/finance/WB_FIRE_LOSS_2026-10-11.md.
--
-- 🔴 Репозиторий публичный: доказательства (склад × nm × количество, документы WB, претензия) — НЕ в Git, а в закрытых
-- таблицах wb_ops.WB_INVENTORY_LOSS_* (infra/terraform/wb_inventory_loss.tf), которые разово заполняет владелец
-- инструментом tools/wb_inventory_loss_import.py из манифеста вне Git (PLAN → APPLY по отпечатку → перечитывание).
-- Количество — из отчётов WB о пострадавших товарах, не из дельт остатков.
-- 🔴 Без evetis_ref (1C1B/1C2A): nm → SKU — wb_raw.REF_SKU_MASTER, себестоимость — wb_mart.UNITKA_COGS_EFFECTIVE; ровно
-- один интервал на дату события, иначе себестоимость неизвестна (не 0) и месяц не закрывается.
-- ============================================================================

-- ─── L1. Позиции утраты: склад × nm × количество отчёта WB, себестоимость на дату события ─────────────────────────
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_INVENTORY_LOSS_ITEMS` AS
WITH ev AS (
  SELECT incident_id, report_warehouse, wb_report_ref, incident_date, incident_evidence, nm_id, lost_qty
  FROM `project-fa311fc0-4d87-4781-986.wb_ops.WB_INVENTORY_LOSS_EVIDENCE`),
ref AS (
  SELECT nm_id, IF(COUNT(DISTINCT internal_sku) = 1, ANY_VALUE(internal_sku), NULL) internal_sku
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.REF_SKU_MASTER` WHERE marketplace = 'WB' GROUP BY 1),
cogs AS (
  SELECT ev.incident_id, ev.nm_id, COUNT(u.internal_sku) cogs_interval_count,
    IF(COUNT(u.internal_sku) = 1, MAX(u.product_cogs_rub), NULL) unit_cogs_rub
  FROM ev LEFT JOIN ref USING (nm_id)
  LEFT JOIN `project-fa311fc0-4d87-4781-986.wb_mart.UNITKA_COGS_EFFECTIVE` u
    ON u.internal_sku = ref.internal_sku AND ev.incident_date BETWEEN u.effective_from AND IFNULL(u.effective_to, DATE '9999-12-31')
  GROUP BY 1, 2)
SELECT ev.incident_id, ev.report_warehouse, ev.wb_report_ref, ev.incident_date, DATE_TRUNC(ev.incident_date, MONTH) incident_month,
  ev.incident_evidence, ev.nm_id, ref.internal_sku, ev.lost_qty, c.cogs_interval_count, c.unit_cogs_rub,
  ev.lost_qty * c.unit_cogs_rub inventory_loss_writeoff_rub
FROM ev LEFT JOIN ref USING (nm_id) LEFT JOIN cogs c USING (incident_id, nm_id);

-- ─── L2. Списание по месяцу события + защита от двойного счёта ───────────────────────────────────────────────────
-- Двойной счёт исключается ДАННЫМИ остатков WB, а не заказами (заказы с 14.08 WB подписывает «Склад WB РФ»):
--  (а) количество отчёта = остаток WB по складу × nm в снимке на дату отчётов 26.08.2026 — это остаток, который не
--      уехал со склада; проданная единица в него не входит (её себестоимость — во «Вкладе SKU» по продаже),
--  (б) после 26.08 остаток этих позиций не уменьшался ни в одном снимке — ни одна списанная единица не ушла в продажу.
-- Расхождение (а) или уменьшение (б) — inventory_loss_double_count_risk_qty > 0: месяц не закрывается.
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_INVENTORY_LOSS_MONTHLY` AS
WITH it AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_INVENTORY_LOSS_ITEMS`),
st AS (
  SELECT _snapshot_date sd, warehouse_name, nm_id, SUM(quantity) q
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_STOCKS_T5`
  WHERE row_type = 'WAREHOUSE' AND _snapshot_date >= DATE '2026-08-26'
    AND warehouse_name IN (SELECT DISTINCT report_warehouse FROM it)
  GROUP BY snapshot_id, 1, 2, 3
  QUALIFY snapshot_id = MAX(snapshot_id) OVER (PARTITION BY _snapshot_date)),
rep_day AS (SELECT warehouse_name, nm_id, q FROM st WHERE sd = DATE '2026-08-26'),
pos AS (
  SELECT it.incident_month, it.report_warehouse, it.nm_id, SUM(it.lost_qty) lost_qty
  FROM it GROUP BY 1, 2, 3),
risk AS (
  SELECT pos.incident_month,
    ABS(pos.lost_qty - IFNULL(r.q, 0))                                                 -- (а) отчёт ≠ остаток 26.08
    + GREATEST(IFNULL(r.q, 0) - IFNULL((SELECT MIN(st.q) FROM st
        WHERE st.warehouse_name = pos.report_warehouse AND st.nm_id = pos.nm_id), 0), 0) risk_qty  -- (б) остаток уменьшился
  FROM pos LEFT JOIN rep_day r ON r.warehouse_name = pos.report_warehouse AND r.nm_id = pos.nm_id),
risk_m AS (SELECT incident_month, SUM(risk_qty) risk_qty FROM risk GROUP BY 1)
SELECT it.incident_month service_month,
  SUM(it.lost_qty) inventory_loss_qty,
  SUM(IFNULL(it.inventory_loss_writeoff_rub, 0)) inventory_loss_writeoff_rub,
  SUM(IF(it.unit_cogs_rub IS NULL, it.lost_qty, 0)) inventory_loss_cogs_missing_qty,
  ANY_VALUE(rm.risk_qty) inventory_loss_double_count_risk_qty,
  COUNT(DISTINCT it.incident_id) inventory_loss_incidents,
  STRING_AGG(DISTINCT CONCAT(FORMAT_DATE('%Y-%m-%d', it.incident_date), ' ', it.report_warehouse), '; '
    ORDER BY CONCAT(FORMAT_DATE('%Y-%m-%d', it.incident_date), ' ', it.report_warehouse)) inventory_loss_incident_list
FROM it LEFT JOIN risk_m rm USING (incident_month) GROUP BY 1;

-- ─── L3. Сверка компенсации WB: каждая выплата «за товары, пострадавшие…» — по документу WB ───────────────────────
-- Месяц P&L компенсации: attributed_incident_month, только если документ WB связывает выплату с событием
-- (attribution_basis = 'DOCUMENT_LINKED'); иначе — месяц проводки ('BOOKING_MONTH'). Выплата в журнале без документа
-- в этом списке или с другой суммой/датой — не сверена: месяц проводки «Есть неразобранные операции».
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_INVENTORY_LOSS_COMPENSATION_EVIDENCE` AS
SELECT wb_document, booking_date, amount_rub, attribution_basis,
  IF(attribution_basis = 'DOCUMENT_LINKED', attributed_incident_month, NULL) attributed_incident_month, evidence
FROM `project-fa311fc0-4d87-4781-986.wb_ops.WB_INVENTORY_LOSS_COMPENSATION_EVIDENCE`;

-- ─── L4. Претензия к WB — справочно, ВНЕ P&L (загрузчик не читает) ─────────────────────────────────────────────────
-- Управленческое списание, документальная база претензии, заявлено, признанная/полученная компенсация и остаток
-- требования. Неоплаченная претензия — не доход: доходом становится только выплата WB, проведённая в финотчёте.
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_INVENTORY_LOSS_CLAIM_TRACKING` AS
WITH w AS (SELECT SUM(inventory_loss_writeoff_rub) writeoff, SUM(lost_qty) qty
           FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_INVENTORY_LOSS_ITEMS`),
comp AS (
  SELECT c.claim_id,
    -SUM(l.amount_rub) received_total,
    -SUM(IF(l.booking_date >= c.filed_date, l.amount_rub, 0)) received_after_claim   -- в день подачи и позже; до — в претензии
  FROM `project-fa311fc0-4d87-4781-986.wb_ops.WB_INVENTORY_LOSS_CLAIM` c
  CROSS JOIN `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_STORE_ACCOUNT_LEDGER` l
  WHERE l.treatment = 'LOSS_COMPENSATION_INCOME' GROUP BY 1)
SELECT c.claim_id, c.filed_date, c.status, w.qty inventory_loss_qty,
  w.writeoff inventory_loss_writeoff_rub,                                   -- управленческое списание (в P&L)
  c.claim_documented_goods_cost_rub,                                         -- документальная база претензии (memo)
  c.claim_documented_goods_cost_rub - w.writeoff claim_basis_difference_rub,  -- в прибыль не входит
  c.compensation_before_claim_rub, c.claimed_goods_rub, c.claimed_other_rub,
  c.claimed_goods_rub + c.claimed_other_rub claimed_total_rub,
  IFNULL(comp.received_total, 0) compensation_received_total_rub,
  IFNULL(comp.received_after_claim, 0) compensation_received_after_claim_rub,
  c.claimed_goods_rub + c.claimed_other_rub - IFNULL(comp.received_after_claim, 0) outstanding_claim_rub,  -- не доход
  ABS(IFNULL(comp.received_total, 0) - IFNULL(comp.received_after_claim, 0) - c.compensation_before_claim_rub) < 0.005
    pre_claim_compensation_reconciled
FROM `project-fa311fc0-4d87-4781-986.wb_ops.WB_INVENTORY_LOSS_CLAIM` c CROSS JOIN w LEFT JOIN comp USING (claim_id);
