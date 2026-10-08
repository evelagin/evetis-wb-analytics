"""R2.2 SAFE_INFORMATION_GAP: an honest deterministic answer where the exact fact is
unavailable and nothing beyond «we cannot confirm it» is needed.

Today: a buyer QUESTION about the next restock that the hard planner sends to a human only
because no service instruction is approved (or because the snapshot lexicon does not
recognise the wording). The answer promises no date and no inventory; it names only the
verified product kind. A message with any other ask, returns/refunds, reviews, safety,
legal and identity routes are untouched. Amount/frequency/longevity unknowns are already
answered by the existing UNKNOWN_FACT wording.
"""
from __future__ import annotations

import re

SAFE_INFORMATION_GAP = "SAFE_INFORMATION_GAP"
_GENITIVE = {"hand_cream": "крема", "body_cream": "крема", "cream": "крема", "serum": "сыворотки",
             "tonic": "тоника", "powder": "пудры", "bundle": "набора"}
# A restock question the snapshot lexicon does not recognise («когда снова будет в продаже?»).
_RESTOCK = (r"(?:когда|будет\s+ли|ожида\w*|планиру\w*)[^.?!]{0,40}"
            r"(?:в\s+наличи|в\s+продаж|поступ|появ|завез|привез|поставк)")
# Any other ask in the same message keeps the human route: never answer half a question.
_OTHER_ASK = (r"возвр|\bверн\w*|обмен|деньг|компенс|достав|\bцен[аыуе]?\b|скидк|брак|подделк|оригинал"
              r"|состав|подойд|подходит|можно\s+ли|как\s+(?:использ|примен|нанос)|срок\w*\s+годн")


def restock_answer(noun: str) -> str:
    return (f"Здравствуйте! Точную дату следующего поступления {noun} пока подтвердить не можем. "
            "Рекомендуем следить за наличием в карточке товара на Wildberries. Спасибо за интерес к EVETIS!")


def apply(msg, cls, hard, res, snap, p):
    """The safe-gap plan, or None when this case is not a bounded information gap."""
    from app.response_quality.core import Aspect, _raw
    from app.v3.text import normalize
    codes = set(cls.codes)
    raw = normalize(_raw(msg))
    intent = getattr(hard, "question_intent", None)
    # Only a WB buyer QUESTION: a review that also asks something keeps its own route.
    if (msg.get("entity_type") != "question" or not cls.is_question or hard.strategy != "HUMAN_REVIEW"
            or res.status != "VERIFIED"):
        return None
    if (intent and intent.fact_types) or re.search(_OTHER_ASK, raw) or "ORDER.return_refund" in codes:
        return None
    if cls.safety and cls.safety.has_safety:
        return None
    restock = (hard.failure_code == "SERVICE_INSTRUCTION_NOT_VERIFIED" and "ORDER.availability" in codes) or \
        (hard.failure_code == "UNCLASSIFIED_QUESTION" and bool(re.search(_RESTOCK, raw)))
    if not restock:
        return None
    product = (snap.product(res.product_id) or {}) if res.product_id else {}
    noun = _GENITIVE.get(product.get("product_type") or product.get("kind"), "товара")
    p.route, p.level, p.information_budget = SAFE_INFORMATION_GAP, "DIRECT", 0
    p.facts, p.explanations, p.clarifications = [], [], []
    p.aspects = [Aspect("direct_answer", "MUST_ADDRESS", "", ())]
    p.direct_answer = restock_answer(noun)
    return p
