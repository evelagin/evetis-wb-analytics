"""Network-free in-image proof of advertising in the daily CURRENT contour (Heldi, 2026-11-09).

  * release: current_ads_version requires current_independence and its adapter; new record kinds and
    source files are packaged;
  * selection: without the flag CURRENT never elects ads (S4 v1 unchanged); with it, ads entities are
    elected after the four Seller entities, SKU only after today's campaign snapshot certificate;
  * settle: a FAILED ads execution with exact Cloud Run provenance (Execution/Operation v2 shapes) is a
    local outcome: lease released (LD_, FAILED_ADS_LOCAL), FAILED checkpoint, RECONCILED{failed}; known
    read -> bounded retry, report INTENT of the failed run -> ADS_REPORT_AMBIGUOUS (never re-posted),
    anything else -> ADS_SOURCE_FAILED; provenance/lease mismatch, a non-ads entity, or ads without the
    flag stay a global STOP;
  * tick: a halted ads entity gets CURRENT_PARTIAL and is never dispatched again; a known read waits
    for its cooldown, is resumed from durable state at most MAX_ATTEMPTS times, never with a lost reply
    repeated;
  * fence: an in-flight report INTENT is the one async scope, a halted plan holds none, old cycles are
    not read; without the flag an INTENT is still fail-closed;
  * catch-up: closed chained owner authority per entity after the FULL ads cutoff; one plan per day;
    accepted data present -> PARTIAL without source call (halt); an empty source day -> UNKNOWN and the
    chain continues; DQ and ads-local failures halt; a live CURRENT day halts; open day drained first,
    new day only after today's CURRENT and the finance catch-up; runtime change halts new days;
  * status: COMPLETE / PARTIAL / UNKNOWN / STALE per entity, a day without data is never zero.
Every patched seam is restored before returning.
"""
from contextlib import contextmanager, ExitStack
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json
from types import SimpleNamespace
from tools.tenancy import current_ads as ADS, ads_catchup as AC, current_controller as CUR, current_policy as P
from tools.tenancy import finance_catchup as FC, current_contract as K, durable_plan as D, tenant_backfill as BF
from pipelines.ozon.runtime import full_resume as FR
from tools.tenancy.validation import parse_tenant_json

NOW = datetime(2026, 11, 8, 10, 30, tzinfo=timezone.utc)
TODAY = date(2026, 11, 8)
CREATED = '2026-11-08T10:00:00+00:00'
DAYS = [(date(2026, 10, 8) + timedelta(days=i)).isoformat() for i in range(24)]   # 08.10 .. 31.10
RUN = 'bf-00000000-0000-0000-0000-000000000002'


@contextmanager
def patched(obj, name, value):
    saved = getattr(obj, name); setattr(obj, name, value)
    try: yield
    finally: setattr(obj, name, saved)


class Store:
    def __init__(self): self.records = []; self.reads = 0
    def history(self, root, **k):
        self.reads += 1
        return [deepcopy(r) for r in self.records if r['root_hash'] == root]
    def commit(self, root, kind, seq, payload, now):
        r = dict(version=D.VERSION, root_hash=root, kind=kind, sequence=seq, payload=deepcopy(payload))
        if r not in self.records: self.records.append(r)
        return D.digest(r)


def rejects(fn):
    try: fn()
    except (ValueError, BF.B.EvidenceError): return True
    return False


def fixture(**changes):
    c = deepcopy(BF.target('client_001')); m = FR.manifest(); env = c['orchestration']['job']['env']
    p = P.sealed(dict(version=1, type=K.TYPE, tenant=c['tenant_id'], project=c['project_id'], historical_root=m['hash'],
        owner_decision_sha256='1' * 64, created_at='2026-10-14T05:00:00+00:00', first_day='2026-10-14', entities=list(P.ORDER),
        snapshot_sla_seconds=86400, fbo_sla_seconds=86400, controller_source=env['CONTROLLER_SOURCE_SHA'],
        controller_image=c['orchestration']['job']['image'], runtime_image=c['marketplaces']['ozon']['runtime_image'],
        runtime_implementation_hash=BF.B.implementation_hash(), fbo_contract_hash=None, source_concurrency=1, finance_finality='PROVISIONAL'))
    kw = dict(serial=1, previous=None, owner_ack_sha256='2' * 64, created_at=CREATED, entity='ads_expense_daily', days=DAYS)
    kw.update(changes)
    a = AC.authority(c, m, p, **kw)
    return c, m, p, a


def current_item(p, day, entity):
    doc = P.document(p, day, entity)
    return {'day': day.isoformat(), 'policy_hash': p['hash'], 'plan': doc, 'shard': P.shard(p, day, doc)}


# ---------------------------------------------------------------- release
def release_check():
    from tools.tenancy import orchestration_contract as O
    from tools.tenancy.validation import parse_tenant_json
    c = BF.target('client_001'); env = c['orchestration']['job']['env']
    rel = parse_tenant_json((BF.REPO / 'infra/tenant/releases/backfill/2253a311871fb4a3b9e4644d5c5de9f18d37a407.json').read_text())
    settings = {'release': rel['source_sha'], 'root_hash': env['BACKFILL_ROOT_HASH'], 'scheduler_state': 'ENABLED'}
    c = O.historical_contract(c, rel)
    ok = dict(rel, current_ads_version=1, verification=dict(rel['verification'], current_ads_adapter='PASS'))
    assert O.block(c, settings, BF.REPO, release=ok)['job']['env']['CURRENT_DATA_MODE'] == K.TYPE
    for bad in (dict(ok, current_ads_version=2), dict(ok, current_ads_version=True),
                dict(ok, verification=rel['verification']),
                {k: v for k, v in ok.items() if k != 'current_independence_version'}):
        assert rejects(lambda: O.block(c, settings, BF.REPO, release=bad))
    assert {'current_ads.py', 'ads_catchup.py', 'current_ads_image_check.py'} <= set(O.SOURCE_FILES)
    assert {'ADSCATCHUP_PLAN', 'ADSCATCHUP_CERT', 'ADSCATCHUP_PARTIAL'} <= D.KINDS
    assert set(ADS.ENTITIES) <= set(P.ORDER) and not set(ADS.ENTITIES) & set(CUR.INDEPENDENT_ENTITIES)
    return 'PASS'


# ---------------------------------------------------------------- settle (Cloud Run v2 contract)
def settle_backend(*, entity='ads_expense_daily', message="EvidenceError('SOURCE_HTTP_503: /api/client/statistics/expense')",
                   report=None, exec_changes=None, lease_owner=RUN, rows=None, qualified=True):
    c, m, p, _ = fixture(); day = TODAY; item = current_item(p, day, entity); doc = item['plan']; rp = doc['runtime_plan']
    base, jobs = BF.resources(c); job = next(n for n, j in jobs.items() if rp['entity'] in j['entities'])
    name = base + '/jobs/' + job + '/executions/' + job + '-x7k2p'
    env = {'INGESTION_RUN_ID': RUN, 'BACKFILL_TARGET_PROJECT': rp['project'], 'ENTITIES': rp['entity'], 'SINCE': rp['from'],
           'UNTIL': rp['to'], 'BACKFILL_MODE': BF.B.VERSION, 'BACKFILL_GENERATION': rp['generation'], 'BACKFILL_ORIGIN': rp['origin'],
           'TENANT_BINDING_REQUIRED': '1', 'STRICT_PAGE_CAPS': '1', 'BACKFILL_MAX_REQUESTS': str(doc['max_requests']),
           'BACKFILL_MAX_UNITS': str(doc['max_units'])}
    execution = {'name': name, 'taskCount': 1, 'failedCount': 1, 'succeededCount': 0, 'completionTime': '2026-11-08T10:21:07.123Z',
                 'template': {'serviceAccount': f"{c['marketplaces']['ozon']['service_accounts']['runtime']}@{c['project_id']}.iam.gserviceaccount.com",
                              'containers': [{'image': doc['image'], 'env': [{'name': k, 'value': v} for k, v in env.items()]}]}}
    for k, v in (exec_changes or {}).items():
        if k == 'image': execution['template']['containers'][0]['image'] = v
        elif k == 'env': execution['template']['containers'][0]['env'] = [{'name': a, 'value': b} for a, b in v.items()]
        else: execution[k] = v
    receipt = {'run_id': RUN, 'lease_generation': 171, 'ack_hash': doc['ack_hash'], 'operation': base + '/operations/op-9f2'}
    operation = {'name': receipt['operation'], 'done': True, 'error': {'code': 2, 'message': 'Task failed'}, 'metadata': {'name': name}}
    cid = BF.B.digest(['BOUNDED_PILOT_EXCLUSIVE', c['project_id']])[:16]
    objects = {('tenant_locks', BF.CK.lease_name(cid, 171)): ({'owner': lease_owner}, D.encoded({'ack_hash': doc['ack_hash'], 'owner': lease_owner}))}
    appended, calls = [], []
    def create_marker(ds, n, labels, desc):
        if (ds, n) in objects: return False
        objects[(ds, n)] = (labels, desc); return True
    tables = SimpleNamespace(get_table=lambda ds, n: objects.get((ds, n)), append=lambda ds, t, rows_: appended.append((ds, t, rows_)), create_marker=create_marker)
    state = {'plan_id': rp['plan_id'], 'sequence': 2, 'complete': False, 'rows': 0, 'requests': 2, 'pages': 0,
             'progress': {'next_day': rp['from'], **({'pending': ['111'], 'report': report} if report else {})}}
    b = object.__new__(CUR.Backend)
    b.__dict__.update(c=c, manifest=m, clock=lambda: NOW, store=Store(), journal='mpa-t-client-001.tenant_ops.INGESTION_RUNS',
                      full_tables=tables, current_policy=p, current_day=day, current_root=P.cycle_root(p, day),
                      _current_ads_qualified=qualified)
    b.authorize_full_leaf = lambda d: calls.append('authorize') or True
    b.state = lambda d: state
    def request(method, url):
        calls.append(method + ' ' + url.rsplit('/', 1)[-1])
        if url.endswith('/' + receipt['operation']): return operation
        if url.endswith('/' + name): return execution
        raise AssertionError('unexpected request ' + url)
    b.request = request
    b.select = lambda sql, params: calls.append('journal') or ([{'error_message': message}] if rows is None else rows)
    return b, doc, receipt, operation, objects, appended, calls, cid


def settle_check():
    b, doc, receipt, op, objects, appended, calls, cid = settle_backend()
    out = b.reconcile({'plan': doc, 'receipt': receipt})
    assert out['failed'] is True and out['retry_category'] == ADS.RETRY and out['source_complete'] is False
    assert K.at(out['eligible_at']) == NOW + timedelta(seconds=ADS.RETRY_SECONDS)
    ld = objects[('tenant_locks', f'LD_{cid}_0171')]
    assert ld[0] == {'owner': RUN} and parse_tenant_json(ld[1])['terminal'] == 'FAILED_ADS_LOCAL'
    assert appended[0][1] == 'BACKFILL_CHECKPOINTS' and appended[0][2][0]['status'] == 'FAILED' and appended[0][2][0]['error_code'] == ADS.RETRY
    assert not any('statistics' in x or 'campaign' in x for x in calls)                       # never a source call
    again = b.reconcile({'plan': doc, 'receipt': receipt})                                    # idempotent release
    assert again['retry_category'] == ADS.RETRY
    # report INTENT of the failed run: the POST outcome is unknown -> never posted again.
    b, doc, receipt, *_ = settle_backend(entity='ads_sku_daily', report={'phase': 'INTENT', 'batch': ['111'], 'execution': RUN})
    assert b.reconcile({'plan': doc, 'receipt': receipt})['retry_category'] == 'ADS_REPORT_AMBIGUOUS'
    for message, rows in (("EvidenceError('SKU report empty/outside requested day')", None),
                          ("EvidenceError('SOURCE_REPORT_FAILED')", None), (None, []),
                          (None, [{'error_message': 'a'}, {'error_message': 'b'}])):
        b, doc, receipt, *_ = settle_backend(entity='ads_sku_daily', message=message, rows=rows, report={'phase': 'POLL', 'batch': ['111'], 'uuid': 'u-1'})
        out = b.reconcile({'plan': doc, 'receipt': receipt})
        assert out['retry_category'] == 'ADS_SOURCE_FAILED' and 'eligible_at' not in out
    # Global STOP stays for: exact provenance/lease mismatch, non-ads entity, ads without the flag.
    for bad in ({'image': 'europe-west1-docker.pkg.dev/mpa-platform/mpa-runtime/ozon@sha256:' + 'e' * 64},
                {'failedCount': 0, 'succeededCount': 1}, {'taskCount': 2}, {'completionTime': None}, {'cancelledCount': 1},
                {'env': {'INGESTION_RUN_ID': 'bf-other'}}):
        b, doc, receipt, *_ = settle_backend(exec_changes=bad)
        assert rejects(lambda: b.reconcile({'plan': doc, 'receipt': receipt})), bad
    b, doc, receipt, *_ = settle_backend(lease_owner='bf-foreign')
    assert rejects(lambda: b.reconcile({'plan': doc, 'receipt': receipt}))
    b, doc, receipt, *_ = settle_backend(qualified=False)
    assert rejects(lambda: b.reconcile({'plan': doc, 'receipt': receipt}))
    b, doc, receipt, _, objects, *_ = settle_backend(entity='stocks')
    assert rejects(lambda: b.reconcile({'plan': doc, 'receipt': receipt})) and not any(n.startswith('LD_') for _, n in objects)
    return 'PASS'


# ---------------------------------------------------------------- gate + CURRENT tick
def gate_check():
    def rec(seq, payload): return {'kind': 'RECONCILED', 'sequence': seq, 'payload': payload}
    retry = lambda at: {'failed': True, 'retry_category': ADS.RETRY, 'eligible_at': at}
    assert ADS.failure_gate([], NOW) is None and ADS.failure_gate([rec(1, {'source_complete': False})], NOW) is None
    later = (NOW + timedelta(minutes=5)).isoformat(); earlier = (NOW - timedelta(minutes=5)).isoformat()
    assert ADS.failure_gate([rec(1, retry(later))], NOW) == ('WAIT', later)
    assert ADS.failure_gate([rec(1, retry(earlier))], NOW) is None
    assert ADS.failure_gate([rec(1, retry(earlier)), rec(2, retry(earlier)), rec(3, retry(earlier))], NOW) == ('HALT', 'ADS_RETRY_EXHAUSTED')
    for reason in ADS.REASONS:
        assert ADS.failure_gate([rec(1, {'failed': True, 'retry_category': reason})], NOW) == ('HALT', reason)
    assert rejects(lambda: ADS.failure_gate([rec(1, {'failed': True, 'retry_category': 'QUOTA'})], NOW))
    assert ADS.failure_gate([rec(1, retry(earlier)), rec(2, {'source_complete': True})], NOW) is None   # a later success wins
    return 'PASS'


def tick_backend(p, day, entity, *, reconcile_payload=None, start=None, clock=None):
    item = current_item(p, day, entity); doc = item['plan']; store = Store(); calls = []
    now = clock or [NOW]
    def default_start(d, before, after):
        before(dict(run_id=RUN, lease_generation=7, ack_hash=d['ack_hash'], job='synthetic', overrides={}))
        calls.append('source_dispatch')
        after(dict(run_id=RUN, lease_generation=7, ack_hash=d['ack_hash'], operation='op'))
    state = {'plan_id': doc['runtime_plan']['plan_id'], 'sequence': 1, 'complete': False, 'progress': {'next_day': doc['runtime_plan']['from']}}
    b = SimpleNamespace(c=BF.target('client_001'), clock=lambda: now[0], current_root=P.cycle_root(p, day), current_execution='x', store=store,
        preflight=lambda leaf: calls.append('preflight'), quota=lambda *x: {'status': 'ELIGIBLE', 'allowance': 0},
        active_runtime_execution=lambda: False, state=lambda d: state, start=start or default_start,
        reconcile=lambda payload: calls.append('reconcile') or deepcopy(reconcile_payload))
    return b, item, store, calls, now


def tick_check():
    c, m, p, _ = fixture(); day = TODAY
    def committed(store, item, seq):
        store.commit(item['shard'], 'DISPATCH_INTENT', seq, {'plan': item['plan']}, NOW)
        store.commit(item['shard'], 'DISPATCH_RECEIPT', seq, {'plan': item['plan'], 'receipt': {'run_id': RUN, 'lease_generation': 7, 'operation': 'op'}}, NOW)
    # Terminal local outcome: CURRENT_PARTIAL with the reason, no dispatch now or later.
    for reason in sorted(ADS.REASONS - {'ADS_RETRY_EXHAUSTED'}):
        b, item, store, calls, _ = tick_backend(p, day, 'ads_expense_daily', reconcile_payload={'failed': True, 'retry_category': reason, 'source_complete': False})
        committed(store, item, 1)
        got = CUR.source_tick(b, p, day, item)
        assert got['basis'] == 'ADS_LOCAL_HALT' and got['reason'] == reason and got['source_dispatches'] == 0
        part = [r for r in store.records if r['kind'] == 'CURRENT_PARTIAL']
        assert len(part) == 1 and part[0]['root_hash'] == P.cycle_root(p, day) and part[0]['payload']['reason'] == reason
        assert part[0]['sequence'] == P.ORDER.index('ads_expense_daily')
        got = CUR.source_tick(b, p, day, item)                                                # next wake: never dispatched
        assert got['basis'] == 'ADS_LOCAL_HALT' and 'source_dispatch' not in calls
    # Known read: wait for the cooldown, resume the same plan, at most MAX_ATTEMPTS failures.
    retry = {'failed': True, 'retry_category': ADS.RETRY, 'source_complete': False, 'eligible_at': (NOW + timedelta(seconds=600)).isoformat()}
    b, item, store, calls, now = tick_backend(p, day, 'ads_expense_daily', reconcile_payload=retry)
    committed(store, item, 1)
    got = CUR.source_tick(b, p, day, item)
    assert got['basis'] == 'ADS_READ_RETRY_COOLDOWN' and 'source_dispatch' not in calls
    now[0] = NOW + timedelta(seconds=601)
    got = CUR.source_tick(b, p, day, item)
    assert got['status'] == 'DISPATCHED' and calls.count('source_dispatch') == 1
    assert [r['sequence'] for r in store.records if r['kind'] == 'DISPATCH_INTENT'] == [1, 2]
    for seq in (2, 3):
        retry['eligible_at'] = (now[0] + timedelta(seconds=600)).isoformat(); b.reconcile = lambda payload: deepcopy(retry)
        got = CUR.source_tick(b, p, day, item)
        if seq == 2:
            now[0] += timedelta(seconds=601); assert CUR.source_tick(b, p, day, item)['status'] == 'DISPATCHED'
    assert got['basis'] == 'ADS_LOCAL_HALT' and got['reason'] == 'ADS_RETRY_EXHAUSTED' and calls.count('source_dispatch') == 2
    # A lost dispatch reply is never repeated (unchanged CURRENT protocol).
    attempts = []
    def lost(d, before, after):
        before(dict(run_id=RUN, lease_generation=7, ack_hash=d['ack_hash'], job='synthetic', overrides={})); attempts.append(1); raise OSError('lost')
    b, item, store, calls, _ = tick_backend(p, day, 'ads_expense_daily', start=lost)
    for _ in range(2): assert rejects(lambda: CUR.source_tick(b, p, day, item))
    assert len(attempts) == 1
    # Seller entities: a failed reconcile payload never reaches them; their FAILED stays a STOP (settle_check).
    return 'PASS'


# ---------------------------------------------------------------- fence
def fence_check():
    c, m, p, _ = fixture()
    def run(*, qualified, intent=None, opened=True, halted=False, cycle=TODAY, live=()):
        item = current_item(p, cycle, 'ads_sku_daily'); store = Store()
        if opened is not None and intent:
            store.commit(item['shard'], 'DISPATCH_INTENT', 1, {'plan': item['plan']}, NOW)
            if not opened: store.commit(item['shard'], 'RECONCILED', 1, {'failed': True, 'retry_category': 'ADS_REPORT_AMBIGUOUS'} if halted else {}, NOW)
        state = {'progress': {'report': intent, 'rate_limit': {'eligible_at': (NOW + timedelta(minutes=30)).isoformat()}}}
        b = SimpleNamespace(clock=lambda: NOW, store=store, state=lambda d: state, _current_ads_qualified=qualified)
        with patched(CUR, 'policy', lambda bb: p), patched(CUR, 'cycles', lambda bb, pp: {cycle.isoformat(): {}}), \
             patched(CUR, 'plans', lambda bb, pp, d: ([], {'ads_sku_daily': item})), patched(AC, 'live_items', lambda bb, pp: list(live)):
            return CUR._current_ads(b), item
    (reports, cool), item = run(qualified=True, intent={'phase': 'INTENT', 'batch': ['1']})
    assert reports == [item['plan']['runtime_plan']['plan_id']] and len(cool) == 1                # in flight: the one scope
    assert rejects(lambda: run(qualified=False, intent={'phase': 'INTENT', 'batch': ['1']}))        # S4 behaviour unchanged
    assert rejects(lambda: run(qualified=True, intent={'phase': 'INTENT', 'batch': ['1']}, opened=False))   # unsettled ambiguity
    (reports, cool), _ = run(qualified=True, intent={'phase': 'INTENT', 'batch': ['1']}, opened=False, halted=True)
    assert reports == [] and cool == []                                                            # halted: no scope, no read
    (reports, _), _ = run(qualified=True, intent={'phase': 'POLL', 'batch': ['1'], 'uuid': 'u'}, opened=False, cycle=TODAY - timedelta(days=5))
    assert reports == []                                                                           # old cycle: not read
    (reports, _), _ = run(qualified=False, intent={'phase': 'POLL', 'batch': ['1'], 'uuid': 'u'}, opened=False, cycle=TODAY - timedelta(days=5))
    assert len(reports) == 1                                                                       # unflagged: unchanged
    return 'PASS'


# ---------------------------------------------------------------- selection / order
def order_check():
    from tools.tenancy import controller_cadence as CC, recent_priority as RP, fbo_diagnostic_owner as DIAG
    c, m, p, a = fixture(); day = NOW.astimezone(K.MSK).date()
    def item(e): return {'day': day.isoformat(), 'policy_hash': p['hash'], 'plan': {'ack_hash': e, 'runtime_plan': {'entity': e, 'from': '2026-11-19'}}, 'shard': 's-' + e}
    def run(*, qualified, certified=(), partial=(), fin=None, ads=None, results=None):
        calls = []; items = {e: item(e) for e in P.ORDER if e != 'fbo_postings'}
        records = [{'kind': 'CURRENT_CERT', 'payload': {'fact': {'entity': e}}} for e in certified]
        records += [{'kind': 'CURRENT_PARTIAL', 'payload': {'entity': e, 'policy_hash': p['hash'], 'ack_hash': e, 'state_hash': D.digest({'progress': {}}), 'status': 'PARTIAL', 'reason': r}} for e, r in partial]
        base = SimpleNamespace(clock=lambda: NOW, store=SimpleNamespace(history=lambda s, **k: [], commit=lambda *x: 'h'), state=lambda d: {'progress': {}}, _current_ads_qualified=qualified)
        none = {'authorities': [], 'pending': [], 'next': None, 'halted': None}
        def tick(b, pp, d, it):
            e = it['plan']['runtime_plan']['entity']; calls.append(e)
            return (results or {}).get(e, {'status': 'WAITING', 'source_dispatches': 0})
        with ExitStack() as s:
            for obj, n, v in ((CUR, 'policy', lambda b: p), (CC, 'ensure', lambda b: None), (RP, 'pending_receipts', lambda *x: []),
                              (CUR, 'cycles', lambda b, pp: {day.isoformat(): {}}), (CUR, 'plans', lambda b, pp, d: (records, items)),
                              (CUR, 'validate_certificate', lambda *x: x[3]), (CUR, 'Backend', lambda *x: None), (CUR, 'source_tick', tick),
                              (DIAG, 'queued', lambda b: None), (FC, 'scan', lambda *x, **k: fin or none), (FC, 'Backend', lambda *x: None),
                              (FC, 'tick', lambda b, aa, d, it: calls.append('fin:' + d.isoformat()) or {'status': 'DISPATCHED', 'source_dispatches': 1}),
                              (AC, 'scan', lambda *x, **k: ads or none), (AC, 'Backend', lambda *x: None),
                              (AC, 'tick', lambda b, aa, d, it: calls.append('ads:' + d.isoformat()) or {'status': 'DISPATCHED', 'source_dispatches': 1})):
                s.enter_context(patched(obj, n, v))
            result = CUR.wake(base, [], {}, {}, set())
        return calls, result
    seller = ['catalog', 'stocks', 'prices', 'finance_accrual']
    calls, _ = run(qualified=False, certified=['catalog'])
    assert calls == ['stocks', 'prices', 'finance_accrual']                                        # S4 v1 unchanged: no ads
    calls, _ = run(qualified=True, certified=['catalog'])
    assert calls == ['stocks', 'prices', 'finance_accrual', 'ads_campaigns', 'ads_expense_daily']  # ads after Seller; SKU waits for campaigns
    calls, _ = run(qualified=True, certified=['catalog', 'ads_campaigns'])
    assert calls[-1] == 'ads_sku_daily' and calls.index('ads_campaigns' if 'ads_campaigns' in calls else 'ads_expense_daily') > calls.index('finance_accrual')
    calls, _ = run(qualified=True, certified=['catalog'], partial=[('ads_expense_daily', 'ADS_SOURCE_FAILED')])
    assert 'ads_expense_daily' not in calls and 'finance_accrual' in calls                          # halted ads: Seller unaffected
    assert rejects(lambda: run(qualified=True, certified=['catalog'], partial=[('stocks', 'ADS_SOURCE_FAILED')]))   # ads reasons are ads-only
    calls, result = run(qualified=True, certified=['catalog'], results={'stocks': {'status': 'DISPATCHED', 'source_dispatches': 1}})
    assert calls == ['stocks'] and result['status'] == 'DISPATCHED'                                  # one source per wake
    # Catch-ups: finance first, then ads; an open ads day is drained before any new work.
    fin = {'authorities': ['f'], 'pending': [], 'next': (None, date(2026, 10, 30), {'plan': {}}), 'halted': None}
    ads = {'authorities': ['a'], 'pending': [], 'next': (a, date(2026, 10, 8), None), 'halted': None}
    all_seller = seller + ['ads_campaigns', 'ads_expense_daily', 'ads_sku_daily']
    with patched(AC, 'document', lambda aa, d: {'ack_hash': 'x', 'runtime_plan': {'entity': 'ads_expense_daily'}}), patched(AC, 'shard', lambda *x: 's'), patched(AC, 'root', lambda *x: 'r'):
        calls, _ = run(qualified=True, certified=all_seller, fin=fin, ads=ads)
        assert calls == ['fin:2026-10-30']
        calls, _ = run(qualified=True, certified=all_seller, ads=ads)
        assert calls == ['ads:2026-10-08']
        calls, _ = run(qualified=True, certified=['catalog'], ads=ads)
        assert 'ads:2026-10-08' not in calls                                                         # only after today's Seller CURRENT
        opened = dict(ads, pending=[(a, date(2026, 10, 8), {'plan': {}})])
        calls, _ = run(qualified=True, certified=['catalog'], ads=opened)
        assert calls == ['ads:2026-10-08']                                                           # drained first
        assert rejects(lambda: run(qualified=True, ads=opened, fin=dict(fin, pending=[(None, date(2026, 10, 30), {})])))
    return 'PASS'


# ---------------------------------------------------------------- catch-up
def authority_check():
    c, m, p, a = fixture()
    assert AC.full_cutoff(m, 'ads_expense_daily') == date(2026, 10, 7) == AC.full_cutoff(m, 'ads_sku_daily')
    assert AC.validate(a, c, m, p, NOW) is a and AC.name(p['hash'], 1) == 'BFADSCATCHUP_' + p['hash'][:16] + '_01'
    def reseal(**changes):
        x = {k: v for k, v in a.items() if k != 'hash'}; x.update(changes); return AC.sealed(x)
    bad = {'inside FULL': reseal(days=['2026-10-07'] + DAYS), 'reachable by CURRENT': reseal(days=DAYS + ['2026-11-07']),
           'snapshot entity': reseal(entity='ads_campaigns'), 'seller entity': reseal(entity='finance_accrual'),
           'unsorted': reseal(days=list(reversed(DAYS))), 'over 31': reseal(days=[(date(2026, 10, 8) + timedelta(days=i)).isoformat() for i in range(32)]),
           'mutable image': reseal(runtime_image='europe-west1-docker.pkg.dev/mpa-platform/mpa-runtime/ozon:latest'),
           'parallel': reseal(source_concurrency=2), 'foreign tenant': reseal(tenant='client_002'), 'tampered': dict(a, days=DAYS[:2]),
           'future': reseal(created_at='2026-11-08T12:00:00+00:00')}
    for label, x in bad.items(): assert rejects(lambda: AC.validate(x, c, m, p, NOW)), label
    sku = AC.authority(c, m, p, serial=2, previous=a, owner_ack_sha256='3' * 64, created_at=CREATED, entity='ads_sku_daily', days=DAYS[:3])
    assert AC.validate(sku, c, m, p, NOW, previous=a, owned={('ads_expense_daily', d) for d in DAYS}) is sku    # same day, other entity
    again = AC.authority(c, m, p, serial=2, previous=a, owner_ack_sha256='3' * 64, created_at=CREATED, entity='ads_expense_daily', days=DAYS[:1])
    assert rejects(lambda: AC.validate(again, c, m, p, NOW, previous=a, owned={('ads_expense_daily', d) for d in DAYS}))
    docs = {d: AC.document(a, date.fromisoformat(d)) for d in DAYS}
    for d, doc in docs.items():
        rp = doc['runtime_plan']
        assert (rp['entity'], rp['from'], rp['to'], rp['window_days']) == ('ads_expense_daily', d, d, 1) and rp['generation'].startswith('acu-')
    assert len({x['runtime_plan']['plan_id'] for x in docs.values()}) == len(DAYS)
    old = reseal(runtime_implementation_hash='e' * 64)
    pinned = AC.document(old, date(2026, 10, 8))
    assert pinned['runtime_plan']['implementation_hash'] == 'e' * 64 and pinned['ack_hash'] != docs['2026-10-08']['ack_hash']
    return 'PASS'


def catchup_tick_backend(a, day, *, rows=0, complete=False, start=None, reconcile_payload=None):
    doc = AC.document(a, day); item = dict(day=day.isoformat(), authority_hash=a['hash'], plan=doc, shard=AC.shard(a, day, doc))
    store = Store(); calls = []
    def select(sql, params):
        calls.append('accepted_readback'); assert params == {'day': ('DATE', day.isoformat()), 'entity': ('STRING', a['entity'])}
        assert AC.TABLES[a['entity']] in sql
        return [{'raw_rows': rows, 'coverage_rows': 0}]
    def default_start(d, before, after):
        before(dict(run_id=RUN, lease_generation=7, ack_hash=doc['ack_hash'], job='synthetic', overrides={}))
        calls.append('source_dispatch'); after(dict(run_id=RUN, lease_generation=7, ack_hash=doc['ack_hash'], operation='op'))
    state = {'plan_id': doc['runtime_plan']['plan_id'], 'sequence': 1 if complete else 0, 'complete': complete, 'progress': {'next_day': day.isoformat()}}
    b = SimpleNamespace(c=BF.target('client_001'), clock=lambda: NOW, catchup_root=AC.root(a, day), current_execution='x', store=store,
        preflight=lambda leaf: calls.append('preflight'), quota=lambda *x: {'status': 'ELIGIBLE', 'allowance': 0},
        active_runtime_execution=lambda: False, state=lambda d: state, select=select, start=start or default_start,
        reconcile=lambda payload: deepcopy(reconcile_payload) if reconcile_payload else {'source_complete': True})
    return b, item, store, calls


def catchup_tick_check():
    c, m, p, a = fixture(); day = date(2026, 10, 8)
    b, item, store, calls = catchup_tick_backend(a, day)
    got = AC.tick(b, a, day, item)
    assert got['status'] == 'DISPATCHED' and calls == ['preflight', 'accepted_readback', 'source_dispatch']
    attempts = []
    def lost(d, before, after):
        before(dict(run_id=RUN, lease_generation=7, ack_hash=d['ack_hash'], job='synthetic', overrides={})); attempts.append(1); raise OSError('lost')
    b, item, store, calls = catchup_tick_backend(a, day, start=lost)
    for _ in range(2): assert rejects(lambda: AC.tick(b, a, day, item))
    assert len(attempts) == 1 and [r['kind'] for r in store.records] == ['DISPATCH_INTENT']
    b, item, store, calls = catchup_tick_backend(a, day, rows=3)
    got = AC.tick(b, a, day, item)
    assert got['reason'] == 'ACCEPTED_DATA_PRESENT' and got['basis'] == 'ADS_CATCHUP_HALTED' and 'source_dispatch' not in calls
    def completed(cover=None, error=None, payload=None):
        b, item, store, calls = catchup_tick_backend(a, day, complete=payload is None, reconcile_payload=payload)
        receipt = dict(run_id=RUN, lease_generation=7, ack_hash=item['plan']['ack_hash'], operation='op')
        store.commit(item['shard'], 'DISPATCH_INTENT', 1, {'plan': item['plan']}, NOW)
        store.commit(item['shard'], 'DISPATCH_RECEIPT', 1, {'plan': item['plan'], 'receipt': receipt}, NOW)
        def verify(doc, ack, backend):
            if error: raise BF.B.EvidenceError(error)
            return {'source_sequence': 1, 'readback': [{'day': day.isoformat(), 'rows': 5, 'keys': 5, 'coverage': cover}]}
        with patched(BF, 'verify_coverage', verify), patched(CUR, 'catalog_current', lambda bb, text: {'day': text}):
            return AC.tick(b, a, day, item), store
    got, store = completed('COMPLETE')
    cert = [r for r in store.records if r['kind'] == 'ADSCATCHUP_CERT'][0]['payload']
    assert got['catchup_complete'] and cert['fact']['entity'] == 'ads_expense_daily' and cert['fact']['finance_finality'] == 'NOT_APPLICABLE'
    got, store = completed('UNKNOWN')
    assert got['basis'] == 'ADS_CATCHUP_DAY_UNKNOWN' and got['reason'] == AC.UNKNOWN_DAY
    got, store = completed(error='PILOT_DATA_RECONCILIATION_FAILED: x')
    assert got['reason'] == 'DQ_RECONCILIATION_FAILED'
    assert rejects(lambda: completed(error='source checkpoint disappeared'))
    for reason in sorted(ADS.REASONS - {'ADS_RETRY_EXHAUSTED'}):
        got, store = completed(payload={'failed': True, 'retry_category': reason, 'source_complete': False})
        assert got['basis'] == 'ADS_CATCHUP_HALTED' and got['reason'] == reason
    old = AC.sealed(dict({k: v for k, v in a.items() if k != 'hash'}, runtime_implementation_hash='e' * 64))
    b, item, store, calls = catchup_tick_backend(old, day)
    assert rejects(lambda: AC.tick(b, old, day, item)) and 'source_dispatch' not in calls          # superseded runtime: no dispatch
    return 'PASS'


def catchup_scope_check():
    from tools.tenancy import cloud_controller as CLOUD
    c, m, p, a = fixture()
    def backend(*, marker=True, qualified=True, store=None):
        env = c['orchestration']['job']['env']; reads = []
        objects = {}
        if marker: objects[('ref', AC.name(p['hash'], 1))] = AC.value(a)
        def get_table(ds, n): reads.append(n); return objects.get((ds, n))
        return SimpleNamespace(c=c, manifest=m, clock=lambda: NOW, store=store or Store(), tables=SimpleNamespace(get_table=get_table),
                               _current_ads_qualified=qualified), reads
    def elect(store, text):
        day = date.fromisoformat(text); doc = AC.document(a, day)
        it = dict(day=text, authority_hash=a['hash'], plan=doc, shard=AC.shard(a, day, doc)); store.commit(AC.root(a, day), 'ADSCATCHUP_PLAN', 0, it, NOW); return it
    b, reads = backend(marker=False)
    s = AC.scan(b, p, election=True)
    assert s['authorities'] == [] and reads == [AC.name(p['hash'], 1)] and b.store.reads == 0   # disabled: one metadata read
    with patched(AC, 'current_owned', lambda bb, pp: set()):
        b, _ = backend(qualified=False); it = elect(b.store, DAYS[0])
        b.store.commit(it['shard'], 'DISPATCH_INTENT', 1, {'plan': it['plan']}, NOW)
        b.store.commit(it['shard'], 'DISPATCH_RECEIPT', 1, {'plan': it['plan'], 'receipt': {'run_id': RUN, 'lease_generation': 7}}, NOW)
        s = AC.scan(b, p, election=True)
        assert s['halted'] == 'RELEASE_NOT_QUALIFIED' and [x[1].isoformat() for x in s['pending']] == [DAYS[0]] and s['next'] is None
        assert AC.receipts(b, p) == [{'run_id': RUN, 'lease_generation': 7}]
        b, _ = backend(); s = AC.scan(b, p, election=True)
        assert s['next'][1].isoformat() == DAYS[0] and s['halted'] is None
        # An empty source day is UNKNOWN and the chain continues; ACCEPTED_DATA_PRESENT halts it.
        it = elect(b.store, DAYS[0])
        b.store.commit(AC.root(a, date.fromisoformat(DAYS[0])), 'ADSCATCHUP_PARTIAL', 0, dict(day=DAYS[0], authority_hash=a['hash'], ack_hash=it['plan']['ack_hash'], status='PARTIAL', reason=AC.UNKNOWN_DAY), NOW)
        s = AC.scan(b, p, election=True)
        assert s['unknown'] == [('ads_expense_daily', DAYS[0])] and s['next'][1].isoformat() == DAYS[1] and s['halted'] is None
        it = elect(b.store, DAYS[1])
        b.store.commit(AC.root(a, date.fromisoformat(DAYS[1])), 'ADSCATCHUP_PARTIAL', 0, dict(day=DAYS[1], authority_hash=a['hash'], ack_hash=it['plan']['ack_hash'], status='PARTIAL', reason='ACCEPTED_DATA_PRESENT'), NOW)
        s = AC.scan(b, p, election=True)
        assert s['halted'] == 'PARTIAL_ACCEPTED_DATA_PRESENT' and s['next'] is None
        assert AC.summary(s)['unknown_days'] == 1 and AC.summary(s)['partial_days'] == 1
    with patched(AC, 'current_owned', lambda bb, pp: {('ads_expense_daily', DAYS[0])}):
        b, _ = backend(); s = AC.scan(b, p, election=True)
        assert s['halted'] == 'DAY_OWNED_BY_CURRENT' and s['next'] is None
        assert AC.scan(b, p)['next'][1].isoformat() == DAYS[0]                                      # read-only use: no CURRENT reads
    with patched(BF.B, 'implementation_hash', lambda: 'e' * 64):
        b, _ = backend()
        with patched(AC, 'current_owned', lambda bb, pp: set()):
            assert AC.scan(b, p, election=True)['halted'] == 'AUTHORITY_RUNTIME_SUPERSEDED'
    # current_owned: today's cycle and live plans only; a dead old plan's day may be caught up.
    item_old = current_item(p, date(2026, 10, 20), 'ads_expense_daily'); item_today = current_item(p, TODAY, 'ads_expense_daily')
    store = Store(); b = SimpleNamespace(clock=lambda: NOW, store=store, state=lambda d: {'progress': {}}, _current_ads_qualified=True)
    store.commit(item_old['shard'], 'DISPATCH_INTENT', 1, {'plan': item_old['plan']}, NOW); store.commit(item_old['shard'], 'RECONCILED', 1, {}, NOW)
    by = {'2026-10-20': item_old, TODAY.isoformat(): item_today}
    with patched(CUR, 'cycles', lambda bb, pp: {k: {} for k in by}), patched(CUR, 'plans', lambda bb, pp, d: ([], {'ads_expense_daily': by[d.isoformat()]})):
        assert AC.current_owned(b, p) == {('ads_expense_daily', '2026-11-07')}
        store.records = [r for r in store.records if r['kind'] != 'RECONCILED']
        assert AC.current_owned(b, p) == {('ads_expense_daily', '2026-11-07'), ('ads_expense_daily', '2026-10-19')}   # open intent: live
    return 'PASS'


# ---------------------------------------------------------------- status
def status_check():
    c, m, p, _ = fixture()
    items = {e: current_item(p, TODAY, e) for e in ADS.ENTITIES}
    yesterday = current_item(p, TODAY - timedelta(days=1), 'ads_campaigns')
    def run(records, states, histories=None, older=()):
        store = SimpleNamespace(history=lambda s, **k: (histories or {}).get(s, []))
        b = SimpleNamespace(clock=lambda: NOW, store=store, state=lambda d: states.get(d['runtime_plan']['entity'], {'sequence': 0, 'progress': {}}))
        cycles = {TODAY.isoformat(): {}, **{d: {} for d, _ in older}}
        def plans(bb, pp, d):
            if d == TODAY: return records, items
            return [r for dd, r in older if dd == d.isoformat()], {'ads_campaigns': yesterday}
        with patched(CUR, 'cycles', lambda bb, pp: cycles), patched(CUR, 'plans', plans), patched(AC, 'complete_days', lambda bb, pp: {e: [] for e in AC.ENTITIES}):
            return ADS.status(b, p)
    s = run([], {})
    assert all(v['status'] == 'STALE' and v['day_status'] == 'UNKNOWN' for v in s.values())         # nothing proven: never 0
    cert = lambda e: {'kind': 'CURRENT_CERT', 'payload': {'fact': {'entity': e}}}
    s = run([cert('ads_campaigns'), {'kind': 'CURRENT_PARTIAL', 'payload': {'entity': 'ads_expense_daily', 'reason': AC.UNKNOWN_DAY}}],
            {'ads_sku_daily': {'sequence': 4, 'progress': {'pending': ['1'] * 65}}})
    assert s['ads_campaigns']['status'] == 'COMPLETE' and s['ads_campaigns']['business_day'] == TODAY.isoformat()
    assert s['ads_expense_daily']['day_status'] == 'UNKNOWN' and s['ads_expense_daily']['business_day'] == '2026-11-07'
    assert s['ads_sku_daily']['day_status'] == 'PARTIAL' and s['ads_sku_daily']['progress'] == {'pending_campaigns': 65}
    s = run([{'kind': 'CURRENT_PARTIAL', 'payload': {'entity': 'ads_campaigns', 'reason': 'ADS_SOURCE_FAILED'}}], {},
            older=[((TODAY - timedelta(days=1)).isoformat(), cert('ads_campaigns'))])
    assert s['ads_campaigns']['day_status'] == 'PARTIAL' and s['ads_campaigns']['reason'] == 'ADS_SOURCE_FAILED'
    assert s['ads_campaigns']['status'] == 'PARTIAL' and s['ads_campaigns']['last_complete_day'] == (TODAY - timedelta(days=1)).isoformat()
    return 'PASS'


def check():
    for label, fn in (('release', release_check), ('settle', settle_check), ('gate', gate_check), ('tick', tick_check),
                      ('fence', fence_check), ('order', order_check), ('authority', authority_check),
                      ('catchup_tick', catchup_tick_check), ('catchup_scope', catchup_scope_check), ('status', status_check)):
        assert fn() == 'PASS', label
    return {'current_ads_adapter': 'PASS'}


if __name__ == '__main__':
    print(json.dumps(check(), sort_keys=True))
