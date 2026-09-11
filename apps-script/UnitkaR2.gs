/**
 * UNITKA 2.0 — R2 (сентябрьский пилот «Крем для рук»). Agent: claude-opus-5, v2.1.2.
 * Все функции идемпотентны. Ячейка с формулой не перезаписывается значением,
 * кроме явно согласованных случаев (остатки T → факт).
 *
 * 🔑 ГЛАВНАЯ ГРАБЛЯ, из-за которой календарь уехал на сутки:
 *    часовой пояс КНИГИ (America/Los_Angeles) не совпадает с часовым поясом СКРИПТА
 *    (Europe/Moscow). setValue(new Date(2026,8,1)) пишет момент времени; книга рисует
 *    его в своём поясе и показывает 31.08. Скрипт при обратном чтении видит 01.09 —
 *    расхождение невидимо изнутри кода.
 *    ЛЕЧЕНИЕ: писать дату ЧИСЛОМ-СЕРИЕЙ (46266 = 01.09.2026) + числовой формат.
 *    Серия не зависит от пояса вообще.
 */
var SSID = '1E4L4JuwfEqr9owhsGkAjb8F24lRpWpWkEyVmSuRxaJg';
var SH   = 'WB_Юнит_2025';
var HDR = 736, FIRST = 737, LAST = 766, TOTAL = 767, PLAN = 768;
var C0 = 13, BLOCK_W = 24;          // M — первый SKU-блок, ширина 24
var SER_2026_09_01 = 46266;         // серия Google Sheets для 01.09.2026
var DOW = ['вс','пн','вт','ср','чт','пт','сб'];

function L_(n){ var s=''; while(n>0){ var m=(n-1)%26; s=String.fromCharCode(65+m)+s; n=(n-1-m)/26; } return s; }

/** Колонки-начала SKU-блоков: там, где в строке заголовков стоит «Дата». */
function blockStarts_(s, lastCol) {
  var hr = s.getRange(HDR, 1, 1, lastCol).getValues()[0], out = [];
  for (var c = C0; c <= lastCol; c += BLOCK_W) if (String(hr[c-1]).trim() === 'Дата') out.push(c);
  return out;
}

/** R2 §1 — календарь сентября числом-серией. Идемпотентно. */
function fixSeptemberCalendar() {
  var s = SpreadsheetApp.openById(SSID).getSheetByName(SH);
  if (s.getRange(FIRST, C0).getDisplayValue().indexOf('01.09') === 0) {
    Logger.log('Календарь уже верный — пропущено'); return;
  }
  var lastCol = s.getLastColumn(), ser = [], dow = [];
  for (var i = 0; i < 30; i++) {
    ser.push([SER_2026_09_01 + i]);
    // день недели считаем в UTC: локальный getDay() зависел бы от пояса скрипта
    dow.push([DOW[new Date(Date.UTC(2026, 8, i + 1)).getUTCDay()]]);
  }
  s.getRange(FIRST, 2, 30, 1).setValues(ser).setNumberFormat('dd.MM.yy');  // колонка B
  s.getRange(FIRST, 1, 30, 1).setValues(dow);                              // колонка A
  var st = blockStarts_(s, lastCol);
  for (var b = 0; b < st.length; b++) {
    s.getRange(FIRST, st[b], 30, 1).setValues(ser).setNumberFormat('dd.MM.yy');
    s.getRange(FIRST, st[b] + 23, 30, 1).setValues(dow);
  }
  Logger.log('Календарь переписан серией в ' + st.length + ' блоках');
}

/**
 * R2 §13 — итоги месяца не должны ссылаться на строку заголовков.
 * Дефект возник при построении блока: смещение строк данных (+35) и итоговой
 * строки (+34) разное, потому что в сентябре 30 дней, а в августе 31.
 * `=AF736*Q767` = текст × число = #VALUE!.
 */
function fixMonthTotals() {
  var s = SpreadsheetApp.openById(SSID).getSheetByName(SH);
  var lastCol = s.getLastColumn(), st = blockStarts_(s, lastCol);
  var tf = s.getRange(TOTAL, 1, 1, lastCol).getFormulas()[0], fixed = 0;
  for (var b = 0; b < st.length; b++) {
    var af = st[b] + 19, q = st[b] + 4;   // AF — логистика, Q — заказы
    // Сумма месяца = Σ(ставка дня × заказы дня). При постоянной ставке совпадает
    // с прежней логикой, при переменной — корректна, и заголовок не участвует.
    var nf = '=SUMPRODUCT(' + L_(af) + FIRST + ':' + L_(af) + LAST + ';' +
                              L_(q)  + FIRST + ':' + L_(q)  + LAST + ')';
    if ((tf[af-1] || '') !== nf) { s.getRange(TOTAL, af).setFormula(nf); fixed++; }
  }
  var left = [];
  var tf2 = s.getRange(TOTAL, 1, 1, lastCol).getFormulas()[0];
  for (var c = 0; c < lastCol; c++) {
    if (tf2[c] && new RegExp('[A-Z]' + HDR + '(?![0-9])').test(tf2[c])) left.push(L_(c+1) + TOTAL);
  }
  Logger.log('Итогов исправлено: ' + fixed + '. Ссылки на заголовки остались: ' + (left.join(',') || 'нет'));
}

/**
 * R2 §5 — оборачиваемость: целые дни и зоны риска.
 * Формулу не трогаем: =IF(Заказы=0;"";Остатки/Заказы) уже отдаёт пусто при нуле,
 * поэтому #DIV/0! не появляется по построению.
 */
function turnoverZones() {
  var s = SpreadsheetApp.openById(SSID).getSheetByName(SH);
  var U = L_(C0 + 8), R = U + FIRST + ':' + U + LAST;
  s.getRange(FIRST, C0 + 8, 30, 1).setNumberFormat('#,##0');
  s.getRange(TOTAL, C0 + 8).setNumberFormat('#,##0');
  var rules = s.getConditionalFormatRules(), keep = [];
  for (var i = 0; i < rules.length; i++) {
    var rr = rules[i].getRanges(), own = false;
    for (var j = 0; j < rr.length; j++) if (rr[j].getA1Notation() === R) own = true;
    if (!own) keep.push(rules[i]);
  }
  var mk = SpreadsheetApp.newConditionalFormatRule;
  keep.push(mk().whenFormulaSatisfied('=AND(' + U + FIRST + '<>"";' + U + FIRST + '<=15)')
    .setBackground('#f4c7c3').setFontColor('#a61c00').setRanges([s.getRange(R)]).build());
  keep.push(mk().whenFormulaSatisfied('=AND(' + U + FIRST + '>15;' + U + FIRST + '<=30)')
    .setBackground('#ffe599').setRanges([s.getRange(R)]).build());
  keep.push(mk().whenFormulaSatisfied('=AND(' + U + FIRST + '>30;' + U + FIRST + '<=60)')
    .setBackground('#b7e1cd').setRanges([s.getRange(R)]).build());
  keep.push(mk().whenFormulaSatisfied('=AND(' + U + FIRST + '<>"";' + U + FIRST + '>60)')
    .setBackground('#cfe2f3').setFontColor('#0b5394').setRanges([s.getRange(R)]).build());
  s.setConditionalFormatRules(keep);
}
