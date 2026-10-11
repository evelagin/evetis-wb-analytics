-- ============================================================================
-- WB FIRE / LOST INVENTORY (OWNER DECISION 2026-10-11) — товар EVETIS, утраченный на складах WB при атаках БПЛА.
-- Списание утраченного товара — РАСХОД месяца события (каноническая себестоимость на дату события); компенсация WB —
-- ДОХОД (месяц события, только если документ WB это доказывает; иначе месяц проводки). Две строки, не неттуются.
-- Претензия к WB — справочно, вне P&L. Док: docs/finance/WB_FIRE_LOSS_2026-10-11.md.
--
-- 🔴 Закрытые доказательства (склад × nm × количество, документы WB, претензия) — НЕ в Git и НЕ в wb_ops/wb_mart, а в
-- отдельном датасете wb_secure (infra/terraform/wb_secure.tf: ACL только владелец; sa-loaders-prod — чтение ровно двух
-- таблиц). Заполняет владелец разово инструментом tools/wb_inventory_loss_import.py из манифеста вне Git.
--
-- Слои:
--   wb_secure.V_WB_INVENTORY_LOSS_ITEMS, wb_secure.V_WB_INVENTORY_LOSS_CLAIM_TRACKING — поштучный аудит и претензия
--     (только владелец),
--   wb_mart.V_WB_INVENTORY_LOSS_MONTHLY, _COMPENSATION_MATCH, _STATUS — только финансовый результат и состояние полноты:
--     без номеров документов, id событий, текстов доказательств и позиций склад × nm.
-- 🔴 Без evetis_ref (1C1B/1C2A): nm → SKU — wb_raw.REF_SKU_MASTER, себестоимость — wb_mart.UNITKA_COGS_EFFECTIVE; ровно
-- один интервал на дату события, иначе себестоимость неизвестна (не 0) и месяц не закрывается.
-- ============================================================================

-- ─── S1. Поштучный аудит (wb_secure, только владелец): позиция, себестоимость на дату события ─────────────────────
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_secure.V_WB_INVENTORY_LOSS_ITEMS` AS
WITH ev AS (
  SELECT incident_id, report_warehouse, wb_report_ref, incident_date, incident_evidence, nm_id, lost_qty, import_run_id
  FROM `project-fa311fc0-4d87-4781-986.wb_secure.WB_INVENTORY_LOSS_EVIDENCE`),
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
  ev.lost_qty * c.unit_cogs_rub inventory_loss_writeoff_rub, ev.import_run_id
FROM ev LEFT JOIN ref USING (nm_id) LEFT JOIN cogs c USING (incident_id, nm_id);

-- ─── L1. Списание по месяцу события + защита от двойного счёта (wb_mart, агрегат) ─────────────────────────────────
-- Поштучные позиции — только внутри запроса, наружу — месяц, количество, сумма, флаги.
-- Двойной счёт исключается ДАННЫМИ остатков WB, а не заказами (заказы с 14.08 WB подписывает «Склад WB РФ»):
--  (а) количество отчёта = остаток WB по складу × nm в снимке на дату отчётов 26.08.2026 — это остаток, который не
--      уехал со склада, проданная единица в него не входит (её себестоимость — во «Вкладе SKU» по продаже),
--  (б) после 26.08 остаток этих позиций не уменьшался ни в одном снимке — ни одна списанная единица не ушла в продажу.
-- Расхождение (а) или уменьшение (б) — inventory_loss_double_count_risk_qty > 0: месяц не закрывается (fail-closed:
-- списание остатка самим WB позже тоже даст риск — тогда ручная сверка владельцем).
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_INVENTORY_LOSS_MONTHLY` AS
WITH ev AS (
  SELECT incident_id, report_warehouse, incident_date, DATE_TRUNC(incident_date, MONTH) incident_month, nm_id, lost_qty
  FROM `project-fa311fc0-4d87-4781-986.wb_secure.WB_INVENTORY_LOSS_EVIDENCE`),
ref AS (
  SELECT nm_id, IF(COUNT(DISTINCT internal_sku) = 1, ANY_VALUE(internal_sku), NULL) internal_sku
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.REF_SKU_MASTER` WHERE marketplace = 'WB' GROUP BY 1),
it AS (
  SELECT ev.incident_id, ev.report_warehouse, ev.incident_month, ev.nm_id, ev.lost_qty,
    IF(COUNT(u.internal_sku) = 1, MAX(u.product_cogs_rub), NULL) unit_cogs_rub
  FROM ev LEFT JOIN ref USING (nm_id)
  LEFT JOIN `project-fa311fc0-4d87-4781-986.wb_mart.UNITKA_COGS_EFFECTIVE` u
    ON u.internal_sku = ref.internal_sku AND ev.incident_date BETWEEN u.effective_from AND IFNULL(u.effective_to, DATE '9999-12-31')
  GROUP BY 1, 2, 3, 4, 5),
st AS (
  SELECT _snapshot_date sd, warehouse_name, nm_id, SUM(quantity) q
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_STOCKS_T5`
  WHERE row_type = 'WAREHOUSE' AND _snapshot_date >= DATE '2026-08-26'
    AND warehouse_name IN (SELECT DISTINCT report_warehouse FROM ev)
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
  SUM(IFNULL(it.lost_qty * it.unit_cogs_rub, 0)) inventory_loss_writeoff_rub,
  SUM(IF(it.unit_cogs_rub IS NULL, it.lost_qty, 0)) inventory_loss_cogs_missing_qty,
  ANY_VALUE(rm.risk_qty) inventory_loss_double_count_risk_qty,
  COUNT(DISTINCT it.incident_id) inventory_loss_incidents
FROM it LEFT JOIN risk_m rm USING (incident_month) GROUP BY 1;

-- ─── L2. Сверка компенсации WB по строке финотчёта (wb_mart, без номеров документов) ─────────────────────────────
-- Строка «Добровольная выплата за товары, пострадавшие…» (класс FORCE_MAJEURE_PAYMENT) сверена, если её документ WB есть
-- в закрытой таблице с той же датой и суммой и это ЕДИНСТВЕННАЯ строка финотчёта с этим документом (дубль не сверен —
-- иначе доход удвоился бы). Месяц P&L: месяц события, только если документ его доказывает (DOCUMENT_LINKED), иначе
-- месяц проводки. Наружу: ключ строки финотчёта, признак сверки, основание и месяц атрибуции.
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_INVENTORY_LOSS_COMPENSATION_MATCH` AS
WITH f AS (
  SELECT finance_row_key, finance_date, deduction_amount_rub,
    REGEXP_EXTRACT(bonus_type_name, r'документ №\s*(\d+)') doc
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_DEDUCTIONS_CLASSIFIED`
  WHERE deduction_class = 'FORCE_MAJEURE_PAYMENT'),
e AS (
  SELECT wb_document, booking_date, amount_rub, attribution_basis,
    IF(attribution_basis = 'DOCUMENT_LINKED', attributed_incident_month, NULL) attributed_incident_month
  FROM `project-fa311fc0-4d87-4781-986.wb_secure.WB_INVENTORY_LOSS_COMPENSATION_EVIDENCE`)
SELECT f.finance_row_key,
  IFNULL(e.booking_date = f.finance_date AND ABS(e.amount_rub + f.deduction_amount_rub) < 0.005, FALSE)
    AND COUNT(DISTINCT f.finance_row_key) OVER (PARTITION BY f.doc) = 1 loss_evidence_matched,
  e.attribution_basis, e.attributed_incident_month
FROM f LEFT JOIN e ON e.wb_document = f.doc;

-- ─── L3. Состояние полноты закрытых доказательств (wb_mart, одна строка, агрегаты) ─────────────────────────────────
-- Пустая закрытая таблица = списание молча 0: гейт публикации отказывает, если доказательства не загружены ровно одним
-- импортом. Здесь же — сверка документов компенсации с финотчётом (каждый документ найден ровно один раз).
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_INVENTORY_LOSS_STATUS` AS
WITH ev AS (
  SELECT COUNT(*) evidence_rows, COUNT(DISTINCT import_run_id) evidence_runs, IFNULL(SUM(lost_qty), 0) evidence_units,
    COUNT(DISTINCT incident_id) evidence_incidents
  FROM `project-fa311fc0-4d87-4781-986.wb_secure.WB_INVENTORY_LOSS_EVIDENCE`),
cp AS (
  SELECT COUNT(*) compensation_documents, IFNULL(SUM(amount_rub), 0) compensation_documents_rub,
    COUNTIF(attribution_basis = 'DOCUMENT_LINKED') compensation_documents_linked
  FROM `project-fa311fc0-4d87-4781-986.wb_secure.WB_INVENTORY_LOSS_COMPENSATION_EVIDENCE`),
f AS (
  SELECT REGEXP_EXTRACT(bonus_type_name, r'документ №\s*(\d+)') doc, COUNT(*) n
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_DEDUCTIONS_CLASSIFIED`
  WHERE deduction_class = 'FORCE_MAJEURE_PAYMENT' GROUP BY 1),
dm AS (
  SELECT COUNTIF(IFNULL(f.n, 0) != 1) compensation_documents_not_once
  FROM `project-fa311fc0-4d87-4781-986.wb_secure.WB_INVENTORY_LOSS_COMPENSATION_EVIDENCE` e LEFT JOIN f ON f.doc = e.wb_document)
SELECT ev.evidence_rows, ev.evidence_runs, ev.evidence_units, ev.evidence_incidents,
  cp.compensation_documents, cp.compensation_documents_rub, cp.compensation_documents_linked, dm.compensation_documents_not_once
FROM ev CROSS JOIN cp CROSS JOIN dm;

-- ─── S2. Претензия к WB (wb_secure, только владелец) — справочно, ВНЕ P&L ─────────────────────────────────────────
-- Управленческое списание, документальная база претензии, заявлено, полученная компенсация (до подачи — в претензии,
-- в день подачи и позже — после) и остаток требования. Неоплаченная претензия — не доход.
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_secure.V_WB_INVENTORY_LOSS_CLAIM_TRACKING` AS
WITH w AS (SELECT SUM(inventory_loss_writeoff_rub) writeoff, SUM(inventory_loss_qty) qty
           FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_INVENTORY_LOSS_MONTHLY`),
comp AS (
  SELECT c.claim_id,
    -SUM(l.amount_rub) received_total,
    -SUM(IF(l.booking_date >= c.filed_date, l.amount_rub, 0)) received_after_claim
  FROM `project-fa311fc0-4d87-4781-986.wb_secure.WB_INVENTORY_LOSS_CLAIM` c
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
FROM `project-fa311fc0-4d87-4781-986.wb_secure.WB_INVENTORY_LOSS_CLAIM` c CROSS JOIN w LEFT JOIN comp USING (claim_id);
