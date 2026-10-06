"""Product-use propositions need public verified support, including in wishes.

Product type authorizes generic cosmetic conversation, not an inferred purpose.
This adds no knowledge: capabilities are projected from the immutable snapshot.
"""
import re
from app.v3.text import normalize

DOMAINS = {
    'makeup_base': r'баз\w*\s+(?:под|для)\s+макияж\w*',
    'makeup_removal': r'(?:сняти\w*|удалени\w*|снимать|смыва\w*)[^.!?;]{0,25}макияж\w*|демакияж\w*',
    'makeup': r'макияж\w*',
    'cleansing': r'очищени\w*|очища\w*|умыва\w*',
    'spf': r'\bspf\b|солнцезащит\w*|защит\w*\s+от\s+(?:солнц\w*|уф\w*)',
    'hair': r'волос\w*',
    'lips': r'\bгуб\w*',
    'eye_area': r'вокруг\s+глаз\w*|област\w*[^.!?;]{0,12}глаз\w*|\bвек(?:и|о|ах)\b',
    'body': r'\bтел(?:о|а|у|ом|е)\b|\bспин\w*|\bплеч\w*',
    'face': r'\bлиц(?:о|а|у|ом|е)\b',
    'hands': r'\bрук(?:и|ам|ах|у)?\b|\bкист\w*',
    'feet': r'\bног(?:и|ам|ах)?\b|\bстоп(?:ы|ам)?\b',
    'nails': r'\bногт\w*',
}
SPECIAL_MAKEUP = {'makeup_base','makeup_removal'}
QUALIFIED_FACTS = {'purpose','product_purpose','routine_role','compatibility','suitability','skin_type_suitability','intended_use','directions'}


def domains(text):
    n=normalize(text);found={k for k,rx in DOMAINS.items() if k!='makeup' and re.search(rx,n)}
    residual=n
    for k in SPECIAL_MAKEUP:residual=re.sub(DOMAINS[k],' ',residual)
    if re.search(DOMAINS['makeup'],residual):found.add('makeup')
    return found


def capabilities(snapshot, product_ids):
    from app.v3.planner import CUSTOMER_DISCLOSURE
    scope=[]
    for pid in product_ids:
        scope.append(pid)
        if (snapshot.product(pid) or {}).get('kind')=='bundle':scope.extend(snapshot.components(pid))
    types=set();areas=set();uses=set();names=[];support=[];roles=[];usage=[]
    for pid in dict.fromkeys(scope):
        p=snapshot.product(pid) or {}
        if p.get('identity_status')!='VERIFIED' or p.get('customer_fact_generation')=='RESTRICTED':continue
        if p.get('kind')=='single':types.add(p.get('product_type'))
        if p.get('customer_name_ru'):names.append(p['customer_name_ru'])
        for f in p.get('facts',[]):
            if f.get('disclosure_policy') not in CUSTOMER_DISCLOSURE or f.get('fact_status') not in {'VERIFIED','OWNER_APPROVED','COMMERCIAL_VERIFIED'}:continue
            if f.get('fact_type')=='intended_use' and isinstance(f.get('value'),list):areas.update(f['value'])
            if f.get('fact_type') in {'compatibility','suitability','skin_type_suitability','routine_role'} and f.get('customer_value_ru'):
                roles.append(f['customer_value_ru'])
            if f.get('fact_type') == 'directions' and f.get('customer_value_ru'):
                usage.append(f['customer_value_ru'])
            if f.get('fact_type') in QUALIFIED_FACTS:
                text=f.get('customer_value_ru') or ''
                # A negative/unknown proposition does not establish positive use.
                if not re.search(r'не\s+(?:подход|рекоменд|предназнач)|не\s+можем|не\s+подтверж|нет\s+данн',normalize(text)):
                    found=domains(text);uses.update(found);support.extend({'domain':d,'fact_id':f['fact_id'],'source_id':f.get('source_id')} for d in sorted(found))
    return {'product_types':sorted(t for t in types if t),'public_identity':names,
            'application_areas':sorted(areas),'use_domains':sorted(uses|areas),
            'generic_cosmetic_context':bool(types),'source_support':support,'explicit_roles':roles,'public_usage_texts':usage}


def unsupported_use(text, caps):
    """Return only unsupported assignments; affective context is not a new use.

    This is a bounded domain grammar, not a universal natural-language proof.
    Numeric/medical/guidance/suitability rules still inspect the original text.
    """
    n=normalize(text);out=[]
    for clause in re.split(r'[.!?;]',n):
        # Epistemic spans may be blanked by the caller only after independent
        # product/target/state verification. Negation in raw prose is no grant.
        for domain in domains(clause):
            if domain in caps.get('use_domains',[]):continue
            m=re.search(DOMAINS[domain],clause)
            out.append({'domain':domain,'span':m.group(),'reason':'PRODUCT_USE_NOT_IN_VERIFIED_CAPABILITIES'})
        # New role/compatibility/suitability beyond an anatomical domain.
        role=re.search(r'\b(?:база|основа)\s+(?:под|для)\s+\w+|(?:сочетается|совместим\w*)\s+с\s+\w+|(?:подходит|подойдет)\s+(?:всем|любому)\b|(?:подходит|подойдет|предназначен\w*|использ[оу]\w*|примен\w*)\s+(?:для|при|как|всем|любому)\b\s*(?:[а-я-]+\s*){1,7}|(?:крем|сыворотка|пудра|тоник|средство)\s+(?:для|как)\s+(?:[а-я-]+\s*){1,7}',clause)
        if role and (not domains(role.group()) or re.search(r'сочетается|совместим',role.group())) and not re.search(r'подар',clause):
            if not any(normalize(x) in clause for x in caps.get('explicit_roles',[])):
                out.append({'domain':'role_or_suitability','span':role.group(),'reason':'ROLE_NOT_IN_VERIFIED_CAPABILITIES'})
        # Instructions/amounts are substantive even without a new body area.
        # Affective wishes grant no regimen; only their non-factual payload is free.
        from app.v3.provenance import semantic_segments
        for payload in semantic_segments(clause):
            for predicate in payload['predicates']:
                if predicate['kind'] != 'PRODUCT_USE_CLAIM':continue
                # A verified purpose authorizes the same purpose assignment,
                # not an imperative, schedule, amount or a new method of use.
                from app.v3.provenance import FREQUENCY
                areas=domains(payload['payload'])
                purpose_only=bool(areas) and areas <= set(caps.get('use_domains',[])) and bool(re.search(r'можно\s+использовать\s+для',payload['payload'])) and not re.search(FREQUENCY,payload['payload'])
                if not purpose_only and not any(normalize(x) == payload['payload'] for x in caps.get('public_usage_texts', [])):
                    out.append({'domain':'usage_instruction','span':predicate['span'],'reason':'USAGE_NOT_IN_VERIFIED_CAPABILITIES'})
        # Gifting is a purchase context, not an application area. Reflection and
        # generic affective wishes add no suitability claim; universal "fits" does.
        if re.search(r'подар\w*',clause) and re.search(r'универсальн|(?:подходит|подойдет)\s+(?:для|в)\s+подар',clause):
            if 'gift_suitability' not in caps.get('use_domains',[]):
                out.append({'domain':'gift_suitability','span':clause.strip(),'reason':'GIFT_SUITABILITY_NOT_VERIFIED'})
        if re.search(r'\bуход\w*',clause) and not caps.get('generic_cosmetic_context'):
            out.append({'domain':'cosmetic_context','span':clause.strip(),'reason':'VERIFIED_COSMETIC_TYPE_REQUIRED'})
    return out
