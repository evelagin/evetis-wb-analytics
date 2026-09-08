"""Numeric grounding validator.

Any numeric PRODUCT claim in the answer — a percentage, a pH, a volume/weight, a
duration or a frequency — must be present in the knowledge that was actually
given to the model (the assembled context). This catches invented percentages
(«ниацинамид 10%»), fake pH, wrong volumes and made-up timelines that the
cross-product ingredient check cannot see.
"""
from __future__ import annotations

import re

from app.communication_engine.models.validation import Severity, ValidationIssue
from app.communication_engine.validators.base import ValidationInput

# Patterns run over NORMALIZED text (lowercased, dashes unified, whitespace removed,
# decimal comma -> dot).
_PATTERNS = (
    re.compile(r"\d+(?:\.\d+)?%"),                                   # 8%  1.3%
    re.compile(r"ph\d+(?:\.\d+)?(?:-\d+(?:\.\d+)?)?"),               # ph8.0-8.5
    re.compile(r"\d+(?:\.\d+)?(?:мл|грамм|гр|г)\b"),                 # 30мл  75г
    re.compile(r"\d+(?:-\d+)?(?:недел|дней|дня|дн|месяц|мес|раз)"),  # 4-6недель  2-3раза
)


def _normalize(text: str) -> str:
    text = text.lower().replace("–", "-").replace("—", "-")
    text = text.replace(",", ".")
    text = re.sub(r"\s+", "", text)
    return text


class NumericGroundingValidator:
    name = "numeric_grounding"

    def validate(self, data: ValidationInput) -> list[ValidationIssue]:
        context_text = _normalize(" ".join(b.body for b in data.context.blocks))
        answer_norm = _normalize(data.answer)
        issues: list[ValidationIssue] = []
        seen: set[str] = set()
        for pattern in _PATTERNS:
            for token in pattern.findall(answer_norm):
                if token in seen:
                    continue
                seen.add(token)
                if token not in context_text:
                    issues.append(ValidationIssue(
                        validator=self.name, severity=Severity.ERROR,
                        message="unsupported numeric claim", detail=token))
        return issues
