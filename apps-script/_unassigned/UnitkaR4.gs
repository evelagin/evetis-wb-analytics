/**
 * UNITKA 2.0 — R4. Сентябрьский блок доведён до мастер-шаблона.
 * Agent: claude-opus-5, v4.2.0. Идемпотентно.
 *
 * 🔑 ТРИ ГРАБЛИ, ради которых стоит читать этот файл:
 *
 * 1. Часовой пояс. Даты пишутся ЧИСЛОМ-СЕРИЕЙ (46266 = 01.09.2026), а не объектом Date:
 *    setValue(new Date(...)) пишет момент времени, и книга рисует его в СВОЁМ поясе.
 *    При книге в America/Los_Angeles и скрипте в Europe/Moscow весь календарь уезжал на сутки.
 *
 * 2. Условное форматирование не умеет ссылаться на другой лист. LAST_CLOSED_DATE живёт
 *    на ZZ_CONFIG, поэтому для CF значение зеркалится в служебную ячейку WB736 этого же листа.
 *
 * 3. SUMPRODUCT на текст даёт #VALUE!. На будущих днях формулы возвращают "" (а не 0),
 *    поэтому средневзвешенные в итоговой строке считаются через FILTER(...; маска закрытых дней).
 */
var SSID = '1E4L4JuwfEqr9owhsGkAjb8F24lRpWpWkEyVmSuRxaJg';
var SH   = 'WB_Юнит_2025';
var HDR = 736, FIRST = 737, LAST = 766, TOTAL = 767, C0 = 13, W = 24;
var MIRROR_COL = 600;                       // служебное зеркало LAST_CLOSED_DATE
var GUARD = [7,8,9,10,12,13,16,18,20,21,22]; // T U V W Y Z AC AE AG AH AI

function L_(n){ var s=''; while(n>0){ var m=(n-1)%26; s=String.fromCharCode(65+m)+s; n=(n-1-m)/26; } return s; }

/**
 * R4 §1 — будущие дни не должны показывать факт.
 * Формулы сохраняются: как только день закроется, они посчитают сами.
 */
function guardFutureDays() {
  var s = SpreadsheetApp.openById(SSID).getSheetByName(SH);
  var lcSerial = Number(s.getRange(HDR, MIRROR_COL).getValue());
  var rng = s.getRange(FIRST, C0, 30, W);
  var F = rng.getFormulas(), V = rng.getValues(), out = [], guarded = 0, cleared = 0, fixedZero = 0;

  for (var y = 0; y < 30; y++) {
    var row = FIRST + y;
    var serial = Number(s.getRange(row, C0).getValue());
    var future = serial > lcSerial;
    var line = [];
    for (var c = 0; c < W; c++) {
      var f = F[y][c], v = V[y][c], isGuard = (GUARD.indexOf(c) >= 0);
      if (!isGuard) { line.push(f ? f : v); continue; }
      if (f) {
        var inner = f.substring(1);
        if (inner.indexOf('IF($' + L_(C0) + row + '>LAST_CLOSED_DATE') === 0) { line.push(f); continue; }
        // Ноль вклада и ноль ДРР при отсутствии заказов — не «ноль рублей», а «нет данных».
        if ((c === 9 || c === 13) && inner.slice(-3) === ';0)') { inner = inner.slice(0, -3) + ';"")'; fixedZero++; }
        line.push('=IF($' + L_(C0) + row + '>LAST_CLOSED_DATE;"";' + inner + ')');
        guarded++;
      } else {
        if (future && v !== '') { line.push(''); cleared++; } else { line.push(v); }
      }
    }
    out.push(line);
  }
  rng.setValues(out);
  Logger.log('под защиту: ' + guarded + ' формул | очищено: ' + cleared + ' | «;0)» -> «;"")»: ' + fixedZero);
}

/** R4 §11 — форматы, выравнивание, рамки. Цвет сообщает исключение, а не украшает. */
function polishVisual() {
  var s = SpreadsheetApp.openById(SSID).getSheetByName(SH);
  var MONEY = [9,10,11,12,14,16,18,19,20,21,22], INT = [1,2,3,4,5,6,7,8,15], PCT = [13,17];
  var rows = [{ r: FIRST, n: 30 }, { r: TOTAL, n: 1 }];
  for (var k = 0; k < rows.length; k++) {
    for (var i = 0; i < MONEY.length; i++) s.getRange(rows[k].r, C0 + MONEY[i], rows[k].n, 1).setNumberFormat('#,##0\\ "₽"').setHorizontalAlignment('right');
    for (var j = 0; j < INT.length; j++)   s.getRange(rows[k].r, C0 + INT[j],   rows[k].n, 1).setNumberFormat('#,##0').setHorizontalAlignment('center');
    for (var p = 0; p < PCT.length; p++)   s.getRange(rows[k].r, C0 + PCT[p],   rows[k].n, 1).setNumberFormat('0.0%').setHorizontalAlignment('center');
  }
  s.getRange(FIRST, C0, 30, 1).setNumberFormat('dd.MM.yy').setHorizontalAlignment('center');
  s.setRowHeight(HDR, 76);
  s.getRange(HDR, C0, 1, W).setVerticalAlignment('middle').setHorizontalAlignment('center').setWrap(true).setFontWeight('bold');
  for (var w = 0; w < W; w++) s.setColumnWidth(C0 + w, w === 23 ? 56 : 84);
  var solid = SpreadsheetApp.BorderStyle.SOLID_MEDIUM;
  s.getRange(735, C0, 34, W).setBorder(true, true, true, true, null, null, '#5f6368', solid);
  s.getRange(735, C0, 1,  W).setBorder(true, true, true, true, null, null, '#5f6368', solid);
  s.getRange(TOTAL, C0, 1, W).setBorder(true, true, true, true, null, null, '#5f6368', solid);
  s.getRange(HDR, C0, 1, W).setBorder(true, true, true, true, true, null, '#9aa0a6', SpreadsheetApp.BorderStyle.SOLID);
  // параметры модели на будущих днях: значение есть, но это не факт дня
  s.getRange(FIRST + 9, C0 + 17, 21, 1).setFontColor('#b7b7b7');
  s.getRange(FIRST + 9, C0 + 19, 21, 1).setFontColor('#b7b7b7');
}

/**
 * R4 §8 — итоговая строка. Каждая колонка получает СВОЙ тип агрегации.
 * Проценты не суммируются, отношения не усредняются по дням.
 */
function rebuildMtdTotals() {
  var s = SpreadsheetApp.openById(SSID).getSheetByName(SH);
  var M = '$' + L_(C0) + '$' + FIRST + ':$' + L_(C0) + '$' + LAST;
  var cond = M + '<=LAST_CLOSED_DATE', Q = L_(C0 + 4);
  function fil(col){ return 'FILTER(' + col + FIRST + ':' + col + LAST + ';' + cond + ')'; }
  function wavg(col){ return '=IFERROR(SUMPRODUCT(' + fil(col) + ';' + fil(Q) + ')/' + Q + TOTAL + ';"")'; }
  function wsum(col){ return '=IFERROR(SUMPRODUCT(' + fil(col) + ';' + fil(Q) + ');"")'; }

  // GAP-колонки: ноль читался бы как «переходов не было». Это неправда — источника нет.
  s.getRange(TOTAL, C0 + 3).clearContent();
  s.getRange(TOTAL, C0 + 5).clearContent();

  s.getRange(TOTAL, C0 + 14).setFormula(wavg('AA'));   // цена — средневзвешенная по заказам
  s.getRange(TOTAL, C0 + 16).setFormula(wavg('AC'));
  s.getRange(TOTAL, C0 + 17).setFormula(wavg('AD'));   // ставка удержаний — не сумма процентов
  s.getRange(TOTAL, C0 + 18).setFormula(wavg('AE'));
  s.getRange(TOTAL, C0 + 21).setFormula(wsum('AH'));   // резерв, справочно: он уже внутри доходности
  s.getRange(TOTAL, C0 + 22).setFormula(wavg('AI'));
}
