"""R1 zero-write pilot gate for the Phase 3.1E quality shadow.

The 3.1E layer may evaluate a communication only inside ONE explicit activation:
flag on, activation id, UTC window [start, end), global cap, operator surfaces off.
Eligibility is fail-closed: no trustworthy timestamps = skipped, never "maybe new".
A claim is taken transactionally BEFORE any LLM call, keyed by activation and
communication only, so poll retries, restarts, overlapping instances and engine,
prompt, policy or snapshot version changes cannot evaluate one communication twice
or push the pilot past its cap. The pilot record holds hashes and verdicts only;
texts stay where the existing shadow ledger already keeps them.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime

PILOT_COLLECTION = "v31_shadow_pilot"
# First real cohort ceiling. Raising it is a code change behind a new owner ACK.
PILOT_HARD_MAX = 20

DISABLED = "SHADOW_DISABLED"
ACTIVATION_INVALID = "SHADOW_ACTIVATION_INVALID"
OPERATOR_SURFACES_ON = "SHADOW_OPERATOR_SURFACES_ENABLED"
UNKNOWN_TIME = "SHADOW_ELIGIBILITY_UNKNOWN_TIME"
BEFORE_ACTIVATION = "SHADOW_BEFORE_ACTIVATION"
WINDOW_CLOSED = "SHADOW_PILOT_WINDOW_CLOSED"
ALREADY_EVALUATED = "SHADOW_ALREADY_EVALUATED"
CAP_REACHED = "PILOT_CAP_REACHED"
CLAIM_ERROR = "SHADOW_CLAIM_ERROR"
CLAIMED = "CLAIMED"


@dataclass(frozen=True)
class Activation:
    activation_id: str
    start_at: datetime
    end_at: datetime
    max_communications: int


def parse_utc(value) -> datetime | None:
    """Timezone-aware instant or None. A naive timestamp is not trustworthy."""
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str) and value.strip():
        try:
            moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return moment if moment.tzinfo is not None and moment.utcoffset() is not None else None


def activation(settings) -> tuple[Activation | None, str | None]:
    """The single valid activation, or the reason the 3.1E layer must stay off."""
    if not getattr(settings, "v31_quality_shadow_enabled", False):
        return None, DISABLED
    if (getattr(settings, "v31_operator_recovery_enabled", False)
            or getattr(settings, "v31_owner_override_enabled", False)):
        return None, OPERATOR_SURFACES_ON
    activation_id = str(getattr(settings, "v31_shadow_activation_id", "") or "")
    start = parse_utc(getattr(settings, "v31_shadow_start_at", ""))
    end = parse_utc(getattr(settings, "v31_shadow_end_at", ""))
    try:
        cap = int(str(getattr(settings, "v31_shadow_pilot_max_communications", "")).strip())
    except ValueError:
        cap = 0
    if (not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", activation_id) or start is None or end is None
            or end <= start or not 1 <= cap <= PILOT_HARD_MAX):
        return None, ACTIVATION_INVALID
    return Activation(activation_id, start, end, cap), None


def eligibility(doc: dict, act: Activation, now: datetime) -> str | None:
    """None when this communication may enter the pilot.

    Both the marketplace creation time and the service's first observation must
    be inside the window: an old review fetched late is still historical.
    """
    created = parse_utc(doc.get("source_created_at"))
    seen = parse_utc(doc.get("first_seen_at"))
    if created is None or seen is None:
        return UNKNOWN_TIME
    if created < act.start_at or seen < act.start_at:
        return BEFORE_ACTIVATION
    if now >= act.end_at or created >= act.end_at:
        return WINDOW_CLOSED
    return None


def source_sha(msg: dict) -> str:
    raw = "\x1f".join(str(msg.get(k) or "") for k in ("text", "pros", "cons"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def counter_id(activation_id: str) -> str:
    return "A." + activation_id


def claim_id(activation_id: str, communication_id: str) -> str:
    return "C." + activation_id + "." + hashlib.sha1(communication_id.encode("utf-8")).hexdigest()
