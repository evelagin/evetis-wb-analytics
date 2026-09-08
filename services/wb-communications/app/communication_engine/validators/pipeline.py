"""ValidationPipeline: runs every validator and aggregates the result."""
from __future__ import annotations

from app.communication_engine.models.validation import ValidationResult
from app.communication_engine.validators.base import ValidationInput, Validator


class ValidationPipeline:
    def __init__(self, validators: list[Validator]):
        self._validators = list(validators)

    @property
    def validators(self) -> list[Validator]:
        return list(self._validators)

    def run(self, data: ValidationInput) -> ValidationResult:
        issues = []
        for validator in self._validators:
            issues.extend(validator.validate(data))
        return ValidationResult(issues=tuple(issues))
