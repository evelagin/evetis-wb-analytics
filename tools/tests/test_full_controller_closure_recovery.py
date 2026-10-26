"""Terminal control bookkeeping recovery must never weaken source fences."""
from copy import deepcopy
from datetime import timedelta
import pytest
from tools.tenancy import full_controller_closure_recovery as R, controller_cadence as CC, orchestration_contract as O, tenant_backfill as BF, durable_plan as D
from tools.tenancy.full_controller_closure_image_check import fixture,check,NOW


def test_packaged_recovery():assert check()['terminal_controller_recovery']=='PASS'


@pytest.mark.parametrize('fault',['valid_overlap','legacy','before_holder','after_execution','missing_event','ambiguous_event','foreign_event','unzonable_event','missing_start','retried','cancelled','success'])
def test_overlapping_wake_needs_exact_post_terminal_stop_event(fault):
 m,p,b,root,objects,_,_=fixture();before=deepcopy(objects)
 p.update(version=2,type=R.RECONCILED_TYPE)
 record=next(r for r in root if r['kind']=='STOPPED')
 holder={'completionTime':(NOW-timedelta(seconds=60)).isoformat()}
 x={'createTime':(NOW-timedelta(seconds=120)).isoformat(),'startTime':(NOW-timedelta(seconds=110)).isoformat(),'completionTime':NOW.isoformat(),'failedCount':1}
 row={'evidence_json':D.encoded(record),'recorded_at':(NOW-timedelta(seconds=3)).isoformat()};rows=[row];queries=[]
 def select(sql,params):queries.append((sql,params));return rows
 b.select=select
 if fault=='legacy':p['version']=1
 if fault=='before_holder':row['recorded_at']=(NOW-timedelta(seconds=70)).isoformat()
 if fault=='after_execution':row['recorded_at']=(NOW+timedelta(seconds=1)).isoformat()
 if fault=='missing_event':rows=[]
 if fault=='ambiguous_event':rows=[row,dict(row,recorded_at=NOW.isoformat())]
 if fault=='foreign_event':row['evidence_json']=D.encoded(dict(record,sequence=999))
 if fault=='unzonable_event':row['recorded_at']='2026-10-13T00:00:00'
 if fault=='missing_start':x.pop('startTime')
 if fault in {'retried','cancelled'}:x[fault+'Count']=1
 if fault=='success':x.update(failedCount=0,succeededCount=1)
 if fault=='valid_overlap':
  assert R.dependent_terminal(b,p,record,x,holder)
  assert len(queries)==1 and 'LIMIT 2' in queries[0][0]
  assert queries[0][1]=={'root':('STRING',m['hash']),'id':('STRING',D.digest(record)[:16])}
 else:
  with pytest.raises(BF.B.EvidenceError):R.dependent_terminal(b,p,record,x,holder)
 assert objects==before


def test_large_root_proof_is_complete_but_owner_reference_is_bounded():
 m,p,*_=fixture();old=R.value(p)
 p.update(version=2,type=R.RECONCILED_TYPE,predicates=dict.fromkeys(R.RECONCILED_PREDICATES,True),protected_root_hashes=[f'{n:064x}' for n in range(1000)])
 p=R.sealed({k:v for k,v in p.items() if k!='hash'});R.header(p,m)
 import json
 ref=json.loads(R.value(p)[1]);assert len(R.value(p)[1].encode())<16384
 assert ref['certificate_hash']==p['hash'] and len(D.encoded(p).encode())>16384
 assert len(p['protected_root_hashes'])==1000 and old[1]!=R.value(p)[1]

@pytest.mark.parametrize('fault',['missing','counts','duplicate','malformed','source','future'])
def test_retained_catalog_source_lineage_is_checked_without_new_source(monkeypatch,fault):
 from types import SimpleNamespace
 b=SimpleNamespace(c={'project_id':'mpa-t-client-001'},clock=lambda:NOW)
 day=NOW.astimezone(BF.B.MSK).date().isoformat();doc={'ack_hash':'a'*64,'runtime_plan':{'entity':'catalog','observation_date':day}}
 cert={'day':day,'plan_hash':doc['ack_hash'],'coverage':{'source_sequence':1,'readback':[{'table':'RAW_OZON_CATALOG','rows':2}]}}
 records=[{'kind':'SNAPSHOT_CERT','payload':cert},{'kind':'DEPENDENCY_PLAN','payload':{'plan':doc}}]
 state={'complete':True,'sequence':1,'rows':2};row={'n':2,'k':2,'invalid_products':0,'invalid_sku':0,'sku_rows':2,'sku_keys':2}
 b.state=lambda d:state;b.select=lambda *a:[row]
 if fault=='missing':records.pop(0)
 if fault=='counts':row['n']=3
 if fault=='duplicate':row['k']=1
 if fault=='malformed':row['invalid_sku']=1
 if fault=='source':state['complete']=False
 if fault=='future':day=(NOW+timedelta(days=1)).astimezone(BF.B.MSK).date().isoformat();cert['day']=day;doc['runtime_plan']['observation_date']=day
 with pytest.raises(BF.B.EvidenceError):R.accepted_catalog_lineage(b,{},records,day)

def test_retained_catalog_is_not_relabelled_as_current_freshness():
 from types import SimpleNamespace
 b=SimpleNamespace(c={'project_id':'mpa-t-client-001'},clock=lambda:NOW+timedelta(days=1))
 day=NOW.astimezone(BF.B.MSK).date().isoformat();doc={'ack_hash':'a'*64,'runtime_plan':{'entity':'catalog','observation_date':day}}
 records=[{'kind':'SNAPSHOT_CERT','payload':{'day':day,'plan_hash':'a'*64,'coverage':{'source_sequence':1,'readback':[{'table':'RAW_OZON_CATALOG','rows':2}]}}},{'kind':'DEPENDENCY_PLAN','payload':{'plan':doc}}]
 b.state=lambda d:{'complete':True,'sequence':1,'rows':2};b.select=lambda *a:[{'n':2,'k':2,'invalid_products':0,'invalid_sku':0,'sku_rows':2,'sku_keys':2}]
 assert R.accepted_catalog_lineage(b,{},records,day)==day
 assert day!=b.clock().astimezone(BF.B.MSK).date().isoformat()


def test_every_exact_recovery_loader_is_required_before_local_isolation(monkeypatch):
 # The shared FULL STOP gate set (stop_acceptance): every exact recovery, including
 # STOP819, f507, R1 and R2 that the former private list here omitted.
 from types import SimpleNamespace
 from tools.tenancy import full_leaf_recovery as FL,full_cooldown_recovery as FC,full_policy_bootstrap_recovery as PB,full_pretransport_recovery as PT,full_first_receipt_recovery as FR,full_fbo_zero_effect_recovery as ZE,full_fbo_diagnostic_closure as LQ,full_pre_authority_gate_recovery as PAG
 from tools.tenancy import full_retained_pretransport_recovery as R1,full_inventory_gate_recovery as R2,current_recon_recovery as CURRENT_RECON,f507_recovery as F507
 modules=[FL,FC,PB,PT,FR,ZE,LQ,PAG,R,R1,R2,CURRENT_RECON,F507];calls=[]
 for i,module in enumerate(modules):
  value={'stop_hashes':['9'*64]} if module is R else {'stop_hash':('%02d'%(i+1))*32}
  if module is LQ:value.update(index=1,isolation_scope='FBO_SOURCE')
  def loader(*args,module=module,value=value):
   calls.append(module);return [value]
  monkeypatch.setattr(module,'load',loader)
 b=SimpleNamespace();m={'programs':[{'entity':'finance_accrual'},{'entity':'fbo_postings'},{'entity':'fbo_postings'}]}
 records=[{'kind':getattr(module,'KIND','NONE')} for module in modules]
 known,isolated=R.accepted_incidents(b,m,records)
 assert set(calls)==set(modules) and len(known)==13 and isolated=={1,2}
 assert b.verified_fbo_zero_effect_failures==[{'stop_hash':'06'*32}]
 monkeypatch.setattr(LQ,'load',lambda *a:(_ for _ in ()).throw(BF.B.EvidenceError('unknown quarantine')))
 with pytest.raises(BF.B.EvidenceError,match='unknown quarantine'):R.accepted_incidents(SimpleNamespace(),m,records)

@pytest.mark.parametrize('field',sorted(R.PREDICATES))
@pytest.mark.parametrize('value',[False,None,1])
def test_every_unknown_or_false_predicate_blocks_before_any_write(field,value):
 m,p,b,root,objects,*_=fixture();before=deepcopy(objects)
 q=deepcopy(p);q['predicates'][field]=value;q=R.sealed({k:v for k,v in q.items() if k!='hash'})
 with pytest.raises(BF.B.EvidenceError,match='unknown mandatory'):R.publish(b,q,m)
 assert objects==before

@pytest.mark.parametrize('fault',['root','claim','stop','protected','receipt','recon','closure','source_budget','source','extra'])
def test_certificate_conflicts_do_not_close_control_or_change_source(fault):
 m,p,b,root,objects,*_=fixture();before=deepcopy(objects);q=deepcopy(p)
 if fault=='root':q['root']='1'*64
 if fault=='claim':q['claim']['generation']=2
 if fault=='stop':q['stop_hashes']=['1'*64]
 if fault=='protected':q['protected_root_hashes']=['1'*64]
 if fault=='receipt':q['last_source']['receipt_hash']='1'*64
 if fault=='recon':q['last_source']['recon_hash']='1'*64
 if fault=='closure':q['closure']['source_dispatches']=1
 if fault=='source_budget':q['source_budget']=1
 if fault=='source':q['new_source']=q['claim']['source']
 if fault=='extra':q['unrecognized']=True
 q=R.sealed({k:v for k,v in q.items() if k!='hash'})
 with pytest.raises((BF.B.EvidenceError,ValueError)):R.validate(q,b,m,root,final=False)
 assert objects==before

@pytest.mark.parametrize('seconds',[600,1799,1801,3600,'1800',True])
def test_unqualified_timeout_not_an_override(seconds):
 m,p,b,root,objects,r,*_=fixture();r=deepcopy(r);r['controller_timeout_seconds']=seconds
 with pytest.raises(ValueError):O.block(b.c,dict(release=r['source_sha'],root_hash=m['hash'],scheduler_state='PAUSED'),BF.REPO,r)


def test_timeout_gate_required_and_historical_release_unchanged():
 m,p,b,root,objects,r,*_=fixture();r=deepcopy(r);r['verification'].pop('terminal_controller_adapter')
 with pytest.raises(ValueError):O.block(b.c,dict(release=r['source_sha'],root_hash=m['hash'],scheduler_state='PAUSED'),BF.REPO,r)
 r.pop('controller_timeout_seconds');settings=dict(release=r['source_sha'],root_hash=m['hash'],scheduler_state='PAUSED')
 assert O.block(b.c,settings,BF.REPO,r)['job']['timeout']=='600s'


def test_missing_owner_certificate_is_not_a_blanket_stop_exemption():
 m,p,b,root,objects,*_=fixture();root.append(dict(version=D.VERSION,root_hash=m['hash'],kind=R.KIND,sequence=p['claim']['generation'],payload=p))
 objects[CC.names(m['hash'],p['claim']['generation'])[1]]=({'kind':'controller_close','root':m['hash'][:16]},D.encoded(p['closure']))
 with pytest.raises(BF.B.EvidenceError,match='owner authority'):R.load(b,m,root)

@pytest.mark.parametrize('fault',['valid_timeout','ordinary_failure','cancelled','retried','active','source_image','root','unregistered_descriptor'])
def test_only_exact_registered_controller_timeout_supported(monkeypatch,tmp_path,fault):
 from tools.tenancy import cloud_controller as C
 m,p,b,root,objects,new,x=fixture();old=deepcopy(new)
 old.update(source_sha=p['claim']['source'],image=p['claim']['image']);old.pop('controller_timeout_seconds');old['verification'].pop('terminal_controller_adapter')
 path=tmp_path/'infra/tenant/releases/backfill';path.mkdir(parents=True);(path/(old['source_sha']+'.json')).write_text(D.encoded(old))
 monkeypatch.setattr(BF,'REPO',tmp_path)
 settings=dict(release=old['source_sha'],root_hash=m['hash'],scheduler_state='ENABLED');dn=C.descriptor_name(old['source_sha'],m['hash'],'ENABLED')
 objects[dn]=({},D.encoded(dict(settings=settings,release=old)))
 holder=x[0];holder.update(completionTime=NOW.isoformat(),failedCount=1,conditions=[dict(type='Completed',state='CONDITION_FAILED',message='The configured timeout was reached.')])
 if fault=='ordinary_failure':holder['conditions'][0]['message']='unrelated failure'
 if fault=='cancelled':holder['cancelledCount']=1
 if fault=='retried':holder['retriedCount']=1
 if fault=='active':holder.pop('completionTime')
 if fault=='source_image':holder['template']['containers'][0]['image']=new['runtime_image']
 if fault=='root':holder['template']['containers'][0]['env'].append(dict(name='BACKFILL_ROOT_HASH',value='f'*64))
 if fault=='unregistered_descriptor':objects.pop(dn)
 before=deepcopy(objects)
 if fault=='valid_timeout':assert R.terminal_timeout(b,p['claim'])==holder
 else:
  with pytest.raises((BF.B.EvidenceError,ValueError)):R.terminal_timeout(b,p['claim'])
 assert objects==before

@pytest.mark.parametrize('fault',['active_controller','active_source','new_runtime','new_control_generation','journal_effect','new_source_intent','unknown_source_time','unpaused_scheduler'])
def test_live_effect_and_ownership_guards_block_without_mutations(monkeypatch,fault):
 from tools.tenancy import full_controller as H
 m,p,b,root,objects,new,x=fixture();base,_=BF.resources(b.c);before=deepcopy(objects)
 monkeypatch.setattr(R,'terminal_timeout',lambda *a:x[0])
 import pathlib
 old=deepcopy(new);old.update(source_sha=p['claim']['source'],image=p['claim']['image']);old.pop('controller_timeout_seconds');old['verification'].pop('terminal_controller_adapter')
 read=pathlib.Path.read_text
 monkeypatch.setattr(pathlib.Path,'read_text',lambda path,*a,**kw:D.encoded(old) if path.name==old['source_sha']+'.json' else read(path,*a,**kw))
 # Keep concrete freeze/election/SELECT guards; historical source certificates
 # are beyond these injected early failures and must never be reached.
 root[:]=[r for r in root if r['kind']!='STOPPED'];p['stop_hashes']=[]
 original_list=b.tables.list_tables
 def tables(ds,with_expiry=False):
  values=original_list(ds,with_expiry)
  if fault=='new_control_generation':values.append((CC.names(m['hash'],2)[0],{},NOW,None))
  if fault in {'new_source_intent','unknown_source_time'}:
   values.append((f"BFQ_{p['last_source']['shard']}_0000000002_DISPATCH_INTENT",{},None if fault=='unknown_source_time' else NOW,None))
  return values
 b.tables.list_tables=tables
 def request(method,url,body=None):
  assert method=='GET'
  if '/jobs?pageSize=100' in url:return dict(jobs=[dict(state='ENABLED' if fault=='unpaused_scheduler' else 'PAUSED') for _ in range(4)])
  if '/executions?' in url:
   job=url.split('/jobs/')[1].split('/')[0]
   if job==O.JOB:
    holder=deepcopy(x[0]);holder.update(completionTime=NOW.isoformat(),failedCount=1)
    if fault=='active_controller':holder.pop('completionTime')
    return dict(executions=[holder])
   if job=='ozon-runtime-fast' and fault in {'active_source','new_runtime'}:
    v=dict(name=base+'/jobs/'+job+'/executions/'+job+'-example',createTime=NOW.isoformat())
    if fault=='new_runtime':v['completionTime']=NOW.isoformat()
    return dict(executions=[v])
   return dict(executions=[])
  raise AssertionError(url)
 b.request=request;b.journal='mpa-t-client-001.ozon_raw.OZON_INGESTION_RUNS';b.select=lambda *a:[dict(n=1 if fault=='journal_effect' else 0)]
 with pytest.raises(BF.B.EvidenceError):R.live_freeze(b,p,m,root)
 assert objects==before


# ── v4: FAILED holder (not a timeout) with a receipted page pending (Heldi STOP aaf68ede) ───
def v4(m,p,b,root,objects,**change):
 """The fixture's certificate re-scoped to v4: page 1 of the fbo leaf receipted, not reconciled."""
 sh=p['last_source']['shard'];doc=next(r for r in root if r['kind']=='CHUNK_PLAN')['payload']['plan']
 intent=dict(version=D.VERSION,root_hash=sh,kind='DISPATCH_INTENT',sequence=1,payload=dict(preparation=dict(run_id='bf-example',lease_generation=1,ack_hash=doc['ack_hash'])))
 receipt=dict(version=D.VERSION,root_hash=sh,kind='DISPATCH_RECEIPT',sequence=1,payload=dict(plan=doc,receipt=dict(run_id='bf-example',lease_generation=1,ack_hash=doc['ack_hash'])))
 root[:]=[r for r in root if r['kind']!='CHUNK_COMPLETE'];shard=[intent,receipt]
 b.store.history=lambda r,**kw:root if r==m['hash'] else shard
 q=deepcopy(p);q.update(version=4,type=R.PENDING_TYPE,predicates=dict.fromkeys(R.PENDING_PREDICATES,True),
  protected_root_hashes=sorted(D.digest(r) for r in root),last_source=dict(index=p['last_source']['index'],shard=sh,intent_hash=D.digest(intent),receipt_hash=D.digest(receipt),sequence=1))
 q.update(change)
 return R.sealed({k:v for k,v in q.items() if k!='hash'}),shard


def test_v4_certificate_is_closed_and_bounded():
 m,p,b,root,objects,*_=fixture();q,_=v4(m,p,b,root,objects)
 assert R.header(q,m) is q and R.validate(q,b,m,root,final=False) is q
 import json
 ref=json.loads(R.value(q)[1]);assert ref['type']=='CONTROLLER_CLOSURE_CERTIFICATE_REF' and ref['certificate_hash']==q['hash']
 for bad in (dict(predicates=dict.fromkeys(R.SEQUENCE_PREDICATES,True)),dict(type=R.SEQUENCE_TYPE),dict(source_budget=1),
             dict(new_source=p['claim']['source'])):
  r,_=v4(m,p,b,root,objects,**bad)
  with pytest.raises(BF.B.EvidenceError):R.header(r,m)
 for field in sorted(R.PENDING_PREDICATES):
  r,_=v4(m,p,b,root,objects);r['predicates'][field]=None;r=R.sealed({k:v for k,v in r.items() if k!='hash'})
  with pytest.raises(BF.B.EvidenceError,match='unknown mandatory'):R.publish(b,r,m)


def test_v4_pending_proof_survives_the_ordinary_reconcile_and_rejects_foreign_pages():
 m,p,b,root,objects,*_=fixture();q,shard=v4(m,p,b,root,objects);before=deepcopy(objects)
 s=q['last_source']
 shard.append(dict(version=D.VERSION,root_hash=s['shard'],kind='RECONCILED',sequence=1,payload=dict(persisted_reconciled=True)))
 assert R.validate_pending(q,b,m,root,s) is q                       # the next wake reconciles the page: still accepted
 for bad in (dict(s,sequence=2),dict(s,intent_hash='5'*64),dict(s,receipt_hash='5'*64),dict(s,shard='6'*64),dict(s,index=s['index']-1),dict(s,recon_hash='7'*64)):
  with pytest.raises(BF.B.EvidenceError):R.validate_pending(q,b,m,root,bad)
 assert objects==before


@pytest.mark.parametrize('fault',['valid_failure','timeout','other_exit','other_task','success','cancelled'])
def test_v4_accepts_only_the_exact_holder_exit_code_one_failure(monkeypatch,tmp_path,fault):
 from tools.tenancy import cloud_controller as C
 m,p,b,root,objects,new,x=fixture();old=deepcopy(new)
 old.update(source_sha=p['claim']['source'],image=p['claim']['image']);old.pop('controller_timeout_seconds');old['verification'].pop('terminal_controller_adapter')
 path=tmp_path/'infra/tenant/releases/backfill';path.mkdir(parents=True);(path/(old['source_sha']+'.json')).write_text(D.encoded(old))
 monkeypatch.setattr(BF,'REPO',tmp_path)
 settings=dict(release=old['source_sha'],root_hash=m['hash'],scheduler_state='ENABLED')
 objects[C.descriptor_name(old['source_sha'],m['hash'],'ENABLED')]=({},D.encoded(dict(settings=settings,release=old)))
 task=p['claim']['execution'].rsplit('/',1)[1]
 message=f'Task {task}-task0 failed with exit code: 1 and message: The container exited with an error.'
 holder=x[0];holder.update(completionTime=NOW.isoformat(),failedCount=1,conditions=[dict(type='Completed',state='CONDITION_FAILED',message=message)])
 if fault=='timeout':holder['conditions'][0]['message']='The configured timeout was reached.'
 if fault=='other_exit':holder['conditions'][0]['message']=message.replace('code: 1 ','code: 137 ')
 if fault=='other_task':holder['conditions'][0]['message']=message.replace(task,'tenant-backfill-controller-zzzzz')
 if fault=='success':holder.update(failedCount=0,succeededCount=1)
 if fault=='cancelled':holder['cancelledCount']=1
 if fault=='valid_failure':assert R.terminal_holder(b,p['claim'],timeout=False)==holder
 else:
  with pytest.raises(BF.B.EvidenceError):R.terminal_holder(b,p['claim'],timeout=False)
 if fault=='timeout':assert R.terminal_timeout(b,p['claim'])==holder   # v1-v3 unchanged
 else:
  with pytest.raises(BF.B.EvidenceError):R.terminal_timeout(b,p['claim'])


@pytest.mark.parametrize('fault',['none','owner_marker','stop_row','own_publication','bfq','bfr','lease','ld','closure','foreign_closure','diagnostic','unknown_time','checkpoint','foreign_stop_row','overflow'])
def test_v4_holder_wrote_nothing_after_its_claim(fault):
 m,p,b,root,objects,*_=fixture();q,_=v4(m,p,b,root,objects);before=deepcopy(objects)
 start=CC.at(q['claim']['created_at']);after=start+timedelta(seconds=5);sh=q['last_source']['shard'];stop=q['stop_hashes'][0]
 extra={'none':[],'owner_marker':[('BF_SPEC_'+'f'*40+'_'+m['hash']+'_PAUSED',after)],'stop_row':[('BFR_'+stop,after)],
  'bfq':[(f'BFQ_{sh}_0000000002_DISPATCH_INTENT',after)],'bfr':[('BFR_'+'c'*64,after)],'lease':[('L_'+'d'*16+'_0002',after)],
  'ld':[('LD_'+'d'*16+'_0001',after)],'closure':[(CC.names(m['hash'],q['claim']['generation'])[1],after)],
  'foreign_closure':[(CC.names(m['hash'],q['claim']['generation'])[1],after)],'diagnostic':[('BFDC_'+'e'*64,after)],
  'own_publication':[(CC.names(m['hash'],q['claim']['generation'])[1],after),('BFR_'+D.digest(R.publication_record(q)),after)],
  'unknown_time':[('BFR_'+'c'*64,None)],'checkpoint':[],'foreign_stop_row':[],'overflow':[]}[fault]
 close=CC.names(m['hash'],q['claim']['generation'])[1]      # an interrupted publication left its own exact closure
 if fault=='own_publication':objects[close]=({'kind':'controller_close','root':m['hash'][:16]},D.encoded(q['closure']))
 if fault=='foreign_closure':objects[close]=({'kind':'controller_close','root':m['hash'][:16]},D.encoded(dict(q['closure'],status='STOPPED')))
 before=deepcopy(objects)
 original=b.tables.list_tables
 b.tables.list_tables=lambda ds,with_expiry=False:original(ds,with_expiry)+[(n,{},at,None) for n,at in extra]
 rows={'checkpoint':[dict(backfill_id='ab739857b0ae170f',entity='supplies')],'foreign_stop_row':[dict(backfill_id='e'*16,entity='orchestration')],'stop_row':[dict(backfill_id=stop[:16],entity='orchestration')],
       'own_publication':[dict(backfill_id=D.digest(R.publication_record(q))[:16],entity='orchestration')],
       'overflow':[dict(backfill_id=stop[:16],entity='orchestration')]*1001}.get(fault,[])
 b.select=lambda sql,params:rows
 if fault in {'none','owner_marker','stop_row','own_publication'}:assert R.no_holder_effect(b,q)
 else:
  with pytest.raises(BF.B.EvidenceError):R.no_holder_effect(b,q)
 assert objects==before


@pytest.mark.parametrize('fault',['valid','later_election','reconciled_record','reconciled_fence','elected_after_claim','receipt_after_claim','released','foreign_lease','complete','foreign_journal','late_journal','prefix_journal'])
def test_v4_pending_page_is_preserved_never_settled(fault):
 m,p,b,root,objects,*_=fixture();q,shard=v4(m,p,b,root,objects)
 s=q['last_source'];start=CC.at(q['claim']['created_at']);before_claim=start-timedelta(seconds=60)
 doc=next(r for r in root if r['kind']=='CHUNK_PLAN')['payload']['plan'];receipt=shard[1]['payload']['receipt']
 prefix=BF.B.digest(['BOUNDED_PILOT_EXCLUSIVE',q['project']])[:16]
 objects[BF.CK.lease_name(prefix,1)]=({'owner':'other-run' if fault=='foreign_lease' else 'bf-example'},D.encoded(dict(ack_hash=doc['ack_hash'])))
 if fault=='released':objects[f'LD_{prefix}_0001']=({'owner':'bf-example'},D.encoded(dict(ack_hash=doc['ack_hash'])))
 times={f"BFQ_{s['shard']}_0000000001_DISPATCH_INTENT":start+timedelta(seconds=1) if fault=='elected_after_claim' else before_claim,
        f"BFQ_{s['shard']}_0000000001_DISPATCH_RECEIPT":start+timedelta(seconds=1) if fault=='receipt_after_claim' else before_claim}
 if fault=='reconciled_fence':times[f"BFQ_{s['shard']}_0000000001_RECONCILED"]=start+timedelta(seconds=9)
 original=b.tables.list_tables
 b.tables.list_tables=lambda ds,with_expiry=False:original(ds,with_expiry)+[(n,{},at,None) for n,at in times.items()]
 if fault=='later_election':shard.append(dict(version=D.VERSION,root_hash=s['shard'],kind='DISPATCH_INTENT',sequence=2,payload={}))
 if fault=='reconciled_record':shard.append(dict(version=D.VERSION,root_hash=s['shard'],kind='RECONCILED',sequence=1,payload={}))
 state={'complete':fault=='complete','sequence':0}
 done=(start+timedelta(seconds=3)).isoformat();b.journal='mpa-t-client-001.ozon_raw.OZON_INGESTION_RUNS'
 rows=[dict(run='bf-example-u1967',started=(start+timedelta(seconds=1)).isoformat()),dict(run='bf-example',started=(start-timedelta(seconds=30)).isoformat())]
 if fault=='foreign_journal':rows.append(dict(run='bf-other-u1',started=(start+timedelta(seconds=1)).isoformat()))
 if fault=='late_journal':rows.append(dict(run='bf-example-u1968',started=(start+timedelta(seconds=4)).isoformat()))
 if fault=='prefix_journal':rows.append(dict(run='bf-example2-u1',started=(start+timedelta(seconds=1)).isoformat()))
 b.select=lambda sql,params:rows
 before=deepcopy(objects)
 if fault=='valid':assert R.live_pending(b,q,m,root,shard,shard[1],doc,state,receipt,prefix,done)
 else:
  with pytest.raises(BF.B.EvidenceError):R.live_pending(b,q,m,root,shard,shard[1],doc,state,receipt,prefix,done)
 assert objects==before
