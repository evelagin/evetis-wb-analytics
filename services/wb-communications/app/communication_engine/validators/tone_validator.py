"""Tone validator: rejects answers that use brand-forbidden phrases/openers.

The forbidden lists live in `brand/forbidden.md` front-matter
(`forbidden_phrases`, `forbidden_openers`) — no code change to add a new ban.
"""
from __future__ import annotations

from app.communication_engine.config import EngineConfig
from app.communication_engine.constants import MetaKey, Namespace
from app.communication_engine.knowledge.registry import KnowledgeRegistry
from app.communication_engine.models.validation import Severity, ValidationIssue
from app.communication_engine.validators.base import ValidationInput

_FORBIDDEN_PHRASES = "forbidden_phrases"
_FORBIDDEN_OPENERS = "forbidden_openers"


class ToneValidator:
    name = "tone"

    def __init__(self, registry: KnowledgeRegistry, config: EngineConfig):
        self._registry = registry
        self._config = config

    def _meta(self, key: str) -> list[str]:
        try:
            doc = self._registry.get_document(Namespace.BRAND, self._config.brand_forbidden_doc)
        except Exception:  # noqa: BLE001
            return []
        return [p.lower() for p in doc.meta_list(key)]

    def validate(self, data: ValidationInput) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        text = data.answer.lower()
        for phrase in self._meta(_FORBIDDEN_PHRASES):
            if phrase and phrase in text:
                issues.append(ValidationIssue(validator=self.name, severity=Severity.ERROR,
                                              message="forbidden phrase", detail=phrase))
        opener = text.lstrip()[:40]
        for banned in self._meta(_FORBIDDEN_OPENERS):
            if banned and opener.startswith(banned):
                issues.append(ValidationIssue(validator=self.name, severity=Severity.ERROR,
                                              message="forbidden opener", detail=banned))
        return issues
