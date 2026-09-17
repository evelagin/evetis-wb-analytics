/**
 * WbAdsProbeV4.gs — get-bids на CPC+manual парах. READ-ONLY.
 *
 * Вопрос: возвращает ли /adv/v0/normquery/get-bids ставки по кластерам
 *   для кампаний с оплатой ЗА КЛИК (payment_type=cpc, bid_type=manual).
 *   Probe v2 доказал это только для CPM+manual (39 ставок по двум парам).
 *   У нас 23 CPC-manual пары — надо знать, включать их в опрос Ads-3 или нет.
 *
 * ⚠️ ПУСТОЙ РЕЗУЛЬТАТ — НЕ ОШИБКА. Это валидный ответ контракта: если WB не ведёт
 *   кластерные ставки для CPC, строк не будет, и Ads-3 просто не станет их опрашивать.
 *
 * 🔑 ПОЗИТИВНЫЙ КОНТРОЛЬ. Без него пустой ответ неотличим от кривого запроса.
 *   Поэтому в тот же запрос кладём пару CPM+manual (37727969/305101361), про которую
 *   из probe v2 точно известно: она отдаёт 25 ставок. Читать так:
 *     контроль вернул строки, CPC нет  → CPC действительно не поддержан;
 *     не вернулось НИЧЕГО              → проблема в запросе/токене, а не в CPC;
 *     вернулись и те и другие          → CPC поддержан, включаем в Ads-3.
 *
 * НИЧЕГО НЕ ПИШЕТ: ни листы, ни BigQuery, ни Script Properties.
 * Запуск: probeV4_runAll → Run → прислать Execution log.
 * Длительность ~2 секунды (два запроса, лимит get-bids 5 запросов/сек).
 */

// Пары CPC + manual из последнего снимка кампаний (13.08.2026).
// У всех placements: search=true, recommendations=false — это чисто поисковые CPC-кампании.
var PV4_CPC_ = [
  { advert_id: 36047250, nm_id: 438775437, status: 11, bid_rub: 12, note: '274 клика/30д, 3 288 ₽ — самая активная' },
  { advert_id: 39120213, nm_id: 535580776, status: 11, bid_rub: 15, note: '35 кликов/30д, создана 10.08' },
  { advert_id: 36016440, nm_id: 305101272, status: 7,  bid_rub: 15, note: 'статус 7 (завершена)' },
  { advert_id: 35588453, nm_id: 305101361, status: 9,  bid_rub: 35, note: 'статус 9 (активна), активности нет' },
  { advert_id: 35955672, nm_id: 535581675, status: 11, bid_rub: 25, note: 'почти без активности' }
];

// Позитивный контроль: CPM + manual, в probe v2 вернул 25 ставок.
var PV4_CONTROL_ = { advert_id: 37727969, nm_id: 305101361, expect_rows: 25 };


function probeV4_runAll() {
  console.log('════════ PROBE v4 · get-bids на CPC+manual · READ-ONLY ════════');
  var tok = getWbAdsToken_();
  if (!tok) { console.log('🔴 STOP: токен WB не найден'); return; }
  console.log('Токен из ключа: ' + tok.key);
  console.log('⚠️ Пустой ответ по CPC — НЕ ошибка. Смысл даёт позитивный контроль.');

  // ── Запрос 1: только CPC-пары ─────────────────────────────────────────────
  console.log('');
  console.log('──── Запрос 1 · только CPC+manual (' + PV4_CPC_.length + ' пар) ────');
  var itemsCpc = [];
  for (var i = 0; i < PV4_CPC_.length; i++) {
    itemsCpc.push({ advert_id: PV4_CPC_[i].advert_id, nm_id: PV4_CPC_[i].nm_id });
    console.log('  запрашиваем ' + PV4_CPC_[i].advert_id + '/' + PV4_CPC_[i].nm_id +
                ' · статус ' + PV4_CPC_[i].status + ' · ставка ' + PV4_CPC_[i].bid_rub + ' ₽ · ' + PV4_CPC_[i].note);
  }
  var r1 = pv4Call_(tok.token, itemsCpc, 'CPC-only');

  Utilities.sleep(1000); // лимит get-bids 5 запросов/сек — с запасом

  // ── Запрос 2: CPC + позитивный контроль CPM ───────────────────────────────
  console.log('');
  console.log('──── Запрос 2 · те же CPC + позитивный контроль CPM ────');
  console.log('  контроль: ' + PV4_CONTROL_.advert_id + '/' + PV4_CONTROL_.nm_id +
              ' (CPM+manual, в probe v2 вернул ' + PV4_CONTROL_.expect_rows + ' ставок)');
  var itemsMix = itemsCpc.slice();
  itemsMix.push({ advert_id: PV4_CONTROL_.advert_id, nm_id: PV4_CONTROL_.nm_id });
  var r2 = pv4Call_(tok.token, itemsMix, 'CPC + контроль');

  // ── Вердикт ───────────────────────────────────────────────────────────────
  console.log('');
  console.log('════════ ИТОГ ════════');
  var cpcRows = 0, ctlRows = 0;
  for (var k in r2.byPair) {
    if (k === PV4_CONTROL_.advert_id + '/' + PV4_CONTROL_.nm_id) ctlRows = r2.byPair[k];
    else cpcRows += r2.byPair[k];
  }
  console.log('Запрос 1 (только CPC): HTTP ' + r1.code + ' · строк ставок ' + r1.total);
  console.log('Запрос 2 (CPC+контроль): HTTP ' + r2.code + ' · всего строк ' + r2.total +
              ' · из них контроль ' + ctlRows + ' · CPC ' + cpcRows);
  console.log('');
  if (r2.ok && ctlRows > 0 && cpcRows === 0) {
    console.log('ВЫВОД: контроль отдал ставки, CPC — ноль ⇒ для CPC кластерных ставок НЕТ.');
    console.log('       Это валидный контракт. В Ads-3 опрашивать ТОЛЬКО CPM+manual (36 пар).');
  } else if (r2.ok && ctlRows > 0 && cpcRows > 0) {
    console.log('ВЫВОД: CPC тоже отдаёт ставки ⇒ в Ads-3 включать ВСЕ manual-пары (59).');
  } else if (r2.ok && ctlRows === 0) {
    console.log('🔴 ВЫВОД: даже позитивный контроль пуст ⇒ дело НЕ в CPC.');
    console.log('       Проверять запрос/токен/статусы кампаний, вывод про CPC не делать.');
  } else {
    console.log('🔴 ВЫВОД: запрос не прошёл (HTTP ' + r2.code + ') — см. тело ответа выше.');
  }
  console.log('════════ КОНЕЦ ════════');
}


/** Один вызов get-bids + разбор по парам. Только чтение. */
function pv4Call_(token, items, label) {
  var resp = wbAdsHttp_('post', WB_ADS_API_HOST_ + '/adv/v0/normquery/get-bids', token, { items: items });
  var out = { ok: !!resp.ok, code: resp.code, total: 0, byPair: {} };

  console.log('  HTTP ' + resp.code + (resp.ok ? ' OK' : ' ❌') + ' · запрошено пар: ' + items.length);
  if (!resp.ok) { console.log('  тело ответа: ' + String(resp.body).substring(0, 800)); return out; }

  var arr = (resp.json && (resp.json.bids || resp.json.items)) || [];
  out.total = arr.length;
  console.log('  СТРОК СТАВОК ВСЕГО: ' + arr.length);

  // разбивка по парам — какие именно пары вернули ставки
  for (var i = 0; i < items.length; i++) {
    out.byPair[items[i].advert_id + '/' + items[i].nm_id] = 0;
  }
  var minBid = null, maxBid = null;
  for (var j = 0; j < arr.length; j++) {
    var b = arr[j];
    var key = b.advert_id + '/' + b.nm_id;
    out.byPair[key] = (out.byPair[key] || 0) + 1;
    var v = Number(b.bid || 0);
    if (v > 0) { if (minBid === null || v < minBid) minBid = v; if (maxBid === null || v > maxBid) maxBid = v; }
  }
  console.log('  РАЗБИВКА ПО ПАРАМ (' + label + '):');
  for (var k in out.byPair) {
    console.log('    ' + k + ' → ' + out.byPair[k] + ' ставок' + (out.byPair[k] === 0 ? '  (пусто — не ошибка)' : ''));
  }
  if (arr.length) {
    console.log('  диапазон ставок: ' + minBid + ' … ' + maxBid + ' ₽');
    console.log('  пример строки: ' + JSON.stringify(arr[0]));
  }
  console.log('  СЫРОЙ ОТВЕТ: ' + String(resp.body).substring(0, 4000));
  return out;
}
