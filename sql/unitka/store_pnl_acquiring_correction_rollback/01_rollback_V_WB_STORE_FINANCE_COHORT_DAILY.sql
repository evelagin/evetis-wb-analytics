-- ОТКАТ store_pnl acquiring_correction: тело V_WB_STORE_FINANCE_COHORT_DAILY в production ДО изменения (git main 45a26503 = после #398, live = Git, проверено 2026-11-06)
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_STORE_FINANCE_COHORT_DAILY` AS
-- H0: доказанные исторические DIRECT_SKU-семейства читаются в когорту ТОЛЬКО в периоде своей карты:
--   «Коррекция продаж» — for_pay по знаку, база — цена×кол-во где цена > 0, кол-во — только если у srid нет «Продажи»,
--   «Корректировка эквайринга» — for_pay (зеркальный acquiring_fee не читается); «Стоимость участия…» — additional_payment.
WITH m AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_FINANCE_OPERATION_MAP`),
raw AS (
  SELECT supplier_oper_name op, finance_status, srid, _rr_date,
    DATE(SAFE.TIMESTAMP(order_dt), 'Europe/Moscow') order_date_msk,
    SAFE_CAST(wb_nm_id AS INT64) nm_id,
    SAFE_CAST(quantity AS NUMERIC) q, SAFE_CAST(retail_price_withdisc_rub AS NUMERIC) pwd,
    SAFE_CAST(for_pay AS NUMERIC) fp, SAFE_CAST(logistics_amount AS NUMERIC) lg,
    IFNULL(SAFE_CAST(additional_payment AS NUMERIC), 0) addp,
    COUNTIF(supplier_oper_name = 'Продажа') OVER (PARTITION BY srid) srid_sales,
    JSON_VALUE(raw_json, '$.bonusTypeName') bonus
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FINANCE_CANONICAL`
  WHERE srid IS NOT NULL AND srid != ''
    AND supplier_oper_name IN ('Продажа', 'Возврат', 'Логистика', 'Доставка', 'Коррекция логистики',
                               'Коррекция продаж', 'Корректировка эквайринга', 'Стоимость участия в программе лояльности')),
f AS (
  SELECT raw.* FROM raw JOIN m ON m.supplier_oper_name = raw.op
  WHERE m.treatment = 'DIRECT_SKU'
    -- Возврат неопознанного товара продавцу (обезличка при приёмке) — не когорта заказа покупателя: плечо идёт в
    -- журнал кабинета (V_WB_STORE_ACCOUNT_LEDGER, UNIDENTIFIED_RETURN). Только точное значение bonusTypeName.
    AND NOT (raw.op IN ('Логистика', 'Доставка') AND IFNULL(raw.bonus, '') = 'Возврат неопознанного товара (К продавцу)')
    AND raw._rr_date BETWEEN IFNULL(m.valid_from, DATE '1900-01-01') AND IFNULL(m.valid_to, DATE '9999-12-31'))
SELECT order_date_msk, nm_id,
  SUM(CASE op WHEN 'Продажа' THEN q WHEN 'Возврат' THEN -q
              WHEN 'Коррекция продаж' THEN IF(srid_sales = 0, IFNULL(q, 0), 0) ELSE 0 END) sold_qty,
  SUM(CASE op WHEN 'Продажа' THEN pwd * q WHEN 'Возврат' THEN -pwd * q
              WHEN 'Коррекция продаж' THEN IF(pwd > 0, pwd * IFNULL(q, 0), 0) ELSE 0 END) seller_base_rub,
  SUM(CASE op WHEN 'Продажа' THEN fp WHEN 'Возврат' THEN -fp
              WHEN 'Коррекция продаж' THEN fp WHEN 'Корректировка эквайринга' THEN fp
              WHEN 'Стоимость участия в программе лояльности' THEN -addp ELSE 0 END) for_pay_rub,
  SUM(CASE op WHEN 'Продажа' THEN pwd * q - fp WHEN 'Возврат' THEN -(pwd * q - fp)
              WHEN 'Коррекция продаж' THEN IF(pwd > 0, pwd * IFNULL(q, 0), 0) - fp
              WHEN 'Корректировка эквайринга' THEN -fp
              WHEN 'Стоимость участия в программе лояльности' THEN addp ELSE 0 END) wb_take_rub,
  SUM(IF(op IN ('Логистика', 'Доставка', 'Коррекция логистики'), IFNULL(lg, 0), 0)) logistics_rub,
  COUNTIF(finance_status = 'PROVISIONAL') provisional_rows
FROM f
WHERE order_date_msk IS NOT NULL AND nm_id IS NOT NULL AND nm_id > 0
GROUP BY 1, 2;
