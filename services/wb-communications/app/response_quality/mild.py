"""3.1F: a customer-reported skin reaction, answered without medical escalation.

Owner decision 11.10.2026: machine-generated WB replies never tell the buyer to see a doctor,
dermatologist or allergist. A MILD active reaction (planner route S1) gets a short contextual
operator draft — the customer's own praise if any, regret for the discomfort, one minimal
non-medical line about continuing, thanks; no diagnosis, no cause, no «adaptation». A
persistent / worsening / severe reaction (S2) goes to a human: no machine draft at all. A
reaction that has already passed (S1R) keeps its approved template, which has no medical
escalation. Publication remains a manual operator act (AUTO-PUBLISH stays off).
"""
from __future__ import annotations

MILD_REACTION = "MILD_REACTION"
_SPECIFIC = ("texture", "softness", "fragrance_liked", "non_sticky", "result_liked")
_POSITIVE = {
    "texture": "Рады, что текстура Вам понравилась.",
    "softness": "Очень приятно, что Вы отметили мягкость средства.",
    "fragrance_liked": "Рады, что аромат Вам понравился.",
    "non_sticky": "Рады, что средство не показалось Вам липким.",
    "result_liked": "Рады, что Вы отметили и хорошие стороны средства.",
    "product_liked": "Спасибо, что отметили и то, что Вам понравилось.",
}
# a non-safety complaint the customer also made (mixed review: both sides are answered)
_OTHER_NEGATIVE = ("dispenser_inconvenient", "packaging_inconvenient", "powder_dusting", "sticky", "drying")
DISCOMFORT = "Жаль, что средство вызвало у Вас дискомфорт."
PAUSE = "Если ощущения повторятся, лучше отложить это средство."
THANKS = "Спасибо, что рассказали об этом опыте."
DOCTOR = r"врач|дерматолог|аллерголог|медицинск\w*\s+помощ|к\s+специалист"


def apply(msg, p, hard):
    """Rewrite the hard SAFETY_TEMPLATE plan for the operator path. Returns the plan."""
    from app.response_quality.core import Aspect, _with_name
    template = (hard.template_ids or [""])[0]
    if template == "T-S2":
        p.human_reason = "SAFETY_ESCALATION"     # persistent / worsening / severe → a human decides
        p.route = "HUMAN_REVIEW"
        return p
    if template != "T-S1":
        p.direct_answer = hard.deterministic_text  # T-S1R: approved, no medical escalation
        p.attention = "SAFETY_TEMPLATE"
        return p
    keys = [a.key for a in p.aspects]
    praise = [k for k in _SPECIFIC if k in keys][:1] or (["product_liked"] if "product_liked" in keys else [])
    other = [a for a in p.aspects if a.key in _OTHER_NEGATIVE][:1]
    p.route, p.attention, p.level, p.information_budget = MILD_REACTION, MILD_REACTION, "P1", 0
    p.facts, p.explanations, p.clarifications = [], [], []
    p.aspects = [a for a in p.aspects if a.key in praise] + other + [
        Aspect("mild_reaction", "MUST_ADDRESS", DISCOMFORT, (r"жаль|сожале", r"дискомфорт|ощущени"))]
    p.direct_answer = _with_name(msg, " ".join(
        [_POSITIVE[k] for k in praise] + [a.response for a in other] + [DISCOMFORT, PAUSE, THANKS]))
    return p
