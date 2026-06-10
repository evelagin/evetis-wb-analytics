/**
 * ══════════════════════════════════════════════════════════════
 * EVETIS WB — WbApiTestUtils.gs  v1.0
 *
 * Безопасные утилиты тестового контура WB API.
 * Только: чтение токенов из Script Properties, HTTP-запросы,
 * task-based поток, сохранение raw JSON в Drive, контрольные суммы,
 * read-only чтение SKU_MASTER, сборка summary.
 *
 * НЕ пишет в боевые листы. НЕ логирует и НЕ сохраняет токены.
 * НЕ вызывает production import-функции.
 * ══════════════════════════════════════════════════════════════
 */


// ═══════════════════════════════════════
// ПРЕДОХРАНИТЕЛЬ
// ═══════════════════════════════════════

/** Бросает исключение, если WB_API_TEST_MODE !== true. */
function assertWbApiTestMode_() {
  if (typeof WB_API_TEST_MODE === 'undefined' || WB_API_TEST_MODE !== true) {
    throw new Error('WB_API_TEST_MODE !== true — тестовый раннер заблокирован.');
  }
}

/** Безопасный лог (никогда не принимает токен). */
function wbApiTestLog_(msg) {
  console.log('[WB_API_TEST] ' + msg);
}


// ═══════════════════════════════════════
// PROPERTIES / ТОКЕНЫ
// ═══════════════════════════════════════

/** Чтение Script Property без падения. */
function wbApiTestProp_(key) {
  try {
    return PropertiesService.getScriptProperties().getProperty(key) || '';
  } catch (e) {
    return '';
  }
}

/**
 * Возвращает токен по категории.
 * @return {Object} { category, present, token } — token используется
 *   только внутри запроса, наружу/в лог/в JSON не отдаётся.
 */
function wbApiTestGetToken_(category) {
  var key = WB_API_TEST_TOKEN_KEYS_[category] || '';
  var token = key ? wbApiTestProp_(key) : '';
  return { category: category, present: !!token, token: token };
}

/** Лог-безопасное описание токена (без значения). */
function wbApiTestTokenInfo_(category, present) {
  wbApiTestLog_('token category: ' + category + '; token present: ' + (present ? 'yes' : 'no'));
}


// ═══════════════════════════════════════
// DRIVE: ПАПКА РЕЗУЛЬТАТОВ
// ═══════════════════════════════════════

/**
 * Возвращает Drive-папку результатов по property
 * WB_API_TEST_RESULTS_FOLDER_ID. Если property нет / папка
 * недоступна — возвращает null (тест получит BLOCKED).
 */
function wbApiTestGetResultsFolder_() {
  var folderId = wbApiTestProp_(WB_API_TEST_RESULTS_FOLDER_PROP_);
  if (!folderId) {
    wbApiTestLog_('Нет property ' + WB_API_TEST_RESULTS_FOLDER_PROP_ + ' → BLOCKED');
    return null;
  }
  try {
    return DriveApp.getFolderById(folderId);
  } catch (e) {
    wbApiTestLog_('Папка результатов недоступна: ' + e.message + ' → BLOCKED');
    return null;
  }
}

/**
 * Пишет JSON-файл в папку результатов. Историю прогонов НЕ удаляет
 * (имена содержат timestamp — коллизий нет).
 * @return {string} file id или '' при ошибке.
 */
function wbApiTestSaveJson_(folder, fileName, obj) {
  if (!folder) return '';
  try {
    var content = JSON.stringify(obj, null, 2);
    var file = folder.createFile(fileName, content, 'application/json');
    wbApiTestLog_('Сохранено: ' + fileName + ' (' + content.length + ' символов)');
    return file.getId();
  } catch (e) {
    wbApiTestLog_('Ошибка сохранения ' + fileName + ': ' + e.message);
    return '';
  }
}

/** Пишет текстовый .md-файл в папку результатов. Историю не удаляет. */
function wbApiTestSaveText_(folder, fileName, text) {
  if (!folder) return '';
  try {
    var file = folder.createFile(fileName, text, 'text/markdown');
    wbApiTestLog_('Сохранено: ' + fileName);
    return file.getId();
  } catch (e) {
    wbApiTestLog_('Ошибка сохранения ' + fileName + ': ' + e.message);
    return '';
  }
}


// ═══════════════════════════════════════
// HTTP
// ═══════════════════════════════════════

/**
 * Безопасный HTTP-запрос. Токен подставляется в Authorization,
 * но НЕ возвращается и НЕ логируется.
 * @return {Object} { code, body, json, ok }
 */
function wbApiTestHttp_(method, url, token, payload) {
  var attempt = 0;
  while (true) {
    attempt++;
    var options = {
      method: method,
      headers: { 'Authorization': token },
      muteHttpExceptions: true
    };
    if (payload !== undefined && payload !== null) {
      options.contentType = 'application/json';
      options.payload = JSON.stringify(payload);
    }

    var resp;
    try {
      resp = UrlFetchApp.fetch(url, options);
    } catch (e) {
      return { code: 0, body: '', json: null, ok: false, error: e.message };
    }
    var code = resp.getResponseCode();
    var body = resp.getContentText();

    if (code === 429 && attempt <= WB_API_TEST_RETRY_429_) {
      wbApiTestLog_('HTTP 429, пауза ' + (WB_API_TEST_RETRY_429_PAUSE_MS_ / 1000) +
        ' c (попытка ' + attempt + ')');
      Utilities.sleep(WB_API_TEST_RETRY_429_PAUSE_MS_);
      continue;
    }

    var json = null;
    try { json = JSON.parse(body); } catch (e) { json = null; }
    return { code: code, body: body, json: json, ok: (code >= 200 && code < 300) };
  }
}

/**
 * Task-based поток: create → status → download.
 * createUrl — полный URL создания; taskBaseUrl — база для
 * /tasks/{id}/status и /tasks/{id}/download.
 * @return {Object} { ok, taskId, statusLast, data, error }
 */
function wbApiTestRunTask_(token, createUrl, taskBaseUrl) {
  var out = { ok: false, taskId: '', statusLast: '', data: null, error: '' };

  var cr = wbApiTestHttp_('get', createUrl, token, null);
  if (!cr.ok) { out.error = 'create HTTP ' + cr.code + ': ' + cr.body.substring(0, 200); return out; }

  var taskId = '';
  if (cr.json && cr.json.data && cr.json.data.taskId) taskId = String(cr.json.data.taskId);
  else if (cr.json && cr.json.data && cr.json.data.id) taskId = String(cr.json.data.id);
  else if (cr.json && cr.json.taskId) taskId = String(cr.json.taskId);
  else if (cr.json && cr.json.task_id) taskId = String(cr.json.task_id);
  else if (cr.json && cr.json.id) taskId = String(cr.json.id);
  if (!taskId) { out.error = 'Нет taskId в ответе create'; return out; }
  out.taskId = taskId;

  var ready = false;
  for (var p = 0; p < WB_API_TEST_MAX_POLLS_; p++) {
    Utilities.sleep(WB_API_TEST_POLL_INTERVAL_MS_);
    var sr = wbApiTestHttp_('get', taskBaseUrl + '/tasks/' + taskId + '/status', token, null);
    var st = '';
    if (sr.json && sr.json.data && sr.json.data.status) st = sr.json.data.status;
    else if (sr.json && sr.json.status) st = sr.json.status;
    st = String(st || '').toLowerCase();
    out.statusLast = st;
    wbApiTestLog_('task ' + taskId + ' опрос ' + (p + 1) + '/' + WB_API_TEST_MAX_POLLS_ + ': ' + st);
    if (st === 'done' || st === 'ready' || st === 'completed' || st === 'success') { ready = true; break; }
    if (st === 'purged' || st === 'canceled' || st === 'cancelled' || st === 'failed' || st === 'error') {
      out.error = 'Задача ' + st; return out;
    }
  }
  if (!ready) { out.error = 'Таймаут ожидания задачи'; return out; }

  var dl = wbApiTestHttp_('get', taskBaseUrl + '/tasks/' + taskId + '/download', token, null);
  if (!dl.ok) { out.error = 'download HTTP ' + dl.code + ': ' + dl.body.substring(0, 200); return out; }
  out.data = dl.json;
  out.ok = true;
  return out;
}


// ═══════════════════════════════════════
// SKU_MASTER (read-only) — для unmatched nmId
// ═══════════════════════════════════════

/**
 * Читает множество nmId из SKU_MASTER (только чтение).
 * @return {Object} { set:{nmId:true}, count }
 */
function wbApiTestLoadSkuNmIds_() {
  var res = { set: {}, count: 0 };
  try {
    var ss = SpreadsheetApp.getActiveSpreadsheet();
    var sheet = ss.getSheetByName(WB_API_TEST_SKU_MASTER_SHEET_);
    if (!sheet || sheet.getLastRow() < 2) return res;

    var lastCol = sheet.getLastColumn();
    var headers = sheet.getRange(1, 1, 1, lastCol).getValues()[0];
    var nmCol = -1;
    for (var c = 0; c < headers.length; c++) {
      var h = String(headers[c] || '').trim().toLowerCase();
      for (var k = 0; k < WB_API_TEST_SKU_NMID_HEADERS_.length; k++) {
        if (h === WB_API_TEST_SKU_NMID_HEADERS_[k].toLowerCase()) { nmCol = c; break; }
      }
      if (nmCol >= 0) break;
    }
    if (nmCol < 0) return res;

    var col = sheet.getRange(2, nmCol + 1, sheet.getLastRow() - 1, 1).getValues();
    for (var r = 0; r < col.length; r++) {
      var v = wbApiTestNormNmId_(col[r][0]);
      if (v && !res.set[v]) { res.set[v] = true; res.count++; }
    }
  } catch (e) {
    wbApiTestLog_('SKU_MASTER read-only: ' + e.message);
  }
  return res;
}


// ═══════════════════════════════════════
// ПАРСИНГ / ХЕЛПЕРЫ ДЛЯ ЧЕК-СУММ
// ═══════════════════════════════════════

/** Денежное значение из строки/числа (поддержка string-полей нового Finance API). */
function wbApiTestMoney_(v) {
  if (v === null || v === undefined || v === '') return 0;
  if (typeof v === 'number') return v;
  var s = String(v).replace(/\s+/g, '');
  if (s.indexOf('.') === -1 && s.indexOf(',') !== -1) s = s.replace(',', '.');
  else s = s.replace(/,/g, '');
  var n = parseFloat(s);
  return isNaN(n) ? 0 : n;
}

/** Нормализация nmId к строке цифр (нецифровое → ''). */
function wbApiTestNormNmId_(v) {
  if (v === null || v === undefined) return '';
  var s = String(v).trim();
  if (!s) return '';
  s = s.replace(/\.0+$/, '');
  return /^\d+$/.test(s) ? s : '';
}

/** Первое непустое значение по списку имён полей. */
function wbApiTestPick_(obj, names) {
  if (!obj) return undefined;
  for (var i = 0; i < names.length; i++) {
    if (obj[names[i]] !== undefined && obj[names[i]] !== null && obj[names[i]] !== '') return obj[names[i]];
  }
  return undefined;
}

/** Список ключей первого объекта массива (фактические поля). */
function wbApiTestFieldList_(arr) {
  if (!arr || !arr.length || typeof arr[0] !== 'object') return [];
  return Object.keys(arr[0]);
}

/** Первые N элементов массива (для сохранения примеров). */
function wbApiTestFirstRows_(arr, n) {
  if (!arr || !arr.length) return [];
  return arr.slice(0, n || 3);
}

function wbApiTestRound_(v) {
  return Math.round((Number(v) || 0) * 100) / 100;
}


// ═══════════════════════════════════════
// СБОРКА РЕЗУЛЬТАТА ТЕСТА
// ═══════════════════════════════════════

/**
 * Единый объект результата теста.
 * raw сохраняется в Drive в обёртке без токена.
 */
function wbApiTestMakeResult_(testId) {
  var reg = WB_API_TEST_REGISTRY_[testId] || {};
  return {
    testId: testId,
    name: reg.name || testId,
    category: reg.category || '',
    method: reg.method || '',
    taskBased: !!reg.taskBased,
    endpoint: '',
    requestParams: {},   // БЕЗ токена
    httpStatus: null,
    timestamp: Utilities.formatDate(new Date(), 'Europe/Moscow', 'yyyy-MM-dd HH:mm:ss'),
    rowsCount: 0,
    fields: [],
    firstRows: [],
    checksums: {},
    errors: [],
    decision: WB_API_TEST_DECISION_TBD_,
    tokenPresent: false,
    savedFile: ''
  };
}

/**
 * Финализация: сохраняет raw JSON в Drive (если папка есть),
 * пишет компактную запись в результат.
 * rawResponse — полный ответ API (или массив частей).
 */
function wbApiTestFinalize_(folder, result, rawResponse) {
  var stamp = Utilities.formatDate(new Date(), 'Europe/Moscow', 'yyyyMMdd_HHmmss');
  var fileName = wbApiTestFileName_(result.testId, stamp);
  var wrapper = {
    testId: result.testId,
    name: result.name,
    category: result.category,
    method: result.method,
    endpoint: result.endpoint,
    requestParams: result.requestParams,   // токена тут нет
    httpStatus: result.httpStatus,
    timestamp: result.timestamp,
    rowsCount: result.rowsCount,
    fields: result.fields,
    firstRows: result.firstRows,
    checksums: result.checksums,
    errors: result.errors,
    decision: result.decision,
    rawResponse: rawResponse
  };
  result.savedFile = wbApiTestSaveJson_(folder, fileName, wrapper);
  wbApiTestLog_(result.testId + ' → ' + result.decision +
    ' (rows=' + result.rowsCount + ', file=' + (result.savedFile ? 'saved' : 'NOT saved') + ')');
  return result;
}

/** Имя файла результата по тесту (timestamp во всех именах → история прогонов). */
function wbApiTestFileName_(testId, stamp) {
  var p = WB_API_TEST_DATE_FROM_ + '_' + WB_API_TEST_DATE_TO_;
  var map = {
    T1:  'T1_finance_detailed_' + p + '_' + stamp + '.json',
    T2:  'T2_legacy_finance_' + p + '_' + stamp + '.json',
    T3:  'T3_orders_' + p + '_' + stamp + '.json',
    T4:  'T4_sales_' + p + '_' + stamp + '.json',
    T5:  'T5_warehouse_remains_snapshot_' + stamp + '.json',
    T6:  'T6_stocks_report_wb_warehouses_snapshot_' + stamp + '.json',
    T7:  'T7_ads_campaigns_' + stamp + '.json',
    T8:  'T8_ads_fullstats_' + p + '_' + stamp + '.json',
    T9:  'T9_ads_upd_' + p + '_' + stamp + '.json',
    T10: 'T10_paid_storage_' + p + '_' + stamp + '.json',
    T11: 'T11_acceptance_report_' + p + '_' + stamp + '.json'
  };
  return map[testId] || (testId + '_' + stamp + '.json');
}


// ═══════════════════════════════════════
// SUMMARY (.md)
// ═══════════════════════════════════════

/**
 * Строит summary-markdown по списку результатов и сохраняет в Drive.
 * Кросс-сверки (T8↔T9, T10/T11↔finance) считаются здесь, если
 * соответствующие результаты присутствуют.
 */
function wbApiTestBuildSummary_(folder, results) {
  var byId = {};
  for (var i = 0; i < results.length; i++) byId[results[i].testId] = results[i];

  var lines = [];
  lines.push('# WB API Test Summary — ' + WB_API_TEST_DATE_FROM_ + ' … ' + WB_API_TEST_DATE_TO_);
  lines.push('');
  lines.push('> Автоген тестового контура (WbApiTestRunner). Production-код не менялся, ' +
    'боевые листы не затрагивались, raw JSON сохранён только в Drive.');
  lines.push('');
  lines.push('Сгенерировано: ' + Utilities.formatDate(new Date(), 'Europe/Moscow', 'yyyy-MM-dd HH:mm:ss'));
  lines.push('');
  lines.push('| Тест | Назначение | HTTP | Строк | Решение |');
  lines.push('|---|---|---|---|---|');
  for (var j = 0; j < results.length; j++) {
    var r = results[j];
    lines.push('| ' + r.testId + ' | ' + r.name + ' | ' + (r.httpStatus === null ? '—' : r.httpStatus) +
      ' | ' + r.rowsCount + ' | ' + r.decision + ' |');
  }
  lines.push('');

  // Кросс-сверка рекламы T8 ↔ T9
  if (byId.T8 && byId.T9) {
    var sumFull = (byId.T8.checksums && byId.T8.checksums.sumSpend) || 0;
    var sumUpd = (byId.T9.checksums && byId.T9.checksums.sumUpd) || 0;
    var dAds = wbApiTestRound_(Math.abs(sumFull - sumUpd));
    lines.push('## Сверка рекламы (T8 ↔ T9)');
    lines.push('- Σ fullstats.sum = ' + wbApiTestRound_(sumFull) + ' ₽');
    lines.push('- Σ updSum = ' + wbApiTestRound_(sumUpd) + ' ₽');
    lines.push('- |Δ| = ' + dAds + ' ₽ (порог ' + WB_API_TEST_ADS_THRESHOLD_ + ' ₽)');
    lines.push('- Статус: ' + (dAds <= WB_API_TEST_ADS_THRESHOLD_
      ? 'API-сверка T8/T9 сходится; финальный SKU-факт только после сверки с кабинетом WB'
      : 'API-сверка T8/T9 НЕ сходится — реклама остаётся оценкой'));
    lines.push('');
  }

  // Кросс-сверка хранения T10 ↔ finance (T1 предпочт., иначе T2)
  var finStorage = wbApiTestFinanceField_(byId, 'storage');
  if (byId.T10 && finStorage !== null) {
    var sumStor = (byId.T10.checksums && byId.T10.checksums.sumWarehousePrice) || 0;
    lines.push('## Сверка хранения (T10 ↔ finance)');
    lines.push('- Σ warehousePrice (T10) = ' + wbApiTestRound_(sumStor) + ' ₽');
    lines.push('- storage из finance = ' + wbApiTestRound_(finStorage) + ' ₽');
    lines.push('- |Δ| = ' + wbApiTestRound_(Math.abs(sumStor - finStorage)) + ' ₽ (порог ' +
      WB_API_TEST_MONEY_THRESHOLD_ + ' ₽). Канон не закрепляем автоматически.');
    lines.push('');
  }

  // Кросс-сверка приёмки T11 ↔ finance
  var finAcc = wbApiTestFinanceField_(byId, 'acceptance');
  if (byId.T11 && finAcc !== null) {
    var sumAcc = (byId.T11.checksums && byId.T11.checksums.sumTotal) || 0;
    lines.push('## Сверка приёмки (T11 ↔ finance)');
    lines.push('- Σ total (T11) = ' + wbApiTestRound_(sumAcc) + ' ₽');
    lines.push('- acceptance из finance = ' + wbApiTestRound_(finAcc) + ' ₽');
    lines.push('- |Δ| = ' + wbApiTestRound_(Math.abs(sumAcc - finAcc)) + ' ₽ (порог ' +
      WB_API_TEST_MONEY_THRESHOLD_ + ' ₽). Канон не закрепляем автоматически.');
    lines.push('');
  }

  lines.push('---');
  lines.push('*T8/T9 — только API-сверка; финальный SKU-факт рекламы возможен только после сверки с кабинетом WB. ' +
    'COGS в WB API не ищется. Excel/Drive не используется как источник факта.*');

  var stamp = Utilities.formatDate(new Date(), 'Europe/Moscow', 'yyyyMMdd_HHmmss');
  var name = 'wb_api_test_summary_' + WB_API_TEST_DATE_FROM_ + '_' + WB_API_TEST_DATE_TO_ + '_' + stamp + '.md';
  wbApiTestSaveText_(folder, name, lines.join('\n'));

  // Дублируем машинно-читаемый JSON summary
  var jsonName = 'wb_api_test_summary_' + WB_API_TEST_DATE_FROM_ + '_' + WB_API_TEST_DATE_TO_ + '_' + stamp + '.json';
  var compact = [];
  for (var k = 0; k < results.length; k++) {
    compact.push({
      testId: results[k].testId, decision: results[k].decision,
      httpStatus: results[k].httpStatus, rowsCount: results[k].rowsCount,
      checksums: results[k].checksums, errors: results[k].errors
    });
  }
  wbApiTestSaveJson_(folder, jsonName, { generatedAt: stamp, results: compact });
}

/** Достаёт finance-поле (storage/acceptance) из T1, иначе T2. */
function wbApiTestFinanceField_(byId, key) {
  if (byId.T1 && byId.T1.checksums && typeof byId.T1.checksums[key] === 'number') return byId.T1.checksums[key];
  if (byId.T2 && byId.T2.checksums && typeof byId.T2.checksums[key] === 'number') return byId.T2.checksums[key];
  return null;
}
