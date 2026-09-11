-- ============================================================================
-- EVETIS · Stage B · Operations backend · 03 — витрины
-- Только чтение. Источники вне evetis_ops читаются, но не меняются:
--   evetis_ref  — общие справочники (BOM, каталог, маппинг SKU) и CT_STOCK_SNAPSHOT (для сверки);
--   ozon_raw    — заказы поставок Ozon (в ozon_mart их нет; чтение RAW — задокументированное
--                 отступление кросс-канального слоя, см. протокол Stage B);
--   wb_mart     — V_CT_INVENTORY_TRUTH: остатки площадок (API), уже в физических единицах.
-- ============================================================================

-- BOM: карточка → физические компоненты. Соло-SKU раскладывается сам в себя.
-- Та же логика, что wb_mart.V_CT_BOM_CURRENT, но на общих справочниках evetis_ref,
-- без зависимости от wb_mart (паритет проверяется тестом).
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_BOM` AS
WITH comp AS (
  SELECT bundle_internal_sku AS card_sku, component_internal_sku AS component_sku, component_qty
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_BUNDLE_COMPONENTS`
  WHERE effective_to IS NULL OR effective_to >= CURRENT_DATE()
),
base AS (
  SELECT internal_sku AS card_sku, internal_sku AS component_sku, 1 AS component_qty
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_PRODUCT_MASTER`
  WHERE NOT is_bundle
)
SELECT card_sku, component_sku, component_qty, card_sku != component_sku AS is_bundle
FROM (SELECT * FROM comp UNION ALL SELECT * FROM base);

-- Заказы поставок Ozon в физических единицах: заказ × строка × компонент.
-- Последняя выгрузка на заказ (RAW хранит только последнее состояние, истории нет).
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_OZON_ORDER_UNITS` AS
WITH o AS (
  SELECT order_id, order_number, state, created_at, state_updated_at, planned_arrival_from,
         dropoff_warehouse_name, extracted_at
  FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY order_id ORDER BY extracted_at DESC) AS rn
        FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_SUPPLY_ORDERS`)
  WHERE rn = 1
),
b AS (
  SELECT order_id, supply_id, CAST(sku AS STRING) AS ozon_sku, offer_id, quantity_planned, quantity_accepted
  FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY order_id, bundle_id, sku ORDER BY extracted_at DESC) AS rn
        FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_SUPPLY_BUNDLES`)
  WHERE rn = 1
),
m AS (
  SELECT DISTINCT internal_sku, CAST(marketplace_sku AS STRING) AS msku
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP`
  WHERE UPPER(marketplace) = 'OZON' AND is_current
)
SELECT o.order_id, o.order_number, o.state, o.created_at, o.state_updated_at, o.planned_arrival_from,
       o.dropoff_warehouse_name, o.extracted_at, b.supply_id, b.ozon_sku,
       m.internal_sku AS card_sku,
       bom.component_sku AS internal_sku,
       bom.is_bundle,
       b.quantity_planned AS cards_planned,
       b.quantity_accepted AS cards_accepted,
       b.quantity_planned * bom.component_qty AS units_planned,
       b.quantity_accepted * bom.component_qty AS units_accepted
FROM o
JOIN b USING (order_id)
LEFT JOIN m ON m.msku = b.ozon_sku
LEFT JOIN `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_BOM` bom ON bom.card_sku = m.internal_sku;

-- Остатки по бакетам журнала. Каждое движение: −qty в бакете from, +qty в бакете to.
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_STOCK_BALANCE` AS
WITH legs AS (
  SELECT internal_sku, from_state AS state, from_location AS location, from_channel AS channel,
         from_container AS container, from_doc AS doc, -qty AS qty
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_STOCK_MOVEMENT` WHERE from_state IS NOT NULL
  UNION ALL
  SELECT internal_sku, to_state, to_location, to_channel, to_container, to_doc, qty
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_STOCK_MOVEMENT` WHERE to_state IS NOT NULL
)
SELECT internal_sku, state, location, channel, container, doc, SUM(qty) AS units,
       state IN ('AVAILABLE', 'RESERVED', 'ASSEMBLED', 'FBS_READY') AS on_ff
FROM legs
GROUP BY internal_sku, state, location, channel, container, doc;

-- Текущий статус отгрузки = последнее событие.
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_SHIPMENT_CURRENT` AS
SELECT * EXCEPT (rn)
FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY shipment_id ORDER BY event_seq DESC) AS rn
      FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_SHIPMENT`)
WHERE rn = 1;

-- Сверка заказов Ozon: журнал ФФ ↔ API. Грейн: заказ × физический SKU.
-- Каждый заказ получает РОВНО ОДИН класс, поэтому одна единица не может оказаться
-- одновременно в резерве и в пути:
--   RESERVED_ON_FF              — в журнале RESERVED/ASSEMBLED, отгрузки нет;
--   IN_TRANSIT_API              — отгружено по журналу, API видит поставку → в пути считается ПО API;
--   SHIPPED_UNCONFIRMED         — отгружено по журналу, API ещё не видит;
--   HANDED_OVER                 — API принял (COMPLETED) → единицы уже в остатке площадки (API);
--   API_RESERVATION_UNCONFIRMED — READY_TO_SUPPLY без записи в журнале: консервативно вычитается из свободного;
--   API_ONLY                    — API видит поставку в пути без записи в журнале;
--   CANCELLED_IN_LEDGER         — отгрузка отменена в журнале.
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_OZON_ORDER_RECON` AS
WITH api AS (
  SELECT order_number, internal_sku,
         SUM(units_planned) AS api_units_planned,
         SUM(units_accepted) AS api_units_accepted,      -- NULL = Ozon ещё не сообщил принятое
         COUNTIF(internal_sku IS NULL) AS unmapped_lines
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_OZON_ORDER_UNITS`
  GROUP BY order_number, internal_sku
),
api_order AS (
  SELECT order_number, ANY_VALUE(order_id) AS order_id, ANY_VALUE(state) AS api_state,
         ANY_VALUE(created_at) AS created_at, ANY_VALUE(state_updated_at) AS state_updated_at
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_OZON_ORDER_UNITS`
  GROUP BY order_number
),
ship_all AS (
  SELECT shipment_id, marketplace_ref AS order_number, status, event_at
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_SHIPMENT_CURRENT`
  WHERE channel = 'OZON' AND marketplace_ref IS NOT NULL
),
ship_one AS (   -- одна отгрузка на заказ: действующая важнее отменённой (две действующие запрещены процедурой резерва)
  SELECT order_number, shipment_id, status AS ledger_status
  FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY order_number ORDER BY status = 'CANCELLED', event_at DESC) AS rn FROM ship_all)
  WHERE rn = 1
),
ledger_units AS (   -- по всем отгрузкам заказа (отменённые дают ноль)
  SELECT sa.order_number, b.internal_sku,
         SUM(IF(b.state = 'RESERVED', b.units, 0)) AS ledger_reserved,
         SUM(IF(b.state = 'ASSEMBLED', b.units, 0)) AS ledger_assembled,
         SUM(IF(b.state = 'SHIPPED', b.units, 0)) AS ledger_shipped
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_STOCK_BALANCE` b
  JOIN ship_all sa ON sa.shipment_id = b.doc
  GROUP BY sa.order_number, b.internal_sku
),
scope AS (   -- открытые в API заказы + всё, что есть в журнале
  SELECT order_number FROM api_order WHERE api_state NOT IN ('CANCELLED', 'COMPLETED')
  UNION DISTINCT SELECT order_number FROM ship_all
),
keys AS (
  SELECT order_number, internal_sku FROM api WHERE order_number IN (SELECT order_number FROM scope)
  UNION DISTINCT SELECT order_number, internal_sku FROM ledger_units
),
j AS (
  SELECT k.order_number, k.internal_sku, ao.order_id, ao.api_state, ao.created_at, ao.state_updated_at,
         IFNULL(a.api_units_planned, 0) AS api_units_planned, a.api_units_accepted, IFNULL(a.unmapped_lines, 0) AS unmapped_lines,
         so.shipment_id, so.ledger_status,
         IFNULL(lu.ledger_reserved, 0) AS ledger_reserved,
         IFNULL(lu.ledger_assembled, 0) AS ledger_assembled,
         IFNULL(lu.ledger_shipped, 0) AS ledger_shipped
  FROM keys k
  LEFT JOIN api a ON a.order_number = k.order_number AND IFNULL(a.internal_sku, '~') = IFNULL(k.internal_sku, '~')
  LEFT JOIN api_order ao ON ao.order_number = k.order_number
  LEFT JOIN ship_one so ON so.order_number = k.order_number
  LEFT JOIN ledger_units lu ON lu.order_number = k.order_number AND lu.internal_sku = k.internal_sku
),
order_level AS (
  SELECT order_number, SUM(ledger_reserved + ledger_assembled) AS o_reserved, SUM(ledger_shipped) AS o_shipped
  FROM j GROUP BY order_number
),
cls AS (
  SELECT j.*,
    CASE
      WHEN j.shipment_id IS NULL AND j.api_state = 'READY_TO_SUPPLY' THEN 'API_RESERVATION_UNCONFIRMED'
      WHEN j.shipment_id IS NULL THEN 'API_ONLY'
      WHEN j.ledger_status = 'CANCELLED' THEN 'CANCELLED_IN_LEDGER'
      WHEN ol.o_reserved > 0 AND ol.o_shipped = 0 THEN 'RESERVED_ON_FF'
      WHEN ol.o_shipped > 0 AND j.api_state = 'COMPLETED' THEN 'HANDED_OVER'
      WHEN ol.o_shipped > 0 AND j.api_state IN ('ACCEPTED_AT_SUPPLY_WAREHOUSE', 'IN_TRANSIT',
             'ACCEPTANCE_AT_STORAGE_WAREHOUSE', 'REPORTS_CONFIRMATION_AWAITING') THEN 'IN_TRANSIT_API'
      WHEN ol.o_shipped > 0 THEN 'SHIPPED_UNCONFIRMED'
      ELSE 'UNCLASSIFIED'
    END AS recon_class
  FROM j JOIN order_level ol USING (order_number)
)
SELECT order_number, order_id, api_state, created_at, state_updated_at, internal_sku,
       shipment_id, ledger_status, recon_class,
       api_units_planned, api_units_accepted, unmapped_lines,
       ledger_reserved, ledger_assembled, ledger_shipped,
       -- единицы, засчитанные в каждом разрезе (на заказ ненулевой ровно один):
       IF(recon_class = 'RESERVED_ON_FF', ledger_reserved + ledger_assembled, 0) AS reserved_on_ff_units,
       -- в пути по API; принятое Ozon уже лежит в его остатке (API) → вычитается, как только Ozon его сообщит
       CASE recon_class
         WHEN 'IN_TRANSIT_API' THEN api_units_planned - IFNULL(api_units_accepted, 0)
         WHEN 'SHIPPED_UNCONFIRMED' THEN ledger_shipped
         WHEN 'API_ONLY' THEN IF(api_state IN ('ACCEPTED_AT_SUPPLY_WAREHOUSE', 'IN_TRANSIT',
                'ACCEPTANCE_AT_STORAGE_WAREHOUSE', 'REPORTS_CONFIRMATION_AWAITING'), api_units_planned - IFNULL(api_units_accepted, 0), 0)
         ELSE 0 END AS pipeline_units,
       -- поставка на приёмке склада Ozon: часть единиц может уже числиться в остатке Ozon (API),
       -- пока Ozon не сообщил принятое количество. Это задокументированный разрыв, а не вывод.
       recon_class IN ('IN_TRANSIT_API', 'API_ONLY')
         AND api_state IN ('ACCEPTANCE_AT_STORAGE_WAREHOUSE', 'REPORTS_CONFIRMATION_AWAITING') AS in_acceptance,
       recon_class IN ('IN_TRANSIT_API', 'API_ONLY')
         AND api_state IN ('ACCEPTANCE_AT_STORAGE_WAREHOUSE', 'REPORTS_CONFIRMATION_AWAITING')
         AND api_units_accepted IS NULL AS accepted_unreported,
       IF(recon_class = 'HANDED_OVER', ledger_shipped, 0) AS handed_over_units,
       IF(recon_class = 'API_RESERVATION_UNCONFIRMED', api_units_planned, 0) AS api_unconfirmed_units,
       -- недопринятое: только когда Ozon сообщил принятое количество; NULL — неизвестно, а не ноль
       IF(recon_class = 'HANDED_OVER' AND api_units_accepted IS NOT NULL, ledger_shipped - api_units_accepted, NULL) AS acceptance_discrepancy_units,
       (ledger_reserved + ledger_assembled + ledger_shipped) AS ledger_units,
       -- журнал и API обязаны описывать один и тот же физический объём заказа
       IF(shipment_id IS NULL OR recon_class = 'CANCELLED_IN_LEDGER', NULL,
          (ledger_reserved + ledger_assembled + ledger_shipped) = api_units_planned) AS ledger_matches_api,
       -- журнал отстал от API: по журналу резерв на ФФ, а Ozon уже видит поставку в пути / принятой /
       -- отменённой. Единицы остаются в резерве ФФ (один источник), но расхождение обязано быть видно.
       recon_class = 'RESERVED_ON_FF' AND IFNULL(api_state, '∅') NOT IN ('DATA_FILLING', 'READY_TO_SUPPLY') AS ledger_behind_api
FROM cls;

-- СОСТОЯНИЕ ЗАПАСА. Грейн: SKU × сторона × состояние × место × канал × контейнер × документ.
--   side = FF          — журнал (единственный источник для запаса на ФФ);
--   side = PIPELINE    — в пути: по API, если API видит поставку, иначе по журналу (один источник на отгрузку);
--                        IN_ACCEPTANCE — поставка на приёмке Ozon: пока Ozon не сообщил принятое количество,
--                        часть этих единиц может уже входить в ON_MARKETPLACE (OZON) — эти две строки не складывать;
--   side = MARKETPLACE — остаток площадок по API (из Control Tower), в журнал не пишется.
-- RESERVED_UNCONFIRMED_API — справочно: резерв, известный из API, но не отмеченный ФФ.
-- В сумму запаса ФФ не входит (он и так внутри AVAILABLE), но вычитается из свободного.
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_STOCK_STATE` AS
WITH ff AS (
  SELECT internal_sku, 'FF' AS side, state, location, channel, container AS container_sku,
         CASE state WHEN 'ASSEMBLED' THEN 'FBO' WHEN 'FBS_READY' THEN 'FBS' END AS purpose,
         doc AS doc_id, units, 'LEDGER' AS source
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_STOCK_BALANCE`
  WHERE on_ff AND units != 0
),
ozon_pipe AS (   -- IN_ACCEPTANCE: пока Ozon не сообщил принятое, не складывать с ON_MARKETPLACE (OZON) — возможно пересечение
  SELECT internal_sku, 'PIPELINE' AS side,
         CASE WHEN recon_class = 'SHIPPED_UNCONFIRMED' THEN 'SHIPPED_UNCONFIRMED'
              WHEN in_acceptance THEN 'IN_ACCEPTANCE' ELSE 'IN_TRANSIT' END AS state,
         CAST(NULL AS STRING) AS location, 'OZON' AS channel, CAST(NULL AS STRING) AS container_sku,
         CAST(NULL AS STRING) AS purpose, COALESCE(shipment_id, CONCAT('OZON:', order_number)) AS doc_id,
         pipeline_units AS units,
         IF(recon_class = 'SHIPPED_UNCONFIRMED', 'LEDGER', 'API') AS source
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_OZON_ORDER_RECON`
  WHERE pipeline_units != 0
),
other_pipe AS (   -- отгрузки WB / B2B: сверки с API в Stage B ещё нет → по журналу
  SELECT internal_sku, 'PIPELINE', 'SHIPPED_UNCONFIRMED', CAST(NULL AS STRING), channel, container,
         CAST(NULL AS STRING), doc, units, 'LEDGER'
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_STOCK_BALANCE`
  WHERE state = 'SHIPPED' AND channel != 'OZON' AND units != 0
),
api_unconfirmed AS (
  SELECT internal_sku, 'FF', 'RESERVED_UNCONFIRMED_API', CAST(NULL AS STRING), 'OZON', CAST(NULL AS STRING),
         CAST(NULL AS STRING), CONCAT('OZON:', order_number), api_unconfirmed_units, 'API'
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_OZON_ORDER_RECON`
  WHERE api_unconfirmed_units != 0
),
marketplace AS (
  SELECT internal_sku, 'MARKETPLACE', 'ON_MARKETPLACE', CAST(NULL AS STRING), 'WB', CAST(NULL AS STRING),
         CAST(NULL AS STRING), CAST(NULL AS STRING), wb_fbo_live_units, 'API'
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_INVENTORY_TRUTH` WHERE wb_fbo_live_units != 0
  UNION ALL
  SELECT internal_sku, 'MARKETPLACE', 'ON_MARKETPLACE', CAST(NULL AS STRING), 'OZON', CAST(NULL AS STRING),
         CAST(NULL AS STRING), CAST(NULL AS STRING), ozon_fbo_units, 'API'
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_INVENTORY_TRUTH` WHERE ozon_fbo_units != 0
)
SELECT *, CURRENT_TIMESTAMP() AS computed_at
FROM (
  SELECT * FROM ff UNION ALL SELECT * FROM ozon_pipe UNION ALL SELECT * FROM other_pipe
  UNION ALL SELECT * FROM api_unconfirmed UNION ALL SELECT * FROM marketplace
);

-- Мощность сборки наборов из СВОБОДНОГО запаса ФФ.
-- 🔴 Мощности НЕ складываются: наборы делят компоненты. capacity_alone — сколько наборов
--    этого SKU можно собрать, если другие наборы из тех же компонентов не собираются.
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_BUNDLE_CAPACITY` AS
WITH avail AS (
  SELECT internal_sku,
         SUM(IF(state = 'AVAILABLE', units, 0)) AS available_total,
         SUM(IF(state = 'AVAILABLE' AND location = 'SHELF', units, 0)) AS available_shelf
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_STOCK_BALANCE`
  GROUP BY internal_sku
),
unconf AS (
  SELECT internal_sku, SUM(api_unconfirmed_units) AS api_unconfirmed
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_OZON_ORDER_RECON`
  GROUP BY internal_sku
),
free AS (
  SELECT a.internal_sku,
         GREATEST(a.available_total - IFNULL(u.api_unconfirmed, 0), 0) AS free_total,
         GREATEST(a.available_shelf - IFNULL(u.api_unconfirmed, 0), 0) AS free_shelf
  FROM avail a LEFT JOIN unconf u USING (internal_sku)
),
bom AS (
  SELECT card_sku AS bundle_sku, component_sku, component_qty
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_BOM` WHERE is_bundle
),
per_comp AS (
  SELECT b.bundle_sku, b.component_sku, b.component_qty,
         IFNULL(f.free_total, 0) AS free_total, IFNULL(f.free_shelf, 0) AS free_shelf,
         DIV(IFNULL(f.free_total, 0), b.component_qty) AS cap_total,
         DIV(IFNULL(f.free_shelf, 0), b.component_qty) AS cap_shelf
  FROM bom b LEFT JOIN free f ON f.internal_sku = b.component_sku
),
cap AS (
  SELECT bundle_sku,
         MIN(cap_total) AS capacity_alone,
         MIN(cap_shelf) AS capacity_alone_shelf,
         ARRAY_AGG(component_sku ORDER BY cap_total, component_sku LIMIT 1)[OFFSET(0)] AS limiting_component,
         COUNT(*) AS components
  FROM per_comp GROUP BY bundle_sku
),
shared AS (
  SELECT a.bundle_sku, STRING_AGG(DISTINCT b.bundle_sku, ', ' ORDER BY b.bundle_sku) AS shares_components_with
  FROM bom a JOIN bom b ON a.component_sku = b.component_sku AND a.bundle_sku != b.bundle_sku
  GROUP BY a.bundle_sku
)
SELECT c.bundle_sku, c.components, c.capacity_alone, c.capacity_alone_shelf, c.limiting_component,
       s.shares_components_with,
       s.shares_components_with IS NULL AS capacity_is_independent,
       'Мощность — при условии, что другие наборы из общих компонентов не собираются. Складывать нельзя.' AS note
FROM cap c LEFT JOIN shared s USING (bundle_sku);

-- СВЕРКА ПОСЕВА: что представляли исторические 369 и как из них получается физический срез.
-- Грейн: физический SKU. Тождества (все обязаны быть TRUE):
--   A  seed369        = выехавшие 03.09 (LEFT_FF) + списанное сидом с полки (полка ФФ − полка CT);
--   B  списанное      = LEAST(потребность поставок 07.09, полка ФФ) — сид взял с полки ровно то, что на ней было;
--   C  дефицит 07.09  = письмо ФФ (41 / 43 / 39);
--   P  паллеты ФФ     = паллеты CT_STOCK_SNAPSHOT.
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_SEED_RECON` AS
WITH snap AS (
  SELECT internal_sku, shelf_units AS shelf_ff, pallet_units AS pallet_ff
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_OPENING_FF_SNAPSHOT`
  WHERE snapshot_date = DATE '2026-09-09' AND in_ledger
),
ct AS (
  SELECT internal_sku, ff_operational_units AS shelf_ct, ff_pallet_units AS pallet_ct,
         in_transit_to_ozon_units AS seed369
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_STOCK_SNAPSHOT`
),
ev AS (
  SELECT u.internal_sku,
         SUM(IF(e.ff_status_at_opening = 'ON_FF', u.units_planned, 0)) AS on_ff_units,
         SUM(IF(e.ff_status_at_opening = 'LEFT_FF', u.units_planned, 0)) AS left_ff_units
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_OZON_ORDER_UNITS` u
  JOIN `project-fa311fc0-4d87-4781-986.evetis_ops.CT_OPENING_OZON_EVIDENCE` e USING (order_number)
  GROUP BY u.internal_sku
),
letter AS (   -- письмо ФФ о нехватке (FF_OPERATIONAL_STOCK_2026-09-09.md §2)
  SELECT * FROM UNNEST([STRUCT('EVT-FS-ACNE-30' AS internal_sku, 41 AS ff_letter_deficit),
                        ('EVT-HC-HAND-300', 43), ('EVT-FC-ACNE-50', 39)])
)
SELECT s.internal_sku,
       s.shelf_ff, s.pallet_ff, ct.shelf_ct, ct.pallet_ct, ct.seed369,
       IFNULL(ev.on_ff_units, 0) AS reserved_ozon_units,
       IFNULL(ev.left_ff_units, 0) AS shipped_before_opening_units,
       s.shelf_ff - ct.shelf_ct AS seed_deducted_from_shelf,
       LEAST(IFNULL(ev.on_ff_units, 0), s.shelf_ff) AS reserved_from_shelf,
       IFNULL(ev.on_ff_units, 0) - LEAST(IFNULL(ev.on_ff_units, 0), s.shelf_ff) AS reserved_from_pallet,
       IFNULL(l.ff_letter_deficit, 0) AS ff_letter_deficit,
       ct.seed369 = IFNULL(ev.left_ff_units, 0) + (s.shelf_ff - ct.shelf_ct) AS id_a_seed369,
       (s.shelf_ff - ct.shelf_ct) = LEAST(IFNULL(ev.on_ff_units, 0), s.shelf_ff) AS id_b_shelf_rule,
       IFNULL(ev.on_ff_units, 0) - LEAST(IFNULL(ev.on_ff_units, 0), s.shelf_ff) = IFNULL(l.ff_letter_deficit, 0) AS id_c_letter,
       s.pallet_ff = ct.pallet_ct AS id_p_pallet,
       -- открытие журнала (физически, на 09.09):
       s.pallet_ff - (IFNULL(ev.on_ff_units, 0) - LEAST(IFNULL(ev.on_ff_units, 0), s.shelf_ff)) AS open_available_pallet,
       s.shelf_ff - LEAST(IFNULL(ev.on_ff_units, 0), s.shelf_ff) AS open_available_shelf,
       s.shelf_ff + s.pallet_ff AS open_ff_total
FROM snap s
LEFT JOIN ct USING (internal_sku)
LEFT JOIN ev USING (internal_sku)
LEFT JOIN letter l USING (internal_sku);
