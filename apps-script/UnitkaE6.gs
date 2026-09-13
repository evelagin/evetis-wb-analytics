/**
 * UNITKA — Stage E6-A (E6.1–E6.5). Bound Apps Script книги «Юнитка_Evetis Cosmetics».
 * НЕ основной проект evetis-wb-analytics. Требует Advanced Service «Google Sheets API» (v4).
 *
 * Порядок (каждая функция — отдельный запуск из редактора; состояние — ScriptProperties, журнал — ZZ_AUDIT_LOG):
 *   e6backup      — полная копия книги (Drive) → E6_BACKUP_ID
 *   e6snapshot    — снимки для QA и отката: значения/формулы всех листов, фон+цвет шрифта (отрисованные),
 *                   статические фоны (userEnteredFormat), все правила УФ (JSON Sheets API) → Drive-папка UNITKA_E6_<дата>
 *   e6dryrun      — план УФ по ЖИВЫМ правилам (без записи) → e6_plan_live.json + сводка в журнал
 *   e6cf          — E6.1: печь фон истории/2024/OZON, удалить все правила, добавить целевые (главный лист)
 *   e6freeze      — E6.2/E6.3: «Акции», «OZON_Юнит_2025» → значения, скрыть
 *   e6fix         — E6.5: F1 (Блогеры из резервной копии, D2), F2 (DP293), F3 (BD663/CB663), F4 (JX760), F5 (сводка июня)
 *   e6trim        — E6.4: удалить пустые колонки/строки
 *   e6qa          — гейты: VALUE/FORMULA/CF-VISUAL REGRESSION, FORMULA_ERRORS, счётчики УФ, STRUCTURE
 *   e6rollback    — откат по снимкам (порядок обратный: trim → fix → freeze → cf)
 *
 * Правила владельца (13.09.2026): D1 = V1 (семантика УФ 1:1 по SKU/месяцу, домены не объединять),
 * D2 (Блогеры только из резервной копии, иначе 0 + ISSUE), D3 (DP293 → пусто + ISSUE), D4 (TD765/TD730 не трогать).
 * Бизнес-значения не меняются, кроме F1–F5. Никаких правок вне этого файла.
 */

var E6 = {
  MAIN: 'WB_Юнит_2025',
  Y2024: 'WB_Юнит_2024',
  OZON: 'OZON_Юнит_2025',
  AKCII: 'Акции',
  SKLAD: 'Склад',
  LOG: 'ZZ_AUDIT_LOG',
  SEP_TOP: 735, SEP_FIRST: 737, SEP_LAST: 766, SEP_END: 768,
  HIST_END: 734,            // последняя строка истории (август 2026)
  MAIN_LAST_COL: 600,       // WB — зеркало LCD; дальше пусто
  BACKUP_SRC: '1H8ldcUNpMbkceVzfHGTNwdMLqdaNVzP8JJ1qK_i3Z6s', // «…_BACKUP_до_аудита» 10.09 14:58 UTC
  BLOGGERS: 'Блогеры',
  TRIM_MARGIN: 50,
  // Секции истории (строки шапка..план) — из аудита E5; строки 1–46 — сводка/поставки.
  SECTIONS: [[1, 46], [47, 81], [82, 113], [114, 148], [149, 182], [183, 217], [218, 251], [252, 286], [287, 321], [322, 355],
             [356, 390], [391, 424], [425, 459], [460, 494], [495, 526], [527, 561], [562, 595], [596, 630], [631, 664], [665, 699], [700, 734]],
  // F2–F5 (F1 — восстановление листа, F6 — не трогаем)
  FIXES: [
    { id: 'F2', a1: 'DP293', formula: null, value: '', issue: 'ADS_MISSING 2025-08-05 305101361' },
    { id: 'F3', a1: 'BD663', formula: '=BD633*AO663' },
    { id: 'F3', a1: 'CB663', formula: '=CB633*BM663' },
    { id: 'F4', a1: 'JX760', formula: '=JX759-JU760+JW759' },
  ],
  JUNE: { first: 633, last: 662, cols: 'CDEFGHIJ', repl: [[':SX', ':UT'], [':SY', ':UU'], [':SZ', ':UV'], [':TA', ':UW'], [':TB', ':UX'], [':TC', ':UY'], [':TG', ':VC'], [':TH', ':VD']] },
};

// ───────────────────────────── инфраструктура ─────────────────────────────
function ss_() { return SpreadsheetApp.getActive(); }
function props_() { return PropertiesService.getScriptProperties(); }
function log_(step, msg) {
  var sh = ss_().getSheetByName(E6.LOG);
  if (sh) sh.appendRow([new Date(), 'E6-A', step, String(msg).slice(0, 4000)]);
  Logger.log('[' + step + '] ' + msg);
}
function folder_() {
  var id = props_().getProperty('E6_FOLDER_ID');
  if (id) return DriveApp.getFolderById(id);
  var f = DriveApp.createFolder('UNITKA_E6_' + Utilities.formatDate(new Date(), 'Europe/Moscow', 'yyyy-MM-dd_HHmm'));
  props_().setProperty('E6_FOLDER_ID', f.getId());
  return f;
}
function saveJson_(name, obj) {
  var f = folder_();
  var it = f.getFilesByName(name);
  while (it.hasNext()) it.next().setTrashed(true);
  var file = f.createFile(name, JSON.stringify(obj), 'application/json');
  return file.getId();
}
function loadJson_(name) {
  var it = folder_().getFilesByName(name);
  if (!it.hasNext()) throw new Error('Нет снимка ' + name + ' — сначала e6snapshot');
  return JSON.parse(it.next().getBlob().getDataAsString());
}
function sheetId_(name) { return ss_().getSheetByName(name).getSheetId(); }
function a1_(r, c) { return colA1_(c) + r; }
function colA1_(c) { var s = ''; while (c > 0) { var m = (c - 1) % 26; s = String.fromCharCode(65 + m) + s; c = (c - 1 - m) / 26; } return s; }
function colIdx_(a) { var n = 0; for (var i = 0; i < a.length; i++) n = n * 26 + (a.charCodeAt(i) - 64); return n; }

// ───────────────────────────── 0. backup ─────────────────────────────
function e6backup() {
  var src = DriveApp.getFileById(ss_().getId());
  var name = 'Юнитка_Evetis Cosmetics_BACKUP_до_E6_' + Utilities.formatDate(new Date(), 'Europe/Moscow', 'yyyy-MM-dd_HHmm');
  var copy = src.makeCopy(name);
  props_().setProperty('E6_BACKUP_ID', copy.getId());
  log_('backup', 'копия книги: ' + name + ' id=' + copy.getId());
}

// ───────────────────────────── 1. snapshot ─────────────────────────────
function e6snapshot() {
  var ss = ss_();
  var t0 = Date.now();
  var sheets = ss.getSheets();
  var vals = {}, forms = {}, dims = {};
  sheets.forEach(function (sh) {
    var r = sh.getLastRow(), c = sh.getLastColumn();
    dims[sh.getName()] = { lastRow: r, lastCol: c, maxRows: sh.getMaxRows(), maxCols: sh.getMaxColumns(), hidden: sh.isSheetHidden() };
    if (r === 0 || c === 0) { vals[sh.getName()] = []; forms[sh.getName()] = []; return; }
    var rg = sh.getRange(1, 1, r, c);
    vals[sh.getName()] = rg.getValues();
    forms[sh.getName()] = rg.getFormulas();
  });
  saveJson_('snap_values.json', vals);
  saveJson_('snap_formulas.json', forms);
  saveJson_('snap_dims.json', dims);
  log_('snapshot', 'значения/формулы ' + sheets.length + ' листов за ' + ((Date.now() - t0) / 1000) + ' с');
  // отрисованные фон/шрифт — только листы с УФ (главный, 2024, OZON, Склад)
  var vis = {};
  [E6.MAIN, E6.Y2024, E6.OZON, E6.SKLAD].forEach(function (n) {
    var sh = ss.getSheetByName(n); if (!sh) return;
    var r = sh.getLastRow(), c = Math.min(sh.getLastColumn(), n === E6.MAIN ? E6.MAIN_LAST_COL : sh.getLastColumn());
    var rg = sh.getRange(1, 1, r, c);
    vis[n] = { rows: r, cols: c, bg: rg.getBackgrounds(), fg: rg.getFontColors() };
  });
  saveJson_('snap_visual.json', vis);
  log_('snapshot', 'отрисованные фон/шрифт за ' + ((Date.now() - t0) / 1000) + ' с');
  // правила УФ — Sheets API (порядок = приоритет)
  var meta = Sheets.Spreadsheets.get(ss.getId(), { fields: 'sheets(properties(sheetId,title),conditionalFormats)' });
  var cf = {};
  meta.sheets.forEach(function (s) { cf[s.properties.title] = { sheetId: s.properties.sheetId, rules: s.conditionalFormats || [] }; });
  saveJson_('snap_cf.json', cf);
  var tot = 0; Object.keys(cf).forEach(function (k) { tot += cf[k].rules.length; });
  log_('snapshot', 'правил УФ: ' + tot + ' (' + Object.keys(cf).map(function (k) { return k + '=' + cf[k].rules.length; }).join(', ') + ')');
  // статические фоны (userEnteredFormat) — для отката печёных фонов; по секциям главного листа + 2024 + OZON
  var stat = {};
  [E6.MAIN, E6.Y2024, E6.OZON].forEach(function (n) { stat[n] = staticBackgrounds_(n); });
  saveJson_('snap_static_bg.json', stat);
  props_().setProperty('E6_SNAPSHOT_AT', new Date().toISOString());
  log_('snapshot', 'готово за ' + ((Date.now() - t0) / 1000) + ' с; папка ' + folder_().getName());
}
/** Статический фон (без УФ) через Sheets API includeGridData; null = фона нет. */
function staticBackgrounds_(name) {
  var ss = ss_(); var sh = ss.getSheetByName(name);
  var r = sh.getLastRow(), c = Math.min(sh.getLastColumn(), name === E6.MAIN ? E6.MAIN_LAST_COL : sh.getLastColumn());
  var out = []; var step = 100;
  for (var r0 = 1; r0 <= r; r0 += step) {
    var r1 = Math.min(r, r0 + step - 1);
    var res = Sheets.Spreadsheets.get(ss.getId(), { ranges: ["'" + name + "'!A" + r0 + ':' + colA1_(c) + r1], includeGridData: true, fields: 'sheets.data.rowData.values.userEnteredFormat.backgroundColor' });
    var rows = (res.sheets[0].data[0].rowData) || [];
    for (var i = 0; i < r1 - r0 + 1; i++) {
      var row = []; var vals = (rows[i] && rows[i].values) || [];
      for (var j = 0; j < c; j++) {
        var f = vals[j] && vals[j].userEnteredFormat && vals[j].userEnteredFormat.backgroundColor;
        row.push(f ? hex_(f) : null);
      }
      out.push(row);
    }
  }
  return { rows: r, cols: c, bg: out };
}
function hex_(col) {
  function h(x) { var v = Math.round((x || 0) * 255); return ('0' + v.toString(16)).slice(-2); }
  return '#' + h(col.red) + h(col.green) + h(col.blue);
}

// ───────────────────────────── 2. план УФ (dry-run) ─────────────────────────────
function e6dryrun() {
  var plan = buildCfPlan_(null);
  saveJson_('e6_plan_live.json', plan);
  log_('dryrun', 'главный лист: правил ' + plan.now.rules + ' → ' + plan.after.rules + ', фрагментов ' + plan.now.parts + ' → ' + plan.after.parts +
    '; история удалить ' + plan.history.rules + ' правил; сентябрь: градиентов ' + plan.sep.gradientNow + ' → ' + plan.sep.gradientAfter +
    ', формульных ' + plan.sep.booleanNow + ' → ' + plan.sep.booleanAfter + '; 2024=' + plan.other[E6.Y2024] + ', OZON=' + plan.other[E6.OZON] + ' (bake+delete), Склад=' + plan.other[E6.SKLAD] + ' (keep)');
  return plan;
}
/** Строит целевой набор правил главного листа из ЖИВЫХ правил (или из снимка, если передан). */
function buildCfPlan_(cfSnap) {
  var ss = ss_();
  var cf = cfSnap || (function () {
    var meta = Sheets.Spreadsheets.get(ss.getId(), { fields: 'sheets(properties(sheetId,title),conditionalFormats)' });
    var o = {}; meta.sheets.forEach(function (s) { o[s.properties.title] = { sheetId: s.properties.sheetId, rules: s.conditionalFormats || [] }; }); return o;
  })();
  var main = cf[E6.MAIN]; var rules = main.rules;
  var sepTop0 = E6.SEP_TOP - 1; // 0-based
  function r1(rule) { return Math.min.apply(null, rule.ranges.map(function (g) { return g.startRowIndex || 0; })); }
  function r2(rule) { return Math.max.apply(null, rule.ranges.map(function (g) { return g.endRowIndex || 1000000; })); }
  var hist = [], sep = [];
  rules.forEach(function (rule, i) { (r2(rule) <= sepTop0 ? hist : sep).push({ i: i, rule: rule }); });
  // сентябрь: градиенты — точные дубликаты и полностью затенённые; формульные — группы по шаблону
  var keepG = [], dropG = [], seen = {}, covered = {};
  var boolRules = [];
  sep.forEach(function (x) {
    var rule = x.rule;
    if (rule.gradientRule) {
      var key = JSON.stringify(rule.ranges.map(gr_).sort()) + '|' + JSON.stringify(rule.gradientRule);
      if (seen[key] !== undefined) { dropG.push({ index: x.i, sameAs: seen[key], ranges: rule.ranges.map(gr_) }); return; }
      var cells = []; rule.ranges.forEach(function (g) { for (var c = g.startColumnIndex; c < g.endColumnIndex; c++) for (var r = g.startRowIndex; r < g.endRowIndex; r++) cells.push(c + ':' + r); });
      if (cells.length && cells.every(function (k) { return covered[k]; })) { dropG.push({ index: x.i, sameAs: 'shadowed', ranges: rule.ranges.map(gr_) }); return; }
      cells.forEach(function (k) { covered[k] = true; });
      seen[key] = x.i; keepG.push(rule);
    } else boolRules.push(x);
  });
  var groups = {}, order = [];
  boolRules.forEach(function (x) {
    var f = boolFormula_(x.rule); var t = template_(f) + '|' + JSON.stringify(x.rule.booleanRule.format || {});
    if (!groups[t]) { groups[t] = []; order.push(t); }
    groups[t].push(x);
  });
  // replacement[originalIndex] = правило, которое встаёт на это место (merged — на месте первого правила группы), null — пропустить
  var replacement = {}; var boolPlan = []; var boolAfter = 0;
  order.forEach(function (t) {
    var xs = groups[t]; var rs = xs.map(function (x) { return x.rule; });
    var singleCol = rs.every(function (r) { return r.ranges.every(function (g) { return g.endColumnIndex - g.startColumnIndex === 1; }); });
    var rowKeys = {}; rs.forEach(function (r) { r.ranges.forEach(function (g) { rowKeys[g.startRowIndex + ':' + g.endRowIndex] = 1; }); });
    var sameRows = Object.keys(rowKeys).length === 1;
    var f0 = boolFormula_(rs[0]);
    var absIn = (f0.match(/\$([A-Z]{1,3})\$?\d+/g) || []).filter(function (m) { return m.indexOf('$WB') !== 0; });
    var action = rs.length === 1 ? 'keep' : (singleCol && sameRows ? (absIn.length ? 'merge-relative' : 'merge') : 'keep-per-block');
    if (action.indexOf('merge') === 0) {
      var merged = JSON.parse(JSON.stringify(rs[0]));
      merged.ranges = []; rs.forEach(function (r) { merged.ranges = merged.ranges.concat(r.ranges); });
      if (action === 'merge-relative') merged.booleanRule.condition.values[0].userEnteredValue = relativize_(f0);
      replacement[xs[0].i] = merged; xs.slice(1).forEach(function (x) { replacement[x.i] = null; }); boolAfter += 1;
    } else { xs.forEach(function (x) { replacement[x.i] = x.rule; }); boolAfter += rs.length; }
    boolPlan.push({ template: t.slice(0, 120), rulesNow: rs.length, action: action, rulesAfter: action.indexOf('merge') === 0 ? 1 : rs.length, formula: f0, formulaAfter: action === 'merge-relative' ? relativize_(f0) : f0 });
  });
  // итоговый порядок = исходный порядок правил сентября (приоритеты сохраняются: формульное/градиент — кто был раньше)
  var target = [];
  sep.forEach(function (x) {
    if (x.rule.gradientRule) { if (keepG.indexOf(x.rule) >= 0) target.push(x.rule); }
    else if (replacement[x.i]) target.push(replacement[x.i]);
  });
  var parts = function (list) { var n = 0; list.forEach(function (r) { n += r.ranges.length; }); return n; };
  var other = {}; [E6.Y2024, E6.OZON, E6.SKLAD].forEach(function (n) { other[n] = cf[n] ? cf[n].rules.length : 0; });
  return {
    sheetId: main.sheetId,
    now: { rules: rules.length, parts: parts(rules) },
    history: { rules: hist.length, parts: parts(hist.map(function (x) { return x.rule; })) },
    sep: { gradientNow: keepG.length + dropG.length, gradientAfter: keepG.length, dropped: dropG, booleanNow: boolRules.length, booleanAfter: boolAfter, boolPlan: boolPlan },
    after: { rules: target.length, parts: parts(target) },
    target: target,
    other: other,
    otherSheetIds: { y2024: cf[E6.Y2024] && cf[E6.Y2024].sheetId, ozon: cf[E6.OZON] && cf[E6.OZON].sheetId },
  };
}
function gr_(g) { return [g.startRowIndex || 0, g.endRowIndex, g.startColumnIndex || 0, g.endColumnIndex].join(','); }
function boolFormula_(rule) { var v = rule.booleanRule && rule.booleanRule.condition && rule.booleanRule.condition.values; return (v && v[0] && v[0].userEnteredValue) || ''; }
function template_(f) { return f.replace(/\$?[A-Z]{1,3}\$?\d+/g, 'REF').replace(/\d+(\.\d+)?/g, '#'); }
function relativize_(f) { return f.replace(/\$([A-Z]{1,3})(\$?)(\d+)/g, function (m, col, d, row) { return col === 'WB' ? m : col + d + row; }); }

// ───────────────────────────── 3. E6.1 УФ ─────────────────────────────
function e6cf() {
  var ss = ss_(); var t0 = Date.now();
  var plan = buildCfPlan_(null);
  saveJson_('e6_plan_applied.json', plan);
  // 3a. печь фон: история главного (секции), 2024, OZON — читаем ОТРИСОВАННЫЙ фон до удаления правил
  var main = ss.getSheetByName(E6.MAIN);
  var baked = 0;
  E6.SECTIONS.forEach(function (s) {
    var rg = main.getRange(s[0], 1, s[1] - s[0] + 1, E6.MAIN_LAST_COL);
    var bg = rg.getBackgrounds();
    rg.setBackgrounds(bg); baked += bg.length * bg[0].length;
  });
  [E6.Y2024, E6.OZON].forEach(function (n) {
    var sh = ss.getSheetByName(n); if (!sh) return;
    var rg = sh.getRange(1, 1, sh.getLastRow(), sh.getLastColumn());
    var bg = rg.getBackgrounds(); rg.setBackgrounds(bg); baked += bg.length * bg[0].length;
  });
  SpreadsheetApp.flush();
  log_('cf', 'фон запечён статически: ' + baked + ' ячеек (история главного, 2024, OZON) за ' + ((Date.now() - t0) / 1000) + ' с');
  // 3b. один batchUpdate: удалить все правила главного/2024/OZON, добавить целевые на главный
  var reqs = [];
  function delAll(sheetId, n) { for (var i = 0; i < n; i++) reqs.push({ deleteConditionalFormatRule: { sheetId: sheetId, index: 0 } }); }
  delAll(plan.sheetId, plan.now.rules);
  if (plan.otherSheetIds.y2024 !== undefined) delAll(plan.otherSheetIds.y2024, plan.other[E6.Y2024]);
  if (plan.otherSheetIds.ozon !== undefined) delAll(plan.otherSheetIds.ozon, plan.other[E6.OZON]);
  plan.target.forEach(function (rule, i) { reqs.push({ addConditionalFormatRule: { rule: rule, index: i } }); });
  var chunk = 2000;
  for (var i = 0; i < reqs.length; i += chunk) Sheets.Spreadsheets.batchUpdate({ requests: reqs.slice(i, i + chunk) }, ss.getId());
  props_().setProperty('E6_CF_DONE', new Date().toISOString());
  log_('cf', 'правила: удалено ' + (plan.now.rules + plan.other[E6.Y2024] + plan.other[E6.OZON]) + ', добавлено ' + plan.target.length + ' (фрагментов ' + plan.after.parts + ') за ' + ((Date.now() - t0) / 1000) + ' с');
}

// ───────────────────────────── 4. E6.2/E6.3 заморозка ─────────────────────────────
function e6freeze() {
  var ss = ss_();
  [E6.AKCII, E6.OZON].forEach(function (n) {
    var sh = ss.getSheetByName(n); if (!sh) return;
    var r = sh.getLastRow(), c = sh.getLastColumn();
    var rg = sh.getRange(1, 1, r, c);
    var vals = rg.getValues(); var errs = 0, forms = 0;
    var fs = rg.getFormulas();
    for (var i = 0; i < r; i++) for (var j = 0; j < c; j++) {
      if (fs[i][j]) forms++;
      var v = vals[i][j];
      if (typeof v === 'string' && /^#(DIV\/0!|VALUE!|REF!|NAME\?|N\/A|NUM!|ERROR!)/.test(v)) { vals[i][j] = ''; errs++; }
    }
    rg.setValues(vals);
    if (n === E6.AKCII) sh.getRange('A2').setNote('АРХИВ с ' + Utilities.formatDate(new Date(), 'Europe/Moscow', 'dd.MM.yyyy') + ' (E6.2): формулы заменены значениями, данные по 05.2026; источник цен — BigQuery RAW_WB_PRICES');
    else sh.getRange('A1').setNote('АРХИВ с ' + Utilities.formatDate(new Date(), 'Europe/Moscow', 'dd.MM.yyyy') + ' (E6.3): формулы заменены значениями до Ozon Engine; ' + errs + ' ячеек-ошибок очищено');
    sh.hideSheet();
    log_('freeze', n + ': формул→значений ' + forms + ', ошибок очищено ' + errs + ', лист скрыт');
  });
  props_().setProperty('E6_FREEZE_DONE', new Date().toISOString());
}

// ───────────────────────────── 5. E6.5 правки ─────────────────────────────
function e6fix() {
  var ss = ss_(); var main = ss.getSheetByName(E6.MAIN); var changes = [];
  // F1 — лист «Блогеры» из резервной копии (D2): values-only
  if (!ss.getSheetByName(E6.BLOGGERS)) {
    var src = null;
    try { src = SpreadsheetApp.openById(E6.BACKUP_SRC).getSheetByName(E6.BLOGGERS); } catch (e) { src = null; }
    if (src) {
      var r = src.getLastRow(), c = src.getLastColumn();
      var vals = src.getRange(1, 1, r, c).getValues();
      var sh = ss.insertSheet(E6.BLOGGERS, ss.getSheets().length);
      sh.getRange(1, 1, r, c).setValues(vals);
      sh.getRange('A1').setNote('RESTORED_FROM_BACKUP ' + E6.BACKUP_SRC + ' (10.09.2026 14:58 UTC), values-only, ' + Utilities.formatDate(new Date(), 'Europe/Moscow', 'dd.MM.yyyy HH:mm') + ' — историческая реконструкция (E6.5 F1, D2-A)');
      sh.hideSheet();
      changes.push(['F1', E6.BLOGGERS, 'restored values ' + r + 'x' + c, 'from backup']);
      log_('fix', 'F1: лист «Блогеры» восстановлен из резервной копии (' + r + '×' + c + ', values-only, скрыт)');
    } else {
      // D2-B: не выдумывать — ссылки на лист заменить нулём с ISSUE
      var targets = [[E6.Y2024, ['C3', 'BU61', 'BU96', 'BU130']], [E6.MAIN, ['VW10']]];
      targets.forEach(function (t) {
        var sh2 = ss.getSheetByName(t[0]);
        t[1].forEach(function (a1) {
          var f = sh2.getRange(a1).getFormula();
          var nf = f.replace(/'Блогеры'![A-Z]+\d+/g, '0');
          if (nf !== f) { sh2.getRange(a1).setFormula(nf); changes.push(['F1', t[0] + '!' + a1, f, nf]); }
        });
      });
      log_('fix', 'F1: ISSUE BLOGGERS_SOURCE_MISSING — лист не найден в резервной копии; ссылки заменены 0, исторический общий доход неполон');
    }
  }
  // F2–F4
  E6.FIXES.forEach(function (fx) {
    var rg = main.getRange(fx.a1); var before = rg.getFormula() || rg.getValue();
    if (fx.formula) rg.setFormula(fx.formula); else rg.setValue(fx.value);
    changes.push([fx.id, E6.MAIN + '!' + fx.a1, String(before), fx.formula || ('"' + fx.value + '"') + (fx.issue ? ' ISSUE ' + fx.issue : '')]);
  });
  // F5 — сводка июня: FILTER до 24-го блока
  for (var k = 0; k < E6.JUNE.cols.length; k++) {
    var col = E6.JUNE.cols[k];
    var rg5 = main.getRange(col + E6.JUNE.first + ':' + col + E6.JUNE.last);
    var fs = rg5.getFormulas(); var n = 0;
    var nfs = fs.map(function (row) {
      var f = row[0]; if (!f) return [f];
      var g = f; E6.JUNE.repl.forEach(function (p) { g = g.split(p[0]).join(p[1]); });
      if (g !== f) n++; return [g];
    });
    rg5.setFormulas(nfs);
    changes.push(['F5', E6.MAIN + '!' + col + E6.JUNE.first + ':' + col + E6.JUNE.last, fs[0][0], nfs[0][0] + ' (' + n + ' строк)']);
  }
  saveJson_('e6_fix_changes.json', changes);
  changes.forEach(function (ch) { log_('fix', ch.join(' | ')); });
  log_('fix', 'ISSUE UNEXPLAINED_MANUAL_ADJUSTMENT: TD765 (+1), TD730 (+1) сохранены без изменений (D4) — сверить с исходной книгой/фактами');
  props_().setProperty('E6_FIX_DONE', new Date().toISOString());
}

// ───────────────────────────── 6. E6.4 сетка ─────────────────────────────
function e6trim() {
  var ss = ss_(); var done = [];
  ss.getSheets().forEach(function (sh) {
    var name = sh.getName();
    var lastR = sh.getLastRow(), lastC = sh.getLastColumn();
    if (lastR === 0 || lastC === 0) return;
    if (name === E6.MAIN) lastC = Math.max(lastC, E6.MAIN_LAST_COL);
    var maxR = sh.getMaxRows(), maxC = sh.getMaxColumns();
    var delC = maxC - lastC, delR = maxR - (lastR + E6.TRIM_MARGIN);
    if (delC > 0) { sh.deleteColumns(lastC + 1, delC); }
    if (delR > 0) { sh.deleteRows(lastR + E6.TRIM_MARGIN + 1, delR); }
    if (delC > 0 || delR > 0) done.push(name + ': -' + Math.max(delC, 0) + ' кол, -' + Math.max(delR, 0) + ' строк');
  });
  saveJson_('e6_trim_done.json', done);
  log_('trim', done.join('; ') || 'ничего не удалено');
  props_().setProperty('E6_TRIM_DONE', new Date().toISOString());
}

// ───────────────────────────── 7. QA ─────────────────────────────
function e6qa() {
  var ss = ss_(); var rep = { at: new Date().toISOString(), gates: {} };
  var vals0 = loadJson_('snap_values.json'), forms0 = loadJson_('snap_formulas.json'), vis0 = loadJson_('snap_visual.json');
  var allowed = {}; // ячейки, которым разрешено измениться (F2–F5 + зависимые ошибки)
  ['DP293', 'BD663', 'CB663', 'JX760'].forEach(function (a) { allowed[E6.MAIN + '!' + a] = 1; });
  for (var k = 0; k < E6.JUNE.cols.length; k++) for (var r = E6.JUNE.first; r <= E6.JUNE.last; r++) allowed[E6.MAIN + '!' + E6.JUNE.cols[k] + r] = 1;
  try { loadJson_('e6_fix_changes.json').forEach(function (ch) { if (ch[1] && ch[1].indexOf('!') > 0 && ch[1].indexOf(':') < 0) allowed[ch[1]] = 1; }); } catch (e) {}
  var frozen = {}; frozen[E6.AKCII] = 1; frozen[E6.OZON] = 1;
  var vdiff = [], fdiff = [], errsNow = {};
  ss.getSheets().forEach(function (sh) {
    var n = sh.getName(); if (n === E6.LOG || n === E6.BLOGGERS) return;
    var v0 = vals0[n], f0 = forms0[n]; if (!v0) return;
    var r = v0.length, c = r ? v0[0].length : 0; if (!r) return;
    var rg = sh.getRange(1, 1, r, c); var v1 = rg.getValues(), f1 = rg.getFormulas(); var e = 0;
    for (var i = 0; i < r; i++) for (var j = 0; j < c; j++) {
      var a1 = n + '!' + a1_(i + 1, j + 1);
      var x = v1[i][j]; if (typeof x === 'string' && x.charAt(0) === '#') e++;
      if (frozen[n]) continue;
      var y = v0[i][j];
      var same = (x instanceof Date && typeof y === 'string') ? (x.toISOString() === y) : (String(x) === String(y));
      // ошибки, ставшие числами из-за F1/F2 — допустимы только если раньше была ошибка
      var wasErr = typeof y === 'string' && y.charAt(0) === '#';
      if (!same && !allowed[a1] && !wasErr) vdiff.push([a1, y, x]);
      if (f1[i][j] !== f0[i][j] && !allowed[a1]) fdiff.push([a1, f0[i][j], f1[i][j]]);
    }
    errsNow[n] = e;
  });
  rep.gates.VALUE_REGRESSION = { pass: vdiff.length === 0, count: vdiff.length, sample: vdiff.slice(0, 20) };
  rep.gates.FORMULA_REGRESSION = { pass: fdiff.length === 0, count: fdiff.length, sample: fdiff.slice(0, 20) };
  // визуальный гейт: фон и цвет шрифта до/после — 0 отличий (V1)
  var visDiff = [];
  Object.keys(vis0).forEach(function (n) {
    var sh = ss.getSheetByName(n); if (!sh) return;
    var s0 = vis0[n]; var rg = sh.getRange(1, 1, s0.rows, s0.cols);
    var bg = rg.getBackgrounds(), fg = rg.getFontColors();
    for (var i = 0; i < s0.rows; i++) for (var j = 0; j < s0.cols; j++) {
      if (bg[i][j] !== s0.bg[i][j] || fg[i][j] !== s0.fg[i][j]) visDiff.push([n + '!' + a1_(i + 1, j + 1), s0.bg[i][j] + '/' + s0.fg[i][j], bg[i][j] + '/' + fg[i][j]]);
    }
  });
  rep.gates.CF_VISUAL_REGRESSION = { pass: visDiff.length === 0, count: visDiff.length, sample: visDiff.slice(0, 30) };
  // счётчики УФ
  var meta = Sheets.Spreadsheets.get(ss.getId(), { fields: 'sheets(properties(title),conditionalFormats)' });
  var cfc = {}; meta.sheets.forEach(function (s) { var rs = s.conditionalFormats || []; var p = 0; rs.forEach(function (r) { p += r.ranges.length; }); cfc[s.properties.title] = { rules: rs.length, parts: p }; });
  rep.gates.CF_COUNTS = { pass: cfc[E6.MAIN].parts <= 900 && cfc[E6.MAIN].rules <= 160, counts: cfc };
  rep.gates.FORMULA_ERRORS = { pass: (errsNow[E6.MAIN] || 0) <= 73, counts: errsNow };
  // структура: 24 nmID в строке 735, зеркало LCD и имя
  var main = ss.getSheetByName(E6.MAIN); var hdr = main.getRange(E6.SEP_TOP, 13, 1, 24 * 24).getValues()[0]; var nb = 0;
  for (var b = 0; b < 24; b++) if (/^\d{6,}/.test(String(hdr[b * 24] || ''))) nb++;
  var lcdName = ss.getRangeByName('LAST_CLOSED_DATE'); var mirror = main.getRange(E6.SEP_TOP + 1, E6.MAIN_LAST_COL).getValue();
  rep.gates.STRUCTURE = { pass: nb === 24 && lcdName && String(lcdName.getValue()) === String(mirror), blocks: nb, lcd: String(lcdName && lcdName.getValue()), mirror: String(mirror) };
  rep.pass = Object.keys(rep.gates).every(function (g) { return rep.gates[g].pass; });
  saveJson_('e6_qa_report.json', rep);
  log_('qa', (rep.pass ? 'PASS' : 'FAIL') + ' | VALUE ' + vdiff.length + ' | FORMULA ' + fdiff.length + ' | VISUAL ' + visDiff.length + ' | CF ' + JSON.stringify(cfc[E6.MAIN]) + ' | ERR ' + JSON.stringify(errsNow) + ' | STRUCT ' + JSON.stringify(rep.gates.STRUCTURE));
  return rep;
}

// ───────────────────────────── 8. rollback ─────────────────────────────
/** Полный откат по снимкам. Порядок обратный шагам. Полная копия книги (e6backup) — резервный путь. */
function e6rollback() {
  var ss = ss_();
  var dims = loadJson_('snap_dims.json'), forms0 = loadJson_('snap_formulas.json'), vals0 = loadJson_('snap_values.json');
  // trim: вернуть размеры
  ss.getSheets().forEach(function (sh) {
    var d = dims[sh.getName()]; if (!d) return;
    if (sh.getMaxColumns() < d.maxCols) sh.insertColumnsAfter(sh.getMaxColumns(), d.maxCols - sh.getMaxColumns());
    if (sh.getMaxRows() < d.maxRows) sh.insertRowsAfter(sh.getMaxRows(), d.maxRows - sh.getMaxRows());
  });
  // fix + freeze: формулы/значения из снимка для затронутых листов
  [E6.MAIN, E6.Y2024, E6.AKCII, E6.OZON].forEach(function (n) {
    var sh = ss.getSheetByName(n); if (!sh || !forms0[n] || !forms0[n].length) return;
    var f0 = forms0[n], v0 = vals0[n]; var r = f0.length, c = f0[0].length;
    var data = [];
    for (var i = 0; i < r; i++) { var row = []; for (var j = 0; j < c; j++) { var v = f0[i][j] ? f0[i][j] : v0[i][j]; if (typeof v === 'string' && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/.test(v)) v = new Date(v); row.push(v); } data.push(row); }
    sh.getRange(1, 1, r, c).setValues(data);
    if (dims[n] && !dims[n].hidden && sh.isSheetHidden()) sh.showSheet();
  });
  var bl = ss.getSheetByName(E6.BLOGGERS);
  if (bl && bl.getRange('A1').getNote().indexOf('RESTORED_FROM_BACKUP') === 0) ss.deleteSheet(bl);
  // cf: удалить текущие правила, вернуть старые; статические фоны — как в снимке
  var cf0 = loadJson_('snap_cf.json'); var stat = loadJson_('snap_static_bg.json');
  var meta = Sheets.Spreadsheets.get(ss.getId(), { fields: 'sheets(properties(sheetId,title),conditionalFormats)' });
  var reqs = [];
  meta.sheets.forEach(function (s) { var n = (s.conditionalFormats || []).length; if (cf0[s.properties.title]) for (var i = 0; i < n; i++) reqs.push({ deleteConditionalFormatRule: { sheetId: s.properties.sheetId, index: 0 } }); });
  Object.keys(cf0).forEach(function (t) { cf0[t].rules.forEach(function (rule, i) { reqs.push({ addConditionalFormatRule: { rule: rule, index: i } }); }); });
  for (var i = 0; i < reqs.length; i += 2000) Sheets.Spreadsheets.batchUpdate({ requests: reqs.slice(i, i + 2000) }, ss.getId());
  Object.keys(stat).forEach(function (n) {
    var sh = ss.getSheetByName(n); if (!sh) return; var s = stat[n];
    var bg = s.bg.map(function (row) { return row.map(function (x) { return x === null ? null : x; }); });
    sh.getRange(1, 1, s.rows, s.cols).setBackgrounds(bg);
  });
  log_('rollback', 'откат по снимкам выполнен: размеры, формулы/значения (' + [E6.MAIN, E6.Y2024, E6.AKCII, E6.OZON].join(', ') + '), УФ ' + reqs.length + ' запросов, статические фоны');
}
