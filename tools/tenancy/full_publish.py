"""Owner-only immutable full manifest/GO publication. No source dispatch or DML.

Explicit owner authority is supplied by the caller; cloud controller append IAM
cannot create the mandatory ref.BFGO marker. This module does not approve gates
from code existence or change lifecycle, Scheduler, identity or permissions.
"""
import json
from tools.tenancy.validation import parse_tenant_json
from tools.tenancy import full_history as F, tenant_backfill as BF
from tools.tenancy import orchestration_contract as O, durable_plan as D


def preflight(backend, manifest, results, *, require_backfilling=False):
    c=F.validate_manifest(manifest)
    F.verify_gate_results(results,manifest['qualification_evidence'])
    if c!=backend.c or c['project_id']!=manifest['project'] or c['tenant_id']!=manifest['tenant']:
        raise BF.B.EvidenceError('owner full publication target/registry differs')
    import lifecycle_core as L
    chain,decisions,ledger,rows,hold=BF.TL.read_state(c,backend.tables)
    allowed={L.BACKFILLING} if require_backfilling else {L.CAPABILITY_DISCOVERY,L.READY_FOR_BACKFILL,L.BACKFILLING}
    if hold or L.audit_history(chain,decisions) or L.current_state(chain) not in allowed:
        raise BF.B.EvidenceError('owner full publication lifecycle/hold denied')
    ph,chunks=BF.CK.latest_plan(ledger,since=BF.TL.cycle_start(chain))
    if ph!=manifest['t5_plan_hash'] or chunks!=F.canonical_chunks(manifest):
        raise BF.B.EvidenceError('owner full publication canonical T5 differs')
    if L.current_state(chain)==L.BACKFILLING:
        events=[e for e in L.ordered(chain) if e.get('to_state')==L.BACKFILLING]
        if not events or decisions.get(events[-1]['seq'],{}).get('plan_hash')!=ph:
            raise BF.B.EvidenceError('owner full publication exact operator approval absent')
    from tools.tenancy.full_controller import verify_capabilities
    verify_capabilities(backend,c)
    bindings,creds=BF.TL.operator_binding(c,backend.tables,backend.clock(),['ads_sku_daily'])
    if bindings!={'seller':'BOUND','performance':'BOUND'} or {k:v['status'] for k,v in creds.items()}!={'seller':'PASS','performance':'PASS'}:
        raise BF.B.EvidenceError('owner full publication binding/credential denied')
    release_path=BF.REPO/'infra/tenant/releases/backfill'/f"{manifest['controller_source_sha']}.json"
    release=parse_tenant_json(release_path.read_text())
    O.block(c,{'release':manifest['controller_source_sha'],'root_hash':manifest['hash'],'scheduler_state':'PAUSED'},BF.REPO,release)
    O.verify_artifact_source(release,BF.REPO)
    if (release['schema_version']!=2 or release['verification'].get('full_history_adapter')!='PASS'
            or release['image']!=manifest['controller_image']
            or release['controller_implementation_hash']!=manifest['controller_implementation_hash']
            or c['marketplaces']['ozon']['runtime_image']!=manifest['runtime_image']):
        raise BF.B.EvidenceError('owner full publication exact release not qualified')
    runtimes=[parse_tenant_json(p.read_text()) for p in (BF.REPO/'infra/tenant/releases/ozon').glob('*.json')]
    runtimes=[r for r in runtimes if r.get('image')==manifest['runtime_image']]
    if (len(runtimes)!=1 or runtimes[0]['source']['commit']!=manifest['runtime_source_sha']
            or runtimes[0]['verification']['built_artifact'].get('full_current_snapshot_adapters')!='PASS'):
        raise BF.B.EvidenceError('owner full publication current-only runtime qualification absent')
    return c


def publish_manifest(backend, manifest, results, *, expected_hash):
    if expected_hash!=manifest['hash']:raise BF.B.EvidenceError('exact owner reviewed full hash required')
    preflight(backend,manifest,results)
    backend.store.commit(expected_hash,'FULL_MANIFEST',0,manifest,backend.clock())
    from tools.tenancy.full_controller import only_manifest
    if only_manifest(backend.store.history(expected_hash))!=manifest:
        raise BF.B.EvidenceError('owner full manifest readback unproven')
    return {'root_hash':expected_hash,'manifest':'COMMITTED','source_dispatches':0,'go':'NOT_PUBLISHED'}


def publish_go(backend, manifest, results, *, expected_hash):
    if expected_hash!=manifest['hash']:raise BF.B.EvidenceError('exact owner reviewed full hash required')
    c=preflight(backend,manifest,results,require_backfilling=True)
    from tools.tenancy.full_controller import only_manifest
    if only_manifest(backend.store.history(expected_hash))!=manifest:
        raise BF.B.EvidenceError('full GO requires committed exact manifest')
    labels,description=F.go_value(manifest,results);name=F.go_marker(manifest)
    existing=backend.tables.get_table(c['datasets']['ref'],name)
    if existing is not None and existing!=(labels,description):
        raise BF.B.EvidenceError('owner full GO marker conflict')
    if existing is None and not backend.tables.create_marker(c['datasets']['ref'],name,labels,description):
        raise BF.B.EvidenceError('owner full GO concurrent publication; read back before continuation')
    if backend.tables.get_table(c['datasets']['ref'],name)!=(labels,description):
        raise BF.B.EvidenceError('owner full GO readback unproven')
    return {'root_hash':expected_hash,'go':'PASS','source_dispatches':0,'ready':'UNPROVEN_FINAL_DQ_REQUIRED'}
