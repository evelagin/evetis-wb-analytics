/** @OnlyCurrentDoc */
/**
 * EVETIS OPERATIONS · Stage C2 — автообновление листа из BigQuery. Конфигурация и контракт раскладки.
 *
 * Архитектура: BigQuery (расчёт, V_OPS_*) → контракт V_OPS_SHEET_* (ops_09_sheet_contract.sql)
 * → этот скрипт (только синхронизация) → существующие диапазоны листа (UI).
 * BigQuery скрипт только ЧИТАЕТ. Решения владельца («Одобрено владельцем») живут в скрытом листе
 * _OPS_STATE и командой на движение товара не являются.
 *
 * Файлы проекта (привязан к таблице EVETIS OPERATIONS):
 *   OpsConfig.gs  — константы, представления, раскладка листов, тексты интерфейса
 *   OpsCore.gs    — чистая логика: типы, проверка выборок, разбор раскладки, состояние решений, отрисовка
 *   OpsSheetIO.gs — чтение и запись листа: маркеры, размеры блоков, значения, _OPS_STATE, установка раскладки
 *   OpsMain.gs    — BigQuery, обновление с блокировкой, снимок, журнал запусков, меню, триггеры, onEdit
 * Документация: docs/ops/STAGE_C2_AUTO_REFRESH_2026-09-14.md
 */

var OPS = {
  PROJECT_ID: 'project-fa311fc0-4d87-4781-986',
  DATASET: 'evetis_ops',
  LOCATION: 'EU',
  PROD_SPREADSHEET_ID: '19J8EW-Xqz_twHk4ID7IvkumFOop4MTTZeX6U0uaNSAc',
  TZ: 'Europe/Moscow',
  LAYOUT_VERSION: 'C2L1',
  LOCK_WAIT_MS: 30000,          // обновление ждёт блокировку не дольше; иначе SKIPPED_LOCKED
  EDIT_LOCK_WAIT_MS: 120000,    // запись решения владельца ждёт окончания обновления
  BQ_TIMEOUT_MS: 240000,
  STALE_REFRESH_HOURS: 2,       // onOpen предупреждает, если успешного обновления не было дольше
  RUNLOG_KEEP: 20,
  SNAPSHOT_CHUNK: 8000,
  REFRESH_HANDLER: 'opsTriggerRefresh',
  EDIT_HANDLER: 'opsOnEditInstalled'
};

var OPS_SHEET = {
  PLAN: '01_SUPPLY_PLAN',
  SHIP: '02_SHIPMENTS',
  FF: '03_FF_STOCK',
  SETTINGS: '04_SETTINGS',
  CALC: '05_РАСЧЁТ',
  DATA: 'DATA',
  STATE: '_OPS_STATE'
};

/** Представления контракта и колонки, без которых отрисовка невозможна (проверка схемы → FAIL CLOSED). */
var OPS_VIEWS = {
  V_OPS_SHEET_PLAN: ['business_key', 'rec_fingerprint', 'sheet_block', 'block_order', 'calc_order', 'channel', 'card_sku',
    'product_name', 'is_bundle', 'rec_final', 'rec_physical_units', 'ff_free_cards_now', 'resulting_cover_days', 'risk_label',
    'short_reason', 'status_code', 'status_label', 'on_marketplace', 'in_transit', 'in_acceptance', 'reserved_on_ff',
    'other_inbound', 'committed_inbound', 'demand_until_arrival', 'projected_at_arrival', 'daily_plan', 'actual_daily_30d',
    'target_at_arrival', 'safety_units', 'need_math', 'multiple_applied', 'plan_actual_ratio', 'demand_confidence',
    'demand_warning', 'reason', 'today', 'contract_today', 'wb_ship_rows', 'wb_ship_positions', 'wb_ship_physical',
    'ozon_ship_rows', 'ozon_ship_positions', 'ozon_ship_physical', 'hold_rows', 'ch_rec_final', 'ch_rec_physical_units',
    'ch_on_marketplace', 'ch_committed_inbound', 'ch_need_math', 'ch_arrival_date', 'ch_arrival_date_max',
    'ch_next_arrival_date', 'ch_next_arrival_date_max'],
  V_OPS_SHEET_BUNDLES: ['row_key', 'sheet_order', 'is_build_row', 'product_name', 'bundle_sku', 'bom_text', 'capacity_alone',
    'capacity_alone_shelf', 'plan_day_wb', 'plan_day_ozon', 'ship_wb', 'ship_ozon', 'fbs_reserve', 'assembled_sets',
    'to_assemble_reserved', 'to_assemble_now', 'limiting_after_plan', 'capacity_after_plan', 'assemble_physical_units',
    't_to_assemble_now', 't_to_assemble_reserved', 't_assemble_physical_units', 't_ship_wb', 't_ship_ozon', 'contract_today'],
  V_OPS_SHEET_BOM: ['row_key', 'sheet_order', 'product_name', 'component_sku', 'component_qty', 'to_assemble_now',
    'units_total', 'units_from_reserved', 'units_from_free', 't_units_total', 't_units_from_reserved', 't_units_from_free',
    'contract_today'],
  V_OPS_SHEET_PICK: ['row_key', 'sheet_order', 'product_name', 'internal_sku', 'solo_wb_need', 'solo_ozon_need',
    'bundle_component_need', 'fbs_component_need', 'total_physical_demand', 'shelf_free', 'pallet_free', 'to_pick_from_pallet',
    'reserved_on_pallet_to_pick', 'pick_total_with_reserved', 'shelf_after_pick', 'pallet_after_pick', 'free_after_plan',
    't_solo_wb_need', 't_solo_ozon_need', 't_bundle_component_need', 't_total_physical_demand', 't_to_pick_from_pallet',
    't_reserved_on_pallet_to_pick', 't_pick_total_with_reserved', 't_pick_units', 'contract_today'],
  V_OPS_SHEET_FF_TASK: ['row_key', 'sheet_order', 'is_move_row', 'is_comp_row', 'product_name', 'internal_sku',
    'move_pallet_to_shelf_new', 'move_pallet_to_shelf_reserved', 'move_pallet_to_shelf_total', 'shelf_after_pick',
    'pallet_after_pick', 'solo_units_wb', 'solo_units_ozon', 'bundle_component_units', 'reserved_units_already',
    'total_physical_units', 'free_ff_after_operation', 't_move_pallet_to_shelf_new', 't_move_pallet_to_shelf_reserved',
    't_move_pallet_to_shelf_total', 't_solo_units_wb', 't_solo_units_ozon', 't_bundle_component_units',
    't_total_physical_units', 't_reserved_units_already', 't_reserved_units_on_pallet', 't_free_ff_after_operation',
    'contract_today'],
  V_OPS_SHEET_FF_STOCK: ['row_key', 'sheet_order', 'product_name', 'internal_sku', 'pallet_free', 'shelf_free', 'reserved_wb',
    'reserved_ozon', 'reserved_ozon_on_pallet', 'assembled', 'fbs_ready', 'shipped_unconfirmed', 'total_physical',
    'ozon_in_acceptance', 'on_ozon', 'on_wb', 't_pallet_free', 't_shelf_free', 't_reserved_wb', 't_reserved_ozon',
    't_reserved_ozon_on_pallet', 't_assembled', 't_fbs_ready', 't_shipped_unconfirmed', 't_total_physical',
    't_ozon_in_acceptance', 't_on_ozon', 't_on_wb', 'contract_today'],
  V_OPS_SHEET_SHIPMENTS: ['row_key', 'sheet_order', 'channel', 'order_number', 'stage', 'api_state', 'ledger_status', 'cards',
    'bundle_cards', 'physical_units', 'reserved_on_ff_units', 'reserved_shelf', 'reserved_pallet', 'pipeline_units',
    'handed_over_units', 'dropoff', 'planned_date', 'created_date', 'evidence', 'wb_rows', 'ozon_rows', 't_cards',
    't_physical_units', 't_reserved_on_ff_units', 't_pipeline_units', 't_handed_over_units', 'contract_today'],
  V_OPS_SHEET_CHANNELS: ['row_key', 'sheet_order', 'channel', 'shipping_method', 'target_cover_days', 'safety_stock_days',
    'lead_time_days', 'ff_prep_working_days', 'max_lot_weight_kg', 'max_lot_units', 'max_lot_volume_l', 'effective_from',
    'source', 'contract_today'],
  V_OPS_SHEET_CONFIG: ['row_key', 'sheet_order', 'config_key', 'config_value', 'note', 'contract_today'],
  V_OPS_SHEET_LOGISTICS: ['row_key', 'sheet_order', 'internal_sku', 'channel', 'shipment_multiple',
    'shipment_multiple_low_demand', 'min_shipment_units', 'factory_carton_qty', 'unit_weight_kg', 'unit_volume_l',
    'fbs_reserve_units', 'weight_class', 'multiple_class', 'carton_hint', 'effective_from', 'contract_today'],
  V_OPS_SHEET_META: ['row_key', 'contract_today', 'wb_shipping_method', 'wb_ship_date', 'wb_next_ship_date',
    'wb_target_cover_days', 'wb_safety_stock_days', 'wb_lead_time_days', 'wb_overstock_limit_days', 'wb_max_lot_weight_kg',
    'wb_max_lot_units', 'wb_max_lot_volume_l', 'ozon_ship_date', 'ozon_next_ship_date', 'ozon_target_cover_days',
    'ozon_safety_stock_days', 'ozon_lead_time_days', 'ozon_overstock_limit_days', 'ff_prep_working_days', 'cadence_days',
    'overstock_tolerance_days', 'expiry_margin_days', 'wb_stock_date', 'ozon_stock_date', 'ozon_orders_at', 'plan_version',
    'ledger_last_at', 'writeback_enabled', 'wb_stock_stale', 'ozon_stock_stale', 'ozon_orders_stale', 'plan_missing']
};

/** Колонки, которые меняются при каждом запросе и не означают изменения данных (не входят в хеш снимка). */
var OPS_VOLATILE_COLUMNS = ['computed_at'];

/** Нижняя и верхняя граница числа строк (защита от пустых или взорвавшихся выборок). */
var OPS_ROW_LIMITS = {
  V_OPS_SHEET_PLAN: [1, 500], V_OPS_SHEET_BUNDLES: [0, 200], V_OPS_SHEET_BOM: [0, 1000], V_OPS_SHEET_PICK: [0, 300],
  V_OPS_SHEET_FF_TASK: [0, 300], V_OPS_SHEET_FF_STOCK: [1, 300], V_OPS_SHEET_SHIPMENTS: [0, 500],
  V_OPS_SHEET_CHANNELS: [1, 50], V_OPS_SHEET_CONFIG: [1, 200], V_OPS_SHEET_LOGISTICS: [1, 2000], V_OPS_SHEET_META: [1, 1]
};

var OPS_USEND_KEYS = ['PICK', 'BUILD', 'SHIP_WB', 'SHIP_OZON', 'RESERVED', 'FREE_AFTER'];
var OPS_STATUS_KEYS = ['LAST_SUCCESS', 'LAST_CHECK', 'STATUS', 'FRESHNESS', 'NEXT_AUTO'];
var OPS_RULE_KEYS = ['1', '2', '3', '4', '5', '6', '7'];
var OPS_FRESH_KEYS = ['WB_STOCK', 'OZON_STOCK', 'OZON_ORDERS', 'PLAN_VERSION', 'LEDGER_LAST', 'WRITEBACK'];

/**
 * Контракт раскладки. В скрытой системной колонке sysCol каждого листа у каждой строки раскладки есть маркер:
 *   @L:<версия>        первая строка листа            @S          статичная строка (не трогается)
 *   @V:<блок>          строка со сгенерированным текстом
 *   @B:<блок>          полоса блока (текст и заметка генерируются)
 *   @H:<блок>          шапка блока (не трогается)
 *   @R:<блок>:<ключ>   строка данных; у отгрузки ключ = business_key#отпечаток
 *   @E:<блок>          пустой блок («Нет позиций»)
 *   @T:<блок>          строка ИТОГО                   @END        конец раскладки
 * Блоки ищутся только по маркерам, не по видимым заголовкам. Обновление меняет число строк ТОЛЬКО
 * внутри блоков kind='rows'; нарушение контракта → FAIL CLOSED без записи.
 *
 * segments — сгенерированные колонки (объединённые ячейки пишутся по левой колонке);
 * merges — объединения, которые нужны новым строкам блока; ownerCol — колонка решения владельца.
 */
var OPS_LAYOUT = {
  '01_SUPPLY_PLAN': { sysCol: 9, grid: 8, blocks: [
    { id: 'STATUS_LINE', kind: 'value' },
    { id: 'USEND_TOP', kind: 'fixed', header: true, keys: OPS_USEND_KEYS, segments: [[1, 3], [4, 4]] },
    { id: 'WB_SHIP', kind: 'rows', band: true, header: true, segments: [[1, 3], [5, 8]], ownerCol: 4 },
    { id: 'OZON_SHIP', kind: 'rows', band: true, header: true, segments: [[1, 3], [5, 8]], ownerCol: 4 },
    { id: 'HOLD', kind: 'rows', band: true, header: true, segments: [[1, 3], [5, 5]], merges: [[3, 4], [5, 8]] },
    { id: 'USEND_TASK', kind: 'fixed', header: true, keys: OPS_USEND_KEYS, segments: [[1, 3], [4, 4]] },
    { id: 'TASK_MOVE', kind: 'rows', header: true, total: true, segments: [[1, 7]] },
    { id: 'TASK_BUILD', kind: 'rows', header: true, total: true, segments: [[1, 8]] },
    { id: 'TASK_COMP', kind: 'rows', header: true, total: true, segments: [[1, 8]] }
  ] },
  '02_SHIPMENTS': { sysCol: 18, grid: 17, blocks: [
    { id: 'SHIP_SUBTITLE', kind: 'value' },
    { id: 'SHIPMENTS', kind: 'rows', band: true, header: true, total: true, segments: [[1, 17]] },
    { id: 'SHIP_WB_LINE', kind: 'value' }
  ] },
  '03_FF_STOCK': { sysCol: 15, grid: 14, blocks: [
    { id: 'FF_SUBTITLE', kind: 'value' },
    { id: 'FF_TOTAL_LINE', kind: 'value' },
    { id: 'FF_STOCK', kind: 'rows', header: true, total: true, segments: [[1, 14]] }
  ] },
  '04_SETTINGS': { sysCol: 14, grid: 13, blocks: [
    { id: 'REFRESH_STATUS', kind: 'fixed', header: false, keys: OPS_STATUS_KEYS, segments: [[1, 1], [3, 3]] },
    { id: 'RULES', kind: 'fixed', header: false, keys: OPS_RULE_KEYS, segments: [[1, 1]] },
    { id: 'CHANNELS', kind: 'rows', header: true, segments: [[1, 11]] },
    { id: 'CONFIG', kind: 'rows', header: true, segments: [[1, 3]] },
    { id: 'LOGISTICS', kind: 'rows', header: true, segments: [[1, 13]] },
    { id: 'FRESHNESS', kind: 'fixed', header: false, keys: OPS_FRESH_KEYS, segments: [[1, 1], [3, 3]] }
  ] },
  '05_РАСЧЁТ': { sysCol: 26, grid: 25, blocks: [
    { id: 'CALC_WB', kind: 'rows', band: true, header: true, total: true, segments: [[1, 25]] },
    { id: 'CALC_OZON', kind: 'rows', band: true, header: true, total: true, segments: [[1, 25]] },
    { id: 'CALC_BUNDLES', kind: 'rows', header: true, total: true, segments: [[1, 15]] },
    { id: 'CALC_BOM', kind: 'rows', header: true, total: true, segments: [[1, 7]] },
    { id: 'CALC_PICK', kind: 'rows', header: true, total: true, segments: [[1, 15]] }
  ] }
};

/** DATA: скрытый сырой слой. Системная колонка A, данные с колонки B; блок на каждое представление. */
var OPS_DATA_SYS_COL = 1;
function opsDataLayout_() {
  return {
    sysCol: OPS_DATA_SYS_COL, grid: 0, dynamicGrid: true,
    blocks: Object.keys(OPS_VIEWS).map(function (v) {
      return { id: 'D_' + v, kind: 'rows', band: true, header: true, segments: null };
    })
  };
}

/** Колонки строк (номер колонки → поле) и итогов (номер колонки → поле-итог или текст). */
var OPS_COLS = {
  SHIP_TOP: { 1: 'product_name', 2: 'rec_final', 3: 'rec_physical_units', 5: 'ff_free_cards_now', 6: 'resulting_cover_days',
    7: 'risk_label', 8: 'short_reason' },
  HOLD: { 1: 'product_name', 2: 'rec_final', 3: 'status_label', 5: 'short_reason' },
  TASK_MOVE: { 1: 'product_name', 2: 'internal_sku', 3: 'move_pallet_to_shelf_new', 4: 'move_pallet_to_shelf_reserved',
    5: 'move_pallet_to_shelf_total', 6: 'shelf_after_pick', 7: 'pallet_after_pick' },
  TASK_MOVE_T: { 3: 't_move_pallet_to_shelf_new', 4: 't_move_pallet_to_shelf_reserved', 5: 't_move_pallet_to_shelf_total' },
  TASK_BUILD: { 1: 'product_name', 2: 'to_assemble_now', 3: 'to_assemble_reserved', 4: 'assemble_physical_units',
    5: 'limiting_after_plan', 8: 'bom_text' },
  TASK_BUILD_T: { 2: 't_to_assemble_now', 3: 't_to_assemble_reserved', 4: 't_assemble_physical_units' },
  TASK_COMP: { 1: 'product_name', 2: 'internal_sku', 3: 'solo_units_wb', 4: 'solo_units_ozon', 5: 'bundle_component_units',
    6: 'reserved_units_already', 7: 'total_physical_units', 8: 'free_ff_after_operation' },
  TASK_COMP_T: { 3: 't_solo_units_wb', 4: 't_solo_units_ozon', 5: 't_bundle_component_units', 6: 't_reserved_units_already',
    7: 't_total_physical_units', 8: 't_free_ff_after_operation' },
  SHIPMENTS: { 1: 'channel', 2: 'order_number', 3: 'stage', 4: 'api_state', 5: 'ledger_status', 6: 'cards', 7: 'bundle_cards',
    8: 'physical_units', 9: 'reserved_on_ff_units', 10: 'reserved_shelf', 11: 'reserved_pallet', 12: 'pipeline_units',
    13: 'handed_over_units', 14: 'dropoff', 15: 'planned_date', 16: 'created_date', 17: 'evidence' },
  SHIPMENTS_T: { 6: 't_cards', 8: 't_physical_units', 9: 't_reserved_on_ff_units', 12: 't_pipeline_units',
    13: 't_handed_over_units' },
  FF_STOCK: { 1: 'product_name', 2: 'internal_sku', 3: 'pallet_free', 4: 'shelf_free', 5: 'reserved_wb', 6: 'reserved_ozon',
    7: 'reserved_ozon_on_pallet', 8: 'assembled', 9: 'fbs_ready', 10: 'shipped_unconfirmed', 11: 'total_physical',
    12: 'ozon_in_acceptance', 13: 'on_ozon', 14: 'on_wb' },
  FF_STOCK_T: { 3: 't_pallet_free', 4: 't_shelf_free', 5: 't_reserved_wb', 6: 't_reserved_ozon', 7: 't_reserved_ozon_on_pallet',
    8: 't_assembled', 9: 't_fbs_ready', 10: 't_shipped_unconfirmed', 11: 't_total_physical', 12: 't_ozon_in_acceptance',
    13: 't_on_ozon', 14: 't_on_wb' },
  CHANNELS: { 1: 'channel', 2: 'shipping_method', 3: 'target_cover_days', 4: 'safety_stock_days', 5: 'lead_time_days',
    6: 'ff_prep_working_days', 7: 'max_lot_weight_kg', 8: 'max_lot_units', 9: 'max_lot_volume_l', 10: 'effective_from',
    11: 'source' },
  CONFIG: { 1: 'config_key', 2: 'config_value', 3: 'note' },
  LOGISTICS: { 1: 'internal_sku', 2: 'channel', 3: 'shipment_multiple', 4: 'shipment_multiple_low_demand',
    5: 'min_shipment_units', 6: 'factory_carton_qty', 7: 'unit_weight_kg', 8: 'unit_volume_l', 9: 'fbs_reserve_units',
    10: 'weight_class', 11: 'multiple_class', 12: 'carton_hint', 13: 'effective_from' },
  CALC_PLAN: { 1: 'product_name', 2: 'card_sku', 3: 'on_marketplace', 4: 'in_transit', 5: 'in_acceptance', 6: 'reserved_on_ff',
    7: 'other_inbound', 8: 'committed_inbound', 9: 'demand_until_arrival', 10: 'projected_at_arrival', 11: 'daily_plan',
    12: 'actual_daily_30d', 13: 'target_at_arrival', 14: 'safety_units', 15: 'need_math', 16: 'multiple_applied',
    17: 'plan_actual_ratio', 18: 'demand_confidence', 19: 'rec_final', 20: 'rec_physical_units', 21: 'resulting_cover_days',
    22: 'ff_free_cards_now', 23: 'status_label', 24: 'demand_warning', 25: 'reason' },
  CALC_PLAN_T: { 3: 'ch_on_marketplace', 8: 'ch_committed_inbound', 15: 'ch_need_math', 19: 'ch_rec_final',
    20: 'ch_rec_physical_units' },
  CALC_BUNDLES: { 1: 'product_name', 2: 'bundle_sku', 3: 'bom_text', 4: 'capacity_alone', 5: 'capacity_alone_shelf',
    6: 'plan_day_wb', 7: 'plan_day_ozon', 8: 'ship_wb', 9: 'ship_ozon', 10: 'fbs_reserve', 11: 'assembled_sets',
    12: 'to_assemble_reserved', 13: 'to_assemble_now', 14: 'limiting_after_plan', 15: 'capacity_after_plan' },
  CALC_BUNDLES_T: { 8: 't_ship_wb', 9: 't_ship_ozon', 12: 't_to_assemble_reserved', 13: 't_to_assemble_now' },
  CALC_BOM: { 1: 'product_name', 2: 'component_sku', 3: 'component_qty', 4: 'to_assemble_now', 5: 'units_total',
    6: 'units_from_reserved', 7: 'units_from_free' },
  CALC_BOM_T: { 5: 't_units_total', 6: 't_units_from_reserved', 7: 't_units_from_free' },
  CALC_PICK: { 1: 'product_name', 2: 'internal_sku', 3: 'solo_wb_need', 4: 'solo_ozon_need', 5: 'bundle_component_need',
    6: 'fbs_component_need', 7: 'total_physical_demand', 8: 'shelf_free', 9: 'pallet_free', 10: 'to_pick_from_pallet',
    11: 'reserved_on_pallet_to_pick', 12: 'pick_total_with_reserved', 13: 'shelf_after_pick', 14: 'pallet_after_pick',
    15: 'free_after_plan' },
  CALC_PICK_T: { 3: 't_solo_wb_need', 4: 't_solo_ozon_need', 5: 't_bundle_component_need', 7: 't_total_physical_demand',
    10: 't_to_pick_from_pallet', 11: 't_reserved_on_pallet_to_pick', 12: 't_pick_total_with_reserved' }
};

/** Палитра C1.2 (tools/ops_sheet_build.py): статус → [заливка, цвет текста, жирный]. */
var OPS_ST = {
  'SHIP': ['#e6f4ea', '#274e13', true], 'WAIT': ['#fff2cc', '#7f6000', false], 'REVIEW': ['#fce8e6', '#a61c00', true],
  'FF LIMIT': ['#fce8e6', '#a61c00', false], 'OK': ['#efefef', '#434343', false], 'NO PLAN': ['#ffffff', '#b7b7b7', false]
};
var OPS_COLOR = { DARK: '#434343', MUTED: '#b7b7b7', GREY: '#666666', BLUE: '#0b5394', BLUE_BG: '#e8f0fe', WHITE: '#ffffff',
  RED: '#a61c00', RED_BG: '#fce8e6', AMBER: '#7f6000', AMBER_BG: '#fff2cc', GREEN: '#274e13', GREEN_BG: '#e6f4ea' };

/** Тексты интерфейса (всё, что видит владелец). */
var OPS_TEXT = {
  EMPTY_ROW: 'Нет позиций',
  // колонка D узкая (ширину C1.2 менять нельзя): в ячейке короткая метка, полный текст — в заметке ячейки
  REAPPROVAL_SHORT: 'ПЕРЕСОГЛ. ',   // «ПЕРЕСОГЛ. 1200» — 99 px шрифтом 9 bold: помещается в колонку D C1.2
  REAPPROVAL_NOTE_PREFIX: 'ПЕРЕСОГЛАСОВАТЬ · ранее: ',
  USEND_LABELS: {
    PICK: 'Снять с паллет на полку', BUILD: 'Собрать наборы', SHIP_WB: 'Подготовить к отгрузке: WILDBERRIES',
    SHIP_OZON: 'Подготовить к отгрузке: OZON', RESERVED: 'Уже зарезервировано на ФФ', FREE_AFTER: 'Останется свободно на ФФ'
  },
  STATUS_LABELS: {
    LAST_SUCCESS: 'Последнее успешное обновление', LAST_CHECK: 'Последняя проверка', STATUS: 'Статус',
    FRESHNESS: 'Свежесть данных', NEXT_AUTO: 'Следующее автообновление'
  },
  FRESH_LABELS: {
    WB_STOCK: 'Остатки WB (снимок)', OZON_STOCK: 'Остатки Ozon (снимок)', OZON_ORDERS: 'Заказы поставок Ozon (выгрузка)',
    PLAN_VERSION: 'Версия плана продаж', LEDGER_LAST: 'Последнее движение в журнале ФФ',
    WRITEBACK: 'Запись владельца (writeback_enabled)'
  },
  ERRORS: {
    BQ_QUERY: 'запрос к BigQuery не выполнен', BQ_TIMEOUT: 'BigQuery не ответил вовремя',
    SCHEMA: 'в данных нет ожидаемых колонок', DUP_KEY: 'повторяется ключ строки', ROWCOUNT: 'неожиданное число строк',
    RECONCILE: 'выборки не сходятся между собой', INCONSISTENT_DAY: 'выборки получены за разные дни',
    LAYOUT: 'структура листа изменена вручную', STATE_SHEET: 'служебный лист _OPS_STATE повреждён',
    WRITE: 'запись в лист прервана — восстановится при следующем обновлении', INTERNAL: 'внутренняя ошибка скрипта'
  },
  OWNER_NOTE: 'Ваше решение по рекомендации этой строки: введите количество, позиций. Решение сохраняется при обновлении, ' +
    'пока рекомендация не изменилась. Если рекомендация изменилась или строка уходила из отгрузки, поле показывает ' +
    '«ПЕРЕСОГЛ. N» (N — прежнее решение, подробности в заметке ячейки) — подтвердите заново. ' +
    'Это решение в интерфейсе, а не команда на отгрузку: ' +
    'в журнал и BigQuery ничего не пишется.',
  NOTE_TASK_MOVE_RESERVED: 'Резерв уже созданных поставок, который физически лежит на паллетах — его тоже нужно снять.',
  NOTE_PICK_RESERVED: 'Уже зарезервировано под созданные поставки, но лежит на паллетах — тоже нужно снять.',
  NOTE_DEMAND_UNTIL_ARRIVAL: 'План продаж с сегодня до даты прибытия: сборка на ФФ (рабочие дни — лист 04_SETTINGS) + срок канала.',
  NOTE_TARGET: '= план/день × (целевое покрытие + страховые дни). Целевое покрытие считается ПОСЛЕ прибытия.',
  NOTE_SAFETY: 'Страховой запас = план/день × страховые дни (по каналу — лист 04_SETTINGS).',
  NOTE_COVER_AFTER: 'На сколько дней хватит после прибытия. Выше порога затоваривания (по каналу — лист 04_SETTINGS) — статус ПРОВЕРИТЬ.',
  NOTE_FBS_RESERVE: 'Резерв наборов под FBS из логистики SKU (0 — FBS не используется).',
  SHIP_EVIDENCE_HEADER: 'Доказательство статуса',
  STATE_TITLE: 'C2 · состояние решений владельца (служебный лист, не редактировать)'
};
