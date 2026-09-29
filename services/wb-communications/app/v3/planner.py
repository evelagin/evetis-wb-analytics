"""WP5 — Required-fact planner + policy engine: classification -> required facts ->
knowledge resolution -> response strategy.

The planner, not the generator, decides policy. Strategies:
  FACT_ANSWER · SERVICE · SAFETY_TEMPLATE · ACKNOWLEDGEMENT · CLARIFICATION_REQUIRED ·
  UNKNOWN_FACT · HUMAN_REVIEW
Resolution states per required fact:
  KNOWN_ALLOWED · KNOWN_RESTRICTED (answer only through an approved abstraction) ·
  CONFLICT · UNKNOWN
There is no fallback to model knowledge: anything not resolvable is UNKNOWN / HUMAN_REVIEW.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

from app.v3.classifier import Classification
from app.v3.resolver import ProductResolution
from app.v3.snapshot import KnowledgeSnapshot

CUSTOMER_DISCLOSURE = {"PUBLIC", "PUBLIC_WITH_APPROVED_WORDING"}
FACT_CODES_PREFIX = ("PRODUCT_INFO.", "USAGE.", "SUITABILITY.", "REGULATORY.", "AUTHENTICITY.question")
HUMAN_UNKNOWN_REASONS = {"T1C_NOT_DIGITIZED", "BRAND_SELLER_FACT_MISSING", "LABEL_INCI_UNAVAILABLE",
                         "BUNDLE_COMPONENT_AMBIGUOUS", "UNRESOLVED_INGREDIENT"}
AREA_RU = {"face": "лица", "hands": "рук", "body": "тела"}
TYPE_RU = {"serum": "сыворотка", "cream": "крем для лица", "tonic": "тоник", "powder": "энзимная пудра",
           "hand_cream": "крем для рук", "body_cream": "крем для рук и тела"}


@dataclass
class ResolvedFact:
    fact_type: str
    product_id: Optional[str]
    state: str                                  # KNOWN_ALLOWED | KNOWN_RESTRICTED | CONFLICT | UNKNOWN
    subject: Optional[str] = None
    customer_value_ru: Optional[str] = None     # the ONLY wording the generator may use
    fact_ids: list = field(default_factory=list)
    source_ids: list = field(default_factory=list)
    template_id: Optional[str] = None
    reason: Optional[str] = None
    numbers: list = field(default_factory=list)  # canonical numbers the text may carry
    ingredient_ids: list = field(default_factory=list)


@dataclass
class Plan:
    strategy: str
    risk_level: str = "R0"
    escalation_domains: list = field(default_factory=list)
    required_facts: list = field(default_factory=list)
    resolved: list = field(default_factory=list)
    template_ids: list = field(default_factory=list)
    rendered_templates: list = field(default_factory=list)
    guidance_ids: list = field(default_factory=list)
    failure_code: Optional[str] = None
    reasons: list = field(default_factory=list)
    operator_warnings: list = field(default_factory=list)
    deterministic_text: Optional[str] = None
    tone: str = "neutral"
    product_ids: list = field(default_factory=list)
    buyer_name: Optional[str] = None

    @property
    def allowed(self) -> list:
        return [r for r in self.resolved if r.state == "KNOWN_ALLOWED" or
                (r.state in ("KNOWN_RESTRICTED", "UNKNOWN") and (r.customer_value_ru or r.template_id))]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["allowed_fact_ids"] = sorted({i for r in self.allowed for i in r.fact_ids})
        return d


def _with_name(text: str, name: Optional[str]) -> str:
    if not name:
        return text
    return f"{name}, {text[:1].lower()}{text[1:]}"


def _clean_name(name) -> Optional[str]:
    n = (name or "").strip()
    if not n or len(n.split()[0]) < 2 or len(n) > 30 or any(ch.isdigit() for ch in n) \
            or n.lower() in ("покупатель", "пользователь", "аноним"):
        return None
    return n.split()[0]


def plan(snapshot: KnowledgeSnapshot, res: ProductResolution, c: Classification, msg: dict) -> Plan:
    policy = snapshot.policy
    name = _clean_name(msg.get("buyer_name"))
    codes = c.codes
    p = Plan(strategy="HUMAN_REVIEW", escalation_domains=list(c.escalation_domains),
             risk_level=c.safety.risk_level if c.safety else "R0", buyer_name=name,
             product_ids=[res.product_id] if res.product_id else [])
    s = c.safety

    # 0. product identity gate
    if res.status == "PRODUCT_NOT_VERIFIED":
        return _human(p, "PRODUCT_NOT_VERIFIED", f"resolver: {res.reason}")

    # 1. emergency / special safety -> human, never generated
    if s and s.route in ("SAFETY_URGENT", "HUMAN_REVIEW"):
        p.risk_level = s.risk_level
        return _human(p, "SAFETY_ESCALATION", "; ".join(s.reasons))

    # 2. legal / authenticity / regulatory / accusations -> human (ODR-16 safe default)
    human_domains = {"LEGAL_THREAT", "COUNTERFEIT_ACCUSATION", "AUTHENTICITY_QUESTION", "REGULATORY"}
    hit = sorted(set(c.escalation_domains) & human_domains)
    if hit:
        return _human(p, "HUMAN_REVIEW_DOMAIN", "domains: " + ",".join(hit))
    if "TRUST.review_removal" in codes:
        return _human(p, "HUMAN_REVIEW_DOMAIN", "review removal accusation")

    # 3. non-emergency safety -> owner-approved template, deterministic; wins over service
    if s and s.route in ("S1", "S1R", "S2"):
        tid = {"S1": "T-S1", "S1R": "T-S1R", "S2": "T-S2"}[s.route]
        p.strategy, p.risk_level = "SAFETY_TEMPLATE", s.risk_level
        p.template_ids = [tid]
        p.rendered_templates = [snapshot.template(tid)]
        p.deterministic_text = _with_name(snapshot.template(tid), name)
        p.reasons.extend(s.reasons)
        if any(x.startswith(("PACKAGING.", "ORDER.")) for x in codes):
            p.operator_warnings.append("SAFETY_AND_SERVICE: service part not answered (safety is not routed via defect flow)")
        return p

    # 4. service / marketplace order (ODR-08)
    packaging = sorted(x for x in codes if x.startswith("PACKAGING."))
    order = sorted(x for x in codes if x in ("ORDER.wrong_product", "ORDER.incomplete_bundle"))
    if packaging or order:
        tid = "T-SVC-WB-ORDER" if order else "T-SVC-WB-DAMAGED"
        p.strategy, p.risk_level = "SERVICE", "R2"
        p.template_ids = [tid]
        p.rendered_templates = [snapshot.template(tid)]
        p.deterministic_text = _with_name(snapshot.template(tid), name)
        p.reasons.append("service: " + ",".join(packaging + order))
        if any(x.startswith(FACT_CODES_PREFIX) for x in codes):
            p.operator_warnings.append("UNANSWERED_PART: product question alongside a service case")
        return p
    if "ORDER.return_refund" in codes or "ORDER.availability" in codes:
        return _human(p, "SERVICE_INSTRUCTION_NOT_VERIFIED", "return/availability without an approved instruction")

    # 5. factual questions
    required = _required_facts(c, policy) if c.is_question else []
    p.required_facts = required
    if c.is_question and not required:
        if c.unresolved_ingredient_terms:
            return _human(p, "REQUIRED_FACT_UNKNOWN", "ingredient not in registry: " + ", ".join(c.unresolved_ingredient_terms))
        if c.text_sentiment in ("positive",) and c.entity_type == "review":
            return _ack(p, c, snapshot, res)
        return _human(p, "UNCLASSIFIED_QUESTION", "no required fact identified")
    if required:
        scope = _scope(snapshot, res, c)
        if scope is None:
            p.resolved = [ResolvedFact(r["fact_type"], None, "UNKNOWN", r.get("subject"),
                                       reason="BUNDLE_COMPONENT_AMBIGUOUS") for r in required]
            return _decide(p, snapshot, res, c)
        for r in required:
            for pid in scope:
                p.resolved.append(resolve_fact(snapshot, pid, r["fact_type"], r.get("subject"), c))
        for t in c.unresolved_ingredient_terms:
            p.resolved.append(ResolvedFact("ingredient_presence", None, "UNKNOWN", t, reason="UNRESOLVED_INGREDIENT"))
        return _decide(p, snapshot, res, c)

    # 6. experience reviews (no question)
    return _ack(p, c, snapshot, res)


def _human(p: Plan, code: str, reason: str) -> Plan:
    p.strategy, p.failure_code = "HUMAN_REVIEW", code
    p.reasons.append(reason)
    return p


def _ack(p: Plan, c: Classification, snapshot: KnowledgeSnapshot, res: ProductResolution) -> Plan:
    txt_empty = not (c.situations or c.text_sentiment not in ("none",))
    if c.text_sentiment in ("positive",) or (c.text_sentiment == "none" and (c.rating or 0) >= 4):
        p.strategy, p.tone = "ACKNOWLEDGEMENT", "positive"
    elif c.text_sentiment == "mixed":
        p.strategy, p.tone = "ACKNOWLEDGEMENT", "mixed"
    elif c.text_sentiment == "negative" or (c.text_sentiment == "none" and (c.rating or 5) <= 3):
        specific = any(x.startswith(("SENSORY.", "EXPECTED_RESULT.")) for x in c.codes)
        p.strategy, p.tone = ("ACKNOWLEDGEMENT" if specific else "CLARIFICATION_REQUIRED"), "negative"
    else:
        p.strategy, p.tone = "ACKNOWLEDGEMENT", "neutral"
    p.reasons.append("experience review" + (" (no text)" if txt_empty else ""))
    return p


def _required_facts(c: Classification, policy: dict) -> list[dict]:
    rf = policy["required_facts"]
    out: list[dict] = []
    codes = c.codes

    def add(ft, subject=None):
        item = {"fact_type": ft, "subject": subject}
        if item not in out:
            out.append(item)

    ingredient_codes = {"PRODUCT_INFO.ingredient_presence", "PRODUCT_INFO.ingredient_concentration"}
    for code in sorted(codes):
        for ft in rf.get(code, []):
            if code in ingredient_codes:
                continue
            if ft == "intended_use":
                for area in c.asked_areas or []:
                    add("intended_use", area)
                continue
            if ft == "fragrance_profile" and "parfum" in c.ingredient_mentions:
                continue
            add(ft)
    concentration = "PRODUCT_INFO.ingredient_concentration" in codes
    for iid in c.ingredient_mentions:
        add("ingredient_concentration" if concentration else "ingredient_presence", iid)
    if concentration and not c.ingredient_mentions:
        add("ingredient_concentration", None)
    # composition question that also names an ingredient = presence question
    if c.ingredient_mentions:
        out = [o for o in out if o["fact_type"] != "composition"]
    return out


def _scope(snapshot: KnowledgeSnapshot, res: ProductResolution, c: Classification) -> Optional[list[str]]:
    if res.kind != "bundle":
        return [res.product_id]
    comps = res.components
    if c.asked_component:
        chosen = [x for x in comps if (snapshot.product(x) or {}).get("product_type") == c.asked_component or
                  (c.asked_component == "cream" and (snapshot.product(x) or {}).get("product_type") in
                   ("cream", "hand_cream", "body_cream"))]
        if len(chosen) == 1:
            return chosen
        if chosen:
            return chosen
    return comps


def _fact_value(f: dict) -> str:
    return f.get("customer_value_ru") or ""


def _numbers(text: str) -> list[str]:
    from app.v3.text import normalize, numbers
    return sorted({n.value for n in numbers(normalize(text))})


def resolve_fact(snapshot: KnowledgeSnapshot, pid: str, fact_type: str, subject, c: Classification) -> ResolvedFact:
    policy = snapshot.policy
    prod = snapshot.product(pid) or {}
    pname = prod.get("customer_name_ru") or pid
    unknown_policy = policy.get("policy_unknown_facts", {}).get(fact_type)
    if unknown_policy:
        st = unknown_policy["state"]
        tid = unknown_policy.get("template")
        if st == "CONFLICT_POLICY":
            return ResolvedFact(fact_type, pid, "CONFLICT", subject, reason=unknown_policy.get("reason"))
        return ResolvedFact(fact_type, pid, "UNKNOWN", subject, template_id=tid,
                            customer_value_ru=snapshot.template(tid) if tid else None,
                            reason=unknown_policy.get("reason") or "POLICY_UNKNOWN")
    if prod.get("customer_fact_generation") == "RESTRICTED":
        return ResolvedFact(fact_type, pid, "KNOWN_RESTRICTED", subject, reason="CUSTOMER_FACT_GENERATION_RESTRICTED")

    if fact_type == "composition":
        rec = snapshot.reconciliation(pid)
        if rec == "CONTENT_MISMATCH":
            return ResolvedFact(fact_type, pid, "CONFLICT", reason="KNOWLEDGE_CONFLICT:INCI_CONTENT_MISMATCH")
        if rec not in ("MATCH", "ORDER_ONLY"):
            return ResolvedFact(fact_type, pid, "UNKNOWN", reason="LABEL_INCI_UNAVAILABLE")
        rows = snapshot.ingredient_rows(pid, "label")
        inci = ", ".join(r["inci_as_written"] for r in rows)
        rf = ResolvedFact(fact_type, pid, "KNOWN_ALLOWED",
                          customer_value_ru=f"Состав ({pname}), как указано на упаковке: {inci}.",
                          fact_ids=[f"{pid}.label_inci"], source_ids=sorted({r["source_id"] for r in rows}),
                          ingredient_ids=[r["ingredient_id"] for r in rows])
        if rec == "ORDER_ONLY":
            rf.reason = "INCI_ORDER_CONFLICT"
        return rf

    if fact_type in ("ingredient_presence", "ingredient_concentration"):
        return _resolve_ingredient(snapshot, pid, fact_type, subject)

    if fact_type == "intended_use":
        f = (snapshot.facts(pid, "intended_use") or [None])[0]
        if not f:
            return ResolvedFact(fact_type, pid, "UNKNOWN", subject, reason="NO_T1_INTENDED_USE")
        areas = f.get("value") or []
        if subject in areas:
            return ResolvedFact(fact_type, pid, "KNOWN_ALLOWED", subject,
                                customer_value_ru=f"По документации {pname} предназначен(а) {f['customer_value_ru']}.",
                                fact_ids=[f["fact_id"]], source_ids=[f["source_id"]])
        wording = policy["intended_use_wording"]
        declared = wording["declared_as_ru"].get(pid) or (
            wording["declared_as_ru"]["default_face"] if areas == ["face"] else wording["declared_as_ru"]["default_hands_body"])
        asked = wording["areas_ru"].get(subject, "для этой зоны")
        text = snapshot.template("T-INTENDED-USE").format(declared_as=declared, asked_area=asked)
        return ResolvedFact(fact_type, pid, "UNKNOWN", subject, customer_value_ru=text, template_id="T-INTENDED-USE",
                            fact_ids=[f["fact_id"]], source_ids=[f["source_id"]], reason="EXPANDED_USE_NOT_CONFIRMED")

    if fact_type == "fragrance_profile":
        f = (snapshot.facts(pid, "fragrance_profile") or [None])[0]
        if f:
            return _from_fact(fact_type, pid, f)
        rows = snapshot.ingredient_rows(pid, "recipe")
        has_parfum = any("fragrance" in (snapshot.ingredients.get(r["ingredient_id"], {}).get("classes") or [])
                         for r in rows)
        if not rows:
            return ResolvedFact(fact_type, pid, "UNKNOWN", reason="NO_COMPOSITION")
        if snapshot.reconciliation(pid) == "CONTENT_MISMATCH":
            return ResolvedFact(fact_type, pid, "CONFLICT", reason="KNOWLEDGE_CONFLICT:INCI_CONTENT_MISMATCH")
        txt = "В составе указана отдушка (Parfum)." if has_parfum else "Отдушка (Parfum) в составе не указана."
        return ResolvedFact(fact_type, pid, "KNOWN_ALLOWED", customer_value_ru=txt,
                            fact_ids=[f"{pid}.ingredient.recipe.parfum"], source_ids=[rows[0]["source_id"]])

    if fact_type == "volume":
        fs = snapshot.facts(pid, "volume") or (snapshot.facts(pid, "net_mass") + snapshot.facts(pid, "container_volume"))
        if not fs:
            return ResolvedFact(fact_type, pid, "UNKNOWN", reason="NO_FACT")
        vals = "; ".join(f["customer_value_ru"] for f in fs if f.get("customer_value_ru"))
        return ResolvedFact(fact_type, pid, "KNOWN_ALLOWED", customer_value_ru=vals,
                            fact_ids=[f["fact_id"] for f in fs], source_ids=sorted({f["source_id"] for f in fs}),
                            numbers=_numbers(vals))

    fs = snapshot.facts(pid, fact_type)
    if not fs:
        return ResolvedFact(fact_type, pid, "UNKNOWN", reason="NO_FACT")
    return _from_fact(fact_type, pid, fs[0])


def _from_fact(fact_type: str, pid: str, f: dict) -> ResolvedFact:
    if f["fact_status"] == "CONFLICT":
        return ResolvedFact(fact_type, pid, "CONFLICT", fact_ids=[f["fact_id"]],
                            reason=f"KNOWLEDGE_CONFLICT:{f.get('conflict_id')}")
    if f["disclosure_policy"] not in CUSTOMER_DISCLOSURE or f["fact_status"] not in (
            "VERIFIED", "OWNER_APPROVED", "COMMERCIAL_VERIFIED"):
        return ResolvedFact(fact_type, pid, "KNOWN_RESTRICTED", fact_ids=[f["fact_id"]],
                            reason=f"DISCLOSURE:{f['disclosure_policy']}")
    if f.get("extraction_confidence") == "LOW":
        return ResolvedFact(fact_type, pid, "UNKNOWN", fact_ids=[f["fact_id"]], reason="LOW_EXTRACTION_CONFIDENCE")
    val = f.get("customer_value_ru") or ""
    return ResolvedFact(fact_type, pid, "KNOWN_ALLOWED", customer_value_ru=val, fact_ids=[f["fact_id"]],
                        source_ids=[f["source_id"]], numbers=_numbers(val))


def _resolve_ingredient(snapshot: KnowledgeSnapshot, pid: str, fact_type: str, iid) -> ResolvedFact:
    if iid is None:
        return ResolvedFact(fact_type, pid, "UNKNOWN", reason="INGREDIENT_NOT_SPECIFIED")
    rec = snapshot.reconciliation(pid)
    if rec == "CONTENT_MISMATCH":
        return ResolvedFact(fact_type, pid, "CONFLICT", iid, reason="KNOWLEDGE_CONFLICT:INCI_CONTENT_MISMATCH")
    rows = snapshot.ingredient_rows(pid, "recipe")
    if not rows:
        return ResolvedFact(fact_type, pid, "UNKNOWN", iid, reason="NO_COMPOSITION")
    ing = snapshot.ingredients.get(iid) or {}
    display = snapshot.ingredient_display(iid)
    src = [rows[0]["source_id"]]
    if "ceramide" in (ing.get("classes") or []):
        present = [r for r in rows if "ceramide" in (snapshot.ingredients.get(r["ingredient_id"], {}).get("classes") or [])]
        if present:
            return ResolvedFact(fact_type, pid, "KNOWN_ALLOWED" if fact_type == "ingredient_presence" else "KNOWN_RESTRICTED",
                                iid, customer_value_ru=snapshot.template("T-CERAMIDES-LIST"),
                                template_id="T-CERAMIDES-LIST", fact_ids=[f"{pid}.ingredient.recipe.ceramides"],
                                source_ids=src, reason=None if fact_type == "ingredient_presence" else "CERAMIDE_POLICY",
                                ingredient_ids=[r["ingredient_id"] for r in present])
    row = next((r for r in rows if r["ingredient_id"] == iid), None)
    if row is None:
        fam = [r for r in rows if r["ingredient_id"] in snapshot.family_members(iid) and r["ingredient_id"] != iid]
        if fam:
            other = snapshot.ingredient_display(fam[0]["ingredient_id"])
            return ResolvedFact(fact_type, pid, "KNOWN_ALLOWED", iid,
                                customer_value_ru=f"В составе, указанном производителем, нет компонента «{display}»; указан компонент «{other}» ({fam[0]['inci_as_written']}).",
                                fact_ids=[f"{pid}.ingredient.recipe.{fam[0]['ingredient_id']}"], source_ids=src,
                                ingredient_ids=[iid, fam[0]["ingredient_id"]])
        return ResolvedFact(fact_type, pid, "KNOWN_ALLOWED", iid,
                            customer_value_ru=f"В составе, указанном производителем, нет компонента «{display}».",
                            fact_ids=[f"{pid}.ingredient.recipe.absent.{iid}"], source_ids=src, ingredient_ids=[iid])
    fid = f"{pid}.ingredient.recipe.{iid}"
    presence = f"В составе есть {display} ({row['inci_as_written']})."
    if fact_type == "ingredient_presence":
        return ResolvedFact(fact_type, pid, "KNOWN_ALLOWED", iid, customer_value_ru=presence, fact_ids=[fid],
                            source_ids=src, ingredient_ids=[iid])
    disc = row.get("concentration_disclosure_policy") or "PUBLIC"
    if disc not in CUSTOMER_DISCLOSURE:
        abstraction = row.get("customer_abstraction_ru")
        return ResolvedFact(fact_type, pid, "KNOWN_RESTRICTED", iid, customer_value_ru=abstraction,
                            fact_ids=[fid], source_ids=src, reason=f"DISCLOSURE:{disc}",
                            ingredient_ids=[iid])
    if row.get("extraction_confidence") == "LOW":
        return ResolvedFact(fact_type, pid, "UNKNOWN", iid, reason="LOW_EXTRACTION_CONFIDENCE")
    pct = str(row["concentration_pct"]).rstrip("0").rstrip(".") if "." in str(row["concentration_pct"]) else str(row["concentration_pct"])
    val = f"{display[:1].upper() + display[1:]} ({row['inci_as_written']}) — {pct.replace('.', ',')}% по спецификации производителя."
    return ResolvedFact(fact_type, pid, "KNOWN_ALLOWED", iid, customer_value_ru=val, fact_ids=[fid],
                        source_ids=src, numbers=[pct], ingredient_ids=[iid])


def _decide(p: Plan, snapshot: KnowledgeSnapshot, res: ProductResolution, c: Classification) -> Plan:
    states = [r.state for r in p.resolved]
    if any(r.state == "CONFLICT" for r in p.resolved):
        why = sorted({r.reason or "" for r in p.resolved if r.state == "CONFLICT"})
        policy_only = all((r.reason or "").startswith("COSMETIC_CLAIMS_DISABLED")
                          for r in p.resolved if r.state == "CONFLICT")
        return _human(p, "POLICY_RESTRICTED_CLAIM" if policy_only else "KNOWLEDGE_CONFLICT", "; ".join(why))
    if any(r.state == "KNOWN_RESTRICTED" and not r.customer_value_ru for r in p.resolved):
        why = sorted({r.reason or "" for r in p.resolved if r.state == "KNOWN_RESTRICTED"})
        return _human(p, "KNOWN_RESTRICTED_NO_ABSTRACTION", "; ".join(why))
    if any(r.state == "UNKNOWN" and (r.reason in HUMAN_UNKNOWN_REASONS) for r in p.resolved):
        why = sorted({r.reason for r in p.resolved if r.state == "UNKNOWN" and r.reason in HUMAN_UNKNOWN_REASONS})
        return _human(p, "REQUIRED_FACT_UNKNOWN", "; ".join(why))
    for r in p.resolved:
        if r.state == "KNOWN_RESTRICTED" and r.customer_value_ru and not r.template_id:
            # approved abstraction of a restricted value (e.g. ODR-07): fixed wording, never
            # paraphrased by the model ("процент не указан" would be false)
            r.template_id = "ABSTRACTION:" + (r.fact_ids[0] if r.fact_ids else r.fact_type)
        if r.template_id and r.template_id not in p.template_ids:
            p.template_ids.append(r.template_id)
            p.rendered_templates.append(r.customer_value_ru)
        if r.reason == "INCI_ORDER_CONFLICT":
            p.operator_warnings.append("INCI_ORDER_CONFLICT")
    plain_unknown = [r for r in p.resolved if r.state == "UNKNOWN" and not r.template_id]
    p.strategy = "UNKNOWN_FACT" if any(s == "UNKNOWN" for s in states) else "FACT_ANSWER"
    if plain_unknown:
        p.reasons.append("unknown: " + ",".join(sorted({f"{r.fact_type}({r.reason})" for r in plain_unknown})))
    # template-only answers are rendered deterministically (no LLM)
    only_templates = all(r.template_id for r in p.resolved) and p.resolved
    if only_templates:
        p.deterministic_text = _with_name(" ".join(dict.fromkeys(p.rendered_templates)), p.buyer_name)
    return p
