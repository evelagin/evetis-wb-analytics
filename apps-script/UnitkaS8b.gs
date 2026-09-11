// UNITKA 2.0 STAGE 8 · часть 2 — вычисляемые ставки, экономный визуал, сводка, QA.
// agent claude-opus-5, v8.1.0. Продолжение UnitkaS8.gs: те же константы S8 и S8_M.
//
// Диспетчер этапа — ПЕРВАЯ функция файла: редактор Apps Script выбирает её сам
// при открытии файла, а выпадающий список автоматизации не поддаётся.
function s8b() { s8qa(); }

var S8B = {
  VER: 'unitka2.0/v8.1.0',
  MIN_N: 10,                      // порог владельца: своя ставка при n >= 10 выкупов
  FIN: 'wb_raw.V_WB_FINANCE_CANONICAL',
  PRJ: 'project-fa311fc0-4d87-4781-986',
  RB_STYLE: 'S8_STYLE_RB', RB_CF: 'S8_CF_RB', RB_RATE: 'S8_RATE_RB', RB_SUM: 'S8_SUM_RB'
};

// ============ §7/§8 СТАВКИ: СЧИТАЮТСЯ ИЗ ФИНОТЧЁТА, НЕ ХАРДКОД =================
// Владелец 11.09: «Не хардкодить текущие ставки». Поэтому таблица S8_PARAM из
// первого прохода здесь заменена расчётом: окно D-30..D-1 берётся от зеркала
// LAST_CLOSED_DATE, ставки пересчитываются при каждом прогоне.
//   логистика SKU = (Логистика + Доставка) ₽ / выкупы шт
//   комиссия SKU  = тариф WB (commission_percent, взвешенный по базе продавца)
//                   + эквайринг (acquiring_fee / база продавца)
//   n < MIN_N     -> обе ставки общемагазинные (решение владельца)

function s8finsql_(d1, d2) {
  var Q = String.fromCharCode(96), T = Q + S8B.PRJ + '.' + S8B.FIN + Q;
  var W = " WHERE _rr_date BETWEEN DATE '" + d1 + "' AND DATE '" + d2 + "'";
  var SALE = "'Продажа'", LG = "('Логистика','Доставка')";
  return 'WITH f AS (SELECT SAFE_CAST(wb_nm_id AS INT64) nm, supplier_oper_name son,' +
    ' SAFE_CAST(quantity AS NUMERIC) q, SAFE_CAST(logistics_amount AS NUMERIC) lg,' +
    ' SAFE_CAST(acquiring_fee AS NUMERIC) acq, SAFE_CAST(retail_price_withdisc_rub AS NUMERIC) rev,' +
    ' SAFE_CAST(commission_percent AS NUMERIC) cp FROM ' + T + W + '),' +
    ' s AS (SELECT nm, SUM(IF(son=' + SALE + ',q,0)) n, SUM(IF(son IN ' + LG + ',lg,0)) lg,' +
    ' SUM(IF(son=' + SALE + ',acq,0)) acq, SUM(IF(son=' + SALE + ',rev*q,0)) base,' +
    ' SUM(IF(son=' + SALE + ',cp*rev*q,0)) cpw FROM f WHERE nm IS NOT NULL AND nm>0 GROUP BY 1)' +
    ' SELECT CAST(nm AS STRING), CAST(n AS STRING), CAST(ROUND(SAFE_DIVIDE(lg,NULLIF(n,0)),4) AS STRING),' +
    ' CAST(ROUND(SAFE_DIVIDE(cpw,NULLIF(base,0))/100,6) AS STRING),' +
    ' CAST(ROUND(SAFE_DIVIDE(acq,NULLIF(base,0)),6) AS STRING) FROM s' +
    " UNION ALL SELECT '0', CAST(SUM(n) AS STRING), CAST(ROUND(SAFE_DIVIDE(SUM(lg),NULLIF(SUM(n),0)),4) AS STRING)," +
    ' CAST(ROUND(SAFE_DIVIDE(SUM(cpw),NULLIF(SUM(base),0))/100,6) AS STRING),' +
    ' CAST(ROUND(SAFE_DIVIDE(SUM(acq),NULLIF(SUM(base),0)),6) AS STRING) FROM s';
}

function s8win_(sh, tz) {
  var m = sh.getRange(S8.HDR, S8.MIR).getValue();
  if (!(m instanceof Date)) return null;
  return {
    d2: Utilities.formatDate(m, tz, 'yyyy-MM-dd'),
    d1: Utilities.formatDate(new Date(m.getTime() - 29 * 86400000), tz, 'yyyy-MM-dd'),
    date: m
  };
}

function s8fin_(d1, d2, L) {
  var rows = r7query_(s8finsql_(d1, d2)), per = {}, store = null;
  for (var i = 0; i < rows.length; i++) {
    var r = rows[i];
    var o = { n: Number(r[1]) || 0, log: Number(r[2]), base: Number(r[3]), acq: Number(r[4]) };
    o.comm = (isNaN(o.base) ? 0 : o.base) + (isNaN(o.acq) ? 0 : o.acq);
    if (String(r[0]) === '0') store = o; else per[String(r[0])] = o;
  }
  if (!store || !store.n) throw new Error('финотчёт за окно ' + d1 + '..' + d2 + ' пуст — ставки не считаются');
  if (L) {
    L.push('ставки: источник ' + S8B.FIN + ', окно ' + d1 + '..' + d2 + ', SKU в финотчёте ' + Object.keys(per).length);
    L.push('магазин: выкупов ' + store.n + ', логистика ' + store.log.toFixed(4) + ' руб/ед, ' +
      'комиссия ' + store.comm.toFixed(6) + ' (тариф ' + store.base.toFixed(6) + ' + эквайринг ' + store.acq.toFixed(6) + ')');
  }
  return {
    store: store,
    get: function (nm) {
      var p = per[nm] || null, n = p ? p.n : 0;
      var own = !!(p && n >= S8B.MIN_N && !isNaN(p.log) && p.log > 0 && !isNaN(p.comm) && p.comm > 0);
      return {
        n: n, ownLog: p && !isNaN(p.log) ? p.log : null,
        log: Math.round((own ? p.log : store.log) * 100) / 100,
        comm: Math.round((own ? p.comm : store.comm) * 1e6) / 1e6,
        src: own ? 'своя' : 'магазин',
        conf: n >= 30 ? 'HIGH' : (n >= S8B.MIN_N ? 'MEDIUM' : 'LOW')
      };
    }
  };
}

/** Разбор шапки: [{i, st, nm, t}] по всем 24 блокам. */
function s8blocks_(head) {
  var out = [];
  for (var b = 0; b < S8.NB; b++) {
    var st = S8.B0 + b * S8.BW, t = '';
    for (var c = 0; c < S8.BW && !t; c++) t = String(head[st - 1 + c] || '').trim();
    out.push({ i: b + 1, st: st, nm: (t.match(/\d{6,12}/) || [''])[0], t: t });
  }
  return out;
}

// ==================== §7/§8 ЗАПИСЬ ВЫЧИСЛЕННЫХ СТАВОК ========================

function s8rates() {
  var L = ['=== STAGE 8 §7/§8 · s8rates · ' + S8B.VER + ' ==='];
  try {
    var t0 = new Date().getTime();
    var ss = SpreadsheetApp.openById(S8.SSID), sh = ss.getSheetByName(S8.SH), tz = ss.getSpreadsheetTimeZone();
    var w = s8win_(sh, tz);
    if (!w) { L.push('СТОП: зеркало LAST_CLOSED_DATE не дата'); return s8out_(L); }
    var fin = s8fin_(w.d1, w.d2, L);

    var head = sh.getRange(S8.TOP, 1, 1, S8.NC).getDisplayValues()[0];
    var wide = sh.getRange(S8.FIRST, 1, S8.DAYS, S8.NC), cur = wide.getValues();
    var bl = s8blocks_(head), plan = [], rb = [], tbl = [], noNm = [];

    for (var b = 0; b < bl.length; b++) {
      var st = bl[b].st, nm = bl[b].nm;
      if (!nm) { noNm.push('#' + bl[b].i); continue; }
      var f = fin.get(nm);
      tbl.push([nm, f.n, f.log, f.comm, f.src, f.conf, bl[b].t.substr(0, 34)]);
      var pr = [[S8_M.logistics, f.log], [S8_M.commission, f.comm]];
      for (var q = 0; q < pr.length; q++) {
        var col = st + pr[q][0], want = pr[q][1], out = [], diff = false;
        for (var d = 0; d < S8.DAYS; d++) {
          out.push([want]);
          if (Math.abs(Number(cur[d][col - 1]) - want) > 1e-9) diff = true;
        }
        if (diff) {
          plan.push({ col: col, vals: out });
          for (var d2 = 0; d2 < S8.DAYS; d2++) rb.push([S8.FIRST + d2, col, cur[d2][col - 1]]);
        }
      }
    }
    L.push('блоков: ' + tbl.length + ' | без nmID: ' + noNm.length + (noNm.length ? ' ' + noNm.join(' ') : ''));
    L.push('колонок к записи: ' + plan.length + ' | ячеек в откате: ' + rb.length);
    if (rb.length && !r75get_(S8B.RB_RATE)) { r75put_(S8B.RB_RATE, { ts: new Date().toISOString(), ver: S8B.VER, cells: rb }); L.push('откат сохранён: ' + S8B.RB_RATE); }

    for (var p = 0; p < plan.length; p++) sh.getRange(S8.FIRST, plan[p].col, S8.DAYS, 1).setValues(plan[p].vals);
    SpreadsheetApp.flush();

    L.push('');
    L.push('nmID | n | логистика | комиссия | источник | доверие');
    tbl.sort(function (a, b2) { return b2[1] - a[1]; });
    for (var i = 0; i < tbl.length; i++)
      L.push(tbl[i][0] + ' | ' + tbl[i][1] + ' | ' + tbl[i][2].toFixed(2) + ' | ' + tbl[i][3].toFixed(6) +
        ' | ' + tbl[i][4] + ' | ' + tbl[i][5] + ' | ' + tbl[i][6]);

    var after = wide.getValues(), bad = 0;
    for (var b3 = 0; b3 < bl.length; b3++) {
      if (!bl[b3].nm) continue;
      var f3 = fin.get(bl[b3].nm);
      for (var d3 = 0; d3 < S8.DAYS; d3++) {
        if (Math.abs(Number(after[d3][bl[b3].st + S8_M.logistics - 1]) - f3.log) > 1e-9) bad++;
        if (Math.abs(Number(after[d3][bl[b3].st + S8_M.commission - 1]) - f3.comm) > 1e-9) bad++;
      }
    }
    L.push('');
    L.push('RATES WRITTEN = ' + (plan.length === 0 ? 'уже актуальны (идемпотентно)' : plan.length + ' колонок'));
    L.push('RATES VERIFY = ' + (bad === 0 ? 'PASS' : 'FAIL (' + bad + ')') + ' | ' + Math.round((new Date().getTime() - t0) / 1000) + ' с');
  } catch (e) { L.push('ОШИБКА: ' + e + (e && e.stack ? '\n' + e.stack : '')); }
  s8out_(L);
}

// ==================== §3 ВИЗУАЛ: СТАТИКА ====================================
// Индивидуальность товара сохраняется: строки 735/736 (заголовок и шапка блока)
// не трогаются вообще, а фон трафика берётся из СОБСТВЕННОЙ колонки «Показы»
// каждого блока — у каждого SKU остаётся его палитра.

var S8C = {
  weekend: '#fcefe3', dim: '#b7b7b7', white: '#ffffff', black: '#000000',
  red: '#cc0000', green: '#38761d', sBg: '#fce8e6', sFc: '#a61c00',
  uCritBg: '#fce8e6', uCritFc: '#a61c00', uWarnBg: '#fff2cc', uWarnFc: '#7f6000',
  uOkBg: '#e6f4ea', uOkFc: '#274e13', uOverBg: '#efefef', uOverFc: '#434343'
};

function s8style() {
  var L = ['=== STAGE 8 §3 · s8style (статика) · ' + S8B.VER + ' ==='];
  try {
    var t0 = new Date().getTime();
    var ss = SpreadsheetApp.openById(S8.SSID), sh = ss.getSheetByName(S8.SH);
    // ЧТЕНИЕ — всё сразу, ДО первой записи (иначе книга пересчитывается на каждой).
    var head = sh.getRange(S8.TOP, 1, 1, S8.NC).getDisplayValues()[0];
    var wide = sh.getRange(S8.FIRST, 1, S8.DAYS, S8.NC);
    var bg = wide.getBackgrounds(), nf = wide.getNumberFormats();
    var bl = s8blocks_(head);

    var colOf = function (c) { var a = []; for (var r = 0; r < S8.DAYS; r++) a.push(bg[r][c - 1]); return a; };
    var nfOf = function (c) { var a = []; for (var r = 0; r < S8.DAYS; r++) a.push([nf[r][c - 1]]); return a; };

    // Единый стандарт трафика берётся из ЭТАЛОНА (блок крема), а не из моды каждого
    // блока: у блоков 2-24 там августовский серый, а не палитра товара. Индивидуальность
    // товара живёт в строках 735/736, которые не трогаются вовсе.
    var REF_AUTO = r75mode_(colOf(S8.B0 + S8_M.views));
    var rb = { ver: S8B.VER, ts: new Date().toISOString(), cols: {}, nf: {} };
    var tasks = [], autos = [];
    var pairs = [{ st: 2 }];            // сводка: A день недели, B дата
    for (var b = 0; b < bl.length; b++) pairs.push({ st: bl[b].st, blk: bl[b] });

    for (var p = 0; p < pairs.length; p++) {
      var dc = pairs[p].st, wc = dc - 1;
      var dBase = r75mode_(colOf(dc)), wBase = r75mode_(colOf(wc));
      rb.cols[dc] = colOf(dc); rb.cols[wc] = colOf(wc);
      if (dBase === wBase) tasks.push({ c: wc, n: 2, bg: dBase });
      else { tasks.push({ c: wc, n: 1, bg: wBase }); tasks.push({ c: dc, n: 1, bg: dBase }); }
      if (!pairs[p].blk) continue;
      var st = pairs[p].blk.st, auto = REF_AUTO;
      autos.push({ i: pairs[p].blk.i, nm: pairs[p].blk.nm, st: st, auto: auto });
      for (var t = 0; t < 4; t++) rb.cols[st + S8_M.views + t] = colOf(st + S8_M.views + t);
      tasks.push({ c: st + S8_M.views, n: 4, bg: auto, fc: S8C.black, fs: 'normal' });
      rb.nf[st + S8_M.turnover] = nfOf(st + S8_M.turnover);
      tasks.push({ c: st + S8_M.turnover, n: 1, nf: '0' });
    }

    // Сводка C..H — те же метрики воронки, что и в блоках: спокойный фон, чёрный текст
    // вместо легаси-градиента. Откат отдельным ключом: снимок блоков уже зафиксирован.
    var rb2 = { ver: S8B.VER, ts: new Date().toISOString(), cols: {} };
    for (var sc = 3; sc <= 8; sc++) rb2.cols[sc] = colOf(sc);
    if (!r75get_('S8_STYLE_RB2')) { r75put_('S8_STYLE_RB2', rb2); L.push('откат статики сводки сохранён: S8_STYLE_RB2'); }
    tasks.push({ c: 3, n: 6, bg: REF_AUTO, fc: S8C.black, fs: 'normal' });
    if (!r75get_(S8B.RB_STYLE)) { r75put_(S8B.RB_STYLE, rb); L.push('откат статики сохранён: ' + S8B.RB_STYLE); }
    else L.push('откат статики уже сохранён первым прогоном — не перезаписан');

    // ЗАПИСЬ
    for (var k = 0; k < tasks.length; k++) {
      var tk = tasks[k], rg = sh.getRange(S8.FIRST, tk.c, S8.DAYS, tk.n);
      if (tk.nf) rg.setNumberFormat(tk.nf);
      else { rg.setBackground(tk.bg); if (tk.fc) rg.setFontColor(tk.fc); if (tk.fs) rg.setFontStyle(tk.fs); }
    }
    SpreadsheetApp.flush();
    L.push('операций записи: ' + tasks.length + ' | ' + Math.round((new Date().getTime() - t0) / 1000) + ' с');

    var pal = {};
    for (var a = 0; a < autos.length; a++) pal[autos[a].auto] = (pal[autos[a].auto] || 0) + 1;
    L.push('');
    L.push('фон трафика — эталон крема ' + REF_AUTO + ' на все блоки:');
    for (var kk in pal) L.push('  ' + kk + ' — блоков ' + pal[kk]);
    L.push('заголовки 735 и шапки 736 НЕ менялись — индивидуальность товара сохранена');

    var bg2 = wide.getBackgrounds(), nf2 = wide.getNumberFormats(), okA = 0, okN = 0;
    for (var a2 = 0; a2 < autos.length; a2++) {
      var s2 = autos[a2].st, good = true;
      for (var r2 = 0; r2 < S8.DAYS; r2++)
        for (var c2 = 0; c2 < 4; c2++) if (bg2[r2][s2 + S8_M.views + c2 - 1] !== autos[a2].auto) good = false;
      if (good) okA++;
      var goodN = true;
      for (var r3 = 0; r3 < S8.DAYS; r3++) if (nf2[r3][s2 + S8_M.turnover - 1] !== '0') goodN = false;
      if (goodN) okN++;
    }
    L.push('');
    L.push('TRAFFIC STYLE ...... ' + okA + ' / ' + autos.length + ' блоков');
    L.push('TURNOVER FORMAT 0 .. ' + okN + ' / ' + autos.length + ' блоков');
    L.push('STYLE STATIC = ' + (okA === autos.length && okN === autos.length ? 'PASS' : 'ЧАСТИЧНО — часть фона задаёт УФ, см. s8cf'));
  } catch (e) { L.push('ОШИБКА: ' + e + (e && e.stack ? '\n' + e.stack : '')); }
  s8out_(L);
}

// ==================== §3 ВИЗУАЛ: ЭКОНОМНОЕ УФ ================================
// 24 блока × 18 правил = 432 правила, если делать в лоб. Вместо этого — ОДНО
// правило на условие с 24–25 диапазонами внутри: Google Sheets разрешает
// относительные ссылки от левого верхнего угла КАЖДОГО диапазона правила.
// Абсолютным остаётся только зеркало $WB$736 (УФ не умеет ссылаться на другой
// лист, поэтому LAST_CLOSED_DATE продублирован в самой книге).

/** d — смещение первой колонки диапазона от даты блока; C(k) — буква колонки. */
var S8_SPEC = [
  { id: 'wk-dim', d: -1, w: 2, sum: 1, st: { bg: S8C.weekend, fc: 'DIM' }, fn: function (C) { return '=AND(' + C(1) + '737<>"";WEEKDAY(' + C(1) + '737;2)>5;' + C(1) + '737>$WB$736)'; } },
  { id: 'wk', d: -1, w: 2, sum: 1, st: { bg: S8C.weekend }, fn: function (C) { return '=AND(' + C(1) + '737<>"";WEEKDAY(' + C(1) + '737;2)>5)'; } },
  { id: 'cart-v', d: 2, w: 4, auto: 1, st: { bg: 'AUTO', fc: S8C.black }, fn: function (C) { return '=' + C(0) + '737<>""'; } },
  { id: 'cart-e', d: 2, w: 4, auto: 1, st: { bg: 'AUTO' }, fn: function (C) { return '=' + C(0) + '737=""'; } },
  { id: 'u1', d: 8, w: 1, st: { bg: S8C.uCritBg, fc: S8C.uCritFc }, fn: function (C) { return '=AND(ISNUMBER(' + C(0) + '737);' + C(0) + '737<=15)'; } },
  { id: 'u2', d: 8, w: 1, st: { bg: S8C.uWarnBg, fc: S8C.uWarnFc }, fn: function (C) { return '=AND(ISNUMBER(' + C(0) + '737);' + C(0) + '737>15;' + C(0) + '737<=30)'; } },
  { id: 'u3', d: 8, w: 1, st: { bg: S8C.uOkBg, fc: S8C.uOkFc }, fn: function (C) { return '=AND(ISNUMBER(' + C(0) + '737);' + C(0) + '737>30;' + C(0) + '737<=60)'; } },
  { id: 'u4', d: 8, w: 1, st: { bg: S8C.uOverBg, fc: S8C.uOverFc }, fn: function (C) { return '=AND(ISNUMBER(' + C(0) + '737);' + C(0) + '737>60)'; } },
  { id: 'v1', d: 9, w: 1, st: { bg: S8C.white, fc: S8C.red }, fn: function (C) { return '=AND(ISNUMBER(' + C(0) + '737);' + C(0) + '737<0)'; } },
  { id: 'v2', d: 9, w: 1, st: { bg: S8C.white, fc: S8C.green }, fn: function (C) { return '=AND(ISNUMBER(' + C(0) + '737);' + C(0) + '737>0)'; } },
  { id: 'v3', d: 9, w: 1, st: { bg: S8C.white }, fn: function (C) { return '=AND(ISNUMBER(' + C(0) + '737);' + C(0) + '737=0)'; } },
  { id: 'v4', d: 9, w: 1, st: { bg: S8C.white }, fn: function (C) { return '=NOT(ISNUMBER(' + C(0) + '737))'; } },
  { id: 'w1', d: 10, w: 1, st: { fc: S8C.green }, fn: function (C) { return '=AND(ISNUMBER(' + C(0) + '737);' + C(0) + '737>0)'; } },
  { id: 'ai1', d: 22, w: 1, st: { fc: S8C.red }, fn: function (C) { return '=AND(ISNUMBER(' + C(0) + '737);' + C(0) + '737<0)'; } },
  { id: 'ai2', d: 22, w: 1, st: { fc: S8C.green }, fn: function (C) { return '=AND(ISNUMBER(' + C(0) + '737);' + C(0) + '737>0)'; } },
  { id: 'sum-v', abs: 3, w: 6, st: { bg: 'AUTO', fc: S8C.black }, fn: function (C) { return '=' + C(0) + '737<>""'; } },
  { id: 'sum-e', abs: 3, w: 6, st: { bg: 'AUTO' }, fn: function (C) { return '=' + C(0) + '737=""'; } },
  { id: 's1', d: 6, w: 1, st: { bg: S8C.sBg, fc: S8C.sFc }, fn: function (C) { return '=AND(' + C(-6) + '737<=$WB$736;ISNUMBER(' + C(-2) + '737);' + C(-2) + '737>0;ISNUMBER(' + C(0) + '737);OR(' + C(0) + '737>=3;AND(' + C(0) + '737>=2;' + C(0) + '737*10>=' + C(-2) + '737*3)))'; } }
];

function s8cfmk_(sh, ranges, f, st) {
  var b = SpreadsheetApp.newConditionalFormatRule().whenFormulaSatisfied(f).setRanges(ranges);
  if (st.bg) b.setBackground(st.bg);
  if (st.fc) b.setFontColor(st.fc);
  return b.build();
}

function s8cf() {
  var L = ['=== STAGE 8 §3 · s8cf (условное форматирование) · ' + S8B.VER + ' ==='];
  try {
    var t0 = new Date().getTime();
    var ss = SpreadsheetApp.openById(S8.SSID), sh = ss.getSheetByName(S8.SH);
    var head = sh.getRange(S8.TOP, 1, 1, S8.NC).getDisplayValues()[0];
    var wide = sh.getRange(S8.FIRST, 1, S8.DAYS, S8.NC);
    var bg = wide.getBackgrounds();
    var bl = s8blocks_(head);
    var autoOf = function (st) { var a = []; for (var r = 0; r < S8.DAYS; r++) a.push(bg[r][st + S8_M.views - 1]); return r75mode_(a); };

    var rules = sh.getConditionalFormatRules();
    L.push('правил на листе до: ' + rules.length + ' (' + Math.round((new Date().getTime() - t0) / 1000) + ' с на чтение)');

    var dimFc = S8C.dim;
    for (var i = 0; i < rules.length; i++) {
      var bc0 = rules[i].getBooleanCondition();
      if (bc0 && String((bc0.getCriteriaValues() || [])[0] || '') === '=$M737>$WB$736' && bc0.getFontColor()) dimFc = bc0.getFontColor();
    }

    // Сборка: одно правило на условие, диапазоны всех блоков внутри.
    var built = [], mineF = {};
    for (var s = 0; s < S8_SPEC.length; s++) {
      var sp = S8_SPEC[s];
      var groups = {};
      if (sp.abs) groups['x'] = [{ c: sp.abs, w: sp.w }];
      if (sp.sum) groups['x'] = [{ c: 2 + sp.d, w: sp.w }];
      for (var b = 0; b < bl.length && !sp.abs; b++) {
        var key = 'x';
        if (!groups[key]) groups[key] = [];
        groups[key].push({ c: bl[b].st + sp.d, w: sp.w });
      }
      for (var g in groups) {
        var list = groups[g], first = list[0];
        var C = (function (base) { return function (k) { return r75col_(base + k); }; })(first.c);
        var f = sp.fn(C);
        var stl = { bg: sp.st.bg === 'AUTO' ? autoOf(S8.B0) : sp.st.bg, fc: sp.st.fc === 'DIM' ? dimFc : sp.st.fc };
        var rgs = list.map(function (x) { return sh.getRange(S8.FIRST, x.c, S8.DAYS, x.w); });
        built.push({ rule: s8cfmk_(sh, rgs, f, stl), id: sp.id, f: f, n: rgs.length });
        mineF[f] = 1;
      }
    }

    // Снимаем прошлые правила R7.5 (18 шт. на крем и сводку) и прошлые свои.
    var old75 = {};
    var sp75 = r75spec_('', '', '');
    for (var q = 0; q < sp75.length; q++) old75[sp75[q][0] + '|' + sp75[q][1]] = 1;
    var keep = [], drop75 = 0, dropMine = 0;
    for (var r2 = 0; r2 < rules.length; r2++) {
      var bc = rules[r2].getBooleanCondition();
      var fx = bc ? String((bc.getCriteriaValues() || [])[0] || '') : '';
      if (fx && mineF[fx]) { dropMine++; continue; }
      var key2 = rules[r2].getRanges().map(function (x) { return x.getA1Notation(); }).join(',') + '|' + fx;
      if (fx && old75[key2]) { drop75++; continue; }
      keep.push(rules[r2]);
    }
    var added = built.map(function (x) { return x.rule; });
    sh.setConditionalFormatRules(added.concat(keep));
    SpreadsheetApp.flush();

    L.push('');
    for (var z = 0; z < built.length; z++) L.push('  ' + built[z].id + ' — диапазонов ' + built[z].n + ' :: ' + s8sp_(built[z].f));
    L.push('');
    L.push('снято: правил R7.5 ' + drop75 + ', прошлых s8cf ' + dropMine + '; поставлено ' + added.length + ' в начало списка');
    L.push('правил на листе после: ' + (added.length + keep.length) + ' (было ' + rules.length + ')');
    L.push('без консолидации потребовалось бы ' + (S8_SPEC.length * S8.NB) + ' правил');

    // Приёмка: оборачиваемость — цвет зоны должен совпасть со значением в КАЖДОМ блоке.
    var v2 = wide.getValues(), bg2 = wide.getBackgrounds(), bad = [], cells = 0;
    var zone = function (u) { return u <= 15 ? S8C.uCritBg : u <= 30 ? S8C.uWarnBg : u <= 60 ? S8C.uOkBg : S8C.uOverBg; };
    for (var b3 = 0; b3 < bl.length; b3++) {
      var cu = bl[b3].st + S8_M.turnover;
      for (var r3 = 0; r3 < S8.DAYS; r3++) {
        var u = v2[r3][cu - 1];
        if (typeof u !== 'number') continue;
        cells++;
        if (bg2[r3][cu - 1] !== zone(u)) bad.push('#' + bl[b3].i + ' ' + r75col_(cu) + (S8.FIRST + r3) + '=' + u + ' фон ' + bg2[r3][cu - 1]);
      }
    }
    L.push('');
    L.push('ПРИЁМКА ОТНОСИТЕЛЬНЫХ ССЫЛОК: оборачиваемость проверена в ' + cells + ' ячейках всех ' + bl.length + ' блоков');
    L.push('  расхождений зоны и фона: ' + bad.length + (bad.length ? ' <-- ' + bad.slice(0, 6).join(' | ') : ''));
    L.push('CF CONSOLIDATED = ' + (bad.length === 0 ? 'PASS' : 'FAIL'));
  } catch (e) { L.push('ОШИБКА: ' + e + (e && e.stack ? '\n' + e.stack : '')); }
  s8out_(L);
}

// ==================== ЧТЕНИЕ: ЧТО СЕЙЧАС В СВОДКЕ И ГДЕ ФИКТИВНЫЕ ЗНАЧЕНИЯ ===

function s8look() {
  var L = ['=== STAGE 8 · s8look (только чтение) · ' + S8B.VER + ' ==='];
  try {
    var ss = SpreadsheetApp.openById(S8.SSID), sh = ss.getSheetByName(S8.SH), tz = ss.getSpreadsheetTimeZone();
    var w = s8win_(sh, tz);
    L.push('LAST_CLOSED_DATE (зеркало WB736): ' + (w ? w.d2 : 'НЕ ДАТА') + ' | окно ставок ' + (w ? w.d1 + '..' + w.d2 : '?'));

    var rgS = sh.getRange(S8.TOP, 1, S8.PLAN - S8.TOP + 1, 11);
    var fS = rgS.getFormulas(), dS = rgS.getDisplayValues();
    var rows = { 735: 'ЗАГОЛОВОК', 736: 'ШАПКА', 737: 'ДЕНЬ 1', 745: 'ДЕНЬ 9', 746: 'ДЕНЬ 10', 767: 'MTD', 768: 'ПЛАН' };
    for (var rr in rows) {
      var idx = Number(rr) - S8.TOP;
      L.push('');
      L.push('--- СВОДКА A:K, строка ' + rr + ' (' + rows[rr] + ') ---');
      for (var c = 0; c < 11; c++) {
        var f = fS[idx][c], d = dS[idx][c];
        if (!f && !d) continue;
        L.push(r75col_(c + 1) + rr + ' [' + d + ']');
        if (f) L.push('      ' + s8chunk_(s8sp_(f)));
      }
    }

    var head = sh.getRange(S8.TOP, 1, 1, S8.NC).getDisplayValues()[0];
    var bl = s8blocks_(head);
    var mr = sh.getRange(S8.MTD, 1, 2, S8.NC);
    var mf = mr.getFormulas(), md = mr.getDisplayValues();
    L.push('');
    L.push('--- MTD 767 эталонного блока (крем, M..AJ) ---');
    for (var c2 = 0; c2 < S8.BW; c2++) {
      var col = S8.B0 + c2;
      L.push(r75col_(col) + '767 [' + md[0][col - 1] + ']' + (mf[0][col - 1] ? '' : ' · значение'));
      if (mf[0][col - 1]) L.push('      ' + s8chunk_(s8sp_(mf[0][col - 1])));
    }

    // §2: фиктивные значения в будущих днях и реальные GAP.
    var wide = sh.getRange(S8.FIRST, 1, S8.DAYS, S8.NC);
    var vv = wide.getValues(), dd = wide.getDisplayValues();
    var dates = [];
    for (var r = 0; r < S8.DAYS; r++) dates.push(vv[r][S8.B0 - 1]);
    var fut = [], err = [], zero = [];
    for (var b = 0; b < bl.length; b++) {
      var st = bl[b].st, nf = 0, ne = 0;
      for (var r2 = 0; r2 < S8.DAYS; r2++) {
        var isFut = dates[r2] instanceof Date && w && dates[r2].getTime() > w.date.getTime();
        for (var o = 0; o < S8.BW; o++) {
          var t = dd[r2][st + o - 1];
          if (/^#(REF!|VALUE!|NAME\?|DIV\/0!|N\/A|ERROR!|NUM!|NULL!)/.test(String(t))) ne++;
          if (isFut && o !== 0 && o !== 23 && o !== S8_M.commission && o !== S8_M.logistics && o !== S8_M.spp && String(t) !== '') nf++;
        }
      }
      if (nf) fut.push('#' + bl[b].i + ' ' + bl[b].nm + ':' + nf);
      if (ne) err.push('#' + bl[b].i + ' ' + bl[b].nm + ':' + ne);
    }
    L.push('');
    L.push('--- §2 БУДУЩИЕ ДНИ И ОШИБКИ ---');
    L.push('блоков с непустой экономикой в будущих днях: ' + fut.length + (fut.length ? ' <-- ' + fut.join(' ') : ''));
    L.push('блоков с ошибками формул: ' + err.length + (err.length ? ' <-- ' + err.join(' ') : ''));
    L.push('');
    L.push('--- БЛОКИ ---');
    for (var b2 = 0; b2 < bl.length; b2++)
      L.push('#' + bl[b2].i + ' ' + r75col_(bl[b2].st) + '..' + r75col_(bl[b2].st + S8.BW - 1) + ' ' + bl[b2].nm + ' ' + bl[b2].t.substr(0, 40));
  } catch (e) { L.push('ОШИБКА: ' + e + (e && e.stack ? '\n' + e.stack : '')); }
  s8out_(L);
}

// ==================== ОТКАТЫ ================================================

function s8stylerollback() {
  var L = ['=== ОТКАТ статики s8style ==='];
  var rb = r75get_(S8B.RB_STYLE), rb2 = r75get_('S8_STYLE_RB2');
  if (!rb) { L.push('снимка нет'); return s8out_(L); }
  var sh = SpreadsheetApp.openById(S8.SSID).getSheetByName(S8.SH), n = 0;
  for (var c in rb.cols) { sh.getRange(S8.FIRST, Number(c), S8.DAYS, 1).setBackgrounds(rb.cols[c].map(function (x) { return [x]; })); n++; }
  if (rb2) for (var c3 in rb2.cols) { sh.getRange(S8.FIRST, Number(c3), S8.DAYS, 1).setBackgrounds(rb2.cols[c3].map(function (x) { return [x]; })); n++; }
  for (var c2 in rb.nf) { sh.getRange(S8.FIRST, Number(c2), S8.DAYS, 1).setNumberFormats(rb.nf[c2]); n++; }
  SpreadsheetApp.flush();
  L.push('восстановлено колонок: ' + n + ' (снимок ' + rb.ts + ')');
  s8out_(L);
}

function s8raterollback() {
  var L = ['=== ОТКАТ ставок s8rates ==='];
  var rb = r75get_(S8B.RB_RATE);
  if (!rb) { L.push('снимка нет'); return s8out_(L); }
  var sh = SpreadsheetApp.openById(S8.SSID).getSheetByName(S8.SH);
  for (var i = 0; i < rb.cells.length; i++) {
    var c = rb.cells[i], g = sh.getRange(c[0], c[1]);
    if (String(c[2]).charAt(0) === '=') g.setFormula(c[2]); else g.setValue(c[2]);
  }
  SpreadsheetApp.flush();
  L.push('восстановлено ячеек: ' + rb.cells.length + ' (снимок ' + rb.ts + ')');
  s8out_(L);
}


// ==================== §1 MTD: ДОБИВКА ПЕРЕХОДОВ И КОРЗИН =====================
// В эталоне P767 и R767 были пустыми значениями — MTD по переходам и корзинам
// отсутствовал во ВСЕХ блоках (s8formulas переносит только то, что есть у эталона).
// Это дефект общего шаблона, а не правка принятого пилота: владелец требует все
// 24 метрики, включая MTD ACTUAL. Сумма считается по закрытым дням; дни вне окна
// «Воронки» остаются GAP и в сумму просто не попадают — об этом подсказка в ячейке.

var S8_NOTE_MTD = 'MTD ACTUAL за закрытые дни (дата <= LAST_CLOSED_DATE).\n' +
  'Источник дневных ячеек — wb_raw.V_WB_FUNNEL_DAILY. Дни вне доступного окна WB\n' +
  '(без подписки «Джем») остаются GAP и в сумму не входят: итог занижен на эти дни.';

function s8mtd() {
  var L = ['=== STAGE 8 §1 · s8mtd · ' + S8B.VER + ' ==='];
  try {
    var ss = SpreadsheetApp.openById(S8.SSID), sh = ss.getSheetByName(S8.SH);
    var head = sh.getRange(S8.TOP, 1, 1, S8.NC).getDisplayValues()[0];
    var bl = s8blocks_(head);
    var mf = sh.getRange(S8.MTD, 1, 1, S8.NC).getFormulas()[0];
    var last = S8.FIRST + S8.DAYS - 1, offs = [S8_M.opens, S8_M.carts];
    var plan = [], have = 0;
    for (var b = 0; b < bl.length; b++) {
      var st = bl[b].st, dc = '$' + r75col_(st) + '$';
      for (var k = 0; k < offs.length; k++) {
        var col = st + offs[k], c = r75col_(col);
        var fx = '=SUMIF(' + dc + S8.FIRST + ':' + dc + last + ';"<="&LAST_CLOSED_DATE;' + c + S8.FIRST + ':' + c + last + ')';
        if (mf[col - 1] === fx) { have++; continue; }
        plan.push({ col: col, f: fx });
      }
    }
    L.push('колонок MTD переходов и корзин: всего ' + (bl.length * 2) + ', уже верных ' + have + ', к записи ' + plan.length);
    for (var p = 0; p < plan.length; p++) sh.getRange(S8.MTD, plan[p].col).setFormula(plan[p].f).setNote(S8_NOTE_MTD);
    SpreadsheetApp.flush();
    var after = sh.getRange(S8.MTD, 1, 1, S8.NC).getFormulas()[0], ok = 0;
    for (var b2 = 0; b2 < bl.length; b2++)
      for (var k2 = 0; k2 < offs.length; k2++) if (after[bl[b2].st + offs[k2] - 1]) ok++;
    L.push('MTD ПЕРЕХОДЫ И КОРЗИНЫ = ' + ok + ' / ' + (bl.length * 2) + (ok === bl.length * 2 ? ' PASS' : ' FAIL'));
  } catch (e) { L.push('ОШИБКА: ' + e + (e && e.stack ? '\n' + e.stack : '')); }
  s8out_(L);
}

// ==================== §4 СВОДКА A:K =========================================
// Агрегация по всем 24 блокам берётся не перечислением имён, а шагом MOD(COLUMN;24):
// новый блок подхватится сам. ДРР — отношение сумм, а не среднее дневных процентов.
// Будущие дни пустые: ни 0, ни -413 руб, ни 0 %.

function s8agg_(o, row) {
  var a = r75col_(S8.B0 + o) + row, z = r75col_(S8.B0 + (S8.NB - 1) * S8.BW + o) + row;
  return 'SUM(FILTER(' + a + ':' + z + ';MOD(COLUMN(' + a + ':' + z + ')-COLUMN(' + a + ');' + S8.BW + ')=0))';
}

function s8arr_(o, r1, r2) {
  var a = '$' + r75col_(S8.B0 + o) + '$' + r1, z = '$' + r75col_(S8.B0 + (S8.NB - 1) * S8.BW + o) + '$' + r2;
  return 'FILTER(' + a + ':' + z + ';MOD(COLUMN(' + a + ':' + z + ')-COLUMN(' + a + ');' + S8.BW + ')=0)';
}

/** Знаменатель ДРР: сумма по всем блокам и дням (заказы - блогеры) * цена с СПП.
 *  IFERROR поэлементно гасит пустые строки будущих дней, не пряча ошибок источника. */
function s8den_(r1, r2) {
  return 'SUMPRODUCT(IFERROR((' + s8arr_(S8_M.orders, r1, r2) + '-' + s8arr_(S8_M.bloggers, r1, r2) + ')*' +
    s8arr_(S8_M.priceSpp, r1, r2) + ';0))';
}

var S8_SUMCOLS = [[3, 1], [4, 2], [5, 3], [6, 4], [7, 5], [8, 6], [9, 10], [10, 11]];

function s8summary() {
  var L = ['=== STAGE 8 §4 · s8summary · ' + S8B.VER + ' ==='];
  try {
    var ss = SpreadsheetApp.openById(S8.SSID), sh = ss.getSheetByName(S8.SH);
    var rg = sh.getRange(S8.HDR, 1, S8.MTD - S8.HDR + 1, 11);
    var curF = rg.getFormulas(), curV = rg.getValues();
    var row0 = S8.HDR, last = S8.FIRST + S8.DAYS - 1;
    var plan = [], rb = [], hdr = [];

    var want = { 6: 'Заказы ' + S8.NB + ' SKU', 7: 'Положили в корзину ' + S8.NB + ' SKU' };
    for (var hc in want) {
      var oldH = String(curV[0][Number(hc) - 1]);
      if (oldH !== want[hc]) { hdr.push({ col: Number(hc), v: want[hc], old: oldH }); rb.push([S8.HDR, Number(hc), oldH]); }
    }

    for (var r = S8.FIRST; r <= last; r++) {
      for (var i = 0; i < S8_SUMCOLS.length; i++) {
        var col = S8_SUMCOLS[i][0], o = S8_SUMCOLS[i][1];
        var fx = '=IF($B' + r + '>LAST_CLOSED_DATE;"";' + s8agg_(o, r) + ')';
        if (curF[r - row0][col - 1] !== fx) { plan.push({ r: r, c: col, f: fx }); rb.push([r, col, curF[r - row0][col - 1]]); }
      }
      var fk = '=IF($B' + r + '>LAST_CLOSED_DATE;"";IFERROR(J' + r + '/' + s8den_(r, r) + ';""))';
      if (curF[r - row0][10] !== fk) { plan.push({ r: r, c: 11, f: fk }); rb.push([r, 11, curF[r - row0][10]]); }
    }
    var fm = '=IFERROR(J' + S8.MTD + '/' + s8den_(S8.FIRST, last) + ';"")';
    if (curF[S8.MTD - row0][10] !== fm) { plan.push({ r: S8.MTD, c: 11, f: fm }); rb.push([S8.MTD, 11, curF[S8.MTD - row0][10]]); }

    L.push('шапок к правке: ' + hdr.length + (hdr.length ? ' — ' + hdr.map(function (x) { return r75col_(x.col) + '736 «' + x.old + '» -> «' + x.v + '»'; }).join('; ') : ''));
    L.push('формул к записи: ' + plan.length + ' (дни 737-766 колонки C..K и MTD K767)');
    if (rb.length && !r75get_(S8B.RB_SUM)) { r75put_(S8B.RB_SUM, { ts: new Date().toISOString(), ver: S8B.VER, cells: rb }); L.push('откат сохранён: ' + S8B.RB_SUM); }

    for (var h = 0; h < hdr.length; h++) sh.getRange(S8.HDR, hdr[h].col).setValue(hdr[h].v);
    for (var p = 0; p < plan.length; p++) sh.getRange(plan[p].r, plan[p].c).setFormula(plan[p].f);
    SpreadsheetApp.flush();

    var aF = rg.getFormulas(), aD = rg.getDisplayValues(), bad = 0, fut = 0, err = 0;
    var dates = sh.getRange(S8.FIRST, 2, S8.DAYS, 1).getValues();
    var mirror = sh.getRange(S8.HDR, S8.MIR).getValue();
    for (var r2 = S8.FIRST; r2 <= last; r2++) {
      var isFut = dates[r2 - S8.FIRST][0] instanceof Date && mirror instanceof Date && dates[r2 - S8.FIRST][0].getTime() > mirror.getTime();
      for (var c2 = 3; c2 <= 11; c2++) {
        var t = String(aD[r2 - row0][c2 - 1]);
        if (/^#(REF!|VALUE!|NAME\?|DIV\/0!|N\/A|ERROR!|NUM!|NULL!)/.test(t)) err++;
        if (isFut && t !== '') fut++;
      }
    }
    for (var i2 = 0; i2 < S8_SUMCOLS.length; i2++) if (aF[S8.FIRST - row0][S8_SUMCOLS[i2][0] - 1].indexOf('LAST_CLOSED_DATE') < 0) bad++;
    L.push('');
    L.push('СВОДКА: непустых ячеек в будущих днях C..K = ' + fut + ' | ошибок формул = ' + err);
    L.push('ДРР 767: было AVERAGEIF по дневным процентам — стало отношение сумм по всем ' + S8.NB + ' блокам');
    L.push('ДРР по дням: было перечисление 23 блоков (блок 2 пропущен) — стало MOD(COLUMN;24) по всем ' + S8.NB);
    L.push('SUMMARY = ' + (fut === 0 && err === 0 && bad === 0 ? 'PASS' : 'FAIL'));
  } catch (e) { L.push('ОШИБКА: ' + e + (e && e.stack ? '\n' + e.stack : '')); }
  s8out_(L);
}

function s8sumrollback() {
  var L = ['=== ОТКАТ сводки s8summary ==='];
  var rb = r75get_(S8B.RB_SUM);
  if (!rb) { L.push('снимка нет'); return s8out_(L); }
  var sh = SpreadsheetApp.openById(S8.SSID).getSheetByName(S8.SH);
  for (var i = 0; i < rb.cells.length; i++) {
    var c = rb.cells[i], rgc = sh.getRange(c[0], c[1]);
    if (String(c[2]).charAt(0) === '=') rgc.setFormula(c[2]); else rgc.setValue(c[2]);
  }
  SpreadsheetApp.flush();
  L.push('восстановлено ячеек: ' + rb.cells.length + ' (снимок ' + rb.ts + ')');
  s8out_(L);
}


// ==================== §5/§6 ПОЛНАЯ ПРОВЕРКА =================================
// Вывод намеренно компактный: Apps Script обрезает длинные журналы.

var S8_ERR = /^#(REF!|VALUE!|NAME\?|DIV\/0!|N\/A|ERROR!|NUM!|NULL!)/;
// Законно живёт в будущих днях: дата, день недели, СПП (ручной), ставки комиссии и
// логистики (константы периода) и ОСТАТОК — у остатка в строках 746+ стоит
// планировочная прокрутка (остаток вчера - заказы сегодня + возвраты вчера).
// Это модель владельца, а не факт и не фиктивный ноль: её не трогаем.
var S8_FUT_OK = [0, 7, 15, 17, 19, 23];
var S8_AUTOOFF = [2, 3, 4, 5, 6, 7, 11, 14, 20];
var S8_MTDOFF = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 18, 19, 20, 21, 22];

function s8qa() {
  var L = ['=== STAGE 8 §5/§6 · s8qa · ' + S8B.VER + ' ==='];
  try {
    var t0 = new Date().getTime();
    var ss = SpreadsheetApp.openById(S8.SSID), sh = ss.getSheetByName(S8.SH), tz = ss.getSpreadsheetTimeZone();
    var w = s8win_(sh, tz);
    var head = sh.getRange(S8.TOP, 1, 1, S8.NC).getDisplayValues()[0];
    var bl = s8blocks_(head);
    var body = sh.getRange(S8.FIRST, 1, S8.PLAN - S8.FIRST + 1, S8.NC);
    var vv = body.getValues(), dd = body.getDisplayValues(), ff = body.getFormulas();
    var M = S8.MTD - S8.FIRST;

    var dates = [];
    for (var r = 0; r < S8.DAYS; r++) dates.push(vv[r][S8.B0 - 1]);
    var closed = [];
    for (var r0 = 0; r0 < S8.DAYS; r0++) closed.push(dates[r0] instanceof Date && w && dates[r0].getTime() <= w.date.getTime());

    // 1 — сопоставление блоков
    var mapped = 0, noNm = [];
    for (var b = 0; b < bl.length; b++) { if (bl[b].nm) mapped++; else noNm.push('#' + bl[b].i); }

    // 2-5 — ошибки, будущее, GAP, MTD
    var err = 0, errWho = [], fut = 0, futWho = [], gap = {}, mtdMiss = [], wd = 0, futOff = {}, futEx = {};
    for (var b2 = 0; b2 < bl.length; b2++) {
      var st = bl[b2].st, e0 = 0, f0 = 0;
      for (var r2 = 0; r2 < S8.DAYS; r2++)
        for (var o = 0; o < S8.BW; o++) {
          var t = String(dd[r2][st + o - 1]);
          if (S8_ERR.test(t)) e0++;
          if (!closed[r2] && S8_FUT_OK.indexOf(o) < 0 && t !== '') { f0++; futOff[o] = (futOff[o] || 0) + 1; if (!futEx[o]) futEx[o] = r75col_(st + o) + (S8.FIRST + r2) + '=[' + t + ']'; }
        }
      for (var o2 = 0; o2 < S8.BW; o2++) if (S8_ERR.test(String(dd[M][st + o2 - 1]))) e0++;
      if (e0) { err += e0; errWho.push('#' + bl[b2].i + ':' + e0); }
      if (f0) { fut += f0; futWho.push('#' + bl[b2].i + ':' + f0); }
      for (var g = 0; g < S8_AUTOOFF.length; g++) {
        var go = S8_AUTOOFF[g], n = 0;
        for (var r3 = 0; r3 < S8.DAYS; r3++) if (closed[r3] && String(dd[r3][st + go - 1]) === '') n++;
        if (n) gap[go] = (gap[go] || 0) + n;
      }
      for (var mo = 0; mo < S8_MTDOFF.length; mo++) if (!ff[M][st + S8_MTDOFF[mo] - 1]) mtdMiss.push('#' + bl[b2].i + '/' + S8_MTDOFF[mo]);
      if (ff[0][st + 23 - 1].indexOf('CHOOSE(WEEKDAY') > 0) wd++;
    }
    if (ff[0][0].indexOf('CHOOSE(WEEKDAY') > 0) wd++;
    if (ff[0][11].indexOf('CHOOSE(WEEKDAY') > 0) wd++;

    // 6 — ставки
    var own = 0, sto = 0, rates = {};
    for (var b3 = 0; b3 < bl.length; b3++) {
      var s3 = bl[b3].st, lg = vv[0][s3 + S8_M.logistics - 1], cm = vv[0][s3 + S8_M.commission - 1];
      rates[bl[b3].nm] = [lg, cm];
      var uniq = true;
      for (var r4 = 1; r4 < S8.DAYS; r4++) if (vv[r4][s3 + S8_M.logistics - 1] !== lg || vv[r4][s3 + S8_M.commission - 1] !== cm) uniq = false;
      if (!uniq) L.push('ВНИМАНИЕ: ставки не постоянны в блоке #' + bl[b3].i);
    }
    var lgSet = {}, cmSet = {};
    for (var k in rates) { lgSet[rates[k][0]] = 1; cmSet[rates[k][1]] = 1; }

    // 7 — сверка сводки с суммой 24 блоков
    var sm = sh.getRange(S8.FIRST, 1, S8.DAYS, 11).getValues();
    var recon = [], worst = 0, worstWho = '';
    for (var i = 0; i < S8_SUMCOLS.length; i++) {
      var sc = S8_SUMCOLS[i][0], so = S8_SUMCOLS[i][1], mx = 0, nd = 0;
      for (var r5 = 0; r5 < S8.DAYS; r5++) {
        if (!closed[r5]) continue;
        var acc = 0;
        for (var b4 = 0; b4 < bl.length; b4++) { var q = vv[r5][bl[b4].st + so - 1]; if (typeof q === 'number') acc += q; }
        var got = sm[r5][sc - 1], d1 = Math.abs((typeof got === 'number' ? got : 0) - acc);
        if (d1 > mx) mx = d1;
        nd++;
      }
      recon.push(r75col_(sc) + '(' + so + ') дней ' + nd + ' max|Δ| ' + mx.toFixed(4));
      if (mx > worst) { worst = mx; worstWho = r75col_(sc); }
    }
    var kmx = 0;
    for (var r6 = 0; r6 < S8.DAYS; r6++) {
      if (!closed[r6]) continue;
      var num = 0, den = 0;
      for (var b5 = 0; b5 < bl.length; b5++) {
        var s5 = bl[b5].st;
        var ad = vv[r6][s5 + S8_M.adsIn - 1], qq = vv[r6][s5 + S8_M.orders - 1];
        var bg = vv[r6][s5 + S8_M.bloggers - 1], pc = vv[r6][s5 + S8_M.priceSpp - 1];
        if (typeof ad === 'number') num += ad;
        if (typeof qq === 'number' && typeof pc === 'number') den += (qq - (typeof bg === 'number' ? bg : 0)) * pc;
      }
      var kg = sm[r6][10], kw = den ? num / den : 0;
      var dk = Math.abs((typeof kg === 'number' ? kg : 0) - kw);
      if (dk > kmx) kmx = dk;
    }

    var futS = 0, errS = 0;
    var smD = sh.getRange(S8.FIRST, 1, S8.DAYS, 11).getDisplayValues();
    for (var r7 = 0; r7 < S8.DAYS; r7++)
      for (var c7 = 3; c7 <= 11; c7++) {
        var ts = String(smD[r7][c7 - 1]);
        if (S8_ERR.test(ts)) errS++;
        if (!closed[r7] && ts !== '') futS++;
      }

    var act = r7query_('SELECT CAST(nm_id AS STRING) FROM ' + String.fromCharCode(96) + S8B.PRJ +
      '.wb_raw.REF_SKU_MASTER' + String.fromCharCode(96) + " WHERE marketplace='WB' AND active");
    var inSheet = {}; for (var b6 = 0; b6 < bl.length; b6++) inSheet[bl[b6].nm] = 1;
    var orphan = [];
    for (var a = 0; a < act.length; a++) if (!inSheet[String(act[a][0])]) orphan.push(String(act[a][0]));

    var nrules = sh.getConditionalFormatRules().length;

    L.push('окно: ' + (w ? w.d1 + '..' + w.d2 : '?') + ' | закрытых дней ' + closed.filter(function (x) { return x; }).length + ' из ' + S8.DAYS);
    L.push('');
    L.push('BLOCKS MAPPED ......... ' + mapped + ' / ' + S8.NB + (noNm.length ? ' <-- ' + noNm.join(' ') : ''));
    L.push('FORMULA ERRORS ........ ' + err + (errWho.length ? ' <-- ' + errWho.join(' ') : ''));
    L.push('FUTURE-DAY LEAKAGE .... ' + fut);
    for (var fo in futOff) L.push('      смещение ' + fo + ' (' + r75col_(S8.B0 + Number(fo)) + ' у эталона) — ячеек ' + futOff[fo] + ', напр. ' + futEx[fo]);
    L.push('MTD METRICS MISSING ... ' + mtdMiss.length + (mtdMiss.length ? ' <-- ' + mtdMiss.slice(0, 8).join(' ') : '') + ' (проверено ' + S8_MTDOFF.length + ' метрик x ' + S8.NB + ')');
    L.push('WEEKDAY FORMULAS ...... ' + wd + ' / ' + (S8.NB + 2) + ' колонок');
    L.push('RATE VARIANTS ......... логистика ' + Object.keys(lgSet).length + ' разных, комиссия ' + Object.keys(cmSet).length + ' разных');
    L.push('CF RULES .............. ' + nrules);
    L.push('SUMMARY ERRORS ........ ' + errS + ' | SUMMARY FUTURE LEAK ... ' + futS);
    L.push('ACTIVE SKU БЕЗ БЛОКА .. ' + orphan.length + (orphan.length ? ' <-- ' + orphan.join(' ') : ''));
    L.push('');
    L.push('GAP в закрытых днях (оставлены пустыми, НЕ нулями):');
    for (var gk in gap) L.push('   смещение ' + gk + ' (' + r75col_(S8.B0 + Number(gk)) + ' у эталона) — ячеек ' + gap[gk]);
    L.push('');
    L.push('СВЕРКА СВОДКИ С СУММОЙ ' + S8.NB + ' БЛОКОВ (закрытые дни):');
    for (var rr = 0; rr < recon.length; rr++) L.push('   ' + recon[rr]);
    L.push('   K ДРР как отношение сумм: max|Δ| ' + kmx.toFixed(6));
    L.push('');
    var ok = (mapped === S8.NB) && err === 0 && fut === 0 && mtdMiss.length === 0 &&
      wd === S8.NB + 2 && errS === 0 && futS === 0 && worst < 0.005 && kmx < 0.0001;
    L.push('RECONCILIATION MAX |Δ| . ' + worst.toFixed(4) + (worstWho ? ' (' + worstWho + ')' : ''));
    L.push('SEPTEMBER ALL-SKU MASTER READY = ' + (ok ? 'YES' : 'NO'));
    L.push('время ' + Math.round((new Date().getTime() - t0) / 1000) + ' с');
  } catch (e) { L.push('ОШИБКА: ' + e + (e && e.stack ? '\n' + e.stack : '')); }
  s8out_(L);
}


// ==================== §2 ЗАЩИТА БУДУЩИХ ДНЕЙ ================================
// Расчётные колонки закрыты формулой IF(дата > LAST_CLOSED_DATE;"";...). Но в
// AUTO-колонках остатков и хранения с августа лежат СТАТИЧНЫЕ нули на будущие дни —
// ровно тот «фиктивный 0», который владелец запретил. Чистим только статичные
// значения: ячейку с формулой не трогаем никогда, реальные GAP закрытых дней тоже.

// Хранение в будущих днях считалось как «остаток x 0,13» — владелец запретил
// оценочное хранение (§9: только факт из wb_raw.RAW_WB_PAID_STORAGE). Будущего
// факта не существует, поэтому ячейка должна быть пустой, а не 0 руб.
var S8_FUT_CLEAR = [20];
var S8_RB_FUT = 'S8_FUT_RB';

function s8future() {
  var L = ['=== STAGE 8 §2 · s8future · ' + S8B.VER + ' ==='];
  try {
    var ss = SpreadsheetApp.openById(S8.SSID), sh = ss.getSheetByName(S8.SH), tz = ss.getSpreadsheetTimeZone();
    var w = s8win_(sh, tz);
    if (!w) { L.push('СТОП: зеркало LAST_CLOSED_DATE не дата'); return s8out_(L); }
    var head = sh.getRange(S8.TOP, 1, 1, S8.NC).getDisplayValues()[0];
    var bl = s8blocks_(head);
    var wide = sh.getRange(S8.FIRST, 1, S8.DAYS, S8.NC);
    var vv = wide.getValues(), ff = wide.getFormulas(), dd = wide.getDisplayValues();
    var dates = [];
    for (var r = 0; r < S8.DAYS; r++) dates.push(vv[r][S8.B0 - 1]);

    var cells = [], rb = [], byOff = {};
    for (var b = 0; b < bl.length; b++) {
      var st = bl[b].st;
      for (var r2 = 0; r2 < S8.DAYS; r2++) {
        if (!(dates[r2] instanceof Date) || dates[r2].getTime() <= w.date.getTime()) continue;
        for (var o = 0; o < S8.BW; o++) {
          if (S8_FUT_OK.indexOf(o) >= 0) continue;
          var col = st + o;
          if (ff[r2][col - 1] && S8_FUT_CLEAR.indexOf(o) < 0) continue;   // чужая формула — не трогаем
          if (String(dd[r2][col - 1]) === '') continue;
          cells.push([S8.FIRST + r2, col]);
          rb.push([S8.FIRST + r2, col, ff[r2][col - 1] ? ff[r2][col - 1] : vv[r2][col - 1]]);
          byOff[o] = (byOff[o] || 0) + 1;
        }
      }
    }
    L.push('ячеек к очистке в будущих днях: ' + cells.length);
    for (var k in byOff) L.push('   смещение ' + k + ' (' + r75col_(S8.B0 + Number(k)) + ' у эталона) — ' + byOff[k] + ' ячеек');
    if (rb.length && !r75get_(S8_RB_FUT)) { r75put_(S8_RB_FUT, { ts: new Date().toISOString(), ver: S8B.VER, cells: rb }); L.push('откат сохранён: ' + S8_RB_FUT); }

    // Пишем диапазонами: подряд идущие строки одной колонки — одним вызовом.
    var byCol = {};
    for (var c = 0; c < cells.length; c++) { if (!byCol[cells[c][1]]) byCol[cells[c][1]] = []; byCol[cells[c][1]].push(cells[c][0]); }
    var calls = 0;
    for (var cc in byCol) {
      var rows = byCol[cc].sort(function (a, z) { return a - z; }), i = 0;
      while (i < rows.length) {
        var j = i; while (j + 1 < rows.length && rows[j + 1] === rows[j] + 1) j++;
        sh.getRange(rows[i], Number(cc), rows[j] - rows[i] + 1, 1).clearContent();
        calls++; i = j + 1;
      }
    }
    SpreadsheetApp.flush();
    L.push('вызовов очистки: ' + calls);

    var d2 = wide.getDisplayValues(), left = 0;
    for (var b2 = 0; b2 < bl.length; b2++)
      for (var r3 = 0; r3 < S8.DAYS; r3++) {
        if (!(dates[r3] instanceof Date) || dates[r3].getTime() <= w.date.getTime()) continue;
        for (var o2 = 0; o2 < S8.BW; o2++) {
          if (S8_FUT_OK.indexOf(o2) >= 0) continue;
          if (String(d2[r3][bl[b2].st + o2 - 1]) !== '') left++;
        }
      }
    L.push('FUTURE-DAY LEAKAGE ПОСЛЕ = ' + left + (left === 0 ? ' PASS' : ' FAIL'));
  } catch (e) { L.push('ОШИБКА: ' + e + (e && e.stack ? '\n' + e.stack : '')); }
  s8out_(L);
}

function s8futrollback() {
  var L = ['=== ОТКАТ s8future ==='];
  var rb = r75get_(S8_RB_FUT);
  if (!rb) { L.push('снимка нет'); return s8out_(L); }
  var sh = SpreadsheetApp.openById(S8.SSID).getSheetByName(S8.SH);
  for (var i = 0; i < rb.cells.length; i++) {
    var c = rb.cells[i], g = sh.getRange(c[0], c[1]);
    if (String(c[2]).charAt(0) === '=') g.setFormula(c[2]); else g.setValue(c[2]);
  }
  SpreadsheetApp.flush();
  L.push('восстановлено ячеек: ' + rb.cells.length + ' (снимок ' + rb.ts + ')');
  s8out_(L);
}
