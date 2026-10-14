"""Synthetic campaign GET/429 accounting checks; no source or cloud authority."""
from copy import deepcopy
from datetime import date
import json
from types import SimpleNamespace
from unittest.mock import patch

from tools.tenancy import tenant_backfill as T


def fixture():
    doc = T.make_plan('client_001', 'ads_campaigns', '2026-10-13', '2026-10-13',
                      'offline-campaign-accounting', '2026-10-14T04:00:00Z',
                      today=date(2026, 10, 14))
    cooldown = dict(count=1, eligible_at='2026-10-14T07:32:06+00:00', safe_cap=15)
    detail = dict(action='SOURCE_THROTTLED', submission_rejected=False,
        source_diagnostic=dict(at='2026-10-14T06:32:06+00:00',
                               method='GET', endpoint='/api/client/campaign', http_status=429),
        cooldown=cooldown, quota_evidence={},
        quota_basis='HTTP429_KNOWN_REJECTION_RESERVATION_RETAINED', logical_requests=2,
        transport_requests=3, transport_retries=0)
    state = T.B.initial(doc['runtime_plan'])
    state.update(sequence=1, requests=2, pages=1)
    state['progress']['rate_limit'] = deepcopy(cooldown)
    units = [dict(backfill_sequence=1, backfill_detail_json=json.dumps(detail))]
    return doc, state, units


def observe(doc, state, units, *, source_only=True, rows=0, keys=None):
    """Execute the real verifier with bounded synthetic readback and denied writes."""
    def denied(*args, **kwargs):
        raise AssertionError('offline accounting attempted source/network/write')

    def reader(c, sql, params, **kwargs):
        if 'SELECT DISTINCT backfill_sequence, backfill_detail_json' in sql:
            return deepcopy(units)
        if 'SELECT DISTINCT evidence_json' in sql:
            return [dict(evidence_json=json.dumps({'state': state}))]
        if 'COUNT(*) AS rows_n' in sql:
            assert params == {'day': ('DATE', doc['runtime_plan']['observation_date'])}
            assert 'RAW_OZON_ADS_CAMPAIGNS' in sql
            return [dict(rows_n=rows, keys_n=rows if keys is None else keys)]
        raise AssertionError('unexpected accounting read')

    with patch.object(T, 'select', reader), patch.object(T, 'preflight',
            lambda *a, **k: (SimpleNamespace(append=denied), [])):
        verify = T.verify_persistence if source_only else T.verify_coverage
        return verify(doc, doc['ack_hash'], backend=SimpleNamespace(request=denied))


def check():
    doc, state, units = fixture()
    result = observe(doc, state, units)
    assert result['source_sequence'] == 1
    assert result['full_history'] == result['economic_finality'] == 'UNPROVEN'
    assert result['history_boundary_published'] is False
    assert result['readback'] == [dict(table='RAW_OZON_ADS_CAMPAIGNS', rows=0,
                                     keys=0, historical_status='UNPROVEN')]
    try:
        observe(doc, state, units, source_only=False)
    except T.B.EvidenceError as error:
        assert str(error) == 'campaign snapshot traversal incomplete'
    else:
        raise AssertionError('partial campaign prefix granted completion')
    for mutate in (
            lambda s, d: s.update(requests=0),
            lambda s, d: s.update(pages=999),
            lambda s, d: s.update(sequence=2),
            lambda s, d: d['source_diagnostic'].update(method='POST'),
            lambda s, d: d['cooldown'].update(eligible_at='invalid'),
            lambda s, d: d['source_diagnostic'].pop('at'),
            lambda s, d: d.update(report={'phase': 'INTENT'})):
        invalid_state = deepcopy(state)
        invalid_detail = json.loads(units[0]['backfill_detail_json'])
        mutate(invalid_state, invalid_detail)
        invalid_units = [dict(backfill_sequence=1, backfill_detail_json=json.dumps(invalid_detail))]
        try:
            observe(doc, invalid_state, invalid_units)
        except T.B.EvidenceError:
            pass
        else:
            raise AssertionError('unknown or contradictory rejected read was accepted')
    assert state['complete'] is False and state['progress']['done'] is False
    return dict(campaign_get_429_source_accounting='PASS',
                campaign_partial_completion_denied='PASS',
                campaign_unknown_accounting_denied='PASS',
                campaign_source_and_business_writes=0)


if __name__ == '__main__':
    print(json.dumps(check(), sort_keys=True))
