"""WP4 — structured classification: intents, situations, sentiment, safety, domains.

Two layers:
1. deterministic signals (policy lexicons, clause-level negation for safety) — always on,
   the only layer the golden safety tests rely on;
2. optional LLM structured classification — can ADD situations/intents/safety events and
   raise risk; it can never remove a deterministic signal or lower risk.

Stars are recorded but never used as sentiment for strategy or safety.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from app.v3 import safety as safety_mod
from app.v3.snapshot import KnowledgeSnapshot
from app.v3.text import clauses, normalize, search_any

CLASSIFIER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "intents": {"type": "array", "items": {"type": "string", "enum": [
            "ask_product_info", "ask_usage", "ask_suitability", "ask_availability", "report_experience",
            "praise", "complain", "request_action", "accuse", "unclear"]}},
        "situations": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                       "properties": {"code": {"type": "string"}, "evidence_span": {"type": "string"},
                                      "negated": {"type": "boolean"}},
                       "required": ["code", "evidence_span", "negated"]}},
        "text_sentiment": {"type": "string", "enum": ["positive", "negative", "mixed", "neutral", "none"]},
        "safety_events": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                          "properties": {
                              "type": {"type": "string", "enum": [
                                  "irritation", "redness", "itching", "burning", "rash", "allergy_reported",
                                  "swelling", "nausea", "breathing_problem", "eye_exposure", "ingestion",
                                  "other_adverse_event"]},
                              "evidence_span": {"type": "string"}, "negated": {"type": "boolean"},
                              "temporal_state": {"type": "string", "enum": [
                                  "current", "onset_new", "resolved", "historical_absent", "unknown"]},
                              "certainty": {"type": "string", "enum": ["asserted", "hedged", "uncertain"]},
                              "severity": {"type": "string", "enum": ["mild", "moderate", "severe", "unknown"]},
                              "persistence": {"type": "string", "enum": ["none", "persistent", "unknown"]},
                              "worsening": {"type": "boolean"},
                              "body_location": {"type": "string"},
                              "confidence": {"type": "number"}},
                          "required": ["type", "evidence_span", "negated", "temporal_state", "certainty",
                                       "severity", "persistence", "worsening", "body_location", "confidence"]}},
        "asked_ingredients": {"type": "array", "items": {"type": "string"}},
        "asked_component": {"type": "string"},
        "classification_confidence": {"type": "number"},
    },
    "required": ["intents", "situations", "text_sentiment", "safety_events", "asked_ingredients",
                 "asked_component", "classification_confidence"],
}

CLASSIFIER_SYSTEM = """Ты классификатор обращений покупателей косметики EVETIS на Wildberries.
Верни ТОЛЬКО JSON по схеме. Не придумывай ответ покупателю.
Коды situations (используй только эти): {codes}.
safety_events: любые нежелательные реакции. Отрицание («раздражения не было») -> negated=true,
temporal_state=historical_absent. «Не проходит»/«до сих пор» -> persistence=persistent.
«Было, но прошло» -> temporal_state=resolved. Если сомневаешься — certainty=uncertain, не отрицай.
Звёзды НЕ определяют тональность — только текст. asked_ingredients: ингредиенты, о которых
спрашивает покупатель (как написал покупатель). asked_component: тип средства из набора, если
вопрос про конкретное средство набора (serum|cream|tonic|powder|hand_cream|body_cream), иначе ""."""


@dataclass
class Classification:
    entity_type: str
    rating: Optional[int]
    situations: list = field(default_factory=list)          # [{code, evidence_span, source}]
    intents: list = field(default_factory=list)
    text_sentiment: str = "none"
    is_question: bool = False
    safety: Any = None                                      # SafetyAssessment
    escalation_domains: list = field(default_factory=list)
    ingredient_mentions: list = field(default_factory=list)  # ingredient ids asked about
    unresolved_ingredient_terms: list = field(default_factory=list)
    asked_component: str = ""
    asked_areas: list = field(default_factory=list)
    question_intent: Any = None
    classification_confidence: float = 1.0
    llm_used: bool = False
    llm_error: Optional[str] = None
    llm_usage: dict = field(default_factory=dict)

    @property
    def codes(self) -> set:
        return {s["code"] for s in self.situations}

    def to_dict(self) -> dict:
        d = asdict(self)
        d["safety"] = self.safety.to_dict() if self.safety else None
        return d


def _text_of(msg: dict) -> str:
    parts = [msg.get("text") or ""]
    if msg.get("pros"):
        parts.append("Достоинства: " + msg["pros"])
    if msg.get("cons"):
        parts.append("Недостатки: " + msg["cons"])
    from app.v3.text import customer_tags
    if customer_tags(msg):
        parts.append("Теги покупателя: " + ", ".join(customer_tags(msg)))
    return "\n".join(p for p in parts if p).strip()


def _sentiment(norm: str, policy: dict) -> str:
    sit = policy["situations"]
    pos = bool(search_any(sit["SOCIAL.praise"], norm))
    neg = bool(search_any(sit["SOCIAL.negative"], norm)) or bool(search_any(sit["EXPECTED_RESULT.no_effect"], norm))
    if not norm:
        return "none"
    if pos and neg:
        return "mixed"
    return "positive" if pos else ("negative" if neg else "neutral")


def _domains(codes: set, safety_route) -> list:
    d = []
    if safety_route:
        d.append("SAFETY")
    if any(c.startswith("PACKAGING.") for c in codes):
        d.append("PRODUCT_QUALITY")
    if codes & {"ORDER.wrong_product", "ORDER.incomplete_bundle", "ORDER.return_refund"}:
        d.append("MARKETPLACE_ORDER")
    if "REGULATORY.certification" in codes:
        d.append("REGULATORY")
    if "AUTHENTICITY.counterfeit_accusation" in codes:
        d.append("COUNTERFEIT_ACCUSATION")
    elif "AUTHENTICITY.question" in codes:
        d.append("AUTHENTICITY_QUESTION")
    if "LEGAL.threat" in codes:
        d.append("LEGAL_THREAT")
    return d


NEGATABLE_PREFIXES = ("PACKAGING.", "ORDER.wrong_product", "ORDER.incomplete_bundle")


def _negated_situation(clause: str, start: int, policy: dict) -> bool:
    """Explicit negation right before a packaging/order signal ('без повреждений',
    'ничего не протекло', 'нет вмятин') — same closed cue list as the safety extractor."""
    before = clause[max(0, start - 30): start]
    cfg = policy["safety"]
    return bool(search_any(cfg["negation_cues_before"] + [r"\bне\s*$", r"\bне\s+\w+\s*$"], before))


def classify_rules(msg: dict, snapshot: KnowledgeSnapshot) -> Classification:
    policy = snapshot.policy
    raw = _text_of(msg)
    norm = normalize(raw)
    c = Classification(entity_type=msg.get("entity_type") or "review", rating=msg.get("rating"))
    c.is_question = c.entity_type == "question" or "?" in raw
    for code, pats in policy["situations"].items():
        if code.startswith("SOCIAL."):
            continue
        for clause in clauses(raw):
            m = search_any(pats, clause)
            if m and code.startswith(NEGATABLE_PREFIXES) and _negated_situation(clause, m.start(), policy):
                continue  # «пришёл без повреждений», «не протёк» — not a defect (WP12 FP-1)
            if m:
                c.situations.append({"code": code, "evidence_span": clause[:160], "source": "rules"})
                break
    from app.v3.service_premise import SERVICE_CODES, premises
    c.situations=[s for s in c.situations if s['code'] not in SERVICE_CODES]
    c.situations.extend(premises(raw,policy))
    c.text_sentiment = _sentiment(norm, policy)
    if c.text_sentiment in ("positive", "mixed"):
        c.situations.append({"code": "SOCIAL.praise", "evidence_span": "", "source": "rules"})
    if c.text_sentiment in ("negative", "mixed"):
        c.situations.append({"code": "SOCIAL.negative", "evidence_span": "", "source": "rules"})
    c.safety = safety_mod.assess(raw, policy)
    c.ingredient_mentions = sorted({iid for iid, _ in snapshot.ingredient_mentions(norm)})
    # a fragrance question is not an ingredient question about 'парфюм' unless asked explicitly
    for area, pats in policy["verifier"]["body_areas"].items():
        if search_any(pats, norm):
            c.asked_areas.append(area)
    for comp, pats in policy["product_type_words"].items():
        if comp in ("hand_cream", "body_cream"):
            continue
        if search_any(pats, norm):
            c.asked_component = c.asked_component or comp
    from app.v3.direct_questions import parse_intent
    c.question_intent = parse_intent(raw, is_question=c.is_question)
    if 'intended_use' in c.question_intent.fact_types:
        c.asked_areas = c.question_intent.application_areas
    c.intents = _intents(c)
    c.escalation_domains = _domains(c.codes, c.safety.route)
    return c


def _intents(c: Classification) -> list:
    codes = c.codes
    it = []
    if any(x.startswith("PRODUCT_INFO.") or x.startswith("REGULATORY.") for x in codes):
        it.append("ask_product_info")
    if any(x.startswith("USAGE.") for x in codes):
        it.append("ask_usage")
    if any(x.startswith("SUITABILITY.") for x in codes):
        it.append("ask_suitability")
    if "ORDER.availability" in codes:
        it.append("ask_availability")
    if "SOCIAL.praise" in codes:
        it.append("praise")
    if "SOCIAL.negative" in codes or any(x.startswith("PACKAGING.") for x in codes):
        it.append("complain")
    if "ORDER.return_refund" in codes:
        it.append("request_action")
    if "AUTHENTICITY.counterfeit_accusation" in codes or "TRUST.review_removal" in codes:
        it.append("accuse")
    if not it:
        it.append("report_experience" if not c.is_question else "unclear")
    return it


def classify(msg: dict, snapshot: KnowledgeSnapshot, llm=None) -> Classification:
    """Rules first; then (optionally) merge an LLM structured classification."""
    c = classify_rules(msg, snapshot)
    if llm is None:
        return c
    raw = _text_of(msg)
    if not raw:
        return c
    codes = sorted(snapshot.policy["situations"].keys())
    try:
        out, usage = llm.structured(CLASSIFIER_SYSTEM.format(codes=", ".join(codes)),
                                    json.dumps({"entity_type": c.entity_type, "text": raw}, ensure_ascii=False),
                                    "v3_classification", CLASSIFIER_SCHEMA)
        c.llm_used, c.llm_usage = True, usage or {}
    except Exception as exc:  # noqa: BLE001 — classifier LLM failure keeps the rules result
        c.llm_error = type(exc).__name__
        return c
    _merge_llm(c, out or {}, raw, snapshot)
    return c


def _merge_llm(c: Classification, out: dict, raw: str, snapshot: KnowledgeSnapshot) -> None:
    policy = snapshot.policy
    known = set(policy["situations"].keys())
    have = c.codes
    for s in out.get("situations") or []:
        code = s.get("code")
        if code in known and code not in have and not s.get("negated"):
            c.situations.append({"code": code, "evidence_span": str(s.get("evidence_span") or "")[:160],
                                 "source": "llm"})
    for i in out.get("intents") or []:
        if i not in c.intents:
            c.intents.append(i)
    # text sentiment: rules and LLM disagree -> 'mixed' (never silently positive)
    ls = out.get("text_sentiment")
    if ls and ls != c.text_sentiment and c.text_sentiment != "none":
        if {ls, c.text_sentiment} & {"negative"}:
            c.text_sentiment = "mixed" if "positive" in (ls, c.text_sentiment) else "negative"
    elif c.text_sentiment in ("none", "neutral") and ls in ("positive", "negative", "mixed"):
        c.text_sentiment = ls
    c.safety = safety_mod.merge_llm_events(c.safety, out.get("safety_events") or [], raw, policy)
    for term in out.get("asked_ingredients") or []:
        ids = {iid for iid, _ in snapshot.ingredient_mentions(normalize(term))}
        if ids:
            c.ingredient_mentions = sorted(set(c.ingredient_mentions) | ids)
        elif term and normalize(term) not in ("", "-"):
            c.unresolved_ingredient_terms.append(term[:60])
    comp = out.get("asked_component") or ""
    if comp and not c.asked_component:
        c.asked_component = comp
    try:
        c.classification_confidence = float(out.get("classification_confidence", 1.0))
    except (TypeError, ValueError):
        c.classification_confidence = 0.5
    c.intents = sorted(set(c.intents) | set(_intents(c)))
    c.escalation_domains = _domains(c.codes, c.safety.route)
