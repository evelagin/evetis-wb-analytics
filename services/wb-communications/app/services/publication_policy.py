"""Explicit publication gate: deterministic policy only, independent of shadow/LLM.

CURRENT means the validated immutable snapshot selected by this deployed revision.
Never consult the generation's prompt version or the shadow decision cache.
"""
import hashlib
from app.v3.snapshot import load_snapshot
from app.v3.resolver import resolve
from app.v3.verifier import context_for_free_text, verify


def validate_for_publication(text, communication, settings, *, include_spans=False):
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
    ctx.customer_experience = normalize(" ".join(str(communication.get(k) or "") for k in ("text","pros","cons")))
    rows = [r for r in approved(snap, product.product_id) if in_context(r,communication,snap)]
    ctx.approved_explanation_texts = [t for r in rows if r['kind']=='ingredient_benefit' for t in matching_texts(r,text)]
    ctx.approved_guidance_texts = [t for r in rows if r['kind']=='guidance' for t in matching_texts(r,text)]
    # Only the approved perception meaning: «аромат каждый воспринимает ...»
    # contains no scent descriptor. Actual descriptors outside this span stay checked.
    ctx.approved_fragrance_meaning_texts = [t for r in rows if r.get('meaning_id')=='fragrance_perception_v1'
                                          for t in matching_texts(r,text)]
    verdict = verify(text, ctx, snap)
    result.update(verdict=verdict.verdict, product_id=product.product_id,
                  violations=[{"rule_id": v.rule_id, "severity": v.severity,
                               "related_fact_id": v.related_fact_id,
                               "related_claim_id": v.related_claim_id} for v in verdict.violations])
    if include_spans:
        result['violation_spans'] = [{'rule_id': v.rule_id, 'span': v.evidence_span}
                                     for v in verdict.violations if v.severity=='BLOCK']
    return result
