/**
 * ══════════════════════════════════════════════════════════════
 * Ads-2 · WbAdsProbeV5.gs — READ-ONLY probe под дизайн query-level stats.
 *
 * 🔴 НИЧЕГО НЕ ПИШЕТ: ни в листы, ни в BigQuery, ни в Script Properties.
 *    Только POST /adv/v0/normquery/stats и GET-справочник кампаний —
 *    оба чтение (POST лишь потому, что список пар идёт телом запроса).
 *    Ни одного write-вызова WB, ставки не трогаются.
 *
 * ЗАЧЕМ. Дизайн Ads-2 упирается в четыре вопроса, на которые нельзя
 *   ответить из нашего хранилища — только у API:
 *     A. Отдаёт ли эндпоинт данные за ОДНИ сутки (from = to)?
 *        От этого зависит, доступен ли суточный грейн вообще.
 *     B. Аддитивны ли сутки? Сумма семи суточных запросов обязана сойтись
 *        с одним недельным запросом за те же даты. Если нет — суточная
 *        нарезка не эквивалентна периоду, и грейн придётся менять.
 *     C. Сколько пар реально принимает один запрос: 10 / 50 / 100?
 *        Документировано 100, на нашем контуре не проверено.
 *     D. Есть ли потолок числа кластеров на пару (обрезка выдачи)?
 *
 *   Плюс E: полный инвентарь полей ответа — чтобы схема RAW не потеряла
 *   поля, как сейчас потеряны spend / shks / currency.
 *
 * ПОЗИТИВНЫЙ КОНТРОЛЬ. Блок B считает ту же неделю, что уже лежит в
 *   RAW_WB_ADV_SEARCH_CLUSTERS (44 строки, загружены 11.07.2026):
 *     пара 36047250 / 438775437, период 2026-07-04 … 2026-07-10
 *     ожидается: 44 строки, spend 297,00 ₽, clicks 27, atbs 3, orders 2, shks 2
 *   Эти же 297 ₽ — ровно сумма списаний с баланса за те дни (21+60+36+108+72).
 *   Если B воспроизводит эти цифры — эндпоинт стабилен и сверяется с деньгами.
 *   Если НЕ воспроизводит — значит выдача за прошлые периоды меняется, и это
 *   само по себе важнее всех остальных блоков.
 *
 * ЗАПУСК: probeV5_runAll() из редактора Apps Script. ~2 минуты.
 *   Весь результат — в Execution log. Скопировать лог целиком.
 *
 * ПЕРЕИСПОЛЬЗУЕТ без изменений: getWbAdsToken_, wbAdsHttp_, wbAdsChunk_,
 *   wbAdsCollectAdvertNmPairs_, WB_ADS_API_HOST_, WB_ADS_NORMQUERY_PAUSE_MS_.
 * ══════════════════════════════════════════════════════════════
 */

var P5_METHOD_        = '/adv/v0/normquery/stats';
var P5_TZ_            = 'Europe/Moscow';
var P5_BUDGET_MS_     = 300000;   // 5 мин: жёсткий потолок ниже лимита Apps Script
var P5_PAUSE_MS_      = 6500;     // лимит normquery/stats 10 запросов/мин

// Контрольная пара и неделя — те, что уже лежат в RAW (позитивный контроль).
var P5_CTRL_ADVERT_   = 36047250;
var P5_CTRL_NM_       = 438775437;
var P5_CTRL_FROM_     = '2026-07-04';
var P5_CTRL_TO_       = '2026-07-10';

// Ожидаемые значения контроля (из BigQuery, V_ADV_SEARCH_CLUSTERS + V_ADV_COSTS).
var P5_EXPECT_ = { rows: 44, spend: 297, clicks: 27, atbs: 3, orders: 2, shks: 2 };

// Лестница batching. Проверяем ровно то, что собираемся использовать.
var P5_LADDER_        = [10, 50, 100];

// Круглые числа, на которых стоит заподозрить обрезку выдачи API.
var P5_SUSPICIOUS_CAPS_ = [20, 25, 30, 50, 60, 100, 200];


// ═══════════════════════════════════════
// ТОЧКА ВХОДА
// ═══════════════════════════════════════

function probeV5_runAll() {
  var t0 = Date.now();
  var st = { t0: t0, http: 0, fields: {}, packFields: {}, maxKeysPerPair: 0, maxKeysCase: '' };

  console.log('╔══════════════════════════════════════════════════════════════');
  console.log('║ Ads-2 PROBE v5 · read-only · ' + p5Now_());
  console.log('║ метод: POST ' + P5_METHOD_ + ' (чтение; тело — список пар)');
  console.log('║ записи никуда не производится');
  console.log('╚══════════════════════════════════════════════════════════════');

  var tok = getWbAdsToken_();
  if (!tok) { console.error('❌ Нет WB Promotion токена в Script Properties. Probe остановлен.'); return; }
  console.log('Токен найден (ключ ' + tok.key + '), значение не логируется.');

  var daily = null, weekly = null;
  try {
    daily  = p5BlockA_(tok.token, st);
    weekly = p5BlockB_(tok.token, st);
    p5BlockAB_Compare_(daily, weekly);
    p5BlockC_(tok.token, st);
  } catch (e) {
    console.error('❌ Исключение probe: ' + ((e && e.message) || e));
  }

  p5BlockDE_(st);

  console.log('');
  console.log('══ ИТОГО: HTTP-вызовов ' + st.http + ', длительность ' +
    ((Date.now() - t0) / 1000).toFixed(1) + ' c ══');
  console.log('Скопируйте весь лог целиком — я разберу.');
}


// ═══════════════════════════════════════
// A. Сутки: from = to
// ═══════════════════════════════════════

/**
 * Семь суточных запросов по контрольной паре за 04…10.07.2026.
 * Отвечает на два вопроса сразу:
 *   • принимает ли API окно нулевой длины (from = to);
 *   • есть ли у суток данные, или статистика существует только «крупными» окнами.
 * 🔴 Пустой день сам по себе НЕ доказывает, что суточное окно не работает:
 *    в тот день по паре могло не быть показов. Поэтому смотрим ВСЕ семь дней
 *    и сверяем их сумму с недельным агрегатом в блоке AB.
 */
function p5BlockA_(token, st) {
  console.log('');
  console.log('┌─ A. СУТОЧНОЕ ОКНО (from = to) ─────────────────────────────');
  console.log('│ пара ' + P5_CTRL_ADVERT_ + ' / ' + P5_CTRL_NM_ + ', дни ' + P5_CTRL_FROM_ + '…' + P5_CTRL_TO_);

  var days = p5DateRange_(P5_CTRL_FROM_, P5_CTRL_TO_);
  var acc = { rows: 0, spend: 0, clicks: 0, views: 0, atbs: 0, orders: 0, shks: 0, keys: {}, okDays: 0, emptyDays: 0 };

  for (var i = 0; i < days.length; i++) {
    if (p5OutOfBudget_(st)) { console.log('│ ⏹ тайм-бюджет исчерпан на дне ' + days[i]); break; }
    var r = p5Call_(token, days[i], days[i], [{ advert_id: P5_CTRL_ADVERT_, nm_id: P5_CTRL_NM_ }], st);
    if (!r.ok) {
      console.log('│ ' + days[i] + '  HTTP ' + r.code + ' ✗  ' + p5Clip_(r.body, 180));
      continue;
    }
    var f = p5Flatten_(r.json, st);
    acc.okDays++;
    if (!f.rows.length) acc.emptyDays++;
    for (var k = 0; k < f.rows.length; k++) {
      var row = f.rows[k];
      acc.rows++; acc.keys[row.norm_query] = 1;
      acc.spend += p5Num_(row.spend); acc.clicks += p5Num_(row.clicks); acc.views += p5Num_(row.views);
      acc.atbs += p5Num_(row.atbs); acc.orders += p5Num_(row.orders); acc.shks += p5Num_(row.shks);
    }
    console.log('│ ' + days[i] + '  HTTP ' + r.code + '  пакетов ' + f.packs +
      '  строк ' + f.rows.length + '  spend ' + p5R2_(p5SumField_(f.rows, 'spend')) +
      '  clicks ' + p5SumField_(f.rows, 'clicks') + '  ' + r.ms + 'мс');
  }

  console.log('│');
  console.log('│ ИТОГО по суткам: дней с HTTP 200 ' + acc.okDays + ', из них пустых ' + acc.emptyDays);
  console.log('│   строк ' + acc.rows + ' · уникальных norm_query ' + Object.keys(acc.keys).length);
  console.log('│   spend ' + p5R2_(acc.spend) + ' · views ' + acc.views + ' · clicks ' + acc.clicks +
    ' · atbs ' + acc.atbs + ' · orders ' + acc.orders + ' · shks ' + acc.shks);
  console.log('│ ВЕРДИКТ A: ' + (acc.okDays === 0 ? '🔴 суточное окно не отработало ни разу'
    : (acc.rows === 0 ? '🟡 HTTP 200, но данных за сутки нет ни в один из ' + acc.okDays + ' дней — суточный грейн под вопросом'
      : '🟢 суточное окно принимается и отдаёт данные')));
  console.log('└────────────────────────────────────────────────────────────');
  return acc;
}


// ═══════════════════════════════════════
// B. Неделя одним запросом + позитивный контроль
// ═══════════════════════════════════════

/**
 * Один запрос за 04…10.07.2026 по той же паре.
 * Это ПОЗИТИВНЫЙ КОНТРОЛЬ: ровно эти данные уже лежат в RAW с 11.07.2026.
 * Расхождение здесь означает, что выдача за прошлое меняется — и тогда
 * весь backfill надо проектировать иначе (с переснятием, а не «залил и забыл»).
 */
function p5BlockB_(token, st) {
  console.log('');
  console.log('┌─ B. НЕДЕЛЬНОЕ ОКНО + ПОЗИТИВНЫЙ КОНТРОЛЬ ──────────────────');
  if (p5OutOfBudget_(st)) { console.log('│ ⏹ пропущено по тайм-бюджету'); console.log('└───'); return null; }

  var r = p5Call_(token, P5_CTRL_FROM_, P5_CTRL_TO_,
    [{ advert_id: P5_CTRL_ADVERT_, nm_id: P5_CTRL_NM_ }], st);
  if (!r.ok) {
    console.log('│ HTTP ' + r.code + ' ✗ ' + p5Clip_(r.body, 300));
    console.log('└────────────────────────────────────────────────────────────');
    return null;
  }

  var f = p5Flatten_(r.json, st);
  var got = {
    rows: f.rows.length,
    spend: p5SumField_(f.rows, 'spend'), clicks: p5SumField_(f.rows, 'clicks'),
    views: p5SumField_(f.rows, 'views'), atbs: p5SumField_(f.rows, 'atbs'),
    orders: p5SumField_(f.rows, 'orders'), shks: p5SumField_(f.rows, 'shks'),
    keys: {}
  };
  for (var i = 0; i < f.rows.length; i++) got.keys[f.rows[i].norm_query] = 1;

  console.log('│ HTTP ' + r.code + '  пакетов ' + f.packs + '  строк ' + got.rows + '  ' + r.ms + 'мс');
  console.log('│');
  console.log('│ сверка с тем, что уже в BigQuery (загружено 11.07.2026):');
  p5Cmp_('строк',  got.rows,   P5_EXPECT_.rows);
  p5Cmp_('spend',  p5R2_(got.spend), P5_EXPECT_.spend);
  p5Cmp_('clicks', got.clicks, P5_EXPECT_.clicks);
  p5Cmp_('atbs',   got.atbs,   P5_EXPECT_.atbs);
  p5Cmp_('orders', got.orders, P5_EXPECT_.orders);
  p5Cmp_('shks',   got.shks,   P5_EXPECT_.shks);
  console.log('│ (spend 297 ₽ = сумма списаний с баланса за 06–10.07: 21+60+36+108+72)');
  console.log('└────────────────────────────────────────────────────────────');
  return got;
}


/** Аддитивность: сутки против недели. Главный вопрос дизайна. */
function p5BlockAB_Compare_(daily, weekly) {
  console.log('');
  console.log('┌─ AB. АДДИТИВНОСТЬ: 7 суточных запросов против 1 недельного ─');
  if (!daily || !weekly) { console.log('│ недостаточно данных для сравнения'); console.log('└───'); return; }

  var dk = Object.keys(daily.keys).length, wk = Object.keys(weekly.keys).length;
  p5Cmp_('spend',  p5R2_(daily.spend),  p5R2_(weekly.spend));
  p5Cmp_('clicks', daily.clicks, weekly.clicks);
  p5Cmp_('views',  daily.views,  weekly.views);
  p5Cmp_('atbs',   daily.atbs,   weekly.atbs);
  p5Cmp_('orders', daily.orders, weekly.orders);
  p5Cmp_('shks',   daily.shks,   weekly.shks);
  console.log('│ уникальных norm_query: по суткам ' + dk + ' · за неделю ' + wk +
    (dk === wk ? '  ✅' : '  ⚠️ наборы ключей различаются'));
  console.log('│');
  console.log('│ 🔴 ЧИТАТЬ ТАК: счётчики (spend/clicks/views/atbs/orders/shks) обязаны');
  console.log('│    совпасть — это одни и те же события, порезанные иначе. Набор ключей');
  console.log('│    совпадать НЕ обязан: у суток порог показа может отсечь редкий запрос,');
  console.log('│    который в неделе набрал вес. Расхождение счётчиков = суточная нарезка');
  console.log('│    НЕ эквивалентна периоду, и суточный грейн брать нельзя.');
  console.log('└────────────────────────────────────────────────────────────');
}


// ═══════════════════════════════════════
// C. Лестница batching 10 → 50 → 100
// ═══════════════════════════════════════

/**
 * Три запроса за ОДИН закрытый день с 10, 50 и 100 парами.
 * 🔴 Проверяем не только HTTP-код: API может принять 100 пар и молча вернуть
 *    данные только по части из них. Поэтому сравниваем ЗАПРОШЕНО против
 *    ВЕРНУЛОСЬ ПАКЕТОВ, а не просто смотрим на 200.
 */
function p5BlockC_(token, st) {
  console.log('');
  console.log('┌─ C. ЛЕСТНИЦА BATCHING 10 → 50 → 100 ───────────────────────');
  if (p5OutOfBudget_(st)) { console.log('│ ⏹ пропущено по тайм-бюджету'); console.log('└───'); return; }

  var day = p5ClosedDay_(2);   // позавчера по МСК — заведомо закрытые сутки
  console.log('│ день ' + day + ' (закрытые сутки, from = to)');

  var collected = wbAdsCollectAdvertNmPairs_(token, 100);
  var pairs = (collected && collected.pairs) || [];
  console.log('│ справочник кампаний вернул пар: ' + pairs.length +
    (pairs.length < 100 ? '  ⚠️ меньше 100 — лестница проверится только до ' + pairs.length : ''));
  if (!pairs.length) {
    console.log('│ 🔴 пар нет — лестница не проверена (это про справочник, не про stats)');
    console.log('└────────────────────────────────────────────────────────────');
    return;
  }

  for (var i = 0; i < P5_LADDER_.length; i++) {
    var n = P5_LADDER_[i];
    if (n > pairs.length) { console.log('│ ' + n + ' пар: пропущено — столько пар нет'); continue; }
    if (p5OutOfBudget_(st)) { console.log('│ ⏹ тайм-бюджет исчерпан перед ступенью ' + n); break; }

    var items = [];
    for (var p = 0; p < n; p++) items.push({ advert_id: pairs[p].advertId, nm_id: pairs[p].nmId });

    var r = p5Call_(token, day, day, items, st);
    if (!r.ok) {
      console.log('│ ' + n + ' пар: HTTP ' + r.code + ' ✗ ' + r.ms + 'мс  ' + p5Clip_(r.body, 200));
      console.log('│   ⇒ ПОТОЛОК ПАЧКИ НАЙДЕН: ' + n + ' не принимается');
      break;
    }
    var f = p5Flatten_(r.json, st);
    console.log('│ ' + n + ' пар: HTTP 200 ✓  пакетов ' + f.packs + '/' + n +
      '  строк ' + f.rows.length + '  пар со строками ' + f.pairsWithRows +
      '  max ключей на пару ' + f.maxKeys + '  ' + r.ms + 'мс' +
      (f.packs < n ? '  ⚠️ вернулось пакетов меньше, чем запрошено пар' : ''));
  }
  console.log('│');
  console.log('│ ⚠️ «пакетов меньше, чем пар» само по себе НЕ дефект: у пары без показов');
  console.log('│    в этот день данных нет. Дефектом это станет, если доля вернувшихся');
  console.log('│    пакетов падает С РОСТОМ размера пачки — тогда API режет выдачу.');
  console.log('└────────────────────────────────────────────────────────────');
}


// ═══════════════════════════════════════
// D + E. Потолок ключей и инвентарь полей
// ═══════════════════════════════════════

function p5BlockDE_(st) {
  console.log('');
  console.log('┌─ D. ПОТОЛОК ЧИСЛА КЛАСТЕРОВ НА ПАРУ ───────────────────────');
  console.log('│ максимум за весь прогон: ' + st.maxKeysPerPair + (st.maxKeysCase ? '  (' + st.maxKeysCase + ')' : ''));
  var suspicious = false;
  for (var i = 0; i < P5_SUSPICIOUS_CAPS_.length; i++) {
    if (st.maxKeysPerPair === P5_SUSPICIOUS_CAPS_[i]) suspicious = true;
  }
  console.log('│ ' + (suspicious
    ? '⚠️ значение подозрительно круглое — похоже на обрезку выдачи, не на реальный максимум'
    : '🟢 круглого потолка не видно; наблюдаемый максимум похож на реальный'));
  console.log('│ (для сравнения: в снимке ставок 14.08 максимум был 19 ключей на пару)');
  console.log('└────────────────────────────────────────────────────────────');

  console.log('');
  console.log('┌─ E. ИНВЕНТАРЬ ПОЛЕЙ ОТВЕТА ────────────────────────────────');
  var known = { norm_query: 1, views: 1, clicks: 1, ctr: 1, cpc: 1, cpm: 1, avg_pos: 1, atbs: 1, orders: 1 };
  var names = Object.keys(st.fields).sort();
  console.log('│ уровень кластера (' + names.length + ' полей):');
  for (var f = 0; f < names.length; f++) {
    var nm = names[f];
    console.log('│   ' + (known[nm] ? '  ' : '🔴') + ' ' + nm + '  — встретилось в ' + st.fields[nm] + ' строках' +
      (known[nm] ? '' : '  ← В СХЕМЕ RAW ЭТОГО ПОЛЯ НЕТ'));
  }
  var pnames = Object.keys(st.packFields).sort();
  console.log('│ уровень пакета (пара): ' + (pnames.length ? pnames.join(', ') : '—'));
  console.log('│');
  console.log('│ 🔴 Поля, отмеченные красным, API отдаёт, а RAW_WB_ADV_SEARCH_CLUSTERS теряет.');
  console.log('│    Сегодня это spend / shks / currency: они попадают только в raw_json.');
  console.log('└────────────────────────────────────────────────────────────');
}


// ═══════════════════════════════════════
// ВСПОМОГАТЕЛЬНОЕ
// ═══════════════════════════════════════

/** Один вызов normquery/stats с обязательной паузой под rate limit. */
function p5Call_(token, from, to, items, st) {
  if (st.http > 0) Utilities.sleep(P5_PAUSE_MS_);
  st.http++;
  var t = Date.now();
  var resp = wbAdsHttp_('post', WB_ADS_API_HOST_ + P5_METHOD_, token,
    { from: from, to: to, items: items });
  return { ok: resp.ok, code: resp.code, json: resp.json, body: resp.body, ms: Date.now() - t };
}

/**
 * Ответ → плоские строки + побочно копит инвентарь полей и максимум ключей.
 * Defensive по обёрткам: у WB встречаются и snake_case, и camelCase.
 */
function p5Flatten_(json, st) {
  var out = { rows: [], packs: 0, pairsWithRows: 0, maxKeys: 0 };
  var arr = (json && (json.stats || json.data || [])) || [];
  for (var s = 0; s < arr.length; s++) {
    var pack = arr[s] || {};
    out.packs++;
    for (var pk in pack) {
      if (!pack.hasOwnProperty(pk) || pk === 'stats') continue;
      st.packFields[pk] = (st.packFields[pk] || 0) + 1;
    }
    var advertId = (pack.advert_id != null) ? pack.advert_id : pack.advertId;
    var nmId = (pack.nm_id != null) ? pack.nm_id : pack.nmId;
    var inner = pack.stats || pack.items || [];
    if (inner.length) out.pairsWithRows++;
    if (inner.length > out.maxKeys) out.maxKeys = inner.length;
    if (inner.length > st.maxKeysPerPair) {
      st.maxKeysPerPair = inner.length;
      st.maxKeysCase = 'advert ' + advertId + ' / nm ' + nmId;
    }
    for (var c = 0; c < inner.length; c++) {
      var cl = inner[c] || {};
      for (var k in cl) { if (cl.hasOwnProperty(k)) st.fields[k] = (st.fields[k] || 0) + 1; }
      cl.norm_query = (cl.norm_query != null) ? cl.norm_query : (cl.normQuery != null ? cl.normQuery : '');
      cl.avg_pos = (cl.avg_pos != null) ? cl.avg_pos : cl.avgPos;
      out.rows.push(cl);
    }
  }
  return out;
}

function p5SumField_(rows, name) {
  var s = 0;
  for (var i = 0; i < rows.length; i++) s += p5Num_(rows[i][name]);
  return s;
}

function p5Num_(v) { var n = Number(v); return isFinite(n) ? n : 0; }
function p5R2_(v) { return Math.round(p5Num_(v) * 100) / 100; }

function p5Cmp_(label, got, expected) {
  var ok = (Number(got) === Number(expected));
  console.log('│   ' + (ok ? '✅' : '❌') + ' ' + label + ': получено ' + got + ', ожидалось ' + expected +
    (ok ? '' : '   ← РАСХОЖДЕНИЕ'));
}

function p5DateRange_(from, to) {
  var out = [], d = new Date(from + 'T00:00:00Z'), end = new Date(to + 'T00:00:00Z');
  while (d <= end) {
    out.push(Utilities.formatDate(d, 'UTC', 'yyyy-MM-dd'));
    d = new Date(d.getTime() + 86400000);
  }
  return out;
}

function p5ClosedDay_(backDays) {
  var d = new Date(Date.now() - backDays * 86400000);
  return Utilities.formatDate(d, P5_TZ_, 'yyyy-MM-dd');
}

function p5Now_() { return Utilities.formatDate(new Date(), P5_TZ_, 'yyyy-MM-dd HH:mm:ss'); }

function p5OutOfBudget_(st) { return (Date.now() - st.t0) > P5_BUDGET_MS_; }

function p5Clip_(s, n) {
  s = String(s == null ? '' : s);
  return s.length > n ? s.substring(0, n) + '…' : s;
}
