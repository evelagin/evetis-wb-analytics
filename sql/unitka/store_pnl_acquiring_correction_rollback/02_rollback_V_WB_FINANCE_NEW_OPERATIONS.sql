-- ОТКАТ store_pnl acquiring_correction: тело V_WB_FINANCE_NEW_OPERATIONS в production ДО изменения (git main 1062db5b, live = Git, проверено 2026-11-06)
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_FINANCE_NEW_OPERATIONS` AS
-- Класс строки — по карте С УЧЁТОМ доказанного периода: вне периода — PENDING_CLASSIFICATION.
WITH f AS (
  SELECT supplier_oper_name, _rr_date,
    ABS(IFNULL(SAFE_CAST(retail_amount AS NUMERIC), 0)) + ABS(IFNULL(SAFE_CAST(for_pay AS NUMERIC), 0))
    + ABS(IFNULL(SAFE_CAST(logistics_amount AS NUMERIC), 0)) + ABS(IFNULL(SAFE_CAST(storage_fee AS NUMERIC), 0))
    + ABS(IFNULL(SAFE_CAST(deduction AS NUMERIC), 0)) + ABS(IFNULL(SAFE_CAST(penalty AS NUMERIC), 0))
    + ABS(IFNULL(SAFE_CAST(acceptance AS NUMERIC), 0)) + ABS(IFNULL(SAFE_CAST(additional_payment AS NUMERIC), 0))
    + ABS(IFNULL(SAFE_CAST(rebill_logistics AS NUMERIC), 0)) + ABS(IFNULL(SAFE_CAST(compensation_amount AS NUMERIC), 0))
    + ABS(IFNULL(SAFE_CAST(other_amount AS NUMERIC), 0)) abs_money_rub,
    srid, SAFE_CAST(wb_nm_id AS INT64) nm_id
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FINANCE_CANONICAL`),
t AS (
  SELECT f.*,
    CASE WHEN m.treatment IS NULL THEN 'NEW_FINANCE_OPERATION'
         WHEN f._rr_date NOT BETWEEN IFNULL(m.valid_from, DATE '1900-01-01') AND IFNULL(m.valid_to, DATE '9999-12-31') THEN 'PENDING_CLASSIFICATION'
         ELSE m.treatment END treatment
  FROM f LEFT JOIN `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_FINANCE_OPERATION_MAP` m USING (supplier_oper_name))
SELECT supplier_oper_name, treatment,
  MIN(_rr_date) first_seen, MAX(_rr_date) last_seen, COUNT(*) rows_n, ROUND(SUM(abs_money_rub), 2) abs_money_rub,
  COUNTIF(srid IS NOT NULL AND srid != '') rows_with_srid, COUNTIF(nm_id > 0) rows_with_nm,
  STRING_AGG(DISTINCT FORMAT_DATE('%Y-%m', DATE_TRUNC(_rr_date, MONTH)) ORDER BY FORMAT_DATE('%Y-%m', DATE_TRUNC(_rr_date, MONTH))) months
FROM t
WHERE treatment IN ('NEW_FINANCE_OPERATION', 'PENDING_CLASSIFICATION')
GROUP BY 1, 2;
