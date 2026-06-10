/**
 * ══════════════════════════════════════════════════════════════
 * EVETIS WB — WbApiTestRunner.gs  v1.0
 *
 * Публичные тестовые функции WB API (по docs/wb_api_test_plan.md).
 * Все имена начинаются с testWbApi… и явно отделены от production.
 *
 * Запуск — ВРУЧНУЮ из редактора Apps Script (по одному тесту).
 * testWbApiRunAll() есть, но из-за лимита времени Apps Script (6 мин)
 * это НЕ основной сценарий (см. docs/wb_api_test_runner_usage.md).
 *
 * Контур НЕ меняет production-код, НЕ пишет в боевые листы,
 * НЕ хранит/не логирует токены, сохраняет raw JSON только в Drive.
 *
 * Endpoints/методы — как в утверждённом плане. Для нового Finance API
 * (T1), candidate B остатков (T6) и adv/v3/fullstats (T8) реальные
 * контракты подтверждаются по факту: раннер сохраняет raw и проставляет
 * PARTIAL/TBD/FAILED, если схема отличается — это и есть цель тестов.
 * ══════════════════════════════════════════════════════════════
 */


// ═══════════════════════════════════════
// ОБЩАЯ ПОДГОТОВКА
// ═══════════════════════════════════════

/**
 * Предохранитель + папка + токен.
 * @return {Object} { ok, folder, token, result } —
 *   при ok=false result уже содержит BLOCKED и (по возможности) сохранён.
 */
function wbApiTestPrepare_(testId, category) {
  assertWbApiTestMode_();
  var result = wbApiTestMakeResult_(testId);

  var folder = wbApiTestGetResultsFolder_();
  if (!folder) {
    result.decision = WB_API_TEST_DECISION_BLOCKED_;
    result.errors.push('Нет Drive-папки результатов (' + WB_API_TEST_RESULTS_FOLDER_PROP_ + ').');
    return { ok: false, folder: null, token: '', result: result };
  }

  var tk = wbApiTestGetToken_(category);
  wbApiTestTokenInfo_(category, tk.present);
  // Гарантия: tk.token используется ТОЛЬКО как аргумент wbApiTestHttp_/wbApiTestRunTask_.
  // Он никогда не попадает в result.requestParams, result.errors, rawResponse, summary и Drive JSON.
  result.tokenPresent = tk.present;
  if (!tk.present) {
    result.decision = WB_API_TEST_DECISION_BLOCKED_;
    result.errors.push('Нет токена категории ' + category + '.');
    wbApiTestFinalize_(folder, result, null);
    return { ok: false, folder: folder, token: '', result: result };
  }

  return { ok: true, folder: folder, token: tk.token, result: result };
}


// ═══════════════════════════════════════
// T1 — Новый Finance API
// ═══════════════════════════════════════

function testWbApiRunFinanceNew() {
  var prep = wbApiTestPrepare_('T1', 'Finance');
  if (!prep.ok) return prep.result;
  var r = prep.result, folder = prep.folder, token = prep.token;

  r.endpoint = WB_API_TEST_HOST_FINANCE_ + '/api/finance/v1/sales-reports/detailed';
  r.requestParams = { dateFrom: WB_API_TEST_DATE_FROM_, dateTo: WB_API_TEST_DATE_TO_,
    limit: WB_API_TEST_PAGE_LIMIT_, rrdId: 0, note: 'POST body; денежные поля ожидаются строками (camelCase)' };

  var all = [];
  var rrdId = 0, pages = 0, lastCode = null, terminatedBy204 = false;
  try {
    while (pages < WB_API_TEST_MAX_PAGES_) {
      pages++;
      var body = { dateFrom: WB_API_TEST_DATE_FROM_, dateTo: WB_API_TEST_DATE_TO_,
        limit: WB_API_TEST_PAGE_LIMIT_, rrdId: rrdId };
      var resp = wbApiTestHttp_('post', r.endpoint, token, body);
      lastCode = resp.code;
      if (resp.code === 204) { terminatedBy204 = true; break; }
      if (!resp.ok) { r.errors.push('HTTP ' + resp.code + ': ' + resp.body.substring(0, 200)); break; }
      var chunk = resp.json;
      // новый API может вернуть массив или {data:[...]}
      if (chunk && chunk.data && chunk.data.length !== undefined) chunk = chunk.data;
      if (!chunk || !chunk.length) break;
      for (var i = 0; i < chunk.length; i++) all.push(chunk[i]);
      var last = chunk[chunk.length - 1];
      var lastId = Number(wbApiTestPick_(last, ['rrdId', 'rrd_id', 'rrdid']) || 0);
      if (!lastId || lastId === rrdId) break;
      rrdId = lastId;
      if (chunk.length < WB_API_TEST_PAGE_LIMIT_) break;
      Utilities.sleep(WB_API_TEST_FINANCE_PAGE_PAUSE_MS_);
    }
  } catch (e) {
    r.errors.push('Исключение: ' + e.message);
  }

  r.httpStatus = lastCode;
  r.rowsCount = all.length;
  r.fields = wbApiTestFieldList_(all);
  r.firstRows = wbApiTestFirstRows_(all, 3);

  var c = wbApiTestFinanceChecksums_(all,
    { gross: ['retailAmount', 'retail_amount', 'ppvzForPay', 'saleAmount'],
      forPay: ['ppvzForPay', 'ppvz_for_pay', 'forPay'],
      logistics: ['deliveryService', 'deliveryRub', 'delivery_rub', 'logistics'],
      storage: ['paidStorage', 'storageFee', 'storage_fee', 'storagePrice'],
      deductions: ['deduction', 'penalty', 'additionalPayment'],
      acceptance: ['paidAcceptance', 'acceptance', 'acceptanceCost'],
      acquiring: ['acquiringFee', 'acquiring_fee'],
      rebillLogistics: ['rebillLogisticCost', 'rebill_logistic_cost'],
      qty: ['quantity', 'qty'] },
    ['nmId', 'nm_id', 'nmID'], ['srid', 'sale_dt'], ['rrdId', 'rrd_id', 'rrdid']);
  c.pages = pages;
  c.terminatedBy204 = terminatedBy204;
  r.checksums = c;

  r.decision = wbApiTestDecideRows_(r, c);
  return wbApiTestFinalize_(folder, r, all);
}


// ═══════════════════════════════════════
// T2 — Legacy Finance (сверка)
// ═══════════════════════════════════════

function testWbApiRunLegacyFinance() {
  var prep = wbApiTestPrepare_('T2', 'Statistics');
  if (!prep.ok) return prep.result;
  var r = prep.result, folder = prep.folder, token = prep.token;

  var base = WB_API_TEST_HOST_STATISTICS_ + '/api/v5/supplier/reportDetailByPeriod';
  r.endpoint = base;
  r.requestParams = { dateFrom: WB_API_TEST_DATE_FROM_ISO_, dateTo: WB_API_TEST_DATE_TO_ISO_,
    limit: WB_API_TEST_PAGE_LIMIT_, rrdid: 0 };

  var all = [];
  var rrdid = 0, pages = 0, lastCode = null;
  try {
    while (pages < WB_API_TEST_MAX_PAGES_) {
      pages++;
      var url = base + '?dateFrom=' + encodeURIComponent(WB_API_TEST_DATE_FROM_ISO_) +
        '&dateTo=' + encodeURIComponent(WB_API_TEST_DATE_TO_ISO_) +
        '&limit=' + WB_API_TEST_PAGE_LIMIT_ + '&rrdid=' + rrdid;
      var resp = wbApiTestHttp_('get', url, token, null);
      lastCode = resp.code;
      if (resp.code === 204) break;
      if (!resp.ok) { r.errors.push('HTTP ' + resp.code + ': ' + resp.body.substring(0, 200)); break; }
      var chunk = resp.json;
      if (!chunk || !chunk.length) break;
      for (var i = 0; i < chunk.length; i++) all.push(chunk[i]);
      var lastId = Number(wbApiTestPick_(chunk[chunk.length - 1], ['rrd_id', 'rrdId']) || 0);
      if (!lastId || lastId === rrdid) break;
      rrdid = lastId;
      if (chunk.length < WB_API_TEST_PAGE_LIMIT_) break;
      Utilities.sleep(WB_API_TEST_FINANCE_PAGE_PAUSE_MS_);
    }
  } catch (e) {
    r.errors.push('Исключение: ' + e.message);
  }

  r.httpStatus = lastCode;
  r.rowsCount = all.length;
  r.fields = wbApiTestFieldList_(all);
  r.firstRows = wbApiTestFirstRows_(all, 3);

  var c = wbApiTestFinanceChecksums_(all,
    { gross: ['retail_amount', 'ppvz_for_pay'],
      forPay: ['ppvz_for_pay'],
      logistics: ['delivery_rub'],
      storage: ['storage_fee'],
      deductions: ['deduction', 'penalty'],
      acceptance: ['acceptance'],
      acquiring: ['acquiring_fee'],
      rebillLogistics: ['rebill_logistic_cost'],
      qty: ['quantity'] },
    ['nm_id', 'nmId'], ['srid'], ['rrd_id']);
  c.pages = pages;
  r.checksums = c;

  r.decision = wbApiTestDecideRows_(r, c);
  return wbApiTestFinalize_(folder, r, all);
}


// ═══════════════════════════════════════
// T3 — Orders
// ═══════════════════════════════════════

function testWbApiRunOrders() {
  var prep = wbApiTestPrepare_('T3', 'Statistics');
  if (!prep.ok) return prep.result;
  var r = prep.result, folder = prep.folder, token = prep.token;

  var base = WB_API_TEST_HOST_STATISTICS_ + '/api/v1/supplier/orders';
  r.endpoint = base;
  r.requestParams = { dateFrom: WB_API_TEST_DATE_FROM_, flag: 0,
    filter: 'строки фильтруются по date в пределах ' + WB_API_TEST_DATE_FROM_ +
      ' 00:00:00 — ' + WB_API_TEST_DATE_TO_ + ' 23:59:59' };

  var rawData = [];
  try {
    var url = base + '?dateFrom=' + encodeURIComponent(WB_API_TEST_DATE_FROM_) + '&flag=0';
    var resp = wbApiTestHttp_('get', url, token, null);
    r.httpStatus = resp.code;
    if (!resp.ok) r.errors.push('HTTP ' + resp.code + ': ' + resp.body.substring(0, 200));
    else if (resp.json && resp.json.length) rawData = resp.json;
  } catch (e) { r.errors.push('Исключение: ' + e.message); }

  var data = [];
  for (var f = 0; f < rawData.length; f++) {
    if (wbApiTestInPeriod_(rawData[f].date)) data.push(rawData[f]);
  }

  r.rowsCount = data.length;
  r.fields = wbApiTestFieldList_(data);
  r.firstRows = wbApiTestFirstRows_(data, 3);

  var sku = wbApiTestLoadSkuNmIds_();
  var nmIds = [], cancels = 0;
  for (var i = 0; i < data.length; i++) {
    var nm = wbApiTestNormNmId_(wbApiTestPick_(data[i], ['nmId', 'nmid', 'nm_id']));
    if (nm) nmIds.push(nm);
    if (data[i].isCancel === true) cancels++;
  }
  var uniq = wbApiTestUniqueAndUnmatched_(nmIds, sku.set);
  r.checksums = { rawRowsCount: rawData.length, filteredRowsCount: data.length,
    filterInfo: WB_API_TEST_DATE_FROM_ + ' 00:00:00 — ' + WB_API_TEST_DATE_TO_ + ' 23:59:59 по полю date',
    ordersCount: data.length, cancels: cancels,
    uniqueNmId: uniq.unique, rowsWithoutNmId: data.length - nmIds.length,
    unmatchedNmId: uniq.unmatched, hasSrid: wbApiTestHasField_(data, ['srid']),
    hasBarcode: wbApiTestHasField_(data, ['barcode']),
    hasSupplierArticle: wbApiTestHasField_(data, ['supplierArticle']) };

  r.decision = wbApiTestDecideRows_(r, r.checksums);
  return wbApiTestFinalize_(folder, r, rawData);
}


// ═══════════════════════════════════════
// T4 — Sales / Returns
// ═══════════════════════════════════════

function testWbApiRunSales() {
  var prep = wbApiTestPrepare_('T4', 'Statistics');
  if (!prep.ok) return prep.result;
  var r = prep.result, folder = prep.folder, token = prep.token;

  var base = WB_API_TEST_HOST_STATISTICS_ + '/api/v1/supplier/sales';
  r.endpoint = base;
  r.requestParams = { dateFrom: WB_API_TEST_DATE_FROM_, flag: 0,
    filter: 'строки фильтруются по date в пределах ' + WB_API_TEST_DATE_FROM_ +
      ' 00:00:00 — ' + WB_API_TEST_DATE_TO_ + ' 23:59:59' };

  var rawData = [];
  try {
    var url = base + '?dateFrom=' + encodeURIComponent(WB_API_TEST_DATE_FROM_) + '&flag=0';
    var resp = wbApiTestHttp_('get', url, token, null);
    r.httpStatus = resp.code;
    if (!resp.ok) r.errors.push('HTTP ' + resp.code + ': ' + resp.body.substring(0, 200));
    else if (resp.json && resp.json.length) rawData = resp.json;
  } catch (e) { r.errors.push('Исключение: ' + e.message); }

  var data = [];
  for (var f = 0; f < rawData.length; f++) {
    if (wbApiTestInPeriod_(rawData[f].date)) data.push(rawData[f]);
  }

  r.rowsCount = data.length;
  r.fields = wbApiTestFieldList_(data);
  r.firstRows = wbApiTestFirstRows_(data, 3);

  var sku = wbApiTestLoadSkuNmIds_();
  var nmIds = [], sales = 0, returns = 0, sumForPay = 0, sumPayment = 0;
  for (var i = 0; i < data.length; i++) {
    var row = data[i];
    var nm = wbApiTestNormNmId_(wbApiTestPick_(row, ['nmId', 'nmid', 'nm_id']));
    if (nm) nmIds.push(nm);
    var saleId = String(wbApiTestPick_(row, ['saleID', 'saleId']) || '');
    if (saleId.charAt(0) === 'R') returns++; else sales++;
    sumForPay += wbApiTestMoney_(wbApiTestPick_(row, ['forPay']));
    sumPayment += wbApiTestMoney_(wbApiTestPick_(row, ['paymentSaleAmount', 'priceWithDisc', 'finishedPrice']));
  }
  var uniq = wbApiTestUniqueAndUnmatched_(nmIds, sku.set);
  r.checksums = { rawRowsCount: rawData.length, filteredRowsCount: data.length,
    filterInfo: WB_API_TEST_DATE_FROM_ + ' 00:00:00 — ' + WB_API_TEST_DATE_TO_ + ' 23:59:59 по полю date',
    rows: data.length, sales: sales, returns: returns,
    sumForPay: wbApiTestRound_(sumForPay), sumPaymentSaleAmount: wbApiTestRound_(sumPayment),
    uniqueNmId: uniq.unique, rowsWithoutNmId: data.length - nmIds.length,
    unmatchedNmId: uniq.unmatched, hasSrid: wbApiTestHasField_(data, ['srid']) };

  r.decision = wbApiTestDecideRows_(r, r.checksums);
  return wbApiTestFinalize_(folder, r, rawData);
}


// ═══════════════════════════════════════
// T5 + T6 — Остатки (candidate A и B)
// ═══════════════════════════════════════

function testWbApiRunStocks() {
  var a = wbApiTestStocksA_();   // T5
  var b = wbApiTestStocksB_();   // T6
  return { T5: a, T6: b };
}

/** T5 — warehouse_remains (task-based). */
function wbApiTestStocksA_() {
  var prep = wbApiTestPrepare_('T5', 'Analytics');
  if (!prep.ok) return prep.result;
  var r = prep.result, folder = prep.folder, token = prep.token;

  var taskBase = WB_API_TEST_HOST_ANALYTICS_ + '/api/v1/warehouse_remains';
  var createUrl = taskBase + '?groupByBrand=false&groupBySubject=false&groupBySa=true' +
    '&groupByNm=true&groupByBarcode=true&groupBySize=true';
  r.endpoint = taskBase;
  r.requestParams = { groupByNm: true, groupBySa: true, groupByBarcode: true, groupBySize: true,
    flow: 'create→status→download' };

  var data = [];
  try {
    var task = wbApiTestRunTask_(token, createUrl, taskBase);
    r.httpStatus = task.ok ? 200 : null;
    if (!task.ok) r.errors.push(task.error);
    else if (task.data && task.data.length) data = task.data;
  } catch (e) { r.errors.push('Исключение: ' + e.message); }

  r.rowsCount = data.length;
  r.fields = wbApiTestFieldList_(data);
  r.firstRows = wbApiTestFirstRows_(data, 3);

  var sku = wbApiTestLoadSkuNmIds_();
  // Спец-категории внутри warehouses[] (не физические склады)
  var WH_TOTAL_ = 'Всего находится на складах';
  var WH_TO_CLIENT_ = 'В пути до получателей';
  var WH_FROM_CLIENT_ = 'В пути возвраты на склад WB';
  var nmIds = [];
  var stockTotalQtyFromTotalRows = 0;   // из строк "Всего находится на складах"
  var stockPhysicalQtyByWarehouses = 0; // сумма по физическим складам
  var inWayToClientQty = 0;
  var inWayFromClientQty = 0;
  var warehouseRows = 0;                 // число физических складских строк
  var totalRowsPresent = false;
  for (var i = 0; i < data.length; i++) {
    var nm = wbApiTestNormNmId_(wbApiTestPick_(data[i], ['nmId', 'nmid', 'nm_id']));
    if (nm) nmIds.push(nm);
    var whs = data[i].warehouses;
    if (whs && whs.length) {
      for (var w = 0; w < whs.length; w++) {
        var whName = String(whs[w].warehouseName || whs[w].warehouse || '');
        var q = Number(whs[w].quantity || 0);
        if (whName === WH_TOTAL_) { stockTotalQtyFromTotalRows += q; totalRowsPresent = true; }
        else if (whName === WH_TO_CLIENT_) { inWayToClientQty += q; }
        else if (whName === WH_FROM_CLIENT_) { inWayFromClientQty += q; }
        else { stockPhysicalQtyByWarehouses += q; warehouseRows++; }
      }
    }
  }
  var uniq = wbApiTestUniqueAndUnmatched_(nmIds, sku.set);
  r.checksums = { rows: data.length, uniqueNmId: uniq.unique,
    stockTotalQtyFromTotalRows: stockTotalQtyFromTotalRows,
    stockPhysicalQtyByWarehouses: stockPhysicalQtyByWarehouses,
    inWayToClientQty: inWayToClientQty, inWayFromClientQty: inWayFromClientQty,
    warehouseRows: warehouseRows, totalRowsPresent: totalRowsPresent,
    unmatchedNmId: uniq.unmatched,
    hasVendorCode: wbApiTestHasField_(data, ['vendorCode']),
    hasBarcode: wbApiTestHasField_(data, ['barcode']),
    expandableToRows: (warehouseRows > 0) };

  r.decision = wbApiTestDecideRows_(r, r.checksums);
  return wbApiTestFinalize_(folder, r, data);
}

/** T6 — stocks-report/wb-warehouses (POST, схема требует проверки → TBD по умолчанию). */
function wbApiTestStocksB_() {
  var prep = wbApiTestPrepare_('T6', 'Analytics');
  if (!prep.ok) return prep.result;
  var r = prep.result, folder = prep.folder, token = prep.token;

  r.endpoint = WB_API_TEST_HOST_ANALYTICS_ + '/api/analytics/v1/stocks-report/wb-warehouses';
  // body не подтверждён — пробуем минимальный; цель теста — узнать контракт
  var body = { currentPeriod: { start: WB_API_TEST_DATE_FROM_, end: WB_API_TEST_DATE_TO_ },
    stockType: '', skipDeletedNm: false };
  r.requestParams = { bodyTried: body, note: 'candidate B; body/schema требуют подтверждения' };

  var data = [], raw = null;
  try {
    var resp = wbApiTestHttp_('post', r.endpoint, token, body);
    r.httpStatus = resp.code;
    raw = resp.json !== null ? resp.json : resp.body;
    if (!resp.ok) {
      r.errors.push('HTTP ' + resp.code + ': ' + resp.body.substring(0, 200));
    } else {
      var arr = resp.json;
      if (arr && arr.data && arr.data.items && arr.data.items.length !== undefined) {
        arr = arr.data.items;
      } else if (arr && arr.data && arr.data.length !== undefined) {
        arr = arr.data;
      }
      if (arr && arr.length) data = arr;
    }
  } catch (e) { r.errors.push('Исключение: ' + e.message); }

  r.rowsCount = data.length;
  r.fields = wbApiTestFieldList_(data);
  r.firstRows = wbApiTestFirstRows_(data, 3);

  var sku = wbApiTestLoadSkuNmIds_();
  var nmIds = [], totalQuantity = 0, inWayToClient = 0, inWayFromClient = 0;
  for (var i = 0; i < data.length; i++) {
    var nm = wbApiTestNormNmId_(wbApiTestPick_(data[i], ['nmId', 'nmid', 'nm_id']));
    if (nm) nmIds.push(nm);
    totalQuantity += Number(wbApiTestPick_(data[i], ['quantity', 'qty', 'quantityFull']) || 0);
    inWayToClient += Number(wbApiTestPick_(data[i], ['inWayToClient', 'in_way_to_client']) || 0);
    inWayFromClient += Number(wbApiTestPick_(data[i], ['inWayFromClient', 'in_way_from_client']) || 0);
  }
  var uniqT6 = wbApiTestUniqueAndUnmatched_(nmIds, sku.set);
  r.checksums = { rows: data.length, uniqueNmId: uniqT6.unique,
    totalQuantity: totalQuantity, inWayToClient: inWayToClient, inWayFromClient: inWayFromClient,
    unmatchedNmId: uniqT6.unmatched,
    hasNmId: wbApiTestHasField_(data, ['nmId', 'nmid', 'nm_id']),
    hasWarehouse: wbApiTestHasField_(data, ['warehouse', 'warehouseName']),
    hasRegion: wbApiTestHasField_(data, ['region', 'regionName', 'oblast']),
    hasWarehouseId: wbApiTestHasField_(data, ['warehouseId', 'officeId']),
    hasChrtId: wbApiTestHasField_(data, ['chrtId', 'chrt_id']) };

  // candidate B: даже при 200 без подтверждённой схемы — PARTIAL/TBD
  if (r.httpStatus === 200 && data.length > 0) r.decision = WB_API_TEST_DECISION_PARTIAL_;
  else if (r.errors.length) r.decision = WB_API_TEST_DECISION_TBD_;
  else r.decision = WB_API_TEST_DECISION_TBD_;

  return wbApiTestFinalize_(folder, r, raw);
}


// ═══════════════════════════════════════
// T7 + T8 + T9 — Реклама
// ═══════════════════════════════════════

function testWbApiRunAds() {
  var t7 = wbApiTestAdsList_();
  var statsAdvertIds = (t7.checksums && t7.checksums.statsAdvertIds) || [];
  var t8 = wbApiTestAdsFullstats_(statsAdvertIds);
  var t9 = wbApiTestAdsUpd_();

  // Реклама: T8 (fullstats) — основной управленческий источник расхода;
  // T9 (upd) — контроль списаний WB. Коэффициент приведения НЕ применяется.
  var sumFull = (t8.checksums && t8.checksums.sumSpend) || 0;
  var sumUpd = (t9.checksums && t9.checksums.sumUpd) || 0;
  var deltaAmount = wbApiTestRound_(Math.abs(sumFull - sumUpd));
  var deltaPercent = sumFull ? wbApiTestRound_(deltaAmount / Math.abs(sumFull) * 100) : null;
  var adsStatus = (deltaPercent !== null && deltaPercent <= WB_API_TEST_ADS_DELTA_PCT_) ? 'OK' : 'WARNING';
  wbApiTestLog_('Реклама: T8 spend=' + wbApiTestRound_(sumFull) + ' ₽ (управленческий источник), ' +
    'T9 updSum=' + wbApiTestRound_(sumUpd) + ' ₽ (контроль) → |Δ|=' + deltaAmount + ' ₽' +
    (deltaPercent !== null ? ' (deltaPercent=' + deltaPercent + '%)' : '') + ' → ' + adsStatus +
    (adsStatus === 'WARNING' ? ' — требуется ручная сверка с кабинетом WB' : ''));

  return { T7: t7, T8: t8, T9: t9 };
}

/** T7 — список кампаний. */
function wbApiTestAdsList_() {
  var prep = wbApiTestPrepare_('T7', 'Promotion');
  if (!prep.ok) return prep.result;
  var r = prep.result, folder = prep.folder, token = prep.token;

  var countUrl = WB_API_TEST_HOST_PROMOTION_ + '/adv/v1/promotion/count';
  var advertsUrl = WB_API_TEST_HOST_PROMOTION_ + '/adv/v1/promotion/adverts';
  r.endpoint = countUrl + ' ; ' + advertsUrl;
  r.requestParams = { note: 'count → adverts; статусы 7/9/11; пачки до ' + WB_API_TEST_ADS_BATCH_ };

  var raw = {}, allAdvertIds = [], statsAdvertIds = [], statuses = {};
  var STATS_STATUSES_ = { 7: true, 9: true, 11: true };
  try {
    var cResp = wbApiTestHttp_('get', countUrl, token, null);
    r.httpStatus = cResp.code;
    raw.count = cResp.json;
    if (!cResp.ok) r.errors.push('count HTTP ' + cResp.code + ': ' + cResp.body.substring(0, 150));
    if (cResp.ok && cResp.json) {
      var adverts = cResp.json.adverts || (cResp.json.data && cResp.json.data.adverts) || [];
      for (var i = 0; i < adverts.length; i++) {
        var st = adverts[i].status;
        var list = adverts[i].advert_list || adverts[i].advertList || [];
        for (var j = 0; j < list.length; j++) {
          var id = list[j].advertId || list[j].advertID || list[j].id;
          if (id) {
            var idNum = Number(id);
            allAdvertIds.push(idNum);
            statuses[st] = (statuses[st] || 0) + 1;
            if (STATS_STATUSES_[st]) statsAdvertIds.push(idNum);
          }
        }
      }
    }
    // детали по adverts — POST батчами до 50 (промо/adverts вызывается POST)
    if (statsAdvertIds.length) {
      var detResp = wbApiTestHttp_('post', advertsUrl, token, statsAdvertIds.slice(0, WB_API_TEST_ADS_BATCH_));
      raw.advertsSample = detResp.json;
    }
  } catch (e) { r.errors.push('Исключение: ' + e.message); }

  r.rowsCount = allAdvertIds.length;
  r.checksums = { allAdvertIds: allAdvertIds, statsAdvertIds: statsAdvertIds,
    allAdvertCount: allAdvertIds.length, statsAdvertCount: statsAdvertIds.length,
    statusBreakdown: statuses, statsStatuses: [7, 9, 11],
    batches: Math.ceil(statsAdvertIds.length / WB_API_TEST_ADS_BATCH_) };
  r.fields = ['advertId', 'status'];
  r.firstRows = allAdvertIds.slice(0, 3);

  if (r.httpStatus === 200 && allAdvertIds.length > 0) r.decision = WB_API_TEST_DECISION_PASSED_;
  else if (r.httpStatus === 200) r.decision = WB_API_TEST_DECISION_PARTIAL_;
  else r.decision = WB_API_TEST_DECISION_FAILED_;

  return wbApiTestFinalize_(folder, r, raw);
}

/** T8 — fullstats (по плану GET; v3 может требовать POST — фиксируем фактический результат). */
function wbApiTestAdsFullstats_(statsAdvertIds) {
  var prep = wbApiTestPrepare_('T8', 'Promotion');
  if (!prep.ok) return prep.result;
  var r = prep.result, folder = prep.folder, token = prep.token;

  r.endpoint = WB_API_TEST_HOST_PROMOTION_ + '/adv/v3/fullstats';
  r.requestParams = { beginDate: WB_API_TEST_DATE_FROM_, endDate: WB_API_TEST_DATE_TO_,
    statsAdvertIdsCount: (statsAdvertIds || []).length,
    note: 'метод GET по плану, beginDate/endDate; если потребуется POST с body — зафиксировать в результате' };

  if (!statsAdvertIds || !statsAdvertIds.length) {
    r.errors.push('Нет statsAdvertId (статусы 7/9/11) из T7 — fullstats не запрошен.');
    r.decision = WB_API_TEST_DECISION_BLOCKED_;
    return wbApiTestFinalize_(folder, r, null);
  }

  var allRows = [];   // развёрнутые {date, advertId, nmId, sum, views, clicks, orders, shks}
  var rawParts = [];
  var sku = wbApiTestLoadSkuNmIds_();
  var zeroSumNonZeroActivity = 0;
  var nmIds = [];

  try {
    for (var b = 0; b < statsAdvertIds.length; b += WB_API_TEST_ADS_BATCH_) {
      var batch = statsAdvertIds.slice(b, b + WB_API_TEST_ADS_BATCH_);
      var url = r.endpoint + '?ids=' + batch.join(',') +
        '&beginDate=' + WB_API_TEST_DATE_FROM_ + '&endDate=' + WB_API_TEST_DATE_TO_;
      var resp = wbApiTestHttp_('get', url, token, null);
      r.httpStatus = resp.code;
      if (!resp.ok) { r.errors.push('batch HTTP ' + resp.code + ': ' + resp.body.substring(0, 150)); continue; }
      var camps = resp.json;
      if (!camps || !camps.length) continue;
      rawParts.push({
        batchIndex: Math.floor(b / WB_API_TEST_ADS_BATCH_) + 1,
        advertIds: batch,
        response: camps
      });
      for (var ci = 0; ci < camps.length; ci++) {
        var camp = camps[ci];
        var advId = camp.advertId || camp.advertID;
        var days = camp.days || [];
        for (var d = 0; d < days.length; d++) {
          var date = days[d].date;
          var apps = days[d].apps || [];
          for (var a = 0; a < apps.length; a++) {
            var nms = apps[a].nm || apps[a].nms || [];
            for (var n = 0; n < nms.length; n++) {
              var nmId = wbApiTestNormNmId_(nms[n].nmId || nms[n].nmID);
              var sum = wbApiTestMoney_(nms[n].sum);
              var views = Number(nms[n].views || 0);
              var clicks = Number(nms[n].clicks || 0);
              var orders = Number(nms[n].orders || 0);
              var shks = Number(nms[n].shks || 0);
              if (nmId) nmIds.push(nmId);
              if (sum === 0 && (clicks > 0 || orders > 0)) zeroSumNonZeroActivity++;
              allRows.push({ date: date, advertId: advId, nmId: nmId, sum: sum,
                views: views, clicks: clicks, orders: orders, shks: shks });
            }
          }
        }
      }
      Utilities.sleep(WB_API_TEST_SOFT_PAUSE_MS_);
    }
  } catch (e) { r.errors.push('Исключение: ' + e.message); }

  var sumSpend = 0;
  for (var i = 0; i < allRows.length; i++) sumSpend += allRows[i].sum;
  var uniq = wbApiTestUniqueAndUnmatched_(nmIds, sku.set);

  r.rowsCount = allRows.length;
  r.fields = ['date', 'advertId', 'nmId', 'sum', 'views', 'clicks', 'orders', 'shks'];
  r.firstRows = wbApiTestFirstRows_(allRows, 3);
  r.checksums = { aggregatedRows: allRows.length, sumSpend: wbApiTestRound_(sumSpend),
    uniqueNmId: uniq.unique, unmatchedNmId: uniq.unmatched,
    zeroSumWithActivity: zeroSumNonZeroActivity,
    note: 'T8 fullstats — основной управленческий источник рекламного расхода (P&L, SKU-воронка); ' +
      'T9 upd — контроль списаний WB' };

  r.decision = wbApiTestDecideRows_(r, r.checksums);
  return wbApiTestFinalize_(folder, r, rawParts);
}

/** T9 — списания adv/v1/upd. */
function wbApiTestAdsUpd_() {
  var prep = wbApiTestPrepare_('T9', 'Promotion');
  if (!prep.ok) return prep.result;
  var r = prep.result, folder = prep.folder, token = prep.token;

  r.endpoint = WB_API_TEST_HOST_PROMOTION_ + '/adv/v1/upd';
  r.requestParams = { from: WB_API_TEST_DATE_FROM_, to: WB_API_TEST_DATE_TO_ };

  var data = [];
  try {
    var url = r.endpoint + '?from=' + WB_API_TEST_DATE_FROM_ + '&to=' + WB_API_TEST_DATE_TO_;
    var resp = wbApiTestHttp_('get', url, token, null);
    r.httpStatus = resp.code;
    if (!resp.ok) r.errors.push('HTTP ' + resp.code + ': ' + resp.body.substring(0, 200));
    else if (resp.json && resp.json.length) data = resp.json;
  } catch (e) { r.errors.push('Исключение: ' + e.message); }

  var sumUpd = 0, byAdvert = {};
  for (var i = 0; i < data.length; i++) {
    var s = wbApiTestMoney_(wbApiTestPick_(data[i], ['updSum', 'sum']));
    var adv = wbApiTestPick_(data[i], ['advertId', 'advertID']);
    sumUpd += s;
    if (adv !== undefined) byAdvert[adv] = wbApiTestRound_((byAdvert[adv] || 0) + s);
  }

  r.rowsCount = data.length;
  r.fields = wbApiTestFieldList_(data);
  r.firstRows = wbApiTestFirstRows_(data, 3);
  r.checksums = { rows: data.length, sumUpd: wbApiTestRound_(sumUpd),
    advertCount: Object.keys(byAdvert).length, sumByAdvert: byAdvert };

  r.decision = wbApiTestDecideRows_(r, r.checksums);
  return wbApiTestFinalize_(folder, r, data);
}


// ═══════════════════════════════════════
// T10 — Хранение (task-based)
// ═══════════════════════════════════════

function testWbApiRunStorage() {
  var prep = wbApiTestPrepare_('T10', 'Analytics');
  if (!prep.ok) return prep.result;
  var r = prep.result, folder = prep.folder, token = prep.token;

  var taskBase = WB_API_TEST_HOST_ANALYTICS_ + '/api/v1/paid_storage';
  var createUrl = taskBase + '?dateFrom=' + WB_API_TEST_DATE_FROM_ + '&dateTo=' + WB_API_TEST_DATE_TO_;
  r.endpoint = taskBase;
  r.requestParams = { dateFrom: WB_API_TEST_DATE_FROM_, dateTo: WB_API_TEST_DATE_TO_, flow: 'create→status→download' };

  var data = [];
  try {
    var task = wbApiTestRunTask_(token, createUrl, taskBase);
    r.httpStatus = task.ok ? 200 : null;
    if (!task.ok) r.errors.push(task.error);
    else if (task.data && task.data.length) data = task.data;
  } catch (e) { r.errors.push('Исключение: ' + e.message); }

  r.rowsCount = data.length;
  r.fields = wbApiTestFieldList_(data);
  r.firstRows = wbApiTestFirstRows_(data, 3);

  var sku = wbApiTestLoadSkuNmIds_();
  var nmIds = [], sumWh = 0;
  for (var i = 0; i < data.length; i++) {
    var nm = wbApiTestNormNmId_(wbApiTestPick_(data[i], ['nmId', 'nmid', 'nm_id']));
    if (nm) nmIds.push(nm);
    sumWh += wbApiTestMoney_(wbApiTestPick_(data[i], ['warehousePrice', 'storage_cost', 'storageCost']));
  }
  var uniq = wbApiTestUniqueAndUnmatched_(nmIds, sku.set);
  r.checksums = { rows: data.length, sumWarehousePrice: wbApiTestRound_(sumWh),
    uniqueNmId: uniq.unique, unmatchedNmId: uniq.unmatched,
    hasWarehouse: wbApiTestHasField_(data, ['warehouse', 'warehouseName']),
    hasDate: wbApiTestHasField_(data, ['date']),
    note: 'сверка с finance (storage) — в summary; канон не закрепляем' };

  r.decision = wbApiTestDecideRows_(r, r.checksums);
  return wbApiTestFinalize_(folder, r, data);
}


// ═══════════════════════════════════════
// T11 — Платная приёмка (task-based)
// ═══════════════════════════════════════

function testWbApiRunAcceptance() {
  var prep = wbApiTestPrepare_('T11', 'Analytics');
  if (!prep.ok) return prep.result;
  var r = prep.result, folder = prep.folder, token = prep.token;

  var taskBase = WB_API_TEST_HOST_ANALYTICS_ + '/api/v1/acceptance_report';
  var createUrl = taskBase + '?dateFrom=' + WB_API_TEST_DATE_FROM_ + '&dateTo=' + WB_API_TEST_DATE_TO_;
  r.endpoint = taskBase;
  r.requestParams = { dateFrom: WB_API_TEST_DATE_FROM_, dateTo: WB_API_TEST_DATE_TO_, flow: 'create→status→download' };

  var data = [];
  try {
    var task = wbApiTestRunTask_(token, createUrl, taskBase);
    r.httpStatus = task.ok ? 200 : null;
    if (!task.ok) r.errors.push(task.error);
    else if (task.data && task.data.length) data = task.data;
  } catch (e) { r.errors.push('Исключение: ' + e.message); }

  r.rowsCount = data.length;
  r.fields = wbApiTestFieldList_(data);
  r.firstRows = wbApiTestFirstRows_(data, 3);

  var sku = wbApiTestLoadSkuNmIds_();
  var nmIds = [], sumTotal = 0, byIncome = {};
  for (var i = 0; i < data.length; i++) {
    var nm = wbApiTestNormNmId_(wbApiTestPick_(data[i], ['nmID', 'nmId', 'nm_id']));
    if (nm) nmIds.push(nm);
    var t = wbApiTestMoney_(wbApiTestPick_(data[i], ['total']));
    sumTotal += t;
    var inc = wbApiTestPick_(data[i], ['incomeId']);
    if (inc !== undefined) byIncome[inc] = wbApiTestRound_((byIncome[inc] || 0) + t);
  }
  var uniq = wbApiTestUniqueAndUnmatched_(nmIds, sku.set);
  r.checksums = { rows: data.length, sumTotal: wbApiTestRound_(sumTotal),
    uniqueNmId: uniq.unique, unmatchedNmId: uniq.unmatched,
    incomeIdCount: Object.keys(byIncome).length,
    note: 'сверка с finance (acceptance) — в summary; канон не закрепляем' };

  r.decision = wbApiTestDecideRows_(r, r.checksums);
  return wbApiTestFinalize_(folder, r, data);
}


// ═══════════════════════════════════════
// RUN ALL (НЕ основной сценарий)
// ═══════════════════════════════════════

/**
 * Прогон всех тестов по очереди. Продолжает работу, даже если
 * отдельный тест BLOCKED/FAILED (каждый в своём try/catch).
 * ВНИМАНИЕ: из-за лимита Apps Script (6 мин) полный прогон может
 * не завершиться. Рекомендуется запускать тесты по одному.
 */
function testWbApiRunAll() {
  assertWbApiTestMode_();
  var folder = wbApiTestGetResultsFolder_();
  var results = [];

  function safe_(fn, ids) {
    try {
      var out = fn();
      if (out && out.testId) results.push(out);
      else if (out) { for (var k = 0; k < ids.length; k++) if (out[ids[k]]) results.push(out[ids[k]]); }
    } catch (e) {
      wbApiTestLog_('Тест ' + ids.join('/') + ' упал: ' + e.message);
      for (var m = 0; m < ids.length; m++) {
        var stub = wbApiTestMakeResult_(ids[m]);
        stub.decision = WB_API_TEST_DECISION_FAILED_;
        stub.errors.push('Исключение в RunAll: ' + e.message);
        results.push(stub);
      }
    }
  }

  safe_(testWbApiRunFinanceNew, ['T1']);
  safe_(testWbApiRunLegacyFinance, ['T2']);
  safe_(testWbApiRunOrders, ['T3']);
  safe_(testWbApiRunSales, ['T4']);
  safe_(testWbApiRunStocks, ['T5', 'T6']);
  safe_(testWbApiRunAds, ['T7', 'T8', 'T9']);
  safe_(testWbApiRunStorage, ['T10']);
  safe_(testWbApiRunAcceptance, ['T11']);

  if (folder) wbApiTestBuildSummary_(folder, results);
  wbApiTestLog_('RunAll завершён: ' + results.length + ' результатов.');
  return results;
}


// ═══════════════════════════════════════
// ОБЩИЕ ХЕЛПЕРЫ РЕШЕНИЯ
// ═══════════════════════════════════════

/**
 * Попадает ли значение даты в контрольный период
 * WB_API_TEST_DATE_FROM_ 00:00:00 — WB_API_TEST_DATE_TO_ 23:59:59.
 * Сравнение строковое по нормализованному "YYYY-MM-DD HH:MM:SS".
 */
function wbApiTestInPeriod_(dateVal) {
  if (dateVal === undefined || dateVal === null || dateVal === '') return false;
  var s = String(dateVal).replace('T', ' ');
  if (s.length > 19) s = s.substring(0, 19);
  if (s.length === 10) s = s + ' 00:00:00';
  var lo = WB_API_TEST_DATE_FROM_ + ' 00:00:00';
  var hi = WB_API_TEST_DATE_TO_ + ' 23:59:59';
  return s >= lo && s <= hi;
}

/** Уникальные nmId + доля unmatched относительно SKU_MASTER. */
function wbApiTestUniqueAndUnmatched_(nmIds, skuSet) {
  var seen = {}, unmatched = 0;
  for (var i = 0; i < nmIds.length; i++) seen[nmIds[i]] = true;
  var uniqueList = Object.keys(seen);
  if (skuSet && Object.keys(skuSet).length) {
    for (var j = 0; j < uniqueList.length; j++) if (!skuSet[uniqueList[j]]) unmatched++;
  } else {
    unmatched = -1; // SKU_MASTER недоступен → не считаем
  }
  return { unique: uniqueList.length, unmatched: unmatched };
}

/** Есть ли поле (по кандидатам) хотя бы в первой строке. */
function wbApiTestHasField_(arr, names) {
  if (!arr || !arr.length) return false;
  return wbApiTestPick_(arr[0], names) !== undefined;
}

/** Базовое решение по наличию данных. */
function wbApiTestDecideRows_(r, c) {
  if (r.errors.length && r.rowsCount === 0) return WB_API_TEST_DECISION_FAILED_;
  if (r.httpStatus !== null && r.httpStatus !== 200 && r.httpStatus !== 204) {
    return r.rowsCount > 0 ? WB_API_TEST_DECISION_PARTIAL_ : WB_API_TEST_DECISION_FAILED_;
  }
  if (r.rowsCount === 0) return WB_API_TEST_DECISION_PARTIAL_; // 200/204, но пусто
  if (r.errors.length) return WB_API_TEST_DECISION_PARTIAL_;
  return WB_API_TEST_DECISION_PASSED_;
}

/** Контрольные суммы финансовых тестов (T1/T2) по карте полей. */
function wbApiTestFinanceChecksums_(rows, moneyMap, nmIdNames, sridNames, rrdNames) {
  var c = { gross: 0, forPay: 0, logistics: 0, storage: 0, deductions: 0,
    acceptance: 0, acquiring: 0, rebillLogistics: 0, qty: 0, rows: rows.length,
    rowsWithoutNmId: 0, uniqueNmId: 0, hasSrid: false, hasRrd: false };
  var seen = {};
  for (var i = 0; i < rows.length; i++) {
    var row = rows[i];
    c.gross += wbApiTestMoney_(wbApiTestPick_(row, moneyMap.gross));
    c.forPay += wbApiTestMoney_(wbApiTestPick_(row, moneyMap.forPay));
    c.logistics += wbApiTestMoney_(wbApiTestPick_(row, moneyMap.logistics));
    c.storage += wbApiTestMoney_(wbApiTestPick_(row, moneyMap.storage));
    c.deductions += wbApiTestMoney_(wbApiTestPick_(row, moneyMap.deductions));
    c.acceptance += wbApiTestMoney_(wbApiTestPick_(row, moneyMap.acceptance));
    c.acquiring += wbApiTestMoney_(wbApiTestPick_(row, moneyMap.acquiring));
    if (moneyMap.rebillLogistics) c.rebillLogistics += wbApiTestMoney_(wbApiTestPick_(row, moneyMap.rebillLogistics));
    c.qty += Number(wbApiTestPick_(row, moneyMap.qty) || 0);
    var nm = wbApiTestNormNmId_(wbApiTestPick_(row, nmIdNames));
    if (nm) seen[nm] = true; else c.rowsWithoutNmId++;
  }
  c.uniqueNmId = Object.keys(seen).length;
  c.hasSrid = wbApiTestHasField_(rows, sridNames);
  c.hasRrd = wbApiTestHasField_(rows, rrdNames);
  // округление
  var keys = ['gross', 'forPay', 'logistics', 'storage', 'deductions', 'acceptance', 'acquiring', 'rebillLogistics'];
  for (var k = 0; k < keys.length; k++) c[keys[k]] = wbApiTestRound_(c[keys[k]]);
  return c;
}
