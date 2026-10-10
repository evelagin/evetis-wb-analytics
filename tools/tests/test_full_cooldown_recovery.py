"""Exact failed incident protocol and independent fail-closed source-free gates."""
import copy,json
from datetime import datetime,timezone
from types import SimpleNamespace
import pytest
from tools.tenancy import full_cooldown_recovery as R,full_history as F,full_controller as H,full_leaf_recovery as FL
from tools.tenancy import durable_plan as D,tenant_backfill as BF,cloud_controller as C
from pipelines.ozon.runtime import full_resume as FR
import cooldown_failed as CF

NOW=datetime(2026,10,10,8,tzinfo=timezone.utc)

def fixture(monkeypatch):
    m=FR.manifest();doc=F.render_leaf(m,88,NOW.date());state=BF.B.initial(doc['runtime_plan'])
    state.update(sequence=3,requests=6,pages=3);state['progress']['rate_limit']={'count':3,'safe_cap':15,'eligible_at':'2026-10-09T20:03:39.579007+00:00'}
    assert D.digest(state)==CF.EXACT['state_hash']
    def record(root,kind,seq,payload):return dict(version=D.VERSION,root_hash=root,kind=kind,sequence=seq,payload=payload)
    r={'ack_hash':doc['ack_hash'],'lease_generation':169,'operation':'projects/mpa-t-client-001/locations/europe-west1/operations/a1035035-8c9b-443d-b4f2-c4df72f6bf49','run_id':CF.EXACT['run_id']}
    rec=record(CF.EXACT['shard'],'DISPATCH_RECEIPT',4,{'plan':doc,'receipt':r});assert D.digest(rec)==CF.EXACT['receipt_hash']
    intent=record(CF.EXACT['shard'],'DISPATCH_INTENT',4,{'plan':doc,'preparation':{k:r[k] for k in ('ack_hash','lease_generation','run_id')}})
    stop=record(m['hash'],'STOPPED',0,{'reason':'synthetic old QUOTA gate'})
    monkeypatch.setattr(CF,'EXACT',dict(CF.EXACT,stop_hash=D.digest(stop)))
    p=CF.sealed(dict(CF.EXACT,version=1,type=CF.TYPE,tenant='client_001',project='mpa-t-client-001',manifest_hash=D.digest(m),intent_hash=D.digest(intent),original_runtime_source=m['runtime_source_sha'],original_runtime_image=m['runtime_image'],runtime_source='1'*40,runtime_image='europe-west1-docker.pkg.dev/mpa-platform/mpa-runtime/ozon-runtime@sha256:'+'1'*64,runtime_implementation_hash=BF.B.implementation_hash(),controller_source='2'*40,controller_image='europe-west1-docker.pkg.dev/mpa-platform/mpa-runtime/tenant-backfill-controller@sha256:'+'2'*64,controller_implementation_hash='3'*64,owner_ack_sha256='4'*64,endpoint_decision_sha256='5'*64,forensic_sha256='6'*64,endpoint_trace='UNPROVEN',static_source_effect_proof_accepted=True,source_budget=0))
    root=[record(m['hash'],'FULL_MANIFEST',0,m),record(m['hash'],'CHUNK_PLAN',88,{'index':88,'plan':doc,'shard_root':p['shard']}),stop]
    previous=record(CF.EXACT['shard'],'DISPATCH_RECEIPT',3,{'plan':doc,'receipt':dict(r,run_id='synthetic-prior',lease_generation=168)})
    prior=record(CF.EXACT['shard'],'RECONCILED',3,dict(sequence=3,state_hash=D.digest(state),persisted_reconciled=True,source_complete=False))
    sh=[previous,prior,intent,rec]
    base_name,jobs=BF.resources(BF.target('client_001'));xname=base_name+'/jobs/ozon-runtime-daily/executions/'+p['runtime_execution']
    env={'INGESTION_RUN_ID':p['run_id'],'ENTITIES':'ads_sku_daily','BACKFILL_TARGET_PROJECT':p['project'],'BACKFILL_MODE':BF.B.VERSION,'SINCE':'2026-10-05','UNTIL':'2026-10-07','BACKFILL_GENERATION':doc['runtime_plan']['generation'],'BACKFILL_ORIGIN':doc['runtime_plan']['origin'],'TENANT_BINDING_REQUIRED':'1','STRICT_PAGE_CAPS':'1','BACKFILL_MAX_REQUESTS':'400','BACKFILL_MAX_UNITS':'20'}
    x={'name':xname,'completionTime':'2026-10-09T19:24:14Z','startTime':'2026-10-09T19:23:44Z','createTime':'2026-10-09T19:23:40Z','taskCount':1,'failedCount':1,'template':{'serviceAccount':'sa-ozon-runtime@mpa-t-client-001.iam.gserviceaccount.com','containers':[{'image':p['original_runtime_image'],'env':[dict(name=k,value=v) for k,v in env.items()]}]}}
    rows=[]
    for n in (1,2,3):
        s=copy.deepcopy(state);s['sequence']=n
        detail={'action':'SOURCE_THROTTLED','source_diagnostic':{'http_status':429}}
        rows.append(dict(ingestion_run_id='synthetic-prior-'+str(n),status='OK',backfill_plan_id=p['plan_id'],backfill_sequence=n,evidence_json=D.encoded({'plan':doc['runtime_plan'],'state':s}),backfill_detail_json=D.encoded(detail),error_message=None,requests=3,retry_count=0,rows_received=0,rows_inserted=0,rows_updated=0))
    rows.append(dict(ingestion_run_id=p['run_id'],status='FAILED',backfill_plan_id=None,backfill_sequence=None,evidence_json=None,backfill_detail_json=None,error_message="EvidenceError('SOURCE_REPEATED_LIMIT_OWNER_REVIEW')",requests=3,retry_count=0,rows_received=0,rows_inserted=0,rows_updated=0))
    lease=({'owner':p['run_id']},D.encoded({'mode':'BOUNDED_PILOT','ack_hash':doc['ack_hash']}))
    cp=[dict(status='RUNNING',lease_generation=169,lease_owner=p['run_id'],error_code=None,evidence_json=D.encoded({'mode':'BOUNDED_PILOT','proof':None}))]
    metadata={};reads=[]
    c=copy.deepcopy(BF.target('client_001'));c['orchestration']['job']['env']['HISTORICAL_SCHEDULER_STATE']='PAUSED'
    def request(method,url,body=None):
        assert method=='GET','source-free check attempted transport mutation';reads.append(url)
        if '/operations/' in url:return {'done':True,'error':{'code':10},'metadata':{'name':xname}}
        if url==BF.RUN_API+'/'+xname:return x
        if '/executions?' in url:return {'executions':[x]} if '/ozon-runtime-daily/' in url else {'executions':[]}
        if '/jobs?' in url:return {'jobs':[dict(user_email='sa-ozon-runtime@mpa-t-client-001.iam.gserviceaccount.com',jobReference=dict(jobId=str(n),location='EU')) for n in (1,2)]}
        if '/jobs/' in url and url.startswith(BF.TT.BQ):return {'statistics':{'query':{'statementType':'SELECT'}},'status':{}}
        raise AssertionError(url)
    def select(q,params):
        if 'ORDER BY started_at LIMIT 50' in q:return rows
        if 'INFORMATION_SCHEMA.COLUMNS' in q:return [{'table_name':'RAW_SYNTHETIC_'+str(n),'column_name':'ingestion_run_id'} for n in range(14)]
        if ' UNION ALL ' in q:return [{'table_name':'RAW_SYNTHETIC_'+str(n),'n':0} for n in range(14)]
        if 'lease_generation=@generation' in q:return cp
        raise AssertionError(q)
    b=SimpleNamespace(c=c,current_execution=None,clock=lambda:NOW,store=SimpleNamespace(history=lambda root,**kw:copy.deepcopy(sh if root==p['shard'] else root_records)),request=request,select=select,journal=p['project']+'.ozon_raw.OZON_INGESTION_RUNS',state=lambda d:copy.deepcopy(state),tables=SimpleNamespace(get_table=lambda ds,n:lease if n=='L_50c5d90dbf43ac9c_0169' else metadata.get(n),list_tables=lambda ds:[('L_50c5d90dbf43ac9c_0169',lease[0],NOW)]),preflight=lambda leaf:None)
    root_records=root
    monkeypatch.setattr(R,'policy',lambda *a,**kw:p);monkeypatch.setattr(FL,'observer',lambda *a:b);monkeypatch.setattr(H,'verify_authority',lambda *a:True)
    return b,m,p,root,sh,rows,x,cp,reads


def test_source_free_observation_preserves_failed_and_has_no_recon(monkeypatch):
    b,m,p,root,sh,rows,x,cp,reads=fixture(monkeypatch);before=copy.deepcopy((root,sh,rows,cp))
    proof,payload,*_=R.observe(b,m)
    assert all(v is True for v in proof['predicates'].values()) and payload['failed'] is True
    assert payload['source_complete'] is payload['persisted_reconciled'] is False and payload['source_budget']==0
    assert (root,sh,rows,cp)==before and reads and not any(r['kind']=='RECONCILED' and r['sequence']==4 for r in sh)
    rec=dict(version=D.VERSION,root_hash=p['shard'],kind=R.SHARD_KIND,sequence=4,payload=payload)
    R.validate(proof,p,m,root,sh+[rec],final=True)


@pytest.mark.parametrize('fault',['success','retry','image','binding','scope','new_unit','post','uuid','rows','failed_error','failed_plan','checkpoint','active','successor'])
def test_new_unsupported_or_unknown_effect_never_qualifies(monkeypatch,fault):
    b,m,p,root,sh,rows,x,cp,reads=fixture(monkeypatch)
    if fault=='success':x.update(failedCount=0,succeededCount=1)
    if fault=='retry':x['retriedCount']=1
    if fault=='image':x['template']['containers'][0]['image']='foreign'
    if fault=='binding':x['template']['containers'][0]['env'].append({'name':'STRICT_PAGE_CAPS','value':'0'})
    if fault=='scope':x['template']['containers'][0]['env'][4]['value']='2026-10-06'
    if fault=='new_unit':rows.append(dict(rows[0],backfill_sequence=4))
    if fault in {'post','uuid'}:
        z=rows[0];e=json.loads(z['evidence_json']);e['state']['progress']['report']={'phase':'INTENT' if fault=='post' else 'POLL','uuid':'synthetic'};z['evidence_json']=D.encoded(e)
    if fault=='rows':rows[-1]['rows_inserted']=1
    if fault=='failed_error':rows[-1]['error_message']='unknown'
    if fault=='failed_plan':rows[-1]['backfill_plan_id']='foreign'
    if fault=='checkpoint':cp[0]['lease_owner']='foreign'
    if fault=='active':x.pop('completionTime')
    if fault=='successor':sh.append(dict(sh[-1],sequence=5))
    with pytest.raises(BF.B.EvidenceError):R.observe(b,m)


@pytest.mark.parametrize('predicate',sorted(CF.PREDICATES))
@pytest.mark.parametrize('bad',[False,None,1,'UNKNOWN'])
def test_no_unknown_predicate_can_authorize_closure(monkeypatch,predicate,bad):
    b,m,p,root,sh,*_=fixture(monkeypatch);proof,*_=R.observe(b,m)
    proof['predicates'][predicate]=bad;proof=CF.sealed({k:v for k,v in proof.items() if k!='hash'})
    with pytest.raises(BF.B.EvidenceError):R.validate(proof,p,m,root,sh)


def test_unverified_marker_cannot_skip_failed_receipt(monkeypatch):
    b,m,p,root,sh,*_=fixture(monkeypatch);proof,payload,*_=R.observe(b,m)
    with pytest.raises(BF.B.EvidenceError,match='lacks committed closure'):
        D.decide_tick(sh,p['shard'],{'status':'ELIGIBLE'},verified_failed_cooldown=[proof])
    # Closed contiguous real authorities still gate the pure dispatcher first.
    history=[]
    for n in (1,2,3):
        history.extend([dict(sh[-2],sequence=n),dict(sh[-1],sequence=n),dict(sh[-1],kind='RECONCILED',sequence=n)])
    history+=sh[-2:]+[dict(version=D.VERSION,root_hash=p['shard'],kind=R.SHARD_KIND,sequence=4,payload=payload)]
    assert D.decide_tick(history,p['shard'],{'status':'ELIGIBLE'},verified_failed_cooldown=[proof])=={'action':'PREPARE_NEXT','sequence':5,'poll_only':False}
    assert not any(r['kind']=='RECONCILED' and r['sequence']==4 for r in history)


def writable_protocol(monkeypatch):
    b,m,p,root,sh,rows,x,cp,reads=fixture(monkeypatch)
    lease=b.tables.get_table('tenant_locks','L_50c5d90dbf43ac9c_0169')
    objects={'L_50c5d90dbf43ac9c_0169':lease,R.name(m['hash'],p['controller_source']):R.value(p)}
    writes=[]
    def create(ds,n,labels,description):
        writes.append(('marker',n))
        if n in objects:return False
        objects[n]=(copy.deepcopy(labels),description);return True
    def append(ds,table,values):
        assert ds=='tenant_ops' and table=='BACKFILL_CHECKPOINTS'
        writes.append(('append',table));cp.extend(copy.deepcopy(values))
    b.tables=SimpleNamespace(get_table=lambda ds,n:objects.get(n),list_tables=lambda ds:[(n,v[0],NOW) for n,v in objects.items()],create_marker=create,append=append)
    read=b.select
    def select(q,params):
        if 'SELECT evidence_json' in q and "status='FAILED'" in q:return [{'evidence_json':z['evidence_json']} for z in cp if z['status']=='FAILED']
        if 'SELECT DISTINCT evidence_json' in q and "status='FAILED'" in q:return [{'evidence_json':s} for s in sorted({z['evidence_json'] for z in cp if z['status']=='FAILED'})]
        if 'SELECT DISTINCT status,lease_owner' in q:return [{k:z[k] for k in ('status','lease_owner','error_code','evidence_json')} for z in cp]
        return read(q,params)
    b.select=select
    def commit(r,kind,seq,payload,when):
        assert kind in {R.KIND,R.SHARD_KIND},'FAILED closure must not fabricate success/reconciliation'
        v=dict(version=D.VERSION,root_hash=r,kind=kind,sequence=seq,payload=copy.deepcopy(payload))
        target=sh if r==p['shard'] else root
        if v not in target:target.append(v);writes.append(('record',kind))
        return D.digest(v)
    b.store.commit=commit
    monkeypatch.setattr(R,'validate_policy',lambda *a,**kw:p)
    return b,m,p,root,sh,cp,objects,writes


def test_additive_failed_closure_is_idempotent_and_never_marks_success(monkeypatch):
    b,m,p,root,sh,cp,objects,writes=writable_protocol(monkeypatch)
    original=copy.deepcopy((root,sh,cp));proof=R.apply(b,m);before=copy.deepcopy(writes)
    assert R.apply(b,m)==proof and writes==before
    assert root[:len(original[0])]==original[0] and sh[:len(original[1])]==original[1] and cp[0]==original[2][0]
    assert {z['status'] for z in cp}=={'RUNNING','FAILED'}
    assert not any(z['kind']=='RECONCILED' and z['sequence']==4 for z in sh)
    assert proof['lease_release'] in objects and R.load(b,m,root)==[proof]
    cp[-1]['error_code']='SUCCESS'
    with pytest.raises(BF.B.EvidenceError,match='checkpoint linkage changed'):R.load(b,m,root)


@pytest.mark.parametrize('stage',['checkpoint','release','shard','root'])
def test_interrupted_closure_retries_only_its_additive_evidence(monkeypatch,stage):
    b,m,p,root,sh,cp,objects,writes=writable_protocol(monkeypatch)
    fired=[]
    def crash():
        if not fired:fired.append(True);raise RuntimeError('synthetic lost append response')
    if stage=='checkpoint':
        old=b.tables.append
        def append(*a):old(*a);crash()
        b.tables.append=append
    elif stage=='release':
        old=b.tables.create_marker
        def create(*a):
            result=old(*a)
            if a[1].startswith('LD_'):crash()
            return result
        b.tables.create_marker=create
    else:
        old=b.store.commit
        def commit(*a):
            result=old(*a)
            if a[1]==(R.KIND if stage=='root' else R.SHARD_KIND):crash()
            return result
        b.store.commit=commit
    with pytest.raises(RuntimeError):R.apply(b,m)
    proof=R.apply(b,m)
    assert R.load(b,m,root)==[proof] and len(cp)==2
    assert len([z for z in sh if z['kind']==R.SHARD_KIND])==1
    assert len([z for z in root if z['kind']==R.KIND])==1
    assert not any(z['kind']=='RECONCILED' and z['sequence']==4 for z in sh)


def test_network_free_packaged_failed_protocol():
    from tools.tenancy.full_cooldown_image_check import check
    assert set(check(artifact=False).values())=={'PASS'}


@pytest.mark.parametrize('entity',['ads_sku_daily','ads_expense_daily','ads_campaigns','supplies'])
def test_source_class_cooldown_crosses_leaves_without_blocking_seller(monkeypatch,entity):
    m=FR.manifest();doc=F.render_leaf(m,88,NOW.date());s=BF.B.initial(doc['runtime_plan'])
    s['sequence']=3;s['progress']['rate_limit']={'count':3,'safe_cap':15,'eligible_at':'2026-10-10T09:00:00+00:00'}
    plans={88:dict(plan=doc)}
    monkeypatch.setattr(H,'reconstruct_plans',lambda *a:plans)
    monkeypatch.setattr(C.Backend,'quota',lambda *a:dict(status='ELIGIBLE',allowance=15))
    seen=[]
    def select(q,params):
        seen.append(q)
        if 'COUNT(*) AS n' in q:return [{'n':0}]
        assert 'MAX(backfill_sequence) OVER' in q
        return [dict(plan_id=doc['runtime_plan']['plan_id'],backfill_sequence=3,evidence_json=D.encoded({'plan':doc['runtime_plan'],'state':s}))]
    b=H.Backend(SimpleNamespace(c=BF.target('client_001'),select=select,store=SimpleNamespace(history=lambda *a:[])),m)
    chosen=copy.deepcopy(doc);chosen['runtime_plan']['entity']=entity
    q=b.quota({'plans':[chosen]},NOW)
    if entity.startswith('ads_'):
        assert q==dict(status='WAITING',allowance=0,eligible_at=s['progress']['rate_limit']['eligible_at'],basis='SOURCE_COOLDOWN')
    else:assert q['status']=='ELIGIBLE'
    assert len(seen)==2


def test_old_unqualified_runtime_is_rejected_for_new_live_execution(monkeypatch):
    m=FR.manifest();doc=F.render_leaf(m,88,NOW.date())
    c=copy.deepcopy(BF.target(m['tenant']));c['marketplaces']['ozon']['runtime_image']=m['runtime_image']
    monkeypatch.setattr(BF,'target',lambda *a:c)
    with pytest.raises(BF.B.EvidenceError,match='matching qualified WINDOW_V1'):
        BF.validate_plan(doc,doc['ack_hash'])
