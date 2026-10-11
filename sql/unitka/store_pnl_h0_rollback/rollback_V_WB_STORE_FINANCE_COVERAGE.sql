-- ROLLBACK (H0): тело production wb_mart.V_WB_STORE_FINANCE_COVERAGE ДО изменения H0, снято tables.get 2026-10-11 (Phase C, main 8753337).
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_STORE_FINANCE_COVERAGE` AS
WITH f AS (
  SELECT row_hash finance_row_key, _rr_date booking_date, supplier_oper_name op, finance_status,
    srid, SAFE.TIMESTAMP(order_dt) odt, SAFE_CAST(wb_nm_id AS INT64) nm_id,
    ABS(IFNULL(SAFE_CAST(retail_amount AS NUMERIC), 0)) retail, ABS(IFNULL(SAFE_CAST(for_pay AS NUMERIC), 0)) fp,
    ABS(IFNULL(SAFE_CAST(logistics_amount AS NUMERIC), 0)) lg, ABS(IFNULL(SAFE_CAST(storage_fee AS NUMERIC), 0)) st,
    ABS(IFNULL(SAFE_CAST(deduction AS NUMERIC), 0)) ded, ABS(IFNULL(SAFE_CAST(penalty AS NUMERIC), 0)) pen,
    ABS(IFNULL(SAFE_CAST(acceptance AS NUMERIC), 0)) acc, ABS(IFNULL(SAFE_CAST(additional_payment AS NUMERIC), 0)) addp,
    ABS(IFNULL(SAFE_CAST(rebill_logistics AS NUMERIC), 0)) rb, ABS(IFNULL(SAFE_CAST(compensation_amount AS NUMERIC), 0)) comp,
    ABS(IFNULL(SAFE_CAST(other_amount AS NUMERIC), 0)) oth, ABS(IFNULL(SAFE_CAST(acquiring_fee AS NUMERIC), 0)) acq,
    ABS(IFNULL(SAFE_CAST(JSON_VALUE(raw_json, '$.ppvzReward') AS NUMERIC), 0)) ppvz,
    -- Р2: возмещение = rebillLogisticCost + ppvzReward, погашено vw + vwNds до копеек, к выплате 0
    ABS(IFNULL(SAFE_CAST(JSON_VALUE(raw_json, '$.rebillLogisticCost') AS NUMERIC), 0)
      + IFNULL(SAFE_CAST(JSON_VALUE(raw_json, '$.ppvzReward') AS NUMERIC), 0)
      + IFNULL(SAFE_CAST(JSON_VALUE(raw_json, '$.vw') AS FLOAT64), 0)
      + IFNULL(SAFE_CAST(JSON_VALUE(raw_json, '$.vwNds') AS NUMERIC), 0)) memo_offset_abs
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FINANCE_CANONICAL`),
c AS (
  SELECT f.*,
    -- MEMO не наследуется: строка возмещения, не выполняющая тождество Р2, — PENDING_CLASSIFICATION до доказательства
    CASE WHEN m.treatment = 'MEMO_NON_PNL' AND (f.memo_offset_abs > 0.01 OR f.fp != 0) THEN 'PENDING_CLASSIFICATION'
         ELSE IFNULL(m.treatment, 'NEW_FINANCE_OPERATION') END treatment,
    CASE WHEN m.treatment IN ('PENDING_CLASSIFICATION') OR m.treatment IS NULL THEN 'NONE'
         WHEN m.treatment = 'MEMO_NON_PNL' AND (f.memo_offset_abs > 0.01 OR f.fp != 0) THEN 'NONE'
         WHEN f.op IN ('Продажа', 'Возврат', 'Логистика', 'Доставка', 'Коррекция логистики') THEN 'COHORT'
         WHEN f.op IN ('Хранение', 'Коррекция хранения') THEN 'STORAGE'
         WHEN f.op = 'Удержание' THEN 'DEDUCTION_LEDGER'
         WHEN f.op IN ('Штраф', 'Платная приемка', 'Пересчет платной приемки') THEN 'ACCOUNT_LEDGER'
         WHEN m.treatment = 'MEMO_NON_PNL' THEN 'MEMO'
         WHEN m.treatment = 'MEMO_ZERO' THEN 'MEMO_ZERO'
         ELSE 'NONE' END consumer
  FROM f LEFT JOIN `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_FINANCE_OPERATION_MAP` m ON m.supplier_oper_name = f.op)
SELECT finance_row_key, booking_date, op supplier_oper_name, treatment, consumer, finance_status,
  CASE consumer
    WHEN 'COHORT' THEN IF(srid IS NULL OR srid = '' OR odt IS NULL OR IFNULL(nm_id, 0) <= 0, fp + lg, 0)
                       + st + ded + pen + acc + addp + rb + comp + oth
    WHEN 'STORAGE' THEN retail + fp + lg + ded + pen + acc + addp + rb + comp + oth + acq + ppvz
    WHEN 'DEDUCTION_LEDGER' THEN retail + fp + lg + st + pen + acc + rb + comp + oth + acq + ppvz
    WHEN 'ACCOUNT_LEDGER' THEN retail + fp + lg + st + ded + addp + rb + comp + oth + acq + ppvz
    WHEN 'MEMO' THEN retail + fp + lg + st + ded + pen + acc + addp + comp + oth + acq
    ELSE retail + fp + lg + st + ded + pen + acc + addp + rb + comp + oth + acq + ppvz END unconsumed_abs_rub
FROM c;
