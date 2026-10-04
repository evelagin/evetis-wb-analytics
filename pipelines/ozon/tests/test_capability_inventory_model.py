"""Broad inventory owner decision: offline-only, synthetic credentials/data."""
import copy
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import common as C
import credentials as CR
import lifecycle as LC
import identity as I

NOW = datetime(2026, 10, 4, tzinfo=timezone.utc)
DANGEROUS = (
    '/v1/cargoes/create', '/v1/cargoes/delete', '/v1/cargoes/transport/activate',
    '/v1/cargoes/transport/bind', '/v1/cargoes/transport/create', '/v2/cargoes/delete',
    '/v1/pricing-strategy/status', '/v1/notification/check',
)


def roles(*extra):
    return {'roles': [{'name': 'Admin read only', 'methods': sorted(C.SELLER_ALLOWED_PATHS | set(extra))}]}


def evaluate(response=None, **kw):
    defaults = dict(model=CR.BROAD_MODEL, authentication_ok=True, identity_valid=True,
                    runtime_integrity=CR.runtime_authorization_evidence(CR.load_policy(), C.SELLER_PROFILES),
                    profiles=C.SELLER_PROFILES)
    defaults.update(kw)
    return CR.evaluate_seller_roles(roles() if response is None else response, ('catalog',), NOW, **defaults)


@pytest.mark.parametrize('path', DANGEROUS)
def test_danger_inventory_warns_but_transport_still_denies(path, monkeypatch):
    result = evaluate(roles(path))
    assert result['status'] == 'PASS'
    assert result['mutation_methods'] == [path]
    assert result['capability_outcomes'][path] == 'FAIL'  # semantics not weakened
    discovered = next(m for m in result['capability_discovery']['methods'] if m['path'] == path)
    assert discovered['dangerous'] is True
    assert discovered['product_state'] == 'DANGEROUS_NEVER_CALL'
    assert discovered['runtime_callable_profiles'] == []
    monkeypatch.setattr(C, '_seller_headers', lambda: pytest.fail('credentials'))
    monkeypatch.setattr(C, '_seller_open', lambda *_a, **_kw: pytest.fail('network'))
    for profile in ('runtime', 'control', 'promo'):
        with pytest.raises(C.ApiPathDenied): C.seller_call(path, {}, method='POST', profile=profile)


@pytest.mark.parametrize('path', ['/v99/new', '/v1/analytics/item_turnover', '/v1/supply-order/content/update/validation'])
def test_unreviewed_is_never_safe(path):
    result = evaluate(roles(path))
    assert result['status'] == 'PASS' and path in result['unknown_methods']
    found = next(m for m in result['capability_discovery']['methods'] if m['path'] == path)
    assert found['lifecycle'] == 'UNRESOLVED' and found['product_state'] == 'UNREVIEWED'
    assert not found['runtime_callable_profiles']


def test_role_name_does_not_change_classification_and_inventory_provenance():
    response = roles('/v1/cargoes/create', '/v1/actions/hotsales/list', '/v1/cargoes-label/create')
    a = evaluate(response)
    response['roles'][0]['name'] = 'Super safe readonly owner'
    b = evaluate(response)
    assert a['status'] == b['status'] == 'PASS'
    assert a['capability_outcomes'] == b['capability_outcomes']
    inv = a['capability_discovery']
    assert len(inv['policy_spec_sha256']) == len(inv['policy_sha256']) == 64
    found = {m['path']: m for m in inv['methods']}
    assert found['/v1/actions/hotsales/list']['product_state'] == 'RETIRED'
    assert found['/v1/actions/hotsales/list']['retirement']
    assert found['/v1/cargoes-label/create']['product_state'] == 'ARTIFACT_CANDIDATE'
    assert found['/v1/roles']['product_state'] == 'SUPPORTED_AND_USED'
    assert found['/v1/roles']['review']
    assert found['/v3/posting/fbs/list']['product_state'] == 'SUPPORTED_NOT_USED'
    assert 'NOT_LIVE_TRAFFIC' in found['/v1/roles']['usage_basis']
    assert inv['role_methods'] == a['role_methods']


@pytest.mark.parametrize('kwargs', [
    {'authentication_ok': False}, {'authentication_ok': None}, {'identity_valid': False},
    {'identity_valid': None}, {'runtime_integrity': None}, {'runtime_integrity': 'fake'}, {'runtime_integrity': {'status': 'PASS'}},
    {'profiles': {}}, {'model': 'admin-read-only'},
])
def test_hard_blockers(kwargs):
    assert evaluate(**kwargs)['status'] == 'FAIL'


@pytest.mark.parametrize('response', [{}, {'roles': []}, {'roles': 'no'}, {'roles': [{}]},
    {'roles': [{'name': 'read', 'methods': ['/v1/roles?x=1']}]},
    {'roles': [{'name': 'read', 'methods': [None]}]},
])
def test_failed_inspection_blocks(response):
    result = evaluate(response)
    assert result['status'] == 'FAIL' and result['dimensions']['inspection_integrity'] == 'FAIL'


def test_missing_full_selected_profile_method_blocks():
    response = roles()
    response['roles'][0]['methods'].remove('/v3/posting/fbs/list')
    result = evaluate(response)
    assert result['status'] == 'FAIL'
    assert result['dimensions']['required_runtime_coverage'] == 'FAIL_MISSING_REQUIRED'


@pytest.mark.parametrize('expiry', ['2020-01-01T00:00:00Z', 'invalid', {'bad': True}, False, '', '2026-12-31'])
def test_expiry_invalid_or_expired_blocks(expiry):
    response = roles(); response['expires_at'] = expiry
    result = evaluate(response)
    assert result['status'] == 'FAIL' and result['dimensions']['expiry_integrity'] == 'FAIL'


def test_runtime_profile_policy_corruption_blocks_even_claimed_pass():
    profiles = dict(C.SELLER_PROFILES)
    profiles['runtime'] = C.SELLER_PROFILES['runtime'] | {('POST', '/v1/cargoes/create')}
    proof = CR.runtime_authorization_evidence(CR.load_policy(), profiles)
    assert proof['status'] == 'FAIL'
    proof['status'] = proof['binding_gate_integrity'] = 'PASS'
    assert evaluate(roles('/v1/cargoes/create'), profiles=profiles, runtime_integrity=proof)['status'] == 'FAIL'


def test_malformed_policy_is_hard_failure():
    policy = copy.deepcopy(CR.load_policy()); policy['approved_mutation_methods'] = ['/v1/cargoes/create']
    with pytest.raises(ValueError): evaluate(policy=policy)


def test_default_strict_keeps_legacy_behavior():
    assert CR.evaluate_seller_roles(roles('/v1/cargoes/create'), ('catalog',), NOW)['status'] == 'FAIL'
    assert CR.evaluate_seller_roles(roles('/v99/new'), ('catalog',), NOW)['status'] == 'FAIL'


def seller_fixture(monkeypatch, http_roles=200, http_info=200, company=None):
    monkeypatch.setenv('SELLER_INVENTORY_MODEL', CR.BROAD_MODEL)
    monkeypatch.setenv('SELLER_IDENTITY_VERSION', I.SELLER_V2)
    monkeypatch.setattr(C, 'PROJECT', 'mpa-t-synthetic-tenant')
    monkeypatch.setattr(C, 'seller_client_id', lambda: 'synthetic-client')
    company = {'inn': 'synthetic-inn', 'ogrn': ''} if company is None else company
    def post(path, payload):
        if path == '/v1/roles': return http_roles, roles('/v1/cargoes/create', '/v99/new')
        assert path == '/v1/seller/info'
        return http_info, {'company': company}
    monkeypatch.setattr(C, 'seller_post', post)
    return SimpleNamespace(entities=('catalog',), now=NOW, run_id='synthetic-observation')


def test_v2_empty_ogrn_and_discovery_persist_in_existing_evidence(monkeypatch):
    ctx = seller_fixture(monkeypatch)
    verdict, row, fp = LC.observe_seller(ctx)
    assert verdict['status'] == 'PASS' and row['status'] == 'OBSERVED'
    assert I.fingerprint_version(fp) == I.SELLER_V2
    evidence = json.loads(row['evidence_json'])
    assert evidence['owner_confirmation'] is False
    assert len(evidence['credential']['capability_discovery']['methods']) == 17
    assert evidence['credential']['dimensions']['authentication'] == 'PASS'


@pytest.mark.parametrize('roles_http,info_http,company', [(403, 200, None), (200, 401, None), (200, 200, {})])
def test_live_structural_failure_not_manufactured_as_pass(monkeypatch, roles_http, info_http, company):
    ctx = seller_fixture(monkeypatch, roles_http, info_http, company)
    verdict, _row, _fp = LC.observe_seller(ctx)
    assert verdict['status'] == 'FAIL'


def test_legacy_cannot_opt_into_broad_before_network(monkeypatch):
    ctx = seller_fixture(monkeypatch)
    monkeypatch.setattr(C, 'PROJECT', C.LEGACY_INGESTION_PROJECT)
    monkeypatch.setattr(C, 'seller_post', lambda *_a: pytest.fail('network'))
    with pytest.raises(C.ConfigError): LC.observe_seller(ctx)


def test_broad_fail_does_not_call_performance(monkeypatch):
    ctx = seller_fixture(monkeypatch, http_roles=403)
    ctx.ads = True
    rows = []
    ctx.store = SimpleNamespace(append=lambda table, values: rows.extend((table, r) for r in values))
    monkeypatch.setattr(LC, 'hold_on', lambda _ctx: False)
    monkeypatch.setattr(LC, 'observe_performance', lambda *_a: pytest.fail('association before Seller eligible'))
    monkeypatch.setattr(LC, 'catalog_skus', lambda: pytest.fail('catalog before Seller eligible'))
    assert LC.cmd_validate(ctx) == 1
    assert len(rows) == 2 and all(r['api'] == 'seller' for _table, r in rows)


def test_capability_dimensions_survive_capability_profile(monkeypatch):
    ctx = seller_fixture(monkeypatch)
    ctx.ads = False
    rows = []
    ctx.store = SimpleNamespace(append=lambda table, values: rows.extend((table, r) for r in values))
    monkeypatch.setattr(LC, 'hold_on', lambda _ctx: False)
    assert LC.cmd_validate(ctx) == 0
    cap = next(r for table, r in rows if table == 'CAPABILITY_PROFILE')
    evidence = json.loads(cap['evidence_json'])
    assert evidence['dimensions']['runtime_authorization_integrity'] == 'PASS'
    assert evidence['dimensions']['capability_inventory_risk'] == ['WARN_DANGEROUS_REPORTED', 'WARN_UNREVIEWED']
    assert 'capability_discovery' not in evidence  # inventory not duplicated across rows
