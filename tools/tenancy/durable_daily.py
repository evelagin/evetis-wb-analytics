"""A2 «Durable Daily» (2026-11-09): a transient read never stops the shop; a local problem stays local.

Four STOPs in three weeks (8ad33cc6, aaf68ede, bf7d1370, eca0335a) came from transport/metadata reads,
not from data. This module holds the closed rules that keep such reads a typed retry and keep an
entity- or leaf-local evidence problem from freezing every other entity. It adds no writer, no record
kind, no source call and no recovery authority:

  * CONTROL_CLOSURE: a TransientReadError while closing this wake's own claim is retried inside the wake
    with bounded backoff (never past the wake budget). The first closure of the exact claim is
    canonical; a lost readback is resolved by readback only, never by a second create POST;
  * CURRENT: an evidence error while certifying an entity whose shard is fully settled (every intent
    receipted and reconciled, lease released by reconciliation) holds only that entity for this wake
    (status UNKNOWN); the other entities proceed. Any open authority keeps the global STOP;
  * FULL leaf: an evidence error while proving completion of an already reconciled leaf becomes the
    existing leaf-local typed class (PERSISTED_SOURCE_RECONCILIATION with the leaf context), which S4
    `current_independence` already admits; up to MAX_LOCAL_FULL_LEAVES such leaves are skipped and the
    FULL queue continues, beyond that the queue stays closed (systemic, not local);
  * RECENT_90: leaves intersecting the rolling last-90-days window run before older history; the
    existing order is preserved inside each tier (in-flight async scopes and dependencies first).

Never relaxed: an unknown source POST, an intent without receipt, an unknown write in the ledger, a
held lease without its receipted owner, a claim/closure conflict, identity/binding/deployment drift.
"""
import time
from datetime import timedelta

from tools.tenancy import durable_plan as D, tenant_backfill as BF

VERSION = 1
CLOSE_RETRY_DELAYS = (5, 15, 30)       # seconds between closure attempts of one wake (bounded backoff)
MAX_LOCAL_FULL_LEAVES = 3              # more unresolved leaf-local STOPs = systemic: FULL queue closed
RECENT_DAYS = 90                       # owner mandate 2026-11-09: 11.08.2026 → today, rolling
TRANSIENT_QUERY_REASONS = frozenset({'backendError', 'internalError', 'rateLimitExceeded',
                                     'jobBackendError', 'jobInternalError', 'jobRateLimitExceeded'})
LOCAL_ERRORS = (BF.B.EvidenceError, ValueError, KeyError, TypeError, IndexError)


def sleep(seconds):
    time.sleep(seconds)


def local_evidence(error):
    """True only for a plain entity/leaf evidence failure. Typed gate failures, the typed waits/pauses
    (OverlapWait, SourceDispatchPaused), a held pilot lease and every transport error keep their path."""
    from tools.tenancy.controller_diagnostics import GateFailure, PILOT_HELD
    from tools.tenancy.cloud_tick import SourceDispatchPaused, OverlapWait
    if not isinstance(error, LOCAL_ERRORS) or isinstance(error, (GateFailure, SourceDispatchPaused, OverlapWait)):
        return False
    return str(error) != PILOT_HELD


def transient(error):
    from tools.tenancy.cloud_access import TransientReadError
    return isinstance(error, TransientReadError)


def retry_transient(attempt, *, budget=None, pause=None, activity=None, delays=CLOSE_RETRY_DELAYS):
    """Run attempt(); retry ONLY a TransientReadError, with bounded backoff inside the wake budget.

    Any other failure propagates unchanged on the first occurrence. The last transient error
    propagates when the delays are exhausted or the next attempt no longer fits the budget.
    """
    pause = pause or sleep
    for delay in (0,) + tuple(delays):
        if delay:
            if budget is not None and not budget.retry_allowed(delay):
                raise last
            pause(delay)
            if activity is not None:
                activity['closure_read_retries'] = activity.get('closure_read_retries', 0) + 1
        try:
            return attempt()
        except Exception as error:  # noqa: BLE001 — only the typed transient read is retried
            if not transient(error):
                raise
            last = error
    raise last


def query_failure(result):
    """Classify an unfinished/failed BigQuery SELECT: a typed transient read or an evidence error."""
    from tools.tenancy.cloud_access import TransientReadError
    if not result.get('jobComplete'):
        return TransientReadError('bounded verification query not complete')
    errors = result.get('errors')
    if errors:
        reasons = {e.get('reason') for e in errors if isinstance(e, dict)}
        if len(reasons) == len(errors) and reasons and reasons <= TRANSIENT_QUERY_REASONS:
            return TransientReadError('bounded verification query transient failure')
    return BF.B.EvidenceError('bounded verification query incomplete/failed')


def settled(history):
    """(sequence, intent, receipt) of the last reconciled dispatch when EVERY intent is receipted and
    reconciled (no open source authority in this shard), else None."""
    seq = {k: {r['sequence'] for r in history if r['kind'] == k}
           for k in ('DISPATCH_INTENT', 'DISPATCH_RECEIPT', 'RECONCILED')}
    if not seq['DISPATCH_INTENT'] or seq['DISPATCH_INTENT'] != seq['DISPATCH_RECEIPT'] or seq['DISPATCH_RECEIPT'] != seq['RECONCILED']:
        return None
    last = max(seq['RECONCILED'])
    intents = [r for r in history if r['kind'] == 'DISPATCH_INTENT' and r['sequence'] == last]
    receipts = [r for r in history if r['kind'] == 'DISPATCH_RECEIPT' and r['sequence'] == last]
    if len(intents) != 1 or len(receipts) != 1:
        return None
    return last, intents[0], receipts[0]


def local_leaf_failure(history, index, shard):
    """The existing leaf-local typed STOP class for a completion-proof failure, or None (stay global).

    Only for a FULL leaf whose shard is fully settled: the diagnostic carries the exact leaf context
    that S4 `current_independence.classify` admits (stage PERSISTED_SOURCE_RECONCILIATION, no write).
    """
    from tools.tenancy.controller_diagnostics import GateFailure
    proof = settled([r for r in history if r.get('root_hash', shard) == shard])
    if proof is None or type(index) is not int:
        return None
    sequence, intent, receipt = proof
    failure = GateFailure('PERSISTED_SOURCE_RECONCILIATION', 'EVIDENCE')
    failure.diagnostic['context'] = {'index': index, 'shard': shard, 'sequence': sequence,
                                     'intent_hash': D.digest(intent), 'receipt_hash': D.digest(receipt)}
    return failure


def hold_current(activity, entity, history, day, *, today):
    """True when this CURRENT entity may be held for the wake instead of a global STOP.

    The entity's shard must be fully settled; the hold is recorded only in the wake result (no
    durable record), so the next wake re-evaluates it from the same immutable evidence.
    """
    if settled(history) is None:
        return False
    if today:
        activity.setdefault('current_entities', {})[entity] = 'UNKNOWN'
    holds = activity.setdefault('current_holds', [])
    if {'entity': entity, 'day': day} not in holds:
        holds.append({'entity': entity, 'day': day})
    return True


def entity_status(activity, entities, certified, partial):
    """Per-entity CURRENT status of the operational day (mandate vocabulary).

    COMPLETE: certified today; PARTIAL: explicit retention gap; UNKNOWN: held on local evidence;
    STALE: not (yet) certified today — the last accepted day is older. Absent is never zero.
    """
    current = activity.setdefault('current_entities', {})
    for entity in entities:
        if current.get(entity) == 'UNKNOWN':
            continue
        current[entity] = 'COMPLETE' if entity in certified else ('PARTIAL' if entity in partial else 'STALE')
    return current


def window_start(today, days=RECENT_DAYS):
    """Owner wording: on 09.11.2026 the window is 11.08.2026 → today (90 closed days before today)."""
    return today - timedelta(days=days)


def recent_first(manifest, order, today, days=RECENT_DAYS):
    """Stable partition: DATED leaves intersecting [today-days, today] first, everything else after.

    The relative order inside each tier is the existing order (recent-priority selection, closure
    siblings, then the historical rank), so old history keeps its low-priority place.
    """
    start = str(window_start(today, days))
    def recent(i):
        p = manifest['programs'][i]
        return p.get('kind') == 'DATED' and p['to'] >= start and p['from'] <= str(today)
    return [i for i in order if recent(i)] + [i for i in order if not recent(i)]


def full_continues(admission):
    """Admitted S4 wake: the leaf-local STOPs to skip, or None when the FULL queue stays closed."""
    if not isinstance(admission, dict) or admission.get('admitted') is not True:
        return None
    local = admission.get('local_full_leaves')
    if not isinstance(local, list) or not local or len(local) > MAX_LOCAL_FULL_LEAVES:
        return None
    if any(type(i) is not int for i in local):
        return None
    return set(local)

