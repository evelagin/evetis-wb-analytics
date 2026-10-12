"""Network-disabled checks of local quarantine and diagnostic owner boundaries."""
from copy import deepcopy
from tools.tenancy import full_fbo_diagnostic_closure as Z,durable_plan as D,tenant_backfill as BF
from tools.tenancy.full_fbo_zero_effect_image_check import fixture as prior_fixture

def diagnostic_protocol():
    from tools.tenancy.controller_cadence_image_check import protocol
    from tools.tenancy import orchestration_contract as O
    from pipelines.ozon.runtime import full_resume as FR
    m,r,c,factory,x,objects,commits=protocol();m=FR.manifest()
    r['cadence_policy']['root']=m['hash']
    c['orchestration']=O.block(c,dict(release=r['source_sha'],root_hash=m['hash'],scheduler_state='ENABLED'),BF.REPO,r)
    for ex in x:ex['template']['containers'][0]['env']=[dict(name=k,value=v) for k,v in c['orchestration']['job']['env'].items()]
    canonical=deepcopy(x[0]['template'])
    def backend(index):
        b=factory(index);original=b.request
        b.request=lambda method,url,body=None:dict(name=url[len(BF.RUN_API)+1:],template=dict(taskCount=1,template=deepcopy(canonical))) if url.endswith('/jobs/'+O.JOB) else original(method,url,body)
        return b
    return m,r,c,backend,x,objects,commits

def diagnostic_policy(m,c,now):
    from datetime import date,timedelta
    import backfill_core as B
    p=m['programs'][269];a=B.utc_ms(p['from']);b=B.utc_ms((date.fromisoformat(p['to'])+timedelta(days=1)).isoformat());env=c['orchestration']['job']['env']
    v=dict(version=1,purpose='FBO_BOUNDED_READ_NO_BUSINESS_EFFECT_V1',tenant=m['tenant'],project=m['project'],root=m['hash'],leaf=269,shard='1'*64,plan_id='2'*64,failed_run='bf-02277a37-829d-45a4-90f4-f3ff8f9cb2fe',failed_generation=192,failed_receipt_hash='3'*64,closure_hash='4'*64,run_id='bf-00000000-0000-0000-0000-000000000193',generation=193,since=B.stamp(a),to=B.stamp(b-1),until_exclusive=B.stamp(b),cursor='',limit=100,request_limit=12,runtime_implementation_hash=BF.B.implementation_hash(),controller_source=env['CONTROLLER_SOURCE_SHA'],controller_image=c['orchestration']['job']['image'],runtime_image=c['marketplaces']['ozon']['runtime_image'],owner_decision_sha256='5'*64,expires_at=(now+timedelta(minutes=30)).isoformat())
    return dict(v,hash=D.digest(v))

def fixture():
    m,old,b,root,sh,objects,rows,effects,doc,rc=prior_fixture()
    original_cp=deepcopy(rows)
    p=dict(version=1,type=Z.TYPE,tenant=m['tenant'],project=m['project'],root=m['hash'],manifest_hash=D.digest(m),index=269,
        shard=old['shard'],plan_id=old['plan_id'],ack_hash=old['ack_hash'],stop_hash=old['stop_hash'],controller_execution=old['controller_execution'],
        execution=old['execution'],run_id=old['run_id'],generation=190,sequence=1,intent_hash=old['intent_hash'],receipt_hash=old['receipt_hash'],
        original_runtime_image=old['original_runtime_image'],controller_source=old['new_source'],controller_image=old['new_image'],
        runtime_image=old['runtime_image'],runtime_implementation_hash=old['runtime_implementation_hash'],
        owner_decision_sha256='4'*64,qualification_hash='5'*64,security_hash='6'*64,effect_hash='7'*64,checkpoint_hash=D.digest(rows),lease_hash=old['original_lease_hash'],
        protected_root_hashes=old['protected_root_hashes'],protected_shard_hashes=old['protected_shard_hashes'],predicates=dict.fromkeys(Z.PREDICATES,True),
        verified_at=old['verified_at'],source_budget=0,state_hash=old['state_hash'],validation_class='OUTSIDE_REQUESTED_INTERVAL',status='QUARANTINED_SOURCE_CONTRACT',isolation_scope='FBO_SOURCE')
    p=Z.sealed(p)
    original_select=b.select
    b.select=lambda *a:deepcopy(original_cp)
    b.state=lambda d:BF.B.initial(d['runtime_plan'])
    return m,p,b,root,sh,objects,rows,effects

def check():
    m,p,b,root,sh,objects,rows,effects=fixture();Z.header(p,m);Z.validate(p,b,m,root,final=False)
    for k in Z.PREDICATES:
        q=deepcopy(p);q['predicates'][k]=None;q=Z.sealed({k:v for k,v in q.items() if k!='hash'})
        try:Z.header(q,m)
        except Exception:pass
        else:raise AssertionError('unknown predicate permitted')
    assert not effects and not any(r['kind'] in ('RECONCILED','CHUNK_COMPLETE') for r in sh+root)
    from pipelines.ozon.runtime.fbo_diagnostic import validate
    try:validate({},m['project'],'0'*64,b.clock())
    except ValueError:pass
    else:raise AssertionError('empty owner policy accepted')
    from tools.tenancy import controller_cadence as CC,orchestration_contract as O,cloud_tick as T
    from types import SimpleNamespace
    cm,r,c,backend,x,objects,commits=diagnostic_protocol();leader=backend(0);dp=diagnostic_policy(cm,c,leader.clock())
    objects['BFDA_'+dp['hash']]=({'kind':'fbo_diagnostic','root':cm['hash'][:16]},D.encoded(dp))
    x[0]['template']['containers'][0]['args']=['--fbo-window-diagnostic',dp['hash']]
    CC.acquire(leader)
    try:CC.acquire(backend(1))
    except T.OverlapWait:pass
    else:raise AssertionError('diagnostic source slot overlaps ingestion')
    CC.close(leader,dict(status='MONITORING',source_dispatches=0));x[0].update(completionTime=leader.clock().isoformat(),succeededCount=1)
    assert CC.acquire(backend(1))['generation']==2 and not commits
    source='a'*40;settings=dict(release=source,root_hash=cm['hash'],scheduler_state='ENABLED')
    release=dict(source_sha=source,controller_implementation_hash=O.implementation_hash(BF.REPO),runtime_implementation_hash=BF.B.implementation_hash())
    assert not (BF.REPO/'infra/tenant/releases/backfill'/f'{source}.json').exists()
    current=SimpleNamespace(c={'datasets':{'tenant_locks':'locks'},'orchestration':{'job':{'env':dict(CONTROLLER_SOURCE_SHA=source,HISTORICAL_SCHEDULER_STATE='ENABLED')}}},tables=SimpleNamespace(get_table=lambda *a:({},D.encoded(dict(settings=settings,release=release)))))
    assert Z.qualified_release(current,cm,source)==release
    return {'fbo_local_quarantine_header':'PASS','fbo_quarantine_unknown_blocks':'PASS','fbo_quarantine_no_source_completion':'PASS','fbo_diagnostic_empty_authority_denied':'PASS','fbo_degraded_shared_source_fence':'PASS','fbo_current_descriptor_without_self_registration':'PASS'}

if __name__=='__main__':
    import json
    print(json.dumps(check(),sort_keys=True))
