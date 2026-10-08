"""Exact owner-only provenance migration after a source-free paused bootstrap.

The failed root remains stopped forever. This additive record authorizes no
source replay or STOP exemption. A separately qualified, semantically equivalent
root still requires its own immutable manifest/GO and live paused bootstrap.
"""
from tools.tenancy import durable_plan as D, full_history as F, tenant_backfill as BF

KIND = 'FULL_BOOTSTRAP_SUPERSEDED'
TYPE = 'PAUSED_ENTRYPOINT_EXCEPTION_IDENTITY_V1'
VERSION = 1
PREDICATES = frozenset({
    'exact_failed_execution', 'paused_boundary_proven', 'exception_identity_defect_proven',
    'no_seller_request', 'no_performance_post', 'no_source_intent', 'no_source_receipt',
    'no_child_runtime', 'no_source_journal', 'no_persisted_source_rows',
    'all_jobs_inactive', 'all_schedulers_paused', 'binding_security_pass',
    'no_active_or_conflicting_lease', 'original_stop_preserved',
    'new_exact_image_qualified', 'runtime_unchanged', 'semantic_plan_equivalent',
})
FIELDS = frozenset({'version','type','tenant','project','old_root','new_root','stop_hash',
    'execution','chunk_plan_hash','index','shard_root','old_controller_image',
    'new_controller_image','new_controller_source','semantic_hash','owner_ack_sha256',
    'predicates','evidence_hashes','verified_at','hash'})
# Every other field, including origin, dates, programs, runtime, finance and caps,
# must be byte-for-byte equal. Qualification references change with the artifact.
PROVENANCE = frozenset({'hash','controller_source_sha','controller_image',
    'controller_implementation_hash','qualification_evidence'})


def fail(message):
    raise BF.B.EvidenceError('full bootstrap recovery: '+message)


def semantic(manifest):
    F.validate_manifest(manifest)
    return {k:v for k,v in manifest.items() if k not in PROVENANCE}


def equivalent(old, new):
    if old['hash']==new['hash'] or semantic(old)!=semantic(new):
        fail('only a provenance change to the identical semantic plan is allowed')
    if old['controller_image']==new['controller_image'] or old['controller_source_sha']==new['controller_source_sha']:
        fail('new exact qualified implementation required')
    return D.digest(semantic(old))


def validate(proof, old, new, records):
    if not isinstance(proof,dict) or set(proof)!=FIELDS:
        fail('closed proof schema required')
    if proof['version']!=VERSION or type(proof['version']) is not int or proof['type']!=TYPE:
        fail('unknown recovery class')
    if D.digest({k:v for k,v in proof.items() if k!='hash'})!=proof['hash']:
        fail('proof digest differs')
    for key in ('old_root','new_root','stop_hash','chunk_plan_hash','shard_root','semantic_hash','owner_ack_sha256','hash'):
        D.check_hash(proof[key])
    if proof['old_root']!=old['hash'] or proof['new_root']!=new['hash']:
        fail('exact old/new root linkage required')
    if proof['tenant']!=old['tenant'] or proof['project']!=old['project']:
        fail('foreign recovery target')
    if proof['semantic_hash']!=equivalent(old,new):fail('semantic evidence differs')
    if (proof['old_controller_image']!=old['controller_image'] or proof['new_controller_image']!=new['controller_image']
            or proof['new_controller_source']!=new['controller_source_sha']):
        fail('exact image/source differs')
    if not isinstance(proof['predicates'],dict) or set(proof['predicates'])!=PREDICATES or any(v is not True for v in proof['predicates'].values()):
        fail('all predicates must be proven, never unknown')
    if not isinstance(proof['evidence_hashes'],dict) or not proof['evidence_hashes']:fail('causal evidence required')
    for value in proof['evidence_hashes'].values():D.check_hash(value)
    F.stamp(proof['verified_at'])
    original=[r for r in records if r['kind']!=KIND]
    migrations=[r for r in records if r['kind']==KIND]
    if len(migrations)>1 or any(r['payload']!=proof or r['root_hash']!=old['hash'] or r['sequence']!=0 for r in migrations):
        fail('conflicting or foreign bootstrap authority')
    if sorted(r['kind'] for r in original)!=['CHUNK_PLAN','FULL_MANIFEST','STOPPED']:
        fail('unexpected old history or source authority')
    if any(r['root_hash']!=old['hash'] or r['version']!=D.VERSION for r in original):fail('foreign/corrupt old history')
    full=next(r for r in original if r['kind']=='FULL_MANIFEST')
    stop=next(r for r in original if r['kind']=='STOPPED')
    chunk=next(r for r in original if r['kind']=='CHUNK_PLAN')
    if full['payload']!=old or full['sequence']!=0:fail('original manifest differs')
    if D.digest(stop)!=proof['stop_hash'] or stop['payload'].get('controller_execution')!=proof['execution'] or stop['payload'].get('reason')!='CONTROLLER_GATE_OR_EVIDENCE_FAILURE':
        fail('exact STOP/execution required')
    expected=f"projects/{old['project']}/locations/{BF.target(old['tenant'])['region']}/jobs/tenant-backfill-controller/executions/tenant-backfill-controller-"
    if not isinstance(proof['execution'],str) or not proof['execution'].startswith(expected):fail('foreign controller execution')
    index=proof['index']
    if type(index) is not int or not 0<=index<len(old['programs']):fail('invalid source leaf')
    item=chunk['payload']
    if D.digest(chunk)!=proof['chunk_plan_hash'] or chunk['sequence']!=index or item.get('index')!=index or item.get('shard_root')!=proof['shard_root']:
        fail('exact selected leaf/shard required')
    F.validate_leaf(old,index,item['plan'],F.stamp(proof['verified_at']).astimezone(BF.B.MSK).date())
    if F.shard_root(old,index,item['plan'])!=proof['shard_root']:fail('shard provenance differs')
    return proof


def publish(backend, proof, old, new):
    """Owner caller must first observe every live predicate with source budget 0.

    Normal controller append access cannot create this owner-only ref marker.
    Neither the old STOP nor its manifest/GO is changed or exempted.
    """
    if backend.c['tenant_id']!=proof['tenant'] or backend.c['project_id']!=proof['project']:
        fail('publisher target differs')
    records=backend.store.history(old['hash']);validate(proof,old,new,records)
    from datetime import timedelta
    age=backend.clock()-F.stamp(proof['verified_at'])
    if age<timedelta(0) or age>timedelta(minutes=15):fail('fresh predicate certificate required')
    from tools.tenancy import orchestration_contract as O
    from tools.tenancy.validation import parse_tenant_json
    release=parse_tenant_json((BF.REPO/'infra/tenant/releases/backfill'/f"{new['controller_source_sha']}.json").read_text())
    O.block(backend.c,{'release':new['controller_source_sha'],'root_hash':new['hash'],'scheduler_state':'PAUSED'},BF.REPO,release)
    O.verify_artifact_source(release,BF.REPO)
    if release['schema_version']!=2 or release['image']!=new['controller_image'] or release['controller_implementation_hash']!=new['controller_implementation_hash']:
        fail('exact qualified replacement artifact required')
    labels={'kind':'full_bootstrap_recovery','root':old['hash'][:16]}
    value=D.encoded(proof);name='BFBR_'+proof['hash'];dataset=backend.c['datasets']['ref']
    prior=backend.tables.get_table(dataset,name)
    if prior is not None and prior!=(labels,value):fail('owner authority marker conflict')
    if prior is None and not backend.tables.create_marker(dataset,name,labels,value):fail('concurrent owner authority; read before continuing')
    if backend.tables.get_table(dataset,name)!=(labels,value):fail('owner authority readback absent')
    record=backend.store.commit(old['hash'],KIND,0,proof,backend.clock())
    after=backend.store.history(old['hash']);validate(proof,old,new,after)
    if not any(r['kind']==KIND and r['payload']==proof for r in after):fail('additive authority record missing')
    return {'record':record,'old_root':old['hash'],'new_root':new['hash'],
            'original_stop_preserved':True,'old_root_resumable':False,'source_dispatches':0}
