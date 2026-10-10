"""Owner-only, source-free closure of the exact missing self-registration STOP.

This is neither class-C recovery nor a future generic STOP exemption. It keeps
the existing FULL root, failed source receipt, source checkpoints and both wake
fences immutable. No cloud automatic publisher or source retry is provided.
"""
import re
from datetime import timedelta
from tools.tenancy import durable_plan as D,tenant_backfill as BF,full_history as F
from tools.tenancy.validation import parse_tenant_json as parse

KIND='FULL_POLICY_BOOTSTRAP_STOP_RECOVERED'
TYPE='EXACT_CURRENT_POLICY_SELF_REGISTRATION_PRE_SOURCE_STOP_V1'
EXACT=dict(root='bb3ceca51ec5add6fe6fa3af8ec3720fbb5d9b07054b84e26d6bbfd6c8824222',
    stop_hash='0df69217a392ae2ad9ecc1175a87a37fb26b958460c02463d23be74a255d1a80',
    execution='projects/mpa-t-client-001/locations/europe-west1/jobs/tenant-backfill-controller/executions/tenant-backfill-controller-l2l8z',
    original_source='c4543b1b572f4a3ec37d7cf7b89e5cf44cff03d5',
    original_image='europe-west1-docker.pkg.dev/mpa-platform/mpa-runtime/tenant-backfill-controller@sha256:f5e9f53f5c6088ce06dacedfc95dff5b520436dbdd68bdcf19e761fd323343dc',
    wake_claim_hash='3e96d111f2d04256ba235c689f299708d772c425ce187d54f55c1b959dda0407',
    wake_close_hash='79931e9c17cf564b0c2efde8eab4a6773166183cabb52e3144dce9dbcfab8c3f',
    failed4_certificate='fb84fe842b63281b61caa86da7bb2fb11437b358009327a4fe9401ec4935fb75')
PREDICATES=frozenset({'exact_failed_bootstrap','compiled_policy_file_failure',
    'replacement_descriptor_reader_pass','bootstrap_source_dispatches_zero',
    'no_new_ingestion_journal','no_child_runtime','no_source_authority_change',
    'all_jobs_inactive','all_schedulers_paused','both_bindings_pass',
    'security_metadata_unchanged','frozen_root_and_recent_set','failed4_recovery_preserved',
    'closed_old_controller_claim','no_checkpoint_or_lease_rewrite','runtime_unchanged',
    'source_budget_zero','no_successor_controller'})
FIELDS=frozenset(EXACT)|{'version','type','tenant','project','manifest_hash',
    'new_source','new_image','new_implementation_hash','runtime_image','owner_decision_sha256',
    'packaged_probe_hash','replacement_image_check_hash','protected_history_hash',
    'protected_record_hashes','predicates','verified_at','source_budget','hash'}


def fail(message):raise BF.B.EvidenceError('policy bootstrap recovery: '+message)
def sealed(p):return dict(p,hash=D.digest(p))
def authority_name(p):return 'BFPB_AUTH_'+p['root']+'_'+p['stop_hash']
def certificate_name(p):return 'BFPB_'+p['hash']
def value(p):return {'kind':'policy_bootstrap','root':p['root'][:16]},D.encoded(p)


def proof_header(p,m):
    if (not isinstance(p,dict) or set(p)!=FIELDS or type(p['version']) is not int
            or p['version']!=1 or p['type']!=TYPE
            or D.digest({k:v for k,v in p.items() if k!='hash'})!=p['hash']):
        fail('closed owner proof required')
    if any(p[k]!=v or type(p[k]) is not type(v) for k,v in EXACT.items()):fail('only exact incident supported')
    for k in FIELDS:
        if k.endswith('_hash') or k.endswith('_sha256') or k in {'root','hash','failed4_certificate'}:D.check_hash(p[k])
    if (p['tenant'],p['project'],p['root'],p['manifest_hash'])!=(m['tenant'],m['project'],m['hash'],D.digest(m)):
        fail('foreign frozen root')
    if (p['tenant'],p['project'])!=('client_001','mpa-t-client-001'):fail('foreign tenant')
    if type(p['source_budget']) is not int or p['source_budget']!=0:fail('source budget must be zero')
    if not isinstance(p['predicates'],dict) or set(p['predicates'])!=PREDICATES or any(v is not True for v in p['predicates'].values()):
        fail('unknown mandatory predicate')
    F.stamp(p['verified_at'])
    if not re.fullmatch('[0-9a-f]{40}',p['new_source']) or p['new_source']==p['original_source']:
        fail('qualified replacement source required')
    if not re.fullmatch('europe-west1-docker.pkg.dev/mpa-platform/mpa-runtime/tenant-backfill-controller@sha256:[0-9a-f]{64}',p['new_image']):
        fail('immutable replacement image required')
    hashes=p['protected_record_hashes']
    if not isinstance(hashes,list) or not 1<=len(hashes)<=10000 or any(not isinstance(h,str) for h in hashes):
        fail('bounded protected record inventory required')
    for h in hashes:D.check_hash(h)
    if hashes!=sorted(set(hashes)) or D.digest(hashes)!=p['protected_history_hash']:
        fail('protected record inventory differs')
    return p


def validate(p,b,m,records):
    proof_header(p,m)
    from tools.tenancy import full_controller as H,full_cooldown_recovery as FC,controller_cadence as CC
    from tools.tenancy import cloud_controller as C,orchestration_contract as O
    if H.only_manifest(records)!=m:fail('original manifest differs')
    if not set(p['protected_record_hashes'])<={D.digest(r) for r in records}:fail('immutable protected history changed')
    stops=[r for r in records if r['kind']=='STOPPED' and D.digest(r)==p['stop_hash']]
    expected={'reason':'CONTROLLER_GATE_OR_EVIDENCE_FAILURE','controller_execution':p['execution']}
    if len(stops)!=1 or stops[0]['payload']!=expected:fail('original STOP differs')
    retained=[r['payload'] for r in records if r['kind']==FC.KIND and r['payload']['hash']==p['failed4_certificate']]
    if len(retained)!=1:fail('accepted FAILED4 certificate missing')
    accepted=b.tables.get_table(b.c['datasets']['ref'],'BFCF_ACCEPTED_'+p['failed4_certificate'])
    accepted_doc=parse(accepted[1]) if accepted else {}
    if accepted_doc.get('proof')!=retained[0]:fail('FAILED4 acceptance changed')
    ds=b.c['datasets']['tenant_locks'];claim_value=b.tables.get_table(ds,CC.names(p['root'],1)[0])
    claim=CC.validate_claim(b,claim_value,allow_closed_history=True)
    x=b.request('GET',BF.RUN_API+'/'+p['execution'])
    env={e['name']:e.get('value') for e in x['template']['containers'][0]['env']}
    if x.get('failedCount')!=1 or x.get('succeededCount',0) or env.get('HISTORICAL_SCHEDULER_STATE')!='PAUSED':
        fail('exact failed paused bootstrap required')
    close_value=b.tables.get_table(ds,CC.names(p['root'],1)[1]);close=parse(close_value[1]) if close_value else {}
    if ((claim['hash'],claim['source'],claim['image'],claim['execution'])
            !=(p['wake_claim_hash'],p['original_source'],p['original_image'],p['execution'])
            or close.get('hash')!=p['wake_close_hash'] or close.get('status')!='STOPPED'
            or close.get('source_dispatches')!=0 or not CC.closed(b,claim)):
        fail('original closed wake differs')
    settings=dict(release=p['new_source'],root_hash=p['root'],scheduler_state='PAUSED')
    marker=b.tables.get_table(ds,C.descriptor_name(p['new_source'],p['root'],'PAUSED'))
    descriptor=parse(marker[1]) if marker else {}
    if set(descriptor)!={'settings','release'} or descriptor['settings']!=settings:fail('replacement descriptor missing')
    release=descriptor['release']
    if (release['source_sha'],release['image'],release['controller_implementation_hash'],release['runtime_image'])!=(p['new_source'],p['new_image'],p['new_implementation_hash'],p['runtime_image']):
        fail('qualified replacement differs')
    current=b.c['orchestration']['job']['env']['CONTROLLER_SOURCE_SHA']
    if current==p['new_source']:O.verify_artifact_source(release,BF.REPO)
    else:
        historical=parse((BF.REPO/'infra/tenant/releases/backfill'/f"{p['new_source']}.json").read_text())
        if historical!=release:fail('historical replacement registration differs')
    O.block(b.c,settings,BF.REPO,release)
    copies=[r for r in records if r['kind']==KIND]
    if len(copies)>1 or any(r['root_hash']!=p['root'] or r['sequence']!=0 or r['payload']!=p for r in copies):
        fail('conflicting recovery record')
    return p


def load(b,m,records):
    found=[r['payload'] for r in records if r['kind']==KIND]
    if len(found)>1:fail('single exact recovery only')
    for p in found:
        validate(p,b,m,records)
        if (b.tables.get_table(b.c['datasets']['ref'],authority_name(p))!=value(p)
                or b.tables.get_table(b.c['datasets']['tenant_locks'],certificate_name(p))!=value(p)):
            fail('owner authority/certificate fence missing')
    return found


def publish(b,p,m):
    """Owner applies only a fresh independent source-free predicate attestation."""
    from tools.tenancy import full_controller as H,full_leaf_recovery as FL,full_cooldown_recovery as FC
    if b.current_execution is not None:fail('owner source-free observation required')
    records=b.store.history(m['hash']);validate(p,b,m,records)
    if b.c['orchestration']['job']['env']['CONTROLLER_SOURCE_SHA']!=p['new_source']:fail('replacement is not current')
    if b.c['orchestration']['job']['env']['HISTORICAL_SCHEDULER_STATE']!='PAUSED':fail('frozen deployment required')
    age=b.clock()-F.stamp(p['verified_at'])
    if age<timedelta(0) or age>timedelta(minutes=15):fail('fresh independent predicates required')
    accepted=FL.load(b,m,records)+FC.load(b,m,records)
    unresolved={D.digest(r) for r in records if r['kind']=='STOPPED'}-{q['stop_hash'] for q in accepted}
    old=load(b,m,records)
    if old:return old[0]
    if unresolved!={p['stop_hash']}:fail('another unresolved incident')
    if sorted(D.digest(r) for r in records)!=p['protected_record_hashes']:
        fail('new source or root authority since predicate observation')
    doc=next(r['payload']['plan'] for r in records if r['kind']=='CHUNK_PLAN' and r['sequence']==88)
    observer=FL.observer(b,m,88);H.verify_authority(observer,m);observer.preflight({'hash':m['hash'],'plans':[doc]})
    n=b.select(f"SELECT COUNT(*) AS n FROM `{b.journal}` WHERE started_at>=@start",{'start':('TIMESTAMP','2026-10-10T09:38:41.371013Z')})
    if n!=[{'n':0}]:fail('new source journal evidence')
    for ds,name in ((b.c['datasets']['ref'],authority_name(p)),(b.c['datasets']['tenant_locks'],certificate_name(p))):
        prior=b.tables.get_table(ds,name)
        if prior is None:b.tables.create_marker(ds,name,*value(p))
        if b.tables.get_table(ds,name)!=value(p):fail('publication conflict')
    b.store.commit(p['root'],KIND,0,p,b.clock())
    if load(b,m,b.store.history(p['root']))!=[p]:fail('additive recovery readback differs')
    return p
