/**
 * ══════════════════════════════════════════════════════════════
 * EVETIS WB — WbSuppliesInventoryProbe4.gs   (проба, только чтение)
 *
 * ЗАЧЕМ. Проба №3 нашла источник: `supplies-api` отдаёт 131 поставку за всю
 * историю с 03.09.2024, выборка не усечена, а карточка поставки содержит
 * `warehouseName` и `actualWarehouseName`. Но она же вскрыла разрыв:
 *
 *   поставка 22971287 от 04.09.2024 → quantity 2400, acceptedQuantity 0
 *   поставка 41047776 от 28.07.2026 → quantity  170, acceptedQuantity 169
 *
 * Поле `acceptedQuantity` ведёт себя по-разному на разных концах истории.
 * Пока не известно, с какой даты оно заполняется, материальный баланс за всю
 * историю строить нельзя — можно посчитать недостачу там, где её нет.
 *
 * 🔴 ГЛАВНОЕ ПРАВИЛО ЭТОЙ ПРОБЫ (правка по ревю аудитора)
 *
 * Семантика `acceptedQuantity` и складские агрегаты считаются ТОЛЬКО по
 * фактически состоявшимся поставкам: `statusID === 5` И непустой `factDate`.
 * Из 131 поставки `factDate` есть у 108, а `statusID 5` — у 113. Незавершённая
 * или отменённая поставка законно имеет `quantity > 0` при `acceptedQuantity
 * = 0`, и если пустить её в анализ, она изобразит «поле ещё не работало» там,
 * где поле работало прекрасно. Остальные поставки считаются отдельным
 * контрольным счётчиком и в выводы о семантике не входят.
 *
 * Второе правило: ошибка чтения API НЕ превращается в данные. Но исключается
 * ровно тот агрегат, который испорчен, а не все сразу:
 *
 *   • не прочитался СОСТАВ → количества неизвестны → поставка не входит ни в
 *     количественные агрегаты, ни в классификацию `acceptedQuantity`;
 *   • не прочиталась КАРТОЧКА → неизвестен склад → поставка не входит в
 *     складскую разбивку, но её достоверно прочитанные количества остаются в
 *     общих суммах. Выбрасывать верные цифры из-за несвязанной ошибки нельзя.
 *
 * Обе ситуации помечаются в журнале как `DATA_MISSING` и считаются отдельными
 * счётчиками `goodsErrors` / `cardErrors`. Ведро вида
 * `DATA_MISSING (назнач. DATA_MISSING)` в складской разбивке не создаётся.
 *
 * ЧТО УСТАНАВЛИВАЕТ ПРОБА
 *   1. Как заполнено `acceptedQuantity` по завершённым поставкам — не двумя
 *      датами, а классификацией: ноль · недобор · ровно · перебор.
 *   2. Есть ли вообще чистая временная граница: встречаются ли нули ПОСЛЕ
 *      первой поставки с положительным `acceptedQuantity`.
 *   3. Склад назначения и фактический склад по каждой поставке.
 *      ⚠️ Склад поставки ≠ текущее расположение остатка. Товар мог быть
 *      перемещён после приёмки. Смешивать эти две величины нельзя.
 *   4. Сверку состоявшихся поставок июля 2026 с тем, что владелец помнит.
 *
 * БЕЗОПАСНОСТЬ. Только чтение. Ни одной записи: ни в лист, ни в BigQuery.
 * Токен в журнал не печатается. Лист не трогается сознательно — книга упёрлась
 * в лимит ячеек, вывод идёт только в Execution log.
 *
 * ЗАПУСК — ТРИ ФУНКЦИИ, по отдельности:
 *   probeJuly4()       — сначала это. Состоявшиеся поставки июля, ~30 секунд.
 *   probeInventory4a() — поставки 1…66 из 131, около 2 минут.
 *   probeInventory4b() — поставки 67…131, около 2 минут.
 * ══════════════════════════════════════════════════════════════
 */

var P4_HOST_ = 'https://supplies-api.wildberries.ru';
var P4_KEYS_ = ['WB_TOKEN_SUPPLIES'];

/** Окно сверки с памятью владельца — обе границы заданы явно, без «от и до сегодня». */
var P4_JULY_FROM_ = '2026-07-01';
var P4_JULY_TILL_ = '2026-07-31';

/** Статус состоявшейся поставки. Вынесен в константу, чтобы не растекался по коду. */
var P4_STATUS_DONE_ = 5;

/** Пауза между запросами, мс. Лимиты supplies-api нам неизвестны — идём мягко. */
var P4_PAUSE_MS_ = 350;

/** Страховка от лимита выполнения Apps Script: 4,5 минуты. */
var P4_TIME_BUDGET_MS_ = 270000;

// ── утилиты ───────────────────────────────────────────────────

function p4Log_(s) { Logger.log(s); }

function p4Token_() {
  var sp = PropertiesService.getScriptProperties();
  for (var i = 0; i < P4_KEYS_.length; i++) {
    var v = '';
    try { v = sp.getProperty(P4_KEYS_[i]) || ''; } catch (e) { v = ''; }
    v = String(v).trim();
    if (v) return { token: v, keyName: P4_KEYS_[i] };
  }
  return { token: '', keyName: '' };
}

/** Запрос с одним ретраем на 429 и 5xx. Тихий: печатает только проблемы. */
function p4Fetch_(method, url, token, payload) {
  for (var attempt = 0; attempt < 2; attempt++) {
    var opt = { method: method, muteHttpExceptions: true, headers: { 'Authorization': token } };
    if (payload !== undefined && payload !== null) {
      opt.contentType = 'application/json';
      opt.payload = JSON.stringify(payload);
    }
    var code = -1, body = '';
    try {
      var resp = UrlFetchApp.fetch(url, opt);
      code = resp.getResponseCode();
      body = resp.getContentText();
    } catch (e) {
      p4Log_('    ИСКЛЮЧЕНИЕ ' + method + ' ' + url + ' → ' + e);
      return { code: -1, json: null, body: '' };
    }
    if ((code === 429 || code >= 500) && attempt === 0) {
      p4Log_('    HTTP ' + code + ' на ' + url + ' — ждём 5 с и повторяем один раз');
      Utilities.sleep(5000);
      continue;
    }
    var json = null;
    try { json = JSON.parse(body); } catch (e2) { json = null; }
    if (code !== 200 && code !== 204) {
      p4Log_('    ⚠️ HTTP ' + code + ' ' + method + ' ' + url);
      p4Log_('       ' + body.substring(0, 200).replace(/\s+/g, ' '));
    }
    return { code: code, json: json, body: body };
  }
  return { code: -1, json: null, body: '' };
}

function p4Rows_(json) {
  if (json instanceof Array) return json;
  if (json && json.data instanceof Array) return json.data;
  return null;
}

function p4Day_(s) { return String(s || '').substring(0, 10); }

/** Состоявшаяся поставка: завершённый статус И есть фактическая дата. */
function p4IsCompleted_(s) {
  return s && Number(s.statusID) === P4_STATUS_DONE_ && p4Day_(s.factDate) !== '';
}

/** Полный список поставок одной страницей + признак усечения. */
function p4List_(token) {
  var limit = 1000;
  var r = p4Fetch_('post', P4_HOST_ + '/api/v1/supplies?limit=' + limit + '&offset=0', token, {});
  var rows = p4Rows_(r.json);
  if (!rows) { p4Log_('❌ список поставок не получен, HTTP ' + r.code); return null; }
  p4Log_('поставок получено: ' + rows.length + ' при limit=' + limit);
  if (rows.length >= limit) p4Log_('⚠️ ВЫБОРКА УСЕЧЕНА — нужен проход по offset, выводы ниже неполны');

  var done = 0, withFact = 0, both = 0;
  for (var i = 0; i < rows.length; i++) {
    if (Number(rows[i].statusID) === P4_STATUS_DONE_) done++;
    if (p4Day_(rows[i].factDate)) withFact++;
    if (p4IsCompleted_(rows[i])) both++;
  }
  p4Log_('из них statusID ' + P4_STATUS_DONE_ + ': ' + done + ' · с непустым factDate: ' + withFact +
         ' · состоявшихся (оба условия): ' + both);

  rows.sort(function (a, b) {
    var x = String(a && a.createDate || ''), y = String(b && b.createDate || '');
    return x < y ? -1 : (x > y ? 1 : 0);
  });
  return rows;
}

/** Карточка поставки. null — ошибка чтения, а не «склада нет». */
function p4Card_(token, id) {
  var r = p4Fetch_('get', P4_HOST_ + '/api/v1/supplies/' + id, token);
  return (r.code === 200 && r.json) ? r.json : null;
}

/** Состав поставки. На supplies-api это GET, POST отвечает 405. null — ошибка чтения. */
function p4Goods_(token, id) {
  var limit = 1000;
  var r = p4Fetch_('get', P4_HOST_ + '/api/v1/supplies/' + id + '/goods?limit=' + limit + '&offset=0', token);
  var rows = p4Rows_(r.json);
  if (!rows) return null;
  if (rows.length >= limit) p4Log_('    ⚠️ состав поставки ' + id + ' усечён на ' + limit + ' позициях');
  return rows;
}

function p4Totals_(goods) {
  var t = { positions: goods.length, quantity: 0, accepted: 0, ready: 0, unloading: 0 };
  for (var i = 0; i < goods.length; i++) {
    var g = goods[i] || {};
    if (typeof g.quantity === 'number') t.quantity += g.quantity;
    if (typeof g.acceptedQuantity === 'number') t.accepted += g.acceptedQuantity;
    if (typeof g.readyForSaleQuantity === 'number') t.ready += g.readyForSaleQuantity;
    if (typeof g.unloadingQuantity === 'number') t.unloading += g.unloadingQuantity;
  }
  return t;
}

// ── 1. Состоявшиеся поставки июля — сверка с памятью владельца ─

function probeJuly4() {
  var t0 = new Date().getTime();
  p4Log_('=== PROBE-4 · сверка состоявшихся поставок ' + P4_JULY_FROM_ + '…' + P4_JULY_TILL_ + ' ===');

  var t = p4Token_();
  if (!t.token) { p4Log_('❌ нет токена WB_TOKEN_SUPPLIES'); return; }
  p4Log_('токен из свойства: ' + t.keyName);

  var list = p4List_(t.token);
  if (!list) return;

  // Фильтр строго по factDate и завершённому статусу. Никаких fallback на
  // supplyDate или createDate: это три разных события.
  var picked = [], skipped = [];
  for (var i = 0; i < list.length; i++) {
    var s = list[i];
    var fact = p4Day_(s.factDate);
    if (p4IsCompleted_(s) && fact >= P4_JULY_FROM_ && fact <= P4_JULY_TILL_) { picked.push(s); continue; }
    // всё, что создано или запланировано в июле, но фактом не подтверждено
    var plan = p4Day_(s.supplyDate), made = p4Day_(s.createDate);
    var touchesJuly = (plan >= P4_JULY_FROM_ && plan <= P4_JULY_TILL_) ||
                      (made >= P4_JULY_FROM_ && made <= P4_JULY_TILL_);
    if (touchesJuly && !(p4IsCompleted_(s) && fact >= P4_JULY_FROM_ && fact <= P4_JULY_TILL_)) skipped.push(s);
  }

  p4Log_('состоявшихся поставок в окне: ' + picked.length);
  if (skipped.length) {
    p4Log_('');
    p4Log_('НЕ входят в сверку фактических поставок (' + skipped.length + ') — есть июльская');
    p4Log_('плановая или дата создания, но нет подтверждённого факта:');
    for (var k = 0; k < skipped.length; k++) {
      var sk = skipped[k];
      p4Log_('  ' + sk.supplyID + ' · statusID ' + sk.statusID +
             ' · создана ' + (p4Day_(sk.createDate) || '—') +
             ' · план ' + (p4Day_(sk.supplyDate) || '—') +
             ' · факт ' + (p4Day_(sk.factDate) || 'НЕТ'));
    }
  }

  p4Log_('');
  p4Log_('Владелец помнит шесть: 06.07 Тула ~40 ед · 24.07 Волгоград, Сарапул,');
  p4Log_('Владимир (через Обухово) · 28.07 Самара (Чехов-1), Екатеринбург (Чехов-2).');
  p4Log_('Сверяем с тем, что отдаёт API. Расхождение — это находка, а не ошибка.');
  p4Log_('');

  for (var j = 0; j < picked.length; j++) {
    var p = picked[j];
    var card = p4Card_(t.token, p.supplyID);
    Utilities.sleep(P4_PAUSE_MS_);
    var goods = p4Goods_(t.token, p.supplyID);
    Utilities.sleep(P4_PAUSE_MS_);

    p4Log_('───────────────────────────────────────────────');
    p4Log_('поставка ' + p.supplyID + ' · preorder ' + (p.preorderID || '—'));
    p4Log_('  создана ' + p4Day_(p.createDate) + ' · план ' + p4Day_(p.supplyDate) +
           ' · факт ' + p4Day_(p.factDate) + ' · обновлена ' + p4Day_(p.updatedDate));
    p4Log_('  statusID ' + p.statusID + ' · boxTypeID ' + p.boxTypeID +
           ' · на паллете: ' + (p.isBoxOnPallet ? 'да' : 'нет'));

    if (card) {
      p4Log_('  🔑 склад назначения: ' + (card.warehouseName || '—') +
             ' (id ' + (card.warehouseID || '—') + ')');
      p4Log_('  🔑 фактический склад: ' + (card.actualWarehouseName || '—') +
             ' (id ' + (card.actualWarehouseID || '—') + ')');
      var known = ['phone','statusID','boxTypeID','createDate','supplyDate','factDate','updatedDate',
                   'warehouseID','warehouseName','actualWarehouseID','actualWarehouseName'];
      var extra = [];
      for (var key in card) if (card.hasOwnProperty(key) && known.indexOf(key) < 0) {
        extra.push(key + '=' + JSON.stringify(card[key]));
      }
      if (extra.length) p4Log_('  прочие поля карточки: ' + extra.join(' · '));
    } else {
      p4Log_('  ⚠️ DATA_MISSING: карточка не прочиталась. Склад НЕИЗВЕСТЕН — это ошибка чтения, не «нет склада».');
    }

    if (!goods) {
      p4Log_('  ⚠️ DATA_MISSING: состав не прочитался. Количества НЕИЗВЕСТНЫ — нулями не заменяем.');
      continue;
    }
    var tt = p4Totals_(goods);
    p4Log_('  позиций ' + tt.positions + ' · quantity ' + tt.quantity +
           ' · accepted ' + tt.accepted + ' · readyForSale ' + tt.ready +
           ' · unloading ' + tt.unloading);
    for (var g = 0; g < goods.length; g++) {
      var it = goods[g] || {};
      p4Log_('    ' + (it.vendorCode || '—') + ' · nm ' + (it.nmID || '—') +
             ' · шк ' + (it.barcode || '—') +
             ' → qty ' + it.quantity + ' / accepted ' + it.acceptedQuantity +
             ' / ready ' + it.readyForSaleQuantity + ' / unload ' + it.unloadingQuantity);
    }
  }

  p4Log_('');
  p4Log_('=== ГОТОВО, ' + Math.round((new Date().getTime() - t0) / 1000) + ' с ===');
  p4Log_('Сверяем поимённо: совпал ли список складов с памятью владельца,');
  p4Log_('и сходится ли quantity с тем, что физически уехало со склада.');
}

// ── 2. Инвентаризация всей истории ────────────────────────────

function probeInventory4a() { p4Inventory_(0, 66); }
function probeInventory4b() { p4Inventory_(66, 1000); }

function p4Inventory_(fromIdx, toIdx) {
  var t0 = new Date().getTime();
  p4Log_('=== PROBE-4 · инвентаризация поставок [' + fromIdx + '…' + toIdx + ') ===');

  var t = p4Token_();
  if (!t.token) { p4Log_('❌ нет токена WB_TOKEN_SUPPLIES'); return; }

  var list = p4List_(t.token);
  if (!list) return;
  var last = Math.min(toIdx, list.length);
  p4Log_('обрабатываем позиции ' + fromIdx + '…' + (last - 1) + ' из ' + list.length +
         ' (порядок по createDate, от старых к новым)');
  p4Log_('');
  p4Log_('В агрегаты и в выводы о семантике идут ТОЛЬКО состоявшиеся поставки:');
  p4Log_('statusID ' + P4_STATUS_DONE_ + ' и непустой factDate. Остальные — отдельным счётчиком.');
  p4Log_('');
  p4Log_('# | supplyID | факт | статус | склад назнач → фактический | поз | qty | accepted | ready | пометка');
  p4Log_('─────────────────────────────────────────────────────────────────────────────────────────');

  var sumQ = 0, sumA = 0, sumR = 0, byWh = {}, done = [];
  var nCompleted = 0, nNotCompleted = 0, nNoFact = 0, nOtherStatus = 0;
  var cardErrors = 0, goodsErrors = 0, whUnknown = 0, processed = 0, stoppedAt = -1;

  for (var i = fromIdx; i < last; i++) {
    if ((new Date().getTime() - t0) > P4_TIME_BUDGET_MS_) { stoppedAt = i; break; }

    var s = list[i] || {};
    var completed = p4IsCompleted_(s);
    if (!completed) {
      nNotCompleted++;
      if (!p4Day_(s.factDate)) nNoFact++;
      if (Number(s.statusID) !== P4_STATUS_DONE_) nOtherStatus++;
    }

    var card = p4Card_(t.token, s.supplyID);
    Utilities.sleep(P4_PAUSE_MS_);
    var goods = p4Goods_(t.token, s.supplyID);
    Utilities.sleep(P4_PAUSE_MS_);

    if (!card) cardErrors++;
    if (!goods) goodsErrors++;

    var wh  = card ? (card.warehouseName || '—') : 'DATA_MISSING';
    var awh = card ? (card.actualWarehouseName || '—') : 'DATA_MISSING';
    var tt  = goods ? p4Totals_(goods) : null;

    var note = [];
    if (!completed) note.push('ИСКЛЮЧЕНА: не состоялась');
    if (!goods) note.push('DATA_MISSING состав');
    if (!card) note.push('DATA_MISSING карточка');

    p4Log_(i + ' | ' + s.supplyID + ' | ' + (p4Day_(s.factDate) || 'нет') + ' | ' + s.statusID +
           ' | ' + wh + ' → ' + awh +
           ' | ' + (tt ? tt.positions : '?') + ' | ' + (tt ? tt.quantity : '?') +
           ' | ' + (tt ? tt.accepted : '?') + ' | ' + (tt ? tt.ready : '?') +
           (note.length ? ' | ' + note.join(' · ') : ''));

    processed++;

    // В количественные агрегаты — только состоявшиеся и только с прочитанным
    // составом. Непрочитанный состав = количества неизвестны.
    if (!completed || !tt) continue;
    nCompleted++;
    sumQ += tt.quantity; sumA += tt.accepted; sumR += tt.ready;
    done.push({ id: s.supplyID, day: p4Day_(s.factDate), q: tt.quantity, a: tt.accepted });

    // В складскую разбивку — только если карточка прочиталась. Иначе склад
    // неизвестен, и ведро «DATA_MISSING» было бы выдуманной группой.
    if (!card) { whUnknown++; continue; }
    var whKey = awh + ' (назнач. ' + wh + ')';
    if (!byWh[whKey]) byWh[whKey] = { supplies: 0, quantity: 0, accepted: 0 };
    byWh[whKey].supplies++; byWh[whKey].quantity += tt.quantity; byWh[whKey].accepted += tt.accepted;
  }

  p4Log_('─────────────────────────────────────────────────────────────────────────────────────────');
  p4Log_('обработано записей: ' + processed +
         ' · состоявшихся в агрегатах: ' + nCompleted +
         ' · исключено как не состоявшиеся: ' + nNotCompleted +
         ' (без factDate: ' + nNoFact + ', статус ≠ ' + P4_STATUS_DONE_ + ': ' + nOtherStatus + ')');
  p4Log_('ошибки чтения: карточек ' + cardErrors + ' · составов ' + goodsErrors);
  if (goodsErrors) p4Log_('  ⚠️ непрочитанный состав → количества неизвестны, такие поставки НЕ в агрегатах');
  if (whUnknown)   p4Log_('  ⚠️ состоявшихся с неизвестным складом: ' + whUnknown +
                          ' — их количества в общих суммах есть, в складской разбивке их НЕТ');
  if (stoppedAt >= 0) {
    p4Log_('⚠️ ОСТАНОВЛЕНО ПО ВРЕМЕНИ на позиции ' + stoppedAt +
           '. Это не вся выборка — продолжить со следующего диапазона.');
  }
  p4Log_('ИТОГО по состоявшимся: quantity ' + sumQ + ' · accepted ' + sumA + ' · readyForSale ' + sumR);

  // ── классификация acceptedQuantity ──────────────────────────
  p4Log_('');
  p4Log_('🔑 ПОВЕДЕНИЕ acceptedQuantity по состоявшимся поставкам:');
  var cZero = 0, cUnder = 0, cExact = 0, cOver = 0, cNoQty = 0;
  var firstPositive = '', lastZero = '';
  for (var d = 0; d < done.length; d++) {
    var rec = done[d];
    if (rec.q <= 0) { cNoQty++; continue; }
    if (rec.a === 0) {
      cZero++;
      if (!lastZero || rec.day > lastZero) lastZero = rec.day;
    } else if (rec.a < rec.q) cUnder++;
    else if (rec.a === rec.q) cExact++;
    else cOver++;
    if (rec.a > 0 && (!firstPositive || rec.day < firstPositive)) firstPositive = rec.day;
  }
  p4Log_('  accepted = 0 ................ ' + cZero);
  p4Log_('  0 < accepted < quantity ..... ' + cUnder + '  (недобор при приёмке)');
  p4Log_('  accepted = quantity ......... ' + cExact);
  p4Log_('  accepted > quantity ......... ' + cOver + (cOver ? '  ⚠️ АНОМАЛИЯ' : ''));
  p4Log_('  quantity = 0 (не считаем) ... ' + cNoQty);
  p4Log_('  самая ранняя с accepted > 0: ' + (firstPositive || 'не встретилась'));
  p4Log_('  самая поздняя с accepted = 0: ' + (lastZero || 'не встретилась'));

  // Есть ли вообще чистая временная граница
  var zerosAfter = 0;
  if (firstPositive) {
    for (var z = 0; z < done.length; z++) {
      if (done[z].q > 0 && done[z].a === 0 && done[z].day > firstPositive) zerosAfter++;
    }
  }
  if (!firstPositive) {
    p4Log_('  ВЫВОД: в этом диапазоне положительных accepted нет — граница не наблюдается здесь.');
  } else if (zerosAfter === 0) {
    p4Log_('  ВЫВОД: нулей ПОСЛЕ ' + firstPositive + ' нет → временная граница выглядит чистой.');
  } else {
    p4Log_('  🔴 ВЫВОД: после ' + firstPositive + ' встречается ещё ' + zerosAfter +
           ' состоявшихся поставок с accepted = 0.');
    p4Log_('     Чистой временной границы НЕТ — поле объясняется не только датой.');
  }
  p4Log_('  ⚠️ Это НАБЛЮДЕНИЕ о заполненности поля. Оно не означает, что товар не был принят:');
  p4Log_('     ноль может быть и незаполненным задним числом полем, и настоящим нулём приёмки.');

  // ── склады ──────────────────────────────────────────────────
  p4Log_('');
  p4Log_('по складам — только состоявшиеся и только с прочитанной карточкой');
  p4Log_('(фактический склад, в скобках склад назначения):');
  if (whUnknown) {
    p4Log_('  ⚠️ сумма по этой разбивке МЕНЬШЕ общей на ' + whUnknown +
           ' поставок с неизвестным складом — это не потеря, это неполнота чтения.');
  }
  for (var w in byWh) if (byWh.hasOwnProperty(w)) {
    p4Log_('  ' + w + ' — поставок ' + byWh[w].supplies +
           ' · quantity ' + byWh[w].quantity + ' · accepted ' + byWh[w].accepted);
  }
  p4Log_('  ⚠️ Склад ПОСТАВКИ — это точка приёмки, а не текущее расположение остатка.');
  p4Log_('     После приёмки товар мог быть перемещён. С обезличенным остатком не смешивать.');
  p4Log_('');
  p4Log_('=== ГОТОВО, ' + Math.round((new Date().getTime() - t0) / 1000) + ' с ===');
}
