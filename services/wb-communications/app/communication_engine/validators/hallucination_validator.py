"""Hallucination validator: catches invented / cross-product ingredients.

Using the registry's ingredient lexicon: if the answer names a KNOWN EVETIS
active that does NOT belong to the classified product, that is a cross-product
hallucination. Only known ingredients are checked, so generic wording never
triggers a false positive. If the product is unknown, the check is skipped.
"""
from __future__ import annotations

from app.communication_engine.knowledge.registry import KnowledgeRegistry
from app.communication_engine.models.validation import Severity, ValidationIssue
from app.communication_engine.validators.base import ValidationInput


class HallucinationValidator:
    name = "hallucination"

    def __init__(self, registry: KnowledgeRegistry):
        self._registry = registry

    def validate(self, data: ValidationInput) -> list[ValidationIssue]:
        product_id = data.context.classification.product_id
        if not product_id:
            return []
        actives_by_product = self._registry.known_actives()
        if not actives_by_product:
            return []
        # allowed = own actives, or for a bundle the UNION of its components'
        # actives (so a set may name any component ingredient, but not a foreign one).
        allowed = self._registry.allowed_actives(product_id)
        if not allowed:
            return []  # unknown product with no lexicon — cannot judge
        known = set().union(*actives_by_product.values())
        foreign = known - allowed
        text = data.answer.lower()
        issues: list[ValidationIssue] = []
        for ingredient in sorted(foreign):
            if ingredient and ingredient in text:
                issues.append(ValidationIssue(
                    validator=self.name, severity=Severity.ERROR,
                    message="ingredient not in this product", detail=ingredient))
        return issues
