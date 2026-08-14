/**
 * ══════════════════════════════════════════════════════════════
 * Ads-2 · WbAdsQueryStats.gs — суточные СРЕЗЫ статистики по поисковым запросам.
 *
 * Спека: docs/ADS2_DESIGN_2026-08-14.md (ред. 2, ACK владельца 14.08.2026).
 *
 * СУЩНОСТЬ. Это СРЕЗ за период, а не снимок состояния (в отличие от Ads-3).
 *   Грейн: period_from = period_to = сутки, advert_id × nm_id × norm_query.
 *
 * 🔴 RAW APPEND-ONLY. DELETE не применяется нигде. Целостность даёт не перезапись,
 *   а канонизация НА УРОВНЕ СРЕЗА во вью: выбирается один прогон на период и
 *   показываются строки только этого прогона.
 *   Причина: probe v5 показал, что два обращения за один и тот же период вернули
 *   разное число строк (61 против 44) при идентичных агрегатах. ПРИЧИНА ЭТОГО
 *   РАЗЛИЧИЯ НЕ УСТАНОВЛЕНА и здесь не постулируется. Вывод, который от причины
 *   не зависит: две выдачи за период — две разные retrieval-версии, и смешивать
 *   их в одном представлении нельзя, иначе получится набор запросов, не
 *   существовавший ни в одном ответе API.
 *
 * 🔴 КАНОНИЧЕСКИЙ СЛОЙ ПУБЛИКУЕТ ТОЛЬКО `OK`. PARTIAL и FAILED пишутся в RAW и
 *   run-log как диагностический материал и официальной версией периода не
 *   становятся никогда. Неопубликованные дни видны в V_ADV_QUERY_STATS_COVERAGE.
 *
 * 🔴 ИСТОЧНИК ПАР — НАШ BigQuery (V_ADV_CAMPAIGN_STATS за этот день), не справочник
 *   WB. Осознанный размен: полнота Ads-2 ≤ полнота fullstats. Пара, которой нет в
 *   fullstats за день, не будет опрошена никогда. Размен сделан измеримым:
 *   scope_pairs / scope_spend_rub / day_spend_costs_rub в run-log.
 *
 * 🔴 FAILURE ISOLATION. Наружу не бросает исключений, в WB_ADS_EXPECTED_SOURCES_
 *   не входит, heartbeat не трогает. Вызывается ПОСЛЕ Ads-3: снимок ставок
 *   невосстановим, срез статистики восстановим — при нехватке бюджета жертвуем
 *   этим источником, а не Ads-3.
 *
 * 🔴 НИ ОДНОГО WRITE-ВЫЗОВА В WB. POST /adv/v0/normquery/stats — это чтение
 *   (POST лишь потому, что список пар передаётся телом запроса).
 * ══════════════════════════════════════════════════════════════
 */

var WB_ADV_RAW_QUERY_STATS_SHEET_      = 'RAW_WB_ADV_QUERY_STATS';
var WB_ADV_RAW_QUERY_STATS_RUNS_SHEET_ = 'RAW_WB_ADV_QUERY_STATS_RUNS';

/** Данные среза. Все колонки STRING — конвенция RAW-слоя проекта. */
var WB_ADV_RAW_QUERY_STATS_HEADERS_ = [
  'load_ts', 'run_id', 'source_method', 'processed_status',
  'period_from', 'period_to',
  'advert_id', 'nm_id', 'norm_query',
  // views/ctr/cpm приходят ТОЛЬКО у CPM — у CPC этих ключей в ответе нет вовсе
  // (probe v6: 485 строк из 1255). Пустое значение здесь — контракт, не потеря.
  'views', 'clicks', 'ctr', 'cpc', 'cpm', 'avg_pos',
  'atbs', 'orders', 'shks', 'spend', 'currency',
  'raw_json'
];

/**
 * Run-log: одна строка на СУТКИ, пишется ВСЕГДА, включая returned_rows = 0.
 * Гейты приёмки: returned_packs vs requested_pairs, requested_batches vs expected_batches.
 * Телеметрия (не гейт): max_keys_per_pair, rows_with_views, scope_*, day_spend_costs_rub.
 */
var WB_ADV_RAW_QUERY_STATS_RUNS_HEADERS_ = [
  'load_ts', 'run_id', 'source_method',
  'period_from', 'period_to',
  'requested_pairs', 'expected_batches', 'requested_batches',
  'returned_packs', 'http_status', 'http_success',
  'returned_rows', 'distinct_pairs_with_rows',
  'max_keys_per_pair', 'rows_with_views',
  'scope_pairs', 'scope_spend_rub', 'day_spend_costs_rub',
  'duration_ms', 'status', 'error_message'
];

var WB_ADS_QSTATS_METHOD_      = 'adv/v0/normquery/stats';
var WB_ADS_QSTATS_MAX_ITEMS_   = 100;    // доказано probe v6: 98 пар не режут выдачу
var WB_ADS_QSTATS_PAUSE_MS_    = 6500;   // лимит 10 запросов/мин
var WB_ADS_QSTATS_BUDGET_MS_   = 120000; // собственный потолок (спека §6.10)
var WB_ADS_QSTATS_WINDOW_DAYS_ = 7;      // скользящее окно, как у costs/fullstats
var WB_ADS_QSTATS_TZ_          = 'Europe/Moscow';

/** Круглые значения, на которых max_keys_per_pair похож на обрезку выдачи API. */
var WB_ADS_QSTATS_SUSPICIOUS_CAPS_ = [100, 200, 500, 1000];


// ═══════════════════════════════════════
// ГЛАВНАЯ ТОЧКА ВХОДА
// ═══════════════════════════════════════

/**
 * Собирает суточные срезы за окно. Вызывается из runWbAdsDaily() ПОСЛЕ Ads-3.
 *
 * @param {string=} runId  сквозной id прогона
 * @param {string=} fromDay 'yyyy-MM-dd'; по умолчанию — вчера минус 6 дней
 * @param {string=} toDay   'yyyy-MM-dd'; по умолчанию — вчера
 * @return {{source: string, status: string, rows: number, days: number, days_ok: number}}
 */
function loadWbAdsQueryStatsRaw(runId, fromDay, toDay) {
  var t0 = Date.now();
  var rid = wbAdsResolveRunId_(runId);
  var srcLabel = 'raw_query_stats';

  // Общий на весь execution счётчик HTTP-вызовов: пауза rate limit относится к
  // прогону целиком, а не к отдельным суткам. Считать её внутри дня значило бы
  // сбрасывать throttle на каждой границе суток и упереться в 429.
  var st = { t0: t0, httpCalls: 0 };

  var days;
  try {
    days = wbAdsQsResolveDays_(fromDay, toDay);
  } catch (eRange) {
    console.error('  query_stats: неверный диапазон — ' + ((eRange && eRange.message) || eRange));
    return { source: srcLabel, status: 'FAILED', rows: 0, days: 0, days_ok: 0 };
  }

  var tok = null;
  try { tok = getWbAdsToken_(); } catch (eTok) { tok = null; }
  if (!tok) {
    console.error('  query_stats BLOCKED: нет WB Promotion токена');
    return { source: srcLabel, status: 'BLOCKED', rows: 0, days: days.length, days_ok: 0 };
  }

  // Телеметрия покрытия по всему окну — ОДНИМ запросом, а не по одному на день.
  // Best-effort: недоступность этой цифры не влияет на статус суток.
  var costsByDay = wbAdsQsCostsByDay_(days);

  var totalRows = 0, daysOk = 0, daysPartial = 0, daysFailed = 0, daysEmpty = 0;

  for (var i = 0; i < days.length; i++) {
    var res;
    try {
      res = wbAdsQsProcessDay_(tok.token, rid, days[i], costsByDay[days[i]], st);
    } catch (eDay) {
      // Исключение на одних сутках не должно останавливать остальные.
      res = { status: 'FAILED', rows: 0 };
      console.error('  query_stats ' + days[i] + ' исключение: ' + ((eDay && eDay.message) || eDay));
    }
    totalRows += res.rows;
    if (res.status === 'OK') daysOk++;
    else if (res.status === 'PARTIAL') daysPartial++;
    else if (res.status === 'EMPTY') daysEmpty++;
    else daysFailed++;

    if (wbAdsQsOutOfBudget_(st)) {
      var left = days.length - i - 1;
      if (left > 0) {
        console.log('  query_stats: тайм-бюджет исчерпан, не обработано суток: ' + left);
        daysFailed += left;
      }
      break;
    }
  }

  // Статус прогона — агрегат по суткам. Он диагностический: в heartbeat не идёт
  // (WB_ADS_EXPECTED_SOURCES_ остаётся 3) и на витрину не влияет.
  var overall = daysFailed > 0 ? 'PARTIAL'
    : (daysOk > 0 ? 'OK' : (daysEmpty > 0 ? 'EMPTY' : 'PARTIAL'));
  if (daysOk === 0 && daysPartial === 0 && daysEmpty === 0) overall = 'FAILED';

  console.log('  query_stats ' + overall +
    ' | суток ' + days.length + ' (OK ' + daysOk + ', PARTIAL ' + daysPartial +
    ', EMPTY ' + daysEmpty + ', FAILED ' + daysFailed + ')' +
    ' | строк ' + totalRows +
    ' | HTTP ' + st.httpCalls +
    ' | ' + (Date.now() - t0) + 'мс');

  return { source: srcLabel, status: overall, rows: totalRows,
           days: days.length, days_ok: daysOk };
}


// ═══════════════════════════════════════
// ОДНИ СУТКИ = ОДИН СРЕЗ = ОДНА СТРОКА RUN-LOG
// ═══════════════════════════════════════

/**
 * Обрабатывает одни сутки и пишет ровно одну строку run-log.
 *
 * 🔴 СТАТУСЫ (спека §4.2, §6.6). Различаются по ПРОИСХОЖДЕНИЮ, а не по объёму:
 *   FAILED  — scope неизвестен (BQ не отдал пары) ИЛИ ни одна пачка не вернула 200
 *             ИЛИ не записался run-log. Срез за сутки недостоверен.
 *   EMPTY   — BQ отработал, пар за сутки нет. Запросов не делали, снимать нечего.
 *             Это НОРМА (реклама не крутилась), а не сбой.
 *   PARTIAL — часть пачек не доехала, ИЛИ вышли по тайм-бюджету, ИЛИ вернулось
 *             пакетов меньше, чем запрошено пар. Данные есть, но срез неполон.
 *   OK      — все пачки отработали И returned_packs == requested_pairs.
 *             returned_rows = 0 при этом ДОПУСТИМО: у пар были показы, но не в поиске.
 *
 * 🔴 EMPTY и FAILED при нуле пар внешне одинаковы (ноль строк). Разделяет их только
 *    статус, поэтому схлопывать их нельзя: EMPTY — норма, FAILED — сутки без среза.
 */
function wbAdsQsProcessDay_(token, rid, day, dayCostsRub, st) {
  var tDay = Date.now();
  var nowTs = wbAdsNow_();

  var log = {
    load_ts: nowTs, run_id: rid, source_method: WB_ADS_QSTATS_METHOD_,
    period_from: day, period_to: day,
    requested_pairs: 0, expected_batches: 0, requested_batches: 0,
    returned_packs: 0, http_status: '', http_success: 'FALSE',
    returned_rows: 0, distinct_pairs_with_rows: 0,
    max_keys_per_pair: 0, rows_with_views: 0,
    scope_pairs: 0, scope_spend_rub: '',
    day_spend_costs_rub: (dayCostsRub != null ? dayCostsRub : ''),
    duration_ms: 0, status: 'FAILED', error_message: ''
  };

  // 1) Scope — пары из НАШЕГО хранилища.
  var scope = wbAdsQsPairsForDay_(day);
  if (!scope.ok) {
    // 🔴 Недоступность BigQuery — это FAILED, а не «пустой scope». Пустой список
    //    пар из-за сбоя запроса и пустой список из-за отсутствия рекламы дают
    //    одинаковый ноль, но означают противоположное.
    log.status = 'FAILED';
    log.error_message = 'Scope не получен из BigQuery: ' + scope.error;
    return wbAdsQsFinishDay_(log, tDay, 0);
  }

  log.scope_pairs = scope.pairs.length;
  log.requested_pairs = scope.pairs.length;
  log.scope_spend_rub = wbAdsQsRound2_(scope.spendRub);

  if (!scope.pairs.length) {
    log.status = 'EMPTY';
    log.error_message = 'Валидный пустой scope: за ' + day +
      ' нет ни одной пары advert×nm в V_ADV_CAMPAIGN_STATS';
    return wbAdsQsFinishDay_(log, tDay, 0);
  }

  // 2) Опрос пачками <= 100.
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = wbAdvRawEnsureSheet_(ss, WB_ADV_RAW_QUERY_STATS_SHEET_,
                                   WB_ADV_RAW_QUERY_STATS_HEADERS_);

  var chunks = wbAdsChunk_(scope.pairs, WB_ADS_QSTATS_MAX_ITEMS_);
  log.expected_batches = chunks.length;

  var written = 0, anyOk = false, failed = 0, lastCode = '';
  var pairsWithRows = {}, maxKeys = 0, rowsWithViews = 0;

  for (var ch = 0; ch < chunks.length; ch++) {
    if (wbAdsQsOutOfBudget_(st)) {
      // Выход по бюджету — это НЕПОЛНЫЙ срез, а не успех. Ловится ниже сравнением
      // requested_batches с expected_batches.
      log.error_message = wbAdsQsAppendMsg_(log.error_message,
        'тайм-бюджет исчерпан на пачке ' + (ch + 1) + ' из ' + chunks.length);
      break;
    }
    if (st.httpCalls > 0) Utilities.sleep(WB_ADS_QSTATS_PAUSE_MS_);
    st.httpCalls++;
    log.requested_batches++;

    var items = chunks[ch].map(function (p) {
      return { advert_id: p.advertId, nm_id: p.nmId };
    });
    // URL собран литералом, а не склейкой из константы-метки: единственный
    // эндпоинт этого файла должен находиться простым grep'ом по коду, иначе
    // проверка «ни одного write-вызова в WB» опирается на чтение глазами.
    var resp = wbAdsHttp_('post', WB_ADS_API_HOST_ + '/adv/v0/normquery/stats',
                          token, { from: day, to: day, items: items });
    lastCode = resp.code;

    if (!resp.ok) {
      failed++;
      log.error_message = wbAdsQsAppendMsg_(log.error_message,
        'пачка ' + (ch + 1) + ' HTTP ' + resp.code);
      continue;
    }
    anyOk = true;

    var flat = wbAdsQsFlatten_(resp.json, rid, day, nowTs);
    log.returned_packs += flat.packs;
    if (flat.maxKeys > maxKeys) maxKeys = flat.maxKeys;
    rowsWithViews += flat.rowsWithViews;
    for (var k = 0; k < flat.pairsWithRows.length; k++) pairsWithRows[flat.pairsWithRows[k]] = 1;

    if (flat.rows.length) written += wbAdvRawAppendRows_(sheet, flat.rows);
  }

  log.http_status = String(lastCode);
  log.http_success = anyOk ? 'TRUE' : 'FALSE';
  log.returned_rows = written;
  log.distinct_pairs_with_rows = Object.keys(pairsWithRows).length;
  log.max_keys_per_pair = maxKeys;
  log.rows_with_views = rowsWithViews;

  // 🔴 Полнота среза — тремя счётчиками, без порогов на объём.
  //    returned_packs < requested_pairs имеет смысл ТОЛЬКО потому, что доказано
  //    (probe v5/v6): WB возвращает пакет на каждую запрошенную пару, даже пустой.
  var batchesIncomplete = (log.requested_batches < log.expected_batches);
  var packsIncomplete = (log.returned_packs < log.requested_pairs);

  if (!anyOk) {
    log.status = 'FAILED';
    log.error_message = wbAdsQsAppendMsg_(log.error_message,
      'ни одна пачка не вернула HTTP 200');
  } else if (failed > 0 || batchesIncomplete || packsIncomplete) {
    log.status = 'PARTIAL';
    if (packsIncomplete) {
      log.error_message = wbAdsQsAppendMsg_(log.error_message,
        'пакетов ' + log.returned_packs + ' при ' + log.requested_pairs + ' парах');
    }
  } else {
    log.status = 'OK';   // в том числе при returned_rows = 0
  }

  // Телеметрия потолка выдачи. НЕ влияет на статус (спека §6.7): потолок API
  // неизвестен, и рост, и падение этого числа законны.
  if (maxKeys > 0 && WB_ADS_QSTATS_SUSPICIOUS_CAPS_.indexOf(maxKeys) >= 0) {
    console.log('  ⚠️ query_stats ' + day + ': max_keys_per_pair = ' + maxKeys +
      ' — круглое значение, похоже на обрезку выдачи. Нужен отдельный замер.');
  }

  return wbAdsQsFinishDay_(log, tDay, written);
}


/**
 * Пишет строку run-log и логирует итог суток.
 * 🔴 Потеря run-log сама по себе делает срез недостоверным: без run-log вью не
 *    сможет выбрать канонический прогон, и строки данных этих суток окажутся
 *    невидимыми. Поэтому статус деградирует до FAILED.
 */
function wbAdsQsFinishDay_(log, tDay, rows) {
  log.duration_ms = Date.now() - tDay;

  var runLogOk = true;
  try {
    var ss = SpreadsheetApp.getActiveSpreadsheet();
    var runsSheet = wbAdvRawEnsureSheet_(ss, WB_ADV_RAW_QUERY_STATS_RUNS_SHEET_,
                                         WB_ADV_RAW_QUERY_STATS_RUNS_HEADERS_);
    wbAdvRawAppendRows_(runsSheet, [log]);
  } catch (e) {
    runLogOk = false;
    console.error('  query_stats ' + log.period_from + ': run-log НЕ записан: ' +
      ((e && e.message) || e));
  }
  if (!runLogOk) {
    log.error_message = wbAdsQsAppendMsg_(log.error_message,
      'RUN-LOG НЕ ЗАПИСАН (исходный статус ' + log.status + ')');
    log.status = 'FAILED';
  }

  console.log('  query_stats ' + log.period_from + ' ' + log.status +
    ' | пар ' + log.requested_pairs +
    ' | пачек ' + log.requested_batches + '/' + log.expected_batches +
    ' | пакетов ' + log.returned_packs +
    ' | строк ' + log.returned_rows +
    ' | пар со строками ' + log.distinct_pairs_with_rows +
    ' | max ключей ' + log.max_keys_per_pair +
    ' | ' + log.duration_ms + 'мс' +
    (log.error_message ? ' | ' + log.error_message : ''));

  return { status: log.status, rows: rows };
}


// ═══════════════════════════════════════
// SCOPE: ПАРЫ ЗА СУТКИ ИЗ НАШЕГО BigQuery
// ═══════════════════════════════════════

/**
 * Пары advert×nm, у которых за эти сутки есть строка в V_ADV_CAMPAIGN_STATS.
 *
 * 🔴 Осознанное ограничение полноты (спека §5.2): полнота Ads-2 ≤ полнота fullstats.
 *    Пара, которой нет в fullstats за день, не будет опрошена НИКОГДА — не «позже»,
 *    а никогда, если день не пересобрать вручную.
 *    Альтернатива (все пары из справочника WB) отвергнута: в probe v5 сто таких пар
 *    дали ноль строк, потому что показов у них не было.
 *
 * @return {{ok: boolean, pairs: Array, spendRub: number, error: string}}
 */
function wbAdsQsPairsForDay_(day) {
  var out = { ok: false, pairs: [], spendRub: 0, error: '' };

  // Защита от подстановки: дата собирается нами, но в SQL идёт текстом.
  if (!/^\d{4}-\d{2}-\d{2}$/.test(String(day))) {
    out.error = 'некорректная дата: ' + day;
    return out;
  }

  try {
    var c = getBqConfig_();
    var sql =
      'SELECT advertId, nmId, SUM(SAFE_CAST(`sum` AS FLOAT64)) AS spend\n' +
      'FROM `' + c.projectId + '.' + c.datasetId + '.V_ADV_CAMPAIGN_STATS`\n' +
      "WHERE DATE(`date`) = DATE('" + day + "')\n" +
      '  AND advertId IS NOT NULL AND nmId IS NOT NULL\n' +
      'GROUP BY advertId, nmId';
    var r = bqQuery_(sql);
    var rows = (r && r.rows) || [];
    for (var i = 0; i < rows.length; i++) {
      var f = rows[i].f;
      var advertId = Number(f[0].v), nmId = Number(f[1].v);
      if (!advertId || !nmId) continue;
      out.pairs.push({ advertId: advertId, nmId: nmId });
      out.spendRub += Number(f[2].v) || 0;
    }
    out.ok = true;
    return out;
  } catch (e) {
    out.error = (e && e.message) || String(e);
    return out;
  }
}

/**
 * Расход по балансу за каждые сутки окна — ОДНИМ запросом на всё окно.
 * Телеметрия покрытия (§5.2): знаменатель для доли «сколько денег мы вообще
 * пытались объяснить». Best-effort — недоступность не влияет на статус суток.
 * ⚠️ Осмысленно только после исправления ключа дедупа V_ADV_COSTS
 *    (docs/ADS_COSTS_DEDUP_FIX_2026-08-14.md): до него знаменатель завышен.
 * @return {Object} 'yyyy-MM-dd' -> число
 */
function wbAdsQsCostsByDay_(days) {
  var out = {};
  if (!days || !days.length) return out;
  try {
    var c = getBqConfig_();
    var sql =
      "SELECT FORMAT_DATE('%Y-%m-%d', DATE(updDate)) AS d,\n" +
      '       SUM(SAFE_CAST(updSum AS FLOAT64)) AS cost\n' +
      'FROM `' + c.projectId + '.' + c.datasetId + '.V_ADV_COSTS`\n' +
      "WHERE DATE(updDate) BETWEEN DATE('" + days[0] + "') AND DATE('" + days[days.length - 1] + "')\n" +
      'GROUP BY d';
    var r = bqQuery_(sql);
    var rows = (r && r.rows) || [];
    for (var i = 0; i < rows.length; i++) {
      out[String(rows[i].f[0].v)] = wbAdsQsRound2_(Number(rows[i].f[1].v) || 0);
    }
  } catch (e) {
    console.log('  query_stats: телеметрия расхода по балансу недоступна — ' +
      ((e && e.message) || e));
  }
  return out;
}


// ═══════════════════════════════════════
// РАЗБОР ОТВЕТА
// ═══════════════════════════════════════

/**
 * Ответ normquery/stats → плоские строки среза.
 * Defensive по обёрткам: у WB встречаются snake_case и camelCase.
 * 🔴 Контекст кампании (payment_type/bid_type/status) НЕ пишем: строка описывает
 *    ПРОШЛЫЙ период, а справочник отдал бы СЕГОДНЯШНЕЕ состояние кампании.
 *    Кому нужен payment_type на дату — джойнит V_ADV_CAMPAIGNS (там снимки по дням).
 */
function wbAdsQsFlatten_(json, rid, day, nowTs) {
  var out = { rows: [], packs: 0, pairsWithRows: [], maxKeys: 0, rowsWithViews: 0 };
  var arr = (json && (json.stats || json.data)) || [];

  for (var s = 0; s < arr.length; s++) {
    var pack = arr[s] || {};
    out.packs++;
    var advertId = (pack.advert_id != null) ? pack.advert_id : pack.advertId;
    var nmId = (pack.nm_id != null) ? pack.nm_id : pack.nmId;
    var inner = pack.stats || pack.items || [];

    if (inner.length) out.pairsWithRows.push(advertId + '|' + nmId);
    if (inner.length > out.maxKeys) out.maxKeys = inner.length;

    for (var c = 0; c < inner.length; c++) {
      var cl = inner[c] || {};
      // 'views' есть только у CPM. Считаем строки с ним — это детектор смены
      // контракта WB, а не признак качества данных.
      if (cl.views != null) out.rowsWithViews++;
      out.rows.push({
        load_ts: nowTs, run_id: rid,
        source_method: WB_ADS_QSTATS_METHOD_, processed_status: 'raw',
        period_from: day, period_to: day,
        advert_id: advertId, nm_id: nmId,
        norm_query: (cl.norm_query != null) ? cl.norm_query
          : (cl.normQuery != null ? cl.normQuery : ''),
        views: cl.views, clicks: cl.clicks, ctr: cl.ctr, cpc: cl.cpc, cpm: cl.cpm,
        avg_pos: (cl.avg_pos != null) ? cl.avg_pos : cl.avgPos,
        atbs: cl.atbs, orders: cl.orders, shks: cl.shks,
        spend: cl.spend, currency: cl.currency,
        raw_json: wbAdvRawJson_(cl)
      });
    }
  }
  return out;
}


// ═══════════════════════════════════════
// РУЧНЫЕ ТОЧКИ ВХОДА (backfill помесячно)
// ═══════════════════════════════════════

/**
 * Backfill за произвольный период, посуточно. Запускается ВРУЧНУЮ, помесячно.
 * Осознанно без чекпоинтов и без самоснимающегося триггера — ровно того механизма,
 * на котором сломался WbAdsClustersJob (триггер удалял сам себя).
 * Идемпотентен по построению: ничего не удаляет, канонизация выберет последний OK.
 */
function wbAdsQueryStatsBackfill(fromDay, toDay) {
  if (!fromDay || !toDay) {
    console.error('wbAdsQueryStatsBackfill(from, to): укажите обе даты, например ' +
      "wbAdsQueryStatsBackfill('2026-04-13','2026-04-30')");
    return null;
  }
  console.log('═══ Ads-2 backfill ' + fromDay + '…' + toDay + ' ═══');
  return loadWbAdsQueryStatsRaw(null, fromDay, toDay);
}


// ═══════════════════════════════════════
// ВСПОМОГАТЕЛЬНОЕ
// ═══════════════════════════════════════

/**
 * Список суток окна. По умолчанию — скользящие 7 дней, заканчивая ВЧЕРА.
 * Сегодняшние сутки не берём: они неполные, и срез по ним заведомо частичный.
 */
function wbAdsQsResolveDays_(fromDay, toDay) {
  var to, from;
  if (fromDay || toDay) {
    from = String(fromDay || toDay);
    to = String(toDay || fromDay);
    if (!/^\d{4}-\d{2}-\d{2}$/.test(from) || !/^\d{4}-\d{2}-\d{2}$/.test(to)) {
      throw new Error('ожидается yyyy-MM-dd, получено ' + from + '…' + to);
    }
  } else {
    var yesterday = new Date(Date.now() - 86400000);
    to = Utilities.formatDate(yesterday, WB_ADS_QSTATS_TZ_, 'yyyy-MM-dd');
    var start = new Date(Date.now() - 86400000 * WB_ADS_QSTATS_WINDOW_DAYS_);
    from = Utilities.formatDate(start, WB_ADS_QSTATS_TZ_, 'yyyy-MM-dd');
  }

  var d = new Date(from + 'T00:00:00Z');
  var end = new Date(to + 'T00:00:00Z');
  if (isNaN(d.getTime()) || isNaN(end.getTime())) {
    throw new Error('не разобрать даты: ' + from + '…' + to);
  }
  if (d > end) throw new Error('from позже to: ' + from + ' > ' + to);

  var out = [];
  while (d <= end) {
    out.push(Utilities.formatDate(d, 'UTC', 'yyyy-MM-dd'));
    d = new Date(d.getTime() + 86400000);
    if (out.length > 400) throw new Error('окно длиннее 400 суток — вероятно ошибка в датах');
  }
  return out;
}

function wbAdsQsOutOfBudget_(st) {
  return (Date.now() - st.t0) > WB_ADS_QSTATS_BUDGET_MS_;
}

function wbAdsQsAppendMsg_(existing, msg) {
  return existing ? (existing + '; ' + msg) : msg;
}

function wbAdsQsRound2_(v) {
  var n = Number(v);
  return isFinite(n) ? Math.round(n * 100) / 100 : 0;
}
