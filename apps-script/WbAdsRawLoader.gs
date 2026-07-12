/**
 * ══════════════════════════════════════════════════════════════
 * EVETIS WB — WbAdsRawLoader.gs  v1.0  (PR #17B)
 *
 * PRODUCTION RAW-ЗАГРУЗЧИК WB Ads API (только ручной, только за период).
 *
 * Опирается на диагностический WbAdsProbe.gs (PR #17A / #17A.1) и
 * переиспользует его хелперы из того же Apps Script проекта:
 *   getWbAdsToken_, wbAdsHttp_, wbAdsFetchCampaigns_,
 *   wbAdsMakeResult_, wbAdsFinish_, wbAdsNow_, wbAdsClip_,
 *   wbAdsResolveRunId_, ensureWbAdsStatusSheet_, writeWbAdsStatusRow_,
 *   а также константы WB_ADS_API_HOST_, WB_ADS_IDS_BATCH_,
 *   WB_ADS_STATS_STATUSES_, WB_ADS_FULLSTATS_PAUSE_MS_,
 *   WB_ADS_NORMQUERY_PAUSE_MS_, WB_ADS_TZ_.
 *
 * SCOPE PR #17B (строго):
 *   • Только ручная RAW-загрузка рекламы за период.
 *   • НЕ трогает CLEAN_WB_DAILY, UNIT_SKU_DAILY, PNL_TOTAL,
 *     RAW_WB_FINANCE, WbDailyRefresh / runWbDailyRefresh.
 *   • НЕ подключается к ежедневному refresh.
 *   • НЕ исторический загрузчик (нет автозагрузки с сентября 2024).
 *   • runWbAdsProbeAll() и probe-функции НЕ меняются.
 *
 * RAW-листы (создаются/расширяются аддитивно):
 *   RAW_WB_ADV_CAMPAIGNS, RAW_WB_ADV_CAMPAIGN_STATS,
 *   RAW_WB_ADV_BOOSTER_STATS, RAW_WB_ADV_SEARCH_CLUSTERS, RAW_WB_ADV_COSTS.
 *   В каждой строке обязательны: load_ts, run_id, period_from, period_to,
 *   source_method, processed_status, raw_json.
 *
 * Ошибки пишутся в WB_ADS_STATUS и НЕ роняют всю загрузку
 * (один упавший advertId / окно / связка — не критично).
 *
 * Fullstats имеет resilient fallback: batch≤50 → пачки по 10 → single advertId.
 * ══════════════════════════════════════════════════════════════
 */


// ═══════════════════════════════════════
// КОНФИГ (RAW-специфичный; общие константы — из WbAdsProbe.gs)
// ═══════════════════════════════════════

var WB_ADV_RAW_CAMPAIGNS_SHEET_       = 'RAW_WB_ADV_CAMPAIGNS';
var WB_ADV_RAW_CAMPAIGN_STATS_SHEET_  = 'RAW_WB_ADV_CAMPAIGN_STATS';
var WB_ADV_RAW_BOOSTER_STATS_SHEET_   = 'RAW_WB_ADV_BOOSTER_STATS';
var WB_ADV_RAW_SEARCH_CLUSTERS_SHEET_ = 'RAW_WB_ADV_SEARCH_CLUSTERS';
var WB_ADV_RAW_COSTS_SHEET_           = 'RAW_WB_ADV_COSTS';

var WB_ADV_RAW_CAMPAIGNS_HEADERS_ = [
  'load_ts', 'run_id', 'period_from', 'period_to', 'source_method', 'processed_status',
  'advertId', 'type', 'status', 'payment_type', 'name', 'campName',
  'create_time', 'change_time', 'start_time', 'end_time',
  'nm_ids', 'nm_count', 'raw_json'
];

var WB_ADV_RAW_CAMPAIGN_STATS_HEADERS_ = [
  'load_ts', 'run_id', 'period_from', 'period_to', 'source_method', 'processed_status',
  'advertId', 'date', 'appType', 'nmId', 'name',
  'views', 'clicks', 'ctr', 'cpc', 'cr', 'atbs', 'orders', 'canceled', 'shks',
  'sum', 'sum_price', 'source_level', 'raw_json'
];

var WB_ADV_RAW_BOOSTER_STATS_HEADERS_ = [
  'load_ts', 'run_id', 'period_from', 'period_to', 'source_method', 'processed_status',
  'advertId', 'date', 'nmId', 'avg_position', 'raw_json'
];

var WB_ADV_RAW_SEARCH_CLUSTERS_HEADERS_ = [
  'load_ts', 'run_id', 'period_from', 'period_to', 'source_method', 'processed_status',
  'advert_id', 'nm_id', 'norm_query',
  'views', 'clicks', 'ctr', 'cpc', 'cpm', 'avg_pos', 'atbs', 'orders', 'raw_json'
];

var WB_ADV_RAW_COSTS_HEADERS_ = [
  'load_ts', 'run_id', 'period_from', 'period_to', 'source_method', 'processed_status',
  'updTime', 'updDate', 'updNum', 'updSum', 'advertId',
  'campName', 'advertType', 'paymentType', 'advertStatus', 'raw_json'
];

/** Лимиты периодов WB: fullstats и upd — максимум 31 день на запрос. */
var WB_ADV_RAW_MAX_DAYS_ = 31;

/**
 * Search clusters — SAMPLE/диагностика, НЕ полный RAW.
 * Каждый прогон берёт первые N связок advertId|nmId без ротации/offset/
 * checkpoint → остальные связки в BQ не попадают. Не строить на
 * RAW_WB_ADV_SEARCH_CLUSTERS / V_ADV_SEARCH_CLUSTERS требование полноты.
 * Полный сбор ключей — отдельная задача Фазы D (checkpoint по паре).
 */
var WB_ADS_SEARCH_MAX_PAIRS_RAW_ = 20;

/** Тайм-бюджеты (Apps Script hard limit ~6 мин). */
var WB_ADS_RAW_TIME_BUDGET_MS_ = 240000;  // одиночный ручной loader: ~4 мин
var WB_ADS_RAW_RUN_BUDGET_MS_  = 320000;  // общий orchestrator: ~5.3 мин

var WB_ADV_RAW_JSON_MAX_ = 45000;         // обрезка raw_json под лимит ячейки

/** Устанавливается orchestrator'ом, чтобы тайм-бюджет был общим на весь прогон. */
var WB_ADS_RAW_RUN_T0_ = null;


// ═══════════════════════════════════════
// МЕНЮ
//   Подключить в Menu v2 → onOpen() одной строкой:  addWbAdsRawLoaderMenu();
// ═══════════════════════════════════════

function addWbAdsRawLoaderMenu() {
  SpreadsheetApp.getUi()
    .createMenu('📥 WB Ads RAW')
    .addItem('Загрузить Ads RAW за 7 дней', 'loadWbAdsRawLast7Days')
    .addItem('Загрузить Ads RAW за период', 'loadWbAdsRawPeriodPrompt')
    .addSeparator()
    .addItem('Только кампании (RAW)', 'loadWbAdsCampaignsRaw')
    .addItem('Только fullstats (RAW, 7 дней)', 'loadWbAdsFullstatsRawLast7Days')
    .addItem('Только расходы upd (RAW, 7 дней)', 'loadWbAdsCostsRawLast7Days')
    .addItem('Только поисковые кластеры (RAW, 7 дней)', 'loadWbAdsSearchClustersRawLast7Days')
    .addToUi();
}


// ═══════════════════════════════════════
// ПУБЛИЧНЫЕ ОРКЕСТРАТОРЫ
// ═══════════════════════════════════════

/** Загрузка всех RAW-источников рекламы за последние 7 дней. */
function loadWbAdsRawLast7Days() {
  var rng = wbAdsLast7Range_();
  return loadWbAdsRawPeriod(rng.from, rng.to);
}

/**
 * Загрузка всех RAW-источников рекламы за произвольный период.
 * Порядок: лёгкие источники раньше, fullstats — последним (если упрётся
 * в тайм-бюджет, ранее загруженные листы уже зафиксированы).
 */
function loadWbAdsRawPeriod(periodFrom, periodTo) {
  var rng = wbAdsRawNormalizeRange_(periodFrom, periodTo);
  var runId = wbAdsRawNewRunId_();
  var t0 = Date.now();
  WB_ADS_RAW_RUN_T0_ = t0; // общий тайм-бюджет на весь прогон

  console.log('═══ loadWbAdsRawPeriod() v1.0 СТАРТ run_id=' + runId +
    ' период ' + rng.from + '…' + rng.to + ' ═══');

  // №3: один прогон оркестратора НЕ гарантирует полноту на длинном
  // периоде — fullstats может не успеть за 6-мин лимит после пауз search
  // clusters (причина — тайм-бюджет и rate-limit, окна wbAdsSplitPeriod_
  // неперекрывающиеся). Backfill истории — ПО ИСТОЧНИКАМ, не оркестратором.
  var spanDays = Math.round(
    (new Date(rng.to + 'T00:00:00Z') - new Date(rng.from + 'T00:00:00Z')) / 86400000) + 1;
  if (spanDays > WB_ADV_RAW_MAX_DAYS_) {
    console.warn('⚠️ Период ' + spanDays + ' дн (> ' + WB_ADV_RAW_MAX_DAYS_ +
      '). Оркестратор НЕ гарантирует полный fullstats — для истории грузите ' +
      'по источникам (loadWbAdsFullstatsRaw малыми окнами).');
  }

  var results = [];
  try {
    results.push(loadWbAdsCampaignsRaw(runId));
    results.push(loadWbAdsCostsRaw(rng.from, rng.to, runId));
    results.push(loadWbAdsSearchClustersRaw(rng.from, rng.to, runId));
    results.push(loadWbAdsFullstatsRaw(rng.from, rng.to, runId));
  } finally {
    WB_ADS_RAW_RUN_T0_ = null;
  }

  var elapsed = ((Date.now() - t0) / 1000).toFixed(1);
  console.log('═══ loadWbAdsRawPeriod() ЗАВЕРШЕНО за ' + elapsed + ' сек ═══');

  try {
    var ui = SpreadsheetApp.getUi();
    var msg = 'run_id: ' + runId + '\nпериод: ' + rng.from + ' … ' + rng.to + '\n\n';
    for (var i = 0; i < results.length; i++) {
      var x = results[i] || {};
      msg += '• ' + (x.source || '?') + ': ' + (x.status || '?') +
        ' (rows=' + (x.rows != null ? x.rows : 0) + ')\n';
    }
    var bqOn = (typeof wbAdsBqSinkOn_ === 'function' && wbAdsBqSinkOn_());
    var destination = bqOn ? 'BigQuery-таблицах RAW_WB_ADV_*' : 'листах RAW_WB_ADV_*';
    msg += '\nДанные — в ' + destination + '. Ошибки — в WB_ADS_STATUS.\n' +
      '⚠️ RAW-загрузка рекламы. CLEAN/UNIT/PNL/RAW_WB_FINANCE и daily refresh НЕ затронуты.\n' +
      'Время: ' + elapsed + ' сек';
    ui.alert('📥 WB Ads RAW', msg, ui.ButtonSet.OK);
  } catch (eUi) { /* нет UI-контекста — норм */ }

  return { run_id: runId, period_from: rng.from, period_to: rng.to, results: results };
}

/** Меню-обёртка: спрашивает период через prompt и вызывает loadWbAdsRawPeriod. */
function loadWbAdsRawPeriodPrompt() {
  var ui = SpreadsheetApp.getUi();
  var resp = ui.prompt(
    '📥 WB Ads RAW за период',
    'Введите период: YYYY-MM-DD YYYY-MM-DD (from to). Напр.: 2025-05-01 2025-05-28',
    ui.ButtonSet.OK_CANCEL
  );
  if (resp.getSelectedButton() !== ui.Button.OK) return;
  var parts = String(resp.getResponseText() || '').trim().split(/\s+/);
  var re = /^\d{4}-\d{2}-\d{2}$/;
  if (parts.length < 2 || !re.test(parts[0]) || !re.test(parts[1])) {
    ui.alert('Неверный формат. Пример: 2025-05-01 2025-05-28');
    return;
  }
  loadWbAdsRawPeriod(parts[0], parts[1]);
}


// ═══════════════════════════════════════
// BACKFILL-ОБЁРТКИ (no-arg, для запуска из выпадающего списка редактора)
//   Кнопка «Выполнить» не передаёт аргументы → период зашит явно.
//   Только per-source (без оркестратора). Правь даты под нужный период.
// ═══════════════════════════════════════

/** Backfill РАСХОДОВ (upd) за 90 ЗАВЕРШЁННЫХ дней (по 11.07, без неполного 12-го). */
function loadWbAdsCostsBackfill90() {
  return loadWbAdsCostsRaw('2026-04-13', '2026-07-11');
}

/** Расходы (upd) за период — prompt (BQ-совместимый, пишет через флаг sink). */
function loadWbAdsCostsRawPeriodPrompt() {
  var ui = SpreadsheetApp.getUi();
  var resp = ui.prompt('WB Ads — расходы за период',
    'Введите даты через пробел: YYYY-MM-DD YYYY-MM-DD\nНапр.: 2026-04-13 2026-04-30',
    ui.ButtonSet.OK_CANCEL);
  if (resp.getSelectedButton() !== ui.Button.OK) return;
  var p = String(resp.getResponseText() || '').trim().split(/\s+/);
  var re = /^\d{4}-\d{2}-\d{2}$/;
  if (p.length !== 2 || !re.test(p[0]) || !re.test(p[1])) {
    ui.alert('Неверный формат. Пример: 2026-04-13 2026-04-30'); return;
  }
  return loadWbAdsCostsRaw(p[0], p[1]);
}

/** fullstats за период — prompt (BQ-совместимый; без replace-slice по листам). */
function loadWbAdsFullstatsRawPeriodPrompt() {
  var ui = SpreadsheetApp.getUi();
  var resp = ui.prompt('WB Ads — fullstats за период',
    'Введите даты через пробел: YYYY-MM-DD YYYY-MM-DD\nНачни с 7 дней, напр.: 2026-04-13 2026-04-19',
    ui.ButtonSet.OK_CANCEL);
  if (resp.getSelectedButton() !== ui.Button.OK) return;
  var p = String(resp.getResponseText() || '').trim().split(/\s+/);
  var re = /^\d{4}-\d{2}-\d{2}$/;
  if (p.length !== 2 || !re.test(p[0]) || !re.test(p[1])) {
    ui.alert('Неверный формат. Пример: 2026-04-13 2026-04-19'); return;
  }
  return loadWbAdsFullstatsRaw(p[0], p[1]);
}


// ═══════════════════════════════════════
// LAST-7 WRAPPER'Ы (entry points для меню per-source)
//   Меню Apps Script вызывает функции без аргументов, поэтому период
//   подставляется явно через wbAdsLast7Range_().
// ═══════════════════════════════════════

/** Меню-обёртка: fullstats RAW за последние 7 дней. */
function loadWbAdsFullstatsRawLast7Days() {
  var rng = wbAdsLast7Range_();
  return loadWbAdsFullstatsRaw(rng.from, rng.to);
}

/** Меню-обёртка: расходы upd RAW за последние 7 дней. */
function loadWbAdsCostsRawLast7Days() {
  var rng = wbAdsLast7Range_();
  return loadWbAdsCostsRaw(rng.from, rng.to);
}

/** Меню-обёртка: поисковые кластеры RAW за последние 7 дней. */
function loadWbAdsSearchClustersRawLast7Days() {
  var rng = wbAdsLast7Range_();
  return loadWbAdsSearchClustersRaw(rng.from, rng.to);
}


// ═══════════════════════════════════════
// ПУБЛИЧНЫЙ: CAMPAIGNS RAW
// ═══════════════════════════════════════

/**
 * RAW кампаний: /adv/v1/promotion/count + /api/advert/v2/adverts (ids ≤ 50).
 * Один row на кампанию (campaign-level): advertId, type/status, payment_type,
 * name/campName, timestamps, nmId-связки (nm_ids), raw_json.
 * @param {string=} runId
 */
function loadWbAdsCampaignsRaw(runId) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var rid = wbAdsResolveRunId_(runId);
  var srcLabel = 'raw_campaigns';

  var tok = getWbAdsToken_();
  if (!tok) {
    wbAdsRawWriteStatus_(rid, srcLabel, '', '', { status: 'BLOCKED', error_message: 'Нет WB Promotion токена' });
    return { source: srcLabel, status: 'BLOCKED', rows: 0 };
  }

  try {
    // 1) count — полный список advertId + type/status по группам
    var cResp = wbAdsHttp_('get', WB_ADS_API_HOST_ + '/adv/v1/promotion/count', tok.token, null);
    if (!cResp.ok) {
      wbAdsRawWriteStatus_(rid, srcLabel, '', '', {
        http_status: cResp.code, status: 'FAILED',
        error_message: 'count HTTP ' + cResp.code + ': ' + wbAdsClip_(cResp.body)
      });
      return { source: srcLabel, status: 'FAILED', rows: 0 };
    }

    var groups = (cResp.json && (cResp.json.adverts || (cResp.json.data && cResp.json.data.adverts))) || [];
    var ids = [], idMeta = {};
    for (var g = 0; g < groups.length; g++) {
      var gType = groups[g].type, gStatus = groups[g].status;
      var list = groups[g].advert_list || groups[g].advertList || [];
      for (var k = 0; k < list.length; k++) {
        var id = list[k].advertId || list[k].advertID || list[k].id;
        if (id == null) continue;
        var idNum = Number(id);
        if (!idMeta[idNum]) {
          idMeta[idNum] = { type: gType, status: gStatus, changeTime: list[k].changeTime || list[k].change_time || '' };
          ids.push(idNum);
        }
      }
    }

    // 2) adverts v2 — детали пачками по 50
    var detailMap = {}, detailHttp = '';
    var chunks = wbAdsChunk_(ids, WB_ADS_IDS_BATCH_);
    for (var ch = 0; ch < chunks.length; ch++) {
      if (ch > 0) Utilities.sleep(1200); // мягкая пауза между пачками деталей
      var aUrl = WB_ADS_API_HOST_ + '/api/advert/v2/adverts?ids=' + chunks[ch].join(',');
      var aResp = wbAdsHttp_('get', aUrl, tok.token, null);
      detailHttp = aResp.code;
      if (!aResp.ok || !aResp.json) continue;
      var adverts = Array.isArray(aResp.json) ? aResp.json
        : ((aResp.json.adverts) || (aResp.json.data && aResp.json.data.adverts) || []);
      for (var a = 0; a < adverts.length; a++) {
        var advId = adverts[a].advertId || adverts[a].id || adverts[a].advertID;
        if (advId != null) detailMap[Number(advId)] = adverts[a];
      }
    }

    // 3) сборка строк (один row на кампанию)
    var sheet = wbAdvRawEnsureSheet_(ss, WB_ADV_RAW_CAMPAIGNS_SHEET_, WB_ADV_RAW_CAMPAIGNS_HEADERS_);
    var rows = [];
    for (var i = 0; i < ids.length; i++) {
      rows.push(wbAdvCampaignRow_(rid, ids[i], idMeta[ids[i]], detailMap[ids[i]] || null));
    }
    var written = wbAdvRawAppendRows_(sheet, rows);

    wbAdsRawWriteStatus_(rid, srcLabel, '', '', {
      campaigns_found: ids.length,
      campaigns_sampled: ids.length,
      rows_or_items_found: written,
      http_status: String(cResp.code) + '/' + detailHttp,
      status: 'OK',
      response_keys_sample: 'campaigns=' + ids.length + '; with_detail=' + Object.keys(detailMap).length
    });
    return { source: srcLabel, status: 'OK', rows: written };
  } catch (e) {
    wbAdsRawWriteStatus_(rid, srcLabel, '', '', { status: 'FAILED', error_message: 'Исключение: ' + e.message });
    return { source: srcLabel, status: 'FAILED', rows: 0 };
  }
}


// ═══════════════════════════════════════
// ПУБЛИЧНЫЙ: FULLSTATS RAW (resilient fallback)
// ═══════════════════════════════════════

/**
 * RAW статистики: /adv/v3/fullstats за период (окна ≤ 31 дня, ids ≤ 50,
 * только статусы 7/9/11). Resilient fallback: batch≤50 → пачки по 10 → single.
 * Один упавший advertId пишется в WB_ADS_STATUS и не роняет загрузку.
 * Плоско: RAW_WB_ADV_CAMPAIGN_STATS (nm-level) + RAW_WB_ADV_BOOSTER_STATS.
 * @param {string=} periodFrom @param {string=} periodTo @param {string=} runId
 */
function loadWbAdsFullstatsRaw(periodFrom, periodTo, runId) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var rid = wbAdsResolveRunId_(runId);
  var rng = wbAdsRawNormalizeRange_(periodFrom, periodTo);
  var srcLabel = 'raw_fullstats';

  var tok = getWbAdsToken_();
  if (!tok) {
    wbAdsRawWriteStatus_(rid, srcLabel, rng.from, rng.to, { status: 'BLOCKED', error_message: 'Нет WB Promotion токена' });
    return { source: srcLabel, status: 'BLOCKED', rows: 0 };
  }

  try {
    var c = wbAdsFetchCampaigns_(tok.token);
    var statsIds = c.statsAdvertIds.slice(0); // статусы 7/9/11
    if (!statsIds.length) {
      wbAdsRawWriteStatus_(rid, srcLabel, rng.from, rng.to, {
        campaigns_found: c.advertIds.length, campaigns_sampled: 0,
        http_status: String(c.countHttp), status: 'SKIPPED',
        error_message: 'Нет кампаний в статусах 7/9/11 — fullstats не запрашивался'
      });
      return { source: srcLabel, status: 'SKIPPED', rows: 0 };
    }

    var statSheet = wbAdvRawEnsureSheet_(ss, WB_ADV_RAW_CAMPAIGN_STATS_SHEET_, WB_ADV_RAW_CAMPAIGN_STATS_HEADERS_);
    var boostSheet = wbAdvRawEnsureSheet_(ss, WB_ADV_RAW_BOOSTER_STATS_SHEET_, WB_ADV_RAW_BOOSTER_STATS_HEADERS_);

    var deadline = wbAdsRawDeadline_();
    var windows = wbAdsSplitPeriod_(rng.from, rng.to, WB_ADV_RAW_MAX_DAYS_);
    var totalStat = 0, totalBoost = 0, totalNoStats = 0, totalSkipped = 0, totalFail = 0, lastCode = '';

    for (var w = 0; w < windows.length; w++) {
      var ctx = wbAdsFullstatsCollect_(tok.token, statsIds, windows[w].from, windows[w].to, rid, deadline);
      lastCode = ctx.lastCode;

      var flat = wbAdvFlattenFullstats_(ctx.collected, rid, windows[w].from, windows[w].to);
      totalStat += wbAdvRawAppendRows_(statSheet, flat.statRows);
      totalBoost += wbAdvRawAppendRows_(boostSheet, flat.boosterRows);

      // no_stats — маркерные строки (HTTP 200, но без статистики)
      var noStatsRows = [];
      for (var nid in ctx.noStats) {
        if (!ctx.noStats.hasOwnProperty(nid)) continue;
        noStatsRows.push(wbAdvCampaignStatNoStatsRow_(rid, Number(nid), windows[w].from, windows[w].to));
      }
      totalNoStats += wbAdvRawAppendRows_(statSheet, noStatsRows);

      totalFail += ctx.failures.length;
      totalSkipped += ctx.skipped.length;

      if (ctx.stopped) {
        wbAdsRawWriteStatus_(rid, srcLabel, windows[w].from, windows[w].to, {
          campaigns_found: statsIds.length, campaigns_sampled: statsIds.length - ctx.skipped.length,
          http_status: String(lastCode), status: 'PARTIAL',
          error_message: 'Остановлено по тайм-бюджету; пропущено advertId: ' + ctx.skipped.length,
          response_keys_sample: 'skipped_ids=' + ctx.skipped.slice(0, 20).join(',')
        });
        break;
      }
    }

    var st = (totalFail > 0 || totalSkipped > 0) ? 'PARTIAL' : 'OK';
    wbAdsRawWriteStatus_(rid, srcLabel, rng.from, rng.to, {
      campaigns_found: statsIds.length, campaigns_sampled: statsIds.length,
      rows_or_items_found: totalStat,
      http_status: String(lastCode), status: st,
      response_keys_sample: 'stat_rows=' + totalStat + '; booster_rows=' + totalBoost +
        '; no_stats=' + totalNoStats + '; failed_single=' + totalFail + '; skipped=' + totalSkipped
    });
    return { source: srcLabel, status: st, rows: totalStat };
  } catch (e) {
    wbAdsRawWriteStatus_(rid, srcLabel, rng.from, rng.to, { status: 'FAILED', error_message: 'Исключение: ' + e.message });
    return { source: srcLabel, status: 'FAILED', rows: 0 };
  }
}


// ═══════════════════════════════════════
// ПУБЛИЧНЫЙ: COSTS RAW
// ═══════════════════════════════════════

/**
 * RAW расходов: /adv/v1/upd за период (окна ≤ 31 дня).
 * @param {string=} periodFrom @param {string=} periodTo @param {string=} runId
 */
function loadWbAdsCostsRaw(periodFrom, periodTo, runId) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var rid = wbAdsResolveRunId_(runId);
  var rng = wbAdsRawNormalizeRange_(periodFrom, periodTo);
  var srcLabel = 'raw_costs';

  var tok = getWbAdsToken_();
  if (!tok) {
    wbAdsRawWriteStatus_(rid, srcLabel, rng.from, rng.to, { status: 'BLOCKED', error_message: 'Нет WB Promotion токена' });
    return { source: srcLabel, status: 'BLOCKED', rows: 0 };
  }

  try {
    var sheet = wbAdvRawEnsureSheet_(ss, WB_ADV_RAW_COSTS_SHEET_, WB_ADV_RAW_COSTS_HEADERS_);
    var windows = wbAdsSplitPeriod_(rng.from, rng.to, WB_ADV_RAW_MAX_DAYS_);
    var total = 0, lastCode = '', failed = 0;

    for (var w = 0; w < windows.length; w++) {
      if (w > 0) Utilities.sleep(WB_ADS_UPD_PAUSE_MS_);
      var url = WB_ADS_API_HOST_ + '/adv/v1/upd?from=' + windows[w].from + '&to=' + windows[w].to;
      var resp = wbAdsHttp_('get', url, tok.token, null);
      lastCode = resp.code;

      if (!resp.ok) {
        failed++;
        wbAdsRawWriteStatus_(rid, srcLabel, windows[w].from, windows[w].to, {
          http_status: resp.code, status: 'FAILED',
          error_message: 'upd HTTP ' + resp.code + ': ' + wbAdsClip_(resp.body)
        });
        continue;
      }

      var data = Array.isArray(resp.json) ? resp.json : ((resp.json && resp.json.data) || []);
      var rows = [];
      for (var i = 0; i < data.length; i++) rows.push(wbAdvCostRow_(rid, data[i], windows[w].from, windows[w].to));
      total += wbAdvRawAppendRows_(sheet, rows);
    }

    var st = failed > 0 ? 'PARTIAL' : 'OK';
    wbAdsRawWriteStatus_(rid, srcLabel, rng.from, rng.to, {
      rows_or_items_found: total, http_status: String(lastCode), status: st,
      response_keys_sample: 'cost_rows=' + total + '; failed_windows=' + failed
    });
    return { source: srcLabel, status: st, rows: total };
  } catch (e) {
    wbAdsRawWriteStatus_(rid, srcLabel, rng.from, rng.to, { status: 'FAILED', error_message: 'Исключение: ' + e.message });
    return { source: srcLabel, status: 'FAILED', rows: 0 };
  }
}


// ═══════════════════════════════════════
// ПУБЛИЧНЫЙ: SEARCH CLUSTERS RAW (safe mode)
// ═══════════════════════════════════════

/**
 * RAW поисковых кластеров: /adv/v0/normquery/stats.
 * ⚠️ SAMPLE, НЕ полный RAW: ≤ WB_ADS_SEARCH_MAX_PAIRS_RAW_ связок advertId+nmId
 * за прогон (первые, без ротации), по одной связке на запрос, пауза ≥ 6.5 c
 * (WB_ADS_NORMQUERY_PAUSE_MS_). Полнота ключей — задача Фазы D.
 * @param {string=} periodFrom @param {string=} periodTo @param {string=} runId
 */
function loadWbAdsSearchClustersRaw(periodFrom, periodTo, runId) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var rid = wbAdsResolveRunId_(runId);
  var rng = wbAdsRawNormalizeRange_(periodFrom, periodTo);
  var srcLabel = 'raw_search_clusters';

  var tok = getWbAdsToken_();
  if (!tok) {
    wbAdsRawWriteStatus_(rid, srcLabel, rng.from, rng.to, { status: 'BLOCKED', error_message: 'Нет WB Promotion токена' });
    return { source: srcLabel, status: 'BLOCKED', rows: 0 };
  }

  try {
    var collected = wbAdsCollectAdvertNmPairs_(tok.token, WB_ADS_SEARCH_MAX_PAIRS_RAW_);
    var pairs = collected.pairs;
    if (!pairs.length) {
      wbAdsRawWriteStatus_(rid, srcLabel, rng.from, rng.to, {
        http_status: String(collected.countHttp), status: 'SKIPPED',
        error_message: 'Не получены связки advertId+nmId из adverts v2 — кластеры не запрашивались'
      });
      return { source: srcLabel, status: 'SKIPPED', rows: 0 };
    }

    var sheet = wbAdvRawEnsureSheet_(ss, WB_ADV_RAW_SEARCH_CLUSTERS_SHEET_, WB_ADV_RAW_SEARCH_CLUSTERS_HEADERS_);
    var deadline = wbAdsRawDeadline_();
    var total = 0, lastCode = '', failed = 0, done = 0;

    for (var i = 0; i < pairs.length; i++) {
      if (Date.now() >= deadline) {
        wbAdsRawWriteStatus_(rid, srcLabel, rng.from, rng.to, {
          campaigns_sampled: done, http_status: String(lastCode), status: 'PARTIAL',
          error_message: 'Остановлено по тайм-бюджету на связке ' + (i + 1) + '/' + pairs.length
        });
        return { source: srcLabel, status: 'PARTIAL', rows: total };
      }
      if (i > 0) Utilities.sleep(WB_ADS_NORMQUERY_PAUSE_MS_);

      var body = { from: rng.from, to: rng.to, items: [{ advert_id: pairs[i].advertId, nm_id: pairs[i].nmId }] };
      var resp = wbAdsHttp_('post', WB_ADS_API_HOST_ + '/adv/v0/normquery/stats', tok.token, body);
      lastCode = resp.code;
      done++;

      if (!resp.ok) {
        failed++;
        wbAdsRawWriteStatus_(rid, srcLabel, rng.from, rng.to, {
          campaigns_sampled: 1, http_status: resp.code, status: 'FAILED',
          error_message: 'advert_id=' + pairs[i].advertId + ' nm_id=' + pairs[i].nmId +
            ' HTTP ' + resp.code + ': ' + wbAdsClip_(resp.body)
        });
        continue;
      }

      var rows = wbAdvFlattenNormquery_(resp.json, rid, rng.from, rng.to);
      total += wbAdvRawAppendRows_(sheet, rows);
    }

    var st = failed > 0 ? 'PARTIAL' : 'OK';
    wbAdsRawWriteStatus_(rid, srcLabel, rng.from, rng.to, {
      campaigns_sampled: pairs.length, rows_or_items_found: total,
      http_status: String(lastCode), status: st,
      response_keys_sample: 'cluster_rows=' + total + '; pairs=' + pairs.length + '; failed_pairs=' + failed
    });
    return { source: srcLabel, status: st, rows: total };
  } catch (e) {
    wbAdsRawWriteStatus_(rid, srcLabel, rng.from, rng.to, { status: 'FAILED', error_message: 'Исключение: ' + e.message });
    return { source: srcLabel, status: 'FAILED', rows: 0 };
  }
}


// ═══════════════════════════════════════
// FULLSTATS: resilient collect (batch≤50 → 10 → single)
// ═══════════════════════════════════════

/**
 * Собирает fullstats по всем ids с дроблением при ошибках.
 * @return {Object} ctx { collected[], noStats{}, failures[], skipped[], stopped, httpCalls, lastCode, deadline }
 */
function wbAdsFullstatsCollect_(token, allIds, from, to, runId, deadline) {
  var ctx = {
    collected: [], noStats: {}, failures: [], skipped: [],
    stopped: false, httpCalls: 0, lastCode: '', deadline: deadline
  };
  var batches = wbAdsChunk_(allIds, WB_ADS_IDS_BATCH_); // по 50
  for (var b = 0; b < batches.length; b++) {
    wbAdsFullstatsTryLevel_(token, batches[b], from, to, runId, ctx);
  }
  return ctx;
}

/** Один уровень запроса fullstats; при ошибке дробит 50→10→single. */
function wbAdsFullstatsTryLevel_(token, ids, from, to, runId, ctx) {
  if (!ids.length) return;
  if (ctx.stopped) { for (var z = 0; z < ids.length; z++) ctx.skipped.push(ids[z]); return; }
  if (Date.now() >= ctx.deadline) {
    ctx.stopped = true;
    for (var z2 = 0; z2 < ids.length; z2++) ctx.skipped.push(ids[z2]);
    return;
  }

  if (ctx.httpCalls > 0) Utilities.sleep(WB_ADS_FULLSTATS_PAUSE_MS_); // ≥ 20 c между fullstats
  ctx.httpCalls++;

  var url = WB_ADS_API_HOST_ + '/adv/v3/fullstats?ids=' + ids.join(',') +
    '&beginDate=' + from + '&endDate=' + to;
  var resp = wbAdsHttp_('get', url, token, null);
  ctx.lastCode = resp.code;

  if (resp.ok) {
    var camps = Array.isArray(resp.json) ? resp.json : ((resp.json && resp.json.data) || []);
    if (camps.length) {
      var present = {};
      for (var i = 0; i < camps.length; i++) {
        var aid = camps[i].advertId || camps[i].advertID;
        if (aid != null) present[Number(aid)] = true;
        ctx.collected.push(camps[i]);
      }
      for (var k = 0; k < ids.length; k++) { if (!present[Number(ids[k])]) ctx.noStats[Number(ids[k])] = true; }
    } else {
      // HTTP 200, пусто → no_stats (не ошибка)
      for (var k2 = 0; k2 < ids.length; k2++) ctx.noStats[Number(ids[k2])] = true;
    }
    return;
  }

  // ошибка (500/429/иное) → дробление
  if (ids.length > 10) {
    var c10 = wbAdsChunk_(ids, 10);
    for (var c = 0; c < c10.length; c++) wbAdsFullstatsTryLevel_(token, c10[c], from, to, runId, ctx);
  } else if (ids.length > 1) {
    for (var s = 0; s < ids.length; s++) wbAdsFullstatsTryLevel_(token, [ids[s]], from, to, runId, ctx);
  } else {
    // single advertId упал → фиксируем ошибку, продолжаем
    ctx.failures.push({ advertId: ids[0], code: resp.code });
    wbAdsRawWriteStatus_(runId, 'raw_fullstats', from, to, {
      campaigns_sampled: 1, http_status: resp.code, status: 'FAILED',
      error_message: 'advertId=' + ids[0] + ' HTTP ' + resp.code + ': ' + wbAdsClip_(resp.body),
      response_keys_sample: 'advertId=' + ids[0]
    });
  }
}


// ═══════════════════════════════════════
// ПЛОСКАЯ РАСКЛАДКА ОТВЕТОВ → RAW-строки
// ═══════════════════════════════════════

/** fullstats → {statRows[], boosterRows[]} (defensive по ключам). */
function wbAdvFlattenFullstats_(camps, rid, from, to) {
  var statRows = [], boosterRows = [];
  for (var i = 0; i < camps.length; i++) {
    var camp = camps[i] || {};
    var advertId = camp.advertId != null ? camp.advertId : camp.advertID;
    var days = camp.days || [];

    for (var d = 0; d < days.length; d++) {
      var day = days[d] || {};
      var date = day.date != null ? day.date : (day.dt || '');
      var apps = day.apps || [];
      for (var a = 0; a < apps.length; a++) {
        var app = apps[a] || {};
        var appType = (app.appType != null) ? app.appType : (app.appName != null ? app.appName : '');
        var nms = app.nm || app.nms || [];
        for (var n = 0; n < nms.length; n++) {
          var nm = nms[n] || {};
          statRows.push({
            load_ts: wbAdsNow_(), run_id: rid, period_from: from, period_to: to,
            source_method: 'adv/v3/fullstats', processed_status: 'raw',
            advertId: advertId, date: date, appType: appType,
            nmId: (nm.nmId != null) ? nm.nmId : (nm.nm != null ? nm.nm : ''),
            name: (nm.name != null) ? nm.name : '',
            views: nm.views, clicks: nm.clicks, ctr: nm.ctr, cpc: nm.cpc, cr: nm.cr,
            atbs: nm.atbs, orders: nm.orders, canceled: nm.canceled, shks: nm.shks,
            sum: nm.sum, sum_price: (nm.sum_price != null) ? nm.sum_price : nm.sumPrice,
            source_level: 'nm', raw_json: wbAdvRawJson_(nm)
          });
        }
      }
    }

    var boost = camp.boosterStats || camp.booster_stats || [];
    for (var b = 0; b < boost.length; b++) {
      var bs = boost[b] || {};
      boosterRows.push({
        load_ts: wbAdsNow_(), run_id: rid, period_from: from, period_to: to,
        source_method: 'adv/v3/fullstats', processed_status: 'raw',
        advertId: advertId, date: bs.date != null ? bs.date : '',
        nmId: (bs.nm != null) ? bs.nm : (bs.nmId != null ? bs.nmId : ''),
        avg_position: (bs.avg_position != null) ? bs.avg_position
          : (bs.position != null ? bs.position : (bs.avgPosition != null ? bs.avgPosition : '')),
        raw_json: wbAdvRawJson_(bs)
      });
    }
  }
  return { statRows: statRows, boosterRows: boosterRows };
}

/** Маркерная строка no_stats для RAW_WB_ADV_CAMPAIGN_STATS. */
function wbAdvCampaignStatNoStatsRow_(rid, advertId, from, to) {
  return {
    load_ts: wbAdsNow_(), run_id: rid, period_from: from, period_to: to,
    source_method: 'adv/v3/fullstats', processed_status: 'no_stats',
    advertId: advertId, date: '', appType: '', nmId: '', name: '',
    views: '', clicks: '', ctr: '', cpc: '', cr: '', atbs: '', orders: '',
    canceled: '', shks: '', sum: '', sum_price: '', source_level: '', raw_json: ''
  };
}

/** normquery/stats → плоские строки кластеров. */
function wbAdvFlattenNormquery_(json, rid, from, to) {
  var rows = [];
  var statsArr = (json && json.stats) || [];
  for (var s = 0; s < statsArr.length; s++) {
    var pack = statsArr[s] || {};
    var advertId = (pack.advert_id != null) ? pack.advert_id : pack.advertId;
    var nmId = (pack.nm_id != null) ? pack.nm_id : pack.nmId;
    var inner = pack.stats || [];
    for (var c = 0; c < inner.length; c++) {
      var cl = inner[c] || {};
      rows.push({
        load_ts: wbAdsNow_(), run_id: rid, period_from: from, period_to: to,
        source_method: 'adv/v0/normquery/stats', processed_status: 'raw',
        advert_id: advertId, nm_id: nmId,
        norm_query: (cl.norm_query != null) ? cl.norm_query : (cl.normQuery != null ? cl.normQuery : ''),
        views: cl.views, clicks: cl.clicks, ctr: cl.ctr, cpc: cl.cpc, cpm: cl.cpm,
        avg_pos: (cl.avg_pos != null) ? cl.avg_pos : cl.avgPos,
        atbs: cl.atbs, orders: cl.orders, raw_json: wbAdvRawJson_(cl)
      });
    }
  }
  return rows;
}

/** Один row кампании для RAW_WB_ADV_CAMPAIGNS (campaign-level). */
function wbAdvCampaignRow_(rid, advertId, meta, detail) {
  var d = detail || {};
  var m = meta || {};
  var nmIds = detail ? wbAdvCollectNmIds_(detail) : [];
  return {
    load_ts: wbAdsNow_(), run_id: rid, period_from: '', period_to: '',
    source_method: 'promotion/count+adverts/v2',
    processed_status: detail ? 'raw' : 'count_only',
    advertId: advertId,
    type: (d.type != null) ? d.type : (m.type != null ? m.type : ''),
    status: (d.status != null) ? d.status : (m.status != null ? m.status : ''),
    payment_type: (d.paymentType != null) ? d.paymentType : (d.payment_type != null ? d.payment_type : ''),
    name: (d.name != null) ? d.name : '',
    campName: (d.campName != null) ? d.campName : (d.name != null ? d.name : ''),
    create_time: (d.createTime != null) ? d.createTime : (d.create_time != null ? d.create_time : ''),
    change_time: (d.changeTime != null) ? d.changeTime : (d.change_time != null ? d.change_time : (m.changeTime || '')),
    start_time: (d.startTime != null) ? d.startTime : (d.start_time != null ? d.start_time : ''),
    end_time: (d.endTime != null) ? d.endTime : (d.end_time != null ? d.end_time : ''),
    nm_ids: nmIds.join(','), nm_count: nmIds.length,
    raw_json: wbAdvRawJson_(detail || { advertId: advertId, count_meta: m })
  };
}

/** Один row расхода для RAW_WB_ADV_COSTS. */
function wbAdvCostRow_(rid, u, from, to) {
  u = u || {};
  var updTime = (u.updTime != null) ? u.updTime : '';
  return {
    load_ts: wbAdsNow_(), run_id: rid, period_from: from, period_to: to,
    source_method: 'adv/v1/upd', processed_status: 'raw',
    updTime: updTime,
    updDate: updTime ? String(updTime).substring(0, 10) : '',
    updNum: (u.updNum != null) ? u.updNum : '',
    updSum: (u.updSum != null) ? u.updSum : '',
    advertId: (u.advertId != null) ? u.advertId : '',
    campName: (u.campName != null) ? u.campName : '',
    advertType: (u.advertType != null) ? u.advertType : '',
    paymentType: (u.paymentType != null) ? u.paymentType : '',
    advertStatus: (u.advertStatus != null) ? u.advertStatus : '',
    raw_json: wbAdvRawJson_(u)
  };
}


// ═══════════════════════════════════════
// СВЯЗКИ advertId+nmId (для search clusters)
// ═══════════════════════════════════════

/** Собирает до maxPairs связок advertId+nmId из adverts v2 (по статусам 7/9/11 приоритетно). */
function wbAdsCollectAdvertNmPairs_(token, maxPairs) {
  var c = wbAdsFetchCampaigns_(token);
  var ids = (c.statsAdvertIds.length ? c.statsAdvertIds : c.advertIds);
  var pairs = [], seen = {};
  var chunks = wbAdsChunk_(ids, WB_ADS_IDS_BATCH_);

  for (var ch = 0; ch < chunks.length && pairs.length < maxPairs; ch++) {
    if (ch > 0) Utilities.sleep(1200);
    var url = WB_ADS_API_HOST_ + '/api/advert/v2/adverts?ids=' + chunks[ch].join(',');
    var resp = wbAdsHttp_('get', url, token, null);
    if (!resp.ok || !resp.json) continue;
    var adverts = Array.isArray(resp.json) ? resp.json
      : ((resp.json.adverts) || (resp.json.data && resp.json.data.adverts) || []);
    for (var a = 0; a < adverts.length && pairs.length < maxPairs; a++) {
      var advertId = adverts[a].id || adverts[a].advertId || adverts[a].advertID;
      if (advertId == null) continue;
      var nmIds = wbAdvCollectNmIds_(adverts[a]);
      for (var n = 0; n < nmIds.length && pairs.length < maxPairs; n++) {
        var key = advertId + '|' + nmIds[n];
        if (seen[key]) continue;
        seen[key] = 1;
        pairs.push({ advertId: Number(advertId), nmId: Number(nmIds[n]) });
      }
    }
  }
  return { pairs: pairs, countHttp: c.countHttp };
}

/** Defensive deep-scan: уникальные nmId из объекта кампании (nm_settings, nms, nm_id…). */
function wbAdvCollectNmIds_(obj) {
  var out = [], seen = {}, budget = { n: 0 };

  function add(v) {
    if (v == null || typeof v === 'object') return;
    var num = Number(v);
    if (!num || seen[num]) return;
    seen[num] = 1;
    out.push(num);
  }
  function walk(node) {
    if (node == null || typeof node !== 'object' || budget.n > 5000) return;
    budget.n++;
    if (node.nm_id != null) add(node.nm_id);
    if (node.nmId != null) add(node.nmId);
    if (node.nmID != null) add(node.nmID);
    for (var k in node) {
      if (!node.hasOwnProperty(k)) continue;
      var v = node[k];
      if (k === 'nm' && (typeof v === 'number' || typeof v === 'string')) add(v);
      if (v && typeof v === 'object') walk(v);
    }
  }
  walk(obj);
  return out;
}


// ═══════════════════════════════════════
// RAW-ЛИСТЫ: ensure (аддитивно) + batch append
// ═══════════════════════════════════════

/** Создаёт RAW-лист при отсутствии; иначе аддитивно дописывает недостающие колонки справа. */
function wbAdvRawEnsureSheet_(ss, name, headers) {
  // BQ-приёмник (Фаза C): вместо листа создаём таблицу в BigQuery и
  // возвращаем лёгкую заглушку с getName() — её ждёт wbAdvRawAppendRows_.
  if (typeof wbAdsBqSinkOn_ === 'function' && wbAdsBqSinkOn_()) {
    wbAdvBqEnsureTable_(name, headers);
    return { getName: function () { return name; }, _bqSink: true };
  }
  var sheet = ss.getSheetByName(name);
  if (!sheet) {
    sheet = ss.insertSheet(name);
    sheet.getRange(1, 1, 1, headers.length).setValues([headers]);
    sheet.setFrozenRows(1);
    console.log('  RAW лист создан: ' + name);
    return sheet;
  }
  if (sheet.getLastRow() === 0) {
    sheet.getRange(1, 1, 1, headers.length).setValues([headers]);
    sheet.setFrozenRows(1);
    return sheet;
  }
  var lastCol = sheet.getLastColumn();
  var existing = sheet.getRange(1, 1, 1, lastCol).getValues()[0];
  var present = {};
  for (var i = 0; i < existing.length; i++) {
    var nm = String(existing[i] || '').trim();
    if (nm) present[nm] = true;
  }
  var missing = [];
  for (var h = 0; h < headers.length; h++) { if (!present[headers[h]]) missing.push(headers[h]); }
  if (missing.length) {
    sheet.getRange(1, lastCol + 1, 1, missing.length).setValues([missing]);
    console.log('  RAW лист ' + name + ': дописаны колонки → ' + missing.join(', '));
  }
  return sheet;
}

/** Пакетно дописывает строки по ИМЕНАМ колонок текущего заголовка. @return число строк. */
function wbAdvRawAppendRows_(sheet, rowObjs) {
  if (!rowObjs || !rowObjs.length) return 0;
  // BQ-приёмник (Фаза C): пишем в BigQuery-таблицу с именем листа.
  if (sheet && sheet._bqSink) return wbAdvBqAppendRows_(sheet.getName(), rowObjs);
  var lastCol = sheet.getLastColumn();
  var headers = sheet.getRange(1, 1, 1, lastCol).getValues()[0];
  var data = [];
  for (var r = 0; r < rowObjs.length; r++) {
    var obj = rowObjs[r], arr = [];
    for (var c = 0; c < lastCol; c++) {
      var name = String(headers[c] || '').trim();
      arr.push((name && obj[name] !== undefined) ? obj[name] : '');
    }
    data.push(arr);
  }
  var startRow = sheet.getLastRow() + 1; if (startRow < 2) startRow = 2;
  sheet.getRange(startRow, 1, data.length, lastCol).setValues(data);
  return data.length;
}


// ═══════════════════════════════════════
// WB_ADS_STATUS writer (через хелперы probe)
// ═══════════════════════════════════════

/** Пишет строку в WB_ADS_STATUS (ошибка/итог loader'а). probe_name = sourceMethod. */
function wbAdsRawWriteStatus_(runId, sourceMethod, from, to, fields) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var r = wbAdsMakeResult_(runId, sourceMethod);
  r.period_from = from || '';
  r.period_to = to || '';
  if (fields) { for (var k in fields) { if (fields.hasOwnProperty(k)) r[k] = fields[k]; } }
  wbAdsFinish_(ss, r); // ensure WB_ADS_STATUS + append row + Logger
}


// ═══════════════════════════════════════
// ВСПОМОГАТЕЛЬНЫЕ
// ═══════════════════════════════════════

/** run_id с префиксом ADSRAW для отличия от диагностических прогонов. */
function wbAdsRawNewRunId_() {
  return 'ADSRAW_' + Utilities.formatDate(new Date(), WB_ADS_TZ_, 'yyyyMMdd_HHmmss') +
    '_' + Math.floor(Math.random() * 1000);
}

/** Нормализует период: если from/to не заданы — последние 7 дней. */
function wbAdsRawNormalizeRange_(from, to) {
  if (from && to) return { from: from, to: to };
  return wbAdsLast7Range_();
}

/** Дедлайн прогона: общий (orchestrator) или индивидуальный (одиночный loader). */
function wbAdsRawDeadline_() {
  if (WB_ADS_RAW_RUN_T0_ != null) return WB_ADS_RAW_RUN_T0_ + WB_ADS_RAW_RUN_BUDGET_MS_;
  return Date.now() + WB_ADS_RAW_TIME_BUDGET_MS_;
}

/** Делит [from,to] на окна ≤ maxDays (для fullstats/upd). */
function wbAdsSplitPeriod_(from, to, maxDays) {
  var f = wbAdsRawParseDate_(from), t = wbAdsRawParseDate_(to);
  if (!f || !t || f > t) return [{ from: from, to: to }];
  var windows = [], curStart = new Date(f.getTime());
  while (curStart <= t) {
    var curEnd = new Date(curStart.getTime());
    curEnd.setDate(curEnd.getDate() + (maxDays - 1));
    if (curEnd > t) curEnd = new Date(t.getTime());
    windows.push({ from: wbAdsRawFmtDate_(curStart), to: wbAdsRawFmtDate_(curEnd) });
    curStart = new Date(curEnd.getTime());
    curStart.setDate(curStart.getDate() + 1);
  }
  return windows;
}

function wbAdsRawParseDate_(s) {
  if (!s) return null;
  var m = String(s).match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!m) return null;
  return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
}

function wbAdsRawFmtDate_(d) {
  return Utilities.formatDate(d, WB_ADS_TZ_, 'yyyy-MM-dd');
}

/** Делит массив на пачки size. */
function wbAdsChunk_(arr, size) {
  var out = [];
  for (var i = 0; i < arr.length; i += size) out.push(arr.slice(i, i + size));
  return out;
}

/** Безопасный JSON.stringify с обрезкой под лимит ячейки. */
function wbAdvRawJson_(obj) {
  try {
    var s = JSON.stringify(obj);
    if (s == null) return '';
    return (s.length > WB_ADV_RAW_JSON_MAX_) ? s.substring(0, WB_ADV_RAW_JSON_MAX_) : s;
  } catch (e) { return ''; }
}
