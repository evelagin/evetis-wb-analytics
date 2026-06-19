/**
 * ══════════════════════════════════════════════════════════════
 * EVETIS WB — UnitSkuDailyBuilder.gs  v1.0  (PR #15B)
 *
 * Сборка ПЛОСКОЙ ежедневной SKU-витрины UNIT_SKU_DAILY из CLEAN_WB_DAILY.
 * Одна строка = date + SKU (гранулярность CLEAN: date|wb_nm_id).
 *
 * SCOPE PR #15B (строго):
 *   • Источник — только CLEAN_WB_DAILY (read-only).
 *   • Цель — лист UNIT_SKU_DAILY (плоский, НЕ транспонированный ЮНИТ_MM_YYYY).
 *   • Переносятся метрики воронки/продаж/выручки + рассчитываются коэффициенты.
 *   • Append-only: недостающие колонки дописываются в КОНЕЦ шапки; старые
 *     колонки не удаляются и не переставляются; запись по ИМЕНИ колонки.
 *
 * НЕ ДЕЛАЕТ / НЕ ТРОГАЕТ:
 *   • Не пишет в CLEAN_WB_DAILY (только читает).
 *   • Не трогает ЮНИТ_MM_YYYY, MonthlyUnitReport*, PNL_TOTAL, dashboards,
 *     RAW_*, loaders, SheetsSchema, Menu, triggers.
 *   • Не заполняет stock/cogs/profit/margin/P&L/status (вынесено в др. PR).
 *
 * ПЕРЕИСПОЛЬЗУЕТ (существующие утилиты проекта, без изменений):
 *   getHeaderMap_, readSheetData_, findCol_, normalizeNmIdFinance_,
 *   normalizeDateKey_, roundTwo_, sec_
 * ══════════════════════════════════════════════════════════════
 */


// ═══════════════════════════════════════
// КОНФИГ
// ═══════════════════════════════════════

var UNIT_SKU_DAILY_SHEET_ = 'UNIT_SKU_DAILY';
var UNIT_SKU_CLEAN_SHEET_ = 'CLEAN_WB_DAILY';

/**
 * Колонки, которые builder заполняет. Должны существовать в шапке;
 * недостающие дописываются append-only в конец (см. ensureUnitColumns_).
 * Если лист создаётся с нуля — шапка = этот список.
 */
var UNIT_SKU_FILL_COLS_ = [
  // идентификация строки
  'date', 'internal_sku', 'product_name', 'row_key',
  // воронка заказов/отмен
  'orders_qty', 'cancel_qty', 'active_orders_qty', 'order_cancel_rate_percent',
  // продажи/возвраты
  'sales_qty', 'returns_qty', 'net_sales_qty',
  'buyout_rate_percent', 'return_rate_percent',
  // выручка и реклама (деньги — из CLEAN)
  'gross_revenue', 'returns_amount', 'net_revenue', 'ads_spend',
  // остатки (PR #16D) — append-only, после всех существующих колонок
  'stock_qty', 'stock_status'
];


// ═══════════════════════════════════════
// МЕНЮ (опционально; НЕ авто-подключается к onOpen в этом PR)
// ═══════════════════════════════════════

/** Опциональное меню. Подключение в onOpen — отдельным PR (Menu не трогаем). */
function addUnitSkuDailyMenu() {
  SpreadsheetApp.getUi()
    .createMenu('📦 UNIT SKU Daily')
    .addItem('📦 Собрать UNIT_SKU_DAILY', 'buildUnitSkuDaily')
    .addToUi();
}


// ═══════════════════════════════════════
// ГЛАВНАЯ ФУНКЦИЯ
// ═══════════════════════════════════════

/**
 * Собирает плоский UNIT_SKU_DAILY из CLEAN_WB_DAILY.
 * Идемпотентно: очищает данные (со 2-й строки) и пишет заново одним setValues.
 */
function buildUnitSkuDaily(options) {
  // PR #15F: optional silent mode — без отдельного UI-alert при вызове из оркестратора.
  options = options || {};
  var silent = options.silent === true;
  var t0 = Date.now();
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var ui = null;  // PR #15F: ленивое получение UI; в silent режиме getUi() не вызывается

  function unitAlert_(title, body) {
    if (silent) return;
    if (!ui) ui = SpreadsheetApp.getUi();
    ui.alert(title, body, ui.ButtonSet.OK);
  }

  console.log('═══ buildUnitSkuDaily() v1.0 (PR #15B) СТАРТ ═══');

  // ── 1. CLEAN_WB_DAILY (источник, только чтение) ──
  var cleanSheet = ss.getSheetByName(UNIT_SKU_CLEAN_SHEET_);
  if (!cleanSheet || cleanSheet.getLastRow() < 2) {
    unitAlert_('❌ Ошибка', UNIT_SKU_CLEAN_SHEET_ + ' пуст или не найден.');
    return { ok: false, error: 'CLEAN пуст/не найден' };
  }

  var cH = getHeaderMap_(cleanSheet);
  var cDateCol    = findCol_(cH, ['date']);
  var cNmCol      = findCol_(cH, ['wb_nm_id', 'nmid', 'nm_id']);
  var cSkuCol     = findCol_(cH, ['internal_sku']);
  var cNameCol    = findCol_(cH, ['product_name']);
  var cOrdersCol  = findCol_(cH, ['orders_qty']);
  var cCancelCol  = findCol_(cH, ['cancel_qty']);
  var cActiveCol  = findCol_(cH, ['active_orders_qty']);
  var cSalesCol   = findCol_(cH, ['sales_qty']);
  var cReturnsCol = findCol_(cH, ['returns_qty']);
  var cNetSalesCol= findCol_(cH, ['net_sales_qty']);
  var cGrossCol   = findCol_(cH, ['gross_revenue']);
  var cRetAmtCol  = findCol_(cH, ['returns_amount']);
  var cNetRevCol  = findCol_(cH, ['net_revenue']);
  var cAdsCol     = findCol_(cH, ['ads_spend']);
  var cStockCol   = findCol_(cH, ['stock_qty']);   // PR #16D: остатки из CLEAN

  if (cDateCol === -1 || cNmCol === -1) {
    unitAlert_('❌ Ошибка', 'В CLEAN нет колонки date или wb_nm_id.');
    return { ok: false, error: 'нет date/wb_nm_id в CLEAN' };
  }
  if (cCancelCol === -1 || cActiveCol === -1) {
    console.log('⚠️ В CLEAN нет cancel_qty/active_orders_qty (PR #15A?). ' +
      'cancel_qty=0, active=orders-cancel как fallback.');
  }

  var cleanData = readSheetData_(cleanSheet);
  console.log('  CLEAN: ' + cleanData.length + ' строк прочитано');

  // ── 2. Трансформация строк (1:1 по гранулярности CLEAN) ──
  var fn_normNm = (typeof normalizeNmIdFinance_ === 'function')
    ? normalizeNmIdFinance_
    : function (v) { return String(v == null ? '' : v).trim().replace(/\.0+$/, ''); };

  var outObjs = [];
  var skippedNoNm = 0, skippedNoDate = 0;

  // контрольные суммы для сверки UNIT ↔ CLEAN
  var sumOrders = 0, sumCancel = 0, sumSales = 0, sumReturns = 0;
  // PR #16D: контроль остатков
  var cntStockData = 0, cntOutOfStock = 0, cntLowStock = 0;

  for (var i = 0; i < cleanData.length; i++) {
    var row = cleanData[i];

    var dateKey = normalizeDateKey_(row[cDateCol]);
    if (!dateKey) { skippedNoDate++; continue; }

    var nm = fn_normNm(row[cNmCol]);
    if (!nm) { skippedNoNm++; continue; }   // кабинетные/технические строки без SKU — пропуск (req #9)

    var orders  = unitNum_(cOrdersCol  >= 0 ? row[cOrdersCol]  : 0);
    var cancel  = unitNum_(cCancelCol  >= 0 ? row[cCancelCol]  : 0);
    var active  = (cActiveCol >= 0) ? unitNum_(row[cActiveCol]) : (orders - cancel);
    var sales   = unitNum_(cSalesCol   >= 0 ? row[cSalesCol]   : 0);
    var returns = unitNum_(cReturnsCol >= 0 ? row[cReturnsCol] : 0);
    var netSales= unitNum_(cNetSalesCol>= 0 ? row[cNetSalesCol]: 0);
    var gross   = unitNum_(cGrossCol   >= 0 ? row[cGrossCol]   : 0);
    var retAmt  = unitNum_(cRetAmtCol  >= 0 ? row[cRetAmtCol]  : 0);
    var netRev  = unitNum_(cNetRevCol  >= 0 ? row[cNetRevCol]  : 0);
    var ads     = unitNum_(cAdsCol     >= 0 ? row[cAdsCol]     : 0);

    // PR #16D: остатки и статус. Пустая ячейка ≠ 0 → no_stock_data.
    var stockRaw = (cStockCol >= 0) ? row[cStockCol] : '';
    var stockQtyOut, stockStatus;
    if (cStockCol < 0 || unitIsBlank_(stockRaw)) {
      stockQtyOut = '';                 // нет данных — оставляем пусто, не 0
      stockStatus = 'no_stock_data';
    } else {
      var stockQty = unitNum_(stockRaw);
      stockQtyOut = stockQty;
      if (stockQty <= 0)       stockStatus = 'out_of_stock';
      else if (stockQty <= 20) stockStatus = 'low_stock';
      else                     stockStatus = 'ok';
    }
    if (stockStatus !== 'no_stock_data') cntStockData++;
    if (stockStatus === 'out_of_stock')  cntOutOfStock++;
    if (stockStatus === 'low_stock')     cntLowStock++;

    // коэффициенты
    var cancelRate = orders > 0 ? roundTwo_(cancel / orders * 100) : 0;
    var buyoutRate = active > 0 ? roundTwo_(sales / active * 100) : 0;   // база — active (PR #15B п.7)
    var returnRate = sales  > 0 ? roundTwo_(returns / sales * 100) : 0;

    sumOrders += orders; sumCancel += cancel; sumSales += sales; sumReturns += returns;

    outObjs.push({
      'date': dateKey,
      'internal_sku': cSkuCol >= 0 ? String(row[cSkuCol] || '') : '',
      'product_name': cNameCol >= 0 ? String(row[cNameCol] || '') : '',
      'row_key': dateKey + '|' + nm,
      'orders_qty': orders,
      'cancel_qty': cancel,
      'active_orders_qty': active,
      'order_cancel_rate_percent': cancelRate,
      'sales_qty': sales,
      'returns_qty': returns,
      'net_sales_qty': netSales,
      'buyout_rate_percent': buyoutRate,
      'return_rate_percent': returnRate,
      'gross_revenue': roundTwo_(gross),
      'returns_amount': roundTwo_(retAmt),
      'net_revenue': roundTwo_(netRev),
      'ads_spend': roundTwo_(ads),
      'stock_qty': stockQtyOut,
      'stock_status': stockStatus
    });
  }

  console.log('  Подготовлено строк UNIT: ' + outObjs.length +
    ' (пропущено без SKU: ' + skippedNoNm + ', без даты: ' + skippedNoDate + ')');

  // ── 3. Лист UNIT_SKU_DAILY: создать/обеспечить шапку (append-only) ──
  var unitSheet = ss.getSheetByName(UNIT_SKU_DAILY_SHEET_);
  if (!unitSheet) {
    unitSheet = ss.insertSheet(UNIT_SKU_DAILY_SHEET_);
    console.log('  UNIT_SKU_DAILY не найден — создан новый лист.');
  }
  ensureUnitColumns_(unitSheet);   // дописывает недостающие FILL-колонки в конец

  // финальная шапка → карта имя→0-based индекс
  var lastCol = unitSheet.getLastColumn();
  var header = unitSheet.getRange(1, 1, 1, lastCol).getValues()[0];
  var colIdx = {};
  for (var h = 0; h < header.length; h++) {
    var key = String(header[h] || '').trim().toLowerCase();
    if (key) colIdx[key] = h;
  }

  // ── 4. Матрица для записи (ширина = текущее число колонок) ──
  var matrix = [];
  for (var r = 0; r < outObjs.length; r++) {
    var arr = new Array(lastCol);
    for (var c = 0; c < lastCol; c++) arr[c] = '';   // незаполняемые колонки остаются пустыми
    var obj = outObjs[r];
    for (var name in obj) {
      var idx = colIdx[name];
      if (idx !== undefined && idx < lastCol) arr[idx] = obj[name];
    }
    matrix.push(arr);
  }

  // ── 5. Очистка данных (со 2-й строки) + запись одним setValues (идемпотентно) ──
  var existingLastRow = unitSheet.getLastRow();
  if (existingLastRow >= 2) {
    unitSheet.getRange(2, 1, existingLastRow - 1, lastCol).clearContent();
  }
  if (matrix.length > 0) {
    var needRows = matrix.length + 1;
    if (unitSheet.getMaxRows() < needRows) {
      unitSheet.insertRowsAfter(unitSheet.getMaxRows(), needRows - unitSheet.getMaxRows());
    }
    unitSheet.getRange(2, 1, matrix.length, lastCol).setValues(matrix);
  }
  SpreadsheetApp.flush();

  // ── 6. Контроль (сверка сумм UNIT ↔ CLEAN) ──
  console.log('══ Контроль UNIT_SKU_DAILY (PR #15B) ══');
  console.log('  строк записано: ' + matrix.length);
  console.log('  Σorders_qty:  ' + sumOrders);
  console.log('  Σcancel_qty:  ' + sumCancel);
  console.log('  Σsales_qty:   ' + sumSales);
  console.log('  Σreturns_qty: ' + sumReturns);
  console.log('  Σstock_data:  ' + cntStockData +
    ' (out_of_stock: ' + cntOutOfStock + ', low_stock: ' + cntLowStock + ')');
  console.log('  (суммы должны совпадать с CLEAN_WB_DAILY за тот же набор дат)');
  console.log('═══ buildUnitSkuDaily() ЗАВЕРШЕНО: ' + sec_(t0) + ' сек ═══');

  unitAlert_('📦 UNIT_SKU_DAILY собран',
    'Строк: ' + matrix.length +
    '\nПропущено без SKU: ' + skippedNoNm +
    '\n\n── Контроль (= должно сходиться с CLEAN) ──' +
    '\norders_qty: ' + sumOrders +
    '\ncancel_qty: ' + sumCancel +
    '\nsales_qty: ' + sumSales +
    '\nreturns_qty: ' + sumReturns +
    '\n\n── Остатки (PR #16D) ──' +
    '\nстрок со stock data: ' + cntStockData +
    '\nout_of_stock: ' + cntOutOfStock +
    '\nlow_stock: ' + cntLowStock +
    '\n\nВремя: ' + sec_(t0) + ' сек');

  return {
    ok: true, rows: matrix.length, skippedNoNm: skippedNoNm,
    orders: sumOrders, cancel: sumCancel, sales: sumSales, returns: sumReturns
  };
}


// ═══════════════════════════════════════
// ВСПОМОГАТЕЛЬНЫЕ (локальные для этого файла)
// ═══════════════════════════════════════

/** Число из ячейки (пустое/мусор → 0). */
function unitNum_(v) {
  var n = Number(v);
  return isNaN(n) ? 0 : n;
}

/** Пустая ячейка (null/undefined/''/пробелы). ВАЖНО: число 0 НЕ считается пустым. */
function unitIsBlank_(v) {
  if (v === null || v === undefined) return true;
  if (typeof v === 'string') return v.trim() === '';
  return false;
}

/**
 * Обеспечивает наличие всех UNIT_SKU_FILL_COLS_ в шапке листа.
 * - Пустой/новый лист (нет реальной шапки) → пишет FILL-колонки в строку 1.
 * - Иначе → дописывает ТОЛЬКО недостающие колонки в КОНЕЦ (append-only).
 * Существующие колонки не удаляет и не переставляет.
 */
function ensureUnitColumns_(sheet) {
  var lastCol = sheet.getLastColumn();
  var header = lastCol >= 1 ? sheet.getRange(1, 1, 1, lastCol).getValues()[0] : [];

  var existing = {};
  var realHeaderCount = 0;
  for (var i = 0; i < header.length; i++) {
    var k = String(header[i] || '').trim().toLowerCase();
    if (k) { existing[k] = true; realHeaderCount++; }
  }

  // Случай 1: реальной шапки нет — создаём с нуля
  if (realHeaderCount === 0) {
    if (sheet.getMaxColumns() < UNIT_SKU_FILL_COLS_.length) {
      sheet.insertColumnsAfter(sheet.getMaxColumns(),
        UNIT_SKU_FILL_COLS_.length - sheet.getMaxColumns());
    }
    sheet.getRange(1, 1, 1, UNIT_SKU_FILL_COLS_.length).setValues([UNIT_SKU_FILL_COLS_]);
    sheet.setFrozenRows(1);
    SpreadsheetApp.flush();
    console.log('  Шапка UNIT создана с нуля: ' + UNIT_SKU_FILL_COLS_.length + ' колонок');
    return;
  }

  // Случай 2: шапка есть — дописываем недостающее в конец
  var toAppend = [];
  for (var j = 0; j < UNIT_SKU_FILL_COLS_.length; j++) {
    var name = UNIT_SKU_FILL_COLS_[j];
    if (!existing[name] && toAppend.indexOf(name) === -1) toAppend.push(name);
  }
  if (toAppend.length === 0) {
    console.log('  Шапка UNIT: все нужные колонки уже есть');
    return;
  }
  var need = lastCol + toAppend.length;
  if (sheet.getMaxColumns() < need) {
    sheet.insertColumnsAfter(sheet.getMaxColumns(), need - sheet.getMaxColumns());
  }
  sheet.getRange(1, lastCol + 1, 1, toAppend.length).setValues([toAppend]);
  SpreadsheetApp.flush();
  console.log('  Шапка UNIT: дописаны append-only колонки: ' + toAppend.join(', '));
}
