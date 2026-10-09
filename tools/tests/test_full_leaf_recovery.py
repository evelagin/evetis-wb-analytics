"""Real durable CAS/readback and source-free predicate fault injection."""
from copy import deepcopy
import json
from datetime import timedelta
from types import SimpleNamespace
import pytest
from tools.tenancy import full_leaf_recovery as R, full_history as F, full_controller as H
from tools.tenancy import tenant_backfill as BF, durable_plan as D, cloud_controller as C
from tools.tenancy import cloud_access as A, orchestration_contract as O
from tools.tenancy.full_leaf_image_check import fixture,check,NOW
from tools.tests.test_durable_plan import Backend as Records


def test_packaged_contract():assert all(x=='PASS' for x in check().values())


@pytest.mark.parametrize('field',sorted(R.PREDICATES))
@pytest.mark.parametrize('bad',[None,False,1,'UNPROVEN'])
def test_every_predicate_is_boolean_and_required(field,bad):
    m,p,q,root,sh,*_=fixture();q['predicates'][field]=bad;q=R.sealed({k:v for k,v in q.items() if k!='hash'})
    with pytest.raises(BF.B.EvidenceError):R.validate(q,m,p,root,sh)


@pytest.mark.parametrize('fault',['foreign_root','foreign_shard','foreign_run','generation','lease','version','sequence','range','state','coverage','complete','extra','deleted_stop','missing_intent','missing_prior','successor'])
def test_certificate_and_topology_are_closed(fault):
    m,p,q,root,sh,*_=fixture()
    if fault=='foreign_root':q['root']='c'*64
    if fault=='foreign_shard':q['shard']='c'*64
    if fault=='foreign_run':q['run_id']='foreign'
    if fault=='generation':q['generation']=True
    if fault=='lease':q['lease_release']='LD_foreign'
    if fault=='version':q['version']=True
    if fault=='sequence':q['receipt_sequence']=1
    if fault=='range':q['unit_start']=1
    if fault=='state':q['state_hash']='c'*64
    if fault=='coverage':q['coverage_hash']='c'*64
    if fault=='complete':q['reconciliation']['source_complete']=True
    if fault=='extra':q['extra']='unreviewed'
    if fault=='deleted_stop':root.pop()
    if fault=='missing_intent':sh.pop(3)
    if fault=='missing_prior':sh.pop(2)
    if fault=='successor':sh.append(dict(sh[-1],sequence=3))
    q=R.sealed({k:v for k,v in q.items() if k!='hash'})
    with pytest.raises(BF.B.EvidenceError):R.validate(q,m,p,root,sh)


@pytest.mark.parametrize('fault',['foreign','runtime','image','unknown','initial','automatic'])
def test_owner_policy_cannot_be_granted_by_code_or_a_loose_descriptor(fault):
    m,p,_,_,_,_,release,*_=fixture()
    if fault=='foreign':p['tenant']='client_002'
    if fault=='runtime':p['runtime_source']='c'*40
    if fault=='image':p['controller_image']='c'*64
    if fault=='unknown':p['extra']=True
    if fault=='initial':p['initial_generation']=True
    if fault=='automatic':p['automatic_class_c']='yes'
    p=R.sealed({k:v for k,v in p.items() if k!='hash'})
    with pytest.raises(BF.B.EvidenceError):R.validate_policy(p,m,release)


def observed(monkeypatch):
    m,p,q,root,sh,recon,release,before,state=fixture();memory=Records()
    c=deepcopy(memory.c)
    c['orchestration']=O.block(c,dict(release=p['controller_source'],root_hash=m['hash'],scheduler_state='PAUSED'),BF.REPO,release=release)
    for r in root+sh:memory.store.commit(r['root_hash'],r['kind'],r['sequence'],r['payload'],NOW)
    memory.meta.create_marker(c['datasets']['ref'],R.policy_name(m['hash'],p['controller_source']),*R.policy_value(p))
    descriptor=dict(settings=dict(release=p['controller_source'],root_hash=m['hash'],scheduler_state='PAUSED'),release=release)
    memory.meta.create_marker(c['datasets']['tenant_locks'],C.descriptor_name(p['controller_source'],m['hash'],'PAUSED'),{'kind':'descriptor'},D.encoded(descriptor))
    run=sh[-1]['payload']['receipt'];doc=sh[-1]['payload']['plan'];rp=doc['runtime_plan']
    lease=(dict(owner=run['run_id']),D.encoded(dict(ack_hash=doc['ack_hash'])))
    cid=BF.B.digest(['BOUNDED_PILOT_EXCLUSIVE',c['project_id']])[:16]
    memory.meta.objects[f'L_{cid}_0002']=lease
    memory.meta.objects[f'LD_{cid}_0002']=(lease[0],D.encoded(dict(ack_hash=doc['ack_hash'],operation=run['operation'])))
    control={'failed':False,'retry':0,'active':False,'second':False,'later':0,'unknown':0,'successor':0,'gap':False,'duplicate':False,'unit_owner':False,'unit_retry':0,'checkpoint':False,'lease':False,'keys':0,'schema':False,'binding':False}
    units=[dict(ingestion_run_id=('bf-synthetic-prior' if n==1 else run['run_id'])+'-u'+str(n),status='OK',backfill_sequence=n,
        backfill_detail_json=D.encoded(dict(action='ORDER_BATCH',source_items=0,transport_requests=0,transport_retries=0))) for n in range(1,4)]
    def stamp(minute):return (NOW.replace(hour=6,minute=minute,second=0)+timedelta(minutes=control.get('_shift',0))).isoformat()
    def select(sql,params):
        assert sql.startswith('SELECT ') and ';' not in sql
        if 'SELECT ingestion_run_id,status' in sql:
            out=deepcopy(units)
            if control['gap']:out.pop()
            if control['duplicate']:out.append(out[-1])
            if control['unit_owner']:out[-1]['ingestion_run_id']='foreign-u3'
            if control['unit_retry']:out[-1]['backfill_detail_json']=D.encoded(dict(action='ORDER_BATCH',transport_requests=1,transport_retries=1))
            return out
        if 'lease_generation BETWEEN' in sql:
            current=deepcopy(state)
            if control['checkpoint']:current['sequence']=4
            return [dict(lease_generation=n,lease_owner=('bf-synthetic-prior' if n!=run['lease_generation'] else run['run_id']),evidence_json=D.encoded(dict(proof=dict(state=s)))) for n,s in [(run['lease_generation']-1,before),(run['lease_generation'],current)]]
        if 'lease_generation>@current' in sql:return [dict(n=control['later'])]
        if 'backfill_id=@prior' in sql:
            prior=max((r for r in sh if r['kind']=='RECONCILED'),key=lambda r:r['sequence'])
            return [dict(evidence_json=D.encoded(prior),updated_at=stamp(0))]
        if 'backfill_id IN' in sql:
            return [dict(evidence_json=D.encoded(r),updated_at=t) for r,t in [(sh[-2],stamp(1)),(sh[-1],stamp(1)),(root[-1],stamp(5))]]
        if 'NOT (ingestion_run_id=@run' in sql:return [dict(n=control['unknown'])]
        if 'attempts>@seq' in sql:return [dict(n=control['successor'])]
        if 'SELECT DISTINCT backfill_sequence' in sql:return [{k:u[k] for k in ('backfill_sequence','backfill_detail_json')} for u in units]
        if 'SELECT DISTINCT evidence_json' in sql:return [dict(evidence_json=D.encoded(dict(state=state)))]
        if 'rows_n' in sql:return [dict(rows_n=0,keys_n=control['keys'])]
        raise AssertionError(sql)
    base='projects/mpa-t-client-001/locations/europe-west1'
    jobs=[dict(name=base+'/jobs/'+x) for x in (*c['marketplaces']['ozon']['jobs'],'tenant-control','tenant-backfill-controller')]
    env=dict(INGESTION_RUN_ID=run['run_id'],ENTITIES=rp['entity'],TENANT_BINDING_REQUIRED='1',STRICT_PAGE_CAPS='1',BACKFILL_TARGET_PROJECT=c['project_id'],
        SINCE=rp['from'],UNTIL=rp['to'],BACKFILL_GENERATION=rp['generation'],BACKFILL_ORIGIN=rp['origin'],BACKFILL_MODE=BF.B.VERSION,
        BACKFILL_MAX_REQUESTS=str(doc['max_requests']),BACKFILL_MAX_UNITS=str(doc['max_units']),**BF.continuation_overrides(doc))
    sent=[]
    def request(method,url,body=None):
        sent.append((method,url))
        if method=='POST':
            assert url==BF.TT.BQ+'/projects/'+c['project_id']+'/queries';A.select_body(c,body)
            params={x['name']:(x['parameterType']['type'],x['parameterValue']['value']) for x in body.get('queryParameters',[])}
            rows=select(body['query'],params);fields=list(rows[0]) if rows else []
            types={k:('INTEGER' if type(rows[0][k]) is int else 'STRING') for k in fields}
            return dict(jobReference=dict(jobId='synthetic'),jobComplete=True,schema=dict(fields=[dict(name=k,type=types[k]) for k in fields]),rows=[dict(f=[dict(v=str(r[k])) for k in fields]) for r in rows])
        assert method=='GET'
        if url.endswith('/jobs?pageSize=1000'):return dict(jobs=jobs)
        if '/executions?' in url:
            if '/jobs/ozon-runtime-daily/' not in url:return dict(executions=[])
            e=dict(name=q['runtime_execution'],createTime=stamp(1),completionTime=stamp(2))
            if control['active']:e.pop('completionTime')
            return dict(executions=[e]+([dict(e,name=e['name']+'x')] if control['second'] else []))
        if url.endswith('/operations/synthetic'):
            response=dict(name=q['runtime_execution'],completionTime=stamp(2),taskCount=1,succeededCount=1,
                retriedCount=control['retry'],template=dict(serviceAccount='sa-ozon-runtime@mpa-t-client-001.iam.gserviceaccount.com',containers=[dict(image=p['runtime_image'],env=[dict(name=k,value=v) for k,v in env.items()])]))
            if control['schema']:response['taskCount']=2
            return dict(done=True,response=response,**(dict(error=dict(code=10)) if control['failed'] else {}))
        if '/tenant-backfill-controller/executions/' in url:return dict(startTime=stamp(4),completionTime=stamp(6))
        raise AssertionError(url)
    b=SimpleNamespace(c=c,tables=memory.meta,store=memory.store,clock=lambda:NOW+timedelta(minutes=control.get('_shift',0)),current_execution=None,state=lambda _:state,select=select,request=request)
    monkeypatch.setattr(H,'verify_authority',lambda *a: (_ for _ in ()).throw(BF.B.EvidenceError('binding regression')) if control['binding'] else c)
    monkeypatch.setattr(BF,'read_proof',lambda *a,**k:dict(state=state))
    # Keep concrete coverage SQL and transport; only external preflight inventory is
    # tested separately by the canonical security suite rather than invented here.
    monkeypatch.setattr(BF,'preflight',lambda *a,backend,**k:(backend.tables,[]))
    monkeypatch.setattr(BF,'validate_plan',lambda *a:c)
    original=memory.meta.get_table
    def get(ds,name):
        if control['lease'] and name.startswith('LD_'):return None
        return original(ds,name)
    memory.meta.get_table=get
    control.update(_run=run,_units=units,_state=state,_before=before,_env=env,_q=q,_root=root,_sh=sh)
    return b,m,p,q,root,sh,control,memory,sent


def test_concrete_observation_does_not_write_checkpoint_lease_coverage_or_source(monkeypatch):
    b,m,p,q,root,sh,control,memory,sent=observed(monkeypatch);snapshot=deepcopy((memory.rows,memory.meta.objects))
    proof=R.observe(b,m,p['initial_stop'],q['index'])
    assert proof['unit_start']==2 and proof['unit_end']==3 and proof['predicates']==dict.fromkeys(R.PREDICATES,True)
    assert snapshot==(memory.rows,memory.meta.objects)
    assert all(method=='GET' or '/queries' in url for method,url in sent)


@pytest.mark.parametrize('fault',['failed','retry','active','second','later','unknown','successor','gap','duplicate','unit_owner','unit_retry','checkpoint','lease','keys','schema','binding'])
def test_fresh_live_mandatory_faults_block_without_mutation(monkeypatch,fault):
    b,m,p,q,root,sh,control,memory,sent=observed(monkeypatch);control[fault]=1;snapshot=deepcopy((memory.rows,memory.meta.objects))
    with pytest.raises(BF.B.EvidenceError):R.observe(b,m,p['initial_stop'],q['index'])
    assert snapshot==(memory.rows,memory.meta.objects)


def test_exact_additive_apply_and_duplicate_call_preserve_originals(monkeypatch):
    b,m,p,q,root,sh,control,memory,sent=observed(monkeypatch);old=deepcopy(memory.rows);markers=deepcopy(memory.meta.objects)
    result=R.apply(b,m,p['initial_stop'],q['index'])
    assert result['source_calls']==0 and result['accepted_sequence']==3
    assert len(memory.rows)==len(old)+2 and memory.rows[:len(old)]==old
    assert all(memory.meta.objects[k]==v for k,v in markers.items())
    assert len(R.load(b,m,b.store.history(m['hash'])))==1
    R.apply(b,m,p['initial_stop'],q['index']);assert len(memory.rows)==len(old)+2


@pytest.mark.parametrize('boundary',['before_recon','after_recon','after_recovery'])
def test_lost_append_ack_resumes_exact_certificate_without_rewriting_bookkeeping(monkeypatch,boundary):
    b,m,p,q,root,sh,control,memory,sent=observed(monkeypatch);original=b.store.commit;crash=[True]
    def commit(root,kind,*args):
        target=kind==('RECONCILED' if boundary!='after_recovery' else R.KIND)
        if crash[0] and target:
            crash[0]=False
            if boundary!='before_recon':original(root,kind,*args)
            raise BF.B.EvidenceError('synthetic lost acknowledgement')
        return original(root,kind,*args)
    monkeypatch.setattr(b.store,'commit',commit)
    with pytest.raises(BF.B.EvidenceError):R.apply(b,m,p['initial_stop'],q['index'])
    out=R.apply(b,m,p['initial_stop'],q['index']);assert out['source_calls']==0
    assert len([r for r in b.store.history(q['shard']) if r['kind']=='RECONCILED' and r['sequence']==2])==1
    assert len(R.load(b,m,b.store.history(m['hash'])))==1


def test_automatic_requires_initial_acceptance_and_exact_typed_leaf_context(monkeypatch):
    b,m,p,q,root,sh,control,memory,sent=observed(monkeypatch)
    with pytest.raises(BF.B.EvidenceError):R.observe(b,m,p['initial_stop'],q['index'],automatic=True)
    assert R.recover_pending(b,m,b.store.history(m['hash'])) is None
    R.apply(b,m,p['initial_stop'],q['index'])
    # A new generic/monitoring STOP is never exempt even after initial activation.
    b.store.commit(m['hash'],'STOPPED',0,dict(reason='different',diagnostic=dict(version=1,stage='MONITORING_WATERMARK',category='EVIDENCE')),NOW)
    assert R.recover_pending(b,m,b.store.history(m['hash'])) is None


def test_observation_wrapper_forbids_authority_writes_and_marketplace_transport(monkeypatch):
    b,m,p,q,*_=observed(monkeypatch);o=R.observer(b,m,q['index'])
    for operation in (lambda:o.store.commit('a'*64,'STOPPED',0,{},NOW),lambda:o.tables.create_marker('tenant_locks','x',{},''),lambda:o.append_request('POST','x',{}),lambda:o.dispatch({}),lambda:o.reconcile({}),lambda:o.request('POST','https://api-seller.ozon.ru/v1/roles',{}),lambda:o.request('GET','https://secretmanager.googleapis.com/v1/projects/mpa-t-client-001/secrets/x/versions/latest:access')):
        with pytest.raises(BF.B.EvidenceError):operation()


def test_cloud_append_allows_only_certificate_fence_not_owner_policy():
    c=BF.target('client_001');sent=[]
    access=A.CloudAccess(c,SimpleNamespace(token=lambda _: 'SYNTHETIC'),lambda *a:sent.append(a))
    def body(name):return dict(tableReference=dict(projectId=c['project_id'],datasetId=c['datasets']['tenant_locks'],tableId=name),schema=dict(fields=[dict(name='marker',type='STRING')]),labels={},description='{}')
    url=BF.TT.BQ+'/projects/'+c['project_id']+'/datasets/tenant_locks/tables'
    access.append_request('POST',url,body('BFFLR_'+'a'*64+'_'+'b'*64));assert len(sent)==1
    for name in ('BFFLR_AUTH_'+'a'*64+'_'+'b'*40,'BFFLR_bad','BFGO_'+'a'*64):
        with pytest.raises(BF.TT.TableError):access.append_request('POST',url,body(name))
    assert len(sent)==1


@pytest.mark.parametrize('crash',[False,True])
def test_future_same_class_automatically_recovers_without_new_owner_policy(monkeypatch,crash):
    b,m,p,q,root,sh,controls,memory,sent=observed(monkeypatch)
    first=R.apply(b,m,p['initial_stop'],q['index'])
    initial_certificate=deepcopy(R.load(b,m,b.store.history(m['hash']))[0])
    old_policy=deepcopy(R.policy(b,m));old_ld=deepcopy(memory.meta.objects[first['lease_release']])
    controls['_shift']=60
    controls['_before'].update(deepcopy(controls['_state']))
    controls['_state']['sequence']=5
    run=controls['_run'];run.update(run_id='bf-synthetic-next',lease_generation=3)
    controls['_env']['INGESTION_RUN_ID']=run['run_id']
    q['runtime_execution']=q['runtime_execution']+'next'
    shard=q['shard'];doc=sh[-1]['payload']['plan']
    intent_payload=dict(plan=doc,preparation=deepcopy(run));receipt_payload=dict(plan=doc,receipt=deepcopy(run))
    ih=b.store.commit(shard,'DISPATCH_INTENT',3,intent_payload,NOW)
    rh=b.store.commit(shard,'DISPATCH_RECEIPT',3,receipt_payload,NOW)
    diagnostic=dict(version=1,stage='RECONCILIATION_APPEND',category='CLOUD_METADATA',
        context=dict(index=q['index'],shard=shard,sequence=3,intent_hash=ih,receipt_hash=rh))
    stop=b.store.commit(m['hash'],'STOPPED',0,dict(reason='CONTROLLER_GATE_OR_EVIDENCE_FAILURE',
        controller_execution=root[-1]['payload']['controller_execution'],diagnostic=diagnostic),NOW)
    sh[:]=b.store.history(shard);sh.sort(key=lambda r:(r['sequence'],{'DISPATCH_INTENT':0,'DISPATCH_RECEIPT':1,'RECONCILED':2}[r['kind']]))
    root[:]=b.store.history(m['hash']);new_stop=next(r for r in root if D.digest(r)==stop)
    root.remove(new_stop);root.append(new_stop)
    for n in (4,5):controls['_units'].append(dict(ingestion_run_id=run['run_id']+'-u'+str(n),status='OK',backfill_sequence=n,backfill_detail_json=D.encoded(dict(action='ORDER_BATCH',source_items=0,transport_requests=0,transport_retries=0))))
    cid=BF.B.digest(['BOUNDED_PILOT_EXCLUSIVE',b.c['project_id']])[:16]
    memory.meta.objects[f'L_{cid}_0003']=(dict(owner=run['run_id']),D.encoded(dict(ack_hash=doc['ack_hash'])))
    memory.meta.objects[f'LD_{cid}_0003']=(dict(owner=run['run_id']),D.encoded(dict(ack_hash=doc['ack_hash'],operation=run['operation'])))
    original=b.store.commit;fail=[crash]
    def commit(root,kind,*args):
        if fail[0] and kind==R.KIND:
            fail[0]=False;raise BF.B.EvidenceError('synthetic interruption after RECON')
        return original(root,kind,*args)
    monkeypatch.setattr(b.store,'commit',commit)
    if crash:
        with pytest.raises(BF.B.EvidenceError):R.recover_pending(b,m,b.store.history(m['hash']))
    out=R.recover_pending(b,m,b.store.history(m['hash']))
    assert out['source_calls']==0 and out['accepted_sequence']==5
    approved=R.load(b,m,b.store.history(m['hash']))
    assert len(approved)==2 and initial_certificate in approved
    assert R.policy(b,m)==old_policy and memory.meta.objects[first['lease_release']]==old_ld
    assert R.recover_pending(b,m,b.store.history(m['hash'])) is None
