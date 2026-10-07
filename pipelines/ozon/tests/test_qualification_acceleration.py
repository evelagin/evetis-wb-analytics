"""Offline qualification continuation, HTTP rejection and ambiguous POST adversaries."""
import copy
import io
import json
import urllib.error
import urllib.request
import zipfile
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import backfill_core as B
import backfill as F
import qualification as Q
import common as C
from test_backfill_engine import harness  # network-denying merge/journal fixture


def prefix():
    p=Q.accepted_doc(Q.SKU)['runtime_plan'];s=B.initial(p)
    s['sequence']=14;s['rows']=30
    s['progress'].update(pending=[str(i) for i in range(31,91)],report=None,skipped=0)
    return p,s


def test_exact_scope_resume_preserves_historical_identity_and_real_artifact_hash():
    p,_=prefix()
    e=dict(BACKFILL_MODE=B.VERSION,TENANT_BINDING_REQUIRED='1',STRICT_PAGE_CAPS='1',
           BACKFILL_TARGET_PROJECT=p['project'],SINCE=p['from'],UNTIL=p['to'],
           BACKFILL_GENERATION=p['generation'],BACKFILL_ORIGIN=p['origin'],BACKFILL_RESUME_PLAN_ID=Q.SKU)
    assert B.plan(e,p['entity'],p['project'],p['raw'],p['ref'],date(2026,10,7))==p
    assert B.implementation_hash()!=p['implementation_hash'] # honest new artifact
    for key,val in [('BACKFILL_TARGET_PROJECT','other'),('UNTIL','2026-09-18'),('BACKFILL_GENERATION','new'),('BACKFILL_RESUME_PLAN_ID','0'*64)]:
        bad=dict(e,**{key:val})
        with pytest.raises(B.EvidenceError): B.plan(bad,p['entity'],p['project'],p['raw'],p['ref'],date(2026,10,7))


def test_calibration_only_exact_accepted_plan_and_reconciled_prefix():
    p,s=prefix();assert Q.guard(p,s)==25
    for completed,cap in [(40,35),(50,45),(60,55),(70,65),(80,75)]:
        s['progress']['pending']=[str(i) for i in range(completed+1,91)]
        assert Q.guard(p,s)==cap
    s['progress']['pending']=['1']*50
    with pytest.raises(B.EvidenceError):Q.guard(p,s)
    assert Q.guard(dict(p,plan_id='1'*64),s)==15
    bad=dict(p,to='2026-09-18')
    with pytest.raises(B.EvidenceError):Q.guard(bad,s)


def test_thirty_preserved_six_bounded_submissions_complete_ninety(harness,monkeypatch):
    p,s=prefix();db,writes,proofs,persist=harness
    submitted=[];reserved=[15];limits=[];current=[None]
    monkeypatch.setattr(F,'export_budget',lambda cap:(limits.append(cap) or max(cap-reserved[0],0)))
    def post(path,body):
        assert path=='/api/client/statistics' and len(body['campaigns'])<=10
        assert not set(body['campaigns']) & {str(i) for i in range(1,31)}
        assert proofs[-1]['detail']['action']=='REPORT_INTENT'
        assert proofs[-1]['detail']['cohort_hash']==Q.cohort(p,p['from'],body['campaigns'])
        current[0]=copy.deepcopy(body['campaigns']);submitted.append(copy.deepcopy(body));return 200,{'UUID':'safe-report'}
    def get(path,**kw):
        if '/report?' not in path:return 200,{'state':'OK'}
        b=io.BytesIO()
        with zipfile.ZipFile(b,'w') as z:
            for cid in current[0]:
                z.writestr(cid+'.csv',f'title\nsku;День;Расход, ₽, с НДС\n{cid};17.09.2026;10\n')
        return 200,b.getvalue().decode('utf-8','surrogateescape')
    monkeypatch.setattr(C,'perf_post',post);monkeypatch.setattr(C,'perf_get',get)
    def ack(result,ev):
        persist(result,ev)
        if ev['detail'].get('exports_reserved'):reserved[0]+=ev['detail']['exports_reserved']
    for i in range(6):
        out=F.Engine(p,'new-'+str(i),'safe',s,unit_budget=3).run(ack)
        s=out['evidence']['state']
    assert s['complete'] and s['sequence']==32 and len(submitted)==6
    assert s['rows']==90 and len(db['RAW_OZON_ADS_SKU_DAILY'])==60
    assert limits==[25,35,45,55,65,75]
    assert all(ev['plan']==p for ev in proofs)


def test_lost_intent_ack_never_posts(harness,monkeypatch):
    p,s=prefix();monkeypatch.setattr(F,'export_budget',lambda cap:10)
    monkeypatch.setattr(C,'perf_post',lambda *a:pytest.fail('POST after missing journal ACK'))
    with pytest.raises(RuntimeError,match='lost ACK'):
        F.Engine(p,'new','safe',s,unit_budget=3).run(lambda *a:(_ for _ in ()).throw(RuntimeError('lost ACK')))


def test_submission_binding_revocation_prevents_post(harness,monkeypatch):
    p,s=prefix();monkeypatch.setattr(F,'export_budget',lambda cap:10)
    e=F.Engine(p,'new','safe',s,unit_budget=3)
    e.before_submit=lambda:(_ for _ in ()).throw(B.EvidenceError('binding revoked'))
    with pytest.raises(B.EvidenceError,match='revoked'):e.run(harness[3])
    assert harness[2][-1]['state']['progress']['report']['phase']=='INTENT'


def test_explicit_429_is_durable_cooldown_not_ambiguous_repost(harness,monkeypatch):
    p,s=prefix();monkeypatch.setattr(F,'export_budget',lambda cap:10)
    C.PERF_DIAGNOSTIC.clear();C.PERF_DIAGNOSTIC.update({'http_status':429,'retry-after':7200,'x-ratelimit-remaining':0})
    posts=[];monkeypatch.setattr(C,'perf_post',lambda *a:(posts.append(1) or 429,{'_error':'not persisted'}))
    e=F.Engine(p,'new','safe',s,unit_budget=3);out=e.run(harness[3]);final=out['evidence']['state']
    assert len(posts)==1 and not final['complete'] and len(final['progress']['pending'])==60
    assert final['progress']['report'] is None and Q.guard(p,final)==15
    assert harness[2][-1]['detail']['submission_rejected'] is True
    again=F.Engine(p,'next','safe',final,unit_budget=3).run(harness[3])
    assert again['evidence']['execution_requests']==0 and len(posts)==1


@pytest.mark.parametrize('failure',[429,500,'network'])
def test_real_http_transport_never_retries_report_post(monkeypatch,failure):
    attempts=[];monkeypatch.setattr(C.time,'sleep',lambda _:pytest.fail('unsafe retry sleep'))
    def fail(req,**kw):
        attempts.append(1)
        if failure=='network':raise TimeoutError('synthetic lost response')
        raise urllib.error.HTTPError(req.full_url,failure,'synthetic',{'Retry-After':'42','Authorization':'DO_NOT_PERSIST'},io.BytesIO(b'not logged'))
    monkeypatch.setattr(C.urllib.request,'urlopen',fail)
    request=urllib.request.Request(C.PERF+'/api/client/statistics',data=b'{}')
    status,_=C._request(request)
    assert status==('NET_ERROR' if failure=='network' else failure) and len(attempts)==1
    assert 'Authorization' not in C.PERF_DIAGNOSTIC


def test_invalid_hash_or_missing_file_cannot_complete(harness,monkeypatch):
    p,s=prefix();s['progress']['report']={'phase':'POLL','batch':['31'],'uuid':'safe','cohort_hash':'bad'}
    with pytest.raises(B.EvidenceError,match='hash'):F.Engine(p,'new','safe',s).run(harness[3])
    assert not harness[0]
