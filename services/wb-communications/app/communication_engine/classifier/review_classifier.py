"""Review classifier — the orchestrator.

Composes the marketplace/product/sentiment classifiers with deterministic
scenario, language and urgency detection to produce a full ``Classification``.
Scenario keywords and urgency come from the knowledge base (case front-matter),
so new scenarios need no code change.
"""
from __future__ import annotations

from app.communication_engine.classifier.marketplace_classifier import MarketplaceClassifier
from app.communication_engine.classifier.product_classifier import ProductClassifier
from app.communication_engine.classifier.sentiment_classifier import SentimentClassifier
from app.communication_engine.config import EngineConfig
from app.communication_engine.constants import Language, Sentiment, Urgency
from app.communication_engine.knowledge.registry import KnowledgeRegistry
from app.communication_engine.models.classification import Classification, ReviewInput


class ScenarioDetector:
    """Matches case keywords (from the registry) against the review text.

    Negation-aware: a keyword preceded by a negation marker (e.g. «аллергии не
    было») does not count as a match, so denied scenarios are not attached.
    """

    def __init__(self, registry: KnowledgeRegistry, config: EngineConfig):
        self._registry = registry
        self._config = config

    def _matches(self, text: str, keyword: str) -> bool:
        span = self._config.negation_lookback
        markers = self._config.negation_markers
        start = 0
        while True:
            idx = text.find(keyword, start)
            if idx == -1:
                return False
            before = text[max(0, idx - span): idx]
            after = text[idx + len(keyword): idx + len(keyword) + span]
            # negation may precede («не было аллергии») or follow («аллергии не было»)
            negated = any(m in before for m in markers) or any(m in after for m in markers)
            if not negated:
                return True  # a genuine, non-negated occurrence
            start = idx + len(keyword)

    def detect(self, review: ReviewInput) -> tuple[str, ...]:
        text = review.combined_text.lower()
        if not text:
            return ()
        matched: list[str] = []
        for rule in self._registry.scenario_rules():  # already priority-sorted
            if any(kw and self._matches(text, kw) for kw in rule.keywords):
                matched.append(rule.case_id)
        return tuple(matched)


class LanguageDetector:
    def __init__(self, config: EngineConfig):
        self._config = config

    def detect(self, review: ReviewInput) -> Language:
        text = review.combined_text
        letters = [c for c in text if c.isalpha()]
        if not letters:
            return Language.RU  # default channel language
        cyrillic = sum(1 for c in letters if "Ѐ" <= c <= "ӿ")
        ratio = cyrillic / len(letters)
        return Language.RU if ratio >= self._config.language_cyrillic_min_ratio else Language.OTHER


class UrgencyRule:
    def __init__(self, config: EngineConfig, registry: KnowledgeRegistry):
        self._config = config
        self._registry = registry

    def evaluate(self, review: ReviewInput, scenarios: tuple[str, ...]) -> Urgency:
        if review.rating is not None and review.rating <= self._config.high_urgency_max_rating:
            return Urgency.HIGH
        if set(scenarios) & set(self._config.high_urgency_scenarios):
            return Urgency.HIGH
        # any case explicitly tagged urgency: high in the knowledge base
        high_from_kb = {r.case_id for r in self._registry.scenario_rules() if r.urgency is Urgency.HIGH}
        if set(scenarios) & high_from_kb:
            return Urgency.HIGH
        return Urgency.NORMAL


class ReviewClassifier:
    def __init__(
        self,
        config: EngineConfig,
        registry: KnowledgeRegistry,
        marketplace: MarketplaceClassifier | None = None,
        product: ProductClassifier | None = None,
        sentiment: SentimentClassifier | None = None,
    ):
        self._config = config
        self._marketplace = marketplace or MarketplaceClassifier()
        self._product = product or ProductClassifier(registry)
        self._sentiment = sentiment or SentimentClassifier(config)
        self._scenario = ScenarioDetector(registry, config)
        self._language = LanguageDetector(config)
        self._urgency = UrgencyRule(config, registry)

    def classify(self, review: ReviewInput) -> Classification:
        scenarios = self._scenario.detect(review)
        resolution = self._product.classify(review)
        marketplace = self._marketplace.classify(review)
        return Classification(
            marketplace=marketplace.marketplace,
            marketplace_resolution_status=marketplace.status,
            communication_type=review.communication_type,
            product_id=resolution.product_id,
            product_resolution_status=resolution.status,
            resolution_method=resolution.method,
            rating=review.rating,
            sentiment=self._sentiment.classify(review),
            scenario=scenarios,
            language=self._language.detect(review),
            urgency=self._urgency.evaluate(review, scenarios),
        )
