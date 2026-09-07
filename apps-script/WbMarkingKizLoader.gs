/**
 * ══════════════════════════════════════════════════════════════
 * EVETIS WB — WbMarkingKizLoader.gs   (PR-Kiz1, загрузчик)
 *
 * ЧТО ДЕЛАЕТ. Скачивает все документы категории «УПД по маркировке» из
 * API документов WB, вынимает из XML коды маркировки и кладёт их в
 * BigQuery в таблицу `RAW_WB_KIZ_SUPPLY`. Один код = одна строка,
 * привязанная к номеру поставки.
 *
 * ЗАЧЕМ. До сих пор ответа на вопрос «какие КИЗ уехали на WB» не было
 * нигде: `supplies-api` отдаёт только количества, а списки кодов
 * приходилось качать из ЛК по одной поставке руками.
 *
 * ЧЕМ ПОДТВЕРЖДЁН ИСТОЧНИК. Проба №2 (24.08.2026) сверила XML с ручными
 * выгрузками владельца: поставка 41047776 → 129 кодов = 129 ожидаемых,
 * поставка 41047801 → 125 = 125. Совпадение точное, не «примерно».
 *
 * 🔴 ПРАВИЛА ЭТОГО ЗАГРУЗЧИКА
 *
 * 1. **Идемпотентность через пропуск.** Таблица наполняется WRITE_APPEND,
 *    поэтому повторный прогон задвоил бы коды. Перед работой загрузчик
 *    читает из BQ уже загруженные `supply_id` и пропускает их. Чтобы
 *    перезалить поставку намеренно, есть `runWbMarkingKizReload(supplyId)`.
 *
 * 2. **Ошибка одного документа не отменяет остальные, но и не молчит.**
 *    Сбой скачивания/разбора помечает поставку как `DOC_FAILED`, она НЕ
 *    пишется в BQ и попадает в итоговый список провалов. Пустой результат
 *    разбора никогда не считается «в поставке нет кодов».
 *
 * 3. **Счёт печатается всегда.** По каждой поставке — сколько кодов
 *    найдено; по контрольным 41047776 и 41047801 — сверка со 129 и 125.
 *    Расхождение печатается как 🔴, а не проглатывается.
 *
 * 4. **429 — это темп, а не ошибка.** Пауза берётся из заголовка
 *    `X-RateLimit-Retry`, как просит шлюз.
 *
 * ЧТО НУЖНО. Токен в `WB_TOKEN_DOCUMENTS` (категория «Документы»).
 * Конфиг BigQuery — общий проектный (`getBqConfig_`), датасет `wb_raw`.
 *
 * КАК ЗАПУСКАТЬ. `runWbMarkingKizLoad` → Run → смотреть Execution log.
 * Первый прогон создаст таблицу сам.
 * ══════════════════════════════════════════════════════════════
 */

var WBKIZ_HOST_ = 'https://documents-api.wildberries.ru';
var WBKIZ_TOKEN_KEY_ = 'WB_TOKEN_DOCUMENTS';
var WBKIZ_CATEGORY_ = 'UPD po markirovke';
var WBKIZ_BEGIN_ = '2025-08-01';
var WBKIZ_TABLE_ = 'RAW_WB_KIZ_SUPPLY';

var WBKIZ_GTIN_PREFIX_ = '0104619689656';
var WBKIZ_PAGE_LIMIT_ = 50;
var WBKIZ_MAX_PAGES_ = 60;
var WBKIZ_PAUSE_MS_ = 1200;
var WBKIZ_MAX_ATTEMPTS_ = 4;

/** Контрольные величины из ручных выгрузок ЛК (проба №2 их подтвердила). */
var WBKIZ_EXPECTED_ = { '41047776': 129, '41047801': 125 };

/* ────────────────────────────── точки входа ────────────────────────────── */

/** Обычный прогон: грузит всё, чего ещё нет в BQ. */
function runWbMarkingKizLoad() {
  wbkizRun_(null);
}

/**
 * Полная перезаливка: чистит таблицу и грузит всё заново.
 * Нужна после правки разбора — например, после фикса числовых XML-сущностей
 * (v1.1), из-за которого 223 кода лежали битыми.
 */
function runWbMarkingKizReloadAll() {
  var c = getBqConfig_();
  wbkizLog_('🔴 ПОЛНАЯ ПЕРЕЗАЛИВКА: удаляю все строки ' + WBKIZ_TABLE_ + ' …');
  bqQuery_('DELETE FROM `' + c.projectId + '.' + c.datasetId + '.' + WBKIZ_TABLE_ +
           '` WHERE TRUE');
  wbkizLog_('Таблица очищена. Гружу заново.');
  wbkizRun_(null);
}

/** Принудительная перезаливка одной поставки (сначала удаляет её строки). */
function runWbMarkingKizReload(supplyId) {
  var sid = String(supplyId || '').trim();
  if (!sid) { wbkizLog_('❌ Укажи номер поставки: runWbMarkingKizReload("41047776")'); return; }
  var c = getBqConfig_();
  wbkizLog_('Удаляю прежние строки поставки ' + sid + ' …');
  bqQuery_('DELETE FROM `' + c.projectId + '.' + c.datasetId + '.' + WBKIZ_TABLE_ +
           '` WHERE supply_id = ' + sid);
  wbkizRun_(sid);
}

/* ────────────────────────────── основной ход ────────────────────────────── */

function wbkizRun_(onlySupplyId) {
  var t0 = new Date();
  var runId = 'KIZ_' + Utilities.formatDate(t0, 'UTC', 'yyyyMMdd_HHmmss') + '_' +
              Utilities.getUuid().substring(0, 8);
  var snapshotTs = t0.toISOString();

  wbkizLog_('═══════════════════════════════════════════════════');
  wbkizLog_('ЗАГРУЗКА КИЗ ИЗ «УПД ПО МАРКИРОВКЕ»');
  wbkizLog_('run_id: ' + runId);
  wbkizLog_('═══════════════════════════════════════════════════');

  var token = String(PropertiesService.getScriptProperties().getProperty(WBKIZ_TOKEN_KEY_) || '').trim();
  if (!token) { wbkizLog_('❌ ОСТАНОВ: нет ключа ' + WBKIZ_TOKEN_KEY_ + ' в Script Properties.'); return; }

  wbkizEnsureTable_();

  var already = onlySupplyId ? {} : wbkizLoadedSupplies_();
  var alreadyN = 0;
  for (var q in already) alreadyN++;
  wbkizLog_('Уже в BigQuery поставок: ' + alreadyN);

  var docs = wbkizListDocs_(token);
  wbkizLog_('Документов «УПД по маркировке»: ' + docs.length);
  wbkizLog_('');

  var okList = [], skipList = [], failList = [];
  var totalRows = 0;

  for (var i = 0; i < docs.length; i++) {
    var doc = docs[i];
    var sn = String(doc.serviceName || '');
    var m = sn.match(/(\d{6,})\s*$/);
    if (!m) { failList.push(sn + ' (нет номера поставки в serviceName)'); continue; }
    var sid = m[1];

    if (onlySupplyId && sid !== onlySupplyId) continue;
    if (!onlySupplyId && already[sid]) { skipList.push(sid); continue; }

    var parsed = wbkizFetchAndParse_(token, doc, sid);
    if (parsed === null) { failList.push(sid + ' (DOC_FAILED)'); continue; }
    if (!parsed.length) { failList.push(sid + ' (разобрано 0 кодов — НЕ считаем это пустой поставкой)'); continue; }

    var rows = [];
    var badLen = 0;
    for (var k = 0; k < parsed.length; k++) {
      var p = parsed[k];
      if (p.badLen) badLen++;
      rows.push({
        snapshot_ts: snapshotTs,
        run_id: runId,
        supply_id: parseInt(sid, 10),
        kiz: p.kiz,
        gtin: p.gtin,
        serial: p.serial,
        position_index: p.pos,
        source_document: sn,
        doc_created_at: String(doc.creationTime || '') || null,
        row_hash: Utilities.base64Encode(
          Utilities.computeDigest(Utilities.DigestAlgorithm.MD5, sid + '|' + p.kiz)),
        _load_date: Utilities.formatDate(t0, 'UTC', 'yyyy-MM-dd')
      });
    }

    try {
      bqLoadRows_(WBKIZ_TABLE_, rows);
      totalRows += rows.length;
      okList.push({ sid: sid, n: rows.length, badLen: badLen });
      wbkizLog_('  ✅ ' + sid + ' → ' + rows.length + ' кодов' + wbkizCheck_(sid, rows.length) +
                (badLen ? '   🔴 длина != 24 у ' + badLen + ' кодов — РАЗБИРАТЬ' : ''));
    } catch (e) {
      failList.push(sid + ' (BQ load: ' + e + ')');
      wbkizLog_('  ❌ ' + sid + ' — ошибка записи в BQ: ' + e);
    }
    Utilities.sleep(WBKIZ_PAUSE_MS_);
  }

  wbkizLog_('');
  wbkizLog_('───── ИТОГ ─────');
  wbkizLog_('Загружено поставок : ' + okList.length);
  wbkizLog_('Загружено кодов    : ' + totalRows);
  wbkizLog_('Пропущено (уже в BQ): ' + skipList.length);
  wbkizLog_('Провалов           : ' + failList.length);
  if (failList.length) {
    wbkizLog_('🔴 Провалы:');
    for (var f = 0; f < failList.length; f++) wbkizLog_('   ' + failList[f]);
  }
  var secs = Math.round((new Date() - t0) / 1000);
  wbkizLog_('');
  wbkizLog_('Готово за ' + secs + ' с. Таблица: ' + WBKIZ_TABLE_);
  wbkizLog_('═══════════════════════════════════════════════════');
}

function wbkizCheck_(sid, n) {
  if (!(sid in WBKIZ_EXPECTED_)) return '';
  var exp = WBKIZ_EXPECTED_[sid];
  return n === exp ? '   ✅ сверка: ' + n + ' = ' + exp
                   : '   🔴 СВЕРКА НЕ СОШЛАСЬ: ' + n + ' против ' + exp;
}

/* ────────────────────────────── разбор XML ────────────────────────────── */

/**
 * Скачивает документ и вынимает коды. Возвращает массив {kiz,gtin,serial,pos}
 * либо null при сбое. Пустой массив — это НЕ «кодов нет», а повод для разбора.
 */
function wbkizFetchAndParse_(token, doc, sid) {
  var ext = (doc.extensions && doc.extensions.length) ? String(doc.extensions[0]) : 'xml';
  var url = WBKIZ_HOST_ + '/api/v1/documents/download'
    + '?serviceName=' + encodeURIComponent(String(doc.serviceName || ''))
    + '&extension=' + encodeURIComponent(ext);

  var r = wbkizFetch_('get', url, token);
  if (!r.ok) { wbkizLog_('  ❌ ' + sid + ' — HTTP ' + r.code + ' ' + wbkizCut_(r.text, 160)); return null; }

  var body = r.json && (r.json.data || r.json);
  var b64 = body ? String(body.document || '') : '';
  if (!b64) { wbkizLog_('  ❌ ' + sid + ' — пустое тело документа'); return null; }

  var xml = wbkizDecode_(b64);
  if (!xml) { wbkizLog_('  ❌ ' + sid + ' — не удалось декодировать'); return null; }

  return wbkizParseKiz_(xml);
}

/** 🔴 Кодировка документа — windows-1251, не UTF-8. */
function wbkizDecode_(b64) {
  var bytes = Utilities.base64Decode(b64);
  var charsets = ['windows-1251', 'Windows-1251', 'cp1251', 'UTF-8'];
  for (var i = 0; i < charsets.length; i++) {
    try {
      var s = Utilities.newBlob(bytes).getDataAsString(charsets[i]);
      if (s && s.indexOf('<') >= 0) return s;
    } catch (e) { /* следующая */ }
  }
  return null;
}

/**
 * Вынимает коды из тегов `КИЗ`. Позиция считается по блокам `СведТов`,
 * чтобы сохранить связь «код → товарная строка документа».
 *
 * 🔴 Амперсанд в кодах приходит как `&amp;` — раскодируем XML-сущности,
 * иначе код `…215DF4&W` уедет в BQ как `…215DF4&amp;W` и не совпадёт
 * ни с выгрузкой ЧЗ, ни с файлами фулфилмента.
 */
function wbkizParseKiz_(xml) {
  var out = [];
  var seen = {};
  var blocks = xml.split(/<СведТов[\s>]/);
  for (var b = 1; b < blocks.length; b++) {
    var re = /<КИЗ>([^<]*)<\/КИЗ>/g;
    var m;
    while ((m = re.exec(blocks[b])) !== null) {
      var code = wbkizUnescape_(m[1]);
      if (!code || code.indexOf(WBKIZ_GTIN_PREFIX_) !== 0) continue;
      if (seen[code]) continue;
      seen[code] = 1;
      // 🔴 Сторож длины: КИ = 01 + GTIN(14) + 21 + серийник(6) = ровно 24.
      // Именно он вскрыл дефект с числовыми сущностями. Не убирать.
      out.push({
        kiz: code, gtin: code.substring(2, 16), serial: code.substring(18),
        pos: b, badLen: (code.length !== 24)
      });
    }
  }
  return out;
}

/**
 * 🔴 ДЕФЕКТ v1.0, найден на первом же прогоне 24.08.2026 и исправлен здесь.
 *
 * Раскодировались только именованные сущности. А WB отдаёт коды и с
 * ЧИСЛОВЫМИ: `&#39;` (апостроф) и `&#34;` (кавычка). В результате 223 кода
 * из 3 068 уехали в BQ битыми — длиной 28 и 32 символа вместо 24:
 *
 *     0104619689656017215&#39;StcU        вместо  0104619689656017215'StcU
 *     0104619689656017215dqX&#39;&#34;   вместо  0104619689656017215dqX'"
 *
 * Дефект обнаружен контролем длины КИ (должна быть ровно 24), а не глазами.
 * Тот же класс ошибки, что и история с энзимной пудрой: код, отличающийся
 * на символ, — это ДРУГОЙ код, и он не совпадёт ни с выгрузкой ЧЗ, ни с
 * файлами фулфилмента.
 *
 * Порядок замен важен: числовые сущности → именованные → `&amp;` последним.
 * Иначе `&amp;#39;` схлопнется в апостроф вместо литерала `&#39;`.
 */
function wbkizUnescape_(s) {
  return String(s || '')
    .replace(/&#x([0-9a-fA-F]+);/g, function (_, h) { return String.fromCharCode(parseInt(h, 16)); })
    .replace(/&#(\d+);/g,           function (_, d) { return String.fromCharCode(parseInt(d, 10)); })
    .replace(/&lt;/g, '<').replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"').replace(/&apos;/g, "'")
    .replace(/&amp;/g, '&')   // амперсанд последним — иначе двойное раскодирование
    .trim();
}

/* ─────────────────────────── список документов ─────────────────────────── */

function wbkizListDocs_(token) {
  var out = [], offset = 0;
  for (var page = 0; page < WBKIZ_MAX_PAGES_; page++) {
    var url = WBKIZ_HOST_ + '/api/v1/documents/list'
      + '?beginTime=' + encodeURIComponent(WBKIZ_BEGIN_)
      + '&endTime=' + encodeURIComponent(wbkizToday_())
      + '&category=' + encodeURIComponent(WBKIZ_CATEGORY_)
      + '&limit=' + WBKIZ_PAGE_LIMIT_ + '&offset=' + offset + '&locale=ru';
    var r = wbkizFetch_('get', url, token);
    if (!r.ok) { wbkizLog_('❌ список, offset=' + offset + ': HTTP ' + r.code); break; }
    var rows = wbkizPick_(r.json, ['data.documents', 'documents', 'data']);
    if (!rows) break;
    out = out.concat(rows);
    if (rows.length < WBKIZ_PAGE_LIMIT_) break;
    offset += WBKIZ_PAGE_LIMIT_;
    Utilities.sleep(WBKIZ_PAUSE_MS_);
  }
  return out;
}

/* ──────────────────────────── BigQuery ──────────────────────────── */

function wbkizEnsureTable_() {
  var c = getBqConfig_();
  try {
    BigQuery.Tables.get(c.projectId, c.datasetId, WBKIZ_TABLE_);
    return false;
  } catch (e) {
    BigQuery.Tables.insert({
      tableReference: { projectId: c.projectId, datasetId: c.datasetId, tableId: WBKIZ_TABLE_ },
      schema: { fields: [
        { name: 'snapshot_ts',     type: 'TIMESTAMP', mode: 'REQUIRED' },
        { name: 'run_id',          type: 'STRING',    mode: 'REQUIRED' },
        { name: 'supply_id',       type: 'INTEGER',   mode: 'REQUIRED' },
        { name: 'kiz',             type: 'STRING',    mode: 'REQUIRED' },
        { name: 'gtin',            type: 'STRING' },
        { name: 'serial',          type: 'STRING' },
        { name: 'position_index',  type: 'INTEGER' },
        { name: 'source_document', type: 'STRING' },
        { name: 'doc_created_at',  type: 'TIMESTAMP' },
        { name: 'row_hash',        type: 'STRING' },
        { name: '_load_date',      type: 'DATE',      mode: 'REQUIRED' }
      ]},
      timePartitioning: { type: 'DAY', field: '_load_date' },
      clustering: { fields: ['supply_id', 'gtin'] }
    }, c.projectId, c.datasetId);
    wbkizLog_('✅ Таблица создана: ' + c.datasetId + '.' + WBKIZ_TABLE_);
    return true;
  }
}

/** Какие поставки уже лежат в BQ — чтобы не задваивать при повторе. */
function wbkizLoadedSupplies_() {
  var c = getBqConfig_();
  var map = {};
  try {
    var res = bqQuery_('SELECT DISTINCT CAST(supply_id AS STRING) AS s FROM `' +
                       c.projectId + '.' + c.datasetId + '.' + WBKIZ_TABLE_ + '`');
    var rows = res.rows || [];
    for (var i = 0; i < rows.length; i++) map[rows[i].f[0].v] = 1;
  } catch (e) {
    wbkizLog_('(таблица пуста или только что создана — грузим всё)');
  }
  return map;
}

/* ──────────────────────────── инфраструктура ──────────────────────────── */

function wbkizFetch_(method, url, token) {
  var last = { ok: false, code: 0, text: '', json: null };
  for (var attempt = 0; attempt < WBKIZ_MAX_ATTEMPTS_; attempt++) {
    var res;
    try {
      res = UrlFetchApp.fetch(url, {
        method: method, muteHttpExceptions: true, headers: { 'Authorization': token }
      });
    } catch (e) {
      last = { ok: false, code: -1, text: String(e), json: null };
      Utilities.sleep(2000 * (attempt + 1));
      continue;
    }
    var code = res.getResponseCode();
    var text = res.getContentText();
    if (code === 429) {
      var wait = 5;
      try {
        var h = res.getAllHeaders();
        var v = h['X-RateLimit-Retry'] || h['x-ratelimit-retry'] || h['Retry-After'] || h['retry-after'];
        if (v) wait = parseInt(String(v), 10) || 5;
      } catch (e2) { /* дефолт */ }
      wait = Math.min(Math.max(wait, 1), 60);
      wbkizLog_('   ⏳ 429: жду ' + wait + ' с…');
      Utilities.sleep(wait * 1000 + 500);
      continue;
    }
    if (code >= 500) {
      last = { ok: false, code: code, text: text, json: null };
      Utilities.sleep(2000 * (attempt + 1));
      continue;
    }
    var json = null;
    try { json = JSON.parse(text); } catch (e3) { json = null; }
    return { ok: code >= 200 && code < 300, code: code, text: text, json: json };
  }
  return last;
}

function wbkizPick_(obj, paths) {
  if (!obj) return null;
  for (var i = 0; i < paths.length; i++) {
    var cur = obj, parts = paths[i].split('.'), ok = true;
    for (var j = 0; j < parts.length; j++) {
      if (cur && typeof cur === 'object' && parts[j] in cur) cur = cur[parts[j]];
      else { ok = false; break; }
    }
    if (ok && cur && cur.length !== undefined) return cur;
  }
  return null;
}

function wbkizToday_() {
  return Utilities.formatDate(new Date(), 'Europe/Moscow', 'yyyy-MM-dd');
}

function wbkizCut_(s, n) {
  s = String(s || '').replace(/\s+/g, ' ');
  return s.length > n ? s.substring(0, n) + '…' : s;
}

function wbkizLog_(msg) {
  Logger.log(msg);
}
