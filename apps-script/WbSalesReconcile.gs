/**
 * ══════════════════════════════════════════════════════════════
 * EVETIS WB — WbSalesReconcile.gs   (Фаза D2c — Night Reconciliation)
 *
 * Ночная пересверка продаж/возвратов. Закрывает eventual-consistency Sales API:
 * строка может всплыть ПОЗЖЕ с lastChangeDate < watermark (hourly её не увидит),
 * плюс пропуски от сбоев/429. Это правильное место для RANGE-WIDE дедупа
 * (в hourly он сознательно НЕ делается).
 *
 * ПРИНЦИПЫ:
 *   • окно: dateFrom = полночь МСК (сегодня − N дней), N = WB_SALES_RECONCILE_DAYS
 *     (Script Property, дефолт 7, валидируется 1..90);
 *   • один fail-closed запрос, noWindow (без фильтра sale_dt), та же валидация
 *     ВСЕХ сырых API-строк ДО нормализации + страховка rows.length===data.length;
 *   • ключ состояния — sale_id | md5(raw_json) (полное состояние, НЕ row_hash);
 *   • внутрипакетно дедуп по sale_id|state — оставляем ВСЕ различные состояния
 *     (НЕ last-wins: цель — залатать все отсутствующие состояния, а не только
 *     самое свежее);
 *   • range-wide набор состояний из RAW (wbSalesBqStateKeysSince_) за то же окно
 *     по last_change_date; append ТОЛЬКО тех sale_id|state, которых нет в RAW
 *     → gaps_filled;
 *   • watermark НЕ трогаем (им владеет hourly). В логе watermark_before ==
 *     watermark_after — авто-доказательство, что пересверка watermark не двигает;
 *   • RAW append-only (дубли состояний не пишутся, т.к. отсекаются набором),
 *     каноничность — во вью V_WB_SALES_RETURNS (last-wins по sale_id). Повторный
 *     прогон после 65 сек физически идемпотентен (RAW не растёт).
 *
 * SHARED RATE-LIMIT GUARD (обязательное дополнение аудита):
 *   Один ScriptLock защищает от ОДНОВРЕМЕННОСТИ, но НЕ от двух ПОСЛЕДОВАТЕЛЬНЫХ
 *   запросов Sales API в пределах минуты (доказанный runtime 429: hourly и nightly
 *   могут стартовать с разницей в секунды). Поэтому ОБА пути (runWbSalesIncremental
 *   и runWbSalesNightReconcile) под общим ScriptLock, непосредственно перед fetch,
 *   вызывают wbSalesApiAcquireRequestSlot_(): если с последней ПОПЫТКИ прошло
 *   < 65 сек — SKIPPED_RATE_LIMIT без HTTP-вызова; иначе timestamp записывается
 *   ДО fetch (даже 429/500 = попытка к API) и выполняется ровно один запрос.
 *
 * СТАТУСЫ: OK / OK_NO_GAPS / SKIPPED_LOCKED / SKIPPED_RATE_LIMIT / ERROR.
 *
 * Файлы, которые НЕ трогаем: RAW-схема/вью/adapter (WbSalesConsumerSource)/
 * Finance/Ads/PNL. Триггер ставит владелец после ручной приёмки.
 * ══════════════════════════════════════════════════════════════
 */

var WB_SALES_RECONCILE_DAYS_PROP_ = 'WB_SALES_RECONCILE_DAYS';
var WB_SALES_RECONCILE_DEFAULT_DAYS_ = 7;
var WB_SALES_RECONCILE_MIN_DAYS_ = 1;
var WB_SALES_RECONCILE_MAX_DAYS_ = 90;   // Sales API держит ~90 дней; окно шире бессмысленно
var WB_SALES_RECON_TRIGGER_FN_ = 'runWbSalesNightReconcile';

// ═══════════════════════════════════════
//  SHARED RATE-LIMIT GUARD (hourly + nightly)
// ═══════════════════════════════════════

var WB_SALES_API_LAST_REQUEST_PROP_ = 'WB_SALES_API_LAST_REQUEST_AT_MS';
var WB_SALES_API_MIN_INTERVAL_MS_   = 65000;   // Sales API 1 req/min → страховочный зазор 65 сек

/**
 * Единый cooldown-слот на Sales API для hourly и nightly. ВЫЗЫВАТЬ ТОЛЬКО ПОД
 * общим ScriptLock, непосредственно ПЕРЕД fetchSalesApiData_ (иначе read-modify-write
 * свойства гонится между путями). Возвращает:
 *   { ok:true, at_ms }                          — слот выдан, timestamp записан ДО fetch;
 *   { ok:false, since_ms, remaining_ms }        — с прошлой попытки < 65 сек, НЕ вызывать API.
 * Timestamp пишется ДО HTTP-вызова намеренно: даже запрос, завершившийся 429/500,
 * считается попыткой к API и участвует в лимите (иначе повтор снова словит 429).
 */
function wbSalesApiAcquireRequestSlot_() {
  var props = PropertiesService.getScriptProperties();
  var lastStr = props.getProperty(WB_SALES_API_LAST_REQUEST_PROP_) || '';
  var last = lastStr ? parseInt(lastStr, 10) : 0;
  var now = Date.now();
  if (last && !isNaN(last) && last > 0) {
    var since = now - last;
    if (since < WB_SALES_API_MIN_INTERVAL_MS_) {
      return { ok: false, since_ms: since, remaining_ms: WB_SALES_API_MIN_INTERVAL_MS_ - since };
    }
  }
  props.setProperty(WB_SALES_API_LAST_REQUEST_PROP_, String(now));   // ДО fetch
  return { ok: true, at_ms: now };
}

// ═══════════════════════════════════════
//  КОНФИГ ОКНА
// ═══════════════════════════════════════

/** N дней пересверки: Script Property WB_SALES_RECONCILE_DAYS, дефолт 7, границы 1..90. */
function wbSalesReconcileDays_() {
  var raw = PropertiesService.getScriptProperties().getProperty(WB_SALES_RECONCILE_DAYS_PROP_);
  var n = raw ? parseInt(String(raw), 10) : WB_SALES_RECONCILE_DEFAULT_DAYS_;
  if (isNaN(n) || n < WB_SALES_RECONCILE_MIN_DAYS_ || n > WB_SALES_RECONCILE_MAX_DAYS_) {
    n = WB_SALES_RECONCILE_DEFAULT_DAYS_;
  }
  return n;
}

/**
 * dateFrom окна = полночь МСК (сегодня − N дней) в формате YYYY-MM-DDT00:00:00.
 * Одна и та же строка используется и как API dateFrom, и как fromLcd для BQ-набора
 * состояний (last_change_date >= fromLcd). МСК фиксирован UTC+3 (без DST) → сдвиг
 * на N*86400000 мс и приведение к календарной дате МСК корректны.
 */
function wbSalesReconcileFromLcd_(days) {
  var fromMs = Date.now() - days * 86400000;
  var day = Utilities.formatDate(new Date(fromMs), WB_SALES_INC_TZ_, 'yyyy-MM-dd');
  return day + 'T00:00:00';
}

// ═══════════════════════════════════════
//  СТАТУС / ДИАГНОСТИКА
// ═══════════════════════════════════════

/** Диагностика пересверки: окно (N дней, fromLcd), watermark hourly, sink, size набора состояний. */
function wbSalesReconcileStatus() {
  var days = wbSalesReconcileDays_();
  var fromLcd = wbSalesReconcileFromLcd_(days);
  var wm = PropertiesService.getScriptProperties().getProperty(WB_SALES_WATERMARK_PROP_) || '(нет)';
  var sinkOn = (typeof wbSalesBqSinkOn_ === 'function') && wbSalesBqSinkOn_();
  var lastReq = PropertiesService.getScriptProperties().getProperty(WB_SALES_API_LAST_REQUEST_PROP_) || '(нет)';
  var stateKeys = '';
  try {
    var set = wbSalesBqStateKeysSince_(fromLcd);
    stateKeys = String(Object.keys(set).length);
  } catch (e) { stateKeys = '(ошибка: ' + ((e && e.message) || e) + ')'; }
  console.log('RECON окно: ' + days + 'д (fromLcd ' + fromLcd + ') | watermark(hourly): ' + wm +
    ' | sink: ' + (sinkOn ? 'ВКЛ' : 'ВЫКЛ') + ' | state-keys за окно: ' + stateKeys +
    ' | last Sales API attempt(ms): ' + lastReq);
  try {
    var ui = SpreadsheetApp.getUi();
    ui.alert('Night Reconciliation (статус)',
      'Окно: ' + days + ' дн (fromLcd ' + fromLcd + ')\nwatermark (hourly): ' + wm +
      '\nsink: ' + (sinkOn ? 'ВКЛ' : 'ВЫКЛ') + '\nsale_id|state за окно: ' + stateKeys, ui.ButtonSet.OK);
  } catch (e2) {}
  return { days: days, from_lcd: fromLcd, watermark: wm, sink_on: sinkOn, state_keys: stateKeys };
}

// ═══════════════════════════════════════
//  ЗАПУСК ПЕРЕСВЕРКИ
// ═══════════════════════════════════════

/** Базовая структура результата пересверки (контракт лога SALE_RECON_). */
function salesReconBaseResult_() {
  return {
    status: 'ERROR', load_id: '',
    watermark_before: '', watermark_after: '',
    period_from: '', period_to: '',
    api_rows_received: 0, rows_after_boundary_dedup: 0, rows_written: 0, gaps_filled: 0,
    unique_saleID: 0,
    started_at: Utilities.formatDate(new Date(), WB_SALES_INC_TZ_, 'yyyy-MM-dd HH:mm:ss'),
    finished_at: '', duration_ms: 0, error_message: ''
  };
}

/**
 * Один запуск ночной пересверки под общим ScriptLock (тем же, что hourly).
 * Параллельный запуск = SKIPPED_LOCKED (не ошибка данных). watermark не трогаем
 * ни при каком исходе.
 */
function runWbSalesNightReconcile() {
  var t0 = Date.now();
  var r = salesReconBaseResult_();
  r.load_id = 'SALE_RECON_' + Utilities.formatDate(new Date(), WB_SALES_INC_TZ_, 'yyyyMMdd_HHmmss');

  var lock = LockService.getScriptLock();
  if (!lock.tryLock(1000)) {
    r.status = 'SKIPPED_LOCKED';
    r.error_message = 'Активен другой запуск (ScriptLock) — пересверка пропущена, watermark не тронут.';
    r.finished_at = Utilities.formatDate(new Date(), WB_SALES_INC_TZ_, 'yyyy-MM-dd HH:mm:ss');
    r.duration_ms = Date.now() - t0;
    salesReconSafeLog_(r);
    return r;
  }
  try {
    wbSalesNightReconcileCore_(r);
  } catch (e) {
    r.status = 'ERROR';
    r.error_message = 'Исключение: ' + ((e && e.message) || e);
    // watermark НЕ трогаем
  } finally {
    lock.releaseLock();
  }
  r.finished_at = Utilities.formatDate(new Date(), WB_SALES_INC_TZ_, 'yyyy-MM-dd HH:mm:ss');
  r.duration_ms = Date.now() - t0;
  salesReconSafeLog_(r);
  console.log('Night Reconcile: ' + r.status + ' | окно ' + r.period_from + '..' + r.period_to +
    ' | wm(before=after)=' + r.watermark_before +
    ' | api_rows=' + r.api_rows_received + ' state_dedup=' + r.rows_after_boundary_dedup +
    ' gaps_filled=' + r.gaps_filled + (r.error_message ? ' | ' + r.error_message : ''));
  return r;
}

/**
 * Ядро пересверки (мутирует r). Ранний выход — установить r.status и return.
 * Валидация ВСЕГО пакета выполняется ДО первого append.
 */
function wbSalesNightReconcileCore_(r) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();

  // Fail-closed: только при включённом BQ-приёмнике.
  if (!(typeof wbSalesBqSinkOn_ === 'function' && wbSalesBqSinkOn_())) {
    r.status = 'ERROR'; r.error_message = 'WB_SALES_BQ_SINK выключен — пересверка только в BigQuery.'; return;
  }
  var tk = getSalesToken_();
  if (!tk) { r.status = 'ERROR'; r.error_message = 'Нет токена WB (' + WB_SALES_TOKEN_KEYS_.join(' / ') + ').'; return; }

  // watermark hourly читаем только для лога (before == after — пересверка его не двигает).
  var wm = PropertiesService.getScriptProperties().getProperty(WB_SALES_WATERMARK_PROP_) || '';
  r.watermark_before = wm;
  r.watermark_after = wm;

  // Окно.
  var days = wbSalesReconcileDays_();
  var fromLcd = wbSalesReconcileFromLcd_(days);       // YYYY-MM-DDT00:00:00
  if (!salesValidWatermark_(fromLcd)) {               // та же строгая валидация, что watermark
    r.status = 'ERROR'; r.error_message = 'Невалидный fromLcd окна: ' + fromLcd; return;
  }
  r.period_from = fromLcd;
  r.period_to = Utilities.formatDate(new Date(), WB_SALES_INC_TZ_, 'yyyy-MM-dd HH:mm:ss');

  // SHARED RATE-LIMIT GUARD — под общим ScriptLock, непосредственно перед fetch.
  var slot = wbSalesApiAcquireRequestSlot_();
  if (!slot.ok) {
    r.status = 'SKIPPED_RATE_LIMIT';
    r.error_message = 'С прошлой попытки Sales API прошло ' + slot.since_ms + ' мс (< ' +
      WB_SALES_API_MIN_INTERVAL_MS_ + ') — HTTP не вызван, RAW/watermark неизменны.';
    return;
  }

  // Один fail-closed запрос. dateFrom = полночь МСК (сегодня − N дней).
  var fetched = fetchSalesApiData_(tk.token, fromLcd);
  r.api_rows_received = (fetched.api_rows_received != null) ? fetched.api_rows_received : ((fetched.data || []).length);
  if (!fetched.ok) {
    r.status = 'ERROR';
    r.error_message = fetched.error || 'Незавершённая выгрузка Sales API';
    return;
  }

  var data = fetched.data || [];
  if (data.length === 0 && r.api_rows_received === 0) {
    r.status = 'OK_NO_GAPS'; return;
  }

  // (1) Валидация ВСЕХ сырых API-строк ДО нормализации (нормализатор молча роняет
  //     строки без даты). Хоть одна плохая → пакет отклонён, RAW неизменен.
  for (var i = 0; i < data.length; i++) {
    var o = data[i];
    var oSid = String((o && o.saleID) || '').trim();
    var oDay = (typeof normalizeDateKey_ === 'function') ? normalizeDateKey_(o && o.date) : String((o && o.date) || '').substring(0, 10);
    var oLcd = String((o && o.lastChangeDate) || '');
    if (oSid === '' || !oDay || !salesValidWatermark_(oLcd)) {
      r.status = 'ERROR';
      r.error_message = 'Невалидная сырая API-строка #' + (i + 1) + ' (saleID/date/lastChangeDate) — пакет отклонён до записи.';
      return;
    }
  }

  // (2) Нормализация (noWindow). Все даты валидны → нормализатор ничего не роняет;
  //     строгая проверка длины как страховка.
  var rawSheet = getRawSalesSheet_(ss);
  var lastCol = rawSheet.getLastColumn();
  var hMap = buildSalesRawHeaderMap_(rawSheet, lastCol);
  var skuIndex = (typeof buildSkuIndex_ === 'function') ? buildSkuIndex_(ss) : { byNm: {}, byBarcode: {} };
  var rows = normalizeSalesApiRows_(data, hMap, lastCol, r.load_id, r.started_at, fromLcd, '', skuIndex, { noWindow: true });

  if (rows.length !== data.length) {
    r.status = 'ERROR';
    r.error_message = 'Нормализатор отбросил строки (' + data.length + '→' + rows.length + ') — пакет отклонён.';
    return;
  }

  var iSaleId = hMap['sale_id'], iRj = hMap['raw_json'];
  if (iSaleId === undefined || iRj === undefined) {
    r.status = 'ERROR'; r.error_message = 'В схеме RAW нет колонки sale_id или raw_json.'; return;
  }

  // (3) Внутрипакетный дедуп по STATE-ключу sale_id|md5(raw_json). В ОТЛИЧИЕ от
  //     hourly (last-wins по sale_id) — оставляем ВСЕ различные состояния (латаем
  //     каждый пропуск). Одинаковый state-ключ в пакете → одна строка. Только после
  //     этого сравниваем с BQ-набором.
  var byState = {};
  for (i = 0; i < rows.length; i++) {
    var row = rows[i];
    var sid = String(row[iSaleId] || '').trim();
    if (!sid) continue;                                  // без sale_id — в каноническую вью не входит
    var sh = salesMd5_(String(row[iRj] || ''));
    var key = sid + '|' + sh;
    if (!byState[key]) byState[key] = row;
  }
  var stateKeysInBatch = Object.keys(byState);
  r.rows_after_boundary_dedup = stateKeysInBatch.length;   // семантика в reconcile: строк после range-wide state-dedup (см. CHANGELOG)

  // (4) Range-wide набор уже присутствующих состояний за окно (last_change_date >= fromLcd).
  //     Append только тех sale_id|state, которых в RAW нет → gaps_filled.
  var existing = wbSalesBqStateKeysSince_(fromLcd);
  var toAppend = [];
  for (var k = 0; k < stateKeysInBatch.length; k++) {
    var key2 = stateKeysInBatch[k];
    if (!existing[key2]) toAppend.push(byState[key2]);
  }
  r.gaps_filled = toAppend.length;
  r.rows_written = toAppend.length;

  if (toAppend.length === 0) {
    r.status = 'OK_NO_GAPS'; return;
  }

  // Append (исключение → пробрасывается в runWbSalesNightReconcile → ERROR, watermark не трогаем).
  appendSalesRows_(rawSheet, toAppend, lastCol);

  try {
    var sums = aggregateSalesRowArray_(toAppend, hMap, '0000-01-01', '9999-12-31', { noWindow: true });
    r.unique_saleID = sums.unique_saleID != null ? sums.unique_saleID : (sums.unique_saleid != null ? sums.unique_saleid : 0);
  } catch (eS) { /* контрольные суммы не критичны для статуса */ }

  // watermark НЕ двигаем — им владеет hourly. (r.watermark_after уже == before.)
  r.status = 'OK';
}

/** Безопасная запись строки пересверки в IMPORT_LOG_SALES_RETURNS (исключение глушим). */
function salesReconSafeLog_(r) {
  try {
    var ss = SpreadsheetApp.getActiveSpreadsheet();
    writeSalesReconcileLogEntry_(ensureImportLogSalesSheet_(ss), r);
  } catch (e) {}
}

/**
 * Пишет строку пересверки в IMPORT_LOG_SALES_RETURNS по тому же расширенному
 * контракту (IMPORT_LOG_SALES_HEADERS_). period_from/period_to = окно; rows_imported
 * и rows_written = gaps_filled; watermark_before == watermark_after (доказательство,
 * что пересверка watermark не двигает).
 */
function writeSalesReconcileLogEntry_(logSheet, r) {
  if (!logSheet) return;
  var rowObj = {
    load_id: r.load_id, loaded_at: r.started_at,
    period_from: r.period_from, period_to: r.period_to,
    rows_imported: r.gaps_filled, unique_saleID: r.unique_saleID,
    status: r.status, error_message: r.error_message,
    watermark_before: r.watermark_before, watermark_after: r.watermark_after,
    api_rows_received: r.api_rows_received,
    rows_after_boundary_dedup: r.rows_after_boundary_dedup,
    rows_written: r.gaps_filled, duration_ms: r.duration_ms
  };
  var rowArr = [];
  for (var i = 0; i < IMPORT_LOG_SALES_HEADERS_.length; i++) {
    var k = IMPORT_LOG_SALES_HEADERS_[i];
    rowArr.push(rowObj[k] !== undefined ? rowObj[k] : '');
  }
  logSheet.getRange(logSheet.getLastRow() + 1, 1, 1, rowArr.length).setValues([rowArr]);
}

// ═══════════════════════════════════════
//  ТРИГГЕР (идемпотентно; ставит владелец после ручной приёмки)
// ═══════════════════════════════════════

/**
 * Идемпотентная установка ночного триггера runWbSalesNightReconcile:
 *   0 → создать 1; 1 → ничего; 2+ → удалить дубли, оставить 1.
 * Расписание everyDays(1).atHour(4).nearMinute(20) (≈ 04:20 МСК — снижает шанс
 * близости к hourly; сам guard дополнительно страхует от 429). Затрагивает ТОЛЬКО
 * обработчик runWbSalesNightReconcile (hourly/Orders/Finance/Ads не трогает).
 * Часовой пояс проекта Apps Script должен быть Europe/Moscow.
 */
function wbSalesReconcileInstallNightlyTrigger() {
  var trs = ScriptApp.getProjectTriggers();
  var mine = [];
  for (var i = 0; i < trs.length; i++) {
    if (trs[i].getHandlerFunction() === WB_SALES_RECON_TRIGGER_FN_) mine.push(trs[i]);
  }
  if (mine.length === 0) {
    ScriptApp.newTrigger(WB_SALES_RECON_TRIGGER_FN_).timeBased().everyDays(1).atHour(4).nearMinute(20).create();
    console.log('✅ Ночной триггер создан (04:20 МСК): ' + WB_SALES_RECON_TRIGGER_FN_);
    return { created: 1, removed: 0, total: 1 };
  }
  var removed = 0;
  for (var j = 1; j < mine.length; j++) { ScriptApp.deleteTrigger(mine[j]); removed++; }
  console.log(mine.length === 1
    ? 'ℹ️ Ночной триггер уже есть — ничего не создано.'
    : '⚠️ Удалены дубли ночного триггера: ' + removed + ', оставлен 1.');
  return { created: 0, removed: removed, total: 1 };
}

/** Удаляет ВСЕ триггеры обработчика runWbSalesNightReconcile (hourly/Orders/Finance/Ads не трогает). */
function wbSalesReconcileRemoveNightlyTrigger() {
  var trs = ScriptApp.getProjectTriggers();
  var n = 0;
  for (var i = 0; i < trs.length; i++) {
    if (trs[i].getHandlerFunction() === WB_SALES_RECON_TRIGGER_FN_) { ScriptApp.deleteTrigger(trs[i]); n++; }
  }
  console.log('🗑 Удалено ночных триггеров ' + WB_SALES_RECON_TRIGGER_FN_ + ': ' + n);
  return { removed: n };
}
