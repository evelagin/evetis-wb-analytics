"""Exact additive controller recovery must not become a generic STOP exemption."""
import copy
import pytest
from tools.tenancy import controller_stop_recovery as R, durable_plan as D, tenant_backfill as BF


def fixture():
    doc=copy.deepcopy(BF.QF.accepted_doc('9b4702a141928eb1f4630ec85b2488838610dace63bd80701a3bf215d7ac1320'));root=BF.QF.ROOT
    def row(kind,payload,seq=42):
        return {'version':D.VERSION,'root_hash':root,'kind':kind,'sequence':seq,'payload':payload}
    run={'run_id':'prior','lease_generation':79,'ack_hash':doc['ack_hash']}
    i=row('DISPATCH_INTENT',{'plan':doc,'preparation':run})
    r=row('DISPATCH_RECEIPT',{'plan':doc,'receipt':run})
    rec=row('RECONCILED',{'sequence':374})
    base='projects/mpa-t-client-001/locations/europe-west1/jobs/'
    x=base+'tenant-backfill-controller/executions/tenant-backfill-controller-old'
    stop=row('STOPPED',{'controller_execution':x,'reason':'CONTROLLER_GATE_OR_EVIDENCE_FAILURE'},0)
    p={'version':R.VERSION,'type':R.TYPE,'tenant':'client_001','project':'mpa-t-client-001',
        'root_hash':root,'stop_hash':D.digest(stop),'predecessor_sequence':42,
        'receipt_hash':D.digest(r),'reconciliation_hash':D.digest(rec),'plan_id':doc['runtime_plan']['plan_id'],
        'state_hash':'a'*64,'state_sequence':374,'controller_execution':x,
        'controller_image':'example/controller@sha256:'+'b'*64,'controller_source_sha':'c'*40,
        'runtime_execution':base+'ozon-runtime-daily/executions/ozon-runtime-daily-prior',
        'runtime_image':'example/runtime@sha256:'+'d'*64,'runtime_source_sha':'e'*40,
        'implementation_sha':'f'*40,'predicates':dict.fromkeys(R.PREDICATES,True),
        'evidence_hashes':{'causal':'1'*64},'verified_at':'2026-10-08T06:00:00Z'}
    p['authorization']={k:p[k] for k in ('tenant','project','root_hash','receipt_hash','reconciliation_hash','plan_id','type','version')}
    p['authorization'].update(owner_ack_sha256='2'*64,stop_hashes=[p['stop_hash']])
    seal(p)
    return p,[i,r,rec,stop]


def seal(p):
    p['hash']=D.digest({k:v for k,v in p.items() if k!='hash'})


def test_exact_reconciled_chain_and_additive_authority_only():
    p,rows=fixture();before=copy.deepcopy(rows)
    assert R.verify_records(p,rows,BF.QF.ROOT,'client_001',publishing=True)
    # Raw certificate without independently verified durable record cannot clear STOP.
    with pytest.raises(BF.B.EvidenceError):
        D.decide_tick(rows,BF.QF.ROOT,{'status':'ELIGIBLE'},verified_controller_recoveries=[p])
    rows.append({'version':D.VERSION,'root_hash':BF.QF.ROOT,'kind':R.KIND,'sequence':42,'payload':p})
    # Chain has no historical 1..41 fixture: gate validates recovery first then
    # rejects dispatch gap, proving recovery cannot weaken the ordinary protocol.
    with pytest.raises(BF.B.EvidenceError,match='gap'):
        D.decide_tick(rows,BF.QF.ROOT,{'status':'ELIGIBLE'},verified_controller_recoveries=[p])
    assert rows[:4]==before
    assert D.decide_tick(rows,BF.QF.ROOT,{'status':'ELIGIBLE'})['action']=='STOPPED'


@pytest.mark.parametrize('predicate',sorted(R.PREDICATES))
@pytest.mark.parametrize('bad',[False,None,'UNKNOWN',1])
def test_every_unknown_or_false_predicate_fails_closed(predicate,bad):
    p,_=fixture();p['predicates'][predicate]=bad;seal(p)
    with pytest.raises(BF.B.EvidenceError):R.validate(p)


@pytest.mark.parametrize('fault',['not_reconciled','receipt','root','tenant','stop','successor_intent','successor_receipt','mutated_stop','deleted_stop','checkpoint','owner_scope','unknown_provenance','cross_tenant'])
def test_adversarial_scope_and_history(fault):
    p,rows=fixture()
    if fault=='not_reconciled':rows.pop(2)
    if fault=='receipt':p['receipt_hash']='3'*64
    if fault=='root':p['root_hash']='3'*64
    if fault=='tenant':p['tenant']='other'
    if fault=='stop':p['stop_hash']='3'*64
    if fault.startswith('successor_'):
        q=copy.deepcopy(rows[0 if fault=='successor_intent' else 1]);q['sequence']=43;rows.append(q)
    if fault=='mutated_stop':rows[3]['payload']['reason']='silently cleared'
    if fault=='deleted_stop':rows.pop(3)
    if fault=='checkpoint':p['state_sequence']=375
    if fault=='owner_scope':p['authorization']['stop_hashes']=['3'*64]
    if fault=='unknown_provenance':p['controller_source_sha']=None
    if fault=='cross_tenant':p['project']='mpa-platform'
    seal(p)
    with pytest.raises((BF.B.EvidenceError,ValueError)):
        R.verify_records(p,rows,BF.QF.ROOT,'client_001',publishing=True)


def test_later_successor_allowed_only_for_consumption_of_already_committed_recovery():
    p,rows=fixture();q=copy.deepcopy(rows[0]);q['sequence']=43;rows.append(q)
    assert R.verify_records(p,rows,BF.QF.ROOT,'client_001')
    with pytest.raises(BF.B.EvidenceError):R.verify_records(p,rows,BF.QF.ROOT,'client_001',publishing=True)


def test_arbitrary_historical_stop_not_covered_by_same_reason():
    p,rows=fixture();q=copy.deepcopy(rows[3]);q['payload']['controller_execution']+='other';rows.append(q)
    p['stop_hash']=D.digest(q);p['controller_execution']=q['payload']['controller_execution'];seal(p)
    with pytest.raises(BF.B.EvidenceError,match='explicitly authorized'):R.validate(p)


def terminal_fixture():
    from types import SimpleNamespace
    p,rows=fixture();r=rows[1]['payload']['receipt'];r['operation']='projects/mpa-t-client-001/locations/europe-west1/operations/synthetic'
    p['receipt_hash']=D.digest(rows[1]);p['authorization']['receipt_hash']=p['receipt_hash'];seal(p)
    env={'TENANT_ID':'client_001','GCP_PROJECT_ID':'mpa-t-client-001','TENANT_BINDING_REQUIRED':'1','STRICT_PAGE_CAPS':'1','BQ_RAW_DATASET':'ozon_raw','BQ_REF_DATASET':'ref'}
    def execution(role):
        e=dict(env)
        if role=='controller':e.update(BACKFILL_ROOT_HASH=p['root_hash'],CONTROLLER_SOURCE_SHA=p['controller_source_sha'])
        else:
            e.pop('TENANT_ID');e['INGESTION_RUN_ID']='prior'
        return {'name':p[role+'_execution'],'completionTime':'2026-10-07T20:00:00Z',
            ('failedCount' if role=='controller' else 'succeededCount'):1,
            'template':{'serviceAccount':('sa-backfill-controller' if role=='controller' else 'sa-ozon-runtime')+'@mpa-t-client-001.iam.gserviceaccount.com',
                'containers':[{'image':p[role+'_image'],'env':[{'name':k,'value':v} for k,v in e.items()]}]}}
    objects={BF.RUN_API+'/'+p[role+'_execution']:execution(role) for role in ('controller','runtime')}
    objects[BF.RUN_API+'/'+r['operation']]={'done':True,'metadata':{'name':p['runtime_execution']}}
    def request(method,url):
        assert method=='GET'
        return objects[url]
    b=SimpleNamespace(c=BF.target('client_001'),store=SimpleNamespace(history=lambda _:rows),request=request)
    return b,p,rows,objects


@pytest.mark.parametrize('fault',['active','runtime_failed','controller_succeeded','image','service_account','run','root','source','binding','strict','operation','operation_error'])
def test_exact_terminal_identity_and_predecessor_linkage(fault):
    b,p,rows,objects=terminal_fixture();R.terminal_executions(b,p)
    runtime=objects[BF.RUN_API+'/'+p['runtime_execution']];controller=objects[BF.RUN_API+'/'+p['controller_execution']]
    env=lambda obj,k:next(x for x in obj['template']['containers'][0]['env'] if x['name']==k)
    if fault=='active':runtime.pop('completionTime')
    if fault=='runtime_failed':runtime['failedCount']=1
    if fault=='controller_succeeded':controller['succeededCount']=1
    if fault=='image':runtime['template']['containers'][0]['image']='unqualified'
    if fault=='service_account':runtime['template']['serviceAccount']='foreign'
    if fault=='run':env(runtime,'INGESTION_RUN_ID')['value']='foreign'
    if fault=='root':env(controller,'BACKFILL_ROOT_HASH')['value']='3'*64
    if fault=='source':env(controller,'CONTROLLER_SOURCE_SHA')['value']='3'*40
    if fault=='binding':env(runtime,'TENANT_BINDING_REQUIRED')['value']='0'
    if fault=='strict':env(runtime,'STRICT_PAGE_CAPS')['value']='0'
    if fault=='operation':objects[BF.RUN_API+'/'+rows[1]['payload']['receipt']['operation']]['metadata']['name']='foreign'
    if fault=='operation_error':objects[BF.RUN_API+'/'+rows[1]['payload']['receipt']['operation']]['error']={'code':1}
    with pytest.raises(BF.B.EvidenceError):R.terminal_executions(b,p)


def test_owner_marker_required_and_reader_never_publishes():
    from types import SimpleNamespace
    b,p,rows,_=terminal_fixture()
    rows.append({'version':D.VERSION,'root_hash':p['root_hash'],'kind':R.KIND,'sequence':42,'payload':p})
    b.tables=SimpleNamespace(get_table=lambda *args:R.marker_value(p))
    assert R.load(b,rows,p['root_hash'])==[p]
    b.tables.get_table=lambda *args:None
    with pytest.raises(BF.B.EvidenceError,match='owner'):R.load(b,rows,p['root_hash'])


def test_full_positive_dispatch_gate_preserves_successful_reconciliation():
    p,rows=fixture();before=copy.deepcopy(rows)
    for seq in range(1,42):
        for kind in ('DISPATCH_INTENT','DISPATCH_RECEIPT','RECONCILED'):
            rows.append({'version':D.VERSION,'root_hash':p['root_hash'],'kind':kind,'sequence':seq,'payload':{'synthetic':True}})
    rows.append({'version':D.VERSION,'root_hash':p['root_hash'],'kind':R.KIND,'sequence':42,'payload':p})
    assert D.decide_tick(rows,p['root_hash'],{'status':'ELIGIBLE'},verified_controller_recoveries=[p])=={'action':'PREPARE_NEXT','sequence':43,'poll_only':False}
    assert rows[:4]==before
    arbitrary=copy.deepcopy(rows[3]);arbitrary['payload']['controller_execution']+='-later';rows.append(arbitrary)
    assert D.decide_tick(rows,p['root_hash'],{'status':'ELIGIBLE'},verified_controller_recoveries=[p])['action']=='STOPPED'


def audit_fixture(monkeypatch):
    """Synthetic read-only transport exercising concrete live predicate branches."""
    from datetime import datetime,timezone
    from types import SimpleNamespace
    import json
    b,p,rows,objects=terminal_fixture();c=b.c
    release=json.loads((BF.REPO/'infra/tenant/releases/backfill/9a4cafed54a42b3520efa7c4a4564ae58a687069.json').read_text())
    p['controller_image']=release['image'];p['controller_source_sha']=release['source_sha']
    p['runtime_image']=release['runtime_image'];p['runtime_source_sha']='06679f59edf975ae58451b4c746ff628cc387fd8'
    p['state_sequence']=1;rows[2]['payload']['sequence']=1;p['reconciliation_hash']=D.digest(rows[2]);p['authorization']['reconciliation_hash']=p['reconciliation_hash']
    state={'sequence':1,'orders':0,'supplies':0,'bundles':0,'progress':{'report':None,'pending':[],'bundle':None,'bundle_links':{},'seen_orders':[]}}
    p['state_hash']=D.digest(state);seal(p)
    for role in ('controller','runtime'):
        e=objects[BF.RUN_API+'/'+p[role+'_execution']];e['template']['containers'][0]['image']=p[role+'_image'];e['startTime']='2026-10-07T18:50:00Z';e['createTime']='2026-10-07T18:49:00Z'
        if role=='controller':next(a for a in e['template']['containers'][0]['env'] if a['name']=='CONTROLLER_SOURCE_SHA')['value']=p['controller_source_sha']
    rows.append({'version':D.VERSION,'root_hash':p['root_hash'],'kind':'MANIFEST','sequence':0,'payload':BF.QF.manifest()})
    b.preflight=lambda *args,**kw:None;b.active_runtime_execution=lambda:False;b.state=lambda _:state;b.journal=c['project_id']+'.ozon_raw.OZON_INGESTION_RUNS'
    controls={'successor':0,'source':0,'keys':0,'active':False,'runtime':False,'lease':79,'hold':False,'fence':False}
    times=[{'evidence_json':json.dumps(rows[3]),'updated_at':'2026-10-07T18:52:00Z'},{'evidence_json':json.dumps(rows[2]),'updated_at':'2026-10-07T18:51:00Z'}]
    def select(sql,params):
        assert sql.startswith('SELECT ')
        if 'backfill_id IN' in sql:return times
        if 'attempts>@seq' in sql:return [{'n':controls['successor']}]
        if 'COUNT(*) AS n' in sql:return [{'n':controls['source']}]
        if 'backfill_detail_json' in sql:return [{'backfill_sequence':1,'backfill_detail_json':'{"action":"SYNTHETIC_NO_SOURCE"}'}]
        if 'rows_n' in sql:return [{'rows_n':0,'keys_n':controls['keys']}]
        raise AssertionError(sql)
    b.select=select
    base=f"projects/{c['project_id']}/locations/{c['region']}"
    jobs=[{'name':base+'/jobs/'+n} for n in ('tenant-control','tenant-backfill-controller','ozon-runtime-daily','ozon-runtime-fast','ozon-runtime-weekly')]
    get=b.request
    def request(method,url):
        assert method=='GET'
        if url==BF.RUN_API+'/'+base+'/jobs?pageSize=1000':return {'jobs':jobs}
        if '/executions?' in url:
            if 'ozon-runtime-daily/' in url and (controls['active'] or controls['runtime']):
                e={'name':p['runtime_execution']+'-successor','createTime':'2026-10-07T19:00:00Z'}
                if not controls['active']:e['completionTime']='2026-10-07T19:01:00Z'
                return {'executions':[e]}
            return {'executions':[]}
        return get(method,url)
    b.request=request
    def list_tables(ds):
        if controls['fence']:return [('BFQ_'+p['root_hash']+'_0000000043_DISPATCH_INTENT',{},None)]
        return []
    r=rows[1]['payload']['receipt']
    b.tables=SimpleNamespace(list_tables=list_tables,get_table=lambda *a:({'owner':r['run_id']},json.dumps({'ack_hash':r['ack_hash']})))
    monkeypatch.setattr(BF.TL,'read_state',lambda *a:([],{},[{'lease_generation':controls['lease'],'evidence_json':'{"mode":"BOUNDED_PILOT"}'}],[],controls['hold']))
    return b,p,rows,controls,datetime(2026,10,8,6,tzinfo=timezone.utc)


def test_canonical_observer_source_free_positive(monkeypatch):
    b,p,rows,controls,now=audit_fixture(monkeypatch)
    assert R.observe(b,p,now)==p


@pytest.mark.parametrize('fault',['successor','source','keys','active','runtime','lease','hold','fence'])
def test_canonical_observer_rejects_live_counterexamples(monkeypatch,fault):
    b,p,rows,controls,now=audit_fixture(monkeypatch)
    controls[fault]=80 if fault=='lease' else True if fault in ('active','runtime','hold','fence') else 1
    with pytest.raises(BF.B.EvidenceError):R.observe(b,p,now)


def test_publication_adds_only_owner_marker_and_record_preserving_originals(monkeypatch):
    from tools.tests.test_durable_plan import Backend,NOW
    b,p,rows,_=terminal_fixture();memory=Backend()
    for r in rows:memory.store.commit(p['root_hash'],r['kind'],r['sequence'],r['payload'],NOW)
    memory.store.commit(p['root_hash'],'MANIFEST',0,BF.QF.manifest(),NOW)
    b.store=memory.store;b.tables=memory.meta;b.preflight=lambda *args,**kw:None;b.active_runtime_execution=lambda:False
    monkeypatch.setattr(R,'observe',lambda backend,proof,now:proof)
    original_rows=copy.deepcopy(memory.rows);original_records=copy.deepcopy(b.store.history(p['root_hash']))
    out=R.publish(b,p,NOW)
    assert out['source_dispatches']==0 and out['original_stop_hash']==p['stop_hash']
    assert memory.rows[:len(original_rows)]==original_rows and len(memory.rows)==len(original_rows)+1
    assert all(r in b.store.history(p['root_hash']) for r in original_records)
    assert R.load(b,b.store.history(p['root_hash']),p['root_hash'])==[p]
    assert R.publish(b,p,NOW)==out and len(memory.rows)==len(original_rows)+1
    # Corrupt/missing owner authority must not be accepted by the cloud reader.
    memory.meta.objects[R.marker(p)]=({},'tampered')
    with pytest.raises(BF.B.EvidenceError):R.load(b,b.store.history(p['root_hash']),p['root_hash'])


def test_failed_fresh_observation_cannot_write_even_owner_marker(monkeypatch):
    from tools.tests.test_durable_plan import Backend,NOW
    b,p,rows,_=terminal_fixture();memory=Backend()
    for r in rows:memory.store.commit(p['root_hash'],r['kind'],r['sequence'],r['payload'],NOW)
    memory.store.commit(p['root_hash'],'MANIFEST',0,BF.QF.manifest(),NOW)
    b.store=memory.store;b.tables=memory.meta;b.preflight=lambda *args,**kw:None;b.active_runtime_execution=lambda:False
    before=copy.deepcopy(memory.rows);metadata=copy.deepcopy(memory.meta.objects)
    def reject(*args):raise BF.B.EvidenceError('unknown source boundary')
    monkeypatch.setattr(R,'observe',reject)
    with pytest.raises(BF.B.EvidenceError):R.publish(b,p,NOW)
    assert memory.rows==before and memory.meta.objects==metadata


def test_existing_lease_with_wrong_owner_cannot_be_treated_as_released(monkeypatch):
    b,p,rows,controls,now=audit_fixture(monkeypatch)
    cid=BF.B.digest(['BOUNDED_PILOT_EXCLUSIVE',b.c['project_id']])[:16]
    b.tables.list_tables=lambda _: [(f'L_{cid}_0079',{'owner':'foreign'},None)]
    with pytest.raises(BF.B.EvidenceError,match='lease'):R.observe(b,p,now)


def test_runtime_explicit_foreign_tenant_or_dataset_is_rejected():
    b,p,rows,objects=terminal_fixture();runtime=objects[BF.RUN_API+'/'+p['runtime_execution']]
    runtime['template']['containers'][0]['env'].append({'name':'TENANT_ID','value':'foreign'})
    with pytest.raises(BF.B.EvidenceError):R.terminal_executions(b,p)
