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
 * Пустой лист/пустой диапазон → [header] (потребители трактуют как «пусто»);
 * в SHEET-режиме пустота не считается ошибкой (аварийный/замороженный снимок).
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
      1,        // quantity — синтез
      false     // is_duplicate — синтез
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

/** Агрегат канонических строк по date×SKU (для parity). Деньги — в копейках. */
function wbSalesParityAggregate_(values) {
  var map = {};
  var minDate = '', maxDate = '';
  for (var r = 1; r < values.length; r++) {
    var row = values[r];
    var day = String(row[0] || '').substring(0, 10);   // sale_dt → yyyy-MM-dd
    if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) continue;
    var sku = String(row[2] || '').trim() || ('nm:' + String(row[3] || '').trim());
    var isRet = (row[8] === true) || (String(row[7] || '').toLowerCase().indexOf('return') >= 0) ||
                (String(row[7] || '').toLowerCase().indexOf('возврат') >= 0);
    var qty = Number(row[10]) || 0;
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
