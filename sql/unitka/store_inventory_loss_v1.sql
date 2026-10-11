-- ============================================================================
-- WB FIRE / LOST INVENTORY (OWNER ACK 2026-10-11) — товар EVETIS, утраченный на складах WB при атаках БПЛА 18.07–16.08.2026.
-- Слой доказательства и списания для P&L магазина: списание утраченного товара — РАСХОД месяца события (по канонической
-- себестоимости на дату события), компенсация WB — ДОХОД месяца её проводки; обе строки видны отдельно, не неттуются.
-- Док: docs/finance/WB_FIRE_LOSS_2026-10-11.md.
--
-- Количества — поштучно из отчётов ООО «РВБ» «Отчёт по утраченному товару в результате обстоятельств непреодолимой
-- силы» от 28–29.08.2026 (9 складов, 131 позиция склад × nm, 3 460 ед.; построчно сверены с PDF-отчётами 01.09.2026).
-- Не реконструированы из дельт остатков. Не вошли (не доказаны отчётом WB): 47 ед., списанных WB 26.08 без
-- отчёта, и 2 ед. СЦ Ереван (склад работал — заказы 01.08 и 19.08).
-- Дата события — склад за складом: публичное сообщение об атаке + наш ряд (последний заказ со склада, остаток неподвижен).
-- 🔴 Без evetis_ref (1C1B/1C2A): nm → SKU — wb_raw.REF_SKU_MASTER, себестоимость — wb_mart.UNITKA_COGS_EFFECTIVE; ровно
-- один интервал на дату события, иначе себестоимость неизвестна (не 0) и месяц не закрывается.
-- ============================================================================

-- ─── L1. Позиции утраты: склад × nm × количество отчёта WB, себестоимость на дату события ─────────────────────────
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_INVENTORY_LOSS_ITEMS` AS
WITH incidents AS (
  SELECT * FROM UNNEST(
  ARRAY<STRUCT<incident_id STRING, report_warehouse STRING, wb_report STRING, incident_date DATE, orders_warehouse STRING, evidence STRING>>[
    ('FM-2026-07-18-ELS', 'Электросталь', 'damaged-product-report-526411', DATE '2026-07-18', 'Электросталь', 'атака БПЛА в ночь на 18.07 (РБК, ДП); последний заказ со склада 17.07, остаток неподвижен с 21.07'),
    ('FM-2026-07-22-KRD', 'Краснодар', 'damaged-product-report-625145', DATE '2026-07-22', 'Краснодар', 'атака БПЛА в ночь на 22.07 (DW, Коммерсантъ); последний заказ 21.07, остаток неподвижен с 23.07'),
    ('FM-2026-07-24-SHU', 'СПБ Шушары', 'damaged-product-report-1338585', DATE '2026-07-24', 'СПБ Шушары Московское', 'атака БПЛА в ночь на 24.07 (РБК, Интерфакс); последний заказ 23.07, остаток неподвижен с 24.07'),
    ('FM-2026-07-24-SIM', 'СЦ Симферополь (Молодежненское)', 'damaged-product-report-1389895', DATE '2026-07-24', CAST(NULL AS STRING), 'атака БПЛА в ночь на 24.07 (РБК, КП); 1 ед. в остатках 24–28.07, позже нет'),
    ('FM-2026-07-30-SAR', 'Сарапул', 'damaged-product-report-967646', DATE '2026-07-30', 'Сарапул', 'атака БПЛА 30.07 (РБК, Коммерсантъ); последний заказ 30.07'),
    ('FM-2026-07-31-VLG', 'Волгоград', 'damaged-product-report-683408', DATE '2026-07-31', 'Волгоградская обл.', 'атака БПЛА в ночь на 31.07 (Ведомости, РБК); последний заказ 30.07'),
    ('FM-2026-08-02-SMR', 'Самара (Новосемейкино)', 'damaged-product-report-1108186', DATE '2026-08-02', 'Самарская обл.', 'атака БПЛА 02.08, «товары уничтожены» (ДП, Moscow Times); последний заказ 01.08'),
    ('FM-2026-08-05-TUL', 'Тула', 'damaged-product-report-982197', DATE '2026-08-05', 'Алексин', 'атака БПЛА в ночь на 05.08, СЦ в Алексине (Коммерсантъ, DW); последний заказ 05.08'),
    ('FM-2026-08-16-KLD', 'Коледино', 'damaged-product-report-357799', DATE '2026-08-16', 'Коледино', 'атака БПЛА в ночь на 16.08 (Expert, РБК); последний заказ 13.08, остаток двигался до 15.08')
  ])),
items AS (
  SELECT * FROM UNNEST(ARRAY<STRUCT<incident_id STRING, nm_id INT64, lost_qty INT64>>[
    ('FM-2026-08-16-KLD', 252442517, 342), ('FM-2026-08-16-KLD', 438775617, 389), ('FM-2026-08-16-KLD', 305101361, 197), ('FM-2026-08-16-KLD', 535580776, 57),
    ('FM-2026-08-16-KLD', 567668635, 28), ('FM-2026-08-16-KLD', 438775437, 58), ('FM-2026-08-16-KLD', 535581675, 27), ('FM-2026-08-16-KLD', 868597351, 8),
    ('FM-2026-08-16-KLD', 535581674, 24), ('FM-2026-08-16-KLD', 930334395, 6), ('FM-2026-08-16-KLD', 930334397, 2), ('FM-2026-08-16-KLD', 910330849, 1),
    ('FM-2026-08-16-KLD', 773170315, 1), ('FM-2026-08-16-KLD', 952068582, 1), ('FM-2026-08-05-TUL', 252442517, 225), ('FM-2026-08-05-TUL', 438775617, 166),
    ('FM-2026-08-05-TUL', 535580776, 70), ('FM-2026-08-05-TUL', 567668635, 29), ('FM-2026-08-05-TUL', 305101361, 51), ('FM-2026-08-05-TUL', 773170315, 20),
    ('FM-2026-08-05-TUL', 535581675, 34), ('FM-2026-08-05-TUL', 535581674, 31), ('FM-2026-08-05-TUL', 868597351, 9), ('FM-2026-08-05-TUL', 773170316, 10),
    ('FM-2026-08-05-TUL', 305101272, 24), ('FM-2026-08-05-TUL', 952068582, 8), ('FM-2026-08-05-TUL', 930334395, 4), ('FM-2026-08-05-TUL', 910584041, 3),
    ('FM-2026-08-05-TUL', 930334396, 3), ('FM-2026-08-05-TUL', 593111986, 7), ('FM-2026-08-05-TUL', 930334397, 1), ('FM-2026-08-05-TUL', 910330849, 1),
    ('FM-2026-08-05-TUL', 1083392113, 1), ('FM-2026-08-05-TUL', 593111985, 1), ('FM-2026-07-18-ELS', 438775617, 202), ('FM-2026-07-18-ELS', 535581675, 121),
    ('FM-2026-07-18-ELS', 535580776, 136), ('FM-2026-07-18-ELS', 535581674, 96), ('FM-2026-07-18-ELS', 305101361, 76), ('FM-2026-07-18-ELS', 567668635, 19),
    ('FM-2026-07-18-ELS', 868597351, 7), ('FM-2026-07-18-ELS', 930334395, 4), ('FM-2026-07-18-ELS', 910584041, 3), ('FM-2026-07-18-ELS', 952068582, 4),
    ('FM-2026-07-18-ELS', 305101272, 9), ('FM-2026-07-18-ELS', 930334396, 2), ('FM-2026-07-18-ELS', 930334397, 1), ('FM-2026-07-22-KRD', 252442517, 66),
    ('FM-2026-07-22-KRD', 438775617, 98), ('FM-2026-07-22-KRD', 535580776, 66), ('FM-2026-07-22-KRD', 567668635, 32), ('FM-2026-07-22-KRD', 535581674, 27),
    ('FM-2026-07-22-KRD', 593111985, 24), ('FM-2026-07-22-KRD', 305101272, 30), ('FM-2026-07-22-KRD', 535581675, 17), ('FM-2026-07-22-KRD', 868597351, 5),
    ('FM-2026-07-22-KRD', 593111986, 13), ('FM-2026-07-22-KRD', 305101361, 15), ('FM-2026-07-22-KRD', 910584041, 4), ('FM-2026-07-22-KRD', 910330849, 5),
    ('FM-2026-07-22-KRD', 930334397, 4), ('FM-2026-07-22-KRD', 1083392113, 4), ('FM-2026-07-22-KRD', 773170315, 3), ('FM-2026-07-22-KRD', 930334395, 1),
    ('FM-2026-08-02-SMR', 252442517, 20), ('FM-2026-08-02-SMR', 868597351, 5), ('FM-2026-08-02-SMR', 305101272, 20), ('FM-2026-08-02-SMR', 305101361, 20),
    ('FM-2026-08-02-SMR', 438775617, 20), ('FM-2026-08-02-SMR', 910584041, 5), ('FM-2026-08-02-SMR', 930334395, 5), ('FM-2026-08-02-SMR', 930334396, 5),
    ('FM-2026-08-02-SMR', 567668635, 7), ('FM-2026-08-02-SMR', 535581674, 10), ('FM-2026-08-02-SMR', 535581675, 10), ('FM-2026-08-02-SMR', 930334397, 5),
    ('FM-2026-08-02-SMR', 535580776, 10), ('FM-2026-08-02-SMR', 593111985, 7), ('FM-2026-08-02-SMR', 593111986, 6), ('FM-2026-08-02-SMR', 910330849, 3),
    ('FM-2026-08-02-SMR', 1083392113, 3), ('FM-2026-08-02-SMR', 952068582, 3), ('FM-2026-08-02-SMR', 773170316, 1), ('FM-2026-07-30-SAR', 252442517, 20),
    ('FM-2026-07-30-SAR', 868597351, 5), ('FM-2026-07-30-SAR', 305101272, 20), ('FM-2026-07-30-SAR', 305101361, 20), ('FM-2026-07-30-SAR', 438775617, 20),
    ('FM-2026-07-30-SAR', 910584041, 5), ('FM-2026-07-30-SAR', 930334395, 5), ('FM-2026-07-30-SAR', 930334396, 5), ('FM-2026-07-30-SAR', 567668635, 7),
    ('FM-2026-07-30-SAR', 535581675, 11), ('FM-2026-07-30-SAR', 930334397, 5), ('FM-2026-07-30-SAR', 535581674, 9), ('FM-2026-07-30-SAR', 535580776, 10),
    ('FM-2026-07-30-SAR', 593111985, 6), ('FM-2026-07-30-SAR', 593111986, 6), ('FM-2026-07-30-SAR', 910330849, 3), ('FM-2026-07-30-SAR', 1083392113, 3),
    ('FM-2026-07-30-SAR', 952068582, 3), ('FM-2026-07-30-SAR', 773170316, 2), ('FM-2026-07-31-VLG', 252442517, 18), ('FM-2026-07-31-VLG', 438775617, 21),
    ('FM-2026-07-31-VLG', 305101361, 20), ('FM-2026-07-31-VLG', 868597351, 4), ('FM-2026-07-31-VLG', 930334396, 6), ('FM-2026-07-31-VLG', 305101272, 16),
    ('FM-2026-07-31-VLG', 930334395, 5), ('FM-2026-07-31-VLG', 567668635, 7), ('FM-2026-07-31-VLG', 535581675, 10), ('FM-2026-07-31-VLG', 910584041, 4),
    ('FM-2026-07-31-VLG', 930334397, 5), ('FM-2026-07-31-VLG', 535581674, 9), ('FM-2026-07-31-VLG', 535580776, 10), ('FM-2026-07-31-VLG', 593111986, 6),
    ('FM-2026-07-31-VLG', 910330849, 3), ('FM-2026-07-31-VLG', 1083392113, 3), ('FM-2026-07-31-VLG', 952068582, 3), ('FM-2026-07-31-VLG', 593111985, 5),
    ('FM-2026-07-31-VLG', 773170316, 1), ('FM-2026-07-24-SHU', 593111986, 12), ('FM-2026-07-24-SHU', 930334395, 4), ('FM-2026-07-24-SHU', 593111985, 8),
    ('FM-2026-07-24-SHU', 868597351, 2), ('FM-2026-07-24-SHU', 930334397, 3), ('FM-2026-07-24-SHU', 305101272, 6), ('FM-2026-07-24-SHU', 930334396, 2),
    ('FM-2026-07-24-SHU', 567668636, 2), ('FM-2026-07-24-SHU', 305101361, 1), ('FM-2026-07-24-SIM', 438775437, 1)
  ])),
ref AS (
  SELECT nm_id, IF(COUNT(DISTINCT internal_sku) = 1, ANY_VALUE(internal_sku), NULL) internal_sku
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.REF_SKU_MASTER` WHERE marketplace = 'WB' GROUP BY 1),
cogs AS (
  SELECT it.incident_id, it.nm_id, COUNT(u.internal_sku) cogs_interval_count,
    IF(COUNT(u.internal_sku) = 1, MAX(u.product_cogs_rub), NULL) unit_cogs_rub
  FROM items it JOIN incidents i USING (incident_id) LEFT JOIN ref USING (nm_id)
  LEFT JOIN `project-fa311fc0-4d87-4781-986.wb_mart.UNITKA_COGS_EFFECTIVE` u
    ON u.internal_sku = ref.internal_sku AND i.incident_date BETWEEN u.effective_from AND IFNULL(u.effective_to, DATE '9999-12-31')
  GROUP BY 1, 2)
SELECT i.incident_id, i.report_warehouse, i.wb_report, i.incident_date, DATE_TRUNC(i.incident_date, MONTH) incident_month,
  i.orders_warehouse, i.evidence, it.nm_id, ref.internal_sku, it.lost_qty, c.cogs_interval_count, c.unit_cogs_rub,
  it.lost_qty * c.unit_cogs_rub inventory_loss_writeoff_rub
FROM items it JOIN incidents i USING (incident_id) LEFT JOIN ref USING (nm_id) LEFT JOIN cogs c USING (incident_id, nm_id);

-- ─── L2. Списание по месяцу события + защита от двойного счёта ───────────────────────────────────────────────────
-- Двойной счёт исключается ДАННЫМИ остатков WB, а не заказами (заказы с 14.08 WB подписывает «Склад WB РФ»):
--  (а) количество отчёта = остаток WB по складу × nm в снимке на дату отчётов 26.08.2026 — это остаток, который не
--      уехал со склада; проданная единица в него не входит (её себестоимость — во «Вкладе SKU» по продаже),
--  (б) после 26.08 остаток этих позиций не уменьшался ни в одном снимке — ни одна списанная единица не ушла в продажу.
-- Расхождение (а) или уменьшение (б) — inventory_loss_double_count_risk_qty > 0: месяц не закрывается.
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_INVENTORY_LOSS_MONTHLY` AS
WITH it AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_INVENTORY_LOSS_ITEMS`),
st AS (
  SELECT _snapshot_date sd, warehouse_name, nm_id, SUM(quantity) q
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_STOCKS_T5`
  WHERE row_type = 'WAREHOUSE' AND _snapshot_date >= DATE '2026-08-26'
    AND warehouse_name IN (SELECT DISTINCT report_warehouse FROM it)
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
  SUM(IFNULL(it.inventory_loss_writeoff_rub, 0)) inventory_loss_writeoff_rub,
  SUM(IF(it.unit_cogs_rub IS NULL, it.lost_qty, 0)) inventory_loss_cogs_missing_qty,
  ANY_VALUE(rm.risk_qty) inventory_loss_double_count_risk_qty,
  COUNT(DISTINCT it.incident_id) inventory_loss_incidents,
  STRING_AGG(DISTINCT CONCAT(FORMAT_DATE('%Y-%m-%d', it.incident_date), ' ', it.report_warehouse), '; '
    ORDER BY CONCAT(FORMAT_DATE('%Y-%m-%d', it.incident_date), ' ', it.report_warehouse)) inventory_loss_incident_list
FROM it LEFT JOIN risk_m rm USING (incident_month) GROUP BY 1;

-- ─── L3. Сверка компенсации WB: каждая выплата «за товары, пострадавшие…» — по номеру документа WB ─────────────────
-- Выплата в журнале (обработка LOSS_COMPENSATION_INCOME), которой нет в этом списке или с другой суммой, — не сверена:
-- месяц её проводки «Есть неразобранные операции», пока владелец не добавит документ сюда.
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_INVENTORY_LOSS_COMPENSATION_EVIDENCE` AS
SELECT * FROM UNNEST(ARRAY<STRUCT<wb_document STRING, booking_date DATE, amount_rub NUMERIC, evidence STRING>>[
  ('2270352', DATE '2026-07-22', NUMERIC '49479.72', 'Добровольная выплата за товары, пострадавшие в результате обстоятельств непреодолимой силы; rrd 3130856735246'),
  ('2336254', DATE '2026-07-30', NUMERIC '20000.00', 'Добровольная выплата за товары, пострадавшие в результате обстоятельств непреодолимой силы; rrd 3131540616009')
]);
