"""Communication Engine v2 shadow evaluation.

Runs the v2 engine ALONGSIDE the production reviews_v1 pipeline for observation
only. It builds a v2 prompt from the same ``Review``, generates a v2 answer,
runs the v2 validators, and produces a single comparison row for the
``communication_engine_shadow`` BigQuery table.

Hard guarantees:
* It NEVER publishes to Wildberries and NEVER sends a Telegram message.
* It ALWAYS returns a row — internal failures (unresolved product, unsupported
  marketplace, generation error, validation failure) are recorded in the row with
  ``v2_usable=False`` rather than raised, so a v2 problem cannot break reviews_v1.

The one hard-failure surface (an unexpected exception before a row can be built)
is caught by the pipeline's isolated ``_run_shadow`` wrapper.
"""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from typing import Any

from app.domain.models import Review
from app.utils.logging import get_logger

logger = get_logger(__name__)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def compute_shadow_id(
    review_id: str, old_prompt_version: str, v2_prompt_version: str, run_version: str
) -> str:
    """Deterministic unique id for one shadow comparison row.

    Built from review_id + BOTH prompt versions + a run/generation version, so:
    * the same review re-shadowed on a DIFFERENT prompt version yields a DIFFERENT
      id (both rows persist — no BigQuery dedup);
    * an exact retry of the same logical shadow run yields the SAME id (idempotent
      insert, so a transient retry does not double-write).
    """
    key = "|".join(
        str(part or "")
        for part in (review_id, old_prompt_version, v2_prompt_version, run_version)
    )
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


class ShadowEngineService:
    """Evaluates Communication Engine v2 for one review and builds a shadow row."""

    def __init__(
        self, engine_service: Any, openai: Any, settings: Any, clock=time.monotonic, now=_utcnow_iso
    ):
        # engine_service: EnginePromptService (build_prompt / build_context / validate)
        self._engine = engine_service
        self._openai = openai
        self._settings = settings
        self._clock = clock
        self._now = now

    def evaluate(
        self, review: Review, *, old_answer: str, old_prompt_version: str, run_version=None
    ) -> dict:
        """Run v2 in shadow and return a fully-populated row. Never raises for
        expected failures; records them in the row instead. ``shadow_id`` is set
        on EVERY return path (via finally), so even an early-failure row is
        uniquely keyed for BigQuery."""
        row = self._base_row(review, old_answer, old_prompt_version)
        try:
            try:
                bundle = self._engine.build_prompt(review)
            except Exception as exc:  # noqa: BLE001 — record, do not raise
                row["error_code"] = type(exc).__name__
                row["error_message"] = str(exc)[:500]
                logger.warning("shadow v2 classify/prompt failed: %s", type(exc).__name__)
                return row

            classification = bundle.classification
            self._apply_classification(row, classification, bundle.prompt_version)

            generated = self._generate(row, bundle)
            if generated is None:
                row["v2_usable"] = False
                return row

            self._validate(row, review, classification, generated)
            return row
        finally:
            # Computed last, once v2_prompt_version is known. Returning `row` (a
            # reference) then mutating it here means the caller sees the shadow_id.
            row["shadow_id"] = compute_shadow_id(
                row["review_id"], row["old_prompt_version"],
                row.get("v2_prompt_version"), self._run_version_str(run_version),
            )

    # --- steps ---------------------------------------------------------------
    def _generate(self, row: dict, bundle) -> Any:
        started = self._clock()
        try:
            gen = self._openai.generate_answer(bundle.system, bundle.user)
        except Exception as exc:  # noqa: BLE001 — a v2 generation failure is recorded
            row["v2_latency_ms"] = int((self._clock() - started) * 1000)
            row["error_code"] = type(exc).__name__
            row["error_message"] = str(exc)[:500]
            logger.warning("shadow v2 generation failed: %s", type(exc).__name__)
            return None
        row["v2_latency_ms"] = int((self._clock() - started) * 1000)
        row["v2_answer"] = gen.text
        row["v2_model"] = gen.model
        usage = gen.usage or {}
        row["v2_token_input"] = usage.get("input_tokens")
        row["v2_token_output"] = usage.get("output_tokens")
        return gen

    def _validate(self, row: dict, review: Review, classification, gen) -> None:
        try:
            context = self._engine.build_context(review)
            result = self._engine.validate(gen.text, context)
        except Exception as exc:  # noqa: BLE001 — validation failure is recorded
            row["validation_passed"] = False
            row["error_code"] = type(exc).__name__
            row["error_message"] = str(exc)[:500]
            row["v2_usable"] = False
            logger.warning("shadow v2 validation failed: %s", type(exc).__name__)
            return
        row["validation_passed"] = result.ok
        row["validation_issues_json"] = json.dumps(
            [
                {"validator": i.validator, "severity": i.severity.value,
                 "message": i.message, "detail": i.detail}
                for i in result.issues
            ],
            ensure_ascii=False,
        )
        # An answer is only "usable" if the product resolved, the marketplace is a
        # supported WB channel (no manual moderation) AND every validator passed.
        row["v2_usable"] = bool(result.ok and not classification.needs_manual_moderation)

    # --- row helpers ---------------------------------------------------------
    @staticmethod
    def _run_version_str(run_version) -> str:
        return "" if run_version is None else str(run_version)

    def _base_row(self, review: Review, old_answer: str, old_prompt_version: str) -> dict:
        return {
            "shadow_id": None,  # set at the end of evaluate(), once versions are known
            "review_id": str(getattr(review, "review_id", "") or ""),
            "channel": (review.platform or "wb").lower(),
            "shadow_at": self._now(),
            "old_prompt_version": old_prompt_version or "",
            "old_answer": old_answer or "",
            "v2_prompt_version": None,
            "v2_answer": None,
            "v2_model": None,
            "classification_json": None,
            "product_id": None,
            "product_resolution_method": None,
            "product_resolution_status": None,
            "marketplace": None,
            "marketplace_resolution_status": None,
            "needs_manual_moderation": None,
            "validation_passed": None,
            "validation_issues_json": None,
            "v2_usable": False,
            "v2_latency_ms": None,
            "v2_token_input": None,
            "v2_token_output": None,
            "error_code": None,
            "error_message": None,
        }

    def _apply_classification(self, row: dict, classification, v2_prompt_version: str) -> None:
        row["v2_prompt_version"] = v2_prompt_version
        row["classification_json"] = json.dumps(
            classification.model_dump(mode="json"), ensure_ascii=False
        )
        row["product_id"] = classification.product_id
        row["product_resolution_method"] = classification.resolution_method or None
        row["product_resolution_status"] = classification.product_resolution_status.value
        row["marketplace"] = (
            classification.marketplace.value if classification.marketplace is not None else None
        )
        row["marketplace_resolution_status"] = classification.marketplace_resolution_status.value
        row["needs_manual_moderation"] = classification.needs_manual_moderation
