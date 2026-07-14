/**
 * ══════════════════════════════════════════════════════════════
 * EVETIS WB — WbSalesIncremental.gs   (Фаза D2c)
 *
 * Watermark-инкремент продаж/возвратов по образцу боевого D1.2 (заказы).
 * Ежечасно спрашивает WB «что изменилось после последнего успешного запуска»
 * (dateFrom = watermark по last_change_date), дописывает в RAW (append-only),
 * последнее состояние выбирает вью V_WB_SALES_RETURNS (last-wins по sale_id).
 *
 * ПРИНЦИПЫ (как в D1.2):
 *   • watermark = last_change_date (Sales хранит с 'T' → сравнение прямое);
 *   • noWindow — без фильтра sale_dt (поздно изменённая старая продажа приходит
 *     с sale_dt < dateFrom и должна сохраниться);
 *   • at-least-once: watermark двигается ТОЛЬКО после успешного append и только
 *     если есть строки строго новее (candidate > watermark_before);
 *   • граница секунды по STATE-ключу: строки last_change_date == watermark
 *     дописываются, только если состояния sale_id|md5(raw_json) ещё нет в RAW
 *     (wbSalesBqBoundaryStateKeys_) — row_hash для этого НЕ годится (не меняется
 *     при изменении цены/склада/скидки);
 *   • fail-closed / гарантии (точная формулировка at-least-once):
 *       – ДО append любая ошибка (sink OFF / нет watermark / ошибка API / битый
 *         ответ / невалидная сырая строка / отброс нормализатором) → RAW и
 *         watermark НЕИЗМЕННЫ;
 *       – ошибка ПОСЛЕ успешного append (напр. setProperty упал) → RAW дополнен,
 *         watermark остаётся прежним. На повторе строки СТРОГО новее watermark
 *         (last_change_date > wm) будут append-нуты СНОВА (в append-only RAW
 *         допустимы дубли — at-least-once); state-ключ отсекает ТОЛЬКО граничные
 *         строки (== watermark). Каноническая идемпотентность обеспечивается
 *         V_WB_SALES_RETURNS (last-wins по sale_id), успешный повтор двигает
 *         watermark. Range-wide дедуп RAW сознательно НЕ делаем (усложняет loader
 *         без бизнес-пользы: вью уже канонична).
 *   • весь цикл под одним ScriptLock (tryLock → finally releaseLock); параллельный
 *     запуск = SKIPPED_LOCKED (не ошибка данных, watermark не трогается).
 *
 * СТАТУСЫ: OK / OK_NO_CHANGES / SKIPPED_LOCKED / SKIPPED_RATE_LIMIT / ERROR.
 * (Отдельный PARTIAL не вводим: единственный доказуемо-неполный сигнал —
 *  упор в лимит строк ответа — трактуется как ERROR, ничего не пишем.
 *  SKIPPED_RATE_LIMIT — общий с ночной пересверкой 65-сек cooldown Sales API
 *  сработал до fetch; не ошибка данных, watermark не тронут, следующий час догонит.)
 *
 * НЕ входит в D2c: ночная пересверка последних 3–7 дней, Finance/Ads-триггеры,
 * единый health-монитор. Триггер ставит владелец после ручной приёмки.
 * ══════════════════════════════════════════════════════════════
 */

var WB_SALES_WATERMARK_PROP_ = 'WB_SALES_LAST_CHANGE_WATERMARK';
var WB_SALES_INC_TRIGGER_FN_ = 'runWbSalesIncremental';
var WB_SALES_INC_TZ_         = 'Europe/Moscow';

/**
 * Строгий формат watermark: YYYY-MM-DDThh:mm:ss[.frac] + календарная корректность.
 * Отсекает и «дата без времени», и мусор вида 2026-99-99T99:99:99 / 0000-...
 */
function salesValidWatermark_(s) {
  var m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(\.\d+)?$/.exec(String(s || ''));
  if (!m) return false;
  var Y = +m[1], Mo = +m[2], D = +m[3], H = +m[4], Mi = +m[5], S = +m[6];
  if (Y < 1) return false;
  if (Mo < 1 || Mo > 12) return false;
  if (H > 23 || Mi > 59 || S > 59) return false;
  var leap = (Y % 4 === 0 && Y % 100 !== 0) || (Y % 400 === 0);
  var daysInMonth = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][Mo - 1];
  if (D < 1 || D > daysInMonth) return false;
  return true;
}

// ───────────────────────────────────────────────────────────────
// BOOTSTRAP
// ───────────────────────────────────────────────────────────────

/**
 * Устанавливает watermark из MAX(last_change_date) текущего RAW. Под ScriptLock
 * (то же свойство меняет инкремент). НЕ перезаписывает существующее значение и
 * НЕ хардкодит якорь. Пустой RAW / нет MAX / битый формат → ERROR (без fallback).
 */
function wbSalesIncrementalBootstrap() {
  var lock = LockService.getScriptLock();
  if (!lock.tryLock(30000)) {
    var lm = 'Script Lock занят — bootstrap не выполнен.';
    console.error('❌ ' + lm);
    return { status: 'ERROR', error_message: lm };
  }
  try {
    if (!(typeof wbSalesBqSinkOn_ === 'function' && wbSalesBqSinkOn_())) {
      var sm = 'WB_SALES_BQ_SINK выключен — bootstrap только при включённом BigQuery-приёмнике.';
      console.error('❌ ' + sm);
      return { status: 'ERROR', error_message: sm };
    }
    var props = PropertiesService.getScriptProperties();
    var existing = props.getProperty(WB_SALES_WATERMARK_PROP_) || '';
    if (existing) {
      if (!salesValidWatermark_(existing)) {
        var bm = 'Существующий watermark НЕВЕРНОГО формата: ' + existing + ' — исправьте вручную, авто-перезапись запрещена.';
        console.error('❌ ' + bm);
        return { status: 'ERROR', error_message: bm };
      }
      console.log('ℹ️ watermark уже установлен: ' + existing + ' — bootstrap пропущен.');
      return { status: 'OK_EXISTS', watermark: existing };
    }
    var maxLcd = wbSalesBqMaxLastChange_();   // Sales хранит с 'T' → уже API-формат
    if (!maxLcd) {
      var em = 'В RAW_WB_SALES_RETURNS нет last_change_date (API-строк) — сначала backfill.';
      console.error('❌ ' + em);
      return { status: 'ERROR', error_message: em };
    }
    if (!salesValidWatermark_(maxLcd)) {
      var fm = 'MAX(last_change_date) в RAW невалиден как watermark: ' + maxLcd;
      console.error('❌ ' + fm);
      return { status: 'ERROR', error_message: fm };
    }
    props.setProperty(WB_SALES_WATERMARK_PROP_, maxLcd);
    console.log('✅ watermark установлен из RAW: ' + maxLcd);
    return { status: 'OK', watermark: maxLcd };
  } finally {
    lock.releaseLock();
  }
}

// ───────────────────────────────────────────────────────────────
// СТАТУС
// ───────────────────────────────────────────────────────────────

/** Диагностика: watermark, sink, RAW MAX(last_change_date), V_WB_SALES_RETURNS count. */
function wbSalesIncrementalStatus() {
  var wm = PropertiesService.getScriptProperties().getProperty(WB_SALES_WATERMARK_PROP_) || '(нет)';
  var sinkOn = (typeof wbSalesBqSinkOn_ === 'function') && wbSalesBqSinkOn_();
  var rawMax = '';
  try { rawMax = wbSalesBqMaxLastChange_(); } catch (e) { rawMax = '(ошибка: ' + ((e && e.message) || e) + ')'; }
  var viewCount = '';
  try { viewCount = wbSalesBqViewCount_(); } catch (e3) { viewCount = '(ошибка: ' + ((e3 && e3.message) || e3) + ')'; }
  console.log('SALES watermark: ' + wm + ' | sink: ' + (sinkOn ? 'ВКЛ' : 'ВЫКЛ') +
    ' | RAW max last_change_date: ' + rawMax + ' | V_WB_SALES_RETURNS: ' + viewCount);
  try {
    var ui = SpreadsheetApp.getUi();
    ui.alert('D2c статус (продажи)', 'watermark: ' + wm + '\nsink: ' + (sinkOn ? 'ВКЛ' : 'ВЫКЛ') +
      '\nRAW max last_change_date: ' + rawMax + '\nV_WB_SALES_RETURNS (уник. sale_id): ' + viewCount, ui.ButtonSet.OK);
  } catch (e2) {}
  return { watermark: wm, sink_on: sinkOn, raw_max_last_change_date: rawMax, view_count: viewCount };
}

// ───────────────────────────────────────────────────────────────
// ЗАПУСК ИНКРЕМЕНТА
// ───────────────────────────────────────────────────────────────

/** Базовая структура результата инкремента (контракт лога D2c). */
function salesIncBaseResult_() {
  return {
    status: 'ERROR', load_id: '',
    watermark_before: '', watermark_candidate: '', watermark_after: '',
    api_rows_received: 0, rows_after_boundary_dedup: 0, rows_written: 0,
    unique_saleID: 0,
    started_at: Utilities.formatDate(new Date(), WB_SALES_INC_TZ_, 'yyyy-MM-dd HH:mm:ss'),
    finished_at: '', duration_ms: 0, error_message: ''
  };
}

/**
 * Один запуск инкремента под ScriptLock. Весь цикл (watermark → API → валидация
 * → BQ append → обновление свойства) внутри try/finally. Параллельный запуск =
 * SKIPPED_LOCKED (не ошибка, watermark не трогается).
 */
function runWbSalesIncremental() {
  var t0 = Date.now();
  var r = salesIncBaseResult_();
  r.load_id = 'SALE_INC_' + Utilities.formatDate(new Date(), WB_SALES_INC_TZ_, 'yyyyMMdd_HHmmss');

  var lock = LockService.getScriptLock();
  if (!lock.tryLock(1000)) {
    r.status = 'SKIPPED_LOCKED';
    r.error_message = 'Активен другой запуск (ScriptLock) — пропущено, watermark не тронут.';
    r.finished_at = Utilities.formatDate(new Date(), WB_SALES_INC_TZ_, 'yyyy-MM-dd HH:mm:ss');
    r.duration_ms = Date.now() - t0;
    salesIncSafeLog_(r);
    return r;
  }
  try {
    wbSalesIncrementalCore_(r);
  } catch (e) {
    r.status = 'ERROR';
    r.error_message = 'Исключение: ' + ((e && e.message) || e);
    // watermark НЕ трогаем
  } finally {
    lock.releaseLock();
  }
  r.finished_at = Utilities.formatDate(new Date(), WB_SALES_INC_TZ_, 'yyyy-MM-dd HH:mm:ss');
  r.duration_ms = Date.now() - t0;
  salesIncSafeLog_(r);
  console.log('D2c инкремент: ' + r.status + ' | before=' + r.watermark_before +
    ' candidate=' + r.watermark_candidate + ' after=' + r.watermark_after +
    ' | api_rows=' + r.api_rows_received + ' boundary_dedup=' + r.rows_after_boundary_dedup +
    ' written=' + r.rows_written + (r.error_message ? ' | ' + r.error_message : ''));
  return r;
}

/**
 * Ядро инкремента (мутирует r). Ранний выход — установить r.status и return.
 * Валидация ВСЕГО пакета выполняется ДО первого append (иначе часть строк
 * записана, а watermark не сдвинут из-за ошибки в конце пакета).
 */
function wbSalesIncrementalCore_(r) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();

  if (!(typeof wbSalesBqSinkOn_ === 'function' && wbSalesBqSinkOn_())) {
    r.status = 'ERROR'; r.error_message = 'WB_SALES_BQ_SINK выключен — инкремент только в BigQuery.'; return;
  }
  var tk = getSalesToken_();
  if (!tk) { r.status = 'ERROR'; r.error_message = 'Нет токена WB (' + WB_SALES_TOKEN_KEYS_.join(' / ') + ').'; return; }

  var props = PropertiesService.getScriptProperties();
  var wm = props.getProperty(WB_SALES_WATERMARK_PROP_) || '';
  if (!wm) { r.status = 'ERROR'; r.error_message = 'watermark отсутствует — выполните wbSalesIncrementalBootstrap().'; return; }
  if (!salesValidWatermark_(wm)) { r.status = 'ERROR'; r.error_message = 'Неверный формат watermark: ' + wm; return; }
  r.watermark_before = wm;
  r.watermark_after = wm;

  // SHARED RATE-LIMIT GUARD (общий с ночной пересверкой). Под уже удерживаемым
  // ScriptLock, непосредственно перед fetch: один ScriptLock защищает от
  // одновременности, но НЕ от двух последовательных запросов Sales API в пределах
  // минуты (hourly и nightly могут стартовать с разницей в секунды → 429). Если
  // с прошлой ПОПЫТКИ < 65 сек — пропускаем без HTTP, watermark не трогаем
  // (следующий час догонит). См. wbSalesApiAcquireRequestSlot_ (WbSalesReconcile.gs).
  var slot = wbSalesApiAcquireRequestSlot_();
  if (!slot.ok) {
    r.status = 'SKIPPED_RATE_LIMIT';
    r.error_message = 'С прошлой попытки Sales API прошло ' + slot.since_ms + ' мс (< ' +
      WB_SALES_API_MIN_INTERVAL_MS_ + ') — HTTP не вызван, watermark не тронут.';
    r.watermark_after = wm;
    return;
  }

  // Один fail-closed запрос (rate limit 1 req/min). dateFrom = watermark.
  var fetched = fetchSalesApiData_(tk.token, wm);
  r.api_rows_received = (fetched.api_rows_received != null) ? fetched.api_rows_received : ((fetched.data || []).length);
  if (!fetched.ok) {
    // HTTP/JSON/не-массив/упор в лимит строк → ERROR (доказуемо-неполный или сбой);
    // ничего не пишем, watermark не двигаем.
    r.status = 'ERROR';
    r.error_message = fetched.error || 'Незавершённая выгрузка Sales API';
    return;
  }

  var data = fetched.data || [];
  if (data.length === 0 && r.api_rows_received === 0) {
    r.status = 'OK_NO_CHANGES'; r.watermark_after = wm; return;
  }

  // (1) Валидация ВСЕХ СЫРЫХ API-строк ДО нормализации. Нормализатор молча роняет
  // строки без даты (`if(!day)continue`) — если валидировать после него, битая
  // строка исчезнет незаметно, а watermark сдвинется. Хоть одна плохая строка →
  // пакет отклонён (с точным номером), RAW/watermark неизменны.
  var candidate = '';
  for (var i = 0; i < data.length; i++) {
    var o = data[i];
    var oSid = String((o && o.saleID) || '').trim();
    var oDay = (typeof normalizeDateKey_ === 'function') ? normalizeDateKey_(o && o.date) : String((o && o.date) || '').substring(0, 10);
    var oLcd = String((o && o.lastChangeDate) || '');
    if (oSid === '' || !oDay || !salesValidWatermark_(oLcd)) {
      r.status = 'ERROR';
      r.error_message = 'Невалидная сырая API-строка #' + (i + 1) + ' (saleID/date/lastChangeDate) — пакет отклонён до записи, watermark не сдвинут.';
      return;
    }
    if (!candidate || oLcd > candidate) candidate = oLcd;   // формат единый → строковый max = хронологический
  }
  r.watermark_candidate = candidate;
  if (!salesValidWatermark_(candidate)) {
    r.status = 'ERROR'; r.error_message = 'Не удалось вычислить валидный candidate из пакета — запись отменена.'; return;
  }
  if (candidate < wm) {
    r.status = 'ERROR'; r.error_message = 'candidate (' + candidate + ') < watermark_before (' + wm + ') — аномалия, запись отменена.'; return;
  }

  // (2) Нормализация (noWindow — без фильтра sale_dt). Все даты уже валидны →
  // нормализатор ничего не должен отбросить; строгая проверка длины как страховка.
  var rawSheet = getRawSalesSheet_(ss);
  var lastCol = rawSheet.getLastColumn();
  var hMap = buildSalesRawHeaderMap_(rawSheet, lastCol);
  var skuIndex = (typeof buildSkuIndex_ === 'function') ? buildSkuIndex_(ss) : { byNm: {}, byBarcode: {} };
  var rows = normalizeSalesApiRows_(data, hMap, lastCol, r.load_id, r.started_at, wm, '', skuIndex, { noWindow: true });

  if (rows.length !== data.length) {
    r.status = 'ERROR';
    r.error_message = 'Нормализатор отбросил строки (' + data.length + '→' + rows.length + ') — пакет отклонён, watermark не сдвинут.';
    return;
  }

  var iSaleId = hMap['sale_id'], iLcd = hMap['last_change_date'], iRj = hMap['raw_json'];
  if (iSaleId === undefined || iLcd === undefined || iRj === undefined) {
    r.status = 'ERROR'; r.error_message = 'В схеме RAW нет одной из колонок sale_id/last_change_date/raw_json.'; return;
  }

  // (3) STATE-hash по raw_json + внутрипакетный last-wins по sale_id.
  // Ключ границы/дедупа — sale_id|md5(raw_json) (полное состояние), НЕ row_hash.
  // При нескольких версиях одной продажи в пакете оставляем максимальную по
  // lastChangeDate; tie-break — по state-hash (детерминированно).
  var bySale = {};
  for (i = 0; i < rows.length; i++) {
    var row = rows[i];
    var sid = String(row[iSaleId] || '').trim();
    var lcd = String(row[iLcd] || '');
    var sh = salesMd5_(String(row[iRj] || ''));
    var cur = bySale[sid];
    if (!cur || lcd > cur.lcd || (lcd === cur.lcd && sh > cur.sh)) {
      bySale[sid] = { row: row, lcd: lcd, sh: sh, sid: sid };
    }
  }

  // (4) Граница секунды по STATE-ключу: sale_id|md5(raw_json) с last_change_date
  // == watermark, уже в RAW. Строки строго новее watermark — всегда; граничные —
  // только если такого состояния ещё нет.
  var boundaryKeys = wbSalesBqBoundaryStateKeys_(wm);
  var toAppend = [];
  var sidKeys = Object.keys(bySale);
  for (var k = 0; k < sidKeys.length; k++) {
    var w = bySale[sidKeys[k]];
    var include = false;
    if (w.lcd > wm) include = true;                                  // строго новее watermark
    else if (w.lcd === wm) include = !boundaryKeys[w.sid + '|' + w.sh]; // граница: только нового состояния
    // w.lcd < wm — уже обработано ранее
    if (include) toAppend.push(w.row);
  }
  r.rows_after_boundary_dedup = toAppend.length;

  if (toAppend.length === 0) {
    r.status = 'OK_NO_CHANGES'; r.watermark_after = wm; return;
  }

  // Append (исключение → пробрасывается в runWbSalesIncremental → ERROR, watermark не двигаем).
  appendSalesRows_(rawSheet, toAppend, lastCol);
  r.rows_written = toAppend.length;

  try {
    var sums = aggregateSalesRowArray_(toAppend, hMap, '0000-01-01', '9999-12-31', { noWindow: true });
    r.unique_saleID = sums.unique_saleID != null ? sums.unique_saleID : (sums.unique_saleid != null ? sums.unique_saleid : 0);
  } catch (eS) { /* контрольные суммы не критичны для статуса */ }

  // watermark двигаем ТОЛЬКО если есть строки строго новее (candidate > watermark_before).
  if (candidate > wm) {
    props.setProperty(WB_SALES_WATERMARK_PROP_, candidate);
    r.watermark_after = candidate;
  } else {
    r.watermark_after = wm;   // записаны только граничные строки — watermark не двигаем
  }
  r.status = 'OK';
}

/** Безопасная запись строки инкремента в IMPORT_LOG_SALES_RETURNS (исключение глушим). */
function salesIncSafeLog_(r) {
  try {
    var ss = SpreadsheetApp.getActiveSpreadsheet();
    writeSalesIncrementalLogEntry_(ensureImportLogSalesSheet_(ss), r);
  } catch (e) {}
}

/** Пишет строку инкремента в IMPORT_LOG_SALES_RETURNS по расширенному контракту. */
function writeSalesIncrementalLogEntry_(logSheet, r) {
  if (!logSheet) return;
  var rowObj = {
    load_id: r.load_id, loaded_at: r.started_at,
    rows_imported: r.rows_written, unique_saleID: r.unique_saleID,
    status: r.status, error_message: r.error_message,
    watermark_before: r.watermark_before, watermark_after: r.watermark_after,
    api_rows_received: r.api_rows_received,
    rows_after_boundary_dedup: r.rows_after_boundary_dedup,
    rows_written: r.rows_written, duration_ms: r.duration_ms
  };
  var rowArr = [];
  for (var i = 0; i < IMPORT_LOG_SALES_HEADERS_.length; i++) {
    var k = IMPORT_LOG_SALES_HEADERS_[i];
    rowArr.push(rowObj[k] !== undefined ? rowObj[k] : '');
  }
  logSheet.getRange(logSheet.getLastRow() + 1, 1, 1, rowArr.length).setValues([rowArr]);
}

// ───────────────────────────────────────────────────────────────
// ТРИГГЕР (идемпотентно; ставит владелец после ручной приёмки)
// ───────────────────────────────────────────────────────────────

/**
 * Идемпотентная установка hourly-триггера runWbSalesIncremental:
 *   0 → создать 1; 1 → ничего; 2+ → удалить дубли, оставить 1.
 * Затрагивает ТОЛЬКО обработчик runWbSalesIncremental (Orders/Finance/Ads не трогает).
 */
function wbSalesIncrementalInstallHourlyTrigger() {
  var trs = ScriptApp.getProjectTriggers();
  var mine = [];
  for (var i = 0; i < trs.length; i++) {
    if (trs[i].getHandlerFunction() === WB_SALES_INC_TRIGGER_FN_) mine.push(trs[i]);
  }
  if (mine.length === 0) {
    ScriptApp.newTrigger(WB_SALES_INC_TRIGGER_FN_).timeBased().everyHours(1).create();
    console.log('✅ Триггер создан (everyHours 1): ' + WB_SALES_INC_TRIGGER_FN_);
    return { created: 1, removed: 0, total: 1 };
  }
  var removed = 0;
  for (var j = 1; j < mine.length; j++) { ScriptApp.deleteTrigger(mine[j]); removed++; }
  console.log(mine.length === 1
    ? 'ℹ️ Триггер уже есть — ничего не создано.'
    : '⚠️ Удалены дубли: ' + removed + ', оставлен 1.');
  return { created: 0, removed: removed, total: 1 };
}

/** Удаляет ВСЕ триггеры обработчика runWbSalesIncremental (Orders/Finance/Ads не трогает). */
function wbSalesIncrementalRemoveTrigger() {
  var trs = ScriptApp.getProjectTriggers();
  var n = 0;
  for (var i = 0; i < trs.length; i++) {
    if (trs[i].getHandlerFunction() === WB_SALES_INC_TRIGGER_FN_) { ScriptApp.deleteTrigger(trs[i]); n++; }
  }
  console.log('🗑 Удалено триггеров ' + WB_SALES_INC_TRIGGER_FN_ + ': ' + n);
  return { removed: n };
}
