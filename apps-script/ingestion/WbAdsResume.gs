/** WB fullstats resume v1. Чистая state machine; I/O только через adapter.
 * Никаких действий при загрузке файла. Активация отдельно от deploy.
 */
var WB_ADS_RESUME_VERSION_ = 1;

function adsResumeAssert_(ok, code) { if (!ok) { var e = new Error(code); e.code = code; throw e; } }
function adsResumeCanonical_(v) {
  if (Array.isArray(v)) return '[' + v.map(adsResumeCanonical_).join(',') + ']';
  if (v && typeof v === 'object') return '{' + Object.keys(v).sort().map(function (k) {
    return JSON.stringify(k) + ':' + adsResumeCanonical_(v[k]);
  }).join(',') + '}';
  return JSON.stringify(v);
}
function adsResumeIds_(ids) {
  adsResumeAssert_(Array.isArray(ids) && ids.length > 0 && ids.length <= 2000, 'CAMPAIGN_COUNT');
  var a = ids.map(function (id) {
    var s = String(id);
    adsResumeAssert_(/^[1-9][0-9]{0,14}$/.test(s) && Number.isSafeInteger(Number(s)), 'CAMPAIGN_ID');
    return s;
  }).sort();
  adsResumeAssert_(a.every(function (id, i) { return !i || id !== a[i - 1]; }), 'DUPLICATE_ID');
  return a;
}
function adsResumeDate_(s) {
  return typeof s === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(s) &&
    Number.isFinite(Date.parse(s)) && new Date(s).toISOString().slice(0, 10) === s;
}
function adsResumeScope_(input, hash) {
  adsResumeAssert_(input && adsResumeDate_(input.logicalDate) && adsResumeDate_(input.from) &&
    adsResumeDate_(input.to) && input.to === input.logicalDate && input.from <= input.to &&
    Date.parse(input.to) - Date.parse(input.from) <= 30 * 86400000, 'DATE_SCOPE');
  adsResumeAssert_(/^[A-Za-z0-9_-]{1,160}$/.test(input.originRunId || '') &&
    /^[A-Za-z0-9_-]{1,160}$/.test(input.parentRunId || ''), 'ORIGIN_REQUIRED');
  var ids = adsResumeIds_(input.campaignIds);
  var allowed = adsResumeIds_(input.allowlist || ids);
  adsResumeAssert_(allowed.every(function (id) { return ids.indexOf(id) >= 0; }), 'ALLOWLIST_SCOPE');
  var campaignHash = hash(adsResumeCanonical_(ids));
  adsResumeAssert_(!input.campaignHash || input.campaignHash === campaignHash, 'CAMPAIGN_HASH');
  var originKind = input.originKind || 'RECOVERY';
  adsResumeAssert_(['RECOVERY', 'DAILY'].indexOf(originKind) >= 0, 'ORIGIN_KIND');
  return { logicalDate: input.logicalDate, from: input.from, to: input.to,
    originRunId: input.originRunId, parentRunId: input.parentRunId,
    campaignIds: ids, campaignHash: campaignHash, allowlist: allowed, originKind: originKind };
}

/** Exact V_ADV_CAMPAIGN_STATS grain (Wbadsbigquery.gs makeView).
 * Validate the WHOLE flattened destination before any RAW publication, including replay.
 * No winner/aggregation/dedupe: identical duplicates are not authorized by the contract.
 * Match existing RAW normalization: omitted/null/empty values load as SQL NULL;
 * all other RAW columns are STRING. Do not truncate dates or coerce numeric keys.
 */
function adsResumeValidateRawGrain_(prepared) {
  adsResumeAssert_(prepared && Array.isArray(prepared.stats), 'RAW_GRAIN_PAYLOAD');
  var fields = ['date', 'advertId', 'nmId', 'appType', 'source_level'];
  var seen = Object.create(null);
  prepared.stats.forEach(function (row) {
    if (row.processed_status !== 'raw') return; // Same predicate as the effective view.
    var key = JSON.stringify(fields.map(function (field) {
      var value = row[field];
      return value === null || value === undefined || value === '' ? null : String(value);
    }));
    adsResumeAssert_(!Object.prototype.hasOwnProperty.call(seen, key), 'RAW_GRAIN_DUPLICATE');
    seen[key] = true;
  });
}

/** Evidence извлекает adapter, а не аргументы публичного entrypoint. */
function adsResumeCreate_(input, evidence, io) {
  var scope = adsResumeScope_(input, io.hash);
  adsResumeAssert_(evidence.campaigns === true && evidence.costs === true && evidence.receipt, 'REQUIRED_PHASES');
  var prior = evidence.covered || {};
  Object.keys(prior).forEach(function (id) {
    adsResumeAssert_(scope.campaignIds.indexOf(id) >= 0 &&
      ['PROCESSED', 'NO_STATS'].indexOf(prior[id]) >= 0, 'PRIOR_COVERAGE');
  });
  var pending = scope.campaignIds.filter(function (id) { return !prior[id]; });
  adsResumeAssert_(pending.every(function (id) { return scope.allowlist.indexOf(id) >= 0; }), 'UNCOVERED_OUTSIDE_ALLOWLIST');
  var key = 'adsr_' + io.hash(adsResumeCanonical_(scope));
  var batches = [];
  for (var i = 0; i < pending.length; i += 50) batches.push({
    id: key + '_b' + batches.length, ids: pending.slice(i, i + 50), state: 'PENDING',
    classifications: {}, receipt: null, attempts: 0, error: null
  });
  return { version: WB_ADS_RESUME_VERSION_, key: key, scope: scope,
    scopeHash: io.hash(adsResumeCanonical_(scope)), revision: 0, phases: evidence,
    batches: batches, state: 'PENDING', createdAt: io.now(), updatedAt: io.now(),
    attempts: 0, noProgress: 0, continuation: { reason: 'INITIAL', needed: true },
    completion: null };
}
function adsResumeValidate_(m, io) {
  adsResumeAssert_(m.version === WB_ADS_RESUME_VERSION_, 'MANIFEST_VERSION');
  adsResumeAssert_(['PENDING', 'READY', 'BLOCKED', 'COMPLETE'].indexOf(m.state) >= 0, 'MANIFEST_STATE');
  var checked = adsResumeScope_(m.scope, io.hash);
  adsResumeAssert_(adsResumeCanonical_(checked) === adsResumeCanonical_(m.scope) &&
    io.hash(adsResumeCanonical_(m.scope)) === m.scopeHash && m.key === 'adsr_' + m.scopeHash, 'SCOPE_HASH');
  var seen = Object.keys(m.phases.covered || {});
  adsResumeAssert_(seen.every(function (id) { return ['PROCESSED', 'NO_STATS'].indexOf(m.phases.covered[id]) >= 0; }), 'PRIOR_COVERAGE');
  m.batches.forEach(function (b, i) {
    adsResumeAssert_(b.id === m.key + '_b' + i && b.ids.length <= 50 &&
      adsResumeCanonical_(adsResumeIds_(b.ids)) === adsResumeCanonical_(b.ids), 'BATCH_ID');
    b.ids.forEach(function (id) {
      adsResumeAssert_(m.scope.allowlist.indexOf(id) >= 0 && seen.indexOf(id) < 0, 'BATCH_SCOPE'); seen.push(id);
    });
    adsResumeAssert_(['PENDING', 'PREPARED', 'DONE', 'FAILED'].indexOf(b.state) >= 0, 'BATCH_STATE');
  });
  adsResumeAssert_(adsResumeCanonical_(seen.sort()) === adsResumeCanonical_(m.scope.campaignIds), 'COVERAGE_SET');
  if (m.state === 'COMPLETE') adsResumeAssert_(adsResumeCoverage_(m).complete && m.completion && m.completion.confirmed === true, 'FALSE_COMPLETE');
}
function adsResumeCoverage_(m) {
  var c = { processed: 0, no_stats: 0, pending: 0, failed: 0, skipped: 0, confirmed: true };
  var all = Object.assign({}, m.phases.covered || {});
  m.batches.forEach(function (b) {
    if (b.state !== 'DONE' || !b.receipt || b.receipt.confirmed !== true || b.receipt.batchId !== b.id) {
      c.confirmed = false; c[b.state === 'FAILED' ? 'failed' : 'pending'] += b.ids.length; return;
    }
    if (Object.keys(b.classifications).length !== b.ids.length) c.confirmed = false;
    b.ids.forEach(function (id) {
      var v = b.classifications[id];
      if (v !== 'PROCESSED' && v !== 'NO_STATS') { c.pending++; c.confirmed = false; }
      else all[id] = v;
    });
  });
  Object.keys(all).forEach(function (id) {
    if (all[id] === 'PROCESSED') c.processed++;
    else if (all[id] === 'NO_STATS') c.no_stats++;
    else c.confirmed = false;
  });
  c.complete = m.phases.campaigns === true && m.phases.costs === true && !!m.phases.receipt &&
    c.confirmed && c.pending === 0 && c.failed === 0 && c.skipped === 0 &&
    c.processed + c.no_stats === m.scope.campaignIds.length;
  return c;
}
function adsResumeSave_(m, io) {
  m.updatedAt = io.now(); m.revision++; m.counts = adsResumeCoverage_(m); io.save(m);
}

/** Одна bounded попытка. PREPARED никогда не получает новый source response. */
function adsResumeStep_(m, io, options) {
  adsResumeValidate_(m, io);
  options = options || {};
  var maxBatches = options.maxBatches === undefined ? 2 : options.maxBatches;
  adsResumeAssert_(Number.isInteger(maxBatches) && maxBatches > 0 && maxBatches <= 4, 'EXECUTION_BOUND');
  if (m.state === 'COMPLETE' || m.state === 'BLOCKED') return m;
  var start = io.now(), before = adsResumeCoverage_(m), progress = 0, done = 0;
  m.continuation.reason = 'PENDING';
  m.attempts++;
  // Persist attempt before I/O: hard kill is observable and cannot reset retry counters.
  adsResumeSave_(m, io);
  for (var i = 0; i < m.batches.length && done < maxBatches; i++) {
    var b = m.batches[i];
    if (b.state === 'DONE') continue;
    if (!io.hasBudget()) { m.continuation.reason = 'DEADLINE'; break; }
    b.attempts++;
    if (b.attempts > 5) { m.state = 'BLOCKED'; b.error = 'ATTEMPT_LIMIT'; break; }
    adsResumeSave_(m, io);
    try {
      // stage() first resolves deterministic landing job, even if no checkpoint survived.
      var prepared = io.stage(m, b);
      adsResumeAssert_(prepared && prepared.batchId === b.id && prepared.scopeHash === m.scopeHash, 'PREPARED_SCOPE');
      adsResumeAssert_(Object.keys(prepared.classifications).sort().join(',') === b.ids.join(',') &&
        b.ids.every(function (id) { return ['PROCESSED', 'NO_STATS'].indexOf(prepared.classifications[id]) >= 0; }), 'PREPARED_COVERAGE');
      adsResumeValidateRawGrain_(prepared);
      if (b.state !== 'PREPARED') progress++;
      b.state = 'PREPARED'; b.error = null; adsResumeSave_(m, io);
      var receipt = io.publish(m, b, prepared);
      adsResumeAssert_(receipt && receipt.confirmed === true && receipt.batchId === b.id, 'SINK_UNCONFIRMED');
      b.classifications = prepared.classifications; b.receipt = receipt; b.state = 'DONE'; progress++; done++;
      adsResumeSave_(m, io);
      io.log({ recovery_id: m.key, logical_date: m.scope.logicalDate, batch_id: b.id,
        campaign_hash: m.scope.campaignHash, state: b.state, counts: adsResumeCoverage_(m),
        budget_remaining_ms: io.remaining() });
    } catch (e) {
      // No WB body / exception message in persisted diagnostics.
      b.error = e && /^[A-Z0-9_]{1,64}$/.test(e.code || '') ? e.code : 'BATCH_IO_ERROR';
      if (b.error === 'RAW_GRAIN_DUPLICATE' || b.error === 'RAW_GRAIN_PAYLOAD') {
        b.state = 'FAILED'; m.state = 'BLOCKED'; // Immutable invalid payload cannot be retried into success.
      } else if (b.state !== 'PREPARED') b.state = 'FAILED';
      m.continuation.reason = b.error;
      adsResumeSave_(m, io); break;
    }
  }
  var coverage = adsResumeCoverage_(m);
  m.noProgress = progress ? 0 : m.noProgress + 1;
  if (coverage.complete) {
    // Final heartbeat is its own replayable step, never mutate origin ERROR.
    m.state = 'READY'; m.continuation.reason = 'FINALIZATION';
    adsResumeSave_(m, io);
    try {
      m.completion = io.finalize(m, coverage);
      adsResumeAssert_(m.completion && m.completion.confirmed === true, 'HEARTBEAT_UNCONFIRMED');
      m.state = 'COMPLETE';
    } catch (e) { m.continuation.reason = e && /^[A-Z0-9_]{1,64}$/.test(e.code || '') ? e.code : 'FINALIZATION_PENDING'; }
  }
  if (m.state !== 'COMPLETE' && m.noProgress >= 3) m.state = 'BLOCKED';
  m.continuation.needed = m.state !== 'COMPLETE' && m.state !== 'BLOCKED';
  if (m.continuation.needed && !m.continuation.reason) m.continuation.reason = 'PENDING';
  adsResumeSave_(m, io);
  io.log({ recovery_id: m.key, origin_run: m.scope.originRunId, parent_run: m.scope.parentRunId,
    logical_date: m.scope.logicalDate, campaign_count: m.scope.campaignIds.length,
    campaign_hash: m.scope.campaignHash, counts: coverage, state: m.state,
    progress: progress, no_progress: m.noProgress, elapsed_ms: io.now() - start,
    budget_remaining_ms: io.remaining(), continuation: m.continuation,
    previous_pending: before.pending + before.failed });
  return m;
}
