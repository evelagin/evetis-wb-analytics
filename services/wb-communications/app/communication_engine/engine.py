"""CommunicationEngine — the composition root / facade.

Wires the classifier, context builder, prompt builder and validation pipeline via
dependency injection. `build()` is a convenience factory for the default
(Markdown-backed) configuration; callers may inject any component for testing or
to swap the knowledge backend.
"""
from __future__ import annotations

from app.communication_engine.builder.context_builder import ContextBuilder
from app.communication_engine.builder.prompt_builder import PromptBuilder
from app.communication_engine.classifier.review_classifier import ReviewClassifier
from app.communication_engine.config import EngineConfig, default_config
from app.communication_engine.knowledge.registry import (
    KnowledgeError,
    KnowledgeRegistry,
    MarkdownKnowledgeSource,
)
from app.communication_engine.models.classification import Classification, ReviewInput
from app.communication_engine.models.prompt_context import PromptBundle, PromptContext
from app.communication_engine.models.validation import ValidationResult
from app.communication_engine.validators.base import ValidationInput
from app.communication_engine.validators.duplicate_validator import DuplicateValidator
from app.communication_engine.validators.hallucination_validator import HallucinationValidator
from app.communication_engine.validators.length_validator import LengthValidator
from app.communication_engine.validators.medical_validator import MedicalValidator
from app.communication_engine.validators.numeric_validator import NumericGroundingValidator
from app.communication_engine.validators.pipeline import ValidationPipeline
from app.communication_engine.validators.tone_validator import ToneValidator


class CommunicationEngine:
    def __init__(
        self,
        config: EngineConfig,
        registry: KnowledgeRegistry,
        classifier: ReviewClassifier,
        context_builder: ContextBuilder,
        prompt_builder: PromptBuilder,
        pipeline: ValidationPipeline,
    ):
        self.config = config
        self.registry = registry
        self.classifier = classifier
        self.context_builder = context_builder
        self.prompt_builder = prompt_builder
        self.pipeline = pipeline

    # --- generation side ---
    def classify(self, review: ReviewInput) -> Classification:
        return self.classifier.classify(review)

    def build_context(self, review: ReviewInput) -> PromptContext:
        return self.context_builder.build(self.classify(review))

    def build_prompt(self, review: ReviewInput) -> PromptBundle:
        return self.prompt_builder.build(self.build_context(review), review)

    # --- validation side (post-LLM) ---
    def validate(
        self, answer: str, context: PromptContext, recent_answers: tuple[str, ...] = ()
    ) -> ValidationResult:
        return self.pipeline.run(
            ValidationInput(answer=answer, context=context, recent_answers=recent_answers)
        )

    def validate_knowledge(self) -> list[str]:
        """Knowledge-base integrity check (run at startup/CI). Empty list = ok."""
        return self.registry.validate_schema()

    # --- default composition ---
    @classmethod
    def build(cls, config: EngineConfig | None = None) -> "CommunicationEngine":
        config = config or default_config()
        registry = KnowledgeRegistry(MarkdownKnowledgeSource(config), config)
        if config.validate_knowledge_on_startup:
            problems = registry.validate_schema()
            if problems:
                raise KnowledgeError(
                    "knowledge schema invalid: " + "; ".join(problems[:10])
                    + (f" (+{len(problems) - 10} more)" if len(problems) > 10 else "")
                )
        classifier = ReviewClassifier(config, registry)
        pipeline = ValidationPipeline([
            ToneValidator(registry, config),
            MedicalValidator(registry, config),
            LengthValidator(config),
            DuplicateValidator(config),
            HallucinationValidator(registry),
            NumericGroundingValidator(),
        ])
        return cls(
            config=config,
            registry=registry,
            classifier=classifier,
            context_builder=ContextBuilder(registry, config),
            prompt_builder=PromptBuilder(config),
            pipeline=pipeline,
        )
