"""Validation result models."""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class Severity(str, Enum):
    WARNING = "warning"
    ERROR = "error"


class ValidationIssue(BaseModel):
    model_config = {"frozen": True}

    validator: str
    severity: Severity
    message: str
    detail: str = ""


class ValidationResult(BaseModel):
    """Aggregate of all validator issues. `ok` is False if any ERROR is present."""

    model_config = {"frozen": True}

    issues: tuple[ValidationIssue, ...] = Field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not any(i.severity is Severity.ERROR for i in self.issues)

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity is Severity.WARNING]
