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
  Logger.log('byNm keys (first 5): ' + Object.keys(skuIndex.byNm).slice(0, 5).join(', '));
  Logger.log('byNm[' + testNmId + '] = ' + JSON.stringify(skuIndex.byNm[testNmId] ? skuIndex.byNm[testNmId].internal_sku : 'NOT FOUND'));
  
  var firstKey = Object.keys(skuIndex.byNm)[0];
  Logger.log('First key: "' + firstKey + '"');
  Logger.log('Looks numeric: ' + /^\d+$/.test(firstKey));
}


/**
 * ──────────────────────────────────────────────────────────────
 * Единый HTTP-вызов WB API с устойчивостью к лимитам и сбоям.
 * Ретраит на HTTP 429 и 5xx (500/502/503/504), уважает Retry-After,
 * экспоненциальный backoff с верхней границей, ограничение по числу повторов.
 *
 * Возвращает HTTPResponse, СОВМЕСТИМЫЙ с UrlFetchApp.fetch:
 *   .getResponseCode(), .getContentText(), .getHeaders() и т.д.
 * Поэтому вызывающий код почти не меняется.
 *
 * Токен (заголовок Authorization) и тело options НЕ логируются.
 *
 * @param {string} url
 * @param {Object} options  опции UrlFetchApp.fetch (method, headers, payload, contentType…)
 * @param {Object} [retryOptions]
 *   maxRetries  {number}  число ПОВТОРОВ после первой попытки (по умолч. 4)
 *   baseDelayMs {number}  базовая пауза backoff, мс (по умолч. 1500)
 *   maxDelayMs  {number}  верхняя граница паузы, мс (по умолч. 60000)
 *   retryCodes  {number[]} коды для повтора (по умолч. [429,500,502,503,504])
 *   label       {string}  метка для логов (напр. 'Orders', 'WB_ADS')
 * @return {HTTPResponse} последний полученный ответ
 */
function wbFetchWithRetry_(url, options, retryOptions) {
  var ro = retryOptions || {};
  var maxRetries = (ro.maxRetries != null) ? ro.maxRetries : 4;
  var baseDelay  = (ro.baseDelayMs != null) ? ro.baseDelayMs : 1500;
  var maxDelay   = (ro.maxDelayMs != null) ? ro.maxDelayMs : 60000;
  var retryCodes = ro.retryCodes || [429, 500, 502, 503, 504];
  var label      = ro.label || 'WB';

  // muteHttpExceptions обязателен: иначе не-2xx бросает исключение, и код ответа не прочитать.
  var opt = {};
  if (options) { for (var k in options) { if (options.hasOwnProperty(k)) opt[k] = options[k]; } }
  opt.muteHttpExceptions = true;

  var safeUrl = String(url).split('?')[0]; // логируем без query-параметров
  var lastResp = null;
  var attempt = 0;

  while (true) {
    attempt++;
    var resp = null, code = 0, threw = false, errMsg = '';
    try {
      resp = UrlFetchApp.fetch(url, opt);
      code = resp.getResponseCode();
    } catch (e) {
      threw = true; errMsg = (e && e.message) ? e.message : String(e);
    }

    if (!threw) {
      lastResp = resp;
      if (retryCodes.indexOf(code) === -1) return resp;  // успех или неретраябельный код
    }

    if (attempt > maxRetries) {
      if (threw) {
        throw new Error('[' + label + '] HTTP-исключение после ' + attempt + ' попыток: ' + errMsg);
      }
      console.log('  [' + label + '] HTTP ' + code + ' — повторы исчерпаны (' +
        attempt + '/' + (maxRetries + 1) + '), ' + safeUrl);
      return lastResp;
    }

    // Пауза: приоритет у Retry-After, иначе экспоненциальный backoff.
    var pause = (!threw && resp) ? wbRetryAfterMs_(resp) : -1;
    if (pause < 0) pause = baseDelay * Math.pow(2, attempt - 1);
    pause = Math.min(maxDelay, pause);

    var reason = threw ? ('исключение: ' + errMsg) : ('HTTP ' + code);
    console.log('  [' + label + '] ' + reason + ' — пауза ' + Math.round(pause / 1000) +
      ' c, повтор ' + (attempt + 1) + '/' + (maxRetries + 1) + ' (' + safeUrl + ')');
    Utilities.sleep(pause);
  }
}

/**
 * Разбор заголовка Retry-After: число секунд или HTTP-дата.
 * @return {number} миллисекунды ожидания, либо -1 если заголовка нет/не распознан.
 */
function wbRetryAfterMs_(resp) {
  try {
    var headers = (resp && resp.getHeaders) ? (resp.getHeaders() || {}) : {};
    var ra = headers['Retry-After'] || headers['retry-after'] || '';
    if (!ra) return -1;
    ra = String(ra).trim();
    if (/^\d+$/.test(ra)) return parseInt(ra, 10) * 1000;   // секунды
    var when = Date.parse(ra);                              // HTTP-дата
    if (!isNaN(when)) { var ms = when - Date.now(); return ms > 0 ? ms : 0; }
  } catch (e) {}
  return -1;
}
