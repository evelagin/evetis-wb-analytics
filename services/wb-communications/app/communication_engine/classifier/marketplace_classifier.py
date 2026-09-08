"""Marketplace classifier: resolves a review's platform string to a Marketplace.

This is a **Wildberries-only** engine. A platform that is not WB (or is unknown)
is deliberately NOT coerced to WB — it resolves to ``UNRESOLVED`` so the pipeline
routes the item to manual moderation instead of answering it with WB knowledge.
Only ``wb`` / ``wildberries`` (see ``MARKETPLACE_ALIASES``) are supported.
"""
from __future__ import annotations

from pydantic import BaseModel

from app.communication_engine.constants import (
    MARKETPLACE_ALIASES,
    Marketplace,
    ResolutionStatus,
)
from app.communication_engine.models.classification import ReviewInput


class UnsupportedMarketplace(ValueError):
    """Raised when a strict caller wants a hard failure on a non-WB platform."""


class MarketplaceResolution(BaseModel):
    """Result of marketplace classification.

    ``marketplace`` is ``None`` when the platform is unsupported; ``status`` then
    is ``UNRESOLVED`` and the caller must route to manual moderation.
    """

    model_config = {"frozen": True}

    marketplace: Marketplace | None
    status: ResolutionStatus
    platform: str


class MarketplaceClassifier:
    """Maps ``platform`` to a supported Wildberries marketplace, or unsupported.

    An empty platform is treated as unsupported by default: only an upstream
    adapter that is *guaranteed* WB-specific may assert WB for empty input, which
    it does by normalizing the platform to ``"wb"`` before the engine sees it.
    """

    def classify(self, review: ReviewInput) -> MarketplaceResolution:
        raw = review.platform or ""
        key = raw.strip().lower()
        marketplace = MARKETPLACE_ALIASES.get(key)
        if marketplace is None:
            return MarketplaceResolution(
                marketplace=None, status=ResolutionStatus.UNRESOLVED, platform=raw
            )
        return MarketplaceResolution(
            marketplace=marketplace, status=ResolutionStatus.RESOLVED, platform=raw
        )

    def classify_strict(self, review: ReviewInput) -> Marketplace:
        """Like :meth:`classify` but raises ``UnsupportedMarketplace`` instead of
        returning an unresolved result. For callers that prefer a hard failure."""
        resolution = self.classify(review)
        if resolution.marketplace is None:
            raise UnsupportedMarketplace(f"unsupported platform: {review.platform!r}")
        return resolution.marketplace
