from datetime import date,datetime,timezone
from types import SimpleNamespace
from copy import deepcopy
import pytest
from tools.tenancy import current_controller as C,current_policy as P,current_contract as K,durable_plan as D

NOW=datetime(2026,10,12,17,tzinfo=timezone.utc)
ROOT='1'*64
ACK='2'*64
class Store:
 def __init__(self,records=()):self.records=list(records);self.calls=[]
 def history(self,root):return [r for r in self.records if r['root_hash']==root]
 def commit(self,root,kind,seq,payload,now):
  r=dict(version=D.VERSION,root_hash=root,kind=kind,sequence=seq,payload=deepcopy(payload))
  self.calls.append(r)
  if r not in self.records:self.records.append(r)
  return D.digest(r)

def fixture():
 doc={'ack_hash':ACK,'runtime_plan':{'entity':'catalog','observation_date':'2026-10-12','plan_id':'3'*64}}
 item={'plan':doc,'shard':ROOT};store=Store();calls=[]
 b=SimpleNamespace(current_root='4'*64,current_execution='projects/mpa-t-client-001/locations/europe-west1/jobs/tenant-backfill-controller/executions/tenant-backfill-controller-test',store=store,clock=lambda:NOW,preflight=lambda leaf:calls.append('preflight'),quota=lambda *a:{'status':'ELIGIBLE','allowance':0},active_runtime_execution=lambda:False,state=lambda doc:{'complete':False},start=None)
 def start(doc,before,after):
  prep=dict(run_id='bf-00000000-0000-0000-0000-000000000001',lease_generation=1,ack_hash=ACK,job='job',overrides={})
  before(prep)
  assert store.history(ROOT)[0]['kind']=='DISPATCH_INTENT'
  calls.append('source_dispatch')
  after({k:prep[k] for k in ('run_id','lease_generation','ack_hash')}|{'operation':'operation'})
 b.start=start
 return b,item,calls

def test_current_intent_elected_before_only_source_dispatch_and_receipt():
 b,item,calls=fixture();result=C.source_tick(b,{'hash':'5'*64},date(2026,10,12),item)
 assert result['source_dispatches']==1 and calls==['preflight','source_dispatch']
 assert [r['kind'] for r in b.store.calls]==['DISPATCH_INTENT','DISPATCH_RECEIPT']
 assert all(r['root_hash']==ROOT for r in b.store.calls) # No historical rewrite.

def test_uncertain_current_dispatch_is_never_retried():
 b,item,calls=fixture()
 def uncertain(doc,before,after):
  before({'run_id':'bf-00000000-0000-0000-0000-000000000001','lease_generation':1,'ack_hash':ACK,'job':'job','overrides':{}})
  calls.append('source_dispatch')
  raise OSError('uncertain response')
 b.start=uncertain
 from tools.tenancy.controller_diagnostics import GateFailure
 with pytest.raises(GateFailure) as failure:C.source_tick(b,{'hash':'5'*64},date(2026,10,12),item)
 assert failure.value.diagnostic['stage']=='SOURCE_DISPATCH_BOUNDARY'
 # Durable intent survives the lost reply. A fresh wake fails closed.
 with pytest.raises(Exception):C.source_tick(b,{'hash':'5'*64},date(2026,10,12),item)
 assert calls.count('source_dispatch')==1

def test_known_quota_wait_has_zero_source_and_can_yield_to_independent_work():
 b,item,calls=fixture();b.quota=lambda *a:{'status':'WAITING','allowance':0,'eligible_at':'2026-10-12T18:00:00+00:00'}
 result=C.source_tick(b,{'hash':'5'*64},date(2026,10,12),item)
 assert result['status']=='WAITING' and 'source_dispatch' not in calls and not b.store.calls

def test_active_current_source_never_creates_a_second_dispatch():
 b,item,calls=fixture();b.active_runtime_execution=lambda:True
 result=C.source_tick(b,{'hash':'5'*64},date(2026,10,12),item)
 assert result['status']=='MONITORING' and 'source_dispatch' not in calls

def test_absent_operational_mode_preserves_historical_path_without_new_authority():
 b=SimpleNamespace(c={'orchestration':{'job':{'env':{'CONTROLLER_SOURCE_SHA':'a'*40}}}})
 assert C.policy(b) is None
 assert C.wake(b,[],{},set(),set()) is None

def test_unknown_mode_fails_before_any_source_or_metadata_access():
 b=SimpleNamespace(c={'orchestration':{'job':{'env':{'CURRENT_DATA_MODE':'unknown'}}}})
 with pytest.raises(Exception):C.policy(b)

def test_installed_current_adapter_qualification_properties():
 from tools.tenancy.current_image_check import check
 v=check()
 assert v['current_elected_plan_authorization']=='PASS'
 assert v['current_lost_dispatch_no_repeat']=='PASS'
 assert v['current_unknown_fbo_denied']=='PASS'
