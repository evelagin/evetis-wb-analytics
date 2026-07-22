/**
 * ══════════════════════════════════════════════════════════════
 * EVETIS WB — WbAdsDaily.gs   (Реклама → production)
 *
 * Автозагрузка рекламы: тонкая обёртка над существующими per-source
 * загрузчиками WbAdsRawLoader.gs. Гоняет campaigns + costs + fullstats
 * за СКОЛЬЗЯЩИЕ 7 дней (встроенная пересверка последних дней), пишет в
 * BigQuery через флаг WB_ADS_BQ_SINK. Дедуп — во вьюхах V_ADV_* .
 *
 * Существующие файлы НЕ меняет — только добавляет этот файл.
 * Без UI (безопасно для триггера). Поисковые кластеры НЕ грузим
 * (это sample/Фаза D, не нужен базовой воронке).
 *
 * Порядок: campaigns (DIM, дёшево) → costs (~сек) → fullstats (тяжёлый,
 * последним: если упрётся в тайм-бюджет 6 мин — предыдущие уже записаны).
 *
 * Fail-closed: WB_ADS_BQ_SINK выключен → ERROR (не пишем молча в листы).
 * Триггер ставит владелец ПОСЛЕ ручной приёмки (wbAdsInstallDailyTrigger).
 *
 * Переиспользует: loadWbAdsCampaignsRaw / loadWbAdsCostsRaw /
 * loadWbAdsFullstatsRaw, wbAdsBqSinkOn_, wbAdsLast7Range_,
 * wbAdsRawNewRunId_, wbAdsRawWriteStatus_, WB_ADS_RAW_RUN_T0_ (общий
 * тайм-бюджет), getBqConfig_/bqQuery_ (свежесть).
 * ══════════════════════════════════════════════════════════════
 */

var WB_ADS_DAILY_TRIGGER_FN_    = 'runWbAdsDaily';
var WB_ADS_DAILY_LOCK_WAIT_MS_  = 30000;   // ждём общий ScriptLock (часовые заказы/продажи короткие)
var WB_ADS_DAILY_STALE_DAYS_    = 2;       // fullstats за вчера — норма; > N дней = устаревание

/**
 * Один суточный прогон рекламы под общим ScriptLock.
 * Возвращает { status, run_id, results, fullstats_max_date, stale }.
 */
function runWbAdsDaily() {
  var t0 = Date.now();
  var lock = LockService.getScriptLock();
  if (!lock.tryLock(WB_ADS_DAILY_LOCK_WAIT_MS_)) {
    console.log('WB Ads daily: SKIPPED_LOCKED (общий ScriptLock занят)');
    return { status: 'SKIPPED_LOCKED' };
  }

  var runId = '';
  var rng = null;
  try {
    runId = wbAdsRawNewRunId_();

    // Fail-closed: без BQ-приёмника не пишем молча в листы.
    if (typeof wbAdsBqSinkOn_ !== 'function' || !wbAdsBqSinkOn_()) {
      console.error('WB Ads daily: WB_ADS_BQ_SINK выключен — прогон отменён');
      wbAdsDailyWriteStatus_(runId, '', '', 'ERROR', 'WB_ADS_BQ_SINK выключен');
      return { status: 'ERROR', run_id: runId, error_message: 'WB_ADS_BQ_SINK выключен' };
    }

    rng = wbAdsLast7Range_();
    WB_ADS_RAW_RUN_T0_ = t0;   // общий тайм-бюджет для per-source загрузчиков

    console.log('═══ runWbAdsDaily run_id=' + runId + ' | ' + rng.from + '…' + rng.to + ' ═══');

    var results = [];
    results.push(loadWbAdsCampaignsRaw(runId));                    // DIM (дёшево)
    results.push(loadWbAdsCostsRaw(rng.from, rng.to, runId));      // расходы (~сек)
    results.push(loadWbAdsFullstatsRaw(rng.from, rng.to, runId));  // fullstats — последним

    var summary = results.map(function (x) {
      x = x || {};
      return (x.source || '?') + '=' + (x.status || '?') + '(' + (x.rows != null ? x.rows : 0) + ')';
    }).join(' | ');

    // Иерархия статусов: ошибка любого источника → ERROR; неполнота → PARTIAL;
    // всё успешно, но данные устарели → STALE; иначе OK.
    var hasError = results.some(function (x) {
      return x && x.status && x.status !== 'OK' && x.status !== 'PARTIAL';
    });
    var hasPartial = results.some(function (x) { return x && x.status === 'PARTIAL'; });

    var fresh = wbAdsDailyFreshness_();
    var overall = hasError ? 'ERROR' : (hasPartial ? 'PARTIAL' : (fresh.stale === true ? 'STALE' : 'OK'));
    var elapsed = ((Date.now() - t0) / 1000).toFixed(1);
    var note = summary + ' | fullstats_max=' + fresh.maxDate + ' | ' + elapsed + 'c';

    wbAdsDailyWriteStatus_(runId, rng.from, rng.to, overall, note);
    console.log('runWbAdsDaily ' + overall + ' | ' + note);
    return { status: overall, run_id: runId, results: results,
      fullstats_max_date: fresh.maxDate, stale: fresh.stale };

  } catch (e) {
    var em = (e && e.message) || String(e);
    console.error('runWbAdsDaily ERROR: ' + em);
    wbAdsDailyWriteStatus_(runId, rng ? rng.from : '', rng ? rng.to : '', 'ERROR', 'Исключение: ' + em);
    return { status: 'ERROR', run_id: runId, error_message: em };
  } finally {
    WB_ADS_RAW_RUN_T0_ = null;
    lock.releaseLock();
  }
}

/** Best-effort запись строки в WB_ADS_STATUS (единое место диагностики). Не роняет прогон. */
function wbAdsDailyWriteStatus_(runId, from, to, status, msg) {
  if (typeof wbAdsRawWriteStatus_ !== 'function') return;
  try {
    wbAdsRawWriteStatus_(runId, 'daily_orchestrator', from || '', to || '',
      { status: status, error_message: msg || '' });
  } catch (e) { /* статус — best-effort */ }
}

/**
 * Свежесть fullstats из BQ: max(date) в V_ADV_CAMPAIGN_STATS и признак
 * устаревания (> WB_ADS_DAILY_STALE_DAYS_ от сегодня МСК). Best-effort:
 * ошибка запроса не роняет прогон.
 */
function wbAdsDailyFreshness_() {
  try {
    var c = getBqConfig_();
    var q = bqQuery_('SELECT MAX(`date`) AS d FROM `' +
      c.projectId + '.' + c.datasetId + '.V_ADV_CAMPAIGN_STATS`');
    var v = (q.rows && q.rows.length && q.rows[0].f[0].v) ? String(q.rows[0].f[0].v) : '';
    var maxDate = v ? v.substring(0, 10) : '';
    if (!maxDate) return { maxDate: '', stale: true };
    var today = Utilities.formatDate(new Date(), 'Europe/Moscow', 'yyyy-MM-dd');
    var diffDays = (new Date(today + 'T00:00:00Z') - new Date(maxDate + 'T00:00:00Z')) / 86400000;
    return { maxDate: maxDate, stale: diffDays > WB_ADS_DAILY_STALE_DAYS_ };
  } catch (e) {
    console.log('WB Ads freshness: недоступно — ' + ((e && e.message) || e));
    return { maxDate: '?', stale: null };
  }
}

// ───────────────────────────────────────────────────────────────
// Триггер (ставит владелец ПОСЛЕ ручной приёмки; в PR не вызываем)
// ───────────────────────────────────────────────────────────────

/** Суточный триггер runWbAdsDaily ~05:00 МСК (после ночной пересверки, до остатков). */
function wbAdsInstallDailyTrigger() {
  // Fail-closed: atHour работает по timezone проекта, а не автоматически по МСК.
  var tz = Session.getScriptTimeZone();
  if (tz !== 'Europe/Moscow') {
    throw new Error('Триггер рекламы требует timezone проекта Europe/Moscow (сейчас: ' + tz +
      '). Смените часовой пояс в Настройках проекта Apps Script и повторите.');
  }
  var trs = ScriptApp.getProjectTriggers(), mine = [];
  for (var i = 0; i < trs.length; i++) if (trs[i].getHandlerFunction() === WB_ADS_DAILY_TRIGGER_FN_) mine.push(trs[i]);
  if (mine.length === 0) {
    ScriptApp.newTrigger(WB_ADS_DAILY_TRIGGER_FN_).timeBased().everyDays(1).atHour(5).nearMinute(0).create();
    console.log('✅ Триггер рекламы создан (~05:00 МСК): ' + WB_ADS_DAILY_TRIGGER_FN_);
    return { created: 1, removed: 0 };
  }
  var removed = 0;
  for (var j = 1; j < mine.length; j++) { ScriptApp.deleteTrigger(mine[j]); removed++; }
  console.log(mine.length === 1 ? 'ℹ️ Триггер рекламы уже есть.' : '⚠️ Удалены дубли: ' + removed);
  return { created: 0, removed: removed };
}

function wbAdsRemoveTrigger() {
  var trs = ScriptApp.getProjectTriggers(), n = 0;
  for (var i = 0; i < trs.length; i++) if (trs[i].getHandlerFunction() === WB_ADS_DAILY_TRIGGER_FN_) { ScriptApp.deleteTrigger(trs[i]); n++; }
  console.log('🗑 Удалено триггеров рекламы: ' + n);
  return { removed: n };
}
