"""Owner-authorised advertising catch-up of exact missed days (Heldi, 2026-11-09).

Why. The frozen FULL manifest ends its advertising programs at 2026-10-07; CURRENT advertising was off
from S4 v1 (2026-10-23) and throttled before that, and a CURRENT cycle only loads D-1 of its own day.
Days from 2026-10-08 to the day before ads run in CURRENT therefore have no source authority. A
missing day is never zero: it stays UNKNOWN until the day has COMPLETE coverage.

Authority. A closed owner marker ``ref.BFADSCATCHUP_<policy16>_<serial>`` (OWNER_ADS_CATCHUP_V1),
bound to the FULL root, to the unchanged CURRENT policy and to the runtime pair it was published for.
One authority = one entity (ads_expense_daily or ads_sku_daily) and ≤31 exact finished days; serials
form a chain, (entity, day) is owned by at most one authority. Snapshots (ads_campaigns) are never
caught up: a past snapshot cannot be reconstructed.

Protocol (the finance catch-up protocol, its own durable scope; nothing accepted is rewritten):
  * a day is admissible only after the FULL cutoff of its entity, at least two days before the authority
    (no natural CURRENT cycle can load it any more) and outside earlier authorities; at election time it
    must not be owned by a live CURRENT plan (today's cycle, an open intent or an async report scope);
  * runs only after today's CURRENT has nothing left and after the finance catch-up has nothing to do;
    an open ads catch-up receipt is drained before any new CURRENT work (one source authority);
  * day root = digest(authority, day): ADSCATCHUP_PLAN, then the plan shard
    DISPATCH_INTENT/RECEIPT/RECONCILED through the one shared source path, then ADSCATCHUP_CERT (the
    CURRENT certificate) or ADSCATCHUP_PARTIAL;
  * before the first dispatch the persisted day must be empty (RAW rows and DATA_COVERAGE), otherwise
    PARTIAL ACCEPTED_DATA_PRESENT without a source call (halts the chain: rows are never rewritten);
  * an empty source day (retention completeness unproven) is PARTIAL = UNKNOWN and does NOT halt;
  * DQ verdicts and ads-local failures (current_ads: report POST outcome unknown, source failure,
    retries exhausted) end the day PARTIAL and halt the chain (a new owner decision is needed);
  * SKU days use the same Performance fences and the 15-export rolling guard as everything else, at the
    lowest priority: while daily CURRENT SKU uses the guard, an SKU catch-up day waits (never COMPLETE
    without a separate quota decision);
  * a runtime change after publication halts new days (AUTHORITY_RUNTIME_SUPERSEDED); new days need the
    qualified release flag current_ads_version; an open day is drained whatever the release.
"""
from copy import deepcopy
from datetime import date, timedelta
from tools.tenancy import tenant_backfill as BF, durable_plan as D, current_contract as K
from tools.tenancy import current_controller as CURRENT, full_controller as H, current_policy as P
from tools.tenancy import current_ads as ADS
from tools.tenancy.validation import parse_tenant_json as parse

TYPE = 'OWNER_ADS_CATCHUP_V1'
ENTITIES = ('ads_expense_daily', 'ads_sku_daily')
TABLES = {'ads_expense_daily': 'RAW_OZON_ADS_EXPENSE_DAILY', 'ads_sku_daily': 'RAW_OZON_ADS_SKU_DAILY'}
FIELDS = frozenset({'version', 'type', 'tenant', 'project', 'historical_root', 'policy_hash', 'serial',
    'previous', 'owner_ack_sha256', 'created_at', 'entity', 'days', 'runtime_image',
    'runtime_implementation_hash', 'source_concurrency', 'hash'})
KINDS = frozenset({'ADSCATCHUP_PLAN', 'ADSCATCHUP_CERT', 'ADSCATCHUP_PARTIAL'})
PARTIAL_FIELDS = frozenset({'day', 'authority_hash', 'ack_hash', 'status', 'reason'})
UNKNOWN_DAY = 'SOURCE_RETENTION_COMPLETENESS_UNPROVEN'
HALTING = frozenset({'ACCEPTED_DATA_PRESENT', 'DQ_RECONCILIATION_FAILED'}) | ADS.REASONS
REASONS = HALTING | {UNKNOWN_DAY}
DQ_CODES = ('PILOT_DATA_RECONCILIATION_FAILED', 'PILOT_SKU_LINKAGE_UNPROVEN', 'PILOT_CATALOG_SKU_JOIN_AMBIGUOUS')
MAX_DAYS = 31
MAX_SERIAL = 20
CLOSED_LAG_DAYS = 2


def fail(s): raise BF.B.EvidenceError('ads catch-up: ' + s)


def name(policy_hash, serial):
    D.check_hash(policy_hash)
    if type(serial) is not int or not 1 <= serial <= MAX_SERIAL: fail('bounded authority serial required')
    return f'BFADSCATCHUP_{policy_hash[:16]}_{serial:02d}'


def value(a): return {'kind': 'ads_catchup', 'root': a['hash'][:16]}, D.encoded(a)
def sealed(a): return dict(a, hash=D.digest(a))


def full_cutoff(manifest, entity):
    ends = [p['to'] for p in manifest['programs'] if p['entity'] == entity and p.get('to')]
    if not ends: fail('frozen FULL advertising scope absent')
    return max(date.fromisoformat(x) for x in ends)


def authority(c, manifest, policy, *, serial, previous, owner_ack_sha256, created_at, entity, days):
    """Owner-side constructor for the live runtime pair; publication is a separate owner step."""
    return sealed(dict(version=1, type=TYPE, tenant=c['tenant_id'], project=c['project_id'],
        historical_root=manifest['hash'], policy_hash=policy['hash'], serial=serial,
        previous=previous['hash'] if previous else None, owner_ack_sha256=owner_ack_sha256,
        created_at=created_at, entity=entity, days=list(days),
        runtime_image=c['marketplaces']['ozon']['runtime_image'],
        runtime_implementation_hash=BF.B.implementation_hash(), source_concurrency=1))


def validate(a, c, manifest, policy, now, *, previous=None, owned=frozenset()):
    if not isinstance(a, dict) or set(a) != FIELDS or type(a['version']) is not int or a['version'] != 1 or a['type'] != TYPE:
        fail('closed owner authority required')
    if a['hash'] != D.digest({k: v for k, v in a.items() if k != 'hash'}): fail('authority digest differs')
    for k in ('hash', 'historical_root', 'policy_hash', 'owner_ack_sha256', 'runtime_implementation_hash'): D.check_hash(a[k])
    if (a['tenant'], a['project']) != (c['tenant_id'], c['project_id']) or (a['tenant'], a['project']) != ('client_001', 'mpa-t-client-001'):
        fail('unqualified tenant scope')
    if a['historical_root'] != manifest['hash'] or a['policy_hash'] != policy['hash']: fail('FULL root/CURRENT policy differs')
    if a['entity'] not in ENTITIES or type(a['source_concurrency']) is not int or a['source_concurrency'] != 1:
        fail('entity/concurrency differs')
    if not isinstance(a['runtime_image'], str) or not a['runtime_image'].startswith('europe-west1-docker.pkg.dev/mpa-platform/mpa-runtime/') or '@sha256:' not in a['runtime_image']:
        fail('immutable runtime image required')
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
    if parsed[0] <= full_cutoff(manifest, a['entity']): fail('day inside the frozen FULL history')
    if parsed[-1] > created.astimezone(K.MSK).date() - timedelta(days=CLOSED_LAG_DAYS):
        fail('day still reachable by a natural CURRENT cycle')
    if {(a['entity'], d) for d in days} & set(owned): fail('day already owned by an earlier authority')
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
        out.append(a); owned |= {(a['entity'], d) for d in a['days']}
    return out


def root(a, day): return D.digest(dict(ads_catchup=a['hash'], moscow_day=day.isoformat()))
def generation(a, day): return 'acu-' + a['hash'][:16] + '-' + day.strftime('%Y%m%d')
def shard(a, day, doc): return D.digest(dict(ads_catchup_day=root(a, day), ack_hash=doc['ack_hash']))
def live_runtime(a): return BF.B.implementation_hash() == a['runtime_implementation_hash']


_DOCUMENTS = {}


def document(a, day, *, make_plan=None):
    """The day plan pinned to the authority's runtime pair (the live plan while that pair is installed).

    Pure in (authority, day, live runtime): memoised per process, a wake scans every authorised day."""
    key = (a['hash'], day.isoformat(), BF.B.implementation_hash())
    if make_plan is None and key in _DOCUMENTS: return deepcopy(_DOCUMENTS[key])
    out = _document(a, day, make_plan=make_plan)
    if make_plan is None: _DOCUMENTS[key] = deepcopy(out)
    return out


def _document(a, day, *, make_plan=None):
    make_plan = make_plan or BF.make_plan
    created = K.at(a['created_at']).astimezone(K.MSK).date()
    live = make_plan(a['tenant'], a['entity'], day.isoformat(), day.isoformat(), generation(a, day),
        a['created_at'], today=created, max_units=20, window_days=1)
    if (live['runtime_plan']['implementation_hash'], live['image']) == (a['runtime_implementation_hash'], a['runtime_image']): return live
    return P.original_runtime({'runtime_implementation_hash': a['runtime_implementation_hash'], 'runtime_image': a['runtime_image']}, live)


def day_state(b, a, day):
    """(plan item | None, certificate | None, partial | None) from the day's own durable root."""
    plans, certs, partials = [], [], []
    doc = document(a, day)
    for r in b.store.history(root(a, day)):
        if r['kind'] not in KINDS or r['sequence'] != 0: fail('unknown catch-up day record')
        v = r['payload']
        if r['kind'] == 'ADSCATCHUP_PLAN':
            if (set(v) != {'day', 'authority_hash', 'plan', 'shard'} or (v['day'], v['authority_hash']) != (day.isoformat(), a['hash'])
                    or v['plan'] != doc or v['shard'] != shard(a, day, v['plan'])):
                fail('catch-up plan ownership differs')
            plans.append(v)
        elif r['kind'] == 'ADSCATCHUP_CERT': certs.append(v)
        else: partials.append(v)
    if len(plans) > 1 or len(certs) > 1 or len(partials) > 1 or (certs and partials): fail('conflicting catch-up day authority')
    item = plans[0] if plans else None
    for cert in certs:
        if (item is None or not isinstance(cert, dict) or cert.get('fact', {}).get('entity') != a['entity']
                or cert.get('fact', {}).get('current_scope') != a['hash']
                or (cert.get('policy_hash'), cert.get('day'), cert.get('ack_hash'), cert.get('shard')) != (a['hash'], day.isoformat(), item['plan']['ack_hash'], item['shard'])):
            fail('catch-up certificate ownership differs')
    for v in partials:
        if (set(v) != PARTIAL_FIELDS or (v['day'], v['authority_hash'], v['status'], v['ack_hash']) != (day.isoformat(), a['hash'], 'PARTIAL', doc['ack_hash'])
                or v['reason'] not in REASONS):
            fail('catch-up partial evidence differs')
    return item, (certs[0] if certs else None), (partials[0] if partials else None)


def current_owned(b, policy):
    """(entity, business day) of LIVE CURRENT ads plans: today's cycle, an open intent or an async report scope.

    An old cycle's plan without either is never dispatched again by CURRENT (dead); its day may be caught up
    when its persisted day is empty (accepted_rows)."""
    out = set(); now = b.clock(); today = now.astimezone(K.MSK).date()
    for text in CURRENT.cycles(b, policy):
        cycle = date.fromisoformat(text)
        _, items = CURRENT.plans(b, policy, cycle)
        for e in ENTITIES:
            item = items.get(e)
            if item is None: continue
            if cycle == today or ADS.live(b, item, cycle, today, now): out.add((e, item['plan']['runtime_plan']['from']))
    return out


def scan(b, policy, *, election=False):
    """One read of the ads catch-up scope per wake (finance_catchup.scan semantics).

    pending: open days; next: the first day without completion; halted: why no further day may start.
    election=False is read-only use (lease ownership, fences): live CURRENT ads days are not read."""
    out = dict(authorities=[], pending=[], next=None, halted=None, complete=[], partial=[], unknown=[], receipts=[], live=[])
    chain = authorities(b, b.manifest, policy)
    if not chain: return out
    current = current_owned(b, policy) if election else None
    out['authorities'] = [a['hash'] for a in chain]
    if not ADS.qualified(b): out['halted'] = 'RELEASE_NOT_QUALIFIED'
    for a in chain:
        if not live_runtime(a): out['halted'] = out['halted'] or 'AUTHORITY_RUNTIME_SUPERSEDED'
        for text in a['days']:
            day = date.fromisoformat(text); key = (a['entity'], text)
            item, cert, partial = day_state(b, a, day)
            if cert is not None: out['complete'].append(key); continue
            if partial is not None:
                if partial['reason'] == UNKNOWN_DAY: out['unknown'].append(key); continue
                out['partial'].append(key); out['halted'] = out['halted'] or 'PARTIAL_' + partial['reason']; continue
            if item is not None:
                hs = b.store.history(item['shard'])
                if ADS.open_intent(hs):
                    out['pending'].append((a, day, item))
                    last = max(r['sequence'] for r in hs if r['kind'] == 'DISPATCH_INTENT')
                    out['receipts'] += [r['payload']['receipt'] for r in hs if r['kind'] == 'DISPATCH_RECEIPT' and r['sequence'] == last]
            if current is not None and key in current:
                out['halted'] = out['halted'] or 'DAY_OWNED_BY_CURRENT'
            if out['next'] is None and out['halted'] is None: out['next'] = (a, day, item)
    if len(out['pending']) > 1: fail('multiple catch-up source authorities')
    out['live'] = [x[2] for x in out['pending']] + ([out['next'][2]] if out['next'] and out['next'][2] and not out['pending'] else [])
    return out


def receipts(b, policy): return scan(b, policy)['receipts']
def live_items(b, policy):
    """Elected catch-up plans that can still hold a source cooldown or an async report scope."""
    return scan(b, policy)['live']


def complete_days(b, policy):
    out = {e: [] for e in ENTITIES}
    for e, d in scan(b, policy)['complete']: out[e].append(d)
    return out


def summary(s):
    return {'authorities': len(s['authorities']), 'complete_days': len(s['complete']), 'unknown_days': len(s['unknown']),
            'partial_days': len(s['partial']), 'halted': s['halted'],
            'next_day': s['next'][1].isoformat() if s['next'] else None}


def drain(base, s):
    a, day, item = s['pending'][0]
    return tick(Backend(base, a, day, item), a, day, item)


def wake(base, s, certs, partial):
    """New ads catch-up work only after today's CURRENT has nothing left; None when there is none."""
    from tools.tenancy import wake_budget as WB
    if not s['authorities'] or s['next'] is None or s['halted'] is not None: return None
    if 'catalog' not in certs or not set(CURRENT.INDEPENDENT_ENTITIES) <= set(certs) | set(partial): return None
    a, day, item = s['next']
    budget = WB.of(base)
    if budget is not None and not budget.allows(WB.LEAF_SECONDS):
        return WB.handoff('LEAF', budget, operational_entity=a['entity'], catchup_day=day.isoformat())
    if item is None:
        doc = document(a, day)
        item = {'day': day.isoformat(), 'authority_hash': a['hash'], 'plan': doc, 'shard': shard(a, day, doc)}
        base.store.commit(root(a, day), 'ADSCATCHUP_PLAN', 0, item, base.clock())
    return tick(Backend(base, a, day, item), a, day, item)


def accepted_rows(b, entity, day):
    """Persisted RAW rows and coverage of the day (both must be zero before the first dispatch)."""
    p, ds = b.c['project_id'], b.c['datasets']
    rows = b.select(f"SELECT (SELECT COUNT(*) FROM `{p}.{ds['ozon_raw']}.{TABLES[entity]}` WHERE date = @day) AS raw_rows,"
                    f"(SELECT COUNT(*) FROM `{p}.{ds['tenant_ops']}.DATA_COVERAGE` WHERE entity = @entity AND coverage_date = @day) AS coverage_rows",
                    {'day': ('DATE', day.isoformat()), 'entity': ('STRING', entity)})
    if len(rows) != 1: fail('accepted-data readback ambiguous')
    return int(rows[0]['raw_rows']) + int(rows[0]['coverage_rows'])


def halt(b, a, day, item, reason):
    v = {'day': day.isoformat(), 'authority_hash': a['hash'], 'ack_hash': item['plan']['ack_hash'], 'status': 'PARTIAL', 'reason': reason}
    b.store.commit(b.catchup_root, 'ADSCATCHUP_PARTIAL', 0, v, b.clock())
    return {'status': 'WAITING', 'source_dispatches': 0, 'basis': 'ADS_CATCHUP_DAY_UNKNOWN' if reason == UNKNOWN_DAY else 'ADS_CATCHUP_HALTED',
            'reason': reason, 'operational_entity': a['entity'], 'catchup_day': day.isoformat()}


def complete(b, a, day, item, history):
    try:
        cert = CURRENT.certificate(b, {'hash': a['hash']}, day, item, history)
    except BF.B.EvidenceError as error:
        if not str(error).startswith(DQ_CODES): raise
        return halt(b, a, day, item, 'DQ_RECONCILIATION_FAILED')
    if cert is None: return halt(b, a, day, item, UNKNOWN_DAY)
    b.store.commit(b.catchup_root, 'ADSCATCHUP_CERT', 0, cert, b.clock())
    return {'status': 'RECONCILED_BOUNDARY', 'source_dispatches': 0, 'operational_entity': a['entity'],
            'catchup_day': day.isoformat(), 'catchup_complete': True}


def tick(b, a, day, item):
    """CURRENT source protocol on the catch-up day scope; at most one reconcile and one dispatch."""
    from tools.tenancy import wake_budget as WB
    from tools.tenancy.controller_diagnostics import gate
    doc = item['plan']; sh = item['shard']; leaf = {'hash': b.catchup_root, 'plans': [doc]}
    tag = {'operational_entity': a['entity'], 'catchup_day': day.isoformat()}
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
    local = ADS.failure_gate(history, b.clock())
    if local is not None:
        if local[0] == 'HALT': return halt(b, a, day, item, local[1])
        return dict(status='WAITING', source_dispatches=0, basis='ADS_READ_RETRY_COOLDOWN', eligible_at=local[1], **tag)
    if b.state(doc)['complete']:
        if CURRENT.catalog_current(b, b.clock().astimezone(K.MSK).date().isoformat()) is None:
            return dict(status='RECONCILED_BOUNDARY' if action == 'RECONCILE' else 'WAITING', source_dispatches=0,
                        basis='ADS_CATCHUP_AWAITS_TODAYS_CATALOG', **tag)
        if budget is not None and not budget.allows(WB.COMPLETE_SECONDS): return WB.handoff('COMPLETE', budget, **tag)
        return complete(b, a, day, item, history)
    if action in {'WAITING', 'RECONCILE'}: return dict(status='WAITING', source_dispatches=0, **tag)
    if action != 'PREPARE_NEXT': fail('unknown catch-up tick decision')
    if budget is not None and not budget.allows(WB.DISPATCH_SECONDS): return WB.handoff('DISPATCH', budget, **tag)
    if not live_runtime(a): fail('authority runtime superseded; never dispatched')
    seq = decision['sequence']
    if seq == 1 and accepted_rows(b, a['entity'], day): return halt(b, a, day, item, 'ACCEPTED_DATA_PRESENT')
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
    """CURRENT source adapter bound to one elected ads catch-up day; never a CURRENT cycle or FULL leaf."""
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


def publish(b, a):
    """Owner publication only; automatic controllers have no authority to create the marker."""
    if getattr(b, 'current_execution', None) is not None or getattr(b, 'phase_b_ack_sha256', None) != a.get('owner_ack_sha256'):
        fail('explicit owner backend required')
    p = CURRENT.policy(b)
    if p is None: fail('CURRENT policy absent')
    chain = authorities(b, b.manifest, p)
    if a['serial'] != len(chain) + 1: fail('serial differs from the published chain')
    validate(a, b.c, b.manifest, p, b.clock(), previous=chain[-1] if chain else None,
             owned={(x['entity'], d) for x in chain for d in x['days']})
    if not live_runtime(a) or a['runtime_image'] != b.c['marketplaces']['ozon']['runtime_image']: fail('authority runtime is not installed')
    if {(a['entity'], d) for d in a['days']} & current_owned(b, p): fail('day owned by a live CURRENT ads plan')
    for text in a['days']:
        if accepted_rows(b, a['entity'], date.fromisoformat(text)): fail('accepted data present for ' + text)
    ds = b.c['datasets']['ref']; n = name(p['hash'], a['serial']); marker = value(a)
    if b.tables.get_table(ds, n) is None: b.tables.create_marker(ds, n, *marker)
    if b.tables.get_table(ds, n) != marker: fail('owner authority CAS conflict')
    return a
