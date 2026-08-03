/**
 * PR-Mart3a — журнал ранов загрузчиков (heartbeat для freshness-gate MART). REV2 (аудит).
 * Дизайн: docs/MART3_ORCHESTRATION_DESIGN_2026-07-31.md (REV4, блоки 1–2).
 * Таблица: wb_raw.INGEST_RUNS (DDL — sql/mart3/pr_mart3a_ingest_runs.sql).
 *
 * ЗАЧЕМ. Mart3 (fail-closed) должен доказывать ФАКТ УСПЕШНОГО ЗАПУСКА загрузчика за
 * целевые сутки. MAX(RAW.loaded_at) это не доказывает: при успешном ране с 0 новых
 * строк в RAW ничего не пишется → гейт ложно заблокировал бы сборку витрины.
 *
 * МОДЕЛЬ: одна строка на run. ingestRunStart_() → INSERT STARTED (возвращает runId);
 *   ingestRunComplete_() / ingestRunError_() → UPDATE ТОЙ ЖЕ строки по run_id.
 *   Терминальность: UPDATE идёт с условием status='STARTED', поэтому COMPLETE/ERROR
 *   не перезаписываются, а повторная финализация идемпотентна.
 *
 * REV2 (по замечаниям аудита):
 *   §2 WHITELIST УСПЕХА. «Всё, кроме ERROR» ≠ успех. ads PARTIAL/sales SKIPPED_RATE_LIMIT
 *       давали бы ложный COMPLETE-heartbeat и Mart3 собрал бы витрину на неполных данных.
 *       Успешные статусы перечислены явно (INGEST_SUCCESS_STATUSES_), всё прочее → ERROR.
 *   §3 ПРОВЕРКА numDmlAffectedRows. INSERT обязан дать ровно 1; финализация: 1 = обновлено,
 *       0 = проверяем SELECT'ом, действительно ли строка уже терминальна (иначе false),
 *       >1 = аномалия (run_id обязан быть уникален) → false + ERROR в лог.
 *
 * ⚠️ КОНТРАКТ УСТОЙЧИВОСТИ: ни одна функция модуля НЕ бросает исключение — сбой
 *   журналирования не должен скрывать/подменять исходную ошибку загрузчика.
 *   Обратная сторона (осознанно): пропущенная запись → гейт Mart3 не увидит heartbeat
 *   и fail-closed остановит сборку витрины. Это лучше «тихо неверной» витрины.
 *
 * Запись через BigQuery.Jobs.query (DML) с NAMED-параметрами:
 *   - DML, а не streaming insertAll → строка сразу доступна для UPDATE;
 *   - параметры, а не конкатенация → error_message с кавычками/переводами строк безопасен.
 */

var INGEST_RUNS_TABLE_ = 'INGEST_RUNS';
var INGEST_SOURCE_APPS_ = 'apps_script';

/**
 * §2 WHITELIST успешных статусов — по каждому загрузчику ЯВНО.
 * Всё, чего здесь нет (PARTIAL, SKIPPED_*, SINK_OFF, unknown, пустое), успехом НЕ считается
 * и пишется как ERROR → freshness-gate Mart3 такой день не пропустит.
 *
 * orders: OK / OK_NO_CHANGES / PARTIAL / ERROR  → успех только первые два
 *         (OK_NO_CHANGES — штатный zero-row success: изменений не было).
 * sales:  + SKIPPED_LOCKED / SKIPPED_RATE_LIMIT → оба НЕ успех.
 * ads:    OK / PARTIAL / STALE / ERROR / SKIPPED_LOCKED.
 *         STALE включён в успех ОСОЗНАННО: все источники загрузились (ран успешен),
 *         устаревание данных на стороне WB — это сценарий ADS_LAGGED, который Mart3
 *         обрабатывает маркером в витрине (ads_lagged / ads_business_max_date),
 *         а не блокировкой сборки. PARTIAL (неполнота загрузки) — успехом НЕ является.
 */
var INGEST_SUCCESS_STATUSES_ = {
  orders: ['OK', 'OK_NO_CHANGES'],
  sales:  ['OK', 'OK_NO_CHANGES'],
  ads:    ['OK', 'STALE'],
  selftest: ['OK']
};

/** Терминальные статусы журнала. */
var INGEST_TERMINAL_STATUSES_ = ['COMPLETE', 'ERROR'];

// ───────────────────────────────────────────────────────────────
// Внутреннее: выполнение SQL с named-параметрами
// ───────────────────────────────────────────────────────────────

/** {name,type,value} → BQ queryParameter. NULL-safe. */
function ingestParam_(name, type, value) {
  return {
    name: name,
    parameterType: { type: type },
    parameterValue: { value: (value === null || value === undefined) ? null : String(value) }
  };
}

/** Общий вызов BQ: возвращает {affected, rows}. Бросает — обёртки ловят. */
function ingestRunQuery_(sql, params) {
  var c = getBqConfig_();
  var req = {
    query: sql, useLegacySql: false, location: c.location,
    parameterMode: 'NAMED', timeoutMs: 30000
  };
  if (params && params.length) req.queryParameters = params;

  var res = BigQuery.Jobs.query(req, c.projectId);
  var jobId = res.jobReference && res.jobReference.jobId;
  var loc = (res.jobReference && res.jobReference.location) || c.location;

  var guard = 0;
  while (res.jobComplete !== true && jobId && guard++ < 60) {
    Utilities.sleep(1000);
    res = BigQuery.Jobs.getQueryResults(c.projectId, jobId, { location: loc, timeoutMs: 30000 });
  }
  if (res.jobComplete !== true) throw new Error('INGEST_RUNS: запрос не завершился за отведённое время');
  if (res.errors && res.errors.length) throw new Error('INGEST_RUNS: ' + JSON.stringify(res.errors));

  return {
    affected: (res.numDmlAffectedRows !== undefined) ? Number(res.numDmlAffectedRows) : null,
    rows: res.rows || []
  };
}

function ingestFqn_() {
  var c = getBqConfig_();
  return '`' + c.projectId + '.' + c.datasetId + '.' + INGEST_RUNS_TABLE_ + '`';
}

/** §3: текущий статус строки по run_id (для разбора affected=0). '' если строки нет. */
function ingestSelectStatus_(runId) {
  var res = ingestRunQuery_(
    'SELECT status FROM ' + ingestFqn_() + ' WHERE run_id=@run_id LIMIT 2',
    [ingestParam_('run_id', 'STRING', runId)]
  );
  if (!res.rows.length) return '';
  return String(res.rows[0].f[0].v || '');
}

/**
 * §3 + REV2-fix: разбор финализирующего UPDATE относительно ОЖИДАЕМОГО статуса.
 * 1  → обновлено (true);
 * >1 → аномалия уникальности run_id → false;
 * 0  → true ТОЛЬКО если строка уже в ОЖИДАЕМОМ терминальном статусе (доказанная идемпотентность).
 *      Противоположный терминальный статус (ждём COMPLETE, а строка ERROR — или наоборот) = КОНФЛИКТ → false.
 *      Раньше принимался любой терминальный статус: ingestRunComplete_ по уже-ERROR строке ложно возвращал true.
 */
function ingestInterpretFinalize_(affected, runId, expectedStatus) {
  if (affected === 1) return true;
  if (affected !== null && affected > 1) {
    Logger.log('INGEST_RUNS АНОМАЛИЯ: финализация в ' + expectedStatus + ' затронула ' + affected +
      ' строк (run_id не уникален?) run_id=' + runId);
    return false;
  }
  // affected === 0 (или неизвестно): сверяем ФАКТИЧЕСКИЙ статус с ОЖИДАЕМЫМ
  var actualStatus = '';
  try { actualStatus = ingestSelectStatus_(runId); } catch (e) {
    Logger.log('INGEST_RUNS: не удалось проверить статус после affected=0: ' + ((e && e.message) || e));
    return false;
  }
  if (actualStatus === expectedStatus) {
    return true; // доказанная идемпотентность: строка уже в нужном терминальном статусе
  }
  Logger.log('INGEST_RUNS КОНФЛИКТ СОСТОЯНИЙ: ожидался ' + expectedStatus +
    ', фактически "' + actualStatus + '", run_id=' + runId);
  return false;
}

// ───────────────────────────────────────────────────────────────
// logical_period — ДЕТЕРМИНИРОВАННЫЙ (не из фактически полученных строк)
// ───────────────────────────────────────────────────────────────

/**
 * Для orders/sales (hourly incremental): последний ПОЛНОСТЬЮ закрытый день —
 * вчерашняя дата по Москве. Любой ран суток X закрывает X−1.
 * Пример: ран 31.07 11:31 МСК → '2026-07-30' (именно его ждёт гейт при build D-1).
 */
function ingestClosedDayMsk_() {
  var d = new Date(Date.now() - 24 * 60 * 60 * 1000);
  return Utilities.formatDate(d, 'Europe/Moscow', 'yyyy-MM-dd');
}

function ingestRunId_(loaderName) {
  var ts = Utilities.formatDate(new Date(), 'Europe/Moscow', 'yyyyMMddHHmmss');
  var uuid8 = Utilities.getUuid().replace(/-/g, '').substring(0, 8);
  return 'INS_' + String(loaderName).toUpperCase() + '_' + ts + '_' + uuid8;
}

// ───────────────────────────────────────────────────────────────
// Публичный API логгера (никогда не бросает)
// ───────────────────────────────────────────────────────────────

/**
 * Открывает ран: INSERT строки STARTED. §3: требует affected=1.
 * @param {string} loaderName    'orders' | 'sales' | 'ads'
 * @param {string} logicalPeriod 'YYYY-MM-DD' — детерминированные сутки, которые закрывает ран
 * @param {string=} triggerType  'SCHEDULED' (по умолчанию) | 'MANUAL'
 * @return {string|null} runId, либо null если журналирование не удалось.
 */
function ingestRunStart_(loaderName, logicalPeriod, triggerType) {
  try {
    if (!loaderName || !logicalPeriod) throw new Error('loaderName/logicalPeriod обязательны');
    if (!/^\d{4}-\d{2}-\d{2}$/.test(String(logicalPeriod))) {
      throw new Error('logical_period должен быть YYYY-MM-DD, получено: ' + logicalPeriod);
    }
    var runId = ingestRunId_(loaderName);
    var res = ingestRunQuery_(
      'INSERT INTO ' + ingestFqn_() +
      ' (run_id, loader_name, logical_period, status, source, trigger_type, started_at) ' +
      'VALUES (@run_id, @loader_name, PARSE_DATE("%Y-%m-%d", @logical_period), ' +
      '        "STARTED", @source, @trigger_type, CURRENT_TIMESTAMP())',
      [
        ingestParam_('run_id', 'STRING', runId),
        ingestParam_('loader_name', 'STRING', loaderName),
        ingestParam_('logical_period', 'STRING', logicalPeriod),
        ingestParam_('source', 'STRING', INGEST_SOURCE_APPS_),
        ingestParam_('trigger_type', 'STRING', triggerType || 'SCHEDULED')
      ]
    );
    if (res.affected !== 1) {   // §3: INSERT обязан вставить ровно одну строку
      Logger.log('INGEST_RUNS: INSERT дал affected=' + res.affected + ' (ожидалось 1) — ран не журналируется');
      return null;
    }
    return runId;
  } catch (e) {
    Logger.log('ingestRunStart_ FAILED (не блокирует загрузчик): ' + ((e && e.message) || e));
    return null;
  }
}

/**
 * Закрывает ран успехом. rowsLoaded=0 — ВАЛИДНЫЙ успех (zero-row success).
 * §3: 1 = обновлено; 0 = true только если строка ДОКАЗАННО уже терминальна; >1 = false.
 */
function ingestRunComplete_(runId, rowsFetched, rowsLoaded) {
  if (!runId) return false;
  try {
    var res = ingestRunQuery_(
      'UPDATE ' + ingestFqn_() + ' SET status="COMPLETE", completed_at=CURRENT_TIMESTAMP(), ' +
      '  rows_fetched=@rows_fetched, rows_loaded=@rows_loaded ' +
      'WHERE run_id=@run_id AND status="STARTED"',
      [
        ingestParam_('run_id', 'STRING', runId),
        ingestParam_('rows_fetched', 'INT64', (rowsFetched === null || rowsFetched === undefined) ? 0 : rowsFetched),
        ingestParam_('rows_loaded', 'INT64', (rowsLoaded === null || rowsLoaded === undefined) ? 0 : rowsLoaded)
      ]
    );
    return ingestInterpretFinalize_(res.affected, runId, 'COMPLETE');
  } catch (e) {
    Logger.log('ingestRunComplete_ FAILED (не блокирует загрузчик): ' + ((e && e.message) || e));
    return false;
  }
}

/**
 * Закрывает ран ошибкой (в т.ч. «неуспешный» статус вне whitelist).
 * Вызывается ПЕРЕД пробросом исходной ошибки; никогда не бросает сам.
 */
function ingestRunError_(runId, errorCode, errorMessage) {
  if (!runId) return false;
  try {
    var msg = String((errorMessage === null || errorMessage === undefined) ? '' : errorMessage);
    if (msg.length > 4000) msg = msg.substring(0, 4000);
    var res = ingestRunQuery_(
      'UPDATE ' + ingestFqn_() + ' SET status="ERROR", completed_at=CURRENT_TIMESTAMP(), ' +
      '  error_code=@error_code, error_message=@error_message ' +
      'WHERE run_id=@run_id AND status="STARTED"',
      [
        ingestParam_('run_id', 'STRING', runId),
        ingestParam_('error_code', 'STRING', errorCode || 'LOADER_ERROR'),
        ingestParam_('error_message', 'STRING', msg)
      ]
    );
    return ingestInterpretFinalize_(res.affected, runId, 'ERROR');
  } catch (e) {
    Logger.log('ingestRunError_ FAILED (не блокирует загрузчик): ' + ((e && e.message) || e));
    return false;
  }
}

/**
 * §2: ЕДИНАЯ точка финализации по статусу загрузчика — применяет whitelist.
 * Успех (статус в INGEST_SUCCESS_STATUSES_[loader]) → COMPLETE; всё остальное → ERROR
 * с кодом вида ADS_PARTIAL / SALES_SKIPPED_RATE_LIMIT / ORDERS_UNKNOWN.
 * Вызывать ОДИН раз в конце рана, когда итоговый статус известен.
 */
function ingestFinalizeByStatus_(runId, loaderName, status, rowsFetched, rowsLoaded, errorMessage) {
  if (!runId) return false;
  try {
    var st = String(status || '').toUpperCase();
    var allowed = INGEST_SUCCESS_STATUSES_[loaderName] || [];
    if (st && allowed.indexOf(st) >= 0) {
      return ingestRunComplete_(runId, rowsFetched, rowsLoaded);
    }
    var code = String(loaderName).toUpperCase() + '_' + (st || 'UNKNOWN');
    var msg = errorMessage || ('Статус "' + st + '" не входит в whitelist успешных для ' + loaderName);
    return ingestRunError_(runId, code, msg);
  } catch (e) {
    Logger.log('ingestFinalizeByStatus_ FAILED (не блокирует загрузчик): ' + ((e && e.message) || e));
    return false;
  }
}

// ───────────────────────────────────────────────────────────────
// Ручные тесты (запускать из редактора Apps Script)
// ───────────────────────────────────────────────────────────────

/** Читает строку журнала целиком — для доказательных проверок в тестах. */
function ingestDebugRow_(runId) {
  var res = ingestRunQuery_(
    'SELECT status, IFNULL(rows_fetched,-1), IFNULL(rows_loaded,-1), IFNULL(error_code,"") ' +
    'FROM ' + ingestFqn_() + ' WHERE run_id=@run_id',
    [ingestParam_('run_id', 'STRING', runId)]
  );
  if (!res.rows.length) return null;
  var f = res.rows[0].f;
  return { status: f[0].v, rows_fetched: Number(f[1].v), rows_loaded: Number(f[2].v), error_code: f[3].v };
}

/**
 * Тест 1: обычный ран + ДОКАЗАТЕЛЬНАЯ проверка идемпотентности.
 * Ожидание: после повторной финализации строка НЕ изменилась (rows_loaded остался 7),
 * а повторный вызов вернул true именно как «уже терминальна».
 */
function ingestSelfTestComplete() {
  var id = ingestRunStart_('selftest', ingestClosedDayMsk_(), 'MANUAL');
  Logger.log('runId=' + id);
  Logger.log('первая финализация: ' + ingestRunComplete_(id, 10, 7));
  var before = ingestDebugRow_(id);
  var second = ingestRunComplete_(id, 999, 999);   // попытка перезаписать
  var after = ingestDebugRow_(id);
  Logger.log('повтор вернул: ' + second + ' (ожидание true — «уже терминальна»)');
  Logger.log('строка ДО:    ' + JSON.stringify(before));
  Logger.log('строка ПОСЛЕ: ' + JSON.stringify(after));
  Logger.log('ИДЕМПОТЕНТНОСТЬ ' + ((after && after.rows_loaded === 7 && after.status === 'COMPLETE') ? 'ДОКАЗАНА ✓' : 'НАРУШЕНА ✗'));
}

/** Тест 2 (ключевой): ZERO-ROW успех — rows_loaded=0 обязан дать COMPLETE. */
function ingestSelfTestZeroRows() {
  var id = ingestRunStart_('selftest', ingestClosedDayMsk_(), 'MANUAL');
  Logger.log('complete=' + ingestRunComplete_(id, 0, 0));
  Logger.log('строка: ' + JSON.stringify(ingestDebugRow_(id)) + ' (ожидание status=COMPLETE, rows_loaded=0)');
}

/** Тест 3: ERROR + кавычки/переводы строк (проверка параметризации). */
function ingestSelfTestError() {
  var id = ingestRunStart_('selftest', ingestClosedDayMsk_(), 'MANUAL');
  Logger.log('error=' + ingestRunError_(id, 'TEST_ERROR', 'Ошибка с "кавычками", \'апострофами\' и\nпереводом строки'));
  Logger.log('строка: ' + JSON.stringify(ingestDebugRow_(id)));
}

/**
 * Тест 4 (§2): whitelist. PARTIAL и SKIPPED_* обязаны дать ERROR, а не COMPLETE.
 * Ожидание: обе строки со status=ERROR и кодами ADS_PARTIAL / SALES_SKIPPED_RATE_LIMIT.
 */
function ingestSelfTestStatusWhitelist() {
  var a = ingestRunStart_('selftest', ingestClosedDayMsk_(), 'MANUAL');
  ingestFinalizeByStatus_(a, 'ads', 'PARTIAL', 100, 50, 'часть источников не догрузилась');
  Logger.log('ads PARTIAL → ' + JSON.stringify(ingestDebugRow_(a)) + ' (ожидание ERROR/ADS_PARTIAL)');

  var b = ingestRunStart_('selftest', ingestClosedDayMsk_(), 'MANUAL');
  ingestFinalizeByStatus_(b, 'sales', 'SKIPPED_RATE_LIMIT', 0, 0, 'rate limit');
  Logger.log('sales SKIPPED_RATE_LIMIT → ' + JSON.stringify(ingestDebugRow_(b)) + ' (ожидание ERROR/SALES_SKIPPED_RATE_LIMIT)');

  var c = ingestRunStart_('selftest', ingestClosedDayMsk_(), 'MANUAL');
  ingestFinalizeByStatus_(c, 'orders', 'OK_NO_CHANGES', 0, 0, '');
  Logger.log('orders OK_NO_CHANGES → ' + JSON.stringify(ingestDebugRow_(c)) + ' (ожидание COMPLETE, rows_loaded=0)');
}

/**
 * Тест 5 (REV2, cross-state): противоположная финализация не должна лгать и не должна менять строку.
 * Ожидание:
 *   COMPLETE → попытка ingestRunError_  = false, строка ОСТАЁТСЯ COMPLETE;
 *   ERROR    → попытка ingestRunComplete_ = false, строка ОСТАЁТСЯ ERROR.
 */
function ingestSelfTestCrossState() {
  // ветка 1: строка уже COMPLETE, пытаемся закрыть ERROR
  var a = ingestRunStart_('selftest', ingestClosedDayMsk_(), 'MANUAL');
  ingestRunComplete_(a, 5, 3);
  var aErr = ingestRunError_(a, 'SHOULD_NOT_APPLY', 'конфликт: строка уже COMPLETE');
  var aRow = ingestDebugRow_(a);
  Logger.log('COMPLETE→ERROR вернул: ' + aErr + ' (ожидание false); строка: ' + JSON.stringify(aRow));
  Logger.log('  ветка 1 ' + ((aErr === false && aRow && aRow.status === 'COMPLETE') ? 'ОК ✓' : 'ПРОВАЛ ✗'));

  // ветка 2: строка уже ERROR, пытаемся закрыть COMPLETE
  var b = ingestRunStart_('selftest', ingestClosedDayMsk_(), 'MANUAL');
  ingestRunError_(b, 'REAL_ERROR', 'исходная ошибка');
  var bCompl = ingestRunComplete_(b, 9, 9);
  var bRow = ingestDebugRow_(b);
  Logger.log('ERROR→COMPLETE вернул: ' + bCompl + ' (ожидание false); строка: ' + JSON.stringify(bRow));
  Logger.log('  ветка 2 ' + ((bCompl === false && bRow && bRow.status === 'ERROR') ? 'ОК ✓' : 'ПРОВАЛ ✗'));
}
