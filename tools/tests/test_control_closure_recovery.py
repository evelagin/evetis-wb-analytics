"""Heldi STOP eca0335a: exact typed CONTROL_CLOSURE/CLOUD_METADATA recovery after a certified CURRENT entity. Offline; no cloud call."""
from copy import deepcopy
import pytest
from tools.tenancy import control_closure_recovery as R, tenant_backfill as BF, durable_plan as D, stop_acceptance as SA
from tools.tenancy import control_closure_image_check as X


def test_certificate_observed_validated_and_published_once():
    assert X.certificate_check() == 'PASS' and X.publish_check() == 'PASS'


@pytest.mark.parametrize('fault', X.negatives(), ids=lambda fn: fn.__name__)
def test_any_authority_or_effect_after_the_certificate_blocks(fault):
    f = X.fixture(); fault(f)
    with pytest.raises(BF.B.EvidenceError): X.observe(f)
    assert not f.effects


def test_next_current_plan_after_the_certificate_blocks_as_authority():
    f = X.fixture(); [fault] = [n for n in X.negatives() if n.__name__ == 'next_plan_record']; fault(f)
    with pytest.raises(BF.B.EvidenceError, match='authority after the CURRENT certificate'): X.observe(f)


@pytest.mark.parametrize('predicate', sorted(R.PREDICATES))
@pytest.mark.parametrize('value', [None, False, 1, 'UNKNOWN'])
def test_any_unknown_predicate_blocks_all_writes(predicate, value):
    f = X.fixture(); p = X.observe(f); p['predicates'][predicate] = value; p = R.sealed({k: v for k, v in p.items() if k != 'hash'})
    with pytest.raises(BF.B.EvidenceError), X.observing(f): R.publish(f.b, p, f.m)
    assert not f.effects


@pytest.mark.parametrize('field', ['stop_hash', 'cert_hash', 'lease_generation', 'claim_generation', 'policy_hash', 'day', 'new_source'])
def test_publication_of_an_altered_certificate_writes_nothing(field):
    f = X.fixture(); p = X.observe(f)
    alt = {'stop_hash': '0' * 64, 'cert_hash': '0' * 64, 'lease_generation': 917, 'claim_generation': 2, 'policy_hash': '1' * 64, 'day': '2026-11-05', 'new_source': 'e' * 40}[field]
    q = R.sealed(dict({k: v for k, v in p.items() if k != 'hash'}, **{field: alt}))
    with pytest.raises(BF.B.EvidenceError), X.observing(f): R.publish(f.b, q, f.m)
    assert not f.effects


def test_loaded_only_with_both_exact_owner_markers():
    f = X.fixture(); p = X.observe(f)
    with X.observing(f): R.publish(f.b, p, f.m)
    with X.repo_with(f.new):
        assert R.load(f.b, f.m, f.root) == [p]
        del f.objects[R.certificate(p)]
        with pytest.raises(BF.B.EvidenceError, match='owner acceptance absent'): R.load(f.b, f.m, f.root)
        with pytest.raises(BF.B.EvidenceError): SA.accepted_hashes(f.b, f.m, f.root, fresh=True)


def test_validate_rejects_a_certificate_for_another_stop_payload():
    f = X.fixture(); p = X.observe(f)
    with X.observing(f): R.publish(f.b, p, f.m)
    f.root[1]['payload']['diagnostic'] = dict(version=1, stage='LEASE', category='EVIDENCE')
    with X.repo_with(f.new), pytest.raises(BF.B.EvidenceError): R.validate(p, f.b, f.m, f.root)


def test_release_contract_and_registrations():
    assert X.release_check() == 'PASS' and X.acceptance_check() == 'PASS'
    assert X.check() == {'control_closure_recovery_adapter': 'PASS'}


def test_registered_installed_release_is_the_deployed_e82ba56():
    import hashlib
    raw = (BF.REPO / 'infra/tenant/releases/backfill' / f'{X.OLD}.json').read_bytes()
    # Byte-exact CANDIDATE_RELEASE.json of the C release (C-R OWNER ACK pins sha256 a0978a27…).
    assert hashlib.sha256(raw).hexdigest() == 'a0978a27d6585b8cfe2312125ef5c8c125d3e665c94b17e4a5143e657b669ee3'
    r = D.parse_tenant_json(raw.decode())
    assert r['image'].endswith('3ed33157966c466932f7201586eaf3a005f720421e5989d7aa14ba33454e0e43') and r['finance_catchup_version'] == 1
    assert 'control_closure_recovery_version' not in r
