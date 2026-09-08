"""Typed exceptions. None of these ever carry secret material in their message.

Exceptions are split into transient (safe to retry / ask Telegram to redeliver)
and permanent (business outcome / auth). See ``is_transient``.
"""
from __future__ import annotations


class EvetisError(Exception):
    """Base for all application errors."""


class TransientError(EvetisError):
    """Marker: a temporary failure. Safe to retry; webhook should return 5xx."""


class ConfigError(EvetisError):
    """Missing or invalid configuration / secret."""


# --- Wildberries ------------------------------------------------------------
class WBApiError(EvetisError):
    """Wildberries API returned a non-success response (generic / 4xx)."""

    def __init__(self, message: str, status_code: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        # body is truncated by the caller before logging; never contains our token
        self.body = body


class WBAuthError(WBApiError):
    """HTTP 401/403 from WB — do NOT retry, do NOT echo the token."""


class WBServerError(WBApiError, TransientError):
    """HTTP 5xx from WB — transient, safe to retry."""


class WBRateLimitError(WBServerError):
    """HTTP 429 from Wildberries — transient, honour Retry-After."""


# --- OpenAI -----------------------------------------------------------------
class OpenAIError(EvetisError):
    """OpenAI request failed permanently (e.g. 400/401/403)."""


class OpenAITransientError(OpenAIError, TransientError):
    """OpenAI transient failure (429 / timeout / connection / 5xx)."""


# --- Telegram ---------------------------------------------------------------
class TelegramError(TransientError):
    """Telegram Bot API call failed — treated as transient (redeliver)."""


# --- Firestore / GCP infra --------------------------------------------------
class FirestoreTransientError(TransientError):
    """Transient Firestore/GCP infra failure (ServiceUnavailable, DeadlineExceeded,
    InternalServerError, Aborted, transport). Webhook should return 5xx so the
    Telegram update is redelivered rather than silently lost."""


# --- domain -----------------------------------------------------------------
class InvalidTransition(EvetisError):
    """A status transition/action was attempted that the current state forbids."""


class NotFound(EvetisError):
    """Firestore document not found."""


class Unauthorized(EvetisError):
    """Webhook signature or user/chat allow-list check failed."""


def is_transient(exc: BaseException) -> bool:
    return isinstance(exc, TransientError)
