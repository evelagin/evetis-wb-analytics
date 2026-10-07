"""Real full authority/recovery gates: injected evidence, no credentials/network."""
import copy
import json
from types import SimpleNamespace
import pytest
from tools.tenancy import full_controller as H, full_history as F, tenant_backfill as BF
from tools.tenancy import durable_plan as D, orchestration_contract as O
from tools.tests.test_full_controller import NOW
from tools.tests.test_full_history import manifest
from tools.tests.test_durable_plan import Backend as Records


@pytest.fixture
def authority(monkeypatch, tmp_path):
    m=manifest();c=copy.deepcopy(BF.target('client_001'))
    schema=tmp_path/'tools/tenancy/schema/tenant_ops/BACKFILL_CHECKPOINTS.json'
    schema.parent.mkdir(parents=True)
    schema.write_bytes((BF.REPO/'tools/tenancy/schema/tenant_ops/BACKFILL_CHECKPOINTS.json').read_bytes())
    c['marketplaces']['ozon']['runtime_image']=m['runtime_image']
    release={'schema_version':2,'image':m['controller_image'],'source_sha':m['controller_source_sha'],
        'runtime_image':m['runtime_image'],'runtime_implementation_hash':m['runtime_implementation_hash'],
        'controller_implementation_hash':m['controller_implementation_hash'],
        'verification':{g:'PASS' for g in ('ci','exact_image','offline_restart','lost_post_no_repeat',
            'quota_wait_no_source','tenant_isolation','reader_append_separation','full_history_adapter')}}
    c['orchestration']=O.block(c,{'release':m['controller_source_sha'],'root_hash':m['hash'],
        'scheduler_state':'ENABLED'},BF.REPO,release)
    from tools.tenancy import platform as PL
    directory=tmp_path/PL.RUNTIME_RELEASES_DIR/'ozon';directory.mkdir(parents=True)
    runtime={'image':m['runtime_image'],'source':{'commit':m['runtime_source_sha']},
        'verification':{'built_artifact':{'full_current_snapshot_adapters':'PASS'}}}
    (directory/'qualified.json').write_text(json.dumps(runtime))
    monkeypatch.setattr(BF,'REPO',tmp_path)
    monkeypatch.setattr(BF,'target',lambda tenant:copy.deepcopy(c))
    monkeypatch.setattr(O,'implementation_hash',lambda repo:m['controller_implementation_hash'])
    import lifecycle_core as L
    event={'seq':1,'to_state':L.BACKFILLING}
    state=[event];decisions={1:{'plan_hash':m['t5_plan_hash']}}
    facts=SimpleNamespace(hold=False,state=L.BACKFILLING,audit=[],bindings={'seller':'BOUND','performance':'BOUND'},
        credentials={'seller':{'status':'PASS'},'performance':{'status':'PASS'}},chunks=F.canonical_chunks(m),plan=m['t5_plan_hash'])
    monkeypatch.setattr(BF.TL,'read_state',lambda *a:(state,decisions,[],[],facts.hold))
    monkeypatch.setattr(L,'audit_history',lambda *a:facts.audit)
    monkeypatch.setattr(L,'current_state',lambda *a:facts.state)
    monkeypatch.setattr(L,'ordered',lambda *a:state)
    monkeypatch.setattr(BF.CK,'latest_plan',lambda *a,**k:(facts.plan,facts.chunks))
    monkeypatch.setattr(BF.TL,'cycle_start',lambda *a:NOW)
    monkeypatch.setattr(BF.TL,'operator_binding',lambda *a:(facts.bindings,facts.credentials))
    objects={}
    env=c['orchestration']['job']['env']
    objects[H.C.descriptor_name(env['CONTROLLER_SOURCE_SHA'],env['BACKFILL_ROOT_HASH'],'ENABLED')]=({},json.dumps({'settings':{'release':m['controller_source_sha'],'root_hash':m['hash'],'scheduler_state':'ENABLED'},'release':release}))
    objects[F.go_marker(m)]=F.go_value(m,{g:'PASS' for g in F.GATES})
    caps=[{'api':'seller','capability':g,'status':'AVAILABLE','discovered_at':NOW.isoformat()} for g in ('stocks','supplies')]
    tables=SimpleNamespace(get_table=lambda ds,name:objects.get(name),rows=lambda ds,name:iter(caps))
    backend=SimpleNamespace(c=c,tables=tables,clock=lambda:NOW)
    return SimpleNamespace(m=m,c=c,b=backend,objects=objects,caps=caps,facts=facts,decisions=decisions,
        runtime=runtime,runtime_path=directory/'qualified.json',release=release)


def test_real_full_authority_accepts_only_exact_live_evidence(authority):
    p=authority
    assert H.verify_authority(p.b,p.m)==p.c
    assert p.b.binding_status=={'seller':'BOUND','performance':'BOUND'}


@pytest.mark.parametrize('change',[
    lambda p:p.objects.pop(F.go_marker(p.m)),
    lambda p:p.objects.__setitem__(F.go_marker(p.m),({},'{}')),
    lambda p:setattr(p.facts,'hold',True),
    lambda p:setattr(p.facts,'state','READY'),
    lambda p:setattr(p.facts,'audit',['bad history']),
    lambda p:p.facts.bindings.update(seller='UNBOUND'),
    lambda p:p.facts.credentials['performance'].update(status='FAIL'),
    lambda p:setattr(p.facts,'plan','8'*64),
    lambda p:p.facts.chunks.pop(),
    lambda p:p.decisions[1].update(plan_hash='9'*64),
    lambda p:p.caps[0].update(status='UNAVAILABLE'),
    lambda p:p.caps.pop(),
    lambda p:p.b.c.update(project_id='foreign'),
    lambda p:p.b.c['orchestration']['job']['env'].update(BACKFILL_ROOT_HASH='8'*64),
])
def test_actual_full_authority_denies_before_dispatch_or_publication(authority,change):
    p=authority;change(p)
    with pytest.raises(BF.B.EvidenceError):H.verify_authority(p.b,p.m)


def test_old_image_cannot_claim_full_adapter(authority):
    p=authority
    key=next(k for k in p.objects if k.startswith('BF_SPEC'))
    bad=copy.deepcopy(p.release);bad['schema_version']=1
    p.objects[key]=({},json.dumps({'settings':{'release':p.m['controller_source_sha'],'root_hash':p.m['hash'],'scheduler_state':'ENABLED'},'release':bad}))
    with pytest.raises(BF.B.EvidenceError,match='no full-adapter'):H.verify_authority(p.b,p.m)


def test_changed_runtime_cannot_reuse_old_snapshot_qualification(authority):
    p=authority;p.runtime['verification']['built_artifact']['full_current_snapshot_adapters']='UNPROVEN'
    p.runtime_path.write_text(json.dumps(p.runtime))
    with pytest.raises(BF.B.EvidenceError,match='no full domain'):H.verify_authority(p.b,p.m)


@pytest.fixture
def failed(monkeypatch):
    m=manifest();c=BF.target('client_001')
    doc=BF.make_plan('client_001','fbo_postings','2026-10-05','2026-10-06','offline-full',NOW.isoformat(),window_days=2)
    receipt={'run_id':'bf-synthetic','lease_generation':10001,'ack_hash':doc['ack_hash'],'operation':'synthetic-op'}
    p=doc['runtime_plan'];base,jobs=BF.resources(c);job=next(n for n,j in jobs.items() if p['entity'] in j['entities'])
    env={'INGESTION_RUN_ID':receipt['run_id'],'BACKFILL_TARGET_PROJECT':p['project'],
        'ENTITIES':p['entity'],'SINCE':p['from'],'UNTIL':p['to'],'BACKFILL_MODE':BF.B.VERSION,
        'BACKFILL_GENERATION':p['generation'],'BACKFILL_ORIGIN':p['origin'],
        'TENANT_BINDING_REQUIRED':'1','STRICT_PAGE_CAPS':'1','BACKFILL_WINDOW_DAYS':'2',
        'BACKFILL_MAX_REQUESTS':str(doc['max_requests']),'BACKFILL_MAX_UNITS':str(doc['max_units'])}
    name=base+'/jobs/'+job+'/executions/synthetic'
    execution={'name':name,'completionTime':NOW.isoformat(),'failedCount':1,
        'template':{'serviceAccount':f"{c['marketplaces']['ozon']['service_accounts']['runtime']}@{p['project']}.iam.gserviceaccount.com",
                    'containers':[{'image':doc['image'],'env':[{'name':k,'value':v} for k,v in env.items()]}]}}
    operation={'done':True,'error':{'code':3},'metadata':{'name':name}}
    state=BF.B.initial(p);writes=[];markers={}
    cid=BF.B.digest(['BOUNDED_PILOT_EXCLUSIVE',c['project_id']])[:16]
    markers[BF.CK.lease_name(cid,10001)]=({'owner':receipt['run_id']},json.dumps({'ack_hash':doc['ack_hash']}))
    def create(ds,n,l,d):
        if n in markers:return False
        markers[n]=(l,d);return True
    tables=SimpleNamespace(get_table=lambda ds,n:markers.get(n),create_marker=create,
        append=lambda ds,t,rs:writes.extend(copy.deepcopy(rs)))
    facts=SimpleNamespace(error=repr(BF.B.EvidenceError('SOURCE_HTTP_503: /v2/posting/fbo/list')))
    backend=object.__new__(H.Backend);backend.c=c;backend.full_tables=tables
    backend.clock=lambda:NOW;backend.request=lambda method,url:copy.deepcopy(execution)
    backend.state=lambda d:copy.deepcopy(state)
    backend.select=lambda *a:[{'error_message':facts.error}]
    backend.authorize_full_leaf=lambda d:True
    return SimpleNamespace(b=backend,doc=doc,receipt=receipt,operation=operation,execution=execution,
        state=state,facts=facts,writes=writes,markers=markers)


def test_known_failed_read_preserves_failure_and_releases_only_exact_terminal_lease(failed):
    p=failed;out=p.b.reconcile_failed(p.doc,p.receipt,p.operation)
    assert out['failed'] and out['retry_category']=='KNOWN_SOURCE_READ'
    assert out['source_complete'] is out['persisted_reconciled'] is False
    assert len(p.writes)==1 and p.writes[0]['status']=='FAILED' and p.writes[0]['lease_generation']==10001
    assert any(n.startswith('LD_') for n in p.markers)
    assert not any(r['status']=='DONE' for r in p.writes)


def test_known_read_429_is_cooldown_not_a_permanent_failure(failed):
    p=failed;p.facts.error=repr(BF.B.EvidenceError('SOURCE_HTTP_429: /v2/posting/fbo/list'))
    out=p.b.reconcile_failed(p.doc,p.receipt,p.operation)
    assert out['retry_category']==BF.CK.QUOTA_ERROR_CLASS
    assert H.C.timestamp(out['eligible_at'])>NOW
    assert p.writes[0]['error_code']==BF.CK.QUOTA_ERROR_CLASS


@pytest.mark.parametrize('change',[
    lambda p:p.execution['template'].update(serviceAccount='foreign'),
    lambda p:p.execution['template']['containers'][0].update(image='foreign'),
    lambda p:p.execution['template']['containers'][0]['env'].append({'name':'TENANT_BINDING_REQUIRED','value':'1'}),
    lambda p:p.execution.update(failedCount=0),
    lambda p:p.execution.update(succeededCount=1),
    lambda p:p.execution.pop('completionTime'),
    lambda p:setattr(p.facts,'error',repr(BF.B.EvidenceError('SOURCE_HTTP_403: /v2/posting/fbo/list'))),
    lambda p:setattr(p.facts,'error','unclassified fatal error'),
    lambda p:setattr(p.facts,'error',repr(BF.B.EvidenceError('SOURCE_HTTP_503: /v2/posting/fbo/list'))+' unrelated'),
    lambda p:p.markers.clear(),
    lambda p:p.execution['template']['containers'][0]['env'].__setitem__(0,{'name':'INGESTION_RUN_ID','value':'foreign'}),
])
def test_unsafe_or_unproven_failure_never_writes_recovery(failed,change):
    p=failed;change(p)
    with pytest.raises(BF.B.EvidenceError):p.b.reconcile_failed(p.doc,p.receipt,p.operation)
    assert not p.writes and not any(n.startswith('LD_') for n in p.markers)


def test_ambiguous_report_intent_stops_before_execution_read_or_write(failed):
    p=failed;p.state['progress']['report']={'phase':'INTENT'}
    p.b.request=lambda *a:pytest.fail('ambiguous report must stop immediately')
    with pytest.raises(BF.B.EvidenceError,match='ambiguous report'):p.b.reconcile_failed(p.doc,p.receipt,p.operation)
    assert not p.writes


@pytest.fixture
def accepted(monkeypatch):
    m=manifest();i=next(i for i,p in enumerate(m['programs']) if p.get('accepted_qualification_plan'))
    doc=BF.QF.accepted_doc(BF.QF.SKU);state=BF.B.initial(doc['runtime_plan']);state.update(complete=True,sequence=34)
    prep={'run_id':'bf-accepted','lease_generation':50,'ack_hash':doc['ack_hash']}
    accounting={'state_hash':D.digest(state),'source_complete':True,'persisted_reconciled':True,'rows':90,'snapshot_date':'2026-10-07'}
    previous=[{'kind':'DISPATCH_INTENT','sequence':13,'payload':{'plan':doc,'preparation':prep}},
        {'kind':'DISPATCH_RECEIPT','sequence':13,'payload':{'plan':doc,'receipt':dict(prep,operation='old-op')}},
        {'kind':'RECONCILED','sequence':13,'payload':{'checkpoint':'DONE','accounting':accounting}}]
    records=Records();item={'index':i,'plan':doc,'shard_root':F.shard_root(m,i,doc)}
    records.store.commit(m['hash'],'FULL_MANIFEST',0,m,NOW)
    records.store.commit(m['hash'],'CHUNK_PLAN',i,item,NOW)
    original=records.store.history
    records.store.history=lambda root,**k:copy.deepcopy(previous) if root==BF.QF.ROOT else original(root,**k)
    release=[({'owner':prep['run_id']},json.dumps({'ack_hash':doc['ack_hash']}))]
    fresh={'required_campaigns':90,'completed_campaigns':90,'pending_campaigns':0,'rows':90}
    b=SimpleNamespace(c=BF.target('client_001'),store=records.store,clock=lambda:NOW,state=lambda d:copy.deepcopy(state),
        tables=SimpleNamespace(get_table=lambda *a:release[0]),sku_accounting=lambda *a,**k:copy.deepcopy(fresh))
    # Parent has other incomplete historical leaves: no T5 write should occur.
    return SimpleNamespace(m=m,i=i,item=item,b=b,previous=previous,state=state,fresh=fresh,release=release)


def test_accepted_sku90_reuse_requires_exact_terminal_join_and_no_source(accepted):
    p=accepted;old=copy.deepcopy(p.previous)
    out=H.import_accepted_sku90(p.b,p.m,p.i,p.item,[],[])
    assert out['source_dispatches']==0 and out['status']=='ACCEPTED_SKU90_REUSED'
    done=H.completed(p.b.store.history(p.m['hash']),p.m)[p.i]
    assert done['dispatch_attempts']==0 and done['sequence']==34
    assert done['reused_qualification']['root']==BF.QF.ROOT and p.previous==old


@pytest.mark.parametrize('change',[
    lambda p:p.state.update(complete=False),lambda p:p.state.update(sequence=35),
    lambda p:p.previous[2]['payload'].update(checkpoint='FAILED'),
    lambda p:p.previous[1]['payload']['receipt'].update(run_id='foreign'),
    lambda p:p.release.__setitem__(0,None),lambda p:p.fresh.update(rows=89),
    lambda p:p.fresh.update(pending_campaigns=1),lambda p:p.previous.append(copy.deepcopy(p.previous[2])),
])
def test_sku90_regression_cannot_be_imported_as_complete(accepted,change):
    p=accepted;change(p)
    with pytest.raises(BF.B.EvidenceError):H.import_accepted_sku90(p.b,p.m,p.i,p.item,[],[])
    assert not H.completed(p.b.store.history(p.m['hash']),p.m)


def test_installed_full_protocol_restores_authority_methods(monkeypatch):
    from tools.tenancy import full_image_check as I
    records=Records();b=SimpleNamespace(c=BF.target('client_001'),store=records.store,clock=lambda:NOW)
    old=(H.verify_authority,H.Backend.preflight,H.Backend.start,H.Backend.reconcile)
    monkeypatch.setattr(BF,'validate_plan',lambda *a:b.c)
    out=I.check(records.store,b,NOW,'1'*40)
    assert out['full_history_adapter']=='PASS' and out['full_history_live_go']=='UNPROVEN'
    assert old==(H.verify_authority,H.Backend.preflight,H.Backend.start,H.Backend.reconcile)


def test_monitoring_uses_exact_published_scope_without_async_identifier(monkeypatch):
    m=manifest();i=next(i for i,p in enumerate(m['programs']) if p['entity']=='catalog')
    doc=F.render_leaf(m,i,NOW.astimezone(BF.B.MSK).date())
    item={'index':i,'plan':doc,'shard_root':F.shard_root(m,i,doc)}
    records=Records();records.store.commit(m['hash'],'FULL_MANIFEST',0,m,NOW)
    records.store.commit(m['hash'],'CHUNK_PLAN',i,item,NOW)
    state=BF.B.initial(doc['runtime_plan']);state.update(sequence=3,rows=7,pages=2,requests=3)
    state['progress']['report']={'phase':'POLL','uuid':'NEVER_PRINT_ASYNC_UUID'}
    seen=[]
    def select(q,params):
        seen.append((q,params));return [{'last_success':str(NOW.timestamp()-120),'failed_attempts':1}]
    monkeypatch.setattr(BF,'validate_plan',lambda *a:records.c)
    b=SimpleNamespace(c=records.c,store=records.store,clock=lambda:NOW,state=lambda d:state,
        select=select,journal='mpa-t-client-001.ozon_raw.OZON_INGESTION_RUNS',binding_status={'seller':'BOUND','performance':'BOUND'})
    out=H.monitoring(b,m['hash'],{'status':'MONITORING','index':i})
    s=out['current_scope']
    assert s['plan_id']==doc['runtime_plan']['plan_id'] and s['sequence']==3
    assert s['source_rows']==7 and s['checkpoint_age_seconds']==120 and s['failed_attempts']==1
    assert s['async_phase']=='POLL' and 'NEVER_PRINT' not in json.dumps(out)
    assert len(seen)==1 and seen[0][1]['pid'][1]==s['plan_id']


@pytest.mark.parametrize('entity,complete',[('catalog',False),('prices',False),('stocks',False),('stocks',True)])
def test_snapshot_readback_verifies_exact_partial_prefix_and_stock_catalog_cohort(monkeypatch,entity,complete):
    c=BF.target('client_001');doc=BF.make_plan('client_001',entity,'2026-10-06','2026-10-06','offline-snapshot',NOW.isoformat(),today=NOW.astimezone(BF.B.MSK).date())
    state=BF.B.initial(doc['runtime_plan']);state.update(sequence=1,complete=complete)
    if entity=='catalog':state['progress']['product_ids']=['1','2'];detail={'action':'CATALOG_DETAIL'}
    elif entity=='prices':
        state['progress']['offers']=['a','b'];detail={'action':'PRICE_PAGE','observation_date':'2026-10-07','tables':{'RAW_OZON_PRICES':2,'RAW_OZON_PRICE_COMMISSIONS':2}}
    else:
        state['progress'].update(last_sku='102',skus=2)
        detail={'action':'STOCKS_BATCH','source_skus':2,'observation_date':'2026-10-07','tables':{'RAW_OZON_STOCKS':2}}
    monkeypatch.setattr(BF,'validate_plan',lambda *a:c)
    monkeypatch.setattr(BF.B,'validate',lambda *a,**k:None) # synthetic minimal state; actual engine state tested separately
    writes=[];tables=SimpleNamespace(append=lambda ds,t,rs:writes.extend(rs))
    monkeypatch.setattr(BF,'preflight',lambda *a,**k:(tables,[]))
    calls=[];bad=[False]
    def reader(c,sql,params,**kw):
        calls.append((sql,params))
        if 'backfill_detail_json' in sql:return [{'backfill_sequence':1,'backfill_detail_json':json.dumps(detail)}]
        if 'SELECT DISTINCT evidence_json' in sql:return [{'evidence_json':json.dumps({'state':state})}]
        if ' AS eligible' in sql:return [{'eligible':3 if bad[0] else 2,'unique_skus':2,'invalid':0}]
        if ' AS invalid_products' in sql:return [{'invalid_products':0,'invalid_skus':0,'valid_skus':2,'sku_rows':2,'sku_absent':0}]
        return [{'rows_n':2,'keys_n':2}]
    monkeypatch.setattr(BF,'select',reader)
    out=BF.verify_coverage(doc,doc['ack_hash'],backend=SimpleNamespace(request=lambda *a:None))
    assert out['source_sequence']==1
    grain_calls=[(q,p) for q,p in calls if ' AS keys_n' in q]
    assert grain_calls
    if not complete:
        assert all(('@prefix' in q if entity in {'catalog','prices'} else '@last' in q) for q,p in grain_calls)
        if entity=='catalog':assert writes[0]['status']=='PARTIAL'
    if entity=='stocks':
        assert out['readback'][0]['catalog_cohort_skus']==2
        previous=copy.deepcopy(writes);bad[0]=True
        with pytest.raises(BF.B.EvidenceError,match='COHORT_ACCOUNTING'):BF.verify_coverage(doc,doc['ack_hash'],backend=SimpleNamespace(request=lambda *a:None))
        assert writes==previous


def test_full_authority_reconstructs_future_deployment_from_immutable_descriptor(authority, monkeypatch):
    p = authority
    packaged = copy.deepcopy(p.c)
    packaged.pop('orchestration')
    monkeypatch.setattr(BF, 'target', lambda tenant: copy.deepcopy(packaged))
    assert H.verify_authority(p.b, p.m) == p.c


@pytest.mark.parametrize('field,value', [('root_hash','8'*64), ('release','8'*40), ('scheduler_state','PAUSED')])
def test_full_descriptor_setting_drift_is_denied(authority, field, value):
    p = authority
    key = next(k for k in p.objects if k.startswith('BF_SPEC'))
    descriptor = json.loads(p.objects[key][1])
    descriptor['settings'][field] = value
    p.objects[key] = ({}, json.dumps(descriptor))
    with pytest.raises(BF.B.EvidenceError, match='descriptor settings'):
        H.verify_authority(p.b, p.m)
