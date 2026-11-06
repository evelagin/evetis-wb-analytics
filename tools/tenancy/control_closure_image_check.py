"""Network-free in-image proof of the Heldi control-closure recovery (STOP eca0335a, 2026-11-05).

  * the exact certificate: a typed CONTROL_CLOSURE/CLOUD_METADATA STOP of a FAILED wake that did
    nothing but its claim is recoverable only while the last source lease belongs to a certified
    CURRENT shard, it was released before the certificate, and no lease, fence, record, ledger row,
    runtime execution or journal row exists after that certificate;
  * every other STOP payload (untyped, other stage/category) is refused; every listed fault is refused
    with zero effects;
  * publication is exactly 4 HTTP writes and becomes part of the one shared STOP acceptance set;
  * the release contract requires the finance catch-up release under it and the adapter PASS;
  * the existing recoveries (TG V1, CR, finance catch-up, S4 independence) stay registered.
Every patched seam is restored before returning.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import json
from tools.tenancy import control_closure_recovery as R, full_history as F, durable_plan as D, tenant_backfill as BF
from tools.tenancy import orchestration_contract as O, cloud_controller as C, controller_cadence as CC
from tools.tenancy.full_terminal_gate_image_check import patched, repo_with, rec
from pipelines.ozon.runtime import full_resume as FR

NOW = datetime(2026, 11, 6, 20, tzinfo=timezone.utc)
OLD = 'e82ba56bb51c0df095157b337e6146d7429d6269'
LEASE = 918; DAY = '2026-11-06'; POLICY = 'f9e2aa2519fb94a0ddac95ef6c2aa30d0b66b33dc155747fb99310bdb55a0aca'
JOB = 'projects/mpa-t-client-001/locations/europe-west1/jobs/tenant-backfill-controller'
EXEC = JOB + '/executions/tenant-backfill-controller-synth9'
RUN = 'bf-00000000-0000-4000-8000-000000000918'
OWNER = dict(owner_decision_sha256='4' * 64, qualification_hash='5' * 64, security_hash='6' * 64, source_probe_hash='8' * 64)
T = lambda h, m, s=0, us=0: datetime(2026, 11, 5, h, m, s, us, tzinfo=timezone.utc)


def new_release(old):
    new = deepcopy(old)
    new.update(source_sha='f' * 40, image=old['image'].split('@')[0] + '@sha256:' + 'f' * 64,
               controller_implementation_hash=O.implementation_hash(BF.REPO), control_closure_recovery_version=1)
    new['verification'] = dict(old['verification'], control_closure_recovery_adapter='PASS')
    return new


def fixture():
    m = FR.manifest()
    old = D.parse_tenant_json((BF.REPO / 'infra/tenant/releases/backfill' / f'{OLD}.json').read_text())
    new = new_release(old)
    c = deepcopy(BF.target('client_001')); c['marketplaces']['ozon']['runtime_image'] = old['runtime_image']
    settings = dict(release=new['source_sha'], root_hash=m['hash'], scheduler_state='PAUSED'); c['orchestration'] = O.block(c, settings, BF.REPO, new)
    hist = O.block(O.historical_contract(c, old, m), dict(release=OLD, root_hash=m['hash'], scheduler_state='ENABLED'), BF.REPO, old)
    started = T(21, 57, 2); claimed = T(21, 58, 40, 452447); stopped = T(22, 1, 39, 193000); closed = T(22, 1, 39, 448000)
    x = dict(name=EXEC, createTime=started.isoformat(), startTime=(started + timedelta(seconds=20)).isoformat(),
             completionTime=(closed + timedelta(seconds=6)).isoformat(), failedCount=1, taskCount=1,
             template=dict(timeout=hist['job']['timeout'], maxRetries=0, serviceAccount=hist['accounts']['controller']['email'],
                           containers=[dict(image=old['image'], command=['python', '-m', 'tools.tenancy.cloud_controller'],
                                            env=[dict(name=k, value=v) for k, v in hist['job']['env'].items()])]))
    stop = rec(m['hash'], 'STOPPED', 0, dict(reason=R.REASON, controller_execution=EXEC, diagnostic=dict(R.DIAGNOSTIC)))
    root = [rec(m['hash'], 'FULL_MANIFEST', 0, m), stop]
    day_root = D.digest(dict(current_policy=POLICY, moscow_day=DAY))
    doc = {'ack_hash': '3' * 64, 'runtime_plan': {'entity': 'stocks', 'observation_date': DAY}}
    shard = D.digest(dict(current_cycle=day_root, ack_hash=doc['ack_hash']))
    state = {'complete': True, 'sequence': 4604}
    receipt = dict(operation='projects/mpa-t-client-001/locations/europe-west1/operations/synthetic', run_id=RUN, lease_generation=LEASE, ack_hash=doc['ack_hash'])
    sh = [rec(shard, 'DISPATCH_INTENT', 1, dict(plan=doc, preparation={k: receipt[k] for k in ('run_id', 'lease_generation', 'ack_hash')}, current_parent_execution=JOB + '/executions/hvnr7')),
          rec(shard, 'DISPATCH_RECEIPT', 1, dict(plan=doc, receipt=receipt)),
          rec(shard, 'RECONCILED', 1, dict(checkpoint='DONE', source_complete=True, sequence=4604))]
    observed = T(21, 55, 20)
    fact = dict(entity='stocks', current_scope=POLICY, observed_at=observed.isoformat(), source_complete=True, persisted_reconciled=True,
                natural_keys_verified=True, duplicate_natural_keys=0, coverage_gap_days=0, source_unit_gaps=0, source_unit_duplicates=0,
                mismatches=0, finance_finality='NOT_APPLICABLE', certificate_hash='7' * 64, source_run_id=RUN)
    cert = rec(day_root, 'CURRENT_CERT', 1, dict(day=DAY, policy_hash=POLICY, ack_hash=doc['ack_hash'], shard=shard, state_hash=D.digest(state),
               source_sequence=4604, fact=fact, coverage={}, accepted_receipt_hash=D.digest(sh[1]), reconciliation_hash=D.digest(sh[2])))
    cycle = [rec(day_root, 'CURRENT_PLAN', 1, dict(day=DAY, policy_hash=POLICY, plan=doc, shard=shard)), cert]
    index = [rec(POLICY, 'CURRENT_DAY', 23, dict(day=DAY, root=day_root, policy_hash=POLICY))]
    roots = {m['hash']: root, POLICY: index, day_root: cycle, shard: sh}
    cid = BF.B.digest(['BOUNDED_PILOT_EXCLUSIVE', c['project_id']])[:16]; objects = {}

    refs = set()
    def put(n, labels, desc, created, ref=False):
        objects[n] = (labels, desc, created)
        if ref: refs.add(n)
    marks = {D.digest(index[0]): T(21, 9, 29), D.digest(cycle[0]): T(21, 42, 14), D.digest(sh[0]): T(21, 43, 22), D.digest(sh[1]): T(21, 43, 23),
             D.digest(sh[2]): T(21, 55, 10), D.digest(cert): T(21, 55, 24, 234000), D.digest(stop): stopped, D.digest(root[0]): T(1, 0)}
    for rs in roots.values():
        for r in rs:
            h = D.digest(r)
            put('BFR_' + h, {'root': r['root_hash'][:16], 'kind': r['kind'].lower()}, D.encoded({'root_hash': r['root_hash'], 'record_hash': h, 'kind': r['kind'], 'sequence': r['sequence']}), marks[h])
    for r in sh: put(f"BFQ_{shard}_{1:010d}_{r['kind']}", {'root': shard[:16], 'kind': r['kind'].lower()}, D.encoded({'record_hash': D.digest(r)}), marks[D.digest(r)])
    put(f'L_{cid}_{LEASE - 1:04d}', {'owner': 'bf-00000000-0000-4000-8000-000000000917', 'until': '0'}, '{}', T(21, 21, 30))
    put(f'LD_{cid}_{LEASE - 1:04d}', {'owner': 'bf-00000000-0000-4000-8000-000000000917'}, '{}', T(21, 31, 20))
    put(f'L_{cid}_{LEASE:04d}', {'owner': RUN, 'until': '1793922189'}, json.dumps({'mode': 'BOUNDED_PILOT', 'ack_hash': doc['ack_hash']}), T(21, 43, 21))
    put(f'LD_{cid}_{LEASE:04d}', {'owner': RUN}, json.dumps({'operation': receipt['operation'], 'ack_hash': doc['ack_hash']}), T(21, 54, 57, 811000))
    claims = ((JOB + '/executions/hvnr7', 'DISPATCHED', 1, T(21, 34, 42), T(21, 43, 23)), (JOB + '/executions/mgqsp', 'RECONCILED_BOUNDARY', 0, T(21, 46, 30), T(21, 55, 24, 500000)),
              (EXEC, 'STOPPED', 0, claimed, closed), (JOB + '/executions/next', 'STOPPED', 0, T(22, 1, 50), T(22, 2, 30)))
    for gen, (exe, status, dispatches, created, done) in enumerate(claims, 1):
        claim = CC.sealed(dict(version=1, tenant=m['tenant'], project=m['project'], root=m['hash'], policy_hash='a' * 64, source=OLD, image=old['image'],
                               execution=exe, generation=gen, created_at=created.isoformat()))
        put(CC.names(m['hash'], gen)[0], *CC.claim_value(claim), created)
        close = CC.sealed(dict(version=1, claim_hash=claim['hash'], status=status, source_dispatches=dispatches, closed_at=done.isoformat()))
        put(CC.names(m['hash'], gen)[1], {'kind': 'controller_close', 'root': m['hash'][:16]}, D.encoded(close), done)
    put(C.descriptor_name(new['source_sha'], m['hash'], 'PAUSED'), {}, D.encoded(dict(settings=settings, release=new)), NOW - timedelta(hours=1))
    put('BFCURHANDOFF_' + new['source_sha'], {}, '{}', NOW - timedelta(hours=1), ref=True)          # the replacement's ref markers
    put('BFRP_AUTH_' + m['hash'] + '_' + new['source_sha'], {}, '{}', NOW - timedelta(hours=1), ref=True)
    put('SELLER_BINDING', {}, '', T(1, 0), ref=True)                                                 # an older ref table
    base, _ = BF.resources(c)
    ctx = dict(sched='PAUSED', journal_after=[], ledger_after=[{'backfill_id': D.digest(stop)[:16], 'entity': 'orchestration'}], ledger_beyond=0, state=state,
               executions={JOB: [x, dict(name=JOB + '/executions/next', createTime=T(22, 0, 2).isoformat(), completionTime=T(22, 2, 31).isoformat(), succeededCount=1, taskCount=1)],
                           base + '/jobs/ozon-runtime-fast': [dict(name=base + '/jobs/ozon-runtime-fast/executions/vrhjn', createTime=T(21, 43, 21).isoformat(),
                                                                   completionTime=T(21, 52, 0).isoformat(), succeededCount=1)]},
               recent=dict(version=1, type='FULL_RECENT_PRIORITY', root=m['hash'], days=180))
    effects = []

    def request(method, url, body=None):
        if method != 'GET' or body is not None: raise AssertionError('owner recovery is read-only')
        if url == BF.RUN_API + '/' + EXEC: return deepcopy(x)
        if url.startswith(BF.RUN_API + '/' + base + '/jobs/') and '/executions?' in url:
            return {'executions': deepcopy(ctx['executions'].get(url[len(BF.RUN_API) + 1:].split('/executions?')[0], []))}
        if url == BF.SCHED_API + '/' + base + '/jobs?pageSize=100':
            return {'jobs': [{'name': base + '/jobs/' + n, 'state': ctx['sched']} for n in ('tenant-backfill-tick', 'ozon-daily', 'ozon-fast', 'ozon-weekly')]}
        raise AssertionError('unmocked cloud read')

    def select(sql, params):
        if 'BACKFILL_CHECKPOINTS' in sql and set(params) == {'t0'}: return deepcopy(ctx['ledger_after'])
        if 'BACKFILL_CHECKPOINTS' in sql and params == {'lease': ('INT64', LEASE)}: return [{'n': ctx['ledger_beyond']}]
        if 'OZON_INGESTION_RUNS' in sql and set(params) == {'t0'}: return deepcopy(ctx['journal_after'])
        raise AssertionError('unmocked query')

    def create(ds, n, labels, description):
        if n in objects: return False
        objects[n] = (labels, description, NOW); effects.append(('marker', n))
        if ds == c['datasets']['ref']: refs.add(n)
        return True

    def commit(root_hash, kind, seq, payload, now):
        r = rec(root_hash, kind, seq, deepcopy(payload)); target = roots.setdefault(root_hash, [])
        if r not in target: target.append(r); effects.append(('record', kind))
        return D.digest(r)
    tables = SimpleNamespace(get_table=lambda ds, n: objects[n][:2] if n in objects else None,
                             list_tables=lambda ds, **kw: [(n, v[0], v[2]) for n, v in objects.items() if (n in refs) == (ds == c['datasets']['ref'])], create_marker=create)
    store = SimpleNamespace(history=lambda r, **kw: deepcopy(roots.get(r, [])), commit=commit)
    b = SimpleNamespace(c=c, current_execution=None, clock=lambda: NOW, request=request, select=select, tables=tables, store=store,
                        state=lambda d: deepcopy(ctx['state']), journal='mpa-t-client-001.ozon_raw.OZON_INGESTION_RUNS')
    return SimpleNamespace(m=m, b=b, refs=refs, root=root, roots=roots, sh=sh, cycle=cycle, index=index, cert=cert, objects=objects, effects=effects, stop=stop, ctx=ctx,
                           new=new, old=old, shard=shard, cid=cid, doc=doc, started=started, stopped=stopped, x=x, day_root=day_root)


@contextmanager
def observing(f):
    """Seams proven by their own adapters: current GO/bindings and the owner RECENT authority."""
    from tools.tenancy import full_controller as H, recent_priority as RP
    with repo_with(f.new), patched(H, 'verify_authority', lambda b, m: None), patched(RP, 'load', lambda b, m, release=None: deepcopy(f.ctx['recent'])): yield


def observe(f, stop_hash=None, **kw):
    with observing(f): return R.observe(f.b, f.m, stop_hash or D.digest(f.stop), **dict(dict(policy_hash=POLICY, day=DAY, **OWNER), **kw))[0]


def certificate_check():
    f = fixture(); p = observe(f)
    assert (p['entity'], p['shard'], p['shard_sequence'], p['lease_generation'], p['claim_generation'], p['day']) == ('stocks', f.shard, 1, LEASE, 3, DAY)
    assert p['cert_hash'] == D.digest(f.cert) and p['old_source'] == OLD and p['new_source'] == f.new['source_sha'] and p['source_budget'] == 0 and not f.effects
    assert p['protected_root_hashes'] == sorted(D.digest(r) for r in f.root) and p['recent_policy_hash'] == D.digest(f.ctx['recent'])
    with repo_with(f.new): R.header(p, f.m); R.validate(p, f.b, f.m, f.root)
    for k in R.PREDICATES:
        q = deepcopy(p); q['predicates'][k] = None; q = R.sealed({k2: v for k2, v in q.items() if k2 != 'hash'})
        try: R.header(q, f.m)
        except BF.B.EvidenceError: pass
        else: raise AssertionError('unknown predicate accepted')
    for k in ('source_budget', 'historical_cause', 'day_root'):
        q = R.sealed(dict({k2: v for k2, v in p.items() if k2 != 'hash'}, **{k: 1 if k == 'source_budget' else ('X' if k == 'historical_cause' else '0' * 64)}))
        try: R.header(q, f.m)
        except BF.B.EvidenceError: pass
        else: raise AssertionError('certificate field accepted: ' + k)
    return 'PASS'


def negatives():
    def untyped(f): f.root[-1]['payload'].pop('diagnostic')
    def other_stage(f): f.root[-1]['payload']['diagnostic'] = dict(version=1, stage='SOURCE_DISPATCH_BOUNDARY', category='CLOUD_METADATA')
    def other_category(f): f.root[-1]['payload']['diagnostic'] = dict(version=1, stage='CONTROL_CLOSURE', category='EVIDENCE')
    def readback_ledger(f): f.root[-1]['payload']['diagnostic'] = dict(R.DIAGNOSTIC, readback=dict.fromkeys(('lease', 'checkpoint', 'intent', 'run', 'receipt'), 'ABSENT'))
    def controller_succeeded(f): f.x.update(failedCount=0, succeededCount=1)
    def controller_retried(f): f.x.update(retriedCount=1)
    def controller_running(f): f.x.pop('completionTime')
    def controller_foreign_root(f): f.x['template']['containers'][0]['env'] = [dict(e, value='0' * 64) if e['name'] == 'BACKFILL_ROOT_HASH' else e for e in f.x['template']['containers'][0]['env']]
    def own_closure(status, dispatches=0, created=None):
        def fault(f):
            name = CC.names(f.m['hash'], 3)[1]; claim = D.parse_tenant_json(f.objects[CC.names(f.m['hash'], 3)[0]][1])
            close = CC.sealed(dict(version=1, claim_hash=claim['hash'], status=status, source_dispatches=dispatches, closed_at=(created or f.stopped).isoformat()))
            f.objects[name] = (f.objects[name][0], D.encoded(close), created or f.objects[name][2])
        fault.__name__ = 'own_closure_%s_%d' % (status, dispatches); return fault
    def own_closure_before_stop(f): name = CC.names(f.m['hash'], 3)[1]; f.objects[name] = f.objects[name][:2] + (f.stopped - timedelta(seconds=1),)
    def later_claim_dispatched(f):
        name = CC.names(f.m['hash'], 4)[1]; claim = D.parse_tenant_json(f.objects[CC.names(f.m['hash'], 4)[0]][1])
        f.objects[name] = (f.objects[name][0], D.encoded(CC.sealed(dict(version=1, claim_hash=claim['hash'], status='DISPATCHED', source_dispatches=1, closed_at=NOW.isoformat()))), f.objects[name][2])
    def claim_unclosed(f): del f.objects[CC.names(f.m['hash'], 4)[1]]
    def claim_gap(f): del f.objects[CC.names(f.m['hash'], 2)[0]]; del f.objects[CC.names(f.m['hash'], 2)[1]]
    def lease_beyond(f): f.objects[f'L_{f.cid}_{LEASE + 1:04d}'] = ({'owner': 'bf-next', 'until': '0'}, '', T(21, 50))
    def release_beyond(f): f.objects[f'LD_{f.cid}_{LEASE + 1:04d}'] = ({'owner': 'bf-11111111-0000-4000-8000-000000000919'}, '', T(21, 50))
    def lease_owner_differs(f): f.objects[f'L_{f.cid}_{LEASE:04d}'] = ({'owner': 'bf-other', 'until': '0'}, '', T(21, 43, 21))
    def released_after_cert(f): k = f'LD_{f.cid}_{LEASE:04d}'; f.objects[k] = f.objects[k][:2] + (T(21, 56),)
    def cert_absent(f): f.cycle.remove(f.cert)
    def cert_binding(f): f.cert['payload']['accepted_receipt_hash'] = '0' * 64
    def cert_other_run(f): f.cert['payload']['fact']['source_run_id'] = 'bf-22222222-0000-4000-8000-000000000918'
    def cert_after_wake(f): k = 'BFR_' + D.digest(f.cert); f.objects[k] = f.objects[k][:2] + (f.started + timedelta(seconds=1),)
    def shard_next_intent(f): f.sh.append(rec(f.shard, 'DISPATCH_INTENT', 2, dict(plan=f.doc, preparation={})))
    def shard_not_done(f): f.sh[2]['payload']['checkpoint'] = 'RUNNING'
    def state_changed(f): f.ctx['state'] = dict(f.ctx['state'], sequence=4605)
    def day_index_differs(f): f.index[0]['payload']['root'] = '0' * 64
    def next_plan_record(f):
        r = rec(f.day_root, 'CURRENT_PLAN', 2, dict(day=DAY, policy_hash=POLICY, plan=dict(f.doc, ack_hash='9' * 64),
                                                    shard=D.digest(dict(current_cycle=f.day_root, ack_hash='9' * 64)))); f.cycle.append(r)
        f.objects['BFR_' + D.digest(r)] = ({}, '', T(21, 59))
    def fence_after_cert(f): f.objects['BFQ_' + '9' * 64 + '_0000000001_DISPATCH_INTENT'] = ({}, '', T(21, 59))
    def foreign_marker_after_cert(f): f.objects['BFDQUEUE_x'] = ({}, '', T(22, 30))
    def catchup_authority_after_cert(f): f.objects['BFFINCATCHUP_f9e2aa2519fb94a0_02'] = ({}, '', T(23, 0)); f.refs.add('BFFINCATCHUP_f9e2aa2519fb94a0_02')
    def fbo_token_after_cert(f): f.objects['BFDQUEUE_' + OLD] = ({}, '', T(23, 0)); f.refs.add('BFDQUEUE_' + OLD)
    def foreign_source_marker(f): f.objects['BF_SPEC_' + 'f' * 40 + '_' + '0' * 64 + '_PAUSED'] = ({}, '', T(23, 0))
    def undated_object(f): f.objects['BFR_' + '1' * 64] = ({}, '', None)
    def runtime_between_release_and_cert(f):
        k = JOB.rsplit('/jobs/', 1)[0] + '/jobs/ozon-runtime-daily'
        f.ctx['executions'][k] = [dict(name='x', createTime=T(21, 55, 5).isoformat(), completionTime=T(21, 58).isoformat())]
    def ledger_row_after_cert(f): f.ctx['ledger_after'].append({'backfill_id': 'abc', 'entity': 'stocks'})
    def ledger_stop_row_missing(f): f.ctx['ledger_after'] = []
    def pilot_beyond_lease(f): f.ctx['ledger_beyond'] = 1
    def runtime_after_cert(f):
        k = JOB.rsplit('/jobs/', 1)[0] + '/jobs/ozon-runtime-daily'
        f.ctx['executions'][k] = [dict(name='x', createTime=T(21, 56).isoformat(), completionTime=T(21, 58).isoformat())]
    def runtime_active(f): f.ctx['executions'][JOB][1].pop('completionTime')
    def journal_after(f): f.ctx['journal_after'] = [{'ingestion_run_id': RUN}]
    def scheduler_enabled(f): f.ctx['sched'] = 'ENABLED'
    def other_stop(f): f.root.append(rec(f.m['hash'], 'STOPPED', 0, dict(reason='X')))
    def stop_outside_execution(f): k = 'BFR_' + D.digest(f.stop); f.objects[k] = f.objects[k][:2] + (f.started - timedelta(seconds=1),)
    def unqualified_replacement(f):
        f.new.pop('control_closure_recovery_version'); f.new['verification'].pop('control_closure_recovery_adapter')
        name = C.descriptor_name(f.new['source_sha'], f.m['hash'], 'PAUSED'); settings = dict(release=f.new['source_sha'], root_hash=f.m['hash'], scheduler_state='PAUSED')
        f.objects[name] = ({}, D.encoded(dict(settings=settings, release=f.new)), f.objects[name][2])
    def descriptor_absent(f): del f.objects[C.descriptor_name(f.new['source_sha'], f.m['hash'], 'PAUSED')]
    return [untyped, other_stage, other_category, readback_ledger, controller_succeeded, controller_retried, controller_running, controller_foreign_root,
            own_closure('MONITORING'), own_closure('STOPPED', 1), own_closure_before_stop, later_claim_dispatched, claim_unclosed, claim_gap,
            lease_beyond, release_beyond, lease_owner_differs, released_after_cert, cert_absent, cert_binding, cert_other_run, cert_after_wake,
            shard_next_intent, shard_not_done, state_changed, day_index_differs, next_plan_record, fence_after_cert, foreign_marker_after_cert,
            catchup_authority_after_cert, fbo_token_after_cert, foreign_source_marker, undated_object, runtime_between_release_and_cert,
            ledger_row_after_cert, ledger_stop_row_missing, pilot_beyond_lease, runtime_after_cert, runtime_active, journal_after, scheduler_enabled,
            other_stop, stop_outside_execution, unqualified_replacement, descriptor_absent]


def negative_check():
    for fault in negatives():
        f = fixture(); fault(f)
        try: observe(f)
        except BF.B.EvidenceError: pass
        else: raise AssertionError('accepted: ' + fault.__name__)
        assert not f.effects, fault.__name__
    f = fixture()
    for kw in (dict(day='2026-11-05'), dict(policy_hash='1' * 64)):
        try: observe(f, **kw)
        except BF.B.EvidenceError: pass
        else: raise AssertionError('foreign CURRENT scope accepted: %r' % kw)
    return 'PASS'


def publish_check():
    from tools.tenancy import stop_acceptance as SA
    f = fixture(); p = observe(f); before = deepcopy(f.root)
    with observing(f): q = R.publish(f.b, p, f.m)
    assert q == p and f.effects == [('marker', R.authority(p)), ('marker', R.certificate(p)), ('record', R.KIND)]
    assert f.root[:len(before)] == before and len(f.root) == len(before) + 1 and f.root[-1]['kind'] == R.KIND and f.root[-1]['sequence'] == 0
    with repo_with(f.new):
        assert R.load(f.b, f.m, f.root) == [p]
        assert SA.accepted_hashes(f.b, f.m, f.root, fresh=True) == {p['stop_hash']} and not SA.unresolved(f.b, f.m, f.root, fresh=True)
    # A marker of the published certificate is part of nothing else: a second observation is refused (STOP resolved).
    try: observe(f)
    except BF.B.EvidenceError: pass
    else: raise AssertionError('resolved STOP observed again')
    # Freshness expiring between re-observation and the first write publishes nothing.
    g = fixture(); p2 = observe(g); ticks = [NOW, NOW + timedelta(seconds=R.FRESH_SECONDS + 1)]
    g.b.clock = lambda: ticks[0] if len(ticks) > 1 and ticks.pop(0) else ticks[0]
    try:
        with observing(g): R.publish(g.b, p2, g.m)
    except BF.B.EvidenceError as e: assert 'fresh proof required' in str(e) and not g.effects
    else: raise AssertionError('expired proof published')
    # A changed fact between observation and publication publishes nothing.
    g = fixture(); p2 = observe(g); g.ctx['recent'] = dict(g.ctx['recent'], days=90)
    try:
        with observing(g): R.publish(g.b, p2, g.m)
    except BF.B.EvidenceError as e: assert 'fresh predicates changed' in str(e) and not g.effects
    else: raise AssertionError('changed RECENT authority published')
    # Exact HTTP write set through the real durable store: 2 markers + commit (checkpoint row + BFR_) = 4.
    h = fixture(); p3 = observe(h); writes = []

    class Meta:
        def list_tables(self, ds, **kw): return [(n, v[0], v[2]) for n, v in h.objects.items() if (n in h.refs) == (ds == 'ref')]
        def get_table(self, ds, n): return h.objects[n][:2] if n in h.objects else None
        def create_marker(self, ds, n, labels, description):
            writes.append((ds, n))
            if n in h.objects: return False
            h.objects[n] = (labels, description, NOW)
            if ds == 'ref': h.refs.add(n)
            return True
    rows = [dict(plan_hash=r['root_hash'], backfill_id=D.digest(r)[:16], evidence_json=D.encoded(r)) for rs in h.roots.values() for r in rs]
    def write_record(row): writes.append(('tenant_ops', 'BACKFILL_CHECKPOINTS')); rows.append(row)
    def select(c_, sql, params): return [{'evidence_json': r['evidence_json']} for r in rows if r['plan_hash'] == params['root'][1] and ('record' not in params or r['backfill_id'] == params['record'][1])]
    h.b.tables = Meta(); h.b.store = D.DurableRecords(h.b.c, Meta(), select, write_record)
    with observing(h): R.publish(h.b, p3, h.m)
    assert writes == R.publication_writes(h.b, p3, h.m) and len(writes) == 4, writes
    big = R.sealed(dict({k: v for k, v in p3.items() if k != 'hash'}, protected_root_hashes=sorted(D.digest({'n': i}) for i in range(1000))))
    assert len(D.encoded(big).encode()) > 16384 and all(len(R.value(q)[1].encode()) < 1024 for q in (p3, big))
    return 'PASS'


def release_check():
    """The release contract: control_closure_recovery_version=1 only on top of the finance catch-up release, adapter PASS."""
    m = FR.manifest(); old = D.parse_tenant_json((BF.REPO / 'infra/tenant/releases/backfill' / f'{OLD}.json').read_text())
    c = deepcopy(BF.target('client_001')); c['marketplaces']['ozon']['runtime_image'] = old['runtime_image']
    new = new_release(old); settings = dict(release=new['source_sha'], root_hash=m['hash'], scheduler_state='PAUSED')
    assert O.block(c, settings, BF.REPO, new)['job']['env']['CONTROLLER_SOURCE_SHA'] == new['source_sha']
    bad = [dict(new, control_closure_recovery_version=2), dict(new, verification={k: v for k, v in new['verification'].items() if k != 'control_closure_recovery_adapter'})]
    no_catchup = deepcopy(new); no_catchup.pop('finance_catchup_version'); no_catchup['verification'].pop('finance_catchup_adapter'); bad.append(no_catchup)
    for r in bad:
        try: O.block(c, settings, BF.REPO, r)
        except ValueError: pass
        else: raise AssertionError('unqualified control-closure release accepted')
    # The installed release (e82ba56) stays readable exactly as registered.
    assert O.block(c, dict(release=OLD, root_hash=m['hash'], scheduler_state='ENABLED'), BF.REPO, old)['job']['image'] == old['image']
    return 'PASS'


def acceptance_check():
    from tools.tenancy import stop_acceptance as SA, full_terminal_gate_recovery as TG, full_controller_closure_recovery as CR, finance_catchup as FC
    with patched(R, 'load', lambda b, m, r: [{'stop_hash': '9' * 64}]):
        assert SA.accepted_hashes(SimpleNamespace(), {}, [{'kind': R.KIND}]) == {'9' * 64}
    assert R.KIND in D.KINDS and TG.KIND in D.KINDS and CR.KIND in D.KINDS and FC.KINDS <= D.KINDS
    for n in ('control_closure_recovery.py', 'control_closure_image_check.py', 'full_terminal_gate_recovery.py', 'full_controller_closure_recovery.py',
              'finance_catchup.py', 'current_independence.py'):
        assert n in O.SOURCE_FILES, n
    return 'PASS'


def check():
    for name, fn in (('certificate', certificate_check), ('negatives', negative_check), ('publish', publish_check), ('release', release_check), ('acceptance', acceptance_check)):
        assert fn() == 'PASS', name
    return {'control_closure_recovery_adapter': 'PASS'}


if __name__ == '__main__':
    print(json.dumps(check(), sort_keys=True))
