-- ROLLBACK (H0): тело production wb_mart.V_WB_STORE_ACCOUNT_LEDGER ДО изменения H0, снято tables.get 2026-10-11 (Phase C, main 8753337).
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
  WHERE supplier_oper_name IN ('Штраф', 'Платная приемка', 'Пересчет платной приемки')),
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
    WHEN 'FORCE_MAJEURE_PAYMENT' THEN 'COMPENSATION_INCOME'
    WHEN 'REVIEW_POINTS_ADVANCE_REFUND' THEN 'REFUND_INCOME'
    WHEN 'UNCLASSIFIED_DEDUCTION' THEN 'UNKNOWN'
    ELSE deduction_class END category,
  CASE WHEN deduction_class IN ('FORCE_MAJEURE_PAYMENT', 'REVIEW_POINTS_ADVANCE_REFUND') THEN 'ACCOUNT_LEVEL_INCOME'
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
SELECT l.*, l.booking_date source_operation_date, p.pt report_date
FROM l LEFT JOIN periods p ON l.booking_date BETWEEN p.pf AND p.pt
QUALIFY ROW_NUMBER() OVER (PARTITION BY l.finance_row_key, l.label, l.amount_rub ORDER BY p.pt) = 1;
