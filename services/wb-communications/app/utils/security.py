"""Webhook authenticity + Telegram allow-list checks."""
from __future__ import annotations

import hmac


def verify_webhook_secret(header_value: str | None, expected: str | None) -> bool:
    """Constant-time compare of the Telegram secret-token header.

    Telegram echoes the `secret_token` set via setWebhook in the
    `X-Telegram-Bot-Api-Secret-Token` header on every update.
    """
    if not expected:
        # Misconfiguration: refuse rather than accept everything.
        return False
    if not header_value:
        return False
    return hmac.compare_digest(str(header_value), str(expected))


def is_allowed(chat_id, user_id, allowed_chat_ids: set[str], allowed_user_ids: set[str]) -> bool:
    """Accept an update only from a known chat AND a known user.

    Both ids are compared as strings so int/str mismatches never slip through.
    """
    if allowed_chat_ids and str(chat_id) not in allowed_chat_ids:
        return False
    if allowed_user_ids and str(user_id) not in allowed_user_ids:
        return False
    return True
