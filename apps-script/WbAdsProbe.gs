/**
 * ══════════════════════════════════════════════════════════════
 * EVETIS WB — WbAdsProbe.gs  v1.0  (PR #17A)
 *
 * ПЕРВЫЙ БЕЗОПАСНЫЙ ДИАГНОСТИЧЕСКИЙ ПРОБ WB Ads API.
 * Цель: проверить ДОСТУПНОСТЬ и ФАКТИЧЕСКУЮ СТРУКТУРУ ответов
 * рекламного API WB перед написанием production-загрузчика.
 *
 * SCOPE PR #17A (строго):
 *   • Только диагностика. Никаких production-расчётов.
 *   • НЕ создаёт исторический загрузчик и НЕ пишет большие RAW-таблицы.
 *   • Результаты — ТОЛЬКО в отдельный лист WB_ADS_STATUS и в Logger.
 *   • Берёт токен существующим способом проекта (Script Properties),
 *     токены не хардкодятся и не логируются.
 *
 * НЕ ТРОГАЕТ / НЕ ВЫЗЫВАЕТ (by design):
 *   CLEAN_WB_DAILY, UNIT_SKU_DAILY, PNL_TOTAL, RAW_WB_FINANCE,
 *   RAW_WB_ORDERS, RAW_WB_SALES_RETURNS, RAW_WB_STOCKS,
 *   runWbDailyRefresh / WbDailyRefresh, любые import*-загрузчики,
 *   SheetsSchema/createAllSheets, finance/recon-логику.
 *
 * ПРОВЕРЯЕМЫЕ МЕТОДЫ WB Ads API (host advert-api.wildberries.ru):
 *   1) GET  /adv/v1/promotion/count        — список кампаний по типам/статусам
 *   2) GET  /api/advert/v2/adverts         — детали кампаний (ids ≤ 50)
 *   3) GET  /adv/v3/fullstats              — статистика (ids ≤ 50, ≤ 31 день, статусы 7/9/11)
 *   4) GET  /adv/v1/upd                    — история затрат (from/to, ≤ 31 день)
 *   5) POST /adv/v0/normquery/stats        — статистика поисковых кластеров (ТОЛЬКО sample 1–3 пары)
 *
 * ПУБЛИЧНЫЕ РУЧНЫЕ ФУНКЦИИ:
 *   probeWbAdsCampaigns()
 *   probeWbAdsFullstatsLast7Days()
 *   probeWbAdsCostsLast7Days()
 *   probeWbAdsSearchClustersSample()
 *   runWbAdsProbeAll()
 *
 * ЗАЩИТА ОТ 429: линейный retry/backoff с паузами не меньше лимитов WB.
 * Ошибки не глотаются — пишутся в WB_ADS_STATUS (колонка error_message).
 * ══════════════════════════════════════════════════════════════
 */


// ═══════════════════════════════════════
// КОНФИГ
// ═══════════════════════════════════════

var WB_ADS_API_HOST_ = 'https://advert-api.wildberries.ru';

/** Категория токена — Promotion. Основной ключ + безопасные фолбэки. */
var WB_ADS_TOKEN_KEYS_ = ['WB_TOKEN_PROMOTION', 'WB_TOKEN_ADVERT', 'WB_TOKEN_ANALYTICS', 'WB_TOKEN_STANDARD'];

var WB_ADS_STATUS_SHEET_ = 'WB_ADS_STATUS';
var WB_ADS_STATUS_HEADERS_ = [
  'run_id', 'started_at', 'finished_at', 'probe_name',
  'period_from', 'period_to',
  'campaigns_found', 'campaigns_sampled', 'rows_or_items_found',
  'http_status', 'status', 'error_message', 'response_keys_sample'
];

var WB_ADS_TZ_ = 'Europe/Moscow';

/** Лимиты WB: ids ≤ 50 за запрос; статистика только по статусам 7/9/11. */
var WB_ADS_IDS_BATCH_ = 50;
var WB_ADS_STATS_STATUSES_ = [7, 9, 11];

/** Поисковые кластеры — массово НЕ опрашиваем, только sample 1–3 связки. */
var WB_ADS_SEARCH_SAMPLE_MAX_ = 3;

/** Защита от 429: линейный backoff, паузы ≥ лимитов WB (fullstats — 3 req/min). */
var WB_ADS_MAX_RETRY_429_ = 3;
var WB_ADS_RETRY_BASE_MS_ = 20000;   // ≥ 20 с (интервал fullstats)

/** Паузы между запросами/пробами (не меньше лимитов WB). */
var WB_ADS_FULLSTATS_PAUSE_MS_ = 21000; // fullstats: 3 req/min → ~20 с
var WB_ADS_UPD_PAUSE_MS_       = 1200;  // upd: 1 req/sec (burst 5)
var WB_ADS_NORMQUERY_PAUSE_MS_ = 6500;  // normquery stats: interval 6s
var WB_ADS_BETWEEN_PROBES_MS_  = 3000;  // между пробами в runWbAdsProbeAll


// ═══════════════════════════════════════
// МЕНЮ
//   Подключить в Menu v2 → onOpen() одной строкой:  addWbAdsProbeMenu();
// ═══════════════════════════════════════

function addWbAdsProbeMenu() {
  SpreadsheetApp.getUi()
    .createMenu('🧪 WB Ads Probe')
    .addItem('Проверить рекламу WB', 'runWbAdsProbeAll')
    .addSeparator()
    .addItem('Кампании (count + adverts v2)', 'probeWbAdsCampaigns')
    .addItem('Fullstats (последние 7 дней)', 'probeWbAdsFullstatsLast7Days')
    .addItem('Расходы upd (последние 7 дней)', 'probeWbAdsCostsLast7Days')
    .addItem('Поисковые кластеры (sample)', 'probeWbAdsSearchClustersSample')
    .addToUi();
}


// ═══════════════════════════════════════
// ПУБЛИЧНЫЕ ПРОБЫ (все ручные)
// ═══════════════════════════════════════

/**
 * Проба #1+#2: список кампаний (/adv/v1/promotion/count) и детали
 * (/api/advert/v2/adverts, ids ≤ 50). Только структура и счётчики.
 * @param {string=} runId — общий run_id (передаётся из runWbAdsProbeAll).
 */
function probeWbAdsCampaigns(runId) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var r = wbAdsMakeResult_(wbAdsResolveRunId_(runId), 'campaigns');

  var tok = getWbAdsToken_();
  if (!tok) { wbAdsFinishBlocked_(ss, r, 'Нет WB Promotion токена (' + WB_ADS_TOKEN_KEYS_.join('/') + ')'); return r; }

  try {
    var c = wbAdsFetchCampaigns_(tok.token);
    r.http_status = String(c.countHttp) + (c.advertsHttp !== '' ? ('/' + c.advertsHttp) : '');
    r.campaigns_found = c.advertIds.length;
    r.campaigns_sampled = Math.min((c.statsAdvertIds.length || c.advertIds.length), WB_ADS_IDS_BATCH_);
    r.rows_or_items_found = c.advertIds.length;
    r.response_keys_sample =
      'count:[' + c.countKeys + ']; adverts_v2:[' + c.advertsKeys + ']; ' +
      'stats_ids(7/9/11):' + c.statsAdvertIds.length + '; statuses:' + JSON.stringify(c.statusBreakdown);
    if (c.error) { r.status = 'FAILED'; r.error_message = c.error; }
    else if (c.advertIds.length > 0) { r.status = 'OK'; }
    else { r.status = 'PARTIAL'; r.error_message = 'Кампании не получены (пустой список)'; }
  } catch (e) {
    r.status = 'FAILED'; r.error_message = 'Исключение: ' + e.message;
  }

  wbAdsFinish_(ss, r);
  return r;
}

/**
 * Проба #3: /adv/v3/fullstats за последние 7 дней.
 * Берёт ≤ 50 advertId в статусах 7/9/11 (одна пачка), beginDate/endDate.
 * @param {string=} runId
 */
function probeWbAdsFullstatsLast7Days(runId) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var r = wbAdsMakeResult_(wbAdsResolveRunId_(runId), 'fullstats_last7');
  var rng = wbAdsLast7Range_();
  r.period_from = rng.from; r.period_to = rng.to;

  var tok = getWbAdsToken_();
  if (!tok) { wbAdsFinishBlocked_(ss, r, 'Нет WB Promotion токена'); return r; }

  try {
    var c = wbAdsFetchCampaigns_(tok.token);
    r.campaigns_found = c.advertIds.length;
    var ids = c.statsAdvertIds.slice(0, WB_ADS_IDS_BATCH_); // одна пачка ≤ 50, только 7/9/11
    r.campaigns_sampled = ids.length;

    if (!ids.length) {
      r.status = 'SKIPPED';
      r.http_status = String(c.countHttp);
      r.error_message = 'Нет кампаний в статусах 7/9/11 — fullstats не запрашивался';
      r.response_keys_sample = 'count:[' + c.countKeys + ']';
      wbAdsFinish_(ss, r); return r;
    }

    var url = WB_ADS_API_HOST_ + '/adv/v3/fullstats?ids=' + ids.join(',') +
      '&beginDate=' + rng.from + '&endDate=' + rng.to;
    var resp = wbAdsHttp_('get', url, tok.token, null);
    r.http_status = resp.code;

    var camps = Array.isArray(resp.json) ? resp.json : ((resp.json && resp.json.data) || []);
    r.rows_or_items_found = camps.length;
    r.response_keys_sample = wbAdsFullstatsKeys_(camps);

    if (!resp.ok) { r.status = 'FAILED'; r.error_message = 'HTTP ' + resp.code + ': ' + wbAdsClip_(resp.body); }
    else if (camps.length) { r.status = 'OK'; }
    else { r.status = 'PARTIAL'; r.error_message = 'Ответ 200, но кампаний в статистике нет'; }
  } catch (e) {
    r.status = 'FAILED'; r.error_message = 'Исключение: ' + e.message;
  }

  wbAdsFinish_(ss, r);
  return r;
}

/**
 * Проба #4: /adv/v1/upd (история затрат) за последние 7 дней (from/to).
 * @param {string=} runId
 */
function probeWbAdsCostsLast7Days(runId) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var r = wbAdsMakeResult_(wbAdsResolveRunId_(runId), 'costs_upd_last7');
  var rng = wbAdsLast7Range_();
  r.period_from = rng.from; r.period_to = rng.to;

  var tok = getWbAdsToken_();
  if (!tok) { wbAdsFinishBlocked_(ss, r, 'Нет WB Promotion токена'); return r; }

  try {
    var url = WB_ADS_API_HOST_ + '/adv/v1/upd?from=' + rng.from + '&to=' + rng.to;
    var resp = wbAdsHttp_('get', url, tok.token, null);
    r.http_status = resp.code;

    var data = Array.isArray(resp.json) ? resp.json : ((resp.json && resp.json.data) || []);
    r.rows_or_items_found = data.length;
    r.response_keys_sample = data.length ? wbAdsKeysSample_(data) : wbAdsKeysSample_(resp.json);

    if (!resp.ok) { r.status = 'FAILED'; r.error_message = 'HTTP ' + resp.code + ': ' + wbAdsClip_(resp.body); }
    else if (data.length) { r.status = 'OK'; }
    else { r.status = 'PARTIAL'; r.error_message = 'Ответ 200, но списаний за период нет'; }
  } catch (e) {
    r.status = 'FAILED'; r.error_message = 'Исключение: ' + e.message;
  }

  wbAdsFinish_(ss, r);
  return r;
}

/**
 * Проба #5: /adv/v0/normquery/stats — ТОЛЬКО безопасный sample (1–3 связки
 * advertId + nmId, полученные из campaigns information). Массово НЕ опрашивает.
 * Схема тела запроса/ответа — TBD; проба фиксирует фактический HTTP и ключи.
 * @param {string=} runId
 */
function probeWbAdsSearchClustersSample(runId) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var r = wbAdsMakeResult_(wbAdsResolveRunId_(runId), 'search_clusters_sample');
  var rng = wbAdsLast7Range_();
  r.period_from = rng.from; r.period_to = rng.to;

  var tok = getWbAdsToken_();
  if (!tok) { wbAdsFinishBlocked_(ss, r, 'Нет WB Promotion токена'); return r; }

  try {
    var c = wbAdsFetchCampaigns_(tok.token);
    r.campaigns_found = c.advertIds.length;
    var pairs = (c.pairs || []).slice(0, WB_ADS_SEARCH_SAMPLE_MAX_);
    r.campaigns_sampled = pairs.length;

    if (!pairs.length) {
      r.status = 'SKIPPED';
      r.http_status = String(c.advertsHttp || c.countHttp);
      r.error_message = 'Не получены связки advertId+nmId из campaigns information — массовый опрос не выполняется';
      r.response_keys_sample = 'adverts_v2:[' + c.advertsKeys + ']';
      wbAdsFinish_(ss, r); return r;
    }

    var lastCode = '', items = 0, keys = '', errs = [];
    for (var i = 0; i < pairs.length; i++) {
      if (i > 0) Utilities.sleep(WB_ADS_NORMQUERY_PAUSE_MS_);
      // Тело запроса по документации WB: from/to + items[] (advert_id + nm_id).
      var body = {
        from: rng.from,
        to: rng.to,
        items: [
          { advert_id: pairs[i].advertId, nm_id: pairs[i].nmId }
        ]
      };
      var resp = wbAdsHttp_('post', WB_ADS_API_HOST_ + '/adv/v0/normquery/stats', tok.token, body);
      lastCode = resp.code;
      if (resp.ok && resp.json) {
        if (!keys) keys = wbAdsNormqueryKeys_(resp.json);
        // rows = сумма внутренних stats.length по всем элементам resp.json.stats
        var statsArr = (resp.json && resp.json.stats) || [];
        for (var s = 0; s < statsArr.length; s++) {
          var inner = statsArr[s] && statsArr[s].stats;
          if (inner && inner.length) items += inner.length;
        }
      } else {
        errs.push('pair' + (i + 1) + ' HTTP ' + resp.code);
      }
    }

    r.http_status = lastCode;
    r.rows_or_items_found = items;
    r.response_keys_sample = 'normquery:' + keys + '; sampled_pairs=' + pairs.length;
    if (errs.length === pairs.length) { r.status = 'FAILED'; r.error_message = errs.join('; '); }
    else if (errs.length) { r.status = 'PARTIAL'; r.error_message = errs.join('; '); }
    else { r.status = 'OK'; }
  } catch (e) {
    r.status = 'FAILED'; r.error_message = 'Исключение: ' + e.message;
  }

  wbAdsFinish_(ss, r);
  return r;
}

/**
 * Запускает все пробы по очереди под общим run_id, с паузами ≥ лимитов WB.
 * Каждая проба пишет свою строку в WB_ADS_STATUS. В конце — alert-сводка
 * (если есть UI-контекст). НИЧЕГО, кроме WB_ADS_STATUS, не меняет.
 * @return {Object} { run_id, results }
 */
function runWbAdsProbeAll() {
  var t0 = Date.now();
  var runId = wbAdsNewRunId_();
  console.log('═══ runWbAdsProbeAll() v1.0 СТАРТ, run_id=' + runId + ' ═══');

  var results = [];
  results.push(probeWbAdsCampaigns(runId));
  Utilities.sleep(WB_ADS_BETWEEN_PROBES_MS_);

  results.push(probeWbAdsFullstatsLast7Days(runId));
  Utilities.sleep(WB_ADS_FULLSTATS_PAUSE_MS_);

  results.push(probeWbAdsCostsLast7Days(runId));
  Utilities.sleep(WB_ADS_UPD_PAUSE_MS_);

  results.push(probeWbAdsSearchClustersSample(runId));

  var elapsed = ((Date.now() - t0) / 1000).toFixed(1);
  console.log('═══ runWbAdsProbeAll() ЗАВЕРШЕНО за ' + elapsed + ' сек ═══');

  // Alert только при наличии UI-контекста (из меню/редактора). Под триггером — пропускаем.
  try {
    var ui = SpreadsheetApp.getUi();
    var msg = 'run_id: ' + runId + '\n\n';
    for (var i = 0; i < results.length; i++) {
      var x = results[i];
      msg += '• ' + x.probe_name + ': ' + x.status + ' (HTTP ' + x.http_status + ')' +
        (x.error_message ? ' — ' + wbAdsClip_(x.error_message, 80) : '') + '\n';
    }
    msg += '\nПодробности — лист WB_ADS_STATUS.\n' +
      '⚠️ Только диагностика. CLEAN/UNIT/PNL/RAW_WB_FINANCE и daily refresh НЕ затронуты.\n' +
      'Время: ' + elapsed + ' сек';
    ui.alert('🧪 WB Ads Probe', msg, ui.ButtonSet.OK);
  } catch (eUi) { /* нет UI-контекста — это нормально */ }

  return { run_id: runId, results: results };
}


// ═══════════════════════════════════════
// СБОР КАМПАНИЙ (count + adverts v2) — общий для проб
// ═══════════════════════════════════════

/**
 * Тянет список кампаний (count) и детали (adverts v2, ids ≤ 50).
 * Defensive-парсинг: структура может отличаться — фиксируем как есть.
 * @return {Object} { countHttp, advertsHttp, advertIds, statsAdvertIds,
 *                     statusBreakdown, pairs, countKeys, advertsKeys, error }
 */
function wbAdsFetchCampaigns_(token) {
  var out = {
    countHttp: '', advertsHttp: '',
    advertIds: [], statsAdvertIds: [], statusBreakdown: {},
    pairs: [], countKeys: '', advertsKeys: '', error: ''
  };

  // 1) /adv/v1/promotion/count
  var cResp = wbAdsHttp_('get', WB_ADS_API_HOST_ + '/adv/v1/promotion/count', token, null);
  out.countHttp = cResp.code;
  out.countKeys = wbAdsKeysSample_(cResp.json);
  if (!cResp.ok) {
    out.error = 'count HTTP ' + cResp.code + ': ' + wbAdsClip_(cResp.body);
    return out;
  }

  var adverts = (cResp.json && (cResp.json.adverts || (cResp.json.data && cResp.json.data.adverts))) || [];
  for (var i = 0; i < adverts.length; i++) {
    var st = adverts[i].status;
    var list = adverts[i].advert_list || adverts[i].advertList || [];
    for (var j = 0; j < list.length; j++) {
      var id = list[j].advertId || list[j].advertID || list[j].id;
      if (id == null) continue;
      var idNum = Number(id);
      out.advertIds.push(idNum);
      out.statusBreakdown[st] = (out.statusBreakdown[st] || 0) + 1;
      if (WB_ADS_STATS_STATUSES_.indexOf(Number(st)) !== -1) out.statsAdvertIds.push(idNum);
    }
  }

  // 2) /api/advert/v2/adverts (ids ≤ 50) — детали + извлечение пар advertId+nmId
  var sampleIds = (out.statsAdvertIds.length ? out.statsAdvertIds : out.advertIds).slice(0, WB_ADS_IDS_BATCH_);
  if (sampleIds.length) {
    var aUrl = WB_ADS_API_HOST_ + '/api/advert/v2/adverts?ids=' + sampleIds.join(',');
    var aResp = wbAdsHttp_('get', aUrl, token, null);
    out.advertsHttp = aResp.code;
    out.advertsKeys = wbAdsKeysSample_(aResp.json);
    if (aResp.ok && aResp.json) out.pairs = wbAdsExtractPairs_(aResp.json, WB_ADS_SEARCH_SAMPLE_MAX_);
  }

  return out;
}

/**
 * Извлекает до maxPairs связок { advertId, nmId } из ответа /api/advert/v2/adverts.
 * Сначала — явный проход по adverts[] (id + nm_settings[].nm_id, как в контракте WB),
 * и только если явных пар нет — defensive deep-scan по всему JSON.
 */
function wbAdsExtractPairs_(json, maxPairs) {
  var pairs = [], seen = {};

  function pushPair(advertId, nmId) {
    if (advertId == null || nmId == null) return false;
    var k = advertId + '|' + nmId;
    if (seen[k]) return false;
    seen[k] = 1;
    pairs.push({ advertId: Number(advertId), nmId: Number(nmId) });
    return true;
  }

  // 1) Явный контракт WB: id кампании + nm_settings[].nm_id.
  var adverts = Array.isArray(json) ? json : ((json && json.adverts) || (json && json.data && json.data.adverts) || []);
  for (var a = 0; a < adverts.length && pairs.length < maxPairs; a++) {
    var advert = adverts[a];
    if (!advert || typeof advert !== 'object') continue;
    var advertId = advert.id || advert.advertId || advert.advertID;
    if (advertId == null) continue;
    var nms = advert.nm_settings || advert.nmSettings || [];
    for (var n = 0; n < nms.length && pairs.length < maxPairs; n++) {
      var item = nms[n] || {};
      var nmId = item.nm_id || item.nmId || item.nmID;
      pushPair(advertId, nmId);
    }
  }
  if (pairs.length) return pairs;

  // 2) Фолбэк: defensive deep-scan, если явных пар не нашлось.
  function walk(node, curAdvert) {
    if (pairs.length >= maxPairs || node == null || typeof node !== 'object') return;
    var advert = curAdvert;
    if (node.id != null) advert = node.id;
    if (node.advertId != null) advert = node.advertId;
    else if (node.advertID != null) advert = node.advertID;

    var nm = (node.nm_id != null) ? node.nm_id
      : ((node.nmId != null) ? node.nmId
      : ((node.nmID != null) ? node.nmID : node.nm));
    if (nm != null && typeof nm !== 'object') pushPair(advert, nm);

    for (var key in node) {
      if (!node.hasOwnProperty(key)) continue;
      var v = node[key];
      if (v && typeof v === 'object') walk(v, advert);
      if (pairs.length >= maxPairs) return;
    }
  }

  if (Array.isArray(json)) {
    for (var i = 0; i < json.length && pairs.length < maxPairs; i++) walk(json[i], null);
  } else {
    walk(json, null);
  }
  return pairs;
}


// ═══════════════════════════════════════
// HTTP + ЗАЩИТА ОТ 429
// ═══════════════════════════════════════

/**
 * Единый HTTP-вызов WB Ads API с линейным backoff на 429.
 * Токен передаётся только в заголовок Authorization (нигде не логируется).
 * @return {Object} { code, ok, body, json, attempts }
 */
function wbAdsHttp_(method, url, token, payload) {
  var opt = { method: method, headers: { 'Authorization': token }, muteHttpExceptions: true };
  if (payload != null) { opt.contentType = 'application/json'; opt.payload = JSON.stringify(payload); }

  var attempts = 0;
  while (true) {
    attempts++;
    var resp = UrlFetchApp.fetch(url, opt);
    var code = resp.getResponseCode();

    if (code === 429 && attempts <= WB_ADS_MAX_RETRY_429_) {
      var pause = WB_ADS_RETRY_BASE_MS_ * attempts; // линейный backoff, ≥ лимита WB
      console.log('  [WB_ADS] 429 на попытке ' + attempts + ', пауза ' + (pause / 1000) + ' с...');
      Utilities.sleep(pause);
      continue;
    }

    var body = resp.getContentText();
    var json = null;
    try { json = JSON.parse(body); } catch (e) { json = null; }
    return { code: code, ok: (code >= 200 && code < 300), body: body, json: json, attempts: attempts };
  }
}


// ═══════════════════════════════════════
// ТОКЕН (существующий способ проекта)
// ═══════════════════════════════════════

function wbAdsScriptProp_(key) {
  try { if (typeof getScriptProperty_ === 'function') { var v = getScriptProperty_(key); if (v) return v; } } catch (e) {}
  try { return PropertiesService.getScriptProperties().getProperty(key) || ''; } catch (e2) { return ''; }
}

function getWbAdsToken_() {
  for (var i = 0; i < WB_ADS_TOKEN_KEYS_.length; i++) {
    var t = wbAdsScriptProp_(WB_ADS_TOKEN_KEYS_[i]);
    if (t) return { token: t, key: WB_ADS_TOKEN_KEYS_[i] };
  }
  return null;
}


// ═══════════════════════════════════════
// WB_ADS_STATUS: лист, строка результата
// ═══════════════════════════════════════

/** Создаёт WB_ADS_STATUS при отсутствии; иначе аддитивно дописывает недостающие колонки. */
function ensureWbAdsStatusSheet_(ss) {
  var sheet = ss.getSheetByName(WB_ADS_STATUS_SHEET_);
  if (!sheet) {
    sheet = ss.insertSheet(WB_ADS_STATUS_SHEET_);
    sheet.getRange(1, 1, 1, WB_ADS_STATUS_HEADERS_.length).setValues([WB_ADS_STATUS_HEADERS_]);
    sheet.setFrozenRows(1);
    console.log('  WB_ADS_STATUS создан');
    return sheet;
  }
  if (sheet.getLastRow() === 0) {
    sheet.getRange(1, 1, 1, WB_ADS_STATUS_HEADERS_.length).setValues([WB_ADS_STATUS_HEADERS_]);
    sheet.setFrozenRows(1);
    return sheet;
  }
  // Аддитивная миграция: дописать недостающие колонки СПРАВА (старые не трогаем).
  var lastCol = sheet.getLastColumn();
  var existing = sheet.getRange(1, 1, 1, lastCol).getValues()[0];
  var present = {};
  for (var i = 0; i < existing.length; i++) {
    var nm = String(existing[i] || '').trim();
    if (nm) present[nm] = true;
  }
  var missing = [];
  for (var h = 0; h < WB_ADS_STATUS_HEADERS_.length; h++) {
    if (!present[WB_ADS_STATUS_HEADERS_[h]]) missing.push(WB_ADS_STATUS_HEADERS_[h]);
  }
  if (missing.length) {
    sheet.getRange(1, lastCol + 1, 1, missing.length).setValues([missing]);
    console.log('  WB_ADS_STATUS: дописаны колонки → ' + missing.join(', '));
  }
  return sheet;
}

/** Пишет одну строку ПО ИМЕНАМ колонок фактического заголовка листа. */
function writeWbAdsStatusRow_(sheet, r) {
  var lastCol = sheet.getLastColumn();
  if (lastCol < 1) return;
  var headers = sheet.getRange(1, 1, 1, lastCol).getValues()[0];
  var row = [];
  for (var c = 0; c < lastCol; c++) {
    var name = String(headers[c] || '').trim();
    row.push((name && r[name] !== undefined) ? r[name] : '');
  }
  var ir = sheet.getLastRow() + 1; if (ir < 2) ir = 2;
  sheet.getRange(ir, 1, 1, lastCol).setValues([row]);
}


// ═══════════════════════════════════════
// ВСПОМОГАТЕЛЬНЫЕ
// ═══════════════════════════════════════

/** Завершает пробу: проставляет finished_at, пишет строку в WB_ADS_STATUS + лог. */
function wbAdsFinish_(ss, r) {
  r.finished_at = wbAdsNow_();
  try {
    var sheet = ensureWbAdsStatusSheet_(ss);
    writeWbAdsStatusRow_(sheet, r);
  } catch (e) {
    console.log('⚠️ WB_ADS_STATUS write: ' + e.message);
  }
  console.log('[WB_ADS_PROBE] ' + r.probe_name + ' → ' + r.status +
    ' (HTTP ' + r.http_status + ')' + (r.error_message ? ' | ' + r.error_message : ''));
}

/** Завершает пробу со статусом BLOCKED (например, нет токена). */
function wbAdsFinishBlocked_(ss, r, msg) {
  r.status = 'BLOCKED';
  r.error_message = msg;
  wbAdsFinish_(ss, r);
}

/** Заготовка строки результата со всеми колонками WB_ADS_STATUS. */
function wbAdsMakeResult_(runId, probeName) {
  return {
    run_id: runId, started_at: wbAdsNow_(), finished_at: '',
    probe_name: probeName, period_from: '', period_to: '',
    campaigns_found: '', campaigns_sampled: '', rows_or_items_found: '',
    http_status: '', status: '', error_message: '', response_keys_sample: ''
  };
}

/** Окно «последние 7 дней»: вчера-6 … вчера (Europe/Moscow). */
function wbAdsLast7Range_() {
  var y = new Date(); y.setDate(y.getDate() - 1);
  var f = new Date(y); f.setDate(f.getDate() - 6);
  return {
    from: Utilities.formatDate(f, WB_ADS_TZ_, 'yyyy-MM-dd'),
    to: Utilities.formatDate(y, WB_ADS_TZ_, 'yyyy-MM-dd')
  };
}

function wbAdsNow_() {
  return Utilities.formatDate(new Date(), WB_ADS_TZ_, 'yyyy-MM-dd HH:mm:ss');
}

function wbAdsNewRunId_() {
  return 'ADSPROBE_' + Utilities.formatDate(new Date(), WB_ADS_TZ_, 'yyyyMMdd_HHmmss') +
    '_' + Math.floor(Math.random() * 1000);
}

/** Меню/триггер не передают строковый runId — генерируем свой. */
function wbAdsResolveRunId_(runId) {
  return (typeof runId === 'string' && runId) ? runId : wbAdsNewRunId_();
}

/** Сэмпл ключей: верхний объект или первый элемент массива (до 25 ключей). */
function wbAdsKeysSample_(json) {
  try {
    if (json == null) return '';
    var obj = Array.isArray(json) ? (json.length ? json[0] : null) : json;
    if (obj && typeof obj === 'object') return Object.keys(obj).slice(0, 25).join(', ');
    return typeof json;
  } catch (e) { return ''; }
}

/** Сэмпл ключей вложенной структуры fullstats: root/days/apps/nms. */
function wbAdsFullstatsKeys_(camps) {
  if (!camps || !camps.length) return '';
  var c0 = camps[0];
  var rootKeys = Object.keys(c0).slice(0, 20).join(', ');
  var dayKeys = '', appKeys = '', nmKeys = '';
  var days = c0.days || [];
  if (days.length) {
    dayKeys = Object.keys(days[0]).slice(0, 20).join(', ');
    var apps = days[0].apps || [];
    if (apps.length) {
      appKeys = Object.keys(apps[0]).slice(0, 20).join(', ');
      var nms = apps[0].nm || apps[0].nms || [];
      if (nms.length) nmKeys = Object.keys(nms[0]).slice(0, 20).join(', ');
    }
  }
  return 'root:[' + rootKeys + ']; days:[' + dayKeys + ']; apps:[' + appKeys + ']; nms:[' + nmKeys + ']';
}

/**
 * Сэмпл ключей ответа /adv/v0/normquery/stats: root (включая stats) +
 * вложенные ключи из первого stats[0] и stats[0].stats[0].
 */
function wbAdsNormqueryKeys_(json) {
  if (!json || typeof json !== 'object') return '';
  var rootKeys = Object.keys(json).slice(0, 20).join(', ');
  var outerKeys = '', innerKeys = '';
  var statsArr = json.stats || [];
  if (statsArr.length && statsArr[0] && typeof statsArr[0] === 'object') {
    outerKeys = Object.keys(statsArr[0]).slice(0, 20).join(', ');
    var inner = statsArr[0].stats || [];
    if (inner.length && inner[0] && typeof inner[0] === 'object') {
      innerKeys = Object.keys(inner[0]).slice(0, 20).join(', ');
    }
  }
  return 'root:[' + rootKeys + ']; stats[]:[' + outerKeys + ']; stats[].stats[]:[' + innerKeys + ']';
}

/** Безопасно обрезает строку (для error_message / тел ответов). */
function wbAdsClip_(s, n) {
  s = String(s == null ? '' : s);
  n = n || 150;
  return s.length > n ? s.substring(0, n) : s;
}
