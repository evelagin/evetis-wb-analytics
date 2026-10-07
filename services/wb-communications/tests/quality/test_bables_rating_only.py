"""WB review tags (`bables`) reach both engines and the card; a rating with no words
gets a warm, product-specific thanks instead of a bare one."""
import json
from types import SimpleNamespace

import pytest

from app.domain.models import Review
from app.response_quality.core import make_plan, prepare
from app.services.publication_policy import validate_for_publication, validate_live_publication
from app.v3 import pilot
from app.v3.shadow import message_from_doc
from app.v3.snapshot import load_snapshot
from app.v3.text import customer_tags, customer_text

S = load_snapshot()
ST = SimpleNamespace(v3_knowledge_snapshot_id=S.snapshot_id)
HAND, TONIC, SERUM, POWDER = "252442517", "535581674", "305101272", "535580776"
OWNER_REFERENCE = "Евгения, спасибо за высокую оценку крема для рук EVETIS! Нам очень приятно, что выбрали именно его."


def msg(nm=HAND, rating=5, text="", tags=None, name="Евгения"):
    return {"nm_id": nm, "text": text, "pros": "", "cons": "", "rating": rating, "buyer_name": name,
            "entity_type": "review", "tags": tags or []}


def run(m, text):
    return prepare(m, "", S, render=lambda *_a, **_k: text, force_generation=True)

# A. ingestion ----------------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    (["Хорошо пахнет", "Удобно пользоваться"], ["Хорошо пахнет", "Удобно пользоваться"]),
    (None, []), ([], []), (["  ", "цена "], ["цена"]),
])
def test_bables_parsed_from_wb(raw, expected):
    fb = {"id": "F1", "productValuation": 5, "createdDate": "2026-10-07T03:06:39Z", "text": "",
          "productDetails": {"nmId": 252442517}, **({"bables": raw} if raw is not None else {})}
    assert Review.from_wb_feedback(fb).bables == expected


def test_bables_stored_and_restored():
    from app.services.repository import _initial_doc
    from app.services.pipeline import _review_from_doc
    doc = _initial_doc(Review(review_id="F1", bables=["Хорошо пахнет"]), 60)
    assert doc["bables"] == ["Хорошо пахнет"]
    assert _review_from_doc(doc).bables == ["Хорошо пахнет"]
    assert message_from_doc("c1", doc)["tags"] == ["Хорошо пахнет"]


def test_customer_text_includes_tags_once():
    m = msg(text="Отличный крем", tags=["Хорошо пахнет"])
    assert customer_tags(m) == ["Хорошо пахнет"]
    assert customer_text(m) == "Отличный крем Теги покупателя: Хорошо пахнет."
    assert customer_text(msg()) == ""

# B. operator card --------------------------------------------------------------------
def test_card_shows_tags_only_when_present():
    from app.services.pipeline import build_card
    base = {"product_name": "Крем", "nm_id": HAND, "rating": 5, "text": "", "final_answer": "Спасибо!"}
    with_tags, _ = build_card({**base, "bables": ["Хорошо пахнет", "Удобно пользоваться"]}, "c1")
    without, _ = build_card(base, "c1")
    assert "Теги покупателя:</b> Хорошо пахнет, Удобно пользоваться" in with_tags
    assert "Теги покупателя" not in without

# C. V2 prompt ----------------------------------------------------------------------------
def test_v2_prompt_and_classification_see_tags():
    from app.services.engine_prompt_service import EnginePromptService
    from app.communication_engine.builder.prompt_builder import PromptBuilder
    from app.communication_engine.config import EngineConfig
    review = Review(review_id="F1", rating=5, nm_id=HAND, bables=["Хорошо пахнет"])
    inp = EnginePromptService.to_input(review)
    assert inp.tags == ("Хорошо пахнет",) and "Хорошо пахнет" in inp.combined_text
    builder = PromptBuilder(EngineConfig.load() if hasattr(EngineConfig, "load") else EngineConfig())
    header = builder._user(SimpleNamespace(classification=SimpleNamespace(
        communication_type=inp.communication_type, marketplace=SimpleNamespace(value="wb"), scenario=[])), inp)
    assert "Теги покупателя" in header and "Хорошо пахнет" in header
    empty = builder._user(SimpleNamespace(classification=SimpleNamespace(
        communication_type=inp.communication_type, marketplace=SimpleNamespace(value="wb"), scenario=[])),
        EnginePromptService.to_input(Review(review_id="F2", rating=5)))
    assert "Теги покупателя" in empty and "—" in empty.split("Теги покупателя")[1]

# D. tags as customer words, polarity from their meaning -------------------------------
@pytest.mark.parametrize("tags,rating,expected", [
    (["Хорошо пахнет"], 5, {"fragrance_liked"}),
    (["Удобно пользоваться"], 5, {"convenience"}),
    (["Хорошо увлажняет"], 5, {"result_liked"}),
    (["Плохо пахнет"], 2, {"product_disliked"}),
    (["Неудобный дозатор"], 3, {"dispenser_inconvenient"}),
])
def test_tag_meaning_drives_aspects(tags, rating, expected):
    keys = {a.key for a in make_plan(msg(tags=tags, rating=rating), S).aspects}
    assert expected <= keys
    assert not ({"fragrance_liked", "convenience"} & keys and rating <= 2)


@pytest.mark.parametrize("tags", [["Цена"], ["Качество"], ["Запах"]])
def test_neutral_tag_is_neither_praise_nor_complaint(tags):
    keys = {a.key for a in make_plan(msg(tags=tags), S).aspects}
    assert keys == {"rating_only_thanks"}


def test_tag_evidence_allows_acknowledgement_not_brand_claim():
    m = msg(tags=["Хорошо пахнет", "Удобно пользоваться"])
    ok = run(m, "Евгения, спасибо за высокую оценку! Рады, что аромат Вам понравился и пользоваться кремом удобно.")
    assert ok.status == "READY" and ok.quality.verdict == "GOOD"
    claim = run(m, "Евгения, спасибо! У крема приятный аромат.")
    assert claim.final_policy["verdict"] == "BLOCK"


def test_language_payload_carries_tags():
    from app.response_quality.language import LanguageRenderer

    class Spy:
        def structured_timed(self, system, user, name, schema):
            self.payload = json.loads(user)
            return {"text": OWNER_REFERENCE}, {}, 0, "spy"
    spy = Spy()
    m = msg(tags=["Хорошо пахнет"])
    LanguageRenderer(spy)(m, make_plan(m, S))
    assert spy.payload["customer"]["tags"] == ["Хорошо пахнет"]
    assert "public_identity" in spy.payload["product_capabilities"]

# F. rating without words ------------------------------------------------------------------
def test_owner_reference_answer_is_good():
    r = run(msg(), OWNER_REFERENCE)
    assert r.status == "READY" and r.final_policy["verdict"] == "PASS" and r.quality.verdict == "GOOD"
    assert validate_live_publication(OWNER_REFERENCE, msg(), ST)["verdict"] == "PASS"


def test_bare_thanks_is_flagged_as_weak():
    r = run(msg(), "Евгения, благодарим за высокую оценку!")
    assert r.quality.verdict == "NEEDS_IMPROVEMENT"
    assert "missing_aspect:rating_only_thanks" in r.quality.reasons


@pytest.mark.parametrize("nm,phrase", [(HAND, "наш крем для рук"), (TONIC, "наш тоник"),
                                       (SERUM, "нашу сыворотку"), (POWDER, "нашу пудру")])
def test_deterministic_fallback_names_the_product(nm, phrase):
    r = prepare(msg(nm=nm, name="Анна"), "", S, force_generation=True)
    assert r.status == "READY" and phrase in r.text and r.text.startswith("Анна, ")
    assert r.quality.dimensions["missed_customer_signal_penalty"] == "GOOD"
    assert validate_for_publication(r.text, msg(nm=nm), ST)["verdict"] != "BLOCK"
    assert validate_live_publication(r.text, msg(nm=nm), ST)["verdict"] != "BLOCK"


def test_low_rating_without_words_regrets_and_asks():
    p = make_plan(msg(rating=2, name=""), S)
    assert [a.key for a in p.aspects] == ["rating_only_low"]
    assert any(c["unknown"] == "specific_difficulty" for c in p.clarifications)
    r = prepare(msg(rating=2, name=""), "", S, force_generation=True)
    assert r.status == "READY" and r.text.startswith("Жаль") and "Расскажите" in r.text


def test_written_text_keeps_previous_behaviour():
    assert "rating_only_thanks" not in {a.key for a in make_plan(msg(text="Норм"), S).aspects}


def test_unverified_product_still_human_review():
    assert prepare(msg(nm="999000321"), "", S, force_generation=True).status == "HUMAN_REVIEW"

# G. pilot identity includes tags -------------------------------------------------------------
def test_pilot_source_hash_covers_tags():
    assert pilot.source_sha(msg()) != pilot.source_sha(msg(tags=["Хорошо пахнет"]))
