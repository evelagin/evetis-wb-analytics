"""Exact owner-reviewed runtime handoff for one immutable FULL program.

The packaged manifest is immutable semantic evidence, not a new plan or GO.
Its old plan identities remain authoritative. Only a separately qualified
runtime release and owner provenance policy can make the handoff operational.
"""
import copy
import json
import re
from datetime import date, timedelta
from pathlib import Path

PLAN_FIELDS=frozenset({'version','project','raw','ref','entity','from','to','generation','origin',
    'window_days','minimum_ms','fbo_page_cap','order_batch','bundle_page_cap','implementation_hash','plan_id'})
DOCUMENT_FIELDS=frozenset({'mode','tenant_id','image','runtime_plan','max_requests','max_units','max_order_batches','ack_hash'})

def manifest():
    import backfill_core as B
    m=json.loads(Path(__file__).with_name('full_resume.json').read_text())
    if m['hash']!=B.digest({k:v for k,v in m.items() if k!='hash'}):
        raise B.EvidenceError('full continuation manifest digest differs')
    return m

def recognized(m):
    return m==manifest()

def scope(candidate, root):
    import backfill_core as B
    m=manifest()
    if root!=m['hash'] or candidate['project']!=m['project'] or candidate['origin']!=m['created_at']:
        raise B.EvidenceError('full continuation namespace differs')
    match=re.fullmatch('full-'+re.escape(root[:24])+'-([0-9]+)',candidate['generation'])
    if not match or not 0<=int(match[1])<len(m['programs']):
        raise B.EvidenceError('full continuation program index differs')
    program=m['programs'][int(match[1])]
    fields=PLAN_FIELDS if program['entity'] in B.DATED or program['entity']=='supplies' else PLAN_FIELDS|{'observation_date'}
    if set(candidate)!=fields:
        raise B.EvidenceError('full continuation plan schema differs')
    if candidate['entity']!=program['entity'] or program.get('accepted_qualification_plan'):
        raise B.EvidenceError('full continuation entity/reused qualification differs')
    if program['kind']=='DATED':
        if (candidate['from'],candidate['to'])!=(program['from'],program['to']):
            raise B.EvidenceError('full continuation business dates differ')
    else:
        observed=date.fromisoformat(candidate.get('observation_date',''))
        if observed<date.fromisoformat(m['created_at'][:10]) or candidate['from']!=candidate['to'] or candidate['to']!=str(observed-timedelta(days=1)):
            raise B.EvidenceError('full continuation observation dates differ')
    expected={'version':B.VERSION,'raw':'ozon_raw','ref':'ref','window_days':program.get('window_days',1),
        'minimum_ms':60000,'fbo_page_cap':200,'order_batch':5,'bundle_page_cap':500}
    if any(candidate.get(k)!=v for k,v in expected.items()):
        raise B.EvidenceError('full continuation source budgets/contract differ')
    return int(match[1])

def resume(candidate, root, pid):
    import backfill_core as B
    scope(candidate,root)
    old=copy.deepcopy(candidate)
    old['implementation_hash']=manifest()['runtime_implementation_hash']
    old.pop('plan_id',None);old['plan_id']=B.digest(old)
    if old['plan_id']!=pid:
        raise B.EvidenceError('full continuation plan identity differs')
    return old

def historical(candidate):
    import backfill_core as B
    m=manifest();scope(candidate,m['hash'])
    out=copy.deepcopy(candidate);out['implementation_hash']=m['runtime_implementation_hash']
    out.pop('plan_id',None);out['plan_id']=B.digest(out)
    return out

def document(current):
    import backfill_core as B
    if not isinstance(current,dict) or set(current)!=DOCUMENT_FIELDS or current.get('mode')!='BOUNDED_PILOT':
        raise B.EvidenceError('full continuation document schema differs')
    out=copy.deepcopy(current)
    if out['tenant_id']!=manifest()['tenant'] or out['max_requests']!=400 or out['max_units']!=20 or out['max_order_batches']!=1:
        raise B.EvidenceError('full continuation execution contract differs')
    out['runtime_plan']=historical(out['runtime_plan']);out['image']=manifest()['runtime_image']
    out.pop('ack_hash',None);out['ack_hash']=B.digest(out)
    return out

def matches(doc):
    import backfill_core as B
    try:
        return isinstance(doc,dict) and document(doc)==doc
    except (B.EvidenceError,KeyError,ValueError,TypeError):
        return False
