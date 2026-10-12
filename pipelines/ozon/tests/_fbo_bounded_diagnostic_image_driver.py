"""Execute actual packaged diagnostic with network disabled and no writes."""
import sys,json
sys.path.insert(0,'/app')
import common as C,fbo_diagnostic as Q
import full_resume as F,backfill_core as B
from datetime import datetime,timezone,timedelta
def denied(*a,**k):raise AssertionError('network/credential/business write denied')
C.secret=C._seller_open=C.record_run=denied
if hasattr(C,'merge'):C.merge=denied
calls=[]
p=dict(since='2026-08-18T21:00:00.000Z',to='2026-08-25T20:59:59.999Z',until_exclusive='2026-08-25T21:00:00.000Z',cursor='',limit=100,request_limit=12)
def send(body):
    calls.append(body);n=len(calls)
    return 200,{'postings':[{'created_at':'2026-08-18T05:16:22.460142Z' if n==12 else '2026-08-19T00:00:00Z','in_process_at':'2026-08-19T03:00:00+03:00','status':'delivering'}],'cursor':'opaque'+str(n),'has_next':True}
e=Q.collect(p,send)
assert len(calls)==12 and e['source_requests']==12 and e['pages'][-1]['records'][0]['created_at']['validation_class']=='OUTSIDE_REQUESTED_INTERVAL'
assert all(e[k]==0 for k in ('raw_writes','source_units','checkpoints','receipts','reconciliation')) and e['semantics']=='UNPROVEN'
now=datetime.now(timezone.utc);m=F.manifest()
policy=dict(version=1,purpose='FBO_BOUNDED_READ_NO_BUSINESS_EFFECT_V1',tenant=m['tenant'],project=m['project'],root=m['hash'],leaf=269,shard='1'*64,plan_id='2'*64,failed_run='bf-02277a37-829d-45a4-90f4-f3ff8f9cb2fe',failed_generation=192,failed_receipt_hash='3'*64,closure_hash='4'*64,run_id='bf-00000000-0000-0000-0000-000000000193',generation=193,**p,runtime_implementation_hash=B.implementation_hash(),controller_source='a'*40,controller_image='controller',runtime_image='runtime',owner_decision_sha256='5'*64,expires_at=(now+timedelta(minutes=30)).isoformat())
policy['hash']=Q.digest(policy);assert Q.validate(policy,m['project'],B.implementation_hash(),now)==policy
print(json.dumps({'bounded_fbo_diagnostic':'PASS','diagnostic_no_business_effects':'PASS','diagnostic_requests_max12':'PASS','fbo_frozen_utc_policy':'PASS','network':'NONE'}))
