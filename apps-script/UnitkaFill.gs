/**
 * UNITKA 2.0 — детерминированный слой записи в книгу «Юнитка_Evetis Cosmetics».
 * Agent: claude-opus-5 · версия unitka2.0/v2.0.4
 *
 * Принципы:
 *   - все функции идемпотентны (повторный запуск не портит данные);
 *   - ячейка с формулой НИКОГДА не перезаписывается значением;
 *   - каждая запись логируется в лист ZZ_AUDIT_LOG;
 *   - автономной записи нет: только ручной запуск.
 *
 * Порядок первого прогона: buildSeptember() -> fillSeptDatesAndClear() -> fillAuto() -> buildQA()
 */

var SSID  = '1E4L4JuwfEqr9owhsGkAjb8F24lRpWpWkEyVmSuRxaJg';
var SH    = 'WB_Юнит_2025';
var AUDIT = 'ZZ_AUDIT_LOG';
var QA    = 'ZZ_QA_СЕНТЯБРЬ_2026';
var VER   = 'unitka2.0/v2.0.4';

// --- геометрия блоков (обнаружена в исходнике, не задана произвольно) ---
var AUG_HDR = 700, AUG_COLHDR = 701, AUG_FIRST = 702, AUG_LAST = 732, AUG_TOTAL = 733, AUG_HELPER = 734;
var SEP_HDR = 735, SEP_COLHDR = 736, SEP_FIRST = 737, SEP_LAST = 766, SEP_TOTAL = 767, SEP_HELPER = 768;
var BLOCK0  = 13, BLOCK_W = 24;              // M = первый SKU-блок, ширина 24 колонки
var CLEAR_OFFSETS = [1,2,3,4,5,6,11,14,15];  // N,O,P,Q,R,S,X,AA,AB — константы месяца
var DOW = ['вс','пн','вт','ср','чт','пт','сб'];

var SRC_ADS   = 'project-fa311fc0-4d87-4781-986.wb_mart.MART_SKU_DAILY';
var SRC_ORD   = 'project-fa311fc0-4d87-4781-986.wb_mart.FACT_ORDERS';
var SRC_STK   = 'project-fa311fc0-4d87-4781-986.wb_mart.FACT_STOCKS_SNAPSHOT';

/**
 * 🔑 СЕМАНТИКА ЗАКАЗОВ — не перепутать (найдено владельцем 10.09.2026):
 *   MART_SKU_DAILY.orders_qty  = ЧИСТЫЕ заказы, отмена уже вычтена. В книгу НЕ брать.
 *   Q «Заказы факт»            = FACT_ORDERS: SUM(quantity)                       — ВАЛОВЫЕ
 *   S «Отменили товаров»       = FACT_ORDERS: SUM(IF(is_cancel, quantity, 0))
 * Иначе отмена учитывается дважды.
 *
 * ⚠️ Отмены приходят задним числом (до ~3 недель). Значит Q и S нельзя записать
 *    один раз: fill_unitka обязан перезаполнять скользящее окно назад.
 */
var REFILL_WINDOW_DAYS = 21;

function L_(n){ var s=''; while(n>0){ var m=(n-1)%26; s=String.fromCharCode(65+m)+s; n=(n-1-m)/26; } return s; }
function ymd_(d){ return Utilities.formatDate(d, 'Europe/Moscow', 'yyyy-MM-dd'); }

/**
 * R7.5: день недели — ТОЛЬКО формулой от ячейки даты, никогда статическим значением.
 * Статика при копировании месяца тянет календарь прошлого месяца (так колонка L в сентябре
 * показывала дни недели августа).
 */
function fillWeekdayFormula_(dateCol, row) {
  return '=IF($' + dateCol + row + '="";"";CHOOSE(WEEKDAY($' + dateCol + row + ';2);"пн";"вт";"ср";"чт";"пт";"сб";"вс"))';
}

/** Колонки-начала SKU-блоков: там, где в строке заголовков стоит «Дата». */
function blockStarts_(s, lastCol) {
  var hdr = s.getRange(SEP_COLHDR, 1, 1, lastCol).getValues()[0], out = [];
  for (var c = BLOCK0; c <= lastCol; c += BLOCK_W) if (String(hdr[c-1]).trim() === 'Дата') out.push(c);
  return out;
}

/** PHASE 2, шаг 1: скопировать АРХИТЕКТУРУ августа в сентябрь (без значений). */
function buildSeptember() {
  var s = SpreadsheetApp.openById(SSID).getSheetByName(SH);
  var lastCol = s.getLastColumn(), log = [];
  if (String(s.getRange(SEP_HDR,1).getValue()).indexOf('Сентябрь 2026') === 0) {
    log.push('ИДЕМПОТЕНТНОСТЬ: блок уже построен, копирование пропущено');
  } else {
    if (s.getMaxRows() < SEP_HELPER) s.insertRowsAfter(s.getMaxRows(), SEP_HELPER - s.getMaxRows());
    s.getRange(AUG_HDR,   1,  2, lastCol).copyTo(s.getRange(SEP_HDR,   1,  2, lastCol));  // +35
    s.getRange(AUG_FIRST, 1, 30, lastCol).copyTo(s.getRange(SEP_FIRST, 1, 30, lastCol));  // +35
    s.getRange(AUG_TOTAL, 1,  2, lastCol).copyTo(s.getRange(SEP_TOTAL, 1,  2, lastCol));  // +34
    s.getRange(SEP_HDR, 1).setValue('Сентябрь 2026');
  }
  // итоговая строка: диапазоны -> 737:766
  var fixed = 0, tf = s.getRange(SEP_TOTAL, 1, 1, lastCol).getFormulas()[0];
  for (var c = 0; c < tf.length; c++) {
    if (!tf[c]) continue;
    var nf = tf[c].replace(/([A-Z]{1,3})(\d+):([A-Z]{1,3})(\d+)/g, function(_, c1, r1, c2, r2) {
      if (Number(r1) >= 730 && Number(r2) <= 770) { fixed++; return c1 + SEP_FIRST + ':' + c2 + SEP_LAST; }
      return _;
    });
    if (nf !== tf[c]) s.getRange(SEP_TOTAL, c+1).setFormula(nf);
  }
  log.push('ИТОГОВАЯ СТРОКА: диапазонов поправлено ' + fixed);
  Logger.log(log.join('\n'));
}

/** PHASE 2, шаг 2: даты 01–30.09 во всех блоках + очистка константных значений августа. */
function fillSeptDatesAndClear() {
  var s = SpreadsheetApp.openById(SSID).getSheetByName(SH);
  var lastCol = s.getLastColumn(), starts = blockStarts_(s, lastCol);
  var F = s.getRange(SEP_FIRST, 1, 30, lastCol).getFormulas();
  var V = s.getRange(SEP_FIRST, 1, 30, lastCol).getValues();
  var dates = 0, cleared = 0, isWk = { 0: true, 11: true };
  for (var y = 0; y < 30; y++) {
    var d = new Date(2026, 8, y + 1), row = SEP_FIRST + y;
    // R7.5: день недели — только формулой от даты: A ← B, L ← M (дата крема), «+23» ← дата блока.
    V[y][0] = fillWeekdayFormula_('B', row); V[y][11] = fillWeekdayFormula_(L_(BLOCK0), row); V[y][1] = d;
    for (var b = 0; b < starts.length; b++) {
      var c0 = starts[b] - 1;
      V[y][c0] = d; V[y][c0 + 23] = fillWeekdayFormula_(L_(c0 + 1), row); isWk[c0 + 23] = true; dates++;
      for (var k = 0; k < CLEAR_OFFSETS.length; k++) {
        var cc = c0 + CLEAR_OFFSETS[k];
        if (!F[y][cc] && V[y][cc] !== '') { V[y][cc] = ''; cleared++; }
      }
    }
  }
  for (var y2 = 0; y2 < 30; y2++) for (var c2 = 0; c2 < lastCol; c2++) if (F[y2][c2] && !isWk[c2]) V[y2][c2] = F[y2][c2];
  s.getRange(SEP_FIRST, 1, 30, lastCol).setValues(V);
  Logger.log('SKU-блоков: ' + starts.length + ' | дат: ' + dates + ' | значений августа очищено: ' + cleared);
}

function auditSheet_(ss) {
  var a = ss.getSheetByName(AUDIT);
  if (!a) {
    a = ss.insertSheet(AUDIT, ss.getNumSheets());
    a.getRange(1,1,1,13).setValues([['run_id','timestamp','target_date','sheet','sku','cell',
      'old_value','new_value','source_table','source_query','status','error','agent_version']]);
    a.setFrozenRows(1); a.getRange(1,1,1,13).setFontWeight('bold');
  }
  return a;
}

/**
 * PHASE 5 / PHASE 12 — ядро записи.
 * rows: [[ 'YYYY-MM-DD', views, gross_orders, cancels, ad_spend, stock_or_null ], ...]
 * blockCol: колонка начала SKU-блока (13 = крем для рук)
 * Ячейка с формулой заменяется значением ТОЛЬКО если allowFormulaOverride = true
 * (так сделан перевод остатков T на факт — по прямому решению владельца).
 */
function fillAutoCore_(blockCol, sku, rows, allowFormulaOverride) {
  var ss = SpreadsheetApp.openById(SSID), s = ss.getSheetByName(SH);
  var runId = 'run_' + new Date().getTime(), now = new Date();
  // смещение колонки от начала блока, индекс в rows, источник
  var FIELD = [
    [ 2, 1, SRC_ADS, 'views'],
    [ 4, 2, SRC_ORD, 'SUM(quantity) — валовые заказы'],
    [ 6, 3, SRC_ORD, 'SUM(IF(is_cancel,quantity,0))'],
    [11, 4, SRC_ADS, 'ad_spend'],
    [ 7, 5, SRC_STK, 'SUM(quantity) — фактический остаток']
  ];

  var dates = s.getRange(SEP_FIRST, blockCol, 30, 1).getValues(), rowOf = {};
  for (var r = 0; r < 30; r++) if (dates[r][0] instanceof Date) rowOf[ymd_(dates[r][0])] = SEP_FIRST + r;

  var audit = [], written = 0, skipped = 0;
  for (var k = 0; k < rows.length; k++) {
    var day = rows[k][0], row = rowOf[day];
    if (!row) { audit.push([runId,now,day,SH,sku,'-','','','—','','ERROR','дата не найдена в блоке',VER]); continue; }
    for (var f = 0; f < FIELD.length; f++) {
      var newV = rows[k][FIELD[f][1]];
      var col = blockCol + FIELD[f][0], cell = s.getRange(row, col), a1 = L_(col) + row;
      var src = FIELD[f][2], q = FIELD[f][3];
      if (newV === null || newV === undefined) {
        audit.push([runId,now,day,SH,sku,a1,'','',src,q,'SKIPPED','в источнике нет значения за этот день',VER]); skipped++; continue;
      }
      var oldF = cell.getFormula(), oldV = cell.getValue();
      if (oldF && !allowFormulaOverride) {
        audit.push([runId,now,day,SH,sku,a1,oldF,newV,src,q,'SKIPPED','в ячейке формула — не перезаписываем',VER]); skipped++; continue;
      }
      if (oldV === newV && !oldF) { audit.push([runId,now,day,SH,sku,a1,oldV,newV,src,q,'UNCHANGED','',VER]); continue; }
      cell.setValue(newV);
      audit.push([runId,now,day,SH,sku,a1,(oldF?('ФОРМУЛА '+oldF):(oldV===''?'(пусто)':oldV)),newV,src,q,'OK',(oldF?'формула заменена фактом по решению владельца':''),VER]);
      written++;
    }
  }
  var a = auditSheet_(ss);
  if (audit.length) a.getRange(a.getLastRow()+1, 1, audit.length, 13).setValues(audit);
  Logger.log('Записано: ' + written + ' | пропущено: ' + skipped + ' | строк аудита: ' + audit.length);
  return { runId: runId, written: written, skipped: skipped };
}
