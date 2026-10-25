"""CURRENT runtime succession (owner handoff V2): original namespace kept, runtime replaced."""
from copy import deepcopy
import pytest
from tools.tenancy import current_runtime_succession_image_check as CHECK
from tools.tenancy import current_release_handoff as A, current_controller as C, current_policy as P
from tools.tenancy import durable_plan as D, orchestration_contract as O, tenant_backfill as BF


def test_packaged_check_passes():
    assert CHECK.check() == {'current_runtime_succession_adapter': 'PASS'}


@pytest.fixture
def succession():
    with CHECK.scenario() as values:
        yield values


def test_every_entity_reads_back_its_pre_succession_plan_but_never_dispatches_it(succession):
    b, policy, _, _ = succession
    assert C.policy(b) == policy
    for entity in P.ORDER:
        if entity == 'fbo_postings': continue
        live = P.document(policy, CHECK.DAY, entity); old = P.original_runtime(policy, live)
        assert old['runtime_plan']['entity'] == entity and old['ack_hash'] != live['ack_hash']
        assert P.shard(policy, CHECK.DAY, old) != P.shard(policy, CHECK.DAY, live)
        assert P.validate_document(policy, CHECK.DAY, old)
        with pytest.raises(ValueError): P.validate_document(policy, CHECK.DAY, old, historical=False)


def test_runtime_mismatch_without_owner_succession_stays_fail_closed(succession):
    b, policy, handoff, _ = succession
    del b.tables.values[(b.c['datasets']['ref'], A.name(handoff['new_source']))]
    # Without the association the original policy names a foreign controller: no CURRENT authority.
    assert C.policy(b) is None
    b.tables.values[(b.c['datasets']['ref'], P.name(handoff['new_source']))] = (
        {'kind': 'current_policy', 'root': policy['hash'][:16]}, D.encoded(policy))
    with pytest.raises(ValueError, match='qualified release pair differs'): C.policy(b)


def test_v2_publication_is_owner_only_and_cas(succession):
    b, _, handoff, release = succession
    b.phase_b_ack_sha256 = None
    with pytest.raises(BF.B.EvidenceError, match='Phase B'): A.publish(b, handoff, release)
    b.phase_b_ack_sha256 = handoff['owner_ack_sha256']
    assert A.publish(b, handoff, release) == handoff and not b.tables.writes   # already present: no write
    other = dict({k: v for k, v in handoff.items() if k != 'hash'}, created_at='2026-10-14T06:00:00+00:00')
    other['hash'] = D.digest(other)
    with pytest.raises(BF.B.EvidenceError, match='CAS'): A.publish(b, other, release)
