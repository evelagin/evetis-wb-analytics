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


@dataclass(frozen=True)
class Secrets:
    openai_api_key: str
    wb_api_token: str
    telegram_bot_token: str
    telegram_webhook_secret: str
    scheduler_secret: str


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
    wb_min_interval_seconds: float = field(
        default_factory=lambda: float(os.environ.get("WB_MIN_INTERVAL_SECONDS", "0.6"))
    )

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
    admin_token: str = field(default_factory=lambda: os.environ.get("ADMIN_TOKEN", ""))
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
