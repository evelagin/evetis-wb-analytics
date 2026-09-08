"""Engine configuration — all paths and thresholds live here.

Nothing else in the engine reads the filesystem layout or hard-codes a numeric
threshold; they receive an ``EngineConfig`` via dependency injection.
"""
from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, Field

from app.communication_engine.constants import Marketplace, Namespace, Sentiment

_PACKAGE_DIR = Path(__file__).resolve().parent
_DEFAULT_KNOWLEDGE_ROOT = _PACKAGE_DIR / "knowledge"


class EngineConfig(BaseModel):
    """Immutable configuration for the whole engine."""

    model_config = {"frozen": True, "arbitrary_types_allowed": True}

    # --- knowledge storage ---
    knowledge_root: Path = Field(default=_DEFAULT_KNOWLEDGE_ROOT)
    file_extension: str = ".md"

    # brand documents always included in every prompt (order preserved)
    brand_core_docs: tuple[str, ...] = ("brand", "tone", "response_rules")
    # brand document that holds forbidden phrases / medical bans (for validators)
    brand_forbidden_doc: str = "forbidden"

    # order of sections in the assembled system prompt
    section_order: tuple[Namespace, ...] = (
        Namespace.BRAND,
        Namespace.PRODUCTS,
        Namespace.CASES,
        Namespace.MARKETPLACES,
    )
    # marketplace enum -> knowledge document id (files are named by full name)
    marketplace_docs: dict[Marketplace, str] = Field(
        default_factory=lambda: {
            Marketplace.WB: "wildberries",
        }
    )
    section_titles: dict[Namespace, str] = Field(
        default_factory=lambda: {
            Namespace.BRAND: "БРЕНД EVETIS",
            Namespace.PRODUCTS: "ПРОДУКТ",
            Namespace.CASES: "СЦЕНАРИЙ",
            Namespace.MARKETPLACES: "ПЛОЩАДКА",
        }
    )

    # --- classifier thresholds ---
    sentiment_negative_max_rating: int = 2   # rating <= this -> negative
    sentiment_positive_min_rating: int = 4   # rating >= this -> positive
    language_cyrillic_min_ratio: float = 0.3  # >= this share of cyrillic -> ru
    # scenarios that force high urgency regardless of rating
    high_urgency_scenarios: tuple[str, ...] = ("allergy", "irritation")
    high_urgency_max_rating: int = 2         # rating <= this -> high urgency
    # negation markers: a scenario keyword preceded by one of these (within the
    # look-back window) is treated as negated and does NOT match.
    negation_markers: tuple[str, ...] = ("не ", "нет ", "без ", "не было", "никаких")
    negation_lookback: int = 16              # chars to look back before a keyword

    # --- validator limits ---
    # max answer length (characters) by sentiment bucket; WB hard limit is 1000
    length_limits: dict[Sentiment, int] = Field(
        default_factory=lambda: {
            Sentiment.POSITIVE: 500,
            Sentiment.NEUTRAL: 800,
            Sentiment.NEGATIVE: 900,
        }
    )
    wb_hard_limit: int = 1000
    duplicate_similarity_threshold: float = 0.82

    # --- prompt versioning ---
    prompt_version_prefix: str = "engine_v1"
    user_template_version: int = 1

    # fail-fast: refuse to build the engine if the knowledge base is invalid
    # (override with env VALIDATE_KNOWLEDGE_ON_STARTUP=false)
    validate_knowledge_on_startup: bool = Field(
        default_factory=lambda: os.environ.get("VALIDATE_KNOWLEDGE_ON_STARTUP", "true").lower()
        != "false"
    )

    def doc_path(self, namespace: Namespace, doc_id: str) -> Path:
        return self.knowledge_root / namespace.value / f"{doc_id}{self.file_extension}"

    def namespace_dir(self, namespace: Namespace) -> Path:
        return self.knowledge_root / namespace.value


def default_config() -> EngineConfig:
    return EngineConfig()
