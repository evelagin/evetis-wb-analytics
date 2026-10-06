"""Failure injection for durable record publication and async dispatch safety."""
from datetime import datetime, timedelta, timezone
import copy
import json
import pytest
from tools.tenancy import durable_plan as D

NOW=datetime(2026,10,6,7,tzinfo=timezone.utc)
ROOT='1'*64
OTHER='2'*64


def reservation(exports=10,seq=2,hours=1):
    return {'plan_id':ROOT,'sequence':seq,'exports':exports,'at':(NOW-timedelta(hours=hours)).isoformat()}


def record(kind,seq=1,payload=None):
    return {'version':D.VERSION,'root_hash':ROOT,'kind':kind,'sequence':seq,'payload':payload or {}}


def test_budget_wait_does_not_claim_ozon_rejection():
    q=D.quota_decision([reservation(),reservation(5,5)],0,NOW)
    assert q=={'status':'WAITING','allowance':0,'eligible_at':(NOW+timedelta(hours=23)).isoformat()}
    assert D.decide_tick([],ROOT,q)['action']=='WAITING'


def test_intent_reservations_collapse_repeated_insert_acknowledgements():
    r=reservation();new=dict(r,at=(NOW-timedelta(minutes=10)).isoformat())
    q=D.quota_decision([r,r,new,reservation(5,5)],0,NOW)
    assert q['status']=='WAITING' and q['eligible_at']==(NOW+timedelta(hours=23)).isoformat()
    assert D.quota_decision([r,new],0,NOW)['allowance']==5


def test_conflicting_reservation_blocks():
    with pytest.raises(D.BF.B.EvidenceError,match='conflict'):
        D.quota_decision([reservation(),reservation(9)],0,NOW)


def test_exact_rolling_expiry_releases_only_expired_reservations():
    q=D.quota_decision([reservation(hours=24),reservation(5,5,hours=23)],0,NOW)
    assert q=={'status':'ELIGIBLE','allowance':10}


def test_submitted_report_may_poll_with_no_new_export_budget():
    q=D.quota_decision([reservation(),reservation(5,5)],0,NOW,'POLL')
    assert q['status']=='POLL_ONLY' and q['allowance']==0
    assert D.decide_tick([],ROOT,q)['poll_only']


def test_ambiguous_intent_never_becomes_retry_post():
    q=D.quota_decision([],0,NOW,'INTENT')
    assert D.decide_tick([],ROOT,q)['action']=='STOPPED'
    assert D.decide_tick([record('DISPATCH_INTENT')],ROOT,{'status':'ELIGIBLE'})['action']=='RECOVER_RECEIPT_OR_STOP'


@pytest.mark.parametrize('phase',["DOWNLOAD",False,0,{},''])
def test_corrupt_async_phase_stops(phase):
    with pytest.raises(D.BF.B.EvidenceError):D.quota_decision([],0,NOW,phase)


@pytest.mark.parametrize('unknown',[1,-1,True,'0',None])
def test_unknown_ordinary_export_accounting_is_not_assumed_zero(unknown):
    with pytest.raises(D.BF.B.EvidenceError):D.quota_decision([],unknown,NOW)


@pytest.mark.parametrize('change',[{'exports':0},{'exports':11},{'exports':True},{'sequence':0},{'sequence':True},{'plan_id':'x'}, {'at':(NOW+timedelta(seconds=1)).isoformat()},{'at':'2026-10-05T07:00:00'}])
def test_bad_reservation_stops(change):
    with pytest.raises(D.BF.B.EvidenceError):D.quota_decision([dict(reservation(),**change)],0,NOW)


def test_active_execution_monitored_never_dispatched_again():
    assert D.decide_tick([record('DISPATCH_INTENT')],ROOT,{'status':'ELIGIBLE'},True)=={'action':'MONITOR'}


def test_terminal_receipt_reconciled_before_next_dispatch_or_quota_wait():
    rs=[record('DISPATCH_INTENT'),record('DISPATCH_RECEIPT')]
    assert D.decide_tick(rs,ROOT,{'status':'WAITING','eligible_at':'later'})=={'action':'RECONCILE','sequence':1}
    rs.append(record('RECONCILED',payload={'complete':False}))
    assert D.decide_tick(rs,ROOT,{'status':'ELIGIBLE'})['sequence']==2


@pytest.mark.parametrize('rows',[[record('DISPATCH_RECEIPT')],[record('RECONCILED')],[record('DISPATCH_INTENT',2)],[record('DISPATCH_INTENT'),record('DISPATCH_INTENT',payload={'run':'other'})]])
def test_gap_conflict_or_orphan_does_not_dispatch(rows):
    with pytest.raises(D.BF.B.EvidenceError):D.decide_tick(rows,ROOT,{'status':'ELIGIBLE'})


def test_security_stop_remains_stopped_despite_new_budget():
    assert D.decide_tick([record('STOPPED')],ROOT,{'status':'ELIGIBLE'})=={'action':'STOPPED'}


class Metadata:
    def __init__(self):self.objects={};self.fail=False
    def get_table(self,ds,name):return self.objects.get(name)
    def list_tables(self,ds):return [(n,v[0],None) for n,v in self.objects.items()]
    def create_marker(self,ds,name,labels,description):
        assert all(len(k)<=63 and len(v)<=63 for k,v in labels.items())
        assert len(description.encode())<=16384
        if self.fail:raise D.BF.B.EvidenceError('marker failed')
        if name in self.objects:return False
        self.objects[name]=(labels,description);return True


class Backend:
    def __init__(self):
        self.meta=Metadata();self.rows=[];self.query_failure=False;self.rejected=False
        self.c=D.BF.target('client_001')
        self.store=D.DurableRecords(self.c,self.meta,self.select,self.append)
    def select(self,c,sql,params):
        assert sql.startswith('SELECT DISTINCT ')
        assert c['project_id']=='mpa-t-client-001' and ';' not in sql
        if self.query_failure:raise D.BF.B.EvidenceError('streaming read unavailable')
        unique={r['evidence_json'] for r in self.rows if ('record' not in params or r['backfill_id']==params['record'][1]) and r['plan_hash']==params['root'][1]}
        return [{'evidence_json':v} for v in unique]
    def append(self,row):
        if self.rejected:raise D.BF.B.EvidenceError('writer insertAll rejected')
        self.rows.append(copy.deepcopy(row))


def test_immutable_readback_survives_new_controller_process():
    b=Backend();h=b.store.commit(ROOT,'WAITING',0,{'until':'later'},NOW)
    new=D.DurableRecords(b.c,b.meta,b.select,b.append)
    assert new.read(ROOT,h)['payload']=={'until':'later'}
    assert new.commit(ROOT,'WAITING',0,{'until':'later'},NOW+timedelta(hours=1))==h
    assert len(b.rows)==1


@pytest.mark.parametrize('fault',['writer','query','marker'])
def test_failed_publication_never_has_committed_marker(fault):
    b=Backend();b.rejected=fault=='writer';b.query_failure=fault=='query';b.meta.fail=fault=='marker'
    with pytest.raises(D.BF.B.EvidenceError):b.store.commit(ROOT,'DISPATCH_INTENT',1,{'run':'known'},NOW)
    assert not any(n.startswith('BFR_') for n in b.meta.objects)
    assert b.store.history(ROOT)==[]


def test_lost_ack_readback_recovery_collapses_identical_rows():
    b=Backend();b.query_failure=True
    with pytest.raises(D.BF.B.EvidenceError):b.store.commit(ROOT,'WAITING',0,{'until':'later'},NOW)
    b.query_failure=False
    h=b.store.commit(ROOT,'WAITING',0,{'until':'later'},NOW)
    assert len(b.rows)==2 and b.store.read(ROOT,h)['kind']=='WAITING'


def test_record_corruption_is_not_a_missing_record_to_overwrite():
    b=Backend();h=b.store.commit(ROOT,'DISPATCH_INTENT',1,{'run':'known'},NOW)
    b.rows[0]['evidence_json']=D.encoded(record('DISPATCH_INTENT',payload={'run':'changed'}))
    with pytest.raises(D.BF.B.EvidenceError):b.store.read(ROOT,h)
    with pytest.raises(D.BF.B.EvidenceError):b.store.commit(ROOT,'DISPATCH_INTENT',1,{'run':'known'},NOW)
    assert len(b.rows)==1


def test_uncommitted_and_foreign_records_are_fail_closed():
    b=Backend();h=b.store.commit(ROOT,'WAITING',0,{},NOW)
    with pytest.raises(D.BF.B.EvidenceError):b.store.read(OTHER,h)
    b.meta.objects.clear()
    with pytest.raises(D.BF.B.EvidenceError,match='uncommitted'):b.store.read(ROOT,h)


def test_oversize_record_cannot_truncate_or_write():
    b=Backend()
    with pytest.raises(D.BF.B.EvidenceError,match='size guard'):
        b.store.commit(ROOT,'WAITING',0,{'value':'x'*D.MAX_RECORD_BYTES},NOW)
    assert not b.rows and not b.meta.objects


def test_storage_foundation_cannot_publish_unproven_full_history(monkeypatch):
    b=Backend();monkeypatch.setattr(D,'validate_manifest',lambda p:None)
    with pytest.raises(D.BF.B.EvidenceError,match='FULL_HISTORY_GO_UNPROVEN'):
        b.store.commit(ROOT,'MANIFEST',0,{'hash':ROOT,'purpose':'FULL_HISTORY'},NOW)
    assert not b.rows

def test_concurrent_different_intents_cannot_both_commit_same_sequence():
    b=Backend();h=b.store.commit(ROOT,'DISPATCH_INTENT',1,{'run':'first'},NOW)
    with pytest.raises(D.BF.B.EvidenceError,match='concurrent durable sequence'):
        b.store.commit(ROOT,'DISPATCH_INTENT',1,{'run':'second'},NOW)
    assert b.store.read(ROOT,h)['payload']=={'run':'first'} and len(b.rows)==1

def test_lost_append_after_intent_fence_never_elects_new_run():
    b=Backend();b.rejected=True
    with pytest.raises(D.BF.B.EvidenceError):b.store.commit(ROOT,'DISPATCH_INTENT',1,{'run':'first'},NOW)
    b.rejected=False
    with pytest.raises(D.BF.B.EvidenceError,match='concurrent durable sequence'):
        b.store.commit(ROOT,'DISPATCH_INTENT',1,{'run':'replacement'},NOW)
    assert b.store.history(ROOT)==[] and not b.rows
    h=b.store.commit(ROOT,'DISPATCH_INTENT',1,{'run':'first'},NOW)
    assert b.store.history(ROOT)==[b.store.read(ROOT,h)]

def test_history_survives_controller_restart_and_rejects_record_overscan():
    b=Backend();b.store.commit(ROOT,'WAITING',0,{'until':'one'},NOW)
    b.store.commit(ROOT,'WAITING',0,{'until':'two'},NOW)
    with pytest.raises(D.BF.B.EvidenceError,match='bounded inventory'):b.store.history(ROOT,max_records=1)
    fresh=D.DurableRecords(b.c,b.meta,b.select,b.append)
    assert len(fresh.history(ROOT))==2 and fresh.history(OTHER)==[]
