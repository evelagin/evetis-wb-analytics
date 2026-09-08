from __future__ import annotations

from fastapi import APIRouter

from app.config import get_settings

router = APIRouter()


@router.get("/health")
def health() -> dict:
    s = get_settings()
    # No secret access here — this endpoint must stay cheap and safe.
    return {
        "status": "ok",
        "environment": s.environment,
        "model": s.openai_model,
        "prompt_version": s.prompt_version,
    }
