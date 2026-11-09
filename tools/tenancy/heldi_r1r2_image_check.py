"""Packaged, network-free proofs for the exact Heldi R1/R2 owner recoveries."""
import hashlib
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from tools.tenancy import cloud_access as A, tenant_backfill as BF, durable_plan as D, full_history as F
from tools.tenancy import full_retained_pretransport_recovery as R1, full_inventory_gate_recovery as R2
from pipelines.ozon.runtime import full_resume as FR
from tools.tenancy.validation import parse_tenant_json as parse

SAMPLE = dict(new_source='2' * 40, new_image='europe-west1-docker.pkg.dev/mpa-platform/mpa-runtime/tenant-backfill-controller@sha256:' + '2' * 64,
              new_implementation_hash='3' * 64, owner_ack_sha256='4' * 64, qualification_hash='5' * 64, review_hash='6' * 64,
              security_hash='7' * 64, verified_at='2026-10-20T10:00:00+00:00')


def sample_r1(m):
    x = R1.baseline(); c = BF.target('client_001')
    p = dict(SAMPLE, version=1, type=R1.TYPE, tenant='client_001', project='mpa-t-client-001', root=R1.ROOT,
        manifest_hash=D.digest(m), stop_hash=R1.STOP, execution=x['actor']['name'], old_source=R1.OLD, old_image=R1.OLD_IMAGE,
        shard=R1.SHARD, intent_hash=R1.INTENT, run_id=R1.RUN, lease_generation=R1.LEASE, ack_hash=R1.ACK_HASH, index=R1.INDEX,
        receipt_sequence=R1.SEQUENCE, runtime_image=c['marketplaces']['ozon']['runtime_image'],
        runtime_implementation_hash=BF.B.implementation_hash(), protected_root_hashes=x['protected_root_hashes'],
        protected_root_set_hash=D.digest(x['protected_root_hashes']), protected_shard_hashes=x['protected_shard_hashes'],
        archive=R1.archive(), archive_hash=D.digest(R1.archive()), effect_hash=D.digest(R1.boundary_summary()),
        closure_hash='0' * 64, predicates=dict.fromkeys(R1.PREDICATES, True), production_gates=dict.fromkeys(R1.GATES, True),
        historical_cause=R1.CAUSE, source_budget=0)
    p['closure_hash'] = D.digest(R1.closure(p))
    return R1.header(R1.sealed(p), m)


def sample_r2(m):
    x = R2.baseline(); c = BF.target('client_001')
    p = dict(SAMPLE, version=1, type=R2.TYPE, tenant='client_001', project='mpa-t-client-001', root=R2.ROOT,
        manifest_hash=D.digest(m), stop_hash=R2.STOP, execution=x['actor']['name'], old_source=R2.OLD, old_image=R2.OLD_IMAGE,
        peer_execution=x['peer']['name'], last_claim_generation=R2.LAST_CLAIM, next_claim_generation=R2.NEXT_CLAIM,
        runtime_image=c['marketplaces']['ozon']['runtime_image'], runtime_implementation_hash=BF.B.implementation_hash(),
        protected_root_hashes=x['protected_root_hashes'], protected_root_set_hash=D.digest(x['protected_root_hashes']),
        archive=R2.archive(), archive_hash=D.digest(R2.archive()), effect_hash=D.digest(R2.boundary_summary()),
        predicates=dict.fromkeys(R2.PREDICATES, True), production_gates=dict.fromkeys(R2.GATES, True),
        historical_cause=R2.CAUSE, source_budget=0)
    return R2.header(R2.sealed(p), m)


def intent_dispatch():
    i = R1.baseline()['intent']; doc = i['payload']['plan']; prep = i['payload']['preparation']
    env = {e['name']: e['value'] for e in prep['overrides']['overrides']['containerOverrides'][0]['env']}
    return i, doc, prep, D.parse_tenant_json(env['BACKFILL_FULL_AUTHORITY'])


def access(calls, tokens):
    class Tokens:
        def token(self, kind):
            tokens.append(kind); return 'SYNTHETIC_NON_CREDENTIAL'
    return A.CloudAccess(BF.target('client_001'), Tokens(), lambda *a: calls.append(a) or {'name': 'synthetic-operation'})


def unknown_predicates_block(module, p, m):
    for key in module.PREDICATES:
        for bad in (False, None, 1, 'UNKNOWN'):
            q = deepcopy(p); q['predicates'][key] = bad; q = module.sealed({k: v for k, v in q.items() if k != 'hash'})
            try:
                module.header(q, m)
            except BF.B.EvidenceError:
                continue
            raise AssertionError('unknown recovery predicate accepted')


def staged_descriptors_real_hashes():
    """Real fresh_effects + verify_artifact_source of THIS image: the byte-pinned 18.10 ec2b3a7 descriptor and the
    image's own staged PAUSED descriptor built from its actual packaged hashes pass; any deviation stops."""
    from tools.tenancy import cloud_controller as C, orchestration_contract as O
    stamp = lambda ms: datetime.fromtimestamp(ms / 1000, timezone.utc)
    pinned = parse((BF.REPO / 'tools/tenancy/fixtures/heldi_post_rollout_staged_descriptor.json').read_text())
    assert hashlib.sha256(pinned['description'].encode()).hexdigest() == R1.STAGED_DESCRIPTORS[pinned['name']][1]
    path = BF.REPO / 'CONTROLLER_SOURCE_SHA'
    own = path.read_text().strip() if path.exists() else '1' * 40
    base = parse((BF.REPO / 'infra/tenant/releases/backfill/ec2b3a7bcf73d52a20c2032db6a28d65f3833887.json').read_text())
    image = 'europe-west1-docker.pkg.dev/mpa-platform/mpa-runtime/tenant-backfill-controller@sha256:' + 'e' * 64
    release = dict(base, source_sha=own, image=image, controller_implementation_hash=O.implementation_hash(BF.REPO))
    assert release['runtime_implementation_hash'] == BF.B.implementation_hash(), 'Ozon runtime changed'
    O.verify_artifact_source(release, BF.REPO)
    labels = {'root': R1.ROOT[:16], 'kind': 'deployment_spec'}

    def spec(source, rel, state='PAUSED'):
        return C.descriptor_name(source, R1.ROOT, state), (dict(labels), D.encoded(dict(settings=dict(release=source, root_hash=R1.ROOT, scheduler_state=state), release=rel)))

    def run(extra, env_source=own, env_image=image, actor=R1.baseline()['actor']['createTime']):
        tables = {('tenant_locks', z['name']): (z['labels'], '{}', z['created_ms']) for z in R1.baseline()['locks_after_actor']}
        tables[('tenant_locks', pinned['name'])] = (pinned['labels'], pinned['description'], pinned['created_ms'])
        for name, value, created in extra:
            tables[('tenant_locks', name)] = (value[0], value[1], created)
        get = lambda ds, n: (lambda v: (v[0], v[1]) if v else None)(tables.get((ds, n)))
        listing = lambda ds, with_expiry=False: [(n, v[0], stamp(v[2]), None) if with_expiry else (n, v[0], stamp(v[2]))
                                                 for (d, n), v in tables.items() if d == ds]
        c = deepcopy(BF.target('client_001'))
        c['orchestration'] = {'job': {'env': {'CONTROLLER_SOURCE_SHA': env_source, 'HISTORICAL_SCHEDULER_STATE': 'PAUSED', 'CONTROLLER_IMAGE': env_image}}}
        b = SimpleNamespace(c=c, tables=SimpleNamespace(get_table=get, list_tables=listing), journal='mpa-t-client-001.ozon_raw.OZON_INGESTION_RUNS',
                            select=lambda sql, params: [{'n': 0}])
        R1.fresh_effects(b, actor)

    later = pinned['created_ms'] + 3600000
    run([spec(own, release) + (later,)])
    run([spec(own, release) + (later,)], actor=R2.baseline()['actor']['createTime'])
    run([])   # the pinned descriptor alone (the current production state)
    forged = dict(release, controller_implementation_hash='0' * 64)
    bad = {
        'foreign_implementation_hash': [spec(own, forged) + (later,)],
        'enabled_own_descriptor': [spec(own, release, 'ENABLED') + (later,)],
        'third_source': [spec('6' * 40, dict(release, source_sha='6' * 40)) + (later,)],
        'image_differs_from_installed': [spec(own, dict(release, image=image.replace('e' * 64, 'f' * 64))) + (later,)],
        'unknown_table': [('X_unknown', ({}, '{}'), later)],
    }
    for case, extra in bad.items():
        try:
            run(extra)
        except BF.B.EvidenceError:
            continue
        raise AssertionError('staged descriptor false pass: ' + case)
    run([], env_source='6' * 40)       # the byte-pinned descriptor never depends on the installed env
    try:
        run([spec(own, release) + (later,)], env_source='6' * 40)   # a foreign installed env cannot vouch for it
    except BF.B.EvidenceError:
        pass
    else:
        raise AssertionError('foreign installed source accepted own descriptor')
    return own


def _check():
    m = FR.manifest(); x1 = R1.baseline()
    own = staged_descriptors_real_hashes()
    i, doc, prep, authority = intent_dispatch(); job = prep['job'].rsplit('/', 1)[-1]
    assert D.digest(i) == R1.INTENT and sorted(D.digest(r) for r in x1['shard_records']) == x1['protected_shard_hashes']
    # Old derivation reproduces the rejection before any credential or transport.
    try:
        FR.scope(doc['runtime_plan'], m['hash'])
    except BF.B.EvidenceError as e:
        assert 'namespace differs' in str(e)
    else:
        raise AssertionError('retained Supplies unexpectedly accepted by FULL scope')
    calls, tokens = [], []
    original = A.retained_supplies_index
    A.retained_supplies_index = lambda m_, d, a, c, t: FR.scope(d['runtime_plan'], m_['hash'])
    try:
        access(calls, tokens).dispatch(doc, job, prep['overrides'], full_authority=authority, full_intent=i)
    except (A.TT.TableError, BF.B.EvidenceError):
        pass
    else:
        raise AssertionError('old derivation sent')
    finally:
        A.retained_supplies_index = original
    assert not calls and not tokens
    # Replacement reaches the send boundary exactly once with the committed body.
    access(calls, tokens).dispatch(doc, job, prep['overrides'], full_authority=authority, full_intent=i)
    assert len(calls) == 1 and tokens == ['reader'] and calls[0][2] == prep['overrides']
    # Without the exact closure the real shard stops; with it the next attempt is a NEW sequence.
    sh = x1['shard_records']; p1 = sample_r1(m)
    assert D.decide_tick(sh, R1.SHARD, {'status': 'ELIGIBLE'}) == {'action': 'RECOVER_RECEIPT_OR_STOP', 'sequence': R1.SEQUENCE}
    after = D.decide_tick(sh + [R1.closure(p1)], R1.SHARD, {'status': 'ELIGIBLE'}, verified_pretransport=[p1])
    assert after.get('sequence') == R1.SEQUENCE + 1 and after['action'] not in {'STOPPED', 'RECOVER_RECEIPT_OR_STOP'}
    for bad in ([R1.closure(p1)], []):
        try:
            D.decide_tick(sh + bad, R1.SHARD, {'status': 'ELIGIBLE'}, verified_pretransport=[dict(p1, hash='f' * 64)])
        except BF.B.EvidenceError:
            pass
        else:
            raise AssertionError('tampered R1 proof accepted')
    unknown_predicates_block(R1, p1, m)
    # R2: independent certificate; never implied by R1; unknown predicates block.
    p2 = sample_r2(m); unknown_predicates_block(R2, p2, m)
    assert p1['stop_hash'] != p2['stop_hash'] and R1.KIND != R2.KIND and R2.CAUSE.startswith('UNPROVEN')
    # R2 replacement: an exact finishing peer is a bounded WAIT (no claim), never a STOP or dispatch.
    from tools.tenancy import controller_cadence as CC, cloud_tick as T
    from tools.tenancy.controller_cadence_image_check import protocol
    _, _, _, b, ex, objects, commits = protocol()
    peer = R2.baseline()['peer']
    ex[0]['failedCount'] = peer['failedCount']
    try:
        CC.acquire(b(2))
    except T.OverlapWait as w:
        assert w.status == 'WAIT_CONTROLLER_FINALIZATION'
    else:
        raise AssertionError('finishing peer did not wait')
    assert not objects and not commits
    return {'r1_old_scope_rejection_reproduced': 'PASS', 'r1_zero_token_and_transport': 'PASS',
            'r1_replacement_exact_dispatch': 'PASS', 'r1_shard_stops_without_closure': 'PASS',
            'r1_new_sequence_after_closure': 'PASS', 'r1_tampered_proof_blocks': 'PASS', 'r1_unknown_predicates_block': 'PASS',
            'r2_independent_certificate': 'PASS', 'r2_unknown_predicates_block': 'PASS', 'r2_finishing_peer_waits': 'PASS',
            'r1r2_staged_descriptors_real_hashes': 'PASS', 'r1r2_staged_descriptor_own_source_' + own: 'PASS'}


def check():
    """Frozen production incident replayed with the runtime it actually ran with."""
    from tools.tenancy.runtime_history_fixture import historical_runtime
    with historical_runtime():
        return _check()


if __name__ == '__main__':
    import json
    print(json.dumps(check(), sort_keys=True))
