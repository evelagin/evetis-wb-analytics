// Offline only: real .gs in vm; no Google clients, credentials, network or production writes.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const crypto = require('node:crypto');
const root = path.resolve(__dirname, '../..');
const clone = x => JSON.parse(JSON.stringify(x));
const hash = x => crypto.createHash('sha256').update(x).digest('hex');
const incidentIds = [36047250,37563883,37649856,37669244,37727817,37727876,37727911,37727969,
  37727999,37736048,37738107,37741306,37742306,37751908,37755293,37755348,37756543,37759444,
  37762360,37792295,37845005,37845016,37931352,37932547,37932669,38085124,38581331,39035441,
  39053649,39120213,40042162];
function context(extra = {}) {
  const c = vm.createContext({ console: { log() {} }, ...extra });
  for (const file of ['WbAdsResume.gs', 'WbAdsResumeRuntime.gs']) {
    vm.runInContext(fs.readFileSync(path.join(root, 'apps-script/ingestion', file), 'utf8'), c, { filename: file });
  }
  return c;
}
function core(n = 121, input = {}) {
  const c = context(); let time = Date.parse('2026-10-01T20:59:00Z'), budget = true, saved;
  const landing = new Map(), published = new Map(), heartbeats = new Set(), events = [];
  const io = { hash, now: () => time, remaining: () => 200000, hasBudget: () => budget,
    save: m => { saved = clone(m); }, log: e => events.push(clone(e)),
    stage: (m, b) => {
      if (!landing.has(b.id)) landing.set(b.id, { batchId: b.id, scopeHash: m.scopeHash,
        classifications: Object.fromEntries(b.ids.map(id => [id, 'PROCESSED'])), stats: [] });
      return clone(landing.get(b.id));
    },
    publish: (m, b) => { published.set(b.id, b.ids); return { confirmed: true, batchId: b.id, rowsLoaded: b.ids.length }; },
    finalize: (m, cov) => { assert.equal(cov.complete, true); heartbeats.add(m.key); return { confirmed: true }; }
  };
  const spec = { originRunId: 'ADSRAW_origin', parentRunId: 'INS_ADS_parent', logicalDate: '2026-09-30',
    from: '2026-09-24', to: '2026-09-30', campaignIds: Array.from({ length: n }, (_, i) => i + 1), ...input };
  let m = c.adsResumeCreate_(spec, { campaigns: true, costs: true, receipt: 'verified', rowsLoaded: 0, covered: {} }, io);
  io.save(m);
  return { c, io, landing, published, heartbeats, events, spec,
    step: options => { m = c.adsResumeStep_(m, io, options); return m; },
    get: () => m, restore: () => { m = clone(saved); }, saved: () => clone(saved),
    time: t => { time = Date.parse(t); }, budget: b => { budget = b; } };
}
test('01 normal bounded multi-batch completion', () => {
  const e = core(); assert.equal(e.step().state, 'PENDING'); assert.equal(e.published.size, 2);
  assert.equal(e.step().state, 'COMPLETE'); assert.equal(e.published.size, 3); assert.equal(e.heartbeats.size, 1);
});
test('02 budget exhaustion before next batch preserves pending', () => {
  const e = core(); e.budget(false); e.step(); assert.equal(e.landing.size, 0);
  assert.equal(e.get().counts.pending, 121); assert.equal(e.get().continuation.reason, 'DEADLINE');
});
test('03 interruption after sink write before checkpoint reuses durable batch', () => {
  const e = core(10), publish = e.io.publish, save = e.io.save;
  e.io.publish = (m,b,p) => { publish(m,b,p); e.io.save = () => { throw Error('process killed'); }; throw Error('process killed'); };
  assert.throws(() => e.step()); assert.equal(e.saved().batches[0].state, 'PREPARED');
  e.io.save = save; e.io.publish = publish; e.restore(); assert.equal(e.step().state, 'COMPLETE');
  assert.equal(e.published.size, 1); assert.equal(e.landing.size, 1);
});
test('04 retry completed batch does not refetch or republish', () => {
  const e = core(51); e.step({ maxBatches: 1 });
  const id = e.get().batches[0].id; e.io.stage = (m,b) => { assert.notEqual(b.id, id);
    return { batchId: b.id, scopeHash: m.scopeHash, classifications: { [b.ids[0]]: 'NO_STATS' }, stats: [] }; };
  assert.equal(e.step().state, 'COMPLETE');
});
test('05 duplicate invocation after completion has no side effects', () => {
  const e = core(1); e.step(); const count = e.events.length;
  e.io.save = e.io.stage = e.io.publish = e.io.finalize = () => { throw Error('unexpected I/O'); };
  assert.equal(e.step().state, 'COMPLETE'); assert.equal(e.events.length, count);
});
test('06 ScriptLock contention never constructs runtime', () => {
  const c = context({ PropertiesService: { getScriptProperties: () => ({ getProperty: () => '1' }) },
    LockService: { getScriptLock: () => ({ tryLock: () => false }) } });
  assert.equal(c.runWbAdsFullstatsContinuation().status, 'SKIPPED_LOCKED');
});
test('07 HTTP 429 backoff near deadline stops before sleeping', () => {
  let now = 0, calls = 0, sleeps = 0;
  const c = context({ Date: { now: () => now, parse: Date.parse }, Utilities: { sleep: ms => { now += ms; sleeps++; } },
    UrlFetchApp: { fetch: () => { calls++; return { getResponseCode: () => 429, getAllHeaders: () => ({ 'Retry-After': '120' }) }; } } });
  assert.throws(() => c.adsResumeHttp_('get','fixture','not-a-credential',null,100000), /DEADLINE/);
  assert.equal(calls, 1); assert.equal(sleeps, 0);
});
test('08 network failures cannot become no_stats or COMPLETE', () => {
  const e = core(1); e.io.stage = () => { throw Error('network payload must not leak'); }; e.step();
  assert.equal(e.get().counts.failed, 1); assert.equal(e.get().counts.no_stats, 0); assert.equal(e.heartbeats.size, 0);
  assert.ok(!JSON.stringify(e.saved()).includes('payload must not leak'));
});
test('09 legitimate no_stats is covered only with confirmed writes', () => {
  const e = core(1); e.io.stage = (m,b) => ({ batchId:b.id,scopeHash:m.scopeHash,classifications:{1:'NO_STATS'},stats:[] });
  assert.equal(e.step().counts.no_stats, 1); assert.equal(e.get().state, 'COMPLETE');
});
test('10 omitted/unprocessed ID never becomes no_stats', () => {
  const e = core(2); e.io.stage = (m,b) => ({ batchId:b.id,scopeHash:m.scopeHash,classifications:{1:'NO_STATS'},stats:[] });
  e.step(); assert.equal(e.get().counts.complete, false); assert.equal(e.published.size, 0);
});
test('11 midnight rollover preserves original dates', () => {
  const e = core(51); e.step({ maxBatches:1 }); e.time('2026-10-03T02:00:00Z'); e.restore(); e.step();
  assert.equal(e.get().scope.logicalDate,'2026-09-30'); assert.equal(e.get().scope.from,'2026-09-24');
});
test('12 campaign/hash mutation is rejected before I/O', () => {
  const e = core(2); e.get().scope.campaignIds[0]='999'; assert.throws(() => e.step(), /SCOPE_HASH|CAMPAIGN_HASH|ALLOWLIST_SCOPE/);
  const changedHash = core(2); changedHash.get().scope.campaignHash='bad'; assert.throws(()=>changedHash.step(),/CAMPAIGN_HASH/);
  assert.equal(e.landing.size,0);
});
test('13 no progress reaches terminal BLOCKED without trigger loop', () => {
  const e = core(1); e.io.stage=()=>{throw Error('HTTP');}; e.step(); e.step(); e.step();
  assert.equal(e.get().state,'BLOCKED'); assert.equal(e.get().continuation.needed,false);
  const attempts=e.get().attempts; e.step(); assert.equal(e.get().attempts,attempts);
});
test('14 COMPLETE prevents continuation scheduling', () => {
  const e = core(1); e.step(); let creates=0, deletes=0;
  e.c.PropertiesService={getScriptProperties:()=>({getProperty:()=> '1'})};
  e.c.ScriptApp={getProjectTriggers:()=>[{getHandlerFunction:()=> 'runWbAdsFullstatsContinuation'}],
    deleteTrigger:()=>deletes++,newTrigger:()=>{creates++;throw Error('must not create');}};
  e.c.adsResumeContinueSchedule_(e.get()); assert.equal(creates,0); assert.equal(deletes,1);
});
test('15 every incomplete coverage combination fails closed', () => {
  for (const state of ['PENDING','PREPARED','FAILED','DONE']) {
    const e=core(2), b=e.get().batches[0]; b.state=state;b.classifications={1:'PROCESSED'};b.receipt={confirmed:true};
    assert.equal(e.c.adsResumeCoverage_(e.get()).complete,false);
  }
  const e=core(1);e.get().phases.costs=false;assert.equal(e.c.adsResumeCoverage_(e.get()).complete,false);
});
test('16 latest-wins effective view key preserved (not SUM of appends)', () => {
  // Read definition only: wbAdsBqCreateViews must NEVER execute, even as a deploy shortcut.
  const sql=fs.readFileSync(path.join(root,'apps-script/ingestion/Wbadsbigquery.gs'),'utf8');
  assert.match(sql,/ROW_NUMBER\(\) OVER/i); assert.match(sql,/PARTITION BY[\s\S]*advertId[\s\S]*nmId[\s\S]*appType[\s\S]*source_level/i);
  assert.match(sql,/load_ts[\s\S]*DESC[\s\S]*run_id DESC/i);
});
test('17 original PARTIAL remains rejected by real heartbeat classifier', () => {
  const c=context();vm.runInContext(fs.readFileSync(path.join(root,'apps-script/ingestion/IngestRunLog.gs'),'utf8'),c);
  assert.equal(c.INGEST_SUCCESS_STATUSES_.ads.includes('PARTIAL'),false);
  const sql=fs.readFileSync(path.join(root,'cloud/src/loaders/mart/bq.ts'),'utf8');
  assert.match(sql,/ORDER BY started_at DESC, run_id DESC/);assert.match(sql,/l.status = 'COMPLETE' AND l.completed_at IS NOT NULL/);
});
test('18 supplied dates/allowlist cannot escape immutable scope', () => {
  assert.throws(()=>core(2,{to:'2026-10-01'}),/DATE_SCOPE/);
  assert.throws(()=>core(2,{allowlist:[1]}),/UNCOVERED_OUTSIDE_ALLOWLIST/);
  assert.throws(()=>core(2,{allowlist:[999]}),/ALLOWLIST_SCOPE/);
  const e=core(1);e.get().batches[0].ids=['999'];assert.throws(()=>e.step(),/BATCH_SCOPE/);
});
test('incident fixture 400 proven no_stats + 31 pending, no production calls', () => {
  const e=core(1), prior=Object.fromEntries(Array.from({length:400},(_,i)=>[String(100000+i),'NO_STATS']));
  const m=e.c.adsResumeCreate_({...e.spec,campaignIds:Object.keys(prior).concat(incidentIds.map(String)),allowlist:incidentIds},
    {campaigns:true,costs:true,receipt:'fixture',covered:prior},e.io);
  assert.equal(m.batches.length,1);assert.equal(m.batches[0].ids.length,31);
  assert.equal(e.c.adsResumeCoverage_(m).no_stats,400);assert.equal(e.c.adsResumeCoverage_(m).complete,false);
});
test('source retries obey absolute deadline and do not log request secrets', () => {
  let now=0,calls=0; const logs=[];
  const c=context({Date:{now:()=>now,parse:Date.parse},console:{log:s=>logs.push(s)},Utilities:{sleep:ms=>now+=ms},
    UrlFetchApp:{fetch:()=>{calls++;return {getResponseCode:()=>503,getAllHeaders:()=>({}),getContentText:()=>'{"private":"payload"}'};}}});
  const r=c.adsResumeHttp_('get','fixture','not-a-credential',null,200000);
  assert.equal(calls,3);assert.equal(r.ok,false);assert.equal(logs.length,0);
});
test('finalization ambiguity retries without source replay', () => {
  const e=core(1), final=e.io.finalize; e.io.finalize=()=>{throw Error('uncertain');};e.step();
  assert.equal(e.get().state,'READY');e.io.stage=()=>{throw Error('must not fetch');};e.io.finalize=final;
  assert.equal(e.step().state,'COMPLETE');
});

// Integration fake for real adapter (BQ jobs + persistent journal + RAW readback).
function runtime() {
  let now=Date.parse('2026-10-01T06:00:00Z'),seq=0,fetches=0,locked=false,killOn=null,dead=false;
  const props={WB_ADS_RESUME_ENABLED:'1'}, jobs=new Map(), tables={ADS_FULLSTATS_RECOVERY:[],RAW_WB_ADV_CAMPAIGN_STATS:[],RAW_WB_ADV_BOOSTER_STATS:[],INGEST_RUNS:[],RAW_WB_ADV_CAMPAIGNS:[],RAW_WB_ADV_COSTS:[],RAW_WB_ADV_COSTS_RUNS:[]};
  const pp=ps=>Object.fromEntries((ps||[]).map(p=>[p.name,p.parameterValue.value]));
  const properties={getProperty:k=>props[k]||null,setProperty:(k,v)=>{props[k]=v;}};
  function execute(sql,ps){
    const p=pp(ps);
    if(sql.startsWith('INSERT INTO')){
      if(!tables.INGEST_RUNS.some(r=>r.run_id===p.run))tables.INGEST_RUNS.push({run_id:p.run,status:'COMPLETE',logical_period:p.date,error_message:p.lineage,completed_at:new Date(now).toISOString(),started_at:now});return [];
    }
    if(sql.includes('ADS_FULLSTATS_RECOVERY'))return tables.ADS_FULLSTATS_RECOVERY.filter(r=>r.recovery_id===p.key&&r.kind===p.kind&&r.record_key===p.record).sort((a,b)=>b.revision-a.revision).slice(0,2);
    if(sql.includes('INGEST_RUNS'))return tables.INGEST_RUNS.filter(r=>p.run?r.run_id===p.run:p.parent?r.run_id===p.parent:r.logical_period===p.date).sort((a,b)=>Date.parse(b.started_at)-Date.parse(a.started_at)).slice(0,p.run||p.parent?2:1);
    if(sql.includes('RAW_WB_ADV_CAMPAIGNS'))return tables.RAW_WB_ADV_CAMPAIGNS.filter(r=>r.run_id===p.origin);
    if(sql.includes('RAW_WB_ADV_COSTS_RUNS'))return tables.RAW_WB_ADV_COSTS_RUNS.filter(r=>r.run_id===p.origin&&r.period_from<=p.to&&r.period_to>=p.to);
    if(sql.includes('RAW_WB_ADV_COSTS'))return [{n:tables.RAW_WB_ADV_COSTS.filter(r=>r.run_id===p.origin&&r.window_index===p.window).length}];
    if(sql.includes('GROUP BY advertId'))return tables.RAW_WB_ADV_CAMPAIGN_STATS.filter(r=>r.run_id===p.origin&&r.processed_status==='no_stats').map(r=>({advertId:r.advertId,n:1}));
    for(const table of ['RAW_WB_ADV_CAMPAIGN_STATS','RAW_WB_ADV_BOOSTER_STATS'])if(sql.includes(table))return tables[table].filter(r=>r.run_id===p.run&&r.period_from===p.from&&r.period_to===p.to);
    throw Error('unexpected query '+sql);
  }
  const c=context({Date:class extends Date{constructor(...a){super(...(a.length?a:[now]));}static now(){return now;}},
    PropertiesService:{getScriptProperties:()=>properties},LockService:{getScriptLock:()=>({tryLock:()=>{if(locked)return false;locked=true;return true;},releaseLock:()=>{locked=false;}})},
    getBqConfig_:()=>({projectId:'project-fa311fc0-4d87-4781-986',datasetId:'wb_raw',location:'EU'}),wbAdsBqSinkOn_:()=>true,
    Utilities:{DigestAlgorithm:{SHA_256:1},Charset:{UTF_8:1},computeDigest:(_,s)=>[...crypto.createHash('sha256').update(s).digest()],
      newBlob:s=>({text:s,getBytes:()=>[...Buffer.from(s)]}),getUuid:()=>String(++seq),sleep:ms=>now+=ms},
    ingestParam_:(name,type,value)=>({name,parameterType:{type},parameterValue:{value:String(value)}}),
    WB_ADS_API_HOST_:'fixture',getWbAdsToken_:()=>({token:'fixture-not-secret'}),
    wbAdsNow_:()=> '2026-10-01 09:00:00',
    UrlFetchApp:{fetch:url=>{fetches++;const ids=/ids=([^&]+)/.exec(url)[1].split(',');return {getResponseCode:()=>200,getContentText:()=>JSON.stringify(ids.map(id=>({advertId:Number(id),days:[{date:'2026-09-30',apps:[{appType:1,nm:[{nmId:2,sum:3,views:1}]}]}]})))};}},
    BigQuery:{Tables:{get:()=>({schema:{fields:Object.entries({recovery_id:'STRING',kind:'STRING',record_key:'STRING',revision:'INTEGER',logical_date:'DATE',payload:'STRING',digest:'STRING'}).map(([name,type])=>({name,type}))}})},
      Jobs:{get:(project,id)=>{if(dead)throw Error('process dead');if(!jobs.has(id))throw Error('404 not found');return clone(jobs.get(id));},
      insert:(j,project,blob)=>{if(dead)throw Error('process dead');const id=j.jobReference.jobId;if(jobs.has(id))throw Error('409');
        const job=clone(j);job.status={state:'DONE'};
        if(j.configuration.load){assert.equal(j.configuration.load.createDisposition,'CREATE_NEVER');const rows=blob.text.split('\n').map(JSON.parse);
          const table=j.configuration.load.destinationTable.tableId;tables[table].push(...rows);job.statistics={load:{outputRows:String(rows.length)}};
        }else{assert.equal(j.configuration.query.useLegacySql,false);job.result=execute(j.configuration.query.query,j.configuration.query.queryParameters);}
        jobs.set(id,job);if(killOn&&id.endsWith(killOn)){dead=true;throw Error('process dead');}return clone(job);},
      getQueryResults:(p,id)=>({jobComplete:true,rows:jobs.get(id).result.map(r=>({f:[{v:JSON.stringify(r)}]}))})}},
    ScriptApp:{getProjectTriggers:()=>[],deleteTrigger:()=>{throw Error('no trigger expected');}}
  });
  vm.runInContext(fs.readFileSync(path.join(root,'apps-script/ingestion/WbAdsRawLoader.gs'),'utf8'),c);
  const spec={originRunId:'origin',parentRunId:'parent',logicalDate:'2026-09-30',from:'2026-09-24',to:'2026-09-30',campaignIds:[11,12]};
  tables.INGEST_RUNS.push({run_id:'parent',status:'ERROR',logical_period:spec.logicalDate,started_at:new Date(now-10000).toISOString(),completed_at:new Date(now+1000).toISOString(),error_code:'ADS_PARTIAL',error_message:'raw_campaigns=OK(2) | raw_costs=OK(1) | raw_fullstats=PARTIAL(0)'});
  tables.RAW_WB_ADV_CAMPAIGNS.push(...[11,12].map(advertId=>({run_id:'origin',advertId:String(advertId),status:'11',load_ts:'2026-10-01 09:00:00'})));
  tables.RAW_WB_ADV_COSTS_RUNS.push({run_id:'origin',window_index:'0',period_from:'2026-09-17',period_to:'2026-09-30',status:'OK',http_success:'true',returned_rows:'1',rows_out_of_window:'0'});
  tables.RAW_WB_ADV_COSTS.push({run_id:'origin',window_index:'0'});
  const io=()=>c.adsResumeRuntime_(now);
  const initial=c.adsResumeCreate_(spec,{campaigns:true,costs:true,rowsLoaded:2,receipt:'verified',covered:{}},io());
  c.adsResumeSave_(initial,io());
  props.WB_ADS_RESUME_ACTIVE_V1=JSON.stringify({key:initial.key,scope:initial.scope});
  return {c,tables,jobs,props,io,spec,key:initial.key,fetches:()=>fetches,
    kill:s=>{killOn=s;},revive:()=>{killOn=null;dead=false;now+=60000;},advance:ms=>{now+=ms;},
    step:()=>c.runWbAdsFullstatsContinuation()};
}
test('adapter normal completion confirms RAW and new heartbeat, preserves origin ERROR',()=>{
  const e=runtime();assert.equal(e.step().status,'COMPLETE');assert.equal(e.tables.RAW_WB_ADV_CAMPAIGN_STATS.length,2);
  assert.equal(e.tables.INGEST_RUNS[0].status,'ERROR');assert.equal(e.tables.INGEST_RUNS.length,2);
  assert.equal(e.step().status,'COMPLETE');assert.equal(e.fetches(),1);
});
test('adapter hard kill after RAW append resumes without duplicate rows/source fetch',()=>{
  const e=runtime();e.kill('_raw0');assert.throws(()=>e.step());assert.equal(e.tables.RAW_WB_ADV_CAMPAIGN_STATS.length,2);
  e.revive();assert.equal(e.step().status,'COMPLETE');assert.equal(e.fetches(),1);assert.equal(e.tables.RAW_WB_ADV_CAMPAIGN_STATS.length,2);
});
test('adapter hard kill after landing before checkpoint uses saved response',()=>{
  const e=runtime();e.kill('_landing');assert.throws(()=>e.step());e.revive();assert.equal(e.step().status,'COMPLETE');assert.equal(e.fetches(),1);
});
test('adapter expired job metadata still uses durable RAW readback',()=>{
  const e=runtime();e.kill('_raw0');assert.throws(()=>e.step());e.revive();e.jobs.delete(e.key+'_b0_raw0');
  assert.equal(e.step().status,'COMPLETE');assert.equal(e.tables.RAW_WB_ADV_CAMPAIGN_STATS.length,2);
});
test('adapter corrupted RAW readback never publishes COMPLETE',()=>{
  const e=runtime();e.kill('_raw0');assert.throws(()=>e.step());e.revive();e.tables.RAW_WB_ADV_CAMPAIGN_STATS[0].sum='999';
  assert.notEqual(e.step().status,'COMPLETE');assert.equal(e.tables.INGEST_RUNS.length,1);
});
test('adapter newer same-period attempt blocks stale composite completion',()=>{
  const e=runtime();e.tables.INGEST_RUNS.push({run_id:'newer',status:'ERROR',logical_period:'2026-09-30',started_at:'2026-10-01T07:00:00Z'});
  assert.equal(e.step().status,'READY');assert.equal(e.tables.INGEST_RUNS.length,2);
});
test('adapter historical phases verified against RAW, costs commit marker and parent time interval',()=>{
  const e=runtime();const proof=e.io().evidence(e.c.adsResumeScope_(e.spec,hash));assert.equal(proof.campaigns,true);assert.equal(proof.costs,true);assert.equal(proof.rowsLoaded,3);
  assert.equal(e.fetches(),0);
});
test('adapter historical evidence rejects unconfirmed costs and wrong parent association',()=>{
  const e=runtime();e.tables.RAW_WB_ADV_COSTS=[];assert.throws(()=>e.io().evidence(e.c.adsResumeScope_(e.spec,hash)),/COSTS_READBACK/);
  const f=runtime();f.tables.RAW_WB_ADV_CAMPAIGNS[0].load_ts='2026-09-30 09:00:00';assert.throws(()=>f.io().evidence(f.c.adsResumeScope_(f.spec,hash)),/ORIGIN_TIME_PROVENANCE/);
});
test('adapter malformed JSON, foreign campaign and out-of-window source data are fail closed',()=>{
  for(const body of ['{}','null','[{"advertId":999,"days":[]}]','[{"advertId":11,"days":[{"date":"2026-10-01","apps":[]}]}]']){
    const e=runtime();e.c.UrlFetchApp.fetch=()=>({getResponseCode:()=>200,getContentText:()=>body});
    assert.notEqual(e.step().status,'COMPLETE');assert.equal(e.tables.RAW_WB_ADV_CAMPAIGN_STATS.length,0);assert.equal(e.tables.INGEST_RUNS.length,1);
  }
});
test('continuation reconciles duplicate own triggers; does not touch unrelated handlers',()=>{
  const e=core(51);e.step({maxBatches:1});let made=0;const deleted=[];
  e.c.PropertiesService={getScriptProperties:()=>({getProperty:()=> '1'})};
  const triggers=['runWbAdsFullstatsContinuation','other','runWbAdsFullstatsContinuation'].map((handler,i)=>({i,getHandlerFunction:()=>handler}));
  e.c.ScriptApp={getProjectTriggers:()=>triggers,deleteTrigger:t=>deleted.push(t.i),newTrigger:()=>({timeBased:()=>({after:()=>({create:()=>made++})})})};
  e.c.adsResumeContinueSchedule_(e.get());assert.deepEqual(deleted,[0,2]);assert.equal(made,1);
});
test('daily and catch-up dispatch to resume only with opt-in and preserve provenance',()=>{
  const c=context();vm.runInContext(fs.readFileSync(path.join(root,'apps-script/ingestion/Wbadsdaily.gs'),'utf8'),c);
  c.adsResumeEnabled_=()=>true;c.adsResumeDaily_=type=>type;
  assert.equal(c.runWbAdsDaily(),'MANUAL');assert.equal(c.runWbAdsDaily({triggerUid:'x'}),'SCHEDULED');assert.equal(c.runWbAdsDailyCatchUp(),'CATCHUP');
});
test('optional stages cannot run before COMPLETE or for historical recovery',()=>{
  const e=core(1);let calls=0;e.c.wbAdsLast7Range_=()=>({to:'2026-09-30'});e.c.loadWbAdsQueryBidsRaw=()=>calls++;
  e.c.adsResumeOptional_(e.get(),e.io);e.step();e.c.adsResumeOptional_(e.get(),e.io);assert.equal(calls,0);
});
test('scope/schema/feature preflight fails before any mutation',()=>{
  const e=runtime();e.props.WB_ADS_RESUME_ENABLED='0';assert.throws(()=>e.step(),/RESUME_DISABLED/);
  e.props.WB_ADS_RESUME_ENABLED='1';e.c.BigQuery.Tables.get=()=>({schema:{fields:[]}});
  assert.throws(()=>e.step(),/LEDGER_SCHEMA/);assert.equal(e.tables.RAW_WB_ADV_CAMPAIGN_STATS.length,0);
});
test('enabled catch-up without manifest skips newer COMPLETE/fresh STARTED under lock',()=>{
  for(const [state,expected] of [['COMPLETE','SKIP_COMPLETE'],['STARTED','SKIP_RUNNING']]) {
    const e=runtime();delete e.props.WB_ADS_RESUME_ACTIVE_V1;
    e.tables.INGEST_RUNS[0].status=state;e.c.wbAdsLast7Range_=()=>({from:'2026-09-24',to:'2026-09-30'});e.c.INGEST_STALE_THRESHOLD_MIN_=15;
    assert.equal(e.c.adsResumeDaily_('CATCHUP').status,expected);assert.equal(e.fetches(),0);assert.equal(e.tables.INGEST_RUNS.length,1);
  }
});
test('native adapters route schema/load/status only while resume context is active',()=>{
  const c=context();let checked=0,loaded=0,logged=0;
  vm.runInContext(fs.readFileSync(path.join(root,'apps-script/ingestion/Wbadsbigquery.gs'),'utf8'),c);
  vm.runInContext(fs.readFileSync(path.join(root,'apps-script/ingestion/WbAdsRawLoader.gs'),'utf8'),c);
  c.WB_ADS_RESUME_IO_={checkTable:()=>{checked++;return false;},appendPhase:(t,r)=>{loaded++;return r.length;},log:()=>logged++};
  c.wbAdvBqEnsureTable_('RAW_WB_ADV_CAMPAIGN_STATS',[]);assert.equal(c.wbAdvBqAppendRows_('RAW_WB_ADV_CAMPAIGN_STATS',[{advertId:1}]),1);
  c.wbAdsRawWriteStatus_('r','raw_fullstats','2026-09-24','2026-09-30',{status:'PARTIAL',error_message:'payload should not be sent'});
  assert.deepEqual([checked,loaded,logged],[1,1,1]);
});
test('DONE/COMPLETE labels without verified coverage are rejected',()=>{
  const e=core(1);e.get().state='COMPLETE';e.get().completion={confirmed:true};assert.throws(()=>e.step(),/FALSE_COMPLETE/);
  const f=core(1);f.get().batches[0].state='DONE';f.get().batches[0].classifications={1:'PROCESSED'};f.get().batches[0].receipt={confirmed:false,batchId:f.get().batches[0].id};
  assert.equal(f.c.adsResumeCoverage_(f.get()).complete,false);
});
test('second RAW destination ambiguity confirms both tables before COMPLETE',()=>{
  const e=runtime();e.c.UrlFetchApp.fetch=()=>({getResponseCode:()=>200,getContentText:()=>JSON.stringify([11,12].map(advertId=>({advertId,
    days:[{date:'2026-09-30',apps:[{appType:1,nm:[{nmId:2,sum:3}]}]}],boosterStats:[{date:'2026-09-30',nm:2,avg_position:1}]})))});
  e.kill('_raw1');assert.throws(()=>e.step());assert.equal(e.tables.INGEST_RUNS.length,1);
  e.revive();assert.equal(e.step().status,'COMPLETE');assert.equal(e.tables.RAW_WB_ADV_CAMPAIGN_STATS.length,2);assert.equal(e.tables.RAW_WB_ADV_BOOSTER_STATS.length,2);
});
test('real adapter empty HTTP 200 produces durable no_stats rows',()=>{
  const e=runtime();e.c.UrlFetchApp.fetch=()=>({getResponseCode:()=>200,getContentText:()=> '[]'});
  const result=e.step();assert.equal(result.status,'COMPLETE');assert.equal(result.counts.no_stats,2);
  assert.ok(e.tables.RAW_WB_ADV_CAMPAIGN_STATS.every(r=>r.processed_status==='no_stats'));
});
test('real HTTP network failure is sanitized',()=>{
  const c=context({UrlFetchApp:{fetch:()=>{throw Error('sensitive request body');}}});
  assert.throws(()=>c.adsResumeHttp_('get','fixture','fixture',null,Date.now()+100000),/^Error: HTTP_NETWORK$/);
});
test('cross-execution fullstats cooldown is respected',()=>{
  const e=runtime();e.props.WB_ADS_RESUME_LAST_HTTP_AT=String(Date.parse('2026-10-01T06:00:00Z'));
  const before=e.io().now();assert.equal(e.step().status,'COMPLETE');assert.ok(e.io().now()-before>=21000);
});
test('sink flag changing during execution cannot fall back to Sheet writes',()=>{
  const c=context();vm.runInContext(fs.readFileSync(path.join(root,'apps-script/ingestion/WbAdsRawLoader.gs'),'utf8'),c);
  c.WB_ADS_RESUME_IO_={};c.wbAdsBqSinkOn_=()=>false;
  assert.throws(()=>c.wbAdvRawEnsureSheet_({},'RAW_WB_ADV_CAMPAIGN_STATS',[]),/RESUME_SINK_DISABLED/);
  assert.throws(()=>c.wbAdvRawAppendRows_({},[{}]),/RESUME_NO_SHEET_WRITES/);
});

// Grain regression: real flattening + adapter; no API calls or production clients.
function grainResponse(parts) {
  return [{advertId:11,days:parts.map(nm=>({date:'2026-09-30',apps:[{appType:1,nm}]}))}];
}
function assertGrainRejected(e) {
  let rawLoads=0;
  const insert=e.c.BigQuery.Jobs.insert;
  e.c.BigQuery.Jobs.insert=(j,...args)=>{
    if(j.configuration.load && /^RAW_/.test(j.configuration.load.destinationTable.tableId)) rawLoads++;
    return insert(j,...args);
  };
  const result=e.step();
  assert.equal(result.status,'BLOCKED');
  const m=e.io().load(e.key);
  assert.equal(m.batches[0].state,'FAILED');
  assert.equal(m.batches[0].error,'RAW_GRAIN_DUPLICATE');
  assert.equal(m.continuation.reason,'RAW_GRAIN_DUPLICATE');
  assert.equal(m.continuation.needed,false);
  assert.equal(m.counts.complete,false);
  assert.equal(rawLoads,0);
  assert.equal(e.tables.RAW_WB_ADV_CAMPAIGN_STATS.length,0);
  assert.equal(e.tables.RAW_WB_ADV_BOOSTER_STATS.length,0);
  assert.equal(e.tables.INGEST_RUNS.length,1);
  assert.equal(e.tables.INGEST_RUNS[0].status,'ERROR');
  assert.equal(e.step().status,'BLOCKED');
  assert.equal(rawLoads,0);
}
for(const [name,parts] of [
  ['conflicting metric',[[{nmId:2,sum:3},{nmId:2,sum:9}]]],
  ['different nested days',[[{nmId:2,sum:3}],[{nmId:2,sum:9}]]],
  ['exact duplicate',[[{nmId:2,sum:3},{nmId:2,sum:3}]]],
  ['semantic duplicate numeric/string',[[{nmId:2,sum:3},{nmId:'2',sum:'3'}]]]
]) test('grain rejects '+name+' before either RAW destination and terminal heartbeat',()=>{
  const e=runtime(),body=grainResponse(parts);
  body[0].boosterStats=[{date:'2026-09-30',nm:2,avg_position:1}];
  e.c.UrlFetchApp.fetch=()=>({getResponseCode:()=>200,getContentText:()=>JSON.stringify(body)});
  assertGrainRejected(e);
});
test('grain unique nm/app/date keys are accepted without aggregation',()=>{
  const e=runtime(),body=grainResponse([[{nmId:2,sum:3},{nmId:3,sum:9}]]);
  body[0].days[0].apps.push({appType:2,nm:[{nmId:2,sum:4}]});
  body[0].days.push({date:'2026-09-29',apps:[{appType:1,nm:[{nmId:2,sum:5}]}]});
  e.c.UrlFetchApp.fetch=()=>({getResponseCode:()=>200,getContentText:()=>JSON.stringify(body)});
  assert.equal(e.step().status,'COMPLETE');
  assert.equal(e.tables.RAW_WB_ADV_CAMPAIGN_STATS.length,5); // Four data rows + missing campaign marker.
});
test('grain validates full durable payload on replay, including distant array boundaries',()=>{
  const e=runtime();e.kill('_landing');assert.throws(()=>e.step());e.revive();
  const row=e.tables.ADS_FULLSTATS_RECOVERY.find(r=>r.kind==='BATCH'),prepared=JSON.parse(row.payload);
  // Simulate an old prepared payload; duplicate at opposite ends, valid journal digest.
  prepared.stats.push({...prepared.stats[0],sum:'999'});
  row.payload=e.c.adsResumeCanonical_(prepared);row.digest=hash(row.payload);
  e.c.UrlFetchApp.fetch=()=>{throw Error('prepared replay must not refetch');};
  assertGrainRejected(e);
});
test('grain core rejects before invoking RAW publisher or finalizer',()=>{
  const e=core(1),stage=e.io.stage;let calls=0;
  const row={processed_status:'raw',date:'2026-09-30',advertId:'1',nmId:'2',appType:'1',source_level:'nm',sum:'3'};
  e.io.stage=(m,b)=>({...stage(m,b),stats:[row,{...row,sum:'4'}]});
  e.io.publish=e.io.finalize=()=>{calls++;throw Error('must not invoke');};
  assert.equal(e.step().state,'BLOCKED');assert.equal(calls,0);
  assert.equal(e.get().batches[0].state,'FAILED');assert.equal(e.get().counts.complete,false);
});
test('grain adapter direct-call guard precedes every RAW I/O',()=>{
  const e=runtime(),io=e.io();
  const row={processed_status:'raw',date:'2026-09-30',advertId:'11',nmId:'2',appType:'1',source_level:'nm'};
  const count=e.jobs.size;
  assert.throws(()=>io.publish({}, {}, {stats:[row,{...row}],boosters:[]}),/RAW_GRAIN_DUPLICATE/);
  assert.equal(e.jobs.size,count);assert.equal(e.tables.RAW_WB_ADV_CAMPAIGN_STATS.length,0);
});
test('grain matches effective-view STRING/NULL keys without broadening dates or source_level',()=>{
  const c=context(),row={processed_status:'raw',date:'2026-09-30',advertId:'1',nmId:'2',appType:null,source_level:'nm'};
  assert.throws(()=>c.adsResumeValidateRawGrain_({stats:[row,{...row,appType:''}]}),/RAW_GRAIN_DUPLICATE/);
  assert.doesNotThrow(()=>c.adsResumeValidateRawGrain_({stats:[row,{...row,date:'2026-09-30T00:00:00Z'},{...row,source_level:'other'}]}));
});
