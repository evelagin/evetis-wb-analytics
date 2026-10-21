"""Network-free in-image proof of the emergency controller changes (2026-10-20).

  * one tenant_locks listing per claimed wake (durable history snapshot, own commits visible);
  * one STOP acceptance set for gate, monitor, FBO diagnostic and closure recovery;
  * a repeat of the same unclosed control claim blocks without a new STOP record,
    also when that claim belongs to the replaced release (typed in acquire);
  * the wake budget declines a leaf, reconcile, completion or dispatch before it
    starts (never mid-dispatch) and a handoff ends the wake;
  * the shared Performance cooldown is read once per wake (leaf loop), its readers are one code path;
  * the v3 controller-closure certificate shape (intermediate page) is closed.
Every patched seam is restored before returning.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import importlib

NOW = datetime(2026, 10, 20, 8, tzinfo=timezone.utc)
MODULES = ('full_leaf_recovery', 'full_cooldown_recovery', 'full_policy_bootstrap_recovery', 'full_pretransport_recovery',
           'full_retained_pretransport_recovery', 'full_fbo_zero_effect_recovery', 'full_first_receipt_recovery',
           'full_controller_closure_recovery', 'full_pre_authority_gate_recovery', 'full_fbo_diagnostic_closure',
           'current_recon_recovery', 'f507_recovery', 'full_inventory_gate_recovery')


def snapshot_check():
    from tools.tenancy import durable_plan as D, tenant_backfill as BF
    c = BF.target('client_001'); objects = {}; rows = []; lists = [0]

    class Meta:
        def list_tables(self, ds):
            lists[0] += 1
            return [(n, v[0], NOW) for n, v in objects.items()]

        def get_table(self, ds, n):
            return objects.get(n)

        def create_marker(self, ds, n, labels, description):
            if n in objects:
                return False
            objects[n] = (labels, description); return True

    def select(c_, sql, params):
        return [{'evidence_json': r['evidence_json']} for r in rows if r['plan_hash'] == params['root'][1]
                and ('record' not in params or r['backfill_id'] == params['record'][1])]

    store = D.DurableRecords(c, Meta(), select, rows.append)
    root, other = '1' * 64, '2' * 64
    store.commit(root, 'WAITING', 0, {'n': 1}, NOW)
    lists[0] = 0
    store.begin_snapshot()
    store.commit(root, 'WAITING', 0, {'n': 2}, NOW); store.commit(other, 'WAITING', 0, {'n': 3}, NOW)
    assert len(store.history(root)) == 2 and len(store.history(other)) == 1 and lists[0] == 1
    assert D.DurableRecords(c, Meta(), select, rows.append).history(root) == store.history(root)
    return 'PASS'


def acceptance_check():
    from tools.tenancy import stop_acceptance as SA
    saved, records, calls = {}, [], []
    try:
        for n, name in enumerate(MODULES):
            module = importlib.import_module('tools.tenancy.' + name)
            saved[module] = module.load
            h = ('%02d' % n) * 32
            if name == 'full_controller_closure_recovery':
                module.load = lambda b, m, r, h=h: calls.append(h) or [{'stop_hashes': [h]}]
            else:
                module.load = lambda b, m, r, h=h: calls.append(h) or [{'stop_hash': h}]
            records.append({'kind': getattr(module, 'KIND', 'NONE_' + name)})
        scope = SA.Scope()
        b = SimpleNamespace(stop_acceptance_scope=scope)
        expected = {('%02d' % n) * 32 for n in range(len(MODULES))}
        assert SA.accepted_hashes(b, {}, records) == expected
        n = len(calls)
        assert SA.accepted_hashes(b, {}, list(records)) == expected and len(calls) == n      # same slice: once per wake
        other = SimpleNamespace(stop_acceptance_scope=scope)                                # the monitor's backend object
        assert SA.accepted_hashes(other, {}, list(records)) == expected and len(calls) == n
        assert SA.accepted_hashes(b, {}, records, fresh=True) == expected and len(calls) > n  # before a source effect
        n = len(calls)
        assert SA.accepted_hashes(SimpleNamespace(stop_acceptance_scope=SA.Scope()), {}, records) == expected and len(calls) > n
    finally:
        for module, load in saved.items():
            module.load = load
    return 'PASS'


def repeat_stop_check():
    from tools.tenancy import durable_plan as D, controller_cadence as CC, controller_diagnostics as CD, cloud_controller as C
    diag = {'version': 1, 'stage': 'MANIFEST_ROOT', 'category': 'EVIDENCE'}
    done = NOW - timedelta(hours=1)
    try:
        with CD.gate('MANIFEST_ROOT'):
            raise CC.UnclosedTerminalHolder({'generation': 7, 'hash': 'c' * 64, 'created_at': (done - timedelta(minutes=29)).isoformat()},
                                            {'completionTime': done.isoformat()})
    except CD.GateFailure as failure:
        typed = failure
    assert CD.safe(typed) == diag
    payload = {'reason': 'CONTROLLER_GATE_OR_EVIDENCE_FAILURE', 'controller_execution': 'x', 'diagnostic': dict(diag)}
    record = {'version': D.VERSION, 'root_hash': '1' * 64, 'kind': 'STOPPED', 'sequence': 0, 'payload': payload}

    def backend(created):
        return SimpleNamespace(c={'datasets': {'tenant_locks': 'tenant_locks'}},
                               tables=SimpleNamespace(list_tables=lambda ds: [('BFR_' + D.digest(record), {}, created)]),
                               store=SimpleNamespace(history=lambda root: [record]))
    assert C.known_unclosed_incident(backend(done + timedelta(seconds=40)), '1' * 64, typed, payload) == D.digest(record)
    assert C.known_unclosed_incident(backend(done - timedelta(seconds=1)), '1' * 64, typed, payload) is None
    assert C.known_unclosed_incident(backend(done + timedelta(seconds=40)), '1' * 64, CD.GateFailure('MANIFEST_ROOT', 'EVIDENCE'), payload) is None
    return 'PASS'


def budget_check():
    from tools.tenancy import wake_budget as WB, controller_cadence as CC
    t = [0.0]
    b = WB.Budget(clock=lambda: t[0])
    assert b.allows(WB.DISPATCH_SECONDS) and b.allows(WB.LEAF_SECONDS) and b.report_allowed()
    t[0] = WB.PLATFORM_SECONDS - WB.STARTUP_SECONDS - WB.CLOSE_RESERVE_SECONDS - WB.DISPATCH_SECONDS + 1
    assert not b.allows(WB.DISPATCH_SECONDS) and b.remaining() > WB.CLOSE_RESERVE_SECONDS
    for step in ('LEAF', 'RECONCILE', 'COMPLETE', 'DISPATCH'):
        h = WB.handoff(step, index=465)
        assert h['status'] in CC.STATUSES and h['source_dispatches'] == 0 and h['basis'] == 'WAKE_BUDGET_' + step and WB.exhausted(h)
    assert not WB.exhausted({'status': 'WAITING', 'basis': 'KNOWN_READ_RETRY_COOLDOWN'})
    assert WB.CLOSE_RESERVE_SECONDS >= 6 * 60 and WB.of(SimpleNamespace()) is None
    return 'PASS'


def cooldown_check():
    from tools.tenancy import full_controller as H, current_controller as CUR
    assert H.PERFORMANCE == frozenset({'ads_campaigns', 'ads_expense_daily', 'ads_sku_daily'})
    assert callable(H.Backend.performance_class) and callable(H.Backend.performance_cooldown) and callable(CUR.current_cooldowns)
    return 'PASS'


def closure_v3_check():
    from tools.tenancy import full_controller_closure_recovery as CR
    assert (3, CR.SEQUENCE_TYPE) in CR.VERSIONS and 'intermediate_page_not_complete' in CR.SEQUENCE_PREDICATES
    assert not {'current_catalog_pass', 'accepted_catalog_lineage_pass', 'last_source_reconciled'} & CR.SEQUENCE_PREDICATES
    try:
        CR.validate_sequence({}, None, {'programs': [{}]}, [], {'index': 0, 'shard': 'x'})
    except CR.BF.B.EvidenceError:
        pass
    else:
        raise AssertionError('open sequence scope accepted')
    return 'PASS'


def check():
    for name, fn in (('snapshot', snapshot_check), ('acceptance', acceptance_check), ('repeat_stop', repeat_stop_check),
                     ('budget', budget_check), ('cooldown', cooldown_check), ('closure_v3', closure_v3_check)):
        assert fn() == 'PASS', name
    return {'controller_wake_budget_adapter': 'PASS'}


if __name__ == '__main__':
    import json
    print(json.dumps(check(), sort_keys=True))
