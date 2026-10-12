"""Diagnostic samples never enter ingestion, including fully valid pages."""
import json
import pytest
import common as C
import fbo_diagnostic as Q

def scope(n=12):return {'since':'2026-08-18T21:00:00.000Z','to':'2026-08-25T20:59:59.999Z','until_exclusive':'2026-08-25T21:00:00.000Z','cursor':'','limit':100,'request_limit':n}
def record(created='2026-08-19T01:00:00Z',processed='2026-08-19T04:00:00+03:00'):
    return {'created_at':created,'in_process_at':processed,'status':'delivering','posting_number':'PRIVATE-POSTING','products':[{'sku':123456}]}

def forbid_writes(monkeypatch):
    def denied(*a,**k):raise AssertionError('diagnostic must never write/advance')
    for name in ('merge','record_run','load_json','load_rows'):
        if hasattr(C,name):monkeypatch.setattr(C,name,denied)

def test_twelve_pages_stop_at_discriminating_pair_without_writes(monkeypatch):
    forbid_writes(monkeypatch);calls=[]
    def send(body):
        calls.append(body.copy());n=len(calls)
        return 200,{'postings':[record('2026-08-18T05:16:22.460142Z') if n==12 else record()], 'cursor':'opaque-'+str(n),'has_next':True}
    e=Q.collect(scope(),send)
    assert len(calls)==e['source_requests']==12 and len(e['pages'])==12
    assert all(b['filter']=={'since':scope()['since'],'to':scope()['to']} for b in calls)
    last=e['pages'][-1]['records'][0]
    assert last['created_at']['validation_class']=='OUTSIDE_REQUESTED_INTERVAL' and last['in_process_at']['validation_class']=='IN_REQUESTED_INTERVAL'
    assert last['page_ordinal']==last['request_ordinal']==12
    assert not any(k in json.dumps(e) for k in ('PRIVATE-POSTING','opaque-','123456'))
    assert e['semantics']=='UNPROVEN' and all(e[k]==0 for k in ('raw_writes','source_units','checkpoints','receipts','reconciliation'))

@pytest.mark.parametrize('value,expected',[('2026-08-18T21:00:00Z','IN_REQUESTED_INTERVAL'),('2026-08-19T00:00:00+03:00','IN_REQUESTED_INTERVAL'),('2026-08-25T21:00:00Z','OUTSIDE_REQUESTED_INTERVAL'),('2026-08-19T00:00:00','MISSING_TIMEZONE'),('malformed','INVALID_TIMESTAMP_FORMAT'),(None,'INVALID_TIMESTAMP_FORMAT')])
def test_strict_paired_timestamp(value,expected,monkeypatch):
    forbid_writes(monkeypatch);e=Q.collect(scope(),lambda b:(200,{'postings':[record(value,value)],'has_next':False,'cursor':''}))
    assert e['source_requests']==1
    assert all(e['pages'][0]['records'][0][k]['validation_class']==expected for k in ('created_at','in_process_at'))

@pytest.mark.parametrize('code',[429,500,502,503,'NET_ERROR'])
def test_no_transport_or_pagination_retry_after_failure(code):
    calls=[]
    def send(b):calls.append(b);return code,None
    assert Q.collect(scope(),send)['source_requests']==1 and len(calls)==1

def test_repeated_cursor_never_loops():
    calls=[]
    def send(b):calls.append(b);return 200,{'postings':[record()],'cursor':'same','has_next':True}
    assert Q.collect(scope(),send)['source_requests']==2 and len(calls)==2

def test_terminal_and_valid_sample_does_not_claim_transport_semantics():
    e=Q.collect(scope(),lambda b:(200,{'postings':[record()],'has_next':False,'cursor':''}))
    assert e['semantics']=='UNPROVEN' and e['source_requests']==1

def test_unknown_authority_rejected_before_source():
    with pytest.raises(ValueError):Q.validate({},'mpa-t-client-001','a'*64,None)

def test_real_frozen_policy_validates_exact_Moscow_to_UTC_bounds():
    from tools.tenancy.fbo_diagnostic_image_check import diagnostic_protocol,diagnostic_policy
    import backfill_core as B
    m,r,c,factory,*_=diagnostic_protocol();b=factory(0);p=diagnostic_policy(m,c,b.clock())
    assert (p['since'],p['to'],p['until_exclusive'])==('2026-08-18T21:00:00.000Z','2026-08-25T20:59:59.999Z','2026-08-25T21:00:00.000Z')
    assert Q.validate(p,m['project'],B.implementation_hash(),b.clock())==p
    p['since']='2026-08-18T00:00:00.000Z';p['hash']=Q.digest({k:v for k,v in p.items() if k!='hash'})
    with pytest.raises(ValueError):Q.validate(p,m['project'],B.implementation_hash(),b.clock())

def test_main_diagnostic_never_runs_ingestion(monkeypatch):
    import main
    monkeypatch.setattr(main.sys,'argv',['main.py','--fbo-window-diagnostic','a'*64]);calls=[]
    monkeypatch.setattr(Q,'run',lambda *a:calls.append(a))
    monkeypatch.setattr(C,'record_run',lambda *a,**k:pytest.fail('ingestion journal written'))
    main.main(['--fbo-window-diagnostic','a'*64]);assert len(calls)==1
