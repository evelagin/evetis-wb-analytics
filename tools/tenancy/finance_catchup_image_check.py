"""Network-free in-image proof of the owner finance catch-up (Heldi, 2026-10-25).

  * authority: closed, sealed, chained owner marker; only days after the frozen FULL finance cutoff,
    closed for natural CURRENT cycles, unique and outside earlier authorities;
  * plan: exact one-day finance plan per (authority, day) with its own root/shard, never a CURRENT
    cycle or FULL leaf; the source adapter accepts only the durably elected plan;
  * tick: intent before the only dispatch, lost reply never repeated, accepted data present ends the
    day PARTIAL without a source call, COMPLETE only through the CURRENT certificate and only with
    today's Catalog CERT, DQ verdicts end the day PARTIAL (no global STOP), any other error stays
    fail-closed;
  * scope: no marker means one metadata read and nothing else; an unqualified release ignores the
    marker; the first PARTIAL or a CURRENT-owned day halts; two open catch-up authorities fail;
  * order: an open catch-up receipt is drained before new CURRENT work; new catch-up work only after
    today's CURRENT has nothing left (Catalog CERT, every independent entity CERT or PARTIAL);
  * release: finance_catchup_version requires current_independence and its adapter.
Every patched seam is restored before returning.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import date, datetime, timezone
import json
from types import SimpleNamespace
from tools.tenancy import finance_catchup as FC, current_controller as CUR, current_policy as P
from tools.tenancy import current_contract as K, durable_plan as D, tenant_backfill as BF
from pipelines.ozon.runtime import full_resume as FR

NOW = datetime(2026, 10, 25, 10, 30, tzinfo=timezone.utc)
CREATED = '2026-10-25T10:00:00+00:00'
DAYS = ['2026-10-08', '2026-10-09', '2026-10-10', '2026-10-11', '2026-10-12', '2026-10-17', '2026-10-19', '2026-10-22', '2026-10-23']
RUN = 'bf-00000000-0000-0000-0000-000000000001'
EXECUTION = 'projects/mpa-t-client-001/locations/europe-west1/jobs/tenant-backfill-controller/executions/tenant-backfill-controller-synthetic'


@contextmanager
def patched(obj, name, value):
    saved = getattr(obj, name); setattr(obj, name, value)
    try: yield
    finally: setattr(obj, name, saved)


class Store:
    def __init__(self): self.records = []; self.reads = 0
    def history(self, root, **k):
        self.reads += 1
        return [deepcopy(r) for r in self.records if r['root_hash'] == root]
    def commit(self, root, kind, seq, payload, now):
        r = dict(version=D.VERSION, root_hash=root, kind=kind, sequence=seq, payload=deepcopy(payload))
        if r not in self.records: self.records.append(r)
        return D.digest(r)


def fixture():
    c = deepcopy(BF.target('client_001')); m = FR.manifest(); env = c['orchestration']['job']['env']
    p = P.sealed(dict(version=1, type=K.TYPE, tenant=c['tenant_id'], project=c['project_id'], historical_root=m['hash'],
        owner_decision_sha256='1' * 64, created_at='2026-10-14T05:00:00+00:00', first_day='2026-10-14', entities=list(P.ORDER),
        snapshot_sla_seconds=86400, fbo_sla_seconds=86400, controller_source=env['CONTROLLER_SOURCE_SHA'],
        controller_image=c['orchestration']['job']['image'], runtime_image=c['marketplaces']['ozon']['runtime_image'],
        runtime_implementation_hash=BF.B.implementation_hash(), fbo_contract_hash=None, source_concurrency=1, finance_finality='PROVISIONAL'))
    a = FC.authority(c, m, p, serial=1, previous=None, owner_ack_sha256='2' * 64, created_at=CREATED, days=DAYS)
    return c, m, p, a


def rejects(fn):
    try: fn()
    except (ValueError, BF.B.EvidenceError): return True
    return False


def authority_check():
    c, m, p, a = fixture()
    assert FC.full_cutoff(m) == date(2026, 10, 7)
    assert FC.validate(a, c, m, p, NOW) is a
    assert FC.name(p['hash'], 1) == 'BFFINCATCHUP_' + p['hash'][:16] + '_01'
    assert FC.value(a) == ({'kind': 'finance_catchup', 'root': a['hash'][:16]}, D.encoded(a))
    def reseal(**changes):
        x = {k: v for k, v in a.items() if k != 'hash'}; x.update(changes); return FC.sealed(x)
    bad = {
        'inside FULL history': reseal(days=['2026-10-07'] + DAYS),
        'day still reachable by CURRENT': reseal(days=DAYS + ['2026-10-24']),
        'unsorted': reseal(days=list(reversed(DAYS))),
        'duplicate': reseal(days=DAYS[:1] + DAYS),
        'empty': reseal(days=[]),
        'non-ISO day': reseal(days=['2026-10-8']),
        'other entity': reseal(entity='fbo_postings'),
        'final finance': reseal(finance_finality='FINAL'),
        'parallel source': reseal(source_concurrency=2),
        'foreign tenant': reseal(tenant='client_002'),
        'other FULL root': reseal(historical_root='e' * 64),
        'other CURRENT policy': reseal(policy_hash='f' * 64),
        'future authority': reseal(created_at='2026-10-26T00:00:00+00:00'),
        'before the CURRENT policy': reseal(created_at='2026-10-13T00:00:00+00:00', days=['2026-10-08']),
        'serial 2 without predecessor': reseal(serial=2, previous=a['hash']),
        'bool version': reseal(version=True),
        'tampered without reseal': dict(a, days=DAYS[:3]),
        'extra field': dict(a, note='x'),
    }
    for label, x in bad.items():
        assert rejects(lambda: FC.validate(x, c, m, p, NOW)), label
    month = [date.fromordinal(date(2026, 10, 8).toordinal() + i).isoformat() for i in range(32)]
    late = NOW.replace(month=11, day=13)
    assert FC.validate(reseal(days=month[:31], created_at='2026-11-12T10:00:00+00:00'), c, m, p, late)
    assert rejects(lambda: FC.validate(reseal(days=month, created_at='2026-11-12T10:00:00+00:00'), c, m, p, late))   # over 31 days
    second = FC.authority(c, m, p, serial=2, previous=a, owner_ack_sha256='3' * 64, created_at=CREATED, days=['2026-10-24'])
    later = NOW.replace(day=27)
    assert rejects(lambda: FC.validate(second, c, m, p, later, previous=a, owned=set(DAYS)))     # 24.10 reachable from 25.10
    second = FC.authority(c, m, p, serial=2, previous=a, owner_ack_sha256='3' * 64, created_at='2026-10-27T09:00:00+00:00', days=['2026-10-24'])
    assert FC.validate(second, c, m, p, later, previous=a, owned=set(DAYS)) is second
    overlap = FC.authority(c, m, p, serial=2, previous=a, owner_ack_sha256='3' * 64, created_at='2026-10-27T09:00:00+00:00', days=['2026-10-23'])
    assert rejects(lambda: FC.validate(overlap, c, m, p, later, previous=a, owned=set(DAYS)))      # owned by serial 1
    assert rejects(lambda: FC.validate(second, c, m, p, later, previous=dict(a, hash='0' * 64), owned=set(DAYS)))
    return 'PASS'


def plan_check():
    c, m, p, a = fixture()
    docs = {d: FC.document(a, date.fromisoformat(d)) for d in DAYS}
    for d, doc in docs.items():
        rp = doc['runtime_plan']
        assert (rp['entity'], rp['from'], rp['to'], rp['origin'], rp['window_days']) == ('finance_accrual', d, d, CREATED, 1)
        assert rp['generation'] == 'fcu-' + a['hash'][:16] + '-' + d.replace('-', '') and doc['max_units'] == 20
        assert BF.validate_plan(doc, doc['ack_hash']) and FC.document(a, date.fromisoformat(d)) == doc
    assert len({doc['runtime_plan']['plan_id'] for doc in docs.values()}) == len(DAYS)
    roots = {FC.root(a, date.fromisoformat(d)) for d in DAYS}
    assert len(roots) == len(DAYS) and a['hash'] not in roots and m['hash'] not in roots
    current_roots = {P.cycle_root(p, date(2026, 10, 14) + (date(2026, 10, 15) - date(2026, 10, 14)) * i) for i in range(12)}
    assert not roots & current_roots
    # The adapter authorises only the durably elected plan of its own day.
    day = date(2026, 10, 8); doc = docs['2026-10-08']; item = dict(day='2026-10-08', authority_hash=a['hash'], plan=doc, shard=FC.shard(a, day, doc))
    store = Store(); store.commit(FC.root(a, day), 'CATCHUP_PLAN', 0, item, NOW)
    objects = {('ref', FC.name(p['hash'], 1)): FC.value(a)}
    actual = object.__new__(FC.Backend)
    actual.c = c; actual.manifest = m; actual.clock = lambda: NOW; actual.store = store
    actual.full_tables = SimpleNamespace(get_table=lambda ds, n: objects.get((ds, n)))
    actual.catchup_authority, actual.catchup_day, actual.catchup_item, actual.catchup_root = a, day, item, FC.root(a, day)
    with patched(CUR, 'policy', lambda b: p):
        assert actual.authorize_full_leaf(doc) is True and actual.active_source_shard(doc) == item['shard']
        assert rejects(lambda: actual.authorize_full_leaf(docs['2026-10-09']))            # another day's plan
        wider = deepcopy(doc); wider['runtime_plan']['to'] = '2026-10-09'
        assert rejects(lambda: actual.authorize_full_leaf(wider))
        assert rejects(lambda: actual.preflight({'hash': m['hash'], 'plans': [doc]}))         # never a FULL/CURRENT scope
        empty = object.__new__(FC.Backend); empty.__dict__.update(actual.__dict__); empty.store = Store()
        assert rejects(lambda: empty.authorize_full_leaf(doc))                                # not elected
        objects.clear()
        assert rejects(lambda: actual.authorize_full_leaf(doc))                               # owner marker withdrawn
    return 'PASS'


def tick_backend(a, day, *, rows=0, complete=False, start=None):
    doc = FC.document(a, day); item = dict(day=day.isoformat(), authority_hash=a['hash'], plan=doc, shard=FC.shard(a, day, doc))
    store = Store(); calls = []
    def select(sql, params):
        calls.append('accepted_readback'); assert params == {'day': ('DATE', day.isoformat())}
        return [{'raw_rows': rows, 'coverage_rows': 0}]
    def default_start(d, before, after):
        before(dict(run_id=RUN, lease_generation=7, ack_hash=doc['ack_hash'], job='synthetic', overrides={}))
        assert store.history(item['shard'])[0]['kind'] == 'DISPATCH_INTENT'
        calls.append('source_dispatch')
        after(dict(run_id=RUN, lease_generation=7, ack_hash=doc['ack_hash'], operation='op'))
    state = {'plan_id': doc['runtime_plan']['plan_id'], 'sequence': 1 if complete else 0, 'complete': complete, 'progress': {'next_day': '2026-10-09'}}
    b = SimpleNamespace(c=BF.target('client_001'), clock=lambda: NOW, catchup_root=FC.root(a, day), current_execution=EXECUTION, store=store,
        preflight=lambda leaf: calls.append('preflight'), quota=lambda *x: {'status': 'ELIGIBLE', 'allowance': 0},
        active_runtime_execution=lambda: False, state=lambda d: state, select=select, start=start or default_start,
        reconcile=lambda payload: {'source_complete': True})
    return b, item, store, calls


def tick_check():
    c, m, p, a = fixture(); day = date(2026, 10, 8)
    b, item, store, calls = tick_backend(a, day)
    got = FC.tick(b, a, day, item)
    assert got['status'] == 'DISPATCHED' and got['source_dispatches'] == 1 and got['catchup_day'] == '2026-10-08'
    assert calls == ['preflight', 'accepted_readback', 'source_dispatch']
    assert [r['kind'] for r in store.records] == ['DISPATCH_INTENT', 'DISPATCH_RECEIPT'] and {r['root_hash'] for r in store.records} == {item['shard']}
    # Lost reply: the elected intent is never repeated.
    attempts = []
    def lost(d, before, after):
        before(dict(run_id=RUN, lease_generation=7, ack_hash=d['ack_hash'], job='synthetic', overrides={}))
        attempts.append(1); raise OSError('synthetic lost reply')
    b, item, store, calls = tick_backend(a, day, start=lost)
    for _ in range(2):
        assert rejects(lambda: FC.tick(b, a, day, item))
    assert len(attempts) == 1 and [r['kind'] for r in store.records] == ['DISPATCH_INTENT']
    # Accepted data present: PARTIAL without any source call; the authority halts.
    b, item, store, calls = tick_backend(a, day, rows=5)
    got = FC.tick(b, a, day, item)
    assert got['basis'] == 'FINANCE_CATCHUP_HALTED' and got['reason'] == 'ACCEPTED_DATA_PRESENT' and 'source_dispatch' not in calls
    assert [r['kind'] for r in store.records] == ['CATCHUP_PARTIAL'] and store.records[0]['root_hash'] == FC.root(a, day)
    # Completion: the CURRENT certificate on the catch-up scope, or an explicit PARTIAL.
    def completed(cover=None, error=None, catalog=True):
        b, item, store, calls = tick_backend(a, day, complete=True)
        receipt = dict(run_id=RUN, lease_generation=7, ack_hash=item['plan']['ack_hash'], operation='op')
        for kind, payload in (('DISPATCH_INTENT', {'plan': item['plan']}), ('DISPATCH_RECEIPT', {'plan': item['plan'], 'receipt': receipt}), ('RECONCILED', {'source_complete': True})):
            store.commit(item['shard'], kind, 1, payload, NOW)
        def verify(doc, ack, backend):
            if error: raise BF.B.EvidenceError(error)
            return {'source_sequence': 1, 'readback': [{'day': day.isoformat(), 'rows': 5, 'keys': 5, 'coverage': cover}]}
        with patched(BF, 'verify_coverage', verify), patched(CUR, 'catalog_current', lambda bb, text: {'day': text} if catalog else None):
            return FC.tick(b, a, day, item), store
    got, store = completed('COMPLETE')
    assert got['status'] == 'RECONCILED_BOUNDARY' and got['catchup_complete'] is True
    cert = [r for r in store.records if r['kind'] == 'CATCHUP_CERT'][0]['payload']
    assert (cert['day'], cert['policy_hash'], cert['fact']['entity'], cert['fact']['finance_finality']) == ('2026-10-08', a['hash'], 'finance_accrual', 'PROVISIONAL')
    assert cert['fact']['source_run_id'] == RUN
    got, store = completed('COMPLETE', catalog=False)                                          # after midnight, before Catalog
    assert got['basis'] == 'FINANCE_CATCHUP_AWAITS_TODAYS_CATALOG' and not any(r['kind'].startswith('CATCHUP_') for r in store.records)
    got, store = completed('UNKNOWN')
    assert got['reason'] == 'SOURCE_RETENTION_COMPLETENESS_UNPROVEN' and not any(r['kind'] == 'CATCHUP_CERT' for r in store.records)
    for code in ('PILOT_DATA_RECONCILIATION_FAILED', 'PILOT_SKU_LINKAGE_UNPROVEN', 'PILOT_CATALOG_SKU_JOIN_AMBIGUOUS'):
        got, store = completed(error=code + ': synthetic')
        assert got['reason'] == 'DQ_RECONCILIATION_FAILED' and [r['kind'] for r in store.records][-1] == 'CATCHUP_PARTIAL'
    assert rejects(lambda: completed(error='source checkpoint disappeared'))                  # other errors stay fail-closed
    return 'PASS'


def scope_backend(a, p, c, m, *, marker=True, release=True, store=None):
    reads = []
    from tools.tenancy import cloud_controller as CLOUD
    env = c['orchestration']['job']['env']
    rel = {'finance_catchup_version': 1, 'verification': {'finance_catchup_adapter': 'PASS'}} if release else {'verification': {}}
    objects = {('tenant_locks', CLOUD.descriptor_name(env['CONTROLLER_SOURCE_SHA'], m['hash'], env['HISTORICAL_SCHEDULER_STATE'])): ({}, D.encoded({'release': rel}))}
    if marker: objects[('ref', FC.name(p['hash'], 1))] = FC.value(a)
    def get_table(ds, n): reads.append(n); return objects.get((ds, n))
    store = store or Store()
    return SimpleNamespace(c=c, manifest=m, clock=lambda: NOW, store=store, tables=SimpleNamespace(get_table=get_table)), reads, store


def elect(store, a, text):
    day = date.fromisoformat(text); doc = FC.document(a, day)
    item = dict(day=text, authority_hash=a['hash'], plan=doc, shard=FC.shard(a, day, doc))
    store.commit(FC.root(a, day), 'CATCHUP_PLAN', 0, item, NOW); return item


def scope_check():
    c, m, p, a = fixture()
    b, reads, store = scope_backend(a, p, c, m, marker=False)
    s = FC.scan(b, p, set())
    assert s['authorities'] == [] and reads == [FC.name(p['hash'], 1)] and store.reads == 0          # disabled: one read
    b, reads, store = scope_backend(a, p, c, m, release=False)
    assert FC.scan(b, p, set())['authorities'] == [] and store.reads == 0                          # unqualified release ignores it
    b, reads, store = scope_backend(a, p, c, m)
    s = FC.scan(b, p, {'2026-10-13', '2026-10-24'})
    assert s['next'][1] == date(2026, 10, 8) and s['next'][2] is None and s['halted'] is None and not s['pending']
    item = elect(store, a, '2026-10-08')
    store.commit(item['shard'], 'DISPATCH_INTENT', 1, {'plan': item['plan']}, NOW)
    store.commit(item['shard'], 'DISPATCH_RECEIPT', 1, {'plan': item['plan'], 'receipt': {'run_id': RUN, 'lease_generation': 7}}, NOW)
    s = FC.scan(b, p, set())
    assert [(x[1], x[2]) for x in s['pending']] == [(date(2026, 10, 8), item)] and s['next'][1] == date(2026, 10, 8)
    assert FC.receipts(b, p) == [{'run_id': RUN, 'lease_generation': 7}]
    store.commit(item['shard'], 'RECONCILED', 1, {}, NOW)
    cert = dict(day='2026-10-08', policy_hash=a['hash'], ack_hash=item['plan']['ack_hash'], shard=item['shard'], fact={'entity': 'finance_accrual', 'current_scope': a['hash']})
    store.commit(FC.root(a, date(2026, 10, 8)), 'CATCHUP_CERT', 0, cert, NOW)
    s = FC.scan(b, p, set())
    assert not s['pending'] and s['complete'] == ['2026-10-08'] and s['next'][1] == date(2026, 10, 9) and FC.receipts(b, p) == []
    assert FC.scan(b, p, {'2026-10-09'})['halted'] == 'DAY_OWNED_BY_CURRENT' and FC.scan(b, p, {'2026-10-09'})['next'] is None
    item9 = elect(store, a, '2026-10-09')
    store.commit(FC.root(a, date(2026, 10, 9)), 'CATCHUP_PARTIAL', 0, dict(day='2026-10-09', authority_hash=a['hash'], ack_hash=item9['plan']['ack_hash'], status='PARTIAL', reason='ACCEPTED_DATA_PRESENT'), NOW)
    s = FC.scan(b, p, set())
    assert s['halted'] == 'PARTIAL_ACCEPTED_DATA_PRESENT' and s['next'] is None and s['partial'] == ['2026-10-09']
    assert FC.summary(s) == {'authorities': 1, 'complete_days': 1, 'partial_days': 1, 'halted': 'PARTIAL_ACCEPTED_DATA_PRESENT', 'next_day': None}
    # Two open catch-up authorities or a conflicting day record fail closed.
    b2, _, store2 = scope_backend(a, p, c, m)
    for text in DAYS[:2]:
        it = elect(store2, a, text); store2.commit(it['shard'], 'DISPATCH_INTENT', 1, {'plan': it['plan']}, NOW)
    assert rejects(lambda: FC.scan(b2, p, set()))
    b3, _, store3 = scope_backend(a, p, c, m); it = elect(store3, a, DAYS[0])
    store3.commit(FC.root(a, date(2026, 10, 8)), 'CATCHUP_PLAN', 0, dict(it, shard='0' * 64), NOW)
    assert rejects(lambda: FC.scan(b3, p, set()))
    return 'PASS'


def order_check():
    """CURRENT.wake: open catch-up drained first; new catch-up work only after today's CURRENT."""
    from tools.tenancy import controller_cadence as CC, recent_priority as RP, fbo_diagnostic_owner as DIAG
    c, m, p, a = fixture(); day = NOW.astimezone(K.MSK).date()
    def item(e): return {'day': day.isoformat(), 'policy_hash': p['hash'], 'plan': {'ack_hash': e, 'runtime_plan': {'entity': e, 'from': '2026-10-24'}}, 'shard': 's-' + e}
    def run(*, certified=(), partial=(), current_pending=None, catchup=None):
        calls = []; items = {e: item(e) for e in CUR.INDEPENDENT_ENTITIES}
        records = [{'kind': 'CURRENT_CERT', 'payload': {'fact': {'entity': e}}} for e in certified]
        records += [{'kind': 'CURRENT_PARTIAL', 'payload': {'entity': e, 'policy_hash': p['hash'], 'ack_hash': e, 'state_hash': D.digest({'progress': {}}), 'status': 'PARTIAL', 'reason': 'SOURCE_RETENTION_COMPLETENESS_UNPROVEN'}} for e in partial]
        def history(shard, **k): return [{'kind': 'DISPATCH_INTENT', 'sequence': 1}] if current_pending and shard == 's-' + current_pending else []
        base = SimpleNamespace(clock=lambda: NOW, store=SimpleNamespace(history=history, commit=lambda *x: 'h'), state=lambda d: {'progress': {}})
        scope = catchup or {'authorities': ['a'], 'pending': [], 'next': (a, date(2026, 10, 8), None), 'halted': None}
        seen = []
        with patched(CUR, 'policy', lambda b: p), patched(CC, 'ensure', lambda b: None), patched(RP, 'pending_receipts', lambda *x: []), \
             patched(CUR, 'cycles', lambda b, pp: {day.isoformat(): {}}), patched(CUR, 'plans', lambda b, pp, d: (records, items)), \
             patched(CUR, 'validate_certificate', lambda *x: x[3]), patched(CUR, 'Backend', lambda *x: None), \
             patched(CUR, 'source_tick', lambda b, pp, d, it: calls.append(it['plan']['runtime_plan']['entity']) or {'status': 'WAITING', 'source_dispatches': 0}), \
             patched(DIAG, 'queued', lambda b: None), patched(FC, 'scan', lambda b, pp, fin: seen.append(fin) or scope), \
             patched(FC, 'Backend', lambda *x: None), patched(FC, 'tick', lambda b, aa, d, it: calls.append('catchup:' + d.isoformat()) or {'status': 'DISPATCHED', 'source_dispatches': 1}):
            result = CUR.wake(base, [], {}, {}, set())
        return calls, result, seen
    open_scope = {'authorities': ['a'], 'pending': [(a, date(2026, 10, 8), {'plan': {}})], 'next': None, 'halted': None}
    calls, result, seen = run(catchup=open_scope)
    assert calls == ['catchup:2026-10-08'] and result['status'] == 'DISPATCHED'                    # drained before CURRENT
    assert seen == [{'2026-10-24'}]                                                                 # CURRENT finance days passed in
    assert rejects(lambda: run(current_pending='stocks', catchup=open_scope))                        # never two authorities
    calls, result, _ = run()
    assert calls == ['catalog'] and result is None                                                  # CURRENT first: nothing else
    calls, result, _ = run(certified=['catalog'])
    assert calls == ['stocks', 'prices', 'finance_accrual'] and result is None                      # today's CURRENT still waiting
    calls, result, _ = run(certified=['catalog', 'stocks', 'finance_accrual'], partial=['prices'])
    assert calls == ['catchup:2026-10-08'] and result['source_dispatches'] == 1                     # after CURRENT: catch-up
    calls, result, _ = run(certified=['catalog', 'stocks', 'prices', 'finance_accrual'], catchup={'authorities': ['a'], 'pending': [], 'next': None, 'halted': 'PARTIAL_DQ_RECONCILIATION_FAILED'})
    assert calls == [] and result is None                                                           # halted: no source
    calls, result, _ = run(certified=['catalog', 'stocks', 'prices', 'finance_accrual'], catchup={'authorities': [], 'pending': [], 'next': None, 'halted': None})
    assert calls == [] and result is None                                                           # no authority: as before
    return 'PASS'


def release_check():
    from tools.tenancy import orchestration_contract as O
    c = BF.target('client_001'); env = c['orchestration']['job']['env']
    from tools.tenancy.validation import parse_tenant_json
    rel = parse_tenant_json((BF.REPO / 'infra/tenant/releases/backfill/73b3e3c255acd3207c6a25b9238f4b49835d3264.json').read_text())
    settings = {'release': rel['source_sha'], 'root_hash': env['BACKFILL_ROOT_HASH'], 'scheduler_state': 'ENABLED'}
    ok = dict(rel, finance_catchup_version=1, verification=dict(rel['verification'], finance_catchup_adapter='PASS'))
    assert O.block(c, settings, BF.REPO, release=ok)['job']['env']['CURRENT_DATA_MODE'] == K.TYPE
    for bad in (dict(ok, finance_catchup_version=2), dict(ok, finance_catchup_version=True),
                dict(ok, verification=rel['verification']),
                {k: v for k, v in ok.items() if k != 'current_independence_version'}):
        assert rejects(lambda: O.block(c, settings, BF.REPO, release=bad))
    assert {'finance_catchup.py', 'finance_catchup_image_check.py'} <= set(O.SOURCE_FILES)
    assert {'CATCHUP_PLAN', 'CATCHUP_CERT', 'CATCHUP_PARTIAL'} <= D.KINDS
    return 'PASS'


def check():
    for label, fn in (('authority', authority_check), ('plan', plan_check), ('tick', tick_check),
                      ('scope', scope_check), ('order', order_check), ('release', release_check)):
        assert fn() == 'PASS', label
    return {'finance_catchup_adapter': 'PASS'}


if __name__ == '__main__':
    print(json.dumps(check(), sort_keys=True))
