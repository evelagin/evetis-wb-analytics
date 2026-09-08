"""Length validator: per-sentiment length limits and the WB hard cap."""
from __future__ import annotations

from app.communication_engine.config import EngineConfig
from app.communication_engine.models.validation import Severity, ValidationIssue
from app.communication_engine.validators.base import ValidationInput


class LengthValidator:
    name = "length"

    def __init__(self, config: EngineConfig):
        self._config = config

    def validate(self, data: ValidationInput) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        length = len(data.answer.strip())
        if length == 0:
            issues.append(ValidationIssue(validator=self.name, severity=Severity.ERROR,
                                          message="empty answer"))
            return issues
        if length > self._config.wb_hard_limit:
            issues.append(ValidationIssue(
                validator=self.name, severity=Severity.ERROR, message="over WB hard limit",
                detail=f"{length} > {self._config.wb_hard_limit}"))
        sentiment = data.context.classification.sentiment
        soft = self._config.length_limits.get(sentiment)
        if soft is not None and length > soft:
            issues.append(ValidationIssue(
                validator=self.name, severity=Severity.WARNING, message="over soft length limit",
                detail=f"{length} > {soft} for {sentiment.value}"))
        return issues
