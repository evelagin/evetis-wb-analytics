/**
 * ══════════════════════════════════════════════════════════════
 *  WbFinanceDailyProbe.gs — READ-ONLY probe ежедневных отчётов
 *  реализации WB (Finance API, period='daily'). v1.1, 2026-07-22.
 *  (v1.1 — 4 правки аудита: суммы по всем строкам; кандидат B/C
 *   только при полном daily-покрытии недели; явная сортировка list;
 *   ANALYSIS_DISABLED при неполном detailed-проходе.)
 *
 *  Цель: доказать контракт ежедневных отчётов ДО production-кода
 *  (D-Fin, двухслойная модель DAILY=PROVISIONAL / WEEKLY=FINAL).
 *
 *  ГАРАНТИИ READ-ONLY:
 *    - только UrlFetchApp + console.log;
 *    - НИКАКИХ записей: Sheets, BigQuery, Script/User Properties,
 *      CacheService, триггеры — не используются вообще
 *      (Properties читаются ТОЛЬКО для токена WB_TOKEN_FINANCE);
 *    - done-set и состояние загрузчиков не трогаются.
 *
 *  ФУНКЦИИ (запускать по одной, между запусками ≥ 1 мин):
 *    A. wbFinProbeA_Lists()          — list daily(14д) + weekly(21д),
 *       BigInt-проверка reportId, покрытие по дням, Σdaily vs weekly
 *       по list-суммам, календарный лаг createDate, константы для B/C.
 *    B. wbFinProbeB_DailyDetailed()  — detailed одного daily-отчёта:
 *       поля, ключ rrdId, суммы, DIGEST для C.
 *    C. wbFinProbeC_WeeklyDetailed() — detailed недельного отчёта:
 *       суммы по дням, сравнение с daily (по вставленным агрегатам),
 *       классификация rrdId: common / daily-only / weekly-only /
 *       common-changed.
 *
 *  ЛИМИТЫ: Finance API = 1 запрос/мин на все методы → пауза 61 с
 *  между запросами; на один запуск ≤ MAX_REQUESTS_PER_RUN запросов
 *  и ≤ MAX_RUNTIME_MS; при неполном проходе печатается continuation
 *  rrdId для повторного запуска.
 *
 *  BigInt: daily reportId и rrdId — int64, могут быть > 2^53.
 *  Стандартный JSON.parse молча искажает такие числа. Поэтому:
 *  сырой body сохраняется; перед parse длинные int-поля берутся
 *  в кавычки (quoted-transform); сверяются raw/safe/native значения
 *  и количества; при расхождении печатается PARSE_MAPPING_ERROR.
 *  В path и в курсор пагинации ID подставляются ТОЛЬКО строкой.
 * ══════════════════════════════════════════════════════════════
 */

// ═══════════════ КОНФИГ (общий) ═══════════════

var WB_FIN_PROBE_HOST_       = 'https://finance-api.wildberries.ru';
var WB_FIN_PROBE_LIST_PATH_  = '/api/finance/v1/sales-reports/list';
var WB_FIN_PROBE_DET_PATH_   = '/api/finance/v1/sales-reports/detailed/'; // + reportId (строкой!)

var WB_FIN_PROBE_SLEEP_MS_            = 61000;  // rate limit 1 req/мин
var WB_FIN_PROBE_MAX_REQUESTS_PER_RUN_ = 4;     // бюджет запросов на запуск
var WB_FIN_PROBE_MAX_RUNTIME_MS_       = 300000; // ~5 мин (лимит Apps Script 6 мин)
var WB_FIN_PROBE_PAGE_LIMIT_           = 100000; // limit detailed
var WB_FIN_PROBE_MONEY_TOL_            = 0.01;   // допуск сравнения денег, ₽

// ── Конфиг A ──
var WB_FIN_PROBE_DAILY_LOOKBACK_DAYS_  = 14;
var WB_FIN_PROBE_WEEKLY_LOOKBACK_DAYS_ = 21;
var WB_FIN_PROBE_LIST_LIMIT_           = 1000;
var WB_FIN_PROBE_LIST_OFFSET_          = 0;     // при rows==limit повторить со смещением

// ── Конфиг B (заполнено из вывода A, прогон 22.07 20:06) ──
var WB_FIN_PROBE_DAILY_ID_       = '409455520260716'; // daily type=1 за 16.07 — СТРОКОЙ
var WB_FIN_PROBE_DAILY_DATE_     = '2026-07-16';       // дата этого отчёта 'YYYY-MM-DD'
var WB_FIN_PROBE_B_START_RRDID_  = '0';  // continuation (строкой), по умолчанию '0'

// ── Конфиг C (WEEKLY_ID заполнен из A; SUMS/DIGEST — из вывода B) ──
var WB_FIN_PROBE_WEEKLY_ID_      = '785600830';        // weekly type=1 за 13–19.07 — СТРОКОЙ
var WB_FIN_PROBE_C_START_RRDID_  = '0';  // continuation (строкой)
// Заполнено из лога B (прогон 22.07 20:56, 95 строк, complete=true):
var WB_FIN_PROBE_DAILY_SUMS_JSON_ = '{"forPay":9942.68,"retailAmount":10015,"retailPriceWithDisc":17136,"deliveryService":1360.57,"paidStorage":700.48,"deduction":0,"penalty":0,"acquiringFee":375.09,"ppvzSalesCommission":-248.16,"additionalPayment":0,"rebillLogisticCost":156.79,"qtySale":18,"rows":95}';
var WB_FIN_PROBE_DAILY_DIGEST_    = '3130286273489:0;3130286273490:0;3130286273491:0;3130286273492:1472.66;3130286273493:0;3130286273494:541.98;3130286273495:0;3130286273496:464.84;3130286273497:0;3130286273498:0;3130286273499:0;3130286273500:607.49;3130286273501:0;3130286273502:536.38;3130286273503:0;3130286273504:609.33;3130286273505:0;3130286273506:477.69;3130286273507:0;3130286273508:542.07;3130286273509:0;3130286273510:0;3130286273511:0;3130286273512:0;3130286273513:0;3130286273514:0;3130286273515:0;3130286273516:552.3;3130286273517:0;3130286273518:540.54;3130286273519:0;3130286273520:564.68;3130286273521:0;3130286273522:488.35;3130286273523:0;3130286273524:543.19;3130286273525:0;3130286273526:333.26;3130286273527:0;3130286273528:522.53;3130286273529:0;3130286273530:455.43;3130286273531:0;3130286273532:330.15;3130286273533:0;3130286273534:359.81;3130286273535:0;3130286273536:0;3130286273537:0;3130286273538:0;3130286273539:0;3130286273540:0;3130286273541:0;3130286273542:0;3130286273543:0;3130286273544:0;3130286273545:0;3130286273546:0;3130286273547:0;3130286273548:0;3130286273549:0;3130286273550:0;3130286273551:0;3130286273552:0;3130286273553:0;3130286273554:0;3130286273555:0;3130286273556:0;3130286273557:0;3130286273558:0;3130286273559:0;3130286273560:0;3130286273561:0;3130286273562:0;3130286273563:0;3130286273564:0;3130286273565:0;3130286273566:0;3130286273567:0;3130286273568:0;3130286273569:0;3130286273570:0;3130286273571:0;3130286273572:0;3130286273573:0;3130286273574:0;3130286273575:0;3130286273576:0;3130286273577:0;3130286273578:0;3130286273579:0;3130286273580:0;3130286273581:0;3130286273582:0;3130286273583:0';

// Денежные поля detailed-строки (суммируются те, что реально присутствуют)
var WB_FIN_PROBE_MONEY_FIELDS_ = [
  'forPay', 'retailAmount', 'retailPriceWithDisc', 'deliveryService',
  'paidStorage', 'deduction', 'penalty', 'acquiringFee',
  'ppvzSalesCommission', 'additionalPayment', 'rebillLogisticCost'
];

// Ключевые int64-поля, которые берём в кавычки до JSON.parse
var WB_FIN_PROBE_BIGINT_FIELDS_ = ['reportId', 'rrdId', 'giId', 'shkId'];


// ═══════════════ ХЕЛПЕРЫ ═══════════════

/** Токен «Финансы» (Properties — ТОЛЬКО чтение). */
function wbFinProbeToken_() {
  if (typeof getFinanceV1Token_ === 'function') {
    var tk = getFinanceV1Token_();
    return tk ? tk.token : null;
  }
  return PropertiesService.getScriptProperties().getProperty('WB_TOKEN_FINANCE');
}

/** POST с готовой строкой payload (payload НЕ проходит через JSON.stringify). */
function wbFinProbePost_(token, path, payloadStr) {
  var resp = UrlFetchApp.fetch(WB_FIN_PROBE_HOST_ + path, {
    method: 'post',
    contentType: 'application/json',
    headers: { 'Authorization': token },
    payload: payloadStr,
    muteHttpExceptions: true
  });
  return { code: resp.getResponseCode(), body: resp.getContentText() };
}

/** Quoted-transform: int64-поля в кавычки, чтобы JSON.parse не терял точность. */
function wbFinProbeQuoteBigInts_(body) {
  var re = new RegExp('"(' + WB_FIN_PROBE_BIGINT_FIELDS_.join('|') + ')"\\s*:\\s*(-?\\d+)', 'g');
  return body.replace(re, '"$1":"$2"');
}

/**
 * Безопасный разбор массива отчётов/строк.
 * Возвращает {arr, rawIds, nativeIds, mappingError, note}:
 *   rawIds    — значения поля idField, извлечённые регексом из сырого body (строки);
 *   arr       — JSON.parse(quoted body): idField уже строкой, без потери точности;
 *   nativeIds — String(Number) из обычного JSON.parse (для проверки потери).
 */
function wbFinProbeParseSafe_(body, idField) {
  var out = { arr: null, rawIds: [], nativeIds: [], mappingError: false, note: '' };

  var re = new RegExp('"' + idField + '"\\s*:\\s*(-?\\d+)', 'g');
  var m;
  while ((m = re.exec(body)) !== null) out.rawIds.push(m[1]);

  try {
    var safe = JSON.parse(wbFinProbeQuoteBigInts_(body));
    out.arr = Array.isArray(safe) ? safe : (safe && safe.data ? safe.data : null);
  } catch (e) {
    out.mappingError = true;
    out.note = 'safe JSON.parse failed: ' + e;
    return out;
  }

  try {
    var nat = JSON.parse(body);
    var natArr = Array.isArray(nat) ? nat : (nat && nat.data ? nat.data : []);
    for (var i = 0; i < natArr.length; i++) out.nativeIds.push(String(natArr[i][idField]));
  } catch (e2) {
    out.note = 'native JSON.parse failed: ' + e2;
  }

  if (!out.arr) { out.mappingError = true; out.note += ' | массив не распознан'; return out; }
  if (out.rawIds.length !== out.arr.length) {
    out.mappingError = true;
    out.note += ' | PARSE_MAPPING_ERROR: regex ' + out.rawIds.length + ' ID vs ' + out.arr.length + ' объектов';
  }
  for (var j = 0; j < out.arr.length; j++) {
    if (String(out.arr[j][idField]) !== out.rawIds[j]) {
      out.mappingError = true;
      out.note += ' | PARSE_MAPPING_ERROR: raw≠safe на позиции ' + j;
      break;
    }
  }
  return out;
}

/** Денежная строка/число → Number. */
function wbFinProbeNum_(v) {
  if (v === null || v === undefined || v === '') return 0;
  if (typeof v === 'number') return v;
  var n = Number(String(v).replace(/\s/g, '').replace(',', '.'));
  return isNaN(n) ? 0 : n;
}

function wbFinProbeR2_(n) { return Math.round(n * 100) / 100; }

function wbFinProbeDateStr_(offsetDays) {
  return Utilities.formatDate(new Date(Date.now() + offsetDays * 86400000), 'Europe/Moscow', 'yyyy-MM-dd');
}

/** Дней между YYYY-MM-DD датами (b − a). */
function wbFinProbeDaysDiff_(a, b) {
  var d = (new Date(String(b).substring(0, 10)) - new Date(String(a).substring(0, 10))) / 86400000;
  return Math.round(d);
}

/**
 * Пагинация detailed по rrdId с бюджетами.
 * Возвращает {ok, rows, complete, cursor, requests, error, mappingError}.
 * cursor — rrdId последней принятой строки (строкой) для continuation.
 */
function wbFinProbeFetchDetailed_(token, reportIdStr, startRrdIdStr, label) {
  var out = { ok: true, rows: [], complete: false, cursor: String(startRrdIdStr || '0'), requests: 0, error: '', mappingError: false };
  var start = Date.now();

  while (true) {
    if (out.requests >= WB_FIN_PROBE_MAX_REQUESTS_PER_RUN_) {
      console.log('⏸️ [' + label + '] Бюджет запросов исчерпан (' + out.requests + ').');
      break;
    }
    if (out.requests > 0) {
      if (Date.now() - start + WB_FIN_PROBE_SLEEP_MS_ + 30000 > WB_FIN_PROBE_MAX_RUNTIME_MS_) {
        console.log('⏸️ [' + label + '] Бюджет времени исчерпан.');
        break;
      }
      Utilities.sleep(WB_FIN_PROBE_SLEEP_MS_);
    }

    // rrdId подставляется в payload ВЕРБАТИМ строкой цифр (без Number!)
    var payload = '{"rrdId":' + out.cursor + ',"limit":' + WB_FIN_PROBE_PAGE_LIMIT_ + '}';
    console.log('POST ' + WB_FIN_PROBE_DET_PATH_ + reportIdStr + '  body=' + payload);
    var r = wbFinProbePost_(token, WB_FIN_PROBE_DET_PATH_ + encodeURIComponent(reportIdStr), payload);
    out.requests++;
    console.log('HTTP ' + r.code + ' | ' + (r.body ? r.body.length : 0) + ' байт');

    if (r.code === 204) { out.complete = true; break; }
    if (r.code !== 200) {
      out.ok = false;
      out.error = 'HTTP ' + r.code + ': ' + String(r.body).substring(0, 300);
      break;
    }

    var p = wbFinProbeParseSafe_(r.body, 'rrdId');
    if (p.mappingError) { out.mappingError = true; console.log('🔴 PARSE_MAPPING_ERROR [' + label + ']: ' + p.note); }
    var arr = p.arr || [];
    if (!arr.length) { out.complete = true; break; }

    for (var i = 0; i < arr.length; i++) out.rows.push(arr[i]);
    out.cursor = String(arr[arr.length - 1].rrdId);

    if (arr.length < WB_FIN_PROBE_PAGE_LIMIT_) { out.complete = true; break; }
  }
  return out;
}

/**
 * Суммы по денежным полям + количество по «Продажа».
 * Наличие поля проверяется ПО ВСЕМ строкам (не только rows[0]): поле
 * включается в результат, если встретилось хотя бы в одной строке.
 */
function wbFinProbeSumRows_(rows) {
  var sums = {};
  var qtySale = 0;
  for (var i = 0; i < rows.length; i++) {
    for (var f = 0; f < WB_FIN_PROBE_MONEY_FIELDS_.length; f++) {
      var name = WB_FIN_PROBE_MONEY_FIELDS_[f];
      if (rows[i][name] !== undefined) {
        sums[name] = (sums[name] || 0) + wbFinProbeNum_(rows[i][name]);
      }
    }
    if (String(rows[i].docTypeName || '') === 'Продажа') qtySale += Number(rows[i].quantity) || 0;
  }
  for (var k in sums) sums[k] = wbFinProbeR2_(sums[k]);
  sums.qtySale = qtySale;
  sums.rows = rows.length;
  return sums;
}

/** Распределение значений поля по строкам: {значение: count}. */
function wbFinProbeCountBy_(rows, field) {
  var acc = {};
  for (var i = 0; i < rows.length; i++) {
    var v = String(rows[i][field] === undefined ? '(нет поля)' : rows[i][field]);
    acc[v] = (acc[v] || 0) + 1;
  }
  return acc;
}

function wbFinProbeFmtCounts_(acc) {
  var parts = [];
  for (var k in acc) parts.push(k + '×' + acc[k]);
  return parts.length ? parts.join(', ') : '—';
}


// ═══════════════════════════════════════════════════
//  A. СПИСКИ: daily(14д) + weekly(21д)  [2 запроса]
// ═══════════════════════════════════════════════════

function wbFinProbeA_Lists() {
  var token = wbFinProbeToken_();
  if (!token) { console.log('❌ Нет токена WB_TOKEN_FINANCE'); return; }

  var today = wbFinProbeDateStr_(0);
  var dailyFrom = wbFinProbeDateStr_(-WB_FIN_PROBE_DAILY_LOOKBACK_DAYS_);
  var weeklyFrom = wbFinProbeDateStr_(-WB_FIN_PROBE_WEEKLY_LOOKBACK_DAYS_);

  // ── A1: list period=daily ──
  var body1 = JSON.stringify({ dateFrom: dailyFrom, dateTo: today, period: 'daily', limit: WB_FIN_PROBE_LIST_LIMIT_, offset: WB_FIN_PROBE_LIST_OFFSET_ });
  console.log('═══ A1: LIST daily ═══');
  console.log('POST ' + WB_FIN_PROBE_LIST_PATH_ + '  body=' + body1);
  var r1 = wbFinProbePost_(token, WB_FIN_PROBE_LIST_PATH_, body1);
  console.log('HTTP ' + r1.code);

  var daily = [];
  if (r1.code === 204) {
    console.log('204: DAILY-ОТЧЁТОВ НЕТ за период ' + dailyFrom + '…' + today + ' — это сам по себе важный результат.');
  } else if (r1.code !== 200) {
    console.log('❌ ' + String(r1.body).substring(0, 400));
    return;
  } else {
    var p1 = wbFinProbeParseSafe_(r1.body, 'reportId');
    daily = p1.arr || [];

    console.log('── BigInt-проверка reportId (daily) ── (до сортировки: raw/native сверяются по порядку ответа API)');
    console.log('regex-ID: ' + p1.rawIds.length + ' | объектов: ' + daily.length + ' | native-ID: ' + p1.nativeIds.length);
    if (p1.mappingError) console.log('🔴 PARSE_MAPPING_ERROR: ' + p1.note);
    var losses = 0;
    for (var i = 0; i < daily.length; i++) {
      if (p1.nativeIds[i] !== undefined && p1.nativeIds[i] !== p1.rawIds[i]) {
        losses++;
        if (losses <= 5) console.log('  LOSS: raw=' + p1.rawIds[i] + ' native=' + p1.nativeIds[i]);
      }
    }
    console.log(losses ? ('🔴 ПОТЕРЯ ТОЧНОСТИ у ' + losses + '/' + daily.length + ' reportId — native JSON.parse ЗАПРЕЩЁН для daily') :
      '✓ Потери точности на текущих ID нет (строковый парсер в лоадере всё равно обязателен)');
    var maxLen = 0;
    for (var L = 0; L < p1.rawIds.length; L++) maxLen = Math.max(maxLen, p1.rawIds[L].length);
    console.log('Макс. длина reportId: ' + maxLen + ' цифр (2^53 ≈ 16 цифр)');

    // Явная сортировка: не полагаемся на порядок ответа API
    daily.sort(function (a, b) {
      var c = String(a.dateFrom).localeCompare(String(b.dateFrom));
      return c !== 0 ? c : String(a.reportType).localeCompare(String(b.reportType));
    });

    if (daily.length >= WB_FIN_PROBE_LIST_LIMIT_) {
      console.log('⚠️ rows == limit (' + WB_FIN_PROBE_LIST_LIMIT_ + ') — есть ещё страницы: повторить с WB_FIN_PROBE_LIST_OFFSET_ = ' + (WB_FIN_PROBE_LIST_OFFSET_ + WB_FIN_PROBE_LIST_LIMIT_));
    }

    console.log('── DAILY-отчёты: ' + daily.length + ' шт ──');
    console.log('дата | type | reportId | createDate | лаг(дн) | forPaySum | retailAmountSum');
    var byDayType = {};
    for (var d = 0; d < daily.length; d++) {
      var o = daily[d];
      var dfrom = String(o.dateFrom || '').substring(0, 10);
      var dto = String(o.dateTo || '').substring(0, 10);
      var lag = o.createDate ? wbFinProbeDaysDiff_(dto, String(o.createDate).substring(0, 10)) : '?';
      console.log('  ' + dfrom + (dto !== dfrom ? ('…' + dto + ' ⚠️(период≠день!)') : '') +
        ' | ' + o.reportType + ' | ' + String(o.reportId) + ' | ' + o.createDate + ' | ' + lag +
        ' | ' + o.forPaySum + ' | ' + o.retailAmountSum);
      byDayType[dfrom + '|' + o.reportType] = o;
    }

    var missT1 = [], haveYesterday = false;
    var yesterday = wbFinProbeDateStr_(-1);
    for (var dd = 1; dd <= WB_FIN_PROBE_DAILY_LOOKBACK_DAYS_; dd++) {
      var ds = wbFinProbeDateStr_(-dd);
      if (!byDayType[ds + '|1']) missT1.push(ds);
      if (ds === yesterday && byDayType[ds + '|1']) haveYesterday = true;
    }
    console.log('Покрытие type=1 за 14 дней: пропуски: ' + (missT1.length ? missT1.join(', ') : 'нет'));
    console.log('Вчера (' + yesterday + ') присутствует: ' + (haveYesterday ? 'ДА' : 'НЕТ'));
  }

  Utilities.sleep(WB_FIN_PROBE_SLEEP_MS_);

  // ── A2: list period=weekly ──
  var body2 = JSON.stringify({ dateFrom: weeklyFrom, dateTo: today, period: 'weekly', limit: WB_FIN_PROBE_LIST_LIMIT_, offset: 0 });
  console.log('═══ A2: LIST weekly ═══');
  console.log('POST ' + WB_FIN_PROBE_LIST_PATH_ + '  body=' + body2);
  var r2 = wbFinProbePost_(token, WB_FIN_PROBE_LIST_PATH_, body2);
  console.log('HTTP ' + r2.code);
  var weekly = [];
  if (r2.code === 200) {
    var p2 = wbFinProbeParseSafe_(r2.body, 'reportId');
    if (p2.mappingError) console.log('🔴 PARSE_MAPPING_ERROR (weekly): ' + p2.note);
    weekly = p2.arr || [];
    // Явная сортировка: не полагаемся на порядок ответа API
    weekly.sort(function (a, b) {
      var c = String(a.dateFrom).localeCompare(String(b.dateFrom));
      return c !== 0 ? c : String(a.reportType).localeCompare(String(b.reportType));
    });
    console.log('── WEEKLY-отчёты: ' + weekly.length + ' шт ──');
    for (var w = 0; w < weekly.length; w++) {
      var ow = weekly[w];
      var lagW = ow.createDate ? wbFinProbeDaysDiff_(String(ow.dateTo).substring(0, 10), String(ow.createDate).substring(0, 10)) : '?';
      console.log('  ' + ow.dateFrom + '…' + ow.dateTo + ' | type ' + ow.reportType + ' | ' + String(ow.reportId) +
        ' | createDate ' + ow.createDate + ' (лаг ' + lagW + ' дн) | forPaySum ' + ow.forPaySum);
    }
  } else {
    console.log((r2.code === 204 ? '204: недельных отчётов нет в окне' : '❌ ' + String(r2.body).substring(0, 300)));
  }

  // ── A3: Σ daily vs weekly по list-суммам (без detailed!) ──
  // ВАЖНО: weekly = FINAL всегда; Δ измеряет ТОЧНОСТЬ ежедневного PROVISIONAL-слоя, а не выбор канона.
  if (daily.length && weekly.length) {
    console.log('═══ A3: Σ DAILY vs WEEKLY (list-суммы; Δ = точность PROVISIONAL) ═══');
    var METRICS = ['forPaySum', 'retailAmountSum', 'deliveryServiceSum', 'paidStorageSum', 'paidAcceptanceSum', 'deductionSum', 'penaltySum'];
    var wDone = 0;
    for (var wi = weekly.length - 1; wi >= 0 && wDone < 4; wi--) {
      var W = weekly[wi];
      var wf = String(W.dateFrom).substring(0, 10), wt = String(W.dateTo).substring(0, 10);
      var span = wbFinProbeDaysDiff_(wf, wt) + 1;
      var dRep = [], missing = [];
      for (var s = 0; s < span; s++) {
        var dsW = Utilities.formatDate(new Date(new Date(wf).getTime() + s * 86400000), 'Europe/Moscow', 'yyyy-MM-dd');
        var hit = null;
        for (var q = 0; q < daily.length; q++) {
          if (String(daily[q].dateFrom).substring(0, 10) === dsW && String(daily[q].reportType) === String(W.reportType)) { hit = daily[q]; break; }
        }
        if (hit) dRep.push(hit); else missing.push(dsW);
      }
      console.log('Неделя ' + wf + '…' + wt + ' type=' + W.reportType + ' [weekly ' + String(W.reportId) + ']: daily-покрытие ' +
        dRep.length + '/' + span + (missing.length ? (' (нет: ' + missing.join(', ') + ')') : ''));
      if (!missing.length) {
        for (var mi = 0; mi < METRICS.length; mi++) {
          var mm = METRICS[mi], sd = 0;
          for (var q2 = 0; q2 < dRep.length; q2++) sd += wbFinProbeNum_(dRep[q2][mm]);
          var swk = wbFinProbeNum_(W[mm]);
          var delta = wbFinProbeR2_(sd - swk);
          var pct = swk !== 0 ? wbFinProbeR2_(delta / swk * 100) : (sd === 0 ? 0 : 100);
          console.log('    ' + mm + ': Σdaily=' + wbFinProbeR2_(sd) + ' weekly=' + swk + ' Δ=' + delta + ' (' + pct + '%)');
        }
        wDone++;
      }
    }
    if (!wDone) console.log('(ни одной недели с полным daily-покрытием в окне — увеличьте lookback A)');

    // Кандидаты-константы для B и C: последняя неделя type=1 с ПОЛНЫМ daily-покрытием
    // (все дни недели имеют daily-отчёт того же reportType), берём середину недели.
    var candFound = false;
    for (var wc = weekly.length - 1; wc >= 0 && !candFound; wc--) {
      var Wc = weekly[wc];
      if (String(Wc.reportType) !== '1') continue;
      var wcf = String(Wc.dateFrom).substring(0, 10);
      var wct = String(Wc.dateTo).substring(0, 10);
      var spanC = wbFinProbeDaysDiff_(wcf, wct) + 1;
      var missC = [], byDateC = {};
      for (var sc = 0; sc < spanC; sc++) {
        var dsC = Utilities.formatDate(new Date(new Date(wcf).getTime() + sc * 86400000), 'Europe/Moscow', 'yyyy-MM-dd');
        var hitC = null;
        for (var q3 = 0; q3 < daily.length; q3++) {
          if (String(daily[q3].dateFrom).substring(0, 10) === dsC && String(daily[q3].reportType) === '1') { hitC = daily[q3]; break; }
        }
        if (hitC) byDateC[dsC] = hitC; else missC.push(dsC);
      }
      if (missC.length) {
        console.log('(неделя ' + wcf + '…' + wct + ' type=1 как кандидат отклонена: нет daily за ' + missC.join(', ') + ')');
        continue;
      }
      var midDate = Utilities.formatDate(new Date(new Date(wcf).getTime() + 3 * 86400000), 'Europe/Moscow', 'yyyy-MM-dd');
      var midRep = byDateC[midDate];
      console.log('═══ КОНСТАНТЫ ДЛЯ B/C (скопировать в шапку probe; неделя ' + wcf + '…' + wct + ', daily-покрытие ' + spanC + '/' + spanC + ') ═══');
      console.log("WB_FIN_PROBE_DAILY_ID_   = '" + String(midRep.reportId) + "';");
      console.log("WB_FIN_PROBE_DAILY_DATE_ = '" + midDate + "';");
      console.log("WB_FIN_PROBE_WEEKLY_ID_  = '" + String(Wc.reportId) + "';");
      candFound = true;
    }
    if (!candFound) console.log('⚠️ Кандидат для B/C не найден: нет недели type=1 с полным daily-покрытием в окне. Увеличьте lookback A.');
  }
  console.log('━━━ A завершён. Скопируйте ВЕСЬ лог в чат. ━━━');
}


// ═══════════════════════════════════════════════════
//  B. DETAILED дневного отчёта  [1..4 запроса]
// ═══════════════════════════════════════════════════

function wbFinProbeB_DailyDetailed() {
  var token = wbFinProbeToken_();
  if (!token) { console.log('❌ Нет токена WB_TOKEN_FINANCE'); return; }
  if (!WB_FIN_PROBE_DAILY_ID_) { console.log('❌ Заполните WB_FIN_PROBE_DAILY_ID_ (из вывода A)'); return; }

  console.log('═══ B: DETAILED daily reportId=' + WB_FIN_PROBE_DAILY_ID_ + ' (день ' + WB_FIN_PROBE_DAILY_DATE_ + ') ═══');
  if (String(WB_FIN_PROBE_B_START_RRDID_) !== '0') {
    console.log('⚠️ Запуск с ненулевого курсора: анализ охватит только хвост отчёта после rrdId=' + WB_FIN_PROBE_B_START_RRDID_);
  }
  var res = wbFinProbeFetchDetailed_(token, WB_FIN_PROBE_DAILY_ID_, WB_FIN_PROBE_B_START_RRDID_, 'B');
  if (!res.ok) { console.log('❌ ' + res.error); return; }
  if (!res.complete) {
    console.log('⏸️ НЕПОЛНЫЙ ПРОХОД. Строк получено: ' + res.rows.length + ' | запросов: ' + res.requests);
    console.log("ПРОДОЛЖЕНИЕ: WB_FIN_PROBE_B_START_RRDID_ = '" + res.cursor + "';");
    console.log('🔴 ANALYSIS_DISABLED: суммы/DIGEST по частичным данным не считаются — сравнение было бы неполным. Для EVETIS ожидается одна страница; повторите запуск.');
    return;
  }
  var rows = res.rows;
  console.log('Строк: ' + rows.length + ' | запросов: ' + res.requests + ' | complete: ' + res.complete);
  if (!rows.length) { console.log('Пусто (204/[]).'); return; }

  console.log('── ПОЛЯ (' + Object.keys(rows[0]).length + '): ' + Object.keys(rows[0]).join(', '));

  // Ключ rrdId внутри отчёта
  var seen = {}, dup = 0, empty = 0;
  for (var i = 0; i < rows.length; i++) {
    var rid = String(rows[i].rrdId || '');
    if (!rid) { empty++; continue; }
    if (seen[rid]) dup++; else seen[rid] = 1;
  }
  console.log('rrdId: пустых=' + empty + ' | дублей ВНУТРИ отчёта=' + dup +
    (dup ? ' 🔴 КОНФЛИКТ КЛЮЧА ВНУТРИ ОТЧЁТА' : ' ✓'));

  // Даты строк
  var rrCnt = wbFinProbeCountBy_(rows, 'rrDate');
  console.log('rrDate по строкам: ' + wbFinProbeFmtCounts_(rrCnt));
  console.log('reportType по строкам: ' + wbFinProbeFmtCounts_(wbFinProbeCountBy_(rows, 'reportType')) +
    ' | dateFrom/dateTo строк: ' + rows[0].dateFrom + ' / ' + rows[0].dateTo);
  console.log('docTypeName: ' + wbFinProbeFmtCounts_(wbFinProbeCountBy_(rows, 'docTypeName')));
  console.log('sellerOperName: ' + wbFinProbeFmtCounts_(wbFinProbeCountBy_(rows, 'sellerOperName')));

  // Суммы
  var sums = wbFinProbeSumRows_(rows);
  console.log('── СУММЫ (сверить с list-строкой этого отчёта из A): ' + JSON.stringify(sums));
  console.log('DAILY_SUMS_JSON (константа для C, скопировать всю строку):');
  console.log("WB_FIN_PROBE_DAILY_SUMS_JSON_ = '" + JSON.stringify(sums) + "';");

  // DIGEST для классификации в C: rrdId:forPay
  console.log('── DIGEST (склеить значения всех строк ЧЕРЕЗ ; в WB_FIN_PROBE_DAILY_DIGEST_) ──');
  var chunk = [], nChunk = 0;
  for (var g = 0; g < rows.length; g++) {
    chunk.push(String(rows[g].rrdId) + ':' + wbFinProbeR2_(wbFinProbeNum_(rows[g].forPay)));
    if (chunk.length === 50 || g === rows.length - 1) {
      console.log('DIGEST[' + (nChunk++) + ']: ' + chunk.join(';'));
      chunk = [];
    }
  }
  console.log('━━━ B завершён. Скопируйте ВЕСЬ лог в чат. ━━━');
}


// ═══════════════════════════════════════════════════
//  C. DETAILED недельного отчёта + сравнение  [1..4 запроса]
// ═══════════════════════════════════════════════════

function wbFinProbeC_WeeklyDetailed() {
  var token = wbFinProbeToken_();
  if (!token) { console.log('❌ Нет токена WB_TOKEN_FINANCE'); return; }
  if (!WB_FIN_PROBE_WEEKLY_ID_) { console.log('❌ Заполните WB_FIN_PROBE_WEEKLY_ID_ (из вывода A)'); return; }

  console.log('═══ C: DETAILED weekly reportId=' + WB_FIN_PROBE_WEEKLY_ID_ + ' ═══');
  if (String(WB_FIN_PROBE_C_START_RRDID_) !== '0') {
    console.log('⚠️ Запуск с ненулевого курсора: анализ охватит только хвост отчёта после rrdId=' + WB_FIN_PROBE_C_START_RRDID_);
  }
  var res = wbFinProbeFetchDetailed_(token, WB_FIN_PROBE_WEEKLY_ID_, WB_FIN_PROBE_C_START_RRDID_, 'C');
  if (!res.ok) { console.log('❌ ' + res.error); return; }
  if (!res.complete) {
    console.log('⏸️ НЕПОЛНЫЙ ПРОХОД. Строк: ' + res.rows.length + ' | запросов: ' + res.requests);
    console.log("ПРОДОЛЖЕНИЕ: WB_FIN_PROBE_C_START_RRDID_ = '" + res.cursor + "';");
    console.log('🔴 ANALYSIS_DISABLED: разбивка/сравнение/классификация по частичным данным не считаются. Для EVETIS ожидается одна страница; повторите запуск.');
    return;
  }
  var rows = res.rows;
  console.log('Строк: ' + rows.length + ' | запросов: ' + res.requests + ' | complete: ' + res.complete);
  if (!rows.length) { console.log('Пусто (204/[]).'); return; }

  // Ключ внутри недельного отчёта
  var seenW = {}, dupW = 0, emptyW = 0;
  var byDay = {};
  for (var i = 0; i < rows.length; i++) {
    var rid = String(rows[i].rrdId || '');
    if (!rid) emptyW++; else if (seenW[rid]) dupW++; else seenW[rid] = 1;
    var day = String(rows[i].rrDate || '').substring(0, 10);
    if (!byDay[day]) byDay[day] = [];
    byDay[day].push(rows[i]);
  }
  console.log('rrdId: пустых=' + emptyW + ' | дублей ВНУТРИ отчёта=' + dupW +
    (dupW ? ' 🔴 КОНФЛИКТ КЛЮЧА ВНУТРИ ОТЧЁТА' : ' ✓'));

  console.log('── СУММЫ weekly (весь отчёт): ' + JSON.stringify(wbFinProbeSumRows_(rows)));
  console.log('── Разбивка weekly по rrDate ──');
  var days = Object.keys(byDay).sort();
  for (var d = 0; d < days.length; d++) {
    var s = wbFinProbeSumRows_(byDay[days[d]]);
    console.log('  ' + days[d] + ': строк=' + s.rows + ' forPay=' + s.forPay + ' retailAmount=' + s.retailAmount +
      ' deliveryService=' + (s.deliveryService === undefined ? '—' : s.deliveryService));
  }
  console.log('sellerOperName (weekly): ' + wbFinProbeFmtCounts_(wbFinProbeCountBy_(rows, 'sellerOperName')));

  // Сравнение с daily по вставленным агрегатам
  if (WB_FIN_PROBE_DAILY_DATE_ && byDay[WB_FIN_PROBE_DAILY_DATE_]) {
    var wx = wbFinProbeSumRows_(byDay[WB_FIN_PROBE_DAILY_DATE_]);
    console.log('── Weekly-строки дня ' + WB_FIN_PROBE_DAILY_DATE_ + ': ' + JSON.stringify(wx));
    if (WB_FIN_PROBE_DAILY_SUMS_JSON_) {
      try {
        var ds = JSON.parse(WB_FIN_PROBE_DAILY_SUMS_JSON_);
        console.log('── Δ (weekly-день − daily-отчёт); Δ = точность PROVISIONAL, weekly = FINAL всегда ──');
        for (var k in wx) {
          if (k === 'rows' || ds[k] === undefined) continue;
          console.log('  ' + k + ': weekly=' + wx[k] + ' daily=' + ds[k] + ' Δ=' + wbFinProbeR2_(wx[k] - ds[k]));
        }
      } catch (e) { console.log('⚠️ DAILY_SUMS_JSON не разобран: ' + e); }
    }
  } else if (WB_FIN_PROBE_DAILY_DATE_) {
    console.log('⚠️ В weekly НЕТ строк с rrDate=' + WB_FIN_PROBE_DAILY_DATE_ + ' — проверить соответствие недель.');
  }

  // Классификация rrdId: common / daily-only / weekly-only / common-changed
  if (WB_FIN_PROBE_DAILY_DIGEST_) {
    var dmap = {}, dtotal = 0;
    var parts = WB_FIN_PROBE_DAILY_DIGEST_.split(';');
    for (var p = 0; p < parts.length; p++) {
      var kv = parts[p].split(':');
      if (kv.length === 2 && kv[0]) { dmap[kv[0]] = Number(kv[1]); dtotal++; }
    }
    var wmapDay = {};
    var scope = byDay[WB_FIN_PROBE_DAILY_DATE_] || [];
    for (var s2 = 0; s2 < scope.length; s2++) wmapDay[String(scope[s2].rrdId)] = wbFinProbeNum_(scope[s2].forPay);

    var common = 0, changed = 0, dailyOnly = 0, weeklyOnly = 0;
    var exChanged = [], exDailyOnly = [], exWeeklyOnly = [];
    for (var dk in dmap) {
      if (wmapDay[dk] !== undefined) {
        if (Math.abs(wmapDay[dk] - dmap[dk]) <= WB_FIN_PROBE_MONEY_TOL_) common++;
        else { changed++; if (exChanged.length < 5) exChanged.push(dk + ' (d=' + dmap[dk] + ' w=' + wmapDay[dk] + ')'); }
      } else { dailyOnly++; if (exDailyOnly.length < 5) exDailyOnly.push(dk); }
    }
    for (var wk in wmapDay) {
      if (dmap[wk] === undefined) { weeklyOnly++; if (exWeeklyOnly.length < 5) exWeeklyOnly.push(wk); }
    }
    // daily-ID где-либо в weekly (вне дня X) — на случай сдвига rrDate
    var anywhere = 0;
    for (var dk2 in dmap) if (seenW[dk2]) anywhere++;

    console.log('═══ КЛАССИФИКАЦИЯ rrdId (день ' + WB_FIN_PROBE_DAILY_DATE_ + ') ═══');
    console.log('daily всего=' + dtotal + ' | weekly в дне=' + scope.length);
    console.log('common (совпали значения)=' + common);
    console.log('common-changed (ID общий, forPay изменился)=' + changed + (exChanged.length ? ' | примеры: ' + exChanged.join('; ') : ''));
    console.log('daily-only=' + dailyOnly + (exDailyOnly.length ? ' | примеры: ' + exDailyOnly.join(', ') : ''));
    console.log('weekly-only=' + weeklyOnly + (exWeeklyOnly.length ? ' | примеры: ' + exWeeklyOnly.join(', ') : ''));
    console.log('daily-ID найдены где-либо в weekly (любой день): ' + anywhere + '/' + dtotal);
  } else {
    console.log('(WB_FIN_PROBE_DAILY_DIGEST_ пуст — классификация rrdId пропущена; заполните из лога B и перезапустите C)');
  }
  console.log('━━━ C завершён. Скопируйте ВЕСЬ лог в чат. ━━━');
}
