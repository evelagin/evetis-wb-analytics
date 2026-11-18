"""Owner FBO refresh of the rolling 90 days (Heldi, 2026-11-09; design A2 #404 §8, contract #406).

Why. FBO is in neither CURRENT v1 (fbo_contract_hash None) nor the frozen FULL manifest (cutover
07.10; every FBO leaf isolated by the leaf-269 FBO_SOURCE quarantine, which stays). Statuses,
cancellations and returns change for weeks, so FBO is not a load-once source: days are re-read.

Authority. One closed owner marker ``ref.BFFBOREFRESH_<policy16>_<serial>`` (OWNER_FBO_REFRESH_V1),
bound to the FULL root, the unchanged CURRENT policy and the proven FBO transport contract hash. The
newest serial is the active one; an earlier serial only drains its open dispatch. It is neither a
FULL leaf nor a CURRENT cycle and changes neither; CURRENT must not own FBO at the same time.

Units (owned windows [a, b) by in_process_at, Moscow days, at most window_days long):
  * DAILY  of MSK day d: [d - daily_days, d - 1];
  * WEEKLY of MSK day d (option 1 only, on weekly_isoweekday): [d - weekly_to, d - weekly_from];
  * HISTORY: [history_from, history_to], newest window first.
  Each wake elects, in that order, the first unit without completion: an elected unit without an
  open dispatch is continued before any new one; DAILY/WEEKLY units of an earlier day that were never
  elected are superseded by the next day's (overlapping) unit.
  Owner options (refresh.mode): DAILY7_WEEKLY8_35 (default) or DAILY14 — closed configurations.

Verdict (replaces the catch-up rule "the day must be empty"): MERGE by (posting_number, sku) updates
statuses without duplicates; tenant_backfill.fbo_day_verdict proves the run per day: COMPLETE, or
SOURCE_DISAPPEARED (source proved in RAW, older rows not returned again; nothing deleted) → local
REFRESH_PARTIAL with the counts. Retention-unproven days and DQ verdicts are local PARTIAL too. Any
other mismatch is unproven persistence and stays a global STOP. A PARTIAL never halts other units.

Budget. Each dispatch is one bounded plan (max_requests 400, max_units_per_run one-day leaves). The
authority caps dispatches per MSK day (REFRESH_DISPATCH ledger in the scope root): the Ozon ceiling
is 400 × max_dispatches_per_day requests a day.

Records (scope root = digest(fbo_refresh=authority hash)): REFRESH_PLAN (sequence = unit ordinal),
REFRESH_DISPATCH (sequence = dispatch ordinal), REFRESH_CERT / REFRESH_PARTIAL (sequence = unit
ordinal). One history read of the scope root per wake, plus one shard read per open unit.
"""
from datetime import date, timedelta
from tools.tenancy import tenant_backfill as BF, durable_plan as D, current_contract as K
from tools.tenancy import current_controller as CURRENT, full_controller as H
from tools.tenancy.validation import parse_tenant_json as parse

VERSION = 1
TYPE = 'OWNER_FBO_REFRESH_V1'
ENTITY = 'fbo_postings'
FIELDS = frozenset({'version', 'type', 'tenant', 'project', 'historical_root', 'policy_hash', 'serial', 'previous',
    'owner_ack_sha256', 'created_at', 'entity', 'fbo_contract_hash', 'history_from', 'history_to', 'window_days',
    'refresh', 'max_units_per_run', 'max_dispatches_per_day', 'source_concurrency', 'hash'})
KINDS = frozenset({'REFRESH_PLAN', 'REFRESH_DISPATCH', 'REFRESH_CERT', 'REFRESH_PARTIAL'})
MODES = {
    'DAILY7_WEEKLY8_35': {'mode': 'DAILY7_WEEKLY8_35', 'daily_days': 7, 'weekly_from': 8, 'weekly_to': 35, 'weekly_isoweekday': 1},
    'DAILY14': {'mode': 'DAILY14', 'daily_days': 14, 'weekly_from': None, 'weekly_to': None, 'weekly_isoweekday': None},
}
DEFAULT_MODE = 'DAILY7_WEEKLY8_35'
UNIT_KINDS = ('DAILY', 'WEEKLY', 'HISTORY')
REASONS = frozenset({BF.FBO_SOURCE_DISAPPEARED, 'SOURCE_RETENTION_COMPLETENESS_UNPROVEN', 'DQ_RECONCILIATION_FAILED'})
DQ_CODES = ('PILOT_SKU_LINKAGE_UNPROVEN', 'PILOT_CATALOG_SKU_JOIN_AMBIGUOUS')
MAX_SERIAL = 20
MAX_HISTORY_DAYS = 120
MAX_REQUESTS_PER_DISPATCH = 400          # tenant_backfill.make_plan max_requests


def fail(s): raise BF.B.EvidenceError('fbo refresh: ' + s)


def name(policy_hash, serial):
    D.check_hash(policy_hash)
    if type(serial) is not int or not 1 <= serial <= MAX_SERIAL: fail('bounded authority serial required')
    return f'BFFBOREFRESH_{policy_hash[:16]}_{serial:02d}'


def value(a): return {'kind': 'fbo_refresh', 'root': a['hash'][:16]}, D.encoded(a)
def sealed(a): return dict(a, hash=D.digest(a))
def scope_root(a): return D.digest(dict(fbo_refresh=a['hash']))
def created_day(a): return K.at(a['created_at']).astimezone(K.MSK).date()


def authority(c, manifest, policy, *, serial, previous, owner_ack_sha256, created_at, history_from, history_to,
              mode=DEFAULT_MODE, window_days=7, max_units_per_run=7, max_dispatches_per_day=8):
    """Owner-side constructor (default: option 1, daily 7 + weekly 8–35); publication is `publish`."""
    if mode not in MODES: fail('unknown owner refresh option')
    return sealed(dict(version=VERSION, type=TYPE, tenant=c['tenant_id'], project=c['project_id'],
        historical_root=manifest['hash'], policy_hash=policy['hash'], serial=serial,
        previous=previous['hash'] if previous else None, owner_ack_sha256=owner_ack_sha256, created_at=created_at,
        entity=ENTITY, fbo_contract_hash=BF.B.FBO_TRANSPORT_CONTRACT_HASH, history_from=history_from,
        history_to=history_to, window_days=window_days, refresh=dict(MODES[mode]),
        max_units_per_run=max_units_per_run, max_dispatches_per_day=max_dispatches_per_day, source_concurrency=1))


def _int(v, low, high): return type(v) is int and low <= v <= high


def _day(text):
    try: d = date.fromisoformat(text)
    except (TypeError, ValueError): fail('exact ISO day required')
    if d.isoformat() != text: fail('exact ISO day required')
    return d


def validate(a, c, manifest, policy, now, *, previous=None):
    if not isinstance(a, dict) or set(a) != FIELDS or type(a['version']) is not int or a['version'] != VERSION or a['type'] != TYPE:
        fail('closed owner authority required')
    if a['hash'] != D.digest({k: v for k, v in a.items() if k != 'hash'}): fail('authority digest differs')
    for k in ('hash', 'historical_root', 'policy_hash', 'owner_ack_sha256', 'fbo_contract_hash'): D.check_hash(a[k])
    if (a['tenant'], a['project']) != (c['tenant_id'], c['project_id']) or (a['tenant'], a['project']) != ('client_001', 'mpa-t-client-001'):
        fail('unqualified tenant scope')
    if a['historical_root'] != manifest['hash'] or a['policy_hash'] != policy['hash']: fail('FULL root/CURRENT policy differs')
    if policy.get('fbo_contract_hash') is not None: fail('CURRENT owns FBO; one FBO writer only')
    if a['entity'] != ENTITY or type(a['source_concurrency']) is not int or a['source_concurrency'] != 1: fail('entity/concurrency differs')
    if a['fbo_contract_hash'] != BF.B.FBO_TRANSPORT_CONTRACT_HASH: fail('installed FBO transport contract differs')
    if not isinstance(a['refresh'], dict) or a['refresh'].get('mode') not in MODES or a['refresh'] != MODES[a['refresh']['mode']]:
        fail('refresh option is not an owner-approved configuration')
    if not _int(a['window_days'], 1, 7): fail('owned window of 1..7 days required')
    if not _int(a['max_units_per_run'], a['window_days'], 20): fail('one dispatch must cover one window (units 1..20)')
    if not _int(a['max_dispatches_per_day'], 1, 12): fail('bounded dispatches per day required')
    if type(a['serial']) is not int or not 1 <= a['serial'] <= MAX_SERIAL: fail('bounded authority serial required')
    if a['serial'] == 1:
        if a['previous'] is not None or previous is not None: fail('first authority has no predecessor')
    elif previous is None or a['previous'] != previous['hash'] or a['serial'] != previous['serial'] + 1:
        fail('authority chain differs')
    created = K.at(a['created_at'])
    if created > now or created < K.at(policy['created_at']) or (previous and created < K.at(previous['created_at'])):
        fail('authority chronology differs')
    lo, hi = _day(a['history_from']), _day(a['history_to'])
    if not lo <= hi < created.astimezone(K.MSK).date() or (hi - lo).days + 1 > MAX_HISTORY_DAYS:
        fail('bounded finished history window required')
    return a


def authorities(b, manifest, policy):
    """Exact owner chain oldest first; [] without a marker (one metadata read)."""
    out = []
    for serial in range(1, MAX_SERIAL + 1):
        marker = b.tables.get_table(b.c['datasets']['ref'], name(policy['hash'], serial))
        if marker is None: break
        a = parse(marker[1])
        validate(a, b.c, manifest, policy, b.clock(), previous=out[-1] if out else None)
        if marker != value(a): fail('owner marker differs')
        out.append(a)
    return out


def qualified(b):
    from tools.tenancy import cloud_controller as C
    env = b.c['orchestration']['job']['env']
    spec = b.tables.get_table(b.c['datasets']['tenant_locks'], C.descriptor_name(env['CONTROLLER_SOURCE_SHA'], b.manifest['hash'], env['HISTORICAL_SCHEDULER_STATE']))
    release = parse(spec[1])['release'] if spec else {}
    return release.get('fbo_refresh_version') == 1 and release.get('verification', {}).get('fbo_refresh_adapter') == 'PASS'


# ── units ──────────────────────────────────────────────────────────────────────────────────
def _chunks(lo, hi, width):
    """[lo, hi] in windows of `width` days, newest first, aligned at hi."""
    out, end = [], hi
    while end >= lo:
        start = max(lo, end - timedelta(days=width - 1))
        out.append((start, end)); end = start - timedelta(days=1)
    return out


def unit(kind, cycle, frm, to):
    if kind not in UNIT_KINDS: fail('unknown refresh unit kind')
    return {'kind': kind, 'cycle': cycle.isoformat() if cycle else None, 'from': frm.isoformat(), 'to': to.isoformat()}


def schedule(a, today):
    """Units the authority asks for on MSK day `today`, in election order."""
    r, w, out = a['refresh'], a['window_days'], []
    if today >= created_day(a):
        out += [unit('DAILY', today, f, t) for f, t in _chunks(today - timedelta(days=r['daily_days']), today - timedelta(days=1), w)]
        if r['weekly_isoweekday'] is not None and today.isoweekday() == r['weekly_isoweekday']:
            out += [unit('WEEKLY', today, f, t) for f, t in _chunks(today - timedelta(days=r['weekly_to']), today - timedelta(days=r['weekly_from']), w)]
    out += [unit('HISTORY', None, f, t) for f, t in _chunks(_day(a['history_from']), _day(a['history_to']), w)]
    return out


def generation(a, u):
    return 'fbr-' + a['hash'][:16] + '-' + u['kind'][0] + (u['cycle'] or '').replace('-', '') + '-' + u['from'].replace('-', '') + u['to'].replace('-', '')


def document(a, u, *, make_plan=None):
    make_plan = make_plan or BF.make_plan
    to = date.fromisoformat(u['to'])
    return make_plan(a['tenant'], ENTITY, u['from'], u['to'], generation(a, u), a['created_at'],
        today=to + timedelta(days=1), max_units=a['max_units_per_run'], window_days=1)


def shard(a, u, doc): return D.digest(dict(fbo_refresh_unit=scope_root(a), unit=u, ack_hash=doc['ack_hash']))


def scope_state(b, a):
    """(plans {seq: item}, terminal {seq: record payload}, dispatch ledger [payload]) of one authority."""
    plans, terminal, ledger = {}, {}, []
    for r in b.store.history(scope_root(a)):
        if r['kind'] not in KINDS: fail('unknown refresh scope record')
        v, s = r['payload'], r['sequence']
        if r['kind'] == 'REFRESH_PLAN':
            u = v.get('unit') if isinstance(v, dict) else None
            if (not isinstance(u, dict) or set(v) != {'unit', 'authority_hash', 'plan', 'shard', 'elected_on'} or v['authority_hash'] != a['hash']
                    or unit(u['kind'], date.fromisoformat(u['cycle']) if u['cycle'] else None, date.fromisoformat(u['from']), date.fromisoformat(u['to'])) != u
                    or v['plan'] != document(a, u) or v['shard'] != shard(a, u, v['plan']) or s in plans):
                fail('refresh plan ownership differs')
            plans[s] = v
        elif r['kind'] == 'REFRESH_DISPATCH':
            if set(v) != {'unit_sequence', 'day', 'shard'}: fail('refresh dispatch ledger differs')
            ledger.append(v)
        else:
            if s in terminal: fail('conflicting refresh unit authority')
            terminal[s] = dict(v, kind=r['kind'])
    for s, v in terminal.items():
        item = plans.get(s)
        if item is None or v.get('ack_hash') != item['plan']['ack_hash'] or v.get('unit') != item['unit']:
            fail('refresh unit verdict without its plan')
        if v['kind'] == 'REFRESH_PARTIAL' and (v.get('status') != 'PARTIAL' or v.get('reason') not in REASONS):
            fail('refresh partial evidence differs')
    return plans, terminal, ledger


def scan(b, policy):
    """One read of the refresh scope per wake (plus one shard read per elected unfinished unit)."""
    out = dict(authorities=[], pending=[], next=None, halted=None, complete=0, partial=[], receipts=[],
               dispatches_today=0, today_daily=None, today_incidents=[])
    chain = authorities(b, b.manifest, policy)
    if not chain: return out
    out['authorities'] = [a['hash'] for a in chain]
    if not qualified(b): out['halted'] = 'RELEASE_NOT_QUALIFIED'
    today = b.clock().astimezone(K.MSK).date()
    for a in chain:
        active = a is chain[-1]
        plans, terminal, ledger = scope_state(b, a)
        out['complete'] += sum(1 for v in terminal.values() if v['kind'] == 'REFRESH_CERT')
        out['partial'] += [{'unit': v['unit'], 'reason': v['reason'], 'days': v.get('days', [])} for v in terminal.values() if v['kind'] == 'REFRESH_PARTIAL']
        out['today_incidents'] += [{'unit': v['unit'], 'reason': v['reason'], 'days': v.get('days', [])} for s, v in sorted(terminal.items())
                                   if v['kind'] == 'REFRESH_PARTIAL' and (plans[s]['elected_on'] == today.isoformat() or v['unit']['cycle'] == today.isoformat())]
        if active: out['dispatches_today'] = sum(1 for v in ledger if v['day'] == today.isoformat())
        for s, item in sorted(plans.items()):
            if s in terminal: continue
            hs = b.store.history(item['shard'])
            intents = [r['sequence'] for r in hs if r['kind'] == 'DISPATCH_INTENT']
            if intents and not any(r['kind'] == 'RECONCILED' and r['sequence'] == max(intents) for r in hs):
                out['pending'].append((a, s, item))
                out['receipts'] += [r['payload']['receipt'] for r in hs if r['kind'] == 'DISPATCH_RECEIPT' and r['sequence'] == max(intents)]
            elif active and out['next'] is None:
                out['next'] = (a, s, item)
        if active:
            done = {D.encoded(p['unit']) for p in plans.values()}
            daily = [u for u in schedule(a, today) if u['kind'] == 'DAILY']
            certified = {D.encoded(plans[s]['unit']) for s, v in terminal.items() if v['kind'] == 'REFRESH_CERT'}
            partial = {D.encoded(plans[s]['unit']) for s, v in terminal.items() if v['kind'] == 'REFRESH_PARTIAL'}
            keys = [D.encoded(u) for u in daily]
            out['today_daily'] = ('PARTIAL' if any(k in partial for k in keys) or out['today_incidents']
                                  else 'COMPLETE' if keys and all(k in certified for k in keys) else 'STALE')
            if out['next'] is None:
                fresh = next((u for u in schedule(a, today) if D.encoded(u) not in done), None)
                if fresh is not None: out['next'] = (a, (max(plans) + 1) if plans else 0, fresh)
    if len(out['pending']) > 1: fail('multiple refresh source authorities')
    return out


def receipts(b, policy):
    """Receipts of open refresh shards (S4: the held lease may belong to exactly one of them)."""
    return scan(b, policy)['receipts']


def entity_status(s):
    """FBO operational status in the A2 vocabulary (durable_daily.entity_status): PARTIAL when a unit
    reached a local PARTIAL verdict today (or today's DAILY did), COMPLETE when every window of today's
    DAILY unit is certified, else STALE. None without an authority (FBO is not an operational entity)."""
    return s['today_daily'] if s['authorities'] else None


def summary(s):
    return {'authorities': len(s['authorities']), 'complete_units': s['complete'], 'partial_units': len(s['partial']),
            'incidents': s['partial'], 'today_incidents': s['today_incidents'], 'halted': s['halted'], 'dispatches_today': s['dispatches_today'],
            'fbo_status': entity_status(s),
            'next_unit': (s['next'][2]['unit'] if 'plan' in s['next'][2] else s['next'][2]) if s['next'] else None}


def annotate(result, summary):
    """The controller's JSON stdout line (stream E, #383): `current_entities.fbo_postings` carries the FBO
    status and `fbo_refresh.incidents` the typed reasons, so the literal token SOURCE_DISAPPEARED reaches
    the log-based metric heldi_source_disappeared and the PARTIAL alert. A verdict written in this very
    wake counts at once. No authority → the line is unchanged."""
    if not isinstance(result, dict) or not summary or not summary.get('authorities'): return result
    status = summary['fbo_status']; incidents = list(summary['today_incidents'])
    if result.get('refresh_verdict') == 'REFRESH_PARTIAL':
        status = 'PARTIAL'
        if not any(i['reason'] == result.get('reason') and i['unit'] == result.get('refresh_unit_window') for i in incidents):
            incidents.append({'unit': result.get('refresh_unit_window'), 'reason': result.get('reason'), 'days': result.get('refresh_days', [])})
    entities = result.setdefault('current_entities', {})
    if isinstance(entities, dict): entities[ENTITY] = status
    result['fbo_refresh'] = {'status': status, 'incidents': incidents, 'halted': summary['halted'],
                             'dispatches_today': summary['dispatches_today'], 'next_unit': summary['next_unit']}
    return result


def drain(base, s):
    a, seq, item = s['pending'][0]
    return tick(Backend(base, a, seq, item), a, seq, item)


def wake(base, s, certs, partial):
    """New refresh work only after today's CURRENT and finance catch-up have nothing left; None if none."""
    from tools.tenancy import wake_budget as WB
    if not s['authorities'] or s['next'] is None or s['halted'] is not None: return None
    if 'catalog' not in certs or not set(CURRENT.INDEPENDENT_ENTITIES) <= set(certs) | set(partial): return None
    a, seq, item = s['next']
    budget = WB.of(base)
    if budget is not None and not budget.allows(WB.LEAF_SECONDS):
        return WB.handoff('LEAF', budget, operational_entity=ENTITY, refresh_unit=seq)
    if 'plan' not in item:                                    # a fresh unit: elect it durably first
        if s['dispatches_today'] >= a['max_dispatches_per_day']:
            return {'status': 'WAITING', 'source_dispatches': 0, 'basis': 'FBO_REFRESH_DAILY_BUDGET', 'operational_entity': ENTITY}
        doc = document(a, item)
        item = {'unit': item, 'authority_hash': a['hash'], 'plan': doc, 'shard': shard(a, item, doc),
                'elected_on': base.clock().astimezone(K.MSK).date().isoformat()}
        base.store.commit(scope_root(a), 'REFRESH_PLAN', seq, item, base.clock())
    b = Backend(base, a, seq, item)
    b.refresh_dispatches_today = s['dispatches_today']
    return tick(b, a, seq, item)


def verdict(b, a, seq, item, history):
    """REFRESH_CERT when every day is COMPLETE; otherwise a local REFRESH_PARTIAL with the counts."""
    doc = item['plan']; state = b.state(doc)
    if state['complete'] is not True: fail('partial source cannot receive refresh completion')
    receipts = {r['sequence']: r for r in history if r['kind'] == 'DISPATCH_RECEIPT'}
    reconciled = {r['sequence']: r for r in history if r['kind'] == 'RECONCILED'}
    if not receipts or set(receipts) != set(reconciled): fail('refresh pending receipt cannot count as complete')
    base = {'unit': item['unit'], 'authority_hash': a['hash'], 'ack_hash': doc['ack_hash'], 'shard': item['shard']}
    try:
        proof = BF.verify_coverage(doc, doc['ack_hash'], backend=b, fbo_run_verdict=True)
    except BF.B.EvidenceError as error:
        if not str(error).startswith(DQ_CODES): raise
        return 'REFRESH_PARTIAL', dict(base, status='PARTIAL', reason='DQ_RECONCILIATION_FAILED', days=[])
    days = [v for v in proof['readback'] if 'day' in v]
    expected = (date.fromisoformat(doc['runtime_plan']['to']) - date.fromisoformat(doc['runtime_plan']['from'])).days + 1
    if len(days) != expected: fail('refresh readback does not cover the unit')
    gone = [{'day': v['day'], 'extra_rows': v['extra_rows'], 'run_rows': v['run_rows']} for v in days if v.get('reason') == BF.FBO_SOURCE_DISAPPEARED]
    if gone:
        return 'REFRESH_PARTIAL', dict(base, status='PARTIAL', reason=BF.FBO_SOURCE_DISAPPEARED, days=gone)
    if any(v.get('coverage') != 'COMPLETE' for v in days):
        return 'REFRESH_PARTIAL', dict(base, status='PARTIAL', reason='SOURCE_RETENTION_COMPLETENESS_UNPROVEN',
                                       days=[{'day': v['day']} for v in days if v.get('coverage') != 'COMPLETE'])
    return 'REFRESH_CERT', dict(base, state_hash=D.digest(state), source_sequence=state['sequence'], rows=sum(v['rows'] for v in days),
        source_run_id=receipts[max(receipts)]['payload']['receipt']['run_id'],
        accepted_receipt_hash=D.digest(receipts[max(receipts)]), reconciliation_hash=D.digest(reconciled[max(reconciled)]),
        certificate_hash=D.digest(dict(plan=doc['ack_hash'], proof=proof, reconciled=[D.digest(r) for _, r in sorted(reconciled.items())])))


def tick(b, a, seq, item):
    """CURRENT source protocol on one refresh unit; at most one reconcile and one dispatch."""
    from tools.tenancy import wake_budget as WB
    from tools.tenancy.controller_diagnostics import gate
    doc = item['plan']; sh = item['shard']; leaf = {'hash': b.refresh_root, 'plans': [doc]}
    tag = {'operational_entity': ENTITY, 'refresh_unit': seq}
    b.preflight(leaf)
    if b.active_runtime_execution(): return dict(status='MONITORING', source_dispatches=0, **tag)
    history = b.store.history(sh); q = b.quota(leaf, b.clock())
    decision = D.decide_tick(history, sh, q, b.active_runtime_execution()); action = decision['action']
    if action == 'MONITOR': return dict(status='MONITORING', source_dispatches=0, **tag)
    if action in {'STOPPED', 'RECOVER_RECEIPT_OR_STOP'}: fail('uncertain refresh dispatch or source authority; never repeat')
    budget = WB.of(b)
    if action == 'RECONCILE':
        if budget is not None and not budget.allows(WB.RECONCILE_SECONDS): return WB.handoff('RECONCILE', budget, **tag)
        s = decision['sequence']
        payload = next(r['payload'] for r in history if r['kind'] == 'DISPATCH_RECEIPT' and r['sequence'] == s)
        if payload['plan'] != doc: fail('refresh receipt plan differs')
        result = b.reconcile(payload)
        if result is None: return dict(status='MONITORING', source_dispatches=0, **tag)
        b.store.commit(sh, 'RECONCILED', s, result, b.clock())
        history = b.store.history(sh)
    if b.state(doc)['complete']:
        if CURRENT.catalog_current(b, b.clock().astimezone(K.MSK).date().isoformat()) is None:
            return dict(status='RECONCILED_BOUNDARY' if action == 'RECONCILE' else 'WAITING', source_dispatches=0,
                        basis='FBO_REFRESH_AWAITS_TODAYS_CATALOG', **tag)
        if budget is not None and not budget.allows(WB.COMPLETE_SECONDS): return WB.handoff('COMPLETE', budget, **tag)
        kind, v = verdict(b, a, seq, item, history)
        b.store.commit(b.refresh_root, kind, seq, v, b.clock())
        return dict(status='RECONCILED_BOUNDARY', source_dispatches=0, refresh_verdict=kind,
                    **({'reason': v['reason'], 'refresh_unit_window': v['unit'], 'refresh_days': v['days']} if kind == 'REFRESH_PARTIAL' else {}), **tag)
    if action in {'WAITING', 'RECONCILE'}: return dict(status='WAITING', source_dispatches=0, **tag)
    if action != 'PREPARE_NEXT': fail('unknown refresh tick decision')
    if budget is not None and not budget.allows(WB.DISPATCH_SECONDS): return WB.handoff('DISPATCH', budget, **tag)
    used = getattr(b, 'refresh_dispatches_today', None)
    if used is None: used = scope_dispatches_today(b, a)
    if used >= a['max_dispatches_per_day']:
        return dict(status='WAITING', source_dispatches=0, basis='FBO_REFRESH_DAILY_BUDGET', **tag)
    s = decision['sequence']
    prepared, received = [], []
    def before(preparation):
        if prepared: fail('one intent per source wake')
        ledger = scope_state(b, a)[2]
        b.store.commit(b.refresh_root, 'REFRESH_DISPATCH', len(ledger) + 1,
                       {'unit_sequence': seq, 'day': b.clock().astimezone(K.MSK).date().isoformat(), 'shard': sh}, b.clock())
        b.store.commit(sh, 'DISPATCH_INTENT', s, {'plan': doc, 'preparation': preparation, 'current_parent_execution': b.current_execution}, b.clock())
        prepared.append(preparation)
    def after(receipt):
        if len(prepared) != 1 or received or any(receipt.get(k) != prepared[0][k] for k in ('run_id', 'lease_generation', 'ack_hash')):
            fail('refresh intent/receipt mismatch')
        b.store.commit(sh, 'DISPATCH_RECEIPT', s, {'plan': doc, 'receipt': receipt}, b.clock()); received.append(receipt)
    with gate('SOURCE_DISPATCH_BOUNDARY'): b.start(doc, before, after)
    if len(prepared) != 1 or len(received) != 1: fail('uncertain refresh dispatch outcome; never repeat')
    return dict(status='DISPATCHED', source_dispatches=1, **tag)


def scope_dispatches_today(b, a):
    today = b.clock().astimezone(K.MSK).date().isoformat()
    return sum(1 for v in scope_state(b, a)[2] if v['day'] == today)


class Backend(CURRENT.Backend):
    """CURRENT source adapter bound to one elected refresh unit; never a CURRENT cycle or FULL leaf."""
    def __init__(self, base, a, seq, item):
        H.Backend.__init__(self, base, base.manifest)
        self.refresh_authority, self.refresh_sequence, self.refresh_item = a, seq, item
        self.refresh_root = scope_root(a)
        self.full_dispatch_authority = None; self.full_dispatch_intent = None

    def authorize_full_leaf(self, doc):
        a = self.refresh_authority; p = CURRENT.policy(self)
        if p is None or a not in authorities(self, self.manifest, p): fail('owner authority changed')
        if document(a, self.refresh_item['unit']) != doc or self.refresh_item['plan'] != doc: fail('refresh plan/window/budget differs')
        if scope_state(self, a)[0].get(self.refresh_sequence) != self.refresh_item: fail('refresh unit is not durably elected')
        return True

    def active_source_shard(self, doc):
        self.authorize_full_leaf(doc)
        return self.refresh_item['shard']

    def preflight(self, leaf, *, owner_observation=False):
        if owner_observation or leaf.get('hash') != self.refresh_root or len(leaf.get('plans', [])) != 1:
            fail('refresh cloud-only scope differs')
        self.active = False
        doc = leaf['plans'][0]
        self.authorize_full_leaf(doc)
        BF.preflight(BF.validate_plan(doc, doc['ack_hash']), doc, self.clock(), backend=self, allow_active=True)

    def reconcile(self, payload):
        """CURRENT reconcile with the per-run FBO verdict: SOURCE_DISAPPEARED is not a STOP here."""
        from tools.tenancy.controller_diagnostics import gate
        doc = payload['plan']; receipt = payload['receipt']
        with gate('RUNTIME_TERMINAL'):
            op = self.request('GET', BF.RUN_API + '/' + receipt['operation'])
            if not op.get('done'): return None
            if op.get('error'): fail('FAILED refresh source needs exact effect investigation; no automatic replay')
        result = BF.reconcile(doc, doc['ack_hash'], receipt, backend=self)
        result['persistence'] = BF.verify_persistence(doc, doc['ack_hash'], backend=self, fbo_run_verdict=True)
        result['source_complete'] = self.state(doc)['complete']
        return result


def budget(a, today):
    """Owner-facing Ozon budget of one MSK day (requests): ceiling and the expected need of the
    schedule at ~25 requests per Heldi FBO day (1 500–2 500 postings, limit 100)."""
    need = sum((date.fromisoformat(u['to']) - date.fromisoformat(u['from'])).days + 1 for u in schedule(a, today) if u['kind'] != 'HISTORY') * 25
    history = sum((date.fromisoformat(u['to']) - date.fromisoformat(u['from'])).days + 1 for u in schedule(a, today) if u['kind'] == 'HISTORY') * 25
    return {'ceiling_requests_per_day': MAX_REQUESTS_PER_DISPATCH * a['max_dispatches_per_day'],
            'refresh_requests_today': need, 'history_requests_once': history}


def publish(b, a):
    """Owner publication only; automatic controllers have no authority to create the marker."""
    if getattr(b, 'current_execution', None) is not None or getattr(b, 'phase_b_ack_sha256', None) != a.get('owner_ack_sha256'):
        fail('explicit owner backend required')
    p = CURRENT.policy(b)
    if p is None: fail('CURRENT policy absent')
    chain = authorities(b, b.manifest, p)
    if a['serial'] != len(chain) + 1: fail('serial differs from the published chain')
    validate(a, b.c, b.manifest, p, b.clock(), previous=chain[-1] if chain else None)
    ds = b.c['datasets']['ref']; n = name(p['hash'], a['serial']); marker = value(a)
    if b.tables.get_table(ds, n) is None: b.tables.create_marker(ds, n, *marker)
    if b.tables.get_table(ds, n) != marker: fail('owner authority CAS conflict')
    return a
