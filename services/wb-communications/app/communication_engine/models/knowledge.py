"""Knowledge document models."""
from __future__ import annotations

from pydantic import BaseModel, Field

from app.communication_engine.constants import Namespace


class KnowledgeRef(BaseModel):
    """A stable reference to a knowledge document (namespace + id)."""

    model_config = {"frozen": True}

    namespace: Namespace
    doc_id: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.namespace.value}/{self.doc_id}"


class KnowledgeDocument(BaseModel):
    """A parsed markdown knowledge file: YAML front-matter + body."""

    model_config = {"frozen": True}

    namespace: Namespace
    doc_id: str
    version: int = 1
    tags: tuple[str, ...] = ()
    metadata: dict = Field(default_factory=dict)
    body: str = ""

    @property
    def ref(self) -> KnowledgeRef:
        return KnowledgeRef(namespace=self.namespace, doc_id=self.doc_id)

    def meta_list(self, key: str) -> list[str]:
        """Return a front-matter value coerced to a list of strings."""
        value = self.metadata.get(key)
        if value is None:
            return []
        if isinstance(value, (list, tuple)):
            return [str(v) for v in value]
        return [str(value)]
