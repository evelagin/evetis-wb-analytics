"""Platform termination during reporting must not orphan completed control work."""
from copy import deepcopy
from types import SimpleNamespace
import pytest
from tools.tenancy import cloud_controller as C,full_controller as H,controller_cadence as CC
from tools.tenancy.controller_cadence_image_check import protocol

class PlatformTermination(BaseException):pass

def setup(monkeypatch):
 m,_,_,make,_,objects,_=protocol();b=make(0);claim=CC.acquire(b)
 b.store=SimpleNamespace(history=lambda root:[{'kind':'FULL_MANIFEST'}])
 monkeypatch.setattr(H,'wake',lambda *a:({'status':'CHUNK_COMPLETE','source_dispatches':0},b))
 return m,b,claim,objects

def test_durable_close_precedes_reporting_even_when_platform_kills_observer(monkeypatch):
 m,b,claim,objects=setup(monkeypatch);original=deepcopy(objects[CC.names(m['hash'],1)[0]])
 def terminated(*a):
  assert CC.closed(b,claim)
  raise PlatformTermination()
 monkeypatch.setattr(H,'monitoring',terminated)
 with pytest.raises(PlatformTermination):
  C.bounded_wake(m['hash'],b,decision_complete=lambda result:C.close_decision(b,result))
 assert b.wake_claim is None and CC.closed(b,claim)
 assert objects[CC.names(m['hash'],1)[0]]==original

def test_action_failure_cannot_publish_a_completed_control_closure(monkeypatch):
 m,b,claim,objects=setup(monkeypatch);before=deepcopy(objects)
 monkeypatch.setattr(H,'wake',lambda *a:(_ for _ in ()).throw(C.BF.B.EvidenceError('unproven source')))
 with pytest.raises(C.BF.B.EvidenceError):
  C.bounded_wake(m['hash'],b,decision_complete=lambda result:C.close_decision(b,result))
 assert objects==before and not CC.closed(b,claim)

def test_diagnostic_child_retains_shared_control_fence(monkeypatch):
 m,b,claim,objects=setup(monkeypatch);before=deepcopy(objects)
 b.wake_activity={'diagnostic_active':True,'dispatch_attempts':1}
 C.close_decision(b,{'status':'MONITORING','source_dispatches':0})
 assert objects==before and not CC.closed(b,claim)

def test_control_close_preserves_actual_dispatch_attempt_count(monkeypatch):
 m,b,claim,objects=setup(monkeypatch)
 b.wake_activity={'dispatch_attempts':1}
 C.close_decision(b,{'status':'WAITING_READ_RETRY','source_dispatches':0})
 from tools.tenancy.validation import parse_tenant_json
 value=parse_tenant_json(objects[CC.names(m['hash'],1)[1]][1])
 assert value['source_dispatches']==1 and value['status']=='WAITING_READ_RETRY'
