const vm = require('vm'), fs = require('fs'), path = require('path');
function load(dir, files, clock) {
  const log = [];
  const props = {};
  const table = [];   // INGEST_RUNS
  const triggers = [];  // ScriptApp project triggers
  let lockAcquirable = true;
  const P = (params, n) => { const p = (params||[]).find(x => x.name === n); if (!p) return undefined;
    if (p.parameterType.type === 'ARRAY') return p.parameterValue.arrayValues.map(v => v.value); return p.parameterValue.value; };
  const ts = s => Date.parse(s);
  const fakeQuery = (req) => {
    const sql = req.query, pr = req.queryParameters;
    if (/^INSERT INTO/.test(sql)) { table.push({ run_id: P(pr,'run_id'), loader_name: P(pr,'loader_name'), logical_period: P(pr,'logical_period'),
      status: 'STARTED', source: P(pr,'source'), trigger_type: P(pr,'trigger_type'), started_at: clock.now(), completed_at: null });
      return { jobComplete: true, numDmlAffectedRows: '1', rows: [] }; }
    const reaperWhere = r => r.status === 'STARTED' && r.source === P(pr,'source') && r.started_at < ts(P(pr,'cutoff')) &&
      r.started_at >= ts(P(pr,'activation')) && (!/loader_name=@loader_name/.test(sql) || r.loader_name === P(pr,'loader_name'));
    if (/^SELECT run_id FROM/.test(sql)) {
      return { jobComplete: true, rows: table.filter(reaperWhere).sort((a,b)=>a.started_at-b.started_at).map(r => ({ f: [{ v: r.run_id }] })) }; }
    if (/^UPDATE/.test(sql) && /IN UNNEST\(@ids\)/.test(sql)) {
      const ids = P(pr,'ids'); let n = 0;
      table.forEach(r => { if (reaperWhere(r) && ids.includes(r.run_id)) { r.status = 'ERROR'; r.completed_at = clock.now(); r.error_code = P(pr,'error_code'); r.error_message = P(pr,'error_message'); n++; } });
      return { jobComplete: true, numDmlAffectedRows: String(n), rows: [] }; }
    if (/^UPDATE/.test(sql) && /WHERE run_id=@run_id AND status="STARTED"/.test(sql)) {
      const to = /status="COMPLETE"/.test(sql) ? 'COMPLETE' : 'ERROR'; let n = 0;
      table.forEach(r => { if (r.run_id === P(pr,'run_id') && r.status === 'STARTED') { r.status = to; r.completed_at = clock.now(); n++; } });
      return { jobComplete: true, numDmlAffectedRows: String(n), rows: [] }; }
    if (/^SELECT status FROM/.test(sql)) { const r = table.find(x => x.run_id === P(pr,'run_id')); return { jobComplete: true, rows: r ? [{ f: [{ v: r.status }] }] : [] }; }
    if (/^SELECT status, run_id, TIMESTAMP_DIFF/.test(sql)) {
      const c = table.filter(r => r.loader_name === P(pr,'loader_name') && r.logical_period === P(pr,'lp')).sort((a,b)=> b.started_at-a.started_at || (b.run_id>a.run_id?1:-1));
      return { jobComplete: true, rows: c.length ? [{ f: [{ v: c[0].status }, { v: c[0].run_id }, { v: Math.floor((clock.now()-c[0].started_at)/60000) }] }] : [] }; }
    if (/MAX\(`date`\)/.test(sql)) return { jobComplete: true, rows: [{ f: [{ v: clock.isoDate(-1) }] }] };
    throw new Error('fake BQ: unsupported SQL ' + sql.slice(0, 80));
  };
  let uuid = 0;
  const ctx = {
    console: { log: m => log.push(String(m)), error: m => log.push('ERR ' + m) },
    Logger: { log: m => log.push(String(m)) },
    Date: class extends Date { constructor(...a) { a.length ? super(...a) : super(clock.now()); } static now() { return clock.now(); } },
    Utilities: { sleep: ms => clock.advance(ms), getUuid: () => 'abcdef12-0000-0000-0000-' + String(++uuid).padStart(12,'0'),
      formatDate: (d, tz, f) => { const x = new Date(d.getTime() + 3*3600e3).toISOString(); // MSK
        return f.replace('yyyy', x.slice(0,4)).replace('MM', x.slice(5,7)).replace('dd', x.slice(8,10)).replace('HH', x.slice(11,13)).replace('mm', x.slice(14,16)).replace('ss', x.slice(17,19)); } },
    PropertiesService: { getScriptProperties: () => ({ getProperty: k => props[k] || null }) },
    LockService: { getScriptLock: () => ({ tryLock: () => lockAcquirable, releaseLock: () => {} }) },
    ScriptApp: {
      getProjectTriggers: () => triggers.slice(),
      deleteTrigger: t => { const i = triggers.indexOf(t); if (i >= 0) triggers.splice(i, 1); },
      newTrigger: fn => { const b = {
        timeBased: () => b, everyDays: () => b,
        atHour: h => { b._h = h; return b; }, nearMinute: m => { b._m = m; return b; },
        inTimezone: tz => { b._tz = tz; return b; },
        create: () => { const t = { getHandlerFunction: () => fn, hour: b._h, minute: b._m, tz: b._tz };
                        triggers.push(t); return t; } };
        return b; }
    },
    BigQuery: { Jobs: { query: req => fakeQuery(req), getQueryResults: () => { throw new Error('no async'); } } },
    getBqConfig_: () => ({ projectId: 'p', datasetId: 'wb_raw', location: 'EU' }),
    bqQuery_: sql => fakeQuery({ query: sql }),
  };
  ctx.globalThis = ctx;
  vm.createContext(ctx);
  // WbAdsProbe constants the loaders rely on
  vm.runInContext('var WB_ADS_IDS_BATCH_=50; var WB_ADS_FULLSTATS_PAUSE_MS_=21000; var WB_ADS_API_HOST_="h"; var WB_ADS_TZ_="Europe/Moscow";', ctx);
  vm.runInContext(fs.readFileSync(path.join(__dirname, 'probe_extract.gs'), 'utf8'), ctx, { filename: 'WbAdsProbe.gs#wbAdsLast7Range_' });
  for (const f of files) vm.runInContext(fs.readFileSync(path.join(dir, f), 'utf8'), ctx, { filename: f });
  return { ctx, table, props, log, triggers, setLock: v => { lockAcquirable = v; } };
}
function makeClock(startIso) { let t = Date.parse(startIso);
  return { now: () => t, advance: ms => { t += ms; }, set: iso => { t = Date.parse(iso); },
    isoDate: off => new Date(t + 3*3600e3 + off*864e5).toISOString().slice(0,10) }; }
module.exports = { load, makeClock };
