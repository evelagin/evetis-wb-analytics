/**
 * ══════════════════════════════════════════════════════════════
 *  WbFinanceDaily.gs — Production Finance Loader (PR-Fin1)  v1.3.1
 *
 *  v1.3.1 — финальная правка аудита (APPROVE WITH ONE MINOR CHANGE):
 *   reportType строго ∈ {1,2} (type 3 не исследован probe'ом и не входит в
 *   rev3-final — его появление в list = fail-closed ошибка, не молчаливое
 *   расширение); finDailyIsDate_ — календарная проверка (2026-99-99 → false).
 *
 *  v1.3 — 4 minor-правки финального code audit (APPROVE WITH MINOR CHANGES):
 *   1) wbFinDeepBackfillRegister() берёт общий ScriptLock на весь прогон;
 *   2) C0-marker ставится ПОСЛЕ успешного finDailyDropTestViews_()
 *      (DROP больше не проглатывает ошибки);
 *   3) finDailyList_ fail-closed валидирует reportType∈{1,2,3},
 *      формат dateFrom/dateTo и dateFrom<=dateTo;
 *   4) finDailyQuery_ проверяет наличие jobId до polling при
 *      jobComplete=false.
 *   Зафиксировано: legacy wbFinanceBackfillAutoTick — НЕ production-
 *   entrypoint (несовместим с нереентерабельным pacer-lock).
 *  Двухслойная модель: DAILY = PROVISIONAL, WEEKLY = FINAL.
 *
 *  v1.2 — правки повторного code audit:
 *   Б1: пейсер — RESERVATION в ScriptProperties ДО fetch (ошибка записи =
 *       fail-closed, запрос не выполняется); бюджет-чек учитывает будущее
 *       ожидание пейсера (projected wait + маржа);
 *   Б2: legacy-вызовы Finance API (WbFinanceApiV1.gs → wbFinV1Post_ —
 *       единая точка для list/detailed из ОБОИХ старых файлов) подключены
 *       к тому же персистентному пейсеру + короткий lock на резервацию
 *       (см. wbFinV1PacerReserve_ в WbFinanceApiV1.gs);
 *   Б3: versioned C0-success marker (ScriptProperty): C0 удаляет marker на
 *       старте, ставит ТОЛЬКО при полном успехе; provали C0 → marker
 *       отсутствует; установщик триггеров и сам тик проверяют marker +
 *       наличие production-объектов (fail-closed);
 *   + finDailyQuery_ бросает исключение при исчерпании guard с
 *     непустым pageToken (частичный результат не выдаётся);
 *   + V_WB_FINANCE создаётся с ЯВНЫМ старым списком колонок (из
 *     INFORMATION_SCHEMA до изменений), а не SELECT *;
 *   + после production CREATE OR REPLACE — программный post-snapshot
 *     assert (не только __TEST);
 *   + тип RAW.loaded_at проверяется; STRING → нормализация
 *     SAFE_CAST(... AS TIMESTAMP) в ORDER BY дедупа;
 *   + в логах провала C0 явно указано, что additive ALTER/таблицы уже
 *     могли быть созданы (это безопасно: nullable / IF NOT EXISTS).
 *
 *  v1.1 — правки code audit (CHANGES REQUIRED → исправлено):
 *   Б1: пейсер Finance API персистентный (ScriptProperties) — 1 req/мин
 *       держится и МЕЖДУ запусками, не только внутри тика;
 *   Б2: finDailyQuery_ не падает при jobComplete=false — polling через
 *       Jobs.getQueryResults + сбор всех страниц по pageToken;
 *   Б3: C0 неразрушительный — сначала __TEST-вью, программное сравнение
 *       снапшотов (count/даты/суммы) и schema-совместимость по
 *       INFORMATION_SCHEMA, и только при полном совпадении замена
 *       production-вью;
 *   + PK-assert manifest в каждом тике до и после discovery;
 *   + best-effort ERROR + диагностика при неприменившемся COMPLETE-MERGE;
 *   + stale-recovery очищает processing_run_id и started_at;
 *   + loaded_at = new Date().toISOString() (UTC с offset'ом, не МСК без);
 *   + в WbFinanceApiV1.gs курсор rrdId держится строкой (отдельный diff).
 *
 *  Источник истины дизайна: docs/FINANCE_DAILY_DESIGN_2026-07-22.md
 *  (ревизия 3-final, утверждена 22.07). Код реализует дизайн 1:1.
 *
 *  Ключевые гарантии:
 *   - manifest FINANCE_REPORT_LOADS: grain = report_id (логический PK,
 *     ВСЕ insert'ы только MERGE ON report_id), DISCOVERED→STARTED→
 *     COMPLETE|ERROR; attempt_count инкрементится ТОЛЬКО при STARTED;
 *     COMPLETE immutable (повторный list лишь сверяет метрики);
 *   - валидность RAW-строк: только (report_id, run_id=processing_run_id)
 *     со status='COMPLETE'; частичные попытки невидимы;
 *   - retry всегда с rrdId=0; COMPLETE только после post-load SQL:
 *     persisted_rows = persisted_distinct_rrd = rows_fetched = rows_loaded;
 *   - BigInt: reportId/rrdId живут СТРОКАМИ (quoted-transform до parse,
 *     сверка raw/safe количеств → PARSE_MAPPING_ERROR), в path/payload
 *     подставляются вербатим;
 *   - rate limit: 1 req/мин на ВСЕ вызовы Finance API — общий пейсер 61с;
 *   - cutover FIN_DAILY_CUTOVER_ = '2026-07-13' (пн, МСК); preflight
 *     fail-closed в C0 и в установщике триггеров;
 *   - деньги в новых таблицах NUMERIC; RAW остаётся STRING (конвенция);
 *   - запись в RAW — атомарные load-job'ы (bqLoadRows_), НЕ streaming.
 *
 *  Функции владельца (порядок приёмки — дизайн §8):
 *    1) wbFinDailyInitC0()            — ALTER RAW + preflight + таблицы + вью
 *                                       + инвариант legacy (count/даты/суммы)
 *    2) runWbFinanceDaily()           — один тик (вручную для приёмки)
 *    3) wbFinDailyStatus()            — состояние manifest/недель/прогонов
 *    4) wbFinInstallDailyTriggers()   — 3 тика/день, ТОЛЬКО после приёмки
 *       wbFinRemoveDailyTriggers()
 *    5) wbFinDeepBackfillRegister()   — ручная регистрация отчётов периода
 *
 *  Существующие файлы не меняются (кроме отдельной 2-строчной правки
 *  rrdid→rrdId в WbFinanceApiV1.gs — идёт тем же PR отдельным diff'ом).
 *  Потребители V_WB_FINANCE не переключаются в этом PR; сама V_WB_FINANCE
 *  пересоздаётся с фильтром WEEKLY-слоя поверх COMPLETE (иначе daily-строки
 *  задвоили бы легаси-чтение — см. дизайн §3.6/§6).
 * ══════════════════════════════════════════════════════════════
 */

// ═══════════════ КОНФИГ ═══════════════

var FIN_DAILY_TRIGGER_FN_       = 'runWbFinanceDaily';
var FIN_DAILY_CUTOVER_          = '2026-07-13';  // пн первой недели нового контура
var FIN_DAILY_LOOKBACK_DAYS_    = 3;             // daily discovery: сегодня−3…сегодня
var FIN_DAILY_STALE_STARTED_MIN_ = 120;          // recovery зависших STARTED
var FIN_DAILY_MAX_ATTEMPTS_     = 5;             // потолок ретраев отчёта
var FIN_DAILY_SLEEP_MS_         = 61000;         // 1 req/мин Finance API
var FIN_DAILY_BUDGET_MS_        = 270000;        // ~4.5 мин на тик
var FIN_DAILY_REQ_MARGIN_       = 30000;         // запас на сам fetch+обработку сверх pacer-wait
var FIN_DAILY_PAGE_LIMIT_       = 100000;        // limit detailed
var FIN_DAILY_LIST_LIMIT_       = 1000;          // limit list (+offset-пагинация)
var FIN_DAILY_LOCK_WAIT_MS_     = 30000;
var FIN_DAILY_METRIC_TOL_       = 0.01;          // допуск сверки метрик, ₽
var FIN_DAILY_SOURCE_API_       = 'WB_API_FIN_V1';
var FIN_DAILY_LOAD_BATCH_       = 10000;         // строк на load-job (отчёт < батча → 1 атомарный job)
var FIN_DAILY_PACER_PROP_       = 'WB_FIN_API_LAST_REQ_MS'; // персистентный пейсер (общий с WbFinanceApiV1.gs)
var FIN_DAILY_C0_MARKER_PROP_   = 'WB_FIN_DAILY_C0_OK';     // versioned-маркер успешного C0
var FIN_DAILY_C0_VERSION_       = 'C0_V1_2026-07-13';       // версия схемы C0 (+cutover)

var FIN_T_RUNS_        = 'FINANCE_LOADER_RUNS';
var FIN_T_LOADS_       = 'FINANCE_REPORT_LOADS';
var FIN_T_WEEK_STATUS_ = 'FINANCE_WEEK_STATUS';
var FIN_T_WEEK_RECON_  = 'FINANCE_WEEK_RECON';
var FIN_V_COMPLETE_    = 'V_WB_FINANCE_COMPLETE';
var FIN_V_CANONICAL_   = 'V_WB_FINANCE_CANONICAL';
var FIN_V_LEGACY_      = 'V_WB_FINANCE';

// list-метрики (имя в list-ответе → колонка manifest); тот же порядок в RECON
var FIN_LIST_METRICS_ = [
  ['forPaySum',          'list_forpay'],
  ['retailAmountSum',    'list_retail'],
  ['deliveryServiceSum', 'list_delivery'],
  ['paidStorageSum',     'list_storage'],
  ['paidAcceptanceSum',  'list_acceptance'],
  ['deductionSum',       'list_deduction'],
  ['penaltySum',         'list_penalty']
];
// detailed-метрики (поле строки → колонка manifest)
var FIN_DET_METRICS_ = [
  ['forPay',          'det_forpay'],
  ['retailAmount',    'det_retail'],
  ['deliveryService', 'det_delivery'],
  ['paidStorage',     'det_storage'],
  ['paidAcceptance',  'det_acceptance'],
  ['deduction',       'det_deduction'],
  ['penalty',         'det_penalty']
];

var FIN_API_HOST_      = 'https://finance-api.wildberries.ru';
var FIN_API_LIST_PATH_ = '/api/finance/v1/sales-reports/list';
var FIN_API_DET_PATH_  = '/api/finance/v1/sales-reports/detailed/'; // + reportId (строкой)


// ═══════════════ БАЗОВЫЕ ХЕЛПЕРЫ ═══════════════

function finDailyToken_() {
  if (typeof getFinanceV1Token_ === 'function') {
    var tk = getFinanceV1Token_();
    return tk ? tk.token : null;
  }
  return PropertiesService.getScriptProperties().getProperty('WB_TOKEN_FINANCE');
}

function finDailyTodayMsk_(offsetDays) {
  return Utilities.formatDate(new Date(Date.now() + (offsetDays || 0) * 86400000),
    'Europe/Moscow', 'yyyy-MM-dd');
}

/** PK-assert manifest (fail-closed): COUNT(*) == COUNT(DISTINCT report_id). */
function finDailyAssertManifestPk_(stage) {
  var q = finDailyQuery_(
    'SELECT COUNT(*) AS c, COUNT(DISTINCT report_id) AS d FROM ' + finDailyTbl_(FIN_T_LOADS_), []);
  var c = String(q.rows[0].f[0].v), d = String(q.rows[0].f[1].v);
  if (c !== d) {
    throw new Error('MANIFEST_PK_VIOLATION (' + stage + '): COUNT=' + c + ' != DISTINCT=' + d);
  }
}

/** Понедельник ISO-недели даты 'YYYY-MM-DD' (чистая календарная арифметика:
 *  даты WB уже бизнес-даты МСК, конверсия часового пояса не нужна). */
function finDailyWeekStart_(ds) {
  var d = new Date(String(ds).substring(0, 10) + 'T00:00:00Z');
  if (isNaN(d.getTime())) return '';
  var dow = (d.getUTCDay() + 6) % 7; // 0=пн … 6=вс
  var m = new Date(d.getTime() - dow * 86400000);
  return m.toISOString().substring(0, 10);
}

function finDailyIsDigits_(s) { return /^\d+$/.test(String(s)); }

/** Календарная проверка даты (не только форма строки: 2026-99-99 → false). */
function finDailyIsDate_(s) {
  s = String(s);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(s)) return false;
  var d = new Date(s + 'T00:00:00Z');
  return !isNaN(d.getTime()) && d.toISOString().substring(0, 10) === s;
}

function finDailyNum_(v) {
  if (v === null || v === undefined || v === '') return 0;
  if (typeof v === 'number') return isFinite(v) ? v : 0;
  var n = Number(String(v).replace(/\s/g, '').replace(',', '.'));
  return isNaN(n) ? 0 : n;
}

function finDailyR2_(n) { return Math.round(n * 100) / 100; }

function finDailyMd5_(s) {
  var b = Utilities.computeDigest(Utilities.DigestAlgorithm.MD5, s, Utilities.Charset.UTF_8);
  var hex = '';
  for (var i = 0; i < b.length; i++) {
    var x = (b[i] + 256) % 256;
    hex += (x < 16 ? '0' : '') + x.toString(16);
  }
  return hex;
}

function finDailyNewRunId_() {
  return 'FINDAILY_' + Utilities.formatDate(new Date(), 'Europe/Moscow', 'yyyyMMdd_HHmmss') +
    '_' + Math.floor(Math.random() * 10000);
}

function finDailyTbl_(name) {
  var c = getBqConfig_();
  return '`' + c.projectId + '.' + c.datasetId + '.' + name + '`';
}


// ═══════════════ ПАРАМЕТРИЗОВАННЫЙ BQ (инъекции исключены) ═══════════════
// bqQuery_ из WbBigQuery.gs не поддерживает параметры → свой хелпер поверх
// того же advanced-сервиса. ВСЕ значения из внешнего мира (report_id, даты,
// метрики, сообщения об ошибках) идут ТОЛЬКО через named-параметры.
// В текст SQL интерполируются только имена таблиц из getBqConfig_() и
// числовые константы файла.

function finDailyP_(name, type, value) {
  return {
    name: name,
    parameterType: { type: type },
    parameterValue: { value: (value === null || value === undefined) ? null : String(value) }
  };
}

function finDailyPArr_(name, type, values) {
  return {
    name: name,
    parameterType: { type: 'ARRAY', arrayType: { type: type } },
    parameterValue: { arrayValues: (values || []).map(function (v) { return { value: String(v) }; }) }
  };
}

/**
 * SELECT/DML с named-параметрами. Возвращает {rows, fields, affected}.
 * jobComplete=false НЕ ошибка: поллим Jobs.getQueryResults до завершения
 * и собираем ВСЕ страницы результата по pageToken.
 */
function finDailyQuery_(sql, params) {
  var c = getBqConfig_();
  var req = {
    query: sql, useLegacySql: false, location: c.location,
    parameterMode: 'NAMED', timeoutMs: 30000
  };
  if (params && params.length) req.queryParameters = params;
  var res = BigQuery.Jobs.query(req, c.projectId);

  var jobId = res.jobReference && res.jobReference.jobId;
  var loc = (res.jobReference && res.jobReference.location) || c.location;
  var rows = [], fields = null, affected = null, pageToken = null;
  var complete = res.jobComplete === true;

  // Fail-closed: без jobId поллить нечего — незавершённый ответ без ссылки на job
  if (!complete && !jobId) {
    throw new Error('BQ query: jobComplete=false и нет jobReference.jobId — polling невозможен');
  }

  if (complete) {
    fields = (res.schema && res.schema.fields) || [];
    if (res.rows) rows = rows.concat(res.rows);
    if (res.numDmlAffectedRows !== undefined) affected = Number(res.numDmlAffectedRows);
    pageToken = res.pageToken || null;
  }

  var guard = 0;
  while ((!complete || pageToken) && guard++ < 300) {
    var opts = { location: loc, timeoutMs: 30000, maxResults: 10000 };
    if (pageToken) opts.pageToken = pageToken;
    var r2 = BigQuery.Jobs.getQueryResults(c.projectId, jobId, opts);
    if (r2.jobComplete !== true) { Utilities.sleep(1000); continue; }
    complete = true;
    if (fields === null) fields = (r2.schema && r2.schema.fields) || [];
    if (r2.rows) rows = rows.concat(r2.rows);
    if (r2.numDmlAffectedRows !== undefined) affected = Number(r2.numDmlAffectedRows);
    pageToken = r2.pageToken || null;
    if (!pageToken) break;
  }
  if (!complete) throw new Error('BQ query: результат не получен (jobId=' + jobId + ')');
  if (pageToken) {
    throw new Error('BQ query: guard исчерпан при непустом pageToken (jobId=' + jobId +
      ') — частичный результат не выдаётся');
  }
  return { rows: rows, fields: fields || [], affected: affected };
}

/** rows API-ответа → массив объектов {имя_колонки: строка|null}. */
function finDailyRowsToObjs_(q) {
  var out = [];
  for (var i = 0; i < q.rows.length; i++) {
    var o = {}, f = q.rows[i].f || [];
    for (var j = 0; j < q.fields.length; j++) o[q.fields[j].name] = f[j] ? f[j].v : null;
    out.push(o);
  }
  return out;
}


// ═══════════════ FINANCE API: пейсер + BigInt-safe разбор ═══════════════

/**
 * Общий пейсер: >=61с между ЛЮБЫМИ запросами Finance API — ПЕРСИСТЕНТНЫЙ
 * (ScriptProperties, общий ключ с legacy-контуром WbFinanceApiV1.gs).
 * Семантика v1.2: слот РЕЗЕРВИРУЕТСЯ записью в property ДО fetch;
 * ошибка записи = fail-closed (запрос не выполняется). Тик работает под
 * общим ScriptLock, поэтому его read-modify-write не гоняется с самим
 * собой; конкурентный legacy-вызов сериализуется коротким lock'ом на
 * стороне wbFinV1PacerReserve_ (WbFinanceApiV1.gs).
 */
function finDailyPacerLast_(st) {
  var last = st.lastReqAt || 0;
  var p = PropertiesService.getScriptProperties().getProperty(FIN_DAILY_PACER_PROP_);
  var pv = p ? Number(p) : 0;
  if (pv > last) last = pv;
  return last;
}

/** Ожидание до ближайшего разрешённого запроса, мс (для budget-чека). */
function finDailyProjectedWait_(st) {
  var last;
  try { last = finDailyPacerLast_(st); } catch (e) { last = st.lastReqAt || 0; }
  return last ? Math.max(0, FIN_DAILY_SLEEP_MS_ - (Date.now() - last)) : 0;
}

/** Резервация слота ДО fetch. Исключение (в т.ч. на setProperty) = fail-closed. */
function finDailyPacerReserve_(st) {
  for (var g = 0; g < 5; g++) {
    var last = finDailyPacerLast_(st); // без try/catch: сбой чтения = fail-closed
    var wait = last ? (FIN_DAILY_SLEEP_MS_ - (Date.now() - last)) : 0;
    if (wait > 0) { Utilities.sleep(wait); continue; } // после сна перечитываем
    var stamp = Date.now();
    PropertiesService.getScriptProperties()
      .setProperty(FIN_DAILY_PACER_PROP_, String(stamp)); // throw → запрос НЕ выполняется
    st.lastReqAt = stamp;
    return;
  }
  throw new Error('Finance API pacer: слот не зарезервирован (конкуренция за пейсер)');
}

function finDailyBudgetLeft_(st) { return FIN_DAILY_BUDGET_MS_ - (Date.now() - st.t0); }

/** Бюджет-чек учитывает БУДУЩЕЕ ожидание пейсера + маржу на fetch/обработку. */
function finDailyCanRequest_(st) {
  return finDailyBudgetLeft_(st) > finDailyProjectedWait_(st) + FIN_DAILY_REQ_MARGIN_;
}

/** POST с готовой строкой payload (значения BigInt-полей — вербатим).
 *  Слот пейсера резервируется ДО fetch (fail-closed при сбое записи). */
function finDailyPost_(st, path, payloadStr) {
  finDailyPacerReserve_(st);
  var resp = UrlFetchApp.fetch(FIN_API_HOST_ + path, {
    method: 'post',
    contentType: 'application/json',
    headers: { 'Authorization': st.token },
    payload: payloadStr,
    muteHttpExceptions: true
  });
  st.requests++;
  return { code: resp.getResponseCode(), body: resp.getContentText() };
}

var FIN_BIGINT_FIELDS_ = ['reportId', 'rrdId', 'giId', 'shkId'];

function finDailyQuoteBigInts_(body) {
  var re = new RegExp('"(' + FIN_BIGINT_FIELDS_.join('|') + ')"\\s*:\\s*(-?\\d+)', 'g');
  return body.replace(re, '"$1":"$2"');
}

/**
 * Безопасный разбор массива: idField извлекается регексом из сырого body
 * (строки), body с закавыченными BigInt-полями парсится; несовпадение
 * количеств или значений → mappingError (PARSE_MAPPING_ERROR).
 */
function finDailyParseSafe_(body, idField) {
  var out = { arr: null, rawIds: [], mappingError: false, note: '' };
  var re = new RegExp('"' + idField + '"\\s*:\\s*(-?\\d+)', 'g'), m;
  while ((m = re.exec(body)) !== null) out.rawIds.push(m[1]);
  try {
    var safe = JSON.parse(finDailyQuoteBigInts_(body));
    out.arr = Array.isArray(safe) ? safe : (safe && safe.data ? safe.data : null);
  } catch (e) {
    out.mappingError = true;
    out.note = 'safe parse: ' + e;
    return out;
  }
  if (!out.arr) { out.mappingError = true; out.note = 'массив не распознан'; return out; }
  if (out.rawIds.length !== out.arr.length) {
    out.mappingError = true;
    out.note = 'PARSE_MAPPING_ERROR: regex ' + out.rawIds.length + ' vs объектов ' + out.arr.length;
    return out;
  }
  for (var i = 0; i < out.arr.length; i++) {
    if (String(out.arr[i][idField]) !== out.rawIds[i]) {
      out.mappingError = true;
      out.note = 'PARSE_MAPPING_ERROR: raw!=safe @' + i;
      return out;
    }
  }
  return out;
}

/**
 * list за период c offset-пагинацией. Возвращает
 * {ok, reports:[{reportId(строка), reportType(число), dateFrom, dateTo,
 *   createDate, metrics:{list_*: число}, metricsJson}], error}.
 */
function finDailyList_(st, period, dateFrom, dateTo) {
  var out = { ok: true, reports: [], error: '' };
  var offset = 0, guard = 0;
  while (guard++ < 20) {
    if (!finDailyCanRequest_(st)) { out.ok = false; out.error = 'BUDGET_EXCEEDED (list)'; return out; }
    var payload = JSON.stringify({
      dateFrom: dateFrom, dateTo: dateTo, period: period,
      limit: FIN_DAILY_LIST_LIMIT_, offset: offset
    });
    var r = finDailyPost_(st, FIN_API_LIST_PATH_, payload);
    if (r.code === 204) return out; // пусто — норма
    if (r.code !== 200) {
      out.ok = false;
      out.error = 'list HTTP ' + r.code + ': ' + String(r.body).substring(0, 200);
      return out;
    }
    var p = finDailyParseSafe_(r.body, 'reportId');
    if (p.mappingError) { out.ok = false; out.error = 'list ' + p.note; return out; }
    for (var i = 0; i < p.arr.length; i++) {
      var o = p.arr[i];
      var rid = String(o.reportId || '');
      if (!finDailyIsDigits_(rid)) { out.ok = false; out.error = 'list: reportId не цифры: ' + rid; return out; }
      // Fail-closed валидация полей list-строки (до попадания в manifest).
      // ТОЛЬКО type 1 и 2: probe/rev3-final доказали семантику только для них;
      // type 3 (Грузия) не исследован — его появление = СТОП, а не молчаливое
      // расширение модели (добавляется позднее отдельным probe + правкой дизайна).
      var rt = Number(o.reportType);
      if (rt !== 1 && rt !== 2) {
        out.ok = false;
        out.error = 'list: неподдерживаемый reportType=' + o.reportType + ' у reportId=' + rid;
        return out;
      }
      var df = String(o.dateFrom || '').substring(0, 10);
      var dt = String(o.dateTo || '').substring(0, 10);
      if (!finDailyIsDate_(df) || !finDailyIsDate_(dt)) {
        out.ok = false; out.error = 'list: битые даты (' + df + '/' + dt + ') у ' + rid; return out;
      }
      if (df > dt) {
        out.ok = false; out.error = 'list: dateFrom > dateTo (' + df + ' > ' + dt + ') у ' + rid; return out;
      }
      var metrics = {}, mj = {};
      for (var k = 0; k < FIN_LIST_METRICS_.length; k++) {
        metrics[FIN_LIST_METRICS_[k][1]] = finDailyR2_(finDailyNum_(o[FIN_LIST_METRICS_[k][0]]));
        mj[FIN_LIST_METRICS_[k][0]] = o[FIN_LIST_METRICS_[k][0]];
      }
      out.reports.push({
        reportId: rid,
        reportType: rt,
        dateFrom: df,
        dateTo: dt,
        createDate: String(o.createDate || '').substring(0, 10),
        metrics: metrics,
        metricsJson: JSON.stringify(mj)
      });
    }
    if (p.arr.length < FIN_DAILY_LIST_LIMIT_) return out;
    offset += FIN_DAILY_LIST_LIMIT_; // редкий случай: страниц больше одной
  }
  out.ok = false; out.error = 'list: превышен guard пагинации';
  return out;
}

/** detailed целиком, ВСЕГДА с rrdId=0 (retry = полная перезагрузка). */
function finDailyFetchDetailed_(st, reportIdStr) {
  var out = { ok: true, rows: [], pages: 0, error: '' };
  var cursor = '0', guard = 0;
  while (guard++ < 100) {
    if (!finDailyCanRequest_(st)) { out.ok = false; out.error = 'BUDGET_EXCEEDED (detailed)'; return out; }
    var payload = '{"rrdId":' + cursor + ',"limit":' + FIN_DAILY_PAGE_LIMIT_ + '}';
    var r = finDailyPost_(st, FIN_API_DET_PATH_ + encodeURIComponent(reportIdStr), payload);
    if (r.code === 204) return out;
    if (r.code !== 200) {
      out.ok = false;
      out.error = 'detailed HTTP ' + r.code + ': ' + String(r.body).substring(0, 200);
      return out;
    }
    var p = finDailyParseSafe_(r.body, 'rrdId');
    if (p.mappingError) { out.ok = false; out.error = 'detailed ' + p.note; return out; }
    if (!p.arr.length) return out;
    for (var i = 0; i < p.arr.length; i++) out.rows.push(p.arr[i]);
    out.pages++;
    var last = String(p.arr[p.arr.length - 1].rrdId || '');
    if (!finDailyIsDigits_(last) || last === cursor) { out.ok = false; out.error = 'detailed: битый курсор rrdId'; return out; }
    cursor = last;
    if (p.arr.length < FIN_DAILY_PAGE_LIMIT_) return out;
  }
  out.ok = false; out.error = 'detailed: превышен guard пагинации';
  return out;
}


// ═══════════════ МАППИНГ detailed-строки → колонки RAW ═══════════════

function finDailyMapRow_(o, ctx) {
  var S = function (v) { return (v === null || v === undefined) ? '' : String(v); };
  var rrd = S(o.rrdId);
  var rrDate = S(o.rrDate).substring(0, 10);
  var row = {
    load_id: ctx.runId, run_id: ctx.runId,
    loaded_at: ctx.loadedAt,
    source_api: FIN_DAILY_SOURCE_API_,
    request_date_from: ctx.dateFrom, request_date_to: ctx.dateTo,
    report_id: ctx.reportId,
    report_period_from: ctx.dateFrom, report_period_to: ctx.dateTo,
    report_period: ctx.reportPeriod,           // 'DAILY' | 'WEEKLY'
    report_type: ctx.reportType,               // INT64 (число)
    row_hash: finDailyMd5_(FIN_DAILY_SOURCE_API_ + '|' + ctx.reportId + '|' + rrd),
    rrd_id: rrd, srid: S(o.srid), shk_id: S(o.shkId), sticker_id: S(o.stickerId),
    doc_type_name: S(o.docTypeName), supplier_oper_name: S(o.sellerOperName),
    order_dt: S(o.orderDt), sale_dt: S(o.saleDt), rr_dt: S(o.rrDate), create_dt: S(o.createDate),
    wb_nm_id: S(o.nmId), wb_vendor_code: S(o.vendorCode), barcode: S(o.sku),
    sa_name: S(o.vendorCode), ts_name: S(o.techSize),
    brand_name: S(o.brandName), subject_name: S(o.subjectName),
    office_name: S(o.officeName), country_name: S(o.country),
    retail_price: S(o.retailPrice), retail_amount: S(o.retailAmount),
    retail_price_withdisc_rub: S(o.retailPriceWithDisc),
    sale_percent: S(o.salePercent), commission_percent: S(o.commissionPercent),
    product_discount_for_report: S(o.productDiscountForReport),
    supplier_promo: S(o.sellerPromo), spp_percent: S(o.spp),
    quantity: S(o.quantity),
    for_pay: S(o.forPay),
    sales_commission: S(o.ppvzSalesCommission),
    logistics_amount: S(o.deliveryService),
    storage_fee: S(o.paidStorage),
    deduction: S(o.deduction), penalty: S(o.penalty),
    acceptance: S(o.paidAcceptance), additional_payment: S(o.additionalPayment),
    acquiring_fee: S(o.acquiringFee),
    currency: S(o.currency),
    rebill_logistics: S(o.rebillLogisticCost),
    processed_status: 'OK',
    // raw_json: JSON ОДНОГО detailed-объекта, одной строкой (не HTTP-body).
    // BigInt-поля внутри — строками (следствие quoted-transform): точность сохранена.
    raw_json: JSON.stringify(o)
  };
  if (finDailyIsDate_(rrDate)) row._rr_date = rrDate;
  else {
    var sd = S(o.saleDt).substring(0, 10);
    if (finDailyIsDate_(sd)) row._rr_date = sd;
  }
  return row;
}


// ═══════════════ MANIFEST: MERGE-переходы (атомарные DML) ═══════════════

/** DISCOVERED: insert-if-absent; для DISCOVERED/ERROR — обновление list-метрик.
 *  COMPLETE-строку НЕ трогает никогда (immutable). */
function finDailyMergeDiscovered_(rep, runId) {
  var sql =
    'MERGE ' + finDailyTbl_(FIN_T_LOADS_) + ' t\n' +
    'USING (SELECT @report_id AS report_id) s ON t.report_id = s.report_id\n' +
    'WHEN MATCHED AND t.status IN (\'DISCOVERED\',\'ERROR\') THEN UPDATE SET\n' +
    '  list_forpay=@m0, list_retail=@m1, list_delivery=@m2, list_storage=@m3,\n' +
    '  list_acceptance=@m4, list_deduction=@m5, list_penalty=@m6,\n' +
    '  list_metrics_json=@mjson\n' +
    'WHEN NOT MATCHED THEN INSERT (report_id, report_period, report_type,\n' +
    '  date_from, date_to, status, discovered_run_id, attempt_count, discovered_at,\n' +
    '  list_forpay, list_retail, list_delivery, list_storage, list_acceptance,\n' +
    '  list_deduction, list_penalty, list_metrics_json)\n' +
    'VALUES (@report_id, @period, @rtype, @dfrom, @dto, \'DISCOVERED\', @run_id, 0,\n' +
    '  CURRENT_TIMESTAMP(), @m0, @m1, @m2, @m3, @m4, @m5, @m6, @mjson)';
  var p = [
    finDailyP_('report_id', 'STRING', rep.reportId),
    finDailyP_('period', 'STRING', rep.period),
    finDailyP_('rtype', 'INT64', rep.reportType),
    finDailyP_('dfrom', 'DATE', rep.dateFrom),
    finDailyP_('dto', 'DATE', rep.dateTo),
    finDailyP_('run_id', 'STRING', runId),
    finDailyP_('mjson', 'STRING', rep.metricsJson)
  ];
  for (var i = 0; i < FIN_LIST_METRICS_.length; i++) {
    p.push(finDailyP_('m' + i, 'NUMERIC', rep.metrics[FIN_LIST_METRICS_[i][1]]));
  }
  finDailyQuery_(sql, p);
}

/** STARTED: ЕДИНСТВЕННАЯ точка инкремента attempt_count. Возвращает true, если строка захвачена. */
function finDailyMergeStarted_(reportId, runId) {
  var sql =
    'MERGE ' + finDailyTbl_(FIN_T_LOADS_) + ' t\n' +
    'USING (SELECT @report_id AS report_id) s ON t.report_id = s.report_id\n' +
    'WHEN MATCHED AND t.status IN (\'DISCOVERED\',\'ERROR\')\n' +
    '  AND t.attempt_count < ' + FIN_DAILY_MAX_ATTEMPTS_ + ' THEN UPDATE SET\n' +
    '  status=\'STARTED\', processing_run_id=@run_id, started_at=CURRENT_TIMESTAMP(),\n' +
    '  attempt_count=t.attempt_count+1, error_message=NULL';
  var q = finDailyQuery_(sql, [
    finDailyP_('report_id', 'STRING', reportId),
    finDailyP_('run_id', 'STRING', runId)
  ]);
  return q.affected === 1;
}

/** COMPLETE: только из STARTED и только своим processing_run_id. */
function finDailyMergeComplete_(reportId, runId, rowsFetched, rowsLoaded, detSums) {
  var sql =
    'MERGE ' + finDailyTbl_(FIN_T_LOADS_) + ' t\n' +
    'USING (SELECT @report_id AS report_id) s ON t.report_id = s.report_id\n' +
    'WHEN MATCHED AND t.status=\'STARTED\' AND t.processing_run_id=@run_id THEN UPDATE SET\n' +
    '  status=\'COMPLETE\', completed_at=CURRENT_TIMESTAMP(),\n' +
    '  rows_fetched=@rf, rows_loaded=@rl,\n' +
    '  det_forpay=@d0, det_retail=@d1, det_delivery=@d2, det_storage=@d3,\n' +
    '  det_acceptance=@d4, det_deduction=@d5, det_penalty=@d6, error_message=NULL';
  var p = [
    finDailyP_('report_id', 'STRING', reportId),
    finDailyP_('run_id', 'STRING', runId),
    finDailyP_('rf', 'INT64', rowsFetched),
    finDailyP_('rl', 'INT64', rowsLoaded)
  ];
  for (var i = 0; i < FIN_DET_METRICS_.length; i++) {
    p.push(finDailyP_('d' + i, 'NUMERIC', detSums[FIN_DET_METRICS_[i][1]]));
  }
  var q = finDailyQuery_(sql, p);
  return q.affected === 1;
}

function finDailyMergeError_(reportId, runId, msg) {
  var sql =
    'MERGE ' + finDailyTbl_(FIN_T_LOADS_) + ' t\n' +
    'USING (SELECT @report_id AS report_id) s ON t.report_id = s.report_id\n' +
    'WHEN MATCHED AND t.status=\'STARTED\' AND t.processing_run_id=@run_id THEN UPDATE SET\n' +
    '  status=\'ERROR\', error_message=@msg';
  finDailyQuery_(sql, [
    finDailyP_('report_id', 'STRING', reportId),
    finDailyP_('run_id', 'STRING', runId),
    finDailyP_('msg', 'STRING', String(msg || '').substring(0, 900))
  ]);
}

/** Stale-recovery: STARTED старше N минут → DISCOVERED. attempt_count НЕ трогаем. */
function finDailyStaleRecovery_() {
  var sql =
    'MERGE ' + finDailyTbl_(FIN_T_LOADS_) + ' t\n' +
    'USING (SELECT 1 AS one) s ON t.status=\'STARTED\'\n' +
    '  AND t.started_at < TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL ' +
    FIN_DAILY_STALE_STARTED_MIN_ + ' MINUTE)\n' +
    'WHEN MATCHED THEN UPDATE SET status=\'DISCOVERED\',\n' +
    '  processing_run_id=NULL, started_at=NULL,\n' +
    '  error_message=CONCAT(\'stale STARTED recovered \', CAST(CURRENT_TIMESTAMP() AS STRING))';
  var q = finDailyQuery_(sql, []);
  return q.affected || 0;
}


// ═══════════════ ЗАГРУЗКА ОДНОГО ОТЧЁТА ═══════════════

/**
 * DISCOVERED/ERROR → STARTED → detailed(rrdId=0) → RAW load-job →
 * post-load SQL → COMPLETE|ERROR. Возвращает 'COMPLETE'|'ERROR'|'SKIPPED'.
 */
function finDailyLoadReport_(st, item) {
  var rid = String(item.report_id);
  if (!finDailyIsDigits_(rid)) { console.error('load: reportId не цифры: ' + rid); return 'SKIPPED'; }

  if (!finDailyMergeStarted_(rid, st.runId)) {
    console.log('  ⏭ ' + rid + ': не захвачен (статус изменился/лимит попыток)');
    return 'SKIPPED';
  }

  var fetched = finDailyFetchDetailed_(st, rid);
  if (!fetched.ok) {
    finDailyMergeError_(rid, st.runId, fetched.error);
    console.error('  ✗ ' + rid + ': ' + fetched.error);
    return 'ERROR';
  }
  var rows = fetched.rows;

  // Ключ внутри отчёта: rrdId заполнен и уникален (fail-closed до записи)
  var seen = {}, dup = 0, empty = 0;
  for (var i = 0; i < rows.length; i++) {
    var rr = String(rows[i].rrdId || '');
    if (!rr || !finDailyIsDigits_(rr)) { empty++; continue; }
    if (seen[rr]) dup++; else seen[rr] = 1;
  }
  if (empty > 0 || dup > 0) {
    finDailyMergeError_(rid, st.runId, 'KEY_CONFLICT: пустых rrdId=' + empty + ', дублей=' + dup);
    console.error('  ✗ ' + rid + ': KEY_CONFLICT empty=' + empty + ' dup=' + dup);
    return 'ERROR';
  }

  // Маппинг + суммы detailed
  var ctx = {
    runId: st.runId, loadedAt: new Date().toISOString(), // UTC c offset'ом (Z), не МСК без offset
    reportId: rid, reportPeriod: item.report_period,
    reportType: Number(item.report_type) || 0,
    dateFrom: item.date_from, dateTo: item.date_to
  };
  var objs = [], detSums = {};
  for (var d = 0; d < FIN_DET_METRICS_.length; d++) detSums[FIN_DET_METRICS_[d][1]] = 0;
  for (var r2 = 0; r2 < rows.length; r2++) {
    objs.push(finDailyMapRow_(rows[r2], ctx));
    for (var d2 = 0; d2 < FIN_DET_METRICS_.length; d2++) {
      detSums[FIN_DET_METRICS_[d2][1]] += finDailyNum_(rows[r2][FIN_DET_METRICS_[d2][0]]);
    }
  }
  for (var d3 = 0; d3 < FIN_DET_METRICS_.length; d3++) {
    detSums[FIN_DET_METRICS_[d3][1]] = finDailyR2_(detSums[FIN_DET_METRICS_[d3][1]]);
  }

  // Запись: атомарные load-job'ы (отчёты EVETIS < батча → ровно 1 job)
  var loaded = 0;
  try {
    for (var b = 0; b < objs.length; b += FIN_DAILY_LOAD_BATCH_) {
      loaded += bqLoadRows_(BQ_TABLE_FINANCE_, objs.slice(b, b + FIN_DAILY_LOAD_BATCH_));
    }
  } catch (e) {
    finDailyMergeError_(rid, st.runId, 'LOAD_JOB: ' + ((e && e.message) || e));
    console.error('  ✗ ' + rid + ': load-job — ' + e);
    return 'ERROR';
  }

  // Post-load SQL: фактически сохранённое в BQ (дизайн §4 шаг 4)
  var chk = finDailyQuery_(
    'SELECT COUNT(*) AS c, COUNT(DISTINCT rrd_id) AS d FROM ' +
    finDailyTbl_(BQ_TABLE_FINANCE_) + '\nWHERE report_id=@rid AND run_id=@run',
    [finDailyP_('rid', 'STRING', rid), finDailyP_('run', 'STRING', st.runId)]
  );
  var persisted = Number(chk.rows[0].f[0].v), distinctRrd = Number(chk.rows[0].f[1].v);
  if (!(persisted === distinctRrd && persisted === rows.length && persisted === loaded)) {
    finDailyMergeError_(rid, st.runId, 'POSTLOAD_MISMATCH: persisted=' + persisted +
      ' distinct=' + distinctRrd + ' fetched=' + rows.length + ' loaded=' + loaded);
    console.error('  ✗ ' + rid + ': POSTLOAD_MISMATCH');
    return 'ERROR';
  }

  if (!finDailyMergeComplete_(rid, st.runId, rows.length, loaded, detSums)) {
    // Диагностика: кто держит строку manifest на самом деле
    var diag = '';
    try {
      var dq2 = finDailyQuery_(
        'SELECT status, processing_run_id, attempt_count FROM ' + finDailyTbl_(FIN_T_LOADS_) +
        '\nWHERE report_id = @rid',
        [finDailyP_('rid', 'STRING', rid)]);
      var dr = finDailyRowsToObjs_(dq2)[0] || {};
      diag = 'manifest: status=' + dr.status + ', processing_run_id=' + dr.processing_run_id +
        ', attempts=' + dr.attempt_count;
    } catch (eDiag) { diag = 'диагностика недоступна: ' + eDiag; }
    console.error('  ✗ ' + rid + ': COMPLETE-MERGE не применился (0 строк). ' + diag);
    // Best-effort ERROR (guard по processing_run_id: чужую строку не тронем)
    try { finDailyMergeError_(rid, st.runId, 'COMPLETE_MERGE_NOT_APPLIED; ' + diag); }
    catch (eErr) { console.error('  best-effort ERROR тоже не применился: ' + eErr); }
    return 'ERROR';
  }
  console.log('  ✓ ' + rid + ' [' + item.report_period + ' t' + item.report_type + ' ' +
    item.date_from + '] строк ' + rows.length + ', стр. ' + fetched.pages);
  return 'COMPLETE';
}


// ═══════════════ DISCOVERY ═══════════════

/** Финализированные недели: {'YYYY-MM-DD|type': true}. */
function finDailyFinalWeeksMap_() {
  var q = finDailyQuery_(
    'SELECT CAST(week_start AS STRING) AS ws, report_type FROM ' +
    finDailyTbl_(FIN_T_WEEK_STATUS_) + ' WHERE weekly_final = TRUE', []);
  var map = {};
  var objs = finDailyRowsToObjs_(q);
  for (var i = 0; i < objs.length; i++) map[objs[i].ws + '|' + objs[i].report_type] = true;
  return map;
}

/** Статусы и метрики manifest по списку report_id (для сверки immutable COMPLETE). */
function finDailyManifestByIds_(ids) {
  if (!ids.length) return {};
  var cols = FIN_LIST_METRICS_.map(function (x) { return x[1]; }).join(', ');
  var q = finDailyQuery_(
    'SELECT report_id, status, ' + cols + ' FROM ' + finDailyTbl_(FIN_T_LOADS_) +
    '\nWHERE report_id IN UNNEST(@ids)',
    [finDailyPArr_('ids', 'STRING', ids)]);
  var map = {};
  var objs = finDailyRowsToObjs_(q);
  for (var i = 0; i < objs.length; i++) map[objs[i].report_id] = objs[i];
  return map;
}

/**
 * Регистрация отчётов из list. Правила (дизайн §3.3/§4):
 *  - daily финализированной (week×type) недели НЕ регистрируется;
 *  - существующая COMPLETE-строка — только сверка метрик; расхождение → anomaly;
 *  - иначе MERGE DISCOVERED (второй queue-item невозможен).
 * Возвращает {registered, skippedFinal, anomalies:[]}.
 */
function finDailyRegisterReports_(st, reports, period, finalWeeks) {
  var res = { registered: 0, skippedFinal: 0, anomalies: [] };
  var candidates = [];
  for (var i = 0; i < reports.length; i++) {
    var rep = reports[i];
    if (period === 'DAILY' &&
        finalWeeks[finDailyWeekStart_(rep.dateFrom) + '|' + rep.reportType]) {
      res.skippedFinal++;
      continue;
    }
    candidates.push(rep);
  }
  if (!candidates.length) return res;

  var manifest = finDailyManifestByIds_(candidates.map(function (r) { return r.reportId; }));
  for (var j = 0; j < candidates.length; j++) {
    var rep2 = candidates[j];
    var ex = manifest[rep2.reportId];
    if (ex && ex.status === 'COMPLETE') {
      // Immutable: только сверка list-метрик
      for (var k = 0; k < FIN_LIST_METRICS_.length; k++) {
        var col = FIN_LIST_METRICS_[k][1];
        var stored = finDailyNum_(ex[col]), fresh = rep2.metrics[col];
        if (Math.abs(stored - fresh) > FIN_DAILY_METRIC_TOL_) {
          res.anomalies.push(rep2.reportId + ':' + col + ' stored=' + stored + ' fresh=' + fresh);
        }
      }
      continue;
    }
    finDailyMergeDiscovered_({
      reportId: rep2.reportId, period: period, reportType: rep2.reportType,
      dateFrom: rep2.dateFrom, dateTo: rep2.dateTo,
      metrics: rep2.metrics, metricsJson: rep2.metricsJson
    }, st.runId);
    if (!ex) res.registered++;
  }
  return res;
}

/** Календарные строки WEEK_STATUS: от cutover до последней завершённой недели, типы 1 и 2. */
function finDailyEnsureWeekRows_() {
  var lastCompleted = finDailyWeekStart_(finDailyTodayMsk_(-7));
  if (lastCompleted < FIN_DAILY_CUTOVER_) return;
  var sql =
    'MERGE ' + finDailyTbl_(FIN_T_WEEK_STATUS_) + ' t\n' +
    'USING (\n' +
    '  SELECT ws AS week_start, rt AS report_type\n' +
    '  FROM UNNEST(GENERATE_DATE_ARRAY(@cutover, @last_week, INTERVAL 7 DAY)) ws,\n' +
    '       UNNEST([1, 2]) rt\n' +
    ') s ON t.week_start = s.week_start AND t.report_type = s.report_type\n' +
    'WHEN NOT MATCHED THEN INSERT (week_start, report_type, weekly_final)\n' +
    'VALUES (s.week_start, s.report_type, FALSE)';
  finDailyQuery_(sql, [
    finDailyP_('cutover', 'DATE', FIN_DAILY_CUTOVER_),
    finDailyP_('last_week', 'DATE', lastCompleted)
  ]);
}


// ═══════════════ ФИНАЛИЗАЦИЯ НЕДЕЛЬ + RECON ═══════════════

function finDailyFinalizeWeeks_() {
  var finalized = 0;
  // Кандидаты: weekly COMPLETE (>= cutover), неделя ещё не финализирована
  var cols = FIN_LIST_METRICS_.map(function (x) { return 'm.' + x[1]; }).join(', ');
  var q = finDailyQuery_(
    'SELECT m.report_id, m.report_type,\n' +
    '  CAST(DATE_TRUNC(m.date_from, WEEK(MONDAY)) AS STRING) AS week_start, ' + cols + '\n' +
    'FROM ' + finDailyTbl_(FIN_T_LOADS_) + ' m\n' +
    'LEFT JOIN ' + finDailyTbl_(FIN_T_WEEK_STATUS_) + ' w\n' +
    '  ON w.week_start = DATE_TRUNC(m.date_from, WEEK(MONDAY)) AND w.report_type = m.report_type\n' +
    'WHERE m.status = \'COMPLETE\' AND m.report_period = \'WEEKLY\'\n' +
    '  AND m.date_from >= @cutover AND COALESCE(w.weekly_final, FALSE) = FALSE',
    [finDailyP_('cutover', 'DATE', FIN_DAILY_CUTOVER_)]);
  var weeklies = finDailyRowsToObjs_(q);

  for (var i = 0; i < weeklies.length; i++) {
    var wk = weeklies[i];
    // Σ daily по manifest той же недели/типа
    var dcols = FIN_LIST_METRICS_.map(function (x) {
      return 'IFNULL(SUM(' + x[1] + '), 0) AS ' + x[1];
    }).join(', ');
    var dq = finDailyQuery_(
      'SELECT COUNT(*) AS days_loaded, ' + dcols + ' FROM ' + finDailyTbl_(FIN_T_LOADS_) + '\n' +
      'WHERE report_period = \'DAILY\' AND status = \'COMPLETE\' AND report_type = @rt\n' +
      '  AND date_from BETWEEN @ws AND DATE_ADD(@ws, INTERVAL 6 DAY)',
      [finDailyP_('rt', 'INT64', wk.report_type), finDailyP_('ws', 'DATE', wk.week_start)]);
    var ds = finDailyRowsToObjs_(dq)[0];

    // RECON: 7 метрик одним MERGE (до установки weekly_final)
    var unionParts = [], rp = [
      finDailyP_('ws', 'DATE', wk.week_start),
      finDailyP_('rt', 'INT64', wk.report_type)
    ];
    for (var m2 = 0; m2 < FIN_LIST_METRICS_.length; m2++) {
      var col = FIN_LIST_METRICS_[m2][1];
      var sd = finDailyR2_(finDailyNum_(ds[col]));
      var sw = finDailyR2_(finDailyNum_(wk[col]));
      unionParts.push('SELECT @mn' + m2 + ' AS metric, CAST(@sd' + m2 +
        ' AS NUMERIC) AS sum_daily, CAST(@sw' + m2 + ' AS NUMERIC) AS sum_weekly');
      rp.push(finDailyP_('mn' + m2, 'STRING', FIN_LIST_METRICS_[m2][0]));
      rp.push(finDailyP_('sd' + m2, 'NUMERIC', sd));
      rp.push(finDailyP_('sw' + m2, 'NUMERIC', sw));
    }
    finDailyQuery_(
      'MERGE ' + finDailyTbl_(FIN_T_WEEK_RECON_) + ' t\n' +
      'USING (' + unionParts.join(' UNION ALL ') + ') s\n' +
      'ON t.week_start = @ws AND t.report_type = @rt AND t.metric = s.metric\n' +
      'WHEN MATCHED THEN UPDATE SET sum_daily = s.sum_daily, sum_weekly = s.sum_weekly,\n' +
      '  delta = s.sum_daily - s.sum_weekly,\n' +
      '  recon_status = IF(ABS(s.sum_daily - s.sum_weekly) <= ' + FIN_DAILY_METRIC_TOL_ + ', \'OK\', \'WARN\'),\n' +
      '  checked_at = CURRENT_TIMESTAMP()\n' +
      'WHEN NOT MATCHED THEN INSERT (week_start, report_type, metric, sum_daily, sum_weekly,\n' +
      '  delta, recon_status, checked_at)\n' +
      'VALUES (@ws, @rt, s.metric, s.sum_daily, s.sum_weekly, s.sum_daily - s.sum_weekly,\n' +
      '  IF(ABS(s.sum_daily - s.sum_weekly) <= ' + FIN_DAILY_METRIC_TOL_ + ', \'OK\', \'WARN\'),\n' +
      '  CURRENT_TIMESTAMP())', rp);

    // WEEK_STATUS → weekly_final=TRUE (после RECON)
    finDailyQuery_(
      'MERGE ' + finDailyTbl_(FIN_T_WEEK_STATUS_) + ' t\n' +
      'USING (SELECT @ws AS week_start, @rt AS report_type) s\n' +
      'ON t.week_start = s.week_start AND t.report_type = s.report_type\n' +
      'WHEN MATCHED THEN UPDATE SET weekly_final = TRUE, weekly_report_id = @rid,\n' +
      '  finalized_at = CURRENT_TIMESTAMP(), daily_days_loaded = @days\n' +
      'WHEN NOT MATCHED THEN INSERT (week_start, report_type, weekly_report_id,\n' +
      '  weekly_final, finalized_at, daily_days_loaded)\n' +
      'VALUES (@ws, @rt, @rid, TRUE, CURRENT_TIMESTAMP(), @days)',
      [
        finDailyP_('ws', 'DATE', wk.week_start),
        finDailyP_('rt', 'INT64', wk.report_type),
        finDailyP_('rid', 'STRING', wk.report_id),
        finDailyP_('days', 'INT64', Number(ds.days_loaded) || 0)
      ]);
    finalized++;
    console.log('  🏁 Неделя ' + wk.week_start + ' type=' + wk.report_type +
      ' финализирована weekly ' + wk.report_id + ' (daily days: ' + ds.days_loaded + ')');
  }
  return finalized;
}


// ═══════════════ ЖУРНАЛ ПРОГОНОВ ═══════════════

function finDailyRunOpen_(runId, triggerType) {
  bqLoadRows_(FIN_T_RUNS_, [{
    run_id: runId,
    started_at: new Date().toISOString(),
    status: 'RUNNING',
    trigger_type: triggerType
  }]);
}

function finDailyRunClose_(runId, status, counters, errMsg) {
  finDailyQuery_(
    'MERGE ' + finDailyTbl_(FIN_T_RUNS_) + ' t\n' +
    'USING (SELECT @run_id AS run_id) s ON t.run_id = s.run_id\n' +
    'WHEN MATCHED THEN UPDATE SET finished_at = CURRENT_TIMESTAMP(), status = @status,\n' +
    '  reports_discovered = @rd, reports_loaded = @rl2, reports_errors = @re,\n' +
    '  requests_made = @rq, error_message = @msg',
    [
      finDailyP_('run_id', 'STRING', runId),
      finDailyP_('status', 'STRING', status),
      finDailyP_('rd', 'INT64', counters.discovered),
      finDailyP_('rl2', 'INT64', counters.loaded),
      finDailyP_('re', 'INT64', counters.errors),
      finDailyP_('rq', 'INT64', counters.requests),
      finDailyP_('msg', 'STRING', String(errMsg || '').substring(0, 900))
    ]);
}


// ═══════════════ ОСНОВНОЙ ТИК ═══════════════

function runWbFinanceDaily() {
  return finDailyRun_('AUTO');
}

/** Ручной запуск того же тика (для приёмки). */
function wbFinDailyRunManual() {
  return finDailyRun_('MANUAL');
}

function finDailyRun_(triggerType) {
  var lock = LockService.getScriptLock();
  if (!lock.tryLock(FIN_DAILY_LOCK_WAIT_MS_)) {
    console.log('FinanceDaily: SKIPPED_LOCKED');
    return { status: 'SKIPPED_LOCKED' };
  }

  var st = {
    t0: Date.now(), lastReqAt: 0, requests: 0,
    runId: finDailyNewRunId_(), token: null
  };
  var counters = { discovered: 0, loaded: 0, errors: 0, requests: 0 };
  var anomalies = [], queueLeft = 0, runErr = '';

  try {
    st.token = finDailyToken_();
    if (!st.token) throw new Error('Нет токена WB_TOKEN_FINANCE');

    // Fail-closed: тик не работает без успешного C0 нужной версии
    var c0marker = PropertiesService.getScriptProperties().getProperty(FIN_DAILY_C0_MARKER_PROP_);
    if (c0marker !== FIN_DAILY_C0_VERSION_) {
      throw new Error('C0-marker отсутствует/не той версии (' + c0marker +
        ') — выполните wbFinDailyInitC0()');
    }

    finDailyRunOpen_(st.runId, triggerType);
    console.log('═══ FinanceDaily run ' + st.runId + ' (' + triggerType + ') ═══');

    // 1. Stale-recovery (attempt_count НЕ инкрементится)
    var recovered = finDailyStaleRecovery_();
    if (recovered) console.log('♻️ stale STARTED → DISCOVERED: ' + recovered);

    // 2. Календарные строки недель (по календарю, не по содержимому таблицы)
    finDailyEnsureWeekRows_();

    // PK-assert ДО discovery (fail-closed)
    finDailyAssertManifestPk_('до discovery');

    // 3. ОЧЕРЕДЬ — до новых list-запросов (дизайн §4 шаг 3)
    var qq = finDailyQuery_(
      'SELECT report_id, report_period, report_type,\n' +
      '  CAST(date_from AS STRING) AS date_from, CAST(date_to AS STRING) AS date_to\n' +
      'FROM ' + finDailyTbl_(FIN_T_LOADS_) + '\n' +
      'WHERE status = \'DISCOVERED\' OR (status = \'ERROR\' AND attempt_count < ' +
      FIN_DAILY_MAX_ATTEMPTS_ + ')\nORDER BY date_from, report_id', []);
    var queue = finDailyRowsToObjs_(qq);
    console.log('Очередь: ' + queue.length);
    for (var i = 0; i < queue.length; i++) {
      if (!finDailyCanRequest_(st)) { queueLeft = queue.length - i; break; }
      var resStatus = finDailyLoadReport_(st, queue[i]);
      if (resStatus === 'COMPLETE') counters.loaded++;
      else if (resStatus === 'ERROR') counters.errors++;
    }

    // 4. DISCOVERY — при остатке бюджета
    var finalWeeks = finDailyFinalWeeksMap_();
    if (finDailyCanRequest_(st)) {
      var dl = finDailyList_(st, 'daily',
        finDailyTodayMsk_(-FIN_DAILY_LOOKBACK_DAYS_), finDailyTodayMsk_(0));
      if (!dl.ok) { counters.errors++; runErr = dl.error; console.error('daily list: ' + dl.error); }
      else {
        var reg = finDailyRegisterReports_(st, dl.reports, 'DAILY', finalWeeks);
        counters.discovered += reg.registered;
        anomalies = anomalies.concat(reg.anomalies);
        console.log('daily discovery: отчётов ' + dl.reports.length + ', новых ' +
          reg.registered + ', пропущено (final week) ' + reg.skippedFinal);
      }
    } else queueLeft = Math.max(queueLeft, 1);

    // Weekly discovery: есть ли незакрытые (week×type) с существующим календарём
    var needWeekly = false, fwq = finDailyQuery_(
      'SELECT COUNT(*) AS n FROM ' + finDailyTbl_(FIN_T_WEEK_STATUS_) +
      ' WHERE weekly_final = FALSE', []);
    needWeekly = Number(fwq.rows[0].f[0].v) > 0;
    if (needWeekly && finDailyCanRequest_(st)) {
      var wl = finDailyList_(st, 'weekly', FIN_DAILY_CUTOVER_, finDailyTodayMsk_(0));
      if (!wl.ok) { counters.errors++; runErr = runErr || wl.error; console.error('weekly list: ' + wl.error); }
      else {
        var regW = finDailyRegisterReports_(st, wl.reports, 'WEEKLY', {});
        counters.discovered += regW.registered;
        anomalies = anomalies.concat(regW.anomalies);
        console.log('weekly discovery: отчётов ' + wl.reports.length + ', новых ' + regW.registered);
      }
    }

    // PK-assert ПОСЛЕ discovery (fail-closed)
    finDailyAssertManifestPk_('после discovery');

    // 5. Свежеоткрытое — в этом же тике при остатке бюджета
    if (counters.discovered > 0) {
      var q2 = finDailyQuery_(
        'SELECT report_id, report_period, report_type,\n' +
        '  CAST(date_from AS STRING) AS date_from, CAST(date_to AS STRING) AS date_to\n' +
        'FROM ' + finDailyTbl_(FIN_T_LOADS_) + '\nWHERE status = \'DISCOVERED\'\n' +
        'ORDER BY date_from, report_id', []);
      var fresh = finDailyRowsToObjs_(q2);
      for (var j = 0; j < fresh.length; j++) {
        if (!finDailyCanRequest_(st)) { queueLeft += fresh.length - j; break; }
        var rs2 = finDailyLoadReport_(st, fresh[j]);
        if (rs2 === 'COMPLETE') counters.loaded++;
        else if (rs2 === 'ERROR') counters.errors++;
      }
    }

    // 6. Финализация недель + RECON (только SQL по manifest — бюджета не ест)
    var finalized = finDailyFinalizeWeeks_();

    // 7. Аномалии immutable-COMPLETE — это ERROR прогона (fail-closed, manifest не тронут)
    if (anomalies.length) {
      runErr = 'IMMUTABLE_ANOMALY: ' + anomalies.join('; ').substring(0, 700);
      console.error('🔴 ' + runErr);
    }

    counters.requests = st.requests;
    var status =
      (anomalies.length || (counters.errors > 0 && counters.loaded === 0)) ? 'ERROR' :
      (counters.errors > 0 || queueLeft > 0) ? 'PARTIAL' :
      (counters.loaded === 0 && counters.discovered === 0) ? 'OK_NO_NEW' : 'OK';

    finDailyRunClose_(st.runId, status, counters, runErr);
    console.log('═══ ' + status + ' | загружено ' + counters.loaded + ', ошибок ' +
      counters.errors + ', открыто ' + counters.discovered + ', хвост очереди ' + queueLeft +
      ', финализировано недель ' + finalized + ', запросов ' + st.requests +
      ', ' + ((Date.now() - st.t0) / 1000).toFixed(0) + 'с ═══');
    return { status: status, run_id: st.runId, counters: counters, queue_left: queueLeft };

  } catch (e) {
    var em = (e && e.message) || String(e);
    console.error('FinanceDaily ERROR: ' + em);
    counters.requests = st.requests;
    try { finDailyRunClose_(st.runId, 'ERROR', counters, em); } catch (e2) { /* best-effort */ }
    return { status: 'ERROR', run_id: st.runId, error_message: em };
  } finally {
    lock.releaseLock();
  }
}


// ═══════════════ C0: ИНИЦИАЛИЗАЦИЯ (ALTER + preflight + таблицы + вью) ═══════════════

/** Снимок инвариантов вью (программный объект): count, min/max даты, Σ for_pay, Σ retail_amount. */
function finDailySnapshotView_(viewName) {
  var q = finDailyQuery_(
    'SELECT COUNT(*) AS c, CAST(MIN(_rr_date) AS STRING) AS mn, CAST(MAX(_rr_date) AS STRING) AS mx,\n' +
    '  ROUND(SUM(SAFE_CAST(REPLACE(REPLACE(for_pay, \' \', \'\'), \',\', \'.\') AS FLOAT64)), 2) AS s_forpay,\n' +
    '  ROUND(SUM(SAFE_CAST(REPLACE(REPLACE(retail_amount, \' \', \'\'), \',\', \'.\') AS FLOAT64)), 2) AS s_retail\n' +
    'FROM ' + finDailyTbl_(viewName), []);
  var o = finDailyRowsToObjs_(q)[0];
  return {
    view: viewName,
    c: String(o.c), mn: String(o.mn), mx: String(o.mx),
    sf: finDailyNum_(o.s_forpay), sr: finDailyNum_(o.s_retail)
  };
}

function finDailySnapshotStr_(s) {
  return s.view + ': count=' + s.c + ' | ' + s.mn + '…' + s.mx +
    ' | Σfor_pay=' + s.sf + ' | Σretail=' + s.sr;
}

/** Программное сравнение снапшотов: count/даты — точно, суммы — с допуском. */
function finDailySnapshotsEqual_(a, b) {
  var diffs = [];
  if (a.c !== b.c) diffs.push('count ' + a.c + '≠' + b.c);
  if (a.mn !== b.mn) diffs.push('min ' + a.mn + '≠' + b.mn);
  if (a.mx !== b.mx) diffs.push('max ' + a.mx + '≠' + b.mx);
  if (Math.abs(a.sf - b.sf) > FIN_DAILY_METRIC_TOL_) diffs.push('Σfor_pay ' + a.sf + '≠' + b.sf);
  if (Math.abs(a.sr - b.sr) > FIN_DAILY_METRIC_TOL_) diffs.push('Σretail ' + a.sr + '≠' + b.sr);
  return diffs;
}

/** Список колонок вью/таблицы по порядку (INFORMATION_SCHEMA). */
function finDailyViewColumns_(name) {
  var c = getBqConfig_();
  var q = finDailyQuery_(
    'SELECT column_name FROM `' + c.projectId + '.' + c.datasetId +
    '`.INFORMATION_SCHEMA.COLUMNS\nWHERE table_name = @tn ORDER BY ordinal_position',
    [finDailyP_('tn', 'STRING', name)]);
  return finDailyRowsToObjs_(q).map(function (o) { return o.column_name; });
}

/** Тип колонки таблицы (INFORMATION_SCHEMA), например RAW.loaded_at. */
function finDailyColumnType_(tableName, columnName) {
  var c = getBqConfig_();
  var q = finDailyQuery_(
    'SELECT data_type FROM `' + c.projectId + '.' + c.datasetId +
    '`.INFORMATION_SCHEMA.COLUMNS\nWHERE table_name = @tn AND column_name = @cn',
    [finDailyP_('tn', 'STRING', tableName), finDailyP_('cn', 'STRING', columnName)]);
  var o = finDailyRowsToObjs_(q)[0];
  return o ? String(o.data_type) : '';
}

/** Существование объектов (таблиц/вью) в датасете. Возвращает список отсутствующих. */
function finDailyMissingObjects_(names) {
  var c = getBqConfig_();
  var q = finDailyQuery_(
    'SELECT table_name FROM `' + c.projectId + '.' + c.datasetId +
    '`.INFORMATION_SCHEMA.TABLES\nWHERE table_name IN UNNEST(@names)',
    [finDailyPArr_('names', 'STRING', names)]);
  var have = {};
  finDailyRowsToObjs_(q).forEach(function (o) { have[o.table_name] = true; });
  return names.filter(function (n) { return !have[n]; });
}

/** Валидация имени колонки перед подстановкой в SQL (инъекции исключены). */
function finDailyAssertIdent_(name) {
  if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(String(name))) {
    throw new Error('Недопустимое имя колонки из INFORMATION_SCHEMA: ' + name);
  }
  return name;
}

/** Preflight (fail-closed): legacy-строк на/после cutover быть не должно. */
function finDailyPreflightCutover_() {
  var q = finDailyQuery_(
    'SELECT COUNT(*) AS n FROM ' + finDailyTbl_(BQ_TABLE_FINANCE_) +
    '\nWHERE run_id IS NULL AND _rr_date >= @cutover',
    [finDailyP_('cutover', 'DATE', FIN_DAILY_CUTOVER_)]);
  var n = Number(q.rows[0].f[0].v);
  if (n > 0) {
    throw new Error('PREFLIGHT FAIL: ' + n + ' legacy-строк с _rr_date >= ' + FIN_DAILY_CUTOVER_ +
      '. Установка остановлена (fail-closed) — нужен ручной разбор (сдвиг cutover или очистка).');
  }
  console.log('✓ Preflight cutover: 0 legacy-строк на/после ' + FIN_DAILY_CUTOVER_);
}

function wbFinDailyInitC0() {
  var c = getBqConfig_();
  console.log('C0 init: проект ' + c.projectId + ', датасет ' + c.datasetId +
    ' | версия ' + FIN_DAILY_C0_VERSION_);
  bqEnsureDataset_();

  // Marker сбрасывается ПЕРВЫМ: упавший C0 не оставит валидного маркера (Б3)
  PropertiesService.getScriptProperties().deleteProperty(FIN_DAILY_C0_MARKER_PROP_);

  // 0. Снимок ДО (по текущему V_WB_FINANCE) — программный объект для сверки,
  //    точный СПИСОК КОЛОНОК старого вью и тип RAW.loaded_at
  var before = finDailySnapshotView_(FIN_V_LEGACY_);
  console.log('ДО:    ' + finDailySnapshotStr_(before));
  var oldCols = finDailyViewColumns_(FIN_V_LEGACY_).map(finDailyAssertIdent_);
  if (!oldCols.length) throw new Error('C0: не прочитан список колонок текущего ' + FIN_V_LEGACY_);
  var loadedAtType = finDailyColumnType_(BQ_TABLE_FINANCE_, 'loaded_at');
  console.log('Старый ' + FIN_V_LEGACY_ + ': ' + oldCols.length + ' колонок | RAW.loaded_at: ' +
    loadedAtType + (loadedAtType === 'STRING' ? ' → ORDER BY с SAFE_CAST-нормализацией' : ''));

  // 1. ALTER RAW: новые nullable-колонки (IF NOT EXISTS — идемпотентно)
  finDailyQuery_(
    'ALTER TABLE ' + finDailyTbl_(BQ_TABLE_FINANCE_) + '\n' +
    'ADD COLUMN IF NOT EXISTS report_period STRING,\n' +
    'ADD COLUMN IF NOT EXISTS report_type INT64,\n' +
    'ADD COLUMN IF NOT EXISTS run_id STRING', []);
  console.log('✓ ALTER RAW_WB_FINANCE (+report_period, +report_type, +run_id; raw_json уже был)');

  // 2. Preflight (fail-closed: дальше не идём при нарушении)
  finDailyPreflightCutover_();

  // 3. Таблицы контура
  finDailyQuery_(
    'CREATE TABLE IF NOT EXISTS ' + finDailyTbl_(FIN_T_RUNS_) + ' (\n' +
    '  run_id STRING, started_at TIMESTAMP, finished_at TIMESTAMP, status STRING,\n' +
    '  trigger_type STRING, reports_discovered INT64, reports_loaded INT64,\n' +
    '  reports_errors INT64, requests_made INT64, error_message STRING)', []);
  finDailyQuery_(
    'CREATE TABLE IF NOT EXISTS ' + finDailyTbl_(FIN_T_LOADS_) + ' (\n' +
    '  report_id STRING NOT NULL, report_period STRING, report_type INT64,\n' +
    '  date_from DATE, date_to DATE, status STRING,\n' +
    '  discovered_run_id STRING, processing_run_id STRING, attempt_count INT64,\n' +
    '  discovered_at TIMESTAMP, started_at TIMESTAMP, completed_at TIMESTAMP,\n' +
    '  rows_fetched INT64, rows_loaded INT64,\n' +
    '  list_forpay NUMERIC, list_retail NUMERIC, list_delivery NUMERIC,\n' +
    '  list_storage NUMERIC, list_acceptance NUMERIC, list_deduction NUMERIC,\n' +
    '  list_penalty NUMERIC,\n' +
    '  det_forpay NUMERIC, det_retail NUMERIC, det_delivery NUMERIC,\n' +
    '  det_storage NUMERIC, det_acceptance NUMERIC, det_deduction NUMERIC,\n' +
    '  det_penalty NUMERIC,\n' +
    '  list_metrics_json STRING, error_message STRING)', []);
  finDailyQuery_(
    'CREATE TABLE IF NOT EXISTS ' + finDailyTbl_(FIN_T_WEEK_STATUS_) + ' (\n' +
    '  week_start DATE, report_type INT64, weekly_report_id STRING,\n' +
    '  weekly_final BOOL, finalized_at TIMESTAMP, daily_days_loaded INT64, notes STRING)', []);
  finDailyQuery_(
    'CREATE TABLE IF NOT EXISTS ' + finDailyTbl_(FIN_T_WEEK_RECON_) + ' (\n' +
    '  week_start DATE, report_type INT64, metric STRING,\n' +
    '  sum_daily NUMERIC, sum_weekly NUMERIC, delta NUMERIC,\n' +
    '  recon_status STRING, checked_at TIMESTAMP)', []);
  console.log('✓ Таблицы контура созданы/существуют');

  // 4. __TEST-вью (production НЕ трогаем), программная сверка снапшотов
  //    и schema-эквивалентности — и только при полном совпадении замена
  finDailyCreateViews_('__TEST', oldCols, loadedAtType);
  console.log('✓ Тестовые вью созданы (' + FIN_V_COMPLETE_ + '__TEST и др.)');

  var testLegacy = finDailySnapshotView_(FIN_V_LEGACY_ + '__TEST');
  var testComplete = finDailySnapshotView_(FIN_V_COMPLETE_ + '__TEST');
  var testCanonical = finDailySnapshotView_(FIN_V_CANONICAL_ + '__TEST');
  console.log('TEST:  ' + finDailySnapshotStr_(testLegacy));
  console.log('TEST:  ' + finDailySnapshotStr_(testComplete));
  console.log('TEST:  ' + finDailySnapshotStr_(testCanonical));

  var allDiffs = []
    .concat(finDailySnapshotsEqual_(before, testLegacy).map(function (d) { return 'LEGACY__TEST: ' + d; }))
    .concat(finDailySnapshotsEqual_(before, testComplete).map(function (d) { return 'COMPLETE__TEST: ' + d; }))
    .concat(finDailySnapshotsEqual_(before, testCanonical).map(function (d) { return 'CANONICAL__TEST: ' + d; }));

  // Schema V_WB_FINANCE: ТОЧНОЕ равенство старому списку и порядку колонок
  // (легаси-вью строится явным списком oldCols — никаких хвостов)
  var newCols = finDailyViewColumns_(FIN_V_LEGACY_ + '__TEST');
  if (newCols.length !== oldCols.length) {
    allDiffs.push('SCHEMA: колонок ' + newCols.length + ' вместо ' + oldCols.length);
  } else {
    for (var ci = 0; ci < oldCols.length; ci++) {
      if (newCols[ci] !== oldCols[ci]) {
        allDiffs.push('SCHEMA: колонка #' + (ci + 1) + ' ' + oldCols[ci] + ' → ' + newCols[ci]);
      }
    }
  }
  console.log('Schema V_WB_FINANCE: ' + oldCols.length + ' колонок, тестовый — ' + newCols.length +
    ' (требуется точное совпадение списка и порядка)');

  if (allDiffs.length) {
    console.error('🔴 C0 ОСТАНОВЛЕН (fail-closed). Production-вью НЕ тронуты. C0-marker НЕ установлен.');
    console.error('⚠️ Additive-изменения уже могли быть применены (ALTER-колонки RAW и таблицы ' +
      'контура — это безопасно: nullable / CREATE IF NOT EXISTS; вью не менялись).');
    allDiffs.forEach(function (d) { console.error('   ' + d); });
    console.error('__TEST-вью оставлены для разбора. После разбора: удалить их и повторить C0.');
    throw new Error('C0 FAILED: ' + allDiffs.length + ' расхождений (см. лог)');
  }

  // 5. Полное совпадение → замена production-вью
  finDailyCreateViews_('', oldCols, loadedAtType);
  console.log('✓ Production-вью заменены: ' + FIN_V_COMPLETE_ + ', ' + FIN_V_CANONICAL_ + ', ' +
    FIN_V_LEGACY_ + ' (weekly-only, старый список колонок).');

  // 6. POST-SNAPSHOT ASSERT по production (не только __TEST)
  var after = finDailySnapshotView_(FIN_V_LEGACY_);
  console.log('ПОСЛЕ: ' + finDailySnapshotStr_(after));
  var postDiffs = finDailySnapshotsEqual_(before, after);
  if (postDiffs.length) {
    console.error('🔴 POST-SNAPSHOT ASSERT провален ПОСЛЕ замены production-вью: ' + postDiffs.join('; '));
    console.error('C0-marker НЕ установлен; триггеры поставить нельзя. Разбор вручную.');
    throw new Error('C0 POST-ASSERT FAILED: ' + postDiffs.join('; '));
  }

  // 7. Успех: уборка __TEST (строго, без проглатывания ошибок) → ТОЛЬКО ПОТОМ маркер
  finDailyDropTestViews_();
  PropertiesService.getScriptProperties()
    .setProperty(FIN_DAILY_C0_MARKER_PROP_, FIN_DAILY_C0_VERSION_);
  console.log('✅ C0 OK: инвариант legacy подтверждён программно (тесты и production), ' +
    '__TEST удалены, marker ' + FIN_DAILY_C0_VERSION_ + ' установлен. Лог — в чат.');
}

/** Удаление __TEST-вью. Ошибка НЕ проглатывается: marker ставится только после успеха. */
function finDailyDropTestViews_() {
  var names = [FIN_V_CANONICAL_, FIN_V_LEGACY_, FIN_V_COMPLETE_];
  for (var i = 0; i < names.length; i++) {
    finDailyQuery_('DROP VIEW IF EXISTS ' + finDailyTbl_(names[i] + '__TEST'), []);
  }
}

/**
 * Пересоздание трёх вью (идемпотентно). suffix='' — production, '__TEST' — тестовые.
 * legacyCols — ЯВНЫЙ список колонок старого V_WB_FINANCE (валидированные идентификаторы);
 * loadedAtType — тип RAW.loaded_at: 'STRING' → нормализация в ORDER BY дедупа.
 */
function finDailyCreateViews_(suffix, legacyCols, loadedAtType) {
  suffix = suffix || '';
  var RAW = finDailyTbl_(BQ_TABLE_FINANCE_);
  var LOADS = finDailyTbl_(FIN_T_LOADS_);
  var WEEKS = finDailyTbl_(FIN_T_WEEK_STATUS_);
  var CUT = 'DATE \'' + FIN_DAILY_CUTOVER_ + '\''; // константа файла, не пользовательский ввод

  // ORDER BY дедупа: STRING loaded_at сравнивается как TIMESTAMP (ISO и
  // 'yyyy-MM-dd HH:mm:ss' парсятся SAFE_CAST'ом; непарсимое — в конец),
  // сырой loaded_at и run_id — детерминированные tie-break'и.
  var orderExpr = (String(loadedAtType) === 'STRING')
    ? 'COALESCE(SAFE_CAST(s.loaded_at AS TIMESTAMP), TIMESTAMP("1970-01-01")) DESC, ' +
      's.loaded_at DESC, IFNULL(s.run_id, "") DESC'
    : 's.loaded_at DESC, IFNULL(s.run_id, "") DESC';

  // COMPLETE: две явные ветви (legacy + manifest-confirmed)
  finDailyQuery_(
    'CREATE OR REPLACE VIEW ' + finDailyTbl_(FIN_V_COMPLETE_ + suffix) + ' AS\n' +
    'WITH src AS (\n' +
    '  SELECT r.* FROM ' + RAW + ' r WHERE r.run_id IS NULL\n' +
    '  UNION ALL\n' +
    '  SELECT r.* FROM ' + RAW + ' r\n' +
    '  JOIN ' + LOADS + ' m\n' +
    '    ON r.report_id = m.report_id AND r.run_id = m.processing_run_id\n' +
    '  WHERE m.status = \'COMPLETE\'\n' +
    ')\n' +
    'SELECT * EXCEPT(_rn) FROM (\n' +
    '  SELECT s.*, ROW_NUMBER() OVER (\n' +
    '    PARTITION BY s.report_id, s.rrd_id\n' +
    '    ORDER BY ' + orderExpr + '\n' +
    '  ) AS _rn\n' +
    '  FROM src s\n' +
    '  WHERE s.rrd_id IS NOT NULL AND s.rrd_id != \'\'\n' +
    ')\nWHERE _rn = 1', []);

  // CANONICAL: cutover + замещение daily→weekly по (неделя × reportType)
  finDailyQuery_(
    'CREATE OR REPLACE VIEW ' + finDailyTbl_(FIN_V_CANONICAL_ + suffix) + ' AS\n' +
    'WITH base AS (\n' +
    '  SELECT c.*, DATE_TRUNC(c._rr_date, WEEK(MONDAY)) AS week_start,\n' +
    '         COALESCE(c.report_period, \'WEEKLY\') AS rp\n' +
    '  FROM ' + finDailyTbl_(FIN_V_COMPLETE_ + suffix) + ' c\n' +
    '),\n' +
    'final_weeks AS (\n' +
    '  SELECT week_start, report_type FROM ' + WEEKS + ' WHERE weekly_final = TRUE\n' +
    '),\n' +
    'backfilled_weeks AS (\n' +
    '  SELECT DISTINCT DATE_TRUNC(date_from, WEEK(MONDAY)) AS week_start\n' +
    '  FROM ' + LOADS + '\n' +
    '  WHERE status = \'COMPLETE\' AND report_period = \'WEEKLY\' AND date_from < ' + CUT + '\n' +
    ')\n' +
    'SELECT b.* EXCEPT(rp),\n' +
    '  CASE WHEN b.run_id IS NULL THEN \'LEGACY\'\n' +
    '       WHEN b.rp = \'DAILY\' THEN \'DAILY\' ELSE \'WEEKLY\' END AS source_layer,\n' +
    '  CASE WHEN b.run_id IS NOT NULL AND b.rp = \'DAILY\'\n' +
    '       THEN \'PROVISIONAL\' ELSE \'FINAL\' END AS finance_status\n' +
    'FROM base b\n' +
    'WHERE\n' +
    '  (b.week_start IS NULL AND b.run_id IS NULL)\n' +   // legacy без даты — сохраняем
    '  OR (b.week_start < ' + CUT + ' AND (\n' +
    '       (b.run_id IS NULL\n' +
    '        AND b.week_start NOT IN (SELECT week_start FROM backfilled_weeks))\n' +
    '    OR (b.run_id IS NOT NULL AND b.rp = \'WEEKLY\'\n' +
    '        AND b.week_start IN (SELECT week_start FROM backfilled_weeks))\n' +
    '  ))\n' +
    '  OR (b.week_start >= ' + CUT + ' AND b.run_id IS NOT NULL AND (\n' +
    '       b.rp = \'WEEKLY\'\n' +
    '    OR (b.rp = \'DAILY\' AND NOT EXISTS (\n' +
    '          SELECT 1 FROM final_weeks f\n' +
    '          WHERE f.week_start = b.week_start AND f.report_type = b.report_type))\n' +
    '  ))', []);

  // V_WB_FINANCE (легаси-имя): WEEKLY-слой поверх COMPLETE — прежняя семантика
  // «недельный канон» для существующих читателей, но без daily-задвоения и
  // без утечки частичных попыток (манифест-гейт наследуется от COMPLETE).
  // Легаси-имя: ЯВНЫЙ старый список колонок (не SELECT *) — существующие
  // читатели получают в точности прежнюю схему; новые служебные колонки
  // (report_period/report_type/run_id) наружу НЕ выставляются.
  var colList = '*';
  if (legacyCols && legacyCols.length) {
    colList = legacyCols.map(finDailyAssertIdent_).join(', ');
  }
  finDailyQuery_(
    'CREATE OR REPLACE VIEW ' + finDailyTbl_(FIN_V_LEGACY_ + suffix) + ' AS\n' +
    'SELECT ' + colList + ' FROM ' + finDailyTbl_(FIN_V_COMPLETE_ + suffix) + '\n' +
    'WHERE COALESCE(report_period, \'WEEKLY\') = \'WEEKLY\'', []);
}


// ═══════════════ СТАТУС / SELF-TEST (read-only) ═══════════════

function wbFinDailyStatus() {
  var q1 = finDailyQuery_(
    'SELECT status, COUNT(*) AS n FROM ' + finDailyTbl_(FIN_T_LOADS_) +
    ' GROUP BY status ORDER BY status', []);
  console.log('── manifest по статусам ──');
  finDailyRowsToObjs_(q1).forEach(function (o) { console.log('  ' + o.status + ': ' + o.n); });

  var q2 = finDailyQuery_(
    'SELECT COUNT(*) AS c, COUNT(DISTINCT report_id) AS d FROM ' + finDailyTbl_(FIN_T_LOADS_), []);
  var o2 = finDailyRowsToObjs_(q2)[0];
  console.log('Логический PK: COUNT=' + o2.c + ' | DISTINCT report_id=' + o2.d +
    (String(o2.c) === String(o2.d) ? ' ✓' : ' 🔴 НАРУШЕН'));

  var q3 = finDailyQuery_(
    'SELECT CAST(week_start AS STRING) AS ws, report_type, weekly_final,\n' +
    '  weekly_report_id, daily_days_loaded\n' +
    'FROM ' + finDailyTbl_(FIN_T_WEEK_STATUS_) + ' ORDER BY ws DESC, report_type LIMIT 12', []);
  console.log('── недели (последние) ──');
  finDailyRowsToObjs_(q3).forEach(function (o) {
    console.log('  ' + o.ws + ' t' + o.report_type + ': ' +
      (o.weekly_final === 'true' ? 'FINAL (' + o.weekly_report_id + ', daily ' + o.daily_days_loaded + ')' : 'PROVISIONAL'));
  });

  var q4 = finDailyQuery_(
    'SELECT run_id, status, CAST(started_at AS STRING) AS s,\n' +
    '  reports_loaded, reports_errors, requests_made\n' +
    'FROM ' + finDailyTbl_(FIN_T_RUNS_) + ' ORDER BY started_at DESC LIMIT 5', []);
  console.log('── прогоны (последние) ──');
  finDailyRowsToObjs_(q4).forEach(function (o) {
    console.log('  ' + o.run_id + ' ' + o.status + ' @' + o.s +
      ' (loaded ' + o.reports_loaded + ', err ' + o.reports_errors + ', req ' + o.requests_made + ')');
  });

  var q5 = finDailyQuery_(
    'SELECT CAST(week_start AS STRING) AS ws, report_type, metric,\n' +
    '  CAST(delta AS STRING) AS delta, recon_status\n' +
    'FROM ' + finDailyTbl_(FIN_T_WEEK_RECON_) + '\n' +
    'WHERE recon_status != \'OK\' ORDER BY ws DESC LIMIT 10', []);
  var warns = finDailyRowsToObjs_(q5);
  console.log('── RECON WARN ──');
  if (!warns.length) console.log('  нет ✓');
  warns.forEach(function (o) {
    console.log('  ' + o.ws + ' t' + o.report_type + ' ' + o.metric + ' Δ=' + o.delta);
  });
}


// ═══════════════ ТРИГГЕРЫ (ставит владелец ПОСЛЕ приёмки) ═══════════════

/** 3 тика/день: ~07:30 (пробное время появления daily, НЕ SLA), ~12:30, ~18:30 МСК.
 *  Fail-closed: таймзона проекта + preflight cutover. */
function wbFinInstallDailyTriggers() {
  var tz = Session.getScriptTimeZone();
  if (tz !== 'Europe/Moscow') {
    throw new Error('Требуется timezone проекта Europe/Moscow (сейчас: ' + tz + ').');
  }
  // Fail-closed (Б3): C0-marker нужной версии + наличие production-объектов
  var marker = PropertiesService.getScriptProperties().getProperty(FIN_DAILY_C0_MARKER_PROP_);
  if (marker !== FIN_DAILY_C0_VERSION_) {
    throw new Error('C0-marker отсутствует/не той версии (' + marker + ' != ' +
      FIN_DAILY_C0_VERSION_ + '). Сначала успешный wbFinDailyInitC0().');
  }
  var missing = finDailyMissingObjects_([
    FIN_T_RUNS_, FIN_T_LOADS_, FIN_T_WEEK_STATUS_, FIN_T_WEEK_RECON_,
    FIN_V_COMPLETE_, FIN_V_CANONICAL_, FIN_V_LEGACY_
  ]);
  if (missing.length) {
    throw new Error('Отсутствуют production-объекты: ' + missing.join(', ') +
      '. Повторите wbFinDailyInitC0().');
  }
  finDailyPreflightCutover_(); // fail-closed до установки

  wbFinRemoveDailyTriggers();
  var hours = [7, 12, 18];
  for (var i = 0; i < hours.length; i++) {
    ScriptApp.newTrigger(FIN_DAILY_TRIGGER_FN_).timeBased()
      .everyDays(1).atHour(hours[i]).nearMinute(30).create();
  }
  console.log('✅ Триггеры FinanceDaily созданы: ~07:30, ~12:30, ~18:30 МСК');
  return { created: hours.length };
}

function wbFinRemoveDailyTriggers() {
  var trs = ScriptApp.getProjectTriggers(), n = 0;
  for (var i = 0; i < trs.length; i++) {
    if (trs[i].getHandlerFunction() === FIN_DAILY_TRIGGER_FN_) {
      ScriptApp.deleteTrigger(trs[i]);
      n++;
    }
  }
  if (n) console.log('🗑 Удалено триггеров FinanceDaily: ' + n);
  return { removed: n };
}


// ═══════════════ DEEP BACKFILL (ручной; только регистрация) ═══════════════

var WB_FIN_BACKFILL_FROM_ = ''; // 'YYYY-MM-DD' — заполнить перед запуском
var WB_FIN_BACKFILL_TO_   = ''; // 'YYYY-MM-DD'

/**
 * Регистрирует WEEKLY-отчёты периода в очередь (DISCOVERED), не грузит.
 * Загрузка — обычными тиками runWbFinanceDaily()/wbFinDailyRunManual().
 * Для недель < cutover действует приоритет canonical (manifest > legacy).
 */
function wbFinDeepBackfillRegister() {
  if (!finDailyIsDate_(WB_FIN_BACKFILL_FROM_) || !finDailyIsDate_(WB_FIN_BACKFILL_TO_)) {
    console.log('❌ Заполните WB_FIN_BACKFILL_FROM_/_TO_ (YYYY-MM-DD)');
    return;
  }
  // Тот же общий ScriptLock на весь прогон, что и у тика: backfill не может
  // пересечься с runWbFinanceDaily (и его пейсер-резервациями).
  var lock = LockService.getScriptLock();
  if (!lock.tryLock(FIN_DAILY_LOCK_WAIT_MS_)) {
    console.log('❌ ScriptLock занят (идёт другой прогон) — повторите позже');
    return;
  }
  try {
    var st = { t0: Date.now(), lastReqAt: 0, requests: 0, runId: finDailyNewRunId_(), token: finDailyToken_() };
    if (!st.token) { console.log('❌ Нет токена WB_TOKEN_FINANCE'); return; }
    if (PropertiesService.getScriptProperties().getProperty(FIN_DAILY_C0_MARKER_PROP_) !== FIN_DAILY_C0_VERSION_) {
      console.log('❌ C0-marker отсутствует — сначала wbFinDailyInitC0()');
      return;
    }
    finDailyRunOpen_(st.runId, 'BACKFILL');

    var wl = finDailyList_(st, 'weekly', WB_FIN_BACKFILL_FROM_, WB_FIN_BACKFILL_TO_);
    if (!wl.ok) {
      console.error('backfill list: ' + wl.error);
      finDailyRunClose_(st.runId, 'ERROR', { discovered: 0, loaded: 0, errors: 1, requests: st.requests }, wl.error);
      return;
    }
    var reg = finDailyRegisterReports_(st, wl.reports, 'WEEKLY', {});
    if (reg.anomalies.length) console.error('🔴 IMMUTABLE_ANOMALY: ' + reg.anomalies.join('; '));
    finDailyRunClose_(st.runId,
      reg.anomalies.length ? 'ERROR' : 'OK',
      { discovered: reg.registered, loaded: 0, errors: reg.anomalies.length ? 1 : 0, requests: st.requests },
      reg.anomalies.join('; '));
    console.log('Backfill: найдено ' + wl.reports.length + ', зарегистрировано ' + reg.registered +
      '. Грузите тиками wbFinDailyRunManual().');
  } finally {
    lock.releaseLock();
  }
}
