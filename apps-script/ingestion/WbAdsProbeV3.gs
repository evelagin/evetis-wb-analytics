/**
 * WbAdsProbeV3.gs — Historical Probe (историчность normquery/stats). READ-ONLY.
 *
 * Вопрос: действительно ли WB отдаёт исторические кластеры за ЗАПРОШЕННЫЙ период,
 *   и до какой глубины / какой длины окно принимает.
 *
 * ⚠️ Недостаточно увидеть HTTP 200. Нужно доказать, что вернулся ИМЕННО апрель.
 *   Поэтому у каждого теста есть контрольная сумма из нашего BigQuery (fullstats
 *   по той же паре и тому же окну) и негативные контроли.
 *
 * ЛОГИКА ДОКАЗАТЕЛЬСТВА:
 *   A. Историчность. Три пары, окно 13.04–13.05. У всех трёх в апреле десятки тысяч
 *      показов, а в свежем окне — единицы. Если API вернёт объём, сопоставимый
 *      с апрельским контролем, — история есть.
 *   B. Оконность. Та же пара на окнах 32 / 60 / 122 дня: принимает ли API окно
 *      длиннее 31 дня. Если 122 дня проходят — вся история берётся ОДНИМ запросом.
 *   C. Негативные контроли — главное. Если API игнорирует период и всегда отдаёт
 *      «последнее», то:
 *        C1 свежее окно (14.07–12.08) вернёт СТОЛЬКО ЖЕ, сколько апрель → период не работает;
 *        C2 окно до создания кампании (январь) вернёт непустой ответ → период не работает.
 *      Ожидаем: C1 ≈ пусто, C2 пусто.
 *
 * ⚠️ Контроль — из fullstats, он покрывает ВСЕ площадки, а normquery — только
 *   поисковые кластеры. Поэтому normquery ДОЛЖЕН быть ≤ контроля. Равенства не ждём
 *   и на равенство не проверяем; смотрим порядок величины и долю.
 *
 * НИЧЕГО НЕ ПИШЕТ: ни листы, ни BigQuery, ни Script Properties. Только чтение + лог.
 * Запуск: выбрать probeV3_runAll → Run → прислать Execution log целиком.
 * Длительность ~1 минута (8 запросов с паузой 6,5 с, лимит WB — 10 запросов/мин).
 */

// ── Контроли из нашего BigQuery (fullstats, все площадки) ────────────────────

var PV3_PRIMARY_ = { advert_id: 35587102, nm_id: 438775437,
                     note: 'status 9 (активна), создана 07.04.2026, search+rec = true' };

var PV3_TESTS_A_ = [
  { advert_id: 35587102, nm_id: 438775437, status: '9 активна',
    from: '2026-04-13', to: '2026-05-13',
    ctl: { views: 100544, clicks: 2171, atbs: 386, orders: 52, spend: 20302.04 } },
  { advert_id: 35135009, nm_id: 868597351, status: '11 пауза',
    from: '2026-04-13', to: '2026-05-13',
    ctl: { views: 40671, clicks: 1327, atbs: 268, orders: 27, spend: 7629.02 } },
  { advert_id: 35368004, nm_id: 305101272, status: '7 завершена',
    from: '2026-04-13', to: '2026-05-13',
    ctl: { views: 35555, clicks: 400, atbs: 89, orders: 11, spend: 8158.01 } }
];

var PV3_TESTS_B_ = [
  { label: 'W2 · 32 дня  (13.04–14.05)',  from: '2026-04-13', to: '2026-05-14',
    ctl: { views: 100558, clicks: 2171, atbs: 386, orders: 52, spend: 20304.97 } },
  { label: 'W3 · 60 дней (13.04–11.06)',  from: '2026-04-13', to: '2026-06-11',
    ctl: { views: 100605, clicks: 2182, atbs: 386, orders: 55, spend: 20314.71 } },
  { label: 'W4 · 122 дня (13.04–12.08) — вся история одним запросом',
    from: '2026-04-13', to: '2026-08-12',
    ctl: { views: 100610, clicks: 2186, atbs: 387, orders: 57, spend: 20315.82 } }
];

var PV3_TESTS_C_ = [
  { label: 'C1 · свежее окно 14.07–12.08 — ОЖИДАЕМ почти пусто',
    from: '2026-07-14', to: '2026-08-12',
    ctl: { views: 3, clicks: 1, atbs: 1, orders: 2, spend: 0.67 } },
  { label: 'C2 · январь, до создания кампании (07.04) — ОЖИДАЕМ ПУСТО',
    from: '2026-01-01', to: '2026-01-31',
    ctl: { views: 0, clicks: 0, atbs: 0, orders: 0, spend: 0 } }
];

var PV3_SAMPLE_ = 3;      // сколько топ-кластеров печатать
var PV3_PAUSE_MS_ = 6500; // лимит WB: 10 запросов/мин


// ── Запуск ───────────────────────────────────────────────────────────────────

function probeV3_runAll() {
  console.log('════════ HISTORICAL PROBE v3 · normquery/stats · READ-ONLY ════════');
  var tok = getWbAdsToken_();
  if (!tok) { console.log('🔴 STOP: токен WB не найден'); return; }
  console.log('Токен из ключа: ' + tok.key);
  console.log('⚠️ Контроль — fullstats (ВСЕ площадки). normquery = только поиск, ждём ≤ контроля.');

  var verdict = { a_ok: 0, a_total: 0, maxWindow: '31 (не проверялось выше)', c1: null, c2: null };

  console.log('');
  console.log('════ A · ИСТОРИЧНОСТЬ: апрель по трём статусам кампаний ════');
  for (var i = 0; i < PV3_TESTS_A_.length; i++) {
    var t = PV3_TESTS_A_[i];
    if (i > 0) Utilities.sleep(PV3_PAUSE_MS_);
    var r = pv3Call_(tok.token, t.advert_id, t.nm_id, t.from, t.to,
                     'A' + (i + 1) + ' · статус ' + t.status, t.ctl);
    verdict.a_total++;
    if (r.ok && r.clusters > 0 && r.agg.views > 1000) verdict.a_ok++;
  }

  console.log('');
  console.log('════ B · ОКОННОСТЬ: принимает ли WB окно длиннее 31 дня ════');
  console.log('пара ' + PV3_PRIMARY_.advert_id + '/' + PV3_PRIMARY_.nm_id);
  for (var j = 0; j < PV3_TESTS_B_.length; j++) {
    Utilities.sleep(PV3_PAUSE_MS_);
    var b = PV3_TESTS_B_[j];
    var rb = pv3Call_(tok.token, PV3_PRIMARY_.advert_id, PV3_PRIMARY_.nm_id, b.from, b.to, b.label, b.ctl);
    if (rb.ok && rb.clusters > 0) verdict.maxWindow = b.label;
  }

  console.log('');
  console.log('════ C · НЕГАТИВНЫЕ КОНТРОЛИ: работает ли фильтр периода вообще ════');
  for (var k = 0; k < PV3_TESTS_C_.length; k++) {
    Utilities.sleep(PV3_PAUSE_MS_);
    var c = PV3_TESTS_C_[k];
    var rc = pv3Call_(tok.token, PV3_PRIMARY_.advert_id, PV3_PRIMARY_.nm_id, c.from, c.to, c.label, c.ctl);
    if (k === 0) verdict.c1 = rc; else verdict.c2 = rc;
  }

  console.log('');
  console.log('════════ ИТОГ ════════');
  console.log('A · историчность: ' + verdict.a_ok + ' из ' + verdict.a_total + ' пар вернули непустой апрель');
  console.log('B · максимальное принятое окно: ' + verdict.maxWindow);
  if (verdict.c1) console.log('C1 · свежее окно: кластеров ' + verdict.c1.clusters + ', показов ' + verdict.c1.agg.views);
  if (verdict.c2) console.log('C2 · до создания кампании: кластеров ' + verdict.c2.clusters + ', показов ' + verdict.c2.agg.views);
  console.log('');
  console.log('ЧИТАТЬ ТАК:');
  console.log('  A ≥ 1 непустой И C2 пусто И C1 ≪ A  →  история есть, фильтр периода работает.');
  console.log('  C1 ≈ A по объёму                    →  🔴 период игнорируется, это НЕ история.');
  console.log('  C2 непусто                          →  🔴 период игнорируется.');
  console.log('  A всё пусто, а C1 непусто           →  WB хранит только недавнее окно.');
  console.log('════════ КОНЕЦ ════════');
}


// ── Один вызов + агрегаты + сверка с контролем ───────────────────────────────

function pv3Call_(token, advertId, nmId, from, to, label, ctl) {
  var body = { from: from, to: to, items: [{ advert_id: advertId, nm_id: nmId }] };
  var resp = wbAdsHttp_('post', WB_ADS_API_HOST_ + '/adv/v0/normquery/stats', token, body);

  console.log('');
  console.log('▶ ' + label + ' · ' + advertId + '/' + nmId + ' · ' + from + ' … ' + to);
  console.log('  HTTP ' + resp.code + (resp.ok ? ' OK' : ' ❌'));

  var out = { ok: !!resp.ok, clusters: 0, agg: { views: 0, clicks: 0, atbs: 0, orders: 0, spend: 0 } };

  if (!resp.ok) {
    console.log('  тело ответа: ' + String(resp.body).substring(0, 800));
    console.log('  → окно этой длины НЕ принято');
    return out;
  }

  var list = pv3Clusters_(resp.json);
  out.clusters = list.length;

  var minPos = null, maxPos = null;
  for (var i = 0; i < list.length; i++) {
    var c = list[i];
    out.agg.views  += Number(c.views  || 0);
    out.agg.clicks += Number(c.clicks || 0);
    out.agg.atbs   += Number(c.atbs   || 0);
    out.agg.orders += Number(c.orders || 0);
    out.agg.spend  += Number(c.spend  || 0);
    var p = Number(c.avg_pos || 0);
    if (p > 0) { if (minPos === null || p < minPos) minPos = p; if (maxPos === null || p > maxPos) maxPos = p; }
  }
  out.agg.spend = Math.round(out.agg.spend * 100) / 100;

  console.log('  КЛАСТЕРОВ: ' + out.clusters);
  console.log('  АГРЕГАТЫ: показы ' + out.agg.views + ' · клики ' + out.agg.clicks +
              ' · корзины ' + out.agg.atbs + ' · заказы ' + out.agg.orders +
              ' · расход ' + out.agg.spend + ' ₽');
  console.log('  средняя позиция: от ' + (minPos === null ? '—' : minPos) + ' до ' + (maxPos === null ? '—' : maxPos));

  if (ctl) {
    console.log('  КОНТРОЛЬ (fullstats, все площадки): показы ' + ctl.views +
                ' · клики ' + ctl.clicks + ' · расход ' + ctl.spend + ' ₽');
    console.log('  ДОЛЯ ОТ КОНТРОЛЯ: показы ' + pv3Pct_(out.agg.views, ctl.views) +
                ' · клики ' + pv3Pct_(out.agg.clicks, ctl.clicks) +
                ' · расход ' + pv3Pct_(out.agg.spend, ctl.spend));
    if (ctl.views > 0 && out.agg.views > ctl.views * 1.05) {
      console.log('  ⚠️ normquery БОЛЬШЕ fullstats — это противоречит «поиск ⊆ все площадки», разобрать');
    }
  }

  list.sort(function (x, y) { return Number(y.views || 0) - Number(x.views || 0); });
  for (var s = 0; s < Math.min(PV3_SAMPLE_, list.length); s++) {
    console.log('  топ-' + (s + 1) + ': ' + JSON.stringify(list[s]));
  }
  if (!list.length) console.log('  (кластеров нет — пустой ответ)');

  return out;
}


// ── Помощники (только чтение) ────────────────────────────────────────────────

/** Плоский список кластеров из вложенной формы {"stats":[{advert_id,nm_id,stats:[...]}]} */
function pv3Clusters_(json) {
  var res = [];
  if (!json) return res;
  var outer = json.stats || json.items || json.data || [];
  for (var i = 0; i < outer.length; i++) {
    var inner = outer[i] && (outer[i].stats || outer[i].clusters || outer[i].items);
    if (inner && inner.length) { for (var j = 0; j < inner.length; j++) res.push(inner[j]); }
    else if (outer[i] && outer[i].norm_query != null) res.push(outer[i]);
  }
  return res;
}

function pv3Pct_(actual, control) {
  if (!control) return (actual ? actual + ' при контроле 0 (!)' : '— (контроль 0)');
  return Math.round(1000 * actual / control) / 10 + '%';
}
