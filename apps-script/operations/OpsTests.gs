/**
 * EVETIS OPERATIONS · C2 — тесты в ВРЕМЕННОЙ КОПИИ книги «EVETIS OPERATIONS · C2 TEST».
 *
 * Этот файл НЕ входит в рабочий проект: без него хуки opsTestHook_ / opsTestViewSuffix_ не существуют.
 * Любая функция отказывается работать, если книга — рабочая EVETIS OPERATIONS.
 *
 * Порядок запуска из редактора (каждая функция — отдельный запуск, результат в журнале выполнения):
 *   opsSetup → opsTest01_normal → opsTest02_approvalPersists → opsTest03_recommendationChanged → opsTest04_rowReorder
 *   → opsTest05_bigQueryFailure → opsTest06_schemaFailure → opsTest07_duplicateKey
 *   → opsTest08a_startLockHolder (подождать 30–90 с) → opsTest08b_refreshWhileLocked
 *   → opsTest09_formattingPreserved → opsTest10_idempotent → opsTest11_displayNameChange
 *   → opsTest12_disappearAndReturn → opsTest13_changeAndReturn → opsTest14_partialWriteRecovery → opsTestSummary
 * Тесты 2–14 берут данные из базового снимка (OPS_TSNAP, сохраняет тест 1) — без BigQuery; неисправности
 * вносятся в копию выборки в памяти. Тест 5 обращается к BigQuery по-настоящему (несуществующее представление).
 */

function opsT_guard_() {
  if (SpreadsheetApp.getActive().getId() === OPS.PROD_SPREADSHEET_ID) {
    throw new Error('Тесты C2 запрещены в рабочей книге EVETIS OPERATIONS');
  }
}

function opsT_props_() {
  return PropertiesService.getScriptProperties();
}

/** Меню «C2 ТЕСТЫ» в тестовой копии: запуск установки и тестов из таблицы (результат — всплывающее сообщение). */
function opsTestMenu_(ui) {
  if (SpreadsheetApp.getActive().getId() === OPS.PROD_SPREADSHEET_ID) return;
  ui.createMenu('C2 ТЕСТЫ')
    .addItem('0 · установка и первое обновление (opsSetup)', 'opsTestSetup')
    .addSeparator()
    .addItem('T01 · обычное обновление', 'opsTest01_normal')
    .addItem('T02 · решение сохраняется', 'opsTest02_approvalPersists')
    .addItem('T03 · рекомендация изменилась', 'opsTest03_recommendationChanged')
    .addItem('T04 · строки поменялись местами', 'opsTest04_rowReorder')
    .addItem('T05 · сбой BigQuery', 'opsTest05_bigQueryFailure')
    .addItem('T06 · сбой схемы', 'opsTest06_schemaFailure')
    .addItem('T07 · повтор ключа', 'opsTest07_duplicateKey')
    .addItem('T08a · занять блокировку', 'opsTest08a_startLockHolder')
    .addItem('T08b · обновление при занятой блокировке', 'opsTest08b_refreshWhileLocked')
    .addItem('T09 · оформление сохраняется', 'opsTest09_formattingPreserved')
    .addItem('T10 · идемпотентность', 'opsTest10_idempotent')
    .addItem('T11 · другое название товара', 'opsTest11_displayNameChange')
    .addItem('T12 · строка ушла и вернулась', 'opsTest12_disappearAndReturn')
    .addItem('T13 · 100 → 120 → 100', 'opsTest13_changeAndReturn')
    .addItem('T14 · прерванная запись', 'opsTest14_partialWriteRecovery')
    .addSeparator()
    .addItem('Визуально: состояние ОШИБКА', 'opsTestShowError')
    .addItem('Визуально: состояние ВНИМАНИЕ (устарели источники)', 'opsTestShowWarning')
    .addSeparator()
    .addItem('Итоги тестов', 'opsTestSummaryAlert')
    .addToUi();
}

function opsT_toast_(text) {
  try { SpreadsheetApp.getActive().toast(String(text).slice(0, 400), 'C2 ТЕСТЫ', 60); } catch (e) { /* без UI */ }
}

function opsTestSetup() {
  opsT_guard_();
  var out = opsSetup();
  opsT_toast_('opsSetup · ' + JSON.stringify(out.refresh));
  return out;
}

function opsTestSummaryAlert() {
  var all = opsTestSummary(), ui = SpreadsheetApp.getUi();
  ui.alert('C2 — итоги тестов', Object.keys(all).sort().map(function (k) { return k + ' · ' + all[k]; }).join('\n\n'), ui.ButtonSet.OK);
}

// ───────────────────────────── хуки рабочего кода ─────────────────────────────

function opsTestViewSuffix_() {
  var s = opsT_props_().getProperty('OPS_TEST_VIEW_SUFFIX');
  if (s) opsT_guard_();
  return s || '';
}

function opsTestHook_(stage, ctx) {
  var props = opsT_props_(), raw = props.getProperty('OPS_TEST_FAULT');
  if (raw) {
    opsT_guard_();
    var f = JSON.parse(raw);
    if (f.stage === stage && opsT_counterReached_(f)) {
      if (f.once) props.deleteProperty('OPS_TEST_FAULT');
      opsT_applyFault_(f, ctx);
    }
  }
  if (stage === 'fetch' && props.getProperty('OPS_TEST_CACHE') === '1') {
    opsT_guard_();
    var base = opsLoadSnapshot_('OPS_TSNAP');
    if (base) {
      base.fetchedAtMs = Date.now();
      return base;
    }
  }
  return null;
}

function opsT_counterReached_(f) {
  if (!f.after) return true;
  var props = opsT_props_(), n = Number(props.getProperty('OPS_TEST_COUNTER') || 0) + 1;
  props.setProperty('OPS_TEST_COUNTER', String(n));
  if (n < f.after) return false;
  props.deleteProperty('OPS_TEST_COUNTER');
  return true;
}

/** Тот же отпечаток, что в V_OPS_SHEET_PLAN (тест 1 сверяет со всеми строками). */
function opsT_fingerprint_(r) {
  return opsDigestHex_([r.business_key, r.shipping_method || '-', r.status_code || '-', r.rule || '-', r.rec_final,
    r.rec_physical_units, r.gates || ''].join('|')).slice(0, 16);
}

function opsT_applyFault_(f, ctx) {
  var plan = ctx && ctx.views ? ctx.views.V_OPS_SHEET_PLAN.rows : null;
  function row(key) {
    var r = plan.filter(function (x) { return x.business_key === key; })[0];
    if (!r) throw new Error('тест: нет ключа ' + key);
    return r;
  }
  switch (f.type) {
    case 'BQ_FAIL':
      throw OpsError_('BQ_QUERY', 'тест: BigQuery недоступен');
    case 'WRITE_FAIL':
      throw OpsError_('WRITE', 'тест: сбой записи после блока ' + ctx.sheet + '/' + ctx.block);
    case 'DROP_COLUMN':
      var v = ctx.views[f.view];
      v.fields = v.fields.filter(function (x) { return x.name !== f.column; });
      v.rows.forEach(function (r) { delete r[f.column]; });
      break;
    case 'DUP_KEY':
      var d = ctx.views[f.view];
      d.rows.push(JSON.parse(JSON.stringify(d.rows[0])));
      break;
    case 'RENAME':
      row(f.key).product_name = f.name;
      break;
    case 'SET_REC':
      var s = row(f.key), per = s.rec_final ? s.rec_physical_units / s.rec_final : 1;
      s.rec_final = f.rec_final;
      s.rec_physical_units = Math.round(f.rec_final * per);
      s.rec_fingerprint = opsT_fingerprint_(s);
      break;
    case 'TO_HOLD':
      var h = row(f.key), maxHold = 0;
      plan.forEach(function (x) { if (x.sheet_block === 'HOLD') maxHold = Math.max(maxHold, x.block_order); });
      h.rec_final = 0;
      h.rec_physical_units = 0;
      h.status_code = 'WAIT';
      h.status_label = 'ЖДАТЬ (тест C2)';
      h.short_reason = 'тест C2: рекомендация временно 0';
      h.sheet_block = 'HOLD';
      h.block_order = maxHold + 1;
      h.rec_fingerprint = opsT_fingerprint_(h);
      break;
    case 'STALE':
      var meta = ctx.views.V_OPS_SHEET_META.rows[0];
      meta.wb_stock_stale = true;
      meta.ozon_orders_stale = true;
      break;
    case 'REORDER':
      var a = row(f.keyA), b = row(f.keyB), tmp = a.block_order;
      a.block_order = b.block_order;
      b.block_order = tmp;
      break;
    default:
      throw new Error('тест: неизвестная неисправность ' + f.type);
  }
}

// ───────────────────────────── помощники ─────────────────────────────

function opsT_fault_(f) {
  opsT_props_().deleteProperty('OPS_TEST_COUNTER');
  opsT_props_().setProperty('OPS_TEST_FAULT', JSON.stringify(f));
}

function opsT_clear_() {
  var props = opsT_props_();
  ['OPS_TEST_FAULT', 'OPS_TEST_COUNTER', 'OPS_TEST_VIEW_SUFFIX'].forEach(function (k) { props.deleteProperty(k); });
}

function opsT_cache_(on) {
  if (on) opsT_props_().setProperty('OPS_TEST_CACHE', '1');
  else opsT_props_().deleteProperty('OPS_TEST_CACHE');
}

function opsT_result_(id, pass, details) {
  opsT_clear_();
  var r = { test: id, result: pass ? 'PASS' : 'FAIL', at: opsNowText_(), details: details };
  console.log(JSON.stringify(r));
  opsT_toast_(id + ' · ' + r.result + ' · ' + JSON.stringify(details).slice(0, 300));
  var props = opsT_props_(), all = JSON.parse(props.getProperty('OPS_TEST_RESULTS') || '{}');
  all[id] = r.result + ' · ' + r.at + ' · ' + JSON.stringify(details).slice(0, 320);
  props.setProperty('OPS_TEST_RESULTS', JSON.stringify(all));
  return r;
}

function opsT_base_() {
  var p = opsLoadSnapshot_('OPS_TSNAP');
  if (!p) throw new Error('нет базового снимка — сначала opsTest01_normal');
  return p;
}

function opsT_keys_() {
  var plan = opsT_base_().views.V_OPS_SHEET_PLAN.rows;
  var block = function (b) {
    return plan.filter(function (r) { return r.sheet_block === b; }).sort(function (x, y) { return x.block_order - y.block_order; });
  };
  var wb = block('WB_SHIP'), oz = block('OZON_SHIP');
  return { A: wb[0].business_key, B: wb[wb.length - 1].business_key, C: wb[1].business_key, D: oz[0].business_key };
}

function opsT_baseRow_(key) {
  return opsT_base_().views.V_OPS_SHEET_PLAN.rows.filter(function (r) { return r.business_key === key; })[0];
}

function opsT_findRow_(block, businessKey) {
  var sh = opsSheet_(SpreadsheetApp.getActive(), OPS_SHEET.PLAN), marks = opsReadMarkers_(sh, OPS_LAYOUT[OPS_SHEET.PLAN].sysCol);
  for (var i = 0; i < marks.length; i++) {
    var t = opsParseMarker_(marks[i], i + 1);
    if (t.type !== 'R' || t.id !== block) continue;
    var k = block === 'WB_SHIP' || block === 'OZON_SHIP' ? opsSplitShipRowKey_(t.key).businessKey : t.key;
    if (k === businessKey) return { sh: sh, row: i + 1 };
  }
  return null;
}

function opsT_shipRow_(key) {
  return opsT_findRow_('WB_SHIP', key) || opsT_findRow_('OZON_SHIP', key);
}

function opsT_owner_(key) {
  var f = opsT_shipRow_(key);
  return f ? f.sh.getRange(f.row, 4).getValue() : null;
}

function opsT_state_(key) {
  return opsReadState_(SpreadsheetApp.getActive()).rows.filter(function (s) { return s.business_key === key; })[0] || null;
}

/** Правка владельца: значение в ячейку + тот же обработчик, что вызывает Google при ручном вводе. */
function opsT_edit_(key, value) {
  var f = opsT_shipRow_(key);
  if (!f) throw new Error('тест: строки ' + key + ' нет в блоке отгрузки');
  var rg = f.sh.getRange(f.row, 4);
  rg.setValue(value);
  opsOnEditInstalled({ range: rg, value: value });
}

/** Хеш всех значений книги, кроме строк статуса (статус при ошибке меняется намеренно). */
function opsT_digest_() {
  var ss = SpreadsheetApp.getActive(), parts = [];
  OPS_WRITE_ORDER.concat([OPS_SHEET.STATE]).forEach(function (name) {
    var sh = ss.getSheetByName(name), vals = sh.getDataRange().getValues();
    if (name === OPS_SHEET.PLAN || name === OPS_SHEET.SETTINGS) {
      var col = OPS_LAYOUT[name].sysCol - 1;
      vals = vals.map(function (row) {
        var m = String(row[col] || '');
        return m === '@V:STATUS_LINE' || m.indexOf('@R:REFRESH_STATUS:') === 0 ? ['<статус>'] : row;
      });
    }
    parts.push(name + '=' + JSON.stringify(vals));
  });
  return opsDigestHex_(parts.join('\n'));
}

/** Оформление статичных строк (полосы, шапки, итоги, служебные) и контракт колонок — для теста 9. */
function opsT_format_() {
  var ss = SpreadsheetApp.getActive(), out = {};
  OPS_WRITE_ORDER.forEach(function (name) {
    var sh = ss.getSheetByName(name), spec = opsSpecFor_(name), grid = spec.dynamicGrid ? 3 : spec.grid;
    var marks = opsReadMarkers_(sh, spec.sysCol), merges = sh.getRange(1, 1, marks.length, grid).getMergedRanges();
    var s = { hidden: sh.isSheetHidden(), frozen: [sh.getFrozenRows(), sh.getFrozenColumns()], widths: [],
      sysHidden: sh.isColumnHiddenByUser(spec.sysCol), rows: {}, dataRowMerges: {}, ownerBg: {} };
    for (var c = 1; c <= grid; c++) s.widths.push(sh.getColumnWidth(c));
    var seen = {};
    marks.forEach(function (m, i) {
      var t = String(m), r = i + 1;
      var rowMerges = merges.filter(function (mr) { return mr.getRow() === r; })
        .map(function (mr) { return mr.getColumn() + '-' + mr.getLastColumn(); }).sort().join(',');
      if (/^@R:/.test(t)) {
        var id = t.split(':')[1];
        s.dataRowMerges[id + '|' + rowMerges] = true;
        if (name === OPS_SHEET.PLAN && (id === 'WB_SHIP' || id === 'OZON_SHIP')) s.ownerBg[sh.getRange(r, 4).getBackground()] = true;
        return;
      }
      if (!/^@(S|L|B|H|T|V|END)/.test(t)) return;
      seen[t] = (seen[t] || 0) + 1;
      var rg = sh.getRange(r, 1, 1, grid);
      s.rows[t + '#' + seen[t]] = { h: sh.getRowHeight(r), notes: rg.getNotes()[0].join('¦'), bg: rg.getBackgrounds()[0].join(','),
        fw: rg.getFontWeights()[0].join(','), merges: rowMerges,
        values: /^@(H|S|L)/.test(t) && name !== OPS_SHEET.DATA ? rg.getValues()[0].join('¦') : '' };
    });
    out[name] = s;
  });
  return out;
}

function opsT_diff_(a, b) {
  var diffs = [];
  Object.keys(a).forEach(function (sheet) {
    ['hidden', 'frozen', 'widths', 'sysHidden', 'dataRowMerges', 'ownerBg'].forEach(function (k) {
      if (JSON.stringify(a[sheet][k]) !== JSON.stringify(b[sheet][k])) diffs.push(sheet + '.' + k);
    });
    var rows = {};
    Object.keys(a[sheet].rows).concat(Object.keys(b[sheet].rows)).forEach(function (k) { rows[k] = true; });
    Object.keys(rows).forEach(function (k) {
      if (JSON.stringify(a[sheet].rows[k]) !== JSON.stringify(b[sheet].rows[k])) diffs.push(sheet + '.' + k);
    });
  });
  return diffs;
}

function opsT_okRun_(run) {
  return run.st === 'OK' || run.st === 'WARNING';
}

// ───────────────────────────── тесты ─────────────────────────────

function opsTest01_normal() {
  opsT_guard_(); opsT_clear_(); opsT_cache_(false);
  var run = opsRefresh_('TEST', { force: true });
  var ss = SpreadsheetApp.getActive(), snap = opsLoadSnapshot_('OPS_SNAP');
  if (!snap) return opsT_result_('T01', false, { st: run.st, err: run.err });
  var v = snap.views, plan = v.V_OPS_SHEET_PLAN.rows, layout = opsValidateLayouts_(ss, {});
  var count = function (sheet, id) { var b = layout[sheet].blocks[id]; return b.empty ? 0 : b.lastRow - b.firstRow + 1; };
  var of = function (b) {
    return plan.filter(function (r) { return r.sheet_block === b; }).sort(function (x, y) { return x.block_order - y.block_order; });
  };
  var wb = of('WB_SHIP'), sh = ss.getSheetByName(OPS_SHEET.PLAN), b = layout[OPS_SHEET.PLAN].blocks.WB_SHIP;
  var c = {
    wbRows: [count(OPS_SHEET.PLAN, 'WB_SHIP'), wb.length],
    ozRows: [count(OPS_SHEET.PLAN, 'OZON_SHIP'), of('OZON_SHIP').length],
    holdRows: [count(OPS_SHEET.PLAN, 'HOLD'), of('HOLD').length],
    calcWb: [count(OPS_SHEET.CALC, 'CALC_WB'), plan.filter(function (r) { return r.channel === 'WB'; }).length],
    firstWbName: [sh.getRange(b.firstRow, 1).getValue(), wb[0].product_name],
    firstWbRec: [sh.getRange(b.firstRow, 2).getValue(), wb[0].rec_final],
    usendWb: [sh.getRange(layout[OPS_SHEET.PLAN].blocks.USEND_TOP.firstRow + 2, 2).getValue(), plan[0].wb_ship_positions],
    ffTotal: [ss.getSheetByName(OPS_SHEET.FF).getRange(layout[OPS_SHEET.FF].blocks.FF_STOCK.totalRow, 11).getValue(),
      v.V_OPS_SHEET_FF_STOCK.rows[0].t_total_physical],
    dataPlan: [count(OPS_SHEET.DATA, 'D_V_OPS_SHEET_PLAN'), plan.length]
  };
  var pairsOk = Object.keys(c).every(function (k) { return c[k][0] === c[k][1]; });
  c.fingerprintHelper = plan.every(function (r) { return opsT_fingerprint_(r) === r.rec_fingerprint; });
  c.st = run.st;
  opsSaveSnapshot_('OPS_TSNAP', snap);
  return opsT_result_('T01', opsT_okRun_(run) && pairsOk && c.fingerprintHelper, c);
}

function opsTest02_approvalPersists() {
  opsT_guard_(); opsT_clear_(); opsT_cache_(true);
  var K = opsT_keys_();
  opsT_edit_(K.A, 777);
  var s1 = opsT_state_(K.A);
  var run = opsRefresh_('TEST', { force: true });
  var s2 = opsT_state_(K.A), shown = opsT_owner_(K.A);
  var pass = opsT_okRun_(run) && s1.approval_status === 'APPROVED' && s2.approval_status === 'APPROVED' && shown === 777 &&
    s2.approved_fingerprint === opsT_baseRow_(K.A).rec_fingerprint;
  return opsT_result_('T02', pass, { key: K.A, st: run.st, before: s1.approval_status, after: s2.approval_status, shown: shown });
}

function opsTest03_recommendationChanged() {
  opsT_guard_(); opsT_clear_(); opsT_cache_(true);
  var K = opsT_keys_(), base = opsT_baseRow_(K.A);
  opsT_fault_({ stage: 'afterValidate', type: 'SET_REC', key: K.A, rec_final: base.rec_final + 10, once: true });
  var run = opsRefresh_('TEST', { force: true }), s = opsT_state_(K.A), shown = opsT_owner_(K.A);
  var pass = opsT_okRun_(run) && s.approval_status === 'REAPPROVAL_REQUIRED' && s.previous_approved_qty === 777 &&
    s.approved_fingerprint === '' && s.invalidation_reason === 'FINGERPRINT_CHANGED' && shown === OPS_TEXT.REAPPROVAL_SHORT + 777;
  return opsT_result_('T03', pass, { st: run.st, status: s.approval_status, reason: s.invalidation_reason, shown: shown });
}

function opsTest04_rowReorder() {
  opsT_guard_(); opsT_clear_(); opsT_cache_(true);
  var K = opsT_keys_();
  opsRefresh_('TEST', { force: true });
  opsT_edit_(K.B, 555);
  var before = opsT_shipRow_(K.B).row;
  opsT_fault_({ stage: 'afterValidate', type: 'REORDER', keyA: K.A, keyB: K.B, once: true });
  var run = opsRefresh_('TEST', { force: true });
  var after = opsT_shipRow_(K.B).row, sh = opsT_shipRow_(K.B).sh;
  var pass = opsT_okRun_(run) && after !== before && sh.getRange(after, 4).getValue() === 555 &&
    sh.getRange(before, 4).getValue() !== 555 && opsT_state_(K.B).approval_status === 'APPROVED';
  return opsT_result_('T04', pass, { st: run.st, rowBefore: before, rowAfter: after, oldRowShows: sh.getRange(before, 4).getValue() });
}

function opsT_failClosed_(id, setup, expectCode) {
  opsT_guard_(); opsT_clear_();
  setup();
  var d0 = opsT_digest_();
  var run = opsRefresh_('TEST', { force: true });
  var d1 = opsT_digest_();
  var line = opsSheet_(SpreadsheetApp.getActive(), OPS_SHEET.PLAN)
    .getRange(opsValidateLayouts_(SpreadsheetApp.getActive(), {})[OPS_SHEET.PLAN].blocks.STATUS_LINE.valueRow, 1).getValue();
  var pass = run.st === 'FAILED_CLOSED' && d0 === d1 && run.err.indexOf(expectCode) >= 0 && /^ОШИБКА ОБНОВЛЕНИЯ/.test(line);
  var r = opsT_result_(id, pass, { st: run.st, unchanged: d0 === d1, err: run.err.slice(0, 160), statusLine: String(line).slice(0, 90) });
  opsT_cache_(true);
  opsRefresh_('TEST', { force: true });   // вернуть статус OK для следующих тестов
  return r;
}

function opsTest05_bigQueryFailure() {
  return opsT_failClosed_('T05', function () {
    opsT_cache_(false);
    opsT_props_().setProperty('OPS_TEST_VIEW_SUFFIX', '_C2_TEST_MISSING');
  }, 'BQ_QUERY');
}

function opsTest06_schemaFailure() {
  return opsT_failClosed_('T06', function () {
    opsT_cache_(true);
    opsT_fault_({ stage: 'afterFetch', type: 'DROP_COLUMN', view: 'V_OPS_SHEET_PLAN', column: 'rec_fingerprint', once: true });
  }, 'SCHEMA');
}

function opsTest07_duplicateKey() {
  return opsT_failClosed_('T07', function () {
    opsT_cache_(true);
    opsT_fault_({ stage: 'afterFetch', type: 'DUP_KEY', view: 'V_OPS_SHEET_PLAN', once: true });
  }, 'DUP_KEY');
}

function opsTest08a_startLockHolder() {
  opsT_guard_(); opsT_clear_();
  ScriptApp.newTrigger('opsT_lockHolder').timeBased().after(1000).create();
  console.log('Держатель блокировки запланирован. Через 30–90 с запустите opsTest08b_refreshWhileLocked.');
}

function opsT_lockHolder() {
  opsT_guard_();
  var lock = LockService.getScriptLock(), props = opsT_props_();
  if (!lock.tryLock(60000)) return;
  try {
    props.setProperty('OPS_TEST_LOCK_HELD', String(Date.now()));
    Utilities.sleep(180000);
  } finally {
    props.deleteProperty('OPS_TEST_LOCK_HELD');
    lock.releaseLock();
    ScriptApp.getProjectTriggers().forEach(function (t) {
      if (t.getHandlerFunction() === 'opsT_lockHolder') ScriptApp.deleteTrigger(t);
    });
  }
}

function opsTest08b_refreshWhileLocked() {
  opsT_guard_(); opsT_clear_(); opsT_cache_(true);
  if (!opsT_props_().getProperty('OPS_TEST_LOCK_HELD')) {
    console.log('Блокировка ещё не взята — подождите и запустите снова.');
    return null;
  }
  var d0 = opsT_digest_(), run = opsRefresh_('TEST', { force: true }), d1 = opsT_digest_();
  var log = JSON.parse(opsT_props_().getProperty('OPS_RUNLOG') || '[]');
  return opsT_result_('T08', run.st === 'SKIPPED_LOCKED' && d0 === d1 && log[0].st === 'SKIPPED_LOCKED',
    { st: run.st, unchanged: d0 === d1, logged: log[0].st });
}

function opsTest09_formattingPreserved() {
  opsT_guard_(); opsT_clear_(); opsT_cache_(true);
  var K = opsT_keys_();
  opsRefresh_('TEST', { force: true });
  var f0 = opsT_format_();
  opsT_fault_({ stage: 'afterValidate', type: 'TO_HOLD', key: K.C, once: true });
  var r1 = opsRefresh_('TEST', { force: true });   // WB −1 строка, HOLD +1 строка
  var f1 = opsT_format_();
  var inHold = !!opsT_findRow_('HOLD', K.C);
  var r2 = opsRefresh_('TEST', { force: true });   // обратно
  var f2 = opsT_format_();
  var d01 = opsT_diff_(f0, f1), d02 = opsT_diff_(f0, f2);
  var pass = opsT_okRun_(r1) && opsT_okRun_(r2) && inHold && d01.length === 0 && d02.length === 0;
  return opsT_result_('T09', pass, { movedToHold: inHold, diffAfterShrink: d01.slice(0, 8), diffAfterRestore: d02.slice(0, 8) });
}

function opsTest10_idempotent() {
  opsT_guard_(); opsT_clear_(); opsT_cache_(true);
  var r1 = opsRefresh_('TEST');
  var d1 = opsT_digest_(), f1 = opsT_format_();
  var r2 = opsRefresh_('TEST');
  var d2 = opsT_digest_(), f2 = opsT_format_();
  var pass = /NO_CHANGE$/.test(r2.st) && d1 === d2 && opsT_diff_(f1, f2).length === 0;
  return opsT_result_('T10', pass, { first: r1.st, second: r2.st, sameValues: d1 === d2 });
}

function opsTest11_displayNameChange() {
  opsT_guard_(); opsT_clear_(); opsT_cache_(true);
  var K = opsT_keys_(), name = 'ТЕСТ C2 · новое название';
  opsT_fault_({ stage: 'afterValidate', type: 'RENAME', key: K.B, name: name, once: false });
  var run = opsRefresh_('TEST', { force: true });
  var f = opsT_shipRow_(K.B), shownName = f.sh.getRange(f.row, 1).getValue(), shown = f.sh.getRange(f.row, 4).getValue();
  var s = opsT_state_(K.B);
  opsT_clear_();
  opsRefresh_('TEST', { force: true });
  var pass = opsT_okRun_(run) && shownName === name && shown === 555 && s.approval_status === 'APPROVED';
  return opsT_result_('T11', pass, { st: run.st, shownName: shownName, shown: shown, status: s.approval_status });
}

function opsTest12_disappearAndReturn() {
  opsT_guard_(); opsT_clear_(); opsT_cache_(true);
  var K = opsT_keys_();
  opsT_edit_(K.C, 321);
  var s0 = opsT_state_(K.C);
  opsT_fault_({ stage: 'afterValidate', type: 'TO_HOLD', key: K.C, once: false });
  var r1 = opsRefresh_('TEST', { force: true });
  var s1 = opsT_state_(K.C), inHold = !!opsT_findRow_('HOLD', K.C), inShip = !!opsT_findRow_('WB_SHIP', K.C);
  opsT_clear_();
  var r2 = opsRefresh_('TEST', { force: true });
  var s2 = opsT_state_(K.C), shown = opsT_owner_(K.C);
  var pass = opsT_okRun_(r1) && opsT_okRun_(r2) && s0.approval_status === 'APPROVED' && s1.approval_status === 'INACTIVE' &&
    s1.invalidation_reason === 'LEFT_SHIP_BLOCK' && inHold && !inShip && s2.approval_status === 'REAPPROVAL_REQUIRED' &&
    s2.approved_fingerprint === '' && s2.current_fingerprint === opsT_baseRow_(K.C).rec_fingerprint &&
    shown === OPS_TEXT.REAPPROVAL_SHORT + 321;
  return opsT_result_('T12', pass, { away: s1.approval_status, back: s2.approval_status, shown: shown });
}

function opsTest13_changeAndReturn() {
  opsT_guard_(); opsT_clear_(); opsT_cache_(true);
  var K = opsT_keys_(), base = opsT_baseRow_(K.D);
  opsT_edit_(K.D, 100);
  opsT_fault_({ stage: 'afterValidate', type: 'SET_REC', key: K.D, rec_final: base.rec_final + 20, once: false });
  var r1 = opsRefresh_('TEST', { force: true }), s1 = opsT_state_(K.D);
  opsT_clear_();
  var r2 = opsRefresh_('TEST', { force: true }), s2 = opsT_state_(K.D), shown2 = opsT_owner_(K.D);
  opsT_edit_(K.D, 100);                             // новое явное подтверждение текущей рекомендации
  var r3 = opsRefresh_('TEST', { force: true }), s3 = opsT_state_(K.D), shown3 = opsT_owner_(K.D);
  var pass = opsT_okRun_(r1) && opsT_okRun_(r2) && opsT_okRun_(r3) &&
    s1.approval_status === 'REAPPROVAL_REQUIRED' && s2.approval_status === 'REAPPROVAL_REQUIRED' &&
    s2.current_fingerprint === base.rec_fingerprint && s2.approved_fingerprint === '' && s2.previous_approved_qty === 100 &&
    shown2 === OPS_TEXT.REAPPROVAL_SHORT + 100 && s3.approval_status === 'APPROVED' && shown3 === 100 &&
    s3.approved_fingerprint === base.rec_fingerprint;
  return opsT_result_('T13', pass, { changed: s1.approval_status, returned: s2.approval_status, reapproved: s3.approval_status });
}

function opsTest14_partialWriteRecovery() {
  opsT_guard_(); opsT_clear_(); opsT_cache_(true);
  opsT_fault_({ stage: 'afterBlock', type: 'WRITE_FAIL', after: 5, once: true });
  var r1 = opsRefresh_('TEST', { force: true });
  var pending1 = opsT_props_().getProperty('OPS_WRITE_IN_PROGRESS');
  // свежие данные недоступны → восстановление из последнего успешного снимка
  opsT_cache_(false);
  opsT_props_().setProperty('OPS_TEST_VIEW_SUFFIX', '_C2_TEST_MISSING');
  var r2 = opsRefresh_('TEST');
  var pending2 = opsT_props_().getProperty('OPS_WRITE_IN_PROGRESS');
  opsT_clear_(); opsT_cache_(true);
  var r3 = opsRefresh_('TEST');
  var layoutOk = true;
  try { opsValidateLayouts_(SpreadsheetApp.getActive(), {}); } catch (e) { layoutOk = false; }
  // после восстановления из снимка те же данные → штатный путь «без изменений» (пишется только статус)
  var pass = r1.st === 'WRITE_FAILED' && !!pending1 && r2.st === 'RESTORED_LAST_GOOD' && !pending2 &&
    /^(OK|WARNING)(_NO_CHANGE)?$/.test(r3.st) && layoutOk &&
    !opsT_props_().getProperty('OPS_WRITE_IN_PROGRESS');
  return opsT_result_('T14', pass, { writeFailed: r1.st, restored: r2.st, next: r3.st, layoutOk: layoutOk });
}

/** Визуальная проверка: реальная ошибка BigQuery, данные на экране не меняются, статус — ОШИБКА. */
function opsTestShowError() {
  opsT_guard_(); opsT_clear_(); opsT_cache_(false);
  opsT_props_().setProperty('OPS_TEST_VIEW_SUFFIX', '_C2_TEST_MISSING');
  var run = opsRefresh_('TEST', { force: true });
  opsT_clear_();
  opsT_toast_('Визуальная проверка: ' + run.st + ' — данные на экране не менялись. Вернуть: EVETIS OPERATIONS → Обновить данные.');
  return run;
}

/** Визуальная проверка: источники помечены устаревшими — запись идёт, статус ВНИМАНИЕ. */
function opsTestShowWarning() {
  opsT_guard_(); opsT_clear_(); opsT_cache_(true);
  opsT_fault_({ stage: 'afterFetch', type: 'STALE', once: true });
  var run = opsRefresh_('TEST', { force: true });
  opsT_toast_('Визуальная проверка: ' + run.st + '. Вернуть: EVETIS OPERATIONS → Обновить данные.');
  return run;
}

function opsTestSummary() {
  opsT_guard_();
  var all = JSON.parse(opsT_props_().getProperty('OPS_TEST_RESULTS') || '{}');
  Object.keys(all).sort().forEach(function (k) { console.log(k + ' · ' + all[k]); });
  return all;
}
