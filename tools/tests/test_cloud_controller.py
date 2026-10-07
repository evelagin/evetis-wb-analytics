"""Concrete cloud adapter tests, never live credentials/cloud/source calls."""
import copy
import json
from datetime import datetime,timezone,date
import pytest
from tools.tenancy import cloud_controller as C,tenant_backfill as BF,orchestration_contract as O
from tools.tenancy import orchestration_identity as I

NOW=datetime(2026,10,6,8,tzinfo=timezone.utc)

class Access:
    def __init__(self):self.c=BF.target('client_001');self.calls=[]
    def durable_records(self):return None
    def request(self,*a):pytest.fail('unexpected live cloud request')


def backend():return C.Backend(Access(),'synthetic-controller-execution',lambda:NOW)

def sku():return BF.make_plan('client_001','ads_sku_daily','2026-09-17','2026-09-17','synthetic-cloud','2026-10-05T00:00:00Z',date(2026,10,6),max_units=3)


def test_real_immutable_plan_and_runtime_image_provenance_validate():
    doc=sku()
    # A candidate checkout never upgrades an old image to current capability.
    from tools.tenancy import platform as PL
    facts=[json.loads(f.read_text()) for f in (BF.REPO/PL.RUNTIME_RELEASES_DIR/'ozon').glob('*.json')]
    release=next(r for r in facts if r.get('image')==doc['image'])
    if release['verification']['built_artifact']['backfill_implementation_hash']==BF.B.implementation_hash():
        assert BF.validate_plan(doc,doc['ack_hash'])['project_id']=='mpa-t-client-001'
    else:
        with pytest.raises(BF.B.EvidenceError,match='qualified WINDOW_V1'):BF.validate_plan(doc,doc['ack_hash'])
    assert doc['runtime_plan']['implementation_hash']==BF.B.implementation_hash()


def test_state_reader_never_resets_disappeared_or_conflicting_durable_checkpoint():
    b=backend();doc=sku();s=BF.B.initial(doc['runtime_plan']);s['sequence']=7
    b.select=lambda q,p:[{'sequence':7}] if 'MAX(' in q else []
    with pytest.raises(BF.B.EvidenceError,match='disappeared'):b.state(doc)
    b.select=lambda q,p:[{'sequence':7}] if 'MAX(' in q else [{'evidence_json':json.dumps({'plan':doc['runtime_plan'],'state':s})}]
    assert b.state(doc)['sequence']==7
    other=copy.deepcopy(s);other['rows']=1
    b.select=lambda q,p:[{'sequence':7}] if 'MAX(' in q else [{'evidence_json':json.dumps({'plan':doc['runtime_plan'],'state':v})} for v in (s,other)]
    with pytest.raises(BF.B.EvidenceError,match='conflict'):b.state(doc)


@pytest.mark.parametrize('at', ['1791225494.492827','1.791225494492827E9'])
def test_quota_from_real_bq_timestamp_encoding_waits_without_dispatch(at):
    b=backend();doc=sku();b.state=lambda _:BF.B.initial(doc['runtime_plan'])
    b.select=lambda q,p:[{'plan_id':'1'*64,'sequence':2,'exports':10,'reserved_at':at},{'plan_id':'1'*64,'sequence':5,'exports':5,'reserved_at':at}] if 'GROUP BY' in q else [{'n':0}]
    result=b.quota({'plans':[doc]},NOW)
    assert result['status']=='WAITING' and result['eligible_at']=='2026-10-06T18:38:14.492827+00:00'


def test_ambiguous_source_intent_stops_even_when_budget_allows():
    b=backend();doc=sku();s=BF.B.initial(doc['runtime_plan']);s['progress']['report']={'phase':'INTENT'}
    b.state=lambda _:s;b.select=lambda q,p:[] if 'GROUP BY' in q else [{'n':0}]
    assert b.quota({'plans':[doc]},NOW)['status']=='STOPPED'
    assert b.recover_receipt({'preparation':{'run_id':'synthetic'}}) is None


@pytest.mark.parametrize('unknown,expected', [(0,'ELIGIBLE'),(1,'STOPPED')])
def test_supplies_does_not_spend_performance_exports_but_unknowns_still_stop(unknown,expected):
    b=backend();doc=sku()
    supply=BF.make_plan('client_001','supplies','2026-09-17','2026-09-17',
                       'synthetic-supply','2026-10-05T00:00:00Z',date(2026,10,6))
    done=BF.B.initial(doc['runtime_plan']);done.update(complete=True,sequence=3)
    done['progress']={'next_day':'2026-09-18'}
    b.state=lambda d:done if d==doc else BF.B.initial(supply['runtime_plan'])
    reservations=[{'plan_id':'1'*64,'sequence':n,'exports':v,'reserved_at':'2026-10-05T18:38:14Z'} for n,v in [(2,10),(3,5)]]
    b.select=lambda q,p:reservations if 'GROUP BY' in q else [{'n':unknown}]
    before=copy.deepcopy(reservations)
    if unknown:
        with pytest.raises(BF.B.EvidenceError,match='unknown ordinary exports'):
            b.quota({'plans':[doc,supply]},NOW)
        assert reservations==before
        return
    result=b.quota({'plans':[doc,supply]},NOW)
    assert result['status']==expected and reservations==before
    if not unknown:
        assert result['allowance']==0 and result['performance_quota']['status']=='WAITING'
        assert result['basis']=='SELLER_SCOPE_NO_PERFORMANCE_EXPORT'
    done['complete']=False
    assert b.quota({'plans':[doc,supply]},NOW)['status']!= 'ELIGIBLE'


def test_next_sku_requires_fresh_catalog_before_binding_linkage_check():
    b=backend();doc=sku();s=BF.B.initial(doc['runtime_plan']);s['sequence']=7
    b.state=lambda _:s;calls=[]
    b.sku_accounting=lambda d,s,**kw:calls.append(kw) or {}
    b.catalog_dependency=lambda *a:{'fresh_snapshot':True}
    assert b.next_plan({'plans':[doc]},[],{'status':'ELIGIBLE'})=={'fresh_snapshot':True}
    assert calls==[{'linkage':False}]
    calls.clear()
    assert b.next_plan({'plans':[doc]},[{'kind':'SNAPSHOT_CERT','payload':{'day':'2026-10-06'}}],{})==doc
    assert calls==[{}]


def test_new_catalog_dependency_preserves_old_plan_and_dated_manifest():
    b=backend();doc=sku();original=copy.deepcopy(doc);saved=[]
    class Store:
        def commit(self,*a):saved.append(a)
    b.store=Store();root='3'*64
    fresh=b.catalog_dependency({'hash':root},[],'2026-10-06')
    assert fresh['runtime_plan']['observation_date']=='2026-10-06' and fresh['runtime_plan']['entity']=='catalog'
    assert doc==original and saved[0][0:3]==(root,'DEPENDENCY_PLAN',0)
    assert b.catalog_dependency({'hash':root},[{'kind':'DEPENDENCY_PLAN','payload':{'day':'2026-10-06','plan':fresh}}],'2026-10-06')==fresh
    assert len(saved)==1


def test_sku_prefix_reconciliation_counts_required_cohort_not_just_nonempty_rows():
    b=backend();doc=sku();s=BF.B.initial(doc['runtime_plan']);s.update(sequence=2,rows=20)
    s['progress'].update(pending=['synthetic']*75,report=None)
    def read(q,p):
        if 'ORDER BY backfill_sequence' in q:return [
            {'backfill_sequence':1,'backfill_detail_json':json.dumps({'action':'SKU_COHORT','day':'2026-09-17','required_campaigns':90})},
            {'backfill_sequence':2,'backfill_detail_json':json.dumps({'action':'SKU_BATCH_COMPLETE','campaigns':15,'expected_unique_rows':20})}]
        if 'unresolved' in q:return [{'unresolved':0}]
        return [{'rows_n':20,'keys_n':20,'campaigns':15}]
    b.select=read
    assert b.sku_accounting(doc,s)['pending_campaigns']==75
    with pytest.raises(BF.B.EvidenceError,match='incomplete'):b.sku_accounting(doc,s,require_complete=True)
    s['progress']['pending'].pop()
    with pytest.raises(BF.B.EvidenceError,match='contradictory'):b.sku_accounting(doc,s)


def release(c):
    return {'schema_version':1,'image':'europe-west1-docker.pkg.dev/mpa-platform/mpa-runtime/tenant-backfill-controller@sha256:'+'4'*64,'source_sha':'5'*40,'runtime_image':c['marketplaces']['ozon']['runtime_image'],'runtime_implementation_hash':BF.B.implementation_hash(),'controller_implementation_hash':O.implementation_hash(BF.REPO),'verification':{k:'PASS' for k in ('ci','exact_image','offline_restart','lost_post_no_repeat','quota_wait_no_source','tenant_isolation','reader_append_separation')}}


def test_canonical_opt_in_is_closed_and_does_not_rewrite_existing_runtime_or_scopes():
    c=BF.target('client_001');before=copy.deepcopy(c)
    settings={'release':'5'*40,'root_hash':'3'*64,'scheduler_state':'PAUSED'}
    block=O.block(c,settings,BF.REPO,release=release(c))
    assert c==before and block['matrix']==I.matrix_for_contract(c)
    assert block['job']['env']['TENANT_BINDING_REQUIRED']=='1'
    assert block['scheduler']['state']=='PAUSED'
    assert C.descriptor_name('5'*40,'3'*64,'PAUSED')!=C.descriptor_name('5'*40,'3'*64,'ENABLED')
    for k in ('image','root_hash','scheduler_state'):
        bad=dict(settings,**{k:'unreviewed'})
        with pytest.raises((ValueError,TypeError)):O.block(c,bad,BF.REPO,release=release(c))


@pytest.mark.parametrize('change',[
    lambda r:r.update(schema_version=True),lambda r:r.update(source_sha='6'*40),
    lambda r:r['verification'].update(ci='UNPROVEN'),lambda r:r['verification'].update(extra='PASS'),
    lambda r:r.update(runtime_implementation_hash='7'*64),lambda r:r.update(controller_implementation_hash='7'*64)])
def test_incomplete_or_fabricated_release_gates_fail_closed(change):
    c=BF.target('client_001');r=release(c);change(r)
    with pytest.raises((ValueError,BF.B.EvidenceError)):
        O.block(c,{'release':'5'*40,'root_hash':'3'*64,'scheduler_state':'PAUSED'},BF.REPO,release=r)
        O.verify_artifact_source(r,BF.REPO)


def test_transient_read_is_retryable_and_never_marks_hard_stop(monkeypatch,capsys):
    from tools.tenancy import cloud_access as A
    monkeypatch.setattr(C,"bootstrap",lambda e:(_ for _ in ()).throw(A.TransientReadError("HTTP 503")))
    assert C.main()==1
    out=json.loads(capsys.readouterr().out)
    assert out=={"status":"WAITING_READ_RETRY","reason":"TRANSIENT_READ_TRANSPORT"}


def test_publisher_requires_current_registry_root_before_any_query(monkeypatch):
    from tools.tenancy import cloud_plan as P
    # This test isolates publication-root authorization; artifact-source gates
    # have independent positive/negative coverage and exact-image qualification.
    monkeypatch.setattr(BF,"validate_plan",lambda *a:BF.target("client_001"))
    doc=sku();manifest=P.freeze("client_001",[doc],"2026-10-06T08:00:00Z")
    monkeypatch.setattr(BF.TT,"_req",lambda *a:pytest.fail("unregistered publication touched cloud"))
    with pytest.raises(BF.B.EvidenceError,match="canonical registry opt-in"):P.publish(manifest)
    assert manifest["plans"][0]==doc and manifest["purpose"]=="QUALIFICATION"


def test_foreign_descriptor_or_identity_cannot_bootstrap(monkeypatch,tmp_path):
    (tmp_path/"CONTROLLER_SOURCE_SHA").write_text("5"*40)
    monkeypatch.setattr(BF,"REPO",tmp_path)
    monkeypatch.setattr(BF.TT,"_req",lambda *a:pytest.fail("owner credential fallback"))
    calls=[]
    def send(method,url,body=None,headers=None):
        calls.append(url)
        return "sa-ozon-runtime@mpa-t-client-001.iam.gserviceaccount.com"
    env={"CONTROLLER_SOURCE_SHA":"5"*40,"TENANT_ID":"client_001","GCP_PROJECT_ID":"mpa-t-client-001","BACKFILL_ROOT_HASH":"3"*64,"HISTORICAL_SCHEDULER_STATE":"PAUSED"}
    with pytest.raises(C.A.TT.TableError,match="identity"):C.bootstrap(env,send)
    assert len(calls)==1 and calls[0].endswith("/email")


def test_held_canonical_lease_waits_without_fabricating_release_or_sticky_stop(monkeypatch,capsys):
    monkeypatch.setattr(C,"bootstrap",lambda e:(_ for _ in ()).throw(BF.B.EvidenceError("pilot lease held; reconcile terminal execution before continuation")))
    assert C.main()==1
    assert json.loads(capsys.readouterr().out)=={"status":"WAITING_LEASE","reason":"CANONICAL_LEASE_NOT_YET_RECLAIMABLE"}


def test_invalid_timestamp_cannot_become_quota_eligibility():
    for value in ('NaN','Infinity','1E999','2026-10-06T08:00:00'):
        with pytest.raises((ValueError,BF.B.EvidenceError)):C.timestamp(value)


def test_cloud_monitoring_reports_qualification_grain_without_identifier_payload():
    b=backend();doc=sku();state=BF.B.initial(doc['runtime_plan']);state['sequence']=7
    state['progress']['pending']=['NEVER_OUTPUT_SOURCE_ID']*75
    b.state=lambda _:state;b.binding_status={'seller':'BOUND','performance':'BOUND'}
    class Store:
        def history(self,root):return [{'kind':'MANIFEST','payload':{'purpose':'QUALIFICATION','plans':[doc]}}]
    b.store=Store();b.select=lambda q,p:[{'last_success':'1.791225494492827E9','last_failure':None,'failed_attempts':0}]
    out=b.monitoring('3'*64,{'status':'WAITING','eligible_at':'2026-10-06T18:38:14.492827Z'})
    assert out['total_chunks']==1 and out['completed_chunks']==0 and out['waiting_chunks']==1 and out['failed_chunks']==0
    assert out['scopes'][0]['pending_campaigns']==75 and out['scopes'][0]['checkpoint_age_seconds']>0
    assert 'NEVER_OUTPUT_SOURCE_ID' not in json.dumps(out)
    assert out['chunk_grain']=='FROZEN_QUALIFICATION_SCOPE'


def test_failed_runtime_rows_without_plan_id_use_committed_receipt_attribution():
    b=backend();doc=sku();b.state=lambda _:BF.B.initial(doc['runtime_plan']);b.binding_status={'seller':'BOUND','performance':'BOUND'}
    class Store:
        def history(self,root):return [
            {'kind':'MANIFEST','payload':{'purpose':'QUALIFICATION','plans':[doc]}},
            {'kind':'DISPATCH_RECEIPT','payload':{'plan':doc,'receipt':{'run_id':'bf-synthetic-known'}}}]
    b.store=Store()
    def select(q,p):
        assert 'OR ingestion_run_id IN (@run0)' in q and p['run0']==('STRING','bf-synthetic-known')
        assert 'bf-synthetic-known' not in q
        return [{'last_success':None,'last_failure':'2026-10-06T07:00:00Z','failed_attempts':1}]
    b.select=select;out=b.monitoring('3'*64,{'status':'STOPPED'})
    assert out['failed_chunks']==1 and out['scopes'][0]['failed_attempts']==1
    assert 'bf-synthetic-known' not in json.dumps(out)


def test_packaged_docker_copy_closure_resolves_canonical_registry(tmp_path):
    import shlex,shutil,subprocess,sys
    for line in (BF.REPO/'tools/tenancy/controller.Dockerfile').read_text().splitlines():
        if not line.startswith('COPY '):continue
        _,source,target=shlex.split(line)
        assert target.startswith('/app/')
        dest=tmp_path/target.removeprefix('/app/')
        dest.parent.mkdir(parents=True,exist_ok=True)
        src=BF.REPO/source
        if src.is_dir():shutil.copytree(src,dest,ignore=shutil.ignore_patterns('__pycache__','artifacts'))
        else:shutil.copy2(src,dest)
    (tmp_path/'CONTROLLER_SOURCE_SHA').write_text('5'*40+'\n')
    result=subprocess.run([sys.executable,'-m','tools.tenancy.controller_image_check'],cwd=tmp_path,capture_output=True,text=True,timeout=60)
    from tools.tenancy import platform as PL
    image=BF.target('client_001')['marketplaces']['ozon']['runtime_image']
    releases=[json.loads(f.read_text()) for f in (BF.REPO/PL.RUNTIME_RELEASES_DIR/'ozon').glob('*.json')]
    facts=next(r for r in releases if r.get('image')==image)['verification']['built_artifact']
    if facts['backfill_implementation_hash']!=BF.B.implementation_hash():
        assert result.returncode!=0 and 'qualified WINDOW_V1' in result.stderr
        return # candidate cannot claim old immutable artifact contains new code
    assert result.returncode==0,result.stderr
    out=json.loads(result.stdout)
    assert out['runtime_implementation_hash']==BF.B.implementation_hash() and out['python_version'].startswith('3.12.')
    assert out['live_deployment']=='UNPROVEN'


def test_direct_cli_does_not_shadow_stdlib_platform():
    import subprocess,sys
    result=subprocess.run([sys.executable,'-S',str(BF.REPO/'tools/tenancy/tenant_backfill.py'),'--help'],cwd=BF.REPO,capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stderr
    code="import sys; sys.path.insert(0,'tools/tenancy'); from tools.tenancy.plan_scan import COMPUTED_APPLIER_FIELDS; import platform; assert callable(platform.system)"
    result=subprocess.run([sys.executable,'-S','-c',code],cwd=BF.REPO,capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stderr


def test_exact_quota_select_serializes_parses_and_executes_typed_rest_stub():
    from tools.tenancy import controller_image_check as image
    assert image.check_quota_query(BF.target('client_001'),sku())=='PASS'


@pytest.mark.parametrize('first,expected',[('RECONCILED',2),('WAITING',1),('MONITORING',1),('STOPPED',1),('DISPATCHED',1)])
def test_bounded_wake_never_loops_or_dispatches_twice(monkeypatch,first,expected):
    b=backend();calls=[]
    class Tick:
        def __init__(self,*a):pass
        def run(self):
            calls.append(1)
            return {'status':first if len(calls)==1 else 'DISPATCHED','source_dispatches':int(len(calls)>1 or first=='DISPATCHED')}
    monkeypatch.setattr(C.T,'Tick',Tick)
    result=C.bounded_wake('3'*64,b)
    assert len(calls)==expected and result['source_dispatches']<=1
    assert (result.get('prior_action')=='RECONCILED')==(expected==2)
