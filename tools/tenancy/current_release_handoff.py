"""Owner-only controller handoff retaining the original current namespace.

No new current policy, source plan or runtime authority is created here. Only
an exact immutable owner marker may associate an unchanged original policy
with a qualified replacement controller paired to the unchanged runtime.
"""
from copy import deepcopy
import re
from tools.tenancy import current_policy as P, durable_plan as D, tenant_backfill as BF
from tools.tenancy.validation import parse_tenant_json as parse

TYPE = 'OWNER_CURRENT_RELEASE_HANDOFF_V1'
FIELDS = frozenset({'version', 'type', 'tenant', 'project', 'root', 'policy_hash',
    'old_source', 'old_image', 'new_source', 'new_image', 'new_implementation_hash',
    'runtime_image', 'runtime_implementation_hash', 'owner_ack_sha256',
    'qualification_hash', 'security_hash', 'created_at', 'source_budget', 'hash'})


def fail(s): raise BF.B.EvidenceError('current release handoff: ' + s)
def name(source): return 'BFCURHANDOFF_' + P.name(source).removeprefix('BFCURRENT_')
def value(p): return {'kind': 'current_release_handoff', 'root': p['root'][:16]}, D.encoded(p)


def resolve(b, source, marker, release):
    """Read an exact owner association; never silently re-elect current sources."""
    if marker is None: return None
    p = parse(marker[1])
    if (not isinstance(p, dict) or set(p) != FIELDS or type(p['version']) is not int
            or p['version'] != 1 or p['type'] != TYPE
            or p['hash'] != D.digest({k: v for k, v in p.items() if k != 'hash'})
            or type(p['source_budget']) is not int or p['source_budget'] != 0
            or marker != value(p)):
        fail('closed source-free owner association required')
    for k in ('root', 'policy_hash', 'new_implementation_hash', 'runtime_implementation_hash',
              'owner_ack_sha256', 'qualification_hash', 'security_hash', 'hash'):
        D.check_hash(p[k])
    if any(not isinstance(p[k], str) or not re.fullmatch('[0-9a-f]{40}', p[k])
           for k in ('old_source', 'new_source')) or p['old_source'] == p['new_source']:
        fail('distinct immutable releases required')
    env = b.c['orchestration']['job']['env']
    if ((p['tenant'], p['project'], p['root'], p['new_source'], p['new_image']) !=
            (b.c['tenant_id'], b.c['project_id'], b.manifest['hash'], source,
             b.c['orchestration']['job']['image'])
            or (p['tenant'], p['project']) != ('client_001', 'mpa-t-client-001')
            or env['CONTROLLER_SOURCE_SHA'] != source):
        fail('tenant/root/replacement differs')
    from tools.tenancy import orchestration_contract as O
    O.verify_artifact_source(release, BF.REPO)
    if ((release['source_sha'], release['image'], release['controller_implementation_hash'],
         release['runtime_image'], release['runtime_implementation_hash']) !=
            (p['new_source'], p['new_image'], p['new_implementation_hash'],
             p['runtime_image'], p['runtime_implementation_hash'])
            or release.get('current_data_version') != 1
            or release.get('verification', {}).get('current_data_adapter') != 'PASS'
            or p['runtime_image'] != b.c['marketplaces']['ozon']['runtime_image']):
        fail('replacement qualification/runtime differs')
    old = parse((BF.REPO / 'infra/tenant/releases/backfill' / (p['old_source'] + '.json')).read_text())
    if ((old['source_sha'], old['image'], old['runtime_image'], old['runtime_implementation_hash']) !=
            (p['old_source'], p['old_image'], p['runtime_image'], p['runtime_implementation_hash'])
            or old.get('current_data_version') != 1
            or old.get('verification', {}).get('current_data_adapter') != 'PASS'):
        fail('original registered pair differs')
    old_marker = b.tables.get_table(b.c['datasets']['ref'], P.name(p['old_source']))
    original = parse(old_marker[1]) if old_marker else {}
    # Historical *read* context only; neither live c nor original policy changes.
    context = deepcopy(b.c)
    context['orchestration']['job']['image'] = p['old_image']
    context['orchestration']['job']['env']['CONTROLLER_SOURCE_SHA'] = p['old_source']
    if 'CONTROLLER_IMAGE' in context['orchestration']['job']['env']:
        context['orchestration']['job']['env']['CONTROLLER_IMAGE'] = p['old_image']
    P.validate(original, context, b.manifest['hash'], b.clock())
    if (original['hash'] != p['policy_hash']
            or original['runtime_implementation_hash'] != p['runtime_implementation_hash']
            or old_marker != ({'kind': 'current_policy', 'root': original['hash'][:16]}, D.encoded(original))
            or P.C.at(p['created_at']) > b.clock()
            or P.C.at(p['created_at']) < P.C.at(original['created_at'])):
        fail('original immutable policy differs')
    if b.tables.get_table(b.c['datasets']['ref'], P.name(source)) is not None:
        fail('replacement policy would fork the original namespace')
    return original


def publish(b, p, release):
    """Phase B owner publication only; automatic controllers have no authority."""
    if (getattr(b, 'current_execution', None) is not None
            or getattr(b, 'phase_b_ack_sha256', None) != p.get('owner_ack_sha256')):
        fail('explicit Phase B owner backend required')
    marker = value(p); resolve(b, p['new_source'], marker, release)
    ds = b.c['datasets']['ref']; n = name(p['new_source'])
    if b.tables.get_table(ds, n) is None: b.tables.create_marker(ds, n, *marker)
    if b.tables.get_table(ds, n) != marker: fail('owner association CAS conflict')
    return p
