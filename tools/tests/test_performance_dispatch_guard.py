"""Offline dispatch boundaries: retained cooldowns cannot mint new source work."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
import pytest
from tools.tenancy import current_controller as C, full_controller as H
from tools.tenancy import cloud_controller as CLOUD, tenant_backfill as BF, durable_plan as D
from tools.tests.test_current_controller import Store

NOW = datetime(2026, 10, 15, 13, 6, tzinfo=timezone.utc)
PERFORMANCE = ('ads_campaigns', 'ads_expense_daily', 'ads_sku_daily')
SELLER = ('catalog', 'stocks', 'prices', 'finance_accrual')
ROOT = 'a' * 64
SHARD = 'b' * 64


def document(entity, generation='guard'):
    return BF.make_plan('client_001', entity, '2026-10-14', '2026-10-14',
                        generation, '2026-10-15T00:00:00Z', NOW.date())


def setup(monkeypatch, producer='ads_campaigns', target='ads_sku_daily',
          scope='current', quota='ELIGIBLE', eligible=None, old_day=False):
    origin = document(producer, 'retained-cooldown')
    doc = origin if producer == target else document(target, 'next-target')
    state = BF.B.initial(origin['runtime_plan'])
    state['sequence'] = 1
    state['progress']['rate_limit'] = dict(count=1, safe_cap=15,
        eligible_at=(eligible or NOW + timedelta(hours=1)).isoformat())
    states = {origin['runtime_plan']['plan_id']: state}
    store = Store()
    calls = []
    policy = {'hash': 'c' * 64}
    source_day = date(2026, 10, 14) if old_day else NOW.date()
    item = {'plan': origin, 'shard': SHARD}
    monkeypatch.setattr(C, 'policy', lambda b: policy)
    monkeypatch.setattr(C, 'cycles', lambda *a: {source_day.isoformat(): {}} if scope == 'current' else {})
    monkeypatch.setattr(C, 'plans', lambda *a: ([], {producer: item}))
    monkeypatch.setattr(H, 'reconstruct_plans', lambda *a: {0: item} if scope == 'historical' else {})
    q = {'status': quota, 'allowance': 15 if quota == 'ELIGIBLE' else 0,
         'eligible_at': (NOW + timedelta(days=1)).isoformat()}
    monkeypatch.setattr(CLOUD.Backend, 'quota', lambda *a: deepcopy(q))
    rows = [dict(plan_id=origin['runtime_plan']['plan_id'], backfill_sequence=1,
                 evidence_json=D.encoded({'plan': origin['runtime_plan'], 'state': state}))]
    def select(sql, params):
        if 'COUNT(*) AS n' in sql: return [{'n': 0}]
        assert "entity IN ('ads_campaigns','ads_expense_daily','ads_sku_daily')" in sql
        assert 'QUALIFY backfill_sequence=MAX(backfill_sequence)' in sql
        assert params['plans'][1] == D.encoded([origin['runtime_plan']['plan_id']])
        return deepcopy(rows)
    b = H.Backend(SimpleNamespace(c=BF.target('client_001'), clock=lambda: NOW,
        store=store, select=select), {'hash': ROOT})
    b.current_root = ROOT
    b.current_execution = 'synthetic-controller'
    b.state = lambda d: deepcopy(states.get(d['runtime_plan']['plan_id'], BF.B.initial(d['runtime_plan'])))
    b.preflight = lambda *a: None
    b.active_runtime_execution = lambda: False
    def start(d, before, after):
        calls.append('prepare_lease_and_Run')
        preparation = dict(run_id='bf-synthetic-new', lease_generation=237,
                           ack_hash=d['ack_hash'])
        before(preparation)
        after(dict(preparation, operation='synthetic-operation'))
    b.start = start
    return SimpleNamespace(b=b, doc=doc, item={'plan': doc, 'shard': SHARD},
        policy=policy, state=state, rows=rows, store=store, calls=calls, origin=origin)


@pytest.mark.parametrize('producer', PERFORMANCE)
@pytest.mark.parametrize('target', PERFORMANCE + SELLER)
@pytest.mark.parametrize('quota', ['ELIGIBLE', 'WAITING'])
def test_current_cooldown_precedes_dispatch_and_rolling_export_exemption(monkeypatch, producer, target, quota):
    p = setup(monkeypatch, producer, target, quota=quota)
    before = deepcopy(p.state)
    out = C.source_tick(p.b, p.policy, NOW.date(), p.item)
    if target in PERFORMANCE:
        assert out['status'] == 'WAITING' and out['source_dispatches'] == 0
        assert not p.calls and not p.store.calls  # No intent, lease, receipt or Run.
    else:
        assert out['status'] == 'DISPATCHED' and p.calls == ['prepare_lease_and_Run']
        assert [r['kind'] for r in p.store.calls] == ['DISPATCH_INTENT', 'DISPATCH_RECEIPT']
    assert p.state == before
    assert not any(r['kind'] in {'CURRENT_CERT', 'COMPLETE', 'FULL_COMPLETE'} for r in p.store.calls)


@pytest.mark.parametrize('producer', PERFORMANCE)
@pytest.mark.parametrize('target', PERFORMANCE + SELLER)
def test_historical_cooldown_blocks_performance_but_preserves_seller(monkeypatch, producer, target):
    p = setup(monkeypatch, producer, target, scope='historical')
    out = C.source_tick(p.b, p.policy, NOW.date(), p.item)
    assert out['source_dispatches'] == int(target in SELLER)
    assert bool(p.calls) == (target in SELLER)
    if target in PERFORMANCE: assert not p.store.calls


@pytest.mark.parametrize('producer', PERFORMANCE)
def test_prior_current_day_cooldown_still_fences_new_day(monkeypatch, producer):
    p = setup(monkeypatch, producer, old_day=True)
    assert C.source_tick(p.b, p.policy, NOW.date(), p.item)['status'] == 'WAITING'
    assert not p.calls and not p.store.calls


@pytest.mark.parametrize('scope', ['current', 'historical'])
@pytest.mark.parametrize('producer', PERFORMANCE)
def test_exact_cooldown_expiry_allows_one_normal_next_dispatch(monkeypatch, scope, producer):
    p = setup(monkeypatch, producer, scope=scope, eligible=NOW)
    out = C.source_tick(p.b, p.policy, NOW.date(), p.item)
    assert out['source_dispatches'] == 1 and p.calls == ['prepare_lease_and_Run']
    assert [r['kind'] for r in p.store.calls] == ['DISPATCH_INTENT', 'DISPATCH_RECEIPT']


def test_pending_receipt_reconciles_without_new_authority_or_false_complete(monkeypatch):
    p = setup(monkeypatch, target='ads_campaigns')
    for kind, payload in [('DISPATCH_INTENT', {'plan': p.doc, 'preparation': {}}),
                          ('DISPATCH_RECEIPT', {'plan': p.doc, 'receipt': {'run_id': 'original'}})]:
        p.store.commit(SHARD, kind, 1, payload, NOW)
    original = deepcopy(p.store.records)
    p.store.calls.clear()
    p.b.reconcile = lambda payload: {'source_complete': False, 'rows_observed': 0}
    for _ in range(2):
        assert C.source_tick(p.b, p.policy, NOW.date(), p.item)['status'] == 'WAITING'
    assert p.store.records[:2] == original
    assert [r['kind'] for r in p.store.calls] == ['RECONCILED']
    assert not p.calls and not any(r['kind'] == 'CURRENT_CERT' for r in p.store.records)


def test_unreceipted_intent_is_not_hidden_by_cooldown(monkeypatch):
    p = setup(monkeypatch)
    p.store.commit(SHARD, 'DISPATCH_INTENT', 1, {'plan': p.doc}, NOW)
    original = deepcopy(p.store.records)
    with pytest.raises(BF.B.EvidenceError, match='uncertain current dispatch'):
        C.source_tick(p.b, p.policy, NOW.date(), p.item)
    assert p.store.records == original and not p.calls


def test_active_source_is_monitored_without_another_authority(monkeypatch):
    p = setup(monkeypatch)
    p.b.active_runtime_execution = lambda: True
    assert C.source_tick(p.b, p.policy, NOW.date(), p.item)['status'] == 'MONITORING'
    assert not p.calls and not p.store.calls


@pytest.mark.parametrize('bad', ['2026-10-15T18:00:00', 'not-a-timestamp'])
def test_bad_cooldown_fails_before_new_source(monkeypatch, bad):
    p = setup(monkeypatch)
    p.state['progress']['rate_limit']['eligible_at'] = bad
    with pytest.raises((ValueError, BF.B.EvidenceError)):
        C.source_tick(p.b, p.policy, NOW.date(), p.item)
    assert not p.calls and not p.store.calls


@pytest.mark.parametrize('phase', ['INTENT', 'UNKNOWN'])
def test_ambiguous_sku_report_still_fails_closed(monkeypatch, phase):
    p = setup(monkeypatch, 'ads_sku_daily')
    p.state['progress']['report'] = {'phase': phase}
    with pytest.raises(BF.B.EvidenceError): C.source_tick(p.b, p.policy, NOW.date(), p.item)
    assert not p.calls and not p.store.calls


def test_same_sku_poll_only_remains_available_after_expiry(monkeypatch):
    p = setup(monkeypatch, 'ads_sku_daily', quota='POLL_ONLY', eligible=NOW)
    p.state['progress']['report'] = {'phase': 'POLL'}
    assert C.source_tick(p.b, p.policy, NOW.date(), p.item)['source_dispatches'] == 1
    assert p.calls == ['prepare_lease_and_Run']


def test_existing_sku_poll_blocks_a_different_performance_scope(monkeypatch):
    p = setup(monkeypatch, 'ads_sku_daily', 'ads_campaigns', eligible=NOW)
    p.state['progress']['report'] = {'phase': 'POLL'}
    assert C.source_tick(p.b, p.policy, NOW.date(), p.item)['status'] == 'WAITING'
    assert not p.calls and not p.store.calls


@pytest.mark.parametrize('change', ['foreign_plan', 'wrong_sequence', 'conflicting_state'])
def test_conflicting_historical_source_proof_cannot_grant_dispatch(monkeypatch, change):
    p = setup(monkeypatch, scope='historical')
    if change == 'wrong_sequence': p.rows[0]['backfill_sequence'] = 2
    elif change == 'foreign_plan':
        proof = json_value(p.rows[0]['evidence_json']);proof['plan']['plan_id'] = 'd' * 64
        p.rows[0]['evidence_json'] = D.encoded(proof)
    else:
        other = deepcopy(p.rows[0]);proof = json_value(other['evidence_json'])
        proof['state']['progress']['rate_limit']['eligible_at'] = (NOW + timedelta(hours=2)).isoformat()
        other['evidence_json'] = D.encoded(proof);p.rows.append(other)
    with pytest.raises((ValueError, BF.B.EvidenceError)):
        C.source_tick(p.b, p.policy, NOW.date(), p.item)
    assert not p.calls and not p.store.calls


def json_value(value):
    import json
    return json.loads(value)


def test_maximum_cooldown_across_current_days_wins(monkeypatch):
    p = setup(monkeypatch)
    other = document('ads_expense_daily', 'another-current-day')
    later = BF.B.initial(other['runtime_plan'])
    later['progress']['rate_limit'] = dict(count=1, safe_cap=15, eligible_at=(NOW + timedelta(hours=2)).isoformat())
    monkeypatch.setattr(C, 'cycles', lambda *a: {'2026-10-14': {}, '2026-10-15': {}})
    monkeypatch.setattr(C, 'plans', lambda b, q, day: ([], {'ads_campaigns': {'plan': p.origin}}) if day.day == 14 else ([], {'ads_expense_daily': {'plan': other}}))
    p.b.state = lambda d: deepcopy(p.state if d == p.origin else later)
    assert p.b.quota({'plans': [p.doc]}, NOW)['eligible_at'] == later['progress']['rate_limit']['eligible_at']
    assert not p.calls and not p.store.calls


def test_multiple_current_async_scopes_still_fail_closed(monkeypatch):
    p = setup(monkeypatch, 'ads_sku_daily', eligible=NOW)
    p.state['progress']['report'] = {'phase': 'POLL'}
    other = document('ads_sku_daily', 'another-sku-scope')
    monkeypatch.setattr(C, 'cycles', lambda *a: {'2026-10-14': {}, '2026-10-15': {}})
    monkeypatch.setattr(C, 'plans', lambda b, q, day: ([], {'ads_sku_daily': {'plan': p.origin if day.day == 14 else other}}))
    p.b.state = lambda d: deepcopy(p.state)
    with pytest.raises(BF.B.EvidenceError, match='multiple current async'):
        C.source_tick(p.b, p.policy, NOW.date(), p.item)
    assert not p.calls and not p.store.calls


def test_network_free_packaged_performance_dispatch_guard():
    from tools.tenancy.performance_dispatch_image_check import check
    assert set(check().values()) == {'PASS'}


def test_replacement_can_read_exact_retained_controller_for_existing_stop819_proof():
    from tools.tenancy import full_fbo_diagnostic_closure as LQ
    # A future controller must retain the original certificate's exact context;
    # this lookup does not create or transfer a recovery certificate.
    source = '9ef7ad3c05cf2c1f1ed043cf4d0e5cf8a36f19b1'
    b = SimpleNamespace(c={'orchestration': {'job': {'env': {'CONTROLLER_SOURCE_SHA': 'e' * 40}}}})
    retained = LQ.qualified_release(b, {'hash': ROOT}, source)
    assert retained['source_sha'] == source
    assert retained['image'].endswith('@sha256:4b8635d1dbc7835d62964047755d3891bdf02c56e79f7ce4ae6fcaac36122526')
    assert retained['controller_implementation_hash'] == 'd8b8447d383ce094a7917fa8dd17a4ab73660cd79a71e553d499e33083dc0844'
    assert retained['runtime_implementation_hash'] == '0babe49f45b8947aecb0bb41fd7f9bfa5c6fb82ab11dee97dca377a58470ed06'
    assert retained['verification']['current_data_adapter'] == 'PASS'


@pytest.mark.parametrize('target', PERFORMANCE + SELLER)
@pytest.mark.parametrize('expired', [False, True])
def test_superseded_historical_snapshot_retains_cooldown_until_exact_expiry(monkeypatch, target, expired):
    from tools.tenancy.performance_dispatch_image_check import rollover_fixture
    b, selected, at, old_id, inventories = rollover_fixture(target, expired=expired)
    original = deepcopy(b.store.records)
    monkeypatch.setattr(CLOUD.Backend, 'quota', lambda *a: dict(status='ELIGIBLE', allowance=15))
    monkeypatch.setattr(C, 'policy', lambda *a: None)
    quota = b.quota({'plans': [selected]}, at)
    assert old_id in inventories[0]
    blocked = target in PERFORMANCE and not expired
    assert quota['status'] == ('WAITING' if blocked else 'ELIGIBLE')
    assert D.decide_tick([], SHARD, quota)['action'] == ('WAITING' if blocked else 'PREPARE_NEXT')
    assert b.store.records == original


def test_unproven_historical_rollover_cannot_grant_dispatch(monkeypatch):
    from tools.tenancy.performance_dispatch_image_check import rollover_fixture
    b, selected, at, _, inventories = rollover_fixture('ads_campaigns')
    b.store.records = [r for r in b.store.records if r['kind'] != 'CHUNK_SUPERSEDED']
    original = deepcopy(b.store.records)
    monkeypatch.setattr(CLOUD.Backend, 'quota', lambda *a: dict(status='ELIGIBLE', allowance=15))
    monkeypatch.setattr(C, 'policy', lambda *a: None)
    with pytest.raises(BF.B.EvidenceError, match='snapshot replacement'):
        b.quota({'plans': [selected]}, at)
    assert not inventories and b.store.records == original
