"""Priority races, fallback, preserved class-C proof and conservative READY gates."""
from copy import deepcopy
import json
from types import SimpleNamespace
import pytest
from tools.tenancy import recent_priority as P, full_controller as H, full_history as F
from tools.tenancy import durable_plan as D, tenant_backfill as BF, full_leaf_recovery as R
from tools.tenancy.full_leaf_image_check import fixture, NOW
from tools.tests.test_full_controller import pilot, item, completion
from tools.tests.test_full_leaf_recovery import observed


def policy(m):
    release=fixture()[6];release=deepcopy(release);release['schema_version']=3
    release['verification']['recent_priority_adapter']='PASS'
    return P.make(m,release,design_hash='a'*64,owner_ack_sha256='b'*64,
        historical_recovery_policies=[dict(source='c'*40,policy_hash='d'*64)],activated_at=NOW.isoformat()),release


def catalog(p):
    i,it=item(p);doc=it['plan'];proof=completion(p,i,doc)
    p.b.store.commit(p.m['hash'],'CHUNK_PLAN',i,it,p.b.clock())
    p.b.store.commit(p.m['hash'],'CHUNK_COMPLETE',i,proof,p.b.clock())
    p.b.store.commit(p.m['hash'],'SNAPSHOT_CERT',0,dict(day=str(p.b.clock().astimezone(BF.B.MSK).date()),plan_hash=doc['ack_hash'],coverage=proof['coverage']),p.b.clock())
    state=BF.B.initial(doc['runtime_plan']);state['complete']=True;state['sequence']=proof['sequence'];p.states[doc['ack_hash']]=state


def supplies(p):
    i=next(i for i,x in enumerate(p.m['programs']) if x['entity']=='supplies');_,it=item(p,i)
    p.b.store.commit(p.m['hash'],'CHUNK_PLAN',i,it,p.b.clock())
    return i,it


def test_installed_network_free_check():
    from tools.tenancy.recent_priority_image_check import check
    assert set(check().values())=={'PASS'}


@pytest.mark.parametrize('fault',['cutoff','scope','selection','parent','runtime','artifact','version','extra','duplicate_authority'])
def test_owner_overlay_is_exact_closed_and_never_changes_dates(fault):
    m=fixture()[0];p,r=policy(m)
    if fault=='cutoff':p['until']='2026-10-09'
    if fault=='scope':p['root']='a'*64
    if fault=='selection':p['selection'].pop()
    if fault=='parent':p['parents'].pop()
    if fault=='runtime':p['runtime_source']='e'*40
    if fault=='artifact':r['verification']['recent_priority_adapter']='UNPROVEN'
    if fault=='version':p['version']=True
    if fault=='extra':p['quota']=90
    if fault=='duplicate_authority':p['historical_recovery_policies']*=2
    p=P.sealed({k:v for k,v in p.items() if k!='hash'})
    with pytest.raises(BF.B.EvidenceError):P.validate(p,m,r)


def test_terminal_receipt_drains_then_switches_without_replaying_supplies(pilot,monkeypatch):
    p=pilot;catalog(p);i,it=supplies(p)
    H.leaf_wake(H.Backend(p.b,p.m),p.m,i,it,p.b.store.history(p.m['hash']))
    before=deepcopy(p.states);overlay,_=policy(p.m);monkeypatch.setattr(P,'load',lambda *a,**k:overlay)
    monkeypatch.setattr(H.Backend,'reconcile',lambda *a:dict(source_complete=False,persisted_reconciled=True))
    result,_=H.wake(p.b,p.m['hash'])
    assert result['source_dispatches']==1 and result['current_entity']=='fbo_postings'
    assert result['index']==P.selection(p.m)[0]
    history=p.b.store.history(it['shard_root'])
    assert [x['kind'] for x in history].count('DISPATCH_INTENT')==1
    assert [x['kind'] for x in history].count('RECONCILED')==1
    assert p.states[it['plan']['ack_hash']]==before[it['plan']['ack_hash']]
    assert len(p.dispatches)==2 and it['plan']==next(x['payload'] for x in p.b.store.history(p.m['hash']) if x['kind']=='CHUNK_PLAN' and x['sequence']==i)['plan']


def test_active_receipt_is_monitored_before_priority(pilot,monkeypatch):
    p=pilot;catalog(p);i,it=supplies(p);H.leaf_wake(H.Backend(p.b,p.m),p.m,i,it,[])
    overlay,_=policy(p.m);monkeypatch.setattr(P,'load',lambda *a,**k:overlay)
    monkeypatch.setattr(H.Backend,'reconcile',lambda *a:None)
    result,_=H.wake(p.b,p.m['hash'])
    assert result['status']=='MONITORING' and result['source_dispatches']==0 and len(p.dispatches)==1


def test_ambiguous_intent_never_reorders_into_a_new_source_execution(pilot,monkeypatch):
    p=pilot;catalog(p);i,it=supplies(p)
    def lost(b,doc,before,after):
        before(dict(run_id='bf-lost',lease_generation=1,ack_hash=doc['ack_hash']))
        p.dispatches.append(doc['ack_hash']);raise BF.B.EvidenceError('lost response')
    monkeypatch.setattr(H.Backend,'start',lost)
    with pytest.raises(BF.B.EvidenceError):H.leaf_wake(H.Backend(p.b,p.m),p.m,i,it,[])
    overlay,_=policy(p.m);monkeypatch.setattr(P,'load',lambda *a,**k:overlay)
    result,_=H.wake(p.b,p.m['hash'])
    assert result['status']=='STOPPED' and len(p.dispatches)==1


def test_temporary_wait_can_fall_back_without_changing_quota(pilot,monkeypatch):
    p=pilot;catalog(p);overlay,_=policy(p.m);monkeypatch.setattr(P,'load',lambda *a,**k:overlay)
    calls=[]
    def quota(b,leaf,now):
        e=leaf['plans'][0]['runtime_plan']['entity'];calls.append(e)
        return dict(status='WAITING',eligible_at=now.isoformat()) if e=='fbo_postings' else dict(status='ELIGIBLE',allowance=0)
    monkeypatch.setattr(H.Backend,'quota',quota)
    result,_=H.wake(p.b,p.m['hash'])
    assert result['source_dispatches']==1 and result['current_entity']=='finance_accrual'
    assert calls[:2]==['fbo_postings','finance_accrual']
    assert p.m['continuation']['performance_rolling_guard']==15


def test_completed_recent_returns_to_deep_order_and_never_replays():
    m=fixture()[0];p,_=policy(m);done={x['index']:{} for x in p['selection']}
    order=P.ordered(m,done,p)
    assert order==[i for i in P.ordered(m,{},None) if i not in done]


def test_unknown_coverage_and_partial_parent_are_not_recent_ready():
    m,_,_,records,*_=fixture();p,_=policy(m)
    i=p['selection'][0]['index'];prog=m['programs'][i]
    done={i:dict(ack_hash='a'*64,completed_at=NOW.isoformat(),coverage=dict(readback=[dict(day=prog['from'],coverage='UNKNOWN',rows=0,keys=0)]))}
    original=deepcopy((m,records,done))
    out=P.progress(m,records,done,p,now=NOW,waiting_receipts=0,unresolved_stops=0)
    assert all(not w['ready'] and w['parents_complete']==0 for w in out['windows'].values())
    assert out['windows']['RECENT_180']['coverage_gaps'][prog['entity']]
    assert (m,records,done)==original
    records.append(dict(kind='T5_PARENT_COMPLETE',payload=dict(parent=prog['t5_chunk_id'],leaf_proofs=[])))
    with pytest.raises(BF.B.EvidenceError,match='false canonical parent'):P.progress(m,records,done,p,now=NOW)


@pytest.mark.parametrize('runtime_changed',[False,True])
def test_old_recovery_certificate_survives_exact_owner_pinned_handoff(monkeypatch,tmp_path,runtime_changed):
    b,m,old,proof,root,sh,control,memory,_=observed(monkeypatch)
    R.apply(b,m,D.digest(root[-1]),proof['index'])
    before=deepcopy(memory.store.history(m['hash']))
    from tools.tenancy.full_leaf_image_check import fixture
    release=fixture()[6];path=tmp_path/'infra/tenant/releases/backfill'/f"{old['controller_source']}.json";path.parent.mkdir(parents=True);path.write_text(json.dumps(release))
    monkeypatch.setattr(BF,'REPO',tmp_path)
    active=R.sealed(dict({k:v for k,v in old.items() if k!='hash'},controller_source='e'*40,controller_image='new-qualified-image',controller_implementation_hash='f'*64))
    monkeypatch.setattr(P,'load',lambda *a:dict(historical_recovery_policies=[dict(source=old['controller_source'],policy_hash=old['hash'])]))
    monkeypatch.setattr(R,'policy',lambda *a:active)
    from tools.tenancy import full_runtime_handoff as FH
    calls=[]
    if runtime_changed:b.c['marketplaces']['ozon']['runtime_image']='europe-west1-docker.pkg.dev/mpa-platform/mpa-runtime/ozon-runtime@sha256:'+'e'*64
    contract=deepcopy(b.c)
    monkeypatch.setattr(FH,'policy',lambda backend,manifest:calls.append((backend,manifest)) or dict(qualified=True))
    accepted=R.load(b,m,memory.store.history(m['hash']))
    assert accepted[0]['policy_hash']==old['hash'] and memory.store.history(m['hash'])==before
    assert b.c==contract and len(calls)==int(runtime_changed)
    if runtime_changed:
        def missing(*a):raise BF.B.EvidenceError('owner runtime handoff absent')
        monkeypatch.setattr(FH,'policy',missing)
        with pytest.raises(BF.B.EvidenceError,match='handoff absent'):R.load(b,m,before)
        monkeypatch.setattr(FH,'policy',lambda *a:dict(qualified=True))
        bad=deepcopy(release);bad['runtime_implementation_hash']='e'*64;path.write_text(json.dumps(bad))
        with pytest.raises(BF.B.EvidenceError,match='historical runtime release differs'):R.load(b,m,before)
        path.write_text(json.dumps(release))
    active['initial_generation']+=1
    with pytest.raises(BF.B.EvidenceError,match='broadened'):R.load(b,m,before)


def test_new_priority_requires_owner_marker_and_qualified_release():
    m=fixture()[0];p,release=policy(m)
    b=SimpleNamespace(c=dict(orchestration=dict(job=dict(env=dict(CONTROLLER_SOURCE_SHA=release['source_sha'],HISTORICAL_SCHEDULER_STATE='ENABLED'))),datasets=dict(ref='ref')),tables=SimpleNamespace(get_table=lambda *a:None))
    with pytest.raises(BF.B.EvidenceError,match='authority absent'):P.load(b,m,release)


def test_readonly_progress_uses_consistent_commit_authority(monkeypatch):
    from tools.tests.test_durable_plan import Backend
    m=fixture()[0];p,_=policy(m);memory=Backend()
    records=[dict(kind='CHUNK_PLAN',payload=dict(index=0,plan=dict(ack_hash='a'*64),shard_root='b'*64))]
    b=SimpleNamespace(c=memory.c,store=memory.store)
    before=deepcopy((memory.rows,memory.meta.objects))
    out=P.monitoring(b,m,records,{},p,NOW)
    assert out['windows']['RECENT_180']['receipts_waiting_reconciliation']==0
    assert (memory.rows,memory.meta.objects)==before


def test_uncommitted_reconciliation_fence_cannot_reorder_or_claim_zero_pending(pilot):
    p=pilot;i,it=supplies(p);sh=it['shard_root']
    p.records.store.commit(sh,'DISPATCH_INTENT',1,{'plan':it['plan'],'preparation':{}},NOW)
    p.records.meta.objects[f'BFQ_{sh}_0000000001_RECONCILED']=({'root':sh[:16],'kind':'reconciled'},D.encoded({'record_hash':'a'*64}))
    b=SimpleNamespace(c=p.b.c,store=p.b.store)
    assert P.pending_receipts(b,{i:it})==[(i,it)]


def test_elected_uncommitted_intent_cannot_yield_to_another_leaf(pilot):
    p=pilot;i,it=supplies(p);sh=it['shard_root']
    p.records.meta.objects[f'BFQ_{sh}_0000000001_DISPATCH_INTENT']=({'root':sh[:16],'kind':'dispatch_intent'},D.encoded({'record_hash':'a'*64}))
    b=SimpleNamespace(c=p.b.c,store=p.b.store)
    with pytest.raises(BF.B.EvidenceError,match='no reordering'):P.pending_receipts(b,{i:it})


def test_reconciled_receipt_yields_even_when_new_report_quota_is_waiting(pilot,monkeypatch):
    p=pilot;catalog(p);i,it=supplies(p);H.leaf_wake(H.Backend(p.b,p.m),p.m,i,it,[])
    overlay,_=policy(p.m);monkeypatch.setattr(P,'load',lambda *a,**k:overlay)
    monkeypatch.setattr(H.Backend,'reconcile',lambda *a:dict(source_complete=False,persisted_reconciled=True))
    monkeypatch.setattr(H.Backend,'quota',lambda b,leaf,now:dict(status='WAITING',eligible_at=now.isoformat()) if leaf['plans'][0]['runtime_plan']['entity']=='supplies' else dict(status='ELIGIBLE',allowance=0))
    out,_=H.wake(p.b,p.m['hash'])
    assert out['current_entity']=='fbo_postings' and len(p.dispatches)==2


def test_eta_requires_observed_post_activation_completions_not_tick_interval():
    from datetime import timedelta
    m=fixture()[0];p,_=policy(m);p['activated_at']=(NOW-timedelta(hours=10)).isoformat()
    indices=P.selection(m);done={};starts={}
    assert len(indices)>=4
    # Fixture has four dated leaves; extend the synthetic telemetry set only for
    # this pure estimate test, never change or publish a production manifest.
    m=deepcopy(m);m['programs']+=deepcopy(m['programs']);indices=P.selection(m);p['selection']=[dict(index=i,program_hash=D.digest(m['programs'][i])) for i in indices]
    for n,i in enumerate(indices[:5]):
        starts[i]=(NOW-timedelta(hours=5-n)).isoformat()
        done[i]=dict(completed_at=(NOW-timedelta(hours=4.5-n)).isoformat(),reused_qualification=None)
    out=P.throughput(m,done,p,NOW,starts,[dict(at=NOW.isoformat(),quota_wait=True)])
    assert out['status']=='OBSERVED_ROLLING_THROUGHPUT' and out['leaves_per_hour']==1
    assert out['median_leaf_completion_seconds']==1800 and out['p90_leaf_completion_seconds'] is None
    assert out['quota_wait_share']==1 and out['estimates']['RECENT_180']['remaining_leaves']==len(indices)-5
    done.pop(indices[0]);assert P.throughput(m,done,p,NOW,starts)['status'].startswith('WAITING_FOR_5')


def test_no_eligible_recent_leaf_returns_cooldown_wait_without_stop_or_dispatch(pilot,monkeypatch):
    p=pilot;catalog(p)
    # The accepted qualification is already DONE in this production scenario;
    # a source cooldown must never cause the offline fixture to reread it.
    i=next(i for i,x in enumerate(p.m['programs']) if x.get('accepted_qualification_plan'))
    _,it=item(p,i);proof=completion(p,i,it['plan'],attempts=0)
    proof['reused_qualification']=dict(root=BF.QF.ROOT,plan_id=BF.QF.SKU,terminal_reconciliation_hash='8'*64)
    p.b.store.commit(p.m['hash'],'CHUNK_PLAN',i,it,p.b.clock())
    p.b.store.commit(p.m['hash'],'CHUNK_COMPLETE',i,proof,p.b.clock())
    overlay,_=policy(p.m);monkeypatch.setattr(P,'load',lambda *a,**k:overlay)
    eligible='2026-10-09T09:00:00+00:00'
    monkeypatch.setattr(H.Backend,'quota',lambda *a:dict(status='WAITING',allowance=0,eligible_at=eligible,basis='SOURCE_COOLDOWN'))
    out,_=H.wake(p.b,p.m['hash'])
    assert out['status']=='WAITING' and out['eligible_at']==eligible and not p.dispatches
    assert not any(r['kind']=='STOPPED' for r in p.b.store.history(p.m['hash']))
