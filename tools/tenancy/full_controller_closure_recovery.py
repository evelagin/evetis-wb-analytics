"""Owner-only recovery of terminal timed-out control ownership before source work.

A control timeout is never source SUCCESS. This reusable, closed contract only
adds a certified control closure and resolves exact dependent gate STOPs after
fresh proof of no new source authority/effects. It does not reclaim SOURCE
leases, retry dispatch, or automatically exempt later failures.
"""
import re
from datetime import timedelta
from tools.tenancy import controller_cadence as CC, durable_plan as D, tenant_backfill as BF
from tools.tenancy import full_history as F
from tools.tenancy.validation import parse_tenant_json as parse

KIND='FULL_CONTROLLER_CLOSURE_RECOVERED'
TYPE='TERMINAL_TIMEOUT_CONTROLLER_PRE_SOURCE_CLOSURE_V1'
PREDICATES=frozenset({'owner_scope','qualified_replacement','registered_terminal_timeout',
 'exact_latest_claim','no_successor_claim','missing_control_closure','frozen_manifest',
 'no_new_source_intent','no_new_source_journal','no_new_runtime','no_pending_source_authority',
 'last_source_reconciled','source_counts_keys_pass','current_catalog_pass','all_jobs_inactive',
 'schedulers_paused','bindings_pass','security_preserved','prior_recoveries_preserved',
 'original_evidence_preserved','exact_guard_reproduced','source_budget_zero'})
FIELDS=frozenset({'version','type','tenant','project','root','manifest_hash','claim',
 'stop_hashes','protected_root_hashes','new_source','new_image','new_implementation_hash',
 'runtime_image','owner_ack_sha256','guard_probe_hash','security_proof_hash','source_proof_hash',
 'last_source','predicates','verified_at','source_budget','closure','hash'})

def fail(reason):raise BF.B.EvidenceError('controller closure recovery: '+reason)
def sealed(p):return dict(p,hash=D.digest(p))
def authority_name(p):return 'BFCC_AUTH_'+p['root']+'_'+p['claim']['hash']
def certificate_name(p):return 'BFCC_'+p['hash']
def value(p):return {'kind':'controller_recovery','root':p['root'][:16]},D.encoded(p)

def header(p,m):
 if not isinstance(p,dict) or set(p)!=FIELDS or type(p['version']) is not int or p['version']!=1 or p['type']!=TYPE:fail('closed certificate required')
 if p['hash']!=D.digest({k:v for k,v in p.items() if k!='hash'}):fail('certificate digest differs')
 if (p['tenant'],p['project'],p['root'],p['manifest_hash'])!=(m['tenant'],m['project'],m['hash'],D.digest(m)):fail('foreign frozen root')
 if type(p['source_budget']) is not int or p['source_budget']!=0:fail('source budget must be zero')
 if not isinstance(p['predicates'],dict) or set(p['predicates'])!=PREDICATES or any(v is not True for v in p['predicates'].values()):fail('unknown mandatory predicate')
 if type(p['claim'].get('version')) is not int or p['claim'].get('version')!=1:fail('claim version differs')
 if set(p['claim'])!=CC.CLAIM_FIELDS or p['claim']['root']!=p['root'] or (p['claim']['tenant'],p['claim']['project'])!=(p['tenant'],p['project']):fail('claim scope differs')
 if p['claim']['hash']!=D.digest({k:v for k,v in p['claim'].items() if k!='hash'}):fail('claim digest differs')
 CC.names(p['root'],p['claim']['generation']);CC.at(p['claim']['created_at'])
 for k in ('stop_hashes','protected_root_hashes'):
  xs=p[k]
  if not isinstance(xs,list) or not 1<=len(xs)<=10000 or xs!=sorted(set(xs)):fail('bounded immutable hash inventory required')
  for h in xs:D.check_hash(h)
 for k in ('hash','root','manifest_hash','owner_ack_sha256','guard_probe_hash','security_proof_hash','source_proof_hash','new_implementation_hash'):D.check_hash(p[k])
 if not re.fullmatch('[0-9a-f]{40}',p['new_source']) or p['new_source']==p['claim']['source']:fail('qualified replacement required')
 if not re.fullmatch('europe-west1-docker.pkg.dev/mpa-platform/mpa-runtime/tenant-backfill-controller@sha256:[0-9a-f]{64}',p['new_image']):fail('immutable replacement required')
 c=p['closure'];expected=CC.sealed(dict(version=1,claim_hash=p['claim']['hash'],status='SOURCE_FREE_RECOVERED',source_dispatches=0,closed_at=p['verified_at']))
 if c!=expected:fail('certified additive control closure differs')
 CC.at(p['verified_at']);return p


def terminal_timeout(b,claim):
 """Read exact historical deployment; never accept arbitrary FAILED runtimes."""
 from tools.tenancy import orchestration_contract as O, cloud_controller as C
 release=parse((BF.REPO/'infra/tenant/releases/backfill'/f"{claim['source']}.json").read_text())
 x=b.request('GET',BF.RUN_API+'/'+claim['execution']);env={e['name']:e.get('value') for e in x['template']['containers'][0]['env']}
 settings=dict(release=claim['source'],root_hash=claim['root'],scheduler_state=env.get('HISTORICAL_SCHEDULER_STATE'))
 marker=b.tables.get_table(b.c['datasets']['tenant_locks'],C.descriptor_name(**dict(source=settings['release'],root=settings['root_hash'],state=settings['scheduler_state'])))
 if not marker or parse(marker[1])!={'settings':settings,'release':release} or release['image']!=claim['image']:fail('original deployment unknown')
 O.verify_job({'template':{'taskCount':x.get('taskCount',1),'template':x['template']}},O.block(b.c,settings,BF.REPO,release))
 conditions=x.get('conditions',[]);completed=[v for v in conditions if v.get('type')=='Completed']
 if (x.get('name')!=claim['execution'] or x.get('failedCount')!=1 or x.get('succeededCount',0)
     or x.get('cancelledCount',0) or x.get('retriedCount',0) or x.get('runningCount',0) or x.get('reconciling',False)
     or not x.get('completionTime') or len(completed)!=1 or completed[0].get('state')!='CONDITION_FAILED'
     or 'The configured timeout was reached.' not in completed[0].get('message','')):fail('unsupported terminal controller result')
 if not CC.at(x['createTime'])<=CC.at(claim['created_at'])<=CC.at(x['completionTime'])<=b.clock():fail('terminal claim interval differs')
 return x


def validate(p,b,m,records,*,final=True):
 from tools.tenancy import full_controller as H, orchestration_contract as O, cloud_controller as C
 header(p,m)
 if H.only_manifest(records)!=m or not set(p['protected_root_hashes'])<={D.digest(r) for r in records}:fail('protected root differs')
 stops=[r for r in records if r['kind']=='STOPPED' and D.digest(r) in p['stop_hashes']]
 if len(stops)!=len(p['stop_hashes']):fail('dependent STOP evidence missing')
 for r in stops:
  payload=r['payload']
  if (set(payload)!={'reason','controller_execution','diagnostic'} or payload['reason']!='CONTROLLER_GATE_OR_EVIDENCE_FAILURE'
      or payload['diagnostic']!={'version':1,'stage':'MANIFEST_ROOT','category':'EVIDENCE'}
      or not payload['controller_execution'].startswith(p['claim']['execution'].rsplit('/',1)[0]+'/tenant-backfill-controller-')):fail('unsupported dependent STOP')
 ds=b.c['datasets']['tenant_locks'];n,close=CC.names(p['root'],p['claim']['generation'])
 if b.tables.get_table(ds,n)!=CC.claim_value(p['claim']):fail('original control claim differs')
 closure_value=b.tables.get_table(ds,close)
 expected=({'kind':'controller_close','root':p['root'][:16]},D.encoded(p['closure']))
 if closure_value is not None and closure_value!=expected:fail('closure conflict')
 if final and closure_value!=expected:fail('certified closure missing')
 marker=b.tables.get_table(ds,C.descriptor_name(p['new_source'],p['root'],'PAUSED'))
 desc=parse(marker[1]) if marker else {};settings=dict(release=p['new_source'],root_hash=p['root'],scheduler_state='PAUSED')
 if set(desc)!={'settings','release'} or desc['settings']!=settings:fail('replacement descriptor absent')
 r=desc['release'];O.block(b.c,settings,BF.REPO,r)
 if (r['source_sha'],r['image'],r['controller_implementation_hash'],r['runtime_image'],r.get('controller_timeout_seconds'),r['verification'].get('terminal_controller_adapter'))!=(p['new_source'],p['new_image'],p['new_implementation_hash'],p['runtime_image'],1800,'PASS'):fail('qualified replacement differs')
 if b.c['orchestration']['job']['env']['CONTROLLER_SOURCE_SHA']==p['new_source']:O.verify_artifact_source(r,BF.REPO)
 elif parse((BF.REPO/'infra/tenant/releases/backfill'/f"{p['new_source']}.json").read_text())!=r:fail('historical replacement differs')
 s=p['last_source']
 if type(s.get('index')) is not int or not 0<=s['index']<len(m['programs']):fail('last source index unknown')
 if set(s)!={'index','shard','receipt_hash','recon_hash','completion_hash'}:fail('closed last-source scope required')
 for k in ('shard','receipt_hash','recon_hash','completion_hash'):D.check_hash(s[k])
 done=H.completed(records,m)
 if s['index'] not in done or done[s['index']]['shard_root']!=s['shard']:fail('last source is not complete')
 completion=[r for r in records if r['kind']=='CHUNK_COMPLETE' and D.digest(r)==s['completion_hash']]
 if len(completion)!=1 or completion[0]['sequence']!=s['index']:fail('last source completion differs')
 sh=b.store.history(s['shard'],max_records=F.MAX_LEAF_RECORDS)
 rr=[r for r in sh if r['kind']=='DISPATCH_RECEIPT' and D.digest(r)==s['receipt_hash']];rc=[r for r in sh if r['kind']=='RECONCILED' and D.digest(r)==s['recon_hash']]
 if len(rr)!=1 or len(rc)!=1 or rr[0]['sequence']!=rc[0]['sequence'] or rc[0]['payload'].get('state_hash')!=done[s['index']]['state_hash'] or rc[0]['payload'].get('persisted_reconciled') is not True:fail('source reconciliation differs')
 return p


def load(b,m,records):
 for r in records:
  if r['kind']==KIND and (r['root_hash']!=m['hash'] or r['sequence']!=r['payload']['claim']['generation']):fail('recovery record scope differs')
 found=[r['payload'] for r in records if r['kind']==KIND]
 if len(found)>100 or len({p['claim']['hash'] for p in found})!=len(found):fail('ambiguous recovery inventory')
 for p in found:
  validate(p,b,m,records)
  if any(b.tables.get_table(ds,n)!=value(p) for ds,n in ((b.c['datasets']['ref'],authority_name(p)),(b.c['datasets']['tenant_locks'],certificate_name(p)))):fail('owner authority/certificate missing')
 return found


def live_freeze(b,p,m,records):
 """Mandatory live checks repeated at publication, not inferred from expiry."""
 from tools.tenancy import orchestration_contract as O, recent_priority as RP, full_controller as H
 from tools.tenancy import full_leaf_recovery as FL,full_cooldown_recovery as FC,full_policy_bootstrap_recovery as PB,full_pretransport_recovery as PT,full_first_receipt_recovery as FR
 if b.current_execution is not None or b.c['orchestration']['job']['env']['HISTORICAL_SCHEDULER_STATE']!='PAUSED':fail('owner frozen observation required')
 ds=b.c['datasets']['tenant_locks'];generation=p['claim']['generation'];prefix='BFCW_'+p['root']+'_'
 found=[]
 for n,labels,created,expiry in b.tables.list_tables(ds,with_expiry=True):
  if n.startswith(prefix):
   suffix=n[len(prefix):]
   if not re.fullmatch('[0-9]{10}',suffix) or expiry:fail('control namespace/retention differs')
   found.append(int(suffix))
 if sorted(found)!=list(range(1,generation+1)):fail('control successor/gap exists')
 existing=b.tables.get_table(ds,CC.names(p['root'],generation)[1])
 expected=({'kind':'controller_close','root':p['root'][:16]},D.encoded(p['closure']))
 if existing is not None and (existing!=expected or b.tables.get_table(ds,certificate_name(p))!=value(p) or b.tables.get_table(b.c['datasets']['ref'],authority_name(p))!=value(p)):fail('original control closure unknown')
 terminal_timeout(b,p['claim'])
 base,_=BF.resources(b.c);ss=b.request('GET',BF.SCHED_API+'/'+base+'/jobs?pageSize=100')
 if len(ss.get('jobs',[]))!=4 or ss.get('nextPageToken') or any(j['state']!='PAUSED' for j in ss['jobs']):fail('Scheduler not frozen')
 for name in (*b.c['marketplaces']['ozon']['jobs'],'tenant-control',O.JOB):
  xs=BF.execution_inventory(b.request,base+'/jobs/'+name)['executions']
  if any(not x.get('completionTime') for x in xs):fail('active execution')
  if name==O.JOB:
   by={x['name']:x for x in xs}
   stop_names=[r['payload']['controller_execution'] for r in records if r['kind']=='STOPPED' and D.digest(r) in p['stop_hashes']]
   old_release=parse((BF.REPO/'infra/tenant/releases/backfill'/f"{p['claim']['source']}.json").read_text())
   original=by.get(p['claim']['execution'])
   if original is None:fail('controller holder not visible')
   for n in stop_names:
    x=by.get(n)
    if x is None or x.get('failedCount')!=1 or x.get('retriedCount',0) or x.get('cancelledCount',0) or CC.at(x['createTime'])<CC.at(original['completionTime']):fail('dependent controller result differs')
    old=O.block(b.c,dict(release=p['claim']['source'],root_hash=p['root'],scheduler_state='ENABLED'),BF.REPO,old_release)
    O.verify_job({'template':{'taskCount':x.get('taskCount',1),'template':x['template']}},old)
  if name!=O.JOB and any(CC.at(x['createTime'])>=CC.at(p['claim']['created_at']) for x in xs):fail('new child/external effect')
 rows=b.select(f"SELECT COUNT(*) AS n FROM `{b.journal}` WHERE started_at>=@start",{'start':('TIMESTAMP',p['claim']['created_at'])})
 if rows!=[{'n':0}]:fail('source journal effect after claim')
 # Inspect election timestamps, including uncommitted intents. No source fence is
 # released, rewritten or treated as expired by this recovery.
 plans=H.reconstruct_plans(records,m,b.clock());shards={x['shard_root'] for x in plans.values()}|{r['payload']['shard_root'] for r in records if r['kind']=='DEPENDENCY_PLAN'}
 for n,labels,created,expiry in b.tables.list_tables(ds,with_expiry=True):
  match=re.fullmatch(r'BFQ_([0-9a-f]{64})_([0-9]{10})_DISPATCH_INTENT',n)
  if match and match[1] in shards and (created is None or created>=CC.at(p['claim']['created_at']) or expiry):fail('new/unknown source election')
 accepted=FL.load(b,m,records)+PB.load(b,m,records)+FR.load(b,m,records)
 failed=FC.load(b,m,records);rejected=PT.load(b,m,records)
 b.verified_cooldown_failures=failed;b.verified_pretransport_rejections=rejected
 accepted+=failed+rejected
 previous=load(b,m,records);known={x['stop_hash'] for x in accepted}|{h for x in previous for h in x['stop_hashes']}
 if {D.digest(r) for r in records if r['kind']=='STOPPED'}-known!=set(p['stop_hashes']):fail('another unresolved incident')
 if RP.pending_receipts(b,plans):fail('unfinished source authority')
 ro=FL.observer(b,m,p['last_source']['index']);H.verify_authority(ro,m);ro.preflight({'hash':m['hash'],'plans':[plans[p['last_source']['index']]['plan']]})
 doc=plans[p['last_source']['index']]['plan'];state=ro.state(doc)
 sh=b.store.history(p['last_source']['shard'],max_records=F.MAX_LEAF_RECORDS)
 rr=next(r for r in sh if D.digest(r)==p['last_source']['receipt_hash']);receipt=rr['payload']['receipt']
 intents=[r for r in sh if r['kind']=='DISPATCH_INTENT' and r['sequence']==rr['sequence']]
 if len(intents)!=1 or any(intents[0]['payload']['preparation'][k]!=receipt[k] for k in ('run_id','lease_generation','ack_hash')):fail('source intent/receipt join differs')
 op=ro.request('GET',BF.RUN_API+'/'+receipt['operation']);x=op.get('response') or {}
 if not op.get('done') or op.get('error') or x.get('succeededCount')!=1 or x.get('failedCount',0) or x.get('retriedCount',0) or x.get('cancelledCount',0) or not x.get('completionTime'):fail('source terminal result unknown')
 task=x['template'];env={e['name']:e.get('value') for e in task['containers'][0]['env']}
 expected_sa=f"{b.c['marketplaces']['ozon']['service_accounts']['runtime']}@{p['project']}.iam.gserviceaccount.com"
 if task['serviceAccount']!=expected_sa or len(task['containers'])!=1 or task['containers'][0]['image']!=BF.execution_image(doc,b.c) or any(env.get(k)!=v for k,v in dict(INGESTION_RUN_ID=receipt['run_id'],TENANT_BINDING_REQUIRED='1',STRICT_PAGE_CAPS='1',BACKFILL_TARGET_PROJECT=p['project']).items()):fail('source identity differs')
 proof=BF.read_proof(b.c,doc,receipt['run_id'],request=ro.request)
 if proof['state']!=state:fail('source aggregate/state mismatch')
 prefix=BF.B.digest(['BOUNDED_PILOT_EXCLUSIVE',p['project']])[:16]
 for n in (BF.CK.lease_name(prefix,receipt['lease_generation']),f"LD_{prefix}_{receipt['lease_generation']:04d}"):
  v=b.tables.get_table(ds,n)
  if not v or v[0].get('owner')!=receipt['run_id'] or parse(v[1]).get('ack_hash')!=doc['ack_hash']:fail('last source lease/LD differs')
 if not state['complete'] or D.digest(state)!=H.completed(records,m)[p['last_source']['index']]['state_hash']:fail('last source checkpoint differs')
 coverage=BF.verify_coverage(doc,doc['ack_hash'],backend=ro)
 if coverage['source_sequence']!=state['sequence'] or any(x.get('rows')!=x.get('keys') for x in coverage['readback']):fail('source persistence/key mismatch')
 day=str(b.clock().astimezone(BF.B.MSK).date())
 certs=[r for r in records if r['kind']=='SNAPSHOT_CERT' and r['payload'].get('day')==day]
 if len(certs)!=1:fail('current Catalog certificate unavailable')
 for x in coverage['readback']:
  if x.get('sku_linkage',{}).get('unresolved_sku_rows',1)!=0:fail('certified last source linkage unknown')
 return True


def publish(b,p,m):
 """Fresh additive owner transaction; no retry or automatic exemption path."""
 header(p,m);records=b.store.history(m['hash']);validate(p,b,m,records,final=False)
 if p in load(b,m,records):return p
 if b.c['orchestration']['job']['env']['CONTROLLER_SOURCE_SHA']!=p['new_source']:fail('replacement not installed')
 age=b.clock()-CC.at(p['verified_at'])
 if age<timedelta(0) or age>timedelta(minutes=15):fail('fresh predicates required')
 if sorted(D.digest(r) for r in records)!=p['protected_root_hashes']:fail('root changed since observation')
 live_freeze(b,p,m,records)
 if b.clock()-CC.at(p['verified_at'])>timedelta(minutes=15):fail('observation exceeded freshness bound')
 # Publish owner evidence before the new closure. Any interruption stays closed
 # through dependent STOPs; no caller may manufacture a successful source run.
 ds=b.c['datasets']['tenant_locks']
 for dataset,n in ((b.c['datasets']['ref'],authority_name(p)),(ds,certificate_name(p))):
  old=b.tables.get_table(dataset,n)
  if old is None:b.tables.create_marker(dataset,n,*value(p))
  if b.tables.get_table(dataset,n)!=value(p):fail('owner publication conflict')
 n=CC.names(p['root'],p['claim']['generation'])[1];v=({'kind':'controller_close','root':p['root'][:16]},D.encoded(p['closure']))
 if not b.tables.create_marker(ds,n,*v) and b.tables.get_table(ds,n)!=v:fail('closure CAS conflict')
 if b.tables.get_table(ds,n)!=v:fail('closure readback conflict')
 b.store.commit(p['root'],KIND,p['claim']['generation'],p,b.clock())
 if p not in load(b,m,b.store.history(m['hash'])):fail('additive recovery acceptance failed')
 return p
