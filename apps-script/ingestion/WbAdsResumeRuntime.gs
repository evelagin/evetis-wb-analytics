/** Apps Script / BigQuery adapter. По умолчанию выключен.
 * Два независимых разрешения: WB_ADS_RESUME_ENABLED, WB_ADS_RESUME_CONTINUATION_ENABLED.
 * Runtime НЕ создаёт таблицы/листы и НЕ меняет схемы. См. schema.sql перед deploy.
 */
var WB_ADS_RESUME_TABLE_ = 'ADS_FULLSTATS_RECOVERY';
var WB_ADS_RESUME_ACTIVE_ = 'WB_ADS_RESUME_ACTIVE_V1';
var WB_ADS_RESUME_HANDLER_ = 'runWbAdsFullstatsContinuation';
var WB_ADS_RESUME_HTTP_DEADLINE_ = null;
var WB_ADS_RESUME_IO_ = null;

function adsResumeHash_(s) {
  return Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, s, Utilities.Charset.UTF_8)
    .map(function (x) { return ('0' + ((x + 256) % 256).toString(16)).slice(-2); }).join('');
}
function adsResumeEnabled_() {
  return PropertiesService.getScriptProperties().getProperty('WB_ADS_RESUME_ENABLED') === '1';
}
function adsResumeError_(code) { var e = new Error(code); e.code = code; return e; }

/** Deadline относится и к sleep, и к каждой новой HTTP попытке. Не логирует URL/token/body. */
function adsResumeHttp_(method, url, token, payload, deadline) {
  var opt = { method: method, headers: { Authorization: token }, muteHttpExceptions: true };
  if (payload != null) { opt.contentType = 'application/json'; opt.payload = JSON.stringify(payload); }
  var response;
  for (var attempt = 0; attempt < 3; attempt++) {
    if (Date.now() + 30000 >= deadline) throw adsResumeError_('DEADLINE');
    try { response = UrlFetchApp.fetch(url, opt); }
    catch (e) { throw adsResumeError_('HTTP_NETWORK'); }
    var code = response.getResponseCode();
    if (code !== 429 && code < 500) break;
    if (attempt === 2) break;
    var headers = response.getAllHeaders ? response.getAllHeaders() : {};
    var retry = headers['Retry-After'] || headers['retry-after'];
    var delay = Math.max(21000 * (attempt + 1), /^[0-9]+$/.test(String(retry)) ? Number(retry) * 1000 :
      (Number.isFinite(Date.parse(retry)) ? Math.max(0, Date.parse(retry) - Date.now()) : 0));
    if (Date.now() + delay + 30000 >= deadline) throw adsResumeError_('DEADLINE');
    Utilities.sleep(delay);
  }
  var body = response.getContentText(), json;
  try { json = JSON.parse(body); } catch (e) { json = null; }
  return { code: response.getResponseCode(), ok: response.getResponseCode() >= 200 && response.getResponseCode() < 300,
    json: json, body: '', attempts: attempt + 1 };
}

function adsResumeRuntime_(started) {
  var c = getBqConfig_();
  adsResumeAssert_(c.projectId === 'project-fa311fc0-4d87-4781-986' && c.datasetId === 'wb_raw' &&
    c.location === 'EU' && wbAdsBqSinkOn_(), 'RUNTIME_CONTEXT');
  var ledgerSchema = BigQuery.Tables.get(c.projectId, c.datasetId, WB_ADS_RESUME_TABLE_).schema;
  var required = { recovery_id: 'STRING', kind: 'STRING', record_key: 'STRING', revision: 'INTEGER',
    logical_date: 'DATE', payload: 'STRING', digest: 'STRING' }, actualFields = {};
  ((ledgerSchema || {}).fields || []).forEach(function (f) { actualFields[f.name] = f.type; });
  adsResumeAssert_(Object.keys(required).every(function (name) { return required[name] === actualFields[name]; }), 'LEDGER_SCHEMA');
  var finishBy = started + 330000; // 30 s до hard wall; полный source stop ещё на 90 s раньше.
  var properties = PropertiesService.getScriptProperties();
  var lastHttp = Number(properties.getProperty('WB_ADS_RESUME_LAST_HTTP_AT') || 0);
  adsResumeAssert_(Number.isFinite(lastHttp) && lastHttp >= 0, 'HTTP_CLOCK_STATE');
  function room(ms) { if (Date.now() + ms >= finishBy) throw adsResumeError_('DEADLINE'); }
  function fqn(table) { return '`' + c.projectId + '.wb_raw.' + table + '`'; }
  function param(name, value) { return ingestParam_(name, 'STRING', value); }
  function jobGet(id) {
    room(10000);
    try { return BigQuery.Jobs.get(c.projectId, id, { location: c.location }); }
    catch (e) {
      if (/not found|notFound|\b404\b/i.test(String(e.message))) return null;
      throw adsResumeError_('BQ_LOOKUP_UNKNOWN');
    }
  }
  function wait(j) {
    while (j.status.state !== 'DONE') {
      room(10000); Utilities.sleep(1000); j = jobGet(j.jobReference.jobId);
      adsResumeAssert_(j, 'BQ_JOB_DISAPPEARED');
    }
    adsResumeAssert_(!j.status.errorResult, 'BQ_JOB_FAILED'); return j;
  }
  function query(sql, params, id) {
    room(30000);
    var jobId = id || 'adsr_q_' + Utilities.getUuid().replace(/-/g, '');
    var cfg = { query: { query: sql, useLegacySql: false, parameterMode: 'NAMED', queryParameters: params || [],
      maximumBytesBilled: '100000000', useQueryCache: false } };
    var j = jobGet(jobId);
    if (!j) {
      try { j = BigQuery.Jobs.insert({ jobReference: { projectId: c.projectId, location: c.location, jobId: jobId }, configuration: cfg }, c.projectId); }
      catch (e) { j = jobGet(jobId); if (!j) throw adsResumeError_('BQ_SUBMISSION_UNKNOWN'); }
    }
    adsResumeAssert_(j.configuration.query.query === sql &&
      adsResumeCanonical_(j.configuration.query.queryParameters || []) === adsResumeCanonical_(params || []), 'QUERY_JOB_COLLISION');
    wait(j); room(10000);
    var res = BigQuery.Jobs.getQueryResults(c.projectId, jobId, { location: c.location, maxResults: 10000, timeoutMs: 1000 });
    adsResumeAssert_(res.jobComplete && !res.pageToken && !res.errors, 'QUERY_INCOMPLETE');
    return (res.rows || []).map(function (r) { return JSON.parse(r.f[0].v); });
  }
  function normalize(rows) {
    return rows.map(function (o) { var out = {}; Object.keys(o).forEach(function (k) {
      if (o[k] !== null && o[k] !== undefined && o[k] !== '') out[k] = String(o[k]);
    }); return out; });
  }
  function load(table, rows, id) {
    if (!rows.length) return;
    room(30000);
    var digest = adsResumeHash_(adsResumeCanonical_(rows)).slice(0, 63);
    var j = jobGet(id);
    if (!j) {
      var cfg = { labels: { ads_resume_hash: digest }, load: {
        destinationTable: { projectId: c.projectId, datasetId: c.datasetId, tableId: table },
        sourceFormat: 'NEWLINE_DELIMITED_JSON', createDisposition: 'CREATE_NEVER', writeDisposition: 'WRITE_APPEND',
        ignoreUnknownValues: false, maxBadRecords: 0 } };
      var blob = Utilities.newBlob(rows.map(function (r) { return JSON.stringify(r); }).join('\n'), 'application/octet-stream');
      try { j = BigQuery.Jobs.insert({ jobReference: { projectId: c.projectId, location: c.location, jobId: id }, configuration: cfg }, c.projectId, blob); }
      catch (e) { j = jobGet(id); if (!j) throw adsResumeError_('BQ_SUBMISSION_UNKNOWN'); }
    }
    adsResumeAssert_(j.configuration.labels && j.configuration.labels.ads_resume_hash === digest &&
      j.configuration.load.destinationTable.tableId === table &&
      j.configuration.load.destinationTable.projectId === c.projectId &&
      j.configuration.load.destinationTable.datasetId === c.datasetId, 'LOAD_JOB_COLLISION');
    j = wait(j);
    adsResumeAssert_(Number(j.statistics.load.outputRows) === rows.length, 'LOAD_ROW_COUNT');
  }
  function records(key, kind, recordKey) {
    return query('SELECT TO_JSON_STRING(t) FROM ' + fqn(WB_ADS_RESUME_TABLE_) +
      ' t WHERE recovery_id=@key AND kind=@kind AND record_key=@record ORDER BY revision DESC LIMIT 2',
      [param('key', key), param('kind', kind), param('record', recordKey)]);
  }
  function record(key, kind, recordKey, revision, logicalDate, value) {
    var payload = adsResumeCanonical_(value);
    adsResumeAssert_(Utilities.newBlob(payload).getBytes().length <= 5000000, 'PAYLOAD_BOUND');
    return { recovery_id: key, kind: kind, record_key: recordKey, revision: revision, logical_date: logicalDate,
      payload: payload, digest: adsResumeHash_(payload) };
  }
  function decode(r) {
    adsResumeAssert_(adsResumeHash_(r.payload) === r.digest, 'JOURNAL_HASH'); return JSON.parse(r.payload);
  }
  function loadManifest(key) {
    var rows = records(key, 'MANIFEST', key);
    if (!rows.length) {
      var initial = jobGet(key + '_m1');
      if (initial) { wait(initial); rows = records(key, 'MANIFEST', key); adsResumeAssert_(rows.length, 'MANIFEST_READBACK'); }
    }
    if (!rows.length) return null;
    adsResumeAssert_(rows.length < 2 || rows[0].revision !== rows[1].revision, 'MANIFEST_DUPLICATE');
    return decode(rows[0]);
  }
  function evidence(scope) {
    var p = [param('origin', scope.originRunId), param('parent', scope.parentRunId),
      param('from', scope.from), param('to', scope.to)];
    var heart = query('SELECT TO_JSON_STRING(STRUCT(run_id,logical_period,status,error_code,error_message,started_at,completed_at)) FROM ' + fqn('INGEST_RUNS') +
      ' WHERE run_id=@parent AND loader_name="ads" AND source="apps_script" AND logical_period=CAST(@to AS DATE) LIMIT 2', p);
    adsResumeAssert_(heart.length === 1 && heart[0].status === 'ERROR' && heart[0].error_code === 'ADS_PARTIAL', 'ORIGIN_HEARTBEAT');
    // Original loader summary is evidence only together with RAW readback below.
    var note = String(heart[0].error_message || '');
    var match = /raw_campaigns=OK\((\d+)\)/.exec(note);
    adsResumeAssert_(match && /raw_costs=OK\(/.test(note), 'PHASE_PROVENANCE');
    var camps = query('SELECT TO_JSON_STRING(STRUCT(advertId,status,load_ts)) FROM ' + fqn('RAW_WB_ADV_CAMPAIGNS') +
      ' WHERE run_id=@origin LIMIT 2001', p);
    adsResumeAssert_(camps.length === Number(match[1]), 'CAMPAIGNS_READBACK');
    // Legacy heartbeat has no origin column: require all RAW campaign timestamps inside its execution.
    adsResumeAssert_(camps.every(function (r) {
      var t = /^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$/.test(r.load_ts) ? Date.parse(r.load_ts.replace(' ', 'T') + '+03:00') : NaN;
      return t >= Date.parse(heart[0].started_at) - 1000 && t <= Date.parse(heart[0].completed_at);
    }), 'ORIGIN_TIME_PROVENANCE');
    var ids = adsResumeIds_(camps.filter(function (r) { return ['7', '9', '11'].indexOf(r.status) >= 0; }).map(function (r) { return r.advertId; }));
    adsResumeAssert_(adsResumeCanonical_(ids) === adsResumeCanonical_(scope.campaignIds), 'CAMPAIGN_SNAPSHOT_MISMATCH');
    var costs = query('SELECT TO_JSON_STRING(STRUCT(run_id,window_index,period_from,period_to,status,http_success,returned_rows,rows_out_of_window)) FROM ' +
      fqn('RAW_WB_ADV_COSTS_RUNS') + ' WHERE run_id=@origin AND period_from<=@to AND period_to>=@to LIMIT 2', p);
    adsResumeAssert_(costs.length === 1 && costs[0].status === 'OK' && costs[0].http_success === 'true' &&
      Number(costs[0].rows_out_of_window) === 0, 'COSTS_MARKER');
    var costCount = query('SELECT TO_JSON_STRING(STRUCT(COUNT(*) AS n)) FROM ' + fqn('RAW_WB_ADV_COSTS') +
      ' WHERE run_id=@origin AND window_index=@window AND period_from=@cf AND period_to=@ct',
      p.concat([param('window', costs[0].window_index), param('cf', costs[0].period_from), param('ct', costs[0].period_to)]));
    adsResumeAssert_(Number(costCount[0].n) === Number(costs[0].returned_rows), 'COSTS_READBACK');
    var markers = query('SELECT TO_JSON_STRING(STRUCT(advertId,COUNT(*) AS n)) FROM ' + fqn('RAW_WB_ADV_CAMPAIGN_STATS') +
      ' WHERE run_id=@origin AND period_from=@from AND period_to=@to AND processed_status="no_stats" GROUP BY advertId', p);
    var covered = {};
    markers.forEach(function (r) { adsResumeAssert_(ids.indexOf(r.advertId) >= 0 && Number(r.n) === 1, 'PRIOR_MARKER'); covered[r.advertId] = 'NO_STATS'; });
    return { campaigns: true, costs: true, covered: covered,
      rowsLoaded: camps.length + Number(costs[0].returned_rows),
      receipt: adsResumeHash_(adsResumeCanonical_({ heart: heart, camps: camps, costs: costs, markers: markers })) };
  }
  function stage(m, b) {
    var existing = records(m.key, 'BATCH', b.id);
    if (existing.length) { adsResumeAssert_(existing.length === 1, 'BATCH_DUPLICATE'); return decode(existing[0]); }
    var landingId = b.id + '_landing';
    var pendingJob = jobGet(landingId);
    if (pendingJob) { wait(pendingJob); existing = records(m.key, 'BATCH', b.id); adsResumeAssert_(existing.length === 1, 'LANDING_READBACK'); return decode(existing[0]); }
    adsResumeAssert_(b.state !== 'PREPARED', 'PREPARED_LOST');
    var pause = Math.max(0, lastHttp + 21000 - Date.now());
    if (Date.now() + pause + 30000 >= finishBy - 90000) throw adsResumeError_('DEADLINE');
    if (pause) Utilities.sleep(pause);
    var token = getWbAdsToken_(); adsResumeAssert_(token && token.token, 'TOKEN_UNAVAILABLE');
    properties.setProperty('WB_ADS_RESUME_LAST_HTTP_AT', String(Date.now()));
    var result = adsResumeHttp_('get', WB_ADS_API_HOST_ + '/adv/v3/fullstats?ids=' + b.ids.join(',') +
      '&beginDate=' + m.scope.from + '&endDate=' + m.scope.to, token.token, null, finishBy - 90000);
    lastHttp = Date.now();
    properties.setProperty('WB_ADS_RESUME_LAST_HTTP_AT', String(lastHttp));
    adsResumeAssert_(result.ok, 'HTTP_FAILED');
    var camps = Array.isArray(result.json) ? result.json : result.json && result.json.data;
    adsResumeAssert_(Array.isArray(camps), 'HTTP_INVALID_JSON');
    var classes = {}, seen = {};
    camps.forEach(function (camp) {
      var id = String(camp.advertId !== undefined ? camp.advertId : camp.advertID);
      adsResumeAssert_(b.ids.indexOf(id) >= 0 && !seen[id], 'HTTP_CAMPAIGN_SCOPE'); seen[id] = true;
      adsResumeAssert_(Array.isArray(camp.days), 'HTTP_DAYS_SCHEMA');
      camp.days.forEach(function (day) {
        var rawDate = String(day.date || day.dt || ''), date = rawDate.slice(0, 10);
        adsResumeAssert_(adsResumeDate_(date) && date >= m.scope.from && date <= m.scope.to && Array.isArray(day.apps), 'HTTP_DATE_SCOPE');
        adsResumeAssert_(rawDate === date || (rawDate.charAt(10) === 'T' && Number.isFinite(Date.parse(rawDate))), 'HTTP_DATE_SCHEMA');
        day.apps.forEach(function (app) { adsResumeAssert_(Array.isArray(app.nm || app.nms), 'HTTP_NM_SCHEMA'); });
      });
      (camp.boosterStats || camp.booster_stats || []).forEach(function (r) {
        var raw = String(r.date || ''), d = raw.slice(0, 10);
        adsResumeAssert_(adsResumeDate_(d) && d >= m.scope.from && d <= m.scope.to &&
          (raw === d || (raw.charAt(10) === 'T' && Number.isFinite(Date.parse(raw)))), 'BOOSTER_DATE_SCOPE');
      });
    });
    b.ids.forEach(function (id) { classes[id] = seen[id] ? 'PROCESSED' : 'NO_STATS'; });
    var flat = wbAdvFlattenFullstats_(camps, b.id, m.scope.from, m.scope.to);
    b.ids.forEach(function (id) { if (!seen[id]) flat.statRows.push(wbAdvCampaignStatNoStatsRow_(b.id, Number(id), m.scope.from, m.scope.to)); });
    var prepared = { batchId: b.id, scopeHash: m.scopeHash, classifications: classes,
      stats: normalize(flat.statRows), boosters: normalize(flat.boosterRows), preparedAt: Date.now() };
    var row = record(m.key, 'BATCH', b.id, 0, m.scope.logicalDate, prepared);
    load(WB_ADS_RESUME_TABLE_, [row], landingId);
    existing = records(m.key, 'BATCH', b.id);
    adsResumeAssert_(existing.length === 1 && existing[0].digest === row.digest, 'LANDING_READBACK');
    return decode(existing[0]);
  }
  function publish(m, b, prepared) {
    adsResumeValidateRawGrain_(prepared); // Also guard direct adapter/replay calls before either RAW destination.
    var targets = [['RAW_WB_ADV_CAMPAIGN_STATS', prepared.stats], ['RAW_WB_ADV_BOOSTER_STATS', prepared.boosters]];
    targets.forEach(function (target, i) {
      var table = target[0], rows = target[1];
      // Durable RAW readback also protects against expired BigQuery job metadata.
      var actual = query('SELECT TO_JSON_STRING(t) FROM ' + fqn(table) +
        ' t WHERE run_id=@run AND period_from=@from AND period_to=@to LIMIT 10000',
        [param('run', b.id), param('from', m.scope.from), param('to', m.scope.to)]);
      if (!actual.length && rows.length) {
        load(table, rows, b.id + '_raw' + i);
        actual = query('SELECT TO_JSON_STRING(t) FROM ' + fqn(table) +
          ' t WHERE run_id=@run AND period_from=@from AND period_to=@to LIMIT 10000',
          [param('run', b.id), param('from', m.scope.from), param('to', m.scope.to)]);
      }
      function fingerprint(a) { return adsResumeCanonical_(normalize(a).map(adsResumeCanonical_).sort()); }
      adsResumeAssert_(rows.length < 10000 && fingerprint(actual) === fingerprint(rows), 'RAW_READBACK');
    });
    return { confirmed: true, batchId: b.id,
      rowsLoaded: prepared.stats.filter(function (r) { return r.processed_status === 'raw'; }).length,
      digest: adsResumeHash_(adsResumeCanonical_(prepared)), confirmedAt: Date.now() };
  }
  function finalize(m, coverage) {
    adsResumeValidate_(m, io); adsResumeAssert_(coverage.complete && adsResumeCoverage_(m).complete, 'INCOMPLETE');
    var rid = 'INS_ADS_RESUME_' + m.scopeHash;
    var latest = query('SELECT TO_JSON_STRING(STRUCT(run_id,status)) FROM ' + fqn('INGEST_RUNS') +
      ' WHERE loader_name="ads" AND source="apps_script" AND logical_period=CAST(@date AS DATE)' +
      ' ORDER BY started_at DESC,run_id DESC LIMIT 1', [param('date', m.scope.logicalDate)]);
    adsResumeAssert_(latest.length === 1 && [m.scope.parentRunId, rid].indexOf(latest[0].run_id) >= 0, 'LATEST_ATTEMPT_CONFLICT');
    var lineage = 'ads_resume/v1 origin=' + m.scope.originRunId + ';parent=' + m.scope.parentRunId +
      ';manifest=' + m.key + ';campaign_hash=' + m.scope.campaignHash + ';coverage=' + m.scope.campaignIds.length;
    var loaded = m.phases.rowsLoaded + m.batches.reduce(function (n, b) { return n + b.receipt.rowsLoaded; }, 0);
    adsResumeAssert_(Number.isFinite(loaded) && loaded >= 0, 'ROW_COUNTS');
    var params = [param('run', rid), param('date', m.scope.logicalDate), param('count', String(loaded))];
    query('INSERT INTO ' + fqn('INGEST_RUNS') +
      ' (run_id,loader_name,logical_period,status,source,trigger_type,started_at,completed_at,rows_fetched,rows_loaded)' +
      ' SELECT @run,"ads",CAST(@date AS DATE),"COMPLETE","apps_script","CATCHUP",CURRENT_TIMESTAMP(),CURRENT_TIMESTAMP(),CAST(@count AS INT64),CAST(@count AS INT64)' +
      ' WHERE NOT EXISTS (SELECT 1 FROM ' + fqn('INGEST_RUNS') + ' WHERE run_id=@run)', params, m.key + '_complete');
    var rows = query('SELECT TO_JSON_STRING(STRUCT(status,logical_period,error_message,completed_at)) FROM ' + fqn('INGEST_RUNS') +
      ' WHERE run_id=@run LIMIT 2', params);
    adsResumeAssert_(rows.length === 1 && rows[0].status === 'COMPLETE' && rows[0].logical_period === m.scope.logicalDate &&
      rows[0].error_message == null && rows[0].completed_at, 'HEARTBEAT_READBACK');
    return { confirmed: true, runId: rid, lineage: lineage };
  }
  var io = { hash: adsResumeHash_, now: Date.now, remaining: function () { return finishBy - Date.now(); },
    hasBudget: function () { return Date.now() + 120000 < finishBy; },
    load: loadManifest, evidence: evidence, stage: stage, publish: publish, finalize: finalize,
    save: function (m) { load(WB_ADS_RESUME_TABLE_, [record(m.key, 'MANIFEST', m.key, m.revision, m.scope.logicalDate, m)], m.key + '_m' + m.revision); },
    log: function (event) { console.log('WB_ADS_RESUME ' + JSON.stringify(event)); },
    query: query, fqn: fqn, param: param,
    checkTable: function (table, headers) {
      room(10000);
      var metadata = BigQuery.Tables.get(c.projectId, c.datasetId, table), fields = {};
      ((metadata.schema || {}).fields || []).forEach(function (f) { fields[f.name] = f.type; });
      adsResumeAssert_(headers.every(function (h) { return fields[h] === 'STRING'; }), 'SCHEMA_MISMATCH');
      return false;
    },
    appendPhase: function (table, rows) {
      var normalized = normalize(rows);
      load(table, normalized, 'adsr_phase_' + adsResumeHash_(table + adsResumeCanonical_(normalized)));
      return rows.length;
    },
    startParent: function (origin, date, triggerType) {
      var rid = 'INS_ADS_RESUME_ORIGIN_' + origin;
      query('INSERT INTO ' + fqn('INGEST_RUNS') +
        ' (run_id,loader_name,logical_period,status,source,trigger_type,started_at)' +
        ' SELECT @run,"ads",CAST(@date AS DATE),"STARTED","apps_script",@trigger,CURRENT_TIMESTAMP()' +
        ' WHERE NOT EXISTS (SELECT 1 FROM ' + fqn('INGEST_RUNS') + ' WHERE run_id=@run)',
        [param('run', rid), param('date', date), param('trigger', triggerType)], 'adsr_origin_' + adsResumeHash_(origin));
      return rid;
    },
    finishParent: function (rid, note) {
      query('UPDATE ' + fqn('INGEST_RUNS') + ' SET status="ERROR",completed_at=CURRENT_TIMESTAMP(),error_code="ADS_PARTIAL",error_message=@note' +
        ' WHERE run_id=@run AND status="STARTED"', [param('run', rid), param('note', note)], 'adsr_origin_end_' + adsResumeHash_(rid));
    } };
  return io;
}

function adsResumeContinueSchedule_(m) {
  var props = PropertiesService.getScriptProperties();
  var enabled = props.getProperty('WB_ADS_RESUME_CONTINUATION_ENABLED') === '1';
  var triggers = ScriptApp.getProjectTriggers().filter(function (t) { return t.getHandlerFunction() === WB_ADS_RESUME_HANDLER_; });
  // Under ScriptLock. Remove consumed/stale/duplicate own triggers only.
  triggers.forEach(function (t) { ScriptApp.deleteTrigger(t); });
  if (enabled && m.continuation.needed && m.state !== 'BLOCKED' && m.state !== 'COMPLETE') {
    ScriptApp.newTrigger(WB_ADS_RESUME_HANDLER_).timeBased().after(60000).create();
  }
}

/** Общий ScriptLock также сериализует legacy daily writer. */
function adsResumeWithLock_(fn) {
  adsResumeAssert_(adsResumeEnabled_(), 'RESUME_DISABLED');
  var lock = LockService.getScriptLock(), started = Date.now();
  if (!lock.tryLock(1000)) return { status: 'SKIPPED_LOCKED' };
  try { WB_ADS_RESUME_IO_ = adsResumeRuntime_(started); return fn(WB_ADS_RESUME_IO_); }
  finally { WB_ADS_RESUME_HTTP_DEADLINE_ = null; WB_ADS_RESUME_IO_ = null; lock.releaseLock(); }
}
function adsResumeActive_(io) {
  var raw = PropertiesService.getScriptProperties().getProperty(WB_ADS_RESUME_ACTIVE_);
  if (!raw) return null;
  var spec = JSON.parse(raw), m = io.load(spec.key);
  if (!m) { m = adsResumeCreate_(spec.scope, io.evidence(spec.scope), io); adsResumeSave_(m, io); }
  adsResumeValidate_(m, io);
  adsResumeAssert_(m.key === spec.key && m.scopeHash === io.hash(adsResumeCanonical_(adsResumeScope_(spec.scope, io.hash))), 'ACTIVE_SCOPE_MISMATCH');
  return m;
}
function adsResumeRegister_(input, io) {
  var scope = adsResumeScope_(input, io.hash), key = 'adsr_' + io.hash(adsResumeCanonical_(scope));
  adsResumeAssert_(scope.logicalDate < new Date(io.now() + 3 * 3600000).toISOString().slice(0, 10), 'UNCLOSED_DATE');
  var active = adsResumeActive_(io);
  adsResumeAssert_(!active || active.key === key || active.state === 'COMPLETE', 'ACTIVE_RECOVERY_CONFLICT');
  var existing = io.load(key);
  if (existing) { adsResumeValidate_(existing, io); adsResumeAssert_(existing.key === key, 'EXISTING_SCOPE_MISMATCH'); return existing; }
  var m = adsResumeCreate_(scope, io.evidence(scope), io);
  var compact = Object.assign({}, scope);
  if (adsResumeCanonical_(scope.campaignIds) === adsResumeCanonical_(scope.allowlist)) delete compact.allowlist;
  var pointer = JSON.stringify({ key: key, scope: compact });
  adsResumeAssert_(Utilities.newBlob(pointer).getBytes().length <= 8000, 'POINTER_BOUND');
  PropertiesService.getScriptProperties().setProperty(WB_ADS_RESUME_ACTIVE_, pointer);
  adsResumeSave_(m, io); return m;
}

/** Исторический recovery. Caller передаёт scope, но не может передать COMPLETE/evidence. */
function resumeWbAdsFullstatsRecovery(input) {
  adsResumeAssert_(input && (!input.originKind || input.originKind === 'RECOVERY'), 'RECOVERY_ONLY');
  return adsResumeWithLock_(function (io) {
    var m = adsResumeRegister_(input, io); m = adsResumeStep_(m, io);
    adsResumeContinueSchedule_(m); return { status: m.state, recoveryId: m.key, counts: m.counts };
  });
}
function runWbAdsFullstatsContinuation() {
  return adsResumeWithLock_(function (io) {
    var m = adsResumeActive_(io);
    if (!m) return { status: 'NO_PENDING_RECOVERY' };
    m = adsResumeStep_(m, io); adsResumeContinueSchedule_(m); adsResumeOptional_(m, io);
    return { status: m.state, recoveryId: m.key, counts: m.counts };
  });
}

/** Необязательные источники только ПОСЛЕ durable COMPLETE, только для daily D-1. */
function adsResumeOptional_(m, io) {
  if (m.scope.originKind !== 'DAILY' || m.state !== 'COMPLETE' || m.optional) return;
  var current = wbAdsLast7Range_().to === m.scope.logicalDate;
  m.optional = { state: current && io.hasBudget() ? 'ATTEMPTED' : 'SKIPPED_BUDGET_OR_DATE', at: io.now() };
  adsResumeSave_(m, io); // at-most-once admission; optional ambiguity never retries critical path
  if (m.optional.state === 'ATTEMPTED') {
    WB_ADS_RESUME_HTTP_DEADLINE_ = io.now() + Math.max(0, io.remaining() - 60000);
    try {
      if (typeof loadWbAdsQueryBidsRaw === 'function') loadWbAdsQueryBidsRaw(m.scope.originRunId);
      if (io.hasBudget() && typeof loadWbAdsQueryStatsRaw === 'function') loadWbAdsQueryStatsRaw(m.scope.originRunId);
    } catch (e) { io.log({ recovery_id: m.key, optional: 'ERROR_AFTER_COMPLETE' }); }
    finally { WB_ADS_RESUME_HTTP_DEADLINE_ = null; }
  }
  io.log({ recovery_id: m.key, optional: m.optional.state });
}

/** Daily first resumes the pinned logical period. */
function adsResumeDaily_(triggerType) {
  return adsResumeWithLock_(function (io) {
    var active = adsResumeActive_(io), rng = wbAdsLast7Range_();
    if (active && (active.state !== 'COMPLETE' || active.scope.logicalDate === rng.to)) {
      var resumed = adsResumeStep_(active, io); adsResumeContinueSchedule_(resumed);
      adsResumeOptional_(resumed, io);
      return { status: resumed.state, recoveryId: resumed.key };
    }
    if (triggerType === 'CATCHUP') {
      var latest = io.query('SELECT TO_JSON_STRING(STRUCT(run_id,status,started_at)) FROM ' + io.fqn('INGEST_RUNS') +
        ' WHERE loader_name="ads" AND source="apps_script" AND logical_period=CAST(@date AS DATE)' +
        ' ORDER BY started_at DESC,run_id DESC LIMIT 1', [io.param('date', rng.to)]);
      if (latest.length && latest[0].status === 'COMPLETE') return { status: 'SKIP_COMPLETE' };
      if (latest.length && latest[0].status === 'STARTED' && io.now() - Date.parse(latest[0].started_at) < INGEST_STALE_THRESHOLD_MIN_ * 60000) {
        return { status: 'SKIP_RUNNING' };
      }
    }
    var origin = wbAdsRawNewRunId_(), parent = io.startParent(origin, rng.to, triggerType || 'SCHEDULED');
    WB_ADS_RESUME_HTTP_DEADLINE_ = Date.now() + Math.max(0, io.remaining() - 90000);
    var campaigns = loadWbAdsCampaignsRaw(origin), cr = wbAdsCostsRangeBack_(WB_ADS_COSTS_OPERATIONAL_DAYS_, 1);
    var costs = loadWbAdsCostsRaw(cr.from, cr.to, origin);
    var note = 'origin=' + origin + ' | raw_campaigns=' + campaigns.status + '(' + campaigns.rows + ')' +
      ' | raw_costs=' + costs.status + '(' + costs.rows + ') | raw_fullstats=PARTIAL(0)';
    io.finishParent(parent, note);
    adsResumeAssert_(campaigns.status === 'OK' && costs.status === 'OK', 'PHASES_FAILED');
    var rows = io.query('SELECT TO_JSON_STRING(STRUCT(advertId)) FROM ' + io.fqn('RAW_WB_ADV_CAMPAIGNS') +
      ' WHERE run_id=@run AND status IN ("7","9","11") LIMIT 2001', [io.param('run', origin)]);
    var m = adsResumeRegister_({ originRunId: origin, parentRunId: parent, logicalDate: rng.to,
      from: rng.from, to: rng.to, originKind: 'DAILY', campaignIds: rows.map(function (r) { return r.advertId; }) }, io);
    m = adsResumeStep_(m, io); adsResumeContinueSchedule_(m); adsResumeOptional_(m, io);
    return { status: m.state, recoveryId: m.key };
  });
}
