"""Owner recovery cannot whitelist a later STOP, release a foreign lease or widen IAM."""
import copy
from datetime import datetime,timezone
import pytest
from tools.tenancy import pre_source_recovery as R,durable_plan as D,runtime_identity as I,tenant_backfill as BF,registry
from tools.tests.test_durable_plan import record,ROOT


def failed():
    p=BF.QF.accepted_doc(BF.QF.SKU)['runtime_plan'];s=BF.B.initial(p);s['rows']=30;s['sequence']=15
    s['progress'].update(pending=[str(i) for i in range(31,91)],report=None,skipped=0)
    s['progress']['report']={'phase':'INTENT','batch':[str(i) for i in range(31,41)],'execution':'failed-run',
        'cohort_hash':BF.QF.cohort(p,p['from'],[str(i) for i in range(31,41)])}
    proof={'version':R.PS.VERSION,'root_hash':'1'*64,'tenant':'client_001','project':p['project'],'plan_id':p['plan_id'],
        'dispatch_sequence':6,'run_id':'failed-run','execution':'ozon-runtime-daily-failed',
        'image':'europe-west1-docker.pkg.dev/mpa-platform/mpa-runtime/ozon-runtime@sha256:'+'2'*64,
        'source_sha':'3'*40,'lease_generation':6,'intent_hash':'4'*64,'receipt_hash':'5'*64,
        'stop_hashes':['6'*64],'unit_sequence':15,'state_hash':BF.B.digest(s),'cohort_hash':s['progress']['report']['cohort_hash'],
        'exports_reserved':10,'query_id':'failed-query','failure_stage':'CAPABILITY_PROFILE_ACCESS_DENIED_BEFORE_REPORT_POST',
        'verified_at':'2026-10-07T07:18:00+00:00','post_attempts':0,'uuid_present':False,'sku_rows_written':0}
    proof['hash']=BF.B.digest(proof)
    return p,s,proof


def chain():
    p,s,proof=failed();proof['dispatch_sequence']=1;proof['root_hash']=ROOT
    doc={'runtime_plan':p};i=record('DISPATCH_INTENT',payload={'plan':doc,'preparation':{'run_id':proof['run_id']}})
    r=record('DISPATCH_RECEIPT',payload={'plan':doc,'receipt':{'run_id':proof['run_id'],'lease_generation':proof['lease_generation']}})
    stop=record('STOPPED',0,{'reason':'old-failure'})
    proof.update(intent_hash=D.digest(i),receipt_hash=D.digest(r),stop_hashes=[D.digest(stop)])
    proof['hash']=D.digest({k:v for k,v in proof.items() if k!='hash'})
    return proof,[i,r,stop,record(R.KIND,1,proof)]


def test_only_verified_owner_record_allows_new_dispatch_and_old_stop_survives():
    p,rows=chain();old=copy.deepcopy(rows)
    assert D.decide_tick(rows,ROOT,{'status':'ELIGIBLE'},verified_failures=[p])['sequence']==2
    assert rows==old and not any(r['kind']=='RECONCILED' for r in rows)
    assert D.decide_tick(rows,ROOT,{'status':'ELIGIBLE'})['action']=='STOPPED'
    newer=rows+[record('STOPPED',0,{'reason':'old-failure','controller_execution':'new-failure'})]
    assert D.decide_tick(newer,ROOT,{'status':'ELIGIBLE'},verified_failures=[p])['action']=='STOPPED'


def test_detached_recovery_record_does_not_clear_stop():
    p,rows=chain()
    with pytest.raises(BF.B.EvidenceError):D.decide_tick(rows[:-1],ROOT,{'status':'ELIGIBLE'},verified_failures=[p])


@pytest.mark.parametrize('fault',['tenant','root','receipt','stop','success'])
def test_scope_or_chain_conflict_rejected(fault):
    p,rows=chain()
    if fault=='tenant':p['tenant']='foreign'
    if fault=='root':p['root_hash']='0'*64
    if fault=='receipt':rows[1]['payload']['receipt']['run_id']='foreign'
    if fault=='stop':rows=rows[:2]+rows[3:]
    if fault=='success':rows.append(record('RECONCILED'))
    with pytest.raises(BF.B.EvidenceError):R.verify_records(p,rows,ROOT,'client_001')


def test_minimum_read_is_exact_generic_table_not_dataset_or_provisioner():
    c=registry.terraform_inputs('client_001');m=I.matrix(c)
    assert len(m)==1 and m[0]['permissions']==['bigquery.tables.getData']
    assert m[0]['resource']=='projects/mpa-t-client-001/datasets/tenant_ops/tables/CAPABILITY_PROFILE'
    assert m[0]['principal']=='sa-ozon-runtime@mpa-t-client-001.iam.gserviceaccount.com'
    other=copy.deepcopy(c);other['project_id']='mpa-t-isolated';other['tenant_id']='isolated'
    assert I.matrix(other)[0]['resource'].startswith('projects/mpa-t-isolated/')
    assert 'client-001' not in str(I.matrix(other))


def test_exact_failed_terminal_lease_uses_new_generation_never_false_done():
    # Lease helpers imported by coordinator use its checkpoint module.
    cid='1'*16;name=BF.CK.lease_name(cid,6);now=datetime(2026,10,7,8,tzinfo=timezone.utc)
    labels={'owner':'failed-run','until':str(int(now.timestamp())+7200)}
    args=([],[(name,labels)],cid,now,lambda _:None)
    with pytest.raises(BF.B.EvidenceError,match='held'):BF.pilot_lease_generation(*args)
    assert BF.pilot_lease_generation(*args,failed_reader=lambda generation,lb:generation==6 and lb['owner']=='failed-run')==7
    with pytest.raises(BF.B.EvidenceError,match='held'):BF.pilot_lease_generation(*args,failed_reader=lambda *a:False)


def reader_fixture():
    import json
    from types import SimpleNamespace
    proof,records=chain();p,state,_=failed()
    metadata=SimpleNamespace(get_table=lambda *a:R.PS.marker_value(proof))
    def select(sql,params):
        if 'evidence_json' in sql:return [{'evidence_json':json.dumps({'plan':p,'state':state}),
            'backfill_detail_json':json.dumps({'action':'REPORT_INTENT','exports_reserved':10})}]
        return [{'status':'FAILED'}]
    c=BF.target('client_001')
    execution={'completionTime':'2026-10-07T07:00:00Z','failedCount':1,'succeededCount':0,
        'template':{'serviceAccount':c['marketplaces']['ozon']['service_accounts']['runtime']+'@'+p['project']+'.iam.gserviceaccount.com',
            'containers':[{'image':proof['image'],'env':[{'name':'INGESTION_RUN_ID','value':proof['run_id']}]}]}}
    b=SimpleNamespace(c=c,tables=metadata,select=select,journal=p['project']+'.ozon_raw.OZON_INGESTION_RUNS',request=lambda *a:execution)
    return b,proof,records,execution


def test_reader_requires_owner_marker_exact_source_and_failed_execution():
    b,p,records,_=reader_fixture()
    assert R.load(b,records,ROOT)==[p]
    b.tables.get_table=lambda *a:None
    with pytest.raises(BF.B.EvidenceError,match='owner'):R.load(b,records,ROOT)


@pytest.mark.parametrize('fault',['active','success','image','principal','run','aggregate','source'])
def test_recovery_reader_rejects_unproven_terminal_or_source_attribution(fault):
    b,p,records,x=reader_fixture()
    if fault=='active':x.pop('completionTime')
    if fault=='success':x['succeededCount']=1
    if fault=='image':x['template']['containers'][0]['image']='unreviewed'
    if fault=='principal':x['template']['serviceAccount']='other@mpa-t-other.iam.gserviceaccount.com'
    if fault=='run':x['template']['containers'][0]['env'][0]['value']='other'
    original=b.select
    if fault=='aggregate':b.select=lambda sql,params:([{'status':'OK'}] if 'SELECT status' in sql else original(sql,params))
    if fault=='source':b.select=lambda sql,params:[]
    with pytest.raises(BF.B.EvidenceError):R.load(b,records,ROOT)


def test_owner_publisher_preserves_all_predecessors_and_rejects_conflicting_marker(monkeypatch):
    from types import SimpleNamespace
    from tools.tests.test_durable_plan import Backend,NOW
    memory=Backend();proof,records=chain()
    for r in records[:3]:memory.store.commit(ROOT,r['kind'],r['sequence'],r['payload'],NOW)
    monkeypatch.setattr(D,'validate_manifest',lambda _:None)
    manifest={'hash':ROOT,'purpose':'QUALIFICATION','plans':[]}
    memory.store.commit(ROOT,'MANIFEST',0,manifest,NOW)
    backend,_,_,_=reader_fixture();backend.tables=memory.meta;backend.store=memory.store
    backend.preflight=lambda m:None;backend.active_runtime_execution=lambda:False
    old=copy.deepcopy(memory.rows)
    out=R.publish(backend,proof,NOW)
    assert out['old_STOP_preserved'] and out['old_reservation_preserved']
    assert memory.rows[:len(old)]==old and memory.rows[-1]['status']=='FAILED'
    assert not any(r['kind']=='RECONCILED' for r in memory.store.history(ROOT))
    assert R.publish(backend,proof,NOW)==out and len(memory.rows)==len(old)+1
    memory.meta.objects[R.PS.marker(proof)]=({},'conflict')
    with pytest.raises(BF.B.EvidenceError,match='conflict'):R.publish(backend,proof,NOW)
