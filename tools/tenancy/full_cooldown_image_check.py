"""Installed pure FAILED-closure checks; synthetic evidence, no source or live GO."""
from copy import deepcopy
from tools.tenancy import full_cooldown_recovery as R,full_history as F,durable_plan as D,tenant_backfill as BF
from tools.tenancy import full_runtime_handoff as FH
from pipelines.ozon.runtime import full_resume as FR
import cooldown_failed as CF


def owner_preflight_check():
    """Exercise inherited FULL and canonical preflight with no live transport."""
    from contextlib import ExitStack
    from types import SimpleNamespace
    from unittest.mock import patch
    from datetime import datetime,timezone
    from tools.tenancy import full_controller as H,full_leaf_recovery as FL,orchestration_contract as O
    import lifecycle_core as L
    c=deepcopy(BF.target('client_001'));base,configs=BF.resources(c)
    release=dict(schema_version=1,image='europe-west1-docker.pkg.dev/mpa-platform/mpa-runtime/tenant-backfill-controller@sha256:'+'b'*64,
        source_sha='a'*40,runtime_image=c['marketplaces']['ozon']['runtime_image'],
        runtime_implementation_hash=BF.B.implementation_hash(),controller_implementation_hash='c'*64,
        verification={k:'PASS' for k in ('ci','exact_image','offline_restart','lost_post_no_repeat','quota_wait_no_source','tenant_isolation','reader_append_separation')})
    c['orchestration']=O.block(c,dict(release='a'*40,root_hash='d'*64,scheduler_state='PAUSED'),BF.REPO,release)
    block=c['orchestration'];jobs=[];schedules=[];calls=[]
    account=c['marketplaces']['ozon']['service_accounts']['runtime']+'@'+c['project_id']+'.iam.gserviceaccount.com'
    for n,cfg in configs.items():
        jobs.append(dict(name=base+'/jobs/'+n,template=dict(taskCount=1,template=dict(timeout='3600s',maxRetries=0,serviceAccount=account,
            containers=[dict(image=c['marketplaces']['ozon']['runtime_image'],env=[dict(name=k,value=v) for k,v in cfg['env'].items()])]))))
        schedules.append(dict(name=base+'/jobs/'+cfg['scheduler'],state='PAUSED'))
    jobs.append(dict(name=base+'/jobs/tenant-control',template=dict(taskCount=1,template=dict(timeout='3600s',maxRetries=0,serviceAccount=c['control']['email'],
        containers=[dict(image=c['marketplaces']['ozon']['runtime_image'],command=['python','lifecycle.py'],args=['status'],env=[dict(name=k,value=v) for k,v in c['control']['job']['env'].items()])]))))
    jobs.append(dict(name=base+'/jobs/'+O.JOB,template=dict(taskCount=1,parallelism=1,template=dict(timeout='600s',maxRetries=0,serviceAccount=block['accounts']['controller']['email'],
        containers=[dict(image=block['job']['image'],command=['python','-m','tools.tenancy.cloud_controller'],env=[dict(name=k,value=v) for k,v in block['job']['env'].items()])]))))
    s=block['scheduler'];schedules.append(dict(name=base+'/jobs/'+O.SCHEDULER,state='PAUSED',schedule=s['schedule'],timeZone=s['time_zone'],
        httpTarget=dict(uri=s['uri'],httpMethod='POST',body='e30=',oauthToken=dict(serviceAccountEmail=block['accounts']['wake']['email'],scope='https://www.googleapis.com/auth/cloud-platform'))))
    mode=None
    def request(method,url,body=None):
        assert method=='GET','source-free preflight attempted transport mutation';calls.append(url)
        if 'cloudscheduler' in url:return dict(jobs=schedules)
        if '/namespaces/' in url:return dict(items=[dict(metadata=dict(name=j['name'].split('/')[-1],labels={'cloud.googleapis.com/location':c['region']})) for j in jobs])
        if '/executions?' in url:
            active=mode is not None and '/jobs/'+mode+'/executions?' in url
            return dict(executions=[dict(name=url.split('/executions?')[0]+'/executions/active')] if active else [])
        if url.startswith(BF.TT.BQ):return {}
        return dict(jobs=jobs)
    original=SimpleNamespace(c=c,request=request,current_execution=None)
    observed=object.__new__(FL.SourceFreeObservation);observed.__dict__.update(original.__dict__)
    observed.original_backend=original;observed.manifest={'hash':'d'*64};observed.clock=lambda:datetime(2026,10,10,tzinfo=timezone.utc)
    observed.observation_tables=SimpleNamespace(list_tables=lambda *a,**kw:[])
    doc=BF.QF.accepted_doc(BF.QF.SKU);leaf=dict(hash=observed.manifest['hash'],plans=[doc])
    with ExitStack() as stack:
        for target,value in [(H,'verify_authority'),(BF,'validate_plan')]:
            stack.enter_context(patch.object(target,value,lambda *a:c))
        stack.enter_context(patch.object(BF.TL,'read_state',lambda *a:([],{},[],[],False)))
        stack.enter_context(patch.object(L,'current_state',lambda _:L.CAPABILITY_DISCOVERY))
        stack.enter_context(patch.object(BF.TL,'operator_binding',lambda *a:({'seller':'BOUND','performance':'BOUND'},{k:{'status':'PASS'} for k in ('seller','performance')})))
        observed.preflight(leaf)
        for mode in (O.JOB,'tenant-control','ozon-runtime-daily'):
            try:observed.preflight(leaf)
            except BF.B.EvidenceError:pass
            else:raise AssertionError('owner observation accepted active Job')
    assert calls and all(not 'ozon.ru' in u for u in calls)
    return 'PASS'


def check(*,artifact=True):
    m=FR.manifest();doc=F.render_leaf(m,CF.EXACT['index'],BF.datetime(2026,10,10).date())
    state=BF.B.initial(doc['runtime_plan']);state.update(sequence=3,requests=6,pages=3)
    state['progress']['rate_limit']={'count':3,'safe_cap':15,'eligible_at':'2026-10-09T20:03:39.579007+00:00'}
    assert D.digest(state)==CF.EXACT['state_hash']
    p=CF.sealed(dict(CF.EXACT,version=1,type=CF.TYPE,tenant=m['tenant'],project=m['project'],manifest_hash=D.digest(m),intent_hash='1'*64,
        original_runtime_source=m['runtime_source_sha'],original_runtime_image=m['runtime_image'],runtime_source='2'*40,
        runtime_image='europe-west1-docker.pkg.dev/mpa-platform/mpa-runtime/ozon-runtime@sha256:'+'2'*64,runtime_implementation_hash=BF.B.implementation_hash(),
        controller_source='3'*40,controller_image='europe-west1-docker.pkg.dev/mpa-platform/mpa-runtime/tenant-backfill-controller@sha256:'+'3'*64,controller_implementation_hash='4'*64,
        owner_ack_sha256='5'*64,endpoint_decision_sha256='6'*64,forensic_sha256='7'*64,endpoint_trace='UNPROVEN',static_source_effect_proof_accepted=True,source_budget=0))
    proof=CF.sealed(dict(CF.EXACT,version=1,type=CF.TYPE,tenant=m['tenant'],project=m['project'],policy_hash=p['hash'],intent_hash=p['intent_hash'],manifest_hash=p['manifest_hash'],
        failed_aggregate_hash='8'*64,unit_evidence_hash='9'*64,lease_hash='a'*64,checkpoint_hash='b'*64,prior_receipt_hash='c'*64,prior_recon_hash='d'*64,
        eligible_at=state['progress']['rate_limit']['eligible_at'],lease_release='LD_50c5d90dbf43ac9c_0169',lease_release_hash='e'*64,closure_hash='f'*64,
        predicates={k:True for k in CF.PREDICATES},verified_at='2026-10-10T08:00:00+00:00'))
    CF.proof(proof,p);closed=R.closure(proof)
    assert closed['failed'] is True and closed['source_complete'] is closed['persisted_reconciled'] is False and closed['source_budget']==0
    for predicate in CF.PREDICATES:
        for bad in (False,None,1,'UNKNOWN'):
            q=deepcopy(proof);q['predicates'][predicate]=bad;q=CF.sealed({k:v for k,v in q.items() if k!='hash'})
            try:CF.proof(q,p)
            except BF.B.EvidenceError:pass
            else:raise AssertionError('unknown mandatory predicate authorized FAILED recovery')
    for changed in ('run_id','receipt_hash','state_hash','root','shard'):
        q=deepcopy(proof);q[changed]='unknown';q=CF.sealed({k:v for k,v in q.items() if k!='hash'})
        try:CF.proof(q,p)
        except BF.B.EvidenceError:pass
        else:raise AssertionError('foreign incident accepted')
    if artifact:
        runtime=FH.runtime_facts(BF.target(m['tenant']),m)
        assert runtime['runtime_implementation_hash']==BF.B.implementation_hash()
    return dict(failed_cooldown_closed_incident='PASS',failed_cooldown_unknown_blocks='PASS',failed_cooldown_never_success_recon='PASS',full_frozen_runtime_handoff='PASS',owner_source_free_preflight=owner_preflight_check())


if __name__=='__main__':
    import json
    print(json.dumps(check(),sort_keys=True))
