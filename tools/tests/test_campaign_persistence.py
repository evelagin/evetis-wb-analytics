"""Partial campaign source accounting cannot grant snapshot/DQ completion."""
from copy import deepcopy
import json
import socket

import pytest

from tools.tenancy import campaign_persistence_image_check as C, tenant_backfill as T


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*a, **k):
        pytest.fail('campaign accounting regression attempted network')
    monkeypatch.setattr(socket, 'create_connection', denied)
    monkeypatch.setattr(socket.socket, 'connect', denied)
    monkeypatch.setattr(T.TT, '_req', denied)


def test_real_verifier_accepts_rejected_read_without_completion():
    assert C.check()['campaign_get_429_source_accounting'] == 'PASS'


@pytest.mark.parametrize('field,value', [
    ('action', 'REPORT_PENDING'), ('submission_rejected', True),
    ('submission_rejected', 0), ('quota_evidence', {'exports': 1}),
    ('quota_basis', 'UNKNOWN'), ('report', {'phase': 'INTENT'}),
])
def test_unknown_or_external_effect_is_not_exempted(field, value):
    doc, state, units = C.fixture()
    detail = json.loads(units[0]['backfill_detail_json']); detail[field] = value
    units[0]['backfill_detail_json'] = json.dumps(detail)
    with pytest.raises(T.B.EvidenceError, match='partial campaign source effects unproven'):
        C.observe(doc, state, units)


@pytest.mark.parametrize('field,value', [
    ('method', 'POST'), ('endpoint', '/api/client/statistics'), ('http_status', 500),
    ('http_status', 429.0), ('retry-after', '3600'), ('retry-after', 86401),
])
def test_only_campaign_get_http429_is_supported(field, value):
    doc, state, units = C.fixture()
    detail = json.loads(units[0]['backfill_detail_json'])
    detail['source_diagnostic'][field] = value
    units[0]['backfill_detail_json'] = json.dumps(detail)
    with pytest.raises(T.B.EvidenceError, match='partial campaign source effects unproven'):
        C.observe(doc, state, units)


@pytest.mark.parametrize('change', ['rows', 'sequence', 'report', 'counter', 'bool_counter', 'rate'])
def test_state_or_cooldown_lineage_mismatch_blocks(change):
    doc, state, units = C.fixture()
    detail = json.loads(units[0]['backfill_detail_json'])
    if change == 'rows': state['rows'] = 1
    elif change == 'sequence': state['sequence'] = 2
    elif change == 'report': state['progress']['report'] = None
    elif change == 'counter': detail['cooldown']['count'] = 2
    elif change == 'bool_counter': detail['cooldown']['count'] = True
    else: state['progress']['rate_limit']['eligible_at'] = '2026-10-15T00:00:00Z'
    units[0]['backfill_detail_json'] = json.dumps(detail)
    with pytest.raises(T.B.EvidenceError, match='partial campaign source effects unproven'):
        C.observe(doc, state, units)


@pytest.mark.parametrize('rows,keys', [(1, 1), (1, 0), (0, 1)])
def test_persisted_business_rows_or_keys_conflict_blocks(rows, keys):
    doc, state, units = C.fixture()
    with pytest.raises(T.B.EvidenceError, match='PILOT_SNAPSHOT_KEY_ACCOUNTING_FAILED'):
        C.observe(doc, state, units, rows=rows, keys=keys)


def test_gap_and_conflicting_duplicate_source_sequence_block():
    doc, state, units = C.fixture()
    bad = deepcopy(units); bad[0]['backfill_sequence'] = 2
    with pytest.raises(T.B.EvidenceError, match='sequence has a gap'):
        C.observe(doc, state, bad)
    bad = deepcopy(units) + [dict(backfill_sequence=1, backfill_detail_json='{"action":"UNKNOWN"}')]
    with pytest.raises(T.B.EvidenceError, match='sequence conflict'):
        C.observe(doc, state, bad)


def test_reordered_multi_rejection_prefix_and_lost_ack_telemetry_are_safe():
    doc, state, units = C.fixture()
    second = json.loads(units[0]['backfill_detail_json'])
    second['cooldown']['count'] = 2
    second['source_diagnostic']['at'] = '2026-10-14T08:00:00Z'
    second['cooldown']['eligible_at'] = '2026-10-14T09:00:00Z'
    units.append(dict(backfill_sequence=2, backfill_detail_json=json.dumps(second)))
    repeat = json.loads(units[0]['backfill_detail_json']); repeat['transport_requests'] = 5
    units.append(dict(backfill_sequence=1, backfill_detail_json=json.dumps(repeat)))
    state.update(sequence=2, requests=4, pages=2)
    state['progress']['rate_limit'] = deepcopy(second['cooldown'])
    assert C.observe(doc, state, list(reversed(units)))['source_sequence'] == 2


def test_completed_campaign_source_accounting_is_unchanged():
    doc, state, units = C.fixture()
    state.update(rows=3, complete=True)
    state['progress'] = {'done': True}
    units[0]['backfill_detail_json'] = json.dumps({'action': 'CAMPAIGNS_COMPLETE', 'campaigns': 3})
    assert C.observe(doc, state, units, rows=3)['readback'][0]['rows'] == 3


def test_current_reconcile_remains_partial_and_never_replays(monkeypatch):
    from types import SimpleNamespace
    from tools.tenancy import current_controller as A
    doc, state, units = C.fixture()
    persistence = C.observe(doc, state, units)
    b = object.__new__(A.Backend)
    b.state = lambda _: deepcopy(state)
    b.request = lambda method, url: {'done': True} if method == 'GET' else pytest.fail('write')
    monkeypatch.setattr(T, 'reconcile', lambda *a, **k: {'checkpoint': 'RUNNING', 'sequence': 1})
    monkeypatch.setattr(T, 'verify_persistence', lambda *a, **k: deepcopy(persistence))
    result = b.reconcile(dict(plan=doc, receipt={'operation': 'synthetic-operation'}))
    assert result['source_complete'] is False and result['checkpoint'] == 'RUNNING'
    with pytest.raises(T.B.EvidenceError, match='partial source cannot receive current completion'):
        A.certificate(SimpleNamespace(state=b.state), {}, None, {'plan': doc}, [])


@pytest.mark.parametrize('counter,value', [('requests', 0), ('pages', 999),
                                         ('orders', 1), ('supplies', 1), ('bundles', 1)])
def test_cumulative_source_accounting_contradiction_blocks(counter, value):
    doc, state, units = C.fixture(); state[counter] = value
    with pytest.raises(T.B.EvidenceError, match='partial campaign source effects unproven'):
        C.observe(doc, state, units)


@pytest.mark.parametrize('target,key,value', [
    ('source_diagnostic', 'at', None), ('source_diagnostic', 'at', 'invalid'),
    ('source_diagnostic', 'at', '2026-10-14T06:32:06'),
    ('cooldown', 'eligible_at', 'invalid'), ('cooldown', 'eligible_at', '2026-10-14T07:32:06'),
    ('cooldown', 'eligible_at', '2026-10-14T06:32:06Z'),
    ('cooldown', 'eligible_at', '2026-10-16T07:32:06Z'),
    ('cooldown', 'safe_cap', 16), ('cooldown', 'safe_cap', True),
])
def test_unknown_or_unbounded_throttle_evidence_blocks(target, key, value):
    doc, state, units = C.fixture(); detail = json.loads(units[0]['backfill_detail_json'])
    if value is None: del detail[target][key]
    else: detail[target][key] = value
    if target == 'cooldown': state['progress']['rate_limit'] = deepcopy(detail[target])
    units[0]['backfill_detail_json'] = json.dumps(detail)
    with pytest.raises(T.B.EvidenceError, match='partial campaign source effects unproven'):
        C.observe(doc, state, units)


@pytest.mark.parametrize('value', [0, -1, True, '2'])
def test_unknown_logical_request_count_blocks(value):
    doc, state, units = C.fixture(); detail = json.loads(units[0]['backfill_detail_json'])
    detail['logical_requests'] = value
    units[0]['backfill_detail_json'] = json.dumps(detail)
    with pytest.raises(T.B.EvidenceError, match='partial campaign source effects unproven'):
        C.observe(doc, state, units)
