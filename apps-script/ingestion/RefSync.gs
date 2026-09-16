/**
 * ══════════════════════════════════════════════════════════════
 * EVETIS WB — RefSync.gs   (Этап 2 «REF Sync», PR1)
 *
 * Версионная публикация справочников Sheets → BigQuery.
 * PR1: инфраструктура (versioned publication) + REF_SKU_MASTER.
 *
 * Реализует docs/ARCHITECTURE_EVETIS_ANALYTICS_v2.md §4/§6 и
 * REF_SYNC_DESIGN_2026-07-20_v2.md. Существующие файлы НЕ меняет —
 * только добавляет объекты в BigQuery и этот файл.
 *
 * Модель all-or-nothing:
 *   *_DATA (строки + ref_run_id, append-only по версиям) +
 *   REF_ACTIVE_VERSION (единый указатель активной версии) +
 *   потребительские VIEW `REF_*` = только активная версия.
 * Каждый прогон rebuild'ит весь набор REF под одним ref_run_id и
 * переключает указатель ОДНОЙ DML. Провал валидации → указатель не
 * меняется, потребители видят прежнюю валидную версию (fail-closed).
 *
 * Переиспользует WbBigQuery.gs: getBqConfig_, bqEnsureDataset_,
 * bqLoadRows_ (WRITE_APPEND — подходит для версий). Триггер ставит
 * владелец ПОСЛЕ ручной приёмки (refInstallDailyTrigger).
 * ══════════════════════════════════════════════════════════════
 */

var REF_MARKETPLACE_    = 'WB';
var REF_KEEP_VERSIONS_  = 10;                 // сколько версий держим (retention)
var REF_TRIGGER_FN_     = 'runRefSync';
var REF_SHEET_SKU_      = 'SKU_MASTER';

// Объекты BigQuery
var REF_T_SKU_DATA_ = 'REF_SKU_MASTER_DATA';
var REF_V_SKU_      = 'REF_SKU_MASTER';
var REF_T_ACTIVE_   = 'REF_ACTIVE_VERSION';
var REF_T_RUNS_     = 'REF_SYNC_RUNS';
var REF_T_TLOG_     = 'REF_SYNC_TABLE_LOG';
var REF_ACTIVE_ID_  = 'singleton';

// ───────────────────────────────────────────────────────────────
// Утилиты нормализации
// ───────────────────────────────────────────────────────────────

function refStr_(v) { return (v === null || v === undefined) ? '' : String(v).trim(); }

function refInt_(v) {
  if (v === null || v === undefined || v === '') return null;
  var n = Number(String(v).replace(/\s+/g, ''));
  if (!isFinite(n) || Math.floor(n) !== n) return null;
  return n;
}

/** TRUE/ДА/1/YES → true; иначе false. Понимает и нативный boolean. */
function refBool_(v) {
  if (v === true) return true;
  if (v === false || v === null || v === undefined) return false;
  var s = String(v).trim().toUpperCase();
  return s === 'TRUE' || s === 'ДА' || s === '1' || s === 'YES' || s === 'Y';
}

function refShortUuid_() { return Utilities.getUuid().replace(/-/g, '').substring(0, 8); }

function refFqn_(tableId) {
  var c = getBqConfig_();
  return '`' + c.projectId + '.' + c.datasetId + '.' + tableId + '`';
}

// ───────────────────────────────────────────────────────────────
// BigQuery: query / DML / DDL
// ───────────────────────────────────────────────────────────────

/** Выполняет SQL (SELECT/DML/DDL), ждёт завершения, кидает при ошибке. Возвращает ответ. */
function refSql_(sql) {
  var c = getBqConfig_();
  var res = BigQuery.Jobs.query(
    { query: sql, useLegacySql: false, location: c.location, timeoutMs: 55000 }, c.projectId);
  var jobId = res.jobReference ? res.jobReference.jobId : null;
  var tries = 0;
  while (!res.jobComplete && jobId && tries < 90) {
    Utilities.sleep(1000);
    res = BigQuery.Jobs.getQueryResults(c.projectId, jobId, { location: c.location });
    tries++;
  }
  if (!res.jobComplete) throw new Error('BQ: запрос не завершился за отведённое время');
  if (res.errors && res.errors.length) throw new Error('BQ SQL error: ' + JSON.stringify(res.errors));
  return res;
}

/** Первая строка результата как массив строковых значений (или []). */
function refRow_(sql) {
  var res = refSql_(sql);
  if (!res.rows || !res.rows.length) return [];
  return res.rows[0].f.map(function (x) { return x.v; });
}

// ───────────────────────────────────────────────────────────────
// Схемы и создание объектов
// ───────────────────────────────────────────────────────────────

function refSkuMasterSchema_() {
  var S = function (n) { return { name: n, type: 'STRING', mode: 'NULLABLE' }; };
  var B = function (n) { return { name: n, type: 'BOOL',   mode: 'NULLABLE' }; };
  return { fields: [
    { name: 'ref_run_id', type: 'STRING', mode: 'REQUIRED' },
    S('marketplace'),
    { name: 'nm_id', type: 'INT64', mode: 'NULLABLE' },
    S('internal_sku'), S('product_name_short'), S('product_name_full'),
    S('category'), S('line'), S('product_type'),
    B('is_bundle'), S('status'), B('active'),
    S('wb_vendor_code'), S('barcode'), S('wb_subject_id'), S('wb_subject_name'),
    S('brand'), S('volume_ml'),
    B('include_in_pnl'), B('include_in_ads_analysis'),
    B('include_in_supply_plan'), B('include_in_stock_alerts'),
    S('data_quality_status'),
    { name: '_synced_at', type: 'TIMESTAMP', mode: 'NULLABLE' }
  ] };
}

function refEnsureTable_(tableId, schema, clusterFields) {
  var c = getBqConfig_();
  try { BigQuery.Tables.get(c.projectId, c.datasetId, tableId); return false; }
  catch (e) {
    var body = { tableReference: { projectId: c.projectId, datasetId: c.datasetId, tableId: tableId }, schema: schema };
    if (clusterFields) body.clustering = { fields: clusterFields };
    BigQuery.Tables.insert(body, c.projectId, c.datasetId);
    console.log('✅ Таблица создана: ' + tableId);
    return true;
  }
}

/** Создаёт все объекты REF (идемпотентно) + пересоздаёт VIEW. */
function refEnsureAll_() {
  bqEnsureDataset_();

  refEnsureTable_(REF_T_SKU_DATA_, refSkuMasterSchema_(), ['ref_run_id']);

  refEnsureTable_(REF_T_ACTIVE_, { fields: [
    { name: 'id', type: 'STRING', mode: 'REQUIRED' },
    { name: 'active_ref_run_id', type: 'STRING', mode: 'NULLABLE' },
    { name: 'activated_at', type: 'TIMESTAMP', mode: 'NULLABLE' }
  ] });

  refEnsureTable_(REF_T_RUNS_, { fields: [
    { name: 'run_id', type: 'STRING' }, { name: 'status', type: 'STRING' },
    { name: 'started_at', type: 'TIMESTAMP' }, { name: 'finished_at', type: 'TIMESTAMP' },
    { name: 'active_version_before', type: 'STRING' }, { name: 'active_version_after', type: 'STRING' },
    { name: 'error_message', type: 'STRING' },
    { name: 'warning_count', type: 'INT64' }, { name: 'warning_fingerprint', type: 'STRING' }
  ] });

  refEnsureTable_(REF_T_TLOG_, { fields: [
    { name: 'run_id', type: 'STRING' }, { name: 'ref_name', type: 'STRING' },
    { name: 'source_rows', type: 'INT64' }, { name: 'normalized_rows', type: 'INT64' },
    { name: 'staged_rows', type: 'INT64' }, { name: 'published_rows', type: 'INT64' },
    { name: 'validation_error_count', type: 'INT64' }, { name: 'status', type: 'STRING' }
  ] });

  // Потребительский VIEW — только активная версия. Идемпотентно.
  refSql_(
    'CREATE OR REPLACE VIEW ' + refFqn_(REF_V_SKU_) + ' AS ' +
    'SELECT d.* EXCEPT(ref_run_id, _synced_at) FROM ' + refFqn_(REF_T_SKU_DATA_) + ' d ' +
    'JOIN ' + refFqn_(REF_T_ACTIVE_) + ' a ' +
    "ON d.ref_run_id = a.active_ref_run_id AND a.id = '" + REF_ACTIVE_ID_ + "'");
}

// ───────────────────────────────────────────────────────────────
// Чтение листа SKU_MASTER → строки REF
// ───────────────────────────────────────────────────────────────

/** Возвращает { rows, source }. Кидает при отсутствии обязательной колонки (защита от рассинхрона схемы). */
function refBuildSkuMaster_(ss, runId, syncedIso) {
  var sh = ss.getSheetByName(REF_SHEET_SKU_);
  if (!sh) throw new Error('Лист ' + REF_SHEET_SKU_ + ' не найден');
  var values = sh.getDataRange().getValues();
  if (values.length < 2) throw new Error(REF_SHEET_SKU_ + ': нет данных');

  var header = values[0], idx = {};
  for (var i = 0; i < header.length; i++) idx[refStr_(header[i])] = i;
  var required = ['active', 'internal_sku', 'product_name_short', 'product_name_full',
    'category', 'line', 'product_type', 'status', 'wb_nm_id', 'wb_vendor_code', 'barcode',
    'wb_subject_id', 'wb_subject_name', 'brand', 'volume_ml', 'is_bundle',
    'include_in_pnl', 'include_in_ads_analysis', 'include_in_supply_plan',
    'include_in_stock_alerts', 'data_quality_status'];
  for (var r0 = 0; r0 < required.length; r0++) {
    if (!(required[r0] in idx)) throw new Error('SKU_MASTER: нет обязательной колонки "' + required[r0] + '"');
  }
  var g = function (row, name) { return row[idx[name]]; };

  var rows = [], source = 0;
  for (var r = 1; r < values.length; r++) {
    var row = values[r];
    var sku = refStr_(g(row, 'internal_sku'));
    var nmRaw = refStr_(g(row, 'wb_nm_id'));
    if (!sku && !nmRaw) continue;  // пустая строка
    source++;
    rows.push({
      ref_run_id: runId,
      marketplace: REF_MARKETPLACE_,
      nm_id: refInt_(g(row, 'wb_nm_id')),
      internal_sku: sku,
      product_name_short: refStr_(g(row, 'product_name_short')),
      product_name_full: refStr_(g(row, 'product_name_full')),
      category: refStr_(g(row, 'category')),
      line: refStr_(g(row, 'line')),
      product_type: refStr_(g(row, 'product_type')),
      is_bundle: refBool_(g(row, 'is_bundle')),
      status: refStr_(g(row, 'status')),
      active: refBool_(g(row, 'active')),
      wb_vendor_code: refStr_(g(row, 'wb_vendor_code')),
      barcode: refStr_(g(row, 'barcode')),
      wb_subject_id: refStr_(g(row, 'wb_subject_id')),
      wb_subject_name: refStr_(g(row, 'wb_subject_name')),
      brand: refStr_(g(row, 'brand')),
      volume_ml: refStr_(g(row, 'volume_ml')),
      include_in_pnl: refBool_(g(row, 'include_in_pnl')),
      include_in_ads_analysis: refBool_(g(row, 'include_in_ads_analysis')),
      include_in_supply_plan: refBool_(g(row, 'include_in_supply_plan')),
      include_in_stock_alerts: refBool_(g(row, 'include_in_stock_alerts')),
      data_quality_status: refStr_(g(row, 'data_quality_status')),
      _synced_at: syncedIso
    });
  }
  return { rows: rows, source: source };
}

// ───────────────────────────────────────────────────────────────
// Hard-валидации REF_SKU_MASTER (по строкам ref_run_id)
// ───────────────────────────────────────────────────────────────

/** { ok, errorCount, errors[], metrics{n,n_nm,n_single,n_bundle} }. */
function refValidateSkuMaster_(runId, stagedExpected) {
  var t = refFqn_(REF_T_SKU_DATA_);
  var v = refRow_(
    'SELECT COUNT(*) n, COUNT(DISTINCT nm_id) n_nm, ' +
    'COUNTIF(nm_id IS NULL) null_nm, ' +
    "COUNTIF(internal_sku IS NULL OR TRIM(internal_sku)='') null_sku, " +
    "COUNTIF((is_bundle AND product_type!='bundle') OR (NOT is_bundle AND product_type!='single')) bundle_mismatch, " +
    "COUNTIF(product_type='single') n_single, COUNTIF(product_type='bundle') n_bundle " +
    'FROM ' + t + " WHERE ref_run_id='" + runId + "'");
  var n = +v[0], n_nm = +v[1], null_nm = +v[2], null_sku = +v[3], mism = +v[4],
      n_single = +v[5], n_bundle = +v[6];
  var errors = [];
  if (n === 0) errors.push('0 строк — лист пуст/не прочитан');
  if (n !== stagedExpected) errors.push('строк ' + n + ' != загруженным ' + stagedExpected);
  if (null_nm > 0) errors.push('пустой nm_id: ' + null_nm);
  if (n !== n_nm) errors.push('nm_id не уникален: строк ' + n + ', различных ' + n_nm);
  if (null_sku > 0) errors.push('пустой internal_sku: ' + null_sku);
  if (mism > 0) errors.push('is_bundle ⇎ product_type: ' + mism);
  return { ok: errors.length === 0, errorCount: errors.length, errors: errors,
    metrics: { n: n, n_nm: n_nm, n_single: n_single, n_bundle: n_bundle } };
}

// ───────────────────────────────────────────────────────────────
// Активация версии, retention, журналы
// ───────────────────────────────────────────────────────────────

function refActiveBefore_() {
  var r = refRow_('SELECT active_ref_run_id FROM ' + refFqn_(REF_T_ACTIVE_) +
    " WHERE id='" + REF_ACTIVE_ID_ + "'");
  return r.length ? r[0] : null;
}

/** Одна атомарная DML — переключить активную версию. */
function refActivate_(runId) {
  refSql_(
    'MERGE ' + refFqn_(REF_T_ACTIVE_) + ' T USING (SELECT ' +
    "'" + REF_ACTIVE_ID_ + "' AS id, '" + runId + "' AS active_ref_run_id, CURRENT_TIMESTAMP() AS activated_at) S " +
    'ON T.id = S.id ' +
    'WHEN MATCHED THEN UPDATE SET active_ref_run_id = S.active_ref_run_id, activated_at = S.activated_at ' +
    'WHEN NOT MATCHED THEN INSERT (id, active_ref_run_id, activated_at) VALUES (S.id, S.active_ref_run_id, S.activated_at)');
}

/** Удаляет старые версии сверх последних N; активную не трогает. */
function refPrune_() {
  var t = refFqn_(REF_T_SKU_DATA_);
  refSql_(
    'DELETE FROM ' + t + ' WHERE ref_run_id NOT IN (' +
    'SELECT ref_run_id FROM (SELECT ref_run_id, MAX(_synced_at) mx FROM ' + t +
    ' GROUP BY ref_run_id ORDER BY mx DESC LIMIT ' + REF_KEEP_VERSIONS_ + ')) ' +
    'AND ref_run_id != IFNULL((SELECT active_ref_run_id FROM ' + refFqn_(REF_T_ACTIVE_) +
    " WHERE id='" + REF_ACTIVE_ID_ + "'), '')");
}

function refRunsInsertStarted_(runId, iso, activeBefore) {
  bqLoadRows_(REF_T_RUNS_, [{
    run_id: runId, status: 'STARTED', started_at: iso, finished_at: null,
    active_version_before: activeBefore, active_version_after: null,
    error_message: '', warning_count: 0, warning_fingerprint: ''
  }]);
}

function refRunsFinalize_(runId, status, activeAfter, errMsg) {
  refSql_('UPDATE ' + refFqn_(REF_T_RUNS_) + ' SET status=' + refQ_(status) +
    ', finished_at=CURRENT_TIMESTAMP(), active_version_after=' + refQ_(activeAfter) +
    ', error_message=' + refQ_(errMsg || '') + " WHERE run_id='" + runId + "'");
}

function refTableLog_(runId, name, src, norm, staged, published, valErr, status) {
  bqLoadRows_(REF_T_TLOG_, [{
    run_id: runId, ref_name: name, source_rows: src, normalized_rows: norm,
    staged_rows: staged, published_rows: published, validation_error_count: valErr, status: status
  }]);
}

/** SQL-строковый литерал (или NULL). */
function refQ_(v) {
  if (v === null || v === undefined) return 'NULL';
  return "'" + String(v).replace(/\\/g, '\\\\').replace(/'/g, "\\'") + "'";
}

// ───────────────────────────────────────────────────────────────
// Точка входа + ядро
// ───────────────────────────────────────────────────────────────

/** Один прогон REF Sync под общим ScriptLock. Кнопка меню / (позже) триггер. */
function runRefSync() {
  var r = { status: 'ERROR', run_id: '', error_message: '', started_iso: '',
    active_before: null, active_after: null, tables: [] };

  var lock = LockService.getScriptLock();
  if (!lock.tryLock(1000)) {
    console.log('REF Sync: SKIPPED_LOCKED (активен другой запуск)');
    return { status: 'SKIPPED_LOCKED' };
  }
  try {
    refSyncCore_(r);
  } catch (e) {
    r.status = 'ERROR';
    r.error_message = 'Исключение: ' + ((e && e.message) || e);
    try {
      if (r.run_id) refRunsFinalize_(r.run_id, 'ERROR', r.active_before, r.error_message);
    } catch (e2) { console.error('REF finalize ERROR не удался: ' + ((e2 && e2.message) || e2)); }
  } finally {
    lock.releaseLock();
  }
  console.log('REF Sync: ' + r.status + ' | run=' + r.run_id +
    ' | active ' + r.active_before + ' → ' + r.active_after +
    (r.error_message ? ' | ' + r.error_message : ''));
  return r;
}

function refSyncCore_(r) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var now = new Date();
  var iso = now.toISOString();
  var stamp = Utilities.formatDate(now, 'Europe/Moscow', 'yyyyMMdd_HHmmss');
  var runId = 'REF_' + stamp + '_' + refShortUuid_();
  r.run_id = runId; r.started_iso = iso;

  refEnsureAll_();
  r.active_before = refActiveBefore_();
  refRunsInsertStarted_(runId, iso, r.active_before);

  // ── REF_SKU_MASTER: build → stage → validate ──
  var built = refBuildSkuMaster_(ss, runId, iso);
  bqLoadRows_(REF_T_SKU_DATA_, built.rows);         // WRITE_APPEND, невидимо
  var staged = built.rows.length;
  var val = refValidateSkuMaster_(runId, staged);
  r.tables.push({ name: REF_V_SKU_, source: built.source, staged: staged, val: val });

  // ── Публикация: всё или ничего ──
  if (!val.ok) {
    refTableLog_(runId, REF_V_SKU_, built.source, staged, staged, 0, val.errorCount, 'ERROR');
    refRunsFinalize_(runId, 'ERROR', r.active_before, 'Валидация: ' + val.errors.join('; '));
    r.status = 'ERROR';
    r.error_message = 'Валидация REF_SKU_MASTER: ' + val.errors.join('; ');
    return;   // fail-closed: указатель НЕ трогаем
  }

  refActivate_(runId);                              // одна DML — переключение
  r.active_after = runId;
  refTableLog_(runId, REF_V_SKU_, built.source, staged, staged, staged, 0, 'COMPLETE');
  refRunsFinalize_(runId, 'COMPLETE', runId, '');
  refPrune_();

  r.status = 'OK';
  console.log('REF_SKU_MASTER OK: строк ' + val.metrics.n + ' | nm_id ' + val.metrics.n_nm +
    ' | single ' + val.metrics.n_single + ' | bundle ' + val.metrics.n_bundle);
}

/** Печать последних прогонов. */
function refSyncStatus() {
  var res = refSql_('SELECT run_id, status, active_version_after, warning_count FROM ' +
    refFqn_(REF_T_RUNS_) + ' ORDER BY started_at DESC LIMIT 5');
  (res.rows || []).forEach(function (row) {
    console.log(row.f.map(function (x) { return x.v; }).join(' | '));
  });
}

// ───────────────────────────────────────────────────────────────
// Триггер (ставит владелец ПОСЛЕ ручной приёмки; в PR1 не вызываем)
// ───────────────────────────────────────────────────────────────

/** Суточный триггер runRefSync (после ночного контура). 0→1 / 1→1 / 2+→1. */
function refInstallDailyTrigger() {
  var trs = ScriptApp.getProjectTriggers(), mine = [];
  for (var i = 0; i < trs.length; i++) if (trs[i].getHandlerFunction() === REF_TRIGGER_FN_) mine.push(trs[i]);
  if (mine.length === 0) {
    ScriptApp.newTrigger(REF_TRIGGER_FN_).timeBased().everyDays(1).atHour(7).nearMinute(30).create();
    console.log('✅ Триггер REF Sync создан (~07:30 МСК)');
    return { created: 1, removed: 0 };
  }
  var removed = 0;
  for (var j = 1; j < mine.length; j++) { ScriptApp.deleteTrigger(mine[j]); removed++; }
  console.log(mine.length === 1 ? 'ℹ️ Триггер REF Sync уже есть.' : '⚠️ Удалены дубли: ' + removed);
  return { created: 0, removed: removed };
}

function refRemoveTrigger() {
  var trs = ScriptApp.getProjectTriggers(), n = 0;
  for (var i = 0; i < trs.length; i++) if (trs[i].getHandlerFunction() === REF_TRIGGER_FN_) { ScriptApp.deleteTrigger(trs[i]); n++; }
  console.log('🗑 Удалено триггеров REF Sync: ' + n);
  return { removed: n };
}
