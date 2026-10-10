"""One decision for every review ingestion path: may this WB review become an ACTIONABLE
Telegram moderation card (a legitimate FIRST seller answer)?

Truth is the direct single-review WB read (GET /api/v1/feedback?id=, the same read the verified
publisher uses), never a list category. Live contract proven in production (09–10.10.2026):

* `isAnswered=true` / the cabinet tab «Есть ответ» is NOT a seller answer. WB auto-processes a
  rating without words (state `wbRu`, answer null, archived at once). Five such reviews received
  a first seller answer from EVETIS: one write each, read-back verified with the exact text.
* A seller answer exists only when `answer.text` is non-empty (`answer.state` = wbRu or
  reviewRequired while WB moderates it; `answer.editable` says whether it can be edited).
* A buyer who rewrites a review creates a NEW review (child, `parentFeedbackId`) that WB lists
  as unanswered and counts in `countUnanswered`; the old one gets `childFeedbackId`. A first
  answer on such a child was accepted by WB on 10.10. The superseded parent is not actionable:
  the current version is the child.
* Review states with a first answer accepted by WB: `none` (fresh text review) and `wbRu`.
  Any other state, a read error, a foreign id or a missing field is AMBIGUOUS → fail closed:
  no card now, a later poll looks again.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.utils.logging import get_logger, log_event

logger = get_logger(__name__)

ACTIONABLE_FIRST_RESPONSE = "ACTIONABLE_FIRST_RESPONSE"
ALREADY_ANSWERED = "ALREADY_ANSWERED"
NON_ACTIONABLE = "NON_ACTIONABLE"
AMBIGUOUS = "AMBIGUOUS"
ANSWERABLE_STATES = frozenset({"none", "wbRu"})
# poll counters per verdict (shared by the normal and the rating-only path and /status)
COUNTER = {ACTIONABLE_FIRST_RESPONSE: "actionable", ALREADY_ANSWERED: "already_answered",
           NON_ACTIONABLE: "non_actionable", AMBIGUOUS: "ambiguous"}


@dataclass(frozen=True)
class Actionability:
    verdict: str
    reason: str
    raw: dict | None = None

    @property
    def actionable(self) -> bool:
        return self.verdict == ACTIONABLE_FIRST_RESPONSE


def seller_answer_text(raw: dict | None) -> str:
    """The seller answer actually present on WB ('' when there is none)."""
    answer = (raw or {}).get("answer")
    return " ".join(str(answer.get("text") or "").split()) if isinstance(answer, dict) else ""


def has_customer_words(raw: dict | None) -> bool:
    raw = raw or {}
    return any(str(raw.get(k) or "").strip() for k in ("text", "pros", "cons")) or bool(raw.get("bables"))


def classify(raw, review_id) -> Actionability:
    """Pure classification of one direct WB review object."""
    if not isinstance(raw, dict) or str(raw.get("id")) != str(review_id):
        return Actionability(AMBIGUOUS, "identity_mismatch")
    if "answer" not in raw:
        return Actionability(AMBIGUOUS, "missing_answer_field", raw)
    if seller_answer_text(raw):
        return Actionability(ALREADY_ANSWERED, "seller_answer_exists", raw)
    if raw.get("answer") is not None:
        return Actionability(AMBIGUOUS, "answer_without_text", raw)
    if raw.get("childFeedbackId"):
        return Actionability(NON_ACTIONABLE, "superseded_by_newer_version", raw)
    if raw.get("state") not in ANSWERABLE_STATES:
        return Actionability(AMBIGUOUS, f"unproven_state:{raw.get('state')}", raw)
    return Actionability(ACTIONABLE_FIRST_RESPONSE, "no_seller_answer", raw)


def read_review(deps, review_id) -> Actionability:
    """Direct authoritative read + classification. Never raises: a read error is AMBIGUOUS."""
    try:
        raw = deps.wb.get_feedback(review_id, retries=1, timeout_seconds=5.0)
    except Exception as exc:  # noqa: BLE001 — fail closed, a later poll looks again
        log_event(logger, "warning", "wb review direct read failed", error=type(exc).__name__)
        return Actionability(AMBIGUOUS, "read_error")
    return classify(raw, review_id)
