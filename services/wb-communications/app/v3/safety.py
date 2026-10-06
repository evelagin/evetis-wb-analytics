"""WP4 — deterministic safety-event extraction and routing (ODR-03 / ODR-04).

Clause-level, lexicon + cue based. Conservative by construction:
* an event is ACTIVE unless explicitly negated / historical-absent / resolved;
* uncertainty ('не могу сказать, что не было', 'кажется') keeps the event active;
* R4 is assigned ONLY with an emergency marker (EM-01..EM-06); persistence or severity alone
  is never an emergency;
* stars and sentiment are not inputs here at all.
The LLM classifier may add events or raise risk later (``merge_llm_events``); it can never
remove a deterministic event or lower its route.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional
import re

from app.v3.text import clauses, normalize, search_any

ACTIVE_STATES = {"current", "onset_new", "unknown"}
SKIN_TYPES = {"irritation", "redness", "itching", "burning", "rash", "allergy_reported", "other_adverse_event"}
SPECIAL_TYPES = {"eye_exposure", "ingestion", "nausea", "breathing_problem", "swelling"}


@dataclass
class SafetyEvent:
    type: str
    evidence_span: str
    negated: bool = False
    temporal_state: str = "unknown"      # current | onset_new | resolved | historical_absent | unknown
    certainty: str = "asserted"          # asserted | hedged | uncertain
    severity: str = "unknown"            # mild | moderate | severe | unknown
    persistence: str = "unknown"         # none | persistent | unknown
    worsening: bool = False
    body_location: str = "unknown"
    emergency_markers: list = field(default_factory=list)
    confidence: float = 0.9
    source: str = "rules"                # rules | llm
    cutaneous_injury: bool = False       # structural injury, not mild stinging

    @property
    def active(self) -> bool:
        return (not self.negated) and self.temporal_state in ACTIVE_STATES

    def to_dict(self) -> dict:
        d = asdict(self)
        d["active"] = self.active
        return d


@dataclass
class SafetyAssessment:
    events: list = field(default_factory=list)
    emergency_markers: list = field(default_factory=list)
    risk_level: str = "R0"               # R0 | R3 | R4
    route: Optional[str] = None          # SAFETY_URGENT | S1 | S1R | S2 | HUMAN_REVIEW | None
    reasons: list = field(default_factory=list)

    @property
    def has_safety(self) -> bool:
        return self.route is not None

    def to_dict(self) -> dict:
        return {"events": [e.to_dict() for e in self.events], "emergency_markers": self.emergency_markers,
                "risk_level": self.risk_level, "route": self.route, "reasons": self.reasons}


def _clause_state(clause: str, sym_start: int, cfg: dict) -> tuple[bool, str, str]:
    """(negated, temporal_state, certainty) for a symptom found at sym_start in clause."""
    before, after = clause[:sym_start], clause[sym_start:]
    certainty = "asserted"
    if search_any(cfg["uncertainty_cues"], clause):
        certainty = "uncertain"
    negated = bool(search_any(cfg["negation_cues_before"], before[-40:])) or \
        bool(search_any(cfg["negation_cues_after"], after))
    if certainty == "uncertain":
        negated = False                 # double negative / hedging -> keep active (conservative)
    if negated:
        # "раньше не было, а сегодня появилось" handled by clause split; inside one clause an
        # explicit onset cue wins over the negation of the past.
        if search_any(cfg["onset_new_cues"], clause) and "раньше" in before:
            return False, "onset_new", certainty
        return True, "historical_absent", certainty
    if search_any(cfg["persistent_cues"], clause):
        return False, "current", certainty
    if search_any(cfg["resolved_cues"], clause):
        return False, "resolved", certainty
    if search_any(cfg["onset_new_cues"], clause):
        return False, "onset_new", certainty
    return False, "current" if certainty == "asserted" else "unknown", certainty


def _location(clause: str, cfg: dict) -> str:
    for loc, pats in cfg["body_location"].items():
        if search_any(pats, clause):
            return loc
    return "unknown"


def extract_events(text: str, policy: dict) -> list[SafetyEvent]:
    cfg = policy["safety"]
    events: list[SafetyEvent] = []
    cls = clauses(text)
    for idx, clause in enumerate(cls):
        for etype, pats in cfg["symptoms"].items():
            m = search_any(pats, clause)
            if not m:
                continue
            negated, temporal, certainty = _clause_state(clause, m.start(), cfg)
            # an explicit resolution in the NEXT clause ('сначала было, сейчас прошло')
            if not negated and temporal in ("current", "unknown") and idx + 1 < len(cls):
                nxt = cls[idx + 1]
                if search_any(cfg["resolved_cues"], nxt) and not search_any(cfg["persistent_cues"], nxt) \
                        and not search_any(cfg["symptoms"][etype], nxt):
                    temporal = "resolved"
            # onset in a following clause of a negated past ('раньше не было, сегодня появилось')
            if negated and idx + 1 < len(cls) and search_any(cfg["onset_new_cues"], cls[idx + 1]):
                negated, temporal = False, "onset_new"
            ev = SafetyEvent(type=etype, evidence_span=clause[max(0, m.start() - 30): m.end() + 40].strip(),
                             negated=negated, temporal_state=temporal, certainty=certainty,
                             body_location=_location(clause, cfg))
            ev.persistence = "persistent" if search_any(cfg["persistent_cues"], clause) else "none"
            ev.worsening = bool(search_any(cfg["worsening_cues"], clause))
            if search_any(cfg["severe_cues"], clause):
                ev.severity = "severe"
            elif search_any(cfg["mild_cues"], clause):
                ev.severity = "mild"
            events.append(ev)
    events.extend(_cutaneous_events(text, cfg))
    return _dedupe(events)


def _cutaneous_events(text, cfg):
    """Compose cutaneous context with lesion/injury or marked pain.

    No change to the closed emergency marker list. Ordinary burning remains
    the existing S1/S2 assessment; structural injury defaults to a human.
    """
    result=[]
    whole=normalize(text)
    context=bool(re.search(r'кож\w*|лиц\w*|рук\w*|тел\w*|после\s+(?:нанес\w*|сыворот\w*|крем\w*|тоник\w*)',whole))
    for clause in clauses(text):
        lesion=re.search(r'\b(?:пузыр(?:и|ь|ей|ями|ях|я)|волдыр\w*|blister(?:-like)?\s+lesions?)\b',clause)
        burn=re.search(r'\bожог\w*|\bобожж\w*|мокнущ\w*\s+(?:поврежден\w*|ран\w*)',clause)
        pain=re.search(r'(?:сильн\w*|резк\w*|выраженн\w*)\s+бол\w*|очень\s+бол\w*',clause)
        if not context or not (lesion or burn or pain):continue
        hit=lesion or burn or pain
        negated,temporal,certainty=_clause_state(clause,hit.start(),cfg)
        if burn and re.search(r'словно|как\s+(?:после|будто)',clause):certainty='uncertain'
        result.append(SafetyEvent('other_adverse_event',clause,negated=negated,
            temporal_state=temporal,certainty=certainty,severity='severe',
            body_location=_location(clause,cfg),cutaneous_injury=bool(lesion or burn)))
    return result


def _dedupe(events: list[SafetyEvent]) -> list[SafetyEvent]:
    """One event per type: an active mention wins over a negated/resolved one."""
    best: dict[str, SafetyEvent] = {}
    rank = {True: 2, False: 1}
    for e in events:
        cur = best.get(e.type)
        if cur is None or rank[e.active] > rank[cur.active] or \
                (e.active == cur.active and (e.persistence == "persistent" or e.worsening or e.severity == "severe")):
            best[e.type] = e
    return list(best.values())


def emergency_markers(text: str, events: list[SafetyEvent], policy: dict) -> list[str]:
    cfg = policy["safety"]["emergency_markers"]
    active_types = {e.type for e in events if e.active}
    found = []
    cls = clauses(text)
    whole = normalize(text)
    for em_id, spec in cfg.items():
        req = spec.get("requires_event")
        if req and req not in active_types:
            continue
        hit = False
        for clause in cls:
            if spec.get("any") and search_any(spec["any"], clause):
                hit = not _negated_marker(clause, policy)
            groups = spec.get("all_in_clause")
            if not hit and groups and all(search_any(g, clause) for g in groups):
                hit = not _negated_marker(clause, policy)
            if hit:
                break
        if not hit and req and spec.get("any") and search_any(spec["any"], whole):
            hit = True  # requires_event markers may sit in another clause (e.g. child + ingestion)
        if hit:
            found.append(em_id)
    return found


def _negated_marker(clause: str, policy: dict) -> bool:
    cfg = policy["safety"]
    return bool(search_any([r"\bне\s+было\b", r"\bнет\b", r"\bбез\b", r"\bникак(ого|ой|их)\b",
                            r"\bобош\w*\s+без"], clause)) and not search_any(cfg["uncertainty_cues"], clause)


def route(events: list[SafetyEvent], markers: list[str], policy: dict) -> SafetyAssessment:
    """ODR-03/ODR-04 routing. R4 requires an emergency marker."""
    a = SafetyAssessment(events=events, emergency_markers=markers)
    active = [e for e in events if e.active]
    resolved = [e for e in events if not e.negated and e.temporal_state == "resolved"]
    threshold = float(policy["safety"].get("low_confidence_threshold", 0.7))
    if markers and (active or any(m in ("EM-03", "EM-04") for m in markers)):
        a.risk_level, a.route = "R4", "SAFETY_URGENT"
        a.reasons.append("EMERGENCY_MARKER:" + ",".join(markers))
        return a
    if active:
        a.risk_level = "R3"
        if any(e.cutaneous_injury for e in active):
            a.route='HUMAN_REVIEW'
            a.reasons.append('SERIOUS_CUTANEOUS_INJURY')
            return a
        types = {e.type for e in active}
        if types & {"eye_exposure", "ingestion", "nausea", "breathing_problem", "swelling"}:
            # no approved customer wording for these (S3-S6 pending) -> human
            a.route = "HUMAN_REVIEW"
            a.reasons.append("SAFETY_EVENT_WITHOUT_APPROVED_TEMPLATE:" + ",".join(sorted(types)))
            return a
        if any(e.certainty != "asserted" for e in active):
            a.route = "HUMAN_REVIEW"
            a.reasons.append("SAFETY_EVENT_UNCERTAIN")
            return a
        escalate = any(e.persistence == "persistent" or e.worsening or e.severity == "severe" for e in active)
        low_conf = any(e.confidence < threshold for e in active)
        a.route = "S2" if (escalate or low_conf) else "S1"
        a.reasons.append("ACTIVE_SKIN_EVENT" + ("_PERSISTENT_OR_SEVERE" if escalate else "")
                         + ("_LOW_CONFIDENCE" if low_conf else ""))
        return a
    if resolved:
        a.risk_level, a.route = "R3", "S1R"
        a.reasons.append("RESOLVED_EVENT")
        if any(e.type in SPECIAL_TYPES for e in resolved):
            a.route = "HUMAN_REVIEW"
            a.reasons.append("RESOLVED_SPECIAL_EVENT")
    return a


def assess(text: str, policy: dict) -> SafetyAssessment:
    events = extract_events(text, policy)
    return route(events, emergency_markers(text, events, policy), policy)


def merge_llm_events(assessment: SafetyAssessment, llm_events: list[dict], text: str,
                     policy: dict) -> SafetyAssessment:
    """Union with LLM-extracted events. The LLM can ADD an event or turn a rules event
    ACTIVE; it can never negate/resolve a rules event. Emergency markers are recomputed by
    rules on the merged set (the LLM cannot declare R4 by itself, and cannot suppress it)."""
    by_type = {e.type: e for e in assessment.events}
    for le in llm_events or []:
        et = le.get("type")
        if et not in policy["safety"]["symptoms"]:
            continue
        cand = SafetyEvent(type=et, evidence_span=str(le.get("evidence_span") or "")[:200],
                           negated=bool(le.get("negated")),
                           temporal_state=le.get("temporal_state") or "unknown",
                           certainty=le.get("certainty") or "asserted",
                           severity=le.get("severity") or "unknown",
                           persistence=le.get("persistence") or "unknown",
                           worsening=bool(le.get("worsening")),
                           body_location=le.get("body_location") or "unknown",
                           confidence=float(le.get("confidence") or 0.5), source="llm")
        cur = by_type.get(et)
        if cur is None:
            if cand.active or cand.temporal_state == "resolved":
                by_type[et] = cand
        elif cand.active and not cur.active:
            cur.negated, cur.temporal_state = False, cand.temporal_state
            cur.source = "rules+llm"
        elif cur.active and cand.active:
            cur.worsening = cur.worsening or cand.worsening
            if cand.persistence == "persistent":
                cur.persistence = "persistent"
            if cand.severity == "severe":
                cur.severity = "severe"
    events = list(by_type.values())
    merged = route(events, emergency_markers(text, events, policy), policy)
    order = {"R0": 0, "R3": 1, "R4": 2}
    if order[merged.risk_level] < order[assessment.risk_level]:
        return assessment  # never lower
    return merged
