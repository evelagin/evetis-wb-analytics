/**
 * ══════════════════════════════════════════════════════════════
 * EVETIS WB — WbApiGateSalesReturns_TEMP.gs  (ВРЕМЕННЫЙ, READ-ONLY)
 *
 * Назначение: отдельный диагностический gate перед PR #14B
 *             (sales/returns production loader).
 *             Подтверждает структуру WB Statistics sales API,
 *             разметку sale vs return и наличие реальных возвратов
 *             в периоде 2026-04-01 — 2026-06-16.
 *
 * ЭТОТ ФАЙЛ ВРЕМЕННЫЙ. В репозиторий НЕ коммитить. После ревью удалить.
 *
 * ЧТО ДЕЛАЕТ:
 *   • GET /api/v1/supplier/sales?dateFrom=...&flag=0 (Statistics token)
 *   • помесячные окна, пагинация по lastChangeDate
 *   • считает общие / sales / returns checksums
 *   • presence/coverage по списку полей
 *   • примеры строк (3 sale, до 10 return) + дифф полей return vs sale
 *   • рекомендации для PR #14B (часть — data-driven)
 *
 * ЧЕГО НЕ ДЕЛАЕТ (предохранители):
 *   • НЕ пишет в RAW_WB_SALES_RETURNS и любые RAW-листы
 *   • НЕ пишет в CLEAN / UNIT / P&L / dashboards
 *   • НЕ трогает WbDailyRefresh, триггеры, SheetsSchema
 *   • НЕ вызывает production import-функции
 *   • токен НЕ логируется и НЕ попадает в JSON/alert
 *
 * РАЗРЕШЕНО: console.log; alert со сводкой; JSON-файл в тестовую
 *   Drive-папку (как тестовый контур — по property WB_API_TEST_RESULTS_FOLDER_ID).
 *   Если property нет — JSON просто не пишется, остаётся console + alert.
 *
 * СЕМАНТИКА flag=0: dateFrom — это КУРСОР по lastChangeDate, не «дата от».
 *   Окна по месяцам нужны для атрибуции строк: внутри окна строки
 *   фильтруются по полю `date` (дата продажи) в границы окна.
 * ══════════════════════════════════════════════════════════════
 */


// ═══════════════════════════════════════
// КОНФИГ
// ═══════════════════════════════════════

var GATE_SR_API_BASE_   = 'https://statistics-api.wildberries.ru';
var GATE_SR_API_PATH_   = '/api/v1/supplier/sales';
var GATE_SR_FLAG_       = 0;

// Тот же Statistics-токен, что и T3/T4/finance; fallback на Analytics — как принято в проекте.
var GATE_SR_TOKEN_KEYS_ = ['WB_TOKEN_STATISTICS', 'WB_TOKEN_ANALYTICS'];

// Тестовая Drive-папка результатов (тот же property, что у WbApiTestRunner).
var GATE_SR_RESULTS_FOLDER_PROP_ = 'WB_API_TEST_RESULTS_FOLDER_ID';

var GATE_SR_TZ_         = 'Europe/Moscow';
var GATE_SR_MAX_PAGES_  = 60;     // потолок страниц на окно (предохранитель)
var GATE_SR_PAGE_PAUSE_ = 2000;   // мс между страницами пагинации
var GATE_SR_RETRY_429_  = 3;      // повторы при HTTP 429
var GATE_SR_RETRY_PAUSE_= 20000;  // мс паузы при 429

var GATE_SR_PERIOD_LABEL_ = '2026-04-01 — 2026-06-16';
var GATE_SR_WINDOWS_ = [
  { from: '2026-04-01', to: '2026-04-30' },
  { from: '2026-05-01', to: '2026-05-31' },
  { from: '2026-06-01', to: '2026-06-16' }
];

// Поля для presence/coverage (имена — как в JSON-ответе WB sales API).
var GATE_SR_COVERAGE_FIELDS_ = [
  'date', 'lastChangeDate', 'srid', 'saleID', 'nmId', 'barcode',
  'supplierArticle', 'warehouseName',
  'oblast', 'oblastOkrugName', 'regionName', 'countryName',
  'forPay', 'paymentSaleAmount', 'finishedPrice',
  'priceWithDisc', 'totalPrice', 'spp', 'discountPercent',
  'isSupply', 'isRealization'
];

// Ключевые поля для примеров строк.
var GATE_SR_SAMPLE_FIELDS_ = [
  'date', 'lastChangeDate', 'srid', 'saleID', 'nmId', 'barcode',
  'supplierArticle', 'warehouseName', 'regionName', 'oblast', 'countryName',
  'forPay', 'paymentSaleAmount', 'finishedPrice', 'priceWithDisc',
  'totalPrice', 'spp', 'discountPercent', 'isSupply', 'isRealization'
];


// ═══════════════════════════════════════
// ОСНОВНАЯ ФУНКЦИЯ
// ═══════════════════════════════════════

/**
 * Запускать ВРУЧНУЮ из редактора Apps Script.
 * Ничего не пишет в листы. Результат: console.log + alert + (опц.) JSON в Drive.
 */
function runGateSalesReturnsDiagnostic() {
  var t0 = Date.now();
  gateSrLog_('═══════════════════════════════════════════════════');
  gateSrLog_('  GATE SALES/RETURNS (PR #14B preflight)');
  gateSrLog_('  Период: ' + GATE_SR_PERIOD_LABEL_);
  gateSrLog_('  Endpoint: ' + GATE_SR_API_BASE_ + GATE_SR_API_PATH_ + '?flag=' + GATE_SR_FLAG_);
  gateSrLog_('═══════════════════════════════════════════════════');

  // ── Токен (без логирования значения) ──
  var tk = gateSrGetToken_();
  gateSrLog_('Token: key=' + (tk.keyUsed || '—') + '; present=' + (tk.present ? 'yes' : 'no'));
  if (!tk.present) {
    gateSrLog_('❌ Нет токена ни в одном из ключей: ' + GATE_SR_TOKEN_KEYS_.join(', '));
    gateSrAlert_('❌ GATE Sales/Returns', 'Нет Statistics-токена (' + GATE_SR_TOKEN_KEYS_.join(' / ') + ').');
    return;
  }

  // ── Аккумуляторы по периоду ──
  var allSales = [];      // строки-продажи (date-filtered, по всем окнам)
  var allReturns = [];    // строки-возвраты (date-filtered)
  var windowsReport = []; // отчёт по окнам

  // ── Проход по окнам ──
  for (var w = 0; w < GATE_SR_WINDOWS_.length; w++) {
    var win = GATE_SR_WINDOWS_[w];
    gateSrLog_('');
    gateSrLog_('─── Окно ' + (w + 1) + '/' + GATE_SR_WINDOWS_.length + ': ' + win.from + ' — ' + win.to + ' ───');

    var fetched = gateSrFetchWindow_(tk.token, win);
    // fetched: { raw: [...], httpStatuses: [...], pages, terminatedBy204, error }

    // Фильтр по дате продажи (поле `date`) в границы окна.
    var inWindow = [];
    for (var i = 0; i < fetched.raw.length; i++) {
      var d = gateSrDateKey_(gateSrPick_(fetched.raw[i], ['date']));
      if (d && d >= win.from && d <= win.to) inWindow.push(fetched.raw[i]);
    }

    // Разделение sale / return по saleID startsWith 'R'.
    var winSales = [], winReturns = [];
    for (var j = 0; j < inWindow.length; j++) {
      if (gateSrIsReturn_(inWindow[j])) winReturns.push(inWindow[j]);
      else winSales.push(inWindow[j]);
    }

    var winRep = {
      window: win.from + ' — ' + win.to,
      http_status: fetched.httpStatuses,
      pages: fetched.pages,
      terminated_by_204: fetched.terminatedBy204,
      error: fetched.error || null,
      raw_rows_in_response: fetched.raw.length,
      rows_after_date_filter: inWindow.length,
      sales_count: winSales.length,
      returns_count: winReturns.length
    };
    windowsReport.push(winRep);

    gateSrLog_('  http_status: ' + JSON.stringify(fetched.httpStatuses) +
      '; pages=' + fetched.pages + (fetched.error ? ('; error=' + fetched.error) : ''));
    gateSrLog_('  raw_rows_in_response=' + fetched.raw.length +
      '; rows_after_date_filter=' + inWindow.length +
      '; sales=' + winSales.length + '; returns=' + winReturns.length);

    for (var s = 0; s < winSales.length; s++) allSales.push(winSales[s]);
    for (var r = 0; r < winReturns.length; r++) allReturns.push(winReturns[r]);
  }

  // ── Checksums по всему периоду ──
  var common  = gateSrCommonChecksums_(allSales, allReturns);
  var sales   = gateSrSideChecksums_(allSales, 'sales');
  var returns = gateSrSideChecksums_(allReturns, 'returns');
  var coverage = gateSrCoverage_(allSales.concat(allReturns));
  var fieldDiff = gateSrFieldDiff_(allSales, allReturns);
  var samplesSales   = gateSrSamples_(allSales, 3);
  var samplesReturns = gateSrSamples_(allReturns, 10);

  // ── Рекомендации (часть data-driven) ──
  var recs = gateSrRecommendations_(common, sales, returns, fieldDiff);

  // ── Сборка результата ──
  var result = {
    gate: 'WbApiGateSalesReturns_TEMP',
    generated_at: Utilities.formatDate(new Date(), GATE_SR_TZ_, 'yyyy-MM-dd HH:mm:ss'),
    endpoint: GATE_SR_API_BASE_ + GATE_SR_API_PATH_,
    flag: GATE_SR_FLAG_,
    period: GATE_SR_PERIOD_LABEL_,
    token_key_used: tk.keyUsed,
    windows: windowsReport,
    common_checksums: common,
    sales_checksums: sales,
    returns_checksums: returns,
    field_coverage: coverage,
    field_diff_return_vs_sale: fieldDiff,
    sample_sales: samplesSales,
    sample_returns: samplesReturns,
    recommendations_pr14b: recs,
    elapsed_sec: Math.round((Date.now() - t0) / 1000)
  };

  // ── Лог сводки ──
  gateSrLogResult_(result);

  // ── JSON в Drive (опционально) ──
  var savedTo = gateSrSaveJsonIfFolder_(result);

  // ── Alert ──
  gateSrAlert_('✅ GATE Sales/Returns готов', gateSrAlertText_(result, savedTo));

  gateSrLog_('');
  gateSrLog_('═══ GATE завершён за ' + result.elapsed_sec + ' сек ═══');
  return result;
}


// ═══════════════════════════════════════
// HTTP / ПАГИНАЦИЯ
// ═══════════════════════════════════════

/**
 * Загружает одно окно: запрос от win.from (курсор lastChangeDate), пагинация
 * по lastChangeDate последней строки, дедуп внутри окна по srid|saleID.
 * @return {Object} { raw, httpStatuses, pages, terminatedBy204, error }
 */
function gateSrFetchWindow_(token, win) {
  var raw = [];
  var seen = {};            // дедуп по srid|saleID внутри окна
  var httpStatuses = [];
  var pages = 0;
  var terminatedBy204 = false;
  var error = null;

  var cursor = win.from + 'T00:00:00';  // RFC3339; dateFrom как курсор lastChangeDate

  try {
    while (pages < GATE_SR_MAX_PAGES_) {
      pages++;
      var url = GATE_SR_API_BASE_ + GATE_SR_API_PATH_ +
        '?dateFrom=' + encodeURIComponent(cursor) + '&flag=' + GATE_SR_FLAG_;

      var resp = gateSrHttp_(url, token);
      httpStatuses.push(resp.code);

      if (resp.code === 204) { terminatedBy204 = true; break; }
      if (!resp.ok) { error = 'HTTP ' + resp.code + ': ' + String(resp.body).substring(0, 180); break; }

      var chunk = resp.json;
      if (!chunk || chunk.length === undefined) { error = 'Ответ не массив'; break; }
      if (chunk.length === 0) break;

      var lastChange = null;
      for (var i = 0; i < chunk.length; i++) {
        var row = chunk[i];
        var key = String(gateSrPick_(row, ['srid']) || '') + '|' + String(gateSrPick_(row, ['saleID']) || '');
        if (!seen[key]) { seen[key] = true; raw.push(row); }
        var lc = gateSrPick_(row, ['lastChangeDate']);
        if (lc) lastChange = lc;
      }

      // Нет прогресса по курсору → стоп (защита от петли).
      if (!lastChange || lastChange === cursor) break;
      cursor = lastChange;

      Utilities.sleep(GATE_SR_PAGE_PAUSE_);
    }
  } catch (e) {
    error = 'Исключение: ' + e.message;
  }

  return { raw: raw, httpStatuses: httpStatuses, pages: pages, terminatedBy204: terminatedBy204, error: error };
}

/**
 * HTTP GET с Authorization-токеном (токен не логируется), retry на 429.
 * @return {Object} { code, body, json, ok }
 */
function gateSrHttp_(url, token) {
  var attempt = 0;
  while (true) {
    attempt++;
    var resp;
    try {
      resp = UrlFetchApp.fetch(url, {
        method: 'get',
        headers: { 'Authorization': token },
        muteHttpExceptions: true
      });
    } catch (e) {
      return { code: 0, body: e.message, json: null, ok: false };
    }
    var code = resp.getResponseCode();
    var body = resp.getContentText();

    if (code === 429 && attempt <= GATE_SR_RETRY_429_) {
      gateSrLog_('  HTTP 429 — пауза ' + (GATE_SR_RETRY_PAUSE_ / 1000) + ' c (попытка ' + attempt + ')');
      Utilities.sleep(GATE_SR_RETRY_PAUSE_);
      continue;
    }

    var json = null;
    try { json = JSON.parse(body); } catch (e2) { json = null; }
    return { code: code, body: body, json: json, ok: (code >= 200 && code < 300) };
  }
}


// ═══════════════════════════════════════
// CHECKSUMS
// ═══════════════════════════════════════

function gateSrCommonChecksums_(salesRows, returnRows) {
  var all = salesRows.concat(returnRows);
  var nmSet = {}, dates = [], changes = [];
  var emptyNm = 0, emptySrid = 0, emptySale = 0;

  for (var i = 0; i < all.length; i++) {
    var row = all[i];
    var nm = gateSrPick_(row, ['nmId']);
    var srid = gateSrPick_(row, ['srid']);
    var sale = gateSrPick_(row, ['saleID']);
    if (gateSrEmpty_(nm)) emptyNm++; else nmSet[String(nm)] = true;
    if (gateSrEmpty_(srid)) emptySrid++;
    if (gateSrEmpty_(sale)) emptySale++;

    var d = gateSrDateKey_(gateSrPick_(row, ['date']));
    if (d) dates.push(d);
    var lc = gateSrDateKey_(gateSrPick_(row, ['lastChangeDate']));
    if (lc) changes.push(lc);
  }

  return {
    endpoint: GATE_SR_API_BASE_ + GATE_SR_API_PATH_,
    periods: GATE_SR_WINDOWS_,
    total_rows_after_filter: all.length,
    min_date: gateSrMin_(dates),
    max_date: gateSrMax_(dates),
    min_lastChangeDate: gateSrMin_(changes),
    max_lastChangeDate: gateSrMax_(changes),
    unique_nmId: Object.keys(nmSet).length,
    rows_empty_nmId: emptyNm,
    rows_empty_srid: emptySrid,
    rows_empty_saleID: emptySale
  };
}

/** Checksums для одной стороны (sales или returns). */
function gateSrSideChecksums_(rows, label) {
  var saleSet = {}, sridSet = {};
  var sumForPay = 0, sumPaySale = 0, sumFinished = 0;
  var negForPay = 0, posForPay = 0, negFinished = 0, posFinished = 0;

  for (var i = 0; i < rows.length; i++) {
    var row = rows[i];
    var sale = gateSrPick_(row, ['saleID']);
    var srid = gateSrPick_(row, ['srid']);
    if (!gateSrEmpty_(sale)) saleSet[String(sale)] = true;
    if (!gateSrEmpty_(srid)) sridSet[String(srid)] = true;

    var fp = gateSrNum_(gateSrPick_(row, ['forPay']));
    var ps = gateSrNum_(gateSrPick_(row, ['paymentSaleAmount']));
    var fin = gateSrNum_(gateSrPick_(row, ['finishedPrice']));
    sumForPay += fp; sumPaySale += ps; sumFinished += fin;
    if (fp < 0) negForPay++; else if (fp > 0) posForPay++;
    if (fin < 0) negFinished++; else if (fin > 0) posFinished++;
  }

  var out = {
    count: rows.length,
    unique_saleID: Object.keys(saleSet).length,
    unique_srid: Object.keys(sridSet).length,
    sum_forPay: gateSrRound_(sumForPay),
    sum_paymentSaleAmount: gateSrRound_(sumPaySale),
    sum_finishedPrice: gateSrRound_(sumFinished),
    sign_forPay: { negative: negForPay, positive: posForPay },
    sign_finishedPrice: { negative: negFinished, positive: posFinished }
  };
  // Совместимость с формулировкой ТЗ:
  out['unique_saleID_' + label] = out.unique_saleID;
  out['unique_srid_' + label] = out.unique_srid;
  return out;
}

/** Presence/coverage по списку полей: сколько строк имеют непустое значение. */
function gateSrCoverage_(rows) {
  var total = rows.length;
  var cov = {};
  for (var f = 0; f < GATE_SR_COVERAGE_FIELDS_.length; f++) {
    var field = GATE_SR_COVERAGE_FIELDS_[f];
    var present = 0;
    for (var i = 0; i < rows.length; i++) {
      if (!gateSrEmpty_(rows[i][field])) present++;
    }
    cov[field] = {
      present: present,
      total: total,
      coverage_pct: total ? gateSrRound_(present / total * 100) : null
    };
  }
  return cov;
}

/** Какие ключи присутствуют у return-строк vs sale-строк (по объединению ключей). */
function gateSrFieldDiff_(salesRows, returnRows) {
  var keys = {};
  gateSrCollectKeys_(salesRows, keys);
  gateSrCollectKeys_(returnRows, keys);

  var onlyInReturns = [], onlyInSales = [], inBoth = [];
  var keyList = Object.keys(keys).sort();
  for (var k = 0; k < keyList.length; k++) {
    var key = keyList[k];
    var inS = gateSrAnyPresent_(salesRows, key);
    var inR = gateSrAnyPresent_(returnRows, key);
    if (inS && inR) inBoth.push(key);
    else if (inR && !inS) onlyInReturns.push(key);
    else if (inS && !inR) onlyInSales.push(key);
  }
  return { only_in_returns: onlyInReturns, only_in_sales: onlyInSales, in_both: inBoth };
}

/** Примеры строк с ключевыми полями. */
function gateSrSamples_(rows, n) {
  var out = [];
  var lim = Math.min(n, rows.length);
  for (var i = 0; i < lim; i++) {
    var row = rows[i];
    var pick = {};
    for (var f = 0; f < GATE_SR_SAMPLE_FIELDS_.length; f++) {
      var field = GATE_SR_SAMPLE_FIELDS_[f];
      if (row[field] !== undefined) pick[field] = row[field];
    }
    out.push(pick);
  }
  return out;
}


// ═══════════════════════════════════════
// РЕКОМЕНДАЦИИ ДЛЯ PR #14B (часть data-driven)
// ═══════════════════════════════════════

function gateSrRecommendations_(common, sales, returns, fieldDiff) {
  var hasReturns = returns.count > 0;

  var rec = {
    '1_sale_vs_return': 'Базовый признак — saleID startsWith "R" (продажа: НЕ начинается с R, обычно "S..."). ' +
      'Доп. сверка: знак forPay/finishedPrice у возвратов (см. sign_* ниже).',
    '2_date_for_raw': 'sale_dt = поле `date`. last_change_date = `lastChangeDate` — ТОЛЬКО для пагинации/трассировки, ' +
      'НЕ как дата факта. (Для возвратов проверь min/max date — см. common_checksums.)',
    '3_return_fields_present': fieldDiff,
    '4_return_sums_sign': {
      forPay: returns.sign_forPay,
      finishedPrice: returns.sign_finishedPrice,
      hint: 'Если у возвратов forPay/finishedPrice приходят со знаком "+", знак не несёт признака возврата — ' +
        'опирайся на saleID startsWith R; вычитание в CLEAN/P&L делать на стороне маппинга.'
    },
    '5_operation_type_mapping': 'Да: saleID startsWith R → operation_type="return", is_return=TRUE; иначе ' +
      'operation_type="sale", is_return=FALSE. Совпадает с тем, как CleanWbDaily уже читает operation_type/is_return.',
    '6_raw_fields_no_schema_change': gateSrSchemaMapping_(),
    '7_no_returns_fallback': hasReturns ? null : gateSrNoReturnsFallback_()
  };
  return rec;
}

/** Маппинг полей API → существующие 34 колонки RAW_WB_SALES_RETURNS без расширения SheetsSchema. */
function gateSrSchemaMapping_() {
  return {
    note: 'Колонки RAW_WB_SALES_RETURNS (34) фиксированы. Маппится без расширения схемы:',
    map: {
      'srid': 'srid',
      'saleID': 'sale_id',
      'date': 'sale_dt',
      'lastChangeDate': 'last_change_date',
      'operation_type (производное)': 'operation_type',
      'is_return (производное saleID startsWith R)': 'is_return',
      'nmId': 'wb_nm_id',
      'supplierArticle': 'wb_vendor_code',
      'barcode': 'barcode',
      '(matched)': 'internal_sku / sku_match_status',
      'category': 'category',
      'subject': 'subject',
      'brand': 'brand',
      'techSize': 'tech_size',
      'warehouseName': 'warehouse_name',
      'regionName || oblast': 'region_name',
      'countryName': 'country_name',
      'totalPrice': 'total_price',
      'discountPercent': 'discount_percent',
      'spp': 'spp',
      'finishedPrice': 'finished_price',
      'forPay': 'for_pay',
      'quantity = 1 (sales API qty по строке не отдаёт)': 'quantity',
      'sticker': 'sticker_id'
    },
    no_column_in_schema: [
      'paymentSaleAmount — нет колонки и нет raw_json в RAW_WB_SALES_RETURNS → без расширения схемы НЕ сохранить',
      'priceWithDisc — отдельной колонки нет (есть total_price/finished_price) → без расширения схемы НЕ сохранить'
    ],
    empty_columns: [
      'order_dt — sales API дату заказа не отдаёт → останется пустым'
    ]
  };
}

function gateSrNoReturnsFallback_() {
  return [
    'Возвратов в периоде 2026-04-01 — 2026-06-16 не найдено (returns_count=0).',
    'Шаг 1: расширить окно назад в пределах лимита истории WB (до 90 дней) — например 2026-03-18 — 2026-06-16, всё так же read-only.',
    'Шаг 2: сверить с финотчётом (RAW_WB_FINANCE): найти даты с doc_type/supplier_oper_name="Возврат" и точечно пройти sales gate по этим датам.',
    'Шаг 3: если возвратов нет нигде — зафиксировать в #14B, что ветка return пока не наблюдалась, и заложить is_return-обработку «по контракту» (saleID startsWith R) без живого примера.',
    'Production-запись для поиска возвратов НЕ использовать.'
  ];
}


// ═══════════════════════════════════════
// УТИЛИТЫ
// ═══════════════════════════════════════

function gateSrLog_(msg) { console.log('[GATE_SR] ' + msg); }

function gateSrProp_(key) {
  try { return PropertiesService.getScriptProperties().getProperty(key) || ''; }
  catch (e) { return ''; }
}

/** Токен по первому доступному ключу. Значение наружу не отдаём в лог/JSON. */
function gateSrGetToken_() {
  for (var i = 0; i < GATE_SR_TOKEN_KEYS_.length; i++) {
    var v = gateSrProp_(GATE_SR_TOKEN_KEYS_[i]);
    if (v) return { present: true, token: v, keyUsed: GATE_SR_TOKEN_KEYS_[i] };
  }
  return { present: false, token: '', keyUsed: '' };
}

/** saleID startsWith 'R' (регистронезависимо) → возврат. */
function gateSrIsReturn_(row) {
  var s = String(gateSrPick_(row, ['saleID']) || '');
  return s.charAt(0) === 'R' || s.charAt(0) === 'r';
}

/** Достаёт первое непустое из списка имён полей. */
function gateSrPick_(obj, keys) {
  if (!obj) return null;
  for (var i = 0; i < keys.length; i++) {
    if (obj[keys[i]] !== undefined && obj[keys[i]] !== null && obj[keys[i]] !== '') return obj[keys[i]];
  }
  return null;
}

function gateSrEmpty_(v) { return v === undefined || v === null || v === ''; }

function gateSrNum_(v) {
  var n = Number(v);
  return isNaN(n) ? 0 : n;
}

function gateSrRound_(n) { return Math.round((Number(n) || 0) * 100) / 100; }

/** Нормализует дату к 'YYYY-MM-DD' (берёт первые 10 символов ISO). */
function gateSrDateKey_(v) {
  if (gateSrEmpty_(v)) return null;
  var s = String(v);
  if (s.length >= 10 && s.charAt(4) === '-' && s.charAt(7) === '-') return s.substring(0, 10);
  try {
    var d = new Date(v);
    if (!isNaN(d.getTime())) return Utilities.formatDate(d, GATE_SR_TZ_, 'yyyy-MM-dd');
  } catch (e) {}
  return null;
}

function gateSrMin_(arr) {
  if (!arr.length) return null;
  var m = arr[0];
  for (var i = 1; i < arr.length; i++) if (arr[i] < m) m = arr[i];
  return m;
}
function gateSrMax_(arr) {
  if (!arr.length) return null;
  var m = arr[0];
  for (var i = 1; i < arr.length; i++) if (arr[i] > m) m = arr[i];
  return m;
}

function gateSrCollectKeys_(rows, into) {
  for (var i = 0; i < rows.length; i++) {
    var ks = Object.keys(rows[i]);
    for (var k = 0; k < ks.length; k++) into[ks[k]] = true;
  }
}
function gateSrAnyPresent_(rows, key) {
  for (var i = 0; i < rows.length; i++) {
    if (!gateSrEmpty_(rows[i][key])) return true;
  }
  return false;
}


// ═══════════════════════════════════════
// ВЫВОД: console / Drive / alert
// ═══════════════════════════════════════

function gateSrLogResult_(r) {
  gateSrLog_('');
  gateSrLog_('───── COMMON ─────');
  gateSrLog_(JSON.stringify(r.common_checksums, null, 2));
  gateSrLog_('───── SALES ─────');
  gateSrLog_(JSON.stringify(r.sales_checksums, null, 2));
  gateSrLog_('───── RETURNS ─────');
  gateSrLog_(JSON.stringify(r.returns_checksums, null, 2));
  gateSrLog_('───── COVERAGE ─────');
  gateSrLog_(JSON.stringify(r.field_coverage, null, 2));
  gateSrLog_('───── FIELD DIFF (return vs sale) ─────');
  gateSrLog_(JSON.stringify(r.field_diff_return_vs_sale, null, 2));
  gateSrLog_('───── SAMPLE SALES (3) ─────');
  gateSrLog_(JSON.stringify(r.sample_sales, null, 2));
  gateSrLog_('───── SAMPLE RETURNS (до 10) ─────');
  gateSrLog_(JSON.stringify(r.sample_returns, null, 2));
  gateSrLog_('───── RECOMMENDATIONS PR #14B ─────');
  gateSrLog_(JSON.stringify(r.recommendations_pr14b, null, 2));
}

/** Пишет JSON в тестовую Drive-папку, если задан property. Иначе пропускает. */
function gateSrSaveJsonIfFolder_(result) {
  var folderId = gateSrProp_(GATE_SR_RESULTS_FOLDER_PROP_);
  if (!folderId) {
    gateSrLog_('JSON не сохранён: нет property ' + GATE_SR_RESULTS_FOLDER_PROP_ + ' (только console + alert).');
    return '';
  }
  try {
    var folder = DriveApp.getFolderById(folderId);
    var stamp = Utilities.formatDate(new Date(), GATE_SR_TZ_, 'yyyyMMdd_HHmmss');
    var name = 'gate_sales_returns_2026-04-01_2026-06-16_' + stamp + '.json';
    var file = folder.createFile(name, JSON.stringify(result, null, 2), 'application/json');
    gateSrLog_('JSON сохранён: ' + name);
    return name + ' (id=' + file.getId() + ')';
  } catch (e) {
    gateSrLog_('Ошибка сохранения JSON: ' + e.message);
    return '';
  }
}

/** Короткая текстовая сводка для alert. */
function gateSrAlertText_(r, savedTo) {
  var c = r.common_checksums, s = r.sales_checksums, rt = r.returns_checksums;
  var lines = [];
  lines.push('Период: ' + r.period);
  lines.push('Окон: ' + r.windows.length + '  •  строк после фильтра по date: ' + c.total_rows_after_filter);
  lines.push('');
  lines.push('SALES:   count=' + s.count + ', uniq saleID=' + s.unique_saleID +
    ', uniq srid=' + s.unique_srid);
  lines.push('  Σ forPay=' + s.sum_forPay + ', Σ paySale=' + s.sum_paymentSaleAmount +
    ', Σ finished=' + s.sum_finishedPrice);
  lines.push('RETURNS: count=' + rt.count + ', uniq saleID=' + rt.unique_saleID +
    ', uniq srid=' + rt.unique_srid);
  lines.push('  Σ forPay=' + rt.sum_forPay + ', Σ finished=' + rt.sum_finishedPrice);
  lines.push('  знак forPay: -' + rt.sign_forPay.negative + ' / +' + rt.sign_forPay.positive);
  lines.push('');
  lines.push('date: ' + c.min_date + ' … ' + c.max_date);
  lines.push('lastChangeDate: ' + c.min_lastChangeDate + ' … ' + c.max_lastChangeDate);
  lines.push('unique_nmId=' + c.unique_nmId +
    ', empty srid=' + c.rows_empty_srid + ', empty saleID=' + c.rows_empty_saleID);
  lines.push('');
  if (rt.count === 0) lines.push('⚠️ ВОЗВРАТОВ НЕТ — см. recommendations 7 (fallback) в логах.');
  lines.push(savedTo ? ('JSON: ' + savedTo) : 'JSON не сохранён (нет Drive-папки) — смотри логи.');
  lines.push('Полные checksums и рекомендации — в Executions / console.log.');
  return lines.join('\n');
}

function gateSrAlert_(title, msg) {
  try {
    SpreadsheetApp.getUi().alert(title, msg, SpreadsheetApp.getUi().ButtonSet.OK);
  } catch (e) {
    // Запуск без привязанного UI — сводка уже в логах.
    gateSrLog_('(alert недоступен в этом контексте) ' + title + '\n' + msg);
  }
}