"""Orchestration for /poll and the Telegram webhook.

All business logic lives here as plain functions over a ``Deps`` bundle, so it
is fully unit-testable with in-memory / fake dependencies (no GCP, no network).
Routes are thin adapters that build ``Deps`` and call these functions.

Transient failures (OpenAI/WB/Telegram/Firestore infra) propagate as exceptions
so the webhook route can return 5xx and let Telegram redeliver. Business
outcomes (published, skipped, stale, unauthorized, publish disabled) return a
dict and are final (HTTP 200).
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.domain.exceptions import (
    FirestoreTransientError,
    InvalidTransition,
    NotFound,
    WBApiError,
    WBPublishOutcomeUnknown,
    is_transient,
)
from app.communication_engine.constants import CommunicationType
from app.domain.models import Question, Review
from app.domain.statuses import EventType, Status
from app.utils.logging import audit_event, get_logger, log_event, redact
from app.utils.security import is_allowed
from app.utils.text import (
    TELEGRAM_MSG_SOFT_LIMIT,
    WB_ANSWER_MAX_LEN,
    clean_answer,
    escape_html,
    is_within_wb_limit,
    truncate,
)

logger = get_logger(__name__)

ACTIONS = {"pub", "edit", "regen", "skip", "show"}
# callback token -> state-machine action name
_ACTION_MAP = {"pub": "publish", "edit": "edit", "regen": "regenerate", "skip": "skip", "show": "show"}
_REVIEW_TEXT_CARD_LIMIT = 700

# Human-readable operator hints per WB publish status code. Keeps diagnosis fast:
# the operator sees WHY it failed, not just a number.
_WB_PUBLISH_HINTS = {
    400: "Неверный формат запроса к WB.",
    401: "API-токен WB недействителен или истёк.",
    403: "Недостаточно прав API-токена (нужен доступ на запись «Вопросы и отзывы»).",
    404: "Отзыв не найден на стороне WB (возможно, устарел или удалён).",
    409: "Конфликт: отзыв уже обрабатывается на стороне WB.",
    422: "Отзыв уже имеет ответ, либо текст не прошёл проверку WB.",
    429: "Превышен лимит запросов WB. Повторите через минуту.",
}


# Question-specific overrides where the reason differs from reviews.
_WB_QUESTION_HINTS = {
    404: "Вопрос не найден или уже обработан.",
    422: "Вопрос уже имеет ответ, либо текст не прошёл проверку WB.",
    409: "Конфликт: вопрос уже обрабатывается на стороне WB.",
}


def _wb_publish_error_message(exc, *, is_question: bool = False) -> str:
    """Build a detailed, operator-friendly Telegram message for a WB publish
    failure: the HTTP code plus a plain-language reason."""
    code = getattr(exc, "status_code", None)
    hint = (_WB_QUESTION_HINTS.get(code) if is_question else None) or _WB_PUBLISH_HINTS.get(code)
    if hint is None and isinstance(code, int) and code >= 500:
        hint = "Временная ошибка на стороне WB. Можно повторить позже."
    header = f"❌ Wildberries вернул {code}" if code else "❌ Ошибка публикации в WB"
    return f"{header}\n\n{hint}" if hint else f"{header}\n\nМожно повторить."


@dataclass
class Deps:
    settings: Any
    repo: Any
    wb: Any
    openai: Any
    telegram: Any
    bq: Any
    prompts: Any
    # Communication Engine v2. `engine` is the PRIMARY generator (set only when
    # V2_PRIMARY=true); `shadow_engine`/`shadow_repo` are the shadow comparison
    # (set only in shadow mode). All None by default -> unchanged reviews_v1.
    engine: Any = None
    shadow_engine: Any = None
    shadow_repo: Any = None
    # Reviews & Q&A v3 SHADOW runtime (app.v3.shadow.V3Runtime) — None unless
    # V3_SHADOW_ENABLED. Observation only: no WB, no Telegram, no v2 doc writes.
    v3: Any = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Telegram card rendering / callback parsing
# --------------------------------------------------------------------------- #
def parse_callback(data: str) -> tuple[Optional[str], Optional[str]]:
    if not data:
        return None, None
    if ":" in data:
        action, _, doc_id = data.partition(":")
    else:  # legacy n8n format action_reviewid
        action, _, doc_id = data.partition("_")
        action = {"send": "pub"}.get(action, action)
    action = action.strip()
    doc_id = doc_id.strip()
    if action not in ACTIONS or not doc_id:
        return None, None
    return action, doc_id


def build_keyboard(doc_id: str, *, show_full: bool = False, retry: bool = False) -> dict:
    rows = [
        [
            {"text": "✅ Опубликовать", "callback_data": f"pub:{doc_id}"},
            {"text": "✏️ Изменить", "callback_data": f"edit:{doc_id}"},
        ],
        [
            {"text": "🔄 Перегенерировать", "callback_data": f"regen:{doc_id}"},
            {"text": "⏭ Пропустить", "callback_data": f"skip:{doc_id}"},
        ],
    ]
    if show_full:
        rows.append([{"text": "📋 Показать полностью", "callback_data": f"show:{doc_id}"}])
    return {"inline_keyboard": rows}


def build_card(doc: dict, doc_id: str, *, full: bool = False) -> tuple[str, bool]:
    def e(value) -> str:
        return escape_html("" if value is None else str(value))

    review_text = doc.get("text") or "—"
    truncated = False
    if not full and len(review_text) > _REVIEW_TEXT_CARD_LIMIT:
        review_text = truncate(review_text, _REVIEW_TEXT_CARD_LIMIT)
        truncated = True

    answer = doc.get("final_answer") or doc.get("ai_answer") or "—"
    lines = [
        "<b>EVETIS / WB — новый отзыв</b>", "",
        f"<b>Товар:</b> {e(doc.get('product_name') or '—')}",
        f"<b>Артикул продавца:</b> {e(doc.get('supplier_article') or '—')}",
        f"<b>nmId:</b> {e(doc.get('nm_id') or '—')}",
        f"<b>Оценка:</b> {e(doc.get('rating') or '—')} / 5",
        f"<b>Покупатель:</b> {e(doc.get('buyer_name') or 'Не указано')}",
        f"<b>Дата:</b> {e(doc.get('source_created_at') or '—')}",
        f"<b>Достоинства:</b> {e(doc.get('pros') or '—')}",
        f"<b>Недостатки:</b> {e(doc.get('cons') or '—')}",
        f"<b>Комментарий:</b> {e(review_text)}", "",
        f"✍️ <b>Проект ответа:</b>\n{e(answer)}", "",
        f"<i>модель: {e(doc.get('openai_model') or '—')} · id: {e(doc_id)}</i>",
    ]
    return truncate("\n".join(lines), TELEGRAM_MSG_SOFT_LIMIT), truncated


def build_question_card(doc: dict, doc_id: str, *, full: bool = False) -> tuple[str, bool]:
    """Telegram card for a WB buyer question — no rating/pros/cons; the buyer's
    question and the draft answer."""
    def e(value) -> str:
        return escape_html("" if value is None else str(value))

    q_text = doc.get("text") or "—"
    truncated = False
    if not full and len(q_text) > _REVIEW_TEXT_CARD_LIMIT:
        q_text = truncate(q_text, _REVIEW_TEXT_CARD_LIMIT)
        truncated = True

    answer = doc.get("final_answer") or doc.get("ai_answer") or "—"
    lines = [
        "<b>EVETIS / WB — новый вопрос</b>", "",
        f"<b>Товар:</b> {e(doc.get('product_name') or '—')}",
        f"<b>Артикул продавца:</b> {e(doc.get('supplier_article') or '—')}",
        f"<b>nmId:</b> {e(doc.get('nm_id') or '—')}",
        f"<b>Дата:</b> {e(doc.get('source_created_at') or '—')}", "",
        f"❓ <b>Вопрос покупателя:</b>\n{e(q_text)}", "",
        f"✍️ <b>Проект ответа:</b>\n{e(answer)}", "",
        f"<i>модель: {e(doc.get('openai_model') or '—')} · id: {e(doc_id)}</i>",
    ]
    return truncate("\n".join(lines), TELEGRAM_MSG_SOFT_LIMIT), truncated


def _card_builder_for(doc: dict | None):
    """Pick the right card renderer for a doc based on its entity_type."""
    return build_question_card if (doc or {}).get("entity_type") == "question" else build_card


def _review_from_doc(doc: dict) -> Review:
    return Review(
        platform=(doc.get("channel") or "wb").upper(),
        review_id=doc.get("source_id", ""),
        rating=doc.get("rating"),
        created_date=doc.get("source_created_at", "") or "",
        text=doc.get("text", "") or "",
        pros=doc.get("pros", "") or "",
        cons=doc.get("cons", "") or "",
        user_name=doc.get("buyer_name", "") or "",
        product_name=doc.get("product_name", "") or "",
        supplier_article=doc.get("supplier_article", "") or "",
        nm_id=doc.get("nm_id", "") or "",
        imt_id=doc.get("imt_id", "") or "",
        brand_name=doc.get("brand_name", "") or "",
    )


def _question_from_doc(doc: dict) -> Question:
    return Question(
        platform=(doc.get("channel") or "wb").upper(),
        question_id=doc.get("source_id", ""),
        created_date=doc.get("source_created_at", "") or "",
        text=doc.get("text", "") or "",
        product_name=doc.get("product_name", "") or "",
        supplier_article=doc.get("supplier_article", "") or "",
        nm_id=doc.get("nm_id", "") or "",
        imt_id=doc.get("imt_id", "") or "",
        brand_name=doc.get("brand_name", "") or "",
        state=doc.get("wb_state", "") or "",
    )


def _subject_from_doc(doc: dict):
    """Reconstruct the engine subject + its communication type from a stored doc."""
    if (doc or {}).get("entity_type") == "question":
        return _question_from_doc(doc), CommunicationType.QUESTION
    return _review_from_doc(doc), CommunicationType.REVIEW


# --------------------------------------------------------------------------- #
# BigQuery events (deterministic id + Firestore-guarded exactly-once) & current
# --------------------------------------------------------------------------- #
def _emit_event(deps: Deps, doc: dict, doc_id: str, event_type: EventType,
                status_before: str = "", status_after: str = "", *, best_effort: bool = True,
                **extra) -> None:
    """Enqueue an event into the outbox.

    best_effort=True (default, used by /poll): a transient enqueue is logged and
    swallowed so it never breaks the flow or reverts an already-committed state.
    best_effort=False (webhook outcomes): a transient enqueue is re-raised so the
    webhook returns 5xx and Telegram redelivers instead of silently dropping it.
    """
    answer_version = extra.get("answer_version") or doc.get("generation_number") or ""
    attempt = extra.get("attempt", "")
    # Deterministic across retries of the SAME logical event -> BQ dedup key.
    event_id = hashlib.sha1(
        "|".join(str(x) for x in (
            doc.get("channel", "wb"), doc.get("entity_type", "review"),
            doc.get("source_id", ""), event_type.value,
            answer_version, status_after, attempt,
        )).encode()
    ).hexdigest()
    usage = doc.get("openai_usage") or {}
    payload = {
        "event_id": event_id, "event_at": _now().isoformat(),
        "channel": doc.get("channel", "wb"), "entity_type": doc.get("entity_type", "review"),
        "source_id": doc.get("source_id", ""), "event_type": event_type.value,
        "status_before": status_before, "status_after": status_after,
        "telegram_user_id": str(extra.get("telegram_user_id") or ""),
        "openai_model": doc.get("openai_model", ""), "prompt_version": doc.get("prompt_version", ""),
        "answer_version": answer_version or None, "latency_ms": doc.get("openai_latency_ms"),
        "token_input": usage.get("input_tokens"), "token_output": usage.get("output_tokens"),
        "error_code": (str(extra["error_code"]) if extra.get("error_code") is not None else None),
        "error_message": redact(extra.get("error_message") or "")[:500] or None,
        "payload_json": (json.dumps(extra["payload"], ensure_ascii=False, default=str)
                         if extra.get("payload") else None),
    }
    # Persist to the Firestore outbox; delivery to BigQuery happens in flush_events.
    try:
        deps.repo.enqueue_event(payload)
    except FirestoreTransientError:
        if best_effort:
            log_event(logger, "warning", "event enqueue transient (best-effort skip)",
                      event_type=event_type.value, doc_id=doc_id)
            return
        raise


def flush_events(deps: Deps, limit: int = 100) -> dict:
    """Deliver pending outbox events to BigQuery. At-least-once, deduped by
    event_id. Each event is CLAIMED (pending->sending, leased) before the BQ
    write so parallel flushes never send the same event at once; a failed write
    returns it to pending for the next flush."""
    delivered = failed = skipped = 0
    for rec in deps.repo.list_pending_events(limit=limit):
        if not deps.repo.claim_outbox_event(rec["event_id"]):
            skipped += 1
            continue
        try:
            ok = deps.bq.insert_event(rec["payload"])
        except Exception:  # noqa: BLE001
            ok = False
        if ok:
            deps.repo.mark_event_delivered(rec["event_id"])
            delivered += 1
        else:
            deps.repo.mark_event_failed(rec["event_id"])
            failed += 1
    return {"delivered": delivered, "failed": failed, "skipped": skipped}


def _current_row(doc: dict) -> dict:
    def as_int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    return {
        "channel": doc.get("channel", "wb"), "entity_type": doc.get("entity_type", "review"),
        "source_id": doc.get("source_id", ""), "status": doc.get("status", ""),
        "rating": as_int(doc.get("rating")), "product_name": doc.get("product_name"),
        "supplier_article": doc.get("supplier_article"), "nm_id": doc.get("nm_id"),
        "brand_name": doc.get("brand_name"), "buyer_name": doc.get("buyer_name"),
        "has_photo": bool(doc.get("has_photo")), "has_video": bool(doc.get("has_video")),
        "ai_answer": doc.get("ai_answer"), "final_answer": doc.get("final_answer"),
        "openai_model": doc.get("openai_model"), "prompt_version": doc.get("prompt_version"),
        "generation_number": as_int(doc.get("generation_number")),
        "first_seen_at": doc.get("first_seen_at"), "published_at": doc.get("published_at"),
        "updated_at": doc.get("updated_at"),
    }


def _sync_current(deps: Deps, doc_id: str, fallback: dict | None = None) -> dict:
    doc = deps.repo.get(doc_id) or fallback or {}
    deps.bq.upsert_current(_current_row(doc))
    return doc


# --------------------------------------------------------------------------- #
# Answer generation — reviews_v1 or Communication Engine v2 (primary)
# --------------------------------------------------------------------------- #
def _generate_reviews_v1(deps: Deps, review: Review):
    """The original production generator (static reviews_v1 prompt)."""
    system = deps.prompts.system_prompt()
    user = deps.prompts.render_user_prompt(review)
    return deps.openai.generate_answer(system, user)


def _v2_flags(deps: Deps, subject, communication_type, bundle, gen) -> list[str]:
    """Human-facing warnings from the v2 classification + validators. Empty when
    the product resolved and every validator passed."""
    flags: list[str] = []
    classification = bundle.classification
    if classification.needs_manual_moderation:
        flags.append("товар/площадка не распознаны — проверьте вручную")
    try:
        context = deps.engine.build_context(subject, communication_type)
        result = deps.engine.validate(gen.text, context)
        if not result.ok:
            details = "; ".join(sorted({(i.detail or i.message) for i in result.errors}))
            flags.append(f"валидатор: {details}")
    except Exception as exc:  # noqa: BLE001 — a validator fault must not block the draft
        log_event(logger, "warning", "v2 validation error (non-blocking)",
                  error=type(exc).__name__)
    return flags


def _generate_answer(deps: Deps, subject, communication_type=CommunicationType.REVIEW):
    """Return ``(gen, v2_meta)``. ``v2_meta`` is None for reviews_v1, else a dict
    with the flags to surface on the card.

    Reviews use v2 only when it is primary (else reviews_v1 — the explicit
    config rollback path); questions ALWAYS use v2 (reviews_v1 has no question
    template). A v2 prompt-build failure is an item error retried on the next
    poll — there is NO silent fallback to reviews_v1 (WP11: its prompt carries
    unsourced product facts and the «обострение — нормальная реакция» advice).
    OpenAI errors propagate as before.
    """
    is_question = communication_type == CommunicationType.QUESTION
    use_v2 = deps.engine is not None and (
        is_question or getattr(deps.settings, "communication_engine_v2_primary", False)
    )
    if not use_v2:
        if is_question:
            raise RuntimeError("questions require the v2 engine (COMMUNICATION_ENGINE_V2_ENABLED)")
        return _generate_reviews_v1(deps, subject), None

    bundle = deps.engine.build_prompt(subject, communication_type)  # failure -> item error
    gen = deps.openai.generate_answer(bundle.system, bundle.user)  # OpenAI errors propagate
    gen = replace(gen, prompt_version=bundle.prompt_version)  # record the ENGINE version
    return gen, {"flags": _v2_flags(deps, subject, communication_type, bundle, gen)}


def _manual_text_flags(deps: Deps, doc: dict, text: str) -> list[str]:
    """Run the SAME validators the AI draft gets over operator-typed text (WP11).
    Advisory for now (surfaced on the card), exactly like the AI draft flags."""
    if deps.engine is None:
        return []
    try:
        subject, communication_type = _subject_from_doc(doc)
        context = deps.engine.build_context(subject, communication_type)
        result = deps.engine.validate(text, context)
        if not result.ok:
            details = "; ".join(sorted({(i.detail or i.message) for i in result.errors}))
            return [f"валидатор (ручной текст): {details}"]
    except Exception as exc:  # noqa: BLE001 — a validator fault must not block the edit
        log_event(logger, "warning", "manual-text validation error (non-blocking)",
                  error=type(exc).__name__)
    return []


def _card_with_flags(card_text: str, v2_meta: Optional[dict]) -> str:
    """Prepend a visible ⚠️ warning line when v2 flagged the draft."""
    if not v2_meta or not v2_meta.get("flags"):
        return card_text
    warn = "⚠️ <b>Проверка v2:</b> " + escape_html("; ".join(v2_meta["flags"]))
    return truncate(f"{warn}\n\n{card_text}", TELEGRAM_MSG_SOFT_LIMIT)


# --------------------------------------------------------------------------- #
# Communication Engine v2 — shadow run (observation only)
# --------------------------------------------------------------------------- #
def _run_shadow(deps: Deps, review: Review, doc: dict, doc_id: str) -> None:
    """Run Communication Engine v2 in shadow alongside reviews_v1.

    Contract:
    * No-op unless COMMUNICATION_ENGINE_V2_ENABLED=true and the shadow components
      are wired — so with the flag off the pipeline behaves exactly as before.
    * Fully isolated: EVERY failure is logged and swallowed here, so a v2 problem
      can never break the reviews_v1 batch, the Telegram card, or WB publishing.
    * v2 NEVER publishes to WB and NEVER sends a second Telegram message; it only
      records a comparison row to the separate shadow table.
    """
    settings = deps.settings
    if not getattr(settings, "communication_engine_v2_enabled", False):
        return
    if deps.shadow_engine is None or deps.shadow_repo is None:
        return
    try:
        row = deps.shadow_engine.evaluate(
            review,
            old_answer=doc.get("final_answer") or doc.get("ai_answer") or "",
            old_prompt_version=doc.get("prompt_version") or settings.prompt_version,
            run_version=doc.get("generation_number"),
        )
        persisted = deps.shadow_repo.insert_shadow(row)
        if not persisted:
            # The row was built but BigQuery rejected/failed the insert. Surface it
            # explicitly (never silently) — but do NOT break reviews_v1.
            log_event(logger, "warning", "communication_engine_v2 shadow persistence failed",
                      doc_id=doc_id, review_id=row.get("review_id"),
                      shadow_id=row.get("shadow_id"))
    except Exception as exc:  # noqa: BLE001 — shadow must never break reviews_v1
        log_event(logger, "warning", "communication_engine_v2 shadow failed (isolated)",
                  doc_id=doc_id, error=type(exc).__name__)


# --------------------------------------------------------------------------- #
# /poll
# --------------------------------------------------------------------------- #
def _draft_and_send(deps: Deps, subject, doc: dict, doc_id: str, communication_type, card_builder) -> dict:
    """Generate a draft, send the Telegram card, move the record to
    PENDING_APPROVAL, emit events. Shared by reviews and questions so both behave
    identically; the caller owns the surrounding try/except."""
    _emit_event(deps, doc, doc_id, EventType.FIRST_SEEN, status_after=Status.PROCESSING.value)
    gen, v2_meta = _generate_answer(deps, subject, communication_type)
    # keep the record leased (PROCESSING) until the card is really sent
    doc = deps.repo.save_generation(doc_id, gen, source="ai", set_pending=False)
    _emit_event(deps, doc, doc_id, EventType.AI_GENERATED,
                status_after=Status.PROCESSING.value, answer_version=doc.get("generation_number"))

    card_text, truncated = card_builder(doc, doc_id)
    card_text = _card_with_flags(card_text, v2_meta)
    keyboard = build_keyboard(doc_id, show_full=truncated)
    msg = deps.telegram.send_message(deps.settings.telegram_chat_id, card_text, keyboard)
    deps.repo.update(doc_id, {
        "telegram_message_id": str(msg.get("message_id", "")),
        "telegram_chat_id": str(deps.settings.telegram_chat_id),
        "status": Status.PENDING_APPROVAL.value, "lock_expires_at": None,
    })
    doc = _sync_current(deps, doc_id, doc)
    _emit_event(deps, doc, doc_id, EventType.SENT_TO_TELEGRAM,
                status_before=Status.PROCESSING.value, status_after=Status.PENDING_APPROVAL.value)
    return doc


def run_poll(deps: Deps) -> dict:
    poll_started = time.monotonic()
    feedbacks = deps.wb.iter_unanswered_feedbacks()
    fetched = len(feedbacks)
    processed = skipped = errors = 0

    for fb in feedbacks:
        review = Review.from_wb_feedback(fb)
        if not review.review_id:
            continue
        should_process, doc_id, doc = deps.repo.claim_review(review)
        if not should_process:
            skipped += 1
            continue
        try:
            doc = _draft_and_send(deps, review, doc, doc_id, CommunicationType.REVIEW, build_card)
            processed += 1
            # Shadow-only: run Communication Engine v2 for observation. Fully
            # isolated — never affects the card just sent or WB publishing.
            _run_shadow(deps, review, doc, doc_id)
        except Exception as exc:  # noqa: BLE001 — one bad review must not kill the batch
            deps.repo.mark_error(doc_id, f"{type(exc).__name__}: {exc}")
            _sync_current(deps, doc_id, doc)
            _emit_event(deps, deps.repo.get(doc_id) or doc, doc_id, EventType.FAILED,
                        status_after=Status.ERROR.value, error_code=type(exc).__name__, error_message=str(exc))
            log_event(logger, "error", "poll item failed", doc_id=doc_id, error=type(exc).__name__)
            errors += 1

    flush_events(deps)
    summary = {"fetched": fetched, "processed": processed, "skipped": skipped, "errors": errors}
    # WB buyer questions — separate entity, after reviews, behind its own flag.
    if getattr(deps.settings, "wb_questions_enabled", False):
        summary["questions"] = _run_questions(deps)
    # v3 SHADOW — strictly after all v2 work of this poll, time-boxed, never raises.
    if getattr(deps.settings, "v3_shadow_enabled", False) and deps.v3 is not None:
        from app.v3.shadow import run_shadow_isolated
        summary["v3_shadow"] = run_shadow_isolated(deps.v3, poll_started=poll_started)
    logger.info("poll done %s", summary)
    return summary


def _run_questions(deps: Deps) -> dict:
    """Poll and draft answers for WB buyer questions (entity_type=question).

    Runs only when WB_QUESTIONS_ENABLED. Caps NEW cards per poll at
    ``wb_questions_first_run_max`` so the first run does not flood Telegram with
    the historical backlog — unprocessed questions stay unanswered on WB and are
    picked up on later polls (newest first)."""
    legacy = _reconcile_legacy_published_questions(deps)
    reverified = _reverify_accepted_questions(deps)
    reverified["legacy"] = legacy
    reverified["recovery_cards"] = _restore_recovery_cards(deps)
    questions = deps.wb.iter_unanswered_questions()
    fetched = len(questions)
    processed = skipped = errors = 0
    cap = deps.settings.wb_questions_first_run_max
    capped = False

    for raw in questions:
        if processed >= cap:
            capped = True
            break
        question = Question.from_wb_question(raw)
        if not question.question_id:
            continue
        should_process, doc_id, doc = deps.repo.claim_question(question)
        if not should_process:
            skipped += 1
            continue
        try:
            _draft_and_send(deps, question, doc, doc_id, CommunicationType.QUESTION, build_question_card)
            processed += 1
        except Exception as exc:  # noqa: BLE001 — one bad question must not kill the batch
            deps.repo.mark_error(doc_id, f"{type(exc).__name__}: {exc}")
            _sync_current(deps, doc_id, doc)
            _emit_event(deps, deps.repo.get(doc_id) or doc, doc_id, EventType.FAILED,
                        status_after=Status.ERROR.value, error_code=type(exc).__name__, error_message=str(exc))
            log_event(logger, "error", "poll question failed", doc_id=doc_id, error=type(exc).__name__)
            errors += 1

    if capped:
        log_event(logger, "info", "WB questions per-poll cap reached; remainder next poll",
                  cap=cap, fetched=fetched)
    flush_events(deps)
    summary = {"fetched": fetched, "processed": processed, "skipped": skipped,
               "errors": errors, "capped": capped, "reverified": reverified}
    logger.info("poll questions done %s", summary)
    return summary


def _reconcile_legacy_published_questions(deps: Deps, limit: int = 20) -> dict:
    """Reconcile questions marked ``published`` BEFORE 1.5.0 against WB.

    Until 1.5.0 a question became ``published`` on any 2xx, and the request body
    was wrong, so such a status is not evidence of an answer on WB. A legacy doc
    is a question in ``published`` with no ``publication_state`` (set by every
    1.5.0 outcome). One read-only GET each; never writes to WB:

    * our text on WB    -> stays ``published``, gains ``verified_at`` (silent);
    * another text      -> ``answered_externally`` (Telegram card corrected);
    * no answer on WB   -> ``publish_unknown`` + «Опубликовать» button, so the
      operator publishes through the corrected path (which reads WB first).
    """
    counts = {"checked": 0, "verified": 0, "external": 0, "unknown": 0, "errors": 0}
    for doc_id, doc in deps.repo.list_by_status(Status.PUBLISHED.value, limit,
                                                entity_type="question"):
        if doc.get("entity_type") != "question" or doc.get("publication_state"):
            continue
        counts["checked"] += 1
        text = clean_answer(doc.get("final_answer") or doc.get("ai_answer"))
        trace = _new_trace(doc_id, doc, phase="legacy_reconcile",
                           state_before=Status.PUBLISHED.value)
        try:
            outcome = _verify_question(deps, doc["source_id"], text, trace, attempts=1)
            if outcome == "verify_error":
                counts["errors"] += 1
                continue  # WB unreadable now: leave as is, retry next poll
            if outcome == "verified":
                now = _now_iso()
                trace.update(final_publication_state=Status.PUBLISHED.value,
                             local_state_after=Status.PUBLISHED.value, finished_at=now)
                deps.repo.record_publication(
                    doc_id, Status.PUBLISHED.value,
                    {"publication_state": Status.PUBLISHED.value, "verified_at": now,
                     "legacy_reconciled_at": now}, trace)
                counts["verified"] += 1
                continue
            status = _finish_question(deps, doc_id, doc, text, outcome, trace,
                                      chat=doc.get("telegram_chat_id"),
                                      message_id=doc.get("telegram_message_id"),
                                      user_id=None, accepted=False)
            counts["external" if status == Status.ANSWERED_EXTERNALLY.value else "unknown"] += 1
        except Exception as exc:  # noqa: BLE001 — one item must not break the poll
            counts["errors"] += 1
            log_event(logger, "warning", "legacy question reconciliation failed",
                      doc_id=doc_id, error=type(exc).__name__)
    return counts


def _reverify_accepted_questions(deps: Deps, limit: int = 20) -> dict:
    """Re-read questions whose answer WB ACCEPTED but did not yet show (WB
    pre-moderates answers). Read-only towards WB — never re-sends. Resolves each
    to PUBLISHED (answer visible and ours), ANSWERED_EXTERNALLY (a different
    answer is there) or, after the verification window, PUBLISH_UNKNOWN."""
    counts = {"checked": 0, "verified": 0, "external": 0, "unknown": 0, "pending": 0, "errors": 0}
    window = timedelta(hours=float(getattr(deps.settings, "wb_question_verify_window_hours", 48)))
    for doc_id, doc in deps.repo.list_by_status(Status.PUBLISH_ACCEPTED.value, limit):
        if doc.get("entity_type") != "question":
            continue
        counts["checked"] += 1
        text = clean_answer(doc.get("final_answer") or doc.get("ai_answer"))
        trace = _new_trace(doc_id, doc, phase="reverify", state_before=Status.PUBLISH_ACCEPTED.value)
        try:
            outcome = _verify_question(deps, doc["source_id"], text, trace, attempts=1)
            if outcome == "not_visible":
                # Older / recovered docs may lack publish_accepted_at: fall back to
                # the last known publish/update time; none at all = window expired.
                accepted_at = _parse_ts(doc.get("publish_accepted_at")
                                        or doc.get("published_at") or doc.get("updated_at"))
                if accepted_at is not None and _now() - accepted_at < window:
                    counts["pending"] += 1
                    continue
                outcome = "window_expired"
            status = _finish_question(deps, doc_id, doc, text, outcome, trace,
                                      chat=doc.get("telegram_chat_id"),
                                      message_id=doc.get("telegram_message_id"), user_id=None,
                                      accepted=True)
            counts[{"published": "verified", "answered_externally": "external"}.get(status, "unknown")] += 1
        except Exception as exc:  # noqa: BLE001 — one item must not break the poll
            counts["errors"] += 1
            log_event(logger, "warning", "question re-verification failed",
                      doc_id=doc_id, error=type(exc).__name__)
    return counts


def _restore_recovery_cards(deps: Deps, limit: int = 20) -> dict:
    """Give every question in ``publish_unknown`` a live «Опубликовать» card.

    A doc that reached ``publish_unknown`` before 1.5.2 (or whose card could not be
    sent) has no ``recovery_card_sent_at``: its stored Telegram message may be a
    superseded card, so the operator sees no button. One read-only GET each:
    answer now on WB -> resolved as usual (no button); no answer -> a NEW actionable
    card. Never writes to WB."""
    counts = {"checked": 0, "sent": 0, "resolved": 0, "errors": 0}
    for doc_id, doc in deps.repo.list_by_status(Status.PUBLISH_UNKNOWN.value, limit,
                                                entity_type="question"):
        if doc.get("entity_type") != "question" or doc.get("recovery_card_sent_at"):
            continue
        counts["checked"] += 1
        text = clean_answer(doc.get("final_answer") or doc.get("ai_answer"))
        trace = _new_trace(doc_id, doc, phase="recovery_card",
                           state_before=Status.PUBLISH_UNKNOWN.value)
        try:
            outcome = _verify_question(deps, doc["source_id"], text, trace, attempts=1)
            if outcome == "verify_error":
                counts["errors"] += 1
                continue  # WB unreadable now: retry next poll
            if outcome in ("verified", "answered_externally"):
                _finish_question(deps, doc_id, doc, text, outcome, trace,
                                 chat=doc.get("telegram_chat_id"),
                                 message_id=doc.get("telegram_message_id"),
                                 user_id=None, accepted=False)
                counts["resolved"] += 1
                continue
            _send_recovery_card(deps, doc_id)
            counts["sent"] += 1
        except Exception as exc:  # noqa: BLE001 — one item must not break the poll
            counts["errors"] += 1
            log_event(logger, "warning", "question recovery card failed",
                      doc_id=doc_id, error=type(exc).__name__)
    return counts


_Q_RECOVERY_HEAD = ("⚠️ <b>Публикация не подтверждена на Wildberries</b>\n\n"
                    "Ответа на вопросе на WB нет, повторная отправка не выполнялась. Нажмите "
                    "«Опубликовать»: сервис сначала проверит вопрос на WB и отправит ответ, "
                    "только если его там нет.\n\n")
_CARD_SUPERSEDED = "ℹ️ Карточка устарела — актуальная отправлена ниже."


def _retire_card(deps: Deps, chat, old_message_id, new_message_id) -> None:
    """Strip the buttons from a superseded card (best effort: the old message may
    be gone or already identical — the new card is what matters)."""
    if not chat or not old_message_id or str(old_message_id) == str(new_message_id):
        return
    try:
        deps.telegram.edit_message_text(chat, old_message_id, _CARD_SUPERSEDED, None)
    except Exception as exc:  # noqa: BLE001
        log_event(logger, "info", "superseded card not retired", error=type(exc).__name__)


def _send_recovery_card(deps: Deps, doc_id: str) -> str:
    """Send a NEW «Опубликовать» card for a question in ``publish_unknown`` and make it
    the doc's card. A background transition cannot trust the stored message id: it
    may point to a card that was superseded (manual edit) or no longer editable."""
    doc = deps.repo.get(doc_id) or {}
    chat = doc.get("telegram_chat_id") or deps.settings.telegram_chat_id
    card_text, truncated = _card_builder_for(doc)(doc, doc_id)
    msg = deps.telegram.send_message(chat, _Q_RECOVERY_HEAD + card_text,
                                     build_keyboard(doc_id, show_full=truncated, retry=True))
    new_id = str((msg or {}).get("message_id", ""))
    deps.repo.update(doc_id, {"telegram_chat_id": str(chat), "telegram_message_id": new_id,
                              "recovery_card_sent_at": _now_iso()})
    _retire_card(deps, chat, doc.get("telegram_message_id"), new_id)
    log_event(logger, "info", "question recovery card sent", doc_id=doc_id,
              telegram_message_id=new_id)
    return new_id


# --------------------------------------------------------------------------- #
# Telegram webhook  (dedup handled by the route; this raises on transient infra)
# --------------------------------------------------------------------------- #
def handle_update(deps: Deps, update: dict) -> dict:
    if "callback_query" in update:
        return _handle_callback(deps, update["callback_query"])
    if "message" in update:
        return _handle_message(deps, update["message"])
    return {"status": "ignored"}


def _allowed(deps: Deps, chat_id, user_id) -> bool:
    ok = is_allowed(
        chat_id, user_id, deps.settings.allowed_chat_ids, deps.settings.telegram_allowed_user_ids
    )
    if not deps.settings.allowed_chat_ids and not deps.settings.telegram_allowed_user_ids:
        # is_allowed fails OPEN with no lists configured: record it as such, never as a real allow-list pass
        audit_event("auth_ok", route="/telegram-webhook", mechanism="telegram_allowlist",
                    principal_class="unrestricted", result="open_no_allowlist")
    else:
        full = bool(deps.settings.allowed_chat_ids) and bool(deps.settings.telegram_allowed_user_ids)
        audit_event("auth_ok" if ok else "auth_denied", route="/telegram-webhook", mechanism="telegram_allowlist",
                    principal_class="allowlisted_user" if ok else "unknown",
                    # only chat AND user lists make a full allow-list pass; one list is recorded as partial
                    result=("ok" if full else "ok_partial_allowlist") if ok else "not_allowlisted")
    return ok


def _handle_callback(deps: Deps, cq: dict) -> dict:
    cq_id = cq.get("id")
    message = cq.get("message", {}) or {}
    chat = (message.get("chat", {}) or {}).get("id")
    message_id = message.get("message_id")
    user_id = (cq.get("from", {}) or {}).get("id")

    if not _allowed(deps, chat, user_id):
        if cq_id:
            deps.telegram.answer_callback_query(cq_id, "Нет доступа")
        return {"status": "unauthorized"}

    action, doc_id = parse_callback(cq.get("data", ""))
    if not action:
        if cq_id:
            deps.telegram.answer_callback_query(cq_id, "Неизвестная команда")
        return {"status": "bad_request"}

    if cq_id:
        deps.telegram.answer_callback_query(cq_id)  # stop the spinner

    if action == "pub":
        return _publish(deps, doc_id, chat, message_id, user_id)
    if action == "skip":
        return _skip(deps, doc_id, chat, message_id)
    if action == "regen":
        return _regenerate(deps, doc_id, chat, message_id)
    if action == "edit":
        return _start_edit(deps, doc_id, chat, user_id)
    if action == "show":
        return _show_full(deps, doc_id, chat, message_id)
    return {"status": "ignored"}


def _stale(deps: Deps, chat, message: str = "⚠️ Действие недоступно: отзыв уже обработан."):
    deps.telegram.send_message(chat, message)
    return {"status": "stale"}


def _publish(deps: Deps, doc_id, chat, message_id, user_id) -> dict:
    # Entity-aware, independent fail-closed publish gates. Questions and reviews
    # each have their own WB_*_PUBLISH_ENABLED flag and their own WB endpoint.
    peek = deps.repo.get(doc_id)
    is_question = (peek or {}).get("entity_type") == "question"
    if is_question:
        gate_open = deps.settings.wb_question_publish_enabled
        disabled_msg = ("🚫 Публикация ответов на вопросы отключена "
                        "(WB_QUESTION_PUBLISH_ENABLED=false).")
    else:
        gate_open = deps.settings.wb_publish_enabled
        disabled_msg = ("🚫 Публикация в WB отключена (WB_PUBLISH_ENABLED=false). "
                        "Включите только после сверки endpoint по live Swagger.")
    if not gate_open:
        deps.telegram.send_message(chat, disabled_msg)
        return {"status": "publish_disabled"}
    blocked = _v3_publish_gate(deps, doc_id, peek, chat)
    if blocked is not None:
        return blocked

    try:
        doc = deps.repo.begin_publish(doc_id)  # atomic; recovers a crashed lease
    except InvalidTransition:
        return _stale(deps, chat, "⚠️ Этот отзыв уже обрабатывается или опубликован.")
    except NotFound:
        deps.telegram.send_message(chat, "⚠️ Запись не найдена.")
        return {"status": "not_found"}

    # Crash recovery: a previous publish held the lease and the process died
    # BEFORE mark_published. For reviews there is no read-back here, so we must
    # NOT blindly re-publish (risk of a second public answer) — ask the operator.
    # Questions are safe: _publish_question reads WB state before any write.
    if (doc.get("recovered_from_publishing") and deps.settings.wb_verify_before_publish
            and not is_question):
        deps.repo.mark_publish_failed(doc_id, "recovered_lease",
                                      "publishing lease recovered — verify WB state before re-publish")
        _sync_current(deps, doc_id, doc)
        deps.telegram.edit_message_text(
            chat, message_id,
            "⚠️ Предыдущая публикация прервалась на полуслове. Проверьте в кабинете WB, "
            "не опубликован ли ответ уже, затем нажмите «Опубликовать» снова.",
            build_keyboard(doc_id, retry=True),
        )
        return {"status": "publish_recovery_needs_verification"}

    text = clean_answer(doc.get("final_answer") or doc.get("ai_answer"))
    if not is_within_wb_limit(text):
        deps.repo.mark_publish_failed(doc_id, "invalid_length", "empty or over WB limit")
        _sync_current(deps, doc_id, doc)
        deps.telegram.edit_message_text(
            chat, message_id,
            "⚠️ Ответ пустой или длиннее лимита WB (1000). Отредактируйте и попробуйте снова.",
            build_keyboard(doc_id, retry=True),
        )
        return {"status": "invalid_length"}

    if is_question:
        return _publish_question(deps, doc_id, doc, text, chat, message_id, user_id)

    try:
        wb_resp = deps.wb.publish_answer(doc["source_id"], text)
    except WBPublishOutcomeUnknown as exc:
        # The POST may have landed: never re-send blindly (duplicate public answer).
        trace = _new_trace(doc_id, doc, phase="publish", state_before=Status.PUBLISHING.value)
        trace.update(http_status=exc.status_code, error_class=type(exc).__name__,
                     final_publication_state=Status.PUBLISH_UNKNOWN.value)
        deps.repo.record_publication(doc_id, Status.PUBLISH_UNKNOWN.value,
                                     {"last_error_code": exc.status_code,
                                      "last_error_message": str(exc)[:500]}, trace)
        after = _sync_current(deps, doc_id, doc)
        _emit_event(deps, after, doc_id, EventType.PUBLISH_UNKNOWN, best_effort=False,
                    status_before=Status.PUBLISHING.value, status_after=Status.PUBLISH_UNKNOWN.value,
                    telegram_user_id=user_id, error_code=exc.status_code, error_message=str(exc),
                    attempt=trace["publication_attempt_id"], payload=trace)
        deps.telegram.edit_message_text(
            chat, message_id,
            "⚠️ <b>Результат публикации неизвестен</b>\n\nWB не подтвердил приём ответа, "
            "повторная отправка не выполнялась. Проверьте отзыв в кабинете WB; если ответа нет — "
            "нажмите «Опубликовать» снова.",
            build_keyboard(doc_id, retry=True),
        )
        return {"status": "publish_unknown", "code": exc.status_code}
    except WBApiError as exc:
        deps.repo.mark_publish_failed(doc_id, exc.status_code, str(exc))
        after = _sync_current(deps, doc_id, doc)
        _emit_event(deps, after, doc_id, EventType.FAILED, best_effort=False,
                    status_before=Status.PUBLISHING.value, status_after=Status.PUBLISH_FAILED.value,
                    telegram_user_id=user_id, error_code=exc.status_code, error_message=str(exc),
                    attempt=after.get("publish_attempts"))
        deps.telegram.edit_message_text(
            chat, message_id, _wb_publish_error_message(exc, is_question=is_question),
            build_keyboard(doc_id, retry=True),
        )
        return {"status": "publish_failed", "code": exc.status_code}

    deps.repo.mark_published(doc_id, wb_resp if isinstance(wb_resp, dict) else {}, user_id)
    after = _sync_current(deps, doc_id, doc)
    _emit_event(deps, after, doc_id, EventType.PUBLISHED, best_effort=False,
                status_before=Status.PUBLISHING.value, status_after=Status.PUBLISHED.value,
                telegram_user_id=user_id)
    deps.telegram.edit_message_text(chat, message_id, f"✅ <b>Опубликовано</b>\n\n{escape_html(text)}", None)
    return {"status": "published"}


# --------------------------------------------------------------------------- #
# Question publication: read-before-write, write, bounded read-back
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    return _now().isoformat()


def _parse_ts(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _norm(text) -> str:
    return " ".join(str(text or "").split())


def _wb_answer_text(question: dict) -> Optional[str]:
    """The answer text currently on WB, or None when the question is unanswered."""
    answer = (question or {}).get("answer")
    if isinstance(answer, dict) and _norm(answer.get("text")):
        return answer.get("text")
    return None


def _new_trace(doc_id: str, doc: dict, *, phase: str, state_before: str) -> dict:
    """One publication/verification record. Holds hashes and safe excerpts only:
    never the auth header, never a token (redacted), never the full answer text."""
    return {
        "publication_attempt_id": uuid.uuid4().hex,
        "phase": phase,
        "communication_id": doc_id,
        "entity_type": doc.get("entity_type", "review"),
        "wb_source_id": doc.get("source_id", ""),
        "local_state_before": state_before,
        "started_at": _now_iso(),
        "verification_attempts": 0,
        "verification_result": None,
        "final_publication_state": None,
        "error_class": None,
    }


def _verify_question(deps: Deps, question_id: str, text: str, trace: dict,
                     attempts: Optional[int] = None) -> str:
    """Read the question back from WB (bounded; reads only, never writes).

    Returns ``verified`` (our answer is on WB), ``answered_externally`` (another
    answer is there), ``not_visible`` (no answer yet — e.g. WB pre-moderation) or
    ``verify_error`` (WB could not be read)."""
    if attempts is None:
        attempts = max(1, int(getattr(deps.settings, "wb_question_verify_attempts", 3)))
    delay = float(getattr(deps.settings, "wb_question_verify_delay_seconds", 2.0))
    result = "not_visible"
    for i in range(attempts):
        if i and delay > 0:
            time.sleep(delay)
        trace["verification_attempts"] += 1
        try:
            current = deps.wb.get_question(question_id)
        except WBApiError as exc:
            result = "verify_error"
            trace["verify_error_code"] = exc.status_code
            continue
        on_wb = _wb_answer_text(current)
        if on_wb is None:
            result = "not_visible"
            continue
        result = "verified" if _norm(on_wb) == _norm(text) else "answered_externally"
        break
    trace["verification_result"] = result
    return result


# Operator-facing texts. Only VERIFIED may say «Опубликовано».
_Q_MSG = {
    "published": "✅ <b>Опубликовано</b> — ответ подтверждён на Wildberries\n\n{text}",
    "publish_accepted": ("⏳ <b>Отправлено в WB, публикация ещё не подтверждена</b>\n\n"
                         "WB принял ответ, но на вопросе его пока нет (ответы продавцов проходят "
                         "модерацию WB). Статус проверяется автоматически при каждом опросе.\n\n{text}"),
    "publish_unknown": ("⚠️ <b>Публикация не подтверждена</b>\n\nОтвет на WB не найден, повторная "
                        "отправка не выполнялась. Нажмите «Опубликовать»: сервис сначала проверит "
                        "вопрос на WB и отправит ответ, только если его там нет.\n\n{text}"),
    "answered_externally": ("ℹ️ <b>На вопрос уже есть другой ответ на WB</b> (например, из кабинета). "
                            "Наш текст не отправлялся.\n\n{text}"),
}
_Q_EVENT = {
    "published": EventType.PUBLISHED,
    "publish_accepted": EventType.PUBLISH_ACCEPTED,
    "publish_unknown": EventType.PUBLISH_UNKNOWN,
    "answered_externally": EventType.ANSWERED_EXTERNALLY,
}


def _finish_question(deps: Deps, doc_id: str, doc: dict, text: str, outcome: str, trace: dict,
                     *, chat, message_id, user_id, accepted: bool) -> str:
    """Map a verification outcome to the final state, persist it with the trace,
    emit the event and tell the operator. Returns the new status."""
    if outcome == "verified":
        status = Status.PUBLISHED.value
    elif outcome == "answered_externally":
        status = Status.ANSWERED_EXTERNALLY.value
    elif accepted and outcome in ("not_visible", "verify_error"):
        status = Status.PUBLISH_ACCEPTED.value
    else:  # write outcome unknown and not visible, or accepted window expired
        status = Status.PUBLISH_UNKNOWN.value

    now = _now_iso()
    fields: dict = {"publication_state": status}
    if status == Status.PUBLISHED.value:
        fields.update(published_at=now, verified_at=now)
        if user_id is not None:
            fields["published_by_telegram_user_id"] = str(user_id)
    elif status == Status.PUBLISH_ACCEPTED.value:
        fields["publish_accepted_at"] = doc.get("publish_accepted_at") or now
        if user_id is not None:
            fields["published_by_telegram_user_id"] = str(user_id)
    interactive = user_id is not None and chat and message_id
    if interactive:
        # the card the operator pressed is the live one: keep Firestore pointing at it
        fields.update(telegram_chat_id=str(chat), telegram_message_id=str(message_id))
        if status == Status.PUBLISH_UNKNOWN.value:
            fields["recovery_card_sent_at"] = now  # this very card gets the button
    trace.update(final_publication_state=status, local_state_after=status, finished_at=now)
    deps.repo.record_publication(doc_id, status, fields, trace)
    after = _sync_current(deps, doc_id, doc)
    _emit_event(deps, after, doc_id, _Q_EVENT[status], best_effort=False,
                status_before=trace.get("local_state_before", ""), status_after=status,
                telegram_user_id=user_id, attempt=trace["publication_attempt_id"], payload=trace)
    if interactive:
        keyboard = build_keyboard(doc_id, retry=True) if status == Status.PUBLISH_UNKNOWN.value else None
        deps.telegram.edit_message_text(chat, message_id,
                                        _Q_MSG[status].format(text=escape_html(text)), keyboard)
    elif status == Status.PUBLISH_UNKNOWN.value:
        # Background (poll) transition: the stored card may be superseded, so editing
        # it can leave the operator without a button. Send a fresh actionable card;
        # if Telegram fails, the next poll's _restore_recovery_cards retries.
        try:
            _send_recovery_card(deps, doc_id)
        except Exception as exc:  # noqa: BLE001 — state is already persisted
            log_event(logger, "warning", "question recovery card failed",
                      doc_id=doc_id, error=type(exc).__name__)
    elif chat and message_id:
        try:
            deps.telegram.edit_message_text(chat, message_id,
                                            _Q_MSG[status].format(text=escape_html(text)), None)
        except Exception as exc:  # noqa: BLE001 — state is already persisted
            log_event(logger, "info", "question card not updated", doc_id=doc_id,
                      error=type(exc).__name__)
    log_event(logger, "info", "question publication outcome", doc_id=doc_id, status=status,
              attempt=trace["publication_attempt_id"], verification=trace.get("verification_result"),
              http_status=trace.get("http_status"))
    return status


def _publish_question(deps: Deps, doc_id: str, doc: dict, text: str, chat, message_id, user_id) -> dict:
    """Publish a WB question answer without false success and without duplicates.

    1. read WB first — already answered? then no write (idempotent re-tap,
       crashed-lease recovery, answers typed in the WB cabinet);
    2. PATCH with the official body (``answer.text``); 2xx = ACCEPTED only;
    3. bounded read-back — only a visible, matching answer is PUBLISHED.
    A write whose outcome is unknown is never repeated automatically.
    """
    qid = doc["source_id"]
    trace = _new_trace(doc_id, doc, phase="publish", state_before=Status.PUBLISHING.value)
    trace.update(endpoint=deps.settings.wb_questions_path,
                 method=deps.settings.wb_question_answer_method)
    try:
        current = deps.wb.get_question(qid)
    except WBApiError as exc:
        # Cannot see WB -> do not write blind. Stays actionable.
        trace.update(precheck="error", error_class=type(exc).__name__, http_status=exc.status_code,
                     final_publication_state=Status.PUBLISH_FAILED.value)
        deps.repo.record_publication(doc_id, Status.PUBLISH_FAILED.value,
                                     {"last_error_code": exc.status_code,
                                      "last_error_message": f"precheck failed: {exc}"[:500]}, trace)
        after = _sync_current(deps, doc_id, doc)
        _emit_event(deps, after, doc_id, EventType.FAILED, best_effort=False,
                    status_before=Status.PUBLISHING.value, status_after=Status.PUBLISH_FAILED.value,
                    telegram_user_id=user_id, error_code=exc.status_code, error_message=str(exc),
                    attempt=trace["publication_attempt_id"], payload=trace)
        deps.telegram.edit_message_text(chat, message_id,
                                        _wb_publish_error_message(exc, is_question=True),
                                        build_keyboard(doc_id, retry=True))
        return {"status": "publish_failed", "code": exc.status_code}

    on_wb = _wb_answer_text(current)
    if on_wb is not None:
        trace.update(precheck="already_answered", write="skipped")
        outcome = "verified" if _norm(on_wb) == _norm(text) else "answered_externally"
        trace["verification_result"] = outcome
        status = _finish_question(deps, doc_id, doc, text, outcome, trace,
                                  chat=chat, message_id=message_id, user_id=user_id, accepted=False)
        return {"status": status, "write": "skipped"}

    trace.update(precheck="unanswered", request_started_at=_now_iso())
    accepted = False
    try:
        res = deps.wb.publish_question_answer(qid, text)
        res = res if isinstance(res, dict) else {}
        body_excerpt = redact(json.dumps(res.get("response"), ensure_ascii=False))[:200]
        trace.update(write="sent", http_status=res.get("status_code"),
                     request_payload_sha256=res.get("request_sha256"),
                     response_excerpt=body_excerpt,
                     response_sha256=hashlib.sha256(body_excerpt.encode()).hexdigest())
        accepted = True
    except WBPublishOutcomeUnknown as exc:
        trace.update(write="outcome_unknown", http_status=exc.status_code,
                     error_class=type(exc).__name__,
                     request_payload_sha256=getattr(exc, "request_sha256", None))
    except WBApiError as exc:
        trace.update(write="rejected", http_status=exc.status_code, error_class=type(exc).__name__,
                     response_excerpt=redact(str(exc.body or ""))[:200],
                     final_publication_state=Status.PUBLISH_FAILED.value)
        deps.repo.record_publication(doc_id, Status.PUBLISH_FAILED.value,
                                     {"last_error_code": exc.status_code,
                                      "last_error_message": str(exc)[:500]}, trace)
        after = _sync_current(deps, doc_id, doc)
        _emit_event(deps, after, doc_id, EventType.FAILED, best_effort=False,
                    status_before=Status.PUBLISHING.value, status_after=Status.PUBLISH_FAILED.value,
                    telegram_user_id=user_id, error_code=exc.status_code, error_message=str(exc),
                    attempt=trace["publication_attempt_id"], payload=trace)
        deps.telegram.edit_message_text(chat, message_id,
                                        _wb_publish_error_message(exc, is_question=True),
                                        build_keyboard(doc_id, retry=True))
        return {"status": "publish_failed", "code": exc.status_code}

    outcome = _verify_question(deps, qid, text, trace)
    status = _finish_question(deps, doc_id, doc, text, outcome, trace,
                              chat=chat, message_id=message_id, user_id=user_id, accepted=accepted)
    return {"status": status, "write": trace.get("write")}


def _skip(deps: Deps, doc_id, chat, message_id) -> dict:
    try:
        deps.repo.begin_action(doc_id, "skip")
    except InvalidTransition:
        return _stale(deps, chat)
    except NotFound:
        deps.telegram.send_message(chat, "⚠️ Запись не найдена.")
        return {"status": "not_found"}
    after = _sync_current(deps, doc_id)
    _emit_event(deps, after, doc_id, EventType.SKIPPED, best_effort=False, status_after=Status.SKIPPED.value)
    deps.telegram.edit_message_text(chat, message_id, "⏭ <b>Пропущено</b>", None)
    return {"status": "skipped"}


def _regenerate(deps: Deps, doc_id, chat, message_id) -> dict:
    # Lock the record in REGENERATING with a token; a concurrent publish/skip/
    # second-regenerate is rejected while we call OpenAI.
    try:
        doc, token = deps.repo.begin_regenerate(doc_id)
    except InvalidTransition:
        return _stale(deps, chat)
    except NotFound:
        deps.telegram.send_message(chat, "⚠️ Запись не найдена.")
        return {"status": "not_found"}

    subject, communication_type = _subject_from_doc(doc)
    try:
        gen, v2_meta = _generate_answer(deps, subject, communication_type)
    except Exception as exc:  # noqa: BLE001
        deps.repo.cancel_draft(doc_id, token)  # unlock back to pending_approval
        if is_transient(exc):
            raise  # webhook returns 5xx, Telegram redelivers
        deps.telegram.send_message(chat, f"⚠️ Не удалось перегенерировать: {type(exc).__name__}")
        return {"status": "regen_failed"}

    try:
        doc = deps.repo.commit_regenerate(doc_id, gen, token)  # verifies lock+token
    except InvalidTransition:
        return _stale(deps, chat, "⚠️ Перегенерация неактуальна: отзыв уже изменён.")
    _sync_current(deps, doc_id, doc)
    _emit_event(deps, doc, doc_id, EventType.REGENERATED, best_effort=False,
                answer_version=doc.get("generation_number"))
    card_text, truncated = _card_builder_for(doc)(doc, doc_id)
    card_text = _card_with_flags(card_text, v2_meta)
    deps.telegram.edit_message_text(chat, message_id, card_text, build_keyboard(doc_id, show_full=truncated))
    return {"status": "regenerated", "generation_number": doc.get("generation_number")}


def _start_edit(deps: Deps, doc_id, chat, user_id) -> dict:
    # Lock the record in EDITING with a token + capture the current version.
    try:
        _, token, expected_gen = deps.repo.begin_edit(doc_id)
    except InvalidTransition:
        return _stale(deps, chat)
    except NotFound:
        deps.telegram.send_message(chat, "⚠️ Запись не найдена.")
        return {"status": "not_found"}

    # If prompting or persisting the session fails, the record must NOT stay
    # stuck in EDITING — roll the lock back to pending_approval, then re-raise so
    # the webhook returns 5xx and Telegram redelivers (a later tap can edit again).
    try:
        prompt = deps.telegram.send_force_reply(
            chat, "✏️ Ответьте на это сообщение новым текстом ответа.\n/cancel — отмена."
        )
        expires = (_now() + timedelta(seconds=deps.settings.telegram_edit_timeout_seconds)).isoformat()
        deps.repo.set_editing_session(user_id, doc_id, expires,
                                      prompt_message_id=prompt.get("message_id"),
                                      lock_token=token, expected_generation=expected_gen)
    except Exception:
        deps.repo.cancel_draft(doc_id, token)  # release the EDITING lock
        raise
    return {"status": "editing_started"}


def _show_full(deps: Deps, doc_id, chat, message_id) -> dict:
    doc = deps.repo.get(doc_id)
    if not doc:
        return {"status": "not_found"}
    card_text, _ = _card_builder_for(doc)(doc, doc_id, full=True)
    deps.telegram.edit_message_text(chat, message_id, card_text, build_keyboard(doc_id))
    return {"status": "shown"}


def _v3_manual_shadow_check(deps: Deps, doc_id: str, doc: dict, text: str) -> None:
    """Shadow-only: record the v3 verifier verdict for a manual edit. Never affects the card,
    the state machine or publication (enforcement is a separate, owner-gated flag)."""
    if deps.v3 is None:
        return
    try:
        from app.v3.shadow import check_text
        check_text(deps.v3, doc_id, doc, text, run_kind="manual_edit_shadow")
    except Exception as exc:  # noqa: BLE001 — shadow must never break the edit flow
        log_event(logger, "warning", "v3 manual-edit shadow check failed (isolated)", error=type(exc).__name__)


def _v3_publish_gate(deps: Deps, doc_id: str, peek: dict | None, chat) -> Optional[dict]:
    """OWNER-GATED (V3_ENFORCE_MANUAL_EDIT_VERIFIER, default false): refuse to publish a
    MANUAL text that the v3 verifier BLOCKs. No state change; the card stays actionable."""
    if not getattr(deps.settings, "v3_enforce_manual_edit_verifier", False) or deps.v3 is None or not peek:
        return None
    versions = peek.get("answer_versions") or []
    last = versions[-1] if versions and isinstance(versions[-1], dict) else {}
    if last.get("source") != "manual":
        return None
    try:
        from app.v3.shadow import check_text
        text = clean_answer(peek.get("final_answer") or peek.get("ai_answer"))
        result = check_text(deps.v3, doc_id, peek, text, run_kind="manual_edit_gate")
    except Exception as exc:  # noqa: BLE001 — fail-closed only when explicitly enforced
        log_event(logger, "warning", "v3 publish gate error", error=type(exc).__name__)
        deps.telegram.send_message(chat, "⛔ Проверка текста v3 недоступна — публикация ручного текста отложена.")
        return {"status": "v3_gate_error"}
    if result is not None and result.blocked:
        rules = ", ".join(result.rule_ids("BLOCK"))
        deps.telegram.send_message(chat, f"⛔ Ручной текст не прошёл проверку ({escape_html(rules)}). "
                                         "Исправьте текст через «Изменить».")
        return {"status": "v3_blocked", "rules": result.rule_ids("BLOCK")}
    return None


def _handle_message(deps: Deps, message: dict) -> dict:
    chat = (message.get("chat", {}) or {}).get("id")
    user_id = (message.get("from", {}) or {}).get("id")
    text = (message.get("text") or "").strip()
    reply_to = ((message.get("reply_to_message") or {}) or {}).get("message_id")

    if not _allowed(deps, chat, user_id):
        return {"status": "unauthorized"}

    session = deps.repo.get_editing_session(user_id)

    if text == "/cancel":
        if session:
            deps.repo.cancel_draft(session["doc_id"], session.get("lock_token"))
        deps.repo.clear_editing_session(user_id)
        deps.telegram.send_message(chat, "Редактирование отменено.")
        return {"status": "cancelled"}

    if not session:
        return {"status": "ignored"}

    # Only accept the new answer as a reply to the specific ForceReply prompt.
    prompt_id = session.get("prompt_message_id")
    if prompt_id is not None and reply_to != prompt_id:
        return {"status": "ignored"}

    if session.get("expires_at") and _now().isoformat() > session["expires_at"]:
        deps.repo.cancel_draft(session["doc_id"], session.get("lock_token"))
        deps.repo.clear_editing_session(user_id)
        deps.telegram.send_message(chat, "⌛ Время редактирования истекло. Нажмите «Изменить» ещё раз.")
        return {"status": "expired"}

    new_text = clean_answer(text)
    if not new_text:
        deps.telegram.send_message(chat, "Пустой текст — редактирование не применено.")
        return {"status": "empty"}
    if len(new_text) > WB_ANSWER_MAX_LEN:
        # keep the session open so the user can resend a shorter version
        deps.telegram.send_message(
            chat, f"⚠️ Слишком длинно: {len(new_text)} символов при лимите WB {WB_ANSWER_MAX_LEN}. "
                  "Пришлите более короткий вариант или /cancel."
        )
        return {"status": "too_long"}

    doc_id = session["doc_id"]
    try:
        # transactional: status still EDITING, our token, unchanged version
        doc = deps.repo.commit_manual_answer(doc_id, new_text, session.get("lock_token"),
                                             session.get("expected_generation"))
    except InvalidTransition:
        deps.repo.clear_editing_session(user_id)
        deps.telegram.send_message(chat, "⚠️ Правка неактуальна: отзыв уже изменён или обработан.")
        return {"status": "stale_edit"}
    except NotFound:
        deps.repo.clear_editing_session(user_id)
        deps.telegram.send_message(chat, "⚠️ Запись не найдена.")
        return {"status": "not_found"}
    deps.repo.clear_editing_session(user_id)
    _sync_current(deps, doc_id, doc)
    _emit_event(deps, doc, doc_id, EventType.MANUALLY_EDITED, best_effort=False,
                telegram_user_id=user_id, answer_version=doc.get("generation_number"))
    card_text, truncated = _card_builder_for(doc)(doc, doc_id)
    card_text = _card_with_flags(card_text, {"flags": _manual_text_flags(deps, doc, new_text)})
    msg = deps.telegram.send_message(chat, card_text, build_keyboard(doc_id, show_full=truncated))
    _v3_manual_shadow_check(deps, doc_id, doc, new_text)
    # The new card supersedes the stored one: without this, later background updates
    # (reconcile / re-verify) edit the OLD message and the operator never sees them.
    new_id = str((msg or {}).get("message_id", ""))
    if new_id:
        deps.repo.update(doc_id, {"telegram_chat_id": str(chat), "telegram_message_id": new_id})
        _retire_card(deps, chat, doc.get("telegram_message_id"), new_id)
    return {"status": "edited"}
