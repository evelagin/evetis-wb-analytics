"""Network-free qualification against /app code in the exact built runtime image.

No credential values, cloud auth or data leave the container. Synthetic adapters
exercise the installed mapper/core/engine combination, not a second implementation.
"""
from __future__ import annotations
import copy
import json
import sys
from datetime import date
from pathlib import Path

runtime=Path('/app') if Path('/app/backfill.py').is_file() else Path(__file__).resolve().parents[1]/'runtime'
sys.path.insert(0,str(runtime))
import backfill_core as B
import backfill as F
import common as C


def qualify():
    original=(C.seller_post,C.merge_rows)
    db={};proofs=[]
    def merge(table,rows,keys,run,**kwargs):
        assert kwargs['use_dml_stats'] is True
        columns=sorted(set().union(*(r.keys() for r in rows))) if rows else []
        C.validate_merge_batch(table,rows,keys,columns,kwargs['on_duplicate_key'])
        accepted={C.merge_key(row,keys):row for row in rows}
        dest=db.setdefault(table,{})
        inserted=sum(key not in dest for key in accepted)
        dest.update(copy.deepcopy(accepted))
        return {'received':len(accepted),'inserted':inserted,'updated':len(accepted)-inserted}
    def plan(entity,**options):
        env={'BACKFILL_MODE':B.VERSION,'TENANT_BINDING_REQUIRED':'1','STRICT_PAGE_CAPS':'1',
          'BACKFILL_TARGET_PROJECT':C.PROJECT,'SINCE':'2020-01-01','UNTIL':'2020-01-01',
          'BACKFILL_GENERATION':'image-offline','BACKFILL_ORIGIN':'2019-12-31T00:00:00Z',**options}
        return B.plan(env,entity,C.PROJECT,'ozon_raw','ref',C.now_msk().date())
    def run(p,state=None,units=20):
        return F.Engine(p,'synthetic-image-run',C.now_msk().isoformat(),state or B.initial(p),unit_budget=units).run(
            lambda result,proof:proofs.append(copy.deepcopy(proof)))
    try:
        C.merge_rows=merge
        def postings(path,body):
            assert path=='/v3/posting/fbo/list'
            start=F.datetime.fromisoformat(body['filter']['since'].replace('Z','+00:00')).timestamp()*1000
            end=F.datetime.fromisoformat(body['filter']['to'].replace('Z','+00:00')).timestamp()*1000+1
            more=end-start>6*3600000
            return 200,{'postings':[{'posting_number':str(int(start)),'created_at':B.stamp(start),
                'products':[{'sku':'1','quantity':1,'price':'100'}]}],
                'has_next':more,'cursor':'synthetic-next' if more else ''}
        C.seller_post=postings
        p=plan('fbo_postings',BACKFILL_FBO_PAGE_CAP='1')
        first=run(p,units=2);assert not first['evidence']['complete']
        final=run(p,first['evidence']['state'])
        assert final['evidence']['complete'] and len(db['RAW_OZON_POSTINGS_FBO'])==4
        assert run(p,final['evidence']['state'])['evidence']['execution_requests']==0
        assert sum(x['detail']['action']=='SPLIT_PAGE_CAP' for x in proofs)==3
        def finance(path,body):
            if path=='/v1/finance/accrual/types':return 200,{'accrual_types':[]}
            assert path=='/v1/finance/accrual/by-day'
            return 200,{'last_id':'','accruals':[{'accrual_id':1,'accrued_category':'SERVICE',
               'non_item_fee':{'type_id':84,'accrued':{'amount':'3.00'}}}]}
        C.seller_post=finance
        assert run(plan('finance_accrual'))['evidence']['complete']
        assert next(iter(db['RAW_OZON_FINANCE_ACCRUAL'].values()))['operation_name']=='UNKNOWN'
        def supplies(path,body):
            if path=='/v3/supply-order/list':
                return 200,{'order_ids':[1],'last_id':''}
            if path=='/v3/supply-order/get':
                return 200,{'orders':[{'order_id':1,'supplies':[{'supply_id':101,'bundle_id':501}]}]}
            assert path=='/v1/supply-order/bundle'
            pos=int(body['last_id'] or 0);end=min(pos+2,3)
            return 200,{'items':[{'sku':i+1,'quantity':1} for i in range(pos,end)],
                       'last_id':str(end) if end<3 else '', 'has_next':end<3,'total_count':3}
        C.seller_post=supplies
        p=plan('supplies');partial=run(p,units=3)
        assert not partial['evidence']['complete'] and len(db['RAW_OZON_SUPPLY_BUNDLES'])==2
        done=run(p,partial['evidence']['state'])
        assert done['evidence']['complete'] and len(db['RAW_OZON_SUPPLY_BUNDLES'])==3
        return {'backfill_window_v1':'PASS','backfill_implementation_hash':B.implementation_hash(),
                'fbo_split_resume_replay':'PASS','finance_unknown84':'PASS','supply_bundle_resume':'PASS',
                'network':'NONE','credential_reads':0}
    finally:
        C.seller_post,C.merge_rows=original


if __name__=='__main__':print(json.dumps(qualify(),sort_keys=True))
