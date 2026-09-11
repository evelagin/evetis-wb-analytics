// UNITKA 2.0 STAGE 8 · часть 3 — визуальная стандартизация всех блоков по эталону.
// agent claude-opus-5, v8.2.0. Эталон — блок 252442517 «Крем для рук» (M..AJ).
//
// Формат клонируется НЕ поэлементно, а запросом copyPaste PASTE_FORMAT через Sheets API v4.
// Это переносит ровно всё: границы, шрифты, размеры, начертание, выравнивание, перенос,
// форматы чисел, статичный фон, отступы — и не оставляет мест, где «забыли свойство».
// Поэлементный перенос всегда что-то упускает; копия формата — нет.
//
// Размеры (ширины колонок, высоты строк) читаются и пишутся тоже через Sheets API:
// 576 вызовов getColumnWidth занимают минуты и упираются в лимит 6 минут, один
// batch-запрос — доли секунды.

function s8c() { s8cvqa(); }

var S8V = {
  VER: 'unitka2.0/v8.2.0',
  RB_W: 'S8_WIDTH_RB', RB_ID: 'S8_IDENT_RB', RB_LEG: 'S8_LEGACY_RB'
};

/** Границы блока в индексах Sheets API (0-based, конец исключительный). */
function s8grid_(gid, col0, ncol) {
  return { sheetId: gid, startRowIndex: S8.TOP - 1, endRowIndex: S8.PLAN,
           startColumnIndex: col0 - 1, endColumnIndex: col0 - 1 + ncol };
}

/** Размеры листа одним запросом: ширины колонок и высоты строк 735..768. */
function s8dims_(gid) {
  var r = Sheets.Spreadsheets.get(S8.SSID, {
    ranges: [S8.SH + '!A' + S8.TOP + ':' + r75col_(S8.NC) + S8.PLAN],
    fields: 'sheets(data(columnMetadata.pixelSize,rowMetadata.pixelSize))',
    includeGridData: true
  });
  var d = r.sheets[0].data[0];
  var w = [], h = [];
  for (var i = 0; i < d.columnMetadata.length; i++) w.push(d.columnMetadata[i].pixelSize);
  for (var j = 0; j < d.rowMetadata.length; j++) h.push(d.rowMetadata[j].pixelSize);
  return { w: w, h: h };
}

/** Устойчивая сериализация формата: ключи отсортированы, иначе сравнение врёт. */
function s8ser_(o) {
  if (o === null || o === undefined) return 'null';
  if (typeof o !== 'object') return JSON.stringify(o);
  if (Object.prototype.toString.call(o) === '[object Array]') {
    var a = [];
    for (var i = 0; i < o.length; i++) a.push(s8ser_(o[i]));
    return '[' + a.join(',') + ']';
  }
  var ks = Object.keys(o).sort(), p = [];
  for (var k = 0; k < ks.length; k++) p.push(JSON.stringify(ks[k]) + ':' + s8ser_(o[ks[k]]));
  return '{' + p.join(',') + '}';
}

/** СТАТИЧЕСКИЙ формат ячеек 735..768. Именно userEnteredFormat, а не effectiveFormat:
 *  effectiveFormat включает результат условного форматирования, а он у каждого SKU
 *  свой по смыслу (у блоков разные значения оборачиваемости и доходности), и сравнение
 *  по нему показывало бы расхождения там, где их нет. Одинаковость самого УФ проверяется
 *  отдельно — по покрытию правил, см. s8ccf_. */
function s8fmtGet_(starts) {
  var ranges = [];
  for (var i = 0; i < starts.length; i++)
    ranges.push(S8.SH + '!' + r75col_(starts[i]) + S8.TOP + ':' + r75col_(starts[i] + S8.BW - 1) + S8.PLAN);
  var r = Sheets.Spreadsheets.get(S8.SSID, {
    ranges: ranges,
    fields: 'sheets(data(rowData(values(userEnteredFormat(backgroundColorStyle,borders,' +
      'horizontalAlignment,verticalAlignment,wrapStrategy,textFormat,numberFormat,padding,textRotation)))))',
    includeGridData: true
  });
  return r.sheets[0].data;
}

/** Ячейка -> строка формата; фон строки 735 исключаем (там идентификация товара). */
function s8cell_(data, row, col) {
  var rd = data.rowData && data.rowData[row] ? data.rowData[row] : null;
  var v = rd && rd.values && rd.values[col] ? rd.values[col].userEnteredFormat : null;
  if (!v) return '{}';
  if (row === 0) { var c = {}; for (var k in v) if (k !== 'backgroundColorStyle') c[k] = v[k]; v = c; }
  return s8ser_(v);
}

// ==================== ПРОБА (только чтение) =================================

function s8cprobe() {
  var L = ['=== STAGE 8 §V · s8cprobe · ' + S8V.VER + ' ==='];
  try {
    var t0 = new Date().getTime();
    var ss = SpreadsheetApp.openById(S8.SSID), sh = ss.getSheetByName(S8.SH);
    var gid = sh.getSheetId();
    var dm = s8dims_(gid);
    L.push('sheetId ' + gid + ' | размеры получены за ' + Math.round((new Date().getTime() - t0) / 1000) + ' с');

    var w = dm.w.slice(S8.B0 - 1, S8.B0 - 1 + S8.BW);
    L.push('ширины эталона M..AJ: ' + w.join(','));
    var hh = {};
    for (var j = 0; j < dm.h.length; j++) hh[dm.h[j]] = (hh[dm.h[j]] || 0) + 1;
    var hs = [];
    for (var k in hh) hs.push(k + 'px x' + hh[k]);
    L.push('высоты строк 735-768: ' + hs.join(', ') + ' — строки общие для всех блоков');

    var head = sh.getRange(S8.TOP, 1, 1, S8.NC).getDisplayValues()[0];
    var bl = s8blocks_(head), diffW = [], totW = 0;
    for (var b = 1; b < bl.length; b++) {
      var bad = 0;
      for (var c = 0; c < S8.BW; c++) if (dm.w[bl[b].st - 1 + c] !== w[c]) bad++;
      if (bad) { diffW.push('#' + bl[b].i + ':' + bad); totW += bad; }
    }
    L.push('блоков с другими ширинами: ' + diffW.length + ' из 23, колонок ' + totW + (diffW.length ? ' — ' + diffW.join(' ') : ''));

    var bg = sh.getRange(S8.TOP, 1, 2, S8.NC).getBackgrounds();
    L.push('');
    L.push('где живёт цвет товара (фон 735 | фон 736, первые 3 колонки блока):');
    for (var b2 = 0; b2 < bl.length; b2++) {
      var st = bl[b2].st, g = [];
      for (var c3 = 0; c3 < 3; c3++) g.push(bg[1][st - 1 + c3]);
      L.push('#' + bl[b2].i + ' ' + bl[b2].nm + ' | ' + bg[0][st - 1] + ' | ' + g.join(' '));
    }
    L.push('');
    L.push('время ' + Math.round((new Date().getTime() - t0) / 1000) + ' с');
  } catch (e) { L.push('ОШИБКА: ' + e + (e && e.stack ? '\n' + e.stack : '')); }
  s8out_(L);
}

/** Формулы моих консолидированных правил — чтобы отличать их от легаси. */
function s8mineF_() {
  var out = {};
  for (var s = 0; s < S8_SPEC.length; s++) {
    var sp = S8_SPEC[s];
    var bases = [];
    if (sp.abs) bases.push(sp.abs);
    else {
      if (sp.sum) bases.push(2 + sp.d);
      for (var b = 0; b < S8.NB; b++) bases.push(S8.B0 + b * S8.BW + sp.d);
    }
    for (var i = 0; i < bases.length; i++) {
      var C = (function (bb) { return function (k) { return r75col_(bb + k); }; })(bases[i]);
      out[sp.fn(C)] = 1;
    }
  }
  return out;
}

/** Сколько легаси-УФ задевает сентябрь внутри колонок блоков. */
function s8cflegacyscan() {
  var L = ['=== STAGE 8 §V · s8cflegacyscan · ' + S8V.VER + ' ==='];
  try {
    var t0 = new Date().getTime();
    var sh = SpreadsheetApp.openById(S8.SSID).getSheetByName(S8.SH);
    var head = sh.getRange(S8.TOP, 1, 1, S8.NC).getDisplayValues()[0];
    var bl = s8blocks_(head);
    var rules = sh.getConditionalFormatRules();
    L.push('правил всего: ' + rules.length + ' (чтение ' + Math.round((new Date().getTime() - t0) / 1000) + ' с)');

    var mine = s8mineF_();
    var legacy = 0, mineN = 0, whole = 0, perBlock = {}, ranges = 0;
    for (var i = 0; i < rules.length; i++) {
      var bc = rules[i].getBooleanCondition();
      var fx = bc ? String((bc.getCriteriaValues() || [])[0] || '') : '';
      var rg = rules[i].getRanges(), touch = false, allInside = true;
      for (var q = 0; q < rg.length; q++) {
        var r1 = rg[q].getRow(), r2 = r1 + rg[q].getNumRows() - 1;
        var c1 = rg[q].getColumn(), c2 = c1 + rg[q].getNumColumns() - 1;
        if (r2 < S8.TOP || r1 > S8.PLAN || c2 < 12 || c1 > S8.NC) { allInside = false; continue; }
        touch = true; ranges++;
        if (r1 < S8.TOP || r2 > S8.PLAN) allInside = false;
        for (var b = 0; b < bl.length; b++) {
          var s3 = bl[b].st;
          if (c2 >= s3 && c1 <= s3 + S8.BW - 1) perBlock[b + 1] = (perBlock[b + 1] || 0) + 1;
        }
      }
      if (!touch) continue;
      if (fx && mine[fx]) { mineN++; continue; }
      legacy++;
      if (allInside) whole++;
    }
    L.push('моих консолидированных правил, задевающих сентябрь: ' + mineN);
    L.push('ЛЕГАСИ-правил, задевающих сентябрь в колонках блоков: ' + legacy);
    L.push('   целиком внутри 735-768 (можно удалить): ' + whole);
    L.push('   пересекающих август (резать, не удалять): ' + (legacy - whole));
    L.push('диапазонов, задевающих сентябрь: ' + ranges);
    var pb = [];
    for (var kk in perBlock) pb.push('#' + kk + ':' + perBlock[kk]);
    L.push('по блокам: ' + pb.join(' '));
    L.push('время ' + Math.round((new Date().getTime() - t0) / 1000) + ' с');
  } catch (e) { L.push('ОШИБКА: ' + e + (e && e.stack ? '\n' + e.stack : '')); }
  s8out_(L);
}

// ==================== ШАГ 1: РАЗМЕРЫ ========================================

function s8cwidth() {
  var L = ['=== STAGE 8 §V.1 · s8cwidth · ' + S8V.VER + ' ==='];
  try {
    var t0 = new Date().getTime();
    var ss = SpreadsheetApp.openById(S8.SSID), sh = ss.getSheetByName(S8.SH);
    var gid = sh.getSheetId(), dm = s8dims_(gid);
    var head = sh.getRange(S8.TOP, 1, 1, S8.NC).getDisplayValues()[0];
    var bl = s8blocks_(head);
    var w = dm.w.slice(S8.B0 - 1, S8.B0 - 1 + S8.BW);

    if (!r75get_(S8V.RB_W)) {
      var snap = {};
      for (var b0 = 1; b0 < bl.length; b0++) {
        var a = [];
        for (var c0 = 0; c0 < S8.BW; c0++) a.push(dm.w[bl[b0].st - 1 + c0]);
        snap[bl[b0].st] = a;
      }
      r75put_(S8V.RB_W, { ts: new Date().toISOString(), ver: S8V.VER, cols: snap });
      L.push('откат ширин сохранён: ' + S8V.RB_W);
    } else L.push('откат ширин уже сохранён первым прогоном');

    var reqs = [], changed = 0;
    for (var b = 1; b < bl.length; b++) {
      var st = bl[b].st;
      for (var c = 0; c < S8.BW; c++) {
        if (dm.w[st - 1 + c] === w[c]) continue;
        reqs.push({ updateDimensionProperties: {
          range: { sheetId: gid, dimension: 'COLUMNS', startIndex: st - 1 + c, endIndex: st + c },
          properties: { pixelSize: w[c] }, fields: 'pixelSize' } });
        changed++;
      }
    }
    L.push('колонок к правке: ' + changed);
    if (reqs.length) Sheets.Spreadsheets.batchUpdate({ requests: reqs }, S8.SSID);

    var dm2 = s8dims_(gid), bad = 0;
    for (var b2 = 1; b2 < bl.length; b2++)
      for (var c2 = 0; c2 < S8.BW; c2++) if (dm2.w[bl[b2].st - 1 + c2] !== w[c2]) bad++;
    L.push('COLUMN WIDTHS = ' + (bad === 0 ? 'PASS 24/24' : 'FAIL, расхождений ' + bad) +
      ' | ' + Math.round((new Date().getTime() - t0) / 1000) + ' с');
    L.push('высоты строк 735-768 общие для всех блоков — приводить нечего');
  } catch (e) { L.push('ОШИБКА: ' + e + (e && e.stack ? '\n' + e.stack : '')); }
  s8out_(L);
}

// ==================== ШАГ 2: КЛОН ФОРМАТА ===================================
// Проба показала: строка 735 БЕЛАЯ во всех блоках, включая эталон, а индивидуальный
// цвет товара лежит ПЛОСКОЙ заливкой в строке 736 — ровно там, где у эталона живут
// смысловые группы AUTO / MANUAL / MODEL / FORMULA (#d9d9d9 дата, #ffe599 ручное,
// #b7e1cd авто). Совместить плоскую заливку и группы нельзя: это одна и та же строка.
// Владелец потребовал воспроизвести группы, поэтому переносим эталон целиком, а старые
// цвета блоков сохраняем в S8_IDENT_RB. Если идентификацию надо вернуть — s8cband()
// положит исторический цвет блока полосой на строку 735, не ломая стандарт строки 736.

function s8cfmt() {
  var L = ['=== STAGE 8 §V.2 · s8cfmt · ' + S8V.VER + ' ==='];
  try {
    var t0 = new Date().getTime();
    var ss = SpreadsheetApp.openById(S8.SSID), sh = ss.getSheetByName(S8.SH);
    var gid = sh.getSheetId();
    var head = sh.getRange(S8.TOP, 1, 1, S8.NC).getDisplayValues()[0];
    var bl = s8blocks_(head);

    var idRow = sh.getRange(S8.TOP, 1, 2, S8.NC);
    var idBg = idRow.getBackgrounds(), idFc = idRow.getFontColors();
    if (!r75get_(S8V.RB_ID)) {
      var snap = {};
      for (var b0 = 0; b0 < bl.length; b0++) {
        var st0 = bl[b0].st, a = [], f = [], a2 = [];
        for (var c0 = 0; c0 < S8.BW; c0++) {
          a.push(idBg[0][st0 - 1 + c0]); f.push(idFc[0][st0 - 1 + c0]); a2.push(idBg[1][st0 - 1 + c0]);
        }
        snap[st0] = { bg: a, fc: f, hdr: a2 };
      }
      r75put_(S8V.RB_ID, { ts: new Date().toISOString(), ver: S8V.VER, rows: snap });
      L.push('исторические цвета строк 735 и 736 сохранены: ' + S8V.RB_ID);
    } else L.push('исторические цвета уже сохранены первым прогоном');

    var src = s8grid_(gid, S8.B0, S8.BW), reqs = [];
    for (var b = 1; b < bl.length; b++)
      reqs.push({ copyPaste: { source: src, destination: s8grid_(gid, bl[b].st, S8.BW),
        pasteType: 'PASTE_FORMAT', pasteOrientation: 'NORMAL' } });
    Sheets.Spreadsheets.batchUpdate({ requests: reqs }, S8.SSID);
    L.push('copyPaste PASTE_FORMAT: блоков ' + reqs.length + ' | ' + Math.round((new Date().getTime() - t0) / 1000) + ' с');

    SpreadsheetApp.flush();
    L.push('строка 736 приведена к эталону — группы AUTO / MANUAL / MODEL / FORMULA');
    L.push('плоская заливка товара в строке 736 снята; исторические цвета в ' + S8V.RB_ID);
    L.push('вернуть идентификацию полосой на строку 735 — s8cband()');
    L.push('время ' + Math.round((new Date().getTime() - t0) / 1000) + ' с');
  } catch (e) { L.push('ОШИБКА: ' + e + (e && e.stack ? '\n' + e.stack : '')); }
  s8out_(L);
}

// ==================== ШАГ 3: СВОДКА A:K =====================================
// У сводки другой набор колонок, поэтому формат берётся поколоночно от той метрики
// блока, которую колонка агрегирует. Так сводка и блоки читаются одинаково.

var S8_SUMMAP = [[1, -1], [2, 0], [3, 1], [4, 2], [5, 3], [6, 4], [7, 5], [8, 6], [9, 10], [10, 11], [11, 13]];

function s8csum() {
  var L = ['=== STAGE 8 §V.3 · s8csum · ' + S8V.VER + ' ==='];
  try {
    var t0 = new Date().getTime();
    var ss = SpreadsheetApp.openById(S8.SSID), sh = ss.getSheetByName(S8.SH);
    var gid = sh.getSheetId(), reqs = [];

    for (var i = 0; i < S8_SUMMAP.length; i++) {
      var dst = S8_SUMMAP[i][0], srcCol = S8.B0 + S8_SUMMAP[i][1];
      reqs.push({ copyPaste: {
        source: { sheetId: gid, startRowIndex: S8.HDR - 1, endRowIndex: S8.PLAN,
                  startColumnIndex: srcCol - 1, endColumnIndex: srcCol },
        destination: { sheetId: gid, startRowIndex: S8.HDR - 1, endRowIndex: S8.PLAN,
                  startColumnIndex: dst - 1, endColumnIndex: dst },
        pasteType: 'PASTE_FORMAT', pasteOrientation: 'NORMAL' } });
    }
    Sheets.Spreadsheets.batchUpdate({ requests: reqs }, S8.SSID);
    L.push('формат сводки взят поколоночно от метрик эталона, колонок: ' + reqs.length);
    for (var j = 0; j < S8_SUMMAP.length; j++)
      L.push('   ' + r75col_(S8_SUMMAP[j][0]) + ' <- ' + r75col_(S8.B0 + S8_SUMMAP[j][1]) +
        ' (смещение ' + S8_SUMMAP[j][1] + ')');
    L.push('строка 735 не тронута: там объединение A735:K735 и заголовок месяца');

    SpreadsheetApp.flush();
    var d2 = sh.getRange(S8.HDR, 1, S8.PLAN - S8.HDR + 1, 11).getDisplayValues(), err = 0;
    for (var r = 0; r < d2.length; r++)
      for (var c = 0; c < 11; c++) if (S8_ERR.test(String(d2[r][c]))) err++;
    L.push('SUMMARY VISUAL QA = ' + (err === 0 ? 'PASS' : 'FAIL, ошибок ' + err) +
      ' | ' + Math.round((new Date().getTime() - t0) / 1000) + ' с');
  } catch (e) { L.push('ОШИБКА: ' + e + (e && e.stack ? '\n' + e.stack : '')); }
  s8out_(L);
}

// ==================== ШАГ 4: VISUAL QA ======================================

function s8chash_(s) {
  var h = 5381;
  for (var i = 0; i < s.length; i++) h = ((h * 33) ^ s.charCodeAt(i)) >>> 0;
  return h;
}

/** Покрытие условным форматированием: одинаков ли набор правил у каждого блока.
 *  Для каждого правила берём его сентябрьские диапазоны и переводим в смещения
 *  внутри блока. Блоки идентичны, если подписи совпадают. */
function s8ccf_(gid, bl) {
  var resp = Sheets.Spreadsheets.get(S8.SSID, { fields: 'sheets(properties.sheetId,conditionalFormats(ranges))' });
  var cf = null;
  for (var i = 0; i < resp.sheets.length; i++) if (resp.sheets[i].properties.sheetId === gid) cf = resp.sheets[i].conditionalFormats || [];
  var R1 = S8.TOP - 1, R2 = S8.PLAN, sig = [];
  for (var b = 0; b < bl.length; b++) sig.push([]);
  for (var k = 0; k < cf.length; k++) {
    var rgs = cf[k].ranges || [];
    for (var g = 0; g < rgs.length; g++) {
      var rg = rgs[g];
      var sr = rg.startRowIndex === undefined ? 0 : rg.startRowIndex;
      var er = rg.endRowIndex === undefined ? R2 : rg.endRowIndex;
      if (er <= R1 || sr >= R2) continue;
      var sc = rg.startColumnIndex, ec = rg.endColumnIndex;
      if (sc === undefined || ec === undefined) continue;
      for (var b2 = 0; b2 < bl.length; b2++) {
        var st = bl[b2].st - 1;
        if (ec <= st || sc >= st + S8.BW) continue;
        sig[b2].push(k + ':' + (Math.max(sc, st) - st) + ':' + (Math.min(ec, st + S8.BW) - st) + ':' + sr + ':' + er);
      }
    }
  }
  var out = [];
  for (var b3 = 0; b3 < bl.length; b3++) { sig[b3].sort(); out.push(sig[b3].join('|')); }
  return out;
}

function s8cvqa() {
  var L = ['=== STAGE 8 §V.4 · s8cvqa · ' + S8V.VER + ' ==='];
  try {
    var t0 = new Date().getTime();
    var ss = SpreadsheetApp.openById(S8.SSID), sh = ss.getSheetByName(S8.SH);
    var gid = sh.getSheetId(), dm = s8dims_(gid);
    var head = sh.getRange(S8.TOP, 1, 1, S8.NC).getDisplayValues()[0];
    var bl = s8blocks_(head);
    var w = dm.w.slice(S8.B0 - 1, S8.B0 - 1 + S8.BW);
    var NR = S8.PLAN - S8.TOP + 1;

    // Блоки читаем ПО ОДНОМУ и сразу сворачиваем в числовые отпечатки:
    // хранить effectiveFormat шести блоков целиком — это out of memory.
    var ref = [];
    var mas = s8fmtGet_([S8.B0])[0];
    for (var r = 0; r < NR; r++) {
      var row = [];
      for (var c = 0; c < S8.BW; c++) row.push(s8chash_(s8cell_(mas, r, c)));
      ref.push(row);
    }
    mas = null;

    var cfsig = s8ccf2_(gid, bl), cfbad = [];
    for (var s = 1; s < cfsig.length; s++) if (cfsig[s] !== cfsig[0]) cfbad.push('#' + bl[s].i);

    var pass = 1, worst = [];
    for (var b = 1; b < bl.length; b++) {
      var data = s8fmtGet_([bl[b].st])[0], bad = 0, first = '';
      for (var r2 = 0; r2 < NR; r2++)
        for (var c2 = 0; c2 < S8.BW; c2++) {
          if (s8chash_(s8cell_(data, r2, c2)) === ref[r2][c2]) continue;
          bad++;
          if (!first) first = r75col_(bl[b].st + c2) + (S8.TOP + r2);
        }
      data = null;
      var wbad = 0;
      for (var c3 = 0; c3 < S8.BW; c3++) if (dm.w[bl[b].st - 1 + c3] !== w[c3]) wbad++;
      var cfb = cfsig[b] !== cfsig[0] ? 1 : 0;
      if (bad === 0 && wbad === 0 && !cfb) pass++;
      else worst.push('#' + bl[b].i + ' ' + bl[b].nm + ' формат ' + bad + ' ширины ' + wbad +
        (cfb ? ' УФ РАСХОДИТСЯ' : '') + (first ? ' первое ' + first : ''));
    }

    L.push('сравнивались: фон, границы, выравнивание по горизонтали и вертикали, перенос,');
    L.push('шрифт (семейство, размер, начертание, цвет), формат числа, отступы, поворот,');
    L.push('ширины колонок. Фон строки 735 исключён — там идентификация товара.');
    L.push('ячеек на блок: ' + (NR * S8.BW) + ' (строки 735-768 x 24 колонки)');
    L.push('покрытие условным форматированием совпадает с эталоном: ' + (bl.length - 1 - cfbad.length) +
      ' из ' + (bl.length - 1) + (cfbad.length ? ' <-- ' + cfbad.join(' ') : ''));
    L.push('');
    for (var x = 0; x < worst.length; x++) L.push('  ' + worst[x]);
    L.push('');
    L.push('VISUAL STANDARD PASS = ' + pass + ' / ' + S8.NB);
    L.push('время ' + Math.round((new Date().getTime() - t0) / 1000) + ' с');
  } catch (e) { L.push('ОШИБКА: ' + e + (e && e.stack ? '\n' + e.stack : '')); }
  s8out_(L);
}

// ==================== ОТКАТЫ ================================================

function s8cwidthrollback() {
  var L = ['=== ОТКАТ ширин s8cwidth ==='];
  var rb = r75get_(S8V.RB_W);
  if (!rb) { L.push('снимка нет'); return s8out_(L); }
  var sh = SpreadsheetApp.openById(S8.SSID).getSheetByName(S8.SH), gid = sh.getSheetId(), reqs = [];
  for (var st in rb.cols) {
    var a = rb.cols[st];
    for (var c = 0; c < a.length; c++)
      reqs.push({ updateDimensionProperties: {
        range: { sheetId: gid, dimension: 'COLUMNS', startIndex: Number(st) - 1 + c, endIndex: Number(st) + c },
        properties: { pixelSize: a[c] }, fields: 'pixelSize' } });
  }
  Sheets.Spreadsheets.batchUpdate({ requests: reqs }, S8.SSID);
  L.push('восстановлено колонок: ' + reqs.length + ' (снимок ' + rb.ts + ')');
  s8out_(L);
}

/** Не откат, а опция: исторический цвет блока полосой на строку 735. */
function s8cband() {
  var L = ['=== ПОЛОСА ИДЕНТИФИКАЦИИ на строку 735 ==='];
  var rb = r75get_(S8V.RB_ID);
  if (!rb) { L.push('снимка нет — сначала s8cfmt'); return s8out_(L); }
  var sh = SpreadsheetApp.openById(S8.SSID).getSheetByName(S8.SH), n = 0;
  for (var st in rb.rows) {
    var h = rb.rows[st].hdr;
    if (!h) continue;
    var cnt = {}, best = '', bn = 0;
    for (var i = 0; i < h.length; i++) { cnt[h[i]] = (cnt[h[i]] || 0) + 1; if (cnt[h[i]] > bn) { bn = cnt[h[i]]; best = h[i]; } }
    sh.getRange(S8.TOP, Number(st), 1, S8.BW).setBackground(best);
    n++;
  }
  SpreadsheetApp.flush();
  L.push('полос поставлено: ' + n + ' (цвет — исторический доминирующий цвет строки 736 блока)');
  s8out_(L);
}

function s8cidentrollback() {
  var L = ['=== ВОЗВРАТ идентификации строки 735 ==='];
  var rb = r75get_(S8V.RB_ID);
  if (!rb) { L.push('снимка нет'); return s8out_(L); }
  var sh = SpreadsheetApp.openById(S8.SSID).getSheetByName(S8.SH), n = 0;
  for (var st in rb.rows) {
    sh.getRange(S8.TOP, Number(st), 1, S8.BW).setBackgrounds([rb.rows[st].bg]).setFontColors([rb.rows[st].fc]);
    n++;
  }
  SpreadsheetApp.flush();
  L.push('восстановлено блоков: ' + n + ' (снимок ' + rb.ts + ')');
  s8out_(L);
}


/** Что именно делают легаси-правила ВНУТРИ эталонного блока. Только чтение. */
function s8cmaster() {
  var L = ['=== STAGE 8 §V · s8cmaster · ' + S8V.VER + ' ==='];
  try {
    var t0 = new Date().getTime();
    var sh = SpreadsheetApp.openById(S8.SSID).getSheetByName(S8.SH);
    var rules = sh.getConditionalFormatRules();
    var mine = s8mineF_();
    var lo = S8.B0 - 1, hi = S8.B0 + S8.BW - 1;          // колонки 12..36
    var byOff = {}, n = 0;
    // Сначала дешёвая проверка диапазонов и только потом чтение условия:
    // getBooleanCondition на 4307 правилах — это минуты, на 40 — мгновение.
    for (var i = 0; i < rules.length; i++) {
      var r = rules[i];
      var rg = r.getRanges(), offs = {};
      for (var q = 0; q < rg.length; q++) {
        var r1 = rg[q].getRow(), r2 = r1 + rg[q].getNumRows() - 1;
        var c1 = rg[q].getColumn(), c2 = c1 + rg[q].getNumColumns() - 1;
        if (r2 < S8.TOP || r1 > S8.PLAN || c2 < lo || c1 > hi) continue;
        for (var c = Math.max(c1, lo); c <= Math.min(c2, hi); c++) offs[c - S8.B0] = 1;
      }
      var ks = Object.keys(offs);
      if (!ks.length) continue;
      var bc = r.getBooleanCondition(), gc = r.getGradientCondition();
      var fx = bc ? String((bc.getCriteriaValues() || [])[0] || '') : '';
      if (fx && mine[fx]) continue;
      n++;
      var desc;
      if (gc) {
        desc = 'ГРАДИЕНТ ' + gc.getMinColor() + ' / ' + gc.getMidColor() + ' / ' + gc.getMaxColor() +
          ' [' + gc.getMinType() + ' ' + gc.getMinValue() + ' | ' + gc.getMidType() + ' ' + gc.getMidValue() +
          ' | ' + gc.getMaxType() + ' ' + gc.getMaxValue() + ']';
      } else if (bc) {
        var cv = bc.getCriteriaValues() || [];
        desc = String(bc.getCriteriaType()) + ' ' + s8sp_(String(cv[0] || '')).substr(0, 60) +
          ' -> фон ' + (bc.getBackground() || '-') + ' шрифт ' + (bc.getFontColor() || '-');
      } else desc = 'без условия';
      for (var k = 0; k < ks.length; k++) {
        if (!byOff[ks[k]]) byOff[ks[k]] = [];
        byOff[ks[k]].push(desc);
      }
    }
    L.push('легаси-правил внутри эталонного блока (колонки L..AJ, строки 735-768): ' + n);
    var order = Object.keys(byOff).sort(function (a, b) { return Number(a) - Number(b); });
    for (var o = 0; o < order.length; o++) {
      var off = order[o];
      L.push('');
      L.push('--- смещение ' + off + ' (' + r75col_(S8.B0 + Number(off)) + ') — правил ' + byOff[off].length);
      var seen = {};
      for (var d = 0; d < byOff[off].length; d++) {
        if (seen[byOff[off][d]]) continue;
        seen[byOff[off][d]] = 1;
        L.push('    ' + s8chunk_(byOff[off][d]));
      }
    }
    L.push('');
    L.push('время ' + Math.round((new Date().getTime() - t0) / 1000) + ' с');
  } catch (e) { L.push('ОШИБКА: ' + e + (e && e.stack ? '\n' + e.stack : '')); }
  s8out_(L);
}


// ==================== ШАГ 2b: УФ СЕНТЯБРЯ ПО ЭТАЛОНУ ========================
// Проба показала, почему блоки выглядят по-разному: сентябрь внутри колонок блоков
// задевают 635 ЛЕГАСИ-правил, от 36 до 111 на блок. Пока они живы, никакая стандартизация
// статического формата блоки не уравняет.
//
// Внутри эталона легаси-правил 18, но ВИДНЫ только четыре: остальные перекрыты
// консолидированным набором R7.5/S8, который стоит выше по списку. Видимые:
//   1) глушение будущих дней   $M737 > $WB$736            -> шрифт #C0C0C0 на весь блок
//   2) градиент общей доходности (смещение 10, W)          красный/жёлтый/зелёный
//   3) отрицательная общая доходность (смещение 10)        -> шрифт #CC0000
//   4) ДРР выше 20 % (смещение 13, Z)                      -> шрифт #CC0000
//
// Их и воспроизводим для блоков 2-24 и для сводки, а сентябрь из легаси вырезаем.
// Эталон НЕ трогаем вовсе: его колонки 12..36 исключены из вырезания.
// Август не затрагивается: из диапазонов удаляются только строки 735-768.

var S8_REPRO = 'S8_REPRO_RULES';
var S8_MUTE = '#c0c0c0', S8_NEG = '#cc0000';
var S8_GRAD = ['#f8696b', '#ffeb84', '#63be7b'];

function s8rgb_(h) {
  var s = String(h).replace('#', '');
  return { red: parseInt(s.substr(0, 2), 16) / 255,
           green: parseInt(s.substr(2, 2), 16) / 255,
           blue: parseInt(s.substr(4, 2), 16) / 255 };
}

function s8rng_(gid, r1, r2, c1, ncol) {
  return { sheetId: gid, startRowIndex: r1 - 1, endRowIndex: r2,
           startColumnIndex: c1 - 1, endColumnIndex: c1 - 1 + ncol };
}

function s8fml_(gid, ranges, formula, fc, bg) {
  var fmt = {};
  if (fc) fmt.textFormat = { foregroundColorStyle: { rgbColor: s8rgb_(fc) } };
  if (bg) fmt.backgroundColorStyle = { rgbColor: s8rgb_(bg) };
  return { ranges: ranges, booleanRule: {
    condition: { type: 'CUSTOM_FORMULA', values: [{ userEnteredValue: formula }] }, format: fmt } };
}

function s8gradrule_(gid, ranges) {
  return { ranges: ranges, gradientRule: {
    minpoint: { colorStyle: { rgbColor: s8rgb_(S8_GRAD[0]) }, type: 'MIN' },
    midpoint: { colorStyle: { rgbColor: s8rgb_(S8_GRAD[1]) }, type: 'PERCENTILE', value: '50' },
    maxpoint: { colorStyle: { rgbColor: s8rgb_(S8_GRAD[2]) }, type: 'MAX' } } };
}

/** Правила, воспроизводящие видимое поведение эталона на блоках 2-24 и сводке. */
function s8repro_(gid, bl) {
  var out = [], D = S8.FIRST, U = S8.FIRST + S8.DAYS - 1;

  // 1. Глушение будущих дней. Диапазон — весь блок, поэтому дату нельзя взять
  //    относительной ссылкой: нужна своя формула на каждый блок.
  for (var b = 0; b < bl.length; b++) {
    var st = bl[b].st, dc = '$' + r75col_(st);
    out.push(s8fml_(gid, [s8rng_(gid, D, U, st, S8.BW)], '=' + dc + D + '>$WB$736', S8_MUTE, null));
  }
  out.push(s8fml_(gid, [s8rng_(gid, D, U, 1, 11)], '=$B' + D + '>$WB$736', S8_MUTE, null));

  // 2. Градиент общей доходности. Диапазоны одноколоночные и отдельным правилом
  //    на блок — так шкала MIN/MAX считается по своему SKU, как у эталона.
  for (var b2 = 0; b2 < bl.length; b2++)
    out.push(s8gradrule_(gid, [s8rng_(gid, D, U, bl[b2].st + S8_M.profitAll, 1)]));
  out.push(s8gradrule_(gid, [s8rng_(gid, D, U, 9, 1)]));

  // 3. Отрицательная общая доходность и 4. ДРР выше 20 % — ссылки относительные,
  //    поэтому хватает одного правила на все блоки.
  var rw = [], rd = [];
  for (var b3 = 0; b3 < bl.length; b3++) {
    rw.push(s8rng_(gid, D, U, bl[b3].st + S8_M.profitAll, 1));
    rd.push(s8rng_(gid, D, U, bl[b3].st + S8_M.drr, 1));
  }
  var wCol = r75col_(bl[0].st + S8_M.profitAll), zCol = r75col_(bl[0].st + S8_M.drr);
  out.push(s8fml_(gid, rw.concat([s8rng_(gid, D, U, 9, 1)]),
    '=AND(' + wCol + D + '<>"";' + wCol + D + '<0)', S8_NEG, null));
  out.push(s8fml_(gid, rd.concat([s8rng_(gid, D, U, 11, 1)]),
    '=AND(' + zCol + D + '<>"";' + zCol + D + '>0,2)', S8_NEG, null));
  return out;
}

function s8cfix() {
  var L = ['=== STAGE 8 §V.2b · s8cfix · ' + S8V.VER + ' ==='];
  try {
    var t0 = new Date().getTime();
    var ss = SpreadsheetApp.openById(S8.SSID), sh = ss.getSheetByName(S8.SH);
    var gid = sh.getSheetId();
    var head2 = sh.getRange(S8.TOP, 1, 1, S8.NC).getDisplayValues()[0];
    var bl = s8blocks_(head2);

    // Никакой сериализации 4307 правил: на прошлой попытке она съела всю память.
    // Сравниваем числа и строки формул напрямую, правило правим на месте.
    var resp = Sheets.Spreadsheets.get(S8.SSID, {
      fields: 'sheets(properties(sheetId,gridProperties(rowCount,columnCount)),conditionalFormats)' });
    var sheet = null;
    for (var i = 0; i < resp.sheets.length; i++) if (resp.sheets[i].properties.sheetId === gid) sheet = resp.sheets[i];
    var cf = sheet.conditionalFormats || [];
    var maxR = sheet.properties.gridProperties.rowCount, maxC = sheet.properties.gridProperties.columnCount;
    L.push('правил на листе: ' + cf.length + ' (Sheets API, ' + Math.round((new Date().getTime() - t0) / 1000) + ' с)');

    var mine = s8mineF_();
    var repro = s8repro_(gid, bl);
    var reproFml = {}, gradKey = {};
    for (var q = 0; q < repro.length; q++) {
      if (repro[q].booleanRule)
        reproFml[repro[q].booleanRule.condition.values[0].userEnteredValue] = 1;
      else if (repro[q].gradientRule) {
        var g0 = repro[q].ranges[0];
        gradKey[g0.startColumnIndex + ':' + g0.startRowIndex + ':' + g0.endRowIndex] = 1;
      }
    }
    var seenFml = {}, seenGrad = {};

    var MB1 = S8.B0 - 1, MB2 = S8.B0 + S8.BW - 1;
    var R1 = S8.TOP - 1, R2 = S8.PLAN;

    var upd = [], del = [], keptMine = 0, keptMaster = 0, already = 0;
    for (var k = 0; k < cf.length; k++) {
      var r = cf[k];
      var fml = '';
      if (r.booleanRule && r.booleanRule.condition && r.booleanRule.condition.type === 'CUSTOM_FORMULA') {
        var vv = r.booleanRule.condition.values;
        fml = vv && vv[0] ? String(vv[0].userEnteredValue || '') : '';
      }
      if (fml && mine[fml]) { keptMine++; continue; }
      if (fml && reproFml[fml]) { already++; seenFml[fml] = 1; continue; }
      if (r.gradientRule && r.ranges && r.ranges.length === 1) {
        var gk = r.ranges[0].startColumnIndex + ':' + r.ranges[0].startRowIndex + ':' + r.ranges[0].endRowIndex;
        if (gradKey[gk] && !seenGrad[gk]) { already++; seenGrad[gk] = 1; continue; }
      }

      var keep = [], touched = false, masterOnly = false;
      for (var g = 0; g < (r.ranges || []).length; g++) {
        var rg = r.ranges[g];
        var sr = rg.startRowIndex === undefined ? 0 : rg.startRowIndex;
        var er = rg.endRowIndex === undefined ? maxR : rg.endRowIndex;
        var sc = rg.startColumnIndex === undefined ? 0 : rg.startColumnIndex;
        var ec = rg.endColumnIndex === undefined ? maxC : rg.endColumnIndex;
        if (er <= R1 || sr >= R2 || ec <= 0 || sc >= S8.NC) { keep.push(rg); continue; }
        if (sc >= MB1 && ec <= MB2) { keep.push(rg); masterOnly = true; continue; }
        touched = true;
        if (sr < R1) keep.push({ sheetId: gid, startRowIndex: sr, endRowIndex: R1, startColumnIndex: sc, endColumnIndex: ec });
        if (er > R2) keep.push({ sheetId: gid, startRowIndex: R2, endRowIndex: er, startColumnIndex: sc, endColumnIndex: ec });
        if (ec > S8.NC) keep.push({ sheetId: gid, startRowIndex: sr < R1 ? R1 : sr, endRowIndex: er > R2 ? R2 : er,
                                    startColumnIndex: S8.NC, endColumnIndex: ec });
      }
      if (!touched) { if (masterOnly) keptMaster++; continue; }
      if (!keep.length) del.push(k);
      else { r.ranges = keep; upd.push(k); }
    }

    L.push('мои консолидированные правила оставлены: ' + keptMine);
    L.push('правила эталона (колонки L..AJ) не тронуты: ' + keptMaster);
    L.push('легаси обрезано, август сохранён: ' + upd.length);
    L.push('легаси удалено целиком, жило только в сентябре: ' + del.length);
    L.push('воспроизводящих правил уже стояло: ' + already);

    var reqs = [];
    for (var u = 0; u < upd.length; u++)
      reqs.push({ updateConditionalFormatRule: { sheetId: gid, index: upd[u], rule: cf[upd[u]] } });
    del.sort(function (a, b) { return b - a; });
    for (var d = 0; d < del.length; d++)
      reqs.push({ deleteConditionalFormatRule: { sheetId: gid, index: del[d] } });
    var toAdd = [];
    for (var p = 0; p < repro.length; p++) {
      if (repro[p].booleanRule) {
        if (!seenFml[repro[p].booleanRule.condition.values[0].userEnteredValue]) toAdd.push(repro[p]);
      } else {
        var g1 = repro[p].ranges[0];
        if (!seenGrad[g1.startColumnIndex + ':' + g1.startRowIndex + ':' + g1.endRowIndex]) toAdd.push(repro[p]);
      }
    }
    for (var a = toAdd.length - 1; a >= 0; a--)
      reqs.push({ addConditionalFormatRule: { rule: toAdd[a], index: 0 } });
    L.push('воспроизводящих правил добавлено: ' + toAdd.length + ' | запросов всего: ' + reqs.length);

    if (reqs.length) Sheets.Spreadsheets.batchUpdate({ requests: reqs }, S8.SSID);
    r75put_(S8_REPRO, { ts: new Date().toISOString(), ver: S8V.VER, n: repro.length });
    L.push('CF SEPTEMBER STANDARD применён | ' + Math.round((new Date().getTime() - t0) / 1000) + ' с');
  } catch (e) { L.push('ОШИБКА: ' + e + (e && e.stack ? '\n' + e.stack : '')); }
  s8out_(L);
}


// ==================== ШАГ 2c: ОСТАТКИ ЛЕГАСИ В ЭТАЛОНЕ =====================
// После s8cfix статический формат совпал 24/24, но ПОКРЫТИЕ условным форматированием
// у эталона осталось своё: в его колонках уцелели легаси-правила, которых нет у других
// блоков. По разбору они перекрыты консолидированным набором и не видны — но «по разбору»
// недостаточно. Поэтому: снимаем фактический вид (effectiveFormat включает результат УФ),
// удаляем правила, снимаем вид снова и сверяем. Если хоть одна ячейка изменилась —
// правило было видимым, и удаление откатывается из S8_MASTER_CF_RB.

var S8_MCF = 'S8_MASTER_CF_RB';

function s8ceff_(start) {
  var r = Sheets.Spreadsheets.get(S8.SSID, {
    ranges: [S8.SH + '!' + r75col_(start) + S8.TOP + ':' + r75col_(start + S8.BW - 1) + S8.PLAN],
    fields: 'sheets(data(rowData(values(effectiveFormat(backgroundColorStyle,textFormat,numberFormat)))))',
    includeGridData: true
  });
  var d = r.sheets[0].data[0], out = [];
  for (var y = 0; y < S8.PLAN - S8.TOP + 1; y++) {
    var row = [];
    for (var x = 0; x < S8.BW; x++) {
      var rd = d.rowData && d.rowData[y] ? d.rowData[y] : null;
      var vv = rd && rd.values && rd.values[x] ? rd.values[x].effectiveFormat : null;
      row.push(s8chash_(vv ? s8ser_(vv) : '{}'));
    }
    out.push(row);
  }
  return out;
}

function s8cmasterclean() {
  var L = ['=== STAGE 8 §V.2c · s8cmasterclean · ' + S8V.VER + ' ==='];
  try {
    var t0 = new Date().getTime();
    var sh = SpreadsheetApp.openById(S8.SSID).getSheetByName(S8.SH);
    var gid = sh.getSheetId();
    var before = s8ceff_(S8.B0);

    var resp = Sheets.Spreadsheets.get(S8.SSID, { fields: 'sheets(properties.sheetId,conditionalFormats)' });
    var cf = null;
    for (var i = 0; i < resp.sheets.length; i++) if (resp.sheets[i].properties.sheetId === gid) cf = resp.sheets[i].conditionalFormats || [];
    var mine = s8mineF_();
    var MB1 = S8.B0 - 1, MB2 = S8.B0 + S8.BW - 1, R1 = S8.TOP - 1, R2 = S8.PLAN;

    var del = [], saved = [];
    for (var k = 0; k < cf.length; k++) {
      var r = cf[k], fml = '';
      if (r.booleanRule && r.booleanRule.condition && r.booleanRule.condition.type === 'CUSTOM_FORMULA') {
        var vv2 = r.booleanRule.condition.values;
        fml = vv2 && vv2[0] ? String(vv2[0].userEnteredValue || '') : '';
      }
      if (fml && mine[fml]) continue;
      var rgs = r.ranges || [], all = rgs.length > 0, sept = false;
      for (var g = 0; g < rgs.length; g++) {
        var rg = rgs[g];
        var sr = rg.startRowIndex === undefined ? 0 : rg.startRowIndex;
        var er = rg.endRowIndex === undefined ? R2 : rg.endRowIndex;
        var sc = rg.startColumnIndex, ec = rg.endColumnIndex;
        if (sc === undefined || ec === undefined) { all = false; break; }
        if (!(sr >= R1 && er <= R2 && sc >= MB1 && ec <= MB2)) { all = false; break; }
        sept = true;
      }
      if (all && sept) { del.push(k); saved.push(r); }
    }
    L.push('легаси-правил только в сентябрьских колонках эталона: ' + del.length);
    if (!del.length) { L.push('чистить нечего'); return s8out_(L); }

    if (!r75get_(S8_MCF)) r75put_(S8_MCF, { ts: new Date().toISOString(), rules: saved });
    var reqs = [];
    del.sort(function (a, b) { return b - a; });
    for (var d2 = 0; d2 < del.length; d2++) reqs.push({ deleteConditionalFormatRule: { sheetId: gid, index: del[d2] } });
    Sheets.Spreadsheets.batchUpdate({ requests: reqs }, S8.SSID);
    SpreadsheetApp.flush();

    var after = s8ceff_(S8.B0), diff = 0, first = '';
    for (var y = 0; y < before.length; y++)
      for (var x = 0; x < S8.BW; x++) if (before[y][x] !== after[y][x]) {
        diff++;
        if (!first) first = r75col_(S8.B0 + x) + (S8.TOP + y);
      }
    L.push('правил удалено: ' + del.length + ' | откат: ' + S8_MCF);
    L.push('ФАКТИЧЕСКИЙ ВИД ЭТАЛОНА ИЗМЕНИЛСЯ В ЯЧЕЙКАХ: ' + diff + (first ? ' (первая ' + first + ')' : ''));
    L.push('MASTER UNCHANGED = ' + (diff === 0 ? 'PASS — правила были перекрыты и невидимы' : 'FAIL — нужен откат s8cmastercfrollback'));
    L.push('время ' + Math.round((new Date().getTime() - t0) / 1000) + ' с');
  } catch (e) { L.push('ОШИБКА: ' + e + (e && e.stack ? '\n' + e.stack : '')); }
  s8out_(L);
}

function s8cmastercfrollback() {
  var L = ['=== ОТКАТ удаления легаси эталона ==='];
  var rb = r75get_(S8_MCF);
  if (!rb) { L.push('снимка нет'); return s8out_(L); }
  var reqs = [];
  for (var i = 0; i < rb.rules.length; i++) reqs.push({ addConditionalFormatRule: { rule: rb.rules[i], index: 0 } });
  Sheets.Spreadsheets.batchUpdate({ requests: reqs }, S8.SSID);
  L.push('восстановлено правил: ' + rb.rules.length + ' (снимок ' + rb.ts + ')');
  s8out_(L);
}


/** То же, но для легаси эталона, которое ЗАХОДИТ и в август: сентябрь вырезаем,
 *  август оставляем нетронутым. Проверка та же — фактический вид эталона до и после. */
var S8_MCF2 = 'S8_MASTER_CF_RB2';

function s8cmastertrim() {
  var L = ['=== STAGE 8 §V.2d · s8cmastertrim · ' + S8V.VER + ' ==='];
  try {
    var t0 = new Date().getTime();
    var sh = SpreadsheetApp.openById(S8.SSID).getSheetByName(S8.SH);
    var gid = sh.getSheetId();
    var before = s8ceff_(S8.B0);

    var resp = Sheets.Spreadsheets.get(S8.SSID, { fields: 'sheets(properties(sheetId,gridProperties(rowCount,columnCount)),conditionalFormats)' });
    var sheet = null;
    for (var i = 0; i < resp.sheets.length; i++) if (resp.sheets[i].properties.sheetId === gid) sheet = resp.sheets[i];
    var cf = sheet.conditionalFormats || [];
    var maxR = sheet.properties.gridProperties.rowCount, maxC = sheet.properties.gridProperties.columnCount;
    var mine = s8mineF_();
    var MB1 = S8.B0 - 1, MB2 = S8.B0 + S8.BW - 1, R1 = S8.TOP - 1, R2 = S8.PLAN;

    var upd = [], del = [], saved = [];
    for (var k = 0; k < cf.length; k++) {
      var r = cf[k], fml = '';
      if (r.booleanRule && r.booleanRule.condition && r.booleanRule.condition.type === 'CUSTOM_FORMULA') {
        var vv2 = r.booleanRule.condition.values;
        fml = vv2 && vv2[0] ? String(vv2[0].userEnteredValue || '') : '';
      }
      if (fml && mine[fml]) continue;
      var keep = [], touched = false;
      for (var g = 0; g < (r.ranges || []).length; g++) {
        var rg = r.ranges[g];
        var sr = rg.startRowIndex === undefined ? 0 : rg.startRowIndex;
        var er = rg.endRowIndex === undefined ? maxR : rg.endRowIndex;
        var sc = rg.startColumnIndex === undefined ? 0 : rg.startColumnIndex;
        var ec = rg.endColumnIndex === undefined ? maxC : rg.endColumnIndex;
        if (er <= R1 || sr >= R2 || ec <= MB1 || sc >= MB2) { keep.push(rg); continue; }
        touched = true;
        if (sr < R1) keep.push({ sheetId: gid, startRowIndex: sr, endRowIndex: R1, startColumnIndex: sc, endColumnIndex: ec });
        if (er > R2) keep.push({ sheetId: gid, startRowIndex: R2, endRowIndex: er, startColumnIndex: sc, endColumnIndex: ec });
        if (sc < MB1) keep.push({ sheetId: gid, startRowIndex: sr < R1 ? R1 : sr, endRowIndex: er > R2 ? R2 : er, startColumnIndex: sc, endColumnIndex: MB1 });
        if (ec > MB2) keep.push({ sheetId: gid, startRowIndex: sr < R1 ? R1 : sr, endRowIndex: er > R2 ? R2 : er, startColumnIndex: MB2, endColumnIndex: ec });
      }
      if (!touched) continue;
      saved.push({ index: k, ranges: JSON.parse(JSON.stringify(r.ranges)) });
      if (!keep.length) del.push(k);
      else { r.ranges = keep; upd.push(k); }
    }
    L.push('легаси эталона, заходящего в сентябрь: ' + (upd.length + del.length) +
      ' (обрезать ' + upd.length + ', удалить ' + del.length + ')');
    if (!upd.length && !del.length) { L.push('чистить нечего'); return s8out_(L); }

    if (!r75get_(S8_MCF2)) r75put_(S8_MCF2, { ts: new Date().toISOString(), items: saved });
    var reqs = [];
    for (var u = 0; u < upd.length; u++) reqs.push({ updateConditionalFormatRule: { sheetId: gid, index: upd[u], rule: cf[upd[u]] } });
    del.sort(function (a, b) { return b - a; });
    for (var d2 = 0; d2 < del.length; d2++) reqs.push({ deleteConditionalFormatRule: { sheetId: gid, index: del[d2] } });
    Sheets.Spreadsheets.batchUpdate({ requests: reqs }, S8.SSID);
    SpreadsheetApp.flush();

    var after = s8ceff_(S8.B0), diff = 0, first = '';
    for (var y = 0; y < before.length; y++)
      for (var x = 0; x < S8.BW; x++) if (before[y][x] !== after[y][x]) {
        diff++;
        if (!first) first = r75col_(S8.B0 + x) + (S8.TOP + y);
      }
    L.push('ФАКТИЧЕСКИЙ ВИД ЭТАЛОНА ИЗМЕНИЛСЯ В ЯЧЕЙКАХ: ' + diff + (first ? ' (первая ' + first + ')' : ''));
    L.push('MASTER UNCHANGED = ' + (diff === 0 ? 'PASS' : 'FAIL') + ' | август не затронут: вырезаны только строки 735-768');
    L.push('время ' + Math.round((new Date().getTime() - t0) / 1000) + ' с');
  } catch (e) { L.push('ОШИБКА: ' + e + (e && e.stack ? '\n' + e.stack : '')); }
  s8out_(L);
}


/** Возврат диапазонов, обрезанных s8cmastertrim. Удалений там не было, индексы стабильны. */
function s8cmastertrimrollback() {
  var L = ['=== ОТКАТ s8cmastertrim ==='];
  try {
    var rb = r75get_(S8_MCF2);
    if (!rb) { L.push('снимка нет'); return s8out_(L); }
    var sh = SpreadsheetApp.openById(S8.SSID).getSheetByName(S8.SH), gid = sh.getSheetId();
    var before = s8ceff_(S8.B0);
    var resp = Sheets.Spreadsheets.get(S8.SSID, { fields: 'sheets(properties.sheetId,conditionalFormats)' });
    var cf = null;
    for (var i = 0; i < resp.sheets.length; i++) if (resp.sheets[i].properties.sheetId === gid) cf = resp.sheets[i].conditionalFormats || [];
    var reqs = [];
    for (var k = 0; k < rb.items.length; k++) {
      var it = rb.items[k], rule = cf[it.index];
      if (!rule) continue;
      rule.ranges = it.ranges;
      reqs.push({ updateConditionalFormatRule: { sheetId: gid, index: it.index, rule: rule } });
    }
    Sheets.Spreadsheets.batchUpdate({ requests: reqs }, S8.SSID);
    SpreadsheetApp.flush();
    var after = s8ceff_(S8.B0), diff = 0;
    for (var y = 0; y < before.length; y++)
      for (var x = 0; x < S8.BW; x++) if (before[y][x] !== after[y][x]) diff++;
    L.push('правил восстановлено: ' + reqs.length + ' | ячеек изменилось при возврате: ' + diff);
    L.push('ожидалось 515 — именно столько испортил обрезающий прогон');
  } catch (e) { L.push('ОШИБКА: ' + e + (e && e.stack ? '\n' + e.stack : '')); }
  s8out_(L);
}


/** Подробности по легаси, которое реально видно в эталоне: что это и на какие
 *  колонки оно распространяется. Нужно, чтобы понять, общие эти правила для блоков
 *  или только эталонные. Только чтение. */
function s8cdump() {
  var L = ['=== STAGE 8 §V · s8cdump · ' + S8V.VER + ' ==='];
  try {
    var sh = SpreadsheetApp.openById(S8.SSID).getSheetByName(S8.SH);
    var gid = sh.getSheetId();
    var resp = Sheets.Spreadsheets.get(S8.SSID, { fields: 'sheets(properties(sheetId,gridProperties(rowCount,columnCount)),conditionalFormats)' });
    var sheet = null;
    for (var i = 0; i < resp.sheets.length; i++) if (resp.sheets[i].properties.sheetId === gid) sheet = resp.sheets[i];
    var cf = sheet.conditionalFormats || [];
    var maxR = sheet.properties.gridProperties.rowCount, maxC = sheet.properties.gridProperties.columnCount;
    var mine = s8mineF_();
    var MB1 = S8.B0 - 1, MB2 = S8.B0 + S8.BW - 1, R1 = S8.TOP - 1, R2 = S8.PLAN;
    var n = 0;
    for (var k = 0; k < cf.length; k++) {
      var r = cf[k], fml = '';
      if (r.booleanRule && r.booleanRule.condition && r.booleanRule.condition.type === 'CUSTOM_FORMULA') {
        var vv = r.booleanRule.condition.values;
        fml = vv && vv[0] ? String(vv[0].userEnteredValue || '') : '';
      }
      if (fml && mine[fml]) continue;
      var hit = false, cols = {}, rows = {}, wide = 0;
      for (var g = 0; g < (r.ranges || []).length; g++) {
        var rg = r.ranges[g];
        var sr = rg.startRowIndex === undefined ? 0 : rg.startRowIndex;
        var er = rg.endRowIndex === undefined ? maxR : rg.endRowIndex;
        var sc = rg.startColumnIndex === undefined ? 0 : rg.startColumnIndex;
        var ec = rg.endColumnIndex === undefined ? maxC : rg.endColumnIndex;
        if (er <= R1 || sr >= R2 || ec <= MB1 || sc >= MB2) { if (sc < S8.NC) wide++; continue; }
        hit = true;
        cols[(sc + 1) + '-' + ec] = 1;
        rows[(sr + 1) + '-' + er] = 1;
      }
      if (!hit) continue;
      n++;
      var kind = r.gradientRule ? 'ГРАДИЕНТ' : (fml ? 'ФОРМУЛА' : 'ДРУГОЕ');
      var fmt = r.booleanRule && r.booleanRule.format ? r.booleanRule.format : null;
      var col = fmt && fmt.textFormat && fmt.textFormat.foregroundColorStyle ? 'шрифт' : (fmt && fmt.backgroundColorStyle ? 'фон' : '');
      L.push('#' + k + ' ' + kind + ' ' + col + ' диап.всего ' + (r.ranges || []).length +
        ' вне эталона ' + wide + ' | кол ' + Object.keys(cols).join(',') + ' | стр ' + Object.keys(rows).join(','));
      if (fml) L.push('      ' + s8chunk_(s8sp_(fml)));
    }
    L.push('');
    L.push('итого легаси, видимого в эталоне: ' + n);
  } catch (e) { L.push('ОШИБКА: ' + e + (e && e.stack ? '\n' + e.stack : '')); }
  s8out_(L);
}


// ==================== ШАГ 2e: ЗЕРКАЛИРОВАНИЕ УФ ЭТАЛОНА ====================
// Разбор показал ошибку шага 2b: часть легаси-правил покрывала СРАЗУ ВСЕ блоки одним
// правилом. Вырезав сентябрь «везде, кроме колонок эталона», я оставил такое правило
// только эталону — отсюда расхождение покрытия. Правильное действие обратное:
// взять сентябрьские диапазоны, которые есть у эталона, и до-дать такие же
// (с тем же смещением внутри блока) остальным 23 блокам.
//
// Правило пропускается, если в его формуле есть АБСОЛЮТНАЯ ссылка на колонку внутри
// блока: её нельзя сдвинуть простым копированием диапазона, для таких у нас уже есть
// отдельные правила на каждый блок.

function s8cabs_(fml) {
  var re = /\$([A-Z]{1,3})\$?\d+/g, mm;
  while ((mm = re.exec(String(fml))) !== null) {
    var c = s8num_(mm[1]);
    if (c >= S8.B0 && c <= S8.NC) return true;
  }
  return false;
}

function s8crestore() {
  var L = ['=== STAGE 8 §V.2e · s8crestore · ' + S8V.VER + ' ==='];
  try {
    var t0 = new Date().getTime();
    var sh = SpreadsheetApp.openById(S8.SSID).getSheetByName(S8.SH);
    var gid = sh.getSheetId();
    var head = sh.getRange(S8.TOP, 1, 1, S8.NC).getDisplayValues()[0];
    var bl = s8blocks_(head);
    var before = s8ceff_(S8.B0);

    var resp = Sheets.Spreadsheets.get(S8.SSID, { fields: 'sheets(properties(sheetId,gridProperties(rowCount,columnCount)),conditionalFormats)' });
    var sheet = null;
    for (var i = 0; i < resp.sheets.length; i++) if (resp.sheets[i].properties.sheetId === gid) sheet = resp.sheets[i];
    var cf = sheet.conditionalFormats || [];
    var maxR = sheet.properties.gridProperties.rowCount, maxC = sheet.properties.gridProperties.columnCount;
    var MB1 = S8.B0 - 1, MB2 = S8.B0 + S8.BW - 1, R1 = S8.TOP - 1, R2 = S8.PLAN;

    var upd = [], skipped = [], added = 0, rb = [];
    for (var k = 0; k < cf.length; k++) {
      var r = cf[k], fml = '';
      if (r.booleanRule && r.booleanRule.condition && r.booleanRule.condition.type === 'CUSTOM_FORMULA') {
        var vv = r.booleanRule.condition.values;
        fml = vv && vv[0] ? String(vv[0].userEnteredValue || '') : '';
      }
      var rgs = r.ranges || [], have = {}, master = [];
      for (var g = 0; g < rgs.length; g++) {
        var rg = rgs[g];
        var sr = rg.startRowIndex === undefined ? 0 : rg.startRowIndex;
        var er = rg.endRowIndex === undefined ? maxR : rg.endRowIndex;
        var sc = rg.startColumnIndex === undefined ? 0 : rg.startColumnIndex;
        var ec = rg.endColumnIndex === undefined ? maxC : rg.endColumnIndex;
        if (er <= R1 || sr >= R2) continue;
        have[sc + ':' + ec + ':' + sr + ':' + er] = 1;
        if (sc >= MB1 && ec <= MB2) master.push({ sc: sc, ec: ec, sr: sr, er: er });
      }
      if (!master.length) continue;
      if (fml && s8cabs_(fml)) { skipped.push('#' + k); continue; }

      var add2 = [];
      for (var mi = 0; mi < master.length; mi++) {
        var mr = master[mi];
        for (var b = 1; b < bl.length; b++) {
          var d = bl[b].st - S8.B0;
          var key = (mr.sc + d) + ':' + (mr.ec + d) + ':' + mr.sr + ':' + mr.er;
          if (have[key]) continue;
          have[key] = 1;
          add2.push({ sheetId: gid, startRowIndex: mr.sr, endRowIndex: mr.er,
                      startColumnIndex: mr.sc + d, endColumnIndex: mr.ec + d });
        }
      }
      if (!add2.length) continue;
      rb.push([k, rgs.length]);
      r.ranges = rgs.concat(add2);
      upd.push(k);
      added += add2.length;
    }
    L.push('правил, у которых сентябрь был только у эталона: ' + upd.length);
    L.push('диапазонов до-дано остальным 23 блокам: ' + added);
    L.push('пропущено из-за абсолютной ссылки на колонку блока: ' + skipped.length +
      (skipped.length ? ' ' + skipped.join(' ') : '') + ' — у них уже есть отдельные правила на блок');
    if (!upd.length) { L.push('добавлять нечего'); return s8out_(L); }
    r75put_('S8_RESTORE_RB', { ver: S8V.VER, ts: new Date().toISOString(), keep: rb });
    L.push('откат сохранён: S8_RESTORE_RB');

    var reqs = [];
    for (var u = 0; u < upd.length; u++)
      reqs.push({ updateConditionalFormatRule: { sheetId: gid, index: upd[u], rule: cf[upd[u]] } });
    Sheets.Spreadsheets.batchUpdate({ requests: reqs }, S8.SSID);
    SpreadsheetApp.flush();

    var after = s8ceff_(S8.B0), diff = 0;
    for (var y = 0; y < before.length; y++)
      for (var x = 0; x < S8.BW; x++) if (before[y][x] !== after[y][x]) diff++;
    L.push('вид ЭТАЛОНА изменился в ячейках: ' + diff + ' (должен остаться 0 — ему ничего не меняли)');
    L.push('MASTER UNCHANGED = ' + (diff === 0 ? 'PASS' : 'FAIL') +
      ' | ' + Math.round((new Date().getTime() - t0) / 1000) + ' с');
  } catch (e) { L.push('ОШИБКА: ' + e + (e && e.stack ? '\n' + e.stack : '')); }
  s8out_(L);
}


/**
 * ОТКАТ s8crestore: добавленные диапазоны шли в КОНЕЦ списка каждого правила,
 * поэтому возврат — это обрезка ranges до исходной длины. Точно и без снимка.
 */
function s8crestorerollback() {
  var L = ['=== STAGE 8 §V.2e · s8crestorerollback · ' + S8V.VER + ' ==='];
  try {
    var rb = r75get_('S8_RESTORE_RB');
    if (!rb || !rb.keep) { L.push('откат не найден: S8_RESTORE_RB'); return s8out_(L); }
    var sh = SpreadsheetApp.openById(S8.SSID).getSheetByName(S8.SH), gid = sh.getSheetId();
    var resp = Sheets.Spreadsheets.get(S8.SSID, { fields: 'sheets(properties(sheetId),conditionalFormats)' });
    var sheet = null;
    for (var i = 0; i < resp.sheets.length; i++) if (resp.sheets[i].properties.sheetId === gid) sheet = resp.sheets[i];
    var cf = sheet.conditionalFormats || [], reqs = [], cut = 0;
    for (var j = 0; j < rb.keep.length; j++) {
      var k = rb.keep[j][0], n = rb.keep[j][1], r = cf[k];
      if (!r || !r.ranges || r.ranges.length <= n) continue;
      cut += r.ranges.length - n;
      r.ranges = r.ranges.slice(0, n);
      reqs.push({ updateConditionalFormatRule: { sheetId: gid, index: k, rule: r } });
    }
    if (!reqs.length) { L.push('нечего откатывать'); return s8out_(L); }
    Sheets.Spreadsheets.batchUpdate({ requests: reqs }, S8.SSID);
    SpreadsheetApp.flush();
    L.push('правил возвращено: ' + reqs.length + ' | диапазонов убрано: ' + cut);
  } catch (e) { L.push('ОШИБКА: ' + e); }
  s8out_(L);
}


/** ЧТЕНИЕ: что именно доопределил s8crestore и где градиенты. */
function s8cgaudit() {
  var L = ['=== STAGE 8 §V.2f · s8cgaudit · ' + S8V.VER + ' ==='];
  try {
    var rb = r75get_('S8_RESTORE_RB');
    var sh = SpreadsheetApp.openById(S8.SSID).getSheetByName(S8.SH), gid = sh.getSheetId();
    var resp = Sheets.Spreadsheets.get(S8.SSID, { fields: 'sheets(properties(sheetId),conditionalFormats)' });
    var sheet = null, i;
    for (i = 0; i < resp.sheets.length; i++) if (resp.sheets[i].properties.sheetId === gid) sheet = resp.sheets[i];
    var cf = sheet.conditionalFormats || [];
    L.push('всего правил на листе: ' + cf.length);
    L.push('записей в S8_RESTORE_RB: ' + (rb && rb.keep ? rb.keep.length : 0));
    var head = sh.getRange(S8.TOP, 1, 1, S8.NC).getDisplayValues()[0];
    var bl = s8blocks_(head);
    var vals = sh.getRange(S8.FIRST, 1, S8.DAYS, S8.NC).getValues();
    for (var j = 0; j < rb.keep.length; j++) {
      var k = rb.keep[j][0], n0 = rb.keep[j][1], r = cf[k];
      var kind = r.gradientRule ? 'ГРАДИЕНТ' : 'ЛОГИКА';
      var sc = r.ranges[0].startColumnIndex, ec = r.ranges[0].endColumnIndex;
      var off = sc + 1 - S8.B0;
      L.push('');
      L.push('#' + k + ' ' + kind + ' · было диап. ' + n0 + ' · стало ' + r.ranges.length +
             ' · колонки 1-й ' + r75col_(sc + 1) + '..' + r75col_(ec) + ' · смещение в блоке ' + off);
      if (r.gradientRule) {
        var g = r.gradientRule, pts = [];
        ['minpoint', 'midpoint', 'maxpoint'].forEach(function (p) {
          if (!g[p]) return;
          var c = g[p].colorStyle && g[p].colorStyle.rgbColor ? g[p].colorStyle.rgbColor : (g[p].color || {});
          pts.push(p + ':' + g[p].type + (g[p].value ? '(' + g[p].value + ')' : '') + '·' + s8hex_(c));
        });
        L.push('   ' + pts.join(' | '));
        var mn1 = null, mx1 = null, mn2 = null, mx2 = null, cnt2 = 0;
        for (var b = 0; b < bl.length; b++) {
          var col = bl[b].st + off;
          for (var rr = 0; rr < S8.DAYS; rr++) {
            var x = vals[rr][col - 1];
            if (typeof x !== 'number') continue;
            if (b === 0) { if (mn1 === null || x < mn1) mn1 = x; if (mx1 === null || x > mx1) mx1 = x; }
            cnt2++;
            if (mn2 === null || x < mn2) mn2 = x; if (mx2 === null || x > mx2) mx2 = x;
          }
        }
        L.push('   шкала только эталон: ' + mn1 + ' .. ' + mx1);
        L.push('   шкала все 24 блока: ' + mn2 + ' .. ' + mx2 + ' (чисел ' + cnt2 + ')');
      }
    }
  } catch (e) { L.push('ОШИБКА: ' + e + (e && e.stack ? '\n' + e.stack : '')); }
  s8out_(L);
}

function s8hex_(c) {
  function h(x) { var s = Math.round((x || 0) * 255).toString(16); return s.length < 2 ? '0' + s : s; }
  return '#' + h(c.red) + h(c.green) + h(c.blue);
}


/**
 * РАЗВЯЗКА ГРАДИЕНТОВ. У градиентного правила шкала MIN/PERCENTILE/MAX считается
 * по ВСЕМ его диапазонам сразу. Поэтому дать одному правилу диапазоны 23 блоков —
 * значит пересчитать шкалу и сдвинуть цвета ЭТАЛОНА (замерено: 8 ячеек).
 * Правильно: у каждого блока своё правило с той же палитрой и той же логикой,
 * шкала считается внутри блока. Логические правила это не затрагивает — там
 * нет связи между диапазонами, они остаются одним правилом.
 */
function s8cgfix() {
  var L = ['=== STAGE 8 §V.2g · s8cgfix · ' + S8V.VER + ' ==='];
  try {
    var t0 = new Date().getTime();
    var rb = r75get_('S8_RESTORE_RB');
    if (!rb || !rb.keep) { L.push('нет S8_RESTORE_RB — сначала s8crestore'); return s8out_(L); }
    var sh = SpreadsheetApp.openById(S8.SSID).getSheetByName(S8.SH), gid = sh.getSheetId();
    var before = s8ceff_(S8.B0);
    var resp = Sheets.Spreadsheets.get(S8.SSID, { fields: 'sheets(properties(sheetId),conditionalFormats)' });
    var sheet = null, i;
    for (i = 0; i < resp.sheets.length; i++) if (resp.sheets[i].properties.sheetId === gid) sheet = resp.sheets[i];
    var cf = sheet.conditionalFormats || [];

    var items = [];
    for (var j = 0; j < rb.keep.length; j++) {
      var k0 = rb.keep[j][0], n0 = rb.keep[j][1];
      if (cf[k0] && cf[k0].gradientRule && cf[k0].ranges.length > n0) items.push({ k: k0, n: n0 });
    }
    items.sort(function (a, b) { return b.k - a.k; });
    L.push('градиентных правил к развязке: ' + items.length);
    if (!items.length) { L.push('нечего делать'); return s8out_(L); }

    var reqs = [], made = 0, rbg = [];
    for (var q = 0; q < items.length; q++) {
      var kk = items[q].k, n = items[q].n, r = cf[kk];
      var extra = r.ranges.slice(n), keep = r.ranges.slice(0, n);
      var byd = {}, order = [];
      for (var e = 0; e < extra.length; e++) {
        var key = String(Math.floor((extra[e].startColumnIndex - (S8.B0 - 1)) / S8.BW));
        if (!byd[key]) { byd[key] = []; order.push(key); }
        byd[key].push(extra[e]);
      }
      var upd = JSON.parse(JSON.stringify(r)); upd.ranges = keep;
      reqs.push({ updateConditionalFormatRule: { sheetId: gid, index: kk, rule: upd } });
      for (var o = order.length - 1; o >= 0; o--) {
        var nr = JSON.parse(JSON.stringify(r)); nr.ranges = byd[order[o]];
        reqs.push({ addConditionalFormatRule: { index: kk + 1, rule: nr } });
        made++;
      }
      rbg.push([kk, order.length]);
      L.push('  #' + kk + ' диапазонов у эталона ' + n + ' · отдельных правил блокам ' + order.length);
    }
    r75put_('S8_GRAD_RB', { ver: S8V.VER, ts: new Date().toISOString(), ins: rbg });
    L.push('откат сохранён: S8_GRAD_RB');
    Sheets.Spreadsheets.batchUpdate({ requests: reqs }, S8.SSID);
    SpreadsheetApp.flush();
    L.push('новых правил на блоки: ' + made);

    var chk = Sheets.Spreadsheets.get(S8.SSID, { fields: 'sheets(properties(sheetId),conditionalFormats(ranges))' });
    var sh2 = null;
    for (i = 0; i < chk.sheets.length; i++) if (chk.sheets[i].properties.sheetId === gid) sh2 = chk.sheets[i];
    var cf2 = sh2.conditionalFormats || [], okg = 0;
    for (q = 0; q < items.length; q++) {
      var shift = 0;
      for (var w = 0; w < rbg.length; w++) if (rbg[w][0] < items[q].k) shift += rbg[w][1];
      if (cf2[items[q].k + shift] && cf2[items[q].k + shift].ranges.length === items[q].n) okg++;
    }
    L.push('ГРАДИЕНТЫ ЭТАЛОНА ВОЗВРАЩЕНЫ ... ' + okg + ' / ' + items.length);

    var after = s8ceff_(S8.B0), diff = [];
    for (var y = 0; y < before.length; y++)
      for (var x = 0; x < S8.BW; x++)
        if (before[y][x] !== after[y][x]) diff.push(r75col_(S8.B0 + x) + (S8.FIRST + y));
    L.push('вид ЭТАЛОНА вернулся в ячейках: ' + diff.length + (diff.length ? ' — ' + diff.join(' ') : ''));
    L.push('MASTER RESTORED = ' + (okg === items.length ? 'PASS' : 'FAIL') +
      ' | ' + Math.round((new Date().getTime() - t0) / 1000) + ' с');
  } catch (e) { L.push('ОШИБКА: ' + e + (e && e.stack ? '\n' + e.stack : '')); }
  s8out_(L);
}

/** ОТКАТ s8cgfix: удалить вставленные правила (сверху вниз по индексу). */
function s8cgfixrollback() {
  var L = ['=== STAGE 8 §V.2g · s8cgfixrollback · ' + S8V.VER + ' ==='];
  try {
    var g = r75get_('S8_GRAD_RB');
    if (!g || !g.ins) { L.push('откат не найден: S8_GRAD_RB'); return s8out_(L); }
    var gid = SpreadsheetApp.openById(S8.SSID).getSheetByName(S8.SH).getSheetId();
    var ins = g.ins.slice().sort(function (a, b) { return b[0] - a[0]; });
    var reqs = [], n = 0;
    for (var j = 0; j < ins.length; j++)
      for (var c = 0; c < ins[j][1]; c++) { reqs.push({ deleteConditionalFormatRule: { sheetId: gid, index: ins[j][0] + 1 } }); n++; }
    Sheets.Spreadsheets.batchUpdate({ requests: reqs }, S8.SSID);
    L.push('правил удалено: ' + n);
  } catch (e) { L.push('ОШИБКА: ' + e); }
  s8out_(L);
}


/* ============ §V.5 ПОКРЫТИЕ УФ: СРАВНЕНИЕ ПО ЭФФЕКТУ, А НЕ ПО НОМЕРУ ПРАВИЛА =====
 * Старый s8ccf_ включал в отпечаток НОМЕР правила. Это годится, только если все
 * блоки покрыты одним и тем же правилом. Но часть дизайна принципиально сделана
 * отдельным правилом на блок: градиенты (у них шкала общая на все диапазоны
 * правила, поэтому одно правило на 24 блока перекрашивает эталон) и приглушение
 * будущих дней. Такие блоки старый отпечаток объявлял расхождением всегда.
 * Правильный отпечаток — САМО ПРАВИЛО: условие (с нормализацией ссылок в
 * смещения) + формат. Тогда «то же оформление другим правилом» считается
 * совпадением, а «нет оформления» — расхождением.
 * =========================================================================== */

function s8cnum_(a) { var n = 0; for (var i = 0; i < a.length; i++) n = n * 26 + (a.charCodeAt(i) - 64); return n; }

function s8chex_(o) {
  if (!o) return '-';
  var c = o.rgbColor ? o.rgbColor : o;
  if (c.themeColor) return 'T' + c.themeColor;
  function h(x) { var s = Math.round((x || 0) * 255).toString(16); return s.length < 2 ? '0' + s : s; }
  return h(c.red) + h(c.green) + h(c.blue);
}

/**
 * Нормализация ссылок в формуле УФ:
 *  - относительная колонка — смещение от якоря правила (c±N);
 *  - АБСОЛЮТНАЯ колонка ВНУТРИ того же блока — смещение от начала блока (C±N).
 *    Так «$M737» у эталона и «$AK737» у блока 2 — одно и то же правило;
 *  - всё остальное (зеркало $WB$736, сводка) — буквально.
 */
function s8cnorm_(f, ac, ar) {
  return String(f).replace(/(\$?)([A-Z]{1,3})(\$?)(\d+)/g, function (all, d1, col, d2, row) {
    var rc = s8cnum_(col);
    if (!d1) return 'c' + (rc - ac) + 'r' + (Number(row) - ar);
    if (rc >= S8.B0 && rc < S8.NC) {
      var bs = S8.B0 + Math.floor((rc - S8.B0) / S8.BW) * S8.BW;
      return 'C' + (rc - bs) + (d2 ? 'R' + row : 'r' + (Number(row) - ar));
    }
    return all;
  });
}

function s8cfp_(r) {
  var rg = (r.ranges && r.ranges[0]) || {};
  var ac = (rg.startColumnIndex || 0) + 1, ar = (rg.startRowIndex || 0) + 1, p;
  if (r.gradientRule) {
    var g = r.gradientRule, s = ['G'];
    ['minpoint', 'midpoint', 'maxpoint'].forEach(function (k) {
      if (!g[k]) return;
      p = g[k];
      s.push(k.charAt(0) + p.type + (p.value === undefined ? '' : p.value) + '#' + s8chex_(p.colorStyle || p.color));
    });
    return s.join('~');
  }
  var b = r.booleanRule || {}, c = b.condition || {}, f = b.format || {}, tf = f.textFormat || {};
  var vals = (c.values || []).map(function (x) {
    var u = x.userEnteredValue === undefined ? '' : String(x.userEnteredValue);
    return u.charAt(0) === '=' ? s8cnorm_(u, ac, ar) : u;
  }).join(',');
  return 'B' + c.type + '(' + vals + ')~bg#' + s8chex_(f.backgroundColorStyle || f.backgroundColor) +
    '~fc#' + s8chex_(tf.foregroundColorStyle || tf.foregroundColor) +
    '~' + (tf.bold ? 'b' : '') + (tf.italic ? 'i' : '') + (tf.strikethrough ? 's' : '');
}

function s8ccfqa() {
  var L = ['=== STAGE 8 §V.5 · s8ccfqa (покрытие УФ по эффекту) · ' + S8V.VER + ' ==='];
  try {
    var t0 = new Date().getTime();
    var sh = SpreadsheetApp.openById(S8.SSID).getSheetByName(S8.SH), gid = sh.getSheetId();
    var head = sh.getRange(S8.TOP, 1, 1, S8.NC).getDisplayValues()[0];
    var bl = s8blocks_(head);
    var resp = Sheets.Spreadsheets.get(S8.SSID, { fields: 'sheets(properties.sheetId,conditionalFormats)' });
    var cf = null, i;
    for (i = 0; i < resp.sheets.length; i++) if (resp.sheets[i].properties.sheetId === gid) cf = resp.sheets[i].conditionalFormats || [];
    L.push('правил на листе: ' + cf.length);

    var R1 = S8.TOP - 1, R2 = S8.PLAN, sig = [];
    for (var b = 0; b < bl.length; b++) sig.push({});
    for (var k = 0; k < cf.length; k++) {
      var rgs = cf[k].ranges || [], hit = false, gg;
      for (gg = 0; gg < rgs.length; gg++) {
        var e1 = rgs[gg].endRowIndex === undefined ? R2 : rgs[gg].endRowIndex;
        var s1 = rgs[gg].startRowIndex === undefined ? 0 : rgs[gg].startRowIndex;
        if (!(e1 <= R1 || s1 >= R2)) { hit = true; break; }
      }
      if (!hit) continue;
      var fp = s8cfp_(cf[k]);
      for (gg = 0; gg < rgs.length; gg++) {
        var rg = rgs[gg];
        var sr = rg.startRowIndex === undefined ? 0 : rg.startRowIndex;
        var er = rg.endRowIndex === undefined ? R2 : rg.endRowIndex;
        if (er <= R1 || sr >= R2) continue;
        var sc = rg.startColumnIndex, ec = rg.endColumnIndex;
        if (sc === undefined || ec === undefined) continue;
        for (var b2 = 0; b2 < bl.length; b2++) {
          var st = bl[b2].st - 1;
          if (ec <= st || sc >= st + S8.BW) continue;
          sig[b2][fp + '@' + (Math.max(sc, st) - st) + ':' + (Math.min(ec, st + S8.BW) - st) + ':' + sr + ':' + er] = 1;
        }
      }
    }

    var ref = sig[0], bad = [], det = [];
    for (var b3 = 1; b3 < bl.length; b3++) {
      var miss = [], extra = [], key;
      for (key in ref) if (!sig[b3][key]) miss.push(key);
      for (key in sig[b3]) if (!ref[key]) extra.push(key);
      if (!miss.length && !extra.length) continue;
      bad.push('#' + bl[b3].i);
      if (det.length < 3) det.push('#' + bl[b3].i + ' ' + bl[b3].nm + ' — нет ' + miss.length + ', лишних ' + extra.length +
        (miss.length ? '\n      НЕТ: ' + miss.slice(0, 4).join('\n           ') : '') +
        (extra.length ? '\n      ЛИШНЕЕ: ' + extra.slice(0, 4).join('\n              ') : ''));
    }
    var n = 0; for (var kk in ref) n++;
    L.push('оформлений у эталона (условие+формат+колонки+строки): ' + n);
    L.push('');
    for (i = 0; i < det.length; i++) L.push(det[i]);
    L.push('');
    L.push('CF COVERAGE ... ' + (bl.length - bad.length) + ' / ' + bl.length + (bad.length ? ' <-- ' + bad.join(' ') : ''));
    L.push('CF COVERAGE = ' + (bad.length ? 'FAIL' : 'PASS 24/24') + ' | ' + Math.round((new Date().getTime() - t0) / 1000) + ' с');
  } catch (e) { L.push('ОШИБКА: ' + e + (e && e.stack ? '\n' + e.stack : '')); }
  s8out_(L);
}


/** Сдвиг всех ссылок формулы УФ внутри сетки блоков на d колонок. Зеркало $WB$736 вне сетки. */
/*
 * Сдвиг формулы УФ при копировании правила в другой блок.
 * ВАЖНО: Sheets хранит формулу так, как она применяется к ЛЕВОМУ ВЕРХНЕМУ углу
 * ПЕРВОГО диапазона правила. У правила-источника первый диапазон может быть
 * в сводке (например F737), и тогда «D737» означает «на две колонки левее».
 * При переносе в однодиапазонное правило якорь меняется, поэтому ОТНОСИТЕЛЬНЫЕ
 * ссылки обязаны сдвинуться на разницу якорей (rc0/rr0), а АБСОЛЮТНЫЕ — на смещение
 * блока d, и только если ссылка внутри сетки блоков (зеркало $WB$736 остаётся).
 */
function s8cshift_(f, rc0, rr0, d) {
  return String(f).replace(/(\$?)([A-Z]{1,3})(\$?)(\d+)/g, function (all, d1, col, d2, row) {
    var c = s8cnum_(col), r = Number(row);
    if (d1) { if (c >= S8.B0 && c < S8.NC) c += d; } else c += rc0;
    if (!d2) r += rr0;
    return d1 + r75col_(c) + d2 + r;
  });
}

/**
 * §V.6 ДОДЕЛАТЬ ПОКРЫТИЕ. Берём оформления, которые есть у ЭТАЛОНА и которых нет
 * у блока, и делаем блоку СВОЁ правило: те же диапазоны со смещением и формула,
 * в которой все ссылки внутри сетки блоков сдвинуты на то же смещение.
 * Эталон не трогаем вообще — только добавляем правила ниже по списку.
 */
function s8cclone() {
  var L = ['=== STAGE 8 §V.6 · s8cclone · ' + S8V.VER + ' ==='];
  try {
    var t0 = new Date().getTime();
    var sh = SpreadsheetApp.openById(S8.SSID).getSheetByName(S8.SH), gid = sh.getSheetId();
    var head = sh.getRange(S8.TOP, 1, 1, S8.NC).getDisplayValues()[0];
    var bl = s8blocks_(head);
    var before = s8ceff_(S8.B0);
    var resp = Sheets.Spreadsheets.get(S8.SSID, { fields: 'sheets(properties.sheetId,conditionalFormats)' });
    var cf = null, i, k, gg;
    for (i = 0; i < resp.sheets.length; i++) if (resp.sheets[i].properties.sheetId === gid) cf = resp.sheets[i].conditionalFormats || [];
    var R1 = S8.TOP - 1, R2 = S8.PLAN;

    // 1) отпечатки по блокам + запоминаем, какие диапазоны эталона дали какой ключ
    var sig = [], src = {};
    for (var b = 0; b < bl.length; b++) sig.push({});
    for (k = 0; k < cf.length; k++) {
      var rgs = cf[k].ranges || [], hit = false;
      for (gg = 0; gg < rgs.length; gg++) {
        var s1 = rgs[gg].startRowIndex === undefined ? 0 : rgs[gg].startRowIndex;
        var e1 = rgs[gg].endRowIndex === undefined ? R2 : rgs[gg].endRowIndex;
        if (!(e1 <= R1 || s1 >= R2)) { hit = true; break; }
      }
      if (!hit) continue;
      var fp = s8cfp_(cf[k]);
      for (gg = 0; gg < rgs.length; gg++) {
        var rg = rgs[gg];
        var sr = rg.startRowIndex === undefined ? 0 : rg.startRowIndex;
        var er = rg.endRowIndex === undefined ? R2 : rg.endRowIndex;
        if (er <= R1 || sr >= R2) continue;
        var sc = rg.startColumnIndex, ec = rg.endColumnIndex;
        if (sc === undefined || ec === undefined) continue;
        for (var b2 = 0; b2 < bl.length; b2++) {
          var st = bl[b2].st - 1;
          if (ec <= st || sc >= st + S8.BW) continue;
          var key = fp + '@' + (Math.max(sc, st) - st) + ':' + (Math.min(ec, st + S8.BW) - st) + ':' + sr + ':' + er;
          sig[b2][key] = 1;
          if (b2 === 0) {
            if (!src[key]) src[key] = { k: k, rg: [] };
            src[key].rg.push({ sc: Math.max(sc, st), ec: Math.min(ec, st + S8.BW), sr: sr, er: er });
          }
        }
      }
    }

    // 2) чего не хватает блокам
    var need = {}, cnt = 0;      // need[k] = { b: [ranges...] }
    for (var b3 = 1; b3 < bl.length; b3++) {
      var d = bl[b3].st - S8.B0;
      for (var key2 in sig[0]) {
        if (sig[b3][key2] || !src[key2]) continue;
        var kk = src[key2].k;
        if (!need[kk]) need[kk] = {};
        if (!need[kk][b3]) need[kk][b3] = [];
        for (i = 0; i < src[key2].rg.length; i++) {
          var r0 = src[key2].rg[i];
          need[kk][b3].push({ sheetId: gid, startRowIndex: r0.sr, endRowIndex: r0.er,
                              startColumnIndex: r0.sc + d, endColumnIndex: r0.ec + d });
        }
        cnt++;
      }
    }
    var keys = [];
    for (var kn in need) keys.push(Number(kn));
    keys.sort(function (a, b) { return b - a; });
    L.push('правил-источников: ' + keys.length + ' · недостающих оформлений всего: ' + cnt);
    if (!keys.length) { L.push('покрытие уже полное'); return s8out_(L); }

    // 3) копии
    var reqs = [], made = 0, rbc = [];
    for (var q = 0; q < keys.length; q++) {
      var ks = keys[q], per = need[ks], bs2 = [];
      for (var bb in per) bs2.push(Number(bb));
      bs2.sort(function (a, b) { return b - a; });
      for (var z = 0; z < bs2.length; z++) {
        var bi = bs2[z], dd = bl[bi].st - S8.B0;
        var nr = JSON.parse(JSON.stringify(cf[ks]));
        nr.ranges = per[bi];
        var o0 = cf[ks].ranges[0];
        var rc0 = per[bi][0].startColumnIndex - (o0.startColumnIndex || 0);
        var rr0 = per[bi][0].startRowIndex - (o0.startRowIndex || 0);
        if (nr.booleanRule && nr.booleanRule.condition && nr.booleanRule.condition.type === 'CUSTOM_FORMULA') {
          nr.booleanRule.condition.values = (nr.booleanRule.condition.values || []).map(function (x) {
            var u = x.userEnteredValue === undefined ? '' : String(x.userEnteredValue);
            return { userEnteredValue: u.charAt(0) === '=' ? s8cshift_(u, rc0, rr0, dd) : u };
          });
        }
        reqs.push({ addConditionalFormatRule: { index: ks + 1, rule: nr } });
        made++;
      }
      rbc.push([ks, bs2.length]);
      L.push('  из #' + ks + ' — копий блокам: ' + bs2.length);
    }
    r75put_('S8_CLONE_RB', { ver: S8V.VER, ts: new Date().toISOString(), ins: rbc });
    L.push('откат сохранён: S8_CLONE_RB');
    Sheets.Spreadsheets.batchUpdate({ requests: reqs }, S8.SSID);
    SpreadsheetApp.flush();
    L.push('новых правил: ' + made);

    var after = s8ceff_(S8.B0), diff = 0;
    for (var y = 0; y < before.length; y++)
      for (var x = 0; x < S8.BW; x++) if (before[y][x] !== after[y][x]) diff++;
    L.push('вид ЭТАЛОНА изменился в ячейках: ' + diff);
    L.push('MASTER UNCHANGED = ' + (diff === 0 ? 'PASS' : 'FAIL') +
      ' | ' + Math.round((new Date().getTime() - t0) / 1000) + ' с');
  } catch (e) { L.push('ОШИБКА: ' + e + (e && e.stack ? '\n' + e.stack : '')); }
  s8out_(L);
}

/** ОТКАТ s8cclone. */
function s8cclonerollback() {
  var L = ['=== STAGE 8 §V.6 · s8cclonerollback · ' + S8V.VER + ' ==='];
  try {
    var g = r75get_('S8_CLONE_RB');
    if (!g || !g.ins) { L.push('откат не найден: S8_CLONE_RB'); return s8out_(L); }
    var gid = SpreadsheetApp.openById(S8.SSID).getSheetByName(S8.SH).getSheetId();
    var ins = g.ins.slice().sort(function (a, b) { return b[0] - a[0]; });
    var reqs = [], n = 0;
    for (var j = 0; j < ins.length; j++)
      for (var c = 0; c < ins[j][1]; c++) { reqs.push({ deleteConditionalFormatRule: { sheetId: gid, index: ins[j][0] + 1 } }); n++; }
    Sheets.Spreadsheets.batchUpdate({ requests: reqs }, S8.SSID);
    L.push('правил удалено: ' + n);
  } catch (e) { L.push('ОШИБКА: ' + e); }
  s8out_(L);
}


/** ЧТЕНИЕ: как на самом деле записана формула правила-источника и его копий. */
function s8cprobe2() {
  var L = ['=== STAGE 8 · s8cprobe2 · ' + S8V.VER + ' ==='];
  try {
    var gid = SpreadsheetApp.openById(S8.SSID).getSheetByName(S8.SH).getSheetId();
    var resp = Sheets.Spreadsheets.get(S8.SSID, { fields: 'sheets(properties.sheetId,conditionalFormats)' });
    var cf = null, i;
    for (i = 0; i < resp.sheets.length; i++) if (resp.sheets[i].properties.sheetId === gid) cf = resp.sheets[i].conditionalFormats || [];
    var g = r75get_('S8_CLONE_RB');
    var k0 = g.ins[0][0], n = g.ins[0][1];
    L.push('источник #' + k0 + ' + копий ' + n);
    for (var j = 0; j <= Math.min(n, 3); j++) {
      var r = cf[k0 + j];
      if (!r) { L.push('#' + (k0 + j) + ' нет'); continue; }
      var rg = r.ranges[0];
      var f = '';
      if (r.booleanRule && r.booleanRule.condition && r.booleanRule.condition.values)
        f = String(r.booleanRule.condition.values[0].userEnteredValue || '');
      L.push('#' + (k0 + j) + ' диап.' + r.ranges.length + ' первый ' + r75col_(rg.startColumnIndex + 1) + (rg.startRowIndex + 1) +
        '..' + r75col_(rg.endColumnIndex) + rg.endRowIndex + ' | ' + s8sp_(f));
    }
  } catch (e) { L.push('ОШИБКА: ' + e); }
  s8out_(L);
}


/** Отпечаток покрытия УФ ПО ЭФФЕКТУ (см. s8ccfqa) — для итогового s8cvqa. */
function s8ccf2_(gid, bl) {
  var resp = Sheets.Spreadsheets.get(S8.SSID, { fields: 'sheets(properties.sheetId,conditionalFormats)' });
  var cf = null, i;
  for (i = 0; i < resp.sheets.length; i++) if (resp.sheets[i].properties.sheetId === gid) cf = resp.sheets[i].conditionalFormats || [];
  var R1 = S8.TOP - 1, R2 = S8.PLAN, sig = [];
  for (var b = 0; b < bl.length; b++) sig.push([]);
  for (var k = 0; k < cf.length; k++) {
    var rgs = cf[k].ranges || [], hit = false, g;
    for (g = 0; g < rgs.length; g++) {
      var s1 = rgs[g].startRowIndex === undefined ? 0 : rgs[g].startRowIndex;
      var e1 = rgs[g].endRowIndex === undefined ? R2 : rgs[g].endRowIndex;
      if (!(e1 <= R1 || s1 >= R2)) { hit = true; break; }
    }
    if (!hit) continue;
    var fp = s8cfp_(cf[k]);
    for (g = 0; g < rgs.length; g++) {
      var rg = rgs[g];
      var sr = rg.startRowIndex === undefined ? 0 : rg.startRowIndex;
      var er = rg.endRowIndex === undefined ? R2 : rg.endRowIndex;
      if (er <= R1 || sr >= R2) continue;
      var sc = rg.startColumnIndex, ec = rg.endColumnIndex;
      if (sc === undefined || ec === undefined) continue;
      for (var b2 = 0; b2 < bl.length; b2++) {
        var st = bl[b2].st - 1;
        if (ec <= st || sc >= st + S8.BW) continue;
        sig[b2].push(fp + '@' + (Math.max(sc, st) - st) + ':' + (Math.min(ec, st + S8.BW) - st) + ':' + sr + ':' + er);
      }
    }
  }
  var out = [];
  for (var b3 = 0; b3 < bl.length; b3++) {
    var seen = {}, u = [];
    for (i = 0; i < sig[b3].length; i++) if (!seen[sig[b3][i]]) { seen[sig[b3][i]] = 1; u.push(sig[b3][i]); }
    u.sort(); out.push(u.join('|'));
  }
  return out;
}
