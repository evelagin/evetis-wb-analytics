/** Read-only диагностика состояния E6-A: свойства E6_* и живые счётчики правил УФ.
 * Лёгкий запрос к Sheets API (только ranges), книгу не меняет; результат — в журнал и e6_state_<HHmm>.json.
 * 14.09 02:05 МСК: e6cfRules завершился MISMATCH (OZON 1183 → 13). Добавлен read-only дамп оставшихся
 * правил OZON (полные тела только этого листа через параметр ranges; при ≤ 50 правил) и сверка со
 * snap_cf.json → e6_ozon_cf_remaining.json. Книгу по-прежнему не меняет. */
function e6state() {
  var all = PropertiesService.getScriptProperties().getProperties();
  var props = {}; Object.keys(all).filter(function (k) { return k.indexOf('E6_') === 0; }).sort().forEach(function (k) { props[k] = all[k]; });
  var meta = Sheets.Spreadsheets.get(ss_().getId(), { fields: 'sheets(properties(title),conditionalFormats(ranges(sheetId)))' });
  var cnt = {}; meta.sheets.forEach(function (s) { var rs = s.conditionalFormats || []; var parts = 0; rs.forEach(function (r) { parts += (r.ranges || []).length; }); cnt[s.properties.title] = { rules: rs.length, parts: parts }; });
  var msg = 'state: ' + JSON.stringify(props) + ' | CF ' + JSON.stringify(cnt);
  Logger.log(msg);
  saveJson_('e6_state_' + Utilities.formatDate(new Date(), 'Europe/Moscow', 'HHmm') + '.json', { at: new Date().toISOString(), props: props, cf: cnt });
  var oz = cnt[E6.OZON];
  if (oz && oz.rules > 0 && oz.rules <= 50) {
    // без параметра ranges: с ним API отдал 0 правил (02:09), хотя счётчик по книге показывает 13 —
    // после e6cf полный ответ по всем листам мал (152 + 93 + остаток), Out of memory не грозит
    var full = Sheets.Spreadsheets.get(ss_().getId(), { fields: 'sheets(properties(sheetId,title),conditionalFormats)' });
    var sh = {}; (full.sheets || []).forEach(function (s) { if (s.properties.title === E6.OZON) sh = s; }); var rules = sh.conditionalFormats || [];
    full = null;
    var snap = loadJson_('snap_cf.json'); var snapOz = (snap[E6.OZON] && snap[E6.OZON].rules) || [];
    var idx = {}; snapOz.forEach(function (r, i) { var k = canon_(r); if (!idx[k]) idx[k] = []; idx[k].push(i); });
    snap = null;
    var match = rules.map(function (r) { var k = canon_(r); return idx[k] ? idx[k] : null; });
    var out = { at: new Date().toISOString(), sheet: sh.properties, snapRules: snapOz.length, liveRules: rules.length, snapIndexOfLive: match, rules: rules };
    saveJson_('e6_ozon_cf_remaining.json', out);
    var m2 = 'OZON остаток: ' + rules.length + ' правил; индексы в snap_cf: ' + JSON.stringify(match) + '; типы: ' + rules.map(function (r) { return r.booleanRule ? 'B:' + (((r.booleanRule.condition || {}).type) || '') : (r.gradientRule ? 'G' : '?'); }).join(',');
    Logger.log(m2); msg += ' | ' + m2;
  }
  // read-only сверка живых правил главного листа с утверждённым планом (e6_plan_live.json → target), по порядку
  var mainCnt = cnt[E6.MAIN];
  if (mainCnt && mainCnt.rules > 0 && mainCnt.rules <= 300) {
    var full2 = Sheets.Spreadsheets.get(ss_().getId(), { fields: 'sheets(properties(sheetId,title),conditionalFormats)' });
    var mainSh = {}; (full2.sheets || []).forEach(function (s) { if (s.properties.title === E6.MAIN) mainSh = s; }); var liveMain = mainSh.conditionalFormats || [];
    full2 = null;
    var plan = loadJson_('e6_plan_live.json'); var target = plan.target || []; plan = null;
    var strip = function (o) { if (o === null || typeof o !== 'object') return o; if (Array.isArray(o)) return o.map(strip); var r = {}; Object.keys(o).forEach(function (k) { if (k !== 'colorStyle') r[k] = strip(o[k]); }); return r; };
    var strict = 0, loose = 0, bad = [];
    for (var i = 0; i < Math.max(liveMain.length, target.length); i++) {
      var a = liveMain[i], b = target[i];
      if (a && b && canon_(a) === canon_(b)) { strict++; loose++; }
      else if (a && b && canon_(strip(a)) === canon_(strip(b))) loose++;
      else bad.push(i);
    }
    var m3 = 'главный vs план: live ' + liveMain.length + ' / target ' + target.length + '; совпали строго ' + strict + ', без colorStyle ' + loose + '; расхождения по индексам: ' + JSON.stringify(bad.slice(0, 50));
    saveJson_('e6_main_cf_vs_plan.json', { at: new Date().toISOString(), live: liveMain.length, target: target.length, strict: strict, loose: loose, badIdx: bad, badLive: bad.slice(0, 20).map(function (i) { return liveMain[i] || null; }), badTarget: bad.slice(0, 20).map(function (i) { return target[i] || null; }) });
    Logger.log(m3); msg += ' | ' + m3;
  }
  // read-only: ошибки формул на главном листе сейчас vs снимок (какие появились / исчезли)
  if (props['E6_QA1_AT']) {
    var main = ss_().getSheetByName(E6.MAIN); var v0 = loadJson_('snap_values.json')[E6.MAIN];
    var r = v0.length, c = v0[0].length; var v1 = main.getRange(1, 1, r, c).getValues();
    var f1 = main.getRange(1, 1, r, c).getFormulas();
    var isErr = function (x) { return typeof x === 'string' && x.charAt(0) === '#'; };
    var added = [], removed = [], kept = 0;
    for (var i = 0; i < r; i++) for (var j = 0; j < c; j++) {
      var e0 = isErr(v0[i][j]), e1 = isErr(v1[i][j]);
      if (e0 && e1) kept++;
      else if (e1) added.push([a1_(i + 1, j + 1), String(v1[i][j]), String(f1[i][j]).slice(0, 160), String(v0[i][j]).slice(0, 40)]);
      else if (e0) removed.push([a1_(i + 1, j + 1), String(v0[i][j]), String(v1[i][j]).slice(0, 40)]);
    }
    v0 = null; v1 = null; f1 = null;
    var m4 = 'ошибки главного: было ' + (kept + removed.length) + ', стало ' + (kept + added.length) + '; новые ' + added.length + ' ' + JSON.stringify(added.slice(0, 12)) + '; исчезли ' + removed.length + ' ' + JSON.stringify(removed.slice(0, 12).map(function (x) { return x[0]; }));
    saveJson_('e6_main_errors_diff.json', { at: new Date().toISOString(), kept: kept, added: added, removed: removed });
    Logger.log(m4); msg += ' | ' + m4;
  }
  return msg;
}
