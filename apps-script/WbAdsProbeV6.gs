/**
 * ══════════════════════════════════════════════════════════════
 * Ads-2 · WbAdsProbeV6.gs — READ-ONLY probe, добор двух незакрытых вопросов.
 *
 * 🔴 НИЧЕГО НЕ ПИШЕТ: ни в листы, ни в BigQuery, ни в Script Properties.
 *    Только POST /adv/v0/normquery/stats — чтение. Ни одного write-вызова WB.
 *
 * ЧТО ОСТАЛОСЬ ПОСЛЕ v5 (14.08.2026)
 *   v5 доказал: суточное окно работает, сутки аддитивны, деньги сходятся
 *   (297,00 ₽ = списания с баланса), пачка 100 принимается транспортом.
 *   Но всё это наблюдалось на ОДНОЙ паре и только на CPC. Два вопроса открыты:
 *
 *   1) Лестница 10/50/100 вернула HTTP 200 и ровно по пакету на пару — но
 *      СТРОК НОЛЬ. Ноль строк объясняется двумя несовместимыми причинами:
 *        • пары были выбраны из справочника «первые 100» и просто не те —
 *          у них в тот день не было показов;
 *        • данные за свежие сутки ещё не готовы (лаг на стороне WB).
 *      Различить их можно только запросом по парам, у которых показы в тот
 *      день ТОЧНО были. Это блок F.
 *
 *   2) CPM не наблюдался вообще. Все 207 строк v5 пришли от CPC-кампании и
 *      не содержали views / ctr / cpm — что соответствует контракту WB для
 *      CPC. Но CPM — это 69% нашего расхода, и для него не проверено НИЧЕГО.
 *      Это блоки G и H.
 *
 * ПАРЫ И ОЖИДАЕМЫЕ ЦИФРЫ ЗАШИТЫ ИЗ НАШЕГО ХРАНИЛИЩА (V_ADV_CAMPAIGN_STATS).
 *   Это принципиально: пары берутся не из «первых 100 справочника», а из дней,
 *   про которые мы ТОЧНО знаем, что показы и расход там были. Ноль строк в
 *   таком запросе — уже утверждение об API, а не о нашей выборке.
 *
 * ЗАПУСК: probeV6_runAll(). ~1,5 минуты, 9 HTTP-вызовов.
 * ══════════════════════════════════════════════════════════════
 */

var P6_METHOD_    = '/adv/v0/normquery/stats';
var P6_TZ_        = 'Europe/Moscow';
var P6_PAUSE_MS_  = 6500;
var P6_BUDGET_MS_ = 300000;

// ─── F: свежие сутки, пары с ГАРАНТИРОВАННЫМИ показами ───
// 2026-08-12, девять пар с расходом по fullstats (₽): 613 390 286 212 197 195 175 168 168.
var P6_F_DAY_   = '2026-08-12';
var P6_F_PAIRS_ = '37669244:252442517,37727999:438775617,37727969:305101361,' +
                  '37736048:593111985,37741306:305101272,39120213:535580776,' +
                  '37727911:535581675,37727817:910584041,36047250:438775437';
var P6_F_SPEND_ = 2004;   // сумма расхода этих девяти пар за день, по fullstats

// ─── G: лаг и поведение CPM. Пара 37727999 / 438775617 — cpm + manual ───
var P6_G_PAIR_  = '37727999:438775617';
var P6_G_DAYS_  = ['2026-08-12', '2026-08-10', '2026-08-07', '2026-08-01', '2026-07-25', '2026-07-20'];
// расход этой пары по fullstats за те же дни, ₽ (порядок совпадает с P6_G_DAYS_)
var P6_G_SPEND_ = [390, 317, 365, 419, 304, 312];
var P6_G_VIEWS_ = [615, 507, 586, 714, 552, 582];

// ─── H: дифференциальная лестница НА ДАННЫХ, день 2026-04-24 ───
// H1 — 51 пара, у всех были показы (расход по fullstats 5 205 ₽).
// H2 — те же 51 плюс 47 пар БЕЗ показов = 98 пар, тот же день.
// Если H2 отдаёт меньше строк, чем H1, — API режет выдачу по объёму пачки.
var P6_H_DAY_   = '2026-04-24';
var P6_H_SPEND_ = 5205;
var P6_H_WITH_VIEWS_ =
  '36047250:438775437,35135009:868597351,35135020:773170315,36015930:438775617,' +
  '35732659:535581674,35116990:773170316,35624720:252442517,35307227:567668636,' +
  '35955672:535581675,36016440:305101272,35587102:438775437,35798561:535580776,' +
  '35734934:305101361,36047484:305101361,35955737:930334397,35588435:438775617,' +
  '35700918:535580776,35368004:305101272,33853974:535581674,35017548:438775437,' +
  '34260695:438775617,35955724:593111985,34676311:438775437,34107799:305101272,' +
  '28381842:305101272,33890267:535580776,34981222:252442517,30161954:438775437,' +
  '33520921:438775437,33197973:252442517,32996230:438775437,30784849:438775437,' +
  '29031401:252442517,34690867:305101361,35003011:305101272,32442363:438775437,' +
  '35955608:252442517,33368175:305101272,30688710:252441968,32897728:438775437,' +
  '30689652:567668635,33790057:438775617,31923438:567668635,34574672:567668636,' +
  '34749424:535580776,35170760:535581675,31932376:438775617,30186117:252442517,' +
  '28729818:252442341,28382095:305101361,34896935:305101272';
var P6_H_ZERO_VIEWS_ =
  '28772372:438775617,33929552:438775437,33929552:535581674,35116990:773170315,' +
  '35116990:438775437,35116990:305101272,35116990:438775617,35116990:910584041,' +
  '35116990:868597351,35135009:252442517,35135009:438775437,35135009:567668636,' +
  '35135009:910584041,35135009:305101272,35135009:773170315,35135009:438775617,' +
  '35135009:593111985,35135009:773170316,35135020:773170316,35135020:952068582,' +
  '35135020:910584041,35307227:535581675,35307227:535580776,35307227:305101361,' +
  '35307227:535581674,35307227:773170316,35307227:567668635,35307227:438775437,' +
  '35307227:305101272,35307227:868597351,35307227:593111986,35368004:438775437,' +
  '35587102:773170315,35624720:535580776,35955672:535581674,36015930:952068582,' +
  '36016440:567668636,36016440:305101361,36016440:535580776,36047484:438775437,' +
  '36047484:535581675,36047484:305101272,36047484:567668635,36047484:868597351,' +
  '36047484:535581674,36047484:567668636,36047484:438775617';


// ═══════════════════════════════════════
// ТОЧКА ВХОДА
// ═══════════════════════════════════════

function probeV6_runAll() {
  var st = { t0: Date.now(), http: 0, fields: {} };

  console.log('╔══════════════════════════════════════════════════════════════');
  console.log('║ Ads-2 PROBE v6 · read-only · ' + p6Now_());
  console.log('║ добор после v5: пары с гарантированными показами + CPM + лаг');
  console.log('╚══════════════════════════════════════════════════════════════');

  var tok = getWbAdsToken_();
  if (!tok) { console.error('❌ Нет WB Promotion токена. Probe остановлен.'); return; }
  console.log('Токен найден (ключ ' + tok.key + '), значение не логируется.');

  try {
    p6BlockF_(tok.token, st);
    p6BlockG_(tok.token, st);
    p6BlockH_(tok.token, st);
  } catch (e) {
    console.error('❌ Исключение probe: ' + ((e && e.message) || e));
  }
  p6BlockE_(st);

  console.log('');
  console.log('══ ИТОГО: HTTP-вызовов ' + st.http + ', длительность ' +
    ((Date.now() - st.t0) / 1000).toFixed(1) + ' c ══');
  console.log('Скопируйте весь лог целиком — я разберу.');
}


// ═══════════════════════════════════════
// F. Свежие сутки на парах с гарантированными показами
// ═══════════════════════════════════════

function p6BlockF_(token, st) {
  console.log('');
  console.log('┌─ F. СВЕЖИЕ СУТКИ, ПАРЫ С ГАРАНТИРОВАННЫМИ ПОКАЗАМИ ────────');
  console.log('│ день ' + P6_F_DAY_ + ', 9 пар, расход по fullstats ' + P6_F_SPEND_ + ' ₽');
  console.log('│ (в v5 лестница за этот день дала 0 строк — проверяем, дело в парах или в дне)');

  var pairs = p6Pairs_(P6_F_PAIRS_);
  var r = p6Call_(token, P6_F_DAY_, P6_F_DAY_, pairs, st);
  if (!r.ok) {
    console.log('│ HTTP ' + r.code + ' ✗ ' + p6Clip_(r.body, 250));
    console.log('└───'); return;
  }
  var f = p6Flatten_(r.json, st);
  console.log('│ HTTP 200 ✓  пакетов ' + f.packs + '/' + pairs.length +
    '  строк ' + f.rows.length + '  пар со строками ' + f.pairsWithRows + '  ' + r.ms + 'мс');
  console.log('│ spend по запросам ' + p6R2_(p6Sum_(f.rows, 'spend')) +
    ' из ' + P6_F_SPEND_ + ' ₽ расхода за день' +
    (f.rows.length ? '  (доля поиска, остальное — полки)' : ''));
  p6PerPair_(f, 9);

  console.log('│');
  console.log('│ 🔴 ВЕРДИКТ F:');
  if (f.rows.length > 0) {
    console.log('│    🟢 данные за свежие сутки ЕСТЬ. Ноль в лестнице v5 объясняется');
    console.log('│       выборкой пар («первые 100 из справочника»), а не лагом API.');
    console.log('│       ⇒ список пар на день брать ИЗ НАШИХ ДАННЫХ, а не из справочника.');
  } else {
    console.log('│    🔴 данных за свежие сутки НЕТ даже там, где расход точно был.');
    console.log('│       ⇒ у выдачи есть лаг; его границу меряет блок G. Суточный');
    console.log('│       ежедневный сбор придётся сдвигать на N дней назад.');
  }
  console.log('└────────────────────────────────────────────────────────────');
}


// ═══════════════════════════════════════
// G. Лаг по дням + первое наблюдение CPM
// ═══════════════════════════════════════

/**
 * Одна CPM+manual пара на шести днях с известным расходом.
 * Отвечает сразу на три вопроса:
 *   • с какого дня назад появляются данные (граница лага);
 *   • отдаёт ли WB для CPM поля views / ctr / cpm (для CPC их нет — контракт);
 *   • какую долю расхода дня забирает поиск (query-level spend против fullstats).
 */
function p6BlockG_(token, st) {
  console.log('');
  console.log('┌─ G. ЛАГ ПО ДНЯМ + ПЕРВОЕ НАБЛЮДЕНИЕ CPM ───────────────────');
  console.log('│ пара ' + P6_G_PAIR_ + ' — payment_type=cpm, bid_type=manual, статус 11');
  console.log('│ формат строки: день · строк · spend(query) / spend(fullstats) · views(query) / views(fullstats)');

  var pairs = p6Pairs_(P6_G_PAIR_);
  for (var i = 0; i < P6_G_DAYS_.length; i++) {
    if (p6OutOfBudget_(st)) { console.log('│ ⏹ тайм-бюджет исчерпан на ' + P6_G_DAYS_[i]); break; }
    var d = P6_G_DAYS_[i];
    var r = p6Call_(token, d, d, pairs, st);
    if (!r.ok) { console.log('│ ' + d + '  HTTP ' + r.code + ' ✗ ' + p6Clip_(r.body, 150)); continue; }
    var f = p6Flatten_(r.json, st);
    var qs = p6R2_(p6Sum_(f.rows, 'spend'));
    var qv = p6Sum_(f.rows, 'views');
    console.log('│ ' + d + ' · строк ' + f.rows.length +
      ' · spend ' + qs + ' / ' + P6_G_SPEND_[i] +
      ' · views ' + (qv || '—') + ' / ' + P6_G_VIEWS_[i] +
      ' · clicks ' + p6Sum_(f.rows, 'clicks') + ' · ' + r.ms + 'мс');
  }
  console.log('│');
  console.log('│ 🔴 ЧИТАТЬ ТАК:');
  console.log('│  • строк 0 на свежих днях и >0 на старых = лаг выдачи; граница видна по дню перелома.');
  console.log('│  • views в колонке query пусто, а в fullstats есть = для CPM показы по запросам');
  console.log('│    тоже не отдаются, и контракт «нет views только у CPC» надо расширять.');
  console.log('│  • spend(query) < spend(fullstats) — норма: это доля ПОИСКА, остальное полки.');
  console.log('│    spend(query) > spend(fullstats) — тревога: пересчёт долей и сверка с балансом.');
  console.log('└────────────────────────────────────────────────────────────');
}


// ═══════════════════════════════════════
// H. Дифференциальная лестница на данных
// ═══════════════════════════════════════

/**
 * H1: 51 пара, у ВСЕХ были показы 24.04.2026.
 * H2: те же 51 + 47 пар без показов = 98 пар, тот же день.
 * 🔴 Сравниваются не коды, а СТРОКИ. Если H2 отдал меньше строк, чем H1, значит
 *    API режет выдачу по объёму пачки — и большие пачки тихо теряют данные.
 *    Такую потерю нельзя увидеть по HTTP-коду, только этим сравнением.
 * Побочно: пары взяты из апреля, часть кампаний уже вне активного справочника —
 *    значит блок заодно проверяет доступность истории по «старым» кампаниям.
 */
function p6BlockH_(token, st) {
  console.log('');
  console.log('┌─ H. ЛЕСТНИЦА НА ДАННЫХ (дифференциальная) ─────────────────');
  console.log('│ день ' + P6_H_DAY_ + ', расход 51 пары по fullstats ' + P6_H_SPEND_ + ' ₽');

  var withViews = p6Pairs_(P6_H_WITH_VIEWS_);
  var zeroViews = p6Pairs_(P6_H_ZERO_VIEWS_);
  var all = withViews.concat(zeroViews);

  var r1 = null, f1 = null;
  if (!p6OutOfBudget_(st)) {
    r1 = p6Call_(token, P6_H_DAY_, P6_H_DAY_, withViews, st);
    if (r1.ok) {
      f1 = p6Flatten_(r1.json, st);
      console.log('│ H1 · ' + withViews.length + ' пар (все с показами): HTTP 200 ✓ пакетов ' +
        f1.packs + '/' + withViews.length + ' строк ' + f1.rows.length +
        ' пар со строками ' + f1.pairsWithRows + ' spend ' + p6R2_(p6Sum_(f1.rows, 'spend')) +
        ' max ключей ' + f1.maxKeys + ' · ' + r1.ms + 'мс');
    } else {
      console.log('│ H1 · ' + withViews.length + ' пар: HTTP ' + r1.code + ' ✗ ' + p6Clip_(r1.body, 200));
    }
  }

  var f2 = null;
  if (!p6OutOfBudget_(st)) {
    var r2 = p6Call_(token, P6_H_DAY_, P6_H_DAY_, all, st);
    if (r2.ok) {
      f2 = p6Flatten_(r2.json, st);
      console.log('│ H2 · ' + all.length + ' пар (те же 51 + 47 пустых): HTTP 200 ✓ пакетов ' +
        f2.packs + '/' + all.length + ' строк ' + f2.rows.length +
        ' пар со строками ' + f2.pairsWithRows + ' spend ' + p6R2_(p6Sum_(f2.rows, 'spend')) +
        ' max ключей ' + f2.maxKeys + ' · ' + r2.ms + 'мс');
    } else {
      console.log('│ H2 · ' + all.length + ' пар: HTTP ' + r2.code + ' ✗ ' + p6Clip_(r2.body, 200));
      console.log('│   ⇒ ПОТОЛОК ПАЧКИ: ' + all.length + ' не принимается, а ' + withViews.length + ' принимается');
    }
  }

  console.log('│');
  if (f1 && f2) {
    var same = (f1.rows.length === f2.rows.length);
    console.log('│ 🔴 СРАВНЕНИЕ H1 против H2: строк ' + f1.rows.length + ' против ' + f2.rows.length +
      (same ? '  ✅ добавление пустых пар выдачу не режет'
            : '  ❌ РАСХОЖДЕНИЕ — большая пачка теряет данные'));
    if (!same) console.log('│    ⇒ размер пачки в Ads-2 ограничить величиной, на которой потерь нет');
  } else {
    console.log('│ сравнение не выполнено — один из запросов не отработал');
  }
  if (f1 && f1.rows.length === 0) {
    console.log('│ ⚠️ 0 строк при 51 паре с показами за апрель = история по этим кампаниям');
    console.log('│    недоступна (они уже вне активного справочника). Тогда backfill');
    console.log('│    возможен только по кампаниям, живым на момент запроса, — это меняет');
    console.log('│    глубину истории и должно попасть в контракт данных.');
  }
  console.log('└────────────────────────────────────────────────────────────');
}


// ═══════════════════════════════════════
// E. Инвентарь полей (повтор v5 — теперь с надеждой увидеть CPM)
// ═══════════════════════════════════════

function p6BlockE_(st) {
  console.log('');
  console.log('┌─ E. ИНВЕНТАРЬ ПОЛЕЙ ОТВЕТА (v6) ───────────────────────────');
  var inRawSchema = { norm_query: 1, views: 1, clicks: 1, ctr: 1, cpc: 1, cpm: 1, avg_pos: 1, atbs: 1, orders: 1 };
  var names = Object.keys(st.fields).sort();
  if (!names.length) { console.log('│ строк не было — инвентарь пуст'); console.log('└───'); return; }
  for (var i = 0; i < names.length; i++) {
    var n = names[i];
    console.log('│   ' + (inRawSchema[n] ? '  ' : '🔴') + ' ' + n + ' — в ' + st.fields[n] + ' строках' +
      (inRawSchema[n] ? '' : '  ← В СХЕМЕ RAW НЕТ'));
  }
  var missing = [];
  var expect = ['views', 'ctr', 'cpm'];
  for (var m = 0; m < expect.length; m++) if (!st.fields[expect[m]]) missing.push(expect[m]);
  console.log('│');
  console.log('│ поля, которых в ответе НЕ БЫЛО НИ РАЗУ: ' + (missing.length ? missing.join(', ') : '—'));
  if (missing.length) {
    console.log('│ ⚠️ Если в этом прогоне участвовал CPM (блоки G/H) и данные пришли,');
    console.log('│    то отсутствие этих полей — контракт не только CPC. Колонки в схеме');
    console.log('│    оставить, но не считать их наличие гарантированным.');
  }
  console.log('└────────────────────────────────────────────────────────────');
}


// ═══════════════════════════════════════
// ВСПОМОГАТЕЛЬНОЕ
// ═══════════════════════════════════════

/** 'advert:nm,advert:nm' → [{advert_id, nm_id}] */
function p6Pairs_(spec) {
  var out = [], parts = String(spec).split(',');
  for (var i = 0; i < parts.length; i++) {
    var s = parts[i].split(':');
    if (s.length !== 2) continue;
    out.push({ advert_id: Number(s[0]), nm_id: Number(s[1]) });
  }
  return out;
}

function p6Call_(token, from, to, items, st) {
  if (st.http > 0) Utilities.sleep(P6_PAUSE_MS_);
  st.http++;
  var t = Date.now();
  var resp = wbAdsHttp_('post', WB_ADS_API_HOST_ + P6_METHOD_, token,
    { from: from, to: to, items: items });
  return { ok: resp.ok, code: resp.code, json: resp.json, body: resp.body, ms: Date.now() - t };
}

function p6Flatten_(json, st) {
  var out = { rows: [], packs: 0, pairsWithRows: 0, maxKeys: 0, byPair: [] };
  var arr = (json && (json.stats || json.data || [])) || [];
  for (var s = 0; s < arr.length; s++) {
    var pack = arr[s] || {};
    out.packs++;
    var advertId = (pack.advert_id != null) ? pack.advert_id : pack.advertId;
    var nmId = (pack.nm_id != null) ? pack.nm_id : pack.nmId;
    var inner = pack.stats || pack.items || [];
    if (inner.length) out.pairsWithRows++;
    if (inner.length > out.maxKeys) out.maxKeys = inner.length;
    var pairSpend = 0;
    for (var c = 0; c < inner.length; c++) {
      var cl = inner[c] || {};
      for (var k in cl) { if (cl.hasOwnProperty(k)) st.fields[k] = (st.fields[k] || 0) + 1; }
      pairSpend += p6Num_(cl.spend);
      out.rows.push(cl);
    }
    if (inner.length) out.byPair.push({ pair: advertId + ':' + nmId, keys: inner.length, spend: pairSpend });
  }
  out.byPair.sort(function (a, b) { return b.spend - a.spend; });
  return out;
}

function p6PerPair_(f, limit) {
  for (var i = 0; i < f.byPair.length && i < limit; i++) {
    console.log('│   ' + f.byPair[i].pair + ' · ключей ' + f.byPair[i].keys +
      ' · spend ' + p6R2_(f.byPair[i].spend));
  }
}

function p6Sum_(rows, name) {
  var s = 0;
  for (var i = 0; i < rows.length; i++) s += p6Num_(rows[i][name]);
  return s;
}

function p6Num_(v) { var n = Number(v); return isFinite(n) ? n : 0; }
function p6R2_(v) { return Math.round(p6Num_(v) * 100) / 100; }
function p6Now_() { return Utilities.formatDate(new Date(), P6_TZ_, 'yyyy-MM-dd HH:mm:ss'); }
function p6OutOfBudget_(st) { return (Date.now() - st.t0) > P6_BUDGET_MS_; }
function p6Clip_(s, n) {
  s = String(s == null ? '' : s);
  return s.length > n ? s.substring(0, n) + '…' : s;
}
