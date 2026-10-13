"""Network-disabled deadline fault check against the packaged control CAS."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch
from tools.tenancy import cloud_controller as C,full_controller as H,controller_cadence as CC
from tools.tenancy.controller_cadence_image_check import protocol

class PlatformTermination(BaseException):pass

def check():
 m,_,_,make,_,objects,_=protocol();b=make(0);claim=CC.acquire(b)
 before=deepcopy(objects[CC.names(m['hash'],claim['generation'])[0]])
 b.store=SimpleNamespace(history=lambda root:[{'kind':'FULL_MANIFEST'}])
 def terminated(*args):
  assert CC.closed(b,claim)
  raise PlatformTermination()
 with patch.object(H,'wake',return_value=({'status':'CHUNK_COMPLETE','source_dispatches':0},b)),patch.object(H,'monitoring',side_effect=terminated):
  try:C.bounded_wake(m['hash'],b,decision_complete=lambda result:C.close_decision(b,result))
  except PlatformTermination:pass
  else:raise AssertionError('deadline fault not reached')
 assert b.wake_claim is None and CC.closed(b,claim)
 assert objects[CC.names(m['hash'],claim['generation'])[0]]==before
 return {'decision_closure_before_monitoring':'PASS','platform_deadline_preserves_control_closure':'PASS','original_claim_preserved':'PASS','deadline_probe_source_budget_zero':'PASS'}

if __name__=='__main__':
 import json
 print(json.dumps(check(),sort_keys=True))
