/** Incident-only 2026-09-30 repair. No I/O at file load; ordinary daily routing is unchanged. */
var WB_ADS_DAY_REPAIR_DATE_ = '2026-09-30';
var WB_ADS_DAY_REPAIR_ORIGIN_ = 'ADSRAW_20261001_050732_749';
var WB_ADS_DAY_REPAIR_PARENT_ = 'INS_ADS_20261001050734_5253f5ba';
var WB_ADS_DAY_REPAIR_CONTROLS_ = ['37563883','37727911','37727969','37727999','38085124',
  '37669244','37751908','37755348','39035441'];
function adsDayAssert_(ok, code) { if (!ok) { var e = new Error(code); e.code = code; throw e; } }
function adsDayCanonical_(v) {
  if (Array.isArray(v)) return '[' + v.map(adsDayCanonical_).join(',') + ']';
  if (v && typeof v === 'object') return '{' + Object.keys(v).sort().map(function (k) {
    return JSON.stringify(k) + ':' + adsDayCanonical_(v[k]);
  }).join(',') + '}';
  return JSON.stringify(v);
}
function adsDayRows_(rows, headers) {
  return rows.map(function (r) { var out = {}; headers.forEach(function (k) {
    out[k] = r[k] === undefined || r[k] === null || r[k] === '' ? null : String(r[k]);
  }); return out; });
}
function adsDayOrder_(campaigns) {
  var seen = {}, rank = {'9':0,'11':1,'7':2};
  var copy = campaigns.map(function (r) {
    var id = String(r.advertId), status = String(r.status);
    adsDayAssert_(/^[1-9][0-9]{0,14}$/.test(id) && Number.isSafeInteger(Number(id)) && !seen[id], 'CAMPAIGN_ID_SET');
    adsDayAssert_(rank[status] !== undefined, 'CAMPAIGN_STATUS'); seen[id] = true;
    return {advertId:id,status:status};
  });
  return copy.sort(function (a,b) {
    return rank[a.status]-rank[b.status] ||
      Number(WB_ADS_DAY_REPAIR_CONTROLS_.indexOf(b.advertId)>=0)-Number(WB_ADS_DAY_REPAIR_CONTROLS_.indexOf(a.advertId)>=0) ||
      Number(a.advertId)-Number(b.advertId);
  });
}
function adsDayValidateEvidence_(e, spec, io) {
  var h = e.parent, m = e.costMarker, cov = e.costCoverage;
  adsDayAssert_(h && h.run_id === WB_ADS_DAY_REPAIR_PARENT_ && h.logical_period === WB_ADS_DAY_REPAIR_DATE_ &&
    h.loader_name === 'ads' && h.source === 'apps_script' && h.status === 'ERROR' && h.error_code === 'ADS_PARTIAL' &&
    /raw_campaigns=OK\(431\)/.test(h.error_message) && /raw_costs=OK\(74\)/.test(h.error_message), 'ORIGIN_HEARTBEAT');
  var ordered = adsDayOrder_(e.campaigns), counts = {'7':0,'9':0,'11':0};
  adsDayAssert_(ordered.length === 431, 'CAMPAIGN_COUNT');
  ordered.forEach(function (r) { counts[r.status]++; });
  adsDayAssert_(counts['7']===411 && counts['9']===1 && counts['11']===19, 'CAMPAIGN_STATUS_COUNTS');
  var snapshot = ordered.slice().sort(function(a,b){return Number(a.advertId)-Number(b.advertId);});
  adsDayAssert_(io.hash(adsDayCanonical_(snapshot))===spec.campaignHash, 'CAMPAIGN_HASH');
  adsDayAssert_(e.campaigns.every(function(r) {
    var t = /^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$/.test(r.load_ts) ? Date.parse(r.load_ts.replace(' ','T')+'+03:00') : NaN;
    return r.run_id===WB_ADS_DAY_REPAIR_ORIGIN_ && r.source_method==='promotion/count+adverts/v2' &&
      ['raw','count_only'].indexOf(r.processed_status)>=0 && t>=Date.parse(h.started_at)-1000 && t<=Date.parse(h.completed_at);
  }), 'CAMPAIGN_TIME_PROVENANCE');
  adsDayAssert_(WB_ADS_DAY_REPAIR_CONTROLS_.every(function(id){return snapshot.some(function(r){return r.advertId===id;});}), 'CONTROL_POPULATION');
  var ct = m && Date.parse(String(m.window_completed_at).replace(' ','T')+'+03:00');
  adsDayAssert_(m && m.run_id===WB_ADS_DAY_REPAIR_ORIGIN_ && m.source_method==='adv/v1/upd' && m.window_index==='0' &&
    m.period_from==='2026-09-17' && m.period_to===WB_ADS_DAY_REPAIR_DATE_ && m.status==='OK' && m.http_success==='true' &&
    Number(m.returned_rows)===74 && Number(m.rows_out_of_window)===0 && e.costCount===74 &&
    ct>=Date.parse(h.started_at) && ct<=Date.parse(h.completed_at), 'COSTS_COMMIT');
  adsDayAssert_(cov && cov.chosen_run_id===WB_ADS_DAY_REPAIR_ORIGIN_ && String(cov.chosen_window_index)==='0' &&
    cov.requested_ok===true && cov.not_loaded===false && Number(cov.answer_rows)===6 && Number(cov.answer_sum_rub)===1737,
    'COSTS_COVERAGE');
  var expected = {'37563883':[1,12],'37727911':[1,323],'37727969':[1,251],'37727999':[2,688],'38085124':[1,463]};
  adsDayAssert_(e.billing.length===5 && e.billing.every(function(r) {
    var v=expected[r.advertId]; return v && Number(r.n)===v[0] && Number(r.spend)===v[1];
  }) && new Set(e.billing.map(function(r){return r.advertId;})).size===5, 'COSTS_DAY_READBACK');
  return ordered;
}
function adsDayDate_(value) {
  var s=String(value || '');
  return s===WB_ADS_DAY_REPAIR_DATE_ ||
    (/^2026-09-30T\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)?$/.test(s) && Number.isFinite(Date.parse(s)));
}
/** Exact V_ADV_BOOSTER_STATS grain (Wbadsbigquery.gs): date / advertId / nmId.
 * Compare keys using the existing RAW STRING/NULL representation; never change payload rows.
 * Every repeated key is invalid, even when all payload values are identical.
 */
function adsDayValidateBoosterGrain_(rows) {
  adsDayAssert_(Array.isArray(rows), 'BOOSTER_GRAIN_PAYLOAD');
  var seen=Object.create(null);
  rows.forEach(function(row) {
    var key=JSON.stringify(['date','advertId','nmId'].map(function(k) {
      var v=row[k]; return v===undefined || v===null || v==='' ? null : String(v);
    }));
    adsDayAssert_(!Object.prototype.hasOwnProperty.call(seen,key), 'BOOSTER_GRAIN_DUPLICATE');
    seen[key]=true;
  });
}
/** Complete persisted field contract. RAW storage is nullable STRING, not a business type.
 * Evidence: WB_ADV_RAW_*_HEADERS_, wbAdvFlattenFullstats_, Mart1 parse-QC,
 * ads3_campaign_performance.sql and ADS3_CAMPAIGN_PERFORMANCE_2026-09-06.md §2.
 * Validation never rewrites source values, fills metrics or changes key bytes.
 */
var WB_ADS_DAY_STAT_SCALARS_ = {
  load_ts:'timestamp',run_id:'text',period_from:'day',period_to:'day',source_method:'text',processed_status:'text',
  advertId:'int',date:'day',appType:'label',nmId:'int',name:'optionalText',
  views:'optionalInt',clicks:'optionalInt',ctr:'optionalFloat',cpc:'optionalFloat',cr:'optionalFloat',
  atbs:'optionalInt',orders:'optionalInt',canceled:'optionalInt',shks:'optionalInt',
  sum:'optionalNumeric',sum_price:'optionalNumeric',source_level:'text',raw_json:'optionalText'
};
var WB_ADS_DAY_BOOSTER_SCALARS_ = {
  load_ts:'timestamp',run_id:'text',period_from:'day',period_to:'day',source_method:'text',processed_status:'text',
  advertId:'int',date:'day',nmId:'int',avg_position:'optionalFloat',raw_json:'optionalText'
};
function adsDayBlankScalar_(v) { return v===undefined || v===null || (typeof v==='string' && v.trim()===''); }
function adsDayIntegerScalar_(v) {
  if(typeof v==='number' && (!Number.isFinite(v) || !Number.isInteger(v))) return false;
  if(typeof v!=='number' && typeof v!=='string') return false;
  // Only after checking the primitive type, inspect the exact STRING sent to INT64 consumers.
  var s=typeof v==='number' ? v.toString() : v.trim(), hex=/^([+-]?)0[xX]([0-9a-fA-F]+)$/.exec(s);
  if(hex) {
    var h=hex[2].toLowerCase().replace(/^0+/,'') || '0', bound=hex[1]==='-'?'8000000000000000':'7fffffffffffffff';
    return h.length<bound.length || (h.length===bound.length && h<=bound);
  }
  var m=/^([+-]?)([0-9]+)$/.exec(s);
  if(!m) return false;
  var digits=m[2].replace(/^0+/,'') || '0', limit=m[1]==='-'?'9223372036854775808':'9223372036854775807';
  return digits.length<limit.length || (digits.length===limit.length && digits<=limit);
}
function adsDayNumberScalar_(v,numeric) {
  if(typeof v!=='number' && typeof v!=='string') return false;
  if(typeof v==='number' && !Number.isFinite(v)) return false;
  var s=typeof v==='number'?v.toString():v.trim();
  // NUMERIC consumers explicitly REPLACE comma with dot; FLOAT leaves have no such consumer.
  if(numeric) s=s.replace(/,/g,'.');
  var m=/^[+-]?(?:([0-9]+)(?:\.([0-9]*))?|\.([0-9]+))(?:[eE]([+-]?[0-9]+))?$/.exec(s);
  if(!m) return false;
  var exponent=Number(m[4] || 0);
  if(!Number.isSafeInteger(exponent)) return false;
  if(!numeric) return Number.isFinite(Number(s));
  var fraction=m[2]===undefined?(m[3] || ''):m[2], digits=((m[1] || '')+fraction).replace(/^0+/,'');
  if(!digits) return true;
  // Model NUMERIC's 38-digit, scale-9 range and rounding using decimal strings, not lossy floats.
  var shift=exponent-fraction.length+9, length=digits.length+shift;
  if(length>38) return false;
  if(length<0) return true; // A tiny finite decimal rounds to zero; retain original bytes.
  var scaled=shift>=0?digits+'0'.repeat(shift):digits.slice(0,length);
  if(shift<0 && digits.charAt(length)>='5') {
    var i=scaled.length-1;
    while(i>=0 && scaled.charAt(i)==='9') i--;
    scaled=i<0?'1'+'0'.repeat(scaled.length):scaled.slice(0,i)+(Number(scaled.charAt(i))+1)+'0'.repeat(scaled.length-i-1);
  }
  return scaled.length<=38;
}
function adsDayTimestampScalar_(v) {
  if(typeof v!=='string') return false;
  var m=/^(\d{4})-(\d\d)-(\d\d) (\d\d):(\d\d):(\d\d)$/.exec(v);
  if(!m) return false;
  var y=Number(m[1]),mo=Number(m[2]),d=Number(m[3]),leap=y%4===0 && (y%100!==0 || y%400===0);
  return y>=1 && mo>=1 && mo<=12 && d>=1 && d<=[31,leap?29:28,31,30,31,30,31,31,30,31,30,31][mo-1] &&
    Number(m[4])<24 && Number(m[5])<60 && Number(m[6])<60;
}
function adsDayScalar_(v,kind) {
  if(kind.indexOf('optional')===0) {
    if(adsDayBlankScalar_(v)) return true;
    kind=kind.slice(8).toLowerCase();
  }
  if(kind==='int') return adsDayIntegerScalar_(v);
  if(kind==='numeric' || kind==='float') return adsDayNumberScalar_(v,kind==='numeric');
  if(kind==='timestamp') return adsDayTimestampScalar_(v);
  if(kind==='day') return typeof v==='string' && adsDayDate_(v);
  if(kind==='label') return (typeof v==='string' && v.trim()!=='') || (typeof v==='number' && Number.isFinite(v));
  return typeof v==='string' && (kind==='text' ? v.trim()!=='' : true);
}
function adsDayValidateScalars_(p) {
  [[p.stats,WB_ADS_DAY_STAT_SCALARS_,'STAT'],[p.boosters,WB_ADS_DAY_BOOSTER_SCALARS_,'BOOSTER']].forEach(function(group) {
    group[0].forEach(function(row) {
      Object.keys(group[1]).forEach(function(field) {
        // The existing no_stats constructor emits empty payload cells, never metric zeros.
        var marker=group[2]==='STAT' && row.processed_status==='no_stats' &&
          ['load_ts','run_id','period_from','period_to','source_method','processed_status','advertId'].indexOf(field)<0;
        adsDayAssert_(marker?adsDayBlankScalar_(row[field]):adsDayScalar_(row[field],group[1][field]),
          'RAW_'+group[2]+'_SCALAR_'+field.toUpperCase());
      });
    });
  });
}
/** Validate the complete flattened payload, never one publication chunk at a time. */
function adsDayValidatePrepared_(p, ordered, runId) {
  var ids=ordered.map(function(r){return r.advertId;}), classified=Object.keys(p.classifications);
  adsDayAssert_(p.failures.length===0 && p.skipped.length===0 && classified.length===ids.length &&
    ids.every(function(id){return ['PROCESSED','NO_STATS'].indexOf(p.classifications[id])>=0;}) &&
    classified.every(function(id){return ids.indexOf(id)>=0;}), 'ADS_PARTIAL');
  var seen=Object.create(null), statIds={};
  p.stats.concat(p.boosters).forEach(function(r) {
    adsDayAssert_(r.run_id===runId && r.period_from===WB_ADS_DAY_REPAIR_DATE_ && r.period_to===WB_ADS_DAY_REPAIR_DATE_ &&
      r.source_method==='adv/v3/fullstats' && ids.indexOf(String(r.advertId))>=0, 'RAW_SCOPE');
    if(r.processed_status==='no_stats') {
      adsDayAssert_(p.stats.indexOf(r)>=0 && p.classifications[String(r.advertId)]==='NO_STATS' && !r.date && !r.nmId && !r.appType, 'MARKER_SCOPE');
    } else adsDayAssert_(r.processed_status==='raw' && adsDayDate_(r.date), 'RAW_DATE_SCOPE');
  });
  p.stats.forEach(function(r) {
    if(r.processed_status!=='raw') return;
    adsDayAssert_(p.classifications[String(r.advertId)]==='PROCESSED' && r.nmId!==undefined && r.nmId!==null && r.nmId!=='' &&
      r.appType!==undefined && r.appType!==null && r.appType!=='' && r.source_level==='nm', 'RAW_KEY_NULL');
    var key=JSON.stringify(['date','advertId','nmId','appType','source_level'].map(function(k){return String(r[k]);}));
    adsDayAssert_(!seen[key], 'RAW_GRAIN_DUPLICATE'); seen[key]=true; statIds[String(r.advertId)]=true;
  });
  adsDayValidateBoosterGrain_(p.boosters);
  ids.forEach(function(id) {
    var markers=p.stats.filter(function(r){return String(r.advertId)===id && r.processed_status==='no_stats';});
    adsDayAssert_(p.classifications[id]==='NO_STATS' ? markers.length===1 && !statIds[id] : markers.length===0 && !!statIds[id], 'RESPONSE_UNRESOLVED');
  });
  adsDayAssert_(WB_ADS_DAY_REPAIR_CONTROLS_.every(function(id){return p.classifications[id]==='PROCESSED' && statIds[id];}), 'CONTROL_ANOMALY');
  adsDayValidateScalars_(p); // Both complete destinations must pass before the first RAW publication.
  adsDayAssert_(p.stats.length>0 && p.stats.length<10000 && p.boosters.length<10000 &&
    adsDayCanonical_(p).length<5000000, 'PAYLOAD_LIMIT');
}
function adsDayExecute_(spec, io) {
  var result={status:'ERROR',logicalDate:WB_ADS_DAY_REPAIR_DATE_,runId:null,origin:WB_ADS_DAY_REPAIR_ORIGIN_,parent:WB_ADS_DAY_REPAIR_PARENT_,controls:[]};
  try {
    adsDayAssert_(spec && spec.logicalDate===WB_ADS_DAY_REPAIR_DATE_ && /^[a-f0-9]{64}$/.test(spec.campaignHash || '') &&
      /^[a-f0-9]{64}$/.test(spec.effectiveViewHash || '') && Object.keys(spec).every(function(k){return ['logicalDate','campaignHash','effectiveViewHash'].indexOf(k)>=0;}), 'REPAIR_SPEC');
    io.preflight(spec);
    var evidence=io.evidence(), ordered=adsDayValidateEvidence_(evidence,spec,io), before=io.baseline();
    adsDayAssert_(before.targetEmpty===true, 'TARGET_ALREADY_PRESENT');
    result.campaignHash=spec.campaignHash; result.evidenceHash=io.hash(adsDayCanonical_(evidence));
    result.billingMaturity={age_ok:evidence.costCoverage.age_ok,stable_ok:evidence.costCoverage.stable_ok,billed_complete:evidence.costCoverage.billed_complete};
    result.runId=io.start();
    adsDayAssert_(result.runId && /^INS_ADS_[A-Za-z0-9_]+$/.test(result.runId) && result.runId!==WB_ADS_DAY_REPAIR_PARENT_, 'NEW_ATTEMPT');
    adsDayAssert_(io.attempt(result.runId)==='STARTED', 'START_READBACK');
    var p=io.prepare(ordered,result.runId);
    result.failures=p.failures; result.skipped=p.skipped;
    result.counts={population:ordered.length,classified:Object.keys(p.classifications).length,
      noStats:Object.keys(p.classifications).filter(function(id){return p.classifications[id]==='NO_STATS';}).length,
      statRows:p.stats.filter(function(r){return r.processed_status==='raw';}).length,boosterRows:p.boosters.length};
    result.controls=WB_ADS_DAY_REPAIR_CONTROLS_.map(function(id) {
      var rows=p.stats.filter(function(r){return String(r.advertId)===id && r.processed_status==='raw';});
      var requests=p.requests.filter(function(r){return r.ids.indexOf(id)>=0;});
      var returned=requests.reduce(function(out,q){return out.concat((q.controls || []).filter(function(c){return c.advertId===id;}));},[]);
      return {advertId:id,inPopulation:true,classification:p.classifications[id] || (p.failures.some(function(f){return String(f.advertId)===id;})?'ERROR':'SKIPPED'),
        requests:requests,responseDetails:returned,responsePresent:p.present.indexOf(id)>=0 || requests.some(function(q){return (q.responseIds || []).indexOf(id)>=0;}),
        returnedDates:Array.from(new Set(rows.map(function(r){return r.date;}))),
        nmIds:Array.from(new Set(rows.map(function(r){return String(r.nmId);}))),appTypes:Array.from(new Set(rows.map(function(r){return String(r.appType);}))),
        rawRows:rows.length,published:false,readback:false,publicationStatus:'NOT_ATTEMPTED'};
    });
    adsDayValidatePrepared_(p,ordered,result.runId);
    adsDayAssert_(result.controls.every(function(r){return r.responsePresent && r.requests.some(function(q){return q.http>=200 && q.http<300 && q.responseIds.indexOf(r.advertId)>=0;});}), 'CONTROL_REQUEST_EVIDENCE');
    // The entire prepared destination has passed date/grain/coverage/control guards BEFORE this first RAW call.
    io.audit(result,'PREPARED');
    result.controls.forEach(function(r){r.published=null;r.readback=null;r.publicationStatus='UNKNOWN';});
    var receipt=io.publish(p,result.runId);
    adsDayAssert_(receipt && receipt.confirmed===true && receipt.readback===true, 'RAW_READBACK');
    result.receipt=receipt;
    result.controls.forEach(function(r){r.published=true;r.readback=true;r.publicationStatus='RAW_VERIFIED';});
    adsDayAssert_(io.validateEffective(before,p,result.runId)===true, 'EFFECTIVE_READBACK');
    var again=io.evidence();
    adsDayAssert_(io.hash(adsDayCanonical_(again))===result.evidenceHash, 'REUSED_EVIDENCE_CHANGED');
    adsDayAssert_(io.attempt(result.runId)==='STARTED', 'ATTEMPT_CONFLICT');
    result.controls.forEach(function(r){r.publicationStatus='EFFECTIVE_VERIFIED';});
    io.audit(result,'VALIDATED'); // All status evidence is durable before COMPLETE; no further Sheet writes on success.
    adsDayAssert_(io.complete(result.runId,p.stats.length+p.boosters.length)===true, 'COMPLETE_UNCONFIRMED');
    adsDayAssert_(io.attempt(result.runId)==='COMPLETE', 'COMPLETE_READBACK');
    result.status='COMPLETE';
  } catch(e) {
    result.error=/^[A-Z][A-Z0-9_]{0,80}$/.test(e.code || '') ? e.code : 'REPAIR_IO_UNKNOWN';
    result.status=result.error==='ADS_PARTIAL'?'PARTIAL':'ERROR';
    if(result.runId && result.runId!==WB_ADS_DAY_REPAIR_PARENT_) {
      try { result.errorRecorded=io.error(result.runId,result.error)===true; } catch(ignored) { result.errorRecorded=false; }
      try { io.audit(result,'ERROR'); } catch(ignored2) { result.auditRecorded=false; }
    }
  }
  return result;
}
