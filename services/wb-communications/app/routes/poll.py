"""POST /poll — invoked by Cloud Scheduler.

The service is publicly reachable (so Telegram can reach the webhook), therefore
/poll is protected by a mandatory shared secret header `X-Scheduler-Secret`
(value in Secret Manager: EVETIS_SCHEDULER_SECRET). Fail-closed: if the secret
is not configured, the endpoint refuses all calls.
"""
from __future__ import annotations

import hmac
import uuid

from fastapi import APIRouter, Header, HTTPException

from app.dependencies import get_deps
from app.services.pipeline import run_poll
from app.utils.audit_events import record_auth
from app.utils.logging import get_logger, set_correlation_id

router = APIRouter()
logger = get_logger(__name__)


@router.post("/poll")
def poll(x_scheduler_secret: str | None = Header(default=None)) -> dict:
    deps = get_deps()
    expected = deps.settings.secrets.scheduler_secret
    if not expected:
        logger.error("SCHEDULER secret not configured — refusing /poll (fail-closed)")
        record_auth("scheduler_secret", ok=False, reason="not_configured")
        raise HTTPException(status_code=503, detail="scheduler secret not configured")
    if not x_scheduler_secret or not hmac.compare_digest(str(x_scheduler_secret), str(expected)):
        record_auth("scheduler_secret", ok=False, reason="missing" if not x_scheduler_secret else "mismatch")
        raise HTTPException(status_code=403, detail="forbidden")
    record_auth("scheduler_secret", ok=True)

    set_correlation_id(f"poll-{uuid.uuid4().hex[:8]}")
    return {"ok": True, **run_poll(deps)}
