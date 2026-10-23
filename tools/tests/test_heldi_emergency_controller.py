"""Heldi emergency controller release: listing snapshot, repeat-STOP suppression, one STOP
acceptance set and the v3 controller-closure recovery. Offline; no cloud call."""
import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from tools.tenancy import durable_plan as D
from tools.tenancy import cloud_controller as CLOUD, controller_cadence as CC, controller_diagnostics as CD
from tools.tenancy import stop_acceptance as SA
from tools.tests.test_durable_plan import Backend, ROOT, OTHER, NOW


# ── listing snapshot ──────────────────────────────────────────────────────────
class Counting(Backend):
    def __init__(self):
        super().__init__()
        self.lists = 0
        inner = self.meta.list_tables

        def counted(ds):
            self.lists += 1
            return inner(ds)
        self.meta.list_tables = counted


def test_history_without_snapshot_lists_every_call():
    b = Counting(); b.store.commit(ROOT, 'WAITING', 0, {'n': 1}, NOW)
    b.lists = 0
    b.store.history(ROOT); b.store.history(OTHER); b.store.history(ROOT)
    assert b.lists == 3


def test_snapshot_serves_all_history_reads_with_one_listing_and_sees_own_commits():
    b = Counting(); b.store.commit(ROOT, 'WAITING', 0, {'n': 1}, NOW)
    b.lists = 0
    b.store.begin_snapshot()
    assert len(b.store.history(ROOT)) == 1 and b.store.history(OTHER) == []
    b.store.commit(ROOT, 'WAITING', 0, {'n': 2}, NOW)                   # own commit joins the snapshot
    b.store.commit(OTHER, 'WAITING', 0, {'n': 3}, NOW)
    assert len(b.store.history(ROOT)) == 2 and len(b.store.history(OTHER)) == 1
    assert b.lists == 1
    fresh = D.DurableRecords(b.c, b.meta, b.select, b.append)          # an independent reader agrees
    assert fresh.history(ROOT) == b.store.history(ROOT)


def test_snapshot_never_hides_an_existing_marker_on_idempotent_commit():
    b = Counting(); h = b.store.commit(ROOT, 'WAITING', 0, {'n': 1}, NOW)
    other = D.DurableRecords(b.c, b.meta, b.select, b.append)
    other.begin_snapshot()
    assert other.commit(ROOT, 'WAITING', 0, {'n': 1}, NOW) == h and len(other.history(ROOT)) == 1


# ── repeat STOP suppression ───────────────────────────────────────────────────
HOLDER_DONE = datetime(2026, 10, 19, 20, 18, 36, tzinfo=timezone.utc)
DIAG = {'version': 1, 'stage': 'MANIFEST_ROOT', 'category': 'EVIDENCE'}


def unclosed():
    claim = {'generation': 1344, 'hash': 'c' * 64, 'created_at': '2026-10-19T19:49:26+00:00'}
    error = CC.UnclosedTerminalHolder(claim, {'completionTime': HOLDER_DONE.isoformat()})
    try:
        with CD.gate('MANIFEST_ROOT'):
            raise error
    except CD.GateFailure as failure:
        return failure


class Tables:
    def __init__(self, created):
        self.created = created

    def list_tables(self, ds):
        return [(n, {}, at) for n, at in self.created.items()]


def stop_backend(stops):
    records = [{'version': D.VERSION, 'root_hash': ROOT, 'kind': 'STOPPED', 'sequence': 0, 'payload': p} for p, _ in stops]
    created = {'BFR_' + D.digest(r): at for r, (_, at) in zip(records, stops)}
    return SimpleNamespace(c={'datasets': {'tenant_locks': 'tenant_locks'}}, tables=Tables(created),
                           store=SimpleNamespace(history=lambda root: records)), records


def payload(execution='x'):
    return {'reason': 'CONTROLLER_GATE_OR_EVIDENCE_FAILURE', 'controller_execution': execution, 'diagnostic': dict(DIAG)}


def test_gate_keeps_the_closed_diagnostic_and_carries_the_typed_cause():
    failure = unclosed()
    assert CD.safe(failure) == DIAG and failure.unclosed_holder['generation'] == 1344
    assert str(CC.UnclosedTerminalHolder({'generation': 1, 'hash': 'a' * 64, 'created_at': 'x'}, {})) == \
        'controller cadence: terminal controller missing canonical closure'


def test_first_occurrence_is_recorded_repeats_are_not():
    failure = unclosed()
    b, _ = stop_backend([])
    assert CLOUD.known_unclosed_incident(b, ROOT, failure, payload()) is None          # first: commit a STOP
    b, records = stop_backend([(payload('first'), HOLDER_DONE + timedelta(seconds=49))])
    assert CLOUD.known_unclosed_incident(b, ROOT, failure, payload()) == D.digest(records[0])


@pytest.mark.parametrize('case', ['older_stop', 'other_diagnostic', 'untyped_failure'])
def test_other_failures_are_never_suppressed(case):
    failure = unclosed()
    if case == 'older_stop':
        b, _ = stop_backend([(payload(), HOLDER_DONE - timedelta(seconds=1))])
    elif case == 'other_diagnostic':
        b, _ = stop_backend([(dict(payload(), diagnostic=dict(DIAG, stage='QUOTA')), HOLDER_DONE + timedelta(1))])
    else:
        b, _ = stop_backend([(payload(), HOLDER_DONE + timedelta(1))])
        failure = CD.GateFailure('MANIFEST_ROOT', 'EVIDENCE')                          # any other evidence failure
    assert CLOUD.known_unclosed_incident(b, ROOT, failure, payload()) is None


def test_unreadable_evidence_records_a_new_stop():
    failure = unclosed()

    def broken(root):
        raise D.BF.TT.TableError('cloud transport HTTP 500')
    b = SimpleNamespace(c={'datasets': {'tenant_locks': 'tenant_locks'}}, tables=Tables({}), store=SimpleNamespace(history=broken))
    assert CLOUD.known_unclosed_incident(b, ROOT, failure, payload()) is None


# ── one acceptance set ────────────────────────────────────────────────────────
MODULES = ['full_leaf_recovery', 'full_cooldown_recovery', 'full_policy_bootstrap_recovery', 'full_pretransport_recovery',
           'full_retained_pretransport_recovery', 'full_fbo_zero_effect_recovery', 'full_first_receipt_recovery',
           'full_controller_closure_recovery', 'full_pre_authority_gate_recovery', 'full_fbo_diagnostic_closure',
           'current_recon_recovery', 'f507_recovery', 'full_inventory_gate_recovery']


def test_acceptance_set_is_the_union_of_every_recovery_module(monkeypatch):
    import importlib
    records = []
    for n, name in enumerate(MODULES):
        module = importlib.import_module('tools.tenancy.' + name)
        h = ('%02d' % n) * 32
        if name == 'full_controller_closure_recovery':
            monkeypatch.setattr(module, 'load', lambda b, m, r, h=h: [{'stop_hashes': [h]}])
        else:
            monkeypatch.setattr(module, 'load', lambda b, m, r, h=h: [{'stop_hash': h}])
        records.append({'kind': getattr(module, 'KIND', 'NONE_' + name)})
    b = SimpleNamespace()
    got = SA.accepted_hashes(b, {}, records)
    assert got == {('%02d' % n) * 32 for n in range(len(MODULES))}
    assert b.verified_pretransport_rejections and b.verified_cooldown_failures is not None


@pytest.mark.parametrize('consumer', ['monitor', 'closure_recovery'])
def test_consumers_use_the_shared_set(monkeypatch, consumer):
    from tools.tenancy import recent_priority as RP, full_controller_closure_recovery as CR, full_fbo_diagnostic_closure as LQ

    class Used(Exception):
        pass

    def spy(*a, **k):
        raise Used()
    monkeypatch.setattr(SA, 'approved', spy)
    monkeypatch.setattr(LQ, 'load', lambda *a: [])
    with pytest.raises(Used):
        if consumer == 'monitor':
            RP.monitoring(SimpleNamespace(), {}, [], {}, {}, NOW)
        else:
            CR.accepted_incidents(SimpleNamespace(), {}, [])


def test_fbo_diagnostic_isolated_uses_the_shared_set(monkeypatch):
    from tools.tenancy import fbo_diagnostic_owner as FDO, full_controller as H, full_fbo_diagnostic_closure as Z, recent_priority as RP
    cert = {'hash': 'h' * 64, 'root': ROOT, 'index': 269, 'shard': 's', 'plan_id': 'p', 'run_id': 'r', 'generation': 192, 'receipt_hash': 'q'}
    p = {'root': ROOT, 'closure_hash': 'h' * 64, 'leaf': 269, 'shard': 's', 'plan_id': 'p', 'failed_run': 'r', 'failed_generation': 192, 'failed_receipt_hash': 'q'}
    monkeypatch.setattr(H, 'only_manifest', lambda r: {})
    monkeypatch.setattr(H, 'verify_authority', lambda *a: None)
    monkeypatch.setattr(H, 'Backend', lambda b, m: b)
    monkeypatch.setattr(Z, 'load', lambda *a: [cert])
    monkeypatch.setattr(RP, 'load', lambda *a: {'selection': [0] * 69, 'parents': [0] * 64})
    stop = {'kind': 'STOPPED', 'payload': {}, 'root_hash': ROOT, 'sequence': 0, 'version': D.VERSION}
    b = SimpleNamespace(store=SimpleNamespace(history=lambda root: [stop]))
    monkeypatch.setattr(SA, 'accepted_hashes', lambda b, m, r, **k: {D.digest(stop)})
    monkeypatch.setattr(CC, 'ensure', lambda b: (_ for _ in ()).throw(RuntimeError('past the STOP gate')))
    with pytest.raises(RuntimeError, match='past the STOP gate'):
        FDO.isolated(b, p)
    monkeypatch.setattr(SA, 'accepted_hashes', lambda b, m, r, **k: set())
    with pytest.raises(D.BF.B.EvidenceError, match='global STOP remains'):
        FDO.isolated(b, p)


def test_acceptance_is_revalidated_once_per_record_slice(monkeypatch):
    import importlib
    calls = []
    for name in MODULES:
        module = importlib.import_module('tools.tenancy.' + name)
        if name == 'full_controller_closure_recovery':
            monkeypatch.setattr(module, 'load', lambda b, m, r, n=name: calls.append(n) or [])
        else:
            monkeypatch.setattr(module, 'load', lambda b, m, r, n=name: calls.append(n) or [{'stop_hash': 'a' * 64}])
    records = [{'kind': 'STOPPED', 'payload': {}, 'n': 1}]
    unscoped = SimpleNamespace()
    SA.approved(unscoped, {}, records, quarantines=[]); n0 = len(calls)
    SA.approved(unscoped, {}, records, quarantines=[])
    assert len(calls) == 2 * n0                                                                          # no claimed wake: no memo
    b = SimpleNamespace(stop_acceptance_scope=object())
    first = SA.approved(b, {}, records, quarantines=[])
    n = len(calls)
    assert SA.approved(b, {}, copy.deepcopy(records), quarantines=[]) == first and len(calls) == n      # same slice: memo
    SA.approved(b, {}, records + [{'kind': 'STOPPED', 'payload': {}, 'n': 2}], quarantines=[])
    assert len(calls) > n                                                                                # new record: revalidated
    fresh = SimpleNamespace(stop_acceptance_scope=object())
    SA.approved(fresh, {}, records, quarantines=[])
    assert len(calls) > n + 1                                                                            # another wake: revalidated


# ── release schema and v3 certificate shape ──────────────────────────────────
def installed_release():
    import json, pathlib
    return json.loads((pathlib.Path(__file__).resolve().parents[2] / 'infra/tenant/releases/backfill/ec2b3a7bcf73d52a20c2032db6a28d65f3833887.json').read_text())


def test_wake_budget_release_requires_its_in_image_proof():
    from tools.tenancy import orchestration_contract as O, tenant_backfill as BF
    release = installed_release()
    with_flag = dict(release, controller_wake_budget_version=1)
    c = BF.target('client_001')
    settings = dict(release=release['source_sha'], root_hash='bb3ceca51ec5add6fe6fa3af8ec3720fbb5d9b07054b84e26d6bbfd6c8824222', scheduler_state='PAUSED')
    with pytest.raises(ValueError, match='gates not PASS'):
        O.block(c, settings, BF.REPO, with_flag)                       # flag without the adapter proof
    qualified = dict(with_flag, verification=dict(release['verification'], controller_wake_budget_adapter='PASS'))
    O.block(c, settings, BF.REPO, qualified)                           # the qualified release passes the schema gate


def v3(**change):
    from tools.tenancy import full_controller_closure_recovery as CR, controller_cadence as CC
    claim = CC.sealed(dict(version=1, tenant='client_001', project='mpa-t-client-001', root=ROOT, policy_hash='p' * 64,
                           source='4012f58c9db733475c673d00b84ebc1365f6f054', image='i', execution='projects/x/executions/smm26',
                           generation=1344, created_at='2026-10-19T19:49:26+00:00'))
    at = '2026-10-20T09:00:00+00:00'
    p = dict(version=3, type=CR.SEQUENCE_TYPE, tenant='client_001', project='mpa-t-client-001', root=ROOT, manifest_hash='m' * 64,
             claim=claim, stop_hashes=['a' * 64], protected_root_hashes=['b' * 64], new_source='9' * 40,
             new_image='europe-west1-docker.pkg.dev/mpa-platform/mpa-runtime/tenant-backfill-controller@sha256:' + 'c' * 64,
             new_implementation_hash='d' * 64, runtime_image='r', owner_ack_sha256='e' * 64, guard_probe_hash='f' * 64,
             security_proof_hash='0' * 64, source_proof_hash='1' * 64,
             last_source=dict(index=465, shard='2' * 64, receipt_hash='3' * 64, recon_hash='4' * 64, sequence=72),
             predicates=dict.fromkeys(CR.SEQUENCE_PREDICATES, True), verified_at=at, source_budget=0,
             closure=CC.sealed(dict(version=1, claim_hash=claim['hash'], status='SOURCE_FREE_RECOVERED', source_dispatches=0, closed_at=at)))
    p.update(change)
    return CR.sealed({k: v for k, v in p.items() if k != 'hash'})


def test_v3_certificate_header_is_closed():
    from tools.tenancy import full_controller_closure_recovery as CR
    m = {'tenant': 'client_001', 'project': 'mpa-t-client-001', 'hash': ROOT}
    good = v3(manifest_hash=D.digest(m))
    assert CR.header(good, m) is good
    for bad in (v3(manifest_hash=D.digest(m), predicates=dict.fromkeys(CR.RECONCILED_PREDICATES, True)),
                v3(manifest_hash=D.digest(m), new_source='4012f58c9db733475c673d00b84ebc1365f6f054'),
                v3(manifest_hash=D.digest(m), source_budget=1),
                v3(manifest_hash=D.digest(m), type=CR.RECONCILED_TYPE)):
        with pytest.raises(D.BF.B.EvidenceError):
            CR.header(bad, m)


def plan_record(index, shard):
    return {'version': D.VERSION, 'root_hash': ROOT, 'kind': 'CHUNK_PLAN', 'sequence': index, 'payload': {'index': index, 'shard_root': shard}}


def test_v3_sequence_proof_requires_the_reconciled_page_in_its_own_shard():
    from tools.tenancy import full_controller_closure_recovery as CR
    shard = '2' * 64
    receipt = {'version': D.VERSION, 'root_hash': shard, 'kind': 'DISPATCH_RECEIPT', 'sequence': 72, 'payload': {'r': 1}}
    recon = {'version': D.VERSION, 'root_hash': shard, 'kind': 'RECONCILED', 'sequence': 72, 'payload': {'persisted_reconciled': True}}
    b = SimpleNamespace(clock=lambda: NOW, store=SimpleNamespace(history=lambda root, max_records=None: [receipt, recon]))
    s = dict(index=465, shard=shard, receipt_hash=D.digest(receipt), recon_hash=D.digest(recon), sequence=72)
    m = {'programs': [None] * 466}
    records = [plan_record(465, shard)]
    assert CR.validate_sequence({}, b, m, records, s) == {}
    # A later rollover plan of the same leaf does not invalidate the accepted certificate.
    assert CR.validate_sequence({}, b, m, records + [plan_record(465, '8' * 64)], s) == {}
    for bad in (dict(s, sequence=71), dict(s, recon_hash='5' * 64), dict(s, shard='6' * 64), dict(s, completion_hash='7' * 64), dict(s, index=464)):
        with pytest.raises(D.BF.B.EvidenceError):
            CR.validate_sequence({}, b, m, records, bad)
    with pytest.raises(D.BF.B.EvidenceError):
        CR.validate_sequence({}, b, m, records + [plan_record(465, shard)], s)                   # ambiguous plan
    unpersisted = dict(recon, payload={'persisted_reconciled': False})
    b2 = SimpleNamespace(clock=lambda: NOW, store=SimpleNamespace(history=lambda root, max_records=None: [receipt, unpersisted]))
    with pytest.raises(D.BF.B.EvidenceError):
        CR.validate_sequence({}, b2, m, records, dict(s, recon_hash=D.digest(unpersisted)))


# ── review fixes: replaced-release holder, observation, fresh decisions, budget ───
def replaced_release_holder(monkeypatch, tmp_path, *, terminal=True):
    from tools.tests.test_controller_cadence_release_handoff import closed_handoff
    b, old, x, objects, commits, *_ = closed_handoff(monkeypatch, tmp_path)
    objects.pop(CC.names(old['root'], 1)[1])                       # the old release's holder never closed
    if not terminal:
        x[0].pop('completionTime')
    return b, old, x, objects, commits


def test_unclosed_terminal_holder_of_a_replaced_release_is_the_typed_incident(monkeypatch, tmp_path):
    b, old, x, objects, commits = replaced_release_holder(monkeypatch, tmp_path)
    before = copy.deepcopy(objects)
    with pytest.raises(CC.UnclosedTerminalHolder) as e:
        CC.acquire(b)
    assert e.value.unclosed_holder == {'generation': 1, 'claim_hash': old['hash'], 'created_at': old['created_at'],
                                       'holder_completion': x[0]['completionTime']}
    assert objects == before and not commits
    with pytest.raises(CD.GateFailure) as g:                        # the entrypoint sees the same closed diagnostic
        with CD.gate('MANIFEST_ROOT'):
            CC.acquire(b)
    assert CD.safe(g.value) == DIAG and g.value.unclosed_holder['generation'] == 1


def test_active_replaced_release_holder_stays_untyped(monkeypatch, tmp_path):
    b, old, x, objects, commits = replaced_release_holder(monkeypatch, tmp_path, terminal=False)
    with pytest.raises(D.BF.B.EvidenceError) as e:                  # an active old-release peer is unknown: plain STOP
        CC.acquire(b)
    assert not isinstance(e.value, CC.UnclosedTerminalHolder) and not commits
    # Even if the inventory did not list it, the holder read itself must be terminal to be typed.
    x[0]['completionTime'] = None
    original = b.request
    b.request = lambda method, url, body=None: dict(executions=[e for e in original(method, url, body)['executions'] if e is not None and e['name'] != old['execution']]) if '/executions?' in url else original(method, url, body)
    with pytest.raises(D.BF.B.EvidenceError, match='historical controller lacks canonical closure') as e:
        CC.acquire(b)
    assert not isinstance(e.value, CC.UnclosedTerminalHolder) and not commits


def test_v3_observation_reads_the_unclosed_replaced_release_claim(monkeypatch, tmp_path):
    from tools.tenancy import full_controller_closure_recovery as CR, full_controller as H
    b, old, x, objects, commits = replaced_release_holder(monkeypatch, tmp_path)
    b.current_execution = None
    b.c['orchestration']['job']['env']['HISTORICAL_SCHEDULER_STATE'] = 'PAUSED'
    m = {'hash': old['root']}
    b.store = SimpleNamespace(history=lambda root, max_records=None: [])
    monkeypatch.setattr(H, 'only_manifest', lambda records: m)

    class Reached(Exception):
        pass
    monkeypatch.setattr(SA, 'accepted_hashes', lambda *a, **k: (_ for _ in ()).throw(Reached()))
    with pytest.raises(Reached):                                     # past the claim checks
        CR.observe_sequence(b, m, 1, owner_ack_sha256='e' * 64, guard_probe_hash='f' * 64, security_proof_hash='0' * 64)
    with pytest.raises(D.BF.B.EvidenceError, match='claim unavailable'):
        CR.observe_sequence(b, m, 2, owner_ack_sha256='e' * 64, guard_probe_hash='f' * 64, security_proof_hash='0' * 64)
    leader_close = CC.sealed(dict(version=1, claim_hash=old['hash'], status='STOPPED', source_dispatches=0, closed_at=old['created_at']))
    objects[CC.names(old['root'], 1)[1]] = ({'kind': 'controller_close', 'root': old['root'][:16]}, D.encoded(leader_close))
    with pytest.raises(D.BF.B.EvidenceError, match='unclosed replaced-release claim'):
        CR.observe_sequence(b, m, 1, owner_ack_sha256='e' * 64, guard_probe_hash='f' * 64, security_proof_hash='0' * 64)


def test_fresh_acceptance_never_reuses_the_gate_memo(monkeypatch):
    import importlib
    calls = []
    for name in MODULES:
        module = importlib.import_module('tools.tenancy.' + name)
        if name == 'full_controller_closure_recovery':
            monkeypatch.setattr(module, 'load', lambda b, m, r, n=name: calls.append(n) or [])
        else:
            monkeypatch.setattr(module, 'load', lambda b, m, r, n=name: calls.append(n) or [{'stop_hash': 'a' * 64}])
    records = [{'kind': 'STOPPED', 'payload': {}, 'n': 1}]
    b = SimpleNamespace(stop_acceptance_scope=object())
    SA.approved(b, {}, records, quarantines=[]); n = len(calls)
    SA.approved(b, {}, records, quarantines=[], fresh=True)
    assert len(calls) == 2 * n


def test_automatic_isolation_and_diagnostic_decide_on_fresh_shared_acceptance():
    import inspect
    from tools.tenancy import full_fbo_diagnostic_closure as LQ, fbo_diagnostic_owner as FDO
    observe = inspect.getsource(LQ.observe)
    assert 'SA.accepted_hashes(b,m,records,quarantines=load(b,m,records),fresh=True)' in observe
    assert 'PAG.load' not in observe and 'refresh_snapshot' in observe
    assert 'fresh=True' in inspect.getsource(FDO.isolated)


def test_refresh_snapshot_sees_other_writers_and_keeps_own_commits():
    b = Counting(); b.store.begin_snapshot()
    mine = b.store.commit(ROOT, 'WAITING', 0, {'n': 1}, NOW)
    other = D.DurableRecords(b.c, b.meta, b.select, b.append)
    other.commit(ROOT, 'STOPPED', 0, {'n': 2}, NOW)                   # e.g. a peer wake's STOP
    assert len(b.store.history(ROOT)) == 1
    hidden = b.meta.list_tables
    b.meta.list_tables = lambda ds: [row for row in hidden(ds) if row[0] != 'BFR_' + mine]   # eventual listing lag
    b.store.refresh_snapshot()
    assert sorted(r['kind'] for r in b.store.history(ROOT)) == ['STOPPED', 'WAITING']


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def late_budget(step):
    from tools.tenancy import wake_budget as WB
    clock = Clock(); budget = WB.Budget(clock=clock)
    clock.t = WB.PLATFORM_SECONDS - WB.STARTUP_SECONDS - WB.CLOSE_RESERVE_SECONDS - step + 1
    return budget


@pytest.mark.parametrize('action,expected', [('RECONCILE', 'WAKE_BUDGET_RECONCILE'), ('PREPARE_NEXT', 'WAKE_BUDGET_COMPLETE')])
def test_full_leaf_declines_reconcile_and_completion_before_starting(monkeypatch, action, expected):
    from tools.tenancy import full_controller as H, wake_budget as WB
    step = WB.RECONCILE_SECONDS if action == 'RECONCILE' else WB.COMPLETE_SECONDS
    monkeypatch.setattr(H.D, 'decide_tick', lambda *a, **k: {'action': action, 'sequence': 3})
    touched = []
    b = SimpleNamespace(index=None, preflight=lambda leaf: None, quota=lambda leaf, now: {}, active_runtime_execution=lambda: None,
                        store=SimpleNamespace(history=lambda *a, **k: [], commit=lambda *a: touched.append(a)),
                        reconcile=lambda payload: touched.append('reconcile'), state=lambda doc: {'complete': True},
                        clock=lambda: NOW, wake_budget=late_budget(step))
    m = {'hash': ROOT, 'programs': [{}] * 3}
    result, _ = H.leaf_wake(b, m, 2, {'plan': {'runtime_plan': {'entity': 'supplies'}}, 'shard_root': '2' * 64}, [])
    assert result['basis'] == expected and result['source_dispatches'] == 0 and not touched and WB.exhausted(result)
    assert result['wake_remaining_seconds'] < WB.CLOSE_RESERVE_SECONDS + (WB.RECONCILE_SECONDS if action == 'RECONCILE' else WB.COMPLETE_SECONDS)


def test_current_entity_loop_ends_on_a_budget_handoff(monkeypatch):
    from datetime import date
    from tools.tenancy import current_controller as CUR, recent_priority as RP, wake_budget as WB
    p = {'hash': 'p' * 64, 'first_day': '2026-10-01'}
    monkeypatch.setattr(CUR, 'policy', lambda base: p)
    monkeypatch.setattr(CUR.CC, 'ensure', lambda base: None)
    monkeypatch.setattr(RP, 'pending_receipts', lambda *a: [])
    day = NOW.astimezone(CUR.K.MSK).date()
    monkeypatch.setattr(CUR, 'cycles', lambda base, p: {day.isoformat(): {}})
    items = {e: {'plan': {'runtime_plan': {'entity': e}}, 'shard': e[0] * 64} for e in ('catalog', 'stocks', 'prices')}   # S4: real v1 entities
    monkeypatch.setattr(CUR, 'plans', lambda base, p, d: ([], items))
    monkeypatch.setattr(CUR.P, 'eligible', lambda p, certs: ['catalog', 'stocks', 'prices'])
    monkeypatch.setattr(CUR, 'Backend', lambda base, p, d: base)
    ticks = []
    monkeypatch.setattr(CUR, 'source_tick', lambda b, p, d, item: ticks.append(item) or WB.handoff('DISPATCH'))
    base = SimpleNamespace(clock=lambda: NOW, store=SimpleNamespace(history=lambda *a, **k: []), state=lambda doc: {'progress': {}})
    result = CUR.wake(base, [], {}, {}, set())
    assert result['basis'] == 'WAKE_BUDGET_DISPATCH' and len(ticks) == 1
    base.wake_budget = late_budget(WB.LEAF_SECONDS)
    ticks.clear()
    assert CUR.wake(base, [], {}, {}, set())['basis'] == 'WAKE_BUDGET_LEAF' and not ticks


def test_acceptance_memo_is_shared_by_every_backend_of_one_wake(monkeypatch):
    """The gate (full backend) and the post-closure monitor (another backend object) read the same slice once."""
    import importlib
    calls = []
    for name in MODULES:
        module = importlib.import_module('tools.tenancy.' + name)
        if name == 'full_controller_closure_recovery':
            monkeypatch.setattr(module, 'load', lambda b, m, r, n=name: calls.append(n) or [])
        else:
            monkeypatch.setattr(module, 'load', lambda b, m, r, n=name: calls.append(n) or [{'stop_hash': 'a' * 64}])
    records = [{'kind': 'STOPPED', 'payload': {}, 'n': 1}]
    scope = SA.Scope()
    gate_backend, monitor_backend = SimpleNamespace(stop_acceptance_scope=scope), SimpleNamespace(stop_acceptance_scope=scope)
    first = SA.approved(gate_backend, {}, records, quarantines=[]); n = len(calls)
    assert SA.approved(monitor_backend, {}, records, quarantines=[]) == first and len(calls) == n
    assert monitor_backend.verified_pretransport_rejections == gate_backend.verified_pretransport_rejections
    SA.approved(SimpleNamespace(stop_acceptance_scope=SA.Scope()), {}, records, quarantines=[])
    assert len(calls) == 2 * n                                                                     # another wake: revalidated


def test_performance_fence_and_class_cooldown_read_the_same_current_plans(monkeypatch):
    from tools.tenancy import current_controller as CUR
    monkeypatch.setattr(CUR, '_current_ads', lambda b: ([], ['2026-10-21T01:00:00+00:00']))
    monkeypatch.setattr(CUR, 'policy', lambda b: {'hash': 'p'})
    assert CUR.current_cooldowns(object()) == ['2026-10-21T01:00:00+00:00']
    fence = CUR.performance_fence(SimpleNamespace(clock=lambda: NOW), {'runtime_plan': {'entity': 'ads_sku_daily', 'plan_id': 'x'}})
    assert fence['status'] == 'WAITING' and fence['basis'] == 'SOURCE_COOLDOWN' and fence['eligible_at'] == '2026-10-21T01:00:00+00:00'
    assert CUR.performance_fence(SimpleNamespace(clock=lambda: NOW), {'runtime_plan': {'entity': 'catalog', 'plan_id': 'x'}}) is None
