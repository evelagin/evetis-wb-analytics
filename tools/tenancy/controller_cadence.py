"""Closed client_001 cadence and atomic controller-wake ownership.

This supplementary orchestration fence never replaces a SOURCE lease, intent,
receipt or reconciliation. Known overlapping wakes wait successfully; unknown
ownership stays closed. There is no marketplace transport or source retry here.
"""
from datetime import datetime, timezone
from statistics import median, mean
import re
from tools.tenancy import tenant_backfill as BF, durable_plan as D
from tools.tenancy.validation import parse_tenant_json as parse

PROFILE = 'CLIENT001_HISTORICAL_3_STABLE_10_V1'
FIELDS = frozenset({'version','profile','tenant','project','root','preferred',
                    'stable_fallback','owner_ack_sha256'})
CLAIM_FIELDS = frozenset({'version','tenant','project','root','policy_hash','source',
                          'image','execution','generation','created_at','hash'})
CLOSE_FIELDS = frozenset({'version','claim_hash','status','source_dispatches','closed_at','hash'})
STATUSES = frozenset({'DISPATCHED','MONITORING','WAITING','CHUNK_COMPLETE',
    'FULL_HISTORY_COMPLETE','SOURCE_FREE_RECOVERED','QUIESCENT_PAUSED',
    'STOPPED','WAITING_READ_RETRY','SNAPSHOT_DAY_ROLLOVER','RECONCILED_BOUNDARY'})
WAITS = frozenset({'WAIT_ACTIVE_CONTROLLER','WAIT_ACTIVE_RUNTIME',
                   'WAIT_RECONCILIATION','SKIP_AUTHORITY_HELD'})


def fail(reason):raise BF.B.EvidenceError('controller cadence: '+reason)
def sealed(value):return dict(value,hash=D.digest(value))


def policy(p, c, root):
    if not isinstance(p,dict) or set(p)!=FIELDS or type(p['version']) is not int or p['version']!=1:
        fail('closed cadence authority required')
    if (p['profile'],p['tenant'],p['project'])!=(PROFILE,'client_001','mpa-t-client-001'):
        fail('unregistered cadence/tenant')
    if (c['tenant_id'],c['project_id'],root)!=(p['tenant'],p['project'],p['root']):
        fail('cadence root/tenant differs')
    D.check_hash(root);D.check_hash(p['owner_ack_sha256'])
    if (p['preferred'],p['stable_fallback'])!=('*/3 * * * *','*/10 * * * *'):
        fail('unapproved cadence')
    return p


def enabled(backend):
    block=getattr(backend,'c',{}).get('orchestration',{})
    p=block.get('cadence')
    if p is None:return False
    policy(p,backend.c,block['job']['env']['BACKFILL_ROOT_HASH'])
    if block['job']['env'].get('HISTORICAL_CADENCE_POLICY')!=D.digest(p):
        fail('cadence environment differs')
    return True


def schedule_allowed(block, schedule):
    p=block.get('cadence')
    return schedule in {p['preferred'],p['stable_fallback']} if p else schedule==block['scheduler']['schedule']


def at(value):
    if not isinstance(value,str):fail('execution timestamp unavailable')
    try:v=datetime.fromisoformat(value.replace('Z','+00:00'))
    except ValueError:fail('execution timestamp malformed')
    if v.utcoffset() is None:fail('execution timestamp unzoned')
    return v.astimezone(timezone.utc)


def execution(backend, x, *, active=False):
    """Exact current release, scope, identity, single task and no retry."""
    from tools.tenancy import orchestration_contract as O
    block=backend.c['orchestration'];base,_=BF.resources(backend.c)
    if not isinstance(x,dict) or not re.fullmatch(re.escape(base+'/jobs/'+O.JOB+'/executions/'+O.JOB+'-')+r'[a-z0-9]+',x.get('name','')):
        fail('unknown controller execution identity')
    if at(x.get('createTime'))>backend.clock() or x.get('retriedCount',0)!=0:
        fail('unknown execution time/retry')
    O.verify_job({'template':{'taskCount':x.get('taskCount',1),'template':x.get('template',{})}},block)
    if active and (x.get('completionTime') or x.get('failedCount',0) or x.get('cancelledCount',0)):
        fail('unknown active controller state')
    if x.get('completionTime') and at(x['completionTime'])<at(x['createTime']):
        fail('unknown terminal controller state')
    return x


def inventory(backend):
    from tools.tenancy import orchestration_contract as O
    base,_=BF.resources(backend.c);name=base+'/jobs/'+O.JOB
    job=backend.request('GET',BF.RUN_API+'/'+name);O.verify_job(job,backend.c['orchestration'])
    values=BF.execution_inventory(backend.request,name)['executions']
    active=[execution(backend,x,active=True) for x in values if not x.get('completionTime')]
    own=getattr(backend,'current_execution',None)
    if not any(x['name']==own for x in active):fail('own active controller not visible')
    if job.get('runningCount',0)>len(active):fail('incomplete active controller inventory')
    return sorted(active,key=lambda x:(at(x['createTime']),x['name']))


def names(root,generation):
    if type(generation) is not int or not 1<=generation<=10000:fail('bounded controller fence exhausted')
    D.check_hash(root)
    return f'BFCW_{root}_{generation:010d}',f'BFCX_{root}_{generation:010d}'


def claim_value(claim):
    return {'kind':'controller_wake','root':claim['root'][:16]},D.encoded(claim)


def validate_claim(backend,value):
    p=backend.c['orchestration']['cadence'];env=backend.c['orchestration']['job']['env']
    claim=parse(value[1] or '{}') if value else {}
    if set(claim)!=CLAIM_FIELDS or type(claim['version']) is not int or claim['version']!=1:
        fail('controller claim unavailable/unknown')
    if claim['hash']!=D.digest({k:v for k,v in claim.items() if k!='hash'}):fail('claim digest differs')
    if (claim['tenant'],claim['project'],claim['root'],claim['policy_hash'],claim['source'],claim['image'])!=(p['tenant'],p['project'],p['root'],D.digest(p),env['CONTROLLER_SOURCE_SHA'],env['CONTROLLER_IMAGE']):
        fail('claim scope/release differs')
    names(claim['root'],claim['generation'])
    if value!=claim_value(claim) or at(claim['created_at'])>backend.clock():fail('claim metadata differs')
    return claim


def closed(backend,claim):
    ds=backend.c['datasets']['tenant_locks'];name=names(claim['root'],claim['generation'])[1]
    value=backend.tables.get_table(ds,name)
    if value is None:return False
    close=parse(value[1] or '{}')
    if (set(close)!=CLOSE_FIELDS or type(close['version']) is not int or close['version']!=1
            or close['claim_hash']!=claim['hash'] or close['status'] not in STATUSES
            or type(close['source_dispatches']) is not int or close['source_dispatches'] not in (0,1)
            or close['hash']!=D.digest({k:v for k,v in close.items() if k!='hash'})
            or value!=({'kind':'controller_close','root':claim['root'][:16]},D.encoded(close))
            or not at(claim['created_at'])<=at(close['closed_at'])<=backend.clock()):
        fail('controller closure unknown')
    return True


def acquire(backend):
    """Atomic generation CAS BEFORE any root/shard/source bookkeeping.

    Eventual list visibility alone cannot serialize two wakes. CAS loser never
    retries a claim or performs work in this wake. A terminal holder without its
    canonical closure is unknown, not an assumed expired/released authority.
    SOURCE lease stale-reclaim rules remain untouched.
    """
    if not enabled(backend):return None
    active=inventory(backend)
    from tools.tenancy.cloud_tick import OverlapWait
    if active[0]['name']!=backend.current_execution:raise OverlapWait('WAIT_ACTIVE_CONTROLLER')
    root=backend.c['orchestration']['cadence']['root'];ds=backend.c['datasets']['tenant_locks']
    prefix='BFCW_'+root+'_';found=[]
    for n,labels,created,expiry in backend.tables.list_tables(ds,with_expiry=True):
        if not n.startswith(prefix):continue
        suffix=n[len(prefix):]
        if not re.fullmatch(r'[0-9]{10}',suffix) or expiry:fail('controller fence retention/namespace differs')
        found.append(int(suffix))
    if sorted(found)!=list(range(1,max(found,default=0)+1)):fail('controller authority generation gap')
    latest=max(found,default=0)
    if latest:
        previous=validate_claim(backend,backend.tables.get_table(ds,names(root,latest)[0]))
        if not closed(backend,previous):
            holder=execution(backend,backend.request('GET',BF.RUN_API+'/'+previous['execution']))
            if holder.get('completionTime'):fail('terminal controller missing canonical closure')
            if holder['name']==backend.current_execution:fail('ambiguous claim creation; never retry')
            execution(backend,holder,active=True)
            raise OverlapWait('SKIP_AUTHORITY_HELD')
    generation=latest+1;env=backend.c['orchestration']['job']['env']
    claim=sealed(dict(version=1,tenant=backend.c['tenant_id'],project=backend.c['project_id'],root=root,
        policy_hash=D.digest(backend.c['orchestration']['cadence']),source=env['CONTROLLER_SOURCE_SHA'],
        image=env['CONTROLLER_IMAGE'],execution=backend.current_execution,generation=generation,
        created_at=backend.clock().isoformat()))
    name=names(root,generation)[0]
    if not backend.tables.create_marker(ds,name,*claim_value(claim)):
        winner=validate_claim(backend,backend.tables.get_table(ds,name))
        if winner['execution']==backend.current_execution:fail('ambiguous controller CAS; never repeat')
        holder=execution(backend,backend.request('GET',BF.RUN_API+'/'+winner['execution']))
        if holder.get('completionTime'):
            if not closed(backend,winner):fail('terminal CAS owner unproven')
            raise OverlapWait('WAIT_RECONCILIATION')
        execution(backend,holder,active=True)
        raise OverlapWait('SKIP_AUTHORITY_HELD')
    if validate_claim(backend,backend.tables.get_table(ds,name))!=claim:fail('claim readback mismatch')
    backend.wake_claim=claim
    return claim


def ensure(backend):
    if not enabled(backend):return
    claim=getattr(backend,'wake_claim',None)
    if not claim or claim['execution']!=backend.current_execution:fail('controller does not own orchestration fence')
    got=validate_claim(backend,backend.tables.get_table(backend.c['datasets']['tenant_locks'],names(claim['root'],claim['generation'])[0]))
    if got!=claim or closed(backend,claim):fail('controller authority changed/closed')


def runtime(backend,doc,x,job_name):
    """An active runtime is known only through its elected FULL shard intent.

    Matching a canonical job image alone is insufficient: ad-hoc execution of
    that same image must not become a healthy wait or a second source authority.
    """
    from tools.tenancy import full_history as F
    from tools.tenancy.cloud_tick import OverlapWait
    manifest=getattr(backend,'manifest',None);index=getattr(backend,'index',None)
    if not manifest or type(index) is not int:fail('active runtime lacks full scope')
    F.validate_leaf(manifest,index,doc,backend.clock().astimezone(BF.B.MSK).date())
    shard=F.shard_root(manifest,index,doc)
    records=backend.store.history(shard,max_records=F.MAX_LEAF_RECORDS)
    container=(x.get('template',{}).get('containers') or [{}])[0]
    env={e['name']:e.get('value') for e in container.get('env',[])}
    intents=[r for r in records if r['kind']=='DISPATCH_INTENT' and r['payload'].get('plan')==doc
             and r['payload'].get('preparation',{}).get('run_id')==env.get('INGESTION_RUN_ID')]
    if len(intents)!=1:fail('active runtime lacks unique elected intent')
    intent=intents[0];prep=intent['payload']['preparation']
    if intent['sequence']!=max(r['sequence'] for r in records if r['kind']=='DISPATCH_INTENT'):
        fail('active runtime is not latest source authority')
    base,jobs=BF.resources(backend.c);p=doc['runtime_plan']
    if prep['job']!=base+'/jobs/'+job_name or not re.fullmatch(re.escape(prep['job']+'/executions/'+job_name+'-')+r'[a-z0-9]+',x.get('name','')):
        fail('active runtime execution/job differs')
    expected={'ENTITIES':p['entity'],'SINCE':p['from'],'UNTIL':p['to'],
        'INGESTION_RUN_ID':prep['run_id'],'BACKFILL_MODE':BF.B.VERSION,
        'BACKFILL_TARGET_PROJECT':backend.c['project_id'],'BACKFILL_GENERATION':p['generation'],
        'BACKFILL_ORIGIN':p['origin'],'BACKFILL_MAX_REQUESTS':str(doc['max_requests']),
        'BACKFILL_MAX_UNITS':str(doc['max_units'])}
    if p['window_days']!=1:expected['BACKFILL_WINDOW_DAYS']=str(p['window_days'])
    expected.update(BF.continuation_overrides(doc))
    body={'overrides':{'containerOverrides':[{'env':[{'name':k,'value':v} for k,v in expected.items()]}]}}
    if prep.get('overrides')!=body or prep.get('ack_hash')!=doc['ack_hash'] or type(prep.get('lease_generation')) is not int or prep['lease_generation']<1:
        fail('active runtime source budgets/intent scope differs')
    BF.canonical_template({'template':{'taskCount':x.get('taskCount',1),'template':x.get('template',{})}},
        dict(jobs[job_name]['env'],**expected),BF.execution_image(doc,backend.c),
        backend.c['marketplaces']['ozon']['service_accounts']['runtime']+'@'+backend.c['project_id']+'.iam.gserviceaccount.com')
    if at(x.get('createTime'))>backend.clock() or x.get('retriedCount',0) or x.get('failedCount',0) or x.get('cancelledCount',0):
        fail('active source execution state unknown')
    receipts=[r for r in records if r['kind']=='DISPATCH_RECEIPT' and r['sequence']==intent['sequence']]
    if len(receipts)!=1 or receipts[0]['payload'].get('plan')!=doc:
        fail('active source receipt unavailable; no second source')
    receipt=receipts[0]['payload']['receipt']
    if any(receipt.get(k)!=prep[k] for k in ('run_id','lease_generation','ack_hash')):
        fail('active source receipt/intent mismatch')
    if any(r['kind']=='RECONCILED' and r['sequence']>=intent['sequence'] for r in records):
        fail('active source conflicts with accepted reconciliation')
    operation=backend.request('GET',BF.RUN_API+'/'+receipt['operation'])
    if operation.get('error'):fail('failed source operation cannot wait')
    response=operation.get('response',{})
    if response and response.get('name')!=x['name']:fail('source operation execution differs')
    if operation.get('done'):
        if response.get('completionTime') and response.get('succeededCount')==1:
            raise OverlapWait('WAIT_RECONCILIATION')
        fail('terminal source operation status unknown')
    if operation.get('metadata',{}).get('name')!=x['name']:
        fail('live operation/source execution linkage unknown')
    cid=BF.B.digest(['BOUNDED_PILOT_EXCLUSIVE',backend.c['project_id']])[:16]
    generation=prep['lease_generation'];locks=backend.c['datasets']['tenant_locks']
    lease=backend.tables.get_table(locks,BF.CK.lease_name(cid,generation))
    if (not lease or lease[0].get('owner')!=prep['run_id']
            or parse(lease[1] or '{}')!={'mode':'BOUNDED_PILOT','ack_hash':doc['ack_hash']}
            or not str(lease[0].get('until','')).isdigit()
            or backend.tables.get_table(locks,f'LD_{cid}_{generation:04d}') is not None):
        fail('live source lease/owner linkage unknown')
    return True


def close(backend,result):
    claim=getattr(backend,'wake_claim',None)
    if claim is None:return
    ensure(backend)
    status=result['status'];count=result.get('source_dispatches',0)
    if status not in STATUSES or type(count) is not int or count not in (0,1):fail('unknown controller terminal result')
    value=sealed(dict(version=1,claim_hash=claim['hash'],status=status,source_dispatches=count,closed_at=backend.clock().isoformat()))
    name=names(claim['root'],claim['generation'])[1];ds=backend.c['datasets']['tenant_locks']
    expected=({'kind':'controller_close','root':claim['root'][:16]},D.encoded(value))
    if not backend.tables.create_marker(ds,name,*expected) and backend.tables.get_table(ds,name)!=expected:
        fail('controller closure conflict')
    if not closed(backend,claim):fail('controller closure readback missing')
    backend.wake_claim=None


def percentile90(values):
    if not values:return None
    values=sorted(values);position=(len(values)-1)*.9
    low=int(position);high=min(low+1,len(values)-1)
    return values[low]+(values[high]-values[low])*(position-low)


def backoff(facts):
    """Pure decision for the owner-authorized qualification observer.

    This does not PATCH a Scheduler or grant the cloud reader that permission.
    The owner observer restores ONLY the dedicated cron after checking its exact
    target/timezone/profile. Healthy typed overlap waits are not errors.
    """
    required={'duplicate_intents','duplicate_runtimes','unresolved_stops',
        'cadence_throttles','material_quota_rejections','reconciliation_mismatches',
        'source_unit_duplicates','source_unit_gaps','unsafe_lease_contention',
        'security_regression','overlap_instability','material_overlaps','controller_durations'}
    if not isinstance(facts,dict) or set(facts)!=required:fail('backoff evidence schema unknown')
    counts=required-{'controller_durations','security_regression'}
    if any(type(facts[k]) is not int or facts[k]<0 for k in counts) or type(facts['security_regression']) is not bool:
        fail('backoff evidence unknown')
    ds=facts['controller_durations']
    if not isinstance(ds,list) or any(type(d) not in (int,float) or not 0<=d<=3600 for d in ds):fail('duration evidence unknown')
    unsafe=[k for k in counts-{'material_overlaps'} if facts[k]]
    if facts['security_regression']:unsafe.append('security_regression')
    # Persistence is assessed across two independent halves of at least ten
    # measured controllers, with real overlapping executions, not cron theory.
    if len(ds)>=10 and facts['material_overlaps']>=2:
        halves=(ds[:len(ds)//2],ds[len(ds)//2:])
        if all(percentile90(h)>=162 for h in halves):unsafe.append('persistent_3_min_latency')
    return dict(action='RESTORE_STABLE_10' if unsafe else 'KEEP',reasons=sorted(unsafe),
        controller_average=mean(ds) if ds else None,controller_p50=median(ds) if ds else None,
        controller_p90=percentile90(ds),sample_size=len(ds))
