"""Epistemic use boundaries are knowledge-state propositions, not use facts.

Linguistic type AND independently resolved product/area state are required.
Only the supported proposition span is exempt from use checks; other rule
families keep inspecting the original text. No routing or registry authority.
"""
from dataclasses import dataclass
import re
from app.v3.text import normalize
from app.v3.grounding import domains

AFFIRMATIVE_PRODUCT_USE = 'AFFIRMATIVE_PRODUCT_USE'
EPISTEMIC_UNKNOWN_BOUNDARY = 'EPISTEMIC_UNKNOWN_BOUNDARY'
NEGATIVE_PRODUCT_FACT = 'NEGATIVE_PRODUCT_FACT'
DIRECTIVE = 'DIRECTIVE'

APPLICATION_NOUN = r'применени\w*|использовани\w*|нанесени\w*'
INSTRUCTION = r'\b(?:наносите|нанесите|используйте|применяйте|распределите|втирайте)\b'
NEGATIVE_USE = r'не\s+(?:предназначен\w*|подходит|подойдет)|нельзя\s+(?:наносить|использовать|применять)'
AFFIRMATIVE_USE = r'можно\s+(?:наносить|использовать|применять)|подходит|подойдет|предназначен\w*'
EPISTEMIC = (r'не\s+(?:подтвержден\w*|указан\w*|описан\w*|документирован\w*)|'
             r'не\s+можем[^.!?;]{0,30}подтвердить|подтвердить[^.!?;]{0,100}не\s+можем|'
             r'не\s+подтверждаем|(?:данн\w*|сведени\w*|информаци\w*)[^.!?;]{0,100}(?:\bнет\b|отсутств\w*)|'
             r'\bнет\b[^.!?;]{0,35}(?:данн\w*|сведени\w*|информаци\w*)')
# Reset at an independent clause, not at every comma/appositive modifier.
RESET = (r',\s*(?=(?:но|а|зато|однако|поэтому|поскольку|и\s+(?:крем|средство|используйте|наносите)|'
         r'в\s+составе|крем\b|средство\b|не\s+(?:наносите|используйте|применяйте))\b)|'
         r'\s+\b(?:но|зато|поэтому|поскольку|однако)\b\s*')

@dataclass
class UseProposition:
    start: int
    end: int
    span: str
    kind: str
    targets: set


def propositions(text):
    n = normalize(text)
    out = []
    for sentence in re.finditer(r'[^.!?;]+', n):
        boundaries = [sentence.start()] + [sentence.start()+m.end() for m in re.finditer(RESET, sentence.group())] + [sentence.end()]
        for start, end in zip(boundaries, boundaries[1:]):
            span = n[start:end].strip(' ,')
            if not span:
                continue
            areas = domains(span)
            if re.search(INSTRUCTION, span):
                kind = DIRECTIVE
            elif re.search(NEGATIVE_USE, span):
                kind = NEGATIVE_PRODUCT_FACT
            elif re.search(AFFIRMATIVE_USE, span):
                kind = AFFIRMATIVE_PRODUCT_USE
            elif re.search(APPLICATION_NOUN, span) and re.search(EPISTEMIC, span) and areas:
                kind = EPISTEMIC_UNKNOWN_BOUNDARY
            else:
                continue
            out.append(UseProposition(start, end, span, kind, areas))
    return out


def resolved_permissions(snapshot, product_ids, *, source='', hard_plan=None):
    """Prove a single resolved product/component and the requested UNKNOWN area.

    A fabricated Plan/permission label alone is insufficient: re-resolve the
    same immutable fact and provenance. A free-text candidate never supplies
    its own intent/state. Component ambiguity never creates this permission.
    """
    if len(product_ids) != 1:
        return []
    if hard_plan is None:
        from app.v3.direct_questions import current_question_plan
        hard_plan = current_question_plan(snapshot, product_ids, source)
    intent = getattr(hard_plan, 'question_intent', None)
    if (hard_plan is None or hard_plan.strategy != 'UNKNOWN_FACT' or intent is None
            or intent.intent_type != 'INTENDED_USE' or intent.resolution != 'UNKNOWN'
            or intent.product_id != product_ids[0]):
        return []
    resolved_ids = {r.product_id for r in hard_plan.resolved}
    if len(resolved_ids) != 1:
        return []
    pid = next(iter(resolved_ids)); product = snapshot.product(pid) or {}
    if (product.get('kind') != 'single' or product.get('identity_status') != 'VERIFIED'
            or product.get('customer_fact_generation') != 'ALLOWED'):
        return []
    root = snapshot.product(product_ids[0]) or {}
    if (root.get('identity_status') != 'VERIFIED'
            or root.get('customer_fact_generation') == 'RESTRICTED'
            or (pid != product_ids[0] and pid not in snapshot.components(product_ids[0]))):
        return []
    from app.v3.planner import resolve_fact
    from app.v3.classifier import Classification
    rows = []
    for r in hard_plan.resolved:
        if (r.fact_type != 'intended_use' or r.state != 'UNKNOWN'
                or r.subject not in intent.application_areas):
            continue
        check = resolve_fact(snapshot, pid, r.fact_type, r.subject, Classification('question', None))
        if (check.state != 'UNKNOWN' or check.reason != r.reason
                or check.fact_ids != r.fact_ids or check.source_ids != r.source_ids):
            continue
        rows.append({'product_id': pid, 'root_product_id': product_ids[0],
                     'component_id': intent.component_id, 'target': r.subject,
                     'resolution': 'UNKNOWN', 'fact_ids': r.fact_ids, 'source_ids': r.source_ids})
    return rows


def matches_target(proposition, permissions, snapshot):
    if not permissions or not proposition.targets:
        return False
    if not proposition.targets <= {p['target'] for p in permissions}:
        return False
    pid = permissions[0]['product_id']; product = snapshot.product(pid) or {}
    ptype = product.get('product_type')
    allowed_types = {ptype, 'cream'} if ptype in {'hand_cream', 'body_cream'} else {ptype}
    # A generic noun shared with the resolved type («крем») is an anaphoric
    # reference to the already-resolved product, as in V-ID same_word: only a
    # pattern the resolved type does not own can name a different product.
    # Identity comes from the permission anchor; the noun never re-resolves it.
    words = snapshot.policy['product_type_words']
    own = {p for t in allowed_types for p in words.get(t, [])}
    if any(re.search(p, proposition.span) for t, rx in words.items()
           if t not in allowed_types for p in rx if p not in own):
        return False
    root = permissions[0]['root_product_id']
    # A named sibling cannot borrow the uniquely resolved component's UNKNOWN.
    for sibling in snapshot.components(root):
        if sibling == pid:
            continue
        p = snapshot.product(sibling) or {}
        labels = [p.get('customer_name_ru') or '']
        labels += [re.sub(r'^аромат\s+', '', normalize(f.get('customer_value_ru')))
                   for f in snapshot.facts(sibling, 'fragrance_profile')
                   if f.get('fact_status') == 'VERIFIED' and f.get('disclosure_policy') in {'PUBLIC','PUBLIC_WITH_APPROVED_WORDING'}]
        if any(label and normalize(label) in proposition.span for label in labels):
            return False
    return True
