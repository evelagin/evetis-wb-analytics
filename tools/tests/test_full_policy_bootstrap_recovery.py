"""Exact owner closure cannot become a generic or source recovery authority."""
from copy import deepcopy
from datetime import datetime,timezone
from types import SimpleNamespace
import pytest
from tools.tenancy import full_policy_bootstrap_recovery as R,full_policy_bootstrap_image_check as CHECK
from tools.tenancy import durable_plan as D,tenant_backfill as BF,orchestration_contract as O
from tools.tenancy import controller_cadence as CC,cloud_controller as C,full_leaf_recovery as FL
from tools.tenancy import full_cooldown_recovery as FC,full_controller as H
from pipelines.ozon.runtime import full_resume as FR
from tools.tests.test_full_controller import pilot

NOW=datetime(2026,10,10,10,tzinfo=timezone.utc)


def protocol(monkeypatch):
    m=FR.manifest();c=deepcopy(BF.target('client_001'))
    old=D.parse_tenant_json((BF.REPO/'infra/tenant/releases/backfill'/f"{R.EXACT['original_source']}.json").read_text())
    release=deepcopy(old);release.update(source_sha='2'*40,image=old['image'].split('@')[0]+'@sha256:'+'2'*64,
        controller_implementation_hash=O.implementation_hash(BF.REPO))
    settings=dict(release=release['source_sha'],root_hash=m['hash'],scheduler_state='PAUSED')
    c['orchestration']=O.block(c,settings,BF.REPO,release)
    claim=CC.sealed(dict(version=1,tenant=m['tenant'],project=m['project'],root=m['hash'],policy_hash=D.digest(old['cadence_policy']),
        source=R.EXACT['original_source'],image=R.EXACT['original_image'],execution=R.EXACT['execution'],generation=1,
        created_at='2026-10-10T09:39:23.108630+00:00'))
    close=CC.sealed(dict(version=1,claim_hash=claim['hash'],status='STOPPED',source_dispatches=0,closed_at='2026-10-10T09:40:04.431242+00:00'))
    assert claim['hash']==R.EXACT['wake_claim_hash'] and close['hash']==R.EXACT['wake_close_hash']
    oldsettings=dict(settings,release=old['source_sha']);oldblock=O.block(c,oldsettings,BF.REPO,old)
    x=dict(name=R.EXACT['execution'],createTime='2026-10-10T09:38:42.677515Z',completionTime='2026-10-10T09:40:07.684053Z',
        failedCount=1,taskCount=1,template=dict(timeout='600s',maxRetries=0,serviceAccount=oldblock['accounts']['controller']['email'],
        containers=[dict(image=old['image'],command=['python','-m','tools.tenancy.cloud_controller'],env=[dict(name=k,value=v) for k,v in oldblock['job']['env'].items()])]))
    accepted=dict(hash=R.EXACT['failed4_certificate'],stop_hash='a'*64)
    def record(kind,payload):return dict(version=D.VERSION,root_hash=m['hash'],kind=kind,sequence=0,payload=payload)
    stop=record('STOPPED',dict(reason='CONTROLLER_GATE_OR_EVIDENCE_FAILURE',controller_execution=R.EXACT['execution']))
    assert D.digest(stop)==R.EXACT['stop_hash']
    records=[record('FULL_MANIFEST',m),record(FC.KIND,accepted),stop]
    objects={CC.names(m['hash'],1)[0]:CC.claim_value(claim),CC.names(m['hash'],1)[1]:({'kind':'controller_close','root':m['hash'][:16]},D.encoded(close)),
        C.descriptor_name(old['source_sha'],m['hash'],'PAUSED'):({},D.encoded(dict(settings=oldsettings,release=old))),
        C.descriptor_name(release['source_sha'],m['hash'],'PAUSED'):({},D.encoded(dict(settings=settings,release=release))),
        'BFCF_ACCEPTED_'+accepted['hash']:({},D.encoded(dict(proof=accepted)))}
    doc=BF.QF.accepted_doc(BF.QF.SKU)
    records.append(dict(version=D.VERSION,root_hash=m['hash'],kind='CHUNK_PLAN',sequence=88,payload=dict(plan=doc)))
    writes=[];reads=[];journal=[{'n':0}]
    def create(ds,n,labels,text):
        assert n.startswith(('BFPB_AUTH_','BFPB_'));writes.append(('marker',n))
        if n in objects:return False
        objects[n]=(labels,text);return True
    def request(method,url,body=None):
        assert method=='GET' and url==BF.RUN_API+'/'+x['name'];reads.append(url);return deepcopy(x)
    def commit(root,kind,seq,payload,at):
        assert (root,kind,seq)==(m['hash'],R.KIND,0),'source/checkpoint/reconciliation mutation'
        r=record(kind,deepcopy(payload))
        if r not in records:records.append(r);writes.append(('record',kind))
        return D.digest(r)
    b=SimpleNamespace(c=c,current_execution=None,clock=lambda:NOW,request=request,
        tables=SimpleNamespace(get_table=lambda ds,n:objects.get(n),create_marker=create),
        store=SimpleNamespace(history=lambda *a,**k:deepcopy(records),commit=commit),journal=m['project']+'.ozon_raw.OZON_INGESTION_RUNS',
        select=lambda sql,params:deepcopy(journal))
    monkeypatch.setattr(FL,'load',lambda *a:[]);monkeypatch.setattr(FC,'load',lambda *a:[accepted])
    monkeypatch.setattr(FL,'observer',lambda *a:SimpleNamespace(preflight=lambda leaf:reads.append('canonical-owner-preflight')))
    monkeypatch.setattr(H,'verify_authority',lambda *a:reads.append('canonical-full-authority'))
    p=R.sealed(dict(R.EXACT,version=1,type=R.TYPE,tenant=m['tenant'],project=m['project'],manifest_hash=D.digest(m),
        new_source=release['source_sha'],new_image=release['image'],new_implementation_hash=release['controller_implementation_hash'],runtime_image=release['runtime_image'],
        owner_decision_sha256='1'*64,packaged_probe_hash='2'*64,replacement_image_check_hash='3'*64,
        protected_record_hashes=sorted(D.digest(r) for r in records),protected_history_hash=D.digest(sorted(D.digest(r) for r in records)),
        predicates={k:True for k in R.PREDICATES},verified_at=NOW.isoformat(),source_budget=0))
    return b,m,p,records,objects,writes,reads,journal,x


def test_packaged_descriptor_and_unknown_predicates_are_network_free():assert set(CHECK.check().values())=={'PASS'}


def test_additive_owner_publish_keeps_stop_failed4_and_closed_wake_immutable(monkeypatch):
    b,m,p,records,objects,writes,reads,*_=protocol(monkeypatch);before=deepcopy((records,objects))
    assert R.publish(b,p,m)==p and R.load(b,m,records)==[p]
    assert records[:len(before[0])]==before[0] and all(objects[n]==v for n,v in before[1].items())
    assert len(records)==len(before[0])+1 and {r['kind'] for r in records}=={'FULL_MANIFEST',FC.KIND,'STOPPED','CHUNK_PLAN',R.KIND}
    prior=deepcopy(writes);assert R.publish(b,p,m)==p and writes==prior
    assert 'canonical-owner-preflight' in reads and 'canonical-full-authority' in reads


@pytest.mark.parametrize('key',sorted(R.PREDICATES))
@pytest.mark.parametrize('bad',[False,None,1,'UNKNOWN'])
def test_every_unknown_predicate_prevents_even_metadata_publication(monkeypatch,key,bad):
    b,m,p,records,objects,writes,*_=protocol(monkeypatch);p['predicates'][key]=bad;p=R.sealed({k:v for k,v in p.items() if k!='hash'})
    before=deepcopy((records,objects))
    with pytest.raises(BF.B.EvidenceError):R.publish(b,p,m)
    assert (records,objects)==before and not writes


@pytest.mark.parametrize('fault',['different_stop','missing_close','wrong_close','failed4_marker','new_journal','active_owner','unresolved_other','unverified_record','positive_source_budget','original_success','missing_protected'])
def test_unknown_new_risk_never_gets_same_class_or_bootstrap_exemption(monkeypatch,fault):
    b,m,p,records,objects,writes,reads,journal,x=protocol(monkeypatch)
    if fault=='different_stop':p['stop_hash']='f'*64;p=R.sealed({k:v for k,v in p.items() if k!='hash'})
    if fault=='missing_close':objects.pop(CC.names(m['hash'],1)[1])
    if fault=='wrong_close':objects[CC.names(m['hash'],1)[1]]=({},'{}')
    if fault=='failed4_marker':objects.pop('BFCF_ACCEPTED_'+R.EXACT['failed4_certificate'])
    if fault=='new_journal':journal[0]['n']=1
    if fault=='active_owner':b.current_execution='active'
    if fault=='unresolved_other':records.append(dict(records[2],payload=dict(reason='unknown new incident')))
    if fault=='unverified_record':records.append(dict(version=D.VERSION,root_hash=m['hash'],kind=R.KIND,sequence=0,payload=p))
    if fault=='positive_source_budget':p['source_budget']=1;p=R.sealed({k:v for k,v in p.items() if k!='hash'})
    if fault=='original_success':x.update(failedCount=0,succeededCount=1)
    if fault=='missing_protected':records.pop()
    before=deepcopy((records,objects))
    with pytest.raises(BF.B.EvidenceError):
        if fault=='unverified_record':R.load(b,m,records)
        else:R.publish(b,p,m)
    assert (records,objects)==before and not writes


def test_full_wake_recognizes_only_verified_exact_bootstrap_closure(pilot,monkeypatch):
    p=pilot;root=p.m['hash']
    stop=p.b.store.commit(root,'STOPPED',0,{'reason':'synthetic pre-source bootstrap'},p.b.clock())
    p.b.store.commit(root,R.KIND,0,{'stop_hash':stop},p.b.clock())
    seen=[]
    monkeypatch.setattr(R,'load',lambda b,m,r:seen.append('verified bootstrap') or [{'stop_hash':stop}])
    monkeypatch.setattr(H,'leaf_wake',lambda *a,**k:({'status':'QUIESCENT_PAUSED','source_dispatches':0},a[0]))
    result,_=H.wake(p.b,root)
    assert seen==['verified bootstrap'] and result['status']=='QUIESCENT_PAUSED'
    assert not p.dispatches and any(r['kind']=='STOPPED' for r in p.b.store.history(root))
    p.b.store.commit(root,'STOPPED',0,{'reason':'another unsupported incident'},p.b.clock())
    result,_=H.wake(p.b,root)
    assert result['status']=='STOPPED' and not p.dispatches


def test_full_policy_read_failure_has_typed_stage_without_private_details(pilot,monkeypatch):
    from tools.tenancy.controller_diagnostics import GateFailure,safe
    p=pilot;p.b.store.commit(p.m['hash'],'STOPPED',0,{'reason':'synthetic bootstrap'},p.b.clock())
    p.b.store.commit(p.m['hash'],R.KIND,0,{},p.b.clock())
    def unreadable(*a):raise FileNotFoundError('synthetic private path')
    monkeypatch.setattr(R,'load',unreadable)
    with pytest.raises(GateFailure) as caught:H.wake(p.b,p.m['hash'])
    assert safe(caught.value)=={'version':1,'stage':'MANIFEST_ROOT','category':'LOCAL_IO'}
    assert not p.dispatches


@pytest.mark.parametrize('prefix',['BFPB_','BFCW_','BFCX_'])
def test_bootstrap_and_wake_authority_cannot_expire(monkeypatch,prefix):
    c=BF.target('client_001');m=FR.manifest()
    b=H.Backend.__new__(H.Backend);b.c=c;b.manifest=m;b.current_execution=None;b.clock=lambda:NOW
    b.full_tables=SimpleNamespace(list_tables=lambda *a,**k:[(prefix+'synthetic',{},NOW,'unsafe-expiry')])
    b.request=lambda *a,**k:{}
    monkeypatch.setattr(H,'verify_authority',lambda *a:None)
    monkeypatch.setattr(BF,'validate_plan',lambda *a:c)
    monkeypatch.setattr(BF,'preflight',lambda *a,**k:None)
    with pytest.raises(BF.B.EvidenceError,match='authoritative marker expires'):
        b.preflight({'hash':m['hash'],'plans':[{'ack_hash':'1'*64}]})
