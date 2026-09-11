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
      tbl.push([nm, f.n, f.log, f.comm, f.src, f.conf, bl[b].t.substr(0, 34)]);   // f.log — только справочно
      // ЛОГИСТИКА здесь больше НЕ пишется: с модели B её ставка считается из
      // прямых плеч и живёт в s8brates(). Оставить обе записи — значит вернуть
      // в колонку ставку «вся логистика / выкупы», внутри которой сидят обратные
      // плечи отказов, то есть вернуть двойной счёт.
      var pr = [[S8_M.commission, f.comm]];
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
        if (Math.abs(Number(after[d3][bl[b3].st + S8_M.commission - 1]) - f3.comm) > 1e-9) bad++;
      }
    }
    L.push('');
    L.push('RATES WRITTEN = ' + (plan.length === 0 ? 'уже актуальны (идемпотентно)' : plan.length + ' колонок'));
    L.push('ВНИМАНИЕ: колонка логистики пишется s8brates() по модели B, здесь только комиссия');
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


/* ============ §B СОБЫТИЙНАЯ МОДЕЛЬ ЛОГИСТИКИ (решение владельца 11.09) ========
 * LOGISTICS_DAY = GROSS_ORDERS x DIRECT_LOGISTICS_RATE + CANCELS x REVERSE_LEG_RATE
 *
 * Почему не «отмены x полная логистика отказа»: прямое плечо отказанного заказа
 * уже оплачено внутри GROSS_ORDERS (колонка «Заказы факт» — валовые заказы,
 * отменённый заказ в них входит). Списать полную логистику отказа — значит
 * посчитать прямое плечо дважды.
 *
 * Почему старая ставка не годится для DIRECT: она считалась как ВСЯ логистика
 * окна / выкупы и потому содержала обратные плечи отказов. Теперь числитель —
 * только прямые плечи (+ «Доставка»), знаменатель — все отправления окна.
 *
 * Разбиение плеч: у отказа в финотчёте ДВЕ строки «Логистика» с одним srid.
 * Большая — прямое плечо, меньшая — обратное (подтверждено аудитом:
 * 63,48 против 33,48 руб на окне 01.06-09.09).
 *
 * Ставки не хардкодятся: окно D-30..D-1 от зеркала LAST_CLOSED_DATE,
 * пересчёт каждым прогоном. Своя ставка SKU — только при n >= 10 наблюдений,
 * иначе магазинная (для обратного плеча сегодня это все 24 SKU).
 * ========================================================================== */

var S8BL = {
  VER: 'unitka2.0/v8.5.0',
  MIN_N: 10,
  RB_RATE: 'S8B_RATE_RB', RB_FML: 'S8B_FML_RB',
  NAME: 'REVERSE_LEG_RATE',
  RROW: 737            // зеркало ставки обратного плеча: строка 737 колонки S8.MIR
};

// ОДОБРЕНО ВЛАДЕЛЬЦЕМ 11.09: популяция прямых отправлений — УНИКАЛЬНЫЕ srid
// с операцией IN ('Логистика','Доставка'), а не только 'Логистика'.
// Это два взаимоисключающих имени ОДНОГО события — прямой перевозки отправления:
// пересечение srid между ними 0, продаж без транспортной строки 0, смешанных схем 0.
// Прежде «Доставка» была в числителе, но её отправления не попадали в знаменатель,
// и оба плеча её отказов считались прямыми. Оба дефекта здесь устранены.
function s8bsql_(d1, d2) {
  var Q = String.fromCharCode(96), T = Q + S8B.PRJ + '.' + S8B.FIN + Q;
  var LG = "'\u041b\u043e\u0433\u0438\u0441\u0442\u0438\u043a\u0430'", DL = "'\u0414\u043e\u0441\u0442\u0430\u0432\u043a\u0430'", SL = "'\u041f\u0440\u043e\u0434\u0430\u0436\u0430'";
  var TR = '(' + LG + ', ' + DL + ')';
  return 'WITH b AS (SELECT srid, SAFE_CAST(wb_nm_id AS INT64) nm, supplier_oper_name son,' +
    ' SAFE_CAST(logistics_amount AS NUMERIC) amt FROM ' + T +
    " WHERE _rr_date BETWEEN DATE '" + d1 + "' AND DATE '" + d2 + "'" +
    ' AND wb_nm_id IS NOT NULL AND srid IS NOT NULL),' +
    ' sale AS (SELECT COUNT(DISTINCT srid) n FROM b WHERE son = ' + SL + '),' +
    ' l AS (SELECT srid, nm, son, amt, ROW_NUMBER() OVER (PARTITION BY srid ORDER BY amt DESC) rn,' +
    ' COUNT(*) OVER (PARTITION BY srid) legs FROM b WHERE son IN ' + TR + '),' +
    ' p AS (SELECT srid, ANY_VALUE(nm) nm, ANY_VALUE(son) son, SUM(IF(rn=1,amt,0)) fwd,' +
    ' SUM(IF(rn>1,amt,0)) rev, MAX(legs) legs FROM l GROUP BY srid),' +
    ' a AS (SELECT nm, COUNT(*) srids, SUM(fwd) fwd, COUNTIF(legs>=2) ref, SUM(rev) rev,' +
    ' COUNTIF(son = ' + LG + ') nlog, COUNTIF(son = ' + DL + ') ndlv,' +
    ' SUM(IF(son = ' + DL + ', fwd, 0)) dlvfwd FROM p GROUP BY nm)' +
    ' SELECT CAST(nm AS STRING), CAST(srids AS STRING),' +
    ' CAST(ROUND(SAFE_DIVIDE(fwd, NULLIF(srids,0)),4) AS STRING),' +
    ' CAST(ref AS STRING), CAST(ROUND(SAFE_DIVIDE(rev, NULLIF(ref,0)),4) AS STRING),' +
    ' CAST(ROUND(fwd,2) AS STRING), CAST(ROUND(rev,2) AS STRING),' +
    ' CAST(nlog AS STRING), CAST(ndlv AS STRING), CAST(ROUND(dlvfwd,2) AS STRING),' +
    ' CAST((SELECT n FROM sale) AS STRING) FROM a' +
    " UNION ALL SELECT '0', CAST(SUM(srids) AS STRING)," +
    ' CAST(ROUND(SAFE_DIVIDE(SUM(fwd), NULLIF(SUM(srids),0)),4) AS STRING),' +
    ' CAST(SUM(ref) AS STRING), CAST(ROUND(SAFE_DIVIDE(SUM(rev), NULLIF(SUM(ref),0)),4) AS STRING),' +
    ' CAST(ROUND(SUM(fwd),2) AS STRING), CAST(ROUND(SUM(rev),2) AS STRING),' +
    ' CAST(SUM(nlog) AS STRING), CAST(SUM(ndlv) AS STRING), CAST(ROUND(SUM(dlvfwd),2) AS STRING),' +
    ' CAST((SELECT n FROM sale) AS STRING) FROM a';
}
function s8brates_(d1, d2, L) {
  var rows = r7query_(s8bsql_(d1, d2)), per = {}, store = null;
  for (var i = 0; i < rows.length; i++) {
    var r = rows[i];
    var o = { n: Number(r[1]) || 0, direct: Number(r[2]), ref: Number(r[3]) || 0, rev: Number(r[4]),
              fwdsum: Number(r[5]) || 0, revsum: Number(r[6]) || 0,
              nlog: Number(r[7]) || 0, ndlv: Number(r[8]) || 0, dlvfwd: Number(r[9]) || 0,
              sales: Number(r[10]) || 0 };
    if (String(r[0]) === '0') store = o; else per[String(r[0])] = o;
  }
  if (!store || !store.n) throw new Error('финотчёт за окно ' + d1 + '..' + d2 + ' пуст');
  if (L) {
    L.push('окно ставок ' + d1 + '..' + d2 + ', источник ' + S8B.FIN);
    L.push('МАГАЗИН: отправлений ' + store.n + ' (Логистика ' + store.nlog +
      ' + Доставка ' + store.ndlv + '), отказов ' + store.ref + ', продаж ' + store.sales);
    var inv = (store.n === store.sales + store.ref);
    L.push('  ИНВАРИАНТ DIRECT SHIPMENTS = SALES + CANCELLED: ' + store.n + ' = ' +
      store.sales + ' + ' + store.ref + ' -> ' + (inv ? 'PASS' : 'FAIL'));
    if (!inv) throw new Error('инвариант популяции не сошёлся: ' + store.n + ' вместо ' + (store.sales + store.ref));
    L.push('  прямые плечи ' + store.fwdsum.toFixed(2) + ' руб (из них «Доставка» ' +
      store.dlvfwd.toFixed(2) + ' руб) / ' + store.n + ' отправлений -> DIRECT = ' + store.direct.toFixed(4) + ' руб/заказ');
    L.push('  обратные плечи ' + store.revsum.toFixed(2) + ' руб / ' + store.ref + ' отказов -> REVERSE = ' + store.rev.toFixed(4) + ' руб/отказ');
    var all = store.fwdsum + store.revsum;
    L.push('  вся логистика окна ' + all.toFixed(2) + ' руб = прямые + обратные, пересечение 0');
    L.push('  знаменатель исправлен 11.09: раньше считались только srid «Логистики» (' + store.nlog +
      '), а деньги «Доставки» входили в числитель целиком, вместе с обратными плечами её отказов');
  }
  return {
    store: store,
    get: function (nm) {
      var p = per[nm] || null;
      var ownD = !!(p && p.n >= S8BL.MIN_N && !isNaN(p.direct) && p.direct > 0);
      var ownR = !!(p && p.ref >= S8BL.MIN_N && !isNaN(p.rev) && p.rev > 0);
      return {
        n: p ? p.n : 0, ref: p ? p.ref : 0,
        direct: Math.round((ownD ? p.direct : store.direct) * 100) / 100,
        rev: Math.round((ownR ? p.rev : store.rev) * 100) / 100,
        srcD: ownD ? 'своя' : 'магазин', srcR: ownR ? 'своя' : 'магазин'
      };
    }
  };
}

/** ШАГ 1: ставки. DIRECT -> колонка логистики каждого блока, REVERSE -> зеркало + имя. */
function s8brates() {
  var L = ['=== STAGE 8 §B.1 · s8brates · ' + S8BL.VER + ' ==='];
  try {
    var t0 = new Date().getTime();
    var ss = SpreadsheetApp.openById(S8.SSID), sh = ss.getSheetByName(S8.SH), tz = ss.getSpreadsheetTimeZone();
    var w = s8win_(sh, tz);
    if (!w) { L.push('СТОП: зеркало LAST_CLOSED_DATE не дата'); return s8out_(L); }
    var fin = s8brates_(w.d1, w.d2, L);

    var head = sh.getRange(S8.TOP, 1, 1, S8.NC).getDisplayValues()[0];
    var bl = s8blocks_(head);
    var wide = sh.getRange(S8.FIRST, 1, S8.DAYS, S8.NC), cur = wide.getValues();
    var plan = [], rb = [], tbl = [];

    for (var b = 0; b < bl.length; b++) {
      var st = bl[b].st, nm = bl[b].nm;
      if (!nm) continue;
      var f = fin.get(nm);
      tbl.push([nm, f.n, f.direct, f.srcD, f.ref, f.rev, f.srcR, bl[b].t.substr(0, 30)]);
      var col = st + S8_M.logistics, out = [], diff = false;
      for (var d = 0; d < S8.DAYS; d++) {
        out.push([f.direct]);
        if (Math.abs(Number(cur[d][col - 1]) - f.direct) > 1e-9) diff = true;
      }
      if (diff) {
        plan.push({ col: col, vals: out });
        for (var d2 = 0; d2 < S8.DAYS; d2++) rb.push([S8.FIRST + d2, col, cur[d2][col - 1]]);
      }
    }

    var revCell = sh.getRange(S8BL.RROW, S8.MIR);
    var oldRev = revCell.getValue();
    rb.push([S8BL.RROW, S8.MIR, oldRev]);
    if (rb.length && !r75get_(S8BL.RB_RATE)) { r75put_(S8BL.RB_RATE, { ts: new Date().toISOString(), ver: S8BL.VER, cells: rb }); L.push('откат сохранён: ' + S8BL.RB_RATE); }

    for (var p = 0; p < plan.length; p++) sh.getRange(S8.FIRST, plan[p].col, S8.DAYS, 1).setValues(plan[p].vals);
    revCell.setValue(fin.store.rev);
    sh.getRange(S8BL.RROW, S8.MIR - 1).setValue('обратная логистика, руб/отказ (rolling D-30..D-1)');
    var named = ss.getRangeByName(S8BL.NAME);
    if (!named) { ss.setNamedRange(S8BL.NAME, revCell); L.push('создано имя ' + S8BL.NAME + ' -> ' + r75col_(S8.MIR) + S8BL.RROW); }
    else L.push('имя ' + S8BL.NAME + ' уже есть');
    SpreadsheetApp.flush();

    L.push('');
    L.push('nmID | отправлений | DIRECT | ист | отказов | REVERSE | ист | товар');
    tbl.sort(function (a, c) { return c[1] - a[1]; });
    for (var i = 0; i < tbl.length; i++)
      L.push(tbl[i][0] + ' | ' + tbl[i][1] + ' | ' + tbl[i][2].toFixed(2) + ' | ' + tbl[i][3] + ' | ' +
        tbl[i][4] + ' | ' + tbl[i][5].toFixed(2) + ' | ' + tbl[i][6] + ' | ' + tbl[i][7]);

    var after = wide.getValues(), bad = 0;
    for (var b3 = 0; b3 < bl.length; b3++) {
      if (!bl[b3].nm) continue;
      var f3 = fin.get(bl[b3].nm);
      for (var d3 = 0; d3 < S8.DAYS; d3++)
        if (Math.abs(Number(after[d3][bl[b3].st + S8_M.logistics - 1]) - f3.direct) > 1e-9) bad++;
    }
    L.push('');
    L.push('колонок логистики переписано: ' + plan.length);
    L.push('REVERSE_LEG_RATE = ' + fin.store.rev.toFixed(2) + ' руб (было в ячейке: ' + oldRev + ')');
    L.push('DIRECT RATES VERIFY = ' + (bad === 0 ? 'PASS' : 'FAIL (' + bad + ')') +
      ' | ' + Math.round((new Date().getTime() - t0) / 1000) + ' с');
  } catch (e) { L.push('ОШИБКА: ' + e + (e && e.stack ? '\n' + e.stack : '')); }
  s8out_(L);
}


/**
 * ШАГ 2: формулы. Дневная доходность блока получает событийную логистику.
 *
 * Было:  Q*AI - X - AG - S*AI + Y
 * Стало: Q*AI - X - AG - S*(AI + AF + REVERSE_LEG_RATE) + Y
 *
 * Разбор добавки: -S*AI снимает экономику непроданной единицы (как и раньше);
 * -S*AF возвращает ПРЯМОЕ плечо, которое WB списал по валовому заказу и которое
 * вместе с экономикой ошибочно снималось; -S*REVERSE_LEG_RATE добавляет плечо
 * обратной доставки. Итого логистика дня = Q*AF + S*REVERSE, ровно по соглашению.
 *
 * MTD-строка логистики блока перестаёт быть только прямой: к SUMPRODUCT(AF*Q)
 * добавляется SUM(отмены)*REVERSE_LEG_RATE.
 *
 * I767 сводки теряет член «- H767 * $F$45»: отмены теперь учтены внутри блоков,
 * и повторное вычитание в сводке было бы двойным счётом.
 */
function s8bformula() {
  var L = ['=== STAGE 8 §B.2 · s8bformula · ' + S8BL.VER + ' ==='];
  try {
    var t0 = new Date().getTime();
    var sh = SpreadsheetApp.openById(S8.SSID).getSheetByName(S8.SH);
    var head = sh.getRange(S8.TOP, 1, 1, S8.NC).getDisplayValues()[0];
    var bl = s8blocks_(head);
    var wide = sh.getRange(S8.FIRST, 1, S8.DAYS, S8.NC), fml = wide.getFormulas();
    var mtd = sh.getRange(S8.MTD, 1, 1, S8.NC), mf = mtd.getFormulas()[0];
    var plan = [], rb = [], miss = [], done = 0, already = 0;

    for (var b = 0; b < bl.length; b++) {
      var st = bl[b].st;
      var CD = r75col_(st), CS = r75col_(st + S8_M.cancels);
      var CAI = r75col_(st + S8_M.unitProfit), CAF = r75col_(st + S8_M.logistics);
      var CW = st + S8_M.profitAll;
      for (var d = 0; d < S8.DAYS; d++) {
        var r = S8.FIRST + d, f = fml[d][CW - 1];
        if (!f) continue;
        if (f.indexOf(S8BL.NAME) >= 0) { already++; continue; }
        var re = new RegExp('-\\s*' + CS + r + '\\s*\\*\\s*' + CAI + r + '(?![0-9])');
        if (!re.test(f)) { if (miss.length < 6) miss.push('#' + bl[b].i + ' ' + r75col_(CW) + r); continue; }
        var nf = f.replace(re, '-' + CS + r + '*(' + CAI + r + '+' + CAF + r + '+' + S8BL.NAME + ')');
        plan.push({ r: r, c: CW, f: nf });
        rb.push([r, CW, f]);
        done++;
      }
      var af = mf[st + S8_M.logistics - 1];
      if (af && af.indexOf(S8BL.NAME) < 0) {
        var naf = af + '+SUMIF($' + CD + '$' + S8.FIRST + ':$' + CD + '$' + (S8.FIRST + S8.DAYS - 1) +
          ';"<="&LAST_CLOSED_DATE;' + CS + S8.FIRST + ':' + CS + (S8.FIRST + S8.DAYS - 1) + ')*' + S8BL.NAME;
        plan.push({ r: S8.MTD, c: st + S8_M.logistics, f: naf });
        rb.push([S8.MTD, st + S8_M.logistics, af]);
      }
    }

    var i767 = sh.getRange(S8.MTD, 9).getFormula();
    var want767 = '=SUM(I' + S8.FIRST + ':I' + (S8.FIRST + S8.DAYS - 1) + ')';
    if (i767 !== want767) { plan.push({ r: S8.MTD, c: 9, f: want767 }); rb.push([S8.MTD, 9, i767]); }

    L.push('дневных формул доходности к правке: ' + done + ' (уже с ' + S8BL.NAME + ': ' + already + ')');
    L.push('не нашли шаблон «-отмены*доходность1шт»: ' + miss.length + (miss.length ? ' ' + miss.join(' ') : ''));
    L.push('I' + S8.MTD + ' было: ' + i767);
    L.push('I' + S8.MTD + ' станет: ' + want767);
    L.push('всего ячеек к записи: ' + plan.length);
    if (!plan.length) { L.push('нечего менять'); return s8out_(L); }
    if (!r75get_(S8BL.RB_FML)) { r75put_(S8BL.RB_FML, { ts: new Date().toISOString(), ver: S8BL.VER, cells: rb }); L.push('откат сохранён: ' + S8BL.RB_FML); }

    for (var p = 0; p < plan.length; p++) sh.getRange(plan[p].r, plan[p].c).setFormula(plan[p].f);
    SpreadsheetApp.flush();

    var af2 = sh.getRange(S8.FIRST, 1, S8.DAYS, S8.NC).getFormulas(), okW = 0;
    for (var b2 = 0; b2 < bl.length; b2++) {
      var good = true;
      for (var d2 = 0; d2 < S8.DAYS; d2++) {
        var ff = af2[d2][bl[b2].st + S8_M.profitAll - 1];
        if (ff && ff.indexOf(S8BL.NAME) < 0) good = false;
        if (ff && ff.split(S8BL.NAME).length - 1 > 1) good = false;
      }
      if (good) okW++;
    }
    L.push('');
    L.push('BLOCKS WITH EVENT LOGISTICS ... ' + okW + ' / ' + bl.length);
    L.push('FORMULA MODEL B = ' + (okW === bl.length && miss.length === 0 ? 'PASS' : 'FAIL') +
      ' | ' + Math.round((new Date().getTime() - t0) / 1000) + ' с');
  } catch (e) { L.push('ОШИБКА: ' + e + (e && e.stack ? '\n' + e.stack : '')); }
  s8out_(L);
}

/** ОТКАТ шагов §B. */
function s8brollback() {
  var L = ['=== STAGE 8 §B · s8brollback ==='];
  try {
    var sh = SpreadsheetApp.openById(S8.SSID).getSheetByName(S8.SH);
    [S8BL.RB_FML, S8BL.RB_RATE].forEach(function (k) {
      var o = r75get_(k);
      if (!o || !o.cells) { L.push(k + ': нет'); return; }
      for (var i = 0; i < o.cells.length; i++) {
        var c = o.cells[i];
        if (typeof c[2] === 'string' && c[2].charAt(0) === '=') sh.getRange(c[0], c[1]).setFormula(c[2]);
        else sh.getRange(c[0], c[1]).setValue(c[2]);
      }
      L.push(k + ': восстановлено ячеек ' + o.cells.length);
    });
    SpreadsheetApp.flush();
  } catch (e) { L.push('ОШИБКА: ' + e); }
  s8out_(L);
}


/**
 * ШАГ 3: приёмка модели B.
 * Двойной счёт проверяется СТРУКТУРНО, а не «на глаз»: в цепочке доходности
 * ссылка на колонку логистики должна встречаться ровно столько раз, сколько
 * плеч мы намерены списать, и ни разу больше.
 *   AI (доходность 1 шт)      — ровно 1 ссылка на AF  (прямое плечо проданной единицы)
 *   W  (доходность дня)       — ровно 1 ссылка на AF  (прямое плечо отменённой единицы)
 *                               и ровно 1 ссылка на REVERSE_LEG_RATE
 *   сводка I767               — ни одной ссылки на старую константу отмен
 * Плюс проверка на уровне ставки: числитель DIRECT не пересекается с обратными
 * плечами (прямые + обратные + доставка = вся логистика окна).
 */
function s8bqa() {
  var L = ['=== STAGE 8 §B.3 · s8bqa · ' + S8BL.VER + ' ==='];
  try {
    var t0 = new Date().getTime();
    var ss = SpreadsheetApp.openById(S8.SSID), sh = ss.getSheetByName(S8.SH), tz = ss.getSpreadsheetTimeZone();
    var w = s8win_(sh, tz);
    var fin = s8brates_(w.d1, w.d2, L);
    var st0 = fin.store;
    var all = st0.fwdsum + st0.revsum;
    var dsum = st0.fwdsum;
    L.push('');
    L.push('--- СТАВКИ: непересечение числителей ---');
    L.push('популяция  = уникальные srid с операцией Логистика или Доставка: ' + st0.n + ' = ' + st0.nlog + ' + ' + st0.ndlv);
    L.push('ИНВАРИАНТ = продаж ' + st0.sales + ' + отказов ' + st0.ref + ' = ' + (st0.sales + st0.ref) + ' -> ' + (st0.n === st0.sales + st0.ref ? 'PASS' : 'FAIL'));
    L.push('DIRECT числитель  = только первые плечи ' + st0.fwdsum.toFixed(2) + ' руб (в т.ч. Доставка ' + st0.dlvfwd.toFixed(2) + ')');
    L.push('REVERSE числитель = обратные ' + st0.revsum.toFixed(2) + ' руб');
    L.push('пересечение = ' + (dsum + st0.revsum - all).toFixed(2) + ' руб (должно быть 0,00)');
    var rateOverlap = Math.abs(dsum + st0.revsum - all) > 0.01 ? 1 : 0;

    var head = sh.getRange(S8.TOP, 1, 1, S8.NC).getDisplayValues()[0];
    var bl = s8blocks_(head);
    var rgAll = sh.getRange(S8.FIRST, 1, S8.DAYS, S8.NC);
    var fml = rgAll.getFormulas(), val = rgAll.getValues(), dsp = rgAll.getDisplayValues();
    var mtdF = sh.getRange(S8.MTD, 1, 2, S8.NC).getFormulas();
    var mtdD = sh.getRange(S8.MTD, 1, 2, S8.NC).getDisplayValues();

    var dcnt = 0, rcnt = 0, badAI = [], badW = [];
    for (var b = 0; b < bl.length; b++) {
      var st = bl[b].st, CAF = r75col_(st + S8_M.logistics);
      var wcol = st + S8_M.profitAll - 1, aicol = st + S8_M.unitProfit - 1;
      for (var d = 0; d < S8.DAYS; d++) {
        var r = S8.FIRST + d;
        var fw = fml[d][wcol], fa = fml[d][aicol];
        var reAF = new RegExp('(?<![A-Z$])' + CAF + r + '(?![0-9])', 'g');
        if (fa) {
          var na = (fa.match(reAF) || []).length;
          if (na !== 1) { dcnt++; if (badAI.length < 5) badAI.push('#' + bl[b].i + ' ' + r75col_(aicol + 1) + r + ':' + na); }
        }
        if (fw) {
          var nw = (fw.match(reAF) || []).length;
          if (nw !== 1) { dcnt++; if (badW.length < 5) badW.push('#' + bl[b].i + ' ' + r75col_(wcol + 1) + r + ' AF:' + nw); }
          var nr = fw.split(S8BL.NAME).length - 1;
          if (nr !== 1) { rcnt++; if (badW.length < 8) badW.push('#' + bl[b].i + ' ' + r75col_(wcol + 1) + r + ' REV:' + nr); }
        }
      }
    }

    // старая константа отмен не должна встречаться в сентябрьских формулах
    var legacy = 0, legWhere = [];
    for (var d2 = 0; d2 < S8.DAYS; d2++)
      for (var cc = 0; cc < S8.NC; cc++)
        if (fml[d2][cc] && fml[d2][cc].indexOf('$F$45') >= 0) { legacy++; if (legWhere.length < 5) legWhere.push(r75col_(cc + 1) + (S8.FIRST + d2)); }
    for (var rr = 0; rr < 2; rr++)
      for (var c2 = 0; c2 < S8.NC; c2++)
        if (mtdF[rr][c2] && mtdF[rr][c2].indexOf('$F$45') >= 0) { legacy++; if (legWhere.length < 5) legWhere.push(r75col_(c2 + 1) + (S8.MTD + rr)); }
    rcnt += legacy;

    L.push('');
    L.push('--- СТРУКТУРА ФОРМУЛ ---');
    L.push('ссылок на колонку логистики вне нормы (AI и W): ' + dcnt + (badAI.length ? ' AI: ' + badAI.join(' ') : '') + (badW.length ? ' W: ' + badW.join(' ') : ''));
    L.push('ссылок на ' + S8BL.NAME + ' вне нормы: ' + (rcnt - legacy));
    L.push('старая константа отмен $F$45 в сентябрьских формулах: ' + legacy + (legWhere.length ? ' ' + legWhere.join(' ') : ''));

    // сверка сводки с суммой блоков по закрытым дням
    var sumRg = sh.getRange(S8.FIRST, 1, S8.DAYS, 11).getValues();
    var mirror = sh.getRange(S8.HDR, S8.MIR).getValue();
    var dates = sh.getRange(S8.FIRST, 2, S8.DAYS, 1).getValues();
    var mx = 0, nd = 0;
    for (var d3 = 0; d3 < S8.DAYS; d3++) {
      if (!(dates[d3][0] instanceof Date) || dates[d3][0].getTime() > mirror.getTime()) continue;
      var s = 0;
      for (var b4 = 0; b4 < bl.length; b4++) {
        var x = val[d3][bl[b4].st + S8_M.profitAll - 1];
        if (typeof x === 'number') s += x;
      }
      var got = Number(sumRg[d3][8]);
      mx = Math.max(mx, Math.abs(s - got)); nd++;
    }
    var i767 = Number(sh.getRange(S8.MTD, 9).getValue()), isum = 0;
    for (var d4 = 0; d4 < S8.DAYS; d4++) { var y = Number(sumRg[d4][8]); if (!isNaN(y)) isum += y; }
    L.push('');
    L.push('--- СВЕРКА СВОДКИ ---');
    L.push('дней сверено: ' + nd + ', max |Δ| доходности (сумма 24 блоков против I) = ' + mx.toFixed(4));
    L.push('I' + S8.MTD + ' = ' + i767.toFixed(2) + ', сумма дневных = ' + isum.toFixed(2) + ', |Δ| = ' + Math.abs(i767 - isum).toFixed(4));

    // ошибки формул
    var err = 0, errW = [];
    for (var d5 = 0; d5 < S8.DAYS; d5++)
      for (var c5 = 0; c5 < S8.NC; c5++) {
        var t = String(dsp[d5][c5]);
        if (/^#(REF!|VALUE!|NAME\?|DIV\/0!|N\/A|ERROR!|NUM!|NULL!)/.test(t)) { err++; if (errW.length < 5) errW.push(r75col_(c5 + 1) + (S8.FIRST + d5)); }
      }
    for (var rr2 = 0; rr2 < 2; rr2++)
      for (var c6 = 0; c6 < S8.NC; c6++) {
        var t2 = String(mtdD[rr2][c6]);
        if (/^#(REF!|VALUE!|NAME\?|DIV\/0!|N\/A|ERROR!|NUM!|NULL!)/.test(t2)) { err++; if (errW.length < 5) errW.push(r75col_(c6 + 1) + (S8.MTD + rr2)); }
      }

    // материальность на сентябре
    var canc = 0, gross = 0, logi = 0;
    for (var b5 = 0; b5 < bl.length; b5++) {
      for (var d6 = 0; d6 < S8.DAYS; d6++) {
        if (!(dates[d6][0] instanceof Date) || dates[d6][0].getTime() > mirror.getTime()) continue;
        var q = Number(val[d6][bl[b5].st + S8_M.orders - 1]) || 0;
        var sc = Number(val[d6][bl[b5].st + S8_M.cancels - 1]) || 0;
        var af = Number(val[d6][bl[b5].st + S8_M.logistics - 1]) || 0;
        gross += q; canc += sc; logi += q * af;
      }
    }
    L.push('');
    L.push('--- ЗАКРЫТЫЕ ДНИ СЕНТЯБРЯ, ЭФФЕКТ МОДЕЛИ ---');
    L.push('валовых заказов ' + gross + ', отмен ' + canc);
    L.push('прямая логистика Q*DIRECT = ' + logi.toFixed(2) + ' руб');
    L.push('обратная логистика отмен = ' + (canc * st0.rev).toFixed(2) + ' руб (' + canc + ' x ' + st0.rev.toFixed(2) + ')');
    L.push('было по старой модели: отмены x 50 руб = ' + (canc * 50).toFixed(2) + ' руб в сводке, прямое плечо отмен не списывалось');

    var okD = (dcnt === 0 && rateOverlap === 0), okR = (rcnt === 0);
    var okS = (mx < 0.01 && Math.abs(i767 - isum) < 0.01);
    L.push('');
    L.push('DIRECT LOGISTICS DOUBLE COUNT = ' + (okD ? 0 : dcnt + rateOverlap));
    L.push('REVERSE LOGISTICS DOUBLE COUNT = ' + (okR ? 0 : rcnt));
    L.push('SUMMARY RECONCILIATION = ' + (okS ? 'PASS' : 'FAIL'));
    L.push('FORMULA ERRORS = ' + err + (errW.length ? ' ' + errW.join(' ') : ''));
    L.push('SEPTEMBER FINANCIAL MASTER READY = ' + (okD && okR && okS && err === 0 ? 'YES' : 'NO') +
      ' | ' + Math.round((new Date().getTime() - t0) / 1000) + ' с');
  } catch (e) { L.push('ОШИБКА: ' + e + (e && e.stack ? '\n' + e.stack : '')); }
  s8out_(L);
}


/* ============ /* ============ STAGE 8.2 §1-§7 ВОРОНКА КАК ИСТОЧНИК ПРАВДЫ ====================
 * Сверка 11.09 по официальному XLSX ЛК WB (01-09.09, 22 артикула):
 * эндпоинт POST /api/analytics/v3/sales-funnel/products/history воспроизводит
 * XLSX ТОЧНО: 0 расхождений на всех ячейках nmID × день × 3 метрики (04-09.09).
 * Прежние «+2..+5 переходов в день» оказались артефактом выгрузки: в XLSX был
 * выбран список из 22 артикулов, а 252441968, 252442341 и 909951444 в него не вошли.
 *
 * ПОЭТОМУ authoritative для «Переходы», «Положили в корзину», «Заказы факт» — воронка.
 * Приоритет источников на каждый день, печатается в лог:
 *   1. FUNNEL_API    — V_WB_FUNNEL_DAILY (04.09 и позже)
 *   2. XLSX_BACKFILL — RAW_WB_FUNNEL_XLSX_BACKFILL (ТОЛЬКО 01-03.09, разовая историческая
 *      загрузка из официального XLSX: глубина эндпоинта без «Джема» — 7 дней,
 *      и эти дни из API уже не достать. Для Engine этот путь ЗАКРЫТ.)
 *   3. ORDERS_API    — FACT_ORDERS, только если нет ни того ни другого.
 *
 * ОТМЕНЫ. В ответе history полей отмен НЕТ вообще (openCount, cartCount,
 * orderCount, orderSum, buyoutCount, buyoutSum, buyoutPercent, addToCartConversion,
 * cartToOrderConversion, addToWishlistCount), и cancel_count в V_WB_FUNNEL_DAILY NULL
 * на 100 % строк. Поэтому: 01-03.09 — официальное значение из backfill,
 * 04.09 и позже — прокси is_cancel из FACT_ORDERS. Это единственная колонка
 * со смешанной семантикой, и она помечена в контракте.
 * Мера расхождения прокси на 04-09.09: 3 SKU-дня из ~150.
 *   +2  отмена в день заказа (cancel_dt = order_dt): WB вообще не считает её
 *       ни заказом, ни отменой (535580776 от 02.09 и 09.09).
 *   -1  невыкуп / возврат: в ленте заказов is_cancel = false, в воронке учтен
 *       как «Отменили» (773170315 от 07.09).
 *   Отмена НЕ в день заказа считается обоими источниками одинаково
 *   (252442517: заказ 02.09, отмена 06.09 — и XLSX и FACT_ORDERS дают 1).
 *
 * ПОЧЕМУ FACT_ORDERS НЕ ВОСПРОИЗВОДИТ ВОРОНКУ 1:1 (§3).
 * Это разные продукты WB: /supplier/orders — лента отгрузок, воронка — события
 * на карточке. 01-09.09: XLSX 137 заказов, FACT_ORDERS 127. Разница — 12 заказов,
 * которых в ленте заказов НЕТ ни в RAW_WB_ORDERS, ни в витрине, минус 2 заказа,
 * отменённых в день заказа. Проверено и исключено: сдвиг часового пояса,
 * дубли srid, sku_match_status, пропуски загрузчика (загрузки есть каждый день,
 * 10-22 в сутки), WB Клуб и юрлица (в XLSX нули), отсутствие 25-го SKU.
 * Финотчёт не содержит ни одного srid, которого нет в FACT_ORDERS, то есть витрина
 * не теряет данные — их не отдаёт сама лента заказов. Поэтому финансовый
 * контур остаётся на FACT_ORDERS, а визуальная воронка — на отчёте воронки.
 */
var S82 = {
  VER: 'unitka2.0/v8.4.0',
  D1: '2026-09-01', D2: '2026-09-10',
  NDAYS: 10,
  RB: 'S82_DATA_RB'
};

function s82sql_() {
  var P = 'project-fa311fc0-4d87-4781-986', Q = String.fromCharCode(96);
  var T = function (t) { return Q + P + '.' + t + Q; };
  var D1 = "DATE '" + S82.D1 + "'", D2 = "DATE '" + S82.D2 + "'";
  var W = ' BETWEEN ' + D1 + ' AND ' + D2;
  return 'WITH days AS (SELECT d FROM UNNEST(GENERATE_DATE_ARRAY(' + D1 + ', ' + D2 + ')) d),' +
    ' sku AS (SELECT nm_id FROM ' + T('wb_raw.REF_SKU_MASTER') + " WHERE marketplace='WB' AND active)," +
    ' g AS (SELECT s.nm_id, d.d FROM sku s CROSS JOIN days d),' +
    ' o AS (SELECT nm_id, order_date d, SUM(quantity) gross, SUM(IF(is_cancel,quantity,0)) canc,' +
    ' SAFE_DIVIDE(SUM(price_with_disc*quantity), NULLIF(SUM(quantity),0)) price FROM ' + T('wb_mart.FACT_ORDERS') +
    ' WHERE order_date' + W + ' GROUP BY 1,2),' +
    ' m AS (SELECT nm_id, day d, SUM(views) views, SUM(ad_spend) ads FROM ' + T('wb_mart.MART_SKU_DAILY') +
    ' WHERE day' + W + ' GROUP BY 1,2),' +
    ' f AS (SELECT nm_id, date_msk d, MAX(open_card_count) opens, MAX(add_to_cart_count) carts,' +
    ' MAX(orders_count) forders FROM ' + T('wb_raw.V_WB_FUNNEL_DAILY') +
    ' WHERE date_msk' + W + ' GROUP BY 1,2),' +
    ' bf AS (SELECT nm_id, date_msk d, open_card_count opens, add_to_cart_count carts,' +
    ' orders_count forders, cancel_count canc FROM ' + T('wb_raw.RAW_WB_FUNNEL_XLSX_BACKFILL') +
    ' WHERE date_msk' + W + '),' +
    ' sd AS (SELECT DISTINCT snapshot_date d FROM ' + T('wb_mart.FACT_STOCKS_SNAPSHOT') + ' WHERE snapshot_date' + W + '),' +
    ' st AS (SELECT nm_id, snapshot_date d, SUM(quantity) stock FROM ' + T('wb_mart.FACT_STOCKS_SNAPSHOT') +
    ' WHERE snapshot_date' + W + ' GROUP BY 1,2),' +
    ' pd AS (SELECT DISTINCT date_msk d FROM ' + T('wb_raw.RAW_WB_PAID_STORAGE') + ' WHERE date_msk' + W + '),' +
    ' ps AS (SELECT SAFE_CAST(nm_id AS INT64) nm_id, date_msk d, SUM(SAFE_CAST(warehouse_price AS NUMERIC)) storage FROM ' +
    T('wb_raw.RAW_WB_PAID_STORAGE') + ' WHERE date_msk' + W + ' GROUP BY 1,2)' +
    ' SELECT CAST(g.nm_id AS STRING), CAST(g.d AS STRING), CAST(IFNULL(m.views,0) AS STRING), CAST(COALESCE(f.opens, bf.opens) AS STRING),' +
    ' CAST(COALESCE(f.carts, bf.carts) AS STRING),' +
    ' CAST(COALESCE(f.forders, bf.forders, o.gross, 0) AS STRING),' +
    ' CAST(COALESCE(bf.canc, o.canc, 0) AS STRING),' +
    ' CAST(IF(sd.d IS NULL, NULL, IFNULL(st.stock,0)) AS STRING), CAST(ROUND(IFNULL(m.ads,0),2) AS STRING),' +
    ' CAST(ROUND(o.price,2) AS STRING), CAST(IF(pd.d IS NULL, NULL, ROUND(IFNULL(ps.storage,0),2)) AS STRING),' +
    " CAST(IF(f.forders IS NOT NULL,'FUNNEL_API',IF(bf.forders IS NOT NULL,'XLSX_BACKFILL','ORDERS_API')) AS STRING)" +
    ' FROM g LEFT JOIN o ON o.nm_id=g.nm_id AND o.d=g.d LEFT JOIN m ON m.nm_id=g.nm_id AND m.d=g.d' +
    ' LEFT JOIN f ON f.nm_id=g.nm_id AND f.d=g.d LEFT JOIN st ON st.nm_id=g.nm_id AND st.d=g.d' +
    ' LEFT JOIN ps ON ps.nm_id=g.nm_id AND ps.d=g.d LEFT JOIN bf ON bf.nm_id=g.nm_id AND bf.d=g.d LEFT JOIN sd ON sd.d=g.d LEFT JOIN pd ON pd.d=g.d ORDER BY 1,2';
}

function s82data() {
  var L = ['=== STAGE 8.2 §1/§4/§7 · s82data · ' + S82.VER + ' ==='];
  try {
    var t0 = new Date().getTime();
    var ss = SpreadsheetApp.openById(S8.SSID), sh = ss.getSheetByName(S8.SH);
    var rows = r7query_(s82sql_());
    L.push('BigQuery: строк ' + rows.length + ' | окно ' + S82.D1 + '..' + S82.D2);

    var bq = {}, srcDay = {};
    for (var i = 0; i < rows.length; i++) {
      var r = rows[i], nm = String(r[0]), dk = String(r[1]);
      if (!bq[nm]) bq[nm] = {};
      bq[nm][dk] = r.slice(2);
      if (!srcDay[dk]) srcDay[dk] = {};
      srcDay[dk][String(r[11])] = (srcDay[dk][String(r[11])] || 0) + 1;
    }
    var IDX = { views: 0, opens: 1, carts: 2, orders: 3, cancels: 4, stock: 5, adsIn: 6, price: 7, storage: 8 };
    var dk_ = function (i) { var n = i + 1; return '2026-09-' + (n < 10 ? '0' + n : n); };

    var wide = sh.getRange(S8.FIRST, 1, S8.DAYS, S8.NC), cur = wide.getValues();
    var head = sh.getRange(S8.TOP, 1, 1, S8.NC).getDisplayValues()[0];
    var plan = [], rb = [], noNm = [], gaps = { opens: 0, carts: 0, stock: 0, price: 0 }, filled = 0;

    for (var b = 0; b < S8.NB; b++) {
      var st0 = S8.B0 + b * S8.BW, title = '';
      for (var c = 0; c < S8.BW && !title; c++) title = String(head[st0 - 1 + c] || '').trim();
      var nm2 = (title.match(/\d{6,12}/) || [''])[0];
      if (!nm2 || !bq[nm2]) { noNm.push('#' + (b + 1) + ' ' + (nm2 || '?')); continue; }
      filled++;
      for (var k = 0; k < S8_ORDER.length; k++) {
        var key = S8_ORDER[k], col = st0 + S8_M[key], out = [], diff = false;
        for (var d = 0; d < S82.NDAYS; d++) {
          var src = bq[nm2][dk_(d)] || [], raw = src[IDX[key]];
          var v = (raw === null || raw === undefined || raw === '') ? '' : Number(raw);
          if (v === '' && gaps[key] !== undefined) gaps[key]++;
          out.push([v]);
          if (String(cur[d][col - 1]) !== String(v)) diff = true;
        }
        if (diff) {
          plan.push({ col: col, n: S82.NDAYS, vals: out });
          for (var d2 = 0; d2 < S82.NDAYS; d2++) rb.push([S8.FIRST + d2, col, cur[d2][col - 1] instanceof Date ? '' : cur[d2][col - 1]]);
        }
      }
    }
    L.push('');
    L.push('источник «Заказы факт» по дням (SKU в дне):');
    for (var dd = 0; dd < S82.NDAYS; dd++) {
      var s = srcDay[dk_(dd)] || {};
      var parts = [];
      var ord = ['FUNNEL_API', 'XLSX_BACKFILL', 'ORDERS_API'];
      for (var q = 0; q < ord.length; q++) if (s[ord[q]]) parts.push(ord[q] + ' ' + s[ord[q]]);
      for (var k3 in s) if (ord.indexOf(k3) < 0) parts.push(k3 + ' ' + s[k3]);
      L.push('  ' + dk_(dd) + '  ' + (parts.length ? parts.join(' + ') : 'нет строк'));
    }
    L.push('');
    L.push('блоков сопоставлено: ' + filled + ' | без сопоставления: ' + noNm.length + (noNm.length ? ' — ' + noNm.join(' ') : ''));
    L.push('диапазонов к записи: ' + plan.length + ' | ячеек в откате: ' + rb.length);
    if (rb.length && !r75get_(S82.RB)) { r75put_(S82.RB, { ts: new Date().toISOString(), ver: S82.VER, cells: rb }); L.push('откат сохранён: ' + S82.RB); }

    for (var p = 0; p < plan.length; p++) sh.getRange(S8.FIRST, plan[p].col, plan[p].n, 1).setValues(plan[p].vals);
    SpreadsheetApp.flush();

    var after = wide.getValues(), mism = [], checked = 0;
    var tot = { opens: 0, carts: 0, orders: 0, cancels: 0 };
    for (var b2 = 0; b2 < S8.NB; b2++) {
      var st2 = S8.B0 + b2 * S8.BW, t2 = '';
      for (var c2 = 0; c2 < S8.BW && !t2; c2++) t2 = String(head[st2 - 1 + c2] || '').trim();
      var nm3 = (t2.match(/\d{6,12}/) || [''])[0];
      if (!nm3 || !bq[nm3]) continue;
      for (var k2 = 0; k2 < S8_ORDER.length; k2++) {
        var key2 = S8_ORDER[k2], col3 = st2 + S8_M[key2];
        for (var d5 = 0; d5 < S82.NDAYS; d5++) {
          var raw2 = (bq[nm3][dk_(d5)] || [])[IDX[key2]];
          var want2 = (raw2 === null || raw2 === undefined || raw2 === '') ? '' : Number(raw2);
          var got = after[d5][col3 - 1]; checked++;
          var ok = (want2 === '') ? (got === '' || got === null) : (Math.abs(Number(got) - want2) < 0.005);
          if (!ok) mism.push(nm3 + ' ' + key2 + ' ' + r75col_(col3) + (S8.FIRST + d5) + ' лист[' + got + '] BQ[' + want2 + ']');
          if (tot[key2] !== undefined && want2 !== '' && d5 < 9) tot[key2] += Number(want2);
        }
      }
    }
    L.push('');
    L.push('=== QA: лист против BigQuery ===');
    L.push('сверено ячеек: ' + checked + ' | MISMATCH = ' + mism.length + (mism.length ? ' <-- ' + mism.slice(0, 8).join(' | ') : ''));
    L.push('GAP оставлено пустыми: переходы ' + gaps.opens + ', корзины ' + gaps.carts + ', остатки ' + gaps.stock + ', цена ' + gaps.price);
    L.push('');
    L.push('ИТОГИ 01-09.09 по 24 блокам. Официальный XLSX (22 артикула): 5382 / 523 / 137 / 5.');
    L.push('  переходы ' + tot.opens + ' | корзины ' + tot.carts + ' | заказы ' + tot.orders + ' | отмены ' + tot.cancels);
    L.push('BQ RECONCILIATION = ' + (mism.length === 0 ? 'PASS' : 'FAIL') + ' | ' + Math.round((new Date().getTime() - t0) / 1000) + ' с');
  } catch (e) { L.push('ОШИБКА: ' + e + (e && e.stack ? '\n' + e.stack : '')); }
  s8out_(L);
}

/** ОТКАТ s82data. */
function s82datarollback() {
  var L = ['=== STAGE 8.2 · s82datarollback ==='];
  try {
    var o = r75get_(S82.RB);
    if (!o || !o.cells) { L.push('откат не найден'); return s8out_(L); }
    var sh = SpreadsheetApp.openById(S8.SSID).getSheetByName(S8.SH);
    for (var i = 0; i < o.cells.length; i++) sh.getRange(o.cells[i][0], o.cells[i][1]).setValue(o.cells[i][2]);
    SpreadsheetApp.flush();
    L.push('восстановлено ячеек: ' + o.cells.length);
  } catch (e) { L.push('ОШИБКА: ' + e); }
  s8out_(L);
}


// ============ STAGE 8.2 §7 · ПЕРЕВОД LAST_CLOSED_DATE НА 10.09 ====================
// Закрытый день переводится ТОЛЬКО после того, как факт 10.09 загружен s82data().
var S82L = { VER: 'unitka2.0/v8.4.0', NEW: '2026-09-10', RB: 'S82_LCD_RB' };

function s82lcd() {
  var L = ['≡≡≡ STAGE 8.2 §7 · s82lcd · ' + S82L.VER + ' ≡≡≡'];
  try {
    var ss = SpreadsheetApp.openById(S8.SSID), sh = ss.getSheetByName(S8.SH);
    var tz = ss.getSpreadsheetTimeZone();
    var rg = ss.getRangeByName('LAST_CLOSED_DATE');
    if (!rg) { L.push('НЕТ именованного диапазона LAST_CLOSED_DATE'); s8out_(L); return; }
    var mir = sh.getRange(S8.HDR, S8.MIR);
    var fmt = function (v) { return v instanceof Date ? Utilities.formatDate(v, tz, 'yyyy-MM-dd') : String(v); };
    var was = fmt(rg.getValue()), wasM = fmt(mir.getValue());
    L.push('было: LAST_CLOSED_DATE '+was+' | зеркало WB736 '+wasM+' | имя на '+rg.getSheet().getName()+'!'+rg.getA1Notation());
    if (!r75get_(S82L.RB)) r75put_(S82L.RB, { ts: new Date().toISOString(), lcd: was, mirror: wasM, a1: rg.getSheet().getName()+'!'+rg.getA1Notation() });
    var d = new Date(2026, 8, 10);
    rg.setValue(d); mir.setValue(d); SpreadsheetApp.flush();
    var now = fmt(rg.getValue()), nowM = fmt(mir.getValue());
    L.push('стало: LAST_CLOSED_DATE '+now+' | зеркало WB736 '+nowM);
    L.push('LCD SYNC = ' + (now === S82L.NEW && nowM === S82L.NEW ? 'PASS' : 'FAIL'));

    var wide = sh.getRange(S8.FIRST, 1, S8.DAYS, S8.NC), vals = wide.getValues();
    var mtd = sh.getRange(S8.MTD, 1, 1, S8.NC).getValues()[0];
    var err = 0, errEx = [], leak = 0, leakEx = [], d10 = 0, d10e = [];
    var isErr = function (v) { return typeof v === 'string' && v.charAt(0) === '#' && /^#(REF|DIV|VALUE|NAME|N\/A|NUM|NULL|ERROR)/.test(v); };
    for (var r = 0; r < S8.DAYS; r++) for (var c = 0; c < S8.NC; c++) {
      var v = vals[r][c];
      if (isErr(v)) { err++; if (errEx.length < 6) errEx.push(r75col_(c+1)+(S8.FIRST+r)+' '+v); }
      if (r > 9 && c + 1 >= S8.B0 && c + 1 < S8.NC && v !== '' && !(v instanceof Date)) {
        var off = (c + 1 - S8.B0) % S8.BW;
        if (off !== S8_M.date && off !== S8_M.weekday && off !== S8_M.logistics && off !== S8_M.commission && off !== S8_M.spp) {
          leak++; if (leakEx.length < 6) leakEx.push(r75col_(c+1)+(S8.FIRST+r)+' [' + v + ']'); }
      }
    }
    for (var c2 = S8.B0; c2 < S8.NC; c2++) if (isErr(mtd[c2-1])) { err++; if (errEx.length < 6) errEx.push(r75col_(c2)+S8.MTD+' '+mtd[c2-1]); }
    for (var b = 0; b < S8.NB; b++) { var st = S8.B0 + b * S8.BW;
      var ks = ['views','opens','orders','carts','cancels'];
      for (var k = 0; k < ks.length; k++) { var vv = vals[9][st + S8_M[ks[k]] - 1]; if (vv !== '' && vv !== null) d10++; else d10e.push('#'+(b+1)+' '+ks[k]); } }
    L.push('');
    L.push('ФАКТ 10.09 заполнен: ' + d10 + ' из ' + (S8.NB*5) + ' ячеек' + (d10e.length ? ' | пусто: ' + d10e.slice(0,8).join(', ') : ''));
    L.push('FORMULA ERRORS = ' + err + (errEx.length ? ' <-- ' + errEx.join(' | ') : ''));
    L.push('FUTURE LEAKAGE (11-30.09) = ' + leak + (leakEx.length ? ' <-- ' + leakEx.join(' | ') : ''));
    L.push('LAST_CLOSED_DATE = 10.09.2026 | откат: ' + S82L.RB);
  } catch (e) { L.push('ОШИБКА: ' + e + (e && e.stack ? '\n' + e.stack : '')); }
  s8out_(L);
}

function s82lcdrollback() {
  var L = ['≡≡≡ s82lcdrollback ≡≡≡'];
  try {
    var o = r75get_(S82L.RB);
    if (!o) { L.push('нет точки отката ' + S82L.RB); s8out_(L); return; }
    var ss = SpreadsheetApp.openById(S8.SSID), sh = ss.getSheetByName(S8.SH);
    var pr = o.lcd.split('-');
    var d = new Date(Number(pr[0]), Number(pr[1]) - 1, Number(pr[2]));
    ss.getRangeByName('LAST_CLOSED_DATE').setValue(d);
    sh.getRange(S8.HDR, S8.MIR).setValue(d);
    SpreadsheetApp.flush();
    L.push('восстановлено LAST_CLOSED_DATE ' + o.lcd);
  } catch (e) { L.push('ОШИБКА: ' + e); }
  s8out_(L);
}


// ============ STAGE 8.2 §9/§10 · ДИАГНОСТИКА (ТОЛЬКО ЧТЕНИЕ) ============
function s82diag() {
  var L = ['≡≡≡ STAGE 8.2 §9/§10 · s82diag · только чтение ≡≡≡'];
  try {
    var ss = SpreadsheetApp.openById(S8.SSID), sh = ss.getSheetByName(S8.SH);
    var last = sh.getLastRow(), lastC = sh.getLastColumn();
    L.push('лист: строк ' + last + ', колонок ' + lastC);

    L.push('');
    L.push('--- §10 СТРОКА 768 (сентябрь, S8.PLAN) ---');
    var r768f = sh.getRange(S8.PLAN, 1, 1, S8.NC).getFormulas()[0];
    var r768v = sh.getRange(S8.PLAN, 1, 1, S8.NC).getValues()[0];
    var r768d = sh.getRange(S8.PLAN, 1, 1, S8.NC).getDisplayValues()[0];
    var nz = [];
    for (var c = 0; c < S8.NC; c++) {
      var f = r768f[c], val = r768v[c];
      if (f || (val !== '' && val !== null)) nz.push(r75col_(c + 1) + S8.PLAN + ' ' + (f ? 'FML ' + f : 'HARD [' + val + ']') + ' -> ' + r768d[c]);
    }
    L.push('заполненных ячеек: ' + nz.length);
    for (var z = 0; z < nz.length && z < 40; z++) L.push('  ' + nz[z]);

    L.push('');
    L.push('--- кто ссылается на 768 на этом листе ---');
    var all = sh.getRange(1, 1, last, lastC).getFormulas();
    var re768 = /(^|[^0-9A-Za-z_])\$?[A-Z]{1,3}\$?768(?![0-9])/;
    var refs = 0, refEx = [];
    for (var rr = 0; rr < all.length; rr++) for (var cc = 0; cc < all[rr].length; cc++) {
      var ff = all[rr][cc];
      if (ff && re768.test(ff) && (rr + 1) !== S8.PLAN) { refs++; if (refEx.length < 12) refEx.push(r75col_(cc + 1) + (rr + 1) + ' :: ' + String(ff).slice(0, 70)); }
    }
    L.push('ссылок на строку 768: ' + refs);
    for (var z2 = 0; z2 < refEx.length; z2++) L.push('  ' + refEx[z2]);

    L.push('');
    L.push('--- августовские аналоги строки плана (чтение, август НЕ трогаем) ---');
    for (var rp = 690; rp <= 740; rp++) {
      var a1 = sh.getRange(rp, 1, 1, 11).getDisplayValues()[0].join(' | ').trim();
      if (/план|ПЛАН|План/.test(a1)) L.push('  строка ' + rp + ': ' + a1.slice(0, 120));
    }

    L.push('');
    L.push('--- §9 ВЫХОДНЫЕ ---');
    var dt = sh.getRange(S8.FIRST, 1, S8.DAYS, 2).getValues();
    var bgA = sh.getRange(S8.FIRST, 1, S8.DAYS, 2).getBackgrounds();
    var st0 = S8.B0;
    var bgB = sh.getRange(S8.FIRST, st0, S8.DAYS, 1).getBackgrounds();
    var bgW = sh.getRange(S8.FIRST, st0 + S8_M.weekday, S8.DAYS, 1).getBackgrounds();
    for (var d = 0; d < S8.DAYS; d++) {
      var dv = dt[d][0], wd = (dv instanceof Date) ? ((dv.getDay() + 6) % 7) + 1 : '?';
      L.push('  ' + (S8.FIRST + d) + ' ' + (dv instanceof Date ? Utilities.formatDate(dv, ss.getSpreadsheetTimeZone(), 'dd.MM') : String(dv)) + ' wd' + wd + ' | A ' + bgA[d][0] + ' B ' + bgA[d][1] + ' | блок дата ' + bgB[d][0] + ' день ' + bgW[d][0]);
    }
  } catch (e) { L.push('ОШИБКА: ' + e + (e && e.stack ? '\n' + e.stack : '')); }
  s8out_(L);
}


// ============ STAGE 8.2 §9 · ВЫХОДНЫЕ: ДИАГНОСТИКА ============
function s82wk() {
  var L = ['≡≡≡ s82wk · выходные ≡≡≡'];
  try {
    var ss = SpreadsheetApp.openById(S8.SSID), sh = ss.getSheetByName(S8.SH);
    var rules = sh.getConditionalFormatRules();
    L.push('всего правил УФ: ' + rules.length);
    var hit = 0;
    for (var i = 0; i < rules.length; i++) {
      var b = rules[i].getBooleanCondition(); if (!b) continue;
      var vals = b.getCriteriaValues() || [];
      var f = vals.length ? String(vals[0]) : '';
      var rgs = rules[i].getRanges(), touch = false;
      for (var t = 0; t < rgs.length; t++) { var c1 = rgs[t].getColumn(), c2 = c1 + rgs[t].getNumColumns() - 1, r1 = rgs[t].getRow(), r2 = r1 + rgs[t].getNumRows() - 1;
        if (c1 <= 2 && c2 >= 1 && r1 <= 766 && r2 >= 737) { touch = true; break; } }
      if (!touch) continue;
      hit++;
      var rg = rules[i].getRanges(), a1 = [];
      for (var j = 0; j < rg.length && j < 4; j++) a1.push(rg[j].getA1Notation());
      if (hit <= 60) L.push('  #' + i + ' [' + f + '] bg ' + (b.getBackgroundObject() ? b.getBackgroundObject().asRgbColor().asHexString() : 'bg-') + '/' + (b.getFontColorObject() ? b.getFontColorObject().asRgbColor().asHexString() : 'fc-') + ' | диап. ' + rg.length + ': ' + a1.join(', ') + (rg.length > 4 ? ' ...' : ''));
    }
    L.push('правил с WEEKDAY: ' + hit);
    L.push('');
    L.push('--- статический фон (userEnteredFormat) ---');
    var res = Sheets.Spreadsheets.get(S8.SSID, { ranges: [S8.SH + '!A' + S8.FIRST + ':B' + (S8.FIRST + S8.DAYS - 1), S8.SH + '!M' + S8.FIRST + ':M' + (S8.FIRST + S8.DAYS - 1), S8.SH + '!AJ' + S8.FIRST + ':AJ' + (S8.FIRST + S8.DAYS - 1)], fields: 'sheets(data(rowData(values(userEnteredFormat(backgroundColor),effectiveFormat(backgroundColor)))))' });
    var hex = function (c) { if (!c) return '-'; var f = function (x) { var s = Math.round((x || 0) * 255).toString(16); return s.length < 2 ? '0' + s : s; }; return '#' + f(c.red) + f(c.green) + f(c.blue); };
    var g = function (di, ri, ci) { try { return hex(res.sheets[0].data[di].rowData[ri].values[ci].userEnteredFormat.backgroundColor); } catch (e) { return '-'; } };
    var ge = function (di, ri, ci) { try { return hex(res.sheets[0].data[di].rowData[ri].values[ci].effectiveFormat.backgroundColor); } catch (e) { return '-'; } };
    var dts = sh.getRange(S8.FIRST, 2, S8.DAYS, 1).getValues();
    for (var d = 0; d < S8.DAYS; d++) {
      var dv = dts[d][0], wd = (dv instanceof Date) ? ((dv.getDay() + 6) % 7) + 1 : 0;
      L.push('  ' + (S8.FIRST + d) + ' ' + (dv instanceof Date ? Utilities.formatDate(dv, ss.getSpreadsheetTimeZone(), 'dd.MM') : '?') + ' wd' + wd + (wd >= 6 ? ' ВЫХ' : '    ') + ' | стат A ' + g(0, d, 0) + ' B ' + g(0, d, 1) + ' | эфф A ' + ge(0, d, 0) + ' B ' + ge(0, d, 1) + ' M ' + ge(1, d, 0) + ' AJ ' + ge(2, d, 0));
    }
  } catch (e) { L.push('ОШИБКА: ' + e + (e && e.stack ? '\n' + e.stack : '')); }
  s8out_(L);
}


// ============ STAGE 8.2 §9 · ВЫХОДНЫЕ НА ВСЕ 30 ДНЕЙ ============
// Старые два правила были привязаны ОТНОСИТЕЛЬНОй ссылкой B737 к углу A737,
// поэтому колонка даты смотрела на СЛЕДУЮЩУЮ колонку (у сводки — на «Блогеры»),
// и весь закрытый период красился «выходным» фоном, а будущие выходные не красились вовсе.
// Новые правила: одно на блок + одно на сводку, ссылка на дату АБСОЛЮТНАЯ по колонке.
var S82W = { RB: 'S82_WK_RB', BG: { red: 0.9882353, green: 0.9372549, blue: 0.8901961 }, SID: 739487431 };

function s82week() {
  var L = ['≡≡≡ STAGE 8.2 §9 · s82week ≡≡≡'];
  try {
    var ss = SpreadsheetApp.openById(S8.SSID), sh = ss.getSheetByName(S8.SH);
    var rules = sh.getConditionalFormatRules(), old = [];
    for (var i = 0; i < rules.length; i++) {
      var b = rules[i].getBooleanCondition(); if (!b) continue;
      var cv = b.getCriteriaValues() || []; var f = cv.length ? String(cv[0]) : '';
      if (f.toUpperCase().indexOf('WEEKDAY') < 0) continue;
      var rg = rules[i].getRanges(), a1 = [];
      for (var j = 0; j < rg.length; j++) a1.push(rg[j].getA1Notation());
      old.push({ i: i, f: f, bg: b.getBackgroundObject() ? b.getBackgroundObject().asRgbColor().asHexString() : null, ranges: a1 });
    }
    L.push('найдено старых правил WEEKDAY: ' + old.length + ' — индексы ' + old.map(function (o) { return o.i; }).join(', '));
    if (!r75get_(S82W.RB)) { r75put_(S82W.RB, { ts: new Date().toISOString(), old: old }); L.push('откат сохранён: ' + S82W.RB); }

    var req = [];
    for (var d = old.length - 1; d >= 0; d--) req.push({ deleteConditionalFormatRule: { sheetId: S82W.SID, index: old[d].i } });

    var mk = function (cols, dateCol) {
      var rng = [];
      for (var k = 0; k < cols.length; k++) rng.push({ sheetId: S82W.SID, startRowIndex: S8.FIRST - 1, endRowIndex: S8.FIRST - 1 + S8.DAYS, startColumnIndex: cols[k] - 1, endColumnIndex: cols[k] });
      return { addConditionalFormatRule: { index: 0, rule: { ranges: rng, booleanRule: {
        condition: { type: 'CUSTOM_FORMULA', values: [{ userEnteredValue: '=WEEKDAY($' + r75col_(dateCol) + S8.FIRST + '\u003b2)>5' }] },
        format: { backgroundColor: S82W.BG } } } } };
    };

    req.push(mk([1, 2], 2));
    for (var b2 = 0; b2 < S8.NB; b2++) { var dc = S8.B0 + b2 * S8.BW; req.push(mk([dc, dc + S8_M.weekday], dc)); }
    L.push('запросов: удалить ' + old.length + ', добавить ' + (req.length - old.length));
    Sheets.Spreadsheets.batchUpdate({ requests: req }, S8.SSID);
    SpreadsheetApp.flush();

    var res = Sheets.Spreadsheets.get(S8.SSID, { ranges: [S8.SH + '!A' + S8.FIRST + ':B' + (S8.FIRST + S8.DAYS - 1), S8.SH + '!M' + S8.FIRST + ':M' + (S8.FIRST + S8.DAYS - 1), S8.SH + '!AJ' + S8.FIRST + ':AJ' + (S8.FIRST + S8.DAYS - 1), S8.SH + '!US' + S8.FIRST + ':US' + (S8.FIRST + S8.DAYS - 1), S8.SH + '!VP' + S8.FIRST + ':VP' + (S8.FIRST + S8.DAYS - 1)], fields: 'sheets(data(rowData(values(effectiveFormat(backgroundColor)))))' });
    var hex = function (c) { if (!c) return '-'; var f = function (x) { var s = Math.round((x || 0) * 255).toString(16); return s.length < 2 ? '0' + s : s; }; return '#' + f(c.red) + f(c.green) + f(c.blue); };
    var ge = function (di, ri, ci) { try { return hex(res.sheets[0].data[di].rowData[ri].values[ci].effectiveFormat.backgroundColor); } catch (e) { return '-'; } };
    var dts = sh.getRange(S8.FIRST, 2, S8.DAYS, 1).getValues();
    var want = S82W.BG; var WHEX = hex(want);
    var okN = 0, badN = 0, bad = [];
    for (var r = 0; r < S8.DAYS; r++) {
      var dv = dts[r][0], wd = (dv instanceof Date) ? ((dv.getDay() + 6) % 7) + 1 : 0;
      var cells = [ge(0, r, 0), ge(0, r, 1), ge(1, r, 0), ge(2, r, 0), ge(3, r, 0), ge(4, r, 0)];
      var nm = ['A', 'B', 'M(бл1 дата)', 'AJ(бл1 день)', 'US(бл24 дата)', 'VP(бл24 день)'];
      for (var q = 0; q < cells.length; q++) {
        var should = (wd >= 6); var isw = (cells[q] === WHEX);
        if (should === isw) okN++; else { badN++; if (bad.length < 10) bad.push(nm[q] + (S8.FIRST + r) + ' ' + cells[q] + (should ? ' ожид ВЫХ' : ' ожид белый')); }
      }
    }
    L.push('');
    L.push('проверено ячеек: ' + (okN + badN) + ' (30 дней × 6 контрольных колонок)');
    L.push('WEEKEND VISUAL QA = ' + (badN === 0 ? 'PASS' : 'FAIL ' + badN) + (bad.length ? ' <-- ' + bad.join(' | ') : ''));
    L.push('правил УФ стало: ' + sh.getConditionalFormatRules().length);
  } catch (e) { L.push('ОШИБКА: ' + e + (e && e.stack ? '\n' + e.stack : '')); }
  s8out_(L);
}


// ============ STAGE 8.2 §12 · ПОЛОСЫ SKU НА СТРОКЕ 735 ============
// Только строка 735. Структура 736+ НЕ трогается.
// Семейство определяется по названию товара, а не по списку nmID.
var S82B = { RB: 'S82_BAND_RB' };
var S82_FAM = [
  { k: 'набор',     re: /набор|нобор|\+|,/i,        bg: '#eaeaee' },
  { k: 'руки',      re: /рук/i,                    bg: '#dce8f2' },
  { k: 'тело',      re: /тела|амбр|виш/i,          bg: '#f3e2e6' },
  { k: 'крем лицо', re: /крем.*лиц|лиц.*крем/i,   bg: '#e2eee4' },
  { k: 'сыворотка', re: /сыворотк/i,             bg: '#f7ead9' },
  { k: 'тоник',     re: /тоник/i,                  bg: '#e6e3f2' },
  { k: 'пудра',     re: /пудр/i,                   bg: '#efeae1' }
];

function s82band() {
  var L = ['≡≡≡ STAGE 8.2 §12 · s82band ≡≡≡'];
  try {
    var ss = SpreadsheetApp.openById(S8.SSID), sh = ss.getSheetByName(S8.SH);
    var head = sh.getRange(S8.TOP, 1, 1, S8.NC).getDisplayValues()[0];
    var cur = sh.getRange(S8.TOP, 1, 1, S8.NC).getBackgrounds()[0];
    if (!r75get_(S82B.RB)) r75put_(S82B.RB, { ts: new Date().toISOString(), bg: cur });
    var cnt = {}, done = 0, miss = [];
    for (var b = 0; b < S8.NB; b++) {
      var st = S8.B0 + b * S8.BW, title = '';
      for (var c = 0; c < S8.BW && !title; c++) title = String(head[st - 1 + c] || '').trim();
      var fam = null;
      for (var q = 0; q < S82_FAM.length; q++) if (S82_FAM[q].re.test(title)) { fam = S82_FAM[q]; break; }
      if (!fam) { miss.push('#' + (b + 1) + ' ' + title.slice(0, 30)); continue; }
      sh.getRange(S8.TOP, st, 1, S8.BW).setBackground(fam.bg);
      cnt[fam.k] = (cnt[fam.k] || 0) + 1; done++;
    }
    SpreadsheetApp.flush();
    L.push('полос поставлено: ' + done + ' из ' + S8.NB + (miss.length ? ' | без семейства: ' + miss.join(', ') : ''));
    for (var k in cnt) L.push('  ' + k + ': ' + cnt[k] + ' SKU');
    L.push('SKU BAND QA = ' + (done === S8.NB ? 'PASS' : 'FAIL'));
    L.push('откат: ' + S82B.RB + ' (s82bandrollback)');
  } catch (e) { L.push('ОШИБКА: ' + e + (e && e.stack ? '\n' + e.stack : '')); }
  s8out_(L);
}

function s82bandrollback() {
  var L = ['≡≡≡ s82bandrollback ≡≡≡'];
  var o = r75get_(S82B.RB);
  if (!o) { L.push('нет точки отката'); return s8out_(L); }
  var sh = SpreadsheetApp.openById(S8.SSID).getSheetByName(S8.SH);
  sh.getRange(S8.TOP, 1, 1, S8.NC).setBackgrounds([o.bg]);
  SpreadsheetApp.flush();
  L.push('строка 735 восстановлена');
  s8out_(L);
}


// ============ STAGE 8.2 §11 · СПОКОЙНАЯ ВИЗУАЛЬНАЯ АНАЛИТИКА ============
// Статический фон, вычисленный скриптом, а НЕ тысячи правил УФ (решение владельца).
// Приоритет: Заказы (зелёный) > Реклама (тёплый) > Корзина (едва заметный).
// Показы/переходы остаются нейтральными. Ноль — нейтрален. Шкала — ВНУТРИ SKU.
var S82V = { RB: 'S82_VIZ_RB', STEPS: 4 };
var S82_VIZ = [
  { key: 'orders', off: S8_M.orders, sum: 6,  acc: [0.478, 0.667, 0.478], a: 0.55, nm: 'Заказы' },
  { key: 'adsIn',  off: S8_M.adsIn,  sum: 10, acc: [0.910, 0.710, 0.384], a: 0.45, nm: 'Реклама' },
  { key: 'carts',  off: S8_M.carts,  sum: 7,  acc: [0.663, 0.769, 0.831], a: 0.25, nm: 'Корзина' }
];

function s82hex_(r, g, b) { var f = function (x) { var s = Math.round(Math.max(0, Math.min(1, x)) * 255).toString(16); return s.length < 2 ? '0' + s : s; }; return '#' + f(r) + f(g) + f(b); }
function s82rgb_(h) { h = String(h || '#ffffff').replace('#', ''); if (h.length === 3) h = h[0]+h[0]+h[1]+h[1]+h[2]+h[2]; return [parseInt(h.substr(0,2),16)/255, parseInt(h.substr(2,2),16)/255, parseInt(h.substr(4,2),16)/255]; }
function s82mix_(base, acc, t) { var b = s82rgb_(base); return s82hex_(b[0]+(acc[0]-b[0])*t, b[1]+(acc[1]-b[1])*t, b[2]+(acc[2]-b[2])*t); }

function s82viz() {
  var L = ['≡≡≡ STAGE 8.2 §11 · s82viz ≡≡≡'];
  try {
    var ss = SpreadsheetApp.openById(S8.SSID), sh = ss.getSheetByName(S8.SH);
    var lcd = sh.getRange(S8.HDR, S8.MIR).getValue();
    var dts = sh.getRange(S8.FIRST, 2, S8.DAYS, 1).getValues();
    var closed = [];
    for (var r0 = 0; r0 < S8.DAYS; r0++) closed.push(dts[r0][0] instanceof Date && lcd instanceof Date && dts[r0][0].getTime() <= lcd.getTime());
    L.push('закрытых дней в шкале: ' + closed.filter(function (x) { return x; }).length);

    var rngs = [], tags = [];
    for (var q0 = 0; q0 < S82_VIZ.length; q0++) {
      var mc = S8.B0 + S82_VIZ[q0].off;
      rngs.push(S8.SH + '!' + r75col_(mc) + S8.FIRST + ':' + r75col_(mc) + (S8.FIRST + S8.DAYS - 1)); tags.push('blk');
      rngs.push(S8.SH + '!' + r75col_(S82_VIZ[q0].sum) + S8.FIRST + ':' + r75col_(S82_VIZ[q0].sum) + (S8.FIRST + S8.DAYS - 1)); tags.push('sum');
    }
    var res = Sheets.Spreadsheets.get(S8.SSID, { ranges: rngs, fields: 'sheets(data(rowData(values(userEnteredFormat(backgroundColor)))))' });
    var base = function (di) { var out = []; for (var r = 0; r < S8.DAYS; r++) { var c = null; try { c = res.sheets[0].data[di].rowData[r].values[0].userEnteredFormat.backgroundColor; } catch (e) {}; out.push(c ? s82hex_(c.red || 0, c.green || 0, c.blue || 0) : '#ffffff'); }; return out; };
    var BASE = { blk: {}, sum: {} };
    for (var q1 = 0; q1 < S82_VIZ.length; q1++) { BASE.blk[S82_VIZ[q1].key] = base(q1 * 2); BASE.sum[S82_VIZ[q1].key] = base(q1 * 2 + 1); }
    if (!r75get_(S82V.RB)) r75put_(S82V.RB, { ts: new Date().toISOString(), base: BASE });

    var wide = sh.getRange(S8.FIRST, 1, S8.DAYS, S8.NC).getValues();
    var painted = 0, cells = 0, stat = {};
    var paint = function (col, vals, cfg, bs) {
      var mx = 0;
      for (var r = 0; r < S8.DAYS; r++) if (closed[r]) { var n = Number(vals[r]); if (isFinite(n) && n > mx) mx = n; }
      var out = [];
      for (var r2 = 0; r2 < S8.DAYS; r2++) {
        var n2 = Number(vals[r2]), col2 = bs[r2];
        if (closed[r2] && mx > 0 && isFinite(n2) && n2 > 0) {
          var k = Math.ceil(n2 / mx * S82V.STEPS); if (k < 1) k = 1; if (k > S82V.STEPS) k = S82V.STEPS;
          col2 = s82mix_(bs[r2], cfg.acc, cfg.a * k / S82V.STEPS);
          stat[cfg.nm + '/' + k] = (stat[cfg.nm + '/' + k] || 0) + 1;
        }
        out.push([col2]); cells++;
      }
      sh.getRange(S8.FIRST, col, S8.DAYS, 1).setBackgrounds(out); painted++;
    };

    for (var b = 0; b < S8.NB; b++) {
      var st = S8.B0 + b * S8.BW;
      for (var q2 = 0; q2 < S82_VIZ.length; q2++) {
        var cfg2 = S82_VIZ[q2], cc = st + cfg2.off, vv = [];
        for (var r3 = 0; r3 < S8.DAYS; r3++) vv.push(wide[r3][cc - 1]);
        paint(cc, vv, cfg2, BASE.blk[cfg2.key]);
      }
    }
    for (var q3 = 0; q3 < S82_VIZ.length; q3++) {
      var cfg3 = S82_VIZ[q3], vs = [];
      for (var r4 = 0; r4 < S8.DAYS; r4++) vs.push(wide[r4][cfg3.sum - 1]);
      paint(cfg3.sum, vs, cfg3, BASE.sum[cfg3.key]);
    }
    SpreadsheetApp.flush();
    L.push('колонок перекрашено: ' + painted + ' (24 блока × 3 + сводка × 3), ячеек ' + cells);
    var ks = []; for (var s in stat) ks.push(s + ':' + stat[s]);
    ks.sort();
    L.push('ступени (метрика/уровень:ячеек): ' + ks.join('  '));
    L.push('правил УФ на листе: ' + sh.getConditionalFormatRules().length + ' (новых не добавлено)');
    L.push('VISUAL ANALYTICS QA = ' + (painted === S8.NB * 3 + 3 ? 'PASS' : 'FAIL'));
    L.push('откат: ' + S82V.RB + ' (s82vizrollback)');
  } catch (e) { L.push('ОШИБКА: ' + e + (e && e.stack ? '\n' + e.stack : '')); }
  s8out_(L);
}

function s82vizrollback() {
  var L = ['≡≡≡ s82vizrollback ≡≡≡'];
  var o = r75get_(S82V.RB);
  if (!o || !o.base) { L.push('нет точки отката'); return s8out_(L); }
  var sh = SpreadsheetApp.openById(S8.SSID).getSheetByName(S8.SH), n = 0;
  var col2d = function (a) { var o2 = []; for (var i = 0; i < a.length; i++) o2.push([a[i]]); return o2; };
  for (var b = 0; b < S8.NB; b++) for (var q = 0; q < S82_VIZ.length; q++) { sh.getRange(S8.FIRST, S8.B0 + b * S8.BW + S82_VIZ[q].off, S8.DAYS, 1).setBackgrounds(col2d(o.base.blk[S82_VIZ[q].key])); n++; }
  for (var q4 = 0; q4 < S82_VIZ.length; q4++) { sh.getRange(S8.FIRST, S82_VIZ[q4].sum, S8.DAYS, 1).setBackgrounds(col2d(o.base.sum[S82_VIZ[q4].key])); n++; }
  SpreadsheetApp.flush();
  L.push('восстановлено колонок: ' + n);
  s8out_(L);
}


function s82probe() {
  var L = ['≡≡≡ s82probe ≡≡≡'];
  try {
    var sh = SpreadsheetApp.openById(S8.SSID).getSheetByName(S8.SH);
    var cols = ['F', 'G', 'J', 'Q', 'R', 'X'];
    var rngs = [];
    for (var i = 0; i < cols.length; i++) rngs.push(S8.SH + '!' + cols[i] + '737:' + cols[i] + '746');
    var res = Sheets.Spreadsheets.get(S8.SSID, { ranges: rngs, fields: 'sheets(data(rowData(values(userEnteredFormat(backgroundColor),effectiveFormat(backgroundColor)))))' });
    var hx = function (c) { if (!c) return '-'; var f = function (x) { var s = Math.round((x || 0) * 255).toString(16); return s.length < 2 ? '0' + s : s; }; return f(c.red) + f(c.green) + f(c.blue); };
    for (var d = 0; d < cols.length; d++) {
      var u = [], ef = [];
      for (var r = 0; r < 10; r++) {
        var cell = null; try { cell = res.sheets[0].data[d].rowData[r].values[0]; } catch (e) {}
        u.push(cell && cell.userEnteredFormat ? hx(cell.userEnteredFormat.backgroundColor) : '-');
        ef.push(cell && cell.effectiveFormat ? hx(cell.effectiveFormat.backgroundColor) : '-');
      }
      L.push(cols[d] + ' стат: ' + u.join(' '));
      L.push(cols[d] + ' эфф: ' + ef.join(' '));
    }
  } catch (e) { L.push('ОШИБКА: ' + e); }
  s8out_(L);
}


// ============ STAGE 8.2 §11 (версия 2) · АНАЛИТИКА УФ, А НЕ СТАТИКА ============
// Почему не статика: колонки группы AUTO уже перекрыты правилом УФ с ровным
// фоном #f1f8f4 — статический фон под ним не виден (замерено s82probe).
// 4 ступени × 3 метрики × 25 целей (24 блока + сводка) "+E+" 300 правил.
// Порог — доля от MAX СВОЕЙ же колонки, поэтому шкала живёт внутри SKU
// и обновляется сама при загрузке нового дня — ежедневный прогон не нужен.
var S82V2 = { RB: 'S82_VIZ2_RB', SID: 739487431 };
var S82_VIZ2 = [
  { off: S8_M.orders, sum: 6,  nm: 'Заказы',  c: ['#e4f1e9', '#d2e8da', '#bfdecb', '#a9d4b8'] },
  { off: S8_M.adsIn,  sum: 10, nm: 'Реклама', c: ['#faf2e3', '#f5e8cd', '#efdcb4', '#e8cf99'] },
  { off: S8_M.carts,  sum: 7,  nm: 'Корзина', c: ['#eef4f6', '#e6eff2', '#dee9ee', '#d6e4ea'] }
];
var S82_LV = [0, '0,25', '0,5', '0,75'];

function s82viz2() {
  var L = ['≡≡≡ STAGE 8.2 §11 · s82viz2 ≡≡≡'];
  try {
    var ss = SpreadsheetApp.openById(S8.SSID), sh = ss.getSheetByName(S8.SH);
    var before = sh.getConditionalFormatRules().length;
    var rgbOf = function (h) { h = h.replace('#', ''); return { red: parseInt(h.substr(0,2),16)/255, green: parseInt(h.substr(2,2),16)/255, blue: parseInt(h.substr(4,2),16)/255 }; };
    var req = [], cols = [];
    for (var q = 0; q < S82_VIZ2.length; q++) {
      cols.push({ c: S82_VIZ2[q].sum, cfg: S82_VIZ2[q] });
      for (var b = 0; b < S8.NB; b++) cols.push({ c: S8.B0 + b * S8.BW + S82_VIZ2[q].off, cfg: S82_VIZ2[q] });
    }
    for (var i = 0; i < cols.length; i++) {
      var cc = cols[i].c, cf = cols[i].cfg, A = r75col_(cc);
      var rng = { sheetId: S82V2.SID, startRowIndex: S8.FIRST - 1, endRowIndex: S8.FIRST - 1 + S8.DAYS, startColumnIndex: cc - 1, endColumnIndex: cc };
      for (var lv = 0; lv < S82_LV.length; lv++) {
        var cond = S82_LV[lv] === 0
          ? '\u003dAND($' + A + S8.FIRST + '<>""\u003b$' + A + S8.FIRST + '>0)'
          : '\u003dAND($' + A + S8.FIRST + '<>""\u003b$' + A + S8.FIRST + '>' + S82_LV[lv] + '*MAX($' + A + '$' + S8.FIRST + ':$' + A + '$' + (S8.FIRST + S8.DAYS - 1) + '))';
        req.push({ addConditionalFormatRule: { index: 0, rule: { ranges: [rng], booleanRule: { condition: { type: 'CUSTOM_FORMULA', values: [{ userEnteredValue: cond }] }, format: { backgroundColor: rgbOf(cf.c[lv]) } } } } });
      }
    }
    L.push('колонок: ' + cols.length + ' | правил к добавлению: ' + req.length);
    if (!r75get_(S82V2.RB)) r75put_(S82V2.RB, { ts: new Date().toISOString(), added: req.length, before: before });
    for (var s = 0; s < req.length; s += 100) Sheets.Spreadsheets.batchUpdate({ requests: req.slice(s, s + 100) }, S8.SSID);
    SpreadsheetApp.flush();

    var probe = [], names = ['F', 'J', 'G', 'Q', 'X', 'R'];
    for (var n = 0; n < names.length; n++) probe.push(S8.SH + '!' + names[n] + S8.FIRST + ':' + names[n] + (S8.FIRST + 9));
    var res = Sheets.Spreadsheets.get(S8.SSID, { ranges: probe, fields: 'sheets(data(rowData(values(effectiveFormat(backgroundColor)))))' });
    var hx = function (c) { if (!c) return '-'; var f = function (x) { var s = Math.round((x || 0) * 255).toString(16); return s.length < 2 ? '0' + s : s; }; return f(c.red) + f(c.green) + f(c.blue); };
    var distinct = 0;
    for (var d = 0; d < names.length; d++) {
      var row = [], seen = {};
      for (var r = 0; r < 10; r++) { var c2 = null; try { c2 = res.sheets[0].data[d].rowData[r].values[0].effectiveFormat.backgroundColor; } catch (e) {}; var h = hx(c2); row.push(h); seen[h] = 1; }
      var k = 0; for (var s2 in seen) k++; if (k > 1) distinct++;
      L.push(names[d] + ' (' + k + ' оттенков): ' + row.join(' '));
    }
    L.push('');
    L.push('правил УФ: было ' + before + ', стало ' + sh.getConditionalFormatRules().length);
    L.push('VISUAL ANALYTICS QA = ' + (distinct === names.length ? 'PASS' : 'FAIL ' + distinct + '/' + names.length));
    L.push('откат: ' + S82V2.RB + ' (s82viz2rollback)');
  } catch (e) { L.push('ОШИБКА: ' + e + (e && e.stack ? '\n' + e.stack : '')); }
  s8out_(L);
}

function s82viz2rollback() {
  var L = ['≡≡≡ s82viz2rollback ≡≡≡'];
  var o = r75get_(S82V2.RB);
  if (!o) { L.push('нет точки отката'); return s8out_(L); }
  var req = [];
  for (var i = 0; i < o.added; i++) req.push({ deleteConditionalFormatRule: { sheetId: S82V2.SID, index: 0 } });
  for (var s = 0; s < req.length; s += 100) Sheets.Spreadsheets.batchUpdate({ requests: req.slice(s, s + 100) }, S8.SSID);
  L.push('удалено правил: ' + o.added);
  s8out_(L);
}


// ============ STAGE 8.2 §10 · СНЯТИЕ СТРОКИ 768 ИЗ SEPTEMBER MASTER ============
// Решение владельца 11.09: это неполный исторический ручной plan-layer:
// заполнен 5 блоков из 24, смешаны две семантики (план месяца и остаток до плана),
// ссылок на строку на листе ноль. Новый planning layer СЕЙЧАС НЕ создаётся.
// Сначала полный снимок в Script Properties и в лог, потом очистка СОДЕРЖИМОГО (формат не трогается).
var S82R = { RB: 'S82_R768_RB' };

function s82r768() {
  var L = ['≡≡≡ STAGE 8.2 §10 · s82r768 ≡≡≡'];
  try {
    var ss = SpreadsheetApp.openById(S8.SSID), sh = ss.getSheetByName(S8.SH);
    var rg = sh.getRange(S8.PLAN, 1, 1, S8.NC);
    var f = rg.getFormulas()[0], val = rg.getValues()[0], dsp = rg.getDisplayValues()[0];
    var snap = [], head = sh.getRange(S8.TOP, 1, 1, S8.NC).getDisplayValues()[0];
    for (var c = 0; c < S8.NC; c++) {
      if (!f[c] && (val[c] === '' || val[c] === null)) continue;
      var col = c + 1, owner = 'сводка', off = -1;
      if (col >= S8.B0 && col < S8.NC) {
        var b = Math.floor((col - S8.B0) / S8.BW), st = S8.B0 + b * S8.BW;
        off = (col - S8.B0) % S8.BW;
        owner = 'блок #' + (b + 1);
        for (var q = 0; q < S8.BW && owner.indexOf(':') < 0; q++) { var t = String(head[st - 1 + q] || '').trim(); if (t) { owner = 'блок #' + (b + 1) + ': ' + t.slice(0, 34); } }
      }
      var mname = '-';
      for (var k in S8_M) if (S8_M[k] === off) mname = k;
      snap.push({ col: col, a1: r75col_(col) + S8.PLAN, f: f[c] || '', v: (val[c] === null ? '' : String(val[c])), d: dsp[c], owner: owner, metric: mname });
    }
    L.push('заполненных ячеек в строке ' + S8.PLAN + ': ' + snap.length);
    L.push('');
    L.push('--- АУДИТ СОДЕРЖИМОГО ПЕРЕД ОЧИСТКОЙ ---');
    for (var s = 0; s < snap.length; s++) {
      var o = snap[s];
      L.push('  ' + o.a1 + ' | ' + o.owner + ' | метрика ' + o.metric + ' | ' + (o.f ? 'ФОРМУЛА ' + o.f : 'КОНСТАНТА ' + o.v) + ' -> ' + o.d);
    }

    var refs = 0, refEx = [];
    var last = sh.getLastRow(), lastC = sh.getLastColumn();
    var all = sh.getRange(1, 1, last, lastC).getFormulas();
    var re = /(^|[^0-9A-Za-z_])\$?[A-Z]{1,3}\$?768(?![0-9])/;
    for (var rr = 0; rr < all.length; rr++) for (var cc = 0; cc < all[rr].length; cc++) {
      var ff = all[rr][cc];
      if (ff && re.test(ff) && (rr + 1) !== S8.PLAN) { refs++; if (refEx.length < 8) refEx.push(r75col_(cc + 1) + (rr + 1)); }
    }
    L.push('');
    L.push('ссылок на строку 768 вне самой строки: ' + refs + (refEx.length ? ' <-- ' + refEx.join(' ') : ''));
    if (refs > 0) { L.push('ОСТАНОВ: на строку ссылаются, очистка НЕ выполнена'); s8out_(L); return; }

    if (!r75get_(S82R.RB)) { r75put_(S82R.RB, { ts: new Date().toISOString(), row: S8.PLAN, cells: snap }); L.push('снимок сохранён: ' + S82R.RB); }
    rg.clearContent();
    SpreadsheetApp.flush();

    var f2 = sh.getRange(S8.PLAN, 1, 1, S8.NC).getFormulas()[0], v2 = sh.getRange(S8.PLAN, 1, 1, S8.NC).getValues()[0];
    var left = 0, leftEx = [];
    for (var c2 = 0; c2 < S8.NC; c2++) if (f2[c2] || (v2[c2] !== '' && v2[c2] !== null)) { left++; if (leftEx.length < 8) leftEx.push(r75col_(c2 + 1) + S8.PLAN); }
    L.push('');
    L.push('очищено ячеек: ' + snap.length + ' | осталось заполненных: ' + left + (leftEx.length ? ' <-- ' + leftEx.join(' ') : ''));
    L.push('формат строки НЕ трогался (clearContent, не clearFormat)');
    L.push('ROW 768 REMOVED FROM SEPTEMBER MASTER = ' + (left === 0 ? 'PASS' : 'FAIL'));
    L.push('откат: ' + S82R.RB + ' (s82r768rollback)');
  } catch (e) { L.push('ОШИБКА: ' + e + (e && e.stack ? '\n' + e.stack : '')); }
  s8out_(L);
}

function s82r768rollback() {
  var L = ['≡≡≡ s82r768rollback ≡≡≡'];
  var o = r75get_(S82R.RB);
  if (!o || !o.cells) { L.push('нет снимка'); return s8out_(L); }
  var sh = SpreadsheetApp.openById(S8.SSID).getSheetByName(S8.SH), n = 0;
  for (var i = 0; i < o.cells.length; i++) {
    var c = o.cells[i];
    if (c.f) sh.getRange(o.row, c.col).setFormula(c.f); else sh.getRange(o.row, c.col).setValue(c.v);
    n++;
  }
  SpreadsheetApp.flush();
  L.push('восстановлено ячеек: ' + n);
  s8out_(L);
}
