/**
 * ══════════════════════════════════════════════════════════════
 * EVETIS WB — WbOperationalPilot.gs  v1.0  (PR #16A)
 *
 * API-first operational pilot foundation.
 * Ручной оркестратор загрузки ОПЕРАЦИОННЫХ данных WB через уже
 * существующие production-loader'ы за пилотный период:
 *   orders        → importWbOrdersFromApiInternal_(from,to)       → RAW_WB_ORDERS
 *   sales/returns → importWbSalesReturnsFromApiInternal_(from,to) → RAW_WB_SALES_RETURNS
 *
 * ЭТО ВОРОНКА (operational), НЕ P&L. Денежный факт остаётся в
 * RAW_WB_FINANCE и этим модулем НЕ затрагивается.
 *
 * SCOPE (строго):
 *   - Только два вызова существующих internal-loader'ов + контрольный alert.
 *   - Идемпотентность обеспечивают САМИ loader'ы (replace-slice по своему
 *     source за период). Этот модуль СВОЕЙ очистки RAW НЕ делает.
 *   - Каждый слой в своём try/catch: падение orders не мешает sales и наоборот.
 *
 * НЕ ТРОГАЕТ И НЕ МЕНЯЕТ:
 *   - runWbDailyRefresh() и его rolling-14 путь.
 *   - WbOrdersLoader.gs / WbSalesReturnsLoader.gs (только ВЫЗЫВАЕТ).
 *   - RAW_WB_FINANCE, finance API-запись, Drive/XLSX.
 *   - stocks (RAW_WB_STOCKS) и ads (RAW_WB_ADS) — отложены в PR #16B / #16C.
 *   - Методологию CLEAN_WB_DAILY и UNIT_SKU_DAILY.
 *   - Себестоимость (COGS), dashboard.
 *
 * ОТЛОЖЕНО (следующие PR):
 *   PR #16B — WB stocks by warehouse API pilot (после выбора канона T5/T6).
 *   PR #16C — WB ads API raw pilot (после T8/T9).
 *   PR #16D — OPERATIONAL_SKU_DAILY mart (источник для dashboard).
 *
 * ПУБЛИЧНЫЕ ТОЧКИ ВХОДА:
 *   backfillWbOperationalPilotLast14Days()  — вчера−13 … вчера
 *   backfillWbOperationalPilotLast30Days()  — вчера−29 … вчера
 *
 * МЕНЮ: addWbOperationalPilotMenu() (отдельное меню «🧪 WB API Pilot»;
 *   подключается одной строкой в Menu v2 → onOpen).
 * ══════════════════════════════════════════════════════════════
 */


// ═══════════════════════════════════════
// КОНФИГ
// ═══════════════════════════════════════

var WB_PILOT_TZ_ = 'Europe/Moscow';


// ═══════════════════════════════════════
// ПЕРИОД (MSK)
// ═══════════════════════════════════════

/**
 * Окно «последние N дней»: вчера−(N−1) … вчера (MSK).
 * Для N=14 переиспользует wbDailyRolling14Range_() из WbDailyRefresh.gs,
 * если он доступен (НЕ изменяя его); иначе — локальный расчёт по тому же
 * принципу, что и financeRolling14Range_() в проекте.
 * @return {{from:string,to:string}} YYYY-MM-DD
 */
function wbPilotRangeDays_(days) {
  if (days === 14 && typeof wbDailyRolling14Range_ === 'function') {
    try {
      var reuse = wbDailyRolling14Range_();
      if (reuse && reuse.from && reuse.to) return reuse;
    } catch (e) {
      // тихий фолбэк на локальный расчёт
    }
  }
  var yesterday = new Date();
  yesterday.setDate(yesterday.getDate() - 1);
  var start = new Date(yesterday);
  start.setDate(start.getDate() - (days - 1));
  return {
    from: Utilities.formatDate(start, WB_PILOT_TZ_, 'yyyy-MM-dd'),
    to:   Utilities.formatDate(yesterday, WB_PILOT_TZ_, 'yyyy-MM-dd')
  };
}


// ═══════════════════════════════════════
// INTERNAL ORCHESTRATOR (без UI-алерта)
// ═══════════════════════════════════════

/** OK+OK→OK; любой ERROR при живом другом→WARN; оба ERROR→ERROR. */
function wbPilotOverallStatus_(ordersStatus, salesStatus) {
  if (ordersStatus === 'ERROR' && salesStatus === 'ERROR') return 'ERROR';
  if (ordersStatus === 'ERROR' || salesStatus === 'ERROR') return 'WARN';
  return 'OK';
}

/**
 * Грузит orders + sales/returns за [dateFrom,dateTo] через существующие
 * loader'ы. Своей очистки RAW НЕ делает (idempotency — внутри loader'ов).
 * @return {Object} контрольный result
 */
function runWbOperationalPilotPeriod_(dateFrom, dateTo, label) {
  var t0 = Date.now();

  var result = {
    label: label || '',
    period_from: dateFrom, period_to: dateTo,
    orders_status: 'SKIPPED', orders_rows: 0, orders_qty: 0, cancel_qty: 0, orders_error: '',
    sales_status: 'SKIPPED',  sales_rows: 0,  sales_qty: 0,  returns_qty: 0, sales_error: '',
    overall: 'OK', elapsed_sec: '0'
  };

  console.log('═══ runWbOperationalPilotPeriod_ [' + result.label + '] ' +
    dateFrom + ' — ' + dateTo + ' СТАРТ ═══');

  // ── Слой 1: Orders → RAW_WB_ORDERS ──
  try {
    if (typeof importWbOrdersFromApiInternal_ !== 'function') {
      result.orders_status = 'ERROR';
      result.orders_error = 'importWbOrdersFromApiInternal_ not found';
      console.log('⚠️ Orders: loader не найден');
    } else {
      var ord = importWbOrdersFromApiInternal_(dateFrom, dateTo);
      result.orders_status = (ord && ord.status) ? ord.status : 'ERROR';
      result.orders_rows   = Number((ord && ord.rows_imported) || 0);
      result.orders_qty    = Number((ord && ord.orders_count) || 0);
      result.cancel_qty    = Number((ord && ord.cancel_count) || 0);
      if (ord && ord.status === 'ERROR') result.orders_error = ord.error_message || 'ERROR';
      console.log('  Orders: ' + result.orders_status + ', rows=' + result.orders_rows +
        ', orders=' + result.orders_qty + ', cancels=' + result.cancel_qty);
    }
  } catch (e) {
    result.orders_status = 'ERROR';
    result.orders_error = e.message;
    console.log('❌ Orders исключение: ' + e.message);
  }

  // ── Слой 2: Sales/Returns → RAW_WB_SALES_RETURNS ──
  try {
    if (typeof importWbSalesReturnsFromApiInternal_ !== 'function') {
      result.sales_status = 'ERROR';
      result.sales_error = 'importWbSalesReturnsFromApiInternal_ not found';
      console.log('⚠️ Sales: loader не найден');
    } else {
      var sal = importWbSalesReturnsFromApiInternal_(dateFrom, dateTo);
      result.sales_status  = (sal && sal.status) ? sal.status : 'ERROR';
      result.sales_rows    = Number((sal && sal.rows_imported) || 0);
      result.sales_qty     = Number((sal && sal.sales_count) || 0);
      result.returns_qty   = Number((sal && sal.returns_count) || 0);
      if (sal && sal.status === 'ERROR') result.sales_error = sal.error_message || 'ERROR';
      console.log('  Sales: ' + result.sales_status + ', rows=' + result.sales_rows +
        ', sales=' + result.sales_qty + ', returns=' + result.returns_qty);
    }
  } catch (e) {
    result.sales_status = 'ERROR';
    result.sales_error = e.message;
    console.log('❌ Sales исключение: ' + e.message);
  }

  result.overall = wbPilotOverallStatus_(result.orders_status, result.sales_status);
  result.elapsed_sec = ((Date.now() - t0) / 1000).toFixed(1);

  console.log('═══ runWbOperationalPilotPeriod_ ЗАВЕРШЕНО: ' + result.overall +
    ', ' + result.elapsed_sec + ' сек ═══');
  return result;
}


// ═══════════════════════════════════════
// КОНТРОЛЬНЫЙ ALERT
// ═══════════════════════════════════════

function showWbOperationalPilotAlert_(r) {
  var ui = SpreadsheetApp.getUi();
  var msg =
    'Label: ' + r.label + '\n' +
    'Период: ' + r.period_from + ' — ' + r.period_to + '\n' +
    'Итог: ' + r.overall + '\n\n' +
    '── ORDERS (RAW_WB_ORDERS) ──\n' +
    'Статус: ' + r.orders_status + '\n' +
    'Строк загружено: ' + r.orders_rows + '\n' +
    'Заказов (qty): ' + r.orders_qty + '\n' +
    'Отмен (qty): ' + r.cancel_qty + '\n' +
    (r.orders_error ? 'Ошибка: ' + r.orders_error + '\n' : '') +
    '\n── SALES/RETURNS (RAW_WB_SALES_RETURNS) ──\n' +
    'Статус: ' + r.sales_status + '\n' +
    'Строк загружено: ' + r.sales_rows + '\n' +
    'Продаж (qty): ' + r.sales_qty + '\n' +
    'Возвратов (qty): ' + r.returns_qty + '\n' +
    (r.sales_error ? 'Ошибка: ' + r.sales_error + '\n' : '') +
    '\nВремя: ' + r.elapsed_sec + ' сек\n\n' +
    '⚠️ Operational API pilot — это воронка (orders/sales), НЕ P&L.\n' +
    'Финансовый факт остаётся в RAW_WB_FINANCE и не затрагивается.';

  ui.alert('🧪 WB API Pilot — ' + (r.label || ''), msg, ui.ButtonSet.OK);
}


// ═══════════════════════════════════════
// ПУБЛИЧНЫЕ ОБЁРТКИ
// ═══════════════════════════════════════

/** 🧪 Operational pilot: orders/sales за последние 14 дней (вчера−13 … вчера). */
function backfillWbOperationalPilotLast14Days() {
  var rng = wbPilotRangeDays_(14);
  var r = runWbOperationalPilotPeriod_(rng.from, rng.to, 'Last14Days');
  showWbOperationalPilotAlert_(r);
  return r;
}

/** 🧪 Operational pilot: orders/sales за последние 30 дней (вчера−29 … вчера). */
function backfillWbOperationalPilotLast30Days() {
  var rng = wbPilotRangeDays_(30);
  var r = runWbOperationalPilotPeriod_(rng.from, rng.to, 'Last30Days');
  showWbOperationalPilotAlert_(r);
  return r;
}


// ═══════════════════════════════════════
// МЕНЮ (подключить из Menu v2 → onOpen одной строкой:
//        addWbOperationalPilotMenu();)
// ═══════════════════════════════════════

function addWbOperationalPilotMenu() {
  SpreadsheetApp.getUi()
    .createMenu('🧪 WB API Pilot')
    .addItem('📥 Загрузить orders/sales за 14 дней', 'backfillWbOperationalPilotLast14Days')
    .addItem('📥 Загрузить orders/sales за 30 дней', 'backfillWbOperationalPilotLast30Days')
    .addToUi();
}
