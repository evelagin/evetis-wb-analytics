/**
 * EVETIS OPERATIONS · C2 — чтение и запись листа.
 *
 * Пишет только в сгенерированные ячейки блоков, найденных по маркерам системной колонки. Ширины колонок,
 * закрепление, заметки и объединения шапок, скрытость листов и оформление колонки владельца не меняются.
 * Число строк меняется только внутри блоков kind='rows': новые строки берут формат и высоту первой строки блока.
 */

/** Колонки с оформлением, зависящим от данных (цвет статуса, выделение рекомендации). */
var OPS_STYLE_COLS = { WB_SHIP: [2, 7], OZON_SHIP: [2, 7], CALC_WB: [19, 23], CALC_OZON: [19, 23], CALC_BUNDLES: [13],
  REFRESH_STATUS: [3] };

var OPS_WRITE_ORDER = ['01_SUPPLY_PLAN', '02_SHIPMENTS', '03_FF_STOCK', '04_SETTINGS', '05_РАСЧЁТ', 'DATA'];

function opsSheet_(ss, name) {
  var sh = ss.getSheetByName(name);
  if (!sh) throw OpsError_('LAYOUT', 'нет листа ' + name);
  return sh;
}

function opsSpecFor_(name) {
  return name === OPS_SHEET.DATA ? opsDataLayout_() : OPS_LAYOUT[name];
}

function opsBlockSpec_(spec, id) {
  for (var i = 0; i < spec.blocks.length; i++) if (spec.blocks[i].id === id) return spec.blocks[i];
  throw OpsError_('INTERNAL', 'нет блока ' + id);
}

function opsNowText_() {
  return Utilities.formatDate(new Date(), OPS.TZ, 'yyyy-MM-dd HH:mm:ss');
}

function opsReadMarkers_(sheet, sysCol) {
  var last = sheet.getLastRow();
  if (last < 1 || sheet.getMaxColumns() < sysCol) return [];
  return sheet.getRange(1, sysCol, last, 1).getValues().map(function (r) { return r[0]; });
}

function opsParseSheetLayout_(sheet, spec, opts) {
  return opsParseLayout_(opsReadMarkers_(sheet, spec.sysCol), spec, opts);
}

/** Контракт раскладки всех листов — до первой записи. */
function opsValidateLayouts_(ss, opts) {
  var out = {};
  OPS_WRITE_ORDER.forEach(function (name) {
    out[name] = opsParseSheetLayout_(opsSheet_(ss, name), opsSpecFor_(name), opts);
  });
  return out;
}

/** Значение для setValues: текст, похожий на число/дату/формулу/логическое, остаётся текстом. */
function opsSheetValue_(x, tz) {
  if (x === null || x === undefined) return '';
  // у книги, импортированной из .xlsx, часовой пояс может быть пустым — тогда часовой пояс скрипта
  if (typeof x === 'object' && x.date) return Utilities.parseDate(String(x.date).slice(0, 10), tz || OPS.TZ, 'yyyy-MM-dd');
  if (typeof x === 'string') {
    if (x === '') return '';
    if (/^[=+\-@']/.test(x) || /^[\s\d.,:\/%()eE+\-]+$/.test(x) || /^(true|false|истина|ложь)$/i.test(x)) return "'" + x;
  }
  return x;
}

function opsEnsureColumns_(sheet, n) {
  var have = sheet.getMaxColumns();
  if (have < n) sheet.insertColumnsAfter(have, n - have);
}

function opsDataLastCol_(render) {
  var w = 2;
  Object.keys(render).forEach(function (id) { if (render[id].header) w = Math.max(w, render[id].header.length + 1); });
  return w;
}

/** Меняет число строк блока до need; сразу пишет маркеры строк, чтобы контракт оставался целым. */
function opsResizeBlock_(sheet, spec, b, blk, need, fmtCol) {
  var have = blk.lastRow - blk.firstRow + 1;
  if (need > have) {
    var add = need - have, start = blk.lastRow + 1;
    sheet.insertRowsAfter(blk.lastRow, add);
    sheet.getRange(blk.firstRow, 1, 1, fmtCol)
      .copyTo(sheet.getRange(start, 1, add, fmtCol), SpreadsheetApp.CopyPasteType.PASTE_FORMAT, false);
    sheet.setRowHeights(start, add, sheet.getRowHeight(blk.firstRow));
    (b.merges || []).forEach(function (m) {
      for (var r = start; r < start + add; r++) {
        var rg = sheet.getRange(r, m[0], 1, m[1] - m[0] + 1);
        if (!rg.isPartOfMerge()) rg.merge();
      }
    });
  } else if (need < have) {
    sheet.deleteRows(blk.firstRow + need, have - need);
  }
  var marks = [];
  for (var i = 0; i < need; i++) marks.push([opsMarker_('R', b.id, '')]);
  sheet.getRange(blk.firstRow, spec.sysCol, need, 1).setValues(marks);
}

/** Пишет один лист по отрисовке. Возвращает число записанных строк. */
function opsWriteSheet_(ss, name, render, opts) {
  var sheet = opsSheet_(ss, name), spec = opsSpecFor_(name);
  var lastCol = spec.dynamicGrid ? opsDataLastCol_(render) : spec.grid;
  if (spec.dynamicGrid) opsEnsureColumns_(sheet, lastCol);
  var fmtCol = spec.dynamicGrid ? lastCol : Math.max(spec.grid, spec.sysCol);
  var model = opsParseSheetLayout_(sheet, spec, { recovery: !!(opts && opts.recovery) });

  var rowBlocks = spec.blocks.filter(function (b) { return b.kind === 'rows'; });
  rowBlocks.sort(function (a, b) { return model.blocks[b.id].firstRow - model.blocks[a.id].firstRow; });   // снизу вверх
  rowBlocks.forEach(function (b) {
    var r = render[b.id];
    if (!r) throw OpsError_('INTERNAL', 'нет отрисовки ' + name + '/' + b.id);
    opsResizeBlock_(sheet, spec, b, model.blocks[b.id], Math.max(1, (r.rows || []).length), fmtCol);
  });
  SpreadsheetApp.flush();
  model = opsParseSheetLayout_(sheet, spec, {});

  var written = 0;
  spec.blocks.forEach(function (b) {
    var segments = spec.dynamicGrid ? [[2, lastCol]] : b.segments;
    written += opsWriteBlock_(sheet, spec, b, model.blocks[b.id], render[b.id], segments);
    if (typeof opsTestHook_ === 'function') opsTestHook_('afterBlock', { sheet: name, block: b.id });
  });
  return written;
}

function opsWriteBlock_(sheet, spec, b, blk, r, segments) {
  if (!r) throw OpsError_('INTERNAL', 'нет отрисовки блока ' + b.id);
  var tz = sheet.getParent().getSpreadsheetTimeZone();
  var textCol = spec.sysCol === 1 ? 2 : 1;

  if (b.kind === 'value') {
    var vc = sheet.getRange(blk.valueRow, textCol);
    vc.setValue(opsSheetValue_(r.value.text, tz));
    if (r.value.fg) vc.setFontColor(r.value.fg);
    return 1;
  }
  if (b.band && r.band) {
    var bc = sheet.getRange(blk.bandRow, textCol);
    bc.setValue(opsSheetValue_(r.band.text, tz));
    if (r.band.note !== undefined) bc.setNote(r.band.note);
  }
  if (r.header) {
    var hdr = [];
    for (var hc = segments[0][0]; hc <= segments[0][1]; hc++) hdr.push(r.header[hc - 2] === undefined ? '' : r.header[hc - 2]);
    sheet.getRange(blk.headerRow, segments[0][0], 1, hdr.length).setValues([hdr])
      .setFontWeight('bold').setFontColor(OPS_COLOR.WHITE).setBackground(OPS_COLOR.DARK);
  }

  var rows = r.rows || [], n = Math.max(1, rows.length), first = blk.firstRow;
  opsCheck_(blk.lastRow - blk.firstRow + 1 === n, 'INTERNAL', b.id + ': строк в листе ' + (blk.lastRow - blk.firstRow + 1) + ' вместо ' + n);
  if (b.kind === 'fixed') {
    opsCheck_(rows.length === b.keys.length && rows.every(function (x, i) { return x.key === b.keys[i]; }),
      'INTERNAL', b.id + ': отрисовка не совпадает с ключами блока');
  }
  var marks = rows.length ? rows.map(function (x) { return [opsMarker_('R', b.id, x.key)]; }) : [[opsMarker_('E', b.id, null)]];
  sheet.getRange(first, spec.sysCol, n, 1).setValues(marks);

  segments.forEach(function (seg, si) {
    var m = [];
    for (var i = 0; i < n; i++) {
      var line = [];
      for (var c = seg[0]; c <= seg[1]; c++) {
        line.push(rows.length ? opsSheetValue_(rows[i].cells[c], tz) : (si === 0 && c === seg[0] ? OPS_TEXT.EMPTY_ROW : ''));
      }
      m.push(line);
    }
    sheet.getRange(first, seg[0], n, seg[1] - seg[0] + 1).setValues(m);
  });

  if (b.ownerCol) {
    var ov = [], of = [], ow = [], oa = [], on = [], os = [];
    for (var k = 0; k < n; k++) {
      var o = rows.length && rows[k].owner ? rows[k].owner
        : { value: '', fg: OPS_COLOR.DARK, bold: false, align: 'right', size: 10, note: '' };
      ov.push([o.value]); of.push([o.fg]); ow.push([o.bold ? 'bold' : 'normal']);
      oa.push([o.align || 'right']); on.push([o.note || '']); os.push([o.size || 10]);
    }
    var orng = sheet.getRange(first, b.ownerCol, n, 1);
    orng.setValues(ov);
    orng.setFontColors(of);
    orng.setFontWeights(ow);
    orng.setHorizontalAlignments(oa);
    orng.setFontSizes(os);
    orng.setNotes(on);
  }

  opsApplyStyles_(sheet, b, first, rows, segments);

  if (b.total) {
    var tot = r.total || { cells: { 1: 'ИТОГО' } };
    segments.forEach(function (seg) {
      var line = [];
      for (var c = seg[0]; c <= seg[1]; c++) line.push(opsSheetValue_(tot.cells[c], tz));
      sheet.getRange(blk.totalRow, seg[0], 1, seg[1] - seg[0] + 1).setValues([line]);
    });
  }
  return n + (b.total ? 1 : 0);
}

function opsApplyStyles_(sheet, b, first, rows, segments) {
  var cols = OPS_STYLE_COLS[b.id];
  if (!cols) return;
  var n = Math.max(1, rows.length), lastC = segments[segments.length - 1][1];
  var rowFg = rows.some(function (x) { return x.rowFg; });
  if (rowFg) {
    sheet.getRange(first, 1, n, lastC).setFontColors(rows.map(function (x) {
      var line = [];
      for (var c = 1; c <= lastC; c++) line.push((x.styles && x.styles[c] && x.styles[c].fg) || x.rowFg || OPS_COLOR.DARK);
      return line;
    }));
  }
  cols.forEach(function (c) {
    var bg = [], fg = [], wt = [];
    for (var i = 0; i < n; i++) {
      var s = rows.length && rows[i].styles && rows[i].styles[c] ? rows[i].styles[c]
        : { bg: OPS_COLOR.WHITE, fg: OPS_COLOR.DARK, bold: false };
      bg.push([s.bg]); fg.push([s.fg]); wt.push([s.bold ? 'bold' : 'normal']);
    }
    var rg = sheet.getRange(first, c, n, 1);
    rg.setBackgrounds(bg);
    if (!rowFg) rg.setFontColors(fg);
    rg.setFontWeights(wt);
  });
}

/** Только статус (строка 2 листа 01 и блок «ОБНОВЛЕНИЕ ДАННЫХ» на 04) — при ошибке данные на экране не трогаются. */
function opsWriteStatusOnly_(ss, status) {
  var specP = OPS_LAYOUT[OPS_SHEET.PLAN], shP = opsSheet_(ss, OPS_SHEET.PLAN);
  var mp = opsParseSheetLayout_(shP, specP, { recovery: true });
  opsWriteBlock_(shP, specP, opsBlockSpec_(specP, 'STATUS_LINE'), mp.blocks.STATUS_LINE, { value: opsStatusLine_(status) }, null);
  var specS = OPS_LAYOUT[OPS_SHEET.SETTINGS], shS = opsSheet_(ss, OPS_SHEET.SETTINGS);
  var ms = opsParseSheetLayout_(shS, specS, { recovery: true }), b = opsBlockSpec_(specS, 'REFRESH_STATUS');
  opsWriteBlock_(shS, specS, b, ms.blocks.REFRESH_STATUS, { rows: opsRenderStatusRows_(status) }, b.segments);
}
