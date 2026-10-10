"""Owner-only exact runtime provenance handoff; frozen FULL semantics unchanged."""
from tools.tenancy import durable_plan as D, tenant_backfill as BF, full_history as F
from tools.tenancy.validation import parse_tenant_json as parse
from pipelines.ozon.runtime import full_resume as FR

TYPE='FULL_RUNTIME_COOLDOWN_COMPATIBLE_HANDOFF'
FIELDS=frozenset({'version','type','tenant','project','root','manifest_hash','runtime_source','runtime_image',
    'runtime_implementation_hash','controller_source','controller_image','controller_implementation_hash','owner_ack_sha256','hash'})

def fail(message):raise BF.B.EvidenceError('full runtime handoff: '+message)
def name(root,source):return 'BFRH_AUTH_'+D.check_hash(root)+'_'+source
def value(p):return {'kind':'runtime_handoff','root':p['root'][:16]},D.encoded(p)

def release_for(c):
    from tools.tenancy import platform as PL
    items=[parse(f.read_text()) for f in (BF.REPO/PL.RUNTIME_RELEASES_DIR/'ozon').glob('*.json')]
    items=[r for r in items if r.get('image')==c['marketplaces']['ozon']['runtime_image']]
    if len(items)!=1:fail('exact qualified runtime release missing')
    return items[0]

def runtime_facts(c,manifest):
    if not FR.recognized(manifest):fail('unregistered semantic continuation')
    r=release_for(c);facts=r.get('verification',{}).get('built_artifact',{})
    if (facts.get('backfill_implementation_hash')!=BF.B.implementation_hash()
            or facts.get('full_resume_root')!=manifest['hash']
            or facts.get('full_current_snapshot_adapters')!='PASS'
            or facts.get('performance_cooldown_before_initialization')!='PASS'):
        fail('exact runtime continuation/cooldown not qualified')
    return {'runtime_source':r['source']['commit'],'runtime_image':r['image'],'runtime_implementation_hash':BF.B.implementation_hash()}

def validate(p,c,manifest,release):
    if not isinstance(p,dict) or set(p)!=FIELDS or type(p['version']) is not int or p['version']!=1 or p['type']!=TYPE:fail('closed owner authority required')
    if D.digest({k:v for k,v in p.items() if k!='hash'})!=p['hash']:fail('authority digest differs')
    for k in ('root','manifest_hash','owner_ack_sha256','hash'):D.check_hash(p[k])
    if (p['tenant'],p['project'],p['root'],p['manifest_hash'])!=(manifest['tenant'],manifest['project'],manifest['hash'],D.digest(manifest)):fail('foreign frozen root')
    runtime=runtime_facts(c,manifest)
    if any(p[k]!=v for k,v in runtime.items()):fail('runtime source/image differs')
    if (p['controller_source'],p['controller_image'],p['controller_implementation_hash'])!=(release['source_sha'],release['image'],release['controller_implementation_hash']):fail('controller source/image differs')
    from tools.tenancy import orchestration_contract as O
    O.verify_artifact_source(release,BF.REPO)
    return p

def policy(backend,manifest,release=None):
    from tools.tenancy import cloud_controller as C
    source=backend.c['orchestration']['job']['env']['CONTROLLER_SOURCE_SHA']
    if release is None:
        env=backend.c['orchestration']['job']['env']
        marker=backend.tables.get_table(backend.c['datasets']['tenant_locks'],C.descriptor_name(source,manifest['hash'],env['HISTORICAL_SCHEDULER_STATE']))
        if not marker:fail('qualified descriptor absent')
        release=parse(marker[1])['release']
    marker=backend.tables.get_table(backend.c['datasets']['ref'],name(manifest['hash'],source))
    if not marker:fail('owner runtime handoff absent')
    p=validate(parse(marker[1]),backend.c,manifest,release)
    if marker!=value(p):fail('owner metadata differs')
    return p

def publish(backend,p,manifest,release):
    validate(p,backend.c,manifest,release)
    marker=name(manifest['hash'],release['source_sha']);old=backend.tables.get_table(backend.c['datasets']['ref'],marker)
    if old is None:
        backend.tables.create_marker(backend.c['datasets']['ref'],marker,*value(p));old=backend.tables.get_table(backend.c['datasets']['ref'],marker)
    if old!=value(p):fail('owner publication conflict')
    return marker
