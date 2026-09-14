-- ============================================================================
-- EVETIS · Stage C2 · evetis_ops · 09 — контракт листов EVETIS OPERATIONS (только чтение)
--
-- Тонкие представления V_OPS_SHEET_* поверх представлений C1 (V_OPS_*): канонический ключ строки,
-- отпечаток рекомендации, принадлежность к блоку листа, порядок строк, итоги и счётчики для полос.
-- Расчёт поставок остаётся в C1 (ops_08_supply_plan.sql) и здесь не повторяется и не меняется:
-- контракт только выбирает, упорядочивает и суммирует то, что уже посчитано.
--
-- Читатель — Apps Script apps-script/evetis_operations (синхронизация листа). Каждое представление
-- читается ОТДЕЛЬНЫМ запросом: объединять плановые V_OPS_* в один запрос нельзя — BigQuery падает
-- на планировании («too complex»). Поэтому итоги — оконные SUM() OVER () на одной ссылке, а
-- согласованность выборок между представлениями проверяют Apps Script и инварианты I24.
--
-- Ключ рекомендации: SUPPLY_SHIP | channel | card_sku. Зерно плана C1 — канал × карточка;
-- способ отгрузки у канала один (REF_CHANNEL_SHIPPING.is_planning_default), он входит в отпечаток.
-- Отпечаток — суть решения владельца: ключ, способ отгрузки, статус, правило округления,
-- количество в позициях и физических единицах, сработавшие ворота. Даты, название товара,
-- время расчёта и уверенность в спросе в отпечаток НЕ входят.
-- ============================================================================

CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_SHEET_PLAN` AS
WITH cal AS (
  SELECT channel, shipping_method FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_SUPPLY_CALENDAR`
),
p AS (
  SELECT
    p.*,
    cal.shipping_method,
    CASE WHEN p.rec_final > 0 THEN CONCAT(p.channel, '_SHIP')
         WHEN p.status_code IN ('REVIEW', 'FF LIMIT', 'WAIT') THEN 'HOLD' END AS sheet_block,
    CONCAT(IF(p.review_overstock, 'O', ''), IF(p.review_expiry, 'E', ''),
           IF(p.acceptance_flip, 'A', ''), IF(p.ff_limited, 'F', '')) AS gates
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_SUPPLY_PLAN` p
  LEFT JOIN cal USING (channel)
)
SELECT
  CONCAT('SUPPLY_SHIP|', channel, '|', card_sku) AS business_key,
  SUBSTR(TO_HEX(SHA256(CONCAT(
    'SUPPLY_SHIP|', channel, '|', card_sku, '|', IFNULL(shipping_method, '-'), '|', IFNULL(status_code, '-'), '|',
    IFNULL(rule, '-'), '|', CAST(IFNULL(rec_final, -1) AS STRING), '|', CAST(IFNULL(rec_physical_units, -1) AS STRING), '|',
    gates))), 1, 16) AS rec_fingerprint,
  CASE WHEN sheet_block = 'HOLD'
         THEN ROW_NUMBER() OVER (PARTITION BY sheet_block ORDER BY status_code, channel, card_sku)
       WHEN sheet_block IS NOT NULL
         THEN ROW_NUMBER() OVER (PARTITION BY sheet_block ORDER BY rec_physical_units DESC, sort_key, card_sku)
  END AS block_order,
  ROW_NUMBER() OVER (PARTITION BY channel ORDER BY sort_key, card_sku) AS calc_order,
  IFNULL(api_reserved_unconfirmed, 0) + IFNULL(shipped_unconfirmed, 0) AS other_inbound,
  -- полосы и задание Usend на 01_SUPPLY_PLAN
  COUNTIF(sheet_block = 'WB_SHIP') OVER () AS wb_ship_rows,
  SUM(IF(sheet_block = 'WB_SHIP', rec_final, 0)) OVER () AS wb_ship_positions,
  SUM(IF(sheet_block = 'WB_SHIP', rec_physical_units, 0)) OVER () AS wb_ship_physical,
  COUNTIF(sheet_block = 'OZON_SHIP') OVER () AS ozon_ship_rows,
  SUM(IF(sheet_block = 'OZON_SHIP', rec_final, 0)) OVER () AS ozon_ship_positions,
  SUM(IF(sheet_block = 'OZON_SHIP', rec_physical_units, 0)) OVER () AS ozon_ship_physical,
  COUNTIF(sheet_block = 'HOLD') OVER () AS hold_rows,
  -- таблицы канала на 05_РАСЧЁТ
  COUNT(*) OVER (PARTITION BY channel) AS ch_rows,
  SUM(rec_final) OVER (PARTITION BY channel) AS ch_rec_final,
  SUM(rec_physical_units) OVER (PARTITION BY channel) AS ch_rec_physical_units,
  SUM(on_marketplace) OVER (PARTITION BY channel) AS ch_on_marketplace,
  SUM(committed_inbound) OVER (PARTITION BY channel) AS ch_committed_inbound,
  SUM(need_math) OVER (PARTITION BY channel) AS ch_need_math,
  MIN(arrival_date) OVER (PARTITION BY channel) AS ch_arrival_date,
  MAX(arrival_date) OVER (PARTITION BY channel) AS ch_arrival_date_max,
  MIN(next_arrival_date) OVER (PARTITION BY channel) AS ch_next_arrival_date,
  MAX(next_arrival_date) OVER (PARTITION BY channel) AS ch_next_arrival_date_max,
  CURRENT_DATE('Europe/Moscow') AS contract_today,
  p.*
FROM p;

CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_SHEET_BUNDLES` AS
SELECT
  bundle_sku AS row_key,
  ROW_NUMBER() OVER (ORDER BY to_assemble_now DESC, bundle_sku) AS sheet_order,
  IFNULL(to_assemble_now, 0) > 0 AS is_build_row,
  IFNULL(to_assemble_now, 0) * IFNULL(components, 0) AS assemble_physical_units,
  COUNTIF(IFNULL(to_assemble_now, 0) > 0) OVER () AS build_rows,
  SUM(to_assemble_now) OVER () AS t_to_assemble_now,
  SUM(to_assemble_reserved) OVER () AS t_to_assemble_reserved,
  SUM(IFNULL(to_assemble_now, 0) * IFNULL(components, 0)) OVER () AS t_assemble_physical_units,
  SUM(ship_wb) OVER () AS t_ship_wb,
  SUM(ship_ozon) OVER () AS t_ship_ozon,
  CURRENT_DATE('Europe/Moscow') AS contract_today,
  b.*
FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_BUNDLE_PRODUCTION` b;

CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_SHEET_BOM` AS
SELECT
  CONCAT(bundle_sku, '|', component_sku) AS row_key,
  ROW_NUMBER() OVER (ORDER BY bundle_sku, component_sku) AS sheet_order,
  SUM(units_total) OVER () AS t_units_total,
  SUM(units_from_reserved) OVER () AS t_units_from_reserved,
  SUM(units_from_free) OVER () AS t_units_from_free,
  CURRENT_DATE('Europe/Moscow') AS contract_today,
  b.*
FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_BUNDLE_BOM_EXPANSION` b;

CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_SHEET_PICK` AS
SELECT
  internal_sku AS row_key,
  ROW_NUMBER() OVER (ORDER BY to_pick_from_pallet DESC, internal_sku) AS sheet_order,
  SUM(solo_wb_need) OVER () AS t_solo_wb_need,
  SUM(solo_ozon_need) OVER () AS t_solo_ozon_need,
  SUM(bundle_component_need) OVER () AS t_bundle_component_need,
  SUM(fbs_component_need) OVER () AS t_fbs_component_need,
  SUM(total_physical_demand) OVER () AS t_total_physical_demand,
  SUM(to_pick_from_pallet) OVER () AS t_to_pick_from_pallet,
  SUM(reserved_on_pallet_to_pick) OVER () AS t_reserved_on_pallet_to_pick,
  SUM(pick_total_with_reserved) OVER () AS t_pick_total_with_reserved,
  SUM(IFNULL(to_pick_from_pallet, 0) + IFNULL(reserved_on_pallet_to_pick, 0)) OVER () AS t_pick_units,
  CURRENT_DATE('Europe/Moscow') AS contract_today,
  k.*
FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_PICK_FROM_STORAGE` k;

CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_SHEET_FF_TASK` AS
SELECT
  internal_sku AS row_key,
  ROW_NUMBER() OVER (ORDER BY move_pallet_to_shelf_total DESC, internal_sku) AS sheet_order,
  IFNULL(move_pallet_to_shelf_total, 0) > 0 AS is_move_row,
  IFNULL(total_physical_units, 0) > 0 OR IFNULL(reserved_units_already, 0) > 0 AS is_comp_row,
  COUNTIF(IFNULL(move_pallet_to_shelf_total, 0) > 0) OVER () AS move_rows,
  COUNTIF(IFNULL(total_physical_units, 0) > 0 OR IFNULL(reserved_units_already, 0) > 0) OVER () AS comp_rows,
  SUM(move_pallet_to_shelf_new) OVER () AS t_move_pallet_to_shelf_new,
  SUM(move_pallet_to_shelf_reserved) OVER () AS t_move_pallet_to_shelf_reserved,
  SUM(move_pallet_to_shelf_total) OVER () AS t_move_pallet_to_shelf_total,
  SUM(solo_units_wb) OVER () AS t_solo_units_wb,
  SUM(solo_units_ozon) OVER () AS t_solo_units_ozon,
  SUM(bundle_component_units) OVER () AS t_bundle_component_units,
  SUM(fbs_component_units) OVER () AS t_fbs_component_units,
  SUM(total_physical_units) OVER () AS t_total_physical_units,
  SUM(reserved_units_already) OVER () AS t_reserved_units_already,
  SUM(reserved_units_on_pallet) OVER () AS t_reserved_units_on_pallet,
  SUM(free_ff_after_operation) OVER () AS t_free_ff_after_operation,
  CURRENT_DATE('Europe/Moscow') AS contract_today,
  t.*
FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_FF_TASK` t;

CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_SHEET_FF_STOCK` AS
SELECT
  internal_sku AS row_key,
  ROW_NUMBER() OVER (ORDER BY total_physical DESC, internal_sku) AS sheet_order,
  SUM(pallet_free) OVER () AS t_pallet_free,
  SUM(shelf_free) OVER () AS t_shelf_free,
  SUM(reserved_wb) OVER () AS t_reserved_wb,
  SUM(reserved_ozon) OVER () AS t_reserved_ozon,
  SUM(reserved_ozon_on_pallet) OVER () AS t_reserved_ozon_on_pallet,
  SUM(assembled) OVER () AS t_assembled,
  SUM(fbs_ready) OVER () AS t_fbs_ready,
  SUM(shipped_unconfirmed) OVER () AS t_shipped_unconfirmed,
  SUM(total_physical) OVER () AS t_total_physical,
  SUM(ozon_in_acceptance) OVER () AS t_ozon_in_acceptance,
  SUM(on_ozon) OVER () AS t_on_ozon,
  SUM(on_wb) OVER () AS t_on_wb,
  CURRENT_DATE('Europe/Moscow') AS contract_today,
  f.*
FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_FF_STOCK_SHEET` f;

CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_SHEET_SHIPMENTS` AS
SELECT
  IFNULL(shipment_id, CONCAT(channel, '|', order_number)) AS row_key,
  ROW_NUMBER() OVER (ORDER BY stage, order_number, shipment_id) AS sheet_order,
  COUNTIF(channel = 'WB') OVER () AS wb_rows,
  COUNTIF(channel = 'OZON') OVER () AS ozon_rows,
  SUM(cards) OVER () AS t_cards,
  SUM(physical_units) OVER () AS t_physical_units,
  SUM(reserved_on_ff_units) OVER () AS t_reserved_on_ff_units,
  SUM(pipeline_units) OVER () AS t_pipeline_units,
  SUM(handed_over_units) OVER () AS t_handed_over_units,
  CURRENT_DATE('Europe/Moscow') AS contract_today,
  s.*
FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_SHIPMENTS_SHEET` s;

CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_SHEET_CHANNELS` AS
SELECT
  CONCAT(channel, '|', shipping_method, '|', CAST(effective_from AS STRING)) AS row_key,
  ROW_NUMBER() OVER (ORDER BY channel DESC, shipping_method, effective_from) AS sheet_order,
  CURRENT_DATE('Europe/Moscow') AS contract_today,
  channel, shipping_method, is_planning_default, target_cover_days, safety_stock_days, lead_time_days,
  ff_prep_working_days, max_lot_weight_kg, max_lot_units, max_lot_volume_l, effective_from, effective_to, source
FROM `project-fa311fc0-4d87-4781-986.evetis_ops.REF_CHANNEL_SHIPPING`;

CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_SHEET_CONFIG` AS
SELECT
  config_key AS row_key,
  ROW_NUMBER() OVER (ORDER BY config_key) AS sheet_order,
  CURRENT_DATE('Europe/Moscow') AS contract_today,
  config_key, config_value, note
FROM `project-fa311fc0-4d87-4781-986.evetis_ops.OPS_CONFIG`;

CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_SHEET_LOGISTICS` AS
SELECT
  CONCAT(channel, '|', internal_sku, '|', CAST(effective_from AS STRING)) AS row_key,
  ROW_NUMBER() OVER (ORDER BY channel DESC, internal_sku, effective_from) AS sheet_order,
  CURRENT_DATE('Europe/Moscow') AS contract_today,
  internal_sku, channel, shipment_multiple, shipment_multiple_low_demand, min_shipment_units,
  factory_carton_qty, unit_weight_kg, unit_volume_l, box_weight_kg, fbs_reserve_units,
  JSON_VALUE(field_provenance, '$.unit_weight_kg.class') AS weight_class,
  JSON_VALUE(field_provenance, '$.shipment_multiple.class') AS multiple_class,
  JSON_VALUE(field_provenance, '$.factory_carton_qty.hint') AS carton_hint,
  effective_from, effective_to, source
FROM `project-fa311fc0-4d87-4781-986.evetis_ops.REF_SKU_LOGISTICS`;

-- Одна строка: календарь поставок по каналам, параметры правил (для текстов листа) и свежесть источников.
-- Источник устарел, если его срез старше OPS_CONFIG.max_api_age_hours (для суточного среза — дата
-- среза раньше даты «сейчас минус порог» по Москве).
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_SHEET_META` AS
WITH cal AS (
  SELECT * FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_SUPPLY_CALENDAR`
),
cfg AS (
  SELECT
    SAFE_CAST(MAX(IF(config_key = 'max_api_age_hours', config_value, NULL)) AS INT64) AS max_api_age_hours,
    MAX(IF(config_key = 'writeback_enabled', config_value, NULL)) AS writeback_enabled
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.OPS_CONFIG`
),
fresh AS (
  SELECT
    (SELECT MAX(_snapshot_date) FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_STOCKS_T5_CURRENT`) AS wb_stock_date,
    (SELECT MAX(snapshot_date) FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_STOCKS`) AS ozon_stock_date,
    (SELECT MAX(extracted_at) FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_SUPPLY_ORDERS`) AS ozon_orders_at,
    (SELECT ANY_VALUE(plan_version) FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_PLAN_ACTIVE`) AS plan_version,
    (SELECT MAX(recorded_at) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_STOCK_MOVEMENT`) AS ledger_last_at
)
SELECT
  'META' AS row_key,
  CURRENT_DATE('Europe/Moscow') AS contract_today,
  wb.shipping_method AS wb_shipping_method,
  wb.ship_date AS wb_ship_date,
  wb.next_ship_date AS wb_next_ship_date,
  wb.target_cover_days AS wb_target_cover_days,
  wb.safety_stock_days AS wb_safety_stock_days,
  wb.lead_time_days AS wb_lead_time_days,
  wb.overstock_limit_days AS wb_overstock_limit_days,
  wb.max_lot_weight_kg AS wb_max_lot_weight_kg,
  wb.max_lot_units AS wb_max_lot_units,
  wb.max_lot_volume_l AS wb_max_lot_volume_l,
  oz.shipping_method AS ozon_shipping_method,
  oz.ship_date AS ozon_ship_date,
  oz.next_ship_date AS ozon_next_ship_date,
  oz.target_cover_days AS ozon_target_cover_days,
  oz.safety_stock_days AS ozon_safety_stock_days,
  oz.lead_time_days AS ozon_lead_time_days,
  oz.overstock_limit_days AS ozon_overstock_limit_days,
  wb.ff_prep_working_days,
  wb.cadence_days,
  wb.overstock_tolerance_days,
  wb.expiry_margin_days,
  fresh.wb_stock_date,
  fresh.ozon_stock_date,
  fresh.ozon_orders_at,
  fresh.plan_version,
  fresh.ledger_last_at,
  cfg.writeback_enabled,
  cfg.max_api_age_hours,
  fresh.wb_stock_date IS NULL
    OR fresh.wb_stock_date < DATE(TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL cfg.max_api_age_hours HOUR), 'Europe/Moscow')
    AS wb_stock_stale,
  fresh.ozon_stock_date IS NULL
    OR fresh.ozon_stock_date < DATE(TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL cfg.max_api_age_hours HOUR), 'Europe/Moscow')
    AS ozon_stock_stale,
  fresh.ozon_orders_at IS NULL
    OR fresh.ozon_orders_at < TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL cfg.max_api_age_hours HOUR)
    AS ozon_orders_stale,
  fresh.plan_version IS NULL AS plan_missing
FROM (SELECT * FROM cal WHERE channel = 'WB') wb
CROSS JOIN (SELECT * FROM cal WHERE channel = 'OZON') oz
CROSS JOIN cfg
CROSS JOIN fresh;
