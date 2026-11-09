"""Network-free in-image contract of A2 «Durable Daily» (2026-11-09). Every patched seam is restored.

  (a) a transient read at each reclassified point is a typed retry, never a STOP:
      control closure (token read, lost readback), BigQuery SELECT not complete/backend error,
      Google 403 rate protection on a read, post-closure reporting;
  (b) the fail-closed points still STOP: closure retries exhausted, closure POST outcome unknown,
      closure conflict, a sent write in the dispatch ledger, an open CURRENT authority, a Catalog
      dependency, a systemic number of leaf-local STOPs, a non-rate 403, a deterministic query error;
  (c) no duplicate dispatch after a retry: the closure never dispatches and keeps the wake's real
      count, the durable verdict after a lost decision is reconcile/STOP (never a new intent), a held
      CURRENT entity is never started again, a skipped FULL leaf is never dispatched;
  (d) priority: RECENT_90 (11.08.2026 → today, rolling) before older history, stable inside tiers.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import date, datetime, timezone
from types import SimpleNamespace
import contextlib
import io
import json

from tools.tenancy import durable_daily as DD, durable_plan as D, tenant_backfill as BF
from tools.tenancy import cloud_access as A, cloud_controller as C, controller_cadence as CC

NOW = datetime(2026, 11, 9, 9, tzinfo=timezone.utc)
TRANSIENT = 'cloud transport HTTP 503'


@contextmanager
def patched(obj, name, value):
    saved = getattr(obj, name); setattr(obj, name, value)
    try: yield
    finally: setattr(obj, name, saved)


class Faulty:
    """Real protocol tables with scripted failures per operation (None = pass through)."""
    def __init__(self, inner, faults):
        self.inner, self.faults, self.calls = inner, faults, []

    def _step(self, op):
        self.calls.append(op)
        queue = self.faults.get(op) or []
        error = queue.pop(0) if queue else None
        if error is not None: raise error

    def list_tables(self, ds, with_expiry=False):
        self._step('list'); return self.inner.list_tables(ds, with_expiry=with_expiry)

    def get_table(self, ds, name):
        self._step('get:' + name[:4]); return self.inner.get_table(ds, name)

    def create_marker(self, ds, name, labels, description):
        self._step('create:' + name[:4]); return self.inner.create_marker(ds, name, labels, description)


def claimed(faults=None, dispatches=0):
    from tools.tenancy.controller_cadence_image_check import protocol
    m, _, _, make, _, objects, commits = protocol()
    b = make(0); claim = CC.acquire(b)
    b.tables = Faulty(b.tables, faults or {}); b.wake_activity = {'dispatch_attempts': dispatches}
    return m, b, claim, objects, commits


def closure(objects, m, generation=1):
    from tools.tenancy.validation import parse_tenant_json
    value = objects.get(CC.names(m['hash'], generation)[1])
    return parse_tenant_json(value[1]) if value else None


def transient():
    return A.TransientReadError(TRANSIENT)


def closure_retry_check():
    """(a)+(c) CONTROL_CLOSURE: token read lost before the POST, and readback lost after it."""
    pauses = []
    m, b, claim, objects, _ = claimed({'create:BFCX': [transient()]}, dispatches=1)
    C.close_decision(b, {'status': 'DISPATCHED', 'source_dispatches': 1}, pause=pauses.append)
    v = closure(objects, m)
    assert pauses == [DD.CLOSE_RETRY_DELAYS[0]] and b.wake_claim is None and CC.closed(b, claim)
    assert (v['status'], v['source_dispatches']) == ('DISPATCHED', 1) and b.wake_activity['closure_read_retries'] == 1
    assert b.tables.calls.count('create:BFCX') == 2 and b.wake_activity['dispatch_attempts'] == 1   # no dispatch, no new count
    # Readback lost after the create landed; the later attempt carries another status (main's
    # WAITING_READ_RETRY). The first closure is canonical: accepted as is, never a conflict STOP.
    m, b, claim, objects, _ = claimed({'get:BFCX': [None, transient()]}, dispatches=1)
    try: CC.close(b, {'status': 'DISPATCHED', 'source_dispatches': 1})
    except A.TransientReadError: pass
    else: raise AssertionError('scripted readback loss not reached')
    before = deepcopy(objects)
    C.close_decision(b, {'status': 'WAITING_READ_RETRY', 'reason': 'TRANSIENT_READ_TRANSPORT'}, pause=pauses.append)
    assert objects == before and closure(objects, m)['status'] == 'DISPATCHED' and b.wake_claim is None
    assert b.tables.calls.count('create:BFCX') == 1                                               # never a second POST
    return 'PASS'


def closure_fail_closed_check():
    """(b) exhaustion, budget, unknown POST outcome and conflict stay fail-closed."""
    m, b, claim, objects, _ = claimed({'create:BFCX': [transient() for _ in range(1 + len(DD.CLOSE_RETRY_DELAYS))]})
    pauses = []
    try: C.close_decision(b, {'status': 'MONITORING', 'source_dispatches': 0}, pause=pauses.append)
    except A.TransientReadError: pass
    else: raise AssertionError('exhausted closure retries must surface')
    assert pauses == list(DD.CLOSE_RETRY_DELAYS) and closure(objects, m) is None
    # The wake budget bounds the retry: no attempt past the closure reserve.
    from tools.tenancy import wake_budget as WB
    m, b, claim, objects, _ = claimed({'create:BFCX': [transient()]})
    b.wake_budget = WB.Budget(clock=lambda: 0, platform=WB.CLOSE_RESERVE_SECONDS + 60 + 1)
    pauses = []
    try: C.close_decision(b, {'status': 'MONITORING', 'source_dispatches': 0}, pause=pauses.append)
    except A.TransientReadError: pass
    else: raise AssertionError('retry beyond the wake budget')
    assert pauses == [] and closure(objects, m) is None
    # A non-transient create failure is an unknown POST outcome: readback only, no repeat, STOP.
    m, b, claim, objects, _ = claimed({'create:BFCX': [BF.TT.TableError('cloud transport HTTP 500')]})
    from tools.tenancy.controller_diagnostics import GateFailure
    try: C.close_decision(b, {'status': 'MONITORING', 'source_dispatches': 0}, pause=pauses.append)
    except GateFailure as failure: assert failure.diagnostic == {'version': 1, 'stage': 'CONTROL_CLOSURE', 'category': 'CLOUD_METADATA'}
    else: raise AssertionError('unknown closure POST outcome accepted')
    try: C.close_decision(b, {'status': 'MONITORING', 'source_dispatches': 0}, pause=pauses.append)
    except GateFailure as failure: assert failure.diagnostic['category'] == 'EVIDENCE'
    else: raise AssertionError('closure POST repeated after an unknown outcome')
    assert b.tables.calls.count('create:BFCX') == 1 and pauses == []
    # The pre-existing STOP fallback still closes the claim STOPPED (CLX recovery expects it).
    CC.close(b, {'status': 'STOPPED', 'source_dispatches': 0}, fallback=True)
    assert closure(objects, m)['status'] == 'STOPPED'
    # A foreign/corrupt closure under our name is a conflict, never adopted.
    m, b, claim, objects, _ = claimed({'create:BFCX': [transient()]})
    name = CC.names(m['hash'], 1)[1]
    def tamper(seconds): objects[name] = ({'kind': 'controller_close', 'root': m['hash'][:16]}, '{"forged": true}')
    try: C.close_decision(b, {'status': 'MONITORING', 'source_dispatches': 0}, pause=tamper)
    except GateFailure as failure: assert failure.diagnostic['stage'] == 'CONTROL_CLOSURE'
    else: raise AssertionError('foreign closure adopted')
    return 'PASS'


def entrypoint_check():
    """The -m entrypoint: one lost closure read → typed result rc 0; exhausted → typed STOP rc 2."""
    def run(faults, result):
        m, b, claim, objects, commits = claimed(faults)
        out = io.StringIO()
        with patched(C, 'bootstrap', lambda env: (b, m['hash'])), patched(C, 'bounded_wake', lambda *a, **k: dict(result)), \
             patched(DD, 'sleep', lambda s: None), contextlib.redirect_stdout(out):
            rc = C.main()
        return rc, D.parse_tenant_json(out.getvalue().strip().splitlines()[-1]), commits, closure(objects, m)
    rc, printed, commits, value = run({'create:BFCX': [transient()]}, {'status': 'MONITORING', 'source_dispatches': 0, 'progress': {}})
    assert rc == 0 and printed['status'] == 'MONITORING' and printed['closure_read_retries'] == 1 and not commits
    assert value['status'] == 'MONITORING'
    rc, printed, commits, value = run({'create:BFCX': [transient() for _ in range(1 + len(DD.CLOSE_RETRY_DELAYS))]},
                                      {'status': 'MONITORING', 'source_dispatches': 0, 'progress': {}})
    assert rc == 2 and printed['status'] == 'STOPPED' and printed['diagnostic'] == {'version': 1, 'stage': 'CONTROL_CLOSURE', 'category': 'CLOUD_METADATA'}
    assert len(commits) == 1 and commits[0][1] == 'STOPPED' and value['status'] == 'STOPPED'   # fallback closure as before
    return 'PASS'


def reporting_check():
    """(a) a transient read in the post-closure report omits it; the decision stays closed and rc 0."""
    from tools.tenancy import full_controller as H
    b = SimpleNamespace(store=SimpleNamespace(history=lambda root: [{'kind': 'FULL_MANIFEST'}]), wake_activity={})
    closed = []
    with patched(H, 'wake', lambda *a: ({'status': 'CHUNK_COMPLETE', 'source_dispatches': 0}, b)), \
         patched(H, 'monitoring', lambda *a: (_ for _ in ()).throw(transient())):
        result = C.bounded_wake('a' * 64, b, decision_complete=closed.append)
    assert closed and result['progress'] == {'omitted': 'TRANSIENT_READ'} and result['status'] == 'CHUNK_COMPLETE'
    with patched(H, 'wake', lambda *a: ({'status': 'CHUNK_COMPLETE', 'source_dispatches': 0}, b)), \
         patched(H, 'monitoring', lambda *a: (_ for _ in ()).throw(BF.B.EvidenceError('watermark'))):
        try: C.bounded_wake('a' * 64, b, decision_complete=closed.append)
        except BF.B.EvidenceError: pass
        else: raise AssertionError('evidence failure in reporting swallowed')
    return 'PASS'


def query_check():
    """(a)/(b) BigQuery SELECT: unfinished or backend/rate error = transient; others = evidence."""
    from tools.tenancy.controller_diagnostics import gate, GateFailure
    c = BF.target('client_001')
    def select(*responses):
        queue = list(responses)
        def request(method, url, body=None): return deepcopy(queue.pop(0) if len(queue) > 1 else queue[0])
        return BF.select(c, 'SELECT 1 AS x', {}, request=request)
    pending = {'jobReference': {'jobId': 'j'}, 'jobComplete': False}
    for response, typed in ((pending, A.TransientReadError),
                            ({'jobReference': {'jobId': 'j'}, 'jobComplete': True, 'errors': [{'reason': 'backendError'}]}, A.TransientReadError),
                            ({'jobReference': {'jobId': 'j'}, 'jobComplete': True, 'errors': [{'reason': 'rateLimitExceeded'}, {'reason': 'internalError'}]}, A.TransientReadError),
                            ({'jobReference': {'jobId': 'j'}, 'jobComplete': True, 'errors': [{'reason': 'invalidQuery'}]}, BF.B.EvidenceError),
                            ({'jobReference': {'jobId': 'j'}, 'jobComplete': True, 'errors': [{'reason': 'backendError'}, {}]}, BF.B.EvidenceError),
                            ({'jobReference': {'jobId': 'j'}, 'jobComplete': True, 'pageToken': 't', 'rows': []}, BF.B.EvidenceError)):
        try: select(response)
        except Exception as error: assert type(error) is typed, (response, type(error))
        else: raise AssertionError('failed query accepted')
    assert select(pending, {'jobReference': {'jobId': 'j'}, 'jobComplete': True, 'schema': {'fields': [{'name': 'x', 'type': 'INTEGER'}]}, 'rows': [{'f': [{'v': '1'}]}]}) == [{'x': 1}]
    # Through a gate the transient read stays a retry; an evidence failure stays a typed STOP.
    try:
        with gate('QUOTA'): select(pending)
    except A.TransientReadError: pass
    else: raise AssertionError('gate swallowed a transient read')
    try:
        with gate('QUOTA'): select({'jobReference': {'jobId': 'j'}, 'jobComplete': True, 'errors': [{'reason': 'invalidQuery'}]})
    except GateFailure as failure: assert failure.diagnostic['stage'] == 'QUOTA'
    return 'PASS'


def wire_check():
    """(a)/(b) Google 403 rate protection on a read retries; any other 403 or a write stays hard."""
    import urllib.error
    def call(method, url, code, body):
        def opener(*a):
            class O:
                def open(self, request, timeout): raise urllib.error.HTTPError(url, code, 'x', {}, io.BytesIO(body))
            return O()
        with patched(A.urllib.request, 'build_opener', opener):
            try: A.wire(method, url, {} if method == 'POST' else None)
            except BF.TT.TableError as error: return type(error), str(error)
        raise AssertionError('HTTP error accepted')
    get = BF.TT.BQ + '/projects/mpa-t-client-001/datasets/tenant_locks/tables/BFCX_x'
    write = BF.TT.BQ + '/projects/mpa-t-client-001/datasets/tenant_locks/tables'
    rate = json.dumps({'error': {'errors': [{'reason': 'rateLimitExceeded', 'message': 'SECRET-DETAIL'}]}}).encode()
    denied = json.dumps({'error': {'errors': [{'reason': 'accessDenied'}]}}).encode()
    assert call('GET', get, 403, rate) == (A.TransientReadError, 'cloud transport HTTP 403')   # no body in the message
    assert call('POST', BF.TT.BQ + '/projects/mpa-t-client-001/queries', 403, rate)[0] is A.TransientReadError
    assert call('GET', get, 403, denied)[0] is BF.TT.TableError
    assert call('GET', get, 403, b'not json')[0] is BF.TT.TableError
    assert call('POST', write, 403, rate)[0] is BF.TT.TableError                                  # a write is never a read retry
    assert call('POST', write, 503, b'')[0] is BF.TT.TableError
    assert call('GET', get, 503, b'')[0] is A.TransientReadError
    return 'PASS'


def write_ledger_check():
    """(b)+(c) a transient read after a sent dispatch write is a typed STOP with the ledger, never a retry."""
    from tools.tenancy.controller_diagnostics import write_boundary, GateFailure
    effects = dict.fromkeys(('lease', 'checkpoint', 'intent', 'run', 'receipt'), 'ABSENT')
    try:
        with write_boundary(effects): raise transient()
    except A.TransientReadError: pass                                                             # nothing sent: retry
    effects['intent'] = 'PRESENT'
    try:
        with write_boundary(effects): raise transient()
    except GateFailure as failure: assert failure.diagnostic['readback']['intent'] == 'PRESENT'
    else: raise AssertionError('retry after a sent write')
    # The next wake's verdict after a lost decision never elects a second intent.
    shard = 'c' * 64
    def rec(kind, seq): return dict(version=D.VERSION, root_hash=shard, kind=kind, sequence=seq, payload={'n': seq})
    eligible = {'status': 'ELIGIBLE'}
    assert D.decide_tick([rec('DISPATCH_INTENT', 1)], shard, eligible)['action'] == 'RECOVER_RECEIPT_OR_STOP'
    assert D.decide_tick([rec('DISPATCH_INTENT', 1), rec('DISPATCH_RECEIPT', 1)], shard, eligible)['action'] == 'RECONCILE'
    assert D.decide_tick([rec('DISPATCH_INTENT', 1), rec('DISPATCH_RECEIPT', 1)], shard, eligible, True)['action'] == 'MONITOR'
    return 'PASS'


def series(shard, n, *, open_last=None):
    out = []
    for q in range(1, n + 1):
        out.append(dict(version=D.VERSION, root_hash=shard, kind='DISPATCH_INTENT', sequence=q, payload={'q': q}))
        if open_last == 'intent' and q == n: continue
        out.append(dict(version=D.VERSION, root_hash=shard, kind='DISPATCH_RECEIPT', sequence=q, payload={'q': q}))
        if open_last == 'receipt' and q == n: continue
        out.append(dict(version=D.VERSION, root_hash=shard, kind='RECONCILED', sequence=q, payload={'q': q}))
    return out


def current_hold_check():
    """(a)/(b)/(c) CURRENT: a settled entity's certification evidence holds only it; open authority stays global."""
    from tools.tenancy import current_controller as CUR, current_policy as P
    from tools.tenancy.controller_diagnostics import GateFailure
    day = NOW.astimezone(BF.B.MSK).date(); shard = 'd' * 64
    doc = {'ack_hash': 'e' * 64, 'runtime_plan': {'entity': 'stocks', 'from': str(day), 'to': str(day)}}
    def tick(history, error):
        starts = []
        b = SimpleNamespace(current_root='f' * 64, clock=lambda: NOW, wake_activity={},
                            preflight=lambda leaf: None, active_runtime_execution=lambda: False,
                            store=SimpleNamespace(history=lambda s, **k: deepcopy(history), commit=lambda *a: starts.append(('commit',) + a[:2])),
                            quota=lambda leaf, now: {'status': 'ELIGIBLE'}, state=lambda d: {'complete': True, 'sequence': 1, 'progress': {}},
                            start=lambda *a: starts.append('start'))
        def certificate(*a): raise error
        with patched(CUR, 'certificate', certificate):
            try: result = CUR.source_tick(b, {'hash': 'a' * 64}, day, {'plan': doc, 'shard': shard})
            except Exception as raised: result = raised
        return result, b.wake_activity, starts
    result, activity, starts = tick(series(shard, 2), BF.B.EvidenceError('PILOT_DATA_RECONCILIATION_FAILED: source/key/persisted counts differ'))
    assert result == {'status': 'WAITING', 'source_dispatches': 0, 'operational_entity': 'stocks', 'basis': 'CURRENT_ENTITY_EVIDENCE_HOLD'}, result
    assert activity['current_entities'] == {'stocks': 'UNKNOWN'} and activity['current_holds'] == [{'entity': 'stocks', 'day': str(day)}]
    assert not starts                                                                             # never dispatched again, nothing written
    again, _, starts = tick(series(shard, 2), BF.B.EvidenceError('same evidence next wake'))
    assert again['basis'] == 'CURRENT_ENTITY_EVIDENCE_HOLD' and not starts
    from tools.tenancy.cloud_tick import OverlapWait, SourceDispatchPaused
    from tools.tenancy.controller_diagnostics import PILOT_HELD
    for history, error in ((series(shard, 0), BF.B.EvidenceError('x')),
                           (series(shard, 2), GateFailure('CHECKPOINT', 'EVIDENCE')),
                           (series(shard, 2), OverlapWait('WAIT_CONTROLLER_FINALIZATION')),
                           (series(shard, 2), SourceDispatchPaused('paused')),
                           (series(shard, 2), BF.B.EvidenceError(PILOT_HELD)),
                           (series(shard, 2), transient())):
        raised, activity, starts = tick(history, error)
        assert raised is error and not activity.get('current_holds') and not starts, (history, error)
    # Defence in depth: any open authority in the shard (intent without receipt or reconciliation) is never held.
    for open_last in ('intent', 'receipt'):
        activity = {}
        assert DD.hold_current(activity, 'stocks', series(shard, 2, open_last=open_last), str(day), today=True) is False and not activity
    # The wake loop: a held entity does not stop the next one; statuses per entity.
    p = {'hash': 'a' * 64, 'first_day': '2026-10-14', 'fbo_contract_hash': None}
    items = {e: {'day': str(day), 'policy_hash': p['hash'], 'plan': {'ack_hash': e, 'runtime_plan': {'entity': e, 'from': str(day)}}, 'shard': 's-' + e} for e in P.ORDER if e != 'fbo_postings'}
    records = [{'kind': 'CURRENT_CERT', 'payload': {'fact': {'entity': 'catalog'}}}]
    base = SimpleNamespace(clock=lambda: NOW, wake_activity={}, store=SimpleNamespace(history=lambda s, **k: [], commit=lambda *a: 'h'), state=lambda d: {'progress': {}})
    ticks = []
    def loop_tick(b, pp, d, it):
        e = it['plan']['runtime_plan']['entity']; ticks.append(e)
        if e == 'stocks':
            assert DD.hold_current(base.wake_activity, e, series('9' * 64, 1), str(d), today=True)
            return {'status': 'WAITING', 'source_dispatches': 0, 'operational_entity': e, 'basis': 'CURRENT_ENTITY_EVIDENCE_HOLD'}
        return {'status': 'DISPATCHED', 'source_dispatches': 1, 'operational_entity': e}
    from tools.tenancy import recent_priority as RP, fbo_diagnostic_owner as DIAG, finance_catchup as FC, ads_catchup as AC, current_ads as ADS
    with patched(CUR, 'policy', lambda b: p), patched(CC, 'ensure', lambda b: None), patched(RP, 'pending_receipts', lambda *a: []), \
         patched(CUR, 'cycles', lambda b, pp: {str(day): {}}), patched(CUR, 'plans', lambda b, pp, d: (records, items)), \
         patched(CUR, 'validate_certificate', lambda *a: a[3]), patched(CUR, 'Backend', lambda *a: None), patched(CUR, 'source_tick', loop_tick), \
         patched(DIAG, 'queued', lambda b: None), patched(FC, 'scan', lambda *a: {'pending': []}), patched(FC, 'wake', lambda *a: None), \
         patched(AC, 'scan', lambda *a, **k: {'authorities': [], 'pending': [], 'next': None, 'halted': None}), patched(ADS, 'qualified', lambda b: False):
        result = CUR.wake(base, [], {}, {}, set())
    assert ticks == ['stocks', 'prices'] and result['operational_entity'] == 'prices'
    assert base.wake_activity['current_entities'] == {'catalog': 'COMPLETE', 'stocks': 'UNKNOWN', 'prices': 'STALE', 'finance_accrual': 'STALE'}
    return 'PASS'


def full_leaf_check():
    """(a)/(b) FULL: a completion proof failure of a reconciled leaf takes the S4-admissible typed leaf class."""
    from tools.tenancy import full_controller as H, full_history as F, current_independence as CI
    from tools.tenancy.controller_diagnostics import GateFailure, safe
    from pipelines.ozon.runtime import full_resume as FR
    m = FR.manifest(); index = 10; leaf_day = date(2026, 10, 23)
    doc = F.render_leaf(m, index, leaf_day); shard = F.shard_root(m, index, doc); item = {'index': index, 'plan': doc, 'shard_root': shard}
    def wake(history, error, *, dependency=False):
        b = SimpleNamespace(index=None, preflight=lambda leaf: None, active_runtime_execution=lambda: False,
                            store=SimpleNamespace(history=lambda s, **k: deepcopy(history), commit=lambda *a: (_ for _ in ()).throw(AssertionError('write'))),
                            quota=lambda leaf, now: {'status': 'ELIGIBLE'}, state=lambda d: {'complete': True, 'sequence': 2, 'progress': {}},
                            clock=lambda: NOW)
        def verify(*a, **k): raise error
        with patched(BF, 'verify_coverage', verify):
            try: return H.leaf_wake(b, m, index, item, [], dependency=dependency)
            except Exception as raised: return raised
    raised = wake(series(shard, 2), BF.B.EvidenceError('PILOT_DATA_RECONCILIATION_FAILED: source/key/persisted counts differ'))
    assert isinstance(raised, GateFailure) and raised.diagnostic['stage'] == 'PERSISTED_SOURCE_RECONCILIATION', raised
    stop = dict(version=D.VERSION, root_hash=m['hash'], kind='STOPPED', sequence=0,
                payload={'reason': 'CONTROLLER_GATE_OR_EVIDENCE_FAILURE', 'controller_execution': 'projects/x/executions/e', 'diagnostic': safe(raised)})
    assert stop['payload']['diagnostic']['context']['shard'] == shard and CI.classify(stop, m) == ('CANDIDATE', index)
    hs = series(shard, 2)
    assert stop['payload']['diagnostic']['context']['intent_hash'] == D.digest(hs[3]) and stop['payload']['diagnostic']['context']['receipt_hash'] == D.digest(hs[4])
    for open_last in ('intent', 'receipt'):
        assert DD.local_leaf_failure(series(shard, 2, open_last=open_last), index, shard) is None
    assert DD.local_leaf_failure([], index, shard) is None
    from tools.tenancy.cloud_tick import OverlapWait
    for history, error, dependency in ((series(shard, 2), BF.B.EvidenceError('x'), True),
                                       (series(shard, 2), OverlapWait('WAIT_ACTIVE_CONTROLLER'), False),
                                       (series(shard, 2), GateFailure('LEASE', 'EVIDENCE'), False),
                                       (series(shard, 2), transient(), False)):
        assert wake(history, error, dependency=dependency) is error, (error, dependency)
    return 'PASS'


def breaker_check():
    assert DD.full_continues({'admitted': True, 'local_full_leaves': [465]}) == {465}
    assert DD.full_continues({'admitted': True, 'local_full_leaves': list(range(DD.MAX_LOCAL_FULL_LEAVES))}) == set(range(DD.MAX_LOCAL_FULL_LEAVES))
    for bad in ({'admitted': True, 'local_full_leaves': list(range(DD.MAX_LOCAL_FULL_LEAVES + 1))},
                {'admitted': False, 'blocker': 'X'}, {'admitted': True, 'local_full_leaves': []},
                {'admitted': True, 'local_full_leaves': ['1']}, {'admitted': True, 'local_full_leaves': [True]}, None):
        assert DD.full_continues(bad) is None, bad
    return 'PASS'


def priority_check():
    """(d) RECENT_90 first, stable inside each tier; the production BFRP order is already recent-first."""
    from tools.tenancy import recent_priority as RP
    from pipelines.ozon.runtime import full_resume as FR
    m = FR.manifest(); today = date(2026, 11, 9)
    assert str(DD.window_start(today)) == '2026-08-11'
    def recent(i): return m['programs'][i]['kind'] == 'DATED' and m['programs'][i]['to'] >= '2026-08-11'
    old = RP.ordered(m, set(), None)
    first_old = next(n for n, i in enumerate(old) if not recent(i))
    assert any(recent(i) for i in old[first_old:])                                               # rank order alone is not recent-first
    tiered = DD.recent_first(m, old, today)
    window = [i for i in tiered if recent(i)]
    assert window and tiered[:len(window)] == [i for i in old if recent(i)] and tiered[len(window):] == [i for i in old if not recent(i)]
    assert sorted(tiered) == sorted(old)
    selection = RP.selection(m)
    p = {'selection': [{'index': i} for i in selection], 'parents': sorted({m['programs'][i]['t5_chunk_id'] for i in selection})}
    production = RP.ordered(m, set(), p)
    assert DD.recent_first(m, production, today) == production                                    # no reordering of the qualified order
    done = set(window[:5])
    rolled = DD.recent_first(m, RP.ordered(m, done, p), date(2026, 11, 20))                     # the window rolls daily
    assert all(m['programs'][i]['to'] >= '2026-08-22' for i in rolled[:3]) and not done & set(rolled)
    return 'PASS'


def release_check():
    """durable_daily_version=1 only on top of the control-closure recovery release, adapter PASS;
    every accepted certificate family (CR v4, TG V1, CLX) stays registered in the one acceptance set."""
    from tools.tenancy import orchestration_contract as O, control_closure_image_check as CLX, stop_acceptance as SA
    from pipelines.ozon.runtime import full_resume as FR
    import inspect
    m = FR.manifest(); old = D.parse_tenant_json((BF.REPO / 'infra/tenant/releases/backfill' / f'{CLX.OLD}.json').read_text())
    c = deepcopy(BF.target('client_001')); c['marketplaces']['ozon']['runtime_image'] = old['runtime_image']
    new = CLX.new_release(old); new['durable_daily_version'] = 1; new['verification']['durable_daily_adapter'] = 'PASS'
    settings = dict(release=new['source_sha'], root_hash=m['hash'], scheduler_state='PAUSED')
    assert O.block(c, settings, BF.REPO, new)['job']['env']['CONTROLLER_SOURCE_SHA'] == new['source_sha']
    bad = [dict(new, durable_daily_version=2), dict(new, durable_daily_version=True),
           dict(new, verification={k: v for k, v in new['verification'].items() if k != 'durable_daily_adapter'})]
    no_clx = deepcopy(new); no_clx.pop('control_closure_recovery_version'); no_clx['verification'].pop('control_closure_recovery_adapter'); bad.append(no_clx)
    for r in bad:
        try: O.block(c, settings, BF.REPO, r)
        except ValueError: pass
        else: raise AssertionError('unqualified durable daily release accepted')
    assert O.block(c, dict(release=CLX.OLD, root_hash=m['hash'], scheduler_state='ENABLED'), BF.REPO, old)['job']['image'] == old['image']
    assert {'durable_daily.py', 'durable_daily_image_check.py'} <= set(O.SOURCE_FILES)
    source = inspect.getsource(SA._revalidate)
    for family in ('CR.load', 'TG.load', 'CLX.load', 'FL.load', 'CURRENT_RECON.load'):
        assert family in source, family
    return 'PASS'


CHECKS = (('closure_retry', closure_retry_check), ('closure_fail_closed', closure_fail_closed_check),
          ('entrypoint', entrypoint_check), ('reporting', reporting_check), ('query', query_check),
          ('wire', wire_check), ('write_ledger', write_ledger_check), ('current_hold', current_hold_check),
          ('full_leaf', full_leaf_check), ('breaker', breaker_check), ('priority', priority_check),
          ('release', release_check))


def check():
    for name, fn in CHECKS:
        assert fn() == 'PASS', name
    return {'durable_daily_adapter': 'PASS'}


if __name__ == '__main__':
    print(json.dumps(check(), sort_keys=True))
