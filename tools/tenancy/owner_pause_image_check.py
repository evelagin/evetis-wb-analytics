"""Network-free in-image proof: an owner pause of the dedicated tick during a wake is a typed quiescent end, not a STOP.

Heldi STOP bf7d1370 (2026-10-28): the owner paused tenant-backfill-tick while wake vwfcr ran; after it had reconciled
supplies page 242 its next dispatch preflight saw the tick PAUSED against the ENABLED descriptor and raised an untyped
EvidenceError -> global STOP. Now:
  * scheduler: only "live PAUSED where the descriptor expects ENABLED", with schedule/time zone/target/body/identity/scope
    exact, is the owner pause; reads/reconciliation pass, a NEW dispatch raises SourceDispatchPaused; every other drift
    (another target, identity, scope, schedule, time zone, body, or ENABLED against a PAUSED descriptor) stays STOP;
  * dispatch boundary: BF.start declines in its preflight, before any lease, checkpoint, intent or Run POST;
  * wake end: cloud_controller.main turns it into QUIESCENT_PAUSED (rc 0), closes the claim with 0 dispatches and writes
    no STOPPED record — also after a reconciliation committed earlier in the same wake (the exact vwfcr sequence);
  * CURRENT and finance catch-up dispatch paths raise it through their SOURCE_DISPATCH_BOUNDARY gate unchanged;
  * FBO diagnostic: a paused tick declines before the BFDC claim, the permission is kept, the claim closes normally.
Every patched seam is restored before returning.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
import io, json, contextlib as _ctx
from tools.tenancy import tenant_backfill as BF, durable_plan as D
from tools.tenancy.cloud_tick import SourceDispatchPaused

NOW = datetime(2026, 10, 28, 4, 43, tzinfo=timezone.utc)


@contextmanager
def patched(obj, name, value):
    saved = getattr(obj, name); setattr(obj, name, value)
    try: yield
    finally: setattr(obj, name, saved)


def rejects(fn, cls=BF.B.EvidenceError):
    try: fn()
    except cls as e: return e
    return None


def schedule(o, **changes):
    s = {'name': 'projects/x/locations/y/jobs/' + o['scheduler']['name'], 'schedule': o['scheduler']['schedule'], 'timeZone': o['scheduler']['time_zone'],
         'state': o['scheduler']['state'], 'httpTarget': {'uri': o['scheduler']['uri'], 'httpMethod': 'POST', 'body': None,
         'oauthToken': {'serviceAccountEmail': o['accounts']['wake']['email'], 'scope': 'https://www.googleapis.com/auth/cloud-platform'}}}
    for k, v in changes.items():
        if k in ('uri', 'httpMethod', 'body'): s['httpTarget'][k] = v
        elif k in ('serviceAccountEmail', 'scope'): s['httpTarget']['oauthToken'][k] = v
        else: s[k] = v
    return s


def scheduler_check():
    o = deepcopy(BF.target('client_001')['orchestration']); assert o['scheduler']['state'] == 'ENABLED'
    assert BF.dedicated_scheduler(o, schedule(o)) == 'EXACT' and BF.dedicated_scheduler(o, schedule(o), dispatch=True) == 'EXACT'
    paused = schedule(o, state='PAUSED')
    assert BF.dedicated_scheduler(o, paused) == 'OWNER_PAUSED'                                       # reads/reconcile valid
    e = rejects(lambda: BF.dedicated_scheduler(o, paused, dispatch=True), SourceDispatchPaused)
    assert e is not None and type(e) is SourceDispatchPaused                                       # typed, not a plain evidence error
    for label, drift in (('target', dict(uri='https://run.googleapis.com/v2/projects/x/jobs/other:run')), ('method', dict(httpMethod='GET')),
                         ('body', dict(body='e30x')), ('identity', dict(serviceAccountEmail='other@x.iam.gserviceaccount.com')),
                         ('scope', dict(scope='https://www.googleapis.com/auth/bigquery')), ('schedule', dict(schedule='*/5 * * * *')),
                         ('time zone', dict(timeZone='UTC')), ('unknown state', dict(state='UPDATE_FAILED'))):
        for state in ('ENABLED', 'PAUSED'):
            s = schedule(o, **dict(dict(state=state), **drift))
            for dispatch in (False, True):
                err = rejects(lambda: BF.dedicated_scheduler(o, s, dispatch=dispatch))
                assert err is not None and type(err) is BF.B.EvidenceError and 'drift' in str(err), (label, state, dispatch)
    po = deepcopy(o); po['scheduler']['state'] = 'PAUSED'                                         # PAUSED descriptor (staging)
    assert BF.dedicated_scheduler(po, schedule(po)) == 'EXACT'
    err = rejects(lambda: BF.dedicated_scheduler(po, schedule(po, state='ENABLED'), dispatch=True))
    assert err is not None and type(err) is BF.B.EvidenceError                                     # enabled against a PAUSED descriptor: STOP
    return 'PASS'


def dispatch_boundary_check():
    """BF.start: the dispatch preflight runs first, with dispatch=True; a pause leaves no lease/checkpoint/intent/POST."""
    seen = []
    def preflight(c, doc, now, backend=None, **k):
        seen.append(k.get('dispatch'))
        raise SourceDispatchPaused('dedicated tick paused by the owner during this wake')
    def forbidden(*a, **k): raise AssertionError('lease/intent path reached')
    doc = {'runtime_plan': {'entity': 'supplies'}, 'ack_hash': 'a' * 64}
    with patched(BF, 'validate_plan', lambda d, h: BF.target('client_001')), patched(BF, 'preflight', preflight), patched(BF, '_start', forbidden):
        e = rejects(lambda: BF.start(doc, doc['ack_hash'], backend=SimpleNamespace(append_request=forbidden), on_prepared=forbidden, on_receipt=forbidden), SourceDispatchPaused)
    assert e is not None and seen == [True]
    return 'PASS'


def vwfcr_sequence_check():
    """The exact vwfcr order with the real scheduler decision: the leaf preflight (reads, reconciliation) passes with the
    tick paused; the next page's BF.start runs the dispatch preflight and declines before _start (lease/intent/POST)."""
    o = deepcopy(BF.target('client_001')['orchestration']); paused = schedule(o, state='PAUSED'); calls = []
    def preflight(c, doc, now, backend=None, *, allow_active=False, owner_observation=False, dispatch=False):
        calls.append(('preflight', dispatch, BF.dedicated_scheduler(o, paused, dispatch=dispatch)))
        return 'tables', 'ledger'
    def forbidden(*a, **k): raise AssertionError('lease/intent path reached')
    doc = {'runtime_plan': {'entity': 'supplies'}, 'ack_hash': 'a' * 64}
    with patched(BF, 'validate_plan', lambda d, h: BF.target('client_001')), patched(BF, 'preflight', preflight), patched(BF, '_start', forbidden):
        BF.preflight(None, doc, NOW, backend=object(), allow_active=True)                          # leaf preflight before the reconcile
        calls.append(('reconciled', 242))                                                            # page 242 reconciled in this wake
        e = rejects(lambda: BF.start(doc, doc['ack_hash'], backend=SimpleNamespace(append_request=forbidden)), SourceDispatchPaused)
    assert e is not None and calls == [('preflight', False, 'OWNER_PAUSED'), ('reconciled', 242)], calls
    return 'PASS'


def wake_end_check():
    """cloud_controller.main: QUIESCENT_PAUSED, claim closed with 0 dispatches, no STOPPED — also after a reconciliation."""
    from tools.tenancy import cloud_controller as CLOUD, controller_cadence as CC, wake_budget as WB
    commits, closes = [], []
    def run(after_reconcile):
        commits.clear(); closes.clear()
        b = SimpleNamespace(c={'orchestration': {'cadence': {}}}, current_execution='e', wake_activity={'dispatch_attempts': 0},
                            store=SimpleNamespace(commit=lambda *a: commits.append(a[1]) or 'h'), clock=lambda: NOW)
        def wake(root, backend):
            if after_reconcile: backend.store.commit('s' * 64, 'RECONCILED', 242, {}, NOW)   # page reconciled in this wake
            raise SourceDispatchPaused('dedicated tick paused by the owner during this wake')
        out = io.StringIO()
        with patched(CLOUD, 'bootstrap', lambda env: (b, 'r' * 64)), patched(CLOUD, 'bounded_wake', wake), \
             patched(CC, 'close', lambda backend, result: closes.append(dict(result))), _ctx.redirect_stdout(out):
            rc = CLOUD.main([])
        from tools.tenancy.validation import parse_tenant_json
        return rc, parse_tenant_json(out.getvalue().strip().splitlines()[-1])
    for after in (False, True):
        rc, printed = run(after)
        assert rc == 0 and printed == {'status': 'QUIESCENT_PAUSED', 'source_dispatches': 0}, printed
        assert closes == [{'status': 'QUIESCENT_PAUSED', 'source_dispatches': 0}] and 'STOPPED' not in commits, (closes, commits)
        assert commits == (['RECONCILED'] if after else [])
    return 'PASS'


def current_paths_check():
    """CURRENT source_tick and the finance catch-up tick: the pause leaves the SOURCE_DISPATCH_BOUNDARY gate typed, no intent."""
    from tools.tenancy import current_controller as CUR, finance_catchup as FC
    from datetime import date
    class Store:
        def __init__(self): self.records = []
        def history(self, root, **k): return [r for r in self.records if r['root_hash'] == root]
        def commit(self, root, kind, seq, payload, now):
            self.records.append(dict(version=D.VERSION, root_hash=root, kind=kind, sequence=seq, payload=payload)); return 'h'
    def paused_start(doc, before, after): raise SourceDispatchPaused('dedicated tick paused by the owner during this wake')
    shard = '1' * 64; doc = {'ack_hash': '2' * 64, 'runtime_plan': {'entity': 'catalog', 'observation_date': '2026-10-28', 'plan_id': '3' * 64}}
    b = SimpleNamespace(current_root='4' * 64, current_execution='e', store=Store(), clock=lambda: NOW, preflight=lambda leaf: None,
                        quota=lambda *a: {'status': 'ELIGIBLE', 'allowance': 0}, active_runtime_execution=lambda: False,
                        state=lambda d: {'complete': False}, start=paused_start)
    assert rejects(lambda: CUR.source_tick(b, {'hash': '5' * 64}, date(2026, 10, 28), {'plan': doc, 'shard': shard}), SourceDispatchPaused)
    assert b.store.records == []
    fdoc = {'ack_hash': '6' * 64, 'runtime_plan': {'entity': 'finance_accrual', 'plan_id': '7' * 64}}
    fb = SimpleNamespace(catchup_root='8' * 64, current_execution='e', store=Store(), clock=lambda: NOW, preflight=lambda leaf: None,
                         quota=lambda *a: {'status': 'ELIGIBLE', 'allowance': 0}, active_runtime_execution=lambda: False,
                         state=lambda d: {'complete': False}, start=paused_start, select=lambda sql, p: [{'raw_rows': 0, 'coverage_rows': 0}],
                         c=BF.target('client_001'))
    with patched(FC, 'accepted_rows', lambda b_, d: 0):
        assert rejects(lambda: FC.tick(fb, {'hash': '9' * 64}, date(2026, 10, 8), {'plan': fdoc, 'shard': shard}), SourceDispatchPaused)
    assert fb.store.records == []
    return 'PASS'


def diagnostic_check():
    """FBO diagnostic: a paused tick declines before BFDC; queued() keeps the permission and lets the claim close."""
    from tools.tenancy import fbo_diagnostic_owner as DIAG
    base = 'projects/mpa-t-client-001/locations/europe-west1'
    def ss(**states):
        names = dict({'tenant-backfill-tick': 'ENABLED', 'ozon-daily': 'PAUSED', 'ozon-fast': 'PAUSED', 'ozon-weekly': 'PAUSED'}, **states)
        return {'jobs': [{'name': base + '/jobs/' + n, 'state': v} for n, v in names.items()]}
    assert DIAG.degraded_schedulers(ss())['state'] == 'ENABLED'
    e = rejects(lambda: DIAG.degraded_schedulers(ss(**{'tenant-backfill-tick': 'PAUSED'})), SourceDispatchPaused)
    assert e is not None and type(e) is SourceDispatchPaused
    for bad in (ss(**{'ozon-daily': 'ENABLED'}), ss(**{'tenant-backfill-tick': 'UPDATE_FAILED'}), {'jobs': ss()['jobs'][:3]}):
        err = rejects(lambda: DIAG.degraded_schedulers(bad))
        assert err is not None and type(err) is BF.B.EvidenceError
    with patched(DIAG, 'dispatch', lambda b, h: (_ for _ in ()).throw(SourceDispatchPaused('dedicated tick paused by the owner; diagnostic permission kept'))), \
         patched(DIAG, 'policy', lambda *a, **k: {'expires_at': '2099-01-01T00:00:00+00:00'}):
        tables = SimpleNamespace(get_table=lambda ds, n: ({'kind': 'fbo_diagnostic_queue'}, D.encoded({'policy_hash': 'd' * 64})) if n.startswith('BFDQUEUE_') else None)
        qb = SimpleNamespace(c=BF.target('client_001'), tables=tables, wake_activity={}, clock=lambda: NOW)
        out = DIAG.queued(qb)
    assert out == {'status': 'QUIESCENT_PAUSED', 'source_dispatches': 0, 'basis': 'DIAGNOSTIC_TICK_PAUSED'} and qb.wake_activity['diagnostic_active'] is False
    return 'PASS'


def check():
    for label, fn in (('scheduler', scheduler_check), ('dispatch', dispatch_boundary_check), ('vwfcr', vwfcr_sequence_check), ('wake', wake_end_check),
                      ('current', current_paths_check), ('diagnostic', diagnostic_check)):
        assert fn() == 'PASS', label
    return {'owner_pause_quiescent_adapter': 'PASS'}


if __name__ == '__main__':
    print(json.dumps(check(), sort_keys=True))
