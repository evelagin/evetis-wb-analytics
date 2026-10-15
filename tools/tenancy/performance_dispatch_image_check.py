"""Synthetic installed-image dispatch boundaries; no cloud or source authority."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch
from tools.tenancy import current_controller as C, full_controller as H
from tools.tenancy import cloud_controller as CLOUD, tenant_backfill as BF, durable_plan as D
from tools.tenancy.current_image_check import Store

PERFORMANCE = ('ads_campaigns', 'ads_expense_daily', 'ads_sku_daily')
SELLER = ('catalog', 'stocks', 'prices', 'finance_accrual')


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
    return dict(performance_cooldown_before_source_authority='PASS',
                performance_campaign_expense_sku_current_and_history='PASS',
                performance_cooldown_precedes_export_exemption='PASS',
                performance_repeated_wait_no_intent_lease_Run='PASS',
                performance_retained_receipts_RECON_state_preserved='PASS',
                performance_independent_seller_sources_continue='PASS',
                performance_wait_never_claims_complete='PASS')


if __name__ == '__main__':
    import json
    print(json.dumps(check(), sort_keys=True))
