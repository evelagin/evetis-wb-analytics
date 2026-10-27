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


def test_retained_f507_acceptance_survives_the_runtime_succession(monkeypatch):
    """The every-wake f507 validation keeps accepting its certificate after the runtime changes.

    The incident and its recovery are replayed with the runtime they ran with; then the live tree
    (replacement runtime, really registered) takes over through an owner V2 association.
    """
    from tools.tenancy import f507_recovery as F507, f507_recovery_image_check as FIX, cloud_controller as CLOUD
    from tools.tenancy import full_fbo_diagnostic_closure as LQ, runtime_history_fixture as RH
    from tools.tenancy.validation import parse_tenant_json as parse
    from contextlib import ExitStack
    if BF.B.implementation_hash() == RH.historical_hash():
        pytest.skip('tree still packages the historical runtime: no succession to prove')
    with ExitStack() as stack:
        with RH.historical_runtime():
            b, f = stack.enter_context(FIX.fixture())
            p = FIX.observed(b); F507.publish(b, p, b.manifest)
        # Replacement controller on the live (registered) runtime, inheriting the installed release.
        succ = 'b' * 40; c = BF.target('client_001')
        base = parse((BF.REPO / 'infra/tenant/releases/backfill' / (CHECK.BASE + '.json')).read_text())
        release = dict(deepcopy(base), source_sha=succ,
            image='europe-west1-docker.pkg.dev/mpa-platform/mpa-runtime/tenant-backfill-controller@sha256:' + '3' * 64,
            controller_implementation_hash=O.implementation_hash(BF.REPO),
            runtime_image=c['marketplaces']['ozon']['runtime_image'], runtime_implementation_hash=BF.B.implementation_hash(),
            current_runtime_succession_version=1,
            verification=dict(base['verification'], current_runtime_succession_adapter='PASS'))
        assert c['marketplaces']['ozon']['runtime_image'] != b.c['marketplaces']['ozon']['runtime_image']
        c['orchestration'] = O.block(c, dict(release=succ, root_hash=F507.ROOT, scheduler_state='PAUSED'), BF.REPO, release)
        b.c = c
        spec = dict(settings=dict(release=succ, root_hash=F507.ROOT, scheduler_state='PAUSED'), release=release)
        b.tables.values[(c['datasets']['tenant_locks'], CLOUD.descriptor_name(succ, F507.ROOT, 'PAUSED'))] = ({}, D.encoded(spec))
        policy = F507.baseline()['current_policy']
        old = parse((BF.REPO / 'infra/tenant/releases/backfill' / (policy['controller_source'] + '.json')).read_text())
        handoff = dict(version=2, type=A.TYPE_V2, tenant='client_001', project=c['project_id'], root=F507.ROOT,
            policy_hash=policy['hash'], old_source=old['source_sha'], old_image=old['image'], new_source=succ,
            new_image=release['image'], new_implementation_hash=release['controller_implementation_hash'],
            runtime_image=release['runtime_image'], runtime_implementation_hash=release['runtime_implementation_hash'],
            original_runtime_image=policy['runtime_image'], original_runtime_implementation_hash=policy['runtime_implementation_hash'],
            owner_ack_sha256='e' * 64, qualification_hash='3' * 64, security_hash='4' * 64,
            created_at='2026-10-16T07:30:00+00:00', source_budget=0)
        handoff['hash'] = D.digest(handoff)
        b.tables.values[(c['datasets']['ref'], A.name(succ))] = A.value(handoff)
        # The synthetic f507 replacement stands for a registered release file (real one: 8429b691).
        real = LQ.qualified_release
        monkeypatch.setattr(LQ, 'qualified_release', lambda bb, m, s: deepcopy(f['release']) if s == FIX.NEW else real(bb, m, s))
        assert C.policy(b) == policy
        assert F507.load(b, b.manifest, b.store.history(F507.ROOT)) == [p]
        # Without the owner association the same wake fails closed (no CURRENT authority swap).
        del b.tables.values[(c['datasets']['ref'], A.name(succ))]
        with pytest.raises((BF.B.EvidenceError, ValueError)):
            F507.load(b, b.manifest, b.store.history(F507.ROOT))
