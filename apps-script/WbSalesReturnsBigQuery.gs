/**
 * ══════════════════════════════════════════════════════════════
 *  WbSalesReturnsBigQuery.gs — BigQuery-приёмник продаж/возвратов (Фаза D2a)
 * ══════════════════════════════════════════════════════════════
 *  Порт по образцу заказов (WbOrdersBigQuery.gs): тяга/нормализация в
 *  WbSalesReturnsLoader НЕ меняют своё листовое поведение. Под флагом
 *  WB_SALES_BQ_SINK общие хелперы продаж пишут не в лист, а в BigQuery
 *  (append-only), дедуп — во вью.
 *
 *  Эмпирика 2 read-only probe (dateFrom 2026-05-28 и 2026-04-13):
 *   • saleID заполнен 100%, уникален на строку, стабилен (не связывает
 *     разные srid/nmId/date), без версий → canonical event_key = saleID;
 *   • srid ключом НЕ является (возврат 'R…' переиспользует srid продажи;
 *     дедуп по srid выкинул бы возврат) — остаётся колонкой для джойнов;
 *   • cap 80 000 не достигнут (3319 строк/90д) → одна страница, resumable
 *     job сейчас не нужен, но упор в лимит = PARTIAL (граница обрезана).
 *
 *  Отличия от заказов:
 *   • типизированная схема (NUMERIC/INT64/BOOL), а не всё STRING — у Sales
 *     API стабильный фактический контракт; raw_json хранит оригинал для
 *     аудита/восстановления;
 *   • дедуп-вью V_WB_SALES_RETURNS: ключ sale_id (не srid), last-wins по
 *     last_change_date; строки без sale_id (processed_status=MISSING_EVENT_KEY)
 *     в каноническую вью НЕ входят;
 *   • партиция по ДАТЕ ПРОДАЖИ (_sale_date DATE), кластер sale_id/srid/wb_nm_id.
 *
 *  Порядок (лестница, как заказы):
 *    C0) wbSalesBqInitC0() — БЕЗ WB API: preflight+флаг, пустая таблица,
 *        вью, счётчики. Fail-closed: при ошибке после флага sink гасится.
 *    C1) importWbSalesReturnsFromApi('YYYY-MM-DD','YYYY-MM-DD') за 1 день →
 *        wbSalesBqStats() → wbSalesBqValidateViews(); проверить в облаке.
 *    Backfill: 90 дней ОДНИМ проходом (один API-fetch, один append).
 *  Откат: wbSalesBqDisable() — снова пишем в лист.
 *
 *  D2a НЕ включает: авто-включение флага, watermark-инкремент, триггер,
 *  cutover потребителей (DashboardWb/Cleanwbdaily/UNIT/PNL не трогаем).
 * ══════════════════════════════════════════════════════════════
 */

var WB_SALES_BQ_PROP_  = 'WB_SALES_BQ_SINK';
var WB_SALES_BQ_TABLE_ = 'RAW_WB_SALES_RETURNS';
var WB_SALES_BQ_VIEW_  = 'V_WB_SALES_RETURNS';
var WB_SALES_BQ_BATCH_ = 2000;

// Типизация колонок RAW (остальные из SALES_RAW_HEADERS_ — STRING).
var SALES_BQ_INT_FIELDS_     = ['raw_row_number', 'income_id', 'wb_nm_id'];
var SALES_BQ_NUMERIC_FIELDS_ = ['total_price', 'discount_percent', 'spp',
                                'payment_sale_amount', 'price_with_disc', 'finished_price', 'for_pay'];
var SALES_BQ_BOOL_FIELDS_    = ['is_supply', 'is_realization', 'is_return'];

/** BigQuery-тип колонки RAW по имени (служебная _sale_date — DATE). */
function salesBqFieldType_(name) {
  if (name === '_sale_date') return 'DATE';
  if (SALES_BQ_INT_FIELDS_.indexOf(name) >= 0) return 'INT64';
  if (SALES_BQ_NUMERIC_FIELDS_.indexOf(name) >= 0) return 'NUMERIC';
  if (SALES_BQ_BOOL_FIELDS_.indexOf(name) >= 0) return 'BOOL';
  return 'STRING';
}

/**
 * Канонизация имени BigQuery-типа для сравнения схемы. BigQuery API отдаёт
 * КАНОНИЧЕСКИЕ имена (INTEGER/BOOLEAN/FLOAT/RECORD), а мы задаём SQL-алиасы
 * (INT64/BOOL/FLOAT64/STRUCT) — это один и тот же тип. Сравнивать нужно
 * канонические формы, иначе аудит схемы ложно падает (C0: raw_row_number
 * INTEGER vs ожидался INT64). Неизвестные типы возвращаются как есть.
 */
function wbSalesBqCanonicalType_(type) {
  var t = String(type || '').toUpperCase().trim();
  var aliases = {
    INT64: 'INTEGER', INTEGER: 'INTEGER',
    BOOL: 'BOOLEAN', BOOLEAN: 'BOOLEAN',
    FLOAT64: 'FLOAT', FLOAT: 'FLOAT',
    STRUCT: 'RECORD', RECORD: 'RECORD'
  };
  return aliases[t] || t;
}

/** allowlist: приёмник продаж пишет ТОЛЬКО в RAW_WB_SALES_RETURNS (fail-closed). */
function wbSalesBqAssertTable_(tableId) {
  if (tableId !== WB_SALES_BQ_TABLE_) {
    throw new Error('Запрещённая Sales BQ-таблица: ' + tableId);
  }
}

function wbSalesBqSinkOn_() {
  return PropertiesService.getScriptProperties().getProperty(WB_SALES_BQ_PROP_) === '1';
}
function wbSalesBqDisable() {
  PropertiesService.getScriptProperties().deleteProperty(WB_SALES_BQ_PROP_);
  console.log('⏹️ Продажи sink → BigQuery ВЫКЛючён. Загрузчик снова пишет в лист.');
}
function wbSalesBqEnable() {
  // Preflight fail-closed: доступ/конфиг/round-trip ДО флага.
  var c = getBqConfig_();
  bqEnsureDataset_();
  bqSelfTest();
  PropertiesService.getScriptProperties().setProperty(WB_SALES_BQ_PROP_, '1');
  console.log('✅ Продажи sink → BigQuery ВКЛючён: ' + c.projectId + '.' + c.datasetId);
}


/** C0 — smoke без WB API: флаг+таблица+вью+счётчики. Fail-closed rollback. */
function wbSalesBqInitC0() {
  try {
    wbSalesBqEnable();
    wbSalesBqEnsureTable_(SALES_RAW_HEADERS_);
    wbSalesBqCreateViews();
    wbSalesBqAssertViews_();
    wbSalesBqStats();
    console.log('✅ C0 продаж готов. Дальше C1 — importWbSalesReturnsFromApi за один день.');
  } catch (e) {
    wbSalesBqDisable();
    console.error('❌ C0 продаж не завершён. Sink ВЫКЛючен: ' + String((e && e.message) || e));
    throw e;
  }
}


/**
 * Гарантирует таблицу RAW_WB_SALES_RETURNS: типизированные колонки из
 * SALES_RAW_HEADERS_ (STRING/INT64/NUMERIC/BOOL) + служебная _sale_date DATE
 * (партиция), кластер sale_id/srid/wb_nm_id. Если таблица есть — аудит типов
 * колонок и аддитивное расширение недостающих; обрыв при несовместимом типе.
 * Партиция по _sale_date проверяется строго. Кластер не проверяем.
 */
function wbSalesBqEnsureTable_(headers) {
  wbSalesBqAssertTable_(WB_SALES_BQ_TABLE_);
  var c = getBqConfig_();
  bqEnsureDataset_();

  var table = null;
  try {
    table = BigQuery.Tables.get(c.projectId, c.datasetId, WB_SALES_BQ_TABLE_);
  } catch (e) {
    var code = Number(e && (e.code || e.statusCode));
    var msg = String((e && e.message) || e);
    var notFound = (code === 404) || (msg.indexOf('Not found') >= 0) || (msg.indexOf('notFound') >= 0);
    if (!notFound) throw new Error('Не удалось проверить RAW_WB_SALES_RETURNS: ' + msg);
  }

  if (!table) {
    var fields = headers.map(function (h) {
      return { name: h, type: salesBqFieldType_(h), mode: 'NULLABLE' };
    });
    fields.push({ name: '_sale_date', type: 'DATE', mode: 'NULLABLE' });
    BigQuery.Tables.insert({
      tableReference: { projectId: c.projectId, datasetId: c.datasetId, tableId: WB_SALES_BQ_TABLE_ },
      schema: { fields: fields },
      timePartitioning: { type: 'DAY', field: '_sale_date' },
      clustering: { fields: ['sale_id', 'srid', 'wb_nm_id'] }
    }, c.projectId, c.datasetId);
    console.log('✅ BQ таблица создана: ' + WB_SALES_BQ_TABLE_ + ' (партиция _sale_date, кластер sale_id/srid/wb_nm_id)');
    return true;
  }

  // Аудит типов колонок (аддитивно).
  var existing = (table.schema && table.schema.fields) || [];
  var byName = {};
  for (var i = 0; i < existing.length; i++) byName[existing[i].name] = existing[i];
  var missing = [];
  for (var h = 0; h < headers.length; h++) {
    var want = salesBqFieldType_(headers[h]);
    var f = byName[headers[h]];
    if (!f) { missing.push({ name: headers[h], type: want, mode: 'NULLABLE' }); continue; }
    // Сравниваем КАНОНИЧЕСКИЕ формы: BigQuery API отдаёт INTEGER/BOOLEAN/…,
    // мы задаём INT64/BOOL/… — это один тип.
    var actualType = wbSalesBqCanonicalType_(f.type);
    var expectedType = wbSalesBqCanonicalType_(want);
    if (actualType !== expectedType) {
      throw new Error('RAW_WB_SALES_RETURNS: колонка ' + headers[h] + ' тип ' + f.type +
        ' (canonical ' + actualType + '), ожидался ' + want + ' (canonical ' + expectedType + ').');
    }
  }
  var sdf = byName['_sale_date'];
  if (!sdf) {
    missing.push({ name: '_sale_date', type: 'DATE', mode: 'NULLABLE' });
  } else if (wbSalesBqCanonicalType_(sdf.type) !== wbSalesBqCanonicalType_('DATE')) {
    throw new Error('RAW_WB_SALES_RETURNS: колонка _sale_date тип ' + sdf.type +
      ' (canonical ' + wbSalesBqCanonicalType_(sdf.type) + '), ожидался DATE.');
  }
  var pf = table.timePartitioning && table.timePartitioning.field;
  if (pf && pf !== '_sale_date') {
    throw new Error('RAW_WB_SALES_RETURNS: партиция по полю ' + pf + ', ожидалось _sale_date.');
  }
  if (!pf) {
    throw new Error('RAW_WB_SALES_RETURNS: таблица не партиционирована по _sale_date. ' +
      'Пересоздайте таблицу (patch партицию не добавляет).');
  }
  if (missing.length) {
    BigQuery.Tables.patch({ schema: { fields: existing.concat(missing) } },
      c.projectId, c.datasetId, WB_SALES_BQ_TABLE_);
    console.log('  RAW_WB_SALES_RETURNS: добавлены колонки → ' + missing.map(function (m) { return m.name; }).join(', '));
  }
  return false;
}


/**
 * Грузит массив объектов-строк продаж в BQ. Значения приводятся к типам
 * колонки (INT64/NUMERIC/BOOL/STRING); служебная _sale_date вычисляется из
 * sale_dt (первые 10 символов) для партиции. Пустые значения опускаются (NULL).
 */
function wbSalesBqAppendRows_(rowObjs) {
  wbSalesBqAssertTable_(WB_SALES_BQ_TABLE_);
  if (!rowObjs || !rowObjs.length) return 0;
  var norm = [];
  for (var i = 0; i < rowObjs.length; i++) {
    var o = rowObjs[i], out = {};
    for (var k in o) {
      if (!o.hasOwnProperty(k)) continue;
      var v = o[k];
      if (v === '' || v === null || v === undefined) continue;
      var t = salesBqFieldType_(k);
      if (t === 'INT64') {
        var iv = parseInt(String(v).replace(/\s/g, ''), 10);
        if (!isNaN(iv)) out[k] = iv;
      } else if (t === 'NUMERIC') {
        var nv = parseFloat(String(v).replace(/\s/g, '').replace(',', '.'));
        if (!isNaN(nv)) out[k] = nv;
      } else if (t === 'BOOL') {
        out[k] = (v === true || String(v).toLowerCase() === 'true');
      } else {
        out[k] = (typeof v === 'string') ? v : String(v);
      }
    }
    var sd = String(o.sale_dt || '').substring(0, 10);
    if (/^\d{4}-\d{2}-\d{2}$/.test(sd)) out._sale_date = sd;
    norm.push(out);
  }
  var total = 0;
  for (var j = 0; j < norm.length; j += WB_SALES_BQ_BATCH_) {
    total += bqLoadRows_(WB_SALES_BQ_TABLE_, norm.slice(j, j + WB_SALES_BQ_BATCH_));
  }
  return total;
}


/**
 * Дедуп-вью V_WB_SALES_RETURNS — ПОСЛЕДНЕЕ состояние каждого события продажи/
 * возврата. Ключ sale_id (доказан probe). Порядок: last_change_date DESC
 * (форма 'T' приводится к пробелу для SAFE_CAST), затем loaded_at DESC,
 * load_id DESC и TO_HEX(MD5(raw_json)) DESC как ПОЛНОСТЬЮ детерминированный
 * tie-break — согласован с внутрипакетным last-wins D2c (при равных
 * last_change_date/loaded_at/load_id выбор состояния не зависит от порядка строк).
 * Строки без sale_id / MISSING_EVENT_KEY исключены. Гарантирует таблицу.
 */
function wbSalesBqCreateViews() {
  wbSalesBqEnsureTable_(SALES_RAW_HEADERS_);
  var c = getBqConfig_();
  function fq(t) { return '`' + c.projectId + '.' + c.datasetId + '.' + t + '`'; }
  var sql =
    'CREATE OR REPLACE VIEW ' + fq(WB_SALES_BQ_VIEW_) + ' AS\n' +
    'SELECT * EXCEPT(_rn) FROM (\n' +
    '  SELECT *, ROW_NUMBER() OVER (\n' +
    '    PARTITION BY sale_id\n' +
    "    ORDER BY SAFE_CAST(REPLACE(last_change_date, 'T', ' ') AS TIMESTAMP) DESC,\n" +
    '             SAFE_CAST(loaded_at AS TIMESTAMP) DESC, load_id DESC,\n' +
    '             TO_HEX(MD5(raw_json)) DESC\n' +
    '  ) AS _rn\n' +
    '  FROM ' + fq(WB_SALES_BQ_TABLE_) + '\n' +
    "  WHERE source_api = '" + SALES_RAW_SOURCE_API_ + "'\n" +
    "    AND sale_id IS NOT NULL AND TRIM(sale_id) <> ''\n" +
    "    AND processed_status <> 'MISSING_EVENT_KEY'\n" +
    ')\nWHERE _rn = 1';
  bqQuery_(sql);
  console.log('✅ Вью создана: ' + WB_SALES_BQ_VIEW_ + ' (sale_id, last-wins по last_change_date)');
}

/** Подтверждает, что V_WB_SALES_RETURNS существует и является VIEW. */
function wbSalesBqAssertViews_() {
  var c = getBqConfig_();
  var t = BigQuery.Tables.get(c.projectId, c.datasetId, WB_SALES_BQ_VIEW_);
  if (!t.view) throw new Error(WB_SALES_BQ_VIEW_ + ': объект существует, но не VIEW');
  console.log('✅ ' + WB_SALES_BQ_VIEW_ + ' подтверждена.');
}

/** Ручной алиас для приёмки (та же проверка, что wbSalesBqAssertViews_). */
function wbSalesBqValidateViews() {
  wbSalesBqAssertViews_();
}

/** Счётчики: строк в RAW, в дедуп-вью и карантин MISSING_EVENT_KEY. */
function wbSalesBqStats() {
  var c = getBqConfig_();
  function q(sql) { var r = bqQuery_(sql); return (r && r.rows && r.rows.length) ? r.rows[0].f[0].v : '0'; }
  function fqt(t) { return '`' + c.projectId + '.' + c.datasetId + '.' + t + '`'; }
  try {
    var raw = q('SELECT COUNT(*) FROM ' + fqt(WB_SALES_BQ_TABLE_));
    console.log(WB_SALES_BQ_TABLE_ + ': ' + raw);
  } catch (e) { console.error('❌ ' + WB_SALES_BQ_TABLE_ + ': ' + String((e && e.message) || e)); }
  try {
    var mek = q('SELECT COUNTIF(processed_status = \'MISSING_EVENT_KEY\') FROM ' + fqt(WB_SALES_BQ_TABLE_));
    console.log(WB_SALES_BQ_TABLE_ + ' (MISSING_EVENT_KEY): ' + mek);
  } catch (e3) {}
  try {
    var v = q('SELECT COUNT(*) FROM ' + fqt(WB_SALES_BQ_VIEW_));
    console.log(WB_SALES_BQ_VIEW_ + ' (уник. sale_id): ' + v);
  } catch (e2) { console.log(WB_SALES_BQ_VIEW_ + ': (вью ещё нет)'); }
}

/** COUNT(*) из дедуп-вью V_WB_SALES_RETURNS (для диагностики). */
function wbSalesBqViewCount_() {
  var c = getBqConfig_();
  var r = bqQuery_('SELECT COUNT(*) FROM `' + c.projectId + '.' + c.datasetId + '.' + WB_SALES_BQ_VIEW_ + '`');
  return (r && r.rows && r.rows.length) ? String(r.rows[0].f[0].v) : '0';
}

// ═══════════════════════════════════════
//  D2c: watermark-инкремент — хелперы чтения RAW
// ═══════════════════════════════════════

/**
 * MAX(last_change_date) среди API-строк RAW (для bootstrap watermark).
 * Sales хранит last_change_date уже с 'T' → возвращаем как есть (без замены
 * пробела, в отличие от Orders). '' если строк нет.
 */
function wbSalesBqMaxLastChange_() {
  var c = getBqConfig_();
  var sql = 'SELECT MAX(last_change_date) FROM `' + c.projectId + '.' + c.datasetId + '.' + WB_SALES_BQ_TABLE_ + '` ' +
            "WHERE source_api = '" + SALES_RAW_SOURCE_API_ + "'";
  var r = bqQuery_(sql);
  var rows = (r && r.rows) || [];
  return (rows.length && rows[0].f[0].v != null) ? String(rows[0].f[0].v) : '';
}

/**
 * STATE-ключи sale_id|md5(raw_json) на ГРАНИЦЕ watermark (last_change_date ==
 * watermark), уже присутствующие в RAW. Защита секундной точности + защита от
 * ложного дубля: новая ИЛИ ИЗМЕНЁННАЯ продажа с тем же lastChangeDate дописывается,
 * если пары sale_id|state ещё нет. Ключ — по полному состоянию (raw_json), а НЕ
 * по row_hash (тот считается лишь из srid/nmId/sale_dt/sale_id/operation_type и
 * не меняется при изменении цены/склада/скидки → давал бы ложный дубль).
 * MD5 согласован с Apps Script salesMd5_ (обе стороны — lowercase hex над одним
 * и тем же raw_json = JSON.stringify(апи-строки)). Схему RAW не меняем.
 * Sales хранит last_change_date с 'T' → сравниваем напрямую.
 */
function wbSalesBqBoundaryStateKeys_(watermark) {
  var c = getBqConfig_();
  var esc = String(watermark || '').replace(/\\/g, '\\\\').replace(/'/g, "\\'");
  var sql = 'SELECT DISTINCT sale_id, TO_HEX(MD5(raw_json)) AS sh FROM `' +
            c.projectId + '.' + c.datasetId + '.' + WB_SALES_BQ_TABLE_ + '` ' +
            "WHERE source_api = '" + SALES_RAW_SOURCE_API_ + "' AND last_change_date = '" + esc + "' " +
            "AND sale_id IS NOT NULL AND TRIM(sale_id) <> '' AND raw_json IS NOT NULL";
  var r = bqQuery_(sql);
  var rows = (r && r.rows) || [];
  var set = {};
  for (var i = 0; i < rows.length; i++) {
    var f = rows[i].f;
    var sid = (f[0] && f[0].v != null) ? String(f[0].v) : '';
    var sh = (f[1] && f[1].v != null) ? String(f[1].v) : '';
    set[sid + '|' + sh] = true;
  }
  return set;
}

/**
 * RANGE-WIDE набор STATE-ключей sale_id|md5(raw_json) для ночной пересверки
 * (Night Reconciliation). Возвращает множество уже присутствующих в RAW состояний,
 * у которых last_change_date >= fromLcd — пересверка дописывает только те sale_id|state,
 * которых в наборе НЕТ (gaps_filled). Отличие от wbSalesBqBoundaryStateKeys_:
 * там точное равенство границе (== watermark), здесь диапазон (>= fromLcd) за всё окно.
 *
 * ВАЖНО: fromLcd — валидированный литерал YYYY-MM-DDThh:mm:ss (та же строгая проверка,
 * что watermark, выполняется у вызывающего ДО SQL); дополнительно экранируется здесь.
 * Sales хранит last_change_date с 'T' (фикс-ISO) → лексикографическое сравнение >=
 * хронологически корректно. Fail-safe фильтры sale_id/raw_json как во вью. Схему RAW
 * не меняем. MD5 согласован с Apps Script salesMd5_ (обе стороны — lowercase hex над
 * одним raw_json = JSON.stringify(апи-строки)).
 */
function wbSalesBqStateKeysSince_(fromLcd) {
  var c = getBqConfig_();
  var esc = String(fromLcd || '').replace(/\\/g, '\\\\').replace(/'/g, "\\'");
  var sql = 'SELECT DISTINCT sale_id, TO_HEX(MD5(raw_json)) AS sh FROM `' +
            c.projectId + '.' + c.datasetId + '.' + WB_SALES_BQ_TABLE_ + '` ' +
            "WHERE source_api = '" + SALES_RAW_SOURCE_API_ + "' AND last_change_date >= '" + esc + "' " +
            "AND sale_id IS NOT NULL AND TRIM(sale_id) <> '' AND raw_json IS NOT NULL";
  var r = bqQuery_(sql);
  var rows = (r && r.rows) || [];
  var set = {};
  for (var i = 0; i < rows.length; i++) {
    var f = rows[i].f;
    var sid = (f[0] && f[0].v != null) ? String(f[0].v) : '';
    var sh = (f[1] && f[1].v != null) ? String(f[1].v) : '';
    set[sid + '|' + sh] = true;
  }
  return set;
}

/** Self-test канонизации типов (без BigQuery). Запускать вручную из редактора. */
function wbSalesBqTypeAliasSelfTest() {
  var cases = [
    ['INT64', 'INTEGER'],
    ['BOOL', 'BOOLEAN'],
    ['FLOAT64', 'FLOAT'],
    ['STRUCT', 'RECORD'],
    ['NUMERIC', 'NUMERIC'],
    ['STRING', 'STRING'],
    ['DATE', 'DATE']
  ];
  cases.forEach(function (pair) {
    var a = wbSalesBqCanonicalType_(pair[0]);
    var b = wbSalesBqCanonicalType_(pair[1]);
    if (a !== b) throw new Error(pair[0] + ' != ' + pair[1] + ' (canonical ' + a + ' / ' + b + ')');
  });
  console.log('✅ wbSalesBqTypeAliasSelfTest PASS');
}
