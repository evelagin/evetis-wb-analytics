"""Owner-only exact closure of FULL retained-Supplies intent 561d5b50 (Heldi STOP e2b59cec).

Reproduced cause: cloud_access.dispatch derived the leaf through full_resume.scope,
which rejects the retained Supplies qualification document, after DISPATCH_INTENT
and before any credential or Run POST. Sequence 68 is closed as rejected; no
receipt, success, reconciliation or replay is created. The original STOP, intent,
RUNNING checkpoint and lease stay immutable. Publication needs a separate owner ACK.
"""
import hashlib
from datetime import timedelta
from tools.tenancy import durable_plan as D, tenant_backfill as BF, full_history as F
from tools.tenancy.validation import parse_tenant_json as parse

KIND = 'FULL_RETAINED_PRETRANSPORT_STOP_RECOVERED'
SHARD_KIND = 'DISPATCH_PRETRANSPORT_REJECTED'
TYPE = 'EXACT_FULL_RETAINED_SUPPLIES_PRETRANSPORT_REJECTION_V1'
ACK_TYPE = 'HELDI_R1_EXACT_RECOVERY_V1'
CAUSE = 'REPRODUCED_FULL_RESUME_SCOPE_REJECTED_RETAINED_SUPPLIES'
ROOT = 'bb3ceca51ec5add6fe6fa3af8ec3720fbb5d9b07054b84e26d6bbfd6c8824222'
STOP = 'e2b59cec26cf61f6cfecb804d6bacdd81fb5ecd5bfadf95ad710837dd8801ea5'
SHARD = '0cbd288401b04b4085d6a71ee4b2cb358b7dcf5fd9a774337865eb48c8a6d716'
INTENT = '561d5b50877b4075b27c7d898ac88fe8fe33e5b8ad5bbfe062037af0dc6321be'
RUN = 'bf-901cc8b0-7b6f-4a5b-91f8-24cd14d4e3a0'
ACK_HASH = '18721b388ab7f956ce4525f54e122624b964af8921094c4c890c0c533071777c'
LEASE, INDEX, SEQUENCE = 285, 465, 68
LEASE_NAME, RELEASE_NAME = 'L_50c5d90dbf43ac9c_0285', 'LD_50c5d90dbf43ac9c_0285'
LEASE_VALUE = ({'owner': RUN, 'until': '1792206880'}, '{"mode": "BOUNDED_PILOT", "ack_hash": "%s"}' % ACK_HASH)
OLD = '8429b691f3c348e515a5db42d57c8ff040a46018'
OLD_IMAGE = 'europe-west1-docker.pkg.dev/mpa-platform/mpa-runtime/tenant-backfill-controller@sha256:3082a258335166edf1a64f21a889b0ad8ee29777e6eb2b2dbb9779ef61cfdc88'
PREDICATES = frozenset({'exact_old_image', 'packaged_pretransport_rejection', 'zero_token_and_transport',
    'replacement_exact_dispatch_pass', 'committed_exact_intent', 'no_receipt_or_operation', 'no_child_runtime',
    'no_journal_or_raw_effect', 'no_source_state_change', 'no_successor_intent', 'no_later_lease',
    'exact_lease_and_running_checkpoint', 'all_jobs_inactive', 'all_schedulers_paused', 'bindings_pass',
    'security_metadata_unchanged', 'runtime_unchanged', 'frozen_root_recent_preserved', 'prior_recoveries_preserved',
    'no_original_evidence_rewrite', 'owner_exact_scope', 'source_budget_zero'})
GATES = frozenset({'actual_owner_ACK', 'qualified_source', 'qualified_image', 'exact_image_qualification',
    'independent_review', 'fresh_publication_predicates'})
FIELDS = frozenset({'version', 'type', 'tenant', 'project', 'root', 'manifest_hash', 'stop_hash', 'execution',
    'old_source', 'old_image', 'shard', 'intent_hash', 'run_id', 'lease_generation', 'ack_hash', 'index',
    'receipt_sequence', 'runtime_image', 'runtime_implementation_hash', 'new_source', 'new_image',
    'new_implementation_hash', 'owner_ack_sha256', 'qualification_hash', 'review_hash', 'security_hash',
    'protected_root_hashes', 'protected_root_set_hash', 'protected_shard_hashes', 'archive', 'archive_hash',
    'effect_hash', 'closure_hash', 'predicates', 'production_gates', 'verified_at', 'historical_cause',
    'source_budget', 'hash'})


# Staged PAUSED descriptors created after the actor by owner rollouts of an R1/R2 replacement,
# byte-pinned: name -> (creationTime ms, sha256 of description). 18.10.2026 13:31:53Z rollout of
# ec2b3a7 (journal sha256 c5a2bc5ced958aeb5a217db48f9cabaa4c73c8c9410da4e1c0c41b3201e57786).
STAGED_DESCRIPTORS = {
    'BF_SPEC_ec2b3a7bcf73d52a20c2032db6a28d65f3833887_' + ROOT + '_PAUSED':
        (1792330313467, '994273f023f0b515038c89c7945699e66b621aacb1d617dbb2f3c65dd55c73cd'),
}


def fail(s): raise BF.B.EvidenceError('exact R1 recovery: ' + s)
def baseline(): return parse((BF.REPO / 'tools/tenancy/fixtures/heldi_r1_boundary.json').read_text())
def sealed(p): return dict(p, hash=D.digest(p))
def auth(p): return 'BFRS_AUTH_' + ROOT + '_' + STOP
def cert(p): return 'BFRS_' + p['hash']
def record(p): return dict(version=D.VERSION, root_hash=ROOT, kind=KIND, sequence=INDEX, payload=p)


def closure(p):
    return dict(version=D.VERSION, root_hash=SHARD, kind=SHARD_KIND, sequence=SEQUENCE,
                payload={k: p[k] for k in ('type', 'stop_hash', 'intent_hash', 'run_id', 'lease_generation', 'source_budget')})


def release_value(p):
    return {'owner': RUN}, D.encoded({'terminal': 'PRETRANSPORT_REJECTED', 'ack_hash': ACK_HASH, 'certificate_hash': p['hash']})


def checkpoint_evidence(p): return {'mode': 'BOUNDED_PILOT', 'pretransport_certificate': p['hash'], 'source_budget': 0}


def reference(p):
    return ({'kind': 'exact_r1_recovery', 'root': ROOT[:16]}, D.encoded(dict(
        certificate_hash=p['hash'], acceptance_record_hash=D.digest(record(p)), closure_hash=p['closure_hash'],
        protected_root_set_hash=p['protected_root_set_hash'], protected_root_count=len(p['protected_root_hashes']))))


def authority(p):
    return ({'kind': 'exact_r1_owner', 'root': ROOT[:16]}, D.encoded({k: p[k] for k in
        ('type', 'root', 'stop_hash', 'intent_hash', 'owner_ack_sha256', 'new_source', 'new_image',
         'new_implementation_hash', 'qualification_hash', 'review_hash', 'security_hash', 'hash')}))


def archive():
    """Exact as-of facts packaged into the canonical acceptance (implementation-hash bound)."""
    x = baseline()
    return {k: x[k] for k in ('actor', 'actor_tasks', 'stop', 'intent', 'lease_0285', 'checkpoints',
        'locks_after_actor', 'journal_after_actor', 'checkpoints_after_actor', 'runtime_runjob_audit', 'observed_at')}


def boundary_summary():
    x = baseline()
    return dict(actor_hash=D.digest(x['actor']), intent_hash=INTENT, root_records=len(x['protected_root_hashes']),
                shard_records=len(x['protected_shard_hashes']), lease_max=x['lease_max'], journal_after=0,
                checkpoints_after=0, runtime_runjob=0)


def header(p, m):
    x = baseline()
    if (not isinstance(p, dict) or set(p) != FIELDS or type(p['version']) is not int or p['version'] != 1
            or p['type'] != TYPE or p['hash'] != D.digest({k: v for k, v in p.items() if k != 'hash'})):
        fail('closed sealed certificate required')
    expected = dict(tenant='client_001', project='mpa-t-client-001', root=ROOT, stop_hash=STOP,
        execution=x['actor']['name'], old_source=OLD, old_image=OLD_IMAGE, shard=SHARD, intent_hash=INTENT,
        run_id=RUN, lease_generation=LEASE, ack_hash=ACK_HASH, index=INDEX, receipt_sequence=SEQUENCE)
    if any(p[k] != v or type(p[k]) is not type(v) for k, v in expected.items()):
        fail('exact incident pins differ')
    if (m['tenant'], m['project'], m['hash'], D.digest(m)) != (p['tenant'], p['project'], ROOT, p['manifest_hash']):
        fail('foreign frozen root')
    if (type(p['source_budget']) is not int or p['source_budget'] != 0 or p['historical_cause'] != CAUSE
            or p['protected_root_hashes'] != x['protected_root_hashes']
            or p['protected_root_set_hash'] != D.digest(x['protected_root_hashes'])
            or p['protected_shard_hashes'] != x['protected_shard_hashes']
            or p['archive'] != archive() or p['archive_hash'] != D.digest(p['archive'])
            or p['effect_hash'] != D.digest(boundary_summary())):
        fail('archived boundary differs')
    for k in ('predicates', 'production_gates'):
        if (not isinstance(p[k], dict) or set(p[k]) != (PREDICATES if k == 'predicates' else GATES)
                or any(v is not True for v in p[k].values())):
            fail('NULL/UNKNOWN/FALSE mandatory predicate')
    for k in FIELDS - {'version', 'type', 'tenant', 'project', 'execution', 'old_source', 'old_image', 'run_id',
                       'lease_generation', 'index', 'receipt_sequence', 'runtime_image', 'new_source', 'new_image',
                       'protected_root_hashes', 'protected_shard_hashes', 'archive', 'predicates', 'production_gates',
                       'verified_at', 'historical_cause', 'source_budget'}:
        D.check_hash(p[k])
    import re
    if not re.fullmatch('[0-9a-f]{40}', p['new_source']) or p['new_source'] == OLD:
        fail('qualified replacement required')
    if not re.fullmatch(r'europe-west1-docker\.pkg\.dev/mpa-platform/mpa-runtime/tenant-backfill-controller@sha256:[0-9a-f]{64}', p['new_image']) or p['new_image'] == OLD_IMAGE:
        fail('immutable replacement required')
    if p['closure_hash'] != D.digest(closure(p)):
        fail('exact rejected intent closure differs')
    F.stamp(p['verified_at'])
    return p


def verify_shard(p, sh, *, final=True, stage=None):
    """Pure shard proof used by decide_tick and pending receipts; never a success."""
    intents = [r for r in sh if r['kind'] == 'DISPATCH_INTENT' and r['sequence'] == SEQUENCE]
    if len(intents) != 1 or D.digest(intents[0]) != INTENT or intents[0] != baseline()['intent']:
        fail('original intent absent/differs')
    if any(r['sequence'] == SEQUENCE and r['kind'] in {'DISPATCH_RECEIPT', 'RECONCILED'} for r in sh):
        fail('rejected attempt has receipt/result')
    stage = stage or ('final' if final else 'pre')
    closures = [r for r in sh if r['kind'] == SHARD_KIND and r['sequence'] == SEQUENCE]
    allowed = {'pre': ([],), 'partial': ([], [closure(p)]), 'final': ([closure(p)],)}[stage]
    if closures not in allowed:
        fail('closure state differs for stage ' + stage)
    return intents[0]['payload']['plan']


def checkpoint_rows(b):
    rows = b.select(f"SELECT DISTINCT status,lease_owner,error_code,evidence_json FROM `mpa-t-client-001.tenant_ops.BACKFILL_CHECKPOINTS` WHERE entity='supplies' AND run_id=@run AND lease_generation=285 AND plan_hash=@ack LIMIT 4",
                    {'run': ('STRING', RUN), 'ack': ('STRING', ACK_HASH)})
    out = []
    for r in rows:
        try:
            out.append(dict(r, evidence_json=D.encoded(parse(r['evidence_json']))))
        except (KeyError, TypeError, ValueError):
            fail('checkpoint evidence is not unambiguous JSON')
    return out


def expected_rows(p):
    base = {'lease_owner': RUN}
    return (dict(base, status='RUNNING', error_code=None, evidence_json=D.encoded({'mode': 'BOUNDED_PILOT', 'proof': None})),
            dict(base, status='FAILED', error_code='PRETRANSPORT_REJECTED', evidence_json=D.encoded(checkpoint_evidence(p))))


def validate(p, b, m, records, *, final=True, stage=None):
    header(p, m); stage = stage or ('final' if final else 'pre')
    from tools.tenancy import full_controller as H, full_fbo_diagnostic_closure as LQ
    if H.only_manifest(records) != m or not set(p['protected_root_hashes']) <= {D.digest(r) for r in records}:
        fail('protected root evidence missing')
    stops = [r for r in records if r['kind'] == 'STOPPED' and D.digest(r) == STOP]
    if stops != [baseline()['stop']]:
        fail('original STOP differs')
    sh = b.store.history(SHARD, max_records=F.MAX_LEAF_RECORDS)
    if not set(p['protected_shard_hashes']) <= {D.digest(r) for r in sh}:
        fail('protected shard evidence missing')
    verify_shard(p, sh, stage=stage)
    locks = b.c['datasets']['tenant_locks']
    lease = b.tables.get_table(locks, LEASE_NAME)
    if lease is None:
        # Only a completed closure may outlive the lease table; the RUNNING checkpoint
        # is the durable copy of its term (same contract as full_pretransport_recovery).
        from tools.tenancy import full_pretransport_recovery as PT
        until = PT.running_lease_until(b, 'mpa-t-client-001', 'supplies', ACK_HASH, LEASE, RUN)
        if stage != 'final' or until is None or not PT.lease_expired(b, until):
            fail('original lease absent before qualified expiration')
    elif lease != LEASE_VALUE:
        fail('original lease differs')
    released = b.tables.get_table(locks, RELEASE_NAME)
    if released not in {'pre': (None,), 'partial': (None, release_value(p)), 'final': (release_value(p),)}[stage]:
        fail('source-free release differs for stage ' + stage)
    rows = checkpoint_rows(b); original, failed = expected_rows(p)
    if original not in rows or any(r not in (original, failed) for r in rows):
        fail('original/additive checkpoint differs')
    if (stage == 'final' and failed not in rows) or (stage == 'pre' and failed in rows):
        fail('additive checkpoint state differs for stage ' + stage)
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
    scope = getattr(b, 'r1_production_authority', None)
    if (getattr(b, 'current_execution', None) is not None or not isinstance(scope, dict)
            or scope != dict(type=ACK_TYPE, stop_hash=STOP, source_budget=0, owner_ack_sha256=ack)):
        fail('separate actual owner production ACK required')
    D.check_hash(ack)


def accepted_stop_hashes(b, m, records):
    """Exactly the full_controller STOP gate acceptance set (prior + f507 + R1/R2)."""
    from tools.tenancy import current_recon_recovery as R, f507_recovery as F507, full_inventory_gate_recovery as R2
    out = {p['stop_hash'] for p in R.prior(b, m, records)} | {p['stop_hash'] for p in F507.load(b, m, records)}
    return out | {p['stop_hash'] for p in load(b, m, records)} | {p['stop_hash'] for p in R2.load(b, m, records)}


def assert_frozen(b, after):
    """Reviewed STOP819 freeze (canonical templates, runtime image, Unitka off, exact dedicated
    target, 4/4 PAUSED, drained) plus: latest execution final and no source/control after the actor."""
    from tools.tenancy import orchestration_contract as O, current_recon_recovery as R
    R.assert_frozen(b, after)
    base, jobs = BF.resources(b.c)
    for name in (*jobs, 'tenant-control', O.JOB):
        job = b.request('GET', BF.RUN_API + '/' + base + '/jobs/' + name)
        xs = BF.execution_inventory(b.request, base + '/jobs/' + name)['executions']
        if job.get('runningCount', 0) or any(not z.get('completionTime') for z in xs):
            fail('active execution')
        latest = job.get('latestCreatedExecution') or {}
        if latest and not latest.get('completionTime'):
            fail('queued or active latest execution')
        if name != O.JOB and any(F.stamp(z['createTime']) >= F.stamp(after) for z in xs):
            fail('source/control execution after actor')
    s = b.request('GET', BF.SCHED_API + '/' + base + '/jobs?pageSize=100')
    expected = {base + '/jobs/' + v['scheduler'] for v in jobs.values()} | {base + '/jobs/' + O.SCHEDULER}
    ss = s.get('jobs', [])
    if s.get('nextPageToken') or len(ss) != 4 or {z.get('name') for z in ss} != expected or any(z.get('state') != 'PAUSED' for z in ss):
        fail('Scheduler not frozen 4/4 PAUSED')


def recovery_artifact(name, labels):
    """Only the additive artifacts of the exact R1/R2 owner recoveries (never source preparation)."""
    from tools.tenancy import full_inventory_gate_recovery as R2
    if name == RELEASE_NAME or name.startswith(('BFRS_', 'BFIG_')):
        return True
    return (name.startswith('BFR_') and labels.get('root') in {ROOT[:16], SHARD[:16]}
            and labels.get('kind') in {KIND.lower(), R2.KIND.lower(), SHARD_KIND.lower()})


def staged_descriptor(b, name, labels, stamp, expiry):
    """Only a staged PAUSED descriptor of a qualified R1/R2 replacement: the byte-pinned rollout one,
    or the installed controller's own descriptor of its qualified release. Never ENABLED, another root,
    another source or the original source."""
    from tools.tenancy import cloud_controller as C, orchestration_contract as O
    if labels != {'root': ROOT[:16], 'kind': 'deployment_spec'} or expiry is not None:
        return False
    marker = b.tables.get_table(b.c['datasets']['tenant_locks'], name)
    if marker is None or marker[0] != labels or not isinstance(marker[1], str):
        return False
    desc = parse(marker[1])
    if (not isinstance(desc, dict) or set(desc) != {'settings', 'release'} or marker[1] != D.encoded(desc)
            or not isinstance(desc['settings'], dict) or not isinstance(desc['release'], dict)):
        return False
    source = desc['settings'].get('release')
    if (desc['settings'] != dict(release=source, root_hash=ROOT, scheduler_state='PAUSED') or source == OLD
            or name != C.descriptor_name(source, ROOT, 'PAUSED') or desc['release'].get('source_sha') != source
            or desc['release'].get('verification', {}).get('exact_r1r2_recovery') != 'PASS'):
        return False
    if name in STAGED_DESCRIPTORS:
        created, digest = STAGED_DESCRIPTORS[name]
        return round(F.stamp(stamp).timestamp() * 1000) == created and hashlib.sha256(marker[1].encode()).hexdigest() == digest
    env = b.c['orchestration']['job']['env']
    if (env.get('CONTROLLER_SOURCE_SHA') != source or env.get('HISTORICAL_SCHEDULER_STATE') != 'PAUSED'
            or env.get('CONTROLLER_IMAGE') != desc['release'].get('image')):
        return False
    O.verify_artifact_source(desc['release'], BF.REPO)
    return True


def fresh_effects(b, after):
    """No source preparation, journal, checkpoint or lock beyond the archived boundary."""
    x = baseline(); locks = b.c['datasets']['tenant_locks']
    archived = {z['name'] for z in x['locks_after_actor']}
    for name, labels, created, *rest in b.tables.list_tables(locks, with_expiry=True):
        stamp = created.isoformat() if hasattr(created, 'isoformat') else created
        if F.stamp(stamp) < F.stamp(after):
            continue
        if name in archived or recovery_artifact(name, labels) or name.startswith(('BFCW_' + ROOT + '_', 'BFCX_' + ROOT + '_')):
            continue
        if name.startswith('BF_SPEC_') and staged_descriptor(b, name, labels, stamp, rest[0] if rest else 'unknown'):
            continue
        fail('unexpected lock/fence/lease after actor: ' + name[:40])
    leases = sorted(n for n, *_ in b.tables.list_tables(locks) if n.startswith('L_50c5d90dbf43ac9c_'))
    if not leases or leases[-1] != x['lease_max']:
        fail('later source lease')
    j = b.select(f"SELECT COUNT(*) AS n FROM `{b.journal}` WHERE started_at>=@t", {'t': ('TIMESTAMP', after)})
    cp = b.select("SELECT COUNT(*) AS n FROM `mpa-t-client-001.tenant_ops.BACKFILL_CHECKPOINTS` WHERE (updated_at>=@t OR updated_at IS NULL) AND IFNULL(entity,'')!='orchestration' AND IFNULL(run_id,'')!=@run",
                  {'t': ('TIMESTAMP', after), 'run': ('STRING', RUN)})
    foreign = b.select("SELECT COUNT(*) AS n FROM `mpa-t-client-001.tenant_ops.BACKFILL_CHECKPOINTS` WHERE run_id=@run AND NOT (IFNULL(entity,'')='supplies' AND IFNULL(lease_generation,-1)=285 AND IFNULL(plan_hash,'')=@ack)",
                       {'run': ('STRING', RUN), 'ack': ('STRING', ACK_HASH)})
    if j != [{'n': 0}] or cp != [{'n': 0}] or foreign != [{'n': 0}]:
        fail('source journal/checkpoint after actor')


def fresh_boundary(b, m, records, sh):
    x = baseline()
    actor = b.request('GET', BF.RUN_API + '/' + x['actor']['name'])
    if actor != x['actor'] or actor.get('failedCount') != 1 or actor.get('succeededCount', 0) or actor.get('retriedCount', 0):
        fail('initial live actor evidence absent/differs')
    assert_frozen(b, actor['createTime'])
    from tools.tenancy import full_inventory_gate_recovery as R2
    original = [r for r in records if r['kind'] not in {KIND, R2.KIND}]
    if sorted(D.digest(r) for r in original) != x['protected_root_hashes']:
        fail('fresh root differs from archived boundary')
    own = [r for r in sh if r['kind'] == SHARD_KIND and r['sequence'] == SEQUENCE and r['payload'].get('type') == TYPE and r['payload'].get('stop_hash') == STOP]
    if sorted(D.digest(r) for r in sh if r not in own) != x['protected_shard_hashes'] or any(r['sequence'] > SEQUENCE for r in sh) or len(own) > 1:
        fail('fresh shard differs or successor authority exists')
    stops = {D.digest(r) for r in original if r['kind'] == 'STOPPED'} - accepted_stop_hashes(b, m, records)
    if STOP not in stops or not stops <= {STOP, R2.STOP}:
        fail('new unsupported STOP or changed prior recovery')
    from tools.tenancy import full_controller as H
    H.verify_authority(H.Backend(b, m), m)
    fresh_effects(b, actor['createTime'])
    return boundary_summary()


def replacement_release(b, m):
    """The installed controller is the staged PAUSED, exactly qualified R1/R2 replacement."""
    from tools.tenancy import full_fbo_diagnostic_closure as LQ, orchestration_contract as O
    env = b.c['orchestration']['job']['env']
    if env['HISTORICAL_SCHEDULER_STATE'] != 'PAUSED' or env['CONTROLLER_SOURCE_SHA'] == OLD:
        fail('frozen replacement required')
    release = LQ.qualified_release(b, m, env['CONTROLLER_SOURCE_SHA'])
    O.verify_artifact_source(release, BF.REPO)
    if release['verification'].get('exact_r1r2_recovery') != 'PASS' or release['runtime_implementation_hash'] != BF.B.implementation_hash():
        fail('exact recovery image unqualified')
    return release


def observe(b, m, *, owner_ack_sha256, qualification_hash, review_hash, security_hash):
    """Read-only materialization under the exact staged PAUSED replacement release."""
    assert_owner(b, owner_ack_sha256)
    started = b.clock()
    rs = b.store.history(ROOT); sh = b.store.history(SHARD, max_records=F.MAX_LEAF_RECORDS)
    facts = fresh_boundary(b, m, rs, sh)
    release = replacement_release(b, m)
    if not 0 <= (b.clock() - started).total_seconds() <= 900:
        fail('fresh observation exceeded 15min')
    x = baseline()
    p = dict(version=1, type=TYPE, tenant='client_001', project='mpa-t-client-001', root=ROOT, manifest_hash=D.digest(m),
        stop_hash=STOP, execution=x['actor']['name'], old_source=OLD, old_image=OLD_IMAGE, shard=SHARD, intent_hash=INTENT,
        run_id=RUN, lease_generation=LEASE, ack_hash=ACK_HASH, index=INDEX, receipt_sequence=SEQUENCE,
        runtime_image=release['runtime_image'], runtime_implementation_hash=release['runtime_implementation_hash'],
        new_source=release['source_sha'], new_image=release['image'], new_implementation_hash=release['controller_implementation_hash'],
        owner_ack_sha256=owner_ack_sha256, qualification_hash=qualification_hash, review_hash=review_hash, security_hash=security_hash,
        protected_root_hashes=x['protected_root_hashes'], protected_root_set_hash=D.digest(x['protected_root_hashes']),
        protected_shard_hashes=x['protected_shard_hashes'], archive=archive(), archive_hash=D.digest(archive()),
        effect_hash=D.digest(facts), closure_hash='0' * 64, predicates=dict.fromkeys(PREDICATES, True),
        production_gates=dict.fromkeys(GATES, True), verified_at=started.isoformat(), historical_cause=CAUSE, source_budget=0)
    p['closure_hash'] = D.digest(closure(p))
    p = sealed(p); validate(p, b, m, rs, final=False); return p


def write_manifest(p, m):
    """Eight exact additive operations, in crash-safe order; this pure function never executes them."""
    header(p, m)
    schema = parse((BF.REPO / 'tools/tenancy/schema/tenant_ops/BACKFILL_CHECKPOINTS.json').read_text())['schema']
    day = F.stamp(p['verified_at']).astimezone(BF.B.MSK).date().isoformat()

    def sidecar(r):
        h = D.digest(r); row = {f['name']: None for f in schema}
        row.update(backfill_id=h[:16], entity='orchestration', window_from=day, window_to=day, status='RUNNING',
                   attempts=r['sequence'], updated_at=p['verified_at'], plan_hash=r['root_hash'], evidence_json=D.encoded(r))
        return row, ('BFR_' + h, ({'root': r['root_hash'][:16], 'kind': r['kind'].lower()},
                     D.encoded(dict(root_hash=r['root_hash'], record_hash=h, kind=r['kind'], sequence=r['sequence']))))
    doc = baseline()['intent']['payload']['plan']
    failed = BF.checkpoint(doc, RUN, 'FAILED', LEASE, F.stamp(p['verified_at']))
    failed['error_code'] = 'PRETRANSPORT_REJECTED'; failed['evidence_json'] = D.encoded(checkpoint_evidence(p))
    crow, (cname, cval) = sidecar(closure(p)); arow, (aname, aval) = sidecar(record(p))
    return [dict(dataset='ref', name=auth(p), value=authority(p)),
            dict(dataset='tenant_locks', name=cert(p), value=reference(p)),
            dict(dataset='tenant_locks', name=RELEASE_NAME, value=release_value(p)),
            dict(dataset='tenant_ops', name='BACKFILL_CHECKPOINTS', row=failed, key='failed_checkpoint'),
            dict(dataset='tenant_ops', name='BACKFILL_CHECKPOINTS', row=crow, key='closure'),
            dict(dataset='tenant_locks', name=cname, value=cval),
            dict(dataset='tenant_ops', name='BACKFILL_CHECKPOINTS', row=arow, key='acceptance'),
            dict(dataset='tenant_locks', name=aname, value=aval)]


def publish(b, p, m):
    """Owner-only, crash-safe, idempotent publication. Existing writes are verified, never repeated."""
    assert_owner(b, p.get('owner_ack_sha256')); header(p, m)
    rs = b.store.history(ROOT); existing = [r for r in rs if r['kind'] == KIND]
    if existing:
        if existing != [record(p)] or load(b, m, rs) != [p]:
            fail('already accepted conflict')
        return p
    if getattr(b, 'r1_certificate_persisted', None) != p['hash']:
        fail('certificate must be durably persisted by the owner executor before any write')
    if partial_state(b, p, m):
        # Interrupted publication of THIS certificate: re-observe the live boundary
        # now, accept only this certificate's exact partial writes, then finish.
        checked = b.clock()
        if not timedelta(0) <= checked - F.stamp(p['verified_at']) <= timedelta(hours=24):
            fail('partial publication older than 24h: owner decision required')
        fresh_boundary(b, m, rs, b.store.history(SHARD, max_records=F.MAX_LEAF_RECORDS))
        release = replacement_release(b, m)
        if (release['source_sha'], release['image'], release['controller_implementation_hash']) != (p['new_source'], p['new_image'], p['new_implementation_hash']):
            fail('installed replacement differs from certificate')
        validate(p, b, m, rs, stage='partial')
    else:
        if not 0 <= (b.clock() - F.stamp(p['verified_at'])).total_seconds() <= 900:
            fail('fresh <=15min proof required')
        checked = F.stamp(p['verified_at'])
        fresh = observe(b, m, **{k: p[k] for k in ('owner_ack_sha256', 'qualification_hash', 'review_hash', 'security_hash')})
        if any(p[k] != fresh[k] for k in FIELDS - {'hash', 'verified_at'}):
            fail('fresh publication predicates changed')
        validate(p, b, m, rs, stage='pre')

    def fresh_write():
        if not 0 <= (b.clock() - checked).total_seconds() <= 900:
            fail('fresh proof expired before write')
    for op in write_manifest(p, m):
        if 'value' in op:
            value = b.tables.get_table(op['dataset'], op['name'])
            if value is None:
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


def row_present(b, op):
    """Exact-content row check; a conflicting row for the same key fails closed."""
    if op['key'] == 'failed_checkpoint':
        rows = [r for r in checkpoint_rows(b) if r['status'] == 'FAILED']
        want = {k: op['row'][k] for k in ('status', 'lease_owner', 'error_code')}
        want['evidence_json'] = D.encoded(parse(op['row']['evidence_json']))
        if any(r != want for r in rows):
            fail('conflicting failed checkpoint')
        return bool(rows)
    rows = b.select("SELECT DISTINCT evidence_json FROM `mpa-t-client-001.tenant_ops.BACKFILL_CHECKPOINTS` WHERE plan_hash=@root AND backfill_id=@record LIMIT 2",
                    {'root': ('STRING', op['row']['plan_hash']), 'record': ('STRING', op['row']['backfill_id'])})
    if rows and rows != [{'evidence_json': op['row']['evidence_json']}]:
        fail('uncommitted sidecar conflict')
    return bool(rows)


def partial_state(b, p, m):
    """True when an interrupted publication of THIS certificate is detectable (any op present)."""
    for op in write_manifest(p, m):
        if 'value' in op:
            if b.tables.get_table(op['dataset'], op['name']) is not None:
                return True
        elif row_present(b, op):
            return True
    return False
