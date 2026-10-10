"""Deterministic real coordination/CAS/access tests; no cloud credentials."""
from copy import deepcopy
from types import SimpleNamespace
from datetime import timedelta
import json
import pytest
from tools.tenancy import controller_cadence as CC, cloud_tick as T, cloud_controller as C
from tools.tenancy import tenant_backfill as BF, cloud_access as A, durable_plan as D
from tools.tenancy import full_history as F
from tools.tenancy.controller_cadence_image_check import protocol, check
from tools.tenancy.full_leaf_image_check import NOW


def test_actual_packaged_overlap_and_entrypoint_contract():
    assert set(check().values())=={'PASS'}


@pytest.mark.parametrize('fault',['image','identity','env','retry','timestamp','terminal','not_visible'])
def test_unknown_peer_never_converts_to_wait(fault):
    m,r,c,b,x,objects,commits=protocol()
    if fault=='image':x[0]['template']['containers'][0]['image']='unqualified'
    if fault=='identity':x[0]['template']['serviceAccount']='foreign'
    if fault=='env':x[0]['template']['containers'][0]['env'].append({'name':'TENANT_BINDING_REQUIRED','value':'0'})
    if fault=='retry':x[0]['retriedCount']=1
    if fault=='timestamp':x[0]['createTime']='unknown'
    if fault=='terminal':x[0]['failedCount']=1
    if fault=='not_visible':b=b(2);b.current_execution+='missing'
    else:b=b(2)
    with pytest.raises(BF.B.EvidenceError) as e:CC.acquire(b)
    assert not isinstance(e.value,T.OverlapWait) and not objects and not commits


def test_atomic_cas_fences_even_when_both_lists_miss_each_other():
    m,r,c,b,x,objects,commits=protocol();left,right=b(0),b(1)
    original=left.request
    left.request=lambda method,url,body=None:dict(executions=[deepcopy(x[0])]) if '/executions?' in url else original(method,url,body)
    right.request=lambda method,url,body=None:dict(executions=[deepcopy(x[1])]) if '/executions?' in url else original(method,url,body)
    first=CC.acquire(left)
    # Simulate eventual list hiding the winner's marker too: atomic insert and
    # direct read still elect exactly the original owner, never a new intent.
    original_list=right.tables.list_tables
    right.tables.list_tables=lambda *a,**k:[]
    with pytest.raises(T.OverlapWait,match='proven') as e:CC.acquire(right)
    assert e.value.status=='SKIP_AUTHORITY_HELD' and len(objects)==1
    right.tables.list_tables=original_list
    CC.ensure(left);assert first==left.wake_claim and not commits


def test_terminal_holder_requires_immutable_closure_and_does_not_use_ttl():
    m,r,c,b,x,objects,commits=protocol();leader=b(0);CC.acquire(leader)
    x[0].update(completionTime=NOW.isoformat(),succeededCount=1)
    with pytest.raises(BF.B.EvidenceError,match='missing canonical closure'):CC.acquire(b(1))
    assert len(objects)==1
    CC.close(leader,dict(status='MONITORING',source_dispatches=0))
    assert CC.acquire(b(1))['generation']==2


@pytest.mark.parametrize('change',[lambda v:v.update(status='COMPLETE'),lambda v:v.update(source_dispatches=2),lambda v:v.update(source_dispatches=True),lambda v:v.update(claim_hash='f'*64)])
def test_corrupt_closure_is_not_release(change):
    m,r,c,b,x,objects,commits=protocol();leader=b(0);cl=CC.acquire(leader);CC.close(leader,dict(status='MONITORING',source_dispatches=0))
    n=CC.names(cl['root'],1)[1];labels,text=objects[n];v=json.loads(text);change(v);v=CC.sealed({k:w for k,w in v.items() if k!='hash'});objects[n]=(labels,D.encoded(v))
    x[0].update(completionTime=NOW.isoformat(),succeededCount=1)
    with pytest.raises(BF.B.EvidenceError,match='closure unknown'):CC.acquire(b(1))


def runtime_fixture(monkeypatch):
    m,r,c,b,x,objects,commits=protocol();backend=b(0);CC.acquire(backend)
    # Synthetic protocol identity is qualified by this fixture, not by the
    # production registry. Real artifact mismatch rejection is tested separately.
    monkeypatch.setattr(BF,'validate_plan',lambda d,ack:c if d['ack_hash']==ack else pytest.fail('modified fixture'))
    i=next(i for i,p in enumerate(m['programs']) if p['entity']=='fbo_postings')
    doc=F.render_leaf(m,i,NOW.astimezone(BF.B.MSK).date());backend.manifest=m;backend.index=i
    p=doc['runtime_plan'];base,jobs=BF.resources(c);job=next(n for n,j in jobs.items() if p['entity'] in j['entities'])
    env=dict(ENTITIES=p['entity'],SINCE=p['from'],UNTIL=p['to'],INGESTION_RUN_ID='bf-synthetic-known',BACKFILL_MODE=BF.B.VERSION,BACKFILL_TARGET_PROJECT=c['project_id'],BACKFILL_GENERATION=p['generation'],BACKFILL_ORIGIN=p['origin'],BACKFILL_MAX_REQUESTS=str(doc['max_requests']),BACKFILL_MAX_UNITS=str(doc['max_units']))
    if p['window_days']!=1:env['BACKFILL_WINDOW_DAYS']=str(p['window_days'])
    env.update(BF.continuation_overrides(doc))
    prep=dict(run_id=env['INGESTION_RUN_ID'],lease_generation=8,ack_hash=doc['ack_hash'],job=base+'/jobs/'+job,overrides=dict(overrides=dict(containerOverrides=[dict(env=[dict(name=k,value=v) for k,v in env.items()])])) )
    receipt={k:prep[k] for k in ('run_id','lease_generation','ack_hash')};receipt['operation']=base+'/operations/synthetic'
    records=[dict(kind='DISPATCH_INTENT',sequence=1,payload=dict(plan=doc,preparation=prep)),dict(kind='DISPATCH_RECEIPT',sequence=1,payload=dict(plan=doc,receipt=receipt))]
    backend.store=SimpleNamespace(history=lambda *a,**k:records)
    task=dict(timeout='3600s',maxRetries=0,serviceAccount=c['marketplaces']['ozon']['service_accounts']['runtime']+'@'+c['project_id']+'.iam.gserviceaccount.com',containers=[dict(image=BF.execution_image(doc,c),env=[dict(name=k,value=v) for k,v in dict(jobs[job]['env'],**env).items()])])
    # Runtime uses the image entrypoint; the canonical task contract is 3600s.
    execution=dict(name=base+'/jobs/'+job+'/executions/'+job+'-known',createTime=(NOW-timedelta(seconds=10)).isoformat(),taskCount=1,template=task)
    operation=dict(name=receipt['operation'],done=False,metadata=dict(name=execution['name']))
    cid=BF.B.digest(['BOUNDED_PILOT_EXCLUSIVE',c['project_id']])[:16]
    objects[BF.CK.lease_name(cid,8)]=(dict(owner=prep['run_id'],until=str(int(NOW.timestamp()+3600))),D.encoded(dict(mode='BOUNDED_PILOT',ack_hash=doc['ack_hash'])))
    backend.request=lambda method,url,body=None:deepcopy(operation)
    return backend,doc,execution,job,records,operation


def test_live_runtime_is_known_only_through_one_exact_intent_and_receipt(monkeypatch):
    b,d,x,j,records,op=runtime_fixture(monkeypatch);assert CC.runtime(b,d,x,j) is True
    records.pop()
    with pytest.raises(BF.B.EvidenceError,match='receipt unavailable'):CC.runtime(b,d,x,j)


@pytest.mark.parametrize('fault',['run','caps','image','identity','retry','intent','recon','operation'])
def test_same_image_adhoc_or_ambiguous_source_cannot_become_healthy_wait(fault,monkeypatch):
    b,d,x,j,records,op=runtime_fixture(monkeypatch);assert CC.runtime(b,d,x,j)
    if fault in ('run','caps'):
        n='INGESTION_RUN_ID' if fault=='run' else 'BACKFILL_MAX_UNITS'
        next(v for v in x['template']['containers'][0]['env'] if v['name']==n)['value']='unapproved'
    if fault=='image':x['template']['containers'][0]['image']='unqualified'
    if fault=='identity':x['template']['serviceAccount']='foreign'
    if fault=='retry':x['retriedCount']=1
    if fault=='intent':records.append(deepcopy(records[0]))
    if fault=='recon':records.append(dict(kind='RECONCILED',sequence=1))
    if fault=='operation':op['error']={'code':10}
    with pytest.raises(BF.B.EvidenceError) as e:CC.runtime(b,d,x,j)
    assert not isinstance(e.value,T.OverlapWait)


def test_terminal_operation_with_lagging_execution_waits_for_canonical_recon(monkeypatch):
    b,d,x,j,records,op=runtime_fixture(monkeypatch);op.update(done=True,response=dict(x,completionTime=NOW.isoformat(),succeededCount=1))
    with pytest.raises(T.OverlapWait) as e:CC.runtime(b,d,x,j)
    assert e.value.status=='WAIT_RECONCILIATION'
    assert len(records)==2 # no new intent, receipt, rewrite or fake COMPLETE


def test_unknown_live_lease_owner_is_a_real_error_not_wait(monkeypatch):
    b,d,x,j,records,op=runtime_fixture(monkeypatch)
    old=b.tables.get_table
    b.tables.get_table=lambda ds,n:({'owner':'bf-foreign','until':'123'},'{}') if n.startswith('L_') else old(ds,n)
    with pytest.raises(BF.B.EvidenceError,match='lease/owner linkage unknown'):CC.runtime(b,d,x,j)


def test_append_marker_route_is_only_current_root_and_exact_metadata(capsys):
    m,r,c,b,x,objects,commits=protocol();calls=[]
    access=A.CloudAccess(c,SimpleNamespace(token=lambda kind:'SYNTHETIC'),lambda *a:calls.append(a) or {})
    access.tables.create_marker('tenant_locks',CC.names(m['hash'],1)[0],{'kind':'controller_wake'},'synthetic')
    assert len(calls)==1
    for root,ds in [('f'*64,'tenant_locks'),(m['hash'],'ref')]:
        with pytest.raises(BF.TT.TableError):access.tables.create_marker(ds,CC.names(root,1)[0],{},'synthetic')
    legacy=A.CloudAccess(BF.target('client_001'),SimpleNamespace(token=lambda kind:pytest.fail('auth')),lambda *a:pytest.fail('transport'))
    with pytest.raises(BF.TT.TableError):legacy.tables.create_marker('tenant_locks',CC.names(m['hash'],1)[0],{},'synthetic')


def test_malformed_cadence_exception_handler_stops_without_revalidating_error(monkeypatch,capsys):
    m,r,c,b,x,objects,commits=protocol();backend=b(0);backend.c['orchestration']['cadence']['preferred']='unknown'
    monkeypatch.setattr(C,'bootstrap',lambda env:(backend,m['hash']))
    monkeypatch.setattr(C,'bounded_wake',lambda *a:CC.enabled(backend))
    assert C.main()==2 and json.loads(capsys.readouterr().out)['status']=='STOPPED' and len(commits)==1


def facts():
    return dict(duplicate_intents=0,duplicate_runtimes=0,unresolved_stops=0,
        cadence_throttles=0,material_quota_rejections=0,reconciliation_mismatches=0,
        source_unit_duplicates=0,source_unit_gaps=0,unsafe_lease_contention=0,
        security_regression=False,overlap_instability=0,material_overlaps=0,controller_durations=[])


@pytest.mark.parametrize('fault',sorted(set(facts())-{'controller_durations','material_overlaps','cadence_throttles','material_quota_rejections'}))
def test_guard_restores_stable_cadence_on_each_real_regression(fault):
    v=facts();v[fault]=True if fault=='security_regression' else 1
    out=CC.backoff(v);assert out['action']=='RESTORE_STABLE_10' and fault in out['reasons']


def test_normal_source_throttle_or_quota_wait_does_not_alone_revert_cadence():
    v=facts();v.update(cadence_throttles=4,material_quota_rejections=1)
    assert CC.backoff(v)['action']=='KEEP'
    v['duplicate_runtimes']=1
    assert CC.backoff(v)['action']=='RESTORE_STABLE_10'


def test_healthy_waits_do_not_revert_but_persistent_latency_with_real_overlap_does():
    v=facts();v.update(material_overlaps=3,controller_durations=[150]*10)
    assert CC.backoff(v)['action']=='KEEP'
    v['controller_durations']=[180]*10
    assert CC.backoff(v)['reasons']==['persistent_3_min_latency']
    v['controller_durations']=[150]*5+[180]*5
    assert CC.backoff(v)['action']=='KEEP' # persistence in both halves not proven


def test_ordinary_lease_reclaim_contract_remains_unchanged():
    cid='0'*16;live=dict(until=str(int(NOW.timestamp()+10)),owner='bf-existing')
    with pytest.raises(BF.B.EvidenceError,match='lease held'):
        BF.pilot_lease_generation([],[(BF.CK.lease_name(cid,9),live)],cid,NOW,lambda n:None)
    stale=dict(live,until=str(int((NOW-BF.CK.VISIBILITY_GRACE).timestamp())))
    assert BF.pilot_lease_generation([],[(BF.CK.lease_name(cid,9),stale)],cid,NOW,lambda n:None)==10


def test_reconciliation_decision_precedes_any_successor_intent():
    root='1'*64
    history=[dict(version=D.VERSION,root_hash=root,kind='DISPATCH_INTENT',sequence=1,payload={}),dict(version=D.VERSION,root_hash=root,kind='DISPATCH_RECEIPT',sequence=1,payload={})]
    assert D.decide_tick(history,root,dict(status='ELIGIBLE'),False)['action']=='RECONCILE'
    assert D.decide_tick(history,root,dict(status='ELIGIBLE'),True)['action']=='MONITOR'
    history.append(dict(version=D.VERSION,root_hash=root,kind='RECONCILED',sequence=1,payload={}))
    assert D.decide_tick(history,root,dict(status='ELIGIBLE'),False)['action']=='PREPARE_NEXT'


def test_class_c_predicates_and_recent_order_are_preserved():
    from tools.tenancy.full_leaf_recovery import PREDICATES
    from tools.tenancy.recent_priority_image_check import check as recent
    assert len(PREDICATES)==19 and recent()['recent_installed_drain_order']=='PASS'
