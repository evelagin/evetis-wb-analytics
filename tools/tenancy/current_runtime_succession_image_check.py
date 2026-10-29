"""Network-disabled check: CURRENT runtime succession (owner handoff V2), synthetic evidence only.

A runtime change must keep the ORIGINAL current policy byte-exact (f507 and CURRENT recon
certificates require it), read back plans elected under the original runtime pair, never
dispatch them, and pair the replacement controller only with an exactly registered runtime.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import date
from tools.tenancy import current_release_handoff as A, current_controller as C, current_policy as P
from tools.tenancy import durable_plan as D, tenant_backfill as BF
from tools.tenancy.validation import parse_tenant_json as parse

DAY = date(2026, 10, 14)
REAL_REGISTERED = A.registered_runtime


@contextmanager
def patched(obj, name, value):
    old = getattr(obj, name); setattr(obj, name, value)
    try: yield
    finally: setattr(obj, name, old)


def expect_fail(fn, text):
    try: fn()
    except (BF.B.EvidenceError, ValueError) as error:
        assert text in str(error), (text, str(error)); return
    raise AssertionError('accepted: ' + text)


BASE = 'e82ba56bb51c0df095157b337e6146d7429d6269'   # installed release (C) the succession inherits
OLD = 'ba9d3d0430e598290ef71e321f235e27941170f5'    # owner of the original CURRENT policy
NEW = 'a' * 40
ACK = 'e' * 64


def fixture():
    """Post-succession shape: live contract on the replacement runtime, original policy on the
    registered predecessor pair, replacement controller = installed release + succession flag."""
    from types import SimpleNamespace
    from tools.tenancy import orchestration_contract as O, cloud_controller as CLOUD
    from tools.tenancy.current_recon_image_check import Tables, Store, NOW
    c = BF.target('client_001'); root = c['orchestration']['job']['env']['BACKFILL_ROOT_HASH']
    old = parse((BF.REPO / 'infra/tenant/releases/backfill' / (OLD + '.json')).read_text())
    base = parse((BF.REPO / 'infra/tenant/releases/backfill' / (BASE + '.json')).read_text())
    release = dict(deepcopy(base), source_sha=NEW,
        image='europe-west1-docker.pkg.dev/mpa-platform/mpa-runtime/tenant-backfill-controller@sha256:' + '2' * 64,
        controller_implementation_hash=O.implementation_hash(BF.REPO),
        runtime_image=c['marketplaces']['ozon']['runtime_image'], runtime_implementation_hash=BF.B.implementation_hash(),
        current_runtime_succession_version=1,
        verification=dict(base['verification'], current_runtime_succession_adapter='PASS'))
    c['orchestration'] = O.block(c, dict(release=NEW, root_hash=root, scheduler_state='PAUSED'), BF.REPO, release)
    policy = P.sealed(dict(version=1, type=P.C.TYPE, tenant='client_001', project=c['project_id'],
        historical_root=root, owner_decision_sha256='1' * 64, created_at='2026-10-14T04:00:00+00:00',
        first_day='2026-10-14', entities=list(P.ORDER), snapshot_sla_seconds=86400, fbo_sla_seconds=86400,
        controller_source=OLD, controller_image=old['image'], runtime_image=old['runtime_image'],
        runtime_implementation_hash=old['runtime_implementation_hash'],
        fbo_contract_hash=None, source_concurrency=1, finance_finality='PROVISIONAL'))
    tables = Tables(); m = dict(hash=root, tenant='client_001', project=c['project_id'], programs=[])
    b = SimpleNamespace(c=c, manifest=m, clock=lambda: NOW, current_execution=None, phase_b_ack_sha256=ACK,
                        tables=tables, store=Store(), journal=c['project_id'] + '.ozon_raw.OZON_INGESTION_RUNS')
    ds = c['datasets']['ref']
    tables.values[(ds, P.name(OLD))] = ({'kind': 'current_policy', 'root': policy['hash'][:16]}, D.encoded(policy))
    spec = dict(settings=dict(release=NEW, root_hash=root, scheduler_state='PAUSED'), release=release)
    tables.values[(c['datasets']['tenant_locks'], CLOUD.descriptor_name(NEW, root, 'PAUSED'))] = ({}, D.encoded(spec))
    handoff = dict(version=2, type=A.TYPE_V2, tenant='client_001', project=c['project_id'], root=root,
        policy_hash=policy['hash'], old_source=OLD, old_image=old['image'], new_source=NEW, new_image=release['image'],
        new_implementation_hash=release['controller_implementation_hash'], runtime_image=release['runtime_image'],
        runtime_implementation_hash=release['runtime_implementation_hash'], original_runtime_image=policy['runtime_image'],
        original_runtime_implementation_hash=policy['runtime_implementation_hash'], owner_ack_sha256=ACK,
        qualification_hash='3' * 64, security_hash='4' * 64, created_at='2026-10-14T05:00:00+00:00', source_budget=0)
    handoff = dict(handoff, hash=D.digest(handoff)); tables.values[(ds, A.name(NEW))] = A.value(handoff)
    return b, policy, handoff, release


@contextmanager
def scenario():
    """Fixture plus the two cloud facts a CI tree cannot hold: the replacement runtime
    registration and the packaged-artifact match. If this tree still packages the original
    runtime, a synthetic replacement implementation stands in for it (plans are rebuilt
    from it consistently)."""
    from tools.tenancy import orchestration_contract as O
    old = parse((BF.REPO / 'infra/tenant/releases/backfill' / (OLD + '.json')).read_text())
    live = BF.B.implementation_hash()
    synthetic = live if live != old['runtime_implementation_hash'] else 'c' * 64
    with patched(BF.B, 'implementation_hash', lambda: synthetic), patched(A, 'registered_runtime', registered), \
            patched(O, 'verify_artifact_source', lambda r, repo: None):
        yield fixture()


def registered(image, implementation):
    return {'image': image, 'verification': {'built_artifact': {'backfill_implementation_hash': implementation,
                                                                 'backfill_window_v1': 'PASS'}}}


def check():
    from tools.tenancy import orchestration_contract as O
    with scenario() as (b, policy, handoff, release):
        # 1. The original policy is returned byte-exact (f507/recon pins); live runtime accepted.
        assert C.policy(b) == policy and D.encoded(C.policy(b)) == D.encoded(policy)
        assert A.resolve(b, handoff['new_source'], A.value(handoff), release) == policy
        # 2. Same day: a pre-succession plan reads back; a live plan is the only dispatchable form.
        live = P.document(policy, DAY, 'catalog'); original = P.original_runtime(policy, live)
        assert original != live and original['image'] == policy['runtime_image']
        assert original['runtime_plan']['implementation_hash'] == policy['runtime_implementation_hash']
        assert P.validate_document(policy, DAY, live) and P.validate_document(policy, DAY, original)
        assert P.validate_document(policy, DAY, live, historical=False)
        expect_fail(lambda: P.validate_document(policy, DAY, original, historical=False), 'current plan/window/budget differs')
        forged = deepcopy(original); forged['runtime_plan']['implementation_hash'] = '7' * 64
        expect_fail(lambda: P.validate_document(policy, DAY, forged), 'current plan/window/budget differs')
        # 3. Fail-closed owner association variants.
        for key, value, text in (('original_runtime_implementation_hash', BF.B.implementation_hash(), 'distinct original'),
                                 ('original_runtime_implementation_hash', '8' * 64, 'original registered pair'),
                                 ('runtime_implementation_hash', '6' * 64, 'replacement qualification/runtime'),
                                 ('version', 1, 'closed source-free'),
                                 ('source_budget', 1, 'closed source-free')):
            p = {k: v for k, v in handoff.items() if k != 'hash'}; p[key] = value; p['hash'] = D.digest(p)
            expect_fail(lambda: A.resolve(b, p['new_source'], A.value(p), release), text)
        unqualified = dict(release, verification={k: v for k, v in release['verification'].items()
                                                  if k != 'current_runtime_succession_adapter'})
        expect_fail(lambda: A.resolve(b, handoff['new_source'], A.value(handoff), unqualified), 'not qualified')
        # 4. A V1 handoff can never carry a changed runtime.
        v1 = {k: v for k, v in handoff.items() if k not in ('hash', 'original_runtime_image', 'original_runtime_implementation_hash')}
        v1.update(version=1, type=A.TYPE); v1['hash'] = D.digest(v1)
        expect_fail(lambda: A.resolve(b, v1['new_source'], A.value(v1), release), 'original registered pair')
    # 5. The replacement runtime must be exactly registered (real registry, no stub).
    expect_fail(lambda: REAL_REGISTERED(release['runtime_image'], 'f' * 64), 'registration missing')
    expect_fail(lambda: REAL_REGISTERED(release['runtime_image'][:-1] + '0', release['runtime_implementation_hash']), 'registration missing')
    # 6. Release contract: succession requires S4 independence and its own adapter.
    s4 = dict(parse((BF.REPO / 'infra/tenant/releases/backfill' / (BASE + '.json')).read_text()),
              runtime_image=b.c['marketplaces']['ozon']['runtime_image'])
    settings = dict(release=s4['source_sha'], root_hash=b.manifest['hash'], scheduler_state='PAUSED')
    good = dict(s4, current_runtime_succession_version=1,
                verification=dict(s4['verification'], current_runtime_succession_adapter='PASS'))
    assert O.block(b.c, settings, BF.REPO, good)['job']['env']['CONTROLLER_SOURCE_SHA'] == s4['source_sha']
    missing = dict(good, verification=s4['verification'])
    expect_fail(lambda: O.block(b.c, settings, BF.REPO, missing), 'gates not PASS')
    early = {k: v for k, v in good.items() if k != 'current_independence_version'}
    expect_fail(lambda: O.block(b.c, settings, BF.REPO, early), 'unqualified current runtime succession')
    return {'current_runtime_succession_adapter': 'PASS'}


if __name__ == '__main__':
    import json
    print(json.dumps(check(), sort_keys=True))
