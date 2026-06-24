/**
 * ══════════════════════════════════════════════════════════════
 * EVETIS WB — WbAdsFullstatsMonth.gs
 *
 * Месячный сбор fullstats (статистика рекламных кампаний) с
 * идемпотентностью. Тонкая надстройка над существующим
 * loadWbAdsFullstatsRaw(): сам сборщик не меняется.
 *
 * Зачем: штатные пункты тянут fullstats окнами ~7 дней, и запись
 * идёт append-only → при повторе/ежедневном обновлении за месяц
 * данные дублируются. Здесь:
 *   1) период = месяц (текущий или выбранный);
 *   2) перед сбором строки fullstats за этот период удаляются
 *      (replace-slice по period_from/period_to + source_method),
 *      затем собираются заново — повтор НЕ плодит дубли;
 *   3) fullstats батчит кампании (50/запрос) и бьёт период на окна
 *      ≤31 дня — месяц укладывается в один прогон; если упрётся в
 *      тайм-бюджет, существующий сборщик вернёт PARTIAL.
 *
 * ПЕРЕИСПОЛЬЗУЕТ (без изменений):
 *   loadWbAdsFullstatsRaw, wbAdsRawNormalizeRange_, wbAdsResolveRunId_,
 *   WB_ADV_RAW_CAMPAIGN_STATS_SHEET_, WB_ADV_RAW_BOOSTER_STATS_SHEET_.
 * НЕ трогает: CLEAN/UNIT/PNL/RAW_WB_FINANCE, схему RAW, daily refresh.
 * ══════════════════════════════════════════════════════════════
 */

var WB_ADS_FS_TZ_ = 'Europe/Moscow';

/** fullstats за ТЕКУЩИЙ месяц (1-е число → сегодня). */
function wbAdsFullstatsMonthCurrent() {
  var now = new Date();
  var from = Utilities.formatDate(new Date(now.getFullYear(), now.getMonth(), 1), WB_ADS_FS_TZ_, 'yyyy-MM-dd');
  var to   = Utilities.formatDate(now, WB_ADS_FS_TZ_, 'yyyy-MM-dd');
  wbAdsFullstatsMonthRun_(from, to);
}

/** fullstats за выбранный период (prompt). */
function wbAdsFullstatsMonthPrompt() {
  var ui = SpreadsheetApp.getUi();
  var resp = ui.prompt('fullstats за период',
    'Введите период: YYYY-MM-DD,YYYY-MM-DD\nНапример: 2026-06-01,2026-06-30',
    ui.ButtonSet.OK_CANCEL);
  if (resp.getSelectedButton() !== ui.Button.OK) return;
  var parts = String(resp.getResponseText() || '').split(',');
  var from = (parts[0] || '').trim(), to = (parts[1] || '').trim();
  if (!/^\d{4}-\d{2}-\d{2}$/.test(from) || !/^\d{4}-\d{2}-\d{2}$/.test(to)) {
    ui.alert('Неверный формат. Ожидается YYYY-MM-DD,YYYY-MM-DD'); return;
  }
  wbAdsFullstatsMonthRun_(from, to);
}

/**
 * Ядро: replace-slice за период на обоих листах статистики, затем сбор.
 */
function wbAdsFullstatsMonthRun_(from, to) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var rng = wbAdsRawNormalizeRange_(from, to);

  // 1) Идемпотентность: чистим прежние fullstats-строки за период.
  var delStat = wbAdsFsDeletePeriodRows_(ss.getSheetByName(WB_ADV_RAW_CAMPAIGN_STATS_SHEET_), rng.from, rng.to);
  var delBoost = wbAdsFsDeletePeriodRows_(ss.getSheetByName(WB_ADV_RAW_BOOSTER_STATS_SHEET_), rng.from, rng.to);
  console.log('  [FS-MONTH] replace-slice: stats -' + delStat + ', booster -' + delBoost + ' за ' + rng.from + '…' + rng.to);

  // 2) Существующий сборщик (батчи 50→10→single, окна ≤31 дня, тайм-бюджет).
  var res = loadWbAdsFullstatsRaw(rng.from, rng.to, wbAdsResolveRunId_(null));

  // 3) Неблокирующая обратная связь.
  var msg = 'Период ' + rng.from + '…' + rng.to + ' · статус ' + (res && res.status) +
            ' · строк ' + (res && res.rows != null ? res.rows : '—') +
            (res && res.status === 'PARTIAL' ? ' · ⏳ упёрлось в тайм-бюджет; повторный запуск пересоберёт период заново' : '');
  try { ss.toast(msg, '📊 fullstats за месяц', res && res.status === 'PARTIAL' ? 12 : 10); } catch (e) {}
}

/**
 * Replace-slice для листов статистики: удаляет строки fullstats за период.
 * Матч: period_from==from И period_to==to И (source_method содержит 'fullstats'
 * ИЛИ processed_status=='no_stats'), TEST не трогаем. @return число удалённых.
 */
function wbAdsFsDeletePeriodRows_(sheet, from, to) {
  if (!sheet) return 0;
  var last = sheet.getLastRow();
  if (last < 2) return 0;
  var lastCol = sheet.getLastColumn();
  var values = sheet.getRange(1, 1, last, lastCol).getValues();
  var header = values[0];
  var idx = {};
  for (var c = 0; c < header.length; c++) idx[String(header[c]).trim()] = c;
  var iFrom = idx['period_from'], iTo = idx['period_to'], iSrc = idx['source_method'], iPs = idx['processed_status'];
  if (iFrom === undefined || iTo === undefined) return 0;

  var keep = [], deleted = 0;
  for (var r = 1; r < values.length; r++) {
    var row = values[r];
    var src = (iSrc !== undefined) ? String(row[iSrc] || '') : '';
    var ps  = (iPs  !== undefined) ? String(row[iPs]  || '') : '';
    var isFs = (src.toLowerCase().indexOf('fullstats') !== -1) || (ps === 'no_stats');
    var match = (String(row[iFrom]) === String(from)) && (String(row[iTo]) === String(to)) &&
                isFs && (src.toUpperCase() !== 'TEST');
    if (match) { deleted++; } else { keep.push(row); }
  }
  if (deleted === 0) return 0;

  sheet.getRange(2, 1, last - 1, lastCol).clearContent();
  if (keep.length) sheet.getRange(2, 1, keep.length, lastCol).setValues(keep);
  return deleted;
}
