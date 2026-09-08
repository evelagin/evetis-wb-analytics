"""Domain models: the normalized review and the communication record.

`Review` mirrors exactly the fields the n8n workflow extracted from
`data.feedbacks[]`, so the OpenAI prompt receives identical input.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict
from typing import Any


def make_doc_id(channel: str, entity_type: str, source_id: str) -> str:
    """Deterministic, short (20 hex chars) Firestore document id.

    Used both as the Firestore key and inside Telegram callback_data. WB review
    ids can be long; a 20-char hash keeps callback_data well under Telegram's
    64-byte limit while staying collision-safe for this volume.
    """
    raw = f"{channel}|{entity_type}|{source_id}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:20]


@dataclass
class Review:
    """One WB feedback, normalized to the same shape the n8n Code node produced."""

    platform: str = "WB"
    review_id: str = ""
    rating: Any = None
    created_date: str = ""
    text: str = ""
    pros: str = ""
    cons: str = ""
    user_name: str = ""
    product_name: str = ""
    supplier_article: str = ""
    nm_id: str = ""
    imt_id: str = ""
    barcode: str = ""
    brand_name: str = ""
    current_answer: Any = None
    state: str = ""

    @classmethod
    def from_wb_feedback(cls, fb: dict) -> "Review":
        details = fb.get("productDetails") or {}
        return cls(
            platform="WB",
            review_id=str(fb.get("id", "")),
            rating=fb.get("productValuation"),
            created_date=fb.get("createdDate", "") or "",
            text=fb.get("text", "") or "",
            pros=fb.get("pros", "") or "",
            cons=fb.get("cons", "") or "",
            user_name=fb.get("userName", "") or "",
            product_name=details.get("productName", "") or "",
            supplier_article=details.get("supplierArticle", "") or "",
            nm_id=str(details.get("nmId", "") or ""),
            imt_id=str(details.get("imtId", "") or ""),
            barcode=str(details.get("barcode", "") or ""),
            brand_name=details.get("brandName", "") or "",
            current_answer=fb.get("answer"),
            state=fb.get("state", "") or "",
        )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Question:
    """One WB buyer question, normalized to a shape the engine adapter accepts.

    Kept attribute-compatible with ``Review`` (empty rating/pros/cons/user_name) so
    the same ``EnginePromptService`` adapter can build a prompt from it with
    ``communication_type=question`` — no special-casing in the engine.
    """

    platform: str = "WB"
    question_id: str = ""
    created_date: str = ""
    text: str = ""
    product_name: str = ""
    supplier_article: str = ""
    nm_id: str = ""
    imt_id: str = ""
    barcode: str = ""
    brand_name: str = ""
    state: str = ""
    # review-compatible fields (always empty for a question) so the adapter's
    # `to_input()` can read them uniformly:
    rating: Any = None
    pros: str = ""
    cons: str = ""
    user_name: str = ""

    @classmethod
    def from_wb_question(cls, q: dict) -> "Question":
        details = q.get("productDetails") or {}
        return cls(
            platform="WB",
            question_id=str(q.get("id", "")),
            created_date=q.get("createdDate", "") or "",
            text=q.get("text", "") or "",
            product_name=details.get("productName", "") or "",
            supplier_article=details.get("supplierArticle", "") or "",
            nm_id=str(details.get("nmId", "") or ""),
            imt_id=str(details.get("imtId", "") or ""),
            barcode=str(details.get("barcode", "") or ""),
            brand_name=details.get("brandName", "") or "",
            state=q.get("state", "") or "",
        )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AnswerVersion:
    text: str
    source: str  # "ai" | "manual" | "regenerated"
    generation_number: int
    openai_model: str = ""
    prompt_version: str = ""
    created_at: str = ""


@dataclass
class GenerationResult:
    """What openai_client returns for one generation."""

    text: str
    model: str
    prompt_version: str
    usage: dict = field(default_factory=dict)
    latency_ms: int = 0
    request_id: str = ""
