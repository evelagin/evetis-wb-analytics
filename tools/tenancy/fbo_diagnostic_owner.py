"""Owner-authorized bounded diagnostic dispatch alongside degraded ingestion.

Consumes an immutable permission BEFORE Run POST. No source checkpoint/receipt,
source lease, RECON, completion or ordinary controller wake is generated here.
The normal controller CAS serializes this session with ingestion source work.
Scheduler stays enabled. The owner independently grants the exact runtime in ref;
without that grant the runtime cannot call the source. Global STOPs remain fences.
"""
from tools.tenancy import tenant_backfill as BF,durable_plan as D,orchestration_contract as O
from tools.tenancy.validation import parse_tenant_json as parse
from pipelines.ozon.runtime import fbo_diagnostic as Q

def fail(s):raise BF.B.EvidenceError('bounded FBO diagnostic: '+s)

def policy(b,h,*,retained=False):
    D.check_hash(h)
    marker=b.tables.get_table(b.c['datasets']['ref'],'BFDA_'+h)
    if not marker:fail('owner policy absent')
    p=parse(marker[1]);when=b.clock()
    if retained:
        from datetime import datetime,timezone
        expiry=datetime.fromisoformat(p['expires_at'].replace('Z','+00:00'))
        if expiry.utcoffset() is not None and expiry<=when:
            # Expired, unconsumed permission is a routine no-source condition.
            # Validate against actual immutable publication time, never a
            # fabricated clock or renewed permission inferred from elapsed time.
            table=b.request('GET',BF.TT.BQ+'/projects/'+b.c['project_id']+'/datasets/'+b.c['datasets']['ref']+'/tables/BFDA_'+h)
            when=datetime.fromtimestamp(int(table['creationTime'])/1000,timezone.utc)
    Q.validate(p,b.c['project_id'],BF.B.implementation_hash(),when)
    if marker!=({'kind':'fbo_diagnostic','root':p['root'][:16]},D.encoded(p)) or p['hash']!=h:fail('owner policy differs')
    env=b.c['orchestration']['job']['env']
    if (env['CONTROLLER_SOURCE_SHA'],b.c['orchestration']['job']['image'],b.c['marketplaces']['ozon']['runtime_image'])!=(p['controller_source'],p['controller_image'],p['runtime_image']):fail('qualified pair differs')
    descriptor=b.tables.get_table(b.c['datasets']['tenant_locks'],'BF_SPEC_'+p['controller_source']+'_'+p['root']+'_'+env['HISTORICAL_SCHEDULER_STATE'])
    if not descriptor:fail('qualified descriptor absent')
    r=parse(descriptor[1])['release'];O.verify_artifact_source(r,BF.REPO)
    if r.get('fbo_source_isolation_version')!=1 or r['verification'].get('fbo_source_isolation_adapter')!='PASS':fail('diagnostic qualification absent')
    return p

def isolated(b,p):
    from tools.tenancy import full_controller as H,full_fbo_diagnostic_closure as Z
    from tools.tenancy import recent_priority as RP
    m=H.only_manifest(b.store.history(p['root']));H.verify_authority(H.Backend(b,m),m)
    certificates=Z.load(b,m,b.store.history(p['root']))
    selected=[z for z in certificates if z['hash']==p['closure_hash']]
    if len(selected)!=1:fail('exact failed closure not accepted')
    cert=selected[0]
    if (cert['root'],cert['index'],cert['shard'],cert['plan_id'],cert['run_id'],cert['generation'],cert['receipt_hash'])!=(p['root'],p['leaf'],p['shard'],p['plan_id'],p['failed_run'],p['failed_generation'],p['failed_receipt_hash']):fail('diagnostic failed scope differs from accepted closure')
    priority=RP.load(b,m)
    if len(priority['selection'])!=69 or len(priority['parents'])!=64:fail('frozen priority differs')
    from tools.tenancy import controller_cadence as CC, stop_acceptance as SA
    records=b.store.history(p['root'])
    # The FULL STOP gate's own acceptance set: a private list omitted STOP819, f507, R1 and R2
    # and would have turned a qualified diagnostic into a new STOP.
    covered=SA.accepted_hashes(b,m,records,quarantines=certificates)
    if any(r['kind']=='STOPPED' and D.digest(r) not in covered for r in records):fail('global STOP remains')
    CC.ensure(b)
    plans=H.reconstruct_plans(records,m,b.clock());done=H.completed(records,m);quarantined=H.isolated_indices(m,certificates)
    candidates={i:v for i,v in plans.items() if i not in done and i not in quarantined}
    candidates.update({'dependency_'+str(i):r['payload'] for i,r in enumerate(records) if r['kind']=='DEPENDENCY_PLAN'})
    from tools.tenancy.cloud_tick import OverlapWait
    if RP.pending_receipts(b,candidates):raise OverlapWait('WAIT_RECONCILIATION')
    cid=BF.B.digest(['BOUNDED_PILOT_EXCLUSIVE',b.c['project_id']])[:16];ds=b.c['datasets']['tenant_locks']
    generations=[int(match[2]) for n,*_ in b.tables.list_tables(ds) if (match:=BF.CK.LEASE_RE.match(n)) and match[1]==cid]
    if generations and b.tables.get_table(ds,f'LD_{cid}_{max(generations):04d}') is None:raise OverlapWait('WAIT_RECONCILIATION')
    base,_=BF.resources(b.c)
    ss=b.request('GET',BF.SCHED_API+'/'+base+'/jobs?pageSize=100')
    if len(ss.get('jobs',[]))!=4 or ss.get('nextPageToken') or any(x['state']!=('ENABLED' if x['name'].endswith('/tenant-backfill-tick') else 'PAUSED') for x in ss['jobs']):fail('degraded Scheduler state differs')
    dedicated=next(x for x in ss['jobs'] if x['name'].endswith('/tenant-backfill-tick'))
    if dedicated['schedule']!='*/3 * * * *' or dedicated['timeZone']!='Europe/Moscow':fail('preserved cadence required')
    current=b.request('GET',BF.RUN_API+'/'+b.current_execution)
    if current.get('retriedCount',0) or current.get('cancelledCount',0) or current.get('taskCount')!=1:fail('single no-retry controller required')
    for name in (*b.c['marketplaces']['ozon']['jobs'],'tenant-control',O.JOB):
        xs=BF.execution_inventory(b.request,base+'/jobs/'+name)['executions']
        if name==O.JOB:CC.inventory(b) # Known peer wakes WAIT before source gates.
        elif any(not x.get('completionTime') for x in xs):
            from tools.tenancy.cloud_tick import OverlapWait
            raise OverlapWait('WAIT_ACTIVE_RUNTIME')
        j=b.request('GET',BF.RUN_API+'/'+base+'/jobs/'+name)
        if name==O.JOB:O.verify_job(j,b.c['orchestration'])
        elif name!='tenant-control':
            t=j['template'];co=t['template']['containers'][0];e={x['name']:x.get('value') for x in co['env']}
            if (co['image']!=p['runtime_image'] or t.get('taskCount',1)!=1 or t.get('parallelism',0) not in (0,1)
                or t['template'].get('maxRetries',0)!=0 or t['template']['serviceAccount']!='sa-ozon-runtime@'+p['project']+'.iam.gserviceaccount.com'
                or e.get('TENANT_BINDING_REQUIRED')!='1' or e.get('STRICT_PAGE_CAPS')!='1'):fail('runtime identity/caps differ')
    return m

def dispatch(b,h):
    p=policy(b,h);isolated(b,p)
    claim={'policy_hash':h,'run_id':p['run_id'],'generation':p['generation'],'controller_execution':b.current_execution,'source_request_limit':p['request_limit']}
    name='BFDC_'+h;value=({'kind':'fbo_diagnostic'},D.encoded(claim));ds=b.c['datasets']['tenant_locks']
    # A claim without an operation is UNCERTAIN, never permission to retry POST.
    if not b.tables.create_marker(ds,name,*value):fail('permission already consumed; never repeat Run POST')
    if b.tables.get_table(ds,name)!=value:fail('diagnostic claim conflict')
    base,jobs=BF.resources(b.c);job=next(n for n,v in jobs.items() if 'fbo_postings' in v['entities'])
    body={'overrides':{'containerOverrides':[{'args':['--fbo-window-diagnostic',h],
        'env':[{'name':'ENTITIES','value':'fbo_postings'},{'name':'INGESTION_RUN_ID','value':p['run_id']}]}]}}
    # Only this reviewed body/job is sent, using the existing controller Run SA.
    result=b.access.send('POST',BF.RUN_API+'/'+base+'/jobs/'+job+':run',body,
        {'Authorization':'Bearer '+b.access.tokens.token('reader'),'Content-Type':'application/json'})
    op=result.get('name');execution=result.get('metadata',{}).get('name')
    import re
    if not isinstance(op,str) or not op.startswith(base+'/operations/') or not isinstance(execution,str) or not re.fullmatch(re.escape(base+'/jobs/'+job+'/executions/'+job)+r'-[a-z0-9]+',execution):fail('uncertain diagnostic Run result; never repeat')
    record={'policy_hash':h,'run_id':p['run_id'],'generation':p['generation'],'operation':op,'execution':execution,'source_request_limit':p['request_limit']}
    v=({'kind':'fbo_diagnostic'},D.encoded(record))
    if not b.tables.create_marker(ds,'BFDO_'+h,*v) or b.tables.get_table(ds,'BFDO_'+h)!=v:fail('operation evidence conflict')
    return record

def await_terminal(b,p,record,*,sleep=None):
    import time
    sleep=sleep or time.sleep
    for _ in range(180):
        x=b.request('GET',BF.RUN_API+'/'+record['execution'])
        t=x.get('template',{});co=(t.get('containers') or [{}])[0];e={z['name']:z.get('value') for z in co.get('env',[])}
        if (x.get('name')!=record['execution'] or x.get('taskCount')!=1 or x.get('retriedCount',0) or x.get('cancelledCount',0)
            or co.get('image')!=p['runtime_image'] or co.get('args')!=['--fbo-window-diagnostic',p['hash']]
            or e.get('INGESTION_RUN_ID')!=p['run_id'] or e.get('ENTITIES')!='fbo_postings'
            or e.get('TENANT_BINDING_REQUIRED')!='1' or e.get('STRICT_PAGE_CAPS')!='1'
            or t.get('serviceAccount')!='sa-ozon-runtime@'+p['project']+'.iam.gserviceaccount.com'):
            fail('diagnostic child identity/effect boundary unknown; retain control fence')
        if x.get('completionTime'):
            if x.get('succeededCount',0)+x.get('failedCount',0)!=1:fail('diagnostic terminal state unknown; retain control fence')
            return dict(execution=record['execution'],terminal_success=x.get('succeededCount',0)==1,completion=x['completionTime'])
        sleep(5)
    fail('diagnostic child still active; retain control fence')

def queued(b):
    """One exact owner queue token; normal wakes share their existing CAS.

    The direct owner publishes this only after fresh qualification and an
    unconsumed diagnostic permission. No token means no change to ingestion.
    Consumed/unknown submissions are never repeated.
    """
    source=b.c['orchestration']['job']['env']['CONTROLLER_SOURCE_SHA']
    marker=b.tables.get_table(b.c['datasets']['ref'],'BFDQUEUE_'+source)
    if marker is None:return None
    value=parse(marker[1])
    if set(value)!={'policy_hash'}:fail('closed diagnostic queue token required')
    h=D.check_hash(value['policy_hash'])
    if marker[0]!={'kind':'fbo_diagnostic_queue'} or marker[1]!=D.encoded(value):fail('diagnostic queue token differs')
    consumed=b.tables.get_table(b.c['datasets']['tenant_locks'],'BFDC_'+h)
    if consumed is not None:
        # A terminal, canonically closed prior controller is checked by CAS.
        # Any missing operation after consumption is uncertain, never replayed.
        operation=b.tables.get_table(b.c['datasets']['tenant_locks'],'BFDO_'+h)
        if operation is None:fail('consumed diagnostic lacks operation; never repeat')
        claim=parse(consumed[1]);op=parse(operation[1])
        if (set(claim)!={'policy_hash','run_id','generation','controller_execution','source_request_limit'}
            or set(op)!={'policy_hash','run_id','generation','operation','execution','source_request_limit'}
            or consumed[0]!={'kind':'fbo_diagnostic'} or operation[0]!={'kind':'fbo_diagnostic'}
            or claim['policy_hash']!=h or any(claim[k]!=op[k] for k in ('policy_hash','run_id','generation','source_request_limit'))):fail('consumed diagnostic evidence differs; never repeat')
        return None
    p=policy(b,h,retained=True)
    from datetime import datetime
    if datetime.fromisoformat(p['expires_at'].replace('Z','+00:00'))<=b.clock():return None
    from tools.tenancy.cloud_tick import OverlapWait
    b.wake_activity['diagnostic_active']=True
    try:record=dispatch(b,h)
    except OverlapWait:
        b.wake_activity['diagnostic_active']=False
        return {'status':'WAITING','source_dispatches':0,'basis':'DIAGNOSTIC_SOURCE_SLOT_WAIT'}
    terminal=await_terminal(b,p,record)
    b.wake_activity['diagnostic_active']=False
    return {'status':'MONITORING','source_dispatches':0,'diagnostic_finished':True,
            'diagnostic_policy_hash':h,'diagnostic_terminal':terminal}

def main(h,env):
    from tools.tenancy.cloud_controller import bootstrap
    from tools.tenancy import controller_cadence as CC
    from tools.tenancy.cloud_tick import OverlapWait
    b,_=bootstrap(env)
    import json
    p=policy(b,h)
    try:
        CC.acquire(b)
        result=dispatch(b,h)
    except OverlapWait as wait:
        CC.close(b,{'status':'MONITORING','source_dispatches':0})
        print(json.dumps({'status':wait.status,'source_requests':0,'permission_consumed':False}));return 0
    # Never close this shared authority while its diagnostic child is active or
    # uncertain. Natural ingestion wakes observe/WAIT throughout this bounded slot.
    terminal=await_terminal(b,p,result)
    CC.close(b,{'status':'MONITORING','source_dispatches':0})
    print(json.dumps({'status':'DIAGNOSTIC_FINISHED','operation_evidence':result,'terminal':terminal,'ingestion_dispatches':0,'checkpoints':0,'reconciliation':0},sort_keys=True))
    return 0
