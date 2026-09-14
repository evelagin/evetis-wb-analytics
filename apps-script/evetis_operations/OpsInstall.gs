/**
 * EVETIS OPERATIONS · C2 — одноразовая установка контракта раскладки поверх C1.2 (opsSetup).
 *
 * Только здесь блоки ищутся по видимым текстам C1.2 — один раз, со строгой сверкой всей раскладки.
 * Если лист отличается от C1.2 хоть одной строкой — установка не начинается (ни одной записи).
 * Дальше обновление работает только по маркерам.
 *
 * Что меняет установка (согласовано владельцем, C2 P2/P3/P4/P5):
 *   — маркеры в скрытой защищённой (с предупреждением) системной колонке листов 01–05;
 *   — 04_SETTINGS: блок «ОБНОВЛЕНИЕ ДАННЫХ» (7 строк после строки 3);
 *   — заметки и заголовок со снимочными утверждениями C1.2 заменяются нейтральными (старые — в отчёт установки);
 *   — DATA (скрытый сырой слой): один раз очищается и размечается под V_OPS_SHEET_*, остаётся скрытым;
 *   — _OPS_STATE: новый скрытый защищённый лист состояния решений владельца.
 */

var OPS_C12_STEPS = {
  '01_SUPPLY_PLAN': [
    ['L', /^EVETIS OPERATIONS · План поставок$/],
    ['V', /^ТОЛЬКО ЧТЕНИЕ · /, 'STATUS_LINE'],
    ['S', /^ИТОГО СЕГОДНЯ · ГОТОВОЕ ЗАДАНИЕ USEND$/],
    ['H', /^Что сделать$/, 'USEND_TOP'],
    ['F', 'USEND_TOP', OPS_USEND_KEYS, 'USEND'],
    ['B', /^ОТГРУЗИТЬ WB СЕЙЧАС/, 'WB_SHIP'],
    ['H', /^Товар$/, 'WB_SHIP'],
    ['RS', 'WB_SHIP', /^ОТГРУЗИТЬ OZON СЕЙЧАС/],
    ['B', /^ОТГРУЗИТЬ OZON СЕЙЧАС/, 'OZON_SHIP'],
    ['H', /^Товар$/, 'OZON_SHIP'],
    ['RS', 'OZON_SHIP', /^ПРОВЕРИТЬ \/ НЕ ОТГРУЖАТЬ/],
    ['B', /^ПРОВЕРИТЬ \/ НЕ ОТГРУЖАТЬ/, 'HOLD'],
    ['H', /^Товар$/, 'HOLD'],
    ['RS', 'HOLD', /^ТЗ ДЛЯ ФУЛФИЛМЕНТА/],
    ['S', /^ТЗ ДЛЯ ФУЛФИЛМЕНТА/],
    ['S', /^Считается из плана выше/],
    ['S', /^ГОТОВОЕ ЗАДАНИЕ USEND$/],
    ['H', /^Что сделать$/, 'USEND_TASK'],
    ['F', 'USEND_TASK', OPS_USEND_KEYS, 'USEND'],
    ['S', /^1\. Снять с паллет на полку$/],
    ['H', /^Товар$/, 'TASK_MOVE'],
    ['RS', 'TASK_MOVE', /^ИТОГО$/],
    ['T', /^ИТОГО$/, 'TASK_MOVE'],
    ['S', /^$/],
    ['S', /^2\. Собрать наборы$/],
    ['H', /^Набор$/, 'TASK_BUILD'],
    ['RS', 'TASK_BUILD', /^ИТОГО$/],
    ['T', /^ИТОГО$/, 'TASK_BUILD'],
    ['S', /^$/],
    ['S', /^3\. Расход компонентов/],
    ['H', /^Товар$/, 'TASK_COMP'],
    ['RS', 'TASK_COMP', /^ИТОГО$/],
    ['T', /^ИТОГО$/, 'TASK_COMP'],
    ['END']
  ],
  '02_SHIPMENTS': [
    ['L', /^Отгрузки и поставки$/],
    ['V', /^Данные на /, 'SHIP_SUBTITLE'],
    ['S', /^$/],
    ['B', /^OZON · поставки$/, 'SHIPMENTS'],
    ['H', /^Канал$/, 'SHIPMENTS'],
    ['RS', 'SHIPMENTS', /^ИТОГО$/],
    ['T', /^ИТОГО$/, 'SHIPMENTS'],
    ['S', /^$/],
    ['V', /^Поставок WB/, 'SHIP_WB_LINE'],
    ['END']
  ],
  '03_FF_STOCK': [
    ['L', /^Запас на фулфилменте \(физические единицы\)$/],
    ['V', /^Источник правды — журнал evetis_ops/, 'FF_SUBTITLE'],
    ['V', /^ИТОГО НА ФФ: /, 'FF_TOTAL_LINE'],
    ['S', /^SHIPPED UNCONFIRMED/],
    ['S', /^$/],
    ['S', /^СОСТОЯНИЕ ПО SKU$/],
    ['H', /^Товар$/, 'FF_STOCK'],
    ['RS', 'FF_STOCK', /^ИТОГО$/],
    ['T', /^ИТОГО$/, 'FF_STOCK'],
    ['END']
  ],
  '04_SETTINGS': [
    ['L', /^Настройки и правила/],
    ['S', /^Значения живут в BigQuery/],
    ['S', /^$/],
    ['S', /^ПРАВИЛА РАСЧЁТА/],
    ['F', 'RULES', OPS_RULE_KEYS, 'RULES'],
    ['S', /^$/],
    ['S', /^КАНАЛ И СПОСОБ ОТГРУЗКИ$/],
    ['H', /^Канал$/, 'CHANNELS'],
    ['RS', 'CHANNELS', /^$/],
    ['S', /^$/],
    ['S', /^ПАРАМЕТРЫ C1$/],
    ['H', /^Ключ$/, 'CONFIG'],
    ['RS', 'CONFIG', /^$/],
    ['S', /^$/],
    ['S', /^ЛОГИСТИКА ПО SKU × КАНАЛ$/],
    ['S', /^Заводской короб/],
    ['H', /^SKU$/, 'LOGISTICS'],
    ['RS', 'LOGISTICS', /^$/],
    ['S', /^$/],
    ['S', /^СВЕЖЕСТЬ ДАННЫХ$/],
    ['F', 'FRESHNESS', OPS_FRESH_KEYS, 'FRESH'],
    ['END']
  ],
  '05_РАСЧЁТ': [
    ['L', /^Подробный расчёт \(для проверки\)$/],
    ['S', /^Те же строки/],
    ['S', /^$/],
    ['S', /^ПОДРОБНЫЙ РАСЧЁТ \(для проверки\)$/],
    ['S', /^Полная таблица/],
    ['B', /^WILDBERRIES   ·   /, 'CALC_WB'],
    ['H', /^Товар$/, 'CALC_WB'],
    ['RS', 'CALC_WB', /^ИТОГО$/],
    ['T', /^ИТОГО$/, 'CALC_WB'],
    ['S', /^$/],
    ['B', /^OZON   ·   /, 'CALC_OZON'],
    ['H', /^Товар$/, 'CALC_OZON'],
    ['RS', 'CALC_OZON', /^ИТОГО$/],
    ['T', /^ИТОГО$/, 'CALC_OZON'],
    ['S', /^$/],
    ['S', /^BUNDLE PRODUCTION · сборка наборов$/],
    ['S', /^Мощности наборов не складываются/],
    ['H', /^Набор$/, 'CALC_BUNDLES'],
    ['RS', 'CALC_BUNDLES', /^ИТОГО$/],
    ['T', /^ИТОГО$/, 'CALC_BUNDLES'],
    ['S', /^$/],
    ['S', /^СОБРАТЬ СЕЙЧАС → компоненты/],
    ['H', /^Набор$/, 'CALC_BOM'],
    ['RS', 'CALC_BOM', /^ИТОГО$/],
    ['T', /^ИТОГО$/, 'CALC_BOM'],
    ['S', /^$/],
    ['S', /^PICK FROM STORAGE · снять с паллет$/],
    ['S', /^Итог спроса/],
    ['H', /^Компонент$/, 'CALC_PICK'],
    ['RS', 'CALC_PICK', /^ИТОГО$/],
    ['T', /^ИТОГО$/, 'CALC_PICK'],
    ['END']
  ]
};

function opsFixedLabel_(kind, key) {
  if (kind === 'USEND') return OPS_TEXT.USEND_LABELS[key];
  if (kind === 'FRESH') return OPS_TEXT.FRESH_LABELS[key];
  return new RegExp('^' + key + '\\. ');   // RULES
}

/** Чистая функция: тексты колонки A листа C1.2 → маркеры (последний — @END). Любое расхождение → OpsError_. */
function opsDiscoverC12_(name, colA) {
  var steps = OPS_C12_STEPS[name];
  opsCheck_(steps, 'LAYOUT', 'нет описания C1.2 для листа ' + name);
  var i = 0, marks = [];
  var txt = function (k) { return k < colA.length ? String(colA[k] === null || colA[k] === undefined ? '' : colA[k]).trim() : null; };
  steps.forEach(function (st) {
    var type = st[0];
    if (type === 'END') {
      opsCheck_(i === colA.length, 'LAYOUT', name + ': после строки ' + i + ' есть строки вне C1.2');
      marks.push('@END');
      return;
    }
    if (type === 'F') {
      st[2].forEach(function (key) {
        var t = txt(i), want = opsFixedLabel_(st[3], key);
        opsCheck_(t !== null && (want instanceof RegExp ? want.test(t) : t === want), 'LAYOUT',
          name + ': строка ' + (i + 1) + ' «' + t + '» — ожидалась строка ' + st[1] + ':' + key);
        marks.push(opsMarker_('R', st[1], key));
        i++;
      });
      return;
    }
    if (type === 'RS') {
      var n0 = i;
      while (i < colA.length && !st[2].test(txt(i))) {
        opsCheck_(txt(i) !== '', 'LAYOUT', name + ': пустая строка ' + (i + 1) + ' внутри блока ' + st[1]);
        marks.push(opsMarker_('R', st[1], ''));
        i++;
      }
      opsCheck_(i > n0, 'LAYOUT', name + ': блок ' + st[1] + ' пуст');
      return;
    }
    var t = txt(i);
    opsCheck_(t !== null && st[1].test(t), 'LAYOUT', name + ': строка ' + (i + 1) + ' «' + t + '» не соответствует C1.2 (' +
      type + (st[2] ? ':' + st[2] : '') + ')');
    marks.push(type === 'L' ? '@L:' + OPS.LAYOUT_VERSION : type === 'S' ? '@S' : opsMarker_(type, st[2], null));
    i++;
  });
  return marks;
}

function opsIsLayoutInstalled_(ss) {
  var sh = ss.getSheetByName(OPS_SHEET.PLAN), col = OPS_LAYOUT[OPS_SHEET.PLAN].sysCol;
  return !!sh && sh.getMaxColumns() >= col && String(sh.getRange(1, col).getValue()) === '@L:' + OPS.LAYOUT_VERSION;
}

function opsProtectSysCol_(sh, col) {
  sh.hideColumns(col);
  var p = sh.getRange(1, col, sh.getMaxRows(), 1).protect();
  p.setDescription('C2: служебная колонка раскладки EVETIS OPERATIONS — не редактировать');
  p.setWarningOnly(true);
}

/** Установка. Сначала полная проверка всех листов без записи, затем запись. Возвращает отчёт. */
function opsInstallLayout_(ss) {
  var names = [OPS_SHEET.PLAN, OPS_SHEET.SHIP, OPS_SHEET.FF, OPS_SHEET.SETTINGS, OPS_SHEET.CALC];
  var report = { sheets: {}, replaced: [], at: opsNowText_() };
  opsCheck_(!ss.getSheetByName(OPS_SHEET.STATE), 'STATE_SHEET', 'лист _OPS_STATE уже существует');
  opsSheet_(ss, OPS_SHEET.DATA);

  var prep = {};
  names.forEach(function (name) {
    var sh = opsSheet_(ss, name), spec = OPS_LAYOUT[name], last = sh.getLastRow();
    var colA = sh.getRange(1, 1, last, 1).getValues().map(function (r) { return r[0]; });
    var marks = opsDiscoverC12_(name, colA);
    if (sh.getMaxColumns() >= spec.sysCol) {
      var used = sh.getRange(1, spec.sysCol, last, 1).getValues().filter(function (r) { return r[0] !== ''; }).length;
      opsCheck_(used === 0, 'LAYOUT', name + ': служебная колонка ' + spec.sysCol + ' не пуста');
    }
    prep[name] = { sheet: sh, spec: spec, marks: marks, last: last };
  });
  var p = prep[OPS_SHEET.PLAN], d = p.sheet.getRange(1, 4, p.last, 1).getValues();
  p.marks.forEach(function (m, i) {
    if (/^@R:(WB_SHIP|OZON_SHIP):/.test(m)) {
      opsCheck_(d[i][0] === '', 'LAYOUT', '«Одобрено владельцем» в строке ' + (i + 1) +
        ' не пусто — переносить решения по названию товара нельзя; установка остановлена');
    }
  });

  names.forEach(function (name) {
    var q = prep[name], sh = q.sheet, marks = q.marks;
    if (name === OPS_SHEET.SETTINGS) marks = opsInstallStatusBlock_(sh, marks);
    opsEnsureColumns_(sh, q.spec.sysCol);
    sh.getRange(1, q.spec.sysCol, marks.length, 1).setValues(marks.map(function (m) { return [m]; }));
    opsProtectSysCol_(sh, q.spec.sysCol);
    report.sheets[name] = marks.length - 1;
  });
  SpreadsheetApp.flush();
  names.forEach(function (name) { opsParseSheetLayout_(prep[name].sheet, OPS_LAYOUT[name], {}); });   // проверка разметки

  opsInstallTexts_(ss, report);
  opsInstallData_(ss);
  opsInstallStateSheet_(ss);
  PropertiesService.getScriptProperties().setProperty('OPS_LAYOUT_INSTALLED', OPS.LAYOUT_VERSION + ' · ' + report.at);
  return report;
}

/** 04_SETTINGS: блок «ОБНОВЛЕНИЕ ДАННЫХ» после строки 3 (формат — от полосы правил и строк свежести). */
function opsInstallStatusBlock_(sh, marks) {
  var cols = OPS_LAYOUT[OPS_SHEET.SETTINGS].grid, add = 7;
  var rulesBand = 4, freshFirst = marks.indexOf('@R:FRESHNESS:WB_STOCK') + 1;
  opsCheck_(marks[rulesBand - 1] === '@S' && freshFirst > 0, 'LAYOUT', '04_SETTINGS: не найдены полоса правил или строки свежести');
  sh.insertRowsAfter(3, add);
  sh.getRange(rulesBand + add, 1, 1, cols).copyTo(sh.getRange(4, 1, 1, cols), SpreadsheetApp.CopyPasteType.PASTE_FORMAT, false);
  sh.setRowHeight(4, sh.getRowHeight(rulesBand + add));
  var band = sh.getRange(4, 1, 1, cols);
  if (!band.isPartOfMerge()) band.merge();
  sh.getRange(4, 1).setValue('ОБНОВЛЕНИЕ ДАННЫХ');
  sh.getRange(freshFirst + add, 1, 1, cols).copyTo(sh.getRange(5, 1, 5, cols), SpreadsheetApp.CopyPasteType.PASTE_FORMAT, false);
  sh.setRowHeights(5, 5, sh.getRowHeight(freshFirst + add));
  var ins = ['@S'].concat(OPS_STATUS_KEYS.map(function (k) { return opsMarker_('R', 'REFRESH_STATUS', k); }), ['@S']);
  return marks.slice(0, 3).concat(ins, marks.slice(3));
}

/** Заметки и заголовок C1.2 с утверждениями, которые через несколько дней становятся неверными. */
function opsInstallTexts_(ss, report) {
  var T = OPS_TEXT;
  function header(name, id) {
    var sh = opsSheet_(ss, name), m = opsParseSheetLayout_(sh, OPS_LAYOUT[name], {});
    return { sh: sh, row: m.blocks[id].headerRow };
  }
  function note(name, id, col, text) {
    var h = header(name, id), c = h.sh.getRange(h.row, col);
    report.replaced.push({ cell: name + '!' + c.getA1Notation(), old_note: c.getNote() });
    c.setNote(text);
  }
  note(OPS_SHEET.PLAN, 'WB_SHIP', 4, T.OWNER_NOTE);
  note(OPS_SHEET.PLAN, 'OZON_SHIP', 4, T.OWNER_NOTE);
  note(OPS_SHEET.PLAN, 'TASK_MOVE', 4, T.NOTE_TASK_MOVE_RESERVED);
  ['CALC_WB', 'CALC_OZON'].forEach(function (id) {
    note(OPS_SHEET.CALC, id, 9, T.NOTE_DEMAND_UNTIL_ARRIVAL);
    note(OPS_SHEET.CALC, id, 13, T.NOTE_TARGET);
    note(OPS_SHEET.CALC, id, 14, T.NOTE_SAFETY);
    note(OPS_SHEET.CALC, id, 21, T.NOTE_COVER_AFTER);
  });
  note(OPS_SHEET.CALC, 'CALC_BUNDLES', 10, T.NOTE_FBS_RESERVE);
  note(OPS_SHEET.CALC, 'CALC_PICK', 11, T.NOTE_PICK_RESERVED);
  var h = header(OPS_SHEET.SHIP, 'SHIPMENTS'), hc = h.sh.getRange(h.row, 17);
  report.replaced.push({ cell: OPS_SHEET.SHIP + '!' + hc.getA1Notation(), old_value: hc.getValue() });
  hc.setValue(T.SHIP_EVIDENCE_HEADER);
}

/** DATA: одноразово очистить старый сырой дамп C1.2 и разметить блоки V_OPS_SHEET_*. Лист остаётся скрытым. */
function opsInstallData_(ss) {
  var sh = opsSheet_(ss, OPS_SHEET.DATA);
  var lastR = Math.max(sh.getLastRow(), 1), lastC = Math.max(sh.getLastColumn(), 2);
  sh.getRange(1, 1, lastR, lastC).clearContent().clearFormat();
  var rows = [['@L:' + OPS.LAYOUT_VERSION, 'Сырые строки контракта evetis_ops (V_OPS_SHEET_*) — источник чисел этой книги. ' +
    'Лист скрыт; колонка A служебная.'], ['@S', '']];
  var bandRows = [];
  Object.keys(OPS_VIEWS).forEach(function (v) {
    bandRows.push(rows.length + 1);
    rows.push(['@B:D_' + v, v], ['@H:D_' + v, ''], ['@E:D_' + v, OPS_TEXT.EMPTY_ROW], ['@S', ''], ['@S', '']);
  });
  rows.push(['@END', '']);
  sh.getRange(1, 1, rows.length, 2).setValues(rows);
  sh.getRange(1, 2).setFontWeight('bold');
  bandRows.forEach(function (r) { sh.getRange(r, 2).setFontWeight('bold').setFontColor('#4a148c'); });
  opsProtectSysCol_(sh, OPS_DATA_SYS_COL);
}
