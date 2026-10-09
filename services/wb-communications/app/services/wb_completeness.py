"""R2.4A.1 operational completeness: rating-only ingestion, stale-card reconciliation, poll
health for /status. No generation and no WB writes here.

Truth about a seller answer is the actual answer TEXT returned by WB, never the isAnswered
flag: WB marks a rating-only review isAnswered=true although nobody answered it.
"""
from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone

from app.utils.logging import get_logger, log_event

logger = get_logger(__name__)
MSK = timezone(timedelta(hours=3))
RECONCILE_STATUSES = ("pending_approval", "policy_blocked", "policy_check_failed", "publish_failed")
PENDING_STATUSES = RECONCILE_STATUSES
_RETIRED = "ℹ️ На Wildberries уже опубликован ответ.\nКарточка закрыта."
_RETIRED_SAME = "✅ Этот ответ уже опубликован на Wildberries.\nКарточка закрыта."


def _now():
    return datetime.now(timezone.utc)


def _norm(text) -> str:
    return " ".join(str(text or "").split())


def seller_answer_text(raw: dict | None) -> str:
    """The seller answer actually present on WB ('' when there is none)."""
    answer = (raw or {}).get("answer")
    return _norm(answer.get("text")) if isinstance(answer, dict) else ""


def rating_only_without_seller_answer(fb: dict) -> bool:
    """A star rating with no words, no tags and no actual seller answer (isAnswered ignored)."""
    if not fb.get("productValuation"):
        return False
    if any(str(fb.get(k) or "").strip() for k in ("text", "pros", "cons")) or fb.get("bables"):
        return False
    return not seller_answer_text(fb)


# --- rating-only ingestion ---------------------------------------------------------------------
def ingest_rating_only(deps, ingest_one) -> dict:
    """Bounded recent window of the answered feed → rating-only reviews without a seller
    answer → the SAME per-review pipeline (claim dedupe by review id → 3.1E → card)."""
    s = deps.settings
    counts = {"fetched": 0, "eligible": 0, "processed": 0, "skipped": 0, "errors": 0, "capped": False}
    cutoff = _now() - timedelta(hours=int(s.wb_rating_only_lookback_hours))
    raw = deps.wb.iter_recent_answered_feedbacks(int(cutoff.timestamp()))
    counts["fetched"] = len(raw)
    for fb in raw:
        try:
            created = datetime.fromisoformat(str(fb.get("createdDate") or "").replace("Z", "+00:00"))
        except ValueError:
            continue
        if created < cutoff or not rating_only_without_seller_answer(fb):
            continue
        counts["eligible"] += 1
        if counts["processed"] >= int(s.wb_rating_only_max_per_poll):
            counts["capped"] = True
            break
        outcome = ingest_one(fb)
        counts[outcome] = counts.get(outcome, 0) + 1
    return counts


# --- reconciliation of cards answered in the WB cabinet ------------------------------------------
def _read_wb(deps, doc):
    if doc.get("entity_type") == "question":
        return deps.wb.get_question(doc["source_id"]), None
    data = deps.wb.get_feedback(doc["source_id"], retries=1, timeout_seconds=5.0)
    if str(data.get("id")) != str(doc["source_id"]):
        raise ValueError("feedback identity mismatch")
    return data, ((data.get("answer") or {}).get("state") if isinstance(data.get("answer"), dict) else None)


def reconcile_external_answers(deps) -> dict:
    """Close draft-state cards whose communication already has a seller answer on WB.
    Bounded per poll; read-only towards WB; a read error leaves the record unchanged."""
    from app.domain.statuses import EventType
    from app.services.pipeline import _emit_event, _sync_current
    counts = {"checked": 0, "answered_externally": 0, "published": 0, "unchanged": 0, "errors": 0}
    candidates = []
    for status in RECONCILE_STATUSES:
        candidates += deps.repo.list_by_status(status, limit=200)
    candidates.sort(key=lambda row: (row[1].get("wb_reconcile_checked_at") or "", row[1].get("first_seen_at") or ""))
    for doc_id, doc in candidates[: int(deps.settings.wb_reconcile_max_per_poll)]:
        counts["checked"] += 1
        stamp = _now().isoformat()
        try:
            data, state = _read_wb(deps, doc)
        except Exception as exc:  # noqa: BLE001 — retry next poll, nothing changes
            counts["errors"] += 1
            log_event(logger, "warning", "wb reconcile read failed", doc_id=doc_id, error=type(exc).__name__)
            continue
        actual = seller_answer_text(data)
        local = _norm(doc.get("final_answer") or doc.get("ai_answer"))
        if not actual or (actual == local and doc.get("entity_type") != "question" and state != "wbRu"):
            # No seller answer (incl. WB's isAnswered=true on a rating-only review), or our own
            # text without proof of public visibility: keep the card, look again later.
            deps.repo.apply_if_unchanged(doc_id, doc.get("status"), doc.get("generation_number", 0),
                                         {"wb_reconcile_checked_at": stamp})
            counts["unchanged"] += 1
            continue
        same = actual == local
        status = "published" if same else "answered_externally"
        fields = {"status": status, "publication_state": status, "reconciled_at": stamp,
                  "wb_reconcile_checked_at": stamp, "feedback_checked_at": stamp,
                  "wb_answer_sha256": hashlib.sha256(actual.encode()).hexdigest()}
        if same:
            fields.update(verified_at=stamp, published_at=doc.get("published_at") or stamp)
        after = deps.repo.apply_if_unchanged(doc_id, doc.get("status"), doc.get("generation_number", 0), fields)
        if after is None:  # an operator acted meanwhile: never reconcile underneath them
            counts["unchanged"] += 1
            continue
        counts[status] += 1
        _sync_current(deps, doc_id, after)
        _emit_event(deps, after, doc_id, EventType.PUBLISHED if same else EventType.ANSWERED_EXTERNALLY,
                    best_effort=True, status_before=doc.get("status"), status_after=status,
                    payload={"reconciled": True, "wb_answer_sha256": fields["wb_answer_sha256"]})
        chat, message_id = doc.get("telegram_chat_id") or deps.settings.telegram_chat_id, doc.get("telegram_message_id")
        if chat and message_id:
            try:
                deps.telegram.edit_message_text(chat, message_id, _RETIRED_SAME if same else _RETIRED, None)
            except Exception as exc:  # noqa: BLE001 — the record is already closed
                log_event(logger, "info", "reconciled card not retired", error=type(exc).__name__)
    return counts


# --- poll health and /status ---------------------------------------------------------------------
def record_poll_health(deps, started_at: datetime, summary: dict | None, error: str | None = None) -> None:
    """One small operational record per natural poll (best effort, never fails the poll)."""
    try:
        summary = summary or {}
        q, ro, rc = summary.get("questions") or {}, summary.get("rating_only") or {}, summary.get("reconciled") or {}
        cards = int(summary.get("processed") or 0) + int(q.get("processed") or 0) + int(ro.get("processed") or 0)
        review_errors = int(summary.get("errors") or 0) + int(ro.get("errors") or 0)
        pending = None
        try:
            pending = sum(len(deps.repo.list_by_status(st, limit=500)) for st in PENDING_STATUSES)
        except Exception:  # noqa: BLE001
            pass
        finished = _now()
        deps.repo.save_poll_health({
            "started_at": started_at.isoformat(), "finished_at": finished.isoformat(),
            "revision": os.environ.get("K_REVISION", ""),
            "reviews_fetched": int(summary.get("fetched") or 0) + int(ro.get("eligible") or 0),
            "reviews_processed": int(summary.get("processed") or 0) + int(ro.get("processed") or 0),
            "reviews_skipped": int(summary.get("skipped") or 0) + int(ro.get("skipped") or 0),
            "reviews_errors": review_errors,
            "rating_only_processed": int(ro.get("processed") or 0),
            "questions_fetched": int(q.get("fetched") or 0), "questions_processed": int(q.get("processed") or 0),
            "questions_skipped": int(q.get("skipped") or 0), "questions_errors": int(q.get("errors") or 0),
            "reconciled_closed": int(rc.get("answered_externally") or 0) + int(rc.get("published") or 0),
            "telegram_cards_sent": cards, "last_card_at": finished.isoformat() if cards else None,
            "pending_count": pending,
            "status": "failed" if error else ("errors" if review_errors or q.get("errors") else "ok"),
            "error_class": error,
        })
    except Exception as exc:  # noqa: BLE001
        log_event(logger, "warning", "poll health not recorded", error=type(exc).__name__)


def _hours(settings) -> tuple[int, int]:
    try:
        first, last = (int(x) for x in str(getattr(settings, "wb_poll_hours", "8-23")).split("-"))
        return first, last
    except ValueError:
        return 8, 23


def next_poll(now: datetime, settings) -> datetime:
    first, last = _hours(settings)
    local = now.astimezone(MSK).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    for _ in range(48):
        if first <= local.hour <= last:
            return local
        local += timedelta(hours=1)
    return local


def _msk(iso: str | None) -> str:
    if not iso:
        return "—"
    return datetime.fromisoformat(iso).astimezone(MSK).strftime("%d.%m %H:%M МСК")


def status_text(deps, now: datetime | None = None) -> str:
    now = now or _now()
    try:
        h = deps.repo.get_poll_health()
    except Exception:  # noqa: BLE001
        return "⚠️ Состояние системы сейчас недоступно. Попробуйте позже."
    if not h:
        return f"ℹ️ EVETIS WB — данных о последнем опросе пока нет.\n\nСледующий опрос: {_msk(next_poll(now, deps.settings).isoformat())}"
    finished = datetime.fromisoformat(h["finished_at"])
    first, last = _hours(deps.settings)
    late = (now - finished) > timedelta(minutes=75) and first <= now.astimezone(MSK).hour <= last \
        and not (now.astimezone(MSK).hour == first and now.astimezone(MSK).minute < 15)
    bad = h.get("status") != "ok"
    head = ("⚠️ EVETIS WB — последний опрос с ошибками" if bad else
            "⚠️ EVETIS WB — опрос давно не выполнялся" if late else "✅ EVETIS WB — система работает")
    lines = [head, "", f"Последний опрос: {_msk(h['finished_at'])} {'⚠️' if bad else '✅'}", "",
             f"Отзывы: получено {h.get('reviews_fetched', 0)}, новых карточек {h.get('reviews_processed', 0)}, "
             f"ошибок {h.get('reviews_errors', 0)}",
             f"Вопросы: получено {h.get('questions_fetched', 0)}, новых карточек {h.get('questions_processed', 0)}, "
             f"ошибок {h.get('questions_errors', 0)}", "",
             f"Последняя карточка: {_msk(h.get('last_card_at'))}",
             f"Следующий опрос: {_msk(next_poll(now, deps.settings).isoformat())}",
             f"Ожидают решения: {h['pending_count'] if h.get('pending_count') is not None else '—'}",
             f"Ревизия: {h.get('revision') or '—'}"]
    if h.get("reconciled_closed"):
        lines.insert(-1, f"Закрыто как отвеченные на WB: {h['reconciled_closed']}")
    return "\n".join(lines)
