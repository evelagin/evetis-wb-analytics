"""Duplicate validator: flags answers too similar to recent ones."""
from __future__ import annotations

from difflib import SequenceMatcher

from app.communication_engine.config import EngineConfig
from app.communication_engine.models.validation import Severity, ValidationIssue
from app.communication_engine.validators.base import ValidationInput


class DuplicateValidator:
    name = "duplicate"

    def __init__(self, config: EngineConfig):
        self._config = config

    def validate(self, data: ValidationInput) -> list[ValidationIssue]:
        answer = data.answer.strip().lower()
        if not answer:
            return []
        threshold = self._config.duplicate_similarity_threshold
        for previous in data.recent_answers:
            ratio = SequenceMatcher(None, answer, (previous or "").strip().lower()).ratio()
            if ratio >= threshold:
                return [ValidationIssue(
                    validator=self.name, severity=Severity.ERROR, message="too similar to a recent answer",
                    detail=f"similarity={ratio:.2f}")]
        return []
