"""Owner-only closure of the exact typed CONTROL_CLOSURE/CLOUD_METADATA STOP after a certified CURRENT entity.

Heldi STOP eca0335a (2026-11-05 22:01:39Z): wake 9sbgd (claim 2404) failed to write its canonical
control closure. cloud_controller.main types a TransientReadError of close_decision (and close_decision's
own gate types any other metadata TableError) as GateFailure('CONTROL_CLOSURE','CLOUD_METADATA'), commits
the STOP and only then closes the claim STOPPED/0. The previous wake had certified the stocks entity of
CURRENT day 06.11 (CURRENT_CERT) and released its source lease; nothing durable followed but the STOP.
The exact HTTP failure is unavailable by design (no exception text is logged), so the cause stays the
closed diagnostic itself, never an invented one.

Recovery is permitted only while every fact proves that the terminal wake did nothing but its claim,
its STOP and its STOPPED/0 closure, and that nothing at all followed the last CURRENT certificate:
  * the STOP is the exact typed payload {reason, controller_execution, diagnostic CONTROL_CLOSURE/CLOUD_METADATA};
  * the controller execution is terminally FAILED, single task, no retry, of its registered release;
  * the canonical claim series is continuous and closed; the terminal claim is closed STOPPED/0 after
    the STOP marker; every later claim dispatched nothing;
  * the last source lease generation is released (LD) by the run of the certified CURRENT shard, before
    the certificate; no lease, fence, record marker or ledger row exists after the certificate other
    than this STOP, the control claims and the qualified replacement's own release markers;
  * no source runtime execution and no ingestion-journal row started after the certificate;
  * every prior certificate still revalidates and this STOP is the only unresolved one;
  * all Schedulers are PAUSED, no job is active, the replacement is qualified for this type.
No lease, receipt, claim, closure, CURRENT plan or certificate is invented. source_budget is 0.
Lease tables (L_) expire by design: only their release markers (LD_, permanent) and the pilot ledger
are required; a still-present L_ table must agree.
"""
from datetime import date, datetime
from tools.tenancy import durable_plan as D, tenant_backfill as BF, full_history as F
from tools.tenancy.validation import parse_tenant_json as parse

KIND = 'CONTROL_CLOSURE_STOP_RECOVERED'
TYPE = 'CONTROL_CLOSURE_METADATA_AFTER_CURRENT_CERT_V1'
CAUSE = 'TYPED_CONTROL_CLOSURE_CLOUD_METADATA'
REASON = 'CONTROLLER_GATE_OR_EVIDENCE_FAILURE'
DIAGNOSTIC = {'version': 1, 'stage': 'CONTROL_CLOSURE', 'category': 'CLOUD_METADATA'}
PREDICATES = frozenset({'exact_typed_control_closure_stop', 'terminal_failed_controller', 'claim_closed_stopped_without_dispatch',
    'canonical_control_series_closed', 'current_cert_last_work', 'current_lease_released_before_certificate',
    'no_authority_after_certificate', 'no_ledger_row_after_certificate', 'no_source_runtime_after_certificate',
    'no_source_journal_after_certificate', 'current_manifest_GO_bindings_caps_valid', 'all_jobs_inactive',
    'all_schedulers_paused', 'prior_certificates_valid', 'recent_priority_preserved', 'original_evidence_preserved',
    'qualified_replacement', 'source_budget_zero'})
FIELDS = frozenset({'version', 'type', 'tenant', 'project', 'root', 'manifest_hash', 'stop_hash', 'execution',
    'policy_hash', 'day', 'day_root', 'entity', 'shard', 'shard_sequence', 'receipt_hash', 'recon_hash', 'cert_hash',
    'lease_generation', 'claim_generation', 'recent_policy_hash',
    'old_source', 'old_image', 'new_source', 'new_image', 'new_implementation_hash', 'runtime_image',
    'owner_decision_sha256', 'qualification_hash', 'security_hash', 'effect_hash', 'source_probe_hash',
    'protected_root_hashes', 'predicates', 'verified_at', 'source_budget', 'historical_cause', 'hash'})
SHARD_KINDS = ('DISPATCH_INTENT', 'DISPATCH_RECEIPT', 'RECONCILED')
CLAIM_PREFIXES = ('BFCW_', 'BFCX_')
FRESH_SECONDS = 900


def publication_writes(b, p, m):
    """The four HTTP writes of publish(), in order: authority marker (ref), certificate marker (tenant_locks),
    the root record's checkpoint row and its BFR_ commit marker (DurableRecords.commit)."""
    record = dict(version=D.VERSION, root_hash=m['hash'], kind=KIND, sequence=0, payload=p)
    return [(b.c['datasets']['ref'], authority(p)), (b.c['datasets']['tenant_locks'], certificate(p)),
            (b.c['datasets']['tenant_ops'], 'BACKFILL_CHECKPOINTS'), (b.c['datasets']['tenant_locks'], 'BFR_' + D.digest(record))]


def fail(s): raise BF.B.EvidenceError('control closure recovery: ' + s)
def sealed(p): return dict(p, hash=D.digest(p))
def authority(p): return 'BFCLX_AUTH_' + p['root'] + '_' + p['stop_hash']
def certificate(p): return 'BFCLX_' + p['hash']


def value(p):
    # The full proof (with the root inventory) lives in its content-addressed root record; the owner
    # markers bind it by digest within the 16-KiB table-description limit (as the terminal-gate markers).
    return {'kind': 'control_closure', 'root': p['root'][:16]}, D.encoded(dict(version=1, type='CONTROL_CLOSURE_CERTIFICATE_REF',
        root=p['root'], stop_hash=p['stop_hash'], certificate_hash=p['hash'], source_budget=0))


def when(v):
    if isinstance(v, datetime):
        if v.utcoffset() is None: fail('marker timestamp unzoned')
        return v
    return F.stamp(v)


def exact_stop(stop, execution):
    return stop['sequence'] == 0 and stop['payload'] == {'reason': REASON, 'controller_execution': execution, 'diagnostic': DIAGNOSTIC}


def header(p, m):
    if (not isinstance(p, dict) or set(p) != FIELDS or type(p['version']) is not int or p['version'] != 1 or p['type'] != TYPE
            or p['hash'] != D.digest({k: v for k, v in p.items() if k != 'hash'})): fail('closed certificate required')
    if (p['tenant'], p['project'], p['root'], p['manifest_hash']) != (m['tenant'], m['project'], m['hash'], D.digest(m)): fail('foreign manifest')
    if type(p['source_budget']) is not int or p['source_budget'] != 0 or p['historical_cause'] != CAUSE: fail('source-free scope only; no invented cause')
    if not isinstance(p['predicates'], dict) or set(p['predicates']) != PREDICATES or any(x is not True for x in p['predicates'].values()): fail('unknown mandatory predicate')
    for k in FIELDS:
        if k.endswith('_hash') or k.endswith('_sha256') or k in ('hash', 'root', 'shard', 'day_root'): D.check_hash(p[k])
    for k in ('shard_sequence', 'lease_generation', 'claim_generation'):
        if type(p[k]) is not int or not 1 <= p[k] <= 9999999999: fail('bounded shard/lease/claim identity required')
    if not isinstance(p['entity'], str) or not isinstance(p['execution'], str): fail('closed identities required')
    try: day = date.fromisoformat(p['day'])
    except (TypeError, ValueError): fail('exact CURRENT day required')
    if p['day_root'] != D.digest(dict(current_policy=p['policy_hash'], moscow_day=day.isoformat())): fail('CURRENT day root differs')
    if (not isinstance(p['protected_root_hashes'], list) or p['protected_root_hashes'] != sorted(set(p['protected_root_hashes']))
            or not 1 <= len(p['protected_root_hashes']) <= 10000): fail('bounded retained records required')
    F.stamp(p['verified_at'])
    return p


def validate(p, b, m, records):
    from tools.tenancy import full_controller as H, full_leaf_recovery as FL
    header(p, m)
    if H.only_manifest(records) != m or not set(p['protected_root_hashes']) <= {D.digest(z) for z in records}: fail('immutable root differs')
    if not exact_stop(FL.one(records, 'STOPPED', p['stop_hash']), p['execution']): fail('only the exact typed control-closure STOP supported')
    r = parse((BF.REPO / 'infra/tenant/releases/backfill' / f"{p['old_source']}.json").read_text())
    v = r.get('verification', {})
    if (r['image'] != p['old_image'] or v.get('cadence_overlap_adapter') != 'PASS' or v.get('controller_wake_budget_adapter') != 'PASS'
            or v.get('terminal_gate_recovery_adapter') != 'PASS'): fail('typed control-closure release absent')
    return p


def load(b, m, records):
    out = []
    for r in records:
        if r['kind'] != KIND: continue
        p = r['payload']; validate(p, b, m, records)
        if r['sequence'] != 0 or r['root_hash'] != m['hash'] or any(b.tables.get_table(ds, n) != value(p) for ds, n in (
                (b.c['datasets']['ref'], authority(p)), (b.c['datasets']['tenant_locks'], certificate(p)))): fail('owner acceptance absent')
        out.append(p)
    return out


def _current_cert(b, policy_hash, day, run_id, lease_generation):
    """The certified CURRENT entity of `day` whose single shard receipt is the run holding the last lease.

    Structural, policy-object free: the day index record, the cycle root, the plan's shard, its
    reconciled receipt and the certificate bind each other by digest. Returns (entity, shard, sequence,
    receipt, recon, cert record)."""
    from tools.tenancy import current_contract as K
    day_root = D.digest(dict(current_policy=policy_hash, moscow_day=day))
    index = [r for r in b.store.history(policy_hash) if r['kind'] == 'CURRENT_DAY' and r['payload'].get('day') == day]
    if len(index) != 1 or index[0]['payload'] != {'day': day, 'root': day_root, 'policy_hash': policy_hash}: fail('CURRENT day index differs')
    cycle = b.store.history(day_root)
    found = []
    for plan in (r for r in cycle if r['kind'] == 'CURRENT_PLAN'):
        v = plan['payload']; doc = v.get('plan') or {}
        if set(v) != {'day', 'policy_hash', 'plan', 'shard'} or v['policy_hash'] != policy_hash or v['day'] != day: fail('CURRENT plan ownership differs')
        if v['shard'] != D.digest(dict(current_cycle=day_root, ack_hash=doc.get('ack_hash'))): fail('CURRENT shard ownership differs')
        sh = b.store.history(v['shard'])
        receipts = [r for r in sh if r['kind'] == 'DISPATCH_RECEIPT' and r['payload']['receipt'].get('run_id') == run_id]
        if not receipts: continue
        if len(receipts) != 1: fail('CURRENT receipt of the last lease ambiguous')
        found.append((v, doc, sh, receipts[0]))
    if len(found) != 1: fail('last lease is not a CURRENT shard of the day')
    v, doc, sh, receipt = found[0]; entity = doc['runtime_plan']['entity']; seq = receipt['sequence']
    if receipt['payload']['plan'] != doc or receipt['payload']['receipt'].get('lease_generation') != lease_generation or receipt['payload']['receipt'].get('ack_hash') != doc['ack_hash']:
        fail('CURRENT receipt identity differs')
    if max((r['sequence'] for r in sh), default=0) != seq or any(r['kind'] not in SHARD_KINDS for r in sh): fail('CURRENT shard authority beyond the certified receipt')
    recon = [r for r in sh if r['kind'] == 'RECONCILED' and r['sequence'] == seq]
    intent = [r for r in sh if r['kind'] == 'DISPATCH_INTENT' and r['sequence'] == seq]
    if len(recon) != 1 or len(intent) != 1: fail('CURRENT receipt intent/reconciliation ambiguous')
    recon = recon[0]
    if recon['payload'].get('checkpoint') != 'DONE' or recon['payload'].get('source_complete') is not True or recon['payload'].get('failed'): fail('CURRENT shard not reconciled complete')
    certs = [r for r in cycle if r['kind'] == 'CURRENT_CERT' and r['payload'].get('fact', {}).get('entity') == entity]
    if len(certs) != 1: fail('CURRENT certificate of the entity absent/ambiguous')
    cert = certs[0]; c = cert['payload']
    if (c.get('policy_hash'), c.get('day'), c.get('shard'), c.get('ack_hash'), c.get('accepted_receipt_hash'), c.get('reconciliation_hash')) != (
            policy_hash, day, v['shard'], doc['ack_hash'], D.digest(receipt), D.digest(recon)): fail('CURRENT certificate binding differs')
    # The fact is checked as of its own observation (its 24 h freshness SLA concerns CURRENT readiness,
    # not this proof); the observation must precede the certificate marker (checked by the caller).
    K.check_fact(c['fact'], policy_hash, K.at(c['fact']['observed_at']))
    if c['fact']['source_run_id'] != run_id: fail('CURRENT certificate run differs')
    if D.digest(b.state(doc)) != c.get('state_hash'): fail('CURRENT source state changed since its certificate')
    return entity, v['shard'], seq, receipt, recon, cert, day_root


def observe(b, m, stop_hash, *, policy_hash, day, owner_decision_sha256, qualification_hash, security_hash, source_probe_hash):
    from tools.tenancy import full_controller as H, full_leaf_recovery as FL, controller_cadence as CC, orchestration_contract as O
    from tools.tenancy import recent_priority as RP, stop_acceptance as SA
    D.check_hash(policy_hash)
    records = b.store.history(m['hash']); stop = FL.one(records, 'STOPPED', stop_hash); execution = stop['payload'].get('controller_execution')
    if not isinstance(execution, str) or not exact_stop(stop, execution): fail('only the exact typed control-closure STOP supported')
    # Terminal FAILED controller of its own registered (claim-enabled) release.
    x = b.request('GET', BF.RUN_API + '/' + execution); co = x['template']['containers'][0]; env = {z['name']: z.get('value') for z in co['env']}
    if (not x.get('completionTime') or x.get('failedCount') != 1 or x.get('succeededCount', 0) or x.get('cancelledCount', 0)
            or x.get('retriedCount', 0) or x.get('runningCount', 0) or x.get('taskCount') != 1): fail('exact terminal FAILED controller required')
    if env.get('BACKFILL_ROOT_HASH') != m['hash'] or env.get('HISTORICAL_SCHEDULER_STATE') != 'ENABLED': fail('claim-enabled exact root required')
    old = parse((BF.REPO / 'infra/tenant/releases/backfill' / f"{env['CONTROLLER_SOURCE_SHA']}.json").read_text())
    c = O.historical_contract(b.c, old, m); block = O.block(c, dict(release=old['source_sha'], root_hash=m['hash'], scheduler_state='ENABLED'), BF.REPO, old)
    O.verify_job({'template': {'taskCount': 1, 'template': x['template']}}, block)
    if b.current_execution is not None: fail('inactive owner freeze required')
    H.verify_authority(b, m)
    started = F.stamp(x['createTime']); completed = F.stamp(x['completionTime'])
    ds = b.c['datasets']['tenant_locks']; listing = {n: (labels, created) for n, labels, created in b.tables.list_tables(ds)}
    if 'BFR_' + stop_hash not in listing: fail('terminal stop marker absent')
    stopped_at = when(listing['BFR_' + stop_hash][1])
    if not started < stopped_at <= completed <= b.clock(): fail('terminal stop outside its controller execution')
    # Canonical control series: every claim closed, continuous; the terminal claim closed STOPPED/0
    # after the STOP marker (no earlier closure landed); every later claim dispatched nothing.
    prefix = 'BFCW_' + m['hash'] + '_'; claims = []; own = None
    for n in sorted(listing):
        if not n.startswith(prefix): continue
        marker = b.tables.get_table(ds, n); p = parse(marker[1])
        if (set(p) != CC.CLAIM_FIELDS or p.get('version') != 1 or p.get('root') != m['hash'] or p.get('tenant') != m['tenant'] or p.get('project') != m['project']
                or p['hash'] != D.digest({k: v for k, v in p.items() if k != 'hash'}) or marker != CC.claim_value(p)): fail('corrupt canonical claim')
        if n != CC.names(m['hash'], p['generation'])[0]: fail('claim generation/name differs')
        close_name = CC.names(m['hash'], p['generation'])[1]; close = b.tables.get_table(ds, close_name)
        if close is None: fail('unclosed canonical control authority')
        q = parse(close[1])
        if (set(q) != CC.CLOSE_FIELDS or q.get('version') != 1 or q['claim_hash'] != p['hash'] or q['status'] not in CC.STATUSES
                or q['hash'] != D.digest({k: v for k, v in q.items() if k != 'hash'}) or type(q['source_dispatches']) is not int
                or close != ({'kind': 'controller_close', 'root': m['hash'][:16]}, D.encoded(q))): fail('corrupt canonical closure')
        if p['execution'] == execution:
            if own is not None or q['status'] != 'STOPPED' or q['source_dispatches'] != 0: fail('terminal claim closure differs')
            if close_name not in listing or not stopped_at <= when(listing[close_name][1]) or not started <= when(p['created_at']) <= stopped_at: fail('terminal claim closure order differs')
            own = p
        claims.append((p['generation'], p['hash'], q['source_dispatches']))
    if own is None: fail('terminal controller claim absent')
    generations = sorted(g for g, _, _ in claims)
    if not generations or generations != list(range(1, max(generations) + 1)): fail('control generation gap')
    if any(sd != 0 for g, _, sd in claims if g >= own['generation']): fail('source dispatch at/after the terminal claim')
    # The last source lease: released (permanent LD_) by one CURRENT shard run; nothing beyond it.
    cid = BF.B.digest(['BOUNDED_PILOT_EXCLUSIVE', b.c['project_id']])[:16]
    leases = {int(z.group(2)): n for n in listing if (z := BF.CK.LEASE_RE.match(n)) and z.group(1) == cid}
    releases = {int(n[len('LD_' + cid + '_'):]): n for n in listing if n.startswith('LD_' + cid + '_') and n[len('LD_' + cid + '_'):].isdigit()}
    if not releases: fail('released lease absent')
    lease_generation = max(releases); released = b.tables.get_table(ds, releases[lease_generation])
    run_id = (released or ({},))[0].get('owner')
    if not isinstance(run_id, str) or not run_id.startswith('bf-'): fail('released lease owner malformed')
    if leases and max(leases) != lease_generation: fail('lease generation beyond the last release')
    if lease_generation in leases and listing[leases[lease_generation]][0].get('owner') != run_id: fail('lease owner differs from its release')
    entity, shard, seq, receipt, recon, cert, day_root = _current_cert(b, policy_hash, day, run_id, lease_generation)
    cert_hash = D.digest(cert)
    if 'BFR_' + cert_hash not in listing: fail('CURRENT certificate marker absent')
    t0 = when(listing['BFR_' + cert_hash][1])
    for k in SHARD_KINDS:
        if f'BFQ_{shard}_{seq:010d}_{k}' not in listing: fail('CURRENT shard fence absent')
    released_at = when(listing[releases[lease_generation]][1])
    if not released_at <= t0 < started or not F.stamp(cert['payload']['fact']['observed_at']) <= t0:
        fail('lease release / certificate / terminal wake order differs')
    # Nothing after the certificate: in tenant_locks only the control claims, this STOP's marker and the
    # replacement's exact release descriptors; in ref only the replacement's exact handoff/association
    # markers (the 8 PAUSED-stage markers of a release). Any other new object — a catch-up or FBO
    # diagnostic authority, a lease, a fence, a record — refuses.
    source = b.c['orchestration']['job']['env']['CONTROLLER_SOURCE_SHA']
    if source == old['source_sha']: fail('replacement release required')
    spec = {'BF_SPEC_' + source + '_' + m['hash'] + '_' + z for z in ('PAUSED', 'ENABLED')}
    for n, (_, created) in listing.items():
        if created is None: fail('object without creation time')
        if when(created) <= t0: continue
        if n.startswith(tuple(z + m['hash'] + '_' for z in CLAIM_PREFIXES)) or n == 'BFR_' + stop_hash or n in spec: continue
        fail('authority after the CURRENT certificate')
    for n, _labels, created in b.tables.list_tables(b.c['datasets']['ref']):
        if created is None: fail('object without creation time')
        if when(created) <= t0 or n == 'BFCURHANDOFF_' + source or n.endswith('_' + m['hash'] + '_' + source): continue
        fail('owner authority after the CURRENT certificate')
    ledger = f"`{b.c['project_id']}.{b.c['datasets']['tenant_ops']}.BACKFILL_CHECKPOINTS`"
    rows = b.select(f"SELECT backfill_id, entity FROM {ledger} WHERE updated_at>@t0 LIMIT 100", {'t0': ('TIMESTAMP', t0.isoformat())})
    if [(r.get('backfill_id'), r.get('entity')) for r in rows] != [(stop_hash[:16], 'orchestration')]: fail('ledger row after the CURRENT certificate')
    beyond = b.select(f"SELECT COUNT(*) AS n FROM {ledger} WHERE entity!='orchestration' AND lease_generation>@lease", {'lease': ('INT64', lease_generation)})
    if len(beyond) != 1 or beyond[0].get('n') not in (0, '0'): fail('pilot checkpoint beyond the last lease')
    # No source effect after the lease release (it precedes the certificate): Run executions and the ingestion journal.
    base, _ = BF.resources(b.c)
    for n in (*b.c['marketplaces']['ozon']['jobs'], 'tenant-control', O.JOB):
        xs = BF.execution_inventory(b.request, base + '/jobs/' + n)['executions']
        if any(not z.get('completionTime') for z in xs): fail('active execution')
        if n != O.JOB and any(F.stamp(z['createTime']) > released_at for z in xs): fail('source execution after the CURRENT lease release')
    if b.select(f"SELECT ingestion_run_id FROM `{b.journal}` WHERE started_at>@t0 LIMIT 1", {'t0': ('TIMESTAMP', released_at.isoformat())}): fail('source journal after the CURRENT lease release')
    ss = b.request('GET', BF.SCHED_API + '/' + base + '/jobs?pageSize=100')
    if len(ss.get('jobs', [])) != 4 or ss.get('nextPageToken') or any(z['state'] != 'PAUSED' for z in ss['jobs']): fail('owner freeze required')
    if [D.digest(z) for z in SA.unresolved(b, m, records, fresh=True)] != [stop_hash]: fail('other global STOP remains')
    accepted = sorted(SA.accepted_hashes(b, m, records, fresh=True))
    rp = RP.load(b, m)
    if rp is None: fail('owner priority authority absent')
    from tools.tenancy import full_fbo_diagnostic_closure as LQ
    new = LQ.qualified_release(b, m, source)
    if new.get('control_closure_recovery_version') != 1 or new.get('verification', {}).get('control_closure_recovery_adapter') != 'PASS': fail('replacement not qualified')
    facts = dict(controller=x, claims=sorted(claims), current=dict(day=day, entity=entity, shard=shard, sequence=seq, cert=cert_hash, certified_at=t0.isoformat()),
                 lease_generation=lease_generation, run_id=run_id, accepted=accepted, recent=D.digest(rp), stopped_at=stopped_at.isoformat())
    p = dict(version=1, type=TYPE, tenant=m['tenant'], project=m['project'], root=m['hash'], manifest_hash=D.digest(m), stop_hash=stop_hash, execution=execution,
             policy_hash=policy_hash, day=day, day_root=day_root, entity=entity, shard=shard, shard_sequence=seq, receipt_hash=D.digest(receipt),
             recon_hash=D.digest(recon), cert_hash=cert_hash, lease_generation=lease_generation, claim_generation=own['generation'], recent_policy_hash=D.digest(rp),
             old_source=old['source_sha'], old_image=old['image'], new_source=source, new_image=new['image'], new_implementation_hash=new['controller_implementation_hash'],
             runtime_image=new['runtime_image'], owner_decision_sha256=owner_decision_sha256, qualification_hash=qualification_hash, security_hash=security_hash,
             effect_hash=D.digest(facts), source_probe_hash=source_probe_hash, protected_root_hashes=sorted(D.digest(z) for z in records),
             predicates=dict.fromkeys(PREDICATES, True), verified_at=b.clock().isoformat(), source_budget=0, historical_cause=CAUSE)
    p = sealed(p); validate(p, b, m, records)
    return p, facts


def fresh(b, p):
    if not 0 <= (b.clock() - F.stamp(p['verified_at'])).total_seconds() <= FRESH_SECONDS: fail('fresh proof required')


def publish(b, p, m):
    header(p, m); fresh(b, p)
    again, _ = observe(b, m, p['stop_hash'], policy_hash=p['policy_hash'], day=p['day'],
                       **{k: p[k] for k in ('owner_decision_sha256', 'qualification_hash', 'security_hash', 'source_probe_hash')})
    if any(p[k] != again[k] for k in FIELDS - {'hash', 'verified_at'}): fail('fresh predicates changed')
    fresh(b, p)   # re-checked right before the first write: a slow re-observation publishes nothing
    for ds, n in ((b.c['datasets']['ref'], authority(p)), (b.c['datasets']['tenant_locks'], certificate(p))):
        if b.tables.get_table(ds, n) is None: b.tables.create_marker(ds, n, *value(p))
        if b.tables.get_table(ds, n) != value(p): fail('publication conflict')
    b.store.commit(m['hash'], KIND, 0, p, b.clock())
    if p not in load(b, m, b.store.history(m['hash'])): fail('independent acceptance differs')
    return p
