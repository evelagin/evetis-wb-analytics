-- ОТКАТ store_pnl acquiring_correction: тело V_WB_STORE_FINANCE_COVERAGE в production ДО изменения (git main 45a26503 = после #398, live = Git, проверено 2026-11-06)
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_STORE_FINANCE_COVERAGE` AS
-- Р2-guard по схеме ключей raw_json (H0, OWNER ACK 11.10.2026):
--   FINANCE_API_CAMEL (WEEKLY/DAILY) — |rebillLogisticCost + ppvzReward + vw + vwNds| ≤ 0,01 (как раньше, 0 различий
--     на всех строках с 13.07.2026),
--   LEGACY_SNAKE — rebill_logistic_cost + ppvz_reward = −vw × НДС (1,20 при дате < 2026-01-01, иначе 1,22),
--   LEGACY ПВЗ — поля вознаграждения в импорте нет: MEMO только при НУЛЕВОМ денежном эффекте строки
--     (for_pay, ppvz_for_pay и все сохранённые денежные поля = 0), иначе PENDING,
--   иначе (нет ключей, смешанная схема) — PENDING (fail closed).
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
    SAFE_CAST(JSON_VALUE(raw_json, '$.rebillLogisticCost') AS NUMERIC) c_rb,
    SAFE_CAST(JSON_VALUE(raw_json, '$.ppvzReward') AS NUMERIC) c_pr,
    JSON_VALUE(raw_json, '$.bonusTypeName') bonus,
    SAFE_CAST(JSON_VALUE(raw_json, '$.vw') AS FLOAT64) c_vw,
    SAFE_CAST(JSON_VALUE(raw_json, '$.vwNds') AS NUMERIC) c_nds,
    SAFE_CAST(JSON_VALUE(raw_json, '$.rebill_logistic_cost') AS NUMERIC) s_rb,
    SAFE_CAST(JSON_VALUE(raw_json, '$.ppvz_reward') AS NUMERIC) s_pr,
    SAFE_CAST(JSON_VALUE(raw_json, '$.ppvz_vw') AS FLOAT64) s_vw,
    SAFE_CAST(JSON_VALUE(raw_json, '$.ppvz_vw_nds') AS NUMERIC) s_nds,
    SAFE_CAST(JSON_VALUE(raw_json, '$.ppvz_for_pay') AS NUMERIC) s_fp,
    (JSON_VALUE(raw_json, '$.rebillLogisticCost') IS NOT NULL OR JSON_VALUE(raw_json, '$.ppvzReward') IS NOT NULL
      OR JSON_VALUE(raw_json, '$.vw') IS NOT NULL OR JSON_VALUE(raw_json, '$.vwNds') IS NOT NULL) any_camel,
    (JSON_VALUE(raw_json, '$.rebill_logistic_cost') IS NOT NULL OR JSON_VALUE(raw_json, '$.ppvz_vw') IS NOT NULL
      OR JSON_VALUE(raw_json, '$.ppvz_for_pay') IS NOT NULL) any_snake
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FINANCE_CANONICAL`),
g AS (
  SELECT f.*,
    CASE WHEN NOT any_snake AND c_rb IS NOT NULL AND c_pr IS NOT NULL AND c_vw IS NOT NULL AND c_nds IS NOT NULL THEN 'FINANCE_API_CAMEL'
         WHEN NOT any_camel AND s_rb IS NOT NULL AND s_vw IS NOT NULL AND s_fp IS NOT NULL AND booking_date IS NOT NULL THEN 'LEGACY_SNAKE'
         ELSE 'UNKNOWN' END memo_schema
  FROM f),
h AS (
  SELECT g.*,
    CASE memo_schema
      WHEN 'FINANCE_API_CAMEL' THEN ABS(IFNULL(c_rb, 0) + IFNULL(c_pr, 0) + IFNULL(c_vw, 0) + IFNULL(c_nds, 0))
      WHEN 'LEGACY_SNAKE' THEN ABS(s_rb + IFNULL(s_pr, 0)
        + IF(s_nds IS NOT NULL, s_vw + s_nds, s_vw * IF(booking_date < DATE '2026-01-01', 1.20, 1.22)))
      ELSE NULL END memo_offset_abs,
    -- нулевой денежный эффект: ни одно сохранённое денежное поле строки не несёт денег продавцу
    (fp + retail + lg + st + ded + pen + acc + addp + rb + comp + oth + acq + ppvz = 0 AND IFNULL(s_fp, 0) = 0
      AND IFNULL(s_rb, 0) = 0 AND IFNULL(s_pr, 0) = 0) zero_cash
  FROM g),
gd AS (
  SELECT h.*,
    CASE
      WHEN op = 'Возмещение за выдачу и возврат товаров на ПВЗ' AND memo_schema = 'LEGACY_SNAKE' AND s_pr IS NULL
        THEN IF(zero_cash, 'LEGACY_ZERO_CASH', NULL)
      WHEN memo_schema = 'UNKNOWN' OR memo_offset_abs IS NULL OR memo_offset_abs > 0.01 OR fp != 0
        OR (memo_schema = 'LEGACY_SNAKE' AND IFNULL(s_fp, 0) != 0) THEN NULL
      ELSE IF(memo_schema = 'FINANCE_API_CAMEL', 'IDENTITY_CAMEL', 'IDENTITY_LEGACY_VAT') END memo_basis
  FROM h),
c AS (
  SELECT gd.*, m.treatment map_treatment,
    gd.booking_date BETWEEN IFNULL(m.valid_from, DATE '1900-01-01') AND IFNULL(m.valid_to, DATE '9999-12-31') in_period
  FROM gd LEFT JOIN `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_FINANCE_OPERATION_MAP` m ON m.supplier_oper_name = gd.op),
t AS (
  SELECT c.*,
    CASE WHEN map_treatment IS NULL THEN 'NEW_FINANCE_OPERATION'
         WHEN NOT in_period THEN 'PENDING_CLASSIFICATION'
         -- MEMO не наследуется: возмещение без доказанного основания — PENDING_CLASSIFICATION
         WHEN map_treatment = 'MEMO_NON_PNL' AND op LIKE 'Возмещение%' AND memo_basis IS NULL THEN 'PENDING_CLASSIFICATION'
         ELSE map_treatment END treatment
  FROM c)
SELECT finance_row_key, booking_date, op supplier_oper_name, treatment, consumer, finance_status, memo_schema, memo_basis,
  CASE consumer
    WHEN 'COHORT' THEN IF(srid IS NULL OR srid = '' OR odt IS NULL OR IFNULL(nm_id, 0) <= 0, fp + lg + IF(op = 'Стоимость участия в программе лояльности', addp, 0), 0)
                       + st + ded + pen + acc + IF(op = 'Стоимость участия в программе лояльности', 0, addp) + rb + comp + oth
                       -- зеркало эквайринга у «Корректировки эквайринга» — не деньги модели
    WHEN 'STORAGE' THEN retail + fp + lg + ded + pen + acc + addp + rb + comp + oth + acq + ppvz
    WHEN 'DEDUCTION_LEDGER' THEN retail + fp + lg + st + pen + acc + rb + comp + oth + acq + ppvz
    WHEN 'ACCOUNT_LEDGER_LOGISTICS' THEN retail + fp + st + ded + pen + acc + addp + rb + comp + oth + acq + ppvz
    WHEN 'ACCOUNT_LEDGER' THEN retail + lg + st + ded + addp + rb + comp + oth + acq + ppvz
                       + IF(op IN ('Добровольная компенсация при возврате', 'Компенсация ущерба'), 0, fp)
    WHEN 'MEMO' THEN retail + fp + lg + st + ded + pen + acc + addp + comp + oth + acq
    WHEN 'MEMO_ZERO' THEN retail + fp + lg + st + ded + pen + acc + addp + rb + comp + oth + acq + ppvz
    ELSE retail + fp + lg + st + ded + pen + acc + addp + rb + comp + oth + acq + ppvz END unconsumed_abs_rub
FROM (
  SELECT t.*,
    CASE WHEN treatment IN ('PENDING_CLASSIFICATION', 'NEW_FINANCE_OPERATION') THEN 'NONE'
         WHEN op IN ('Логистика', 'Доставка') AND IFNULL(bonus, '') = 'Возврат неопознанного товара (К продавцу)' THEN 'ACCOUNT_LEDGER_LOGISTICS'
         WHEN op IN ('Продажа', 'Возврат', 'Логистика', 'Доставка', 'Коррекция логистики',
                     'Коррекция продаж', 'Корректировка эквайринга', 'Стоимость участия в программе лояльности') THEN 'COHORT'
         WHEN op IN ('Хранение', 'Коррекция хранения') THEN 'STORAGE'
         WHEN op = 'Удержание' THEN 'DEDUCTION_LEDGER'
         WHEN op IN ('Штраф', 'Платная приемка', 'Пересчет платной приемки',
                     'Добровольная компенсация при возврате', 'Компенсация ущерба') THEN 'ACCOUNT_LEDGER'
         WHEN treatment = 'MEMO_NON_PNL' THEN 'MEMO'
         WHEN treatment = 'MEMO_ZERO' THEN 'MEMO_ZERO'
         ELSE 'NONE' END consumer
  FROM t);
