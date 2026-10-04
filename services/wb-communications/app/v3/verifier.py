"""WP7 — deterministic verifier. ONE verifier for AI drafts, v2 drafts and manual operator text.

Verdict: BLOCK | WARNING | INFO | PASS. Rules (BLOCK unless noted):
  V-LEN V-REFUSAL V-TM V-CER V-RESTRICTED V-CONFLICT V-NUM V-GENERAL V-MEDICAL V-CLAIM
  V-FREEFROM V-USE V-SUITABILITY V-SAFETY V-SERVICE V-CONTACT V-DISPENSER V-ID V-FACT
  V-UNKNOWN V-TEMPLATE ;  V-TONE V-ADV (WARNING) ; V-INCI-ORDER (INFO).
"Semantic" beyond literal strings = morphological stems, clause co-occurrence (ingredient +
number, subject + verdict), numeric canonicalization (2,25 == 2.25), exemptions only for
wording that is literally present in the allowed facts / approved templates.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Iterable, Optional

from app.v3.planner import CUSTOMER_DISCLOSURE, Plan
from app.v3.snapshot import KnowledgeSnapshot
from app.v3.text import (NUMBER_WORDS, find_all, find_literal_spans, normalize, numbers, search_any,
                         strip_spans)

UNKNOWN_SHAPES = {
    "frequency": [r"\bраз\s+в\s+(день|неделю)", r"\bежедневн", r"\bутром\b", r"\bвечером\b", r"\bна\s+ночь\b"],
    "amount": [r"\bкапл", r"\bгорош", r"\bнажат", r"\bпипетк"],
    "pao": [r"после\s+(вскрыти|открыти)[^.!?]{0,40}\d"],
    "shelf_life": [r"срок\w*\s+годност[^.!?]{0,30}\d"],
    "country_of_origin": [r"\bкита[йе]", r"\bкоре[яйи]", r"\bросси[ия]", r"\bфранци", r"\bитали", r"\bяпони", r"\bсша\b", r"\bгермани"],
    "manufacturer": [r"guangzhou", r"\bфабрик", r"\bзавод"],
    "ph": [r"\bph\b", r"\bпш\b"],
    "directions": [r"\bнанос", r"\bраспредел", r"\bвтира", r"\bсмыв"],
    "skin_type_suitability": [r"(подход|подойд)\w*\s+(для|при)\s+(сух|жирн|чувствит|комбинир|проблемн|нормальн|зрел)"],
    "fragrance_profile": [r"\bаромат\w*\s+(вишн|уд|древес|ванил|амбр)", r"\bноты\b"],
}
FACT_TYPES_WITH_SHAPES = tuple(UNKNOWN_SHAPES)
DIGIT_NAME_RX = re.compile(r"(c10-30|gum-1|-20\b|-4\b|1,2-|b12|b3\b|ceramide|церамид\w*\s+(ns|ng|np|eop|ap|as))")


@dataclass
class Violation:
    rule_id: str
    severity: str
    evidence_span: str
    explanation: str
    related_fact_id: Optional[str] = None
    related_claim_id: Optional[str] = None


@dataclass
class VerifierResult:
    verdict: str
    violations: list = field(default_factory=list)
    mode: str = "plan"

    @property
    def blocked(self) -> bool:
        return self.verdict == "BLOCK"

    def rule_ids(self, severity: str = "BLOCK") -> list:
        return sorted({v.rule_id for v in self.violations if v.severity == severity})

    def to_dict(self) -> dict:
        return {"verdict": self.verdict, "mode": self.mode, "violations": [asdict(v) for v in self.violations]}


@dataclass
class VerifierContext:
    mode: str                                   # plan | free_text
    product_ids: list
    allowed_corpus: str = ""
    allowed_numbers: set = field(default_factory=set)
    allowed_number_units: set = field(default_factory=set)   # {(value, unit)} — a number is bound to its unit
    allowed_ingredient_ids: set = field(default_factory=set)
    templates: list = field(default_factory=list)          # approved wording spans (exempt)
    required_templates: list = field(default_factory=list)
    intended_areas: set = field(default_factory=set)
    product_types: set = field(default_factory=set)
    name_spans_text: list = field(default_factory=list)    # product names (exempt for V-ID/V-USE)
    restricted_numbers: dict = field(default_factory=dict)  # canonical number -> fact id
    restricted_texts: dict = field(default_factory=dict)    # regex -> fact/conflict id
    restricted_concentration_ingredients: set = field(default_factory=set)
    conflict_ingredient_products: bool = False             # CONTENT_MISMATCH/RESTRICTED in scope
    unknown_fact_types: set = field(default_factory=set)
    strategy: Optional[str] = None
    inci_order_warning: bool = False
    own_identifiers: set = field(default_factory=set)
    approved_explanation_texts: list = field(default_factory=list)
    approved_guidance_texts: list = field(default_factory=list)


# --------------------------------------------------------------------------------------------
# context builders
# --------------------------------------------------------------------------------------------
def _scope_products(snapshot: KnowledgeSnapshot, product_ids: Iterable[str]) -> list[str]:
    out = []
    for pid in product_ids:
        if not pid:
            continue
        out.append(pid)
        out.extend(snapshot.components(pid) if (snapshot.product(pid) or {}).get("kind") == "bundle" else [])
    return list(dict.fromkeys(out))


def _base_context(snapshot: KnowledgeSnapshot, mode: str, product_ids: list[str]) -> VerifierContext:
    ctx = VerifierContext(mode=mode, product_ids=product_ids)
    scope = _scope_products(snapshot, product_ids)
    policy = snapshot.policy
    for pid in scope:
        pr = snapshot.product(pid) or {}
        if pr.get("kind") == "single":
            ctx.product_types.add(pr.get("product_type"))
            for f in snapshot.facts(pid, "intended_use"):
                ctx.intended_areas.update(f.get("value") or [])
        if pr.get("customer_name_ru"):
            ctx.name_spans_text.append(pr["customer_name_ru"])
        # restricted literal values of this product
        for f in pr.get("facts", []):
            if f["disclosure_policy"] not in CUSTOMER_DISCLOSURE or f["fact_status"] == "CONFLICT":
                for tok in numbers(normalize(str(f.get("value")))):
                    ctx.restricted_numbers.setdefault(tok.value, f["fact_id"])
                if f["fact_type"] == "commercial_age_marker":
                    ctx.restricted_texts[r"\b12\s*\+|\bс\s*12\s*лет|\b12\s*лет\s+и\s+старше"] = f["fact_id"]
        for o in ((pr.get("ingredients") or {}).get("recipe") or []):
            if o.get("concentration_disclosure_policy") not in (None, *CUSTOMER_DISCLOSURE):
                ctx.restricted_concentration_ingredients.add(o["ingredient_id"])
                ctx.restricted_numbers.setdefault(str(o["concentration_pct"]).rstrip("0").rstrip("."),
                                                  f"{pid}.ingredient.recipe.{o['ingredient_id']}")
        rec = ((pr.get("ingredients") or {}).get("reconciliation"))
        if rec in ("CONTENT_MISMATCH", "RESTRICTED") or pr.get("customer_fact_generation") == "RESTRICTED":
            ctx.conflict_ingredient_products = True
        for cid in pr.get("conflicts", []):
            for bv in (snapshot.conflicts.get(cid) or {}).get("blocked_values") or []:
                ctx.restricted_texts[re.escape(normalize(bv))] = cid
    ctx.templates = [normalize(t["text"]) for t in policy["templates"].values() if "{" not in t["text"]]
    # the product's own marketplace identifiers (e.g. «арт. 868597351») are not factual claims
    ctx.own_identifiers = {i["value"] for i in snapshot.data["identifiers"]
                           if i["product_id"] in scope and i["id_type"] in ("wb_nm_id", "label_ean")}
    return ctx


def context_for_plan(plan: Plan, snapshot: KnowledgeSnapshot) -> VerifierContext:
    ctx = _base_context(snapshot, "plan", plan.product_ids)
    ctx.strategy = plan.strategy
    corpus = []
    for r in plan.allowed:
        if r.customer_value_ru:
            corpus.append(r.customer_value_ru)
            ctx.allowed_ingredient_ids.update(r.ingredient_ids)
    ctx.required_templates = [normalize(t) for t in plan.rendered_templates if t]
    corpus.extend(plan.rendered_templates)
    ctx.allowed_corpus = normalize(" ".join(corpus))
    ctx.allowed_numbers = {n.value for n in numbers(ctx.allowed_corpus)}
    ctx.allowed_number_units = {(n.value, n.unit) for n in numbers(ctx.allowed_corpus)}
    known_types = {r.fact_type for r in plan.resolved if r.state == "KNOWN_ALLOWED"}
    ctx.unknown_fact_types = ({r.fact_type for r in plan.resolved if r.state != "KNOWN_ALLOWED"} |
                              set(FACT_TYPES_WITH_SHAPES)) - known_types
    ctx.inci_order_warning = "INCI_ORDER_CONFLICT" in plan.operator_warnings
    # rendered (parameterized) templates are approved wording too
    ctx.templates = list(dict.fromkeys(ctx.templates + ctx.required_templates))
    return ctx


def context_for_free_text(snapshot: KnowledgeSnapshot, product_ids: list[str]) -> VerifierContext:
    """Context for text that did not come from a v3 plan (v2 drafts, manual operator edits):
    everything customer-disclosable about the product(s) is allowed; nothing else."""
    ctx = _base_context(snapshot, "free_text", product_ids)
    corpus, known_types = [], set()
    for pid in _scope_products(snapshot, product_ids):
        pr = snapshot.product(pid) or {}
        if pr.get("kind") != "single" or pr.get("customer_fact_generation") == "RESTRICTED":
            continue
        for f in pr.get("facts", []):
            if f["disclosure_policy"] in CUSTOMER_DISCLOSURE and f["fact_status"] in (
                    "VERIFIED", "OWNER_APPROVED", "COMMERCIAL_VERIFIED") and f.get("customer_value_ru"):
                corpus.append(f["customer_value_ru"])
                known_types.add(f["fact_type"])
        ing = pr.get("ingredients") or {}
        if ing.get("reconciliation") in ("MATCH", "ORDER_ONLY", "LABEL_UNAVAILABLE"):
            for o in ing.get("recipe") or []:
                ctx.allowed_ingredient_ids.add(o["ingredient_id"])
                corpus.append(snapshot.ingredient_display(o["ingredient_id"]) + " " + o["inci_as_written"])
                if o.get("concentration_disclosure_policy") in CUSTOMER_DISCLOSURE:
                    corpus.append(str(o["concentration_pct"]).rstrip("0").rstrip(".") + "%")
            known_types.update({"composition"})
    t = snapshot.policy["templates"]
    corpus.extend(v["text"] for v in t.values() if "{" not in v["text"])
    ctx.allowed_corpus = normalize(" ".join(corpus))
    ctx.allowed_numbers = {n.value for n in numbers(ctx.allowed_corpus)}
    ctx.allowed_number_units = {(n.value, n.unit) for n in numbers(ctx.allowed_corpus)}
    ctx.unknown_fact_types = set(FACT_TYPES_WITH_SHAPES) - known_types
    ctx.strategy = "FREE_TEXT"
    return ctx


# --------------------------------------------------------------------------------------------
# rules
# --------------------------------------------------------------------------------------------
def _is_list_marker(text: str, tok) -> bool:
    """'1. плотно…' / '2) …' — enumeration, not a quantity."""
    before = text[max(0, tok.start - 2): tok.start]
    after = text[tok.end: tok.end + 2]
    return len(tok.raw) <= 2 and re.match(r"[.)]\s", after) is not None and (tok.start == 0 or before[-1:] in (" ", ":", "\n"))


def _in_corpus(ctx: VerifierContext, phrase: str) -> bool:
    p = phrase.strip()
    return bool(p) and p in ctx.allowed_corpus


def verify(text: Optional[str], ctx: VerifierContext, snapshot: KnowledgeSnapshot) -> VerifierResult:
    policy = snapshot.policy
    vp = policy["verifier"]
    out: list[Violation] = []

    def add(rule, sev, span, why, fact=None, claim=None):
        out.append(Violation(rule, sev, (span or "")[:160], why, fact, claim))

    raw = text or ""
    norm = normalize(raw)
    if not norm:
        add("V-LEN", "BLOCK", "", "empty text")
        return _result(out, ctx)
    if len(raw) > int(vp.get("max_len", 1000)):
        add("V-LEN", "BLOCK", raw[:40], f"{len(raw)} chars > WB limit")

    # approved wording (templates) + product names are exempt from content rules
    tpl_spans = [s for t in ctx.templates for s in find_literal_spans(norm, t)]
    name_spans = [s for n in ctx.name_spans_text for s in find_literal_spans(norm, n)]
    free = strip_spans(norm, tpl_spans)                 # text outside approved templates
    free_no_names = strip_spans(free, name_spans)

    m = search_any(vp["refusal_meta"], norm)
    if m:
        add("V-REFUSAL", "BLOCK", m.group(), "model refusal / meta text")
    for m in find_all(policy["trademark_policy"]["blocked_terms"], norm):
        add("V-TM", "BLOCK", m.group(), "blocked trademark / fragrance name (ODR-02)")
    for m in find_all(policy["ceramide_policy"]["blocked_patterns"], norm):
        add("V-CER", "BLOCK", m.group(), "ceramide count wording (ODR-15)")

    # --- restricted / conflicting values
    toks = [t for t in numbers(free)
            if not DIGIT_NAME_RX.search(free[max(0, t.start - 12): t.end + 3])
            and t.raw not in ctx.own_identifiers
            and not _is_list_marker(free, t)]
    for t in toks:
        if t.value in ctx.restricted_numbers and t.value not in ctx.allowed_numbers:
            add("V-RESTRICTED", "BLOCK", free[max(0, t.start - 25): t.end + 5],
                "restricted / conflicting value disclosed", fact=ctx.restricted_numbers[t.value])
    for rx, ref in ctx.restricted_texts.items():
        for m in re.finditer(rx, free):
            rule = "V-CONFLICT" if str(ref).startswith("KC-") else "V-RESTRICTED"
            add(rule, "BLOCK", m.group(), f"restricted/conflict value ({ref})", fact=str(ref))
    if ctx.restricted_concentration_ingredients:
        for m in find_all(vp["comparative_disclosure"], free):
            add("V-RESTRICTED", "BLOCK", m.group(), "indirect / comparative disclosure of a restricted concentration")
        for sent in re.split(r"[.!?\n]", free):
            ids = {iid for iid, _ in snapshot.ingredient_mentions(sent)}
            if ids & ctx.restricted_concentration_ingredients and (
                    re.search(r"\d", sent) or re.search(r"концентрац|процент|%", sent)):
                add("V-RESTRICTED", "BLOCK", sent.strip(), "number/concentration next to a restricted ingredient")

    # --- numbers
    for t in toks:
        if t.value in ctx.restricted_numbers and t.value not in ctx.allowed_numbers:
            continue  # already V-RESTRICTED
        if t.value not in ctx.allowed_numbers:
            add("V-NUM", "BLOCK", free[max(0, t.start - 25): t.end + 8], f"number {t.raw} not in allowed facts")
        elif t.unit and (t.value, t.unit) not in ctx.allowed_number_units:
            add("V-NUM", "BLOCK", free[max(0, t.start - 25): t.end + 8],
                f"number {t.raw} {t.unit} is not bound to an allowed fact with that unit")
    for m in re.finditer(NUMBER_WORDS + r"\s+(раз\w*|недел\w*|дн\w*|месяц\w*|кап\w*|нажат\w*|минут\w*|час\w*)", free):
        if not _in_corpus(ctx, m.group()):
            add("V-NUM", "BLOCK", m.group(), "spelled-out quantity not in allowed facts")

    # Exact reviewed variants are exempt ONLY from their claim family. Every other
    # rule still inspects the original text (medical/numeric/restricted/identity).
    claim_free = strip_spans(free_no_names, [s for t in ctx.approved_explanation_texts
                            for s in find_literal_spans(free_no_names, normalize(t))])
    guidance_free = strip_spans(free, [s for t in ctx.approved_guidance_texts
                               for s in find_literal_spans(free, normalize(t))])

    # --- general advice (library empty -> any advice not literally in allowed facts is BLOCK)
    for family, pats in vp["general_advice"].items():
        for m in find_all(pats, guidance_free):
            if not _in_corpus(ctx, m.group()):
                add("V-GENERAL", "BLOCK", m.group(), f"general advice ({family}) without approved guidance_id")
    for m in find_all(vp["medical"], free):
        if not _in_corpus(ctx, m.group()):
            add("V-MEDICAL", "BLOCK", m.group(), "medical / high-risk claim")
    for m in find_all(vp["cosmetic_claims"], claim_free):
        if not _in_corpus(ctx, m.group()):
            add("V-CLAIM", "BLOCK", m.group(), "cosmetic claim while cosmetic_claim_generation is disabled (ODR-06)")
    # Presence is not causal efficacy. The shared ingredient alias fix must not make
    # "ingredient provides softness" publishable merely because presence is known.
    for clause in re.split(r"[.!?;]", claim_free):
        if snapshot.ingredient_mentions(clause) and re.search(
                r"(обеспечива\w*|прида[её]т|делает)[^.!?]{0,70}(мягк|нежн)", clause):
            if not _in_corpus(ctx, clause):
                add("V-CLAIM", "BLOCK", clause, "ingredient presence does not authorize a causal softness claim")
    for clause in re.split(r"[.!?;]", claim_free):
        if snapshot.ingredient_mentions(clause) and re.search(r'помога|способству|поддержива|увлажня|пита[её]|защища|смягча|лечит|восстанавлива',clause):
            if not _in_corpus(ctx,clause):
                add('V-CLAIM','BLOCK',clause,'ingredient benefit requires explicit scoped expertise approval')
    for m in find_all(vp["free_from_claims"], free):
        if not _in_corpus(ctx, m.group()):
            add("V-FREEFROM", "BLOCK", m.group(), "derived free-from claim (ODR-10)")

    for clause in re.split(r"[.!?;]", free_no_names):
        for m in re.finditer(r"\b[а-яё-]+(?:ый|ий|ой|ая|ое|ые|ую|ым)\s+(?:аромат|запах)\w*|(?:аромат|запах)\w*\s+[а-яё-]+(?:ый|ий|ой|ая|ое|ые|ую|ым)\b", clause):
            if not _in_corpus(ctx, m.group()):
                add("V-FACT", "BLOCK", m.group(), "fragrance descriptor not approved for this product")

    # --- intended use
    for area, pats in vp["body_areas"].items():
        if ctx.intended_areas and area not in ctx.intended_areas:
            for m in find_all(pats, free_no_names):
                if not _in_corpus(ctx, m.group()):
                    add("V-USE", "BLOCK", m.group(), f"area '{area}' beyond T1 intended use {sorted(ctx.intended_areas)}")

    # --- suitability (pregnancy / children)
    for sent in re.split(r"[.!?\n]", free):
        if search_any(vp["suitability"]["subject"], sent):
            v = search_any(vp["suitability"]["verdict"], sent)
            if v:
                add("V-SUITABILITY", "BLOCK", sent.strip(), "pregnancy/children verdict without evidence (ODR-01)")

    # --- safety language / causality
    for m in find_all(vp["safety_language"], free):
        add("V-SAFETY", "BLOCK", m.group(), "unsafe safety language (adaptation / purging / normalisation)")
    for m in find_all(vp["causal_explanation"], free):
        add("V-SAFETY", "BLOCK", m.group(), "unsupported causal explanation")

    # --- service promises / contacts / dispenser
    for m in find_all(vp["service_promises"], free):
        add("V-SERVICE", "BLOCK", m.group(), "unauthorized service promise (ODR-08)")
    for m in find_all(vp["invented_contact"], free):
        add("V-CONTACT", "BLOCK", m.group(), "invented contact channel (ODR-08)")
    if re.search(r"дозатор|помп|нажат|качк|прокач|пипетк", free):
        for m in find_all(vp["dispenser"], free):
            add("V-DISPENSER", "BLOCK", m.group(), "unverified dispenser instruction (ODR-09)")

    # --- identity: other product types
    for ptype, pats in policy["product_type_words"].items():
        if ptype in ctx.product_types or not ctx.product_types:
            continue
        for m in find_all(pats, free_no_names):
            stem = m.group().strip()
            same_word = any(stem in normalize(" ".join(policy["product_type_words"].get(t, []))) for t in ctx.product_types)
            if not same_word and not _in_corpus(ctx, stem):
                add("V-ID", "BLOCK", m.group(), f"mentions another product type '{ptype}'")

    # --- facts: ingredient mentions must be backed by allowed facts
    for iid, (a, b) in snapshot.ingredient_mentions(free_no_names):
        if ctx.conflict_ingredient_products and ctx.mode == "free_text" and iid not in ctx.allowed_ingredient_ids:
            add("V-CONFLICT", "BLOCK", free_no_names[a:b], f"ingredient '{iid}' named for a product with INCI conflict/restriction")
        elif iid not in ctx.allowed_ingredient_ids:
            add("V-FACT", "BLOCK", free_no_names[a:b], f"ingredient '{iid}' not supported by allowed facts")
    if re.search(r"в\s+состав|содерж|концентрац", free_no_names) and not ctx.allowed_ingredient_ids:
        add("V-FACT", "BLOCK", "в составе/содержит", "composition statement without an allowed composition fact")

    # --- UNKNOWN turned into an assertion
    for ft in sorted(ctx.unknown_fact_types):
        for m in find_all(UNKNOWN_SHAPES.get(ft, []), free_no_names):
            if not _in_corpus(ctx, m.group()):
                add("V-UNKNOWN", "BLOCK", m.group(), f"asserts '{ft}' which is UNKNOWN / not allowed")

    # --- required templates present verbatim
    for t in ctx.required_templates:
        if t and t not in norm:
            add("V-TEMPLATE", "BLOCK", t[:60], "required approved wording missing or altered")

    # --- warnings / info
    for m in find_all(vp["defensive_tone"], free):
        add("V-TONE", "WARNING", m.group(), "defensive / blaming tone")
    for m in find_all(vp["advertising"], free):
        add("V-ADV", "WARNING", m.group(), "advertising in an answer")
    if ctx.inci_order_warning:
        add("V-INCI-ORDER", "INFO", "", "label INCI order differs from recipe (INCI_ORDER_CONFLICT)")
    return _result(out, ctx)


def _result(out: list[Violation], ctx: VerifierContext) -> VerifierResult:
    dedup, seen = [], set()
    for v in out:
        key = (v.rule_id, v.evidence_span)
        if key not in seen:
            seen.add(key)
            dedup.append(v)
    sev = {v.severity for v in dedup}
    verdict = "BLOCK" if "BLOCK" in sev else "WARNING" if "WARNING" in sev else "INFO" if "INFO" in sev else "PASS"
    return VerifierResult(verdict=verdict, violations=dedup, mode=ctx.mode)
