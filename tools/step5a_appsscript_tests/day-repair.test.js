// Offline only: actual incident source and legacy collector/flatten in VM; every I/O is a fixture.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),crypto=require('node:crypto');
const root=path.resolve(__dirname,'../..'),hash=s=>crypto.createHash('sha256').update(s).digest('hex');
const clone=x=>JSON.parse(JSON.stringify(x));
function context(extra={}) {
  const c=vm.createContext({console:{log(){}},...extra});
  for(const f of ['WbAdsProbe.gs','WbAdsRawLoader.gs','WbAdsDayRepair.gs','WbAdsDayRepairRuntime.gs'])
    vm.runInContext(fs.readFileSync(path.join(root,'apps-script/ingestion',f),'utf8'),c,{filename:f});
  c.wbAdsNow_=()=> '2026-10-02 10:00:00';
  return c;
}
function fixture() {
  const c=context(),events=[],day='2026-09-30',rid='INS_ADS_fixture_new',origin=c.WB_ADS_DAY_REPAIR_ORIGIN_;
  const campaigns=[...c.WB_ADS_DAY_REPAIR_CONTROLS_.map((advertId,i)=>({advertId,status:i===0?'9':'11'})),
    ...Array.from({length:11},(_,i)=>({advertId:String(50000000+i),status:'11'})),
    ...Array.from({length:411},(_,i)=>({advertId:String(10000000+i),status:'7'}))]
    .map(r=>({...r,run_id:origin,load_ts:'2026-10-01 05:08:00',source_method:'promotion/count+adverts/v2',processed_status:'count_only'}));
  const evidence={parent:{run_id:c.WB_ADS_DAY_REPAIR_PARENT_,logical_period:day,loader_name:'ads',source:'apps_script',status:'ERROR',error_code:'ADS_PARTIAL',
    error_message:'raw_campaigns=OK(431) | raw_costs=OK(74)',started_at:'2026-10-01T02:07:36Z',completed_at:'2026-10-01T02:12:46Z'},campaigns,
    costMarker:{run_id:origin,source_method:'adv/v1/upd',window_index:'0',period_from:'2026-09-17',period_to:day,status:'OK',http_success:'true',returned_rows:'74',rows_out_of_window:'0',window_completed_at:'2026-10-01 05:09:10'},costCount:74,
    costCoverage:{chosen_run_id:origin,chosen_window_index:'0',requested_ok:true,not_loaded:false,answer_rows:6,answer_sum_rub:'1737',age_ok:false,stable_ok:false,billed_complete:false},
    billing:[['37563883',1,12],['37727911',1,323],['37727969',1,251],['37727999',2,688],['38085124',1,463]].map(([advertId,n,spend])=>({advertId,n,spend}))};
  const spec={logicalDate:day,campaignHash:hash(c.adsDayCanonical_(c.adsDayOrder_(campaigns).sort((a,b)=>Number(a.advertId)-Number(b.advertId)))),effectiveViewHash:hash('fixture-view')};
  const p={stats:campaigns.map(r=>({load_ts:'2026-10-02 10:00:00',raw_json:'{}',run_id:rid,period_from:day,period_to:day,source_method:'adv/v3/fullstats',processed_status:'raw',
    advertId:r.advertId,date:day,nmId:'909000001',appType:'1',source_level:'nm',sum:'1'})),boosters:[],
    classifications:Object.fromEntries(campaigns.map(r=>[r.advertId,'PROCESSED'])),failures:[],skipped:[],
    requests:[{ids:campaigns.map(r=>r.advertId),http:200,responseIds:campaigns.map(r=>r.advertId)}],present:campaigns.map(r=>r.advertId)};
  let state='STARTED',baseline={targetEmpty:true,historyHash:'unchanged'};
  const io={hash,preflight:()=>events.push('preflight'),evidence:()=>clone(evidence),baseline:()=>baseline,
    start:()=>{events.push('start');return rid;},attempt:()=>state,prepare:()=>p,
    publish:()=>{events.push('RAW');return {confirmed:true,readback:true};},validateEffective:()=>{events.push('effective');return true;},
    audit:(r,s)=>events.push(s),complete:id=>{assert.equal(id,rid);events.push('COMPLETE');state='COMPLETE';return true;},
    error:(id,code)=>{assert.equal(id,rid);events.push('ERROR:'+code);state='ERROR';return true;}};
  return {c,evidence,spec,p,io,events,rid,baseline,run:()=>c.adsDayExecute_(spec,io)};
}
function rejected(mutator,code) {const f=fixture();mutator(f);const r=f.run();assert.notEqual(r.status,'COMPLETE');
  assert.equal(r.error,code);assert.ok(!f.events.includes('RAW'));assert.ok(!f.events.includes('COMPLETE'));return r;}
test('01 normal full-population completion runs finalizer only after every predicate',()=>{
 const f=fixture(),r=f.run();assert.equal(r.status,'COMPLETE');assert.equal(r.controls.length,9);
 assert.ok(r.controls.every(x=>x.inPopulation&&x.responsePresent&&x.published&&x.readback&&x.rawRows===1));
 assert.ok(f.events.indexOf('RAW')<f.events.indexOf('effective'));assert.ok(f.events.indexOf('VALIDATED')<f.events.indexOf('COMPLETE'));
 assert.deepEqual(clone(r.billingMaturity),{age_ok:false,stable_ok:false,billed_complete:false});
});
test('02 wrong date rejected before new attempt',()=>{rejected(f=>f.spec.logicalDate='2026-10-01','REPAIR_SPEC');});
test('03 caller cannot widen window or supply campaign allowlist',()=>{rejected(f=>f.spec.from='2026-09-24','REPAIR_SPEC');});
test('04 pinned date survives current-date/month-end drift',()=>{const f=fixture();f.c.Date={now:()=>Date.parse('2027-01-01'),parse:Date.parse};assert.equal(f.run().logicalDate,'2026-09-30');});
test('05 ordering active paused completed, controls first within group',()=>{const f=fixture(),a=f.c.adsDayOrder_(f.evidence.campaigns);
 assert.equal(a[0].status,'9');assert.equal(a[1].advertId,'37669244');assert.equal(a[19].status,'11');assert.equal(a[20].status,'7');});
test('06 same 431 IDs exactly once after deterministic ordering',()=>{const f=fixture(),a=clone(f.c.adsDayOrder_(f.evidence.campaigns));
 assert.deepEqual(a.map(x=>x.advertId).sort(),f.evidence.campaigns.map(x=>x.advertId).sort());assert.equal(new Set(a.map(x=>x.advertId)).size,431);
 assert.deepEqual(a,clone(f.c.adsDayOrder_(f.evidence.campaigns.slice().reverse())));});
test('07 repeated input ID cannot be silently deduplicated',()=>{rejected(f=>f.evidence.campaigns[9]=clone(f.evidence.campaigns[0]),'CAMPAIGN_ID_SET');});
test('08 all completed campaigns remain eligible',()=>{const f=fixture();assert.equal(f.c.adsDayOrder_(f.evidence.campaigns).filter(x=>x.status==='7').length,411);});
test('09 controls done but remaining population skipped is PARTIAL, zero RAW',()=>{const r=rejected(f=>{const id=f.evidence.campaigns[430].advertId;delete f.p.classifications[id];f.p.skipped.push(id);},'ADS_PARTIAL');assert.equal(r.status,'PARTIAL');});
test('10 failed campaign prevents COMPLETE',()=>{rejected(f=>f.p.failures.push({advertId:'10000000',code:500}),'ADS_PARTIAL');});
test('11 unclassified work prevents COMPLETE even with empty failed/skipped',()=>{rejected(f=>delete f.p.classifications['10000000'],'ADS_PARTIAL');});
test('12 stat from 29 rejected before RAW',()=>{rejected(f=>f.p.stats[0].date='2026-09-29','RAW_DATE_SCOPE');});
test('13 booster from October rejected before RAW',()=>{rejected(f=>f.p.boosters=[{...f.p.stats[0],date:'2026-10-01'}],'RAW_DATE_SCOPE');});
test('14 conflicting duplicate key across payload boundaries fails before RAW',()=>{rejected(f=>f.p.stats.push({...f.p.stats[0],sum:'999'}),'RAW_GRAIN_DUPLICATE');});
test('15 exact duplicate also fails closed, no winner policy',()=>{rejected(f=>f.p.stats.push({...f.p.stats[0]}),'RAW_GRAIN_DUPLICATE');});
test('16 null grain key fails closed',()=>{rejected(f=>f.p.stats[0].nmId='','RAW_KEY_NULL');});
test('17 no_stats marker cannot carry another date',()=>{rejected(f=>f.p.stats.push({...f.p.stats[0],processed_status:'no_stats',date:'2026-09-29'}),'MARKER_SCOPE');});
test('18 unexplained control no_stats prevents COMPLETE',()=>{rejected(f=>{const r=f.p.stats[0];f.p.classifications[r.advertId]='NO_STATS';Object.assign(r,{processed_status:'no_stats',date:'',nmId:'',appType:''});},'CONTROL_ANOMALY');});
test('19 empty returned campaign is unresolved, never synthetic success',()=>{rejected(f=>f.p.stats.splice(0,1),'RESPONSE_UNRESOLVED');});
test('20 controls require request and response evidence',()=>{rejected(f=>f.p.requests=[],'CONTROL_REQUEST_EVIDENCE');});
test('21 persisted campaign hash mismatch fails closed',()=>{rejected(f=>f.spec.campaignHash='a'.repeat(64),'CAMPAIGN_HASH');});
test('22 persisted snapshot cannot be replaced by nine controls',()=>{rejected(f=>f.evidence.campaigns=f.evidence.campaigns.slice(0,9),'CAMPAIGN_COUNT');});
test('23 uncommitted costs fail before RAW',()=>{rejected(f=>f.evidence.costMarker.status='ERROR','COSTS_COMMIT');});
test('24 costs row-count mismatch fails before RAW',()=>{rejected(f=>f.evidence.costCount=73,'COSTS_COMMIT');});
test('25 canonical billing origin mismatch fails',()=>{rejected(f=>f.evidence.costCoverage.chosen_run_id='other','COSTS_COVERAGE');});
test('26 billing spend is validated, never substituted into fullstats',()=>{rejected(f=>f.evidence.billing[0].spend=13,'COSTS_DAY_READBACK');});
test('27 old heartbeat untouched on success and failure',()=>{const f=fixture(),before=clone(f.evidence.parent);f.run();assert.deepEqual(f.evidence.parent,before);
 const g=fixture();g.p.failures.push({});g.run();assert.deepEqual(g.evidence.parent,before);});
test('28 existing target RAW/effective data prevents a blind retry',()=>{rejected(f=>f.baseline.targetEmpty=false,'TARGET_ALREADY_PRESENT');});
test('29 sink/schema/Sheet preflight mismatch blocks before any attempt',()=>{for(const code of ['RAW_SCHEMA','STATUS_SCHEMA','RUNTIME_CONTEXT','JOURNAL_SCHEMA'])
 rejected(f=>f.io.preflight=()=>f.c.adsDayAssert_(false,code),code);});
test('30 BQ receipt uncertainty cannot COMPLETE',()=>{const f=fixture();f.io.publish=()=>({confirmed:false,readback:true});assert.equal(f.run().error,'RAW_READBACK');assert.ok(!f.events.includes('COMPLETE'));});
test('31 RAW readback mismatch cannot COMPLETE',()=>{const f=fixture();f.io.publish=()=>({confirmed:true,readback:false});assert.equal(f.run().error,'RAW_READBACK');});
test('32 effective target/history mismatch cannot COMPLETE',()=>{const f=fixture();f.io.validateEffective=()=>false;assert.equal(f.run().error,'EFFECTIVE_READBACK');assert.ok(!f.events.includes('COMPLETE'));});
test('33 reused evidence changing during publication prevents COMPLETE',()=>{const f=fixture();f.io.publish=()=>{f.evidence.billing[0].spend=13;return {confirmed:true,readback:true};};assert.equal(f.run().error,'REUSED_EVIDENCE_CHANGED');});
test('34 failed status evidence prevents RAW or COMPLETE',()=>{rejected(f=>f.io.audit=()=>{throw Error('offline fixture failure');},'REPAIR_IO_UNKNOWN');});
test('35 finalizer returning false is not success',()=>{const f=fixture();f.io.complete=()=>false;assert.equal(f.run().error,'COMPLETE_UNCONFIRMED');});
test('36 no new ID/no STARTED journal means zero RAW',()=>{rejected(f=>f.io.start=()=>null,'NEW_ATTEMPT');});
test('37 old attempt cannot become repair ID',()=>{rejected(f=>f.io.start=()=>f.c.WB_ADS_DAY_REPAIR_PARENT_,'NEW_ATTEMPT');});
test('38 disabled entrypoint performs no I/O',()=>{const c=context({PropertiesService:{getScriptProperties:()=>({getProperty:()=>null})}});assert.equal(c.runWbAdsRepair20260930().status,'DISABLED');});
test('39 lock contention does not construct adapter or call WB',()=>{const c=context({PropertiesService:{getScriptProperties:()=>({getProperty:k=>k==='WB_ADS_REPAIR_20260930_ENABLED'?'1':null})},LockService:{getScriptLock:()=>({tryLock:()=>false})}});
 assert.equal(c.runWbAdsRepair20260930().status,'SKIPPED_LOCKED');});
// Use the REAL legacy collector + flatten: duplicate nested portions and cross-50-ID collection are whole-payload checked.
test('40 duplicate key in different nested response portions fails before publication',()=>{
 const f=fixture(),camp={advertId:37563883,days:[{date:'2026-09-30',apps:[{appType:1,nm:[{nmId:1,sum:1},{nmId:1,sum:2}]}]}]};
 const flat=f.c.wbAdvFlattenFullstats_([camp],f.rid,'2026-09-30','2026-09-30');
 f.p.stats=f.p.stats.filter(r=>r.advertId!=='37563883').concat(flat.statRows);assert.equal(f.run().error,'RAW_GRAIN_DUPLICATE');assert.ok(!f.events.includes('RAW'));
});
test('41 non-control omitted campaign has one observational no_stats marker',()=>{const f=fixture(),id='10000000';f.p.stats=f.p.stats.filter(r=>r.advertId!==id);
 f.p.stats.push(f.c.wbAdvCampaignStatNoStatsRow_(f.rid,id,'2026-09-30','2026-09-30'));f.p.classifications[id]='NO_STATS';assert.equal(f.run().status,'COMPLETE');});
test('42 collector retains full population and 50-ID batching without catalog/cost/optional calls',()=>{
 let now=0,calls=[],statusCalls=0;const c=context({Date:{now:()=>now},Utilities:{sleep:ms=>{now+=ms;}},wbAdsNow_:()=> 'fixture'});
 c.wbAdsRawWriteStatus_=()=>statusCalls++;
 const ids=Array.from({length:431},(_,i)=>i+1);
 const ctx=c.wbAdsFullstatsCollect_('fixture-not-a-token',ids,'2026-09-30','2026-09-30','new',240000,
 {fetch:(batch,url)=>{calls.push({ids:clone(batch),url});return {ok:true,code:200,json:[]};}});
 assert.equal(calls.length,9);assert.equal(Object.keys(ctx.noStats).length,431);assert.equal(ctx.skipped.length,0);assert.equal(statusCalls,0);
 assert.ok(calls.every(x=>x.ids.length<=50&&x.url.endsWith('beginDate=2026-09-30&endDate=2026-09-30')));
 assert.deepEqual(calls.flatMap(x=>x.ids),ids);
});
test('43 legacy collector error split uses incident evidence and no implicit Sheet writes',()=>{
 let now=0;const c=context({Date:{now:()=>now},Utilities:{sleep:ms=>{now+=ms;}}});c.wbAdsRawWriteStatus_=()=>{throw Error('forbidden status fallback');};
 const ctx=c.wbAdsFullstatsCollect_('fixture',[1,2],'2026-09-30','2026-09-30','new',240000,{fetch:()=>({ok:false,code:500})});
 assert.equal(ctx.failures.length,2);assert.equal(ctx.collected.length,0);
});
function runtimeFixture() {
 const seed=fixture(),calls=[],jobs=new Map(),tables={RAW_WB_ADV_CAMPAIGN_STATS:[],RAW_WB_ADV_BOOSTER_STATS:[]};let now=0,uuid=0,state=null;
 const oldParent=clone(seed.evidence.parent),status=[[]],props={WB_ADS_REPAIR_20260930_ENABLED:'1'};
 const c=context({Date:class extends Date {static now(){return now;}},
   PropertiesService:{getScriptProperties:()=>({getProperty:k=>props[k]||null})},
   LockService:{getScriptLock:()=>({tryLock:()=>true,releaseLock:()=>calls.push('unlock')})},
   Utilities:{sleep:ms=>{now+=ms;},getUuid:()=>String(++uuid).padStart(32,'0'),DigestAlgorithm:{SHA_256:'sha256'},Charset:{UTF_8:'utf8'},
    computeDigest:(a,s)=>Array.from(crypto.createHash('sha256').update(s).digest()),
    newBlob:s=>({text:s,getBytes:()=>Buffer.from(s)}),formatDate:()=> '2026-10-02 10:00:00'},
   getBqConfig_:()=>({projectId:'project-fa311fc0-4d87-4781-986',datasetId:'wb_raw',location:'EU'}),wbAdsBqSinkOn_:()=>true,
   getWbAdsToken_:()=>({token:'fixture-not-a-credential'}),
   ingestParam_:(name,type,value)=>({name,parameterType:{type},parameterValue:{value:String(value)}}),
   ingestRunStart_:(loader,day,trigger)=>{assert.equal(loader,'ads');assert.equal(day,'2026-09-30');assert.equal(trigger,'MANUAL');state='STARTED';calls.push('journal:start');return seed.rid;},
   ingestRunComplete_:(rid,n,m)=>{assert.equal(rid,seed.rid);assert.equal(n,m);state='COMPLETE';calls.push('journal:complete');return true;},
   ingestRunError_:(rid,code)=>{assert.equal(rid,seed.rid);state='ERROR';calls.push('journal:error:'+code);return true;}
 });
 c.getWbAdsToken_=()=>({token:'fixture-not-a-credential'});
 const headers={RAW_WB_ADV_CAMPAIGN_STATS:clone(c.WB_ADV_RAW_CAMPAIGN_STATS_HEADERS_),RAW_WB_ADV_BOOSTER_STATS:clone(c.WB_ADV_RAW_BOOSTER_STATS_HEADERS_)};
 status[0]=clone(c.WB_ADS_STATUS_HEADERS_);
 const sheet={getLastColumn:()=>status[0].length,getLastRow:()=>status.length,
   getRange:(r,col,n,width)=>({getValues:()=>status.slice(r-1,r-1+n).map(x=>x.slice(col-1,col-1+width)),
     setValues:rs=>{assert.ok(r>1,'no header/schema writes');for(let i=0;i<rs.length;i++)status[r-1+i]=clone(rs[i]);calls.push('status');}})};
 c.SpreadsheetApp={getActiveSpreadsheet:()=>({getSheetByName:name=>name==='WB_ADS_STATUS'?sheet:null})};
 const schemas=Object.fromEntries(Object.entries(headers).map(([t,h])=>[t,{type:'TABLE',schema:{fields:h.map(name=>({name,type:'STRING',mode:'NULLABLE'}))}}]));
 schemas.V_ADV_CAMPAIGN_STATS={type:'VIEW',view:{query:'fixture-view',useLegacySql:false}};
 schemas.INGEST_RUNS={type:'TABLE',schema:{fields:[...['run_id','loader_name','status','source','trigger_type','error_code','error_message'].map(name=>({name,type:'STRING'})),
   {name:'logical_period',type:'DATE'},...['started_at','completed_at'].map(name=>({name,type:'TIMESTAMP'})),...['rows_fetched','rows_loaded'].map(name=>({name,type:'INTEGER'}))]}};
 const history={...clone(seed.p.stats[0]),date:'2026-09-29',run_id:'historic'},baselineHistory=clone(history);
 function select(sql,values) {
   const p=Object.fromEntries(values.map(x=>[x.name,x.parameterValue.value]));
   if(sql.includes('V_ADV_CAMPAIGN_STATS'))return c.adsDayRows_([history,...tables.RAW_WB_ADV_CAMPAIGN_STATS.filter(x=>x.processed_status==='raw')],headers.RAW_WB_ADV_CAMPAIGN_STATS);
   if(sql.includes('RAW_WB_ADV_CAMPAIGNS`'))return clone(seed.evidence.campaigns);
   if(sql.includes('RAW_WB_ADV_COSTS_RUNS`'))return [clone(seed.evidence.costMarker)];
   if(sql.includes('RAW_WB_ADV_COSTS`'))return [{n:74}];
   if(sql.includes('V_ADV_COSTS_DAY_COVERAGE'))return [{...clone(seed.evidence.costCoverage),chosen_window_index:0}];
   if(sql.includes('V_ADV_COSTS`'))return clone(seed.evidence.billing);
   if(sql.includes('INGEST_RUNS')) {
     if(p.parent)return [oldParent];if(p.rid)return [{status:state,logical_period:'2026-09-30',loader_name:'ads',source:'apps_script'}];return [{n:0}];
   }
   const table=Object.keys(tables).find(t=>sql.includes('`'+t+'`')||sql.includes('.'+t+'`'));
   assert.ok(table,'unsupported offline query: '+sql);
   if(sql.includes('COUNT(*)'))return [{n:tables[table].filter(x=>(x.date||'').slice(0,10)==='2026-09-30').length}];
   return tables[table].filter(x=>x.run_id===p.run);
 }
 c.BigQuery={Datasets:{get:()=>({location:'EU'})},Tables:{get:(project,ds,t)=>{assert.equal(ds,'wb_raw');return clone(schemas[t]);}},Jobs:{
   insert:(job,project,blob)=>{
     assert.equal(project,'project-fa311fc0-4d87-4781-986');assert.equal(job.jobReference.location,'EU');
     const id=job.jobReference.jobId,j={...job,status:{state:'DONE'},statistics:{}};
     if(job.configuration.query){const q=job.configuration.query;assert.equal(q.useLegacySql,false);assert.ok(q.query.startsWith('SELECT '));
       assert.equal(q.maximumBytesBilled,'100000000');j.result=clone(select(q.query,q.queryParameters));calls.push('SELECT');}
     else {const l=job.configuration.load;assert.equal(l.createDisposition,'CREATE_NEVER');assert.equal(l.writeDisposition,'WRITE_APPEND');assert.equal(l.ignoreUnknownValues,false);
       assert.ok(Object.hasOwn(tables,l.destinationTable.tableId));const data=blob.text.split('\n').map(JSON.parse);tables[l.destinationTable.tableId].push(...data);
       j.statistics.load={outputRows:String(data.length)};calls.push('RAW:'+l.destinationTable.tableId);}
     jobs.set(id,j);return j;
   },get:(project,id)=>jobs.get(id),getQueryResults:(project,id)=>{const r=jobs.get(id).result;return {jobComplete:true,totalRows:String(r.length),rows:r.map(x=>({f:[{v:JSON.stringify(x)}]}))};}
 }};
 c.UrlFetchApp={fetch:(url,options)=>{
   assert.ok(url.includes('/adv/v3/fullstats?ids='));assert.ok(url.endsWith('&beginDate=2026-09-30&endDate=2026-09-30'));assert.equal(options.method,'get');
   calls.push('WB');const ids=url.match(/ids=([^&]+)/)[1].split(',');const response=ids.map(advertId=>({advertId:Number(advertId),days:[{date:'2026-09-30',apps:[{appType:1,nm:[{nmId:909000001,sum:1}]}]}],
     boosterStats:[{date:'2026-09-30',nm:909000001,avg_position:5}]}));
   return {getResponseCode:()=>200,getAllHeaders:()=>({}),getContentText:()=>JSON.stringify(response)};
 }};
 for(const fn of ['loadWbAdsCampaignsRaw','loadWbAdsCostsRaw','loadWbAdsFullstatsRaw','WbAdsFullstatsMonth','wbAdsFetchCampaigns_',
   'wbAdvRawEnsureSheet_','wbAdvBqEnsureTable_','wbAdsDailyRun','ensureWbAdsStatusSheet_','loadWbAdsSearchClustersRaw','wbAdsLoadQueryStatsDaily_','wbAdsLoadQueryBidsDaily_'])
   c[fn]=()=>{throw Error('forbidden stage '+fn);};
 return {c,seed,calls,tables,jobs,schemas,status,props,history,baselineHistory,oldParent,run:()=>c.runWbAdsRepair20260930(seed.spec)};
}
test('44 real narrow adapter completes 431 IDs in 9 calls with only stat/booster/status/new journal writes',()=>{
 const f=runtimeFixture(),r=f.run();assert.equal(r.status,'COMPLETE',JSON.stringify(r));assert.equal(f.calls.filter(x=>x==='WB').length,9);
 assert.equal(f.tables.RAW_WB_ADV_CAMPAIGN_STATS.length,431);assert.equal(f.tables.RAW_WB_ADV_BOOSTER_STATS.length,431);
 assert.ok(Object.values(f.tables).flat().every(x=>(x.date||'').slice(0,10)==='2026-09-30'&&x.period_from===x.period_to&&x.run_id===f.seed.rid));
 assert.deepEqual(f.history,f.baselineHistory);assert.deepEqual(f.oldParent,f.seed.evidence.parent);
 assert.equal(f.status.length,21);assert.equal(f.calls.filter(x=>x==='journal:complete').length,1);
 assert.ok(r.controls.every(x=>x.requests.length===1&&x.nmIds[0]==='909000001'&&x.appTypes[0]==='1'&&x.readback));
});
test('45 real adapter schema expansion surprise fails without WB/RAW/journal mutation',()=>{
 const f=runtimeFixture();f.schemas.RAW_WB_ADV_CAMPAIGN_STATS.schema.fields.push({name:'surprise',type:'STRING'});const r=f.run();assert.equal(r.error,'RAW_SCHEMA');
 assert.ok(!f.calls.includes('WB'));assert.ok(!f.calls.includes('journal:start'));assert.equal(f.tables.RAW_WB_ADV_CAMPAIGN_STATS.length,0);
});
test('46 real adapter Sheet/header surprise cannot fallback or create a sheet',()=>{const f=runtimeFixture();f.status[0].push('surprise');assert.equal(f.run().error,'STATUS_SCHEMA');assert.ok(!f.calls.includes('WB'));});
test('47 real HTTP returned prior-day stats fail before RAW and expose reason in journal/status',()=>{
 const f=runtimeFixture(),fetch=f.c.UrlFetchApp.fetch;f.c.UrlFetchApp.fetch=(url,o)=>{const r=fetch(url,o),rows=JSON.parse(r.getContentText());rows[0].days[0].date='2026-09-29';return {...r,getContentText:()=>JSON.stringify(rows)};};
 const r=f.run();assert.notEqual(r.status,'COMPLETE');assert.ok(r.failures.some(x=>x.code==='HTTP_DATE_SCOPE'));
 assert.equal(f.tables.RAW_WB_ADV_CAMPAIGN_STATS.length,0);assert.ok(f.status.flat().some(x=>String(x).includes('HTTP_DATE_SCOPE')));
});
test('48 real RAW readback corruption keeps journal ERROR',()=>{
 const f=runtimeFixture(),get=f.c.BigQuery.Jobs.getQueryResults;f.c.BigQuery.Jobs.getQueryResults=(project,id)=>{
   const j=f.jobs.get(id);if(j.configuration.query.query.includes('RAW_WB_ADV_CAMPAIGN_STATS')&&j.configuration.query.query.includes('run_id=@run')){
    const r=get(project,id),row=JSON.parse(r.rows[0].f[0].v);row.sum='999';r.rows[0].f[0].v=JSON.stringify(row);return r;}return get(project,id);
 };assert.equal(f.run().error,'RAW_READBACK');assert.ok(!f.calls.includes('journal:complete'));
});
test('49 real history change after RAW blocks finalization',()=>{
 const f=runtimeFixture(),insert=f.c.BigQuery.Jobs.insert;f.c.BigQuery.Jobs.insert=(j,p,b)=>{const r=insert(j,p,b);if(j.configuration.load)f.history.sum='999';return r;};
 assert.equal(f.run().error,'EFFECTIVE_READBACK');assert.ok(!f.calls.includes('journal:complete'));
});
test('50 real booster outside date fails before stat publication',()=>{
 const f=runtimeFixture(),fetch=f.c.UrlFetchApp.fetch;f.c.UrlFetchApp.fetch=(url,o)=>{const r=fetch(url,o),rows=JSON.parse(r.getContentText());rows[0].boosterStats[0].date='2026-10-01';return {...r,getContentText:()=>JSON.stringify(rows)};};
 const r=f.run();assert.notEqual(r.status,'COMPLETE');assert.equal(f.tables.RAW_WB_ADV_CAMPAIGN_STATS.length,0);assert.ok(r.failures.some(x=>x.code==='BOOSTER_DATE_SCOPE'));
});
test('51 real nested duplicate metrics fail before both RAW destinations',()=>{
 const f=runtimeFixture(),fetch=f.c.UrlFetchApp.fetch;f.c.UrlFetchApp.fetch=(url,o)=>{const r=fetch(url,o),rows=JSON.parse(r.getContentText());const app=rows[0].days[0].apps[0];app.nm.push({...app.nm[0],sum:99});return {...r,getContentText:()=>JSON.stringify(rows)};};
 assert.equal(f.run().error,'RAW_GRAIN_DUPLICATE');assert.equal(f.tables.RAW_WB_ADV_CAMPAIGN_STATS.length,0);assert.equal(f.tables.RAW_WB_ADV_BOOSTER_STATS.length,0);
});
test('52 real load job failure cannot produce COMPLETE',()=>{
 const f=runtimeFixture(),insert=f.c.BigQuery.Jobs.insert;f.c.BigQuery.Jobs.insert=(j,p,b)=>{const r=insert(j,p,b);if(j.configuration.load)r.status.errorResult={reason:'fixture'};return r;};
 assert.equal(f.run().error,'BQ_JOB_FAILED');assert.ok(!f.calls.includes('journal:complete'));
});
test('53 real non-2xx cannot silently become no_stats',()=>{const f=runtimeFixture();f.c.UrlFetchApp.fetch=()=>({getResponseCode:()=>403,getAllHeaders:()=>({})});
 const r=f.run();assert.equal(r.status,'PARTIAL');assert.equal(f.tables.RAW_WB_ADV_CAMPAIGN_STATS.length,0);assert.ok(!f.calls.includes('journal:complete'));
});
test('54 real source deadline after controls does not publish partial payload',()=>{
 const f=runtimeFixture(),fetch=f.c.UrlFetchApp.fetch;let count=0;f.c.UrlFetchApp.fetch=(url,o)=>{const r=fetch(url,o);if(++count===1)f.c.Utilities.sleep(250000);return r;};
 const r=f.run();assert.equal(r.status,'PARTIAL');assert.equal(f.tables.RAW_WB_ADV_CAMPAIGN_STATS.length,0);assert.ok(!f.calls.includes('journal:complete'));
});
test('55 real view hash mismatch stops before WB/journal',()=>{const f=runtimeFixture();f.schemas.V_ADV_CAMPAIGN_STATS.view.query='different';assert.equal(f.run().error,'EFFECTIVE_VIEW_HASH');assert.ok(!f.calls.includes('WB'));});
test('56 real truncated SELECT results are UNPROVEN, never interpreted as complete evidence',()=>{
 const f=runtimeFixture(),get=f.c.BigQuery.Jobs.getQueryResults;f.c.BigQuery.Jobs.getQueryResults=(p,id)=>({...get(p,id),pageToken:'next'});
 assert.equal(f.run().error,'BQ_QUERY_INCOMPLETE');assert.ok(!f.calls.includes('WB'));
});
test('57 runtime config drift before finalization cannot send journal writes to another project',()=>{
 const f=runtimeFixture(),insert=f.c.BigQuery.Jobs.insert;f.c.BigQuery.Jobs.insert=(j,p,b)=>{const r=insert(j,p,b);if(j.configuration.load)
   f.c.getBqConfig_=()=>({projectId:'wrong-project',datasetId:'wb_raw',location:'EU'});return r;};
 const r=f.run();assert.equal(r.error,'RUNTIME_CONTEXT_CHANGED');assert.equal(r.errorRecorded,false);assert.ok(!f.calls.includes('journal:complete'));assert.ok(!f.calls.some(x=>x.startsWith('journal:error:')));
});
test('58 partial after the first batch keeps successful control evidence inspectable',()=>{
 const f=runtimeFixture(),fetch=f.c.UrlFetchApp.fetch;let count=0;f.c.UrlFetchApp.fetch=(url,o)=>{const r=fetch(url,o);if(++count===2)f.c.Utilities.sleep(250000);return r;};
 const r=f.run();assert.equal(r.status,'PARTIAL');assert.ok(r.controls.every(x=>x.responsePresent&&x.rawRows>0&&!x.published));assert.equal(f.tables.RAW_WB_ADV_CAMPAIGN_STATS.length,0);
});

test('59 campaign source provenance mismatch cannot be reused',()=>{rejected(f=>f.evidence.campaigns[0].source_method='unexpected','CAMPAIGN_TIME_PROVENANCE');});
test('60 costs source provenance mismatch cannot be reused',()=>{rejected(f=>f.evidence.costMarker.source_method='unexpected','COSTS_COMMIT');});

test('61 uncertain RAW publication reports UNKNOWN, never falsely claims no write',()=>{const f=fixture();f.io.publish=()=>{throw Error('uncertain submission');};const r=f.run();assert.notEqual(r.status,'COMPLETE');assert.ok(r.controls.every(x=>x.publicationStatus==='UNKNOWN'&&x.published===null&&x.readback===null));});
test('62 confirmed RAW stays inspectable when effective validation fails',()=>{const f=fixture();f.io.validateEffective=()=>false;const r=f.run();assert.equal(r.error,'EFFECTIVE_READBACK');assert.ok(r.controls.every(x=>x.publicationStatus==='RAW_VERIFIED'&&x.published&&x.readback));assert.ok(!f.events.includes('COMPLETE'));});
// Booster-grain regression: exact V_ADV_BOOSTER_STATS key, separate from pinned-date guard.
function boosterFixtureRow(f,extra={}) {
 return {load_ts:'2026-10-02 10:00:00',raw_json:'{}',run_id:f.rid,period_from:'2026-09-30',period_to:'2026-09-30',source_method:'adv/v3/fullstats',
   processed_status:'raw',date:'2026-09-30',advertId:'37563883',nmId:'909000001',avg_position:5,...extra};
}
function assertBoosterRejected(f) {
 const audit=[],original=f.io.audit;f.io.audit=(r,s)=>{audit.push({status:s,error:r.error});original(r,s);};
 const r=f.run();assert.equal(r.status,'ERROR');assert.equal(r.error,'BOOSTER_GRAIN_DUPLICATE');assert.equal(r.errorRecorded,true);
 assert.ok(!f.events.includes('RAW'));assert.ok(!f.events.includes('COMPLETE'));assert.ok(audit.some(x=>x.status==='ERROR'&&x.error==='BOOSTER_GRAIN_DUPLICATE'));
 return r;
}
test('63 unique booster keys accepted without modifying the prepared payload',()=>{
 const f=fixture(),rows=[boosterFixtureRow(f),boosterFixtureRow(f,{nmId:'909000002'})],before=clone(rows);
 f.c.adsDayValidateBoosterGrain_(rows);assert.deepEqual(rows,before);f.p.boosters=rows;assert.equal(f.run().status,'COMPLETE');
});
test('64 identical duplicate booster row rejected before RAW/COMPLETE',()=>{
 const f=fixture(),b=boosterFixtureRow(f);f.p.boosters=[b,{...b}];assertBoosterRejected(f);
});
test('65 exact review counterexample 30.09/37563883/909000001 positions 5 and 99 fails atomically',()=>{
 const f=fixture();f.p.boosters=[boosterFixtureRow(f),boosterFixtureRow(f,{avg_position:99})];assertBoosterRejected(f);
});
test('66 differences outside avg_position cannot make duplicate booster keys unique',()=>{
 const f=fixture();f.p.boosters=[boosterFixtureRow(f,{raw_json:'{"position":5}'}),boosterFixtureRow(f,{raw_json:'{"position":99}',load_ts:'later'})];assertBoosterRejected(f);
});
test('67 duplicate from separate nested/source portions rejected after complete flatten',()=>{
 const f=fixture(),camp={advertId:37563883,days:[],boosterStats:[{date:'2026-09-30',nm:909000001,avg_position:5}]};
 const a=f.c.wbAdvFlattenFullstats_([camp],f.rid,'2026-09-30','2026-09-30');
 const b=f.c.wbAdvFlattenFullstats_([{...camp,boosterStats:[{date:'2026-09-30',nmId:909000001,avgPosition:99}]}],f.rid,'2026-09-30','2026-09-30');
 f.p.boosters=a.boosterRows.concat(b.boosterRows);assertBoosterRejected(f);
});
test('68 same nmId under a different advertId is a unique booster business key',()=>{
 const f=fixture();f.p.boosters=[boosterFixtureRow(f),boosterFixtureRow(f,{advertId:'37727911'})];assert.equal(f.run().status,'COMPLETE');
});
test('69 generic booster grain distinguishes dates while incident date guard rejects 29.09',()=>{
 const f=fixture(),rows=[boosterFixtureRow(f),boosterFixtureRow(f,{date:'2026-09-29'})];f.c.adsDayValidateBoosterGrain_(rows);
 f.p.boosters=rows;const r=f.run();assert.equal(r.error,'RAW_DATE_SCOPE');assert.ok(!f.events.includes('RAW'));assert.ok(!f.events.includes('COMPLETE'));
});
test('70 valid stats plus invalid boosters invokes neither real runtime RAW writer',()=>{
 const f=runtimeFixture(),fetch=f.c.UrlFetchApp.fetch;let injected=false;
 f.c.UrlFetchApp.fetch=(url,options)=>{
  const response=fetch(url,options),camps=JSON.parse(response.getContentText());
  if(!injected){assert.equal(camps[0].advertId,37563883);camps[0].boosterStats.push({...camps[0].boosterStats[0],avg_position:99});injected=true;}
  return {...response,getContentText:()=>JSON.stringify(camps)};
 };
 const r=f.run();assert.equal(r.status,'ERROR');assert.equal(r.error,'BOOSTER_GRAIN_DUPLICATE');assert.equal(r.counts.population,431);assert.equal(r.counts.statRows,431);assert.equal(r.counts.boosterRows,432);
 assert.equal(f.calls.filter(x=>x.startsWith('RAW:')).length,0);assert.equal(f.tables.RAW_WB_ADV_CAMPAIGN_STATS.length,0);assert.equal(f.tables.RAW_WB_ADV_BOOSTER_STATS.length,0);
 assert.ok(!f.calls.includes('journal:complete'));assert.ok(f.calls.includes('journal:error:ADS_REPAIR_BOOSTER_GRAIN_DUPLICATE'));
 assert.ok(f.status.flat().some(x=>String(x).includes('BOOSTER_GRAIN_DUPLICATE')));assert.deepEqual(f.oldParent,f.seed.evidence.parent);
});
test('71 identical booster duplicate in source response also blocks both runtime writers',()=>{
 const f=runtimeFixture(),fetch=f.c.UrlFetchApp.fetch;
 f.c.UrlFetchApp.fetch=(url,o)=>{const response=fetch(url,o),camps=JSON.parse(response.getContentText());camps[0].boosterStats.push({...camps[0].boosterStats[0]});return {...response,getContentText:()=>JSON.stringify(camps)};};
 assert.equal(f.run().error,'BOOSTER_GRAIN_DUPLICATE');assert.ok(!f.calls.some(x=>x.startsWith('RAW:')));assert.ok(!f.calls.includes('journal:complete'));
});
test('72 invalid stat with valid boosters remains atomic pre-publication failure',()=>{
 const f=fixture();f.p.boosters=[boosterFixtureRow(f)];f.p.stats.push({...f.p.stats[0],sum:99});
 const r=f.run();assert.equal(r.error,'RAW_GRAIN_DUPLICATE');assert.ok(!f.events.includes('RAW'));assert.ok(!f.events.includes('COMPLETE'));
});
test('73 generic booster key uses existing STRING/NULL storage representation',()=>{
 const f=fixture(),b=boosterFixtureRow(f);assert.throws(()=>f.c.adsDayValidateBoosterGrain_([b,{...b,advertId:Number(b.advertId),nmId:Number(b.nmId)}]),/BOOSTER_GRAIN_DUPLICATE/);
 assert.throws(()=>f.c.adsDayValidateBoosterGrain_([{...b,nmId:null},{...b,nmId:''}]),/BOOSTER_GRAIN_DUPLICATE/);
});
test('74 malformed booster payload is rejected by the generic validator',()=>{
 assert.throws(()=>fixture().c.adsDayValidateBoosterGrain_(null),/BOOSTER_GRAIN_PAYLOAD/);
});

// Source-shape regression: real incident adapter/collector/flatten, fixture services only.
function boosterSourceFixture(mutate,request=1) {
 const f=runtimeFixture(),fetch=f.c.UrlFetchApp.fetch;let calls=0;
 f.c.UrlFetchApp.fetch=(url,options)=>{
  const response=fetch(url,options),camps=JSON.parse(response.getContentText());
  if(++calls===request) mutate(camps[0]);
  return {...response,getContentText:()=>JSON.stringify(camps)};
 };
 return f;
}
function assertBoosterSourceRejected(f,code) {
 const r=f.run();assert.notEqual(r.status,'COMPLETE');assert.equal(r.error,'ADS_PARTIAL');
 assert.ok(r.failures.some(x=>x.code===code),'inspectable structural error');
 assert.equal(f.calls.filter(x=>x==='RAW:RAW_WB_ADV_CAMPAIGN_STATS').length,0);
 assert.equal(f.calls.filter(x=>x==='RAW:RAW_WB_ADV_BOOSTER_STATS').length,0);
 assert.equal(f.tables.RAW_WB_ADV_CAMPAIGN_STATS.length,0);assert.equal(f.tables.RAW_WB_ADV_BOOSTER_STATS.length,0);
 assert.ok(!f.calls.includes('journal:complete'));assert.ok(f.calls.includes('journal:error:ADS_REPAIR_ADS_PARTIAL'));
 assert.ok(f.status.flat().some(x=>String(x).includes(code)));assert.deepEqual(f.oldParent,f.seed.evidence.parent);
 return r;
}
for(const alias of ['boosterStats','booster_stats']) {
 for(const [name,value] of [['false',false],['object',{}],['string','invalid'],['number',5],['zero',0],['empty string',''],['null',null]]) {
  test('source shape: present '+alias+' '+name+' blocks both RAW writers and COMPLETE',()=>{
   const f=boosterSourceFixture(camp=>{delete camp.boosterStats;camp[alias]=value;});
   assertBoosterSourceRejected(f,'HTTP_BOOSTER_SCHEMA');
  });
 }
 test('source shape: valid empty '+alias+' remains accepted',()=>{
  const f=boosterSourceFixture(camp=>{delete camp.boosterStats;camp[alias]=[];});
  const r=f.run();assert.equal(r.status,'COMPLETE');assert.equal(r.counts.statRows,431);assert.equal(r.counts.boosterRows,430);
  assert.equal(f.calls.filter(x=>x.startsWith('RAW:')).length,2);assert.deepEqual(f.oldParent,f.seed.evidence.parent);
 });
 test('source shape: valid '+alias+' array preserves its source and normal incident behavior',()=>{
  let source,before;const f=boosterSourceFixture(camp=>{source=camp.boosterStats;before=clone(source);delete camp.boosterStats;camp[alias]=source;});
  const r=f.run();assert.equal(r.status,'COMPLETE');assert.equal(r.counts.statRows,431);assert.equal(r.counts.boosterRows,431);
  assert.deepEqual(source,before);assert.equal(f.calls.filter(x=>x==='WB').length,9);assert.deepEqual(f.oldParent,f.seed.evidence.parent);
 });
}
test('source shape: both booster aliases absent follows existing optional-booster contract',()=>{
 const f=boosterSourceFixture(camp=>{delete camp.boosterStats;delete camp.booster_stats;});
 const r=f.run();assert.equal(r.status,'COMPLETE');assert.equal(r.counts.statRows,431);assert.equal(r.counts.boosterRows,430);
});
for(const [name,camel,snake] of [['two empty arrays',[],[]],['two identical arrays',[{date:'2026-09-30',nm:909000001,avg_position:5}],[{date:'2026-09-30',nm:909000001,avg_position:5}]],
 ['malformed camel and valid snake',false,[]],['valid camel and malformed snake',[],false]]) {
 test('source shape: both aliases present ('+name+') deterministically fail closed',()=>{
  assertBoosterSourceRejected(boosterSourceFixture(camp=>{camp.boosterStats=camel;camp.booster_stats=snake;}),'HTTP_BOOSTER_ALIASES');
 });
}
test('source shape: present undefined is invalid and absence is explicitly distinguished',()=>{
 const c=fixture().c;assert.throws(()=>c.adsDayBoosterSource_({boosterStats:undefined}),/HTTP_BOOSTER_SCHEMA/);
 assert.throws(()=>c.adsDayBoosterSource_({booster_stats:undefined}),/HTTP_BOOSTER_SCHEMA/);
 assert.deepEqual(clone(c.adsDayBoosterSource_({})),[]);
});
test('source shape: exact independent-review false-booster counterexample cannot publish or COMPLETE',()=>{
 const f=boosterSourceFixture(camp=>{assert.equal(camp.advertId,37563883);assert.equal(camp.days[0].apps[0].nm[0].nmId,909000001);camp.boosterStats=false;});
 assertBoosterSourceRejected(f,'HTTP_BOOSTER_SCHEMA');
});
test('source shape: malformed booster in final batch blocks all earlier prepared stats and boosters',()=>{
 const f=boosterSourceFixture(camp=>{camp.boosterStats=false;},9),r=assertBoosterSourceRejected(f,'HTTP_BOOSTER_SCHEMA');
 assert.equal(r.counts.statRows,400);assert.equal(r.counts.boosterRows,400);assert.equal(f.calls.filter(x=>x==='WB').length,9);
});

// Full consumed-source hierarchy: real incident adapter, all services remain fixtures.
function fullstatsStructureFixture(transform,request=1) {
 const f=runtimeFixture(),fetch=f.c.UrlFetchApp.fetch;let calls=0;
 f.c.UrlFetchApp.fetch=(url,options)=>{
  const response=fetch(url,options),original=JSON.parse(response.getContentText());
  const body=++calls===request?transform(original):original;
  return {...response,getContentText:()=>JSON.stringify(body)};
 };
 return f;
}
const structuralArrayCases=[
 ['days','HTTP_DAYS_SCHEMA',(c,v)=>{c.days=v;}],
 ['apps','HTTP_APPS_SCHEMA',(c,v)=>{c.days[0].apps=v;}],
 ['nm','HTTP_NM_SCHEMA',(c,v)=>{c.days[0].apps[0].nm=v;}],
 ['nms','HTTP_NM_SCHEMA',(c,v)=>{const a=c.days[0].apps[0];delete a.nm;a.nms=v;}],
 ['boosterStats','HTTP_BOOSTER_SCHEMA',(c,v)=>{c.boosterStats=v;}],
 ['booster_stats','HTTP_BOOSTER_SCHEMA',(c,v)=>{delete c.boosterStats;c.booster_stats=v;}]
];
const invalidArrays=[['false',false],['null',null],['zero',0],['number',5],['string','invalid'],['empty string',''],['object',{}]];
for(const [field,code,set] of structuralArrayCases) {
 for(const [name,value] of invalidArrays) {
  test('full structure: '+field+' '+name+' fails before both writers',()=>{
   assertBoosterSourceRejected(fullstatsStructureFixture(rows=>{set(rows[0],value);return rows;}),code);
  });
 }
}
const structuralObjectCases=[
 ['campaign','HTTP_CAMPAIGN_SCHEMA',(rows,v)=>{rows[0]=v;}],
 ['day','HTTP_DAY_SCHEMA',(rows,v)=>{rows[0].days[0]=v;}],
 ['app','HTTP_APP_SCHEMA',(rows,v)=>{rows[0].days[0].apps[0]=v;}],
 ['nm row','HTTP_NM_ROW_SCHEMA',(rows,v)=>{rows[0].days[0].apps[0].nm[0]=v;}],
 ['booster row','HTTP_BOOSTER_ROW_SCHEMA',(rows,v)=>{rows[0].boosterStats[0]=v;}]
];
for(const [field,code,set] of structuralObjectCases) {
 for(const [name,value] of [['null',null],['false',false],['zero',0],['string','invalid'],['array',[]]]) {
  test('full structure: '+field+' '+name+' cannot masquerade as an object',()=>{
   assertBoosterSourceRejected(fullstatsStructureFixture(rows=>{set(rows,value);return rows;}),code);
  });
 }
}
for(const [name,body] of [['null',null],['false',false],['number',1],['string','invalid'],['missing data',{}],
 ['data false',{data:false}],['data null',{data:null}],['data object',{data:{}}],['data number',{data:1}],['data string',{data:''}]]) {
 test('full structure: malformed top-level '+name+' is not empty/no_stats',()=>{
  assertBoosterSourceRejected(fullstatsStructureFixture(()=>body),'HTTP_RESPONSE_SCHEMA');
 });
}
for(const [field,code,remove] of [
 ['days','HTTP_DAYS_SCHEMA',c=>delete c.days],
 ['apps','HTTP_APPS_SCHEMA',c=>delete c.days[0].apps],
 ['nm/nms','HTTP_NM_SCHEMA',c=>delete c.days[0].apps[0].nm]]) {
 test('full structure: missing required '+field+' is not defaulted to []',()=>{
  assertBoosterSourceRejected(fullstatsStructureFixture(rows=>{remove(rows[0]);return rows;}),code);
 });
}
for(const [name,nm,nms] of [['two empty arrays',[],[]],['false and empty array',false,[]],['valid and false',[],false],
 ['two identical arrays',[{nmId:909000001,sum:1}],[{nmId:909000001,sum:1}]]]) {
 test('full structure: both nm aliases ('+name+') fail closed',()=>{
  assertBoosterSourceRejected(fullstatsStructureFixture(rows=>{Object.assign(rows[0].days[0].apps[0],{nm,nms});return rows;}),'HTTP_NM_ALIASES');
 });
}
const scalarShapeCases=[
 ['advertId','HTTP_CAMPAIGN_VALUE_SCHEMA',(c,v)=>{c.advertId=v;}],
 ['date','HTTP_DAY_VALUE_SCHEMA',(c,v)=>{c.days[0].date=v;}],
 ['appType','HTTP_APP_VALUE_SCHEMA',(c,v)=>{c.days[0].apps[0].appType=v;}],
 ['nmId','HTTP_NM_VALUE_SCHEMA',(c,v)=>{c.days[0].apps[0].nm[0].nmId=v;}],
 ['name','HTTP_NM_VALUE_SCHEMA',(c,v)=>{c.days[0].apps[0].nm[0].name=v;}],
 ['sum','HTTP_NM_VALUE_SCHEMA',(c,v)=>{c.days[0].apps[0].nm[0].sum=v;}],
 ['sum_price','HTTP_NM_VALUE_SCHEMA',(c,v)=>{c.days[0].apps[0].nm[0].sum_price=v;}],
 ['booster date','HTTP_BOOSTER_VALUE_SCHEMA',(c,v)=>{c.boosterStats[0].date=v;}],
 ['booster nm','HTTP_BOOSTER_VALUE_SCHEMA',(c,v)=>{c.boosterStats[0].nm=v;}],
 ['avg_position','HTTP_BOOSTER_VALUE_SCHEMA',(c,v)=>{c.boosterStats[0].avg_position=v;}]
];
for(const [field,code,set] of scalarShapeCases) {
 for(const [name,value] of [['object',{}],['array',[]]]) {
  test('full structure: consumed scalar '+field+' cannot be a '+name,()=>{
   assertBoosterSourceRejected(fullstatsStructureFixture(rows=>{set(rows[0],value);return rows;}),code);
  });
 }
}
for(const metric of ['views','clicks','ctr','cpc','cr','atbs','orders','canceled','shks','sumPrice']) {
 test('full structure: remaining consumed metric '+metric+' container rejected',()=>{
  assertBoosterSourceRejected(fullstatsStructureFixture(rows=>{rows[0].days[0].apps[0].nm[0][metric]={};return rows;}),'HTTP_NM_VALUE_SCHEMA');
 });
}
for(const [name,code,mutate] of [
 ['advertId/advertID','HTTP_CAMPAIGN_VALUE_ALIASES',c=>{c.advertID=c.advertId;}],
 ['date/dt','HTTP_DAY_VALUE_ALIASES',c=>{c.days[0].dt=c.days[0].date;}],
 ['appType/appName','HTTP_APP_VALUE_ALIASES',c=>{c.days[0].apps[0].appName=c.days[0].apps[0].appType;}],
 ['nmId/nm','HTTP_NM_VALUE_ALIASES',c=>{const n=c.days[0].apps[0].nm[0];n.nm=n.nmId;}],
 ['sum_price/sumPrice','HTTP_NM_VALUE_ALIASES',c=>{Object.assign(c.days[0].apps[0].nm[0],{sum_price:null,sumPrice:1});}],
 ['booster nm/nmId','HTTP_BOOSTER_VALUE_ALIASES',c=>{c.boosterStats[0].nmId=c.boosterStats[0].nm;}],
 ['position aliases','HTTP_BOOSTER_VALUE_ALIASES',c=>{c.boosterStats[0].position=c.boosterStats[0].avg_position;}],
 ['avgPosition aliases','HTTP_BOOSTER_VALUE_ALIASES',c=>{c.boosterStats[0].avgPosition=c.boosterStats[0].avg_position;}],
 ['position/avgPosition','HTTP_BOOSTER_VALUE_ALIASES',c=>{delete c.boosterStats[0].avg_position;Object.assign(c.boosterStats[0],{position:5,avgPosition:5});}]]) {
 test('full structure: competing consumed aliases '+name+' cannot choose a winner',()=>{
  assertBoosterSourceRejected(fullstatsStructureFixture(rows=>{mutate(rows[0]);return rows;}),code);
 });
}
test('full structure: exact nm:false nms:[] review counterexample with another valid app fails atomically',()=>{
 const f=fullstatsStructureFixture(rows=>{
  assert.equal(rows[0].advertId,37563883);const apps=rows[0].days[0].apps,good=clone(apps[0]);good.appType=2;
  apps[0].nm=false;apps[0].nms=[];apps.push(good);return rows;
 });
 assertBoosterSourceRejected(f,'HTTP_NM_ALIASES');
});
test('full structure: malformed final-batch nm blocks all earlier prepared source rows',()=>{
 const f=fullstatsStructureFixture(rows=>{rows[0].days[0].apps[0].nm=false;return rows;},9);
 const r=assertBoosterSourceRejected(f,'HTTP_NM_SCHEMA');assert.equal(r.counts.statRows,400);assert.equal(r.counts.boosterRows,400);
});
test('full structure: malformed later campaign in same response blocks the whole batch',()=>{
 const f=fullstatsStructureFixture(rows=>{rows[49].days[0].apps[0].nm[0]=null;return rows;});
 const r=assertBoosterSourceRejected(f,'HTTP_NM_ROW_SCHEMA');assert.equal(r.counts.statRows,0);assert.equal(r.counts.boosterRows,0);
});
test('full structure: wrapped data array retains the existing successful container contract',()=>{
 const f=fullstatsStructureFixture(rows=>({data:rows}));assert.equal(f.run().status,'COMPLETE');
});
test('full structure: single alternate aliases throughout the consumed hierarchy remain valid',()=>{
 const f=fullstatsStructureFixture(rows=>{
  const c=rows[0],d=c.days[0],a=d.apps[0],n=a.nm[0],b=c.boosterStats[0];
  c.advertID=c.advertId;delete c.advertId;d.dt=d.date;delete d.date;a.appName=a.appType;delete a.appType;
  n.nm=n.nmId;delete n.nmId;n.sumPrice=1;a.nms=a.nm;delete a.nm;
  b.nmId=b.nm;delete b.nm;b.avgPosition=b.avg_position;delete b.avg_position;
  c.booster_stats=c.boosterStats;delete c.boosterStats;return rows;
 });
 const r=f.run();assert.equal(r.status,'COMPLETE');assert.equal(r.counts.statRows,431);assert.equal(r.counts.boosterRows,431);
});
test('full structure: legitimate non-control omission produces existing observational no_stats only',()=>{
 const f=fullstatsStructureFixture(rows=>rows.filter(c=>c.advertId!==10000000));
 const r=f.run();assert.equal(r.status,'COMPLETE');assert.equal(r.counts.noStats,1);
 assert.ok(f.tables.RAW_WB_ADV_CAMPAIGN_STATS.some(x=>x.advertId==='10000000'&&x.processed_status==='no_stats'));
});
test('full structure: valid empty non-control response follows existing no_stats contract',()=>{
 const f=fullstatsStructureFixture(()=>[],9);const r=f.run();assert.equal(r.status,'COMPLETE');assert.equal(r.counts.noStats,31);
});
test('full structure: original arrays/objects remain unchanged; opaque unconsumed metadata is allowed',()=>{
 const f=fixture(),source=[{advertId:37563883,days:[{date:'2026-09-30',apps:[{appType:1,nm:[{nmId:909000001,sum:null,metadata:{nested:[]}}]}]}],boosterStats:[]}],before=clone(source);
 assert.equal(f.c.adsDayFullstatsSource_(source),source);assert.deepEqual(source,before);
 const g=fullstatsStructureFixture(rows=>{rows[0].days[0].apps[0].nm[0].metadata={nested:[]};return rows;});assert.equal(g.run().status,'COMPLETE');
});

// Complete scalar contract sweep: independent header coverage + real incident adapter/writers.
const scalarStatFields={load_ts:'timestamp',run_id:'text',period_from:'day',period_to:'day',source_method:'text',processed_status:'text',
 advertId:'int',date:'day',appType:'label',nmId:'int',name:'optionalText',views:'optionalInt',clicks:'optionalInt',ctr:'optionalFloat',
 cpc:'optionalFloat',cr:'optionalFloat',atbs:'optionalInt',orders:'optionalInt',canceled:'optionalInt',shks:'optionalInt',
 sum:'optionalNumeric',sum_price:'optionalNumeric',source_level:'text',raw_json:'optionalText'};
const scalarBoosterFields={load_ts:'timestamp',run_id:'text',period_from:'day',period_to:'day',source_method:'text',processed_status:'text',
 advertId:'int',date:'day',nmId:'int',avg_position:'optionalFloat',raw_json:'optionalText'};
function preparedScalarFixture(destination,field,value) {
 const f=runtimeFixture(),runtime=f.c.adsDayRuntime_;
 f.c.adsDayRuntime_=started=>{
  const io=runtime(started),prepare=io.prepare;
  io.prepare=(...args)=>{const p=prepare(...args);p[destination][0][field]=value;return p;};
  return io;
 };
 return f;
}
function assertScalarRejected(f) {
 const old=clone(f.oldParent),r=f.run();
 assert.notEqual(r.status,'COMPLETE',JSON.stringify(r));assert.ok(r.error,'inspectable terminal reason');
 assert.equal(f.calls.filter(x=>x==='RAW:RAW_WB_ADV_CAMPAIGN_STATS').length,0);
 assert.equal(f.calls.filter(x=>x==='RAW:RAW_WB_ADV_BOOSTER_STATS').length,0);
 assert.equal(f.calls.filter(x=>x==='journal:complete').length,0);
 assert.equal(f.tables.RAW_WB_ADV_CAMPAIGN_STATS.length,0);assert.equal(f.tables.RAW_WB_ADV_BOOSTER_STATS.length,0);
 assert.ok(f.calls.some(x=>x.startsWith('journal:error:')));assert.deepEqual(f.oldParent,old);
 return r;
}
test('scalar contract explicitly covers every actual persisted header in both destinations',()=>{
 const c=context();assert.deepEqual(Object.keys(scalarStatFields),clone(c.WB_ADV_RAW_CAMPAIGN_STATS_HEADERS_));
 assert.deepEqual(Object.keys(scalarBoosterFields),clone(c.WB_ADV_RAW_BOOSTER_STATS_HEADERS_));
 assert.deepEqual(clone(c.WB_ADS_DAY_STAT_SCALARS_),scalarStatFields);assert.deepEqual(clone(c.WB_ADS_DAY_BOOSTER_SCALARS_),scalarBoosterFields);
});
for(const [destination,contract] of [['stats',scalarStatFields],['boosters',scalarBoosterFields]]) {
 for(const [field,kind] of Object.entries(contract)) {
  const invalid=[['boolean false',false],['boolean true',true],['object',{}],['array',[]]];
  if(!kind.startsWith('optional'))invalid.push(['null',null],['undefined',undefined],['empty','']);
  if(/int|numeric|float/i.test(kind))invalid.push(['nonnumeric string','not-a-number'],['NaN',NaN],['Infinity',Infinity],['negative Infinity',-Infinity]);
  if(/int/i.test(kind))invalid.push(['fraction',1.5],['fraction string','1.5'],['INT64 overflow','9223372036854775808']);
  if(kind==='optionalNumeric')invalid.push(['NUMERIC overflow','1e29'],['unsafe exponent','1e-999999999999999999999999'],['rounding overflow','99999999999999999999999999999.9999999995']);
  if(kind==='timestamp')invalid.push(['invalid timestamp','2026-02-30 10:00:00'],['wrong text','now']);
  if(kind==='day')invalid.push(['wrong day','2026-09-29'],['invalid time','2026-09-30T25:00:00Z']);
  for(const [label,value] of invalid)test('scalar sweep: '+destination+'.'+field+' rejects '+label+' before BOTH writers/COMPLETE',()=>{
   assertScalarRejected(preparedScalarFixture(destination,field,value));
  });
 }
}
// Valid leaf representations remain source faithful, including NULL/absence and numerical strings.
for(const [destination,contract] of [['stats',scalarStatFields],['boosters',scalarBoosterFields]]) {
 for(const [field,kind] of Object.entries(contract)) {
  if(!kind.startsWith('optional'))continue;
  const values=[['null',null],['absent',undefined],['empty','']];
  if(kind==='optionalText') values.push(['text','Сохранённый текст']);
  else values.push(['zero',0],['number',2],['numeric string','2'],['signed string',' +2 '],['negative',-2]);
  if(kind==='optionalNumeric')values.push(['decimal comma','1,25'],['exponent','1.25e2'],['NUMERIC maximum','99999999999999999999999999999.999999999'],
   ['NUMERIC minimum','-99999999999999999999999999999.999999999'],['extra scale','1.2345678915'],['underflow','1e-400']);
  if(kind==='optionalFloat')values.push(['fraction',1.25],['fraction string','1.25'],['exponent','1.25e2']);
  if(kind==='optionalInt')values.push(['INT64 max','9223372036854775807'],['INT64 min','-9223372036854775808'],['hex integer','0x10']);
  for(const [label,value] of values)test('scalar compatibility: '+destination+'.'+field+' accepts '+label+' without rewriting',()=>{
   const f=preparedScalarFixture(destination,field,value),r=f.run();assert.equal(r.status,'COMPLETE',JSON.stringify(r));
   const table=destination==='stats'?'RAW_WB_ADV_CAMPAIGN_STATS':'RAW_WB_ADV_BOOSTER_STATS';
   const row=f.tables[table].find(x=>x.advertId==='37563883');
   assert.equal(row[field],value===null||value===undefined||value===''?null:String(value));
  });
 }
}
const sourceScalarFields=[
 ['advertId',(c,v)=>{c.advertId=v;}],['date',(c,v)=>{c.days[0].date=v;}],['appType',(c,v)=>{c.days[0].apps[0].appType=v;}],
 ...['nmId','name','views','clicks','ctr','cpc','cr','atbs','orders','canceled','shks','sum','sum_price'].map(k=>[k,(c,v)=>{c.days[0].apps[0].nm[0][k]=v;}]),
 ['booster date',(c,v)=>{c.boosterStats[0].date=v;}],['booster nm',(c,v)=>{c.boosterStats[0].nm=v;}],
 ['avg_position',(c,v)=>{c.boosterStats[0].avg_position=v;}]
];
for(const [field,set] of sourceScalarFields)for(const [label,value] of [['boolean',false],['object',{}],['array',[]]]) {
 test('original scalar source: '+field+' '+label+' cannot be normalized into empty/valid data',()=>{
  assertScalarRejected(fullstatsStructureFixture(rows=>{set(rows[0],value);return rows;}));
 });
}
test('scalar regression exact review sum:false with otherwise valid fullstats through real incident adapter',()=>{
 const f=fullstatsStructureFixture(rows=>{rows[0].days[0].apps[0].nm[0].sum=false;return rows;}),r=assertScalarRejected(f);
 assert.equal(r.status,'PARTIAL');assert.equal(r.error,'ADS_PARTIAL');assert.ok(r.failures.some(x=>x.code==='HTTP_NM_VALUE_SCHEMA'));
});
test('prepared sum:false independently fails even if a source adapter returns a malformed prepared row',()=>{
 const r=assertScalarRejected(preparedScalarFixture('stats','sum',false));assert.equal(r.error,'RAW_STAT_SCALAR_SUM');assert.equal(r.status,'ERROR');
});
test('malformed scalar in an empty app cannot be hidden by valid stats in another app',()=>{
 assertScalarRejected(fullstatsStructureFixture(rows=>{rows[0].days[0].apps.push({appType:false,nm:[]});return rows;}));
});
test('malformed scalar in final batch blocks the earlier complete prepared payload',()=>{
 const r=assertScalarRejected(fullstatsStructureFixture(rows=>{rows[0].days[0].apps[0].nm[0].sum=false;return rows;},9));
 assert.equal(r.counts.statRows,400);assert.equal(r.counts.boosterRows,400);
});
test('marker scalar contract permits only blank business payload cells, not booleans/metrics',()=>{
 const f=fixture(),marker=f.c.wbAdvCampaignStatNoStatsRow_(f.rid,'10000000','2026-09-30','2026-09-30');
 for(const field of Object.keys(scalarStatFields).filter(k=>!['load_ts','run_id','period_from','period_to','source_method','processed_status','advertId'].includes(k))) {
  for(const bad of [false,{},[],1])assert.throws(()=>f.c.adsDayValidateScalars_({stats:[{...marker,[field]:bad}],boosters:[]}),new RegExp('RAW_STAT_SCALAR_'+field.toUpperCase()));
 }
});
test('raw_json keeps the repository truncated-string contract, never requires valid JSON parsing',()=>{
 const f=preparedScalarFixture('stats','raw_json','{"intentionally_truncated":');assert.equal(f.run().status,'COMPLETE');
});
test('source scalar aliases preserve valid numeric/name/date representations',()=>{
 const f=fullstatsStructureFixture(rows=>{const c=rows[0],a=c.days[0].apps[0],n=a.nm[0],b=c.boosterStats[0];
  c.advertID=c.advertId;delete c.advertId;a.appName='android';delete a.appType;n.nm='909000001';delete n.nmId;
  n.name='Товар';n.views='12';n.sum='1,25';n.sumPrice='2.5';b.nmId='909000001';delete b.nm;b.position='5.5';delete b.avg_position;return rows;});
 assert.equal(f.run().status,'COMPLETE');assert.equal(f.tables.RAW_WB_ADV_CAMPAIGN_STATS[0].sum,'1,25');
});
for(const value of ['NaN','Infinity','-Infinity','abc','true','1.2.3','1,2,3','+','1e','0e99999999999999999999999']) {
 test('numeric scalar lexical/range contract rejects '+value,()=>{
  const c=context();assert.equal(c.adsDayNumberScalar_(value,true),false);assert.equal(c.adsDayNumberScalar_(value,false),false);
 });
}
for(const [field,set] of sourceScalarFields.filter(([field])=>!['date','appType','name','booster date'].includes(field))) {
 test('original scalar numeric '+field+' rejects nonnumeric text before flatten/publication',()=>{
  assertScalarRejected(fullstatsStructureFixture(rows=>{set(rows[0],'not-a-number');return rows;}));
 });
}
test('complete field validators preserve the entire valid prepared payload bytes',()=>{
 const f=fixture();f.p.boosters=[boosterFixtureRow(f)];const before=JSON.stringify(f.p);
 f.c.adsDayValidateScalars_(f.p);assert.equal(JSON.stringify(f.p),before);assert.equal(f.run().status,'COMPLETE');
});
