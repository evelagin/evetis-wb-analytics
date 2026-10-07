"""Installed runtime current-only adapters: no real clients, credentials or network."""
import copy
import json
import sys
from pathlib import Path
from datetime import timedelta
sys.path.insert(0,str(Path('/app') if Path('/app/backfill.py').is_file() else Path(__file__).resolve().parents[1]/'runtime'))
import backfill as F
import backfill_core as B
import common as C


def qualify():
    saved=(C.seller_post,C.merge_rows,F.Engine.stock_catalog_page,F.internal_preflight)
    db={};proofs=[];calls=[];day=C.now_msk().date()
    def plan(entity):
        prior=str(day-timedelta(days=1))
        return B.plan({'BACKFILL_MODE':B.VERSION,'TENANT_BINDING_REQUIRED':'1','STRICT_PAGE_CAPS':'1',
            'BACKFILL_TARGET_PROJECT':C.PROJECT,'SINCE':prior,'UNTIL':prior,
            'BACKFILL_GENERATION':'installed-current-snapshot','BACKFILL_ORIGIN':'2026-10-01T00:00:00Z'},
            entity,C.PROJECT,'ozon_raw','ref',day)
    def merge(table,rows,keys,run,**kw):
        columns=sorted(set().union(*(r.keys() for r in rows))) if rows else []
        C.validate_merge_batch(table,rows,keys,columns,kw['on_duplicate_key'])
        dest=db.setdefault(table,{})
        for row in rows:dest[C.merge_key(row,keys)]=copy.deepcopy(row)
        return {'received':len(rows),'inserted':len(rows),'updated':0}
    def run(p,state=None,units=20):
        return F.Engine(p,'synthetic-installed-run',C.now_msk().isoformat(),state or B.initial(p),unit_budget=units).run(
            lambda result,proof:proofs.append(copy.deepcopy(proof)))['evidence']['state']
    def source(path,body):
        calls.append(path)
        if path=='/v5/product/info/prices':
            offer='offer-a' if not body['cursor'] else 'offer-b'
            return 200,{'total':2,'items':[{'offer_id':offer,'product_id':7,'price':{'price':'100'},'commissions':{}}],
                        'cursor':'second' if not body['cursor'] else ''}
        if path=='/v1/analytics/stocks':
            assert body['skus']
            return 200,{'items':[{'sku':x,'warehouse_id':8,'available_stock_count':0} for x in body['skus']]}
        if path=='/v1/seller/info':return 200,{'company':{'inn':'NEVER_STORE','ogrn':'NEVER_STORE','currency':'RUB'}}
        if path=='/v1/rating/summary':return 200,{'premium':False}
        if path=='/v1/cluster/list':
            assert body=={'cluster_type':'CLUSTER_TYPE_OZON'}
            return 200,{'clusters':[]}
        raise AssertionError('unreviewed source path')
    try:
        C.seller_post=source;C.merge_rows=merge
        F.Engine.stock_catalog_page=lambda self,day,last:['100','101'] if not last else []
        F.internal_preflight=lambda *a,**k:{'catalog':'PASS'}
        p=plan('prices');first=run(p,units=1)
        assert not first['complete'] and len(db['RAW_OZON_PRICES'])==1
        final=run(p,first);assert final['complete'] and len(db['RAW_OZON_PRICES'])==2
        assert len(db['RAW_OZON_PRICE_COMMISSIONS'])==2
        p=plan('stocks');first=run(p,units=1)
        assert not first['complete'] and first['progress']['skus']==2
        assert run(p,first)['complete'] and calls.count('/v1/analytics/stocks')==1
        assert run(plan('seller_info'))['complete'] and 'NEVER_STORE' not in repr(db)
        assert run(plan('clusters'))['complete']
        assert all(r['snapshot_date']==str(day) for table in db.values() for r in table.values())
        stale=plan('prices');stale['observation_date']=str(day-timedelta(days=1));stale['plan_id']=B.digest({k:v for k,v in stale.items() if k!='plan_id'})
        before=len(calls)
        try:run(stale)
        except B.EvidenceError:pass
        else:raise AssertionError('stale snapshot allowed')
        assert len(calls)==before
        return {'full_current_snapshot_adapters':'PASS','current_snapshot_resume':'PASS',
                'snapshot_no_fake_history':'PASS','backfill_implementation_hash':B.implementation_hash(),
                'credential_reads':0,'network':'NONE'}
    finally:
        C.seller_post,C.merge_rows,F.Engine.stock_catalog_page,F.internal_preflight=saved

if __name__=='__main__':print(json.dumps(qualify(),sort_keys=True))
