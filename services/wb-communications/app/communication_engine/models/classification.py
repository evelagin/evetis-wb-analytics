"""Input review and its classification result."""
from __future__ import annotations

from pydantic import BaseModel, Field

from app.communication_engine.constants import (
    CommunicationType,
    Language,
    Marketplace,
    ResolutionStatus,
    Sentiment,
    Urgency,
)


class ReviewInput(BaseModel):
    """Normalized communication fed into the engine.

    Type-agnostic within Wildberries: the same shape is produced from WB
    feedbacks, questions and chats, so a single engine serves every WB channel.
    Other marketplaces are separate systems and are not handled here.
    """

    model_config = {"frozen": True}

    platform: str = "wb"
    communication_type: CommunicationType = CommunicationType.REVIEW
    rating: int | None = None
    text: str = ""
    pros: str = ""
    cons: str = ""
    user_name: str = ""
    product_name: str = ""
    supplier_article: str = ""
    nm_id: str = ""
    barcode: str = ""
    brand_name: str = "EVETIS"
    created_date: str = ""

    @property
    def combined_text(self) -> str:
        """All buyer-authored text, used for keyword/language classification."""
        return " ".join(p for p in (self.text, self.pros, self.cons) if p).strip()


class Classification(BaseModel):
    """Deterministic classification of a communication."""

    model_config = {"frozen": True}

    # None when the platform is not Wildberries (or unknown). A WB-only engine
    # must never silently treat a foreign/unknown platform as WB.
    marketplace: Marketplace | None = None
    marketplace_resolution_status: ResolutionStatus = ResolutionStatus.RESOLVED
    communication_type: CommunicationType = CommunicationType.REVIEW
    product_id: str | None = None
    product_resolution_status: ResolutionStatus = ResolutionStatus.RESOLVED
    resolution_method: str = ""  # article | nm_id | barcode | alias | none
    rating: int | None = None
    sentiment: Sentiment = Sentiment.NEUTRAL
    scenario: tuple[str, ...] = Field(default_factory=tuple)
    language: Language = Language.RU
    urgency: Urgency = Urgency.NORMAL

    @property
    def needs_manual_moderation(self) -> bool:
        """We must not auto-answer when the product is missing/ambiguous OR the
        platform is not a supported Wildberries channel."""
        return (
            self.product_resolution_status is ResolutionStatus.UNRESOLVED
            or self.marketplace_resolution_status is ResolutionStatus.UNRESOLVED
        )
