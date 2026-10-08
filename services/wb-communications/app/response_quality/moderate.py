"""R2.1 moderate discomfort: a cautious, deterministic operator draft.

Used only when the hard planner would send a fragrance-attributed sensation (nausea,
dizziness, headache, no serious marker) to a human, or would answer it as ordinary
feedback. No product claim, no medical escalation, no blanket stop: acknowledge the
customer's own positive words, acknowledge the discomfort without disputing it, suggest
setting aside that scent if it repeats, thank. Publication remains a manual operator act.
"""
from __future__ import annotations

import re

from app.v3.safety import MODERATE_DISCOMFORT, moderate_discomfort

# The customer's own praise, specific first; a generic "liked it" only when nothing specific.
_SPECIFIC = ("softness", "texture", "non_sticky", "result_liked")
_POSITIVE = {
    "softness": "Очень приятно, что Вы отметили мягкость средства.",
    "texture": "Рады, что текстура Вам понравилась.",
    "non_sticky": "Рады, что средство не показалось Вам липким.",
    "result_liked": "Рады, что Вы довольны результатом применения.",
    "product_liked": "Спасибо, что отдельно отметили само средство — нам очень приятно, что оно Вам понравилось.",
}
DISCOMFORT = "Жаль, что аромат оказался для Вас слишком насыщенным и вызвал неприятные ощущения."
PAUSE_SET = "Если такое повторяется именно с одним из ароматов набора, лучше отложить этот вариант."
PAUSE_ONE = "Если такое повторится, лучше отложить это средство."
CLOSE = "Спасибо за честный отзыв — Ваше впечатление для нас важно."
_HUMAN_DOMAINS = {"LEGAL_THREAT", "COUNTERFEIT_ACCUSATION", "AUTHENTICITY_QUESTION", "REGULATORY"}


def signals(msg, cls, hard, raw) -> list[str] | None:
    """Moderate sensations when this plan may become a moderate draft, else None.

    Only a pure safety escalation or an ordinary experience route converts; identity,
    legal, service and question routes keep their own gate."""
    if cls.is_question or not cls.safety:
        return None
    if set(cls.escalation_domains) & _HUMAN_DOMAINS or "TRUST.review_removal" in cls.codes:
        return None
    if hard.strategy == "HUMAN_REVIEW":
        if hard.failure_code != "SAFETY_ESCALATION":
            return None
    elif hard.strategy in {"SAFETY_TEMPLATE", "SERVICE"}:
        return None
    return moderate_discomfort(raw, cls.safety)


def _is_set(res, snap) -> bool:
    product = (snap.product(res.product_id) or {}) if res.product_id else {}
    return (product.get("product_type") or product.get("kind")) == "bundle" and len(product.get("components") or []) > 1


def apply(msg, res, snap, p, found):
    from app.response_quality.core import Aspect, _with_name
    keys = [a.key for a in p.aspects]
    specific = [k for k in _SPECIFIC if k in keys]
    praise = specific[:1] or (["product_liked"] if "product_liked" in keys else [])
    p.route, p.attention, p.level, p.information_budget = MODERATE_DISCOMFORT, MODERATE_DISCOMFORT, "P1", 0
    p.facts, p.explanations, p.clarifications = [], [], []
    p.aspects = [a for a in p.aspects if a.key in praise] + [
        Aspect("fragrance_discomfort", "MUST_ADDRESS", DISCOMFORT, (r"жаль|сожале", r"аромат|запах"))]
    p.moderate_signals = list(found)
    p.direct_answer = _with_name(msg, " ".join([_POSITIVE[k] for k in praise] + [
        DISCOMFORT, PAUSE_SET if _is_set(res, snap) else PAUSE_ONE, CLOSE]))
    return p


def diagnostics(text) -> list[str]:
    """Style checks for a moderate answer (diagnostic, never a hard block)."""
    from app.v3.text import normalize
    n = normalize(text or "")
    out = []
    if re.search(r"врач|дерматолог|аллерголог|медицин", n):
        out.append("MODERATE_SAFETY_OVER_ESCALATION")
    if re.search(r"(?:прекрат|перестан|откаж)\w*[^.!?]{0,40}\b(?:всех|всей|любых)\b|\bвсех\s+(?:кремов|средств)|\bвсей\s+продукции", n):
        out.append("MODERATE_SAFETY_BLANKET_STOP")
    return out
