"""Communication Engine v2 as the PRIMARY draft generator.

Verifies the cutover: with V2_PRIMARY=true the Telegram draft is produced by the
verified-KB engine + validators, validator/moderation flags surface on the card,
a v2 fault falls back to reviews_v1 (never regresses), and publishing is untouched.
"""
from __future__ import annotations

from app.domain.models import GenerationResult
from app.services.pipeline import run_poll
from tests.conftest import make_deps


def _fb(**details):
    return {
        "id": details.pop("id", "REVIEW_1"),
        "productValuation": details.pop("rating", 5),
        "createdDate": "2026-07-20T10:00:00Z",
        "text": details.pop("text", "Отличный крем!"),
        "userName": "Анна",
        "productDetails": {
            "productName": details.pop("productName", "Крем"),
            "supplierArticle": details.pop("supplierArticle", "438775437"),
            "nmId": details.pop("nmId", 111),
            "imtId": 222,
            "brandName": "EVETIS",
        },
    }


class FixedOpenAI:
    def __init__(self, text):
        self._text = text
        self.calls = 0

    def generate_answer(self, system, user):
        self.calls += 1
        return GenerationResult(
            text=self._text, model="gpt-5.6-terra", prompt_version="reviews_v1",
            usage={"input_tokens": 5, "output_tokens": 3}, latency_ms=9, request_id="p",
        )


class BrokenEngine:
    def build_prompt(self, review):
        raise RuntimeError("engine boom")


# --- baseline: default (no primary) still uses reviews_v1 --------------------
def test_default_uses_reviews_v1():
    deps = make_deps([_fb()])
    run_poll(deps)
    # prompt_version recorded on the synced row is the reviews_v1 one
    assert deps.bq.current[-1]["prompt_version"] == "reviews_v1"


# --- primary: draft comes from the engine (engine_v1 prompt version) ---------
def test_primary_generates_via_engine():
    deps = make_deps([_fb(supplierArticle="438775437")], primary=True)
    summary = run_poll(deps)
    assert summary["processed"] == 1 and summary["errors"] == 0
    assert len(deps.telegram.sent) == 1          # exactly one card
    assert deps.wb.published == []               # no publish
    row = deps.bq.current[-1]
    assert row["prompt_version"].startswith("engine_v1:")   # engine, not reviews_v1


# --- primary: a validator failure is surfaced on the card -------------------
def test_primary_validator_flag_on_card():
    # acne_serum verified ниацинамид is 5%; a 10% claim must trip the numeric
    # validator and the card must carry a visible v2 warning.
    deps = make_deps(
        [_fb(supplierArticle="305101361", text="про прыщи")],
        primary=True, openai=FixedOpenAI("Ниацинамид 10% отлично работает"),
    )
    run_poll(deps)
    card = deps.telegram.sent[0][1]
    assert "Проверка v2" in card
    assert "10%" in card


# --- primary: unresolved product is flagged for manual moderation -----------
def test_primary_unresolved_product_flagged():
    deps = make_deps(
        [_fb(supplierArticle="nope", nmId=0, productName="нечто неизвестное")],
        primary=True,
    )
    run_poll(deps)
    card = deps.telegram.sent[0][1]
    assert "Проверка v2" in card
    assert "не распознан" in card


# --- primary: a v2 fault falls back to reviews_v1 (never regress) -----------
def test_primary_falls_back_to_reviews_v1_on_engine_error():
    deps = make_deps([_fb()], primary=True, engine=BrokenEngine())
    summary = run_poll(deps)
    assert summary["processed"] == 1 and summary["errors"] == 0
    assert len(deps.telegram.sent) == 1
    row = deps.bq.current[-1]
    assert row["prompt_version"] == "reviews_v1"          # fell back
    assert "Проверка v2" in deps.telegram.sent[0][1]      # fallback noted


# --- wiring: build_primary_engine / build_shadow_components ------------------
def test_wiring_primary_vs_shadow():
    from app.dependencies import build_primary_engine, build_shadow_components
    from tests.conftest import make_settings

    # primary on -> engine built, shadow suppressed
    s = make_settings(communication_engine_v2_enabled=True, communication_engine_v2_primary=True)
    assert build_primary_engine(s) is not None
    assert build_shadow_components(s, openai=object(), bq="BQ") == (None, None)

    # shadow on -> no primary engine, shadow built
    s2 = make_settings(communication_engine_v2_enabled=True, communication_engine_v2_shadow_only=True)
    assert build_primary_engine(s2) is None
    eng, repo = build_shadow_components(s2, openai=object(), bq="BQ")
    assert eng is not None and repo == "BQ"

    # fully off -> nothing
    s3 = make_settings()
    assert build_primary_engine(s3) is None
    assert build_shadow_components(s3, openai=object(), bq="BQ") == (None, None)
