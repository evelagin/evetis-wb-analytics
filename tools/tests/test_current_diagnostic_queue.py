from types import SimpleNamespace
from datetime import datetime,timezone
import pytest
from tools.tenancy import fbo_diagnostic_owner as Q,durable_plan as D,cloud_tick as T

SOURCE='a'*40
HASH='b'*64

def fixture(consumed=False,operation=False):
 objects={('ref','BFDQUEUE_'+SOURCE):({'kind':'fbo_diagnostic_queue'},D.encoded({'policy_hash':HASH}))}
 if consumed:objects[('locks','BFDC_'+HASH)]=({},'consumed')
 if operation:objects[('locks','BFDO_'+HASH)]=({},'operation')
 b=SimpleNamespace(c={'datasets':{'ref':'ref','tenant_locks':'locks'},'orchestration':{'job':{'env':{'CONTROLLER_SOURCE_SHA':SOURCE}}}},tables=SimpleNamespace(get_table=lambda ds,n:objects.get((ds,n))),wake_activity={},clock=lambda:datetime(2026,10,12,0,tzinfo=timezone.utc))
 return b,objects

def test_no_queue_token_does_not_change_ingestion():
 b,o=fixture();o.clear();assert Q.queued(b) is None and not b.wake_activity

def test_consumed_unknown_operation_never_repeats_source(monkeypatch):
 b,o=fixture(consumed=True)
 monkeypatch.setattr(Q,'dispatch',lambda *a:pytest.fail('uncertain source repeated'))
 with pytest.raises(Exception):Q.queued(b)

def test_queued_diagnostic_retains_shared_authority_until_child_terminal(monkeypatch):
 b,o=fixture();calls=[]
 monkeypatch.setattr(Q,'policy',lambda *a,**k:{'hash':HASH,'expires_at':'2026-10-12T01:00:00+00:00'})
 monkeypatch.setattr(Q,'dispatch',lambda *a:calls.append('dispatch') or {'execution':'child'})
 def terminal(*a):
  assert b.wake_activity['diagnostic_active'] is True
  calls.append('terminal');return {'execution':'child','terminal_success':True}
 monkeypatch.setattr(Q,'await_terminal',terminal)
 r=Q.queued(b)
 assert calls==['dispatch','terminal'] and r['source_dispatches']==0 and r['diagnostic_finished']
 assert b.wake_activity['diagnostic_active'] is False

def test_unknown_diagnostic_child_keeps_fence(monkeypatch):
 b,o=fixture()
 monkeypatch.setattr(Q,'policy',lambda *a,**k:{'hash':HASH,'expires_at':'2026-10-12T01:00:00+00:00'})
 monkeypatch.setattr(Q,'dispatch',lambda *a:{'execution':'child'})
 def unavailable(*a):raise OSError('terminal metadata unavailable')
 monkeypatch.setattr(Q,'await_terminal',unavailable)
 with pytest.raises(OSError):Q.queued(b)
 assert b.wake_activity['diagnostic_active'] is True

def test_proven_wait_before_consumption_clears_diagnostic_flag(monkeypatch):
 b,o=fixture()
 monkeypatch.setattr(Q,'policy',lambda *a,**k:{'hash':HASH,'expires_at':'2026-10-12T01:00:00+00:00'})
 def wait(*a):raise T.OverlapWait('WAIT_RECONCILIATION')
 monkeypatch.setattr(Q,'dispatch',wait)
 assert Q.queued(b)['status']=='WAITING' and b.wake_activity['diagnostic_active'] is False

def test_expired_valid_permission_is_quiet_and_does_not_pause_ingestion(monkeypatch):
 b,o=fixture()
 monkeypatch.setattr(Q,'policy',lambda *a,**k:{'hash':HASH,'expires_at':'2026-10-11T23:00:00+00:00'})
 monkeypatch.setattr(Q,'dispatch',lambda *a:pytest.fail('expired permission dispatched'))
 assert Q.queued(b) is None and not b.wake_activity
