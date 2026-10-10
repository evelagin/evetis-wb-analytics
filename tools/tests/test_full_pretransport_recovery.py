from copy import deepcopy
import pytest
from tools.tenancy import full_pretransport_image_check as CHECK,full_pretransport_recovery as R
from tools.tenancy import cloud_access as A,cloud_controller as C,tenant_backfill as BF,durable_plan as D
from tools.tenancy import full_controller as H,full_leaf_recovery as FL,full_cooldown_recovery as FC,full_policy_bootstrap_recovery as PB,orchestration_contract as O
from datetime import datetime,timezone
from types import SimpleNamespace

def test_packaged_real_boundary_and_unknown_recovery_blocks():assert set(CHECK.check().values())=={'PASS'}

@pytest.mark.parametrize('fault',['body_root','body_shard','body_extra','wrong_plan','wrong_intent','no_intent','no_authority','duplicate_env','uncommitted_context','other_run','bool_sequence','bool_index'])
def test_full_context_or_environment_drift_never_obtains_token_or_sends(fault):
 _,doc,job,body,auth,intent=CHECK.fixture();calls=[];tokens=[]
 class Tokens:
  def token(self,kind):tokens.append(kind);return 'SYNTHETIC_NON_CREDENTIAL'
 access=A.CloudAccess(BF.target('client_001'),Tokens(),lambda *a:calls.append(a))
 if fault.startswith('body_'):
  values=next(x for x in body['overrides']['containerOverrides'][0]['env'] if x['name']=='BACKFILL_FULL_AUTHORITY')
  q=D.parse_tenant_json(values['value'])
  q[{'body_root':'root','body_shard':'shard','body_extra':'unknown'}[fault]]='f'*64
  values['value']=D.encoded(q)
 if fault=='wrong_plan':auth['plan_id']='f'*64
 if fault=='wrong_intent':intent['sequence']=2
 if fault=='no_intent':intent=None
 if fault=='no_authority':auth=None
 if fault=='duplicate_env':body['overrides']['containerOverrides'][0]['env'].append(deepcopy(body['overrides']['containerOverrides'][0]['env'][0]))
 if fault=='uncommitted_context':intent['kind']='WAITING'
 if fault=='other_run':intent['payload']['preparation']['run_id']='bf-'+'1'*8+'-'+'1'*4+'-'+'1'*4+'-'+'1'*4+'-'+'1'*12
 if fault=='bool_sequence':auth['receipt_sequence']=True
 if fault=='bool_index':auth['index']=True
 with pytest.raises((A.TT.TableError,BF.B.EvidenceError)):access.dispatch(doc,job,body,full_authority=auth,full_intent=intent)
 assert not calls and not tokens

def test_real_backend_passes_only_context_of_the_committed_preparation():
 _,doc,job,body,auth,intent=CHECK.fixture();calls=[]
 class Tokens:
  def token(self,kind):return 'SYNTHETIC_NON_CREDENTIAL'
 b=C.Backend(A.CloudAccess(BF.target('client_001'),Tokens(),lambda *a:calls.append(a)),None)
 b.full_dispatch_authority=auth;b.full_dispatch_intent=intent
 b.dispatch(doc,job,body);assert len(calls)==1
 with pytest.raises(BF.B.EvidenceError,match='one source dispatch'):b.dispatch(doc,job,body)
 assert len(calls)==1

def test_closed_attempt_never_fabricates_success_or_allows_unknown_intent():
 _,doc,_,_,auth,intent=CHECK.fixture()
 with pytest.raises(BF.B.EvidenceError):R.verify_shard(dict(R.EXACT,type=R.TYPE,source_budget=0),[intent])
 with pytest.raises(BF.B.EvidenceError):D.decide_tick([intent,dict(intent,kind=R.SHARD_KIND)],auth['shard'],{'status':'ELIGIBLE'},verified_pretransport=[{}])

def protocol(monkeypatch):
 m,doc,_,_,_,intent=CHECK.fixture();now=datetime(2026,10,10,15,tzinfo=timezone.utc)
 c=deepcopy(BF.target('client_001'));release=D.parse_tenant_json((BF.REPO/'infra/tenant/releases/backfill'/f"{R.EXACT['original_source']}.json").read_text())
 release.update(source_sha='2'*40,image=release['image'].split('@')[0]+'@sha256:'+'2'*64,controller_implementation_hash=O.implementation_hash(BF.REPO))
 settings={'release':release['source_sha'],'root_hash':m['hash'],'scheduler_state':'PAUSED'};c['orchestration']=O.block(c,settings,BF.REPO,release)
 def record(kind,payload,seq=0):return {'version':D.VERSION,'root_hash':m['hash'],'kind':kind,'sequence':seq,'payload':payload}
 stop=record('STOPPED',{'reason':'CONTROLLER_GATE_OR_EVIDENCE_FAILURE','controller_execution':R.EXACT['execution'],'diagnostic':{'version':1,'stage':'SOURCE_DISPATCH_BOUNDARY','category':'CLOUD_METADATA'}})
 assert D.digest(stop)==R.EXACT['stop_hash']
 records=[record('FULL_MANIFEST',m),stop,record('DEPENDENCY_PLAN',{'day':'2026-10-10','plan':doc,'shard_root':R.EXACT['shard']})];sh=[deepcopy(intent)]
 p=R.sealed(dict(R.EXACT,version=1,type=R.TYPE,tenant=m['tenant'],project=m['project'],manifest_hash=D.digest(m),new_source=release['source_sha'],
  new_image=release['image'],new_implementation_hash=release['controller_implementation_hash'],runtime_image=release['runtime_image'],owner_decision_sha256='3'*64,
  packaged_probe_hash='4'*64,replacement_check_hash='5'*64,protected_record_hashes=sorted(D.digest(r) for r in records),predicates={k:True for k in R.PREDICATES},
  verified_at=now.isoformat(),source_budget=0,source_state_hash=D.digest(BF.B.initial(doc['runtime_plan'])),closure_hash='6'*64))
 p['closure_hash']=D.digest(R.closure(p));p=R.sealed({k:v for k,v in p.items() if k!='hash'})
 original={'status':'RUNNING','lease_owner':p['run_id'],'error_code':None,'evidence_json':D.encoded({'mode':'BOUNDED_PILOT','proof':None})};rows=[original]
 objects={'L_50c5d90dbf43ac9c_0170':({'owner':p['run_id']},D.encoded({'mode':'BOUNDED_PILOT','ack_hash':p['ack_hash']})),
  C.descriptor_name(p['new_source'],p['root'],'PAUSED'):({},D.encoded({'settings':settings,'release':release}))}
 effects=[];journal=[{'n':0}];child=[];active=[];state=BF.B.initial(doc['runtime_plan'])
 def create(ds,n,lb,desc):
  assert n.startswith(('BFPT_AUTH_','BFPT_')) or n=='LD_50c5d90dbf43ac9c_0170';effects.append(('marker',n))
  if n in objects:return False
  objects[n]=(lb,desc);return True
 def append(ds,t,rs):
  assert (ds,t)==('tenant_ops','BACKFILL_CHECKPOINTS') and len(rs)==1 and rs[0]['status']=='FAILED'
  effects.append(('checkpoint','FAILED'));rows.extend({k:r[k] for k in original} for r in rs)
 def commit(root,kind,seq,payload,at):
  assert (root,kind,seq) in {(p['root'],R.KIND,89),(p['shard'],R.SHARD_KIND,1)}
  r={'version':D.VERSION,'root_hash':root,'kind':kind,'sequence':seq,'payload':deepcopy(payload)};dst=records if root==p['root'] else sh
  if r not in dst:dst.append(r);effects.append(('record',kind))
  return D.digest(r)
 def request(method,url,body=None):
  assert method=='GET','unexpected cloud write/source transport'
  if url==BF.RUN_API+'/'+p['execution']:return {'failedCount':1,'completionTime':'2026-10-10T11:33:03Z','template':{'containers':[{'image':p['original_image'],'env':[{'name':k,'value':v} for k,v in {'CONTROLLER_SOURCE_SHA':p['original_source'],'BACKFILL_ROOT_HASH':p['root'],'TENANT_BINDING_REQUIRED':'1','STRICT_PAGE_CAPS':'1'}.items()]}]}}
  if '/jobs?pageSize=100' in url:return {'jobs':[{'state':'PAUSED'}]*4}
  if '/executions?' in url:return {'executions':deepcopy(child+active) if 'ozon-runtime-daily' in url else []}
  raise AssertionError(url)
 b=SimpleNamespace(c=c,current_execution=None,clock=lambda:now,journal=m['project']+'.ozon_raw.OZON_INGESTION_RUNS',request=request,state=lambda d:deepcopy(state),
  select=lambda sql,params:deepcopy(rows if 'SELECT DISTINCT status' in sql else journal),
  tables=SimpleNamespace(get_table=lambda ds,n:objects.get(n),list_tables=lambda ds:[(n,v[0],None) for n,v in objects.items()],create_marker=create,append=append),
  store=SimpleNamespace(history=lambda root,**k:deepcopy(records if root==p['root'] else sh),commit=commit))
 monkeypatch.setattr(FL,'load',lambda *a:[]);monkeypatch.setattr(FC,'load',lambda *a:[]);monkeypatch.setattr(PB,'load',lambda *a:[])
 monkeypatch.setattr(FL,'observer',lambda *a:SimpleNamespace(preflight=lambda *a:None));monkeypatch.setattr(H,'verify_authority',lambda *a:None)
 return b,m,p,records,sh,objects,rows,effects,journal,child,active,state

def test_owner_additive_closure_is_idempotent_retains_intent_and_running_checkpoint(monkeypatch):
 b,m,p,records,sh,objects,rows,effects,*_=protocol(monkeypatch);old=deepcopy((records,sh,objects,rows))
 assert R.publish(b,p,m)==p and R.load(b,m,records)==[p]
 assert records[:len(old[0])]==old[0] and sh[0]==old[1][0] and rows[0]==old[3][0]
 assert all(objects[k]==v for k,v in old[2].items())
 assert len(records)==len(old[0])+1 and len(sh)==2 and len(rows)==2
 assert not any(r['kind'] in {'DISPATCH_RECEIPT','RECONCILED','CHUNK_COMPLETE'} for r in sh+records)
 before=deepcopy(effects);assert R.publish(b,p,m)==p and effects==before
 assert D.decide_tick(sh,p['shard'],{'status':'ELIGIBLE'},verified_pretransport=[p])=={'action':'PREPARE_NEXT','sequence':2,'poll_only':False}

@pytest.mark.parametrize('key',sorted(R.PREDICATES))
@pytest.mark.parametrize('bad',[False,None,1,'UNKNOWN'])
def test_unknown_predicate_prevents_every_recovery_write(monkeypatch,key,bad):
 b,m,p,*rest=protocol(monkeypatch);p['predicates'][key]=bad;p=R.sealed({k:v for k,v in p.items() if k!='hash'})
 with pytest.raises(BF.B.EvidenceError):R.publish(b,p,m)
 assert not rest[4]

@pytest.mark.parametrize('fault',['positive_budget','foreign_stop','receipt','successor','later_lease','source_effect','child','active','source_state','missing_original','cloud_owner','unknown_stop'])
def test_effect_ambiguity_or_new_unsupported_risk_never_closes_intent(monkeypatch,fault):
 b,m,p,records,sh,objects,rows,effects,journal,child,active,state=protocol(monkeypatch)
 if fault=='positive_budget':p['source_budget']=1
 if fault=='foreign_stop':p['stop_hash']='f'*64
 if fault in ('positive_budget','foreign_stop'):p=R.sealed({k:v for k,v in p.items() if k!='hash'})
 if fault=='receipt':sh.append(dict(sh[0],kind='DISPATCH_RECEIPT'))
 if fault=='successor':sh.append(dict(sh[0],sequence=2))
 if fault=='later_lease':objects['L_50c5d90dbf43ac9c_0171']=({},'{}')
 if fault=='source_effect':journal[0]['n']=1
 if fault=='child':child.append({'name':'child','createTime':'2026-10-10T11:31:00Z','completionTime':'2026-10-10T11:32:00Z'})
 if fault=='active':active.append({'name':'active','createTime':'2026-10-10T11:31:00Z'})
 if fault=='source_state':state['sequence']=1
 if fault=='missing_original':rows.clear()
 if fault=='cloud_owner':b.current_execution='cloud-wake'
 if fault=='unknown_stop':records.append(dict(records[1],payload={'reason':'new unsupported incident'}))
 with pytest.raises(BF.B.EvidenceError):R.publish(b,p,m)
 assert not effects
