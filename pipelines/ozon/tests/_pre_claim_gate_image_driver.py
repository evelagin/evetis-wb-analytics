"""Fault-inject actual archived controller gate BEFORE claim, network disabled.

Proves the barrier property; never claims to reproduce a missing historical
exception or response. No credentials, source dispatch or source state exists.
"""
import sys,json
from types import SimpleNamespace
from datetime import datetime,timezone
from unittest.mock import patch
sys.path.insert(0,'/app')
from tools.tenancy import cloud_controller as C,full_controller as H,tenant_backfill as BF,durable_plan as D
from pipelines.ozon.runtime import full_resume as FR
m=FR.manifest();effects=[]
record=dict(version=D.VERSION,root_hash=m['hash'],kind='FULL_MANIFEST',sequence=0,payload=m)
def denied(*a,**k):raise AssertionError('source/network/credentials denied')
b=SimpleNamespace(c=BF.target(m['tenant']),clock=lambda:datetime.now(timezone.utc),current_execution='synthetic-pre-claim',
    store=SimpleNamespace(history=lambda root,**kw:[record],commit=lambda *a:effects.append(a)),request=denied,dispatch=denied)
with patch.object(C,'bootstrap',return_value=(b,m['hash'])),patch.object(H,'verify_authority',side_effect=BF.B.EvidenceError('synthetic safe gate failure')):
    assert C.main()==2
assert len(effects)==1 and effects[0][1]=='STOPPED' and not hasattr(b,'wake_claim')
source=(BF.REPO/'CONTROLLER_SOURCE_SHA').read_text().strip()
print(json.dumps({'archived_pre_claim_barrier':'PASS','compiled_source':source,'source_dispatches':0,'credentials':0,'claim_created':False,'source_state_writes':0,'fault_injection':'PROPERTY_ONLY_NOT_HISTORICAL_TRACE'}))
