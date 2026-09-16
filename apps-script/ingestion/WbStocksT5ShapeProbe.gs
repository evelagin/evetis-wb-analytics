/**
 * ══════════════════════════════════════════════════════════════
 * EVETIS WB — WbStocksT5ShapeProbe.gs
 *
 * Проверка: сохранилась ли детализация по складам в T5
 * `/api/v1/warehouse_remains` (Analytics, task-based).
 *
 * ПОВОД. 16.08.2026 T6 обезличен полностью: 23 строки, warehouseId = -999999,
 * warehouseName и regionName = «Склад WB». НО в тот же день T5-контроль дал
 * сумму 4778 против 2405 у T6 — расхождение 2373, почти ровно равное объёму
 * сгоревшего (2327 ед). Значит T5 живёт по другим правилам: либо ещё не
 * обезличен, либо ещё не списал сгоревшее, либо и то и другое.
 *
 * Если T5 сохранил `warehouses[]` с настоящими именами складов — разрез по
 * складам можно вернуть, сделав канон остатков из T5 вместо T6. Региона у T5
 * нет никогда, но имя склада → округ у нас есть в истории до 15.08.
 *
 * 🔴 ПРОБА ТОЛЬКО ЧИТАЕТ. Ничего не пишет: ни BigQuery, ни лист, ни manifest,
 * ни sink-флаг, ни триггеры.
 *
 * ⚠️ Идёт долго: T5 task-based, опрос статуса раз в 9 секунд (до ~3 минут).
 *
 * ЗАПУСК: выбрать `probeWbStocksT5Shape` → Run → прислать Execution log целиком.
 * Токен берётся из Script Properties, в лог не печатается.
 *
 * Зависимости: `wbStocksGetToken_`, `wbStocksHttp_`, константы хоста и пути
 * из WbStocksSnapshot.gs.
 * ══════════════════════════════════════════════════════════════
 */

function probeWbStocksT5Shape() {
  console.log('=== PROBE T5 warehouse_remains ===');

  var tk = wbStocksGetToken_();
  if (!tk) { console.error('❌ Нет токена Analytics в Script Properties.'); return; }

  var taskBase = WB_STOCKS_ANALYTICS_HOST_ + WB_STOCKS_T5_PATH_;
  var createUrl = taskBase + '?groupByBrand=false&groupBySubject=false&groupBySa=true' +
    '&groupByNm=true&groupByBarcode=true&groupBySize=true';

  var cr = wbStocksHttp_('get', createUrl, tk.token, null);
  if (!cr.ok) { console.error('❌ T5 create: ' + cr.error); return; }
  var taskId = '';
  if (cr.json && cr.json.data && cr.json.data.taskId) taskId = String(cr.json.data.taskId);
  else if (cr.json && cr.json.data && cr.json.data.id) taskId = String(cr.json.data.id);
  else if (cr.json && cr.json.taskId) taskId = String(cr.json.taskId);
  if (!taskId) { console.error('❌ T5: нет taskId в ответе create'); return; }
  console.log('задача создана: ' + taskId);

  var ready = false;
  for (var p = 0; p < 20; p++) {
    Utilities.sleep(9000);
    var sr = wbStocksHttp_('get', taskBase + '/tasks/' + taskId + '/status', tk.token, null);
    var st = '';
    if (sr.json && sr.json.data && sr.json.data.status) st = sr.json.data.status;
    else if (sr.json && sr.json.status) st = sr.json.status;
    st = String(st || '').toLowerCase();
    if (st === 'done' || st === 'ready' || st === 'completed' || st === 'success') { ready = true; break; }
    if (st === 'purged' || st === 'canceled' || st === 'cancelled' || st === 'failed' || st === 'error') {
      console.error('❌ T5 задача ' + st); return;
    }
  }
  if (!ready) { console.error('❌ T5: таймаут ожидания задачи'); return; }

  var dl = wbStocksHttp_('get', taskBase + '/tasks/' + taskId + '/download', tk.token, null);
  if (!dl.ok) { console.error('❌ T5 download: ' + dl.error); return; }
  var data = Array.isArray(dl.json) ? dl.json : [];
  console.log('строк верхнего уровня (товаров): ' + data.length);
  if (!data.length) { console.error('❌ T5 вернул 0 строк.'); return; }

  // 1. Сырой пример — первый товар целиком, со всем вложенным warehouses[].
  console.log('--- 1. Первый объект целиком ---');
  console.log(JSON.stringify(data[0]));

  // 2. Поля верхнего уровня.
  var topKeys = {};
  for (var a = 0; a < data.length; a++) for (var k in data[a]) topKeys[k] = true;
  console.log('--- 2. Поля товара: ' + Object.keys(topKeys).sort().join(', '));

  // 3. 🔑 ГЛАВНОЕ: какие имена складов внутри warehouses[].
  var PSEUDO = { 'Всего находится на складах': 1, 'В пути до получателей': 1, 'В пути возвраты на склад WB': 1 };
  var byWh = {}, whKeys = {}, rowsWh = 0;
  for (var b = 0; b < data.length; b++) {
    var whs = data[b].warehouses || [];
    for (var w = 0; w < whs.length; w++) {
      rowsWh++;
      for (var kk in whs[w]) whKeys[kk] = true;
      var nm = String(whs[w].warehouseName || whs[w].warehouse || '').trim() || '<пусто>';
      var q = Number(whs[w].quantity || 0);
      if (!byWh[nm]) byWh[nm] = { rows: 0, qty: 0 };
      byWh[nm].rows++; byWh[nm].qty += q;
    }
  }
  console.log('--- 3. Поля внутри warehouses[]: ' + Object.keys(whKeys).sort().join(', '));
  var names = Object.keys(byWh).sort();
  console.log('--- 4. Складов в T5: ' + names.length + ' (строк склад×товар: ' + rowsWh + ')');
  var realNames = 0, sumReal = 0, sumPseudo = 0;
  for (var n = 0; n < names.length; n++) {
    var isPseudo = !!PSEUDO[names[n]];
    if (!isPseudo) { realNames++; sumReal += byWh[names[n]].qty; } else { sumPseudo += byWh[names[n]].qty; }
    console.log('   ' + (isPseudo ? '[псевдо] ' : '[склад]  ') + names[n] +
                ' — строк ' + byWh[names[n]].rows + ', Σ ' + byWh[names[n]].qty);
  }

  // 5. Вердикт: вернулась ли детализация.
  console.log('--- 5. ВЕРДИКТ ---');
  console.log('   реальных складов (не псевдо): ' + realNames + ', Σ по ним ' + sumReal);
  if (realNames >= 5) {
    console.log('   ✅ T5 СОХРАНИЛ ДЕТАЛИЗАЦИЮ — разрез по складам можно вернуть, перенеся канон остатков на T5');
  } else if (realNames === 1) {
    console.log('   ❌ T5 тоже обезличен — один склад «' + names.filter(function (x) { return !PSEUDO[x]; })[0] + '»');
  } else {
    console.log('   ⚠️ промежуточный случай — разбирать вручную по списку выше');
  }
  console.log('   для сверки: T6 на 16.08 дал Σ 2405 по 23 товарам;');
  console.log('   T5-контроль в снимке 11:45 дал 4778 (расхождение 2373 ≈ сгоревшие 2327).');
  console.log('   Если Σ по реальным складам ≈ 4778 — T5 ещё НЕ списал сгоревшее.');
  console.log('=== PROBE завершён. Ничего не записано. ===');
}
