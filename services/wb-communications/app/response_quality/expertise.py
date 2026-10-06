"""Scoped, reviewed expertise. Pending proposals are never generation permission.

No external lookup. Approval must bind the immutable knowledge snapshot, source,
owner decision, product and exact reviewed semantic variants. No blanket claim switch.
"""
import hashlib
import json
import re
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
        decision=snapshot.decisions.get(row.get('owner_decision_id')) or data.get('owner_decisions',{}).get(row.get('owner_decision_id'),{})
        if decision.get('status')!='CLOSED':continue
        if not row.get('source_ids'):continue
        source_ok=True
        for sid in row['source_ids']:
            if sid in snapshot.data['sources']:continue
            source=data.get('supporting_sources',{}).get(sid,{})
            path=FILE.parent/source.get('file','')
            if (not path.is_file() or FILE.parent.resolve() not in path.resolve().parents
                or hashlib.sha256(path.read_bytes()).hexdigest()!=source.get('file_sha256')
                or source.get('file_sha256')!=row.get('evidence_sha256')):
                source_ok=False;break
        if not source_ok:continue
        if row.get('meaning_id') not in {None,'ha_hydration_v1','fragrance_perception_v1'}:continue
        if row.get('context_rule') not in {None,'positive_or_neutral_texture'}:continue
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


def in_context(row, message, snapshot=None):
    """A reviewed texture promise cannot contradict a buyer's negative experience."""
    if snapshot is not None and row.get('owner_key'):
        from app.v3.classifier import classify_rules
        safety=classify_rules(message,snapshot).safety
        if safety and safety.has_safety:
            return False  # consume existing calibration, never reclassify or lower it
    if row.get('context_rule') != 'positive_or_neutral_texture':
        return True
    from app.response_quality.core import aspects_of, _raw
    keys={a.key for a in aspects_of(message)}
    raw=normalize(_raw(message))
    negative=keys & {'sticky','drying','no_effect','product_disliked'}
    # Negation applies to the buyer's whole clause, not just one preceding word.
    texture_raw=re.sub(r'(?:не\s+оставля\w*|без)[^.!?;]{0,40}жирн\w*\s+пл[её]нк\w*','',raw)
    negative = negative or re.search(r'жирн\w*\s+пл[её]нк|плохо\s+впиты|не\s+впиты',texture_raw)
    return not negative and bool(keys & {'texture','non_sticky'} or re.search(r'текстур|впитыва',raw))


def matching_texts(row, text):
    """Bounded grammatical spans, never a blanket waiver of a claim family.

    А1 is approved as meaning. These recognise that meaning, not reply templates.
    Medical, numeric, causality and restricted checks always inspect original text.
    """
    n=normalize(text or '')
    found=[v.rstrip('.') for v in row['variants_ru'] if normalize(v.rstrip('.')) in n]
    patterns={
        'У2': r'сыворотка(?:,? которая)? помогает поддерживать увлажненность кожи',
        'У3': r'крем(?: для рук)?(?:,? который)? помогает защищать кожу рук от сухости',
        'М1': r'крем(?: для рук)? рассчитан на быстрое впитывание без жирной пленки',
        'ha_hydration_v1': r'гиалуроновая кислота(?:,? которая)? помогает поддерживать увлажненность кожи',
        'fragrance_perception_v1': r'(?:восприятие (?:аромата|ароматов) (?:действительно |очень |тоже )*индивидуально|индивидуальное восприятие аромата|(?:каждый (?:из нас )?воспринимает аромат|аромат каждый (?:из нас )?воспринимает) по-своему|аромат(?:ы)? (?:мы )?воспринима(?:ем|ются) (?:действительно |очень )*по-разному|аромат(?:ы)? (?:действительно |очень )*воспринимаются (?:действительно |очень )*индивидуально)',
    }
    pattern=patterns.get(row.get('meaning_id')) or patterns.get(row.get('owner_key'))
    if pattern: found.extend(m.group() for m in re.finditer(pattern,n))
    # Subject aliases preserve a whole-product proposition only when approval
    # binds exactly one product. An ingredient subject cannot become 'средство'.
    if row.get('kind')=='ingredient_benefit' and not row.get('ingredient_id') and len(row.get('product_ids',[]))==1:
        for wording in row['variants_ru']:
            canonical=normalize(wording).rstrip('.')
            m=re.match(r'(.+?) (помога\w*|рассчитан\w*)',canonical)
            if m:
                tail=canonical[m.end(1):]
                for match in re.finditer(r'\bсредство(?:,? которое)?'+re.escape(tail),n):
                    found.append(match.group())
    if row.get('meaning_id')=='fragrance_perception_v1':
        perception = r'(?:его|восприятие (?:аромата|ароматов))\s*(?:восприятие\s+)?(?:действительно |очень )*(?:индивидуально|у каждого(?: человека)? (?:может быть )?(?:свое|разным))'
        for m in re.finditer(perception,n):
            if not m.group().startswith('его'):
                found.append(m.group()); continue
            prefix=n[max(0,m.start()-180):m.start()]
            nouns=list(re.finditer(r'\b(?:аромат\w*|запах\w*|текстур\w*|крем\w*|средств\w*|сыворот\w*)',prefix))
            if nouns and re.fullmatch(r'аромат\w*|запах\w*',nouns[-1].group()) and len(re.findall(r'[.!?;]',prefix[nouns[-1].end():]))<=1:
                found.append(m.group())
    if row.get('meaning_id')=='fragrance_perception_v1':
        from app.v3.provenance import fragrance_perception_spans
        found.extend(fragrance_perception_spans(n))
    return list(dict.fromkeys(found))

def claim_variants(snapshot,product_ids):
    return [t for pid in product_ids for r in approved(snapshot,pid)
            if r['kind']=='ingredient_benefit' for t in r['variants_ru']]
