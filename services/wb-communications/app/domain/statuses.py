"""Lifecycle statuses, allowed actions, and event types for communications.

A single record moves through these statuses. Transitions are enforced in the
Firestore repository (atomic/transactional) so two Telegram callbacks can never
publish the same answer twice, and a stale button on an already-handled record
is rejected.
"""
from __future__ import annotations

from enum import Enum


class Status(str, Enum):
    # /poll created the record and is generating the draft (leased)
    PROCESSING = "processing"
    # draft is ready and card is shown in Telegram, waiting for a human tap
    PENDING_APPROVAL = "pending_approval"
    # human tapped "publish" — we hold this lease while calling WB
    PUBLISHING = "publishing"
    # human tapped "regenerate" — OpenAI in-flight, record locked with a token
    REGENERATING = "regenerating"
    # human tapped "edit" — waiting for the reply, record locked with a token
    EDITING = "editing"
    # Both entities: a matching answer has been read back from WB.
    PUBLISHED = "published"
    # Question: WB accepted the write (2xx, error=false) but the answer is not
    # visible yet (WB pre-moderates answers). Re-verified on later polls; NEVER
    # reported to the operator as "published".
    PUBLISH_ACCEPTED = "publish_accepted"
    # The write outcome is unknown (timeout / 5xx after send / accepted but still
    # not visible after the verification window). No automatic re-send: the
    # operator re-taps «Опубликовать», which reads WB state BEFORE writing.
    PUBLISH_UNKNOWN = "publish_unknown"
    # Question already carries an answer on WB with a DIFFERENT text (e.g. typed
    # in the WB cabinet). Our text was not sent.
    ANSWERED_EXTERNALLY = "answered_externally"
    POLICY_BLOCKED = "policy_blocked"
    POLICY_CHECK_FAILED = "policy_check_failed"
    # WB rejected / errored — safe to retry
    PUBLISH_FAILED = "publish_failed"
    # human tapped "skip"
    SKIPPED = "skipped"
    # generation or send failed in /poll — safe to retry on next poll
    ERROR = "error"


# Statuses that mean "already handled or in-flight" — /poll must NOT resend.
HANDLED_STATUSES = frozenset(
    {
        Status.PROCESSING,
        Status.PENDING_APPROVAL,
        Status.PUBLISHING,
        Status.PUBLISHED,
        Status.PUBLISH_ACCEPTED,
        Status.PUBLISH_UNKNOWN,
        Status.ANSWERED_EXTERNALLY,
        Status.SKIPPED,
    }
)

# Statuses from which /poll is allowed to retry (regenerate + resend).
RETRIABLE_POLL_STATUSES = frozenset({Status.ERROR})

# Statuses that carry a lease (lock_expires_at) and can be reclaimed on crash.
LEASED_STATUSES = frozenset(
    {Status.PROCESSING, Status.PUBLISHING, Status.REGENERATING, Status.EDITING}
)

# Source statuses from which a fresh draft action (edit/regenerate) may START.
DRAFTABLE_FROM = frozenset({Status.PENDING_APPROVAL.value, Status.PUBLISH_FAILED.value,
                          Status.POLICY_BLOCKED.value, Status.POLICY_CHECK_FAILED.value})


# --- Telegram action state machine -----------------------------------------
# Which callback actions are valid from each status. Anything not listed is a
# stale/no-op tap and is rejected transactionally.
ALLOWED_ACTIONS: dict[str, frozenset[str]] = {
    Status.POLICY_BLOCKED.value: frozenset({"publish", "edit", "regenerate", "skip", "show"}),
    Status.POLICY_CHECK_FAILED.value: frozenset({"publish", "edit", "regenerate", "skip", "show"}),
    Status.PENDING_APPROVAL.value: frozenset({"publish", "edit", "regenerate", "skip", "show"}),
    Status.PUBLISH_FAILED.value: frozenset({"publish", "edit", "regenerate", "skip", "show"}),
    Status.PUBLISHING.value: frozenset({"show"}),
    Status.REGENERATING.value: frozenset({"show"}),
    Status.EDITING.value: frozenset({"show"}),
    Status.PUBLISHED.value: frozenset({"show"}),
    Status.PUBLISH_ACCEPTED.value: frozenset({"show"}),
    # publish = "check WB, and write only if still unanswered" (never a blind re-send)
    Status.PUBLISH_UNKNOWN.value: frozenset({"publish", "skip", "show"}),
    Status.ANSWERED_EXTERNALLY.value: frozenset({"show"}),
    Status.SKIPPED.value: frozenset({"show", "restore"}),
    Status.PROCESSING.value: frozenset({"show"}),
    Status.ERROR.value: frozenset({"show"}),
}

# Actions that begin a publish lease.
PUBLISH_ACTIONS = frozenset({"publish"})
# Actions that produce a fresh draft and return the record to pending_approval.
DRAFT_ACTIONS = frozenset({"edit", "regenerate"})


def action_allowed(status: str, action: str) -> bool:
    return action in ALLOWED_ACTIONS.get(status, frozenset())


class EventType(str, Enum):
    FIRST_SEEN = "first_seen"
    AI_GENERATED = "ai_generated"
    SENT_TO_TELEGRAM = "sent_to_telegram"
    MANUALLY_EDITED = "manually_edited"
    REGENERATED = "regenerated"
    PUBLISH_REQUESTED = "publish_requested"
    PUBLISHED = "published"
    PUBLISH_ACCEPTED = "publish_accepted"
    PUBLISH_UNKNOWN = "publish_unknown"
    ANSWERED_EXTERNALLY = "answered_externally"
    SKIPPED = "skipped"
    RESTORED = "restored"
    EDIT_STARTED = "edit_started"            # R2.4A operator identity: who opened an edit
    OVERRIDE_REQUESTED = "override_requested"  # R2.4A: first step of a RED owner decision
    FAILED = "failed"
