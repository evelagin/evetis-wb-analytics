-- ROLLBACK (H0): тело production wb_mart.V_WB_FINANCE_NEW_OPERATIONS ДО изменения H0, снято tables.get 2026-10-11 (Phase C, main 8753337).
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_FINANCE_NEW_OPERATIONS` AS
WITH f AS (
  SELECT supplier_oper_name, _rr_date,
    ABS(IFNULL(SAFE_CAST(retail_amount AS NUMERIC), 0)) + ABS(IFNULL(SAFE_CAST(for_pay AS NUMERIC), 0))
    + ABS(IFNULL(SAFE_CAST(logistics_amount AS NUMERIC), 0)) + ABS(IFNULL(SAFE_CAST(storage_fee AS NUMERIC), 0))
    + ABS(IFNULL(SAFE_CAST(deduction AS NUMERIC), 0)) + ABS(IFNULL(SAFE_CAST(penalty AS NUMERIC), 0))
    + ABS(IFNULL(SAFE_CAST(acceptance AS NUMERIC), 0)) + ABS(IFNULL(SAFE_CAST(additional_payment AS NUMERIC), 0))
    + ABS(IFNULL(SAFE_CAST(rebill_logistics AS NUMERIC), 0)) + ABS(IFNULL(SAFE_CAST(compensation_amount AS NUMERIC), 0))
    + ABS(IFNULL(SAFE_CAST(other_amount AS NUMERIC), 0)) abs_money_rub,
    srid, SAFE_CAST(wb_nm_id AS INT64) nm_id
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FINANCE_CANONICAL`)
SELECT f.supplier_oper_name, IFNULL(m.treatment, 'NEW_FINANCE_OPERATION') treatment,
  MIN(f._rr_date) first_seen, MAX(f._rr_date) last_seen, COUNT(*) rows_n, ROUND(SUM(f.abs_money_rub), 2) abs_money_rub,
  COUNTIF(f.srid IS NOT NULL AND f.srid != '') rows_with_srid, COUNTIF(f.nm_id > 0) rows_with_nm,
  STRING_AGG(DISTINCT FORMAT_DATE('%Y-%m', DATE_TRUNC(f._rr_date, MONTH)) ORDER BY FORMAT_DATE('%Y-%m', DATE_TRUNC(f._rr_date, MONTH))) months
FROM f LEFT JOIN `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_FINANCE_OPERATION_MAP` m USING (supplier_oper_name)
WHERE m.treatment IS NULL OR m.treatment = 'PENDING_CLASSIFICATION'
GROUP BY 1, 2;
