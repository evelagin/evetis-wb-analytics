-- ============================================================================
-- PR-A · Регрессионный контроль финансового контура
-- Назначение: доказать, что после правок ingestion канон НЕ сдвинулся.
-- Эталон — контрольные цифры REV2.4 (артефакт evetis-wb-finance-rev2, 11.08.2026).
--
-- РЕЖИМ: read-only. Ни одной записи. Запускать ДО правки и ПОСЛЕ.
-- Критерий приёмки PR-A: все строки verdict = 'PASS'.
--
-- ⚠️ Эталонные суммы зафиксированы на срезе 11.08.2026 (данные по 10.08.2026
--    включительно). При прогоне позже свежие дни изменят накопительные итоги —
--    поэтому все денежные проверки ограничены окном _rr_date <= '2026-08-10'.
-- ============================================================================

WITH src AS (
  SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FINANCE_CANONICAL`
  WHERE _rr_date <= DATE '2026-08-10'
),
raw_src AS (
  SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_FINANCE`
  WHERE _rr_date <= DATE '2026-08-10'
),
-- payload_hash: только источниковые поля. НЕ включает report_id, run_id,
-- loaded_at, load_id, report_period, report_type — этим отличается от row_hash.
payload AS (
  SELECT rrd_id, TO_HEX(MD5(CONCAT(
      IFNULL(supplier_oper_name,'~'),'|',IFNULL(doc_type_name,'~'),'|',IFNULL(srid,'~'),'|',
      IFNULL(wb_nm_id,'~'),'|',IFNULL(barcode,'~'),'|',IFNULL(sa_name,'~'),'|',IFNULL(ts_name,'~'),'|',
      IFNULL(order_dt,'~'),'|',IFNULL(sale_dt,'~'),'|',IFNULL(rr_dt,'~'),'|',
      IFNULL(quantity,'~'),'|',IFNULL(retail_price,'~'),'|',IFNULL(retail_amount,'~'),'|',
      IFNULL(retail_price_withdisc_rub,'~'),'|',IFNULL(sale_percent,'~'),'|',
      IFNULL(commission_percent,'~'),'|',IFNULL(spp_percent,'~'),'|',IFNULL(for_pay,'~'),'|',
      IFNULL(acquiring_fee,'~'),'|',IFNULL(logistics_amount,'~'),'|',IFNULL(rebill_logistics,'~'),'|',
      IFNULL(storage_fee,'~'),'|',IFNULL(deduction,'~'),'|',IFNULL(penalty,'~'),'|',
      IFNULL(acceptance,'~'),'|',IFNULL(additional_payment,'~'),'|',IFNULL(office_name,'~'),'|',
      IFNULL(warehouse_name,'~'),'|',IFNULL(region_name,'~')
  ))) payload_hash
  FROM raw_src
),
-- классификация логистики: 5 классов, fail-closed
sale_ev   AS (SELECT DISTINCT srid, sale_dt FROM src WHERE supplier_oper_name='Продажа'),
ret_ev    AS (SELECT DISTINCT srid, sale_dt FROM src WHERE supplier_oper_name='Возврат'),
first_sale AS (SELECT srid, MIN(sale_dt) sale_ts FROM src WHERE supplier_oper_name='Продажа' GROUP BY srid),
logi AS (
  SELECT SAFE_CAST(l.logistics_amount AS FLOAT64) log,
    CASE
      WHEN se.srid IS NOT NULL          THEN 'FORWARD'
      WHEN re.srid IS NOT NULL          THEN 'RETURN_AFTER_SALE'
      WHEN fs.srid IS NULL              THEN 'NON_BUYOUT_LOGISTICS'
      WHEN l.sale_dt < fs.sale_ts       THEN 'CANCELLED_DELIVERY'
      ELSE 'UNKNOWN'
    END klass
  FROM src l
  LEFT JOIN sale_ev   se ON se.srid=l.srid AND se.sale_dt=l.sale_dt
  LEFT JOIN ret_ev    re ON re.srid=l.srid AND re.sale_dt=l.sale_dt
  LEFT JOIN first_sale fs ON fs.srid=l.srid
  WHERE l.supplier_oper_name='Логистика'
),
m AS (
  SELECT
    (SELECT COUNT(*) FROM src) rows_canon,
    (SELECT COUNT(DISTINCT rrd_id) FROM src) rrd_canon,
    (SELECT COUNT(*) FROM raw_src) rows_raw,
    (SELECT COUNT(DISTINCT CONCAT(report_id,'|',rrd_id)) FROM raw_src) raw_pk,
    (SELECT COUNT(*) FROM (SELECT rrd_id FROM payload GROUP BY rrd_id
                           HAVING COUNT(DISTINCT payload_hash)>1)) natkey_violations,
    (SELECT COUNTIF(supplier_oper_name='Продажа') FROM src) units,
    (SELECT ROUND(SUM(IF(supplier_oper_name='Продажа',SAFE_CAST(retail_price_withdisc_rub AS FLOAT64),0)),2) FROM src) sale_srev,
    (SELECT ROUND(SUM(IF(supplier_oper_name='Продажа',SAFE_CAST(for_pay AS FLOAT64),0)),2) FROM src) sale_forpay,
    (SELECT ROUND(SUM(IF(supplier_oper_name='Продажа',IFNULL(SAFE_CAST(acquiring_fee AS FLOAT64),0),0)),2) FROM src) acq,
    (SELECT ROUND(SUM(IF(supplier_oper_name='Логистика',SAFE_CAST(logistics_amount AS FLOAT64),0)),2) FROM src) log_gross,
    (SELECT ROUND(SUM(IF(supplier_oper_name='Хранение',SAFE_CAST(storage_fee AS FLOAT64),0)),2) FROM src) storage,
    (SELECT ROUND(SUM(IF(supplier_oper_name LIKE '%риемк%',SAFE_CAST(acceptance AS FLOAT64),0)),2) FROM src) acceptance,
    (SELECT ROUND(SUM(IF(supplier_oper_name='Штраф',SAFE_CAST(penalty AS FLOAT64),0)),2) FROM src) penalty,
    (SELECT ROUND(SUM(IF(supplier_oper_name='Удержание',SAFE_CAST(deduction AS FLOAT64),0)),2) FROM src) deduction,
    (SELECT COUNTIF(supplier_oper_name='Удержание') FROM src) ded_rows,
    (SELECT ROUND(SUM(IFNULL(SAFE_CAST(rebill_logistics AS FLOAT64),0)),2) FROM src) rebill,
    (SELECT ROUND(SUM(IF(klass='FORWARD',log,0)),2) FROM logi) log_fwd,
    (SELECT COUNTIF(klass='FORWARD') FROM logi) log_fwd_rows,
    (SELECT ROUND(SUM(IF(klass<>'FORWARD',log,0)),2) FROM logi) log_ret,
    (SELECT COUNTIF(klass='UNKNOWN') FROM logi) log_unknown_rows
),
checks AS (
  SELECT c.* FROM m, UNNEST([
    STRUCT('01 CANON: строк'                          AS check_name, CAST(m.rows_canon AS STRING) AS actual, '203736' AS expected),
    STRUCT('02 CANON: уникальных rrd_id',                   CAST(m.rrd_canon AS STRING),              '203736'),
    STRUCT('03 CANON: дублей rrd_id НЕТ (fail-closed)',     CAST(m.rows_canon-m.rrd_canon AS STRING), '0'),
    STRUCT('04 RAW: PK (report_id, rrd_id) не нарушен',     CAST(m.rows_raw-m.raw_pk AS STRING),      '0'),
    STRUCT('05 NATURAL KEY: rrd_id с разным payload',       CAST(m.natkey_violations AS STRING),      '0'),
    STRUCT('06 Проданных единиц',                           CAST(m.units AS STRING),                  '39139'),
    STRUCT('07 Реализация «Продажа»',                       FORMAT('%.2f', m.sale_srev),              '26674088.76'),
    STRUCT('08 for_pay «Продажа»',                          FORMAT('%.2f', m.sale_forpay),            '18777190.75'),
    STRUCT('09 Эквайринг',                                  FORMAT('%.2f', m.acq),                    '444751.92'),
    STRUCT('10 Логистика gross',                            FORMAT('%.2f', m.log_gross),              '2754251.97'),
    STRUCT('11 Логистика FORWARD',                          FORMAT('%.2f', m.log_fwd),                '2399116.99'),
    STRUCT('12 FORWARD-строк = проданных единиц',           CAST(m.log_fwd_rows AS STRING),           '39139'),
    STRUCT('13 Логистика RETURN (3 класса)',                FORMAT('%.2f', m.log_ret),                '355134.98'),
    STRUCT('14 Логистика UNKNOWN (fail-closed)',            CAST(m.log_unknown_rows AS STRING),       '0'),
    STRUCT('15 Хранение',                                   FORMAT('%.2f', m.storage),                '409307.65'),
    STRUCT('16 Платная приёмка',                            FORMAT('%.2f', m.acceptance),             '116587.80'),
    STRUCT('17 Штрафы',                                     FORMAT('%.2f', m.penalty),                '28314.00'),
    STRUCT('18 «Удержание», ₽',                             FORMAT('%.2f', m.deduction),              '4599011.91'),
    STRUCT('19 «Удержание», строк',                         CAST(m.ded_rows AS STRING),               '368'),
    STRUCT('20 rebill (информационная)',                    FORMAT('%.2f', m.rebill),                 '377374.39')
  ]) c
)
SELECT check_name, expected, actual,
       IF(actual = expected, 'PASS', '🔴 FAIL') verdict
FROM checks
ORDER BY check_name;
