"""Medical validator: forbids medical claims (diagnoses, cure promises).

The banned formulations live in `brand/forbidden.md` (`medical_forbidden`).
"""
from __future__ import annotations

from app.communication_engine.config import EngineConfig
from app.communication_engine.constants import Namespace
from app.communication_engine.knowledge.registry import KnowledgeRegistry
from app.communication_engine.models.validation import Severity, ValidationIssue
from app.communication_engine.validators.base import ValidationInput

_MEDICAL_FORBIDDEN = "medical_forbidden"


class MedicalValidator:
    name = "medical"

    def __init__(self, registry: KnowledgeRegistry, config: EngineConfig):
        self._registry = registry
        self._config = config

    def _terms(self) -> list[str]:
        try:
            doc = self._registry.get_document(Namespace.BRAND, self._config.brand_forbidden_doc)
        except Exception:  # noqa: BLE001
            return []
        return [t.lower() for t in doc.meta_list(_MEDICAL_FORBIDDEN)]

    def validate(self, data: ValidationInput) -> list[ValidationIssue]:
        text = data.answer.lower()
        return [
            ValidationIssue(validator=self.name, severity=Severity.ERROR,
                            message="medical claim", detail=term)
            for term in self._terms()
            if term and term in text
        ]
