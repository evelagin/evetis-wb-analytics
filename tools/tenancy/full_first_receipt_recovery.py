"""Qualified first-receipt SUCCESS recovery, additive and source-free.

A first authority starts at source unit one, with no earlier checkpoint, receipt
or RECON. This separate contract does not relax legacy class-C predecessors.
Source persistence and current Catalog/DQ completion remain distinct proofs.
"""
from tools.tenancy import full_leaf_recovery as R, durable_plan as D, tenant_backfill as BF
from tools.tenancy.validation import parse_tenant_json as parse

TYPE='FIRST_RECEIPT_POST_DISPATCH_TERMINAL_SUCCESS_UNRECONCILED'
VERSION=1
KIND='FULL_FIRST_RECEIPT_STOP_RECOVERED'
FIELDS=(R.FIELDS-{'prior_receipt_hash','prior_recon_hash'})|{'initial_state_hash'}
PREDICATES=(R.PREDICATES-{'predecessor_reconciled'})|{'first_authority','initial_source_prefix','no_prior_checkpoint'}
POLICY_FIELDS=(R.POLICY_FIELDS-{'automatic_class_c'})|{'automatic_first_receipt','historical_policies'}
# Retained Supplies and async report effects require their own accepted contract.
ENTITIES=frozenset({'finance_accrual','fbo_postings','ads_expense_daily'})
fail=R.fail
sealed=R.sealed


def policy_name(root,source):return 'BFFRF_AUTH_'+D.check_hash(root)+'_'+source

def policy_value(p):return {'kind':'first_receipt_recovery','root':p['root'][:16]},D.encoded(p)

def fence_name(p):return 'BFFRF_'+p['root']+'_'+p['stop_hash']

def fence_value(p):return {'kind':'first_receipt_recovery','root':p['root'][:16]},D.encoded(p)


def validate_policy(p,manifest,release,*,historical=False):
    if not isinstance(p,dict) or set(p)!=POLICY_FIELDS:fail('closed first-receipt owner policy required')
    if p['type']!=TYPE or p['automatic_first_receipt'] is not True or p['initial_unit_start']!=1:fail('first-receipt owner scope differs')
    if D.digest({k:v for k,v in p.items() if k!='hash'})!=p['hash']:fail('first policy digest differs')
    pins=p['historical_policies']
    if not isinstance(pins,list) or len(pins)>100 or any(not isinstance(x,dict) or set(x)!={'source','policy_hash'} for x in pins):fail('closed historical first policies required')
    import re
    if any(not isinstance(x['source'],str) or not re.fullmatch('[0-9a-f]{40}',x['source']) for x in pins) or len({x['source'] for x in pins})!=len(pins):fail('historical first policy source differs')
    for x in pins:D.check_hash(x['policy_hash'])
    # Reuse exact tenant/root/artifact/runtime/initial-authority validation, not
    # another incident's certificate or its predecessor predicate.
    common={k:v for k,v in p.items() if k not in {'hash','automatic_first_receipt','historical_policies'}}
    common.update(type=R.TYPE,automatic_class_c=True)
    R.validate_policy(sealed(common),manifest,release,historical=historical)
    return p


def policy(backend,manifest,release=None):
    from tools.tenancy import cloud_controller as C
    env=backend.c['orchestration']['job']['env'];source=env['CONTROLLER_SOURCE_SHA']
    if release is None:
        marker=backend.tables.get_table(backend.c['datasets']['tenant_locks'],C.descriptor_name(source,manifest['hash'],env['HISTORICAL_SCHEDULER_STATE']))
        if not marker:fail('first receipt descriptor absent')
        release=parse(marker[1]).get('release',{})
    marker=backend.tables.get_table(backend.c['datasets']['ref'],policy_name(manifest['hash'],source))
    p=parse(marker[1]) if marker else {};validate_policy(p,manifest,release)
    if marker!=policy_value(p):fail('first receipt owner authority absent/conflicting')
    return p


def publish_policy(backend,p,manifest,release):
    validate_policy(p,manifest,release)
    if (backend.c['tenant_id'],backend.c['project_id'])!=(p['tenant'],p['project']):fail('foreign policy publisher')
    name=policy_name(p['root'],p['controller_source']);old=backend.tables.get_table(backend.c['datasets']['ref'],name)
    if old is None:backend.tables.create_marker(backend.c['datasets']['ref'],name,*policy_value(p))
    if backend.tables.get_table(backend.c['datasets']['ref'],name)!=policy_value(p):fail('first authority CAS conflict')
    return name


def historical_policy(backend,manifest,active,source):
    from tools.tenancy import orchestration_contract as O
    refs=[x for x in active['historical_policies'] if x['source']==source]
    if len(refs)!=1:fail('historical first receipt authority unpinned')
    marker=backend.tables.get_table(backend.c['datasets']['ref'],policy_name(manifest['hash'],source))
    old=parse(marker[1]) if marker else {}
    release=parse((BF.REPO/'infra/tenant/releases/backfill'/f'{source}.json').read_text())
    validate_policy(old,manifest,release,historical=True)
    keys={'initial_stop','initial_receipt','initial_intent','initial_generation','initial_unit_start','initial_unit_end','automatic_first_receipt','owner_ack_sha256'}
    if old['hash']!=refs[0]['policy_hash'] or marker!=policy_value(old) or any(old[k]!=active[k] for k in keys):fail('historical first authority broadened')
    return old


def validate(proof,manifest,p,root_records,shard_records,*,final=False):
    doc,receipt=R._validate(proof,manifest,p,root_records,shard_records,final=final,first=True)
    if doc['runtime_plan']['entity'] not in ENTITIES:fail('unsupported first receipt source-effect contract')
    coverage=proof['reconciliation']['coverage_readback']
    if coverage.get('catalog_linkage')!='PENDING_CURRENT_DAY_DQ':fail('source-only proof must preserve pending Catalog/DQ')
    if any(not isinstance(r,dict) or type(r.get('rows')) is not int or r['rows']<0 or r.get('keys')!=r['rows'] for r in coverage['readback']):fail('source-only counts/keys unproven')
    if sum(r['rows'] for r in coverage['readback'])!=proof['reconciliation']['rows_observed']:fail('source total differs')
    return doc,receipt


def observe(base,manifest,stop_hash,index,*,automatic=False,existing=None):
    records=base.store.history(manifest['hash'])
    docs=[r['payload']['plan'] for r in records if r['kind']=='CHUNK_PLAN' and r['sequence']==index]
    if len(docs)!=1 or docs[0]['runtime_plan']['entity'] not in ENTITIES:fail('unsupported first source contract')
    return R._observe(base,manifest,stop_hash,index,automatic=automatic,existing=existing,first=True)


def load(backend,manifest,records):return R._load(backend,manifest,records,first=True)

def apply(base,manifest,stop_hash,index,*,automatic=False):return R._apply(base,manifest,stop_hash,index,automatic=automatic,first=True)

def recover_pending(backend,manifest,records):
    # Older registered images/policies remain valid without granting this class.
    env=backend.c['orchestration']['job']['env']
    if backend.tables.get_table(backend.c['datasets']['ref'],policy_name(manifest['hash'],env['CONTROLLER_SOURCE_SHA'])) is None:return None
    return R._recover_pending(backend,manifest,records,first=True)
