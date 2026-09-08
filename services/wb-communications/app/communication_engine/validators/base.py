"""Validator protocol and the input passed to every validator."""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

from app.communication_engine.models.prompt_context import PromptContext
from app.communication_engine.models.validation import ValidationIssue


class ValidationInput(BaseModel):
    """Everything a validator may need to judge a generated answer."""

    model_config = {"frozen": True}

    answer: str
    context: PromptContext
    recent_answers: tuple[str, ...] = Field(default_factory=tuple)


@runtime_checkable
class Validator(Protocol):
    """A post-generation check. Pure: same input -> same issues."""

    @property
    def name(self) -> str: ...

    def validate(self, data: ValidationInput) -> list[ValidationIssue]: ...
