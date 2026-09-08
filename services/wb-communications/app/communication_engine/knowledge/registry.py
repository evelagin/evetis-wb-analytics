"""Knowledge registry: the single indirection between business logic and storage.

`ContextBuilder`, classifiers and validators never touch the filesystem — they
ask the ``KnowledgeRegistry``. Swapping Markdown for a database or CMS later is a
matter of providing a different ``KnowledgeSource``; nothing else changes.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

import yaml
from pydantic import BaseModel

from app.communication_engine.config import EngineConfig
from app.communication_engine.constants import MetaKey, Namespace, SourceStatus, Urgency
from app.communication_engine.models.knowledge import KnowledgeDocument, KnowledgeRef
from app.communication_engine.models.product import Product


class KnowledgeError(Exception):
    """Raised when a knowledge document is missing or malformed."""


class ScenarioRule(BaseModel):
    model_config = {"frozen": True}

    case_id: str
    keywords: tuple[str, ...]
    urgency: Urgency = Urgency.NORMAL
    priority: int = 100


# --------------------------------------------------------------------------- #
# Sources
# --------------------------------------------------------------------------- #
class KnowledgeSource(ABC):
    """Abstract backend. Implementations must be side-effect free on read."""

    @abstractmethod
    def load(self, namespace: Namespace, doc_id: str) -> KnowledgeDocument: ...

    @abstractmethod
    def exists(self, namespace: Namespace, doc_id: str) -> bool: ...

    @abstractmethod
    def list_ids(self, namespace: Namespace) -> list[str]: ...


def parse_markdown(namespace: Namespace, doc_id: str, raw: str) -> KnowledgeDocument:
    """Parse `--- yaml --- body` markdown into a KnowledgeDocument."""
    metadata: dict = {}
    body = raw
    if raw.lstrip().startswith("---"):
        stripped = raw.lstrip()
        parts = stripped.split("---", 2)
        # parts = ['', '<yaml>', '<body>']
        if len(parts) >= 3:
            metadata = yaml.safe_load(parts[1]) or {}
            body = parts[2].lstrip("\n")
    if not isinstance(metadata, dict):
        raise KnowledgeError(f"{namespace.value}/{doc_id}: front-matter is not a mapping")
    declared_id = metadata.get(MetaKey.ID.value)
    if declared_id is not None and str(declared_id) != doc_id:
        raise KnowledgeError(
            f"{namespace.value}/{doc_id}: front-matter id '{declared_id}' != filename"
        )
    tags = metadata.get(MetaKey.TAGS.value) or []
    return KnowledgeDocument(
        namespace=namespace,
        doc_id=doc_id,
        version=int(metadata.get(MetaKey.VERSION.value, 1)),
        tags=tuple(str(t) for t in tags),
        metadata=metadata,
        body=body.strip(),
    )


class MarkdownKnowledgeSource(KnowledgeSource):
    def __init__(self, config: EngineConfig):
        self._config = config

    def load(self, namespace: Namespace, doc_id: str) -> KnowledgeDocument:
        path = self._config.doc_path(namespace, doc_id)
        if not path.is_file():
            raise KnowledgeError(f"knowledge document not found: {namespace.value}/{doc_id}")
        return parse_markdown(namespace, doc_id, path.read_text(encoding="utf-8"))

    def exists(self, namespace: Namespace, doc_id: str) -> bool:
        return self._config.doc_path(namespace, doc_id).is_file()

    def list_ids(self, namespace: Namespace) -> list[str]:
        directory = self._config.namespace_dir(namespace)
        if not directory.is_dir():
            return []
        ext = self._config.file_extension
        return sorted(p.name[: -len(ext)] for p in directory.glob(f"*{ext}"))


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
class KnowledgeRegistry:
    """Loads, caches and indexes knowledge documents through a source backend."""

    def __init__(self, source: KnowledgeSource, config: EngineConfig):
        self._source = source
        self._config = config
        self._cache: dict[str, KnowledgeDocument] = {}
        self._article_index: dict[str, str] | None = None
        self._scenario_rules: list[ScenarioRule] | None = None
        self._active_refs: set[str] = set()

    # --- basic access ---
    def get(self, ref: KnowledgeRef) -> KnowledgeDocument:
        key = str(ref)
        cached = self._cache.get(key)
        if cached is None:
            cached = self._source.load(ref.namespace, ref.doc_id)
            self._cache[key] = cached
        return cached

    def get_document(self, namespace: Namespace, doc_id: str) -> KnowledgeDocument:
        return self.get(KnowledgeRef(namespace=namespace, doc_id=doc_id))

    def exists(self, namespace: Namespace, doc_id: str) -> bool:
        return self._source.exists(namespace, doc_id)

    # --- explicit registration (validates + warms cache) ---
    def _register(self, namespace: Namespace, doc_id: str) -> KnowledgeDocument:
        doc = self.get_document(namespace, doc_id)  # raises if missing
        self._active_refs.add(str(doc.ref))
        return doc

    def register_brand(self, doc_id: str) -> KnowledgeDocument:
        return self._register(Namespace.BRAND, doc_id)

    def register_product(self, doc_id: str) -> Product:
        return Product.from_document(self._register(Namespace.PRODUCTS, doc_id))

    def register_case(self, doc_id: str) -> KnowledgeDocument:
        return self._register(Namespace.CASES, doc_id)

    def register_marketplace(self, doc_id: str) -> KnowledgeDocument:
        return self._register(Namespace.MARKETPLACES, doc_id)

    @property
    def active_refs(self) -> set[str]:
        return set(self._active_refs)

    # --- products & article index ---
    def products(self) -> list[Product]:
        return [
            Product.from_document(self.get_document(Namespace.PRODUCTS, pid))
            for pid in self._source.list_ids(Namespace.PRODUCTS)
        ]

    def article_index(self) -> dict[str, str]:
        if self._article_index is None:
            index: dict[str, str] = {}
            for product in self.products():
                for article in product.articles:
                    index[str(article).strip()] = product.id
            self._article_index = index
        return dict(self._article_index)

    def nm_id_index(self) -> dict[str, str]:
        index: dict[str, str] = {}
        for product in self.products():
            for nm in product.wb_nm_ids:
                index[str(nm).strip()] = product.id
        return index

    def barcode_index(self) -> dict[str, str]:
        index: dict[str, str] = {}
        for product in self.products():
            for barcode in product.barcodes:
                index[str(barcode).strip()] = product.id
        return index

    def alias_index(self) -> dict[str, str]:
        """Map alias -> product_id, dropping aliases that are ambiguous."""
        seen: dict[str, str] = {}
        ambiguous: set[str] = set()
        for product in self.products():
            candidates = set(product.aliases) | {product.name.lower()} if product.name else set(product.aliases)
            for alias in candidates:
                alias = alias.strip().lower()
                if not alias:
                    continue
                if alias in seen and seen[alias] != product.id:
                    ambiguous.add(alias)
                seen[alias] = product.id
        return {a: pid for a, pid in seen.items() if a not in ambiguous}

    def product_id_for_article(self, article: str | int | None) -> str | None:
        if article in (None, ""):
            return None
        return self.article_index().get(str(article).strip())

    def product_id_for_nm(self, nm_id: str | int | None) -> str | None:
        if nm_id in (None, ""):
            return None
        return self.nm_id_index().get(str(nm_id).strip())

    def product_id_for_barcode(self, barcode: str | int | None) -> str | None:
        if barcode in (None, ""):
            return None
        return self.barcode_index().get(str(barcode).strip())

    def product_id_for_name(self, product_name: str | None) -> str | None:
        """Unique alias/name substring match against a free-text product name."""
        if not product_name:
            return None
        text = product_name.strip().lower()
        matches = {pid for alias, pid in self.alias_index().items() if alias and alias in text}
        return next(iter(matches)) if len(matches) == 1 else None

    def known_actives(self) -> dict[str, set[str]]:
        """Map product_id -> set of lowercased active names (bundles excluded)."""
        return {p.id: {a.lower() for a in p.actives} for p in self.products() if not p.is_bundle}

    def allowed_actives(self, product_id: str) -> set[str]:
        """Actives a product may legitimately mention.

        Single product -> its own actives. Bundle -> the union of its components'
        actives, so a set answer may name any component's ingredient but not an
        active from a different line.
        """
        by_product = self.known_actives()
        try:
            product = self.get_product(product_id)
        except KnowledgeError:
            return set()
        if not product.is_bundle:
            return by_product.get(product_id, set())
        allowed: set[str] = set()
        # bundle_components are internal product_ids (not external numeric ids).
        for component_id in product.bundle_components:
            allowed |= by_product.get(component_id, set())
        return allowed

    def get_product(self, product_id: str) -> Product:
        return Product.from_document(self.get_document(Namespace.PRODUCTS, product_id))

    def validate_schema(self) -> list[str]:
        """Knowledge-base integrity check. Returns a list of problems (empty = ok)."""
        problems: list[str] = []
        seen_articles: dict[str, str] = {}
        seen_nm: dict[str, str] = {}
        seen_barcodes: dict[str, str] = {}
        products: list[Product] = []
        all_ids = set(self._source.list_ids(Namespace.PRODUCTS))
        for pid in sorted(all_ids):
            doc = self.get_document(Namespace.PRODUCTS, pid)
            meta = doc.metadata
            if MetaKey.SOURCE_STATUS.value not in meta:
                problems.append(f"{pid}: missing source_status")
            else:
                status = str(meta[MetaKey.SOURCE_STATUS.value])
                if status not in {s.value for s in SourceStatus}:
                    problems.append(f"{pid}: invalid source_status '{status}'")
            if MetaKey.VERSION.value not in meta:
                problems.append(f"{pid}: missing version")
            product = Product.from_document(doc)
            products.append(product)
            for article in product.articles:
                if article in seen_articles and seen_articles[article] != pid:
                    problems.append(f"duplicate article {article}: {seen_articles[article]} & {pid}")
                seen_articles[article] = pid
            for nm in product.wb_nm_ids:
                if nm in seen_nm and seen_nm[nm] != pid:
                    problems.append(f"duplicate nm_id {nm}: {seen_nm[nm]} & {pid}")
                seen_nm[nm] = pid
            for barcode in product.barcodes:
                if barcode in seen_barcodes and seen_barcodes[barcode] != pid:
                    problems.append(
                        f"duplicate barcode {barcode}: {seen_barcodes[barcode]} & {pid}"
                    )
                seen_barcodes[barcode] = pid
        # bundle components must reference existing internal product_ids, contain
        # no duplicates, and never point at the bundle itself.
        for product in products:
            if not product.bundle_components:
                continue
            seen_components: set[str] = set()
            for component_id in product.bundle_components:
                if component_id == product.id:
                    problems.append(f"{product.id}: bundle_components references itself")
                elif component_id not in all_ids:
                    problems.append(
                        f"{product.id}: bundle component '{component_id}' does not exist"
                    )
                if component_id in seen_components:
                    problems.append(
                        f"{product.id}: duplicate bundle component '{component_id}'"
                    )
                seen_components.add(component_id)
        return problems

    # --- scenario rules ---
    def scenario_rules(self) -> list[ScenarioRule]:
        if self._scenario_rules is None:
            rules: list[ScenarioRule] = []
            for case_id in self._source.list_ids(Namespace.CASES):
                doc = self.get_document(Namespace.CASES, case_id)
                keywords = tuple(k.lower() for k in doc.meta_list(MetaKey.KEYWORDS.value))
                urgency_raw = str(doc.metadata.get(MetaKey.URGENCY.value, Urgency.NORMAL.value))
                urgency = Urgency(urgency_raw) if urgency_raw in {u.value for u in Urgency} else Urgency.NORMAL
                priority = int(doc.metadata.get(MetaKey.PRIORITY.value, 100))
                rules.append(ScenarioRule(case_id=case_id, keywords=keywords,
                                          urgency=urgency, priority=priority))
            rules.sort(key=lambda r: r.priority)
            self._scenario_rules = rules
        return list(self._scenario_rules)

    # --- brand helpers for validators ---
    def brand_meta_list(self, doc_id: str, key: str) -> list[str]:
        try:
            return self.get_document(Namespace.BRAND, doc_id).meta_list(key)
        except KnowledgeError:
            return []
