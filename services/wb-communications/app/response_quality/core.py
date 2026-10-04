"""Bounded, deterministic human voice; the immutable snapshot remains the truth source.

No clients, credentials, marketplace writes or ledger writes are reachable here. Quality
is a diagnostic checklist, not a permission to publish and not a hard-policy verdict.
"""
from __future__ import annotations
import hashlib
import re
from dataclasses import asdict, dataclass, field
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
    ("product_liked", r"отлич|понрав|хорош|супер|довольн", "Рады, что покупка Вам понравилась.", (r"рад|приятн", r"понрав|впечатлен|оценк|покупк|мягк|текстур|липк")),
    ("softness", r"мягк", "Очень приятно, что Вы отметили мягкость средства.", (r"мягк",)),
    ("non_sticky", r"не\s+лип|нелипк", "Рады, что средство не показалось Вам липким.", (r"липк|липким",)),
    ("texture", r"(?:приятн|нежн|легк|лёгк)\w*\s+текстур|текстур\w*[^.!?]{0,20}(?:приятн|нежн|легк|лёгк)", "Рады, что текстура Вам понравилась.", (r"текстур",)),
    ("fragrance_harsh", r"резк\w*[^.!?]{0,20}(запах|аромат)|(запах|аромат)[^.!?]{0,20}резк", "Понимаем, что аромат показался Вам резким.", (r"аромат|запах", r"резк")),
    ("fragrance_liked", r"(?:приятн|хорош)\w*\s+(запах|аромат)|(запах|аромат)[^.!?]{0,20}понрав|приятно\s+пах", "Рады, что аромат Вам понравился.", (r"аромат|запах", r"понрав|приятн")),
    ("recommendation", r"рекомендую|советую", "Спасибо за рекомендацию.", (r"рекомендац",)),
    ("delivery", r"быстр\w*[^.!?]{0,20}достав|достав\w*[^.!?]{0,20}быстр", "Спасибо, что отметили быструю доставку.", (r"достав", r"быстр|оператив")),
    ("repeat_purchase", r"(беру|покупаю|заказываю)[^.!?]{0,30}(снова|повтор|не\s+перв|втор|трет)|не\s+перв\w*\s+раз|постоянно\s+(беру|покупаю|заказываю)", "Спасибо, что снова выбрали EVETIS.", (r"снова|повтор|не\s+перв",)),
    ("no_effect", r"не\s+увидел\w*[^.!?]{0,20}(эффект|результат)|нет\s+(эффект|результат)|без\s+результат|не\s+работает", "Жаль, что результат не совпал с Вашими ожиданиями.", (r"результат|ожидан",)),
    ("product_disliked", r"не\s+понрав|разочар|ужас|плох", "Жаль, что покупка Вас разочаровала.", (r"жаль|разочар|ожидан",)),
    ("price", r"дорог|цен[ауы]\s+(высок|завыш)|дороговат|подешевел", "Понимаем Ваше замечание о цене.", (r"цен",)),
]


def aspects_of(msg):
    t = normalize(_raw(msg))
    out = []
    for key, pattern, response, coverage in _ASPECTS:
        if not re.search(pattern, t):
            continue
        if key == "product_liked" and not any(re.search(pattern,c) and not re.search(r"не\s+понрав|не\s+довольн|не\s+хорош",c)
                                                   for c in re.split(r'[,;.!?]|\bно\b',t)):
            continue
        if key == 'fragrance_liked' and re.search(r'(?:аромат|запах)[^.!?]{0,20}не\s+понрав',t):
            continue
        if key == "softness" and re.search(r"не\s+мягк", t):
            continue
        if key == "recommendation" and re.search(r"не\s+рекомендую|не\s+советую", t):
            continue
        out.append(Aspect(key, "MUST_ADDRESS", response, coverage))
    extra = [
        ('sticky', r'(?<!не )липк', 'Жаль, что после нанесения Вы ощущаете липкость.', (r'липк',)),
        ('drying', r'сушит|сухость|стягива', 'Жаль, что после применения Вы ощущаете сухость.', (r'сух|стяг',)),
        ('fragrance_disliked', r'не\s+понрав[^.!?]{0,35}(аромат|запах)|(?:аромат|запах)[^.!?]{0,35}не\s+понрав', 'Жаль, что аромат Вам не понравился.', (r'аромат|запах',r'жаль|не\s+понрав')),
        ('packaging_inconvenient', r'неудобн[^.!?]{0,20}упаков|упаков[^.!?]{0,20}неудоб', 'Жаль, что упаковка оказалась для Вас неудобной.', (r'упаков',r'неудоб')),
        ('dispenser_inconvenient', r'неудобн[^.!?]{0,20}дозатор|дозатор[^.!?]{0,20}неудоб', 'Жаль, что дозатор оказался неудобным в использовании.', (r'дозатор',r'неудоб')),
        ('result_liked', r'эффект[^.!?]{0,15}(есть|хорош)|хорош[^.!?]{0,15}(эффект|результат)', 'Рады, что Вы довольны результатом применения.', (r'результат|эффект',)),
        ('future_purchase',r'буду\s+(брать|покупать|заказыв)|куплю\s+ещ[её]', 'Будем рады видеть Вас снова.', (r'снова|ещ[её]',)),
    ]
    for key,rx,response,coverage in extra:
        if re.search(rx,t):out.append(Aspect(key,'MUST_ADDRESS',response,coverage))
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
        p.aspects = [Aspect("service_damage", "MUST_ADDRESS", "", (r"жаль", r"обращени", r"wildberries", r"фотограф"))]
        if "T-SVC-WB-ORDER" in hard.template_ids:
            p.direct_answer = _with_name(msg, "Жаль, что Вы получили не тот заказ, на который рассчитывали. "
                "Оформите обращение по заказу в личном кабинете Wildberries: укажите, что именно пришло, "
                "и по возможности приложите фотографии. Решение по обращению принимает площадка.")
        else:
            damage = "повреждениями"
            raw = normalize(_raw(msg))
            if "крышк" in raw and re.search(r"лоп|трес|слома", raw):
                damage = "лопнувшей крышкой" + (" и повреждённой упаковкой" if "упаков" in raw else "")
            p.direct_answer = _with_name(msg, f"Жаль, что покупка пришла с {damage}. "
                "Оформите обращение по товару в личном кабинете Wildberries и приложите фотографии "
                "товара и упаковки. Решение по обращению принимает площадка.")
        return p
    if cls.is_question:
        p.aspects = [Aspect("direct_answer", "MUST_ADDRESS", "", ())]
        p.level = "DIRECT"
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
    from app.response_quality.expertise import approved
    if res.product_id and res.status=='VERIFIED' and not res.restricted_components:
        p.explanations = [r for r in approved(snap,res.product_id) if keys & set(r['signals'])][:1]
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


def prepare(msg, original, snap, *, safe_v3_draft=None, hard_plan=None, render=None):
    """At most one corrected candidate. Final policy is checked independently again.

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
    if first["verdict"] != "BLOCK" and q.verdict == "GOOD":
        return PreparedResponse("READY", original, False, first, first, q, p, [])
    candidate = render(msg, p, safe_v3_draft=safe_v3_draft)
    final = validate_for_publication(candidate or "", msg, st)
    fq = evaluate(candidate, msg, p, final)
    ready = bool(candidate) and final["verdict"] != "BLOCK"
    if not ready and first["verdict"] != "BLOCK":
        # A failed STYLE improvement may never block the already safe operator candidate.
        return PreparedResponse("READY", original, False, first, first, q, p, reasons)
    return PreparedResponse("READY" if ready else "HUMAN_REVIEW", candidate if ready else None,
                            ready and candidate != original, first, final, fq, p, reasons)
