"""Synthetic closed post-dispatch recovery, separate from temporal monitoring."""
from copy import deepcopy
import pytest
from tools.tenancy import controller_dispatch_recovery as R,durable_plan as D,tenant_backfill as BF
from tools.tenancy.controller_dispatch_image_check import fixture,seal,check


def test_packaged_closed_protocol():assert check()['typed_terminal_recovery']=='PASS'

@pytest.mark.parametrize('predicate',sorted(R.PREDICATES))
@pytest.mark.parametrize('bad',[None,False,1,'UNKNOWN'])
def test_every_unknown_predicate_blocks(predicate,bad):
    p,_,_=fixture();p['predicates'][predicate]=bad;seal(p)
    with pytest.raises(BF.B.EvidenceError):R.validate(p)

@pytest.mark.parametrize('fault',['scope','owner','deleted_stop','mutated_stop','missing_intent','receipt','run','lease','units','predecessor','successor','missing_recon','false_complete','source','boolean_version'])
def test_exact_scope_and_authority_adversaries(fault):
    p,rows,recon=fixture();rows.append(recon)
    if fault=='scope':p['root_hash']='3'*64
    if fault=='owner':p['authorization']['stop_hash']='3'*64
    if fault=='deleted_stop':rows.pop(5)
    if fault=='mutated_stop':rows[5]['payload']['reason']='erased'
    if fault=='missing_intent':rows.pop(3)
    if fault=='receipt':p['receipt_hash']='3'*64
    if fault=='run':p['run_id']='other';p['authorization']['run_id']='other'
    if fault=='lease':p['lease_generation']=3;p['authorization']['lease_generation']=3
    if fault=='units':p['unit_start']=1;p['authorization']['unit_start']=1
    if fault=='predecessor':p['predecessor_sequence']=2
    if fault=='successor':q=deepcopy(rows[3]);q['sequence']=3;rows.append(q)
    if fault=='missing_recon':rows.pop()
    if fault=='false_complete':rows[-1]['payload']['checkpoint']='DONE';p['reconciliation_hash']=D.digest(rows[-1])
    if fault=='source':p['runtime_source_sha']=None
    if fault=='boolean_version':p['version']=True;p['authorization']['version']=True
    seal(p)
    with pytest.raises((BF.B.EvidenceError,ValueError)):
        R.verify_records(p,rows,p['root_hash'],'client_001',publishing=True)


def test_later_normal_successor_does_not_revoke_immutable_recovery():
    p,rows,recon=fixture();rows.append(recon);q=deepcopy(rows[3]);q['sequence']=3;rows.append(q)
    R.verify_records(p,rows,p['root_hash'],'client_001')
    with pytest.raises(BF.B.EvidenceError):R.verify_records(p,rows,p['root_hash'],'client_001',publishing=True)


def test_missing_recovery_or_missing_reconciliation_never_clears_stop():
    p,rows,recon=fixture()
    with pytest.raises(BF.B.EvidenceError):D.decide_tick(rows,p['root_hash'],{'status':'ELIGIBLE'},verified_dispatch_recoveries=[p])
    rows.append(recon)
    with pytest.raises(BF.B.EvidenceError):D.decide_tick(rows,p['root_hash'],{'status':'ELIGIBLE'},verified_dispatch_recoveries=[p])
    assert D.decide_tick(rows,p['root_hash'],{'status':'ELIGIBLE'})['action']=='STOPPED'


def test_monitoring_fix_cannot_authorize_recovery():
    from tools.tenancy.controller_dispatch_image_check import check
    assert check()['monitoring_read_watermark']=='PASS'
    p,rows,recon=fixture();rows.append(recon)
    assert D.decide_tick(rows,p['root_hash'],{'status':'ELIGIBLE'})['action']=='STOPPED'


def observer_fixture(monkeypatch):
    import json
    from types import SimpleNamespace
    from tools.tenancy import controller_stop_recovery as CR,platform as PL
    p,rows,recon=fixture();c=BF.target('client_001');doc=rows[4]['payload']['plan']
    historical=json.loads((BF.REPO/'infra/tenant/releases/backfill/b799bf6108982142cef6706e333b1f677fbb630f.json').read_text())
    p['controller_source_sha']=historical['source_sha'];p['controller_image']=historical['image'];p['runtime_image']=historical['runtime_image']
    rr=[json.loads(f.read_text()) for f in (BF.REPO/PL.RUNTIME_RELEASES_DIR/'ozon').glob('*.json')]
    p['runtime_source_sha']=next(x for x in rr if x['image']==p['runtime_image'])['source']['commit']
    state=BF.B.initial(doc['runtime_plan']);state['sequence']=3;p['state_hash']=D.digest(state);seal(p)
    manifest={'version':D.VERSION,'root_hash':p['root_hash'],'kind':'MANIFEST','sequence':0,'payload':BF.QF.manifest()};rows.append(manifest)
    controls={'later':0,'active':False,'second_child':False,'lease_owner':'synthetic-current','later_gen':False,'retry':0,'missing_unit':False,'wrong_unit_owner':False,'keys':0,'hold':False,'unknown':0,'accepted_conflict':False,'time_order':False}
    c['orchestration']['job']['env']['HISTORICAL_SCHEDULER_STATE']='PAUSED'
    params={'stop_hash':'2026-10-08T09:05:00Z','intent_hash':'2026-10-08T09:04:00Z','receipt_hash':'2026-10-08T09:04:01Z','predecessor_reconciliation_hash':'2026-10-08T09:02:00Z'}
    index={D.digest(x):x for x in rows}
    def select(sql,args):
        assert sql.startswith('SELECT ')
        if 'SELECT DISTINCT evidence_json' in sql:
            accepted=deepcopy(state);accepted['sequence']=4 if controls['accepted_conflict'] else 1
            return [{'evidence_json':json.dumps({'proof':None})},{'evidence_json':json.dumps({'proof':{'state':accepted}})}]
        if 'backfill_id IN' in sql:
            return [{'evidence_json':json.dumps(index[p[k]]),'updated_at':('2026-10-08T09:01:00Z' if controls['time_order'] and k=='stop_hash' else t)} for k,t in params.items()]
        if 'attempts>@seq' in sql:return [{'n':controls['later']}]
        if 'backfill_detail_json' in sql:
            return [{'ingestion_run_id':('wrong' if controls['wrong_unit_owner'] and n==2 else p['run_id'])+'-u'+str(n),'status':'OK','backfill_sequence':n,'backfill_detail_json':json.dumps({'action':'SYNTHETIC_NO_SOURCE','transport_requests':0,'transport_retries':controls['retry'] if n==2 else 0})} for n in (range(1,3) if controls['missing_unit'] else range(1,4))]
        if 'rows_n' in sql:return [{'rows_n':0,'keys_n':controls['keys']}]
        if 'COUNT(*) AS n' in sql:return [{'n':controls['unknown']}]
        raise AssertionError(sql)
    base='projects/mpa-t-client-001/locations/europe-west1';jobs=[{'name':base+'/jobs/'+x} for x in ('tenant-control','tenant-backfill-controller','ozon-runtime-daily','ozon-runtime-fast','ozon-runtime-weekly')]
    def request(method,url):
        assert method=='GET'
        if url==BF.RUN_API+'/'+base+'/jobs?pageSize=1000':return {'jobs':jobs}
        if '/executions?' in url:
            if '/ozon-runtime-daily/' not in url:return {'executions':[]}
            x={'name':p['runtime_execution'],'createTime':'2026-10-08T09:04:00Z','completionTime':'2026-10-08T09:06:00Z'}
            if controls['active']:x.pop('completionTime')
            return {'executions':[x]+([dict(x,name=x['name']+'-second')] if controls['second_child'] else [])}
        if url.endswith(p['runtime_execution']):return {'startTime':'2026-10-08T09:04:00Z','completionTime':'2026-10-08T09:06:00Z','taskCount':1}
        if url.endswith(p['controller_execution']):return {'startTime':'2026-10-08T09:00:00Z'}
        raise AssertionError(url)
    lease={'owner':p['run_id']}
    def get(ds,name):
        if name.startswith('LD_'):return None
        return ({'owner':controls['lease_owner']},json.dumps({'ack_hash':doc['ack_hash']}))
    b=SimpleNamespace(c=c,store=SimpleNamespace(history=lambda _:rows),tables=SimpleNamespace(list_tables=lambda _:[],get_table=get),preflight=lambda *a,**k:None,active_runtime_execution=lambda:False,state=lambda _:state,request=request,select=select,journal=c['project_id']+'.ozon_raw.OZON_INGESTION_RUNS')
    before=deepcopy(state);before['sequence']=1
    monkeypatch.setattr(BF,'read_proof',lambda *a,**k:{'state':state})
    monkeypatch.setattr(CR,'terminal_executions',lambda *a:None)
    def ledger(*_):
        accepted=deepcopy(before)
        if controls['accepted_conflict']:accepted['sequence']=4
        return [],{},[{'entity':'supplies','plan_hash':doc['ack_hash'],'lease_generation':3 if controls['later_gen'] else 2,'evidence_json':json.dumps({'mode':'BOUNDED_PILOT','proof':{'state':accepted}})}],[],controls['hold']
    monkeypatch.setattr(BF.TL,'read_state',ledger)
    return b,p,rows,controls


def test_observer_is_read_only_and_exact(monkeypatch):
    b,p,rows,controls=observer_fixture(monkeypatch);before=deepcopy(rows)
    assert R.observe(b,p)[2]==2 and rows==before

@pytest.mark.parametrize('fault',['later','active','second_child','lease_owner','later_gen','retry','missing_unit','wrong_unit_owner','keys','hold','unknown','accepted_conflict','time_order'])
def test_live_predicate_counterexamples_block(monkeypatch,fault):
    b,p,rows,controls=observer_fixture(monkeypatch)
    controls[fault]='foreign' if fault=='lease_owner' else 1
    with pytest.raises(BF.B.EvidenceError):R.observe(b,p)


def test_source_budget_apply_contains_no_dispatch_or_coverage_path():
    import ast,inspect
    tree=ast.parse(inspect.getsource(R.apply))
    attrs={n.attr for n in ast.walk(tree) if isinstance(n,ast.Attribute)}
    assert not attrs.intersection({'start','run','verify_coverage','execute','recover_receipt'})
    assert 'reconcile' in attrs


def applier_fixture(monkeypatch):
    from types import SimpleNamespace
    from tools.tests.test_durable_plan import Backend,NOW
    from tools.tenancy import orchestration_contract as O,controller_stop_recovery as CR,pre_source_recovery as PR
    p,rows,_=fixture();memory=Backend();doc=rows[4]['payload']['plan'];r=rows[4]['payload']['receipt']
    result={'checkpoint':'RUNNING','sequence':3,'rows_observed':0,'orders':0,'supplies':0,'bundles':0,'coverage':'UNPROVEN_PENDING_DQ','lifecycle_changed':False,'scheduler_changed':False}
    record={'version':D.VERSION,'root_hash':p['root_hash'],'kind':'RECONCILED','sequence':2,'payload':result}
    p['reconciliation_hash']=D.digest(record);p['implementation_sha']=memory.c['orchestration']['job']['env']['CONTROLLER_SOURCE_SHA'];p['verified_at']=NOW.isoformat();seal(p)
    for x in rows:memory.store.commit(p['root_hash'],x['kind'],x['sequence'],x['payload'],NOW)
    memory.store.commit(p['root_hash'],'MANIFEST',0,BF.QF.manifest(),NOW)
    b=SimpleNamespace(c=memory.c,store=memory.store,tables=memory.meta,request=lambda *a:None)
    monkeypatch.setattr(O,'verify_artifact_source',lambda *a:None)
    monkeypatch.setattr(CR,'load',lambda *a:[]);monkeypatch.setattr(PR,'load',lambda *a:[])
    monkeypatch.setattr(CR,'terminal_executions',lambda *a:None)
    monkeypatch.setattr(R,'observe',lambda *a,**k:(doc,r,2))
    monkeypatch.setattr(BF,'read_proof',lambda *a,**k:{'state':dict(rows=0,orders=0,supplies=0,bundles=0)})
    counts={'bookkeeping':0,'fail':False}
    def reconcile(*a,**k):
        counts['bookkeeping']+=1
        cid=BF.B.digest(['BOUNDED_PILOT_EXCLUSIVE',b.c['project_id']])[:16]
        b.tables.create_marker(b.c['datasets']['tenant_locks'],f'LD_{cid}_0002',{'owner':r['run_id']},D.encoded({'ack_hash':doc['ack_hash']}))
        if counts['fail']:raise BF.B.EvidenceError('synthetic crash after bookkeeping')
        return result
    monkeypatch.setattr(BF,'reconcile',reconcile)
    def forbidden(*a,**k):raise AssertionError('source/coverage execution forbidden')
    monkeypatch.setattr(BF,'start',forbidden);monkeypatch.setattr(BF,'verify_coverage',forbidden)
    return b,p,memory,counts,NOW


def test_apply_preserves_history_and_is_idempotent_without_source_replay(monkeypatch):
    b,p,memory,counts,now=applier_fixture(monkeypatch);before=deepcopy(memory.rows)
    result=R.apply(b,p,now)
    assert result['source_calls']==result['source_dispatches']==0
    assert memory.rows[:len(before)]==before and len(memory.rows)==len(before)+2
    assert counts['bookkeeping']==1
    assert R.apply(b,p,now)==result and counts['bookkeeping']==1
    assert len(memory.rows)==len(before)+2
    assert D.decide_tick(b.store.history(p['root_hash']),p['root_hash'],{'status':'ELIGIBLE'},verified_dispatch_recoveries=R.load(b,b.store.history(p['root_hash']),p['root_hash']))['sequence']==3


def test_partial_bookkeeping_never_authorizes_dispatch_and_same_certificate_can_finish(monkeypatch):
    b,p,memory,counts,now=applier_fixture(monkeypatch);counts['fail']=True
    with pytest.raises(BF.B.EvidenceError):R.apply(b,p,now)
    assert D.decide_tick(b.store.history(p['root_hash']),p['root_hash'],{'status':'ELIGIBLE'})['action']=='STOPPED'
    assert not any(x['kind']==R.KIND for x in b.store.history(p['root_hash']))
    counts['fail']=False
    assert R.apply(b,p,now)['source_calls']==0
    assert counts['bookkeeping']==2


def test_failed_fresh_predicate_cannot_publish_owner_marker(monkeypatch):
    b,p,memory,counts,now=applier_fixture(monkeypatch);before=deepcopy(memory.meta.objects)
    def deny(*a,**k):raise BF.B.EvidenceError('unknown mandatory predicate')
    monkeypatch.setattr(R,'observe',deny)
    with pytest.raises(BF.B.EvidenceError):R.apply(b,p,now)
    assert memory.meta.objects==before and counts['bookkeeping']==0

@pytest.mark.parametrize('method,url,body',[
 ('POST','https://run.googleapis.com/v2/projects/mpa-t-client-001/locations/europe-west1/jobs/ozon-runtime-daily:run',{}),
 ('POST','https://api-seller.ozon.ru/v1/supply-order/list',{}),
 ('GET','https://secretmanager.googleapis.com/v1/projects/mpa-t-client-001/secrets/synthetic/versions/latest:access',None),
 ('POST','https://www.googleapis.com/bigquery/v2/projects/mpa-t-client-001/queries',{'query':'DELETE business'}),
 ('PATCH','https://run.googleapis.com/v2/synthetic',{})])
def test_zero_source_transport_denies_side_effects(method,url,body):
    from types import SimpleNamespace
    calls=[];b=R.SourceFreeBackend(SimpleNamespace(request=lambda *args:calls.append(args)))
    with pytest.raises(BF.B.EvidenceError):b.request(method,url,body)
    assert not calls


def test_zero_source_backend_has_no_dispatcher_or_coverage_method():
    from types import SimpleNamespace
    b=R.SourceFreeBackend(SimpleNamespace())
    for name in ('start','next_plan','recover_receipt','reconcile'):
        with pytest.raises(BF.B.EvidenceError):getattr(b,name)


def test_source_free_transport_rejects_foreign_tenant_reads():
    from types import SimpleNamespace
    calls=[];b=R.SourceFreeBackend(SimpleNamespace(c=BF.target('client_001'),request=lambda *a:calls.append(a)))
    with pytest.raises(BF.B.EvidenceError):b.request('GET','https://run.googleapis.com/v2/projects/foreign/locations/europe-west1/jobs/x')
    assert calls==[]


def test_canonical_reconcile_current_qualified_runtime_without_old_handoff(monkeypatch):
    from types import SimpleNamespace
    from datetime import datetime,timezone
    from tools.tests.test_durable_plan import Backend
    p,rows,_=fixture();memory=Backend();doc=rows[4]['payload']['plan'];r=rows[4]['payload']['receipt']
    p['runtime_image']=memory.c['marketplaces']['ozon']['runtime_image']
    p['implementation_sha']=memory.c['orchestration']['job']['env']['CONTROLLER_SOURCE_SHA'];seal(p)
    runtime_plan=doc['runtime_plan']
    env={'INGESTION_RUN_ID':r['run_id'],'BACKFILL_TARGET_PROJECT':memory.c['project_id'],
         'ENTITIES':runtime_plan['entity'],'SINCE':runtime_plan['from'],'UNTIL':runtime_plan['to'],
         'BACKFILL_MODE':BF.B.VERSION,'BACKFILL_GENERATION':runtime_plan['generation'],
         'BACKFILL_ORIGIN':runtime_plan['origin'],'TENANT_BINDING_REQUIRED':'1','STRICT_PAGE_CAPS':'1',
         'BACKFILL_MAX_REQUESTS':str(doc['max_requests']),'BACKFILL_MAX_UNITS':str(doc['max_units'])}
    if runtime_plan['window_days']!=1:env['BACKFILL_WINDOW_DAYS']=str(runtime_plan['window_days'])
    env.update(BF.continuation_overrides(doc))
    execution={'name':p['runtime_execution'],'completionTime':'2026-10-08T05:59:00Z','succeededCount':1,
       'template':{'serviceAccount':f"{memory.c['marketplaces']['ozon']['service_accounts']['runtime']}@{memory.c['project_id']}.iam.gserviceaccount.com",
                   'containers':[{'image':p['runtime_image'],'env':[{'name':k,'value':v} for k,v in env.items()]}]}}
    calls=[]
    def request(method,url,body=None):
        calls.append((method,url));assert method=='GET' and url.endswith(r['operation'])
        return {'done':True,'response':execution}
    inner=SimpleNamespace(c=memory.c,tables=memory.meta,request=request)
    guard=R.SourceFreeBackend(inner)
    memory.meta.create_marker(memory.c['datasets']['ref'],R.marker(p),*R.marker_value(p))
    monkeypatch.setattr(BF,'preflight',lambda *a,**k:(memory.meta,[]))
    monkeypatch.setattr(BF,'read_proof',lambda *a,**k:{'state':{'complete':False,'sequence':3,'rows':2,'orders':1,'supplies':1,'bundles':1}})
    memory.meta.create_marker(memory.c['datasets']['tenant_locks'],BF.CK.lease_name(BF.B.digest(['BOUNDED_PILOT_EXCLUSIVE',memory.c['project_id']])[:16],r['lease_generation']),{'owner':r['run_id']},D.encoded({'ack_hash':doc['ack_hash']}))
    appends=[];monkeypatch.setattr(memory.meta,'append',lambda ds,table,records:appends.append((ds,table,records)),raising=False)
    result=BF.reconcile(doc,doc['ack_hash'],r,backend=guard)
    assert 'deployment_handoff' not in result and result['checkpoint']=='RUNNING'
    assert BF.execution_image(doc,memory.c)==p['runtime_image']
    assert doc['image']!=p['runtime_image']
    assert result['coverage']=='UNPROVEN_PENDING_DQ'
    assert len(appends)==1 and appends[0][1]=='BACKFILL_CHECKPOINTS' and len(calls)==1
    assert memory.meta.get_table(memory.c['datasets']['tenant_locks'],f"LD_{BF.B.digest(['BOUNDED_PILOT_EXCLUSIVE',memory.c['project_id']])[:16]}_{r['lease_generation']:04d}")



def test_old_cloud_only_handoff_still_denies_owner_backend():
    from types import SimpleNamespace
    p,rows,_=fixture()
    with pytest.raises(BF.B.EvidenceError):BF.verify_paused_supplies_handoff(rows[4]["payload"]["plan"],BF.target("client_001"),rows[4]["payload"]["receipt"],{},SimpleNamespace())
