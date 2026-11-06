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
RECONCILED_TYPE='TERMINAL_TIMEOUT_CONTROLLER_RECONCILED_SOURCE_CLOSURE_V2'
PREDICATES=frozenset({'owner_scope','qualified_replacement','registered_terminal_timeout',
 'exact_latest_claim','no_successor_claim','missing_control_closure','frozen_manifest',
 'no_new_source_intent','no_new_source_journal','no_new_runtime','no_pending_source_authority',
 'last_source_reconciled','source_counts_keys_pass','current_catalog_pass','all_jobs_inactive',
 'schedulers_paused','bindings_pass','security_preserved','prior_recoveries_preserved',
 'original_evidence_preserved','exact_guard_reproduced','source_budget_zero'})
RECONCILED_PREDICATES=(PREDICATES-{'current_catalog_pass'})|{'accepted_catalog_lineage_pass'}
# v3: the holder timed out after a reconciled INTERMEDIATE page of a paged leaf (supplies).
# The leaf is not complete and is never declared complete; the last reconciled
# sequence, its receipt and its persisted prefix are proven instead.
SEQUENCE_TYPE='TERMINAL_TIMEOUT_CONTROLLER_RECONCILED_SEQUENCE_CLOSURE_V3'
SEQUENCE_PREDICATES=(PREDICATES-{'current_catalog_pass','last_source_reconciled'})|{
 'last_sequence_reconciled','intermediate_page_not_complete','no_later_source_election'}
# v4: the holder FAILED (not a timeout) before its decision closure while a page elected
# and receipted under the PREVIOUS, closed claim awaited reconciliation (Heldi STOP
# aaf68ede, 2026-10-25: an untyped transport exception). The holder wrote nothing but its
# claim; the pending receipt is preserved for the ordinary reconcile and never settled here.
PENDING_TYPE='TERMINAL_FAILED_CONTROLLER_PENDING_RECEIPT_CLOSURE_V4'
# Counts/keys of the pending page are proven by its ordinary reconcile, not here; after the claim
# the source journal holds only the pending run's own units (2026-10-25: u1967 one second later).
PENDING_PREDICATES=(PREDICATES-{'registered_terminal_timeout','current_catalog_pass','last_source_reconciled','no_pending_source_authority',
 'source_counts_keys_pass','no_new_source_journal'})|{
 'registered_terminal_failure','no_holder_effect_after_claim','pending_receipt_before_claim','pending_runtime_succeeded','pending_receipt_preserved',
 'pending_source_proof_read','source_journal_only_pending_run'}
VERSIONS={(1,TYPE):PREDICATES,(2,RECONCILED_TYPE):RECONCILED_PREDICATES,(3,SEQUENCE_TYPE):SEQUENCE_PREDICATES,(4,PENDING_TYPE):PENDING_PREDICATES}
# Every tenant_locks name the controller identity may create (cloud_access append boundary);
# owner release markers never use them.
CONTROLLER_LOCK=re.compile(r'(BFQ_[0-9a-f]{64}_[0-9]{10}_[A-Z_]+|BFR_[0-9a-f]{64}|LD?_[0-9a-f]{16}_[0-9]{4,10}|BFC[WX]_[0-9a-f]{64}_[0-9]{10}'
 r'|(?:BFFLR|BFFRF)_[0-9a-f]{64}_[0-9a-f]{64}|BFD[CO]_[0-9a-f]{64}|BFLQ_[0-9a-f]{64})')
FAILED_MESSAGE=re.compile(r'Task tenant-backfill-controller-[a-z0-9]{5}-task0 failed with exit code: 1 and message: The container exited with an error\.')
FIELDS=frozenset({'version','type','tenant','project','root','manifest_hash','claim',
 'stop_hashes','protected_root_hashes','new_source','new_image','new_implementation_hash',
 'runtime_image','owner_ack_sha256','guard_probe_hash','security_proof_hash','source_proof_hash',
 'last_source','predicates','verified_at','source_budget','closure','hash'})

def fail(reason):raise BF.B.EvidenceError('controller closure recovery: '+reason)
def sealed(p):return dict(p,hash=D.digest(p))
def authority_name(p):return 'BFCC_AUTH_'+p['root']+'_'+p['claim']['hash']
def certificate_name(p):return 'BFCC_'+p['hash']
def value(p):
 description=D.encoded(p)
 if p.get('version') in (2,3,4):
  # The full closed proof remains in its content-addressed durable root record.
  # Owner metadata binds that exact proof, without truncation or a second copy
  # of the growing root inventory in a 16-KiB table description.
  description=D.encoded(dict(version=p['version'],type='CONTROLLER_CLOSURE_CERTIFICATE_REF',root=p['root'],claim_hash=p['claim']['hash'],certificate_hash=p['hash'],source_budget=0))
 return {'kind':'controller_recovery','root':p['root'][:16]},description

def header(p,m):
 if not isinstance(p,dict) or set(p)!=FIELDS or type(p['version']) is not int or (p['version'],p['type']) not in VERSIONS:fail('closed certificate required')
 if p['hash']!=D.digest({k:v for k,v in p.items() if k!='hash'}):fail('certificate digest differs')
 if (p['tenant'],p['project'],p['root'],p['manifest_hash'])!=(m['tenant'],m['project'],m['hash'],D.digest(m)):fail('foreign frozen root')
 if type(p['source_budget']) is not int or p['source_budget']!=0:fail('source budget must be zero')
 required=VERSIONS[(p['version'],p['type'])]
 if not isinstance(p['predicates'],dict) or set(p['predicates'])!=required or any(v is not True for v in p['predicates'].values()):fail('unknown mandatory predicate')
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


def terminal_timeout(b,claim):return terminal_holder(b,claim,timeout=True)


def terminal_holder(b,claim,*,timeout):
 """Read exact historical deployment; never accept arbitrary FAILED runtimes.

 timeout=False (v4) accepts only the platform's exit-code-1 container failure of the
 exact holder task; a timeout, signal, OOM or any other result stays unsupported."""
 from tools.tenancy import orchestration_contract as O, cloud_controller as C
 release=parse((BF.REPO/'infra/tenant/releases/backfill'/f"{claim['source']}.json").read_text())
 x=b.request('GET',BF.RUN_API+'/'+claim['execution']);env={e['name']:e.get('value') for e in x['template']['containers'][0]['env']}
 settings=dict(release=claim['source'],root_hash=claim['root'],scheduler_state=env.get('HISTORICAL_SCHEDULER_STATE'))
 marker=b.tables.get_table(b.c['datasets']['tenant_locks'],C.descriptor_name(**dict(source=settings['release'],root=settings['root_hash'],state=settings['scheduler_state'])))
 if not marker or parse(marker[1])!={'settings':settings,'release':release} or release['image']!=claim['image']:fail('original deployment unknown')
 O.verify_job({'template':{'taskCount':x.get('taskCount',1),'template':x['template']}},O.block(O.historical_contract(b.c,release),settings,BF.REPO,release))
 conditions=x.get('conditions',[]);completed=[v for v in conditions if v.get('type')=='Completed']
 if (x.get('name')!=claim['execution'] or x.get('failedCount')!=1 or x.get('succeededCount',0)
     or x.get('cancelledCount',0) or x.get('retriedCount',0) or x.get('runningCount',0) or x.get('reconciling',False)
     or not x.get('completionTime') or len(completed)!=1 or completed[0].get('state')!='CONDITION_FAILED'
     or (timeout and 'The configured timeout was reached.' not in completed[0].get('message',''))
     or (not timeout and (not FAILED_MESSAGE.fullmatch(completed[0].get('message',''))
                          or not completed[0]['message'].startswith('Task '+claim['execution'].rsplit('/',1)[1]+'-task0 ')))):fail('unsupported terminal controller result')
 if not CC.at(x['createTime'])<=CC.at(claim['created_at'])<=CC.at(x['completionTime'])<=b.clock():fail('terminal claim interval differs')
 return x


def dependent_terminal(b,p,record,x,holder):
 """An overlapping wake may fail after the holder becomes terminal.

 Creation is admission time, not the gate decision time. Only the reconciled
 v2 owner contract can additionally prove that decision from the exact retained
 STOP row. Missing/ambiguous chronology still blocks; v1 is unchanged.
 """
 if (x is None or x.get('failedCount')!=1 or x.get('retriedCount',0)
     or x.get('cancelledCount',0)):fail('dependent controller result differs')
 terminal=CC.at(holder['completionTime'])
 if CC.at(x['createTime'])>=terminal:return True
 if p['version']<2:fail('dependent controller result differs')
 if not x.get('startTime') or not x.get('completionTime'):fail('dependent STOP chronology unknown')
 created,start,end=(CC.at(x[k]) for k in ('createTime','startTime','completionTime'))
 if not created<=start<=end<=b.clock():fail('dependent STOP chronology differs')
 h=D.digest(record)
 rows=b.select(f"SELECT DISTINCT evidence_json,FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E6SZ',updated_at,'UTC') AS recorded_at FROM `{p['project']}.tenant_ops.BACKFILL_CHECKPOINTS` WHERE entity='orchestration' AND plan_hash=@root AND backfill_id=@id LIMIT 2",{'root':('STRING',p['root']),'id':('STRING',h[:16])})
 if len(rows)!=1 or parse(rows[0]['evidence_json'])!=record:fail('exact dependent STOP event unavailable/ambiguous')
 event=CC.at(rows[0]['recorded_at'])
 if not max(terminal,start)<=event<=end:fail('dependent STOP predates terminal holder/outside execution')
 return True


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
 r=desc['release'];O.block(O.historical_contract(b.c,r,m),settings,BF.REPO,r)
 if (r['source_sha'],r['image'],r['controller_implementation_hash'],r['runtime_image'],r.get('controller_timeout_seconds'),r['verification'].get('terminal_controller_adapter'))!=(p['new_source'],p['new_image'],p['new_implementation_hash'],p['runtime_image'],1800,'PASS'):fail('qualified replacement differs')
 if b.c['orchestration']['job']['env']['CONTROLLER_SOURCE_SHA']==p['new_source']:O.verify_artifact_source(r,BF.REPO)
 elif parse((BF.REPO/'infra/tenant/releases/backfill'/f"{p['new_source']}.json").read_text())!=r:fail('historical replacement differs')
 s=p['last_source']
 if type(s.get('index')) is not int or not 0<=s['index']<len(m['programs']):fail('last source index unknown')
 if p['version']==3:return validate_sequence(p,b,m,records,s)
 if p['version']==4:return validate_pending(p,b,m,records,s)
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


def validate_sequence(p,b,m,records,s):
 """Immutable facts only (re-run on every wake): the claimed receipt and its
 RECONCILED record of the same sequence exist in the leaf's own shard, persisted.
 Leaf incompleteness and the absence of a later election are publication-time
 predicates (live_freeze): later wakes legitimately continue the same leaf."""
 if set(s)!={'index','shard','receipt_hash','recon_hash','sequence'} or type(s['sequence']) is not int or s['sequence']<1:fail('closed sequence scope required')
 for k in ('shard','receipt_hash','recon_hash'):D.check_hash(s[k])
 # The exact committed leaf plan, not the plan current at b.clock(): a dated leaf's
 # later rollover must not invalidate an accepted certificate on every wake.
 owned=[r for r in records if r['kind']=='CHUNK_PLAN' and r['sequence']==s['index'] and r['payload'].get('shard_root')==s['shard']]
 if len(owned)!=1:fail('last source shard differs')
 sh=b.store.history(s['shard'],max_records=F.MAX_LEAF_RECORDS)
 rr=[r for r in sh if r['kind']=='DISPATCH_RECEIPT' and D.digest(r)==s['receipt_hash']];rc=[r for r in sh if r['kind']=='RECONCILED' and D.digest(r)==s['recon_hash']]
 if (len(rr)!=1 or len(rc)!=1 or rr[0]['sequence']!=s['sequence'] or rc[0]['sequence']!=s['sequence']
     or rc[0]['payload'].get('persisted_reconciled') is not True):fail('source sequence reconciliation differs')
 return p


def validate_pending(p,b,m,records,s):
 """Immutable v4 facts only (re-run on every wake): the pending page's intent and
 receipt of one sequence exist in the leaf's own committed shard. Its later ordinary
 RECONCILED record, a later page or the leaf's completion never invalidate them;
 that the page was pending and preceded the claim is a publication predicate."""
 if set(s)!={'index','shard','intent_hash','receipt_hash','sequence'} or type(s['sequence']) is not int or s['sequence']<1:fail('closed pending scope required')
 for k in ('shard','intent_hash','receipt_hash'):D.check_hash(s[k])
 owned=[r for r in records if r['kind']=='CHUNK_PLAN' and r['sequence']==s['index'] and r['payload'].get('shard_root')==s['shard']]
 if len(owned)!=1:fail('pending source shard differs')
 sh=b.store.history(s['shard'],max_records=F.MAX_LEAF_RECORDS)
 ri=[r for r in sh if r['kind']=='DISPATCH_INTENT' and D.digest(r)==s['intent_hash']];rr=[r for r in sh if r['kind']=='DISPATCH_RECEIPT' and D.digest(r)==s['receipt_hash']]
 if len(ri)!=1 or len(rr)!=1 or ri[0]['sequence']!=s['sequence'] or rr[0]['sequence']!=s['sequence']:fail('pending source intent/receipt differs')
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


def accepted_incidents(b,m,records):
 """Revalidate every supported exact incident; quarantine is never COMPLETE.

 The shared FULL STOP gate set (stop_acceptance): a private list here omitted
 STOP819, f507, R1 and R2 and would have called those accepted STOPs foreign.
 """
 from tools.tenancy import full_controller as H, full_fbo_diagnostic_closure as LQ, stop_acceptance as SA
 quarantines=LQ.load(b,m,records)
 known=SA.accepted_hashes(b,m,records,quarantines=quarantines)
 return known,H.isolated_indices(m,quarantines)


def accepted_catalog_lineage(b,m,records,day):
 """Retained source/DQ evidence only; this never certifies current freshness."""
 from tools.tenancy import full_controller as H
 certs=[r['payload'] for r in records if r['kind']=='SNAPSHOT_CERT' and r['payload'].get('day')==day]
 if len(certs)!=1:fail('accepted Catalog lineage unavailable/ambiguous')
 cert=certs[0]
 if CC.at(day+'T00:00:00+03:00')>b.clock():fail('Catalog lineage in future')
 items=[r['payload']['plan'] for r in records if r['kind'] in {'CHUNK_PLAN','DEPENDENCY_PLAN'} and r['payload']['plan']['ack_hash']==cert.get('plan_hash')]
 if not items or any(d!=items[0] for d in items):fail('accepted Catalog scope ambiguous')
 doc=items[0];state=b.state(doc)
 if doc['runtime_plan'].get('observation_date')!=day or doc['runtime_plan']['entity']!='catalog' or state['complete'] is not True or cert['coverage']['source_sequence']!=state['sequence']:fail('accepted Catalog source linkage differs')
 rows=b.select(f"SELECT COUNT(*) AS n,COUNT(DISTINCT TO_JSON_STRING(STRUCT(snapshot_date,product_id))) AS k,COUNTIF(SAFE_CAST(product_id AS INT64) IS NULL OR SAFE_CAST(product_id AS INT64)<=0) AS invalid_products,COUNTIF(sku IS NOT NULL AND (SAFE_CAST(sku AS INT64) IS NULL OR SAFE_CAST(sku AS INT64)<=0)) AS invalid_sku,COUNTIF(sku IS NOT NULL) AS sku_rows,COUNT(DISTINCT sku) AS sku_keys FROM `{b.c['project_id']}.ozon_raw.RAW_OZON_CATALOG` WHERE snapshot_date=@day",{'day':('DATE',day)})
 checks=[x for x in cert['coverage']['readback'] if x.get('table')=='RAW_OZON_CATALOG']
 if len(rows)!=1 or len(checks)!=1:fail('accepted Catalog count proof ambiguous')
 r=rows[0]
 if r['n']!=r['k'] or r['n']!=state['rows'] or r['n']!=checks[0]['rows'] or r['invalid_products'] or r['invalid_sku'] or r['sku_rows']!=r['sku_keys']:fail('accepted Catalog natural-key evidence changed')
 return day


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
 terminal_holder(b,p['claim'],timeout=False) if p['version']==4 else terminal_timeout(b,p['claim'])
 if p['version']==4:no_holder_effect(b,p)
 base,_=BF.resources(b.c);ss=b.request('GET',BF.SCHED_API+'/'+base+'/jobs?pageSize=100')
 if len(ss.get('jobs',[]))!=4 or ss.get('nextPageToken') or any(j['state']!='PAUSED' for j in ss['jobs']):fail('Scheduler not frozen')
 for name in (*b.c['marketplaces']['ozon']['jobs'],'tenant-control',O.JOB):
  xs=BF.execution_inventory(b.request,base+'/jobs/'+name)['executions']
  if any(not x.get('completionTime') for x in xs):fail('active execution')
  if name==O.JOB:
   by={x['name']:x for x in xs}
   dependent_stops=[r for r in records if r['kind']=='STOPPED' and D.digest(r) in p['stop_hashes']]
   old_release=parse((BF.REPO/'infra/tenant/releases/backfill'/f"{p['claim']['source']}.json").read_text())
   original=by.get(p['claim']['execution'])
   if original is None:fail('controller holder not visible')
   for record in dependent_stops:
    x=by.get(record['payload']['controller_execution'])
    dependent_terminal(b,p,record,x,original)
    old=O.block(O.historical_contract(b.c,old_release,m),dict(release=p['claim']['source'],root_hash=p['root'],scheduler_state='ENABLED'),BF.REPO,old_release)
    O.verify_job({'template':{'taskCount':x.get('taskCount',1),'template':x['template']}},old)
  if name!=O.JOB and any(CC.at(x['createTime'])>=CC.at(p['claim']['created_at']) for x in xs):fail('new child/external effect')
 if p['version']!=4:
  rows=b.select(f"SELECT COUNT(*) AS n FROM `{b.journal}` WHERE started_at>=@start",{'start':('TIMESTAMP',p['claim']['created_at'])})
  if rows!=[{'n':0}]:fail('source journal effect after claim')
 # Inspect election timestamps, including uncommitted intents. No source fence is
 # released, rewritten or treated as expired by this recovery.
 plans=H.reconstruct_plans(records,m,b.clock());shards={x['shard_root'] for x in plans.values()}|{r['payload']['shard_root'] for r in records if r['kind']=='DEPENDENCY_PLAN'}
 for n,labels,created,expiry in b.tables.list_tables(ds,with_expiry=True):
  match=re.fullmatch(r'BFQ_([0-9a-f]{64})_([0-9]{10})_DISPATCH_INTENT',n)
  if match and match[1] in shards and (created is None or created>=CC.at(p['claim']['created_at']) or expiry):fail('new/unknown source election')
 known,isolated=accepted_incidents(b,m,records)
 if {D.digest(r) for r in records if r['kind']=='STOPPED'}-known!=set(p['stop_hashes']):fail('another unresolved incident')
 pending=RP.pending_receipts(b,{i:item for i,item in plans.items() if i not in isolated})
 if p['version']==4:
  if [i for i,_ in pending]!=[p['last_source']['index']]:fail('pending source authority differs')
 elif pending:fail('unfinished source authority')
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
 prefix=BF.B.digest(['BOUNDED_PILOT_EXCLUSIVE',p['project']])[:16]
 if p['version']==4:return live_pending(b,p,m,records,sh,rr,doc,state,receipt,prefix,x['completionTime'])
 proof=BF.read_proof(b.c,doc,receipt['run_id'],request=ro.request)
 if proof['state']!=state:fail('source aggregate/state mismatch')
 for n in (BF.CK.lease_name(prefix,receipt['lease_generation']),f"LD_{prefix}_{receipt['lease_generation']:04d}"):
  v=b.tables.get_table(ds,n)
  if not v or v[0].get('owner')!=receipt['run_id'] or parse(v[1]).get('ack_hash')!=doc['ack_hash']:fail('last source lease/LD differs')
 if p['version']==3:return live_sequence(b,p,m,records,sh,rr,doc,state,ro)
 if not state['complete'] or D.digest(state)!=H.completed(records,m)[p['last_source']['index']]['state_hash']:fail('last source checkpoint differs')
 coverage=(BF.verify_coverage if p['version']==1 else BF.verify_persistence)(doc,doc['ack_hash'],backend=ro)
 if coverage['source_sequence']!=state['sequence'] or any(x.get('rows')!=x.get('keys') for x in coverage['readback']):fail('source persistence/key mismatch')
 if p['version']==1:
  day=str(b.clock().astimezone(BF.B.MSK).date())
  certs=[r for r in records if r['kind']=='SNAPSHOT_CERT' and r['payload'].get('day')==day]
  if len(certs)!=1:fail('current Catalog certificate unavailable')
  for x in coverage['readback']:
   if x.get('sku_linkage',{}).get('unresolved_sku_rows',1)!=0:fail('certified last source linkage unknown')
 else:
  completion=H.completed(records,m)[p['last_source']['index']]
  day=CC.at(completion['completed_at']).astimezone(BF.B.MSK).date().isoformat()
  day=accepted_catalog_lineage(b,m,records,day)
  # Require the accepted COMPLETE leaf's original strict linkage to the same
  # retained Catalog day. No fresh-day substitution or new completion is made.
  accepted=completion['coverage']['readback']
  for x in accepted:
   link=x.get('sku_linkage',{})
   if link.get('unresolved_sku_rows',1)!=0:fail('accepted leaf/Catalog linkage differs')
  entity=doc['runtime_plan']['entity']
  mapping={'finance_accrual':('RAW_OZON_FINANCE_ACCRUAL','event_date'),'fbo_postings':('RAW_OZON_POSTINGS_FBO',"DATE(order_date,'Europe/Moscow')"),'ads_sku_daily':('RAW_OZON_ADS_SKU_DAILY','date')}
  if entity not in mapping:fail('unsupported retained source linkage')
  table,col=mapping[entity];rp=doc['runtime_plan']
  rows=b.select(f"SELECT COUNTIF(r.sku IS NOT NULL AND NOT EXISTS (SELECT 1 FROM `{b.c['project_id']}.ozon_raw.RAW_OZON_CATALOG` c WHERE c.snapshot_date=@snapshot AND c.sku=r.sku)) AS unresolved FROM `{b.c['project_id']}.ozon_raw.{table}` r WHERE {col} BETWEEN @frm AND @to",{'snapshot':('DATE',day),'frm':('DATE',rp['from']),'to':('DATE',rp['to'])})
  if rows!=[{'unresolved':0}]:fail('retained source/Catalog linkage changed')
 return True


def live_sequence(b,p,m,records,sh,rr,doc,state,ro):
 """v3 publication proof: the reconciled page is the leaf's latest election, the
 leaf is NOT complete, and the persisted prefix still reconciles key-exactly."""
 from tools.tenancy import full_controller as H
 s=p['last_source']
 intents=[r['sequence'] for r in sh if r['kind']=='DISPATCH_INTENT']
 if not intents or max(intents)!=s['sequence'] or rr['sequence']!=s['sequence']:fail('later source election exists')
 rc=[r for r in sh if r['kind']=='RECONCILED' and D.digest(r)==s['recon_hash']]
 if len(rc)!=1 or rc[0]['payload'].get('state_hash')!=D.digest(state):fail('reconciled page state differs')
 if state['complete'] is not False or s['index'] in H.completed(records,m):fail('intermediate page must not complete the leaf')
 if state['sequence']<s['sequence']:fail('durable source state precedes the reconciled page')
 coverage=BF.verify_persistence(doc,doc['ack_hash'],backend=ro)
 if coverage['source_sequence']!=state['sequence'] or any(x.get('rows')!=x.get('keys') for x in coverage['readback'] if 'keys' in x):fail('source persistence/key mismatch')
 return True


def publication_record(p):return dict(version=D.VERSION,root_hash=p['root'],kind=KIND,sequence=p['claim']['generation'],payload=p)


def no_holder_effect(b,p):
 """v4: after its claim the holder committed nothing. Every controller-namespace lock
 and every checkpoint row created since the claim is the claim itself, a dependent STOP
 or this certificate's own publication (its exact closure, record marker and row), so an
 interrupted publication is resumable; owner release markers are other namespaces."""
 ds=b.c['datasets']['tenant_locks'];start=CC.at(p['claim']['created_at'])
 own=D.digest(publication_record(p));closure=CC.names(p['root'],p['claim']['generation'])[1]
 allowed={CC.names(p['root'],p['claim']['generation'])[0],'BFR_'+own}|{'BFR_'+h for h in p['stop_hashes']}
 for n,labels,created,expiry in b.tables.list_tables(ds,with_expiry=True):
  if not CONTROLLER_LOCK.fullmatch(n):continue
  if created is None:fail('controller lock time unknown')
  if created<start or n in allowed:continue
  if n==closure and b.tables.get_table(ds,n)==({'kind':'controller_close','root':p['root'][:16]},D.encoded(p['closure'])):continue
  fail('holder effect after claim')
 rows=b.select(f"SELECT backfill_id,entity FROM `{p['project']}.tenant_ops.BACKFILL_CHECKPOINTS` WHERE updated_at>=@start LIMIT 1001",{'start':('TIMESTAMP',p['claim']['created_at'])})
 if len(rows)>1000 or any(r['entity']!='orchestration' or r['backfill_id'] not in {h[:16] for h in p['stop_hashes']+[own]} for r in rows):fail('checkpoint effect after claim')
 return True


def live_pending(b,p,m,records,sh,rr,doc,state,receipt,prefix,runtime_completed):
 """v4 publication proof: the pending page is the leaf's latest election, elected and
 receipted BEFORE the failed claim, its runtime succeeded (checked by the caller), it is
 not reconciled, and its pilot lease is still held by that run (no LD). The runtime may
 still journal its own units after the claim (2026-10-25: u1967 one second later), never
 another run and never after its completion. Nothing here settles, releases or
 reconciles the page: the ordinary controller reconciles it next."""
 from tools.tenancy import full_controller as H
 s=p['last_source'];ds=b.c['datasets']['tenant_locks'];start=CC.at(p['claim']['created_at'])
 intents=[r['sequence'] for r in sh if r['kind']=='DISPATCH_INTENT']
 if not intents or max(intents)!=s['sequence'] or rr['sequence']!=s['sequence']:fail('later source election exists')
 if any(r['kind']=='RECONCILED' and r['sequence']==s['sequence'] for r in sh):fail('pending page already reconciled')
 if s['index'] in H.completed(records,m) or state['complete'] is not False:fail('pending page must not complete the leaf')
 created={n:at for n,labels,at,expiry in b.tables.list_tables(ds,with_expiry=True)}
 for kind in ('DISPATCH_INTENT','DISPATCH_RECEIPT'):
  at=created.get(f"BFQ_{s['shard']}_{s['sequence']:010d}_{kind}")
  if at is None or at>=start:fail('pending election not before the failed claim')
 if f"BFQ_{s['shard']}_{s['sequence']:010d}_RECONCILED" in created:fail('pending page already reconciled')
 lease=b.tables.get_table(ds,BF.CK.lease_name(prefix,receipt['lease_generation']))
 if not lease or lease[0].get('owner')!=receipt['run_id'] or parse(lease[1]).get('ack_hash')!=doc['ack_hash']:fail('pending source lease differs')
 if b.tables.get_table(ds,f"LD_{prefix}_{receipt['lease_generation']:04d}") is not None:fail('pending source lease already released')
 rows=b.select(f"SELECT ingestion_run_id AS run,FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E6SZ',started_at,'UTC') AS started FROM `{b.journal}` WHERE started_at>=@start LIMIT 1001",{'start':('TIMESTAMP',p['claim']['created_at'])})
 own=re.compile(re.escape(receipt['run_id'])+r'(-u[0-9]+)?')
 if len(rows)>1000 or any(not own.fullmatch(r['run'] or '') or CC.at(r['started'])>CC.at(runtime_completed) for r in rows):fail('source journal effect after claim')
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


def observe_sequence(b,m,generation,*,owner_ack_sha256,guard_probe_hash,security_proof_hash):
 """Owner-only, read-only v3 certificate on ONE snapshot (the incident of 2026-10-19).

 The STOP set is exactly the STOPPED records the shared acceptance set does not
 cover on this snapshot; each must then prove itself dependent (live_freeze).
 The last source is the single page RECONCILED after the claim was taken; the
 leaf stays unfinished. Predicates are asserted only after validate() and
 live_freeze() pass on the same evidence; publish() repeats the live checks.
 """
 from tools.tenancy import full_controller as H, stop_acceptance as SA, cloud_controller as C, orchestration_contract as O
 if b.current_execution is not None or b.c['orchestration']['job']['env']['HISTORICAL_SCHEDULER_STATE']!='PAUSED':fail('owner frozen observation required')
 verified=b.clock();records=b.store.history(m['hash'])
 if H.only_manifest(records)!=m:fail('foreign frozen root')
 # The holder's claim belongs to the replaced release and has no closure: exact bytes
 # only here; terminal_timeout() (live_freeze) proves its registered deployment.
 ds=b.c['datasets']['tenant_locks'];claim=CC.claim_fields(b,b.tables.get_table(ds,CC.names(m['hash'],generation)[0]))
 if claim['generation']!=generation or not CC.historical(b,claim) or CC.closed(b,claim):fail('unclosed replaced-release claim required')
 known=SA.accepted_hashes(b,m,records)
 stops=sorted(D.digest(r) for r in records if r['kind']=='STOPPED' and D.digest(r) not in known)
 if not stops:fail('no unresolved STOP to recover')
 # The single source page reconciled by the timed-out holder: its RECONCILED fence
 # was elected after the claim; exactly one such page, in exactly one plan shard.
 plans=H.reconstruct_plans(records,m,b.clock())
 by_shard={item['shard_root']:i for i,item in plans.items()}
 found=[]
 for n,labels,created,expiry in b.tables.list_tables(ds,with_expiry=True):
  match=re.fullmatch(r'BFQ_([0-9a-f]{64})_([0-9]{10})_RECONCILED',n)
  if match and match[1] in by_shard and created is not None and created>=CC.at(claim['created_at']):found.append((match[1],int(match[2])))
 if len(found)!=1:fail('exactly one page reconciled under the claim required')
 shard,sequence=found[0];index=by_shard[shard]
 sh=b.store.history(shard,max_records=F.MAX_LEAF_RECORDS)
 rr=[r for r in sh if r['kind']=='DISPATCH_RECEIPT' and r['sequence']==sequence];rc=[r for r in sh if r['kind']=='RECONCILED' and r['sequence']==sequence]
 if len(rr)!=1 or len(rc)!=1:fail('reconciled page evidence ambiguous')
 marker=b.tables.get_table(ds,C.descriptor_name(b.c['orchestration']['job']['env']['CONTROLLER_SOURCE_SHA'],m['hash'],'PAUSED'))
 if not marker:fail('installed PAUSED replacement descriptor absent')
 release=parse(marker[1])['release']
 doc=plans[index]['plan'];ro=FL_observer(b,m,index);proof=BF.verify_persistence(doc,doc['ack_hash'],backend=ro)
 p=dict(version=3,type=SEQUENCE_TYPE,tenant=m['tenant'],project=m['project'],root=m['hash'],manifest_hash=D.digest(m),claim=claim,
  stop_hashes=stops,protected_root_hashes=sorted(D.digest(r) for r in records),new_source=release['source_sha'],
  new_image=release['image'],new_implementation_hash=release['controller_implementation_hash'],runtime_image=release['runtime_image'],
  owner_ack_sha256=owner_ack_sha256,guard_probe_hash=guard_probe_hash,security_proof_hash=security_proof_hash,
  source_proof_hash=D.digest(proof),last_source=dict(index=index,shard=shard,receipt_hash=D.digest(rr[0]),recon_hash=D.digest(rc[0]),sequence=sequence),
  predicates=dict.fromkeys(SEQUENCE_PREDICATES,True),verified_at=verified.isoformat(),source_budget=0,
  closure=CC.sealed(dict(version=1,claim_hash=claim['hash'],status='SOURCE_FREE_RECOVERED',source_dispatches=0,closed_at=verified.isoformat())))
 p=sealed(p)
 validate(p,b,m,records,final=False);live_freeze(b,p,m,records)
 return p


def observe_pending(b,m,generation,*,owner_ack_sha256,guard_probe_hash,security_proof_hash):
 """Owner-only, read-only v4 certificate on ONE snapshot (the incident of 2026-10-25).

 The failed holder's claim is unclosed; exactly one plan shard has a receipted page
 without its RECONCILED fence and it is the shard's latest election. The STOP set is
 the STOPPED records the shared acceptance set does not cover. Predicates are asserted
 only after validate() and live_freeze() pass; publish() repeats the live checks.
 """
 from tools.tenancy import full_controller as H, cloud_controller as C
 if b.current_execution is not None or b.c['orchestration']['job']['env']['HISTORICAL_SCHEDULER_STATE']!='PAUSED':fail('owner frozen observation required')
 verified=b.clock();records=b.store.history(m['hash'])
 if H.only_manifest(records)!=m:fail('foreign frozen root')
 ds=b.c['datasets']['tenant_locks'];claim=CC.claim_fields(b,b.tables.get_table(ds,CC.names(m['hash'],generation)[0]))
 if claim['generation']!=generation or not CC.historical(b,claim) or CC.closed(b,claim):fail('unclosed replaced-release claim required')
 # The same pending inventory as live_freeze (recent_priority.pending_receipts): receipts
 # settled by accepted recoveries or isolated by quarantine are not pending authority.
 from tools.tenancy import recent_priority as RP
 known,isolated=accepted_incidents(b,m,records)
 stops=sorted(D.digest(r) for r in records if r['kind']=='STOPPED' and D.digest(r) not in known)
 if not stops:fail('no unresolved STOP to recover')
 plans=H.reconstruct_plans(records,m,b.clock())
 pending=RP.pending_receipts(b,{i:item for i,item in plans.items() if i not in isolated})
 if len(pending)!=1:fail('exactly one pending receipted page required')
 index=pending[0][0];shard=plans[index]['shard_root'];latest={}
 for n,labels,created,expiry in b.tables.list_tables(ds,with_expiry=True):
  match=re.fullmatch(r'BFQ_([0-9a-f]{64})_([0-9]{10})_(DISPATCH_INTENT|DISPATCH_RECEIPT|RECONCILED)',n)
  if match and match[1]==shard:latest[match[3]]=max(int(match[2]),latest.get(match[3],0))
 sequence=latest.get('DISPATCH_RECEIPT',0)
 if not sequence or latest.get('DISPATCH_INTENT')!=sequence or latest.get('RECONCILED',0)>=sequence:fail('pending page is not the latest receipted election')
 sh=b.store.history(shard,max_records=F.MAX_LEAF_RECORDS)
 ri=[r for r in sh if r['kind']=='DISPATCH_INTENT' and r['sequence']==sequence];rr=[r for r in sh if r['kind']=='DISPATCH_RECEIPT' and r['sequence']==sequence]
 if len(ri)!=1 or len(rr)!=1:fail('pending page evidence ambiguous')
 marker=b.tables.get_table(ds,C.descriptor_name(b.c['orchestration']['job']['env']['CONTROLLER_SOURCE_SHA'],m['hash'],'PAUSED'))
 if not marker:fail('installed PAUSED replacement descriptor absent')
 release=parse(marker[1])['release']
 doc=plans[index]['plan'];ro=FL_observer(b,m,index);receipt=rr[0]['payload']['receipt']
 proof=BF.read_proof(b.c,doc,receipt['run_id'],request=ro.request)
 p=dict(version=4,type=PENDING_TYPE,tenant=m['tenant'],project=m['project'],root=m['hash'],manifest_hash=D.digest(m),claim=claim,
  stop_hashes=stops,protected_root_hashes=sorted(D.digest(r) for r in records),new_source=release['source_sha'],
  new_image=release['image'],new_implementation_hash=release['controller_implementation_hash'],runtime_image=release['runtime_image'],
  owner_ack_sha256=owner_ack_sha256,guard_probe_hash=guard_probe_hash,security_proof_hash=security_proof_hash,
  source_proof_hash=D.digest(proof),last_source=dict(index=index,shard=shard,intent_hash=D.digest(ri[0]),receipt_hash=D.digest(rr[0]),sequence=sequence),
  predicates=dict.fromkeys(PENDING_PREDICATES,True),verified_at=verified.isoformat(),source_budget=0,
  closure=CC.sealed(dict(version=1,claim_hash=claim['hash'],status='SOURCE_FREE_RECOVERED',source_dispatches=0,closed_at=verified.isoformat())))
 p=sealed(p)
 validate(p,b,m,records,final=False);live_freeze(b,p,m,records)
 return p


def FL_observer(b,m,index):
 from tools.tenancy import full_leaf_recovery as FL
 return FL.observer(b,m,index)
