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

// PR-Mart3a REV5: fail-closed расчёт итогового статуса рекламного прогона.
// 🔴 НЕ МЕНЯТЬ. Ровно три mart-критичных источника: campaigns + costs + fullstats.
// Ads-3 (query bids) и Ads-2 (query stats) СОЗНАТЕЛЬНО сюда не входят: их статус
// не должен влиять на heartbeat и freshness-гейт витрины (см. врезки у вызовов
// в runWbAdsDaily). Витрина ни ставок, ни query-level статистики не потребляет.
var WB_ADS_EXPECTED_SOURCES_ = 3;  // campaigns + costs + fullstats

/**
 * Чистый расчёт итогового статуса рекламного прогона (без API/BQ — тестируется offline).
 * Fail-closed: отсутствующий/битый результат, нет поля status, неизвестный статус ИЛИ
 * неверное число результатов => ERROR. Иначе null/{}/сырой статус проскочил бы ДО whitelist
 * на этапе overall и дал бы ложный OK/STALE => ложный COMPLETE-heartbeat => Mart3 собрал бы
 * витрину на неполной рекламе.
 *   OK+OK+OK               => OK (или STALE, если stale===true)
 *   >=1 PARTIAL без ошибок => PARTIAL
 *   null / {} / нет status / неизвестный статус / не 3 результата / не массив => ERROR
 * @param {Array} results  результаты трёх per-source загрузчиков
 * @param {boolean|null} stale  признак устаревания fullstats (true|false|null)
 * @return {string} 'OK' | 'PARTIAL' | 'STALE' | 'ERROR'
 */
function wbAdsOverallStatus_(results, stale) {
  var invalid =
    !Array.isArray(results) ||
    results.length !== WB_ADS_EXPECTED_SOURCES_ ||
    results.some(function (x) { return !x || !x.status; });
  if (invalid) return 'ERROR';

  var badStatus = results.some(function (x) {
    var st = String(x.status).toUpperCase();
    return st !== 'OK' && st !== 'PARTIAL';
  });
  if (badStatus) return 'ERROR';

  var anyPartial = results.some(function (x) {
    return String(x.status).toUpperCase() === 'PARTIAL';
  });
  if (anyPartial) return 'PARTIAL';

  return stale === true ? 'STALE' : 'OK';
}

/**
 * Регрессионный self-тест wbAdsOverallStatus_ (offline, без API/BQ).
 * Запускать из редактора Apps Script. Бросает Error при первом расхождении.
 */
function wbAdsSelfTestOverallStatus() {
  function r(status) { return { source: 's', status: status, rows: 0 }; }
  var cases = [
    { n: 'OK+OK+OK',              in: [r('OK'), r('OK'), r('OK')],          stale: false, exp: 'OK' },
    { n: 'OK+OK+OK stale=true',   in: [r('OK'), r('OK'), r('OK')],          stale: true,  exp: 'STALE' },
    { n: 'OK+OK+OK stale=null',   in: [r('OK'), r('OK'), r('OK')],          stale: null,  exp: 'OK' },
    { n: 'один PARTIAL',          in: [r('OK'), r('PARTIAL'), r('OK')],     stale: false, exp: 'PARTIAL' },
    { n: 'PARTIAL важнее STALE',  in: [r('OK'), r('PARTIAL'), r('OK')],     stale: true,  exp: 'PARTIAL' },
    { n: 'ERROR перебивает всё',  in: [r('OK'), r('ERROR'), r('OK')],       stale: true,  exp: 'ERROR' },
    { n: 'null-результат',        in: [r('OK'), null, r('OK')],             stale: false, exp: 'ERROR' },
    { n: 'пустой объект {}',      in: [r('OK'), {}, r('OK')],               stale: false, exp: 'ERROR' },
    { n: 'нет поля status',       in: [r('OK'), { source: 's' }, r('OK')],  stale: false, exp: 'ERROR' },
    { n: 'неизвестный статус',    in: [r('OK'), r('WAT'), r('OK')],         stale: false, exp: 'ERROR' },
    { n: 'мало результатов (2)',  in: [r('OK'), r('OK')],                   stale: false, exp: 'ERROR' },
    { n: 'много результатов (4)', in: [r('OK'), r('OK'), r('OK'), r('OK')], stale: false, exp: 'ERROR' },
    { n: 'не массив',             in: null,                                  stale: false, exp: 'ERROR' },
    { n: 'регистр ok/partial',    in: [r('ok'), r('partial'), r('OK')],     stale: false, exp: 'PARTIAL' }
  ];
  var failed = 0;
  for (var i = 0; i < cases.length; i++) {
    var c = cases[i];
    var got = wbAdsOverallStatus_(c.in, c.stale);
    var ok = got === c.exp;
    if (!ok) failed++;
    Logger.log((ok ? 'OK   ' : 'FAIL ') + c.n + ' => ожид. ' + c.exp + ', получено ' + got);
  }
  if (failed > 0) throw new Error('wbAdsSelfTestOverallStatus: провалено ' + failed + ' из ' + cases.length);
  Logger.log('wbAdsSelfTestOverallStatus: все ' + cases.length + ' кейсов прошли OK');
  return { passed: cases.length, failed: 0 };
}

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
  var ingestRunIdAds = null;   // PR-Mart3a: id строки heartbeat-журнала
  var heartbeatFinalized = false;  // Ads-3: после финализации catch не трогает heartbeat
  try {
    runId = wbAdsRawNewRunId_();

    // Fail-closed: без BQ-приёмника не пишем молча в листы.
    if (typeof wbAdsBqSinkOn_ !== 'function' || !wbAdsBqSinkOn_()) {
      console.error('WB Ads daily: WB_ADS_BQ_SINK выключен — прогон отменён');
      wbAdsDailyWriteStatus_(runId, '', '', 'ERROR', 'WB_ADS_BQ_SINK выключен');
      // PR-Mart3a: early-return — фиксируем неуспех явно (точный период API ещё не вычислен,
      // берём закрытый день; в норме он совпадает с rng.to).
      ingestRunIdAds = ingestRunStart_('ads', ingestClosedDayMsk_(), 'SCHEDULED');
      ingestRunError_(ingestRunIdAds, 'ADS_SINK_OFF', 'WB_ADS_BQ_SINK выключен');
      return { status: 'ERROR', run_id: runId, error_message: 'WB_ADS_BQ_SINK выключен' };
    }

    rng = wbAdsLast7Range_();
    // PR-Mart3a: logical_period = ЦЕЛЕВОЙ ПЕРИОД API (period_to), а не дата запуска.
    ingestRunIdAds = ingestRunStart_('ads', rng.to, 'SCHEDULED');
    WB_ADS_RAW_RUN_T0_ = t0;   // общий тайм-бюджет для per-source загрузчиков

    console.log('═══ runWbAdsDaily run_id=' + runId + ' | ' + rng.from + '…' + rng.to + ' ═══');

    var results = [];
    results.push(loadWbAdsCampaignsRaw(runId));                    // DIM (дёшево)
    // Stage 3B.1: у расходов СВОЁ окно, независимое от общего rng.
    //   🔴 ФАЗА A: D−7 … D−1 — то же окно, что и раньше. Расширять его в Фазе A нельзя:
    //   union умеет только расти, и лишние перечитывания подняли бы суммы FACT ещё до
    //   cutover. Переход на D−14 … D−1 — шаг B4a Фазы B (наблюдавшаяся ревизия биллинга
    //   приходила на D+7, ровно на границе 7-дневного окна).
    //   Размер окна задаётся ОДНОЙ константой WB_ADS_COSTS_OPERATIONAL_DAYS_.
    //   Окна campaigns и fullstats НЕ меняются.
    var costsRng = wbAdsCostsRangeBack_(WB_ADS_COSTS_OPERATIONAL_DAYS_, 1);
    results.push(loadWbAdsCostsRaw(costsRng.from, costsRng.to, runId));   // расходы (~сек)
    results.push(loadWbAdsFullstatsRaw(rng.from, rng.to, runId));  // fullstats — последним

    var summary = results.map(function (x) {
      x = x || {};
      return (x.source || '?') + '=' + (x.status || '?') + '(' + (x.rows != null ? x.rows : 0) + ')';
    }).join(' | ');

    // PR-Mart3a REV5: итог вынесен в чистую wbAdsOverallStatus_ (fail-closed, offline self-test).
    // null/{}/без-status/неизвестный статус/не 3 результата => ERROR (не проскочит в OK до whitelist).
    var fresh = wbAdsDailyFreshness_();
    var overall = wbAdsOverallStatus_(results, fresh.stale);
    var elapsed = ((Date.now() - t0) / 1000).toFixed(1);
    var note = summary + ' | fullstats_max=' + fresh.maxDate + ' | ' + elapsed + 'c';

    wbAdsDailyWriteStatus_(runId, rng.from, rng.to, overall, note);
    console.log('runWbAdsDaily ' + overall + ' | ' + note);
    // PR-Mart3a: whitelist (OK/STALE = успех; PARTIAL/ERROR -> ERROR-heartbeat).
    var adsRowsTotal = results.reduce(function (a, x) { return a + ((x && x.rows) || 0); }, 0);
    ingestFinalizeByStatus_(ingestRunIdAds, 'ads', overall, adsRowsTotal, adsRowsTotal, note);
    heartbeatFinalized = true;   // дальше идёт ТОЛЬКО необязательная работа

    // ── Ads-3: снимок ставок по поисковым кластерам ──────────────────────────
    // 🔴 СТРОГО ПОСЛЕ ingestFinalizeByStatus_. Причина: try/catch спасает от
    //    исключения, но НЕ от жёсткого таймаута Apps Script (6 мин) — тот убивает
    //    execution без всякого catch. Если бы Ads-3 стоял до финализации и съел
    //    остаток бюджета, mart-критичный RAW был бы уже загружен, а heartbeat
    //    'ads' остался бы незакрытым → freshness-гейт не увидел бы COMPLETE →
    //    сборка MART_SKU_DAILY встала бы из-за необязательного источника.
    //    Теперь худший случай таймаута здесь = отсутствие снимка ставок за день,
    //    и только это; критичный путь уже полностью завершён и зафиксирован.
    // 🔴 heartbeatFinalized блокирует catch-ветку: после финализации она НЕ имеет
    //    права переписать успешный heartbeat в ERROR из-за сбоя Ads-3.
    // ⚠️ Список пар Ads-3 собирает СВОИМИ вызовами /adv/v1/promotion/count и
    //    /api/advert/v2/adverts. Он НЕ читает RAW-справочник, загруженный выше в
    //    этом же прогоне: это дополнительные read-запросы к WB, и состав пар может
    //    отличаться от загруженного справочника, если кампании изменились между
    //    вызовами. Сделано намеренно — снимок ставок должен опираться на состояние
    //    кампаний на момент СНИМКА, а не на состояние начала прогона.
    // WB_ADS_EXPECTED_SOURCES_ остаётся 3 — Ads-3 в results не входит.
    var bidsResult = { source: 'raw_query_bids', status: 'SKIPPED', rows: 0, pairs: 0 };
    try {
      if (typeof loadWbAdsQueryBidsRaw === 'function') {
        bidsResult = loadWbAdsQueryBidsRaw(runId) || bidsResult;
      }
    } catch (eBids) {
      bidsResult = { source: 'raw_query_bids', status: 'FAILED', rows: 0, pairs: 0 };
      console.error('  query_bids изолированный сбой: ' + ((eBids && eBids.message) || eBids));
    }
    console.log('runWbAdsDaily query_bids=' + bidsResult.status +
      '(' + (bidsResult.rows || 0) + ' строк, ' + (bidsResult.pairs || 0) + ' пар)');

    // ── Ads-2: суточные срезы query-level статистики ─────────────────────────
    // 🔴 СТРОГО ПОСЛЕ Ads-3, и порядок между ними НЕ произволен:
    //    снимок ставок (Ads-3) невосстановим — эндпоинта истории у WB нет, сутки без
    //    снимка потеряны навсегда. Срез статистики (Ads-2) восстановим: история у WB
    //    есть с апреля, пропущенные сутки добираются повторным прогоном.
    //    Значит при нехватке тайм-бюджета жертвовать надо этим источником, а не
    //    Ads-3 — отсюда позиция в коде, а не эстетика порядка вызовов.
    // 🔴 Как и Ads-3: после ingestFinalizeByStatus_, в results НЕ входит,
    //    WB_ADS_EXPECTED_SOURCES_ остаётся 3, heartbeat не трогается. Витрина
    //    query-level не потребляет, и сбой этого источника не имеет права
    //    остановить сборку MART_SKU_DAILY.
    // ⚠️ Список пар Ads-2 берёт из V_ADV_CAMPAIGN_STATS (наш BigQuery), а не из
    //    справочника WB — осознанное ограничение полноты, см. ADS2_DESIGN §5.2.
    var qstatsResult = { source: 'raw_query_stats', status: 'SKIPPED', rows: 0, days: 0 };
    try {
      if (typeof loadWbAdsQueryStatsRaw === 'function') {
        qstatsResult = loadWbAdsQueryStatsRaw(runId) || qstatsResult;
      }
    } catch (eQs) {
      qstatsResult = { source: 'raw_query_stats', status: 'FAILED', rows: 0, days: 0 };
      console.error('  query_stats изолированный сбой: ' + ((eQs && eQs.message) || eQs));
    }
    console.log('runWbAdsDaily query_stats=' + qstatsResult.status +
      '(' + (qstatsResult.rows || 0) + ' строк, ' + (qstatsResult.days || 0) + ' суток)');

    return { status: overall, run_id: runId, results: results,
      query_bids: bidsResult,
      query_stats: qstatsResult,
      fullstats_max_date: fresh.maxDate, stale: fresh.stale };

  } catch (e) {
    var em = (e && e.message) || String(e);
    console.error('runWbAdsDaily ERROR: ' + em);
    wbAdsDailyWriteStatus_(runId, rng ? rng.from : '', rng ? rng.to : '', 'ERROR', 'Исключение: ' + em);
    // 🔴 Если heartbeat уже финализирован, сбой мог прийти только из необязательной
    //    части (Ads-3). Переписывать успешный heartbeat в ERROR из-за неё нельзя —
    //    это остановило бы витрину ровно так же, как BLOCKER 1.
    if (!heartbeatFinalized) {
      ingestRunError_(ingestRunIdAds, 'ADS_EXCEPTION', em);   // PR-Mart3a: catch-ветка
    } else {
      console.error('  (heartbeat уже финализирован — не трогаем; сбой в необязательной части)');
    }
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
