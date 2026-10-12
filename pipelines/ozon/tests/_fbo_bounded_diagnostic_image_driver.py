"""Execute actual packaged diagnostic with network disabled and no writes."""
import sys,json
sys.path.insert(0,'/app')
import common as C,fbo_diagnostic as Q
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
print(json.dumps({'bounded_fbo_diagnostic':'PASS','diagnostic_no_business_effects':'PASS','diagnostic_requests_max12':'PASS','network':'NONE'}))
