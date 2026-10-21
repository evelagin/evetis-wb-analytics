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
