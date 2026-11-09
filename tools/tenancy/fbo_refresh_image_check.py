"""Network-free in-image proof of the owner FBO refresh (OWNER_FBO_REFRESH_V1, Heldi 2026-11-09).

  * authority: closed, sealed, chained owner marker bound to the FULL root, the CURRENT policy (which
    must not own FBO) and the proven FBO transport contract; refresh option is one of the two owner
    configurations (default daily 7 + weekly 8–35; alternative daily 14);
  * schedule: DAILY [d-K, d-1], WEEKLY [d-35, d-8] on its weekday (option 1 only), HISTORY newest
    window first; windows ≤ window_days; deterministic plans and roots per unit;
  * verdict: per-run FBO day verdict — COMPLETE, SOURCE_DISAPPEARED (local PARTIAL), else fail-closed;
  * tick: ledger + intent before the only dispatch, lost reply never repeated, daily dispatch budget,
    COMPLETE/PARTIAL only through the per-run verdict and only with today's Catalog CERT;
  * scope: no marker → one metadata read; unqualified release elects nothing new but drains; an
    elected unfinished unit is continued before a new one; two open units fail;
  * order: open refresh drained before new CURRENT work; new refresh work only after CURRENT and the
    finance catch-up have nothing left;
  * release: fbo_refresh_version requires control_closure and its adapter.
"""
from copy import deepcopy
from datetime import date, datetime, timezone
import json
from types import SimpleNamespace
from tools.tenancy import fbo_refresh as FBR, finance_catchup as FC, current_controller as CUR
from tools.tenancy import current_contract as K, durable_plan as D, tenant_backfill as BF
from tools.tenancy.finance_catchup_image_check import Store, patched, rejects, fixture as catchup_fixture
from tools.tenancy.validation import parse_tenant_json

NOW = datetime(2026, 11, 9, 5, 30, tzinfo=timezone.utc)               # Monday, MSK 08:30
CREATED = '2026-11-09T05:00:00+00:00'
RUN = 'bf-00000000-0000-0000-0000-000000000002'
EXECUTION = 'projects/mpa-t-client-001/locations/europe-west1/jobs/tenant-backfill-controller/executions/tenant-backfill-controller-synthetic'


def fixture(mode=FBR.DEFAULT_MODE, **kw):
    c, m, p, _ = catchup_fixture()
    a = FBR.authority(c, m, p, serial=1, previous=None, owner_ack_sha256='4' * 64, created_at=CREATED,
                      history_from='2026-08-11', history_to='2026-11-08', mode=mode, **kw)
    return c, m, p, a


def reseal(a, **changes):
    x = {k: v for k, v in a.items() if k != 'hash'}; x.update(changes); return FBR.sealed(x)


def authority_check():
    c, m, p, a = fixture()
    assert FBR.validate(a, c, m, p, NOW) is a and a['refresh'] == FBR.MODES['DAILY7_WEEKLY8_35']
    assert FBR.name(p['hash'], 1) == 'BFFBOREFRESH_' + p['hash'][:16] + '_01'
    assert FBR.value(a) == ({'kind': 'fbo_refresh', 'root': a['hash'][:16]}, D.encoded(a))
    assert FBR.validate(fixture('DAILY14')[3], c, m, p, NOW)
    bad = {
        'custom refresh': reseal(a, refresh=dict(a['refresh'], daily_days=10)),
        'unknown mode': reseal(a, refresh={'mode': 'DAILY30'}),
        'other contract': reseal(a, fbo_contract_hash='e' * 64),
        'history includes today': reseal(a, history_to='2026-11-09'),
        'history too long': reseal(a, history_from='2026-06-01'),
        'unsorted history': reseal(a, history_from='2026-11-08', history_to='2026-08-11'),
        'window 8 days': reseal(a, window_days=8),
        'units below window': reseal(a, max_units_per_run=6),
        'units over 20': reseal(a, max_units_per_run=21),
        'zero dispatch budget': reseal(a, max_dispatches_per_day=0),
        'dispatch budget 13': reseal(a, max_dispatches_per_day=13),
        'parallel source': reseal(a, source_concurrency=2),
        'other entity': reseal(a, entity='finance_accrual'),
        'foreign tenant': reseal(a, tenant='client_002'),
        'other FULL root': reseal(a, historical_root='e' * 64),
        'other policy': reseal(a, policy_hash='f' * 64),
        'future': reseal(a, created_at='2026-11-10T00:00:00+00:00'),
        'serial 2 alone': reseal(a, serial=2, previous=a['hash']),
        'bool version': reseal(a, version=True),
        'tampered': dict(a, window_days=3),
        'extra field': dict(a, note='x'),
    }
    for label, x in bad.items():
        assert rejects(lambda: FBR.validate(x, c, m, p, NOW)), label
    assert rejects(lambda: FBR.validate(a, c, m, dict(p, fbo_contract_hash='1' * 64), NOW))      # CURRENT owns FBO
    assert rejects(lambda: FBR.authority(c, m, p, serial=1, previous=None, owner_ack_sha256='4' * 64, created_at=CREATED,
                                         history_from='2026-08-11', history_to='2026-11-08', mode='DAILY30'))
    return 'PASS'


def schedule_check():
    _, _, _, a = fixture()
    monday, tuesday = date(2026, 11, 9), date(2026, 11, 10)
    units = FBR.schedule(a, monday)
    daily = [u for u in units if u['kind'] == 'DAILY']; weekly = [u for u in units if u['kind'] == 'WEEKLY']
    history = [u for u in units if u['kind'] == 'HISTORY']
    assert daily == [FBR.unit('DAILY', monday, date(2026, 11, 2), date(2026, 11, 8))]
    assert [(u['from'], u['to']) for u in weekly] == [('2026-10-26', '2026-11-01'), ('2026-10-19', '2026-10-25'),
                                                       ('2026-10-12', '2026-10-18'), ('2026-10-05', '2026-10-11')]
    assert history[0]['to'] == '2026-11-08' and history[-1]['from'] == '2026-08-11' and len(history) == 13
    days = set()
    for u in history:
        span = (date.fromisoformat(u['to']) - date.fromisoformat(u['from'])).days + 1
        assert 1 <= span <= 7
        new = {date.fromordinal(date.fromisoformat(u['from']).toordinal() + i) for i in range(span)}
        assert not new & days; days |= new
    assert len(days) == 90                                                                         # 11.08 → 08.11, no gap/overlap
    assert [u['kind'] for u in units] == ['DAILY'] + ['WEEKLY'] * 4 + ['HISTORY'] * 13               # election order
    assert not [u for u in FBR.schedule(a, tuesday) if u['kind'] == 'WEEKLY']
    b14 = fixture('DAILY14')[3]
    d14 = FBR.schedule(b14, monday)
    assert [(u['kind'], u['from'], u['to']) for u in d14 if u['kind'] != 'HISTORY'] == [
        ('DAILY', '2026-11-02', '2026-11-08'), ('DAILY', '2026-10-26', '2026-11-01')]               # option 2: no weekly
    docs = [FBR.document(a, u) for u in units]
    for u, doc in zip(units, docs):
        rp = doc['runtime_plan']
        assert (rp['entity'], rp['from'], rp['to'], rp['window_days'], doc['max_units'], doc['max_requests']) == ('fbo_postings', u['from'], u['to'], 1, 7, 400)
        assert BF.validate_plan(doc, doc['ack_hash']) and FBR.document(a, u) == doc
    assert len({d['runtime_plan']['plan_id'] for d in docs}) == len(docs)
    assert len({FBR.shard(a, u, d) for u, d in zip(units, docs)}) == len(docs) and FBR.scope_root(a) not in {FBR.shard(a, u, d) for u, d in zip(units, docs)}
    budget = FBR.budget(a, monday)
    assert budget == {'ceiling_requests_per_day': 3200, 'refresh_requests_today': (7 + 28) * 25, 'history_requests_once': 90 * 25}
    return 'PASS'


def verdict_check():
    V = BF.fbo_day_verdict
    assert V(10, 10, 10, 10) == 'COMPLETE'
    assert V(12, 12, 10, 10) == BF.FBO_SOURCE_DISAPPEARED                                         # 2 old rows not returned again
    assert V(0, 0, 0, 0) == 'COMPLETE'
    for args in ((12, 11, 10, 10), (10, 10, 9, 10), (9, 9, 9, 10), (11, 11, 11, 10), (10, 10, 10, True), (-1, -1, 0, 0)):
        assert V(*args) is None, args                                                              # duplicates / unproven run → global
    return 'PASS'


def elect(store, a, seq, u, day='2026-11-09'):
    doc = FBR.document(a, u)
    item = {'unit': u, 'authority_hash': a['hash'], 'plan': doc, 'shard': FBR.shard(a, u, doc), 'elected_on': day}
    store.commit(FBR.scope_root(a), 'REFRESH_PLAN', seq, item, NOW); return item


def tick_backend(a, seq, u, *, complete=False, start=None, store=None, verify=None):
    store = store or Store(); item = elect(store, a, seq, u); calls = []
    doc = item['plan']
    def default_start(d, before, after):
        before(dict(run_id=RUN, lease_generation=7, ack_hash=doc['ack_hash'], job='synthetic', overrides={}))
        calls.append('source_dispatch')
        after(dict(run_id=RUN, lease_generation=7, ack_hash=doc['ack_hash'], operation='op'))
    state = {'plan_id': doc['runtime_plan']['plan_id'], 'sequence': 1 if complete else 0, 'complete': complete, 'progress': {}}
    b = SimpleNamespace(c=BF.target('client_001'), clock=lambda: NOW, refresh_root=FBR.scope_root(a), current_execution=EXECUTION,
        store=store, preflight=lambda leaf: calls.append('preflight'), quota=lambda *x: {'status': 'ELIGIBLE', 'allowance': 0},
        active_runtime_execution=lambda: False, state=lambda d: state, start=start or default_start,
        reconcile=lambda payload: {'source_complete': True})
    return b, item, store, calls


def tick_check():
    c, m, p, a = fixture(); u = FBR.schedule(a, date(2026, 11, 9))[0]
    b, item, store, calls = tick_backend(a, 0, u)
    got = FBR.tick(b, a, 0, item)
    assert got['status'] == 'DISPATCHED' and got['source_dispatches'] == 1 and calls == ['preflight', 'source_dispatch']
    kinds = [(r['kind'], r['root_hash'] == FBR.scope_root(a)) for r in store.records]
    assert kinds == [('REFRESH_PLAN', True), ('REFRESH_DISPATCH', True), ('DISPATCH_INTENT', False), ('DISPATCH_RECEIPT', False)]
    assert FBR.scope_dispatches_today(b, a) == 1
    # Daily budget: no dispatch beyond max_dispatches_per_day.
    small = reseal(a, max_dispatches_per_day=1)
    b, item, store, calls = tick_backend(small, 0, u)
    store.commit(FBR.scope_root(small), 'REFRESH_DISPATCH', 1, {'unit_sequence': 9, 'day': '2026-11-09', 'shard': '0' * 64}, NOW)
    got = FBR.tick(b, small, 0, item)
    assert got['basis'] == 'FBO_REFRESH_DAILY_BUDGET' and 'source_dispatch' not in calls
    # Lost reply: the elected intent is never repeated.
    attempts = []
    def lost(d, before, after):
        before(dict(run_id=RUN, lease_generation=7, ack_hash=d['ack_hash'], job='synthetic', overrides={}))
        attempts.append(1); raise OSError('synthetic lost reply')
    b, item, store, calls = tick_backend(a, 0, u, start=lost)
    for _ in range(2):
        assert rejects(lambda: FBR.tick(b, a, 0, item))
    assert len(attempts) == 1 and [r['kind'] for r in store.records].count('DISPATCH_INTENT') == 1
    # Completion through the per-run verdict.
    def completed(readback=None, error=None, catalog=True):
        b, item, store, calls = tick_backend(a, 0, u, complete=True)
        receipt = dict(run_id=RUN, lease_generation=7, ack_hash=item['plan']['ack_hash'], operation='op')
        for kind, payload in (('DISPATCH_INTENT', {'plan': item['plan']}), ('DISPATCH_RECEIPT', {'plan': item['plan'], 'receipt': receipt}), ('RECONCILED', {'source_complete': True})):
            store.commit(item['shard'], kind, 1, payload, NOW)
        def verify(doc, ack, backend, fbo_run_verdict=False):
            assert fbo_run_verdict is True
            if error: raise BF.B.EvidenceError(error)
            return {'source_sequence': 1, 'readback': readback}
        with patched(BF, 'verify_coverage', verify), patched(CUR, 'catalog_current', lambda bb, text: {'day': text} if catalog else None):
            return FBR.tick(b, a, 0, item), store
    days = [date.fromordinal(date(2026, 11, 2).toordinal() + i).isoformat() for i in range(7)]
    ok = [{'day': d, 'rows': 10, 'keys': 10, 'coverage': 'COMPLETE'} for d in days]
    got, store = completed(ok)
    cert = [r for r in store.records if r['kind'] == 'REFRESH_CERT'][0]
    assert got['refresh_verdict'] == 'REFRESH_CERT' and cert['root_hash'] == FBR.scope_root(a) and cert['sequence'] == 0
    assert cert['payload']['rows'] == 70 and cert['payload']['source_run_id'] == RUN
    gone = deepcopy(ok); gone[3].update(coverage='PARTIAL', reason=BF.FBO_SOURCE_DISAPPEARED, extra_rows=2, run_rows=10, rows=12, keys=12)
    got, store = completed(gone)
    part = [r for r in store.records if r['kind'] == 'REFRESH_PARTIAL'][0]['payload']
    assert got['reason'] == BF.FBO_SOURCE_DISAPPEARED and part['days'] == [{'day': days[3], 'extra_rows': 2, 'run_rows': 10}]
    unknown = deepcopy(ok); unknown[0]['coverage'] = 'UNKNOWN'
    assert completed(unknown)[0]['reason'] == 'SOURCE_RETENTION_COMPLETENESS_UNPROVEN'
    for code in FBR.DQ_CODES:
        assert completed(error=code + ': synthetic')[0]['reason'] == 'DQ_RECONCILIATION_FAILED'
    assert rejects(lambda: completed(error='PILOT_DATA_RECONCILIATION_FAILED: synthetic'))           # unproven run stays global
    assert rejects(lambda: completed(ok[:6]))                                                         # readback misses a day
    got, store = completed(ok, catalog=False)
    assert got['basis'] == 'FBO_REFRESH_AWAITS_TODAYS_CATALOG' and not any(r['kind'] in {'REFRESH_CERT', 'REFRESH_PARTIAL'} for r in store.records)
    return 'PASS'


def scope_backend(a, p, c, m, *, marker=True, release=True, store=None, now=NOW):
    reads = []
    from tools.tenancy import cloud_controller as CLOUD
    env = c['orchestration']['job']['env']
    rel = {'fbo_refresh_version': 1, 'verification': {'fbo_refresh_adapter': 'PASS'}} if release else {'verification': {}}
    objects = {('tenant_locks', CLOUD.descriptor_name(env['CONTROLLER_SOURCE_SHA'], m['hash'], env['HISTORICAL_SCHEDULER_STATE'])): ({}, D.encoded({'release': rel}))}
    if marker: objects[('ref', FBR.name(p['hash'], 1))] = FBR.value(a)
    def get_table(ds, n): reads.append(n); return objects.get((ds, n))
    store = store or Store()
    return SimpleNamespace(c=c, manifest=m, clock=lambda: now, store=store, tables=SimpleNamespace(get_table=get_table)), reads, store


def scope_check():
    c, m, p, a = fixture()
    b, reads, store = scope_backend(a, p, c, m, marker=False)
    s = FBR.scan(b, p)
    assert s['authorities'] == [] and reads == [FBR.name(p['hash'], 1)] and store.reads == 0 and FBR.entity_status(s) is None
    b, reads, store = scope_backend(a, p, c, m)
    s = FBR.scan(b, p)
    units = FBR.schedule(a, date(2026, 11, 9))
    assert s['next'] == (a, 0, units[0]) and s['halted'] is None and FBR.entity_status(s) == 'STALE'
    item = elect(store, a, 0, units[0])
    s = FBR.scan(b, p)
    assert s['next'] == (a, 0, item)                                                               # elected unfinished: continued
    store.commit(item['shard'], 'DISPATCH_INTENT', 1, {'plan': item['plan']}, NOW)
    store.commit(item['shard'], 'DISPATCH_RECEIPT', 1, {'plan': item['plan'], 'receipt': {'run_id': RUN, 'lease_generation': 7}}, NOW)
    s = FBR.scan(b, p)
    assert [x[1] for x in s['pending']] == [0] and FBR.receipts(b, p) == [{'run_id': RUN, 'lease_generation': 7}]
    store.commit(item['shard'], 'RECONCILED', 1, {}, NOW)
    store.commit(FBR.scope_root(a), 'REFRESH_CERT', 0, {'unit': item['unit'], 'authority_hash': a['hash'], 'ack_hash': item['plan']['ack_hash'], 'shard': item['shard']}, NOW)
    s = FBR.scan(b, p)
    assert not s['pending'] and s['complete'] == 1 and s['next'] == (a, 1, units[1]) and FBR.entity_status(s) == 'COMPLETE'
    # A PARTIAL unit is an incident, never a halt.
    item1 = elect(store, a, 1, units[1])
    store.commit(FBR.scope_root(a), 'REFRESH_PARTIAL', 1, {'unit': item1['unit'], 'authority_hash': a['hash'], 'ack_hash': item1['plan']['ack_hash'],
                 'shard': item1['shard'], 'status': 'PARTIAL', 'reason': BF.FBO_SOURCE_DISAPPEARED, 'days': [{'day': '2026-10-30', 'extra_rows': 1, 'run_rows': 9}]}, NOW)
    s = FBR.scan(b, p)
    assert s['halted'] is None and s['next'] == (a, 2, units[2]) and s['partial'][0]['reason'] == BF.FBO_SOURCE_DISAPPEARED
    summary = FBR.summary(s)
    assert summary['incidents'][0]['days'][0]['extra_rows'] == 1 and summary['fbo_status'] == 'PARTIAL'   # incident today
    # Next day: yesterday's DAILY is superseded by today's unit; history keeps its place.
    b2, _, _ = scope_backend(a, p, c, m, store=store, now=datetime(2026, 11, 10, 10, tzinfo=timezone.utc))
    s = FBR.scan(b2, p)
    assert s['next'][2] == FBR.unit('DAILY', date(2026, 11, 10), date(2026, 11, 3), date(2026, 11, 9)) and FBR.entity_status(s) == 'STALE'
    # Unqualified release: nothing new, but an open unit is still drained.
    b3, _, store3 = scope_backend(a, p, c, m, release=False)
    s = FBR.scan(b3, p)
    assert s['halted'] == 'RELEASE_NOT_QUALIFIED' and FBR.wake(b3, s, {'catalog': 1}, set(CUR.INDEPENDENT_ENTITIES)) is None
    it = elect(store3, a, 0, units[0]); store3.commit(it['shard'], 'DISPATCH_INTENT', 1, {'plan': it['plan']}, NOW)
    assert [x[1] for x in FBR.scan(b3, p)['pending']] == [0]
    # Two open units or a forged plan fail closed.
    b4, _, store4 = scope_backend(a, p, c, m)
    for seq in (0, 1):
        it = elect(store4, a, seq, units[seq]); store4.commit(it['shard'], 'DISPATCH_INTENT', 1, {'plan': it['plan']}, NOW)
    assert rejects(lambda: FBR.scan(b4, p))
    b5, _, store5 = scope_backend(a, p, c, m); it = elect(store5, a, 0, units[0])
    store5.commit(FBR.scope_root(a), 'REFRESH_PLAN', 0, dict(it, shard='0' * 64), NOW)
    assert rejects(lambda: FBR.scan(b5, p))
    return 'PASS'


def order_check():
    """CURRENT.wake: open refresh drained first; new refresh work only after CURRENT and catch-up."""
    from tools.tenancy import controller_cadence as CC, recent_priority as RP, fbo_diagnostic_owner as DIAG
    c, m, p, a = fixture(); day = NOW.astimezone(K.MSK).date()
    def item(e): return {'day': day.isoformat(), 'policy_hash': p['hash'], 'plan': {'ack_hash': e, 'runtime_plan': {'entity': e, 'from': '2026-11-08'}}, 'shard': 's-' + e}
    idle = {'authorities': [], 'pending': [], 'next': None, 'halted': None, 'complete': [], 'partial': [], 'receipts': []}
    def run(*, certified=(), refresh=None, catchup=None, catchup_result=None):
        calls = []; items = {e: item(e) for e in CUR.INDEPENDENT_ENTITIES}
        records = [{'kind': 'CURRENT_CERT', 'payload': {'fact': {'entity': e}}} for e in certified]
        base = SimpleNamespace(clock=lambda: NOW, store=SimpleNamespace(history=lambda *x, **k: [], commit=lambda *x: 'h'), state=lambda d: {'progress': {}})
        scope = refresh or {'authorities': ['a'], 'pending': [], 'next': (a, 0, FBR.schedule(a, day)[0]), 'halted': None,
                            'complete': 0, 'partial': [], 'receipts': [], 'dispatches_today': 0, 'today_daily': 'STALE', 'today_incidents': []}
        with patched(CUR, 'policy', lambda b: p), patched(CC, 'ensure', lambda b: None), patched(RP, 'pending_receipts', lambda *x: []), \
             patched(CUR, 'cycles', lambda b, pp: {day.isoformat(): {}}), patched(CUR, 'plans', lambda b, pp, d: (records, items)), \
             patched(CUR, 'validate_certificate', lambda *x: x[3]), patched(CUR, 'Backend', lambda *x: None), \
             patched(CUR, 'source_tick', lambda b, pp, d, it: calls.append(it['plan']['runtime_plan']['entity']) or {'status': 'WAITING', 'source_dispatches': 0}), \
             patched(DIAG, 'queued', lambda b: None), patched(FC, 'scan', lambda b, pp, fin: catchup or idle), \
             patched(FC, 'drain', lambda b, s: calls.append('catchup') or {'status': 'DISPATCHED', 'source_dispatches': 1}), \
             patched(FC, 'wake', lambda b, s, ce, pa: catchup_result), \
             patched(FBR, 'scan', lambda b, pp: scope), patched(FBR, 'Backend', lambda *x: SimpleNamespace()), \
             patched(FBR, 'tick', lambda b, aa, s, it: calls.append('refresh:' + str(s)) or {'status': 'DISPATCHED', 'source_dispatches': 1}), \
             patched(FBR, 'document', lambda aa, u: {'ack_hash': 'x', 'runtime_plan': {}}), patched(FBR, 'shard', lambda *x: 'f' * 64):
            result = CUR.wake(base, [], {}, {}, set())
        return calls, result
    item0 = {'unit': {}, 'plan': {}, 'shard': 'x'}
    open_scope = {'authorities': ['a'], 'pending': [(a, 0, item0)], 'next': None, 'halted': None, 'complete': 0, 'partial': [], 'receipts': [], 'dispatches_today': 0, 'today_daily': 'STALE', 'today_incidents': []}
    calls, result = run(refresh=open_scope)
    assert calls == ['refresh:0'] and result['status'] == 'DISPATCHED'                              # drained before CURRENT
    open_catchup = dict(idle, authorities=['c'], pending=[(None, date(2026, 10, 8), {})])
    assert rejects(lambda: run(refresh=open_scope, catchup=open_catchup))                            # never two authorities
    calls, result = run()
    assert calls == ['catalog'] and result is None                                                   # CURRENT first
    calls, result = run(certified=['catalog'])
    assert calls == ['stocks', 'prices', 'finance_accrual'] and result is None
    done = ['catalog', 'stocks', 'prices', 'finance_accrual']
    calls, result = run(certified=done, catchup_result={'status': 'DISPATCHED', 'source_dispatches': 1, 'catchup_day': '2026-10-08'})
    assert calls == [] and result['catchup_day'] == '2026-10-08'                                     # catch-up before refresh
    calls, result = run(certified=done)
    assert calls == ['refresh:0'] and result['source_dispatches'] == 1                               # then refresh
    calls, result = run(certified=done, refresh=dict(open_scope, pending=[], next=None, authorities=[]))
    assert calls == [] and result is None                                                            # no authority: as before
    return 'PASS'


def release_check():
    from tools.tenancy import orchestration_contract as O
    from tools.tenancy.validation import parse_tenant_json
    c = BF.target('client_001'); env = c['orchestration']['job']['env']
    rel = parse_tenant_json((BF.REPO / 'infra/tenant/releases/backfill/2253a311871fb4a3b9e4644d5c5de9f18d37a407.json').read_text())
    settings = {'release': rel['source_sha'], 'root_hash': env['BACKFILL_ROOT_HASH'], 'scheduler_state': 'ENABLED'}
    base = dict(rel, finance_catchup_version=1, control_closure_recovery_version=1,
                verification=dict(rel['verification'], finance_catchup_adapter='PASS', control_closure_recovery_adapter='PASS'))
    ok = dict(base, fbo_refresh_version=1, verification=dict(base['verification'], fbo_refresh_adapter='PASS'))
    assert O.block(c, settings, BF.REPO, release=ok)['job']['env']['CURRENT_DATA_MODE'] == K.TYPE
    for bad in (dict(ok, fbo_refresh_version=2), dict(ok, fbo_refresh_version=True), dict(ok, verification=base['verification']),
                {k: v for k, v in ok.items() if k != 'control_closure_recovery_version'}):
        assert rejects(lambda: O.block(c, settings, BF.REPO, release=bad))
    assert {'fbo_refresh.py', 'fbo_refresh_image_check.py'} <= set(O.SOURCE_FILES) and FBR.KINDS <= D.KINDS
    return 'PASS'


def log_shape_check():
    """Stream E (#383): the controller's JSON stdout line carries the literal token SOURCE_DISAPPEARED and
    current_entities.fbo_postings = PARTIAL (log-based metric heldi_source_disappeared + PARTIAL alert)."""
    from tools.tenancy import cloud_controller as CC_, full_controller as FH, wake_budget as WB
    c, m, p, a = fixture(); units = FBR.schedule(a, date(2026, 11, 9))
    b, _, store = scope_backend(a, p, c, m)
    item = elect(store, a, 0, units[4])                                                            # a WEEKLY unit elected today
    gone = [{'day': '2026-10-08', 'extra_rows': 2, 'run_rows': 1800}]
    store.commit(FBR.scope_root(a), 'REFRESH_PARTIAL', 0, {'unit': item['unit'], 'authority_hash': a['hash'], 'ack_hash': item['plan']['ack_hash'],
                 'shard': item['shard'], 'status': 'PARTIAL', 'reason': BF.FBO_SOURCE_DISAPPEARED, 'days': gone}, NOW)
    summary = FBR.summary(FBR.scan(b, p))
    def line(result, summ):
        full = SimpleNamespace(fbo_refresh_summary=summ)
        backend = SimpleNamespace(store=SimpleNamespace(history=lambda root: [{'kind': 'FULL_MANIFEST'}]))
        with patched(FH, 'wake', lambda bb, root: (result, full)), patched(FH, 'monitoring', lambda *x: {'synthetic': True}), \
             patched(WB, 'of', lambda bb: None):
            out = CC_.bounded_wake('a' * 64, backend, decision_complete=lambda r: None)
        return json.dumps(out, sort_keys=True)
    text = line({'status': 'WAITING', 'source_dispatches': 0}, summary)
    parsed = parse_tenant_json(text)
    assert '"SOURCE_DISAPPEARED"' in text and parsed['current_entities']['fbo_postings'] == 'PARTIAL'
    assert parsed['fbo_refresh']['incidents'][0]['days'] == gone and parsed['progress'] == {'synthetic': True}
    # A verdict written in this very wake counts at once (the scan ran before it).
    clean = FBR.summary(FBR.scan(scope_backend(a, p, c, m)[0], p))
    text = line({'status': 'RECONCILED_BOUNDARY', 'source_dispatches': 0, 'refresh_verdict': 'REFRESH_PARTIAL',
                 'reason': BF.FBO_SOURCE_DISAPPEARED, 'refresh_unit_window': units[0], 'refresh_days': gone}, clean)
    assert '"SOURCE_DISAPPEARED"' in text and parse_tenant_json(text)['current_entities']['fbo_postings'] == 'PARTIAL'
    # COMPLETE / STALE and no-authority shapes.
    assert parse_tenant_json(line({'status': 'WAITING'}, clean))['current_entities'] == {'fbo_postings': 'STALE'}
    none = FBR.summary(FBR.scan(scope_backend(a, p, c, m, marker=False)[0], p))
    assert 'current_entities' not in parse_tenant_json(line({'status': 'WAITING'}, none)) and 'fbo_refresh' not in parse_tenant_json(line({'status': 'WAITING'}, none))
    merged = parse_tenant_json(line({'status': 'WAITING', 'current_entities': {'catalog': 'COMPLETE'}}, summary))['current_entities']
    assert merged == {'catalog': 'COMPLETE', 'fbo_postings': 'PARTIAL'}                             # merges with A2 entity statuses
    return 'PASS'


def check():
    for label, fn in (('authority', authority_check), ('schedule', schedule_check), ('verdict', verdict_check),
                      ('tick', tick_check), ('scope', scope_check), ('order', order_check), ('release', release_check), ('log', log_shape_check)):
        assert fn() == 'PASS', label
    return {'fbo_refresh_adapter': 'PASS'}


if __name__ == '__main__':
    print(json.dumps(check(), sort_keys=True))
