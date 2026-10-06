"""Question target, separate from adjacent words and product facts.

These are intent families, not answers or case IDs. Missing exact information
stays missing; a verified neighbouring fact cannot stand in for it.
"""
import re
from dataclasses import dataclass, field
from app.v3.text import normalize

UNKNOWN_WORDING = {
 'fragrance_longevity':'Подтверждённых данных о стойкости аромата на указанный срок у нас нет.',
 'fragrance_perception_causes':'Подтверждённых данных о причинах различий в восприятии этого аромата у нас нет.',
 'fragrance_popularity':'Подтверждённых данных о том, какой доле покупателей нравится этот аромат, у нас нет.',
 'amount':'Подтверждённое количество средства или нажатий на одно применение в документации не указано.',
 'frequency':'Подтверждённая частота применения в документации не указана.',
}
RESTRICTED_BOUNDARY = 'Точную концентрацию или её диапазон в подтверждённых данных для ответа покупателям не раскрываем.'

@dataclass
class QuestionIntent:
    intent_type: str
    fact_types: list = field(default_factory=list)
    product_id: str | None = None
    component_id: str | None = None
    application_areas: list = field(default_factory=list)
    unit_kind: str | None = None
    resolution: str = 'UNKNOWN'


def parse_intent(source, *, is_question=True):
    """One bounded intent contract, independent of product knowledge/LLM labels.

    Reported usage never becomes a verified instruction. Resolution is attached
    only after the planner resolves the actual product and required facts.
    """
    n = normalize(source)
    q = QuestionIntent('OTHER_DIRECT')
    if not is_question:
        return q
    scent = bool(re.search(r'аромат|запах|пахн|парфюмер', n))
    if scent:
        if re.search(r'держ\w*|стойк\w*|до\s+(?:утра|вечера)|сутк|час', n):
            q.fact_types.append('fragrance_longevity')
        if re.search(r'от\s+чего|завис\w*|температур|тип\w*\s+кож', n):
            q.fact_types.append('fragrance_perception_causes')
        if re.search(r'(?:почти\s+)?всем|большинств|покупател\w*\s+(?:нрав|люб)|дол\w*\s+покупател', n):
            q.fact_types.append('fragrance_popularity')
    concentration = bool(re.search(r'процент|концентрац|точн\w*\s+дол|диапазон\w*\s+(?:содержан|концентрац)|в\s+каком\s+количестве\s+содерж', n))
    if concentration:
        q.fact_types.append('ingredient_concentration')
    if not concentration and not re.search(r'сколько\s+раз', n) and re.search(r'сколько|какое\s+количество|доз\w*', n) and re.search(r'нажат|нанос|нанес|примен|кап(?:л|ел)|порц|средств|использ', n):
        q.fact_types.append('amount')
        q.unit_kind = ('DROPS' if re.search(r'кап(?:л|ел)', n) else
                       'PUMPS' if re.search(r'нажат', n) else 'GENERIC_AMOUNT')
    if re.search(r'как\s+часто|частот\w*|сколько\s+раз|раз\s+в\s+день|каждый\s+день|ежедневно', n):
        q.fact_types.append('frequency')
        q.unit_kind = q.unit_kind or 'FREQUENCY'
    areas = {'face': r'лиц\w*', 'body': r'тел\w*', 'hands': r'рук\w*',
             'hair': r'волос\w*', 'feet': r'ног\w*', 'nails': r'ногт\w*',
             'eye_area': r'вокруг\s+глаз|област\w*\s+глаз'}
    q.application_areas = [area for area, rx in areas.items() if re.search(rx, n)]
    use = re.search(r'использ|примен|нанос|нанес|предназнач|мазать|пользоваться|подойдет\s+для|подходит\s+для|можно\s+(?:на|для)|для\s+какой\s+зоны', n)
    if use and q.application_areas and not q.fact_types:
        q.fact_types.append('intended_use')
    if re.search(r'вид\w*\s+(?:этого\s+)?средств|как\s+называется\s+продукт|(?:сыворот\w*|пенк\w*|крем\w*)\s+или\s+(?:крем\w*|порош\w*|пудр\w*)|(?:готов\w*\s+)?пенк\w*\s+или\s+порош', n):
        q.fact_types.append('product_identity')
    if not q.fact_types and re.search(r'состав|\binci\b', n):
        q.fact_types.append('composition')
    types = {'intended_use': 'INTENDED_USE', 'amount': 'AMOUNT',
             'frequency': 'FREQUENCY', 'fragrance_longevity': 'LONGEVITY',
             'composition': 'COMPOSITION', 'ingredient_concentration': 'CONCENTRATION',
             'product_identity': 'PRODUCT_IDENTITY'}
    q.fact_types = list(dict.fromkeys(q.fact_types))
    q.intent_type = types.get(next(iter(q.fact_types), ''), 'OTHER_DIRECT')
    return q


def targets(source):
    return parse_intent(source).fact_types


def usage_question(source):
    return 'intended_use' in parse_intent(source).fact_types


def unknown_wording(fact_type, intent):
    if fact_type == 'amount':
        unit = {'DROPS': 'капель', 'PUMPS': 'нажатий'}.get(intent.unit_kind, 'средства')
        return f'Подтверждённое количество {unit} в доступной документации не указано.'
    return UNKNOWN_WORDING[fact_type]


def current_question_plan(snapshot, product_ids, source):
    """Rebuild the hard route independently; absence of data is not a fact claim.

    Return the independently resolved question state, never a model assertion.
    A conflict, special route or unresolved component never grants this channel.
    """
    if len(product_ids) != 1 or not targets(source):
        return None
    n=normalize(source)
    if '?' not in source and not re.search(r'\b(?:сколько|как\s+часто|от\s+чего|будет\s+ли|правда\s+ли|какая\s+стойкость|можно|подойдет)\b',n):
        return None
    pid=product_ids[0]; product=snapshot.product(pid) or {}
    if (product.get('identity_status') != 'VERIFIED'
            or product.get('customer_fact_generation') == 'RESTRICTED'):
        return None
    from app.v3.classifier import classify_rules
    from app.v3.planner import plan
    from app.v3.resolver import resolve
    ident=next((i for i in snapshot.data['identifiers'] if i['product_id']==pid and i['id_type']=='wb_nm_id'),None)
    if not ident:
        return None
    msg={'text':source,'entity_type':'question','nm_id':ident['value']}
    hard=plan(snapshot,resolve(snapshot,nm_id=ident['value'],marketplace='wb'),classify_rules(msg,snapshot),msg)
    if hard.strategy != 'UNKNOWN_FACT':
        return None
    resolved_products={r.product_id for r in hard.resolved}
    if len(resolved_products)!=1:
        return None
    target=snapshot.product(next(iter(resolved_products))) or {}
    if (target.get('kind')!='single' or target.get('identity_status')!='VERIFIED'
            or target.get('customer_fact_generation')!='ALLOWED'):
        return None
    return hard


def ordinary_unknown_texts(snapshot, product_ids, source):
    hard = current_question_plan(snapshot, product_ids, source)
    if hard is None:
        return []
    denials = [unknown_wording(r.fact_type, hard.question_intent) for r in hard.resolved
               if r.state == 'UNKNOWN' and r.reason in {'NO_FACT', 'POLICY_UNKNOWN'}
               and r.fact_type in UNKNOWN_WORDING]
    # The same independently rebuilt, parameterized intended-use boundary.
    # Only its exact approved denial is permitted; adjacent advice is not.
    denials.extend(r.customer_value_ru for r in hard.resolved
                   if r.state == 'UNKNOWN' and r.fact_type == 'intended_use'
                   and r.reason == 'EXPANDED_USE_NOT_CONFIRMED'
                   and r.template_id == 'T-INTENDED-USE' and r.customer_value_ru)
    return denials

def diagnostics(text,source,is_question):
    if not is_question:return []
    requested=targets(source);n=normalize(text or '');out=[]
    boundary=bool(re.search(r'не\s+(?:раскрываем|подтвержден|указан)|данных[^.!?]{0,100}\bнет\b|не\s+можем\s+подтверд',n))
    if any(t.startswith('fragrance_') for t in requested) and not boundary:
        out += ['DIRECT_QUESTION_UNANSWERED','ADJACENT_FACT_SUBSTITUTION']
    if 'amount' in requested and not (boundary or re.search(r'\d+\s*(?:нажат|капл)|количество[^.!?]{0,50}документац',n)):
        out += ['DIRECT_QUESTION_UNANSWERED','ADJACENT_FACT_SUBSTITUTION']
    if 'ingredient_concentration' in requested and not boundary:
        out.append('RESTRICTED_QUESTION_NOT_ACKNOWLEDGED')
    return list(dict.fromkeys(out))
