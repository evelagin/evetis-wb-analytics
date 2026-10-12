from copy import deepcopy
from types import SimpleNamespace
from datetime import timedelta
import pytest
from tools.tenancy import durable_plan as D,tenant_backfill as BF,orchestration_contract as O
from tools.tenancy import controller_cadence as CC,fbo_diagnostic_owner as Q,full_fbo_diagnostic_closure as Z
from tools.tenancy.cloud_tick import OverlapWait
from tools.tenancy.fbo_diagnostic_image_check import diagnostic_policy,diagnostic_protocol as protocol

def test_diagnostic_holds_the_same_CAS_until_child_terminal():
    m,r,c,backend,x,objects,commits=protocol();b=backend(0);p=diagnostic_policy(m,c,b.clock())
    objects['BFDA_'+p['hash']]=({'kind':'fbo_diagnostic','root':m['hash'][:16]},D.encoded(p))
    x[0]['template']['containers'][0]['args']=['--fbo-window-diagnostic',p['hash']]
    CC.acquire(b)
    with pytest.raises(OverlapWait):CC.acquire(backend(1))
    # Only a terminal child permits this caller to close the shared fence.
    child=dict(name='child',taskCount=1,template=dict(serviceAccount='sa-ozon-runtime@'+m['project']+'.iam.gserviceaccount.com',containers=[dict(image=p['runtime_image'],args=['--fbo-window-diagnostic',p['hash']],env=[dict(name=k,value=v) for k,v in dict(INGESTION_RUN_ID=p['run_id'],ENTITIES='fbo_postings',TENANT_BINDING_REQUIRED='1',STRICT_PAGE_CAPS='1').items()])]))
    pending=deepcopy(child);terminal=dict(child,completionTime=b.clock().isoformat(),succeededCount=1);responses=iter([pending,terminal]);b.request=lambda *a:next(responses)
    assert Q.await_terminal(b,p,{'execution':'child'},sleep=lambda n:None)['terminal_success'] is True
    b.request=backend(0).request;CC.close(b,dict(status='MONITORING',source_dispatches=0))
    x[0].update(completionTime=b.clock().isoformat(),succeededCount=1)
    assert CC.acquire(backend(1))['generation']==2 and not commits

@pytest.mark.parametrize('fault',['missing','hash','source','env','command'])
def test_unknown_diagnostic_controller_never_becomes_a_safe_overlap(fault):
    m,r,c,backend,x,objects,commits=protocol();b=backend(0);p=diagnostic_policy(m,c,b.clock())
    objects['BFDA_'+p['hash']]=({'kind':'fbo_diagnostic','root':m['hash'][:16]},D.encoded(p))
    x[0]['template']['containers'][0]['args']=['--fbo-window-diagnostic',p['hash']]
    if fault=='missing':objects.clear()
    if fault=='hash':x[0]['template']['containers'][0]['args'][1]='f'*64
    if fault=='source':p['controller_source']='f'*40;objects['BFDA_'+p['hash']]=(objects['BFDA_'+p['hash']][0],D.encoded(p))
    if fault=='env':x[0]['template']['containers'][0]['env'].append(dict(name='FOREIGN',value='1'))
    if fault=='command':x[0]['template']['containers'][0]['command']=['foreign']
    with pytest.raises((BF.B.EvidenceError,ValueError)):CC.acquire(b)
    assert not commits and not any(n.startswith('BFCW_') for n in objects)

def test_wait_does_not_consume_diagnostic_permission(monkeypatch):
    from tools.tenancy import cloud_controller as C
    b=SimpleNamespace(wake_claim=None);calls=[]
    monkeypatch.setattr(C,'bootstrap',lambda e:(b,'root'));monkeypatch.setattr(Q,'policy',lambda *a:{})
    monkeypatch.setattr(CC,'acquire',lambda b:(_ for _ in ()).throw(OverlapWait('WAIT_ACTIVE_CONTROLLER')))
    monkeypatch.setattr(Q,'dispatch',lambda *a:calls.append('dispatch'))
    assert Q.main('h',{})==0 and calls==[]

def test_current_quarantine_uses_immutable_descriptor_without_own_registration(monkeypatch,tmp_path):
    source='a'*40;m={'hash':'1'*64};settings=dict(release=source,root_hash=m['hash'],scheduler_state='ENABLED')
    r={'source_sha':source,'controller_implementation_hash':O.implementation_hash(BF.REPO),'runtime_implementation_hash':BF.B.implementation_hash()}
    b=SimpleNamespace(c={'datasets':{'tenant_locks':'locks'},'orchestration':{'job':{'env':dict(CONTROLLER_SOURCE_SHA=source,HISTORICAL_SCHEDULER_STATE='ENABLED')}}},tables=SimpleNamespace(get_table=lambda *a:({},D.encoded(dict(settings=settings,release=r)))))
    monkeypatch.setattr(BF,'REPO',tmp_path);monkeypatch.setattr(O,'implementation_hash',lambda repo:r['controller_implementation_hash'])
    assert Z.qualified_release(b,m,source)==r
    b.tables.get_table=lambda *a:({},D.encoded(dict(settings=dict(settings,root_hash='2'*64),release=r)))
    with pytest.raises(BF.B.EvidenceError):Z.qualified_release(b,m,source)
