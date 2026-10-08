"""Explicit publication gate: deterministic policy only, independent of shadow/LLM.

CURRENT means the validated immutable snapshot selected by this deployed revision.
Never consult the generation's prompt version or the shadow decision cache.
"""
import hashlib
from app.v3.snapshot import load_snapshot
from app.v3.resolver import resolve
from app.v3.verifier import context_for_free_text, verify


def validate_for_publication(text, communication, settings, *, include_spans=False, human_reviewed_safety=False):
    snap = load_snapshot(settings.v3_knowledge_snapshot_id or None)
    from app.response_quality.expertise import approved, registry_sha, in_context, matching_texts
    result = {
        "expertise_sha256": registry_sha(),
        "version": snap.policy["policy_version"], "snapshot": snap.snapshot_id,
        "snapshot_sha256": snap.data["content_sha256"],
        "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "verdict": "BLOCK", "violations": [],
    }
    if snap.policy["runtime"].get("auto_publish") is not False:
        raise ValueError("auto_publish must remain false")
    product = resolve(snap, nm_id=communication.get("nm_id"),
                      supplier_article=communication.get("supplier_article"),
                      barcode=communication.get("barcode"), marketplace=communication.get("channel", "wb"))
    if product.status != "VERIFIED" or not product.product_id or product.restricted_components:
        result["violations"] = [{"rule_id": "PRODUCT_NOT_VERIFIED", "severity": "BLOCK"}]
        return result
    ctx = context_for_free_text(snap, [product.product_id])
    from app.v3.text import normalize
    from app.v3.text import customer_text
    ctx.customer_experience = normalize(customer_text(communication))
    rows = [r for r in approved(snap, product.product_id) if in_context(r,communication,snap)]
    ctx.approved_explanation_texts = [t for r in rows if r['kind']=='ingredient_benefit' for t in matching_texts(r,text)]
    ctx.approved_guidance_texts = [t for r in rows if r['kind']=='guidance' for t in matching_texts(r,text)]
    # Only the approved perception meaning: «аромат каждый воспринимает ...»
    # contains no scent descriptor. Actual descriptors outside this span stay checked.
    ctx.approved_fragrance_meaning_texts = [t for r in rows if r.get('meaning_id')=='fragrance_perception_v1'
                                          for t in matching_texts(r,text)]
    ctx.human_reviewed_safety = bool(human_reviewed_safety)
    verdict = verify(text, ctx, snap)
    result.update(verdict=verdict.verdict, product_id=product.product_id,
                  violations=[{"rule_id": v.rule_id, "severity": v.severity,
                               "related_fact_id": v.related_fact_id,
                               "related_claim_id": v.related_claim_id} for v in verdict.violations])
    if include_spans:
        result['violation_spans'] = [{'rule_id': v.rule_id, 'span': v.evidence_span}
                                     for v in verdict.violations if v.severity=='BLOCK']
    return result


# --- LIVE v2 publication gate (R1) -------------------------------------------------------
# The v3.1E verifier above is shadow-only during R1. Operator publication of v2 drafts keeps
# the production contract of main 3632bae: app/v3/verifier_live.py is that verifier.py byte
# for byte (git blob 52f6ba20, pinned by a test), and ingredient matching is the production
# one. Switching live publication to the v3.1E policy is an R2 owner decision:
# V31_ENFORCE_LIVE_PUBLICATION_POLICY=true (and R1 refuses to start with it).

class _ProductionSnapshot:
    """The knowledge snapshot as the production verifier read it (main 3632bae)."""

    def __init__(self, snap):
        self._snap = snap

    def __getattr__(self, name):
        return getattr(self._snap, name)

    def ingredient_mentions(self, normalized_text):
        hits = []
        for iid, rx in self._snap._ingredient_patterns:
            for m in rx.finditer(normalized_text):
                hits.append((iid, m.span()))
        keep = []
        for iid, (a, b) in hits:
            inside = any((a2 <= a and b <= b2) and (b2 - a2) > (b - a) and iid2 != iid
                         for iid2, (a2, b2) in hits)
            if not inside:
                keep.append((iid, (a, b)))
        return keep


def validate_live_publication(text, communication, settings, *, include_spans=False):
    """Production-compatible gate for the live v2 publish path (body of main 3632bae).

    `include_spans` only adds operator-card evidence; it never changes the verdict.
    """
    from app.v3.verifier_live import context_for_free_text as live_context, verify as live_verify
    snap = load_snapshot(settings.v3_knowledge_snapshot_id or None)
    result = {
        "version": snap.policy["policy_version"], "snapshot": snap.snapshot_id,
        "snapshot_sha256": snap.data["content_sha256"],
        "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "verdict": "BLOCK", "violations": [], "gate": "LIVE_V2",
    }
    if snap.policy["runtime"].get("auto_publish") is not False:
        raise ValueError("auto_publish must remain false")
    product = resolve(snap, nm_id=communication.get("nm_id"),
                      supplier_article=communication.get("supplier_article"),
                      barcode=communication.get("barcode"), marketplace=communication.get("channel", "wb"))
    if product.status != "VERIFIED" or not product.product_id or product.restricted_components:
        result["violations"] = [{"rule_id": "PRODUCT_NOT_VERIFIED", "severity": "BLOCK"}]
        return result
    production = _ProductionSnapshot(snap)
    verdict = live_verify(text, live_context(production, [product.product_id]), production)
    result.update(verdict=verdict.verdict, product_id=product.product_id,
                  violations=[{"rule_id": v.rule_id, "severity": v.severity,
                               "related_fact_id": v.related_fact_id,
                               "related_claim_id": v.related_claim_id} for v in verdict.violations])
    if include_spans:
        result["violation_spans"] = [{"rule_id": v.rule_id, "span": v.evidence_span}
                                     for v in verdict.violations if v.evidence_span]
    return result


def validate_human_safety_publication(text, communication, settings, *, include_spans=False):
    """v3.1E policy for an operator-written answer to a SERIOUS_SAFETY case: the human review
    satisfies the routing prohibition only; facts, restricted values, claims, service premises,
    unsafe directives, length and product resolution are checked exactly as usual."""
    result = validate_for_publication(text, communication, settings, include_spans=include_spans,
                                      human_reviewed_safety=True)
    result["gate"] = "V31_HUMAN_SAFETY"
    return result


def live_publication_validator(settings):
    """Gate for operator publication of v2 drafts; never chosen by the R1 shadow flags."""
    if getattr(settings, "v31_enforce_live_publication_policy", False):
        return validate_for_publication
    return validate_live_publication
