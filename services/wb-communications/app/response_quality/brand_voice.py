"""Editorial heuristics, not policy permission or a simulated human verdict.

Render only experience acknowledgement and explicit approved context. A missing
approved explanation remains visible as a quality gap; style never forbids publish.
"""
import re
from app.v3.text import normalize

AXES = ('acknowledgment_specificity', 'warmth', 'empathy', 'usefulness',
        'product_relevance', 'factual_grounding', 'naturalness', 'brand_voice',
        'future_buyer_value', 'personalization', 'repetition_penalty',
        'robotic_language_penalty', 'unnecessary_defensiveness',
        'overexplaining_penalty', 'missed_customer_signal_penalty')


def render(msg, p, *, safe_v3_draft=None):
    from app.response_quality.core import _with_name, _raw, sha
    if p.human_reason:
        return None
    if p.direct_answer:
        return p.direct_answer
    seed=int(sha(_raw(msg)+str(msg.get('communication_id') or msg.get('source_id') or ''))[:8],16)
    keys = {a.key for a in p.aspects}
    negative = keys & {'fragrance_harsh','fragrance_disliked','sticky','drying',
                       'no_effect','product_disliked','price','packaging_inconvenient','dispenser_inconvenient'}
    positive = []
    if 'softness' in keys: positive.append('мягкость средства Вам понравилась')
    if 'texture' in keys: positive.append('Вы оценили текстуру')
    if 'non_sticky' in keys: positive.append('Вы не ощущаете липкости после нанесения')
    if 'fragrance_liked' in keys: positive.append('аромат Вам понравился')
    if 'result_liked' in keys: positive.append('Вы довольны результатом применения')
    if 'product_liked' in keys and not positive: positive.append('само средство Вам понравилось')
    parts = []
    if positive:
        parts.append('Рады, что ' + ', а '.join(positive) + '.')
    elif not negative:
        parts.append('Спасибо за высокую оценку!' if (msg.get('rating') or 0)>=4 else 'Спасибо, что поделились впечатлением.')
    if 'repeat_purchase' in keys:
        parts.append('Спасибо, что снова выбираете EVETIS — нам очень приятно Ваше доверие.')
    if 'fragrance_harsh' in keys:
        parts.append('Жаль, что аромат оказался для Вас резковатым.')
    if 'fragrance_disliked' in keys:
        parts.append('Жаль, что аромат Вам не понравился.')
    if 'sticky' in keys: parts.append('Жаль, что после нанесения Вы ощущаете липкость.')
    if 'drying' in keys: parts.append('Жаль, что после применения Вы ощущаете сухость.')
    if 'no_effect' in keys: parts.append('Жаль, что Вы не увидели ожидаемого результата.')
    if 'product_disliked' in keys and not negative-{'product_disliked'}:
        parts.append('Жаль, что покупка Вас разочаровала. Расскажите, пожалуйста, что именно не понравилось.')
    if 'price' in keys: parts.append('Ваше замечание о цене тоже услышали.')
    for a in p.aspects:
        if a.key in {'packaging_inconvenient','dispenser_inconvenient'}:
            parts.append(a.response + ' Расскажите, пожалуйста, что именно затрудняет использование.')
    # At most one relevant explanation. Existing fragrance profile is a FACT,
    # never a new claim about longevity, intensity, individual tolerance or benefit.
    for row in p.explanations:
        wording=row['variants_ru'][int(sha(_raw(msg))[:4],16)%len(row['variants_ru'])]
        parts.append(('В этой версии средства — '+wording.rstrip('.')+'.') if row['kind']=='fact' else wording)
    if 'recommendation' in keys: parts.append('Спасибо за Вашу рекомендацию!')
    if 'delivery' in keys: parts.append('Здорово, что доставка была быстрой.')
    if negative and not any(k in keys for k in ('packaging_inconvenient','dispenser_inconvenient','product_disliked')):
        closes = ('Спасибо, что рассказали об этом: Ваше впечатление для нас важно.',
                  'Спасибо за честный отзыв — Ваше впечатление для нас важно.')
        parts.append(closes[int(sha(_raw(msg))[:4],16)%len(closes)])
    elif 'future_purchase' in keys or not _raw(msg):
        parts.append('Будем рады видеть Вас снова!')
    elif not negative and not keys & {'repeat_purchase','recommendation','delivery'}:
        closes=('Спасибо, что выбрали EVETIS!', 'Спасибо, что поделились этим впечатлением!',
                'Нам очень приятно читать такие отзывы!')
        parts.append(closes[seed%len(closes)])
    return _with_name(msg,' '.join(parts))


def assess(text,msg,p,hard_policy,*,previous_answers=()):
    from app.response_quality.core import QualityReport, _raw, _name
    n=normalize(text or '')
    missing=[a.key for a in p.aspects if a.priority=='MUST_ADDRESS' and a.coverage_patterns
             and not all(re.search(rx,n) for rx in a.coverage_patterns)]
    negative=bool({a.key for a in p.aspects}&{'fragrance_harsh','fragrance_disliked','sticky','drying','no_effect',
                  'product_disliked','price','service_damage','packaging_inconvenient','dispenser_inconvenient'})
    direct=p.level=='DIRECT'; hard=p.route in {'SAFETY_TEMPLATE','HUMAN_REVIEW'}
    detail=bool({a.key for a in p.aspects}&{'softness','texture','fragrance_harsh','no_effect','sticky','drying'})
    # Bare ingredient presence is grounding, not useful explanation. This gap is
    # intentional until owner approves expertise; never invent context to score GOOD.
    explanation=any(normalize(v) in n for r in p.explanations for v in r['variants_ru'])
    action=bool(re.search(r'обращени|приложите|расскажите|уточните|подтвержд|документаци',n))
    useful=bool(text) and (not detail or explanation or action or hard)
    robotic=bool(re.search(r'благодарим за обратную связь|данная продукция|к сведению|понимаем, что .*показал|спасибо, что отметили',n))
    generic=n in {'спасибо за отзыв.','рады, что вам понравилось.','в составе есть гиалуроновая кислота.'}
    warm=bool(re.search(r'спасибо|жаль|рады|приятно|доверие',n)) or direct or hard
    checks={
        'acknowledgment_specificity':bool(text) and not missing and not generic,
        'warmth':warm,
        'empathy':not negative or bool(re.search(r'жаль|услышали|извин|сожал',n)) or hard,
        'usefulness':useful and not generic,
        'product_relevance':bool(text) and len(p.facts)<=p.information_budget,
        'factual_grounding':hard_policy.get('verdict') in {'PASS','INFO','WARNING'},
        'naturalness':not robotic and not generic,
        'brand_voice':warm and not robotic and not generic,
        'future_buyer_value':not detail or explanation or action or hard,
        'personalization':not missing and (direct or hard or not _name(msg) or normalize(_name(msg)) in n),
        'repetition_penalty':n not in {normalize(t) for t in previous_answers if t},
        'robotic_language_penalty':not robotic,
        'unnecessary_defensiveness':not bool(re.search(r'вы неправильно|сами виноваты|это нормально|вы должны понимать',n))
            and not (not hard and bool(re.search(r'врач|медицин|дерматолог',n))),
        'overexplaining_penalty':len(text or '') <= (1000 if direct or hard or p.route=='SERVICE' else 600)
            and not bool(re.search(r'купите|советуем приобрести',n)),
        'missed_customer_signal_penalty':not missing,
    }
    reasons=['missing_aspect:'+a for a in missing]+[k for k,v in checks.items() if not v]
    if detail and not explanation and not action and not hard: reasons.append('approved_explanation_missing')
    return QualityReport('GOOD' if all(checks.values()) else 'NEEDS_IMPROVEMENT',
                         {k:'GOOD' if v else 'NEEDS_IMPROVEMENT' for k,v in checks.items()},reasons)
