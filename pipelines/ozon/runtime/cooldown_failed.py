"""Closed owner-attested failed initialization incident. Never class-C recovery."""
import re
import backfill_core as B

TYPE='FULL_PERFORMANCE_INITIALIZATION_COOLDOWN_FAILED_ATTEMPT'
KIND='FULL_COOLDOWN_FAILED_RECOVERED'
SHARD_KIND='FAILED_SOURCE_ATTEMPT_SUPERSEDED'
EXACT={'root':'bb3ceca51ec5add6fe6fa3af8ec3720fbb5d9b07054b84e26d6bbfd6c8824222',
    'index':88,'shard':'6389f181d7523e2ef41e8aa540af508b9bfe09db2756ea65b1de202636e4e9c7',
    'stop_hash':'ae188c9c7055382a03b0126670b6dd63abbbc412606883e3d0387c4d50d5b578',
    'receipt_hash':'5d76d3afb7fb34797ff9d04ea95c4f4661e5c4760e607609752c137ee97d3dee',
    'receipt_sequence':4,'plan_id':'7a69ea48bfc2e442273094794b7a0dc33c6489b80866fcdcadafdf42bfb573aa',
    'run_id':'bf-e2dc960c-d74f-4ef2-8e75-337aa98daae2','generation':169,
    'runtime_execution':'ozon-runtime-daily-v2bph','state_hash':'d5382fae5efa40e98237d02ac7ce5a669b2ab42d0553dfc71bfb932e38287a51'}
POLICY_FIELDS=frozenset(EXACT)|{'version','type','tenant','project','manifest_hash','intent_hash',
    'original_runtime_source','original_runtime_image','runtime_source','runtime_image','runtime_implementation_hash',
    'controller_source','controller_image','controller_implementation_hash','owner_ack_sha256','endpoint_decision_sha256',
    'forensic_sha256','endpoint_trace','static_source_effect_proof_accepted','source_budget','hash'}
PREDICATES=frozenset({'exact_incident','exact_original_image','terminal_failed_no_retry','initialization_only',
    'all_prior_units_429','no_new_source_units','no_report_intent_or_uuid','report_post_unreachable',
    'endpoint_unknown_explicitly_accepted','no_source_result_to_reconcile','raw_rows_zero','source_jobs_select_only',
    'no_successor_authority','no_successor_runtime','one_child','checkpoint_unchanged','lease_exact',
    'no_later_generation','bindings_credentials_valid','qualified_replacement','frozen_schedulers',
    'root_manifest_unchanged','source_budget_zero','accepted_predecessor'})
PROOF_FIELDS=frozenset(EXACT)|{'version','type','tenant','project','policy_hash','intent_hash','manifest_hash',
    'failed_aggregate_hash','unit_evidence_hash','lease_hash','checkpoint_hash','prior_receipt_hash','prior_recon_hash','eligible_at','lease_release',
    'lease_release_hash','closure_hash','predicates','verified_at','hash'}

def fail(message):raise B.EvidenceError('failed cooldown recovery: '+message)
def sealed(p):return dict(p,hash=B.digest(p))
def digest(p,fields):
    if not isinstance(p,dict) or set(p)!=fields or p.get('version')!=1 or type(p['version']) is not int or p.get('type')!=TYPE:fail('closed schema/type required')
    if B.digest({k:v for k,v in p.items() if k!='hash'})!=p['hash']:fail('digest differs')
    for k,v in p.items():
        if k.endswith('_hash') or k.endswith('_sha256') or k in {'root','shard','hash'}:
            if not isinstance(v,str) or not re.fullmatch('[0-9a-f]{64}',v):fail('unproven hash')
    if any(p[k]!=v or type(p[k]) is not type(v) for k,v in EXACT.items()):fail('only exact owner failed incident supported')
    import full_resume as FR
    m=FR.manifest()
    if (p['tenant'],p['project'],p['root'])!=(m['tenant'],m['project'],m['hash']):fail('foreign tenant')
    return p

def policy(p):
    digest(p,POLICY_FIELDS)
    for k in ('original_runtime_source','runtime_source','controller_source'):
        if not isinstance(p[k],str) or not re.fullmatch('[0-9a-f]{40}',p[k]):fail('source provenance absent')
    for k,component in (('runtime_image','ozon-runtime'),('controller_image','tenant-backfill-controller')):
        if not isinstance(p[k],str) or not re.fullmatch('europe-west1-docker.pkg.dev/mpa-platform/mpa-runtime/'+component+'@sha256:[0-9a-f]{64}',p[k]):fail('immutable replacement image absent')
    if (p['original_runtime_source'],p['original_runtime_image'])!=('c9d9710b35ac85891b0d5fdcbb4b93ae2eacaa62',
        'europe-west1-docker.pkg.dev/mpa-platform/mpa-runtime/ozon-runtime@sha256:09eb30307a3a645108d1d008c152e628d101afb3cf55f6e3562cdb11a6d01dd2'):fail('historical control-flow image differs')
    if p['source_budget']!=0 or type(p['source_budget']) is not int or p['static_source_effect_proof_accepted'] is not True or p['endpoint_trace']!='UNPROVEN':fail('no source or invented HTTP trace authority')
    return p

def proof(v,p):
    policy(p);digest(v,PROOF_FIELDS)
    if (v['policy_hash'],v['intent_hash'],v['manifest_hash'])!=(p['hash'],p['intent_hash'],p['manifest_hash']):fail('owner/incident linkage differs')
    if not isinstance(v['predicates'],dict) or set(v['predicates'])!=PREDICATES or any(x is not True for x in v['predicates'].values()):fail('mandatory predicate unknown/false')
    from datetime import datetime
    for k in ('verified_at','eligible_at'):
        try:valid=isinstance(v[k],str) and datetime.fromisoformat(v[k].replace('Z','+00:00')).utcoffset() is not None
        except ValueError:valid=False
        if not valid:fail('timestamp unknown')
    if v['lease_release']!='LD_50c5d90dbf43ac9c_0169':fail('lease target differs')
    return v
