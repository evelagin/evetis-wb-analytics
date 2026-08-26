/**
 * ══════════════════════════════════════════════════════════════
 * EVETIS WB — WbDocumentsProbe.gs   (проба, только чтение)
 *
 * ЗАЧЕМ. Нужно закрыть КИЗ-контур: понять, по каким кодам маркировки
 * товар реально уехал на склады WB, и сверить это с состоянием кодов
 * в «Честном знаке». Количества по поставкам у нас уже есть
 * (RAW_WB_SUPPLIES / RAW_WB_SUPPLIES_GOODS, 115 поставок, из них 63
 * с `needKiz = true`), но САМИХ КОДОВ в supplies-api нет — там только
 * штрихкод, количество и флаг «нужен КИЗ».
 *
 * Сейчас списки КИЗ владелец качает руками из ЛК WB по одной поставке
 * (файлы вида `chestniyznak41047776.csv`). На 63 поставки это 63
 * скачивания, и повторять это каждый месяц бессмысленно.
 *
 * Гипотеза: списки КИЗ доступны через публичное API документов
 * (`documents-api.wildberries.ru`), и тогда их можно грузить в BigQuery
 * тем же способом, что остальные потоки. Спецификация API документов
 * полный перечень категорий НЕ приводит — его отдаёт только живой
 * метод `/categories`. Поэтому нужна проба.
 *
 * 🔴 ГЛАВНОЕ ПРАВИЛО ЭТОЙ ПРОБЫ
 *
 * Проба НИЧЕГО не утверждает без ответа сервера. Если категория с КИЗ
 * не найдена — это результат «не найдено», а не повод достроить вывод
 * догадкой. Отсутствие категории означает, что путь через API документов
 * закрыт и грузить придётся иначе; такой вывод так же ценен, как
 * положительный, и должен быть виден в журнале явно.
 *
 * Второе правило: ошибка чтения НЕ превращается в данные. Неудачный
 * запрос печатается как ERROR со статусом и телом ответа, соответствующий
 * блок помечается `DATA_MISSING`, и на выводы о наличии/отсутствии
 * КИЗ-документов такой блок не влияет.
 *
 * ЧТО УСТАНАВЛИВАЕТ ПРОБА
 *   1. Полный перечень категорий документов (`name` → `title`), которые
 *      реально доступны этому кабинету.
 *   2. Есть ли среди них категория со списком КИЗ / кодов маркировки,
 *      и как она называется технически.
 *   3. Сколько документов доступно за период маркировки (с 01.08.2025),
 *      с разбивкой по категориям и по доступным расширениям.
 *   4. Привязан ли документ к номеру поставки — по вхождению supplyID
 *      в `serviceName` или `name`. Проверяется на пяти известных
 *      поставках июля 2026 (41047776, 41047801, 41037541, 41039407,
 *      41039644), для которых у владельца уже есть выгрузки из ЛК.
 *   5. Что реально отдаёт `/download` для одного кандидата: расширение,
 *      размер, признак ZIP/CSV. Сами коды в журнал НЕ печатаются —
 *      только длина и первые 120 символов для опознания формата.
 *
 * ЧЕГО ПРОБА НЕ ДЕЛАЕТ
 *   • не пишет ни в лист, ни в BigQuery;
 *   • не скачивает больше одного документа-кандидата;
 *   • не печатает токен и не печатает коды маркировки целиком.
 *
 * ИСТОРИЯ ПРОГОНОВ
 *   ред.1, 24.08.2026 16:13 — HTTP 403 «scope is not allowed for this
 *   resource», origin `ag-documents`, токен WB_TOKEN_FINANCE. Вывод:
 *   🔑 эндпоинт живой и путь верный, упёрлись в ПРАВА токена, а не в
 *   отсутствие метода. Ред.2 добавляет ШАГ 0 — перебор всех ключей
 *   токенов, чтобы за один прогон узнать, есть ли доступ хоть у одного.
 *
 * КАК ЗАПУСКАТЬ. Выбрать функцию `wbDocsProbeRun` → Run → смотреть
 * Execution log. Ничего настраивать не нужно, токен берётся из
 * Script Properties по тем же ключам, что и остальные загрузчики.
 * ══════════════════════════════════════════════════════════════
 */

var WBDOC_HOST_ = 'https://documents-api.wildberries.ru';

/**
 * Все ключи токенов, какие есть в проекте. Проба ред.2 перебирает их ВСЕ
 * и показывает, у какого есть доступ к категории «Документы».
 * Причина: прогон 24.08 дал 403 «scope is not allowed for this resource»
 * (origin `ag-documents`) на WB_TOKEN_FINANCE. Эндпоинт живой, дело в правах.
 */
var WBDOC_TOKEN_KEYS_ = [
  'WB_TOKEN_DOCUMENTS',
  'WB_TOKEN_STANDARD',
  'WB_TOKEN_FINANCE',
  'WB_TOKEN_SUPPLIES',
  'WB_TOKEN_ANALYTICS',
  'WB_TOKEN_STATISTICS',
  'WB_TOKEN_ADVERT',
  'WB_TOKEN_PROMOTION'
];

/** Период маркировки: первая поставка с needKiz = true была 18.08.2025. */
var WBDOC_BEGIN_ = '2025-08-01';

/** Поставки, по которым у владельца уже есть ручные выгрузки из ЛК. */
var WBDOC_KNOWN_SUPPLIES_ = ['41047776', '41047801', '41037541', '41039407', '41039644'];

/** Слова-маркеры, по которым опознаём КИЗ-документ. */
var WBDOC_KIZ_HINTS_ = ['киз', 'киз', 'марк', 'честн', 'chestn', 'kiz', 'mark', 'cis', 'crpt'];

var WBDOC_PAGE_LIMIT_ = 50;      // максимум, разрешённый API
var WBDOC_MAX_PAGES_ = 40;       // потолок 2000 документов — защита от бесконечной страницы
var WBDOC_PAUSE_MS_ = 700;       // вежливая пауза между запросами
var WBDOC_MAX_ATTEMPTS_ = 3;

/* ────────────────────────────── точка входа ────────────────────────────── */

function wbDocsProbeRun() {
  var t0 = new Date();
  wbDocLog_('═══════════════════════════════════════════════════');
  wbDocLog_('ПРОБА API ДОКУМЕНТОВ WB — только чтение');
  wbDocLog_('Запуск: ' + t0.toISOString());
  wbDocLog_('═══════════════════════════════════════════════════');

  var auth = wbDocStep0Tokens_();
  if (!auth) {
    wbDocLog_('');
    wbDocLog_('❌ ОСТАНОВ: ни один токен не имеет доступа к категории «Документы».');
    wbDocLog_('');
    wbDocLog_('   ЧТО СДЕЛАТЬ:');
    wbDocLog_('   1) ЛК WB → название компании (правый верхний угол) → «Интеграции по API»');
    wbDocLog_('   2) Создать новый токен, отметив категорию «Документы»');
    wbDocLog_('      (у существующего токена набор категорий изменить нельзя — только новый)');
    wbDocLog_('   3) Положить его в Script Properties под именем WB_TOKEN_DOCUMENTS');
    wbDocLog_('   4) Запустить wbDocsProbeRun ещё раз');
    wbDocLog_('');
    wbDocLog_('   Если категории «Документы» в списке при создании токена НЕТ —');
    wbDocLog_('   это тоже результат: путь через API документов закрыт для кабинета,');
    wbDocLog_('   и грузить КИЗ придётся выгрузками из ЛК. Сообщи, что увидел.');
    return;
  }
  wbDocLog_('');
  wbDocLog_('✅ Рабочий токен: ' + auth.keyName + ' (значение не печатается)');

  var end = wbDocToday_();
  wbDocLog_('Период запроса: ' + WBDOC_BEGIN_ + ' … ' + end);
  wbDocLog_('');

  var categories = wbDocStep1Categories_(auth.token);
  var docs = wbDocStep2List_(auth.token, WBDOC_BEGIN_, end);
  wbDocStep3Analyze_(docs, categories);
  wbDocStep4Download_(auth.token, docs);

  var secs = Math.round((new Date() - t0) / 1000);
  wbDocLog_('');
  wbDocLog_('═══════════════════════════════════════════════════');
  wbDocLog_('ПРОБА ЗАВЕРШЕНА за ' + secs + ' с. Записей не производилось.');
  wbDocLog_('═══════════════════════════════════════════════════');
}

/* ──────────────────── шаг 0: какой токен пускают в документы ──────────────────── */

/**
 * Перебирает ВСЕ ключи токенов и проверяет каждый на `/categories`.
 * Возвращает первый, который ответил 2xx, либо null.
 *
 * 🔴 Токены не печатаются. В журнал идёт только имя ключа и код ответа.
 */
function wbDocStep0Tokens_() {
  wbDocLog_('───── ШАГ 0. Проверка прав токенов на раздел «Документы» ─────');
  var sp = PropertiesService.getScriptProperties();
  var winner = null;
  var checked = 0;

  for (var i = 0; i < WBDOC_TOKEN_KEYS_.length; i++) {
    var key = WBDOC_TOKEN_KEYS_[i];
    var val = String(sp.getProperty(key) || '').trim();
    if (!val) {
      wbDocLog_('  ' + wbDocPad_(key, 22) + ' │ — не задан');
      continue;
    }
    checked++;
    var r = wbDocFetch_('get', WBDOC_HOST_ + '/api/v1/documents/categories', val);
    var verdict;
    if (r.ok) {
      verdict = '✅ HTTP ' + r.code + ' — ДОСТУП ЕСТЬ';
      if (!winner) winner = { token: val, keyName: key };
    } else if (r.code === 403) {
      verdict = '✖ HTTP 403 — нет категории «Документы»';
    } else if (r.code === 401) {
      verdict = '✖ HTTP 401 — токен недействителен или истёк';
    } else {
      verdict = '✖ HTTP ' + r.code + ' — ' + wbDocCut_(r.text, 120);
    }
    wbDocLog_('  ' + wbDocPad_(key, 22) + ' │ ' + verdict);
    Utilities.sleep(400);
  }

  wbDocLog_('');
  wbDocLog_('Проверено токенов: ' + checked + ' из ' + WBDOC_TOKEN_KEYS_.length + ' возможных ключей.');
  return winner;
}

/* ─────────────────────────── шаг 1: категории ─────────────────────────── */

function wbDocStep1Categories_(token) {
  wbDocLog_('───── ШАГ 1. Категории документов ─────');
  var r = wbDocFetch_('get', WBDOC_HOST_ + '/api/v1/documents/categories', token);
  if (!r.ok) {
    wbDocLog_('❌ DATA_MISSING: категории не прочитались. HTTP ' + r.code + ' ' + wbDocCut_(r.text, 300));
    wbDocLog_('   Частая причина — у токена нет доступа к категории «Документы».');
    return null;
  }
  var list = wbDocPick_(r.json, ['data.categories', 'categories', 'data']);
  if (!list || !list.length) {
    wbDocLog_('⚠️ Ответ разобран, но список категорий пуст. Сырой ответ: ' + wbDocCut_(r.text, 400));
    return [];
  }
  wbDocLog_('Категорий доступно: ' + list.length);
  var hits = [];
  for (var i = 0; i < list.length; i++) {
    var name = String(list[i].name || list[i].category || '');
    var title = String(list[i].title || list[i].displayName || '');
    var flag = wbDocLooksKiz_(name + ' ' + title) ? '   ⬅️ ПОХОЖЕ НА КИЗ' : '';
    if (flag) hits.push(name + ' / ' + title);
    wbDocLog_('  • ' + wbDocPad_(name, 34) + ' │ ' + title + flag);
  }
  wbDocLog_('');
  wbDocLog_(hits.length
    ? '✅ Кандидатов на КИЗ-документ среди категорий: ' + hits.length
    : '⚠️ Среди категорий ничего КИЗ-подобного НЕ найдено. Это valid-результат: см. шаг 3.');
  wbDocLog_('');
  return list;
}

/* ──────────────────────── шаг 2: список документов ──────────────────────── */

function wbDocStep2List_(token, begin, end) {
  wbDocLog_('───── ШАГ 2. Список документов за период ─────');
  var all = [];
  var offset = 0;
  for (var page = 0; page < WBDOC_MAX_PAGES_; page++) {
    var url = WBDOC_HOST_ + '/api/v1/documents/list'
      + '?beginTime=' + encodeURIComponent(begin)
      + '&endTime=' + encodeURIComponent(end)
      + '&limit=' + WBDOC_PAGE_LIMIT_
      + '&offset=' + offset
      + '&locale=ru';
    var r = wbDocFetch_('get', url, token);
    if (!r.ok) {
      wbDocLog_('❌ DATA_MISSING на offset=' + offset + ': HTTP ' + r.code + ' ' + wbDocCut_(r.text, 300));
      break;
    }
    var rows = wbDocPick_(r.json, ['data.documents', 'documents', 'data']);
    if (!rows) {
      wbDocLog_('❌ DATA_MISSING: ответ не разобрался на offset=' + offset + '. Сырой: ' + wbDocCut_(r.text, 300));
      break;
    }
    all = all.concat(rows);
    if (rows.length < WBDOC_PAGE_LIMIT_) break;
    offset += WBDOC_PAGE_LIMIT_;
    Utilities.sleep(WBDOC_PAUSE_MS_);
  }
  wbDocLog_('Документов получено: ' + all.length);
  wbDocLog_('');
  return all;
}

/* ───────────────────────── шаг 3: анализ находок ───────────────────────── */

function wbDocStep3Analyze_(docs, categories) {
  wbDocLog_('───── ШАГ 3. Что в этих документах ─────');
  if (!docs || !docs.length) {
    wbDocLog_('⚠️ Документов нет — анализировать нечего.');
    wbDocLog_('');
    return;
  }

  var byCat = {};
  var byExt = {};
  for (var i = 0; i < docs.length; i++) {
    var c = String(docs[i].category || '(без категории)');
    byCat[c] = (byCat[c] || 0) + 1;
    var ex = docs[i].extensions;
    if (ex && ex.length) {
      for (var j = 0; j < ex.length; j++) {
        var e = String(ex[j]);
        byExt[e] = (byExt[e] || 0) + 1;
      }
    }
  }

  wbDocLog_('Разбивка по категориям:');
  var cats = Object.keys(byCat).sort(function (a, b) { return byCat[b] - byCat[a]; });
  for (var k = 0; k < cats.length; k++) {
    var mark = wbDocLooksKiz_(cats[k]) ? '   ⬅️ ПОХОЖЕ НА КИЗ' : '';
    wbDocLog_('  ' + wbDocPad_(String(byCat[cats[k]]), 5) + ' │ ' + cats[k] + mark);
  }
  wbDocLog_('');
  wbDocLog_('Доступные расширения: ' + JSON.stringify(byExt));
  wbDocLog_('');

  // Привязка к поставкам: ищем номера известных поставок в serviceName/name.
  wbDocLog_('Привязка документов к номерам поставок:');
  var linked = 0;
  for (var s = 0; s < WBDOC_KNOWN_SUPPLIES_.length; s++) {
    var sup = WBDOC_KNOWN_SUPPLIES_[s];
    var found = [];
    for (var d = 0; d < docs.length; d++) {
      var hay = String(docs[d].serviceName || '') + ' ' + String(docs[d].name || '');
      if (hay.indexOf(sup) >= 0) {
        found.push(String(docs[d].category || '?') + ' → ' + String(docs[d].serviceName || '?'));
      }
    }
    if (found.length) {
      linked++;
      wbDocLog_('  ✅ поставка ' + sup + ': найдено документов ' + found.length);
      for (var f = 0; f < Math.min(found.length, 4); f++) wbDocLog_('       ' + found[f]);
    } else {
      wbDocLog_('  ✖ поставка ' + sup + ': документов с этим номером не найдено');
    }
  }
  wbDocLog_('');
  wbDocLog_(linked
    ? '✅ ВЫВОД: документы привязываются к поставке через serviceName/name (' + linked + ' из ' + WBDOC_KNOWN_SUPPLIES_.length + ').'
    : '⚠️ ВЫВОД: связь «документ ↔ поставка» по номеру НЕ обнаружена. Значит либо КИЗ-документов в этом API нет, либо связь идёт другим полем — смотри примеры ниже.');
  wbDocLog_('');

  wbDocLog_('Первые 15 документов как образец структуры:');
  for (var p = 0; p < Math.min(docs.length, 15); p++) {
    wbDocLog_('  ' + JSON.stringify(docs[p]));
  }
  wbDocLog_('');
}

/* ─────────────────── шаг 4: пробное скачивание кандидата ─────────────────── */

function wbDocStep4Download_(token, docs) {
  wbDocLog_('───── ШАГ 4. Пробное скачивание одного кандидата ─────');
  if (!docs || !docs.length) {
    wbDocLog_('⚠️ Документов нет — скачивать нечего.');
    return;
  }

  var cand = null;
  for (var i = 0; i < docs.length && !cand; i++) {
    var hay = String(docs[i].category || '') + ' ' + String(docs[i].name || '') + ' ' + String(docs[i].serviceName || '');
    if (wbDocLooksKiz_(hay)) cand = docs[i];
  }
  if (!cand) {
    for (var j = 0; j < docs.length && !cand; j++) {
      var h2 = String(docs[j].serviceName || '') + ' ' + String(docs[j].name || '');
      for (var s = 0; s < WBDOC_KNOWN_SUPPLIES_.length; s++) {
        if (h2.indexOf(WBDOC_KNOWN_SUPPLIES_[s]) >= 0) { cand = docs[j]; break; }
      }
    }
  }
  if (!cand) {
    wbDocLog_('⚠️ Кандидат не выбран: ни КИЗ-подобных документов, ни документов с номером поставки.');
    wbDocLog_('   Это означает, что путь «КИЗ через API документов» скорее всего закрыт.');
    return;
  }

  var ext = (cand.extensions && cand.extensions.length) ? String(cand.extensions[0]) : 'zip';
  wbDocLog_('Кандидат: ' + JSON.stringify(cand));
  wbDocLog_('Скачиваю с extension=' + ext + ' …');

  var url = WBDOC_HOST_ + '/api/v1/documents/download'
    + '?serviceName=' + encodeURIComponent(String(cand.serviceName || ''))
    + '&extension=' + encodeURIComponent(ext);
  var r = wbDocFetch_('get', url, token);
  if (!r.ok) {
    wbDocLog_('❌ DATA_MISSING: скачивание не удалось. HTTP ' + r.code + ' ' + wbDocCut_(r.text, 300));
    return;
  }
  var body = r.json && (r.json.data || r.json);
  var b64 = body ? String(body.document || '') : '';
  wbDocLog_('fileName : ' + (body ? body.fileName : '(нет)'));
  wbDocLog_('extension: ' + (body ? body.extension : '(нет)'));
  wbDocLog_('base64   : длина ' + b64.length + ' символов (~' + Math.round(b64.length * 0.75 / 1024) + ' КБ)');

  if (!b64) { wbDocLog_('⚠️ Тело документа пустое.'); return; }

  // Опознаём формат по сигнатуре, коды целиком НЕ печатаем.
  try {
    var bytes = Utilities.base64Decode(b64);
    var isZip = bytes.length > 1 && bytes[0] === 80 && bytes[1] === 75; // 'PK'
    wbDocLog_('Формат   : ' + (isZip ? 'ZIP-архив' : 'не ZIP (вероятно текст/CSV)'));
    if (!isZip) {
      var text = Utilities.newBlob(bytes).getDataAsString('UTF-8');
      wbDocLog_('Первые 120 символов: ' + wbDocCut_(text, 120));
      wbDocLog_('Всего символов: ' + text.length);
    } else {
      wbDocLog_('Внутри ZIP — распаковку в пробе не делаем, это работа загрузчика.');
    }
  } catch (e) {
    wbDocLog_('⚠️ Не удалось декодировать base64: ' + e);
  }
}

/* ──────────────────────────── инфраструктура ──────────────────────────── */

/*
 * Функция `wbDocToken_()` из ред.1 удалена сознательно: она брала ПЕРВЫЙ
 * непустой токен и на нём же останавливалась. Именно поэтому прогон 24.08
 * упёрся в WB_TOKEN_FINANCE и вернул 403, не проверив остальные ключи.
 * Её работу выполняет `wbDocStep0Tokens_()`, которая перебирает все ключи
 * и выбирает тот, что реально пускают в раздел документов.
 */

function wbDocFetch_(method, url, token) {
  var last = { ok: false, code: 0, text: '', json: null };
  for (var attempt = 0; attempt < WBDOC_MAX_ATTEMPTS_; attempt++) {
    var opt = { method: method, muteHttpExceptions: true, headers: { 'Authorization': token } };
    var res;
    try {
      res = UrlFetchApp.fetch(url, opt);
    } catch (e) {
      last = { ok: false, code: -1, text: String(e), json: null };
      Utilities.sleep(1500 * (attempt + 1));
      continue;
    }
    var code = res.getResponseCode();
    var text = res.getContentText();
    if (code === 429 || code >= 500) {
      last = { ok: false, code: code, text: text, json: null };
      Utilities.sleep(2000 * (attempt + 1));
      continue;
    }
    var json = null;
    try { json = JSON.parse(text); } catch (e2) { json = null; }
    return { ok: code >= 200 && code < 300, code: code, text: text, json: json };
  }
  return last;
}

/** Достаёт массив по первому подошедшему пути вида 'data.documents'. */
function wbDocPick_(obj, paths) {
  if (!obj) return null;
  for (var i = 0; i < paths.length; i++) {
    var cur = obj;
    var parts = paths[i].split('.');
    var ok = true;
    for (var j = 0; j < parts.length; j++) {
      if (cur && typeof cur === 'object' && parts[j] in cur) cur = cur[parts[j]];
      else { ok = false; break; }
    }
    if (ok && cur && cur.length !== undefined) return cur;
  }
  return null;
}

function wbDocLooksKiz_(s) {
  var low = String(s || '').toLowerCase();
  for (var i = 0; i < WBDOC_KIZ_HINTS_.length; i++) {
    if (low.indexOf(WBDOC_KIZ_HINTS_[i]) >= 0) return true;
  }
  return false;
}

function wbDocToday_() {
  return Utilities.formatDate(new Date(), 'Europe/Moscow', 'yyyy-MM-dd');
}

function wbDocPad_(s, n) {
  s = String(s);
  while (s.length < n) s += ' ';
  return s;
}

function wbDocCut_(s, n) {
  s = String(s || '').replace(/\s+/g, ' ');
  return s.length > n ? s.substring(0, n) + '…' : s;
}

function wbDocLog_(msg) {
  Logger.log(msg);
}
