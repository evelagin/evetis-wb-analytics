"""POST /telegram-webhook — receives Telegram updates.

Security & reliability layers, in order:
  1. `X-Telegram-Bot-Api-Secret-Token` must equal our webhook secret (else 403).
  2. `update_id` de-dup with received→processing→processed and lease recovery.
  3. chat_id / user_id allow-list (enforced in pipeline per update).

Delivery semantics:
  * business outcome (published / skipped / stale / unauthorized / disabled)
    -> mark the update processed, return 200 (no redelivery);
  * transient failure (OpenAI/WB/Telegram/Firestore infra) -> release the update
    and return 5xx so Telegram redelivers it.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Request, Response

from app.dependencies import get_deps
from app.services.pipeline import flush_events, handle_update
from app.utils.logging import audit_event, get_logger, set_correlation_id
from app.utils.security import verify_webhook_secret

router = APIRouter()
logger = get_logger(__name__)

_SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"


@router.post("/telegram-webhook")
async def telegram_webhook(request: Request) -> Response:
    set_correlation_id(f"tg-{uuid.uuid4().hex[:8]}")
    deps = get_deps()

    if not verify_webhook_secret(
        request.headers.get(_SECRET_HEADER), deps.settings.secrets.telegram_webhook_secret
    ):
        logger.warning("webhook secret check failed")
        audit_event("auth_denied", route="/telegram-webhook", mechanism="telegram_secret_token",
                    principal_class="unknown", result="bad_secret")
        return Response(status_code=403)
    audit_event("auth_ok", route="/telegram-webhook", mechanism="telegram_secret_token",
                principal_class="telegram_platform", result="ok")

    try:
        update = await request.json()
    except Exception:  # noqa: BLE001
        return Response(status_code=400)

    update_id = update.get("update_id")
    if update_id is not None:
        state = deps.repo.begin_update(update_id)
        if state == "duplicate":
            return Response(status_code=200)
        if state == "giveup":
            # poison update: exceeded max transient attempts — stop redelivering.
            logger.error("webhook update %s exceeded max attempts — giving up", update_id)
            deps.repo.complete_update(update_id)
            return Response(status_code=200)

    try:
        result = handle_update(deps, update)
        if update_id is not None:
            deps.repo.complete_update(update_id)
        flush_events(deps)  # best-effort BQ delivery of any queued events
        logger.info("webhook handled: %s", result.get("status"))
        return Response(status_code=200)
    except Exception as exc:  # noqa: BLE001
        # Default to TRANSIENT: unknown/infra errors (incl. Firestore
        # ServiceUnavailable -> FirestoreTransientError) release the update and
        # return 5xx so Telegram redelivers. The attempts counter + "giveup"
        # bound this so a genuine poison update cannot loop forever.
        if update_id is not None:
            deps.repo.release_update(update_id)
        logger.warning("webhook error, releasing for retry: %s", type(exc).__name__)
        return Response(status_code=503)
