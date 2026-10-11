"""First-authority recovery fault injection; no network or marketplace source."""
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
import pytest
from tools.tenancy import full_first_receipt_recovery as R, full_leaf_recovery as FL, full_controller as H, tenant_backfill as BF, durable_plan as D, cloud_controller as C, cloud_access as A, orchestration_contract as O
from tools.tenancy.full_first_receipt_image_check import fixture,check,NOW
from tools.tests.test_durable_plan import Backend as Records

def observed(monkeypatch):
    m,p,q,root,sh,recon,release,state=fixture();before=BF.B.initial(sh[-1]['payload']['plan']['runtime_plan']);memory=Records()
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
    units=[dict(ingestion_run_id=run['run_id']+'-u'+str(n),status='OK',backfill_sequence=n,
        backfill_detail_json=D.encoded(dict(action='DAY_COMPLETE',day=rp['from'],expected_unique_rows=0,transport_requests=1,transport_retries=0))) for n in range(1,2)]
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
        if 'lease_owner,evidence_json' in sql:
            current=deepcopy(state)
            if control['checkpoint']:current['sequence']=4
            return [dict(status='DONE',lease_generation=run['lease_generation']-(1 if control.get('prior_checkpoint') else 0),lease_owner=run['run_id'],evidence_json=D.encoded(dict(mode='BOUNDED_PILOT',proof=dict(state=current))))]
        if 'lease_generation>@current' in sql:return [dict(n=control['later'])]
        if 'backfill_id=@prior' in sql:
            prior=next(r for r in root if r['kind']=='CHUNK_PLAN' and D.digest(r)[:16]==params['prior'][1])
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


def test_packaged_first_receipt_contract():assert set(check().values())=={'PASS'}


@pytest.mark.parametrize('field',sorted(R.PREDICATES))
@pytest.mark.parametrize('value',[None,False,1,'UNKNOWN'])
def test_unknown_predicate_cannot_close_stop(field,value):
    m,p,q,root,sh,*_=fixture();q['predicates'][field]=value;q=R.sealed({k:v for k,v in q.items() if k!='hash'})
    with pytest.raises(BF.B.EvidenceError):R.validate(q,m,p,root,sh)


def test_first_observation_needs_no_fictitious_predecessor(monkeypatch):
    b,m,p,q,root,sh,control,memory,sent=observed(monkeypatch);saved=deepcopy((memory.rows,memory.meta.objects))
    proof=R.observe(b,m,p['initial_stop'],q['index'])
    assert proof['unit_start']==proof['unit_end']==proof['receipt_sequence']==1
    assert 'prior_receipt_hash' not in proof and proof['predicates']==dict.fromkeys(R.PREDICATES,True)
    assert proof['reconciliation']['coverage_readback']['catalog_linkage']=='PENDING_CURRENT_DAY_DQ'
    assert saved==(memory.rows,memory.meta.objects)
    assert all(method=='GET' or '/queries' in url for method,url in sent)


@pytest.mark.parametrize('fault',['failed','retry','active','second','later','unknown','successor','gap','duplicate','unit_owner','unit_retry','checkpoint','prior_checkpoint','lease','keys','schema','binding'])
def test_live_unknown_or_conflicting_evidence_blocks_without_writes(monkeypatch,fault):
    b,m,p,q,root,sh,control,memory,sent=observed(monkeypatch);control[fault]=1;saved=deepcopy((memory.rows,memory.meta.objects))
    with pytest.raises(BF.B.EvidenceError):R.observe(b,m,p['initial_stop'],q['index'])
    assert saved==(memory.rows,memory.meta.objects)


def test_additive_recovery_never_repeats_checkpoint_release_or_source(monkeypatch):
    b,m,p,q,root,sh,control,memory,sent=observed(monkeypatch);old=deepcopy(memory.rows);markers=deepcopy(memory.meta.objects)
    result=R.apply(b,m,p['initial_stop'],q['index'])
    assert result['source_calls']==0 and result['accepted_sequence']==1
    assert memory.rows[:len(old)]==old and len(memory.rows)==len(old)+2
    assert all(memory.meta.objects[k]==v for k,v in markers.items())
    assert len(R.load(b,m,b.store.history(m['hash'])))==1
    assert not any(r['kind']=='CHUNK_COMPLETE' for r in b.store.history(m['hash']))
    R.apply(b,m,p['initial_stop'],q['index']);assert len(memory.rows)==len(old)+2


@pytest.mark.parametrize('boundary',['before_recon','after_recon','after_recovery'])
def test_lost_append_ack_preserves_exact_fence_and_evidence(monkeypatch,boundary):
    b,m,p,q,root,sh,control,memory,sent=observed(monkeypatch);original=b.store.commit;once=[True]
    def commit(root,kind,*args):
        if once[0] and kind==('RECONCILED' if boundary!='after_recovery' else R.KIND):
            once[0]=False
            if boundary!='before_recon':original(root,kind,*args)
            raise BF.B.EvidenceError('synthetic lost acknowledgement')
        return original(root,kind,*args)
    monkeypatch.setattr(b.store,'commit',commit)
    with pytest.raises(BF.B.EvidenceError):R.apply(b,m,p['initial_stop'],q['index'])
    R.apply(b,m,p['initial_stop'],q['index'])
    assert len([r for r in b.store.history(q['shard']) if r['kind']=='RECONCILED'])==1
    assert len(R.load(b,m,b.store.history(m['hash'])))==1


def test_automatic_needs_initial_acceptance_and_exact_stage_context(monkeypatch):
    b,m,p,q,root,sh,*_=observed(monkeypatch)
    with pytest.raises(BF.B.EvidenceError):R.observe(b,m,p['initial_stop'],q['index'],automatic=True)
    R.apply(b,m,p['initial_stop'],q['index'])
    b.store.commit(m['hash'],'STOPPED',0,dict(reason='unsupported',diagnostic=dict(version=1,stage='MONITORING_WATERMARK',category='EVIDENCE')),NOW)
    assert R.recover_pending(b,m,b.store.history(m['hash'])) is None


def test_cloud_append_can_create_closed_certificate_but_never_owner_authority():
    c=BF.target('client_001');sent=[];access=A.CloudAccess(c,SimpleNamespace(token=lambda _: 'SYNTHETIC'),lambda *a:sent.append(a))
    def body(name):return dict(tableReference=dict(projectId=c['project_id'],datasetId=c['datasets']['tenant_locks'],tableId=name),schema=dict(fields=[dict(name='marker',type='STRING')]),labels={},description='{}')
    url=BF.TT.BQ+'/projects/'+c['project_id']+'/datasets/tenant_locks/tables'
    access.append_request('POST',url,body('BFFRF_'+'a'*64+'_'+'b'*64));assert len(sent)==1
    for name in ('BFFRF_AUTH_'+'a'*64+'_'+'b'*40,'BFFRF_bad','BFGO_'+'a'*64):
        with pytest.raises(BF.TT.TableError):access.append_request('POST',url,body(name))
    assert len(sent)==1


def test_midnight_source_persistence_is_not_a_catalog_linkage_exemption(monkeypatch):
    m,p,q,root,sh,recon,release,state=fixture();doc=sh[-1]['payload']['plan'];c=BF.target('client_001');state['rows']=1;appends=[]
    tables=SimpleNamespace(append=lambda *a:appends.append(a))
    monkeypatch.setattr(BF,'preflight',lambda *a,**kw:(tables,[]))
    monkeypatch.setattr(BF,'validate_plan',lambda *a:c)
    fault=[False];queries=[]
    def select(c,sql,params,request=None):
        queries.append(sql)
        if 'backfill_detail_json' in sql:return [dict(backfill_sequence=1,backfill_detail_json=D.encoded(dict(action='DAY_COMPLETE',day=doc['runtime_plan']['from'],expected_unique_rows=1)))]
        if 'SELECT DISTINCT evidence_json' in sql:return [dict(evidence_json=D.encoded(dict(state=state)))]
        if 'rows_n' in sql:return [dict(rows_n=2 if fault[0] else 1,keys_n=1)]
        if 'unresolved_sku_rows' in sql:return [dict(sku_rows=1,unresolved_sku_rows=1)]
        if 'HAVING COUNT(DISTINCT product_id)' in sql:return [dict(n=0)]
        raise AssertionError(sql)
    monkeypatch.setattr(BF,'select',select);backend=SimpleNamespace(request=lambda *a:(_ for _ in ()).throw(AssertionError('no source/transport')))
    with pytest.raises(BF.B.EvidenceError,match='PILOT_SKU_LINKAGE_UNPROVEN'):BF.verify_coverage(doc,doc['ack_hash'],backend=backend)
    queries.clear();proof=BF.verify_persistence(doc,doc['ack_hash'],backend=backend)
    assert proof['readback'][0]['rows']==proof['readback'][0]['keys']==1
    assert proof['catalog_linkage']=='PENDING_CURRENT_DAY_DQ' and proof['full_history']=='UNPROVEN'
    assert not appends and not any('RAW_OZON_CATALOG' in sql for sql in queries)
    fault[0]=True
    with pytest.raises(BF.B.EvidenceError,match='PILOT_DATA_RECONCILIATION_FAILED'):BF.verify_persistence(doc,doc['ack_hash'],backend=backend)
    assert not appends


def test_future_first_authority_recovers_under_standing_policy_without_replay(monkeypatch):
    from tools.tenancy import full_history as F
    b,m,p,q,root,sh,controls,memory,sent=observed(monkeypatch)
    R.apply(b,m,p['initial_stop'],q['index']);old_policy=deepcopy(R.policy(b,m));original=deepcopy(memory.rows)
    controls['_shift']=60
    index=next(i for i,x in enumerate(m['programs']) if x['entity']=='ads_expense_daily')
    doc=F.render_leaf(m,index,NOW.astimezone(BF.B.MSK).date());shard=F.shard_root(m,index,doc)
    def rec(kind,payload,scope=shard,seq=1):return dict(version=D.VERSION,root_hash=scope,kind=kind,sequence=seq,payload=payload)
    run=controls['_run'];run.update(run_id='bf-synthetic-next',lease_generation=3,ack_hash=doc['ack_hash'])
    state=controls['_state'];state.clear();state.update(BF.B.initial(doc['runtime_plan']));state.update(sequence=1,complete=True);state['progress']['next_day']='2026-10-07'
    controls['_env'].update(INGESTION_RUN_ID=run['run_id'],ENTITIES=doc['runtime_plan']['entity'],BACKFILL_GENERATION=doc['runtime_plan']['generation'],BACKFILL_ORIGIN=doc['runtime_plan']['origin'],**BF.continuation_overrides(doc))
    units=controls['_units'];units[:]=[dict(ingestion_run_id=run['run_id']+'-u1',status='OK',backfill_sequence=1,backfill_detail_json=D.encoded(dict(action='DAY_COMPLETE',day=doc['runtime_plan']['from'],expected_unique_rows=0,transport_requests=1,transport_retries=0)))]
    plan=rec('CHUNK_PLAN',dict(index=index,plan=doc,shard_root=shard),m['hash'],index)
    intent=rec('DISPATCH_INTENT',dict(plan=doc,preparation=deepcopy(run)));receipt=rec('DISPATCH_RECEIPT',dict(plan=doc,receipt=deepcopy(run)))
    stop=rec('STOPPED',dict(reason='same_class_new_path',controller_execution=root[-1]['payload']['controller_execution'],diagnostic=dict(version=1,stage='PERSISTED_SOURCE_RECONCILIATION',category='EVIDENCE',context=dict(index=index,shard=shard,sequence=1,intent_hash=D.digest(intent),receipt_hash=D.digest(receipt)))),m['hash'],0)
    root.extend([plan,stop]);sh[:]=[intent,receipt]
    for r in [plan,intent,receipt,stop]:b.store.commit(r['root_hash'],r['kind'],r['sequence'],r['payload'],NOW+timedelta(minutes=60))
    cid=BF.B.digest(['BOUNDED_PILOT_EXCLUSIVE',b.c['project_id']])[:16]
    memory.meta.objects[f'L_{cid}_0003']=(dict(owner=run['run_id']),D.encoded(dict(ack_hash=doc['ack_hash'])))
    memory.meta.objects[f'LD_{cid}_0003']=(dict(owner=run['run_id']),D.encoded(dict(ack_hash=doc['ack_hash'],operation=run['operation'])))
    result=R.recover_pending(b,m,b.store.history(m['hash']))
    assert result['source_calls']==0 and result['accepted_sequence']==1
    assert len(R.load(b,m,b.store.history(m['hash'])))==2 and R.policy(b,m)==old_policy
    assert memory.rows[:len(original)]==original
    assert all(method=='GET' or '/queries' in url for method,url in sent)
