/**
 * ══════════════════════════════════════════════════════════════
 * EVETIS WB — WbWorkbookAudit.gs   (read-only диагностика ёмкости книги)
 *
 * Лимит Google Sheets = 10 000 000 ячеек на КНИГУ, где считается
 * АЛЛОЦИРОВАННАЯ сетка каждого листа (maxRows × maxColumns), включая
 * ПУСТЫЕ зарезервированные ячейки — а не только заполненные. Поэтому
 * лист «на миллион строк × 40 колонок» съедает 40 млн-эквивалент места,
 * даже если данных в нём 3000 строк.
 *
 * Функция ничего не пишет и не удаляет — только console.log. Показывает
 * по каждому листу: сетку (maxR×maxC), использованный диапазон
 * (lastRow×lastCol) и «пустой резерв» (сетка − использовано), отсортировано
 * по величине сетки. Внизу — сумма по книге и остаток до лимита.
 * ══════════════════════════════════════════════════════════════
 */

var WB_SHEETS_CELL_LIMIT_ = 10000000;

/** Read-only аудит ёмкости книги: кто занимает ячейки (сетка), где пустой резерв. */
function wbWorkbookCellAudit() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheets = ss.getSheets();
  var rows = [];
  var totalGrid = 0, totalUsed = 0;

  sheets.forEach(function (sh) {
    var maxR = sh.getMaxRows();
    var maxC = sh.getMaxColumns();
    var usedR = sh.getLastRow();
    var usedC = sh.getLastColumn();
    var grid = maxR * maxC;
    var used = usedR * usedC;
    totalGrid += grid;
    totalUsed += used;
    rows.push({ name: sh.getName(), maxR: maxR, maxC: maxC, usedR: usedR, usedC: usedC,
      grid: grid, waste: grid - used });
  });

  rows.sort(function (a, b) { return b.grid - a.grid; });

  console.log('════════ АУДИТ ЁМКОСТИ КНИГИ ════════');
  console.log('Листов: ' + sheets.length +
    ' | сетка(книга)=' + totalGrid + ' из ' + WB_SHEETS_CELL_LIMIT_ +
    ' (' + (totalGrid * 100 / WB_SHEETS_CELL_LIMIT_).toFixed(1) + '%)' +
    ' | заполнено≈' + totalUsed +
    ' | остаток до лимита=' + (WB_SHEETS_CELL_LIMIT_ - totalGrid));
  console.log('─── по листам (сортировка по сетке) ───');
  rows.forEach(function (r) {
    console.log(r.name + ': сетка ' + r.maxR + '×' + r.maxC + '=' + r.grid +
      ' | использовано ' + r.usedR + '×' + r.usedC +
      ' | пустой резерв=' + r.waste);
  });
  console.log('Подсказка: крупный «пустой резерв» на листе → безопасно ужать сетку ' +
    '(удалить лишние строки/столбцы) без потери данных.');

  return { sheets: sheets.length, totalGrid: totalGrid, totalUsed: totalUsed,
    remaining: WB_SHEETS_CELL_LIMIT_ - totalGrid, rows: rows };
}