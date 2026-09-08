"""Typed view over a product knowledge document."""
from __future__ import annotations

from pydantic import BaseModel

from app.communication_engine.constants import MetaKey, SourceStatus
from app.communication_engine.models.knowledge import KnowledgeDocument


class Product(BaseModel):
    """Structured product facts derived from a `products/*.md` front-matter."""

    model_config = {"frozen": True}

    id: str
    name: str = ""
    line: str = ""
    supplier_articles: tuple[str, ...] = ()   # seller vendorCode(s)
    wb_nm_ids: tuple[str, ...] = ()           # WB nmID(s)
    wb_imt_ids: tuple[str, ...] = ()          # WB imtID (parent card id)
    barcodes: tuple[str, ...] = ()            # WB barcode(s) / GTIN
    id_status: str = "unverified"             # provenance of the identifiers
    aliases: tuple[str, ...] = ()
    actives: tuple[str, ...] = ()
    source_status: SourceStatus = SourceStatus.UNVERIFIED
    verified_facts: tuple[str, ...] = ()    # physical/concentration/regime facts
    verified_claims: tuple[str, ...] = ()   # explicitly approved effect statements
    prohibited_claims: tuple[str, ...] = ()
    volume_ml: float | None = None
    weight_g: float | None = None
    ph: str = ""
    aroma: str = ""
    is_bundle: bool = False
    bundle_components: tuple[str, ...] = ()

    @property
    def articles(self) -> tuple[str, ...]:
        """Back-compat alias: the operational resolution key (supplier articles)."""
        return self.supplier_articles

    @classmethod
    def from_document(cls, doc: KnowledgeDocument) -> "Product":
        meta = doc.metadata
        status_raw = str(meta.get(MetaKey.SOURCE_STATUS.value, SourceStatus.UNVERIFIED.value))
        status = (
            SourceStatus(status_raw)
            if status_raw in {s.value for s in SourceStatus}
            else SourceStatus.UNVERIFIED
        )
        return cls(
            id=doc.doc_id,
            name=str(meta.get(MetaKey.NAME.value, "")),
            line=str(meta.get(MetaKey.LINE.value, "")),
            supplier_articles=tuple(doc.meta_list(MetaKey.SUPPLIER_ARTICLES.value)),
            wb_nm_ids=tuple(doc.meta_list(MetaKey.WB_NM_IDS.value)),
            wb_imt_ids=tuple(doc.meta_list(MetaKey.WB_IMT_IDS.value)),
            barcodes=tuple(doc.meta_list(MetaKey.BARCODES.value)),
            id_status=str(meta.get(MetaKey.ID_STATUS.value, "unverified")),
            aliases=tuple(a.lower() for a in doc.meta_list(MetaKey.ALIASES.value)),
            actives=tuple(doc.meta_list(MetaKey.ACTIVES.value)),
            source_status=status,
            verified_facts=tuple(doc.meta_list(MetaKey.VERIFIED_FACTS.value)),
            verified_claims=tuple(doc.meta_list(MetaKey.VERIFIED_CLAIMS.value)),
            prohibited_claims=tuple(doc.meta_list(MetaKey.PROHIBITED_CLAIMS.value)),
            volume_ml=_as_float(meta.get(MetaKey.VOLUME_ML.value)),
            weight_g=_as_float(meta.get(MetaKey.WEIGHT_G.value)),
            ph=str(meta.get(MetaKey.PH.value, "")),
            aroma=str(meta.get(MetaKey.AROMA.value, "")),
            is_bundle=bool(meta.get(MetaKey.IS_BUNDLE.value, False)),
            bundle_components=tuple(doc.meta_list(MetaKey.BUNDLE_COMPONENTS.value)),
        )


def _as_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
