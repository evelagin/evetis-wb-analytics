"""Full telemetry uses read-end evidence and cannot forgive source/authority failures."""
import json
from datetime import timedelta
from types import SimpleNamespace
import pytest
from tools.tenancy import full_controller as H,full_history as F,tenant_backfill as BF,durable_plan as D
from tools.tests.test_full_safety import manifest
from tools.tests.test_full_controller import NOW
from tools.tests.test_durable_plan import Backend as Records


def monitor(monkeypatch, offsets=(0,1,2,5), success=4, *, rows=1,status='MONITORING'):
    m=manifest();i=next(i for i,p in enumerate(m['programs']) if p['entity']=='catalog')
    doc=F.render_leaf(m,i,NOW.astimezone(BF.B.MSK).date())
    item={'index':i,'plan':doc,'shard_root':F.shard_root(m,i,doc)}
    store=Records();store.store.commit(m['hash'],'FULL_MANIFEST',0,m,NOW)
    store.store.commit(m['hash'],'CHUNK_PLAN',i,item,NOW)
    state=BF.B.initial(doc['runtime_plan']);state.update(sequence=3,rows=7,pages=2,requests=3)
    state['progress']['report']={'phase':'POLL','uuid':'NEVER_PRINT_ASYNC_UUID'}
    events=[];times=iter(NOW+timedelta(seconds=n) for n in offsets)
    def select(q,params):
        events.append('last-query');return [{'last_success':(NOW+timedelta(seconds=success)).isoformat(),'failed_attempts':1}]*rows
    def state_read(d):events.append('state-read');return state
    monkeypatch.setattr(BF,'validate_plan',lambda *a:store.c)
    # Isolate the legacy telemetry clock contract from priority projection.
    monkeypatch.setattr(H.RP,'load',lambda *a,**k:None)
    b=SimpleNamespace(c=store.c,store=store.store,clock=lambda:next(times),state=state_read,select=select,
        journal='mpa-t-client-001.ozon_raw.OZON_INGESTION_RUNS',binding_status={'seller':'BOUND','performance':'BOUND'})
    return H.monitoring(b,m['hash'],{'status':status,'index':i}),events


@pytest.mark.parametrize('success',[0,1.5,3,4.999,5])
def test_unchanged_and_commit_between_ordered_reads_and_final_read(monkeypatch,success):
    out,events=monitor(monkeypatch,success=success)
    assert events==['state-read','last-query']
    assert out['current_scope']['checkpoint_age_seconds']==int(5-success)
    assert out['current_scope']['observed_at']==(NOW+timedelta(seconds=5)).isoformat()
    assert out['observation_start']==NOW.isoformat() and out['observation_end']==out['current_scope']['observed_at']
    assert out['current_scope']['source_complete'] is False and out['completed_chunks']==0
    assert 'NEVER_PRINT' not in json.dumps(out)


def test_genuinely_future_commit_remains_denied(monkeypatch):
    with pytest.raises(BF.B.EvidenceError,match='in future'):monitor(monkeypatch,success=5.001)


@pytest.mark.parametrize('offsets',[(1,0,2,5),(0,2,1,5),(0,1,2,1)])
def test_backward_clock_at_each_ordered_read_remains_denied(monkeypatch,offsets):
    with pytest.raises(BF.B.EvidenceError,match='clock/order anomaly'):monitor(monkeypatch,offsets=offsets)


def test_stale_telemetry_keeps_real_age_and_does_not_complete(monkeypatch):
    out,_=monitor(monkeypatch,success=-3600)
    assert out['current_scope']['checkpoint_age_seconds']==3605 and out['completed_chunks']==0


@pytest.mark.parametrize('status',['MONITORING','DISPATCHED','STOPPED'])
def test_active_terminal_or_stopped_result_is_not_healed_by_watermark(monkeypatch,status):
    out,_=monitor(monkeypatch,status=status)
    assert out['running_chunks']==int(status in {'MONITORING','DISPATCHED'})
    assert out['failed_chunks']==int(status=='STOPPED')
    assert out['current_scope']['failed_attempts']==1 and out['completed_chunks']==0


@pytest.mark.parametrize('rows',[0,2])
def test_ambiguous_telemetry_remains_blocked(monkeypatch,rows):
    with pytest.raises(BF.B.EvidenceError,match='telemetry ambiguous'):monitor(monkeypatch,rows=rows)


def test_future_completed_leaf_remains_denied(monkeypatch):
    monkeypatch.setattr(H,'completed',lambda *a:{0:{'completed_at':(NOW+timedelta(seconds=6)).isoformat()}})
    with pytest.raises(BF.B.EvidenceError,match='completed telemetry in future'):monitor(monkeypatch)
