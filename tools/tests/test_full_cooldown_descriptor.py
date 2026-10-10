"""Current proof readers must work in pre-registration packaged images."""
from copy import deepcopy
from types import SimpleNamespace
import pytest
from tools.tenancy import full_cooldown_recovery as R, full_runtime_handoff as FH
from tools.tenancy import tenant_backfill as BF, durable_plan as D, orchestration_contract as O
from tools.tenancy import cloud_controller as C
from pipelines.ozon.runtime import full_resume as FR
from tools.tests.test_full_cooldown_recovery import fixture


def descriptor_fixture(monkeypatch):
    b,m,p,*_=fixture(monkeypatch)
    c=deepcopy(BF.target('client_001'));source=p['controller_source']
    runtime=FH.runtime_facts(c,m)
    release=dict(schema_version=3,source_sha=source,image=p['controller_image'],
        runtime_image=runtime['runtime_image'],runtime_implementation_hash=runtime['runtime_implementation_hash'],
        controller_implementation_hash=O.implementation_hash(BF.REPO),
        verification={k:'PASS' for k in ('ci','exact_image','offline_restart','lost_post_no_repeat',
            'quota_wait_no_source','tenant_isolation','reader_append_separation','full_history_adapter','recent_priority_adapter')})
    settings=dict(release=source,root_hash=m['hash'],scheduler_state='PAUSED')
    c['orchestration']=O.block(c,settings,BF.REPO,release)
    p.update(runtime,controller_implementation_hash=release['controller_implementation_hash'])
    p['hash']=D.digest({k:v for k,v in p.items() if k!='hash'})
    descriptor=dict(settings=settings,release=release);reads=[]
    def get(ds,n):
        reads.append((ds,n));return ({},D.encoded(descriptor))
    b.c=c;b.tables=SimpleNamespace(get_table=get)
    return b,m,p,descriptor,reads


def test_current_policy_uses_exact_descriptor_without_future_self_registration(monkeypatch):
    b,m,p,descriptor,reads=descriptor_fixture(monkeypatch)
    assert not (BF.REPO/'infra/tenant/releases/backfill'/f"{p['controller_source']}.json").exists()
    assert R.validate_policy(p,b,m)==p
    assert reads==[(b.c['datasets']['tenant_locks'],C.descriptor_name(p['controller_source'],m['hash'],'PAUSED'))]


def test_owner_prepare_can_supply_exact_qualified_release_before_descriptor_publication(monkeypatch):
    b,m,p,d,reads=descriptor_fixture(monkeypatch)
    b.tables.get_table=lambda *a:None
    assert R.validate_policy(p,b,m,release=d['release'])==p
    assert not reads
    changed=deepcopy(d['release']);changed['controller_implementation_hash']='f'*64
    with pytest.raises(BF.B.EvidenceError):R.validate_policy(p,b,m,release=changed)


@pytest.mark.parametrize('fault',['absent','settings','source','image','implementation','runtime','qualification','unknown'])
def test_current_descriptor_drift_or_unknown_remains_closed(monkeypatch,fault):
    b,m,p,d,reads=descriptor_fixture(monkeypatch)
    if fault=='absent':b.tables.get_table=lambda *a:None
    if fault=='settings':d['settings']['scheduler_state']='ENABLED'
    if fault=='source':d['release']['source_sha']='f'*40
    if fault=='image':d['release']['image']=d['release']['image'].split('@')[0]+'@sha256:'+'f'*64
    if fault=='implementation':d['release']['controller_implementation_hash']='f'*64
    if fault=='runtime':d['release']['runtime_implementation_hash']='f'*64
    if fault=='qualification':d['release']['verification']['exact_image']='UNPROVEN'
    if fault=='unknown':d['extra']='unknown'
    with pytest.raises((BF.B.EvidenceError,ValueError)):R.validate_policy(p,b,m)


def test_historical_policy_still_requires_immutable_registered_release(monkeypatch):
    b,m,p,*_=descriptor_fixture(monkeypatch)
    with pytest.raises(FileNotFoundError):R.validate_policy(p,b,m,historical=True)


def test_policy_loader_local_io_gets_safe_manifest_stage():
    from tools.tenancy.controller_diagnostics import gate,GateFailure,safe
    with pytest.raises(GateFailure) as caught:
        with gate('MANIFEST_ROOT'):raise FileNotFoundError('synthetic missing private path')
    assert safe(caught.value)=={'version':1,'stage':'MANIFEST_ROOT','category':'LOCAL_IO'}
    assert 'private' not in D.encoded(safe(caught.value))
