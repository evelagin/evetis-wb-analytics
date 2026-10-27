"""Exact retained f507 evidence through real durable storage, no network/source.

Only the previously qualified authority loaders are stubbed here; their own
image groups still run independently. New f507 validation/reads/writes are real.
"""
from copy import deepcopy
from contextlib import contextmanager, ExitStack
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch
import json
import re
from functools import lru_cache
from tools.tenancy import f507_recovery as R, durable_plan as D, tenant_backfill as BF
from tools.tenancy import orchestration_contract as O, cloud_controller as CLOUD
from tools.tenancy import current_release_handoff as A, current_policy as P
from tools.tenancy import current_recon_recovery as OLD_R, full_controller as H
from tools.tenancy import full_fbo_diagnostic_closure as LQ, recent_priority as RP
from tools.tenancy.validation import parse_tenant_json as parse

NOW = datetime(2026, 10, 16, 8, tzinfo=timezone.utc)
NEW = 'a' * 40
ACK = 'e' * 64  # synthetic final production ACK, never an actual authorization


@lru_cache(maxsize=1)
def historical_plans():
    """Validate unchanged historical fixture once, then copy its pure result.

    The new observer/consumer remain real; repeated fault cases need not rerun
    the separately qualified 51-leaf reconstruction on identical immutable data.
    """
    roots=parse((BF.REPO/'tools/tenancy/fixtures/f507_records.json').read_text())
    m=H.only_manifest(roots[R.ROOT])
    return H.reconstruct_plans(roots[R.ROOT],m,NOW)


class Tables:
    def __init__(self): self.values = {}; self.writes = []; self.created = {}
    def get_table(self, ds, name): return deepcopy(self.values.get((ds, name)))
    def create_marker(self, ds, name, labels, text):
        if (ds, name) in self.values: return False
        self.writes.append((ds, name)); self.values[(ds, name)] = (deepcopy(labels), text)
        self.created[(ds, name)] = NOW
        return True
    def list_tables(self, ds, with_expiry=False):
        out = []
        for (dataset, name), value in self.values.items():
            if dataset != ds: continue
            row = (name, value[0], self.created.get((ds, name), datetime(2026, 10, 15, 19, tzinfo=timezone.utc)))
            out.append(row + (None,) if with_expiry else row)
        return out


@contextmanager
def fixture():
    base = R.baseline(); fresh = parse((BF.REPO / 'tools/tenancy/fixtures/f507_fresh.json').read_text())
    roots = parse((BF.REPO / 'tools/tenancy/fixtures/f507_records.json').read_text())
    c = BF.target('client_001'); tables = Tables(); rows = []; inserts = []
    m = H.only_manifest(roots[R.ROOT])
    release = parse((BF.REPO / 'infra/tenant/releases/backfill' / (R.OLD + '.json')).read_text())
    release.update(source_sha=NEW, image='europe-west1-docker.pkg.dev/mpa-platform/mpa-runtime/tenant-backfill-controller@sha256:' + '2' * 64,
        controller_implementation_hash=O.implementation_hash(BF.REPO), exact_f507_recovery_version=1)
    release['verification']['exact_f507_recovery'] = 'PASS'
    c['orchestration'] = O.block(c, dict(release=NEW, root_hash=R.ROOT, scheduler_state='PAUSED'), BF.REPO, release)
    for path, value in fresh['markers'].items():
        ds, name = path.split('/', 1); tables.values[(ds, name)] = tuple(value)
    locks = c['datasets']['tenant_locks']; cn, xn = __import__('tools.tenancy.controller_cadence', fromlist=['names']).names(R.ROOT, 1129)
    tables.values[(locks, cn)] = tuple(base['claim']); tables.values[(locks, xn)] = tuple(base['closure'])
    for name, value in fresh['fences'].items(): tables.values[(locks, name)] = tuple(value)
    for rs in roots.values():
        for r in rs:
            h = D.digest(r); rows.append(dict(backfill_id=h[:16], plan_hash=r['root_hash'], evidence_json=D.encoded(r)))
            tables.values[(locks, 'BFR_' + h)] = ({'root': r['root_hash'][:16], 'kind': r['kind'].lower()}, D.encoded(dict(root_hash=r['root_hash'], record_hash=h, kind=r['kind'], sequence=r['sequence'])))
    spec = dict(settings=dict(release=NEW, root_hash=R.ROOT, scheduler_state='PAUSED'), release=release)
    tables.values[(locks, CLOUD.descriptor_name(NEW, R.ROOT, 'PAUSED'))] = ({}, D.encoded(spec))
    policy = base['current_policy']; old_source = policy['controller_source']
    old = parse((BF.REPO / 'infra/tenant/releases/backfill' / (old_source + '.json')).read_text())
    handoff = dict(version=1, type=A.TYPE, tenant='client_001', project=c['project_id'], root=R.ROOT,
        policy_hash=policy['hash'], old_source=old_source, old_image=old['image'], new_source=NEW,
        new_image=release['image'], new_implementation_hash=release['controller_implementation_hash'],
        runtime_image=release['runtime_image'], runtime_implementation_hash=release['runtime_implementation_hash'],
        owner_ack_sha256=ACK, qualification_hash='3'*64, security_hash='4'*64,
        created_at='2026-10-16T07:00:00+00:00', source_budget=0)
    handoff = dict(handoff, hash=D.digest(handoff)); tables.values[(c['datasets']['ref'], A.name(NEW))] = A.value(handoff)

    def select(sql, params):
        assert sql.startswith('SELECT ') and ';' not in sql
        if 'SELECT DISTINCT evidence_json' in sql:
            root = params['root'][1]; record = params.get('record', (None, None))[1]
            return [dict(evidence_json=s) for s in sorted({r['evidence_json'] for r in rows if r['plan_hash']==root and (record is None or r['backfill_id']==record)})]
        if 'COUNT(*) AS n' in sql: return [dict(n=0)]
        if 'WHERE run_id IN UNNEST' in sql:
            runs = set(parse(params['runs'][1])); result=[]
            for values in fresh['checkpoints'].values(): result.extend(deepcopy(r) for r in values if r['run_id'] in runs)
            return sorted(result, key=lambda r:r['updated_at'])
        for table, counts in base['RAW'].items():
            if table+'`' in sql:return [{k:v for k,v in counts.items() if k!='day'}]
        raise AssertionError('unexpected read: ' + sql)
    def write(row): inserts.append(deepcopy(row)); rows.append(deepcopy(row))
    b = SimpleNamespace(c=c, manifest=m, clock=lambda:NOW, current_execution=None, tables=tables,
        journal=c['project_id']+'.ozon_raw.OZON_INGESTION_RUNS', select=select,
        f507_production_authority=dict(type=R.ACK_TYPE, stop_hash=R.STOP, source_budget=0, owner_ack_sha256=ACK))
    b.store = D.DurableRecords(c, tables, lambda c,s,p:select(s,p), write)
    resources={}; inventories={}; baseurl,jobs=BF.resources(c)
    for name, value in fresh['jobs'].items():
        resources[baseurl+'/jobs/'+name]=deepcopy(value['job']); inventories[baseurl+'/jobs/'+name]=deepcopy(value['executions'])
    resources[baseurl+'/jobs/'+O.JOB]['template']=dict(taskCount=1,parallelism=1,template=dict(timeout=c['orchestration']['job']['timeout'],maxRetries=0,serviceAccount=c['orchestration']['accounts']['controller']['email'],containers=[dict(image=release['image'],command=['python','-m','tools.tenancy.cloud_controller'],env=[dict(name=k,value=v) for k,v in c['orchestration']['job']['env'].items()])]))
    schedules=deepcopy(fresh['schedulers']); requests=[]
    def request(method,url,body=None):
        assert method=='GET' and body is None, 'source/mutation route denied'
        requests.append(url)
        if url==BF.RUN_API+'/'+base['actor']['name']:return deepcopy(base['actor'])
        if url==BF.global_job_url(c['project_id']):
            return {'items':[dict(metadata=dict(name=n.rsplit('/',1)[-1],labels={'cloud.googleapis.com/location':c['region']})) for n in resources]}
        if url.startswith(BF.SCHED_API+'/'):return deepcopy(schedules)
        path=url.removeprefix(BF.RUN_API+'/').split('?')[0]
        if path.endswith('/executions'):return dict(executions=deepcopy(inventories[path.removesuffix('/executions')]))
        if path in resources:return deepcopy(resources[path])
        raise AssertionError('unexpected route: '+url)
    b.request=request
    states={v['item']['plan']['runtime_plan']['plan_id']:v['state'] for d in fresh['current'].values() for v in d['items'].values()}
    b.state=lambda doc:deepcopy(states.get(doc['runtime_plan']['plan_id'],BF.B.initial(doc['runtime_plan'])))
    prior=[dict(stop_hash=D.digest(r)) for r in roots[R.ROOT] if r['kind']=='STOPPED' and D.digest(r)!=R.STOP]
    # Old accepted mechanisms retain their own independent image groups.
    retained_plans=historical_plans()
    with ExitStack() as stack:
        stack.enter_context(patch.object(OLD_R,'prior',lambda *a:deepcopy(prior)))
        stack.enter_context(patch.object(H,'verify_authority',lambda *a:True))
        stack.enter_context(patch.object(LQ,'load',lambda *a:[]))
        stack.enter_context(patch.object(RP,'pending_receipts',lambda *a:[]))
        stack.enter_context(patch.object(H,'reconstruct_plans',lambda *a:deepcopy(retained_plans)))
        yield b,dict(base=base,fresh=fresh,roots=roots,rows=rows,inserts=inserts,tables=tables,
            release=release,resources=resources,inventories=inventories,schedules=schedules,
            states=states,requests=requests,prior=prior)


def observed(b):
    return R.observe(b,b.manifest,owner_ack_sha256=ACK,qualification_hash='3'*64,
                     review_hash='5'*64,security_hash='4'*64)


def _check():
    with fixture() as (b,f):
        p=observed(b); original=deepcopy(f['rows']); markers=deepcopy(b.tables.values)
        ops=R.write_manifest(p,b.manifest)
        assert len(ops)==4 and ops[2]['row']['entity']=='orchestration'
        assert ops[2]['row']['run_id'] is None and ops[2]['row']['lease_generation'] is None
        assert len(ops[1]['value'][1].encode())<16384 and len(ops[2]['row']['evidence_json'].encode())<900000
        assert R.publish(b,p,b.manifest)==p
        assert len(b.tables.writes)==3 and len(f['inserts'])==1
        assert f['rows'][:len(original)]==original
        assert all(b.tables.values[k]==v for k,v in markers.items())
        assert R.load(b,b.manifest,b.store.history(R.ROOT))==[p]
        before=deepcopy((f['rows'],b.tables.values)); R.publish(b,p,b.manifest)
        assert before==(f['rows'],b.tables.values)
        assert not any(parse(r['evidence_json'])['kind'] in {'RECONCILED','CURRENT_CERT','COMPLETE','FULL_COMPLETE'} for r in f['inserts'])
    return dict(exact_f507='PASS',real_four_write_transport='PASS',originals_preserved='PASS',
                idempotent_no_source='PASS',partial_not_complete='PASS')


def check():
    """Frozen production incident replayed with the runtime it actually ran with."""
    from tools.tenancy.runtime_history_fixture import historical_runtime
    with historical_runtime():
        return _check()


if __name__=='__main__': print(json.dumps(check(),sort_keys=True))
