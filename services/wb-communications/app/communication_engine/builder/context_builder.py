"""ContextBuilder: turns a Classification into an ordered set of knowledge blocks.

It resolves *which* documents apply from data (config + classification), loads
them through the registry, and returns a storage-agnostic ``PromptContext``.
There are no scenario/product ``if`` chains here — the mapping is declarative.
"""
from __future__ import annotations

from app.communication_engine.config import EngineConfig
from app.communication_engine.constants import Namespace
from app.communication_engine.knowledge.registry import KnowledgeError, KnowledgeRegistry
from app.communication_engine.models.classification import Classification
from app.communication_engine.models.knowledge import KnowledgeRef
from app.communication_engine.models.product import Product
from app.communication_engine.models.prompt_context import ContextBlock, PromptContext


class ContextBuilder:
    def __init__(self, registry: KnowledgeRegistry, config: EngineConfig):
        self._registry = registry
        self._config = config

    def _product_refs(self, product_id: str) -> list[KnowledgeRef]:
        """Product ref plus, for a bundle, each component product ref.

        ``bundle_components`` hold internal product_ids (not external numeric ids),
        so they are referenced directly — the internal structure no longer depends
        on ambiguous external identifiers.
        """
        refs = [KnowledgeRef(namespace=Namespace.PRODUCTS, doc_id=product_id)]
        try:
            product = self._registry.get_product(product_id)
        except KnowledgeError:
            return refs  # missing product doc — programming/schema errors still propagate
        for component_id in product.bundle_components:
            if component_id != product_id and self._registry.exists(
                Namespace.PRODUCTS, component_id
            ):
                refs.append(KnowledgeRef(namespace=Namespace.PRODUCTS, doc_id=component_id))
        return refs

    def _refs(self, classification: Classification) -> list[KnowledgeRef]:
        """Declarative resolution: brand core + product(+components) + cases + marketplace."""
        refs: list[KnowledgeRef] = [
            KnowledgeRef(namespace=Namespace.BRAND, doc_id=doc_id)
            for doc_id in self._config.brand_core_docs
        ]
        if classification.product_id:
            refs.extend(self._product_refs(classification.product_id))
        refs.extend(
            KnowledgeRef(namespace=Namespace.CASES, doc_id=case_id)
            for case_id in classification.scenario
        )
        # Only add a marketplace doc for a supported (WB) marketplace. An
        # unsupported/unknown platform (marketplace is None) contributes no doc;
        # such an item is already flagged for manual moderation.
        if classification.marketplace is not None:
            marketplace_doc = self._config.marketplace_docs.get(
                classification.marketplace, classification.marketplace.value
            )
            refs.append(KnowledgeRef(namespace=Namespace.MARKETPLACES, doc_id=marketplace_doc))
        return refs

    def _product_body_and_constraints(self, doc) -> tuple[str, tuple[str, ...]]:
        """Fold YAML verified_facts/verified_claims into the grounding body and
        surface prohibited_claims as constraints — so the structured YAML is the
        real source, not something a human must also repeat in the markdown body.
        Prohibited claims are deliberately kept OUT of the grounding body so the
        numeric validator never 'grounds' a forbidden example number.
        """
        product = Product.from_document(doc)
        body = doc.body
        if product.verified_facts:
            body += "\n\nПодтверждённые факты (можно называть): " + "; ".join(product.verified_facts)
        if product.verified_claims:
            body += "\nПодтверждённые свойства: " + "; ".join(product.verified_claims)
        return body, product.prohibited_claims

    def build(self, classification: Classification) -> PromptContext:
        order = {ns: i for i, ns in enumerate(self._config.section_order)}
        blocks: list[ContextBlock] = []
        for ref in self._refs(classification):
            if not self._registry.exists(ref.namespace, ref.doc_id):
                continue  # missing optional doc — skip rather than fail the whole prompt
            doc = self._registry.get(ref)
            body, constraints = doc.body, ()
            if ref.namespace is Namespace.PRODUCTS:
                body, constraints = self._product_body_and_constraints(doc)
            blocks.append(
                ContextBlock(
                    ref=ref,
                    namespace=ref.namespace,
                    title=self._config.section_titles.get(ref.namespace, ref.namespace.value),
                    body=body,
                    version=doc.version,
                    constraints=constraints,
                )
            )
        blocks.sort(key=lambda b: order.get(b.namespace, len(order)))
        return PromptContext(classification=classification, blocks=tuple(blocks))
