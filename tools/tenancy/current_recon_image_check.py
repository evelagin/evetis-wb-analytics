"""Network-disabled owner procedure regression; synthetic evidence only."""
from copy import deepcopy
from datetime import datetime, timezone, date, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from contextlib import ExitStack, contextmanager
import json
from tools.tenancy import current_release_handoff as A, current_recon_recovery as R
from tools.tenancy import current_controller as C, current_policy as P, durable_plan as D
from tools.tenancy import tenant_backfill as BF, orchestration_contract as O, cloud_controller as CLOUD
from tools.tenancy import campaign_persistence_image_check as CHECK
from tools.tenancy import controller_cadence as CC, full_controller as H
from tools.tenancy.validation import parse_tenant_json as parse
NOW = datetime(2026, 10, 14, 20, tzinfo=timezone.utc)
OLD = 'ba9d3d0430e598290ef71e321f235e27941170f5'
NEW = 'a' * 40
ACK = 'e' * 64


class Tables:
    def __init__(self): self.values = {}; self.writes = []
    def get_table(self, ds, name): return deepcopy(self.values.get((ds, name)))
    def create_marker(self, ds, name, labels, text):
        self.writes.append((ds, name)); self.values.setdefault((ds, name), (labels, text)); return True
    def list_tables(self, ds): return [(name, value[0], None) for (dataset, name), value in self.values.items() if dataset == ds]


class Store:
    def __init__(self): self.records = []; self.writes = []
    def history(self, root): return deepcopy([r for r in self.records if r['root_hash'] == root])
    def commit(self, root, kind, seq, payload, now):
        assert kind in D.KINDS
        r = dict(version=D.VERSION, root_hash=root, kind=kind, sequence=seq, payload=deepcopy(payload))
        self.writes.append(r)
        if r not in self.records: self.records.append(r)
        return D.digest(r)


def setup():
    c = BF.target('client_001'); root = c['orchestration']['job']['env']['BACKFILL_ROOT_HASH']
    old = parse((BF.REPO / 'infra/tenant/releases/backfill' / (OLD + '.json')).read_text())
    release = deepcopy(old); release.update(source_sha=NEW,
        image='europe-west1-docker.pkg.dev/mpa-platform/mpa-runtime/tenant-backfill-controller@sha256:' + '2' * 64,
        controller_implementation_hash=O.implementation_hash(BF.REPO))
    c['orchestration'] = O.block(c, dict(release=NEW, root_hash=root, scheduler_state='PAUSED'), BF.REPO, release)
    policy = P.sealed(dict(version=1, type=P.C.TYPE, tenant='client_001', project=c['project_id'], historical_root=root,
        owner_decision_sha256='1' * 64, created_at='2026-10-14T04:00:00+00:00', first_day='2026-10-14',
        entities=list(P.ORDER), snapshot_sla_seconds=86400, fbo_sla_seconds=86400,
        controller_source=OLD, controller_image=old['image'], runtime_image=old['runtime_image'],
        runtime_implementation_hash=BF.B.implementation_hash(), fbo_contract_hash=None,
        source_concurrency=1, finance_finality='PROVISIONAL'))
    tables = Tables(); store = Store(); m = dict(hash=root, tenant='client_001', project=c['project_id'], programs=[])
    b = SimpleNamespace(c=c, manifest=m, clock=lambda: NOW, current_execution=None, phase_b_ack_sha256=ACK,
                        tables=tables, store=store, journal=c['project_id'] + '.ozon_raw.OZON_INGESTION_RUNS')
    ds = c['datasets']['ref']; tables.values[(ds, P.name(OLD))] = ({'kind': 'current_policy', 'root': policy['hash'][:16]}, D.encoded(policy))
    spec = dict(settings=dict(release=NEW, root_hash=root, scheduler_state='PAUSED'), release=release)
    tables.values[(c['datasets']['tenant_locks'], CLOUD.descriptor_name(NEW, root, 'PAUSED'))] = ({}, D.encoded(spec))
    handoff = dict(version=1, type=A.TYPE, tenant='client_001', project=c['project_id'], root=root,
        policy_hash=policy['hash'], old_source=OLD, old_image=old['image'], new_source=NEW, new_image=release['image'],
        new_implementation_hash=release['controller_implementation_hash'], runtime_image=release['runtime_image'],
        runtime_implementation_hash=BF.B.implementation_hash(), owner_ack_sha256=ACK, qualification_hash='3' * 64,
        security_hash='4' * 64, created_at='2026-10-14T05:00:00+00:00', source_budget=0)
    handoff = dict(handoff, hash=D.digest(handoff)); tables.values[(ds, A.name(NEW))] = A.value(handoff)
    return b, policy, handoff, release



def runtime_job(c, name, cfg, control=False):
    account = c['control']['email'] if control else f"{c['marketplaces']['ozon']['service_accounts']['runtime']}@{c['project_id']}.iam.gserviceaccount.com"
    co = dict(image=c['marketplaces']['ozon']['runtime_image'], env=[dict(name=k,value=v) for k,v in cfg['env'].items()])
    if control: co.update(command=['python','lifecycle.py'], args=['status'])
    return dict(name=name, template=dict(taskCount=1,parallelism=1,template=dict(timeout='3600s',maxRetries=0,serviceAccount=account,containers=[co])))


def controller_job(block, name):
    co=dict(image=block['job']['image'], command=['python','-m','tools.tenancy.cloud_controller'],env=[dict(name=k,value=v) for k,v in block['job']['env'].items()])
    return dict(name=name,template=dict(taskCount=1,parallelism=1,template=dict(timeout=block['job']['timeout'],maxRetries=0,serviceAccount=block['accounts']['controller']['email'],containers=[co])))


@contextmanager
def full_fixture():
    b, policy, handoff, release = setup(); b.full_tables=b.tables
    rows=[]
    def record_select(c,sql,params):
        assert sql.startswith('SELECT DISTINCT ') and ';' not in sql
        return [{'evidence_json':x} for x in sorted({r['evidence_json'] for r in rows if r['plan_hash']==params['root'][1] and ('record' not in params or r['backfill_id']==params['record'][1])})]
    b.store=D.DurableRecords(b.c,b.tables,record_select,lambda row: rows.append(deepcopy(row)))
    day=date(2026,10,14);doc=P.document(policy,day,'ads_campaigns'); shard=P.shard(policy,day,doc)
    doc0,state,us=CHECK.fixture(); state['plan_id']=doc['runtime_plan']['plan_id']; BF.B.validate(doc['runtime_plan'],state)
    unit=dict(backfill_detail_json=us[0]['backfill_detail_json'],evidence_json=json.dumps(dict(plan=doc['runtime_plan'],state=state)))
    run='bf-00000000-0000-0000-0000-000000000001';gen=230;base,jobs=BF.resources(b.c)
    opname=base+'/operations/offlinereceipt';job=next(n for n,cfg in jobs.items() if 'ads_campaigns' in cfg['entities'])
    b.store.commit(policy['hash'],'CURRENT_DAY',0,dict(day=str(day),root=P.cycle_root(policy,day),policy_hash=policy['hash']),NOW)
    b.store.commit(P.cycle_root(policy,day),'CURRENT_PLAN',0,dict(day=str(day),policy_hash=policy['hash'],plan=doc,shard=shard),NOW)
    prep=dict(run_id=run,lease_generation=gen,ack_hash=doc['ack_hash']); receipt=dict(prep,operation=opname)
    b.store.commit(shard,'DISPATCH_INTENT',1,dict(plan=doc,preparation=prep),NOW)
    b.store.commit(shard,'DISPATCH_RECEIPT',1,dict(plan=doc,receipt=receipt),NOW)
    actor=base+'/jobs/'+O.JOB+'/executions/'+O.JOB+'-offline'
    stop=b.store.commit(b.manifest['hash'],'STOPPED',0,dict(reason='CONTROLLER_GATE_OR_EVIDENCE_FAILURE',controller_execution=actor),NOW)
    old=parse((BF.REPO/'infra/tenant/releases/backfill'/(OLD+'.json')).read_text())
    oldblock=O.block(b.c,dict(release=OLD,root_hash=b.manifest['hash'],scheduler_state='ENABLED'),BF.REPO,old)
    x=controller_job(oldblock,actor);x.update(template=x['template']['template'],taskCount=1,createTime='2026-10-14T06:33:00Z',startTime='2026-10-14T06:33:16Z',completionTime='2026-10-14T06:37:38Z',failedCount=1,retriedCount=0)
    source=runtime_job(b.c,base+'/jobs/'+job+'/executions/'+job+'-offline',jobs[job]);source.update(template=source['template']['template'],taskCount=1,createTime='2026-10-14T06:31:26Z',completionTime='2026-10-14T06:32:10Z',succeededCount=1,retriedCount=0)
    env={v['name']:v['value'] for v in source['template']['containers'][0]['env']}
    env.update(INGESTION_RUN_ID=run,ENTITIES='ads_campaigns',SINCE=doc['runtime_plan']['from'],UNTIL=doc['runtime_plan']['to'],BACKFILL_MODE=BF.B.VERSION,BACKFILL_GENERATION=doc['runtime_plan']['generation'],BACKFILL_ORIGIN=doc['runtime_plan']['origin'],BACKFILL_TARGET_PROJECT=b.c['project_id'],BACKFILL_MAX_REQUESTS=str(doc['max_requests']),BACKFILL_MAX_UNITS=str(doc['max_units']))
    source['template']['containers'][0]['env']=[dict(name=k,value=v) for k,v in env.items()]
    claim=CC.sealed(dict(version=1,tenant=b.manifest['tenant'],project=b.manifest['project'],root=b.manifest['hash'],policy_hash=D.digest(b.c['orchestration']['cadence']),source=OLD,image=old['image'],execution=actor,generation=517,created_at='2026-10-14T06:33:17Z'))
    close=CC.sealed(dict(version=1,claim_hash=claim['hash'],status='STOPPED',source_dispatches=0,closed_at='2026-10-14T06:37:30Z'))
    ds=b.c['datasets']['tenant_locks'];cn,xn=CC.names(b.manifest['hash'],517)
    b.tables.values[(ds,cn)]=CC.claim_value(claim);b.tables.values[(ds,xn)]=({'kind':'controller_close','root':b.manifest['hash'][:16]},D.encoded(close))
    cid=BF.B.digest(['BOUNDED_PILOT_EXCLUSIVE',b.c['project_id']])[:16];lease_name=BF.CK.lease_name(cid,gen)
    until=datetime(2026,10,14,8,31,26,652000,tzinfo=timezone.utc)
    lease=({'owner':run,'until':str(int(until.timestamp()))},json.dumps(dict(mode='BOUNDED_PILOT',ack_hash=doc['ack_hash'])))
    b.tables.values[(ds,lease_name)]=lease;b.tables.values[(ds,f'LD_{cid}_{gen:04d}')]=({'owner':run},json.dumps(dict(operation=opname,ack_hash=doc['ack_hash'])))
    cps=[dict(backfill_id='initial',status='RUNNING',lease_generation=gen,evidence_json=json.dumps(dict(proof=None)),updated_at='2026-10-14T06:31:26Z'),dict(backfill_id='observed',status='RUNNING',lease_generation=gen,evidence_json=json.dumps(dict(proof=dict(plan=doc['runtime_plan'],state=state))),updated_at='2026-10-14T06:37:17Z')]
    def select(sql,params):
        assert sql.startswith('SELECT ') and ';' not in sql
        if 'SELECT DISTINCT ingestion_run_id' in sql:return [dict(ingestion_run_id=run+'-u1',backfill_sequence=1,**deepcopy(unit))]
        if 'COUNT(*) AS n' in sql:return [dict(n=0)]
        if 'FROM `'+b.journal+'`' in sql: return [deepcopy(unit)]
        if 'RAW_OZON_ADS_CAMPAIGNS' in sql:return [dict(rows_n=0,keys_n=0)]
        if 'BACKFILL_CHECKPOINTS' in sql:return deepcopy(cps)
        raise AssertionError('unbounded/unexpected owner query')
    b.select=select;b.state=lambda d:deepcopy(state)
    schedules=[];resources={}
    for n,cfg in jobs.items():resources[base+'/jobs/'+n]=runtime_job(b.c,base+'/jobs/'+n,cfg)
    resources[base+'/jobs/tenant-control']=runtime_job(b.c,base+'/jobs/tenant-control',b.c['control']['job'],True)
    resources[base+'/jobs/'+O.JOB]=controller_job(b.c['orchestration'],base+'/jobs/'+O.JOB)
    for n in [v['scheduler'] for v in jobs.values()]+[O.SCHEDULER]:
        sch=dict(name=base+'/jobs/'+n,state='PAUSED')
        if n==O.SCHEDULER:sch.update(schedule='*/3 * * * *',timeZone='Europe/Moscow',httpTarget=dict(uri=b.c['orchestration']['scheduler']['uri'],httpMethod='POST',oauthToken=dict(serviceAccountEmail=b.c['orchestration']['accounts']['wake']['email'],scope='https://www.googleapis.com/auth/cloud-platform')))
        schedules.append(sch)
    calls=[]
    def request(method,url,*args,**kwargs):
        assert method=='GET' and not args and not kwargs,'owner observer attempted transport/write'
        calls.append(url)
        if url==BF.global_job_url(b.c['project_id']):return dict(items=[dict(metadata=dict(name=n.rsplit('/',1)[-1],labels={'cloud.googleapis.com/location':b.c['region']})) for n in resources])
        if url==BF.RUN_API+'/'+actor:return deepcopy(x)
        if url==BF.RUN_API+'/'+opname:return dict(done=True,response=deepcopy(source))
        if url.startswith(BF.TT.BQ):return dict(expirationTime=str(int((until+BF.CK.LEASE_TABLE_KEEP).timestamp()*1000)))
        if url.startswith(BF.SCHED_API):return dict(jobs=deepcopy(schedules))
        if url.endswith('/executions?pageSize=1000'):return dict(executions=[deepcopy(source)] if '/jobs/'+job+'/' in url else [])
        name=url.removeprefix(BF.RUN_API+'/')
        if name in resources:return deepcopy(resources[name])
        raise AssertionError('unexpected owner metadata read')
    b.request=request; authority=[]
    # Tenant/global gates themselves are already regression-tested by H. Here
    # they are an explicit required boundary, never silently bypassed in code.
    with patch.object(H,'verify_authority',lambda backend,m: authority.append((backend.c,m))):
        yield b,dict(policy=policy,doc=doc,unit=unit,state=state,stop=stop,actor=x,source=source,claim=claim,cps=cps,rows=rows,schedules=schedules,resources=resources,calls=calls,lease_name=lease_name,authority=authority)


def observed(b,f):
    return R.observe(b,b.manifest,f['stop'],'2026-10-14',owner_ack_sha256=ACK,qualification_hash='3'*64,security_hash='4'*64,source_probe_hash='5'*64)[0]


def check():
    with full_fixture() as (b,f):
        before_observation=deepcopy((f['rows'],b.tables.values))
        p=observed(b,f); assert f['authority'] and before_observation==(f['rows'],b.tables.values)
        originals=deepcopy((f['cps'],f['unit'],b.tables.get_table(b.c['datasets']['tenant_locks'],f['lease_name'])))
        before=len(f['rows']);R.publish(b,p,b.manifest)
        assert len(f['rows'])==before+2
        assert [parse(x['evidence_json'])['kind'] for x in f['rows'][before:]]==['RECONCILED',R.KIND]
        assert R.load(b,b.manifest,b.store.history(b.manifest['hash']))==[p]
        assert originals==(f['cps'],f['unit'],b.tables.get_table(b.c['datasets']['tenant_locks'],f['lease_name']))
        assert not any(parse(x['evidence_json'])['kind']=='CURRENT_CERT' for x in f['rows'])
        b.clock=lambda:NOW+timedelta(days=8)
        del b.tables.values[(b.c['datasets']['tenant_locks'],f['lease_name'])]
        assert R.load(b,b.manifest,b.store.history(b.manifest['hash']))==[p]
    return dict(owner_phase_b_gate='PASS',byte_exact_current_namespace='PASS',real_durable_append='PASS',partial_not_complete='PASS',expired_retained_lease='PASS',source_calls=0,business_writes=0)

if __name__=='__main__':print(json.dumps(check(),sort_keys=True))
