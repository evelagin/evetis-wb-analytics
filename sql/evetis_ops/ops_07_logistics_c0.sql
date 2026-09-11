-- ============================================================================
-- EVETIS · Stage C0 · Logistics Master — миграция схемы и значения, одобренные владельцем
-- Решения владельца 2026-09-11: docs/ops/STAGE_C0_LOGISTICS_MASTER_2026-09-11.md §7.
-- Идемпотентно: ALTER … IF [NOT] EXISTS; MERGE вставляет только отсутствующие ключи.
-- Порядок на существующей базе: ops_01 (создаст REF_CHANNEL_SHIPPING) → ops_07.
-- В журнал движений и в отгрузки ничего не пишет.
--
-- Классы происхождения (field_provenance.class):
--   FACT             — значение из API / документа как есть;
--   OBSERVED         — посчитано по фактическим поставкам;
--   ASSUMPTION       — допущение владельца до появления данных;
--   OWNER_CONVENTION — правило планирования владельца (не физический факт);
--   OWNER_REQUIRED   — значения нет (NULL); hint — только доказательство, не значение;
--   INHERITED        — берётся из REF_CHANNEL_SHIPPING.
-- ============================================================================

-- 1. REF_SKU_LOGISTICS: units_per_box заменяется на factory_carton_qty (заводской короб отделён от
--    кратности отгрузки); лимит лота уходит на уровень канала; добавлены объём, кратность при низком
--    спросе, происхождение значений. До C0 таблица ПУСТА (проверяется перед применением), поэтому
--    DROP + ADD ничего не теряет и, в отличие от RENAME, повторяется без ошибки.
ALTER TABLE `project-fa311fc0-4d87-4781-986.evetis_ops.REF_SKU_LOGISTICS`
  DROP COLUMN IF EXISTS units_per_box,
  DROP COLUMN IF EXISTS max_lot_weight_kg;
ALTER TABLE `project-fa311fc0-4d87-4781-986.evetis_ops.REF_SKU_LOGISTICS`
  ADD COLUMN IF NOT EXISTS factory_carton_qty INT64,
  ADD COLUMN IF NOT EXISTS unit_volume_l NUMERIC,
  ADD COLUMN IF NOT EXISTS shipment_multiple_low_demand INT64,
  ADD COLUMN IF NOT EXISTS field_provenance STRING;
ALTER TABLE `project-fa311fc0-4d87-4781-986.evetis_ops.REF_SKU_LOGISTICS`
  SET OPTIONS (description = 'Versioned logistics settings per catalogue SKU x channel (C0, owner-approved). factory_carton_qty only as FACT; shipment_multiple is a planning convention, not packaging. Channel defaults in REF_CHANNEL_SHIPPING.');

-- 2. Каналы и способы отгрузки: одно значение на канал, не дублируется по SKU.
MERGE `project-fa311fc0-4d87-4781-986.evetis_ops.REF_CHANNEL_SHIPPING` t
USING (
  SELECT * FROM UNNEST([
    STRUCT(
      'WB' AS channel, 'WB_FBW_PVZ' AS shipping_method, TRUE AS is_planning_default,
      45 AS target_cover_days, 7 AS safety_stock_days, 7 AS lead_time_days, CAST(NULL AS INT64) AS ff_prep_working_days,
      NUMERIC '25' AS max_lot_weight_kg, 500 AS max_lot_units, NUMERIC '200' AS max_lot_volume_l,
      TO_JSON_STRING(STRUCT(
        STRUCT('OWNER_CONVENTION' AS class, 'HIGH' AS confidence, 'C0 D4 2026-09-11' AS source) AS target_cover_days,
        STRUCT('OWNER_CONVENTION' AS class, 'HIGH' AS confidence, 'C0 D6 2026-09-11: 7 days of planned demand' AS source) AS safety_stock_days,
        STRUCT('ASSUMPTION' AS class, 'LOW' AS confidence,
               'C0 D5 2026-09-11: 7 days until enough post-15.08 pickup-point supplies exist; pre-PVZ observed create->acceptance median 4 / p75 6 (29 box supplies 2026)' AS source) AS lead_time_days,
        STRUCT('FACT' AS class, 'MEDIUM' AS confidence,
               'WB cabinet errors, FBW via any PVZ since 15.08.2026: 25 kg / 500 units / 200 L per supply (CONTROL_TOWER_MODEL_REVIEW.md:16); CT-0015 open: boxes/pallet via SC' AS source) AS max_lot_limits
      )) AS field_provenance,
      'WB: отгрузка через любой ПВЗ; ограничивает вес (25 кг = 63 крема 300 мл / 125 тоников / 155 кремов 50 мл / 183 сыворотки / 200 пудр)' AS note),
    STRUCT(
      'OZON', 'OZON_FBO_DROPOFF', TRUE,
      45, 10, 10, CAST(NULL AS INT64),
      CAST(NULL AS NUMERIC), CAST(NULL AS INT64), CAST(NULL AS NUMERIC),
      TO_JSON_STRING(STRUCT(
        STRUCT('OWNER_CONVENTION' AS class, 'HIGH' AS confidence, 'C0 D4 2026-09-11' AS source) AS target_cover_days,
        STRUCT('OWNER_CONVENTION' AS class, 'HIGH' AS confidence, 'C0 D6 2026-09-11: 10 days of planned demand' AS source) AS safety_stock_days,
        STRUCT('OBSERVED' AS class, 'MEDIUM' AS confidence,
               'C0 D5: created->COMPLETED median 10 days, p25 6 / p75 14, 52 supplies (RAW_OZON_SUPPLY_ORDERS); created->slot median 2' AS source) AS lead_time_days,
        STRUCT('OWNER_REQUIRED' AS class, CAST(NULL AS STRING) AS confidence,
               'no Ozon per-supply limit found in data; not enforced while NULL' AS source) AS max_lot_limits
      )),
      'Ozon: поставка через точку сдачи; лимит поставки в данных не найден'),
    STRUCT(
      'FF', 'FF_PICK_BUILD', TRUE,
      CAST(NULL AS INT64), CAST(NULL AS INT64), CAST(NULL AS INT64), 2,
      CAST(NULL AS NUMERIC), CAST(NULL AS INT64), CAST(NULL AS NUMERIC),
      TO_JSON_STRING(STRUCT(
        STRUCT('ASSUMPTION' AS class, 'MEDIUM' AS confidence,
               'C0 D5 2026-09-11: FF pick from pallet / bundle build = 2 working days (owner-stated, not measured)' AS source) AS ff_prep_working_days
      )),
      'ФФ: снятие с паллет и сборка наборов; добавляется к сроку поставки канала')
  ])
) s
ON t.channel = s.channel AND t.shipping_method = s.shipping_method
WHEN NOT MATCHED THEN
  INSERT (channel, shipping_method, is_planning_default, target_cover_days, safety_stock_days, lead_time_days,
          ff_prep_working_days, max_lot_weight_kg, max_lot_units, max_lot_volume_l, field_provenance,
          effective_from, effective_to, source, note, created_at, created_by)
  VALUES (s.channel, s.shipping_method, s.is_planning_default, s.target_cover_days, s.safety_stock_days, s.lead_time_days,
          s.ff_prep_working_days, s.max_lot_weight_kg, s.max_lot_units, s.max_lot_volume_l, s.field_provenance,
          DATE '2026-09-12', NULL, 'C0_OWNER_APPROVED_2026-09-11', s.note, CURRENT_TIMESTAMP(), 'stage-c0');

-- 3. SKU × канал. Строка создаётся только там, где у SKU есть текущая карточка канала.
--    Вес — карточки Ozon (FACT, MEDIUM: заявлен, не взвешен); объём — данные площадки канала.
--    Кратность отгрузки — конвенция владельца по типу товара (НЕ заводской короб):
--      WB:   сыворотка 30 мл 20 · крем для лица 50 мл 20 · тоник 150 мл 30 · пудра 20 · кремы 300 мл 20 · наборы 5
--      Ozon: одиночный SKU 10 (при низком спросе 5) · наборы 5 (при низком спросе 2)
--    Минимальная отгрузка = кратность: меньше — «ЖДАТЬ / В СЛЕДУЮЩУЮ ПОСТАВКУ» (D3).
MERGE `project-fa311fc0-4d87-4781-986.evetis_ops.REF_SKU_LOGISTICS` t
USING (
  WITH src AS (
    SELECT * FROM UNNEST([
      STRUCT('EVT-EP-ENZYME-75' AS internal_sku, 'ENZYME' AS kind,
             NUMERIC '0.125' AS weight_kg, NUMERIC '0.2459' AS vol_ozon_l, NUMERIC '0.35' AS vol_wb_l,
             'HINT 110: pallets 3630 = 33 x 110; WB 2026 supply lines 110 x6, 220 x1' AS carton_hint,
             CAST(NULL AS STRING) AS weight_hint),
      STRUCT('EVT-FC-ACNE-50', 'FACE_CREAM_50', NUMERIC '0.161', NUMERIC '0.264', NUMERIC '0.272',
             'no pattern: WB lines 75-multiples in 2025, 20 x5 in 2026', CAST(NULL AS STRING)),
      STRUCT('EVT-FC-MOIST-50', 'FACE_CREAM_50', NUMERIC '0.161', NUMERIC '0.264', NUMERIC '0.332',
             'no pattern: WB lines 125-multiples and 96/30/17', CAST(NULL AS STRING)),
      STRUCT('EVT-FS-ACNE-30', 'SERUM_30', NUMERIC '0.136', NUMERIC '0.16', NUMERIC '0.16',
             'no pattern: pallets 5200; WB lines 35-multiples (2025) and 20 x6 (2026)', CAST(NULL AS STRING)),
      STRUCT('EVT-FS-MOIST-30', 'SERUM_30', NUMERIC '0.136', NUMERIC '0.16', NUMERIC '0.25',
             'no pattern: pallets 3600; WB lines 35-multiples (2025), 100 x8 and 20 x8 (2026)', CAST(NULL AS STRING)),
      STRUCT('EVT-FT-ACNE-150', 'TONIC_150', NUMERIC '0.200', NUMERIC '0.2866', NUMERIC '0.4',
             'HINT 90: pallets 3600 = 40 x 90; WB 2026 supply lines 90 x6, 180 x1', CAST(NULL AS STRING)),
      STRUCT('EVT-FT-MOIST-150', 'TONIC_150', NUMERIC '0.200', NUMERIC '0.2866', NUMERIC '0.4',
             'HINT 90: pallets 3600 = 40 x 90; WB 2026 supply lines 90 x6, 180 x1', CAST(NULL AS STRING)),
      STRUCT('EVT-HC-AMBER-300', 'CREAM_300', NUMERIC '0.394', NUMERIC '0.7277', NUMERIC '0.833',
             'weak HINT 40: pallets 4400 divisible by 40 and 80; WB 2026 lines 40 x2', CAST(NULL AS STRING)),
      STRUCT('EVT-HC-CHERRY-300', 'CREAM_300', NUMERIC '0.394', NUMERIC '0.7277', NUMERIC '0.833',
             'weak HINT 40: pallets 4320 divisible by 40 and 80; WB 2026 lines 40 x2', CAST(NULL AS STRING)),
      STRUCT('EVT-HC-HAND-300', 'CREAM_300', NUMERIC '0.391', NUMERIC '0.7056', NUMERIC '0.833',
             'weak HINT 40: pallets 2720 divisible by 40 and 80; WB 2024-25 lines 40-multiples but also 250/375/500/625', CAST(NULL AS STRING)),
      STRUCT('EVT-HC-BODY-300', 'CREAM_300', CAST(NULL AS NUMERIC), CAST(NULL AS NUMERIC), CAST(NULL AS NUMERIC),
             'inactive SKU (FF 0, no plan)', 'no Ozon card (archived); season model assumes 0.391'),
      STRUCT('EVT-SET-4PC-ACNE', 'BUNDLE', NUMERIC '0.637', NUMERIC '1.1778', NUMERIC '1.294', CAST(NULL AS STRING), CAST(NULL AS STRING)),
      STRUCT('EVT-SET-ACNE-POWDER-SERUM-CREAM', 'BUNDLE', NUMERIC '0.422', NUMERIC '0.8727', CAST(NULL AS NUMERIC),
             CAST(NULL AS STRING), CAST(NULL AS STRING)),
      STRUCT('EVT-SET-ACNE-TONIC-SERUM', 'BUNDLE', NUMERIC '0.341', NUMERIC '0.5532', NUMERIC '0.72', CAST(NULL AS STRING), CAST(NULL AS STRING)),
      STRUCT('EVT-SET-CHERRY-AMBER', 'BUNDLE', NUMERIC '0.788', NUMERIC '1.4536', NUMERIC '1.558', CAST(NULL AS STRING), CAST(NULL AS STRING)),
      STRUCT('EVT-SET-HAND-AMBER', 'BUNDLE', NUMERIC '0.788', NUMERIC '1.4536', NUMERIC '1.476', CAST(NULL AS STRING), CAST(NULL AS STRING)),
      STRUCT('EVT-SET-HAND-BODY', 'BUNDLE', CAST(NULL AS NUMERIC), CAST(NULL AS NUMERIC), CAST(NULL AS NUMERIC),
             CAST(NULL AS STRING), 'inactive bundle (HC-BODY-300 = 0); no card weight'),
      STRUCT('EVT-SET-HAND-CHERRY', 'BUNDLE', NUMERIC '0.788', NUMERIC '1.4536', NUMERIC '1.563', CAST(NULL AS STRING), CAST(NULL AS STRING)),
      STRUCT('EVT-SET-MOIST-TONIC-SERUM', 'BUNDLE', NUMERIC '0.341', NUMERIC '0.5532', NUMERIC '0.72', CAST(NULL AS STRING), CAST(NULL AS STRING)),
      STRUCT('EVT-SET-SER-CREAM-ACNE', 'BUNDLE', NUMERIC '0.296', NUMERIC '0.528', NUMERIC '0.544', CAST(NULL AS STRING), CAST(NULL AS STRING)),
      STRUCT('EVT-SET-SER-CREAM-MOIST', 'BUNDLE', NUMERIC '0.296', NUMERIC '0.528', NUMERIC '0.544', CAST(NULL AS STRING), CAST(NULL AS STRING)),
      STRUCT('EVT-SET-TON-CREAM-ACNE', 'BUNDLE', CAST(NULL AS NUMERIC), CAST(NULL AS NUMERIC), NUMERIC '0.765',
             CAST(NULL AS STRING), 'no Ozon card; components sum 0.361 kg without set packaging (hint only)'),
      STRUCT('EVT-SET-TON-CREAM-MOIST', 'BUNDLE', CAST(NULL AS NUMERIC), CAST(NULL AS NUMERIC), NUMERIC '0.765',
             CAST(NULL AS STRING), 'no Ozon card; components sum 0.361 kg without set packaging (hint only)'),
      STRUCT('EVT-SET-TON-SER-CREAM-ACNE', 'BUNDLE', NUMERIC '0.502', NUMERIC '0.8727', NUMERIC '1.105', CAST(NULL AS STRING), CAST(NULL AS STRING)),
      STRUCT('EVT-SET-TON-SER-CREAM-MOIST', 'BUNDLE', CAST(NULL AS NUMERIC), CAST(NULL AS NUMERIC), NUMERIC '1.105',
             CAST(NULL AS STRING), 'no Ozon card; components sum 0.497 kg without set packaging (hint only)')
    ])
  ),
  ch AS (
    SELECT DISTINCT internal_sku, UPPER(marketplace) AS channel
    FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP`
    WHERE is_current AND UPPER(marketplace) IN ('WB', 'OZON')
  ),
  r AS (
    SELECT s.internal_sku, c.channel, s.kind, s.weight_kg, s.carton_hint, s.weight_hint,
           IF(c.channel = 'WB', s.vol_wb_l, s.vol_ozon_l) AS unit_volume_l,
           CASE
             WHEN c.channel = 'WB' THEN
               CASE s.kind WHEN 'SERUM_30' THEN 20 WHEN 'FACE_CREAM_50' THEN 20 WHEN 'TONIC_150' THEN 30
                           WHEN 'ENZYME' THEN 20 WHEN 'CREAM_300' THEN 20 WHEN 'BUNDLE' THEN 5 END
             ELSE IF(s.kind = 'BUNDLE', 5, 10)
           END AS shipment_multiple,
           IF(c.channel = 'OZON', IF(s.kind = 'BUNDLE', 2, 5), NULL) AS shipment_multiple_low_demand
    FROM src s JOIN ch c USING (internal_sku)
  )
  SELECT internal_sku, channel, unit_volume_l, weight_kg AS unit_weight_kg,
         shipment_multiple, shipment_multiple_low_demand, shipment_multiple AS min_shipment_units,
         TO_JSON_STRING(STRUCT(
           STRUCT('OWNER_REQUIRED' AS class, CAST(NULL AS STRING) AS confidence,
                  'C0 D1: factory carton only as FACT confirmed by FF / packing list' AS source, carton_hint AS hint) AS factory_carton_qty,
           IF(weight_kg IS NOT NULL,
              STRUCT('FACT' AS class, 'MEDIUM' AS confidence,
                     'C0 D8: Ozon card 2026-09-04 (/v4/product/info/attributes), declared, not weighed' AS source, CAST(NULL AS STRING) AS hint),
              STRUCT('OWNER_REQUIRED' AS class, CAST(NULL AS STRING) AS confidence, 'no card weight' AS source, weight_hint AS hint)) AS unit_weight_kg,
           IF(unit_volume_l IS NOT NULL,
              STRUCT('FACT' AS class, 'HIGH' AS confidence,
                     IF(channel = 'WB', 'WB paid storage volume (RAW_WB_PAID_STORAGE.volume)',
                        'Ozon card / supply volume_in_litres') AS source, CAST(NULL AS STRING) AS hint),
              STRUCT('OWNER_REQUIRED' AS class, CAST(NULL AS STRING) AS confidence, 'no marketplace volume on this channel' AS source,
                     CAST(NULL AS STRING) AS hint)) AS unit_volume_l,
           STRUCT('OWNER_REQUIRED' AS class, CAST(NULL AS STRING) AS confidence, 'no evidence' AS source, CAST(NULL AS STRING) AS hint) AS box_weight_kg,
           STRUCT('OWNER_CONVENTION' AS class, 'HIGH' AS confidence,
                  'C0 D2 2026-09-11: planning/rounding convention by product type, NOT packaging' AS source, CAST(NULL AS STRING) AS hint) AS shipment_multiple,
           IF(channel = 'OZON',
              STRUCT('OWNER_CONVENTION' AS class, 'HIGH' AS confidence,
                     'C0 D2: Ozon solo 5 / bundles 2 allowed where demand is low' AS source, CAST(NULL AS STRING) AS hint),
              STRUCT('NOT_APPLICABLE' AS class, CAST(NULL AS STRING) AS confidence, 'WB: no low-demand allowance (C0 D2)' AS source,
                     CAST(NULL AS STRING) AS hint)) AS shipment_multiple_low_demand,
           STRUCT('OWNER_CONVENTION' AS class, 'HIGH' AS confidence,
                  'C0 D3: below the multiple -> WAIT / ADD TO NEXT SUPPLY' AS source, CAST(NULL AS STRING) AS hint) AS min_shipment_units,
           STRUCT('OWNER_CONVENTION' AS class, 'HIGH' AS confidence, 'C0 D7: 0 until CT-0012 (WB FBS) is decided' AS source,
                  CAST(NULL AS STRING) AS hint) AS fbs_reserve_units,
           STRUCT('INHERITED' AS class, CAST(NULL AS STRING) AS confidence, 'REF_CHANNEL_SHIPPING' AS source,
                  CAST(NULL AS STRING) AS hint) AS target_cover_safety_lead
         )) AS field_provenance
  FROM r
) s
ON t.internal_sku = s.internal_sku AND t.channel = s.channel
WHEN NOT MATCHED THEN
  INSERT (internal_sku, channel, factory_carton_qty, unit_weight_kg, box_weight_kg, min_shipment_units, shipment_multiple,
          target_cover_days, safety_stock_days, lead_time_days, fbs_reserve_units, effective_from, effective_to,
          source, note, created_at, created_by, unit_volume_l, shipment_multiple_low_demand, field_provenance)
  VALUES (s.internal_sku, s.channel, NULL, s.unit_weight_kg, NULL, s.min_shipment_units, s.shipment_multiple,
          NULL, NULL, NULL, 0, DATE '2026-09-12', NULL,
          'C0_OWNER_APPROVED_2026-09-11', NULL, CURRENT_TIMESTAMP(), 'stage-c0',
          s.unit_volume_l, s.shipment_multiple_low_demand, s.field_provenance);
