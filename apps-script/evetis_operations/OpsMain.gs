/**
 * EVETIS OPERATIONS · C2 — обновление листа и точки входа.
 *
 * Обновление (opsRefresh_): блокировка → все запросы к контракту → проверка схемы, ключей, сверок, одного дня
 * → проверка раскладки всех листов → загрузка и сверка решений владельца → отрисовка в памяти → ТОЛЬКО ПОТОМ запись.
 * Ошибка до записи: в книге меняется только статус (ОШИБКА), данные на экране — последний проверенный снимок.
 * Ошибка во время записи: флаг OPS_WRITE_IN_PROGRESS; следующее обновление перерисовывает сгенерированные
 * диапазоны из свежих данных, а если их не получить — из последнего успешного снимка. _OPS_STATE не откатывается.
 *
 * Точки входа: onOpen (меню, без BigQuery), opsMenuRefresh / opsMenuStatus / opsMenuEnableAuto / opsMenuDisableAuto,
 * opsTriggerRefresh (каждый час), opsOnEditInstalled (решение владельца), opsSetup (однократно из редактора).
 */

// ───────────────────────────── меню ─────────────────────────────

function onOpen() {
  var ui = SpreadsheetApp.getUi();
  ui.createMenu('EVETIS OPERATIONS')
    .addItem('Обновить данные', 'opsMenuRefresh')
    .addItem('Проверить статус', 'opsMenuStatus')
    .addSeparator()
    .addItem('Включить автообновление (каждый час)', 'opsMenuEnableAuto')
    .addItem('Выключить автообновление', 'opsMenuDisableAuto')
    .addToUi();
  if (typeof opsTestMenu_ === 'function') opsTestMenu_(ui);   // только во временной тестовой копии (OpsTests.gs)
  try {
    var st = JSON.parse(PropertiesService.getScriptProperties().getProperty('OPS_STATUS') || 'null');
    if (!st) return;
    var ss = SpreadsheetApp.getActive();
    var ageH = (Date.now() - Number(st.lastOkCheckMs || st.lastSuccessMs || 0)) / 3600000;
    if (st.status === 'ERROR') {
      ss.toast('Последнее обновление не прошло проверку: ' + st.message + '. На экране — данные на ' +
        opsFmtMsk_(st.dataAsOfMs) + ' МСК.', 'EVETIS OPERATIONS', 15);
    } else if (ageH > OPS.STALE_REFRESH_HOURS) {
      ss.toast('Успешной проверки данных не было ' + Math.floor(ageH) + ' ч (данные на ' + opsFmtMsk_(st.dataAsOfMs) +
        ' МСК). Меню EVETIS OPERATIONS → Обновить данные.', 'EVETIS OPERATIONS', 15);
    }
  } catch (e) {
    // onOpen не должен падать из-за статуса
  }
}

function opsMenuRefresh() {
  var run = opsRefresh_('MENU', { force: true });
  SpreadsheetApp.getActive().toast(opsRunText_(run), 'EVETIS OPERATIONS', 12);
}

function opsMenuStatus() {
  var props = PropertiesService.getScriptProperties(), ui = SpreadsheetApp.getUi();
  var st = JSON.parse(props.getProperty('OPS_STATUS') || '{}'), log = JSON.parse(props.getProperty('OPS_RUNLOG') || '[]');
  var lines = [
    'Статус: ' + (st.status || 'обновлений ещё не было') + (st.message ? ' — ' + st.message : ''),
    'Последнее успешное обновление: ' + (st.lastSuccessMs ? opsFmtMsk_(st.lastSuccessMs) + ' МСК' : '—'),
    'Последняя проверка: ' + (st.lastCheckMs ? opsFmtMsk_(st.lastCheckMs) + ' МСК' : '—'),
    'Данные на экране: ' + (st.dataAsOfMs ? opsFmtMsk_(st.dataAsOfMs) + ' МСК' : '—'),
    'Автообновление: ' + opsNextAutoText_(Date.now()),
    '',
    'Последние запуски:'
  ];
  log.slice(0, 8).forEach(function (r) {
    lines.push(r.t0 + ' · ' + r.src + ' · ' + r.st + ' · ' + r.s + ' с · строк ' + r.rr + ' / записано ' + r.rw +
      (r.err ? ' · ' + r.err : ''));
  });
  ui.alert('EVETIS OPERATIONS — обновление данных', lines.join('\n'), ui.ButtonSet.OK);
}

function opsMenuEnableAuto() {
  opsSetAutoRefresh_(true);
  opsRedrawStatusFromProps_();
  SpreadsheetApp.getActive().toast('Автообновление включено: каждый час.', 'EVETIS OPERATIONS', 8);
}

function opsMenuDisableAuto() {
  opsSetAutoRefresh_(false);
  opsRedrawStatusFromProps_();
  SpreadsheetApp.getActive().toast('Автообновление выключено. Обновлять — через меню.', 'EVETIS OPERATIONS', 8);
}

function opsTriggerRefresh() {
  opsRefresh_('TRIGGER');
}

/** Однократно из редактора Apps Script: разметка C1.2 → C2, триггер решений владельца, первое обновление. */
function opsSetup() {
  var ss = SpreadsheetApp.getActive(), out = {};
  var lock = LockService.getScriptLock();
  lock.waitLock(60000);
  try {
    out.install = opsIsLayoutInstalled_(ss) ? 'уже установлено' : opsInstallLayout_(ss);
  } finally {
    lock.releaseLock();
  }
  opsEnsureEditTrigger_();
  var run = opsRefresh_('SETUP', { force: true });
  out.refresh = { st: run.st, err: run.err, rows: run.rr, written: run.rw };
  console.log(JSON.stringify({ ops_setup: out }));
  return out;
}

/** Из редактора или меню: включить ежечасное обновление. */
function opsEnableAutoRefresh() {
  opsSetAutoRefresh_(true);
  opsRedrawStatusFromProps_();
  console.log('ops: автообновление включено');
}

function opsSetAutoRefresh_(on) {
  var ts = ScriptApp.getProjectTriggers().filter(function (t) { return t.getHandlerFunction() === OPS.REFRESH_HANDLER; });
  if (on && !ts.length) ScriptApp.newTrigger(OPS.REFRESH_HANDLER).timeBased().everyHours(1).create();
  if (!on) ts.forEach(function (t) { ScriptApp.deleteTrigger(t); });
}

function opsNextAutoText_(nowMs) {
  var on = ScriptApp.getProjectTriggers().some(function (t) { return t.getHandlerFunction() === OPS.REFRESH_HANDLER; });
  if (!on) return 'выключено (меню EVETIS OPERATIONS → Включить автообновление)';
  var last = Number(PropertiesService.getScriptProperties().getProperty('OPS_LAST_TRIGGER_AT') || 0);
  var next = last ? last + 3600000 : 0;
  while (next && next < nowMs) next += 3600000;
  return 'каждый час' + (next ? ' · ориентировочно ' + opsFmtMsk_(next, 'HH:mm') + ' МСК' : ' · первый запуск в течение часа');
}

// ───────────────────────────── обновление ─────────────────────────────

function opsRefresh_(source, opts) {
  opts = opts || {};
  var run = { id: Utilities.getUuid().slice(0, 8), src: source, t0: Date.now(), st: '', rr: 0, rw: 0, sts: '', err: '' };
  var lock = LockService.getScriptLock();
  if (!lock.tryLock(OPS.LOCK_WAIT_MS)) {
    run.st = 'SKIPPED_LOCKED';
    opsLogRun_(run);
    return run;
  }
  var props = PropertiesService.getScriptProperties(), ss = SpreadsheetApp.getActive();
  try {
    if (source === 'TRIGGER') props.setProperty('OPS_LAST_TRIGGER_AT', String(run.t0));
    var pending = props.getProperty('OPS_WRITE_IN_PROGRESS');
    var prepared = null, failure = null;
    try {
      var payload = opsFetchAll_();
      if (typeof opsTestHook_ === 'function') opsTestHook_('afterFetch', payload);
      prepared = opsPrepare_(ss, payload, { recovery: !!pending });
    } catch (err) {
      failure = err;
      run.err = opsErrText_(err);
    }
    if (failure && pending) {
      var last = opsLoadSnapshot_('OPS_SNAP');
      if (last) {
        try {
          prepared = opsPrepare_(ss, last, { recovery: true });
          prepared.restored = true;
        } catch (err2) {
          run.err += ' | восстановление: ' + opsErrText_(err2);
          prepared = null;
        }
      }
    }
    if (!prepared) {
      run.st = 'FAILED_CLOSED';
      opsWriteStatusSafe_(ss, opsStatusError_(props, failure, run.t0, null), run);
      return run;
    }
    run.rr = prepared.rowsReceived;
    run.sts = prepared.sourceTs;

    var unchanged = !pending && !prepared.restored && !opts.force && prepared.state.events.length === 0 &&
      prepared.hash === props.getProperty('OPS_LAST_HASH');
    var status = prepared.restored ? opsStatusError_(props, failure, run.t0, prepared)
      : opsStatusOk_(prepared, run.t0, unchanged, props);
    if (unchanged) {
      // проверка без изменений не сдвигает «Данные на» и «Последнее успешное обновление» — только «Последняя проверка»
      props.setProperty('OPS_LAST_OK_CHECK_AT', String(run.t0));
      opsWriteStatusSafe_(ss, status, run);
      run.st = prepared.warnings.length ? 'WARNING_NO_CHANGE' : 'OK_NO_CHANGE';
      return run;
    }

    var render = opsRender_(prepared.payload, prepared.state.byKey, status);   // вся отрисовка — до первой записи
    props.setProperty('OPS_WRITE_IN_PROGRESS', run.id);
    opsWriteState_(ss, prepared.state.rows, prepared.state.events, prepared.stateEventCount);
    var recovery = !!pending || prepared.restored;
    OPS_WRITE_ORDER.forEach(function (name) {
      run.rw += opsWriteSheet_(ss, name, render[name], { recovery: recovery });
    });
    SpreadsheetApp.flush();
    if (!prepared.restored) {
      opsSaveSnapshot_('OPS_SNAP', prepared.payload);
      props.setProperties({
        OPS_LAST_HASH: prepared.hash, OPS_LAST_GOOD_AT: String(prepared.payload.fetchedAtMs), OPS_LAST_GOOD_TAIL: status.tail,
        OPS_LAST_GOOD_FRESH: status.freshness, OPS_LAST_SUCCESS_AT: String(run.t0), OPS_LAST_OK_CHECK_AT: String(run.t0)
      });
    }
    props.deleteProperty('OPS_WRITE_IN_PROGRESS');
    opsSaveStatus_(props, status);
    run.st = prepared.restored ? 'RESTORED_LAST_GOOD' : (prepared.warnings.length ? 'WARNING' : 'OK');
    return run;
  } catch (err) {
    run.st = 'WRITE_FAILED';
    run.err = (run.err ? run.err + ' | ' : '') + opsErrText_(err);
    opsWriteStatusSafe_(ss, opsStatusError_(props, OpsError_('WRITE', opsErrText_(err)), run.t0, null), run);
    return run;
  } finally {
    opsLogRun_(run);
    lock.releaseLock();
  }
}

/** Всё, что должно пройти до первой записи. */
function opsPrepare_(ss, payload, opts) {
  var validation = opsValidatePayload_(payload);
  if (typeof opsTestHook_ === 'function') opsTestHook_('afterValidate', payload);
  opsValidateLayouts_(ss, { recovery: !!opts.recovery });
  var stateRead = opsReadState_(ss);
  var state = opsReconcileState_(stateRead.rows, payload.views.V_OPS_SHEET_PLAN.rows, opsNowText_());
  var meta = payload.views.V_OPS_SHEET_META.rows[0];
  return {
    payload: payload, warnings: validation.warnings, rowsReceived: validation.rowsReceived, state: state,
    stateEventCount: stateRead.eventCount, hash: opsDigestHex_(opsHashInput_(payload)),
    sourceTs: 'WB ' + meta.wb_stock_date + ' · Ozon ' + meta.ozon_stock_date, restored: false
  };
}

/**
 * «Данные на» и «Последнее успешное обновление» — время последней ЗАПИСИ данных в лист.
 * При проверке без изменений они остаются прежними; меняется только «Последняя проверка».
 */
function opsStatusOk_(prepared, nowMs, unchanged, props) {
  var meta = prepared.payload.views.V_OPS_SHEET_META.rows[0], warn = prepared.warnings;
  var lastGoodAt = Number(props.getProperty('OPS_LAST_GOOD_AT') || 0);
  var lastWriteAt = Number(props.getProperty('OPS_LAST_SUCCESS_AT') || 0);
  return {
    status: warn.length ? 'WARNING' : 'OK', message: warn.join(', '),
    dataAsOfMs: unchanged && lastGoodAt ? lastGoodAt : prepared.payload.fetchedAtMs,
    lastSuccessMs: unchanged && lastWriteAt ? lastWriteAt : nowMs,
    lastCheckMs: nowMs, tail: opsStatusLineTail_(prepared.payload), freshness: opsFreshnessText_(meta),
    nextAuto: opsNextAutoText_(nowMs), checkResult: unchanged ? 'данные не изменились' : 'данные обновлены'
  };
}

function opsStatusError_(props, err, nowMs, prepared) {
  return {
    status: 'ERROR', message: opsUserError_(err),
    dataAsOfMs: prepared ? prepared.payload.fetchedAtMs : Number(props.getProperty('OPS_LAST_GOOD_AT') || 0),
    lastSuccessMs: Number(props.getProperty('OPS_LAST_SUCCESS_AT') || 0), lastCheckMs: nowMs,
    tail: prepared ? opsStatusLineTail_(prepared.payload) : (props.getProperty('OPS_LAST_GOOD_TAIL') || ''),
    freshness: prepared ? opsFreshnessText_(prepared.payload.views.V_OPS_SHEET_META.rows[0]) : (props.getProperty('OPS_LAST_GOOD_FRESH') || ''),
    nextAuto: opsNextAutoText_(nowMs),
    checkResult: prepared ? 'ошибка — показан последний успешный снимок' : 'ошибка — данные на экране не менялись'
  };
}

/** Короткий текст ошибки для владельца. Техническая подробность — только в журнале запусков («Проверить статус»). */
function opsUserError_(err) {
  var code = err && err.opsCode ? err.opsCode : 'INTERNAL';
  var text = OPS_TEXT.ERRORS[code] || OPS_TEXT.ERRORS.INTERNAL;
  if (/^(BQ_QUERY|BQ_TIMEOUT|WRITE|INTERNAL)$/.test(code)) return text;
  var detail = err && err.message ? String(err.message).replace(/^[A-Z_]+: /, '') : '';
  return text + (detail ? ' (' + (detail.length > 60 ? detail.slice(0, 57) + '…' : detail) + ')' : '');
}

function opsErrText_(err) {
  return String(err && err.message ? err.message : err).slice(0, 400);
}

function opsSaveStatus_(props, st) {
  props.setProperty('OPS_STATUS', JSON.stringify({ status: st.status, message: st.message, dataAsOfMs: st.dataAsOfMs,
    lastSuccessMs: st.lastSuccessMs, lastCheckMs: st.lastCheckMs, checkResult: st.checkResult,
    lastOkCheckMs: Number(props.getProperty('OPS_LAST_OK_CHECK_AT') || 0) }));
}

function opsWriteStatusSafe_(ss, status, run) {
  var props = PropertiesService.getScriptProperties();
  try {
    opsWriteStatusOnly_(ss, status);
  } catch (e) {
    run.err = (run.err ? run.err + ' | ' : '') + 'статус не записан: ' + opsErrText_(e);
  }
  opsSaveStatus_(props, status);
}

function opsRedrawStatusFromProps_() {
  var props = PropertiesService.getScriptProperties(), s = JSON.parse(props.getProperty('OPS_STATUS') || 'null');
  if (!s) return;
  s.tail = props.getProperty('OPS_LAST_GOOD_TAIL') || '';
  s.freshness = props.getProperty('OPS_LAST_GOOD_FRESH') || '';
  s.nextAuto = opsNextAutoText_(Date.now());
  try { opsWriteStatusOnly_(SpreadsheetApp.getActive(), s); } catch (e) { console.warn('ops: статус не перерисован: ' + opsErrText_(e)); }
}

function opsRunText_(run) {
  var map = {
    OK: 'Данные обновлены.', OK_NO_CHANGE: 'Проверено: данные не изменились.',
    WARNING: 'Данные обновлены, но часть источников устарела — см. 04_SETTINGS.',
    WARNING_NO_CHANGE: 'Данные не изменились, часть источников устарела — см. 04_SETTINGS.',
    FAILED_CLOSED: 'Обновление не прошло проверку — на экране остались последние проверенные данные.',
    RESTORED_LAST_GOOD: 'Свежие данные не получены — восстановлен последний успешный снимок.',
    WRITE_FAILED: 'Запись прервана — при следующем обновлении лист восстановится.',
    SKIPPED_LOCKED: 'Обновление уже идёт — повторите через минуту.'
  };
  return (map[run.st] || run.st) + (run.err ? ' ' + run.err.slice(0, 160) : '');
}

// ───────────────────────────── BigQuery ─────────────────────────────

function opsFetchAll_() {
  if (typeof opsTestHook_ === 'function') {
    var cached = opsTestHook_('fetch', null);
    if (cached) return cached;
  }
  var P = OPS.PROJECT_ID, suffix = typeof opsTestViewSuffix_ === 'function' ? opsTestViewSuffix_() : '';
  var jobs = Object.keys(OPS_VIEWS).map(function (name) {
    try {
      var job = BigQuery.Jobs.insert({
        jobReference: { projectId: P, location: OPS.LOCATION },
        configuration: {
          labels: { stage: 'stage-c2', component: 'evetis-operations-sheet' },
          query: { query: 'SELECT * FROM `' + P + '.' + OPS.DATASET + '.' + name + suffix + '`', useLegacySql: false }
        }
      }, P);
      return { name: name, id: job.jobReference.jobId };
    } catch (err) {
      throw OpsError_('BQ_QUERY', name + ': ' + opsErrText_(err));
    }
  });
  var views = {}, deadline = Date.now() + OPS.BQ_TIMEOUT_MS;
  jobs.forEach(function (j) {
    var res;
    while (true) {
      try {
        res = BigQuery.Jobs.getQueryResults(P, j.id, { location: OPS.LOCATION, timeoutMs: 30000, maxResults: 10000 });
      } catch (err) {
        throw OpsError_('BQ_QUERY', j.name + ': ' + opsErrText_(err));
      }
      if (res.jobComplete) break;
      if (Date.now() > deadline) throw OpsError_('BQ_TIMEOUT', j.name);
    }
    var raw = res.rows || [], token = res.pageToken;
    while (token) {
      var page = BigQuery.Jobs.getQueryResults(P, j.id, { location: OPS.LOCATION, pageToken: token, maxResults: 10000 });
      raw = raw.concat(page.rows || []);
      token = page.pageToken;
    }
    opsCheck_(Number(res.totalRows) === raw.length, 'BQ_QUERY', j.name + ': получено ' + raw.length + ' строк из ' + res.totalRows);
    var fields = res.schema.fields.map(function (f) { return { name: f.name, type: f.type }; });
    views[j.name] = { fields: fields, rows: opsTypeRows_(fields, raw) };
  });
  return { fetchedAtMs: Date.now(), views: views };
}

function opsDigestHex_(s) {
  return Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, s, Utilities.Charset.UTF_8)
    .map(function (b) { return ('0' + (b & 0xff).toString(16)).slice(-2); }).join('');
}

// ───────────────────────────── снимок и журнал запусков ─────────────────────────────

function opsSaveSnapshot_(prefix, payload) {
  var props = PropertiesService.getScriptProperties();
  var b64 = Utilities.base64Encode(Utilities.gzip(Utilities.newBlob(JSON.stringify(payload), 'application/json')).getBytes());
  var n = Math.ceil(b64.length / OPS.SNAPSHOT_CHUNK), chunks = {}, old = Number(props.getProperty(prefix + '_N') || 0);
  for (var i = 0; i < n; i++) chunks[prefix + '_' + i] = b64.substr(i * OPS.SNAPSHOT_CHUNK, OPS.SNAPSHOT_CHUNK);
  chunks[prefix + '_N'] = String(n);
  props.setProperties(chunks);
  for (var j = n; j < old; j++) props.deleteProperty(prefix + '_' + j);
}

function opsLoadSnapshot_(prefix) {
  var props = PropertiesService.getScriptProperties(), n = Number(props.getProperty(prefix + '_N') || 0);
  if (!n) return null;
  var b64 = '';
  for (var i = 0; i < n; i++) b64 += props.getProperty(prefix + '_' + i) || '';
  try {
    var blob = Utilities.ungzip(Utilities.newBlob(Utilities.base64Decode(b64), 'application/x-gzip'));
    return JSON.parse(blob.getDataAsString());
  } catch (e) {
    return null;
  }
}

function opsLogRun_(run) {
  run.t1 = Date.now();
  console.log(JSON.stringify({ ops_refresh: run }));
  var props = PropertiesService.getScriptProperties(), log = [];
  try { log = JSON.parse(props.getProperty('OPS_RUNLOG') || '[]'); } catch (e) { log = []; }
  log.unshift({ id: run.id, src: run.src, st: run.st, t0: opsFmtMsk_(run.t0, 'dd.MM HH:mm:ss'), s: Math.round((run.t1 - run.t0) / 1000),
    rr: run.rr, rw: run.rw, sts: run.sts, err: String(run.err || '').slice(0, 180) });
  while (log.length > OPS.RUNLOG_KEEP || JSON.stringify(log).length > 8500) log.pop();
  props.setProperty('OPS_RUNLOG', JSON.stringify(log));
}
