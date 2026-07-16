/**
 * ══════════════════════════════════════════════════════════════
 * EVETIS WB — WbStocksBigQuery.gs   (Фаза E — остатки, BQ-слой)
 *
 * Приёмник остатков в BigQuery: RAW_WB_STOCKS (append-only снапшоты) +
 * manifest WB_STOCKS_SNAPSHOTS (источник истины статуса снимка) +
 * V_WB_STOCKS_CURRENT (только последний COMPLETE-снимок).
 *
 * Источник данных — T6 `stocks-report/wb-warehouses` (плоский снимок,
 * grain: snapshot × nmId × chrtId × warehouseId). Тяга/оркестрация — в
 * WbStocksSnapshot.gs. Здесь только BQ-механика.
 *
 * КЛЮЧЕВЫЕ ГАРАНТИИ (аудит C1–C3):
 *   • C2 — manifest STARTED вставляется ДО fetch; финал UPDATE строго
 *     WHERE status='STARTED' с проверкой numDmlAffectedRows==1;
 *   • C3 — детерминированные BigQuery job ID на каждый batch/insert
 *     (STOCK_<snapshot_id>_BATCH_<n>); при сетевом повторе НЕ вставляем
 *     заново, а находим существующий job и ждём DONE (иначе — дубли RAW);
 *   • load job коммитит сразу (не streaming) → DML UPDATE вставленной
 *     manifest-строки безопасен;
 *   • VIEW отдаёт строки RAW только последнего COMPLETE snapshot_id —
 *     частично записанный/ошибочный снимок в текущие остатки не попадает.
 *
 * Общие bqLoadRows_/bqQuery_ НЕ трогаем (их используют продажи/заказы/
 * финансы). Здесь — свои stocks-локальные обёртки с детерминированным
 * jobId и numDmlAffectedRows.
 * ══════════════════════════════════════════════════════════════
 */

var WB_STOCKS_BQ_SINK_PROP_   = 'WB_STOCKS_BQ_SINK';
var WB_STOCKS_RAW_TABLE_      = 'RAW_WB_STOCKS';
var WB_STOCKS_MANIFEST_TABLE_ = 'WB_STOCKS_SNAPSHOTS';
var WB_STOCKS_VIEW_           = 'V_WB_STOCKS_CURRENT';
var WB_STOCKS_SOURCE_API_     = 'WB_API_STOCKS';
var WB_STOCKS_BQ_BATCH_       = 2000;

// ───────────────────────────────────────────────────────────────
// Схемы
// ───────────────────────────────────────────────────────────────

/** Поля RAW_WB_STOCKS (без служебной _snapshot_date — её добавляет ensure). */
function wbStocksRawFields_() {
  return [
    { name: 'load_id', type: 'STRING' },
    { name: 'snapshot_id', type: 'STRING' },
    { name: 'snapshot_ts', type: 'TIMESTAMP' },
    { name: 'source_api', type: 'STRING' },
    { name: 'nm_id', type: 'INT64' },
    { name: 'chrt_id', type: 'INT64' },
    { name: 'warehouse_id', type: 'INT64' },
    { name: 'warehouse_name', type: 'STRING' },
    { name: 'region_name', type: 'STRING' },
    { name: 'quantity', type: 'INT64' },
    { name: 'in_way_to_client', type: 'INT64' },
    { name: 'in_way_from_client', type: 'INT64' },
    { name: 'is_aggregate_warehouse', type: 'BOOL' },
    { name: 'internal_sku', type: 'STRING' },
    { name: 'sku_match_status', type: 'STRING' },
    { name: 'raw_json', type: 'STRING' }
  ];
}

/** Поля WB_STOCKS_SNAPSHOTS (manifest — источник истины статуса снимка). */
function wbStocksManifestFields_() {
  return [
    { name: 'snapshot_id', type: 'STRING' },
    { name: 'started_at', type: 'TIMESTAMP' },
    { name: 'completed_at', type: 'TIMESTAMP' },
    { name: 'status', type: 'STRING' },              // STARTED / COMPLETE / ERROR
    { name: 'period_from', type: 'STRING' },
    { name: 'period_to', type: 'STRING' },
    { name: 'expected_rows', type: 'INT64' },
    { name: 'written_rows', type: 'INT64' },
    { name: 'distinct_keys', type: 'INT64' },
    { name: 'duplicate_keys', type: 'INT64' },
    { name: 'unique_nm_ids', type: 'INT64' },
    { name: 'warehouses_count', type: 'INT64' },
    { name: 'qty_positive_rows', type: 'INT64' },
    { name: 'qty_zero_rows', type: 'INT64' },
    { name: 'aggregate_warehouse_rows', type: 'INT64' },
    { name: 'sum_quantity_all_t6', type: 'INT64' },
    { name: 'sum_quantity_physical_t6', type: 'INT64' },
    { name: 't5_control_sum', type: 'INT64' },
    { name: 'control_status', type: 'STRING' },       // OK / MISMATCH / T5_UNAVAILABLE
    { name: 'control_delta', type: 'INT64' },
    { name: 'unmatched_nm_ids', type: 'STRING' },      // JSON-массив
    { name: 'error_message', type: 'STRING' }
  ];
}

// ───────────────────────────────────────────────────────────────
// Sink флаг + preflight
// ───────────────────────────────────────────────────────────────

function wbStocksBqSinkOn_() {
  return PropertiesService.getScriptProperties().getProperty(WB_STOCKS_BQ_SINK_PROP_) === '1';
}
function wbStocksBqDisable() {
  PropertiesService.getScriptProperties().deleteProperty(WB_STOCKS_BQ_SINK_PROP_);
  console.log('⏹️ Остатки sink → BigQuery ВЫКЛючён.');
}
function wbStocksBqEnable() {
  var c = getBqConfig_();
  bqEnsureDataset_();
  bqSelfTest();
  PropertiesService.getScriptProperties().setProperty(WB_STOCKS_BQ_SINK_PROP_, '1');
  console.log('✅ Остатки sink → BigQuery ВКЛючён: ' + c.projectId + '.' + c.datasetId);
}

/** allowlist: приёмник остатков пишет ТОЛЬКО в RAW_WB_STOCKS / WB_STOCKS_SNAPSHOTS. */
function wbStocksBqAssertTable_(tableId) {
  if (tableId !== WB_STOCKS_RAW_TABLE_ && tableId !== WB_STOCKS_MANIFEST_TABLE_) {
    throw new Error('Запрещённая Stocks BQ-таблица: ' + tableId);
  }
}

// ───────────────────────────────────────────────────────────────
// Ensure таблиц
// ───────────────────────────────────────────────────────────────

/** RAW_WB_STOCKS: партиция _snapshot_date DATE, кластер nm_id/warehouse_id. Create-if-missing. */
function wbStocksBqEnsureRaw_() {
  var c = getBqConfig_();
  bqEnsureDataset_();
  try {
    BigQuery.Tables.get(c.projectId, c.datasetId, WB_STOCKS_RAW_TABLE_);
    return false;
  } catch (e) {
    if (!wbStocksBqIsNotFound_(e)) throw new Error('Не удалось проверить ' + WB_STOCKS_RAW_TABLE_ + ': ' + ((e && e.message) || e));
  }
  var fields = wbStocksRawFields_().slice();
  fields.push({ name: '_snapshot_date', type: 'DATE' });
  BigQuery.Tables.insert({
    tableReference: { projectId: c.projectId, datasetId: c.datasetId, tableId: WB_STOCKS_RAW_TABLE_ },
    schema: { fields: fields },
    timePartitioning: { type: 'DAY', field: '_snapshot_date' },
    clustering: { fields: ['nm_id', 'warehouse_id'] }
  }, c.projectId, c.datasetId);
  console.log('✅ BQ таблица создана: ' + WB_STOCKS_RAW_TABLE_ + ' (партиция _snapshot_date, кластер nm_id/warehouse_id)');
  return true;
}

/** WB_STOCKS_SNAPSHOTS (manifest). Create-if-missing (небольшая таблица, без партиции). */
function wbStocksBqEnsureManifest_() {
  var c = getBqConfig_();
  bqEnsureDataset_();
  try {
    BigQuery.Tables.get(c.projectId, c.datasetId, WB_STOCKS_MANIFEST_TABLE_);
    return false;
  } catch (e) {
    if (!wbStocksBqIsNotFound_(e)) throw new Error('Не удалось проверить ' + WB_STOCKS_MANIFEST_TABLE_ + ': ' + ((e && e.message) || e));
  }
  BigQuery.Tables.insert({
    tableReference: { projectId: c.projectId, datasetId: c.datasetId, tableId: WB_STOCKS_MANIFEST_TABLE_ },
    schema: { fields: wbStocksManifestFields_() }
  }, c.projectId, c.datasetId);
  console.log('✅ BQ таблица создана: ' + WB_STOCKS_MANIFEST_TABLE_ + ' (manifest снимков)');
  return true;
}

function wbStocksBqIsNotFound_(e) {
  var code = Number(e && (e.code || e.statusCode));
  var msg = String((e && e.message) || e);
  return (code === 404) || (msg.indexOf('Not found') >= 0) || (msg.indexOf('notFound') >= 0);
}

// ───────────────────────────────────────────────────────────────
// C3: детерминированный load-wrapper (idempotent по jobId)
// ───────────────────────────────────────────────────────────────

/**
 * Грузит rows в tableId одним load-job с ДЕТЕРМИНИРОВАННЫМ jobId. При сетевом
 * повторе (тот же jobId уже существует) НЕ вставляет заново, а находит job и
 * ждёт DONE — так batch засчитывается ровно один раз (защита от дублей RAW при
 * timeout между «BQ принял» и «Apps Script получил ответ»). Внутрипрогонный
 * retry на insert использует ТОТ ЖЕ jobId.
 */
function wbStocksBqLoadDeterministic_(tableId, rows, jobId) {
  wbStocksBqAssertTable_(tableId);
  if (!rows || !rows.length) return 0;
  var c = getBqConfig_();

  var lines = new Array(rows.length);
  for (var i = 0; i < rows.length; i++) lines[i] = JSON.stringify(rows[i]);
  var blob = Utilities.newBlob(lines.join('\n'), 'application/octet-stream');

  var job = {
    jobReference: { projectId: c.projectId, location: c.location, jobId: jobId },
    configuration: {
      load: {
        destinationTable: { projectId: c.projectId, datasetId: c.datasetId, tableId: tableId },
        sourceFormat: 'NEWLINE_DELIMITED_JSON',
        writeDisposition: 'WRITE_APPEND',
        ignoreUnknownValues: false,
        maxBadRecords: 0
      }
    }
  };

  var jobLocation = c.location;
  var submitted = false, attempt = 0;
  while (!submitted) {
    attempt++;
    try {
      var ins = BigQuery.Jobs.insert(job, c.projectId, blob);
      jobLocation = (ins.jobReference && ins.jobReference.location) || c.location;
      submitted = true;
    } catch (e) {
      var msg = String((e && e.message) || e);
      if (msg.indexOf('Already Exists') >= 0 || msg.indexOf('duplicate') >= 0 || msg.indexOf('already exists') >= 0) {
        console.log('ℹ️ load job уже существует (' + jobId + ') — повторный append НЕ делаем, ждём его завершения.');
        submitted = true;   // job уже отправлен ранее → просто дожидаемся
      } else if (attempt >= 3) {
        throw e;
      } else {
        Utilities.sleep(2000);
      }
    }
  }

  var state = '', tries = 0;
  do {
    Utilities.sleep(1500);
    var j = BigQuery.Jobs.get(c.projectId, jobId, { location: jobLocation });
    state = j.status.state;
    if (j.status.errorResult) throw new Error('BQ load error (' + jobId + '): ' + JSON.stringify(j.status.errorResult));
    tries++;
  } while (state !== 'DONE' && tries < 120);
  if (state !== 'DONE') throw new Error('BQ load job не завершился: ' + jobId);
  return rows.length;
}

/** stocks-локальный DML → numDmlAffectedRows (общий bqQuery_ его не отдаёт). */
function wbStocksBqDml_(sql) {
  var c = getBqConfig_();
  var res = BigQuery.Jobs.query({ query: sql, useLegacySql: false, location: c.location, timeoutMs: 30000 }, c.projectId);
  return Number((res && res.numDmlAffectedRows) || 0);
}

/** SQL-литерал строки (экранирование, включая переводы строк — иначе UPDATE с
 *  многострочным error_message даст синтаксическую ошибку и manifest не финализируется). */
function wbStocksSqlStr_(v) {
  var s = String(v == null ? '' : v)
    .replace(/\\/g, '\\\\').replace(/'/g, "\\'")
    .replace(/\n/g, '\\n').replace(/\r/g, '\\r').replace(/\t/g, '\\t');
  return "'" + s + "'";
}
/** SQL-целое или NULL. */
function wbStocksSqlInt_(v) {
  if (v === null || v === undefined || v === '' || isNaN(Number(v))) return 'NULL';
  return String(Math.round(Number(v)));
}

// ───────────────────────────────────────────────────────────────
// RAW append (детерминированные batch jobId)
// ───────────────────────────────────────────────────────────────

/**
 * Пишет RAW-строки снимка. Каждый batch — свой детерминированный jobId
 * STOCK_<snapshot_id>_BATCH_<n>. Значения уже типизированы вызывающим
 * (числа — числами, is_aggregate_warehouse — boolean, TIMESTAMP — ISO-строка).
 */
function wbStocksBqAppendRaw_(rowObjs, snapshotId) {
  wbStocksBqAssertTable_(WB_STOCKS_RAW_TABLE_);
  if (!rowObjs || !rowObjs.length) return 0;
  var total = 0, batchNo = 0;
  for (var j = 0; j < rowObjs.length; j += WB_STOCKS_BQ_BATCH_) {
    batchNo++;
    var slice = rowObjs.slice(j, j + WB_STOCKS_BQ_BATCH_);
    total += wbStocksBqLoadDeterministic_(WB_STOCKS_RAW_TABLE_, slice, 'STOCK_' + snapshotId + '_BATCH_' + batchNo);
  }
  return total;
}

// ───────────────────────────────────────────────────────────────
// Manifest STARTED / финализация (C2)
// ───────────────────────────────────────────────────────────────

/** Вставляет manifest-строку STARTED (детерминированный jobId). Минимальные поля. */
function wbStocksBqManifestStart_(snapshotId, startedAtIso, periodFrom, periodTo) {
  var row = {
    snapshot_id: snapshotId, started_at: startedAtIso, status: 'STARTED',
    period_from: periodFrom, period_to: periodTo
  };
  wbStocksBqLoadDeterministic_(WB_STOCKS_MANIFEST_TABLE_, [row], 'STOCK_' + snapshotId + '_MANIFEST_START');
}

/**
 * Финализирует manifest: UPDATE STARTED → COMPLETE/ERROR c метриками.
 * Строго WHERE status='STARTED'; требует numDmlAffectedRows==1, иначе бросает
 * (переход не подтверждён). completed_at = CURRENT_TIMESTAMP().
 * @param {Object} m — метрики (любое поле может отсутствовать → NULL).
 */
function wbStocksBqManifestFinalize_(snapshotId, status, m, errorMessage) {
  var c = getBqConfig_();
  m = m || {};
  var sets = [
    'status=' + wbStocksSqlStr_(status),
    'completed_at=CURRENT_TIMESTAMP()',
    'expected_rows=' + wbStocksSqlInt_(m.expected_rows),
    'written_rows=' + wbStocksSqlInt_(m.written_rows),
    'distinct_keys=' + wbStocksSqlInt_(m.distinct_keys),
    'duplicate_keys=' + wbStocksSqlInt_(m.duplicate_keys),
    'unique_nm_ids=' + wbStocksSqlInt_(m.unique_nm_ids),
    'warehouses_count=' + wbStocksSqlInt_(m.warehouses_count),
    'qty_positive_rows=' + wbStocksSqlInt_(m.qty_positive_rows),
    'qty_zero_rows=' + wbStocksSqlInt_(m.qty_zero_rows),
    'aggregate_warehouse_rows=' + wbStocksSqlInt_(m.aggregate_warehouse_rows),
    'sum_quantity_all_t6=' + wbStocksSqlInt_(m.sum_quantity_all_t6),
    'sum_quantity_physical_t6=' + wbStocksSqlInt_(m.sum_quantity_physical_t6),
    't5_control_sum=' + wbStocksSqlInt_(m.t5_control_sum),
    'control_status=' + (m.control_status ? wbStocksSqlStr_(m.control_status) : 'NULL'),
    'control_delta=' + wbStocksSqlInt_(m.control_delta),
    'unmatched_nm_ids=' + (m.unmatched_nm_ids ? wbStocksSqlStr_(m.unmatched_nm_ids) : 'NULL'),
    'error_message=' + (errorMessage ? wbStocksSqlStr_(errorMessage) : 'NULL')
  ];
  var sql = 'UPDATE `' + c.projectId + '.' + c.datasetId + '.' + WB_STOCKS_MANIFEST_TABLE_ + '` SET ' +
    sets.join(', ') + ' WHERE snapshot_id=' + wbStocksSqlStr_(snapshotId) + " AND status='STARTED'";
  var affected = wbStocksBqDml_(sql);
  if (affected !== 1) {
    throw new Error('Manifest finalize(' + status + '): numDmlAffectedRows=' + affected + ' (ожидалось 1) для ' + snapshotId);
  }
  return affected;
}

// ───────────────────────────────────────────────────────────────
// Пост-проверка снимка + VIEW
// ───────────────────────────────────────────────────────────────

/**
 * Фактические счётчики RAW по snapshot_id (после append): всего строк,
 * distinct естественного ключа nm_id|chrt_id|warehouse_id, строк с пустым
 * snapshot_id. Фильтр по партиции _snapshot_date для pruning.
 */
function wbStocksBqSnapshotCounts_(snapshotId, snapshotDate) {
  var c = getBqConfig_();
  var sql = 'SELECT COUNT(*) AS c, ' +
    'COUNT(DISTINCT CONCAT(CAST(nm_id AS STRING),"|",CAST(chrt_id AS STRING),"|",CAST(warehouse_id AS STRING))) AS d, ' +
    "COUNTIF(snapshot_id IS NULL OR snapshot_id='') AS nullkey " +
    'FROM `' + c.projectId + '.' + c.datasetId + '.' + WB_STOCKS_RAW_TABLE_ + '` ' +
    'WHERE snapshot_id=' + wbStocksSqlStr_(snapshotId) +
    (snapshotDate ? ' AND _snapshot_date=' + wbStocksSqlStr_(snapshotDate) : '');
  var r = bqQuery_(sql);
  var f = (r && r.rows && r.rows[0] && r.rows[0].f) || [];
  return {
    count: Number(f[0] && f[0].v != null ? f[0].v : 0),
    distinct: Number(f[1] && f[1].v != null ? f[1].v : 0),
    nullKey: Number(f[2] && f[2].v != null ? f[2].v : 0)
  };
}

/** V_WB_STOCKS_CURRENT: строки RAW только последнего COMPLETE-снимка. */
function wbStocksBqCreateView() {
  wbStocksBqEnsureRaw_();
  wbStocksBqEnsureManifest_();
  var c = getBqConfig_();
  function fq(t) { return '`' + c.projectId + '.' + c.datasetId + '.' + t + '`'; }
  var sql =
    'CREATE OR REPLACE VIEW ' + fq(WB_STOCKS_VIEW_) + ' AS\n' +
    'WITH last_complete AS (\n' +
    '  SELECT snapshot_id FROM ' + fq(WB_STOCKS_MANIFEST_TABLE_) + '\n' +
    "  WHERE status = 'COMPLETE'\n" +
    '  ORDER BY completed_at DESC, snapshot_id DESC\n' +
    '  LIMIT 1\n' +
    ')\n' +
    'SELECT r.* FROM ' + fq(WB_STOCKS_RAW_TABLE_) + ' r\n' +
    'JOIN last_complete lc USING (snapshot_id)';
  bqQuery_(sql);
  console.log('✅ Вью создана: ' + WB_STOCKS_VIEW_ + ' (последний COMPLETE снимок)');
}

/** C0 smoke без WB API: sink + таблицы + вью. Fail-closed rollback флага. */
function wbStocksBqInitC0() {
  try {
    wbStocksBqEnable();
    wbStocksBqEnsureRaw_();
    wbStocksBqEnsureManifest_();
    wbStocksBqCreateView();
    console.log('✅ C0 остатков готов (RAW + manifest + VIEW). Дальше — runWbStocksSnapshot за один снимок.');
  } catch (e) {
    wbStocksBqDisable();
    console.error('❌ C0 остатков не завершён. Sink ВЫКЛючен: ' + String((e && e.message) || e));
    throw e;
  }
}

/** Диагностика: sink, последний COMPLETE снимок, число строк во вью. */
function wbStocksBqStatus() {
  var sinkOn = wbStocksBqSinkOn_();
  var c = getBqConfig_();
  var last = '(нет)', viewCount = '';
  try {
    var r = bqQuery_('SELECT snapshot_id, completed_at FROM `' + c.projectId + '.' + c.datasetId + '.' + WB_STOCKS_MANIFEST_TABLE_ +
      "` WHERE status='COMPLETE' ORDER BY completed_at DESC, snapshot_id DESC LIMIT 1");
    if (r && r.rows && r.rows.length) last = r.rows[0].f[0].v + ' @ ' + r.rows[0].f[1].v;
  } catch (e) { last = '(ошибка: ' + ((e && e.message) || e) + ')'; }
  try {
    var v = bqQuery_('SELECT COUNT(*) FROM `' + c.projectId + '.' + c.datasetId + '.' + WB_STOCKS_VIEW_ + '`');
    viewCount = (v && v.rows && v.rows.length) ? String(v.rows[0].f[0].v) : '0';
  } catch (e2) { viewCount = '(вью ещё нет)'; }
  console.log('STOCKS sink: ' + (sinkOn ? 'ВКЛ' : 'ВЫКЛ') + ' | последний COMPLETE: ' + last + ' | V_WB_STOCKS_CURRENT: ' + viewCount);
  return { sink_on: sinkOn, last_complete: last, view_count: viewCount };
}
