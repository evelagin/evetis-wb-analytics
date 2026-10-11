-- ROLLBACK (H0): тело production wb_mart.V_WB_STORE_FINANCE_COHORT_DAILY ДО изменения H0, снято tables.get 2026-10-11 (Phase C, main 8753337).
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_STORE_FINANCE_COHORT_DAILY` AS
WITH f AS (
  SELECT supplier_oper_name op, finance_status,
    DATE(SAFE.TIMESTAMP(order_dt), 'Europe/Moscow') order_date_msk,
    SAFE_CAST(wb_nm_id AS INT64) nm_id,
    SAFE_CAST(quantity AS NUMERIC) q, SAFE_CAST(retail_price_withdisc_rub AS NUMERIC) pwd,
    SAFE_CAST(for_pay AS NUMERIC) fp, SAFE_CAST(logistics_amount AS NUMERIC) lg
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FINANCE_CANONICAL`
  WHERE srid IS NOT NULL AND srid != ''
    AND supplier_oper_name IN ('Продажа', 'Возврат', 'Логистика', 'Доставка', 'Коррекция логистики'))
SELECT order_date_msk, nm_id,
  SUM(CASE op WHEN 'Продажа' THEN q WHEN 'Возврат' THEN -q ELSE 0 END) sold_qty,
  SUM(CASE op WHEN 'Продажа' THEN pwd * q WHEN 'Возврат' THEN -pwd * q ELSE 0 END) seller_base_rub,
  SUM(CASE op WHEN 'Продажа' THEN fp WHEN 'Возврат' THEN -fp ELSE 0 END) for_pay_rub,
  SUM(CASE op WHEN 'Продажа' THEN pwd * q - fp WHEN 'Возврат' THEN -(pwd * q - fp) ELSE 0 END) wb_take_rub,
  SUM(IF(op IN ('Логистика', 'Доставка', 'Коррекция логистики'), IFNULL(lg, 0), 0)) logistics_rub,
  COUNTIF(finance_status = 'PROVISIONAL') provisional_rows
FROM f
WHERE order_date_msk IS NOT NULL AND nm_id IS NOT NULL AND nm_id > 0
GROUP BY 1, 2;
