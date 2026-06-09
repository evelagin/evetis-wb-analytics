/**
 * ══════════════════════════════════════════════════════════════
 * EVETIS WB — Utils.gs
 * Вспомогательные функции
 * ══════════════════════════════════════════════════════════════
 */

/**
 * Получить или создать лист по имени.
 * Если лист существует — возвращает его.
 * Если нет — создаёт новый.
 */
function getOrCreateSheet_(ss, sheetName) {
  var sheet = ss.getSheetByName(sheetName);
  if (!sheet) {
    sheet = ss.insertSheet(sheetName);
    Logger.log('✅ Создан лист: ' + sheetName);
  } else {
    Logger.log('ℹ️ Лист уже существует: ' + sheetName);
  }
  return sheet;
}

/**
 * Безопасное удаление стандартного листа "Лист1" / "Sheet1"
 */
function removeDefaultSheet_(ss) {
  var defaultNames = ['Лист1', 'Sheet1', 'Лист 1', 'Sheet 1'];
  for (var i = 0; i < defaultNames.length; i++) {
    var sheet = ss.getSheetByName(defaultNames[i]);
    if (sheet && ss.getSheets().length > 1) {
      ss.deleteSheet(sheet);
      Logger.log('🗑️ Удалён стандартный лист: ' + defaultNames[i]);
    }
  }
}

/**
 * Логирование с временной меткой
 */
function log_(message) {
  var timestamp = Utilities.formatDate(new Date(), 'Europe/Moscow', 'HH:mm:ss');
  Logger.log('[' + timestamp + '] ' + message);
}


/**
 * Записать ошибки остатков в ERRORS_CONTROL.
 * Формат errors: [{ source, sku, severity, type, message, nmId, barcode, vendor }]
 */
function writeStocksErrors_(ss, errors) {
  if (!errors || errors.length === 0) return;
  
  var sheet = ss.getSheetByName('ERRORS_CONTROL');
  if (!sheet) {
    Logger.log('⚠️ writeStocksErrors_: лист ERRORS_CONTROL не найден');
    return;
  }
  
  var now = Utilities.formatDate(new Date(), 'Europe/Moscow', 'yyyy-MM-dd HH:mm:ss');
  var rows = [];
  
  for (var i = 0; i < errors.length; i++) {
    var e = errors[i];
    var errId = 'ERR-STK-' + now.replace(/[^0-9]/g, '').substring(0, 12) + '-' + (i + 1);
    rows.push([
      errId,                           // error_id
      now,                             // detected_at
      e.source || 'RAW_WB_STOCKS',    // source_sheet
      '',                              // source_row
      e.type || 'unknown',            // error_type
      e.severity || 'warning',        // severity
      e.sku || '',                    // internal_sku
      e.nmId || '',                   // wb_nm_id
      e.message || '',                // error_message
      '',                              // expected_value
      '',                              // actual_value
      'Нет',                           // resolved
      '',                              // resolved_at
      '',                              // resolved_by
      ''                               // comment
    ]);
  }
  
  if (rows.length > 0) {
    var startRow = Math.max(sheet.getLastRow() + 1, 2);
    sheet.getRange(startRow, 1, rows.length, 15).setValues(rows);
    Logger.log('📝 writeStocksErrors_: записано ' + rows.length + ' ошибок в ERRORS_CONTROL');
  }
}

/**
 * Форматирование числа с разделителями
 */
function fmtNum_(num) {
  if (!num && num !== 0) return '';
  return num.toString().replace(/\B(?=(\d{3})+(?!\d))/g, ' ');
}

/**
 * Безопасное получение значения из Script Properties
 */
function getScriptProperty_(key, fallback) {
  try {
    var val = PropertiesService.getScriptProperties().getProperty(key);
    return val || fallback || '';
  } catch (e) {
    return fallback || '';
  }
}

/**
 * Сохранение значения в Script Properties
 */
function setScriptProperty_(key, value) {
  PropertiesService.getScriptProperties().setProperty(key, String(value));
}


function debugSkuMatching() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var skuIndex = buildSkuIndex_(ss);
  
  var testNmId = '252442341';
  Logger.log('=== DEBUG SKU MATCHING v2 ===');
  Logger.log('byNmId keys (first 5): ' + Object.keys(skuIndex.byNmId).slice(0, 5).join(', '));
  Logger.log('byNmId[' + testNmId + '] = ' + JSON.stringify(skuIndex.byNmId[testNmId] ? skuIndex.byNmId[testNmId].internal_sku : 'NOT FOUND'));
  
  var firstKey = Object.keys(skuIndex.byNmId)[0];
  Logger.log('First key: "' + firstKey + '"');
  Logger.log('Looks numeric: ' + /^\d+$/.test(firstKey));
}
