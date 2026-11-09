-- ОТКАТ store_pnl unidentified_return: тело V_WB_STORE_ACCOUNT_LEDGER в production ДО изменения (git main 1062db5b, live = Git, проверено 2026-11-06)
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_STORE_ACCOUNT_LEDGER` AS
WITH months AS (
  SELECT * FROM UNNEST(['январь', 'февраль', 'март', 'апрель', 'май', 'июнь', 'июль', 'август', 'сентябрь',
                        'октябрь', 'ноябрь', 'декабрь']) name WITH OFFSET i),
ded AS (
  SELECT d.finance_row_key, d.finance_date, d.bonus_type_name label, d.deduction_class,
    d.deduction_amount_rub amount_rub, d.srid, d.transit_supply_id,
    REGEXP_EXTRACT(LOWER(d.bonus_type_name), r'за ([а-я]+) \d{4}') m_name,
    SAFE_CAST(REGEXP_EXTRACT(d.bonus_type_name, r'за [А-Яа-я]+ (\d{4})') AS INT64) m_year
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_DEDUCTIONS_CLASSIFIED` d
  WHERE d.deduction_class != 'AD_BILLING'),
ded2 AS (
  SELECT ded.*, months.i + 1 m_num FROM ded LEFT JOIN months ON months.name = ded.m_name),
other AS (
  SELECT row_hash finance_row_key, _rr_date finance_date, supplier_oper_name label,
    CASE WHEN supplier_oper_name = 'Штраф' THEN 'PENALTY'
         WHEN supplier_oper_name IN ('Платная приемка', 'Пересчет платной приемки') THEN 'ACCEPTANCE' END deduction_class,
    IFNULL(SAFE_CAST(penalty AS NUMERIC), 0) + IFNULL(SAFE_CAST(acceptance AS NUMERIC), 0) amount_rub, srid
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FINANCE_CANONICAL`
  WHERE supplier_oper_name IN ('Штраф', 'Платная приемка', 'Пересчет платной приемки')
  UNION ALL
  -- H0: компенсации WB (for_pay, +) — доход кабинета, только в доказанном периоде карты; знак ledger: доход < 0
  SELECT f.row_hash, f._rr_date, f.supplier_oper_name, 'MARKETPLACE_COMPENSATION', -IFNULL(SAFE_CAST(f.for_pay AS NUMERIC), 0), f.srid
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FINANCE_CANONICAL` f
  JOIN `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_FINANCE_OPERATION_MAP` mp ON mp.supplier_oper_name = f.supplier_oper_name
  WHERE mp.treatment = 'ACCOUNT_LEVEL_INCOME'
    AND f._rr_date BETWEEN IFNULL(mp.valid_from, DATE '1900-01-01') AND IFNULL(mp.valid_to, DATE '9999-12-31')),
l AS (
SELECT finance_row_key, finance_date booking_date, DATE_TRUNC(finance_date, MONTH) booking_month,
  CASE WHEN deduction_class = 'UTILIZATION' AND m_num IS NOT NULL AND m_year IS NOT NULL THEN DATE(m_year, m_num, 1)
       WHEN deduction_class = 'MINIMUM_PAYMENT_ADJUSTMENT' THEN DATE_SUB(DATE_TRUNC(finance_date, MONTH), INTERVAL 1 MONTH)
       ELSE DATE_TRUNC(finance_date, MONTH) END service_month,
  CASE WHEN deduction_class = 'UTILIZATION' AND m_num IS NOT NULL THEN 'LABEL_PERIOD'
       WHEN deduction_class = 'MINIMUM_PAYMENT_ADJUSTMENT' THEN 'INFERRED_PREVIOUS_MONTH'
       ELSE 'BOOKING_MONTH' END service_period_basis,
  CASE deduction_class
    WHEN 'MINIMUM_PAYMENT_ADJUSTMENT' THEN 'MINIMUM_PAYMENT'
    WHEN 'UTILIZATION' THEN 'UTILIZATION'
    WHEN 'TRANSIT_DEDUCTION' THEN 'TRANSIT'
    -- «Добровольная выплата за товары, пострадавшие в результате обстоятельств непреодолимой силы» — отдельная строка
    -- P&L «Компенсация WB за утрату товара» (FIRE/LOST INVENTORY, OWNER ACK 11.10); месяц — ниже, по документу WB
    WHEN 'FORCE_MAJEURE_PAYMENT' THEN 'LOSS_COMPENSATION'
    WHEN 'MARKETPLACE_COMPENSATION' THEN 'COMPENSATION_INCOME'
    -- «Баллы за отзывы»: аванс и возврат его остатка — один расход кабинета со знаком (H0, OWNER ACK 11.10)
    WHEN 'REVIEW_POINTS_ADVANCE_REFUND' THEN 'REVIEW_POINTS'
    WHEN 'UNCLASSIFIED_DEDUCTION' THEN 'UNKNOWN'
    ELSE deduction_class END category,
  CASE WHEN deduction_class = 'FORCE_MAJEURE_PAYMENT' THEN 'LOSS_COMPENSATION_INCOME'
       WHEN deduction_class = 'MARKETPLACE_COMPENSATION' THEN 'ACCOUNT_LEVEL_INCOME'
       WHEN deduction_class = 'REVIEW_POINTS_ADVANCE_REFUND' THEN 'ACCOUNT_LEVEL'
       -- утилизация без «за <месяц> <год>»: месяц услуги не доказан → не закрывать месяц молча
       WHEN deduction_class = 'UTILIZATION' AND (m_num IS NULL OR m_year IS NULL) THEN 'UNKNOWN'
       WHEN deduction_class = 'UNCLASSIFIED_DEDUCTION' THEN 'UNKNOWN'
       WHEN deduction_class IN ('MINIMUM_PAYMENT_ADJUSTMENT', 'UTILIZATION', 'TRANSIT_DEDUCTION', 'PENALTY', 'ACCEPTANCE') THEN 'ACCOUNT_LEVEL'
       ELSE 'UNKNOWN' END treatment,
  -- знак: + = расход продавца; доходы (компенсации, возвраты аванса) приходят отрицательными
  amount_rub, label, srid, transit_supply_id
FROM (
  SELECT finance_row_key, finance_date, label, deduction_class, amount_rub, srid, transit_supply_id, m_num, m_year FROM ded2
  UNION ALL
  SELECT finance_row_key, finance_date, label, deduction_class, amount_rub, srid, CAST(NULL AS STRING), NULL, NULL FROM other
  WHERE amount_rub != 0)),
periods AS (
  SELECT DISTINCT SAFE_CAST(report_period_from AS DATE) pf, SAFE_CAST(report_period_to AS DATE) pt
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FINANCE_CANONICAL`)
-- Три даты раздельно (OWNER ACK 09.10): месяц услуги (service_month), дата операции в отчёте WB
-- (source_operation_date = booking_date, по ней окна), дата отчёта WB, в котором строка пришла (report_date).
-- Поздний счёт за прошлый месяц услуги ложится в СВОЙ месяц услуги — P&L этого месяца пересчитывается сам.
-- Компенсация за утрату товара (FIRE/LOST INVENTORY): сверка — wb_mart.V_WB_INVENTORY_LOSS_COMPENSATION_MATCH (по документу
-- WB из закрытого wb_secure; одна выплата — одна строка финотчёта). Месяц услуги — месяц события, только если документ его
-- доказывает (DOCUMENT_LINKED), иначе месяц проводки (BOOKING_MONTH). Номера документов здесь не раскрываются.
SELECT x.* EXCEPT (loss_attribution_basis, attributed_incident_month) REPLACE (
    -- номер документа WB компенсации за утрату в аналитическом журнале не раскрывается (OWNER DECISION 11.10 §5)
    IF(x.treatment = 'LOSS_COMPENSATION_INCOME', REGEXP_REPLACE(x.label, r'документ №\s*\d+', 'документ № [скрыт]'), x.label) AS label,
    IF(x.treatment = 'LOSS_COMPENSATION_INCOME' AND x.loss_evidence_matched AND x.attributed_incident_month IS NOT NULL,
       x.attributed_incident_month, x.service_month) AS service_month,
    IF(x.treatment = 'LOSS_COMPENSATION_INCOME', IF(x.loss_evidence_matched, x.loss_attribution_basis, 'UNRECONCILED'),
       x.service_period_basis) AS service_period_basis)
FROM (
  SELECT l.*, l.booking_date source_operation_date, p.pt report_date,
    IF(l.treatment = 'LOSS_COMPENSATION_INCOME', IFNULL(cm.loss_evidence_matched, FALSE), NULL) loss_evidence_matched,
    cm.attribution_basis loss_attribution_basis, cm.attributed_incident_month
  FROM l LEFT JOIN periods p ON l.booking_date BETWEEN p.pf AND p.pt
  LEFT JOIN `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_INVENTORY_LOSS_COMPENSATION_MATCH` cm
    ON cm.finance_row_key = l.finance_row_key AND l.treatment = 'LOSS_COMPENSATION_INCOME'
  QUALIFY ROW_NUMBER() OVER (PARTITION BY l.finance_row_key, l.label, l.amount_rub ORDER BY p.pt) = 1) x;
