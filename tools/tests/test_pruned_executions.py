"""Cloud Run prunes completed executions after a bounded retention (observed 2026-10-22: the oldest controller
executions of 10.10 vanished, breaking the per-wake revalidation of the policy-bootstrap certificate and with it
every wake). A platform-pruned execution of an already closed historical claim / an accepted certificate older than
the retention is tolerated; within the retention, or for any other read error, the previous STOP stays."""
from copy import deepcopy
from datetime import timedelta
import pytest
from tools.tenancy import controller_cadence as CC,tenant_backfill as BF,full_policy_bootstrap_recovery as R
from tools.tests.test_full_policy_bootstrap_recovery import protocol,NOW

def gone(method,url,body=None):raise BF.TT.TableError('cloud transport HTTP 404')
def broken(method,url,body=None):raise BF.TT.TableError('cloud transport HTTP 500')

def test_policy_bootstrap_certificate_survives_pruned_execution_only_after_retention(monkeypatch):
    b,m,p,records,*_=protocol(monkeypatch)
    assert R.validate(p,b,m,records)==p                                   # execution retained: unchanged behaviour
    b.request=gone
    with pytest.raises(BF.TT.TableError):R.validate(p,b,m,records)       # pruned within retention: STOP as before
    b.clock=lambda:NOW+CC.EXECUTION_RETENTION+timedelta(hours=1)
    assert R.validate(p,b,m,records)==p                                   # pruned after retention: durable evidence suffices
    b.request=broken
    with pytest.raises(BF.TT.TableError):R.validate(p,b,m,records)       # any other read error: STOP as before

def test_historical_closed_claim_tolerates_only_pruned_terminal_executions(monkeypatch):
    b,m,p,records,objects,*_=protocol(monkeypatch)
    ds=b.c['datasets']['tenant_locks'];claim=CC.claim_fields(b,b.tables.get_table(ds,CC.names(p['root'],1)[0]))
    b.request=gone
    with pytest.raises(BF.TT.TableError):CC.historical_closed_claim(b,claim)
    b.clock=lambda:CC.at(claim['created_at'])+CC.EXECUTION_RETENTION+timedelta(seconds=1)
    assert CC.historical_closed_claim(b,claim) is None
    name=CC.names(p['root'],1)[1];saved=objects.pop(name)                    # no canonical closure: never tolerated
    with pytest.raises(BF.B.EvidenceError):CC.historical_closed_claim(b,claim)
    objects[name]=saved
    assert CC.EXECUTION_RETENTION==timedelta(days=7) and CC.pruned(BF.TT.TableError('HTTP 404')) and not CC.pruned(BF.TT.TableError('HTTP 403'))
