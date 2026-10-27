"""Exact Heldi R1/R2 owner recoveries: publication mechanics on the real DurableRecords.

Live boundary observation and release qualification are faked; every write, validation,
readback and the decide_tick consequence are real. No network.
"""
import copy
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from tools.tenancy import durable_plan as D, tenant_backfill as BF, full_history as F
from tools.tenancy import full_retained_pretransport_recovery as R1, full_inventory_gate_recovery as R2
from tools.tenancy import full_fbo_diagnostic_closure as LQ, full_controller as H, controller_cadence as CC
from tools.tenancy.heldi_r1r2_image_check import sample_r1, sample_r2, check
from pipelines.ozon.runtime import full_resume as FR


@pytest.fixture(scope='module', autouse=True)
def _historical_runtime():
    """Frozen production incident: replay it with the runtime it actually ran with."""
    from tools.tenancy.runtime_history_fixture import historical_runtime
    with historical_runtime():
        yield


class Crash(Exception):
    pass


class World:
    """In-memory tenant_locks/ref metadata + BACKFILL_CHECKPOINTS rows."""

    def __init__(self):
        self.meta = {}; self.rows = []; self.writes = 0; self.attempts = 0; self.crash_at = None; self.lose_at = None
        self.c = BF.target('client_001')

    # --- fault injection: crash BEFORE write k, or lose the response AFTER write k
    def _write(self, apply):
        self.attempts += 1
        if self.crash_at == self.attempts:
            raise Crash('crash before write %d' % self.attempts)
        apply(); self.writes += 1
        if self.lose_at == self.attempts:
            raise Crash('response lost after write %d' % self.writes)

    # metadata (Tables-like)
    def get_table(self, ds, name):
        return copy.deepcopy(self.meta.get((ds, name)))

    def create_marker(self, ds, name, labels, desc):
        if (ds, name) in self.meta:
            return False
        self._write(lambda: self.meta.__setitem__((ds, name), (dict(labels), desc)))
        return True

    def list_tables(self, ds, with_expiry=False):
        return [(n, v[0], '2026-10-20T10:00:00+00:00', None) if with_expiry else (n, v[0], '2026-10-20T10:00:00+00:00')
                for (d, n), v in self.meta.items() if d == ds]

    def append(self, ds, table, rows):
        assert (ds, table) == ('tenant_ops', 'BACKFILL_CHECKPOINTS')
        self._write(lambda: self.rows.extend(copy.deepcopy(rows)))

    def raw_append(self, row):
        self.rows.append(copy.deepcopy(row))

    # SELECT router (only the shapes used by the modules under test)
    def select(self, sql, params):
        p = {k: v[1] for k, v in params.items()}
        if 'lease_until' in sql and 'FORMAT_TIMESTAMP' in sql:
            got = {r['lease_until'] for r in self.rows if r.get('entity') == p['entity'] and r.get('plan_hash') == p['ack']
                   and r.get('lease_generation') == p['gen'] and r.get('status') == 'RUNNING' and r.get('run_id') == p['run']}
            return [{'lease_until': v} for v in sorted(got)]
        if "entity='supplies' AND run_id=@run" in sql:
            got = [r for r in self.rows if r.get('entity') == 'supplies' and r.get('run_id') == p['run']
                   and r.get('lease_generation') == 285 and r.get('plan_hash') == p['ack']]
            out = []
            for r in got:
                v = {k: r.get(k) for k in ('status', 'lease_owner', 'error_code', 'evidence_json')}
                if v not in out:
                    out.append(v)
            return out
        if 'SELECT DISTINCT evidence_json' in sql:
            vals = []
            for r in self.rows:
                if r.get('plan_hash') == p['root'] and ('record' not in p or r.get('backfill_id') == p['record']):
                    if r['evidence_json'] not in vals:
                        vals.append(r['evidence_json'])
            return [{'evidence_json': v} for v in vals]
        raise AssertionError('unexpected query ' + sql[:60])

    def seed(self, record):
        h = D.digest(record)
        self.meta[('tenant_locks', 'BFR_' + h)] = ({'root': record['root_hash'][:16], 'kind': record['kind'].lower()},
            D.encoded(dict(root_hash=record['root_hash'], record_hash=h, kind=record['kind'], sequence=record['sequence'])))
        self.rows.append(dict(backfill_id=h[:16], plan_hash=record['root_hash'], entity='orchestration', evidence_json=D.encoded(record)))


def make(module, monkeypatch):
    m = FR.manifest(); w = World()
    manifest = dict(version=D.VERSION, root_hash=m['hash'], kind='FULL_MANIFEST', sequence=0, payload=m)
    x1, x2 = R1.baseline(), R2.baseline()
    root = [manifest, x1['stop'], x2['stop']]
    for r in root:
        w.seed(r)
    for r in x1['shard_records']:
        w.seed(r)
    hashes = sorted(D.digest(r) for r in root)
    for mod, x in ((R1, x1), (R2, x2)):
        y = dict(x, protected_root_hashes=hashes)
        monkeypatch.setattr(mod, 'baseline', (lambda y=y: copy.deepcopy(y)))
    w.meta[('tenant_locks', R1.LEASE_NAME)] = R1.LEASE_VALUE
    w.raw_append(dict(backfill_id='ab739857b0ae170f', entity='supplies', run_id=R1.RUN, lease_generation=285, plan_hash=R1.ACK_HASH,
                      status='RUNNING', lease_owner=R1.RUN, error_code=None, evidence_json='{"mode": "BOUNDED_PILOT", "proof": null}',
                      lease_until='2026-10-17T03:14:40.714308Z'))
    for g, v in x2['claims'].items():
        cn, xn = CC.names(R2.ROOT, int(g))
        w.meta[('tenant_locks', cn)] = tuple(v['claim']); w.meta[('tenant_locks', xn)] = tuple(v['closure'])
    p = sample_r1(m) if module is R1 else sample_r2(m)
    now = [F.stamp(p['verified_at']) + timedelta(minutes=1)]
    tables = SimpleNamespace(get_table=w.get_table, create_marker=w.create_marker, list_tables=w.list_tables, append=w.append)
    store = D.DurableRecords(w.c, tables, lambda c, s, q: w.select(s, q), w.raw_append)
    b = SimpleNamespace(c=copy.deepcopy(w.c), tables=tables, store=store, select=w.select, clock=lambda: now[0], current_execution=None)
    b.c['orchestration'] = {'job': {'env': {'CONTROLLER_SOURCE_SHA': p['new_source'], 'HISTORICAL_SCHEDULER_STATE': 'PAUSED'}}}
    setattr(b, 'r1_production_authority' if module is R1 else 'r2_production_authority',
            dict(type=module.ACK_TYPE, stop_hash=module.STOP, source_budget=0, owner_ack_sha256=p['owner_ack_sha256']))
    setattr(b, 'r1_certificate_persisted' if module is R1 else 'r2_certificate_persisted', p['hash'])
    release = dict(image=p['new_image'], controller_implementation_hash=p['new_implementation_hash'], runtime_image=p['runtime_image'],
                   runtime_implementation_hash=p['runtime_implementation_hash'], verification={'exact_r1r2_recovery': 'PASS'})
    monkeypatch.setattr(LQ, 'qualified_release', lambda b_, m_, s: dict(release) if s == p['new_source'] else (_ for _ in ()).throw(AssertionError('foreign source')))
    replacement = dict(release, source_sha=p['new_source'])
    monkeypatch.setattr(R1, 'replacement_release', lambda b_, m_: dict(replacement))
    observed = []
    monkeypatch.setattr(module, 'fresh_boundary', lambda *a: observed.append(a) or module.boundary_summary())
    monkeypatch.setattr(module, 'observe', lambda b_, m_, **k: observed.append(k) or p)
    return SimpleNamespace(m=m, w=w, b=b, p=p, now=now, observed=observed)


def artifacts(w):
    return sorted(n for (d, n) in w.meta) , sorted(json.dumps(r, sort_keys=True, default=str) for r in w.rows)


# ---------------------------------------------------------------- packaged check
def test_packaged_image_check_passes():
    assert set(check().values()) == {'PASS'}


# ---------------------------------------------------------------- R1
def test_r1_publish_writes_exact_eight_operations_and_unblocks_only_a_new_sequence(monkeypatch):
    s = make(R1, monkeypatch)
    assert D.decide_tick(s.b.store.history(R1.SHARD), R1.SHARD, {'status': 'ELIGIBLE'}) == {'action': 'RECOVER_RECEIPT_OR_STOP', 'sequence': 68}
    before = s.w.writes
    assert R1.publish(s.b, s.p, s.m) == s.p and s.w.writes - before == 8
    assert R1.load(s.b, s.m, s.b.store.history(R1.ROOT)) == [s.p]
    sh = s.b.store.history(R1.SHARD)
    nxt = D.decide_tick(sh, R1.SHARD, {'status': 'ELIGIBLE'}, verified_pretransport=[s.p])
    assert nxt.get('sequence') == 69 and nxt['action'] not in {'STOPPED', 'RECOVER_RECEIPT_OR_STOP'}
    assert not any(r['kind'] in {'DISPATCH_RECEIPT', 'RECONCILED'} and r['sequence'] == 68 for r in sh)
    assert [r for r in sh if r['kind'] == 'DISPATCH_INTENT' and r['sequence'] == 68][0] == R1.baseline()['intent']
    assert s.w.get_table('tenant_locks', R1.LEASE_NAME) == R1.LEASE_VALUE


def test_r1_acceptance_never_covers_r2(monkeypatch):
    s = make(R1, monkeypatch); R1.publish(s.b, s.p, s.m)
    rs = s.b.store.history(R1.ROOT)
    covered = {p['stop_hash'] for p in R1.load(s.b, s.m, rs)} | {p['stop_hash'] for p in R2.load(s.b, s.m, rs)}
    assert {D.digest(r) for r in rs if r['kind'] == 'STOPPED'} - covered == {R2.STOP}


@pytest.mark.parametrize('k', range(1, 9))
@pytest.mark.parametrize('fault', ['crash_at', 'lose_at'])
def test_r1_interruption_at_every_write_then_idempotent_resume(monkeypatch, k, fault):
    s = make(R1, monkeypatch); base = s.w.writes
    setattr(s.w, fault, s.w.attempts + k)
    with pytest.raises(Crash):
        R1.publish(s.b, s.p, s.m)
    setattr(s.w, fault, None)
    if fault == 'crash_at' and k == 1:
        # Nothing was written: no partial state, so after the window a NEW certificate is required.
        s.now[0] += timedelta(hours=3)
        with pytest.raises(BF.B.EvidenceError, match='fresh <=15min'):
            R1.publish(s.b, s.p, s.m)
        s.now[0] -= timedelta(hours=3)
    else:
        s.now[0] += timedelta(hours=3)  # resume long after the window: live boundary is re-observed
    assert R1.publish(s.b, s.p, s.m) == s.p
    assert s.w.writes - base == 8  # every operation exactly once in total
    once = artifacts(s.w)
    assert R1.publish(s.b, s.p, s.m) == s.p and artifacts(s.w) == once and s.w.writes - base == 8


def test_r1_first_publication_requires_fresh_15min_proof(monkeypatch):
    s = make(R1, monkeypatch); s.now[0] += timedelta(minutes=20)
    with pytest.raises(BF.B.EvidenceError, match='fresh <=15min'):
        R1.publish(s.b, s.p, s.m)
    assert s.w.writes == 0 or not any(n.startswith('BFRS') for (d, n) in s.w.meta)


@pytest.mark.parametrize('owner', ['missing', 'other_ack', 'controller_execution'])
def test_r1_requires_exact_owner_authority(monkeypatch, owner):
    s = make(R1, monkeypatch)
    if owner == 'missing':
        del s.b.r1_production_authority
    if owner == 'other_ack':
        s.b.r1_production_authority = dict(s.b.r1_production_authority, owner_ack_sha256='9' * 64)
    if owner == 'controller_execution':
        s.b.current_execution = 'projects/x/executions/tenant-backfill-controller-abcde'
    with pytest.raises(BF.B.EvidenceError, match='owner'):
        R1.publish(s.b, s.p, s.m)
    assert not any(n.startswith(('BFRS', 'LD_')) for (d, n) in s.w.meta)


@pytest.mark.parametrize('corrupt', ['auth', 'release', 'closure_row', 'failed_row', 'receipt_68', 'stop', 'lease', 'intent'])
def test_r1_corrupted_or_conflicting_evidence_fails_closed(monkeypatch, corrupt):
    s = make(R1, monkeypatch); ops = R1.write_manifest(s.p, s.m)
    if corrupt == 'auth':
        s.w.meta[('ref', ops[0]['name'])] = ({'kind': 'exact_r1_owner', 'root': R1.ROOT[:16]}, '{}')
    if corrupt == 'release':
        s.w.meta[('tenant_locks', R1.RELEASE_NAME)] = ({'owner': R1.RUN}, '{"terminal":"SUCCEEDED"}')
    if corrupt == 'closure_row':
        s.w.raw_append(dict(ops[4]['row'], evidence_json=D.encoded(dict(R1.closure(s.p), payload={}))))
    if corrupt == 'failed_row':
        s.w.raw_append(dict(ops[3]['row'], evidence_json='{"mode": "BOUNDED_PILOT", "pretransport_certificate": "x", "source_budget": 0}'))
    if corrupt == 'receipt_68':
        s.w.seed(dict(version=D.VERSION, root_hash=R1.SHARD, kind='DISPATCH_RECEIPT', sequence=68, payload={'fabricated': True}))
    if corrupt == 'stop':
        s.w.meta.pop(('tenant_locks', 'BFR_' + R1.STOP))
    if corrupt == 'lease':
        s.w.meta[('tenant_locks', R1.LEASE_NAME)] = ({'owner': 'bf-other', 'until': '1792206880'}, R1.LEASE_VALUE[1])
    if corrupt == 'intent':
        s.w.meta.pop(('tenant_locks', 'BFR_' + R1.INTENT))
    with pytest.raises(BF.B.EvidenceError):
        R1.publish(s.b, s.p, s.m)
    assert not any(r['kind'] == R1.KIND for r in s.b.store.history(R1.ROOT)) if corrupt != 'stop' else True


def test_r1_certificate_tampering_and_unknown_predicates_block(monkeypatch):
    s = make(R1, monkeypatch)
    for field, value in (('run_id', 'bf-other'), ('lease_generation', 286), ('receipt_sequence', 69), ('index', 464),
                         ('intent_hash', 'f' * 64), ('historical_cause', 'UNPROVEN'), ('source_budget', 1), ('new_source', R1.OLD)):
        q = R1.sealed({k: v for k, v in dict(s.p, **{field: value}).items() if k != 'hash'})
        with pytest.raises(BF.B.EvidenceError):
            R1.header(q, s.m)
    q = copy.deepcopy(s.p); q['predicates']['no_child_runtime'] = None; q = R1.sealed({k: v for k, v in q.items() if k != 'hash'})
    with pytest.raises(BF.B.EvidenceError, match='predicate'):
        R1.header(q, s.m)


def test_r1_unknown_pretransport_type_is_refused():
    with pytest.raises(BF.B.EvidenceError, match='unknown pretransport'):
        D.pretransport_module({'type': 'SOMETHING_ELSE'})


# ---------------------------------------------------------------- R2
def test_r2_publish_writes_exact_four_operations_and_is_independent(monkeypatch):
    s = make(R2, monkeypatch); before = s.w.writes
    assert R2.publish(s.b, s.p, s.m) == s.p and s.w.writes - before == 4
    rs = s.b.store.history(R2.ROOT)
    assert R2.load(s.b, s.m, rs) == [s.p] and R1.load(s.b, s.m, rs) == []
    assert {D.digest(r) for r in rs if r['kind'] == 'STOPPED'} - {s.p['stop_hash']} == {R1.STOP}


@pytest.mark.parametrize('k', range(1, 5))
@pytest.mark.parametrize('fault', ['crash_at', 'lose_at'])
def test_r2_interruption_at_every_write_then_idempotent_resume(monkeypatch, k, fault):
    s = make(R2, monkeypatch); base = s.w.writes
    setattr(s.w, fault, s.w.attempts + k)
    with pytest.raises(Crash):
        R2.publish(s.b, s.p, s.m)
    setattr(s.w, fault, None)
    if fault == 'crash_at' and k == 1:
        # Nothing was written: no partial state, so after the window a NEW certificate is required.
        s.now[0] += timedelta(hours=3)
        with pytest.raises(BF.B.EvidenceError, match='fresh <=15min'):
            R2.publish(s.b, s.p, s.m)
        s.now[0] -= timedelta(hours=3)
    else:
        s.now[0] += timedelta(hours=3)  # resume long after the window: live boundary is re-observed
    assert R2.publish(s.b, s.p, s.m) == s.p and s.w.writes - base == 4
    once = artifacts(s.w)
    assert R2.publish(s.b, s.p, s.m) == s.p and artifacts(s.w) == once


def test_r2_cause_stays_unproven_and_claims_must_be_unchanged(monkeypatch):
    s = make(R2, monkeypatch)
    q = R2.sealed({k: v for k, v in dict(s.p, historical_cause='CONFIRMED_CLOUD_RUN_RACE').items() if k != 'hash'})
    with pytest.raises(BF.B.EvidenceError):
        R2.header(q, s.m)
    cn, _ = CC.names(R2.ROOT, 1292)
    s.w.meta[('tenant_locks', cn)] = (s.w.meta[('tenant_locks', cn)][0], '{"execution":"x/tenant-backfill-controller-ff9bq"}')
    with pytest.raises(BF.B.EvidenceError, match='claim chain'):
        R2.publish(s.b, s.p, s.m)


def test_r2_requires_its_own_owner_authority_even_after_r1(monkeypatch):
    s = make(R2, monkeypatch); s.b.r1_production_authority = dict(type=R1.ACK_TYPE, stop_hash=R1.STOP, source_budget=0, owner_ack_sha256=s.p['owner_ack_sha256'])
    del s.b.r2_production_authority
    with pytest.raises(BF.B.EvidenceError, match='owner'):
        R2.publish(s.b, s.p, s.m)


def test_installed_release_8429b691_is_retained_for_historical_validation():
    """After any new controller source, f507/STOP819 validation and closed 8429b691 claims read this file."""
    from tools.tenancy import orchestration_contract as O
    source = '8429b691f3c348e515a5db42d57c8ff040a46018'
    release = json.loads((BF.REPO / 'infra/tenant/releases/backfill' / (source + '.json')).read_text())
    assert release['source_sha'] == source and release['image'].endswith('@sha256:3082a258335166edf1a64f21a889b0ad8ee29777e6eb2b2dbb9779ef61cfdc88')
    assert release['exact_f507_recovery_version'] == 1 and set(release['verification'].values()) == {'PASS'}
    O.block(BF.target('client_001'), dict(release=source, root_hash=R1.ROOT, scheduler_state='PAUSED'), BF.REPO, release)


# ---------------------------------------------------------------- review follow-ups
@pytest.mark.parametrize('module', [R1, R2])
def test_publication_requires_durably_persisted_certificate(monkeypatch, module):
    s = make(module, monkeypatch)
    setattr(s.b, 'r1_certificate_persisted' if module is R1 else 'r2_certificate_persisted', None)
    with pytest.raises(BF.B.EvidenceError, match='persisted'):
        module.publish(s.b, s.p, s.m)
    assert s.w.writes == 0


@pytest.mark.parametrize('module,n', [(R1, 8), (R2, 4)])
def test_partial_publication_older_than_24h_needs_owner_decision(monkeypatch, module, n):
    s = make(module, monkeypatch); s.w.lose_at = s.w.attempts + 2
    with pytest.raises(Crash):
        module.publish(s.b, s.p, s.m)
    s.w.lose_at = None; s.now[0] += timedelta(hours=25)
    with pytest.raises(BF.B.EvidenceError, match='24h'):
        module.publish(s.b, s.p, s.m)


@pytest.mark.parametrize('module', [R1, R2])
def test_resume_requires_the_certified_replacement_installed(monkeypatch, module):
    s = make(module, monkeypatch); s.w.lose_at = s.w.attempts + 1
    with pytest.raises(Crash):
        module.publish(s.b, s.p, s.m)
    s.w.lose_at = None
    monkeypatch.setattr(R1, 'replacement_release', lambda b_, m_: dict(source_sha='9' * 40, image=s.p['new_image'], controller_implementation_hash=s.p['new_implementation_hash']))
    with pytest.raises(BF.B.EvidenceError, match='installed replacement differs'):
        module.publish(s.b, s.p, s.m)


def test_r1_survives_lease_ttl_only_after_completion_and_qualified_expiry(monkeypatch):
    s = make(R1, monkeypatch); R1.publish(s.b, s.p, s.m)
    s.w.meta.pop(('tenant_locks', R1.LEASE_NAME))   # BigQuery TTL removed the lease table
    s.now[0] = F.stamp('2026-11-16T00:00:10+00:00')
    assert R1.load(s.b, s.m, s.b.store.history(R1.ROOT)) == [s.p]
    s.now[0] = F.stamp('2026-10-20T10:00:00+00:00')  # absent before the qualified expiry: fail closed
    with pytest.raises(BF.B.EvidenceError, match='lease absent'):
        R1.load(s.b, s.m, s.b.store.history(R1.ROOT))


def test_r1_lease_absence_before_publication_blocks(monkeypatch):
    s = make(R1, monkeypatch); s.w.meta.pop(('tenant_locks', R1.LEASE_NAME)); s.now[0] = F.stamp('2026-11-17T00:00:00+00:00')
    s.p = s.p  # certificate itself is unchanged
    with pytest.raises(BF.B.EvidenceError):
        R1.publish(s.b, s.p, s.m)
    assert not any(n.startswith(('BFRS', 'LD_')) for (d, n) in s.w.meta)


# ---------------------------------------------------------------- real live-boundary functions (no monkeypatched fresh_boundary)
class Live:
    def __init__(self, module, monkeypatch, mutate=None):
        self.s = s = make(module, monkeypatch)
        monkeypatch.undo()  # restore the real fresh_boundary/observe for this module
        x1, x2 = R1.baseline(), R2.baseline()
        self.module = module; self.x = x1 if module is R1 else x2
        hashes = sorted(D.digest(r) for r in s.b.store.history(R1.ROOT))
        for mod, x in ((R1, x1), (R2, x2)):
            y = dict(x, protected_root_hashes=hashes)
            monkeypatch.setattr(mod, 'baseline', (lambda y=y: copy.deepcopy(y)))
        self.calls = []
        monkeypatch.setattr(R1, 'assert_frozen', lambda b, after: self.calls.append(('frozen', after)))
        monkeypatch.setattr(H, 'verify_authority', lambda *a: self.calls.append('authority'))
        monkeypatch.setattr(R1, 'accepted_stop_hashes', lambda b, m, rs: set())
        self.executions = {x1['actor']['name']: copy.deepcopy(x1['actor']), x2['actor']['name']: copy.deepcopy(x2['actor']),
                           x2['peer']['name']: copy.deepcopy(x2['peer'])}
        self.counts = {'journal': 0, 'checkpoints': 0, 'foreign': 0}
        self.created = {}   # table name -> creationTime ms (default: before every actor)
        self.expiry = {}    # table name -> expirationTime (default: none)
        # archived post-actor locks that are not durable records of this test store (claims, lease, fences)
        self.locks = [(z['name'], z['labels'], z['created_ms']) for z in self.x['locks_after_actor'] if not z['name'].startswith('BFR_')]
        b = s.b
        b.request = lambda method, url, body=None: copy.deepcopy(self.executions[url.split('/v2/', 1)[1]])
        b.journal = 'mpa-t-client-001.ozon_raw.OZON_INGESTION_RUNS'
        def select(sql, params):
            if 'COUNT(*)' in sql:
                key = 'journal' if 'OZON_INGESTION_RUNS' in sql else ('foreign' if 'NOT (IFNULL(entity' in sql else 'checkpoints')
                return [{'n': self.counts[key]}]
            return s.w.select(sql, params)
        b.select = select
        from datetime import datetime, timezone
        def listing(ds, with_expiry=False):
            if ds != 'tenant_locks':
                return []
            rows = [(n, v[0], self.created.get(n, 1792100000000)) for (d, n), v in s.w.meta.items() if d == 'tenant_locks']
            rows += [z for z in self.locks if ('tenant_locks', z[0]) not in s.w.meta]
            ts = lambda c: datetime.fromtimestamp(c / 1000, timezone.utc)
            return [(n, l, ts(c), self.expiry.get(n)) if with_expiry else (n, l, ts(c)) for n, l, c in rows]
        b.tables.list_tables = listing
        for g in range(1, 1324):  # the real chain is complete; archived values cover 1291..1293
            cn, xn = CC.names(R1.ROOT, g)
            for n in (cn, xn):
                s.w.meta.setdefault(('tenant_locks', n), ({'kind': 'controller_wake', 'root': R1.ROOT[:16]},
                                                          json.dumps({'execution': 'x/tenant-backfill-controller-g%d' % g})))
        if mutate:
            mutate(self)

    def run(self):
        s = self.s
        if self.module is R1:
            return R1.fresh_boundary(s.b, s.m, s.b.store.history(R1.ROOT), s.b.store.history(R1.SHARD))
        return R2.fresh_boundary(s.b, s.m, s.b.store.history(R2.ROOT))


@pytest.mark.parametrize('module', [R1, R2])
def test_live_boundary_clean_state_passes_and_freezes_from_actor_start(monkeypatch, module):
    L = Live(module, monkeypatch)
    assert L.run() == module.boundary_summary()
    assert ('frozen', L.x['actor']['createTime']) in L.calls and 'authority' in L.calls


R1_FAULTS = {
    'actor_changed': lambda L: L.executions[L.x['actor']['name']].__setitem__('failedCount', 0),
    'actor_deleted': lambda L: L.executions.pop(L.x['actor']['name']),
    'journal_row': lambda L: L.counts.__setitem__('journal', 1),
    'checkpoint_row_null_run': lambda L: L.counts.__setitem__('checkpoints', 1),
    'foreign_row_of_run': lambda L: L.counts.__setitem__('foreign', 1),
    'later_lease': lambda L: L.locks.append(('L_50c5d90dbf43ac9c_0286', {'owner': 'bf-x'}, 1792400000000)),
    'new_fence': lambda L: L.locks.append(('BFQ_' + R1.SHARD + '_0000000069_DISPATCH_INTENT', {}, 1792400000000)),
    'successor_intent': lambda L: L.s.w.seed(dict(version=D.VERSION, root_hash=R1.SHARD, kind='DISPATCH_INTENT', sequence=69, payload={})),
    'extra_root_record': lambda L: L.s.w.seed(dict(version=D.VERSION, root_hash=R1.ROOT, kind='STOPPED', sequence=0, payload={'reason': 'NEW'})),
    'foreign_closure': lambda L: L.s.w.seed(dict(version=D.VERSION, root_hash=R1.SHARD, kind=R1.SHARD_KIND, sequence=67, payload={'type': R1.TYPE, 'stop_hash': R1.STOP})),
}


@pytest.mark.parametrize('fault', sorted(R1_FAULTS))
def test_r1_live_boundary_false_pass_cases_fail_closed(monkeypatch, fault):
    L = Live(R1, monkeypatch, R1_FAULTS[fault])
    with pytest.raises((BF.B.EvidenceError, KeyError)):
        L.run()


R2_FAULTS = {
    'peer_changed': lambda L: L.executions[L.x['peer']['name']].__setitem__('completionTime', None),
    'claim_by_actor': lambda L: L.s.w.meta.__setitem__(('tenant_locks', CC.names(R2.ROOT, 1293)[0]),
        (L.s.w.meta[('tenant_locks', CC.names(R2.ROOT, 1293)[0])][0], json.dumps({'execution': L.x['actor']['name']}))),
    'claim_gap': lambda L: L.s.w.meta.pop(('tenant_locks', CC.names(R2.ROOT, 1291)[1])),
    'journal_row': lambda L: L.counts.__setitem__('journal', 1),
    'later_lease': lambda L: L.locks.append(('L_50c5d90dbf43ac9c_0286', {'owner': 'bf-x'}, 1792400000000)),
}


@pytest.mark.parametrize('fault', sorted(R2_FAULTS))
def test_r2_live_boundary_false_pass_cases_fail_closed(monkeypatch, fault):
    L = Live(R2, monkeypatch, R2_FAULTS[fault])
    with pytest.raises((BF.B.EvidenceError, KeyError, TypeError)):
        L.run()


# ---------------------------------------------------------------- partial publication is never an acceptance
@pytest.mark.parametrize('module,n', [(R1, 8), (R2, 4)])
def test_partial_publication_never_resolves_the_stop(monkeypatch, module, n):
    for k in range(1, n):  # every interruption point before the final commit marker
        s = make(module, monkeypatch); s.w.lose_at = s.w.attempts + k
        with pytest.raises(Crash):
            module.publish(s.b, s.p, s.m)
        rs = s.b.store.history(module.ROOT)
        assert module.load(s.b, s.m, rs) == []
        assert module.STOP in {D.digest(r) for r in rs if r['kind'] == 'STOPPED'}
        if module is R1:  # the shard stays blocked: no proof is loadable from a partial state
            assert D.decide_tick(s.b.store.history(R1.SHARD), R1.SHARD, {'status': 'ELIGIBLE'},
                                 verified_pretransport=R1.load(s.b, s.m, rs))['action'] == 'RECOVER_RECEIPT_OR_STOP'
        monkeypatch.undo()


# ---------------------------------------------------------------- actual post-rollout state (18.10 PAUSED rollout of ec2b3a7)
POST = json.loads((BF.REPO / 'tools/tenancy/fixtures/heldi_post_rollout_staged_descriptor.json').read_text())


def staged(L, name=None, labels=None, created=None, description=None):
    name = name or POST['name']
    L.s.w.meta[('tenant_locks', name)] = (dict(labels or POST['labels']), description if description is not None else POST['description'])
    L.created[name] = POST['created_ms'] if created is None else created
    return name


def full_release(L):
    p = L.s.p
    return dict(source_sha=p['new_source'], image=p['new_image'], controller_implementation_hash=p['new_implementation_hash'],
                runtime_image=p['runtime_image'], runtime_implementation_hash=p['runtime_implementation_hash'],
                verification={'exact_r1r2_recovery': 'PASS'})


def installed(L, source=None, settings=None, release=None, created=1792400000000):
    from tools.tenancy import cloud_controller as C
    p = L.s.p; source = source or p['new_source']
    L.s.b.c['orchestration']['job']['env'].setdefault('CONTROLLER_IMAGE', p['new_image'])
    settings = settings or dict(release=source, root_hash=R1.ROOT, scheduler_state='PAUSED')
    release = release or dict(source_sha=source, image=p['new_image'], controller_implementation_hash=p['new_implementation_hash'],
                              runtime_image=p['runtime_image'], runtime_implementation_hash=p['runtime_implementation_hash'],
                              verification={'exact_r1r2_recovery': 'PASS'})
    return staged(L, C.descriptor_name(settings['release'], settings['root_hash'], settings['scheduler_state']),
                  description=D.encoded(dict(settings=settings, release=release)), created=created)


def test_post_rollout_fixture_is_the_live_descriptor():
    from tools.tenancy import cloud_controller as C
    import hashlib
    desc = json.loads(POST['description'])
    assert POST['name'] == C.descriptor_name('ec2b3a7bcf73d52a20c2032db6a28d65f3833887', R1.ROOT, 'PAUSED')
    assert hashlib.sha256(POST['description'].encode()).hexdigest() == POST['description_sha256'] == R1.STAGED_DESCRIPTORS[POST['name']][1]
    assert POST['created_ms'] == R1.STAGED_DESCRIPTORS[POST['name']][0]
    assert desc['settings'] == dict(release='ec2b3a7bcf73d52a20c2032db6a28d65f3833887', root_hash=R1.ROOT, scheduler_state='PAUSED')
    assert POST['created_ms'] > F.stamp(R2.baseline()['actor']['createTime']).timestamp() * 1000   # after both actors


@pytest.mark.parametrize('module', [R1, R2])
def test_post_rollout_state_blocked_without_exact_allowance(monkeypatch, module):
    """Reproduces ec2b3a7: the staged descriptor of the rollout is an 'unexpected lock' for R1 and R2."""
    L = Live(module, monkeypatch, mutate=staged)
    monkeypatch.setattr(R1, 'STAGED_DESCRIPTORS', {})
    with pytest.raises(BF.B.EvidenceError, match='unexpected lock/fence/lease after actor: BF_SPEC_ec2b3a7'):
        L.run()


@pytest.mark.parametrize('module', [R1, R2])
def test_post_rollout_pinned_descriptor_passes(monkeypatch, module):
    L = Live(module, monkeypatch, mutate=staged)
    assert L.run() == module.boundary_summary()


@pytest.mark.parametrize('module', [R1, R2])
def test_post_rollout_pinned_and_installed_replacement_pass(monkeypatch, module):
    calls = []
    L = Live(module, monkeypatch, mutate=lambda L: (staged(L), installed(L)))
    from tools.tenancy import orchestration_contract as O
    monkeypatch.setattr(O, 'verify_artifact_source', lambda r, repo: calls.append(r['source_sha']))
    assert L.run() == module.boundary_summary() and calls == [L.s.p['new_source']]


PINNED_FAULTS = {
    'description_byte': lambda L: staged(L, description=POST['description'].replace('"PAUSED"', '"PAUSED" ', 1)),
    'description_noncanonical': lambda L: staged(L, description=json.dumps(json.loads(POST['description']), indent=1)),
    'created_plus_1ms': lambda L: staged(L, created=POST['created_ms'] + 1),
    'recreated_later': lambda L: staged(L, created=POST['created_ms'] + 3600000),
    'labels_kind': lambda L: staged(L, labels=dict(POST['labels'], kind='deployment')),
    'labels_extra': lambda L: staged(L, labels=dict(POST['labels'], x='y')),
    'enabled_same_body': lambda L: (staged(L), staged(L, name=POST['name'][:-len('PAUSED')] + 'ENABLED')),
    'enabled_settings': lambda L: staged(L, name=POST['name'][:-len('PAUSED')] + 'ENABLED',
                                         description=POST['description'].replace('"scheduler_state":"PAUSED"', '"scheduler_state":"ENABLED"')),
    'other_root': lambda L: (staged(L), staged(L, name=POST['name'].replace(R1.ROOT, '0' * 64), labels=dict(POST['labels'], root='0' * 16))),
    'unknown_table': lambda L: (staged(L), L.locks.append(('X_unknown', {}, POST['created_ms']))),
    'descriptor_unreadable': lambda L: (L.locks.append((POST['name'], POST['labels'], POST['created_ms']))),
    'expiration_set': lambda L: (staged(L), L.expiry.__setitem__(POST['name'], '1794787200000')),
    'description_null': lambda L: staged(L, description='null'),
    'release_not_object': lambda L: staged(L, description=D.encoded(dict(settings=json.loads(POST['description'])['settings'], release='x'))),
    'description_not_json': lambda L: staged(L, description='{"settings": '),
    'description_duplicate_keys': lambda L: staged(L, description='{"release":{},"release":{},"settings":{}}'),
}


@pytest.mark.parametrize('module', [R1, R2])
@pytest.mark.parametrize('fault', sorted(PINNED_FAULTS))
def test_post_rollout_pinned_descriptor_false_pass_cases_fail_closed(monkeypatch, module, fault):
    L = Live(module, monkeypatch, mutate=PINNED_FAULTS[fault])
    with pytest.raises(BF.B.EvidenceError, match='unexpected lock/fence/lease after actor'):
        L.run()


INSTALLED_FAULTS = {
    'env_enabled': lambda L: L.s.b.c['orchestration']['job']['env'].__setitem__('HISTORICAL_SCHEDULER_STATE', 'ENABLED'),
    'env_other_source': lambda L: L.s.b.c['orchestration']['job']['env'].__setitem__('CONTROLLER_SOURCE_SHA', '7' * 40),
    'settings_enabled': lambda L: L.__setattr__('override', dict(settings=dict(release=L.s.p['new_source'], root_hash=R1.ROOT, scheduler_state='ENABLED'))),
    'release_other_source': lambda L: L.__setattr__('override', dict(release=dict(source_sha='7' * 40, verification={'exact_r1r2_recovery': 'PASS'}))),
    'release_unqualified': lambda L: L.__setattr__('override', dict(release=dict(full_release(L), verification={'exact_r1r2_recovery': 'FAIL'}))),
    'release_without_verification': lambda L: L.__setattr__('override', dict(release=dict(full_release(L), verification='PASS'))),
    'release_without_hashes': lambda L: L.__setattr__('override', dict(release={k: v for k, v in full_release(L).items() if k != 'runtime_implementation_hash'})),
    'original_source': lambda L: (L.s.b.c['orchestration']['job']['env'].__setitem__('CONTROLLER_SOURCE_SHA', R1.OLD),
                                  L.__setattr__('override', dict(source=R1.OLD, release=dict(full_release(L), source_sha=R1.OLD)))),
    'third_source': lambda L: L.__setattr__('override', dict(source='6' * 40)),
    'image_differs_from_installed': lambda L: L.s.b.c['orchestration']['job']['env'].__setitem__('CONTROLLER_IMAGE', 'other@sha256:' + '0' * 64),
}


@pytest.mark.parametrize('module', [R1, R2])
@pytest.mark.parametrize('fault', sorted(INSTALLED_FAULTS))
def test_post_rollout_installed_descriptor_false_pass_cases_fail_closed(monkeypatch, module, fault):
    def mutate(L):
        L.override = {}
        INSTALLED_FAULTS[fault](L)
        staged(L); installed(L, **L.override)
    L = Live(module, monkeypatch, mutate=mutate)
    from tools.tenancy import orchestration_contract as O
    monkeypatch.setattr(O, 'verify_artifact_source', lambda r, repo: None)
    with pytest.raises(BF.B.EvidenceError, match='unexpected lock/fence/lease after actor'):
        L.run()


@pytest.mark.parametrize('module', [R1, R2])
def test_post_rollout_installed_descriptor_requires_canonical_encoding(monkeypatch, module):
    def mutate(L):
        staged(L); name = installed(L)
        labels, text = L.s.w.meta[('tenant_locks', name)]
        L.s.w.meta[('tenant_locks', name)] = (labels, json.dumps(json.loads(text), indent=1))   # same content, not canonical
    L = Live(module, monkeypatch, mutate=mutate)
    from tools.tenancy import orchestration_contract as O
    monkeypatch.setattr(O, 'verify_artifact_source', lambda r, repo: None)
    with pytest.raises(BF.B.EvidenceError, match='unexpected lock/fence/lease after actor'):
        L.run()


@pytest.mark.parametrize('module', [R1, R2])
def test_post_rollout_installed_descriptor_requires_exact_artifact_source(monkeypatch, module):
    L = Live(module, monkeypatch, mutate=lambda L: (staged(L), installed(L)))
    from tools.tenancy import orchestration_contract as O
    monkeypatch.setattr(O, 'verify_artifact_source', lambda r, repo: (_ for _ in ()).throw(ValueError('implementation hash differs')))
    with pytest.raises(ValueError, match='implementation hash differs'):
        L.run()
