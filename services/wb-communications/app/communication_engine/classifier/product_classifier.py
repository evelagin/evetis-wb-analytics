"""Product classifier with a priority resolution chain.

Order: supplier_article -> nm_id -> barcode -> alias/product_name. If nothing
resolves the classification is marked ``unresolved`` so the pipeline can route to
manual moderation instead of answering with only brand knowledge.
"""
from __future__ import annotations

from pydantic import BaseModel

from app.communication_engine.constants import ResolutionStatus
from app.communication_engine.knowledge.registry import KnowledgeRegistry
from app.communication_engine.models.classification import ReviewInput


class ProductResolution(BaseModel):
    model_config = {"frozen": True}

    product_id: str | None
    status: ResolutionStatus
    method: str  # article | nm_id | barcode | alias | none


class ProductClassifier:
    def __init__(self, registry: KnowledgeRegistry):
        self._registry = registry

    def classify(self, review: ReviewInput) -> ProductResolution:
        pid = self._registry.product_id_for_article(review.supplier_article)
        if pid:
            return ProductResolution(product_id=pid, status=ResolutionStatus.RESOLVED, method="article")
        pid = self._registry.product_id_for_nm(review.nm_id)
        if pid:
            return ProductResolution(product_id=pid, status=ResolutionStatus.RESOLVED, method="nm_id")
        pid = self._registry.product_id_for_barcode(review.barcode)
        if pid:
            return ProductResolution(product_id=pid, status=ResolutionStatus.RESOLVED, method="barcode")
        pid = self._registry.product_id_for_name(review.product_name)
        if pid:
            return ProductResolution(product_id=pid, status=ResolutionStatus.RESOLVED, method="alias")
        return ProductResolution(product_id=None, status=ResolutionStatus.UNRESOLVED, method="none")
