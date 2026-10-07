"""PR7 integration adapter (NOT wired into the running pipeline yet).

Bridges the existing service layer (`app.domain.models.Review`) to the new
`communication_engine`. When you are ready to retire `reviews_v1`, swap
`PromptService` for this in `app/dependencies.py` and change `run_poll` to use
`build_prompt(review)` (which returns BOTH system and user, since the system
prompt is now context-dependent) and to run `validate()` after generation.

Keeping this adapter separate means the production service stays green until the
switch is made deliberately.
"""
from __future__ import annotations

from app.communication_engine.constants import CommunicationType
from app.communication_engine.engine import CommunicationEngine
from app.communication_engine.models.classification import ReviewInput
from app.communication_engine.models.prompt_context import PromptBundle, PromptContext
from app.communication_engine.models.validation import ValidationResult
from app.domain.models import Review


class UnsupportedEntityType(ValueError):
    """Raised for an unrecognized WB entity type — never silently treated as a review."""


# WB entity -> engine communication type. Reviews today; questions/chats later
# arrive as different WB entities and map here. Unknown types are a controlled
# error (route to manual moderation), NOT a silent default to review.
_ENTITY_TO_TYPE: dict[str, CommunicationType] = {
    "review": CommunicationType.REVIEW,
    "feedback": CommunicationType.REVIEW,
    "question": CommunicationType.QUESTION,
    "chat": CommunicationType.CHAT,
}


class EnginePromptService:
    """Facade the pipeline can depend on instead of the static PromptService."""

    def __init__(self, engine: CommunicationEngine):
        self._engine = engine

    @classmethod
    def create(cls) -> "EnginePromptService":
        return cls(CommunicationEngine.build())

    @staticmethod
    def to_input(
        review: Review, communication_type: CommunicationType = CommunicationType.REVIEW
    ) -> ReviewInput:
        return ReviewInput(
            platform=(review.platform or "wb"),
            communication_type=communication_type,
            rating=_as_int(review.rating),
            text=review.text or "",
            pros=review.pros or "",
            cons=review.cons or "",
            tags=tuple(getattr(review, "bables", None) or ()),
            user_name=review.user_name or "",
            product_name=review.product_name or "",
            supplier_article=review.supplier_article or "",
            nm_id=str(review.nm_id or ""),
            barcode=str(getattr(review, "barcode", "") or ""),
            brand_name=review.brand_name or "EVETIS",
            created_date=review.created_date or "",
        )

    @classmethod
    def type_for_entity(cls, entity_type: str) -> CommunicationType:
        """Map a WB entity type to a communication type.

        Raises ``UnsupportedEntityType`` for anything unrecognized — the caller
        routes that to manual moderation rather than answering it as a review.
        """
        key = (entity_type or "").lower()
        if key not in _ENTITY_TO_TYPE:
            raise UnsupportedEntityType(f"unsupported WB entity type: {entity_type!r}")
        return _ENTITY_TO_TYPE[key]

    def build_prompt(
        self, review: Review, communication_type: CommunicationType = CommunicationType.REVIEW
    ) -> PromptBundle:
        return self._engine.build_prompt(self.to_input(review, communication_type))

    def build_context(
        self, review: Review, communication_type: CommunicationType = CommunicationType.REVIEW
    ) -> PromptContext:
        return self._engine.build_context(self.to_input(review, communication_type))

    def validate(
        self, answer: str, context: PromptContext, recent_answers: tuple[str, ...] = ()
    ) -> ValidationResult:
        return self._engine.validate(answer, context, recent_answers)


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
