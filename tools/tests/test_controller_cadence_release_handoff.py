"""Release changes may observe an exact closed old wake, never reclaim it."""
from copy import deepcopy
from types import SimpleNamespace
import pytest
from tools.tenancy import controller_cadence as CC,orchestration_contract as O
from tools.tenancy import cloud_controller as C,tenant_backfill as BF,durable_plan as D
from tools.tenancy.controller_cadence_image_check import protocol,NOW


def closed_handoff(monkeypatch,tmp_path):
    m,r,c,make,x,objects,commits=protocol();leader=make(0);old=CC.acquire(leader)
    CC.close(leader,dict(status='STOPPED',source_dispatches=0))
    x[0].update(completionTime=NOW.isoformat(),failedCount=1)
    path=tmp_path/'infra/tenant/releases/backfill';path.mkdir(parents=True)
    (path/(r['source_sha']+'.json')).write_text(D.encoded(r));monkeypatch.setattr(BF,'REPO',tmp_path)
    settings=dict(release=r['source_sha'],root_hash=m['hash'],scheduler_state='ENABLED')
    name=C.descriptor_name(r['source_sha'],m['hash'],'ENABLED');objects[name]=({},D.encoded(dict(settings=settings,release=r)))
    newer=deepcopy(r);newer.update(source_sha='f'*40,image=r['image'].split('@')[0]+'@sha256:'+'f'*64)
    block=O.block(c,dict(settings,release=newer['source_sha']),tmp_path,newer)
    task=dict(timeout='600s',maxRetries=0,serviceAccount=block['accounts']['controller']['email'],
        containers=[dict(image=newer['image'],command=['python','-m','tools.tenancy.cloud_controller'],
            env=[dict(name=k,value=v) for k,v in block['job']['env'].items()])])
    for e in x[1:]:e['template']=deepcopy(task)
    b=make(1);b.c['orchestration']=block;original=b.request
    def request(method,url,body=None):
        assert method=='GET'
        if url.endswith('/jobs/'+O.JOB):return dict(template=dict(taskCount=1,template=deepcopy(task)))
        return original(method,url,body)
    b.request=request
    return b,old,x,objects,commits,name,r,path


def test_exact_registered_closed_historical_wake_preserved_across_release(monkeypatch,tmp_path):
    b,old,x,objects,commits,*_=closed_handoff(monkeypatch,tmp_path)
    prior=deepcopy(objects)
    claim=CC.acquire(b)
    assert claim['generation']==2 and claim['source']=='f'*40
    assert all(objects[n]==v for n,v in prior.items()) and not commits
    # Current authority verification never permits a historical current owner.
    with pytest.raises(BF.B.EvidenceError,match='scope/release differs'):
        CC.validate_claim(b,CC.claim_value(old))


@pytest.mark.parametrize('fault',['closure_missing','closure_corrupt','active','retry','image','descriptor','registration','policy'])
def test_unknown_historical_authority_cannot_become_release_or_wait(monkeypatch,tmp_path,fault):
    b,old,x,objects,commits,n,r,path=closed_handoff(monkeypatch,tmp_path)
    close=CC.names(old['root'],1)[1]
    if fault=='closure_missing':objects.pop(close)
    if fault=='closure_corrupt':objects[close]=({},'{}')
    if fault=='active':x[0].pop('completionTime')
    if fault=='retry':x[0]['retriedCount']=1
    if fault=='image':x[0]['template']['containers'][0]['image']='foreign'
    if fault=='descriptor':objects[n]=({},D.encoded(dict(settings={},release=r)))
    if fault=='registration':(path/(r['source_sha']+'.json')).unlink()
    if fault=='policy':
        r=deepcopy(r);r['cadence_policy']['owner_ack_sha256']='e'*64
        (path/(r['source_sha']+'.json')).write_text(D.encoded(r))
        d=dict(settings=dict(release=r['source_sha'],root_hash=old['root'],scheduler_state='ENABLED'),release=r)
        objects[n]=({},D.encoded(d))
    before=deepcopy(objects)
    with pytest.raises((BF.B.EvidenceError,ValueError,OSError)) as caught:CC.acquire(b)
    from tools.tenancy.cloud_tick import OverlapWait
    assert not isinstance(caught.value,OverlapWait)
    assert objects==before and not commits
