"""Shadow-integration tests for Communication Engine v2.

Covers the acceptance list: disabled → engine untouched; enabled → reviews_v1
unchanged; v2 error isolated; row recorded; no WB publish from v2; unresolved
product recorded; validation failure recorded; Telegram payload unchanged.
"""
from __future__ import annotations

from app.domain.models import GenerationResult
from app.services.pipeline import run_poll
from tests.conftest import (
    FakeShadowRepo,
    RaisingShadowEngine,
    FailingOpenAI,
    SAMPLE_FEEDBACK,
    make_deps,
)


def _fb(**details):
    fb = {
        "id": details.pop("id", "REVIEW_1"),
        "productValuation": details.pop("rating", 5),
        "createdDate": "2026-07-20T10:00:00Z",
        "text": details.pop("text", "Отличный крем!"),
        "userName": "Анна",
        "productDetails": {
            "productName": details.pop("productName", "Крем для лица увлажняющий"),
            "supplierArticle": details.pop("supplierArticle", "438775437"),
            "nmId": details.pop("nmId", 111),
            "imtId": 222,
            "brandName": "EVETIS",
        },
    }
    return fb


class CountingShadowEngine:
    """Spy: counts evaluate() calls and returns a trivial row."""

    def __init__(self):
        self.calls = 0

    def evaluate(self, review, *, old_answer, old_prompt_version):
        self.calls += 1
        return {"review_id": review.review_id, "v2_usable": True}


class FixedOpenAI:
    """Returns a caller-supplied answer text (used as the v2 shadow client)."""

    def __init__(self, text):
        self._text = text
        self.calls = 0

    def generate_answer(self, system, user):
        self.calls += 1
        return GenerationResult(
            text=self._text, model="gpt-4.1-mini", prompt_version="reviews_v1",
            usage={"input_tokens": 7, "output_tokens": 3}, latency_ms=11, request_id="v2",
        )


# 1) V2_ENABLED=false → the engine is never called ---------------------------
def test_disabled_engine_not_called():
    spy = CountingShadowEngine()
    repo = FakeShadowRepo()
    deps = make_deps(
        [_fb()], shadow=True, communication_engine_v2_enabled=False,
        shadow_engine=spy, shadow_repo=repo,
    )
    run_poll(deps)
    assert spy.calls == 0
    assert repo.rows == []


# 2) shadow enabled → reviews_v1 works exactly as before ---------------------
def test_shadow_on_old_pipeline_unchanged():
    deps = make_deps([_fb()], shadow=True)
    summary = run_poll(deps)
    assert summary["processed"] == 1 and summary["errors"] == 0
    # exactly one Telegram card was sent (no second message from v2)
    assert len(deps.telegram.sent) == 1
    # main answer still the reviews_v1 one
    assert "Ответ-вариант-1" in deps.telegram.sent[0][1]


# 3) v2 error → the old pipeline keeps running -------------------------------
def test_v2_error_does_not_break_pipeline():
    deps = make_deps([_fb()], shadow=True, shadow_engine=RaisingShadowEngine(),
                     shadow_repo=FakeShadowRepo())
    summary = run_poll(deps)
    assert summary["processed"] == 1 and summary["errors"] == 0
    assert len(deps.telegram.sent) == 1  # card still delivered


def test_v2_generation_failure_recorded_and_isolated():
    repo = FakeShadowRepo()
    deps = make_deps([_fb(supplierArticle="305101361")], shadow=True,
                     shadow_openai=FailingOpenAI(), shadow_repo=repo)
    summary = run_poll(deps)
    assert summary["processed"] == 1 and summary["errors"] == 0
    assert len(repo.rows) == 1
    row = repo.rows[0]
    assert row["v2_usable"] is False
    assert row["error_code"] == "OpenAIError"


# 4) v2 result is written to the shadow repository ---------------------------
def test_shadow_row_recorded_with_all_fields():
    repo = FakeShadowRepo()
    deps = make_deps([_fb(supplierArticle="438775437")], shadow=True, shadow_repo=repo)
    run_poll(deps)
    assert len(repo.rows) == 1
    row = repo.rows[0]
    for key in (
        "shadow_id", "review_id", "old_prompt_version", "old_answer", "v2_prompt_version",
        "v2_answer", "classification_json", "product_resolution_method",
        "marketplace_resolution_status", "product_resolution_status",
        "needs_manual_moderation", "validation_passed", "validation_issues_json",
        "v2_latency_ms", "v2_model", "shadow_at",
    ):
        assert key in row, key
    assert row["shadow_id"]  # non-empty unique id
    assert row["review_id"] == "REVIEW_1"
    assert row["old_prompt_version"] == "reviews_v1"
    assert row["product_id"] == "moisturizing_cream"
    assert row["product_resolution_method"] == "article"
    assert row["marketplace"] == "wb"
    assert row["marketplace_resolution_status"] == "resolved"
    assert row["needs_manual_moderation"] is False


# 5) no WB publish is ever triggered by v2 -----------------------------------
def test_v2_never_publishes_to_wb():
    deps = make_deps([_fb()], shadow=True)
    run_poll(deps)
    assert deps.wb.published == []  # poll never publishes; v2 must not either


# 6) unresolved product is recorded correctly --------------------------------
def test_unresolved_product_recorded():
    repo = FakeShadowRepo()
    deps = make_deps(
        [_fb(supplierArticle="does-not-exist", nmId=0, productName="нечто неизвестное")],
        shadow=True, shadow_repo=repo,
    )
    run_poll(deps)
    assert len(repo.rows) == 1
    row = repo.rows[0]
    assert row["product_id"] is None
    assert row["product_resolution_status"] == "unresolved"
    assert row["needs_manual_moderation"] is True
    assert row["v2_usable"] is False


# 7) a validators failure is recorded ----------------------------------------
def test_validation_failure_recorded():
    repo = FakeShadowRepo()
    # acne_serum verified ниацинамид is 5%; a v2 answer claiming 10% must fail the
    # numeric-grounding validator -> validation_passed False, v2_usable False.
    deps = make_deps(
        [_fb(supplierArticle="305101361", text="про прыщи")],
        shadow=True, shadow_openai=FixedOpenAI("Ниацинамид 10% отлично работает"),
        shadow_repo=repo,
    )
    run_poll(deps)
    assert len(repo.rows) == 1
    row = repo.rows[0]
    assert row["validation_passed"] is False
    assert row["v2_usable"] is False
    assert "numeric_grounding" in row["validation_issues_json"]


# 8) the Telegram payload is identical with shadow on vs off -----------------
def test_telegram_payload_unchanged_by_shadow():
    off = make_deps([_fb()])
    on = make_deps([_fb()], shadow=True)
    run_poll(off)
    run_poll(on)
    assert off.telegram.sent[0][1] == on.telegram.sent[0][1]  # identical card text
    assert off.telegram.sent[0][2] == on.telegram.sent[0][2]  # identical keyboard


# --- fix 2: SHADOW_ONLY=false is fail-closed --------------------------------
def _settings_flags(enabled: bool, shadow_only: bool):
    from tests.conftest import make_settings

    return make_settings(
        communication_engine_v2_enabled=enabled,
        communication_engine_v2_shadow_only=shadow_only,
    )


def test_shadow_only_false_is_fail_closed(caplog):
    """enabled=true + shadow_only=false must NOT build the engine (no publish
    path) and must emit a CRITICAL log — never a fallback to non-shadow."""
    import logging

    from app.dependencies import build_shadow_components

    settings = _settings_flags(enabled=True, shadow_only=False)
    with caplog.at_level(logging.CRITICAL):
        engine, repo = build_shadow_components(settings, openai=object(), bq=object())
    assert engine is None and repo is None      # engine not created -> no publish path
    assert any(r.levelno == logging.CRITICAL for r in caplog.records)


def test_shadow_only_false_pipeline_never_publishes():
    # Even if someone flips SHADOW_ONLY off, the wired deps have no shadow engine,
    # so _run_shadow is a no-op and nothing v2 can publish.
    from app.dependencies import build_shadow_components

    deps = make_deps(
        [_fb()],
        communication_engine_v2_enabled=True, communication_engine_v2_shadow_only=False,
    )
    eng, repo = build_shadow_components(deps.settings, openai=object(), bq=object())
    deps.shadow_engine, deps.shadow_repo = eng, repo
    assert eng is None and repo is None
    summary = run_poll(deps)
    assert summary["processed"] == 1
    assert deps.wb.published == []


def test_enabled_shadow_only_true_builds_engine():
    from app.dependencies import build_shadow_components

    settings = _settings_flags(enabled=True, shadow_only=True)
    sentinel_bq = object()
    engine, repo = build_shadow_components(settings, openai=object(), bq=sentinel_bq)
    assert engine is not None
    assert repo is sentinel_bq


# --- fix 3: insert_shadow returns False is detected + logged -----------------
def test_shadow_persistence_failure_logged_but_pipeline_ok(caplog):
    import logging

    repo = FakeShadowRepo(ok=False)  # records row but reports failure
    deps = make_deps([_fb()], shadow=True, shadow_repo=repo)
    with caplog.at_level(logging.WARNING):
        summary = run_poll(deps)
    assert summary["processed"] == 1 and summary["errors"] == 0
    assert len(deps.telegram.sent) == 1                 # card still delivered
    assert len(repo.rows) == 1                          # row was attempted
    assert any("shadow persistence failed" in r.getMessage() for r in caplog.records)


# --- fix 4: shadow_id is unique per prompt version --------------------------
def test_shadow_id_differs_across_prompt_versions():
    from app.services.shadow_engine import compute_shadow_id

    a = compute_shadow_id("REVIEW_1", "reviews_v1", "engine_v1_a", "1")
    b = compute_shadow_id("REVIEW_1", "reviews_v1", "engine_v1_b", "1")
    assert a != b                       # different v2 prompt version -> different id
    # same logical run is idempotent (stable id -> BigQuery dedup on retry)
    assert a == compute_shadow_id("REVIEW_1", "reviews_v1", "engine_v1_a", "1")


def test_two_real_evaluations_different_versions_both_persist():
    """Full path: two real evaluate() calls for the SAME review_id but different
    products yield different v2 prompt versions -> different shadow_ids -> both
    rows persist (no BigQuery dedup). Exercises evaluate(), not just
    compute_shadow_id()."""
    from app.domain.models import Review
    from tests.conftest import FakeOpenAI, make_settings, make_shadow_engine

    settings = make_settings(communication_engine_v2_enabled=True)
    engine = make_shadow_engine(settings, openai=FakeOpenAI())
    repo = FakeShadowRepo()

    # same review_id, two different products -> two different engine prompt versions
    r_serum = Review(platform="WB", review_id="REVIEW_1", supplier_article="305101361")
    r_powder = Review(platform="WB", review_id="REVIEW_1", supplier_article="535580776")

    row1 = engine.evaluate(r_serum, old_answer="a", old_prompt_version="reviews_v1", run_version=1)
    row2 = engine.evaluate(r_powder, old_answer="a", old_prompt_version="reviews_v1", run_version=1)

    # the engine really did produce two different v2 prompt versions
    assert row1["v2_prompt_version"] != row2["v2_prompt_version"]
    # ... so the deterministic shadow_ids differ
    assert row1["shadow_id"] != row2["shadow_id"]

    assert repo.insert_shadow(row1) is True
    assert repo.insert_shadow(row2) is True
    ids = [r["shadow_id"] for r in repo.rows]
    assert len(ids) == len(set(ids)) == 2          # both unique -> both persist
