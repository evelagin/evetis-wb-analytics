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
    # 3.1F: LLM-only safety findings without a customer-reported symptom — operator diagnostic,
    # never a route («POSSIBLE_SAFETY_CONTEXT»).
    possible_context: list = field(default_factory=list)

    @property
    def has_safety(self) -> bool:
        return self.route is not None

    def to_dict(self) -> dict:
        return {"events": [e.to_dict() for e in self.events], "emergency_markers": self.emergency_markers,
                "risk_level": self.risk_level, "route": self.route, "reasons": self.reasons,
                "possible_context": self.possible_context}


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


# --- 3.1F: what a symptom WORD is doing in the customer's sentence ---------------------------
# A skin-symptom word is a safety event only when the customer reports it as something that
# HAPPENED to them. Live replay (10.10, 4 363 real WB reviews): 221 safety routes, the large
# majority praise — «убирает покраснения», «стало меньше высыпаний», «аллергии не вызывает»,
# «для аллергика», and «высыпаю горстку пудры на руки» (to pour). Serious/special types
# (breathing, swelling, eye, ingestion, nausea) and every emergency marker are NOT touched.
ROLE_ADVERSE, ROLE_ABSENT, ROLE_IMPROVED, ROLE_PRIOR, ROLE_NOT_SYMPTOM = (
    "adverse", "absent", "improved", "prior_condition", "not_a_symptom")
_ROLE_TYPES = SKIN_TYPES
_WIN = 45
# the reaction happened / got worse (a verb of onset or worsening RIGHT NEXT to the symptom)
_ONSET = (r"появил\w*|\bпош(?:ел|ла|ли|ло)\b|начал(?:ся|ась|ись|ось|о|а|и)?\b|высыпал(?:о|и)\b|обсыпал\w*|вылез\w*"
          r"|покраснел\w*|покрасне(?:ет|ют)|сожгл\w*|сожж\w*|воспалил\w*|зачесал\w*|ухудш\w*|\bстал\w*\s+(?:больше|хуже)"
          r"|ещ[её]\s+больше|\bхуже\b|\bвызвал\w*|\bвызыва(?:ет|ют)\b")
# the customer attributes the condition to a product («от него», «после крема»): blocks «prior condition» only
_ATTRIB = (r"\bот\s+(?:него|не[её]|этого|этой|крема|сыворотк\w*|тоник\w*|пудр\w*|средств\w*)\b"
           r"|\bпосле\s+(?:него|не[её]|нанесени\w*|применени\w*|использовани\w*|крема|сыворотк\w*|тоник\w*|пудр\w*|умывани\w*)")
_ONSET_WIN = 22
# the product reduced / removed / prevents a condition (a benefit, not an adverse event)
_IMPROVED = (r"убира|убрал|\bубер|избав|уменьш|\bменьше|\bменее\b|снима|\bснял|успока|подсуш|справля|\bбор(?:ет|ется|ьб|оть)"
             r"|побор|помога|\bпомог\b|помоч|\bпротив\b|предотвращ|профилакт|\bреже\b|осветл|высветл|бледн|сглаж|выравн|устраня"
             r"|\bспас|решил|сократ|подсыха|увлажня|\bот\s+(?:высыпан|покраснен|раздражен|шелушен|прыщ|пят|акне|аллерги)\w*"
             r"|(?:средств|уход)\w*\s+от")
# «прошли / ушли / исчезли» and «были» mean improvement / history for a skin CONDITION the product
# targets (rash, redness, peeling, spots) — but a resolved REACTION for a sensation (burning,
# itching, irritation, allergy): «жжение прошло» stays S1R.
_CONDITION_TYPES = {"rash", "redness", "other_adverse_event"}
_RESOLVED_CONDITION = (r"\bушл[аио]?\b|\bуш[её]л\b|\bпрошл[аио]?\b|\bпрош[её]л\b|исчез|пропал|не\s+стало|\bуход(?:ят|ит|ить)\b"
                       r"|\bпроход(?:ят|ит|ить)\b")
# the customer describes their skin / history, not a reaction to this product
_PRIOR = (r"аллергик|аллерген|склонн\w*\s+к|проблемн\w*|чувствительн\w*|постоянн\w*|периодическ\w*|регулярн\w*|\bредко\b"
          r"|\b(?:с|при)\s+(?:\w+\s+)?(?:аллерги|высыпан|раздражен|покраснен|шелушен|постакне|акне)\w*|\bбывает\b|\bчасто\b|\bвечно\b"
          r"|холодов|после\s+(?:родов|бритья|улицы|солнца|мороза)|\bпостакне|пост\s+акне|пигмент|\bот\s+пост|когда\s+\w*\s*(?:обсыпа|высыпа)"
          r"|\bу\s+меня\s+(?:аллерги|чувствит|проблемн)|аллерги\w*\s+на\s+(?:вс|многое|цитрус|пыльц|кошек|холод)|\bгде\s+есть"
          r"|други\w*\s+(?:крем|средств|сыворот|тоник|пудр)")
_PRIOR_CONDITION = r"\bбыл[аио]?\b"
# an explicit negation that follows or precedes the symptom word in the same clause
_ABSENT = (r"\bне\s+(?:вызыв|вызвал|успева|наблюда|замеч|замет|появля|появил|был|буд|оставля|раздража|щип|жж|чеш|обсып|высып|сушит"
           r"|красне|давал|дает|дал|подчеркива)\w*|\bни\s+(?:одного|единого|малейш|следа)|\bне\s+единого|\bнет\b|\bбез\b"
           r"|\bникак\w*|\bотсутств\w*|\bне\s+было")
# words that only look like a symptom
_NOT_SYMPTOM = (r"высыпа(?:ю|ешь|ет|ем|ете|ть)\b(?!ся)|высыплю|высыпь|высыпала?\s+(?:в|на|из|горст|пудр|порош|немного|чуть)"
                r"|высыпа\w*(?:ся|сь)\b|\bреакци\w*\s+(?:в\s*виде\s+)?(?:приятн|ввиде\s+пощип)|приятн\w*\s+реакци"
                r"|\bвидео\b[^.!?]{0,40}реакци|\bпятн\w*\s+(?:от\s+)?(?:пост|акне|пигмент)|пигментн\w*\s+пятн"
                r"|раздражающ\w*\s*,?\s*(?:запах|аромат|смесь)|(?:запах|аромат)\w*[^.!?]{0,25}раздражающ"
                r"|раздража\w*\s*,?\s*(?:что|когда|то\b|это)|красн\w*\s+зв[её]здочк")
# a habitual present «появляются» qualified by «реже / меньше» is an improvement, not an onset
_HABITUAL_BETTER = r"появля\w*[^.!?]{0,25}(?:реже|меньше|не\s+так)|(?:реже|меньше)[^.!?]{0,25}появля"


def _improved(near: str, pattern: str = _IMPROVED) -> bool:
    """A benefit cue that is not negated («не помог», «не убирает», «не справляется»)."""
    return any(not re.search(r"\bне\s+$", near[max(0, m.start() - 4): m.start()])
               for m in re.finditer(pattern, near))


def _onset(near: str) -> bool:
    """An onset/worsening verb that is not itself negated («не появилось», «не вызывает») and
    is not about starting to USE the product («начала пользоваться»)."""
    for m in re.finditer(_ONSET, near):
        if re.search(r"\bне\s+$", near[max(0, m.start() - 4): m.start()]):
            continue
        if m.group(0).startswith("начал") and re.search(r"пользов|использ|применя|нанос", near[max(0, m.start() - 20): m.end() + 20]):
            continue
        return True
    return False


def symptom_role(etype: str, clause: str, start: int, end: int) -> str:
    """adverse | absent | improved | prior_condition | not_a_symptom for one symptom mention."""
    if etype not in _ROLE_TYPES:
        return ROLE_ADVERSE
    near = clause[max(0, start - _WIN): end + _WIN]
    if re.search(_NOT_SYMPTOM, clause[max(0, start - 30): end + 30]):
        return ROLE_NOT_SYMPTOM
    onset = _onset(clause[max(0, start - _ONSET_WIN): end + _ONSET_WIN])
    if etype == "other_adverse_event" and re.search(r"\bпятн", clause[start:end]) and not onset:
        return ROLE_NOT_SYMPTOM        # «пятна» without onset = pigmentation the product targets
    if re.search(_HABITUAL_BETTER, near):
        return ROLE_IMPROVED
    if re.search(_ABSENT, clause[max(0, start - 30): end + 50]) and not onset:
        return ROLE_ABSENT
    condition = etype in _CONDITION_TYPES
    if (_improved(near) or (condition and _improved(near, _RESOLVED_CONDITION))) and not onset:
        return ROLE_IMPROVED
    if (re.search(_PRIOR, near) or (condition and re.search(_PRIOR_CONDITION, near))) \
            and not onset and not re.search(_ATTRIB, near):
        return ROLE_PRIOR
    return ROLE_ADVERSE


def context_mentions(text: str, policy: dict) -> list[dict]:
    """Symptom words that are NOT reactions (improvement, prior condition, negation, look-alike)."""
    out = []
    for clause in clauses(text):
        for etype, pats in policy["safety"]["symptoms"].items():
            m = search_any(pats, clause)
            if m:
                role = symptom_role(etype, clause, m.start(), m.end())
                if role != ROLE_ADVERSE:
                    out.append({"type": etype, "role": role, "evidence_span": clause[max(0, m.start() - 30): m.end() + 40].strip()})
    return out


def extract_events(text: str, policy: dict) -> list[SafetyEvent]:
    cfg = policy["safety"]
    events: list[SafetyEvent] = []
    cls = clauses(text)
    for idx, clause in enumerate(cls):
        for etype, pats in cfg["symptoms"].items():
            m = search_any(pats, clause)
            if not m:
                continue
            role = symptom_role(etype, clause, m.start(), m.end())
            if role == ROLE_ABSENT and search_any(cfg["uncertainty_cues"], clause):
                role = ROLE_ADVERSE      # «не могу сказать, что не было» stays conservative (SAF-05)
            if role in (ROLE_IMPROVED, ROLE_PRIOR, ROLE_NOT_SYMPTOM):
                continue                 # praise / profile / look-alike: never a safety event
            negated, temporal, certainty = _clause_state(clause, m.start(), cfg)
            if role == ROLE_ABSENT:
                negated, temporal = True, "historical_absent"
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
        pain=re.search(r'(?:сильн\w*|резк\w*|выраженн\w*|очень)\s+бол(?:ь|и|ью|ит|ят|ело|ела|ели|езненн\w*|ьн\w*)\b',clause)
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
    """Union with LLM-extracted events. The LLM can never negate/resolve a rules event, and
    emergency markers are recomputed by rules on the merged set (the LLM cannot declare R4 by
    itself, and cannot suppress it).

    3.1F calibration: a SKIN event the rules did not find as an actual reaction (LLM-only, or a
    rules mention that is negated / an improvement / a prior condition) never routes S1/S1R/S2
    — it is kept as POSSIBLE_SAFETY_CONTEXT for the operator. A special event (breathing,
    swelling, eye, ingestion, nausea) from the LLM still routes to a human unless its evidence is
    only an exposure/avoidance statement («стараюсь не вдыхать», «пудра пылит»)."""
    by_type = {e.type: e for e in assessment.events}
    possible = list(assessment.possible_context)
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
        speculative = (et in SKIN_TYPES and not actual_skin_evidence(text, cand.evidence_span)) \
            or exposure_only(cand.evidence_span)
        if speculative and (cur is None or not cur.active) and (cand.active or cand.temporal_state == "resolved"):
            possible.append({"type": et, "evidence_span": cand.evidence_span, "source": "llm",
                             "reason": "NO_CUSTOMER_REPORTED_SYMPTOM" if et in SKIN_TYPES else "EXPOSURE_ONLY"})
            continue
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
    result = assessment if order[merged.risk_level] < order[assessment.risk_level] else merged  # never lower
    result.possible_context = possible
    return result


# Potential exposure / avoidance is not an adverse event: «стараюсь не вдыхать», «пудра пылит»,
# «частички летят в воздухе» — unless an actual symptom is named in the same evidence.
_EXPOSURE = (r"\bне\s+вдых|вдыхать|вдыхая|\bпыл(?:ит|ится|ь|ью)\b|распыл\w*|частич\w*|разлета\w*|\bлет(?:ят|ит|ают)\b"
             r"|в\s+воздух\w*|облак\w*\s+пудр")
_ACTUAL_SYMPTOM = (r"трудно|тяжело|задыха|кашл|першит|перш|\bчих|удушь|одышк|сдавил|отек|отёк|опух|распух|слез|жж|щип|резь"
                   r"|\bрежет|тошн|рвот|плохо\s+стало|стало\s+плохо|голов\w*\s+кру")


# A sensation the customer reports in their own words (wider than the symptom lexicon, so the
# LLM may still catch «кожа горит» or «плохо стало с кожей» that the rules missed).
_SENSATION = (r"гор(?:ит|ела|ел|ели|еть)|\bжж|жж[её]т|\bжгл|щип|\bзуд|чеш|красн|сып|прыщ|раздраж|аллерг|\bпятн|шелуш"
              r"|воспал|\bпеч(?:ет|ет|ь|ло)|плохо\s+стало|стало\s+плохо|стянул|стягива|покалыва|колет")


def actual_skin_evidence(text: str, evidence: str) -> bool:
    """True when a customer-worded sensation is reported as something that HAPPENED (not praise,
    not a prior condition, not a negation) in the LLM evidence span or, without one, the text."""
    for source in (evidence, "" if evidence and normalize(evidence) in normalize(text) else text):
        for clause in clauses(source or ""):
            for m in re.finditer(_SENSATION, clause):
                if symptom_role("other_adverse_event", clause, m.start(), m.end()) == ROLE_ADVERSE \
                        and not re.search(_ABSENT, clause[max(0, m.start() - 30): m.end() + 50]):
                    return True
    return False


def exposure_only(evidence: str) -> bool:
    n = normalize(evidence or "")
    return bool(re.search(_EXPOSURE, n)) and not re.search(_ACTUAL_SYMPTOM, n)


# --- R2.1 operator assist: split the HUMAN_REVIEW safety class -------------------------------
# A fragrance-attributed sensation (dizziness, headache, nausea) with no serious marker is
# MODERATE: the operator gets a cautious draft and an attention badge, publication stays manual.
# Everything else that routes to a human (breathing, swelling, fainting, emergency care, eye,
# ingestion, vomiting, cutaneous injury, children, pregnancy, mixed skin events) is unchanged.
MODERATE_DISCOMFORT = "MODERATE_DISCOMFORT"
_FRAGRANCE = r"запах|аромат|отдушк|пахн|\bпах\b|воня|вонь|парфюм"
_SENSATIONS = {
    "dizziness": r"голов\w*\s+(?:\w+\s+)?(?:кружит|кружил|закружил)|(?:кружит|кружил|закружил)\w*\s+(?:\w+\s+)?голов|головокруж",
    "headache": (r"голов\w*\s+(?:\w+\s+)?(?:болит|болел|разболел|заболел|раскалыва)"
                 r"|(?:болит|болел|разболел|заболел|раскалыва)\w*\s+(?:\w+\s+)?голов|головн\w*\s+бол\w*|мигрен"),
    "nausea": r"\bтошн|\bмутит\b",
}
_SERIOUS_CONTEXT = (r"\bрвот|\bвырвал|\bстошнил|сознани|\bобморок|\bупал[аи]?\b|давлени|\bскор(?:ую|ая|ой)\b"
                    r"|\bврач\w*\s+(?:вызва|приезжа)|\bреб[её]н|\bдет(?:и|ей|ям|ский|ская)\b|\bмалыш|\bсын\w*|\bдоч\w*"
                    r"|беремен|корм\w*\s+грудь|грудн\w*\s+вскармлив")


def sensations(text: str) -> list[str]:
    n = normalize(text)
    return [k for k, rx in _SENSATIONS.items() if re.search(rx, n)]


def moderate_discomfort(text: str, assessment: "SafetyAssessment") -> list[str] | None:
    """The sensations when this is a MODERATE fragrance discomfort, else None.

    Never lowers SAFETY_URGENT, an emergency marker, a cutaneous injury or any event
    other than nausea; vomiting and vulnerable-person context stay serious."""
    n = normalize(text)
    found = sensations(text)
    if not found or not re.search(_FRAGRANCE, n) or re.search(_SERIOUS_CONTEXT, n):
        return None
    if assessment.emergency_markers or assessment.route == "SAFETY_URGENT":
        return None
    if any(e.cutaneous_injury for e in assessment.events):
        return None
    if any(e.type != "nausea" for e in assessment.events if e.active or e.temporal_state == "resolved"):
        return None
    return found
