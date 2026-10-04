/** Narrow incident adapter: no schema creation, property writes, continuations or general recovery ledger. */
function adsDayHash_(s) {
  return Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256,s,Utilities.Charset.UTF_8)
    .map(function(x){return ('0'+((x+256)%256).toString(16)).slice(-2);}).join('');
}
function adsDaySourceObject_(value,code) {
  adsDayAssert_(value!==null && typeof value==='object' && !Array.isArray(value),code);
}
function adsDaySourceNames_(record,names,code) {
  var present=names.filter(function(name){return Object.prototype.hasOwnProperty.call(record,name);});
  adsDayAssert_(present.length<=1,code+'_ALIASES');
  return present;
}
function adsDaySourceArray_(record,names,optional,code) {
  var present=adsDaySourceNames_(record,names,code);
  if(!present.length && optional) return [];
  adsDayAssert_(present.length===1 && Array.isArray(record[present[0]]),code+'_SCHEMA');
  return record[present[0]];
}
function adsDaySourceScalars_(record,groups,code) {
  groups.forEach(function(names){
    var present=adsDaySourceNames_(record,names,code);
    if(!present.length) return; // Existing nullable/missing leaf values retain their semantics.
    var value=record[present[0]];
    var field=names[0]==='nm'?'nmId':names[0];
    var kind=WB_ADS_DAY_STAT_SCALARS_[field] || WB_ADS_DAY_BOOSTER_SCALARS_[field];
    // Preserve absent/null/blank leaf semantics; never convert malformed originals into empty rows.
    // Keep the existing HTTP_DATE_SCOPE / BOOSTER_DATE_SCOPE guards responsible for date values.
    adsDayAssert_(kind && (adsDayBlankScalar_(value) || (kind==='day'?typeof value==='string':adsDayScalar_(value,kind))),code+'_SCHEMA');
  });
}
function adsDayBoosterSource_(camp) {
  return adsDaySourceArray_(camp,['boosterStats','booster_stats'],true,'HTTP_BOOSTER');
}
/** Check ORIGINAL consumed structures before any evidence/parser/flatten fallback. Never mutate source. */
function adsDayFullstatsSource_(json) {
  var camps;
  if(Array.isArray(json)) camps=json;
  else {
    adsDaySourceObject_(json,'HTTP_RESPONSE_SCHEMA');
    camps=adsDaySourceArray_(json,['data'],false,'HTTP_RESPONSE');
  }
  camps.forEach(function(camp){
    adsDaySourceObject_(camp,'HTTP_CAMPAIGN_SCHEMA');
    adsDaySourceScalars_(camp,[['advertId','advertID']],'HTTP_CAMPAIGN_VALUE');
    var days=adsDaySourceArray_(camp,['days'],false,'HTTP_DAYS');
    days.forEach(function(day){
      adsDaySourceObject_(day,'HTTP_DAY_SCHEMA');
      adsDaySourceScalars_(day,[['date','dt']],'HTTP_DAY_VALUE');
      var apps=adsDaySourceArray_(day,['apps'],false,'HTTP_APPS');
      apps.forEach(function(app){
        adsDaySourceObject_(app,'HTTP_APP_SCHEMA');
        adsDaySourceScalars_(app,[['appType','appName']],'HTTP_APP_VALUE');
        var nms=adsDaySourceArray_(app,['nm','nms'],false,'HTTP_NM');
        nms.forEach(function(nm){
          adsDaySourceObject_(nm,'HTTP_NM_ROW_SCHEMA');
          adsDaySourceScalars_(nm,[['nmId','nm'],['name'],['views'],['clicks'],['ctr'],['cpc'],['cr'],
            ['atbs'],['orders'],['canceled'],['shks'],['sum'],['sum_price','sumPrice']],'HTTP_NM_VALUE');
        });
      });
    });
    adsDayBoosterSource_(camp).forEach(function(b){
      adsDaySourceObject_(b,'HTTP_BOOSTER_ROW_SCHEMA');
      adsDaySourceScalars_(b,[['date'],['nm','nmId'],['avg_position','position','avgPosition']],'HTTP_BOOSTER_VALUE');
    });
  });
  return camps;
}
function runWbAdsRepair20260930(spec) {
  var properties=PropertiesService.getScriptProperties();
  if(properties.getProperty('WB_ADS_REPAIR_20260930_ENABLED')!=='1') return {status:'DISABLED'};
  adsDayAssert_(properties.getProperty('WB_ADS_RESUME_ENABLED')!=='1' &&
    (typeof WB_ADS_RESUME_IO_==='undefined' || !WB_ADS_RESUME_IO_) && WB_ADS_RAW_RUN_T0_===null, 'OTHER_ADS_CONTEXT');
  var lock=LockService.getScriptLock();
  if(!lock.tryLock(1)) return {status:'SKIPPED_LOCKED'};
  var started=Date.now();
  try { return adsDayExecute_(spec,adsDayRuntime_(started)); }
  finally { lock.releaseLock(); }
}
function adsDayRuntime_(started) {
  var c=getBqConfig_(), finishBy=started+330000, sourceBy=finishBy-75000, sheet;
  adsDayAssert_(c.projectId==='project-fa311fc0-4d87-4781-986' && c.datasetId==='wb_raw' && c.location==='EU' && wbAdsBqSinkOn_(), 'RUNTIME_CONTEXT');
  var destinations={RAW_WB_ADV_CAMPAIGN_STATS:WB_ADV_RAW_CAMPAIGN_STATS_HEADERS_,RAW_WB_ADV_BOOSTER_STATS:WB_ADV_RAW_BOOSTER_STATS_HEADERS_};
  function room(ms) { adsDayAssert_(Date.now()+ms<finishBy,'DEADLINE'); }
  function contextCheck() {
    var current=getBqConfig_();
    adsDayAssert_(current.projectId===c.projectId && current.datasetId===c.datasetId && current.location===c.location && wbAdsBqSinkOn_(), 'RUNTIME_CONTEXT_CHANGED');
  }
  function fq(table) { return '`'+c.projectId+'.wb_raw.'+table+'`'; }
  function params(values) { return Object.keys(values || {}).map(function(k){return ingestParam_(k,'STRING',values[k]);}); }
  function wait(job) {
    while(job.status.state!=='DONE') { room(15000); Utilities.sleep(500); job=BigQuery.Jobs.get(c.projectId,job.jobReference.jobId,{location:c.location}); }
    adsDayAssert_(!job.status.errorResult && !(job.status.errors || []).length,'BQ_JOB_FAILED'); return job;
  }
  function query(sql,values) {
    room(15000); adsDayAssert_(/^SELECT\b/.test(sql),'SELECT_ONLY');
    var job=BigQuery.Jobs.insert({jobReference:{projectId:c.projectId,location:c.location,jobId:'adsday_q_'+Utilities.getUuid().replace(/-/g,'')},
      configuration:{query:{query:sql,useLegacySql:false,parameterMode:'NAMED',queryParameters:params(values),
        maximumBytesBilled:'100000000',useQueryCache:false}}},c.projectId);
    job=wait(job); room(10000);
    var res=BigQuery.Jobs.getQueryResults(c.projectId,job.jobReference.jobId,{location:c.location,maxResults:10000,timeoutMs:1000});
    adsDayAssert_(res.jobComplete===true && !res.pageToken && !(res.errors || []).length &&
      Number(res.totalRows || 0)===(res.rows || []).length,'BQ_QUERY_INCOMPLETE');
    return (res.rows || []).map(function(r){return JSON.parse(r.f[0].v);});
  }
  function one(sql,values) { var rows=query(sql,values); adsDayAssert_(rows.length===1,'EVIDENCE_CARDINALITY'); return rows[0]; }
  function columns(headers) { return headers.map(function(k){return '`'+k+'`';}).join(','); }
  function rows(table,headers,predicate,values) {
    return query('SELECT TO_JSON_STRING(STRUCT('+columns(headers)+')) FROM '+fq(table)+' WHERE '+predicate,values);
  }
  function digest(rs) { return adsDayHash_(adsDayCanonical_(rs.map(adsDayCanonical_).sort())); }
  function evidence() {
    var p={parent:WB_ADS_DAY_REPAIR_PARENT_,origin:WB_ADS_DAY_REPAIR_ORIGIN_,day:WB_ADS_DAY_REPAIR_DATE_};
    var parent=one('SELECT TO_JSON_STRING(STRUCT(run_id,loader_name,logical_period,source,status,error_code,error_message,started_at,completed_at)) FROM '+fq('INGEST_RUNS')+' WHERE run_id=@parent',p);
    var campaigns=rows('RAW_WB_ADV_CAMPAIGNS',['run_id','advertId','status','load_ts','source_method','processed_status'],'run_id=@origin',p)
      .sort(function(a,b){return Number(a.advertId)-Number(b.advertId);});
    var marker=one('SELECT TO_JSON_STRING(STRUCT(run_id,source_method,window_index,period_from,period_to,status,http_success,returned_rows,rows_out_of_window,window_completed_at)) FROM '+
      fq('RAW_WB_ADV_COSTS_RUNS')+' WHERE run_id=@origin AND period_from<=@day AND period_to>=@day',p);
    var count=one('SELECT TO_JSON_STRING(STRUCT(COUNT(*) AS n)) FROM '+fq('RAW_WB_ADV_COSTS')+
      ' WHERE run_id=@origin AND window_index=@window AND period_from=@from AND period_to=@to',
      {origin:p.origin,window:marker.window_index,from:marker.period_from,to:marker.period_to});
    var billing=query('SELECT TO_JSON_STRING(STRUCT(advertId,COUNT(*) AS n,SUM(SAFE_CAST(updSum AS NUMERIC)) AS spend)) FROM '+fq('V_ADV_COSTS')+
      ' WHERE SUBSTR(updDate,1,10)=@day GROUP BY advertId',p).sort(function(a,b){return Number(a.advertId)-Number(b.advertId);});
    var coverage=one('SELECT TO_JSON_STRING(STRUCT(chosen_run_id,chosen_window_index,requested_ok,not_loaded,answer_rows,answer_sum_rub,age_ok,stable_ok,billed_complete)) FROM '+
      fq('V_ADV_COSTS_DAY_COVERAGE')+' WHERE date=CAST(@day AS DATE)',p);
    return {parent:parent,campaigns:campaigns,costMarker:marker,costCount:Number(count.n),billing:billing,costCoverage:coverage};
  }
  function tableCheck(table,headers) {
    room(10000); var t=BigQuery.Tables.get(c.projectId,c.datasetId,table), fields=(t.schema || {}).fields || [];
    adsDayAssert_(t.type==='TABLE' && fields.length===headers.length && headers.every(function(h,i){
      return fields[i].name===h && fields[i].type==='STRING' && (!fields[i].mode || fields[i].mode==='NULLABLE');
    }),'RAW_SCHEMA');
  }
  function effective() {
    return rows('V_ADV_CAMPAIGN_STATS',WB_ADV_RAW_CAMPAIGN_STATS_HEADERS_,
      'SUBSTR(date,1,10) BETWEEN "2026-09-24" AND "2026-09-30"',{});
  }
  function baseline() {
    var rs=effective(), target=rs.filter(function(r){return String(r.date).slice(0,10)===WB_ADS_DAY_REPAIR_DATE_;});
    var physical=Object.keys(destinations).map(function(t) {
      return one('SELECT TO_JSON_STRING(STRUCT(COUNT(*) AS n)) FROM '+fq(t)+' WHERE SUBSTR(date,1,10)=@day',{day:WB_ADS_DAY_REPAIR_DATE_}).n;
    });
    return {historyHash:digest(rs.filter(function(r){return String(r.date).slice(0,10)!==WB_ADS_DAY_REPAIR_DATE_;})),
      targetEmpty:target.length===0 && physical.every(function(n){return Number(n)===0;})};
  }
  function http(ids,url,token,requests,present) {
    adsDayAssert_(Date.now()+30000<sourceBy,'DEADLINE');
    var response;
    for(var retry=0;retry<3;retry++) {
      adsDayAssert_(Date.now()+30000<sourceBy,'DEADLINE');
      try { response=UrlFetchApp.fetch(url,{method:'get',headers:{Authorization:token},muteHttpExceptions:true}); }
      catch(ignored) { adsDayAssert_(false,'HTTP_NETWORK'); }
      var code=response.getResponseCode();
      if(code!==429 && code<500) break;
      if(retry===2) break;
      var headers=response.getAllHeaders(), ra=headers['Retry-After'] || headers['retry-after'];
      var delay=Math.max(21000*(retry+1),/^[0-9]+$/.test(String(ra))?Number(ra)*1000:
        (Number.isFinite(Date.parse(ra))?Math.max(0,Date.parse(ra)-Date.now()):0));
      adsDayAssert_(Date.now()+delay+30000<sourceBy,'DEADLINE'); Utilities.sleep(delay);
    }
    var record={ids:ids.map(String),http:code,attempts:retry+1,at:wbAdsNow_(),responseIds:[],controls:[]}; requests.push(record);
    if(code<200 || code>=300) return {ok:false,code:code,body:''};
    var json;
    try { json=JSON.parse(response.getContentText()); } catch(ignored2) { adsDayAssert_(false,'HTTP_JSON'); }
    var camps=adsDayFullstatsSource_(json), seen={};
    camps.forEach(function(camp) {
      var boosters=adsDayBoosterSource_(camp);
      var id=String(camp.advertId!=null?camp.advertId:camp.advertID);
      record.responseIds.push(id);
      if(WB_ADS_DAY_REPAIR_CONTROLS_.indexOf(id)>=0) {
        var detail={advertId:id,dates:[],nmIds:[],appTypes:[],boosterDates:[]};
        camp.days.forEach(function(day) {
          detail.dates.push(String(day.date!=null?day.date:day.dt));
          day.apps.forEach(function(app) {
            detail.appTypes.push(String(app.appType!=null?app.appType:app.appName));
            adsDaySourceArray_(app,['nm','nms'],false,'HTTP_NM').forEach(function(nm) {detail.nmIds.push(String(nm.nmId!=null?nm.nmId:nm.nm));});
          });
        });
        boosters.forEach(function(b){detail.boosterDates.push(String(b.date));});
        record.controls.push(detail);
      }
      adsDayAssert_(record.ids.indexOf(id)>=0 && !seen[id],'HTTP_CAMPAIGN_SCOPE'); seen[id]=true;
      camp.days.forEach(function(day) {
        adsDayAssert_(adsDayDate_(day.date!=null?day.date:day.dt) && Array.isArray(day.apps),'HTTP_DATE_SCOPE');
      });
      boosters.forEach(function(b){adsDayAssert_(adsDayDate_(b.date),'BOOSTER_DATE_SCOPE');});
      present.push(id);
    });
    record.responseIds=Object.keys(seen); return {ok:true,code:code,json:camps,body:''};
  }
  function prepare(ordered,rid) {
    var token=getWbAdsToken_(); adsDayAssert_(token && token.token,'TOKEN_UNAVAILABLE');
    var requests=[],present=[],ctx;
    try {
      ctx=wbAdsFullstatsCollect_(token.token,ordered.map(function(r){return Number(r.advertId);}),
        WB_ADS_DAY_REPAIR_DATE_,WB_ADS_DAY_REPAIR_DATE_,rid,sourceBy,
        {capture:function(value){ctx=value;},fetch:function(ids,url,t){return http(ids,url,t,requests,present);}});
    } catch(e) {
      // Keep inspectable request/control evidence even if HTTP shape/deadline guard throws.
      ctx=ctx || {collected:[],noStats:{},failures:[],skipped:[]};
      ctx.failures.push({code:e.code || 'HTTP_UNKNOWN'});
      var covered=ctx.collected.map(function(camp){return String(camp.advertId!=null?camp.advertId:camp.advertID);}).concat(Object.keys(ctx.noStats));
      ctx.skipped=ordered.map(function(r){return r.advertId;}).filter(function(id){return covered.indexOf(id)<0;});
    }
    var flat=wbAdvFlattenFullstats_(ctx.collected,rid,WB_ADS_DAY_REPAIR_DATE_,WB_ADS_DAY_REPAIR_DATE_), classifications={};
    ctx.collected.forEach(function(camp){classifications[String(camp.advertId!=null?camp.advertId:camp.advertID)]='PROCESSED';});
    Object.keys(ctx.noStats).forEach(function(id){classifications[id]='NO_STATS';
      flat.statRows.push(wbAdvCampaignStatNoStatsRow_(rid,Number(id),WB_ADS_DAY_REPAIR_DATE_,WB_ADS_DAY_REPAIR_DATE_));});
    return {stats:flat.statRows,boosters:flat.boosterRows,classifications:classifications,failures:ctx.failures,
      skipped:ctx.skipped,requests:requests,present:present};
  }
  function publish(p,rid) {
    var jobs=[],total=0;
    Object.keys(destinations).forEach(function(table) {
      var data=adsDayRows_(table==='RAW_WB_ADV_CAMPAIGN_STATS'?p.stats:p.boosters,destinations[table]);
      if(!data.length) return;
      room(25000); tableCheck(table,destinations[table]);
      var blob=Utilities.newBlob(data.map(function(r){return JSON.stringify(r);}).join('\n'),'application/octet-stream');
      adsDayAssert_(blob.getBytes().length<5000000,'LOAD_SIZE');
      var job=BigQuery.Jobs.insert({jobReference:{projectId:c.projectId,location:c.location,jobId:rid+'_'+table},configuration:{load:{
        destinationTable:{projectId:c.projectId,datasetId:c.datasetId,tableId:table},sourceFormat:'NEWLINE_DELIMITED_JSON',
        createDisposition:'CREATE_NEVER',writeDisposition:'WRITE_APPEND',ignoreUnknownValues:false,maxBadRecords:0}}},c.projectId,blob);
      job=wait(job); adsDayAssert_(Number(job.statistics.load.outputRows)===data.length,'LOAD_ROW_COUNT');
      var readback=rows(table,destinations[table],'run_id=@run',{run:rid});
      adsDayAssert_(digest(readback)===digest(data),'RAW_READBACK'); total+=data.length;
      jobs.push({table:table,jobId:job.jobReference.jobId,rows:data.length,sha256:digest(data)});
    });
    return {confirmed:true,readback:true,rows:total,jobs:jobs};
  }
  function audit(result,status) {
    var entries=result.controls.concat([{origin:result.origin,parent:result.parent,campaignHash:result.campaignHash,
      evidenceHash:result.evidenceHash,billingMaturity:result.billingMaturity,receipt:result.receipt,counts:result.counts,error:result.error,failures:result.failures,skipped:result.skipped}]);
    entries.forEach(function(entry) {
      room(2000); var r=wbAdsMakeResult_(result.runId,'repair_fullstats_20260930');
      r.period_from=r.period_to=WB_ADS_DAY_REPAIR_DATE_; r.finished_at=wbAdsNow_();r.status=status;
      r.campaigns_found=431;r.campaigns_sampled=result.counts?result.counts.classified:0;r.rows_or_items_found=result.counts?result.counts.statRows:0;r.response_keys_sample=JSON.stringify(entry);r.error_message=result.error || '';
      adsDayAssert_(r.response_keys_sample.length<45000,'STATUS_SIZE');
      var index=sheet.getLastRow()+1; writeWbAdsStatusRow_(sheet,r);
      var row=sheet.getRange(index,1,1,WB_ADS_STATUS_HEADERS_.length).getValues()[0];
      adsDayAssert_(row[0]===result.runId && row[10]===status && row[12]===r.response_keys_sample,'STATUS_READBACK');
    });
  }
  function attempt(rid) {
    adsDayAssert_(rid!==WB_ADS_DAY_REPAIR_PARENT_,'OLD_ATTEMPT_WRITE');
    var r=one('SELECT TO_JSON_STRING(STRUCT(status,logical_period,loader_name,source)) FROM '+fq('INGEST_RUNS')+' WHERE run_id=@rid',{rid:rid});
    adsDayAssert_(r.logical_period===WB_ADS_DAY_REPAIR_DATE_ && r.loader_name==='ads' && r.source==='apps_script','ATTEMPT_SCOPE'); return r.status;
  }
  return {hash:adsDayHash_,evidence:evidence,baseline:baseline,prepare:prepare,publish:publish,audit:audit,attempt:attempt,
    preflight:function(spec) {
      room(20000); adsDayAssert_(BigQuery.Datasets.get(c.projectId,c.datasetId).location==='EU','DATASET_LOCATION');
      Object.keys(destinations).forEach(function(t){tableCheck(t,destinations[t]);});
      var view=BigQuery.Tables.get(c.projectId,c.datasetId,'V_ADV_CAMPAIGN_STATS');
      adsDayAssert_(view.type==='VIEW' && view.view && !view.view.useLegacySql && adsDayHash_(view.view.query)===spec.effectiveViewHash,'EFFECTIVE_VIEW_HASH');
      var journal=BigQuery.Tables.get(c.projectId,c.datasetId,'INGEST_RUNS'), fields={};
      (journal.schema.fields || []).forEach(function(f){fields[f.name]=f.type;});
      adsDayAssert_(journal.type==='TABLE' && ['run_id','loader_name','status','source','trigger_type','error_code','error_message'].every(function(k){return fields[k]==='STRING';}) &&
        fields.logical_period==='DATE' && fields.started_at==='TIMESTAMP' && fields.completed_at==='TIMESTAMP' &&
        ['rows_fetched','rows_loaded'].every(function(k){return fields[k]==='INTEGER' || fields[k]==='INT64';}),'JOURNAL_SCHEMA');
      sheet=SpreadsheetApp.getActiveSpreadsheet().getSheetByName('WB_ADS_STATUS');
      adsDayAssert_(sheet && sheet.getLastColumn()===WB_ADS_STATUS_HEADERS_.length &&
        adsDayCanonical_(sheet.getRange(1,1,1,WB_ADS_STATUS_HEADERS_.length).getValues()[0])===adsDayCanonical_(WB_ADS_STATUS_HEADERS_),'STATUS_SCHEMA');
      var concurrent=one('SELECT TO_JSON_STRING(STRUCT(COUNT(*) AS n)) FROM '+fq('INGEST_RUNS')+
        ' WHERE loader_name="ads" AND logical_period=CAST(@day AS DATE) AND status IN ("STARTED","COMPLETE")',{day:WB_ADS_DAY_REPAIR_DATE_});
      adsDayAssert_(Number(concurrent.n)===0,'OTHER_ATTEMPT');
    },
    start:function(){room(20000);contextCheck();return ingestRunStart_('ads',WB_ADS_DAY_REPAIR_DATE_,'MANUAL');},
    complete:function(rid,n){room(15000);contextCheck();adsDayAssert_(rid!==WB_ADS_DAY_REPAIR_PARENT_,'OLD_ATTEMPT_WRITE');return ingestRunComplete_(rid,n,n);},
    error:function(rid,code){contextCheck();adsDayAssert_(rid!==WB_ADS_DAY_REPAIR_PARENT_,'OLD_ATTEMPT_WRITE');return ingestRunError_(rid,'ADS_REPAIR_'+code,
      'origin='+WB_ADS_DAY_REPAIR_ORIGIN_+'; parent='+WB_ADS_DAY_REPAIR_PARENT_+'; reason='+code+'; inspect WB_ADS_STATUS and RAW jobs before retry');},
    validateEffective:function(before,p,rid){
      var rs=effective(), target=rs.filter(function(r){return String(r.date).slice(0,10)===WB_ADS_DAY_REPAIR_DATE_;});
      return digest(target)===digest(adsDayRows_(p.stats.filter(function(r){return r.processed_status==='raw';}),WB_ADV_RAW_CAMPAIGN_STATS_HEADERS_)) &&
        digest(rs.filter(function(r){return String(r.date).slice(0,10)!==WB_ADS_DAY_REPAIR_DATE_;}))===before.historyHash;
    }
  };
}
