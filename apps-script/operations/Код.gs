// EVETIS OPERATIONS · C2 — сборка из apps-script/evetis_operations: OpsConfig.gs, OpsCore.gs, OpsSheetIO.gs, OpsInstall.gs, OpsMain.gs (порядок важен)

// ═══════════ OpsConfig.gs ═══════════
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

/** _OPS_STATE: таблица состояния (A:N) и журнал событий решений (P:V). */
var OPS_STATE_COLS = ['business_key', 'current_fingerprint', 'current_block', 'current_rec_final', 'approved_fingerprint',
  'approved_qty', 'approval_status', 'approved_at', 'last_seen_at', 'previous_approved_qty', 'previous_approved_fingerprint',
  'previous_approved_at', 'invalidated_at', 'invalidation_reason'];
var OPS_EVENT_COLS = ['event_at', 'business_key', 'event', 'qty', 'fingerprint', 'reason', 'source'];
var OPS_EVENT_COL0 = OPS_STATE_COLS.length + 2;   // P

// ═══════════ OpsCore.gs ═══════════
/**
 * EVETIS OPERATIONS · C2 — чистая логика без SpreadsheetApp и BigQuery.
 * Проверяется локально: node tools/ops_c2_logic_test.js
 *
 * Здесь НЕТ расчёта поставок: только типы значений, проверка и сверка выборок контракта, разбор маркеров
 * раскладки, машина состояний решения владельца и раскладка уже посчитанных значений по ячейкам.
 */

function OpsError_(code, message, details) {
  var e = new Error(code + ': ' + message);
  e.opsCode = code;
  e.details = details || null;
  return e;
}

function opsCheck_(ok, code, message) {
  if (!ok) throw OpsError_(code, message);
}

function opsN_(x) {
  return (x === null || x === undefined || x === '') ? 0 : Number(x);
}

function opsFirst_(rows) {
  return rows && rows.length ? rows[0] : {};
}

// ───────────────────────────── типы значений BigQuery ─────────────────────────────

function opsTypeValue_(type, v) {
  if (v === null || v === undefined) return null;
  switch (type) {
    case 'INTEGER': case 'INT64': case 'FLOAT': case 'FLOAT64': case 'NUMERIC': case 'BIGNUMERIC':
      return Number(v);
    case 'BOOLEAN': case 'BOOL':
      return v === true || v === 'true';
    case 'TIMESTAMP':
      return Math.round(Number(v) * 1000);   // миллисекунды эпохи
    default:
      return String(v);                      // STRING, DATE ('YYYY-MM-DD'), DATETIME
  }
}

function opsTypeRows_(fields, rawRows) {
  return (rawRows || []).map(function (row) {
    var rec = {};
    for (var i = 0; i < fields.length; i++) {
      var cell = row.f[i];
      rec[fields[i].name] = opsTypeValue_(fields[i].type, cell ? cell.v : null);
    }
    return rec;
  });
}

function opsFieldTypes_(view) {
  var t = {};
  view.fields.forEach(function (f) { t[f.name] = f.type; });
  return t;
}

// ───────────────────────────── форматирование текста ─────────────────────────────

function opsFmtMsk_(ms, pattern) {
  if (ms === null || ms === undefined || ms === '') return '';
  return Utilities.formatDate(new Date(Number(ms)), OPS.TZ, pattern || 'dd.MM.yyyy HH:mm');
}

function opsFmtDate_(iso) {
  if (!iso) return '';
  var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(iso));
  return m ? m[3] + '.' + m[2] + '.' + m[1] : String(iso);
}

function opsFmtNum_(x) {
  if (x === null || x === undefined || x === '') return '';
  var n = Number(x);
  return Math.round(n) === n ? String(n) : String(Math.round(n * 10) / 10);
}

function opsFmtThousands_(x) {
  return String(Math.round(opsN_(x))).replace(/\B(?=(\d{3})+(?!\d))/g, ' ');
}

// ───────────────────────────── проверка выборок (FAIL CLOSED) ─────────────────────────────

function opsAssertUnique_(label, rows, col) {
  var seen = {};
  rows.forEach(function (r) {
    var k = r[col];
    if (k === null || k === undefined || k === '') throw OpsError_('DUP_KEY', label + ': пустой ' + col);
    if (seen[k]) throw OpsError_('DUP_KEY', label + ': повтор ' + col + ' = ' + k);
    seen[k] = true;
  });
}

function opsSum_(rows, pred, col) {
  var s = 0;
  rows.forEach(function (r) { if (pred(r)) s += opsN_(r[col]); });
  return s;
}

/** Проверяет полный набор выборок до первой записи. Возвращает { warnings: [...] }, иначе бросает OpsError_. */
function opsValidatePayload_(payload) {
  var v = payload.views;
  Object.keys(OPS_VIEWS).forEach(function (name) {
    var view = v[name];
    if (!view) throw OpsError_('SCHEMA', 'нет выборки ' + name);
    var types = opsFieldTypes_(view);
    var missing = OPS_VIEWS[name].filter(function (c) { return !(c in types); });
    if (missing.length) throw OpsError_('SCHEMA', name + ': нет колонок ' + missing.join(', '));
    var lim = OPS_ROW_LIMITS[name], n = view.rows.length;
    if (n < lim[0] || n > lim[1]) throw OpsError_('ROWCOUNT', name + ': строк ' + n + ', допустимо ' + lim[0] + '…' + lim[1]);
    opsAssertUnique_(name, view.rows, name === 'V_OPS_SHEET_PLAN' ? 'business_key' : 'row_key');
    if (name !== 'V_OPS_SHEET_PLAN' && name !== 'V_OPS_SHEET_META') opsAssertUnique_(name, view.rows, 'sheet_order');
  });

  var plan = v.V_OPS_SHEET_PLAN.rows;
  var blocks = { WB_SHIP: [], OZON_SHIP: [], HOLD: [] };
  plan.forEach(function (r) {
    opsCheck_(/^[0-9a-f]{16}$/.test(r.rec_fingerprint || ''), 'SCHEMA', 'неверный отпечаток у ' + r.business_key);
    opsCheck_(r.business_key === 'SUPPLY_SHIP|' + r.channel + '|' + r.card_sku, 'SCHEMA', 'ключ не канонический: ' + r.business_key);
    if (r.sheet_block !== null) {
      opsCheck_(r.sheet_block in blocks, 'SCHEMA', 'неизвестный блок ' + r.sheet_block);
      blocks[r.sheet_block].push(r);
    }
    opsCheck_(opsN_(r.rec_final) >= 0 && opsN_(r.rec_physical_units) >= 0, 'RECONCILE', 'отрицательная рекомендация ' + r.business_key);
    opsCheck_(r.ch_arrival_date === r.ch_arrival_date_max && r.ch_next_arrival_date === r.ch_next_arrival_date_max,
      'RECONCILE', 'разные даты прибытия в канале ' + r.channel);
  });
  Object.keys(blocks).forEach(function (b) { opsAssertUnique_('V_OPS_SHEET_PLAN/' + b, blocks[b], 'block_order'); });
  var p0 = plan[0];
  opsCheck_(p0.wb_ship_rows === blocks.WB_SHIP.length && p0.ozon_ship_rows === blocks.OZON_SHIP.length &&
    p0.hold_rows === blocks.HOLD.length, 'RECONCILE', 'счётчики блоков плана не равны числу строк');

  // все выборки — за один день (запросы могут попасть по разные стороны полуночи)
  var days = {};
  Object.keys(OPS_VIEWS).forEach(function (name) {
    v[name].rows.forEach(function (r) { if (r.contract_today) days[r.contract_today] = true; });
  });
  plan.forEach(function (r) { days[r.today] = true; });
  opsCheck_(Object.keys(days).length === 1, 'INCONSISTENT_DAY', Object.keys(days).join(' / '));

  // сверка между представлениями: те же тождества, что в инварианте I24
  var pick = opsFirst_(v.V_OPS_SHEET_PICK.rows), task = opsFirst_(v.V_OPS_SHEET_FF_TASK.rows);
  var bund = opsFirst_(v.V_OPS_SHEET_BUNDLES.rows), bom = opsFirst_(v.V_OPS_SHEET_BOM.rows);
  var ff = opsFirst_(v.V_OPS_SHEET_FF_STOCK.rows);
  var all = function () { return true; };
  var solo = function (ch) { return function (r) { return r.channel === ch && !r.is_bundle; }; };
  var sets = function (ch) { return function (r) { return r.channel === ch && r.is_bundle; }; };
  var pairs = [
    ['R1 физ. единицы плана = спрос снятия', opsSum_(plan, all, 'rec_physical_units'), opsN_(pick.t_total_physical_demand)],
    ['R2 соло WB', opsSum_(plan, solo('WB'), 'rec_final'), opsN_(pick.t_solo_wb_need)],
    ['R2 соло Ozon', opsSum_(plan, solo('OZON'), 'rec_final'), opsN_(pick.t_solo_ozon_need)],
    ['R3 снять под новые', opsN_(task.t_move_pallet_to_shelf_new), opsN_(pick.t_to_pick_from_pallet)],
    ['R3 снять под резерв', opsN_(task.t_move_pallet_to_shelf_reserved), opsN_(pick.t_reserved_on_pallet_to_pick)],
    ['R3 ТЗ = спрос снятия', opsN_(task.t_total_physical_units), opsN_(pick.t_total_physical_demand)],
    ['R4 BOM = сборка', opsN_(bom.t_units_total), opsN_(bund.t_assemble_physical_units)],
    ['R5 наборы WB', opsN_(bund.t_ship_wb), opsSum_(plan, sets('WB'), 'rec_final')],
    ['R5 наборы Ozon', opsN_(bund.t_ship_ozon), opsSum_(plan, sets('OZON'), 'rec_final')],
    ['R6 резервы ФФ', opsN_(ff.t_reserved_wb) + opsN_(ff.t_reserved_ozon), opsN_(task.t_reserved_units_already)]
  ];
  pairs.forEach(function (p) {
    opsCheck_(p[1] === p[2], 'RECONCILE', p[0] + ': ' + p[1] + ' ≠ ' + p[2]);
  });

  var meta = v.V_OPS_SHEET_META.rows[0], warnings = [];
  if (meta.wb_stock_stale) warnings.push('остатки WB');
  if (meta.ozon_stock_stale) warnings.push('остатки Ozon');
  if (meta.ozon_orders_stale) warnings.push('поставки Ozon');
  if (meta.plan_missing) warnings.push('нет активного плана продаж');
  return { warnings: warnings, rowsReceived: Object.keys(OPS_VIEWS).reduce(function (s, n) { return s + v[n].rows.length; }, 0) };
}

/** Строка для хеша снимка: без колонок, которые меняются при каждом запросе. */
function opsHashInput_(payload) {
  var out = {};
  Object.keys(OPS_VIEWS).forEach(function (name) {
    var view = payload.views[name];
    var cols = view.fields.map(function (f) { return f.name; }).filter(function (c) { return OPS_VOLATILE_COLUMNS.indexOf(c) < 0; });
    out[name] = view.rows.map(function (r) { return cols.map(function (c) { return r[c]; }); });
  });
  return JSON.stringify(out);
}

// ───────────────────────────── контракт раскладки: разбор маркеров ─────────────────────────────

function opsParseMarker_(text, row) {
  var m = String(text === null || text === undefined ? '' : text).trim();
  if (m === '') return { row: row, type: '', id: '', key: null };
  if (m === '@END') return { row: row, type: 'END', id: '', key: null };
  var mm = /^@(L|S|V|B|H|R|E|T)(?::([^:]*))?(?::([\s\S]*))?$/.exec(m);
  if (!mm) throw OpsError_('LAYOUT', 'непонятный маркер «' + m + '» в строке ' + row);
  return { row: row, type: mm[1], id: mm[2] || '', key: mm[3] === undefined ? null : mm[3] };
}

function opsMarker_(type, id, key) {
  return '@' + type + (id ? ':' + id : '') + (key !== null && key !== undefined ? ':' + key : '');
}

/**
 * Разбирает маркеры системной колонки листа по спецификации. markers[0] — строка 1.
 * opts.recovery: после прерванной записи строки без маркера сразу за строками блока признаются строками этого блока.
 */
function opsParseLayout_(markers, spec, opts) {
  opts = opts || {};
  var tokens = [];
  for (var i = 0; i < markers.length; i++) {
    var t = opsParseMarker_(markers[i], i + 1);
    if (t.type === 'END') break;
    tokens.push(t);
  }
  if (i >= markers.length) throw OpsError_('LAYOUT', 'нет маркера @END');
  var endRow = i + 1;
  opsCheck_(tokens.length > 0 && tokens[0].type === 'L', 'LAYOUT', 'нет маркера раскладки в строке 1');
  opsCheck_(tokens[0].id === OPS.LAYOUT_VERSION, 'LAYOUT', 'версия раскладки ' + tokens[0].id + ' вместо ' + OPS.LAYOUT_VERSION);

  var pos = 1, model = {};
  function skipStatic() { while (pos < tokens.length && tokens[pos].type === 'S') pos++; }
  spec.blocks.forEach(function (b) {
    skipStatic();
    var blk = { id: b.id, kind: b.kind };
    function expect(type) {
      var t = tokens[pos];
      if (!t || t.type !== type || t.id !== b.id) {
        throw OpsError_('LAYOUT', 'блок ' + b.id + ': ожидался маркер @' + type + ' в строке ' + (t ? t.row : endRow) +
          (t ? ', найден «' + opsMarker_(t.type, t.id, t.key) + '»' : ''));
      }
      pos++;
      return t;
    }
    if (b.kind === 'value') {
      blk.valueRow = expect('V').row;
    } else {
      if (b.band) blk.bandRow = expect('B').row;
      if (b.header) blk.headerRow = expect('H').row;
      var rows = [];
      while (pos < tokens.length) {
        var t = tokens[pos];
        if ((t.type === 'R' || t.type === 'E') && t.id === b.id) { rows.push(t); pos++; continue; }
        if (t.type === '' && opts.recovery && b.kind === 'rows' && rows.length) {
          rows.push({ row: t.row, type: 'R', id: b.id, key: '', adopted: true }); pos++; continue;
        }
        break;
      }
      opsCheck_(rows.length > 0, 'LAYOUT', 'блок ' + b.id + ': нет строк данных');
      var empties = rows.filter(function (r) { return r.type === 'E'; }).length;
      opsCheck_(empties === 0 || rows.length === 1 || opts.recovery, 'LAYOUT', 'блок ' + b.id + ': «Нет позиций» вместе со строками');
      if (b.kind === 'fixed') {
        opsCheck_(rows.length === b.keys.length, 'LAYOUT', 'блок ' + b.id + ': строк ' + rows.length + ' вместо ' + b.keys.length);
        rows.forEach(function (r, k) {
          opsCheck_(r.type === 'R' && r.key === b.keys[k], 'LAYOUT', 'блок ' + b.id + ': строка ' + r.row + ' не ' + b.keys[k]);
        });
      } else {
        var seen = {};
        rows.forEach(function (r) {
          if (!r.key) return;
          opsCheck_(!seen[r.key], 'LAYOUT', 'блок ' + b.id + ': повтор ключа строки ' + r.key);
          seen[r.key] = true;
        });
      }
      blk.rows = rows;
      blk.firstRow = rows[0].row;
      blk.lastRow = rows[rows.length - 1].row;
      blk.empty = rows.length === 1 && rows[0].type === 'E';
      if (b.total) blk.totalRow = expect('T').row;
    }
    model[b.id] = blk;
  });
  skipStatic();
  if (pos !== tokens.length) {
    var extra = tokens[pos];
    throw OpsError_('LAYOUT', 'строка ' + extra.row + ' вне контракта раскладки («' + opsMarker_(extra.type, extra.id, extra.key) + '»)');
  }
  return { blocks: model, endRow: endRow };
}

// ───────────────────────────── решение владельца: машина состояний ─────────────────────────────

function opsIsShipBlock_(block) {
  return block === 'WB_SHIP' || block === 'OZON_SHIP';
}

function opsNewStateRow_(key) {
  var s = {};
  OPS_STATE_COLS.forEach(function (c) { s[c] = ''; });
  s.business_key = key;
  s.approval_status = 'NONE';
  return s;
}

function opsEvent_(at, key, event, qty, fp, reason, source) {
  return { event_at: at, business_key: key, event: event, qty: qty === undefined ? '' : qty, fingerprint: fp || '',
    reason: reason || '', source: source };
}

function opsInvalidate_(s, reason, now) {
  s.previous_approved_qty = s.approved_qty;
  s.previous_approved_fingerprint = s.approved_fingerprint;
  s.previous_approved_at = s.approved_at;
  s.approved_qty = '';
  s.approved_fingerprint = '';
  s.approved_at = '';
  s.invalidated_at = now;
  s.invalidation_reason = reason;
}

/**
 * Сверяет решения владельца с новой выборкой плана. Решение живёт, пока строка в блоке отгрузки
 * и отпечаток рекомендации равен одобренному. Иначе — недействительно навсегда: прежнее значение
 * уходит в previous_*, строка требует нового подтверждения. Старое одобрение само не оживает (T12, T13).
 */
function opsReconcileState_(stateRows, planRows, now) {
  var order = [], byKey = {}, plan = {}, events = [];
  stateRows.forEach(function (r) {
    var c = {};
    OPS_STATE_COLS.forEach(function (k) { c[k] = r[k] === undefined || r[k] === null ? '' : r[k]; });
    byKey[c.business_key] = c;
    order.push(c.business_key);
  });
  planRows.forEach(function (p) {
    plan[p.business_key] = p;
    if (opsIsShipBlock_(p.sheet_block) && !byKey[p.business_key]) {
      byKey[p.business_key] = opsNewStateRow_(p.business_key);
      order.push(p.business_key);
    }
  });
  order.forEach(function (k) {
    var s = byKey[k], p = plan[k] || null, inShip = !!p && opsIsShipBlock_(p.sheet_block);
    s.current_fingerprint = p ? p.rec_fingerprint : '';
    s.current_block = p ? (p.sheet_block || 'NONE') : 'ABSENT';
    s.current_rec_final = p ? opsN_(p.rec_final) : '';
    if (p) s.last_seen_at = now;
    switch (s.approval_status) {
      case 'APPROVED':
        if (inShip && s.approved_fingerprint === p.rec_fingerprint) break;
        var reason = inShip ? 'FINGERPRINT_CHANGED' : (p ? 'LEFT_SHIP_BLOCK' : 'KEY_ABSENT');
        opsInvalidate_(s, reason, now);
        s.approval_status = inShip ? 'REAPPROVAL_REQUIRED' : 'INACTIVE';
        events.push(opsEvent_(now, k, 'INVALIDATED', s.previous_approved_qty, s.previous_approved_fingerprint, reason, 'REFRESH'));
        break;
      case 'REAPPROVAL_REQUIRED':
        if (!inShip) {
          s.approval_status = 'INACTIVE';
          events.push(opsEvent_(now, k, 'INACTIVE', '', s.current_fingerprint, p ? 'LEFT_SHIP_BLOCK' : 'KEY_ABSENT', 'REFRESH'));
        }
        break;
      case 'INACTIVE':
        if (inShip) {
          s.approval_status = 'REAPPROVAL_REQUIRED';
          events.push(opsEvent_(now, k, 'RETURNED', '', p.rec_fingerprint, 'RETURNED_TO_SHIP_BLOCK', 'REFRESH'));
        }
        break;
      default:
        s.approval_status = 'NONE';
    }
  });
  return { rows: order.map(function (k) { return byKey[k]; }), byKey: byKey, events: events };
}

/** Количество из ячейки владельца: целое ≥ 0 или null. */
function opsParseQty_(raw) {
  if (typeof raw === 'number') return (isFinite(raw) && raw >= 0 && Math.floor(raw) === raw) ? raw : null;
  var t = String(raw).replace(/[\s ]/g, '').replace(',', '.');
  if (!/^\d+(\.0+)?$/.test(t)) return null;
  return Number(t);
}

/**
 * Правка «Одобрено владельцем». shownFingerprint — отпечаток из маркера строки, которую владелец видел.
 * Возвращает { state, events, ignored?, invalid? }. state мутируется.
 */
function opsApplyOwnerEdit_(s, raw, shownFingerprint, now) {
  var events = [], key = s.business_key;
  var text = raw === null || raw === undefined ? '' : String(raw).trim();
  if (text.indexOf('ПЕРЕСОГЛ') === 0) return { state: s, events: events, ignored: true };
  if (text === '') {
    if (s.approval_status === 'APPROVED') {
      opsInvalidate_(s, 'CLEARED_BY_OWNER', now);
      s.approval_status = 'NONE';
      events.push(opsEvent_(now, key, 'CLEARED', s.previous_approved_qty, s.previous_approved_fingerprint, 'CLEARED_BY_OWNER', 'ONEDIT'));
    } else if (s.approval_status === 'REAPPROVAL_REQUIRED' || s.approval_status === 'INACTIVE') {
      s.approval_status = 'NONE';
      events.push(opsEvent_(now, key, 'CLEARED', '', shownFingerprint, 'REAPPROVAL_DISMISSED_BY_OWNER', 'ONEDIT'));
    } else {
      return { state: s, events: events, ignored: true };
    }
    return { state: s, events: events };
  }
  var n = opsParseQty_(raw);
  if (n === null) return { state: s, events: events, invalid: true };
  var again = s.approval_status === 'REAPPROVAL_REQUIRED' || s.approval_status === 'INACTIVE';
  s.approved_qty = n;
  s.approved_fingerprint = shownFingerprint;
  s.approved_at = now;
  s.approval_status = 'APPROVED';
  events.push(opsEvent_(now, key, again ? 'REAPPROVED' : 'APPROVED', n, shownFingerprint, '', 'ONEDIT'));
  if (s.current_fingerprint && shownFingerprint !== s.current_fingerprint) {
    // между показом строки и записью решения обновление уже сменило рекомендацию
    opsInvalidate_(s, 'FINGERPRINT_CHANGED', now);
    s.approval_status = 'REAPPROVAL_REQUIRED';
    events.push(opsEvent_(now, key, 'INVALIDATED', n, shownFingerprint, 'FINGERPRINT_CHANGED', 'ONEDIT'));
  }
  return { state: s, events: events };
}

/** Что показать в «Одобрено владельцем». */
function opsOwnerDisplay_(s) {
  if (s && s.approval_status === 'APPROVED') {
    return { value: Number(s.approved_qty), fg: OPS_COLOR.DARK, bold: false, align: 'right', size: 10, note: '' };
  }
  if (s && s.approval_status === 'REAPPROVAL_REQUIRED') {
    return { value: OPS_TEXT.REAPPROVAL_SHORT + s.previous_approved_qty, fg: OPS_COLOR.RED, bold: true, align: 'left', size: 9,
      note: OPS_TEXT.REAPPROVAL_NOTE_PREFIX + s.previous_approved_qty +
        (s.previous_approved_at ? ' (одобрено ' + s.previous_approved_at + ')' : '') +
        '. Рекомендация изменилась или строка уходила из отгрузки после вашего решения. ' +
        'Введите количество заново — это станет решением по текущей рекомендации.' };
  }
  return { value: '', fg: OPS_COLOR.DARK, bold: false, align: 'right', size: 10, note: '' };
}

function opsShipRowKey_(p) {
  return p.business_key + '#' + p.rec_fingerprint;
}

function opsSplitShipRowKey_(key) {
  var i = String(key || '').lastIndexOf('#');
  return i < 0 ? { businessKey: '', fingerprint: '' } : { businessKey: key.slice(0, i), fingerprint: key.slice(i + 1) };
}

// ───────────────────────────── отрисовка: значения по ячейкам ─────────────────────────────

/** Значение ячейки: дата-поле → {date}, остальное как есть. */
function opsCellValue_(type, value) {
  if (value === null || value === undefined) return null;
  if (type === 'DATE') return { date: value };
  return value;
}

function opsRowsFor_(rows, types, cols, keyFn, extra) {
  return rows.map(function (r) {
    var cells = {};
    Object.keys(cols).forEach(function (c) { cells[c] = opsCellValue_(types[cols[c]], r[cols[c]]); });
    var out = { key: keyFn(r), cells: cells };
    if (extra) extra(r, out);
    return out;
  });
}

function opsTotalFor_(first, cols) {
  var cells = { 1: 'ИТОГО' };
  Object.keys(cols).forEach(function (c) { cells[c] = opsN_(first[cols[c]]); });
  return { cells: cells };
}

function opsSortBy_(rows, col) {
  return rows.slice().sort(function (a, b) { return opsN_(a[col]) - opsN_(b[col]); });
}

function opsBoldKeyStyle_(on) {
  return { bg: on ? OPS_COLOR.BLUE_BG : OPS_COLOR.WHITE, fg: on ? OPS_COLOR.BLUE : OPS_COLOR.MUTED, bold: !!on };
}

function opsStatusStyle_(code) {
  var st = OPS_ST[code || ''] || [OPS_COLOR.WHITE, OPS_COLOR.DARK, false];
  return { bg: st[0], fg: st[1], bold: st[2] };
}

function opsCoverText_(meta) {
  return meta.wb_target_cover_days === meta.ozon_target_cover_days ? String(meta.wb_target_cover_days)
    : 'WB ' + meta.wb_target_cover_days + ' / Ozon ' + meta.ozon_target_cover_days;
}

function opsArrivals_(plan) {
  var a = { WB: '', OZON: '' }, n = { WB: '', OZON: '' };
  plan.forEach(function (r) {
    if (r.channel in a && !a[r.channel]) { a[r.channel] = r.ch_arrival_date || ''; n[r.channel] = r.ch_next_arrival_date || ''; }
  });
  return { arrival: a, next: n };
}

/** Хвост строки статуса (всё после времени и статуса) — берётся из снимка, который сейчас на экране. */
function opsStatusLineTail_(payload) {
  var meta = payload.views.V_OPS_SHEET_META.rows[0], arr = opsArrivals_(payload.views.V_OPS_SHEET_PLAN.rows);
  return 'отгрузка с ФФ ' + meta.wb_ship_date + ' → прибытие WB ' + arr.arrival.WB + ' / Ozon ' + arr.arrival.OZON +
    ' · следующая поставка ' + meta.wb_next_ship_date + ' → ' + arr.next.WB + ' / ' + arr.next.OZON +
    ' · покрытие ' + opsCoverText_(meta) + ' дн считается ПОСЛЕ прибытия · подробный расчёт на листе 05_РАСЧЁТ.';
}

/**
 * status: { status: OK|WARNING|ERROR, message, dataAsOfMs, lastSuccessMs, lastCheckMs, tail, nextAuto, freshness }
 * Возвращает { text, fg } для строки 2 листа 01.
 */
function opsStatusLine_(status) {
  var data = status.dataAsOfMs ? opsFmtMsk_(status.dataAsOfMs) : '—';
  if (status.status === 'ERROR') {
    return { text: 'ОШИБКА ОБНОВЛЕНИЯ ' + opsFmtMsk_(status.lastCheckMs) + ': ' + status.message + ' · на экране данные на ' +
      data + ' МСК · ' + status.tail, fg: OPS_COLOR.RED };
  }
  if (status.status === 'WARNING') {
    return { text: 'evetis_ops на ' + data + ' МСК · ВНИМАНИЕ: устарели ' + status.message + ' · ' + status.tail, fg: OPS_COLOR.AMBER };
  }
  return { text: 'evetis_ops на ' + data + ' МСК · проверено ' + opsFmtMsk_(status.lastCheckMs) + ' · ' + status.tail, fg: OPS_COLOR.GREY };
}

function opsFreshnessText_(meta) {
  var mark = function (stale) { return stale ? ' (устарело)' : ''; };
  return 'остатки WB ' + opsFmtDate_(meta.wb_stock_date) + mark(meta.wb_stock_stale) +
    ' · остатки Ozon ' + opsFmtDate_(meta.ozon_stock_date) + mark(meta.ozon_stock_stale) +
    ' · поставки Ozon ' + opsFmtMsk_(meta.ozon_orders_at) + mark(meta.ozon_orders_stale) +
    ' · план ' + (meta.plan_version || 'нет');
}

/** Блок «ОБНОВЛЕНИЕ ДАННЫХ» на 04_SETTINGS. */
function opsRenderStatusRows_(status) {
  var L = OPS_TEXT.STATUS_LABELS, word;
  if (status.status === 'ERROR') word = 'ОШИБКА: ' + status.message + ' · на экране данные на ' + opsFmtMsk_(status.dataAsOfMs) + ' МСК';
  else if (status.status === 'WARNING') word = 'ВНИМАНИЕ: устарели ' + status.message;
  else word = 'OK';
  var st = status.status === 'ERROR' ? { bg: OPS_COLOR.RED_BG, fg: OPS_COLOR.RED, bold: true }
    : status.status === 'WARNING' ? { bg: OPS_COLOR.AMBER_BG, fg: OPS_COLOR.AMBER, bold: true }
      : { bg: OPS_COLOR.GREEN_BG, fg: OPS_COLOR.GREEN, bold: true };
  var vals = {
    LAST_SUCCESS: status.lastSuccessMs ? opsFmtMsk_(status.lastSuccessMs) + ' МСК' : 'ещё не было',
    LAST_CHECK: opsFmtMsk_(status.lastCheckMs) + ' МСК · ' + (status.checkResult || ''),
    STATUS: word,
    FRESHNESS: status.freshness || '',
    NEXT_AUTO: status.nextAuto || ''
  };
  return OPS_STATUS_KEYS.map(function (k) {
    return { key: k, cells: { 1: L[k], 3: vals[k] },
      styles: { 3: k === 'STATUS' ? st : { bg: OPS_COLOR.WHITE, fg: OPS_COLOR.DARK, bold: true } } };
  });
}

function opsUsendRows_(v) {
  var plan = v.V_OPS_SHEET_PLAN.rows, p0 = plan[0], arr = opsArrivals_(plan).arrival;
  var pick = opsFirst_(v.V_OPS_SHEET_PICK.rows), bund = opsFirst_(v.V_OPS_SHEET_BUNDLES.rows);
  var task = opsFirst_(v.V_OPS_SHEET_FF_TASK.rows), L = OPS_TEXT.USEND_LABELS;
  var data = {
    PICK: ['', opsN_(pick.t_pick_units), opsN_(pick.t_to_pick_from_pallet) + ' под новые отгрузки + ' +
      opsN_(pick.t_reserved_on_pallet_to_pick) + ' под уже зарезервированные поставки'],
    BUILD: [opsN_(bund.t_to_assemble_now), opsN_(bund.t_assemble_physical_units),
      'позиции = наборы, физические единицы = флаконы в них; в т.ч. ' + opsN_(bund.t_to_assemble_reserved) + ' наборов под резерв отгрузок'],
    SHIP_WB: [p0.wb_ship_positions, p0.wb_ship_physical, p0.wb_ship_rows + ' позиций · прибытие ' + arr.WB],
    SHIP_OZON: [p0.ozon_ship_positions, p0.ozon_ship_physical, p0.ozon_ship_rows + ' позиций · прибытие ' + arr.OZON],
    RESERVED: ['', opsN_(task.t_reserved_units_already), 'из них ' + opsN_(task.t_reserved_units_on_pallet) +
      ' лежит на паллетах — их тоже нужно снять'],
    FREE_AFTER: ['', opsN_(task.t_free_ff_after_operation), 'после снятия, сборки и отгрузки']
  };
  return OPS_USEND_KEYS.map(function (k) {
    return { key: k, cells: { 1: L[k], 2: data[k][0], 3: data[k][1], 4: data[k][2] } };
  });
}

function opsRulesRows_(meta) {
  var tol = meta.overstock_tolerance_days;
  var lines = [
    '1. Целевое покрытие ' + opsCoverText_(meta) + ' дней — ПОСЛЕ прибытия. Сборка ФФ (' + meta.ff_prep_working_days +
      ' рабочих дня) и срок канала учитываются отдельно и видны в листе.',
    '2. Округление вверх до операционной кратности. Потребность меньше кратности → ЖДАТЬ, если страховой запас доживёт ' +
      'до следующей возможной поставки (цикл ' + meta.cadence_days + ' дней); иначе отгружается один минимум.',
    '3. Ozon при низком спросе: уменьшенная кратность (по SKU — колонка «Низкий спрос» в таблице логистики ниже) — ' +
      'только если потребность меньше обычной кратности и не меньше уменьшенной.',
    '4. ПРОВЕРИТЬ, если после округления покрытие выше: WB ' + meta.wb_overstock_limit_days + ' дней (' + meta.wb_target_cover_days +
      ' + ' + meta.wb_safety_stock_days + ' + ' + tol + '), Ozon ' + meta.ozon_overstock_limit_days + ' дней (' +
      meta.ozon_target_cover_days + ' + ' + meta.ozon_safety_stock_days + ' + ' + tol + '); или если запас не распродаётся ' +
      'минимум за ' + meta.expiry_margin_days + ' дней до срока годности. Количество при этом не уменьшается — решает владелец.',
    '5. Позиция запаса: остаток площадки + подтверждённое входящее + приёмка. Каждая единица считается один раз.',
    '6. Ozon: ON_OZON, IN_ACCEPTANCE и RESERVED_OZON_ON_FF — раздельно. Приёмка никогда не прибавляется к остатку площадки. ' +
      'Если неизвестное принятое количество может изменить решение «отгрузить ↔ ждать» — статус ПРОВЕРИТЬ.',
    '7. ФФ — один общий пул: WB и Ozon не могут претендовать на одну и ту же единицу.'
  ];
  return OPS_RULE_KEYS.map(function (k, i) { return { key: k, cells: { 1: lines[i] } }; });
}

function opsFreshRows_(meta) {
  var L = OPS_TEXT.FRESH_LABELS, mark = function (stale) { return stale ? ' — устарело' : ''; };
  var vals = {
    WB_STOCK: opsFmtDate_(meta.wb_stock_date) + mark(meta.wb_stock_stale),
    OZON_STOCK: opsFmtDate_(meta.ozon_stock_date) + mark(meta.ozon_stock_stale),
    OZON_ORDERS: opsFmtMsk_(meta.ozon_orders_at) + mark(meta.ozon_orders_stale),
    PLAN_VERSION: meta.plan_version || 'нет активного плана',
    LEDGER_LAST: opsFmtMsk_(meta.ledger_last_at),
    WRITEBACK: meta.writeback_enabled
  };
  return OPS_FRESH_KEYS.map(function (k) { return { key: k, cells: { 1: L[k], 3: vals[k] } }; });
}

/**
 * Полная отрисовка всех листов из проверенного снимка и состояния решений.
 * Возвращает { '<лист>': { '<блок>': { band?, value?, rows?, total? } } }.
 */
function opsRender_(payload, stateByKey, status) {
  var v = payload.views, C = OPS_COLS;
  var plan = v.V_OPS_SHEET_PLAN.rows, meta = v.V_OPS_SHEET_META.rows[0], p0 = plan[0];
  var tPlan = opsFieldTypes_(v.V_OPS_SHEET_PLAN);
  var byBlock = function (b) { return opsSortBy_(plan.filter(function (r) { return r.sheet_block === b; }), 'block_order'); };
  var byChannel = function (ch) { return opsSortBy_(plan.filter(function (r) { return r.channel === ch; }), 'calc_order'); };
  var rowsOf = function (name, pred) {
    var rows = v[name].rows.filter(pred || function () { return true; });
    return opsSortBy_(rows, 'sheet_order');
  };
  var keyRow = function (r) { return r.row_key; };
  var keyBiz = function (r) { return r.business_key; };
  var line = opsStatusLine_(status);
  var shipExtra = function (r, out) {
    out.key = opsShipRowKey_(r);
    out.owner = opsOwnerDisplay_(stateByKey[r.business_key]);
    out.styles = { 2: opsBoldKeyStyle_(opsN_(r.rec_final) > 0), 7: opsStatusStyle_(r.risk_label) };
  };
  var calcExtra = function (r, out) {
    out.rowFg = r.status_code === 'NO PLAN' ? OPS_COLOR.MUTED : OPS_COLOR.DARK;
    out.styles = { 19: opsBoldKeyStyle_(opsN_(r.rec_final) > 0), 23: opsStatusStyle_(r.status_code) };
  };
  var arr = opsArrivals_(plan).arrival;
  var calcBand = function (ch, name, prefix) {
    var rows = byChannel(ch), f = opsFirst_(rows);
    return name + '   ·   отгрузка ' + meta[prefix + 'ship_date'] + ' → прибытие ' + arr[ch] +
      '   ·   покрытие ' + meta[prefix + 'target_cover_days'] + ' дн + страховой ' + meta[prefix + 'safety_stock_days'] +
      ' дн   ·   срок канала ' + meta[prefix + 'lead_time_days'] + ' дн   ·   к отгрузке ' + opsN_(f.ch_rec_final) +
      ' позиций / ' + opsN_(f.ch_rec_physical_units) + ' физ. ед.';
  };
  var ship = v.V_OPS_SHEET_SHIPMENTS, ship0 = opsFirst_(ship.rows);
  var ff0 = opsFirst_(v.V_OPS_SHEET_FF_STOCK.rows);
  var R = {};

  R[OPS_SHEET.PLAN] = {
    STATUS_LINE: { value: line },
    USEND_TOP: { rows: opsUsendRows_(v) },
    WB_SHIP: {
      band: { text: 'ОТГРУЗИТЬ WB СЕЙЧАС   ·   ' + p0.wb_ship_positions + ' позиций / ' + p0.wb_ship_physical + ' физ. единиц',
        note: 'Лимит одной поставки WB (' + meta.wb_shipping_method + '): ' + opsFmtNum_(meta.wb_max_lot_weight_kg) + ' кг / ' +
          opsFmtNum_(meta.wb_max_lot_units) + ' ед. / ' + opsFmtNum_(meta.wb_max_lot_volume_l) + ' л.' },
      rows: opsRowsFor_(byBlock('WB_SHIP'), tPlan, C.SHIP_TOP, keyBiz, shipExtra)
    },
    OZON_SHIP: {
      band: { text: 'ОТГРУЗИТЬ OZON СЕЙЧАС   ·   ' + p0.ozon_ship_positions + ' позиций / ' + p0.ozon_ship_physical + ' физ. единиц' },
      rows: opsRowsFor_(byBlock('OZON_SHIP'), tPlan, C.SHIP_TOP, keyBiz, shipExtra)
    },
    HOLD: {
      band: { text: 'ПРОВЕРИТЬ / НЕ ОТГРУЖАТЬ   ·   ' + p0.hold_rows + ' строк' },
      rows: opsRowsFor_(byBlock('HOLD'), tPlan, C.HOLD, keyBiz)
    },
    USEND_TASK: { rows: opsUsendRows_(v) },
    TASK_MOVE: {
      rows: opsRowsFor_(rowsOf('V_OPS_SHEET_FF_TASK', function (r) { return r.is_move_row; }),
        opsFieldTypes_(v.V_OPS_SHEET_FF_TASK), C.TASK_MOVE, keyRow),
      total: opsTotalFor_(opsFirst_(v.V_OPS_SHEET_FF_TASK.rows), C.TASK_MOVE_T)
    },
    TASK_BUILD: {
      rows: opsRowsFor_(rowsOf('V_OPS_SHEET_BUNDLES', function (r) { return r.is_build_row; }),
        opsFieldTypes_(v.V_OPS_SHEET_BUNDLES), C.TASK_BUILD, keyRow),
      total: opsTotalFor_(opsFirst_(v.V_OPS_SHEET_BUNDLES.rows), C.TASK_BUILD_T)
    },
    TASK_COMP: {
      rows: opsRowsFor_(rowsOf('V_OPS_SHEET_FF_TASK', function (r) { return r.is_comp_row; }),
        opsFieldTypes_(v.V_OPS_SHEET_FF_TASK), C.TASK_COMP, keyRow),
      total: opsTotalFor_(opsFirst_(v.V_OPS_SHEET_FF_TASK.rows), C.TASK_COMP_T)
    }
  };

  R[OPS_SHEET.SHIP] = {
    SHIP_SUBTITLE: { value: { text: 'Данные на ' + opsFmtMsk_(status.dataAsOfMs) + ' (МСК). Один заказ — один этап: резерв на ФФ, ' +
      'приёмка, в пути или принято. Единица не может быть одновременно в двух этапах.' } },
    SHIPMENTS: {
      band: { text: 'ПОСТАВКИ   ·   OZON ' + opsN_(ship0.ozon_rows) + '   ·   WB ' + opsN_(ship0.wb_rows) },
      rows: opsRowsFor_(rowsOf('V_OPS_SHEET_SHIPMENTS'), opsFieldTypes_(ship), C.SHIPMENTS, keyRow),
      total: opsTotalFor_(ship0, C.SHIPMENTS_T)
    },
    SHIP_WB_LINE: { value: { text: opsN_(ship0.wb_rows) === 0
      ? 'Поставок WB в журнале evetis_ops нет; черновики в кабинете поставками не считаются.'
      : 'Поставок WB в журнале evetis_ops: ' + opsN_(ship0.wb_rows) + '.' } }
  };
  R[OPS_SHEET.SHIP].SHIPMENTS.total.cells[1] = 'ИТОГО';

  R[OPS_SHEET.FF] = {
    FF_SUBTITLE: { value: { text: 'Источник правды — журнал evetis_ops на ' + opsFmtMsk_(status.dataAsOfMs) +
      ' (МСК). Старое число Control Tower как физическую правду не использовать.' } },
    FF_TOTAL_LINE: { value: { text: 'ИТОГО НА ФФ: ' + opsFmtThousands_(ff0.t_total_physical) + ' единиц' } },
    FF_STOCK: {
      rows: opsRowsFor_(rowsOf('V_OPS_SHEET_FF_STOCK'), opsFieldTypes_(v.V_OPS_SHEET_FF_STOCK), C.FF_STOCK, keyRow),
      total: opsTotalFor_(ff0, C.FF_STOCK_T)
    }
  };

  R[OPS_SHEET.SETTINGS] = {
    REFRESH_STATUS: { rows: opsRenderStatusRows_(status) },
    RULES: { rows: opsRulesRows_(meta) },
    CHANNELS: { rows: opsRowsFor_(rowsOf('V_OPS_SHEET_CHANNELS'), opsFieldTypes_(v.V_OPS_SHEET_CHANNELS), C.CHANNELS, keyRow) },
    CONFIG: { rows: opsRowsFor_(rowsOf('V_OPS_SHEET_CONFIG'), opsFieldTypes_(v.V_OPS_SHEET_CONFIG), C.CONFIG, keyRow) },
    LOGISTICS: { rows: opsRowsFor_(rowsOf('V_OPS_SHEET_LOGISTICS'), opsFieldTypes_(v.V_OPS_SHEET_LOGISTICS), C.LOGISTICS, keyRow) },
    FRESHNESS: { rows: opsFreshRows_(meta) }
  };

  var calcTotal = function (ch) { return opsTotalFor_(opsFirst_(byChannel(ch)), C.CALC_PLAN_T); };
  R[OPS_SHEET.CALC] = {
    CALC_WB: { band: { text: calcBand('WB', 'WILDBERRIES', 'wb_') },
      rows: opsRowsFor_(byChannel('WB'), tPlan, C.CALC_PLAN, keyBiz, calcExtra), total: calcTotal('WB') },
    CALC_OZON: { band: { text: calcBand('OZON', 'OZON', 'ozon_') },
      rows: opsRowsFor_(byChannel('OZON'), tPlan, C.CALC_PLAN, keyBiz, calcExtra), total: calcTotal('OZON') },
    CALC_BUNDLES: {
      rows: opsRowsFor_(rowsOf('V_OPS_SHEET_BUNDLES'), opsFieldTypes_(v.V_OPS_SHEET_BUNDLES), C.CALC_BUNDLES, keyRow,
        function (r, out) { out.styles = { 13: opsBoldKeyStyle_(opsN_(r.to_assemble_now) > 0) }; }),
      total: opsTotalFor_(opsFirst_(v.V_OPS_SHEET_BUNDLES.rows), C.CALC_BUNDLES_T)
    },
    CALC_BOM: { rows: opsRowsFor_(rowsOf('V_OPS_SHEET_BOM'), opsFieldTypes_(v.V_OPS_SHEET_BOM), C.CALC_BOM, keyRow),
      total: opsTotalFor_(opsFirst_(v.V_OPS_SHEET_BOM.rows), C.CALC_BOM_T) },
    CALC_PICK: { rows: opsRowsFor_(rowsOf('V_OPS_SHEET_PICK'), opsFieldTypes_(v.V_OPS_SHEET_PICK), C.CALC_PICK, keyRow),
      total: opsTotalFor_(opsFirst_(v.V_OPS_SHEET_PICK.rows), C.CALC_PICK_T) }
  };

  R[OPS_SHEET.DATA] = opsRenderData_(payload);
  return R;
}

/** DATA: сырые строки контракта, как пришли из BigQuery (TIMESTAMP — МСК, BOOL — True/False). */
function opsRenderData_(payload) {
  var out = {};
  Object.keys(OPS_VIEWS).forEach(function (name) {
    var view = payload.views[name], fields = view.fields;
    var keyCol = name === 'V_OPS_SHEET_PLAN' ? 'business_key' : 'row_key';
    var rows = view.rows.slice().sort(function (a, b) { return String(a[keyCol]) < String(b[keyCol]) ? -1 : 1; });
    out['D_' + name] = {
      band: { text: name },
      header: fields.map(function (f) { return f.name; }),
      rows: rows.map(function (r) {
        var cells = {};
        fields.forEach(function (f, i) {
          var x = r[f.name];
          if (x !== null && f.type === 'TIMESTAMP') x = opsFmtMsk_(x, 'yyyy-MM-dd HH:mm');
          else if (x !== null && (f.type === 'BOOLEAN' || f.type === 'BOOL')) x = x ? 'True' : 'False';
          cells[i + 2] = x;   // колонка A — служебная, данные с B
        });
        return { key: String(r[keyCol]), cells: cells };
      })
    };
  });
  return out;
}

// ═══════════ OpsSheetIO.gs ═══════════
/**
 * EVETIS OPERATIONS · C2 — чтение и запись листа.
 *
 * Пишет только в сгенерированные ячейки блоков, найденных по маркерам системной колонки. Ширины колонок,
 * закрепление, заметки и объединения шапок, скрытость листов и оформление колонки владельца не меняются.
 * Число строк меняется только внутри блоков kind='rows': новые строки берут формат и высоту первой строки блока.
 */

/** Колонки с оформлением, зависящим от данных (цвет статуса, выделение рекомендации). */
var OPS_STYLE_COLS = { WB_SHIP: [2, 7], OZON_SHIP: [2, 7], CALC_WB: [19, 23], CALC_OZON: [19, 23], CALC_BUNDLES: [13],
  REFRESH_STATUS: [3] };

var OPS_WRITE_ORDER = ['01_SUPPLY_PLAN', '02_SHIPMENTS', '03_FF_STOCK', '04_SETTINGS', '05_РАСЧЁТ', 'DATA'];

function opsSheet_(ss, name) {
  var sh = ss.getSheetByName(name);
  if (!sh) throw OpsError_('LAYOUT', 'нет листа ' + name);
  return sh;
}

function opsSpecFor_(name) {
  return name === OPS_SHEET.DATA ? opsDataLayout_() : OPS_LAYOUT[name];
}

function opsBlockSpec_(spec, id) {
  for (var i = 0; i < spec.blocks.length; i++) if (spec.blocks[i].id === id) return spec.blocks[i];
  throw OpsError_('INTERNAL', 'нет блока ' + id);
}

function opsNowText_() {
  return Utilities.formatDate(new Date(), OPS.TZ, 'yyyy-MM-dd HH:mm:ss');
}

function opsReadMarkers_(sheet, sysCol) {
  var last = sheet.getLastRow();
  if (last < 1 || sheet.getMaxColumns() < sysCol) return [];
  return sheet.getRange(1, sysCol, last, 1).getValues().map(function (r) { return r[0]; });
}

function opsParseSheetLayout_(sheet, spec, opts) {
  return opsParseLayout_(opsReadMarkers_(sheet, spec.sysCol), spec, opts);
}

/** Контракт раскладки всех листов — до первой записи. */
function opsValidateLayouts_(ss, opts) {
  var out = {};
  OPS_WRITE_ORDER.forEach(function (name) {
    out[name] = opsParseSheetLayout_(opsSheet_(ss, name), opsSpecFor_(name), opts);
  });
  return out;
}

/** Значение для setValues: текст, похожий на число/дату/формулу/логическое, остаётся текстом. */
function opsSheetValue_(x, tz) {
  if (x === null || x === undefined) return '';
  // у книги, импортированной из .xlsx, часовой пояс может быть пустым — тогда часовой пояс скрипта
  if (typeof x === 'object' && x.date) return Utilities.parseDate(String(x.date).slice(0, 10), tz || OPS.TZ, 'yyyy-MM-dd');
  if (typeof x === 'string') {
    if (x === '') return '';
    if (/^[=+\-@']/.test(x) || /^[\s\d.,:\/%()eE+\-]+$/.test(x) || /^(true|false|истина|ложь)$/i.test(x)) return "'" + x;
  }
  return x;
}

function opsEnsureColumns_(sheet, n) {
  var have = sheet.getMaxColumns();
  if (have < n) sheet.insertColumnsAfter(have, n - have);
}

function opsDataLastCol_(render) {
  var w = 2;
  Object.keys(render).forEach(function (id) { if (render[id].header) w = Math.max(w, render[id].header.length + 1); });
  return w;
}

/** Меняет число строк блока до need; сразу пишет маркеры строк, чтобы контракт оставался целым. */
function opsResizeBlock_(sheet, spec, b, blk, need, fmtCol) {
  var have = blk.lastRow - blk.firstRow + 1;
  if (need > have) {
    var add = need - have, start = blk.lastRow + 1;
    sheet.insertRowsAfter(blk.lastRow, add);
    sheet.getRange(blk.firstRow, 1, 1, fmtCol)
      .copyTo(sheet.getRange(start, 1, add, fmtCol), SpreadsheetApp.CopyPasteType.PASTE_FORMAT, false);
    sheet.setRowHeights(start, add, sheet.getRowHeight(blk.firstRow));
    (b.merges || []).forEach(function (m) {
      for (var r = start; r < start + add; r++) {
        var rg = sheet.getRange(r, m[0], 1, m[1] - m[0] + 1);
        if (!rg.isPartOfMerge()) rg.merge();
      }
    });
  } else if (need < have) {
    sheet.deleteRows(blk.firstRow + need, have - need);
  }
  var marks = [];
  for (var i = 0; i < need; i++) marks.push([opsMarker_('R', b.id, '')]);
  sheet.getRange(blk.firstRow, spec.sysCol, need, 1).setValues(marks);
}

/** Пишет один лист по отрисовке. Возвращает число записанных строк. */
function opsWriteSheet_(ss, name, render, opts) {
  var sheet = opsSheet_(ss, name), spec = opsSpecFor_(name);
  var lastCol = spec.dynamicGrid ? opsDataLastCol_(render) : spec.grid;
  if (spec.dynamicGrid) opsEnsureColumns_(sheet, lastCol);
  var fmtCol = spec.dynamicGrid ? lastCol : Math.max(spec.grid, spec.sysCol);
  var model = opsParseSheetLayout_(sheet, spec, { recovery: !!(opts && opts.recovery) });

  var rowBlocks = spec.blocks.filter(function (b) { return b.kind === 'rows'; });
  rowBlocks.sort(function (a, b) { return model.blocks[b.id].firstRow - model.blocks[a.id].firstRow; });   // снизу вверх
  rowBlocks.forEach(function (b) {
    var r = render[b.id];
    if (!r) throw OpsError_('INTERNAL', 'нет отрисовки ' + name + '/' + b.id);
    opsResizeBlock_(sheet, spec, b, model.blocks[b.id], Math.max(1, (r.rows || []).length), fmtCol);
  });
  SpreadsheetApp.flush();
  model = opsParseSheetLayout_(sheet, spec, {});

  var written = 0;
  spec.blocks.forEach(function (b) {
    var segments = spec.dynamicGrid ? [[2, lastCol]] : b.segments;
    written += opsWriteBlock_(sheet, spec, b, model.blocks[b.id], render[b.id], segments);
    if (typeof opsTestHook_ === 'function') opsTestHook_('afterBlock', { sheet: name, block: b.id });
  });
  return written;
}

function opsWriteBlock_(sheet, spec, b, blk, r, segments) {
  if (!r) throw OpsError_('INTERNAL', 'нет отрисовки блока ' + b.id);
  var tz = sheet.getParent().getSpreadsheetTimeZone();
  var textCol = spec.sysCol === 1 ? 2 : 1;

  if (b.kind === 'value') {
    var vc = sheet.getRange(blk.valueRow, textCol);
    vc.setValue(opsSheetValue_(r.value.text, tz));
    if (r.value.fg) vc.setFontColor(r.value.fg);
    return 1;
  }
  if (b.band && r.band) {
    var bc = sheet.getRange(blk.bandRow, textCol);
    bc.setValue(opsSheetValue_(r.band.text, tz));
    if (r.band.note !== undefined) bc.setNote(r.band.note);
  }
  if (r.header) {
    var hdr = [];
    for (var hc = segments[0][0]; hc <= segments[0][1]; hc++) hdr.push(r.header[hc - 2] === undefined ? '' : r.header[hc - 2]);
    sheet.getRange(blk.headerRow, segments[0][0], 1, hdr.length).setValues([hdr])
      .setFontWeight('bold').setFontColor(OPS_COLOR.WHITE).setBackground(OPS_COLOR.DARK);
  }

  var rows = r.rows || [], n = Math.max(1, rows.length), first = blk.firstRow;
  opsCheck_(blk.lastRow - blk.firstRow + 1 === n, 'INTERNAL', b.id + ': строк в листе ' + (blk.lastRow - blk.firstRow + 1) + ' вместо ' + n);
  if (b.kind === 'fixed') {
    opsCheck_(rows.length === b.keys.length && rows.every(function (x, i) { return x.key === b.keys[i]; }),
      'INTERNAL', b.id + ': отрисовка не совпадает с ключами блока');
  }
  var marks = rows.length ? rows.map(function (x) { return [opsMarker_('R', b.id, x.key)]; }) : [[opsMarker_('E', b.id, null)]];
  sheet.getRange(first, spec.sysCol, n, 1).setValues(marks);

  segments.forEach(function (seg, si) {
    var m = [];
    for (var i = 0; i < n; i++) {
      var line = [];
      for (var c = seg[0]; c <= seg[1]; c++) {
        line.push(rows.length ? opsSheetValue_(rows[i].cells[c], tz) : (si === 0 && c === seg[0] ? OPS_TEXT.EMPTY_ROW : ''));
      }
      m.push(line);
    }
    sheet.getRange(first, seg[0], n, seg[1] - seg[0] + 1).setValues(m);
  });

  if (b.ownerCol) {
    var ov = [], of = [], ow = [], oa = [], on = [], os = [];
    for (var k = 0; k < n; k++) {
      var o = rows.length && rows[k].owner ? rows[k].owner
        : { value: '', fg: OPS_COLOR.DARK, bold: false, align: 'right', size: 10, note: '' };
      ov.push([o.value]); of.push([o.fg]); ow.push([o.bold ? 'bold' : 'normal']);
      oa.push([o.align || 'right']); on.push([o.note || '']); os.push([o.size || 10]);
    }
    var orng = sheet.getRange(first, b.ownerCol, n, 1);
    orng.setValues(ov);
    orng.setFontColors(of);
    orng.setFontWeights(ow);
    orng.setHorizontalAlignments(oa);
    orng.setFontSizes(os);
    orng.setNotes(on);
  }

  opsApplyStyles_(sheet, b, first, rows, segments);

  if (b.total) {
    var tot = r.total || { cells: { 1: 'ИТОГО' } };
    segments.forEach(function (seg) {
      var line = [];
      for (var c = seg[0]; c <= seg[1]; c++) line.push(opsSheetValue_(tot.cells[c], tz));
      sheet.getRange(blk.totalRow, seg[0], 1, seg[1] - seg[0] + 1).setValues([line]);
    });
  }
  return n + (b.total ? 1 : 0);
}

function opsApplyStyles_(sheet, b, first, rows, segments) {
  var cols = OPS_STYLE_COLS[b.id];
  if (!cols) return;
  var n = Math.max(1, rows.length), lastC = segments[segments.length - 1][1];
  var rowFg = rows.some(function (x) { return x.rowFg; });
  if (rowFg) {
    sheet.getRange(first, 1, n, lastC).setFontColors(rows.map(function (x) {
      var line = [];
      for (var c = 1; c <= lastC; c++) line.push((x.styles && x.styles[c] && x.styles[c].fg) || x.rowFg || OPS_COLOR.DARK);
      return line;
    }));
  }
  cols.forEach(function (c) {
    var bg = [], fg = [], wt = [];
    for (var i = 0; i < n; i++) {
      var s = rows.length && rows[i].styles && rows[i].styles[c] ? rows[i].styles[c]
        : { bg: OPS_COLOR.WHITE, fg: OPS_COLOR.DARK, bold: false };
      bg.push([s.bg]); fg.push([s.fg]); wt.push([s.bold ? 'bold' : 'normal']);
    }
    var rg = sheet.getRange(first, c, n, 1);
    rg.setBackgrounds(bg);
    if (!rowFg) rg.setFontColors(fg);
    rg.setFontWeights(wt);
  });
}

/** Только статус (строка 2 листа 01 и блок «ОБНОВЛЕНИЕ ДАННЫХ» на 04) — при ошибке данные на экране не трогаются. */
function opsWriteStatusOnly_(ss, status) {
  var specP = OPS_LAYOUT[OPS_SHEET.PLAN], shP = opsSheet_(ss, OPS_SHEET.PLAN);
  var mp = opsParseSheetLayout_(shP, specP, { recovery: true });
  opsWriteBlock_(shP, specP, opsBlockSpec_(specP, 'STATUS_LINE'), mp.blocks.STATUS_LINE, { value: opsStatusLine_(status) }, null);
  var specS = OPS_LAYOUT[OPS_SHEET.SETTINGS], shS = opsSheet_(ss, OPS_SHEET.SETTINGS);
  var ms = opsParseSheetLayout_(shS, specS, { recovery: true }), b = opsBlockSpec_(specS, 'REFRESH_STATUS');
  opsWriteBlock_(shS, specS, b, ms.blocks.REFRESH_STATUS, { rows: opsRenderStatusRows_(status) }, b.segments);
}

// ───────────────────────────── _OPS_STATE ─────────────────────────────

function opsStateSheet_(ss) {
  var sh = ss.getSheetByName(OPS_SHEET.STATE);
  if (!sh) throw OpsError_('STATE_SHEET', 'нет листа ' + OPS_SHEET.STATE);
  return sh;
}

function opsReadState_(ss) {
  var sh = opsStateSheet_(ss);
  var head = sh.getRange(1, 1, 1, OPS_STATE_COLS.length).getValues()[0];
  opsCheck_(head.join('|') === OPS_STATE_COLS.join('|'), 'STATE_SHEET', 'заголовок таблицы состояния изменён');
  var evHead = sh.getRange(1, OPS_EVENT_COL0, 1, OPS_EVENT_COLS.length).getValues()[0];
  opsCheck_(evHead.join('|') === OPS_EVENT_COLS.join('|'), 'STATE_SHEET', 'заголовок журнала решений изменён');
  var last = sh.getLastRow(), rows = [], eventCount = 0, seen = {};
  if (last >= 2) {
    sh.getRange(2, 1, last - 1, OPS_STATE_COLS.length).getValues().forEach(function (v) {
      if (v[0] === '') return;
      var s = {};
      OPS_STATE_COLS.forEach(function (c, i) { s[c] = v[i]; });
      opsCheck_(!seen[s.business_key], 'STATE_SHEET', 'повтор ключа в _OPS_STATE: ' + s.business_key);
      seen[s.business_key] = true;
      rows.push(s);
    });
    var ev = sh.getRange(2, OPS_EVENT_COL0, last - 1, 1).getValues();
    while (eventCount < ev.length && ev[eventCount][0] !== '') eventCount++;
  }
  return { rows: rows, eventCount: eventCount };
}

/** Перезаписывает таблицу состояния (ключи только добавляются) и дописывает события в журнал. */
function opsWriteState_(ss, rows, events, eventCount) {
  var sh = opsStateSheet_(ss), tz = ss.getSpreadsheetTimeZone();
  if (rows.length) {
    sh.getRange(2, 1, rows.length, OPS_STATE_COLS.length).setValues(rows.map(function (s) {
      return OPS_STATE_COLS.map(function (c) { return opsSheetValue_(s[c], tz); });
    }));
  }
  if (events && events.length) {
    if (eventCount === undefined) eventCount = opsReadState_(ss).eventCount;
    sh.getRange(2 + eventCount, OPS_EVENT_COL0, events.length, OPS_EVENT_COLS.length).setValues(events.map(function (e) {
      return OPS_EVENT_COLS.map(function (c) { return opsSheetValue_(e[c], tz); });
    }));
  }
}

// ═══════════ OpsInstall.gs ═══════════
/**
 * EVETIS OPERATIONS · C2 — одноразовая установка контракта раскладки поверх C1.2 (opsSetup).
 *
 * Только здесь блоки ищутся по видимым текстам C1.2 — один раз, со строгой сверкой всей раскладки.
 * Если лист отличается от C1.2 хоть одной строкой — установка не начинается (ни одной записи).
 * Дальше обновление работает только по маркерам.
 *
 * Что меняет установка (согласовано владельцем, C2 P2/P3/P4/P5):
 *   — маркеры в скрытой защищённой (с предупреждением) системной колонке листов 01–05;
 *   — 04_SETTINGS: блок «ОБНОВЛЕНИЕ ДАННЫХ» (7 строк после строки 3);
 *   — заметки и заголовок со снимочными утверждениями C1.2 заменяются нейтральными (старые — в отчёт установки);
 *   — DATA (скрытый сырой слой): один раз очищается и размечается под V_OPS_SHEET_*, остаётся скрытым;
 *   — _OPS_STATE: новый скрытый защищённый лист состояния решений владельца.
 */

var OPS_C12_STEPS = {
  '01_SUPPLY_PLAN': [
    ['L', /^EVETIS OPERATIONS · План поставок$/],
    ['V', /^ТОЛЬКО ЧТЕНИЕ · /, 'STATUS_LINE'],
    ['S', /^ИТОГО СЕГОДНЯ · ГОТОВОЕ ЗАДАНИЕ USEND$/],
    ['H', /^Что сделать$/, 'USEND_TOP'],
    ['F', 'USEND_TOP', OPS_USEND_KEYS, 'USEND'],
    ['B', /^ОТГРУЗИТЬ WB СЕЙЧАС/, 'WB_SHIP'],
    ['H', /^Товар$/, 'WB_SHIP'],
    ['RS', 'WB_SHIP', /^ОТГРУЗИТЬ OZON СЕЙЧАС/],
    ['B', /^ОТГРУЗИТЬ OZON СЕЙЧАС/, 'OZON_SHIP'],
    ['H', /^Товар$/, 'OZON_SHIP'],
    ['RS', 'OZON_SHIP', /^ПРОВЕРИТЬ \/ НЕ ОТГРУЖАТЬ/],
    ['B', /^ПРОВЕРИТЬ \/ НЕ ОТГРУЖАТЬ/, 'HOLD'],
    ['H', /^Товар$/, 'HOLD'],
    ['RS', 'HOLD', /^ТЗ ДЛЯ ФУЛФИЛМЕНТА/],
    ['S', /^ТЗ ДЛЯ ФУЛФИЛМЕНТА/],
    ['S', /^Считается из плана выше/],
    ['S', /^ГОТОВОЕ ЗАДАНИЕ USEND$/],
    ['H', /^Что сделать$/, 'USEND_TASK'],
    ['F', 'USEND_TASK', OPS_USEND_KEYS, 'USEND'],
    ['S', /^1\. Снять с паллет на полку$/],
    ['H', /^Товар$/, 'TASK_MOVE'],
    ['RS', 'TASK_MOVE', /^ИТОГО$/],
    ['T', /^ИТОГО$/, 'TASK_MOVE'],
    ['S', /^$/],
    ['S', /^2\. Собрать наборы$/],
    ['H', /^Набор$/, 'TASK_BUILD'],
    ['RS', 'TASK_BUILD', /^ИТОГО$/],
    ['T', /^ИТОГО$/, 'TASK_BUILD'],
    ['S', /^$/],
    ['S', /^3\. Расход компонентов/],
    ['H', /^Товар$/, 'TASK_COMP'],
    ['RS', 'TASK_COMP', /^ИТОГО$/],
    ['T', /^ИТОГО$/, 'TASK_COMP'],
    ['END']
  ],
  '02_SHIPMENTS': [
    ['L', /^Отгрузки и поставки$/],
    ['V', /^Данные на /, 'SHIP_SUBTITLE'],
    ['S', /^$/],
    ['B', /^OZON · поставки$/, 'SHIPMENTS'],
    ['H', /^Канал$/, 'SHIPMENTS'],
    ['RS', 'SHIPMENTS', /^ИТОГО$/],
    ['T', /^ИТОГО$/, 'SHIPMENTS'],
    ['S', /^$/],
    ['V', /^Поставок WB/, 'SHIP_WB_LINE'],
    ['END']
  ],
  '03_FF_STOCK': [
    ['L', /^Запас на фулфилменте \(физические единицы\)$/],
    ['V', /^Источник правды — журнал evetis_ops/, 'FF_SUBTITLE'],
    ['V', /^ИТОГО НА ФФ: /, 'FF_TOTAL_LINE'],
    ['S', /^SHIPPED UNCONFIRMED/],
    ['S', /^$/],
    ['S', /^СОСТОЯНИЕ ПО SKU$/],
    ['H', /^Товар$/, 'FF_STOCK'],
    ['RS', 'FF_STOCK', /^ИТОГО$/],
    ['T', /^ИТОГО$/, 'FF_STOCK'],
    ['END']
  ],
  '04_SETTINGS': [
    ['L', /^Настройки и правила/],
    ['S', /^Значения живут в BigQuery/],
    ['S', /^$/],
    ['S', /^ПРАВИЛА РАСЧЁТА/],
    ['F', 'RULES', OPS_RULE_KEYS, 'RULES'],
    ['S', /^$/],
    ['S', /^КАНАЛ И СПОСОБ ОТГРУЗКИ$/],
    ['H', /^Канал$/, 'CHANNELS'],
    ['RS', 'CHANNELS', /^$/],
    ['S', /^$/],
    ['S', /^ПАРАМЕТРЫ C1$/],
    ['H', /^Ключ$/, 'CONFIG'],
    ['RS', 'CONFIG', /^$/],
    ['S', /^$/],
    ['S', /^ЛОГИСТИКА ПО SKU × КАНАЛ$/],
    ['S', /^Заводской короб/],
    ['H', /^SKU$/, 'LOGISTICS'],
    ['RS', 'LOGISTICS', /^$/],
    ['S', /^$/],
    ['S', /^СВЕЖЕСТЬ ДАННЫХ$/],
    ['F', 'FRESHNESS', OPS_FRESH_KEYS, 'FRESH'],
    ['END']
  ],
  '05_РАСЧЁТ': [
    ['L', /^Подробный расчёт \(для проверки\)$/],
    ['S', /^Те же строки/],
    ['S', /^$/],
    ['S', /^ПОДРОБНЫЙ РАСЧЁТ \(для проверки\)$/],
    ['S', /^Полная таблица/],
    ['B', /^WILDBERRIES   ·   /, 'CALC_WB'],
    ['H', /^Товар$/, 'CALC_WB'],
    ['RS', 'CALC_WB', /^ИТОГО$/],
    ['T', /^ИТОГО$/, 'CALC_WB'],
    ['S', /^$/],
    ['B', /^OZON   ·   /, 'CALC_OZON'],
    ['H', /^Товар$/, 'CALC_OZON'],
    ['RS', 'CALC_OZON', /^ИТОГО$/],
    ['T', /^ИТОГО$/, 'CALC_OZON'],
    ['S', /^$/],
    ['S', /^BUNDLE PRODUCTION · сборка наборов$/],
    ['S', /^Мощности наборов не складываются/],
    ['H', /^Набор$/, 'CALC_BUNDLES'],
    ['RS', 'CALC_BUNDLES', /^ИТОГО$/],
    ['T', /^ИТОГО$/, 'CALC_BUNDLES'],
    ['S', /^$/],
    ['S', /^СОБРАТЬ СЕЙЧАС → компоненты/],
    ['H', /^Набор$/, 'CALC_BOM'],
    ['RS', 'CALC_BOM', /^ИТОГО$/],
    ['T', /^ИТОГО$/, 'CALC_BOM'],
    ['S', /^$/],
    ['S', /^PICK FROM STORAGE · снять с паллет$/],
    ['S', /^Итог спроса/],
    ['H', /^Компонент$/, 'CALC_PICK'],
    ['RS', 'CALC_PICK', /^ИТОГО$/],
    ['T', /^ИТОГО$/, 'CALC_PICK'],
    ['END']
  ]
};

function opsFixedLabel_(kind, key) {
  if (kind === 'USEND') return OPS_TEXT.USEND_LABELS[key];
  if (kind === 'FRESH') return OPS_TEXT.FRESH_LABELS[key];
  return new RegExp('^' + key + '\\. ');   // RULES
}

/** Чистая функция: тексты колонки A листа C1.2 → маркеры (последний — @END). Любое расхождение → OpsError_. */
function opsDiscoverC12_(name, colA) {
  var steps = OPS_C12_STEPS[name];
  opsCheck_(steps, 'LAYOUT', 'нет описания C1.2 для листа ' + name);
  var i = 0, marks = [];
  var txt = function (k) { return k < colA.length ? String(colA[k] === null || colA[k] === undefined ? '' : colA[k]).trim() : null; };
  steps.forEach(function (st) {
    var type = st[0];
    if (type === 'END') {
      opsCheck_(i === colA.length, 'LAYOUT', name + ': после строки ' + i + ' есть строки вне C1.2');
      marks.push('@END');
      return;
    }
    if (type === 'F') {
      st[2].forEach(function (key) {
        var t = txt(i), want = opsFixedLabel_(st[3], key);
        opsCheck_(t !== null && (want instanceof RegExp ? want.test(t) : t === want), 'LAYOUT',
          name + ': строка ' + (i + 1) + ' «' + t + '» — ожидалась строка ' + st[1] + ':' + key);
        marks.push(opsMarker_('R', st[1], key));
        i++;
      });
      return;
    }
    if (type === 'RS') {
      var n0 = i;
      while (i < colA.length && !st[2].test(txt(i))) {
        opsCheck_(txt(i) !== '', 'LAYOUT', name + ': пустая строка ' + (i + 1) + ' внутри блока ' + st[1]);
        marks.push(opsMarker_('R', st[1], ''));
        i++;
      }
      opsCheck_(i > n0, 'LAYOUT', name + ': блок ' + st[1] + ' пуст');
      return;
    }
    var t = txt(i);
    opsCheck_(t !== null && st[1].test(t), 'LAYOUT', name + ': строка ' + (i + 1) + ' «' + t + '» не соответствует C1.2 (' +
      type + (st[2] ? ':' + st[2] : '') + ')');
    marks.push(type === 'L' ? '@L:' + OPS.LAYOUT_VERSION : type === 'S' ? '@S' : opsMarker_(type, st[2], null));
    i++;
  });
  return marks;
}

function opsIsLayoutInstalled_(ss) {
  var sh = ss.getSheetByName(OPS_SHEET.PLAN), col = OPS_LAYOUT[OPS_SHEET.PLAN].sysCol;
  return !!sh && sh.getMaxColumns() >= col && String(sh.getRange(1, col).getValue()) === '@L:' + OPS.LAYOUT_VERSION;
}

function opsProtectSysCol_(sh, col) {
  sh.hideColumns(col);
  var p = sh.getRange(1, col, sh.getMaxRows(), 1).protect();
  p.setDescription('C2: служебная колонка раскладки EVETIS OPERATIONS — не редактировать');
  p.setWarningOnly(true);
}

/** Установка. Сначала полная проверка всех листов без записи, затем запись. Возвращает отчёт. */
function opsInstallLayout_(ss) {
  var names = [OPS_SHEET.PLAN, OPS_SHEET.SHIP, OPS_SHEET.FF, OPS_SHEET.SETTINGS, OPS_SHEET.CALC];
  var report = { sheets: {}, replaced: [], at: opsNowText_() };
  opsCheck_(!ss.getSheetByName(OPS_SHEET.STATE), 'STATE_SHEET', 'лист _OPS_STATE уже существует');
  opsSheet_(ss, OPS_SHEET.DATA);

  var prep = {};
  names.forEach(function (name) {
    var sh = opsSheet_(ss, name), spec = OPS_LAYOUT[name], last = sh.getLastRow();
    var colA = sh.getRange(1, 1, last, 1).getValues().map(function (r) { return r[0]; });
    var marks = opsDiscoverC12_(name, colA);
    if (sh.getMaxColumns() >= spec.sysCol) {
      var used = sh.getRange(1, spec.sysCol, last, 1).getValues().filter(function (r) { return r[0] !== ''; }).length;
      opsCheck_(used === 0, 'LAYOUT', name + ': служебная колонка ' + spec.sysCol + ' не пуста');
    }
    prep[name] = { sheet: sh, spec: spec, marks: marks, last: last };
  });
  var p = prep[OPS_SHEET.PLAN], d = p.sheet.getRange(1, 4, p.last, 1).getValues();
  p.marks.forEach(function (m, i) {
    if (/^@R:(WB_SHIP|OZON_SHIP):/.test(m)) {
      opsCheck_(d[i][0] === '', 'LAYOUT', '«Одобрено владельцем» в строке ' + (i + 1) +
        ' не пусто — переносить решения по названию товара нельзя; установка остановлена');
    }
  });

  names.forEach(function (name) {
    var q = prep[name], sh = q.sheet, marks = q.marks;
    if (name === OPS_SHEET.SETTINGS) marks = opsInstallStatusBlock_(sh, marks);
    opsEnsureColumns_(sh, q.spec.sysCol);
    sh.getRange(1, q.spec.sysCol, marks.length, 1).setValues(marks.map(function (m) { return [m]; }));
    opsProtectSysCol_(sh, q.spec.sysCol);
    report.sheets[name] = marks.length - 1;
  });
  SpreadsheetApp.flush();
  names.forEach(function (name) { opsParseSheetLayout_(prep[name].sheet, OPS_LAYOUT[name], {}); });   // проверка разметки

  opsInstallTexts_(ss, report);
  opsInstallData_(ss);
  opsInstallStateSheet_(ss);
  PropertiesService.getScriptProperties().setProperty('OPS_LAYOUT_INSTALLED', OPS.LAYOUT_VERSION + ' · ' + report.at);
  return report;
}

/** 04_SETTINGS: блок «ОБНОВЛЕНИЕ ДАННЫХ» после строки 3 (формат — от полосы правил и строк свежести). */
function opsInstallStatusBlock_(sh, marks) {
  var cols = OPS_LAYOUT[OPS_SHEET.SETTINGS].grid, add = 7;
  var rulesBand = 4, freshFirst = marks.indexOf('@R:FRESHNESS:WB_STOCK') + 1;
  opsCheck_(marks[rulesBand - 1] === '@S' && freshFirst > 0, 'LAYOUT', '04_SETTINGS: не найдены полоса правил или строки свежести');
  sh.insertRowsAfter(3, add);
  sh.getRange(rulesBand + add, 1, 1, cols).copyTo(sh.getRange(4, 1, 1, cols), SpreadsheetApp.CopyPasteType.PASTE_FORMAT, false);
  sh.setRowHeight(4, sh.getRowHeight(rulesBand + add));
  var band = sh.getRange(4, 1, 1, cols);
  if (!band.isPartOfMerge()) band.merge();
  sh.getRange(4, 1).setValue('ОБНОВЛЕНИЕ ДАННЫХ');
  sh.getRange(freshFirst + add, 1, 1, cols).copyTo(sh.getRange(5, 1, 5, cols), SpreadsheetApp.CopyPasteType.PASTE_FORMAT, false);
  sh.setRowHeights(5, 5, sh.getRowHeight(freshFirst + add));
  var ins = ['@S'].concat(OPS_STATUS_KEYS.map(function (k) { return opsMarker_('R', 'REFRESH_STATUS', k); }), ['@S']);
  return marks.slice(0, 3).concat(ins, marks.slice(3));
}

/** Заметки и заголовок C1.2 с утверждениями, которые через несколько дней становятся неверными. */
function opsInstallTexts_(ss, report) {
  var T = OPS_TEXT;
  function header(name, id) {
    var sh = opsSheet_(ss, name), m = opsParseSheetLayout_(sh, OPS_LAYOUT[name], {});
    return { sh: sh, row: m.blocks[id].headerRow };
  }
  function note(name, id, col, text) {
    var h = header(name, id), c = h.sh.getRange(h.row, col);
    report.replaced.push({ cell: name + '!' + c.getA1Notation(), old_note: c.getNote() });
    c.setNote(text);
  }
  note(OPS_SHEET.PLAN, 'WB_SHIP', 4, T.OWNER_NOTE);
  note(OPS_SHEET.PLAN, 'OZON_SHIP', 4, T.OWNER_NOTE);
  note(OPS_SHEET.PLAN, 'TASK_MOVE', 4, T.NOTE_TASK_MOVE_RESERVED);
  ['CALC_WB', 'CALC_OZON'].forEach(function (id) {
    note(OPS_SHEET.CALC, id, 9, T.NOTE_DEMAND_UNTIL_ARRIVAL);
    note(OPS_SHEET.CALC, id, 13, T.NOTE_TARGET);
    note(OPS_SHEET.CALC, id, 14, T.NOTE_SAFETY);
    note(OPS_SHEET.CALC, id, 21, T.NOTE_COVER_AFTER);
  });
  note(OPS_SHEET.CALC, 'CALC_BUNDLES', 10, T.NOTE_FBS_RESERVE);
  note(OPS_SHEET.CALC, 'CALC_PICK', 11, T.NOTE_PICK_RESERVED);
  var h = header(OPS_SHEET.SHIP, 'SHIPMENTS'), hc = h.sh.getRange(h.row, 17);
  report.replaced.push({ cell: OPS_SHEET.SHIP + '!' + hc.getA1Notation(), old_value: hc.getValue() });
  hc.setValue(T.SHIP_EVIDENCE_HEADER);
}

/** DATA: одноразово очистить старый сырой дамп C1.2 и разметить блоки V_OPS_SHEET_*. Лист остаётся скрытым. */
function opsInstallData_(ss) {
  var sh = opsSheet_(ss, OPS_SHEET.DATA);
  var lastR = Math.max(sh.getLastRow(), 1), lastC = Math.max(sh.getLastColumn(), 2);
  sh.getRange(1, 1, lastR, lastC).clearContent().clearFormat();
  var rows = [['@L:' + OPS.LAYOUT_VERSION, 'Сырые строки контракта evetis_ops (V_OPS_SHEET_*) — источник чисел этой книги. ' +
    'Лист скрыт; колонка A служебная.'], ['@S', '']];
  var bandRows = [];
  Object.keys(OPS_VIEWS).forEach(function (v) {
    bandRows.push(rows.length + 1);
    rows.push(['@B:D_' + v, v], ['@H:D_' + v, ''], ['@E:D_' + v, OPS_TEXT.EMPTY_ROW], ['@S', ''], ['@S', '']);
  });
  rows.push(['@END', '']);
  sh.getRange(1, 1, rows.length, 2).setValues(rows);
  sh.getRange(1, 2).setFontWeight('bold');
  bandRows.forEach(function (r) { sh.getRange(r, 2).setFontWeight('bold').setFontColor('#4a148c'); });
  opsProtectSysCol_(sh, OPS_DATA_SYS_COL);
}

function opsInstallStateSheet_(ss) {
  var sh = ss.insertSheet(OPS_SHEET.STATE, ss.getSheets().length);
  sh.getRange(1, 1, 1, OPS_STATE_COLS.length).setValues([OPS_STATE_COLS])
    .setFontWeight('bold').setFontColor(OPS_COLOR.WHITE).setBackground(OPS_COLOR.DARK);
  sh.getRange(1, OPS_EVENT_COL0, 1, OPS_EVENT_COLS.length).setValues([OPS_EVENT_COLS])
    .setFontWeight('bold').setFontColor(OPS_COLOR.WHITE).setBackground('#274e13');
  sh.getRange(1, 1).setNote(OPS_TEXT.STATE_TITLE);
  sh.setFrozenRows(1);
  var p = sh.protect();
  p.setDescription(OPS_TEXT.STATE_TITLE);
  p.setWarningOnly(true);
  sh.hideSheet();
  ss.getSheetByName(OPS_SHEET.PLAN).activate();
}

// ═══════════ OpsMain.gs ═══════════
/**
 * EVETIS OPERATIONS · C2 — обновление листа и точки входа.
 *
 * Обновление (opsRefresh_): блокировка → все запросы к контракту → проверка схемы, ключей, сверок, одного дня
 * → проверка раскладки всех листов → загрузка и сверка решений владельца → отрисовка в памяти → ТОЛЬКО ПОТОМ запись.
 * Ошибка до записи: в книге меняется только статус (ОШИБКА), данные на экране — последний проверенный снимок.
 * Ошибка во время записи: флаг OPS_WRITE_IN_PROGRESS; следующее обновление перерисовывает сгенерированные
 * диапазоны из свежих данных, а если их не получить — из последнего успешного снимка. _OPS_STATE не откатывается.
 *
 * Точки входа: onOpen (меню, без BigQuery), opsMenuRefresh / opsMenuStatus / opsMenuEnableAuto / opsMenuDisableAuto,
 * opsTriggerRefresh (каждый час), opsOnEditInstalled (решение владельца), opsSetup (однократно из редактора).
 */

// ───────────────────────────── меню ─────────────────────────────

function onOpen() {
  var ui = SpreadsheetApp.getUi();
  ui.createMenu('EVETIS OPERATIONS')
    .addItem('Обновить данные', 'opsMenuRefresh')
    .addItem('Проверить статус', 'opsMenuStatus')
    .addSeparator()
    .addItem('Включить автообновление (каждый час)', 'opsMenuEnableAuto')
    .addItem('Выключить автообновление', 'opsMenuDisableAuto')
    .addToUi();
  if (typeof opsTestMenu_ === 'function') opsTestMenu_(ui);   // только во временной тестовой копии (OpsTests.gs)
  try {
    var st = JSON.parse(PropertiesService.getScriptProperties().getProperty('OPS_STATUS') || 'null');
    if (!st) return;
    var ss = SpreadsheetApp.getActive();
    var ageH = (Date.now() - Number(st.lastOkCheckMs || st.lastSuccessMs || 0)) / 3600000;
    if (st.status === 'ERROR') {
      ss.toast('Последнее обновление не прошло проверку: ' + st.message + '. На экране — данные на ' +
        opsFmtMsk_(st.dataAsOfMs) + ' МСК.', 'EVETIS OPERATIONS', 15);
    } else if (ageH > OPS.STALE_REFRESH_HOURS) {
      ss.toast('Успешной проверки данных не было ' + Math.floor(ageH) + ' ч (данные на ' + opsFmtMsk_(st.dataAsOfMs) +
        ' МСК). Меню EVETIS OPERATIONS → Обновить данные.', 'EVETIS OPERATIONS', 15);
    }
  } catch (e) {
    // onOpen не должен падать из-за статуса
  }
}

function opsMenuRefresh() {
  var run = opsRefresh_('MENU', { force: true });
  SpreadsheetApp.getActive().toast(opsRunText_(run), 'EVETIS OPERATIONS', 12);
}

function opsMenuStatus() {
  var props = PropertiesService.getScriptProperties(), ui = SpreadsheetApp.getUi();
  var st = JSON.parse(props.getProperty('OPS_STATUS') || '{}'), log = JSON.parse(props.getProperty('OPS_RUNLOG') || '[]');
  var lines = [
    'Статус: ' + (st.status || 'обновлений ещё не было') + (st.message ? ' — ' + st.message : ''),
    'Последнее успешное обновление: ' + (st.lastSuccessMs ? opsFmtMsk_(st.lastSuccessMs) + ' МСК' : '—'),
    'Последняя проверка: ' + (st.lastCheckMs ? opsFmtMsk_(st.lastCheckMs) + ' МСК' : '—'),
    'Данные на экране: ' + (st.dataAsOfMs ? opsFmtMsk_(st.dataAsOfMs) + ' МСК' : '—'),
    'Автообновление: ' + opsNextAutoText_(Date.now()),
    '',
    'Последние запуски:'
  ];
  log.slice(0, 8).forEach(function (r) {
    lines.push(r.t0 + ' · ' + r.src + ' · ' + r.st + ' · ' + r.s + ' с · строк ' + r.rr + ' / записано ' + r.rw +
      (r.err ? ' · ' + r.err : ''));
  });
  ui.alert('EVETIS OPERATIONS — обновление данных', lines.join('\n'), ui.ButtonSet.OK);
}

function opsMenuEnableAuto() {
  opsSetAutoRefresh_(true);
  opsRedrawStatusFromProps_();
  SpreadsheetApp.getActive().toast('Автообновление включено: каждый час.', 'EVETIS OPERATIONS', 8);
}

function opsMenuDisableAuto() {
  opsSetAutoRefresh_(false);
  opsRedrawStatusFromProps_();
  SpreadsheetApp.getActive().toast('Автообновление выключено. Обновлять — через меню.', 'EVETIS OPERATIONS', 8);
}

function opsTriggerRefresh() {
  opsRefresh_('TRIGGER');
}

/** Однократно из редактора Apps Script: разметка C1.2 → C2, триггер решений владельца, первое обновление. */
function opsSetup() {
  var ss = SpreadsheetApp.getActive(), out = {};
  var lock = LockService.getScriptLock();
  lock.waitLock(60000);
  try {
    out.install = opsIsLayoutInstalled_(ss) ? 'уже установлено' : opsInstallLayout_(ss);
  } finally {
    lock.releaseLock();
  }
  opsEnsureEditTrigger_();
  var run = opsRefresh_('SETUP', { force: true });
  out.refresh = { st: run.st, err: run.err, rows: run.rr, written: run.rw };
  console.log(JSON.stringify({ ops_setup: out }));
  return out;
}

/** Из редактора или меню: включить ежечасное обновление. */
function opsEnableAutoRefresh() {
  opsSetAutoRefresh_(true);
  opsRedrawStatusFromProps_();
  console.log('ops: автообновление включено');
}

function opsEnsureEditTrigger_() {
  var has = ScriptApp.getProjectTriggers().some(function (t) { return t.getHandlerFunction() === OPS.EDIT_HANDLER; });
  if (!has) ScriptApp.newTrigger(OPS.EDIT_HANDLER).forSpreadsheet(SpreadsheetApp.getActive()).onEdit().create();
}

function opsSetAutoRefresh_(on) {
  var ts = ScriptApp.getProjectTriggers().filter(function (t) { return t.getHandlerFunction() === OPS.REFRESH_HANDLER; });
  if (on && !ts.length) ScriptApp.newTrigger(OPS.REFRESH_HANDLER).timeBased().everyHours(1).create();
  if (!on) ts.forEach(function (t) { ScriptApp.deleteTrigger(t); });
}

function opsNextAutoText_(nowMs) {
  var on = ScriptApp.getProjectTriggers().some(function (t) { return t.getHandlerFunction() === OPS.REFRESH_HANDLER; });
  if (!on) return 'выключено (меню EVETIS OPERATIONS → Включить автообновление)';
  var last = Number(PropertiesService.getScriptProperties().getProperty('OPS_LAST_TRIGGER_AT') || 0);
  var next = last ? last + 3600000 : 0;
  while (next && next < nowMs) next += 3600000;
  return 'каждый час' + (next ? ' · ориентировочно ' + opsFmtMsk_(next, 'HH:mm') + ' МСК' : ' · первый запуск в течение часа');
}

// ───────────────────────────── обновление ─────────────────────────────

function opsRefresh_(source, opts) {
  opts = opts || {};
  var run = { id: Utilities.getUuid().slice(0, 8), src: source, t0: Date.now(), st: '', rr: 0, rw: 0, sts: '', err: '' };
  var lock = LockService.getScriptLock();
  if (!lock.tryLock(OPS.LOCK_WAIT_MS)) {
    run.st = 'SKIPPED_LOCKED';
    opsLogRun_(run);
    return run;
  }
  var props = PropertiesService.getScriptProperties(), ss = SpreadsheetApp.getActive();
  try {
    if (source === 'TRIGGER') props.setProperty('OPS_LAST_TRIGGER_AT', String(run.t0));
    var pending = props.getProperty('OPS_WRITE_IN_PROGRESS');
    var prepared = null, failure = null;
    try {
      var payload = opsFetchAll_();
      if (typeof opsTestHook_ === 'function') opsTestHook_('afterFetch', payload);
      prepared = opsPrepare_(ss, payload, { recovery: !!pending });
    } catch (err) {
      failure = err;
      run.err = opsErrText_(err);
    }
    if (failure && pending) {
      var last = opsLoadSnapshot_('OPS_SNAP');
      if (last) {
        try {
          prepared = opsPrepare_(ss, last, { recovery: true });
          prepared.restored = true;
        } catch (err2) {
          run.err += ' | восстановление: ' + opsErrText_(err2);
          prepared = null;
        }
      }
    }
    if (!prepared) {
      run.st = 'FAILED_CLOSED';
      opsWriteStatusSafe_(ss, opsStatusError_(props, failure, run.t0, null), run);
      return run;
    }
    run.rr = prepared.rowsReceived;
    run.sts = prepared.sourceTs;

    var unchanged = !pending && !prepared.restored && !opts.force && prepared.state.events.length === 0 &&
      prepared.hash === props.getProperty('OPS_LAST_HASH');
    var status = prepared.restored ? opsStatusError_(props, failure, run.t0, prepared)
      : opsStatusOk_(prepared, run.t0, unchanged, props);
    if (unchanged) {
      // проверка без изменений не сдвигает «Данные на» и «Последнее успешное обновление» — только «Последняя проверка»
      props.setProperty('OPS_LAST_OK_CHECK_AT', String(run.t0));
      opsWriteStatusSafe_(ss, status, run);
      run.st = prepared.warnings.length ? 'WARNING_NO_CHANGE' : 'OK_NO_CHANGE';
      return run;
    }

    var render = opsRender_(prepared.payload, prepared.state.byKey, status);   // вся отрисовка — до первой записи
    props.setProperty('OPS_WRITE_IN_PROGRESS', run.id);
    opsWriteState_(ss, prepared.state.rows, prepared.state.events, prepared.stateEventCount);
    var recovery = !!pending || prepared.restored;
    OPS_WRITE_ORDER.forEach(function (name) {
      run.rw += opsWriteSheet_(ss, name, render[name], { recovery: recovery });
    });
    SpreadsheetApp.flush();
    if (!prepared.restored) {
      opsSaveSnapshot_('OPS_SNAP', prepared.payload);
      props.setProperties({
        OPS_LAST_HASH: prepared.hash, OPS_LAST_GOOD_AT: String(prepared.payload.fetchedAtMs), OPS_LAST_GOOD_TAIL: status.tail,
        OPS_LAST_GOOD_FRESH: status.freshness, OPS_LAST_SUCCESS_AT: String(run.t0), OPS_LAST_OK_CHECK_AT: String(run.t0)
      });
    }
    props.deleteProperty('OPS_WRITE_IN_PROGRESS');
    opsSaveStatus_(props, status);
    run.st = prepared.restored ? 'RESTORED_LAST_GOOD' : (prepared.warnings.length ? 'WARNING' : 'OK');
    return run;
  } catch (err) {
    run.st = 'WRITE_FAILED';
    run.err = (run.err ? run.err + ' | ' : '') + opsErrText_(err);
    opsWriteStatusSafe_(ss, opsStatusError_(props, OpsError_('WRITE', opsErrText_(err)), run.t0, null), run);
    return run;
  } finally {
    opsLogRun_(run);
    lock.releaseLock();
  }
}

/** Всё, что должно пройти до первой записи. */
function opsPrepare_(ss, payload, opts) {
  var validation = opsValidatePayload_(payload);
  if (typeof opsTestHook_ === 'function') opsTestHook_('afterValidate', payload);
  opsValidateLayouts_(ss, { recovery: !!opts.recovery });
  var stateRead = opsReadState_(ss);
  var state = opsReconcileState_(stateRead.rows, payload.views.V_OPS_SHEET_PLAN.rows, opsNowText_());
  var meta = payload.views.V_OPS_SHEET_META.rows[0];
  return {
    payload: payload, warnings: validation.warnings, rowsReceived: validation.rowsReceived, state: state,
    stateEventCount: stateRead.eventCount, hash: opsDigestHex_(opsHashInput_(payload)),
    sourceTs: 'WB ' + meta.wb_stock_date + ' · Ozon ' + meta.ozon_stock_date, restored: false
  };
}

/**
 * «Данные на» и «Последнее успешное обновление» — время последней ЗАПИСИ данных в лист.
 * При проверке без изменений они остаются прежними; меняется только «Последняя проверка».
 */
function opsStatusOk_(prepared, nowMs, unchanged, props) {
  var meta = prepared.payload.views.V_OPS_SHEET_META.rows[0], warn = prepared.warnings;
  var lastGoodAt = Number(props.getProperty('OPS_LAST_GOOD_AT') || 0);
  var lastWriteAt = Number(props.getProperty('OPS_LAST_SUCCESS_AT') || 0);
  return {
    status: warn.length ? 'WARNING' : 'OK', message: warn.join(', '),
    dataAsOfMs: unchanged && lastGoodAt ? lastGoodAt : prepared.payload.fetchedAtMs,
    lastSuccessMs: unchanged && lastWriteAt ? lastWriteAt : nowMs,
    lastCheckMs: nowMs, tail: opsStatusLineTail_(prepared.payload), freshness: opsFreshnessText_(meta),
    nextAuto: opsNextAutoText_(nowMs), checkResult: unchanged ? 'данные не изменились' : 'данные обновлены'
  };
}

function opsStatusError_(props, err, nowMs, prepared) {
  return {
    status: 'ERROR', message: opsUserError_(err),
    dataAsOfMs: prepared ? prepared.payload.fetchedAtMs : Number(props.getProperty('OPS_LAST_GOOD_AT') || 0),
    lastSuccessMs: Number(props.getProperty('OPS_LAST_SUCCESS_AT') || 0), lastCheckMs: nowMs,
    tail: prepared ? opsStatusLineTail_(prepared.payload) : (props.getProperty('OPS_LAST_GOOD_TAIL') || ''),
    freshness: prepared ? opsFreshnessText_(prepared.payload.views.V_OPS_SHEET_META.rows[0]) : (props.getProperty('OPS_LAST_GOOD_FRESH') || ''),
    nextAuto: opsNextAutoText_(nowMs),
    checkResult: prepared ? 'ошибка — показан последний успешный снимок' : 'ошибка — данные на экране не менялись'
  };
}

/** Короткий текст ошибки для владельца. Техническая подробность — только в журнале запусков («Проверить статус»). */
function opsUserError_(err) {
  var code = err && err.opsCode ? err.opsCode : 'INTERNAL';
  var text = OPS_TEXT.ERRORS[code] || OPS_TEXT.ERRORS.INTERNAL;
  if (/^(BQ_QUERY|BQ_TIMEOUT|WRITE|INTERNAL)$/.test(code)) return text;
  var detail = err && err.message ? String(err.message).replace(/^[A-Z_]+: /, '') : '';
  return text + (detail ? ' (' + (detail.length > 60 ? detail.slice(0, 57) + '…' : detail) + ')' : '');
}

function opsErrText_(err) {
  return String(err && err.message ? err.message : err).slice(0, 400);
}

function opsSaveStatus_(props, st) {
  props.setProperty('OPS_STATUS', JSON.stringify({ status: st.status, message: st.message, dataAsOfMs: st.dataAsOfMs,
    lastSuccessMs: st.lastSuccessMs, lastCheckMs: st.lastCheckMs, checkResult: st.checkResult,
    lastOkCheckMs: Number(props.getProperty('OPS_LAST_OK_CHECK_AT') || 0) }));
}

function opsWriteStatusSafe_(ss, status, run) {
  var props = PropertiesService.getScriptProperties();
  try {
    opsWriteStatusOnly_(ss, status);
  } catch (e) {
    run.err = (run.err ? run.err + ' | ' : '') + 'статус не записан: ' + opsErrText_(e);
  }
  opsSaveStatus_(props, status);
}

function opsRedrawStatusFromProps_() {
  var props = PropertiesService.getScriptProperties(), s = JSON.parse(props.getProperty('OPS_STATUS') || 'null');
  if (!s) return;
  s.tail = props.getProperty('OPS_LAST_GOOD_TAIL') || '';
  s.freshness = props.getProperty('OPS_LAST_GOOD_FRESH') || '';
  s.nextAuto = opsNextAutoText_(Date.now());
  try { opsWriteStatusOnly_(SpreadsheetApp.getActive(), s); } catch (e) { console.warn('ops: статус не перерисован: ' + opsErrText_(e)); }
}

function opsRunText_(run) {
  var map = {
    OK: 'Данные обновлены.', OK_NO_CHANGE: 'Проверено: данные не изменились.',
    WARNING: 'Данные обновлены, но часть источников устарела — см. 04_SETTINGS.',
    WARNING_NO_CHANGE: 'Данные не изменились, часть источников устарела — см. 04_SETTINGS.',
    FAILED_CLOSED: 'Обновление не прошло проверку — на экране остались последние проверенные данные.',
    RESTORED_LAST_GOOD: 'Свежие данные не получены — восстановлен последний успешный снимок.',
    WRITE_FAILED: 'Запись прервана — при следующем обновлении лист восстановится.',
    SKIPPED_LOCKED: 'Обновление уже идёт — повторите через минуту.'
  };
  return (map[run.st] || run.st) + (run.err ? ' ' + run.err.slice(0, 160) : '');
}

// ───────────────────────────── BigQuery ─────────────────────────────

function opsFetchAll_() {
  if (typeof opsTestHook_ === 'function') {
    var cached = opsTestHook_('fetch', null);
    if (cached) return cached;
  }
  var P = OPS.PROJECT_ID, suffix = typeof opsTestViewSuffix_ === 'function' ? opsTestViewSuffix_() : '';
  var jobs = Object.keys(OPS_VIEWS).map(function (name) {
    try {
      var job = BigQuery.Jobs.insert({
        jobReference: { projectId: P, location: OPS.LOCATION },
        configuration: {
          labels: { stage: 'stage-c2', component: 'evetis-operations-sheet' },
          query: { query: 'SELECT * FROM `' + P + '.' + OPS.DATASET + '.' + name + suffix + '`', useLegacySql: false }
        }
      }, P);
      return { name: name, id: job.jobReference.jobId };
    } catch (err) {
      throw OpsError_('BQ_QUERY', name + ': ' + opsErrText_(err));
    }
  });
  var views = {}, deadline = Date.now() + OPS.BQ_TIMEOUT_MS;
  jobs.forEach(function (j) {
    var res;
    while (true) {
      try {
        res = BigQuery.Jobs.getQueryResults(P, j.id, { location: OPS.LOCATION, timeoutMs: 30000, maxResults: 10000 });
      } catch (err) {
        throw OpsError_('BQ_QUERY', j.name + ': ' + opsErrText_(err));
      }
      if (res.jobComplete) break;
      if (Date.now() > deadline) throw OpsError_('BQ_TIMEOUT', j.name);
    }
    var raw = res.rows || [], token = res.pageToken;
    while (token) {
      var page = BigQuery.Jobs.getQueryResults(P, j.id, { location: OPS.LOCATION, pageToken: token, maxResults: 10000 });
      raw = raw.concat(page.rows || []);
      token = page.pageToken;
    }
    opsCheck_(Number(res.totalRows) === raw.length, 'BQ_QUERY', j.name + ': получено ' + raw.length + ' строк из ' + res.totalRows);
    var fields = res.schema.fields.map(function (f) { return { name: f.name, type: f.type }; });
    views[j.name] = { fields: fields, rows: opsTypeRows_(fields, raw) };
  });
  return { fetchedAtMs: Date.now(), views: views };
}

function opsDigestHex_(s) {
  return Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, s, Utilities.Charset.UTF_8)
    .map(function (b) { return ('0' + (b & 0xff).toString(16)).slice(-2); }).join('');
}

// ───────────────────────────── снимок и журнал запусков ─────────────────────────────

function opsSaveSnapshot_(prefix, payload) {
  var props = PropertiesService.getScriptProperties();
  var b64 = Utilities.base64Encode(Utilities.gzip(Utilities.newBlob(JSON.stringify(payload), 'application/json')).getBytes());
  var n = Math.ceil(b64.length / OPS.SNAPSHOT_CHUNK), chunks = {}, old = Number(props.getProperty(prefix + '_N') || 0);
  for (var i = 0; i < n; i++) chunks[prefix + '_' + i] = b64.substr(i * OPS.SNAPSHOT_CHUNK, OPS.SNAPSHOT_CHUNK);
  chunks[prefix + '_N'] = String(n);
  props.setProperties(chunks);
  for (var j = n; j < old; j++) props.deleteProperty(prefix + '_' + j);
}

function opsLoadSnapshot_(prefix) {
  var props = PropertiesService.getScriptProperties(), n = Number(props.getProperty(prefix + '_N') || 0);
  if (!n) return null;
  var b64 = '';
  for (var i = 0; i < n; i++) b64 += props.getProperty(prefix + '_' + i) || '';
  try {
    var blob = Utilities.ungzip(Utilities.newBlob(Utilities.base64Decode(b64), 'application/x-gzip'));
    return JSON.parse(blob.getDataAsString());
  } catch (e) {
    return null;
  }
}

function opsLogRun_(run) {
  run.t1 = Date.now();
  console.log(JSON.stringify({ ops_refresh: run }));
  var props = PropertiesService.getScriptProperties(), log = [];
  try { log = JSON.parse(props.getProperty('OPS_RUNLOG') || '[]'); } catch (e) { log = []; }
  log.unshift({ id: run.id, src: run.src, st: run.st, t0: opsFmtMsk_(run.t0, 'dd.MM HH:mm:ss'), s: Math.round((run.t1 - run.t0) / 1000),
    rr: run.rr, rw: run.rw, sts: run.sts, err: String(run.err || '').slice(0, 180) });
  while (log.length > OPS.RUNLOG_KEEP || JSON.stringify(log).length > 8500) log.pop();
  props.setProperty('OPS_RUNLOG', JSON.stringify(log));
}

// ───────────────────────────── решение владельца (installable onEdit) ─────────────────────────────

function opsOnEditInstalled(e) {
  if (!e || !e.range) return;
  var sheet = e.range.getSheet();
  if (sheet.getName() !== OPS_SHEET.PLAN) return;
  var spec = OPS_LAYOUT[OPS_SHEET.PLAN], ss = sheet.getParent();
  var r0 = e.range.getRow(), n = e.range.getNumRows(), c0 = e.range.getColumn(), c1 = c0 + e.range.getNumColumns() - 1;
  if (n > 300 || c0 > spec.grid) return;
  try {
    var marks = sheet.getRange(r0, spec.sysCol, n, 1).getValues();
    var owner = c0 <= 4 && c1 >= 4 ? sheet.getRange(r0, 4, n, 1).getValues() : null;
    var captured = [], generatedTouched = false, notReady = false;
    for (var i = 0; i < n; i++) {
      var t;
      try { t = opsParseMarker_(marks[i][0], r0 + i); } catch (x) { continue; }
      var ship = t.type === 'R' && (t.id === 'WB_SHIP' || t.id === 'OZON_SHIP');
      if (ship && owner) {
        var k = opsSplitShipRowKey_(t.key);
        if (k.businessKey) captured.push({ key: k.businessKey, fp: k.fingerprint, value: owner[i][0] });
        else notReady = true;
      }
      if (/^[RTVB]$/.test(t.type) && !(ship && c0 === 4 && c1 === 4)) generatedTouched = true;
    }
    if (generatedTouched) {
      ss.toast('Это рассчитанное поле: при следующем обновлении его перезапишут данные BigQuery.', 'EVETIS OPERATIONS', 6);
    }
    if (!captured.length) {
      if (notReady) ss.toast('Сначала обновите данные (EVETIS OPERATIONS → Обновить данные), затем внесите решение.', 'EVETIS OPERATIONS', 8);
      return;
    }
    var lock = LockService.getScriptLock();
    if (!lock.tryLock(OPS.EDIT_LOCK_WAIT_MS)) {
      ss.toast('Решение НЕ сохранено: идёт обновление данных. Введите значение ещё раз через минуту.', 'EVETIS OPERATIONS', 12);
      return;
    }
    try {
      var st = opsReadState_(ss), byKey = {}, events = [], invalid = 0, now = opsNowText_();
      st.rows.forEach(function (s) { byKey[s.business_key] = s; });
      captured.forEach(function (c) {
        var s = byKey[c.key];
        if (!s) {
          s = opsNewStateRow_(c.key);
          s.current_fingerprint = c.fp;
          st.rows.push(s);
          byKey[c.key] = s;
        }
        var res = opsApplyOwnerEdit_(s, c.value, c.fp, now);
        if (res.invalid) invalid++;
        events = events.concat(res.events);
      });
      opsWriteState_(ss, st.rows, events, st.eventCount);
      opsRedrawOwnerCells_(sheet, spec, byKey, captured);
      if (invalid) ss.toast('В «Одобрено владельцем» нужно целое число позиций (0 или больше). Значение не принято.', 'EVETIS OPERATIONS', 10);
    } finally {
      lock.releaseLock();
    }
  } catch (err) {
    console.error(JSON.stringify({ ops_onedit_error: opsErrText_(err) }));
    ss.toast('Решение не сохранено: ' + opsErrText_(err).slice(0, 140), 'EVETIS OPERATIONS', 12);
  }
}

/** Перерисовать «Одобрено владельцем» у строк с этими ключами — там, где строки находятся сейчас. */
function opsRedrawOwnerCells_(sheet, spec, byKey, captured) {
  var want = {};
  captured.forEach(function (c) { want[c.key] = true; });
  opsReadMarkers_(sheet, spec.sysCol).forEach(function (m, i) {
    var t;
    try { t = opsParseMarker_(m, i + 1); } catch (x) { return; }
    if (t.type !== 'R' || (t.id !== 'WB_SHIP' && t.id !== 'OZON_SHIP')) return;
    var k = opsSplitShipRowKey_(t.key).businessKey;
    if (!want[k]) return;
    var d = opsOwnerDisplay_(byKey[k]), cell = sheet.getRange(i + 1, 4);
    cell.setValue(d.value);
    cell.setFontColor(d.fg);
    cell.setFontWeight(d.bold ? 'bold' : 'normal');
    cell.setHorizontalAlignment(d.align);
    cell.setFontSize(d.size);
    cell.setNote(d.note);
  });
}
