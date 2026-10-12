"""Frozen owner-authorized, once-only bounded diagnostic dispatch outside ingestion.

Consumes an immutable permission BEFORE Run POST. No source checkpoint/receipt,
source lease, RECON, completion or ordinary controller wake is generated here.
The owner independently grants the returned exact runtime execution in ref;
without that grant the runtime cannot call the source. Source STOPs remain fences.
"""
from tools.tenancy import tenant_backfill as BF,durable_plan as D,orchestration_contract as O
from tools.tenancy.validation import parse_tenant_json as parse
from pipelines.ozon.runtime import fbo_diagnostic as Q

def fail(s):raise BF.B.EvidenceError('bounded FBO diagnostic: '+s)

def policy(b,h):
    D.check_hash(h)
    marker=b.tables.get_table(b.c['datasets']['ref'],'BFDA_'+h)
    if not marker:fail('owner policy absent')
    p=parse(marker[1]);Q.validate(p,b.c['project_id'],BF.B.implementation_hash(),b.clock())
    if marker!=({'kind':'fbo_diagnostic','root':p['root'][:16]},D.encoded(p)) or p['hash']!=h:fail('owner policy differs')
    env=b.c['orchestration']['job']['env']
    if (env['CONTROLLER_SOURCE_SHA'],b.c['orchestration']['job']['image'],b.c['marketplaces']['ozon']['runtime_image'])!=(p['controller_source'],p['controller_image'],p['runtime_image']):fail('qualified pair differs')
    descriptor=b.tables.get_table(b.c['datasets']['tenant_locks'],'BF_SPEC_'+p['controller_source']+'_'+p['root']+'_'+env['HISTORICAL_SCHEDULER_STATE'])
    if not descriptor:fail('qualified descriptor absent')
    r=parse(descriptor[1])['release'];O.verify_artifact_source(r,BF.REPO)
    if r.get('fbo_source_isolation_version')!=1 or r['verification'].get('fbo_source_isolation_adapter')!='PASS':fail('diagnostic qualification absent')
    return p

def frozen(b,p):
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
    base,_=BF.resources(b.c)
    ss=b.request('GET',BF.SCHED_API+'/'+base+'/jobs?pageSize=100')
    if len(ss.get('jobs',[]))!=4 or ss.get('nextPageToken') or any(x['state']!='PAUSED' for x in ss['jobs']):fail('dedicated investigation freeze required')
    dedicated=next(x for x in ss['jobs'] if x['name'].endswith('/tenant-backfill-tick'))
    if dedicated['schedule']!='*/3 * * * *' or dedicated['timeZone']!='Europe/Moscow':fail('preserved cadence required')
    current=b.request('GET',BF.RUN_API+'/'+b.current_execution)
    if current.get('retriedCount',0) or current.get('cancelledCount',0) or current.get('taskCount')!=1:fail('single no-retry controller required')
    for name in (*b.c['marketplaces']['ozon']['jobs'],'tenant-control',O.JOB):
        xs=BF.execution_inventory(b.request,base+'/jobs/'+name)['executions']
        if any(not x.get('completionTime') and x['name']!=b.current_execution for x in xs):fail('conflicting active/queued execution')
        j=b.request('GET',BF.RUN_API+'/'+base+'/jobs/'+name)
        if name==O.JOB:O.verify_job(j,b.c['orchestration'])
        elif name!='tenant-control':
            t=j['template'];co=t['template']['containers'][0];e={x['name']:x.get('value') for x in co['env']}
            if (co['image']!=p['runtime_image'] or t.get('taskCount',1)!=1 or t.get('parallelism',0) not in (0,1)
                or t['template'].get('maxRetries',0)!=0 or t['template']['serviceAccount']!='sa-ozon-runtime@'+p['project']+'.iam.gserviceaccount.com'
                or e.get('TENANT_BINDING_REQUIRED')!='1' or e.get('STRICT_PAGE_CAPS')!='1'):fail('runtime identity/caps differ')
    return m

def dispatch(b,h):
    p=policy(b,h);frozen(b,p)
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

def main(h,env):
    from tools.tenancy.cloud_controller import bootstrap
    b,_=bootstrap(env)
    result=dispatch(b,h)
    import json
    print(json.dumps({'status':'DIAGNOSTIC_DISPATCHED','operation_evidence':result,'ingestion_dispatches':0,'checkpoints':0,'reconciliation':0},sort_keys=True))
