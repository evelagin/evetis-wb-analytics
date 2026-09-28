"""Telegram Bot API client.

Uses HTML parse mode; all buyer-supplied text is escaped by the caller so it can
never break markup. The bot token is passed in from Secret Manager and NEVER
appears in a log line (the base URL is not logged).
"""
from __future__ import annotations

import httpx

from app.domain.exceptions import TelegramError
from app.utils.logging import audit_event, get_logger
from app.utils.retry import retry_call

logger = get_logger(__name__)


class TelegramClient:
    def __init__(self, bot_token: str, client: httpx.Client | None = None):
        self._base = f"https://api.telegram.org/bot{bot_token}"
        self._client = client or httpx.Client(timeout=30.0)

    def _call(self, method: str, payload: dict) -> dict:
        if method.startswith("get"):          # reads (getMe, getWebhookInfo…): not a mutation
            return self._call_api(method, payload)
        audit = {"mutation_class": f"telegram:{method}", "target_system": "telegram"}
        audit_event("mutation_attempt", **audit)
        try:
            result = self._call_api(method, payload)
        except Exception as exc:
            audit_event("mutation_failure", **audit, result="failed", error_class=type(exc).__name__)
            raise
        audit_event("mutation_success", **audit, result="ok")
        return result

    def _call_api(self, method: str, payload: dict) -> dict:
        def _do() -> httpx.Response:
            return self._client.post(f"{self._base}/{method}", json=payload)

        failure = None
        try:
            resp = retry_call(_do, retries=2, retry_on=(httpx.TransportError,))
        except httpx.HTTPError as exc:
            failure = type(exc).__name__
        if failure is not None:
            # httpx errors carry the request (and its URL with the bot token). Raise OUTSIDE the
            # except block so the original is neither __cause__ nor __context__ of the new error.
            raise TelegramError(f"telegram {method}: transport {failure}")
        try:
            data = resp.json()
        except ValueError:
            raise TelegramError(f"telegram {method}: non-JSON response {resp.status_code}")
        if not data.get("ok"):
            # description may contain chat/user ids but never our token
            raise TelegramError(f"telegram {method} failed: {data.get('description')}")
        return data.get("result", {})

    def send_message(self, chat_id, text: str, reply_markup: dict | None = None) -> dict:
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return self._call("sendMessage", payload)

    def send_force_reply(self, chat_id, text: str) -> dict:
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "reply_markup": {"force_reply": True, "input_field_placeholder": "Новый текст ответа"},
        }
        return self._call("sendMessage", payload)

    def edit_message_text(self, chat_id, message_id, text: str, reply_markup: dict | None = None) -> dict:
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return self._call("editMessageText", payload)

    def answer_callback_query(self, callback_query_id: str, text: str = "") -> dict:
        payload = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        return self._call("answerCallbackQuery", payload)

    def set_webhook(self, url: str, secret_token: str, allowed_updates: list[str] | None = None) -> dict:
        payload = {
            "url": url,
            "secret_token": secret_token,
            "allowed_updates": allowed_updates or ["callback_query", "message"],
            "drop_pending_updates": False,
        }
        return self._call("setWebhook", payload)

    def close(self) -> None:
        self._client.close()
