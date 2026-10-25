"""Owner-authorised finance catch-up of exact missed accrual days (Heldi, 2026-10-25).

Why. CURRENT creates a cycle only for the Moscow day it runs on, and cycle D loads accruals of D-1;
the frozen FULL manifest ends at its finance cutoff and is never extended. Days between that cutoff
and the first CURRENT day, and D-1 of every day without a natural cycle, therefore have no source
authority at all. A missing accrual day is never zero: analytics_share keeps the whole finance tail
PARTIAL until the day has COMPLETE coverage.

Authority. One closed owner marker ``ref.BFFINCATCHUP_<policy16>_<serial>`` (OWNER_FINANCE_CATCHUP_V1),
bound to the FULL root and to the unchanged CURRENT policy, so it survives a controller handoff. It
lists exact finished days. It is neither a FULL leaf nor a CURRENT cycle and changes neither.

Protocol (its own durable scope; nothing accepted is rewritten):
  * a day is admissible only after the frozen FULL finance cutoff, at least two days before the
    authority (its CURRENT cycle can no longer be created), outside every CURRENT finance plan and
    outside every earlier authority of the chain;
  * the controller runs catch-up only after today's CURRENT has nothing left (Catalog CERT, every
    independent entity CERT or PARTIAL) and only when no CURRENT/FULL authority is open; an open
    catch-up receipt is drained before any new CURRENT work and holds the shared lease for S4;
  * day root = digest(authority, day): CATCHUP_PLAN, then the plan shard DISPATCH_INTENT/RECEIPT/
    RECONCILED through the one shared source path and pilot lease, then CATCHUP_CERT (the CURRENT
    certificate: COMPLETE dated readback, natural keys, DQ and DATA_COVERAGE) or CATCHUP_PARTIAL;
  * before the first dispatch of a day the persisted day must be empty (RAW rows and DATA_COVERAGE),
    otherwise CATCHUP_PARTIAL ACCEPTED_DATA_PRESENT without any source call;
  * the first PARTIAL halts this authority and every later serial without a STOP; CURRENT continues.
"""
from datetime import date, timedelta
from tools.tenancy import tenant_backfill as BF, durable_plan as D, current_contract as K
from tools.tenancy import current_controller as CURRENT, full_controller as H
from tools.tenancy.validation import parse_tenant_json as parse

TYPE = 'OWNER_FINANCE_CATCHUP_V1'
ENTITY = 'finance_accrual'
FIELDS = frozenset({'version', 'type', 'tenant', 'project', 'historical_root', 'policy_hash', 'serial',
    'previous', 'owner_ack_sha256', 'created_at', 'entity', 'days', 'source_concurrency',
    'finance_finality', 'hash'})
KINDS = frozenset({'CATCHUP_PLAN', 'CATCHUP_CERT', 'CATCHUP_PARTIAL'})
PARTIAL_FIELDS = frozenset({'day', 'authority_hash', 'ack_hash', 'status', 'reason'})
REASONS = frozenset({'SOURCE_RETENTION_COMPLETENESS_UNPROVEN', 'ACCEPTED_DATA_PRESENT', 'DQ_RECONCILIATION_FAILED'})
# Persisted-data verdicts of the dated readback. They end this day as PARTIAL (never COMPLETE) and
# halt the authority instead of a global STOP; every other error stays fail-closed.
DQ_CODES = ('PILOT_DATA_RECONCILIATION_FAILED', 'PILOT_SKU_LINKAGE_UNPROVEN', 'PILOT_CATALOG_SKU_JOIN_AMBIGUOUS')
MAX_DAYS = 31
MAX_SERIAL = 20
CLOSED_LAG_DAYS = 2


def fail(s): raise BF.B.EvidenceError('finance catch-up: ' + s)


def name(policy_hash, serial):
    D.check_hash(policy_hash)
    if type(serial) is not int or not 1 <= serial <= MAX_SERIAL: fail('bounded authority serial required')
    return f'BFFINCATCHUP_{policy_hash[:16]}_{serial:02d}'


def value(a): return {'kind': 'finance_catchup', 'root': a['hash'][:16]}, D.encoded(a)
def sealed(a): return dict(a, hash=D.digest(a))


def full_cutoff(manifest):
    ends = [p['to'] for p in manifest['programs'] if p['entity'] == ENTITY]
    if not ends: fail('frozen FULL finance scope absent')
    return max(date.fromisoformat(x) for x in ends)


def authority(c, manifest, policy, *, serial, previous, owner_ack_sha256, created_at, days):
    """Owner-side constructor; publication is a separate owner step (publish)."""
    return sealed(dict(version=1, type=TYPE, tenant=c['tenant_id'], project=c['project_id'],
        historical_root=manifest['hash'], policy_hash=policy['hash'], serial=serial,
        previous=previous['hash'] if previous else None, owner_ack_sha256=owner_ack_sha256,
        created_at=created_at, entity=ENTITY, days=list(days), source_concurrency=1,
        finance_finality='PROVISIONAL'))


def validate(a, c, manifest, policy, now, *, previous=None, owned=frozenset()):
    if not isinstance(a, dict) or set(a) != FIELDS or type(a['version']) is not int or a['version'] != 1 or a['type'] != TYPE:
        fail('closed owner authority required')
    if a['hash'] != D.digest({k: v for k, v in a.items() if k != 'hash'}): fail('authority digest differs')
    for k in ('hash', 'historical_root', 'policy_hash', 'owner_ack_sha256'): D.check_hash(a[k])
    if (a['tenant'], a['project']) != (c['tenant_id'], c['project_id']) or (a['tenant'], a['project']) != ('client_001', 'mpa-t-client-001'):
        fail('unqualified tenant scope')
    if a['historical_root'] != manifest['hash'] or a['policy_hash'] != policy['hash']: fail('FULL root/CURRENT policy differs')
    if (a['entity'] != ENTITY or a['finance_finality'] != 'PROVISIONAL'
            or type(a['source_concurrency']) is not int or a['source_concurrency'] != 1):
        fail('entity/finality/concurrency differs')
    if type(a['serial']) is not int or not 1 <= a['serial'] <= MAX_SERIAL: fail('bounded authority serial required')
    if a['serial'] == 1:
        if a['previous'] is not None or previous is not None: fail('first authority has no predecessor')
    elif previous is None or a['previous'] != previous['hash'] or a['serial'] != previous['serial'] + 1:
        fail('authority chain differs')
    created = K.at(a['created_at'])
    if created > now or created < K.at(policy['created_at']) or (previous and created < K.at(previous['created_at'])):
        fail('authority chronology differs')
    days = a['days']
    if not isinstance(days, list) or not 1 <= len(days) <= MAX_DAYS: fail('bounded day list required')
    parsed = []
    for text in days:
        try: day = date.fromisoformat(text)
        except (TypeError, ValueError): fail('exact ISO day required')
        if day.isoformat() != text: fail('exact ISO day required')
        parsed.append(day)
    if parsed != sorted(set(parsed)): fail('strictly ascending unique days required')
    if parsed[0] <= full_cutoff(manifest): fail('day inside the frozen FULL history')
    if parsed[-1] > created.astimezone(K.MSK).date() - timedelta(days=CLOSED_LAG_DAYS):
        fail('day still reachable by a natural CURRENT cycle')
    if set(days) & set(owned): fail('day already owned by an earlier authority')
    return a


def authorities(b, manifest, policy):
    """Exact owner chain oldest first; [] without a marker (one metadata read)."""
    out = []; owned = set()
    for serial in range(1, MAX_SERIAL + 1):
        marker = b.tables.get_table(b.c['datasets']['ref'], name(policy['hash'], serial))
        if marker is None: break
        a = parse(marker[1])
        validate(a, b.c, manifest, policy, b.clock(), previous=out[-1] if out else None, owned=owned)
        if marker != value(a): fail('owner marker differs')
        out.append(a); owned |= set(a['days'])
    return out


def qualified(b):
    """The installed release explicitly carries the qualified catch-up adapter."""
    from tools.tenancy import cloud_controller as C
    env = b.c['orchestration']['job']['env']
    spec = b.tables.get_table(b.c['datasets']['tenant_locks'], C.descriptor_name(env['CONTROLLER_SOURCE_SHA'], b.manifest['hash'], env['HISTORICAL_SCHEDULER_STATE']))
    release = parse(spec[1])['release'] if spec else {}
    return release.get('finance_catchup_version') == 1 and release.get('verification', {}).get('finance_catchup_adapter') == 'PASS'


def root(a, day): return D.digest(dict(finance_catchup=a['hash'], moscow_day=day.isoformat()))
def generation(a, day): return 'fcu-' + a['hash'][:16] + '-' + day.strftime('%Y%m%d')
def shard(a, day, doc): return D.digest(dict(finance_catchup_day=root(a, day), ack_hash=doc['ack_hash']))


def document(a, day, *, make_plan=None):
    make_plan = make_plan or BF.make_plan
    created = K.at(a['created_at']).astimezone(K.MSK).date()
    return make_plan(a['tenant'], ENTITY, day.isoformat(), day.isoformat(), generation(a, day),
        a['created_at'], today=created, max_units=20, window_days=1)


def day_state(b, a, day):
    """(plan item | None, certificate | None, partial | None) from the day's own durable root."""
    plans, certs, partials = [], [], []
    for r in b.store.history(root(a, day)):
        if r['kind'] not in KINDS or r['sequence'] != 0: fail('unknown catch-up day record')
        v = r['payload']
        if r['kind'] == 'CATCHUP_PLAN':
            if (set(v) != {'day', 'authority_hash', 'plan', 'shard'} or (v['day'], v['authority_hash']) != (day.isoformat(), a['hash'])
                    or v['plan'] != document(a, day) or v['shard'] != shard(a, day, v['plan'])):
                fail('catch-up plan ownership differs')
            plans.append(v)
        elif r['kind'] == 'CATCHUP_CERT': certs.append(v)
        else: partials.append(v)
    if len(plans) > 1 or len(certs) > 1 or len(partials) > 1 or (certs and partials): fail('conflicting catch-up day authority')
    item = plans[0] if plans else None
    for cert in certs:
        if (item is None or not isinstance(cert, dict) or cert.get('fact', {}).get('entity') != ENTITY
                or cert.get('fact', {}).get('current_scope') != a['hash']
                or (cert.get('policy_hash'), cert.get('day'), cert.get('ack_hash'), cert.get('shard')) != (a['hash'], day.isoformat(), item['plan']['ack_hash'], item['shard'])):
            fail('catch-up certificate ownership differs')
    for v in partials:
        if (set(v) != PARTIAL_FIELDS or (v['day'], v['authority_hash'], v['status']) != (day.isoformat(), a['hash'], 'PARTIAL')
                or v['reason'] not in REASONS or v['ack_hash'] != document(a, day)['ack_hash']):
            fail('catch-up partial evidence differs')
    return item, (certs[0] if certs else None), (partials[0] if partials else None)


def scan(b, policy, current_finance=None):
    """One read of the catch-up scope per wake.

    pending: open (intent without reconciliation) days; next: the first day without completion;
    halted: the reason no further day may start. current_finance (days of CURRENT finance plans) is
    None only for read-only use (S4 lease ownership), never for a new election.
    """
    out = dict(authorities=[], pending=[], next=None, halted=None, complete=[], partial=[])
    chain = authorities(b, b.manifest, policy)
    if not chain or not qualified(b): return out
    out['authorities'] = [a['hash'] for a in chain]
    for a in chain:
        for text in a['days']:
            day = date.fromisoformat(text)
            item, cert, partial = day_state(b, a, day)
            if item is not None:
                hs = b.store.history(item['shard'])
                intents = [r['sequence'] for r in hs if r['kind'] == 'DISPATCH_INTENT']
                if intents and not any(r['kind'] == 'RECONCILED' and r['sequence'] == max(intents) for r in hs):
                    out['pending'].append((a, day, item))
            if cert is not None: out['complete'].append(text); continue
            if partial is not None:
                out['partial'].append(text); out['halted'] = out['halted'] or 'PARTIAL_' + partial['reason']; continue
            if current_finance is not None and text in current_finance:
                out['halted'] = out['halted'] or 'DAY_OWNED_BY_CURRENT'
            if out['next'] is None and out['halted'] is None: out['next'] = (a, day, item)
    if len(out['pending']) > 1: fail('multiple catch-up source authorities')
    return out


def receipts(b, policy):
    """Receipts of open catch-up shards (S4: the held lease may belong to exactly one of them)."""
    out = []
    for a, day, item in scan(b, policy)['pending']:
        hs = b.store.history(item['shard'])
        last = max(r['sequence'] for r in hs if r['kind'] == 'DISPATCH_INTENT')
        out += [r['payload']['receipt'] for r in hs if r['kind'] == 'DISPATCH_RECEIPT' and r['sequence'] == last]
    return out


def summary(s):
    return {'authorities': len(s['authorities']), 'complete_days': len(s['complete']),
            'partial_days': len(s['partial']), 'halted': s['halted'],
            'next_day': s['next'][1].isoformat() if s['next'] else None}


def drain(base, s):
    a, day, item = s['pending'][0]
    return tick(Backend(base, a, day, item), a, day, item)


def wake(base, s, certs, partial):
    """New catch-up work only after today's CURRENT has nothing left; None when there is none."""
    from tools.tenancy import wake_budget as WB
    if not s['authorities'] or s['next'] is None or s['halted'] is not None: return None
    if 'catalog' not in certs or not set(CURRENT.INDEPENDENT_ENTITIES) <= set(certs) | set(partial): return None
    a, day, item = s['next']
    budget = WB.of(base)
    if budget is not None and not budget.allows(WB.LEAF_SECONDS):
        return WB.handoff('LEAF', budget, operational_entity=ENTITY, catchup_day=day.isoformat())
    if item is None:
        doc = document(a, day)
        item = {'day': day.isoformat(), 'authority_hash': a['hash'], 'plan': doc, 'shard': shard(a, day, doc)}
        base.store.commit(root(a, day), 'CATCHUP_PLAN', 0, item, base.clock())
    return tick(Backend(base, a, day, item), a, day, item)


def accepted_rows(b, day):
    """Persisted RAW rows and coverage of the day (both must be zero before the first dispatch)."""
    p, ds = b.c['project_id'], b.c['datasets']
    rows = b.select(f"SELECT (SELECT COUNT(*) FROM `{p}.{ds['ozon_raw']}.RAW_OZON_FINANCE_ACCRUAL` WHERE event_date = @day) AS raw_rows,"
                    f"(SELECT COUNT(*) FROM `{p}.{ds['tenant_ops']}.DATA_COVERAGE` WHERE entity = 'finance_accrual' AND coverage_date = @day) AS coverage_rows",
                    {'day': ('DATE', day.isoformat())})
    if len(rows) != 1: fail('accepted-data readback ambiguous')
    return int(rows[0]['raw_rows']) + int(rows[0]['coverage_rows'])


def halt(b, a, day, item, reason):
    v = {'day': day.isoformat(), 'authority_hash': a['hash'], 'ack_hash': item['plan']['ack_hash'], 'status': 'PARTIAL', 'reason': reason}
    b.store.commit(b.catchup_root, 'CATCHUP_PARTIAL', 0, v, b.clock())
    return {'status': 'WAITING', 'source_dispatches': 0, 'basis': 'FINANCE_CATCHUP_HALTED', 'reason': reason,
            'operational_entity': ENTITY, 'catchup_day': day.isoformat()}


def complete(b, a, day, item, history):
    try:
        cert = CURRENT.certificate(b, {'hash': a['hash']}, day, item, history)
    except BF.B.EvidenceError as error:
        if not str(error).startswith(DQ_CODES): raise
        return halt(b, a, day, item, 'DQ_RECONCILIATION_FAILED')
    if cert is None: return halt(b, a, day, item, 'SOURCE_RETENTION_COMPLETENESS_UNPROVEN')
    b.store.commit(b.catchup_root, 'CATCHUP_CERT', 0, cert, b.clock())
    return {'status': 'RECONCILED_BOUNDARY', 'source_dispatches': 0, 'operational_entity': ENTITY,
            'catchup_day': day.isoformat(), 'catchup_complete': True}


def tick(b, a, day, item):
    """CURRENT source protocol on the catch-up day scope; at most one reconcile and one dispatch."""
    from tools.tenancy import wake_budget as WB
    from tools.tenancy.controller_diagnostics import gate
    doc = item['plan']; sh = item['shard']; leaf = {'hash': b.catchup_root, 'plans': [doc]}
    tag = {'operational_entity': ENTITY, 'catchup_day': day.isoformat()}
    b.preflight(leaf)
    if b.active_runtime_execution(): return dict(status='MONITORING', source_dispatches=0, **tag)
    history = b.store.history(sh); q = b.quota(leaf, b.clock())
    decision = D.decide_tick(history, sh, q, b.active_runtime_execution()); action = decision['action']
    if action == 'MONITOR': return dict(status='MONITORING', source_dispatches=0, **tag)
    if action in {'STOPPED', 'RECOVER_RECEIPT_OR_STOP'}: fail('uncertain catch-up dispatch or source authority; never repeat')
    budget = WB.of(b)
    if action == 'RECONCILE':
        if budget is not None and not budget.allows(WB.RECONCILE_SECONDS): return WB.handoff('RECONCILE', budget, **tag)
        seq = decision['sequence']
        payload = next(r['payload'] for r in history if r['kind'] == 'DISPATCH_RECEIPT' and r['sequence'] == seq)
        if payload['plan'] != doc: fail('catch-up receipt plan differs')
        result = b.reconcile(payload)
        if result is None: return dict(status='MONITORING', source_dispatches=0, **tag)
        b.store.commit(sh, 'RECONCILED', seq, result, b.clock())
        history = b.store.history(sh)
    if b.state(doc)['complete']:
        # The certificate links every SKU to today's Catalog snapshot. A receipt drained after Moscow
        # midnight waits for today's CURRENT Catalog CERT instead of failing the linkage.
        if CURRENT.catalog_current(b, b.clock().astimezone(K.MSK).date().isoformat()) is None:
            return dict(status='RECONCILED_BOUNDARY' if action == 'RECONCILE' else 'WAITING', source_dispatches=0,
                        basis='FINANCE_CATCHUP_AWAITS_TODAYS_CATALOG', **tag)
        if budget is not None and not budget.allows(WB.COMPLETE_SECONDS): return WB.handoff('COMPLETE', budget, **tag)
        return complete(b, a, day, item, history)
    if action in {'WAITING', 'RECONCILE'}: return dict(status='WAITING', source_dispatches=0, **tag)
    if action != 'PREPARE_NEXT': fail('unknown catch-up tick decision')
    if budget is not None and not budget.allows(WB.DISPATCH_SECONDS): return WB.handoff('DISPATCH', budget, **tag)
    seq = decision['sequence']
    if seq == 1 and accepted_rows(b, day): return halt(b, a, day, item, 'ACCEPTED_DATA_PRESENT')
    prepared, received = [], []
    def before(preparation):
        if prepared: fail('one intent per source wake')
        b.store.commit(sh, 'DISPATCH_INTENT', seq, {'plan': doc, 'preparation': preparation, 'current_parent_execution': b.current_execution}, b.clock())
        prepared.append(preparation)
    def after(receipt):
        if len(prepared) != 1 or received or any(receipt.get(k) != prepared[0][k] for k in ('run_id', 'lease_generation', 'ack_hash')):
            fail('catch-up intent/receipt mismatch')
        b.store.commit(sh, 'DISPATCH_RECEIPT', seq, {'plan': doc, 'receipt': receipt}, b.clock()); received.append(receipt)
    with gate('SOURCE_DISPATCH_BOUNDARY'): b.start(doc, before, after)
    if len(prepared) != 1 or len(received) != 1: fail('uncertain catch-up dispatch outcome; never repeat')
    return dict(status='DISPATCHED', source_dispatches=1, **tag)


class Backend(CURRENT.Backend):
    """CURRENT source adapter bound to one elected catch-up day; never a CURRENT cycle or FULL leaf."""
    def __init__(self, base, a, day, item):
        H.Backend.__init__(self, base, base.manifest)
        self.catchup_authority, self.catchup_day, self.catchup_item = a, day, item
        self.catchup_root = root(a, day)
        self.full_dispatch_authority = None; self.full_dispatch_intent = None

    def authorize_full_leaf(self, doc):
        a = self.catchup_authority; p = CURRENT.policy(self)
        if p is None or a not in authorities(self, self.manifest, p): fail('owner authority changed')
        if document(a, self.catchup_day) != doc or self.catchup_item['plan'] != doc: fail('catch-up plan/window/budget differs')
        if day_state(self, a, self.catchup_day)[0] != self.catchup_item: fail('catch-up plan is not durably elected')
        return True

    def active_source_shard(self, doc):
        self.authorize_full_leaf(doc)
        return self.catchup_item['shard']

    def preflight(self, leaf, *, owner_observation=False):
        if owner_observation or leaf.get('hash') != self.catchup_root or len(leaf.get('plans', [])) != 1:
            fail('catch-up cloud-only scope differs')
        self.active = False
        doc = leaf['plans'][0]
        self.authorize_full_leaf(doc)
        BF.preflight(BF.validate_plan(doc, doc['ack_hash']), doc, self.clock(), backend=self, allow_active=True)


def current_finance_days(b, policy):
    """Accrual days owned by CURRENT finance plans (owner-side preview)."""
    out = set()
    for text in CURRENT.cycles(b, policy):
        _, items = CURRENT.plans(b, policy, date.fromisoformat(text))
        if ENTITY in items: out.add(items[ENTITY]['plan']['runtime_plan']['from'])
    return out


def publish(b, a):
    """Owner publication only; automatic controllers have no authority to create the marker."""
    if getattr(b, 'current_execution', None) is not None or getattr(b, 'phase_b_ack_sha256', None) != a.get('owner_ack_sha256'):
        fail('explicit owner backend required')
    p = CURRENT.policy(b)
    if p is None: fail('CURRENT policy absent')
    chain = authorities(b, b.manifest, p)
    if a['serial'] != len(chain) + 1: fail('serial differs from the published chain')
    validate(a, b.c, b.manifest, p, b.clock(), previous=chain[-1] if chain else None, owned={d for x in chain for d in x['days']})
    if set(a['days']) & current_finance_days(b, p): fail('day owned by a CURRENT finance plan')
    for text in a['days']:
        if accepted_rows(b, date.fromisoformat(text)): fail('accepted data present for ' + text)
    ds = b.c['datasets']['ref']; n = name(p['hash'], a['serial']); marker = value(a)
    if b.tables.get_table(ds, n) is None: b.tables.create_marker(ds, n, *marker)
    if b.tables.get_table(ds, n) != marker: fail('owner authority CAS conflict')
    return a
