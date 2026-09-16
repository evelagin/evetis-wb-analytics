/**
 * ══════════════════════════════════════════════════════════════
 * 📕 УСТАРЕЛА — НЕ ЗАПУСКАТЬ. Заменена на WbIncomesShapeProbe3.gs.
 *
 * Файл хранится как исторический снимок: журнал этой пробы разобран в
 * `docs/wb_incomes_api_retirement.md`, и без исходника нельзя проверить, что
 * именно запускалось. Пробу №1 мы уже потеряли по этой причине, повторять не
 * стоит.
 *
 * 🔴 В пробе ДВЕ ИЗВЕСТНЫЕ ОШИБКИ, обе исправлены в пробе №3:
 *   1. `GET /api/v1/acceptance_report` принят за синхронное чтение. На деле он
 *      СОЗДАЁТ ЗАДАЧУ и возвращает `taskId`. Проба этого не распознала, ушла в
 *      `POST` и получила 405 — это её дефект, а не отказ WB.
 *   2. Контролем живости шлюза выбран `/api/v1/supplier/stocks`, который сам
 *      отключён 23.06.2026 — и это стояло в нашем же `docs/wb_api_test_plan.md`.
 *      Рабочим контролем оказался `/ping`.
 * ══════════════════════════════════════════════════════════════
 *
 * ══════════════════════════════════════════════════════════════
 * EVETIS WB — WbIncomesShapeProbe2.gs   (проба, только чтение)
 *
 * ЗАЧЕМ. Проба №1 (probeWbIncomes, 17.08.2026) получила:
 *   /api/v1/supplier/incomes → 404 path not found (origin ag-statistics)
 *   /api/v2/supplier/incomes → 404 path not found (origin ag-statistics)
 *   supplies-api /api/v1/warehouses → 403 scope is not allowed
 * По документации WB метод «Поставки» (/api/v1/supplier/incomes) помечен как
 * устаревший с датой снятия 11 марта. 404 от самого шлюза статистики
 * согласуется с тем, что метод уже снят.
 *
 * НО из журнала №1 нельзя отличить три причины 404:
 *   (а) метод снят у всех;
 *   (б) шлюз статистики недоступен нашему токену в принципе;
 *   (в) ошибка в самой пробе (кривой URL).
 * Проба №2 разводит эти версии контрольным вызовом ЗАВЕДОМО ЖИВОГО метода
 * той же категории и печатает полный URL каждого запроса.
 *
 * ЧТО ДЕЛАЕТ (ничего не пишет, только GET/POST-задачи на отчёты):
 *   Блок A. Шлюз статистики: /ping, живой /supplier/stocks, повтор /supplier/incomes.
 *   Блок B. Отчёт о платной приёмке на seller-analytics-api (категория «Аналитика»,
 *           у нас открыта): свежее окно + окно сентября 2024 для замера глубины.
 *   Блок C. Шлюз поставок: /ping, /warehouses, /supplies — под токеном
 *           WB_TOKEN_SUPPLIES, если владелец его завёл.
 *
 * БЕЗОПАСНОСТЬ. Токен в журнал не печатается — печатается только ИМЯ свойства,
 * из которого он взят. Ни одного метода записи. BigQuery не трогается.
 *
 * ЗАПУСК. Функция probeWbIncomes2. Идёт до ~4 минут из-за лимита WB
 * «1 запрос в минуту» на отчёты. Лимит Apps Script — 6 минут, укладываемся.
 * ══════════════════════════════════════════════════════════════
 */

var P2_HOST_STAT_     = 'https://statistics-api.wildberries.ru';
var P2_HOST_ANALYTICS_= 'https://seller-analytics-api.wildberries.ru';
var P2_HOST_SUPPLIES_ = 'https://supplies-api.wildberries.ru';

var P2_KEYS_STAT_     = ['WB_TOKEN_STATISTICS', 'WB_TOKEN_ANALYTICS'];
var P2_KEYS_ANALYTICS_= ['WB_TOKEN_ANALYTICS', 'WB_TOKEN_STATISTICS'];
var P2_KEYS_SUPPLIES_ = ['WB_TOKEN_SUPPLIES', 'WB_TOKEN_STATISTICS', 'WB_TOKEN_ANALYTICS'];

/** Замер глубины истории (окно сентября 2024). Выключить, если проба не успевает. */
var P2_DEEP_ = true;

/** Окно «свежей» приёмки: последние 31 день (максимум периода по документации). */
var P2_RECENT_DAYS_ = 31;

// ── утилиты ───────────────────────────────────────────────────

function p2Log_(s) { Logger.log(s); }

/** Токен из свойств скрипта/пользователя. Возвращает {token, keyName} без печати токена. */
function p2Token_(keys) {
  var sp = PropertiesService.getScriptProperties();
  var up = null;
  try { up = PropertiesService.getUserProperties(); } catch (e) { up = null; }
  for (var i = 0; i < keys.length; i++) {
    var k = keys[i], v = '';
    try { v = (sp && sp.getProperty(k)) || ''; } catch (e) { v = ''; }
    if (!v && up) { try { v = up.getProperty(k) || ''; } catch (e) {} }
    v = String(v || '').trim();
    if (v) return { token: v, keyName: k };
  }
  return { token: '', keyName: '' };
}

/** Одиночный запрос. Печатает метод, полный URL, код и первые 300 символов тела. */
function p2Call_(label, method, url, token, payload) {
  var opt = {
    method: method,
    muteHttpExceptions: true,
    followRedirects: true,
    headers: { 'Authorization': token }
  };
  if (payload) {
    opt.contentType = 'application/json';
    opt.payload = JSON.stringify(payload);
  }
  var code = -1, body = '', err = '';
  try {
    var resp = UrlFetchApp.fetch(url, opt);
    code = resp.getResponseCode();
    body = resp.getContentText();
  } catch (e) {
    err = String(e);
  }
  p2Log_('--- ' + label);
  p2Log_('    ' + method + ' ' + url);
  if (err) { p2Log_('    ИСКЛЮЧЕНИЕ: ' + err); return { code: -1, body: '', json: null }; }
  p2Log_('    HTTP ' + code + ', тело ' + body.length + ' симв.');
  p2Log_('    ' + body.substring(0, 300).replace(/\s+/g, ' '));
  var json = null;
  try { json = JSON.parse(body); } catch (e2) { json = null; }
  return { code: code, body: body, json: json };
}

function p2Ymd_(d) {
  return Utilities.formatDate(d, 'Europe/Moscow', 'yyyy-MM-dd');
}

function p2Keys_(obj) {
  var out = [];
  for (var k in obj) if (obj.hasOwnProperty(k)) out.push(k);
  return out;
}

/** Разбор массива строк отчёта о приёмке: сколько строк, какие поля, границы дат. */
function p2SummarizeAcceptance_(label, arr) {
  if (!arr || !arr.length) { p2Log_('    [' + label + '] строк: 0'); return; }
  p2Log_('    [' + label + '] строк: ' + arr.length);
  p2Log_('    [' + label + '] поля первой строки: ' + p2Keys_(arr[0]).join(', '));
  p2Log_('    [' + label + '] первая строка: ' + JSON.stringify(arr[0]).substring(0, 400));

  var minD = '', maxD = '', sumCnt = 0, sumTotal = 0, incomes = {};
  for (var i = 0; i < arr.length; i++) {
    var r = arr[i] || {};
    var d = String(r.shkCreateDate || r.giCreateDate || '');
    if (d) {
      if (!minD || d < minD) minD = d;
      if (!maxD || d > maxD) maxD = d;
    }
    if (typeof r.count === 'number') sumCnt += r.count;
    if (typeof r.total === 'number') sumTotal += r.total;
    if (r.incomeId) incomes[String(r.incomeId)] = 1;
  }
  p2Log_('    [' + label + '] даты: ' + (minD || '—') + ' … ' + (maxD || '—'));
  p2Log_('    [' + label + '] сумма count: ' + sumCnt + ', сумма total: ' + sumTotal);
  p2Log_('    [' + label + '] уникальных incomeId: ' + p2Keys_(incomes).length);
}

/**
 * Отчёт о платной приёмке за окно. Сначала пробует синхронный GET,
 * при неуспехе — задачный контур POST → status → download.
 * Возвращает массив строк либо null.
 */
function p2Acceptance_(label, token, dateFrom, dateTo) {
  var qs = '?dateFrom=' + dateFrom + '&dateTo=' + dateTo;

  var sync = p2Call_('приёмка ' + label + ' · синхронный GET', 'get',
                     P2_HOST_ANALYTICS_ + '/api/v1/acceptance_report' + qs, token);
  if (sync.code === 200 && sync.json) {
    var rows = (sync.json instanceof Array) ? sync.json
             : (sync.json.report || sync.json.data || null);
    if (rows instanceof Array) { p2SummarizeAcceptance_(label, rows); return rows; }
    p2Log_('    ответ 200, но массив строк не найден; ключи: ' + p2Keys_(sync.json).join(', '));
  }

  Utilities.sleep(3000);
  var create = p2Call_('приёмка ' + label + ' · создание задачи', 'post',
                       P2_HOST_ANALYTICS_ + '/api/v1/acceptance_report' + qs, token);
  var taskId = '';
  if (create.json) {
    if (create.json.data && create.json.data.taskId) taskId = String(create.json.data.taskId);
    else if (create.json.taskId) taskId = String(create.json.taskId);
  }
  if (!taskId) { p2Log_('    [' + label + '] taskId не получен — задачный контур недоступен'); return null; }
  p2Log_('    [' + label + '] taskId = ' + taskId);

  var status = '';
  for (var i = 0; i < 12; i++) {
    Utilities.sleep(5000);
    var st = p2Call_('приёмка ' + label + ' · статус #' + (i + 1), 'get',
                     P2_HOST_ANALYTICS_ + '/api/v1/acceptance_report/tasks/' + taskId + '/status', token);
    if (st.json && st.json.data && st.json.data.status) status = String(st.json.data.status);
    else if (st.json && st.json.status) status = String(st.json.status);
    if (status === 'done' || status === 'DONE' || status === 'ready') break;
    if (status === 'canceled' || status === 'purged') break;
  }
  p2Log_('    [' + label + '] статус задачи: ' + (status || '(не определён)'));
  if (status !== 'done' && status !== 'DONE' && status !== 'ready') return null;

  Utilities.sleep(2000);
  var dl = p2Call_('приёмка ' + label + ' · выгрузка', 'get',
                   P2_HOST_ANALYTICS_ + '/api/v1/acceptance_report/tasks/' + taskId + '/download', token);
  var out = null;
  if (dl.json instanceof Array) out = dl.json;
  else if (dl.json && dl.json.data instanceof Array) out = dl.json.data;
  if (out) p2SummarizeAcceptance_(label, out);
  else p2Log_('    [' + label + '] выгрузка не разобралась в массив');
  return out;
}

// ── основная функция ──────────────────────────────────────────

function probeWbIncomes2() {
  var t0 = new Date().getTime();
  p2Log_('=== PROBE-2 incomes · развод версий 404 и поиск замены ===');
  p2Log_('время старта: ' + Utilities.formatDate(new Date(), 'Europe/Moscow', 'yyyy-MM-dd HH:mm:ss'));

  // ── Блок A. Шлюз статистики ────────────────────────────────
  p2Log_('');
  p2Log_('### БЛОК A — шлюз статистики');
  var tStat = p2Token_(P2_KEYS_STAT_);
  if (!tStat.token) { p2Log_('❌ нет токена статистики: ' + P2_KEYS_STAT_.join(' / ')); }
  else {
    p2Log_('токен статистики взят из свойства: ' + tStat.keyName);

    p2Call_('A1 · ping статистики', 'get', P2_HOST_STAT_ + '/ping', tStat.token);

    Utilities.sleep(2000);
    var stocks = p2Call_('A2 · КОНТРОЛЬ, живой метод остатков', 'get',
      P2_HOST_STAT_ + '/api/v1/supplier/stocks?dateFrom=2019-06-20', tStat.token);
    if (stocks.code === 200 && stocks.json instanceof Array) {
      p2Log_('    контроль: строк ' + stocks.json.length +
             (stocks.json.length ? ', поля: ' + p2Keys_(stocks.json[0]).join(', ') : ''));
    }

    Utilities.sleep(2000);
    p2Call_('A3 · повтор incomes v1 (полный URL в журнале)', 'get',
      P2_HOST_STAT_ + '/api/v1/supplier/incomes?dateFrom=2024-01-01T00:00:00', tStat.token);

    Utilities.sleep(2000);
    p2Call_('A4 · incomes без параметров', 'get',
      P2_HOST_STAT_ + '/api/v1/supplier/incomes', tStat.token);
  }

  // ── Блок B. Отчёт о платной приёмке ────────────────────────
  p2Log_('');
  p2Log_('### БЛОК B — отчёт о приёмке (категория «Аналитика»)');
  var tAn = p2Token_(P2_KEYS_ANALYTICS_);
  if (!tAn.token) { p2Log_('❌ нет токена аналитики: ' + P2_KEYS_ANALYTICS_.join(' / ')); }
  else {
    p2Log_('токен аналитики взят из свойства: ' + tAn.keyName);

    var now = new Date();
    var from = new Date(now.getTime() - P2_RECENT_DAYS_ * 24 * 3600 * 1000);
    p2Acceptance_('свежее окно', tAn.token, p2Ymd_(from), p2Ymd_(now));

    if (P2_DEEP_ && (new Date().getTime() - t0) < 210000) {
      p2Log_('    пауза 65 с — лимит WB «1 запрос в минуту» на отчёты');
      Utilities.sleep(65000);
      p2Acceptance_('сентябрь 2024 (глубина)', tAn.token, '2024-09-01', '2024-09-30');
    } else if (P2_DEEP_) {
      p2Log_('    замер глубины пропущен: осталось мало времени выполнения');
    }
  }

  // ── Блок C. Шлюз поставок ──────────────────────────────────
  p2Log_('');
  p2Log_('### БЛОК C — шлюз поставок');
  var tSup = p2Token_(P2_KEYS_SUPPLIES_);
  if (!tSup.token) { p2Log_('❌ нет ни одного токена для supplies-api'); }
  else {
    p2Log_('токен для поставок взят из свойства: ' + tSup.keyName +
           (tSup.keyName === 'WB_TOKEN_SUPPLIES' ? ' (отдельный токен «Поставки»)' :
            ' (ВНИМАНИЕ: это НЕ токен категории «Поставки», ожидается 403)'));

    p2Call_('C1 · ping поставок', 'get', P2_HOST_SUPPLIES_ + '/ping', tSup.token);
    Utilities.sleep(1500);
    p2Call_('C2 · список складов FBW', 'get', P2_HOST_SUPPLIES_ + '/api/v1/warehouses', tSup.token);
    Utilities.sleep(1500);
    p2Call_('C3 · список поставок', 'get', P2_HOST_SUPPLIES_ + '/api/v1/supplies?limit=50&offset=0', tSup.token);
  }

  p2Log_('');
  p2Log_('=== ГОТОВО, ' + Math.round((new Date().getTime() - t0) / 1000) + ' с ===');
  p2Log_('Прислать журнал целиком. Читаем так:');
  p2Log_('  A2 = 200 → шлюз и токен живы, значит incomes действительно снят с обслуживания.');
  p2Log_('  A2 ≠ 200 → проблема шире, чем один метод, разбираем токен и категорию.');
  p2Log_('  B со строками за сентябрь 2024 → история приёмки достаётся, строим загрузчик на ней.');
  p2Log_('  C1/C2 = 200 → токен «Поставки» работает, поимённый контур поставок доступен.');
}
