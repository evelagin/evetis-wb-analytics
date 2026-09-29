"""ODR-07 hotfix (29.09.2026, production case 879824da): production v2 must never be GIVEN the
salicylic-acid concentration of the anti-acne cream, so it cannot state it or compare it.
Allowed level: presence only («в составе есть салициловая кислота»).

Checked on the prompt the model actually receives (system + user, built from the production
knowledge base) for the cream and every set that contains it, for reviews and questions, plus the
reviews_v1 rollback prompt. The v3 registry keeps the verified structured fact — not tested here."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.communication_engine.constants import CommunicationType
from app.communication_engine.models.classification import ReviewInput

# acne_cream (438775617) and every bundle with acne_cream among bundle_components.
CREAM_NM_IDS = ["438775617", "567668635", "952068582", "868597351", "910584041"]
LITERAL = re.compile(r"2\s*[,.]\s*25")
# comparative / indirect level of the salicylic concentration in grounding text
COMPARATIVE = re.compile(
    r"(высок|максимальн|предельн|повышенн|усиленн|двойн|сильн)\w*\s+(концентрац|процент|дозировк|содержани)"
    r"|салицилов\w*\s+(кислот\w*\s+)?(\(bha\)\s*)?(более|свыше|около|почти|до)?\s*\d")
ROOT = Path(__file__).resolve().parents[2]


def _grounding(ctx, doc_id: str) -> str:
    """Grounding body of one knowledge document (constraints are NOT grounding)."""
    blocks = [b for b in ctx.blocks if b.ref.doc_id == doc_id]
    assert blocks, f"{doc_id} not in context"
    return " ".join(b.body for b in blocks).lower().replace("ё", "е")


@pytest.mark.parametrize("nm_id", CREAM_NM_IDS)
@pytest.mark.parametrize("ctype", [CommunicationType.REVIEW, CommunicationType.QUESTION])
@pytest.mark.parametrize("text", ["Сколько процентов салициловой кислоты в креме?",
                                  "Какая концентрация кислоты? Сильная?", ""])
def test_v2_prompt_never_carries_salicylic_concentration(engine, nm_id, ctype, text):
    review = ReviewInput(communication_type=ctype, nm_id=nm_id, supplier_article=nm_id,
                         rating=None if ctype == CommunicationType.QUESTION else 5, text=text,
                         user_name="Юлия")
    bundle = engine.build_prompt(review)
    assert bundle.classification.product_id, "must resolve to the cream or its set"
    prompt = bundle.system + "\n" + bundle.user
    assert not LITERAL.search(prompt.replace(text, "")), "2,25 reached the v2 prompt"
    ground = _grounding(engine.build_context(review), "acne_cream")
    assert "салицилов" in ground                       # presence stays allowed
    assert not COMPARATIVE.search(ground), "indirect/comparative salicylic level in grounding"


def test_v2_validators_flag_a_draft_that_states_the_value(engine):
    """Defence in depth: with the value out of grounding, a draft that still states it is
    ungrounded for the (advisory) numeric validator."""
    ctx = engine.build_context(ReviewInput(nm_id="567668635", supplier_article="567668635", rating=5))
    result = engine.validate("Юлия, спасибо! В креме 2,25% салициловой кислоты.", ctx)
    assert not result.ok


def test_reviews_v1_rollback_prompt_has_no_value():
    system = (ROOT / "prompts" / "reviews" / "system_v1.txt").read_text(encoding="utf-8")
    assert not LITERAL.search(system)
