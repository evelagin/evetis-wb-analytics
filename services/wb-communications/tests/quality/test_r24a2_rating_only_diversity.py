"""R2.4A.2: answers to a high rating without words are short, warm, claim-free, stable per
review and varied across reviews (no single template dominates)."""
import json
import re
from collections import Counter
from types import SimpleNamespace

import pytest

from app.response_quality import language
from app.response_quality.core import (RATING_ONLY_VARIANTS, make_plan, prepare, stable_variant)
from app.services.publication_policy import validate_for_publication, validate_live_publication
from app.v3.snapshot import load_snapshot

S = load_snapshot()
ST = SimpleNamespace(v3_knowledge_snapshot_id=S.snapshot_id)
PRODUCTS = ("252442517", "535581674", "305101272", "535580776")
NAMES = ("Анна", "Ольга", "", "Мария", "Ирина", "Светлана")
CLAIMS = re.compile(r"аромат|пахн|увлажн|результат|эффект|снова|повторн|подойд|подходит|состав|кож[аеиу]|"
                    r"мягк|нежн|впиты|отличн\w* выбор|лучш", re.I)


def msg(i, rating=5):
    return {"communication_id": f"wb:review:SYN{i:03d}", "source_id": f"SYN{i:03d}", "entity_type": "review",
            "nm_id": PRODUCTS[i % len(PRODUCTS)], "text": "", "pros": "", "cons": "", "tags": [],
            "rating": rating, "buyer_name": NAMES[i % len(NAMES)]}


SAMPLE = [msg(i, rating=5 if i % 5 else 4) for i in range(24)]


def fallback(m):
    return prepare(m, "", S, force_generation=True)


def test_sample_is_varied_and_no_template_dominates():
    results = [fallback(m) for m in SAMPLE]
    texts = [r.text for r in results]
    structures = Counter(stable_variant(m, len(RATING_ONLY_VARIANTS)) for m in SAMPLE)
    openings = Counter(re.sub(r"^[А-ЯЁ][а-яё]+, ", "", t).split()[0].lower().strip("!,.") for t in texts)
    assert len(set(texts)) >= 18
    assert len(structures) >= 5
    assert max(structures.values()) / len(SAMPLE) <= 0.40
    assert max(openings.values()) / len(SAMPLE) <= 0.40
    ending = Counter("выбрали именно" in t for t in texts)
    assert ending[True] == 0


@pytest.mark.parametrize("m", SAMPLE, ids=lambda m: m["source_id"])
def test_every_sample_answer_is_ready_safe_and_claim_free(m):
    r = fallback(m)
    assert r.status == "READY" and r.quality.verdict == "GOOD"
    assert not CLAIMS.search(r.text)
    assert 1 <= len(re.findall(r"[.!?](?:\s|$)", r.text)) <= 3
    assert validate_for_publication(r.text, m, ST)["verdict"] != "BLOCK"
    assert validate_live_publication(r.text, m, ST)["verdict"] != "BLOCK"
    if m["buyer_name"]:
        assert r.text.startswith(m["buyer_name"] + ", ")


def test_same_review_gets_the_same_answer():
    for m in SAMPLE[:6]:
        assert fallback(m).text == fallback(dict(m)).text
        assert language.rating_only_style_hint(m) == language.rating_only_style_hint(dict(m))


@pytest.mark.parametrize("variant", range(len(RATING_ONLY_VARIANTS)))
@pytest.mark.parametrize("nm", PRODUCTS)
def test_every_variant_passes_both_policies(variant, nm):
    text = "Анна, " + RATING_ONLY_VARIANTS[variant].format(chosen="EVETIS")
    m = {**msg(0), "nm_id": nm}
    assert validate_for_publication(text, m, ST)["verdict"] != "BLOCK"
    assert validate_live_publication(text, m, ST)["verdict"] != "BLOCK"


@pytest.mark.parametrize("text", [
    "Ольга, как приятно видеть Вашу высокую оценку! Желаем приятного использования.",
    "Анна, очень рады Вашей пятёрке! Спасибо, что выбрали EVETIS.",
    "Мария, спасибо за пять звёзд! Рады, что Вы с EVETIS.",
    "Благодарим за высокую оценку нашего тоника — нам очень приятно.",
])
def test_varied_wording_without_product_name_is_good(text):
    m = {**msg(1), "buyer_name": ""}
    r = prepare(m, "", S, render=lambda *_a, **_k: text, force_generation=True)
    assert r.status == "READY" and r.quality.verdict == "GOOD", (text, r.quality.reasons)


def test_bare_thanks_is_still_weak():
    r = prepare(msg(2), "", S, render=lambda *_a, **_k: "Спасибо за высокую оценку!", force_generation=True)
    assert "missing_aspect:rating_only_thanks" in r.quality.reasons


def test_prompt_carries_a_stable_style_hint_only_for_rating_only():
    class Spy:
        def structured_timed(self, system, user, name, schema):
            self.system, self.payload = system, json.loads(user)
            return {"text": "Спасибо за высокую оценку! Рады, что Вы выбрали EVETIS."}, {}, 0, "spy"
    hints = set()
    for m in SAMPLE:
        spy = Spy()
        language.LanguageRenderer(spy)(m, make_plan(m, S))
        hints.add(spy.payload["rating_only_style_hint"])
    assert len(hints) >= 4
    assert "выбрал именно его" not in spy.system and "rating_only_style_hint" in spy.system
    spy = Spy()
    worded = {**msg(3), "text": "Хороший тоник"}
    language.LanguageRenderer(spy)(worded, make_plan(worded, S))
    assert "rating_only_style_hint" not in spy.payload


def test_low_rating_unchanged():
    assert [a.key for a in make_plan(msg(4, rating=2), S).aspects] == ["rating_only_low"]
