/**
 * ══════════════════════════════════════════════════════════════
 * EVETIS WB — WbStorageApiDiag.gs  v1.0
 *
 * Диагностический патч для API хранения WB.
 * Выявляет правильное имя поля суммы хранения в JSON-ответе.
 *
 * НЕ ТРОГАЕТ: buildMonthlyUnitReportV10, ЮНИТ, Drive Folder Loader,
 *   CLEAN_WB_DAILY, RAW_WB_FINANCE, RAW_WB_ADS, RAW_WB_STORAGE данные.
 *
 * ЗАВИСИМОСТИ:
 *   getScriptProperty_(key)
 *   pad2_(n), sec_(tStart)
 *   normalizeStorageDate_(val)
 *   WB_STORAGE_API_BASE_, WB_STORAGE_TOKEN_KEY_,
 *   WB_STORAGE_API_MAX_POLLS_, WB_STORAGE_API_POLL_INTERVAL_
 *   (из WbStorageFolderLoader.gs)
 *
 * ══════════════════════════════════════════════════════════════
 */


// ═══════════════════════════════════════
// ЗАДАЧА 1: АУДИТ ОТВЕТА API
// ═══════════════════════════════════════

/**
 * Скачивает JSON из WB Paid Storage API и подробно логирует структуру.
 *
 * Не пишет в RAW_WB_STORAGE — только диагностика.
 *
 * @param {string} dateFrom — "YYYY-MM-DD"
 * @param {string} dateTo — "YYYY-MM-DD"
 */
function auditWbStorageApiResponse(dateFrom, dateTo) {
  var t0 = Date.now();
  console.log('═══════════════════════════════════════════════════');
  console.log('  AUDIT WB STORAGE API: ' + dateFrom + ' — ' + dateTo);
  console.log('═══════════════════════════════════════════════════');

  // ── Токен ──
  var token = getScriptProperty_(WB_STORAGE_TOKEN_KEY_);
  if (!token) {
    console.log('❌ Нет токена: ' + WB_STORAGE_TOKEN_KEY_);
    // Пробуем альтернативный ключ
    token = getScriptProperty_('WB_TOKEN_ANALYTICS');
    if (!token) {
      console.log('❌ Нет токена: WB_TOKEN_ANALYTICS');
      return;
    }
    console.log('  ✅ Используем WB_TOKEN_ANALYTICS');
  } else {
    console.log('  ✅ Используем ' + WB_STORAGE_TOKEN_KEY_);
  }

  // ── 1. Создать задачу ──
  var createUrl = WB_STORAGE_API_BASE_ + '/api/v1/paid_storage?dateFrom=' +
    dateFrom + '&dateTo=' + dateTo;
  console.log('  URL: ' + createUrl);

  var createResp = UrlFetchApp.fetch(createUrl, {
    method: 'get',
    headers: { 'Authorization': token },
    muteHttpExceptions: true
  });

  var createCode = createResp.getResponseCode();
  var createBody = createResp.getContentText();
  console.log('  HTTP: ' + createCode);
  console.log('  Body (первые 500): ' + createBody.substring(0, 500));

  if (createCode !== 200) {
    console.log('❌ Не удалось создать задачу');
    return;
  }

  var createData;
  try { createData = JSON.parse(createBody); } catch (e) {
    console.log('❌ JSON parse error: ' + e.message);
    return;
  }

  console.log('  Ответ создания (ключи): ' + Object.keys(createData).join(', '));
  console.log('  Ответ создания (JSON): ' + JSON.stringify(createData).substring(0, 300));

  var taskId = '';
  if (createData.data && createData.data.taskId) taskId = String(createData.data.taskId);
  else if (createData.taskId) taskId = String(createData.taskId);
  else taskId = String(createBody).replace(/[^a-zA-Z0-9\-]/g, '').substring(0, 50);

  console.log('  taskId: ' + taskId);
  if (!taskId) { console.log('❌ Нет taskId'); return; }

  // ── 2. Опрос статуса ──
  var statusUrl = WB_STORAGE_API_BASE_ + '/api/v1/paid_storage/tasks/' + taskId + '/status';
  var reportReady = false;

  for (var poll = 0; poll < WB_STORAGE_API_MAX_POLLS_; poll++) {
    Utilities.sleep(WB_STORAGE_API_POLL_INTERVAL_);
    var statusResp = UrlFetchApp.fetch(statusUrl, {
      method: 'get',
      headers: { 'Authorization': token },
      muteHttpExceptions: true
    });
    var statusBody = statusResp.getContentText();
    var statusData;
    try { statusData = JSON.parse(statusBody); } catch (e) { statusData = {}; }

    var taskStatus = '';
    if (statusData.data && statusData.data.status) taskStatus = statusData.data.status;
    else if (statusData.status) taskStatus = statusData.status;

    console.log('  Опрос ' + (poll + 1) + ': HTTP ' + statusResp.getResponseCode() + ', status=' + taskStatus);
    console.log('    body: ' + statusBody.substring(0, 200));

    if (taskStatus === 'done') { reportReady = true; break; }
    if (taskStatus === 'purged' || taskStatus === 'canceled') {
      console.log('❌ Задача ' + taskStatus); return;
    }
  }

  if (!reportReady) { console.log('❌ Таймаут'); return; }

  // ── 3. Скачать отчёт ──
  var downloadUrl = WB_STORAGE_API_BASE_ + '/api/v1/paid_storage/tasks/' + taskId + '/download';
  console.log('  Download URL: ' + downloadUrl);

  var dlResp = UrlFetchApp.fetch(downloadUrl, {
    method: 'get',
    headers: { 'Authorization': token },
    muteHttpExceptions: true
  });

  var dlCode = dlResp.getResponseCode();
  var dlBody = dlResp.getContentText();
  console.log('  Download HTTP: ' + dlCode);
  console.log('  Body length: ' + dlBody.length + ' chars');

  if (dlCode !== 200) {
    console.log('❌ Download error: ' + dlBody.substring(0, 300));
    return;
  }

  var reportData;
  try { reportData = JSON.parse(dlBody); } catch (e) {
    console.log('❌ JSON parse error: ' + e.message);
    console.log('  Body start: ' + dlBody.substring(0, 500));
    return;
  }

  // ── 4. Анализ структуры ──
  console.log('');
  console.log('═══════════════════════════════════════════════════');
  console.log('  СТРУКТУРА ОТВЕТА');
  console.log('═══════════════════════════════════════════════════');

  // Проверяем — массив или объект с вложенным массивом
  var items = reportData;
  if (!Array.isArray(reportData)) {
    console.log('  Ответ — НЕ массив. Тип: ' + typeof reportData);
    console.log('  Ключи верхнего уровня: ' + Object.keys(reportData).join(', '));
    console.log('  JSON (первые 500): ' + JSON.stringify(reportData).substring(0, 500));

    // Попытки найти массив данных
    if (reportData.data && Array.isArray(reportData.data)) {
      items = reportData.data;
      console.log('  → Нашли массив в reportData.data (' + items.length + ' записей)');
    } else if (reportData.report && Array.isArray(reportData.report)) {
      items = reportData.report;
      console.log('  → Нашли массив в reportData.report (' + items.length + ' записей)');
    } else if (reportData.result && Array.isArray(reportData.result)) {
      items = reportData.result;
      console.log('  → Нашли массив в reportData.result (' + items.length + ' записей)');
    } else {
      console.log('  ❌ Не удалось найти массив данных');
      // Дамп всех ключей рекурсивно
      dumpObjectStructure_(reportData, '  ', 2);
      return;
    }
  }

  console.log('  Записей: ' + items.length);

  if (items.length === 0) {
    console.log('  ⚠️ Пустой массив');
    return;
  }

  // ── 5. Ключи первой записи ──
  var first = items[0];
  var allKeys = Object.keys(first);
  console.log('');
  console.log('  КЛЮЧИ ПЕРВОЙ ЗАПИСИ (' + allKeys.length + '):');
  for (var ki = 0; ki < allKeys.length; ki++) {
    var k = allKeys[ki];
    var v = first[k];
    var vType = typeof v;
    var vStr = String(v);
    if (vStr.length > 80) vStr = vStr.substring(0, 80) + '...';
    console.log('    [' + ki + '] ' + k + ' = ' + vStr + '  (тип: ' + vType + ')');
  }

  // ── 6. Первые 5 записей (полный дамп) ──
  console.log('');
  console.log('  ПЕРВЫЕ 5 ЗАПИСЕЙ:');
  for (var ei = 0; ei < Math.min(5, items.length); ei++) {
    console.log('  --- Запись #' + ei + ' ---');
    console.log('  ' + JSON.stringify(items[ei]));
  }

  // ── 7. Числовые поля — суммы по всему ответу ──
  console.log('');
  console.log('═══════════════════════════════════════════════════');
  console.log('  ЧИСЛОВЫЕ ПОЛЯ — СУММЫ');
  console.log('═══════════════════════════════════════════════════');

  // Собираем все числовые поля из первой записи
  var numericKeys = [];
  for (var nk = 0; nk < allKeys.length; nk++) {
    var val = first[allKeys[nk]];
    if (typeof val === 'number') numericKeys.push(allKeys[nk]);
    // Проверяем строки, которые выглядят как числа
    if (typeof val === 'string' && val !== '' && !isNaN(Number(val)) && val.length < 20) {
      numericKeys.push(allKeys[nk] + ' (string→number)');
    }
  }

  console.log('  Числовые ключи: ' + numericKeys.join(', '));

  // Считаем суммы
  var sums = {};
  var counts = {};
  var examples = {};
  for (var ii = 0; ii < items.length; ii++) {
    var item = items[ii];
    for (var nki = 0; nki < allKeys.length; nki++) {
      var key = allKeys[nki];
      var rawVal = item[key];
      var numVal = Number(rawVal);
      if (isNaN(numVal) || rawVal === null || rawVal === '' || typeof rawVal === 'boolean') continue;

      if (!sums[key]) { sums[key] = 0; counts[key] = 0; examples[key] = []; }
      sums[key] += numVal;
      counts[key]++;
      if (examples[key].length < 3) examples[key].push(rawVal);
    }
  }

  // Выводим суммы, отсортированные по абсолютному значению
  var sumKeys = Object.keys(sums);
  sumKeys.sort(function(a, b) { return Math.abs(sums[b]) - Math.abs(sums[a]); });

  for (var sk = 0; sk < sumKeys.length; sk++) {
    var sKey = sumKeys[sk];
    console.log('  ' + sKey + ':');
    console.log('    Сумма: ' + sums[sKey]);
    console.log('    Кол-во: ' + counts[sKey] + ' из ' + items.length);
    console.log('    Примеры: ' + JSON.stringify(examples[sKey]));
  }

  // ── 8. Проверка конкретных полей, которые могут быть суммой хранения ──
  console.log('');
  console.log('═══════════════════════════════════════════════════');
  console.log('  ПРОВЕРКА ПОЛЕЙ-КАНДИДАТОВ НА СУММУ ХРАНЕНИЯ');
  console.log('═══════════════════════════════════════════════════');

  var candidateFields = [
    'storageCost', 'storage_cost', 'warehousePrice', 'warehouse_price',
    'cost', 'price', 'amount', 'sum', 'total', 'tariff',
    'palletStorageValueExpr', 'boxStorageBase', 'boxStorageLiter',
    'storagePricePerBarcode', 'storagePricePerBarcodeAvg',
    'storagePricePerBarcodeBase',
    'warehousePricePerBarcode', 'warehousePricePerBarcodeAvg',
    'ppvzStorageFee', 'ppvz_storage_fee'
  ];

  for (var ci = 0; ci < candidateFields.length; ci++) {
    var cf = candidateFields[ci];
    if (first[cf] !== undefined) {
      console.log('  ✅ НАЙДЕНО: ' + cf + ' = ' + first[cf] + ' (тип: ' + typeof first[cf] + ')');
      if (sums[cf] !== undefined) {
        console.log('    Сумма по всем записям: ' + sums[cf]);
      }
    }
  }

  // ── 9. Поиск поля с суммой, совпадающей с ожидаемой (~870 ₽/день) ──
  console.log('');
  console.log('  ПОИСК ПОЛЯ С РАЗУМНОЙ СУММОЙ ХРАНЕНИЯ:');
  console.log('  (ожидаем ~800-1000 ₽/день для 20 SKU)');

  for (var sk2 = 0; sk2 < sumKeys.length; sk2++) {
    var sk2Key = sumKeys[sk2];
    var s = sums[sk2Key];
    // Хранение за 1 день ~ 500-2000 ₽, за неделю ~ 3500-14000 ₽
    if (Math.abs(s) > 100 && Math.abs(s) < 50000) {
      console.log('  🔍 ' + sk2Key + ' = ' + s + ' ₽ (кандидат)');
    }
  }

  // ── 10. Проверяем поля в camelCase и snake_case ──
  console.log('');
  console.log('  ВСЕ КЛЮЧИ (для поиска нужного):');
  for (var ak = 0; ak < allKeys.length; ak++) {
    console.log('    ' + allKeys[ak]);
  }

  var elapsed = sec_(t0);
  console.log('');
  console.log('═══════════════════════════════════════════════════');
  console.log('  АУДИТ ЗАВЕРШЁН: ' + elapsed + ' сек');
  console.log('═══════════════════════════════════════════════════');
}


/**
 * Рекурсивный дамп структуры объекта (для нестандартных ответов).
 */
function dumpObjectStructure_(obj, indent, maxDepth) {
  if (maxDepth <= 0) { console.log(indent + '...'); return; }
  var keys = Object.keys(obj);
  for (var i = 0; i < keys.length; i++) {
    var k = keys[i];
    var v = obj[k];
    var t = typeof v;
    if (v === null) {
      console.log(indent + k + ': null');
    } else if (Array.isArray(v)) {
      console.log(indent + k + ': Array[' + v.length + ']');
      if (v.length > 0 && typeof v[0] === 'object') {
        dumpObjectStructure_(v[0], indent + '  ', maxDepth - 1);
      }
    } else if (t === 'object') {
      console.log(indent + k + ': Object');
      dumpObjectStructure_(v, indent + '  ', maxDepth - 1);
    } else {
      var sv = String(v);
      if (sv.length > 60) sv = sv.substring(0, 60) + '...';
      console.log(indent + k + ': ' + sv + ' (' + t + ')');
    }
  }
}


// ═══════════════════════════════════════
// ЗАДАЧА 2: ТЕСТОВЫЕ ФУНКЦИИ
// ═══════════════════════════════════════

/**
 * Аудит API за 28.05.2026.
 */
function testAuditStorageApi_2026_05_28() {
  auditWbStorageApiResponse('2026-05-28', '2026-05-28');
}

/**
 * Аудит API за 25-28.05.2026.
 */
function testAuditStorageApi_2026_05_25_28() {
  auditWbStorageApiResponse('2026-05-25', '2026-05-28');
}

/**
 * Аудит API за 18-24.05.2026.
 * Ожидаемый результат: ~6099 ₽ (как из XLSX).
 */
function testAuditStorageApi_2026_05_18_24() {
  auditWbStorageApiResponse('2026-05-18', '2026-05-24');
}


// ═══════════════════════════════════════
// ЗАДАЧА 3: ROLLING 7 DAYS
// ═══════════════════════════════════════

/**
 * [ДИАГНОСТИКА] Импорт хранения через API за последние 7 дней (вчера - 6 дней).
 *
 * Переименована в приватную *_Diag_ для устранения дубля глобального имени:
 * боевая реализация живёт в Wbstoragefolderloader (importWbStorageFromApiRolling7Days),
 * на неё ссылаются меню и updateWbStorageData. В Apps Script все .gs делят одно
 * глобальное пространство имён, поэтому два определения одного имени перетирали друг друга.
 *
 * НЕ подключать к триггеру, пока не проверен маппинг суммы.
 */
function importWbStorageFromApiRolling7DaysDiag_() {
  var yesterday = new Date();
  yesterday.setDate(yesterday.getDate() - 1);

  var weekAgo = new Date(yesterday);
  weekAgo.setDate(weekAgo.getDate() - 6);

  var dateFrom = Utilities.formatDate(weekAgo, 'Europe/Moscow', 'yyyy-MM-dd');
  var dateTo = Utilities.formatDate(yesterday, 'Europe/Moscow', 'yyyy-MM-dd');

  console.log('  Rolling 7 days: ' + dateFrom + ' — ' + dateTo);
  importWbStorageFromApi(dateFrom, dateTo);
}


// ═══════════════════════════════════════
// ЗАДАЧА 4: WARNING_ZERO_STORAGE_SUM
// ═══════════════════════════════════════

// Этот патч модифицирует поведение importWbStorageFromApi.
// Вместо прямой модификации — добавляем функцию пост-валидации,
// которую можно вызвать после импорта.

/**
 * Проверяет последний импорт API в IMPORT_LOG_STORAGE.
 * Если rows_imported > 0 и storage_sum = 0, меняет status на WARNING_ZERO_STORAGE_SUM.
 *
 * Вызывать после importWbStorageFromApi(), если есть подозрение на
 * неверный маппинг суммы.
 */
function validateLastApiImportLog() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var logSheet = ss.getSheetByName(IMPORT_LOG_STORAGE_SHEET_NAME_);
  if (!logSheet || logSheet.getLastRow() < 2) {
    console.log('⚠️ IMPORT_LOG_STORAGE пуст');
    return;
  }

  var lastRow = logSheet.getLastRow();
  var data = logSheet.getRange(2, 1, lastRow - 1, IMPORT_LOG_STORAGE_HEADERS_.length).getValues();

  var hMap = {};
  for (var i = 0; i < IMPORT_LOG_STORAGE_HEADERS_.length; i++) {
    hMap[IMPORT_LOG_STORAGE_HEADERS_[i]] = i;
  }

  var fixed = 0;
  for (var r = 0; r < data.length; r++) {
    var sourceType = String(data[r][hMap['source_type']] || '').trim();
    var status = String(data[r][hMap['status']] || '').trim();
    var rowsImported = Number(data[r][hMap['rows_imported']] || 0);
    var storageSum = Number(data[r][hMap['storage_sum']] || 0);

    if (sourceType === 'API' && status === 'OK' && rowsImported > 0 && storageSum === 0) {
      // Меняем status на WARNING_ZERO_STORAGE_SUM
      var statusCol = hMap['status'] + 1; // 1-based
      var errorCol = hMap['error_message'] + 1;
      var sheetRow = r + 2; // 1-based + header

      logSheet.getRange(sheetRow, statusCol).setValue('WARNING_ZERO_STORAGE_SUM');
      logSheet.getRange(sheetRow, errorCol).setValue(
        'rows=' + rowsImported + ' но storage_sum=0. ' +
        'Возможно, маппинг поля суммы неверный. ' +
        'Запустите auditWbStorageApiResponse() для диагностики.'
      );

      console.log('  ⚠️ Строка ' + sheetRow + ': OK → WARNING_ZERO_STORAGE_SUM (rows=' + rowsImported + ', sum=0)');
      fixed++;
    }
  }

  SpreadsheetApp.flush();
  console.log('  Обновлено записей: ' + fixed);

  if (fixed > 0) {
    SpreadsheetApp.getUi().alert('⚠️ Найдены подозрительные импорты',
      'Обновлено ' + fixed + ' записей в IMPORT_LOG_STORAGE.\n\n' +
      'Статус изменён на WARNING_ZERO_STORAGE_SUM.\n' +
      'Это означает: API вернул строки, но сумма хранения = 0 ₽.\n\n' +
      'Следующий шаг:\n' +
      '1. Запустите testAuditStorageApi_2026_05_18_24()\n' +
      '2. Посмотрите в логе, какое поле содержит сумму\n' +
      '3. Обновите маппинг в normalizeApiRows_()',
      SpreadsheetApp.getUi().ButtonSet.OK);
  } else {
    SpreadsheetApp.getUi().alert('✅ Всё OK',
      'Нет API-импортов с rows > 0 и sum = 0.',
      SpreadsheetApp.getUi().ButtonSet.OK);
  }
}


// ═══════════════════════════════════════
// ЗАДАЧА 5: АВТОМАТИЧЕСКАЯ ФИКСАЦИЯ МАППИНГА
// ═══════════════════════════════════════

/**
 * Запускает аудит API за 18-24 мая и автоматически определяет
 * правильное поле суммы хранения.
 *
 * Если XLSX за тот же период дал 6099.37 ₽, а API дал 0 ₽ —
 * ищем поле, сумма которого ≈ 6099 ₽.
 *
 * Результат выводит в лог с рекомендацией.
 */
function detectStorageCostField() {
  var t0 = Date.now();
  console.log('═══ detectStorageCostField() ═══');
  console.log('  Цель: найти поле с суммой хранения в API JSON');
  console.log('  Ожидаемая сумма за 18-24 мая: ~6099 ₽');

  var dateFrom = '2026-05-18';
  var dateTo = '2026-05-24';

  var token = getScriptProperty_(WB_STORAGE_TOKEN_KEY_);
  if (!token) token = getScriptProperty_('WB_TOKEN_ANALYTICS');
  if (!token) { console.log('❌ Нет токена'); return; }

  // Создаём задачу
  var createUrl = WB_STORAGE_API_BASE_ + '/api/v1/paid_storage?dateFrom=' +
    dateFrom + '&dateTo=' + dateTo;

  var createResp = UrlFetchApp.fetch(createUrl, {
    method: 'get',
    headers: { 'Authorization': token },
    muteHttpExceptions: true
  });

  if (createResp.getResponseCode() !== 200) {
    console.log('❌ HTTP ' + createResp.getResponseCode());
    return;
  }

  var createData = JSON.parse(createResp.getContentText());
  var taskId = '';
  if (createData.data && createData.data.taskId) taskId = String(createData.data.taskId);
  else if (createData.taskId) taskId = String(createData.taskId);
  else taskId = String(createResp.getContentText()).replace(/[^a-zA-Z0-9\-]/g, '');

  if (!taskId) { console.log('❌ Нет taskId'); return; }
  console.log('  taskId: ' + taskId);

  // Ждём готовности
  var statusUrl = WB_STORAGE_API_BASE_ + '/api/v1/paid_storage/tasks/' + taskId + '/status';
  var ready = false;
  for (var p = 0; p < WB_STORAGE_API_MAX_POLLS_; p++) {
    Utilities.sleep(WB_STORAGE_API_POLL_INTERVAL_);
    var sr = UrlFetchApp.fetch(statusUrl, {
      method: 'get', headers: { 'Authorization': token }, muteHttpExceptions: true
    });
    var sd;
    try { sd = JSON.parse(sr.getContentText()); } catch (e) { sd = {}; }
    var st = (sd.data && sd.data.status) || sd.status || '';
    console.log('  Опрос ' + (p + 1) + ': ' + st);
    if (st === 'done') { ready = true; break; }
    if (st === 'purged' || st === 'canceled') { console.log('❌ ' + st); return; }
  }
  if (!ready) { console.log('❌ Таймаут'); return; }

  // Скачиваем
  var dlResp = UrlFetchApp.fetch(
    WB_STORAGE_API_BASE_ + '/api/v1/paid_storage/tasks/' + taskId + '/download',
    { method: 'get', headers: { 'Authorization': token }, muteHttpExceptions: true }
  );
  if (dlResp.getResponseCode() !== 200) {
    console.log('❌ Download HTTP ' + dlResp.getResponseCode());
    return;
  }

  var items = JSON.parse(dlResp.getContentText());
  if (!Array.isArray(items)) {
    if (items.data && Array.isArray(items.data)) items = items.data;
    else if (items.report && Array.isArray(items.report)) items = items.report;
    else { console.log('❌ Не массив'); return; }
  }

  console.log('  Записей: ' + items.length);
  if (items.length === 0) { console.log('⚠️ Пусто'); return; }

  // Считаем суммы по всем числовым полям
  var allKeys = Object.keys(items[0]);
  var fieldSums = {};

  for (var i = 0; i < items.length; i++) {
    for (var ki = 0; ki < allKeys.length; ki++) {
      var key = allKeys[ki];
      var val = items[i][key];
      var num = Number(val);
      if (isNaN(num) || val === null || val === '' || typeof val === 'boolean') continue;
      if (!fieldSums[key]) fieldSums[key] = 0;
      fieldSums[key] += num;
    }
  }

  // Ищем поле, сумма которого ≈ 6099
  var targetSum = 6099.37;
  var tolerance = 100; // ±100 ₽

  console.log('');
  console.log('═══════════════════════════════════════════════════');
  console.log('  РЕЗУЛЬТАТ: ПОИСК ПОЛЯ С СУММОЙ ≈ ' + targetSum + ' ₽');
  console.log('═══════════════════════════════════════════════════');

  var bestField = null;
  var bestDelta = Infinity;

  var fieldKeys = Object.keys(fieldSums);
  fieldKeys.sort(function(a, b) {
    return Math.abs(fieldSums[a] - targetSum) - Math.abs(fieldSums[b] - targetSum);
  });

  for (var fk = 0; fk < fieldKeys.length; fk++) {
    var fKey = fieldKeys[fk];
    var fSum = fieldSums[fKey];
    var fDelta = Math.abs(fSum - targetSum);
    var marker = fDelta <= tolerance ? ' ✅ СОВПАДЕНИЕ!' : '';

    if (fDelta <= tolerance || fk < 10) {
      console.log('  ' + fKey + ' = ' + fSum + '  (Δ=' + fDelta.toFixed(2) + ')' + marker);
    }

    if (fDelta < bestDelta) {
      bestDelta = fDelta;
      bestField = fKey;
    }
  }

  console.log('');
  if (bestDelta <= tolerance) {
    console.log('  ✅ НАЙДЕНО ПОЛЕ СУММЫ ХРАНЕНИЯ: ' + bestField);
    console.log('     Сумма: ' + fieldSums[bestField] + ' ₽ (ожидалось ' + targetSum + ' ₽)');
    console.log('');
    console.log('  РЕКОМЕНДАЦИЯ:');
    console.log('  В normalizeApiRows_() замените:');
    console.log('    var cost = Number(item.storageCost || item.storage_cost || 0) || 0;');
    console.log('  на:');
    console.log('    var cost = Number(item.' + bestField + ' || item.storageCost || item.storage_cost || 0) || 0;');
    console.log('');
    console.log('  Или обновите WB_STORAGE_TOKEN_KEY_ / маппинг.');
  } else {
    console.log('  ⚠️ Нет точного совпадения.');
    console.log('  Ближайшее: ' + bestField + ' = ' + fieldSums[bestField] + ' (Δ=' + bestDelta.toFixed(2) + ')');
    console.log('');
    console.log('  Возможные причины:');
    console.log('  1. API отдаёт сумму в копейках (разделить на 100)');
    console.log('  2. API отдаёт сумму за штрихкод, а не общую');
    console.log('  3. API и XLSX считают хранение по-разному');
  }

  // Проверяем деление на 100 (копейки)
  console.log('');
  console.log('  ПРОВЕРКА КОПЕЕК (суммы / 100):');
  for (var fk2 = 0; fk2 < Math.min(10, fieldKeys.length); fk2++) {
    var fKey2 = fieldKeys[fk2];
    var divided = fieldSums[fKey2] / 100;
    var delta100 = Math.abs(divided - targetSum);
    if (delta100 <= tolerance) {
      console.log('  ✅ ' + fKey2 + ' / 100 = ' + divided + ' (Δ=' + delta100.toFixed(2) + ') — ВОЗМОЖНО В КОПЕЙКАХ!');
    }
  }

  console.log('');
  console.log('═══ detectStorageCostField() ЗАВЕРШЕНО: ' + sec_(t0) + ' сек ═══');
}
