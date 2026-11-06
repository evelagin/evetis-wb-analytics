"""Exact owner-only partial campaign accounting recovery; no source authority.

The original failed controller already closed its control claim. A successful
runtime can have a zero-row GET429 prefix. Only that prefix is reconciled here,
without another checkpoint/lease write, source replay or completion certificate.
"""
from datetime import date
import re
from tools.tenancy import durable_plan as D, tenant_backfill as BF, current_policy as P
from tools.tenancy.validation import parse_tenant_json as parse

TYPE = 'OWNER_CURRENT_PARTIAL_PERSISTENCE_STOP_RECOVERY_V1'
KIND = 'FULL_CURRENT_PARTIAL_PERSISTENCE_STOP_RECOVERED'
PREDICATES = frozenset({'explicit_phase_b', 'qualified_replacement', 'runtime_unchanged',
    'bindings_healthy', 'manifest_unchanged', 'prior_certificates_valid', 'only_exact_stop',
    'terminal_failed_controller', 'canonical_closed_control_claim', 'original_current_scope',
    'exact_first_intent_receipt', 'terminal_success_runtime', 'contiguous_source_prefix',
    'campaign_get429_only', 'no_external_report_effect', 'bounded_cooldown',
    'zero_partial_source_state', 'zero_RAW_snapshot', 'no_source_successor',
    'original_checkpoint_lease_LD', 'no_conflicting_RECON', 'paused_inactive_deployment',
    'originals_retained', 'qualified_exact_recovery', 'source_budget_zero'})
FIELDS = frozenset({'version', 'type', 'tenant', 'project', 'root', 'manifest_hash',
    'stop_hash', 'controller_execution', 'actor_created_at', 'actor_completed_at', 'control_generation', 'claim_hash', 'closure_hash',
    'current_policy_hash', 'day', 'shard', 'plan_id', 'ack_hash', 'run_id', 'source_generation',
    'intent_hash', 'receipt_hash', 'state_hash', 'unit_hash', 'checkpoint_hash', 'lease_hash',
    'lease_value', 'lease_expires_at', 'LD_hash', 'reconciliation', 'reconciliation_hash', 'old_source', 'old_image',
    'new_source', 'new_image', 'new_implementation_hash', 'runtime_image',
    'runtime_implementation_hash', 'owner_ack_sha256', 'qualification_hash', 'security_hash',
    'source_probe_hash', 'effect_hash', 'protected_root_hashes', 'protected_shard_hashes',
    'predicates', 'verified_at', 'source_budget', 'historical_exception', 'hash'})


def fail(s): raise BF.B.EvidenceError('current partial recovery: ' + s)
def sealed(p): return dict(p, hash=D.digest(p))
def auth(p): return 'BFCURR_AUTH_' + p['root'] + '_' + p['stop_hash']
def cert(p): return 'BFCURR_' + p['hash']
def ref(p):
    # Protected root hashes may exceed a table-description limit. Full proof
    # remains in its immutable durable record, never truncated or rewritten.
    return ({'kind': 'current_partial_recovery', 'root': p['root'][:16]},
            D.encoded(dict(version=1, type=TYPE, certificate_hash=p['hash'],
                           root=p['root'], stop_hash=p['stop_hash'], source_budget=0)))


def header(p, m):
    if (not isinstance(p, dict) or set(p) != FIELDS or type(p['version']) is not int
            or p['version'] != 1 or p['type'] != TYPE
            or p['hash'] != D.digest({k: v for k, v in p.items() if k != 'hash'})
            or (p['tenant'], p['project'], p['root'], p['manifest_hash']) !=
               (m['tenant'], m['project'], m['hash'], D.digest(m))
            or (p['tenant'], p['project']) != ('client_001', 'mpa-t-client-001')):
        fail('closed exact owner certificate required')
    if (type(p['source_budget']) is not int or p['source_budget'] != 0
            or p['historical_exception'] != 'UNPROVEN_FIRST_CLOUD_STACKTRACE'
            or not isinstance(p['predicates'], dict) or set(p['predicates']) != PREDICATES
            or any(v is not True for v in p['predicates'].values())
            or any(type(p[k]) is not int or p[k] < 1 for k in ('control_generation', 'source_generation'))):
        fail('unknown mandatory/source-free predicate')
    for k in FIELDS:
        if k.endswith('_hash') or k.endswith('_sha256') or k in {'hash', 'root', 'shard'}:
            D.check_hash(p[k])
    if any(not isinstance(p[k], str) or not re.fullmatch('[0-9a-f]{40}', p[k])
           for k in ('old_source', 'new_source')): fail('immutable releases required')
    for k in ('protected_root_hashes', 'protected_shard_hashes'):
        if (not isinstance(p[k], list) or not 1 <= len(p[k]) <= 10000
                or p[k] != sorted(set(p[k]))): fail('bounded immutable originals required')
        for h in p[k]: D.check_hash(h)
    from tools.tenancy import full_history as F
    F.stamp(p['verified_at']); F.stamp(p['lease_expires_at'])
    if not F.stamp(p['actor_created_at']) <= F.stamp(p['actor_completed_at']) <= F.stamp(p['verified_at']):
        fail('original actor lifetime differs')
    if (not isinstance(p['lease_value'], list) or len(p['lease_value']) != 2
            or D.digest(p['lease_value']) != p['lease_hash']): fail('retained lease proof differs')
    return p


def reconciliation(doc, state, unit, snapshot):
    """Pure result equivalent to current Backend.reconcile, without CP/LD writes."""
    BF.B.validate(doc['runtime_plan'], state)
    if (doc['runtime_plan']['entity'] != 'ads_campaigns' or state['complete'] is not False
            or state['sequence'] != 1
            or not BF.rejected_campaign_prefix({1: BF.source_detail(unit)}, state)
            or snapshot != {'rows_n': 0, 'keys_n': 0}):
        fail('only proven zero-row first campaign GET429 prefix supported')
    persistence = dict(catalog_linkage='PENDING_CURRENT_DAY_DQ', scope='BOUNDED_PILOT',
        source_sequence=1, readback=[dict(table='RAW_OZON_ADS_CAMPAIGNS', rows=0, keys=0,
            historical_status='UNPROVEN')], unknown_finance_type_ids=[],
        history_boundary_published=False, full_history='UNPROVEN', economic_finality='UNPROVEN')
    return dict(checkpoint='RUNNING', sequence=1, rows_observed=0, orders=0, supplies=0,
                bundles=0, coverage='UNPROVEN_PENDING_DQ', lifecycle_changed=False,
                scheduler_changed=False, persistence=persistence, source_complete=False)


def recon_record(p):
    return dict(version=D.VERSION, root_hash=p['shard'], kind='RECONCILED', sequence=1,
                payload=p['reconciliation'])


def validate(p, b, m, records, *, final=True):
    """Retained-prefix validation; normal future source work is never reset."""
    header(p, m)
    from tools.tenancy import full_leaf_recovery as FL, current_controller as C, controller_cadence as CC
    from tools.tenancy import full_fbo_diagnostic_closure as LQ
    registered = LQ.qualified_release(b, m, p['new_source'])
    if ((registered['image'], registered['controller_implementation_hash'], registered['runtime_image'], registered['runtime_implementation_hash']) !=
            (p['new_image'], p['new_implementation_hash'], p['runtime_image'], p['runtime_implementation_hash'])
            or registered.get('verification', {}).get('current_data_adapter') != 'PASS'):
        fail('retained replacement qualification differs')
    if not set(p['protected_root_hashes']) <= {D.digest(r) for r in records}:
        fail('original root evidence differs')
    stop = FL.one(records, 'STOPPED', p['stop_hash'])
    if stop['payload'] != {'reason': 'CONTROLLER_GATE_OR_EVIDENCE_FAILURE',
                            'controller_execution': p['controller_execution']}:
        fail('only exact retained generic controller STOP supported')
    policy = C.policy(b)
    if policy is None or policy['hash'] != p['current_policy_hash']: fail('current policy forked')
    day = date.fromisoformat(p['day']); _, items = C.plans(b, policy, day)
    item = items.get('ads_campaigns')
    if not item or item['shard'] != p['shard']: fail('original elected campaign scope absent')
    doc = item['plan']
    if (doc['runtime_plan']['plan_id'], doc['ack_hash'], doc['image']) != (
            p['plan_id'], p['ack_hash'], p['runtime_image']): fail('original plan differs')
    hs = b.store.history(p['shard']); intent = FL.one(hs, 'DISPATCH_INTENT', p['intent_hash'])
    receipt = FL.one(hs, 'DISPATCH_RECEIPT', p['receipt_hash']); rc = receipt['payload']['receipt']
    if (intent['sequence'] != 1 or receipt['sequence'] != 1
            or intent['payload']['plan'] != doc or receipt['payload']['plan'] != doc
            or (rc['run_id'], rc['lease_generation'], rc['ack_hash']) !=
               (p['run_id'], p['source_generation'], p['ack_hash'])
            or any(intent['payload']['preparation'][k] != rc[k]
                   for k in ('run_id', 'lease_generation', 'ack_hash'))
            or not set(p['protected_shard_hashes']) <= {D.digest(r) for r in hs}):
        fail('exact first authority/originals differ')
    if p['reconciliation_hash'] != D.digest(recon_record(p)):
        fail('canonical partial RECON hash differs')
    recons = [r for r in hs if r['kind'] == 'RECONCILED' and r['sequence'] == 1]
    if (final and recons != [recon_record(p)]) or (not final and recons not in ([], [recon_record(p)])):
        fail('missing or conflicting partial RECON')
    # Re-read ORIGINAL run/prefix; a successor may naturally progress later.
    units = b.select(f"SELECT DISTINCT backfill_detail_json,evidence_json FROM `{b.journal}` WHERE started_at>=@origin AND backfill_plan_id=@pid AND entity='ads_campaigns' AND status='OK' AND backfill_sequence=1 AND ingestion_run_id=@unit LIMIT 2",
        {'origin': ('TIMESTAMP', doc['runtime_plan']['origin']), 'pid': ('STRING', p['plan_id']),
         'unit': ('STRING', p['run_id'] + '-u1')})
    if len(units) != 1 or D.digest(units[0]) != p['unit_hash']: fail('original unit changed')
    proof = parse(units[0]['evidence_json']); state = proof['state']; unit = parse(units[0]['backfill_detail_json'])
    if (proof['plan'] != doc['runtime_plan'] or D.digest(state) != p['state_hash']
            or reconciliation(doc, state, unit, {'rows_n': 0, 'keys_n': 0}) != p['reconciliation']):
        fail('original partial prefix/accounting differs')
    ds = b.c['datasets']['tenant_locks']
    claim_marker = b.tables.get_table(ds, CC.names(p['root'], p['control_generation'])[0])
    claim = parse(claim_marker[1]) if claim_marker else {}
    close = b.tables.get_table(ds, CC.names(p['root'], p['control_generation'])[1])
    if (claim.get('hash') != p['claim_hash'] or claim.get('execution') != p['controller_execution']
            or set(claim) != CC.CLAIM_FIELDS or type(claim['version']) is not int or claim['version'] != 1
            or claim['hash'] != D.digest({k: v for k, v in claim.items() if k != 'hash'})
            or (claim['tenant'], claim['project'], claim['root'], claim['generation']) !=
               (p['tenant'], p['project'], p['root'], p['control_generation'])
            or claim['policy_hash'] != D.digest(b.c['orchestration']['cadence'])
            or claim.get('source') != p['old_source'] or claim.get('image') != p['old_image']
            or claim_marker != CC.claim_value(claim) or not CC.closed(b, claim)
            or parse(close[1]).get('hash') != p['closure_hash']
            or parse(close[1]).get('status') != 'STOPPED' or parse(close[1]).get('source_dispatches') != 0
            or not CC.at(p['actor_created_at']) <= CC.at(claim['created_at']) <= CC.at(parse(close[1])['closed_at']) <= CC.at(p['actor_completed_at'])):
        fail('already closed original control claim differs')
    from tools.tenancy import full_history as F
    if p['control_generation'] == 506: fail('claim506 is never this recovery')
    # Original evidence_json spacing is retained as bytes inside the row hashes.
    cps = b.select(f"SELECT DISTINCT backfill_id,status,lease_generation,evidence_json,FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E6SZ',updated_at,'UTC') AS updated_at FROM `{b.c['project_id']}.{b.c['datasets']['tenant_ops']}.BACKFILL_CHECKPOINTS` WHERE run_id=@run AND entity='ads_campaigns' ORDER BY updated_at LIMIT 20",
                   {'run': ('STRING', p['run_id'])})
    cid = BF.B.digest(['BOUNDED_PILOT_EXCLUSIVE', p['project']])[:16]
    if (D.digest(cps) != p['checkpoint_hash']
            or D.digest(b.tables.get_table(ds, f"LD_{cid}_{p['source_generation']:04d}")) != p['LD_hash']):
        fail('original checkpoint/lease/LD bytes differ')
    lease = b.tables.get_table(ds, BF.CK.lease_name(cid, p['source_generation']))
    saved = p['lease_value']
    if (saved[0].get('owner') != p['run_id'] or parse(saved[1]) != {'mode': 'BOUNDED_PILOT', 'ack_hash': p['ack_hash']}
            or not 0 <= F.stamp(p['lease_expires_at']).timestamp() - (int(saved[0]['until']) + BF.CK.LEASE_TABLE_KEEP.total_seconds()) < 1
            or (lease is not None and D.digest(lease) != p['lease_hash'])
            or (lease is None and b.clock() < F.stamp(p['lease_expires_at']))):
        fail('original lease changed or disappeared before qualified expiration')
    return p


def observe(b, m, stop_hash, day, *, owner_ack_sha256, qualification_hash, security_hash, source_probe_hash):
    """Fresh bounded source-free observations; no mutation or source transport."""
    from tools.tenancy import full_controller as H, current_controller as C, controller_cadence as CC
    from tools.tenancy import full_leaf_recovery as FL, orchestration_contract as O, cloud_controller as CLOUD
    if getattr(b, 'current_execution', None) is not None: fail('inactive owner operation required')
    if getattr(b, 'phase_b_ack_sha256', None) != owner_ack_sha256:
        fail('explicit actual Phase B owner scope required')
    records = b.store.history(m['hash']); stop = FL.one(records, 'STOPPED', stop_hash)
    H.verify_authority(H.Backend(b, m), m)
    actor = stop['payload'].get('controller_execution')
    x = b.request('GET', BF.RUN_API + '/' + actor)
    if (x.get('name') != actor or not x.get('completionTime') or x.get('failedCount') != 1
            or x.get('succeededCount', 0) or x.get('cancelledCount', 0) or x.get('retriedCount', 0)
            or x.get('taskCount') != 1): fail('exact failed controller required')
    env = {v['name']: v.get('value') for v in x['template']['containers'][0]['env']}
    old = parse((BF.REPO / 'infra/tenant/releases/backfill' / (env['CONTROLLER_SOURCE_SHA'] + '.json')).read_text())
    old_block = O.block(O.historical_contract(b.c, old, m), dict(release=old['source_sha'], root_hash=m['hash'], scheduler_state='ENABLED'), BF.REPO, old)
    CC.verify_execution_template(b, x, old_block)
    policy = C.policy(b)
    if policy is None: fail('original current namespace unavailable')
    _, items = C.plans(b, policy, date.fromisoformat(day)); item = items.get('ads_campaigns')
    if not item: fail('exact elected Ads scope required')
    doc = item['plan']; BF.validate_plan(doc, doc['ack_hash']); shard = item['shard']
    hs = b.store.history(shard)
    intents = [r for r in hs if r['kind'] == 'DISPATCH_INTENT']
    receipts = [r for r in hs if r['kind'] == 'DISPATCH_RECEIPT']
    if (len(intents) != 1 or len(receipts) != 1
            or intents[0]['sequence'] != 1 or receipts[0]['sequence'] != 1):
        fail('unique first source authority required')
    receipt = receipts[0]['payload']['receipt']; run = receipt['run_id']
    base, jobs = BF.resources(b.c); job = next(n for n, cfg in jobs.items() if 'ads_campaigns' in cfg['entities'])
    if not receipt['operation'].startswith(base + '/operations/'): fail('foreign source receipt')
    operation = b.request('GET', BF.RUN_API + '/' + receipt['operation'])
    ex = operation.get('response', {})
    t = ex.get('template', {}); containers = t.get('containers', []); co = containers[0] if len(containers) == 1 else {}
    runtime_env = {v['name']: v.get('value') for v in co.get('env', [])}
    required = {'INGESTION_RUN_ID': run, 'ENTITIES': 'ads_campaigns', 'SINCE': doc['runtime_plan']['from'],
        'UNTIL': doc['runtime_plan']['to'], 'BACKFILL_MODE': BF.B.VERSION,
        'BACKFILL_GENERATION': doc['runtime_plan']['generation'], 'BACKFILL_ORIGIN': doc['runtime_plan']['origin'],
        'BACKFILL_TARGET_PROJECT': b.c['project_id'], 'TENANT_BINDING_REQUIRED': '1', 'STRICT_PAGE_CAPS': '1',
        'BACKFILL_MAX_REQUESTS': str(doc['max_requests']), 'BACKFILL_MAX_UNITS': str(doc['max_units'])}
    if (not receipt['operation'].startswith(base + '/operations/') or not operation.get('done')
            or operation.get('error') or not ex.get('name', '').startswith(base + '/jobs/' + job + '/executions/')
            or not ex.get('completionTime') or ex.get('failedCount', 0) or ex.get('cancelledCount', 0)
            or ex.get('retriedCount', 0) or ex.get('succeededCount') != 1 or ex.get('taskCount') != 1
            or co.get('image') != doc['image'] or co.get('command') or co.get('args')
            or len(runtime_env) != len(co.get('env', [])) or t.get('timeout') != '3600s' or t.get('maxRetries', 0) != 0
            or t.get('serviceAccount') != f"{b.c['marketplaces']['ozon']['service_accounts']['runtime']}@{b.c['project_id']}.iam.gserviceaccount.com"
            or any(runtime_env.get(k) != v for k, v in required.items())
            or CLOUD.timestamp(ex['completionTime']) >= CLOUD.timestamp(x['startTime'])):
        fail('exact prior terminalSUCCESS runtime required')
    units = b.select(f"SELECT DISTINCT backfill_detail_json,evidence_json FROM `{b.journal}` WHERE started_at>=@origin AND backfill_plan_id=@pid AND entity='ads_campaigns' AND status='OK' AND backfill_sequence=1 AND ingestion_run_id=@unit LIMIT 2",
        {'origin': ('TIMESTAMP', doc['runtime_plan']['origin']), 'pid': ('STRING', doc['runtime_plan']['plan_id']), 'unit': ('STRING', run + '-u1')})
    if len(units) != 1: fail('original prefix ambiguous')
    prefix = b.select(f"SELECT DISTINCT ingestion_run_id,backfill_sequence,backfill_detail_json,evidence_json FROM `{b.journal}` WHERE started_at>=@origin AND backfill_plan_id=@pid AND entity='ads_campaigns' AND status='OK' AND backfill_sequence IS NOT NULL LIMIT 3",
        {'origin': ('TIMESTAMP', doc['runtime_plan']['origin']), 'pid': ('STRING', doc['runtime_plan']['plan_id'])})
    if prefix != [dict(ingestion_run_id=run+'-u1',backfill_sequence=1,**units[0])]: fail('source prefix gap/duplicate/conflict')
    proof = parse(units[0]['evidence_json']); state = proof['state']; unit = parse(units[0]['backfill_detail_json'])
    if proof['plan'] != doc['runtime_plan'] or b.state(doc) != state: fail('source prefix advanced or differs')
    snapshot = b.select(f"SELECT COUNT(*) AS rows_n,COUNT(DISTINCT TO_JSON_STRING(STRUCT(snapshot_date,campaign_id))) AS keys_n FROM `{b.c['project_id']}.ozon_raw.RAW_OZON_ADS_CAMPAIGNS` WHERE snapshot_date=@day",
                        {'day': ('DATE', doc['runtime_plan']['observation_date'])})
    if len(snapshot) != 1: fail('snapshot readback ambiguous')
    recon = reconciliation(doc, state, unit, snapshot[0])
    expected_recon = dict(version=D.VERSION, root_hash=shard, kind='RECONCILED', sequence=1, payload=recon)
    if any(r['kind'] == 'RECONCILED' and r != expected_recon for r in hs): fail('conflicting existing RECON')
    ds = b.c['datasets']['tenant_locks']; claims = []
    for n, *_ in b.tables.list_tables(ds):
        if n.startswith('BFCW_' + m['hash'] + '_'):
            marker = b.tables.get_table(ds, n); claim = parse(marker[1])
            if claim.get('execution') == actor: claims.append((claim, marker))
    if len(claims) != 1: fail('unique original closed control claim required')
    claim, _ = claims[0]; close = b.tables.get_table(ds, CC.names(m['hash'], claim['generation'])[1])
    if not CC.closed(b, claim): fail('never repair a missing control closure')
    cid = BF.B.digest(['BOUNDED_PILOT_EXCLUSIVE', b.c['project_id']])[:16]
    lease = b.tables.get_table(ds, BF.CK.lease_name(cid, receipt['lease_generation']))
    lease_metadata = b.request('GET', BF.TT.BQ + f"/projects/{b.c['project_id']}/datasets/{ds}/tables/{BF.CK.lease_name(cid, receipt['lease_generation'])}")
    from datetime import datetime, timezone
    expiry = datetime.fromtimestamp(int(lease_metadata['expirationTime']) / 1000, timezone.utc).isoformat()
    ld = b.tables.get_table(ds, f"LD_{cid}_{receipt['lease_generation']:04d}")
    if (not lease or lease[0].get('owner') != run or parse(lease[1]).get('ack_hash') != doc['ack_hash']
            or not ld or ld[0] != {'owner': run}
            or parse(ld[1]) != {'operation': receipt['operation'], 'ack_hash': doc['ack_hash']}):
        fail('original source lease/terminal LD unproven')
    cps = b.select(f"SELECT DISTINCT backfill_id,status,lease_generation,evidence_json,FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E6SZ',updated_at,'UTC') AS updated_at FROM `{b.c['project_id']}.{b.c['datasets']['tenant_ops']}.BACKFILL_CHECKPOINTS` WHERE run_id=@run AND entity='ads_campaigns' ORDER BY updated_at LIMIT 20", {'run': ('STRING', run)})
    saved = [parse(z['evidence_json']).get('proof') for z in cps]
    if (len(cps) != 2 or any(z['status'] != 'RUNNING' or z['lease_generation'] != receipt['lease_generation'] for z in cps)
            or saved[0] is not None or not isinstance(saved[1], dict)
            or saved[1].get('state') != state or saved[1].get('plan') != doc['runtime_plan']):
        fail('original two RUNNING checkpoint bytes unproven')
    assert_frozen(b, x['startTime'])
    accepted = prior(b, m, records)
    covered = {p['stop_hash'] for p in accepted}
    if {D.digest(r) for r in records if r['kind'] == 'STOPPED'} - covered != {stop_hash}:
        fail('another unresolved STOP')
    done = H.completed(records, m)
    from tools.tenancy import full_fbo_diagnostic_closure as LQ, recent_priority as RP
    quarantine = H.isolated_indices(m, LQ.load(b, m, records))
    if RP.pending_receipts(b, C.historical_candidates(records, H.reconstruct_plans(records, m, b.clock()), done, quarantine)):
        fail('another historical pending source authority')
    for text in C.cycles(b, policy):
        _, current = C.plans(b, policy, date.fromisoformat(text))
        pending_elections = RP.pending_receipts(b, {k: dict(shard_root=v['shard'], plan=v['plan']) for k, v in current.items()})
        if any(v['shard_root'] != shard for _, v in pending_elections): fail('another current elected authority')
        for v in current.values():
            rh = b.store.history(v['shard'])
            if any(r['kind'] == 'DISPATCH_INTENT' and not any(q['kind'] == 'DISPATCH_RECEIPT' and q['sequence'] == r['sequence'] for q in rh) for r in rh):
                fail('unreceipted current source intent')
            pending = {r['sequence'] for r in rh if r['kind'] == 'DISPATCH_RECEIPT'} - {r['sequence'] for r in rh if r['kind'] == 'RECONCILED'}
            if pending and (v['shard'] != shard or pending != {1}): fail('another current pending source')
    successors = b.select(f"SELECT COUNT(*) AS n FROM `{b.journal}` WHERE started_at>=@start", {'start': ('TIMESTAMP', x['startTime'])})
    if successors != [{'n': 0}]: fail('source journal successor after failed actor')
    active_env = b.c['orchestration']['job']['env']; source = active_env['CONTROLLER_SOURCE_SHA']
    release = parse(b.tables.get_table(ds, CLOUD.descriptor_name(source, m['hash'], active_env['HISTORICAL_SCHEDULER_STATE']))[1])['release']
    O.verify_artifact_source(release, BF.REPO)
    facts = dict(unit=units[0], snapshot=snapshot, actor=x, runtime=ex, claim=claim,
                 closure=close, checkpoints=cps, lease=lease, LD=ld)
    p = dict(version=1, type=TYPE, tenant=m['tenant'], project=m['project'], root=m['hash'], manifest_hash=D.digest(m),
        stop_hash=stop_hash, controller_execution=actor, actor_created_at=x['createTime'], actor_completed_at=x['completionTime'], control_generation=claim['generation'], claim_hash=claim['hash'], closure_hash=parse(close[1])['hash'],
        current_policy_hash=policy['hash'], day=day, shard=shard, plan_id=doc['runtime_plan']['plan_id'], ack_hash=doc['ack_hash'], run_id=run,
        source_generation=receipt['lease_generation'], intent_hash=D.digest(intents[0]), receipt_hash=D.digest(receipts[0]), state_hash=D.digest(state),
        unit_hash=D.digest(units[0]), checkpoint_hash=D.digest(cps), lease_hash=D.digest(lease), lease_value=list(lease), lease_expires_at=expiry, LD_hash=D.digest(ld), reconciliation=recon,
        old_source=old['source_sha'], old_image=old['image'], new_source=source, new_image=release['image'], new_implementation_hash=release['controller_implementation_hash'],
        runtime_image=doc['image'], runtime_implementation_hash=BF.B.implementation_hash(), owner_ack_sha256=owner_ack_sha256, qualification_hash=qualification_hash,
        security_hash=security_hash, source_probe_hash=source_probe_hash, effect_hash=D.digest(facts), protected_root_hashes=sorted(D.digest(r) for r in records),
        protected_shard_hashes=sorted(D.digest(r) for r in hs if r != expected_recon), predicates=dict.fromkeys(PREDICATES, True), verified_at=b.clock().isoformat(),
        source_budget=0, historical_exception='UNPROVEN_FIRST_CLOUD_STACKTRACE')
    p['reconciliation_hash'] = D.digest(recon_record(p)); p = sealed(p)
    validate(p, b, m, records, final=False); return p, facts


def prior(b, m, records):
    from tools.tenancy import full_leaf_recovery as FL, full_cooldown_recovery as FC, full_policy_bootstrap_recovery as PB
    from tools.tenancy import full_pretransport_recovery as PT, full_first_receipt_recovery as FR, full_controller_closure_recovery as CR
    from tools.tenancy import full_fbo_zero_effect_recovery as ZE, full_fbo_diagnostic_closure as LQ, full_pre_authority_gate_recovery as PAG
    out = []
    for module in (FL, FC, PB, PT, FR, ZE, LQ, PAG):
        if any(r['kind'] == module.KIND for r in records): out += module.load(b, m, records)
    if any(r['kind'] == CR.KIND for r in records):
        out += [dict(stop_hash=h) for p in CR.load(b, m, records) for h in p['stop_hashes']]
    out += load(b, m, records)
    return out


def assert_frozen(b, after):
    """Only exact dedicated deployment freeze, no cancellation/source successor."""
    from tools.tenancy import orchestration_contract as O, cloud_controller as C
    base, jobs = BF.resources(b.c)
    BF.verify_global_jobs(b.request('GET', BF.global_job_url(b.c['project_id'])), set(jobs) | {'tenant-control', O.JOB}, b.c['region'])
    if parse((BF.REPO/'tenants/client_001/tenant.json').read_text())['modules']['unitka'] is not False:
        fail('Unitka remains disabled')
    for job in list(jobs) + ['tenant-control', O.JOB]:
        x = b.request('GET', BF.RUN_API + '/' + base + '/jobs/' + job)
        if job == O.JOB: O.verify_job(x, b.c['orchestration'])
        elif job in jobs:
            BF.canonical_template(x, jobs[job]['env'], b.c['marketplaces']['ozon']['runtime_image'],
                f"{b.c['marketplaces']['ozon']['service_accounts']['runtime']}@{b.c['project_id']}.iam.gserviceaccount.com")
        else:
            BF.canonical_template(x, b.c['control']['job']['env'], b.c['marketplaces']['ozon']['runtime_image'], b.c['control']['email'], control=True)
        if x.get('runningCount', 0): fail('active execution during recovery')
        inventory = BF.execution_inventory(b.request, base + '/jobs/' + job)['executions']
        if any(not z.get('completionTime') for z in inventory): fail('incomplete deployment drain')
        if job in jobs and any(C.timestamp(z['createTime']) > C.timestamp(after) for z in inventory):
            fail('source successor after failed actor')
    schedules = b.request('GET', BF.SCHED_API + '/' + base + '/jobs?pageSize=100')
    expected_names = {v['scheduler'] for v in jobs.values()} | {O.SCHEDULER}
    names = [x.get('name') for x in schedules.get('jobs', [])]
    if (schedules.get('nextPageToken') or len(names) != len(expected_names)
            or set(names) != {base + '/jobs/' + n for n in expected_names}): fail('Scheduler inventory unproven')
    for x in schedules['jobs']:
        if x['state'] != 'PAUSED': fail('all Schedulers must remain paused during owner recovery')
        if x['name'].endswith('/' + O.SCHEDULER):
            target = x.get('httpTarget', {}); scheduler = b.c['orchestration']['scheduler']
            if (x['schedule'] != '*/3 * * * *' or x['timeZone'] != 'Europe/Moscow'
                    or target.get('uri') != scheduler['uri'] or target.get('httpMethod') != 'POST'
                    or target.get('body') not in (None, '', 'e30=')
                    or target.get('oauthToken', {}).get('scope') != 'https://www.googleapis.com/auth/cloud-platform'
                    or target.get('oauthToken', {}).get('serviceAccountEmail') != b.c['orchestration']['accounts']['wake']['email']):
                fail('dedicated cadence/target/identity changed')
    if b.c['orchestration']['job']['env']['HISTORICAL_SCHEDULER_STATE'] != 'PAUSED':
        fail('paused qualified controller descriptor required')


def publish(b, p, m):
    """Fresh Phase B owner transaction: RECON1 then exact acceptance, no source."""
    from tools.tenancy import full_history as F
    if (getattr(b, 'current_execution', None) is not None
            or getattr(b, 'phase_b_ack_sha256', None) != p.get('owner_ack_sha256')):
        fail('actual Phase B owner backend required')
    header(p, m)
    existing = [r for r in b.store.history(m['hash']) if r['kind'] == KIND and r['payload'].get('stop_hash') == p['stop_hash']]
    if existing:
        if len(existing) != 1 or existing[0]['payload'] != p: fail('conflicting already accepted owner recovery')
        if p not in load(b, m, b.store.history(m['hash'])): fail('accepted recovery readback differs')
        return p
    if not 0 <= (b.clock() - F.stamp(p['verified_at'])).total_seconds() <= 900: fail('fresh recovery proof required')
    fresh, _ = observe(b, m, p['stop_hash'], p['day'], **{k: p[k] for k in ('owner_ack_sha256', 'qualification_hash', 'security_hash', 'source_probe_hash')})
    if any(p[k] != fresh[k] for k in FIELDS - {'verified_at', 'hash'}): fail('mandatory fresh evidence changed')
    b.store.commit(p['shard'], 'RECONCILED', 1, p['reconciliation'], b.clock())
    validate(p, b, m, b.store.history(m['hash']))
    for ds, n in ((b.c['datasets']['ref'], auth(p)), (b.c['datasets']['tenant_locks'], cert(p))):
        if b.tables.get_table(ds, n) is None: b.tables.create_marker(ds, n, *ref(p))
        if b.tables.get_table(ds, n) != ref(p): fail('owner acceptance CAS conflict')
    b.store.commit(m['hash'], KIND, 0, p, b.clock())
    if p not in load(b, m, b.store.history(m['hash'])): fail('independent exact recovery acceptance differs')
    return p


def load(b, m, records):
    result = []
    for r in records:
        if r['kind'] != KIND: continue
        p = r['payload']; validate(p, b, m, records)
        if (r['sequence'] != 0 or r['root_hash'] != m['hash']
                or b.tables.get_table(b.c['datasets']['ref'], auth(p)) != ref(p)
                or b.tables.get_table(b.c['datasets']['tenant_locks'], cert(p)) != ref(p)):
            fail('exact owner acceptance absent')
        result.append(p)
    return result
