"""WP3 — deterministic product resolver: marketplace identifier -> internal SKU / bundle.

Order: WB nmId (authoritative) -> WB vendor code (supplierArticle) -> label barcode.
Product name / customer text are NEVER used to pick a product: an unknown identifier is
PRODUCT_NOT_VERIFIED (no similarity fallback). A known-but-restricted product resolves to
RESTRICTED so the planner cannot generate product facts for it.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

from app.v3.snapshot import KnowledgeSnapshot


@dataclass
class ProductResolution:
    status: str                      # VERIFIED | RESTRICTED | PRODUCT_NOT_VERIFIED
    product_id: Optional[str] = None
    kind: Optional[str] = None       # single | bundle
    components: list = field(default_factory=list)
    method: Optional[str] = None     # wb_nm_id | wb_vendor_code | label_ean
    confidence: float = 0.0
    identifiers_seen: dict = field(default_factory=dict)
    restricted_components: list = field(default_factory=list)
    reason: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


def resolve(snapshot: KnowledgeSnapshot, *, nm_id=None, supplier_article=None,
            barcode=None, marketplace: str = "WB") -> ProductResolution:
    seen = {"marketplace": marketplace, "nm_id": nm_id or None,
            "supplier_article": supplier_article or None, "barcode": barcode or None}
    if (marketplace or "WB").upper() != "WB":
        return ProductResolution("PRODUCT_NOT_VERIFIED", identifiers_seen=seen,
                                 reason="UNSUPPORTED_MARKETPLACE")
    candidates = [("wb_nm_id", nm_id), ("wb_vendor_code", supplier_article), ("label_ean", barcode)]
    hits = [(m, snapshot.product_by_identifier(m, v)) for m, v in candidates]
    found = [(m, pid) for m, pid in hits if pid]
    if not found:
        return ProductResolution("PRODUCT_NOT_VERIFIED", identifiers_seen=seen, reason="UNKNOWN_IDENTIFIER")
    method, pid = found[0]
    # identifiers pointing to DIFFERENT products = conflict, never guess
    if len({p for _, p in found}) > 1:
        return ProductResolution("PRODUCT_NOT_VERIFIED", identifiers_seen=seen, method=method,
                                 reason="IDENTIFIER_CONFLICT:" + ",".join(f"{m}->{p}" for m, p in found))
    product = snapshot.product(pid) or {}
    kind = product.get("kind", "single")
    comps = snapshot.components(pid)
    restricted = [c for c in comps if (snapshot.product(c) or {}).get("customer_fact_generation") == "RESTRICTED"]
    status = "RESTRICTED" if kind == "single" and restricted else "VERIFIED"
    return ProductResolution(status=status, product_id=pid, kind=kind,
                             components=comps if kind == "bundle" else [], method=method,
                             confidence=1.0 if method == "wb_nm_id" else 0.9, identifiers_seen=seen,
                             restricted_components=restricted,
                             reason="CUSTOMER_FACT_GENERATION_RESTRICTED" if status == "RESTRICTED" else None)
