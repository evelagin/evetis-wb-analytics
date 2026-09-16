/**
 * ══════════════════════════════════════════════════════════════
 * EVETIS WB — WbStocksWarehouseShapeProbe.gs
 *
 * Диагностика формы ответа T6 `/api/analytics/v1/stocks-report/wb-warehouses`
 * после того, как WB обезличил склады (13–16.08.2026).
 *
 * ПОВОД: 16.08.2026 в 06:23 МСК суточный снимок упал с
 *   «Строка #1: warehouseId не INT64 ≥0»
 * Валидация fail-closed сработала штатно — битый снимок в RAW не попал, —
 * но остатки перестали собираться. Прежде чем менять production, надо увидеть,
 * ЧТО именно теперь присылает WB.
 *
 * 🔴 ПРОБА ТОЛЬКО ЧИТАЕТ. Не пишет ни в BigQuery, ни в лист, ни в manifest,
 * не трогает sink-флаг и не создаёт триггеров. Запускать безопасно в любой момент.
 *
 * ЗАПУСК: выбрать функцию `probeWbStocksWarehouseShape` → Run → скопировать
 * Execution log целиком. Токен берётся из Script Properties (WB_TOKEN_ANALYTICS),
 * в лог НЕ печатается.
 *
 * Зависимости: только `wbStocksGetToken_`, `wbStocksFetchT6_`, `wbStocksInt_`
 * из WbStocksSnapshot.gs. Разбор склада проба делает СВОЕЙ локальной копией
 * (`probeWh_`), чтобы её можно было запускать и ДО вставки патча, и после.
 * ══════════════════════════════════════════════════════════════
 */

/** Локальная копия разбора склада — проба не зависит от версии основного файла. */
function probeWh_(o) {
  var raw = (o.warehouseId !== undefined) ? o.warehouseId : o.warehouse_id;
  var code = (raw === null || raw === undefined) ? '' : String(raw).trim();
  var id = wbStocksInt_(raw);
  var anon = (id === null || id < 0);
  if (anon) id = null;
  var name = String(o.warehouseName || o.warehouse || '').trim();
  return { id: id, code: code, name: name, key: (code !== '' ? code : name), anonymized: anon };
}

function probeWbStocksWarehouseShape() {
  var tz = (typeof WB_STOCKS_TZ_ !== 'undefined') ? WB_STOCKS_TZ_ : 'Europe/Moscow';
  var today = Utilities.formatDate(new Date(), tz, 'yyyy-MM-dd');
  console.log('=== PROBE warehouse shape · период ' + today + '..' + today + ' ===');

  var tk = wbStocksGetToken_();
  if (!tk) { console.error('❌ Нет токена Analytics в Script Properties.'); return; }

  var t6 = wbStocksFetchT6_(tk.token, today, today);
  if (!t6.ok) { console.error('❌ T6 недоступен: ' + t6.error); return; }

  var data = t6.data;
  if (!Array.isArray(data)) { console.error('❌ T6 вернул не массив: ' + typeof data); return; }
  console.log('строк в ответе: ' + data.length);
  if (!data.length) { console.error('❌ T6 вернул 0 строк.'); return; }

  // 1. Сырые примеры — первые три объекта как есть.
  console.log('--- 1. Первые 3 объекта (сырые) ---');
  for (var i = 0; i < Math.min(3, data.length); i++) console.log('[' + i + '] ' + JSON.stringify(data[i]));

  // 2. Полный набор имён полей во всём ответе (WB мог не только убрать, но и добавить).
  var keys = {};
  for (var k = 0; k < data.length; k++) for (var kk in data[k]) keys[kk] = true;
  console.log('--- 2. Поля ответа: ' + Object.keys(keys).sort().join(', '));

  // 3. Что теперь в warehouseId: тип, сырое значение, парсится ли в INT64.
  var byId = {}, nonNumeric = 0, missing = 0;
  for (var a = 0; a < data.length; a++) {
    var raw = (data[a].warehouseId !== undefined) ? data[a].warehouseId : data[a].warehouse_id;
    var sig = (raw === undefined) ? '<undefined>' : (raw === null ? '<null>' : (typeof raw) + ':"' + String(raw) + '"');
    byId[sig] = (byId[sig] || 0) + 1;
    if (raw === undefined || raw === null || String(raw).trim() === '') missing++;
    if (wbStocksInt_(raw) === null) nonNumeric++;
  }
  console.log('--- 3. warehouseId: нечисловых строк ' + nonNumeric + ' из ' + data.length +
              ', пустых/отсутствующих ' + missing);
  var idSigs = Object.keys(byId).sort();
  for (var s = 0; s < Math.min(idSigs.length, 40); s++) console.log('   ' + idSigs[s] + ' × ' + byId[idSigs[s]]);
  if (idSigs.length > 40) console.log('   … всего различных значений: ' + idSigs.length);

  // 4. Имена складов — остались ли они и какие.
  var byName = {};
  for (var b = 0; b < data.length; b++) {
    var nm = String(data[b].warehouseName || data[b].warehouse || '').trim() || '<пусто>';
    byName[nm] = (byName[nm] || 0) + 1;
  }
  var names = Object.keys(byName).sort();
  console.log('--- 4. warehouseName: различных ' + names.length);
  for (var n = 0; n < names.length; n++) console.log('   ' + names[n] + ' × ' + byName[names[n]]);

  // 5. Регион — сохранился ли (на нём держится анализ локализации).
  var byRegion = {};
  for (var c2 = 0; c2 < data.length; c2++) {
    var rg = String(data[c2].regionName || data[c2].region || '').trim() || '<пусто>';
    byRegion[rg] = (byRegion[rg] || 0) + 1;
  }
  console.log('--- 5. regionName: ' + Object.keys(byRegion).sort().map(function (x) {
    return x + ' × ' + byRegion[x];
  }).join(' | '));

  // 6. Ключ грейна ПОСЛЕ правки: code, иначе name. Если distinct < rows — дедуп сломан.
  var seen = {}, dup = 0, noKey = 0;
  for (var d = 0; d < data.length; d++) {
    var wh = probeWh_(data[d]);
    if (wh.key === '') { noKey++; continue; }
    var nmId = wbStocksInt_(data[d].nmId !== undefined ? data[d].nmId : data[d].nm_id);
    var chrt = wbStocksInt_(data[d].chrtId !== undefined ? data[d].chrtId : data[d].chrt_id);
    var key = nmId + '|' + chrt + '|' + wh.key;
    if (seen[key]) dup++; else seen[key] = true;
  }
  console.log('--- 6. Новый ключ nmId|chrtId|склад: distinct ' + Object.keys(seen).length +
              ' из ' + data.length + ' строк, дублей ' + dup + ', без ключа ' + noKey);
  console.log(dup === 0 && noKey === 0
    ? '   ✅ ключ рабочий — правку можно катить'
    : '   ❌ ключ НЕ рабочий — дедуп на этом наборе не строится, нужен другой грейн');

  // 7. Суммы — сверить с последним валидным снимком 15.08 (4 741 всего / 39 складов).
  var sumAll = 0, sumPhys = 0, aggRows = 0;
  for (var e = 0; e < data.length; e++) {
    var q = wbStocksInt_(data[e].quantity !== undefined ? data[e].quantity : data[e].qty) || 0;
    var w2 = probeWh_(data[e]);
    var isAgg = (w2.id === 0 || w2.code === '0' || w2.name === 'Остальные');
    sumAll += q;
    if (!isAgg) sumPhys += q; else aggRows++;
  }
  console.log('--- 7. Σ quantity всего ' + sumAll + ', физический (без агрегата) ' + sumPhys +
              ', агрегатных строк ' + aggRows);
  console.log('   для сверки: снимок 15.08 дал 241 строку / 39 складов / Σфиз 4741,');
  console.log('   из них живых 2414 и фантомных (сгоревших) 2327.');

  // 8. Списал ли WB сгоревшее? Сверка по трём якорным SKU (числа снимка 15.08).
  var anchors = { 252442517: { live: 617, dead: 351 }, 438775617: { live: 695, dead: 528 },
                  305101272: { live: 39, dead: 129 } };
  console.log('--- 8. Списал ли WB сгоревшее (по якорным SKU) ---');
  for (var g = 0; g < data.length; g++) {
    var an = anchors[String(wbStocksInt_(data[g].nmId))];
    if (!an) continue;
    var qq = wbStocksInt_(data[g].quantity) || 0;
    console.log('   nm ' + data[g].nmId + ': сейчас ' + qq + ' | 15.08 живых ' + an.live +
                ' + сгоревших ' + an.dead + ' = ' + (an.live + an.dead) +
                ' → ' + (Math.abs(qq - an.live) <= 20 ? 'СПИСАНО (≈ живой остаток)'
                                                      : (Math.abs(qq - (an.live + an.dead)) <= 20
                                                         ? 'НЕ списано (≈ всё вместе)' : 'не сходится ни с чем')));
  }
  console.log('=== PROBE завершён. Ничего не записано. ===');
}
