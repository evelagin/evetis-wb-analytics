"""Synthetic installed-image dispatch boundaries; no cloud or source authority."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch
from tools.tenancy import current_controller as C, full_controller as H
from tools.tenancy import cloud_controller as CLOUD, tenant_backfill as BF, durable_plan as D
from tools.tenancy.current_image_check import Store
from tools.tenancy.validation import parse_tenant_json

PERFORMANCE = ('ads_campaigns', 'ads_expense_daily', 'ads_sku_daily')
SELLER = ('catalog', 'stocks', 'prices', 'finance_accrual')


def rollover_fixture(target, *, expired=False):
    """Real manifest/reconstruction with an older throttled campaign snapshot."""
    from tools.tenancy import full_history as F
    from tools.tenancy.full_leaf_image_check import fixture
    m = fixture()[0]
    now = datetime(2026, 10, 15, 0, 5, tzinfo=timezone.utc)
    index = next(i for i, p in enumerate(m['programs']) if p['entity'] == 'ads_campaigns')
    old = F.render_leaf(m, index, now.date() - timedelta(days=1))
    new = F.render_leaf(m, index, now.date())
    state = BF.B.initial(old['runtime_plan']);state['sequence'] = 1
    state['progress']['rate_limit'] = dict(count=1, safe_cap=15,
        eligible_at=(now if expired else now + timedelta(hours=1)).isoformat())
    store = Store()
    for doc in (old, new):
        store.commit(m['hash'], 'CHUNK_PLAN', index,
            dict(index=index, plan=doc, shard_root=F.shard_root(m, index, doc)), now)
    store.commit(m['hash'], 'CHUNK_SUPERSEDED', index,
        dict(index=index, previous_ack=old['ack_hash'], next_ack=new['ack_hash'],
             previous_state_hash=D.digest(state), reason='PRESERVED_PARTIAL_DAY_ROLLOVER'), now)
    selected = new if target == 'ads_campaigns' else BF.make_plan('client_001', target,
        '2026-10-14', '2026-10-14', 'rollover-target', now.isoformat(), now.date())
    inventories = []
    def select(sql, params):
        if 'COUNT(*) AS n' in sql: return [{'n': 0}]
        ids = parse_tenant_json(params['plans'][1]);inventories.append(ids)
        return [dict(plan_id=old['runtime_plan']['plan_id'], backfill_sequence=1,
            evidence_json=D.encoded(dict(plan=old['runtime_plan'], state=state)))] if old['runtime_plan']['plan_id'] in ids else []
    b = H.Backend(SimpleNamespace(c=BF.target('client_001'), store=store,
        select=select, clock=lambda: now), m)
    return b, selected, now, old['runtime_plan']['plan_id'], inventories


def check():
    now = datetime(2026, 10, 15, 13, 6, tzinfo=timezone.utc)
    root, shard = 'a' * 64, 'b' * 64
    policy = {'hash': 'c' * 64}
    c = BF.target('client_001')
    def doc(entity, generation):
        return BF.make_plan('client_001', entity, '2026-10-14', '2026-10-14',
                            generation, '2026-10-15T00:00:00Z', now.date())
    for scope in ('current', 'historical'):
        for producer in PERFORMANCE:
            origin = doc(producer, 'image-retained')
            for target in PERFORMANCE + SELLER:
                for quota_status in ('ELIGIBLE', 'WAITING'):
                    chosen = origin if target == producer else doc(target, 'image-next')
                    state = BF.B.initial(origin['runtime_plan'])
                    state['sequence'] = 1
                    state['progress']['rate_limit'] = dict(count=1, safe_cap=15,
                        eligible_at=(now + timedelta(hours=1)).isoformat())
                    before_state = D.encoded(state)
                    store = Store()
                    # A retained receipt/reconciliation must survive every wait.
                    for kind in ('DISPATCH_INTENT', 'DISPATCH_RECEIPT', 'RECONCILED'):
                        store.commit(shard, kind, 1, {'plan': chosen}, now)
                    originals = deepcopy(store.records)
                    attempts = []
                    item = {'plan': origin, 'shard': shard}
                    row = dict(plan_id=origin['runtime_plan']['plan_id'], backfill_sequence=1,
                        evidence_json=D.encoded({'plan': origin['runtime_plan'], 'state': state}))
                    def select(sql, params):
                        if 'COUNT(*) AS n' in sql: return [{'n': 0}]
                        assert "entity IN ('ads_campaigns','ads_expense_daily','ads_sku_daily')" in sql
                        return [deepcopy(row)]
                    b = H.Backend(SimpleNamespace(c=deepcopy(c), store=store,
                        select=select, clock=lambda: now), {'hash': root})
                    b.current_root, b.current_execution = root, 'synthetic-image-controller'
                    b.preflight = lambda *a: None
                    b.active_runtime_execution = lambda: False
                    b.state = lambda d: deepcopy(state if d == origin else BF.B.initial(d['runtime_plan']))
                    def start(d, before, after):
                        attempts.append('lease_and_Run')
                        prep = dict(run_id='bf-synthetic-next', lease_generation=237, ack_hash=d['ack_hash'])
                        before(prep);after(dict(prep, operation='synthetic-operation'))
                    b.start = start
                    q = dict(status=quota_status, allowance=15 if quota_status == 'ELIGIBLE' else 0,
                             eligible_at=(now + timedelta(days=1)).isoformat())
                    with patch.object(CLOUD.Backend, 'quota', lambda *a: deepcopy(q)), \
                         patch.object(H, 'reconstruct_plans', lambda *a: {0: item} if scope == 'historical' else {}), \
                         patch.object(C, 'policy', lambda *a: policy), \
                         patch.object(C, 'cycles', lambda *a: {now.date().isoformat(): {}} if scope == 'current' else {}), \
                         patch.object(C, 'plans', lambda *a: ([], {producer: item})):
                        selected = {'plan': chosen, 'shard': shard}
                        for _ in range(3 if target in PERFORMANCE else 1):
                            out = C.source_tick(b, policy, now.date(), selected)
                            if target in PERFORMANCE:
                                assert out['status'] == 'WAITING' and out['source_dispatches'] == 0
                                assert not attempts and store.records == originals
                            else:
                                assert out['source_dispatches'] == 1 and attempts == ['lease_and_Run']
                    assert store.records[:3] == originals
                    assert D.encoded(state) == before_state
                    assert not any(r['kind'] in {'CURRENT_CERT', 'COMPLETE', 'FULL_COMPLETE'} for r in store.records)
    for target in PERFORMANCE + SELLER:
        for expired in (False, True):
            b, selected, at, old_id, inventories = rollover_fixture(target, expired=expired)
            originals = deepcopy(b.store.records)
            with patch.object(CLOUD.Backend, 'quota', lambda *a: dict(status='ELIGIBLE', allowance=15)), \
                 patch.object(C, 'policy', lambda *a: None):
                quota = b.quota({'plans': [selected]}, at)
            assert old_id in inventories[0]
            blocked = target in PERFORMANCE and not expired
            assert quota['status'] == ('WAITING' if blocked else 'ELIGIBLE')
            decision = D.decide_tick([], 'b' * 64, quota)
            assert decision['action'] == ('WAITING' if blocked else 'PREPARE_NEXT')
            assert b.store.records == originals
    return dict(performance_retained_snapshot_rollover_cooldown='PASS',
                performance_cooldown_before_source_authority='PASS',
                performance_campaign_expense_sku_current_and_history='PASS',
                performance_cooldown_precedes_export_exemption='PASS',
                performance_repeated_wait_no_intent_lease_Run='PASS',
                performance_retained_receipts_RECON_state_preserved='PASS',
                performance_independent_seller_sources_continue='PASS',
                performance_wait_never_claims_complete='PASS')


if __name__ == '__main__':
    import json
    print(json.dumps(check(), sort_keys=True))
