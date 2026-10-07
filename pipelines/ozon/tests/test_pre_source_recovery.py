"""Pre-intent permission and immutable failed-before-POST recovery adversaries."""
import copy
from types import SimpleNamespace
import pytest
import backfill as F
import backfill_core as B
import common as C
import pre_source as PS
from test_backfill_engine import harness
from test_qualification_acceleration import prefix


def failed():
    p,s=prefix();s['sequence']=15
    s['progress']['report']={'phase':'INTENT','batch':[str(i) for i in range(31,41)],'execution':'failed-run',
        'cohort_hash':F.QF.cohort(p,p['from'],[str(i) for i in range(31,41)])}
    proof={'version':PS.VERSION,'root_hash':'1'*64,'tenant':'client_001','project':p['project'],'plan_id':p['plan_id'],
        'dispatch_sequence':6,'run_id':'failed-run','execution':'ozon-runtime-daily-failed',
        'image':'europe-west1-docker.pkg.dev/mpa-platform/mpa-runtime/ozon-runtime@sha256:'+'2'*64,
        'source_sha':'3'*40,'lease_generation':6,'intent_hash':'4'*64,'receipt_hash':'5'*64,
        'stop_hashes':['6'*64],'unit_sequence':15,'state_hash':B.digest(s),'cohort_hash':s['progress']['report']['cohort_hash'],
        'exports_reserved':10,'query_id':'failed-query','failure_stage':'CAPABILITY_PROFILE_ACCESS_DENIED_BEFORE_REPORT_POST',
        'verified_at':'2026-10-07T07:18:00+00:00','post_attempts':0,'uuid_present':False,'sku_rows_written':0}
    proof['hash']=B.digest(proof)
    return p,s,proof


@pytest.mark.parametrize('message',['CAPABILITY_PROFILE accessDenied','permission revoked','binding revoked','Catalog missing','budget metadata unavailable'])
def test_internal_gate_failure_creates_no_intent_or_post(harness,monkeypatch,message):
    p,s=prefix();old=copy.deepcopy(s);engine=F.Engine(p,'new','safe',s,unit_budget=3)
    engine.before_intent=lambda:(_ for _ in ()).throw(B.EvidenceError(message))
    monkeypatch.setattr(C,'perf_post',lambda *a:pytest.fail('source POST before preflight'))
    with pytest.raises(B.EvidenceError,match=message):engine.run(harness[3])
    assert not harness[2] and not harness[0] and s==old


def test_recovery_appends_new_unit_then_new_intent_preserving_thirty(harness,monkeypatch):
    p,s,proof=failed();old=copy.deepcopy(s);oldproof=copy.deepcopy(proof)
    e=F.Engine(p,'new-run','safe',s,unit_budget=3);e.before_intent=lambda:None;e.recovery_reader=lambda *a:proof
    monkeypatch.setattr(F,'export_budget',lambda *a,**kw:10)
    monkeypatch.setattr(C,'perf_post',lambda *a:(200,{'UUID':'new-safe-report'}))
    result=e.run(harness[3]);units=harness[2]
    assert [u['detail']['action'] for u in units]==['PRE_SOURCE_RECOVERED','REPORT_INTENT','REPORT_SUBMITTED']
    assert [u['state']['sequence'] for u in units]==[16,17,18]
    assert units[0]['state']['rows']==30 and units[0]['state']['progress']['pending']==old['progress']['pending']
    assert units[1]['state']['progress']['report']['execution']=='new-run'
    assert not result['evidence']['state']['complete'] and old['sequence']==15 and proof==oldproof


@pytest.mark.parametrize('change',[{'post_attempts':1},{'uuid_present':True},{'sku_rows_written':1},{'project':'foreign'},
    {'failure_stage':'HTTP_500_AFTER_POST'},{'stop_hashes':[]},{'unit_sequence':14},{'state_hash':'0'*64}])
def test_ambiguous_foreign_or_changed_proof_never_retries(harness,monkeypatch,change):
    p,s,proof=failed();proof.update(change);proof['hash']=B.digest({k:v for k,v in proof.items() if k!='hash'})
    e=F.Engine(p,'new-run','safe',s,unit_budget=3);e.recovery_reader=lambda *a:proof
    monkeypatch.setattr(C,'perf_post',lambda *a:pytest.fail('ambiguous POST repeated'))
    with pytest.raises(B.EvidenceError):e.run(harness[3])
    assert not harness[2] and not harness[0]


def test_missing_owner_attestation_stays_ambiguous(harness,monkeypatch):
    p,s,_=failed();e=F.Engine(p,'new-run','safe',s)
    with pytest.raises(B.EvidenceError,match='AMBIGUOUS'):e.run(harness[3])
    assert not harness[2]


def test_exact_runtime_permission_failure_precedes_catalog_and_writes(monkeypatch):
    p,_=prefix();queries=[]
    monkeypatch.setattr(F.bigquery,'QueryJobConfig',lambda **kw:SimpleNamespace(**kw),raising=False)
    class DB:
        def query(self,sql,**kw):
            queries.append(sql);raise PermissionError('CAPABILITY_PROFILE denied')
    monkeypatch.setattr(C,'bq',lambda:DB())
    with pytest.raises(PermissionError):F.internal_preflight(p,source_binding=False)
    assert len(queries)==1 and 'CAPABILITY_PROFILE' in queries[0] and queries[0].startswith('SELECT ')


@pytest.mark.parametrize('invalid', [0, 1])
def test_internal_catalog_gate_honors_nullable_string_sku_schema(monkeypatch, invalid):
    # RAW Catalog stores SKU as nullable STRING, and product_id is also STRING.
    # Accept NULL (archived SKU) and positive numeric strings; reject malformed/zero.
    p,_=prefix();queries=[]
    monkeypatch.setattr(F.bigquery,'QueryJobConfig',lambda **kw:SimpleNamespace(**kw),raising=False)
    monkeypatch.setattr(F.bigquery,'ScalarQueryParameter',lambda *args:args,raising=False)
    class DB:
        def query(self,sql,**kw):
            queries.append(sql)
            if 'CAPABILITY_PROFILE' in sql:
                rows=[{'api':'seller','status':'AVAILABLE'},{'api':'performance','status':'AVAILABLE'}]
            elif 'RAW_OZON_CATALOG' in sql:
                # Fail the regression if SQL directly compares a STRING SKU to INT64.
                import re
                assert not re.search(r'\b(?:sku|product_id)\s*<=\s*0',sql)
                assert 'SAFE_CAST(product_id AS INT64) IS NULL' in sql
                assert 'SAFE_CAST(sku AS INT64) IS NULL' in sql
                rows=[{'n':3,'products':3,'invalid':invalid}]
            else:rows=[{'n':1}]
            return SimpleNamespace(result=lambda:rows)
    monkeypatch.setattr(C,'bq',lambda:DB())
    if invalid:
        with pytest.raises(B.EvidenceError,match='PRE_INTENT_CURRENT_CATALOG_DENIED'):
            F.internal_preflight(p,source_binding=False)
        assert len(queries)==2
    else:
        assert F.internal_preflight(p,source_binding=False)['CAPABILITY_PROFILE_readable'] is True
        assert len(queries)==3


def test_full_plan_quota_revalidates_exact_original_owner_exception_only(monkeypatch):
    p,s,proof=failed();proof['root_hash']=F.QF.ROOT;proof['hash']=B.digest({k:v for k,v in proof.items() if k!='hash'});new=B.plan({'BACKFILL_MODE':B.VERSION,'BACKFILL_TARGET_PROJECT':p['project'],
        'TENANT_BINDING_REQUIRED':'1','STRICT_PAGE_CAPS':'1','SINCE':'2023-03-24','UNTIL':'2023-03-24',
        'BACKFILL_GENERATION':'full-new','BACKFILL_ORIGIN':'2026-10-07T00:00:00Z'},
        'ads_sku_daily',p['project'],'ozon_raw','ref',__import__('datetime').date(2026,10,7))
    from types import SimpleNamespace
    name=PS.marker(proof);calls=[]
    class DB:
        def list_tables(self,where):return [SimpleNamespace(table_id=name)]
        def get_table(self,where):return SimpleNamespace(description=PS.marker_value(proof)[1],labels=PS.marker_value(proof)[0])
        def query(self,sql,**kw):
            calls.append(sql)
            values=[{'evidence_json':__import__('json').dumps({'plan':p,'state':s}),
                     'backfill_detail_json':__import__('json').dumps({'action':'REPORT_INTENT','exports_reserved':10})}] if 'SELECT DISTINCT' in sql else [{'status':'FAILED'}]
            return SimpleNamespace(result=lambda:values)
    monkeypatch.setattr(C,'bq',lambda:DB())
    monkeypatch.setattr(F.bigquery,'QueryJobConfig',lambda **kw:SimpleNamespace(**kw),raising=False)
    monkeypatch.setattr(F.bigquery,'ScalarQueryParameter',lambda *a:a,raising=False)
    assert F.read_recovery_proofs(new)==[] and not calls
    assert F.read_recovery_proofs(new,accounting=True)==[proof] and len(calls)==2
    proof['post_attempts']=1;proof['hash']=B.digest({k:v for k,v in proof.items() if k!='hash'});name=PS.marker(proof)
    with pytest.raises(B.EvidenceError):F.read_recovery_proofs(new,accounting=True)


def test_quota_exception_matches_plan_and_sequence_pair_not_sequence_alone(monkeypatch):
    p,s,proof=failed();seen=[]
    monkeypatch.setattr(F,'read_recovery_proofs',lambda p,**kw:[proof])
    monkeypatch.setattr(F.bigquery,'QueryJobConfig',lambda **kw:SimpleNamespace(**kw),raising=False)
    monkeypatch.setattr(F.bigquery,'ArrayQueryParameter',lambda *a:a,raising=False)
    class DB:
        def query(self,sql,**kw):
            seen.append((sql,kw['job_config'].query_parameters))
            return SimpleNamespace(result=lambda:[{'unknown_runs':0,'used':15,'accepted':15}])
    monkeypatch.setattr(C,'bq',lambda:DB())
    assert F.export_budget(p=p)==0
    sql,params=seen[0]
    assert 'CONCAT(backfill_plan_id' in sql
    assert params[1]==('no_post_units','STRING',[p['plan_id']+':15'])
