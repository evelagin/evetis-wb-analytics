"""Real initialization ordering and diagnostic boundaries; network denied."""
import copy
import io
import json
import urllib.request
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
import pytest
import backfill as F
import backfill_core as B
import common as C
from test_backfill_engine import harness,plan

NOW=datetime(2026,10,10,8,tzinfo=timezone.utc)

def cooled():
    p=plan('ads_sku_daily');s=B.initial(p);s['sequence']=3
    s['progress']['rate_limit']={'safe_cap':15,'count':3,'eligible_at':(NOW+timedelta(hours=1)).isoformat()}
    return p,s

def run(p,s,persist):
    e=F.Engine(p,'synthetic-cooldown','safe',s);e.clock=lambda:NOW
    return e,e.run(persist)

def test_uninitialized_active_cooldown_has_no_oauth_campaign_expense_or_journal(harness,monkeypatch):
    p,s=cooled();before=copy.deepcopy(s)
    monkeypatch.setattr(C,'perf_token',lambda:pytest.fail('OAuth during cooldown'))
    e,out=run(p,s,harness[3])
    assert e.requests==0 and out['evidence']['state']==before and not harness[2] and not harness[0]
    assert out['evidence']['wait']=={'basis':'SOURCE_COOLDOWN','eligible_at':s['progress']['rate_limit']['eligible_at']}

def test_expired_cooldown_initialization_is_eligible_and_fourth_429_is_saved(harness,monkeypatch):
    p,s=cooled();s['progress']['rate_limit']['eligible_at']=NOW.isoformat();calls=[]
    def get(path,**kw):
        calls.append(path)
        C.PERF_DIAGNOSTIC.clear();C.PERF_DIAGNOSTIC.update(http_status=429,**{'retry-after':7200})
        return 429,{}
    monkeypatch.setattr(C,'perf_get',get)
    e,out=run(p,s,harness[3]);state=out['evidence']['state']
    assert calls==['/api/client/campaign?page=1&pageSize=100']
    assert state['sequence']==4 and state['progress']['rate_limit']=={'safe_cap':15,'count':4,'eligible_at':(NOW+timedelta(hours=2)).isoformat()}
    assert harness[2][-1]['detail']['action']=='SOURCE_THROTTLED' and not state['complete'] and 'pending' not in state['progress']
    e,again=run(p,state,harness[3]);assert e.requests==0 and again['evidence']['state']==state and len(calls)==1

def test_ambiguous_intent_is_not_hidden_by_cooldown(harness):
    p,s=cooled();s['progress'].update(pending=['synthetic'],report={'phase':'INTENT','execution':'unknown','batch':['synthetic']})
    with pytest.raises(B.EvidenceError,match='REPORT_SUBMISSION_AMBIGUOUS'):run(p,s,harness[3])
    assert not harness[2]

@pytest.mark.parametrize('path,expected',[
    ('/api/client/statistics/expense?dateFrom=2026-10-05&dateTo=2026-10-05','/api/client/statistics/expense'),
    ('/api/client/statistics/report?UUID=synthetic','/api/client/statistics/report'),
    ('/api/client/statistics/synthetic-private-report','/api/client/statistics/{report_id}'),
    ('/api/client/campaign/123/v2/products?page=1','/api/client/campaign/{campaign_id}/v2/products'),
])
def test_trace_keeps_route_and_status_without_identifiers(monkeypatch,path,expected):
    monkeypatch.setattr(C,'PERF_REQUEST_TRACE',[]);monkeypatch.setattr(C,'PERF_TRACE_COMPLETE',True)
    logs=[];monkeypatch.setattr(C,'log',lambda **kw:logs.append(kw))
    C._perf_request_evidence(urllib.request.Request(C.PERF+path),429)
    assert C.perf_trace()=={'complete':True,'requests':[{'method':'GET','endpoint':expected,'http_status':429}]}
    assert 'UUID' not in str(logs) and 'synthetic-private-report' not in str(logs)

def test_logging_failure_does_not_retry_accepted_http(monkeypatch):
    calls=[];monkeypatch.setattr(C,'PERF_REQUEST_TRACE',[]);monkeypatch.setattr(C,'PERF_TRACE_COMPLETE',True)
    class Response:
        status=200;headers={}
        def __enter__(self):return self
        def __exit__(self,*a):return False
        def read(self):return b'{"UUID":"synthetic"}'
    monkeypatch.setattr(C.urllib.request,'urlopen',lambda *a,**kw:calls.append(1) or Response())
    monkeypatch.setattr(C,'log',lambda **kw:(_ for _ in ()).throw(OSError('synthetic stdout failure')))
    status,result=C._request(urllib.request.Request(C.PERF+'/api/client/statistics',data=b'{}'))
    assert status==200 and result=={'UUID':'synthetic'} and calls==[1] and C.perf_trace()['complete'] is False


def test_installed_image_check_executes_real_engine_network_free():
    from cooldown_image_check import check
    out=check()
    assert out['performance_cooldown_before_initialization']=='PASS'
    assert out['performance_repeated_429_wait']=='PASS'
    assert out['full_frozen_plan_resume']=='PASS'


def test_failed_full_attempt_retains_authority_without_fabricating_ok_unit(monkeypatch):
    import full_resume as FR
    import cooldown_failed as CF
    m=FR.manifest();i=CF.EXACT['index'];program=m['programs'][i]
    env=dict(BACKFILL_MODE=B.VERSION,BACKFILL_TARGET_PROJECT=m['project'],TENANT_BINDING_REQUIRED='1',STRICT_PAGE_CAPS='1',
        SINCE=program['from'],UNTIL=program['to'],BACKFILL_GENERATION='full-'+m['hash'][:24]+'-'+str(i),
        BACKFILL_ORIGIN=m['created_at'],BACKFILL_FULL_ROOT_HASH=m['hash'],BACKFILL_RESUME_PLAN_ID=CF.EXACT['plan_id'])
    p=B.plan(env,program['entity'],m['project'],'ozon_raw','ref',NOW.date())
    doc=FR.document(dict(mode='BOUNDED_PILOT',tenant_id=m['tenant'],image=m['runtime_image'],runtime_plan=p,
        max_requests=400,max_units=20,max_order_batches=1,ack_hash=''))
    authority=dict(root=m['hash'],index=i,shard=B.digest(dict(full_root=m['hash'],index=i,ack_hash=doc['ack_hash'])),receipt_sequence=5,plan_id=p['plan_id'])
    monkeypatch.setenv('BACKFILL_FULL_AUTHORITY',json.dumps(authority));monkeypatch.setenv('BACKFILL_FULL_ROOT_HASH',m['hash'])
    monkeypatch.setenv('CLOUD_RUN_EXECUTION','synthetic-failed-execution')
    monkeypatch.setattr(C,'perf_trace',lambda:dict(complete=True,requests=[dict(method='GET',endpoint='/api/client/campaign',http_status=429)]))
    s=B.initial(p);s['sequence']=3
    proof=F.failure_evidence(p,s)
    rows=[];monkeypatch.setattr(C,'bq',lambda:SimpleNamespace(insert_rows_json=lambda table,values:rows.extend(values) or []))
    monkeypatch.setattr(C,'log',lambda **kw:None)
    C.record_run('synthetic-failed',p['entity'],NOW,p['from'],p['to'],dict(evidence=proof),'FAILED',error='synthetic',requests_n=1)
    row=rows[0];saved=json.loads(row['evidence_json'])
    assert row['status']=='FAILED' and row['backfill_plan_id']==p['plan_id'] and 'backfill_sequence' not in row
    assert saved['authority']==authority and saved['runtime_execution']=='synthetic-failed-execution'
    assert saved['failure'] is True and saved['source_category']=='PERFORMANCE' and not saved['state']['complete']
    authority['shard']='0'*64;monkeypatch.setenv('BACKFILL_FULL_AUTHORITY',json.dumps(authority))
    with pytest.raises(B.EvidenceError,match='shard/plan linkage'):F.failure_evidence(p,s)
