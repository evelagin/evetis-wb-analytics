"""Admin/diagnostic endpoints.

These are NOT public: Cloud Run itself requires IAM auth, and if ADMIN_TOKEN is
set they additionally require an `X-Admin-Token` header. They never publish to WB.
"""
from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException

from app.dependencies import get_deps
from app.domain.models import Review
from app.utils.logging import audit_event

router = APIRouter(prefix="/admin")


def _check_admin(token: str | None) -> None:
    """Fail-closed. In production an ADMIN_TOKEN is mandatory to use these."""
    deps = get_deps()
    configured = deps.settings.admin_token
    if deps.settings.is_production and not configured:
        # should not happen (router isn't registered in prod without a token)
        raise HTTPException(status_code=403, detail="admin disabled")
    if configured:
        import hmac

        if not token or not hmac.compare_digest(str(token), str(configured)):
            audit_event("auth_denied", route="/admin", mechanism="admin_token", principal_class="unknown",
                        result="bad_token")
            raise HTTPException(status_code=403, detail="forbidden")
        audit_event("auth_ok", route="/admin", mechanism="admin_token", principal_class="operator", result="ok")
    else:  # non-production without a token: no authentication — recorded as such, never as auth_ok
        audit_event("auth_denied", route="/admin", mechanism="none", principal_class="unknown",
                    result="unauthenticated_nonproduction")


@router.post("/test-openai")
def test_openai(x_admin_token: str | None = Header(default=None)) -> dict:
    _check_admin(x_admin_token)
    deps = get_deps()
    sample = Review(
        review_id="TEST",
        rating=5,
        text="Крем отличный, кожа мягкая",
        user_name="Тест",
        product_name="Крем для лица увлажняющий",
        supplier_article="438775437",
        nm_id="0",
    )
    gen = deps.openai.generate_answer(
        deps.prompts.system_prompt(), deps.prompts.render_user_prompt(sample)
    )
    return {"ok": True, "model": gen.model, "answer": gen.text, "usage": gen.usage}


@router.post("/test-wb")
def test_wb(x_admin_token: str | None = Header(default=None)) -> dict:
    _check_admin(x_admin_token)
    deps = get_deps()
    feedbacks = deps.wb.get_unanswered_feedbacks(take=1)
    return {"ok": True, "unanswered_sample_count": len(feedbacks)}


@router.post("/test-telegram")
def test_telegram(x_admin_token: str | None = Header(default=None)) -> dict:
    _check_admin(x_admin_token)
    deps = get_deps()
    deps.telegram.send_message(
        deps.settings.telegram_chat_id, "🔧 EVETIS WB Communications — тестовое сообщение."
    )
    return {"ok": True}
