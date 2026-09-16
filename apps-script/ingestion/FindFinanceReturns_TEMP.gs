/**
 * ══════════════════════════════════════════════════════════════
 * EVETIS WB — FindFinanceReturns_TEMP.gs  (ВРЕМЕННЫЙ, READ-ONLY)
 *
 * Назначение: перед PR #14B выяснить, есть ли в существующем
 *   RAW_WB_FINANCE строки-возвраты (live-примеры), раз через
 *   /supplier/sales за 2026-04-01 — 2026-06-16 возвратов не нашлось.
 *
 * ЭТОТ ФАЙЛ ВРЕМЕННЫЙ. В Git НЕ коммитить. После ревью удалить.
 *
 * ЧТО ДЕЛАЕТ:
 *   • открывает RAW_WB_FINANCE, читает фактические заголовки
 *   • находит колонки «типа операции» (doc/oper/operation/type/name/предмет/операц)
 *   • ищет строки, где текстовое поле содержит:
 *       возврат / сторно / коррекц / return / refund
 *   • отдельно — «денежный» сигнал: return_amount_rub != 0 или return_count > 0
 *   • считает total / min-max date / группировки / sample до 20 строк
 *   • если возвраты есть → точечные периоды для повторного sales-gate (±3 дня)
 *   • если нет → фиксирует отсутствие live-примеров + рекомендация для #14B
 *
 * ЧЕГО НЕ ДЕЛАЕТ (предохранители):
 *   • НЕ пишет в RAW / CLEAN / UNIT / P&L / dashboards
 *   • НЕ меняет production-файлы, схему, триггеры
 *   • НЕ вызывает import-функции
 *   • самодостаточный: свои хелперы ffr_*, чужих не трогает
 *
 * РАЗРЕШЕНО: console.log; alert; JSON в тестовую Drive-папку
 *   (property WB_API_TEST_RESULTS_FOLDER_ID). Нет property → только console+alert.
 *
 * NB про raw_json: эта колонка содержит ИМЕНА полей (return_count и т.п.),
 *   поэтому по raw_json ищем только русские маркеры (возврат/сторно/коррекц),
 *   а return/refund — по остальным текстовым колонкам. См. scan_notes в выводе.
 * ══════════════════════════════════════════════════════════════
 */


// ═══════════════════════════════════════
// КОНФИГ
// ═══════════════════════════════════════

var FFR_SHEET_NAME_ = 'RAW_WB_FINANCE';
var FFR_RESULTS_FOLDER_PROP_ = 'WB_API_TEST_RESULTS_FOLDER_ID';
var FFR_TZ_ = 'Europe/Moscow';
var FFR_SAMPLE_LIMIT_ = 20;
var FFR_DATE_PAD_DAYS_ = 3;          // ± дней для точечного sales-gate
var FFR_MAX_DATE_RANGES_ = 40;       // потолок предлагаемых периодов

// Маркеры. Русские ищем везде; английские — кроме raw_json.
var FFR_KEYWORDS_RU_  = ['возврат', 'сторно', 'коррекц'];
var FFR_KEYWORDS_EN_  = ['return', 'refund'];

// Подстроки в названии колонки → колонка «типа операции».
var FFR_OP_NAME_HINTS_ = ['type', 'oper', 'operation', 'doc', 'name', 'предмет', 'операц'];

// Явные кандидаты операции (как в проекте).
var FFR_OP_COLS_PREFERRED_ = [
  'doc_type_name', 'doc_type', 'supplier_oper_name', 'supplier_oper',
  'operation_type_normalized', 'operation_type', 'operation'
];

// Имена колонок (фактические в RAW_WB_FINANCE) для sample/дат/сумм.
var FFR_DATE_COLS_   = ['rr_dt', 'sale_dt', 'order_dt', 'date', 'create_dt'];
var FFR_COL_NM_      = ['wb_nm_id', 'nm_id', 'nmid'];
var FFR_COL_DOCTYPE_ = ['doc_type_name', 'doc_type'];
var FFR_COL_OPER_    = ['supplier_oper_name', 'supplier_oper', 'operation_type_normalized'];
var FFR_COL_QTY_     = ['quantity', 'qty'];
var FFR_COL_RETAIL_  = ['retail_amount'];
var FFR_COL_FORPAY_  = ['for_pay'];
var FFR_COL_LOG_     = ['logistics_amount'];
var FFR_COL_STOR_    = ['storage_fee'];
var FFR_COL_DEDUCT_  = ['deduction'];
var FFR_COL_PENALTY_ = ['penalty'];
var FFR_COL_ACQ_     = ['acquiring_fee'];
var FFR_COL_RETAMT_  = ['return_amount_rub'];
var FFR_COL_RETCNT_  = ['return_count'];
var FFR_COL_SRID_    = ['srid'];
var FFR_COL_RAWJSON_ = ['raw_json'];


// ═══════════════════════════════════════
// ОСНОВНАЯ ФУНКЦИЯ
// ═══════════════════════════════════════

/**
 * Запускать ВРУЧНУЮ из редактора Apps Script. Ничего не пишет в листы.
 */
function runFindFinanceReturnsDiagnostic() {
  var t0 = Date.now();
  ffrLog_('═══════════════════════════════════════════════════');
  ffrLog_('  FIND FINANCE RETURNS (PR #14B preflight)');
  ffrLog_('  Лист: ' + FFR_SHEET_NAME_);
  ffrLog_('═══════════════════════════════════════════════════');

  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = ss.getSheetByName(FFR_SHEET_NAME_);
  if (!sheet || sheet.getLastRow() < 2) {
    ffrLog_('❌ ' + FFR_SHEET_NAME_ + ' пуст или не найден.');
    ffrAlert_('❌ Finance Returns', FFR_SHEET_NAME_ + ' пуст или не найден.');
    return;
  }

  // ── Заголовки и данные ──
  var lastCol = sheet.getLastColumn();
  var headers = sheet.getRange(1, 1, 1, lastCol).getValues()[0];
  var headerKeys = [];
  var colIdx = {};
  for (var h = 0; h < headers.length; h++) {
    var name = String(headers[h] || '').trim();
    headerKeys.push(name);
    colIdx[name.toLowerCase()] = h;
  }
  var data = sheet.getRange(2, 1, sheet.getLastRow() - 1, lastCol).getValues();
  ffrLog_('Заголовков: ' + headerKeys.length + '; строк данных: ' + data.length);

  // ── Колонки «типа операции» ──
  var opCols = ffrDetectOpColumns_(headerKeys);
  ffrLog_('Колонки операции: ' + JSON.stringify(opCols));

  // ── Резолв ключевых индексов ──
  var rawJsonIdx = ffrIdx_(colIdx, FFR_COL_RAWJSON_);
  var dateIdxList = ffrIdxList_(colIdx, FFR_DATE_COLS_);
  var idx = {
    nm:      ffrIdx_(colIdx, FFR_COL_NM_),
    docType: ffrIdx_(colIdx, FFR_COL_DOCTYPE_),
    oper:    ffrIdx_(colIdx, FFR_COL_OPER_),
    qty:     ffrIdx_(colIdx, FFR_COL_QTY_),
    retail:  ffrIdx_(colIdx, FFR_COL_RETAIL_),
    forPay:  ffrIdx_(colIdx, FFR_COL_FORPAY_),
    log:     ffrIdx_(colIdx, FFR_COL_LOG_),
    stor:    ffrIdx_(colIdx, FFR_COL_STOR_),
    deduct:  ffrIdx_(colIdx, FFR_COL_DEDUCT_),
    penalty: ffrIdx_(colIdx, FFR_COL_PENALTY_),
    acq:     ffrIdx_(colIdx, FFR_COL_ACQ_),
    retAmt:  ffrIdx_(colIdx, FFR_COL_RETAMT_),
    retCnt:  ffrIdx_(colIdx, FFR_COL_RETCNT_),
    srid:    ffrIdx_(colIdx, FFR_COL_SRID_)
  };

  // ── Проход по строкам ──
  var matched = [];          // keyword-based return-like
  var amountSignalRows = []; // return_amount_rub != 0 || return_count > 0
  var matchByKeyword = {};   // keyword → count
  var matchByColumn = {};    // имя колонки → count

  for (var r = 0; r < data.length; r++) {
    var row = data[r];

    // 1) keyword scan по текстовым полям
    var hit = ffrScanRow_(row, headerKeys, rawJsonIdx);
    if (hit.matched) {
      matched.push(ffrExtractRow_(row, idx, dateIdxList, hit, r + 2));
      for (var k = 0; k < hit.keywords.length; k++)
        matchByKeyword[hit.keywords[k]] = (matchByKeyword[hit.keywords[k]] || 0) + 1;
      for (var c = 0; c < hit.columns.length; c++)
        matchByColumn[hit.columns[c]] = (matchByColumn[hit.columns[c]] || 0) + 1;
    }

    // 2) денежный сигнал возврата
    var retAmt = idx.retAmt >= 0 ? ffrNum_(row[idx.retAmt]) : 0;
    var retCnt = idx.retCnt >= 0 ? ffrNum_(row[idx.retCnt]) : 0;
    if (retAmt !== 0 || retCnt > 0) {
      amountSignalRows.push(ffrExtractRow_(row, idx, dateIdxList,
        { keywords: [], columns: [], note: 'amount_signal' }, r + 2));
    }
  }

  // ── Агрегации по keyword-matched ──
  var dates = [];
  var byOpText = {};
  var byDate = {};
  for (var m = 0; m < matched.length; m++) {
    var mr = matched[m];
    if (mr.date) {
      dates.push(mr.date);
      byDate[mr.date] = (byDate[mr.date] || 0) + 1;
    }
    var opText = (mr.doc_type || '(пусто)') + ' | ' + (mr.supplier_oper_name || '(пусто)');
    byOpText[opText] = (byOpText[opText] || 0) + 1;
  }

  // ── Результат ──
  var found = matched.length > 0;
  var result = {
    gate: 'FindFinanceReturns_TEMP',
    generated_at: Utilities.formatDate(new Date(), FFR_TZ_, 'yyyy-MM-dd HH:mm:ss'),
    sheet: FFR_SHEET_NAME_,
    rows_scanned: data.length,
    op_columns_detected: opCols,
    scan_notes: 'Русские маркеры (' + FFR_KEYWORDS_RU_.join('/') + ') ищутся по всем текстовым полям; ' +
      'английские (' + FFR_KEYWORDS_EN_.join('/') + ') — по всем, КРОМЕ raw_json (там это имена полей).',
    keyword_based: {
      total_return_like_rows: matched.length,
      min_date: ffrMin_(dates),
      max_date: ffrMax_(dates),
      match_by_keyword: matchByKeyword,
      match_by_column: matchByColumn,
      group_by_operation_text: byOpText,
      group_by_date: byDate,
      sample: matched.slice(0, FFR_SAMPLE_LIMIT_)
    },
    amount_signal_based: {
      total_rows: amountSignalRows.length,
      note: 'Строки с return_amount_rub != 0 или return_count > 0 (независимо от текста).',
      sample: amountSignalRows.slice(0, FFR_SAMPLE_LIMIT_)
    },
    suggested_sales_gate_periods: found ? ffrSuggestPeriods_(dates) : null,
    recommendation_pr14b: found ? ffrFoundRecommendation_(matched.length, dates)
                                : ffrNotFoundRecommendation_()
  };

  // ── Лог + Drive + alert ──
  ffrLogResult_(result);
  var savedTo = ffrSaveJsonIfFolder_(result);
  ffrAlert_(found ? '✅ Возвраты в финансах найдены' : '⚠️ Возвраты в финансах НЕ найдены',
            ffrAlertText_(result, savedTo));

  ffrLog_('');
  ffrLog_('═══ Завершено за ' + Math.round((Date.now() - t0) / 1000) + ' сек ═══');
  return result;
}


// ═══════════════════════════════════════
// ДЕТЕКТ КОЛОНОК ОПЕРАЦИИ
// ═══════════════════════════════════════

function ffrDetectOpColumns_(headerKeys) {
  var found = {};
  // 1) явные кандидаты
  for (var i = 0; i < headerKeys.length; i++) {
    var low = headerKeys[i].toLowerCase();
    for (var p = 0; p < FFR_OP_COLS_PREFERRED_.length; p++) {
      if (low === FFR_OP_COLS_PREFERRED_[p]) found[headerKeys[i]] = true;
    }
  }
  // 2) по подстрокам в названии
  for (var j = 0; j < headerKeys.length; j++) {
    var name = headerKeys[j];
    var nl = name.toLowerCase();
    for (var hn = 0; hn < FFR_OP_NAME_HINTS_.length; hn++) {
      if (nl.indexOf(FFR_OP_NAME_HINTS_[hn]) >= 0) { found[name] = true; break; }
    }
  }
  return Object.keys(found);
}


// ═══════════════════════════════════════
// СКАН СТРОКИ
// ═══════════════════════════════════════

/**
 * Ищет маркеры в текстовых полях строки.
 * raw_json: только русские маркеры. Остальное: русские + английские.
 * @return {Object} { matched, keywords:[], columns:[] }
 */
function ffrScanRow_(row, headerKeys, rawJsonIdx) {
  var keywords = {}, columns = {};
  for (var c = 0; c < row.length; c++) {
    var v = row[c];
    if (typeof v !== 'string' || !v) continue;   // даты-объекты и числа пропускаем
    var low = v.toLowerCase();

    // русские — всегда
    for (var i = 0; i < FFR_KEYWORDS_RU_.length; i++) {
      if (low.indexOf(FFR_KEYWORDS_RU_[i]) >= 0) {
        keywords[FFR_KEYWORDS_RU_[i]] = true;
        columns[headerKeys[c] || ('col' + c)] = true;
      }
    }
    // английские — кроме raw_json
    if (c !== rawJsonIdx) {
      for (var e = 0; e < FFR_KEYWORDS_EN_.length; e++) {
        if (low.indexOf(FFR_KEYWORDS_EN_[e]) >= 0) {
          keywords[FFR_KEYWORDS_EN_[e]] = true;
          columns[headerKeys[c] || ('col' + c)] = true;
        }
      }
    }
  }
  var kk = Object.keys(keywords);
  return { matched: kk.length > 0, keywords: kk, columns: Object.keys(columns) };
}

/** Достаёт ключевые поля строки в плоский объект. */
function ffrExtractRow_(row, idx, dateIdxList, hit, sheetRow) {
  return {
    sheet_row: sheetRow,
    date: ffrFirstDate_(row, dateIdxList),
    nmId: idx.nm >= 0 ? row[idx.nm] : null,
    doc_type: idx.docType >= 0 ? row[idx.docType] : null,
    supplier_oper_name: idx.oper >= 0 ? row[idx.oper] : null,
    quantity: idx.qty >= 0 ? ffrNum_(row[idx.qty]) : null,
    retail_amount: idx.retail >= 0 ? ffrNum_(row[idx.retail]) : null,
    for_pay: idx.forPay >= 0 ? ffrNum_(row[idx.forPay]) : null,
    logistics: idx.log >= 0 ? ffrNum_(row[idx.log]) : null,
    storage: idx.stor >= 0 ? ffrNum_(row[idx.stor]) : null,
    deductions: idx.deduct >= 0 ? ffrNum_(row[idx.deduct]) : null,
    penalties: idx.penalty >= 0 ? ffrNum_(row[idx.penalty]) : null,
    acquiring: idx.acq >= 0 ? ffrNum_(row[idx.acq]) : null,
    return_amount_rub: idx.retAmt >= 0 ? ffrNum_(row[idx.retAmt]) : null,
    srid: idx.srid >= 0 ? row[idx.srid] : null,
    matched_keyword: hit.keywords.join(',') || (hit.note || ''),
    matched_in: hit.columns.join(',')
  };
}


// ═══════════════════════════════════════
// ТОЧЕЧНЫЕ ПЕРИОДЫ ДЛЯ SALES-GATE
// ═══════════════════════════════════════

/** Уникальные даты возвратов → диапазоны ±N дней, со слиянием пересечений. */
function ffrSuggestPeriods_(dates) {
  var uniq = {};
  for (var i = 0; i < dates.length; i++) if (dates[i]) uniq[dates[i]] = true;
  var list = Object.keys(uniq).sort();
  if (!list.length) return [];

  var ranges = [];
  for (var j = 0; j < list.length; j++) {
    ranges.push({ from: ffrShiftDate_(list[j], -FFR_DATE_PAD_DAYS_),
                  to:   ffrShiftDate_(list[j],  FFR_DATE_PAD_DAYS_) });
  }
  // слияние пересекающихся/смежных
  ranges.sort(function (a, b) { return a.from < b.from ? -1 : (a.from > b.from ? 1 : 0); });
  var merged = [ranges[0]];
  for (var k = 1; k < ranges.length; k++) {
    var last = merged[merged.length - 1];
    if (ranges[k].from <= last.to) {
      if (ranges[k].to > last.to) last.to = ranges[k].to;
    } else {
      merged.push(ranges[k]);
    }
  }
  if (merged.length > FFR_MAX_DATE_RANGES_) {
    var capped = merged.slice(0, FFR_MAX_DATE_RANGES_);
    capped.push({ note: 'обрезано: всего диапазонов ' + merged.length });
    return capped;
  }
  return merged;
}


// ═══════════════════════════════════════
// РЕКОМЕНДАЦИИ
// ═══════════════════════════════════════

function ffrFoundRecommendation_(count, dates) {
  return [
    'В RAW_WB_FINANCE найдено return-like строк: ' + count + ' (даты ' +
      ffrMin_(dates) + ' … ' + ffrMax_(dates) + ').',
    'Прогнать sales-gate (WbApiGateSalesReturns_TEMP) точечно по suggested_sales_gate_periods (дата возврата ± ' +
      FFR_DATE_PAD_DAYS_ + ' дн.), чтобы поймать те же возвраты через /supplier/sales (saleID startsWith "R").',
    'Сверить: возврат из финотчёта (return_amount_rub / doc_type=Возврат) против return-строки sales API по srid/nmId/дате.',
    'Если в sales API по этим датам возврат всё равно не виден — зафиксировать расхождение источников в #14B (финотчёт видит возврат, sales — нет).',
    'Всё read-only, production-запись не использовать.'
  ];
}

function ffrNotFoundRecommendation_() {
  return [
    'В текущем RAW_WB_FINANCE live-return примеров нет (ни по тексту, ни по return_amount_rub/return_count).',
    'Для PR #14B: реализовать return-ветку ПО КОНТРАКТУ — saleID startsWith "R" → operation_type="return", is_return=TRUE.',
    'Юнит-логику возврата (вычитание qty/сумм) покрыть на синтетической строке (как в Testrawtoclean), но НЕ закреплять как проверенную на проде.',
    'Отложить фактическую проверку return-ветки до появления реального возврата; завести лёгкий мониторинг: периодически прогонять этот gate + sales-gate, пока returns_count не станет > 0.',
    'Не блокировать #14B из-за отсутствия возвратов: sales-часть подтверждена (3142 строки), return-часть идёт «по контракту».'
  ];
}


// ═══════════════════════════════════════
// ВЫВОД
// ═══════════════════════════════════════

function ffrLogResult_(r) {
  ffrLog_('');
  ffrLog_('───── KEYWORD-BASED ─────');
  ffrLog_('total_return_like_rows=' + r.keyword_based.total_return_like_rows +
    '; dates ' + r.keyword_based.min_date + ' … ' + r.keyword_based.max_date);
  ffrLog_('match_by_keyword: ' + JSON.stringify(r.keyword_based.match_by_keyword));
  ffrLog_('match_by_column: ' + JSON.stringify(r.keyword_based.match_by_column));
  ffrLog_('group_by_operation_text: ' + JSON.stringify(r.keyword_based.group_by_operation_text, null, 2));
  ffrLog_('group_by_date: ' + JSON.stringify(r.keyword_based.group_by_date, null, 2));
  ffrLog_('sample (до ' + FFR_SAMPLE_LIMIT_ + '): ' + JSON.stringify(r.keyword_based.sample, null, 2));
  ffrLog_('───── AMOUNT-SIGNAL ─────');
  ffrLog_('total_rows=' + r.amount_signal_based.total_rows);
  ffrLog_('sample: ' + JSON.stringify(r.amount_signal_based.sample, null, 2));
  ffrLog_('───── SUGGESTED SALES-GATE PERIODS ─────');
  ffrLog_(JSON.stringify(r.suggested_sales_gate_periods, null, 2));
  ffrLog_('───── RECOMMENDATION PR #14B ─────');
  ffrLog_(JSON.stringify(r.recommendation_pr14b, null, 2));
}

function ffrSaveJsonIfFolder_(result) {
  var folderId = ffrProp_(FFR_RESULTS_FOLDER_PROP_);
  if (!folderId) {
    ffrLog_('JSON не сохранён: нет property ' + FFR_RESULTS_FOLDER_PROP_ + ' (только console + alert).');
    return '';
  }
  try {
    var folder = DriveApp.getFolderById(folderId);
    var stamp = Utilities.formatDate(new Date(), FFR_TZ_, 'yyyyMMdd_HHmmss');
    var name = 'find_finance_returns_' + stamp + '.json';
    var file = folder.createFile(name, JSON.stringify(result, null, 2), 'application/json');
    ffrLog_('JSON сохранён: ' + name);
    return name + ' (id=' + file.getId() + ')';
  } catch (e) {
    ffrLog_('Ошибка сохранения JSON: ' + e.message);
    return '';
  }
}

function ffrAlertText_(r, savedTo) {
  var kb = r.keyword_based, ab = r.amount_signal_based;
  var lines = [];
  lines.push('Лист: ' + r.sheet + '  •  строк просканировано: ' + r.rows_scanned);
  lines.push('');
  lines.push('Keyword-based: ' + kb.total_return_like_rows + ' строк');
  if (kb.total_return_like_rows > 0) {
    lines.push('  даты: ' + kb.min_date + ' … ' + kb.max_date);
    lines.push('  по маркеру: ' + JSON.stringify(kb.match_by_keyword));
    lines.push('  по колонке: ' + JSON.stringify(kb.match_by_column));
  }
  lines.push('Amount-signal (return_amount_rub/return_count): ' + ab.total_rows + ' строк');
  lines.push('');
  if (kb.total_return_like_rows > 0) {
    var n = r.suggested_sales_gate_periods ? r.suggested_sales_gate_periods.length : 0;
    lines.push('→ Предложено периодов для sales-gate: ' + n + ' (±' + FFR_DATE_PAD_DAYS_ + ' дн.)');
  } else {
    lines.push('→ Live-возвратов нет. #14B: return-ветка «по контракту» (saleID startsWith R).');
  }
  lines.push(savedTo ? ('JSON: ' + savedTo) : 'JSON не сохранён — смотри логи.');
  lines.push('Полные группировки/sample/рекомендации — в Executions / console.log.');
  return lines.join('\n');
}

function ffrAlert_(title, msg) {
  try {
    SpreadsheetApp.getUi().alert(title, msg, SpreadsheetApp.getUi().ButtonSet.OK);
  } catch (e) {
    ffrLog_('(alert недоступен) ' + title + '\n' + msg);
  }
}


// ═══════════════════════════════════════
// УТИЛИТЫ (самодостаточные, ffr_)
// ═══════════════════════════════════════

function ffrLog_(msg) { console.log('[FFR] ' + msg); }

function ffrProp_(key) {
  try { return PropertiesService.getScriptProperties().getProperty(key) || ''; }
  catch (e) { return ''; }
}

/** Индекс первой найденной колонки из списка имён (lowercase-сравнение). */
function ffrIdx_(colIdx, names) {
  for (var i = 0; i < names.length; i++) {
    if (colIdx[names[i]] !== undefined) return colIdx[names[i]];
  }
  return -1;
}
function ffrIdxList_(colIdx, names) {
  var out = [];
  for (var i = 0; i < names.length; i++) {
    if (colIdx[names[i]] !== undefined) out.push(colIdx[names[i]]);
  }
  return out;
}

function ffrNum_(v) { var n = Number(v); return isNaN(n) ? 0 : n; }

/** Первое непустое значение даты из списка колонок → 'YYYY-MM-DD'. */
function ffrFirstDate_(row, dateIdxList) {
  for (var i = 0; i < dateIdxList.length; i++) {
    var d = ffrDateKey_(row[dateIdxList[i]]);
    if (d) return d;
  }
  return null;
}

/** Нормализует значение даты к 'YYYY-MM-DD' (Date-объект или ISO-строка). */
function ffrDateKey_(v) {
  if (v === undefined || v === null || v === '') return null;
  if (Object.prototype.toString.call(v) === '[object Date]' && !isNaN(v.getTime())) {
    return Utilities.formatDate(v, FFR_TZ_, 'yyyy-MM-dd');
  }
  var s = String(v);
  if (s.length >= 10 && s.charAt(4) === '-' && s.charAt(7) === '-') return s.substring(0, 10);
  try {
    var d = new Date(v);
    if (!isNaN(d.getTime())) return Utilities.formatDate(d, FFR_TZ_, 'yyyy-MM-dd');
  } catch (e) {}
  return null;
}

/** Сдвиг 'YYYY-MM-DD' на dDays. */
function ffrShiftDate_(dateStr, dDays) {
  var p = dateStr.split('-');
  var d = new Date(Number(p[0]), Number(p[1]) - 1, Number(p[2]));
  d.setDate(d.getDate() + dDays);
  return Utilities.formatDate(d, FFR_TZ_, 'yyyy-MM-dd');
}

function ffrMin_(arr) {
  if (!arr.length) return null;
  var m = arr[0];
  for (var i = 1; i < arr.length; i++) if (arr[i] < m) m = arr[i];
  return m;
}
function ffrMax_(arr) {
  if (!arr.length) return null;
  var m = arr[0];
  for (var i = 1; i < arr.length; i++) if (arr[i] > m) m = arr[i];
  return m;
}