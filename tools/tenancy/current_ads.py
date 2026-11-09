"""Advertising in the daily CURRENT contour, isolated from the Seller entities (Heldi, 2026-11-09).

Why. S4 v1 (2026-10-23) selects new CURRENT work only for catalog/stocks/prices/finance_accrual: an
advertising failure was a FAILED source execution, and a FAILED CURRENT execution is a global STOP that
freezes the whole shop. Owner mandate 09.11: ads load daily, a local ads error never stops the other
entities, ads get their own freshness/error status, a day without data is UNKNOWN (never 0).

What (only with the qualified release flag ``current_ads_version``; without it nothing changes):
  * selection: today's CURRENT also elects ads_campaigns (today's snapshot), ads_expense_daily and
    ads_sku_daily (both D-1) after the four Seller entities; the unchanged owner policy already lists
    them (current_policy.ORDER), so no new policy, plan shape or authority is needed;
  * source protocol: unchanged (one durable intent before the only dispatch, receipts drained first,
    one shared lease, Performance cooldown/async-report fences, the 15-export rolling guard);
  * local failure: a FAILED ads execution is settled locally instead of a global STOP, after the same
    exact provenance checks as a FULL failed read (job, image, identity, env, terminal failedCount=1,
    lease owner). The lease of the terminal execution is released (``LD_``, terminal FAILED_ADS_LOCAL)
    and the shard gets RECONCILED{failed}. Categories:
      - ADS_KNOWN_READ: journal SOURCE_HTTP_429/5xx/NET_ERROR without an open report intent; the same
        plan resumes from its durable state after RETRY_SECONDS (GET + idempotent MERGE), at most
        MAX_ATTEMPTS failed attempts, then ADS_RETRY_EXHAUSTED;
      - ADS_REPORT_AMBIGUOUS: the durable state holds a report INTENT of the failed run, so the POST
        outcome is unknown. The plan is never dispatched again (no second POST, no recovery here);
      - ADS_SOURCE_FAILED: any other or unknown category (data contract, report ERROR, crash).
    A halted entity gets CURRENT_PARTIAL with that reason; the Seller entities, finance catch-up and
    FULL continue. An uncertain DISPATCH (no receipt), a foreign/unattributed execution or lease and
    every authority/duplicate check stay fail-closed (global STOP) exactly as before;
  * status: ``status`` reports COMPLETE / PARTIAL / STALE / UNKNOWN per ads entity from durable records.

Quota (fact, not a choice of this module): one day of SKU statistics needs ~78-88 campaign exports;
the guard allows 15 per rolling 24 h (owner rule 2026-10-29: not raised without a separate Ozon quota
assessment). ads_sku_daily therefore stays PARTIAL every day (≤15 campaigns), never COMPLETE.
"""
from datetime import date, timedelta
import re
from tools.tenancy import tenant_backfill as BF, durable_plan as D, current_contract as K
from tools.tenancy.validation import parse_tenant_json as parse

ENTITIES = ('ads_campaigns', 'ads_expense_daily', 'ads_sku_daily')
RETRY = 'ADS_KNOWN_READ'
REASONS = frozenset({'ADS_SOURCE_FAILED', 'ADS_REPORT_AMBIGUOUS', 'ADS_RETRY_EXHAUSTED'})
KNOWN_READ = re.compile(r"EvidenceError\(['\"]SOURCE_HTTP_(429|500|502|503|504|NET_ERROR): /[A-Za-z0-9_./-]+['\"]\)")
MAX_ATTEMPTS = 3
RETRY_SECONDS = 600
STALE_DAYS = 2


def fail(s): raise BF.B.EvidenceError('current ads: ' + s)


def release(b):
    from tools.tenancy import cloud_controller as C
    env = b.c['orchestration']['job']['env']
    spec = b.tables.get_table(b.c['datasets']['tenant_locks'], C.descriptor_name(env['CONTROLLER_SOURCE_SHA'], b.manifest['hash'], env['HISTORICAL_SCHEDULER_STATE']))
    return parse(spec[1])['release'] if spec else {}


def qualified(b):
    """The installed release explicitly carries the qualified ads-in-CURRENT adapter (one read per object)."""
    cached = getattr(b, '_current_ads_qualified', None)
    if cached is not None: return cached
    r = release(b)
    out = r.get('current_ads_version') == 1 and r.get('verification', {}).get('current_ads_adapter') == 'PASS'
    try: b._current_ads_qualified = out
    except AttributeError: pass
    return out


def local(b, doc):
    """A FAILED execution of this plan is settled as an ads-local outcome (not a global STOP)."""
    return doc['runtime_plan']['entity'] in ENTITIES and qualified(b)


def selectable(b, independent):
    return tuple(independent) + (ENTITIES if qualified(b) else ())


def open_intent(history):
    intents = [r['sequence'] for r in history if r['kind'] == 'DISPATCH_INTENT']
    return bool(intents) and not any(r['kind'] == 'RECONCILED' and r['sequence'] == max(intents) for r in history)


def failure_gate(history, now):
    """('HALT', reason) | ('WAIT', eligible_at) | None from the shard's own RECONCILED records."""
    done = [r for r in history if r['kind'] == 'RECONCILED']
    if not done: return None
    last = max(done, key=lambda r: r['sequence'])['payload']
    if not last.get('failed'): return None
    category = last.get('retry_category')
    if category in REASONS: return ('HALT', category)
    if category != RETRY: fail('unknown ads-local failure category')
    if sum(1 for r in done if r['payload'].get('failed')) >= MAX_ATTEMPTS: return ('HALT', 'ADS_RETRY_EXHAUSTED')
    if K.at(last['eligible_at']) > now: return ('WAIT', last['eligible_at'])
    return None


LOOKBACK_DAYS = 2


def live(b, item, cycle, today, now):
    """Can this CURRENT ads plan still be dispatched/polled by CURRENT? (flagged path only)

    CURRENT dispatches only today's plans; it drains every open intent and continues an async report scope.
    A plan with an open intent is live. A halted plan is dead. A plan never dispatched holds no state. A
    report scope is followed only within LOOKBACK_DAYS cycles: an older unfinished report is abandoned
    (its day stays PARTIAL/UNKNOWN; its reservation stays in the journal's 24 h accounting). This bounds the
    per-wake state reads to the recent cycles instead of every cycle since the policy began.
    """
    hs = b.store.history(item['shard'])
    if open_intent(hs): return True
    if halted(hs, now) or not any(r['kind'] == 'DISPATCH_INTENT' for r in hs): return False
    if (today - cycle).days > LOOKBACK_DAYS or item['plan']['runtime_plan']['entity'] != 'ads_sku_daily': return False
    return bool(b.state(item['plan'])['progress'].get('report'))


def halted(history, now):
    gate = failure_gate(history, now)
    return gate is not None and gate[0] == 'HALT'


def settle_failed(b, doc, receipt, operation):
    """Exact terminal FAILED ads execution -> RECONCILED{failed} payload; the lease is released.

    Never a source call. Provenance, terminal state and lease attribution are verified exactly as for a
    FULL failed read; any mismatch raises (global STOP). Only the business outcome is local.
    """
    p = doc['runtime_plan']
    if p['entity'] not in ENTITIES: fail('not an advertising plan')
    b.authorize_full_leaf(doc)
    name = operation.get('metadata', {}).get('name', '')
    base, jobs = BF.resources(b.c)
    job = next(n for n, j in jobs.items() if p['entity'] in j['entities'])
    if not name.startswith(base + '/jobs/' + job + '/executions/'): fail('failed runtime provenance absent')
    execution = b.request('GET', BF.RUN_API + '/' + name)
    t = execution['template']; container = t['containers'][0]
    env = {v['name']: v.get('value') for v in container.get('env', [])}
    expected = {'INGESTION_RUN_ID': receipt['run_id'], 'BACKFILL_TARGET_PROJECT': p['project'],
        'ENTITIES': p['entity'], 'SINCE': p['from'], 'UNTIL': p['to'], 'BACKFILL_MODE': BF.B.VERSION,
        'BACKFILL_GENERATION': p['generation'], 'BACKFILL_ORIGIN': p['origin'],
        'TENANT_BINDING_REQUIRED': '1', 'STRICT_PAGE_CAPS': '1',
        'BACKFILL_MAX_REQUESTS': str(doc['max_requests']), 'BACKFILL_MAX_UNITS': str(doc['max_units'])}
    if p['window_days'] != 1: expected['BACKFILL_WINDOW_DAYS'] = str(p['window_days'])
    expected.update(BF.continuation_overrides(doc))
    if (len(t['containers']) != 1 or execution.get('taskCount', 1) != 1
            or len(env) != len(container.get('env', [])) or any(env.get(k) != v for k, v in expected.items())
            or not execution.get('completionTime') or execution.get('failedCount') != 1
            or execution.get('succeededCount', 0) or execution.get('cancelledCount', 0)
            or t['serviceAccount'] != f"{b.c['marketplaces']['ozon']['service_accounts']['runtime']}@{p['project']}.iam.gserviceaccount.com"
            or container['image'] != BF.execution_image(doc, b.c)):
        fail('failed ads runtime exact provenance unproven')
    cid = BF.B.digest(['BOUNDED_PILOT_EXCLUSIVE', b.c['project_id']])[:16]
    lease = b.tables.get_table(b.c['datasets']['tenant_locks'], BF.CK.lease_name(cid, receipt['lease_generation']))
    if not lease or lease[0].get('owner') != receipt['run_id'] or parse(lease[1])['ack_hash'] != doc['ack_hash']:
        fail('failed ads terminal lease attribution differs')
    state = b.state(doc)
    rows = b.select(f"SELECT DISTINCT error_message FROM `{b.journal}` WHERE ingestion_run_id=@run AND entity=@entity AND status='FAILED' LIMIT 2",
                    {'run': ('STRING', receipt['run_id']), 'entity': ('STRING', p['entity'])})
    message = (rows[0]['error_message'] or '') if len(rows) == 1 else ''
    report = state['progress'].get('report') or {}
    if report.get('phase') == 'INTENT':
        category = 'ADS_REPORT_AMBIGUOUS'      # POST outcome unknown: this plan never posts again
    elif len(rows) == 1 and KNOWN_READ.fullmatch(message):
        category = RETRY
    else:
        category = 'ADS_SOURCE_FAILED'
    now = b.clock()
    row = BF.checkpoint(doc, receipt['run_id'], 'FAILED', receipt['lease_generation'], now)
    row['error_code'] = category
    row['evidence_json'] = D.encoded({'mode': 'BOUNDED_PILOT', 'source_state_hash': D.digest(state),
                                      'failed_operation': receipt['operation'], 'retry_category': category})
    b.tables.append(b.c['datasets']['tenant_ops'], 'BACKFILL_CHECKPOINTS', [row])
    marker = f"LD_{cid}_{receipt['lease_generation']:04d}"
    desc = D.encoded({'operation': receipt['operation'], 'ack_hash': doc['ack_hash'], 'terminal': 'FAILED_ADS_LOCAL'})
    if not b.tables.create_marker(b.c['datasets']['tenant_locks'], marker, {'owner': receipt['run_id']}, desc):
        if b.tables.get_table(b.c['datasets']['tenant_locks'], marker) != ({'owner': receipt['run_id']}, desc):
            fail('failed ads lease release conflict')
    out = {'failed': True, 'checkpoint': 'FAILED', 'source_complete': False, 'persisted_reconciled': False,
           'state_hash': D.digest(state), 'retry_category': category}
    if category == RETRY: out['eligible_at'] = (now + timedelta(seconds=RETRY_SECONDS)).isoformat()
    return out


def partial_payload(p, doc, state, reason):
    return {'entity': doc['runtime_plan']['entity'], 'policy_hash': p['hash'], 'ack_hash': doc['ack_hash'],
            'state_hash': D.digest(state), 'status': 'PARTIAL', 'reason': reason}


def partial_reasons(entity):
    return {'SOURCE_RETENTION_COMPLETENESS_UNPROVEN'} | (REASONS if entity in ENTITIES else set())


def status(b, p, today=None):
    """Read-only per-entity ads status from durable CURRENT records (and owner catch-up certificates).

    business_day is D-1 for the dated entities and today's snapshot day for ads_campaigns. A day without
    a certificate is never zero: day_status UNKNOWN (nothing observed / no data proven) or PARTIAL (some
    progress or a local error); status is day_status, or STALE when the business day is not COMPLETE and
    the last COMPLETE business day is older than STALE_DAYS (or absent).
    """
    from tools.tenancy import current_controller as CURRENT, current_policy as P, ads_catchup as AC
    today = today or b.clock().astimezone(K.MSK).date()
    cycles = CURRENT.cycles(b, p)
    complete = {e: [] for e in ENTITIES}; out = {}
    for text in cycles:
        records, items = CURRENT.plans(b, p, date.fromisoformat(text))
        for r in records:
            e = r['payload'].get('fact', {}).get('entity') if r['kind'] == 'CURRENT_CERT' else None
            if e in ENTITIES: complete[e].append(items[e]['plan']['runtime_plan'].get('observation_date') or items[e]['plan']['runtime_plan']['from'])
    for e, days in AC.complete_days(b, p).items(): complete[e] += days
    records, items = CURRENT.plans(b, p, today) if today.isoformat() in cycles else ([], {})
    partials = {r['payload']['entity']: r['payload']['reason'] for r in records if r['kind'] == 'CURRENT_PARTIAL'}
    for e in ENTITIES:
        business = today if e in K.SNAPSHOTS else today - timedelta(days=1)
        last = max(complete[e], default=None)
        v = {'business_day': business.isoformat(), 'last_complete_day': last, 'reason': None, 'progress': None}
        item = items.get(e)
        if last == business.isoformat(): v['status'] = 'COMPLETE'
        elif e in partials:
            v['reason'] = partials[e]
            v['status'] = 'UNKNOWN' if partials[e] == 'SOURCE_RETENTION_COMPLETENESS_UNPROVEN' else 'PARTIAL'
        elif item is None: v['status'] = 'UNKNOWN'; v['reason'] = 'NOT_ELECTED'
        else:
            state = b.state(item['plan']); pr = state['progress']
            gate = failure_gate(b.store.history(item['shard']), b.clock())
            if gate: v['reason'] = gate[1] if gate[0] == 'HALT' else 'ADS_READ_RETRY_COOLDOWN'
            if e == 'ads_sku_daily' and 'pending' in pr:
                v['progress'] = {'pending_campaigns': len(pr['pending'])}
            v['status'] = 'PARTIAL' if state['sequence'] > 0 else 'UNKNOWN'
        v['day_status'] = v.pop('status')
        stale = last is None or date.fromisoformat(last) < business - timedelta(days=STALE_DAYS)
        v['status'] = 'STALE' if v['day_status'] != 'COMPLETE' and stale else v['day_status']
        out[e] = v
    return out
