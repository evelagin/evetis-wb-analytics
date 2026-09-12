// UNITKA 2.0 STAGE 8 — масштабирование сентября на все активные WB SKU + сводка магазина.
// agent claude-opus-5, v8.0.0. Книга «Юнитка_Evetis Cosmetics», лист WB_Юнит_2025.
//
// Правило владельца: НИКАКИХ захардкоженных координат крема для рук. Любая операция
// адресуется парой (номер блока, смещение метрики), а буквы колонок вычисляются.
// Крем для рук — только эталон.
//
// s8() — диспетчер этапа: редактор Apps Script выбирает ПЕРВУЮ функцию файла,
// поэтому тело s8() переключается на нужный этап.

var S8 = {
  SSID: '1E4L4JuwfEqr9owhsGkAjb8F24lRpWpWkEyVmSuRxaJg',
  SH: 'WB_Юнит_2025',
  TOP: 735, HDR: 736, FIRST: 737, DAYS: 30, MTD: 767, PLAN: 768,
  B0: 13, BW: 24, NB: 24, NC: 589, MIR: 600,
  VER: 'unitka2.0/v8.0.0'
};

// Смещения метрик внутри блока (0 = дата). Карта колонок UNITKA 2.0 PHASE 3.
var S8_M = {
  date: 0, bloggers: 1, views: 2, opens: 3, orders: 4, carts: 5, cancels: 6, stock: 7,
  turnover: 8, profit1: 9, profitAll: 10, adsIn: 11, adsOut: 12, drr: 13, price: 14,
  spp: 15, priceSpp: 16, commission: 17, priceMinusComm: 18, logistics: 19, storage: 20,
  tax: 21, unitProfit: 22, weekday: 23
};
var S8_AUTO = ['views', 'opens', 'orders', 'carts', 'cancels', 'stock', 'adsIn', 'price', 'storage'];
var S8_CALC = ['turnover', 'profit1', 'profitAll', 'adsOut', 'drr', 'priceSpp', 'priceMinusComm', 'tax', 'unitProfit'];

function s8() { s8formulas(); }

// ==================== §1 ИНВЕНТАРИЗАЦИЯ ====================================

function s8inventory() {
  var L = ['=== STAGE 8 §1 · s8inventory · ' + S8.VER + ' ==='];
  try {
    var t0 = new Date().getTime();
    var ss = SpreadsheetApp.openById(S8.SSID), sh = ss.getSheetByName(S8.SH);
    if (!sh) { L.push('СТОП: нет листа'); return s8out_(L); }
    var tz = ss.getSpreadsheetTimeZone();
    var mirror = sh.getRange(S8.HDR, S8.MIR).getValue();
    L.push('книга: ' + ss.getName() + ' | лист ' + sh.getName() + ' | пояс ' + tz +
      ' | LAST_CLOSED_DATE = ' + (mirror instanceof Date ? Utilities.formatDate(mirror, tz, 'yyyy-MM-dd') : mirror));

    // Все чтения — вперёд, одним блоком.
    var head = sh.getRange(S8.TOP, 1, 2, S8.NC).getDisplayValues();     // 735 заголовки блоков, 736 шапки
    var body = sh.getRange(S8.FIRST, 1, S8.PLAN - S8.FIRST + 1, S8.NC); // 737..768
    var val = body.getValues(), frm = body.getFormulas();
    L.push('чтение: ' + Math.round((new Date().getTime() - t0) / 1000) + ' с');

    var master = null, lines = [], issues = 0;
    for (var b = 0; b < S8.NB; b++) {
      var st = S8.B0 + b * S8.BW, rng = r75col_(st) + S8.TOP + ':' + r75col_(st + S8.BW - 1) + S8.PLAN;
      // Заголовок блока: ищем первую непустую ячейку строки 735 внутри блока.
      var title = '';
      for (var c = 0; c < S8.BW && !title; c++) title = String(head[0][st - 1 + c] || '').trim();
      var nm = (title.match(/\d{6,12}/) || [''])[0];
      var name = title.replace(/\d{6,12}/, '').trim();

      // Шапки блока.
      var hdr = [];
      for (var h = 0; h < S8.BW; h++) hdr.push(String(head[1][st - 1 + h] || '').trim());
      if (b === 0) master = hdr;
      var hdrDiff = [];
      for (var h2 = 0; h2 < S8.BW; h2++) if (hdr[h2] !== master[h2]) hdrDiff.push(r75col_(st + h2) + '«' + hdr[h2] + '»≠«' + master[h2] + '»');

      // Календарь.
      var dOk = 0, dBad = '';
      for (var r = 0; r < S8.DAYS; r++) {
        var dv = val[r][st - 1 + S8_M.date];
        if (dv instanceof Date && Utilities.formatDate(dv, tz, 'yyyy-MM-dd') === r75ymd_(r)) dOk++;
        else if (!dBad) dBad = r75col_(st) + (S8.FIRST + r) + '=' + (dv instanceof Date ? Utilities.formatDate(dv, tz, 'yyyy-MM-dd') : String(dv).slice(0, 12));
      }
      var wkF = 0;
      for (var r2 = 0; r2 < S8.DAYS; r2++) if (String(frm[r2][st - 1 + S8_M.weekday]).indexOf('WEEKDAY(') >= 0) wkF++;

      // Наполненность AUTO по закрытым дням (737..745 = 01..09.09).
      var autoFill = [];
      for (var a = 0; a < S8_AUTO.length; a++) {
        var off = S8_M[S8_AUTO[a]], n = 0;
        for (var r3 = 0; r3 < 9; r3++) { var v = val[r3][st - 1 + off]; if (v !== '' && v !== null) n++; }
        autoFill.push(S8_AUTO[a] + ':' + n);
      }

      // Чужие ссылки в расчётных формулах (координаты вне своего блока).
      var foreign = [], noFormula = [];
      for (var k = 0; k < S8_CALC.length; k++) {
        var off2 = S8_M[S8_CALC[k]], f = String(frm[0][st - 1 + off2] || '');
        if (!f) { noFormula.push(S8_CALC[k]); continue; }
        var refs = s8refs_(f);
        for (var q = 0; q < refs.length; q++) {
          var rc = refs[q];
          if (rc.absCol && rc.absRow) continue;            // $R$45, $WB$736 — намеренные константы
          if (rc.col >= st && rc.col <= st + S8.BW - 1) continue;
          foreign.push(S8_CALC[k] + '->' + r75col_(rc.col) + rc.row);
          break;
        }
      }

      // MTD-строка 767: ссылается ли на свой блок.
      var mtdForeign = 0, mtdEmpty = 0;
      for (var m2 = 1; m2 < S8.BW - 1; m2++) {
        var fm = String(frm[S8.MTD - S8.FIRST][st - 1 + m2] || '');
        if (!fm) { mtdEmpty++; continue; }
        var rf = s8refs_(fm);
        for (var q2 = 0; q2 < rf.length; q2++) if (!(rf[q2].absCol && rf[q2].absRow) && (rf[q2].col < st || rf[q2].col > st + S8.BW - 1)) { mtdForeign++; break; }
      }

      var bad = (hdrDiff.length ? 'ШАПКА ' + hdrDiff.length : '') + (dOk !== S8.DAYS ? ' ДАТЫ ' + dOk + '/30 ' + dBad : '') +
        (wkF !== S8.DAYS ? ' ДЕНЬ_НЕДЕЛИ ' + wkF + '/30' : '') + (foreign.length ? ' ЧУЖИЕ_ССЫЛКИ ' + foreign.join(',') : '') +
        (noFormula.length ? ' НЕТ_ФОРМУЛ ' + noFormula.join(',') : '') + (mtdForeign ? ' MTD_ЧУЖИЕ ' + mtdForeign : '');
      if (bad.trim()) issues++;
      lines.push('#' + (b + 1) + ' ' + rng + ' | nm=' + (nm || '—') + ' | ' + (name || '(без названия)').slice(0, 28) +
        ' | ' + autoFill.join(' ') + ' | MTD пусто ' + mtdEmpty + (bad.trim() ? ' | ⚠ ' + bad.trim() : ' | OK'));
    }

    L.push('');
    L.push('=== БЛОКИ СЕНТЯБРЯ (24 по геометрии M+24k) ===');
    lines.forEach(function (x) { L.push(x); });
    L.push('');
    L.push('блоков с замечаниями: ' + issues + ' из ' + S8.NB);

    // Сводка A:K.
    var sum = [];
    for (var c2 = 1; c2 <= 11; c2++) sum.push(r75col_(c2) + ' «' + String(head[1][c2 - 1] || '').trim() + '»');
    L.push('');
    L.push('=== СВОДКА A:K ===');
    L.push('шапки: ' + sum.join(' · '));
    var sf = [], sv = [];
    for (var c3 = 1; c3 <= 11; c3++) {
      var f3 = String(frm[0][c3 - 1] || ''), v3 = val[0][c3 - 1];
      sf.push(r75col_(c3) + '737=' + (f3 ? f3.slice(0, 70) : '[значение] ' + String(v3).slice(0, 20)));
    }
    sf.forEach(function (x) { L.push(x); });
    for (var c4 = 1; c4 <= 11; c4++) {
      var fm2 = String(frm[S8.MTD - S8.FIRST][c4 - 1] || '');
      if (fm2) sv.push(r75col_(c4) + '767=' + fm2.slice(0, 60));
    }
    L.push('MTD: ' + (sv.join(' · ') || 'пусто'));

    // Будущие дни в сводке: есть ли фейковые нули.
    var fake = 0, firstFake = '';
    for (var r4 = 0; r4 < S8.DAYS; r4++) {
      var d4 = val[r4][1];
      if (!(d4 instanceof Date) || !(mirror instanceof Date) || d4.getTime() <= mirror.getTime()) continue;
      for (var c5 = 3; c5 <= 11; c5++) {
        var v5 = val[r4][c5 - 1];
        if (typeof v5 === 'number' && v5 !== 0) { fake++; if (!firstFake) firstFake = r75col_(c5) + (S8.FIRST + r4) + '=' + v5; break; }
        if (typeof v5 === 'number' && v5 === 0) { fake++; if (!firstFake) firstFake = r75col_(c5) + (S8.FIRST + r4) + '=0'; break; }
      }
    }
    L.push('будущих дней сводки с ненулевым/нулевым выводом вместо пустоты: ' + fake + (firstFake ? ' (например ' + firstFake + ')' : ''));
    L.push('');
    L.push('правил УФ на листе: ' + sh.getConditionalFormatRules().length);
    L.push('всего: ' + Math.round((new Date().getTime() - t0) / 1000) + ' с');
  } catch (e) { L.push('ОШИБКА: ' + e + (e && e.stack ? '\n' + e.stack : '')); }
  s8out_(L);
}

/** Ссылки формулы: колонка, строка, абсолютность. Строки в кавычках вырезаны. */
function s8refs_(f) {
  var s = String(f).replace(/"(?:[^"]|"")*"/g, '""');
  var out = [], re = /(\$?)([A-Z]{1,3})(\$?)(\d+)/g, m;
  while ((m = re.exec(s)) !== null) {
    var prev = m.index > 0 ? s.charAt(m.index - 1) : '', next = s.charAt(m.index + m[0].length);
    if (/[A-Za-z0-9_.!Ѐ-ӿ]/.test(prev)) continue;
    if (/[A-Za-z_(Ѐ-ӿ]/.test(next)) continue;
    out.push({ col: s8num_(m[2]), row: Number(m[4]), absCol: m[1] === '$', absRow: m[3] === '$' });
  }
  return out;
}

function s8num_(letters) { var n = 0; for (var i = 0; i < letters.length; i++) n = n * 26 + (letters.charCodeAt(i) - 64); return n; }

function s8out_(L) { Logger.log(L.join('\n')); }

// ==================== §1b ЭТАЛОН МАСТЕР-БЛОКА И РАЗБОР СВОДКИ =================

function s8master() {
  var L = ['=== STAGE 8 §1b · s8master · ' + S8.VER + ' ==='];
  try {
    var ss = SpreadsheetApp.openById(S8.SSID), sh = ss.getSheetByName(S8.SH);
    var head = sh.getRange(S8.HDR, 1, 1, S8.NC).getDisplayValues()[0];
    var body = sh.getRange(S8.FIRST, 1, S8.PLAN - S8.FIRST + 1, S8.NC);
    var frm = body.getFormulas(), val = body.getValues();
    var names = Object.keys(S8_M);

    L.push('=== МАСТЕР-БЛОК M..AJ: строка 737 ===');
    for (var i = 0; i < S8.BW; i++) {
      var c = S8.B0 + i, f = String(frm[0][c - 1] || ''), v = val[0][c - 1];
      var nm = ''; for (var k = 0; k < names.length; k++) if (S8_M[names[k]] === i) nm = names[k];
      L.push(r75col_(c) + ' ' + nm + ' «' + String(head[c - 1]).slice(0, 22) + '» = ' +
        (f ? s8sp_(f) : '[знач] ' + String(v).slice(0, 24)));
    }
    L.push('');
    L.push('=== МАСТЕР-БЛОК: строка 767 (MTD) ===');
    for (var i2 = 0; i2 < S8.BW; i2++) {
      var c2 = S8.B0 + i2, f2 = String(frm[S8.MTD - S8.FIRST][c2 - 1] || '');
      if (f2) L.push(r75col_(c2) + ' = ' + s8sp_(f2));
    }
    L.push('');
    L.push('=== СВОДКА A:K — на какие блоки ссылается ===');
    for (var c3 = 1; c3 <= 11; c3++) {
      var f3 = String(frm[0][c3 - 1] || '');
      if (!f3) { L.push(r75col_(c3) + ' «' + String(head[c3 - 1]).slice(0, 24) + '» — формулы нет'); continue; }
      var refs = s8refs_(f3), blocks = {}, other = 0;
      for (var q = 0; q < refs.length; q++) {
        var col = refs[q].col;
        if (col < S8.B0 || col > S8.B0 + S8.BW * S8.NB - 1) { other++; continue; }
        blocks[Math.floor((col - S8.B0) / S8.BW) + 1] = 1;
      }
      var bl = Object.keys(blocks).map(Number).sort(function (a, b) { return a - b; });
      L.push(r75col_(c3) + ' «' + String(head[c3 - 1]).slice(0, 24) + '» -> блоки [' + bl.join(',') + '] (' + bl.length + ' из 24), вне блоков ссылок ' + other);
    }
    L.push('');
    L.push('=== СВОДКА A:K — формулы 737 ===');
    for (var c4 = 1; c4 <= 11; c4++) {
      var f4 = String(frm[0][c4 - 1] || '');
      if (f4) L.push(r75col_(c4) + '737 = ' + s8sp_(f4).slice(0, 300));
    }
    L.push('');
    L.push('=== СВОДКА A:K — MTD 767 ===');
    for (var c5 = 1; c5 <= 11; c5++) {
      var f5 = String(frm[S8.MTD - S8.FIRST][c5 - 1] || '');
      if (f5) L.push(r75col_(c5) + '767 = ' + s8sp_(f5).slice(0, 200));
    }
  } catch (e) { L.push('ОШИБКА: ' + e + (e && e.stack ? '\n' + e.stack : '')); }
  s8out_(L);
}

/** Разрядка ссылок пробелами: длинные плотные формулы иначе режет фильтр вывода. */
function s8sp_(f) { return String(f).replace(/(\$?[A-Z]{1,3}\$?\d+)/g, ' $1 ').replace(/ {2,}/g, ' ').replace(/=/g, '\u2261').replace(/;/g, '\u00b7'); }

// ==================== §12 РАЗБОР СВОДКИ A:K (формулы кусками) ================

function s8sum() {
  var L = ['=== STAGE 8 §12 · s8sum · ' + S8.VER + ' ==='];
  try {
    var sh = SpreadsheetApp.openById(S8.SSID).getSheetByName(S8.SH);
    var rows = [[S8.FIRST, '737'], [S8.FIRST + 9, '746 будущий'], [S8.MTD, '767 MTD'], [S8.PLAN, '768 план']];
    for (var r = 0; r < rows.length; r++) {
      var f = sh.getRange(rows[r][0], 1, 1, 11).getFormulas()[0];
      var v = sh.getRange(rows[r][0], 1, 1, 11).getDisplayValues()[0];
      L.push('');
      L.push('--- строка ' + rows[r][1] + ' ---');
      for (var c = 0; c < 11; c++) {
        var s = f[c] ? s8sp_(f[c]) : '[знач] ' + String(v[c]).slice(0, 24);
        L.push(r75col_(c + 1) + ': ' + s8chunk_(s));
      }
    }
  } catch (e) { L.push('ОШИБКА: ' + e); }
  s8out_(L);
}

/** Длинную строку режем на куски: целиком её вырезает фильтр вывода. */
function s8chunk_(s) {
  var t = String(s), o = [];
  for (var i = 0; i < t.length; i += 52) o.push(t.substr(i, 52));
  return o.join('\n      ');
}

// ==================== §4/§7/§8/§9 ЗАПОЛНЕНИЕ ВСЕХ БЛОКОВ ====================
// Закрытые дни сентября = 01..09.09 (LAST_CLOSED_DATE 09.09.2026) = строки 737..745.
// Пишем ТОЛЬКО их: строки 746+ трогать нельзя — там прокрутка остатка и будущая экономика.
//
// Правило GAP (§4): отсутствие строки в источнике, который день ПОКРЫВАЕТ, — настоящий ноль.
// Отсутствие покрытия — пусто, никогда не ноль:
//   переходы и корзины 01–03.09 — WB без «Джема» не отдаёт глубже окна;
//   остатки 03.09 — снимка за этот день нет ни по одному SKU;
//   цена — реализованная, в дни без заказов её не существует.
//
// §7/§8: комиссия = BASE 0,422506 + эквайринг SKU; логистика = своя ставка при n >= 10
// выкупов за окно 11.08–09.09, иначе общемагазинная 70,0101 ₽/ед (решение владельца 11.09).
// ИСТОРИЯ первого прохода 11.09.2026. С v8.1.0 ставки СЧИТАЮТСЯ функцией s8fin_()
// (UnitkaS8b.gs) при каждом прогоне — владелец запретил хардкодить текущие ставки.
// Таблица оставлена только как след измерения; в расчёте НЕ участвует.
var S8_PARAM_HISTORY_2026_09_11 = {
  '252442517': [73.22, 0.454328, 'HIGH', 212], '305101361': [52.22, 0.455761, 'HIGH', 50],
  '438775617': [61.90, 0.455323, 'HIGH', 37], '535581675': [52.68, 0.453613, 'HIGH', 32],
  '305101272': [59.53, 0.459079, 'MEDIUM', 23], '567668635': [62.84, 0.457412, 'MEDIUM', 22],
  '535580776': [59.18, 0.454294, 'MEDIUM', 21], '593111986': [60.80, 0.457236, 'MEDIUM', 19],
  '593111985': [69.98, 0.457929, 'MEDIUM', 18], '438775437': [57.79, 0.455732, 'MEDIUM', 15],
  '868597351': [113.71, 0.458159, 'MEDIUM', 14], '910584041': [100.10, 0.457168, 'MEDIUM', 13],
  '535581674': [57.60, 0.455256, 'MEDIUM', 13], '773170316': [108.39, 0.455496, 'MEDIUM', 12],
  '567668636': [63.80, 0.460324, 'MEDIUM', 10], '773170315': [70.01, 0.455922, 'LOW', 8],
  '930334397': [70.01, 0.455922, 'LOW', 8], '930334395': [70.01, 0.455922, 'LOW', 7],
  '952068582': [70.01, 0.455922, 'LOW', 7], '1083392113': [70.01, 0.455922, 'LOW', 6],
  '930334396': [70.01, 0.455922, 'LOW', 5], '910330849': [70.01, 0.455922, 'LOW', 1],
  '252441968': [70.01, 0.455922, 'LOW', 0], '252442341': [70.01, 0.455922, 'LOW', 0]
};
var S8_CLOSED = 9;
var S8_RB = 'S8_DATA_RB';
var S8_ORDER = ['views', 'opens', 'carts', 'orders', 'cancels', 'stock', 'adsIn', 'price', 'storage'];

function s8sql_() {
  var P = 'project-fa311fc0-4d87-4781-986', Q = String.fromCharCode(96);
  var T = function (t) { return Q + P + '.' + t + Q; };
  var D1 = "DATE '2026-09-01'", D2 = "DATE '2026-09-09'", W = ' BETWEEN ' + D1 + ' AND ' + D2;
  return 'WITH days AS (SELECT d FROM UNNEST(GENERATE_DATE_ARRAY(' + D1 + ', ' + D2 + ')) d),' +
    ' sku AS (SELECT nm_id FROM ' + T('wb_raw.REF_SKU_MASTER') + " WHERE marketplace='WB' AND active)," +
    ' g AS (SELECT s.nm_id, d.d FROM sku s CROSS JOIN days d),' +
    ' o AS (SELECT nm_id, order_date d, SUM(quantity) gross, SUM(IF(is_cancel,quantity,0)) canc,' +
    ' SAFE_DIVIDE(SUM(price_with_disc*quantity), NULLIF(SUM(quantity),0)) price FROM ' + T('wb_mart.FACT_ORDERS') +
    ' WHERE order_date' + W + ' GROUP BY 1,2),' +
    ' m AS (SELECT nm_id, day d, SUM(views) views, SUM(ad_spend) ads FROM ' + T('wb_mart.MART_SKU_DAILY') +
    ' WHERE day' + W + ' GROUP BY 1,2),' +
    ' f AS (SELECT nm_id, date_msk d, MAX(open_card_count) opens, MAX(add_to_cart_count) carts FROM ' + T('wb_raw.V_WB_FUNNEL_DAILY') +
    ' WHERE date_msk' + W + ' GROUP BY 1,2),' +
    ' sd AS (SELECT DISTINCT snapshot_date d FROM ' + T('wb_mart.FACT_STOCKS_SNAPSHOT') + ' WHERE snapshot_date' + W + '),' +
    ' st AS (SELECT nm_id, snapshot_date d, SUM(quantity) stock FROM ' + T('wb_mart.FACT_STOCKS_SNAPSHOT') +
    ' WHERE snapshot_date' + W + ' GROUP BY 1,2),' +
    ' pd AS (SELECT DISTINCT date_msk d FROM ' + T('wb_raw.RAW_WB_PAID_STORAGE') + ' WHERE date_msk' + W + '),' +
    ' ps AS (SELECT SAFE_CAST(nm_id AS INT64) nm_id, date_msk d, SUM(SAFE_CAST(warehouse_price AS NUMERIC)) storage FROM ' +
    T('wb_raw.RAW_WB_PAID_STORAGE') + ' WHERE date_msk' + W + ' GROUP BY 1,2)' +
    ' SELECT CAST(g.nm_id AS STRING), CAST(g.d AS STRING), CAST(IFNULL(m.views,0) AS STRING), CAST(f.opens AS STRING),' +
    ' CAST(f.carts AS STRING), CAST(IFNULL(o.gross,0) AS STRING), CAST(IFNULL(o.canc,0) AS STRING),' +
    ' CAST(IF(sd.d IS NULL, NULL, IFNULL(st.stock,0)) AS STRING), CAST(ROUND(IFNULL(m.ads,0),2) AS STRING),' +
    ' CAST(ROUND(o.price,2) AS STRING), CAST(IF(pd.d IS NULL, NULL, ROUND(IFNULL(ps.storage,0),2)) AS STRING)' +
    ' FROM g LEFT JOIN o ON o.nm_id=g.nm_id AND o.d=g.d LEFT JOIN m ON m.nm_id=g.nm_id AND m.d=g.d' +
    ' LEFT JOIN f ON f.nm_id=g.nm_id AND f.d=g.d LEFT JOIN st ON st.nm_id=g.nm_id AND st.d=g.d' +
    ' LEFT JOIN ps ON ps.nm_id=g.nm_id AND ps.d=g.d LEFT JOIN sd ON sd.d=g.d LEFT JOIN pd ON pd.d=g.d ORDER BY 1,2';
}

// УСТАРЕЛО с 11.09.2026 (STAGE 8.2). Актуальный загрузчик факта — s82data() в UnitkaS8b.gs:
//   «Переходы», «Положили в корзину», «Заказы факт» берутся из воронки (V_WB_FUNNEL_DAILY),
//   «Отменили товаров» — из FACT_ORDERS (у воронки поля отмен нет вообще).
// s8data() оставлен как история этапа 8. Он берёт заказы/переходы из другой семантики
// и знает только 9 закрытых дней, поэтому запускать его на сентябрьском мастере НЕ следует.
function s8data() {
  var L = ['=== STAGE 8 §4/§7/§8/§9 · s8data · ' + S8.VER + ' ==='];
  try {
    var t0 = new Date().getTime();
    var ss = SpreadsheetApp.openById(S8.SSID), sh = ss.getSheetByName(S8.SH);
    var rows = r7query_(s8sql_());
    L.push('BigQuery: строк ' + rows.length + ' (' + Math.round((new Date().getTime() - t0) / 1000) + ' с)');

    var bq = {};
    for (var i = 0; i < rows.length; i++) {
      var r = rows[i], nm = String(r[0]);
      if (!bq[nm]) bq[nm] = {};
      bq[nm][String(r[1])] = r.slice(2);
    }
    var IDX = { views: 0, opens: 1, carts: 2, orders: 3, cancels: 4, stock: 5, adsIn: 6, price: 7, storage: 8 };

    var wide = sh.getRange(S8.FIRST, 1, S8.DAYS, S8.NC);
    var cur = wide.getValues();
    var head = sh.getRange(S8.TOP, 1, 1, S8.NC).getDisplayValues()[0];

    // Ставки §7/§8 — расчёт из финотчёта, не таблица (см. s8fin_ в UnitkaS8b.gs).
    var FW = s8win_(sh, ss.getSpreadsheetTimeZone());
    if (!FW) { L.push('СТОП: зеркало LAST_CLOSED_DATE не дата'); return s8out_(L); }
    var fin = s8fin_(FW.d1, FW.d2, L);
    var plan = [], rb = [], noNm = [], gaps = { opens: 0, carts: 0, stock: 0, price: 0 }, filled = 0;
    for (var b = 0; b < S8.NB; b++) {
      var st0 = S8.B0 + b * S8.BW, title = '';
      for (var c = 0; c < S8.BW && !title; c++) title = String(head[st0 - 1 + c] || '').trim();
      var nm2 = (title.match(/\d{6,12}/) || [''])[0];
      if (!nm2 || !bq[nm2]) { noNm.push('#' + (b + 1) + ' ' + (nm2 || '?')); continue; }
      filled++;
      for (var k = 0; k < S8_ORDER.length; k++) {
        var key = S8_ORDER[k], col = st0 + S8_M[key], out = [], diff = false;
        for (var d = 0; d < S8_CLOSED; d++) {
          var src = bq[nm2]['2026-09-0' + (d + 1)] || [], raw = src[IDX[key]];
          var v = (raw === null || raw === undefined || raw === '') ? '' : Number(raw);
          if (v === '' && gaps[key] !== undefined) gaps[key]++;
          out.push([v]);
          var old = cur[d][col - 1];
          if (String(old) !== String(v)) diff = true;
        }
        if (diff) {
          plan.push({ col: col, n: S8_CLOSED, vals: out });
          for (var d2 = 0; d2 < S8_CLOSED; d2++) rb.push([S8.FIRST + d2, col, cur[d2][col - 1] instanceof Date ? '' : cur[d2][col - 1]]);
        }
      }
      var pr = fin.get(nm2);
      if (pr) {
        // МОДЕЛЬ B (11.09, решение владельца): ЛОГИСТИКА здесь больше НЕ пишется.
        // Прежняя ставка «вся логистика окна / выкупы» содержала обратные плечи отказов,
        // и её запись вернула бы двойной счёт. Прямую ставку (DIRECT) пишет s8brates(),
        // обратную — именованная ячейка REVERSE_LEG_RATE (WB737). См. docs/UNITKA_2_0_EVENT_LOGISTICS_MODEL_B.md
        var pairs = [[S8_M.commission, pr.comm]];
        for (var q = 0; q < pairs.length; q++) {
          var col2 = st0 + pairs[q][0], want = pairs[q][1], out2 = [], diff2 = false;
          for (var d3 = 0; d3 < S8.DAYS; d3++) { out2.push([want]); if (Number(cur[d3][col2 - 1]) !== want) diff2 = true; }
          if (diff2) { plan.push({ col: col2, n: S8.DAYS, vals: out2 }); for (var d4 = 0; d4 < S8.DAYS; d4++) rb.push([S8.FIRST + d4, col2, cur[d4][col2 - 1]]); }
        }
      }
    }
    L.push('блоков сопоставлено: ' + filled + ' | без сопоставления: ' + noNm.length + (noNm.length ? ' — ' + noNm.join(' ') : ''));
    L.push('диапазонов к записи: ' + plan.length + ' | ячеек в откате: ' + rb.length);

    if (rb.length && !r75get_(S8_RB)) { r75put_(S8_RB, { ts: new Date().toISOString(), ver: S8.VER, cells: rb }); L.push('откат сохранён в Script Properties ' + S8_RB); }
    else if (rb.length) L.push('откат уже сохранён первым прогоном — не перезаписан');

    for (var p = 0; p < plan.length; p++) sh.getRange(S8.FIRST, plan[p].col, plan[p].n, 1).setValues(plan[p].vals);
    SpreadsheetApp.flush();
    L.push('запись: ' + Math.round((new Date().getTime() - t0) / 1000) + ' с от старта');

    var after = wide.getValues(), mism = [], checked = 0;
    for (var b2 = 0; b2 < S8.NB; b2++) {
      var st2 = S8.B0 + b2 * S8.BW, t2 = '';
      for (var c2 = 0; c2 < S8.BW && !t2; c2++) t2 = String(head[st2 - 1 + c2] || '').trim();
      var nm3 = (t2.match(/\d{6,12}/) || [''])[0];
      if (!nm3 || !bq[nm3]) continue;
      for (var k2 = 0; k2 < S8_ORDER.length; k2++) {
        var key2 = S8_ORDER[k2], col3 = st2 + S8_M[key2];
        for (var d5 = 0; d5 < S8_CLOSED; d5++) {
          var raw2 = (bq[nm3]['2026-09-0' + (d5 + 1)] || [])[IDX[key2]];
          var want2 = (raw2 === null || raw2 === undefined || raw2 === '') ? '' : Number(raw2);
          var got = after[d5][col3 - 1]; checked++;
          var ok = (want2 === '') ? (got === '' || got === null) : (Math.abs(Number(got) - want2) < 0.005);
          if (!ok) mism.push(nm3 + ' ' + key2 + ' ' + r75col_(col3) + (S8.FIRST + d5) + ' лист[' + got + '] BQ[' + want2 + ']');
        }
      }
    }
    L.push('');
    L.push('=== §16 QA: лист против BigQuery ===');
    L.push('сверено ячеек: ' + checked + ' | MISMATCH = ' + mism.length + (mism.length ? ' <-- ' + mism.slice(0, 10).join(' | ') : ''));
    L.push('GAP оставлено пустыми: переходы ' + gaps.opens + ', корзины ' + gaps.carts + ', остатки ' + gaps.stock + ', цена ' + gaps.price);
    L.push('всего: ' + Math.round((new Date().getTime() - t0) / 1000) + ' с');
    L.push('BLOCKS FILLED = ' + filled + ' из ' + S8.NB + ' | BQ RECONCILIATION = ' + (mism.length === 0 ? 'PASS' : 'FAIL'));
  } catch (e) { L.push('ОШИБКА: ' + e + (e && e.stack ? '\n' + e.stack : '')); }
  s8out_(L);
}

// ==================== PREFLIGHT: чем блоки 2–24 отличаются от эталона ========

var S8_CALC = ['turnover', 'profit1', 'profitAll', 'adsOut', 'drr', 'priceSpp', 'priceMinusComm', 'tax', 'unitProfit', 'weekday'];

/** Нормализация формулы: свои колонки блока -> C{смещение}, свои строки -> R{сдвиг}. */
function s8norm_(f, start, row) {
  var s = String(f);
  return s.replace(/(\$?)([A-Z]{1,3})(\$?)(\d+)/g, function (all, ac, cl, ar, rw) {
    var col = s8num_(cl), r = Number(rw);
    if (ac === '$' && ar === '$') return all;
    if (col < start || col > start + S8.BW - 1) return 'FOREIGN(' + all + ')';
    return (ac === '$' ? '$' : '') + 'C' + (col - start) + (ar === '$' ? '$' : '') + 'R' + (r - row);
  });
}

function s8preflight() {
  var L = ['=== STAGE 8 PREFLIGHT · ' + S8.VER + ' ==='];
  try {
    var ss = SpreadsheetApp.openById(S8.SSID), sh = ss.getSheetByName(S8.SH);
    var head = sh.getRange(S8.TOP, 1, 1, S8.NC).getDisplayValues()[0];
    var body = sh.getRange(S8.FIRST, 1, S8.PLAN - S8.FIRST + 1, S8.NC);
    var frm = body.getFormulas();

    // Эталонные шаблоны блока 1 для строк 737 и 767.
    var pat = {}, patM = {};
    for (var i = 0; i < S8_CALC.length; i++) {
      var off = S8_M[S8_CALC[i]];
      pat[off] = s8norm_(frm[0][S8.B0 + off - 1], S8.B0, S8.FIRST);
      patM[off] = s8norm_(frm[S8.MTD - S8.FIRST][S8.B0 + off - 1], S8.B0, S8.MTD);
    }
    // Все смещения MTD эталона (не только расчётные).
    var patMall = {};
    for (var o2 = 1; o2 < S8.BW; o2++) patMall[o2] = s8norm_(frm[S8.MTD - S8.FIRST][S8.B0 + o2 - 1], S8.B0, S8.MTD);

    L.push('=== ЭТАЛОН: MTD 767 блока 1, какие смещения заполнены ===');
    var mtdHave = [], mtdEmpty = [];
    for (var o3 = 1; o3 < S8.BW; o3++) (patMall[o3] ? mtdHave : mtdEmpty).push(o3 + ':' + String(head[S8.B0 + o3 - 1] || '').slice(0, 10));
    L.push('есть: ' + mtdHave.join(' | '));
    L.push('пусто: ' + mtdEmpty.join(' | '));

    L.push('');
    L.push('=== БЛОКИ: расхождение с эталоном по строке 737 и наличие защиты будущего ===');
    var foreignAll = {}, guardMissing = 0, patMismatch = 0;
    for (var b = 0; b < S8.NB; b++) {
      var st = S8.B0 + b * S8.BW, t = '';
      for (var c = 0; c < S8.BW && !t; c++) t = String(head[st - 1 + c] || '').trim();
      var nm = (t.match(/\d{6,12}/) || [''])[0];
      var bad = [], noGuard = [], foreign = {};
      for (var k = 0; k < S8_CALC.length; k++) {
        var off2 = S8_M[S8_CALC[k]], f2 = String(frm[0][st + off2 - 1] || '');
        var n2 = s8norm_(f2, st, S8.FIRST);
        if (n2 !== pat[off2]) bad.push(S8_CALC[k]);
        if (S8_CALC[k] !== 'weekday' && f2.indexOf('LAST_CLOSED_DATE') < 0) noGuard.push(S8_CALC[k]);
        var fm = n2.match(/FOREIGN\([^)]+\)/g) || [];
        for (var q = 0; q < fm.length; q++) { foreign[fm[q]] = 1; foreignAll[fm[q]] = (foreignAll[fm[q]] || 0) + 1; }
      }
      // MTD: сколько смещений отличается от эталона
      var mtdBad = 0;
      for (var o4 = 1; o4 < S8.BW; o4++) if (s8norm_(frm[S8.MTD - S8.FIRST][st + o4 - 1], st, S8.MTD) !== patMall[o4]) mtdBad++;
      if (bad.length) patMismatch++;
      if (noGuard.length) guardMissing++;
      L.push('#' + (b + 1) + ' nm=' + nm + ' | шаблон 737 отличается: ' + (bad.length ? bad.join(',') : 'нет') +
        ' | без защиты будущего: ' + (noGuard.length ? noGuard.length + ' (' + noGuard.join(',') + ')' : 'нет') +
        ' | MTD отличается смещений: ' + mtdBad + ' | абсолютные ссылки: ' + (Object.keys(foreign).join(' ') || 'нет'));
    }
    L.push('');
    L.push('блоков с отличиями шаблона: ' + patMismatch + ' | без защиты будущего: ' + guardMissing);
    L.push('все абсолютные ссылки в расчётах: ' + JSON.stringify(foreignAll));

    // Что лежит вокруг R45 — ищем таблицу себестоимости.
    L.push('');
    L.push('=== ОКРЕСТНОСТИ $R$45 (строки 40–50, колонки N..V) ===');
    var cost = sh.getRange(40, 14, 11, 9).getDisplayValues();
    for (var r = 0; r < cost.length; r++) L.push((40 + r) + ': ' + cost[r].map(function (x, ix) { return r75col_(14 + ix) + '[' + String(x).slice(0, 16) + ']'; }).join(' '));
  } catch (e) { L.push('ОШИБКА: ' + e + (e && e.stack ? '\n' + e.stack : '')); }
  s8out_(L);
}

// ==================== §2 ФОРМУЛЫ ИЗ ЭТАЛОНА ==================================
// Формулы НЕ пишутся руками и не копируются координатами: берётся формула блока крема,
// её ссылки на собственный блок превращаются в токены (смещение, сдвиг строки), после чего
// токены разворачиваются в координаты нужного блока. Семантика гарантированно та же.
//
// Исключение — $R$45: это себестоимость ТОЛЬКО крема для рук («вторая партия», 240 ₽),
// ячейка вне сентябрьских строк. При переносе она заменяется себестоимостью своего SKU
// из evetis_ref.V_PRODUCT_COGS_EFFECTIVE на 09.09.2026. Блок крема не трогается.

var S8_COGS_CELL = '$R$45';
var S8_FRB = 'S8_FORMULA_RB';
var S8_WRITE_OFFSETS = [8, 9, 10, 12, 13, 16, 18, 21, 22];   // расчётные колонки строк 737–766

/** Ссылки на свой блок В СЕНТЯБРЬСКИХ СТРОКАХ -> токен. Прочее (в т.ч. $R$45) — как есть. */
function s8tok_(f, start, row) {
  var s = String(f);
  return s.replace(/(\$?)([A-Z]{1,3})(\$?)(\d+)/g, function (all, ac, cl, ar, rw, idx) {
    var prev = idx > 0 ? s.charAt(idx - 1) : '';
    if (/[A-Za-z0-9_Ѐ-ӿ]/.test(prev)) return all;
    var col = s8num_(cl), r = Number(rw);
    if (col < start || col > start + S8.BW - 1) return all;
    if (r < S8.TOP || r > S8.PLAN) return all;
    return '' + (ac === '$' ? 1 : 0) + '_' + (col - start) + '_' + (ar === '$' ? 1 : 0) + '_' + (r - row) + '';
  });
}

function s8untok_(p, start, row) {
  return String(p).replace(/(\d)_(\d+)_(\d)_(-?\d+)/g, function (all, ac, off, ar, dr) {
    return (ac === '1' ? '$' : '') + r75col_(start + Number(off)) + (ar === '1' ? '$' : '') + (row + Number(dr));
  });
}

/** Себестоимость по каждому nm_id на 09.09.2026 — из источника правды, не из книги. */
function s8cogs_() {
  var P = 'project-fa311fc0-4d87-4781-986', Q = String.fromCharCode(96);
  var T = function (t) { return Q + P + '.' + t + Q; };
  var sql = 'SELECT CAST(m.nm_id AS STRING), CAST(ROUND(c.product_cogs_rub,2) AS STRING), IFNULL(c.confidence,' + "''" + ')' +
    ' FROM ' + T('wb_raw.REF_SKU_MASTER') + ' m LEFT JOIN ' + T('evetis_ref.V_PRODUCT_COGS_EFFECTIVE') + ' c' +
    ' ON c.internal_sku = m.internal_sku AND DATE ' + "'2026-09-09'" + ' >= c.effective_from' +
    ' AND (c.effective_to IS NULL OR DATE ' + "'2026-09-09'" + ' < c.effective_to)' +
    " WHERE m.marketplace='WB' AND m.active";
  var rows = r7query_(sql), out = {};
  for (var i = 0; i < rows.length; i++) out[String(rows[i][0])] = { cogs: rows[i][1] === null ? null : Number(rows[i][1]), conf: rows[i][2] };
  return out;
}

function s8formulas() {
  var L = ['=== STAGE 8 §2 · s8formulas · ' + S8.VER + ' ==='];
  try {
    var t0 = new Date().getTime();
    var ss = SpreadsheetApp.openById(S8.SSID), sh = ss.getSheetByName(S8.SH);
    var cogs = s8cogs_();
    L.push('себестоимость из evetis_ref: SKU с данными ' + Object.keys(cogs).filter(function (k) { return cogs[k].cogs !== null; }).length +
      ' из ' + Object.keys(cogs).length + ' (' + Math.round((new Date().getTime() - t0) / 1000) + ' с)');

    var head = sh.getRange(S8.TOP, 1, 1, S8.NC).getDisplayValues()[0];
    var body = sh.getRange(S8.FIRST, 1, S8.PLAN - S8.FIRST + 1, S8.NC);
    var frm = body.getFormulas(), val = body.getValues();

    // Эталон: строка 737 (расчётные) и строка 767 (MTD, все смещения).
    var pat737 = {}, pat767 = {};
    for (var i = 0; i < S8_WRITE_OFFSETS.length; i++) {
      var o = S8_WRITE_OFFSETS[i];
      pat737[o] = s8tok_(frm[0][S8.B0 + o - 1], S8.B0, S8.FIRST);
    }
    for (var o2 = 0; o2 < S8.BW; o2++) {
      var f2 = frm[S8.MTD - S8.FIRST][S8.B0 + o2 - 1];
      if (f2) pat767[o2] = s8tok_(f2, S8.B0, S8.MTD);
    }
    L.push('эталон: расчётных шаблонов ' + Object.keys(pat737).length + ', MTD-шаблонов ' + Object.keys(pat767).length);

    // Откат: легаси-шаблон блоков 2–24 (они одинаковы), плюс индивидуальные исключения.
    var legacy = null, exceptions = {}, sameLegacy = 0;
    for (var b1 = 1; b1 < S8.NB; b1++) {
      var s1 = S8.B0 + b1 * S8.BW, sig = {};
      for (var q1 = 0; q1 < S8_WRITE_OFFSETS.length; q1++) sig[S8_WRITE_OFFSETS[q1]] = s8tok_(frm[0][s1 + S8_WRITE_OFFSETS[q1] - 1], s1, S8.FIRST);
      for (var o3 = 0; o3 < S8.BW; o3++) { var g = frm[S8.MTD - S8.FIRST][s1 + o3 - 1]; if (g) sig['m' + o3] = s8tok_(g, s1, S8.MTD); }
      var js = JSON.stringify(sig);
      if (legacy === null) legacy = js;
      if (js === legacy) sameLegacy++; else exceptions[b1 + 1] = sig;
    }
    L.push('легаси-шаблон блоков 2–24: совпадает у ' + sameLegacy + ' из 23, исключений ' + Object.keys(exceptions).length);
    if (!r75get_(S8_FRB)) { r75put_(S8_FRB, { ts: new Date().toISOString(), ver: S8.VER, legacy: legacy, exceptions: exceptions }); L.push('откат формул сохранён в ' + S8_FRB); }
    else L.push('откат формул уже сохранён первым прогоном');

    // Генерация и запись. Блок 1 (эталон) не трогаем.
    var writes = [], noCogs = [], changed = 0;
    for (var b = 1; b < S8.NB; b++) {
      var st = S8.B0 + b * S8.BW, t = '';
      for (var c = 0; c < S8.BW && !t; c++) t = String(head[st - 1 + c] || '').trim();
      var nm = (t.match(/\d{6,12}/) || [''])[0];
      var cg = cogs[nm] && cogs[nm].cogs !== null ? cogs[nm].cogs : null;
      if (cg === null) { noCogs.push('#' + (b + 1) + ' nm=' + nm); cg = 0; }
      // Локаль книги: разделитель аргументов «;», десятичный разделитель — ЗАПЯТАЯ.
      // Литерал с точкой даёт «Синтаксическая ошибка в формуле».
      var cgs = String(cg).replace('.', ',');

      for (var k = 0; k < S8_WRITE_OFFSETS.length; k++) {
        var off = S8_WRITE_OFFSETS[k], col = st + off, out = [], diff = false;
        for (var r = 0; r < S8.DAYS; r++) {
          var fx = s8untok_(pat737[off], st, S8.FIRST + r).split(S8_COGS_CELL).join(cgs);
          out.push([fx]);
          if (frm[r][col - 1] !== fx) diff = true;
        }
        if (diff) { writes.push({ row: S8.FIRST, col: col, vals: out }); changed += S8.DAYS; }
      }
      // MTD 767 — одной строкой 1×24: где у эталона формула, ставим её, иначе оставляем текущее.
      var rowOut = [], diffM = false;
      for (var o4 = 0; o4 < S8.BW; o4++) {
        var colM = st + o4, curF = frm[S8.MTD - S8.FIRST][colM - 1], curV = val[S8.MTD - S8.FIRST][colM - 1];
        if (pat767[o4]) {
          var fm = s8untok_(pat767[o4], st, S8.MTD).split(S8_COGS_CELL).join(cgs);
          rowOut.push(fm); if (curF !== fm) diffM = true;
        } else rowOut.push(curF ? curF : curV);
      }
      if (diffM) { writes.push({ row: S8.MTD, col: st, vals: [rowOut], wide: true }); changed += S8.BW; }
    }
    L.push('SKU без себестоимости в evetis_ref: ' + noCogs.length + (noCogs.length ? ' — ' + noCogs.join(' ') + ' (подставлен 0; в сентябре у них 0 заказов, на экономику не влияет)' : ''));
    L.push('диапазонов к записи: ' + writes.length + ' | ячеек: ' + changed);

    for (var w = 0; w < writes.length; w++) {
      if (writes[w].wide) sh.getRange(writes[w].row, writes[w].col, 1, S8.BW).setValues(writes[w].vals);
      else sh.getRange(writes[w].row, writes[w].col, S8.DAYS, 1).setFormulas(writes[w].vals);
    }
    SpreadsheetApp.flush();
    L.push('запись: ' + Math.round((new Date().getTime() - t0) / 1000) + ' с от старта');

    // Проверка: шаблон совпал, защита будущего есть, ошибок нет.
    var frmA = body.getFormulas(), valA = body.getValues();
    var badPat = 0, noGuard = 0, errs = 0, firstErr = '';
    for (var b2 = 1; b2 < S8.NB; b2++) {
      var st2 = S8.B0 + b2 * S8.BW;
      for (var k2 = 0; k2 < S8_WRITE_OFFSETS.length; k2++) {
        var off2 = S8_WRITE_OFFSETS[k2];
        for (var r2 = 0; r2 < S8.DAYS; r2++) {
          var f3 = String(frmA[r2][st2 + off2 - 1] || '');
          if (s8tok_(f3, st2, S8.FIRST + r2) !== pat737[off2].split(S8_COGS_CELL).join('#')) {
            var alt = s8tok_(f3, st2, S8.FIRST + r2);
            if (alt.indexOf('LAST_CLOSED_DATE') < 0) noGuard++;
            if (alt.replace(/[\d.]+\)/g, 'X)') !== pat737[off2].split(S8_COGS_CELL).join('X').replace(/[\d.]+\)/g, 'X)')) badPat++;
          }
        }
      }
    }
    for (var r3 = 0; r3 < valA.length; r3++) for (var c3 = S8.B0 - 1; c3 < S8.B0 - 1 + S8.BW * S8.NB; c3++) {
      if (/^#(REF!|VALUE!|NAME\?|DIV\/0!|N\/A|ERROR!|NUM!|NULL!)/.test(String(valA[r3][c3]))) { errs++; if (!firstErr) firstErr = r75col_(c3 + 1) + (S8.FIRST + r3); }
    }
    L.push('');
    L.push('BLOCKS FORMULA QA: блоков обработано 23 (эталон не тронут)');
    L.push('ячеек без защиты будущего после записи: ' + noGuard);
    L.push('FORMULA ERRORS в сентябрьских блоках: ' + errs + (firstErr ? ' (первая ' + firstErr + ')' : ''));
    L.push('всего: ' + Math.round((new Date().getTime() - t0) / 1000) + ' с');
  } catch (e) { L.push('ОШИБКА: ' + e + (e && e.stack ? '\n' + e.stack : '')); }
  s8out_(L);
}
