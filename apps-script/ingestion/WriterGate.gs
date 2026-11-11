/**
 * ══════════════════════════════════════════════════════════════
 * WRITER GATE — единый fencing-источник правды для писателей WB (этап C, OWNER ACK 11.11.2026)
 * ══════════════════════════════════════════════════════════════
 *
 * Владелец каждого потока — строка в wb_ops.INGEST_WRITER_OWNERSHIP (owner ∈ APPS_SCRIPT | CLOUD_RUN | NONE,
 * epoch — поколение владения). Ей подчиняются ОБА писателя: Cloud Run (cloud/src/ownership/gate.ts) и
 * Apps Script (этот файл). Механизм НЕ опирается на триггеры: ScriptApp видит только триггеры текущего
 * пользователя, поэтому их не перечисляем и не удаляем — неправообладатель просто ничего не пишет.
 *
 * Две линии защиты:
 *   1) wbWriterGate_(stream) — в точке входа, после ScriptLock и до любой записи (BigQuery, лист,
 *      Script Properties). owner ≠ APPS_SCRIPT → SKIPPED_FENCED, выход без записи; ошибка чтения или нет
 *      строки → SKIPPED_GATE_UNAVAILABLE (fail-closed). Запоминает epoch исполнения.
 *   2) wbBqJobsQuery_ / wbBqJobsInsert_ / wbBqTablesInsert_ / wbBqTablesPatch_ — ЕДИНСТВЕННЫЕ записывающие
 *      вызовы BigQuery в коде загрузки (тест cloud/test/writer_gate_apps_script.test.ts; вне потоков остались
 *      только Unitka R3 и КИЗ со своими таблицами). Перед КАЖДОЙ записью (load, DML, DDL, схема) в
 *      таблицу потока владелец перечитывается: не APPS_SCRIPT или epoch ≠ epoch входа → исключение
 *      WRITER_NOT_OWNER / WRITER_EPOCH_CHANGED, запрос в BigQuery не уходит. Это покрывает и пути без
 *      точки входа: меню, ручной backfill, catch-up, runWbDailyRefresh, importWb*FromApiInternal_ и т.п.
 *
 * Остаточное окно (честно): между перечиткой владельца и приёмом задания BigQuery проходят миллисекунды,
 * а уже принятое задание (load/DML) дорабатывает после fence. DML здесь не делается условным в той же
 * транзакции: код загрузчиков проверяет numDmlAffectedRows одиночного DML, а у скрипта его нет.
 * Окно закрывает дренаж координатора: ≥ 7 мин (лимит исполнения 6 мин) + проверка, что в
 * INFORMATION_SCHEMA.JOBS_BY_PROJECT нет незавершённых заданий, пишущих в таблицы потока.
 */

var WB_WRITER_GATE_DATASET_ = 'wb_ops';
var WB_WRITER_GATE_TABLE_ = 'INGEST_WRITER_OWNERSHIP';
/** Минимум тарификации — 10 МБ на таблицу; потолок с запасом, но маленький. */
var WB_WRITER_GATE_MAX_BYTES_ = '20971520';
var WB_WRITER_GATE_EPOCH_PROP_PREFIX_ = 'WB_WRITER_GATE_EPOCH_';
var WB_WRITER_GATE_OWNERS_ = ['APPS_SCRIPT', 'CLOUD_RUN', 'NONE'];

/**
 * Таблицы потоков в датасете BigQuery Apps Script (wb_raw). Тот же список — writeTables профилей
 * координатора (cloud/src/loaders/cutover/profiles.ts); совпадение проверяет тест.
 * INGEST_RUNS общий у заказов/продаж и рекламы.
 */
var WB_WRITER_STREAM_TABLES_ = {
  stocks: { exact: ['RAW_WB_STOCKS', 'RAW_WB_STOCKS_T5', 'WB_STOCKS_SNAPSHOTS'], prefixes: [] },
  statistics: { exact: ['RAW_WB_ORDERS', 'RAW_WB_SALES_RETURNS', 'INGEST_RUNS'], prefixes: [] },
  // Ровно таблицы M3. RAW_WB_ADV_SEARCH_CLUSTERS не переносится — остаётся за Apps Script, gate её не трогает.
  ads: { exact: ['INGEST_RUNS', 'RAW_WB_ADV_CAMPAIGNS', 'RAW_WB_ADV_CAMPAIGN_STATS', 'RAW_WB_ADV_BOOSTER_STATS', 'RAW_WB_ADV_COSTS', 'RAW_WB_ADV_COSTS_RUNS', 'RAW_WB_ADV_QUERY_BIDS', 'RAW_WB_ADV_QUERY_BIDS_RUNS', 'RAW_WB_ADV_QUERY_STATS', 'RAW_WB_ADV_QUERY_STATS_RUNS'], prefixes: [] },
  finance: { exact: ['RAW_WB_FINANCE', 'FINANCE_LOADER_RUNS', 'FINANCE_REPORT_LOADS', 'FINANCE_WEEK_STATUS', 'FINANCE_WEEK_RECON'], prefixes: [] },
  supplies: { exact: ['RAW_WB_SUPPLIES', 'RAW_WB_SUPPLIES_GOODS'], prefixes: [] },
  ref: { exact: ['REF_SKU_MASTER_DATA', 'REF_ACTIVE_VERSION', 'REF_SYNC_RUNS', 'REF_SYNC_TABLE_LOG', 'REF_SKU_MASTER'], prefixes: [] }
};

/** Поколение владения, под которым идёт ЭТО исполнение: { stream: epoch }. Заполняет wbWriterGate_. */
var WB_WRITER_GATE_CTX_ = {};

/**
 * Потоки, у которых gate ВЫКЛЮЧЕН по умолчанию. Остатки (M1) до приёмки координатора переключаются ручной
 * процедурой MIG1B (снятие триггера + resume Scheduler) — gate не должен ей мешать. Включение — Script
 * Property WB_WRITER_GATE_STOCKS = '1' вместе с WRITER_GATE_STOCKS=1 у wb-stocks-prod (док §8).
 */
var WB_WRITER_GATE_OPT_IN_ = { stocks: 'WB_WRITER_GATE_STOCKS' };

/**
 * Журналы прогонов: закрыть СВОЮ строку (UPDATE/MERGE — статус, исход) прогон, открывший её под владением,
 * может и после потери владения: это исход, а не новые данные. Без этого строка осталась бы STARTED навсегда.
 * Новые строки журналов (INSERT/load) и данные — под проверкой.
 */
var WB_WRITER_JOURNAL_TABLES_ = ['INGEST_RUNS', 'FINANCE_LOADER_RUNS', 'WB_STOCKS_SNAPSHOTS', 'REF_SYNC_RUNS'];

/**
 * Метки запросов к таблице владения (текст запроса начинается с метки — INFORMATION_SCHEMA.JOBS):
 *   WRITER_GATE:AS:<поток>:<точка входа>:v1 — ТОЛЬКО gate точки входа: доказательство для координатора;
 *   WRITER_GUARD:AS:<поток>:v1             — перечитка перед записью;
 *   WRITER_GATE_STATUS:AS:<поток>:v1       — ручной wbWriterGateStatus() (доказательством не считается).
 */
var WB_WRITER_GATE_MARK_VERSION_ = 'v1';

/** Строки журналов, открытые ЭТИМ исполнением: { table: { id: true } } — закрыть можно только их. */
var WB_WRITER_OWN_RUNS_ = {};

function wbWriterGateEnabled_(stream) {
  var key = WB_WRITER_GATE_OPT_IN_[stream];
  if (!key) return true;
  return PropertiesService.getScriptProperties().getProperty(key) === '1';
}

// ───────────────────────────────────────────────────────────────
// Точка входа
// ───────────────────────────────────────────────────────────────

/**
 * Gate точки входа. Вызывать ПОСЛЕ ScriptLock и ДО любой записи.
 * @return {{allowed: boolean, result: Object}} allowed=false → вернуть result и выйти, ничего не записав.
 */
function wbWriterGate_(stream, entry) {
  var row;
  try {
    if (!WB_WRITER_STREAM_TABLES_[stream]) throw new Error('неизвестный поток ' + stream);
    if (!/^[A-Za-z0-9_]+$/.test(String(entry || ''))) throw new Error('не указана точка входа');
    if (!wbWriterGateEnabled_(stream)) return { allowed: true, result: null };   // gate потока выключен (M1)
    row = wbWriterGateRead_(stream, 'WRITER_GATE:AS:' + stream + ':' + entry);
  } catch (e) {
    var m = String((e && e.message) || e);
    console.error('WRITER_GATE ' + stream + ': SKIPPED_GATE_UNAVAILABLE — ' + m + ' (fail-closed: запись не выполнена)');
    return { allowed: false, result: { status: 'SKIPPED_GATE_UNAVAILABLE', stream: stream, error_message: m } };
  }
  if (row.owner !== 'APPS_SCRIPT') {
    console.log('WRITER_GATE ' + stream + ': SKIPPED_FENCED — владелец ' + row.owner + ', epoch ' + row.epoch);
    return { allowed: false, result: { status: 'SKIPPED_FENCED', stream: stream, owner: row.owner, epoch: row.epoch } };
  }
  try {
    wbWriterGateOnEpoch_(stream, row.epoch);
  } catch (e2) {
    var m2 = String((e2 && e2.message) || e2);
    console.error('WRITER_GATE ' + stream + ': SKIPPED_GATE_UNAVAILABLE — смена поколения не обработана: ' + m2);
    return { allowed: false, result: { status: 'SKIPPED_GATE_UNAVAILABLE', stream: stream, error_message: m2 } };
  }
  WB_WRITER_GATE_CTX_[stream] = row.epoch;
  return { allowed: true, result: null };
}

/** Короткий параметризованный SELECT; кидает при любой неоднозначности (fail-closed у вызывающего). */
function wbWriterGateRead_(stream, mark) {
  var c = getBqConfig_();
  var res = BigQuery.Jobs.query({
    query: '/* ' + mark + ':' + WB_WRITER_GATE_MARK_VERSION_ + ' */ ' +
      'SELECT owner, CAST(epoch AS STRING) AS epoch FROM `' + c.projectId + '.' + WB_WRITER_GATE_DATASET_ + '.' +
      WB_WRITER_GATE_TABLE_ + '` WHERE stream = @s',
    useLegacySql: false, location: c.location, parameterMode: 'NAMED',
    queryParameters: [{ name: 's', parameterType: { type: 'STRING' }, parameterValue: { value: stream } }],
    maximumBytesBilled: WB_WRITER_GATE_MAX_BYTES_, useQueryCache: false, timeoutMs: 20000
  }, c.projectId);
  if (res.jobComplete !== true) throw new Error('чтение владения не завершилось');
  var rows = res.rows || [];
  if (rows.length === 0) throw new Error('нет строки потока ' + stream + ' в ' + WB_WRITER_GATE_TABLE_);
  if (rows.length > 1) throw new Error(rows.length + ' строк потока ' + stream);
  var owner = String(rows[0].f[0].v || '');
  var epoch = Number(rows[0].f[1].v);
  if (WB_WRITER_GATE_OWNERS_.indexOf(owner) < 0) throw new Error('неизвестный owner ' + owner);
  if (!(epoch >= 1) || Math.floor(epoch) !== epoch) throw new Error('невалидный epoch ' + rows[0].f[1].v);
  return { owner: owner, epoch: epoch };
}

/**
 * Поколение сменилось с прошлого исполнения: владение вернулось к Apps Script после Cloud Run (откат).
 * Заказы/продажи хранят watermark в Script Properties, а Cloud Run двигал его в таблице — без
 * пересинхронизации Apps Script перечитал бы и дописал всё, что уже записал Cloud Run. Берём отметку
 * ровно так, как её читает Cloud Run (MAX строк инкремента ORD_INC_/SALE_INC_). Нет отметки
 * поколения = 1 (до первого переключения): ничего не пишем.
 */
function wbWriterGateOnEpoch_(stream, epoch) {
  var props = PropertiesService.getScriptProperties();
  var key = WB_WRITER_GATE_EPOCH_PROP_PREFIX_ + stream.toUpperCase();
  var seen = props.getProperty(key);
  var seenEpoch = seen ? Number(seen) : 1;
  if (seenEpoch === epoch) return;
  if (stream === 'statistics') wbWriterGateResyncStatistics_();
  props.setProperty(key, String(epoch));
  console.log('WRITER_GATE ' + stream + ': поколение ' + seenEpoch + ' → ' + epoch + (stream === 'statistics' ? ', watermark из таблицы' : ''));
}

function wbWriterGateResyncStatistics_() {
  var c = getBqConfig_();
  var fq = function (t) { return '`' + c.projectId + '.' + c.datasetId + '.' + t + '`'; };
  var res = BigQuery.Jobs.query({
    query: 'SELECT ' +
      "(SELECT COALESCE(MAX(IF(STARTS_WITH(IFNULL(load_id, ''), 'ORD_INC_'), last_change_date, NULL)), MAX(last_change_date)) " +
      'FROM ' + fq(WB_ORDERS_BQ_TABLE_) + " WHERE source_api = '" + ORDERS_RAW_SOURCE_API_ + "' AND last_change_date <> '') AS o, " +
      "(SELECT COALESCE(MAX(IF(STARTS_WITH(IFNULL(load_id, ''), 'SALE_INC_'), last_change_date, NULL)), " +
      "MAX(IF(STARTS_WITH(IFNULL(load_id, ''), 'SALE_RECON_'), NULL, last_change_date))) " +
      'FROM ' + fq(WB_SALES_BQ_TABLE_) + " WHERE source_api = '" + SALES_RAW_SOURCE_API_ + "') AS s",
    useLegacySql: false, location: c.location, useQueryCache: false, timeoutMs: 30000
  }, c.projectId);
  if (res.jobComplete !== true) throw new Error('watermark statistics: запрос не завершился');
  var r = (res.rows || [])[0];
  var o = r ? r.f[0].v : null, s = r ? r.f[1].v : null;
  if (!o || !s) throw new Error('watermark statistics: в таблицах нет отметки');
  var oApi = String(o).replace(' ', 'T');
  if (!ordersValidWatermark_(oApi)) throw new Error('watermark заказов невалиден: ' + o);
  if (!salesValidWatermark_(String(s))) throw new Error('watermark продаж невалиден: ' + s);
  var props = PropertiesService.getScriptProperties();
  props.setProperty(WB_ORDERS_WATERMARK_PROP_, oApi);
  props.setProperty(WB_SALES_WATERMARK_PROP_, String(s));
}

// ───────────────────────────────────────────────────────────────
// Перед каждой записью
// ───────────────────────────────────────────────────────────────

/** Потоки, которым принадлежит таблица (в датасете Apps Script). */
function wbWriterStreamsOfTable_(tableId) {
  var out = [];
  for (var s in WB_WRITER_STREAM_TABLES_) {
    var d = WB_WRITER_STREAM_TABLES_[s];
    var hit = d.exact.indexOf(tableId) >= 0;
    for (var i = 0; !hit && i < d.prefixes.length; i++) hit = tableId.indexOf(d.prefixes[i]) === 0 && !/__CR$/.test(tableId);
    if (hit) out.push(s);
  }
  return out;
}

var WB_WRITER_WRITE_RE_ = /\b(INSERT\s+(?:INTO\s+)?|UPDATE\s+|MERGE\s+(?:INTO\s+)?|DELETE\s+(?:FROM\s+)?|TRUNCATE\s+TABLE\s+|CREATE\s+(?:OR\s+REPLACE\s+)?(?:TEMP(?:ORARY)?\s+)?(?:TABLE|VIEW|MATERIALIZED\s+VIEW)\s+(?:IF\s+NOT\s+EXISTS\s+)?|DROP\s+(?:TABLE|VIEW|MATERIALIZED\s+VIEW)\s+(?:IF\s+EXISTS\s+)?|ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?)`?([A-Za-z0-9_.\-]+)`?/gi;

/** Цели записи в тексте SQL: [{dataset, table, verb}]. Пусто — запрос только читает. */
function wbWriterTargetsOfSql_(sql) {
  var out = [], m;
  var re = new RegExp(WB_WRITER_WRITE_RE_.source, 'gi');
  while ((m = re.exec(String(sql || ''))) !== null) {
    var parts = m[2].split('.');
    out.push({ dataset: parts.length >= 2 ? parts[parts.length - 2] : '', table: parts[parts.length - 1],
      verb: m[1].replace(/\s.*$/, '').toUpperCase() });
  }
  return out;
}

/**
 * Проверка перед записью в таблицы targets. Таблица потока → владелец перечитывается сейчас:
 *   не APPS_SCRIPT                       → WRITER_NOT_OWNER;
 *   epoch ≠ epoch входа этого исполнения → WRITER_EPOCH_CHANGED;
 *   чтение не удалось                    → WRITER_GATE_UNAVAILABLE.
 * Исполнение без точки входа (меню, ручной backfill) проверяет только владельца.
 * Таблица общая у двух потоков (INGEST_RUNS): проверяется поток текущей точки входа, а без неё — оба.
 */
function wbWriterGuard_(targets, what, sqlText, params) {
  var c = getBqConfig_();
  var streams = {};
  for (var i = 0; i < targets.length; i++) {
    var t = targets[i];
    if (t.dataset && t.dataset !== c.datasetId) continue;
    var owners = wbWriterStreamsOfTable_(t.table).filter(wbWriterGateEnabled_);
    // Закрытие СВОЕЙ строки журнала: UPDATE/MERGE без вставки, по id строки, которую открыло это исполнение
    // под владением (поколение входа есть). Остатки при включённом gate — только исходом ERROR.
    var ctxOwners = owners.filter(function (s) { return WB_WRITER_GATE_CTX_[s] !== undefined; });
    if (ctxOwners.length && wbWriterIsOwnJournalClose_(t, sqlText || '', params || [])) continue;
    var withCtx = owners.filter(function (s) { return WB_WRITER_GATE_CTX_[s] !== undefined; });
    var check = withCtx.length ? withCtx : owners;
    for (var j = 0; j < check.length; j++) streams[check[j]] = true;
  }
  for (var s in streams) {
    var row;
    try { row = wbWriterGateRead_(s, 'WRITER_GUARD:AS:' + s); }
    catch (e) { throw new Error('WRITER_GATE_UNAVAILABLE: ' + s + ' — ' + ((e && e.message) || e) + ' (' + what + ' не выполнен)'); }
    if (row.owner !== 'APPS_SCRIPT') {
      throw new Error('WRITER_NOT_OWNER: поток ' + s + ' принадлежит ' + row.owner + ' (epoch ' + row.epoch + ') — ' + what + ' не выполнен');
    }
    var ctx = WB_WRITER_GATE_CTX_[s];
    if (ctx !== undefined && ctx !== row.epoch) {
      throw new Error('WRITER_EPOCH_CHANGED: поток ' + s + ' epoch ' + ctx + ' → ' + row.epoch + ' во время исполнения — ' + what + ' не выполнен');
    }
  }
}

/** Закрытие своей строки журнала (см. WB_WRITER_JOURNAL_TABLES_): только UPDATE/MERGE без вставки, по своему id. */
function wbWriterIsOwnJournalClose_(t, sqlText, params) {
  if (t.verb !== 'UPDATE' && t.verb !== 'MERGE') return false;
  if (WB_WRITER_JOURNAL_TABLES_.indexOf(t.table) < 0) return false;
  if (/WHEN\s+NOT\s+MATCHED/i.test(sqlText)) return false;
  var own = WB_WRITER_OWN_RUNS_[t.table] || {};
  var values = params.map(function (p) { return String((p && p.parameterValue && p.parameterValue.value) || ''); });
  var mine = false;
  for (var id in own) {
    if (sqlText.indexOf(id) >= 0 || values.indexOf(id) >= 0) { mine = true; break; }
  }
  if (!mine) return false;
  if (t.table === 'WB_STOCKS_SNAPSHOTS' && wbWriterGateEnabled_('stocks')) {
    return /\bstatus\s*=\s*'ERROR'/.test(sqlText) && !/'COMPLETE'/.test(sqlText.replace(/status\s*=\s*'STARTED'/, ''));
  }
  return true;
}

/** id строки журнала, открытой этим исполнением (INSERT с параметром run_id / load строк журнала). */
function wbWriterRememberOwnRuns_(table, ids) {
  if (WB_WRITER_JOURNAL_TABLES_.indexOf(table) < 0) return;
  var m = WB_WRITER_OWN_RUNS_[table] || (WB_WRITER_OWN_RUNS_[table] = {});
  for (var i = 0; i < ids.length; i++) if (ids[i]) m[String(ids[i])] = true;
}

/** Единственный вызов BigQuery.Jobs.query загрузчиков: SELECT — как есть, запись — после wbWriterGuard_. */
function wbBqJobsQuery_(req, projectId) {
  var targets = wbWriterTargetsOfSql_(req && req.query);
  var params = (req && req.queryParameters) || [];
  if (targets.length) wbWriterGuard_(targets, 'query', String(req.query || ''), params);
  var res = BigQuery.Jobs.query(req, projectId);
  for (var i = 0; i < targets.length; i++) {
    if (targets[i].verb !== 'INSERT') continue;
    wbWriterRememberOwnRuns_(targets[i].table, params.filter(function (p) { return p && /^(run_id|snapshot_id)$/.test(p.name); })
      .map(function (p) { return p.parameterValue && p.parameterValue.value; }));
  }
  return res;
}

/** Единственный вызов BigQuery.Jobs.insert загрузчиков (load или query-задание). */
function wbBqJobsInsert_(job, projectId, blob) {
  var conf = (job && job.configuration) || {};
  var targets = [];
  if (conf.load && conf.load.destinationTable) {
    targets.push({ dataset: conf.load.destinationTable.datasetId || '', table: conf.load.destinationTable.tableId || '' });
  }
  if (conf.query) {
    targets = targets.concat(wbWriterTargetsOfSql_(conf.query.query));
    if (conf.query.destinationTable) {
      targets.push({ dataset: conf.query.destinationTable.datasetId || '', table: conf.query.destinationTable.tableId || '' });
    }
  }
  var sqlText = conf.query ? String(conf.query.query || '') : '';
  if (targets.length) wbWriterGuard_(targets, conf.load ? 'load' : 'query-задание', sqlText, []);
  var res = blob === undefined ? BigQuery.Jobs.insert(job, projectId) : BigQuery.Jobs.insert(job, projectId, blob);
  // load в журнал (открытие строки): запомнить её id, чтобы потом можно было закрыть только её.
  if (conf.load && conf.load.destinationTable && blob && typeof blob.getDataAsString === 'function') {
    try {
      var ids = String(blob.getDataAsString()).split('\n').filter(String).map(function (l) {
        var o = JSON.parse(l); return o.run_id || o.snapshot_id || '';
      });
      wbWriterRememberOwnRuns_(conf.load.destinationTable.tableId || '', ids);
    } catch (e) { /* не журнал или не NDJSON — нечего запоминать */ }
  }
  return res;
}

/** Создание таблицы (DDL «ensure») — та же проверка, если таблица принадлежит потоку. */
function wbBqTablesInsert_(body, projectId, datasetId) {
  var ref = (body && body.tableReference) || {};
  wbWriterGuard_([{ dataset: ref.datasetId || datasetId || '', table: ref.tableId || '' }], 'Tables.insert');
  return BigQuery.Tables.insert(body, projectId, datasetId);
}

/** Изменение схемы таблицы потока (добавление колонок) — та же проверка. */
function wbBqTablesPatch_(body, projectId, datasetId, tableId) {
  wbWriterGuard_([{ dataset: datasetId || '', table: tableId || '' }], 'Tables.patch');
  return BigQuery.Tables.patch(body, projectId, datasetId, tableId);
}

/**
 * Только чтение: владелец и поколение каждого потока так, как их видит Apps Script. Запускает владелец из
 * редактора после выката (docs/ops/INGEST_CUTOVER_COORDINATOR.md §5, шаг «все писатели видят владельца»).
 * Ничего не пишет: ни BigQuery, ни лист, ни Script Properties.
 */
function wbWriterGateStatus() {
  var out = {};
  for (var s in WB_WRITER_STREAM_TABLES_) {
    if (!wbWriterGateEnabled_(s)) { out[s] = 'gate выключен (' + WB_WRITER_GATE_OPT_IN_[s] + ' ≠ 1) — Apps Script пишет без проверки'; continue; }
    try {
      var r = wbWriterGateRead_(s, 'WRITER_GATE_STATUS:AS:' + s);
      out[s] = r.owner + ' / epoch ' + r.epoch + (r.owner === 'APPS_SCRIPT' ? ' — Apps Script пишет' : ' — Apps Script НЕ пишет');
    } catch (e) {
      out[s] = 'НЕДОСТУПНО: ' + ((e && e.message) || e) + ' — Apps Script НЕ пишет (fail-closed)';
    }
  }
  console.log('WRITER_GATE статус: ' + JSON.stringify(out, null, 2));
  return out;
}

/** Удаление таблицы — та же проверка, если таблица принадлежит потоку. */
function wbBqTablesRemove_(projectId, datasetId, tableId) {
  wbWriterGuard_([{ dataset: datasetId || '', table: tableId || '' }], 'Tables.remove');
  return BigQuery.Tables.remove(projectId, datasetId, tableId);
}
