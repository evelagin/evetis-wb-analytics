/**
 * ════════════════════════════════════════════════════════════════════════
 * WbSalesRetryTest — self-tests для fix(sales): retry transient WB API rate limits.
 *
 * Детерминированные, БЕЗ реальных запросов к WB и БЕЗ реальных пауз:
 *   • Группы 1–2 инъектируют transport_ в fetchSalesApiData_/salesHttpGet_ (Diff A).
 *   • Группа 3 тестирует саму retry-петлю wbFetchWithRetry_ через инъекцию fetchImpl_
 *     (опциональный тест-сид Diff B в utils.gs). Если Diff B не применён
 *     (arity wbFetchWithRetry_ < 4) — группа 3 БЕЗОПАСНО пропускается и НЕ делает
 *     реальных вызовов; ставится с baseDelayMs:1, чтобы backoff не спал по-настоящему.
 *
 * Запуск: runSalesRetrySelfTests() в редакторе Apps Script → смотреть Logger/return.
 * Побочных эффектов нет: RAW/watermark/INGEST_RUNS/Script Properties не трогаются.
 * ════════════════════════════════════════════════════════════════════════
 */

// ── Фейковый HTTPResponse, совместимый с UrlFetchApp/​wbFetchWithRetry_ ──
function _srtResp_(code, body, headers) {
  return {
    getResponseCode: function () { return code; },
    getContentText: function () { return body != null ? String(body) : ''; },
    getHeaders: function () { return headers || {}; }
  };
}

// transport_ для salesHttpGet_: (url, options, retryOptions) => HTTPResponse.
// Моделирует ИТОГОВЫЙ ответ helper'а (после его внутренних повторов).
function _srtTransportReturns_(code, body) {
  return function (_url, _opt, _ro) { return _srtResp_(code, body); };
}
function _srtTransportThrows_(msg) {
  return function () { throw new Error(msg || 'boom'); };
}

// fetchImpl_ для wbFetchWithRetry_ (Diff B): последовательность ответов + счётчик вызовов.
function _srtSeqFetch_(seq) {
  var state = { i: 0, calls: 0 };
  var fn = function (_u, _o) {
    state.calls++;
    var code = seq[Math.min(state.i, seq.length - 1)];
    state.i++;
    return _srtResp_(code, '{"code":"' + code + '"}');
  };
  fn.state = state;
  return fn;
}

function _srtAssert_(res, name, cond, detail) {
  res.total++;
  if (cond) { res.passed++; res.details.push('PASS  ' + name); }
  else { res.failed++; res.details.push('FAIL  ' + name + (detail ? ' — ' + detail : '')); }
}

function _srtArrEq_(a, b) {
  if (!a || !b || a.length !== b.length) return false;
  for (var i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
  return true;
}

function runSalesRetrySelfTests() {
  var res = { total: 0, passed: 0, failed: 0, details: [] };
  var TOK = 'TESTTOKEN', URL = 'https://example/api/v1/supplier/sales?dateFrom=x';

  // ═══ Группа 0: retry-политика Sales подключена к helper (Diff A, детерминированно, без сети) ═══
  (function () {
    var cap = {};
    var capTransport = function (u, o, ro) { cap.url = u; cap.opt = o; cap.ro = ro; return _srtResp_(200, '[]'); };
    var r0 = salesHttpGet_(URL, TOK, capTransport);
    _srtAssert_(res, '0.helper вызван, ok:true', r0.ok === true && !!cap.ro, JSON.stringify(cap.ro));
    _srtAssert_(res, '0.label=Sales', !!cap.ro && cap.ro.label === 'Sales', cap.ro && cap.ro.label);
    _srtAssert_(res, '0.maxRetries=3', !!cap.ro && cap.ro.maxRetries === 3, String(cap.ro && cap.ro.maxRetries));
    _srtAssert_(res, '0.baseDelayMs=20000', !!cap.ro && cap.ro.baseDelayMs === 20000, String(cap.ro && cap.ro.baseDelayMs));
    _srtAssert_(res, '0.retryCodes=[429,500,502,503,504]',
      !!cap.ro && _srtArrEq_(cap.ro.retryCodes, [429, 500, 502, 503, 504]),
      cap.ro && JSON.stringify(cap.ro.retryCodes));
    _srtAssert_(res, '0.options: get+Authorization+muteHttpExceptions',
      !!cap.opt && cap.opt.method === 'get' && cap.opt.muteHttpExceptions === true &&
      !!cap.opt.headers && cap.opt.headers.Authorization === TOK, JSON.stringify(cap.opt));
  })();

  // ═══ Группа 1: salesHttpGet_ маппинг ответа (Diff A) ═══
  (function () {
    var r1 = salesHttpGet_(URL, TOK, _srtTransportReturns_(200, '[]'));
    _srtAssert_(res, '1.200→ok', r1.ok === true && r1.code === 200, JSON.stringify(r1));

    var r2 = salesHttpGet_(URL, TOK, _srtTransportReturns_(429, '{"code":"461","title":"too many requests"}'));
    _srtAssert_(res, '4a.429-exhausted→ERROR', r2.ok === false && r2.code === 429 &&
      r2.error.indexOf('HTTP 429') === 0, JSON.stringify(r2));

    var r3 = salesHttpGet_(URL, TOK, _srtTransportReturns_(400, 'bad request'));
    _srtAssert_(res, '6a.400→ERROR(no-retry-класс)', r3.ok === false && r3.code === 400, JSON.stringify(r3));

    var r4 = salesHttpGet_(URL, TOK, _srtTransportThrows_('network down'));
    _srtAssert_(res, 'exc→ERROR code0', r4.ok === false && r4.code === 0 &&
      r4.error.indexOf('HTTP исключение') === 0, JSON.stringify(r4));
  })();

  // ═══ Группа 2: fetchSalesApiData_ проверки ПОСЛЕ 200 (Diff A) ═══
  (function () {
    var okBody = '[{"saleID":"S1","date":"2026-01-01","lastChangeDate":"2026-01-01T00:00:00"}]';
    var g1 = fetchSalesApiData_(TOK, '2026-01-01T00:00:00', _srtTransportReturns_(200, okBody));
    _srtAssert_(res, '1b.200+массив→ok', g1.ok === true && (g1.data || []).length === 1, JSON.stringify(g1));

    var g2 = fetchSalesApiData_(TOK, 'x', _srtTransportReturns_(200, '{oops not json'));
    _srtAssert_(res, '8.битый JSON после 200→ERROR', g2.ok === false &&
      String(g2.error).indexOf('Повреждённый JSON') === 0, JSON.stringify(g2));

    var g3 = fetchSalesApiData_(TOK, 'x', _srtTransportReturns_(200, '{}'));
    _srtAssert_(res, 'non-array после 200→ERROR', g3.ok === false &&
      String(g3.error).indexOf('Ответ WB не является массивом') === 0, JSON.stringify(g3));

    var n = (typeof WB_SALES_API_ROWS_CAP_ !== 'undefined') ? WB_SALES_API_ROWS_CAP_ : 80000;
    var big = new Array(n);
    for (var i = 0; i < n; i++) big[i] = 0;
    var g4 = fetchSalesApiData_(TOK, 'x', _srtTransportReturns_(200, JSON.stringify(big)));
    _srtAssert_(res, 'rows-cap→ERROR(partial)', g4.ok === false && g4.partial === true &&
      String(g4.error).indexOf('лимит') >= 0, 'len=' + n + ' ' + JSON.stringify({ ok: g4.ok, partial: g4.partial }));
  })();

  // ═══ Группа 3: сама retry-петля wbFetchWithRetry_ (нужен Diff B: fetchImpl_) ═══
  if (typeof wbFetchWithRetry_ === 'function' && wbFetchWithRetry_.length >= 4) {
    var RO = { label: 'SalesTest', maxRetries: 3, baseDelayMs: 1, retryCodes: [429, 500, 502, 503, 504] };

    var f1 = _srtSeqFetch_([429, 200]);
    var a1 = wbFetchWithRetry_(URL, {}, RO, f1);
    _srtAssert_(res, '2.429→200 retry→OK', a1.getResponseCode() === 200 && f1.state.calls === 2, 'calls=' + f1.state.calls);

    var f2 = _srtSeqFetch_([429, 429, 200]);
    var a2 = wbFetchWithRetry_(URL, {}, RO, f2);
    _srtAssert_(res, '3.429×2→200→OK', a2.getResponseCode() === 200 && f2.state.calls === 3, 'calls=' + f2.state.calls);

    var f3 = _srtSeqFetch_([500, 200]);
    var a3 = wbFetchWithRetry_(URL, {}, RO, f3);
    _srtAssert_(res, '5.500→200 retry→OK', a3.getResponseCode() === 200 && f3.state.calls === 2, 'calls=' + f3.state.calls);

    var f4 = _srtSeqFetch_([429, 429, 429, 429]);
    var a4 = wbFetchWithRetry_(URL, {}, RO, f4);
    _srtAssert_(res, '4b.429 на всех попытках→последний 429', a4.getResponseCode() === 429 && f4.state.calls === 4, 'calls=' + f4.state.calls);

    var f5 = _srtSeqFetch_([400]);
    var a5 = wbFetchWithRetry_(URL, {}, RO, f5);
    _srtAssert_(res, '6b.400 без retry', a5.getResponseCode() === 400 && f5.state.calls === 1, 'calls=' + f5.state.calls);

    var f6 = _srtSeqFetch_([401]);
    var a6 = wbFetchWithRetry_(URL, {}, RO, f6);
    _srtAssert_(res, '7a.401 без retry', a6.getResponseCode() === 401 && f6.state.calls === 1, 'calls=' + f6.state.calls);

    var f7 = _srtSeqFetch_([403]);
    var a7 = wbFetchWithRetry_(URL, {}, RO, f7);
    _srtAssert_(res, '7b.403 без retry', a7.getResponseCode() === 403 && f7.state.calls === 1, 'calls=' + f7.state.calls);

    // end-to-end: salesHttpGet_ поверх реального helper с fetchImpl_ — транзиентный 429→200 даёт ok:true
    var e2e = salesHttpGet_(URL, TOK, function (u, o, _ro) {
      return wbFetchWithRetry_(u, o, RO, _srtSeqFetch_([429, 200]));
    });
    _srtAssert_(res, 'e2e.salesHttpGet поверх retry: 429→200→ok', e2e.ok === true && e2e.code === 200, JSON.stringify(e2e));
  } else {
    res.details.push('SKIP  Группа 3 (retry-петля): требуется Diff B — опциональный тест-сид fetchImpl_ в wbFetchWithRetry_ (utils.gs). ' +
      'Без него петля НЕ тестируется здесь (она уже в проде у orders/stocks). Реальные вызовы НЕ делались.');
  }

  var summary = 'SalesRetry self-tests: ' + res.passed + '/' + res.total + ' PASS, ' + res.failed + ' FAIL';
  Logger.log(summary);
  for (var d = 0; d < res.details.length; d++) Logger.log('  ' + res.details[d]);
  return { summary: summary, passed: res.passed, failed: res.failed, total: res.total, details: res.details };
}
