"""Closed rolling operational authority, separate from the historical manifest.

Pure validation and deterministic ownership. Publication, source transport and
financial finality are deliberately absent. The cloud adapter also requires
the exact qualified descriptor, fresh tenant gates and shared source fences.
"""
from datetime import date, datetime, timedelta, timezone
import re
from tools.tenancy import current_contract as C, durable_plan as D

FIELDS=frozenset({'version','type','tenant','project','historical_root','owner_decision_sha256',
    'created_at','first_day','entities','snapshot_sla_seconds','fbo_sla_seconds',
    'controller_source','controller_image','runtime_image','runtime_implementation_hash',
    'fbo_contract_hash','source_concurrency','finance_finality','hash'})
ORDER=('catalog','stocks','prices','finance_accrual','ads_campaigns','ads_expense_daily',
       'fbo_postings','ads_sku_daily')

def fail(s):raise ValueError('current authority: '+s)
def sealed(p):return dict(p,hash=D.digest(p))
def name(source):
    if not isinstance(source,str) or not re.fullmatch('[0-9a-f]{40}',source):fail('immutable controller source required')
    return 'BFCURRENT_'+source

def validate(p, c, historical_root, now):
    if not isinstance(p,dict) or set(p)!=FIELDS or type(p['version']) is not int or p['version']!=1 or p['type']!=C.TYPE:fail('closed owner policy required')
    if p['hash']!=D.digest({k:v for k,v in p.items() if k!='hash'}):fail('policy digest differs')
    if (p['tenant'],p['project'],p['historical_root'])!=(c['tenant_id'],c['project_id'],historical_root):fail('tenant/root differs')
    if (p['tenant'],p['project'])!=('client_001','mpa-t-client-001'):fail('unqualified tenant scope')
    for k in ('hash','historical_root','owner_decision_sha256','runtime_implementation_hash'):D.check_hash(p[k])
    if p['fbo_contract_hash'] is not None:D.check_hash(p['fbo_contract_hash'])
    name(p['controller_source'])
    env=c['orchestration']['job']['env']
    if (p['controller_source'],p['controller_image'],p['runtime_image'])!=(env['CONTROLLER_SOURCE_SHA'],c['orchestration']['job']['image'],c['marketplaces']['ozon']['runtime_image']):fail('qualified release pair differs')
    if p['entities']!=list(ORDER):fail('operational domains differ')
    if type(p['source_concurrency']) is not int or p['source_concurrency']!=1 or p['finance_finality']!='PROVISIONAL':fail('concurrency/finality differs')
    if type(p['snapshot_sla_seconds']) is not int or p['snapshot_sla_seconds']!=86400 or type(p['fbo_sla_seconds']) is not int or p['fbo_sla_seconds']!=86400:fail('freshness SLA differs')
    if C.at(p['created_at'])>now:fail('future authority')
    try:day=date.fromisoformat(p['first_day'])
    except (ValueError,TypeError):fail('first operational day unavailable')
    if day!=C.at(p['created_at']).astimezone(C.MSK).date():fail('operational origin day differs')
    return p

def cycle_root(p, day):
    if type(day) is not date or day<date.fromisoformat(p['first_day']):fail('unknown operational day')
    return D.digest(dict(current_policy=p['hash'],moscow_day=day.isoformat()))

def generation(p,day,entity):
    if entity not in ORDER:fail('unknown current entity')
    cycle_root(p,day)
    return 'cur-'+p['hash'][:16]+'-'+day.strftime('%Y%m%d')+'-'+entity

def shard(p,day,doc):
    return D.digest(dict(current_cycle=cycle_root(p,day),ack_hash=doc['ack_hash']))

def document(p, day, entity, *, make_plan=None):
    from tools.tenancy import tenant_backfill as BF
    make_plan=make_plan or BF.make_plan
    if entity=='fbo_postings' and p['fbo_contract_hash'] is None:fail('shared FBO source contract is unproven')
    w=C.source_window(entity,day)
    return make_plan(p['tenant'],entity,w['from_date'],w['to_date'],generation(p,day,entity),
        p['created_at'],today=day,max_units=20,window_days=1)

def original_runtime(p, doc):
    """The same plan pinned to the policy's ORIGINAL runtime pair (succession read-back only).

    Plans elected before an owner runtime succession carry the original runtime image and
    implementation hash. Business scope, budgets and namespace are unchanged; only the
    runtime pin is re-derived, then plan_id and ack_hash are recomputed exactly as made.
    """
    rp={k:v for k,v in doc['runtime_plan'].items() if k!='plan_id'}
    rp['implementation_hash']=p['runtime_implementation_hash'];rp['plan_id']=D.digest(rp)
    out={k:v for k,v in doc.items() if k!='ack_hash'}
    out.update(runtime_plan=rp,image=p['runtime_image']);out['ack_hash']=D.digest(out)
    return out

def validate_document(p, day, doc, *, make_plan=None, historical=True):
    """Live plan, or (read-back only) the same plan pinned to the original runtime pair.

    Without a runtime succession both forms are identical. A historical-pair plan is never
    accepted for dispatch (historical=False): it may only be read, certified or drained.
    """
    entity=doc.get('runtime_plan',{}).get('entity') if isinstance(doc,dict) else None
    expected=document(p,day,entity,make_plan=make_plan)
    if expected==doc:return True
    # Only a plan actually pinned to the policy's original runtime (and differing from the live one)
    # is re-derived; without a succession the two pins are equal and nothing else is accepted.
    pin=doc.get('runtime_plan',{}).get('implementation_hash') if isinstance(doc,dict) else None
    if (historical and pin==p['runtime_implementation_hash']!=expected['runtime_plan'].get('implementation_hash')
            and original_runtime(p,expected)==doc):return True
    fail('current plan/window/budget differs')

def eligible(p, certificates, *, pending_entities=()):
    """P0 independent work before P1; unknown FBO never stalls Seller snapshots.

    Pending elected source authorities are drained first by the cloud adapter.
    A complete certificate cannot be inferred from an empty/failed response.
    """
    if not isinstance(certificates,dict) or set(certificates)-set(ORDER):fail('unknown certificate entity')
    if any(type(v) is not bool for v in certificates.values()):fail('unknown current completion')
    if not set(pending_entities)<=set(ORDER):fail('unknown pending source authority')
    selected=[e for e in ORDER if not certificates.get(e,False)]
    if p['fbo_contract_hash'] is None:selected=[e for e in selected if e!='fbo_postings']
    # Campaigns must precede SKU; Catalog is a strict dependency for Stocks,
    # Prices, Finance and advertising SKU linkage.
    if not certificates.get('catalog',False):return ['catalog']
    if not certificates.get('ads_campaigns',False):selected=[e for e in selected if e!='ads_sku_daily']
    return selected
