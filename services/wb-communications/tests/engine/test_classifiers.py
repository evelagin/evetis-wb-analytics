from __future__ import annotations

from app.communication_engine.classifier.marketplace_classifier import MarketplaceClassifier
from app.communication_engine.classifier.product_classifier import ProductClassifier
from app.communication_engine.classifier.review_classifier import LanguageDetector
from app.communication_engine.classifier.sentiment_classifier import SentimentClassifier
from app.communication_engine.constants import Language, Marketplace, Sentiment, Urgency
from app.communication_engine.models.classification import ReviewInput


def test_marketplace_aliases():
    mc = MarketplaceClassifier()
    from app.communication_engine.constants import ResolutionStatus

    wb = mc.classify(ReviewInput(platform="WB"))
    assert wb.marketplace is Marketplace.WB and wb.status is ResolutionStatus.RESOLVED
    assert mc.classify(ReviewInput(platform="wildberries")).marketplace is Marketplace.WB


def test_unknown_platform_is_not_silently_wb():
    """WB-only engine: a foreign/unknown platform must NOT become WB — it is
    unsupported and routes to manual moderation."""
    from app.communication_engine.constants import ResolutionStatus

    mc = MarketplaceClassifier()
    for platform in ("ozon", "unknown", "aliexpress", ""):
        res = mc.classify(ReviewInput(platform=platform))
        assert res.marketplace is None, platform
        assert res.status is ResolutionStatus.UNRESOLVED, platform


def test_unsupported_marketplace_flags_manual_moderation(engine):
    c = engine.classify(ReviewInput(platform="ozon", supplier_article="305101361"))
    assert c.marketplace is None
    assert c.needs_manual_moderation is True


def test_strict_classifier_raises_on_unsupported():
    import pytest

    from app.communication_engine.classifier.marketplace_classifier import UnsupportedMarketplace

    mc = MarketplaceClassifier()
    assert mc.classify_strict(ReviewInput(platform="wb")) is Marketplace.WB
    with pytest.raises(UnsupportedMarketplace):
        mc.classify_strict(ReviewInput(platform="ozon"))


def test_product_classifier_priority_chain(registry):
    pc = ProductClassifier(registry)
    assert pc.classify(ReviewInput(supplier_article="535580776")).method == "article"
    assert pc.classify(ReviewInput(supplier_article="535580776")).product_id == "enzyme_powder"
    nm = pc.classify(ReviewInput(nm_id="305101272"))
    assert nm.product_id == "moisturizing_serum" and nm.method == "nm_id"
    alias = pc.classify(ReviewInput(product_name="Энзимная пудра для умывания"))
    assert alias.product_id == "enzyme_powder" and alias.method == "alias"
    miss = pc.classify(ReviewInput(supplier_article="zzz"))
    assert miss.product_id is None and miss.status.value == "unresolved"


def test_sentiment_thresholds(config):
    sc = SentimentClassifier(config)
    assert sc.classify(ReviewInput(rating=1)) is Sentiment.NEGATIVE
    assert sc.classify(ReviewInput(rating=3)) is Sentiment.NEUTRAL
    assert sc.classify(ReviewInput(rating=5)) is Sentiment.POSITIVE
    assert sc.classify(ReviewInput(rating=None)) is Sentiment.NEUTRAL


def test_language_detector(config):
    ld = LanguageDetector(config)
    assert ld.detect(ReviewInput(text="Отличный крем")) is Language.RU
    assert ld.detect(ReviewInput(text="great product, love it")) is Language.OTHER
    assert ld.detect(ReviewInput(text="12345 !!!")) is Language.RU  # no letters -> default


def test_review_classifier_full_negative_irritation(engine):
    c = engine.classify(ReviewInput(platform="wb", rating=1, supplier_article="305101361",
                                    text="Началось жжение и раздражение"))
    assert c.product_id == "acne_serum"
    assert c.sentiment is Sentiment.NEGATIVE
    assert "irritation" in c.scenario
    assert c.urgency is Urgency.HIGH
    assert c.language is Language.RU


def test_review_classifier_positive_no_scenario(engine):
    c = engine.classify(ReviewInput(platform="wb", rating=5, supplier_article="535580776",
                                    text="Очень нравится, кожа мягкая"))
    assert c.marketplace is Marketplace.WB
    assert c.sentiment is Sentiment.POSITIVE
    assert c.urgency is Urgency.NORMAL
    assert c.scenario == ()


def test_urgency_high_by_scenario_even_if_rating_ok(engine):
    # rating 4 (not <=2), but allergy scenario forces high urgency
    c = engine.classify(ReviewInput(rating=4, supplier_article="305101361",
                                    text="После нанесения отёк и сыпь"))
    assert "allergy" in c.scenario
    assert c.urgency is Urgency.HIGH
