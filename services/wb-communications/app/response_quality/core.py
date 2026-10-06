"""Bounded planning and independent policy checks; snapshot + approval registry are truth.

No clients, credentials, marketplace writes or ledger writes are reachable here. Quality
is a diagnostic checklist, not a permission to publish and not a hard-policy verdict.
"""
from __future__ import annotations
import hashlib
import re
from dataclasses import asdict, dataclass, field, replace
from types import SimpleNamespace
from app.response_quality import VERSION
from app.services.publication_policy import validate_for_publication
from app.v3 import classifier, planner, resolver, verifier
from app.v3.text import normalize


def sha(text):
    return hashlib.sha256((text or "").encode()).hexdigest()


@dataclass
class Aspect:
    key: str
    priority: str                    # MUST_ADDRESS | OPTIONAL | IGNORE
    response: str
    coverage_patterns: tuple[str, ...]


@dataclass
class VoicePlan:
    route: str
    level: str
    information_budget: int          # maximum independent product facts, never a quota
    aspects: list[Aspect]
    facts: list[dict] = field(default_factory=list)
    direct_answer: str | None = None
    human_reason: str | None = None
    explanations: list[dict] = field(default_factory=list)
    product_types: list[str] = field(default_factory=list)
    clarifications: list[dict] = field(default_factory=list)
    block_reason: str | None = None
    product_capabilities: dict = field(default_factory=dict)
    service_premises: list = field(default_factory=list)
    question_targets: list = field(default_factory=list)


@dataclass
class QualityReport:
    verdict: str                     # GOOD | NEEDS_IMPROVEMENT
    dimensions: dict[str, str]
    reasons: list[str]


@dataclass
class PreparedResponse:
    status: str                      # READY | HUMAN_REVIEW
    text: str | None
    repaired: bool
    original_policy: dict
    final_policy: dict
    quality: QualityReport
    plan: VoicePlan
    reasons: list[str]

    def metadata(self):
        # Private operator metadata; original_block spans must never be logged/outboxed.
        return {"version": VERSION, "status": self.status,
                "operator_state": ("HUMAN_REVIEW" if self.plan.human_reason else
                                   "BLOCK" if self.status!='READY' else
                                   "WARNING" if self.quality.verdict!='GOOD' or self.final_policy.get("verdict")=="WARNING" else "READY"),
                "approved_explanation_ids": [r['explanation_id'] for r in self.plan.explanations],
                "original_block": {'violations':self.original_policy.get('violations',[]),
                                   'violation_spans':self.original_policy.get('violation_spans',[])}, "repaired": self.repaired,
                "original_text_sha256": self.original_policy.get("text_sha256"),
                "final_text_sha256": sha(self.text), "reasons": self.reasons,
                "quality": asdict(self.quality), "information_budget": self.plan.information_budget,
                "fact_ids": [f["fact_id"] for f in self.plan.facts]}


def _name(msg):
    n = (msg.get("buyer_name") or "").strip().split(" ")[0]
    return n if re.fullmatch(r"[А-Яа-яЁёA-Za-z-]{2,30}", n) else ""


def _with_name(msg, text):
    n = _name(msg)
    return f"{n}, {text[:1].lower()}{text[1:]}" if n and text else text


def _raw(msg):
    return " ".join(str(msg.get(k) or "") for k in ("text", "pros", "cons")).strip()


# The acknowledgement is explicitly about the buyer's experience, not a new efficacy claim.
_ASPECTS = [
    ("product_liked", r"отлич|понрав|нрав|хорош|супер|довольн|обож|любов", "Рады, что покупка Вам понравилась.", (r"рад|приятн", r"понрав|нравится|впечатлен|оценк|покупк|мягк|текстур|липк|довольн|результат|фаворит|покорил|выбором|качество|любим")),
    ("softness", r"мягк", "Очень приятно, что Вы отметили мягкость средства.", (r"мягк",)),
    ("non_sticky", r"\bне\s+лип|нелипк|без\s+липк|\bне\s+оставля[^.!?]{0,30}липк", "Рады, что средство не показалось Вам липким.", (r"липк|липким",)),
    ("texture", r"(?:приятн|нежн|легк|лёгк|нрав)\w*\s+текстур|текстур\w*[^.!?]{0,20}(?:приятн|нежн|легк|лёгк|нрав)|(?:быстро|хорошо)\s+впитыва", "Рады, что текстура Вам понравилась.", (r"текстур|впитыва",)),
    ("fragrance_harsh", r"резк\w*[^.!?]{0,20}(запах|аромат)|(запах|аромат)[^.!?]{0,20}резк", "Понимаем, что аромат показался Вам резким.", (r"аромат|запах", r"резк")),
    ("fragrance_liked", r"(?:приятн|хорош|нежн)\w*\s+(запах|аромат)|(запах|аромат)[^.!?]{0,30}(?:понрав|хорош|приятн|нежн)|приятно\s+пах", "Рады, что аромат Вам понравился.", (r"аромат|запах", r"понрав|приятн|покорил")),
    ("recommendation", r"рекомендую|советую", "Спасибо за рекомендацию.", (r"рекоменд(?:ац|овать|уете)|советуете",)),
    ("delivery", r"быстр\w*[^.!?]{0,20}достав|достав\w*[^.!?]{0,20}быстр", "Спасибо, что отметили быструю доставку.", (r"достав|заказ[^.!?]{0,25}добрал", r"быстр|оператив")),
    ("repeat_purchase", r"(беру|покупаю|заказываю)[^.!?]{0,30}(снова|повтор|не\s+перв|втор|трет)|не\s+перв\w*\s+раз|постоянно\s+(беру|покупаю|заказываю)", "Спасибо, что снова выбрали EVETIS.", (r"снова|повтор|не\s+перв|возвращаетесь|в\s+(?:третий|второй)\s+раз|проверенный\s+фаворит",)),
    ("no_effect", r"не\s+увидел\w*[^.!?]{0,20}(эффект|результат)|нет\s+(эффект|результат)|без\s+результат|не\s+работает", "Жаль, что результат не совпал с Вашими ожиданиями.", (r"результат|ожидан",)),
    ("product_disliked", r"\bне\s+(?:понрав|нрав)|разочар|ужас|плох", "Жаль, что покупка Вас разочаровала.", (r"жаль|разочар|ожидан",)),
    ("price", r"\bдорого\b|\bдороговат|цен[ауы]\s+(высок|завыш)|подешевел|(?:товар|крем|сыворотк|тоник|пудр)\w*[^.!?]{0,15}\bдорог|\bдорог\w*\s+(?:товар|крем|сыворотк|тоник|пудр)", "Понимаем Ваше замечание о цене.", (r"цен",)),
]


def aspects_of(msg):
    t = normalize(_raw(msg))
    out = []
    for key, pattern, response, coverage in _ASPECTS:
        if not re.search(pattern, t):
            continue
        if key == "product_liked" and not any(re.search(pattern,c) and not re.search(r"\bне\s+(?:понрав|нрав|довольн|хорош)",c)
                                                   for c in re.split(r'[,;.!?]|\bно\b',t)):
            continue
        if key == 'fragrance_liked' and re.search(r'(?:аромат|запах)[^.!?]{0,20}\bне\s+(?:понрав|нрав)',t):
            continue
        if key == "softness" and re.search(r"не\s+мягк", t):
            continue
        if key == "recommendation" and re.search(r"не\s+рекомендую|не\s+советую", t):
            continue
        out.append(Aspect(key, "MUST_ADDRESS", response, coverage))
    extra = [
        ('sticky', r'(?<!не )липк', 'Жаль, что после нанесения Вы ощущаете липкость.', (r'липк',)),
        ('drying', r'сушит|сухость|стягива', 'Жаль, что после применения Вы ощущаете сухость.', (r'сух|стяг',)),
        ('fragrance_disliked', r'\bне\s+(?:понрав|нрав)[^.!?]{0,35}(аромат|запах)|(?:аромат|запах)[^.!?]{0,35}\bне\s+(?:понрав|нрав)', 'Жаль, что аромат Вам не понравился.', (r'аромат|запах',r'жаль|не\s+понрав')),
        ('packaging_inconvenient', r'неудобн[^.!?]{0,20}упаков|упаков[^.!?]{0,20}неудоб', 'Жаль, что упаковка оказалась для Вас неудобной.', (r'упаков',r'неудоб')),
        ('dispenser_inconvenient', r'неудобн[^.!?]{0,20}дозатор|дозатор[^.!?]{0,20}неудоб', 'Жаль, что дозатор оказался неудобным в использовании.', (r'дозатор',r'неудоб')),
        ('result_liked', r'эффект[^.!?]{0,15}(есть|хорош)|хорош[^.!?]{0,15}(эффект|результат)|(?:кож|рук)\w*[^.!?]{0,35}(?:увлажнен|ухожен|нежн)', 'Рады, что Вы довольны результатом применения.', (r'результат|эффект|(?:кож|рук)[^.!?]{0,30}(?:нежн|ухожен|увлажнен)',)),
        ('future_purchase',r'буду\s+(брать|покупать|заказыв)|куплю\s+ещ[её]', 'Будем рады видеть Вас снова.', (r'снова|ещ[её]',)),
    ]
    for key,rx,response,coverage in extra:
        if re.search(rx,t):
            if key=='result_liked' and re.search(r'\bне\s+(?:увлажнен|ухожен|нежн)',t):continue
            out.append(Aspect(key,'MUST_ADDRESS',response,coverage))
    if re.search(r'эффекта?\s+(никакого\s+)?нет|ни\s+эффект',t) and not any(a.key=='no_effect' for a in out):
        out.append(Aspect('no_effect','MUST_ADDRESS','Жаль, что Вы не увидели ожидаемого результата.',(r'результат|ожидан',)))
    # A positive mention embedded in negation is never praise.
    if re.search(r'не\s+оставля[^.!?]{0,30}липк|без\s+липк|не\s+лип',t):out=[a for a in out if a.key!='sticky']
    return out


def make_plan(msg, snap, *, hard_plan=None):
    res = resolver.resolve(snap, nm_id=msg.get("nm_id"), supplier_article=msg.get("supplier_article"),
                           barcode=msg.get("barcode"), marketplace=msg.get("channel") or msg.get("marketplace") or "WB")
    cls = classifier.classify_rules(msg, snap)
    raw = normalize(_raw(msg))
    # Local voice-layer coverage supplements; never override safety/legal/identity gates.
    supplemented = False
    if re.search(r"крышк\w*[^.!?]{0,30}(лоп|трес|слома)", raw) and not re.search(r"не\s+(лоп|трес|слома)", raw):
        cls.situations.append({"code": "PACKAGING.broken_package", "source": "voice_rules"})
        supplemented = True
    age_question = cls.is_question and bool(re.search(r"со\s+скольк|с\s+какого\s+возраст|скольк\w*\s+лет", raw))
    if age_question:
        cls.situations.append({"code": "SUITABILITY.children", "source": "voice_rules"})
        supplemented = True
    local_hard = planner.plan(snap, res, cls, msg)
    hard = hard_plan or local_hard
    if supplemented and (not hard_plan or hard_plan.strategy not in {"HUMAN_REVIEW", "SAFETY_TEMPLATE"}
                         or hard_plan.failure_code == "UNCLASSIFIED_QUESTION"):
        hard = local_hard
    p = VoicePlan(hard.strategy, "P1", 0, aspects_of(msg))
    # Hard routes dominate the voice layer, regardless of stars or friendliness.
    if hard.strategy == "HUMAN_REVIEW":
        p.human_reason = hard.failure_code or "HUMAN_REVIEW"
        return p
    if hard.strategy == "SAFETY_TEMPLATE":
        p.direct_answer = hard.deterministic_text  # no mild-discomfort recalibration
        return p
    if hard.strategy == "SERVICE":
        p.service_premises=hard.service_premises
        positive=[a for a in p.aspects if a.key in {'fragrance_liked','texture','softness','product_liked'}]
        p.aspects = positive+[Aspect("service_damage", "MUST_ADDRESS", "", (r"жаль", r"обращени", r"wildberries", r"фотограф"))]
        events = {s['code'] for s in hard.service_premises}
        if "T-SVC-WB-ORDER" in hard.template_ids:
            premise = ("Жаль, что в заказе не оказалось одной из позиций набора. "
                       if 'ORDER.incomplete_bundle' in events else
                       "Жаль, что Вы получили не тот заказ, на который рассчитывали. ")
            p.direct_answer = _with_name(msg, premise +
                "Оформите обращение по заказу в личном кабинете Wildberries: укажите, что именно пришло, "
                "и по возможности приложите фотографии. Решение по обращению принимает площадка.")
        else:
            # Preserve the event family rather than substituting general damage.
            acknowledgments = {
                'PACKAGING.empty_package': 'Жаль, что флакон оказался пустым. ',
                'PACKAGING.leakage': 'Жаль, что средство протекло. ',
                'PACKAGING.dispenser_failure': 'Жаль, что дозатор не работает. ',
                'PACKAGING.broken_package': 'Жаль, что упаковка повреждена. ',
            }
            premise = ''.join(acknowledgments[e] for e in acknowledgments if e in events)
            p.direct_answer = _with_name(msg, premise +
                "Оформите обращение по товару в личном кабинете Wildberries и приложите фотографии "
                "товара и упаковки. Решение по обращению принимает площадка.")
        if positive:
            preferred=next((a for a in positive if a.key=='fragrance_liked'),positive[0])
            p.direct_answer=preferred.response+' '+p.direct_answer
        return p
    if cls.is_question:
        p.aspects = [Aspect("direct_answer", "MUST_ADDRESS", "", ())]
        p.level = "DIRECT"
        from app.v3.direct_questions import UNKNOWN_WORDING, RESTRICTED_BOUNDARY, unknown_wording
        p.question_targets=hard.question_intent.fact_types
        exact_unknown=[r for r in hard.resolved if r.state=='UNKNOWN' and r.fact_type in UNKNOWN_WORDING]
        restricted=[r for r in hard.resolved if r.state=='KNOWN_RESTRICTED' and r.fact_type=='ingredient_concentration']
        if exact_unknown or restricted:
            boundary=[unknown_wording(r.fact_type, hard.question_intent) for r in exact_unknown]
            if restricted:boundary.insert(0,RESTRICTED_BOUNDARY)
            adjacent=[r.customer_value_ru for r in hard.allowed if r.customer_value_ru and r not in exact_unknown and r not in restricted]
            if restricted:adjacent.extend(r.customer_value_ru for r in restricted if r.customer_value_ru)
            p.direct_answer=' '.join(dict.fromkeys(boundary+adjacent[:1]))
            return p
        # Owner-approved Phase 3.1 age-information wording. This asserts UNKNOWN, not suitability.
        if re.search(r"со\s+скольк|с\s+какого\s+возраст|возраст|скольк\w*\s+лет", normalize(_raw(msg))) \
                and "SUITABILITY.children" in cls.codes:
            p.route = "UNKNOWN_FACT"
            p.direct_answer = ("Подтверждённый возраст начала применения этого средства в документации "
                               "производителя не указан. Уточните, пожалуйста, для какого возраста Вы выбираете средство.")
            return p
        if hard.strategy == "UNKNOWN_FACT" and hard.resolved and all(
                r.state == "UNKNOWN" and r.fact_type in {"frequency", "amount"} for r in hard.resolved):
            missing = ["частота применения" if r.fact_type == "frequency" else "количество средства для одного применения"
                       for r in hard.resolved]
            p.direct_answer = "В документации производителя не подтверждены: " + "; ".join(dict.fromkeys(missing)) + "."
        elif hard.deterministic_text:
            p.direct_answer = hard.deterministic_text
        else:
            allowed = [r for r in hard.allowed if r.customer_value_ru]
            p.information_budget = min(2, len(allowed))
            p.facts = [{"fact_id": ",".join(r.fact_ids), "text": r.customer_value_ru,
                        "source_ids": r.source_ids} for r in allowed[:2]]
            p.direct_answer = " ".join(f["text"].rstrip(".") + "." for f in p.facts)
            # Never silently drop an unanswered part of a multi-part question.
            if len(allowed) > 2:
                p.human_reason = "QUESTION_EXCEEDS_INFORMATION_BUDGET"
            elif not p.direct_answer:
                p.human_reason = "REQUIRED_FACT_UNKNOWN"
        return p
    if not _raw(msg) and (res.status != "VERIFIED" or res.restricted_components):
        p.block_reason = "PRODUCT_NOT_VERIFIED"
        return p
    p.information_budget = 1 if _raw(msg) else 0
    p.level = "P1" if p.aspects else "P0"
    keys = {a.key for a in p.aspects}
    if keys & {"softness", "texture"} and not keys & {"product_disliked", "no_effect", "fragrance_harsh"}:
        ctx = verifier.context_for_free_text(snap, [res.product_id] if res.product_id else [])
        # One relevant presence fact; no concentration or causal claim. Never use restricted scope.
        if res.status == "VERIFIED" and not res.restricted_components and "hyaluronic_acid" in ctx.allowed_ingredient_ids:
            for row in snap.ingredient_rows(res.product_id):
                if row["ingredient_id"] == "hyaluronic_acid":
                    p.facts = [{"fact_id": f"{res.product_id}.ingredient.recipe.hyaluronic_acid",
                                "text": "В формуле есть гиалуроновая кислота.",
                                "source_ids": [row["source_id"]]}]
                    p.level = "P2"
                    break
    from app.response_quality.expertise import approved, in_context
    if res.product_id and res.status=='VERIFIED' and not res.restricted_components:
        # Approval alone does not make a statement helpful for this complaint.
        # Never counter no-effect / tackiness / dryness with product advertising.
        candidates = [r for r in approved(snap,res.product_id) if keys & set(r['signals']) and in_context(r,msg,snap)]
        if keys & {'no_effect', 'sticky', 'drying', 'price'}:
            candidates = [r for r in candidates if r['kind'] != 'ingredient_benefit']
        # Guidance about perception is more relevant to a scent complaint than
        # repeating its name; texture context outranks an incidental fragrance fact.
        def relevance(row):
            matched = keys & set(row['signals'])
            return (row['kind']=='guidance' and bool(keys & {'fragrance_harsh','fragrance_disliked'}),
                    row['kind']=='ingredient_benefit' and bool(matched & {'softness','texture','non_sticky'}))
        # Bounded context: relevance is a planner decision, background is optional.
        p.explanations = [dict(r,execution='USEFUL_AND_RELEVANT' if r['kind']!='fact' else 'OPTIONAL_BACKGROUND')
                          for r in sorted(candidates, key=relevance, reverse=True)[:2]]
    p.product_types = sorted(verifier.context_for_free_text(snap,[res.product_id]).product_types) if res.product_id else []
    from app.v3.grounding import capabilities
    p.product_capabilities=capabilities(snap,[res.product_id] if res.product_id else [])
    specific=keys & {'sticky','drying','fragrance_harsh','fragrance_disliked','price','no_effect','packaging_inconvenient','dispenser_inconvenient'}
    if re.search(r'дозатор|упаковк',raw) and re.search(r'слишком|туго|не (?:открыва|закрыва|выда|нажима)|застрева|протека',raw):
        specific.add('reported_component_problem')
    if 'no_effect' in keys and not re.search(r'(?:ожидал|ждал|хотел)\w*[^.!?]{0,45}(?:увлажнен|мягк|исчез|уменьш|очищен)',raw):
        p.clarifications.append({'unknown':'expected_result','changes':'identify the unmet expectation; no efficacy advice'})
    elif keys & {'packaging_inconvenient','dispenser_inconvenient'}:
        # Generic inconvenience names a component, but not what actually fails.
        if not re.search(r'не (?:открыва|закрыва|выда|нажима)|застрева|слишком|туго|протека',raw):
            p.clarifications.append({'unknown':'specific_difficulty','changes':'identify the unreported component difficulty for operator/service routing'})
    elif 'product_disliked' in keys and not specific:
        p.clarifications.append({'unknown':'specific_difficulty','changes':'identify the actual issue absent from customer report'})
    elif keys & {'sticky','drying'} and not specific-{'sticky','drying'} and not re.search(r'вначале|сначала|сразу|поначалу|в\s+течение|долго|после\s+нанесения',raw):
        p.clarifications.append({'unknown':'sensation_timing','changes':'establish whether the sensation is initial or persistent; no regime advice'})
    return p


def human_voice(msg, p, *, safe_v3_draft=None):
    # A v3 draft is repair input only. Hard templates are kept; experience responses are replanned.
    if p.human_reason:
        return None
    if p.direct_answer:
        return p.direct_answer
    aspects = list(p.aspects)
    if any(a.key in {"softness", "texture", "non_sticky"} for a in aspects):
        aspects = [a for a in aspects if a.key != "product_liked"]  # detail already acknowledges the praise
    text = " ".join(a.response for a in aspects)
    if not text:
        text = ("Спасибо за высокую оценку. Рады Вашему выбору EVETIS." if (msg.get("rating") or 0) >= 4 else
                "Жаль, что покупка не оправдала ожиданий. Уточните, пожалуйста, что именно Вас разочаровало.")
    text = _with_name(msg, text)
    if p.facts:
        text += " " + " ".join(f["text"] for f in p.facts)
    return text


def evaluate(text, msg, p, hard_policy, *, previous_answers=()):
    from app.response_quality.brand_voice import assess
    return assess(text,msg,p,hard_policy,previous_answers=previous_answers)


def prepare(msg, original, snap, *, safe_v3_draft=None, hard_plan=None, render=None, force_generation=False):
    """At most one corrected candidate from the injected language pass (or offline renderer).

    Final policy is checked independently again. Corpus force_generation evaluates
    the new renderer even when a saved original passed; it does not publish anything.

    A serious-safety/legal/identity hard route can never be repaired into an ordinary answer.
    Quality failure remains NEEDS_IMPROVEMENT; it never becomes a hard BLOCK.
    """
    from app.response_quality.brand_voice import render as default_render
    render = render or default_render
    p = make_plan(msg, snap, hard_plan=hard_plan)
    st = SimpleNamespace(v3_knowledge_snapshot_id=snap.snapshot_id)
    first = validate_for_publication(original or "", msg, st, include_spans=True)
    q = evaluate(original, msg, p, first)
    reasons = sorted({v["rule_id"] for v in first.get("violations", [])} | set(q.reasons))
    if p.human_reason:
        return PreparedResponse("HUMAN_REVIEW", None, False, first, {"verdict": "BLOCK"}, q, p,
                                [p.human_reason])
    if p.block_reason:
        return PreparedResponse("HUMAN_REVIEW", None, False, first, {"verdict":"BLOCK", "reason":p.block_reason}, q, p, [p.block_reason])
    if first["verdict"] != "BLOCK" and q.verdict == "GOOD" and not force_generation:
        return PreparedResponse("READY", original, False, first, first, q, p, [])
    from app.response_quality.generation_contract import GenerationContractError
    contract_violations=[]
    try:
        candidate = render(msg, p, safe_v3_draft=safe_v3_draft)
    except GenerationContractError as exc:
        candidate=None
        contract_violations=exc.violations
    final = (dict(verdict='BLOCK',violations=contract_violations) if contract_violations else
             validate_for_publication(candidate or "", msg, st))
    fq = evaluate(candidate, msg, p, final)
    ready = bool(candidate) and final["verdict"] != "BLOCK"
    if not ready and first["verdict"] != "BLOCK" and not p.direct_answer:
        # Use only the existing customer-acknowledgement plan. No new expertise,
        # claims or invented clarification. Independently gate this fallback too.
        fallback = human_voice(msg, replace(p, facts=[], explanations=[]))
        fallback_policy = validate_for_publication(fallback or "", msg, st)
        fallback_quality = evaluate(fallback,msg,p,fallback_policy)
        old_missing = sum(x.startswith('missing_aspect:') for x in q.reasons)
        fallback_missing = sum(x.startswith('missing_aspect:') for x in fallback_quality.reasons)
        if fallback_policy['verdict'] != 'BLOCK' and fallback_missing < old_missing:
            return PreparedResponse('READY', fallback, fallback != original, first, fallback_policy,
                                    fallback_quality,p,reasons+['CUSTOMER_SIGNALS_SAFE_FALLBACK'])
    if not ready and first["verdict"] != "BLOCK":
        # A failed STYLE improvement may never block the already safe operator candidate.
        return PreparedResponse("READY", original, False, first, first, q, p, reasons)
    return PreparedResponse("READY" if ready else "HUMAN_REVIEW", candidate if ready else None,
                            ready and candidate != original, first, final, fq, p, reasons)
