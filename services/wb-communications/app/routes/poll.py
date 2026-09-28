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
from app.utils.logging import audit_event, get_logger, set_correlation_id

router = APIRouter()
logger = get_logger(__name__)


@router.post("/poll")
def poll(x_scheduler_secret: str | None = Header(default=None)) -> dict:
    deps = get_deps()
    expected = deps.settings.secrets.scheduler_secret
    if not expected:
        logger.error("SCHEDULER secret not configured — refusing /poll (fail-closed)")
        audit_event("auth_denied", route="/poll", mechanism="scheduler_shared_secret",
                    principal_class="unknown", result="not_configured")
        raise HTTPException(status_code=503, detail="scheduler secret not configured")
    if not x_scheduler_secret or not hmac.compare_digest(str(x_scheduler_secret), str(expected)):
        audit_event("auth_denied", route="/poll", mechanism="scheduler_shared_secret",
                    principal_class="unknown", result="bad_secret")
        raise HTTPException(status_code=403, detail="forbidden")
    audit_event("auth_ok", route="/poll", mechanism="scheduler_shared_secret",
                principal_class="cloud_scheduler", result="ok")

    set_correlation_id(f"poll-{uuid.uuid4().hex[:8]}")
    return {"ok": True, **run_poll(deps)}
