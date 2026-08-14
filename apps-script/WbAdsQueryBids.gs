/**
 * ══════════════════════════════════════════════════════════════
 * Ads-3 · WbAdsQueryBids.gs — суточный SNAPSHOT ставок по поисковым кластерам.
 *
 * ЗАЧЕМ. У WB НЕТ эндпоинта истории ставок. `get-bids` отдаёт только ТЕКУЩЕЕ
 *   состояние. Значит история ставки по конкретному запросу не существует до
 *   первого снимка и не может быть восстановлена задним числом — каждый день
 *   без снимка потерян безвозвратно. Это единственная задача файла.
 *
 * SCOPE (доказанный контур, Advertising Data Contract v1.2):
 *   опрашиваем ТОЛЬКО payment_type='cpm' И bid_type='manual'.
 *     • unified  → get-bids возвращает пустой bids[] (probe v2: 0 строк по 2 парам);
 *     • CPC+manual → пустой bids[] при HTTP 200 (probe v4: 5 пар, все 0).
 *   Оба случая — ВАЛИДНЫЙ исход контракта, а не дефект. Тратить на них запросы
 *   незачем. ⚠️ Из probe v4 НЕ следует глобальное «у CPC кластерных ставок нет» —
 *   доказано только их отсутствие в get-bids для проверенных 5 пар из 23.
 *
 * ИДЕМПОТЕНТНОСТЬ: APPEND-ONLY. Никакого replace-slice и никакого DELETE.
 *   Это противоположность RAW_WB_ADV_SEARCH_CLUSTERS, где перезапись периода
 *   уместна. Здесь перезапись вчерашнего снимка = потеря того единственного,
 *   что файл создаёт. Повторный запуск в те же сутки даёт ещё один снимок;
 *   выбор одного снимка на сутки делает ВЬЮ, а не загрузчик.
 *   🔴 ИДЕНТИЧНОСТЬ СНИМКА = (snapshot_date, snapshot_ts, run_id). Не один
 *   snapshot_ts: он с точностью до секунды, и два execution, стартовавших в
 *   одну секунду, получат одинаковый ts при разных run_id. Ключом снимка
 *   является run_id — уникальный на execution; ts несёт время, дата — сутки.
 *
 * 🔴 FAILURE ISOLATION. Функция НИКОГДА не бросает исключение наружу и не влияет
 *   на overall-статус рекламного прогона: витрина ставки по кластерам не
 *   потребляет, и сбой этого источника не должен ронять heartbeat и
 *   останавливать сборку MART_SKU_DAILY. Всё ловится внутри, наружу уходит
 *   только { source, status, rows } для диагностики.
 *
 * 🔴 НИ ОДНОГО WRITE-ВЫЗОВА В WB. Только POST /adv/v0/normquery/get-bids —
 *   это чтение (POST лишь потому, что список пар передаётся телом запроса).
 *
 * Спека: docs/ADS3_FINAL_DESIGN_2026-08-13.md
 * ══════════════════════════════════════════════════════════════
 */

var WB_ADV_RAW_QUERY_BIDS_SHEET_      = 'RAW_WB_ADV_QUERY_BIDS';
var WB_ADV_RAW_QUERY_BIDS_RUNS_SHEET_ = 'RAW_WB_ADV_QUERY_BIDS_RUNS';

/** Данные снимка. Все колонки STRING — конвенция RAW-слоя проекта (типизация ниже по течению). */
var WB_ADV_RAW_QUERY_BIDS_HEADERS_ = [
  'load_ts', 'run_id', 'source_method', 'processed_status',
  'snapshot_ts', 'snapshot_date',
  'advert_id', 'nm_id', 'norm_query',
  'bid', 'bid_kopecks', 'currency',
  'payment_type', 'bid_type', 'campaign_status',
  'raw_json'
];

/** Run-log: пишется ВСЕГДА, в том числе при returned_bid_rows = 0. */
var WB_ADV_RAW_QUERY_BIDS_RUNS_HEADERS_ = [
  'load_ts', 'run_id', 'source_method',
  'snapshot_ts', 'snapshot_date',
  'requested_pairs', 'expected_batches', 'requested_batches',
  'http_status', 'http_success',
  'returned_bid_rows', 'distinct_pairs_with_bids',
  'duration_ms', 'status', 'error_message'
];

var WB_ADS_QBIDS_METHOD_     = 'adv/v0/normquery/get-bids';
var WB_ADS_QBIDS_MAX_ITEMS_  = 100;    // контракт API: <= 100 items на запрос
var WB_ADS_QBIDS_PAUSE_MS_   = 250;    // лимит get-bids 5 запросов/сек — берём с запасом
var WB_ADS_QBIDS_BUDGET_MS_  = 90000;  // собственный потолок, чтобы не съесть тайм-бюджет прогона


// ═══════════════════════════════════════
// ГЛАВНАЯ ТОЧКА ВХОДА
// ═══════════════════════════════════════

/**
 * Снимает текущие ставки по поисковым кластерам для CPM+manual пар.
 * Вызывается из runWbAdsDaily() ПОСЛЕ трёх mart-критичных загрузчиков,
 * ОТДЕЛЬНОЙ переменной — НЕ в массиве results (см. failure isolation в шапке).
 *
 * @param {string} runId сквозной id прогона
 * @return {{source: string, status: string, rows: number, pairs: number}}
 */
function loadWbAdsQueryBidsRaw(runId) {
  var t0 = Date.now();
  var rid = wbAdsResolveRunId_(runId);
  var srcLabel = 'raw_query_bids';

  // ОДИН snapshot_ts на весь прогон: все строки этого снимка обязаны иметь
  // одно и то же значение, иначе грейн распадётся и вью не схлопнет ретрай.
  // ⚠️ Сам по себе snapshot_ts снимок НЕ идентифицирует — точность до секунды.
  //    Идентичность снимка даёт пара (snapshot_ts, run_id): rid получен выше,
  //    уникален на execution и пишется в КАЖДУЮ строку снимка и в run-log.
  //    Без run_id два execution одной секунды слились бы во вью в гибрид.
  var snapshotTs = wbAdsNow_();                       // 'yyyy-MM-dd HH:mm:ss' по Europe/Moscow
  var snapshotDate = snapshotTs.substring(0, 10);     // 'yyyy-MM-dd'

  var log = {
    load_ts: snapshotTs, run_id: rid, source_method: WB_ADS_QBIDS_METHOD_,
    snapshot_ts: snapshotTs, snapshot_date: snapshotDate,
    requested_pairs: 0, expected_batches: 0, requested_batches: 0,
    http_status: '', http_success: 'FALSE',
    returned_bid_rows: 0, distinct_pairs_with_bids: 0,
    duration_ms: 0, status: 'ERROR', error_message: ''
  };

  try {
    var tok = getWbAdsToken_();
    if (!tok) {
      log.status = 'BLOCKED';
      log.error_message = 'Нет WB Promotion токена';
      return wbAdsQbFinish_(rid, srcLabel, log, t0, 0);
    }

    // 1) Список пар CPM+manual. Собирается СОБСТВЕННЫМИ вызовами count+adverts,
    //    а не чтением RAW этого прогона (см. врезку у вызова в runWbAdsDaily).
    var collected = wbAdsCollectCpmManualPairs_(tok.token);
    log.requested_pairs = collected.pairs.length;

    // 🔴 requested_pairs = 0 САМ ПО СЕБЕ не доказывает ни сбоя, ни пустоты.
    //    Ноль пар получается в двух принципиально разных случаях:
    //      • справочник не доехал (count/adverts упали) → FAILED, снимка за день нет;
    //      • CPM+manual кампаний действительно нет      → EMPTY, снимать нечего.
    //    Свалить их в один ERROR значит либо поднимать ложную тревогу каждый день,
    //    когда все CPM-кампании выключены, либо — что хуже — приучиться игнорировать
    //    этот статус и пропустить настоящий сбой справочника. Различаем по исходам
    //    самих вызовов, а не по количеству пар.
    //    Отдельно: неполный справочник (часть пачек /adverts не доехала) при нуле пар
    //    не даёт права сказать «пусто» — непросмотренная пачка могла содержать
    //    CPM+manual. Такой исход тоже FAILED.
    if (!collected.pairs.length) {
      var advTotal = collected.advBatchesTotal, advOk = collected.advBatchesOk;
      if (!collected.countOk) {
        log.status = 'FAILED';
        log.error_message = 'Справочник не доехал: /promotion/count HTTP ' +
          (collected.countHttp || 'нет ответа');
      } else if (advTotal > 0 && advOk === 0) {
        log.status = 'FAILED';
        log.error_message = 'Справочник не доехал: /adverts не отдал ни одной из ' +
          advTotal + ' пачек (' + collected.idsFound + ' кампаний в count)';
      } else if (advOk < advTotal) {
        log.status = 'FAILED';
        log.error_message = 'Справочник неполон (' + advOk + '/' + advTotal +
          ' пачек) и пар нет — пустой scope НЕ доказан';
      } else {
        // count отработал, все пачки /adverts отработали, CPM+manual просто нет.
        log.status = 'EMPTY';
        log.error_message = 'Валидный пустой scope: среди ' + collected.idsFound +
          ' кампаний нет ни одной CPM+manual';
      }
      // http_status/http_success относятся к get-bids, а он не вызывался ни разу:
      // подставлять сюда код справочника значит врать о том, чего не происходило.
      log.http_status = '';
      log.http_success = 'FALSE';
      return wbAdsQbFinish_(rid, srcLabel, log, t0, 0);
    }

    // Неполный справочник при НЕпустом списке пар: опросить можно, но снимок
    // заведомо неполный по составу — это PARTIAL, а не OK (см. ниже scopeIncomplete).
    var scopeIncomplete = (collected.advBatchesOk < collected.advBatchesTotal);
    if (scopeIncomplete) {
      log.error_message = 'Справочник неполон: ' + collected.advBatchesOk + '/' +
        collected.advBatchesTotal + ' пачек /adverts';
    }

    // 2) Опрос пачками <= 100.
    var ss = SpreadsheetApp.getActiveSpreadsheet();
    var sheet = wbAdvRawEnsureSheet_(ss, WB_ADV_RAW_QUERY_BIDS_SHEET_, WB_ADV_RAW_QUERY_BIDS_HEADERS_);

    var ctx = collected.ctx;                       // 'advert|nm' -> {payment_type,bid_type,status}
    var chunks = wbAdsChunk_(collected.pairs, WB_ADS_QBIDS_MAX_ITEMS_);
    log.expected_batches = chunks.length;
    var written = 0, pairsWithBids = {}, lastCode = '', anyOk = false, failed = 0;

    for (var ch = 0; ch < chunks.length; ch++) {
      if (Date.now() - t0 > WB_ADS_QBIDS_BUDGET_MS_) {
        // 🔴 Выход по тайм-бюджету — это НЕПОЛНЫЙ снимок, а не успех.
        //    Раньше break не влиял на статус, и прогон, оборвавшийся после
        //    первой удачной пачки, получал OK. Неполнота ловится ниже
        //    сравнением requested_batches с expected_batches.
        log.error_message = 'Тайм-бюджет исчерпан на пачке ' + (ch + 1) + ' из ' + chunks.length;
        break;
      }
      if (ch > 0) Utilities.sleep(WB_ADS_QBIDS_PAUSE_MS_);
      log.requested_batches++;

      var items = chunks[ch].map(function (p) { return { advert_id: p.advertId, nm_id: p.nmId }; });
      var resp = wbAdsHttp_('post', WB_ADS_API_HOST_ + '/adv/v0/normquery/get-bids',
                            tok.token, { items: items });
      lastCode = resp.code;
      if (!resp.ok) {
        failed++;
        log.error_message = (log.error_message ? log.error_message + '; ' : '') +
          'пачка ' + (ch + 1) + ' HTTP ' + resp.code;
        continue;
      }
      anyOk = true;

      var bids = (resp.json && (resp.json.bids || resp.json.items)) || [];
      // ⚠️ Пустой bids[] — ВАЛИДНЫЙ исход контракта (ставки могут быть не выставлены).
      //    Не ошибка, не warning, просто ноль строк у этой пачки.
      var rows = [];
      for (var b = 0; b < bids.length; b++) {
        var it = bids[b] || {};
        var key = it.advert_id + '|' + it.nm_id;
        var c = ctx[key] || {};
        pairsWithBids[key] = 1;
        rows.push({
          load_ts: snapshotTs,
          run_id: rid,
          source_method: WB_ADS_QBIDS_METHOD_,
          processed_status: 'raw',
          snapshot_ts: snapshotTs,
          snapshot_date: snapshotDate,
          advert_id: it.advert_id,
          nm_id: it.nm_id,
          norm_query: it.norm_query,
          // source-faithful: пишем ОБА поля ровно как отдал API, без пересчёта
          // одного из другого. Расхождение ловит QC ниже по течению — это
          // детектор смены единиц на стороне WB (ср. spp_percent).
          bid: it.bid,
          bid_kopecks: it.bid_kopecks,
          currency: it.currency,
          // контекст кампании на момент снимка: без payment_type ставки CPC (₽/клик)
          // и CPM (₽/1000 показов) смешаются в одной колонке и станут бессмысленными
          payment_type: c.payment_type || '',
          bid_type: c.bid_type || '',
          campaign_status: (c.status != null ? c.status : ''),
          raw_json: wbAdvRawJson_(it)
        });
      }
      if (rows.length) written += wbAdvRawAppendRows_(sheet, rows);
    }

    log.http_status = String(lastCode);
    log.http_success = anyOk ? 'TRUE' : 'FALSE';
    log.returned_bid_rows = written;
    log.distinct_pairs_with_bids = Object.keys(pairsWithBids).length;

    // Снимок считается полным ТОЛЬКО если отработали все пачки get-bids без сбоев
    // И если состав опрашиваемых пар был собран по ПОЛНОМУ справочнику.
    var incomplete = (log.requested_batches < log.expected_batches);
    if (!anyOk)                                          log.status = 'FAILED';
    else if (failed > 0 || incomplete || scopeIncomplete) log.status = 'PARTIAL';
    else                                                 log.status = 'OK';   // в т.ч. при returned_bid_rows = 0

    return wbAdsQbFinish_(rid, srcLabel, log, t0, written);

  } catch (e) {
    // Ничего не выпускаем наружу: исключение здесь не должно ронять прогон рекламы.
    log.status = 'FAILED';
    log.error_message = 'Исключение: ' + ((e && e.message) || String(e));
    try { return wbAdsQbFinish_(rid, srcLabel, log, t0, 0); }
    catch (e2) { return { source: srcLabel, status: 'FAILED', rows: 0, pairs: 0 }; }
  }
}


// ═══════════════════════════════════════
// СПИСОК ПАР CPM + MANUAL
// ═══════════════════════════════════════

/**
 * Собирает пары advert_id+nm_id ТОЛЬКО по кампаниям payment_type='cpm' и bid_type='manual'.
 * Источник — /adv/v1/promotion/count (список id по статусам) + /api/advert/v2/adverts (детали).
 * Оба вызова — GET, чтение.
 *
 * 🔴 Возвращает не только пары, но и ПРОИСХОЖДЕНИЕ пустоты. Пустой список пар сам по
 *    себе ничего не доказывает: он одинаково выглядит и когда справочник не доехал
 *    (сбой), и когда CPM+manual кампаний действительно нет (валидный пустой scope).
 *    Различить их можно только по тому, чем закончились сами вызовы, поэтому
 *    countOk / advBatchesOk / advBatchesTotal / idsFound — часть контракта функции.
 *
 * @return {{pairs: Array, ctx: Object, countHttp: (number|string), countOk: boolean,
 *           idsFound: number, advBatchesTotal: number, advBatchesOk: number}}
 */
function wbAdsCollectCpmManualPairs_(token) {
  var out = { pairs: [], ctx: {}, countHttp: '', countOk: false,
              idsFound: 0, advBatchesTotal: 0, advBatchesOk: 0 };

  var cResp = wbAdsHttp_('get', WB_ADS_API_HOST_ + '/adv/v1/promotion/count', token, null);
  out.countHttp = cResp.code;
  if (!cResp.ok || !cResp.json) return out;   // countOk остаётся false — это СБОЙ, не пустота
  out.countOk = true;

  // Плоский список advertId из групп ответа count.
  // ⚠️ Разбор ОБЯЗАН повторять defensive-логику loadWbAdsCampaignsRaw
  //    (WbAdsRawLoader.gs:306-310): у WB встречаются обе формы обёртки
  //    (adverts | data.adverts) и обе формы списка (advert_list | advertList).
  //    Более бедный парсер дал бы тихий ноль пар и ложный ERROR «справочник
  //    не загрузился» при полностью исправном API.
  var ids = [], seenId = {};
  var groups = (cResp.json.adverts || (cResp.json.data && cResp.json.data.adverts)) || [];
  for (var g = 0; g < groups.length; g++) {
    var list = (groups[g] && (groups[g].advert_list || groups[g].advertList)) || [];
    for (var i = 0; i < list.length; i++) {
      var id = list[i] && (list[i].advertId || list[i].advertID || list[i].id);
      if (id != null && !seenId[id]) { seenId[id] = 1; ids.push(Number(id)); }
    }
  }
  out.idsFound = ids.length;
  // count отработал, но кампаний нет вообще — это валидный пустой scope, а не сбой.
  if (!ids.length) return out;

  var seenPair = {};
  var chunks = wbAdsChunk_(ids, WB_ADS_IDS_BATCH_);
  out.advBatchesTotal = chunks.length;
  for (var ch = 0; ch < chunks.length; ch++) {
    if (ch > 0) Utilities.sleep(1200);
    var url = WB_ADS_API_HOST_ + '/api/advert/v2/adverts?ids=' + chunks[ch].join(',');
    var resp = wbAdsHttp_('get', url, token, null);
    if (!resp.ok || !resp.json) continue;   // пачка не доехала — advBatchesOk не растёт
    out.advBatchesOk++;

    var adverts = Array.isArray(resp.json) ? resp.json
      : ((resp.json.adverts) || (resp.json.data && resp.json.data.adverts) || []);

    for (var a = 0; a < adverts.length; a++) {
      var adv = adverts[a] || {};
      var advertId = adv.id || adv.advertId || adv.advertID;
      if (advertId == null) continue;

      var payType = (adv.settings && adv.settings.payment_type) || adv.payment_type || '';
      var bidType = adv.bid_type || '';
      // 🔴 Единственный фильтр scope. Расширять его без нового probe нельзя:
      //    для unified и CPC+manual пустой ответ доказан, это контракт, а не баг.
      if (String(payType).toLowerCase() !== 'cpm') continue;
      if (String(bidType).toLowerCase() !== 'manual') continue;

      var nmSettings = adv.nm_settings || [];
      for (var n = 0; n < nmSettings.length; n++) {
        var nmId = nmSettings[n] && (nmSettings[n].nm_id || nmSettings[n].nmId);
        if (nmId == null) continue;
        var key = advertId + '|' + nmId;
        if (seenPair[key]) continue;
        seenPair[key] = 1;
        out.pairs.push({ advertId: Number(advertId), nmId: Number(nmId) });
        out.ctx[key] = { payment_type: payType, bid_type: bidType, status: adv.status };
      }
    }
  }
  return out;
}


// ═══════════════════════════════════════
// RUN-LOG
// ═══════════════════════════════════════

/**
 * Пишет run-log (ВСЕГДА, в том числе при returned_bid_rows = 0) и строку в WB_ADS_STATUS.
 * Именно run-log отличает валидный пустой ответ от несостоявшегося прогона:
 *   requested>0, success=TRUE, returned=0, status=OK      → валидный пусто: ставок нет
 *   requested=0, status=EMPTY                             → валидный пустой scope:
 *                                                           CPM+manual кампаний нет
 *   requested=0, status=FAILED                            → справочник не доехал
 *                                                           (см. error_message)
 *   success=FALSE при requested>0                         → сбой вызова get-bids
 *   status=PARTIAL                                        → снимок неполон: пачки
 *                                                           get-bids или справочника
 *   строки за сутки отсутствуют                           → прогон не состоялся
 *
 * 🔴 EMPTY и FAILED при requested=0 внешне неразличимы по данным — обе дают ноль
 *    строк. Разделяет их только этот статус, поэтому схлопывать их обратно в один
 *    код нельзя: EMPTY — норма, FAILED — потеря дня, который не восстановить.
 */
function wbAdsQbFinish_(rid, srcLabel, log, t0, rows) {
  log.duration_ms = Date.now() - t0;

  // 🔴 Run-log — это контракт наблюдаемости: без него нельзя отличить валидный
  //    пустой ответ от несостоявшегося прогона. Поэтому его потеря САМА ПО СЕБЕ
  //    делает прогон неуспешным, каким бы удачным ни был вызов API.
  var runLogOk = true;
  try {
    var ss = SpreadsheetApp.getActiveSpreadsheet();
    var runsSheet = wbAdvRawEnsureSheet_(ss, WB_ADV_RAW_QUERY_BIDS_RUNS_SHEET_,
                                         WB_ADV_RAW_QUERY_BIDS_RUNS_HEADERS_);
    wbAdvRawAppendRows_(runsSheet, [log]);
  } catch (e) {
    runLogOk = false;
    console.error('  query_bids: run-log НЕ записан: ' + ((e && e.message) || e));
  }
  if (!runLogOk) {
    log.error_message = (log.error_message ? log.error_message + '; ' : '') +
      'RUN-LOG НЕ ЗАПИСАН (исходный статус ' + log.status + ')';
    log.status = 'FAILED';
  }

  try {
    wbAdsRawWriteStatus_(rid, srcLabel, '', '', {
      http_status: log.http_status,
      status: log.status,
      rows_or_items_found: log.returned_bid_rows,
      error_message: log.error_message
    });
  } catch (e2) { /* статус-лист не критичен */ }

  console.log('  query_bids ' + log.status +
    ' | пар ' + log.requested_pairs +
    ' | пачек ' + log.requested_batches + '/' + log.expected_batches +
    ' | строк ставок ' + log.returned_bid_rows +
    ' | пар со ставками ' + log.distinct_pairs_with_bids +
    ' | ' + log.duration_ms + 'мс' +
    (log.error_message ? ' | ' + log.error_message : ''));

  return { source: srcLabel, status: log.status, rows: rows, pairs: log.requested_pairs };
}
