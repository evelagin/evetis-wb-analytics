"""Packaged network-free initialization ordering and frozen plan identity proof."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone,date
from unittest.mock import patch
import backfill_core as B
import backfill as F
import common as C
import full_resume as FR
import cooldown_failed as CF

NOW=datetime(2026,10,10,8,tzinfo=timezone.utc)

def check():
    env={'BACKFILL_MODE':B.VERSION,'BACKFILL_TARGET_PROJECT':'synthetic-project','TENANT_BINDING_REQUIRED':'1','STRICT_PAGE_CAPS':'1','SINCE':'2026-10-05','UNTIL':'2026-10-07','BACKFILL_GENERATION':'synthetic-image-cooldown','BACKFILL_ORIGIN':'2026-10-09T00:00:00Z'}
    p=B.plan(env,'ads_sku_daily','synthetic-project','ozon_raw','ref',NOW.date());s=B.initial(p)
    s['sequence']=3;s['progress']['rate_limit']={'safe_cap':15,'count':3,'eligible_at':(NOW+timedelta(hours=1)).isoformat()}
    calls=[];writes=[]
    def denied(*a,**kw):raise AssertionError('cooldown reached credential/source boundary')
    with patch.object(C,'perf_token',denied),patch.object(C,'perf_get',denied),patch.object(C,'perf_post',denied):
        e=F.Engine(p,'synthetic-no-source','safe',s);e.clock=lambda:NOW
        out=e.run(lambda r,v:writes.append(v))
        assert not writes and e.requests==0 and out['evidence']['state']==s
        assert out['evidence']['wait']['eligible_at']==s['progress']['rate_limit']['eligible_at']
        ambiguous=deepcopy(s);ambiguous['progress']['report']={'phase':'INTENT','batch':['synthetic'],'execution':'unknown'}
        ambiguous['progress']['pending']=['synthetic']
        e=F.Engine(p,'synthetic-ambiguous','safe',ambiguous);e.clock=lambda:NOW
        try:e.run(denied)
        except B.EvidenceError as error:assert str(error).startswith('REPORT_SUBMISSION_AMBIGUOUS')
        else:raise AssertionError('ambiguous submission hidden by cooldown')
    s['progress']['rate_limit']['eligible_at']=NOW.isoformat()
    def rejected(path,**kw):
        calls.append(path)
        return 429,{}
    with patch.object(C,'perf_get',rejected),patch.object(C,'perf_post',denied),patch.object(C,'perf_diagnostic',lambda:dict(http_status=429,**{'retry-after':7200})):
        e=F.Engine(p,'synthetic-expired','safe',s);e.clock=lambda:NOW
        out=e.run(lambda r,v:writes.append(v));s=out['evidence']['state']
        assert s['sequence']==4 and s['progress']['rate_limit']['count']==4 and not s['complete']
        assert s['progress']['rate_limit']['eligible_at']==(NOW+timedelta(hours=2)).isoformat() and len(calls)==1
        e=F.Engine(p,'synthetic-wait-again','safe',s);e.clock=lambda:NOW;e.run(denied)
        assert e.requests==0 and len(calls)==1 and len(writes)==1
    m=FR.manifest();pr=m['programs'][CF.EXACT['index']]
    env.update(SINCE=pr['from'],UNTIL=pr['to'],BACKFILL_TARGET_PROJECT=m['project'],BACKFILL_GENERATION='full-'+m['hash'][:24]+'-'+str(CF.EXACT['index']),BACKFILL_ORIGIN=m['created_at'],BACKFILL_FULL_ROOT_HASH=m['hash'],BACKFILL_RESUME_PLAN_ID=CF.EXACT['plan_id'])
    original=B.plan(env,pr['entity'],m['project'],'ozon_raw','ref',date(2026,10,10))
    assert original['plan_id']==CF.EXACT['plan_id'] and original['implementation_hash']==m['runtime_implementation_hash']
    return dict(performance_cooldown_before_initialization='PASS',performance_repeated_429_wait='PASS',performance_ambiguity_stops='PASS',full_frozen_plan_resume='PASS',full_resume_root=m['hash'])

if __name__=='__main__':
    import json
    print(json.dumps(check(),sort_keys=True))
