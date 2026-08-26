/**
 * ══════════════════════════════════════════════════════════════
 * EVETIS WB — WbMarkingUpdProbe.gs   (проба №2, только чтение)
 *
 * ЗАЧЕМ. Проба №1 (`WbDocumentsProbe.gs`, прогон 24.08 16:25) нашла источник:
 * в API документов есть категория **`UPD po markirovke` / «УПД по маркировке»**,
 * и её документы привязаны к поставке через `serviceName`:
 *
 *     UPD po markirovke-41047776  →  «Упд по акту приемки №41047776.xml»
 *
 * Все пять контрольных поставок июля опознались 5 из 5. Формат — XML в
 * кодировке **windows-1251**, ~16 КБ на поставку 41047776.
 *
 * Но проба №1 НЕ установила главного: **лежат ли внутри этого XML сами коды
 * маркировки**. 16 КБ достаточно и для списка из 129 КИЗ, и для документа
 * вообще без кодов. Пока это не проверено, строить загрузчик нельзя.
 *
 * 🔴 ГЛАВНОЕ ПРАВИЛО ЭТОЙ ПРОБЫ
 *
 * Наличие кодов проверяется **счётом против известной величины**, а не на
 * глаз. По поставке 41047776 у владельца есть ручная выгрузка из ЛК:
 * там ровно **129 КИЗ**, по 41047801 — **125**. Если XML отдаёт столько же —
 * источник подтверждён. Если меньше, больше или ноль — источник НЕ подтверждён,
 * и это фиксируется как отрицательный результат, а не «примерно сходится».
 *
 * Второе правило: структура XML не угадывается. Проба печатает фактический
 * перечень тегов с частотами и ищет коды по префиксу GTIN, а не по
 * предполагаемому имени элемента. Схема ФНС может назвать поле как угодно.
 *
 * Третье правило: 429 — это не ошибка данных, а темп. Проба №1 упёрлась в
 * `rate limit exceeded` на offset=400. Здесь пагинация читает заголовок
 * `X-RateLimit-Retry` и ждёт столько, сколько просит шлюз.
 *
 * ЧТО УСТАНАВЛИВАЕТ ПРОБА
 *   1. Сколько всего документов «УПД по маркировке» доступно за период
 *      маркировки и по каким поставкам.
 *   2. Покрытие: для скольких из 63 поставок с `needKiz = true` документ
 *      есть, а для скольких его нет. Список непокрытых печатается поимённо —
 *      именно они станут ручной работой.
 *   3. Есть ли внутри XML коды маркировки: перечень тегов, число строк с
 *      префиксом GTIN, три образца.
 *   4. Сходится ли счёт по 41047776 со 129 и по 41047801 со 125.
 *   5. Видно ли в документе, подписан ли УПД и кем — это отвечает на вопрос
 *      «передавались ли коды на баланс WB».
 *
 * ЧЕГО ПРОБА НЕ ДЕЛАЕТ
 *   • не пишет ни в лист, ни в BigQuery;
 *   • не скачивает больше двух документов;
 *   • не печатает токен; коды печатает только тремя образцами.
 *
 * КАК ЗАПУСКАТЬ. Выбрать `wbMarkUpdProbeRun` → Run → смотреть Execution log.
 * Нужен токен в `WB_TOKEN_DOCUMENTS` (создан 24.08, доступ подтверждён).
 * ══════════════════════════════════════════════════════════════
 */

var WBMU_HOST_ = 'https://documents-api.wildberries.ru';
var WBMU_TOKEN_KEY_ = 'WB_TOKEN_DOCUMENTS';
var WBMU_CATEGORY_ = 'UPD po markirovke';
var WBMU_BEGIN_ = '2025-08-01';

var WBMU_PAGE_LIMIT_ = 50;
var WBMU_MAX_PAGES_ = 60;
var WBMU_PAUSE_MS_ = 1200;      // база; при 429 ждём столько, сколько скажет шлюз
var WBMU_MAX_ATTEMPTS_ = 4;

/** Префикс всех наших GTIN — по нему опознаём код маркировки в любом теге. */
var WBMU_GTIN_PREFIX_ = '0104619689656';

/** Контрольные величины из ручных выгрузок ЛК. */
var WBMU_EXPECTED_ = { '41047776': 129, '41047801': 125 };

/**
 * 63 поставки с `needKiz = true` и принятым количеством > 0.
 * Источник: V_WB_SUPPLIES_GOODS_CURRENT (BigQuery), срез 24.08.2026.
 */
var WBMU_KIZ_SUPPLIES_ = [
  '41047801','41047776','41037541','41039644','41039407','40321392','40322208',
  '39717509','39803829','39775737','39578843','39400464','39177144','39082588',
  '39078237','39082894','39024769','38762291','38761951','38527290','38508885',
  '38227942','38436358','38229326','38145272','37925770','37925186','37569790',
  '37587336','37587114','37726537','37316783','37315202','37211478','37211426',
  '36759493','36282794','36248475','36247684','36246900','35744184','35576731',
  '35116175','35023072','34815252','34726774','34727043','34726669','34731000',
  '34236366','34464977','34236331','34236347','33920464','33697709','33713730',
  '33713835','33713549','33681688','32735481','31973654','31973577','31872022'
];

/* ────────────────────────────── точка входа ────────────────────────────── */

function wbMarkUpdProbeRun() {
  var t0 = new Date();
  wbmuLog_('═══════════════════════════════════════════════════');
  wbmuLog_('ПРОБА №2 — «УПД ПО МАРКИРОВКЕ». Только чтение.');
  wbmuLog_('Запуск: ' + t0.toISOString());
  wbmuLog_('═══════════════════════════════════════════════════');

  var token = String(PropertiesService.getScriptProperties().getProperty(WBMU_TOKEN_KEY_) || '').trim();
  if (!token) {
    wbmuLog_('❌ ОСТАНОВ: в Script Properties нет ключа ' + WBMU_TOKEN_KEY_ + '.');
    return;
  }
  wbmuLog_('Токен: ' + WBMU_TOKEN_KEY_ + ' (значение не печатается)');
  wbmuLog_('Период: ' + WBMU_BEGIN_ + ' … ' + wbmuToday_());
  wbmuLog_('');

  var docs = wbmuStep1List_(token);
  var bySupply = wbmuStep2Coverage_(docs);
  wbmuStep3Content_(token, bySupply);

  var secs = Math.round((new Date() - t0) / 1000);
  wbmuLog_('');
  wbmuLog_('═══════════════════════════════════════════════════');
  wbmuLog_('ПРОБА ЗАВЕРШЕНА за ' + secs + ' с. Записей не производилось.');
  wbmuLog_('═══════════════════════════════════════════════════');
}

/* ──────────────── шаг 1: все документы категории маркировки ──────────────── */

function wbmuStep1List_(token) {
  wbmuLog_('───── ШАГ 1. Документы категории «' + WBMU_CATEGORY_ + '» ─────');

  var docs = wbmuListWithCategory_(token, WBMU_CATEGORY_);
  if (docs === null) {
    wbmuLog_('⚠️ Фильтр по категории не сработал — читаю весь список и фильтрую на своей стороне.');
    var all = wbmuListWithCategory_(token, null);
    docs = [];
    if (all) {
      for (var i = 0; i < all.length; i++) {
        var sn = String(all[i].serviceName || '');
        if (sn.indexOf(WBMU_CATEGORY_) === 0) docs.push(all[i]);
      }
    }
  }

  wbmuLog_('Документов «УПД по маркировке» получено: ' + docs.length);
  wbmuLog_('');
  return docs;
}

function wbmuListWithCategory_(token, category) {
  var out = [];
  var offset = 0;
  for (var page = 0; page < WBMU_MAX_PAGES_; page++) {
    var url = WBMU_HOST_ + '/api/v1/documents/list'
      + '?beginTime=' + encodeURIComponent(WBMU_BEGIN_)
      + '&endTime=' + encodeURIComponent(wbmuToday_())
      + '&limit=' + WBMU_PAGE_LIMIT_
      + '&offset=' + offset
      + '&locale=ru';
    if (category) url += '&category=' + encodeURIComponent(category);

    var r = wbmuFetch_('get', url, token);
    if (!r.ok) {
      wbmuLog_('❌ DATA_MISSING на offset=' + offset + ': HTTP ' + r.code + ' ' + wbmuCut_(r.text, 200));
      if (page === 0 && category) return null;   // фильтр не поддержан — сигнал наверх
      break;
    }
    var rows = wbmuPick_(r.json, ['data.documents', 'documents', 'data']);
    if (!rows) {
      wbmuLog_('❌ DATA_MISSING: ответ не разобрался на offset=' + offset);
      break;
    }
    if (page === 0 && category && rows.length === 0) return null;  // фильтр съел всё — подозрительно

    out = out.concat(rows);
    if (rows.length < WBMU_PAGE_LIMIT_) break;
    offset += WBMU_PAGE_LIMIT_;
    Utilities.sleep(WBMU_PAUSE_MS_);
  }
  return out;
}

/* ─────────────────────── шаг 2: покрытие 63 поставок ─────────────────────── */

function wbmuStep2Coverage_(docs) {
  wbmuLog_('───── ШАГ 2. Покрытие поставок с маркировкой ─────');

  var bySupply = {};
  for (var i = 0; i < docs.length; i++) {
    var sn = String(docs[i].serviceName || '');
    var m = sn.match(/(\d{6,})\s*$/);
    if (m) bySupply[m[1]] = docs[i];
  }

  var have = [], miss = [];
  for (var k = 0; k < WBMU_KIZ_SUPPLIES_.length; k++) {
    var sup = WBMU_KIZ_SUPPLIES_[k];
    if (bySupply[sup]) have.push(sup); else miss.push(sup);
  }

  wbmuLog_('Поставок с needKiz = true : ' + WBMU_KIZ_SUPPLIES_.length);
  wbmuLog_('Документ найден           : ' + have.length);
  wbmuLog_('Документа НЕТ             : ' + miss.length);
  wbmuLog_('');

  if (miss.length) {
    wbmuLog_('🔴 Поставки БЕЗ «УПД по маркировке» (кандидаты в ручную работу');
    wbmuLog_('   и одновременно кандидаты в «коды не переданы на баланс WB»):');
    var line = '   ';
    for (var j = 0; j < miss.length; j++) {
      line += miss[j] + '  ';
      if ((j + 1) % 8 === 0) { wbmuLog_(line); line = '   '; }
    }
    if (line.trim()) wbmuLog_(line);
    wbmuLog_('');
  }

  // Документы, которые есть, но поставки нет в нашем списке 63 — тоже сигнал.
  var extra = [];
  for (var s in bySupply) {
    if (WBMU_KIZ_SUPPLIES_.indexOf(s) < 0) extra.push(s);
  }
  if (extra.length) {
    wbmuLog_('⚠️ Документы по поставкам, которых нет в списке 63: ' + extra.join(', '));
    wbmuLog_('   Разобрать: либо поставка вне периода, либо needKiz посчитан не так.');
    wbmuLog_('');
  }

  return bySupply;
}

/* ──────────────────── шаг 3: что внутри XML ──────────────────── */

function wbmuStep3Content_(token, bySupply) {
  wbmuLog_('───── ШАГ 3. Содержимое документа ─────');

  var targets = [];
  for (var sup in WBMU_EXPECTED_) {
    if (bySupply[sup]) targets.push({ supply: sup, doc: bySupply[sup] });
  }
  if (!targets.length) {
    wbmuLog_('⚠️ Ни одной контрольной поставки (41047776, 41047801) среди документов — сверить счёт не с чем.');
    return;
  }

  for (var t = 0; t < targets.length; t++) {
    var sup = targets[t].supply;
    var doc = targets[t].doc;
    var expected = WBMU_EXPECTED_[sup];

    wbmuLog_('');
    wbmuLog_('■ Поставка ' + sup + ' — ожидаем ' + expected + ' КИЗ');
    wbmuLog_('  Документ: ' + JSON.stringify(doc));

    var ext = (doc.extensions && doc.extensions.length) ? String(doc.extensions[0]) : 'xml';
    var url = WBMU_HOST_ + '/api/v1/documents/download'
      + '?serviceName=' + encodeURIComponent(String(doc.serviceName || ''))
      + '&extension=' + encodeURIComponent(ext);

    var r = wbmuFetch_('get', url, token);
    if (!r.ok) {
      wbmuLog_('  ❌ DATA_MISSING: HTTP ' + r.code + ' ' + wbmuCut_(r.text, 200));
      continue;
    }
    var body = r.json && (r.json.data || r.json);
    var b64 = body ? String(body.document || '') : '';
    if (!b64) { wbmuLog_('  ⚠️ Тело пустое.'); continue; }

    var xml = wbmuDecode_(b64);
    if (!xml) { wbmuLog_('  ⚠️ Не удалось декодировать.'); continue; }

    wbmuLog_('  Размер после декодирования: ' + xml.length + ' символов');
    wbmuAnalyzeXml_(xml, expected);
    Utilities.sleep(WBMU_PAUSE_MS_);
  }
}

/**
 * 🔴 Кодировка windows-1251. Проба №1 читала как UTF-8 и получила крокозябры —
 * это была ошибка чтения, а не порча документа.
 */
function wbmuDecode_(b64) {
  var bytes = Utilities.base64Decode(b64);
  var charsets = ['windows-1251', 'Windows-1251', 'cp1251', 'UTF-8'];
  for (var i = 0; i < charsets.length; i++) {
    try {
      var s = Utilities.newBlob(bytes).getDataAsString(charsets[i]);
      if (s && s.indexOf('<') >= 0) {
        wbmuLog_('  Кодировка прочитана как: ' + charsets[i]);
        return s;
      }
    } catch (e) { /* пробуем следующую */ }
  }
  return null;
}

function wbmuAnalyzeXml_(xml, expected) {
  // 1. Коды маркировки по префиксу GTIN — независимо от имени тега.
  var codes = [];
  var re = new RegExp('>\\s*(' + WBMU_GTIN_PREFIX_ + '[^<]{4,60}?)\\s*<', 'g');
  var m;
  while ((m = re.exec(xml)) !== null) codes.push(m[1]);

  // Дополнительно — коды внутри атрибутов.
  var reAttr = new RegExp('"(' + WBMU_GTIN_PREFIX_ + '[^"]{4,60})"', 'g');
  while ((m = reAttr.exec(xml)) !== null) codes.push(m[1]);

  var uniq = {};
  var uniqList = [];
  for (var i = 0; i < codes.length; i++) {
    if (!uniq[codes[i]]) { uniq[codes[i]] = 1; uniqList.push(codes[i]); }
  }

  wbmuLog_('  Найдено кодов с префиксом ' + WBMU_GTIN_PREFIX_ + ': ' + codes.length + ' (уникальных ' + uniqList.length + ')');

  if (uniqList.length) {
    wbmuLog_('  Образцы (3 шт.):');
    for (var s = 0; s < Math.min(3, uniqList.length); s++) wbmuLog_('    ' + uniqList[s]);
  }

  // 2. Вердикт по контрольной величине.
  if (uniqList.length === expected) {
    wbmuLog_('  ✅ СХОДИТСЯ: ' + uniqList.length + ' = ' + expected + '. Источник подтверждён.');
  } else if (uniqList.length === 0) {
    wbmuLog_('  🔴 КОДОВ НЕТ. Документ не является списком КИЗ — источник НЕ подтверждён.');
  } else {
    wbmuLog_('  🔴 НЕ СХОДИТСЯ: ' + uniqList.length + ' против ожидаемых ' + expected + '.');
    wbmuLog_('     Не принимать как «примерно то же» — разбирать причину.');
  }

  // 3. Перечень тегов: структуру не угадываем, а смотрим.
  var tags = {};
  var reTag = /<\/?([A-Za-zА-Яа-я_][^\s\/>]*)/g;
  while ((m = reTag.exec(xml)) !== null) {
    var name = m[1];
    if (name.charAt(0) === '?' || name.charAt(0) === '!') continue;
    tags[name] = (tags[name] || 0) + 1;
  }
  var names = Object.keys(tags).sort(function (a, b) { return tags[b] - tags[a]; });
  wbmuLog_('  Теги документа (топ-25 по частоте):');
  var out = '    ';
  for (var t = 0; t < Math.min(names.length, 25); t++) {
    out += names[t] + '(' + tags[names[t]] + ')  ';
    if ((t + 1) % 5 === 0) { wbmuLog_(out); out = '    '; }
  }
  if (out.trim()) wbmuLog_(out);

  // 4. Признаки подписи — отвечают на вопрос «переданы ли коды на баланс WB».
  var signHints = ['Подпис', 'ЭЦП', 'Signature', 'СвПрод', 'СвПокуп', 'ИдОтпр', 'ИдПол', 'ДатаИнфПр'];
  var found = [];
  for (var h = 0; h < signHints.length; h++) {
    if (xml.indexOf(signHints[h]) >= 0) found.push(signHints[h]);
  }
  wbmuLog_('  Признаки сторон/подписи в документе: ' + (found.length ? found.join(', ') : 'не найдены'));
}

/* ──────────────────────────── инфраструктура ──────────────────────────── */

/**
 * 🔴 429 обрабатывается по заголовку `X-RateLimit-Retry`, а не фиксированной
 * паузой. Проба №1 упёрлась в лимит на offset=400 именно из-за слепого темпа.
 */
function wbmuFetch_(method, url, token) {
  var last = { ok: false, code: 0, text: '', json: null };
  for (var attempt = 0; attempt < WBMU_MAX_ATTEMPTS_; attempt++) {
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
      } catch (e2) { /* оставим 5 */ }
      wait = Math.min(Math.max(wait, 1), 60);
      wbmuLog_('   ⏳ 429: жду ' + wait + ' с по требованию шлюза…');
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

function wbmuPick_(obj, paths) {
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

function wbmuToday_() {
  return Utilities.formatDate(new Date(), 'Europe/Moscow', 'yyyy-MM-dd');
}

function wbmuCut_(s, n) {
  s = String(s || '').replace(/\s+/g, ' ');
  return s.length > n ? s.substring(0, n) + '…' : s;
}

function wbmuLog_(msg) {
  Logger.log(msg);
}
