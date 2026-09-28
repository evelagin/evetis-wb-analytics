"""FastAPI application entrypoint (Cloud Run).

Run locally:  uvicorn app.main:app --reload --port 8080
"""
from __future__ import annotations

from fastapi import FastAPI

from app.config import get_settings
from app.routes import admin, health, poll, telegram_webhook
from app.utils.logging import audit_event, configure_logging, get_logger, trace_middleware

settings = get_settings()
configure_logging(settings.log_level)
logger = get_logger(__name__)

app = FastAPI(title="EVETIS WB Communications", version="1.1.0")
app.middleware("http")(trace_middleware)
audit_event("instrumentation_ready", result="ok")  # per instance: this revision emits audit events
app.include_router(health.router)
app.include_router(poll.router)
app.include_router(telegram_webhook.router)

# Admin endpoints: NOT registered in production unless an ADMIN_TOKEN is set
# (and even then they require the X-Admin-Token header). Fail-closed.
if (not settings.is_production) or settings.admin_token:
    app.include_router(admin.router)
    logger.info("admin routes registered")
else:
    logger.info("admin routes disabled (production without ADMIN_TOKEN)")


@app.on_event("startup")
def _startup() -> None:
    logger.info(
        "EVETIS WB Communications starting env=%s model=%s prompt=%s",
        settings.environment,
        settings.openai_model,
        settings.prompt_version,
    )
