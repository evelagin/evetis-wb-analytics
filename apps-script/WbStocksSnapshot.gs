/**
 * ══════════════════════════════════════════════════════════════
 * EVETIS WB — WbStocksSnapshot.gs   (Фаза E — остатки, оркестрация)
 *
 * Суточный снимок остатков: T6 stocks-report/wb-warehouses (плоский,
 * grain snapshot×nmId×chrtId×warehouseId) → RAW_WB_STOCKS (append-only) +
 * manifest WB_STOCKS_SNAPSHOTS. T5 warehouse_remains — НЕблокирующий контроль
 * суммы физических остатков. BQ-механика — в WbStocksBigQuery.gs.
 *
 * Порядок (аудит C1–C3):
 *   lock (ОБЩИЙ project-wide ScriptLock — другого в проекте нет) →
 *   sink ON? (OFF → runtime ERROR, БЕЗ manifest) → ensure RAW+manifest →
 *   snapshot_id+started_at → manifest STARTED (ДО fetch) → T6 fetch
 *   (today..today МСК) → валидация всего пакета (пустой=ERROR) →
 *   нормализация+SKU → T5-контроль (не блокирует) → RAW append
 *   (детерминированные batch jobId) → пост-COUNT → manifest COMPLETE/ERROR
 *   (WHERE status='STARTED', numDmlAffectedRows==1).
 *
 * Легаси листовой WbStocksLoader НЕ используется (BQ-first). Триггер — ~06:30 МСК.
 * ══════════════════════════════════════════════════════════════
 */

var WB_STOCKS_ANALYTICS_HOST_ = 'https://seller-analytics-api.wildberries.ru';
var WB_STOCKS_T6_PATH_        = '/api/analytics/v1/stocks-report/wb-warehouses';
var WB_STOCKS_T5_PATH_        = '/api/v1/warehouse_remains';
var WB_STOCKS_TOKEN_KEYS_     = ['WB_TOKEN_ANALYTICS'];
var WB_STOCKS_TZ_             = 'Europe/Moscow';
var WB_STOCKS_TRIGGER_FN_     = 'runWbStocksSnapshot';
var WB_STOCKS_LOG_SHEET_      = 'IMPORT_LOG_STOCKS';
var WB_STOCKS_CONTROL_TOLERANCE_ = 2;   // ед.: допуск T5/T6 физ. (снимки сняты с разницей в секунды)
var WB_STOCKS_AGG_WH_NAME_    = 'Остальные';

// ───────────────────────────────────────────────────────────────
// Утилиты
// ───────────────────────────────────────────────────────────────

/** Строгое целое (иначе null). Отрицательные допускаются вызывающим отдельно. */
function wbStocksInt_(v) {
  if (v === null || v === undefined || v === '') return null;
  var n = Number(v);
  if (!isFinite(n) || Math.floor(n) !== n) return null;
  return n;
}

/** Короткий UUID (8 hex) для уникальности snapshot_id при двух запусках в одну секунду. */
function wbStocksShortUuid_() {
  return Utilities.getUuid().replace(/-/g, '').substring(0, 8);
}

/** Токен Analytics (для T6/T5). Не логируется. */
function wbStocksGetToken_() {
  var props = PropertiesService.getScriptProperties();
  for (var i = 0; i < WB_STOCKS_TOKEN_KEYS_.length; i++) {
    var key = WB_STOCKS_TOKEN_KEYS_[i], v = '';
    try {
      if (typeof getScriptProperty_ === 'function') v = getScriptProperty_(key) || '';
      if (!v) v = props.getProperty(key) || '';
    } catch (e) { v = ''; }
    if (v) return { key: key, token: v };
  }
  return null;
}

/** HTTP с Authorization; 429 → пауза 21с и повтор (до 3). Токен не логируется. */
function wbStocksHttp_(method, url, token, payload) {
  var attempt = 0;
  while (true) {
    attempt++;
    var options = { method: method, headers: { Authorization: token }, muteHttpExceptions: true };
    if (payload !== undefined && payload !== null) {
      options.contentType = 'application/json';
      options.payload = JSON.stringify(payload);
    }
    var resp;
    try { resp = UrlFetchApp.fetch(url, options); }
    catch (e) { return { ok: false, code: 0, body: '', json: null, error: 'HTTP исключение: ' + ((e && e.message) || e) }; }
    var code = resp.getResponseCode();
    var body = resp.getContentText();
    if (code === 429 && attempt <= 3) {
      console.log('STOCKS HTTP 429 — пауза 21с (попытка ' + attempt + ')');
      Utilities.sleep(21000);
      continue;
    }
    var json = null;
    try { json = JSON.parse(body); } catch (e2) { json = null; }
    return { ok: (code >= 200 && code < 300), code: code, body: body, json: json,
      error: (code >= 200 && code < 300) ? '' : ('HTTP ' + code + ': ' + String(body).substring(0, 200)) };
  }
}

// ───────────────────────────────────────────────────────────────
// FETCH T6 (источник) + T5 (контроль)
// ───────────────────────────────────────────────────────────────

/** T6: один POST, currentPeriod=today..today. Возвращает { ok, data[], http, error }. */
function wbStocksFetchT6_(token, from, to) {
  var url = WB_STOCKS_ANALYTICS_HOST_ + WB_STOCKS_T6_PATH_;
  var body = { currentPeriod: { start: from, end: to }, stockType: '', skipDeletedNm: false };
  var resp = wbStocksHttp_('post', url, token, body);
  if (!resp.ok) return { ok: false, data: [], http: resp.code, error: resp.error };
  var arr = resp.json;
  if (arr && arr.data && arr.data.items && arr.data.items.length !== undefined) arr = arr.data.items;
  else if (arr && arr.data && arr.data.length !== undefined) arr = arr.data;
  else if (!Array.isArray(arr)) return { ok: false, data: [], http: resp.code, error: 'T6: неожиданная форма ответа (не массив/data.items)' };
  return { ok: true, data: arr, http: resp.code, error: '' };
}

/** T5 (контроль): task-based, сумма физических остатков (без псевдо-складов). { ok, sum, error }. */
function wbStocksT5PhysicalSum_(token) {
  var taskBase = WB_STOCKS_ANALYTICS_HOST_ + WB_STOCKS_T5_PATH_;
  var createUrl = taskBase + '?groupByBrand=false&groupBySubject=false&groupBySa=true' +
    '&groupByNm=true&groupByBarcode=true&groupBySize=true';
  var cr = wbStocksHttp_('get', createUrl, token, null);
  if (!cr.ok) return { ok: false, sum: 0, error: 'T5 create: ' + cr.error };
  var taskId = '';
  if (cr.json && cr.json.data && cr.json.data.taskId) taskId = String(cr.json.data.taskId);
  else if (cr.json && cr.json.data && cr.json.data.id) taskId = String(cr.json.data.id);
  else if (cr.json && cr.json.taskId) taskId = String(cr.json.taskId);
  if (!taskId) return { ok: false, sum: 0, error: 'T5: нет taskId в ответе create' };

  var ready = false;
  for (var p = 0; p < 20; p++) {
    Utilities.sleep(9000);
    var sr = wbStocksHttp_('get', taskBase + '/tasks/' + taskId + '/status', token, null);
    var st = '';
    if (sr.json && sr.json.data && sr.json.data.status) st = sr.json.data.status;
    else if (sr.json && sr.json.status) st = sr.json.status;
    st = String(st || '').toLowerCase();
    if (st === 'done' || st === 'ready' || st === 'completed' || st === 'success') { ready = true; break; }
    if (st === 'purged' || st === 'canceled' || st === 'cancelled' || st === 'failed' || st === 'error') {
      return { ok: false, sum: 0, error: 'T5 задача ' + st };
    }
  }
  if (!ready) return { ok: false, sum: 0, error: 'T5: таймаут ожидания задачи' };

  var dl = wbStocksHttp_('get', taskBase + '/tasks/' + taskId + '/download', token, null);
  if (!dl.ok) return { ok: false, sum: 0, error: 'T5 download: ' + dl.error };
  var data = Array.isArray(dl.json) ? dl.json : [];
  var PSEUDO = { 'Всего находится на складах': 1, 'В пути до получателей': 1, 'В пути возвраты на склад WB': 1 };
  var sum = 0;
  for (var i = 0; i < data.length; i++) {
    var whs = data[i].warehouses || [];
    for (var w = 0; w < whs.length; w++) {
      var name = String(whs[w].warehouseName || whs[w].warehouse || '');
      if (!PSEUDO[name]) sum += Number(whs[w].quantity || 0);
    }
  }
  return { ok: true, sum: Math.round(sum), error: '' };
}

// ───────────────────────────────────────────────────────────────
// ВАЛИДАЦИЯ (весь пакет до записи) + НОРМАЛИЗАЦИЯ
// ───────────────────────────────────────────────────────────────

/**
 * Валидация всего пакета T6 ДО записи. Пустой ответ для действующего кабинета =
 * ERROR (не принимаем за нулевой остаток). Проверяет типы и уникальность ключа
 * nmId|chrtId|warehouseId (distinct==rows, dup==0). { ok, error, distinctKeys, duplicateKeys }.
 */
function wbStocksValidateT6_(data) {
  if (!Array.isArray(data)) return { ok: false, error: 'T6 ответ не массив' };
  if (data.length === 0) return { ok: false, error: 'T6 вернул 0 строк — для действующего кабинета трактуем как ERROR.' };
  var keySeen = {}, dup = 0;
  for (var i = 0; i < data.length; i++) {
    var o = data[i];
    var nm = wbStocksInt_(o.nmId !== undefined ? o.nmId : (o.nmid !== undefined ? o.nmid : o.nm_id));
    var chrt = wbStocksInt_(o.chrtId !== undefined ? o.chrtId : o.chrt_id);
    var wh = wbStocksInt_(o.warehouseId !== undefined ? o.warehouseId : o.warehouse_id);
    if (nm === null || nm <= 0) return { ok: false, error: 'Строка #' + (i + 1) + ': nmId не положительный INT64' };
    if (chrt === null) return { ok: false, error: 'Строка #' + (i + 1) + ': chrtId не INT64' };
    if (wh === null || wh < 0) return { ok: false, error: 'Строка #' + (i + 1) + ': warehouseId не INT64 ≥0' };
    var q = wbStocksInt_(o.quantity !== undefined ? o.quantity : o.qty);
    var t = wbStocksInt_(o.inWayToClient !== undefined ? o.inWayToClient : o.in_way_to_client);
    var f = wbStocksInt_(o.inWayFromClient !== undefined ? o.inWayFromClient : o.in_way_from_client);
    if (q === null || q < 0) return { ok: false, error: 'Строка #' + (i + 1) + ': quantity не целое ≥0' };
    if (t === null || t < 0) return { ok: false, error: 'Строка #' + (i + 1) + ': inWayToClient не целое ≥0' };
    if (f === null || f < 0) return { ok: false, error: 'Строка #' + (i + 1) + ': inWayFromClient не целое ≥0' };
    var key = nm + '|' + chrt + '|' + wh;
    if (keySeen[key]) dup++; else keySeen[key] = true;
  }
  if (dup > 0) return { ok: false, error: 'T6: дубли ключа nmId|chrtId|warehouseId = ' + dup + ' (ожидалось 0)' };
  return { ok: true, error: '', distinctKeys: Object.keys(keySeen).length, duplicateKeys: 0 };
}

/** Нормализация T6 → RAW-объекты + метрики снимка. SKU-привязка по nmId (у T6 нет barcode). */
function wbStocksNormalize_(data, snapshotId, snapshotTsIso, snapshotDate, loadId, skuIndex) {
  var rows = [], nmSet = {}, whSet = {}, qtyPos = 0, qtyZero = 0, aggRows = 0, sumAll = 0, sumPhys = 0, unmatched = {};
  for (var i = 0; i < data.length; i++) {
    var o = data[i];
    var nm = wbStocksInt_(o.nmId !== undefined ? o.nmId : (o.nmid !== undefined ? o.nmid : o.nm_id));
    var chrt = wbStocksInt_(o.chrtId !== undefined ? o.chrtId : o.chrt_id);
    var wh = wbStocksInt_(o.warehouseId !== undefined ? o.warehouseId : o.warehouse_id);
    var whName = String(o.warehouseName || o.warehouse || '');
    var region = String(o.regionName || o.region || '');
    var q = wbStocksInt_(o.quantity !== undefined ? o.quantity : o.qty) || 0;
    var t = wbStocksInt_(o.inWayToClient !== undefined ? o.inWayToClient : o.in_way_to_client) || 0;
    var f = wbStocksInt_(o.inWayFromClient !== undefined ? o.inWayFromClient : o.in_way_from_client) || 0;
    var isAgg = (wh === 0 || whName === WB_STOCKS_AGG_WH_NAME_);

    var nmStr = (typeof normalizeNmIdFinance_ === 'function') ? normalizeNmIdFinance_(nm) : String(nm);
    var internalSku = '', matchStatus = 'not_found';
    if (nmStr && skuIndex && skuIndex.byNm && skuIndex.byNm[nmStr]) {
      internalSku = skuIndex.byNm[nmStr].sku || ''; matchStatus = 'matched';
    } else {
      unmatched[String(nm)] = true;
    }

    rows.push({
      load_id: loadId, snapshot_id: snapshotId, snapshot_ts: snapshotTsIso, source_api: WB_STOCKS_SOURCE_API_,
      nm_id: nm, chrt_id: chrt, warehouse_id: wh, warehouse_name: whName, region_name: region,
      quantity: q, in_way_to_client: t, in_way_from_client: f, is_aggregate_warehouse: isAgg,
      internal_sku: internalSku, sku_match_status: matchStatus, raw_json: JSON.stringify(o),
      _snapshot_date: snapshotDate
    });

    if (nm != null) nmSet[nm] = true;
    if (whName) whSet[whName] = true;
    if (q > 0) qtyPos++; else qtyZero++;
    sumAll += q;
    if (!isAgg) sumPhys += q;
    if (isAgg) aggRows++;
  }
  return { rows: rows, metrics: {
    expected_rows: rows.length, unique_nm_ids: Object.keys(nmSet).length,
    warehouses_count: Object.keys(whSet).length, qty_positive_rows: qtyPos, qty_zero_rows: qtyZero,
    aggregate_warehouse_rows: aggRows, sum_quantity_all_t6: sumAll, sum_quantity_physical_t6: sumPhys,
    unmatched_list: Object.keys(unmatched) } };
}

// ───────────────────────────────────────────────────────────────
// ЗАПУСК СНИМКА
// ───────────────────────────────────────────────────────────────

/**
 * Один суточный снимок под ОБЩИМ ScriptLock. Параллельный запуск = SKIPPED_LOCKED.
 * Любая ошибка после manifest STARTED → manifest ERROR (если ещё не финализирован).
 */
function runWbStocksSnapshot() {
  var t0 = Date.now();
  var r = { status: 'ERROR', snapshot_id: '', started_at_iso: '', period_from: '', period_to: '',
    error_message: '', control_status: '', metrics: null, written_rows: 0,
    _manifestStarted: false, _manifestFinalized: false };

  var lock = LockService.getScriptLock();
  if (!lock.tryLock(1000)) {
    r.status = 'SKIPPED_LOCKED';
    r.error_message = 'Активен другой запуск (общий ScriptLock) — снимок пропущен.';
    wbStocksSafeSheetLog_(r, Date.now() - t0);
    console.log('STOCKS: ' + r.status + ' | ' + r.error_message);
    return r;
  }
  try {
    wbStocksSnapshotCore_(r);
  } catch (e) {
    r.status = 'ERROR';
    r.error_message = 'Исключение: ' + ((e && e.message) || e);
    if (r.snapshot_id && r._manifestStarted && !r._manifestFinalized) {
      try { wbStocksBqManifestFinalize_(r.snapshot_id, 'ERROR', r.metrics || {}, r.error_message); r._manifestFinalized = true; }
      catch (e2) { console.error('Manifest ERROR finalize не удался: ' + ((e2 && e2.message) || e2)); }
    }
  } finally {
    lock.releaseLock();
  }
  wbStocksSafeSheetLog_(r, Date.now() - t0);
  console.log('STOCKS snapshot: ' + r.status + ' | id=' + r.snapshot_id + ' | written=' + r.written_rows +
    ' | control=' + r.control_status + (r.error_message ? ' | ' + r.error_message : ''));
  return r;
}

/** Ядро снимка (мутирует r). Порядок строго по C2. */
function wbStocksSnapshotCore_(r) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();

  // sink OFF → runtime ERROR, БЕЗ manifest (C2: не обещаем manifest при выключенном приёмнике).
  if (!wbStocksBqSinkOn_()) {
    r.status = 'ERROR'; r.error_message = 'WB_STOCKS_BQ_SINK выключен — снимок только в BigQuery (manifest не пишем).';
    return;
  }
  // BQ availability: таблицы.
  wbStocksBqEnsureRaw_();
  wbStocksBqEnsureManifest_();

  var now = new Date();
  r.started_at_iso = now.toISOString();
  var snapshotDate = Utilities.formatDate(now, WB_STOCKS_TZ_, 'yyyy-MM-dd');
  var stamp = Utilities.formatDate(now, WB_STOCKS_TZ_, 'yyyyMMdd_HHmmss');
  var snapshotId = 'STOCK_SNAP_' + stamp + '_' + wbStocksShortUuid_();
  r.snapshot_id = snapshotId;
  var from = snapshotDate, to = snapshotDate;   // today..today МСК (B2: период не влияет; хардкода нет)
  r.period_from = from; r.period_to = to;

  // C2: manifest STARTED ДО fetch.
  wbStocksBqManifestStart_(snapshotId, r.started_at_iso, from, to);
  r._manifestStarted = true;

  var tk = wbStocksGetToken_();
  if (!tk) {
    r.status = 'ERROR'; r.error_message = 'Нет токена Analytics (' + WB_STOCKS_TOKEN_KEYS_.join('/') + ').';
    wbStocksBqManifestFinalize_(snapshotId, 'ERROR', {}, r.error_message); r._manifestFinalized = true; return;
  }

  var t6 = wbStocksFetchT6_(tk.token, from, to);
  if (!t6.ok) {
    r.status = 'ERROR'; r.error_message = 'T6: ' + t6.error;
    wbStocksBqManifestFinalize_(snapshotId, 'ERROR', {}, r.error_message); r._manifestFinalized = true; return;
  }

  var val = wbStocksValidateT6_(t6.data);
  if (!val.ok) {
    r.status = 'ERROR'; r.error_message = val.error;
    wbStocksBqManifestFinalize_(snapshotId, 'ERROR', {}, r.error_message); r._manifestFinalized = true; return;
  }

  var skuIndex = (typeof buildSkuIndex_ === 'function') ? buildSkuIndex_(ss) : { byNm: {}, byBarcode: {} };
  var loadId = 'STOCK_LOAD_' + stamp;
  var norm = wbStocksNormalize_(t6.data, snapshotId, r.started_at_iso, snapshotDate, loadId, skuIndex);
  var m = norm.metrics;
  m.distinct_keys = val.distinctKeys;
  m.duplicate_keys = val.duplicateKeys;
  m.unmatched_nm_ids = JSON.stringify(m.unmatched_list || []);

  // T5-контроль (НЕ блокирует).
  var t5 = wbStocksT5PhysicalSum_(tk.token);
  if (!t5.ok) {
    m.control_status = 'T5_UNAVAILABLE'; m.t5_control_sum = null; m.control_delta = null;
    console.log('STOCKS control: T5 недоступен — ' + t5.error);
  } else {
    m.t5_control_sum = t5.sum;
    m.control_delta = Math.abs(t5.sum - m.sum_quantity_physical_t6);
    m.control_status = (m.control_delta <= WB_STOCKS_CONTROL_TOLERANCE_) ? 'OK' : 'MISMATCH';
  }
  r.control_status = m.control_status;
  r.metrics = m;

  // RAW append (детерминированные batch jobId — C3).
  wbStocksBqAppendRaw_(norm.rows, snapshotId);

  // Пост-проверка фактически записанного.
  var cnt = wbStocksBqSnapshotCounts_(snapshotId, snapshotDate);
  m.written_rows = cnt.count;
  r.written_rows = cnt.count;
  var okCounts = (cnt.count === m.expected_rows && cnt.distinct === m.expected_rows);
  if (!okCounts) {
    r.status = 'ERROR';
    r.error_message = 'Пост-проверка не сошлась: expected=' + m.expected_rows + ' written=' + cnt.count +
      ' distinct=' + cnt.distinct;
    wbStocksBqManifestFinalize_(snapshotId, 'ERROR', m, r.error_message); r._manifestFinalized = true; return;
  }

  wbStocksBqManifestFinalize_(snapshotId, 'COMPLETE', m, '');
  r._manifestFinalized = true;
  r.status = 'OK';
}

/** Best-effort запись в Sheet IMPORT_LOG_STOCKS (НЕ источник истины; книга у лимита). */
function wbStocksSafeSheetLog_(r, durationMs) {
  try {
    var ss = SpreadsheetApp.getActiveSpreadsheet();
    var headers = ['snapshot_id', 'started_at', 'status', 'period_from', 'period_to',
      'written_rows', 'control_status', 'error_message', 'duration_ms'];
    var sh = ss.getSheetByName(WB_STOCKS_LOG_SHEET_);
    if (!sh) {
      sh = ss.insertSheet(WB_STOCKS_LOG_SHEET_);
      sh.getRange(1, 1, 1, headers.length).setValues([headers]); sh.setFrozenRows(1);
    } else if (sh.getLastRow() < 1) {
      sh.getRange(1, 1, 1, headers.length).setValues([headers]); sh.setFrozenRows(1);
    }
    var row = [r.snapshot_id, r.started_at_iso, r.status, r.period_from, r.period_to,
      r.written_rows, r.control_status, r.error_message, durationMs];
    sh.getRange(sh.getLastRow() + 1, 1, 1, row.length).setValues([row]);
  } catch (e) { /* best-effort; manifest — источник истины */ }
}

// ───────────────────────────────────────────────────────────────
// ТРИГГЕР (идемпотентно; ставит владелец после приёмки)
// ───────────────────────────────────────────────────────────────

/** Дневной триггер runWbStocksSnapshot ~06:30 МСК; 0→1/1→1/2+→1. Только свой обработчик. */
function wbStocksInstallDailyTrigger() {
  var trs = ScriptApp.getProjectTriggers(), mine = [];
  for (var i = 0; i < trs.length; i++) if (trs[i].getHandlerFunction() === WB_STOCKS_TRIGGER_FN_) mine.push(trs[i]);
  if (mine.length === 0) {
    ScriptApp.newTrigger(WB_STOCKS_TRIGGER_FN_).timeBased().everyDays(1).atHour(6).nearMinute(30).create();
    console.log('✅ Дневной триггер остатков создан (~06:30 МСК): ' + WB_STOCKS_TRIGGER_FN_);
    return { created: 1, removed: 0, total: 1 };
  }
  var removed = 0;
  for (var j = 1; j < mine.length; j++) { ScriptApp.deleteTrigger(mine[j]); removed++; }
  console.log(mine.length === 1 ? 'ℹ️ Триггер остатков уже есть.' : '⚠️ Удалены дубли: ' + removed + ', оставлен 1.');
  return { created: 0, removed: removed, total: 1 };
}

/** Удаляет ВСЕ триггеры runWbStocksSnapshot (другие обработчики не трогает). */
function wbStocksRemoveTrigger() {
  var trs = ScriptApp.getProjectTriggers(), n = 0;
  for (var i = 0; i < trs.length; i++) if (trs[i].getHandlerFunction() === WB_STOCKS_TRIGGER_FN_) { ScriptApp.deleteTrigger(trs[i]); n++; }
  console.log('🗑 Удалено триггеров остатков: ' + n);
  return { removed: n };
}
