"""Central configuration.

Non-secret settings come from environment variables (see .env.example).
Secrets are pulled from Secret Manager (or env for local dev) lazily via
`Settings.secrets`, so importing this module never requires credentials.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import cached_property

from app.services.secrets import get_secret


def _split_ids(raw: str) -> set[str]:
    return {part.strip() for part in (raw or "").split(",") if part.strip()}


# Secret resource names in Secret Manager. Names (not values) live in code.
SECRET_OPENAI = "EVETIS_OPENAI_API_KEY"
SECRET_WB = "EVETIS_WB_API_TOKEN"
SECRET_TELEGRAM_BOT = "EVETIS_TELEGRAM_BOT_TOKEN"
SECRET_TELEGRAM_WEBHOOK = "EVETIS_TELEGRAM_WEBHOOK_SECRET"
SECRET_SCHEDULER = "EVETIS_SCHEDULER_SECRET"


def _registered(value: str) -> str:
    """Secret read straight from the environment: mask it in logs like get_secret does."""
    from app.utils.logging import register_secret
    register_secret(value)
    return value


@dataclass(frozen=True)
class Secrets:
    openai_api_key: str = field(repr=False)
    wb_api_token: str = field(repr=False)
    telegram_bot_token: str = field(repr=False)
    telegram_webhook_secret: str = field(repr=False)
    scheduler_secret: str = field(repr=False)


@dataclass
class Settings:
    # --- GCP ---
    gcp_project_id: str = field(default_factory=lambda: os.environ.get("GCP_PROJECT_ID", ""))
    gcp_region: str = field(default_factory=lambda: os.environ.get("GCP_REGION", "europe-west1"))
    firestore_database: str = field(
        default_factory=lambda: os.environ.get("FIRESTORE_DATABASE", "(default)")
    )
    # Firestore location is its OWN setting — the BigQuery multi-region "EU" is
    # NOT a valid Firestore location. Default eur3 (europe multi-region).
    firestore_location: str = field(
        default_factory=lambda: os.environ.get("FIRESTORE_LOCATION", "eur3")
    )
    bigquery_location: str = field(
        default_factory=lambda: os.environ.get("BIGQUERY_LOCATION", "EU")
    )
    bigquery_dataset: str = field(
        default_factory=lambda: os.environ.get("BIGQUERY_DATASET", "evetis_communications")
    )
    bigquery_current_table: str = field(
        default_factory=lambda: os.environ.get("BIGQUERY_CURRENT_TABLE", "communications_current")
    )
    bigquery_events_table: str = field(
        default_factory=lambda: os.environ.get("BIGQUERY_EVENTS_TABLE", "communication_events")
    )
    firestore_collection: str = field(
        default_factory=lambda: os.environ.get("FIRESTORE_COLLECTION", "communications")
    )

    # --- OpenAI ---
    openai_model: str = field(default_factory=lambda: os.environ.get("OPENAI_MODEL", "gpt-4.1-mini"))
    openai_temperature: float = field(
        default_factory=lambda: float(os.environ.get("OPENAI_TEMPERATURE", "0.7"))
    )
    openai_max_output_tokens: int = field(
        default_factory=lambda: int(os.environ.get("OPENAI_MAX_OUTPUT_TOKENS", "600"))
    )
    prompt_version: str = field(default_factory=lambda: os.environ.get("PROMPT_VERSION", "reviews_v1"))
    prompts_dir: str = field(default_factory=lambda: os.environ.get("PROMPTS_DIR", "prompts"))

    # --- Communication Engine v2 (shadow) ---
    # OFF by default: when disabled, the service behaves EXACTLY as before — the
    # v2 engine is never constructed and never called.
    communication_engine_v2_enabled: bool = field(
        default_factory=lambda: os.environ.get("COMMUNICATION_ENGINE_V2_ENABLED", "false").lower()
        == "true"
    )
    # When true (default), v2 runs strictly in shadow: it never influences the
    # published answer or moderation, only records a comparison row.
    communication_engine_v2_shadow_only: bool = field(
        default_factory=lambda: os.environ.get(
            "COMMUNICATION_ENGINE_V2_SHADOW_ONLY", "true"
        ).lower()
        == "true"
    )
    # When true, v2 becomes the PRIMARY generator: the Telegram draft is produced
    # by the verified-KB engine + validators instead of reviews_v1. Requires
    # V2_ENABLED=true. Still human-approved; still NO auto-publish (WB gate
    # unchanged). Off by default — flip deliberately after reviewing shadow data.
    communication_engine_v2_primary: bool = field(
        default_factory=lambda: os.environ.get(
            "COMMUNICATION_ENGINE_V2_PRIMARY", "false"
        ).lower()
        == "true"
    )
    bigquery_shadow_table: str = field(
        default_factory=lambda: os.environ.get(
            "BIGQUERY_SHADOW_TABLE", "communication_engine_shadow"
        )
    )

    # --- Reviews & Q&A v3 (Phase 3) — SHADOW ONLY ---
    # v3 never produces customer-facing text, never publishes, never touches Telegram cards.
    # Off by default: with the flag off /poll does not build or run v3 at all.
    v3_shadow_enabled: bool = field(
        default_factory=lambda: os.environ.get("V3_SHADOW_ENABLED", "false").lower() == "true"
    )
    v3_knowledge_snapshot_id: str = field(
        default_factory=lambda: os.environ.get("V3_KNOWLEDGE_SNAPSHOT_ID", "")  # "" = snapshots/ACTIVE
    )
    v3_llm_classifier_enabled: bool = field(
        default_factory=lambda: os.environ.get("V3_LLM_CLASSIFIER_ENABLED", "true").lower() == "true"
    )
    v3_shadow_max_items_per_poll: int = field(
        default_factory=lambda: int(os.environ.get("V3_SHADOW_MAX_ITEMS_PER_POLL", "6"))
    )
    v3_shadow_budget_seconds: float = field(
        default_factory=lambda: float(os.environ.get("V3_SHADOW_BUDGET_SECONDS", "60"))
    )
    # hard stop measured from the START of /poll (Cloud Scheduler attempt deadline is 180 s)
    v3_shadow_poll_deadline_seconds: float = field(
        default_factory=lambda: float(os.environ.get("V3_SHADOW_POLL_DEADLINE_SECONDS", "150"))
    )
    bigquery_v3_decisions_table: str = field(
        default_factory=lambda: os.environ.get("BIGQUERY_V3_DECISIONS_TABLE", "communication_v3_decisions")
    )
    # Legacy compatibility setting; the mandatory publication policy gate now covers
    # every text source regardless of this value or the shadow runtime flag.
    v3_enforce_manual_edit_verifier: bool = field(
        default_factory=lambda: os.environ.get("V3_ENFORCE_MANUAL_EDIT_VERIFIER", "false").lower() == "true"
    )

    # Phase 3.1E quality shadow (R1 pilot) — a layer inside the existing v3 shadow runner
    # (V3_SHADOW_ENABLED controls the v3 baseline; these control only the 3.1E layer).
    # Explicit opt-in: no variable = no 3.1E evaluation and no 3.1E LLM call. Even when on,
    # it runs only inside a valid activation (app.v3.pilot): id, UTC window, global cap.
    v31_quality_shadow_enabled: bool = field(
        default_factory=lambda: os.environ.get("V31_QUALITY_SHADOW_ENABLED", "false").lower() == "true"
    )
    v31_shadow_activation_id: str = field(
        default_factory=lambda: os.environ.get("V31_SHADOW_ACTIVATION_ID", "")
    )
    # ISO-8601 with an explicit offset; start inclusive, end exclusive.
    v31_shadow_start_at: str = field(
        default_factory=lambda: os.environ.get("V31_SHADOW_START_AT", "")
    )
    v31_shadow_end_at: str = field(
        default_factory=lambda: os.environ.get("V31_SHADOW_END_AT", "")
    )
    # Live v2 publication gate: false = production-compatible contract (R1 requirement);
    # true = v3.1E policy for operator publication, an R2 owner decision. R1 refuses to start
    # while it is true.
    v31_enforce_live_publication_policy: bool = field(
        default_factory=lambda: os.environ.get("V31_ENFORCE_LIVE_PUBLICATION_POLICY", "false").lower() == "true"
    )
    # R2 operator assist: a v3.1E draft is prepared for every new communication and shown
    # in the Telegram card next to the v2 draft, with its own «Опубликовать 3.1E» button.
    # Publishing it still goes through the verified publisher; nothing is auto-published.
    v31_operator_draft_enabled: bool = field(
        default_factory=lambda: os.environ.get("V31_OPERATOR_DRAFT_ENABLED", "false").lower() == "true"
    )
    # R2.1: v3.1E is THE operator draft. A READY 3.1E answer becomes the active answer, «✅
    # Опубликовать» publishes it under the v3.1E policy, edits inherit that policy, and V2 is
    # only a secondary fallback. Implies 3.1E drafts. false = R2 behaviour. Never auto-publish.
    v31_primary_operator_enabled: bool = field(
        default_factory=lambda: os.environ.get("V31_PRIMARY_OPERATOR_ENABLED", "false").lower() == "true"
    )
    # R2.2: v3.1E is the ONLY normal operator answer. No V2 promotion, no V2 button: when 3.1E
    # has no answer the card asks the operator to retry or write one; every operator text is
    # published under the v3.1E policy (v31_human_safety for serious safety). Implies R2.1.
    v31_only_operator_enabled: bool = field(
        default_factory=lambda: os.environ.get("V31_ONLY_OPERATOR_ENABLED", "false").lower() == "true"
    )
    # Seconds since poll start after which new cards go out without a 3.1E draft.
    v31_operator_draft_budget_seconds: float = field(
        default_factory=lambda: float(os.environ.get("V31_OPERATOR_DRAFT_BUDGET_SECONDS", "100") or 100)
    )
    # Kept as text: a malformed value disables the pilot (app.v3.pilot), never v2 startup.
    v31_shadow_pilot_max_communications: str = field(
        default_factory=lambda: os.environ.get("V31_SHADOW_PILOT_MAX_COMMUNICATIONS", "20")
    )
    v31_operator_recovery_enabled: bool = field(
        default_factory=lambda: os.environ.get("V31_OPERATOR_RECOVERY_ENABLED", "false").lower() == "true"
    )

    # Separate activation gate: implementation never changes production config.
    v31_owner_override_enabled: bool = field(
        default_factory=lambda: os.environ.get("V31_OWNER_OVERRIDE_ENABLED", "false").lower() == "true"
    )
    # R2.4A: Telegram team access. On: ACTIVE Firestore team members of the moderation chat are
    # authorised in addition to the bootstrap env users (who stay OWNER). Off: env lists only.
    telegram_dynamic_access_enabled: bool = field(
        default_factory=lambda: os.environ.get("TELEGRAM_DYNAMIC_ACCESS_ENABLED", "false").lower() == "true"
    )
    # R2.3 owner final authority on R2.2 cards: ONLY these Telegram users may publish past a
    # content-policy BLOCK/ERROR (two explicit taps). Empty = nobody (fail closed).
    v31_owner_override_user_ids: set = field(
        default_factory=lambda: _split_ids(os.environ.get("V31_OWNER_OVERRIDE_USER_IDS", ""))
    )

    # --- Wildberries ---
    wb_api_base_url: str = field(
        default_factory=lambda: os.environ.get(
            "WB_API_BASE_URL", "https://feedbacks-api.wildberries.ru"
        )
    )
    wb_poll_batch_size: int = field(
        default_factory=lambda: int(os.environ.get("WB_POLL_BATCH_SIZE", "30"))
    )
    wb_max_pages: int = field(default_factory=lambda: int(os.environ.get("WB_MAX_PAGES", "20")))
    wb_max_items: int = field(default_factory=lambda: int(os.environ.get("WB_MAX_ITEMS", "500")))
    # Fail-closed publish gate: real WB publishing stays OFF until the live
    # Swagger contract is confirmed and this is explicitly set to "true".
    wb_publish_enabled: bool = field(
        default_factory=lambda: os.environ.get("WB_PUBLISH_ENABLED", "false").lower() == "true"
    )
    # When a crashed PUBLISHING lease is recovered, refuse to blindly re-publish
    # (WB PATCH idempotency unconfirmed) and ask for manual verification.
    wb_verify_before_publish: bool = field(
        default_factory=lambda: os.environ.get("WB_VERIFY_BEFORE_PUBLISH", "true").lower() == "true"
    )
    # Publish call is kept configurable because the exact WB answer endpoint
    # must be confirmed against the live Swagger before the first real publish.
    # Per the current WB OpenAPI spec, a FIRST answer to a feedback is
    # `POST /api/v1/feedbacks/answer` (editing a published answer is PATCH on the
    # same path). Our flow only ever posts a first answer. Confirm on live Swagger.
    wb_answer_method: str = field(
        default_factory=lambda: os.environ.get("WB_ANSWER_METHOD", "POST")
    )
    wb_answer_path: str = field(
        default_factory=lambda: os.environ.get("WB_ANSWER_PATH", "/api/v1/feedbacks/answer")
    )
    # Listing unanswered feedbacks stays on the base path.
    wb_feedbacks_path: str = field(
        default_factory=lambda: os.environ.get("WB_FEEDBACKS_PATH", "/api/v1/feedbacks")
    )

    # --- Wildberries questions (separate entity, separate gates) ---
    # Off by default: when false, questions are neither polled nor answered and the
    # reviews contour is completely unaffected.
    wb_questions_enabled: bool = field(
        default_factory=lambda: os.environ.get("WB_QUESTIONS_ENABLED", "false").lower() == "true"
    )
    # Independent fail-closed publish gate for question answers.
    wb_question_publish_enabled: bool = field(
        default_factory=lambda: os.environ.get("WB_QUESTION_PUBLISH_ENABLED", "false").lower()
        == "true"
    )
    # GET (list) and PATCH (answer) share this path per the WB spec.
    wb_questions_path: str = field(
        default_factory=lambda: os.environ.get("WB_QUESTIONS_PATH", "/api/v1/questions")
    )
    wb_question_answer_method: str = field(
        default_factory=lambda: os.environ.get("WB_QUESTION_ANSWER_METHOD", "PATCH")
    )
    # "wbRu" = answer shown publicly; "none" = replies buyer but not published on site.
    wb_question_answer_state: str = field(
        default_factory=lambda: os.environ.get("WB_QUESTION_ANSWER_STATE", "wbRu")
    )
    # Cap on NEW questions processed per poll, so the first run does not flood
    # Telegram with the historical backlog. Nothing is lost — unprocessed questions
    # stay unanswered on WB and are picked up on subsequent polls (newest first).
    wb_questions_first_run_max: int = field(
        default_factory=lambda: int(os.environ.get("WB_QUESTIONS_FIRST_RUN_MAX", "20"))
    )
    # R2.4A.1: WB marks a rating-only review (no text/pros/cons/tags) isAnswered=true without
    # any seller answer, so it never appears in the unanswered feed. When on, a bounded recent
    # window of the answered feed is read and ONLY rating-only reviews with no seller answer
    # enter the normal pipeline. Off = unanswered feed only.
    wb_rating_only_ingest_enabled: bool = field(
        default_factory=lambda: os.environ.get("WB_RATING_ONLY_INGEST_ENABLED", "false").lower() == "true"
    )
    wb_rating_only_lookback_hours: int = field(
        default_factory=lambda: int(os.environ.get("WB_RATING_ONLY_LOOKBACK_HOURS", "48"))
    )
    wb_rating_only_max_per_poll: int = field(
        default_factory=lambda: int(os.environ.get("WB_RATING_ONLY_MAX_PER_POLL", "20"))
    )
    # R2.4A.1: close cards already answered in the WB cabinet (read-only WB GET per card,
    # bounded per poll). Off = no reconciliation.
    wb_reconcile_external_answers_enabled: bool = field(
        default_factory=lambda: os.environ.get("WB_RECONCILE_EXTERNAL_ANSWERS_ENABLED", "false").lower() == "true"
    )
    wb_reconcile_max_per_poll: int = field(
        default_factory=lambda: int(os.environ.get("WB_RECONCILE_MAX_PER_POLL", "10"))
    )
    # Scheduler hours (Europe/Moscow) used only to show the next poll in /status.
    wb_poll_hours: str = field(default_factory=lambda: os.environ.get("WB_POLL_HOURS", "8-23"))
    # Single-question read used to VERIFY a publish (GET /api/v1/question?id=).
    wb_question_path: str = field(
        default_factory=lambda: os.environ.get("WB_QUESTION_PATH", "/api/v1/question")
    )
    # Bounded read-back right after a question write (inside the webhook call).
    wb_question_verify_attempts: int = field(
        default_factory=lambda: int(os.environ.get("WB_QUESTION_VERIFY_ATTEMPTS", "3"))
    )
    wb_question_verify_delay_seconds: float = field(
        default_factory=lambda: float(os.environ.get("WB_QUESTION_VERIFY_DELAY_SECONDS", "2.0"))
    )
    # How long an ACCEPTED-but-not-visible answer is re-verified on /poll (WB
    # pre-moderates answers) before it is escalated as PUBLISH_UNKNOWN.
    wb_question_verify_window_hours: float = field(
        default_factory=lambda: float(os.environ.get("WB_QUESTION_VERIFY_WINDOW_HOURS", "48"))
    )
    wb_min_interval_seconds: float = field(
        default_factory=lambda: float(os.environ.get("WB_MIN_INTERVAL_SECONDS", "0.6"))
    )

    # Feedback verification budgets; no claim about WB moderation duration.
    wb_feedback_verify_attempts: int = 3
    wb_feedback_verify_delay_seconds: float = 2.0
    wb_feedback_reconcile_limit: int = 5
    wb_feedback_reconcile_budget_seconds: float = 10.0

    # --- Telegram ---
    telegram_chat_id: str = field(
        default_factory=lambda: os.environ.get("TELEGRAM_CHAT_ID", "302044578")
    )
    telegram_allowed_user_ids: set = field(
        default_factory=lambda: _split_ids(
            os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "302044578")
        )
    )
    telegram_webhook_url: str = field(
        default_factory=lambda: os.environ.get("TELEGRAM_WEBHOOK_URL", "")
    )
    telegram_edit_timeout_seconds: int = field(
        default_factory=lambda: int(os.environ.get("TELEGRAM_EDIT_TIMEOUT_SECONDS", "900"))
    )

    # --- Concurrency / recovery ---
    # Lease for PROCESSING/PUBLISHING locks. A crashed run leaves a lease that
    # expires, after which the record can be safely reclaimed.
    lock_lease_seconds: int = field(
        default_factory=lambda: int(os.environ.get("LOCK_LEASE_SECONDS", "300"))
    )
    update_lease_seconds: int = field(
        default_factory=lambda: int(os.environ.get("UPDATE_LEASE_SECONDS", "120"))
    )
    # Lease for a single outbox event while it is being delivered to BigQuery,
    # so parallel flushes never send the same event at once.
    outbox_lease_seconds: int = field(
        default_factory=lambda: int(os.environ.get("OUTBOX_LEASE_SECONDS", "60"))
    )

    # --- Ops ---
    log_level: str = field(default_factory=lambda: os.environ.get("LOG_LEVEL", "INFO"))
    environment: str = field(default_factory=lambda: os.environ.get("ENVIRONMENT", "production"))
    admin_token_secret_enabled: bool = field(
        default_factory=lambda: os.environ.get("ADMIN_TOKEN", "") != ""
    )
    admin_token: str = field(default_factory=lambda: _registered(os.environ.get("ADMIN_TOKEN", "")), repr=False)
    # Optional: keep the legacy Google Sheet as an external journal (off by default)
    sheets_journal_enabled: bool = field(
        default_factory=lambda: os.environ.get("SHEETS_JOURNAL_ENABLED", "false").lower() == "true"
    )

    @property
    def allowed_chat_ids(self) -> set:
        return {self.telegram_chat_id} if self.telegram_chat_id else set()

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"

    @cached_property
    def secrets(self) -> Secrets:
        return Secrets(
            openai_api_key=get_secret(SECRET_OPENAI, self.gcp_project_id),
            wb_api_token=get_secret(SECRET_WB, self.gcp_project_id),
            telegram_bot_token=get_secret(SECRET_TELEGRAM_BOT, self.gcp_project_id),
            telegram_webhook_secret=get_secret(SECRET_TELEGRAM_WEBHOOK, self.gcp_project_id),
            scheduler_secret=get_secret(SECRET_SCHEDULER, self.gcp_project_id, required=False),
        )


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
