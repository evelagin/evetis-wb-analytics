"""Owner-only zero-effect FAILED settlement and one diagnostic natural attempt.

The legacy timestamp cause is explicitly UNPROVEN. This closes only the owner
accepted empty dispatch, not its business leaf, and never publishes SUCCESS or
RECON. New FAILED attempts, including ordinary read errors, remain stopped.
"""
import re
from datetime import timedelta
from tools.tenancy import durable_plan as D,tenant_backfill as BF,full_history as F
from tools.tenancy.validation import parse_tenant_json as parse

KIND='FULL_FBO_ZERO_EFFECT_FAILED_RECOVERED'
SHARD_KIND='DISPATCH_ZERO_EFFECT_FAILED_CLOSED'
TYPE='OWNER_FBO_ZERO_EFFECT_DIAGNOSTIC_RETRY_V1'
EXACT=dict(root='bb3ceca51ec5add6fe6fa3af8ec3720fbb5d9b07054b84e26d6bbfd6c8824222',
 stop_hash='9efe5811aae5f9f9f7f365b2134b5610d57691accff3523651dfeb1b6155689e',
 execution='projects/mpa-t-client-001/locations/europe-west1/jobs/ozon-runtime-fast/executions/ozon-runtime-fast-x574f',
 controller_execution='projects/mpa-t-client-001/locations/europe-west1/jobs/tenant-backfill-controller/executions/tenant-backfill-controller-2hbzq',
 original_runtime_source='8f7e937d1636844b46df336cbba63fae09eb41c7',
 original_runtime_image='europe-west1-docker.pkg.dev/mpa-platform/mpa-runtime/ozon-runtime@sha256:37dda2748e42fa9923baa5363f209696421806a3f61a45604a4229d291e71ea1',
 shard='d9250b4a1ffbb169986101a3abcdc702ecfb570976a88ea51973d5c9d137ec0a',
 plan_id='8283809c8c37f1606f8ffeae538da66e67ed7d738c2e4b116238c0eea728b9de',
 intent_hash='13c9b1163fd8f3064cc4a95ca03e6e18bf1fda3c4512a6a9e6161da4431cdb20',
 receipt_hash='0c5ccfc59508e89a155a29fcbf4f9c55b5bad19bad9b9220a12350f6d8e52965',
 run_id='bf-87592741-0592-48d4-ba97-7d8ae453ff62',lease_generation=190,index=269,receipt_sequence=1,
 ack_hash='760c4c0372e648ca24794dc66babb56c9b04d118c78ef6ec88ee54eab3f69063')
PREDICATES=frozenset({'exact_original_failed_runtime','exact_stop_intent_receipt','initial_state_sequence_zero',
 'no_successful_source_units','raw_provenance_rows_zero','runtime_bigquery_no_writes','read_only_no_async_effect',
 'no_successor_runtime_or_intent','exact_original_lease_checkpoint','no_later_source_lease',
 'all_jobs_inactive','all_schedulers_paused','bindings_pass','security_metadata_unchanged',
 'strict_typed_timestamp_diagnostics_qualified','new_exact_runtime_controller_qualified',
 'original_bytes_preserved','frozen_root_recent_preserved','previous_certificates_preserved',
 'one_new_attempt_failed_never_retried','explicit_owner_zero_effect_scope','source_budget_zero'})
FIELDS=frozenset(EXACT)|{'version','type','tenant','project','manifest_hash','new_source','new_image',
 'new_implementation_hash','runtime_source','runtime_image','runtime_implementation_hash','owner_decision_sha256',
 'qualification_hash','security_hash','source_effect_hash','protected_root_hashes','protected_shard_hashes',
 'predicates','verified_at','source_budget','state_hash','original_checkpoint_hash','original_lease_hash',
 'closure_hash','legacy_cause','retry_sequence','retry_limit','hash'}

def fail(reason):raise BF.B.EvidenceError('FBO zero-effect settlement: '+reason)
def sealed(p):return dict(p,hash=D.digest(p))
def authority_name(p):return 'BFZE_AUTH_'+p['root']+'_'+p['stop_hash']
def certificate_name(p):return 'BFZE_'+p['hash']
def value(p):return {'kind':'fbo_zero_effect','root':p['root'][:16]},D.encoded(p)
def closure(p):
 return dict(version=D.VERSION,root_hash=p['shard'],kind=SHARD_KIND,sequence=p['receipt_sequence'],
  payload={k:p[k] for k in ('type','stop_hash','intent_hash','receipt_hash','run_id','lease_generation','source_budget','retry_sequence','retry_limit')})
def release_value(p):return {'owner':p['run_id']},D.encoded(dict(terminal='ZERO_EFFECT_FAILED',ack_hash=p['ack_hash'],certificate_hash=p['hash']))
def checkpoint_evidence(p):return dict(mode='BOUNDED_PILOT',zero_effect_certificate=p['hash'],source_budget=0)

def stable_source_evidence(evidence):
 # UNION ALL results have no guaranteed order. Canonicalize row order only;
 # keep every table/count and all other evidence unchanged for fresh comparison.
 return dict(evidence,raw=sorted(evidence['raw'],key=lambda row:row['table_name']))

def header(p,m):
 if (not isinstance(p,dict) or set(p)!=FIELDS or type(p['version']) is not int or p['version']!=1
     or p['type']!=TYPE or p['hash']!=D.digest({k:v for k,v in p.items() if k!='hash'})):fail('closed certificate required')
 if any(p[k]!=v or type(p[k]) is not type(v) for k,v in EXACT.items()):fail('only owner accepted legacy incident supported')
 if (p['tenant'],p['project'],p['root'],p['manifest_hash'])!=(m['tenant'],m['project'],m['hash'],D.digest(m)):fail('foreign manifest')
 if p['source_budget']!=0 or type(p['source_budget']) is not int:fail('source-free only')
 if p['retry_sequence']!=2 or type(p['retry_sequence']) is not int or p['retry_limit']!=1 or type(p['retry_limit']) is not int:fail('one new attempt only')
 if p['legacy_cause']!='UNPROVEN_TIMESTAMP_NOT_RETAINED':fail('no invented historical cause')
 if not isinstance(p['predicates'],dict) or set(p['predicates'])!=PREDICATES or any(x is not True for x in p['predicates'].values()):fail('unknown mandatory predicate')
 for k in FIELDS:
  if k.endswith('_hash') or k.endswith('_sha256') or k in {'hash','root','shard'}:D.check_hash(p[k])
 for k in ('new_source','runtime_source'):
  if not isinstance(p[k],str) or not re.fullmatch('[0-9a-f]{40}',p[k]):fail('immutable source required')
 for k in ('protected_root_hashes','protected_shard_hashes'):
  if not isinstance(p[k],list) or not 1<=len(p[k])<=10000 or p[k]!=sorted(set(p[k])):fail('bounded protected history required')
  for h in p[k]:D.check_hash(h)
 if p['runtime_source']==p['original_runtime_source'] or p['runtime_image']==p['original_runtime_image']:fail('qualified diagnostic replacement required')
 if D.digest(closure(p))!=p['closure_hash']:fail('closure differs')
 F.stamp(p['verified_at']);return p

def verify_shard(p,sh,*,final=True):
 from tools.tenancy import full_leaf_recovery as FL
 i=FL.one(sh,'DISPATCH_INTENT',p['intent_hash']);r=FL.one(sh,'DISPATCH_RECEIPT',p['receipt_hash'])
 doc=i['payload']['plan'];receipt=r['payload']['receipt']
 if (i['root_hash'],i['sequence'],r['sequence'],r['payload']['plan'],doc['ack_hash'],doc['runtime_plan']['plan_id'])!=(p['shard'],1,1,doc,p['ack_hash'],p['plan_id']):fail('original plan/receipt differs')
 if (doc['runtime_plan']['entity'],doc['runtime_plan']['from'],doc['runtime_plan']['to'])!=('fbo_postings','2026-08-19','2026-08-25'):fail('frozen FBO window differs')
 if any(receipt[k]!=p[k] or i['payload']['preparation'][k]!=p[k] for k in ('run_id','lease_generation','ack_hash')):fail('original dispatch differs')
 if any(r['kind']=='RECONCILED' and r['sequence']==1 for r in sh):fail('failed original must never become RECON')
 if not set(p['protected_shard_hashes'])<={D.digest(x) for x in sh}:fail('original shard bytes changed')
 if final and closure(p) not in sh:fail('closure missing')
 return doc,receipt

def validate(p,b,m,records,*,final=True):
 from tools.tenancy import full_controller as H,cloud_controller as C,orchestration_contract as O,full_pretransport_recovery as PT
 header(p,m)
 if H.only_manifest(records)!=m or not set(p['protected_root_hashes'])<={D.digest(r) for r in records}:fail('original root differs')
 stops=[r for r in records if r['kind']=='STOPPED' and D.digest(r)==p['stop_hash']]
 if len(stops)!=1 or stops[0]['payload']!={'reason':'CONTROLLER_GATE_OR_EVIDENCE_FAILURE','controller_execution':p['controller_execution'],
     'diagnostic':{'version':1,'stage':'RUNTIME_TERMINAL','category':'EVIDENCE','context':dict(index=p['index'],shard=p['shard'],sequence=p['receipt_sequence'],intent_hash=p['intent_hash'],receipt_hash=p['receipt_hash'])}}:fail('original STOP differs')
 sh=b.store.history(p['shard'],max_records=F.MAX_LEAF_RECORDS);doc,receipt=verify_shard(p,sh,final=final)
 if not any(r['kind']=='CHUNK_PLAN' and r['sequence']==p['index'] and r['payload']['plan']==doc and r['payload']['shard_root']==p['shard'] for r in records):fail('frozen leaf absent')
 lease=b.tables.get_table(b.c['datasets']['tenant_locks'],BF.CK.lease_name('50c5d90dbf43ac9c',190))
 if not lease or D.digest(lease)!=p['original_lease_hash']:fail('original lease changed')
 rows=b.select(f"SELECT DISTINCT status,lease_owner,error_code,evidence_json FROM `{p['project']}.tenant_ops.BACKFILL_CHECKPOINTS` WHERE entity='fbo_postings' AND plan_hash=@ack AND lease_generation=190 LIMIT 3",{'ack':('STRING',p['ack_hash'])})
 original=dict(status='RUNNING',lease_owner=p['run_id'],error_code=None,evidence_json='{"mode": "BOUNDED_PILOT", "proof": null}')
 if D.digest(original)!=p['original_checkpoint_hash'] or original not in rows:fail('original checkpoint bytes changed')
 failed=dict(status='FAILED',lease_owner=p['run_id'],error_code='ZERO_EFFECT_FBO_FAILED',evidence_json=D.encoded(checkpoint_evidence(p)))
 if len(rows)>2 or any(PT.checkpoint_row(r) not in (PT.checkpoint_row(original),failed) for r in rows):fail('unexpected checkpoint')
 if final and (not any(PT.checkpoint_row(r)==failed for r in rows) or b.tables.get_table(b.c['datasets']['tenant_locks'],'LD_50c5d90dbf43ac9c_0190')!=release_value(p)):fail('additive failed closure absent')
 settings=dict(release=p['new_source'],root_hash=p['root'],scheduler_state='PAUSED')
 marker=b.tables.get_table(b.c['datasets']['tenant_locks'],C.descriptor_name(p['new_source'],p['root'],'PAUSED'))
 desc=parse(marker[1]) if marker else {}
 if set(desc)!={'settings','release'} or desc['settings']!=settings:fail('qualified descriptor missing')
 r=desc['release']
 if (r['source_sha'],r['image'],r['controller_implementation_hash'],r['runtime_image'],r['runtime_implementation_hash'])!=(p['new_source'],p['new_image'],p['new_implementation_hash'],p['runtime_image'],p['runtime_implementation_hash']):fail('replacement qualification differs')
 if b.c['orchestration']['job']['env']['CONTROLLER_SOURCE_SHA']==p['new_source']:O.verify_artifact_source(r,BF.REPO)
 else:
  if parse((BF.REPO/'infra/tenant/releases/backfill'/f"{p['new_source']}.json").read_text())!=r:fail('historical qualification differs')
 O.block(O.historical_contract(b.c,r,m),settings,BF.REPO,r)
 return doc,receipt

def load(b,m,records):
 found=[r for r in records if r['kind']==KIND]
 if len(found)>1:fail('single owner incident only')
 result=[]
 for r in found:
  p=r['payload'];validate(p,b,m,records)
  if r['root_hash']!=p['root'] or r['sequence']!=p['index']:fail('recovery root record differs')
  if any(b.tables.get_table(ds,n)!=value(p) for ds,n in ((b.c['datasets']['ref'],authority_name(p)),(b.c['datasets']['tenant_locks'],certificate_name(p)))):fail('owner certificate absent')
  result.append(p)
 return result

def protected_retry(b,doc,receipt):
 """Every terminal FAILED successor requires forensic review, even HTTP429."""
 return any(p['plan_id']==doc['runtime_plan']['plan_id'] and receipt['run_id']!=p['run_id']
     and receipt['lease_generation']>p['lease_generation'] for p in getattr(b,'verified_fbo_zero_effect_failures',[]))

def observe(b,m,*,owner_decision_sha256,qualification_hash,security_hash):
 """Fresh metadata/SELECT-only proof. No Ozon calls or business-state writes."""
 from tools.tenancy import full_controller as H,full_leaf_recovery as FL,orchestration_contract as O,full_runtime_handoff as FH,recent_priority as RP
 records=b.store.history(m['hash']);sh=b.store.history(EXACT['shard'],max_records=F.MAX_LEAF_RECORDS)
 # Use the exact original document, not the replacement runtime's mutable state.
 intent=FL.one(sh,'DISPATCH_INTENT',EXACT['intent_hash']);doc=intent['payload']['plan']
 receipt=FL.one(sh,'DISPATCH_RECEIPT',EXACT['receipt_hash'])['payload']['receipt']
 if any(r['sequence']>1 or r['kind']=='RECONCILED' for r in sh):fail('successor/reconciliation exists')
 if b.current_execution is not None or b.c['orchestration']['job']['env']['HISTORICAL_SCHEDULER_STATE']!='PAUSED':fail('inactive owner freeze required')
 observer=FL.observer(b,m,269);H.verify_authority(observer,m);observer.preflight({'hash':m['hash'],'plans':[doc]})
 priority=RP.load(b,m)
 if len(priority['selection'])!=69 or len(priority['parents'])!=64:fail('exact RECENT selection differs')
 base,_=BF.resources(b.c)
 sched=b.request('GET',BF.SCHED_API+'/'+base+'/jobs?pageSize=100')
 if len(sched.get('jobs',[]))!=4 or sched.get('nextPageToken') or any(x['state']!='PAUSED' for x in sched['jobs']):fail('all Scheduler jobs must be paused')
 x=b.request('GET',BF.RUN_API+'/'+EXACT['execution']);t=x['template'];container=t['containers'][0]
 env={v['name']:v.get('value') for v in container['env']};rp=doc['runtime_plan']
 authority=dict(root=m['hash'],index=269,shard=EXACT['shard'],receipt_sequence=1,plan_id=EXACT['plan_id'])
 expected=dict(INGESTION_RUN_ID=EXACT['run_id'],ENTITIES='fbo_postings',SINCE=rp['from'],UNTIL=rp['to'],
  BACKFILL_MODE=BF.B.VERSION,BACKFILL_GENERATION=rp['generation'],BACKFILL_ORIGIN=rp['origin'],
  BACKFILL_TARGET_PROJECT=rp['project'],BACKFILL_WINDOW_DAYS='7',BACKFILL_MAX_REQUESTS='400',BACKFILL_MAX_UNITS='20',
  TENANT_BINDING_REQUIRED='1',STRICT_PAGE_CAPS='1',BACKFILL_FULL_AUTHORITY=D.encoded(authority))
 expected.update(BF.continuation_overrides(doc))
 if (x.get('name')!=EXACT['execution'] or len(t['containers'])!=1 or len(env)!=len(container['env'])
     or any(env.get(k)!=v for k,v in expected.items()) or container['image']!=EXACT['original_runtime_image']
     or t['serviceAccount']!='sa-ozon-runtime@mpa-t-client-001.iam.gserviceaccount.com'
     or not x.get('completionTime') or x.get('failedCount')!=1 or x.get('succeededCount',0)
     or x.get('cancelledCount',0) or x.get('retriedCount',0) or x.get('runningCount',0) or x.get('taskCount')!=1):fail('original exact failed runtime differs')
 op=b.request('GET',BF.RUN_API+'/'+receipt['operation'])
 if op.get('done') is not True or not op.get('error') or op.get('metadata',{}).get('name')!=EXACT['execution']:fail('failed operation differs')
 inventories={}
 for name in (*b.c['marketplaces']['ozon']['jobs'],'tenant-control',O.JOB):
  xs=BF.execution_inventory(b.request,base+'/jobs/'+name)['executions'];inventories[name]=xs
  if any(not z.get('completionTime') or z.get('runningCount',0) for z in xs):fail('active execution')
  if name!=O.JOB and any(F.stamp(z['createTime'])>=F.stamp(x['createTime']) and z['name']!=EXACT['execution'] for z in xs):fail('successor runtime/control effect')
 rows=b.select(f"SELECT ingestion_run_id,status,entity,backfill_sequence,evidence_json,error_message FROM `{b.journal}` WHERE backfill_plan_id=@pid OR ingestion_run_id=@run OR STARTS_WITH(ingestion_run_id,CONCAT(@run,'-u')) LIMIT 1000",{'pid':('STRING',rp['plan_id']),'run':('STRING',EXACT['run_id'])})
 if len(rows)!=1 or tuple(rows[0][k] for k in ('ingestion_run_id','status','entity','backfill_sequence','error_message'))!=(EXACT['run_id'],'FAILED','fbo_postings',None,"EvidenceError('FBO record outside requested UTC interval')"):fail('original journal/unit effects differ')
 state=parse(rows[0]['evidence_json'])['state']
 if state!=BF.B.initial(rp) or b.state(doc)!=state:fail('source state changed')
 columns=b.select(f"SELECT table_name,column_name FROM `{m['project']}.ozon_raw.INFORMATION_SCHEMA.COLUMNS` WHERE STARTS_WITH(table_name,'RAW_') AND column_name IN ('ingestion_run_id','run_id') ORDER BY table_name,column_name",{})
 if len(columns)!=14 or any(not re.fullmatch(r'RAW_[A-Z0-9_]+',z['table_name']) or z['column_name'] not in {'ingestion_run_id','run_id'} for z in columns):fail('RAW provenance inventory differs')
 sql=' UNION ALL '.join(f"SELECT '{z['table_name']}' AS table_name,COUNT(*) AS n FROM `{m['project']}.ozon_raw.{z['table_name']}` WHERE {z['column_name']}=@run OR STARTS_WITH({z['column_name']},CONCAT(@run,'-u'))" for z in columns)
 raw=b.select(sql,{'run':('STRING',EXACT['run_id'])})
 if len(raw)!=14 or {z['table_name'] for z in raw}!={z['table_name'] for z in columns} or any(type(z['n']) is not int or z['n']!=0 for z in raw):fail('business source effects not zero')
 start=F.stamp(x['startTime'])-timedelta(seconds=5);end=F.stamp(x['completionTime'])+timedelta(seconds=5)
 jl=b.request('GET',BF.TT.BQ+'/projects/'+m['project']+'/jobs?allUsers=true&projection=full&maxResults=1000&minCreationTime='+str(int(start.timestamp()*1000))+'&maxCreationTime='+str(int(end.timestamp()*1000)))
 if jl.get('nextPageToken'):fail('BQ job effects inventory truncated')
 jobs=[]
 for j in jl.get('jobs',[]):
  if j.get('user_email')!=t['serviceAccount']:continue
  ref=j['jobReference'];job=b.request('GET',BF.TT.BQ+'/projects/'+m['project']+'/jobs/'+ref['jobId']+'?location='+ref.get('location','EU'))
  if job.get('status',{}).get('state')!='DONE' or job['status'].get('errorResult') or job.get('statistics',{}).get('query',{}).get('statementType')!='SELECT' or set(job['configuration'])-{'query','jobType'}:fail('runtime BQ write/effect not excluded')
  jobs.append(job)
 if len(jobs)!=1:fail('complete original runtime SELECT proof required')
 # The preserved reviewed policy must describe both READ calls as effect-free.
 import seller_policy as SP
 policy=parse((BF.REPO/'pipelines/ozon/runtime/seller_method_policy.json').read_text());SP.validate(policy)
 routes=policy['methods']
 if any(routes.get(path,{}).get('semantics')!='READ' or routes[path].get('side_effects')!='NONE' for path in ('/v3/posting/fbo/list','/v1/seller/info')):fail('unsupported external side-effect contract')
 ds=b.c['datasets']['tenant_locks'];lease=b.tables.get_table(ds,'L_50c5d90dbf43ac9c_0190')
 if not lease or lease[0].get('owner')!=EXACT['run_id'] or parse(lease[1]).get('ack_hash')!=EXACT['ack_hash'] or b.tables.get_table(ds,'LD_50c5d90dbf43ac9c_0190') is not None:fail('original lease/release differs')
 for n,*_ in b.tables.list_tables(ds):
  match=BF.CK.LEASE_RE.match(n)
  if match and match[1]=='50c5d90dbf43ac9c' and int(match[2])>190:fail('later source lease')
 cp=b.select(f"SELECT DISTINCT status,lease_owner,error_code,evidence_json FROM `{m['project']}.tenant_ops.BACKFILL_CHECKPOINTS` WHERE entity='fbo_postings' AND plan_hash=@ack AND lease_generation=190 LIMIT 3",{'ack':('STRING',EXACT['ack_hash'])})
 if cp!=[dict(status='RUNNING',lease_owner=EXACT['run_id'],error_code=None,evidence_json='{"mode": "BOUNDED_PILOT", "proof": null}')]:fail('original checkpoint changed')
 from tools.tenancy import full_cooldown_recovery as FC,full_policy_bootstrap_recovery as PB,full_pretransport_recovery as PT,full_first_receipt_recovery as FR,full_controller_closure_recovery as CR
 accepted=FL.load(b,m,records)+FC.load(b,m,records)+PB.load(b,m,records)+PT.load(b,m,records)+FR.load(b,m,records)
 covered={z['stop_hash'] for z in accepted}|{h for z in CR.load(b,m,records) for h in z['stop_hashes']}
 if {D.digest(z) for z in records if z['kind']=='STOPPED'}-covered!={EXACT['stop_hash']}:fail('another unresolved incident')
 source=b.c['orchestration']['job']['env']['CONTROLLER_SOURCE_SHA'];release=parse((BF.REPO/'infra/tenant/releases/backfill'/f'{source}.json').read_text());O.verify_artifact_source(release,BF.REPO)
 runtime=FH.runtime_facts(b.c,m);rr=FH.release_for(b.c)
 if release.get('fbo_zero_effect_recovery_version')!=1 or release['verification'].get('fbo_zero_effect_adapter')!='PASS':fail('diagnostic controller adapter unqualified')
 if rr['verification']['built_artifact'].get('fbo_window_typed_diagnostics')!='PASS':fail('diagnostic exact image unqualified')
 evidence=stable_source_evidence(dict(raw=raw,journal=rows,jobs=jobs,lease=lease,checkpoint=cp,state=state,priority_policy_hash=priority['hash']))
 p=dict(EXACT,version=1,type=TYPE,tenant=m['tenant'],project=m['project'],manifest_hash=D.digest(m),
  new_source=source,new_image=release['image'],new_implementation_hash=release['controller_implementation_hash'],**runtime,
  owner_decision_sha256=owner_decision_sha256,qualification_hash=qualification_hash,security_hash=security_hash,
  source_effect_hash=D.digest(evidence),protected_root_hashes=sorted(D.digest(z) for z in records),protected_shard_hashes=sorted(D.digest(z) for z in sh),
  predicates={k:True for k in PREDICATES},verified_at=b.clock().isoformat(),source_budget=0,state_hash=D.digest(state),
  original_checkpoint_hash=D.digest(cp[0]),original_lease_hash=D.digest(lease),closure_hash='0'*64,
  legacy_cause='UNPROVEN_TIMESTAMP_NOT_RETAINED',retry_sequence=2,retry_limit=1)
 p['closure_hash']=D.digest(closure(p));p=sealed(p);validate(p,b,m,records,final=False)
 return p,evidence

def publish(b,p,m):
 """Owner append only, after independently qualified code and fresh zero effects."""
 header(p,m);records=b.store.history(m['hash'])
 if load(b,m,records):return load(b,m,records)[0]
 validate(p,b,m,records,final=False)
 age=b.clock()-F.stamp(p['verified_at'])
 if age<timedelta(0) or age>timedelta(minutes=15):fail('fresh proof required')
 fresh,evidence=observe(b,m,owner_decision_sha256=p['owner_decision_sha256'],qualification_hash=p['qualification_hash'],security_hash=p['security_hash'])
 if any(fresh[k]!=p[k] for k in FIELDS-{'hash','verified_at'}):fail('fresh mandatory evidence changed')
 for ds,n in ((b.c['datasets']['ref'],authority_name(p)),(b.c['datasets']['tenant_locks'],certificate_name(p))):
  old=b.tables.get_table(ds,n)
  if old is None:b.tables.create_marker(ds,n,*value(p))
  if b.tables.get_table(ds,n)!=value(p):fail('owner publication conflict')
 doc,_=verify_shard(p,b.store.history(p['shard']),final=False)
 row=BF.checkpoint(doc,p['run_id'],'FAILED',190,b.clock());row['error_code']='ZERO_EFFECT_FBO_FAILED';row['evidence_json']=D.encoded(checkpoint_evidence(p))
 b.tables.append(b.c['datasets']['tenant_ops'],'BACKFILL_CHECKPOINTS',[row])
 ds=b.c['datasets']['tenant_locks'];b.tables.create_marker(ds,'LD_50c5d90dbf43ac9c_0190',*release_value(p))
 if b.tables.get_table(ds,'LD_50c5d90dbf43ac9c_0190')!=release_value(p):fail('failed closure release conflict')
 z=closure(p);b.store.commit(p['shard'],SHARD_KIND,1,z['payload'],b.clock());b.store.commit(p['root'],KIND,269,p,b.clock())
 if load(b,m,b.store.history(m['hash']))!=[p]:fail('independent additive acceptance differs')
 return p
