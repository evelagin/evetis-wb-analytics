// UNITKA 2.0 R7.5 — сентябрьский UX: календарь, стиль, QA. agent claude-opus-5, v7.5.1
// Книга «Юнитка_Evetis Cosmetics», лист WB_Юнит_2025, сентябрь (735 шапка · 736 заголовки ·
// 737–766 дни · 767 итог · 768 план). Проект «Проект без названия» (1DrcKy55…). BigQuery не нужен.
//
// Четыре публичные функции, каждая идемпотентна и укладывается заведомо в лимит 6 минут.
// Полного снимка листа 768×600 НЕТ — только адресные диапазоны (правило владельца 11.09.2026).
//
//   r75calendar() — PHASE 1. День недели ФОРМУЛОЙ от своей даты во всех 26 колонках сентября
//                   (A←B, L←M, «+23» каждого из 24 SKU-блоков ← дата этого же блока).
//                   Здесь же снимается базовый снимок для r75qa (Script Properties, чанками).
//   r75style()    — PHASE 2. Стиль ТОЛЬКО сентябрьского блока «Крем для рук» (M..AJ):
//                   выходные от даты, спокойный трафик O/P/Q/R, сигнал отмен, 4 зоны
//                   оборачиваемости целым числом, знак доходности. Экономика не трогается.
//   r75qa()       — PHASE 3. Targeted-регрессия против снимка r75calendar.
//   r75rollback() — откат ТОЛЬКО визуала r75style. Формулы дня недели не откатываются:
//                   это исправление ошибки, а не оформление.
//
// Существующие правила условного форматирования не удаляются и не редактируются: правила R7.5
// ставятся в начало списка и перекрывают старые (в Google Sheets побеждает первое подходящее).
// Поэтому август и другие SKU физически не затронуты, а откат — удаление только своих правил.

var R75 = {
  SSID: '1E4L4JuwfEqr9owhsGkAjb8F24lRpWpWkEyVmSuRxaJg',
  SH: 'WB_Юнит_2025',
  TOP: 735, HDR: 736, FIRST: 737, DAYS: 30, BOT: 768,
  B0: 13, BW: 24, NB: 24,
  NC: 589,
  MIR: 600,
  VER: 'unitka2.0/v7.5.1',
  BASE: 'R75_BASE', RB: 'R75_STYLE_RB'
};
var R75_DOW = ['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс'];
var R75_C = {
  weekend: '#fcefe3', dim: '#b7b7b7', white: '#ffffff',
  red: '#cc0000', green: '#38761d',
  sBg: '#fce8e6', sFc: '#a61c00',
  uCritBg: '#fce8e6', uCritFc: '#a61c00',
  uWarnBg: '#fff2cc', uWarnFc: '#7f6000',
  uOkBg: '#e6f4ea', uOkFc: '#274e13',
  uOverBg: '#efefef', uOverFc: '#434343'
};
var R75_NOTE_P = 'AUTO · wb_raw.V_WB_FUNNEL_DAILY.open_card_count\n' +
  'WB «Воронка продаж» v3 products/history, поле openCount — ВСЕ переходы в карточку, включая органику.\n' +
  'Клики рекламы (MART_SKU_DAILY.clicks) — НЕ переходы. Пишет r7run из BigQuery. Руками не вводить.\n' +
  'Без подписки «Джем» WB отдаёт не глубже доступного окна: дни старше — пусто (GAP).';
var R75_NOTE_R = 'AUTO · wb_raw.V_WB_FUNNEL_DAILY.add_to_cart_count\n' +
  'WB «Воронка продаж» v3 products/history, поле cartCount — положили в корзину.\n' +
  'Пишет r7run из BigQuery. Руками не вводить. Дни старше доступного окна без «Джема» — пусто (GAP).';

// ==================== PHASE 1 — КАЛЕНДАРЬ ==========================================

function r75calendar() {
  var L = ['=== R7.5 PHASE 1 · r75calendar · ' + R75.VER + ' ==='];
  try {
    var t0 = new Date().getTime();
    var ctx = r75open_(L); if (!ctx) return r75out_(L);
    var sh = ctx.sh, tz = ctx.tz;

    // ЧТЕНИЕ — всё сразу, ДО первой записи. Любое чтение после записи заставляет книгу
    // пересчитаться целиком (180 тыс. формул); 26 таких пересчётов = превышение лимита 6 минут.
    var wide = sh.getRange(R75.FIRST, 1, R75.DAYS, R75.NC);
    var vals = wide.getValues(), forms = wide.getFormulas();

    var plan = [{ wc: 1, dc: 2 }, { wc: 12, dc: R75.B0 }];
    for (var b = 0; b < R75.NB; b++) plan.push({ wc: R75.B0 + b * R75.BW + R75.BW - 1, dc: R75.B0 + b * R75.BW });
    var known = {}; plan.forEach(function (p) { known[p.wc] = 1; });

    var stray = [];
    for (var c = 1; c <= R75.NC; c++) {
      if (known[c]) continue;
      var n = 0;
      for (var r = 0; r < R75.DAYS; r++) if (R75_DOW.indexOf(String(vals[r][c - 1]).trim().toLowerCase()) >= 0) n++;
      if (n >= 20) stray.push(r75col_(c));
    }

    var wrongBefore = 0, byCol = {};
    for (var k = 0; k < plan.length; k++) {
      var p = plan[k];
      for (var r2 = 0; r2 < R75.DAYS; r2++) {
        var dv = vals[r2][p.dc - 1];
        if (!(dv instanceof Date)) { L.push('СТОП: ' + r75col_(p.dc) + (R75.FIRST + r2) + ' не дата — изменений НЕ вносилось'); return r75out_(L); }
        if (Utilities.formatDate(dv, tz, 'yyyy-MM-dd') !== r75ymd_(r2)) { L.push('СТОП: ' + r75col_(p.dc) + (R75.FIRST + r2) + ' ≠ ' + r75ymd_(r2) + ' — изменений НЕ вносилось'); return r75out_(L); }
        var e = R75_DOW[Number(Utilities.formatDate(dv, tz, 'u')) - 1];
        if (String(vals[r2][p.wc - 1]).trim().toLowerCase() !== e) { wrongBefore++; byCol[r75col_(p.wc)] = (byCol[r75col_(p.wc)] || 0) + 1; }
      }
    }
    L.push('обнаружено колонок дня недели: ' + plan.length + ' — ' +
      plan.map(function (p) { return r75col_(p.wc) + '←' + r75col_(p.dc); }).join(' '));
    L.push('колонок дня недели вне известной геометрии: ' + stray.length + (stray.length ? ' <-- ' + stray.join(' ') : ''));
    L.push('неверных значений дня недели ДО: ' + wrongBefore + (wrongBefore ? ' — ' + JSON.stringify(byCol) : ''));

    var base = r75get_(R75.BASE);
    if (base) L.push('базовый снимок уже есть (' + base.ts + ') — не перезаписан, эталон для r75qa сохранён');
    else { r75put_(R75.BASE, r75snapshot_(sh, tz)); L.push('базовый снимок для r75qa записан (M735:AJ768: значения, хэши формул, ошибки, отпечатки УФ)'); }
    L.push('чтение и снимок: ' + Math.round((new Date().getTime() - t0) / 1000) + ' с');

    // ЗАПИСЬ — 26 вызовов подряд, без единого чтения между ними: один пересчёт на все.
    var written = 0, touched = 0;
    for (var k2 = 0; k2 < plan.length; k2++) {
      var pp = plan[k2], dcl = r75col_(pp.dc), out = [], diff = false;
      for (var r3 = 0; r3 < R75.DAYS; r3++) {
        var row = R75.FIRST + r3, fx = r75dowFormula_(dcl, row);
        out.push([fx]); if (forms[r3][pp.wc - 1] !== fx) { diff = true; written++; }
      }
      if (diff) { sh.getRange(R75.FIRST, pp.wc, R75.DAYS, 1).setFormulas(out); touched++; }
    }
    SpreadsheetApp.flush();
    L.push('запись: ' + Math.round((new Date().getTime() - t0) / 1000) + ' с от старта');

    // ПРОВЕРКА — два чтения, один пересчёт.
    var vA = wide.getValues(), fA = wide.getFormulas();
    var wrongAfter = 0, notFormula = 0;
    for (var k3 = 0; k3 < plan.length; k3++) {
      for (var r4 = 0; r4 < R75.DAYS; r4++) {
        var e2 = R75_DOW[Number(Utilities.formatDate(vals[r4][plan[k3].dc - 1], tz, 'u')) - 1];
        if (String(vA[r4][plan[k3].wc - 1]).trim().toLowerCase() !== e2) wrongAfter++;
        if (String(fA[r4][plan[k3].wc - 1]).indexOf('WEEKDAY(') < 0) notFormula++;
      }
    }
    var we = [];
    for (var r5 = 0; r5 < R75.DAYS; r5++) {
      var u = Number(Utilities.formatDate(vals[r5][R75.B0 - 1], tz, 'u'));
      if (u > 5) we.push(r75ymd_(r5).slice(8) + '.09 ' + R75_DOW[u - 1]);
    }
    L.push('');
    L.push('формул записано: ' + written + ' в ' + touched + ' колонках (0 = уже стояли, повторный прогон)');
    L.push('L737 ссылается на: ' + r75col_(R75.B0) + '737 (решение владельца: L←M, а не L←B)');
    L.push('выходные сентября по дате: ' + we.join(' · '));
    L.push('всего: ' + Math.round((new Date().getTime() - t0) / 1000) + ' с');
    L.push('');
    L.push('WEEKDAY COLUMNS DETECTED ... ' + plan.length + (plan.length === 26 ? ' (PASS)' : ' (ожидалось 26 — FAIL)'));
    L.push('WEEKDAY FORMULAS WRITTEN ... ' + (notFormula === 0 ? 'PASS' : 'FAIL') + ' (ячеек без формулы: ' + notFormula + ')');
    L.push('WEEKDAY ERRORS ............. ' + (wrongAfter + notFormula));
    L.push('PHASE 1 = ' + (wrongAfter === 0 && notFormula === 0 && plan.length === 26 && stray.length === 0 ? 'PASS' : 'FAIL'));
  } catch (e) { L.push('ОШИБКА: ' + e + (e && e.stack ? '\n' + e.stack : '')); }
  r75out_(L);
}

// ==================== PHASE 2 — СТИЛЬ ==============================================

function r75style() {
  var L = ['=== R7.5 PHASE 2 · r75style · ' + R75.VER + ' ==='];
  try {
    var ctx = r75open_(L); if (!ctx) return r75out_(L);
    var sh = ctx.sh, mirror = ctx.mirror;
    if (!r75get_(R75.BASE)) { L.push('СТОП: нет базового снимка — сначала r75calendar'); return r75out_(L); }

    // Эталон стиля AUTO берём из самого листа: колонка O «Показы».
    var rO = sh.getRange(R75.FIRST, 15, R75.DAYS, 1);
    var autoBg = r75mode_(r75flat_(rO.getBackgrounds()));
    // Чёрный читаемый текст — требование владельца. Режим колонки O брать нельзя: у пустых
    // будущих дней там светло-серый #c0c0c0, он побеждает по частоте и делает цифры нечитаемыми.
    var oFc = r75mode_(r75flat_(rO.getFontColors())), autoFc = '#000000';
    var autoHdr = sh.getRange(R75.HDR, 15).getBackground();
    L.push('стандарт AUTO из O «Показы»: фон данных ' + autoBg + ', шапка ' + autoHdr + '; шрифт ' + autoFc + ' (в самой O режим ' + oFc + ' — не берём)');

    var rules = sh.getConditionalFormatRules(), dimFc = R75_C.dim;
    for (var i = 0; i < rules.length; i++) {
      var bc = rules[i].getBooleanCondition();
      if (bc && String((bc.getCriteriaValues() || [])[0] || '') === '=$M737>$WB$736' && bc.getFontColor()) dimFc = bc.getFontColor();
    }
    var spec = r75spec_(autoBg, autoFc, dimFc);

    var rA = sh.getRange(R75.FIRST, 1, R75.DAYS, 1), rB = sh.getRange(R75.FIRST, 2, R75.DAYS, 1);
    var rL = sh.getRange(R75.FIRST, 12, R75.DAYS, 1), rM = sh.getRange(R75.FIRST, 13, R75.DAYS, 1);
    var rU = sh.getRange(R75.FIRST, 21, R75.DAYS, 1);
    var cols = {}, frozenC = sh.getFrozenColumns();
    [15, 16, 17, 18].forEach(function (c) {
      var g = sh.getRange(R75.FIRST, c, R75.DAYS, 1);
      cols[c] = { bg: r75flat_(g.getBackgrounds()), fc: r75flat_(g.getFontColors()), fs: r75flat_(g.getFontStyles()) };
    });
    var lBg = r75flat_(rL.getBackgrounds()), mBg = r75flat_(rM.getBackgrounds());
    var aBg = r75flat_(rA.getBackgrounds()), bBg = r75flat_(rB.getBackgrounds());
    if (!r75get_(R75.RB)) {
      r75put_(R75.RB, {
        ver: R75.VER, ts: new Date().toISOString(), frozenC: frozenC,
        L: lBg, M: mBg, A: aBg, B: bBg, U: r75flat_(rU.getNumberFormats()), cols: cols,
        P736: { bg: sh.getRange(R75.HDR, 16).getBackground(), note: sh.getRange(R75.HDR, 16).getNote() },
        R736: { bg: sh.getRange(R75.HDR, 18).getBackground(), note: sh.getRange(R75.HDR, 18).getNote() },
        spec: spec.map(function (s) { return s[0] + '|' + s[1]; })
      });
      L.push('состояние для отката сохранено в Script Properties «' + R75.RB + '»');
    } else L.push('состояние для отката уже сохранено первым прогоном — не перезаписано');

    // 2.1 Выходные: снять статичную заливку календаря августа с L и M.
    var lBase = r75mode_(lBg), mBase = r75mode_(mBg);
    var lOff = lBg.filter(function (x) { return x !== lBase; }).length;
    var mOff = mBg.filter(function (x) { return x !== mBase; }).length;
    var aBase = r75mode_(aBg), bBase = r75mode_(bBg);
    var aOff = aBg.filter(function (x) { return x !== aBase; }).length;
    var bOff = bBg.filter(function (x) { return x !== bBase; }).length;
    rL.setBackground(lBase); rM.setBackground(mBase);
    rA.setBackground(aBase); rB.setBackground(bBase);

    // 2.2 Трафик: O, P, Q, R — один спокойный стиль AUTO, чёрный прямой шрифт.
    [15, 16, 17, 18].forEach(function (c) {
      sh.getRange(R75.FIRST, c, R75.DAYS, 1).setBackground(autoBg).setFontColor(autoFc).setFontStyle('normal');
    });
    sh.getRange(R75.HDR, 16).setBackground(autoHdr).setNote(R75_NOTE_P);
    sh.getRange(R75.HDR, 18).setBackground(autoHdr).setNote(R75_NOTE_R);

    // 2.3 Оборачиваемость — целым числом.
    rU.setNumberFormat('0');

    // 2.4 Правила R7.5: свои снимаем и ставим заново в начало списка (идемпотентно).
    var mine = {}; spec.forEach(function (s) { mine[s[0] + '|' + s[1]] = 1; });
    var keep = [], dropped = 0;
    for (var q = 0; q < rules.length; q++) {
      var b2 = rules[q].getBooleanCondition();
      var key = rules[q].getRanges().map(function (x) { return x.getA1Notation(); }).join(',') + '|' +
        (b2 ? String((b2.getCriteriaValues() || [])[0] || '') : '');
      if (b2 && mine[key]) { dropped++; continue; }
      keep.push(rules[q]);
    }
    var added = spec.map(function (s) { return r75mk_(sh, s[0], s[1], s[2]); });
    sh.setConditionalFormatRules(added.concat(keep));
    SpreadsheetApp.flush();

    // 2.5 Закрепление A:B — merge A:K не трогаем (решение владельца).
    var merges = sh.getRange(1, 1, sh.getMaxRows(), 3).getMergedRanges().filter(function (m) { return m.getColumn() <= 2 && m.getLastColumn() >= 3; });
    var freeze;
    if (merges.length) freeze = 'DEFERRED — мешает объединение ' + merges.slice(0, 3).map(function (m) { return m.getA1Notation(); }).join(' ') +
      (merges.length > 3 ? ' и ещё ' + (merges.length - 3) : '') + '; merge и шапка не менялись';
    else { sh.setFrozenColumns(2); freeze = sh.getFrozenColumns() === 2 ? 'PASS' : 'FAIL'; }

    var vv = sh.getRange(R75.FIRST, 13, R75.DAYS, 24).getValues();
    var sHits = [], uz = [0, 0, 0, 0];
    for (var r = 0; r < R75.DAYS; r++) {
      var d = vv[r][0], qv = vv[r][4], sv = vv[r][6], uv = vv[r][8];
      if (typeof uv === 'number') uz[uv <= 15 ? 0 : uv <= 30 ? 1 : uv <= 60 ? 2 : 3]++;
      if (d instanceof Date && mirror instanceof Date && d.getTime() <= mirror.getTime() &&
        typeof qv === 'number' && qv > 0 && typeof sv === 'number' && (sv >= 3 || (sv >= 2 && sv * 10 >= qv * 3)))
        sHits.push('S' + (R75.FIRST + r) + '=' + sv + '/Q' + qv);
    }
    var okStyle = [15, 16, 17, 18].every(function (c) {
      var g = sh.getRange(R75.FIRST, c, R75.DAYS, 1);
      return r75flat_(g.getBackgrounds()).every(function (x) { return x === autoBg; }) &&
        r75flat_(g.getFontStyles()).every(function (x) { return x === 'normal'; });
    });
    // Повторно правила НЕ вычитываем: их на листе 4,3 тыс., каждое чтение — десятки секунд.
    var afterN = added.length + keep.length;

    L.push('');
    L.push('выходные: статичной заливки августа снято — A ' + aOff + ', B ' + bOff + ', L ' + lOff + ', M ' + mOff + ' яч.; подсветка теперь УФ ОТ ДАТЫ на A737:B766 (мастер) и L737:M766 (крем)');
    L.push('трафик O/P/Q/R: единый стиль AUTO ' + (okStyle ? 'OK' : 'НЕТ') + '; шапки P736/R736 → ' + autoHdr + ', подсказки обновлены');
    L.push('оборачиваемость: формат «0» (целое); зоны ≤15 / 16–30 / 31–60 / >60 = ' + uz.join(' / ') + ' дней');
    L.push('отмены: S ≥ 3 ИЛИ (S ≥ 2 И S/Q ≥ 30 %), закрытые дни, Q > 0 — срабатываний ' + sHits.length + (sHits.length ? ': ' + sHits.join(' ') : ''));
    L.push('правил R7.5: снято прошлых ' + dropped + ', поставлено ' + added.length + ' в начало; всего правил ' + afterN + ' (было ' + rules.length + ')');
    L.push('FREEZE A:B ...... ' + freeze);
    L.push('PHASE 2 = ' + (okStyle ? 'PASS' : 'FAIL') + ' — экономика и формулы не менялись');
  } catch (e) { L.push('ОШИБКА: ' + e + (e && e.stack ? '\n' + e.stack : '')); }
  r75out_(L);
}

// ==================== PHASE 3 — QA =================================================

function r75qa() {
  var L = ['=== R7.5 PHASE 3 · r75qa · ' + R75.VER + ' ==='];
  try {
    var ctx = r75open_(L); if (!ctx) return r75out_(L);
    var sh = ctx.sh, tz = ctx.tz;
    var base = r75get_(R75.BASE);
    if (!base) { L.push('СТОП: нет базового снимка — сначала r75calendar'); return r75out_(L); }
    L.push('эталон от ' + base.ts + ' (' + base.ver + ')');

    var now = r75snapshot_(sh, tz);

    var valReg = [], formReg = [], okForm = 0;
    for (var y = 0; y < base.vals.length; y++) {
      for (var x = 0; x < base.vals[y].length; x++) {
        var row = R75.TOP + y, col = R75.B0 + x, a1 = r75col_(col) + row;
        var approved = (col === R75.B0 + R75.BW - 1 && row >= R75.FIRST && row < R75.FIRST + R75.DAYS);
        if (base.fh[y][x] !== now.fh[y][x]) { if (approved) okForm++; else formReg.push(a1); }
        if (base.vals[y][x] !== now.vals[y][x] && !approved) valReg.push(a1 + ': [' + base.vals[y][x] + '] -> [' + now.vals[y][x] + ']');
      }
    }

    // Сравнивать ошибки можно только со снимком той же геометрии: у эталона, снятого прежней
    // версией, массив ошибок был по всем 589 колонкам, а не по блоку крема.
    var errNew = [], errCmp = (base.errc.length === now.errc.length);
    if (errCmp) for (var c = 0; c < base.errc.length; c++) if (now.errc[c] > base.errc[c]) errNew.push(r75col_(R75.B0 + c) + ' +' + (now.errc[c] - base.errc[c]));
    var sum = function (a) { var t = 0; for (var i = 0; i < a.length; i++) t += a[i]; return t; };
    var errTotal = sum(now.errc), errBase = sum(base.errc);

    var cfReg = [], shift = now.cf.length - base.cf.length;
    if (shift < 0) cfReg.push('правил стало меньше: ' + now.cf.length + ' < ' + base.cf.length);
    else for (var z = 0; z < base.cf.length; z++) if (now.cf[z + shift] !== base.cf[z]) cfReg.push('#' + z);

    var wide = sh.getRange(R75.FIRST, 1, R75.DAYS, R75.NC);
    var vals = wide.getValues(), form = wide.getFormulas();
    var plan = [{ wc: 1, dc: 2 }, { wc: 12, dc: R75.B0 }];
    for (var b = 0; b < R75.NB; b++) plan.push({ wc: R75.B0 + b * R75.BW + R75.BW - 1, dc: R75.B0 + b * R75.BW });
    var wkErr = 0, wkStatic = 0;
    plan.forEach(function (p) {
      for (var r = 0; r < R75.DAYS; r++) {
        var dv = vals[r][p.dc - 1];
        var exp = (dv instanceof Date) ? R75_DOW[Number(Utilities.formatDate(dv, tz, 'u')) - 1] : null;
        if (exp === null || String(vals[r][p.wc - 1]).trim().toLowerCase() !== exp) wkErr++;
        if (String(form[r][p.wc - 1]).indexOf('WEEKDAY(') < 0) wkStatic++;
      }
    });

    var fn = [];
    for (var r2 = 0; r2 < R75.DAYS; r2++) {
      var pv = String(vals[r2][15]), rv = String(vals[r2][17]);
      if (pv !== '' || rv !== '') fn.push(r75ymd_(r2).slice(8) + '.09 П' + (pv === '' ? '—' : pv) + '/К' + (rv === '' ? '—' : rv));
    }

    L.push('');
    L.push('=== ЗНАЧЕНИЯ И ФОРМУЛЫ M735:AJ768 (заказы, отмены, остатки, реклама, цена, комиссия, логистика, хранение, налог, прибыль, воронка R7) ===');
    L.push('одобренных изменений формул (AJ737:AJ766, день недели): ' + okForm);
    L.push('VALUE REGRESSION = ' + valReg.length + (valReg.length ? ' <-- ' + valReg.slice(0, 15).join(' | ') : ''));
    L.push('UNAPPROVED FORMULA REGRESSION = ' + formReg.length + (formReg.length ? ' <-- ' + formReg.slice(0, 15).join(' | ') : ''));
    L.push('');
    L.push('=== ДЕНЬ НЕДЕЛИ (26 колонок × 30 дней = ' + (plan.length * R75.DAYS) + ' ячеек) ===');
    L.push('WEEKDAY ERRORS = ' + wkErr + ' | статических (без формулы) = ' + wkStatic);
    L.push('');
    L.push('=== УФ И ОШИБКИ ===');
    L.push('правил УФ: было ' + base.cf.length + ', стало ' + now.cf.length + ' (+' + shift + ' — правила R7.5 сверху)');
    L.push('CF REGRESSION (изменённые старые правила) = ' + cfReg.length + (cfReg.length ? ' <-- ' + cfReg.slice(0, 15).join(' ') : ''));
    L.push(errCmp
      ? 'ошибок формул в блоке M735:AJ768: было ' + errBase + ', стало ' + errTotal + '; новых колонок с ошибками ' + errNew.length + (errNew.length ? ' <-- ' + errNew.slice(0, 15).join(' ') : '')
      : 'ошибок формул в блоке M735:AJ768 сейчас: ' + errTotal + '. Сравнение с эталоном НЕ проводится: он снят прежней геометрией (' + base.errc.length + ' колонок против ' + now.errc.length + ')');
    L.push('');
    L.push('=== ВОРОНКА R7 (P переходы / R корзины, крем для рук) ===');
    L.push(fn.join(' · ') || 'пусто');
    L.push('');
    L.push('=== ИТОГ R7.5 ===');
    L.push('VALUE REGRESSION ............... ' + valReg.length + (valReg.length === 0 ? ' PASS' : ' FAIL'));
    L.push('UNAPPROVED FORMULA REGRESSION .. ' + formReg.length + (formReg.length === 0 ? ' PASS' : ' FAIL'));
    L.push('WEEKDAY ERRORS ................. ' + wkErr + (wkErr === 0 ? ' PASS' : ' FAIL'));
    L.push('CF REGRESSION .................. ' + cfReg.length + (cfReg.length === 0 ? ' PASS' : ' FAIL'));
    L.push('ERRORS IN BLOCK ................ ' + errTotal + (errCmp ? ' (было ' + errBase + ')' : ' (эталон несопоставим, сравнения нет)') + (errTotal === 0 ? ' PASS' : (errCmp && errTotal <= errBase ? ' PASS' : ' FAIL')));
    L.push('PHASE 3 = ' + (valReg.length === 0 && formReg.length === 0 && wkErr === 0 && cfReg.length === 0 && (errTotal === 0 || (errCmp && errTotal <= errBase)) ? 'PASS' : 'FAIL'));
  } catch (e) { L.push('ОШИБКА: ' + e + (e && e.stack ? '\n' + e.stack : '')); }
  r75out_(L);
}

// ==================== ОТКАТ ВИЗУАЛА ================================================

function r75rollback() {
  var L = ['=== R7.5 ROLLBACK (только визуал r75style) ==='];
  try {
    var rb = r75get_(R75.RB);
    if (!rb) { L.push('СТОП: нет сохранённого состояния — r75style в этом проекте не запускался'); return r75out_(L); }
    var sh = SpreadsheetApp.openById(R75.SSID).getSheetByName(R75.SH);
    L.push('состояние от ' + rb.ts + ' (' + rb.ver + ')');

    var mine = {}; rb.spec.forEach(function (k) { mine[k] = 1; });
    var rules = sh.getConditionalFormatRules(), keep = [], removed = 0;
    for (var i = 0; i < rules.length; i++) {
      var bc = rules[i].getBooleanCondition();
      var key = rules[i].getRanges().map(function (x) { return x.getA1Notation(); }).join(',') + '|' +
        (bc ? String((bc.getCriteriaValues() || [])[0] || '') : '');
      if (bc && mine[key]) { removed++; continue; }
      keep.push(rules[i]);
    }
    sh.setConditionalFormatRules(keep);

    var one = function (v) { return [v]; };
    sh.getRange(R75.FIRST, 12, R75.DAYS, 1).setBackgrounds(rb.L.map(one));
    sh.getRange(R75.FIRST, 13, R75.DAYS, 1).setBackgrounds(rb.M.map(one));
    if (rb.A) sh.getRange(R75.FIRST, 1, R75.DAYS, 1).setBackgrounds(rb.A.map(one));
    if (rb.B) sh.getRange(R75.FIRST, 2, R75.DAYS, 1).setBackgrounds(rb.B.map(one));
    sh.getRange(R75.FIRST, 21, R75.DAYS, 1).setNumberFormats(rb.U.map(one));
    [15, 16, 17, 18].forEach(function (c) {
      var s = rb.cols[c]; if (!s) return;
      sh.getRange(R75.FIRST, c, R75.DAYS, 1).setBackgrounds(s.bg.map(one)).setFontColors(s.fc.map(one)).setFontStyles(s.fs.map(one));
    });
    sh.getRange(R75.HDR, 16).setBackground(rb.P736.bg).setNote(rb.P736.note);
    sh.getRange(R75.HDR, 18).setBackground(rb.R736.bg).setNote(rb.R736.note);
    if (sh.getFrozenColumns() !== rb.frozenC) sh.setFrozenColumns(rb.frozenC);
    SpreadsheetApp.flush();
    PropertiesService.getScriptProperties().deleteProperty(R75.RB + '#n');

    L.push('правил R7.5 удалено: ' + removed + ' | заливка L/M, стиль O/P/Q/R, шапки, формат U и закрепление — как до r75style');
    L.push('формулы дня недели оставлены: это исправление ошибки, а не оформление');
  } catch (e) { L.push('ОШИБКА: ' + e + (e && e.stack ? '\n' + e.stack : '')); }
  r75out_(L);
}

// ==================== ПОМОЩНИКИ ====================================================

function r75dowFormula_(dateCol, row) {
  return '=IF($' + dateCol + row + '="";"";CHOOSE(WEEKDAY($' + dateCol + row + ';2);"пн";"вт";"ср";"чт";"пт";"сб";"вс"))';
}

function r75spec_(autoBg, autoFc, dimFc) {
  return [
    ['A737:B766', '=AND($B737<>"";WEEKDAY($B737;2)>5;$B737>$WB$736)', { bg: R75_C.weekend, fc: dimFc }],
    ['A737:B766', '=AND($B737<>"";WEEKDAY($B737;2)>5)', { bg: R75_C.weekend }],
    ['L737:M766', '=AND($M737<>"";WEEKDAY($M737;2)>5;$M737>$WB$736)', { bg: R75_C.weekend, fc: dimFc }],
    ['L737:M766', '=AND($M737<>"";WEEKDAY($M737;2)>5)', { bg: R75_C.weekend }],
    ['R737:R766', '=R737<>""', { bg: autoBg, fc: autoFc }],
    ['R737:R766', '=R737=""', { bg: autoBg }],
    ['U737:U766', '=AND(ISNUMBER(U737);U737<=15)', { bg: R75_C.uCritBg, fc: R75_C.uCritFc }],
    ['U737:U766', '=AND(ISNUMBER(U737);U737>15;U737<=30)', { bg: R75_C.uWarnBg, fc: R75_C.uWarnFc }],
    ['U737:U766', '=AND(ISNUMBER(U737);U737>30;U737<=60)', { bg: R75_C.uOkBg, fc: R75_C.uOkFc }],
    ['U737:U766', '=AND(ISNUMBER(U737);U737>60)', { bg: R75_C.uOverBg, fc: R75_C.uOverFc }],
    ['V737:V766', '=AND(ISNUMBER(V737);V737<0)', { bg: R75_C.white, fc: R75_C.red }],
    ['V737:V766', '=AND(ISNUMBER(V737);V737>0)', { bg: R75_C.white, fc: R75_C.green }],
    ['V737:V766', '=AND(ISNUMBER(V737);V737=0)', { bg: R75_C.white }],
    ['V737:V766', '=NOT(ISNUMBER(V737))', { bg: R75_C.white }],
    ['W737:W766', '=AND(ISNUMBER(W737);W737>0)', { fc: R75_C.green }],
    ['AI737:AI766', '=AND(ISNUMBER(AI737);AI737<0)', { fc: R75_C.red }],
    ['AI737:AI766', '=AND(ISNUMBER(AI737);AI737>0)', { fc: R75_C.green }],
    ['S737:S766', '=AND($M737<=$WB$736;ISNUMBER(Q737);Q737>0;ISNUMBER(S737);OR(S737>=3;AND(S737>=2;S737*10>=Q737*3)))', { bg: R75_C.sBg, fc: R75_C.sFc }]
  ];
}

function r75mk_(sh, a1, f, st) {
  var b = SpreadsheetApp.newConditionalFormatRule().whenFormulaSatisfied(f).setRanges([sh.getRange(a1)]);
  if (st.bg) b.setBackground(st.bg);
  if (st.fc) b.setFontColor(st.fc);
  return b.build();
}

function r75snapshot_(sh, tz) {
  var rows = R75.BOT - R75.TOP + 1;
  var blk = sh.getRange(R75.TOP, R75.B0, rows, R75.BW);
  var v = blk.getValues(), f = blk.getFormulas(), d = blk.getDisplayValues();
  var vals = [], fh = [], errc = [];
  for (var x0 = 0; x0 < R75.BW; x0++) errc.push(0);
  for (var y = 0; y < rows; y++) {
    var vr = [], fr = [];
    for (var x = 0; x < R75.BW; x++) {
      vr.push(r75ser_(v[y][x], tz)); fr.push(r75h_(f[y][x]));
      if (/^#(REF!|VALUE!|NAME\?|DIV\/0!|N\/A|ERROR!|NUM!|NULL!)/.test(String(d[y][x]))) errc[x]++;
    }
    vals.push(vr); fh.push(fr);
  }
  return { ver: R75.VER, ts: new Date().toISOString(), vals: vals, fh: fh, errc: errc, cf: sh.getConditionalFormatRules().map(r75fp_) };
}

function r75fp_(rule) {
  var a = [rule.getRanges().map(function (x) { return x.getA1Notation(); }).join(',')];
  var b = rule.getBooleanCondition(), g = rule.getGradientCondition();
  if (b) a.push('B', String(b.getCriteriaType()), JSON.stringify(b.getCriteriaValues()), b.getBackground(), b.getFontColor(), b.getBold(), b.getItalic());
  if (g) a.push('G', g.getMinColor(), String(g.getMinType()), g.getMinValue(), g.getMidColor(), String(g.getMidType()), g.getMidValue(), g.getMaxColor(), String(g.getMaxType()), g.getMaxValue());
  return r75h_(JSON.stringify(a));
}

function r75ser_(v, tz) {
  if (v instanceof Date) return 'D' + Utilities.formatDate(v, tz, 'yyyy-MM-dd');
  if (typeof v === 'number') return 'N' + (Math.round(v * 1e6) / 1e6);
  if (v === '' || v === null || v === undefined) return '';
  return 'S' + String(v);
}

function r75h_(s) {
  if (!s) return '';
  var b = Utilities.computeDigest(Utilities.DigestAlgorithm.MD5, String(s), Utilities.Charset.UTF_8), o = '';
  for (var i = 0; i < 5; i++) { var x = (b[i] + 256) % 256; o += (x < 16 ? '0' : '') + x.toString(16); }
  return o;
}

// Script Properties: значение ограничено 9 КБ, поэтому пишем чанками по 4000 символов.
function r75put_(key, obj) {
  var p = PropertiesService.getScriptProperties(), s = JSON.stringify(obj), all = p.getProperties();
  for (var k in all) if (k.indexOf(key + '#') === 0) p.deleteProperty(k);
  var n = Math.ceil(s.length / 4000);
  for (var i = 0; i < n; i++) p.setProperty(key + '#' + i, s.substring(i * 4000, (i + 1) * 4000));
  p.setProperty(key + '#n', String(n));
}

function r75get_(key) {
  var p = PropertiesService.getScriptProperties(), n = Number(p.getProperty(key + '#n') || 0);
  if (!n) return null;
  var s = '';
  for (var i = 0; i < n; i++) s += p.getProperty(key + '#' + i) || '';
  try { return JSON.parse(s); } catch (e) { return null; }
}

function r75open_(L) {
  var ss = SpreadsheetApp.openById(R75.SSID), sh = ss.getSheetByName(R75.SH);
  if (!sh) { L.push('СТОП: нет листа ' + R75.SH); return null; }
  var tz = ss.getSpreadsheetTimeZone();
  if (String(sh.getRange(R75.TOP, 1).getDisplayValue()).indexOf('Сентябрь 2026') !== 0) { L.push('СТОП: A735 не «Сентябрь 2026»'); return null; }
  var hdr = sh.getRange(R75.HDR, R75.B0, 1, R75.BW).getDisplayValues()[0];
  var want = [[0, /^дата$/i], [2, /показ/i], [3, /переход/i], [4, /заказ/i], [5, /корзин/i], [6, /отмен/i], [8, /оборач/i], [9, /доходн/i], [13, /дрр/i], [22, /доходн/i]];
  for (var h = 0; h < want.length; h++) {
    if (!want[h][1].test(String(hdr[want[h][0]]).trim())) { L.push('СТОП: ' + r75col_(R75.B0 + want[h][0]) + '736 = [' + hdr[want[h][0]] + '] — карта колонок не совпала'); return null; }
  }
  var mirror = sh.getRange(R75.HDR, R75.MIR).getValue();
  L.push('книга: ' + ss.getName() + ' | лист ' + sh.getName() + ' | пояс ' + tz +
    ' | LAST_CLOSED_DATE (WB736) = ' + (mirror instanceof Date ? Utilities.formatDate(mirror, tz, 'yyyy-MM-dd') : mirror));
  return { ss: ss, sh: sh, tz: tz, mirror: mirror };
}

function r75flat_(a) { return a.map(function (x) { return x[0]; }); }

function r75mode_(arr) {
  if (!arr || !arr.length) return '#ffffff';
  var cnt = {}, best = arr[0], bn = 0;
  arr.forEach(function (x) { cnt[x] = (cnt[x] || 0) + 1; if (cnt[x] > bn) { bn = cnt[x]; best = x; } });
  return best;
}

function r75ymd_(i) { return '2026-09-' + (i < 9 ? '0' : '') + (i + 1); }

function r75col_(n) { var s = ''; while (n > 0) { var m = (n - 1) % 26; s = String.fromCharCode(65 + m) + s; n = (n - 1 - m) / 26; } return s; }

function r75out_(L) { Logger.log(L.join('\n')); }
