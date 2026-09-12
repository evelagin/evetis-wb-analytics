/**
 * UNITKA 2.0 R3 — фактическое платное хранение WB -> BigQuery.
 * Живёт в проекте Apps Script `evetis-wb-analitics` (там лежат токены WB).
 * Токены берутся из Script Properties и НИКОГДА не логируются.
 * Ничего из существующих файлов не трогает. Agent: claude-opus-5, v3.1.1
 *
 * Контракт WB — проверен фактическими ответами 10.09.2026, не по документации
 * (dev.wildberries.ru отдаёт 498 автоматическим запросам):
 *   GET /api/v1/paid_storage?dateFrom=&dateTo=        -> 200 {data:{taskId}}   создание задания
 *   GET /api/v1/paid_storage/tasks/{taskId}/status    -> {data:{status}}       done | canceled | purged
 *   GET /api/v1/paid_storage/tasks/{taskId}/download  -> массив строк отчёта
 *
 * 🔑 Токен: только WB_TOKEN_ANALYTICS. Проверено: STATISTICS / SUPPLIES / FINANCE /
 *    MARKETPLACE дают 403 «scope is not allowed for this resource». POST на этот путь
 *    даёт 405 — эндпоинт GET-овый, хотя и создаёт задание.
 */
var R3_HOST = 'https://seller-analytics-api.wildberries.ru';
var R3_PROJECT = 'project-fa311fc0-4d87-4781-986';
var R3_DATASET = 'wb_raw';
var R3_TABLE   = 'RAW_WB_PAID_STORAGE';
var R3_FROM = '2026-09-01', R3_TO = '2026-09-09';

function r3Tok_(n){ return PropertiesService.getScriptProperties().getProperty(n); }
function r3Fetch_(url, tok){ return UrlFetchApp.fetch(url, { method:'get', headers:{Authorization:tok}, muteHttpExceptions:true }); }
function r3Num_(v){ if(v===null||v===undefined||v==='') return null; var n=Number(v); return isFinite(n)?n:null; }
function r3Day_(v){ var s=String(v||'').slice(0,10); return /^\d{4}-\d{2}-\d{2}$/.test(s)?s:null; }

/** Полный цикл: создать задание, дождаться, скачать, разложить, залить в BigQuery. */
function r3StorageToBq() {
  var tok = r3Tok_('WB_TOKEN_ANALYTICS');
  if(!tok){ Logger.log('Нет WB_TOKEN_ANALYTICS'); return; }
  var L = [];
  var c = r3Fetch_(R3_HOST + '/api/v1/paid_storage?dateFrom=' + R3_FROM + '&dateTo=' + R3_TO, tok);
  if(c.getResponseCode() !== 200){ Logger.log('Создание задания -> ' + c.getResponseCode() + ' | ' + c.getContentText().slice(0,300)); return; }
  var taskId = JSON.parse(c.getContentText()).data.taskId;
  L.push('taskId = ' + taskId);

  var status = '', tries = 0;
  while(tries < 40){
    tries++; Utilities.sleep(5000);
    var s = r3Fetch_(R3_HOST + '/api/v1/paid_storage/tasks/' + taskId + '/status', tok);
    if(s.getResponseCode() !== 200){ L.push('status -> ' + s.getResponseCode() + ' | ' + s.getContentText().slice(0,200)); break; }
    status = String((JSON.parse(s.getContentText()).data||{}).status||'').toLowerCase();
    if(status === 'done' || status === 'canceled' || status === 'purged') break;
  }
  L.push('статус после ' + tries + ' опросов: ' + status);
  if(status !== 'done'){ Logger.log(L.join('\n')); return; }

  var d = r3Fetch_(R3_HOST + '/api/v1/paid_storage/tasks/' + taskId + '/download', tok);
  if(d.getResponseCode() !== 200){ L.push('download -> ' + d.getResponseCode()); Logger.log(L.join('\n')); return; }
  var raw = JSON.parse(d.getContentText());
  // Пустой отчёт — отказ, а не «хранения не было»: у товара на складе WB строки есть всегда.
  if(!raw || !raw.length){ L.push('Отчёт пуст — это отказ'); Logger.log(L.join('\n')); return; }
  L.push('строк в отчёте: ' + raw.length);

  var obs = 'WBPS_' + R3_FROM.replace(/-/g,'') + '_' + R3_TO.replace(/-/g,'');
  var runId = 'r3_' + new Date().getTime(), nowIso = new Date().toISOString();
  var rows = [], skipped = 0;
  for(var i=0;i<raw.length;i++){
    var r = raw[i], day = r3Day_(r.date), nm = r3Num_(r.nmId);
    if(day===null || nm===null){ skipped++; continue; }
    rows.push({ json: {
      observation_id: obs, run_id: runId, observed_at: nowIso,
      date_msk: day, nm_id: nm, chrt_id: r3Num_(r.chrtId), barcode: String(r.barcode||''),
      warehouse: String(r.warehouse||''), office_id: r3Num_(r.officeId),
      warehouse_coef: r3Num_(r.warehouseCoef), log_warehouse_coef: r3Num_(r.logWarehouseCoef),
      subject: String(r.subject||''), brand: String(r.brand||''), vendor_code: String(r.vendorCode||''),
      volume: r3Num_(r.volume), calc_type: String(r.calcType||''),
      warehouse_price: r3Num_(r.warehousePrice), barcodes_count: r3Num_(r.barcodesCount),
      pallet_place_code: r3Num_(r.palletPlaceCode), pallet_count: r3Num_(r.palletCount),
      loyalty_discount: r3Num_(r.loyaltyDiscount),
      tariff_fix_date: String(r.tariffFixDate||''), tariff_lower_date: String(r.tariffLowerDate||''),
      raw_row_json: JSON.stringify(r), ingested_at: nowIso } });
  }
  L.push('к записи: ' + rows.length + ' | отброшено (нет даты/nmId): ' + skipped);

  // ИДЕМПОТЕНТНОСТЬ: insertAll не дедуплицирует, поэтому окно сначала вычищается.
  // Без этого повторный запуск удвоил бы хранение — и все выводы по деньгам вместе с ним.
  var del = 'DELETE FROM `' + R3_PROJECT + '.' + R3_DATASET + '.' + R3_TABLE +
            '` WHERE date_msk BETWEEN DATE("' + R3_FROM + '") AND DATE("' + R3_TO + '")';
  var job = BigQuery.Jobs.query({ query: del, useLegacySql: false }, R3_PROJECT);
  L.push('окно очищено перед записью: ' + (job.numDmlAffectedRows || 0) + ' строк удалено');

  var written = 0, errs = [];
  for(var off=0; off<rows.length; off+=500){
    var chunk = rows.slice(off, off+500);
    var resp = BigQuery.Tabledata.insertAll({ rows: chunk, skipInvalidRows: false, ignoreUnknownValues: false },
                                            R3_PROJECT, R3_DATASET, R3_TABLE);
    if(resp.insertErrors && resp.insertErrors.length){ errs.push(JSON.stringify(resp.insertErrors[0]).slice(0,300)); }
    else written += chunk.length;
  }
  L.push('записано в BigQuery: ' + written + (errs.length ? ' | ОШИБКИ: ' + errs.join(' ; ') : ''));
  Logger.log(L.join('\n'));
}

/**
 * Разведка прав токенов. Значения токенов не выводятся — только имена и коды ответов.
 * Результат прогона 10.09.2026:
 *   paid_storage           WB_TOKEN_ANALYTICS -> 200 (остальные 403 scope)
 *   nm-report GET          WB_TOKEN_ANALYTICS -> 200
 *   nm-report POST         WB_TOKEN_ANALYTICS -> 403 «Report not available»  <-- блокер воронки
 *   nm-report/detail/history                  -> 404 path not found (ручка снята)
 */
function r3ProbeScopes() {
  var L = [], F = '2026-09-01', T = '2026-09-09';
  var toks = ['WB_TOKEN_ANALYTICS','WB_TOKEN_STATISTICS','WB_TOKEN_SUPPLIES','WB_TOKEN_FINANCE','WB_TOKEN_MARKETPLACE'];
  for (var i = 0; i < toks.length; i++) {
    var t = r3Tok_(toks[i]); if (!t) { L.push(toks[i] + ': ключа нет'); continue; }
    var r = r3Fetch_(R3_HOST + '/api/v1/paid_storage?dateFrom=' + F + '&dateTo=' + T, t);
    L.push('paid_storage [' + toks[i] + '] -> ' + r.getResponseCode());
  }
  var a = r3Tok_('WB_TOKEN_ANALYTICS');
  if (a) {
    var g = r3Fetch_(R3_HOST + '/api/v2/nm-report/downloads', a);
    L.push('nm-report GET  [WB_TOKEN_ANALYTICS] -> ' + g.getResponseCode());
    var p = UrlFetchApp.fetch(R3_HOST + '/api/v2/nm-report/downloads', {
      method: 'post', contentType: 'application/json', headers: { Authorization: a },
      payload: JSON.stringify({ id: 'b7c9d1e2-3f40-4a5b-8c6d-7e8f90a1b2c3', reportType: 'DETAIL_HISTORY_REPORT',
        userReportName: 'probe', params: { startDate: F, endDate: T, timezone: 'Europe/Moscow',
        aggregationLevel: 'day', skipDeletedNm: false } }), muteHttpExceptions: true });
    L.push('nm-report POST [WB_TOKEN_ANALYTICS] -> ' + p.getResponseCode() + ' | ' + String(p.getContentText()).slice(0, 200));
  }
  Logger.log(L.join('\n'));
}
