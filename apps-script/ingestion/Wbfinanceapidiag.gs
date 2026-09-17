/**
 * ══════════════════════════════════════════════════════════════
 * EVETIS WB — WbFinanceApiDiag.gs  v1.0
 *
 * БЕЗОПАСНЫЙ ТЕСТОВЫЙ РЕЖИМ для Finance API (reportDetailByPeriod).
 * Только чтение/диагностика. НИЧЕГО не пишет в RAW_WB_FINANCE.
 *
 * Назначение: доказать маппинг API на наших данных ДО первого
 * боевого запуска updateWbFinanceData().
 *
 * НЕ ТРОГАЕТ И НЕ МЕНЯЕТ:
 *   - buildMonthlyUnitReportV10(), ЮНИТ_*
 *   - RAW_WB_FINANCE (только читает), RAW_WB_STORAGE, RAW_WB_ADS
 *   - рабочий парсер XLSX
 *   - WbFinanceLoader.gs (только ВЫЗЫВАЕТ его хелперы)
 *
 * ЗАВИСИМОСТИ (из WbFinanceLoader.gs):
 *   getFinanceToken_(), fetchFinanceApiData_(token, from, to),
 *   FINANCE_API_FIELD_MAP_, normalizeFinanceApiRows_(...),
 *   aggregateFinanceSums_(rows, hMap), buildFinanceRawHeaderMap_(),
 *   readFinanceRowsForPeriod_(), getRawFinanceSheet_(),
 *   WB_FINANCE_TOKEN_KEYS_
 * Из проекта: roundTwo_, sec_, normalizeDateKey_.
 *
 * ВАЖНО: пока compareWbFinanceApiVsRawXlsx_2026_05_18_24() НЕ покажет
 * сходимость API с XLSX — updateWbFinanceData() не ставить в ежедневный
 * сценарий; основным режимом остаётся Drive Folder Loader.
 * ══════════════════════════════════════════════════════════════
 */


// ═══════════════════════════════════════
// ЭТАЛОН XLSX за 18–24 мая 2026 (из ТЗ)
// ═══════════════════════════════════════

var FINANCE_XLSX_REFERENCE_2026_05_18_24_ = {
  from: '2026-05-18',
  to:   '2026-05-24',
  sales_qty:           313,
  gross_revenue:       158190.03,
  wb_for_pay:          168174.71,
  wb_logistics:        20697.22,
  wb_storage:          6099.35,
  wb_deductions:       28763.49,
  wb_compensations:    277.40,
  wb_after_expenses:   112892.05
};

/** Поля API, которые обычно несут деньги/количество (для подсветки в аудите). */
var FINANCE_API_MONEY_FIELDS_ = [
  'quantity', 'retail_price', 'retail_amount', 'retail_price_withdisc_rub',
  'ppvz_for_pay', 'ppvz_vw', 'ppvz_vw_nds', 'ppvz_sales_commission', 'ppvz_reward',
  'delivery_rub', 'storage_fee', 'deduction', 'penalty', 'acceptance',
  'additional_payment', 'acquiring_fee', 'rebill_logistic_cost',
  'product_discount_for_report', 'supplier_promo'
];


// ═══════════════════════════════════════
// 1. АУДИТ ОТВЕТА API
// ═══════════════════════════════════════

/**
 * auditWbFinanceApiResponse(dateFrom, dateTo)
 *
 * Только чтение. Тянет отчёт реализации WB через API и подробно
 * логирует структуру: количество записей, ключи первой записи,
 * 5 примеров, числовые поля по первым 5 записям, суммы по всем
 * числовым полям за весь ответ, и классификацию по бизнес-метрикам.
 */
function auditWbFinanceApiResponse(dateFrom, dateTo) {
  var t0 = Date.now();
  console.log('═══════════════════════════════════════════════════');
  console.log('  AUDIT WB FINANCE API: ' + dateFrom + ' — ' + dateTo);
  console.log('  (только чтение, RAW_WB_FINANCE не изменяется)');
  console.log('═══════════════════════════════════════════════════');

  // ── Токен ──
  var tk = getFinanceToken_();
  if (!tk) {
    var noTokMsg = 'Нет токена WB. Задайте Script Property: ' + WB_FINANCE_TOKEN_KEYS_.join(' или ');
    console.log('❌ ' + noTokMsg);
    SpreadsheetApp.getUi().alert('❌ Нет токена', noTokMsg, SpreadsheetApp.getUi().ButtonSet.OK);
    return null;
  }
  console.log('  Токен: ' + tk.key);

  // ── Запрос ──
  var fetched = fetchFinanceApiData_(tk.token, dateFrom, dateTo);
  if (!fetched.ok) {
    console.log('❌ Ошибка API: ' + fetched.error);
    SpreadsheetApp.getUi().alert('❌ Ошибка API', fetched.error, SpreadsheetApp.getUi().ButtonSet.OK);
    return null;
  }

  var data = fetched.data;
  var n = data.length;
  console.log('');
  console.log('── КОЛИЧЕСТВО ЗАПИСЕЙ: ' + n + ' (страниц: ' + fetched.pages + ') ──');

  if (n === 0) {
    console.log('⚠️ Пустой ответ за период.');
    SpreadsheetApp.getUi().alert('⚠️ Пустой ответ',
      'API вернул 0 записей за ' + dateFrom + ' — ' + dateTo + '.',
      SpreadsheetApp.getUi().ButtonSet.OK);
    return { count: 0 };
  }

  // ── Ключи первой записи ──
  var firstKeys = Object.keys(data[0]);
  console.log('');
  console.log('── КЛЮЧИ ПЕРВОЙ ЗАПИСИ (' + firstKeys.length + ') ──');
  console.log('  ' + firstKeys.join(', '));

  // ── 5 примеров записей ──
  console.log('');
  console.log('── 5 ПРИМЕРОВ ЗАПИСЕЙ ──');
  for (var e = 0; e < Math.min(5, n); e++) {
    var js = '';
    try { js = JSON.stringify(data[e]); } catch (err) { js = '(не сериализуется)'; }
    if (js.length > 1500) js = js.substring(0, 1500) + '…';
    console.log('  [' + (e + 1) + '] ' + js);
  }

  // ── Числовые поля по первым 5 записям ──
  console.log('');
  console.log('── ЧИСЛОВЫЕ ПОЛЯ (первые 5 записей) ──');
  for (var r = 0; r < Math.min(5, n); r++) {
    console.log('  Запись ' + (r + 1) + ':');
    var rec = data[r];
    var rkeys = Object.keys(rec);
    for (var k = 0; k < rkeys.length; k++) {
      var key = rkeys[k];
      var val = rec[key];
      if (isFinanceNumeric_(val) && Number(val) !== 0) {
        console.log('    ' + key + ' = ' + val);
      }
    }
  }

  // ── Суммы по всем числовым полям за весь ответ ──
  var fieldSums = {};
  var fieldCounts = {};
  for (var i = 0; i < n; i++) {
    var item = data[i];
    var ikeys = Object.keys(item);
    for (var j = 0; j < ikeys.length; j++) {
      var fk = ikeys[j];
      var v = item[fk];
      if (isFinanceNumeric_(v)) {
        var num = Number(v);
        if (!fieldSums[fk]) { fieldSums[fk] = 0; fieldCounts[fk] = 0; }
        fieldSums[fk] += num;
        if (num !== 0) fieldCounts[fk]++;
      }
    }
  }

  var sortedKeys = Object.keys(fieldSums).sort(function(a, b) {
    return Math.abs(fieldSums[b]) - Math.abs(fieldSums[a]);
  });

  console.log('');
  console.log('── СУММЫ ПО ВСЕМ ЧИСЛОВЫМ ПОЛЯМ (весь ответ, сорт. по |сумме|) ──');
  for (var s = 0; s < sortedKeys.length; s++) {
    var sk = sortedKeys[s];
    var moneyMark = FINANCE_API_MONEY_FIELDS_.indexOf(sk) >= 0 ? '  💰' : '';
    console.log('  ' + sk + ': сумма=' + roundTwo_(fieldSums[sk]) +
      ', ненулевых=' + fieldCounts[sk] + '/' + n + moneyMark);
  }

  // ── Классификация по бизнес-метрикам ──
  var biz = auditFinanceBizMetricsFromApi_(data);
  console.log('');
  console.log('── ПРОВЕРКА БИЗНЕС-МЕТРИК (по supplier_oper_name / doc_type_name) ──');
  console.log('  Продажи, шт:               ' + biz.sales_qty);
  console.log('  Сумма реализации WB:       ' + roundTwo_(biz.gross_revenue) + '   (retail_amount по продажам)');
  console.log('  К перечислению продавцу:   ' + roundTwo_(biz.wb_for_pay) + '   (Σ ppvz_for_pay)');
  console.log('  Логистика:                 ' + roundTwo_(biz.wb_logistics) + '   (Σ delivery_rub по логистике)');
  console.log('  Хранение:                  ' + roundTwo_(biz.wb_storage) + '   (Σ storage_fee)');
  console.log('  Удержания:                 ' + roundTwo_(biz.wb_deductions) + '   (Σ deduction по удержаниям)');
  console.log('  WB продвижение/реклама:    ' + roundTwo_(biz.wb_promotion));
  console.log('  Транзитные/возмещ.издержек:' + roundTwo_(biz.wb_transit_delivery));
  console.log('  Компенсации:               ' + roundTwo_(biz.wb_compensations));
  console.log('  Эквайринг:                 ' + roundTwo_(biz.acquiring) + '   (Σ acquiring_fee)');
  console.log('  ────────────────────────────');
  console.log('  После расходов WB:         ' + roundTwo_(biz.wb_after_expenses));

  // ── Уникальные supplier_oper_name (помогает увидеть категории) ──
  console.log('');
  console.log('── УНИКАЛЬНЫЕ supplier_oper_name ──');
  var opNames = Object.keys(biz.opCounts).sort();
  for (var o = 0; o < opNames.length; o++) {
    console.log('  "' + opNames[o] + '": ' + biz.opCounts[opNames[o]] + ' строк');
  }

  console.log('');
  console.log('═══ auditWbFinanceApiResponse() ЗАВЕРШЕНО: ' + sec_(t0) + ' сек ═══');

  SpreadsheetApp.getUi().alert('✅ Аудит Finance API завершён',
    'Период: ' + dateFrom + ' — ' + dateTo + '\n' +
    'Записей: ' + n + '\n\n' +
    'Бизнес-метрики (по маппингу):\n' +
    '  Продажи, шт: ' + biz.sales_qty + '\n' +
    '  Реализация: ' + roundTwo_(biz.gross_revenue) + ' ₽\n' +
    '  К перечислению: ' + roundTwo_(biz.wb_for_pay) + ' ₽\n' +
    '  Логистика: ' + roundTwo_(biz.wb_logistics) + ' ₽\n' +
    '  Хранение: ' + roundTwo_(biz.wb_storage) + ' ₽\n' +
    '  Удержания: ' + roundTwo_(biz.wb_deductions) + ' ₽\n' +
    '  Компенсации: ' + roundTwo_(biz.wb_compensations) + ' ₽\n' +
    '  После расходов WB: ' + roundTwo_(biz.wb_after_expenses) + ' ₽\n\n' +
    'Полные детали (ключи, примеры, суммы по полям) — в Журнале выполнения (View → Logs).',
    SpreadsheetApp.getUi().ButtonSet.OK);

  return { count: n, fieldSums: fieldSums, biz: biz };
}

/** true, если значение можно считать числом (не пустое). */
function isFinanceNumeric_(v) {
  if (v === null || v === undefined || v === '') return false;
  if (typeof v === 'number') return !isNaN(v);
  if (typeof v === 'boolean') return false;
  var s = String(v).trim();
  if (s === '') return false;
  return !isNaN(Number(s));
}

/**
 * Считает бизнес-метрики прямо из JSON API (без записи в RAW).
 * Логика классификации совпадает с aggregateFinanceSums_().
 */
function auditFinanceBizMetricsFromApi_(data) {
  function num(item, f) { var v = item[f]; return isFinanceNumeric_(v) ? Number(v) : 0; }

  var salesQty = 0, gross = 0, forPay = 0, log = 0, stor = 0;
  var deduct = 0, promo = 0, transit = 0, comp = 0, acq = 0;
  var opCounts = {};

  for (var i = 0; i < data.length; i++) {
    var item = data[i];
    var dt = String(item.doc_type_name || '').trim();
    var op = String(item.supplier_oper_name || '').trim();
    var opLow = op.toLowerCase();
    opCounts[op || '(пусто)'] = (opCounts[op || '(пусто)'] || 0) + 1;

    var isSale = (dt === 'Продажа' && op === 'Продажа');
    var isLog = opLow.indexOf('логистика') >= 0;
    var isStor = opLow.indexOf('хранение') >= 0;
    var isDeduct = opLow.indexOf('удержан') >= 0;
    var isPromo = opLow.indexOf('продвижен') >= 0 || opLow.indexOf('реклам') >= 0;
    var isLoyalty = opLow.indexOf('компенсация скидки') >= 0 || opLow.indexOf('лояльност') >= 0;
    var isTransit = opLow.indexOf('возмещение издержек') >= 0;
    var isPvz = opLow.indexOf('возмещение за выдачу') >= 0;

    forPay += num(item, 'ppvz_for_pay');
    acq += num(item, 'acquiring_fee');

    if (isSale) {
      salesQty += Math.abs(num(item, 'quantity'));
      gross += Math.abs(num(item, 'retail_amount'));
    }
    if (isLog) log += Math.abs(num(item, 'delivery_rub') || num(item, 'ppvz_for_pay'));
    if (isStor) stor += Math.abs(num(item, 'storage_fee') || num(item, 'ppvz_for_pay'));
    if (isDeduct) deduct += Math.abs(num(item, 'deduction') || num(item, 'ppvz_for_pay'));
    if (isPromo) promo += Math.abs(num(item, 'deduction') || num(item, 'ppvz_for_pay'));
    if (isTransit) transit += (num(item, 'additional_payment') || num(item, 'ppvz_for_pay'));
    if (isLoyalty) comp += (num(item, 'ppvz_for_pay') || num(item, 'additional_payment'));
    if (isPvz) comp += (num(item, 'additional_payment') || num(item, 'ppvz_for_pay'));
  }

  var afterExpenses = forPay - log - stor - deduct - promo + transit + comp;

  return {
    sales_qty: salesQty,
    gross_revenue: gross,
    wb_for_pay: forPay,
    wb_logistics: log,
    wb_storage: stor,
    wb_deductions: deduct,
    wb_promotion: promo,
    wb_transit_delivery: transit,
    wb_compensations: comp,
    acquiring: acq,
    wb_after_expenses: afterExpenses,
    opCounts: opCounts
  };
}


// ═══════════════════════════════════════
// 2. ЗАПУСК АУДИТА БЕЗ ПАРАМЕТРОВ
// ═══════════════════════════════════════

/** Запуск аудита API за 18–24 мая 2026 (без параметров — для меню/IDE). */
function testAuditWbFinanceApi_2026_05_18_24() {
  return auditWbFinanceApiResponse('2026-05-18', '2026-05-24');
}


// ═══════════════════════════════════════
// 3. СРАВНЕНИЕ API vs XLSX (RAW_WB_FINANCE)
// ═══════════════════════════════════════

/**
 * compareWbFinanceApiVsRawXlsx_2026_05_18_24()
 *
 * Сравнивает три источника за 18–24 мая:
 *   1. Эталон XLSX (числа из ТЗ)
 *   2. RAW_WB_FINANCE сейчас (загруженный XLSX)
 *   3. API (нормализованный В ПАМЯТИ через тот же маппинг загрузчика)
 *
 * НИЧЕГО не пишет в RAW_WB_FINANCE.
 * Печатает дельты и выносит вердикт: сходится / не сходится.
 */
function compareWbFinanceApiVsRawXlsx_2026_05_18_24() {
  var t0 = Date.now();
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var ui = SpreadsheetApp.getUi();
  var ref = FINANCE_XLSX_REFERENCE_2026_05_18_24_;
  var dateFrom = ref.from, dateTo = ref.to;

  console.log('═══ compareWbFinanceApiVsRawXlsx_2026_05_18_24() СТАРТ ═══');

  // ── RAW сейчас ──
  var rawSheet = getRawFinanceSheet_(ss);
  if (!rawSheet || rawSheet.getLastRow() < 2) {
    ui.alert('⚠️', 'RAW_WB_FINANCE пуст — не с чем сравнивать XLSX.', ui.ButtonSet.OK);
    return null;
  }
  var rawLastCol = rawSheet.getLastColumn();
  var hMap = buildFinanceRawHeaderMap_(rawSheet, rawLastCol);
  var rawPeriodRows = readFinanceRowsForPeriod_(rawSheet, hMap, dateFrom, dateTo);
  var rawSums = aggregateFinanceSums_(rawPeriodRows, hMap);
  console.log('  RAW_WB_FINANCE строк за период: ' + rawPeriodRows.length);

  // ── API в память (без записи) ──
  var tk = getFinanceToken_();
  if (!tk) {
    ui.alert('❌ Нет токена',
      'Задайте Script Property: ' + WB_FINANCE_TOKEN_KEYS_.join(' или ') +
      '\n\nСравнить можно будет только после добавления токена.',
      ui.ButtonSet.OK);
    return null;
  }
  var fetched = fetchFinanceApiData_(tk.token, dateFrom, dateTo);
  if (!fetched.ok) {
    ui.alert('❌ Ошибка API', fetched.error, ui.ButtonSet.OK);
    return null;
  }
  var apiData = fetched.data;
  console.log('  API записей: ' + apiData.length);

  var apiSums;
  if (apiData.length === 0) {
    apiSums = { sales_qty: 0, gross_revenue: 0, wb_for_pay: 0, wb_logistics: 0,
      wb_storage: 0, wb_deductions: 0, wb_promotion: 0, wb_transit_delivery: 0,
      wb_compensations: 0, wb_after_expenses: 0 };
  } else {
    var loadedAt = Utilities.formatDate(new Date(), 'Europe/Moscow', 'yyyy-MM-dd HH:mm:ss');
    var apiRows = normalizeFinanceApiRows_(
      apiData, hMap, rawLastCol, 'AUDIT_NO_WRITE', loadedAt,
      dateFrom, dateTo, '', null /* skuIndex не нужен для сумм */
    );
    apiSums = aggregateFinanceSums_(apiRows, hMap);
  }

  // ── Таблица сравнения ──
  var metrics = [
    ['Продажи, шт',           'sales_qty',          0.02],   // допуск 2% (целое)
    ['Сумма реализации WB',   'gross_revenue',      0.01],
    ['К перечислению WB',     'wb_for_pay',         0.01],
    ['Логистика WB',          'wb_logistics',       0.01],
    ['Хранение WB',           'wb_storage',         0.01],
    ['Удержания WB',          'wb_deductions',      0.01],
    ['Компенсации WB',        'wb_compensations',   0.05],   // «около» — допуск шире
    ['После расходов WB',     'wb_after_expenses',  0.02]
  ];

  console.log('');
  console.log('── СРАВНЕНИЕ ЗА ' + dateFrom + ' — ' + dateTo + ' ──');
  console.log('  Метрика | Эталон XLSX | RAW сейчас | API | Δ(API−эталон) | %');

  var allCriticalOk = true;
  var rowsForAlert = [];

  for (var m = 0; m < metrics.length; m++) {
    var label = metrics[m][0];
    var key = metrics[m][1];
    var tol = metrics[m][2];

    var refVal = ref[key];
    var rawVal = rawSums[key] !== undefined ? rawSums[key] : 0;
    var apiVal = apiSums[key] !== undefined ? apiSums[key] : 0;

    var delta = roundTwo_(apiVal - refVal);
    var pct = refVal !== 0 ? (delta / Math.abs(refVal)) : (apiVal === 0 ? 0 : 1);
    var ok = Math.abs(pct) <= tol;

    // «Компенсации» помечаем, но не считаем критичной для общего вердикта
    var critical = (key !== 'wb_compensations');
    if (critical && !ok) allCriticalOk = false;

    var mark = ok ? '✅' : '⚠️';
    console.log('  ' + label + ' | ' + refVal + ' | ' + rawVal + ' | ' + apiVal +
      ' | ' + delta + ' | ' + roundTwo_(pct * 100) + '% ' + mark);

    rowsForAlert.push(label + ': эталон ' + refVal + ' / API ' + apiVal +
      ' (Δ ' + delta + ', ' + roundTwo_(pct * 100) + '%) ' + mark);
  }

  var verdict = allCriticalOk
    ? '✅ API-маппинг СХОДИТСЯ с XLSX.\nМожно включать updateWbFinanceData() / API-режим.'
    : '⚠️ API-маппинг НЕ сходится с XLSX.\nОставить основным режимом Drive Folder Loader,\nне ставить updateWbFinanceData() в ежедневный сценарий.\nПроверьте FINANCE_API_FIELD_MAP_ по логам аудита.';

  console.log('');
  console.log(verdict.replace(/\n/g, ' '));
  console.log('═══ compareWbFinanceApiVsRawXlsx_2026_05_18_24() ЗАВЕРШЕНО: ' + sec_(t0) + ' сек ═══');

  ui.alert('📊 Сравнение API ↔ XLSX (18–24 мая)',
    'Записей: RAW=' + rawPeriodRows.length + ', API=' + apiData.length + '\n\n' +
    rowsForAlert.join('\n') + '\n\n' +
    '──────────────────\n' + verdict,
    ui.ButtonSet.OK);

  return { ok: allCriticalOk, refSums: ref, rawSums: rawSums, apiSums: apiSums };
}


// ═══════════════════════════════════════
// МЕНЮ ДИАГНОСТИКИ
// ═══════════════════════════════════════

function addWbFinanceDiagMenu() {
  SpreadsheetApp.getUi().createMenu('🧪 Тест Finance API')
    .addItem('🔍 Аудит API 18–24 мая', 'testAuditWbFinanceApi_2026_05_18_24')
    .addItem('📊 Сравнить API ↔ XLSX 18–24 мая', 'compareWbFinanceApiVsRawXlsx_2026_05_18_24')
    .addToUi();
}