/**
 * ══════════════════════════════════════════════════════════════
 * EVETIS WB — WbAdsClustersJob.gs
 *
 * Полный сбор поисковых кластеров рекламы (normquery/stats) по ВСЕМ
 * активным парам advertId+nmId, частями с сохранением прогресса —
 * чтобы не упираться в 6-минутный лимит Apps Script, и за МЕСЯЦ.
 *
 * Идемпотентность: на старте джоба строки кластеров за период УДАЛЯЮТСЯ
 * (replace-slice по period_from/period_to через deleteRows — лист остаётся
 * сплошным, без пустых разрывов), затем собираются заново.
 * Данные за ДРУГИЕ месяцы НЕ трогаются — история копится накопительно.
 *
 * Прогресс хранится в служебном скрытом листе _ADS_CLUSTERS_JOB
 * (одна строка на пару, перезаписывается на каждый «Старт») + мета в
 * Script Properties. Это рабочий лист джоба, а НЕ хранилище данных.
 *
 * ТАЙМИНГ: пачка ограничена тайм-бюджетом (~3.5 мин) с резервом под
 * последнюю итерацию; обратная связь — НЕблокирующий toast (не ui.alert),
 * чтобы ожидание клика не съедало 6-минутный лимит выполнения.
 *
 * ПЕРЕИСПОЛЬЗУЕТ (без изменений):
 *   getWbAdsToken_, wbAdsHttp_, wbAdsCollectAdvertNmPairs_,
 *   wbAdvFlattenNormquery_, wbAdvRawEnsureSheet_, wbAdvRawAppendRows_,
 *   wbAdsRawNormalizeRange_, wbAdsResolveRunId_, wbAdsRawWriteStatus_,
 *   WB_ADV_RAW_SEARCH_CLUSTERS_SHEET_, WB_ADV_RAW_SEARCH_CLUSTERS_HEADERS_,
 *   WB_ADS_API_HOST_, WB_ADS_NORMQUERY_PAUSE_MS_.
 * НЕ трогает: CLEAN/UNIT/PNL/RAW_WB_FINANCE, схему RAW, daily refresh.
 * ══════════════════════════════════════════════════════════════
 */

var WB_ADS_CLJOB_SHEET_           = '_ADS_CLUSTERS_JOB';
var WB_ADS_CLJOB_BUDGET_MS_       = 210000;  // ~3.5 мин на пачку (запас до 6-мин лимита)
var WB_ADS_CLJOB_ITER_RESERVE_MS_ = 35000;   // резерв под последнюю итерацию (пауза + возможный 429-ретрай)
var WB_ADS_CLJOB_TZ_              = 'Europe/Moscow';
var WB_ADS_CLJOB_TRIGGER_FN_      = 'wbAdsClustersJobTriggerTick_';
var WB_ADS_CLJOB_HEADERS_         = ['idx', 'advert_id', 'nm_id', 'done', 'rows_written', 'http_code', 'processed_at'];

// ─── Script Properties (мета джоба) ───
function wbAdsClProp_(k)      { return getScriptProperty_('CLJOB_' + k, ''); }
function wbAdsClSetProp_(k,v) { setScriptProperty_('CLJOB_' + k, String(v)); }
function wbAdsClClearProps_() {
  var p = PropertiesService.getScriptProperties();
  var keys = ['PERIOD_FROM','PERIOD_TO','RUN_ID','CURSOR','TOTAL','STATUS','STARTED_AT','ROWS'];
  for (var i = 0; i < keys.length; i++) { try { p.deleteProperty('CLJOB_' + keys[i]); } catch (e) {} }
}


// ═══════════════════════════════════════
// МЕНЮ-ТОЧКИ ВХОДА
// ═══════════════════════════════════════

/** Старт сбора кластеров за ТЕКУЩИЙ месяц (1-е число → сегодня). */
function wbAdsClustersJobStartCurrentMonth() {
  var now = new Date();
  var from = Utilities.formatDate(new Date(now.getFullYear(), now.getMonth(), 1), WB_ADS_CLJOB_TZ_, 'yyyy-MM-dd');
  var to   = Utilities.formatDate(now, WB_ADS_CLJOB_TZ_, 'yyyy-MM-dd');
  wbAdsClustersJobStart_(from, to, false);
}

/** Старт сбора кластеров за произвольный период (prompt). */
function wbAdsClustersJobStartPrompt() {
  var ui = SpreadsheetApp.getUi();
  var resp = ui.prompt('Кластеры за период',
    'Введите период: YYYY-MM-DD,YYYY-MM-DD\nНапример: 2026-06-01,2026-06-30',
    ui.ButtonSet.OK_CANCEL);
  if (resp.getSelectedButton() !== ui.Button.OK) return;
  var parts = String(resp.getResponseText() || '').split(',');
  var from = (parts[0] || '').trim(), to = (parts[1] || '').trim();
  if (!/^\d{4}-\d{2}-\d{2}$/.test(from) || !/^\d{4}-\d{2}-\d{2}$/.test(to)) {
    ui.alert('Неверный формат. Ожидается YYYY-MM-DD,YYYY-MM-DD'); return;
  }
  wbAdsClustersJobStart_(from, to, false);
}

/** Продолжить незавершённый сбор (обработать следующую пачку). */
function wbAdsClustersJobContinue() {
  if (wbAdsClProp_('STATUS') !== 'running') {
    SpreadsheetApp.getActiveSpreadsheet().toast('Нет активного джоба. Сначала «Старт».', '📊 Кластеры', 6);
    return;
  }
  var pr = wbAdsClustersJobProcessBatch_();
  wbAdsClJobToast_(pr);
}

/** Показать статус джоба (быстрый, неблокирующий). */
function wbAdsClustersJobStatus() {
  var status = wbAdsClProp_('STATUS') || 'none';
  if (status === 'none' || status === '') {
    SpreadsheetApp.getActiveSpreadsheet().toast('Джоб кластеров не запускался.', '📊 Кластеры', 6);
    return;
  }
  var cursor = parseInt(wbAdsClProp_('CURSOR') || '0', 10);
  var total  = parseInt(wbAdsClProp_('TOTAL') || '0', 10);
  var rows   = parseInt(wbAdsClProp_('ROWS') || '0', 10);
  SpreadsheetApp.getActiveSpreadsheet().toast(
    'Период ' + wbAdsClProp_('PERIOD_FROM') + '…' + wbAdsClProp_('PERIOD_TO') +
    ' | статус: ' + status + ' | пар ' + cursor + '/' + total + ' | строк ' + rows,
    '📊 Кластеры — статус', 10);
}


// ═══════════════════════════════════════
// ЯДРО
// ═══════════════════════════════════════

/** Старт джоба: полный список пар, replace-slice за период, сбор первой пачки. */
function wbAdsClustersJobStart_(from, to, silent) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var tok = getWbAdsToken_();
  if (!tok) {
    if (!silent) ss.toast('Нет WB Promotion токена (Script Property).', '📊 Кластеры', 8);
    return;
  }
  var rng = wbAdsRawNormalizeRange_(from, to);
  var runId = wbAdsResolveRunId_(null);

  var collected = wbAdsCollectAdvertNmPairs_(tok.token, 100000);
  var pairs = (collected && collected.pairs) || [];
  if (!pairs.length) {
    if (!silent) ss.toast('Не удалось получить пары advertId+nmId из adverts v2.', '📊 Кластеры', 8);
    return;
  }

  var sheet = wbAdvRawEnsureSheet_(ss, WB_ADV_RAW_SEARCH_CLUSTERS_SHEET_, WB_ADV_RAW_SEARCH_CLUSTERS_HEADERS_);
  var deleted = wbAdsClDeletePeriodRows_(sheet, rng.from, rng.to);

  var jobSheet = wbAdsClJobSheet_(ss);
  wbAdsClEnsureCapacity_(jobSheet, pairs.length + 1, WB_ADS_CLJOB_HEADERS_.length);
  if (jobSheet.getLastRow() > 1) {
    jobSheet.getRange(2, 1, jobSheet.getLastRow() - 1, WB_ADS_CLJOB_HEADERS_.length).clearContent();
  }
  var rows = [];
  for (var i = 0; i < pairs.length; i++) rows.push([i, pairs[i].advertId, pairs[i].nmId, 0, '', '', '']);
  if (rows.length) jobSheet.getRange(2, 1, rows.length, WB_ADS_CLJOB_HEADERS_.length).setValues(rows);

  wbAdsClSetProp_('PERIOD_FROM', rng.from); wbAdsClSetProp_('PERIOD_TO', rng.to);
  wbAdsClSetProp_('RUN_ID', runId); wbAdsClSetProp_('CURSOR', 0);
  wbAdsClSetProp_('TOTAL', pairs.length); wbAdsClSetProp_('ROWS', 0);
  wbAdsClSetProp_('STATUS', 'running');
  wbAdsClSetProp_('STARTED_AT', Utilities.formatDate(new Date(), WB_ADS_CLJOB_TZ_, 'yyyy-MM-dd HH:mm:ss'));

  console.log('  [CLJOB] старт ' + rng.from + '…' + rng.to + ', пар: ' + pairs.length + ', удалено старых строк периода: ' + deleted);

  var pr = wbAdsClustersJobProcessBatch_();
  if (!silent) wbAdsClJobToast_(pr);
}

/** Обработать очередную пачку пар в пределах тайм-бюджета. */
function wbAdsClustersJobProcessBatch_() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  if (wbAdsClProp_('STATUS') !== 'running') return { processed: 0, total: 0, rows_added: 0, done: true };
  var tok = getWbAdsToken_();
  if (!tok) return { processed: 0, total: 0, rows_added: 0, done: false, error: 'нет токена' };

  var from = wbAdsClProp_('PERIOD_FROM'), to = wbAdsClProp_('PERIOD_TO'), runId = wbAdsClProp_('RUN_ID');
  var cursor = parseInt(wbAdsClProp_('CURSOR') || '0', 10);
  var total  = parseInt(wbAdsClProp_('TOTAL') || '0', 10);
  var rowsAcc = parseInt(wbAdsClProp_('ROWS') || '0', 10);

  var sheet = wbAdvRawEnsureSheet_(ss, WB_ADV_RAW_SEARCH_CLUSTERS_SHEET_, WB_ADV_RAW_SEARCH_CLUSTERS_HEADERS_);
  var jobSheet = wbAdsClJobSheet_(ss);
  if (total < 1) return { processed: cursor, total: total, rows_added: 0, total_rows: rowsAcc, done: true };
  var jobData = jobSheet.getRange(2, 1, total, WB_ADS_CLJOB_HEADERS_.length).getValues();

  var deadline = Date.now() + WB_ADS_CLJOB_BUDGET_MS_;
  var addedThisBatch = 0, httpThisBatch = 0, processedNow = 0;

  for (var i = cursor; i < total; i++) {
    if (Date.now() + WB_ADS_CLJOB_ITER_RESERVE_MS_ >= deadline) break;
    var advertId = jobData[i][1], nmId = jobData[i][2];
    if (httpThisBatch > 0) Utilities.sleep(WB_ADS_NORMQUERY_PAUSE_MS_);
    httpThisBatch++;

    var resp = wbAdsHttp_('post', WB_ADS_API_HOST_ + '/adv/v0/normquery/stats', tok.token,
      { from: from, to: to, items: [{ advert_id: advertId, nm_id: nmId }] });

    var written = 0;
    if (resp.ok) { written = wbAdvRawAppendRows_(sheet, wbAdvFlattenNormquery_(resp.json, runId, from, to)); addedThisBatch += written; }
    jobSheet.getRange(i + 2, 4, 1, 4).setValues([[1, written, resp.code, Utilities.formatDate(new Date(), WB_ADS_CLJOB_TZ_, 'yyyy-MM-dd HH:mm:ss')]]);

    cursor = i + 1; processedNow++;
    if (processedNow % 10 === 0) { wbAdsClSetProp_('CURSOR', cursor); wbAdsClSetProp_('ROWS', rowsAcc + addedThisBatch); }
  }

  rowsAcc += addedThisBatch;
  wbAdsClSetProp_('CURSOR', cursor); wbAdsClSetProp_('ROWS', rowsAcc);

  var done = (cursor >= total);
  if (done) {
    wbAdsClSetProp_('STATUS', 'done');
    try {
      wbAdsRawWriteStatus_(runId, 'raw_search_clusters_full', from, to, {
        campaigns_sampled: total, rows_or_items_found: rowsAcc, status: 'OK',
        response_keys_sample: 'full_job: pairs=' + total + '; cluster_rows=' + rowsAcc });
    } catch (e) {}
    console.log('  [CLJOB] ЗАВЕРШЕНО: пар ' + total + ', строк ' + rowsAcc);
  } else {
    console.log('  [CLJOB] пачка: обработано ' + cursor + '/' + total + ', +' + addedThisBatch + ' строк');
  }
  return { processed: cursor, total: total, rows_added: addedThisBatch, total_rows: rowsAcc, done: done };
}

/** НЕблокирующая обратная связь (toast не ждёт клика → не съедает лимит выполнения). */
function wbAdsClJobToast_(pr) {
  if (!pr) return;
  var msg = pr.error ? ('Ошибка: ' + pr.error)
    : ('Пар ' + pr.processed + '/' + pr.total + ' · строк всего ' + (pr.total_rows != null ? pr.total_rows : '—') +
       (pr.done ? ' · ✅ завершено' : ' · ⏳ нажмите «Продолжить» или включите авто-сбор'));
  try { SpreadsheetApp.getActiveSpreadsheet().toast(msg, '📊 Кластеры за месяц', pr.done ? 12 : 8); } catch (e) {}
}


// ═══════════════════════════════════════
// REPLACE-SLICE (deleteRows) + СЛУЖЕБНЫЙ ЛИСТ
// ═══════════════════════════════════════

/**
 * Удаляет строки кластеров за период РЕАЛЬНЫМ deleteRows (лист остаётся сплошным,
 * без пустых разрывов — важно для чтения и для накопительного хранения).
 * TEST и другие периоды не трогаем. @return число удалённых строк.
 */
function wbAdsClDeletePeriodRows_(sheet, from, to) {
  if (!sheet) return 0;
  var last = sheet.getLastRow();
  if (last < 2) return 0;
  var lastCol = sheet.getLastColumn();
  var values = sheet.getRange(1, 1, last, lastCol).getValues();
  var header = values[0]; var idx = {};
  for (var c = 0; c < header.length; c++) idx[String(header[c]).trim()] = c;
  var iFrom = idx['period_from'], iTo = idx['period_to'], iSrc = idx['source_method'];
  if (iFrom === undefined || iTo === undefined) return 0;

  var toDel = [];
  for (var r = 1; r < values.length; r++) {
    var row = values[r];
    var src = (iSrc !== undefined) ? String(row[iSrc] || '') : '';
    if (String(row[iFrom]) === String(from) && String(row[iTo]) === String(to) && src.toUpperCase() !== 'TEST') {
      toDel.push(r + 1); // 1-based row number
    }
  }
  if (!toDel.length) return 0;
  // Удаляем сплошными диапазонами снизу вверх (чтобы индексы не сдвигались).
  toDel.sort(function (a, b) { return a - b; });
  var i = toDel.length - 1, removed = 0;
  while (i >= 0) {
    var end = toDel[i], start = end;
    while (i > 0 && toDel[i - 1] === start - 1) { start = toDel[i - 1]; i--; }
    sheet.deleteRows(start, end - start + 1);
    removed += (end - start + 1);
    i--;
  }
  return removed;
}

/** Служебный скрытый лист прогресса. */
function wbAdsClJobSheet_(ss) {
  var sheet = ss.getSheetByName(WB_ADS_CLJOB_SHEET_);
  if (!sheet) {
    sheet = ss.insertSheet(WB_ADS_CLJOB_SHEET_);
    sheet.getRange(1, 1, 1, WB_ADS_CLJOB_HEADERS_.length).setValues([WB_ADS_CLJOB_HEADERS_]);
    sheet.setFrozenRows(1);
  }
  try { sheet.hideSheet(); } catch (e) {}
  return sheet;
}

/** Гарантирует достаточную ёмкость листа: строк >= needRows, колонок >= needCols. */
function wbAdsClEnsureCapacity_(sheet, needRows, needCols) {
  var maxRows = sheet.getMaxRows();
  if (maxRows < needRows) sheet.insertRowsAfter(maxRows, needRows - maxRows);
  var maxCols = sheet.getMaxColumns();
  if (maxCols < needCols) sheet.insertColumnsAfter(maxCols, needCols - maxCols);
}


// ═══════════════════════════════════════
// АВТО-СБОР (временной триггер)
// ═══════════════════════════════════════

/** Поставить временной триггер авто-добора (каждые 10 минут). */
function wbAdsClustersJobInstallTrigger() {
  wbAdsClustersJobRemoveTrigger_();
  ScriptApp.newTrigger(WB_ADS_CLJOB_TRIGGER_FN_).timeBased().everyMinutes(10).create();
  SpreadsheetApp.getActiveSpreadsheet().toast('Авто-сбор включён: каждые 10 минут, пока не доберётся. Снимется сам по завершении.', '🤖 Кластеры', 10);
}

/** Снять триггер авто-добора (ручной пункт меню). */
function wbAdsClustersJobRemoveTrigger() {
  var n = wbAdsClustersJobRemoveTrigger_();
  SpreadsheetApp.getActiveSpreadsheet().toast(n ? ('Авто-сбор выключен (снято триггеров: ' + n + ').') : 'Активных триггеров не было.', '🛑 Кластеры', 8);
}

function wbAdsClustersJobRemoveTrigger_() {
  var trs = ScriptApp.getProjectTriggers(); var n = 0;
  for (var i = 0; i < trs.length; i++) {
    if (trs[i].getHandlerFunction() === WB_ADS_CLJOB_TRIGGER_FN_) { ScriptApp.deleteTrigger(trs[i]); n++; }
  }
  return n;
}

/** Тик триггера: добирает пачку; по завершении сам снимает триггер. */
function wbAdsClustersJobTriggerTick_() {
  if (wbAdsClProp_('STATUS') !== 'running') { wbAdsClustersJobRemoveTrigger_(); return; }
  var pr = wbAdsClustersJobProcessBatch_();
  if (pr && pr.done) wbAdsClustersJobRemoveTrigger_();
}
