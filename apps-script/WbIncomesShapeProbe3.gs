/**
 * ══════════════════════════════════════════════════════════════
 * EVETIS WB — WbIncomesShapeProbe3.gs   (проба, только чтение)
 *
 * ЧТО ИСПРАВЛЕНО ПОСЛЕ ПРОБЫ №2 (журнал 17.08.2026 16:58)
 *
 * 1) Отчёт о приёмке. Проба №2 считала GET синхронным чтением, а он оказался
 *    СОЗДАНИЕМ ЗАДАЧИ: вернул 200 и `{"data":{"taskId":"..."}}`. Проба это не
 *    распознала, пошла в POST и получила 405. Здесь контур правильный:
 *      GET  /api/v1/acceptance_report?dateFrom&dateTo   → taskId
 *      GET  /api/v1/acceptance_report/tasks/{id}/status → status
 *      GET  /api/v1/acceptance_report/tasks/{id}/download → строки
 *    🔑 Документация WB говорит, что создание задачи — POST. Живой шлюз говорит
 *    обратное. Верим шлюзу.
 *
 * 2) Список поставок. `GET /api/v1/supplies` отдал 405 — метод POST, с телом
 *    фильтра и параметрами limit/offset в строке запроса.
 *
 * 3) Контроль шлюза статистики. В пробе №2 контролем был выбран
 *    `/api/v1/supplier/stocks` — а он сам отключён (404 «This method is
 *    deprecated», release-notes 494), это моя ошибка выбора: в наших же
 *    `docs/wb_api_test_plan.md` отключение стояло на 23 июня. Рабочим контролем
 *    оказался `/ping` = 200. Здесь контроль оставлен только как `/ping`.
 *
 * 4) Добавлен блок сторожа legacy-семейства `/api/v1/supplier/*`: заказы и
 *    продажи живут на том же шлюзе, что и уже снятые остатки и поставки.
 *    Проверяем, не появилась ли на них пометка об отключении.
 *
 * ПРАВКИ ПОСЛЕ РЕВЮ АУДИТОРА (17.08.2026)
 *
 *   • Подсказки в журнале переведены в нейтральную форму: проба фиксирует
 *     наблюдение, а не назначает ему причину. Пустая выгрузка приёмки означает
 *     «источник не подтверждён», а не «приёмка бесплатная».
 *   • Список поставок: печатается признак усечения выборки. Если пришло ровно
 *     `limit` строк, «самая старая поставка» НЕ считается доказанной.
 *   • Даты поставки больше не сливаются в одно поле через `||`. `createDate`,
 *     `supplyDate` и `factDate` — три разных события, журнал показывает границы
 *     по каждому отдельно. Опорной для «физически поступило» берётся `factDate`,
 *     подмена другим полем пишется в журнал явно.
 *   • Пара `quantity` / `acceptedQuantity` названа кандидатом на «передано /
 *     принято». Бухгалтерской семантикой она станет только после сверки с
 *     шестью поставками июля, которые владелец помнит поимённо.
 *   • Блок D сделан contract canary: классифицирует не только код ответа, но и
 *     структуру тела. `200` означает «маршрут сейчас операционно доступен» и
 *     ничего не говорит о будущей дате отключения.
 *
 * БЕЗОПАСНОСТЬ. Только чтение. Токен в журнал не печатается — печатается имя
 * свойства. BigQuery и листы не трогаются.
 *
 * ЗАПУСК — ДВЕ ОТДЕЛЬНЫЕ ФУНКЦИИ, чтобы не упереться в 6 минут Apps Script:
 *   probeAcceptance3()  — блок B, история приёмки (идёт до ~3 минут)
 *   probeSupplies3()    — блоки C и D, поставки и сторож legacy (до ~1 минуты)
 * ══════════════════════════════════════════════════════════════
 */

var P3_HOST_STAT_      = 'https://statistics-api.wildberries.ru';
var P3_HOST_ANALYTICS_ = 'https://seller-analytics-api.wildberries.ru';
var P3_HOST_SUPPLIES_  = 'https://supplies-api.wildberries.ru';

var P3_KEYS_ANALYTICS_ = ['WB_TOKEN_ANALYTICS', 'WB_TOKEN_STATISTICS'];
var P3_KEYS_STAT_      = ['WB_TOKEN_STATISTICS', 'WB_TOKEN_ANALYTICS'];
var P3_KEYS_SUPPLIES_  = ['WB_TOKEN_SUPPLIES'];

/** Окна для замера глубины приёмки: свежее + самое старое. */
var P3_WINDOW_RECENT_ = { from: '', till: '' };          // считается от сегодня
var P3_WINDOW_OLD_    = { from: '2024-09-01', till: '2024-09-30' };

/** Размер страницы списка поставок. Проба ходит одной страницей и печатает признак усечения. */
var P3_SUPPLIES_LIMIT_ = 1000;

// ── утилиты ───────────────────────────────────────────────────

function p3Log_(s) { Logger.log(s); }

function p3Token_(keys) {
  var sp = PropertiesService.getScriptProperties();
  var up = null;
  try { up = PropertiesService.getUserProperties(); } catch (e) { up = null; }
  for (var i = 0; i < keys.length; i++) {
    var k = keys[i], v = '';
    try { v = (sp && sp.getProperty(k)) || ''; } catch (e) { v = ''; }
    if (!v && up) { try { v = up.getProperty(k) || ''; } catch (e2) {} }
    v = String(v || '').trim();
    if (v) return { token: v, keyName: k };
  }
  return { token: '', keyName: '' };
}

function p3Call_(label, method, url, token, payload, quiet) {
  var opt = {
    method: method,
    muteHttpExceptions: true,
    followRedirects: true,
    headers: { 'Authorization': token }
  };
  if (payload !== undefined && payload !== null) {
    opt.contentType = 'application/json';
    opt.payload = JSON.stringify(payload);
  }
  var code = -1, body = '', err = '';
  try {
    var resp = UrlFetchApp.fetch(url, opt);
    code = resp.getResponseCode();
    body = resp.getContentText();
  } catch (e) { err = String(e); }

  p3Log_('--- ' + label);
  p3Log_('    ' + method.toUpperCase() + ' ' + url);
  if (payload !== undefined && payload !== null) p3Log_('    тело запроса: ' + JSON.stringify(payload));
  if (err) { p3Log_('    ИСКЛЮЧЕНИЕ: ' + err); return { code: -1, body: '', json: null }; }
  p3Log_('    HTTP ' + code + ', тело ' + body.length + ' симв.');
  if (!quiet || code !== 200) p3Log_('    ' + body.substring(0, 300).replace(/\s+/g, ' '));

  var json = null;
  try { json = JSON.parse(body); } catch (e3) { json = null; }
  return { code: code, body: body, json: json };
}

function p3Ymd_(d) { return Utilities.formatDate(d, 'Europe/Moscow', 'yyyy-MM-dd'); }

function p3Keys_(o) { var a = []; for (var k in o) if (o.hasOwnProperty(k)) a.push(k); return a; }

/** Достаёт массив строк из ответа любой из встречающихся у WB форм. */
function p3Rows_(json) {
  if (json instanceof Array) return json;
  if (json && json.data instanceof Array) return json.data;
  if (json && json.report instanceof Array) return json.report;
  if (json && json.supplies instanceof Array) return json.supplies;
  if (json && json.goods instanceof Array) return json.goods;
  if (json && json.data && json.data.supplies instanceof Array) return json.data.supplies;
  if (json && json.data && json.data.goods instanceof Array) return json.data.goods;
  return null;
}

// ── БЛОК B. История приёмки ───────────────────────────────────

/** Полный задачный контур отчёта о приёмке за одно окно. Возвращает массив строк или null. */
function p3Acceptance_(label, token, dateFrom, dateTo) {
  var create = p3Call_('приёмка [' + label + '] · создание задачи', 'get',
    P3_HOST_ANALYTICS_ + '/api/v1/acceptance_report?dateFrom=' + dateFrom + '&dateTo=' + dateTo, token);

  var taskId = '';
  if (create.json) {
    if (create.json.data && create.json.data.taskId) taskId = String(create.json.data.taskId);
    else if (create.json.taskId) taskId = String(create.json.taskId);
  }
  if (!taskId) { p3Log_('    [' + label + '] ❌ taskId не получен'); return null; }
  p3Log_('    [' + label + '] taskId = ' + taskId);

  var status = '', done = false;
  for (var i = 0; i < 15; i++) {
    Utilities.sleep(4000);
    var st = p3Call_('приёмка [' + label + '] · статус #' + (i + 1), 'get',
      P3_HOST_ANALYTICS_ + '/api/v1/acceptance_report/tasks/' + taskId + '/status', token, null, true);
    var s = '';
    if (st.json && st.json.data && st.json.data.status) s = String(st.json.data.status);
    else if (st.json && st.json.status) s = String(st.json.status);
    status = s || status;
    p3Log_('    статус: ' + (s || '(не разобран)'));
    if (/^(done|ready|success)$/i.test(s)) { done = true; break; }
    if (/^(canceled|purged|error|failed)$/i.test(s)) break;
  }
  if (!done) { p3Log_('    [' + label + '] ❌ задача не дошла до done, последний статус: ' + status); return null; }

  Utilities.sleep(2000);
  var dl = p3Call_('приёмка [' + label + '] · выгрузка', 'get',
    P3_HOST_ANALYTICS_ + '/api/v1/acceptance_report/tasks/' + taskId + '/download', token, null, true);

  var rows = p3Rows_(dl.json);
  if (!rows) {
    p3Log_('    [' + label + '] ❌ выгрузка не разобралась в массив; ключи: ' +
           (dl.json ? p3Keys_(dl.json).join(', ') : '(не JSON)'));
    return null;
  }
  p3SummarizeAcceptance_(label, rows);
  return rows;
}

function p3SummarizeAcceptance_(label, arr) {
  p3Log_('    ═══ ИТОГ [' + label + '] ═══');
  p3Log_('    строк: ' + arr.length);
  if (!arr.length) return;

  p3Log_('    поля: ' + p3Keys_(arr[0]).join(', '));
  p3Log_('    первая строка: ' + JSON.stringify(arr[0]).substring(0, 400));
  p3Log_('    последняя строка: ' + JSON.stringify(arr[arr.length - 1]).substring(0, 400));

  var minD = '', maxD = '', sumCnt = 0, sumTotal = 0, inc = {}, nm = {};
  for (var i = 0; i < arr.length; i++) {
    var r = arr[i] || {};
    var d = String(r.shkCreateDate || r.giCreateDate || '');
    if (d) { if (!minD || d < minD) minD = d; if (!maxD || d > maxD) maxD = d; }
    if (typeof r.count === 'number') sumCnt += r.count;
    if (typeof r.total === 'number') sumTotal += r.total;
    if (r.incomeId !== undefined && r.incomeId !== null) inc[String(r.incomeId)] = 1;
    if (r.nmID !== undefined && r.nmID !== null) nm[String(r.nmID)] = 1;
  }
  p3Log_('    даты: ' + (minD || '—') + ' … ' + (maxD || '—'));
  p3Log_('    сумма count: ' + sumCnt + ' · сумма total: ' + sumTotal);
  p3Log_('    уникальных incomeId: ' + p3Keys_(inc).length + ' · уникальных nmID: ' + p3Keys_(nm).length);
}

function probeAcceptance3() {
  var t0 = new Date().getTime();
  p3Log_('=== PROBE-3 · БЛОК B — история приёмки ===');
  p3Log_('старт: ' + Utilities.formatDate(new Date(), 'Europe/Moscow', 'yyyy-MM-dd HH:mm:ss'));

  var t = p3Token_(P3_KEYS_ANALYTICS_);
  if (!t.token) { p3Log_('❌ нет токена аналитики: ' + P3_KEYS_ANALYTICS_.join(' / ')); return; }
  p3Log_('токен взят из свойства: ' + t.keyName);

  var now = new Date();
  var from = new Date(now.getTime() - 30 * 24 * 3600 * 1000);
  var recentFrom = P3_WINDOW_RECENT_.from || p3Ymd_(from);
  var recentTill = P3_WINDOW_RECENT_.till || p3Ymd_(now);

  p3Acceptance_('свежее окно ' + recentFrom + '…' + recentTill, t.token, recentFrom, recentTill);

  p3Log_('');
  p3Log_('пауза 65 с — лимит WB «1 запрос в минуту» на создание отчёта');
  Utilities.sleep(65000);

  p3Acceptance_('глубина ' + P3_WINDOW_OLD_.from + '…' + P3_WINDOW_OLD_.till,
                t.token, P3_WINDOW_OLD_.from, P3_WINDOW_OLD_.till);

  p3Log_('');
  p3Log_('=== БЛОК B ГОТОВ, ' + Math.round((new Date().getTime() - t0) / 1000) + ' с ===');
  p3Log_('Читаем так — это наблюдения, причины устанавливаются отдельно:');
  p3Log_('  строки за сентябрь 2024 есть → метод отдаёт историю на нашу глубину, кандидат в источники подтверждён.');
  p3Log_('  строк за 2024 нет, за 2026 есть → глубина метода ограничена; насколько — меряем отдельными окнами.');
  p3Log_('  нет строк ни в одном окне → acceptance_report НЕ подтверждён как источник нашей истории.');
  p3Log_('    Причина не назначается: это может быть и семантика отчёта, и отсутствие записей в выбранных');
  p3Log_('    окнах, и иная структура выгрузки. Выбор источника переносится на блок C, причина — в отдельную проверку.');
}

// ── БЛОК C. Поставки  +  БЛОК D. Сторож legacy ────────────────

/**
 * Разбор списка поставок.
 * Границы считаются ОТДЕЛЬНО по каждому полю даты — createDate, supplyDate,
 * factDate суть три разных события, и молчаливый fallback между ними исказил бы
 * ответ на вопрос «когда товар физически поступил».
 * Возвращает опорную пару поставок и признак усечения выборки.
 */
function p3SummarizeSupplies_(arr, limit) {
  p3Log_('    ═══ ИТОГ по списку поставок ═══');
  p3Log_('    получено строк: ' + arr.length + ' при limit=' + limit + ', offset=0');

  var truncated = (arr.length >= limit);
  if (truncated) {
    p3Log_('    ⚠️ ВЫБОРКА ПОТЕНЦИАЛЬНО УСЕЧЕНА: пришло ровно limit.');
    p3Log_('    ⚠️ «Самая старая поставка» ниже НЕ считается доказанной — нужна пагинация по offset.');
  } else {
    p3Log_('    выборка не усечена: пришло меньше limit, это вся история по заданному фильтру.');
  }
  if (!arr.length) return null;

  p3Log_('    поля: ' + p3Keys_(arr[0]).join(', '));
  p3Log_('    первая: ' + JSON.stringify(arr[0]).substring(0, 400));

  // границы по каждому полю даты отдельно
  var fields = ['factDate', 'supplyDate', 'createDate', 'updatedDate'];
  var ends = {};
  for (var f = 0; f < fields.length; f++) {
    var fld = fields[f], minD = '', maxD = '', oldest = null, newest = null, filled = 0;
    for (var i = 0; i < arr.length; i++) {
      var r = arr[i] || {};
      var d = (r[fld] === undefined || r[fld] === null) ? '' : String(r[fld]);
      if (!d) continue;
      filled++;
      if (!minD || d < minD) { minD = d; oldest = r; }
      if (!maxD || d > maxD) { maxD = d; newest = r; }
    }
    ends[fld] = { min: minD, max: maxD, oldest: oldest, newest: newest, filled: filled };
    p3Log_('    ' + fld + ': заполнено ' + filled + ' из ' + arr.length +
           ' · ' + (minD || '—') + ' … ' + (maxD || '—'));
  }

  var byStatus = {};
  for (var j = 0; j < arr.length; j++) {
    var s = String(arr[j] && arr[j].statusID !== undefined ? arr[j].statusID : '(нет)');
    byStatus[s] = (byStatus[s] || 0) + 1;
  }
  var parts = [];
  for (var k in byStatus) if (byStatus.hasOwnProperty(k)) parts.push('statusID ' + k + ': ' + byStatus[k]);
  p3Log_('    по статусам — ' + parts.join(' · '));

  // опорное поле: фактическая дата. Подмена — только явно и в журнал.
  var anchor = 'factDate';
  if (!ends.factDate.filled) {
    anchor = ends.supplyDate.filled ? 'supplyDate' : (ends.createDate.filled ? 'createDate' : '');
    p3Log_('    ⚠️ factDate не заполнен ни в одной строке. Опорным полем взят: ' +
           (anchor || '(нет ни одного) — границы не определены'));
    p3Log_('    ⚠️ Это РЕКОНСТРУКЦИЯ: выбранное поле означает другое событие, чем физическое поступление.');
  } else {
    p3Log_('    опорное поле для «физически поступило»: factDate');
  }
  if (!anchor) return null;

  return {
    anchorField: anchor,
    truncated: truncated,
    oldest: ends[anchor].oldest,
    newest: ends[anchor].newest
  };
}

function p3SupplyGoods_(label, token, id) {
  var url = P3_HOST_SUPPLIES_ + '/api/v1/supplies/' + id + '/goods?limit=1000&offset=0';
  var r = p3Call_('состав поставки [' + label + '] id=' + id, 'post', url, token, {});
  if (r.code === 405) r = p3Call_('состав поставки [' + label + '] id=' + id + ' · повтор GET', 'get', url, token);

  var rows = p3Rows_(r.json);
  if (!rows) { p3Log_('    ❌ состав не разобрался; ключи: ' + (r.json ? p3Keys_(r.json).join(', ') : '(не JSON)')); return; }

  p3Log_('    позиций: ' + rows.length);
  if (!rows.length) return;
  p3Log_('    поля: ' + p3Keys_(rows[0]).join(', '));
  p3Log_('    первая позиция: ' + JSON.stringify(rows[0]).substring(0, 400));

  var q = 0, acc = 0, unl = 0;
  for (var i = 0; i < rows.length; i++) {
    var g = rows[i] || {};
    if (typeof g.quantity === 'number') q += g.quantity;
    if (typeof g.acceptedQuantity === 'number') acc += g.acceptedQuantity;
    if (typeof g.unloadingQuantity === 'number') unl += g.unloadingQuantity;
  }
  p3Log_('    quantity: ' + q + ' · acceptedQuantity: ' + acc + ' · unloadingQuantity: ' + unl);
  p3Log_('    ⚠️ Это КАНДИДАТ на пару «передано / принято», а не установленная семантика.');
  p3Log_('    ⚠️ Открыто: в какой момент WB фиксирует acceptedQuantity и меняется ли поле задним числом.');
  p3Log_('    Проверяется сверкой с шестью поставками июля, которые владелец помнит поимённо.');
}

/**
 * Contract canary: классифицирует не только код ответа, но и структуру тела.
 * Uptime-проверка «200 или нет» пропустила бы и молчаливый дрейф формата,
 * и подмену массива объектом-обёрткой.
 */
function p3Canary_(label, url, token) {
  var r = p3Call_(label, 'get', url, token, null, true);
  var verdict = '', detail = '';

  if (r.code === 200) {
    var rows = p3Rows_(r.json);
    if (rows) {
      verdict = 'ЖИВОЙ';
      detail = 'массив, строк ' + rows.length +
               (rows.length ? ', поля: ' + p3Keys_(rows[0]).join(', ') : ' (окно пустое — это не отказ)');
    } else if (r.json) {
      verdict = '⚠️ ДРЕЙФ СТРУКТУРЫ';
      detail = '200, но массива нет; ключи: ' + p3Keys_(r.json).join(', ');
    } else {
      verdict = '⚠️ ДРЕЙФ СТРУКТУРЫ';
      detail = '200, но тело не разобралось как JSON';
    }
  } else if (r.code === 404 && /deprecat/i.test(r.body)) {
    verdict = '🔴 СНЯТ';
    detail = 'шлюз вернул именную пометку deprecated — в теле есть ссылка на релиз-ноту';
  } else if (r.code === 404) {
    verdict = '🔴 МАРШРУТА НЕТ';
    detail = 'path not found — путь удалён вместе с заглушкой';
  } else if (r.code === 401 || r.code === 403) {
    verdict = '⚠️ ДОСТУП';
    detail = 'HTTP ' + r.code + ' — токен или категория, не состояние метода';
  } else if (r.code === 429) {
    verdict = 'ЛИМИТ';
    detail = '429 — о состоянии метода не говорит, повторить позже';
  } else if (r.code >= 500) {
    verdict = '⚠️ СБОЙ ШЛЮЗА';
    detail = 'HTTP ' + r.code + ' — временное, повторить';
  } else {
    verdict = '⚠️ НЕОЖИДАННЫЙ ОТВЕТ';
    detail = 'HTTP ' + r.code;
  }

  p3Log_('    ВЕРДИКТ: ' + verdict + ' · ' + detail);
  return { verdict: verdict, code: r.code };
}

function probeSupplies3() {
  var t0 = new Date().getTime();
  p3Log_('=== PROBE-3 · БЛОК C — поставки, БЛОК D — сторож legacy ===');
  p3Log_('старт: ' + Utilities.formatDate(new Date(), 'Europe/Moscow', 'yyyy-MM-dd HH:mm:ss'));

  var t = p3Token_(P3_KEYS_SUPPLIES_);
  if (!t.token) { p3Log_('❌ нет токена WB_TOKEN_SUPPLIES'); }
  else {
    p3Log_('токен поставок взят из свойства: ' + t.keyName);

    // C1 — список поставок, POST с пустым фильтром
    var url = P3_HOST_SUPPLIES_ + '/api/v1/supplies?limit=' + P3_SUPPLIES_LIMIT_ + '&offset=0';
    var r = p3Call_('C1 · список поставок, фильтр пустой', 'post', url, t.token, {});

    // C1b — если пустой фильтр не принят, задаём диапазон дат явно
    if (r.code === 400 || r.code === 422) {
      Utilities.sleep(1500);
      r = p3Call_('C1b · список поставок, фильтр по датам', 'post', url, t.token, {
        dates: [{ from: '2024-09-01', till: p3Ymd_(new Date()), type: 'factDate' }]
      });
    }

    var rows = p3Rows_(r.json);
    if (!rows) {
      p3Log_('    ❌ список не разобрался; ключи: ' + (r.json ? p3Keys_(r.json).join(', ') : '(не JSON)'));
    } else {
      var ends = p3SummarizeSupplies_(rows, P3_SUPPLIES_LIMIT_);

      // C2 — карточка самой старой поставки: есть ли там склад
      if (ends && ends.oldest && ends.oldest.supplyID) {
        Utilities.sleep(1500);
        var dUrl = P3_HOST_SUPPLIES_ + '/api/v1/supplies/' + ends.oldest.supplyID;
        var d = p3Call_('C2 · карточка самой старой поставки', 'get', dUrl, t.token);
        if (d.code === 405) p3Call_('C2b · карточка, повтор POST', 'post', dUrl, t.token, {});
      }

      // C3 — состав самой старой и самой свежей поставки
      var mark = (ends && ends.truncated) ? ' ⚠️ в усечённой выборке' : '';
      if (ends && ends.oldest && ends.oldest.supplyID) {
        Utilities.sleep(1500);
        p3SupplyGoods_('самая ранняя по ' + ends.anchorField + mark, t.token, ends.oldest.supplyID);
      }
      if (ends && ends.newest && ends.newest.supplyID &&
          (!ends.oldest || ends.newest.supplyID !== ends.oldest.supplyID)) {
        Utilities.sleep(1500);
        p3SupplyGoods_('самая поздняя по ' + ends.anchorField + mark, t.token, ends.newest.supplyID);
      }
    }
  }

  // ── БЛОК D. Сторож legacy-семейства ─────────────────────────
  p3Log_('');
  p3Log_('### БЛОК D — сторож legacy /api/v1/supplier/*');
  p3Log_('остатки и поставки из этого семейства уже сняты; проверяем заказы и продажи');
  var ts = p3Token_(P3_KEYS_STAT_);
  if (!ts.token) { p3Log_('❌ нет токена статистики'); }
  else {
    var since = Utilities.formatDate(new Date(new Date().getTime() - 3600 * 1000),
                                     'Europe/Moscow', "yyyy-MM-dd'T'HH:mm:ss");
    p3Canary_('D1 · заказы',
      P3_HOST_STAT_ + '/api/v1/supplier/orders?dateFrom=' + since + '&flag=0', ts.token);
    Utilities.sleep(2000);
    p3Canary_('D2 · продажи',
      P3_HOST_STAT_ + '/api/v1/supplier/sales?dateFrom=' + since + '&flag=0', ts.token);
  }

  p3Log_('');
  p3Log_('=== БЛОКИ C и D ГОТОВЫ, ' + Math.round((new Date().getTime() - t0) / 1000) + ' с ===');
  p3Log_('Читаем так — это наблюдения, причины устанавливаются отдельно:');
  p3Log_('  C1 = 200, выборка не усечена, factDate уходит в 2024 → метод отдаёт историю поставок на нашу глубину.');
  p3Log_('  C1 = 200, но выборка усечена → глубину подтвердит только проход по offset до конца.');
  p3Log_('  quantity против acceptedQuantity — кандидат на пару «передано / принято».');
  p3Log_('    Семантикой станет после сверки с шестью поставками июля: Тула, Волгоград, Сарапул,');
  p3Log_('    Владимир, Самара, Екатеринбург.');
  p3Log_('  D1/D2 = ЖИВОЙ → маршрут СЕЙЧАС операционно доступен. О будущей дате отключения это не говорит ничего.');
  p3Log_('  D1/D2 = СНЯТ или ДРЕЙФ → искать замену немедленно: на этих методах стоит операционная витрина.');
}
