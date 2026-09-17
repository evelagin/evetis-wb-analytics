// UNITKA 2.0 R7 — воронка: P «Переходы» и R «Положили в корзину».
// Только сентябрь 2026, только блок крема для рук (nmID 252442517, первый блок, колонка M).
// Август не трогается. Другие SKU не трогаются. Формат и формулы не трогаются.
// Agent: claude-opus-5, v7.2.0
//
// ГДЕ ЗАПУСКАТЬ: отдельный проект Apps Script «Проект без названия»
// (script id 1DrcKy55LH5TrN3aPz6l18WpfPIOCUrRMEy95SP6TQXxaWWXtsPW3SBW3) — слой записи
// книги «Юнитка_Evetis Cosmetics». НЕ в проекте evetis-wb-analitics.
//
// ПРЕДУСЛОВИЕ: в проекте подключён сервис «BigQuery API» (Редактор → Сервисы → + →
// BigQuery API → Добавить). Без него r7run остановится ДО записи и скажет об этом.
//
// Публичная функция РОВНО ОДНА — r7run. Остальные с суффиксом _ (выбор функции в
// редакторе срабатывает ненадёжно, см. R6).
//
// Источник — wb_raw.V_WB_FUNNEL_DAILY: одна строка на date_msk × nm_id, последнее
// наблюдение загрузчика wb-funnel-prod (WB POST /api/analytics/v3/sales-funnel/products/history).
// P = open_card_count (openCount: все переходы в карточку, включая органику),
// R = add_to_cart_count (cartCount). Клики рекламы вместо переходов НЕ подставляются.
//
// Пишутся только закрытые дни (<= LAST_CLOSED_DATE), по которым в BigQuery есть строка.
// Нет строки — ячейка не трогается и остаётся пустой (01–03.09 глубже недели: без
// подписки «Джем» WB их не отдаёт). Будущие дни не пишутся никогда.
//
// РЕГРЕСС. База — снимок ВСЕГО листа прямо перед записью (отображение + формулы всех
// ячеек). После записи лист снимается ещё раз. Допустимо только:
//   - записанные ячейки P/R;
//   - пересчёт формулы, которая по ссылкам (прямо или через другие пересчитанные
//     формулы) зависит от записанной ячейки — например, сводка 7 SKU
//     E740 = SUM(FILTER(P740:UV740; …)) и итоги строки 767;
//   - волатильные формулы (NOW, RAND, IMPORT…) — перечисляются отдельно.
// Любое другое изменение значения или формулы = РЕГРЕСС. Эталон R6 — только справочно.
//
// Помимо P/R скрипт дописывает строки аудита в служебный лист ZZ_AUDIT_LOG (как R1–R6).
var R7_SSID = '1E4L4JuwfEqr9owhsGkAjb8F24lRpWpWkEyVmSuRxaJg';
var R7_SH = 'WB_Юнит_2025';
var R7_NM = 252442517;
var R7_PROJECT = 'project-fa311fc0-4d87-4781-986';
var R7_SRC = R7_PROJECT + '.wb_raw.V_WB_FUNNEL_DAILY';
var R7_FIRST = 737, R7_DAYS = 30;                   // M737:M766 = 01..30.09
var R7_BLOCK = 13, R7_BLOCK_W = 24;                 // M — первый SKU-блок, ширина 24
var R7_COL_P = 16, R7_COL_R = 18;                   // P, R первого блока
var R7_MIRROR_COL = 600;                            // WB736 — зеркало LAST_CLOSED_DATE
var R7_LCD_MIN = '2026-09-09';                      // LAST_CLOSED_DATE после R6; назад не ходит
var R7_SEP = '\u0001';                              // разделитель ячеек в снимке строки
var R7_VER = 'unitka2.0/v7.2.0';
// Справочно: ключевые метрики R6. Сверено с живым листом 11.09.2026 (копия A735:AJ768):
// все 10 значений и формула AH737 совпали с R6. На вердикт не влияет.
var R7_R6_REF = [['AD737', '45,4%'], ['AD767', '45,4%'], ['AH737', '10 ₽'], ['AE737', '272 ₽'],
  ['AI737', '-51 ₽'], ['AI767', '-43 ₽'], ['AG767', '264 ₽'], ['AF737', '73 ₽'], ['W767', '-8 728 ₽'], ['Q767', '59']];

function r7run() {
  var L = [];
  try {
    r7main_(L);
  } catch (e) {
    L.push('ОШИБКА ВЫПОЛНЕНИЯ: ' + e + (e && e.stack ? '\n' + e.stack : ''));
  }
  Logger.log(L.join('\n'));
}

function r7main_(L) {
  L.push('=== R7 PRECHECK ===');
  var stop = function (msg) { L.push('СТОП: ' + msg + ' — запись НЕ выполнялась'); return false; };

  // 0. Сервис BigQuery.
  if (typeof BigQuery === 'undefined' || !BigQuery.Jobs) {
    return stop('в проекте не подключён сервис BigQuery API (Редактор → Сервисы → + → BigQuery API → Добавить)');
  }

  // 1. Книга и лист.
  var ss = SpreadsheetApp.openById(R7_SSID);
  var active = SpreadsheetApp.getActiveSpreadsheet();
  L.push('книга: ' + ss.getName() + ' | id ' + ss.getId());
  if (ss.getId() !== R7_SSID) return stop('id книги ' + ss.getId() + ' вместо ' + R7_SSID);
  if (active && active.getId() !== R7_SSID) return stop('скрипт привязан к другой книге: ' + active.getId());
  var sh = ss.getSheetByName(R7_SH);
  if (!sh) return stop('нет листа ' + R7_SH);
  var tz = ss.getSpreadsheetTimeZone();
  L.push('лист: ' + sh.getName() + ' | часовой пояс книги ' + tz);

  // 2. Строки сентября и даты.
  var a735 = String(sh.getRange(735, 1).getDisplayValue());
  L.push('A735 = [' + a735 + ']');
  if (a735.indexOf('Сентябрь 2026') !== 0) return stop('A735 не «Сентябрь 2026»');
  var dates = sh.getRange(R7_FIRST, R7_BLOCK, R7_DAYS + 1, 1).getValues();
  var ymd = [];
  for (var i = 0; i < R7_DAYS; i++) {
    var want = '2026-09-' + (i < 9 ? '0' : '') + (i + 1);
    var got = dates[i][0] instanceof Date ? Utilities.formatDate(dates[i][0], tz, 'yyyy-MM-dd') : String(dates[i][0]);
    if (got !== want) return stop('M' + (R7_FIRST + i) + ' = ' + got + ', ожидалось ' + want);
    ymd.push(want);
  }
  if (dates[R7_DAYS][0] instanceof Date) return stop('M767 — дата, а должна быть строка итога');
  L.push('M737:M766 = 2026-09-01 .. 2026-09-30 — OK');

  // 3. Заголовки колонок блока.
  var hdr = sh.getRange(736, R7_BLOCK, 1, 7).getDisplayValues()[0]; // M N O P Q R S
  var hP = String(hdr[3]), hQ = String(hdr[4]), hR = String(hdr[5]), hS = String(hdr[6]);
  L.push('M736=[' + hdr[0] + '] P736=[' + hP + '] Q736=[' + hQ + '] R736=[' + hR + '] S736=[' + hS + ']');
  if (String(hdr[0]).trim() !== 'Дата') return stop('M736 не «Дата»');
  if (!/переход/i.test(hP)) return stop('P736 не «Переходы»');
  if (!/корзин/i.test(hR)) return stop('R736 не «Положили в корзину»');
  if (!/заказ/i.test(hQ) || !/отмен/i.test(hS)) return stop('Q736/S736 не «Заказы»/«Отмены» — колонки сдвинуты');

  // 4. LAST_CLOSED_DATE.
  var lcdRange = ss.getRangeByName('LAST_CLOSED_DATE');
  if (!lcdRange) return stop('нет именованного диапазона LAST_CLOSED_DATE');
  var lcdV = lcdRange.getValue(), mirror = sh.getRange(736, R7_MIRROR_COL).getValue();
  if (!(lcdV instanceof Date)) return stop('LAST_CLOSED_DATE не дата: ' + lcdV);
  var lcd = Utilities.formatDate(lcdV, tz, 'yyyy-MM-dd');
  var lcdMirror = mirror instanceof Date ? Utilities.formatDate(mirror, tz, 'yyyy-MM-dd') : String(mirror);
  var todayMsk = Utilities.formatDate(new Date(), 'Europe/Moscow', 'yyyy-MM-dd');
  L.push('LAST_CLOSED_DATE = ' + lcd + ' | зеркало WB736 = ' + lcdMirror + ' | сегодня МСК ' + todayMsk);
  if (lcd !== lcdMirror) return stop('зеркало WB736 расходится с LAST_CLOSED_DATE');
  if (lcd < R7_LCD_MIN || lcd > '2026-09-30') return stop('LAST_CLOSED_DATE вне окна ' + R7_LCD_MIN + '..2026-09-30');
  if (lcd >= todayMsk) return stop('LAST_CLOSED_DATE не закрыт: он не раньше сегодняшнего дня');

  // 5. В P/R сентября нет формул.
  var rngP = sh.getRange(R7_FIRST, R7_COL_P, R7_DAYS, 1), rngR = sh.getRange(R7_FIRST, R7_COL_R, R7_DAYS, 1);
  var fP = rngP.getFormulas(), fR = rngR.getFormulas(), nF = 0;
  for (var j = 0; j < R7_DAYS; j++) if (fP[j][0] || fR[j][0]) nF++;
  if (nF > 0) return stop('в P737:P766 / R737:R766 формул: ' + nF + ' — значением не перезаписываем');

  // 6. BigQuery — источник истины.
  var bq = r7query_('SELECT CAST(date_msk AS STRING) d, open_card_count p, add_to_cart_count r, ' +
    'observation_id oid FROM `' + R7_SRC + '` WHERE nm_id = ' + R7_NM +
    " AND date_msk BETWEEN '2026-09-01' AND '2026-09-30' ORDER BY d");
  var byDay = {}, dup = 0;
  for (var k = 0; k < bq.length; k++) {
    if (byDay[bq[k][0]]) dup++;
    byDay[bq[k][0]] = { p: bq[k][1] === null ? null : Number(bq[k][1]), r: bq[k][2] === null ? null : Number(bq[k][2]), oid: bq[k][3] };
  }
  L.push('BigQuery ' + R7_SRC + ': строк ' + bq.length + ', дублей date×nm ' + dup +
    (bq.length ? ', даты ' + bq[0][0] + '..' + bq[bq.length - 1][0] : ''));
  if (dup > 0) return stop('grain date×nm нарушен');
  if (bq.length === 0) return stop('в BigQuery нет данных воронки по nmID ' + R7_NM);

  // 7. План записи: закрытые дни с данными.
  var vP = rngP.getValues(), vR = rngR.getValues();
  var target = {}, plan = [];
  for (var i2 = 0; i2 < R7_DAYS; i2++) {
    var d = ymd[i2], b = byDay[d];
    if (d > lcd || !b || b.p === null || b.r === null) continue;
    target[i2] = b; plan.push(d);
  }
  L.push('дней к записи: ' + plan.length + (plan.length ? ' (' + plan[0] + '..' + plan[plan.length - 1] + ')' : ''));
  if (plan.length === 0) return stop('нет закрытых дней с данными воронки');

  // 8. База регресса — снимок ВСЕГО листа прямо перед записью.
  var nRows = sh.getLastRow(), nCols = Math.max(sh.getLastColumn(), R7_MIRROR_COL);
  var t0 = new Date().getTime();
  var before = r7snap_(sh, nRows, nCols);
  L.push('снимок ДО: ' + nRows + ' строк × ' + nCols + ' колонок, ' + (new Date().getTime() - t0) + ' мс');

  // 9. Запись: только изменившиеся ячейки, непрерывными блоками.
  var runId = 'r7_' + new Date().getTime(), now = new Date(), audit = [], written = [];
  r7writeCol_(sh, R7_COL_P, 'P', vP, target, 'p', 'open_card_count', ymd, runId, now, audit, written);
  r7writeCol_(sh, R7_COL_R, 'R', vR, target, 'r', 'add_to_cart_count', ymd, runId, now, audit, written);
  for (var g = 0; g < R7_DAYS; g++) {
    if (ymd[g] <= lcd && !target[g]) {
      audit.push([runId, now, ymd[g], R7_SH, String(R7_NM), 'P' + (R7_FIRST + g) + ',R' + (R7_FIRST + g), '', '',
        R7_SRC, 'нет строки', 'SKIPPED', 'GAP: без подписки «Джем» WB не отдаёт дни глубже недели', R7_VER]);
    }
  }
  var lg = ss.getSheetByName('ZZ_AUDIT_LOG');
  if (lg && audit.length) lg.getRange(lg.getLastRow() + 1, 1, audit.length, 13).setValues(audit);
  SpreadsheetApp.flush();
  L.push('');
  L.push('=== R7 APPLY ===');
  L.push('записано ячеек ' + written.length + ' | строк аудита ' + audit.length + (lg ? '' : ' (лист ZZ_AUDIT_LOG не найден — аудит не записан)'));

  // 10. Снимок ПОСЛЕ и разбор каждого изменения.
  t0 = new Date().getTime();
  var after = r7snap_(sh, nRows, nCols);
  var df = r7diff_(before, after, nCols, written);
  L.push('');
  L.push('=== R7 DIFF: весь лист, ' + nRows + ' × ' + nCols + ' (' + (new Date().getTime() - t0) + ' мс) ===');
  L.push('записанные P/R: ' + df.target + ' из ' + written.length);
  L.push('зависимый пересчёт (формула ссылается на записанное): ' + df.dependent.length +
    (df.dependent.length ? ' — ' + df.dependent.slice(0, 15).join(' | ') : ''));
  L.push('волатильные формулы (NOW/RAND/IMPORT…): ' + df.volatile.length +
    (df.volatile.length ? ' — ' + df.volatile.slice(0, 10).join(' | ') : ''));
  L.push('РЕГРЕСС (прочие изменения значений и формул): ' + df.regress.length +
    (df.regress.length ? '  <-- ' + df.regress.slice(0, 25).join(' | ') : '  (норма 0)'));

  var qa = r7qa_(L, sh, ymd, lcd, byDay, before, after);

  // 11. Вердикт. Эталон R6 в него не входит — он справочный.
  var funnelOk = qa.misP === 0 && qa.misR === 0;
  var r6Ok = df.regress.length === 0 && df.target === written.length;
  var ready = funnelOk && qa.mismatch === 0 && qa.errors === 0 && qa.dirtyFuture === 0 && r6Ok;
  L.push('');
  L.push('=== R7 ИТОГ ===');
  L.push('FUNNEL ............ ' + (funnelOk ? 'PASS' : 'FAIL') + ' (закрытых дней с данными: ' + plan.length + ')');
  L.push('CARD OPENS ........ ' + (qa.misP === 0 ? 'PASS' : 'FAIL'));
  L.push('ADD TO CART ....... ' + (qa.misR === 0 ? 'PASS' : 'FAIL'));
  L.push('BQ->SHEETS ........ ' + (qa.mismatch === 0 ? 'PASS' : 'FAIL'));
  L.push('MISMATCH .......... ' + qa.mismatch);
  L.push('ОШИБОК В БЛОКЕ .... ' + qa.errors);
  L.push('R6 REGRESSION ..... ' + (r6Ok ? 'PASS' : 'FAIL') + ' (регресс ' + df.regress.length +
    ', зависимый пересчёт ' + df.dependent.length + ', волатильных ' + df.volatile.length + ')');
  L.push('SEPTEMBER MASTER TEMPLATE READY = ' + (ready ? 'YES' : 'NO'));
}

/** Пишет одну колонку: только изменившиеся ячейки целевых дней, непрерывными блоками. */
function r7writeCol_(sh, col, letter, cur, target, key, field, ymd, runId, now, audit, written) {
  var run = [];
  var flush = function () {
    if (!run.length) return;
    sh.getRange(R7_FIRST + run[0].i, col, run.length, 1).setValues(run.map(function (c) { return [c.v]; }));
    for (var q = 0; q < run.length; q++) written.push({ r: R7_FIRST + run[q].i, c: col });
    run = [];
  };
  for (var i = 0; i < R7_DAYS; i++) {
    var b = target[i];
    if (!b) { flush(); continue; }
    var old = cur[i][0], nv = b[key], a1 = letter + (R7_FIRST + i);
    if (old === nv) {
      flush();
      audit.push([runId, now, ymd[i], R7_SH, String(R7_NM), a1, String(old), String(nv), R7_SRC, field, 'UNCHANGED', b.oid, R7_VER]);
      continue;
    }
    run.push({ i: i, v: nv });
    audit.push([runId, now, ymd[i], R7_SH, String(R7_NM), a1, old === '' ? '(пусто)' : String(old), String(nv), R7_SRC, field, 'OK', b.oid, R7_VER]);
  }
  flush();
}

/** Снимок листа: по строке — склеенные отображаемые значения и формулы. Читаем блоками по 100 строк. */
function r7snap_(sh, nRows, nCols) {
  var disp = [], form = [];
  for (var r0 = 1; r0 <= nRows; r0 += 100) {
    var n = Math.min(100, nRows - r0 + 1), rg = sh.getRange(r0, 1, n, nCols);
    var dv = rg.getDisplayValues(), fv = rg.getFormulas();
    for (var i = 0; i < n; i++) { disp.push(dv[i].join(R7_SEP)); form.push(fv[i].join(R7_SEP)); }
  }
  return { disp: disp, form: form };
}

/** Отображаемое значение ячейки из снимка. */
function r7cell_(snap, row, col) {
  var line = snap.disp[row - 1];
  return line === undefined ? '' : (line.split(R7_SEP)[col - 1] || '');
}

/** Разбор изменений: записанное / зависимый пересчёт / волатильное / регресс. */
function r7diff_(before, after, nCols, written) {
  var isWritten = {};
  for (var w = 0; w < written.length; w++) isWritten[written[w].r + ',' + written[w].c] = true;
  var res = { target: 0, dependent: [], volatile: [], regress: [] }, cand = [];
  for (var i = 0; i < before.disp.length; i++) {
    if (before.disp[i] === after.disp[i] && before.form[i] === after.form[i]) continue;
    var bd = before.disp[i].split(R7_SEP), ad = after.disp[i].split(R7_SEP);
    var bf = before.form[i].split(R7_SEP), af = after.form[i].split(R7_SEP);
    for (var c = 0; c < nCols; c++) {
      if (bd[c] === ad[c] && bf[c] === af[c]) continue;
      var row = i + 1, a1 = r7col_(c + 1) + row, line = a1 + ': [' + bd[c] + '] -> [' + ad[c] + ']';
      if (isWritten[row + ',' + (c + 1)]) { res.target++; continue; }
      if (bf[c] !== af[c]) { res.regress.push(a1 + ': формула [' + bf[c] + '] -> [' + af[c] + ']'); continue; }
      if (!bf[c]) { res.regress.push(line + ' (значение без формулы)'); continue; }
      if (/(^|[^A-Z])(NOW|RAND|RANDBETWEEN|RANDARRAY|IMPORTRANGE|IMPORTDATA|IMPORTXML|IMPORTHTML|IMPORTFEED|GOOGLEFINANCE)\s*\(/i.test(bf[c])) {
        res.volatile.push(line); continue;
      }
      cand.push({ r: row, c: c + 1, line: line, rects: r7refs_(bf[c]), ok: false });
    }
  }
  // Замыкание: формула законно пересчиталась, если ссылается на записанную ячейку
  // или на формулу, уже признанную зависимой.
  var pts = written.slice(), grew = true;
  while (grew) {
    grew = false;
    for (var k = 0; k < cand.length; k++) {
      if (cand[k].ok || !r7hits_(cand[k].rects, pts)) continue;
      cand[k].ok = true; pts.push({ r: cand[k].r, c: cand[k].c }); grew = true;
    }
  }
  for (var m = 0; m < cand.length; m++) (cand[m].ok ? res.dependent : res.regress).push(cand[m].line);
  return res;
}

/** Ссылки формулы на ЭТОТ лист: [{r1,r2,c1,c2}]. Строковые литералы и чужие листы отброшены. */
function r7refs_(f) {
  var s = String(f).replace(/"(?:[^"]|"")*"/g, '""');
  s = s.replace(/(?:'((?:[^']|'')+)'|([A-Za-z\u0400-\u04FF_][A-Za-z0-9\u0400-\u04FF_.]*))!(\$?[A-Z]{1,3}\$?\d*(?::\$?[A-Z]{1,3}\$?\d*)?)/g,
    function (all, quoted, plain, ref) {
      var name = quoted ? quoted.replace(/''/g, "'") : plain;
      return name === R7_SH ? ' ' + ref : ' 0';
    });
  var out = [], re = /\$?([A-Z]{1,3})\$?(\d+)(?::\$?([A-Z]{1,3})\$?(\d+))?|\$?([A-Z]{1,3}):\$?([A-Z]{1,3})(?![0-9])/g, m;
  while ((m = re.exec(s)) !== null) {
    var prev = m.index > 0 ? s.charAt(m.index - 1) : '', next = s.charAt(m.index + m[0].length);
    if (/[A-Za-z0-9_.\u0400-\u04FF]/.test(prev) || /[A-Za-z0-9_(\u0400-\u04FF]/.test(next)) continue;
    if (m[1]) {
      var r1 = Number(m[2]), c1 = r7num_(m[1]);
      var r2 = m[3] ? Number(m[4]) : r1, c2 = m[3] ? r7num_(m[3]) : c1;
      out.push({ r1: Math.min(r1, r2), r2: Math.max(r1, r2), c1: Math.min(c1, c2), c2: Math.max(c1, c2) });
    } else {
      var a = r7num_(m[5]), z = r7num_(m[6]);
      out.push({ r1: 1, r2: 1e9, c1: Math.min(a, z), c2: Math.max(a, z) });
    }
  }
  return out;
}

function r7hits_(rects, pts) {
  for (var i = 0; i < rects.length; i++) for (var j = 0; j < pts.length; j++) {
    var q = rects[i], p = pts[j];
    if (p.r >= q.r1 && p.r <= q.r2 && p.c >= q.c1 && p.c <= q.c2) return true;
  }
  return false;
}

function r7qa_(L, sh, ymd, lcd, byDay, before, after) {
  L.push('');
  L.push('=== R7 QA: BQ vs SHEETS ===');
  L.push('DATE | BQ_CARD_OPENS | SHEETS_CARD_OPENS | BQ_ADD_TO_CART | SHEETS_ADD_TO_CART | STATUS');
  var vP = sh.getRange(R7_FIRST, R7_COL_P, R7_DAYS, 1).getValues();
  var vR = sh.getRange(R7_FIRST, R7_COL_R, R7_DAYS, 1).getValues();
  var res = { ok: 0, gap: 0, mismatch: 0, misP: 0, misR: 0, future: 0, dirtyFuture: 0, errors: 0 };
  for (var i = 0; i < R7_DAYS; i++) {
    var d = ymd[i], b = byDay[d], sp = vP[i][0], sr = vR[i][0], st;
    if (d > lcd) {
      if (sp === '' && sr === '') res.future++; else { res.dirtyFuture++; res.mismatch++; L.push(d + ' | — | ' + sp + ' | — | ' + sr + ' | FUTURE_NOT_EMPTY'); }
      continue;
    }
    if (!b || b.p === null || b.r === null) {
      st = (sp === '' && sr === '') ? 'GAP' : 'GAP_NOT_BLANK';
    } else {
      var okP = sp !== '' && Number(sp) === b.p, okR = sr !== '' && Number(sr) === b.r;
      if (!okP) res.misP++;
      if (!okR) res.misR++;
      st = okP && okR ? 'OK' : 'MISMATCH';
    }
    if (st === 'OK') res.ok++; else if (st === 'GAP') res.gap++; else res.mismatch++;
    L.push(d + ' | ' + (b ? b.p : '—') + ' | ' + sp + ' | ' + (b ? b.r : '—') + ' | ' + sr + ' | ' + st);
  }
  L.push('OK ' + res.ok + ' | GAP ' + res.gap + ' | MISMATCH ' + res.mismatch + ' | будущих пустых ' + res.future);

  L.push('');
  L.push('=== R7 QA: сентябрь, блок крема ===');
  var vals = sh.getRange(735, R7_BLOCK, 34, R7_BLOCK_W).getDisplayValues();
  var errs = ['#REF!', '#VALUE!', '#NAME?', '#DIV/0!', '#N/A', '#ERROR!'], cnt = {};
  for (var e = 0; e < errs.length; e++) cnt[errs[e]] = 0;
  for (var y = 0; y < vals.length; y++) for (var x = 0; x < vals[y].length; x++) {
    for (var e2 = 0; e2 < errs.length; e2++) if (String(vals[y][x]).indexOf(errs[e2]) >= 0) { cnt[errs[e2]]++; res.errors++; }
  }
  var errLine = [];
  for (var e3 = 0; e3 < errs.length; e3++) errLine.push(errs[e3] + ' = ' + cnt[errs[e3]]);
  L.push(errLine.join(' · ') + ' · ВСЕГО = ' + res.errors);

  // Будущие дни: экономика и воронка пусты (как в R6, плюс P и R).
  var chk = [16, 18, 20, 21, 22, 23, 26, 29, 31, 33, 34, 35], futRows = 0, dirty = 0;
  var block = sh.getRange(R7_FIRST, 1, R7_DAYS, 36).getDisplayValues();
  for (var r = 0; r < R7_DAYS; r++) {
    if (ymd[r] <= lcd) continue;
    futRows++;
    for (var c = 0; c < chk.length; c++) if (String(block[r][chk[c] - 1]) !== '') dirty++;
  }
  L.push('будущих дней ' + futRows + ', непустых экономических ячеек в них ' + dirty + ' (норма 0)');
  res.dirtyFuture += dirty;

  // Сводка 7 SKU (E «Переходы», G «Положили в корзину») — зависимый пересчёт, справочно.
  var sum = [];
  for (var s = 0; s < R7_DAYS; s++) if (ymd[s] <= lcd && byDay[ymd[s]]) {
    var rr = R7_FIRST + s;
    sum.push(ymd[s].slice(8) + ': E ' + r7cell_(before, rr, 5) + '→' + r7cell_(after, rr, 5) + ', G ' + r7cell_(before, rr, 7) + '→' + r7cell_(after, rr, 7));
  }
  L.push('сводка 7 SKU (E/G): ' + sum.join(' | '));

  L.push('');
  L.push('=== R7 СПРАВОЧНО: метрики R6 (R6 | до записи | после) — на вердикт не влияет ===');
  for (var q = 0; q < R7_R6_REF.length; q++) {
    var m = R7_R6_REF[q][0].match(/^([A-Z]+)(\d+)$/), col = r7num_(m[1]), row = Number(m[2]);
    var vb = r7cell_(before, row, col), va = r7cell_(after, row, col);
    L.push(R7_R6_REF[q][0] + ': ' + R7_R6_REF[q][1] + ' | ' + vb + ' | ' + va +
      (vb === va ? '  не изменилось' : '  ИЗМЕНИЛОСЬ ПРИ ЗАПИСИ') +
      (r7norm_(vb) === r7norm_(R7_R6_REF[q][1]) ? '' : '  (до записи уже отличалось от R6 — не регресс R7)'));
  }
  var fAH = sh.getRange('AH737').getFormula();
  L.push('AH737 формула = ' + fAH + (/IF\(\$M737>LAST_CLOSED_DATE[;,]""[;,]AA737\*2%\)/.test(fAH) ? '  = R6' : '  (отличается от R6)'));
  L.push('P767 = [' + r7cell_(after, 767, R7_COL_P) + '] | R767 = [' + r7cell_(after, 767, R7_COL_R) + ']');
  return res;
}

function r7norm_(s) { return String(s).replace(/[\s\u00a0\u202f\u20bd]/g, '').replace(/\u2212/g, '-'); }

function r7query_(sql) {
  var res = BigQuery.Jobs.query({ query: sql, useLegacySql: false, location: 'EU' }, R7_PROJECT);
  var jobId = res.jobReference.jobId, n = 0;
  while (!res.jobComplete && n < 30) {
    Utilities.sleep(1000); n++;
    res = BigQuery.Jobs.getQueryResults(R7_PROJECT, jobId, { location: 'EU' });
  }
  if (!res.jobComplete) throw new Error('BigQuery: запрос не завершился за 30 с');
  var out = [], rows = res.rows || [];
  for (var i = 0; i < rows.length; i++) out.push(rows[i].f.map(function (c) { return c.v; }));
  return out;
}

function r7num_(letters) { var n = 0; for (var i = 0; i < letters.length; i++) n = n * 26 + (letters.charCodeAt(i) - 64); return n; }

function r7col_(n) { var s = ''; while (n > 0) { var m = (n - 1) % 26; s = String.fromCharCode(65 + m) + s; n = (n - 1 - m) / 26; } return s; }
