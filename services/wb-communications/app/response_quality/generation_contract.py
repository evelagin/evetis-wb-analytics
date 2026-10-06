"""Closed-world generation admission, before an answer reaches prepare's gate.

Global knowledge is used only to detect unavailable meanings, never sent to the
model as permission. Publication independently validates current product policy.
"""
import re
from app.v3.text import normalize


class GenerationContractError(ValueError):
    def __init__(self, violations):
        self.violations=violations
        super().__init__('generation violated case capability contract')


def required_subjects(row):
    """Explicit semantic subjects from the approved proposition, no owner-key switch."""
    if row.get('kind')!='ingredient_benefit':return []
    subjects=[]
    for wording in row.get('variants_ru',[]):
        m=re.match(r'(.+?)\s+(?:помога\w*|рассчитан\w*)\b',normalize(wording))
        if m:subjects.append(m.group(1))
    # A single verified product may also be referred to by the explicit noun
    # "средство". An ingredient benefit may not be reassigned to the whole product.
    if not row.get('ingredient_id') and len(row.get('product_ids',[]))==1:subjects.append('средство')
    return sorted(set(subjects))


def unavailable_expertise(text, supplied):
    from app.response_quality.expertise import load_registry,matching_texts
    available={r['explanation_id'] for r in supplied};n=normalize(text)
    out=[]
    for row in load_registry()['explanations']:
        if row.get('status')!='APPROVED' or row.get('kind')=='fact' or row['explanation_id'] in available:continue
        found=matching_texts(row,text)
        if row.get('meaning_id')=='fragrance_perception_v1':
            # Recognition for containment is intentionally wider than an approval
            # span. It cannot grant a rule exemption or expand A1's allowed meaning.
            found=found or (re.search(r'аромат|запах',n) and re.search(r'восприяти\w*[^.!?]{0,50}индивидуальн|воспринима\w*[^.!?]{0,40}(?:по-разному|по-своему)|восприяти\w*[^.!?]{0,50}у каждого',n))
        if found:out.append({'rule_id':'GENERATION_CAPABILITY_SCOPE','explanation_id':row['explanation_id']})
    return out


def validate(text, plan):
    out=unavailable_expertise(text,plan.explanations)
    from app.v3.grounding import unsupported_use
    out.extend({'rule_id':'GENERATION_PRODUCT_USE',**v} for v in unsupported_use(text,plan.product_capabilities))
    return out
