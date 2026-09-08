"""Assembled context and final prompt bundle."""
from __future__ import annotations

from pydantic import BaseModel, Field

from app.communication_engine.constants import Namespace
from app.communication_engine.models.classification import Classification
from app.communication_engine.models.knowledge import KnowledgeRef


class ContextBlock(BaseModel):
    """One knowledge document selected for the prompt."""

    model_config = {"frozen": True}

    ref: KnowledgeRef
    namespace: Namespace
    title: str
    body: str                                 # knowledge text used for grounding
    version: int = 1
    constraints: tuple[str, ...] = ()         # prohibited claims (NOT grounding text)


class PromptContext(BaseModel):
    """Everything the PromptBuilder needs, with no knowledge of storage."""

    model_config = {"frozen": True}

    classification: Classification
    blocks: tuple[ContextBlock, ...] = Field(default_factory=tuple)

    def block_ids(self) -> list[str]:
        return [str(b.ref) for b in self.blocks]


class PromptBundle(BaseModel):
    """Final system + user prompts ready for the LLM client."""

    model_config = {"frozen": True}

    system: str
    user: str
    prompt_version: str
    classification: Classification
