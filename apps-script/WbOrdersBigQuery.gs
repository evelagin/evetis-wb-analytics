/**
 * ══════════════════════════════════════════════════════════════
 *  WbOrdersBigQuery.gs — BigQuery-приёмник заказов (Фаза D1)
 * ══════════════════════════════════════════════════════════════
 *  Порт по образцу рекламы (WbAdsBigQuery.gs): тяга/нормализация в
 *  WbOrdersLoader НЕ меняются. Под флагом WB_ORDERS_BQ_SINK общие
 *  хелперы заказов пишут не в лист, а в BigQuery (append-only),
 *  дедуп — во вью.
 *
 *  Отличия от рекламы (заказы МУТИРУЮТ: заказ → отмена):
 *   • партиция по ДАТЕ ЗАКАЗА (типизированная _order_date DATE), не по
 *     времени загрузки — для дешёвых запросов витрин по order_dt;
 *   • дедуп-вью V_WB_ORDERS хранит ПОСЛЕДНЕЕ состояние каждого заказа:
 *     ключ srid, порядок last_change_date DESC (время изменения на стороне WB),
 *     затем loaded_at DESC, load_id DESC как tie-break. row_hash как ключ НЕ
 *     годится — он включает is_cancel, и отменённая версия одной srid стала бы
 *     отдельной строкой (задвоение заказа).
 *
 *  Порядок (лестница, как реклама):
 *    C0) wbOrdersBqInit()  — БЕЗ WB API: preflight+флаг, пустая таблица,
 *        вью, счётчики. Fail-closed: при ошибке после флага sink гасится.
 *    C1) importWbOrdersFromApi('YYYY-MM-DD','YYYY-MM-DD') за 1 день →
 *        wbOrdersBqStats() → wbOrdersBqCreateViews(); проверить в облаке.
 *    Backfill: последние 90 дней (потолок API заказов) ОДНИМ проходом
 *        importWbOrdersFromApi('<начало 90д>','<сегодня>') — у эндпоинта нет
 *        dateTo, окна объём ответа не уменьшают (только перетягивают историю).
 *  Откат: wbOrdersBqDisable() — снова пишем в лист.
 * ══════════════════════════════════════════════════════════════
 */

var WB_ORDERS_BQ_PROP_  = 'WB_ORDERS_BQ_SINK';
var WB_ORDERS_BQ_TABLE_ = 'RAW_WB_ORDERS';
var WB_ORDERS_BQ_VIEW_  = 'V_WB_ORDERS';
var WB_ORDERS_BQ_BATCH_ = 2000;

/** allowlist: приёмник заказов пишет ТОЛЬКО в RAW_WB_ORDERS (fail-closed). */
function wbOrdersBqAssertTable_(tableId) {
  if (tableId !== WB_ORDERS_BQ_TABLE_) {
    throw new Error('Запрещённая Orders BQ-таблица: ' + tableId);
  }
}

function wbOrdersBqSinkOn_() {
  return PropertiesService.getScriptProperties().getProperty(WB_ORDERS_BQ_PROP_) === '1';
}
function wbOrdersBqDisable() {
  PropertiesService.getScriptProperties().deleteProperty(WB_ORDERS_BQ_PROP_);
  console.log('⏹️ Заказы sink → BigQuery ВЫКЛючён. Загрузчик снова пишет в лист.');
}
function wbOrdersBqEnable() {
  // Preflight fail-closed: доступ/конфиг/round-trip ДО флага.
  var c = getBqConfig_();
  bqEnsureDataset_();
  bqSelfTest();
  PropertiesService.getScriptProperties().setProperty(WB_ORDERS_BQ_PROP_, '1');
  console.log('✅ Заказы sink → BigQuery ВКЛючён: ' + c.projectId + '.' + c.datasetId);
}


/** C0 — smoke без WB API: флаг+таблица+вью+счётчики. Fail-closed rollback. */
function wbOrdersBqInit() {
  try {
    wbOrdersBqEnable();
    wbOrdersBqEnsureTable_(ORDERS_RAW_HEADERS_);
    wbOrdersBqCreateViews();
    wbOrdersBqAssertViews_();
    wbOrdersBqStats();
    console.log('✅ C0 заказов готов. Дальше C1 — importWbOrdersFromApi за один день.');
  } catch (e) {
    wbOrdersBqDisable();
    console.error('❌ C0 заказов не завершён. Sink ВЫКЛючен: ' + String((e && e.message) || e));
    throw e;
  }
}


/**
 * Гарантирует таблицу RAW_WB_ORDERS: STRING-колонки из headers + служебная
 * _order_date DATE (партиция), кластер wb_nm_id/srid. Если таблица есть —
 * аудит СХЕМЫ КОЛОНОК и аддитивное расширение (STRING NULLABLE); обрыв при
 * несовместимом типе. Партиция по _order_date проверяется строго (обрыв, если
 * таблица не партиционирована или партиция по другому полю). Кластер не проверяем.
 */
function wbOrdersBqEnsureTable_(headers) {
  wbOrdersBqAssertTable_(WB_ORDERS_BQ_TABLE_);
  var c = getBqConfig_();
  bqEnsureDataset_();

  var table = null;
  try {
    table = BigQuery.Tables.get(c.projectId, c.datasetId, WB_ORDERS_BQ_TABLE_);
  } catch (e) {
    var code = Number(e && (e.code || e.statusCode));
    var msg = String((e && e.message) || e);
    var notFound = (code === 404) || (msg.indexOf('Not found') >= 0) || (msg.indexOf('notFound') >= 0);
    if (!notFound) throw new Error('Не удалось проверить RAW_WB_ORDERS: ' + msg);
  }

  if (!table) {
    var fields = headers.map(function (h) { return { name: h, type: 'STRING', mode: 'NULLABLE' }; });
    fields.push({ name: '_order_date', type: 'DATE', mode: 'NULLABLE' });
    BigQuery.Tables.insert({
      tableReference: { projectId: c.projectId, datasetId: c.datasetId, tableId: WB_ORDERS_BQ_TABLE_ },
      schema: { fields: fields },
      timePartitioning: { type: 'DAY', field: '_order_date' },
      clustering: { fields: ['wb_nm_id', 'srid'] }
    }, c.projectId, c.datasetId);
    console.log('✅ BQ таблица создана: ' + WB_ORDERS_BQ_TABLE_ + ' (партиция _order_date, кластер wb_nm_id/srid)');
    return true;
  }

  // Аудит схемы колонок (аддитивно).
  var existing = (table.schema && table.schema.fields) || [];
  var byName = {};
  for (var i = 0; i < existing.length; i++) byName[existing[i].name] = existing[i];
  var missing = [];
  for (var h = 0; h < headers.length; h++) {
    var f = byName[headers[h]];
    if (!f) { missing.push({ name: headers[h], type: 'STRING', mode: 'NULLABLE' }); continue; }
    if (String(f.type).toUpperCase() !== 'STRING') {
      throw new Error('RAW_WB_ORDERS: колонка ' + headers[h] + ' тип ' + f.type + ', ожидался STRING.');
    }
  }
  // Служебная партиционная колонка _order_date: должна быть DATE. Если её нет —
  // добавляем аддитивно (partition field задаётся ТОЛЬКО при создании; patch
  // колонку добавит, но непартиционированную таблицу партиционированной не сделает).
  var odf = byName['_order_date'];
  if (!odf) {
    missing.push({ name: '_order_date', type: 'DATE', mode: 'NULLABLE' });
  } else if (String(odf.type).toUpperCase() !== 'DATE') {
    throw new Error('RAW_WB_ORDERS: колонка _order_date тип ' + odf.type + ', ожидался DATE.');
  }
  // Строгая проверка: существующая таблица обязана быть партиционирована по
  // _order_date. Иначе — стоп (иначе тихо получим дорогие full-scan запросы витрин).
  var pf = table.timePartitioning && table.timePartitioning.field;
  if (pf && pf !== '_order_date') {
    throw new Error('RAW_WB_ORDERS: партиция по полю ' + pf + ', ожидалось _order_date.');
  }
  if (!pf) {
    throw new Error('RAW_WB_ORDERS: таблица не партиционирована по _order_date. ' +
      'Пересоздайте таблицу (patch партицию не добавляет).');
  }
  if (missing.length) {
    BigQuery.Tables.patch({ schema: { fields: existing.concat(missing) } },
      c.projectId, c.datasetId, WB_ORDERS_BQ_TABLE_);
    console.log('  RAW_WB_ORDERS: добавлены колонки → ' + missing.map(function (m) { return m.name; }).join(', '));
  }
  return false;
}


/**
 * Грузит массив объектов-строк заказов в BQ. Значения → STRING; служебная
 * _order_date вычисляется из order_dt (первые 10 символов) для партиции.
 */
function wbOrdersBqAppendRows_(rowObjs) {
  wbOrdersBqAssertTable_(WB_ORDERS_BQ_TABLE_);
  if (!rowObjs || !rowObjs.length) return 0;
  var norm = [];
  for (var i = 0; i < rowObjs.length; i++) {
    var o = rowObjs[i], out = {};
    for (var k in o) {
      if (!o.hasOwnProperty(k)) continue;
      var v = o[k];
      if (v === '' || v === null || v === undefined) continue;
      out[k] = (typeof v === 'string') ? v : String(v);
    }
    var od = String(o.order_dt || '').substring(0, 10);
    if (/^\d{4}-\d{2}-\d{2}$/.test(od)) out._order_date = od;
    norm.push(out);
  }
  var total = 0;
  for (var j = 0; j < norm.length; j += WB_ORDERS_BQ_BATCH_) {
    total += bqLoadRows_(WB_ORDERS_BQ_TABLE_, norm.slice(j, j + WB_ORDERS_BQ_BATCH_));
  }
  return total;
}


/**
 * Дедуп-вью V_WB_ORDERS — ПОСЛЕДНЕЕ состояние каждого заказа.
 * Ключ srid (при пустом — row_hash). Порядок: last_change_date DESC (время
 * изменения заказа на стороне WB — устойчивый last-wins при перекрытии
 * backfill/поздней отмене), затем loaded_at DESC, load_id DESC как tie-break.
 * Гарантирует таблицу перед созданием.
 */
function wbOrdersBqCreateViews() {
  wbOrdersBqEnsureTable_(ORDERS_RAW_HEADERS_);
  var c = getBqConfig_();
  function fq(t) { return '`' + c.projectId + '.' + c.datasetId + '.' + t + '`'; }
  var sql =
    'CREATE OR REPLACE VIEW ' + fq(WB_ORDERS_BQ_VIEW_) + ' AS\n' +
    'SELECT * EXCEPT(_rn) FROM (\n' +
    '  SELECT *, ROW_NUMBER() OVER (\n' +
    "    PARTITION BY COALESCE(NULLIF(srid, ''), row_hash)\n" +
    '    ORDER BY SAFE_CAST(last_change_date AS TIMESTAMP) DESC,\n' +
    '             SAFE_CAST(loaded_at AS TIMESTAMP) DESC, load_id DESC\n' +
    '  ) AS _rn\n' +
    '  FROM ' + fq(WB_ORDERS_BQ_TABLE_) + '\n' +
    "  WHERE source_api = '" + ORDERS_RAW_SOURCE_API_ + "'\n" +
    ')\nWHERE _rn = 1';
  bqQuery_(sql);
  console.log('✅ Вью создана: ' + WB_ORDERS_BQ_VIEW_ + ' (srid, last-wins по last_change_date)');
}

/** Подтверждает, что V_WB_ORDERS существует и является VIEW. */
function wbOrdersBqAssertViews_() {
  var c = getBqConfig_();
  var t = BigQuery.Tables.get(c.projectId, c.datasetId, WB_ORDERS_BQ_VIEW_);
  if (!t.view) throw new Error(WB_ORDERS_BQ_VIEW_ + ': объект существует, но не VIEW');
  console.log('✅ ' + WB_ORDERS_BQ_VIEW_ + ' подтверждена.');
}

/** Счётчики: строк в RAW и в дедуп-вью. */
function wbOrdersBqStats() {
  var c = getBqConfig_();
  function q(sql) { var r = bqQuery_(sql); return (r && r.rows && r.rows.length) ? r.rows[0].f[0].v : '0'; }
  try {
    var raw = q('SELECT COUNT(*) FROM `' + c.projectId + '.' + c.datasetId + '.' + WB_ORDERS_BQ_TABLE_ + '`');
    console.log(WB_ORDERS_BQ_TABLE_ + ': ' + raw);
  } catch (e) { console.error('❌ ' + WB_ORDERS_BQ_TABLE_ + ': ' + String((e && e.message) || e)); }
  try {
    var v = q('SELECT COUNT(*) FROM `' + c.projectId + '.' + c.datasetId + '.' + WB_ORDERS_BQ_VIEW_ + '`');
    console.log(WB_ORDERS_BQ_VIEW_ + ' (уник. заказов): ' + v);
  } catch (e2) { console.log(WB_ORDERS_BQ_VIEW_ + ': (вью ещё нет)'); }
}

/**
 * MAX(last_change_date) из RAW (source_api='WB_API_ORDERS') — источник bootstrap
 * watermark для D1.2. Возвращает строку в формате хранения ('YYYY-MM-DD HH:MM:SS')
 * или '' если данных нет. Лексический MAX корректен: формат фиксированной ширины.
 */
function wbOrdersBqMaxLastChangeDate_() {
  var c = getBqConfig_();
  var sql = 'SELECT MAX(last_change_date) FROM `' + c.projectId + '.' + c.datasetId + '.' + WB_ORDERS_BQ_TABLE_ + '` ' +
            "WHERE source_api = '" + ORDERS_RAW_SOURCE_API_ + "' AND last_change_date <> ''";
  var r = bqQuery_(sql);
  var v = (r && r.rows && r.rows.length) ? r.rows[0].f[0].v : null;
  return (v === null || v === undefined) ? '' : String(v);
}

/**
 * Существующие пары srid|row_hash в RAW на ТОЧНОЙ границе last_change_date
 * (форма хранения, с пробелом). Для D1.2: инкремент дописывает граничные строки,
 * которых ещё нет, и не размножает уже записанные. Возвращает map {srid|row_hash:true}.
 */
function wbOrdersBqBoundaryKeys_(lastChangeStorage) {
  var c = getBqConfig_();
  var esc = String(lastChangeStorage || '').replace(/\\/g, '\\\\').replace(/'/g, "\\'");
  var sql = 'SELECT DISTINCT srid, row_hash FROM `' + c.projectId + '.' + c.datasetId + '.' + WB_ORDERS_BQ_TABLE_ + '` ' +
            "WHERE source_api = '" + ORDERS_RAW_SOURCE_API_ + "' AND last_change_date = '" + esc + "'";
  var r = bqQuery_(sql);
  var rows = (r && r.rows) || [];
  var set = {};
  for (var i = 0; i < rows.length; i++) {
    var f = rows[i].f;
    var srid = (f[0] && f[0].v != null) ? String(f[0].v) : '';
    var rh = (f[1] && f[1].v != null) ? String(f[1].v) : '';
    set[srid + '|' + rh] = true;
  }
  return set;
}

/** COUNT(*) из дедуп-вью V_WB_ORDERS (для диагностики). */
function wbOrdersBqViewCount_() {
  var c = getBqConfig_();
  var r = bqQuery_('SELECT COUNT(*) FROM `' + c.projectId + '.' + c.datasetId + '.' + WB_ORDERS_BQ_VIEW_ + '`');
  return (r && r.rows && r.rows.length) ? String(r.rows[0].f[0].v) : '0';
}
