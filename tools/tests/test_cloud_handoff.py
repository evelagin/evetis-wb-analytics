"""Exact staged QF Supplies drain; no source/network or owner reconciliation."""
import copy
import json
from datetime import datetime,timezone
from types import SimpleNamespace
import pytest
from tools.tenancy import tenant_backfill as B,cloud_controller as C,orchestration_contract as O

NOW=datetime(2026,10,7,18,10,tzinfo=timezone.utc)

@pytest.fixture
def handoff():
    doc=next(d for d in B.QF.manifest()['plans'] if d['runtime_plan']['entity']=='supplies')
    c=copy.deepcopy(B.target('client_001'))
    source=c['orchestration']['job']['env']['CONTROLLER_SOURCE_SHA']
    release=json.loads((B.REPO/'infra/tenant/releases/backfill'/f'{source}.json').read_text())
    release['controller_implementation_hash']=O.implementation_hash(B.REPO)
    release['runtime_implementation_hash']=B.B.implementation_hash()
    settings={'release':source,'root_hash':B.QF.ROOT,'scheduler_state':'PAUSED'}
    c['orchestration']=O.block(c,settings,B.REPO,release)
    base,_=B.resources(c)
    receipt={'run_id':'bf-synthetic-terminal','lease_generation':70,'ack_hash':doc['ack_hash'],'operation':base+'/operations/synthetic'}
    prep={k:receipt[k] for k in ('run_id','lease_generation','ack_hash')}
    records=[{'kind':'DISPATCH_INTENT','sequence':39,'payload':{'plan':doc,'preparation':prep}},
             {'kind':'DISPATCH_RECEIPT','sequence':39,'payload':{'plan':doc,'receipt':receipt}}]
    old=json.loads((B.REPO/'infra/tenant/releases/ozon/06679f5.json').read_text())
    execution={'startTime':'2026-10-07T18:00:00Z','completionTime':'2026-10-07T18:05:00Z','succeededCount':1,
               'template':{'containers':[{'image':old['image']}]}}
    metadata={'creationTime':str(int(datetime(2026,10,7,18,8,tzinfo=timezone.utc).timestamp()*1000)),
              'description':json.dumps({'settings':settings,'release':release})}
    reads=[]
    b=object.__new__(C.Backend);b.c=c;b.current_execution=base+'/jobs/tenant-backfill-controller/executions/synthetic'
    b.clock=lambda:NOW;b.store=SimpleNamespace(history=lambda root:copy.deepcopy(records))
    def read(method,url):
        assert method=='GET' and url.endswith(C.descriptor_name(source,B.QF.ROOT,'PAUSED'))
        reads.append(url);return copy.deepcopy(metadata)
    b.request=read
    return SimpleNamespace(b=b,c=c,doc=doc,receipt=receipt,execution=execution,metadata=metadata,records=records,reads=reads)


def test_exact_terminal_prior_qualified_supplies_image_can_be_drained_in_cloud(handoff):
    p=handoff
    assert B.verify_paused_supplies_handoff(p.doc,p.c,p.receipt,p.execution,p.b)==p.execution['template']['containers'][0]['image']
    assert len(p.reads)==1


@pytest.mark.parametrize('change',[
    lambda p:setattr(p,'b',None),
    lambda p:setattr(p.b,'current_execution',None),
    lambda p:p.b.c['orchestration']['job']['env'].update(HISTORICAL_SCHEDULER_STATE='ENABLED'),
    lambda p:p.b.c['orchestration']['job']['env'].update(BACKFILL_ROOT_HASH='8'*64),
    lambda p:p.execution.update(failedCount=1),
    lambda p:p.execution.update(succeededCount=0),
    lambda p:p.execution.pop('completionTime'),
    lambda p:p.execution.update(completionTime='2026-10-07T18:09:00Z'),
    lambda p:p.execution.update(startTime='2026-10-07T12:00:00Z'),
    lambda p:p.execution['template']['containers'][0].update(image='unregistered'),
    lambda p:p.metadata.update(creationTime=None),
    lambda p:p.metadata.update(description='{}'),
    lambda p:p.records.append(copy.deepcopy(p.records[1])),
    lambda p:p.records[0]['payload']['preparation'].update(run_id='foreign'),
    lambda p:setattr(p,'doc',B.QF.accepted_doc(B.QF.SKU)),
])
def test_historical_handoff_denies_other_scope_or_unproven_provenance(handoff,change):
    p=handoff;change(p)
    with pytest.raises(B.B.EvidenceError):B.verify_paused_supplies_handoff(p.doc,p.c,p.receipt,p.execution,p.b)
