// UNITKA 2.0 R6 — разделение комиссии (BASE + ACQUIRING) и база налогового резерва.
// Только сентябрь, только блок крема для рук (nmID 252442517, первый блок, колонка M).
// Август не трогается. Другие SKU не трогаются. Agent: claude-opus-5, v6.0.1
//
// Публичная функция РОВНО ОДНА — r6run. Остальные с суффиксом _ и в выпадающем
// списке не показываются: выбор функции в редакторе срабатывает ненадёжно,
// и единственная публичная функция снимает риск запустить не то.
//
// Измерено в BigQuery 11.09.2026 по V_WB_FINANCE_CANONICAL, только реализованные
// строки «Продажа» этого SKU. Окно D-30..D-1 при D = LAST_CLOSED_DATE + 1 = 10.09:
//   BASE   42,2506 % — тариф WB, взвешенный по базе продавца (min=max=42,25)
//   ACQ     3,1822 % — SUM(эквайринг ₽) / SUM(база продавца ₽), 212 наблюдений
//   TOTAL  45,4328 %
//
// Результат прогона 11.09.2026: AD 0.4532 -> 0.454328; AH: 30 формул AC -> AA;
// ZZ_CONFIG 8 ключей, confidence HIGH; ошибок в блоке 0; будущих дней 21,
// непустых экономических ячеек в них 0.
var R6_SSID = '1E4L4JuwfEqr9owhsGkAjb8F24lRpWpWkEyVmSuRxaJg';
var R6_SH = 'WB_Юнит_2025';
var R6_BASE = 0.422506;
var R6_ACQ = 0.031822;
var R6_TOTAL = 0.454328;
var R6_WIN_A = '2026-08-11', R6_WIN_B = '2026-09-09';
var R6_N = 212, R6_MIN_N = 30;
var R6_SPP30 = 0.1702;
var R6_VER = 'unitka2.0/v6.0.1';

function r6run() {
  var L = [];
  r6apply_(L);
  r6qa_(L);
  Logger.log(L.join('\n'));
}

function r6cfgSet_(sh, key, val, note) {
  var n = Math.max(sh.getLastRow(), 1);
  var keys = sh.getRange(1, 1, n, 1).getValues();
  var row = 0;
  for (var i = 0; i < n; i++) if (String(keys[i][0]) === key) { row = i + 1; break; }
  if (row === 0) row = n + 1;
  sh.getRange(row, 1, 1, 3).setValues([[key, val, note]]);
}

function r6audit_(ss, cell, oldv, newv, note) {
  var lg = ss.getSheetByName('ZZ_AUDIT_LOG');
  if (!lg) return;
  lg.appendRow(['r6_' + new Date().getTime(), new Date(), '2026-09', R6_SH, '252442517',
    cell, String(oldv), String(newv), 'wb_raw.V_WB_FINANCE_CANONICAL', note, 'OK', '', R6_VER]);
}

function r6apply_(L) {
  var ss = SpreadsheetApp.openById(R6_SSID);
  var sh = ss.getSheetByName(R6_SH);
  L.push('=== R6 APPLY ===');

  // AD — колонка REFERENCE: хранит число, не формулу. Здесь лежит ТОЛЬКО сумма
  // BASE + ACQUIRING; сами компоненты — в ZZ_CONFIG, чтобы их можно было
  // пересчитать и проверить по отдельности.
  var oldAd = sh.getRange(737, 30).getValue();
  var ad = [];
  for (var r = 737; r <= 766; r++) ad.push([R6_TOTAL]);
  sh.getRange(737, 30, 30, 1).setValues(ad);
  L.push('AD737:AD766: ' + oldAd + ' -> ' + R6_TOTAL);
  r6audit_(ss, 'AD737:AD766', oldAd, R6_TOTAL,
    'BASE ' + R6_BASE + ' + ACQ_30D ' + R6_ACQ + ', окно ' + R6_WIN_A + '..' + R6_WIN_B + ', n=' + R6_N);

  // AH — база резерва AC (цена с СПП) -> AA (цена продавца). СПП финансирует WB
  // и экономическую выручку продавца не уменьшает, поэтому базой быть не может.
  // Замена строковая, чтобы обёртка закрытого дня из R4 осталась нетронутой.
  var fh = sh.getRange(737, 34, 30, 1).getFormulas();
  var out = [], changed = 0, sample = '';
  for (var i = 0; i < 30; i++) {
    var f = fh[i][0], rr = 737 + i;
    var nf = f.split('AC' + rr).join('AA' + rr);
    if (nf !== f) { changed++; if (!sample) sample = f + ' -> ' + nf; }
    out.push([nf]);
  }
  sh.getRange(737, 34, 30, 1).setFormulas(out);
  L.push('AH737:AH766: база AC -> AA, заменено ' + changed + ' формул');
  L.push('  пример: ' + sample);
  r6audit_(ss, 'AH737:AH766', 'AC*2%', 'AA*2%',
    'СПП финансирует WB и выручку продавца не уменьшает; резерв управленческий, не УСН');

  var cfg = ss.getSheetByName('ZZ_CONFIG') || ss.insertSheet('ZZ_CONFIG');
  var conf = R6_N >= R6_MIN_N ? 'HIGH' : 'LOW';
  r6cfgSet_(cfg, 'BASE_COMMISSION_RATE', R6_BASE, 'тариф WB, взвеш. по базе продавца');
  r6cfgSet_(cfg, 'ROLLING_EFFECTIVE_ACQUIRING_30D', R6_ACQ, 'SUM(эквайринг) / SUM(база продавца), только nmID 252442517');
  r6cfgSet_(cfg, 'TOTAL_OPERATIONAL_RATE', R6_TOTAL, 'BASE + ACQUIRING; это значение стоит в AD');
  r6cfgSet_(cfg, 'ACQUIRING_WINDOW', R6_WIN_A + '..' + R6_WIN_B, 'D-30..D-1, D = LAST_CLOSED_DATE + 1');
  r6cfgSet_(cfg, 'ACQUIRING_SAMPLE_SIZE', R6_N, 'реализованных наблюдений в окне');
  r6cfgSet_(cfg, 'ACQUIRING_CONFIDENCE', conf, 'порог ' + R6_MIN_N);
  r6cfgSet_(cfg, 'REALIZED_SPP_ROLLING_30D', R6_SPP30, 'ДИАГНОСТИКА: не влияет на комиссию, прибыль, резерв, выручку');
  r6cfgSet_(cfg, 'TAX_RESERVE_BASE', 'AA (цена продавца)', 'управленческий резерв 2 %, НЕ расчёт УСН');
  L.push('ZZ_CONFIG: 8 ключей записано, confidence = ' + conf);
  SpreadsheetApp.flush();
}

function r6qa_(L) {
  var ss = SpreadsheetApp.openById(R6_SSID);
  var sh = ss.getSheetByName(R6_SH);
  L.push('');
  L.push('=== R6 QA ===');
  var vals = sh.getRange(737, 13, 31, 24).getDisplayValues();
  var errs = ['#REF!', '#VALUE!', '#NAME?', '#DIV/0!', '#N/A', '#ERROR!'];
  var cnt = {}, total = 0;
  for (var e = 0; e < errs.length; e++) cnt[errs[e]] = 0;
  for (var i = 0; i < vals.length; i++) for (var j = 0; j < vals[i].length; j++) {
    var v = String(vals[i][j]);
    for (var e = 0; e < errs.length; e++) if (v.indexOf(errs[e]) >= 0) { cnt[errs[e]]++; total++; }
  }
  for (var e = 0; e < errs.length; e++) L.push(errs[e] + ' = ' + cnt[errs[e]]);
  L.push('ВСЕГО ОШИБОК = ' + total);
  var lcd = sh.getRange(736, 600).getValue();
  var dates = sh.getRange(737, 13, 30, 1).getValues();
  var chk = [20, 21, 22, 23, 26, 29, 31, 33, 34, 35];
  var futureRows = 0, dirty = 0;
  for (var i = 0; i < 30; i++) {
    var d = dates[i][0];
    if (d instanceof Date && lcd instanceof Date && d.getTime() > lcd.getTime()) {
      futureRows++;
      for (var k = 0; k < chk.length; k++) if (String(sh.getRange(737 + i, chk[k]).getDisplayValue()) !== '') dirty++;
    }
  }
  L.push('LAST_CLOSED_DATE = ' + lcd);
  L.push('будущих дней ' + futureRows + ', непустых экономических ячеек в них ' + dirty + ' (норма 0)');
  L.push('AD737 = ' + sh.getRange(737, 30).getDisplayValue() + ' | AD767 = ' + sh.getRange(767, 30).getDisplayValue());
  L.push('AH737 = ' + sh.getRange(737, 34).getFormula() + ' -> ' + sh.getRange(737, 34).getDisplayValue());
  L.push('AE737 = ' + sh.getRange(737, 31).getDisplayValue() + ' | AI737 = ' + sh.getRange(737, 35).getDisplayValue());
  L.push('AH767 = ' + sh.getRange(767, 34).getDisplayValue() + ' | AI767 = ' + sh.getRange(767, 35).getDisplayValue());
  L.push('AG767 хранение = ' + sh.getRange(767, 33).getDisplayValue() + ' | AF737 логистика = ' + sh.getRange(737, 32).getDisplayValue());
  L.push('W767 доходность общая = ' + sh.getRange(767, 23).getDisplayValue() + ' | Q767 заказы = ' + sh.getRange(767, 17).getDisplayValue());
  L.push('P737 переходы = [' + sh.getRange(737, 16).getDisplayValue() + '] | R737 корзины = [' + sh.getRange(737, 18).getDisplayValue() + ']  (GAP, воронка без доступа)');
}
