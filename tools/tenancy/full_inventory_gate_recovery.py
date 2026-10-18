"""Owner-only exact acceptance of Heldi STOP e29a27b1 (MANIFEST_ROOT/EVIDENCE, execution ff9bq).

The first exception remains UNPROVEN (leading hypothesis: a peer Cloud Run execution
showed a failed task before its completionTime). Acceptance rests only on independent
predicates: the actor took no claim, created no lease/intent/fence, caused no source,
journal or checkpoint effect, and the claim chain stayed closed. Never accepted
together with or as a consequence of any other recovery; publication needs its own ACK.
"""
from tools.tenancy import durable_plan as D, tenant_backfill as BF, full_history as F
from tools.tenancy.validation import parse_tenant_json as parse

KIND = 'FULL_INVENTORY_GATE_STOP_RECOVERED'
TYPE = 'EXACT_CONTROLLER_INVENTORY_TRANSIENT_GATE_FAILURE_V1'
ACK_TYPE = 'HELDI_R2_EXACT_RECOVERY_V1'
CAUSE = 'UNPROVEN_TRANSIENT_INVENTORY_STATE'
ROOT = 'bb3ceca51ec5add6fe6fa3af8ec3720fbb5d9b07054b84e26d6bbfd6c8824222'
STOP = 'e29a27b161ccc1ff537765ad3f7b928f46b62722d0adf640064d2c3157495e4e'
OLD = '8429b691f3c348e515a5db42d57c8ff040a46018'
OLD_IMAGE = 'europe-west1-docker.pkg.dev/mpa-platform/mpa-runtime/tenant-backfill-controller@sha256:3082a258335166edf1a64f21a889b0ad8ee29777e6eb2b2dbb9779ef61cfdc88'
LAST_CLAIM, NEXT_CLAIM = 1292, 1293
PREDICATES = frozenset({'exact_stop_payload', 'exact_actor_terminal', 'no_claim_by_actor', 'claim_chain_closed',
    'no_dispatch_effect', 'persistent_state_valid_after', 'peer_finalization_window', 'replacement_waits_not_stops',
    'all_jobs_inactive', 'all_schedulers_paused', 'frozen_root_recent_preserved', 'prior_recoveries_preserved',
    'no_original_evidence_rewrite', 'owner_exact_scope', 'source_budget_zero', 'historical_cause_unproven'})
GATES = frozenset({'actual_owner_ACK', 'qualified_source', 'qualified_image', 'exact_image_qualification',
    'independent_review', 'fresh_publication_predicates'})
FIELDS = frozenset({'version', 'type', 'tenant', 'project', 'root', 'manifest_hash', 'stop_hash', 'execution',
    'old_source', 'old_image', 'peer_execution', 'last_claim_generation', 'next_claim_generation', 'runtime_image',
    'runtime_implementation_hash', 'new_source', 'new_image', 'new_implementation_hash', 'owner_ack_sha256',
    'qualification_hash', 'review_hash', 'security_hash', 'protected_root_hashes', 'protected_root_set_hash',
    'archive', 'archive_hash', 'effect_hash', 'predicates', 'production_gates', 'verified_at', 'historical_cause',
    'source_budget', 'hash'})


def fail(s): raise BF.B.EvidenceError('exact R2 recovery: ' + s)
def baseline(): return parse((BF.REPO / 'tools/tenancy/fixtures/heldi_r2_boundary.json').read_text())
def sealed(p): return dict(p, hash=D.digest(p))
def auth(p): return 'BFIG_AUTH_' + ROOT + '_' + STOP
def cert(p): return 'BFIG_' + p['hash']
def record(p): return dict(version=D.VERSION, root_hash=ROOT, kind=KIND, sequence=0, payload=p)


def reference(p):
    return ({'kind': 'exact_r2_recovery', 'root': ROOT[:16]}, D.encoded(dict(
        certificate_hash=p['hash'], acceptance_record_hash=D.digest(record(p)),
        protected_root_set_hash=p['protected_root_set_hash'], protected_root_count=len(p['protected_root_hashes']))))


def authority(p):
    return ({'kind': 'exact_r2_owner', 'root': ROOT[:16]}, D.encoded({k: p[k] for k in
        ('type', 'root', 'stop_hash', 'historical_cause', 'owner_ack_sha256', 'new_source', 'new_image',
         'new_implementation_hash', 'qualification_hash', 'review_hash', 'security_hash', 'hash')}))


def archive():
    x = baseline()
    return {k: x[k] for k in ('actor', 'actor_tasks', 'peer', 'peer_tasks', 'claims', 'stop', 'locks_after_actor',
        'journal_after_actor', 'checkpoints_after_actor', 'runtime_runjob_audit', 'observed_at')}


def boundary_summary():
    x = baseline()
    return dict(actor_hash=D.digest(x['actor']), peer_hash=D.digest(x['peer']), claims_hash=D.digest(x['claims']),
                root_records=len(x['protected_root_hashes']), journal_after=0, checkpoints_after=0, runtime_runjob=0,
                lease_max=x['lease_max'])


def claim_execution(value):
    return parse(value[1])['execution'].rsplit('/', 1)[-1] if value else None


def archived_claims_valid(x):
    """1292 = peer smzs2 closed; 1293 = later wake; none is the actor (no claim by actor)."""
    c = x['claims']; actor = x['actor']['name'].rsplit('/', 1)[-1]; peer = x['peer']['name'].rsplit('/', 1)[-1]
    if set(c) != {'1291', '1292', '1293'} or any(not v['claim'] or not v['closure'] for v in c.values()):
        fail('archived claim chain incomplete')
    if claim_execution(c['1292']['claim']) != peer or actor in {claim_execution(v['claim']) for v in c.values()}:
        fail('archived claim ownership differs')


def header(p, m):
    x = baseline()
    if (not isinstance(p, dict) or set(p) != FIELDS or type(p['version']) is not int or p['version'] != 1
            or p['type'] != TYPE or p['hash'] != D.digest({k: v for k, v in p.items() if k != 'hash'})):
        fail('closed sealed certificate required')
    expected = dict(tenant='client_001', project='mpa-t-client-001', root=ROOT, stop_hash=STOP,
        execution=x['actor']['name'], old_source=OLD, old_image=OLD_IMAGE, peer_execution=x['peer']['name'],
        last_claim_generation=LAST_CLAIM, next_claim_generation=NEXT_CLAIM)
    if any(p[k] != v or type(p[k]) is not type(v) for k, v in expected.items()):
        fail('exact incident pins differ')
    if (m['tenant'], m['project'], m['hash'], D.digest(m)) != (p['tenant'], p['project'], ROOT, p['manifest_hash']):
        fail('foreign frozen root')
    if (type(p['source_budget']) is not int or p['source_budget'] != 0 or p['historical_cause'] != CAUSE
            or p['protected_root_hashes'] != x['protected_root_hashes']
            or p['protected_root_set_hash'] != D.digest(x['protected_root_hashes'])
            or p['archive'] != archive() or p['archive_hash'] != D.digest(p['archive'])
            or p['effect_hash'] != D.digest(boundary_summary())):
        fail('archived boundary differs')
    archived_claims_valid(x)
    for k in ('predicates', 'production_gates'):
        if (not isinstance(p[k], dict) or set(p[k]) != (PREDICATES if k == 'predicates' else GATES)
                or any(v is not True for v in p[k].values())):
            fail('NULL/UNKNOWN/FALSE mandatory predicate')
    for k in ('root', 'manifest_hash', 'stop_hash', 'runtime_implementation_hash', 'new_implementation_hash',
              'owner_ack_sha256', 'qualification_hash', 'review_hash', 'security_hash', 'protected_root_set_hash',
              'archive_hash', 'effect_hash', 'hash'):
        D.check_hash(p[k])
    import re
    if not re.fullmatch('[0-9a-f]{40}', p['new_source']) or p['new_source'] == OLD:
        fail('qualified replacement required')
    if not re.fullmatch(r'europe-west1-docker\.pkg\.dev/mpa-platform/mpa-runtime/tenant-backfill-controller@sha256:[0-9a-f]{64}', p['new_image']) or p['new_image'] == OLD_IMAGE:
        fail('immutable replacement required')
    F.stamp(p['verified_at'])
    return p


def validate(p, b, m, records):
    header(p, m)
    from tools.tenancy import full_controller as H, full_fbo_diagnostic_closure as LQ, controller_cadence as CC
    if H.only_manifest(records) != m or not set(p['protected_root_hashes']) <= {D.digest(r) for r in records}:
        fail('protected root evidence missing')
    if [r for r in records if r['kind'] == 'STOPPED' and D.digest(r) == STOP] != [baseline()['stop']]:
        fail('original STOP differs')
    ds = b.c['datasets']['tenant_locks']; c = baseline()['claims']
    for g in ('1291', '1292', '1293'):
        claim, close = CC.names(ROOT, int(g))
        if [list(x) if x else None for x in (b.tables.get_table(ds, claim), b.tables.get_table(ds, close))] != [c[g]['claim'], c[g]['closure']]:
            fail('retained claim chain changed')
    release = LQ.qualified_release(b, m, p['new_source'])
    if ((release['image'], release['controller_implementation_hash'], release['runtime_image'], release['runtime_implementation_hash'])
            != (p['new_image'], p['new_implementation_hash'], p['runtime_image'], p['runtime_implementation_hash'])
            or release.get('verification', {}).get('exact_r1r2_recovery') != 'PASS'):
        fail('qualified exact recovery release differs')
    return p


def load(b, m, records):
    values = [r for r in records if r['kind'] == KIND]
    if len(values) > 1:
        fail('conflicting exact owner acceptance')
    out = []
    for r in values:
        p = r['payload']; validate(p, b, m, records)
        if (r != record(p) or b.tables.get_table(b.c['datasets']['ref'], auth(p)) != authority(p)
                or b.tables.get_table(b.c['datasets']['tenant_locks'], cert(p)) != reference(p)):
            fail('canonical owner acceptance/ref absent')
        out.append(p)
    return out


def assert_owner(b, ack):
    scope = getattr(b, 'r2_production_authority', None)
    if (getattr(b, 'current_execution', None) is not None or not isinstance(scope, dict)
            or scope != dict(type=ACK_TYPE, stop_hash=STOP, source_budget=0, owner_ack_sha256=ack)):
        fail('separate actual owner production ACK required')
    D.check_hash(ack)


def fresh_boundary(b, m, records):
    from tools.tenancy import full_retained_pretransport_recovery as R1, controller_cadence as CC, full_controller as H
    x = baseline()
    actor = b.request('GET', BF.RUN_API + '/' + x['actor']['name'])
    peer = b.request('GET', BF.RUN_API + '/' + x['peer']['name'])
    if actor != x['actor'] or actor.get('failedCount') != 1 or actor.get('succeededCount', 0) or actor.get('retriedCount', 0):
        fail('initial live actor evidence absent/differs')
    if peer != x['peer'] or peer.get('failedCount') != 1 or not peer.get('completionTime'):
        fail('live peer evidence absent/differs')
    R1.assert_frozen(b, actor['createTime'])
    original = [r for r in records if r['kind'] not in {KIND, R1.KIND}]
    if sorted(D.digest(r) for r in original) != x['protected_root_hashes']:
        fail('fresh root differs from archived boundary')
    stops = {D.digest(r) for r in original if r['kind'] == 'STOPPED'} - R1.accepted_stop_hashes(b, m, records)
    if STOP not in stops or not stops <= {STOP, R1.STOP}:
        fail('new unsupported STOP or changed prior recovery')
    ds = b.c['datasets']['tenant_locks']
    claims = sorted(int(n.rsplit('_', 1)[-1]) for n, *_ in b.tables.list_tables(ds) if n.startswith('BFCW_' + ROOT + '_'))
    if not claims or claims != list(range(1, claims[-1] + 1)):
        fail('claim generation gap')
    actor_name = x['actor']['name']
    for g in range(LAST_CLAIM - 1, claims[-1] + 1):
        w, xclose = (b.tables.get_table(ds, n) for n in CC.names(ROOT, g))
        if not w or not xclose or parse(w[1])['execution'] == actor_name:
            fail('claim by actor or unclosed claim')
    H.verify_authority(H.Backend(b, m), m)
    R1.fresh_effects(b, actor['createTime'])
    return boundary_summary()


def observe(b, m, *, owner_ack_sha256, qualification_hash, review_hash, security_hash):
    from tools.tenancy import full_retained_pretransport_recovery as R1
    assert_owner(b, owner_ack_sha256)
    started = b.clock(); rs = b.store.history(ROOT)
    facts = fresh_boundary(b, m, rs)
    release = R1.replacement_release(b, m)
    if not 0 <= (b.clock() - started).total_seconds() <= 900:
        fail('fresh observation exceeded 15min')
    x = baseline()
    p = dict(version=1, type=TYPE, tenant='client_001', project='mpa-t-client-001', root=ROOT, manifest_hash=D.digest(m),
        stop_hash=STOP, execution=x['actor']['name'], old_source=OLD, old_image=OLD_IMAGE, peer_execution=x['peer']['name'],
        last_claim_generation=LAST_CLAIM, next_claim_generation=NEXT_CLAIM, runtime_image=release['runtime_image'],
        runtime_implementation_hash=release['runtime_implementation_hash'], new_source=release['source_sha'],
        new_image=release['image'], new_implementation_hash=release['controller_implementation_hash'],
        owner_ack_sha256=owner_ack_sha256, qualification_hash=qualification_hash, review_hash=review_hash,
        security_hash=security_hash, protected_root_hashes=x['protected_root_hashes'],
        protected_root_set_hash=D.digest(x['protected_root_hashes']), archive=archive(), archive_hash=D.digest(archive()),
        effect_hash=D.digest(facts), predicates=dict.fromkeys(PREDICATES, True), production_gates=dict.fromkeys(GATES, True),
        verified_at=started.isoformat(), historical_cause=CAUSE, source_budget=0)
    p = sealed(p); validate(p, b, m, rs); return p


def write_manifest(p, m):
    """Four exact additive operations; this pure function never executes them."""
    header(p, m); r = record(p); h = D.digest(r)
    schema = parse((BF.REPO / 'tools/tenancy/schema/tenant_ops/BACKFILL_CHECKPOINTS.json').read_text())['schema']
    row = {f['name']: None for f in schema}; day = F.stamp(p['verified_at']).astimezone(BF.B.MSK).date().isoformat()
    row.update(backfill_id=h[:16], entity='orchestration', window_from=day, window_to=day, status='RUNNING', attempts=0,
               updated_at=p['verified_at'], plan_hash=ROOT, evidence_json=D.encoded(r))
    return [dict(dataset='ref', name=auth(p), value=authority(p)),
            dict(dataset='tenant_locks', name=cert(p), value=reference(p)),
            dict(dataset='tenant_ops', name='BACKFILL_CHECKPOINTS', row=row, key='acceptance'),
            dict(dataset='tenant_locks', name='BFR_' + h, value=({'root': ROOT[:16], 'kind': KIND.lower()},
                 D.encoded(dict(root_hash=ROOT, record_hash=h, kind=KIND, sequence=0))))]


def row_present(b, op):
    rows = b.select("SELECT DISTINCT evidence_json FROM `mpa-t-client-001.tenant_ops.BACKFILL_CHECKPOINTS` WHERE plan_hash=@root AND backfill_id=@record LIMIT 2",
                    {'root': ('STRING', ROOT), 'record': ('STRING', op['row']['backfill_id'])})
    if rows and rows != [{'evidence_json': op['row']['evidence_json']}]:
        fail('uncommitted sidecar conflict')
    return bool(rows)


def partial_state(b, p, m):
    return any((b.tables.get_table(op['dataset'], op['name']) is not None) if 'value' in op else row_present(b, op)
               for op in write_manifest(p, m))


def publish(b, p, m):
    """Owner-only, crash-safe, idempotent publication; never implied by R1."""
    assert_owner(b, p.get('owner_ack_sha256')); header(p, m)
    rs = b.store.history(ROOT); existing = [r for r in rs if r['kind'] == KIND]
    if existing:
        if existing != [record(p)] or load(b, m, rs) != [p]:
            fail('already accepted conflict')
        return p
    if getattr(b, 'r2_certificate_persisted', None) != p['hash']:
        fail('certificate must be durably persisted by the owner executor before any write')
    if partial_state(b, p, m):
        from datetime import timedelta
        from tools.tenancy import full_retained_pretransport_recovery as R1
        checked = b.clock()
        if not timedelta(0) <= checked - F.stamp(p['verified_at']) <= timedelta(hours=24):
            fail('partial publication older than 24h: owner decision required')
        fresh_boundary(b, m, rs); release = R1.replacement_release(b, m)
        if (release['source_sha'], release['image'], release['controller_implementation_hash']) != (p['new_source'], p['new_image'], p['new_implementation_hash']):
            fail('installed replacement differs from certificate')
        validate(p, b, m, rs)
    else:
        if not 0 <= (b.clock() - F.stamp(p['verified_at'])).total_seconds() <= 900:
            fail('fresh <=15min proof required')
        checked = F.stamp(p['verified_at'])
        fresh = observe(b, m, **{k: p[k] for k in ('owner_ack_sha256', 'qualification_hash', 'review_hash', 'security_hash')})
        if any(p[k] != fresh[k] for k in FIELDS - {'hash', 'verified_at'}):
            fail('fresh publication predicates changed')
        validate(p, b, m, rs)

    def fresh_write():
        if not 0 <= (b.clock() - checked).total_seconds() <= 900:
            fail('fresh proof expired before write')
    for op in write_manifest(p, m):
        if 'value' in op:
            if b.tables.get_table(op['dataset'], op['name']) is None:
                fresh_write(); b.tables.create_marker(op['dataset'], op['name'], *op['value'])
            if b.tables.get_table(op['dataset'], op['name']) != op['value']:
                fail('owner metadata CAS conflict: ' + op['name'][:24])
        else:
            if not row_present(b, op):
                fresh_write(); b.tables.append(b.c['datasets']['tenant_ops'], 'BACKFILL_CHECKPOINTS', [op['row']])
            if not row_present(b, op):
                fail('additive row not visible')
    if load(b, m, b.store.history(ROOT)) != [p]:
        fail('independent acceptance readback differs')
    return p
