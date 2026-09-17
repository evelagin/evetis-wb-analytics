/**
 * WbAdsProbeV2.gs — Advertising Probe v2 (P1 + P2). READ-ONLY.
 *
 * Назначение: принести СЫРЫЕ ответы WB API по двум вопросам, которые нельзя
 *   закрыть по уже собранным данным:
 *     P1 — возвращает ли /adv/v0/normquery/stats поля views/ctr/cpm
 *          для НАШИХ CPM-кампаний (для CPC они не возвращаются по контракту WB
 *          с 02.04.2026 — это уже доказано на advertId 36047250);
 *     P2 — какова фактическая структура ответа /adv/v0/normquery/get-bids
 *          (ставка в разрезе advert_id + nm_id + norm_query).
 *
 * ⚠️ НИЧЕГО НЕ ПИШЕТ: ни в листы, ни в BigQuery, ни в Script Properties.
 *    Только GET/POST на чтение + console.log. Побочных эффектов нет.
 *
 * Использует существующие хелперы проекта: getWbAdsToken_(), wbAdsHttp_(),
 *   WB_ADS_API_HOST_, WB_ADS_NORMQUERY_PAUSE_MS_ (WbAdsProbe.gs).
 *
 * Как запускать: выбрать функцию probeV2_runAll и нажать Run,
 *   затем скопировать Execution log целиком и прислать.
 *
 * Пары выбраны из production-данных 13.08.2026: активные CPM-кампании
 *   с ненулевыми кликами за последние 14 дней, два bid_type — unified и manual.
 */

// ── Параметры прогона ────────────────────────────────────────────────────────

/** Окно запроса. У normquery/stats ограничение — не более 31 дня. */
var PV2_FROM_ = '2026-07-14';
var PV2_TO_   = '2026-08-12';

/**
 * Пары CPM-кампания × товар. bid_type указан для понимания результата:
 *   unified — общая ставка на поиск и рекомендации;
 *   manual  — раздельные ставки, у этих кампаний rec = 0 (только поиск).
 */
var PV2_PAIRS_ = [
  { advert_id: 37669244, nm_id: 252442517, bid_type: 'unified', bid_search_rub: 130, note: 'арк, 1103 клика/14д' },
  { advert_id: 37727876, nm_id: 868597351, bid_type: 'unified', bid_search_rub: 140, note: 'арк, 439 кликов/14д'  },
  { advert_id: 37727969, nm_id: 305101361, bid_type: 'manual',  bid_search_rub: 520, note: 'поиск, 309 кликов/14д' },
  { advert_id: 37727999, nm_id: 438775617, bid_type: 'manual',  bid_search_rub: 530, note: 'поиск, 198 кликов/14д' }
];

/** Ограничение длины дампа одного ответа в лог (Apps Script режет очень длинные строки). */
var PV2_LOG_MAX_ = 6000;


// ── Запуск ───────────────────────────────────────────────────────────────────

/** Главная точка входа: P1 и P2 подряд. */
function probeV2_runAll() {
  console.log('════════ ADVERTISING PROBE v2 — READ-ONLY ════════');
  console.log('Окно: ' + PV2_FROM_ + ' … ' + PV2_TO_ + ' · пар: ' + PV2_PAIRS_.length);
  probeV2_P1_CpmNormqueryStats();
  console.log('');
  probeV2_P2_ClusterGetBids();
  console.log('════════ КОНЕЦ ════════');
}


// ── P1: normquery/stats на CPM-кампаниях ─────────────────────────────────────

/**
 * Вопрос: приходят ли views / ctr / cpm для CPM-кампаний.
 * Ответ ищем в ПЕРВОМ объекте кластера каждого ответа — печатаем его целиком,
 * плюс отдельно список ключей, чтобы не гадать по глазам.
 */
function probeV2_P1_CpmNormqueryStats() {
  console.log('──── P1 · POST /adv/v0/normquery/stats (только CPM-кампании) ────');
  var tok = getWbAdsToken_();
  if (!tok) { console.log('🔴 STOP: токен WB не найден в Script Properties'); return; }
  console.log('Токен взят из ключа: ' + tok.key);

  for (var i = 0; i < PV2_PAIRS_.length; i++) {
    var p = PV2_PAIRS_[i];
    if (i > 0) Utilities.sleep(WB_ADS_NORMQUERY_PAUSE_MS_);

    var body = { from: PV2_FROM_, to: PV2_TO_,
                 items: [{ advert_id: p.advert_id, nm_id: p.nm_id }] };

    var resp = wbAdsHttp_('post', WB_ADS_API_HOST_ + '/adv/v0/normquery/stats', tok.token, body);

    console.log('');
    console.log('▶ пара ' + (i + 1) + '/' + PV2_PAIRS_.length +
                ' · advert_id=' + p.advert_id + ' nm_id=' + p.nm_id +
                ' · bid_type=' + p.bid_type + ' · ставка поиск ' + p.bid_search_rub + ' ₽ · ' + p.note);
    console.log('  HTTP ' + resp.code + (resp.ok ? ' OK' : ' ❌'));

    if (!resp.ok) { console.log('  тело ответа: ' + pv2Cut_(resp.body)); continue; }

    // Ключи первого кластера — прямой ответ на вопрос P1.
    var firstCluster = pv2FirstCluster_(resp.json);
    if (firstCluster) {
      var keys = Object.keys(firstCluster).sort();
      console.log('  КЛЮЧИ КЛАСТЕРА: [' + keys.join(', ') + ']');
      console.log('  есть views: ' + (keys.indexOf('views') >= 0) +
                  ' · есть ctr: '  + (keys.indexOf('ctr')   >= 0) +
                  ' · есть cpm: '  + (keys.indexOf('cpm')   >= 0));
      console.log('  первый кластер целиком: ' + JSON.stringify(firstCluster));
    } else {
      console.log('  ⚠️ кластеров в ответе нет (пустой stats)');
    }
    console.log('  СЫРОЙ ОТВЕТ: ' + pv2Cut_(resp.body));
  }
}


// ── P2: normquery/get-bids ───────────────────────────────────────────────────

/**
 * Вопрос: какова фактическая структура ставок по поисковым кластерам.
 * Метод принимает до 100 пар за раз — шлём все одним запросом.
 */
function probeV2_P2_ClusterGetBids() {
  console.log('──── P2 · POST /adv/v0/normquery/get-bids ────');
  var tok = getWbAdsToken_();
  if (!tok) { console.log('🔴 STOP: токен WB не найден в Script Properties'); return; }

  var items = [];
  for (var i = 0; i < PV2_PAIRS_.length; i++) {
    items.push({ advert_id: PV2_PAIRS_[i].advert_id, nm_id: PV2_PAIRS_[i].nm_id });
  }

  var resp = wbAdsHttp_('post', WB_ADS_API_HOST_ + '/adv/v0/normquery/get-bids', tok.token, { items: items });
  console.log('  запрошено пар: ' + items.length);
  console.log('  HTTP ' + resp.code + (resp.ok ? ' OK' : ' ❌'));

  if (!resp.ok) { console.log('  тело ответа: ' + pv2Cut_(resp.body)); return; }

  console.log('  КЛЮЧИ ВЕРХНЕГО УРОВНЯ: [' + Object.keys(resp.json || {}).sort().join(', ') + ']');

  var arr = pv2FirstArray_(resp.json);
  if (arr && arr.length) {
    console.log('  элементов в массиве: ' + arr.length);
    console.log('  КЛЮЧИ ЭЛЕМЕНТА: [' + Object.keys(arr[0]).sort().join(', ') + ']');
    console.log('  первый элемент целиком: ' + JSON.stringify(arr[0]));
  } else {
    console.log('  ⚠️ массив ставок пуст или не найден');
  }
  console.log('  СЫРОЙ ОТВЕТ: ' + pv2Cut_(resp.body));
}


// ── Мелкие помощники (только чтение) ─────────────────────────────────────────

function pv2Cut_(s) {
  s = String(s == null ? '' : s);
  return s.length > PV2_LOG_MAX_ ? s.substring(0, PV2_LOG_MAX_) + ' …[обрезано, всего ' + s.length + ' симв.]' : s;
}

/** Достаёт первый объект кластера, не завися от точной формы обёртки. */
function pv2FirstCluster_(json) {
  if (!json) return null;
  var outer = json.stats || json.items || json.data || null;
  if (outer && outer.length) {
    for (var i = 0; i < outer.length; i++) {
      var inner = outer[i] && (outer[i].stats || outer[i].clusters || outer[i].items);
      if (inner && inner.length) return inner[0];
      if (outer[i] && outer[i].norm_query != null) return outer[i];
    }
  }
  return null;
}

/** Первый непустой массив объектов в ответе — для get-bids. */
function pv2FirstArray_(json) {
  if (!json) return null;
  var candidates = ['bids', 'items', 'stats', 'data'];
  for (var i = 0; i < candidates.length; i++) {
    var a = json[candidates[i]];
    if (a && a.length) {
      var inner = a[0] && (a[0].bids || a[0].clusters);
      if (inner && inner.length) return inner;
      return a;
    }
  }
  return null;
}
