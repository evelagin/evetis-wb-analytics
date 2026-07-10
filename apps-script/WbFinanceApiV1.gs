/**
 * WbFinanceApiV1.gs — Фаза 0, ШАГ 1 (разведка + sales-reports/list)
 * ════════════════════════════════════════════════════════════════
 * Назначение: миграция финансов на новые методы категории «Финансы»
 *   (старый GET /api/v5/supplier/reportDetailByPeriod отключается 15.07.2026).
 *
 * Файл состоит из двух частей:
 *   А. РАЗВЕДКА (только чтение/логи):
 *      1) showWbFinanceV1ReportsList() — sales-reports/list, фактические периоды reportId.
 *      2) wbFinanceV1DetailedSample() — реальные имена полей detailed.
 *   Б. ЗАГРУЗЧИК (ПИШЕТ в RAW_WB_FINANCE):
 *      wbFinV1ImportReport_ / wbFinanceV1ImportOneReportTest — импорт одного reportId
 *      через существующий конвейер (normalizeFinanceApiRows_ + appendFinanceRows_),
 *      с replace-slice ПО reportId (удаляет только свои строки source_api=WB_API_FIN_V1
 *      с этим report_id) и парсингом string-сумм. Запись additive и обратима по метке.
 *
 * ПРЕДУСЛОВИЕ: токен категории «Финансы» в Script Properties под ключом
 *   WB_TOKEN_FINANCE (Project Settings → Script Properties).
 */

// ── Константы (если first-run даст 404/DNS — правим здесь и пробуем альтернативу) ──
var WB_FIN_V1_BASE_      = 'https://finance-api.wildberries.ru';
var WB_FIN_V1_LIST_PATH_ = '/api/finance/v1/sales-reports/list';
var WB_FIN_V1_DET_PATH_  = '/api/finance/v1/sales-reports/detailed/'; // + reportId
// Альтернативы хоста/пути на случай 404 (проверяем по очереди при ошибке):
//   https://finance-api.wildberries.ru/api/v1/sales-reports/list
//   https://common-api.wildberries.ru/api/finance/v1/sales-reports/list

var WB_FIN_V1_TOKEN_KEYS_ = ['WB_TOKEN_FINANCE']; // строго токен категории «Финансы», без fallback
var WB_FIN_V1_SAMPLE_LIMIT_ = 10;

// Период для list. dateFrom специально пораньше, чтобы сразу увидеть, отдаёт ли API осень 2024.
// (по доке детализация — с 01.01.2025; проверяем фактически). Пусто в TO = сегодня.
var WB_FIN_V1_LIST_FROM_ = '2024-09-01';
var WB_FIN_V1_LIST_TO_   = '';

/** Чтение токена «Финансы» из Script Properties. */
function getFinanceV1Token_() {
  var props = PropertiesService.getScriptProperties();
  for (var i = 0; i < WB_FIN_V1_TOKEN_KEYS_.length; i++) {
    var t = props.getProperty(WB_FIN_V1_TOKEN_KEYS_[i]);
    if (t) return { token: t, key: WB_FIN_V1_TOKEN_KEYS_[i] };
  }
  return null;
}

/** Универсальный POST к новому API «Финансы». Возвращает {code, body, json}. */
function wbFinV1Post_(token, path, bodyObj) {
  var url = WB_FIN_V1_BASE_ + path;
  var resp = UrlFetchApp.fetch(url, {
    method: 'post',
    contentType: 'application/json',
    headers: { 'Authorization': token },
    payload: JSON.stringify(bodyObj || {}),
    muteHttpExceptions: true
  });
  var code = resp.getResponseCode();
  var body = resp.getContentText();
  var json = null;
  try { json = JSON.parse(body); } catch (e) {}
  return { code: code, body: body, json: json, url: url };
}

/**
 * ШАГ 1.1 — список отчётов реализации (фактическая глубина по кабинету).
 * Запускать из редактора Apps Script: выбрать функцию → Run.
 */
function showWbFinanceV1ReportsList() {
  var tk = getFinanceV1Token_();
  if (!tk) {
    console.log('❌ Нет токена. Задайте Script Property WB_TOKEN_FINANCE (категория «Финансы»).');
    return;
  }
  console.log('Токен из ключа: ' + tk.key);

  // API требует обязательные dateFrom/dateTo (подтверждено ответом 400 FinancialGeneralReportRequestV3).
  // Если эти camelCase-имена не подойдут — пробуем PascalCase DateFrom/DateTo.
  var to = WB_FIN_V1_LIST_TO_ || Utilities.formatDate(new Date(), 'Europe/Moscow', 'yyyy-MM-dd');
  var body = { dateFrom: WB_FIN_V1_LIST_FROM_, dateTo: to };

  console.log('POST ' + WB_FIN_V1_BASE_ + WB_FIN_V1_LIST_PATH_ + '  body=' + JSON.stringify(body));
  var r = wbFinV1Post_(tk.token, WB_FIN_V1_LIST_PATH_, body);
  console.log('HTTP ' + r.code);

  if (r.code !== 200) {
    console.log('Ответ (первые 500 симв.): ' + String(r.body).substring(0, 500));
    console.log('→ Если 404/DNS — поправьте WB_FIN_V1_BASE_/PATH (см. альтернативы в шапке файла).');
    console.log('→ Если 400 — в теле обычно указано, какие поля нужны (добавим в body).');
    console.log('→ Если 401/403 — токен не той категории (нужна «Финансы»).');
    return;
  }

  // Структура ответа точно не известна — печатаем как есть и пытаемся разобрать.
  var arr = Array.isArray(r.json) ? r.json : (r.json && r.json.data ? r.json.data : null);
  if (!arr) {
    console.log('Ответ 200, но структура не распознана. RAW (первые 800 симв.):');
    console.log(String(r.body).substring(0, 800));
    return;
  }

  console.log('Получено отчётов: ' + arr.length);
  console.log('Ключи первой записи: ' + (arr[0] ? Object.keys(arr[0]).join(', ') : '—'));

  var minD = null, maxD = null;
  for (var i = 0; i < arr.length; i++) {
    var o = arr[i];
    // Имена полей подтвердим по ключам выше; пробуем распространённые варианты.
    var rid = o.reportId || o.id || o.realizationreport_id || '';
    var df  = o.dateFrom || o.from || o.periodFrom || o.createDt || '';
    var dt  = o.dateTo   || o.to   || o.periodTo   || '';
    console.log('  reportId=' + rid + '  период ' + df + ' … ' + dt);
    if (df && (!minD || df < minD)) minD = df;
    if (dt && (!maxD || dt > maxD)) maxD = dt;
  }
  console.log('━━━ ФАКТИЧЕСКАЯ ГЛУБИНА: ' + (minD || '?') + ' … ' + (maxD || '?') + ' ━━━');
  console.log('Скопируйте этот вывод в чат — по нему пишем detailed-загрузчик и план бэкфилла.');
}

/**
 * ШАГ 1.2 — образец detailed по одному reportId: показывает РЕАЛЬНЫЕ имена полей.
 * Перед запуском впишите REPORT_ID ниже (берётся из вывода showWbFinanceV1ReportsList).
 */
// Образцы detailed: свежий отчёт (чистые имена полей) + самый ранний 2024 (проверка глубины detailed).
var WB_FIN_V1_SAMPLE_REPORT_IDS_ = ['757272781', '273365321'];

function wbFinanceV1DetailedSample() {
  var tk = getFinanceV1Token_();
  if (!tk) { console.log('❌ Нет токена WB_TOKEN_FINANCE.'); return; }

  for (var k = 0; k < WB_FIN_V1_SAMPLE_REPORT_IDS_.length; k++) {
    var rid = WB_FIN_V1_SAMPLE_REPORT_IDS_[k];
    console.log('═══════════ reportId ' + rid + ' ═══════════');

    var path = WB_FIN_V1_DET_PATH_ + encodeURIComponent(rid);
    var body = { rrdid: 0, limit: WB_FIN_V1_SAMPLE_LIMIT_ }; // пагинация по rrdid
    console.log('POST ' + WB_FIN_V1_BASE_ + path + '  body=' + JSON.stringify(body));

    var r = wbFinV1Post_(tk.token, path, body);
    console.log('HTTP ' + r.code);
    if (r.code !== 200) {
      console.log('Ответ (первые 500): ' + String(r.body).substring(0, 500));
      continue;
    }

    var arr = Array.isArray(r.json) ? r.json : (r.json && r.json.data ? r.json.data : null);
    if (!arr || !arr.length) {
      console.log('200, но строк 0 → detailed НЕ отдаёт этот период. RAW(300): ' + String(r.body).substring(0, 300));
      continue;
    }

    console.log('Строк в образце: ' + arr.length);
    console.log('── ИМЕНА ПОЛЕЙ (camelCase) ──');
    console.log(Object.keys(arr[0]).join(', '));
    console.log('── ПЕРВАЯ СТРОКА (проверка сумм-строк/дат) ──');
    console.log(JSON.stringify(arr[0], null, 2).substring(0, 1800));
  }
  console.log('Скопируйте весь вывод в чат — по нему делаем точный маппинг в RAW_WB_FINANCE.');
}


// ════════════════════════════════════════════════════════════════
// ПРОДАКШН-ЗАГРУЗЧИК (через существующий конвейер записи RAW_WB_FINANCE)
// ════════════════════════════════════════════════════════════════
// Принцип: новые camelCase-поля → старые имена (которые знает
// FINANCE_API_FIELD_MAP_ в Wbfinanceloader) + парсинг строк-сумм в числа,
// затем переиспользуем normalizeFinanceApiRows_ / row_hash-дедуп / append.
// БЕЗ period-clear: грузим по reportId, идемпотентность — по row_hash (rrd_id).

var WB_FIN_V1_PAGE_LIMIT_   = 100000;
var WB_FIN_V1_SOURCE_TAG_   = 'WB_API_FIN_V1';   // метка source_api, чтобы отличать от старого метода

/** Парсинг денежной строки WB ("45.34" / "0" / "1 234,5") → число. */
function parseMoneyV1_(v) {
  if (v === null || v === undefined || v === '') return 0;
  if (typeof v === 'number') return v;
  var s = String(v).replace(/\s/g, '').replace(',', '.');
  var n = Number(s);
  return isNaN(n) ? 0 : n;
}

/** Тянет ВСЕ строки detailed по одному reportId (пагинация по rrdId). */
function wbFinV1FetchDetailedAll_(token, reportId) {
  var all = [];
  var rrdid = 0, pages = 0, guard = 0;
  while (guard++ < 500) {
    var path = WB_FIN_V1_DET_PATH_ + encodeURIComponent(reportId);
    var r = wbFinV1Post_(token, path, { rrdid: rrdid, limit: WB_FIN_V1_PAGE_LIMIT_ });
    if (r.code === 429) { Utilities.sleep(20000); continue; }
    if (r.code === 204) break; // нет данных — нормальное завершение пагинации
    if (r.code !== 200) return { ok: false, data: all, error: 'HTTP ' + r.code + ': ' + String(r.body).substring(0, 200) };
    var arr = Array.isArray(r.json) ? r.json : (r.json && r.json.data ? r.json.data : []);
    if (!arr || !arr.length) break;
    for (var i = 0; i < arr.length; i++) all.push(arr[i]);
    pages++;
    var last = arr[arr.length - 1];
    var lastId = Number(last.rrdId || 0);
    if (!lastId || lastId === rrdid) break;
    rrdid = lastId;
    if (arr.length < WB_FIN_V1_PAGE_LIMIT_) break;
    Utilities.sleep(500);
  }
  return { ok: true, data: all, pages: pages };
}

/** Адаптер: строка нового API (camelCase) → объект со СТАРЫМИ именами (для FINANCE_API_FIELD_MAP_), суммы — числа. */
function wbFinV1AdaptRow_(o) {
  return {
    rrd_id: o.rrdId, srid: o.srid, shk_id: o.shkId, sticker_id: o.stickerId,
    realizationreport_id: o.reportId, gi_id: o.giId,
    doc_type_name: o.docTypeName, supplier_oper_name: o.sellerOperName,
    order_dt: o.orderDt, sale_dt: o.saleDt, rr_dt: o.rrDate, create_dt: o.createDate,
    nm_id: o.nmId, sa_name: o.vendorCode, barcode: o.sku, ts_name: o.techSize,
    brand_name: o.brandName, subject_name: o.subjectName, office_name: o.officeName,
    site_country: o.country,
    retail_price: parseMoneyV1_(o.retailPrice),
    retail_amount: parseMoneyV1_(o.retailAmount),
    retail_price_withdisc_rub: parseMoneyV1_(o.retailPriceWithDisc),
    sale_percent: o.salePercent, commission_percent: o.commissionPercent,
    product_discount_for_report: o.productDiscountForReport, supplier_promo: o.sellerPromo,
    ppvz_spp_prc: o.spp, quantity: o.quantity,
    ppvz_for_pay: parseMoneyV1_(o.forPay),
    ppvz_vw: parseMoneyV1_(o.vw),
    ppvz_sales_commission: parseMoneyV1_(o.ppvzSalesCommission),
    delivery_rub: parseMoneyV1_(o.deliveryService),     // ЛОГИСТИКА-деньги (не deliveryAmount!)
    storage_fee: parseMoneyV1_(o.paidStorage),
    deduction: parseMoneyV1_(o.deduction),
    penalty: parseMoneyV1_(o.penalty),
    acceptance: parseMoneyV1_(o.paidAcceptance),
    additional_payment: parseMoneyV1_(o.additionalPayment),
    acquiring_fee: parseMoneyV1_(o.acquiringFee),
    rebill_logistic_cost: parseMoneyV1_(o.rebillLogisticCost),
    currency_name: o.currency
  };
}

/**
 * Импорт ОДНОГО отчёта реализации в RAW_WB_FINANCE через существующий конвейер.
 * @return {Object} результат с контрольными суммами.
 */
function wbFinV1ImportReport_(reportId, reportFrom, reportTo, skuIndexOpt) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var stamp = Utilities.formatDate(new Date(), 'Europe/Moscow', 'yyyyMMdd_HHmmss');
  var res = { reportId: reportId, fetched: 0, written: 0, cleared: 0, status: 'ERROR', error: '',
              sums: { forPay: 0, retail: 0, logistics: 0, storage: 0, deduction: 0, penalty: 0, acceptance: 0, salesQty: 0 },
              unmapped: {} };

  var tk = getFinanceV1Token_();
  if (!tk) { res.error = 'Нет токена WB_TOKEN_FINANCE'; return res; }
  var rawSheet = getRawFinanceSheet_(ss);
  if (!rawSheet) { res.error = 'Лист RAW_WB_FINANCE не найден'; return res; }

  var fetched = wbFinV1FetchDetailedAll_(tk.token, reportId);
  if (!fetched.ok) { res.error = fetched.error; return res; }
  res.fetched = fetched.data.length;
  if (!fetched.data.length) { res.status = 'OK'; res.error = 'Пусто'; return res; }

  // Адаптация + контрольные суммы (по фактическим строкам отчёта)
  var KNOWN_OPERS_ = { 'Продажа':1,'Возврат':1,'Логистика':1,'Хранение':1,'Штраф':1,'Удержание':1,
    'Платная приемка':1,'Пересчет платной приемки':1,'Корректная продажа':1,'Логистика сторно':1,
    'Сторно продаж':1,'Сторно возвратов':1,'Авансовая оплата за товар без движения':1,'Оплата брака':1,
    'Оплата потерянного товара':1,'Компенсация подмененного товара':1,'Возмещение издержек по перевозу/по складу':1 };
  var adapted = [];
  for (var i = 0; i < fetched.data.length; i++) {
    var raw = fetched.data[i];
    var a = wbFinV1AdaptRow_(raw);
    adapted.push(a);
    res.sums.forPay     += a.ppvz_for_pay;
    res.sums.retail     += a.retail_amount;
    res.sums.logistics  += a.delivery_rub;
    res.sums.storage    += a.storage_fee;
    res.sums.deduction  += a.deduction;
    res.sums.penalty    += a.penalty;
    res.sums.acceptance += a.acceptance;
    if (a.doc_type_name === 'Продажа') res.sums.salesQty += (Number(a.quantity) || 0);
    var op = a.supplier_oper_name || '';
    if (op && !KNOWN_OPERS_[op]) res.unmapped[op] = (res.unmapped[op] || 0) + 1; // UNMAPPED-диагностика
  }

  // Запись через существующий конвейер
  var rawLastCol = rawSheet.getLastColumn();
  var hMap = buildFinanceRawHeaderMap_(rawSheet, rawLastCol);

  // Безопасность: без source_api откат по метке невозможен → запись запрещена.
  if (hMap['source_api'] === undefined) {
    res.error = 'В RAW_WB_FINANCE нет колонки source_api — запись запрещена (откат по WB_API_FIN_V1 невозможен)';
    return res;
  }
  // Безопасность: нужна report_id для replace-slice по reportId.
  if (hMap['report_id'] === undefined) {
    res.error = 'В RAW_WB_FINANCE нет колонки report_id — replace-slice по reportId невозможен';
    return res;
  }

  var skuIndex = skuIndexOpt || ((typeof buildSkuIndex_ === 'function') ? buildSkuIndex_(ss) : null);
  var loadId = 'FIN_V1_' + stamp;
  var loadedAt = Utilities.formatDate(new Date(), 'Europe/Moscow', 'yyyy-MM-dd HH:mm:ss');

  var rows = normalizeFinanceApiRows_(adapted, hMap, rawLastCol, loadId, loadedAt, reportFrom || '', reportTo || '', '', skuIndex);

  // Метка источника + безопасный детерминированный row_hash = WB_API_FIN_V1|reportId|rrdId
  for (var j = 0; j < rows.length; j++) {
    rows[j][hMap['source_api']] = WB_FIN_V1_SOURCE_TAG_;
    var rrdVal = (hMap['rrd_id'] !== undefined) ? rows[j][hMap['rrd_id']] : '';
    var reportVal = (hMap['report_id'] !== undefined) ? rows[j][hMap['report_id']] : reportId;
    if (hMap['row_hash'] !== undefined) {
      rows[j][hMap['row_hash']] = financeMd5_(WB_FIN_V1_SOURCE_TAG_ + '|' + reportVal + '|' + rrdVal);
    }
  }

  // Replace-slice ПО reportId: удаляем только свои строки (WB_API_FIN_V1 + этот report_id), затем пишем fresh.
  // НЕ чистим по периоду — на неделю бывает несколько reportId.
  res.cleared = wbFinV1ClearOwnReport_(rawSheet, hMap, reportId);
  appendFinanceRows_(rawSheet, rows, rawLastCol);
  res.written = rows.length;
  res.status = 'OK';
  return res;
}

/** Удаляет из RAW_WB_FINANCE только строки source_api=WB_API_FIN_V1 И report_id=reportId. */
function wbFinV1ClearOwnReport_(rawSheet, hMap, reportId) {
  var lr = rawSheet.getLastRow();
  if (lr < 2) return 0;
  var srcCol = hMap['source_api'], ridCol = hMap['report_id'];
  if (srcCol === undefined || ridCol === undefined) return 0;
  var data = rawSheet.getRange(2, 1, lr - 1, rawSheet.getLastColumn()).getValues();
  var toDelete = [];
  for (var i = 0; i < data.length; i++) {
    if (String(data[i][srcCol]).trim() === WB_FIN_V1_SOURCE_TAG_ &&
        String(data[i][ridCol]).trim() === String(reportId)) {
      toDelete.push(i + 2); // абсолютный номер строки в листе
    }
  }
  for (var d = toDelete.length - 1; d >= 0; d--) rawSheet.deleteRow(toDelete[d]); // снизу вверх
  return toDelete.length;
}

/** Возвращает запись из list по reportId (для сверки недельных *Sum). */
function wbFinV1FindReportSums_(token, from, to, reportId) {
  var r = wbFinV1Post_(token, WB_FIN_V1_LIST_PATH_, { dateFrom: from, dateTo: to });
  if (r.code !== 200) return null;
  var arr = Array.isArray(r.json) ? r.json : (r.json && r.json.data ? r.json.data : []);
  for (var i = 0; i < arr.length; i++) {
    if (String(arr[i].reportId) === String(reportId)) return arr[i];
  }
  return null;
}

/**
 * ТЕСТ: импорт ОДНОГО отчёта + контрольные суммы (сверить с недельными итогами из list).
 * Меняй reportId ниже. Источник в RAW помечается 'WB_API_FIN_V1' — легко удалить при откате.
 */
var WB_FIN_V1_TEST_REPORT_ID_   = '757272781';
var WB_FIN_V1_TEST_REPORT_FROM_ = '2026-06-15';
var WB_FIN_V1_TEST_REPORT_TO_   = '2026-06-21';

function wbFinanceV1ImportOneReportTest() {
  console.log('Импорт reportId ' + WB_FIN_V1_TEST_REPORT_ID_ + ' (' + WB_FIN_V1_TEST_REPORT_FROM_ + '…' + WB_FIN_V1_TEST_REPORT_TO_ + ')');
  var r = wbFinV1ImportReport_(WB_FIN_V1_TEST_REPORT_ID_, WB_FIN_V1_TEST_REPORT_FROM_, WB_FIN_V1_TEST_REPORT_TO_);
  console.log('Статус: ' + r.status + (r.error ? ' (' + r.error + ')' : ''));
  console.log('Строк получено: ' + r.fetched + ' | удалено старых своих: ' + r.cleared + ' | записано: ' + r.written);
  console.log('━━━ КОНТРОЛЬНЫЕ СУММЫ (сверить с list: forPaySum / retailAmountSum / deliveryServiceSum / paidStorageSum / deductionSum / penaltySum / paidAcceptanceSum) ━━━');
  console.log('  forPay (к перечислению): ' + r.sums.forPay.toFixed(2));
  console.log('  retailAmount (реализация): ' + r.sums.retail.toFixed(2));
  console.log('  logistics (deliveryService): ' + r.sums.logistics.toFixed(2));
  console.log('  storage (paidStorage): ' + r.sums.storage.toFixed(2));
  console.log('  deduction: ' + r.sums.deduction.toFixed(2));
  console.log('  penalty: ' + r.sums.penalty.toFixed(2));
  console.log('  acceptance (paidAcceptance): ' + r.sums.acceptance.toFixed(2));
  console.log('  кол-во проданных (Продажа): ' + r.sums.salesQty);

  // Самопроверка: те же итоги из list по этому reportId
  var tk2 = getFinanceV1Token_();
  var ls = tk2 ? wbFinV1FindReportSums_(tk2.token, WB_FIN_V1_TEST_REPORT_FROM_, WB_FIN_V1_TEST_REPORT_TO_, WB_FIN_V1_TEST_REPORT_ID_) : null;
  if (ls) {
    console.log('━━━ ИТОГИ ИЗ LIST (должны совпасть с нашими detailed-суммами) ━━━');
    console.log('  forPaySum=' + ls.forPaySum + ' | retailAmountSum=' + ls.retailAmountSum +
      ' | deliveryServiceSum=' + ls.deliveryServiceSum + ' | paidStorageSum=' + ls.paidStorageSum +
      ' | deductionSum=' + ls.deductionSum + ' | penaltySum=' + ls.penaltySum +
      ' | paidAcceptanceSum=' + ls.paidAcceptanceSum);
  } else {
    console.log('(итоги из list по этому reportId не получены — сверь вручную с кабинетом)');
  }

  var uk = Object.keys(r.unmapped);
  console.log('UNMAPPED операций: ' + (uk.length ? uk.map(function(k){return k+'×'+r.unmapped[k];}).join(', ') : 'нет (все sellerOperName распознаны)'));
  console.log('Строки помечены source_api=WB_API_FIN_V1 — откат: удалить эти строки по метке.');
}


// ════════════════════════════════════════════════════════════════
// РЕЗЮМИРУЕМЫЙ БЭКФИЛЛ по всем reportId (сен 2024 → сейчас)
// ════════════════════════════════════════════════════════════════
// Apps Script режет выполнение ~6 мин. Поэтому: бюджет времени, прогресс
// сохраняется в Script Property после КАЖДОГО отчёта. Запускать
// wbFinanceV1Backfill повторно, пока не напишет «БЭКФИЛЛ ЗАВЕРШЁН».
// Каждый отчёт пишется через replace-slice по reportId — повторный запуск
// уже готовых пропускает, прерванный отчёт перезапишется начисто.

var WB_FIN_V1_BACKFILL_FROM_ = '2024-09-01';
var WB_FIN_V1_DONE_PROP_     = 'WB_FIN_V1_DONE';
var WB_FIN_V1_BUDGET_MS_     = 270000; // 4.5 мин, с запасом до лимита 6 мин

/** Получить список отчётов реализации (массив). */
function wbFinV1ListAll_(token, from, to) {
  var r = wbFinV1Post_(token, WB_FIN_V1_LIST_PATH_, { dateFrom: from, dateTo: to });
  if (r.code !== 200) return { ok: false, error: 'HTTP ' + r.code + ': ' + String(r.body).substring(0, 200), data: [] };
  var arr = Array.isArray(r.json) ? r.json : (r.json && r.json.data ? r.json.data : []);
  return { ok: true, data: arr };
}

/** Один скан RAW: множество уже записанных ключей report_id|rrd_id для нашего источника. */
function wbFinV1BuildSeenRrdSet_(rawSheet, hMap) {
  var set = {};
  var lr = rawSheet.getLastRow();
  if (lr < 2) return set;
  var srcCol = hMap['source_api'], ridCol = hMap['report_id'], rrdCol = hMap['rrd_id'];
  if (srcCol === undefined || ridCol === undefined || rrdCol === undefined) return set;
  var data = rawSheet.getRange(2, 1, lr - 1, rawSheet.getLastColumn()).getValues();
  for (var i = 0; i < data.length; i++) {
    if (String(data[i][srcCol]).trim() === WB_FIN_V1_SOURCE_TAG_) {
      var rid = String(data[i][ridCol] || '').trim();
      var rrd = String(data[i][rrdCol] || '').trim();
      if (rid && rrd) set[rid + '|' + rrd] = 1;
    }
  }
  return set;
}

function wbFinanceV1Backfill() {
  var tk = getFinanceV1Token_();
  if (!tk) { console.log('❌ Нет токена WB_TOKEN_FINANCE'); return; }
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var props = PropertiesService.getScriptProperties();
  var rawSheet = getRawFinanceSheet_(ss);
  if (!rawSheet) { console.log('❌ Лист RAW_WB_FINANCE не найден'); return; }
  var rawLastCol = rawSheet.getLastColumn();
  var hMap = buildFinanceRawHeaderMap_(rawSheet, rawLastCol);
  if (hMap['source_api'] === undefined || hMap['report_id'] === undefined || hMap['rrd_id'] === undefined) {
    console.log('❌ В RAW нет колонок source_api / report_id / rrd_id — бэкфилл остановлен'); return;
  }

  var done = {};
  try { var rawDone = props.getProperty(WB_FIN_V1_DONE_PROP_); if (rawDone) done = JSON.parse(rawDone) || {}; } catch (e) {}

  // ОДИН скан листа за прогон — множество уже записанных ключей reportId|rrdId.
  var seen = wbFinV1BuildSeenRrdSet_(rawSheet, hMap);
  console.log('Наших строк в RAW (уник. reportId|rrdId): ' + Object.keys(seen).length);

  var today = Utilities.formatDate(new Date(), 'Europe/Moscow', 'yyyy-MM-dd');
  var lst = wbFinV1ListAll_(tk.token, WB_FIN_V1_BACKFILL_FROM_, today);
  if (!lst.ok) { console.log('❌ list: ' + lst.error); return; }
  var reports = lst.data;
  reports.sort(function (a, b) { return String(a.dateFrom).localeCompare(String(b.dateFrom)); });
  console.log('Всего отчётов: ' + reports.length + ' | готово: ' + Object.keys(done).length);

  var skuIndex = (typeof buildSkuIndex_ === 'function') ? buildSkuIndex_(ss) : null;
  var start = Date.now();
  var processed = 0, appended = 0, errs = 0;

  for (var i = 0; i < reports.length; i++) {
    var rep = reports[i];
    var rid = String(rep.reportId);
    if (done[rid]) continue;

    if (Date.now() - start > WB_FIN_V1_BUDGET_MS_) {
      props.setProperty(WB_FIN_V1_DONE_PROP_, JSON.stringify(done));
      console.log('⏸️ Бюджет исчерпан. За прогон: отчётов ' + processed + ', строк +' + appended + '. Запустите wbFinanceV1Backfill ещё раз.');
      return;
    }

    var fetched = wbFinV1FetchDetailedAll_(tk.token, rid);
    if (!fetched.ok) { errs++; console.log('✗ ' + rid + ' (' + rep.dateFrom + '): ' + fetched.error); continue; }

    var adapted = [];
    for (var a = 0; a < fetched.data.length; a++) adapted.push(wbFinV1AdaptRow_(fetched.data[a]));
    var rows = normalizeFinanceApiRows_(adapted, hMap, rawLastCol, 'FIN_V1_BF', '', rep.dateFrom, rep.dateTo, '', skuIndex);

    var toWrite = [];
    for (var rI = 0; rI < rows.length; rI++) {
      rows[rI][hMap['source_api']] = WB_FIN_V1_SOURCE_TAG_;
      var rrd = String(rows[rI][hMap['rrd_id']] || '').trim();
      var key = rid + '|' + rrd;
      if (hMap['row_hash'] !== undefined) rows[rI][hMap['row_hash']] = financeMd5_(WB_FIN_V1_SOURCE_TAG_ + '|' + key);
      if (!rrd || seen[key]) continue;        // дедуп по reportId|rrdId
      seen[key] = 1;
      toWrite.push(rows[rI]);
    }
    if (toWrite.length) appendFinanceRows_(rawSheet, toWrite, rawLastCol);
    appended += toWrite.length;
    done[rid] = 1; processed++;
    props.setProperty(WB_FIN_V1_DONE_PROP_, JSON.stringify(done)); // прогресс после каждого отчёта
    console.log('✓ ' + rep.dateFrom + '…' + rep.dateTo + ' [' + rid + '] +' + toWrite.length + ' (из ' + rows.length + ')');
  }

  console.log('━━━ БЭКФИЛЛ ЗАВЕРШЁН ━━━ готово ' + Object.keys(done).length + '/' + reports.length + ', добавлено за прогон: ' + appended + ', ошибок: ' + errs);
}

/** ДИАГНОСТИКА (только чтение): сколько наших строк, уникальных rrd_id и есть ли дубли. */
function wbFinanceV1CheckDuplicates() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var rawSheet = getRawFinanceSheet_(ss);
  if (!rawSheet) { console.log('❌ нет RAW_WB_FINANCE'); return; }
  var lastCol = rawSheet.getLastColumn();
  var hMap = buildFinanceRawHeaderMap_(rawSheet, lastCol);
  var lr = rawSheet.getLastRow();
  if (lr < 2) { console.log('лист пуст'); return; }
  var srcCol = hMap['source_api'], ridCol = hMap['report_id'], rrdCol = hMap['rrd_id'], fpCol = hMap['for_pay'], retCol = hMap['retail_amount'];
  var data = rawSheet.getRange(2, 1, lr - 1, lastCol).getValues();
  var total = 0, seen = {}, dup = 0, sumForPay = 0, sumRetail = 0, excel = 0;
  for (var i = 0; i < data.length; i++) {
    var src = String(data[i][srcCol]).trim();
    if (src === 'DRIVE_XLSX_REPORT') excel++;
    if (src !== WB_FIN_V1_SOURCE_TAG_) continue;
    total++;
    var rid = String(data[i][ridCol] || '').trim();
    var rrd = String(data[i][rrdCol] || '').trim();
    var key = rid + '|' + rrd;
    if (rid && rrd) { if (seen[key]) dup++; else seen[key] = 1; }
    if (fpCol !== undefined) sumForPay += Number(data[i][fpCol]) || 0;
    if (retCol !== undefined) sumRetail += Number(data[i][retCol]) || 0;
  }
  console.log('WB_API_FIN_V1: строк ' + total + ' | уник. reportId|rrdId ' + Object.keys(seen).length + ' | ДУБЛЕЙ по reportId|rrdId: ' + dup);
  console.log('Сумма for_pay: ' + sumForPay.toFixed(2) + ' | retail_amount: ' + sumRetail.toFixed(2));
  console.log('Excel-строк (DRIVE_XLSX_REPORT) в листе: ' + excel);
  console.log(dup > 0 ? '⚠️ Есть дубли — почистим (дам функцию). ' : '✓ Дублей нет.');
}

/** Статус бэкфилла: сколько отчётов готово из всех. */
function wbFinanceV1BackfillStatus() {
  var tk = getFinanceV1Token_(); if (!tk) { console.log('❌ нет токена'); return; }
  var props = PropertiesService.getScriptProperties();
  var done = {}; try { var rd = props.getProperty(WB_FIN_V1_DONE_PROP_); if (rd) done = JSON.parse(rd) || {}; } catch (e) {}
  var today = Utilities.formatDate(new Date(), 'Europe/Moscow', 'yyyy-MM-dd');
  var lst = wbFinV1ListAll_(tk.token, WB_FIN_V1_BACKFILL_FROM_, today);
  console.log('Готово ' + Object.keys(done).length + ' из ' + (lst.ok ? lst.data.length : '?') + ' отчётов.');
}

/** Сброс прогресса бэкфилла (RAW не трогает; следующий запуск пойдёт заново, replace-slice не задвоит). */
function wbFinanceV1BackfillReset() {
  PropertiesService.getScriptProperties().deleteProperty(WB_FIN_V1_DONE_PROP_);
  console.log('Прогресс бэкфилла сброшен. RAW не тронут.');
}
