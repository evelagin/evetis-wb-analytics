"""Canonical opt-in controller deployment contract, never live-state evidence."""
import hashlib
import re
from tools.tenancy import orchestration_identity as I
from tools.tenancy.validation import parse_tenant_json

JOB = 'tenant-backfill-controller'
SCHEDULER = 'tenant-backfill-tick'
SOURCE_FILES = (
    'cloud_access.py','cloud_tick.py','cloud_controller.py','durable_plan.py',
    'orchestration_contract.py','orchestration_identity.py','tenant_backfill.py','tenant_tables.py','pre_source_recovery.py','controller_stop_recovery.py','controller_stop_image_check.py','controller_dispatch_recovery.py','controller_dispatch_image_check.py',
    'full_history.py', 'full_controller.py', 'full_image_check.py', 'full_bootstrap_recovery.py',
)


def implementation_hash(repo):
    from tools.tenancy import tenant_backfill as BF
    return BF.B.digest({n:hashlib.sha256((repo/'tools/tenancy'/n).read_bytes()).hexdigest() for n in SOURCE_FILES})


def block(c, settings, repo, release=None):
    from tools.tenancy import tenant_backfill as BF
    if not c['marketplaces'].get('ozon') or set(settings)!={'release','root_hash','scheduler_state'}:
        raise ValueError('closed dedicated Ozon orchestration settings required')
    if not re.fullmatch(r'[0-9a-f]{40}',settings['release']) or not re.fullmatch(r'[0-9a-f]{64}',settings['root_hash']):
        raise ValueError('immutable controller release and plan root required')
    if settings['scheduler_state'] not in {'PAUSED','ENABLED'}:
        raise ValueError('explicit historical Scheduler state required')
    release=release or parse_tenant_json((repo/'infra/tenant/releases/backfill'/f"{settings['release']}.json").read_text())
    expected={'schema_version','image','source_sha','runtime_image','runtime_implementation_hash','controller_implementation_hash','verification'}
    if set(release)!=expected or type(release['schema_version']) is not int or release['schema_version'] not in {1,2} or release['source_sha']!=settings['release']:
        raise ValueError('controller release schema/provenance mismatch')
    if not re.fullmatch(r'europe-west1-docker\.pkg\.dev/mpa-platform/mpa-runtime/tenant-backfill-controller@sha256:[0-9a-f]{64}',release['image']):
        raise ValueError('immutable canonical controller image required')
    if release['runtime_image']!=c['marketplaces']['ozon']['runtime_image']:
        raise ValueError('controller/runtime implementation qualification differs')
    required={'ci','exact_image','offline_restart','lost_post_no_repeat','quota_wait_no_source','tenant_isolation','reader_append_separation'}
    if release['schema_version']==2:required.add('full_history_adapter')
    if set(release['verification'])!=required or any(v!='PASS' for v in release['verification'].values()):
        raise ValueError('controller release gates not PASS')
    p=c['project_id'];base=f"projects/{p}/locations/{c['region']}"
    accounts={kind:{'id':f'sa-backfill-{kind}','email':f'sa-backfill-{kind}@{p}.iam.gserviceaccount.com'} for kind in ('controller','append','wake')}
    role=lambda name:f'projects/{p}/roles/{name}'
    grants=[]
    for key in ('ozon_raw','tenant_ops','ref','tenant_locks'):
        name='backfillReadRef' if key=='ref' else ('backfillLockRead' if key=='tenant_locks' else 'backfillRead')
        grants.append({'dataset_key':key,'role':role(name),'email':accounts['controller']['email']})
    grants.append({'dataset_key':'tenant_locks','role':role('backfillLockCreate'),'email':accounts['append']['email']})
    grants.append({'dataset_key':'tenant_ops','role':role('backfillAppendDatasetMetadata'),'email':accounts['append']['email']})
    env={'TENANT_ID':c['tenant_id'],'GCP_PROJECT_ID':p,'BQ_RAW_DATASET':c['datasets']['ozon_raw'],
         'BQ_REF_DATASET':c['datasets']['ref'],'BQ_LOCATION':'EU','TENANT_BINDING_REQUIRED':'1','STRICT_PAGE_CAPS':'1',
         'BACKFILL_ROOT_HASH':settings['root_hash'],'CONTROLLER_IMAGE':release['image'],
         'CONTROLLER_SOURCE_SHA':release['source_sha'],'HISTORICAL_SCHEDULER_STATE':settings['scheduler_state']}
    return {'accounts':accounts,'roles':{n:list(v) for n,v in I.ROLE_PERMISSIONS.items()},
            'dataset_grants':grants,'matrix':I.matrix_for_contract(c),
            'job':{'name':JOB,'image':release['image'],'env':env,'timeout':'600s'},
            'scheduler':{'name':SCHEDULER,'schedule':'0 * * * *' if release['runtime_implementation_hash']==BF.QF.accepted_doc(BF.QF.SKU)['runtime_plan']['implementation_hash'] else '*/10 * * * *','time_zone':'Europe/Moscow','state':settings['scheduler_state'],
                         'uri':f'https://run.googleapis.com/v2/{base}/jobs/{JOB}:run'}}


def paused_contract(c, repo):
    """Owner staging can only pause the same qualified root/release, never alter scope."""
    from copy import deepcopy
    out=deepcopy(c);old=out.get('orchestration')
    if not old:raise ValueError('registered controller required for paused staging')
    env=old['job']['env']
    out['orchestration']=block(out,{'release':env['CONTROLLER_SOURCE_SHA'],
        'root_hash':env['BACKFILL_ROOT_HASH'],'scheduler_state':'PAUSED'},repo)
    return out


def verify_job(job, expected):
    from tools.tenancy import tenant_backfill as BF
    outer=job['template'];t=outer['template'];containers=t['containers']
    if outer.get('taskCount',1)!=1 or outer.get('parallelism',0) not in (0,1) or t.get('timeout')!=expected['job']['timeout'] or t.get('maxRetries',0)!=0:
        raise BF.B.EvidenceError('controller concurrency/retry/timeout drift')
    if len(containers)!=1 or t.get('serviceAccount')!=expected['accounts']['controller']['email']:
        raise BF.B.EvidenceError('controller service identity drift')
    container=containers[0];entries=container.get('env',[]);env={e['name']:e.get('value') for e in entries}
    if container.get('image')!=expected['job']['image'] or container.get('command')!=['python','-m','tools.tenancy.cloud_controller'] or (container.get('args') or []) or len(env)!=len(entries) or env!=expected['job']['env']:
        raise BF.B.EvidenceError('controller image/entrypoint/environment drift')


def verify_artifact_source(release, repo):
    """Registry describes immutable deployment facts, not candidate source fitness.

    Every executing image must separately match its packaged implementation. A
    feature checkout can read old registry without pretending to be released.
    """
    from tools.tenancy import tenant_backfill as BF
    if release['runtime_implementation_hash'] != BF.B.implementation_hash() or release['controller_implementation_hash'] != implementation_hash(repo):
        raise BF.B.EvidenceError('controller/runtime packaged implementation qualification differs')
