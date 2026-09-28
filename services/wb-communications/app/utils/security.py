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
    Every decision is recorded as a security event (ids are never included); an allow-list that
    is not configured at all is recorded as such, so the audit does not count it as authorization.
    """
    from app.utils.audit_events import record_authz

    configured = bool(allowed_chat_ids) and bool(allowed_user_ids)
    ok = True
    if allowed_chat_ids and str(chat_id) not in allowed_chat_ids:
        ok = False
    elif allowed_user_ids and str(user_id) not in allowed_user_ids:
        ok = False
    record_authz("telegram_allowlist", ok=ok, allowlist_configured=configured)
    return ok
