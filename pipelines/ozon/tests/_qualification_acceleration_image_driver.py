"""Installed runtime qualification with network and credential access denied."""
import copy
import io
import json
import sys
import urllib.request
import urllib.error
import zipfile
from pathlib import Path
sys.path.insert(0,'/app')
import common as C
import backfill as F
import backfill_core as B
import qualification as Q


def qualify():
    denied=lambda *a,**kw:(_ for _ in ()).throw(AssertionError('network/credential access'))
    C.secret=denied;C.perf_token=denied;C.bq=denied
    p=Q.accepted_doc(Q.SKU)['runtime_plan'];s=B.initial(p)
    s.update(sequence=14,rows=30);s['progress'].update(pending=[str(i) for i in range(31,91)],report=None,skipped=0)
    db={};proofs=[];posts=[];limits=[];reserved=[15];batch=[None]
    F.time.sleep=lambda _:None
    F.export_budget=lambda cap:(limits.append(cap) or max(0,cap-reserved[0]))
    def ack(result,ev):
        proofs.append(copy.deepcopy(ev));reserved[0]+=ev['detail'].get('exports_reserved',0)
    def merge(table,rows,keys,run,**kw):
        C.validate_merge_batch(table,rows,keys,sorted(set().union(*(r.keys() for r in rows))))
        for r in rows:
            k=C.merge_key(r,keys);assert k not in db;db[k]=r
        return {'received':len(rows),'inserted':len(rows),'updated':0}
    C.merge_rows=merge
    def post(path,body):
        assert proofs[-1]['detail']['action']=='REPORT_INTENT' and path=='/api/client/statistics'
        assert len(body['campaigns'])<=10 and all(int(cid)>30 for cid in body['campaigns'])
        batch[0]=body['campaigns'];posts.append(body);return 200,{'UUID':'synthetic-image-report'}
    def get(path,**kw):
        if '/report?' not in path:return 200,{'state':'OK'}
        b=io.BytesIO()
        with zipfile.ZipFile(b,'w') as z:
            for cid in batch[0]:z.writestr(cid+'.csv',f'title\nsku;День;Расход, ₽, с НДС\n{cid};17.09.2026;10\n')
        return 200,b.getvalue().decode('utf-8','surrogateescape')
    C.perf_post=post;C.perf_get=get
    for i in range(6):s=F.Engine(p,'synthetic-'+str(i),'safe',s,unit_budget=3).run(ack)['evidence']['state']
    assert s['complete'] and s['rows']==90 and len(db)==60 and len(posts)==6 and s['plan_id']==Q.SKU
    assert limits==[25,35,45,55,65,75]
    s=B.initial(p);s['sequence']=14;s['progress'].update(pending=[str(i) for i in range(31,91)],report=None)
    s['progress']['report']={'phase':'INTENT','batch':['31'],'execution':'lost-response'}
    try:F.Engine(p,'different','safe',s).run(ack)
    except B.EvidenceError as e:assert 'AMBIGUOUS' in str(e)
    else:raise AssertionError('ambiguous POST repeated')
    attempts=[]
    def fail(req,**kw):
        attempts.append(1);raise urllib.error.HTTPError(req.full_url,429,'synthetic',{'Retry-After':'7200','Authorization':'secret-not-emitted'},io.BytesIO(b'ignored'))
    C.urllib.request.urlopen=fail
    status,_=C._request(urllib.request.Request(C.PERF+'/api/client/statistics',data=b'{}'))
    assert status==429 and len(attempts)==1 and C.perf_diagnostic()['retry-after']==7200 and 'Authorization' not in C.perf_diagnostic()
    return {'qualification_acceleration':'PASS','qualification_resume_root':Q.ROOT,'backfill_implementation_hash':B.implementation_hash(),
            'prefix_30_preserved':'PASS','six_reports_remaining_60':'PASS','cohort_hash':'PASS','lost_post_no_repeat':'PASS',
            'numeric_diagnostic_whitelist':'PASS','network':'NONE','credential_reads':0}


if __name__=='__main__':print(json.dumps(qualify(),sort_keys=True))
