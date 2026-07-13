/**
 * ══════════════════════════════════════════════════════════════
 * EVETIS WB — WbSalesConsumerSource.gs   (Фаза D2b)
 *
 * ЕДИНЫЙ слой чтения продаж/возвратов для потребителей витрин.
 * Потребители (DashboardWb, Cleanwbdaily) больше НЕ открывают лист
 * RAW_WB_SALES_RETURNS напрямую и НЕ знают физического источника —
 * они получают строки через readCanonicalSalesRows_().
 *
 * Источник переключается feature-flag'ом (Script Property):
 *
 *     WB_SALES_CONSUMER_SOURCE = SHEET | BIGQUERY      (default SHEET)
 *
 *   • SHEET    — легаси-лист RAW_WB_SALES_RETURNS (аварийный/переходный режим).
 *   • BIGQUERY — дедуп-вью V_WB_SALES_RETURNS (целевой режим после приёмки).
 *
 * Оба источника нормализуются в ОДИН канонический 12-колоночный контракт
 * с одинаковыми логическими типами — потребители не зависят ни от
 * физического источника, ни от различий legacy-схемы:
 *
 *     sale_dt            string   (yyyy-MM-ddTHH:mm:ss)
 *     last_change_date   string   (yyyy-MM-ddTHH:mm:ss)
 *     internal_sku       string
 *     wb_nm_id           string   (INT64 может выходить за Number → строка)
 *     wb_vendor_code     string
 *     barcode            string
 *     finished_price     number
 *     operation_type     string
 *     is_return          boolean
 *     source_api         string
 *     quantity           number   (синтез = 1)
 *     is_duplicate       boolean  (синтез = false)
 *
 * ГРАНИЦЫ (Фаза D2b — НЕ трогаем):
 *   loader D2a, watermark, триггеры, RAW-схема, SQL вью, finance, ads, MART,
 *   PNL-формулы, общий bqQuery_ (не расширяем — дата идёт валидированным
 *   литералом DATE 'YYYY-MM-DD', не query-параметром).
 *
 * FAIL-CLOSED: в режиме BIGQUERY любая ошибка запроса ИЛИ пустой результат
 * при allowEmpty=false → исключение. Молчаливого отката на SHEET НЕТ.
 * Чтение у потребителей идёт ДО очистки витрин, поэтому исключение
 * прерывает сборку, не повредив данные.
 * ══════════════════════════════════════════════════════════════
 */

var WB_SALES_CONSUMER_PROP_       = 'WB_SALES_CONSUMER_SOURCE';
var WB_SALES_CONSUMER_FROM_PROP_  = 'WB_SALES_CONSUMER_FROM';
var WB_SALES_CONSUMER_FROM_DEF_   = '2024-09-01';   // постоянная стартовая дата проекта
var WB_SALES_CONSUMER_VIEW_       = 'V_WB_SALES_RETURNS';
var WB_SALES_CONSUMER_SHEET_      = 'RAW_WB_SALES_RETURNS';
var WB_SALES_CONSUMER_TZ_         = 'Europe/Moscow';

/** Канонический порядок колонок контракта (row[0] возвращаемого 2D-массива). */
var WB_SALES_CANON_HEADERS_ = [
  'sale_dt', 'last_change_date', 'internal_sku', 'wb_nm_id',
  'wb_vendor_code', 'barcode', 'finished_price', 'operation_type',
  'is_return', 'source_api', 'quantity', 'is_duplicate'
];

// ───────────────────────────────────────────────────────────────
// FEATURE FLAG
// ───────────────────────────────────────────────────────────────

/**
 * Текущий источник: 'BIGQUERY' | 'SHEET'.
 * SHEET по умолчанию ТОЛЬКО при отсутствии/пустом свойстве. Любое иное
 * (неизвестное/повреждённое) значение → исключение, а не молчаливый SHEET:
 * иначе опечатка после cutover дала бы внешне корректную устаревшую отчётность.
 */
function wbSalesConsumerSource_() {
  var raw = PropertiesService.getScriptProperties().getProperty(WB_SALES_CONSUMER_PROP_);
  if (raw === null || String(raw).trim() === '') return 'SHEET';
  var value = String(raw).trim().toUpperCase();
  if (value === 'SHEET' || value === 'BIGQUERY') return value;
  throw new Error('[SALES ADAPTER] Invalid ' + WB_SALES_CONSUMER_PROP_ + '="' + raw +
    '". Ожидалось SHEET или BIGQUERY.');
}

/** Переключить потребителей на BigQuery-вью. */
function wbSalesConsumerUseBigQuery() {
  PropertiesService.getScriptProperties().setProperty(WB_SALES_CONSUMER_PROP_, 'BIGQUERY');
  console.log('✅ WB_SALES_CONSUMER_SOURCE = BIGQUERY (потребители читают ' + WB_SALES_CONSUMER_VIEW_ + ')');
}

/** Откат: вернуть потребителей на легаси-лист (аварийный режим). */
function wbSalesConsumerUseSheet() {
  PropertiesService.getScriptProperties().setProperty(WB_SALES_CONSUMER_PROP_, 'SHEET');
  console.log('↩️ WB_SALES_CONSUMER_SOURCE = SHEET (потребители читают лист ' + WB_SALES_CONSUMER_SHEET_ + ')');
}

/** Показать текущий источник и нижнюю границу истории. */
function wbSalesConsumerSourceStatus() {
  var src = wbSalesConsumerSource_();
  var from = wbSalesConsumerFrom_();
  console.log('WB_SALES_CONSUMER_SOURCE = ' + src + ' | history from = ' + from);
  return { source: src, fromDate: from };
}

/**
 * Нижняя граница истории (обязательный partition filter в BQ).
 * Из Script Property WB_SALES_CONSUMER_FROM либо постоянный дефолт
 * WB_SALES_CONSUMER_FROM_DEF_ — НИКОГДА не скользящее окно.
 */
function wbSalesConsumerFrom_() {
  var v = String(PropertiesService.getScriptProperties().getProperty(WB_SALES_CONSUMER_FROM_PROP_) || '').trim();
  if (!v) v = WB_SALES_CONSUMER_FROM_DEF_;
  return wbSalesAssertDate_(v, 'WB_SALES_CONSUMER_FROM');
}

// ───────────────────────────────────────────────────────────────
// ГЛАВНЫЙ ЧИТАТЕЛЬ
// ───────────────────────────────────────────────────────────────

/**
 * Канонические строки продаж/возвратов в форме [header, ...rows]
 * (та же форма, что getRange().getValues() — потребители используют
 * именные пикеры и работают без изменений в обоих режимах).
 *
 * @param {{fromDate:(string|undefined), toDate:(string|undefined),
 *          allowEmpty:(boolean|undefined)}=} opts
 *   fromDate/toDate — 'YYYY-MM-DD' (по умолчанию from = wbSalesConsumerFrom_(),
 *     to не ограничен). allowEmpty (default false) — только для проверок и
 *     узких диапазонов; production-вызовы потребителей передают false.
 * @return {Array<Array>} [WB_SALES_CANON_HEADERS_, ...normalizedRows]
 */
function readCanonicalSalesRows_(opts) {
  opts = opts || {};
  var allowEmpty = (opts.allowEmpty === true);
  var src = wbSalesConsumerSource_();

  if (src === 'BIGQUERY') {
    return wbSalesReadBqCanonical_(opts.fromDate, opts.toDate, allowEmpty);
  }
  return wbSalesReadSheetCanonical_(opts.fromDate, opts.toDate, allowEmpty);
}

// ───────────────────────────────────────────────────────────────
// ЧИТАТЕЛЬ: BIGQUERY (V_WB_SALES_RETURNS)
// ───────────────────────────────────────────────────────────────

/**
 * Явный SELECT только нужных полей в каноническом порядке + синтез
 * quantity=1, is_duplicate=false. Обязательный partition-filter по
 * _sale_date. Детерминированный ORDER BY для воспроизводимости.
 * FAIL-CLOSED: ошибка запроса или (при !allowEmpty) 0 строк → throw.
 */
function wbSalesReadBqCanonical_(fromDate, toDate, allowEmpty) {
  var from = fromDate ? wbSalesAssertDate_(fromDate, 'fromDate') : wbSalesConsumerFrom_();
  var to = toDate ? wbSalesAssertDate_(toDate, 'toDate') : '';

  var c = getBqConfig_();
  var fqView = '`' + c.projectId + '.' + c.datasetId + '.' + WB_SALES_CONSUMER_VIEW_ + '`';

  var sql =
    'SELECT\n' +
    '  sale_dt,\n' +
    '  last_change_date,\n' +
    '  internal_sku,\n' +
    '  CAST(wb_nm_id AS STRING) AS wb_nm_id,\n' +
    '  wb_vendor_code,\n' +
    '  barcode,\n' +
    '  finished_price,\n' +
    '  operation_type,\n' +
    '  is_return,\n' +
    '  source_api,\n' +
    '  1 AS quantity,\n' +
    '  FALSE AS is_duplicate\n' +
    'FROM ' + fqView + '\n' +
    "WHERE _sale_date >= DATE '" + from + "'\n" +
    (to ? "  AND _sale_date <= DATE '" + to + "'\n" : '') +
    'ORDER BY sale_dt, sale_id';

  var res;
  try {
    res = bqQuery_(sql);
  } catch (e) {
    throw new Error('[SALES ADAPTER] BigQuery read failed (' + WB_SALES_CONSUMER_VIEW_ + '): ' + e);
  }

  var bqRows = (res && res.rows) ? res.rows : [];
  if (bqRows.length === 0 && !allowEmpty) {
    throw new Error('[SALES ADAPTER] empty BigQuery result from ' + WB_SALES_CONSUMER_VIEW_ +
      " (from=" + from + (to ? ', to=' + to : '') + '). ' +
      'Проверьте project/dataset/view/фильтр — production-чтение с allowEmpty=false не переключается на лист.');
  }

  var out = [WB_SALES_CANON_HEADERS_.slice()];
  for (var i = 0; i < bqRows.length; i++) {
    // Контролируемый fail-closed при неожиданной форме ответа (вместо TypeError).
    if (!bqRows[i] || !Array.isArray(bqRows[i].f) ||
        bqRows[i].f.length !== WB_SALES_CANON_HEADERS_.length) {
      throw new Error('[SALES ADAPTER] Invalid BigQuery row shape at index ' + i +
        ': ожидалось ' + WB_SALES_CANON_HEADERS_.length + ' ячеек.');
    }
    var f = bqRows[i].f;   // порядок ячеек = порядок SELECT = канонический
    out.push([
      wbSalesDateStr_(f[0].v),   // sale_dt
      wbSalesDateStr_(f[1].v),   // last_change_date
      wbSalesStr_(f[2].v),       // internal_sku
      wbSalesStr_(f[3].v),       // wb_nm_id (строка)
      wbSalesStr_(f[4].v),       // wb_vendor_code
      wbSalesStr_(f[5].v),       // barcode
      wbSalesNumber_(f[6].v),    // finished_price
      wbSalesStr_(f[7].v),       // operation_type
      wbSalesBool_(f[8].v),      // is_return
      wbSalesStr_(f[9].v),       // source_api
      wbSalesNumber_(f[10].v),   // quantity (=1)
      wbSalesBool_(f[11].v)      // is_duplicate (=false)
    ]);
  }
  return out;
}

// ───────────────────────────────────────────────────────────────
// ЧИТАТЕЛЬ: SHEET (легаси RAW_WB_SALES_RETURNS)
// ───────────────────────────────────────────────────────────────

/**
 * Легаси-лист → тот же канонический контракт (одинаковые типы).
 * quantity/is_duplicate синтезируются (в листе их нет), как и в BQ.
 * Границы fromDate/toDate применяются симметрично BQ-режиму — по первым
 * 10 символам sale_dt (единый публичный контракт адаптера, не зависящий
 * от источника). from по умолчанию = wbSalesConsumerFrom_() (2024-09-01),
 * а не «вся история».
 * При allowEmpty=false пустой лист/отсутствие листа ИЛИ пустой результат
 * после фильтрации fromDate/toDate → исключение (единый контракт с BQ,
 * защита от rollback с пустыми продажами). [header] возвращается только
 * при allowEmpty=true (parity/узкие проверки).
 */
function wbSalesReadSheetCanonical_(fromDate, toDate, allowEmpty) {
  var from = fromDate ? wbSalesAssertDate_(fromDate, 'fromDate') : wbSalesConsumerFrom_();
  var to = toDate ? wbSalesAssertDate_(toDate, 'toDate') : '';

  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sh = ss.getSheetByName(WB_SALES_CONSUMER_SHEET_);
  var header = [WB_SALES_CANON_HEADERS_.slice()];
  if (!sh || sh.getLastRow() < 2 || sh.getLastColumn() < 1) {
    if (!allowEmpty) {
      throw new Error('[SALES ADAPTER] empty Sheet source ' + WB_SALES_CONSUMER_SHEET_ +
        ' (from=' + from + (to ? ', to=' + to : '') + '). ' +
        'Единый контракт: production-чтение с allowEmpty=false не отдаёт пустые продажи.');
    }
    return header;
  }

  var values = sh.getRange(1, 1, sh.getLastRow(), sh.getLastColumn()).getValues();
  var hmap = wbSalesLowerHeaderMap_(values[0]);

  var iDate   = wbSalesFindCol_(hmap, ['sale_dt', 'sale_date', 'date']);
  var iLcd    = wbSalesFindCol_(hmap, ['last_change_date', 'lastchangedate']);
  var iSku    = wbSalesFindCol_(hmap, ['internal_sku']);
  var iNm     = wbSalesFindCol_(hmap, ['wb_nm_id', 'nmid', 'nm_id']);
  var iVendor = wbSalesFindCol_(hmap, ['wb_vendor_code', 'vendor_code', 'vendorcode', 'supplierarticle']);
  var iBar    = wbSalesFindCol_(hmap, ['barcode', 'barcodes']);
  var iFin    = wbSalesFindCol_(hmap, ['finished_price', 'finishedprice']);
  var iOper   = wbSalesFindCol_(hmap, ['operation_type', 'supplier_oper_name', 'type']);
  var iRet    = wbSalesFindCol_(hmap, ['is_return']);
  var iSrc    = wbSalesFindCol_(hmap, ['source_api']);
  // Реальная legacy-семантика: колонки quantity/is_duplicate есть в листе
  // (loader D2a их сохранял), в дедуп-вью — нет. Синтез только при отсутствии.
  var iQty    = wbSalesFindCol_(hmap, ['quantity', 'qty']);
  var iDup    = wbSalesFindCol_(hmap, ['is_duplicate', 'isduplicate']);

  var out = header;
  for (var r = 1; r < values.length; r++) {
    var row = values[r];
    var saleDt = iDate >= 0 ? wbSalesDateStr_(row[iDate]) : '';
    // Симметричный BQ фильтр по диапазону (первые 10 символов sale_dt).
    var day = String(saleDt).substring(0, 10);
    if (/^\d{4}-\d{2}-\d{2}$/.test(day)) {
      if (day < from) continue;
      if (to && day > to) continue;
    }
    out.push([
      saleDt,
      iLcd    >= 0 ? wbSalesDateStr_(row[iLcd])  : '',
      iSku    >= 0 ? wbSalesStr_(row[iSku])      : '',
      iNm     >= 0 ? wbSalesStr_(row[iNm])       : '',
      iVendor >= 0 ? wbSalesStr_(row[iVendor])   : '',
      iBar    >= 0 ? wbSalesStr_(row[iBar])      : '',
      iFin    >= 0 ? wbSalesNumber_(row[iFin])   : 0,
      iOper   >= 0 ? wbSalesStr_(row[iOper])     : '',
      iRet    >= 0 ? wbSalesBool_(row[iRet])     : false,
      iSrc    >= 0 ? wbSalesStr_(row[iSrc])      : 'WB_API_SALES',
      iQty    >= 0 ? wbSalesNumber_(row[iQty])   : 1,      // реальный quantity, иначе синтез 1
      iDup    >= 0 ? wbSalesBool_(row[iDup])     : false   // реальный is_duplicate, иначе синтез false
    ]);
  }

  // Симметрично BQ: пустой результат после фильтрации при allowEmpty=false → ошибка.
  if (out.length === 1 && !allowEmpty) {
    throw new Error('[SALES ADAPTER] empty Sheet result after filtering ' + WB_SALES_CONSUMER_SHEET_ +
      ' (from=' + from + (to ? ', to=' + to : '') + '). ' +
      'Единый контракт: production-чтение с allowEmpty=false не отдаёт пустые продажи.');
  }
  return out;
}

// ───────────────────────────────────────────────────────────────
// КОНТРАКТ ДЛЯ ПОТРЕБИТЕЛЕЙ, ИСПОЛЬЗУЮЩИХ findCol_(headerMap,…)
// ───────────────────────────────────────────────────────────────

/**
 * Карта {имя(lower)->индекс} из канонической строки-заголовка.
 * Совместима с общим findCol_ (он приводит варианты к lower).
 * Используется Cleanwbdaily вместо getHeaderMap_(sheet).
 */
function salesHeaderMapFromRow_(headerRow) {
  return wbSalesLowerHeaderMap_(headerRow);
}

// ───────────────────────────────────────────────────────────────
// НОРМАЛИЗАТОРЫ (единые типы для обоих источников)
// ───────────────────────────────────────────────────────────────

/** Строка (trim); null/undefined → ''. */
function wbSalesStr_(v) {
  if (v === null || v === undefined) return '';
  return String(v).trim();
}

/** Число; поддержка запятой-десятичного и пробелов-разрядов; иначе 0. */
function wbSalesNumber_(v) {
  if (v === '' || v === null || v === undefined) return 0;
  if (typeof v === 'number') return v;
  var s = String(v).replace(/\s/g, '').replace(',', '.');
  var n = parseFloat(s);
  return isNaN(n) ? 0 : n;
}

/** Boolean; принимает true/'true'/'1'/'да'. */
function wbSalesBool_(v) {
  if (v === true) return true;
  if (v === false || v === null || v === undefined) return false;
  var s = String(v).trim().toLowerCase();
  return s === 'true' || s === '1' || s === 'да';
}

/** Дата → единый 'yyyy-MM-ddTHH:mm:ss' (Date или строка). */
function wbSalesDateStr_(v) {
  if (v === null || v === undefined || v === '') return '';
  if (v instanceof Date) {
    if (isNaN(v.getTime())) return '';
    return Utilities.formatDate(v, WB_SALES_CONSUMER_TZ_, "yyyy-MM-dd'T'HH:mm:ss");
  }
  return String(v).trim().replace(' ', 'T');
}

/** Строгая проверка 'YYYY-MM-DD'; иначе исключение. Возвращает ту же строку. */
function wbSalesAssertDate_(s, label) {
  var v = String(s || '').trim();
  if (!/^\d{4}-\d{2}-\d{2}$/.test(v)) {
    throw new Error('[SALES ADAPTER] ' + (label || 'date') + ': ожидался формат YYYY-MM-DD, получено "' + s + '".');
  }
  return v;
}

/** {имя(lower)->индекс} из массива-заголовка. */
function wbSalesLowerHeaderMap_(headerRow) {
  var map = {};
  for (var i = 0; i < headerRow.length; i++) {
    var h = String(headerRow[i] || '').trim().toLowerCase();
    if (h && map[h] === undefined) map[h] = i;
  }
  return map;
}

/** Первый индекс среди вариантов (lower) или -1. */
function wbSalesFindCol_(hmap, variants) {
  for (var i = 0; i < variants.length; i++) {
    var v = variants[i].toLowerCase();
    if (hmap[v] !== undefined) return hmap[v];
  }
  return -1;
}

// ───────────────────────────────────────────────────────────────
// PARITY SELF-TEST (read-only, до переключения флага)
// ───────────────────────────────────────────────────────────────

/**
 * Сверяет SHEET vs BIGQUERY по date×SKU на пересечении окон.
 * Допуск: quantity mismatch = 0; money mismatch (копейки) = 0; missing keys = 0.
 * Ничего не переключает и не пишет. Печатает границы и результат.
 */
function wbSalesConsumerParityTest_() {
  var sheetRows = wbSalesReadSheetCanonical_(undefined, undefined, true);
  var bqRows = wbSalesReadBqCanonical_(undefined, undefined, true);

  var sAgg = wbSalesParityAggregate_(sheetRows);
  var bAgg = wbSalesParityAggregate_(bqRows);

  console.log('SHEET rows=' + (sheetRows.length - 1) + ' | min=' + sAgg.minDate + ' | max=' + sAgg.maxDate);
  console.log('BQ    rows=' + (bqRows.length - 1) + ' | min=' + bAgg.minDate + ' | max=' + bAgg.maxDate);

  // Точная граница пересечения: max(min), min(max).
  var from = (sAgg.minDate > bAgg.minDate) ? sAgg.minDate : bAgg.minDate;
  var to   = (sAgg.maxDate < bAgg.maxDate) ? sAgg.maxDate : bAgg.maxDate;
  console.log('comparison from=' + from + ' | to=' + to);

  if (!from || !to || from > to) {
    console.log('❌ PARITY: пустое или некорректное пересечение окон — сверять нечего.');
    return { ok: false, reason: 'empty_overlap' };
  }

  // Ключи в границах пересечения.
  var keys = {};
  Object.keys(sAgg.map).forEach(function (k) { if (wbSalesKeyInRange_(k, from, to)) keys[k] = true; });
  Object.keys(bAgg.map).forEach(function (k) { if (wbSalesKeyInRange_(k, from, to)) keys[k] = true; });

  var qtyMismatch = 0, moneyMismatch = 0, missing = 0;
  var samples = [];
  Object.keys(keys).forEach(function (k) {
    var s = sAgg.map[k] || null;
    var b = bAgg.map[k] || null;
    if (!s || !b) {
      missing++;
      if (samples.length < 10) samples.push('MISSING ' + k + ' sheet=' + (!!s) + ' bq=' + (!!b));
      return;
    }
    if (s.salesQty !== b.salesQty || s.returnsQty !== b.returnsQty) {
      qtyMismatch++;
      if (samples.length < 10) samples.push('QTY ' + k + ' sheet(s=' + s.salesQty + ',r=' + s.returnsQty +
        ') bq(s=' + b.salesQty + ',r=' + b.returnsQty + ')');
    }
    if (s.grossKop !== b.grossKop || s.retKop !== b.retKop) {
      moneyMismatch++;
      if (samples.length < 10) samples.push('MONEY ' + k + ' sheet(g=' + s.grossKop + ',r=' + s.retKop +
        ') bq(g=' + b.grossKop + ',r=' + b.retKop + ')');
    }
  });

  var ok = (qtyMismatch === 0 && moneyMismatch === 0 && missing === 0);
  console.log((ok ? '✅' : '❌') + ' PARITY: keys=' + Object.keys(keys).length +
    ' | quantity mismatch=' + qtyMismatch +
    ' | money mismatch (копейки)=' + moneyMismatch +
    ' | missing keys=' + missing);
  samples.forEach(function (s) { console.log('   • ' + s); });
  return { ok: ok, quantityMismatch: qtyMismatch, moneyMismatch: moneyMismatch, missingKeys: missing, from: from, to: to };
}

/**
 * Агрегат канонических строк по date×SKU (для parity). Деньги — в копейках.
 * Обе стороны уже нормализованы в единый контракт, поэтому:
 *   • строки is_duplicate === true ПРОПУСКАЮТСЯ (legacy-дубли; во вью их нет,
 *     на BQ-стороне фильтр безвреден) — сравниваем «лист без дублей» vs BQ;
 *   • qty = Math.abs(quantity) || 1 (legacy-продажи поштучные);
 *   • возврат — по авторитетному каноническому is_return, без повторной
 *     деривации из operation_type.
 */
function wbSalesParityAggregate_(values) {
  var map = {};
  var minDate = '', maxDate = '';
  for (var r = 1; r < values.length; r++) {
    var row = values[r];
    if (row[11] === true) continue;   // is_duplicate → пропуск
    var day = String(row[0] || '').substring(0, 10);   // sale_dt → yyyy-MM-dd
    if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) continue;
    var sku = String(row[2] || '').trim() || ('nm:' + String(row[3] || '').trim());
    var isRet = (row[8] === true);   // каноническое is_return, авторитетно
    var qty = Math.abs(Number(row[10])) || 1;
    var kop = Math.round((Number(row[6]) || 0) * 100);

    var k = day + '|' + sku;
    if (!map[k]) map[k] = { salesQty: 0, returnsQty: 0, grossKop: 0, retKop: 0 };
    if (isRet) { map[k].returnsQty += qty; map[k].retKop += kop; }
    else { map[k].salesQty += qty; map[k].grossKop += kop; }

    if (!minDate || day < minDate) minDate = day;
    if (!maxDate || day > maxDate) maxDate = day;
  }
  return { map: map, minDate: minDate, maxDate: maxDate };
}

/** Ключ 'yyyy-MM-dd|sku' попадает в [from,to] по дате. */
function wbSalesKeyInRange_(k, from, to) {
  var day = k.substring(0, 10);
  return day >= from && day <= to;
}

// ───────────────────────────────────────────────────────────────
// ПУБЛИЧНЫЕ ОБЁРТКИ ЗАПУСКА (репозиторий = Apps Script)
// ───────────────────────────────────────────────────────────────

/** Публичный запуск parity-сверки (SHEET vs BIGQUERY). Ничего не переключает. */
function wbSalesConsumerParityTest() {
  return wbSalesConsumerParityTest_();
}

// ───────────────────────────────────────────────────────────────
// READ-ONLY ДИАГНОСТИКА (не меняет flag, ничего не пишет)
// ───────────────────────────────────────────────────────────────

/**
 * Диагностика расхождений parity: доказывает, что дельта SHEET vs BQ —
 * это legacy-дубли (is_duplicate=true), а не потеря валидных продаж в BQ.
 * Только console.log. Флаг и данные не трогает.
 */
function wbSalesConsumerParityDiagnostics() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();

  // --- Сырой лист: наличие колонок и счётчики дублей ---
  var sh = ss.getSheetByName(WB_SALES_CONSUMER_SHEET_);
  var rawRows = 0, dupCol = false, qtyCol = false, dupTotal = 0;
  if (sh && sh.getLastRow() > 1 && sh.getLastColumn() > 0) {
    var vals = sh.getRange(1, 1, sh.getLastRow(), sh.getLastColumn()).getValues();
    var hmap = wbSalesLowerHeaderMap_(vals[0]);
    var iDup = wbSalesFindCol_(hmap, ['is_duplicate', 'isduplicate']);
    var iQty = wbSalesFindCol_(hmap, ['quantity', 'qty']);
    dupCol = (iDup >= 0);
    qtyCol = (iQty >= 0);
    rawRows = vals.length - 1;
    if (iDup >= 0) {
      for (var i = 1; i < vals.length; i++) {
        if (wbSalesBool_(vals[i][iDup])) dupTotal++;
      }
    }
  }

  // --- Канонические строки (SHEET carries real is_duplicate/quantity) ---
  var sheetCanon = wbSalesReadSheetCanonical_(undefined, undefined, true);
  var bqCanon = wbSalesReadBqCanonical_(undefined, undefined, true);

  var sMinMax = wbSalesDiagMinMax_(sheetCanon);
  var bMinMax = wbSalesDiagMinMax_(bqCanon);
  var from = (sMinMax.min > bMinMax.min) ? sMinMax.min : bMinMax.min;
  var to   = (sMinMax.max < bMinMax.max) ? sMinMax.max : bMinMax.max;

  console.log('WB_SALES_CONSUMER_SOURCE = ' + wbSalesConsumerSource_() + ' (диагностика read-only, не переключает)');
  console.log('SHEET raw rows=' + rawRows +
    ' | is_duplicate column found=' + dupCol +
    ' | quantity column found=' + qtyCol +
    ' | duplicate rows total=' + dupTotal);
  console.log('SHEET canonical rows=' + (sheetCanon.length - 1) + ' | min=' + sMinMax.min + ' | max=' + sMinMax.max);
  console.log('BQ    canonical rows=' + (bqCanon.length - 1) + ' | min=' + bMinMax.min + ' | max=' + bMinMax.max);
  console.log('overlap from=' + from + ' | to=' + to);

  if (!from || !to || from > to) {
    console.log('❌ пустое пересечение — сравнивать нечего.');
    return;
  }

  // Счётчики строк листа в overlap: raw / duplicate / non-duplicate.
  var sIn = 0, sDupIn = 0, sNonDupIn = 0;
  for (var r = 1; r < sheetCanon.length; r++) {
    var day = String(sheetCanon[r][0] || '').substring(0, 10);
    if (day < from || day > to) continue;
    sIn++;
    if (sheetCanon[r][11] === true) sDupIn++; else sNonDupIn++;
  }
  var bIn = 0;
  for (var rb = 1; rb < bqCanon.length; rb++) {
    var db = String(bqCanon[rb][0] || '').substring(0, 10);
    if (db >= from && db <= to) bIn++;
  }
  console.log('overlap: sheet rows=' + sIn + ' (dup=' + sDupIn + ', non-dup=' + sNonDupIn + ') | bq rows=' + bIn);

  // Агрегаты BEFORE (все строки) и AFTER (без дублей) исключения.
  var sBefore = wbSalesDiagAgg_(sheetCanon, from, to, false);
  var sAfter  = wbSalesDiagAgg_(sheetCanon, from, to, true);
  var bAgg    = wbSalesDiagAgg_(bqCanon, from, to, true);   // в BQ дублей нет, exclude безвреден

  var cmpBefore = wbSalesDiagCompare_(sBefore, bAgg);
  var cmpAfter  = wbSalesDiagCompare_(sAfter, bAgg);
  console.log('BEFORE dup-exclusion: missing keys=' + cmpBefore.missing +
    ' | qty mismatch=' + cmpBefore.qty + ' | money mismatch=' + cmpBefore.money);
  console.log('AFTER  dup-exclusion: missing keys=' + cmpAfter.missing +
    ' | qty mismatch=' + cmpAfter.qty + ' | money mismatch=' + cmpAfter.money);

  // Первые 20 спорных ключей ПОСЛЕ исключения дублей.
  console.log('--- спорные ключи (после исключения дублей, до 20) ---');
  var shown = 0;
  var allKeys = {};
  Object.keys(sAfter).forEach(function (k) { allKeys[k] = true; });
  Object.keys(bAgg).forEach(function (k) { allKeys[k] = true; });
  Object.keys(allKeys).sort().forEach(function (k) {
    if (shown >= 20) return;
    var sa = sAfter[k], ba = bAgg[k];
    var diff = (!sa || !ba) || sa.salesQty !== ba.salesQty || sa.returnsQty !== ba.returnsQty ||
               sa.grossKop !== ba.grossKop || sa.retKop !== ba.retKop;
    if (!diff) return;
    var rk = wbSalesDiagRawByKey_(sheetCanon, k, from, to);
    console.log('• ' + k +
      ' | sheet raw=' + rk.raw + ' dup=' + rk.dup + ' non-dup=' + rk.nonDup +
      ' qty=' + (sa ? sa.salesQty : 0) + ' amountKop=' + (sa ? sa.grossKop : 0) +
      ' || bq rows=' + (ba ? (ba.salesQty + ba.returnsQty) : 0) +
      ' qty=' + (ba ? ba.salesQty : 0) + ' amountKop=' + (ba ? ba.grossKop : 0));
    shown++;
  });

  // BODY-300: явный дамп исходных канонических строк листа в overlap.
  console.log('--- EVT-HC-BODY-300: строки листа в overlap ---');
  var bodyShown = 0;
  for (var rr = 1; rr < sheetCanon.length; rr++) {
    var row = sheetCanon[rr];
    if (String(row[2] || '').trim() !== 'EVT-HC-BODY-300') continue;
    var d = String(row[0] || '').substring(0, 10);
    if (d < from || d > to) continue;
    if (bodyShown >= 50) { console.log('   … (обрезано)'); break; }
    console.log('   sale_dt=' + row[0] + ' nm=' + row[3] + ' fin=' + row[6] +
      ' qty=' + row[10] + ' is_dup=' + row[11] + ' oper=' + row[7] + ' is_return=' + row[8]);
    bodyShown++;
  }
  if (bodyShown === 0) console.log('   (нет строк BODY-300 в overlap)');
}

/** min/max дня (yyyy-MM-dd) по канонической строке sale_dt. */
function wbSalesDiagMinMax_(values) {
  var min = '', max = '';
  for (var r = 1; r < values.length; r++) {
    var day = String(values[r][0] || '').substring(0, 10);
    if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) continue;
    if (!min || day < min) min = day;
    if (!max || day > max) max = day;
  }
  return { min: min, max: max };
}

/** Агрегат day×SKU в [from,to]; excludeDup=true пропускает is_duplicate. */
function wbSalesDiagAgg_(values, from, to, excludeDup) {
  var map = {};
  for (var r = 1; r < values.length; r++) {
    var row = values[r];
    if (excludeDup && row[11] === true) continue;
    var day = String(row[0] || '').substring(0, 10);
    if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) continue;
    if (day < from || day > to) continue;
    var sku = String(row[2] || '').trim() || ('nm:' + String(row[3] || '').trim());
    var isRet = (row[8] === true);
    var qty = Math.abs(Number(row[10])) || 1;
    var kop = Math.round((Number(row[6]) || 0) * 100);
    var k = day + '|' + sku;
    if (!map[k]) map[k] = { salesQty: 0, returnsQty: 0, grossKop: 0, retKop: 0 };
    if (isRet) { map[k].returnsQty += qty; map[k].retKop += kop; }
    else { map[k].salesQty += qty; map[k].grossKop += kop; }
  }
  return map;
}

/** Сравнение двух агрегатов: {missing, qty, money}. */
function wbSalesDiagCompare_(a, b) {
  var keys = {};
  Object.keys(a).forEach(function (k) { keys[k] = true; });
  Object.keys(b).forEach(function (k) { keys[k] = true; });
  var missing = 0, qty = 0, money = 0;
  Object.keys(keys).forEach(function (k) {
    var x = a[k], y = b[k];
    if (!x || !y) { missing++; return; }
    if (x.salesQty !== y.salesQty || x.returnsQty !== y.returnsQty) qty++;
    if (x.grossKop !== y.grossKop || x.retKop !== y.retKop) money++;
  });
  return { missing: missing, qty: qty, money: money };
}

/** Разбивка сырых канонических строк листа по ключу: raw/dup/non-dup. */
function wbSalesDiagRawByKey_(values, key, from, to) {
  var raw = 0, dup = 0, nonDup = 0;
  for (var r = 1; r < values.length; r++) {
    var row = values[r];
    var day = String(row[0] || '').substring(0, 10);
    if (day < from || day > to) continue;
    var sku = String(row[2] || '').trim() || ('nm:' + String(row[3] || '').trim());
    if ((day + '|' + sku) !== key) continue;
    raw++;
    if (row[11] === true) dup++; else nonDup++;
  }
  return { raw: raw, dup: dup, nonDup: nonDup };
}

// ───────────────────────────────────────────────────────────────
// READ-ONLY КЛАССИФИКАЦИЯ ИСТОЧНИКОВ (Период × source_api)
// ───────────────────────────────────────────────────────────────

var WB_SALES_BQ_BOUNDARY_ = '2026-04-13';   // нижний край сплошного покрытия BQ (доказано)
var WB_SALES_DIAG_ROW_CAP_ = 300;           // предел построчного дампа missing

/**
 * Классифицирует строки ЛИСТА по осям Период(до/с boundary) × source_api
 * (WB_API_SALES / пусто / иной) и разбирает 141 missing-ключ относительно BQ.
 * Читает СЫРОЙ лист напрямую (нужен sale_id/event_key, которого нет в
 * каноническом контракте). Только console.log — flag/данные не трогает.
 */
function wbSalesConsumerSourceClassification() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sh = ss.getSheetByName(WB_SALES_CONSUMER_SHEET_);
  if (!sh || sh.getLastRow() < 2 || sh.getLastColumn() < 1) {
    console.log('❌ Лист ' + WB_SALES_CONSUMER_SHEET_ + ' пуст/не найден.');
    return;
  }
  var vals = sh.getRange(1, 1, sh.getLastRow(), sh.getLastColumn()).getValues();
  var h = wbSalesLowerHeaderMap_(vals[0]);
  var c = {
    saleDt: wbSalesFindCol_(h, ['sale_dt']),
    lcd:    wbSalesFindCol_(h, ['last_change_date', 'lastchangedate']),
    sku:    wbSalesFindCol_(h, ['internal_sku']),
    nm:     wbSalesFindCol_(h, ['wb_nm_id', 'nmid', 'nm_id']),
    vendor: wbSalesFindCol_(h, ['wb_vendor_code', 'vendor_code', 'vendorcode', 'supplierarticle']),
    bar:    wbSalesFindCol_(h, ['barcode', 'barcodes']),
    saleId: wbSalesFindCol_(h, ['sale_id', 'saleid', 'event_key']),
    src:    wbSalesFindCol_(h, ['source_api']),
    oper:   wbSalesFindCol_(h, ['operation_type', 'supplier_oper_name', 'type']),
    qty:    wbSalesFindCol_(h, ['quantity', 'qty']),
    fin:    wbSalesFindCol_(h, ['finished_price', 'finishedprice']),
    ret:    wbSalesFindCol_(h, ['is_return']),
    dup:    wbSalesFindCol_(h, ['is_duplicate', 'isduplicate'])
  };
  console.log('boundary=' + WB_SALES_BQ_BOUNDARY_ + ' | колонки: ' +
    'sale_id=' + (c.saleId >= 0) + ', source_api=' + (c.src >= 0) + ', quantity=' + (c.qty >= 0));

  // Множество ключей BQ (day|sku) — для missing-детекции.
  var bq = wbSalesReadBqCanonical_(undefined, undefined, true);
  var bqKeys = {};
  for (var b = 1; b < bq.length; b++) {
    var bd = String(bq[b][0] || '').substring(0, 10);
    var bsku = String(bq[b][2] || '').trim() || ('nm:' + String(bq[b][3] || '').trim());
    if (/^\d{4}-\d{2}-\d{2}$/.test(bd)) bqKeys[bd + '|' + bsku] = true;
  }

  // Бакеты Период × Источник + сбор строк по missing-ключам.
  var buckets = {};
  var keyRows = {};   // key -> [rowIdx...]
  function bkt(period, srcLabel) {
    var id = period + ' | ' + srcLabel;
    if (!buckets[id]) buckets[id] = { rows: 0, keys: {}, qty: 0, kop: 0,
      minDt: '', maxDt: '', minLcd: '', maxLcd: '' };
    return buckets[id];
  }
  function srcLabelOf(v) {
    var s = String(v == null ? '' : v).trim();
    if (s === 'WB_API_SALES') return '1_API';
    if (s === '') return '2_EMPTY';
    return '3_OTHER(' + s + ')';
  }

  for (var r = 1; r < vals.length; r++) {
    var row = vals[r];
    var day = c.saleDt >= 0 ? String(row[c.saleDt] || '').substring(0, 10) : '';
    if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) continue;
    var sku = c.sku >= 0 ? (String(row[c.sku] || '').trim() || ('nm:' + (c.nm >= 0 ? String(row[c.nm] || '').trim() : ''))) : '';
    var key = day + '|' + sku;
    var period = (day < WB_SALES_BQ_BOUNDARY_) ? 'A(<' + WB_SALES_BQ_BOUNDARY_ + ')' : 'B(>=' + WB_SALES_BQ_BOUNDARY_ + ')';
    var srcLabel = c.src >= 0 ? srcLabelOf(row[c.src]) : '2_EMPTY';
    var qv = c.qty >= 0 ? (Math.abs(Number(row[c.qty])) || 1) : 1;
    var kop = c.fin >= 0 ? Math.round((Number(row[c.fin]) || 0) * 100) : 0;
    var lcd = c.lcd >= 0 ? String(row[c.lcd] || '') : '';

    var bu = bkt(period, srcLabel);
    bu.rows++; bu.keys[key] = true; bu.qty += qv; bu.kop += kop;
    if (!bu.minDt || day < bu.minDt) bu.minDt = day;
    if (!bu.maxDt || day > bu.maxDt) bu.maxDt = day;
    if (lcd) { if (!bu.minLcd || lcd < bu.minLcd) bu.minLcd = lcd; if (!bu.maxLcd || lcd > bu.maxLcd) bu.maxLcd = lcd; }

    if (!bqKeys[key]) { if (!keyRows[key]) keyRows[key] = []; keyRows[key].push(r); }
  }

  console.log('=== БАКЕТЫ Период × source_api ===');
  Object.keys(buckets).sort().forEach(function (id) {
    var x = buckets[id];
    console.log('[' + id + '] rows=' + x.rows + ' keys=' + Object.keys(x.keys).length +
      ' qty=' + x.qty + ' amountKop=' + x.kop +
      ' sale_dt=' + x.minDt + '…' + x.maxDt + ' lcd=' + x.minLcd + '…' + x.maxLcd);
  });

  // Агрегаты по missing-ключам.
  var missKeys = Object.keys(keyRows);
  var mBefore = 0, mAfter = 0, mApi = 0, mEmpty = 0, mOther = 0;
  missKeys.forEach(function (key) {
    var day = key.substring(0, 10);
    if (day < WB_SALES_BQ_BOUNDARY_) mBefore++; else mAfter++;
    var hasApi = false, hasEmpty = false, hasOther = false;
    keyRows[key].forEach(function (ri) {
      var lbl = c.src >= 0 ? srcLabelOf(vals[ri][c.src]) : '2_EMPTY';
      if (lbl === '1_API') hasApi = true; else if (lbl === '2_EMPTY') hasEmpty = true; else hasOther = true;
    });
    if (hasApi) mApi++; if (hasEmpty) mEmpty++; if (hasOther) mOther++;
  });
  console.log('=== MISSING-КЛЮЧИ (нет в BQ), всего уник=' + missKeys.length + ' ===');
  console.log('missing before ' + WB_SALES_BQ_BOUNDARY_ + '=' + mBefore +
    ' | from ' + WB_SALES_BQ_BOUNDARY_ + '=' + mAfter);
  console.log('missing keys с API-строкой=' + mApi +
    ' | с empty-source=' + mEmpty + ' | с other-source=' + mOther);

  // Построчный дамп missing (с предохранителем).
  console.log('=== MISSING строки (до ' + WB_SALES_DIAG_ROW_CAP_ + ') ===');
  var dumped = 0;
  missKeys.sort().forEach(function (key) {
    keyRows[key].forEach(function (ri) {
      if (dumped >= WB_SALES_DIAG_ROW_CAP_) return;
      wbSalesDiagDumpRow_(vals[ri], c);
      dumped++;
    });
  });
  if (dumped >= WB_SALES_DIAG_ROW_CAP_) console.log('   … дамп обрезан на ' + WB_SALES_DIAG_ROW_CAP_ + ' (см. агрегаты выше)');

  // BODY-300: все строки листа (любая дата), полный сырой вид.
  console.log('=== EVT-HC-BODY-300: все строки листа ===');
  var bodyN = 0;
  for (var rb = 1; rb < vals.length; rb++) {
    if (c.sku < 0 || String(vals[rb][c.sku] || '').trim() !== 'EVT-HC-BODY-300') continue;
    wbSalesDiagDumpRow_(vals[rb], c);
    bodyN++;
  }
  if (bodyN === 0) console.log('   (нет строк BODY-300 в листе)');
}

/** Печать сырой строки листа в диагностике (нужные поля). */
function wbSalesDiagDumpRow_(row, c) {
  function g(i) { return i >= 0 ? row[i] : ''; }
  console.log('   sale_dt=' + g(c.saleDt) +
    ' | lcd=' + g(c.lcd) +
    ' | sku=' + g(c.sku) +
    ' | nm=' + g(c.nm) +
    ' | vendor=' + g(c.vendor) +
    ' | barcode=' + g(c.bar) +
    ' | sale_id=' + g(c.saleId) +
    ' | source_api=' + g(c.src) +
    ' | oper=' + g(c.oper) +
    ' | qty=' + g(c.qty) +
    ' | fin=' + g(c.fin) +
    ' | is_return=' + g(c.ret) +
    ' | is_dup=' + g(c.dup));
}
