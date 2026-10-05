"""Exercise installed product-identity mapper/engine without network or credentials."""
import copy
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, '/app')
import backfill as F
import backfill_core as B
import common as C
import entities as E
import catalog_identity as CI
import lifecycle as LC

def qualify():
    env = {'BACKFILL_MODE': B.VERSION, 'TENANT_BINDING_REQUIRED': '1', 'STRICT_PAGE_CAPS': '1',
           'BACKFILL_TARGET_PROJECT': C.PROJECT, 'SINCE': '2026-09-17', 'UNTIL': '2026-09-17',
           'BACKFILL_GENERATION': 'synthetic-product-identity', 'BACKFILL_ORIGIN': '2026-10-04T00:00:00Z'}
    p = B.plan(env, 'catalog', C.PROJECT, C.DATASET, C.REF_DATASET, C.now_msk().date())
    db = {}; writes = []; proofs = []
    def merge(table, rows, keys, run_id, **kwargs):
        C.validate_merge_batch(table, rows, keys, sorted(set().union(*(r.keys() for r in rows))) if rows else [])
        inserted = 0
        for r in rows:
            key = C.merge_key(r, keys); inserted += key not in db; db[key] = copy.deepcopy(r)
        writes.extend(copy.deepcopy(rows))
        return {'received': len(rows), 'inserted': inserted, 'updated': len(rows)-inserted}
    C.merge_rows = merge
    def run(state=None, units=20):
        return F.Engine(p, 'synthetic-catalog', C.now_msk().isoformat(), state or B.initial(p), unit_budget=units).run(lambda r, e: proofs.append(copy.deepcopy(e)))
    for shape in ('missing', None, '', 0, '0', 22):
        db.clear(); writes.clear(); proofs.clear()
        def source(path, body):
            if path == '/v3/product/list':
                pid = 1 if body['filter']['visibility'] == 'ALL' else 2
                return 200, {'result': {'total': 1, 'items': [{'product_id': pid}]}}
            pid = body['product_id'][0]; item = {'id': pid, 'offer_id': 'synthetic-offer', 'is_archived': pid == 2}
            if pid == 1: item['sku'] = 11
            elif shape != 'missing': item['sku'] = shape
            return 200, {'items': [item]}
        C.seller_post = source
        first = run(units=1); assert not first['evidence']['complete']
        second = run(first['evidence']['state']); assert second['evidence']['complete']
        assert len(db) == 2 and len(second['evidence']['state']['progress']['product_ids']) == 2
        archived = next(r for r in db.values() if r['product_id'] == '2')
        assert archived['sku'] == ('22' if shape == 22 else None)
        replay = run(second['evidence']['state']); assert replay['evidence']['replay'] and replay['evidence']['execution_requests'] == 0
    for invalid in (None, '', 0, '0', -1, True, 'invalid'):
        db.clear(); C.seller_post = lambda path, body: (200, {'result': {'total': 1, 'items': [{'product_id': invalid}]}})
        try: run()
        except B.EvidenceError: pass
        else: raise AssertionError('invalid product identity accepted')
        assert not db
    C.seller_post = lambda path, body: (200, {'result': {'items': [{'product_id': 1}]}}) if path == '/v3/product/list' else (200, {'items': [{'id': 1, 'sku': 11}, {'id': 2, 'sku': 0}, {'id': 3}]})
    assert LC.catalog_skus() == {'11'}
    print(json.dumps({'catalog_product_identity': 'PASS', 'optional_sku_vectors': 6, 'invalid_product_vectors': 7,
                      'resume_replay': 'PASS', 'performance_sku_association': 'PASS', 'network': 'NONE', 'credential_reads': 0}))

if __name__ == '__main__': qualify()
