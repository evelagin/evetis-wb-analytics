"""Pure FULL_HISTORY program contract. Code/manifest existence is never GO.

Date programs link to the canonical T5 chunks. Each bounded source leaf has a
separate durable shard; a root does not accumulate every report/page receipt.
Snapshot observation time is actual execution time, never historical SINCE.
No credentials, cloud transport, source calls or publication occur here.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
import re
from tools.tenancy import tenant_backfill as BF

VERSION = 'FULL_HISTORY_V1'
DOMAINS = tuple(sorted(BF.B.DOMAINS))
SNAPSHOTS = frozenset(BF.B.DOMAINS - BF.B.DATED - {'supplies'})
MAX_MANIFEST_BYTES = 900000
MAX_LEAF_DAYS = 31
MAX_LEAF_RECORDS = 9000
GATES = frozenset({
    'seller_bound', 'performance_bound', 'credentials', 'sku90',
    'stocks_probe', 'supplies_probe', 'fbo_split', 'finance_resume',
    'supplies_scale', 'state_size', 'cloud_restart', 'reconciliation',
    'tenant_isolation', 'runtime_security', 'exact_images',
})
FINANCE = {
    'source_completeness': 'AS_OF_OBSERVATION',
    'economic_finality': 'PROVISIONAL',
    'rolling_refresh_days': 30,
    'older_corrections': 'EXPLICIT_OWNER_REOPEN',
    'type_84_source_label': 'RETAINED',
    'type_84_pnl_mapping': 'UNKNOWN',
}
LIMITATIONS = {
    'catalog': 'CURRENT_ALL_AND_ARCHIVED_NOT_HISTORICAL_STATUS',
    'supplies': 'RETAINED_INVENTORY_NOT_DATED_HISTORY',
    'ads_campaigns': 'CURRENT_COHORT_NOT_HISTORICAL_CAMPAIGN_STATUS',
    'ads_expense_daily': 'API_RETENTION_EMPTY_IS_NOT_PROVEN_ABSENCE',
    'ads_sku_daily': 'BOUNDARY_DERIVED_FROM_EXPENSE_EMPTY_RETENTION_UNPROVEN',
    'finance_accrual': 'SAMPLED_LOWER_BOUND_NOT_PROVEN_EARLIEST_ACTIVITY',
    'prices': 'CURRENT_ONLY_NO_HISTORICAL_PRICES',
    'stocks': 'CURRENT_ONLY_NO_HISTORICAL_STOCKS',
    'seller_info': 'CURRENT_ONLY',
    'clusters': 'CURRENT_ONLY',
}


def stamp(value):
    try:
        at = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if at.utcoffset() != timedelta(0):
            raise ValueError
        return at
    except (AttributeError, TypeError, ValueError):
        raise BF.B.EvidenceError('full program requires exact UTC timestamp') from None


def leaf_windows(start, end):
    """Split one T5 interval without gaps/overlap; preserve T5 parent identity."""
    cur = start
    while cur <= end:
        stop = min(cur + timedelta(days=MAX_LEAF_DAYS - 1), end)
        yield str(cur), str(stop)
        cur = stop + timedelta(days=1)


def programs(chunks, cutover, starts):
    """A canonical T5 hash plus eleven explicit domain programs, no business guesses."""
    if set(starts) != BF.B.DATED or any(not isinstance(v, date) for v in starts.values()):
        raise BF.B.EvidenceError('exact confirmed starts required for every dated domain')
    if not chunks or len({c.chunk_id for c in chunks}) != len(chunks):
        raise BF.B.EvidenceError('nonempty unique canonical T5 chunks required')
    by = {e: [] for e in BF.B.DATED}
    for c in sorted(chunks, key=lambda x: (x.domain, x.start)):
        if c.domain not in by or c.start > c.end or c.end > cutover:
            raise BF.B.EvidenceError('invalid full date chunk')
        by[c.domain].append(c)
    if any(not rows for rows in by.values()):
        raise BF.B.EvidenceError('all dated domain boundaries must be proven')
    out = []
    for entity in DOMAINS:
        if entity in by:
            rows = by[entity]
            cursor = starts[entity]
            for c in rows:
                if c.start != cursor:
                    raise BF.B.EvidenceError('canonical date coverage gap/overlap')
                cursor = c.end + timedelta(days=1)
            if cursor != cutover + timedelta(days=1):
                raise BF.B.EvidenceError('canonical date coverage does not reach cutover')
            for c in rows:
                for frm, to in leaf_windows(c.start, c.end):
                    ranges=[(frm,to)]
                    accepted=BF.QF.accepted_doc(BF.QF.SKU)['runtime_plan']['from']
                    if entity=='ads_sku_daily' and frm<=accepted<=to:
                        ranges=[]
                        if frm<accepted:ranges.append((frm,str(date.fromisoformat(accepted)-timedelta(days=1))))
                        ranges.append((accepted,accepted))
                        if accepted<to:ranges.append((str(date.fromisoformat(accepted)+timedelta(days=1)),to))
                    for a,b in ranges:
                        program={'entity': entity, 'from': a, 'to': b,
                                't5_chunk_id': c.chunk_id,
                                't5_from': str(c.start), 't5_to': str(c.end),
                                'kind': 'DATED', 'window_days': 7 if entity == 'fbo_postings' else 1}
                        if entity=='ads_sku_daily' and a==b==accepted:
                            program['accepted_qualification_plan']=BF.QF.SKU
                        out.append(program)
        else:
            out.append({'entity': entity, 'kind': 'RETAINED_INVENTORY' if entity == 'supplies' else 'CURRENT_SNAPSHOT',
                        't5_chunk_id': None, 'observation': 'ACTUAL_MOSCOW_EXECUTION_DAY'})
    return out


def make_manifest(*, tenant, created_at, cutover, chunks, runtime_source, runtime_image,
                  controller_source, controller_image, controller_implementation,
                  boundary_evidence, starts, qualification_evidence, retained_supplies):
    c = BF.target(tenant)
    at = stamp(created_at)
    end = date.fromisoformat(cutover)
    if str(end) != cutover or end >= at.astimezone(BF.B.MSK).date():
        raise BF.B.EvidenceError('full cutover must be a finished Moscow day')
    for source in (runtime_source, controller_source):
        if not isinstance(source, str) or not re.fullmatch(r'[0-9a-f]{40}', source):
            raise BF.B.EvidenceError('immutable full source SHA required')
    for image, name in ((runtime_image, 'ozon-runtime'), (controller_image, 'tenant-backfill-controller')):
        if not re.fullmatch(r'europe-west1-docker\.pkg\.dev/mpa-platform/mpa-runtime/'+name+r'@sha256:[0-9a-f]{64}', image):
            raise BF.B.EvidenceError('immutable full image required')
    if set(boundary_evidence) != BF.B.DATED:
        raise BF.B.EvidenceError('all exact boundary evidence references required')
    for h in [controller_implementation, *boundary_evidence.values(), *qualification_evidence.values()]:
        if not isinstance(h, str) or not re.fullmatch(r'[0-9a-f]{64}', h):
            raise BF.B.EvidenceError('full evidence must be immutable hash references')
    if set(qualification_evidence) != GATES:
        raise BF.B.EvidenceError('full qualification evidence set differs')
    if not BF.QF.matches(retained_supplies) or retained_supplies['runtime_plan']['entity'] != 'supplies':
        raise BF.B.EvidenceError('retained Supplies identity must remain exact')
    out = {
        'version': VERSION, 'purpose': 'FULL_HISTORY', 'tenant': tenant, 'project': c['project_id'],
        'created_at': created_at, 'cutover': cutover,
        'runtime_source_sha': runtime_source, 'runtime_image': runtime_image,
        'runtime_implementation_hash': BF.B.implementation_hash(),
        'controller_source_sha': controller_source, 'controller_image': controller_image,
        'controller_implementation_hash': controller_implementation,
        't5_plan_hash': BF.CK.plan_hash(chunks), 'programs': programs(chunks, end, starts), 'starts': {k: str(v) for k, v in starts.items()},
        'boundaries': boundary_evidence, 'qualification_evidence': qualification_evidence,
        'retained_supplies': retained_supplies,
        'finance': dict(FINANCE), 'limitations': dict(LIMITATIONS),
        'continuation': {
            'max_leaf_days': MAX_LEAF_DAYS, 'max_leaf_records': MAX_LEAF_RECORDS,
            'max_state_bytes': MAX_MANIFEST_BYTES, 'max_source_dispatches_per_wake': 1,
            'performance_rolling_guard': 15, 'ambiguous_post': 'STOP_NO_REPEAT',
            'known_source_failure': 'PRESERVE_AND_BOUNDED_RETRY_MAX_5',
            'fbo': 'HALF_OPEN_ADAPTIVE_SPLIT_MIN_60_SECONDS',
            'snapshots': 'NO_HISTORICAL_FABRICATION',
            'snapshot_rollover': 'PRESERVE_TERMINAL_PARTIAL_NEW_IMMUTABLE_DATED_SCOPE',
            'catalog_linkage': 'DAILY_CERTIFIED_ALL_ARCHIVED_DATED_DEPENDENCY',
            'regular_schedulers': 'PAUSED',
            'trial': 'PRESERVE_MERGE_CONVERGENCE_WITH_PROVENANCE',
            'sku90': 'REUSE_EXACT_ACCEPTED_DAY_NO_SOURCE_RERUN',
        },
    }
    out['hash'] = BF.B.digest(out)
    if len(__import__('json').dumps(out, sort_keys=True, separators=(',', ':')).encode()) > MAX_MANIFEST_BYTES:
        raise BF.B.EvidenceError('full manifest size guard; shard before publication')
    return out


def verify_gate_results(results, evidence_hashes):
    if set(results) != GATES or set(evidence_hashes) != GATES:
        raise BF.B.EvidenceError('full GO evidence incomplete')
    if any(results[k] != 'PASS' for k in GATES):
        raise BF.B.EvidenceError('FULL_HISTORY_GO_UNPROVEN')
    if any(not isinstance(h, str) or not re.fullmatch(r'[0-9a-f]{64}', h) for h in evidence_hashes.values()):
        raise BF.B.EvidenceError('GO must reference exact retained evidence')
    # Caller must independently read live gates and verify retained evidence.
    # This pure structural verifier is not a cloud activation operation.
    return {'verdict': 'PASS', 'evidence_hashes': dict(evidence_hashes)}


def canonical_chunks(manifest):
    parents = {}
    for p in manifest['programs']:
        if p['kind'] != 'DATED':
            continue
        chunk = BF.CK.Chunk(p['entity'], date.fromisoformat(p['t5_from']), date.fromisoformat(p['t5_to']))
        if chunk.chunk_id != p['t5_chunk_id']:
            raise BF.B.EvidenceError('full program T5 parent identity differs')
        old = parents.setdefault(chunk.chunk_id, chunk)
        if old != chunk:
            raise BF.B.EvidenceError('conflicting full parent scope')
    chunks = sorted(parents.values(), key=lambda c: (c.domain, c.start))
    if BF.CK.plan_hash(chunks) != manifest['t5_plan_hash']:
        raise BF.B.EvidenceError('full program/canonical T5 plan hash differs')
    return chunks


def validate_manifest(manifest):
    if not isinstance(manifest, dict) or manifest.get('version') != VERSION:
        raise BF.B.EvidenceError('unsupported full program schema')
    rebuilt = make_manifest(
        tenant=manifest['tenant'], created_at=manifest['created_at'], cutover=manifest['cutover'],
        chunks=canonical_chunks(manifest), runtime_source=manifest['runtime_source_sha'],
        runtime_image=manifest['runtime_image'], controller_source=manifest['controller_source_sha'],
        controller_image=manifest['controller_image'], controller_implementation=manifest['controller_implementation_hash'],
        boundary_evidence=manifest['boundaries'], starts={k: date.fromisoformat(v) for k,v in manifest['starts'].items()},
        qualification_evidence=manifest['qualification_evidence'], retained_supplies=manifest['retained_supplies'])
    if manifest != rebuilt:
        raise BF.B.EvidenceError('full immutable manifest content/hash differs')
    return BF.target(manifest['tenant'])


def go_marker(manifest):
    validate_manifest(manifest)
    return 'BFGO_' + manifest['hash']


def go_value(manifest, results):
    proof = verify_gate_results(results, manifest['qualification_evidence'])
    value = {
        'version': VERSION, 'root_hash': manifest['hash'], 'tenant': manifest['tenant'],
        'project': manifest['project'], 't5_plan_hash': manifest['t5_plan_hash'],
        'runtime_image': manifest['runtime_image'], 'controller_image': manifest['controller_image'],
        'proof': proof,
    }
    import json
    text = json.dumps(value, sort_keys=True, separators=(',', ':'))
    if len(text.encode()) > 16384:
        raise BF.B.EvidenceError('owner GO metadata guard')
    return {'kind': 'full_history_go', 'root': manifest['hash'][:16]}, text


def shard_root(manifest, index, doc):
    if type(index) is not int or not 0 <= index < len(manifest['programs']):
        raise BF.B.EvidenceError('full leaf program index invalid')
    return BF.B.digest({'full_root': manifest['hash'], 'index': index, 'ack_hash': doc['ack_hash']})


def render_leaf(manifest, index, day):
    validate_manifest(manifest)
    if type(index) is not int or not 0 <= index < len(manifest['programs']):
        raise BF.B.EvidenceError('full program leaf index invalid')
    p = manifest['programs'][index]
    if p.get('accepted_qualification_plan')==BF.QF.SKU:
        return BF.QF.accepted_doc(BF.QF.SKU)
    if p['entity'] == 'supplies':
        return manifest['retained_supplies']
    if p['kind'] == 'DATED':
        frm, to = p['from'], p['to']
    else:
        frm = to = str(day - timedelta(days=1))
    return BF.make_plan(manifest['tenant'], p['entity'], frm, to,
                        'full-' + manifest['hash'][:24] + '-' + str(index), manifest['created_at'],
                        today=day, max_units=20, window_days=p.get('window_days', 1))


def validate_leaf(manifest, index, doc, today):
    observed = doc['runtime_plan'].get('observation_date')
    day = date.fromisoformat(observed) if observed else today
    if day > today or day < stamp(manifest['created_at']).astimezone(BF.B.MSK).date():
        raise BF.B.EvidenceError('full source observation date outside approved program')
    if doc != render_leaf(manifest, index, day):
        raise BF.B.EvidenceError('full source leaf differs from exact immutable program')
    return BF.validate_plan(doc, doc['ack_hash'])
