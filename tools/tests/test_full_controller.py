"""Offline failure injection at real durable CAS/commit boundaries; no cloud/source."""
import copy
import json
from datetime import datetime,timezone
from types import SimpleNamespace
import pytest
from tools.tenancy import full_controller as H, full_history as F, durable_plan as D, tenant_backfill as BF, controller_cadence as CC
from tools.tests.test_full_history import manifest
from tools.tests.test_durable_plan import Backend as Records

NOW=datetime(2026,10,7,14,tzinfo=timezone.utc)

@pytest.fixture
def pilot(monkeypatch):
    m=manifest();records=Records();states={};dispatches=[]
    # Release fitness is independently tested/qualified. These tests exercise
    # the dispatch/state protocol, never claim this synthetic image is deployed.
    monkeypatch.setattr(BF,'validate_plan',lambda *a:BF.target('client_001'))
    base=SimpleNamespace(c=BF.target('client_001'),store=records.store,clock=lambda:NOW,
                         binding_status={'seller':'BOUND','performance':'BOUND'})
    monkeypatch.setattr(H,'verify_authority',lambda b,m:b.c)
    # This synthetic legacy protocol has no priority authority. Priority tests
    # replace this stub with their independently validated overlay.
    monkeypatch.setattr(H.RP,'load',lambda *a,**k:None)
    # Isolate the legacy state protocol; cadence tests independently exercise
    # real executing-controller identity and atomic wake ownership.
    monkeypatch.setattr(CC,'acquire',lambda *a:None)
    monkeypatch.setattr(CC,'enabled',lambda *a:False)
    monkeypatch.setattr(H.Backend,'preflight',lambda *a,**k:None)
    monkeypatch.setattr(H.Backend,'quota',lambda *a:{'status':'ELIGIBLE','allowance':0})
    monkeypatch.setattr(H.Backend,'active_runtime_execution',lambda _:False)
    def state(_,doc):return copy.deepcopy(states.setdefault(doc['ack_hash'],BF.B.initial(doc['runtime_plan'])))
    monkeypatch.setattr(H.Backend,'state',state)
    def start(b,doc,before,after):
        seq=len(dispatches)+1
        prep={'run_id':f'synthetic-{seq}','lease_generation':seq,'ack_hash':doc['ack_hash']}
        before(prep);dispatches.append(doc['ack_hash'])
        after(dict(prep,operation=f'projects/mpa-t-client-001/locations/europe-west1/operations/synthetic-{seq}'))
    monkeypatch.setattr(H.Backend,'start',start)
    records.store.commit(m['hash'],'FULL_MANIFEST',0,m,NOW)
    return SimpleNamespace(m=m,b=base,records=records,states=states,dispatches=dispatches)


def item(p,index=None):
    if index is None:index=next(i for i,x in enumerate(p.m['programs']) if x['entity']=='catalog')
    doc=F.render_leaf(p.m,index,NOW.astimezone(BF.B.MSK).date())
    return index,{'index':index,'plan':doc,'shard_root':F.shard_root(p.m,index,doc)}


def test_lost_run_post_receipt_never_repeats(pilot,monkeypatch):
    p=pilot
    def lost(b,doc,before,after):
        before({'run_id':'lost','lease_generation':1,'ack_hash':doc['ack_hash']})
        p.dispatches.append(doc['ack_hash']);raise BF.B.EvidenceError('transport response lost')
    monkeypatch.setattr(H.Backend,'start',lost)
    with pytest.raises(BF.B.EvidenceError):H.wake(p.b,p.m['hash'])
    out,_=H.wake(p.b,p.m['hash'])
    assert out['status']=='STOPPED' and len(p.dispatches)==1
    assert any(r['kind']=='STOPPED' for r in p.b.store.history(p.m['hash']))


def test_two_independent_wakes_reconstruct_terminal_before_one_new_dispatch(pilot,monkeypatch):
    p=pilot;first,_=H.wake(p.b,p.m['hash']);assert first['source_dispatches']==1
    events=[]
    def reconcile(b,payload):
        events.append('reconcile')
        return {'source_complete':False,'persisted_reconciled':True,'checkpoint':'RUNNING'}
    monkeypatch.setattr(H.Backend,'reconcile',reconcile)
    second,_=H.wake(p.b,p.m['hash'])
    assert second['source_dispatches']==1 and second['sequence']==2 and events==['reconcile']
    index,it=item(p)
    assert [r['kind'] for r in p.b.store.history(it['shard_root'])].count('RECONCILED')==1
    assert len(p.dispatches)==2


def test_active_execution_is_monitored_without_source(pilot,monkeypatch):
    p=pilot;monkeypatch.setattr(H.Backend,'active_runtime_execution',lambda _:True)
    out,_=H.wake(p.b,p.m['hash'])
    assert out['status']=='MONITORING' and not p.dispatches


def test_authority_regression_stops_before_any_new_publication_or_dispatch(pilot,monkeypatch):
    p=pilot;before=copy.deepcopy(p.records.rows)
    def deny(*a):raise BF.B.EvidenceError('owner GO / binding absent')
    monkeypatch.setattr(H,'verify_authority',deny)
    with pytest.raises(BF.B.EvidenceError):H.wake(p.b,p.m['hash'])
    assert p.records.rows==before and not p.dispatches


def test_partial_source_never_creates_chunk_or_full_complete(pilot,monkeypatch):
    p=pilot;H.wake(p.b,p.m['hash'])
    monkeypatch.setattr(H.Backend,'reconcile',lambda *a:{'source_complete':False,'persisted_reconciled':True})
    H.wake(p.b,p.m['hash'])
    assert not any(r['kind'] in {'CHUNK_COMPLETE','FULL_COMPLETE'} for r in p.b.store.history(p.m['hash']))


def completion(p,index,doc,attempts=2):
    return {'index':index,'shard_root':F.shard_root(p.m,index,doc),'ack_hash':doc['ack_hash'],
            'state_hash':'9'*64,'sequence':20,'dispatch_attempts':attempts,'reused_qualification':None,'source_complete':True,
            'persisted_reconciled':True,'coverage':{'source_sequence':20,'readback':[{'rows':3,'keys':3}]},
            'completed_at':NOW.isoformat()}


def test_parent_done_waits_for_all_60_day_parent_leaves_and_repairs_crash(pilot):
    p=pilot;parents={}
    for i,x in enumerate(p.m['programs']):
        if x['entity']=='ads_sku_daily':parents.setdefault(x['t5_chunk_id'],[]).append(i)
    parent,siblings=next((k,v) for k,v in parents.items() if len(v)==2)
    done={};rows=[];commits=[]
    class Tables:
        def append(self,ds,table,values):rows.extend(copy.deepcopy(values))
    b=SimpleNamespace(c=p.b.c,tables=Tables(),clock=lambda:NOW,
                      store=SimpleNamespace(commit=lambda *a:commits.append(a)))
    def select(q,params):
        return [{k:r[k] for k in ('evidence_json','run_id','attempts','rows_written')}
                for r in rows if r['backfill_id']==params['parent'][1]]
    b.select=select
    for i in siblings:
        _,it=item(p,i);done[i]=completion(p,i,it['plan'])
        if len(done)==1:
            H.repair_t5_completions(b,p.m,[],done);assert rows==[]
    # Simulate interruption after canonical append, before mapping marker.
    def crash(*a):raise RuntimeError('lost mapping acknowledgement')
    b.store.commit=crash
    with pytest.raises(RuntimeError):H.repair_t5_completions(b,p.m,[],done)
    assert len(rows)==1 and rows[0]['attempts']==4 # executions, not 40 source units
    b.store.commit=lambda *a:commits.append(a)
    H.repair_t5_completions(b,p.m,[],done)
    assert len(rows)==1 and commits[0][1]=='T5_PARENT_COMPLETE'
    mapped={'kind':'T5_PARENT_COMPLETE','sequence':min(siblings),'payload':commits[0][3]}
    H.repair_t5_completions(b,p.m,[mapped],done);assert len(commits)==1
    with pytest.raises(BF.B.EvidenceError,match='before all leaves'):
        H.repair_t5_completions(b,p.m,[mapped],{siblings[0]:done[siblings[0]]})


@pytest.mark.parametrize('change',[lambda d:d.update(source_complete=False),
    lambda d:d.update(ack_hash='8'*64),lambda d:d.update(sequence=19),
    lambda d:d.update(dispatch_attempts=0),lambda d:d.update(state_hash='bad'),
    lambda d:d.update(extra='invented')])
def test_detached_or_false_completion_is_rejected(pilot,change):
    p=pilot;i,it=item(p);d=completion(p,i,it['plan'])
    records=[{'kind':'CHUNK_PLAN','sequence':i,'payload':it},
             {'kind':'CHUNK_COMPLETE','sequence':i,'payload':d}]
    assert H.completed(records,p.m)[i]==d
    change(d)
    with pytest.raises(BF.B.EvidenceError):H.completed(records,p.m)


def test_state_size_guard_is_not_raised_by_full_program():
    assert F.MAX_MANIFEST_BYTES==900000 and F.MAX_LEAF_RECORDS==9000
    assert F.MAX_LEAF_DAYS==31


def test_projected_ledger_preserves_mode_generation_and_excludes_large_payload():
    seen=[];c=BF.target('client_001')
    def select(sql,params):
        seen.append(sql)
        return [{'lease_generation':10000,'evidence_json':'{"mode":"BOUNDED_PILOT"}',
                 'updated_at':'1.7913852E9'}]
    b=SimpleNamespace(c=c,select=select)
    t=H.ProjectedTables(b,None);rows=list(t.rows(c['datasets']['tenant_ops'],'BACKFILL_CHECKPOINTS'))
    assert BF.pilot_lease_generation(rows,[],'0'*16,NOW,lambda _:None)==10001
    assert rows[0]['updated_at'].endswith('+00:00')
    assert "JSON_VALUE(evidence_json,'$.mode')" in seen[0]
    assert "WHERE entity != 'orchestration'" in seen[0]
    from tools.tenancy.cloud_access import select_body
    select_body(c,{'query':seen[0],'useLegacySql':False,'location':'EU','maximumBytesBilled':'1073741824'})


def test_snapshot_rollover_preserves_old_partial_and_freezes_new_identity(pilot):
    p=pilot;H.wake(p.b,p.m['hash']);i,it=item(p)
    # Already terminal/reconciled previous-day source, partial state remains.
    receipt=next(r for r in p.b.store.history(it['shard_root']) if r['kind']=='DISPATCH_RECEIPT')
    p.b.store.commit(it['shard_root'],'RECONCILED',receipt['sequence'],{'source_complete':False,'persisted_reconciled':True},NOW)
    historical=copy.deepcopy(p.b.store.history(it['shard_root']))
    p.b.clock=lambda:NOW.replace(day=8)
    result,_=H.wake(p.b,p.m['hash'])
    assert result['status']=='SNAPSHOT_DAY_ROLLOVER' and len(p.dispatches)==1
    records=p.b.store.history(p.m['hash'])
    assert not any(r['kind']=='CHUNK_COMPLETE' for r in records)
    plans=H.reconstruct_plans(records,p.m,p.b.clock())
    assert plans[i]['plan']['runtime_plan']['observation_date']=='2026-10-08'
    assert plans[i]['plan']['ack_hash']!=it['plan']['ack_hash']
    assert any(r['kind']=='CHUNK_SUPERSEDED' for r in records)
    assert p.b.store.history(it['shard_root'])==historical


def test_arbitrary_snapshot_replacement_without_rollover_proof_fails(pilot):
    p=pilot;i,it=item(p);day=NOW.replace(day=8).astimezone(BF.B.MSK).date()
    doc=F.render_leaf(p.m,i,day);fresh={'index':i,'plan':doc,'shard_root':F.shard_root(p.m,i,doc)}
    rows=[{'kind':'CHUNK_PLAN','sequence':i,'payload':x} for x in (it,fresh)]
    with pytest.raises(BF.B.EvidenceError,match='rollover proof'):H.reconstruct_plans(rows,p.m,NOW.replace(day=8))
