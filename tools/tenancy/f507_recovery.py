"""Owner-only acceptance of one proven source-free historical abort.

The first cloud exception remains UNPROVEN. This is neither a generic gate
exception nor an automatic recovery path. Publication requires a separate
production ACK and fresh observations; retained reads allow normal later work.
"""
from datetime import date
import re
from tools.tenancy import durable_plan as D, tenant_backfill as BF, full_history as F
from tools.tenancy.validation import parse_tenant_json as parse

KIND = 'FULL_EXACT_PRE_DISPATCH_ABORT_RECOVERED'
TYPE = 'EXACT_OWNER_ONLY_PRE_DISPATCH_ABORT_V1'
ACK_TYPE = 'HELDI_F507_EXACT_RECOVERY_ROLLOUT_V1'
IMPLEMENTATION_ACK = '3314075aafdfee36a4ee2a5e4db51ce7982e459b5e8929ae8e27e77122a7a2f3'
STOP = 'f507723075c393505983b7c07353d1c0c820427c415cf2d4ffcf0aad72f0eac4'
ROOT = 'bb3ceca51ec5add6fe6fa3af8ec3720fbb5d9b07054b84e26d6bbfd6c8824222'
OLD = '9ef7ad3c05cf2c1f1ed043cf4d0e5cf8a36f19b1'
GATES = frozenset({'actual_owner_ACK', 'qualified_source', 'qualified_image',
    'exact_image_qualification', 'independent_review', 'fresh_publication_predicates'})
PREDICATES = frozenset({'exact_stop_actor_closed_claim', 'registered_source_free_boundary',
    'originals_preserved', 'source_highwater_268', 'no_source_successor',
    'no_source_preparation', 'no_source_journal_or_checkpoint', 'all_receipts_reconciled',
    'no_pending_or_external_report', 'RAW_equivalence_no_duplicates', 'prior_proofs_valid',
    'policy_namespace_progress_unchanged', 'binding_capability_healthy',
    'runtime_contract_unchanged', 'paused_inactive', 'source_budget_zero'})
FIELDS = frozenset({'version', 'type', 'tenant', 'project', 'root', 'manifest_hash',
    'stop_hash', 'execution', 'old_source', 'old_image', 'control_generation', 'claim_hash',
    'closure_hash', 'runtime_image', 'runtime_implementation_hash', 'current_policy_hash',
    'new_source', 'new_image', 'new_implementation_hash', 'owner_ack_sha256',
    'qualification_hash', 'review_hash', 'security_hash', 'protected_root_hashes',
    'protected_root_set_hash', 'archive', 'archive_hash', 'effect_hash', 'predicates',
    'production_gates', 'verified_at', 'historical_cause', 'source_budget', 'hash'})


def fail(s): raise BF.B.EvidenceError('exact f507 recovery: ' + s)
def baseline(): return parse((BF.REPO / 'tools/tenancy/fixtures/f507_boundary.json').read_text())
def sealed(p): return dict(p, hash=D.digest(p))
def auth(p): return 'BFPDA_AUTH_' + p['root'] + '_' + p['stop_hash']
def cert(p): return 'BFPDA_' + p['hash']
def record(p):
    return dict(version=D.VERSION, root_hash=ROOT, kind=KIND, sequence=1129, payload=p)


def reference(p):
    return ({'kind': 'exact_f507_recovery', 'root': ROOT[:16]}, D.encoded(dict(
        certificate_hash=p['hash'], acceptance_record_hash=D.digest(record(p)),
        protected_root_set_hash=p['protected_root_set_hash'], protected_root_count=290)))


def authority(p):
    return ({'kind': 'exact_f507_owner', 'root': ROOT[:16]}, D.encoded({k: p[k] for k in
        ('type', 'root', 'stop_hash', 'owner_ack_sha256', 'new_source', 'new_image',
         'new_implementation_hash', 'qualification_hash', 'review_hash', 'security_hash', 'hash')}))


def archive():
    """Exact reviewed as-of facts packaged into the canonical acceptance row.

    No future lookup of a local file or expired execution is substituted for an
    initial live observation. The file is implementation-hash bound in the image.
    """
    x = baseline()
    return {k: x[k] for k in ('actor', 'actor_hash', 'claim', 'closure',
        'source_highwater', 'prior_certificates', 'STOP819', 'current_receipts',
        'current_hash', 'checkpoint_hashes', 'fence_hashes', 'reference_hashes',
        'RAW', 'source_effect_proof_hash', 'preservation_QA_sha256', 'protected_current', 'scheduler_contract')}


def boundary_summary():
    x = baseline()
    return dict(actor_hash=x['actor_hash'], current_hash=x['current_hash'], receipts=43,
                source_journal=0, source_checkpoints=0, highwater=268,
                original_root_hashes=x['protected_root_hashes'])


def header(p, m):
    x = baseline()
    if (not isinstance(p, dict) or set(p) != FIELDS or type(p['version']) is not int
            or p['version'] != 1 or p['type'] != TYPE
            or p['hash'] != D.digest({k: v for k, v in p.items() if k != 'hash'})):
        fail('closed sealed certificate required')
    expected = ('client_001', 'mpa-t-client-001', ROOT, x['manifest_hash'], STOP,
        x['actor']['name'], OLD, x['actor']['template']['containers'][0]['image'],
        1129, parse(x['claim'][1])['hash'], parse(x['closure'][1])['hash'],
        x['current_policy']['runtime_image'], x['current_policy']['runtime_implementation_hash'],
        x['current_policy']['hash'])
    keys = ('tenant', 'project', 'root', 'manifest_hash', 'stop_hash', 'execution',
        'old_source', 'old_image', 'control_generation', 'claim_hash', 'closure_hash',
        'runtime_image', 'runtime_implementation_hash', 'current_policy_hash')
    if (tuple(p[k] for k in keys) != expected or type(p['control_generation']) is not int
            or (m['tenant'], m['project'], m['hash'], D.digest(m)) != expected[:4]):
        fail('exact incident/tenant/manifest pins differ')
    if (type(p['source_budget']) is not int or p['source_budget'] != 0
            or p['historical_cause'] != 'UNPROVEN_FIRST_EXCEPTION'
            or p['protected_root_hashes'] != x['protected_root_hashes']
            or p['protected_root_set_hash'] != x['protected_root_set_hash']
            or D.digest(p['protected_root_hashes']) != p['protected_root_set_hash']
            or p['archive'] != archive() or p['archive_hash'] != D.digest(p['archive'])
            or p['effect_hash'] != D.digest(boundary_summary())):
        fail('source-free as-of archive/protected prefix differs')
    for k in ('predicates', 'production_gates'):
        if (not isinstance(p[k], dict) or set(p[k]) != (PREDICATES if k == 'predicates' else GATES)
                or any(v is not True for v in p[k].values())):
            fail('NULL/UNKNOWN/FALSE mandatory predicate')
    for k in FIELDS:
        if k.endswith('_hash') or k.endswith('_sha256') or k in {'root', 'hash'}:
            D.check_hash(p[k])
    if (not isinstance(p['new_source'], str) or not re.fullmatch('[0-9a-f]{40}', p['new_source'])
            or p['new_source'] == OLD or p['owner_ack_sha256'] == IMPLEMENTATION_ACK
            or not re.fullmatch(r'europe-west1-docker\.pkg\.dev/mpa-platform/mpa-runtime/tenant-backfill-controller@sha256:[0-9a-f]{64}', p['new_image'])):
        fail('qualified replacement and separate production ACK required')
    if not F.stamp(x['actor']['completionTime']) <= F.stamp(p['verified_at']):
        fail('invalid as-of acceptance interval')
    if len(D.encoded(record(p)).encode()) > D.MAX_RECORD_BYTES:
        fail('canonical archive exceeds sidecar byte limit')
    return p


def validate(p, b, m, records):
    """Retained validation: no present-day RAW/state/no-source-forever check."""
    from tools.tenancy import full_controller as H, full_leaf_recovery as FL
    from tools.tenancy import controller_cadence as CC, orchestration_contract as O
    from tools.tenancy import full_fbo_diagnostic_closure as LQ, current_controller as C
    header(p, m)
    if H.only_manifest(records) != m or not set(p['protected_root_hashes']) <= {D.digest(r) for r in records}:
        fail('protected original root evidence missing')
    stop = FL.one(records, 'STOPPED', STOP)
    if stop['payload'] != dict(reason='CONTROLLER_GATE_OR_EVIDENCE_FAILURE',
            controller_execution=p['execution'], diagnostic=dict(version=1,
                stage='SOURCE_DISPATCH_BOUNDARY', category='EVIDENCE')):
        fail('exact retained STOP payload differs')
    old = parse((BF.REPO / 'infra/tenant/releases/backfill' / (OLD + '.json')).read_text())
    oldblock = O.block(b.c, dict(release=OLD, root_hash=ROOT, scheduler_state='ENABLED'), BF.REPO, old)
    actor = p['archive']['actor']
    CC.verify_execution_template(b, actor, oldblock)
    if (D.digest(actor) != p['archive']['actor_hash'] or actor.get('failedCount') != 1
            or actor.get('succeededCount', 0) or actor.get('cancelledCount', 0)
            or actor.get('retriedCount', 0) or actor.get('runningCount', 0)
            or actor.get('taskCount') != 1 or not actor.get('completionTime')):
        fail('exact archived terminal actor differs')
    ds = b.c['datasets']['tenant_locks']; cn, xn = CC.names(ROOT, 1129)
    claim = b.tables.get_table(ds, cn); close = b.tables.get_table(ds, xn)
    if (D.digest(claim) != D.digest(p['archive']['claim'])
            or D.digest(close) != D.digest(p['archive']['closure'])):
        fail('original closed control claim changed')
    if not CC.closed(b, parse(claim[1])): fail('canonical original closure absent')
    for path, expected in p['archive']['reference_hashes'].items():
        dataset, name = path.split('/', 1)
        if D.digest(b.tables.get_table(dataset, name)) != expected: fail('retained original reference changed')
    for root, protected in p['archive']['protected_current'].items():
        if not set(protected) <= {D.digest(r) for r in b.store.history(root)}:
            fail('retained accepted current prefix missing')
    policy = C.policy(b)
    if policy != baseline()['current_policy']: fail('original policy namespace/semantics differ')
    release = LQ.qualified_release(b, m, p['new_source'])
    if ((release['image'], release['controller_implementation_hash'], release['runtime_image'], release['runtime_implementation_hash']) !=
            (p['new_image'], p['new_implementation_hash'], p['runtime_image'], p['runtime_implementation_hash'])
            or release.get('verification', {}).get('exact_f507_recovery') != 'PASS'):
        fail('retained exact recovery qualification differs')
    return p


def load(b, m, records):
    values = [r for r in records if r['kind'] == KIND]
    if len(values) > 1: fail('conflicting exact owner acceptance')
    out = []
    for r in values:
        p = r['payload']; validate(p, b, m, records)
        if (r != record(p)
                or b.tables.get_table(b.c['datasets']['ref'], auth(p)) != authority(p)
                or b.tables.get_table(b.c['datasets']['tenant_locks'], cert(p)) != reference(p)):
            fail('canonical owner acceptance/ref absent')
        out.append(p)
    return out


def assert_owner(b, ack):
    scope = getattr(b, 'f507_production_authority', None)
    if (getattr(b, 'current_execution', None) is not None or not isinstance(scope, dict)
            or scope != dict(type=ACK_TYPE, stop_hash=STOP, source_budget=0, owner_ack_sha256=ack)
            or ack == IMPLEMENTATION_ACK):
        fail('separate actual owner production ACK required')
    D.check_hash(ack)


def current_snapshot(b, policy):
    from tools.tenancy import current_controller as C
    out = {}
    for day in C.cycles(b, policy):
        rs, items = C.plans(b, policy, date.fromisoformat(day)); detail = {}
        for entity, item in items.items():
            hs = b.store.history(item['shard'])
            detail[entity] = dict(item=item, records=hs, hashes=sorted(D.digest(r) for r in hs), state=b.state(item['plan']))
        out[day] = dict(records=rs, hashes=sorted(D.digest(r) for r in rs), items=detail)
    return out


def assert_frozen(b, after):
    """Exact old installed or new staged controller; actual Schedulers PAUSED.

    The installed old descriptor is still ENABLED during natural drain. That
    known profile is independently checked, never rewritten or treated as a
    fabricated PAUSED response. The replacement profile must be PAUSED.
    """
    from tools.tenancy import orchestration_contract as O
    env=b.c['orchestration']['job']['env']
    x=baseline()
    if env['CONTROLLER_SOURCE_SHA']==OLD:
        if (env['HISTORICAL_SCHEDULER_STATE']!='ENABLED'
                or b.c['orchestration']['job']['image']!=x['actor']['template']['containers'][0]['image']):
            fail('exact old installed profile differs')
    elif env['HISTORICAL_SCHEDULER_STATE']!='PAUSED':
        fail('replacement recovery profile must be PAUSED')
    base,jobs=BF.resources(b.c)
    BF.verify_global_jobs(b.request('GET',BF.global_job_url(b.c['project_id'])),set(jobs)|{'tenant-control',O.JOB},b.c['region'])
    for name in (*jobs,'tenant-control',O.JOB):
        job=b.request('GET',BF.RUN_API+'/'+base+'/jobs/'+name)
        if name==O.JOB:O.verify_job(job,b.c['orchestration'])
        elif name=='tenant-control':BF.canonical_template(job,b.c['control']['job']['env'],b.c['marketplaces']['ozon']['runtime_image'],b.c['control']['email'],control=True)
        else:BF.canonical_template(job,jobs[name]['env'],b.c['marketplaces']['ozon']['runtime_image'],f"{b.c['marketplaces']['ozon']['service_accounts']['runtime']}@{b.c['project_id']}.iam.gserviceaccount.com")
        xs=BF.execution_inventory(b.request,base+'/jobs/'+name)['executions']
        if job.get('runningCount',0) or any(not z.get('completionTime') for z in xs):fail('active execution')
        count=job.get('executionCount');latest=job.get('latestCreatedExecution')
        if type(count) is not int or count<len(xs):fail('execution inventory count unknown')
        if not latest:
            if count or xs:fail('latest execution metadata missing')
        else:
            matches=[z for z in xs if z.get('name','').rsplit('/',1)[-1]==latest.get('name','').rsplit('/',1)[-1]]
            if (len(matches)!=1 or not matches[0].get('completionTime')
                    or latest.get('completionStatus') not in {'EXECUTION_SUCCEEDED','EXECUTION_FAILED','EXECUTION_CANCELLED'}):fail('queued or absent latest execution')
        if name in jobs and any(F.stamp(z['createTime'])>=F.stamp(after) for z in xs):fail('source successor')
    schedules=b.request('GET',BF.SCHED_API+'/'+base+'/jobs?pageSize=100')
    expected={base+'/jobs/'+v['scheduler'] for v in jobs.values()}|{base+'/jobs/'+O.SCHEDULER}
    ss=schedules.get('jobs',[])
    if schedules.get('nextPageToken') or len(ss)!=4 or {z.get('name') for z in ss}!=expected or any(z.get('state')!='PAUSED' for z in ss):fail('paused Scheduler inventory differs')
    if {z['name']:{k:z.get(k) for k in ('schedule','timeZone','httpTarget','attemptDeadline','retryConfig')} for z in ss}!=x['scheduler_contract']:fail('frozen Scheduler configuration changed')
    dedicated=next(z for z in ss if z['name'].endswith('/'+O.SCHEDULER));target=dedicated.get('httpTarget',{})
    if (dedicated.get('schedule')!='*/3 * * * *' or dedicated.get('timeZone')!='Europe/Moscow'
            or target.get('uri')!=b.c['orchestration']['scheduler']['uri'] or target.get('httpMethod')!='POST'
            or target.get('body') not in (None,'','e30=')
            or target.get('oauthToken',{}).get('scope')!='https://www.googleapis.com/auth/cloud-platform'
            or target.get('oauthToken',{}).get('serviceAccountEmail')!=b.c['orchestration']['accounts']['wake']['email']):fail('original dedicated target/identity/cadence differs')
    if parse((BF.REPO/'tenants/client_001/tenant.json').read_text())['modules']['unitka'] is not False:fail('Unitka must stay disabled')


def fresh_boundary(b, m, records):
    """Bounded live observations required only before initial publication."""
    from tools.tenancy import current_recon_recovery as R, full_controller as H
    from tools.tenancy import current_controller as C, full_fbo_diagnostic_closure as LQ, recent_priority as RP
    x = baseline(); actor = b.request('GET', BF.RUN_API + '/' + x['actor']['name'])
    if actor != x['actor']: fail('initial live actor evidence absent/differs')
    assert_frozen(b, actor['createTime'])
    original = [r for r in records if r['kind'] != KIND]
    if sorted(D.digest(r) for r in original) != x['protected_root_hashes']:
        fail('fresh original root/unknown STOP differs')
    prior = R.prior(b, m, original)
    covered = {p['stop_hash'] for p in prior}
    if {D.digest(r) for r in original if r['kind'] == 'STOPPED'} - covered != {STOP}:
        fail('new unsupported STOP or changed prior recovery')
    H.verify_authority(H.Backend(b, m), m)
    policy = C.policy(b)
    if policy != x['current_policy']: fail('fresh original policy differs')
    cur = current_snapshot(b, policy)
    if D.digest(cur) != x['current_hash']: fail('current source progress changed')
    ds = b.c['datasets']['tenant_locks']
    for name, expected in x['fence_hashes'].items():
        if D.digest(b.tables.get_table(ds, name)) != expected: fail('retained source fence differs')
    for path, expected in x['reference_hashes'].items():
        dataset, name = path.split('/', 1)
        if D.digest(b.tables.get_table(dataset, name)) != expected: fail('retained authority/reference differs')
    cid = BF.B.digest(['BOUNDED_PILOT_EXCLUSIVE', b.c['project_id']])[:16]
    generations = []
    for name, labels, created, *rest in b.tables.list_tables(ds, with_expiry=True):
        if name.startswith('L_' + cid + '_'): generations.append(int(name.rsplit('_', 1)[-1]))
        if name.startswith(('L_' + cid + '_', 'LD_' + cid + '_', 'BFQ_')):
            if F.stamp(created.isoformat() if hasattr(created, 'isoformat') else created) >= F.stamp(actor['createTime']):
                fail('source preparation after actor')
    if not generations or max(generations) != 268: fail('source highwater changed')
    counts = b.select(f"SELECT COUNT(*) AS n FROM `{b.journal}` WHERE started_at>=@start", {'start': ('TIMESTAMP', actor['createTime'])})
    cp_after = b.select(f"SELECT COUNT(*) AS n FROM `{b.c['project_id']}.tenant_ops.BACKFILL_CHECKPOINTS` WHERE updated_at>=@start AND entity!='orchestration'", {'start': ('TIMESTAMP', actor['createTime'])})
    if counts != [{'n': 0}] or cp_after != [{'n': 0}]: fail('source journal/checkpoint after actor')
    total = 0
    for day in cur.values():
        for value in day['items'].values():
            doc = value['item']['plan']; hs = value['records']; plan = doc['runtime_plan']['plan_id']
            receipts = [r for r in hs if r['kind'] == 'DISPATCH_RECEIPT']
            recons = [r for r in hs if r['kind'] == 'RECONCILED']
            if {r['sequence'] for r in receipts} != {r['sequence'] for r in recons}: fail('pending current receipt')
            total += len(receipts)
            if value['state']['progress'].get('report'): fail('unfinished external current report')
            runs = sorted({r['payload']['receipt']['run_id'] for r in receipts})
            cps = b.select(f"SELECT DISTINCT backfill_id,entity,status,lease_generation,run_id,evidence_json,FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E6SZ',updated_at,'UTC') AS updated_at FROM `{b.c['project_id']}.tenant_ops.BACKFILL_CHECKPOINTS` WHERE run_id IN UNNEST(JSON_VALUE_ARRAY(@runs)) ORDER BY updated_at LIMIT 1000", {'runs': ('STRING', D.encoded(runs))}) if runs else []
            if len(cps) >= 1000 or D.digest(sorted(cps, key=D.digest)) != x['checkpoint_hashes'][plan]:
                fail('original source checkpoint bytes changed')
    if total != 43: fail('original accepted receipt inventory differs')
    done = H.completed(original, m); quarantined = H.isolated_indices(m, LQ.load(b, m, original))
    plans = H.reconstruct_plans(original, m, b.clock())
    eligible = {i: p for i, p in plans.items() if i not in done and i not in quarantined}
    if RP.pending_receipts(b, eligible): fail('pending historical authority')
    # A completed/superseded plan can still retain an external report intent.
    # Inspect every immutable retained Ads plan, not only the next eligible leaf.
    docs={r['payload']['plan']['ack_hash']:r['payload']['plan'] for r in original if r['kind']=='CHUNK_PLAN'}
    for doc in docs.values():
        if doc['runtime_plan']['entity'].startswith('ads_') and b.state(doc)['progress'].get('report'):
            fail('unfinished historical report')
    keys = {'RAW_OZON_CATALOG': ('snapshot_date', 'snapshot_date,product_id'),
        'RAW_OZON_STOCKS': ('snapshot_date', 'snapshot_date,sku,warehouse_id'),
        'RAW_OZON_PRICES': ('snapshot_date', 'snapshot_date,offer_id'),
        'RAW_OZON_PRICE_COMMISSIONS': ('snapshot_date', 'snapshot_date,offer_id,sale_scheme,commission_component'),
        'RAW_OZON_FINANCE_ACCRUAL': ('event_date', 'accrual_id,type_id,sku'),
        'RAW_OZON_ADS_CAMPAIGNS': ('snapshot_date', 'snapshot_date,campaign_id')}
    for table, expected in x['RAW'].items():
        col, key = keys[table]
        actual = b.select(f"SELECT COUNT(*) AS rows_n,COUNT(DISTINCT TO_JSON_STRING(STRUCT({key}))) AS keys_n,BIT_XOR(FARM_FINGERPRINT(TO_JSON_STRING(t))) AS fingerprint FROM `mpa-t-client-001.ozon_raw.{table}` t WHERE {col}=@day", {'day': ('DATE', expected['day'])})
        if actual != [{k: v for k, v in expected.items() if k != 'day'}] or expected['rows_n'] != expected['keys_n']:
            fail('RAW partition mismatch/duplicate')
    return dict(actor_hash=D.digest(actor), current_hash=D.digest(cur), receipts=total,
                source_journal=0, source_checkpoints=0, highwater=268,
                original_root_hashes=x['protected_root_hashes'])


def observe(b, m, *, owner_ack_sha256, qualification_hash, review_hash, security_hash):
    """Read-only materialization under the exact staged PAUSED release.

    Pre-install safety is independently read in the exact installed checkout;
    it is never impersonated as the replacement's executing qualification.
    """
    from tools.tenancy import full_fbo_diagnostic_closure as LQ, orchestration_contract as O
    assert_owner(b, owner_ack_sha256)
    started=b.clock()
    rs = b.store.history(ROOT); facts = fresh_boundary(b, m, rs)
    env = b.c['orchestration']['job']['env']; release = LQ.qualified_release(b, m, env['CONTROLLER_SOURCE_SHA'])
    O.verify_artifact_source(release, BF.REPO)
    O.block(b.c,dict(release=release['source_sha'],root_hash=ROOT,scheduler_state='PAUSED'),BF.REPO,release)
    if (release['verification'].get('exact_f507_recovery') != 'PASS'
            or release['runtime_implementation_hash'] != BF.B.implementation_hash()): fail('exact recovery image unqualified')
    if not 0 <= (b.clock()-started).total_seconds() <= 900:fail('fresh observation exceeded 15min')
    x = baseline()
    p = dict(version=1, type=TYPE, tenant='client_001', project='mpa-t-client-001', root=ROOT,
        manifest_hash=x['manifest_hash'], stop_hash=STOP, execution=x['actor']['name'],
        old_source=OLD, old_image=x['actor']['template']['containers'][0]['image'], control_generation=1129,
        claim_hash=parse(x['claim'][1])['hash'], closure_hash=parse(x['closure'][1])['hash'],
        runtime_image=release['runtime_image'], runtime_implementation_hash=release['runtime_implementation_hash'],
        current_policy_hash=x['current_policy']['hash'], new_source=release['source_sha'], new_image=release['image'],
        new_implementation_hash=release['controller_implementation_hash'], owner_ack_sha256=owner_ack_sha256,
        qualification_hash=qualification_hash, review_hash=review_hash, security_hash=security_hash,
        protected_root_hashes=x['protected_root_hashes'], protected_root_set_hash=x['protected_root_set_hash'],
        archive=archive(), archive_hash=D.digest(archive()), effect_hash=D.digest(facts),
        predicates=dict.fromkeys(PREDICATES, True), production_gates=dict.fromkeys(GATES, True),
        verified_at=started.isoformat(), historical_cause='UNPROVEN_FIRST_EXCEPTION', source_budget=0)
    p = sealed(p); validate(p, b, m, rs); return p


def write_manifest(p, m):
    """Four exact additive operations; this pure function never executes them."""
    header(p, m); r = record(p); h = D.digest(r)
    schema = parse((BF.REPO / 'tools/tenancy/schema/tenant_ops/BACKFILL_CHECKPOINTS.json').read_text())['schema']
    row = {f['name']: None for f in schema}; day = F.stamp(p['verified_at']).astimezone(BF.B.MSK).date().isoformat()
    row.update(backfill_id=h[:16], entity='orchestration', window_from=day, window_to=day,
        status='RUNNING', attempts=1129, updated_at=p['verified_at'], plan_hash=ROOT, evidence_json=D.encoded(r))
    return [dict(dataset='ref', name=auth(p), value=authority(p)),
        dict(dataset='tenant_locks', name=cert(p), value=reference(p)),
        dict(dataset='tenant_ops', name='BACKFILL_CHECKPOINTS', row=row),
        dict(dataset='tenant_locks', name='BFR_' + h, value=({'root': ROOT[:16], 'kind': KIND.lower()},
            D.encoded(dict(root_hash=ROOT, record_hash=h, kind=KIND, sequence=1129))))]


def publish(b, p, m):
    """Owner-only, crash-safe publication. Never re-insert an existing sidecar."""
    assert_owner(b, p.get('owner_ack_sha256')); header(p, m)
    rs = b.store.history(ROOT); existing = [r for r in rs if r['kind'] == KIND]
    if existing:
        if existing != [record(p)] or load(b, m, rs) != [p]: fail('already accepted conflict')
        return p
    if not 0 <= (b.clock() - F.stamp(p['verified_at'])).total_seconds() <= 900: fail('fresh <=15min proof required')
    fresh = observe(b, m, **{k: p[k] for k in ('owner_ack_sha256', 'qualification_hash', 'review_hash', 'security_hash')})
    if any(p[k] != fresh[k] for k in FIELDS - {'hash', 'verified_at'}): fail('fresh publication predicates changed')
    def fresh_write():
        if not 0 <= (b.clock()-F.stamp(p['verified_at'])).total_seconds() <= 900:fail('fresh proof expired before write')
    ops = write_manifest(p, m)
    for op in ops[:2]:
        value = b.tables.get_table(op['dataset'], op['name'])
        if value is None:
            fresh_write();b.tables.create_marker(op['dataset'], op['name'], *op['value'])
        if b.tables.get_table(op['dataset'], op['name']) != op['value']: fail('owner metadata CAS conflict')
    h = D.digest(record(p))
    rows = b.select(f"SELECT DISTINCT evidence_json FROM `{p['project']}.tenant_ops.BACKFILL_CHECKPOINTS` WHERE plan_hash=@root AND backfill_id=@record LIMIT 2",
        {'root': ('STRING', ROOT), 'record': ('STRING', h[:16])})
    if rows and rows != [{'evidence_json': D.encoded(record(p))}]: fail('uncommitted sidecar conflict')
    if not rows:
        fresh_write();b.store.write_record(ops[2]['row'])
    b.store._read_rows(ROOT, h)
    op = ops[3]; value = b.tables.get_table(op['dataset'], op['name'])
    if value is None:
        fresh_write();b.tables.create_marker(op['dataset'], op['name'], *op['value'])
    if b.tables.get_table(op['dataset'], op['name']) != op['value']: fail('commit marker conflict')
    if load(b, m, b.store.history(ROOT)) != [p]: fail('independent acceptance readback differs')
    return p
