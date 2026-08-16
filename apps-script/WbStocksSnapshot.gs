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

// T5 после обезличивания 16.08.2026: один агрегат «Склад WB РФ» + поимённо те склады,
// которые WB обещал оставить видимыми, + три псевдо-строки.
var WB_STOCKS_T5_AGG_NAME_    = 'Склад WB РФ';
// ── Детектор доступности (16.08.2026) ──
// Пороги выведены из наблюдаемой истории складов, а не назначены произвольно:
// самый маленький значимый склад в нашем контуре давал ~159 ед (Владимир),
// самый большой — 1 109 (Коледино). При агрегате порядка 2,4 тыс это 6–45%.
// Поэтому «резкий скачок» = не меньше 100 ед И не меньше 5% агрегата: ниже этой
// планки лежат обычные суточные колебания продаж и возвратов, выше — выбытие склада.
// ⚠️ Это калибровочная бизнес-гипотеза, а не гарантия: склад с остатком ~70 ед
// намеренно НЕ даст WAREHOUSE_DROP, он попадёт в DEGRADED. Это цена защиты от
// ложных тревог. Пересматривать пороги только после того, как накопится
// распределение обычного gap_delta за несколько суток.
var WB_STOCKS_AVAIL_DROP_ABS_   = 100;    // ед., нижняя граница «резкого скачка»
var WB_STOCKS_AVAIL_DROP_REL_   = 0.05;   // доля агрегата
var WB_STOCKS_AVAIL_OK_REL_     = 0.01;   // разрыв ≤1% агрегата считаем шумом

var WB_STOCKS_T5_PSEUDO_      = {
  'Всего находится на складах': 'PSEUDO_TOTAL',
  'В пути до получателей':      'PSEUDO_TO_CLIENT',
  'В пути возвраты на склад WB':'PSEUDO_FROM_CLIENT'
};

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

/**
 * Разбор склада из строки T6.
 *
 * 🔴 16.08.2026 — ФАКТ, снят пробой `probeWbStocksWarehouseShape()` в 11:37 МСК:
 * WB обезличил склады в T6. Вместо 241 строки по 39 складам приходит 23 строки
 * (по одной на chrtId), и во ВСЕХ:
 *     warehouseId = -999999,  warehouseName = "Склад WB",  regionName = "Склад WB"
 * `-999999` — это сентинел «склад не раскрываем», а не идентификатор. Прежняя
 * валидация требовала `warehouseId >= 0` и роняла весь снимок на первой строке.
 *
 * Теперь: реальный неотрицательный id сохраняем в `warehouse_id`; сентинел
 * (и любое отрицательное/нечисловое значение) кладём строкой в `warehouse_code`,
 * а `warehouse_id` = NULL. Ключ грейна — код склада, а если его нет — имя.
 * Снимок падает, только если склад не опознать ничем.
 *
 * @return {{id:(number|null), code:string, name:string, key:string, anonymized:boolean}}
 */
var WB_STOCKS_ANON_WH_ID_ = -999999;   // сентинел WB: «склад обезличен»

function wbStocksWarehouse_(o) {
  var raw = (o.warehouseId !== undefined) ? o.warehouseId : o.warehouse_id;
  var code = (raw === null || raw === undefined) ? '' : String(raw).trim();
  var id = wbStocksInt_(raw);
  var anon = (id === null || id < 0);          // -999999 и любое отрицательное — не идентификатор
  if (anon) id = null;
  var name = String(o.warehouseName || o.warehouse || '').trim();
  return { id: id, code: code, name: name, key: (code !== '' ? code : name), anonymized: anon };
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
  var sum = 0, aggSum = 0, namedSum = 0;
  for (var i = 0; i < data.length; i++) {
    var whs = data[i].warehouses || [];
    for (var w = 0; w < whs.length; w++) {
      var name = String(whs[w].warehouseName || whs[w].warehouse || '').trim();
      if (WB_STOCKS_T5_PSEUDO_[name]) continue;              // псевдо-строки в сумму не идут
      var q = Number(whs[w].quantity || 0);
      sum += q;
      if (name === WB_STOCKS_T5_AGG_NAME_) aggSum += q; else namedSum += q;
    }
  }
  return { ok: true, sum: Math.round(sum), aggSum: Math.round(aggSum),
           namedSum: Math.round(namedSum), data: data, error: '' };
}

/**
 * Нормализация T5 → строки RAW_WB_STOCKS_T5.
 *
 * 🔴 Зачем это вообще хранится. С 16.08.2026 T6 отдаёт ТОЛЬКО обезличенный
 * агрегат «Склад WB» — товар на поимённых складах (Электросталь, Краснодар,
 * Тула, Самара, Сарапул, Волгоград, СПБ Шушары) из T6 просто исчез. В T5 он
 * остался. Для нас это ровно тот остаток, что сгорел: пока WB держит его на
 * балансе, мы обязаны его видеть — это предмет заявки на компенсацию.
 *
 * Грейн: snapshot × nmId × barcode × techSize × warehouseName.
 * row_type: WAREHOUSE (реальный склад) | AGGREGATE («Склад WB РФ») | PSEUDO_*.
 */
function wbStocksNormalizeT5_(data, snapshotId, snapshotTsIso, snapshotDate, loadId, skuIndex) {
  var rows = [];
  for (var i = 0; i < data.length; i++) {
    var o = data[i];
    var nm = wbStocksInt_(o.nmId !== undefined ? o.nmId : o.nm_id);
    var nmStr = (typeof normalizeNmIdFinance_ === 'function') ? normalizeNmIdFinance_(nm) : String(nm);
    var internalSku = '', matchStatus = 'not_found';
    if (nmStr && skuIndex && skuIndex.byNm && skuIndex.byNm[nmStr]) {
      internalSku = skuIndex.byNm[nmStr].sku || ''; matchStatus = 'matched';
    }
    var whs = o.warehouses || [];
    for (var w = 0; w < whs.length; w++) {
      var name = String(whs[w].warehouseName || whs[w].warehouse || '').trim();
      var rowType = WB_STOCKS_T5_PSEUDO_[name] ||
                    (name === WB_STOCKS_T5_AGG_NAME_ ? 'AGGREGATE' : 'WAREHOUSE');
      rows.push({
        load_id: loadId, snapshot_id: snapshotId, snapshot_ts: snapshotTsIso,
        source_api: 'WB_API_STOCKS_T5',
        nm_id: nm, barcode: String(o.barcode || ''), tech_size: String(o.techSize || ''),
        vendor_code: String(o.vendorCode || ''), volume: Number(o.volume || 0),
        warehouse_name: name, row_type: rowType,
        quantity: wbStocksInt_(whs[w].quantity) || 0,
        internal_sku: internalSku, sku_match_status: matchStatus,
        _snapshot_date: snapshotDate
      });
    }
  }
  return { rows: rows };
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
    var wh = wbStocksWarehouse_(o);
    if (nm === null || nm <= 0) return { ok: false, error: 'Строка #' + (i + 1) + ': nmId не положительный INT64' };
    if (chrt === null) return { ok: false, error: 'Строка #' + (i + 1) + ': chrtId не INT64' };
    if (wh.key === '') return { ok: false, error: 'Строка #' + (i + 1) + ': склад не опознан — пусты и warehouseId, и warehouseName' };
    var q = wbStocksInt_(o.quantity !== undefined ? o.quantity : o.qty);
    var t = wbStocksInt_(o.inWayToClient !== undefined ? o.inWayToClient : o.in_way_to_client);
    var f = wbStocksInt_(o.inWayFromClient !== undefined ? o.inWayFromClient : o.in_way_from_client);
    if (q === null || q < 0) return { ok: false, error: 'Строка #' + (i + 1) + ': quantity не целое ≥0' };
    if (t === null || t < 0) return { ok: false, error: 'Строка #' + (i + 1) + ': inWayToClient не целое ≥0' };
    if (f === null || f < 0) return { ok: false, error: 'Строка #' + (i + 1) + ': inWayFromClient не целое ≥0' };
    var key = nm + '|' + chrt + '|' + wh.key;
    if (keySeen[key]) dup++; else keySeen[key] = true;
  }
  if (dup > 0) return { ok: false, error: 'T6: дубли ключа nmId|chrtId|склад = ' + dup + ' (ожидалось 0)' };
  return { ok: true, error: '', distinctKeys: Object.keys(keySeen).length, duplicateKeys: 0 };
}

/** Нормализация T6 → RAW-объекты + метрики снимка. SKU-привязка по nmId (у T6 нет barcode). */
function wbStocksNormalize_(data, snapshotId, snapshotTsIso, snapshotDate, loadId, skuIndex) {
  var rows = [], nmSet = {}, whSet = {}, qtyPos = 0, qtyZero = 0, aggRows = 0, sumAll = 0, sumPhys = 0, unmatched = {};
  var sumInWayFrom = 0;
  var anonWh = 0;   // строк с обезличенным складом (warehouseId = -999999) — индикатор перехода WB
  for (var i = 0; i < data.length; i++) {
    var o = data[i];
    var nm = wbStocksInt_(o.nmId !== undefined ? o.nmId : (o.nmid !== undefined ? o.nmid : o.nm_id));
    var chrt = wbStocksInt_(o.chrtId !== undefined ? o.chrtId : o.chrt_id);
    var wh = wbStocksWarehouse_(o);
    var whName = wh.name;
    var region = String(o.regionName || o.region || '');
    var q = wbStocksInt_(o.quantity !== undefined ? o.quantity : o.qty) || 0;
    var t = wbStocksInt_(o.inWayToClient !== undefined ? o.inWayToClient : o.in_way_to_client) || 0;
    var f = wbStocksInt_(o.inWayFromClient !== undefined ? o.inWayFromClient : o.in_way_from_client) || 0;
    var isAgg = (wh.id === 0 || wh.code === '0' || whName === WB_STOCKS_AGG_WH_NAME_);
    if (wh.anonymized) anonWh++;

    var nmStr = (typeof normalizeNmIdFinance_ === 'function') ? normalizeNmIdFinance_(nm) : String(nm);
    var internalSku = '', matchStatus = 'not_found';
    if (nmStr && skuIndex && skuIndex.byNm && skuIndex.byNm[nmStr]) {
      internalSku = skuIndex.byNm[nmStr].sku || ''; matchStatus = 'matched';
    } else {
      unmatched[String(nm)] = true;
    }

    rows.push({
      load_id: loadId, snapshot_id: snapshotId, snapshot_ts: snapshotTsIso, source_api: WB_STOCKS_SOURCE_API_,
      nm_id: nm, chrt_id: chrt, warehouse_id: wh.id, warehouse_code: wh.code,
      warehouse_name: whName, region_name: region,
      quantity: q, in_way_to_client: t, in_way_from_client: f, is_aggregate_warehouse: isAgg,
      internal_sku: internalSku, sku_match_status: matchStatus, raw_json: JSON.stringify(o),
      _snapshot_date: snapshotDate
    });

    if (nm != null) nmSet[nm] = true;
    if (wh.key) whSet[wh.key] = true;
    if (q > 0) qtyPos++; else qtyZero++;
    sumInWayFrom += f;
    sumAll += q;
    if (!isAgg) sumPhys += q;
    if (isAgg) aggRows++;
  }
  return { rows: rows, metrics: {
    expected_rows: rows.length, unique_nm_ids: Object.keys(nmSet).length,
    warehouses_count: Object.keys(whSet).length, qty_positive_rows: qtyPos, qty_zero_rows: qtyZero,
    aggregate_warehouse_rows: aggRows, sum_quantity_all_t6: sumAll, sum_quantity_physical_t6: sumPhys,
    anonymized_warehouse_rows: anonWh, sum_in_way_from_client: sumInWayFrom,
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

  // `t6_comparable` зависит ТОЛЬКО от T6, поэтому считается и сохраняется ДО обращения
  // к T5: если второй источник недоступен, эту величину мы всё равно знаем, и терять
  // её незачем — диагностический слой хранит входы независимо от судьбы соседей.
  //
  // 🔴 16.08.2026, ОПРОВЕРГНУТАЯ ГИПОТЕЗА — не возвращать слагаемое обратно.
  // Была версия, что `quantity` в T5 включает возвраты, едущие на склад, и потому
  // сравнивать надо с `sum_quantity_physical_t6 + sum_in_way_from_client`. Она
  // родилась из ОДНОГО совпадения (T5 агрегат 2445 против 2405 + 38 = 2443).
  // Опровергается замером 15.08, где сходимость была точной БЕЗ всякой поправки:
  //     T6 physical 4741 == T5 control 4741, delta 0, при in_way_from_client 37.
  // Будь слагаемое верным, T5 показал бы 4778. И контрольный факт: 16.08 разрыв
  // без поправки гулял +40 → +23 при in_way_from_client 38 → 41 — связи нет.
  // Со слагаемым детектор давал воспроизводимый ложный DATA_ERROR (gap −18).
  // `sum_in_way_from_client` остаётся самостоятельной наблюдаемой метрикой,
  // но в инвариант T5↔T6 не входит.
  var t6Comparable = m.sum_quantity_physical_t6;
  m.t6_comparable = t6Comparable;

  // T5-контроль (НЕ блокирует) + сохранение детализации T5.
  var t5 = wbStocksT5PhysicalSum_(tk.token);
  if (!t5.ok) {
    m.control_status = 'T5_UNAVAILABLE'; m.t5_control_sum = null; m.control_delta = null;
    m.t5_wb_rf_sum = null; m.t5_named_sum = null; m.t5_rows_written = 0;
    // t6_comparable НЕ обнуляем — это независимая метрика T6.
    m.availability_gap = null; m.availability_gap_prev = null; m.availability_gap_delta = null;
    m.availability_ratio = null; m.availability_status = 'T5_UNAVAILABLE';
    console.log('STOCKS control: T5 недоступен — ' + t5.error);
  } else {
    m.t5_control_sum = t5.sum;        // все реальные склады T5 (агрегат + поимённые)
    m.t5_wb_rf_sum   = t5.aggSum;     // только «Склад WB РФ» — сопоставим с T6
    m.t5_named_sum   = t5.namedSum;   // поимённые склады — их T6 больше не показывает

    // 🔴 Сопоставляем T6 с АГРЕГАТОМ T5, а не с его общим итогом: с 16.08.2026 T6
    // отдаёт только «Склад WB», а поимённые склады живут отдельной частью T5.
    // Инвариант: T5 total = aggregate + named — балансовый контур; T5 aggregate ↔
    // T6 physical — доступный контур. Потоки (inWayToClient/inWayFromClient) в
    // инвариант не входят, см. опровержение выше.
    m.control_delta = Math.abs(t5.aggSum - t6Comparable);
    m.control_status = (m.control_delta <= WB_STOCKS_CONTROL_TOLERANCE_) ? 'OK' : 'MISMATCH';

    // 🔴 ДЕТЕКТОР ДОСТУПНОСТИ. Равенство T5-агрегата и T6 перестало быть инвариантом
    // 16.08.2026: Коледино выпало из продаваемого T6, оставшись в балансовом T5, и
    // разрыв скакнул с ~2 до 1 147. Поэтому вместо pass/fail разделяем две разные
    // вещи: целостность данных (T6 не может быть БОЛЬШЕ баланса) и бизнес-событие
    // (склад выбыл из продаваемого контура). Поля отдельные — control_* остаются
    // историческими, чтобы накопленные значения не стали несопоставимы.
    m.availability_gap = t5.aggSum - t6Comparable;
    m.availability_ratio = t5.aggSum > 0 ? (t6Comparable / t5.aggSum) : null;
    try { m.availability_gap_prev = wbStocksBqPrevAvailabilityGap_(); }
    catch (ePrev) { m.availability_gap_prev = null; console.error('availability_gap_prev не прочитан: ' + ((ePrev && ePrev.message) || ePrev)); }
    m.availability_gap_delta = (m.availability_gap_prev === null || m.availability_gap_prev === undefined)
      ? null : (m.availability_gap - m.availability_gap_prev);

    var dropThreshold = Math.max(WB_STOCKS_AVAIL_DROP_ABS_, Math.round(t5.aggSum * WB_STOCKS_AVAIL_DROP_REL_));
    var okThreshold   = Math.max(WB_STOCKS_CONTROL_TOLERANCE_, Math.round(t5.aggSum * WB_STOCKS_AVAIL_OK_REL_));

    if (t6Comparable > t5.aggSum + WB_STOCKS_CONTROL_TOLERANCE_) {
      // Продаваемого больше, чем на балансе — так не бывает: один из источников врёт.
      m.availability_status = 'DATA_ERROR';
    } else if (m.availability_gap_delta === null) {
      m.availability_status = 'NO_BASELINE';   // не с чем сравнивать: первый снимок с детектором
    } else if (m.availability_gap_delta >= dropThreshold) {
      m.availability_status = 'WAREHOUSE_DROP';
    } else if (m.availability_gap > okThreshold) {
      m.availability_status = 'DEGRADED';
    } else {
      m.availability_status = 'OK';
    }
    console.log('STOCKS availability: T5агрегат ' + t5.aggSum + ' | T6 доступно ' + t6Comparable +
                ' | gap ' + m.availability_gap +
                ' | gap_delta ' + (m.availability_gap_delta === null ? 'н/д' : m.availability_gap_delta) +
                ' | ratio ' + (m.availability_ratio === null ? 'н/д' : m.availability_ratio.toFixed(3)) +
                ' | status ' + m.availability_status);

    // Детализация T5 — best-effort: её потеря не должна валить снимок T6.
    try {
      wbStocksBqEnsureT5_();
      var t5n = wbStocksNormalizeT5_(t5.data, snapshotId, r.started_at_iso, snapshotDate, loadId, skuIndex);
      wbStocksBqAppendT5_(t5n.rows, snapshotId);
      m.t5_rows_written = t5n.rows.length;
      console.log('STOCKS T5 detail: записано строк ' + t5n.rows.length +
                  ' (агрегат ' + t5.aggSum + ', поимённые склады ' + t5.namedSum + ')');
    } catch (eT5) {
      m.t5_rows_written = 0;
      console.error('STOCKS T5 detail НЕ сохранён (снимок T6 не затронут): ' + ((eT5 && eT5.message) || eT5));
    }
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
