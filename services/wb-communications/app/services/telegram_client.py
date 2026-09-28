"""Telegram Bot API client.

Uses HTML parse mode; all buyer-supplied text is escaped by the caller so it can
never break markup. The bot token is passed in from Secret Manager and NEVER
appears in a log line (the base URL is not logged).
"""
from __future__ import annotations

import httpx

from app.domain.exceptions import TelegramError
from app.utils.logging import get_logger
from app.utils.retry import retry_call

logger = get_logger(__name__)

# D-19b: every Bot API method this client calls changes state in Telegram.
_MUTATION_CLASS = {"sendMessage": "tg_send_message", "editMessageText": "tg_edit_message",
                   "answerCallbackQuery": "tg_answer_callback", "setWebhook": "tg_set_webhook"}
# Transport failures that provably happened before the request was sent.
_NOT_SENT = frozenset({"ConnectError", "ConnectTimeout"})


class TelegramClient:
    def __init__(self, bot_token: str, client: httpx.Client | None = None):
        self._base = f"https://api.telegram.org/bot{bot_token}"
        self._client = client or httpx.Client(timeout=30.0)

    def _call(self, method: str, payload: dict) -> dict:
        """Audited mutation (D-19b): mutation_attempt before the call, mutation_success|failure
        after it. The target is a hash of the chat/callback id — never the id, text or token."""
        from app.utils import audit_events

        if "chat_id" in payload:
            ref = audit_events.chat_ref(payload["chat_id"])
        elif "callback_query_id" in payload:
            ref = audit_events.safe_ref("tg_callback", payload["callback_query_id"])
        else:
            ref = "tg:bot"
        m = audit_events.start_mutation(_MUTATION_CLASS.get(method, "tg_other"), "telegram", ref)
        try:
            result = self._call_once(method, payload)
        except TelegramError as exc:
            audit_events.finish_mutation(m, getattr(exc, "audit_outcome", "outcome_unknown"),
                                         getattr(exc, "audit_error_class", type(exc).__name__))
            raise
        except BaseException as exc:
            audit_events.finish_mutation(m, "outcome_unknown", type(exc).__name__)
            raise
        audit_events.finish_mutation(m, "success")
        return result

    def _call_once(self, method: str, payload: dict) -> dict:
        maybe_sent = {"any": False}      # did ANY retried attempt possibly reach Telegram?

        def _do() -> httpx.Response:
            try:
                return self._client.post(f"{self._base}/{method}", json=payload)
            except httpx.TransportError as exc:
                if type(exc).__name__ not in _NOT_SENT:
                    maybe_sent["any"] = True
                raise

        failure = None
        try:
            resp = retry_call(_do, retries=2, retry_on=(httpx.TransportError,))
        except httpx.HTTPError as exc:
            failure = type(exc).__name__
        if failure is not None:
            # httpx errors carry the request (and its URL with the bot token). Raise OUTSIDE the
            # except block so the original is neither __cause__ nor __context__ of the new error.
            err = TelegramError(f"telegram {method}: transport {failure}")
            # "error" (provably not sent) only if NO attempt could have been sent: ReadTimeout → ConnectError
            # is outcome_unknown, because the first attempt may have been delivered.
            not_sent = failure in _NOT_SENT and not maybe_sent["any"]
            err.audit_outcome = "error" if not_sent else "outcome_unknown"
            err.audit_error_class = failure
            raise err
        try:
            data = resp.json()
        except ValueError:
            err = TelegramError(f"telegram {method}: non-JSON response {resp.status_code}")
            err.audit_outcome, err.audit_error_class = "outcome_unknown", "NonJsonResponse"
            raise err
        if not data.get("ok"):
            # description may contain chat/user ids but never our token
            err = TelegramError(f"telegram {method} failed: {data.get('description')}")
            err.audit_outcome, err.audit_error_class = "rejected", "TelegramNotOk"
            raise err
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
