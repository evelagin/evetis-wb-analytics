"""Scoped, reviewed expertise. Pending proposals are never generation permission.

No external lookup. Approval must bind the immutable knowledge snapshot, source,
owner decision, product and exact reviewed semantic variants. No blanket claim switch.
"""
import hashlib
import json
from pathlib import Path
from app.v3.text import normalize

FILE = Path(__file__).with_name('expertise_registry.json')

def load_registry():
    data=json.loads(FILE.read_text())
    if data.get('schema')!='evetis-approved-expertise/1':raise ValueError('expertise schema')
    return data

def registry_sha():
    return hashlib.sha256(json.dumps(load_registry(),sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()

def approved(snapshot, product_id, *, registry=None):
    data=registry if registry is not None else load_registry()
    if data.get('snapshot_sha256')!=snapshot.data['content_sha256']:return []
    from app.v3.verifier import context_for_free_text, verify
    ctx=context_for_free_text(snapshot,[product_id]);out=[]
    for row in data['explanations']:
        if row.get('kind') not in {'fact','ingredient_benefit','guidance'}:continue
        if row.get('status')!='APPROVED' or product_id not in row.get('product_ids',[]):continue
        decision=snapshot.decisions.get(row.get('owner_decision_id'),{})
        if decision.get('status')!='CLOSED':continue
        if not row.get('source_ids') or any(s not in snapshot.data['sources'] for s in row['source_ids']):continue
        iid=row.get('ingredient_id')
        if iid and iid not in ctx.allowed_ingredient_ids:continue
        if row['kind']=='fact':
            # Existing approved/public factual wording; no new benefit approval inferred.
            facts=[f for f in (snapshot.product(product_id) or {}).get('facts', []) if f['fact_id'] in row.get('fact_ids',[])]
            if {f['fact_id'] for f in facts}!=set(row.get('fact_ids',[])):continue
            if not facts or any(f.get('disclosure_policy') not in {'PUBLIC','PUBLIC_WITH_APPROVED_WORDING'} for f in facts):continue
            if any(normalize(t) not in ctx.allowed_corpus for t in row['variants_ru']):continue
        else:
            approval=row.get('approval') or {}
            body={k:v for k,v in row.items() if k!='approval'}
            digest=hashlib.sha256(json.dumps(body,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
            if not approval.get('approved_by') or not approval.get('approved_at') or approval.get('content_sha256')!=digest:continue
            if decision.get('topic')!='approved_expert_explanation' or decision.get('explanation_sha256')!=digest:continue
        allowed_rules={'V-CLAIM'} if row['kind']=='ingredient_benefit' else ({'V-GENERAL'} if row['kind']=='guidance' else set())
        # Approval cannot exempt medical, numeric, restricted, identity or conflict rules.
        if any(set(verify(t,ctx,snapshot).rule_ids())-allowed_rules for t in row['variants_ru']):continue
        out.append(row)
    return out

def claim_variants(snapshot,product_ids):
    return [t for pid in product_ids for r in approved(snapshot,pid)
            if r['kind']=='ingredient_benefit' for t in r['variants_ru']]
