/**
 * ══════════════════════════════════════════════════════════════
 * EVETIS WB — WbStocksBigQuery.gs   (Фаза E — остатки, BQ-слой)
 *
 * Приёмник остатков в BigQuery: RAW_WB_STOCKS (append-only снапшоты) +
 * RAW_WB_STOCKS_T5 (детализация по складам из T5) + manifest
 * WB_STOCKS_SNAPSHOTS (источник истины статуса снимка) + две вью по
 * последнему COMPLETE-снимку: V_WB_STOCKS_CURRENT и V_WB_STOCKS_T5_CURRENT.
 *
 * Источники: T6 `stocks-report/wb-warehouses` (grain snapshot × nmId × chrtId ×
 * warehouseId) и T5 `warehouse_remains` (grain snapshot × nmId × barcode ×
 * techSize × warehouseName). 🔴 С 16.08.2026 T6 обезличен и отдаёт только
 * агрегат «Склад WB»; поимённые склады остались ТОЛЬКО в T5, поэтому T5 из
 * контрольного источника стал ещё и источником складской детализации.
 * Тяга/оркестрация — в WbStocksSnapshot.gs. Здесь только BQ-механика.
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
var WB_STOCKS_T5_TABLE_       = 'RAW_WB_STOCKS_T5';
var WB_STOCKS_T5_VIEW_        = 'V_WB_STOCKS_T5_CURRENT';
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
    { name: 'warehouse_id', type: 'INT64' },        // NULL с 16.08.2026, если WB отдал нечисловой id
    { name: 'warehouse_code', type: 'STRING' },     // сырой warehouseId строкой — ключ грейна после обезличивания
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

/**
 * Поля RAW_WB_STOCKS_T5 — детализация из T5 `warehouse_remains`.
 * С 16.08.2026 это ЕДИНСТВЕННОЕ место, где виден товар на поимённых складах:
 * T6 схлопнул всё в «Склад WB». Грейн: snapshot × nmId × barcode × techSize × warehouseName.
 */
function wbStocksT5RawFields_() {
  return [
    { name: 'load_id', type: 'STRING' },
    { name: 'snapshot_id', type: 'STRING' },
    { name: 'snapshot_ts', type: 'TIMESTAMP' },
    { name: 'source_api', type: 'STRING' },
    { name: 'nm_id', type: 'INT64' },
    { name: 'barcode', type: 'STRING' },
    { name: 'tech_size', type: 'STRING' },
    { name: 'vendor_code', type: 'STRING' },
    { name: 'volume', type: 'FLOAT64' },
    { name: 'warehouse_name', type: 'STRING' },
    { name: 'row_type', type: 'STRING' },     // WAREHOUSE | AGGREGATE | PSEUDO_TOTAL | PSEUDO_TO_CLIENT | PSEUDO_FROM_CLIENT
    { name: 'quantity', type: 'INT64' },
    { name: 'internal_sku', type: 'STRING' },
    { name: 'sku_match_status', type: 'STRING' }
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
    { name: 'anonymized_warehouse_rows', type: 'INT64' },   // строк со складом -999999 «Склад WB» (обезличивание WB, с 16.08.2026)
    { name: 'sum_quantity_all_t6', type: 'INT64' },
    { name: 'sum_quantity_physical_t6', type: 'INT64' },
    { name: 't5_control_sum', type: 'INT64' },        // все реальные склады T5
    { name: 't5_wb_rf_sum', type: 'INT64' },          // только агрегат «Склад WB РФ»
    { name: 't5_named_sum', type: 'INT64' },          // поимённые склады — их T6 больше не отдаёт
    { name: 't5_rows_written', type: 'INT64' },
    { name: 'control_status', type: 'STRING' },       // OK / MISMATCH / T5_UNAVAILABLE — ИСТОРИЧЕСКАЯ метрика, семантику не менять
    { name: 'control_delta', type: 'INT64' },
    // Детектор доступности (с 16.08.2026). Отдельные поля, а не переопределение
    // control_*: иначе исторические значения станут несопоставимы с будущими.
    { name: 't6_comparable', type: 'INT64' },           // T6 physical quantity — величина, сопоставляемая с агрегатом T5; потоки in_way_* в инвариант НЕ входят
    { name: 'availability_gap', type: 'INT64' },        // t5_wb_rf_sum − t6_comparable
    { name: 'availability_gap_prev', type: 'INT64' },   // gap предыдущего COMPLETE-снимка
    { name: 'availability_gap_delta', type: 'INT64' },  // прирост разрыва — сигнал выпадения склада
    { name: 'availability_ratio', type: 'FLOAT64' },    // доля продаваемого от балансового агрегата
    { name: 'availability_status', type: 'STRING' },    // OK / DEGRADED / WAREHOUSE_DROP / DATA_ERROR / NO_BASELINE / T5_UNAVAILABLE
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

/** allowlist: приёмник остатков пишет ТОЛЬКО в RAW_WB_STOCKS / RAW_WB_STOCKS_T5 / WB_STOCKS_SNAPSHOTS. */
function wbStocksBqAssertTable_(tableId) {
  if (tableId !== WB_STOCKS_RAW_TABLE_ && tableId !== WB_STOCKS_MANIFEST_TABLE_ &&
      tableId !== WB_STOCKS_T5_TABLE_) {
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
    wbStocksBqEnsureColumns_(WB_STOCKS_RAW_TABLE_, wbStocksRawFields_());
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

/** RAW_WB_STOCKS_T5: партиция _snapshot_date, кластер nm_id/warehouse_name. Create-if-missing. */
function wbStocksBqEnsureT5_() {
  var c = getBqConfig_();
  bqEnsureDataset_();
  try {
    BigQuery.Tables.get(c.projectId, c.datasetId, WB_STOCKS_T5_TABLE_);
    wbStocksBqEnsureColumns_(WB_STOCKS_T5_TABLE_, wbStocksT5RawFields_());
    return false;
  } catch (e) {
    if (!wbStocksBqIsNotFound_(e)) throw new Error('Не удалось проверить ' + WB_STOCKS_T5_TABLE_ + ': ' + ((e && e.message) || e));
  }
  var fields = wbStocksT5RawFields_().slice();
  fields.push({ name: '_snapshot_date', type: 'DATE' });
  BigQuery.Tables.insert({
    tableReference: { projectId: c.projectId, datasetId: c.datasetId, tableId: WB_STOCKS_T5_TABLE_ },
    schema: { fields: fields },
    timePartitioning: { type: 'DAY', field: '_snapshot_date' },
    clustering: { fields: ['nm_id', 'warehouse_name'] }
  }, c.projectId, c.datasetId);
  console.log('✅ BQ таблица создана: ' + WB_STOCKS_T5_TABLE_ + ' (детализация T5 по складам)');
  return true;
}

/** Append детализации T5 батчами с детерминированным jobId (C3). */
function wbStocksBqAppendT5_(rowObjs, snapshotId) {
  wbStocksBqAssertTable_(WB_STOCKS_T5_TABLE_);
  if (!rowObjs || !rowObjs.length) return 0;
  var total = 0, batchNo = 0;
  for (var j = 0; j < rowObjs.length; j += WB_STOCKS_BQ_BATCH_) {
    batchNo++;
    var slice = rowObjs.slice(j, j + WB_STOCKS_BQ_BATCH_);
    total += wbStocksBqLoadDeterministic_(WB_STOCKS_T5_TABLE_, slice, 'STOCKT5_' + snapshotId + '_BATCH_' + batchNo);
  }
  return total;
}

/** WB_STOCKS_SNAPSHOTS (manifest). Create-if-missing (небольшая таблица, без партиции). */
function wbStocksBqEnsureManifest_() {
  var c = getBqConfig_();
  bqEnsureDataset_();
  try {
    BigQuery.Tables.get(c.projectId, c.datasetId, WB_STOCKS_MANIFEST_TABLE_);
    wbStocksBqEnsureColumns_(WB_STOCKS_MANIFEST_TABLE_, wbStocksManifestFields_());
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

/**
 * Добивает недостающие NULLABLE-колонки в существующую таблицу (schema evolution).
 * Только ДОБАВЛЕНИЕ: существующие поля не трогаем, типы не меняем, ничего не удаляем —
 * поэтому операция безопасна и идемпотентна. Нужна, чтобы `warehouse_code` в RAW
 * и `anonymized_warehouse_rows`, `t6_comparable`, `availability_*` в manifest
 * появились в уже созданных таблицах без пересоздания.
 */
function wbStocksBqEnsureColumns_(tableId, wantFields) {
  wbStocksBqAssertTable_(tableId);
  var c = getBqConfig_();
  var tbl = BigQuery.Tables.get(c.projectId, c.datasetId, tableId);
  var have = {};
  var cur = (tbl.schema && tbl.schema.fields) || [];
  for (var i = 0; i < cur.length; i++) have[cur[i].name] = true;

  var added = [];
  for (var j = 0; j < wantFields.length; j++) {
    if (!have[wantFields[j].name]) {
      cur.push({ name: wantFields[j].name, type: wantFields[j].type, mode: 'NULLABLE' });
      added.push(wantFields[j].name);
    }
  }
  if (!added.length) return [];

  BigQuery.Tables.patch({ schema: { fields: cur } }, c.projectId, c.datasetId, tableId);
  console.log('✅ ' + tableId + ': добавлены колонки — ' + added.join(', '));
  return added;
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
        console.log('ℹ️ load job уже существует (' + jobId + ') — повторный append НЕ делаем, ждём завершения.');
        submitted = true;   // job уже принят ранее → дожидаемся
        break;
      }
      // Timeout/сетевая неопределённость после insert: BQ мог УЖЕ принять job.
      // Перед retry/throw проверяем существование по детерминированному jobId.
      var chk = wbStocksBqJobExists_(jobId, jobLocation);
      if (chk.exists) {
        console.log('ℹ️ load job принят несмотря на ошибку ответа (' + jobId + ') — повторно НЕ вставляем.');
        submitted = true; break;                 // job есть → не дублируем append
      }
      if (chk.unknown) throw e;                    // не смогли проверить → fail closed
      if (attempt >= 3) throw e;                    // точно not found и попытки исчерпаны
      Utilities.sleep(2000);                        // точно not found → можно повторить insert
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

/**
 * Существует ли BigQuery job с данным jobId. { exists, unknown }:
 *   exists=true  — job найден (был принят);
 *   not found    — exists=false, unknown=false (можно вставлять);
 *   иная ошибка  — exists=false, unknown=true (проверить не смогли → fail closed).
 */
function wbStocksBqJobExists_(jobId, jobLocation) {
  var c = getBqConfig_();
  try {
    BigQuery.Jobs.get(c.projectId, jobId, { location: jobLocation || c.location });
    return { exists: true, unknown: false };
  } catch (e) {
    if (wbStocksBqIsNotFound_(e)) return { exists: false, unknown: false };
    return { exists: false, unknown: true };
  }
}

/**
 * stocks-локальный DML → numDmlAffectedRows. Jobs.query может вернуть
 * jobComplete=false (query job ещё идёт) — тогда numDmlAffectedRows читать рано.
 * Дожидаемся завершения через getQueryResults по jobReference.jobId, проверяем
 * errorResult (Jobs.get) и только после этого возвращаем numDmlAffectedRows.
 */
function wbStocksBqDml_(sql) {
  var c = getBqConfig_();
  var res = BigQuery.Jobs.query({ query: sql, useLegacySql: false, location: c.location, timeoutMs: 30000 }, c.projectId);
  var jobId = res.jobReference && res.jobReference.jobId;
  var jobLocation = (res.jobReference && res.jobReference.location) || c.location;

  var complete = (res.jobComplete === true);
  var tries = 0;
  while (!complete && jobId && tries < 120) {
    Utilities.sleep(1000);
    res = BigQuery.Jobs.getQueryResults(c.projectId, jobId, { location: jobLocation, timeoutMs: 30000 });
    complete = (res.jobComplete === true);
    tries++;
  }
  if (!complete) throw new Error('BQ DML: query job не завершился' + (jobId ? ' (' + jobId + ')' : ''));

  // Проверка ошибки самого job до чтения результата.
  if (jobId) {
    var j = BigQuery.Jobs.get(c.projectId, jobId, { location: jobLocation });
    if (j.status && j.status.errorResult) {
      throw new Error('BQ DML error (' + jobId + '): ' + JSON.stringify(j.status.errorResult));
    }
  }
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
/** SQL-число с плавающей точкой или NULL (для availability_ratio). */
function wbStocksSqlNum_(v) {
  if (v === null || v === undefined || v === '' || isNaN(Number(v)) || !isFinite(Number(v))) return 'NULL';
  return String(Number(v));
}

/**
 * `availability_gap` предыдущего COMPLETE-снимка — база для `availability_gap_delta`.
 * Берём последний COMPLETE с НЕпустым gap: снимки до внедрения детектора его не писали,
 * и брать NULL за базу нельзя — иначе первый же выпавший склад не отличится от старта.
 * Текущий снимок в выборку не попадает: он ещё STARTED.
 * @return {number|null}
 */
function wbStocksBqPrevAvailabilityGap_() {
  var c = getBqConfig_();
  var sql = 'SELECT availability_gap FROM `' + c.projectId + '.' + c.datasetId + '.' +
    WB_STOCKS_MANIFEST_TABLE_ + "` WHERE status='COMPLETE' AND availability_gap IS NOT NULL " +
    'ORDER BY completed_at DESC, snapshot_id DESC LIMIT 1';
  var r = bqQuery_(sql);
  var f = (r && r.rows && r.rows[0] && r.rows[0].f) || [];
  if (!f.length || f[0].v == null) return null;
  return Number(f[0].v);
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
    'anonymized_warehouse_rows=' + wbStocksSqlInt_(m.anonymized_warehouse_rows),
    'sum_quantity_all_t6=' + wbStocksSqlInt_(m.sum_quantity_all_t6),
    'sum_quantity_physical_t6=' + wbStocksSqlInt_(m.sum_quantity_physical_t6),
    't5_control_sum=' + wbStocksSqlInt_(m.t5_control_sum),
    't5_wb_rf_sum=' + wbStocksSqlInt_(m.t5_wb_rf_sum),
    't5_named_sum=' + wbStocksSqlInt_(m.t5_named_sum),
    't5_rows_written=' + wbStocksSqlInt_(m.t5_rows_written),
    'control_status=' + (m.control_status ? wbStocksSqlStr_(m.control_status) : 'NULL'),
    'control_delta=' + wbStocksSqlInt_(m.control_delta),
    't6_comparable=' + wbStocksSqlInt_(m.t6_comparable),
    'availability_gap=' + wbStocksSqlInt_(m.availability_gap),
    'availability_gap_prev=' + wbStocksSqlInt_(m.availability_gap_prev),
    'availability_gap_delta=' + wbStocksSqlInt_(m.availability_gap_delta),
    'availability_ratio=' + wbStocksSqlNum_(m.availability_ratio),
    'availability_status=' + (m.availability_status ? wbStocksSqlStr_(m.availability_status) : 'NULL'),
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
 * Фактические счётчики RAW по snapshot_id (после append): всего строк и distinct
 * естественного ключа nm_id|chrt_id|warehouse_id. Фильтр по партиции _snapshot_date
 * для pruning. (Проверку «пустой snapshot_id» не делаем — запрос и так фильтрует
 * по snapshot_id=<id>, так что в выборке он не может быть пустым; строки всегда
 * пишутся с snapshot_id.)
 */
function wbStocksBqSnapshotCounts_(snapshotId, snapshotDate) {
  var c = getBqConfig_();
  var sql = 'SELECT COUNT(*) AS c, ' +
    // 🔴 Ключ грейна берём тот же, что и валидация: код склада, иначе имя.
    // Раньше здесь был CAST(warehouse_id AS STRING) — при NULL весь CONCAT
    // становится NULL, COUNT(DISTINCT) молча теряет строки и гейт
    // «distinct == expected» валит корректный снимок.
    'COUNT(DISTINCT CONCAT(CAST(nm_id AS STRING),"|",CAST(chrt_id AS STRING),"|",' +
    'COALESCE(NULLIF(warehouse_code,""),CAST(warehouse_id AS STRING),warehouse_name,""))) AS d ' +
    'FROM `' + c.projectId + '.' + c.datasetId + '.' + WB_STOCKS_RAW_TABLE_ + '` ' +
    'WHERE snapshot_id=' + wbStocksSqlStr_(snapshotId) +
    (snapshotDate ? ' AND _snapshot_date=' + wbStocksSqlStr_(snapshotDate) : '');
  var r = bqQuery_(sql);
  var f = (r && r.rows && r.rows[0] && r.rows[0].f) || [];
  return {
    count: Number(f[0] && f[0].v != null ? f[0].v : 0),
    distinct: Number(f[1] && f[1].v != null ? f[1].v : 0)
  };
}

/** V_WB_STOCKS_CURRENT: строки RAW только последнего COMPLETE-снимка. */
function wbStocksBqCreateView() {
  wbStocksBqEnsureRaw_();
  wbStocksBqEnsureManifest_();
  // 🔴 Обязательно ДО создания V_WB_STOCKS_T5_CURRENT: вью ссылается на
  // RAW_WB_STOCKS_T5, а её создаёт только wbStocksBqEnsureT5_(). Без этой
  // строки на чистом внедрении CREATE VIEW падает — таблицы ещё нет
  // (в снимке она появлялась позже, в best-effort ветке T5).
  wbStocksBqEnsureT5_();
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

  var sqlT5 =
    'CREATE OR REPLACE VIEW ' + fq(WB_STOCKS_T5_VIEW_) + ' AS\n' +
    'WITH last_complete AS (\n' +
    '  SELECT snapshot_id FROM ' + fq(WB_STOCKS_MANIFEST_TABLE_) + '\n' +
    "  WHERE status = 'COMPLETE'\n" +
    '  ORDER BY completed_at DESC, snapshot_id DESC\n' +
    '  LIMIT 1\n' +
    ')\n' +
    'SELECT r.* FROM ' + fq(WB_STOCKS_T5_TABLE_) + ' r\n' +
    'JOIN last_complete lc USING (snapshot_id)';
  bqQuery_(sqlT5);
  console.log('✅ Вью создана: ' + WB_STOCKS_T5_VIEW_ + ' (детализация T5 последнего COMPLETE снимка)');
}

/** C0 smoke без WB API: sink + таблицы + вью. Fail-closed rollback флага. */
function wbStocksBqInitC0() {
  try {
    wbStocksBqEnable();
    wbStocksBqEnsureRaw_();
    wbStocksBqEnsureManifest_();
    wbStocksBqCreateView();
    console.log('✅ C0 остатков готов (RAW + RAW_T5 + manifest + обе VIEW). Дальше — runWbStocksSnapshot за один снимок.');
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
