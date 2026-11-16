/**
 * Отдельный ежедневный запуск ставок и статистики ключей WB (16.11.2026, ACK владельца).
 *
 * Зачем. В общем прогоне рекламы (Wbadsdaily, ~05:07 МСК) ставки (Ads-3) и статистика ключей (Ads-2)
 * идут «хвостом» и примерно через день не успевают:
 *   • 09.11 и 13.11 — справочник /adverts отвечал медленно и съел собственный бюджет ставок (85 с из 90);
 *   • 07.11 и 15.11 — в 6-минутном прогоне не осталось места (основная часть шла до 300 с).
 * Снимок ставок за пропущенный день не восстановить; статистика добиралась только за одни сутки
 * (12.11 так и осталась без данных).
 *
 * Что делает — в своей 6-минутной квоте Apps Script, общий прогон рекламы не трогает:
 *   1) ставки: если за сегодня (МСК) в RAW_WB_ADV_QUERY_BIDS_RUNS нет снимка OK — снимает их
 *      с бюджетом 4 мин вместо 90 с;
 *   2) статистика: добирает сутки за последние 7 дней без разрешённого исхода (OK или EMPTY)
 *      по V_ADV_QUERY_STATS_COVERAGE, а также сутки, по которым попыток не было вовсе.
 * Идемпотентно: если «хвост» общего прогона успел, ставки пропускаются, а у статистики нет дыр.
 * Пишет теми же загрузчиками в те же листы и BigQuery (через BQ-приёмник рекламы).
 *
 * Мост на время миграции WB в Cloud Run: удалить вместе с триггером после переноса Ads-2/Ads-3.
 *
 * Установка: один раз выполнить wbAdsKeywordsInstallTrigger() из редактора (ежедневно ~08:30
 * по часовому поясу проекта). Ручной прогон — wbAdsKeywordsDaily().
 */

var WB_ADS_KW_BIDS_BUDGET_MS_ = 240000;   // 4 мин на ставки: справочник /adverts бывает медленным
var WB_ADS_KW_STATS_WINDOW_DAYS_ = 7;     // окно добора статистики ключей
var WB_ADS_KW_TRIGGER_HOUR_ = 8;          // ~08:30: после общего прогона рекламы (05:07) и до агента роста (10:15)

function wbAdsKeywordsDaily() {
  var t0 = Date.now();
  var today = Utilities.formatDate(new Date(), 'Europe/Moscow', 'yyyy-MM-dd');
  var out = { today: today, bids: null, stats: null };

  // 1) Ставки: снимок «на сейчас», поэтому только если за сегодня ещё нет OK.
  if (wbAdsKwBidsOkToday_(today)) {
    out.bids = 'SKIP: снимок за ' + today + ' уже OK';
  } else {
    WB_ADS_QBIDS_BUDGET_MS_ = WB_ADS_KW_BIDS_BUDGET_MS_;   // своё время, а не хвост общего прогона
    var b = loadWbAdsQueryBidsRaw(null);
    out.bids = b ? (b.status + ' · строк ' + b.rows + ' · пар ' + b.pairs) : 'нет результата';
  }

  // 2) Статистика: последние 7 суток без разрешённого исхода.
  var pending = wbAdsKwStatsPending_();
  if (!pending.ok) {
    out.stats = 'не выполнено: ' + pending.error;
  } else if (!pending.days.length) {
    out.stats = 'дыр нет за ' + WB_ADS_KW_STATS_WINDOW_DAYS_ + ' дн';
  } else {
    var st = { t0: Date.now(), httpCalls: 0 };
    var s = wbAdsQsRunDays_(pending.days, wbAdsResolveRunId_(null), st);
    out.stats = (s ? s.status + ' · суток ' + (s.days || 0) + ' · OK ' + (s.days_ok || 0) : 'нет результата') +
                ' · добирались: ' + pending.days.join(', ');
  }

  console.log('Ставки и статистика ключей WB: ' + JSON.stringify(out) +
              ' · ' + Math.round((Date.now() - t0) / 1000) + ' с');
  return out;
}

/** Есть ли за сегодня (МСК) снимок ставок со статусом OK. При ошибке чтения — false (снимаем заново). */
function wbAdsKwBidsOkToday_(today) {
  try {
    var c = getBqConfig_();
    var r = bqQuery_(
      'SELECT COUNT(*) FROM `' + c.projectId + '.' + c.datasetId + '.RAW_WB_ADV_QUERY_BIDS_RUNS`\n' +
      "WHERE CAST(snapshot_date AS STRING) = '" + today + "' AND status = 'OK'");
    return Number(r.rows[0].f[0].v) > 0;
  } catch (e) {
    console.warn('Проверка снимка ставок не удалась, снимаем заново: ' + ((e && e.message) || e));
    return false;
  }
}

/** Сутки D-7…D-1 без OK/EMPTY в покрытии, включая сутки без единой попытки. Старые дыры не трогает. */
function wbAdsKwStatsPending_() {
  var out = { ok: false, days: [], error: '' };
  try {
    var c = getBqConfig_();
    var sql =
      'SELECT FORMAT_DATE("%Y-%m-%d", d) AS day\n' +
      'FROM UNNEST(GENERATE_DATE_ARRAY(DATE_SUB(CURRENT_DATE("Europe/Moscow"), INTERVAL ' + WB_ADS_KW_STATS_WINDOW_DAYS_ + ' DAY),\n' +
      '                                DATE_SUB(CURRENT_DATE("Europe/Moscow"), INTERVAL 1 DAY))) AS d\n' +
      'LEFT JOIN `' + c.projectId + '.' + c.datasetId + '.V_ADV_QUERY_STATS_COVERAGE` cv\n' +
      '  ON cv.period_from = FORMAT_DATE("%Y-%m-%d", d)\n' +
      'WHERE IFNULL(cv.ok_runs, 0) = 0 AND IFNULL(cv.empty_runs, 0) = 0\n' +
      'ORDER BY d';
    var r = bqQuery_(sql);
    var rows = (r && r.rows) || [];
    for (var i = 0; i < rows.length; i++) out.days.push(String(rows[i].f[0].v));
    out.ok = true;
  } catch (e) {
    out.error = (e && e.message) || String(e);
  }
  return out;
}

/** Ставит (пересоздаёт) ежедневный триггер wbAdsKeywordsDaily. Повторный вызов безопасен. */
function wbAdsKeywordsInstallTrigger() {
  var fn = 'wbAdsKeywordsDaily';
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === fn) ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger(fn).timeBased().everyDays(1).atHour(WB_ADS_KW_TRIGGER_HOUR_).nearMinute(30).create();
  console.log('Триггер ' + fn + ': ежедневно ~' + WB_ADS_KW_TRIGGER_HOUR_ + ':30, часовой пояс проекта ' +
              Session.getScriptTimeZone());
}
