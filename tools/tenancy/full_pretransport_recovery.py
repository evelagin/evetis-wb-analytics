"""Owner-only closure of one exact FULL intent rejected before token/transport.

No automatic recovery, success receipt, reconciliation or source replay. The
original STOP, intent, RUNNING checkpoint and lease remain immutable. A fresh
owner attestation and packaged old-image proof are mandatory before publication.
"""
import re
from datetime import timedelta
from tools.tenancy import durable_plan as D,tenant_backfill as BF,full_history as F
from tools.tenancy.validation import parse_tenant_json as parse

KIND='FULL_PRETRANSPORT_STOP_RECOVERED'
SHARD_KIND='DISPATCH_PRETRANSPORT_REJECTED'
TYPE='EXACT_FULL_AUTHORITY_OVERRIDE_PRETRANSPORT_REJECTION_V1'
EXACT=dict(root='bb3ceca51ec5add6fe6fa3af8ec3720fbb5d9b07054b84e26d6bbfd6c8824222',
 stop_hash='5bb1fba2a935bbfc6a5075535009fa291ecf4a6d0db9303d017b0b0b96e17fc0',
 execution='projects/mpa-t-client-001/locations/europe-west1/jobs/tenant-backfill-controller/executions/tenant-backfill-controller-sz47j',
 original_source='c2bb7ca99e5ec2133d0eec7fdb7508ec1078d1af',
 original_image='europe-west1-docker.pkg.dev/mpa-platform/mpa-runtime/tenant-backfill-controller@sha256:1aa5ce54aa12178454c682a431c5c6518d27758359b3671f4693b514c2868344',
 shard='8fec4c2c620a9821c8490671f9fdf50c30328de122ea0aa78339b16b5b3c7e10',
 intent_hash='dfc8954c4e458303c7c0519c47798c96dfb3f7f3c1ce73de9264aaf7990bd698',
 run_id='bf-8f37145a-e369-4dd1-b3d1-80303ed58753',lease_generation=170,
 ack_hash='d1fcf0c4079955fc41bc6653e33915d2ded804ddcc609bc1a08ab5d7740d305e',
 index=89,receipt_sequence=1)
PREDICATES=frozenset({'exact_old_image','packaged_pretransport_rejection','zero_token_and_transport',
 'replacement_exact_dispatch_pass','committed_exact_intent','no_receipt_or_operation','no_child_runtime',
 'no_journal_or_raw_effect','no_source_state_change','no_successor_intent','no_later_lease',
 'exact_lease_and_running_checkpoint','all_jobs_inactive','all_schedulers_paused','bindings_pass',
 'security_metadata_unchanged','runtime_unchanged','frozen_root_recent_preserved','prior_recoveries_preserved',
 'no_original_evidence_rewrite','owner_exact_scope','source_budget_zero'})
FIELDS=frozenset(EXACT)|{'version','type','tenant','project','manifest_hash','new_source','new_image',
 'new_implementation_hash','runtime_image','owner_decision_sha256','packaged_probe_hash',
 'replacement_check_hash','protected_record_hashes','predicates','verified_at','source_budget',
 'source_state_hash','closure_hash','hash'}

def fail(message):raise BF.B.EvidenceError('full pretransport recovery: '+message)
def sealed(p):return dict(p,hash=D.digest(p))
def authority_name(p):return 'BFPT_AUTH_'+p['root']+'_'+p['stop_hash']
def certificate_name(p):return 'BFPT_'+p['hash']
def value(p):return {'kind':'full_pretransport','root':p['root'][:16]},D.encoded(p)
def closure(p):
 return {'version':D.VERSION,'root_hash':p['shard'],'kind':SHARD_KIND,'sequence':p['receipt_sequence'],
  'payload':{k:p[k] for k in ('type','stop_hash','intent_hash','run_id','lease_generation','source_budget')}}
def release_value(p):return {'owner':p['run_id']},D.encoded({'terminal':'PRETRANSPORT_REJECTED','ack_hash':p['ack_hash'],'certificate_hash':p['hash']})
def checkpoint_evidence(p):return {'mode':'BOUNDED_PILOT','pretransport_certificate':p['hash'],'source_budget':0}

def header(p,m):
 if (not isinstance(p,dict) or set(p)!=FIELDS or type(p['version']) is not int or p['version']!=1
     or p['type']!=TYPE or D.digest({k:v for k,v in p.items() if k!='hash'})!=p['hash']):fail('closed exact certificate required')
 if any(p[k]!=v or type(p[k]) is not type(v) for k,v in EXACT.items()):fail('only exact incident supported')
 if (p['tenant'],p['project'],p['root'],p['manifest_hash'])!=(m['tenant'],m['project'],m['hash'],D.digest(m)):fail('foreign frozen root')
 if (p['tenant'],p['project'])!=('client_001','mpa-t-client-001'):fail('foreign tenant')
 if type(p['source_budget']) is not int or p['source_budget']!=0:fail('source budget must be zero')
 if not isinstance(p['predicates'],dict) or set(p['predicates'])!=PREDICATES or any(v is not True for v in p['predicates'].values()):fail('unknown mandatory predicate')
 for k in FIELDS:
  if k.endswith('_hash') or k.endswith('_sha256') or k in {'hash','root','shard'}:D.check_hash(p[k])
 if not re.fullmatch('[0-9a-f]{40}',p['new_source']) or p['new_source']==p['original_source']:fail('qualified replacement required')
 if not re.fullmatch('europe-west1-docker.pkg.dev/mpa-platform/mpa-runtime/tenant-backfill-controller@sha256:[0-9a-f]{64}',p['new_image']):fail('immutable replacement required')
 hs=p['protected_record_hashes']
 if not isinstance(hs,list) or not 1<=len(hs)<=10000 or any(not isinstance(h,str) for h in hs) or hs!=sorted(set(hs)):fail('bounded protected history required')
 for h in hs:D.check_hash(h)
 if D.digest(closure(p))!=p['closure_hash']:fail('exact rejected intent closure differs')
 F.stamp(p['verified_at']);return p

def verify_shard(p,sh,*,final=True):
 intents=[r for r in sh if r['kind']=='DISPATCH_INTENT' and D.digest(r)==p['intent_hash']]
 if len(intents)!=1:fail('original intent absent')
 i=intents[0];doc=i['payload']['plan'];prep=i['payload']['preparation']
 if (i['sequence']!=1 or i['root_hash']!=p['shard'] or doc['ack_hash']!=p['ack_hash']
     or any(prep[k]!=p[k] for k in ('run_id','lease_generation','ack_hash'))):fail('original intent scope differs')
 if any(r['sequence']==1 and r['kind'] in {'DISPATCH_RECEIPT','RECONCILED'} for r in sh):fail('rejected attempt has receipt/result')
 if final and closure(p) not in sh:fail('additive closure missing')
 return doc

def validate(p,b,m,records,*,final=True):
 header(p,m)
 from tools.tenancy import full_controller as H,cloud_controller as C,orchestration_contract as O
 if H.only_manifest(records)!=m or not set(p['protected_record_hashes'])<={D.digest(r) for r in records}:fail('protected root changed')
 expected={'reason':'CONTROLLER_GATE_OR_EVIDENCE_FAILURE','controller_execution':p['execution'],
           'diagnostic':{'version':1,'stage':'SOURCE_DISPATCH_BOUNDARY','category':'CLOUD_METADATA'}}
 stops=[r for r in records if r['kind']=='STOPPED' and D.digest(r)==p['stop_hash']]
 if len(stops)!=1 or stops[0]['payload']!=expected:fail('original STOP differs')
 sh=b.store.history(p['shard'],max_records=F.MAX_LEAF_RECORDS);doc=verify_shard(p,sh,final=final)
 if not any(r['kind']=='DEPENDENCY_PLAN' and r['payload']['plan']==doc and r['payload']['shard_root']==p['shard'] for r in records):fail('frozen dependency missing')
 lease=b.tables.get_table(b.c['datasets']['tenant_locks'],f"L_50c5d90dbf43ac9c_{p['lease_generation']:04d}")
 if not lease or lease[0].get('owner')!=p['run_id'] or parse(lease[1]).get('ack_hash')!=p['ack_hash']:fail('original lease differs')
 if final and b.tables.get_table(b.c['datasets']['tenant_locks'],f"LD_50c5d90dbf43ac9c_{p['lease_generation']:04d}")!=release_value(p):fail('source-free release differs')
 rows=b.select(f"SELECT DISTINCT status,lease_owner,error_code,evidence_json FROM `{p['project']}.tenant_ops.BACKFILL_CHECKPOINTS` WHERE entity='catalog' AND plan_hash=@ack AND lease_generation=170 LIMIT 3",{'ack':('STRING',p['ack_hash'])})
 original={'status':'RUNNING','lease_owner':p['run_id'],'error_code':None,'evidence_json':D.encoded({'mode':'BOUNDED_PILOT','proof':None})}
 failed={'status':'FAILED','lease_owner':p['run_id'],'error_code':'PRETRANSPORT_REJECTED','evidence_json':D.encoded(checkpoint_evidence(p))}
 if original not in rows or any(r not in (original,failed) for r in rows) or (final and failed not in rows):fail('original/additive checkpoint differs')
 marker=b.tables.get_table(b.c['datasets']['tenant_locks'],C.descriptor_name(p['new_source'],p['root'],'PAUSED'))
 descriptor=parse(marker[1]) if marker else {};settings={'release':p['new_source'],'root_hash':p['root'],'scheduler_state':'PAUSED'}
 if set(descriptor)!={'settings','release'} or descriptor['settings']!=settings:fail('replacement descriptor missing')
 release=descriptor['release']
 if (release['source_sha'],release['image'],release['controller_implementation_hash'],release['runtime_image'])!=(p['new_source'],p['new_image'],p['new_implementation_hash'],p['runtime_image']):fail('qualified replacement differs')
 if b.c['orchestration']['job']['env']['CONTROLLER_SOURCE_SHA']==p['new_source']:O.verify_artifact_source(release,BF.REPO)
 elif parse((BF.REPO/'infra/tenant/releases/backfill'/f"{p['new_source']}.json").read_text())!=release:fail('historical replacement differs')
 O.block(b.c,settings,BF.REPO,release)
 return doc

def load(b,m,records):
 for r in records:
  if r['kind']==KIND and (r['root_hash']!=m['hash'] or r['sequence']!=EXACT['index']):fail('root recovery scope differs')
 found=[r['payload'] for r in records if r['kind']==KIND]
 if len(found)>1:fail('single exact recovery only')
 for p in found:
  validate(p,b,m,records)
  if any(b.tables.get_table(ds,n)!=value(p) for ds,n in ((b.c['datasets']['ref'],authority_name(p)),(b.c['datasets']['tenant_locks'],certificate_name(p)))):fail('owner certificate/fence missing')
 return found

def publish(b,p,m):
 """Owner exact additive closure; no automatic publisher or source execution."""
 from tools.tenancy import full_controller as H,full_leaf_recovery as FL,full_cooldown_recovery as FC,full_policy_bootstrap_recovery as PB
 if b.current_execution is not None:fail('owner source-free observation required')
 records=b.store.history(m['hash']);doc=validate(p,b,m,records,final=False)
 if b.c['orchestration']['job']['env']['CONTROLLER_SOURCE_SHA']!=p['new_source'] or b.c['orchestration']['job']['env']['HISTORICAL_SCHEDULER_STATE']!='PAUSED':fail('frozen replacement required')
 age=b.clock()-F.stamp(p['verified_at'])
 if age<timedelta(0) or age>timedelta(minutes=15):fail('fresh predicates required')
 accepted=FL.load(b,m,records)+FC.load(b,m,records)+PB.load(b,m,records)
 if {D.digest(r) for r in records if r['kind']=='STOPPED'}-{x['stop_hash'] for x in accepted}!={p['stop_hash']}:fail('another unresolved incident')
 if load(b,m,records):return p
 if sorted(D.digest(r) for r in records)!=p['protected_record_hashes']:fail('root changed since observation')
 sh=b.store.history(p['shard'],max_records=F.MAX_LEAF_RECORDS)
 if any(r['sequence']>1 for r in sh):fail('successor authority exists')
 x=b.request('GET',BF.RUN_API+'/'+p['execution']);env={e['name']:e.get('value') for e in x['template']['containers'][0]['env']}
 if (x.get('failedCount')!=1 or x.get('succeededCount',0) or x.get('retriedCount',0)
     or not x.get('completionTime') or x['template']['containers'][0]['image']!=p['original_image']
     or env.get('CONTROLLER_SOURCE_SHA')!=p['original_source']
     or env.get('BACKFILL_ROOT_HASH')!=p['root'] or env.get('TENANT_BINDING_REQUIRED')!='1'
     or env.get('STRICT_PAGE_CAPS')!='1'):fail('original terminal controller differs')
 observer=FL.observer(b,m,p['index']);H.verify_authority(observer,m);observer.preflight({'hash':m['hash'],'plans':[doc]})
 if D.digest(b.state(doc))!=p['source_state_hash'] or b.state(doc)!=BF.B.initial(doc['runtime_plan']):fail('source state/effect differs')
 n=b.select(f"SELECT COUNT(*) AS n FROM `{b.journal}` WHERE started_at>=@start",{'start':('TIMESTAMP','2026-10-10T11:28:55.186669Z')})
 if n!=[{'n':0}]:fail('new source evidence')
 base=f"projects/{p['project']}/locations/{b.c['region']}"
 sched=b.request('GET',BF.SCHED_API+'/'+base+'/jobs?pageSize=100')
 if len(sched.get('jobs',[]))!=4 or sched.get('nextPageToken') or any(j['state']!='PAUSED' for j in sched['jobs']):fail('Scheduler not frozen')
 for name in (*b.c['marketplaces']['ozon']['jobs'],'tenant-control','tenant-backfill-controller'):
  xs=BF.execution_inventory(b.request,base+'/jobs/'+name)['executions']
  if any(not x.get('completionTime') for x in xs):fail('active execution')
  if name!='tenant-backfill-controller' and any(F.stamp(x['createTime'])>=F.stamp('2026-10-10T11:28:55.186669Z') for x in xs):fail('child/control effect exists')
 for name,*_ in b.tables.list_tables(b.c['datasets']['tenant_locks']):
  match=BF.CK.LEASE_RE.match(name)
  if match and match[1]=='50c5d90dbf43ac9c' and int(match[2])>170:fail('later source lease')
 existing=b.tables.get_table(b.c['datasets']['tenant_locks'],'LD_50c5d90dbf43ac9c_0170')
 if existing is not None and existing!=release_value(p):fail('preexisting uncertain release')
 for ds,name in ((b.c['datasets']['ref'],authority_name(p)),(b.c['datasets']['tenant_locks'],certificate_name(p))):
  old=b.tables.get_table(ds,name)
  if old is None:b.tables.create_marker(ds,name,*value(p))
  if b.tables.get_table(ds,name)!=value(p):fail('owner publication conflict')
 row=BF.checkpoint(doc,p['run_id'],'FAILED',170,b.clock());row['error_code']='PRETRANSPORT_REJECTED';row['evidence_json']=D.encoded(checkpoint_evidence(p))
 b.tables.append(b.c['datasets']['tenant_ops'],'BACKFILL_CHECKPOINTS',[row])
 name='LD_50c5d90dbf43ac9c_0170';b.tables.create_marker(b.c['datasets']['tenant_locks'],name,*release_value(p))
 if b.tables.get_table(b.c['datasets']['tenant_locks'],name)!=release_value(p):fail('closure release conflict')
 z=closure(p);b.store.commit(p['shard'],SHARD_KIND,1,z['payload'],b.clock())
 b.store.commit(p['root'],KIND,p['index'],p,b.clock())
 if load(b,m,b.store.history(p['root']))!=[p]:fail('additive acceptance differs')
 return p
