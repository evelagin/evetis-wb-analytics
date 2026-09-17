/**
 * ══════════════════════════════════════════════════════════════
 * EVETIS WB — WbIncomesShapeProbe.gs
 *
 * Проба истории ПОСТАВОК И ПРИЁМКИ на склады WB.
 *
 * ПОВОД. 17.08.2026 выяснилось, что 1 648 единиц лежат на шести складах
 * (Тула, Краснодар, Самара-Новосемейкино, Волгоград, Сарапул, СПБ Шушары)
 * и не отгружаются с конца июля. Владелец назвал даты поставок по памяти:
 *   06.07 Тула · 24.07 Волгоград, Сарапул, Владимир (транзит Обухово)
 *   28.07 Самара-Новосемейкино (транзит Чехов-1), Екатеринбург (Чехов-2)
 * Поставки той же волны на Владимир и Екатеринбург продаются, а на
 * Волгоград, Сарапул и Самару — нет. Чтобы это доказать документально,
 * нужна история приёмки из API, а её мы никогда не грузили.
 *
 * ЧТО ДЕЛАЕТ. Пробует получить историю поставок тремя способами и
 * показывает, какой работает и что именно отдаёт:
 *   1) GET statistics-api.wildberries.ru/api/v1/supplier/incomes
 *   2) GET statistics-api.wildberries.ru/api/v2/supplier/incomes
 *   3) GET supplies-api.wildberries.ru/api/v1/warehouses (список складов FBW)
 *
 * 🔴 ПРОБА ТОЛЬКО ЧИТАЕТ. Ничего не пишет: ни BigQuery, ни лист, ни manifest,
 * ни sink-флаг, ни триггеры. Токен берётся из Script Properties и в лог
 * НЕ печатается.
 *
 * ЗАПУСК: выбрать функцию `probeWbIncomes` → Run → прислать Execution log
 * целиком. Проба самодостаточна: не зависит от других файлов проекта.
 * ══════════════════════════════════════════════════════════════
 */

var PROBE_INC_TOKEN_KEYS_ = ['WB_TOKEN_STATISTICS', 'WB_TOKEN_ANALYTICS'];
var PROBE_INC_DATE_FROM_  = '2024-01-01T00:00:00';   // пробуем максимальную глубину

/** Локальный доступ к токену — проба не зависит от версии других файлов. */
function probeIncToken_() {
  var props = PropertiesService.getScriptProperties();
  for (var i = 0; i < PROBE_INC_TOKEN_KEYS_.length; i++) {
    var k = PROBE_INC_TOKEN_KEYS_[i], v = '';
    try { v = props.getProperty(k) || ''; } catch (e) { v = ''; }
    if (v) return { key: k, token: String(v).trim() };
  }
  return null;
}

function probeIncHttp_(url, token) {
  var r;
  try {
    r = UrlFetchApp.fetch(url, {
      method: 'get',
      headers: { Authorization: token },
      muteHttpExceptions: true,
      followRedirects: true
    });
  } catch (e) {
    return { ok: false, code: 0, error: 'fetch: ' + e };
  }
  var code = r.getResponseCode();
  var body = r.getContentText();
  var json = null;
  try { json = JSON.parse(body); } catch (e) { json = null; }
  return {
    ok: code >= 200 && code < 300,
    code: code,
    json: json,
    head: body ? body.substring(0, 300) : '',
    len: body ? body.length : 0
  };
}

function probeWbIncomes() {
  console.log('=== PROBE incomes · история поставок и приёмки ===');

  var tk = probeIncToken_();
  if (!tk) { console.error('❌ Нет токена в Script Properties (' + PROBE_INC_TOKEN_KEYS_.join(', ') + ')'); return; }
  console.log('токен взят из свойства: ' + tk.key);

  var attempts = [
    { name: 'v1 incomes', url: 'https://statistics-api.wildberries.ru/api/v1/supplier/incomes?dateFrom=' + PROBE_INC_DATE_FROM_ },
    { name: 'v2 incomes', url: 'https://statistics-api.wildberries.ru/api/v2/supplier/incomes?dateFrom=' + PROBE_INC_DATE_FROM_ }
  ];

  var data = null, used = '';
  for (var a = 0; a < attempts.length; a++) {
    var res = probeIncHttp_(attempts[a].url, tk.token);
    console.log('--- ' + attempts[a].name + ': HTTP ' + res.code + ', тело ' + res.len + ' симв.');
    if (!res.ok) { console.log('    ответ: ' + res.head); continue; }
    var arr = Array.isArray(res.json) ? res.json
            : (res.json && Array.isArray(res.json.data) ? res.json.data : null);
    if (!arr) { console.log('    ⚠️ не массив, начало ответа: ' + res.head); continue; }
    console.log('    ✅ строк: ' + arr.length);
    if (arr.length) { data = arr; used = attempts[a].name; break; }
  }

  // Список складов FBW — отдельная категория токена, может не открыться. Это нормально.
  var wres = probeIncHttp_('https://supplies-api.wildberries.ru/api/v1/warehouses', tk.token);
  console.log('--- список складов FBW: HTTP ' + wres.code +
              (wres.ok && Array.isArray(wres.json) ? ', складов ' + wres.json.length : ' (' + wres.head.substring(0, 120) + ')'));
  if (wres.ok && Array.isArray(wres.json)) {
    var names = [];
    for (var q = 0; q < Math.min(wres.json.length, 60); q++) {
      names.push((wres.json[q].ID !== undefined ? wres.json[q].ID : wres.json[q].id) + ':' + (wres.json[q].name || ''));
    }
    console.log('    ' + names.join(' | '));
  }

  if (!data) { console.error('❌ Историю поставок получить не удалось ни одним способом. Смотреть коды выше.'); return; }
  console.log('');
  console.log('=== РАЗБОР (' + used + ') ===');

  // 1. Сырой пример.
  console.log('--- 1. Первая строка целиком ---');
  console.log(JSON.stringify(data[0]));

  // 2. Полный набор полей.
  var keys = {};
  for (var i = 0; i < data.length; i++) for (var k in data[i]) keys[k] = true;
  console.log('--- 2. Поля: ' + Object.keys(keys).sort().join(', '));

  // 3. Глубина истории.
  var minD = '9999', maxD = '0';
  for (var b = 0; b < data.length; b++) {
    var dt = String(data[b].date || '');
    if (dt && dt < minD) minD = dt;
    if (dt && dt > maxD) maxD = dt;
  }
  console.log('--- 3. Глубина истории: ' + minD + ' … ' + maxD + ', строк ' + data.length);

  // 4. 🔑 ГЛАВНОЕ: склады приёмки и суммы по ним.
  var byWh = {};
  for (var c = 0; c < data.length; c++) {
    var w = String(data[c].warehouseName || '<пусто>').trim();
    var q2 = Number(data[c].quantity || 0);
    if (!byWh[w]) byWh[w] = { rows: 0, qty: 0, first: '9999', last: '0' };
    byWh[w].rows++; byWh[w].qty += q2;
    var d2 = String(data[c].date || '');
    if (d2 && d2 < byWh[w].first) byWh[w].first = d2;
    if (d2 && d2 > byWh[w].last) byWh[w].last = d2;
  }
  var whs = Object.keys(byWh).sort(function (x, y) { return byWh[y].qty - byWh[x].qty; });
  console.log('--- 4. Склады приёмки: ' + whs.length);
  for (var e = 0; e < whs.length; e++) {
    console.log('   ' + whs[e] + ' — строк ' + byWh[whs[e]].rows + ', Σ ' + byWh[whs[e]].qty +
                ', с ' + byWh[whs[e]].first.substring(0, 10) + ' по ' + byWh[whs[e]].last.substring(0, 10));
  }

  // 5. Статусы приёмки.
  var byStatus = {};
  for (var f = 0; f < data.length; f++) {
    var st = String(data[f].status || '<пусто>');
    byStatus[st] = (byStatus[st] || 0) + 1;
  }
  console.log('--- 5. Статусы: ' + Object.keys(byStatus).map(function (x) { return x + ' × ' + byStatus[x]; }).join(' | '));

  // 6. Поставки июля 2026 — сверка с тем, что владелец назвал по памяти.
  console.log('--- 6. Поставки июля 2026 (владелец назвал 06.07 Тула · 24.07 Волгоград/Сарапул/Владимир · 28.07 Самара/Екатеринбург) ---');
  var jul = {};
  for (var g = 0; g < data.length; g++) {
    var dd = String(data[g].date || '').substring(0, 10);
    if (dd < '2026-07-01' || dd > '2026-08-17') continue;
    var wn = String(data[g].warehouseName || '<пусто>').trim();
    var key = dd + ' → ' + wn;
    if (!jul[key]) jul[key] = { qty: 0, rows: 0, incomes: {} };
    jul[key].qty += Number(data[g].quantity || 0);
    jul[key].rows++;
    jul[key].incomes[String(data[g].incomeId || data[g].number || '')] = true;
  }
  var jk = Object.keys(jul).sort();
  if (!jk.length) console.log('   ⚠️ строк за июль–август 2026 нет');
  for (var h = 0; h < jk.length; h++) {
    console.log('   ' + jk[h] + ' — Σ ' + jul[jk[h]].qty + ' ед, строк ' + jul[jk[h]].rows +
                ', поставки: ' + Object.keys(jul[jk[h]].incomes).join(',').substring(0, 80));
  }

  // 7. Есть ли транзитные СЦ отдельной строкой (Обухово, Чехов) или только склад назначения.
  console.log('--- 7. Есть ли в именах складов транзитные СЦ ---');
  var transit = whs.filter(function (x) { return /обухово|чехов|сц /i.test(x); });
  console.log(transit.length ? '   найдены: ' + transit.join(' | ')
                             : '   нет — API отдаёт только склад назначения, транзит не виден');

  console.log('=== PROBE завершён. Ничего не записано. ===');
}