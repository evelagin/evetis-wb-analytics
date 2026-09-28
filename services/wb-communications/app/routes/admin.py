"""Admin/diagnostic endpoints.

These are NOT public: Cloud Run itself requires IAM auth, and if ADMIN_TOKEN is
set they additionally require an `X-Admin-Token` header. They never publish to WB.
"""
from __future__ import annotations

import time

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


# --- Reviews & Q&A v3 SHADOW (inspection / backfill only; never publishes, never messages) ---
@router.post("/v3/shadow-run")
def v3_shadow_run(max_items: int = 20, budget_seconds: float = 240,
                  x_admin_token: str | None = Header(default=None)) -> dict:
    """Process up to `max_items` real communications that have no v3 decision yet
    (newest first). Same code path as /poll; bounded by the Cloud Run request timeout."""
    _check_admin(x_admin_token)
    deps = get_deps()
    if deps.v3 is None:
        raise HTTPException(status_code=409, detail="v3 shadow disabled")
    from app.v3.shadow import run_shadow
    s = run_shadow(deps.v3, max_items=max(1, min(int(max_items), 50)),
                   deadline=time.monotonic() + max(5.0, min(float(budget_seconds), 270.0)), scan_limit=400)
    return {"ok": True, **s.to_dict()}


@router.get("/v3/decision/{communication_id}")
def v3_decision(communication_id: str, x_admin_token: str | None = Header(default=None)) -> dict:
    """Latest v3 shadow decision for one communication, rendered for an operator."""
    _check_admin(x_admin_token)
    deps = get_deps()
    from google.cloud import bigquery

    from app.v3.operator_view import render
    table = f"{deps.settings.gcp_project_id}.{deps.settings.bigquery_dataset}.{deps.settings.bigquery_v3_decisions_table}"
    job = deps.bq._lazy().query(
        f"SELECT * FROM `{table}` WHERE communication_id = @cid AND run_kind = 'shadow' "
        "ORDER BY created_at DESC LIMIT 1",
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("cid", "STRING", communication_id)]),
        location=deps.settings.bigquery_location)
    rows = [dict(r) for r in job.result()]
    if not rows:
        raise HTTPException(status_code=404, detail="no v3 decision")
    row = {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in rows[0].items()}
    return {"ok": True, "text": render(row), "decision": row}
