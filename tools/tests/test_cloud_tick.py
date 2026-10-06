"""Restart/failure and exactly-once intent protocol tests, no live cloud/source."""
import copy
from datetime import datetime,timezone
import pytest
from tools.tenancy import durable_plan as D, cloud_tick as C
from tools.tests.test_durable_plan import Backend,ROOT,NOW


class Execution:
    def __init__(self):
        self.calls=0;self.reads=0;self.active=False;self.mode='normal';self.result=None
        self.quota_value={'status':'ELIGIBLE','allowance':15}
        self.plan={'ack_hash':'2'*64,'mode':'SYNTHETIC'}
        self.complete={'source_complete':True,'persisted_reconciled':True}
    def preflight(self,manifest):
        self.reads+=1
        if self.mode=='binding_regression':raise D.BF.B.EvidenceError('binding regression')
    def active_runtime_execution(self):return self.active
    def quota(self,manifest,now):return self.quota_value
    def next_plan(self,*args):return self.plan
    def all_complete(self,manifest):return self.plan is None
    def completion_evidence(self,manifest):return self.complete
    def recover_receipt(self,intent):return self.result
    def reconcile(self,receipt):return self.result
    def start(self,plan,before,after):
        prepared={'run_id':'synthetic-run','lease_generation':1,'ack_hash':plan['ack_hash']}
        before(prepared)
        self.calls+=1
        if self.mode=='lost_post_response':raise D.BF.B.EvidenceError('ambiguous POST')
        receipt=dict(prepared,operation='synthetic-operation')
        if self.mode=='foreign_receipt':receipt['run_id']='foreign-run'
        after(receipt)


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setattr(D,'validate_manifest',lambda d:None)
    monkeypatch.setattr(D.BF,'validate_plan',lambda *a:None)
    b=Backend();e=Execution()
    manifest={'hash':ROOT,'purpose':'QUALIFICATION','plans':[copy.deepcopy(e.plan)]}
    b.store.commit(ROOT,'MANIFEST',0,manifest,NOW)
    return b,e,C.Tick(ROOT,b.store,e,lambda:NOW)


def test_dispatch_intent_precedes_single_post_and_receipt_survives_restart(setup):
    b,e,t=setup
    assert t.run()['status']=='DISPATCHED' and e.calls==1
    records=b.store.history(ROOT)
    assert {r['kind'] for r in records}=={'MANIFEST','DISPATCH_INTENT','DISPATCH_RECEIPT'}
    e.result={'source_complete':False,'checkpoint':'RUNNING'}
    assert C.Tick(ROOT,b.store,e,lambda:NOW).run()['status']=='RECONCILED'
    assert e.calls==1


def test_lost_run_post_response_is_never_repeated_after_restart(setup):
    b,e,t=setup;e.mode='lost_post_response'
    with pytest.raises(D.BF.B.EvidenceError,match='ambiguous POST'):t.run()
    assert e.calls==1
    assert C.Tick(ROOT,b.store,e,lambda:NOW).run()['status']=='STOPPED'
    assert e.calls==1
    assert t.run()['status']=='STOPPED' and e.calls==1


def test_receipt_scope_mismatch_preserves_intent_and_never_retries_post(setup):
    b,e,t=setup;e.mode='foreign_receipt'
    with pytest.raises(D.BF.B.EvidenceError,match='scope mismatch'):t.run()
    assert e.calls==1
    assert {r['kind'] for r in b.store.history(ROOT)}=={'MANIFEST','DISPATCH_INTENT'}
    assert t.run()['status']=='STOPPED' and e.calls==1


def test_intent_publication_failure_does_not_send_run_post(setup):
    b,e,t=setup;b.rejected=True
    with pytest.raises(D.BF.B.EvidenceError):t.run()
    assert e.calls==0


def test_pending_execution_monitored_even_if_budget_waits(setup):
    b,e,t=setup;e.active=True;e.quota_value={'status':'WAITING','eligible_at':'later'}
    assert t.run()['status']=='MONITORING' and e.calls==0


def test_waiting_preserves_immutable_plan_and_makes_no_dispatch(setup):
    b,e,t=setup;e.quota_value={'status':'WAITING','eligible_at':'2026-10-06T18:38:14Z'}
    old=copy.deepcopy(b.rows[0]);assert t.run()['status']=='WAITING'
    assert t.run()['status']=='WAITING' and e.calls==0 and b.rows[0]==old
    assert len([r for r in b.store.history(ROOT) if r['kind']=='WAITING'])==1


def test_known_receipt_recovery_does_not_dispatch(setup):
    b,e,t=setup;e.mode='lost_post_response'
    with pytest.raises(D.BF.B.EvidenceError):t.run()
    e.result={'plan':e.plan,'receipt':{'operation':'synthetic','run_id':'synthetic-run','lease_generation':1,'ack_hash':e.plan['ack_hash']}}
    assert t.run()['status']=='RECEIPT_RECOVERED' and e.calls==1


def test_terminal_partial_never_claims_qualification_complete(setup):
    b,e,t=setup;t.run();e.result={'checkpoint':'RUNNING','source_complete':False}
    assert t.run()['status']=='RECONCILED'
    assert 'COMPLETE' not in {r['kind'] for r in b.store.history(ROOT)}


@pytest.mark.parametrize('proof',[None,{}, {'source_complete':False,'persisted_reconciled':True},{'source_complete':True,'persisted_reconciled':False}])
def test_no_pending_plan_without_complete_source_and_dq_does_not_fake_done(setup,proof):
    b,e,t=setup;e.plan=None;e.complete=proof
    with pytest.raises(D.BF.B.EvidenceError,match='completion evidence'):t.run()
    assert 'COMPLETE' not in {r['kind'] for r in b.store.history(ROOT)} and e.calls==0


def test_completed_qualification_stops_source_work_on_subsequent_wake(setup):
    b,e,t=setup;e.plan=None
    assert t.run()['status']=='QUALIFICATION_COMPLETE'
    e.plan={'ack_hash':'2'*64};e.quota_value={'status':'WAITING','eligible_at':'later'}
    assert t.run()['status']=='QUALIFICATION_COMPLETE' and e.calls==0


def test_security_regression_stops_before_intent_or_dispatch(setup):
    b,e,t=setup;e.mode='binding_regression'
    with pytest.raises(D.BF.B.EvidenceError,match='binding'):t.run()
    assert {r['kind'] for r in b.store.history(ROOT)}=={'MANIFEST'} and e.calls==0


def test_full_history_does_not_inherit_qualification_authority(setup):
    b,e,t=setup;b.rows.clear();b.meta.objects.clear()
    # Simulate retained bytes from an unsupported future publisher, not using
    # current commit (which independently refuses FULL_HISTORY publication).
    manifest={'hash':ROOT,'purpose':'QUALIFICATION','plans':[]}
    h=b.store.commit(ROOT,'MANIFEST',0,manifest,NOW)
    bad=b.store.read(ROOT,h);bad['payload']['purpose']='FULL_HISTORY'
    t.store.history=lambda _: [bad]
    with pytest.raises(D.BF.B.EvidenceError,match='full-history'):t.run()
    assert e.calls==0


def test_final_completion_is_published_despite_exhausted_quota(setup):
    b,e,t=setup;e.plan=None;e.quota_value={"status":"WAITING","eligible_at":"later"}
    assert t.run()["status"]=="QUALIFICATION_COMPLETE" and e.calls==0
    assert "COMPLETE" in {r["kind"] for r in b.store.history(ROOT)}
