"""Synthetic deterministic ordered monitoring; no time sleeps or live services."""
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
import pytest
from tools.tenancy import cloud_controller as C,tenant_backfill as BF

NOW=datetime(2026,10,8,6,tzinfo=timezone.utc)

def observe(times,updates,*,active=True):
    doc=BF.QF.accepted_doc('9b4702a141928eb1f4630ec85b2488838610dace63bd80701a3bf215d7ac1320');b=object.__new__(C.Backend)
    clocks=iter(times);values=iter(updates);b.clock=lambda:next(clocks);b.c=BF.target('client_001');b.active=active;b.binding_status={}
    b.state=lambda _:BF.B.initial(doc['runtime_plan']);b.store=SimpleNamespace(history=lambda _: [{'kind':'MANIFEST','payload':{'purpose':'QUALIFICATION','plans':[doc]*len(updates)}}])
    def query(*_):
        value=next(values)
        if callable(value):value=value(b)
        return [{'last_success':value.isoformat() if value else None,'last_failure':None,'failed_attempts':0}]
    b.select=query
    return b.monitoring(BF.QF.ROOT,{'status':'MONITORING'})

@pytest.mark.parametrize('updates',[[NOW]*3,[NOW,NOW+timedelta(seconds=3),NOW+timedelta(seconds=5)],[NOW,NOW,NOW+timedelta(seconds=6)]])
def test_ordered_queries_accept_commits_during_reads(updates):
    times=[NOW]
    for n in range(3):times.extend([NOW+timedelta(seconds=2*n),NOW+timedelta(seconds=2*n+2)])
    out=observe(times,updates)
    assert len(out['scopes'])==3 and all(s['checkpoint_age_seconds']>=0 for s in out['scopes'])
    assert out['observation_end']==(NOW+timedelta(seconds=6)).isoformat()


def test_exact_old_reference_then_commit_then_later_query():
    out=observe([NOW,NOW,NOW+timedelta(seconds=1),NOW+timedelta(seconds=1),NOW+timedelta(seconds=3)],[NOW,NOW+timedelta(seconds=2)])
    assert out['scopes'][1]['checkpoint_age_seconds']==1

@pytest.mark.parametrize('times,updates,reason',[
 ([NOW,NOW,NOW],[NOW+timedelta(microseconds=1)],'future'),
 ([NOW,NOW-timedelta(seconds=1),NOW],[NOW],'clock/order'),
 ([NOW,NOW+timedelta(seconds=1),NOW],[NOW],'clock/order')])
def test_future_and_order_anomalies_remain_hard_errors(times,updates,reason):
    with pytest.raises(BF.B.EvidenceError,match=reason):observe(times,updates)


def test_stale_result_remains_stale_not_completed_or_pass():
    out=observe([NOW,NOW,NOW],[NOW-timedelta(days=2)])
    assert out['completed_chunks']==0 and out['scopes'][0]['checkpoint_age_seconds']==172800


def test_active_child_remains_active_and_terminal_change_is_observed():
    out=observe([NOW,NOW,NOW],[NOW]);assert out['active_runtime_execution'] is True
    def terminal(b):b.active=False;return NOW
    out=observe([NOW,NOW,NOW],[terminal]);assert out['active_runtime_execution'] is False
    assert out['completed_chunks']==0


def test_inconsistent_evidence_not_temporally_exempted():
    # An inconsistent state query aborts before temporal projection, not a PASS.
    doc=BF.QF.accepted_doc(BF.QF.SKU);b=object.__new__(C.Backend);b.clock=lambda:NOW
    b.store=SimpleNamespace(history=lambda _: [{'kind':'MANIFEST','payload':{'plans':[doc],'purpose':'QUALIFICATION'}}])
    def inconsistent(_):raise BF.B.EvidenceError('source checkpoint sequence conflict')
    b.state=inconsistent
    with pytest.raises(BF.B.EvidenceError,match='conflict'):b.monitoring(BF.QF.ROOT,{'status':'MONITORING'})
