"""OpenAI client using the official SDK Responses API.

The model is `OPENAI_MODEL` (env), defaulting to gpt-4.1-mini so the migration
reproduces the current behaviour exactly. Switching models is a one-variable
change after the A/B comparison (scripts/compare_openai_models.py).

Captured per generation for Firestore + BigQuery: model, prompt_version, usage
(token counts), latency, request id.
"""
from __future__ import annotations

import time

from app.config import Settings
from app.domain.exceptions import OpenAIError, OpenAITransientError
from app.domain.models import GenerationResult
from app.utils.logging import get_logger
from app.utils.retry import retry_call

logger = get_logger(__name__)

# Retry only transient failures. 400/401/403 are permanent and must not retry.
_TRANSIENT_STATUS = {408, 409, 429, 500, 502, 503, 504}
_TRANSIENT_NAMES = ("ratelimit", "timeout", "connection", "internalserver", "apierror")
_PERMANENT_STATUS = {400, 401, 403, 404, 422}


def _is_transient_openai(exc: BaseException) -> bool:
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if status in _PERMANENT_STATUS:
        return False
    if status in _TRANSIENT_STATUS:
        return True
    name = type(exc).__name__.lower()
    if any(tag in name for tag in ("badrequest", "authentication", "permission", "notfound")):
        return False
    return any(tag in name for tag in _TRANSIENT_NAMES)


class OpenAIClient:
    def __init__(self, settings: Settings, api_key: str, client=None):
        self._s = settings
        self._api_key = api_key
        self._client = client  # injected in tests
        self._sdk_error = Exception

    def _ensure_client(self):
        if self._client is None:
            from openai import OpenAI  # lazy import

            self._client = OpenAI(api_key=self._api_key, timeout=60.0)
        return self._client

    def _create(self, system_prompt: str, user_prompt: str):
        client = self._ensure_client()
        kwargs = dict(
            model=self._s.openai_model,
            instructions=system_prompt,
            input=user_prompt,
            max_output_tokens=self._s.openai_max_output_tokens,
        )
        # Some newer models reject `temperature`; try with it, fall back without.
        try:
            return client.responses.create(temperature=self._s.openai_temperature, **kwargs)
        except Exception as exc:  # noqa: BLE001
            msg = str(exc).lower()
            if "temperature" in msg or "unsupported" in msg:
                logger.warning("model %s rejected temperature; retrying without", self._s.openai_model)
                return client.responses.create(**kwargs)
            raise

    def generate_answer(self, system_prompt: str, user_prompt: str) -> GenerationResult:
        started = time.monotonic()
        try:
            response = retry_call(
                lambda: self._create(system_prompt, user_prompt),
                retries=2,
                retry_on=(Exception,),
                should_retry=_is_transient_openai,
            )
        except Exception as exc:  # noqa: BLE001
            # Preserve transient-ness so the webhook can decide 5xx vs 200.
            if _is_transient_openai(exc):
                raise OpenAITransientError(
                    f"OpenAI transient failure: {type(exc).__name__}"
                ) from exc
            raise OpenAIError(f"OpenAI generation failed: {type(exc).__name__}") from exc

        latency_ms = int((time.monotonic() - started) * 1000)
        text = _extract_text(response)
        if not text:
            raise OpenAIError("OpenAI returned an empty answer")

        usage = _extract_usage(response)
        request_id = getattr(response, "id", "") or ""
        logger.info(
            "openai ok model=%s tokens_in=%s tokens_out=%s latency_ms=%s",
            self._s.openai_model,
            usage.get("input_tokens"),
            usage.get("output_tokens"),
            latency_ms,
        )
        return GenerationResult(
            text=text.strip(),
            model=self._s.openai_model,
            prompt_version=self._s.prompt_version,
            usage=usage,
            latency_ms=latency_ms,
            request_id=request_id,
        )


def _extract_text(response) -> str:
    # Responses API convenience accessor
    text = getattr(response, "output_text", None)
    if text:
        return text
    # Fallback: walk output -> content -> output_text
    output = getattr(response, "output", None) or []
    parts = []
    for item in output:
        for content in getattr(item, "content", None) or []:
            if getattr(content, "type", "") == "output_text":
                parts.append(getattr(content, "text", ""))
    if parts:
        return "".join(parts)
    # dict-shaped (tests / mocks)
    if isinstance(response, dict):
        if response.get("output_text"):
            return response["output_text"]
    return ""


def _extract_usage(response) -> dict:
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    if usage is None:
        return {}
    # SDK usage object or dict
    def g(name):
        if isinstance(usage, dict):
            return usage.get(name)
        return getattr(usage, name, None)

    return {
        "input_tokens": g("input_tokens") or g("prompt_tokens"),
        "output_tokens": g("output_tokens") or g("completion_tokens"),
        "total_tokens": g("total_tokens"),
    }
