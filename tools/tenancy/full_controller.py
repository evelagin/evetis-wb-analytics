"""Opt-in full-history adapter over the qualified bounded canonical coordinator.

Owner-only ref.BFGO is mandatory; neither cloud append nor a code/manifest can
invent authorization. Each leaf has its own immutable dispatch/reconcile shard.
One wake emits at most one Run POST. Ambiguous report/Run submissions never retry.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from tools.tenancy.validation import parse_tenant_json
import re
from tools.tenancy import full_history as F, cloud_controller as C
from tools.tenancy import durable_plan as D, tenant_backfill as BF
from tools.tenancy import pre_source_recovery as R
from tools.tenancy.controller_diagnostics import gate, GateFailure
from tools.tenancy import recent_priority as RP

KINDS = frozenset({'FULL_MANIFEST', 'CHUNK_PLAN', 'CHUNK_COMPLETE', 'FULL_COMPLETE', 'T5_PARENT_COMPLETE'})
# Matches the last existing Seller transport BACKOFF interval. Controller wakes
# remain bounded; explicit 429 never becomes a penalized permanent read failure.
READ_RETRY_COOLDOWN_SECONDS = 48


def only_manifest(records):
    values = [r for r in records if r['kind'] == 'FULL_MANIFEST']
    if len(values) != 1 or values[0]['sequence'] != 0:
        raise BF.B.EvidenceError('full manifest missing/ambiguous')
    manifest = values[0]['payload']; F.validate_manifest(manifest)
    return manifest


def completed(records, manifest):
    done = {}
    plans = {}
    for record in records:
        if record['kind'] == 'CHUNK_PLAN':
            value = record['payload']
            plans[(value['index'], value['plan']['ack_hash'])] = value
    for r in records:
        if r['kind'] != 'CHUNK_COMPLETE':
            continue
        p = r['payload']; index = p['index']
        if type(index) is not int or not 0 <= index < len(manifest['programs']) or r['sequence'] != index:
            raise BF.B.EvidenceError('full completion index/scope differs')
        if p.get('source_complete') is not True or p.get('persisted_reconciled') is not True:
            raise BF.B.EvidenceError('partial full source leaf cannot complete')
        required = {'index','shard_root','ack_hash','state_hash','sequence',
                    'dispatch_attempts','source_complete','persisted_reconciled','coverage','completed_at','reused_qualification'}
        if (set(p) != required or type(p['sequence']) is not int or p['sequence']<1
                or type(p['dispatch_attempts']) is not int or p['dispatch_attempts']<0):
            raise BF.B.EvidenceError('full completion proof schema differs')
        reuse=p['reused_qualification']
        expected=manifest['programs'][index].get('accepted_qualification_plan')
        if expected:
            if (expected!=BF.QF.SKU or not isinstance(reuse,dict) or set(reuse)!={'root','plan_id','terminal_reconciliation_hash'}
                    or reuse['root']!=BF.QF.ROOT or reuse['plan_id']!=expected or p['dispatch_attempts']!=0):
                raise BF.B.EvidenceError('accepted source day cannot be relabeled or rerun')
            D.check_hash(reuse['terminal_reconciliation_hash'])
        elif reuse is not None or not p['dispatch_attempts']:
            raise BF.B.EvidenceError('new full source completion lacks dispatch evidence')
        for k in ('shard_root','ack_hash','state_hash'):D.check_hash(p[k])
        F.stamp(p['completed_at'])
        plan = plans.get((index, p['ack_hash']))
        if not plan or plan['shard_root'] != p['shard_root'] or p['shard_root'] != F.shard_root(manifest,index,plan['plan']):
            raise BF.B.EvidenceError('full completion lacks exact published child plan')
        coverage = p['coverage']
        if (not isinstance(coverage,dict) or coverage.get('source_sequence') != p['sequence']
                or not isinstance(coverage.get('readback'),list)):
            raise BF.B.EvidenceError('full completion/source coverage linkage differs')
        if index in done and done[index] != p:
            raise BF.B.EvidenceError('conflicting full source completion')
        done[index] = p
    return done


def verify_authority(backend, manifest):
    c = F.validate_manifest(manifest)
    if manifest['tenant'] != backend.c['tenant_id'] or manifest['project'] != backend.c['project_id']:
        raise BF.B.EvidenceError('foreign full authority')
    # The immutable controller is built before this deployment profile can be
    # registered. Its durable immutable descriptor supplies deployment settings;
    # the packaged registry still supplies tenant/project/runtime authority.
    # Never require future full-root registration inside an older source image.
    expected = backend.c.get('orchestration')
    if not expected:
        raise BF.B.EvidenceError('full canonical deployment contract absent')
    env = expected['job']['env']
    active_source=env['CONTROLLER_SOURCE_SHA']
    settings = {'release': active_source,
                'root_hash': manifest['hash'],
                'scheduler_state': env.get('HISTORICAL_SCHEDULER_STATE')}
    marker = backend.tables.get_table(c['datasets']['tenant_locks'], C.descriptor_name(
        settings['release'], settings['root_hash'], settings['scheduler_state']))
    descriptor = parse_tenant_json(marker[1]) if marker else {}
    release = descriptor.get('release', {})
    if set(descriptor) != {'settings', 'release'} or descriptor['settings'] != settings:
        raise BF.B.EvidenceError('full deployment descriptor settings differ')
    if release.get('schema_version') not in {2,3,4} or release.get('verification',{}).get('full_history_adapter')!='PASS':
        raise BF.B.EvidenceError('exact controller image has no full-adapter qualification')
    canonical = C.O.block(c, settings, BF.REPO, release=release)
    if canonical != expected:
        raise BF.B.EvidenceError('full canonical deployment contract differs')
    c = dict(c, orchestration=canonical)
    if env['BACKFILL_ROOT_HASH'] != manifest['hash']:
        raise BF.B.EvidenceError('full descriptor/root/source differs')
    handoff=c['marketplaces']['ozon']['runtime_image'] != manifest['runtime_image']
    if handoff:
        from tools.tenancy import full_runtime_handoff as FH
        FH.policy(backend,manifest,release)
    if active_source==manifest['controller_source_sha']:
        if expected['job']['image']!=manifest['controller_image'] or manifest['controller_implementation_hash']!=C.O.implementation_hash(BF.REPO):
            raise BF.B.EvidenceError('full packaged controller implementation differs')
    else:
        # Owner-only provenance handoff retains the exact immutable semantic
        # manifest, GO and every existing source shard. It exempts no STOP.
        from tools.tenancy import full_leaf_recovery as FL
        FL.policy(backend,manifest,release)
        if release['image']!=expected['job']['image']:
            raise BF.B.EvidenceError('replacement controller image differs')
    from tools.tenancy import platform as PL
    runtimes=[parse_tenant_json(f.read_text()) for f in (BF.REPO/PL.RUNTIME_RELEASES_DIR/'ozon').glob('*.json')]
    runtime_image=c['marketplaces']['ozon']['runtime_image'] if handoff else manifest['runtime_image']
    runtimes=[r for r in runtimes if r.get('image')==runtime_image]
    if (len(runtimes)!=1 or (not handoff and runtimes[0]['source']['commit']!=manifest['runtime_source_sha'])
            or runtimes[0]['verification']['built_artifact'].get('full_current_snapshot_adapters')!='PASS'
            or (not handoff and manifest['runtime_implementation_hash']!=BF.B.implementation_hash())):
        raise BF.B.EvidenceError('exact runtime image has no full domain qualification')
    results = {g:'PASS' for g in F.GATES}
    if backend.tables.get_table(c['datasets']['ref'], F.go_marker(manifest)) != F.go_value(manifest, results):
        raise BF.B.EvidenceError('owner-only full GO absent/corrupt')
    chain, decisions, ledger, rows, hold = BF.TL.read_state(c, backend.tables)
    import lifecycle_core as L
    if hold or L.audit_history(chain, decisions) or L.current_state(chain) != L.BACKFILLING:
        raise BF.B.EvidenceError('full canonical lifecycle/hold gate denied')
    ph, chunks = BF.CK.latest_plan(ledger, since=BF.TL.cycle_start(chain))
    if ph != manifest['t5_plan_hash'] or chunks != F.canonical_chunks(manifest):
        raise BF.B.EvidenceError('full canonical T5 approved plan differs')
    events = [e for e in L.ordered(chain) if e.get('to_state') == L.BACKFILLING]
    if not events or decisions.get(events[-1]['seq'], {}).get('plan_hash') != ph:
        raise BF.B.EvidenceError('full exact operator plan approval absent')
    with gate('BINDING'):
        bindings, credentials = BF.TL.operator_binding(c, backend.tables, backend.clock(), ['ads_sku_daily'])
        if bindings != {'seller':'BOUND','performance':'BOUND'} or {k:v['status'] for k,v in credentials.items()} != {'seller':'PASS','performance':'PASS'}:
            raise BF.B.EvidenceError('full live binding/credential gate denied')
    backend.binding_status = bindings
    backend.canonical_ledger = ledger
    verify_capabilities(backend,c)
    return c


def verify_capabilities(backend,c):
    required = {('seller', 'stocks'), ('seller', 'supplies')}
    latest = {}
    for cap in backend.tables.rows(c['datasets']['tenant_ops'], 'CAPABILITY_PROFILE'):
        key = (cap.get('api'), cap.get('capability'))
        if key in required:
            at=C.timestamp(cap['discovered_at'])
            if at>backend.clock():raise BF.B.EvidenceError('full capability evidence in future')
            if key not in latest or at>latest[key][0]:latest[key]=(at,cap)
    if set(latest) != required or any(v[1].get('status') != 'AVAILABLE' for v in latest.values()):
        raise BF.B.EvidenceError('full corrected capability regression')


class ProjectedTables:
    """Lifecycle needs typed ledger columns, not every large source-state JSON.

    Committed orchestration/shard records still use DurableRecords' exact hash
    reader. Do not weaken that authority or synthesize lifecycle/business rows.
    """
    def __init__(self, backend, tables):
        self.backend, self.original = backend, tables

    def __getattr__(self, name):return getattr(self.original, name)

    def rows(self, dataset, table):
        b = self.backend
        if dataset != b.c['datasets']['tenant_ops'] or table != 'BACKFILL_CHECKPOINTS':
            yield from self.original.rows(dataset, table)
            return
        fields = [f['name'] for f in parse_tenant_json((BF.REPO/'tools/tenancy/schema/tenant_ops/BACKFILL_CHECKPOINTS.json').read_text())['schema'] if f['name'] != 'evidence_json']
        # Preserve the factual lease-mode discriminator. Dropping it could
        # reset generation after old lease tables expire, reusing an old LD.
        mode = "CASE WHEN JSON_VALUE(evidence_json,'$.mode')='BOUNDED_PILOT' THEN '{\"mode\":\"BOUNDED_PILOT\"}' ELSE NULL END AS evidence_json"
        values = b.select(f"SELECT {','.join(fields)},{mode} FROM `{b.c['project_id']}.{dataset}.{table}` WHERE entity != 'orchestration'", {})
        for value in values:
            value = dict(value)
            for k in ('updated_at','lease_until','started_at','completed_at'):
                if value.get(k) is not None:value[k] = C.timestamp(value[k]).isoformat()
            yield value


class Backend(C.Backend):
    def __init__(self, base, manifest):
        self.__dict__.update(base.__dict__)
        self.manifest = manifest
        self.index = None
        if hasattr(base, 'access'):
            self.full_tables = ProjectedTables(self, base.tables)

    @property
    def tables(self):
        return self.full_tables

    def authorize_full_leaf(self, doc):
        verify_authority(self, self.manifest)
        if self.index is None:
            raise BF.B.EvidenceError('full leaf index not reconstructed')
        F.validate_leaf(self.manifest, self.index, doc, self.clock().astimezone(BF.B.MSK).date())
        return True

    def preflight(self, leaf, *, owner_observation=False):
        if owner_observation:
            raise BF.B.EvidenceError('full source adapter is cloud execution only')
        verify_authority(self, self.manifest)
        if leaf.get('hash') != self.manifest['hash'] or len(leaf.get('plans', [])) != 1:
            raise BF.B.EvidenceError('one exact full leaf per preflight')
        doc = leaf['plans'][0]
        self.active = False
        # A source-free owner observer has no cloud execution identity and must
        # reject every active Job. Only an identified cloud wake may observe
        # active work under the existing reconciliation/overlap fences.
        BF.preflight(BF.validate_plan(doc, doc['ack_hash']), doc, self.clock(), backend=self,
                     allow_active=self.current_execution is not None)
        for ds in ('tenant_ops','tenant_locks'):
            table = self.request('GET', f"{BF.TT.BQ}/projects/{self.c['project_id']}/datasets/{self.c['datasets'][ds]}?datasetView=METADATA")
            if table.get('defaultTableExpirationMs') or table.get('defaultPartitionExpirationMs'):
                raise BF.B.EvidenceError('full authoritative evidence retention unsafe')
        checkpoint = self.request('GET', f"{BF.TT.BQ}/projects/{self.c['project_id']}/datasets/{self.c['datasets']['tenant_ops']}/tables/BACKFILL_CHECKPOINTS")
        if checkpoint.get('expirationTime') or (checkpoint.get('timePartitioning') or {}).get('expirationMs'):
            raise BF.B.EvidenceError('full checkpoint evidence expires')
        for name,labels,created,expiry in self.tables.list_tables(self.c['datasets']['tenant_locks'],with_expiry=True):
            if name.startswith(('BFR_','BFQ_','BF_SPEC_','BFFLR_','BFCF_')) and expiry:
                raise BF.B.EvidenceError('full authoritative marker expires')

    def quota(self, leaf, now):
        # Exact prior owner recovery is revalidated, never silently ignored.
        # An old failure outside the rolling ledger must not make a long full
        # traversal depend on Cloud Run retaining that historical Execution.
        # No exception is applied when there are no current unknown aggregates.
        unknown = self.select(f"SELECT COUNT(*) AS n FROM `{self.journal}` WHERE started_at>=@since AND entity='ads_sku_daily' AND backfill_plan_id IS NULL",
                              {'since':('TIMESTAMP',(now-timedelta(hours=24)).isoformat())})
        if len(unknown) != 1:raise BF.B.EvidenceError('full rolling aggregate accounting ambiguous')
        self.verified_pre_source_failures = R.load(self,self.store.history(BF.QF.ROOT),BF.QF.ROOT) if unknown[0]['n'] else []
        from tools.tenancy import full_cooldown_recovery as FC
        if unknown[0]['n']:
            records=self.store.history(self.manifest['hash'])
            failed=FC.load(self,self.manifest,records) if any(r['kind']==FC.KIND for r in records) else []
            self.verified_cooldown_failures=failed
        doc = leaf['plans'][0]
        q = super().quota(leaf, now)
        if q['status']!='STOPPED':
            # One Performance source class across frozen leaves. Moving the
            # priority queue must not escape a different leaf's active cooldown.
            records=self.store.history(self.manifest['hash'])
            plans=reconstruct_plans(records,self.manifest,now)
            docs={v['plan']['runtime_plan']['plan_id']:v['plan'] for v in plans.values()
                  if v['plan']['runtime_plan']['entity']=='ads_sku_daily'}
            if docs:
                rows=self.select(f"SELECT backfill_plan_id AS plan_id,backfill_sequence,evidence_json FROM `{self.journal}` WHERE entity='ads_sku_daily' AND status='OK' AND backfill_plan_id IN UNNEST(JSON_VALUE_ARRAY(@plans)) AND backfill_sequence IS NOT NULL QUALIFY backfill_sequence=MAX(backfill_sequence) OVER(PARTITION BY backfill_plan_id) LIMIT 1000",{'plans':('STRING',D.encoded(sorted(docs)))})
                if len(rows)>=1000:raise BF.B.EvidenceError('Performance source state inventory exceeded')
                states={}
                for r in rows:
                    proof=parse_tenant_json(r['evidence_json']);rp=docs[r['plan_id']]['runtime_plan']
                    if proof.get('plan')!=rp:raise BF.B.EvidenceError('Performance class source scope differs')
                    s=BF.B.validate(rp,proof['state'])
                    if s['sequence']!=r['backfill_sequence'] or (r['plan_id'] in states and states[r['plan_id']]!=s):raise BF.B.EvidenceError('Performance class source checkpoint conflicts')
                    states[r['plan_id']]=s
                rates=[];reports=[]
                for pid,s in states.items():
                    report=s['progress'].get('report')
                    if report:
                        if report.get('phase')=='INTENT':raise BF.B.EvidenceError('REPORT_SUBMISSION_AMBIGUOUS')
                        if report.get('phase')!='POLL':raise BF.B.EvidenceError('unknown Performance async state')
                        reports.append(pid)
                    eligible=s['progress'].get('rate_limit',{}).get('eligible_at')
                    if eligible and C.timestamp(eligible)>now:rates.append(eligible)
                if len(reports)>1:raise BF.B.EvidenceError('multiple Performance async scopes')
                if doc['runtime_plan']['entity'].startswith('ads_'):
                    if rates:return {'status':'WAITING','allowance':0,'eligible_at':max(rates,key=C.timestamp),'basis':'SOURCE_COOLDOWN'}
                    if reports and reports!=[doc['runtime_plan']['plan_id']]:
                        return {'status':'WAITING','allowance':0,'eligible_at':(now+timedelta(minutes=3)).isoformat(),'basis':'EXISTING_ASYNC_SCOPE'}
        if doc['runtime_plan']['entity'] != 'ads_sku_daily' and q['status'] == 'WAITING':
            return {'status':'ELIGIBLE','allowance':0,'performance_quota':q,
                    'basis':'NO_NEW_PERFORMANCE_REPORT_IN_THIS_SOURCE_SCOPE'}
        return q

    def reconcile(self, payload):
        doc = payload['plan']; receipt = payload['receipt']
        with gate('RUNTIME_TERMINAL'):
            operation = self.request('GET', f"{BF.RUN_API}/{receipt['operation']}")
            if not operation.get('done'):return None
            if operation.get('error'):return self.reconcile_failed(doc, receipt, operation)
        with gate('CHECKPOINT'):
            result = BF.reconcile(doc, doc['ack_hash'], receipt, backend=self)
        with gate('PERSISTED_SOURCE_RECONCILIATION'):
            result['coverage_readback'] = BF.verify_coverage(doc, doc['ack_hash'], backend=self)
            state = self.state(doc)
        result['source_complete'] = state['complete']
        result['persisted_reconciled'] = True
        result['state_hash'] = D.digest(state)
        if doc['runtime_plan']['entity'] == 'catalog' and state['complete']:
            self.store.commit(self.manifest['hash'], 'SNAPSHOT_CERT', 0, {
                'day':doc['runtime_plan']['observation_date'], 'plan_hash':doc['ack_hash'],
                'coverage':result['coverage_readback']}, self.clock())
        return result

    def reconcile_failed(self, doc, receipt, operation):
        p = doc['runtime_plan']; state = self.state(doc)
        if (state['progress'].get('report') or {}).get('phase') == 'INTENT':
            raise BF.B.EvidenceError('ambiguous report intent; never repeat source POST')
        name = operation.get('metadata', {}).get('name', '')
        base, jobs = BF.resources(self.c)
        job = next(n for n, j in jobs.items() if p['entity'] in j['entities'])
        if not name.startswith(base+'/jobs/'+job+'/executions/'):
            raise BF.B.EvidenceError('failed runtime provenance absent')
        execution = self.request('GET', BF.RUN_API+'/'+name)
        t = execution['template']; container = t['containers'][0]
        env = {v['name']:v.get('value') for v in container.get('env', [])}
        expected = {'INGESTION_RUN_ID':receipt['run_id'],'BACKFILL_TARGET_PROJECT':p['project'],
            'ENTITIES':p['entity'],'SINCE':p['from'],'UNTIL':p['to'],'BACKFILL_MODE':BF.B.VERSION,
            'BACKFILL_GENERATION':p['generation'],'BACKFILL_ORIGIN':p['origin'],
            'TENANT_BINDING_REQUIRED':'1','STRICT_PAGE_CAPS':'1',
            'BACKFILL_MAX_REQUESTS':str(doc['max_requests']),'BACKFILL_MAX_UNITS':str(doc['max_units'])}
        if p['window_days'] != 1:expected['BACKFILL_WINDOW_DAYS'] = str(p['window_days'])
        expected.update(BF.continuation_overrides(doc))
        if (len(t['containers']) != 1 or execution.get('taskCount',1) != 1
                or len(env) != len(container.get('env',[]))
                or any(env.get(k) != v for k,v in expected.items())
                or not execution.get('completionTime') or execution.get('failedCount') != 1
                or execution.get('succeededCount',0) or execution.get('cancelledCount',0)
                or t['serviceAccount'] != f"{self.c['marketplaces']['ozon']['service_accounts']['runtime']}@{p['project']}.iam.gserviceaccount.com"
                or container['image'] != BF.execution_image(doc,self.c)
                or env.get('INGESTION_RUN_ID') != receipt['run_id']
                or env.get('TENANT_BINDING_REQUIRED') != '1' or env.get('STRICT_PAGE_CAPS') != '1'):
            raise BF.B.EvidenceError('failed full runtime exact provenance unproven')
        rows = self.select(f"SELECT DISTINCT error_message FROM `{self.journal}` WHERE ingestion_run_id=@run AND entity=@entity AND status='FAILED' LIMIT 2",
                           {'run':('STRING',receipt['run_id']), 'entity':('STRING',p['entity'])})
        if len(rows) != 1:raise BF.B.EvidenceError('failed source category ambiguous')
        message = rows[0]['error_message'] or ''
        rollover = (p.get('observation_date') is not None
                    and p['observation_date'] < str(self.clock().astimezone(BF.B.MSK).date())
                    and message in {repr(BF.B.EvidenceError(x)) for x in (
                        'snapshot observation day stale; fresh dated scope required',
                        'catalog snapshot date changed; fresh generation required')})
        known=re.fullmatch(r"EvidenceError\(['\"]SOURCE_HTTP_(429|500|502|503|504|NET_ERROR): /[A-Za-z0-9_./-]+['\"]\)",message)
        if not rollover and not known:
            raise BF.B.EvidenceError('failed source category needs forensic review; no blind retry')
        # No diagnostic/body is copied. A known read failure may replay safe MERGE
        # units, preserving the exact latest durable source state and failed receipt.
        self.authorize_full_leaf(doc)
        cid = BF.B.digest(['BOUNDED_PILOT_EXCLUSIVE', self.c['project_id']])[:16]
        lease = self.tables.get_table(self.c['datasets']['tenant_locks'], BF.CK.lease_name(cid,receipt['lease_generation']))
        if not lease or lease[0].get('owner') != receipt['run_id'] or parse_tenant_json(lease[1])['ack_hash'] != doc['ack_hash']:
            raise BF.B.EvidenceError('failed full terminal lease attribution differs')
        now = self.clock()
        row = BF.checkpoint(doc,receipt['run_id'],'FAILED',receipt['lease_generation'],now)
        quota=known is not None and known.group(1)=='429'
        category = 'SNAPSHOT_DAY_ROLLOVER' if rollover else ('QUOTA' if quota else 'KNOWN_SOURCE_READ')
        row['error_code'] = category
        row['evidence_json'] = D.encoded({'mode':'BOUNDED_PILOT','source_state_hash':D.digest(state),
                                        'failed_operation':receipt['operation'], 'retry_category':category})
        self.tables.append(self.c['datasets']['tenant_ops'],'BACKFILL_CHECKPOINTS',[row])
        marker = f"LD_{cid}_{receipt['lease_generation']:04d}"
        desc = D.encoded({'operation':receipt['operation'],'ack_hash':doc['ack_hash'],'terminal':'FAILED_READ'})
        if not self.tables.create_marker(self.c['datasets']['tenant_locks'],marker,{'owner':receipt['run_id']},desc):
            if self.tables.get_table(self.c['datasets']['tenant_locks'],marker) != ({'owner':receipt['run_id']},desc):
                raise BF.B.EvidenceError('failed full lease release conflict')
        return {'failed':True,'checkpoint':'FAILED','source_complete':False,
                'persisted_reconciled':False,'state_hash':D.digest(state), 'retry_category':category,
                'eligible_at':(now+timedelta(seconds=READ_RETRY_COOLDOWN_SECONDS)).isoformat()}


def _dispatch(backend, manifest, index, doc, root, sequence):
    from pipelines.ozon.runtime import full_resume as FR
    if FR.recognized(manifest):
        backend.full_dispatch_authority={'root':manifest['hash'],'index':index,'shard':root,'receipt_sequence':sequence,'plan_id':doc['runtime_plan']['plan_id']}
    prepared, received = [], []
    def before(preparation):
        if prepared: raise BF.B.EvidenceError('full wake dispatch repeated')
        backend.store.commit(root,'DISPATCH_INTENT',sequence,{'plan':doc,'preparation':preparation},backend.clock())
        prepared.append(preparation)
    def after(receipt):
        if len(prepared)!=1 or received or any(receipt.get(k)!=prepared[0][k] for k in ('run_id','lease_generation','ack_hash')):
            raise BF.B.EvidenceError('full receipt exact intent linkage differs')
        backend.store.commit(root,'DISPATCH_RECEIPT',sequence,{'plan':doc,'receipt':receipt},backend.clock())
        received.append(receipt)
    with gate('SOURCE_DISPATCH_BOUNDARY'):backend.start(doc,before,after)
    if len(prepared)!=1 or len(received)!=1:
        raise BF.B.EvidenceError('full dispatch receipt unproven; never repeat Run POST')
    return {'status':'DISPATCHED','index':index,'sequence':sequence,'source_dispatches':1,
            'current_entity':doc['runtime_plan']['entity'],'from':doc['runtime_plan']['from'],'to':doc['runtime_plan']['to']}


def reconstruct_plans(records, manifest, now):
    versions = {}
    for r in records:
        if r['kind'] != 'CHUNK_PLAN':continue
        p=r['payload'];index=p['index'];doc=p['plan']
        F.validate_leaf(manifest,index,doc,now.astimezone(BF.B.MSK).date())
        if r['sequence']!=index or p['shard_root']!=F.shard_root(manifest,index,doc):
            raise BF.B.EvidenceError('full child publication provenance differs')
        by = versions.setdefault(index,{})
        if doc['ack_hash'] in by and by[doc['ack_hash']] != p:
            raise BF.B.EvidenceError('full child publication conflict')
        by[doc['ack_hash']] = p
    chosen = {}
    transitions = [r for r in records if r['kind']=='CHUNK_SUPERSEDED']
    for index, by in versions.items():
        values = sorted(by.values(),key=lambda v:v['plan']['runtime_plan'].get('observation_date',''))
        for previous,current in zip(values,values[1:]):
            a,b = previous['plan']['runtime_plan'],current['plan']['runtime_plan']
            if not a.get('observation_date') or b['observation_date'] <= a['observation_date']:
                raise BF.B.EvidenceError('dated source chunk cannot change generation')
            proofs = [r for r in transitions if r['sequence']==index
                      and r['payload'].get('previous_ack')==previous['plan']['ack_hash']
                      and r['payload'].get('next_ack')==current['plan']['ack_hash']]
            if len(proofs)!=1 or proofs[0]['payload'].get('reason')!='PRESERVED_PARTIAL_DAY_ROLLOVER':
                raise BF.B.EvidenceError('snapshot replacement lacks preserved rollover proof')
            D.check_hash(proofs[0]['payload'].get('previous_state_hash'))
        chosen[index] = values[-1]
    return chosen


def wake(base, root):
    with gate('MANIFEST_ROOT'):
        records = base.store.history(root)
        manifest = only_manifest(records)
        if manifest['hash'] != root:
            raise BF.B.EvidenceError('full root differs')
    backend = Backend(base,manifest)
    with gate('MANIFEST_ROOT'):verify_authority(backend,manifest)
    from tools.tenancy import controller_cadence as CC
    with gate('MANIFEST_ROOT'):
        claim=CC.acquire(backend)
        if claim is not None:
            base.wake_claim=claim
            # A preceding holder may have committed completion/STOP while this
            # wake was reading authority. Reconstruct AFTER the atomic claim.
            records=base.store.history(root)
            if only_manifest(records)!=manifest:raise BF.B.EvidenceError('fenced root changed')
    if any(r['kind']=='STOPPED' for r in records):
        from tools.tenancy import full_leaf_recovery as FL
        approved=FL.load(backend,manifest,records) if any(r['kind']==FL.KIND for r in records) else []
        from tools.tenancy import full_cooldown_recovery as FC
        failed=FC.load(backend,manifest,records) if any(r['kind']==FC.KIND for r in records) else []
        backend.verified_cooldown_failures=failed
        approved=approved+failed
        unresolved=[r for r in records if r['kind']=='STOPPED' and D.digest(r) not in {p['stop_hash'] for p in approved}]
        if unresolved:
            # No source in a recovery wake; a later independent wake resumes.
            if CC.enabled(backend) and len(CC.inventory(backend))>1:
                # Defer the existing recovery rather than falsifying its strict
                # no-conflicting-execution predicate while a known peer waits.
                from tools.tenancy.cloud_tick import OverlapWait
                raise OverlapWait('WAIT_ACTIVE_CONTROLLER')
            recovered=FL.recover_pending(backend,manifest,records) if any(r['payload'].get('diagnostic',{}).get('stage') in FL.AUTO_STAGES for r in unresolved) else None
            if recovered:return {'status':'SOURCE_FREE_RECOVERED','source_dispatches':0,'recovery':recovered},backend
            return {'status':'STOPPED','source_dispatches':0}, backend
    done = completed(records,manifest)
    repair_t5_completions(backend, manifest, records, done)
    if len(done)==len(manifest['programs']):
        backend.store.commit(root,'FULL_COMPLETE',0,{'source_complete':True,'persisted_reconciled':True,
            'completed_chunks':len(done),'total_chunks':len(manifest['programs']),'ready':'UNPROVEN_FINAL_DQ_REQUIRED'},backend.clock())
        return {'status':'FULL_HISTORY_COMPLETE','source_dispatches':0},backend
    plans = reconstruct_plans(records, manifest, backend.clock())
    overlay=RP.load(backend,manifest)
    if overlay:
        # Drain any immutable elected intent/receipt BEFORE queue reordering.
        # No inference from lease TTL, runtime snapshot or local progress.
        drain=RP.pending_receipts(backend,{i:item for i,item in plans.items() if i not in done})
        if len(drain)>1:raise BF.B.EvidenceError('multiple pending full receipt authorities')
        if drain:
            i,item=drain[0]
            result,backend=leaf_wake(backend,manifest,i,item,records,defer_next=True)
            if result['status']!='RECONCILED_BOUNDARY':return result,backend
            records=backend.store.history(root);done=completed(records,manifest)
            plans=reconstruct_plans(records,manifest,backend.clock())
    pending=[i for i in range(len(manifest['programs'])) if i not in done]
    # Existing async scope always precedes any new report intent.
    reports=[i for i,p in plans.items() if i not in done and backend.state(p['plan'])['progress'].get('report')]
    if len(reports)>1:raise BF.B.EvidenceError('multiple full async scopes')
    pending=reports+[i for i in RP.ordered(manifest,done,overlay) if i not in reports]
    day=str(backend.clock().astimezone(BF.B.MSK).date())
    certs=[r['payload'] for r in records if r['kind']=='SNAPSHOT_CERT' and r['payload'].get('day')==day]
    catalog=next(i for i,p in enumerate(manifest['programs']) if p['entity']=='catalog')
    candidates=[p['plan'] for p in plans.values() if p['plan']['runtime_plan']['entity']=='catalog']
    candidates += [r['payload']['plan'] for r in records if r['kind']=='DEPENDENCY_PLAN']
    for cert in certs:
        docs=[d for d in candidates if d['ack_hash']==cert.get('plan_hash')]
        if not docs or docs[0]['runtime_plan'].get('observation_date') != day:
            raise BF.B.EvidenceError('dated Catalog certificate lacks exact published scope')
        F.validate_leaf(manifest,catalog,docs[0],backend.clock().astimezone(BF.B.MSK).date())
        source=backend.state(docs[0])
        if source['complete'] is not True or cert.get('coverage',{}).get('source_sequence') != source['sequence']:
            raise BF.B.EvidenceError('dated Catalog certificate lacks complete source state')
    if not certs:
        # The initial Catalog program is handled by the normal path. Later days
        # require a fresh dependency even though its initial snapshot is complete.
        if catalog in done:
            # Close every prior dependency receipt before creating today's
            # scope. An expired lease or midnight never erases a pending receipt.
            for record in records:
                if record['kind']!='DEPENDENCY_PLAN' or record['payload']['day']>=day:continue
                prior=record['payload'];history=backend.store.history(prior['shard_root'])
                decision=D.decide_tick(history,prior['shard_root'],{'status':'ELIGIBLE'},False)
                if decision['action'] in {'RECONCILE','RECOVER_RECEIPT_OR_STOP','STOPPED'}:
                    return leaf_wake(backend,manifest,catalog,prior,records,dependency=True)
            dependencies=[r['payload'] for r in records if r['kind']=='DEPENDENCY_PLAN' and r['payload'].get('day')==day]
            if len(dependencies)>1:raise BF.B.EvidenceError('full dated Catalog dependency ambiguous')
            if dependencies:
                item=dependencies[0]
                F.validate_leaf(manifest,catalog,item['plan'],backend.clock().astimezone(BF.B.MSK).date())
                if item['shard_root']!=F.shard_root(manifest,catalog,item['plan']):
                    raise BF.B.EvidenceError('full Catalog dependency shard differs')
            else:
                doc=F.render_leaf(manifest,catalog,backend.clock().astimezone(BF.B.MSK).date())
                item={'day':day,'plan':doc,'shard_root':F.shard_root(manifest,catalog,doc)}
                backend.store.commit(root,'DEPENDENCY_PLAN',0,item,backend.clock())
            return leaf_wake(backend,manifest,catalog,item,records,dependency=True)
        pending=[catalog]+[i for i in pending if i!=catalog]
    waits=[]
    for index in pending:
        backend.index=index
        item=plans.get(index)
        if item is None:
            doc=F.render_leaf(manifest,index,backend.clock().astimezone(BF.B.MSK).date())
            F.validate_leaf(manifest,index,doc,backend.clock().astimezone(BF.B.MSK).date())
            item={'index':index,'plan':doc,'shard_root':F.shard_root(manifest,index,doc)}
            backend.store.commit(root,'CHUNK_PLAN',index,item,backend.clock())
        result,backend=leaf_wake(backend,manifest,index,item,records)
        if result['status']=='SNAPSHOT_DAY_ROLLOVER':
            fresh=F.render_leaf(manifest,index,backend.clock().astimezone(BF.B.MSK).date())
            transition={'index':index,'previous_ack':item['plan']['ack_hash'],'next_ack':fresh['ack_hash'],
                'previous_state_hash':D.digest(backend.state(item['plan'])),'reason':'PRESERVED_PARTIAL_DAY_ROLLOVER'}
            backend.store.commit(root,'CHUNK_SUPERSEDED',index,transition,backend.clock())
            backend.store.commit(root,'CHUNK_PLAN',index,{'index':index,'plan':fresh,'shard_root':F.shard_root(manifest,index,fresh)},backend.clock())
        if overlay and result['status']=='WAITING':
            # A proven temporary WAIT is not a STOP and does not consume source
            # quota. Another eligible FULL leaf may run; no async intent replay.
            waits.append(result)
            continue
        return result,backend
    if waits:return waits[0],backend
    return {'status':'WAITING','source_dispatches':0,'basis':'FROZEN_SOURCE_BUDGET'},backend



def leaf_wake(backend,manifest,index,item,records,*,dependency=False,reconciled=False,defer_next=False):
    root=manifest["hash"]
    backend.index=index
    doc=item['plan'];shard=item['shard_root'];leaf={'hash':root,'plans':[doc]}
    backend.preflight(leaf)
    history=backend.store.history(shard,max_records=F.MAX_LEAF_RECORDS)
    if manifest['programs'][index].get('accepted_qualification_plan'):
        return import_accepted_sku90(backend,manifest,index,item,records,history),backend
    with gate('QUOTA'):quota=backend.quota(leaf,backend.clock())
    decision=D.decide_tick(history,shard,quota,backend.active_runtime_execution(),verified_failed_cooldown=[p for p in getattr(backend,'verified_cooldown_failures',[]) if p['shard']==shard])
    action=decision['action'];sequence=decision.get('sequence')
    if action=='MONITOR':return {'status':'MONITORING','source_dispatches':0,'index':index},backend
    if action in {'STOPPED','RECOVER_RECEIPT_OR_STOP'}:
        backend.store.commit(root,'STOPPED',0,{'reason':'FULL_DISPATCH_OR_REPORT_EVIDENCE_UNPROVEN','index':index},backend.clock())
        return {'status':'STOPPED','source_dispatches':0,'index':index},backend
    if action=='RECONCILE':
        payload=next(r['payload'] for r in history if r['kind']=='DISPATCH_RECEIPT' and r['sequence']==sequence)
        if payload['plan']!=doc:raise BF.B.EvidenceError('full terminal receipt scope differs')
        intent=next(r for r in history if r['kind']=='DISPATCH_INTENT' and r['sequence']==sequence)
        receipt=next(r for r in history if r['kind']=='DISPATCH_RECEIPT' and r['sequence']==sequence)
        try:
            result=backend.reconcile(payload)
            if result is None:return {'status':'MONITORING','source_dispatches':0,'index':index},backend
            if reconciled:raise BF.B.EvidenceError('multiple terminal receipts in one full wake')
            with gate('RECONCILIATION_APPEND'):
                backend.store.commit(shard,'RECONCILED',sequence,result,backend.clock())
        except GateFailure as error:
            error.diagnostic['context']={'index':index,'shard':shard,'sequence':sequence,'intent_hash':D.digest(intent),'receipt_hash':D.digest(receipt)}
            raise
        return leaf_wake(backend,manifest,index,item,records,dependency=dependency,reconciled=True,defer_next=defer_next)
    state=backend.state(doc)
    if state['complete']:
        proof=BF.verify_coverage(doc,doc['ack_hash'],backend=backend)
        payload={'index':index,'shard_root':shard,'ack_hash':doc['ack_hash'],'state_hash':D.digest(state),
            'sequence':state['sequence'],'dispatch_attempts':sum(r['kind']=='DISPATCH_INTENT' for r in history),
            'reused_qualification':None,
            'source_complete':True,'persisted_reconciled':True,
            'coverage':proof,'completed_at':backend.clock().isoformat()}
        if not dependency:
            backend.store.commit(root,'CHUNK_COMPLETE',index,payload,backend.clock())
            repair_t5_completions(backend,manifest,backend.store.history(root),completed(backend.store.history(root),manifest))
        if doc['runtime_plan']['entity']=='catalog':
            backend.store.commit(root,'SNAPSHOT_CERT',0,{'day':doc['runtime_plan']['observation_date'],
                'plan_hash':doc['ack_hash'],'coverage':proof},backend.clock())
        return {'status':'CHUNK_COMPLETE','source_dispatches':0,'index':index},backend
    observed=doc['runtime_plan'].get('observation_date')
    if observed and observed < str(backend.clock().astimezone(BF.B.MSK).date()):
        return {'status':'SNAPSHOT_DAY_ROLLOVER','source_dispatches':0,'index':index},backend
    if defer_next and reconciled:
        return {'status':'RECONCILED_BOUNDARY','source_dispatches':0,'index':index},backend
    if action=='WAITING':
        return {'status':'WAITING','source_dispatches':0,'eligible_at':decision['eligible_at'],'index':index},backend
    if action!='PREPARE_NEXT':raise BF.B.EvidenceError('unhandled full tick decision')
    if defer_next:
        return {'status':'RECONCILED_BOUNDARY','source_dispatches':0,'index':index},backend
    terminal=[r for r in history if r['kind']=='RECONCILED']
    if terminal:
        last=max(terminal,key=lambda r:r['sequence'])['payload']
        if last.get('eligible_at') and C.timestamp(last['eligible_at'])>backend.clock():
            return {'status':'WAITING','source_dispatches':0,'eligible_at':last['eligible_at'],'index':index,
                    'basis':'KNOWN_READ_RETRY_COOLDOWN'},backend
    failures=sum(bool(r['payload'].get('failed')) and r['payload'].get('retry_category')!='QUOTA' for r in terminal)
    if failures>=BF.CK.MAX_ATTEMPTS:raise BF.B.EvidenceError('full bounded source retry exhausted')
    return _dispatch(backend,manifest,index,doc,shard,sequence),backend

def import_accepted_sku90(backend,manifest,index,item,records,history):
    if history:raise BF.B.EvidenceError('accepted SKU90 day acquired an unexpected source dispatch shard')
    doc=item['plan'];state=backend.state(doc)
    if doc!=BF.QF.accepted_doc(BF.QF.SKU) or not state['complete']:
        raise BF.B.EvidenceError('accepted SKU90 identity/completion regressed')
    previous=backend.store.history(BF.QF.ROOT)
    final=[r for r in previous if r['kind']=='RECONCILED'
           and r['payload'].get('accounting',{}).get('state_hash')==D.digest(state)
           and r['payload'].get('accounting',{}).get('source_complete') is True
           and r['payload'].get('accounting',{}).get('persisted_reconciled') is True]
    if len(final)!=1:raise BF.B.EvidenceError('accepted SKU90 terminal reconciliation ambiguous')
    sequence=final[0]['sequence']
    receipts=[r for r in previous if r['kind']=='DISPATCH_RECEIPT' and r['sequence']==sequence]
    intents=[r for r in previous if r['kind']=='DISPATCH_INTENT' and r['sequence']==sequence]
    if (len(receipts)!=1 or len(intents)!=1 or receipts[0]['payload'].get('plan')!=doc
            or intents[0]['payload'].get('plan')!=doc or final[0]['payload'].get('checkpoint')!='DONE'):
        raise BF.B.EvidenceError('accepted SKU90 lacks terminal dispatch provenance')
    receipt=receipts[0]['payload']['receipt'];prepared=intents[0]['payload']['preparation']
    if any(receipt.get(k)!=prepared.get(k) for k in ('run_id','lease_generation','ack_hash')) or receipt['ack_hash']!=doc['ack_hash']:
        raise BF.B.EvidenceError('accepted SKU90 receipt/intent join differs')
    cid=BF.B.digest(['BOUNDED_PILOT_EXCLUSIVE',backend.c['project_id']])[:16]
    release=backend.tables.get_table(backend.c['datasets']['tenant_locks'],f"LD_{cid}_{receipt['lease_generation']:04d}")
    if (not release or release[0].get('owner')!=receipt['run_id']
            or parse_tenant_json(release[1]).get('ack_hash')!=doc['ack_hash']):
        raise BF.B.EvidenceError('accepted SKU90 terminal lease release absent')
    accounting=final[0]['payload']['accounting']
    fresh=backend.sku_accounting(doc,state,require_complete=True,snapshot=accounting['snapshot_date'])
    if (fresh['required_campaigns']!=90 or fresh['completed_campaigns']!=90 or fresh['pending_campaigns']
            or fresh['rows']!=accounting['rows']):
        raise BF.B.EvidenceError('accepted SKU90 persisted source footprint regressed')
    payload={'index':index,'shard_root':item['shard_root'],'ack_hash':doc['ack_hash'],
        'state_hash':D.digest(state),'sequence':state['sequence'],'dispatch_attempts':0,
        'source_complete':True,'persisted_reconciled':True,
        'coverage':{'source_sequence':state['sequence'],'readback':[{'rows':fresh['rows'],'keys':fresh['rows'],
                    'campaigns':90,'snapshot_date':accounting['snapshot_date']}],
                    'basis':'EXACT_ACCEPTED_QUALIFICATION_DAY_NO_NEW_SOURCE'},
        'completed_at':backend.clock().isoformat(),
        'reused_qualification':{'root':BF.QF.ROOT,'plan_id':BF.QF.SKU,'terminal_reconciliation_hash':D.digest(final[0])}}
    backend.store.commit(manifest['hash'],'CHUNK_COMPLETE',index,payload,backend.clock())
    repair_t5_completions(backend,manifest,backend.store.history(manifest['hash']),completed(backend.store.history(manifest['hash']),manifest))
    return {'status':'ACCEPTED_SKU90_REUSED','source_dispatches':0,'index':index}


def monitoring(backend, root, result):
    observed_start=backend.clock()
    records=backend.store.history(root);manifest=only_manifest(records);done=completed(records,manifest)
    latest=max((p['completed_at'] for p in done.values()),default=None)
    now=backend.clock()
    if now<observed_start:raise BF.B.EvidenceError('full monitoring clock/order anomaly')
    watermark=now
    index=result.get('index'); current=None
    if index is not None:
        read_start=backend.clock()
        if read_start<watermark:raise BF.B.EvidenceError('full monitoring clock/order anomaly')
        plans=reconstruct_plans(records,manifest,now)
        item=plans.get(index)
        # A current-day Catalog dependency may have the same program index.
        deps=[r['payload'] for r in records if r['kind']=='DEPENDENCY_PLAN'
              and r['payload']['day']==str(now.astimezone(BF.B.MSK).date())]
        if manifest['programs'][index]['entity']=='catalog' and deps:item=deps[0]
        if item:
            doc=item['plan'];p=doc['runtime_plan'];state=backend.state(doc)
            history=backend.store.history(item['shard_root'],max_records=F.MAX_LEAF_RECORDS)
            runs=[r['payload']['receipt']['run_id'] for r in history if r['kind']=='DISPATCH_RECEIPT']
            params={'origin':('TIMESTAMP',p['origin']),'pid':('STRING',p['plan_id']),
                    'entity':('STRING',p['entity']),'runs':('STRING',D.encoded(runs))}
            stats=backend.select(f"SELECT MAX(IF(status='OK',completed_at,NULL)) AS last_success,COUNT(DISTINCT IF(status='FAILED',ingestion_run_id,NULL)) AS failed_attempts FROM `{backend.journal}` WHERE started_at>=@origin AND entity=@entity AND (backfill_plan_id=@pid OR ingestion_run_id IN UNNEST(JSON_VALUE_ARRAY(@runs)))",params)
            observed_end=backend.clock()
            if observed_end<read_start:raise BF.B.EvidenceError('full monitoring clock/order anomaly')
            watermark=observed_end
            if len(stats)!=1:raise BF.B.EvidenceError('full checkpoint telemetry ambiguous')
            success=C.timestamp(stats[0]['last_success']) if stats[0]['last_success'] is not None else None
            if success and success>observed_end:raise BF.B.EvidenceError('full checkpoint telemetry in future')
            current={'entity':p['entity'],'from':p['from'],'to':p['to'],'plan_id':p['plan_id'],
                'observation_date':p.get('observation_date'),'sequence':state['sequence'],
                'source_rows':state['rows'],'source_pages':state['pages'],'source_requests':state['requests'],
                'state_bytes':len(D.encoded(state).encode()),'source_complete':state['complete'],
                'failed_attempts':stats[0]['failed_attempts'],
                'last_success':success.isoformat() if success else None,
                'checkpoint_age_seconds':int((observed_end-success).total_seconds()) if success else None,
                'observed_at':observed_end.isoformat(),
                'async_phase':(state['progress'].get('report') or {}).get('phase')}
    if latest and C.timestamp(latest)>watermark:raise BF.B.EvidenceError('full completed telemetry in future')
    recent=None
    overlay=RP.load(backend,manifest)
    if overlay:recent=RP.monitoring(backend,manifest,records,done,overlay,backend.clock())
    return {'root_hash':root,'purpose':'FULL_HISTORY','t5_plan_hash':manifest['t5_plan_hash'],
        'observation_start':observed_start.isoformat(),'observation_end':watermark.isoformat(),
        'chunk_grain':'BOUNDED_SOURCE_LEAF_WITH_CANONICAL_T5_PARENT',
        'total_chunks':len(manifest['programs']),'completed_chunks':len(done),
        'running_chunks':int(result['status'] in {'DISPATCHED','MONITORING'}),
        'waiting_chunks':int(result['status']=='WAITING'),
        'failed_chunks':int(result['status']=='STOPPED'),
        'current_index':result.get('index'),'current_entity':result.get('current_entity'),
        'current_from':result.get('from'),'current_to':result.get('to'),
        'current_scope':current,
        'last_successful_chunk':latest,
        'checkpoint_age_seconds':int((watermark-C.timestamp(latest)).total_seconds()) if latest else None,
        'bindings':backend.binding_status,'quota_wait_until':result.get('eligible_at'),
        'dq_reconciliation':'VERIFIED_FOR_COMPLETED_LEAVES_PENDING_FOR_REMAINDER',
        'finance_economic_finality':'PROVISIONAL','ready':'UNPROVEN_FINAL_DQ_REQUIRED',
        **({'recent_priority':recent} if recent else {})}


def repair_t5_completions(backend, manifest, records, done):
    """Crash-safe parent mapping; no parent is DONE before every child proof.

    A lost insert acknowledgement may leave identical rows, never a different
    proof. Restart reconstructs the deterministic row from immutable children.
    A committed mapping marker is written only after canonical readback.
    """
    parents = {}
    for index, p in enumerate(manifest['programs']):
        if p['t5_chunk_id'] is not None:
            parents.setdefault(p['t5_chunk_id'], []).append(index)
    mapped = {}
    for r in records:
        if r['kind'] != 'T5_PARENT_COMPLETE':continue
        payload = r['payload']; parent = payload.get('parent')
        if parent not in parents or r['sequence'] != min(parents[parent]):
            raise BF.B.EvidenceError('foreign canonical parent mapping')
        if parent in mapped and mapped[parent] != payload:
            raise BF.B.EvidenceError('conflicting canonical parent mapping')
        mapped[parent] = payload
    for parent, siblings in parents.items():
        if any(i not in done for i in siblings):
            if parent in mapped:raise BF.B.EvidenceError('false canonical DONE before all leaves')
            continue
        proofs = [done[i] for i in siblings]
        p = manifest['programs'][siblings[0]]
        evidence = {'full_root':manifest['hash'],'source_complete':True,'persisted_reconciled':True,
                    'leaf_proofs':[D.digest(x) for x in proofs],
                    'economic_finality':'PROVISIONAL' if p['entity']=='finance_accrual' else 'NOT_APPLICABLE'}
        at = max(x['completed_at'] for x in proofs)
        row = {f['name']:None for f in parse_tenant_json((BF.REPO/'tools/tenancy/schema/tenant_ops/BACKFILL_CHECKPOINTS.json').read_text())['schema']}
        row.update(backfill_id=parent,entity=p['entity'],window_from=p['t5_from'],window_to=p['t5_to'],
            status='DONE',attempts=sum(x['dispatch_attempts'] for x in proofs),
            rows_written=sum(sum(x.get('rows',0) for x in proof['coverage']['readback']) for proof in proofs),
            run_id='full-'+D.digest(evidence)[:32],updated_at=at,completed_at=at,
            plan_hash=manifest['t5_plan_hash'],evidence_json=D.encoded(evidence))
        mapping = {'parent':parent,'row_hash':D.digest(row),'leaf_proofs':evidence['leaf_proofs']}
        if parent in mapped:
            if mapped[parent] != mapping:raise BF.B.EvidenceError('canonical mapping/leaf proof conflict')
            continue
        # Verify even after insert transport failure; a later wake repeats the
        # same immutable row only when readback proves it was not acknowledged.
        existing = backend.select(f"SELECT DISTINCT evidence_json,run_id,attempts,rows_written FROM `{backend.c['project_id']}.{backend.c['datasets']['tenant_ops']}.BACKFILL_CHECKPOINTS` WHERE plan_hash=@plan AND backfill_id=@parent AND status='DONE' LIMIT 2",
                                  {'plan':('STRING',manifest['t5_plan_hash']),'parent':('STRING',parent)})
        expected = {k:row[k] for k in ('evidence_json','run_id','attempts','rows_written')}
        if existing and existing != [expected]:
            raise BF.B.EvidenceError('canonical parent already has a different DONE proof')
        if not existing:
            backend.tables.append(backend.c['datasets']['tenant_ops'],'BACKFILL_CHECKPOINTS',[row])
            existing = backend.select(f"SELECT DISTINCT evidence_json,run_id,attempts,rows_written FROM `{backend.c['project_id']}.{backend.c['datasets']['tenant_ops']}.BACKFILL_CHECKPOINTS` WHERE plan_hash=@plan AND backfill_id=@parent AND status='DONE' LIMIT 2",
                                      {'plan':('STRING',manifest['t5_plan_hash']),'parent':('STRING',parent)})
            if existing != [expected]:raise BF.B.EvidenceError('canonical DONE readback unproven')
        backend.store.commit(manifest['hash'],'T5_PARENT_COMPLETE',min(siblings),mapping,backend.clock())
